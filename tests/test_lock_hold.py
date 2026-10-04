"""A post-processing stage must not hold the database write lock while it does CPU work.

Found by eval/loadtest_mixed.py: a `BEGIN IMMEDIATE` probe on a second connection, polled while a
real index job ran, was refused for 12.8 s during the `geocode` stage while the web app's edits
(favourite, rate, hide) waited that long. Cause: `geocode_photos` interleaved the slow reverse
lookups with `upsert_place`, so the first INSERT opened a write transaction that stayed open
through every remaining lookup. The time grows with the number of distinct ~110 m cells, so on a
real library it is minutes, far past the 60 s busy timeout.

The test makes each lookup try to take the write lock from another connection. Before the fix every
lookup after the first insert is refused; after it, none is.
"""
from __future__ import annotations

import sqlite3

import pytest

from photointel import db
from photointel.engine import places as places_mod
from photointel.geo import GeoPlace, GeoResult
from photointel.pipeline.indexer import Indexer


def _place(i: int) -> GeoPlace:
    return GeoPlace(geoname_id=1000 + i, name=f"Town {i}", lat=17.0 + i * .01, lon=78.0, population=50_000,
                    feature_code="PPL", country_code="IN", admin1="Telangana", admin2=None, country="India")


class SlowGeocoder:
    """Stands in for the GeoNames tree. Every lookup asks: could a web request write right now?"""

    def __init__(self, data_dir):
        self.refused = 0
        self.calls = 0
        self.probe = sqlite3.connect(str(data_dir / "library.db"), timeout=0.05, isolation_level=None)

    def lookup(self, lat, lon):
        self.calls += 1
        try:
            self.probe.execute("BEGIN IMMEDIATE")
            self.probe.execute("ROLLBACK")
        except sqlite3.OperationalError:
            self.refused += 1
        p = _place(self.calls)
        return GeoResult(locality=p, city=p, landmark=None, distance_km=0.1)


@pytest.fixture
def geo_ctx(ctx, library, monkeypatch):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    conn.execute("UPDATE photos SET place_id = NULL")
    conn.commit()
    fake = SlowGeocoder(ctx.paths.data)
    monkeypatch.setattr(places_mod.ReverseGeocoder, "available", staticmethod(lambda *_: True))
    monkeypatch.setattr(places_mod.ReverseGeocoder, "get", staticmethod(lambda *_: fake))
    return ctx, conn, fake


def test_geocoding_never_holds_the_write_lock_during_a_lookup(geo_ctx):
    ctx, conn, fake = geo_ctx
    out = places_mod.geocode_photos(ctx, conn)
    assert out["geocoded"] >= 8 and fake.calls >= 2, "the fixture must give the stage several distinct cells"
    assert fake.refused == 0, f"{fake.refused} of {fake.calls} lookups ran while this stage held the write lock"
    assert not conn.in_transaction


def test_geocoding_still_places_every_photo(geo_ctx):
    ctx, conn, fake = geo_ctx
    places_mod.geocode_photos(ctx, conn)
    missing = conn.execute("SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL AND status='ok' "
                           "AND place_id IS NULL").fetchone()[0]
    assert missing == 0
    # one place row per distinct cell, numbered in order of first appearance (results unchanged)
    names = [r[0] for r in conn.execute("SELECT name FROM places ORDER BY id")]
    assert names == [f"Town {i}" for i in range(1, fake.calls + 1)]
