"""SQLite access layer: connections, schema/migrations and small shared helpers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 5
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Columns added to `photos` after v1: (name, SQL type/default).
V2_PHOTO_COLUMNS = [
    ("media_type", "TEXT NOT NULL DEFAULT 'image'"),   # image | video
    ("duration", "REAL"),                              # seconds (videos)
    ("video_codec", "TEXT"),
    ("live_video_id", "INTEGER"),       # on a still: the separate file holding its motion (iPhone Live)
    ("live_component", "INTEGER NOT NULL DEFAULT 0"),  # on a video: 1 = it is the motion half of a Live photo
    ("motion_offset", "INTEGER"),       # on a still: byte offset of an embedded MP4 (Google/Samsung motion photo)
    ("description", "TEXT"),            # written by a person (e.g. in Google Photos), never generated
    ("ocr_text", "TEXT"),
    ("ocr_model", "INTEGER"),
]
# v3: ratings, stacks, birthdays, smart albums.
V3_COLUMNS = {
    "photos": [
        ("rating", "INTEGER NOT NULL DEFAULT 0"),       # 0 = unrated, 1..5 stars (the user's)
        ("stack_id", "INTEGER"),                        # photo id of the stack's cover (RAW+JPEG, burst)
        ("stack_hidden", "INTEGER NOT NULL DEFAULT 0"),  # 1 = folded under its stack's cover in the timeline
    ],
    "persons": [
        ("birth_date", "TEXT"),                         # 'YYYY-MM-DD', or '--MM-DD' when the year is unknown
    ],
    "albums": [
        ("kind", "TEXT NOT NULL DEFAULT 'manual'"),     # manual | smart
        ("query", "TEXT"),                              # smart: the saved search
    ],
}
MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_photos_media ON photos(media_type)",
    "CREATE INDEX IF NOT EXISTS ix_photos_live ON photos(live_component)",
    "CREATE INDEX IF NOT EXISTS ix_photos_stack ON photos(stack_id)",
]


def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add every post-v1 column that is missing. Idempotent; runs for new and old databases."""
    _add_columns(conn, "photos", V2_PHOTO_COLUMNS)
    for table, cols in V3_COLUMNS.items():
        _add_columns(conn, table, cols)
    for sql in MIGRATION_INDEXES:
        conn.execute(sql)


def connect(db_path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    db_path = str(db_path)
    if readonly:
        uri = "file:" + Path(db_path).as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -65536")   # ~64 MB page cache
    conn.execute("PRAGMA mmap_size = 268435456")
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
    current = get_meta(conn, "schema_version")
    migrate(conn, int(current) if current is not None else 1)
    conn.commit()
    return conn


def migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Forward-only migrations. Each step must be idempotent."""
    if from_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{from_version} is newer than this software (v{SCHEMA_VERSION})."
        )
    # v2: video, live/motion photos, descriptions, OCR. v3: ratings, stacks, birthdays,
    # smart albums. v4: share links and GPX tracks. v5: trash (tables only). New tables are in schema.sql; columns are added here, unconditionally:
    # it is idempotent, and a fresh database needs them too.
    _ensure_columns(conn)
    set_meta(conn, "schema_version", SCHEMA_VERSION)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def bump_generation(conn: sqlite3.Connection, name: str) -> int:
    """Monotonic counters used to invalidate in-memory caches across processes."""
    key = f"gen:{name}"
    val = int(get_meta(conn, key, 0)) + 1
    set_meta(conn, key, val)
    return val


def audit(conn: sqlite3.Connection, action: str, entity_type: str, entity_id: int | None,
          details: dict | None = None, actor: str = "user") -> None:
    conn.execute(
        "INSERT INTO audit_log(created_at, action, entity_type, entity_id, details, actor) VALUES (?,?,?,?,?,?)",
        (time.time(), action, entity_type, entity_id, json.dumps(details or {}), actor),
    )


def register_model(conn: sqlite3.Connection, kind: str, name: str, version: str,
                   dim: int | None, params: dict | None = None) -> int:
    row = conn.execute(
        "SELECT id FROM models WHERE kind = ? AND name = ? AND version = ?", (kind, name, version)
    ).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO models(kind, name, version, dim, params, created_at) VALUES (?,?,?,?,?,?)",
        (kind, name, version, dim, json.dumps(params or {}), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def active_model_id(conn: sqlite3.Connection, kind: str) -> int | None:
    val = get_meta(conn, f"active_model:{kind}")
    return int(val) if val is not None else None


def set_active_model(conn: sqlite3.Connection, kind: str, model_id: int) -> None:
    set_meta(conn, f"active_model:{kind}", model_id)


def chunked(seq: Iterable[Any], size: int) -> Iterator[list[Any]]:
    buf: list[Any] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


class ThreadLocalDB:
    """One connection per thread (the API server uses a thread pool)."""

    def __init__(self, db_path: Path | str):
        self.db_path = db_path
        self._local = threading.local()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.db_path)
            self._local.conn = conn
        return conn
