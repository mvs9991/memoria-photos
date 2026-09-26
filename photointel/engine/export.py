"""Export copies of original files: a selection, an album, an event, a year or month, or
the photos of one or more people (a folder each, only where they are together, or any).

Only ever *copies*. The originals are read, never changed, and a destination inside a
photo root is refused. Copies keep the file's bytes and modification time exactly; with
`xmp` a `<file>.xmp` beside each copy carries people, tags, stars and corrections for
other apps. Re-running an export into the same folder skips files already there, so an
interrupted export resumes. The same selection can instead stream as a .zip download,
which is what a phone or another computer on the network gets.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from ..metadata import naive_to_ts, ts_to_naive
from . import albums as albums_mod
from .people import person_label
from .xmp import ExportError, _check_destination, sidecars_for

log = logging.getLogger(__name__)

LAYOUTS = ("date", "flat", "original")
PERSON_MODES = ("each", "together", "any")
VISIBLE = "p.status = 'ok' AND p.live_component = 0"
CHUNK = 1 << 20


@dataclass
class ExportSpec:
    photo_ids: list[int] | None = None
    album_id: int | None = None           # a manual album (a smart one is resolved to photo_ids by the caller)
    event_id: int | None = None
    person_ids: list[int] | None = None
    person_mode: str = "each"
    year: int | None = None
    month: int | None = None
    layout: str = "date"
    include_live: bool = True             # a Live photo's video goes with its still
    include_stack_frames: bool = False    # every file of a RAW+JPEG pair / burst, not just its cover
    xmp: bool = False
    folder: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ExportSpec":
        spec = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if spec.layout not in LAYOUTS:
            raise ExportError(f"layout must be one of {', '.join(LAYOUTS)}")
        if spec.person_mode not in PERSON_MODES:
            raise ExportError(f"person_mode must be one of {', '.join(PERSON_MODES)}")
        if not any([spec.photo_ids, spec.album_id, spec.event_id, spec.person_ids, spec.year]):
            raise ExportError("choose what to export")
        return spec


@dataclass
class Item:
    photo_id: int
    src: Path
    rel_dir: str                          # folder inside the export, posix
    filename: str
    size: int
    sha256: str | None
    mtime: float


@dataclass
class Plan:
    groups: list[dict] = field(default_factory=list)   # [{"label", "count", "bytes"}]
    items: list[Item] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(i.size for i in self.items)


# ------------------------------------------------------------------ what to export

def _filtered(conn: sqlite3.Connection, spec: ExportSpec, person_clause: tuple[str, list] | None) -> list[int]:
    where, args = [VISIBLE], []
    explicit = spec.photo_ids is not None
    if not explicit:
        where.append("p.hidden = 0")
    if spec.photo_ids is not None:
        pool = list(dict.fromkeys(int(x) for x in spec.photo_ids))
    elif spec.album_id is not None:
        pool = albums_mod.album_photo_ids(conn, spec.album_id)
    else:
        pool = None
    if spec.event_id is not None:
        where.append("(p.event_id = ? OR EXISTS (SELECT 1 FROM trip_photos tp WHERE tp.photo_id = p.id "
                     "AND tp.trip_id = ?))")
        args += [spec.event_id, spec.event_id]
    if spec.year:
        start = datetime(spec.year, spec.month or 1, 1)
        end = (datetime(spec.year + 1, 1, 1) if not spec.month or spec.month == 12
               else datetime(spec.year, spec.month + 1, 1))
        where.append("p.taken_ts >= ? AND p.taken_ts < ?")
        args += [naive_to_ts(start), naive_to_ts(end)]
    if person_clause:
        where.append(person_clause[0])
        args += person_clause[1]
    sql = f"SELECT p.id FROM photos p WHERE {' AND '.join(where)}"
    if pool is None:
        return [int(r[0]) for r in conn.execute(sql + " ORDER BY p.taken_ts, p.id", args)]
    keep: set[int] = set()
    for i in range(0, len(pool), 900):
        chunk = pool[i:i + 900]
        keep |= {int(r[0]) for r in conn.execute(sql + f" AND p.id IN ({','.join('?' * len(chunk))})",
                                                 (*args, *chunk))}
    return [pid for pid in pool if pid in keep]


def _has_person(pid: int) -> tuple[str, list]:
    return "EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id = ?)", [pid]


def resolve_groups(conn: sqlite3.Connection, spec: ExportSpec) -> list[tuple[str, list[int]]]:
    """-> [(sub-folder label or "", photo ids)]. People in `each` mode get a folder apiece."""
    people = [int(p) for p in spec.person_ids or []]
    if not people:
        return [("", _filtered(conn, spec, None))]
    if spec.person_mode == "each":
        out = []
        for pid in people:
            row = conn.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
            if row is None:
                raise ExportError(f"no person #{pid}")
            out.append((_safe(person_label(row)), _filtered(conn, spec, _has_person(pid))))
        return out
    if spec.person_mode == "together":
        clauses = [_has_person(p) for p in people]
        clause = (" AND ".join(c[0] for c in clauses), [a for c in clauses for a in c[1]])
    else:
        clause = ("EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id AND f.person_id IN "
                  f"({','.join('?' * len(people))}))", people)
    return [("", _filtered(conn, spec, clause))]


def _expand(conn: sqlite3.Connection, ids: list[int], spec: ExportSpec) -> list[int]:
    out = list(ids)
    seen = set(ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        marks = ",".join("?" * len(chunk))
        extra = []
        if spec.include_stack_frames:
            extra += [r[0] for r in conn.execute(
                f"SELECT id FROM photos WHERE stack_id IN ({marks}) AND status = 'ok' ORDER BY id", chunk)]
        if spec.include_live:
            extra += [r[0] for r in conn.execute(
                f"SELECT live_video_id FROM photos WHERE id IN ({marks}) AND live_video_id IS NOT NULL", chunk)]
        for pid in extra:
            if pid not in seen:
                seen.add(pid)
                out.append(int(pid))
    return out


def plan(conn: sqlite3.Connection, spec: ExportSpec) -> Plan:
    result = Plan()
    roots = {int(r[0]): (Path(r[1]), _safe(Path(r[1]).name or f"root{r[0]}"))
             for r in conn.execute("SELECT id, path FROM roots")}
    for label, ids in resolve_groups(conn, spec):
        ids = _expand(conn, ids, spec)
        rows: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in conn.execute(
                    f"SELECT id, root_id, rel_path, folder, filename, size, mtime, sha256, taken_ts FROM photos "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                rows[int(r["id"])] = r
        n_bytes = 0
        count = 0
        for pid in ids:
            r = rows.get(pid)
            if r is None or r["root_id"] not in roots:
                continue
            root, root_name = roots[r["root_id"]]
            if spec.layout == "date":
                sub = ts_to_naive(r["taken_ts"]).strftime("%Y/%m") if r["taken_ts"] else "Undated"
            elif spec.layout == "original":
                sub = "/".join(x for x in (root_name, r["folder"]) if x)
            else:
                sub = ""
            rel_dir = "/".join(x for x in (label, sub) if x)
            result.items.append(Item(pid, root / r["rel_path"], rel_dir, r["filename"], int(r["size"] or 0),
                                     r["sha256"], float(r["mtime"] or 0)))
            n_bytes += int(r["size"] or 0)
            count += 1
        result.groups.append({"label": label, "count": count, "bytes": n_bytes})
    return result


# ------------------------------------------------------------------ copying to a folder

def export_to_folder(conn: sqlite3.Connection, spec: ExportSpec, progress: Callable[[int, int], None] | None = None,
                     should_stop: Callable[[], bool] | None = None) -> dict:
    if not spec.folder:
        raise ExportError("choose a folder to export to")
    out = _check_destination(conn, Path(spec.folder))
    t0 = time.time()
    p = plan(conn, spec)
    if not p.items:
        raise ExportError("nothing matches — no photos to export")
    out.mkdir(parents=True, exist_ok=True)
    xmps = sidecars_for(conn, [i.photo_id for i in p.items]) if spec.xmp else {}
    copied = already = failed = 0
    written_bytes = 0
    errors: list[dict] = []
    manifest: list[dict] = []
    for n, it in enumerate(p.items):
        if should_stop and should_stop():
            break
        dest_dir = out / it.rel_dir if it.rel_dir else out
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest, same = _destination(dest_dir, it)
            if same:
                already += 1
            else:
                tmp = dest.with_name(dest.name + ".part")
                shutil.copy2(it.src, tmp)           # bytes and modification time, from a read-only open
                os.replace(tmp, dest)
                copied += 1
                written_bytes += it.size
            if it.photo_id in xmps:
                (dest.with_name(dest.name + ".xmp")).write_text(xmps[it.photo_id], encoding="utf-8")
            manifest.append({"photo_id": it.photo_id, "from": str(it.src), "to": dest.relative_to(out).as_posix()})
        except OSError as exc:
            failed += 1
            errors.append({"photo_id": it.photo_id, "file": str(it.src), "error": str(exc.strerror or exc)})
        if progress and (n % 25 == 0 or n == len(p.items) - 1):
            progress(n + 1, len(p.items))
    summary = {"folder": str(out), "copied": copied, "already_there": already, "failed": failed,
               "bytes": written_bytes, "items": len(p.items), "groups": p.groups, "errors": errors[:50],
               "seconds": round(time.time() - t0, 1), "cancelled": bool(should_stop and should_stop())}
    (out / "memoria-copies.json").write_text(json.dumps({
        "exported_at": time.time(), "spec": {k: v for k, v in asdict(spec).items() if k != "folder"},
        **{k: summary[k] for k in ("copied", "already_there", "failed")}, "files": manifest}, indent=1),
        encoding="utf-8")
    return summary


def _destination(dest_dir: Path, it: Item) -> tuple[Path, bool]:
    """A free path for this file — or the existing one if it already holds these bytes (a re-run)."""
    stem, ext = os.path.splitext(it.filename)
    for n in range(1, 10_000):
        cand = dest_dir / (it.filename if n == 1 else f"{stem} ({n}){ext}")
        if not cand.exists():
            return cand, False
        if _same_file(cand, it):
            return cand, True
    raise ExportError(f"too many files named {it.filename} in {dest_dir}")


def _same_file(path: Path, it: Item) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size != it.size:
        return False
    if it.sha256:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
        return h.hexdigest() == it.sha256
    return abs(st.st_mtime - it.mtime) < 2


# ------------------------------------------------------------------ streaming a .zip

class _Sink:
    """A write-only, unseekable stream: zipfile then writes data descriptors, so the
    archive can be sent while it is being made, with no temporary file."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def iter_zip(conn: sqlite3.Connection, spec: ExportSpec) -> Iterator[bytes]:
    p = plan(conn, spec)
    if not p.items:
        raise ExportError("nothing matches — no photos to export")
    xmps = sidecars_for(conn, [i.photo_id for i in p.items]) if spec.xmp else {}
    return _zip_stream(p, xmps)


def _zip_stream(p: Plan, xmps: dict[int, str]) -> Iterator[bytes]:
    sink = _Sink()
    used: set[str] = set()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for it in p.items:
            name = _free_arcname(used, it)
            try:
                info = zipfile.ZipInfo.from_file(it.src, name, strict_timestamps=False)
            except OSError:
                log.warning("Skipping unreadable %s", it.src)
                continue
            info.compress_type = zipfile.ZIP_STORED      # photos and videos are already compressed
            with open(it.src, "rb") as src, zf.open(info, "w") as w:
                for block in iter(lambda: src.read(CHUNK), b""):
                    w.write(block)
                    if len(sink.buf) >= CHUNK:
                        yield sink.take()
            if it.photo_id in xmps:
                zf.writestr(name + ".xmp", xmps[it.photo_id])
            yield sink.take()
    yield sink.take()


def _free_arcname(used: set[str], it: Item) -> str:
    stem, ext = os.path.splitext(it.filename)
    for n in range(1, 10_000):
        fn = it.filename if n == 1 else f"{stem} ({n}){ext}"
        name = f"{it.rel_dir}/{fn}" if it.rel_dir else fn
        if name.lower() not in used:
            used.add(name.lower())
            return name
    raise ExportError(f"too many files named {it.filename}")


def _safe(name: str) -> str:
    """A folder name any filesystem accepts."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip().rstrip(".")
    return out or "Unnamed"


# ------------------------------------------------------------------ as a tracked job

def run_job(ctx, job_id: int | None, spec: ExportSpec) -> dict:
    from ..pipeline.jobs import JobReporter

    reporter = JobReporter(ctx, job_id)
    reporter.start()
    conn = ctx.connect()

    def should_stop() -> bool:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else None
        return bool(row and row[0])

    try:
        out = export_to_folder(conn, spec, should_stop=should_stop,
                               progress=lambda d, t: reporter.progress("export", d, t, f"Copied {d:,} of {t:,}"))
        msg = (f"Copied {out['copied']:,} to {out['folder']}"
               + (f" ({out['already_there']:,} already there)" if out["already_there"] else "")
               + (f", {out['failed']:,} failed" if out["failed"] else ""))
        reporter.finish("cancelled" if out["cancelled"] else "done", msg)
        return out
    except Exception as exc:
        log.exception("Export failed")
        reporter.finish("failed", "Export failed", str(exc))
        raise
    finally:
        conn.close()
