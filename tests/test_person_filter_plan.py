"""Opening a person must not scan the whole library once per photo.

On a real 29,500-photo library the person page took 82 seconds to show its photos: the
page loaded instantly and then sat blank, which looked exactly like "clicking a person
does nothing". With only single-column indexes on faces, SQLite answered "does this photo
contain person 6?" by walking all 6,700 of person 6's faces for every photo it examined.
A composite (person_id, photo_id) index makes each of those checks one seek.

A timing assertion would be flaky and a small test library is too small to be slow, so
these tests pin the thing that decides the speed: the query plan.
"""
from __future__ import annotations

import pytest

from photointel import db
from photointel.api.routes_library import photo_filter_sql
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages


@pytest.fixture
def conn_with_people(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    return conn


def plan_of(conn, where: str, args: list) -> str:
    rows = conn.execute(f"EXPLAIN QUERY PLAN SELECT p.id FROM photos p WHERE {where}", args).fetchall()
    return " | ".join(r[3] for r in rows)


def test_the_composite_index_exists_on_a_fresh_database(ctx):
    conn = ctx.connect()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_faces_person_photo" in names


def test_an_existing_library_gets_the_index_when_it_is_opened(tmp_path):
    """Libraries created before the index existed must pick it up on their next start."""
    path = tmp_path / "old.db"
    conn = db.init_db(path)
    conn.execute("DROP INDEX ix_faces_person_photo")
    conn.commit()
    conn.close()
    conn = db.init_db(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "ix_faces_person_photo" in names


def test_the_person_filter_uses_it(conn_with_people):
    conn = conn_with_people
    pid = conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC").fetchone()[0]
    where, args = photo_filter_sql(conn, person=[pid])
    plan = plan_of(conn, where, args)
    assert "ix_faces_person_photo" in plan, f"person filter no longer uses the composite index:\n{plan}"


def test_two_people_filter_uses_it_for_both(conn_with_people):
    conn = conn_with_people
    ids = [r[0] for r in conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY id LIMIT 2")]
    assert len(ids) == 2
    where, args = photo_filter_sql(conn, person=ids)
    assert plan_of(conn, where, args).count("ix_faces_person_photo") == 2


def test_the_filter_still_returns_exactly_that_persons_photos(conn_with_people):
    conn = conn_with_people
    pid = conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC").fetchone()[0]
    where, args = photo_filter_sql(conn, person=[pid])
    got = {r[0] for r in conn.execute(f"SELECT p.id FROM photos p WHERE {where}", args)}
    truth = {r[0] for r in conn.execute(
        "SELECT DISTINCT f.photo_id FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.person_id = ? AND p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0", (pid,))}
    assert got == truth and truth, "the speed-up must not change which photos a person has"
