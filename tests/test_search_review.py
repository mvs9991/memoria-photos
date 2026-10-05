"""Search bugs found by a review (2026-10-05). Each failed before its fix."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photointel import db
from photointel.engine import albums as albums_mod, locked as locked_mod, people as people_mod
from photointel.metadata import naive_to_ts
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from photointel.search.parser import parse

NOW = datetime(2026, 1, 3, 10, 0)


@pytest.fixture
def conn(ctx):
    c = ctx.connect()
    yield c
    c.close()


def _person(conn, name):
    pid = people_mod.create_person(conn, name, actor="test")
    conn.commit()
    return pid


# 1 ------------------------------------------------------------------------------------------
def test_last_month_is_the_whole_calendar_month(conn):
    q = parse("photos from last month", conn, now=NOW)
    assert q.date.start == naive_to_ts(datetime(2025, 12, 1))
    assert q.date.end == naive_to_ts(datetime(2025, 12, 31, 23, 59, 59))


# 2 ------------------------------------------------------------------------------------------
def test_or_between_two_people_means_either_even_with_with(conn):
    a, b = _person(conn, "Alice"), _person(conn, "Bob")
    q = parse("photos with alice or bob", conn, now=NOW)
    assert set(q.persons_any) == {a, b} and not q.persons_all


# 3 ------------------------------------------------------------------------------------------
def test_a_person_named_june_is_not_also_the_month(conn):
    june = _person(conn, "June")
    q = parse("photos of june", conn, now=NOW)
    assert q.persons_all == [june]
    assert q.date.month_only is None, q.date


# 4 ------------------------------------------------------------------------------------------
def test_a_negated_tag_is_not_required(conn):
    conn.execute("INSERT INTO tags(name, category) VALUES ('dog', 'object'), ('beach', 'scene')")
    conn.commit()
    q = parse("beach without dogs", conn, now=NOW)
    assert "dog" not in q.tags, q.tags


def test_a_negated_year_is_left_out_not_required(conn):
    q = parse("photos not in 2019", conn, now=NOW)
    assert q.date.exclude and q.date.label.startswith("not"), q.date      # was: only 2019 required
    assert parse("photos in 2019", conn, now=NOW).date.exclude is False


def test_a_negated_month_leaves_those_photos_out(ctx, client):
    # The test library has March and July 2024 photos.
    got = client.get("/api/search", params={"q": "photos not in march", "limit": 2000}).json()["photos"]
    months = {datetime.utcfromtimestamp(p["ts"]).month for p in got if p["ts"]}
    assert months and 3 not in months, months          # was: March only


# 5 ------------------------------------------------------------------------------------------
def test_names_with_apostrophes_and_periods_match(conn):
    pid = _person(conn, "D'Souza")
    conn.execute("INSERT INTO places(geoname_id, name, kind, city, country) VALUES (1, 'St. Louis', 'city', 'St. Louis', 'United States')")
    albums_mod.create_album(conn, "Mom's 60th")
    conn.commit()
    assert parse("d'souza", conn, now=NOW).persons_all == [pid]
    assert parse("st. louis", conn, now=NOW).place_ids
    assert parse("mom's 60th", conn, now=NOW).album_ids


# 6 ------------------------------------------------------------------------------------------
def test_curly_apostrophe_possessive(conn):
    conn.execute("INSERT INTO tags(name, category) VALUES ('birthday', 'event')")
    conn.commit()
    _person(conn, "Alice")
    q = parse("alice’s birthday", conn, now=NOW)
    assert q.semantic_text == "birthday", q.semantic_text


# 7, 8 -- engine / API -------------------------------------------------------------------------
@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    c = ctx.connect()
    run_post_stages(ctx, c)
    c.close()
    return TestClient(create_app(ctx))


def test_keyword_fallback_never_returns_a_locked_or_hidden_photo(ctx, client):
    conn = ctx.connect()
    pid = conn.execute("SELECT id FROM photos WHERE filename = 'IMG_20240303_110003.jpg'").fetchone()[0]
    before = client.get("/api/search?q=110003").json()
    assert pid in [p["id"] for p in before["photos"]], before      # found by filename
    locked_mod.lock(conn, [pid])
    hid = conn.execute("SELECT id FROM photos WHERE filename = 'IMG_20240304_110004.jpg'").fetchone()[0]
    conn.execute("UPDATE photos SET hidden = 1 WHERE id = ?", (hid,))
    conn.commit()
    conn.close()
    got = [p["id"] for p in client.get("/api/search?q=110003").json()["photos"]]
    assert pid not in got, "a locked photo came back from search"
    got = [p["id"] for p in client.get("/api/search?q=110004").json()["photos"]]
    assert hid not in got, "a hidden photo came back from search"


def test_filter_plus_visual_word_reports_every_match(ctx, client):
    conn = ctx.connect()
    pid, n = conn.execute("""SELECT f.person_id, COUNT(DISTINCT f.photo_id) n FROM faces f JOIN photos p ON p.id = f.photo_id
                             WHERE f.person_id IS NOT NULL AND p.status='ok' AND p.hidden=0 AND p.live_component=0
                             GROUP BY f.person_id ORDER BY n DESC""").fetchone()
    people_mod.rename_person(conn, pid, "Ghat")
    conn.commit()
    conn.close()
    assert n >= 3
    full = client.get("/api/search?q=Ghat zebra&limit=2000").json()
    cut = client.get("/api/search?q=Ghat zebra&limit=2").json()
    assert len(cut["photos"]) == 2
    assert cut["total"] == full["total"] == n, (cut["total"], full["total"], n)


@pytest.mark.parametrize("q", ['"', '""', "*", "^", "NEAR(a b)", "a AND", "(", ")", "-", "'", "%", "_", "東京",
                               'sign with text "a" "', 'text "NEAR"', 'saying "*"', "x" * 5000])
def test_odd_queries_never_500(client, q):
    r = client.get("/api/search", params={"q": q})
    assert r.status_code == 200, (q, r.status_code, r.text[:300])


def test_llm_answer_with_accented_names_resolves(conn):
    from photointel.search.llm import _to_parsed_query
    pid = _person(conn, "Noël")
    conn.execute("INSERT INTO places(geoname_id, name, kind, city, country) VALUES (2, 'Kandukūr', 'city', 'Kandukūr', 'India')")
    conn.commit()
    q = _to_parsed_query(conn, "x", {"people_all": ["Noël"], "places": ["Kandukūr"]})
    assert q.persons_all == [pid] and q.place_ids, (q.persons_all, q.place_ids)
