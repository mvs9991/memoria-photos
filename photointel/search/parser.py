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
import unicodedata
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

NEGATORS = {"not", "no", "without", "except", "excluding", "exclude", "minus", "non"}
IMPERATIVES = {"show", "give", "find", "get", "let", "bring", "send", "fetch", "pull", "gimme", "tell"}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1}
SOURCE_WORDS = {"whatsapp": "whatsapp", "download": "download", "downloads": "download", "downloaded": "download",
                "edited": "edited"}
SOURCE_LABELS = {"whatsapp": "From WhatsApp", "phone": "From a phone", "camera": "From a camera",
                 "download": "Downloaded", "edited": "Edited"}
_PHOTO_WORDS = {"photo", "photos", "picture", "pictures", "pic", "pics", "image", "images", "shots", "shot",
                "videos", "video", "roll"}
_DATE_TITLE = re.compile(r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r") (?:19|20)\d{2}")
QUALITY_WORDS = {"best", "top", "greatest", "finest", "nicest", "good", "great", "favourite", "favorite",
                 "favourites", "favorites"}
BAD_QUALITY_WORDS = {"blurry", "blurred", "bad", "worst"}


def fold(text: str) -> str:
    """Lower-case and strip accents: place names are stored as the gazetteer spells them
    (Kandukūr, Guddalaguntapālem) but people type Kandukur."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


@dataclass
class DateRange:
    start: float | None = None
    end: float | None = None
    label: str = ""
    month_only: int | None = None      # e.g. "in January" with no year
    alts: list = field(default_factory=list)   # more (start, end) windows: "2020 and 2022" is either year
    exclude: bool = False              # "not in 2019": everything outside the window(s)


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
    only_videos: bool = False
    only_live: bool = False
    album_ids: list[int] = field(default_factory=list)
    album_label: str | None = None
    user_tags: list[str] = field(default_factory=list)      # tags a person set: always a hard filter
    text_phrases: list[str] = field(default_factory=list)   # words written in the photo (OCR) or its description
    min_rating: int = 0
    persons_exclude: list[int] = field(default_factory=list)   # "without Sanjay", "not Sanjay"
    no_people: bool = False                                    # "without people", "no faces"
    exclude_videos: bool = False                               # "no videos"
    force_no_screenshots: bool = False                         # "not screenshots"
    source_kinds: list[str] = field(default_factory=list)      # whatsapp, phone, camera, download, edited
    sort_explicit: bool = False                                # "best", "oldest", "latest"...
    colors: list[str] = field(default_factory=list)          # dominant colours, for a colour-only query
    tags_exclude: list[str] = field(default_factory=list)      # "without dogs"
    places_exclude: list[int] = field(default_factory=list)    # "not in paris"
    unmatched: list[str] = field(default_factory=list)
    interpretation: list[dict] = field(default_factory=list)
    source: str = "rules"

    def is_empty(self) -> bool:
        return not any([self.persons_all, self.persons_any, self.place_ids, self.event_ids, self.tags,
                        self.date.start, self.date.end, self.date.month_only,
                        self.persons_exclude, self.no_people, self.exclude_videos, self.force_no_screenshots,
                        self.source_kinds, self.sort_explicit, self.result_type != "photos", self.semantic_text, self.only_favorites,
                        self.only_screenshots, self.only_selfies, self.trips_only, self.only_videos,
                        self.only_live, self.album_ids, self.user_tags, self.text_phrases, self.min_rating,
                        self.colors, self.tags_exclude, self.places_exclude])

    def chip(self, kind: str, label: str, detail: str | None = None) -> None:
        self.interpretation.append({"kind": kind, "label": label, "detail": detail})


def _key(text: str) -> str:
    """How a name is looked up: the same tokens a query is split into ("D'Souza" -> "d souza")."""
    return " ".join(_tokenize(text))


class Vocabulary:
    """Searchable names drawn from the library itself (cached briefly)."""

    def __init__(self, conn: sqlite3.Connection):
        self.persons: dict[str, int] = {}
        self.person_labels: dict[int, str] = {}
        self.birth_dates: dict[int, str] = {}
        for r in conn.execute("SELECT id, name, display_no, birth_date FROM persons WHERE merged_into IS NULL"):
            if r["birth_date"]:
                self.birth_dates[r["id"]] = r["birth_date"]
            if r["name"]:
                self.persons[_key(r["name"])] = r["id"]
                self.person_labels[r["id"]] = r["name"]
                first = (_key(r["name"]).split() or [""])[0]
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
                    self.places.setdefault(_key(key), []).append(r["id"])

        self.events: dict[str, list[int]] = {}
        for r in conn.execute("SELECT id, auto_title, user_title FROM events"):
            for key in (r["user_title"], r["auto_title"]):
                if key:
                    self.events.setdefault(_key(key), []).append(r["id"])

        self.tags: set[str] = {fold(r[0]) for r in conn.execute("SELECT name FROM tags")}
        self.user_tags: set[str] = {r[0].lower() for r in conn.execute(
            "SELECT DISTINCT t.name FROM tags t JOIN photo_tags pt ON pt.tag_id = t.id WHERE pt.source = 'user'")}
        self.albums: dict[str, list[int]] = {}
        # Only albums that hold photos. A smart album *is* a search; matching its name would
        # turn "screenshots" into "photos in the album Screenshots", which holds no rows.
        for r in conn.execute("SELECT id, name FROM albums WHERE hidden = 0 AND kind = 'manual'"):
            self.albums.setdefault(_key(r["name"]), []).append(r["id"])
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
    for key in ("gen:people", "gen:events", "gen:places", "gen:albums", "gen:tags"):
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
    text = re.sub(r"['\u2019]s\b", "", fold(text))
    return [t for t in re.split(r"[^a-z0-9\-]+", text) if t]


def _negated(tokens: list[str], idx: int) -> bool:
    """Is the term at `idx` directly negated: "not X", "without X", "no X", "except any X"?"""
    if idx >= 1 and tokens[idx - 1] in NEGATORS:
        return True
    return idx >= 2 and tokens[idx - 1] in ("any", "the", "a", "an", "of", "in", "at", "from") and tokens[idx - 2] in NEGATORS


def _neg_span(tokens: list[str], idx: int) -> set[int]:
    if idx >= 1 and tokens[idx - 1] in NEGATORS:
        return {idx, idx - 1}
    return {idx, idx - 1, idx - 2}


def _source_context(tokens: list[str], idx: int) -> bool:
    """"phone"/"camera" is a source only in "phone photos" or "from (my) phone", not "photo of a camera"."""
    if idx + 1 < len(tokens) and tokens[idx + 1] in _PHOTO_WORDS:
        return True
    if idx >= 1 and tokens[idx - 1] == "from":
        return True
    return idx >= 2 and tokens[idx - 1] in ("my", "the") and tokens[idx - 2] == "from"


def _fuzzy_person(token: str, vocab: Vocabulary, cutoff: float = 0.84) -> int | None:
    best, best_score = None, cutoff
    for name, pid in vocab.persons.items():
        if abs(len(name) - len(token)) > 3:
            continue
        score = SequenceMatcher(None, name, token).ratio()
        if score > best_score:
            best, best_score = pid, score
    return best


def _colour(word: str) -> str | None:
    from ..engine.colors import normalise

    return normalise(word)


def parse(query: str, conn: sqlite3.Connection, me_person_id: int | None = None,
          generation: int | None = None, now: datetime | None = None) -> ParsedQuery:
    vocab = get_vocabulary(conn, generation)
    q = ParsedQuery(raw=query.strip())
    # Words written *in* the photo: "receipt that says Reliance", or anything in quotes.
    # Pulled out first so a quoted word is never read as a person, place or date.
    remainder, phrases = _extract_text_phrases(query.strip())
    for phrase in phrases:
        q.text_phrases.append(phrase)
        q.chip("text", f"“{phrase}”", "text in the photo or its description")
    text = remainder.lower()
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
    # The word that picked the kind of answer is grammar, not something to look for in the pictures.
    intent_words = {"events": {"event", "events", "occasion", "occasions"},
                    "people": {"who", "people"},
                    "places": {"place", "places", "cities", "location", "locations"}}.get(q.result_type, set())
    if q.trips_only:
        intent_words = intent_words | {"trip", "trips", "vacation", "vacations", "holiday", "holidays"}
    for idx, tok in enumerate(tokens):
        if tok in intent_words:
            consumed.add(idx)

    # ---- multi-word vocabulary matching (longest n-gram first) ---------------------------
    max_n = 4
    i = 0
    matched_terms: list[tuple[str, str, object]] = []
    matched_start: list[int] = []
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
            # album (a one-word album name must not shadow a tag of the same name)
            elif phrase in vocab.albums and (n > 1 or phrase not in vocab.tags):
                matched_terms.append(("album", phrase, vocab.albums[phrase]))
            # event title
            elif phrase in vocab.events and n > 1 and not _DATE_TITLE.fullmatch(phrase):
                # (An event auto-titled "March 2022" is a *date*: matching it as an event would
                # return that one cluster instead of the month.)
                matched_terms.append(("event", phrase, vocab.events[phrase]))
            # tag ("screenshot" is a source filter, below, not the weaker visual tag)
            elif (phrase in vocab.tags or phrase in vocab.tag_aliases) and phrase not in ("screenshot", "screenshots"):
                matched_terms.append(("tag", phrase, vocab.tag_aliases.get(phrase, phrase)))
            else:
                continue
            for k in range(i, i + n):
                consumed.add(k)
            matched_start.append(i)
            i += n
            matched = True
            break
        if not matched:
            i += 1

    # ---- negation: "without Sanjay", "not Sanjay", "except Sanjay" ------------------------------
    negated_persons: list[int] = []
    for pos, (kind, _phrase, value) in zip(matched_start, matched_terms):
        if kind == "person" and _negated(tokens, pos):
            negated_persons.append(value)
            consumed.add(pos - 1) if tokens[pos - 1] in NEGATORS else consumed.update((pos - 2, pos - 1))
    for (kind, phrase, value), pos in zip(matched_terms, matched_start):
        if kind in ("tag", "place") and _negated(tokens, pos):
            if kind == "tag":
                q.tags_exclude.append(value)
            else:
                q.places_exclude.extend(value)
            q.chip(kind, phrase.title(), "excluded")
            consumed.update(_neg_span(tokens, pos))
    matched_terms = [t for t, pos in zip(matched_terms, matched_start)
                     if not (t[0] in ("person", "tag", "place") and _negated(tokens, pos))]

    # ---- stars: "5 stars", "4 star photos", "rated" ------------------------------------------
    stars = re.search(r"\b([1-5]|one|two|three|four|five)\s*(?:-\s*)?stars?\b", " ".join(tokens))
    if stars:
        q.min_rating = int(stars.group(1)) if stars.group(1).isdigit() else NUMBER_WORDS[stars.group(1)]
        consumed |= _token_span(tokens, stars.group(0))
        q.chip("filter", f"{q.min_rating}★ and up")
    elif "rated" in tokens:
        q.min_rating = 1
        consumed.add(tokens.index("rated"))
        q.chip("filter", "Rated by you")

    # ---- a person's age: "priya at age 5", "ravi aged 3", "priya when she was 6" -------------
    age_m = re.search(r"\b(?:at age|age|aged|at the age of|when (?:he|she|they) was|when (?:he|she|they) were)"
                      r"\s+(\d{1,2})\b", " ".join(tokens))
    person_hits = [v for kind, _, v in matched_terms if kind == "person"]
    age_range = None
    if age_m and len(set(person_hits)) == 1 and vocab.birth_dates.get(person_hits[0], "")[:1].isdigit():
        birth = datetime.strptime(vocab.birth_dates[person_hits[0]][:10], "%Y-%m-%d")
        n = int(age_m.group(1))
        try:
            lo, hi = birth.replace(year=birth.year + n), birth.replace(year=birth.year + n + 1)
        except ValueError:  # 29 February
            lo, hi = datetime(birth.year + n, 3, 1), datetime(birth.year + n + 1, 3, 1)
        age_range = DateRange(naive_to_ts(lo), naive_to_ts(hi) - 1, f"age {n}")
        consumed |= _token_span(tokens, age_m.group(0))

    # ---- dates --------------------------------------------------------------------------
    date, date_tokens = _parse_dates(tokens, consumed, now)
    if age_range is not None:
        date, date_tokens = age_range, set()
    if date:
        # "not in 2019", "except december": the window is left out rather than required.
        first = min(date_tokens) if date_tokens else None
        if first is not None:
            k = first - 1
            while k >= 0 and tokens[k] in ("in", "from", "during", "of", "the"):
                k -= 1
            if k >= 0 and tokens[k] in NEGATORS:
                date.exclude = True
                date.label = f"not {date.label}" if date.label else "excluded dates"
                date_tokens = date_tokens | set(range(k, first))
        q.date = date
        consumed |= date_tokens

    # ---- flags --------------------------------------------------------------------------
    for idx, tok in enumerate(tokens):
        if idx in consumed:
            continue
        neg = _negated(tokens, idx)
        if neg and tok in ("people", "faces", "persons", "anyone", "someone", "person", "face"):
            q.no_people = True
            q.chip("filter", "No people")
            consumed.update(_neg_span(tokens, idx))
        elif neg and tok in ("screenshot", "screenshots"):
            q.force_no_screenshots = True
            q.chip("filter", "Not screenshots")
            consumed.update(_neg_span(tokens, idx))
        elif neg and tok in ("video", "videos", "movie", "movies"):
            q.exclude_videos = True
            q.chip("filter", "Not videos")
            consumed.update(_neg_span(tokens, idx))
        elif tok in SOURCE_WORDS or (tok in ("phone", "camera") and _source_context(tokens, idx)):
            kind = SOURCE_WORDS.get(tok, tok)
            if kind not in q.source_kinds:
                q.source_kinds.append(kind)
                q.chip("filter", SOURCE_LABELS[kind])
            consumed.add(idx)
        elif tok in QUALITY_WORDS:
            if tok in ("favourite", "favorite", "favourites", "favorites"):
                q.only_favorites = True
                q.chip("filter", "Favourites")
            else:
                q.sort = "quality"
                q.chip("sort", "Best first")
            q.sort_explicit = True
            consumed.add(idx)
        elif tok in BAD_QUALITY_WORDS:
            q.sort = "worst"
            q.sort_explicit = True
            q.chip("sort", "Lowest quality first")
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
        elif tok in ("video", "videos", "movie", "movies") and not q.only_videos:   # not "clip": paper clips
            q.only_videos = True
            q.chip("filter", "Videos")
            consumed.add(idx)
        elif tok == "live" and idx + 1 < len(tokens) and tokens[idx + 1] in ("photo", "photos", "pictures", "pics"):
            q.only_live = True
            q.chip("filter", "Live & motion photos")
            consumed.update((idx, idx + 1))
        elif tok in ("me", "myself") and me_person_id and not (idx > 0 and tokens[idx - 1] in IMPERATIVES):
            # ("show me Sanjay" asks for Sanjay; it does not also require the owner in the photo.)
            q.persons_all.append(me_person_id)
            q.person_labels[me_person_id] = "You"
            q.chip("person", "You")
            consumed.add(idx)
        elif tok in ("oldest", "earliest"):
            q.sort = "date_asc"
            q.sort_explicit = True
            q.chip("sort", "Oldest first")
            consumed.add(idx)
        elif tok in ("recent", "latest", "newest"):
            q.sort = "date_desc"
            q.sort_explicit = True
            q.chip("sort", "Newest first")
            consumed.add(idx)

    # ---- apply matched vocabulary --------------------------------------------------------
    person_ids = [v for kind, _, v in matched_terms if kind == "person"]
    for pid in negated_persons:
        if pid not in q.persons_exclude:
            q.persons_exclude.append(pid)
            q.person_labels[pid] = vocab.person_labels.get(pid, f"Person {pid}")
            q.chip("person", q.person_labels[pid], "excluded")
    if person_ids:
        # "A and B" / "A with B" / "together" -> photos containing all of them
        conjunction = (bool(re.search(r"\b(and|with|together|both)\b", joined)) and len(person_ids) > 1
                       and not re.search(r"\b(or|either)\b", joined))   # "with A or B": either
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
        elif kind == "album":
            q.album_ids.extend(value)
            q.album_label = phrase.title()
            q.chip("album", phrase.title())
        elif kind == "tag" and str(value) in vocab.user_tags:
            q.user_tags.append(str(value))
            q.chip("tag", str(value).title(), "your tag")
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
    # Only colours left ("blue photos", "red and white"): a dominant-colour filter. With
    # other words ("red car") the visual search reads the whole phrase instead.
    colour_names = [_colour(t) for t in semantic_words if t not in ("colour", "color", "colours", "colors",
                                                                   "coloured", "colored")]
    if semantic_words and colour_names and all(colour_names):
        q.colors = list(dict.fromkeys(colour_names))
        q.chip("filter", " & ".join(q.colors), "mostly these colours")
        semantic_words = []
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


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')
_SAYS = re.compile(r"\b(?:that says|which says|saying|with the text|with text|with the words)\s+(.+)$", re.I)


def _extract_text_phrases(query: str) -> tuple[str, list[str]]:
    phrases = []
    for m in _QUOTED.finditer(query):
        phrase = " ".join((m.group(1) or m.group(2) or "").split())
        if phrase:
            phrases.append(phrase)
    rest = _QUOTED.sub(" ", query)
    m = _SAYS.search(rest)
    if m:
        phrase = " ".join(m.group(1).split()).strip(" .?!")
        if phrase:
            phrases.append(phrase)
        rest = rest[: m.start()]
    return rest, phrases


def _parse_dates(tokens: list[str], consumed: set[int], now: datetime) -> tuple[DateRange | None, set[int]]:
    used: set[int] = set()
    tokens = [("~" if i in consumed else t) for i, t in enumerate(tokens)]   # June the person is not June the month
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
            month1 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            first = (month1 - timedelta(days=1)).replace(day=1)
            last = month1 - timedelta(seconds=1)
            return rng(first, last, first.strftime("%B %Y")), used
        if unit == "week":
            return rng(now - timedelta(days=7), now, "last week"), used
        if unit in SEASONS:
            a, b = SEASONS[unit]
            end_year = now.year if now.month > b else now.year - 1      # the latest finished one
            if a > b:                                                   # winter: Dec -> Feb
                return rng(datetime(end_year - 1, a, 1), _month_end(end_year, b),
                           f"Winter {end_year - 1}–{str(end_year)[2:]}"), used
            return rng(datetime(end_year, a, 1), _month_end(end_year, b), f"{unit.title()} {end_year}"), used
    # rolling windows: "in the last 3 months", "past 2 weeks", "last 10 days"
    m = re.search(r"\b(?:last|past)\s+(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
                  r"\s+(day|week|month|year)s?\b", text)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
        unit = m.group(2)
        used |= _token_span(tokens, m.group(0))
        if unit == "day":
            start = now - timedelta(days=n)
        elif unit == "week":
            start = now - timedelta(weeks=n)
        elif unit == "month":
            total = now.year * 12 + now.month - 1 - n
            y, mo = divmod(total, 12)
            start = now.replace(year=y, month=mo + 1, day=min(now.day, calendar.monthrange(y, mo + 1)[1]))
        else:
            try:
                start = now.replace(year=now.year - n)
            except ValueError:      # 29 February
                start = now.replace(year=now.year - n, day=28)
        return rng(start, now, f"last {n} {unit}{'s' if n != 1 else ''}"), used
    m = re.search(r"\b(last|this)\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b(?!\s+(?:19|20)\d{2})", text)
    if m:
        mo = MONTHS[m.group(2)]
        if m.group(1) == "last":
            year = now.year if now.month > mo else now.year - 1       # the latest finished one
        else:
            year = now.year
        used |= _token_span(tokens, m.group(0))
        return rng(datetime(year, mo, 1), _month_end(year, mo), f"{calendar.month_name[mo]} {year}"), used
    if re.search(r"\bthis month\b", text):
        used |= _token_span(tokens, "this month")
        return rng(datetime(now.year, now.month, 1), now, "this month"), used
    if re.search(r"\bthis week\b", text):
        used |= _token_span(tokens, "this week")
        monday = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        return rng(monday, now, "this week"), used
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
    m = re.search(r"\b(?<!between )((?:19|20)\d{2})\s+(?:and|or)\s+((?:19|20)\d{2})\b", text)
    if m:       # "2020 and 2022": those two years, not the two and the one between
        used |= _token_span(tokens, m.group(0))
        y1, y2 = sorted((int(m.group(1)), int(m.group(2))))
        a, b = (datetime(y1, 1, 1), datetime(y1, 12, 31, 23, 59, 59)), (datetime(y2, 1, 1), datetime(y2, 12, 31, 23, 59, 59))
        r = rng(a[0], a[1], f"{y1} & {y2}")
        if y2 != y1:
            r.alts = [(naive_to_ts(b[0]), naive_to_ts(b[1]))]
        return r, used
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
        if a > b:       # winter 2025 = Dec 2025 - Feb 2026 (the winter that begins that year)
            return rng(datetime(y, a, 1), _month_end(y + 1, b), f"Winter {y}–{str(y + 1)[2:]}"), used
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
