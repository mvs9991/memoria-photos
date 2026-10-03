"""A library must say so when it has stopped getting place names.

Without the offline place data the geocoding stage logs a warning and skips, so photos keep
arriving with GPS and no place, and nothing in the app said anything. 697 photos were added to a
real library that way, found only by reading /api/health. The alert names the count and the fix.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from photointel import health
from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image


def keys(ctx, conn) -> dict[str, dict]:
    ok = lambda path, *a, **k: True
    return {p["key"]: p for p in health.check(ctx, conn, isdir=ok)}


@pytest.fixture
def geotagged(ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTOINTEL_GEO", raising=False)
    root = tmp_path / "lib"
    for i in range(3):
        make_image(root / f"p{i}.jpg", colour=(40 + 30 * i, 90, 160), taken=datetime(2024, 3, 9, 11, i),
                   gps=(17.385, 78.4867), noise=14)
    Indexer(ctx, workers=2).run(roots=[str(root)])
    return ctx, ctx.connect()


def test_missing_place_data_with_geotagged_photos_raises_an_alert(geotagged):
    ctx, conn = geotagged
    assert not (ctx.paths.geo / "cities500.txt").exists()
    got = keys(ctx, conn)
    assert "geo:missing" in got
    alert = got["geo:missing"]
    assert alert["level"] == "warn"
    assert "3 photos" in alert["detail"] and str(ctx.paths.geo) in alert["detail"]
    assert "geo-setup" in alert["fix"] and "PHOTOINTEL_GEO" in alert["fix"]


def test_no_alert_when_the_place_data_is_there(geotagged):
    ctx, conn = geotagged
    ctx.paths.geo.mkdir(parents=True, exist_ok=True)
    (ctx.paths.geo / "cities500.txt").write_text("", encoding="utf-8")       # available() only checks it exists
    assert "geo:missing" not in keys(ctx, conn)


def test_no_alert_when_nothing_needs_a_place(ctx, tmp_path, monkeypatch):
    """No GPS photos means the missing data costs nothing, so there is nothing to warn about."""
    monkeypatch.delenv("PHOTOINTEL_GEO", raising=False)
    root = tmp_path / "plain"
    make_image(root / "a.jpg", colour=(10, 20, 30), taken=datetime(2024, 3, 9, 11, 0), noise=14)
    Indexer(ctx, workers=2).run(roots=[str(root)])
    assert "geo:missing" not in keys(ctx, ctx.connect())


def test_the_alert_goes_away_once_photos_have_places(geotagged):
    ctx, conn = geotagged
    conn.execute("INSERT OR IGNORE INTO places(id, name, kind, lat, lon, country_code) "
                 "VALUES (1, 'Somewhere', 'city', 17.3, 78.4, 'IN')")
    conn.execute("UPDATE photos SET place_id = 1 WHERE gps_lat IS NOT NULL")
    conn.commit()
    assert "geo:missing" not in keys(ctx, conn)
