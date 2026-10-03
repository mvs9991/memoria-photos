"""The mutating endpoints, swept with hostile bodies.

Two properties, and the second is the one that matters most in this project:

1. A malformed body is answered 4xx, never an unhandled 500.
2. No sequence of malformed requests touches an original photo. Every file under
   the library root must be byte-for-byte identical afterwards, and still there.

That second check is the non-negotiable stated in CLAUDE.md, asserted against the
whole write surface at once rather than one endpoint at a time.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from tests.conftest import make_image
from tests.test_api_fuzz import crashed


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


@pytest.fixture
def library_root(ctx, tmp_path):
    root = tmp_path / "lib"
    for i in range(6):
        make_image(root / f"p{i}.jpg", colour=(40 + 30 * i, 90, 160),
                   taken=datetime(2024, 3, 9, 11, i), gps=(17.385, 78.4867), noise=16)
    Indexer(ctx, workers=2).run(roots=[str(root)])
    return root


# Endpoints left out, and why. Everything else is fair game.
SKIP = {
    "/api/service/restart",        # would restart the process under the test runner
    "/api/service/autostart",      # writes to the OS task scheduler / registry
    "/api/jobs",                   # spawns a background index run; timing, not validation
    "/api/https/setup",            # reaches the network
    "/api/offsite/setup",
    "/api/offsite/run",
    "/api/offsite/verify",
    # Loads the Florence-2 captioner, which is not downloaded in a test environment
    # and would try to fetch it from the network once per hostile body.
    "/api/photos/{photo_id}/describe",
}

BODIES = [
    None,                                    # no body at all
    {},                                      # empty object
    [],                                      # an array where an object is expected
    "a string, not an object",
    {"ids": None},
    {"ids": "not-a-list"},
    {"ids": [None, "x", -1, 10**21]},
    {"ids": list(range(5000))},              # a very large batch
    {"photo_ids": [-1]},
    {"photo_ids": [10**21]},
    {"id": "' OR 1=1 --"},
    {"name": ""},
    {"name": "a" * 10000},
    {"name": "../../etc/passwd"},
    {"name": "\x00"},
    {"path": "../../../windows/system32/config/sam"},
    {"folder": "C:/Windows"},
    {"value": 1e308},                        # the largest finite float
    {"rating": 999},
    {"rating": -5},
    {"degrees": 37},                         # not a multiple of 90
    {"lat": 500, "lon": 500},
    {"date": "9999-99-99T99:99:99"},
    {"unexpected_field": "surprise"},
]


def fingerprint(root: Path) -> dict[str, str]:
    """Path -> sha256 for every file under the library root."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def mutating_endpoints(app):
    schema = app.openapi()
    out = []
    for path, item in sorted(schema["paths"].items()):
        if path in SKIP:
            continue
        for method in ("post", "put", "patch", "delete"):
            if method in item:
                out.append((method, path))
    return out


def fill(path: str, photo_id: int) -> str | None:
    if "{" not in path:
        return path
    filled = path
    for token in ("{photo_id}", "{id}", "{person_id}", "{album_id}", "{event_id}",
                  "{place_id}", "{group_id}", "{face_id}", "{job_id}", "{root_id}",
                  "{stack_id}"):
        filled = filled.replace(token, str(photo_id))
    filled = filled.replace("{token}", "not-a-real-token")
    return None if "{" in filled else filled


def test_hostile_bodies_never_crash_a_mutating_endpoint(ctx, app, library_root):
    client = TestClient(app, raise_server_exceptions=False)
    conn = ctx.connect()
    photo_id = int(conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0])
    conn.close()

    endpoints = [(m, fill(p, photo_id)) for m, p in mutating_endpoints(app)]
    endpoints = [(m, p) for m, p in endpoints if p]
    assert len(endpoints) > 30, f"only {len(endpoints)} mutating endpoints swept"

    failures = []
    for method, path in endpoints:
        for body in BODIES:
            r = client.request(method.upper(), path, json=body)
            if crashed(r):
                failures.append(f"{method.upper()} {path} {str(body)[:60]} -> {r.status_code}")
                break
    assert not failures, f"{len(failures)} mutating endpoints crashed:\n" + "\n".join(failures[:40])


def test_no_malformed_request_can_alter_an_original(ctx, app, library_root):
    """The non-negotiable, asserted against the whole write surface.

    Originals are opened read-only and only the Trash may move one, on an explicit
    click. After throwing every hostile body at every mutating endpoint, each file
    under the library root must still be present and byte-for-byte identical.
    """
    client = TestClient(app, raise_server_exceptions=False)
    conn = ctx.connect()
    photo_id = int(conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0])
    conn.close()

    before = fingerprint(library_root)
    assert len(before) == 6, f"expected 6 originals, found {len(before)}"

    tried = 0
    for method, path in mutating_endpoints(app):
        filled = fill(path, photo_id)
        if not filled:
            continue
        for body in BODIES:
            client.request(method.upper(), filled, json=body)
            tried += 1
    # So the test cannot pass by quietly iterating over nothing.
    assert tried > 700, f"only {tried} requests sent"

    after = fingerprint(library_root)
    assert set(after) == set(before), (
        f"files appeared or vanished from the library root.\n"
        f"  gone: {sorted(set(before) - set(after))}\n"
        f"  new:  {sorted(set(after) - set(before))}")
    changed = [k for k in before if before[k] != after[k]]
    assert not changed, f"originals were modified: {changed}"


def test_an_unwritable_export_folder_is_refused_clearly(ctx, app, library_root):
    """Picking a folder you cannot write to is a mistake to report, not a crash.

    It is checked before any file is written: failing partway left a half-finished
    export behind. _check_destination is shared by the XMP, photo and backup
    exports, so all three inherit this.
    """
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/export/xmp", json={"folder": "C:/Windows"})
    assert r.status_code == 400, f"-> {r.status_code} {r.text[:160]}"
    assert "cannot write" in r.json()["detail"].lower()
    # Nothing was created on the way to finding that out.
    assert not Path("C:/Windows/memoria-export.json").exists()


def test_an_export_folder_inside_the_library_is_still_refused(ctx, app, library_root):
    """The writability probe must not have displaced the check that protects
    originals: an export may never write into a photo root."""
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/export/xmp", json={"folder": str(library_root)})
    assert r.status_code == 400
    assert "inside the photo folder" in r.json()["detail"]
    assert not list(library_root.glob("*.xmp"))


# ----------------------------------------------------------------- the CLI

def test_the_cli_reports_an_export_refusal_instead_of_a_traceback(ctx, library_root, capsys):
    """`export-xmp` into a photo root is refused by design — but the command had no
    handler at all, so the documented refusal arrived as a Python traceback."""
    from photointel import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--data", str(ctx.paths.data), "export-xmp", str(library_root)])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "Export refused" in err and "inside the photo folder" in err
    assert "Traceback" not in err


def test_the_cli_reports_an_unwritable_export_folder(ctx, library_root, capsys):
    from photointel import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--data", str(ctx.paths.data), "export-xmp", "C:/Windows"])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "cannot write to" in err and "Traceback" not in err
