"""Query parsing, search execution and the HTTP API."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from photointel import db
from photointel.engine import people as people_mod
from photointel.metadata import ts_to_naive
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from photointel.search.engine import SearchEngine
from photointel.search.parser import parse
from tests.conftest import make_image

NOW = datetime(2026, 6, 15, 12, 0)


@pytest.fixture
def ready(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    pid = conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC").fetchone()[0]
    people_mod.rename_person(conn, pid, "Ghat")
    return ctx, conn, pid


# ----------------------------------------------------------------- parser

def test_parse_person_and_place(ready):
    ctx, conn, pid = ready
    q = parse("photos of Ghat in Goa", conn, now=NOW)
    assert pid in q.persons_all
    assert any(c["kind"] == "person" for c in q.interpretation)


def test_parse_two_people_requires_both(ready):
    ctx, conn, pid = ready
    other = people_mod.create_person(conn, "Priya", actor="test")
    conn.commit()
    q = parse("Ghat and Priya together", conn, now=NOW)
    assert set(q.persons_all) == {pid, other}
    assert not q.semantic_text          # "together" is grammar, not a visual query


@pytest.mark.parametrize("text,year,month", [
    ("photos from 2023", 2023, None),
    ("January 2025", 2025, 1),
    ("show me august 2024 photos", 2024, 8),
])
def test_parse_absolute_dates(ready, text, year, month):
    ctx, conn, _ = ready
    q = parse(text, conn, now=NOW)
    assert q.date.start is not None
    start = ts_to_naive(q.date.start)
    assert start.year == year
    if month:
        assert start.month == month


def test_parse_relative_dates(ready):
    ctx, conn, _ = ready
    q = parse("photos from last year", conn, now=NOW)
    assert ts_to_naive(q.date.start).year == NOW.year - 1
    q = parse("photos from this year", conn, now=NOW)
    assert ts_to_naive(q.date.start).year == NOW.year
    q = parse("photos before 2020", conn, now=NOW)
    assert q.date.start is None and ts_to_naive(q.date.end).year == 2020
    q = parse("between 2019 and 2021", conn, now=NOW)
    assert ts_to_naive(q.date.start).year == 2019 and ts_to_naive(q.date.end).year == 2021


def test_parse_flags_and_sorting(ready):
    ctx, conn, _ = ready
    assert parse("screenshots", conn, now=NOW).only_screenshots
    assert parse("best photos from 2024", conn, now=NOW).sort == "quality"
    assert parse("my trips", conn, now=NOW).trips_only
    assert parse("events with Ghat", conn, now=NOW).result_type == "events"


def test_parse_fuzzy_person_name(ready):
    ctx, conn, pid = ready
    q = parse("photos of Ghatt", conn, now=NOW)       # one typo
    assert pid in q.persons_all


def test_parse_empty_and_gibberish(ready):
    ctx, conn, _ = ready
    assert parse("", conn, now=NOW).is_empty()
    q = parse("zzzqqq", conn, now=NOW)
    assert q.semantic_text == "zzzqqq"                # falls through to visual search


# ----------------------------------------------------------------- search execution

def test_search_by_person_returns_their_photos(ready):
    ctx, conn, pid = ready
    engine = SearchEngine(ctx)
    res = engine.search(conn, "photos of Ghat")
    assert res.total > 0
    marks = ",".join("?" * len(res.photo_ids))
    n = conn.execute(
        f"SELECT COUNT(DISTINCT photo_id) FROM faces WHERE person_id=? AND photo_id IN ({marks})",
        (pid, *res.photo_ids)).fetchone()[0]
    assert n == len(res.photo_ids)                    # every result really contains that person


def test_search_by_date_filters(ready):
    ctx, conn, _ = ready
    engine = SearchEngine(ctx)
    res = engine.search(conn, "photos from 2024")
    assert res.total > 0
    for pid_ in res.photo_ids:
        ts = conn.execute("SELECT taken_ts FROM photos WHERE id=?", (pid_,)).fetchone()[0]
        assert ts_to_naive(ts).year == 2024


def test_search_screenshots_flag(ready):
    ctx, conn, _ = ready
    res = SearchEngine(ctx).search(conn, "screenshots")
    assert res.total >= 1
    for pid_ in res.photo_ids:
        kind = conn.execute("SELECT source_kind FROM photos WHERE id=?", (pid_,)).fetchone()[0]
        assert kind == "screenshot"


def test_search_interpretation_is_explained(ready):
    ctx, conn, _ = ready
    res = SearchEngine(ctx).search(conn, "photos of Ghat from 2024")
    kinds = {c["kind"] for c in res.interpretation}
    assert "person" in kinds and "date" in kinds
    assert res.explanation


def test_search_unknown_term_does_not_crash(ready):
    ctx, conn, _ = ready
    res = SearchEngine(ctx).search(conn, "flurbleflorb")
    assert isinstance(res.photo_ids, list)


# ----------------------------------------------------------------- HTTP API

@pytest.fixture
def client(ready):
    from photointel.api.app import create_app

    ctx, conn, pid = ready
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c, ctx, conn, pid


def test_api_stats_and_health(client):
    c, *_ = client
    s = c.get("/api/stats").json()
    assert s["photos"] > 0 and "people" in s
    h = c.get("/api/health").json()
    assert h["ok"] is True


def test_api_photo_index_and_detail(client):
    c, ctx, conn, pid = client
    idx = c.get("/api/photos/index").json()
    assert idx["total"] == len(idx["ids"]) == len(idx["ratio"]) == len(idx["ts"])
    detail = c.get(f"/api/photos/{idx['ids'][0]}").json()
    assert "filename" in detail and "faces" in detail and "quality" in detail


def test_api_thumbnail_and_original(client):
    c, ctx, conn, pid = client
    photo_id = c.get("/api/photos/index").json()["ids"][0]
    for size in ("sm", "m", "l"):
        r = c.get(f"/api/thumb/{photo_id}?s={size}")
        assert r.status_code == 200 and r.content[:2] in (b"\xff\xd8", b"RI", b"\x89P")
    assert c.get(f"/api/photos/{photo_id}/original").status_code == 200


def test_api_face_crop(client):
    c, ctx, conn, pid = client
    face_id = conn.execute("SELECT id FROM faces LIMIT 1").fetchone()[0]
    r = c.get(f"/api/faces/{face_id}/crop?size=128")
    assert r.status_code == 200 and len(r.content) > 500


def test_api_person_detail_with_places(client):
    """Regression: a place row once shadowed the person row and 500'd this endpoint."""
    c, ctx, conn, pid = client
    conn.execute("""INSERT INTO places(geoname_id, name, kind, city, admin1, country, lat, lon, population)
                    VALUES (99001, 'Testville', 'city', 'Testville', 'Teststate', 'Testland', 1.0, 2.0, 5000)""")
    place_id = conn.execute("SELECT id FROM places WHERE geoname_id=99001").fetchone()[0]
    photo_id = conn.execute(
        "SELECT photo_id FROM faces WHERE person_id=? LIMIT 1", (pid,)).fetchone()[0]
    conn.execute("UPDATE photos SET place_id=? WHERE id=?", (place_id, photo_id))
    conn.commit()
    detail = c.get(f"/api/people/{pid}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["label"]
    assert any(p["name"] == "Testville" for p in body["places"])


def test_api_people_flow(client):
    c, ctx, conn, pid = client
    people = c.get("/api/people").json()["people"]
    assert any(p["label"] == "Ghat" for p in people)
    detail = c.get(f"/api/people/{pid}").json()
    assert detail["photo_count"] > 0
    assert c.post(f"/api/people/{pid}/rename", json={"name": "Ghat R"}).status_code == 200
    assert c.get(f"/api/people/{pid}").json()["label"] == "Ghat R"
    faces = c.get(f"/api/people/{pid}/faces").json()["faces"]
    assert faces and "confidence" in faces[0]


def test_api_reject_and_assign(client):
    c, ctx, conn, pid = client
    face_id = conn.execute("SELECT id FROM faces WHERE person_id=? LIMIT 1", (pid,)).fetchone()[0]
    assert c.post("/api/faces/reject", json={"face_ids": [face_id], "person_id": pid}).status_code == 200
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[0] is None
    out = c.post("/api/faces/assign", json={"face_ids": [face_id], "name": "New Person"}).json()
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[0] == out["person_id"]


def test_api_search_endpoint(client):
    c, *_ = client
    r = c.get("/api/search", params={"q": "photos of Ghat"}).json()
    assert r["total"] > 0 and r["interpretation"]
    assert c.get("/api/search/suggestions", params={"q": "gh"}).json()["suggestions"]


def test_api_events_places_duplicates(client):
    c, ctx, conn, pid = client
    events = c.get("/api/events").json()["events"]
    if events:
        detail = c.get(f"/api/events/{events[0]['id']}").json()
        assert "photos" in detail and "people" in detail
    assert "hierarchy" in c.get("/api/places").json()
    dups = c.get("/api/duplicates").json()
    assert "groups" in dups and dups["counts"]


def test_api_favorite_and_hidden_roundtrip(client):
    c, ctx, conn, pid = client
    photo_id = c.get("/api/photos/index").json()["ids"][0]
    c.post(f"/api/photos/{photo_id}/flags", json={"favorite": True})
    assert c.get(f"/api/photos/{photo_id}").json()["favorite"] is True
    c.post(f"/api/photos/{photo_id}/flags", json={"hidden": True})
    assert photo_id not in c.get("/api/photos/index").json()["ids"]


def test_api_settings_roundtrip(client):
    c, *_ = client
    before = c.get("/api/settings").json()["settings"]
    assert before["llm_enabled"] is False
    c.post("/api/settings", json={"allow_online_map_tiles": True})
    assert c.get("/api/settings").json()["settings"]["allow_online_map_tiles"] is True


def test_api_never_leaks_api_key(client):
    c, *_ = client
    c.post("/api/settings", json={"anthropic_api_key": "sk-ant-secret"})
    body = c.get("/api/settings").text
    assert "sk-ant-secret" not in body


def test_api_404s(client):
    c, *_ = client
    assert c.get("/api/photos/99999").status_code == 404
    assert c.get("/api/people/99999").status_code == 404
    assert c.get("/api/events/99999").status_code == 404


def test_dev_cors_is_opt_in(ctx, monkeypatch):
    """A personal library must not trust another local origin unless asked."""
    from fastapi.middleware.cors import CORSMiddleware

    from photointel.api.app import create_app

    monkeypatch.delenv("PHOTOINTEL_DEV", raising=False)
    plain = create_app(ctx)
    assert not any(m.cls is CORSMiddleware for m in plain.user_middleware)

    monkeypatch.setenv("PHOTOINTEL_DEV", "1")
    dev = create_app(ctx)
    assert any(m.cls is CORSMiddleware for m in dev.user_middleware)


def test_duplicate_reclaimable_bytes_covers_whole_library(client):
    """The headline figure must span every group, not just the page returned.

    It used to sum only the current page, so a real library reported 1.8 GB
    reclaimable next to "8,732 groups" when the true figure was 33.9 GB.
    """
    c, *_ = client
    full = c.get("/api/duplicates", params={"limit": 1000}).json()
    if len(full["groups"]) < 2:
        pytest.skip("fixture library has too few duplicate groups")
    paged = c.get("/api/duplicates", params={"limit": 1}).json()
    assert len(paged["groups"]) == 1
    # Same library, same filter -> same total, whatever the page size.
    assert paged["reclaimable_bytes"] == full["reclaimable_bytes"]
    assert paged["reclaimable_bytes"] >= paged["groups"][0]["reclaimable_bytes"]
