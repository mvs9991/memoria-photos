"""Search execution: parsed query -> SQL candidate set -> semantic ranking -> results."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from .. import db
from ..vectors import IndexCache, normalize
from .parser import ParsedQuery, parse

log = logging.getLogger(__name__)

# SigLIP's absolute match probability is tiny for short queries (p99 ~0.001 on COCO),
# so an absolute cut-off would return a handful of photos and hide the rest. Results are
# cut by where the similarity distribution falls off instead.
# Where to stop a visual result set. Swept against COCO val2017 instance labels
# (eval/eval_visual_search.py, 20 queries, 18k-photo library): the sigma-only cut
# (head 0.0) gives precision/recall 0.39/0.88, the chosen value 0.78/0.77, and
# p@20 stays at 0.97 throughout — this trims the tail, it does not reorder.
# F1 peaks across 0.60-0.65; the lower end is preferred because in a photo
# library a missing photo is worse than an extra one.
SEM_SIGMA = 1.6               # keep photos this many std devs above the library mean
SEM_HEAD_FRACTION = 0.60      # ...and at least this fraction of the best matches' margin
SEM_MIN_RESULTS = 40
SEM_MAX_RESULTS = 800


@dataclass
class SearchResult:
    photo_ids: list[int] = field(default_factory=list)
    scores: dict[int, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    places: list[dict] = field(default_factory=list)
    interpretation: list[dict] = field(default_factory=list)
    total: int = 0
    took_ms: int = 0
    result_type: str = "photos"
    query: str = ""
    explanation: str = ""


class SearchEngine:
    def __init__(self, ctx, index_cache: IndexCache | None = None):
        self.ctx = ctx
        self.index_cache = index_cache or IndexCache()

    # ------------------------------------------------------------------ SQL candidates
    def _candidate_sql(self, conn: sqlite3.Connection, q: ParsedQuery, limit: int | None = None
                       ) -> tuple[str, list]:
        where = ["p.status = 'ok'", "p.hidden = 0"]
        args: list = []
        for pid in q.persons_all:
            where.append("EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id = ?)")
            args.append(pid)
        if q.persons_any:
            marks = ",".join("?" * len(q.persons_any))
            where.append(f"EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id IN ({marks}))")
            args.extend(q.persons_any)
        if q.place_ids:
            ids = set(q.place_ids)
            expanded = self._expand_places(conn, ids)
            marks = ",".join("?" * len(expanded))
            where.append(f"p.place_id IN ({marks})")
            args.extend(expanded)
        if q.event_ids:
            marks = ",".join("?" * len(q.event_ids))
            where.append(f"(p.event_id IN ({marks}) OR EXISTS (SELECT 1 FROM trip_photos tp "
                         f"WHERE tp.photo_id = p.id AND tp.trip_id IN ({marks})))")
            args.extend(q.event_ids)
            args.extend(q.event_ids)
        if q.date.start is not None:
            where.append("p.taken_ts >= ?")
            args.append(q.date.start)
        if q.date.end is not None:
            where.append("p.taken_ts <= ?")
            args.append(q.date.end)
        if q.date.month_only:
            where.append("CAST(strftime('%m', p.taken_ts, 'unixepoch') AS INT) = ?")
            args.append(q.date.month_only)
        if q.tags:
            for tag in q.tags:
                where.append("EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                             "WHERE pt.photo_id = p.id AND t.name = ? AND pt.score >= 2.0)")
                args.append(str(tag))
        if q.only_favorites:
            where.append("p.favorite = 1")
        if q.only_screenshots:
            where.append("p.source_kind = 'screenshot'")
        elif q.exclude_screenshots and (q.semantic_text or q.sort == "quality"):
            where.append("COALESCE(p.source_kind,'') != 'screenshot'")
        if q.only_selfies:
            where.append("EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND (f.x2 - f.x1) > 0.22) "
                         "AND p.face_count <= 2")
        if q.require_faces:
            where.append("p.face_count > 0")
        sql = f"SELECT p.id FROM photos p WHERE {' AND '.join(where)}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return sql, args

    _places_cache: tuple[int, list] | None = None

    @classmethod
    def _all_places(cls, conn: sqlite3.Connection) -> list:
        """Places change only when photos are geocoded, so cache the table."""
        gen = int(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0])
        if cls._places_cache is None or cls._places_cache[0] != gen:
            rows = conn.execute("SELECT id, name, city, admin1, country FROM places").fetchall()
            cls._places_cache = (gen, [(r["id"], r["name"], r["city"], r["admin1"], r["country"]) for r in rows])
        return cls._places_cache[1]

    @classmethod
    def _expand_places(cls, conn: sqlite3.Connection, ids: set[int]) -> list[int]:
        """A city query should also match its neighbourhoods; a state should match its cities."""
        rows = conn.execute(
            f"SELECT id, name, city, admin1, country FROM places WHERE id IN ({','.join('?' * len(ids))})",
            tuple(ids)).fetchall()
        names = {r["name"] for r in rows}
        cities = {r["city"] for r in rows if r["city"]}
        admins = {r["admin1"] for r in rows if r["admin1"]}
        countries = {r["country"] for r in rows if r["country"]}
        admin_targets = names & admins
        country_targets = names & countries
        out = set(ids)
        for pid, name, city, admin1, country in cls._all_places(conn):
            if (city and (city in names or city in cities)) or (name in cities) \
                    or (admin1 and admin1 in admin_targets) or (country and country in country_targets):
                out.add(pid)
        return sorted(out)

    # ------------------------------------------------------------------ main entry
    def search(self, conn: sqlite3.Connection, query: str, limit: int = 500,
               use_llm: bool | None = None) -> SearchResult:
        t0 = time.time()
        q = parse(query, conn, me_person_id=self.ctx.settings.me_person_id)

        if q.is_empty() and query.strip():
            q = self._maybe_llm_parse(conn, query, q, use_llm)
        if q.is_empty() and query.strip():
            return self._keyword_fallback(conn, query, t0)

        res = SearchResult(interpretation=q.interpretation, result_type=q.result_type, query=query)
        structured = bool(q.persons_all or q.persons_any or q.place_ids or q.event_ids
                          or q.date.start or q.date.end or q.date.month_only or q.only_favorites
                          or q.only_screenshots or q.only_selfies)
        # "someone playing tennis" matches the broad tag "playing" and leaves "tennis"
        # as a residue. Using the tag as a hard filter there throws away the word that
        # actually identifies the photo, so with nothing structured to anchor the query
        # the tag becomes advisory and the whole phrase drives the visual search.
        if q.tags and q.unmatched and not structured:
            q.interpretation = [i for i in q.interpretation if i.get("kind") != "tag"]
            q.interpretation.append({"kind": "visual", "label": query.strip(),
                                     "detail": "searched visually for the whole phrase"})
            q.tags = []
            q.semantic_text = query.strip()
            res.interpretation = q.interpretation
        sql, args = self._candidate_sql(conn, q)
        candidates = [int(r[0]) for r in conn.execute(sql, args)]
        # A bare tag query ("beach photos") should behave like "show me beach-looking
        # photos", not "photos where the beach tag cleared a strict threshold". Widen
        # the candidate set with the tag's weaker matches plus visual neighbours.
        if q.tags and not q.unmatched:
            widened = self._widen_by_tag(conn, q, candidates)
            if widened:
                candidates = widened
        if q.semantic_text and candidates:
            ranked, scores = self._semantic_rank(conn, q.semantic_text, candidates, limit,
                                                 structured=structured)
            res.photo_ids, res.scores = ranked, scores
            # A tag that almost nothing matched shouldn't shrink the answer to nothing:
            # fall back to ranking the whole library visually.
            if not structured and len(ranked) < 20:
                everything = [int(r[0]) for r in conn.execute(
                    "SELECT id FROM photos WHERE status='ok' AND hidden=0 "
                    "AND COALESCE(source_kind,'') != 'screenshot'")]
                ranked, scores = self._semantic_rank(conn, q.semantic_text, everything, limit)
                res.photo_ids, res.scores = ranked, scores
                if q.tags:
                    q.interpretation.append({"kind": "visual", "label": ", ".join(str(t) for t in q.tags),
                                             "detail": "few tagged photos — ranked the library visually"})
        else:
            res.photo_ids = self._order(conn, candidates, q, limit)
        res.total = len(res.photo_ids)

        if q.result_type in ("events", "people", "places"):
            self._aggregate_entities(conn, q, res)
        else:
            # Always surface matching people/events as context cards.
            self._context_cards(conn, q, res)
        res.took_ms = int((time.time() - t0) * 1000)
        res.explanation = _explain(q, res)
        return res

    def _order(self, conn: sqlite3.Connection, candidates: list[int], q: ParsedQuery, limit: int) -> list[int]:
        if not candidates:
            return []
        marks = ",".join("?" * len(candidates)) if len(candidates) <= 5000 else None
        if q.sort == "quality":
            if marks:
                rows = conn.execute(
                    f"SELECT id FROM photos WHERE id IN ({marks}) ORDER BY COALESCE(quality_score,0) DESC, taken_ts DESC"
                    f" LIMIT ?", (*candidates, limit * 3)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM photos WHERE status='ok' ORDER BY COALESCE(quality_score,0) DESC LIMIT ?",
                    (limit * 3,)).fetchall()
            ids = [int(r[0]) for r in rows]
            return self._diversify(conn, ids, limit)
        order = "ASC" if q.sort == "date_asc" else "DESC"
        if marks:
            rows = conn.execute(
                f"SELECT id FROM photos WHERE id IN ({marks}) ORDER BY taken_ts {order} LIMIT ?",
                (*candidates, limit)).fetchall()
        else:
            cand = set(candidates)
            rows = [r for r in conn.execute(f"SELECT id FROM photos WHERE status='ok' ORDER BY taken_ts {order}")
                    if int(r[0]) in cand][:limit]
        return [int(r[0]) for r in rows]

    def _diversify(self, conn: sqlite3.Connection, ids: list[int], limit: int, sim_cut: float = 0.93) -> list[int]:
        """Drop near-identical shots so 'best photos' isn't ten frames of one burst."""
        if len(ids) <= 1:
            return ids[:limit]
        model_id = db.active_model_id(conn, "semantic")
        if not model_id:
            return ids[:limit]
        gen = int(db.get_meta(conn, "gen:embeddings", 0) or 0)
        index = self.index_cache.get(conn, model_id, gen)
        row_of = index.id_to_row()
        chosen: list[int] = []
        chosen_vecs: list[np.ndarray] = []
        for pid in ids:
            r = row_of.get(pid)
            if r is None:
                chosen.append(pid)
            else:
                v = index.mat[r]
                if chosen_vecs and max(float(v @ c) for c in chosen_vecs[-40:]) > sim_cut:
                    continue
                chosen.append(pid)
                chosen_vecs.append(v)
            if len(chosen) >= limit:
                break
        return chosen

    def _widen_by_tag(self, conn: sqlite3.Connection, q: ParsedQuery, candidates: list[int]) -> list[int]:
        """Re-run the candidate query with a lower tag threshold (recall over precision)."""
        relaxed = ParsedQuery(**{**q.__dict__, "tags": []})
        sql, args = self._candidate_sql(conn, relaxed)
        base = [int(r[0]) for r in conn.execute(sql, args)]
        if not base:
            return candidates
        marks = ",".join("?" * len(q.tags))
        weak = {int(r[0]) for r in conn.execute(
            f"""SELECT DISTINCT pt.photo_id FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
                WHERE t.name IN ({marks}) AND pt.score >= 1.2""", [str(t) for t in q.tags])}
        base_set = set(base)
        out = set(candidates) | (weak & base_set)
        return sorted(out) if out else candidates

    def _semantic_rank(self, conn: sqlite3.Connection, text: str, candidates: list[int], limit: int,
                       structured: bool = False) -> tuple[list[int], dict[int, float]]:
        model_id = db.active_model_id(conn, "semantic")
        if not model_id:
            return candidates[:limit], {}
        gen = int(db.get_meta(conn, "gen:embeddings", 0) or 0)
        index = self.index_cache.get(conn, model_id, gen)
        if len(index.ids) == 0:
            return candidates[:limit], {}
        model = self.ctx.semantic_model()
        qvec = normalize(model.encode_texts([f"a photo of {text}", text]).mean(0, keepdims=True))[0]
        row_of = index.id_to_row()
        rows = np.array([row_of[p] for p in candidates if p in row_of], dtype=np.int64)
        ids = np.array([p for p in candidates if p in row_of], dtype=np.int64)
        if len(rows) == 0:
            return [], {}
        sims = index.mat[rows] @ qvec
        order = np.argsort(-sims)

        if structured:
            # A person/place/date filter already defines the answer; similarity only orders it.
            keep = list(order[:limit])
        else:
            # Cut where this query's similarities stop standing out from the library baseline.
            baseline = index.mat @ qvec if len(index.ids) <= 200_000 else sims
            mu, sd = float(baseline.mean()), float(baseline.std() + 1e-6)
            # A fixed sigma keeps a fixed *fraction* of the library, which is wrong:
            # "elephants" and "a bus" then return the same number of photos however
            # many actually match. Also cut relative to how strong this query's best
            # matches are, so a query with a small tight match set stops early.
            head = float(np.mean(sims[order[:max(3, len(order) // 200)]]))
            cut = max(mu + SEM_SIGMA * sd, mu + SEM_HEAD_FRACTION * (head - mu))
            keep = [i for i in order if sims[i] >= cut]
            if len(keep) < min(SEM_MIN_RESULTS, len(order)):
                keep = list(order[: min(SEM_MIN_RESULTS, len(order))])
            keep = keep[: min(limit, SEM_MAX_RESULTS)]
        probs = model.probability(sims)
        return [int(ids[i]) for i in keep], {int(ids[i]): round(float(probs[i]), 5) for i in keep}

    # ------------------------------------------------------------------ entity results
    def _aggregate_entities(self, conn: sqlite3.Connection, q: ParsedQuery, res: SearchResult) -> None:
        from ..engine.events import event_title

        if q.result_type == "events":
            ids = res.photo_ids
            if ids:
                marks = ",".join("?" * min(len(ids), 900))
                rows = conn.execute(
                    f"""SELECT e.*, COUNT(p.id) AS matched FROM events e JOIN photos p ON p.event_id = e.id
                        WHERE p.id IN ({marks}) GROUP BY e.id ORDER BY matched DESC, e.start_ts DESC""",
                    ids[:900]).fetchall()
            else:
                sql, args = self._candidate_sql(conn, q)
                rows = conn.execute(
                    f"""SELECT e.*, COUNT(p.id) AS matched FROM events e JOIN photos p ON p.event_id = e.id
                        WHERE p.id IN ({sql}) GROUP BY e.id ORDER BY matched DESC""", args).fetchall()
            for r in rows:
                if q.trips_only and r["kind"] != "trip":
                    parent = r["parent_id"]
                    if not parent:
                        continue
                res.events.append({
                    "id": r["id"], "title": event_title(r), "kind": r["kind"], "start_ts": r["start_ts"],
                    "end_ts": r["end_ts"], "photo_count": r["photo_count"], "matched": r["matched"],
                    "cover_photo_id": r["cover_photo_id"], "category": r["category"],
                })
            if q.trips_only:
                trip_ids = {e["id"] for e in res.events if e["kind"] == "trip"}
                parents = {r["parent_id"] for r in rows if r["parent_id"]}
                for tid in parents - trip_ids:
                    tr = conn.execute("SELECT * FROM events WHERE id=?", (tid,)).fetchone()
                    if tr:
                        res.events.append({
                            "id": tr["id"], "title": event_title(tr), "kind": "trip", "start_ts": tr["start_ts"],
                            "end_ts": tr["end_ts"], "photo_count": tr["photo_count"], "matched": tr["photo_count"],
                            "cover_photo_id": tr["cover_photo_id"], "category": "trip"})
                res.events = [e for e in res.events if e["kind"] == "trip"]
                res.events.sort(key=lambda e: -e["start_ts"])
        elif q.result_type == "people":
            from ..engine.people import person_label

            ids = res.photo_ids[:900]
            if ids:
                marks = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"""SELECT pr.*, COUNT(DISTINCT f.photo_id) n FROM persons pr JOIN faces f ON f.person_id = pr.id
                        WHERE f.photo_id IN ({marks}) AND pr.merged_into IS NULL AND pr.ignored = 0
                        GROUP BY pr.id ORDER BY n DESC""", ids).fetchall()
                res.people = [{"id": r["id"], "label": person_label(r), "cover_face_id": r["cover_face_id"],
                               "photo_count": r["photo_count"], "matched": r["n"]} for r in rows]
        elif q.result_type == "places":
            ids = res.photo_ids[:900]
            if ids:
                marks = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"""SELECT pl.*, COUNT(p.id) n FROM places pl JOIN photos p ON p.place_id = pl.id
                        WHERE p.id IN ({marks}) GROUP BY pl.id ORDER BY n DESC""", ids).fetchall()
                res.places = [{"id": r["id"], "name": r["name"], "city": r["city"], "admin1": r["admin1"],
                               "country": r["country"], "lat": r["lat"], "lon": r["lon"], "matched": r["n"]}
                              for r in rows]

    def _context_cards(self, conn: sqlite3.Connection, q: ParsedQuery, res: SearchResult) -> None:
        from ..engine.events import event_title
        from ..engine.people import person_label

        for pid in (q.persons_all + q.persons_any)[:4]:
            r = conn.execute("SELECT * FROM persons WHERE id=? AND merged_into IS NULL", (pid,)).fetchone()
            if r:
                res.people.append({"id": r["id"], "label": person_label(r), "cover_face_id": r["cover_face_id"],
                                   "photo_count": r["photo_count"]})
        if res.photo_ids:
            marks = ",".join("?" * min(len(res.photo_ids), 900))
            rows = conn.execute(
                f"""SELECT e.*, COUNT(p.id) matched FROM events e JOIN photos p ON p.event_id = e.id
                    WHERE p.id IN ({marks}) GROUP BY e.id ORDER BY matched DESC LIMIT 20""",
                res.photo_ids[:900]).fetchall()
            # Only show an event if a real share of it matched, otherwise every large
            # event turns up for every query.
            res.events = [{"id": r["id"], "title": event_title(r), "kind": r["kind"], "start_ts": r["start_ts"],
                           "end_ts": r["end_ts"], "photo_count": r["photo_count"], "matched": r["matched"],
                           "cover_photo_id": r["cover_photo_id"], "category": r["category"]}
                          for r in rows
                          if r["matched"] >= 3 and r["matched"] >= 0.12 * max(r["photo_count"], 1)][:6]

    # ------------------------------------------------------------------ fallbacks
    def _keyword_fallback(self, conn: sqlite3.Connection, query: str, t0: float) -> SearchResult:
        res = SearchResult(result_type="photos", query=query)
        terms = " ".join(f'"{t}"' for t in query.split() if t.strip())
        try:
            rows = conn.execute(
                "SELECT rowid FROM photo_fts WHERE photo_fts MATCH ? ORDER BY rank LIMIT 500", (terms,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        res.photo_ids = [int(r[0]) for r in rows]
        res.total = len(res.photo_ids)
        if res.photo_ids:
            res.interpretation = [{"kind": "keyword", "label": query, "detail": "filename / folder / caption match"}]
        res.took_ms = int((time.time() - t0) * 1000)
        res.explanation = "Keyword match" if res.photo_ids else "No matches"
        return res

    def _maybe_llm_parse(self, conn: sqlite3.Connection, query: str, q: ParsedQuery, use_llm: bool | None):
        enabled = self.ctx.settings.llm_enabled if use_llm is None else use_llm
        if not enabled:
            return q
        try:
            from .llm import llm_parse

            parsed = llm_parse(self.ctx, conn, query)
            if parsed is not None:
                parsed.source = "llm"
                return parsed
        except Exception as exc:
            log.warning("LLM query parsing failed: %s", exc)
        return q


def _explain(q: ParsedQuery, res: SearchResult) -> str:
    bits = []
    if q.person_labels:
        names = [q.person_labels[p] for p in q.persons_all] or [q.person_labels[p] for p in q.persons_any]
        joiner = " and " if q.persons_all and len(names) > 1 else " or "
        bits.append(joiner.join(names))
    if q.place_label:
        bits.append(f"in {q.place_label}")
    if q.event_label:
        bits.append(f"during {q.event_label}")
    if q.date.label:
        bits.append(q.date.label)
    if q.semantic_text:
        bits.append(f"looking like “{q.semantic_text}”")
    if not bits:
        return f"{res.total} results"
    return f"{res.total} results · " + " · ".join(bits)
