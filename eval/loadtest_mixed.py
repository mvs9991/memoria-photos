"""Mixed-load test: does a UI write fail or stall while an indexing job runs against the same database?

Runs against a SYNTHETIC library (never a real one). Three steps, each a subcommand:

    python eval/loadtest_mixed.py gen   --lib D:/pi_cache/loadtest/lib --n 3000
    python eval/loadtest_mixed.py setup --lib ... --data D:/pi_cache/loadtest/data
        (add-root + a first index with faces/semantic off + seeded fake faces so the people stage has work)
    # start the server:  PHOTOINTEL_DATA=<data> PHOTOINTEL_DEVICE=cpu python -m photointel serve --port 8767
    python eval/loadtest_mixed.py run   --lib ... --data ... --port 8767 --baseline 25 --new 600

`run` (1) hammers the API for --baseline seconds with no job, (2) adds --new fresh photos to the library,
POSTs /api/jobs (a real `photointel index` child process: analysis AND all 17 post stages) and hammers
the API from several threads until the job ends, while a probe connection polls `BEGIN IMMEDIATE` with
a tiny timeout (a lock hold longer than --hold-report seconds is recorded with the stage that was
running) and a watcher polls the jobs table (was the job ever marked interrupted while its process
lived?). Writes are checked afterwards against what the clients were told: a lost write is a failure.

It writes only under --lib / --data, and the only thing it removes is nothing.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLACES = [(17.385, 78.4867), (15.5439, 73.7553), (13.6288, 79.4192), (16.5062, 80.648), (12.9716, 77.5946)]
FOLDERS = ["DCIM/Camera", "Trips/Goa", "Trips/Tirupati", "WhatsApp/Images", "Wedding", "Family", "Work/BLR"]
PY = sys.executable


# ------------------------------------------------------------------ library generation
def gen(lib: Path, n: int, start_index: int = 0, seed: int = 11) -> int:
    import numpy as np
    import piexif
    from PIL import Image

    rng = random.Random(seed + start_index)
    nprng = np.random.default_rng(seed + start_index)
    made = 0
    for i in range(start_index, start_index + n):
        day = datetime(2023, 1, 1) + timedelta(days=rng.randint(0, 900), hours=rng.randint(7, 21),
                                               minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
        if rng.random() < 0.5:  # clumps -> events
            day = datetime(2024, 3, 1 + (i // 40) % 25, 10, 0) + timedelta(minutes=(i % 40) * 3)
        folder = FOLDERS[(i // 7) % len(FOLDERS)]
        path = lib / folder / f"IMG_{i:06d}.jpg"
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        base = nprng.integers(0, 255, 3)
        arr = np.clip(base + nprng.integers(-40, 40, (48, 64, 3)), 0, 255).astype(np.uint8)
        exif = {"0th": {piexif.ImageIFD.Make: b"samsung", piexif.ImageIFD.Model: b"SM-S918B"},
                "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        if rng.random() < 0.85:
            stamp = day.strftime("%Y:%m:%d %H:%M:%S").encode()
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
        if rng.random() < 0.6:
            lat, lon = PLACES[(i // 40) % len(PLACES)]
            lat += rng.uniform(-0.01, 0.01)
            lon += rng.uniform(-0.01, 0.01)

            def dms(v):
                d = int(abs(v)); m = int((abs(v) - d) * 60); s = round(((abs(v) - d) * 60 - m) * 6000)
                return ((d, 1), (m, 1), (s, 100))
            exif["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: dms(lat),
                           piexif.GPSIFD.GPSLongitudeRef: b"E", piexif.GPSIFD.GPSLongitude: dms(lon)}
        Image.fromarray(arr).save(path, "JPEG", quality=85, exif=piexif.dump(exif))
        ts = day.timestamp()
        os.utime(path, (ts, ts))
        made += 1
    return made


def seed_faces(data: Path, identities: int = 40, per: int = 100) -> int:
    """Insert synthetic face rows (random unit vectors around centres) so the people stage has work."""
    import numpy as np
    from photointel import db
    conn = db.connect(data / "library.db") if (data / "library.db").exists() else None
    if conn is None:
        raise SystemExit("database not found; run `setup` first")
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO models(kind,name,version,dim,params,created_at) VALUES('face','fake','1',64,'{}',?)", (now,))
    mid = conn.execute("SELECT id FROM models WHERE kind='face' AND name='fake'").fetchone()[0]
    db.set_active_model(conn, "face", mid)
    photos = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' ORDER BY id")]
    rng = np.random.default_rng(5)
    rows = []
    for ident in range(identities):
        c = rng.normal(size=64)
        for _ in range(per):
            v = c + rng.normal(scale=0.25, size=64)
            v = (v / np.linalg.norm(v)).astype(np.float16)
            pid = photos[int(rng.integers(0, len(photos)))]
            rows.append((pid, mid, .2, .2, .5, .5, 0.95, 120.0, 150.0, 0.0, 0.9, v.tobytes(), now))
    conn.executemany("INSERT INTO faces(photo_id,model_id,x1,y1,x2,y2,det_score,size_px,sharpness,yaw,quality,embedding,created_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def cli(data: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PHOTOINTEL_DATA=str(data), PHOTOINTEL_DEVICE="cpu", **(env or {}))
    return subprocess.run([PY, "-m", "photointel", "--data", str(data), *args], capture_output=True, text=True, env=e)


# ------------------------------------------------------------------ HTTP client + recorders
class Rec:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows: list[tuple[str, int, float, float]] = []   # label, status, latency, wall time

    def add(self, label, status, lat):
        with self.lock:
            self.rows.append((label, status, lat, time.time()))


class Client:
    def __init__(self, port, rec: Rec):
        self.port, self.rec = port, rec
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)

    def call(self, label, method, path, body=None):
        t0 = time.time()
        try:
            hdr = {"Content-Type": "application/json"} if body is not None else {}
            self.c.request(method, path, json.dumps(body) if body is not None else None, hdr)
            r = self.c.getresponse()
            data = r.read()
            st = r.status
        except Exception as exc:                      # connection trouble counts as a failure (status 0)
            st, data = 0, str(exc).encode()
            self.c.close()
            self.c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        self.rec.add(label, st, time.time() - t0)
        return st, data


def worker(kind, port, rec, stop, ids, person_ids, expect, errors, seed):
    rng = random.Random(seed)
    cl = Client(port, rec)
    mine = ids[seed % 4::4] if kind.startswith("w") else ids
    n = 0
    while not stop.is_set():
        n += 1
        pid = rng.choice(mine)
        if kind == "reader":
            r = rng.random()
            if r < .3:
                cl.call("GET photos/index", "GET", "/api/photos/index?limit=200")
            elif r < .45:
                cl.call("GET people", "GET", "/api/people")
            elif r < .6:
                cl.call("GET events", "GET", "/api/events")
            elif r < .75:
                cl.call("GET search", "GET", "/api/search?q=" + rng.choice(["goa", "photos from 2024", "hyderabad", "IMG 0001", "tirupati 2023"]))
            elif r < .85:
                cl.call("GET stats", "GET", "/api/stats")
            else:
                cl.call("GET thumb", "GET", f"/api/thumb/{pid}?s=sm")
        else:                                           # writers: each owns a quarter of the ids
            r = rng.random()
            if r < .2:
                st, _ = cl.call("POST hide", "POST", "/api/photos/hide", {"photo_ids": [pid], "hidden": True})
                if st == 200:
                    st, _ = cl.call("POST unhide", "POST", "/api/photos/hide", {"photo_ids": [pid], "hidden": False})
                    if st == 200:
                        expect["hidden"][pid] = 0
            elif r < .45:
                v = rng.randint(1, 5)
                st, _ = cl.call("POST rate", "POST", "/api/photos/rate", {"photo_ids": [pid], "rating": v})
                if st == 200:
                    expect["rating"][pid] = v
            elif r < .65:
                name = f"lt{seed}-{n}"
                st, _ = cl.call("POST tags", "POST", "/api/photos/tags", {"photo_ids": [pid], "name": name})
                if st == 200:
                    expect["tag"].append((pid, name))
            elif r < .8 and person_ids:
                person = person_ids[seed % len(person_ids)]       # one writer per person: last write wins
                name = f"Person-{seed}-{n}"
                st, _ = cl.call("POST rename", "POST", f"/api/people/{person}/rename", {"name": name})
                if st == 200:
                    expect["person"][person] = name
            else:
                fav = rng.random() < .5
                st, _ = cl.call("POST favourite", "POST", f"/api/photos/{pid}/flags", {"favorite": fav})
                if st == 200:
                    expect["fav"][pid] = fav
        time.sleep(rng.uniform(0.0, 0.05))


def probe(data: Path, stop, holds, state, min_hold):
    """Poll BEGIN IMMEDIATE with a tiny timeout; a streak of failures is one lock hold."""
    conn = sqlite3.connect(str(data / "library.db"), timeout=0.05, isolation_level=None)
    streak_start = None
    stage_at_start = None
    while not stop.is_set():
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            if streak_start is not None:
                dur = time.time() - streak_start
                if dur >= min_hold:
                    holds.append((streak_start, dur, stage_at_start, state.get("stage")))
                streak_start = None
        except sqlite3.OperationalError:
            if streak_start is None:
                streak_start, stage_at_start = time.time(), state.get("stage")
        time.sleep(0.02)
    conn.close()


def watch_jobs(data: Path, job_id, stop, state, events):
    conn = sqlite3.connect(str(data / "library.db"), timeout=5)
    last = None
    while not stop.is_set():
        try:
            row = conn.execute("SELECT status,stage,pid,heartbeat_at,progress_done,progress_total FROM jobs WHERE id=?", (job_id,)).fetchone()
        except sqlite3.OperationalError:
            time.sleep(.2)
            continue
        if row:
            st, stage, pid, hb, d, t = row
            state["status"], state["stage"], state["pid"], state["hb"] = st, stage, pid, hb
            if (st, stage) != last:
                events.append((time.time(), st, stage, d, t))
                last = (st, stage)
            if st == "interrupted":
                events.append((time.time(), "INTERRUPTED seen", stage, d, t))
            if hb:
                state["max_hb_age"] = max(state.get("max_hb_age", 0), time.time() - hb) if st == "running" else state.get("max_hb_age", 0)
        time.sleep(0.25)
    conn.close()


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def table(rows, title):
    print(f"\n### {title}  ({len(rows)} requests)")
    print(f"{'endpoint':<18}{'n':>6}{'non2xx':>8}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>10}{'>5s':>5}")
    by: dict[str, list] = {}
    for lab, st, lat, _ in rows:
        by.setdefault(lab, []).append((st, lat))
    out = {}
    for lab in sorted(by) + ["ALL"]:
        xs = [(st, lat) for r in (rows if lab == "ALL" else []) for st, lat in [(r[1], r[2])]] if lab == "ALL" else by[lab]
        lats = [l for _, l in xs]
        bad = sum(1 for s, _ in xs if not 200 <= s < 300)
        slow = sum(1 for l in lats if l > 5)
        print(f"{lab:<18}{len(xs):>6}{bad:>8}{pct(lats, .5) * 1000:>9.1f}{pct(lats, .95) * 1000:>9.1f}{max(lats, default=0) * 1000:>10.1f}{slow:>5}")
        out[lab] = dict(n=len(xs), non2xx=bad, p50=pct(lats, .5), p95=pct(lats, .95), max=max(lats, default=0), over5s=slow)
    return out


def hammer(port, ids, person_ids, seconds, stop_event=None, nread=3, nwrite=4, data=None):
    rec, stop = Rec(), threading.Event()
    expect = {"hidden": {}, "rating": {}, "tag": [], "person": {}, "fav": {}}
    errors: list = []
    ts = [threading.Thread(target=worker, args=("reader", port, rec, stop, ids, person_ids, expect, errors, i)) for i in range(nread)]
    ts += [threading.Thread(target=worker, args=(f"w", port, rec, stop, ids, person_ids, expect, errors, 100 + i)) for i in range(nwrite)]
    for t in ts:
        t.start()
    return rec, stop, ts, expect


def verify(data: Path, expect):
    conn = sqlite3.connect(str(data / "library.db"), timeout=30)
    lost = []
    for pid, v in expect["rating"].items():
        got = conn.execute("SELECT rating FROM photos WHERE id=?", (pid,)).fetchone()[0]
        if got != v:
            lost.append(("rating", pid, v, got))
    for pid in expect["hidden"]:
        got = conn.execute("SELECT hidden FROM photos WHERE id=?", (pid,)).fetchone()[0]
        if got != 0:
            lost.append(("unhide", pid, 0, got))
    for pid, name in expect["tag"]:
        n = conn.execute("SELECT COUNT(*) FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.photo_id=? AND t.name=?", (pid, name.lower())).fetchone()[0]
        if not n:
            n = conn.execute("SELECT COUNT(*) FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.photo_id=? AND lower(t.name)=?", (pid, name.lower())).fetchone()[0]
        if not n:
            lost.append(("tag", pid, name, 0))
    for person, name in expect["person"].items():
        got = conn.execute("SELECT name FROM persons WHERE id=?", (person,)).fetchone()
        if not got or got[0] != name:
            lost.append(("rename", person, name, got and got[0]))
    for pid, fav in expect["fav"].items():
        n = conn.execute("SELECT COUNT(*) FROM favorites WHERE photo_id=?", (pid,)).fetchone()[0] if _has(conn, "favorites") else None
        if n is not None and bool(n) != fav:
            lost.append(("favourite", pid, fav, n))
    conn.close()
    return lost


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["gen", "setup", "run"])
    ap.add_argument("--lib", default="D:/pi_cache/loadtest/lib")
    ap.add_argument("--data", default="D:/pi_cache/loadtest/data_a")
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--baseline", type=float, default=25)
    ap.add_argument("--new", type=int, default=600, help="fresh photos added before the job so it has analysis to do")
    ap.add_argument("--hold-report", type=float, default=1.0)
    ap.add_argument("--job-args", default="", help="extra JSON merged into the POST /api/jobs body")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    lib, data = Path(a.lib), Path(a.data)
    assert "loadtest" in str(lib).lower() and "loadtest" in str(data).lower(), "refusing: not a loadtest directory"
    if a.cmd == "gen":
        print("made", gen(lib, a.n))
        return
    if a.cmd == "setup":
        data.mkdir(parents=True, exist_ok=True)
        # No neural nets: the load test is about the database, and CPU CLIP over thousands of images
        # would only slow the run down. The tags stage skips itself when semantic_model is empty.
        (data / "settings.json").write_text(json.dumps({"semantic_model": "", "ocr_enabled": False}), encoding="utf-8")
        print(cli(data, "add-root", str(lib)).stdout)
        r = cli(data, "index", "--no-faces", "--no-semantic")
        print(r.stdout[-600:], r.stderr[-600:])
        print("seeded faces:", seed_faces(data))
        return
    # ---- run
    ids = [r[0] for r in sqlite3.connect(str(data / "library.db")).execute("SELECT id FROM photos WHERE status='ok' ORDER BY id LIMIT 800")]
    person_ids = [r[0] for r in sqlite3.connect(str(data / "library.db")).execute("SELECT id FROM persons WHERE merged_into IS NULL ORDER BY id LIMIT 8")]
    print(f"{len(ids)} photos to touch, {len(person_ids)} people (0 people = the people stage will create them)")
    result: dict = {}
    # 1) baseline
    rec, stop, ts, expect = hammer(a.port, ids, person_ids, a.baseline)
    time.sleep(a.baseline)
    stop.set()
    [t.join() for t in ts]
    result["baseline"] = table(rec.rows, "NO JOB (baseline)")
    lost = verify(data, expect)
    print("lost writes (baseline):", lost[:5], len(lost))
    result["baseline_lost"] = len(lost)
    # 2) with a job
    print("adding", a.new, "new photos")
    start = len(list(lib.rglob("*.jpg")))
    gen(lib, a.new, start_index=start + 100000)
    body = {"kind": "index"}
    body.update(json.loads(a.job_args or "{}"))
    c = Client(a.port, Rec())
    st, raw = c.call("job", "POST", "/api/jobs", body)
    job_id = json.loads(raw)["job_id"]
    print("job", job_id, "started", st)
    stop_all = threading.Event()
    holds: list = []
    state: dict = {}
    events: list = []
    tp = threading.Thread(target=probe, args=(data, stop_all, holds, state, a.hold_report))
    tw = threading.Thread(target=watch_jobs, args=(data, job_id, stop_all, state, events))
    tp.start(); tw.start()
    rec2, stop2, ts2, expect2 = hammer(a.port, ids, person_ids, 0)
    t_start = time.time()
    while True:
        time.sleep(1)
        if state.get("status") in ("done", "failed", "cancelled", "interrupted") and time.time() - t_start > 3:
            break
        if time.time() - t_start > 3600:
            break
    time.sleep(2)
    stop2.set()
    [t.join() for t in ts2]
    stop_all.set(); tp.join(); tw.join()
    result["job"] = table(rec2.rows, f"WITH JOB {job_id} ({time.time() - t_start:.0f} s)")
    lost2 = verify(data, expect2)
    print("lost writes (during job):", lost2[:8], len(lost2))
    result["job_lost"] = len(lost2)
    print("\njob timeline:")
    for t, st, stage, d, tot in events:
        print(f"  +{t - t_start:7.1f}s  {st:<12} {stage} {d}/{tot}")
    print("final job status:", state.get("status"), " max heartbeat age seen while running: %.1fs" % state.get("max_hb_age", 0))
    print("\nlock holds >= %.1fs seen by probe (BEGIN IMMEDIATE, 50 ms timeout):" % a.hold_report)
    for s, d, st0, st1 in holds:
        print(f"  +{s - t_start:7.1f}s  held {d:6.1f}s  stage at start={st0} at end={st1}")
    result.update(job_status=state.get("status"), holds=[(s - t_start, d, x, y) for s, d, x, y in holds],
                  events=[(t - t_start, st, stage) for t, st, stage, _, _ in events])
    bad = [(l, s, round(t, 1)) for l, s, t, _ in rec2.rows if not 200 <= s < 300][:20]
    print("non-2xx during job:", bad)
    slow = sorted([(round(t, 1), l, s, round(w - t_start, 1)) for l, s, t, w in rec2.rows if t > 5], reverse=True)[:20]
    print("requests waiting > 5 s:", slow)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
