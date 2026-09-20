"""Natural-language query parsing.

Deterministic and explainable: the query is matched against the library's own
vocabulary (people, places, events, tags) plus date grammar, and whatever is left
becomes the semantic (visual) part of the query. Every decision is returned as an
interpretation chip so the UI can show *why* results came back.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from ..metadata import naive_to_ts

STOPWORDS = {
    "show", "me", "my", "the", "a", "an", "of", "in", "at", "on", "from", "with", "and", "or", "photos",
    "photo", "pictures", "picture", "pics", "pic", "images", "image", "find", "search", "get", "all",
    "some", "any", "taken", "where", "was", "were", "is", "are", "that", "this", "those", "these", "to",
    "please", "i", "we", "us", "there", "his", "her", "their", "our", "shots", "shot", "photograph",
    "photographs", "display", "list", "give", "showing", "containing", "has", "have", "had",
    "together", "both", "along", "each", "other", "me", "see", "look", "looking", "for", "about",
    "which", "what", "when", "who", "whom", "was", "am", "be", "been", "do", "does", "did", "please",
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
SEASONS = {"winter": (12, 2), "spring": (3, 5), "summer": (4, 6), "monsoon": (7, 9), "rainy": (7, 9),
           "autumn": (9, 11), "fall": (9, 11)}

QUALITY_WORDS = {"best", "top", "greatest", "finest", "nicest", "good", "great", "favourite", "favorite"}
BAD_QUALITY_WORDS = {"blurry", "blurred", "bad", "worst"}


@dataclass
class DateRange:
    start: float | None = None
    end: float | None = None
    label: str = ""
    month_only: int | None = None      # e.g. "in January" with no year


@dataclass
class ParsedQuery:
    raw: str
    persons_all: list[int] = field(default_factory=list)   # must all appear in the photo
    persons_any: list[int] = field(default_factory=list)
    person_labels: dict[int, str] = field(default_factory=dict)
    place_ids: list[int] = field(default_factory=list)
    place_label: str | None = None
    event_ids: list[int] = field(default_factory=list)
    event_label: str | None = None
    tags: list[str] = field(default_factory=list)
    date: DateRange = field(default_factory=DateRange)
    semantic_text: str | None = None
    result_type: str = "photos"        # photos | events | people | places
    sort: str = "auto"                 # auto | date_desc | date_asc | quality
    only_favorites: bool = False
    only_screenshots: bool = False
    exclude_screenshots: bool = True
    only_selfies: bool = False
    require_faces: bool = False
    trips_only: bool = False
    unmatched: list[str] = field(default_factory=list)
    interpretation: list[dict] = field(default_factory=list)
    source: str = "rules"

    def is_empty(self) -> bool:
        return not any([self.persons_all, self.persons_any, self.place_ids, self.event_ids, self.tags,
                        self.date.start, self.date.month_only, self.semantic_text, self.only_favorites,
                        self.only_screenshots, self.only_selfies, self.trips_only])

    def chip(self, kind: str, label: str, detail: str | None = None) -> None:
        self.interpretation.append({"kind": kind, "label": label, "detail": detail})


class Vocabulary:
    """Searchable names drawn from the library itself (cached briefly)."""

    def __init__(self, conn: sqlite3.Connection):
        self.persons: dict[str, int] = {}
        self.person_labels: dict[int, str] = {}
        for r in conn.execute("SELECT id, name, display_no FROM persons WHERE merged_into IS NULL"):
            if r["name"]:
                self.persons[r["name"].lower()] = r["id"]
                self.person_labels[r["id"]] = r["name"]
                first = r["name"].split()[0].lower()
                self.persons.setdefault(first, r["id"])
            label = f"person {r['display_no']}" if r["display_no"] else f"person {r['id']}"
            self.persons.setdefault(label, r["id"])
            self.person_labels.setdefault(r["id"], r["name"] or label.title())

        self.places: dict[str, list[int]] = {}
        self.place_labels: dict[int, str] = {}
        for r in conn.execute("SELECT id, name, city, admin1, admin2, country FROM places"):
            self.place_labels[r["id"]] = r["name"]
            for key in (r["name"], r["city"], r["admin1"], r["admin2"], r["country"]):
                if key:
                    self.places.setdefault(key.lower(), []).append(r["id"])

        self.events: dict[str, list[int]] = {}
        for r in conn.execute("SELECT id, auto_title, user_title FROM events"):
            for key in (r["user_title"], r["auto_title"]):
                if key:
                    self.events.setdefault(key.lower(), []).append(r["id"])

        self.tags: set[str] = {r[0].lower() for r in conn.execute("SELECT name FROM tags")}
        self.tag_aliases = {
            "wedding": "wedding", "weddings": "wedding", "marriage": "wedding", "birthday": "birthday",
            "birthdays": "birthday", "beaches": "beach", "sea": "beach", "ocean": "beach",
            "mountain": "mountains", "hills": "mountains", "temples": "temple", "party": "party",
            "parties": "party", "cake": "cake", "food": "food", "dogs": "dog", "cats": "cat",
            "selfies": "selfie", "sunsets": "sunset", "snowy": "snow", "festivals": "festival",
            "graduations": "graduation", "concerts": "concert", "picnics": "picnic",
        }


_vocab_cache: dict[tuple[str, int], tuple[float, Vocabulary]] = {}


def vocabulary_generation(conn: sqlite3.Connection) -> int:
    """Cheap fingerprint of the searchable vocabulary.

    Derived from the DB rather than passed in by callers: renaming a person has to
    make that name searchable immediately, and a caller that forgot to pass its
    generation would otherwise keep serving a stale vocabulary.
    """
    total = 0
    for key in ("gen:people", "gen:events", "gen:places"):
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row and row[0] is not None:
            try:
                total += int(row[0])
            except (TypeError, ValueError):
                pass
    return total


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row else "?"
    except Exception:
        return "?"


def get_vocabulary(conn: sqlite3.Connection, generation: int | None = None, ttl: float = 20.0) -> Vocabulary:
    # Keyed by database as well as generation: two libraries open in one process
    # must never share a vocabulary.
    gen = vocabulary_generation(conn) if generation is None else generation
    key = (_db_key(conn), gen)
    hit = _vocab_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    vocab = Vocabulary(conn)
    _vocab_cache.clear()
    _vocab_cache[key] = (now, vocab)
    return vocab


def _tokenize(text: str) -> list[str]:
    text = text.lower().replace("'s", "")
    return [t for t in re.split(r"[^a-z0-9\-]+", text) if t]


def _fuzzy_person(token: str, vocab: Vocabulary, cutoff: float = 0.84) -> int | None:
    best, best_score = None, cutoff
    for name, pid in vocab.persons.items():
        if abs(len(name) - len(token)) > 3:
            continue
        score = SequenceMatcher(None, name, token).ratio()
        if score > best_score:
            best, best_score = pid, score
    return best


def parse(query: str, conn: sqlite3.Connection, me_person_id: int | None = None,
          generation: int | None = None, now: datetime | None = None) -> ParsedQuery:
    vocab = get_vocabulary(conn, generation)
    q = ParsedQuery(raw=query.strip())
    text = query.strip().lower()
    now = now or datetime.now()
    consumed: set[int] = set()
    tokens = _tokenize(text)
    if not tokens:
        return q

    # ---- intent words -------------------------------------------------------------------
    joined = " " + " ".join(tokens) + " "
    if re.search(r"\b(events?|occasions?)\b", joined):
        q.result_type = "events"
    if re.search(r"\btrips?\b|\bvacations?\b|\bholidays?\b", joined):
        q.result_type = "events"
        q.trips_only = True
    if re.search(r"\bwho\b", joined) or re.search(r"\bpeople\b", joined) and "photos" not in joined:
        q.result_type = "people" if "who" in tokens else q.result_type
    if re.search(r"\bplaces?\b|\bcities\b|\blocations?\b", joined) and "photos" not in joined:
        q.result_type = "places"

    # ---- multi-word vocabulary matching (longest n-gram first) ---------------------------
    max_n = 4
    i = 0
    matched_terms: list[tuple[str, str, object]] = []
    while i < len(tokens):
        if i in consumed:
            i += 1
            continue
        matched = False
        for n in range(min(max_n, len(tokens) - i), 0, -1):
            phrase = " ".join(tokens[i:i + n])
            if phrase in STOPWORDS and n == 1:
                break
            # person
            if phrase in vocab.persons:
                pid = vocab.persons[phrase]
                matched_terms.append(("person", phrase, pid))
            # place
            elif phrase in vocab.places:
                matched_terms.append(("place", phrase, vocab.places[phrase]))
            # event title
            elif phrase in vocab.events and n > 1:
                matched_terms.append(("event", phrase, vocab.events[phrase]))
            # tag
            elif phrase in vocab.tags or phrase in vocab.tag_aliases:
                matched_terms.append(("tag", phrase, vocab.tag_aliases.get(phrase, phrase)))
            else:
                continue
            for k in range(i, i + n):
                consumed.add(k)
            i += n
            matched = True
            break
        if not matched:
            i += 1

    # ---- dates --------------------------------------------------------------------------
    date, date_tokens = _parse_dates(tokens, consumed, now)
    if date:
        q.date = date
        consumed |= date_tokens

    # ---- flags --------------------------------------------------------------------------
    for idx, tok in enumerate(tokens):
        if idx in consumed:
            continue
        if tok in QUALITY_WORDS:
            if tok in ("favourite", "favorite"):
                q.only_favorites = True
                q.chip("filter", "Favourites")
            else:
                q.sort = "quality"
                q.chip("sort", "Best first")
            consumed.add(idx)
        elif tok in BAD_QUALITY_WORDS:
            q.sort = "worst"
            consumed.add(idx)
        elif tok in ("screenshot", "screenshots"):
            q.only_screenshots = True
            q.exclude_screenshots = False
            q.chip("filter", "Screenshots")
            consumed.add(idx)
        elif tok in ("selfie", "selfies"):
            q.only_selfies = True
            q.chip("filter", "Selfies")
            consumed.add(idx)
        elif tok in ("me", "myself") and me_person_id:
            q.persons_all.append(me_person_id)
            q.person_labels[me_person_id] = "You"
            consumed.add(idx)
        elif tok in ("oldest", "earliest"):
            q.sort = "date_asc"
            consumed.add(idx)
        elif tok in ("recent", "latest", "newest"):
            q.sort = "date_desc"
            consumed.add(idx)

    # ---- apply matched vocabulary --------------------------------------------------------
    person_ids = [v for kind, _, v in matched_terms if kind == "person"]
    if person_ids:
        # "A and B" / "A with B" / "together" -> photos containing all of them
        conjunction = bool(re.search(r"\b(and|with|together|both)\b", joined)) and len(person_ids) > 1
        if conjunction or len(person_ids) == 1:
            q.persons_all.extend(person_ids)
        else:
            q.persons_any.extend(person_ids)
        for pid in person_ids:
            q.person_labels[pid] = vocab.person_labels.get(pid, f"Person {pid}")
            q.chip("person", q.person_labels[pid])
    for kind, phrase, value in matched_terms:
        if kind == "place":
            q.place_ids.extend(value)
            q.place_label = phrase.title()
            q.chip("place", phrase.title())
        elif kind == "event":
            q.event_ids.extend(value)
            q.event_label = phrase.title()
            q.chip("event", phrase.title())
        elif kind == "tag":
            q.tags.append(value)
            q.chip("tag", str(value).title())

    if q.date.label:
        q.chip("date", q.date.label)

    # ---- fuzzy person match for unknown capitalised-looking tokens -----------------------
    leftovers = [t for i2, t in enumerate(tokens) if i2 not in consumed and t not in STOPWORDS]
    if not person_ids and leftovers:
        for tok in list(leftovers):
            if len(tok) >= 4 and tok not in vocab.tags:
                pid = _fuzzy_person(tok, vocab)
                if pid is not None:
                    q.persons_all.append(pid)
                    q.person_labels[pid] = vocab.person_labels.get(pid, f"Person {pid}")
                    q.chip("person", q.person_labels[pid], "closest name match")
                    leftovers.remove(tok)
                    break

    # ---- remaining words become the visual query -----------------------------------------
    semantic_words = [t for t in leftovers if t not in STOPWORDS and not t.isdigit()]
    if semantic_words:
        q.semantic_text = " ".join(semantic_words)
        q.unmatched = semantic_words
        q.chip("visual", q.semantic_text, "matched visually")
    elif q.tags:
        # A tag alone also drives visual ranking (better recall than the tag threshold alone).
        q.semantic_text = ", ".join(str(t) for t in q.tags)

    if q.sort == "auto":
        q.sort = "relevance" if q.semantic_text else "date_desc"
    return q


def _parse_dates(tokens: list[str], consumed: set[int], now: datetime) -> tuple[DateRange | None, set[int]]:
    used: set[int] = set()
    text = " ".join(tokens)

    def rng(start: datetime, end: datetime, label: str) -> DateRange:
        return DateRange(naive_to_ts(start), naive_to_ts(end), label)

    # relative
    m = re.search(r"\b(last|past|previous)\s+(year|month|week|summer|winter|spring|monsoon|autumn|fall)\b", text)
    if m:
        unit = m.group(2)
        used |= _token_span(tokens, m.group(0))
        if unit == "year":
            y = now.year - 1
            return rng(datetime(y, 1, 1), datetime(y, 12, 31, 23, 59, 59), str(y)), used
        if unit == "month":
            first = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
            last = now.replace(day=1) - timedelta(seconds=1)
            return rng(first, last, first.strftime("%B %Y")), used
        if unit == "week":
            return rng(now - timedelta(days=7), now, "last week"), used
        if unit in SEASONS:
            a, b = SEASONS[unit]
            year = now.year if now.month > b else now.year - 1
            return rng(datetime(year, a, 1), _month_end(year, b), f"{unit.title()} {year}"), used
    if re.search(r"\bthis year\b", text):
        used |= _token_span(tokens, "this year")
        return rng(datetime(now.year, 1, 1), now, str(now.year)), used
    if re.search(r"\byesterday\b", text):
        used |= _token_span(tokens, "yesterday")
        d = now - timedelta(days=1)
        return rng(datetime(d.year, d.month, d.day), datetime(d.year, d.month, d.day, 23, 59, 59), "yesterday"), used
    if re.search(r"\btoday\b", text):
        used |= _token_span(tokens, "today")
        return rng(datetime(now.year, now.month, now.day), now, "today"), used

    # explicit: "12 august 2025", "august 12 2025", "2025-08-12"
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        used |= _token_span(tokens, m.group(0))
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            start = datetime(y, mo, d)
            return rng(start, start + timedelta(days=1) - timedelta(seconds=1), start.strftime("%d %B %Y")), used
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\s+(\d{4})\b", text) or \
        re.search(r"\b([a-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b", text)
    if m:
        g = m.groups()
        if g[0].isdigit():
            d, mon, y = int(g[0]), MONTHS.get(g[1]), int(g[2])
        else:
            mon, d, y = MONTHS.get(g[0]), int(g[1]), int(g[2])
        if mon:
            used |= _token_span(tokens, m.group(0))
            try:
                start = datetime(y, mon, d)
                return rng(start, start + timedelta(days=1) - timedelta(seconds=1), start.strftime("%d %B %Y")), used
            except ValueError:
                pass

    # ranges: "between 2019 and 2021", "2019 to 2021", "before 2020", "after 2021", "since 2022"
    m = re.search(r"\b(?:between\s+)?((?:19|20)\d{2})\s*(?:-|to|and|until)\s*((?:19|20)\d{2})\b", text)
    if m:
        used |= _token_span(tokens, m.group(0))
        y1, y2 = sorted((int(m.group(1)), int(m.group(2))))
        return rng(datetime(y1, 1, 1), datetime(y2, 12, 31, 23, 59, 59), f"{y1}–{y2}"), used
    m = re.search(r"\b(before|after|since)\s+((?:19|20)\d{2})\b", text)
    if m:
        used |= _token_span(tokens, m.group(0))
        y = int(m.group(2))
        if m.group(1) == "before":
            return DateRange(None, naive_to_ts(datetime(y, 1, 1)), f"before {y}"), used
        return DateRange(naive_to_ts(datetime(y, 1, 1)), None, f"{m.group(1)} {y}"), used

    # month + year, month alone, season + year, year alone
    m = re.search(r"\b([a-z]{3,9})\s+((?:19|20)\d{2})\b", text)
    if m and m.group(1) in MONTHS:
        used |= _token_span(tokens, m.group(0))
        mo, y = MONTHS[m.group(1)], int(m.group(2))
        return rng(datetime(y, mo, 1), _month_end(y, mo), f"{calendar.month_name[mo]} {y}"), used
    if m and m.group(1) in SEASONS:
        used |= _token_span(tokens, m.group(0))
        a, b = SEASONS[m.group(1)]
        y = int(m.group(2))
        return rng(datetime(y, a, 1), _month_end(y, b), f"{m.group(1).title()} {y}"), used
    m = re.search(r"\b((?:19|20)\d{2})\b", text)
    if m:
        used |= _token_span(tokens, m.group(1))
        y = int(m.group(1))
        if 1985 <= y <= now.year + 1:
            return rng(datetime(y, 1, 1), datetime(y, 12, 31, 23, 59, 59), str(y)), used
    for name, mo in MONTHS.items():
        if len(name) > 3 and re.search(rf"\b{name}\b", text):
            used |= _token_span(tokens, name)
            return DateRange(None, None, calendar.month_name[mo], month_only=mo), used
    return None, used


def _month_end(year: int, month: int) -> datetime:
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, last, 23, 59, 59)


def _token_span(tokens: list[str], phrase: str) -> set[int]:
    ph = _tokenize(phrase)
    out: set[int] = set()
    for i in range(len(tokens) - len(ph) + 1):
        if tokens[i:i + len(ph)] == ph:
            out |= set(range(i, i + len(ph)))
    return out
