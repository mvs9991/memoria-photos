"""Hiding many people at once, the People page's bulk action.

A real library discovers thousands of one-off faces from crowds. Tidying them one
request at a time is not a workflow, so selecting a few hundred and hiding them is
one call. Hiding only flips a flag: no photo is touched and nothing is deleted.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages


@pytest.fixture
def client_and_people(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY id")]
    conn.close()
    assert len(ids) >= 2, "the fixture library should produce at least two people"
    return TestClient(create_app(ctx), raise_server_exceptions=False), ids


def visible(client, hidden=False) -> set[int]:
    r = client.get("/api/people", params={"include_hidden": hidden})
    return {p["id"] for p in r.json()["people"]}


def test_hiding_a_selection_removes_exactly_those_people(client_and_people):
    client, ids = client_and_people
    before = visible(client)
    target = ids[:1]
    r = client.post("/api/people/hide", json={"ids": target})
    assert r.status_code == 200 and r.json() == {"changed": 1}
    after = visible(client)
    assert after == before - set(target)
    assert set(target) <= visible(client, hidden=True)       # still there, just hidden


def test_hiding_is_reversible(client_and_people):
    client, ids = client_and_people
    client.post("/api/people/hide", json={"ids": ids})
    assert visible(client) == set()
    r = client.post("/api/people/hide", json={"ids": ids, "hidden": False})
    assert r.json()["changed"] == len(ids)
    assert set(ids) <= visible(client)


def test_hiding_twice_changes_nothing_the_second_time(client_and_people):
    client, ids = client_and_people
    assert client.post("/api/people/hide", json={"ids": ids}).json()["changed"] == len(ids)
    assert client.post("/api/people/hide", json={"ids": ids}).json()["changed"] == 0


def test_unknown_and_duplicate_ids_are_harmless(client_and_people):
    client, ids = client_and_people
    r = client.post("/api/people/hide", json={"ids": [ids[0], ids[0], 987654321, -5]})
    assert r.status_code == 200 and r.json()["changed"] == 1


def test_an_empty_selection_is_a_noop(client_and_people):
    client, ids = client_and_people
    r = client.post("/api/people/hide", json={"ids": []})
    assert r.status_code == 200 and r.json() == {"changed": 0}
    assert set(ids) <= visible(client)


def test_a_merged_person_is_not_resurrected_or_changed(client_and_people, ctx):
    client, ids = client_and_people
    conn = ctx.connect()
    conn.execute("UPDATE persons SET merged_into = ? WHERE id = ?", (ids[0], ids[1]))
    conn.commit()
    conn.close()
    r = client.post("/api/people/hide", json={"ids": [ids[1]]})
    assert r.json()["changed"] == 0                      # merged people are not ours to flag


def test_hiding_never_touches_photos(client_and_people, ctx, library):
    client, ids = client_and_people
    files = sorted(p.read_bytes() for p in library.rglob("*.jpg"))
    conn = ctx.connect()
    photos_before = conn.execute("SELECT COUNT(*) FROM photos WHERE status = 'ok'").fetchone()[0]
    conn.close()
    client.post("/api/people/hide", json={"ids": ids})
    conn = ctx.connect()
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE status = 'ok'").fetchone()[0] == photos_before
    conn.close()
    assert sorted(p.read_bytes() for p in library.rglob("*.jpg")) == files


def test_an_absurd_selection_is_refused(client_and_people):
    client, _ = client_and_people
    assert client.post("/api/people/hide", json={"ids": list(range(20001))}).status_code == 422
    assert client.post("/api/people/hide", json={"ids": [10 ** 21]}).status_code == 422
    assert client.post("/api/people/hide", json={"ids": "all"}).status_code == 422


def test_the_action_is_recorded_in_the_audit_log(client_and_people, ctx):
    client, ids = client_and_people
    client.post("/api/people/hide", json={"ids": ids})
    conn = ctx.connect()
    row = conn.execute("SELECT action, details FROM audit_log WHERE action = 'people_hidden'").fetchone()
    conn.close()
    assert row is not None and str(len(ids)) in row["details"]
