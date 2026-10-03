"""Search ground truth: a small library built row by row, so the right answer to a query is known.

Every test here compares what the search returns with the set a person would expect, computed
from the rows that were inserted. Found by running ~190 queries against a real 29k-photo
library and checking each result against SQL over the same database.
"""
import time
from datetime import datetime

import pytest

from photointel.metadata import naive_to_ts
from photointel.search.engine import SearchEngine
from photointel.search.parser import parse

NOW = datetime(2026, 10, 3, 12, 0)     # a Saturday


def ts(y, m=1, d=1, h=12):
    return naive_to_ts(datetime(y, m, d, h))


class Lib:
    def __init__(self, ctx):
        self.ctx = ctx
        self.conn = ctx.connect()
        c = self.conn
        c.execute("INSERT INTO roots(path, added_at) VALUES ('/lib', 0)")
        c.execute("INSERT INTO models(kind, name, version, dim, created_at) VALUES ('face','t','1',4,0)")
        self.n = 0
        self.person = {}
        for name in ("Ana", "Ben", "Chitra"):
            c.execute("INSERT INTO persons(name, created_at, updated_at) VALUES (?, 0, 0)", (name,))
            self.person[name] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO places(geoname_id, name, kind, city, admin1, country, lat, lon, population) "
                  "VALUES (1, 'Kandukūr', 'city', 'Kandukūr', 'Andhra Pradesh', 'India', 15.2, 79.9, 5000)")
        self.kandukur = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add(self, when, people=(), source=None, media="image", place=None, event=None, quality=0.5,
            hidden=0):
        self.n += 1
        c = self.conn
        c.execute("""INSERT INTO photos(id, root_id, rel_path, folder, filename, ext, size, mtime, status,
                       first_seen_at, last_seen_at, taken_ts, source_kind, media_type, place_id, event_id,
                       quality_score, face_count, hidden)
                     VALUES (?, 1, ?, '', ?, 'jpg', 1, 0, 'ok', 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (self.n, f"p{self.n}.jpg", f"p{self.n}.jpg", when, source, media, place, event, quality,
                   len(people), hidden))
        for name in people:
            c.execute("""INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
                           embedding, person_id, created_at)
                         VALUES (?, 1, 0, 0, .1, .1, .9, 50, .9, x'00', ?, 0)""", (self.n, self.person[name]))
        return self.n

    def event(self, title, start, end, kind="event"):
        c = self.conn
        c.execute("""INSERT INTO events(kind, auto_title, start_ts, end_ts, photo_count, created_at, updated_at)
                     VALUES (?, ?, ?, ?, 1, 0, 0)""", (kind, title, start, end))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def candidates(self, text, now=NOW):
        q = parse(text, self.conn, me_person_id=self.person["Chitra"], now=now)
        sql, args = SearchEngine(self.ctx)._candidate_sql(self.conn, q)
        return {int(r[0]) for r in self.conn.execute(sql, args)}, q

    def search(self, text, **kw):
        return SearchEngine(self.ctx).search(self.conn, text, **kw)


@pytest.fixture
def lib(ctx):
    ctx.settings.me_person_id = None
    return Lib(ctx)


# ------------------------------------------------------------------ people

def test_show_me_is_not_the_owner_filter(lib):
    """"show me Ana's photos" asked for Ana AND the owner (here Chitra): zero results on a real library."""
    a = lib.add(ts(2022, 5, 1), ["Ana"])
    lib.add(ts(2022, 5, 2), ["Ben"])
    lib.add(ts(2022, 5, 3), ["Ana", "Chitra"])
    lib.ctx.settings.me_person_id = lib.person["Chitra"]
    got, q = lib.candidates("show me Ana's photos")
    assert a in got and len(got) == 2
    assert not q.persons_all or lib.person["Chitra"] not in q.persons_all
    mine, q = lib.candidates("photos of me")
    assert len(mine) == 1 and any(c["label"] == "You" for c in q.interpretation)


def test_without_and_not_exclude_a_person(lib):
    both = lib.add(ts(2022, 5, 1), ["Ana", "Ben"])
    only_a = lib.add(ts(2022, 5, 2), ["Ana"])
    lib.add(ts(2022, 5, 3), ["Ben"])
    for text in ("Ana without Ben", "photos of Ana not Ben", "Ana except Ben"):
        got, q = lib.candidates(text)
        assert got == {only_a}, text
        assert both not in got
    got, _ = lib.candidates("without people")
    assert got == set()
    lone = lib.add(ts(2022, 6, 1), [])
    got, _ = lib.candidates("without people")
    assert got == {lone}


def test_not_screenshots_excludes_them(lib):
    shot = lib.add(ts(2022, 5, 1), source="screenshot")
    real = lib.add(ts(2022, 5, 2), source="phone")
    got, _ = lib.candidates("not screenshots")
    assert got == {real}
    got, _ = lib.candidates("screenshot")           # the singular is the source filter too, not a weak tag
    assert got == {shot}
    got, _ = lib.candidates("screenshots")
    assert got == {shot}


def test_no_videos(lib):
    v = lib.add(ts(2022, 5, 1), media="video")
    i = lib.add(ts(2022, 5, 2))
    assert lib.candidates("no videos")[0] == {i}
    assert lib.candidates("videos")[0] == {v}


# ------------------------------------------------------------------ sources

def test_source_words_filter_by_source_kind(lib):
    w = lib.add(ts(2022, 5, 1), source="whatsapp")
    p = lib.add(ts(2022, 5, 2), source="phone")
    cam = lib.add(ts(2022, 5, 3), source="camera")
    d = lib.add(ts(2022, 5, 4), source="download")
    e = lib.add(ts(2022, 5, 5), source="edited")
    assert lib.candidates("whatsapp")[0] == {w}
    assert lib.candidates("from whatsapp photos")[0] == {w}
    assert lib.candidates("from my phone")[0] == {p}
    assert lib.candidates("phone photos")[0] == {p}
    assert lib.candidates("camera photos")[0] == {cam}
    assert lib.candidates("downloads")[0] == {d}
    assert lib.candidates("edited photos")[0] == {e}


def test_phone_or_camera_as_an_object_is_still_visual(lib):
    lib.add(ts(2022, 5, 1), source="phone")
    q = parse("a photo of a camera", lib.conn, now=NOW)
    assert not q.source_kinds and q.semantic_text == "camera"


# ------------------------------------------------------------------ dates

def test_before_a_year_is_a_search_not_a_keyword_lookup(lib):
    old = lib.add(ts(2010, 3, 1))
    lib.add(ts(2022, 3, 1))
    res = lib.search("before 2015")
    assert res.photo_ids == [old]
    assert any(c["kind"] == "date" for c in res.interpretation)


def test_month_year_means_the_month_not_an_event_that_shares_its_title(lib):
    """An event auto-titled "March 2022" covers one day; "March 2022" means the whole month."""
    ev = lib.event("March 2022", ts(2022, 3, 5, 0), ts(2022, 3, 5, 23))
    a = lib.add(ts(2022, 3, 5), event=ev)
    b = lib.add(ts(2022, 3, 20))
    lib.add(ts(2022, 4, 2))
    got, q = lib.candidates("in March 2022")
    assert got == {a, b}
    assert not q.event_ids


def test_last_and_this_month_name(lib):
    mar = lib.add(ts(2026, 3, 9))
    lib.add(ts(2025, 3, 9))
    got, q = lib.candidates("last march")            # now = Oct 2026: the March just gone
    assert got == {mar} and not q.semantic_text
    got, _ = lib.candidates("this march")
    assert got == {mar}


def test_this_month_and_this_week(lib):
    now = datetime(2026, 10, 3, 12, 0)               # Saturday; week starts Monday 28 Sep
    today = lib.add(ts(2026, 10, 3, 9))
    monday = lib.add(ts(2026, 9, 28, 9))
    sunday = lib.add(ts(2026, 9, 27, 9))
    sept1 = lib.add(ts(2026, 9, 1, 9))
    assert lib.candidates("this week", now)[0] == {today, monday}
    assert lib.candidates("this month", now)[0] == {today}
    q = parse("this month", lib.conn, now=now)
    assert not q.semantic_text
    assert sunday and sept1


def test_rolling_windows(lib):
    now = datetime(2026, 10, 3, 12, 0)
    recent = lib.add(ts(2026, 8, 20))
    old = lib.add(ts(2026, 6, 1))
    assert lib.candidates("in the last 3 months", now)[0] == {recent}
    assert lib.candidates("past 6 months", now)[0] == {recent, old}
    assert lib.candidates("last 10 days", now)[0] == set()
    assert not parse("in the last 3 months", lib.conn, now=now).semantic_text


def test_winter_is_not_an_empty_range(lib):
    """Winter was Dec 1 to Feb 28 *of the same year*: end before start, nothing ever matched."""
    dec = lib.add(ts(2025, 12, 25))
    feb = lib.add(ts(2026, 2, 10))
    lib.add(ts(2026, 7, 1))
    got, q = lib.candidates("last winter")
    assert got == {dec, feb}, q.date
    got, _ = lib.candidates("winter 2025")
    assert got == {dec, feb}


def test_two_years_with_and_are_those_two_years(lib):
    """"2020 and 2022" used to become the range 2020-2022, adding all of 2021."""
    a = lib.add(ts(2020, 6, 1))
    mid = lib.add(ts(2021, 6, 1))
    b = lib.add(ts(2022, 6, 1))
    assert lib.candidates("photos from 2020 and 2022")[0] == {a, b}
    assert lib.candidates("2020 or 2022")[0] == {a, b}
    assert lib.candidates("between 2020 and 2022")[0] == {a, mid, b}


def test_five_star_in_words(lib):
    q = parse("five star photos", lib.conn, now=NOW)
    assert q.min_rating == 5 and not q.semantic_text


# ------------------------------------------------------------------ places

def test_place_names_match_without_accents(lib):
    k = lib.add(ts(2022, 1, 1), place=lib.kandukur)
    lib.add(ts(2022, 1, 2))
    for text in ("photos in Kandukur", "photos in Kandukūr", "KANDUKUR"):
        got, q = lib.candidates(text)
        assert got == {k}, text
        assert q.place_ids and not q.semantic_text


# ------------------------------------------------------------------ best / worst / entity results

def test_best_photos_without_filters_is_a_search_not_a_filename_lookup(lib):
    for i in range(3):
        lib.add(ts(2022, 5, 1 + i), quality=0.1 * i)
    res = lib.search("best photos")
    assert res.total == 3 and res.photo_ids[0] == 3
    assert lib.search("worst photos").photo_ids[0] == 1


def test_best_of_a_person_keeps_the_filter_past_5000_candidates(lib):
    """With more than 5,000 candidates the ranking query dropped them and sorted the whole library."""
    c = lib.conn
    ana = lib.person["Ana"]
    rows, faces = [], []
    for i in range(1, 5201):
        rows.append((i, f"p{i}.jpg", ts(2022, 1, 1) + i, 0.1 if i <= 5100 else 0.9, 1 if i <= 5100 else 0))
    c.executemany("""INSERT INTO photos(id, root_id, rel_path, folder, filename, ext, size, mtime, status,
                       first_seen_at, last_seen_at, taken_ts, quality_score, face_count)
                     VALUES (?, 1, ?, '', ?, 'jpg', 1, 0, 'ok', 0, 0, ?, ?, ?)""",
                  [(i, p, p, t, qs, fc) for i, p, t, qs, fc in rows])
    c.executemany("""INSERT INTO faces(photo_id, model_id, x1, y1, x2, y2, det_score, size_px, quality,
                       embedding, person_id, created_at) VALUES (?, 1, 0, 0, .1, .1, .9, 50, .9, x'00', ?, 0)""",
                  [(i, ana) for i in range(1, 5101)])
    c.commit()
    res = lib.search("best photos of Ana", limit=100)
    assert res.photo_ids and all(i <= 5100 for i in res.photo_ids)
    res = lib.search("best of 2022", limit=100)         # every photo is 2022; the filter must still apply
    assert res.photo_ids


def test_trips_lists_every_trip_not_those_in_the_newest_photos(lib):
    """Entity results were aggregated from the first `limit` photos only."""
    for k in range(4):
        start = ts(2020 + k, 5, 1)
        ev = lib.event(f"Trip {k}", start, start + 86400, kind="trip")
        for j in range(3):
            pid = lib.add(start + j * 60)
            lib.conn.execute("UPDATE photos SET event_id = ? WHERE id = ?", (ev, pid))
    lib.conn.commit()
    res = lib.search("trips", limit=3)                   # 12 photos, only 3 fit the page
    assert res.result_type == "events"
    assert {e["title"] for e in res.events} == {f"Trip {k}" for k in range(4)}
    res = lib.search("my trips in 2021")
    assert [e["title"] for e in res.events] == ["Trip 1"]


# ------------------------------------------------------------------ never slow, never 5xx

@pytest.mark.parametrize("text", ["", " ", "\"", "'", "(", "a OR b", "NEAR(a b)", "photo*", "col:val", "%", "\\",
                                  "'; DROP TABLE photos; --", "x" * 3000, "Ana " * 500, "no no no", "not",
                                  "without", "last", "last 0 days", "last 999 years", "2020 and", "తెలుగు"])
def test_odd_queries_do_not_raise_or_hang(lib, text):
    lib.add(ts(2022, 5, 1), ["Ana"])
    t = time.time()
    res = lib.search(text)
    assert isinstance(res.photo_ids, list)
    assert time.time() - t < 5


def test_hidden_photos_never_returned(lib):
    h = lib.add(ts(2022, 5, 1), ["Ana"], hidden=1)
    v = lib.add(ts(2022, 5, 2), ["Ana"])
    for text in ("Ana", "photos from 2022", "best photos", "Ana in 2022", "not screenshots"):
        assert h not in lib.search(text).photo_ids, text
    assert v in lib.search("Ana").photo_ids


# ------------------------------------------------------------------ design choices, pinned

def test_last_week_is_the_trailing_seven_days(lib):
    """DESIGN QUESTION: "last month"/"last year" are calendar periods, "last week" is rolling."""
    in_window = lib.add(ts(2026, 9, 28))
    lib.add(ts(2026, 9, 20))
    assert lib.candidates("last week")[0] == {in_window}


def test_last_year_is_the_calendar_year(lib):
    a = lib.add(ts(2025, 1, 2))
    lib.add(ts(2026, 1, 2))
    assert lib.candidates("last year")[0] == {a}
