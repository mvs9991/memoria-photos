"""Optional Claude reasoning layer.

The LLM is a *language* interface, never the database and never the recogniser:
it turns an awkward sentence into the same structured filter the rule parser
produces, and writes event summaries from facts we already computed locally.
Face matching, ranking and every retrieval decision stay deterministic.

Privacy
-------
Disabled by default. When enabled it sends only:
  * the user's query text, and
  * a vocabulary list (person names, place names, event titles, tag names)
Photos, file paths, GPS coordinates and EXIF are never sent unless the user
separately enables `llm_send_images` and asks for a description of one photo.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta

from ..metadata import naive_to_ts
from .parser import DateRange, ParsedQuery, get_vocabulary

log = logging.getLogger(__name__)

MAX_VOCAB_ITEMS = 120
REQUEST_TIMEOUT = 30.0

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "people_all": {"type": "array", "items": {"type": "string"},
                       "description": "Names that must ALL appear in the same photo."},
        "people_any": {"type": "array", "items": {"type": "string"},
                       "description": "Names where any one of them appearing is enough."},
        "places": {"type": "array", "items": {"type": "string"},
                   "description": "Place names exactly as given in the vocabulary."},
        "events": {"type": "array", "items": {"type": "string"},
                   "description": "Event titles exactly as given in the vocabulary."},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Tag names from the vocabulary describing what is visible."},
        "date_from": {"type": ["string", "null"], "description": "ISO date, inclusive, or null."},
        "date_to": {"type": ["string", "null"], "description": "ISO date, inclusive, or null."},
        "visual_query": {"type": ["string", "null"],
                         "description": "What the photo should look like, for visual search. Null if not applicable."},
        "result_type": {"type": "string", "enum": ["photos", "events", "people", "places"]},
        "sort": {"type": "string", "enum": ["relevance", "date_desc", "date_asc", "quality"]},
        "only_favorites": {"type": "boolean"},
        "only_screenshots": {"type": "boolean"},
        "trips_only": {"type": "boolean"},
        "reasoning": {"type": "string", "description": "One short sentence for the user explaining the reading."},
    },
    "required": ["people_all", "people_any", "places", "events", "tags", "date_from", "date_to",
                 "visual_query", "result_type", "sort", "only_favorites", "only_screenshots",
                 "trips_only", "reasoning"],
    "additionalProperties": False,
}

SYSTEM = """You convert a person's question about their own photo library into a structured filter.

Rules:
- Only use names that appear in the provided vocabulary. Never invent a person, place or event.
- If the question mentions people who should appear together in the same photo, put them in people_all.
- Put anything describing how the photo *looks* (scenery, objects, activity) in visual_query.
- Dates: resolve relative expressions ("last summer", "two years ago") against the given today's date.
- result_type is "events" when the user asks which events/trips something happened at, "people" when
  they ask who, "places" when they ask where, otherwise "photos".
- Prefer leaving a field empty over guessing.
"""


def _client(ctx):
    import anthropic

    key = ctx.settings.anthropic_api_key
    if not key:
        raise RuntimeError("No Anthropic API key configured")
    return anthropic.Anthropic(api_key=key, timeout=REQUEST_TIMEOUT, max_retries=1)


def _vocabulary_payload(conn: sqlite3.Connection) -> dict:
    vocab = get_vocabulary(conn)
    people = [r[0] for r in conn.execute(
        "SELECT name FROM persons WHERE name IS NOT NULL AND merged_into IS NULL "
        "ORDER BY photo_count DESC LIMIT ?", (MAX_VOCAB_ITEMS,))]
    places = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(city, name) FROM places ORDER BY population DESC LIMIT ?", (MAX_VOCAB_ITEMS,))]
    regions = [r[0] for r in conn.execute(
        "SELECT DISTINCT admin1 FROM places WHERE admin1 IS NOT NULL LIMIT 40")]
    countries = [r[0] for r in conn.execute(
        "SELECT DISTINCT country FROM places WHERE country IS NOT NULL LIMIT 40")]
    events = [r[0] for r in conn.execute(
        "SELECT COALESCE(user_title, auto_title) FROM events ORDER BY photo_count DESC LIMIT 60")]
    tags = sorted(vocab.tags)
    years = [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT strftime('%Y', taken_ts, 'unixepoch') FROM photos "
        "WHERE taken_ts IS NOT NULL ORDER BY 1") if r[0]]
    return {"people": people, "places": places + regions + countries, "events": events,
            "tags": tags, "years": years}


def llm_parse(ctx, conn: sqlite3.Connection, query: str) -> ParsedQuery | None:
    """Ask Claude for a structured filter. Returns None if the layer is unavailable."""
    if not ctx.settings.llm_enabled or not ctx.settings.anthropic_api_key:
        return None
    try:
        client = _client(ctx)
    except Exception as exc:
        log.warning("LLM layer unavailable: %s", exc)
        return None

    vocab_payload = _vocabulary_payload(conn)
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = time.time()
    try:
        response = client.messages.create(
            model=ctx.settings.llm_model or "claude-opus-5",
            max_tokens=2048,
            system=SYSTEM,
            output_config={
                "effort": "low",          # a short extraction; depth is not the bottleneck
                "format": {"type": "json_schema", "schema": QUERY_SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": (
                    f"Today is {today}.\n\n"
                    f"Vocabulary available in this library:\n{json.dumps(vocab_payload, ensure_ascii=False)}\n\n"
                    f"Question: {query}"
                ),
            }],
        )
    except Exception as exc:
        log.warning("LLM query parsing failed: %s", exc)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        log.warning("LLM declined the query")
        return None
    try:
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except Exception as exc:
        log.warning("Could not read LLM response: %s", exc)
        return None
    log.info("LLM parsed query in %.1fs (%s in / %s out tokens)", time.time() - t0,
             response.usage.input_tokens, response.usage.output_tokens)
    return _to_parsed_query(conn, query, data)


def _to_parsed_query(conn: sqlite3.Connection, query: str, data: dict) -> ParsedQuery:
    """Map the model's answer onto library ids. Unknown names are dropped, never guessed."""
    vocab = get_vocabulary(conn)
    q = ParsedQuery(raw=query, source="llm")

    for name in data.get("people_all") or []:
        pid = vocab.persons.get(str(name).lower())
        if pid:
            q.persons_all.append(pid)
            q.person_labels[pid] = vocab.person_labels.get(pid, str(name))
            q.chip("person", q.person_labels[pid])
    for name in data.get("people_any") or []:
        pid = vocab.persons.get(str(name).lower())
        if pid and pid not in q.persons_all:
            q.persons_any.append(pid)
            q.person_labels[pid] = vocab.person_labels.get(pid, str(name))
            q.chip("person", q.person_labels[pid], "any of")
    for name in data.get("places") or []:
        ids = vocab.places.get(str(name).lower())
        if ids:
            q.place_ids.extend(ids)
            q.place_label = str(name)
            q.chip("place", str(name))
    for title in data.get("events") or []:
        ids = vocab.events.get(str(title).lower())
        if ids:
            q.event_ids.extend(ids)
            q.event_label = str(title)
            q.chip("event", str(title))
    for tag in data.get("tags") or []:
        if str(tag).lower() in vocab.tags:
            q.tags.append(str(tag).lower())
            q.chip("tag", str(tag).title())

    start = _iso_to_ts(data.get("date_from"))
    end = _iso_to_ts(data.get("date_to"), end_of_day=True)
    if start or end:
        label = " – ".join(x for x in (data.get("date_from"), data.get("date_to")) if x)
        q.date = DateRange(start=start, end=end, label=label)
        q.chip("date", label)

    visual = (data.get("visual_query") or "").strip()
    if visual:
        q.semantic_text = visual
        q.chip("visual", visual, "matched visually")
    elif q.tags:
        q.semantic_text = ", ".join(q.tags)

    q.result_type = data.get("result_type") or "photos"
    q.sort = data.get("sort") or ("relevance" if q.semantic_text else "date_desc")
    q.only_favorites = bool(data.get("only_favorites"))
    q.only_screenshots = bool(data.get("only_screenshots"))
    if q.only_screenshots:
        q.exclude_screenshots = False
    q.trips_only = bool(data.get("trips_only"))
    reasoning = (data.get("reasoning") or "").strip()
    if reasoning:
        q.chip("ai", reasoning, "interpreted by Claude")
    return q


def _iso_to_ts(value, end_of_day: bool = False) -> float | None:
    """Accept 2024, 2024-05 or 2024-05-17; anything else is ignored rather than guessed."""
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end_of_day:
            if fmt == "%Y":
                dt = dt.replace(month=12, day=31)
            elif fmt == "%Y-%m":
                next_month = dt.replace(day=28) + timedelta(days=4)
                dt = next_month - timedelta(days=next_month.day)
            dt = dt.replace(hour=23, minute=59, second=59)
        return naive_to_ts(dt)
    return None


# ---------------------------------------------------------------- event summaries

SUMMARY_SYSTEM = """You write one short, warm paragraph describing a set of photos from someone's
personal library, using only the facts given. Two or three sentences. No invented details, no
speculation about feelings, no marketing tone. If a fact is missing, leave it out."""


def llm_event_summary(ctx, conn: sqlite3.Connection, event_id: int) -> str:
    """Rewrite an event's template summary in natural language from local facts only."""
    from ..engine.events import build_summary, event_title
    from ..engine.places import place_label, place_row
    from ..engine.tags import top_tags_for_photos

    fallback = build_summary(conn, event_id)
    if not ctx.settings.llm_enabled or not ctx.settings.anthropic_api_key:
        return fallback
    ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if ev is None:
        return fallback
    if ev["kind"] == "trip":
        photo_ids = [int(r[0]) for r in conn.execute("SELECT photo_id FROM trip_photos WHERE trip_id=?", (event_id,))]
    else:
        photo_ids = [int(r[0]) for r in conn.execute("SELECT id FROM photos WHERE event_id=?", (event_id,))]
    people = [r[0] for r in conn.execute(
        f"""SELECT p.name FROM persons p JOIN faces f ON f.person_id = p.id
            WHERE f.photo_id IN ({','.join('?' * min(len(photo_ids), 900))}) AND p.name IS NOT NULL
            GROUP BY p.id ORDER BY COUNT(*) DESC LIMIT 8""", photo_ids[:900])] if photo_ids else []
    place = place_row(conn, ev["place_id"])
    facts = {
        "title": event_title(ev),
        "dates": {"start": ev["start_ts"], "end": ev["end_ts"]},
        "photo_count": ev["photo_count"],
        "place": place_label(place, include_country=True) if place is not None else None,
        "people": people,
        "scenes": [t for t, _ in top_tags_for_photos(conn, photo_ids, limit=8)],
        "category": ev["category"],
    }
    try:
        client = _client(ctx)
        response = client.messages.create(
            model=ctx.settings.llm_model or "claude-opus-5",
            max_tokens=500,
            system=SUMMARY_SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return fallback
        return next(b.text for b in response.content if b.type == "text").strip() or fallback
    except Exception as exc:
        log.warning("LLM summary failed: %s", exc)
        return fallback


def llm_available(ctx) -> bool:
    return bool(ctx.settings.llm_enabled and ctx.settings.anthropic_api_key)
