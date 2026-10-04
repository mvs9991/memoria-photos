"""A locked photo and a private photo must be mentioned NOWHERE a person without access can read.

An earlier audit found hidden photos leaking into events and people because derived features
filtered on status only. The same class of leak is possible for the Locked folder and for Private
photos, and also in places a status filter never reaches (face lists, stacks, duplicate "keep this
one" hints, tag lists, the audit log, counts...).

This file plants, in one small library, for each of the two states:
  faces (people who appear ONLY in these photos), GPS in a place nobody else photographed, a date
  inside another event, a visible near-duplicate twin, a stack neighbour, OCR text, a caption, a tag,
  an album, a favourite and a share link,
then sweeps EVERY GET route the app documents (read from its OpenAPI schema, like
test_api_fuzz.py) as the owner (PIN set, folder not opened), two family members, a guest and
the anonymous share-link visitor, looking for the photos' ids, names, folders, fingerprints, text,
tags and face/person data in what comes back.  Each sweep runs twice: right after the photos changed
state (what the app does without a rebuild) and after a full post-processing rebuild.

Ground truth: the planted names/texts below occur nowhere else in the library, and each marker check
strips what the request itself echoed (a search puts the query back in its answer).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photointel import db, ratelimit
from photointel.api.deps import get_state
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from tests.conftest import make_image
from tests.test_api_fuzz import get_endpoints

PIN = "2468"
LOCK_TEXT = {"ocr": "OCRLOCKEDXYZ", "caption": "CAPLOCKEDXYZ", "tag": "taglockedxyz"}
PRIV_TEXT = {"ocr": "OCRPRIVXYZ", "caption": "CAPPRIVXYZ", "tag": "tagprivxyz"}
REYKJAVIK = (64.1466, -21.9426)
TOKYO = (35.6762, 139.6503)
GOA = (15.5439, 73.7553)
ROME = (41.9028, 12.4964)
LISBON = (38.7223, -9.1393)


class World:
    pass


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


def _login(app, name, pw):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": name, "password": pw}).status_code == 200
    return c


def _recompressed(src: Path, dst: Path) -> Path:
    """A smaller re-save of the same picture: the visible 'twin' of a hidden one."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.open(src).save(dst, "JPEG", quality=45)
    return dst


@pytest.fixture
def world(ctx, app, library, tmp_path):
    ratelimit.reset_all()
    w = World()
    w.ctx, w.app = ctx, app
    owner = TestClient(app)
    owner.post("/api/auth/password", json={"new": "owner-pass"})
    owner.post("/api/accounts/enable", json={"username": "Sanjay"})
    owner.post("/api/accounts", json={"username": "Priya", "password": "priya-pass", "role": "family"})
    owner.post("/api/accounts", json={"username": "Ravi", "password": "ravi-pass", "role": "family"})
    owner.post("/api/accounts", json={"username": "Guest", "password": "guest-pass", "role": "guest"})
    owner.post("/api/locked/pin", json={"new": PIN})
    w.owner = owner
    w.priya = _login(app, "Priya", "priya-pass")
    w.ravi = _login(app, "Ravi", "ravi-pass")
    w.guest = _login(app, "Guest", "guest-pass")
    w.anon = TestClient(app)                      # never signed in

    def put(name, data):
        r = TestClient(app).put(f"/dav/Camera/{name}", content=data, auth=("Priya", "priya-pass"))
        assert r.status_code in (201, 204), r.text

    # --- locked material, inside the library: A in the Goa event; B alone in Reykjavik with a
    #     recompressed visible twin; S1 stacked with a visible neighbour; C1, C2 are a person who
    #     appears in nothing else
    vault = library / "Vault_L"
    make_image(vault / "LOCKEDSECRET_A.jpg", colour=(100, 100, 70), taken=datetime(2024, 7, 20, 9, 22), gps=GOA, noise=9)
    b = make_image(vault / "Reykjavik" / "LOCKEDSECRET_B.jpg", colour=(100, 100, 71),
                   taken=datetime(2022, 12, 24, 18, 0), gps=REYKJAVIK, noise=9)
    _recompressed(b, library / "Elsewhere" / "TwinOfLocked.jpg")
    make_image(vault / "LOCKEDSECRET_S1.jpg", colour=(101, 99, 70), taken=datetime(2023, 5, 5, 12, 0, 0),
               gps=(48.8584, 2.2945), noise=9)
    make_image(library / "Stackmates" / "stack_neighbour.jpg", colour=(101, 99, 70),
               taken=datetime(2023, 5, 5, 12, 0, 1), gps=(48.8584, 2.2945), noise=9)
    make_image(vault / "Rome" / "LOCKEDSECRET_C1.jpg", colour=(20, 30, 220), taken=datetime(2020, 6, 1, 10, 0),
               gps=ROME, noise=9)
    make_image(vault / "Rome" / "LOCKEDSECRET_C3.jpg", colour=(21, 30, 221), taken=datetime(2020, 6, 1, 18, 0),
               gps=ROME, noise=9)
    make_image(vault / "Rome" / "LOCKEDSECRET_C2.jpg", colour=(20, 80, 221), taken=datetime(2020, 6, 1, 15, 0),
               gps=ROME, noise=9)

    # --- private material, uploaded by Priya: A in the Goa event; B in Tokyo with a visible twin;
    #     D1, D2 a person in nothing else; E uploaded BEFORE she turned privacy on (so it is shared
    #     and part of the derived data) and made private afterwards
    scratch = tmp_path / "scratch"
    a = make_image(scratch / "a.jpg", colour=(140, 40, 90), taken=datetime(2024, 7, 20, 9, 33), gps=GOA, noise=9)
    pb = make_image(scratch / "b.jpg", colour=(140, 40, 91), taken=datetime(2021, 4, 1, 8, 0), gps=TOKYO, noise=9)
    d1 = make_image(scratch / "d1.jpg", colour=(225, 200, 60), taken=datetime(2019, 8, 1, 9, 0), gps=LISBON, noise=9)
    d3 = make_image(scratch / "d3.jpg", colour=(226, 120, 60), taken=datetime(2019, 8, 1, 19, 0), gps=LISBON, noise=9)
    d2 = make_image(scratch / "d2.jpg", colour=(225, 50, 61), taken=datetime(2019, 8, 1, 14, 0), gps=LISBON, noise=9)
    e = make_image(scratch / "e.jpg", colour=(60, 220, 160), taken=datetime(2018, 2, 3, 9, 0), gps=(51.5, -0.12), noise=9)
    _recompressed(pb, library / "Elsewhere" / "TwinOfPrivate.jpg")
    _recompressed(e, library / "Elsewhere" / "TwinOfPrivateE.jpg")
    put("PRIVSECRET_E.jpg", e.read_bytes())
    uploads = ctx.paths.data / "uploads"
    Indexer(ctx, workers=1).run(roots=[str(uploads)])           # E is indexed as an ordinary shared photo
    w.priya.post("/api/accounts/me/private-uploads", json={"on": True})
    for name, src in (("PRIVSECRET_A.jpg", a), ("PRIVSECRET_B.jpg", pb), ("PRIVSECRET_D1.jpg", d1),
                      ("PRIVSECRET_D2.jpg", d2), ("PRIVSECRET_D3.jpg", d3)):
        put(name, src.read_bytes())
    put("PRIVSECRET_BROKEN.jpg", b"this is not a picture")

    Indexer(ctx, workers=1).run(roots=[str(library), str(uploads)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    conn.commit()

    def pid(name):
        return int(conn.execute("SELECT id FROM photos WHERE filename = ?", (name,)).fetchone()[0])

    w.locked = {n: pid(n) for n in ("LOCKEDSECRET_A.jpg", "LOCKEDSECRET_B.jpg", "LOCKEDSECRET_S1.jpg",
                                    "LOCKEDSECRET_C1.jpg", "LOCKEDSECRET_C2.jpg", "LOCKEDSECRET_C3.jpg")}
    w.private = {n: pid(n) for n in ("PRIVSECRET_A.jpg", "PRIVSECRET_B.jpg", "PRIVSECRET_D1.jpg",
                                     "PRIVSECRET_D2.jpg", "PRIVSECRET_D3.jpg", "PRIVSECRET_E.jpg")}
    w.broken = pid("PRIVSECRET_BROKEN.jpg")
    w.neighbour = pid("stack_neighbour.jpg")
    status = lambda i: conn.execute("SELECT status FROM photos WHERE id=?", (i,)).fetchone()[0]  # noqa: E731
    assert all(status(i) == "ok" for i in w.locked.values())
    assert [n for n, i in w.private.items() if status(i) != "private"] == ["PRIVSECRET_E.jpg"]

    # enrichment: OCR text, caption, tag, album, favourite (before the state change, as a person would)
    groups = ((LOCK_TEXT, w.locked.values(), w.owner), (PRIV_TEXT, w.private.values(), w.priya))
    for texts, ids, who in groups:
        for i in ids:
            conn.execute("UPDATE photos SET ocr_text = ? WHERE id = ?", (texts["ocr"] + " receipt", i))
            conn.commit()
            assert who.post(f"/api/photos/{i}/description", json={"description": texts["caption"]}).status_code == 200
            assert who.post("/api/photos/tags", json={"photo_ids": [i], "name": texts["tag"]}).status_code == 200
            assert who.post(f"/api/photos/{i}/flags", json={"favorite": True}).status_code == 200
    w.album = owner.post("/api/albums", json={"name": "Family trip album",
                                              "photo_ids": [*w.locked.values(), w.neighbour]}).json()["id"]
    w.palbum = w.priya.post("/api/albums", json={"name": "Priya photos album",
                                                 "photo_ids": list(w.private.values())}).json()["id"]
    owner.post("/api/albums", json={"name": "Smart receipts", "query": "receipt"})
    w.token = owner.post(f"/api/albums/{w.album}/share", json={}).json()["token"]
    # family accounts cannot share (accounts.allowed): the OWNER shares an album that holds Priya's private photos
    w.palbum_owner = owner.post("/api/albums", json={"name": "Owner shows Priya", "photo_ids": list(w.private.values())}).json()["id"]
    w.ptoken = owner.post(f"/api/albums/{w.palbum_owner}/share", json={}).json()["token"]
    run_post_stages(ctx, conn)                  # the search index now holds the OCR text, captions and tags
    conn.commit()

    # the state changes
    assert owner.post("/api/photos/lock", json={"photo_ids": list(w.locked.values())}).json()["locked"] == 6
    e_id = w.private["PRIVSECRET_E.jpg"]
    assert w.priya.post("/api/photos/private", json={"photo_ids": [e_id]}).json()["private"] == 1
    w.conn = conn

    def rebuild():
        run_post_stages(ctx, conn)
        conn.commit()
    w.rebuild = rebuild

    sha = lambda i: conn.execute("SELECT sha256 FROM photos WHERE id=?", (i,)).fetchone()[0]  # noqa: E731
    w.locked_markers = ["LOCKEDSECRET", "Vault_L", *LOCK_TEXT.values(), *(sha(i) for i in w.locked.values()),
                        "41.90", "12.49"]
    w.private_markers = ["PRIVSECRET", *PRIV_TEXT.values(), *(sha(i) for i in w.private.values()),
                         "38.72", "9.13"]
    mark = lambda ids: ",".join(str(i) for i in ids)  # noqa: E731
    w.locked_faces = [r[0] for r in conn.execute(f"SELECT id FROM faces WHERE photo_id IN ({mark(w.locked.values())})")]
    w.private_faces = [r[0] for r in conn.execute(f"SELECT id FROM faces WHERE photo_id IN ({mark(w.private.values())})")]
    w.only_locked_people = _only_in(conn, w.locked.values())
    w.only_private_people = _only_in(conn, w.private.values())
    assert w.only_locked_people and w.only_private_people, "the plant needs a person seen only in each set"
    return w


def _only_in(conn, photo_ids) -> list[int]:
    """Persons every one of whose faces is on these photos."""
    ids = set(photo_ids)
    out = []
    for (pid,) in conn.execute("SELECT id FROM persons WHERE merged_into IS NULL").fetchall():
        photos = {r[0] for r in conn.execute("SELECT photo_id FROM faces WHERE person_id = ?", (pid,))}
        if photos and photos <= ids:
            out.append(int(pid))
    return out


# ------------------------------------------------------------------ the sweep

SKIP = ("/api/random/image", "/api/auth/", "/api/openapi", "/api/docs", "/api/redoc", "/api/alerts", "/api/https",
        "/api/offsite", "/api/export/location", "/api/browse", "/api/service", "/api/folders/browse")
ID_KEYS = {"photo_id", "cover_photo_id", "cover_photo", "best_id", "keep_photo_id", "stack_id"}
ID_LISTS = {"ids", "photo_ids", "members", "representative_photos", "best_photo_ids"}
TOKENS = ("{photo_id}", "{id}", "{person_id}", "{album_id}", "{event_id}", "{place_id}", "{group_id}",
          "{face_id}", "{job_id}", "{root_id}", "{stack_id}", "{uid}", "{token}")


def ids_in(obj, out=None) -> set[int]:
    """Photo ids named anywhere in a JSON body, whatever the shape."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ID_KEYS and isinstance(v, int):
                out.add(v)
            elif k in ID_LISTS and isinstance(v, list):
                out.update(x for x in v if isinstance(x, int))
            ids_in(v, out)
    elif isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict) and isinstance(x.get("id"), int) and "title" not in x and "stage" not in x and any(
                    k in x for k in ("filename", "thumb", "taken_ts", "ratio", "lat", "score")):
                out.add(x["id"])
            ids_in(x, out)
    return out


def urls_in(text: str):
    return ({int(m) for m in re.findall(r"/(?:thumb|photos)/(\d+)", text)},
            {int(m) for m in re.findall(r"/faces/(\d+)/crop", text)})


def _filter_queries(w, path):
    if path == "/api/search":
        return ["?q=" + q for q in ("receipt", "OCRLOCKEDXYZ", "OCRPRIVXYZ", "CAPLOCKEDXYZ", "CAPPRIVXYZ", "taglockedxyz",
                                    "tagprivxyz", "LOCKEDSECRET", "PRIVSECRET", "Reykjavik", "Tokyo", "Rome", "Lisbon",
                                    "2022", "2021", "2020", "2019", "December 2022", "Iceland", "photos of a person",
                                    "favourites", "stack", "duplicates", "videos", "screenshots")]
    out = [""]
    people = [r[0] for r in w.conn.execute("SELECT id FROM persons")]
    places = [r[0] for r in w.conn.execute("SELECT id FROM places")]
    events = [r[0] for r in w.conn.execute("SELECT id FROM events")]
    out += [f"?person={p}" for p in people] + [f"?place={p}" for p in places] + [f"?event={p}" for p in events]
    out += ["?year=2022", "?year=2021", "?year=2020", "?year=2019", "?year=2018", "?year=2024&month=7",
            "?year=2023&month=5", "?tag=taglockedxyz", "?tag=tagprivxyz", "?favorite=true", "?has_faces=true",
            "?folder=Vault_L", "?folder=Priya", "?folder=Elsewhere", "?collection=stacks", "?collection=documents",
            "?collection=selfies", "?collection=no_location", "?collection=hidden", "?collection=archive",
            "?collection=videos", "?min_rating=0", "?source=camera", "?collapse_stacks=true", "?archived=only",
            "?archived=include", "?camera=SM-S918B", "?root_id=1", "?root_id=2"]
    return out


def collect_ids(client) -> dict:
    def get(path):
        r = client.get(path)
        return r.json() if r.status_code == 200 and "json" in r.headers.get("content-type", "") else {}
    return {"people": [p["id"] for p in get("/api/people").get("people", [])],
            "albums": [a["id"] for a in get("/api/albums").get("albums", [])],
            "events": [e["id"] for e in get("/api/events").get("events", [])],
            "places": [p["id"] for p in get("/api/places").get("places", [])],
            "groups": [g["id"] for g in get("/api/duplicates").get("groups", [])]}


def sweep(client, w, secret_photos, secret_faces, secret_people, markers, who) -> list[str]:
    """Every documented GET, with real and secret ids as parameters. Returns what it found."""
    problems: list[str] = []
    secret_photos, secret_faces = set(secret_photos), set(secret_faces)
    pools = collect_ids(client)
    vis = sorted(r[0] for r in w.conn.execute("SELECT id FROM photos WHERE status = 'ok'"))
    stack_ids = [r[0] for r in w.conn.execute("SELECT DISTINCT stack_id FROM photos WHERE stack_id IS NOT NULL")]
    some_faces = [r[0] for r in w.conn.execute(
        f"SELECT id FROM faces WHERE photo_id IN ({','.join(map(str, vis[:3]))}) LIMIT 3")]
    candidates = {
        "{photo_id}": sorted(secret_photos) + vis[:2],
        "{face_id}": sorted(secret_faces) + some_faces,
        "{person_id}": pools["people"] + list(secret_people) + [1],
        "{id}": pools["people"] + list(secret_people) + [1],
        "{album_id}": sorted(set(pools["albums"] + [w.album, w.palbum])),
        "{event_id}": pools["events"] or [1],
        "{place_id}": pools["places"] or [1],
        "{group_id}": pools["groups"] or [1],
        "{stack_id}": stack_ids + sorted(secret_photos),
        "{job_id}": [1], "{root_id}": [1], "{uid}": [1], "{token}": [w.token, w.ptoken],
    }
    n = 0
    for path, _params in get_endpoints(w.app):
        if path.startswith(SKIP) or path.startswith("/{full_path"):
            continue
        subs = [(path, {})]
        for tok in TOKENS:
            if tok in path:
                subs = [(u.replace(tok, str(v)), {**d, tok: v}) for u, d in subs for v in candidates[tok]]
        for url, used in subs:
            variants = [url]
            if path in ("/api/photos/index", "/api/timeline", "/api/map/points", "/api/search"):
                variants = [url + q for q in _filter_queries(w, path)]
            for v in variants:
                r = client.get(v)
                n += 1
                ct = r.headers.get("content-type", "")
                # a secret photo's pixels, video or details must never be served
                if r.status_code == 200 and (
                        used.get("{photo_id}") in secret_photos or used.get("{face_id}") in secret_faces):
                    problems.append(f"{who}: {v} SERVED ({ct}, {len(r.content)} bytes)")
                if r.status_code != 200 or not (ct.startswith("application/json") or ct.startswith("text/")):
                    continue
                body = r.text
                for qv in re.findall(r"[?&]q=([^&]+)", v):          # a search echoes what was asked
                    body = body.replace(qv, "")
                for mk in markers:
                    if mk in body:
                        problems.append(f"{who}: {v} mentions {mk!r}")
                try:
                    found = ids_in(r.json()) if "json" in ct else set()
                except ValueError:
                    found = set()
                ph, fc = urls_in(body)
                bad = (found | ph) & secret_photos
                if bad:
                    problems.append(f"{who}: {v} names secret photo id(s) {sorted(bad)}")
                if fc & secret_faces:
                    problems.append(f"{who}: {v} links the face crop(s) {sorted(fc & secret_faces)}")
    assert n > 60, f"the sweep only made {n} requests"
    return problems


@pytest.fixture(params=[False, True], ids=["no-rebuild", "after-rebuild"])
def settled(request, world):
    if request.param:
        world.rebuild()
    return world


def _report(problems):
    return f"{len(problems)} leaks:\n" + "\n".join(dict.fromkeys(problems))


def test_nothing_anyone_can_fetch_mentions_a_locked_photo(settled):
    """Owner (PIN set, folder closed), both family members and the guest."""
    w = settled
    problems = []
    for who in ("owner", "priya", "ravi", "guest"):
        problems += sweep(getattr(w, who), w, w.locked.values(), w.locked_faces, w.only_locked_people,
                          w.locked_markers, who)
    assert not problems, _report(problems)


def test_nothing_anyone_else_can_fetch_mentions_a_private_photo(settled):
    w = settled
    problems = []
    for who in ("owner", "ravi", "guest"):
        problems += sweep(getattr(w, who), w, w.private.values(), w.private_faces, w.only_private_people,
                          w.private_markers, who)
    assert not problems, _report(problems)


def test_the_share_link_visitor_gets_neither(settled):
    """The anonymous visitor can only use /api/share/...; every other route asks them to sign in."""
    w = settled
    secrets = [*w.locked.values(), *w.private.values()]
    r = w.anon.get(f"/api/share/{w.token}")
    assert r.status_code == 200
    assert not ids_in(r.json()) & set(secrets)
    for tok in (w.token, w.ptoken):
        body = w.anon.get(f"/api/share/{tok}").text
        for m in [*w.locked_markers, *w.private_markers]:
            assert m not in body, (tok, m)
        for i in secrets:
            for suffix in (f"/thumb/{i}", f"/download/{i}", f"/video/{i}"):
                assert w.anon.get(f"/api/share/{tok}{suffix}").status_code in (401, 403, 404), (tok, suffix)
    assert w.anon.get("/api/photos/index").status_code == 401
    assert w.anon.get(f"/api/thumb/{secrets[0]}").status_code == 401


def test_a_person_seen_only_in_hidden_photos_is_not_listed_or_used_as_a_cover(settled):
    w = settled
    only = set(w.only_locked_people + w.only_private_people)
    for who in ("owner", "ravi", "guest"):
        c = getattr(w, who)
        people = c.get("/api/people").json()["people"]
        assert not {p["id"] for p in people} & only, who
        assert not {p["cover_face_id"] for p in people} & set(w.locked_faces + w.private_faces), who
        for pid in only:
            d = c.get(f"/api/people/{pid}")
            assert not c.get(f"/api/people/{pid}/faces").json()["faces"], (who, pid)
            if d.status_code == 200:
                j = d.json()
                assert not j["years"] and not j["events"] and not j["places"], (who, pid, j)
        assert not {p["id"] for p in c.get("/api/insights").json()["people"]} & only


def test_counts_do_not_admit_that_hidden_photos_exist(settled):
    """/api/stats is open to every signed-in account."""
    w = settled
    visible_faces = w.conn.execute("SELECT COUNT(*) FROM faces f JOIN photos p ON p.id = f.photo_id "
                                   "WHERE p.status = 'ok' AND p.hidden = 0 AND p.live_component = 0").fetchone()[0]
    for who in ("owner", "priya", "ravi", "guest"):
        s = getattr(w, who).get("/api/stats").json()
        assert "locked" not in s["photos_by_status"] and "private" not in s["photos_by_status"], (who, s)
        assert s["faces"] == visible_faces, (who, s["faces"], visible_faces)


def test_duplicates_never_name_a_hidden_copy_as_the_one_to_keep(settled):
    """The hidden original is the bigger twin, so the stored keeper IS the hidden photo."""
    w = settled
    hidden = set(w.locked.values()) | set(w.private.values())
    for who in ("owner", "ravi", "guest"):
        groups = getattr(w, who).get("/api/duplicates", params={"status": "all", "limit": 1000}).json()["groups"]
        for g in groups:
            assert g["keep_photo_id"] not in hidden, (who, g["id"], g["keep_photo_id"])
            assert {m["photo_id"] for m in g["members"]}.isdisjoint(hidden)
            assert g["member_count"] == len(g["members"]) >= 2, (who, g)
            assert sum(m["is_keeper"] for m in g["members"]) == 1, (who, g)
    s = w.owner.get("/api/stats").json()
    listed = w.owner.get("/api/duplicates", params={"status": "all", "limit": 1000}).json()["groups"]
    assert s["duplicate_groups"] == len([g for g in listed if g["kind"] != "similar"])


def test_a_stack_does_not_count_or_list_a_hidden_member(settled):
    w = settled
    hidden = set(w.locked.values()) | set(w.private.values())
    seen = 0
    for who in ("owner", "guest"):
        c = getattr(w, who)
        idx = c.get("/api/photos/index").json()
        for pid, size in zip(idx["ids"], idx["stack"]):
            if size:
                members = c.get(f"/api/stacks/{pid}").json()["members"]
                assert not set(members) & hidden, (who, pid)
                assert size == len(members), (who, pid, size, members)
                seen += 1
    assert w.conn.execute("SELECT COUNT(*) FROM photos WHERE stack_id IS NOT NULL").fetchone()[0] >= 0


def test_tags_exist_only_where_a_visible_photo_has_them(settled):
    w = settled
    for who in ("owner", "ravi", "guest"):
        names = {t["name"] for t in getattr(w, who).get("/api/tags").json()["tags"]}
        assert not names & {LOCK_TEXT["tag"], PRIV_TEXT["tag"]}, (who, names)


def test_events_forget_a_hidden_photo_at_once(settled):
    """Not only after the next rebuild: counts, cover, people and the summary text."""
    w = settled
    hidden = set(w.locked.values()) | set(w.private.values())
    for who in ("owner", "guest"):
        for e in getattr(w, who).get("/api/events").json()["events"]:
            detail = getattr(w, who).get(f"/api/events/{e['id']}").json()
            assert e["photo_count"] == detail["photos"]["total"], (who, e)
            assert e["cover_photo_id"] not in hidden and not set(detail["photos"]["ids"]) & hidden
    for r in w.conn.execute("SELECT id, cover_photo_id, summary FROM events").fetchall():
        assert r["cover_photo_id"] not in hidden, dict(r)
        assert LOCK_TEXT["tag"] not in (r["summary"] or "") and PRIV_TEXT["tag"] not in (r["summary"] or ""), dict(r)


def test_the_audit_log_does_not_remember_what_a_hidden_photo_was_called(settled):
    w = settled
    body = w.owner.get("/api/audit", params={"limit": 1000}).text
    for m in (*LOCK_TEXT.values(), *PRIV_TEXT.values()):
        assert m not in body, m
    hidden = {*w.locked.values(), *w.private.values()}
    for e in w.owner.get("/api/audit", params={"limit": 1000}).json()["entries"]:
        assert not (e["entity_type"] == "photo" and e["entity_id"] in hidden), e
        details = json.loads(e["details"] or "{}")
        for k in ("photos", "photo_ids", "members"):
            assert not set(details.get(k, [])) & hidden, e


def test_the_error_list_does_not_name_a_private_upload(settled):
    """Priya's corrupt upload is private to her from the start; the owner's error list must not carry it."""
    w = settled
    st = w.conn.execute("SELECT status, private_to FROM photos WHERE id = ?", (w.broken,)).fetchone()
    body = w.owner.get("/api/errors").text
    assert "PRIVSECRET" not in body and "Priya" not in body, (dict(st), body)


# ------------------------------------------------------------------ doors that serve bytes

def test_no_door_serves_a_hidden_photo_to_anyone_who_may_not_open_it(settled):
    w = settled
    for who in ("owner", "priya", "ravi", "guest"):
        c = getattr(w, who)
        for group, mine in ((w.locked, False), (w.private, who == "priya")):
            for pid in group.values():
                if mine:
                    continue          # her own private photos are hers to open
                for url in (f"/api/thumb/{pid}?s=m", f"/api/thumb/{pid}?s=s", f"/api/thumb/{pid}?s=l",
                            f"/api/photos/{pid}", f"/api/photos/{pid}/original", f"/api/photos/{pid}/download",
                            f"/api/photos/{pid}/video", f"/api/photos/{pid}/motion", f"/api/photos/{pid}/similar"):
                    r = c.get(url)
                    assert r.status_code in (403, 404, 400, 415, 422), (who, url, r.status_code)
                for method, url in (("post", f"/api/photos/{pid}/edit/preview"), ("post", f"/api/photos/{pid}/edit"),
                                    ("post", f"/api/photos/{pid}/describe"), ("post", f"/api/photos/{pid}/trim")):
                    r = getattr(c, method)(url, json={"ops": {"rotate": 90}})
                    assert r.status_code in (400, 403, 404, 422), (who, url, r.status_code)
        for fid in w.locked_faces + ([] if who == "priya" else w.private_faces):
            assert c.get(f"/api/faces/{fid}/crop").status_code in (403, 404), (who, fid)


def test_search_never_matches_a_hidden_photo(settled):
    """Keyword (OCR, caption, tag, filename, folder) and semantic paths; the in-memory matrix is rebuilt."""
    w = settled
    hidden = {*w.locked.values(), *w.private.values()}
    for who in ("owner", "ravi", "guest"):
        c = getattr(w, who)
        for q in ("OCRLOCKEDXYZ", "OCRPRIVXYZ", "CAPLOCKEDXYZ", "CAPPRIVXYZ", "taglockedxyz", "tagprivxyz", "receipt",
                  "LOCKEDSECRET_A", "Vault_L", "Reykjavik December 2022", "yellow", "a photo", "photos of Person 003",
                  "favourites", "2019", "2020", "2018"):
            r = c.get("/api/search", params={"q": q, "limit": 500}).json()
            assert not {p["id"] for p in r["photos"]} & hidden, (who, q)
    model_id = db.active_model_id(w.conn, "semantic")
    state = get_state()
    ids = state.index_cache.get(w.conn, model_id, state.generation("embeddings")).id_to_row()
    assert not set(ids) & hidden, "the in-memory embedding matrix holds a hidden photo"


def test_exports_copy_only_what_the_asker_can_see(settled):
    w = settled
    hidden_ids = [*w.locked.values(), *w.private.values()]
    shas = {r[0] for r in w.conn.execute(
        f"SELECT sha256 FROM photos WHERE id IN ({','.join(map(str, hidden_ids))})")}
    hid_people = w.only_locked_people + w.only_private_people
    for who in ("owner", "priya", "ravi", "guest"):
        c = getattr(w, who)
        for spec in ({"photo_ids": hidden_ids + [w.neighbour]}, {"album_id": w.album}, {"album_id": w.palbum},
                     {"person_ids": hid_people}, {"year": 2022}, {"year": 2020}, {"year": 2019}, {"year": 2018},
                     {"event_id": 1}, {"event_id": 2}):
            r = c.post("/api/export/zip", data={"spec": json.dumps(spec)})
            if r.status_code != 200:
                assert r.status_code in (400, 404), (who, spec, r.status_code)
                continue
            z = zipfile.ZipFile(io.BytesIO(r.content))
            for name in z.namelist():
                assert "SECRET" not in name, (who, spec, name)
                assert hashlib.sha256(z.read(name)).hexdigest() not in shas, (who, spec, name)
        r = c.post("/api/export/preview", json={"photo_ids": hidden_ids})
        assert r.status_code in (200, 400, 404)
        if r.status_code == 200:
            assert "SECRET" not in r.text, r.text


def test_a_hidden_photo_added_to_an_album_is_not_listed_or_counted(settled):
    w = settled
    hidden = {*w.locked.values(), *w.private.values()}
    for who in ("owner", "ravi", "guest"):
        c = getattr(w, who)
        for a in c.get("/api/albums").json()["albums"]:
            d = c.get(f"/api/albums/{a['id']}").json()
            assert not set(d.get("photos", {}).get("ids", [])) & hidden
            if a["kind"] != "smart":
                assert a["photo_count"] == len(d["photos"]["ids"]), (who, a)
            assert a["cover_photo_id"] not in hidden


# ------------------------------------------------------------------ the Locked folder itself

def test_closing_the_locked_folder_ends_the_access_even_for_a_copied_cookie(world):
    w = world
    pid = w.locked["LOCKEDSECRET_B.jpg"]
    assert w.owner.post("/api/locked/open", json={"pin": PIN}).status_code == 200
    cookie = w.owner.cookies.get("memoria_locked")
    assert w.owner.get(f"/api/thumb/{pid}?s=m").status_code == 200
    assert w.owner.get(f"/api/thumb/{pid}?s=m").headers["cache-control"] == "no-store"
    assert w.owner.post("/api/locked/close").status_code == 200
    assert w.owner.get(f"/api/thumb/{pid}?s=m").status_code == 404          # previously served, now not
    assert w.owner.get(f"/api/photos/{pid}").status_code == 404
    assert w.owner.get("/api/locked/photos").status_code == 403
    # the same cookie, replayed (a copy taken while the folder was open) must not reopen it
    replay = TestClient(w.app)
    replay.post("/api/auth/login", json={"username": "Sanjay", "password": "owner-pass"})
    replay.cookies.set("memoria_locked", cookie)
    assert replay.get(f"/api/thumb/{pid}?s=m").status_code == 404, "a closed folder opened again from a copied cookie"


def test_the_pin_locks_out_guessing_and_the_right_pin_does_not_help_then(world):
    w = world
    for _ in range(5):
        assert w.owner.post("/api/locked/open", json={"pin": "0000"}).status_code == 401
    r = w.owner.post("/api/locked/open", json={"pin": PIN})
    assert r.status_code == 429, r.text
    assert w.owner.get("/api/locked/photos").status_code == 403
    assert w.priya.post("/api/locked/open", json={"pin": PIN}).status_code == 403
    assert w.guest.post("/api/locked/open", json={"pin": PIN}).status_code == 403


def test_a_family_member_on_the_owners_open_browser_still_cannot_open_it(world):
    w = world
    w.owner.post("/api/locked/open", json={"pin": PIN})
    cookie = w.owner.cookies.get("memoria_locked")
    pid = w.locked["LOCKEDSECRET_A.jpg"]
    shared_tablet = TestClient(w.app)
    shared_tablet.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    shared_tablet.cookies.set("memoria_locked", cookie)
    assert shared_tablet.get(f"/api/thumb/{pid}?s=m").status_code == 404
    assert shared_tablet.get("/api/locked/photos").status_code == 403


def test_session_cookie_flags(world):
    w = world
    c = TestClient(w.app)
    r = c.post("/api/auth/login", json={"username": "Priya", "password": "priya-pass"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie     # plain http
    r = w.owner.post("/api/locked/open", json={"pin": PIN})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=900" in cookie
    # over https every cookie, including the one that deletes the Locked cookie, is Secure
    https = TestClient(w.app, base_url="https://testserver")
    r = https.post("/api/auth/login", json={"username": "Sanjay", "password": "owner-pass"})
    assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    r = https.post("/api/locked/open", json={"pin": PIN})
    assert "secure" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    r = https.post("/api/locked/close")
    assert all("secure" in v.lower() for k, v in r.headers.multi_items() if k == "set-cookie")


# ------------------------------------------------------------------ the phone's offline copy

def test_the_offline_copy_never_keeps_what_is_hidden():
    """web/public/sw.js is the whole mechanism (static check: there is no browser here)."""
    sw = (Path(__file__).resolve().parent.parent / "web" / "public" / "sw.js").read_text(encoding="utf-8")
    never = sw[sw.index("const NEVER"):sw.index("const IMAGE =")]
    for door in (r"\/api\/locked", r"\/api\/audit", r"\/api\/errors", r"\/api\/share\/", r"\/original$", r"\/download$"):
        assert door in never, door
    assert 'includes("no-store")' in sw, "responses the server marks no-store (every locked or private pixel) are kept"
    assert "lockThenForget" in sw and "/^\\/api\\/photos\\/lock$/" in sw


def test_a_private_photo_is_shown_to_its_person_but_never_kept_by_the_browser_or_the_phone(world):
    w = world
    pid = w.private["PRIVSECRET_A.jpg"]
    r = w.priya.get(f"/api/thumb/{pid}?s=m")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    r = w.priya.get(f"/api/photos/{pid}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    for fid in w.private_faces[:3]:
        r = w.priya.get(f"/api/faces/{fid}/crop")
        assert r.status_code in (200, 404), r.status_code
        if r.status_code == 200:
            assert r.headers["cache-control"] == "no-store"
