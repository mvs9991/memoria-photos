"""Every GET endpoint, swept with hostile parameters.

A malformed request is the caller's problem and should come back 4xx. A 500 is
ours: it means an unvalidated value reached the database or an engine. Rather
than spot-checking a handful of routes, this walks every GET path FastAPI
documents, reads the parameters each one declares, and pushes the same battery
of bad values through them — so an endpoint added later is covered the day it
appears, without anybody remembering to extend a list here.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


@pytest.fixture
def small_library(ctx, tmp_path):
    """A handful of real photos, so endpoints have something to page over."""
    root = tmp_path / "lib"
    for i in range(6):
        make_image(root / f"p{i}.jpg", colour=(40 + 30 * i, 90, 160),
                   taken=datetime(2024, 3, 9, 11, i), gps=(17.385, 78.4867), noise=16)
    Indexer(ctx, workers=2).run(roots=[str(root)])
    return root


# Values that have broken photo apps before: SQL and path traversal, numbers that
# are negative, enormous or not numbers at all, and text that is empty, huge or
# non-Latin.
HOSTILE = [
    "",
    "-1",
    "999999999999999999999",          # larger than a 64-bit integer
    "1e309",                          # overflows to inf as a float
    "nan",
    "abc",
    "null",
    "' OR 1=1 --",
    '"; DROP TABLE photos; --',
    "../../etc/passwd",
    "\x00",
    "a" * 5000,
    "\U0001F600" * 50,
    "照片",
    "9999-99-99T99:99:99",
]

# Tried on every endpoint on top of whatever it declares: an endpoint that ignores
# one simply returns its normal answer, and these are the names most likely to be
# added to a route later without a validator.
UNIVERSAL = ["limit", "offset", "q", "sort", "order"]


def crashed(r) -> bool:
    """True when the response came from the unhandled-exception safety net.

    Not every 5xx is a bug: asking for off-site snapshots when restic is not
    installed is a 502 with a sentence explaining why, which is a correct answer.
    The catch-all handler is the one that means we lost — it alone adds an "error"
    key naming the exception type. Anything below 500 is the caller's problem and
    is exactly what we want a malformed request to get.
    """
    if r.status_code < 500:
        return False
    try:
        return "error" in r.json()
    except ValueError:
        return True            # a 5xx that is not even JSON


def declared_params(item: dict) -> list[str]:
    """The query parameter names an endpoint documents."""
    return [p["name"] for p in item.get("get", {}).get("parameters", [])
            if p.get("in") == "query"]


def fill(path: str, photo_id: int) -> str | None:
    """Substitute a real id for each {placeholder}; skip paths we cannot fill."""
    if "{" not in path:
        return path
    filled = path
    for token in ("{photo_id}", "{id}", "{person_id}", "{album_id}", "{event_id}",
                  "{place_id}", "{group_id}", "{face_id}", "{job_id}", "{root_id}"):
        filled = filled.replace(token, str(photo_id))
    return None if "{" in filled else filled


def get_endpoints(app):
    """(path, [query parameter names]) for every documented GET."""
    schema = app.openapi()
    return sorted((p, declared_params(i)) for p, i in schema["paths"].items() if "get" in i)


def test_every_get_endpoint_survives_hostile_parameters(ctx, app, small_library):
    """No GET may answer 5xx, whatever is put in its query string."""
    client = TestClient(app, raise_server_exceptions=False)
    conn = ctx.connect()
    photo_id = int(conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0])
    conn.close()

    endpoints = [(fill(p, photo_id), params) for p, params in get_endpoints(app)]
    endpoints = [(p, params) for p, params in endpoints if p]
    assert len(endpoints) > 40, f"only {len(endpoints)} GET paths swept - has the sweep broken?"

    failures = []
    for path, params in endpoints:
        for param in dict.fromkeys(params + UNIVERSAL):
            for value in HOSTILE:
                r = client.get(path, params={param: value})
                if crashed(r):
                    failures.append(f"{path}?{param}={value[:40]!r} -> {r.status_code}")
                    break          # one report per path+param is plenty
    assert not failures, f"{len(failures)} endpoints returned 5xx:\n" + "\n".join(failures[:40])


def test_every_get_endpoint_survives_a_hostile_path_segment(ctx, app, small_library):
    """The same, but with the hostile value in the path rather than the query."""
    client = TestClient(app, raise_server_exceptions=False)
    templated = [p for p, _ in get_endpoints(app) if "{" in p]
    assert templated, "no templated GET paths found"

    failures = []
    for path in templated:
        for value in ["-1", "0", "abc", "999999999999999999999", "' OR 1=1 --",
                      "../../etc/passwd", "a" * 300, "\U0001F600"]:
            filled = path
            while "{" in filled:
                start = filled.index("{")
                end = filled.index("}", start)
                filled = filled[:start] + value + filled[end + 1:]
            try:
                r = client.get(filled)
            except Exception as exc:                 # a raised exception is itself the bug
                failures.append(f"{path} with {value[:30]!r} raised {type(exc).__name__}: {exc}")
                continue
            if crashed(r):
                failures.append(f"{path} with {value[:30]!r} -> {r.status_code}")
    assert not failures, f"{len(failures)} failures on a hostile path segment:\n" + "\n".join(failures[:40])


@pytest.mark.parametrize("limit,offset", [
    (0, 0), (-1, 0), (1, -1), (10**9, 0), (1, 10**9), (10**9, 10**9),
])
def test_absurd_paging_is_refused_or_empty_never_a_crash(ctx, app, small_library, limit, offset):
    """Paging past the end of a library, or asking for a billion rows, must not
    fall over or try to materialise the lot."""
    client = TestClient(app, raise_server_exceptions=False)
    declares = dict(get_endpoints(app))
    for path in ("/api/photos/index", "/api/people", "/api/events", "/api/albums",
                 "/api/duplicates", "/api/places"):
        r = client.get(path, params={"limit": limit, "offset": offset})
        assert not crashed(r), f"{path} limit={limit} offset={offset} -> {r.status_code}"
        # Only endpoints that actually take an offset can be expected to honour one.
        # /api/photos/index deliberately has none: it returns the whole index in one
        # go so the grid can virtualise over it.
        if (r.status_code == 200 and offset >= 10**9
                and "offset" in declares.get(path, [])
                and "json" in r.headers.get("content-type", "")):
            body = r.json()
            rows = next((v for v in body.values() if isinstance(v, list)), [])
            assert rows == [], f"{path} returned rows from past the end of the library"


def test_search_survives_hostile_queries(ctx, app, small_library):
    """Search takes free text, so it is the widest door into the system."""
    client = TestClient(app, raise_server_exceptions=False)
    queries = HOSTILE + [
        "photos of ' OR 1=1",
        "*" * 200,
        "(((((((((",                     # unbalanced groupings
        'NOT AND OR "',                  # FTS operators with nothing to operate on
        "NEAR/999999",
        "a AND " * 500,                  # a very deep boolean expression
        "\\",
        "photos from 9999-99-99",
        "me and " * 300,
    ]
    failures = []
    for q in queries:
        r = client.get("/api/search", params={"q": q})
        if crashed(r):
            failures.append(f"{q[:50]!r} -> {r.status_code}")
    assert not failures, "search returned 5xx for:\n" + "\n".join(failures[:20])


def test_an_unknown_id_is_a_404_not_a_500(ctx, app, small_library):
    """A missing row is a normal outcome and must be reported as one."""
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/api/photos/987654321", "/api/people/987654321",
                 "/api/events/987654321", "/api/albums/987654321"):
        r = client.get(path)
        assert r.status_code in (401, 403, 404, 422), f"{path} -> {r.status_code}"


# The two bugs this sweep actually found, pinned as named cases so a regression is
# legible in the test report rather than buried in a sweep of thousands of requests.

@pytest.mark.parametrize("url", [
    "/api/photos/999999999999999999999",
    "/api/people/999999999999999999999",
    "/api/events/999999999999999999999",
    "/api/photos/index?person=999999999999999999999",
    "/api/duplicates?offset=999999999999999999999",
    "/api/insights?year=999999999999999999999",
])
def test_an_integer_too_big_for_sqlite_is_a_422(ctx, app, small_library, url):
    """FastAPI validates `int` with Python's arbitrary-precision ints, so a 21-digit
    number passes the signature and only fails when SQLite is asked to bind it."""
    r = TestClient(app, raise_server_exceptions=False).get(url)
    assert r.status_code == 422, f"{url} -> {r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("params", [
    {"year": -1},
    {"year": 99999},
    {"year": 2024, "month": 0},
    {"year": 2024, "month": 13},
    {"year": 2024, "month": -5},
])
def test_an_impossible_year_or_month_is_a_422(ctx, app, small_library, params):
    """These reach datetime(), which raises for anything out of range. Seven
    endpoints share the filter, so it is validated there rather than per route.

    `month` is only checked where it is accepted — /api/insights takes a year
    alone, so a month in its query string is an unknown parameter and ignored.
    """
    client = TestClient(app, raise_server_exceptions=False)
    paths = ["/api/photos/index"] if "month" in params else ["/api/photos/index", "/api/insights"]
    for path in paths:
        r = client.get(path, params=params)
        assert r.status_code == 422, f"{path} {params} -> {r.status_code} {r.text[:120]}"


def test_year_zero_means_no_filter_rather_than_an_error(ctx, app, small_library):
    """year=0 is falsy, so the filter is skipped and everything comes back. Pinned
    because it is the one value in the range that is deliberately not a 422."""
    r = TestClient(app, raise_server_exceptions=False).get("/api/photos/index", params={"year": 0})
    assert r.status_code == 200
    assert len(r.json()["ids"]) == 6


@pytest.mark.parametrize("path", [
    "/api/nope",
    "/api/photos/index/extra",
    "/api/PEOPLE",                 # the wrong case is still the wrong path
    "/api/",
    "/api",
])
def test_an_unknown_api_path_is_a_json_404_not_the_app_shell(ctx, app, small_library, path):
    """The SPA catch-all serves index.html for client-side routes like /photos, but
    it was also answering unknown /api/ paths — 200, with HTML. A caller expecting
    JSON got "Unexpected token <" rather than a plain 404."""
    r = TestClient(app, raise_server_exceptions=False).get(path)
    assert r.status_code == 404, f"{path} -> {r.status_code}"
    assert "html" not in r.headers.get("content-type", ""), f"{path} served the app shell"


def test_a_client_side_route_still_serves_the_app_shell(ctx, app, small_library):
    """The other half of that fix: real UI routes must still get index.html, or
    deep links and refreshes break."""
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/photos", "/people/1", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "html" in r.headers.get("content-type", ""), f"{path} did not serve the shell"


# ----------------------------------------------- paths the filesystem rejects outright

def test_an_overlong_path_is_refused_not_a_crash(ctx, app, small_library, monkeypatch):
    """A path longer than the filesystem allows makes is_dir()/is_file() *raise*
    rather than answer False. Linux raises ENAMETOOLONG where Windows does not, so
    this only ever failed in CI; it is simulated here so it is caught on either.
    """
    import pathlib

    real_is_dir, real_is_file = pathlib.Path.is_dir, pathlib.Path.is_file

    def boom(self, *a, **kw):
        if "aaaa" in str(self):
            raise OSError(36, "File name too long")
        return real_is_dir(self, *a, **kw)

    def boom_file(self, *a, **kw):
        if "aaaa" in str(self):
            raise OSError(36, "File name too long")
        return real_is_file(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "is_dir", boom)
    monkeypatch.setattr(pathlib.Path, "is_file", boom_file)

    client = TestClient(app, raise_server_exceptions=False)
    long_name = "a" * 400
    r = client.get("/api/browse", params={"path": long_name})
    assert not crashed(r) and r.status_code == 400, f"browse -> {r.status_code} {r.text[:120]}"
    # The SPA catch-all must fall through to the app shell, not 500.
    r = client.get(f"/{long_name}")
    assert not crashed(r), f"catch-all -> {r.status_code} {r.text[:120]}"


def test_browse_reports_an_unlistable_folder(ctx, app, small_library, monkeypatch):
    """A symlink loop or a share that drops mid-listing is the caller's problem to
    see, not an unhandled 500. Only PermissionError was handled before."""
    import os as _os

    def boom(*a, **kw):
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(_os, "scandir", boom)
    r = TestClient(app, raise_server_exceptions=False).get(
        "/api/browse", params={"path": str(small_library)})
    assert r.status_code == 400 and "cannot list" in r.json()["detail"]
