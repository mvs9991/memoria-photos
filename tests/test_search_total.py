"""A search's "N results" is how many photos matched, not how many were sent.

Searching a person with 6,434 photos said "1,000 results": the engine found every match, kept the first
1,000 (the page's limit) and reported the length of what it kept.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer


@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    return TestClient(create_app(ctx))


def test_a_filter_search_reports_every_match_even_when_fewer_are_sent(ctx, client):
    conn = ctx.connect()
    year, n = conn.execute(
        "SELECT strftime('%Y', taken_ts, 'unixepoch') y, COUNT(*) n FROM photos WHERE status = 'ok' AND hidden = 0 "
        "AND live_component = 0 AND taken_ts IS NOT NULL AND COALESCE(source_kind, '') != 'screenshot' "
        "GROUP BY y ORDER BY n DESC LIMIT 1").fetchone()
    conn.close()
    assert n >= 3, "the fixture needs a year with a few photos"
    full = client.get(f"/api/search?q={year}&limit=2000").json()
    assert full["total"] == len(full["photos"]) > 2, full["interpretation"]
    cut = client.get(f"/api/search?q={year}&limit=2").json()
    assert len(cut["photos"]) == 2
    assert cut["total"] == full["total"], f"said {cut['total']} results; {full['total']} photos matched"
