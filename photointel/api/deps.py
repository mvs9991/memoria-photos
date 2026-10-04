"""Shared per-process state for the API."""
from __future__ import annotations

import sqlite3
import logging
import threading

from ..context import AppContext
from ..db import ThreadLocalDB, get_meta
from ..search.engine import SearchEngine
from ..vectors import IndexCache


log = logging.getLogger(__name__)


class ApiState:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.db = ThreadLocalDB(ctx.paths.db)
        self.index_cache = IndexCache()
        self.search = SearchEngine(ctx, self.index_cache)
        self.lock = threading.Lock()
        self._seen = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = self.db.get()
        # The first use of this thread's connection in a new request: a transaction still open here was
        # left by an earlier request that failed between a write and its commit. Left alone it pins a
        # stale snapshot, and SQLite then refuses every write on this connection at once ("database is
        # locked"), so one lost race broke saving for good on whichever worker thread it happened on.
        mark = _request_mark.get()
        if mark is not None and getattr(self._seen, "mark", None) is not mark:
            left_by = getattr(self._seen, "path", "?")
            self._seen.mark, self._seen.path = mark, mark[1]
            if c.in_transaction:
                log.warning("Rolled back a transaction left open by an earlier request (%s)", left_by)
                c.rollback()
        return c

    def generation(self, name: str) -> int:
        return int(get_meta(self.conn(), f"gen:{name}", 0) or 0)


_state: ApiState | None = None


def set_state(state: ApiState) -> None:
    global _state
    _state = state


def get_state() -> ApiState:
    assert _state is not None, "API state not initialised"
    return _state


def get_conn() -> sqlite3.Connection:
    return get_state().conn()


# The signed-in account for the current request (set by the login middleware), or None
# when the library has no accounts. Used to attribute uploads and to check roles.
from contextvars import ContextVar  # noqa: E402

_request_mark: ContextVar[object | None] = ContextVar("memoria_request", default=None)


def start_request(path: str = "") -> None:
    """Called once per HTTP request (app middleware), so ApiState.conn can tell requests apart."""
    _request_mark.set((object(), path))


_current_user: ContextVar[dict | None] = ContextVar("memoria_user", default=None)


def current_user() -> str | None:
    u = _current_user.get()
    return u["username"] if u else None


def current_role() -> str:
    """'owner' when nobody is signed in: a library without accounts belongs to whoever runs it."""
    u = _current_user.get()
    return u["role"] if u else "owner"


def set_current_user(user: dict | None):
    return _current_user.set(user)


_locked_open: ContextVar[bool] = ContextVar("memoria_locked_open", default=False)


def locked_open() -> bool:
    """Whether this request comes from a browser that has opened the Locked folder."""
    return _locked_open.get()


def set_locked_open(value: bool):
    return _locked_open.set(value)


# Per-request notes for the middleware. A dict, not a flag: sync routes run on a copy of the
# context, so only a change made *inside* this shared object reaches the response.
_response_notes: ContextVar[dict | None] = ContextVar("memoria_response_notes", default=None)


def start_response_notes() -> dict:
    notes: dict = {}
    _response_notes.set(notes)
    return notes


def guard_locked(row) -> None:
    """A locked photo is invisible (404, not 403: its existence is not confirmed) unless the
    Locked folder is open in this browser and the account may use it."""
    from fastapi import HTTPException

    keys = row.keys()
    locked = ("locked" in keys and row["locked"]) or ("status" in keys and row["status"] == "locked")
    if locked and not (locked_open() and current_role() == "owner"):
        raise HTTPException(404, "photo not found")
    # A private photo exists only for its person (engine/private.py) — not even for the owner.
    if ("private_to" in keys and row["private_to"] is not None and row["private_to"] != current_user_id()) \
            or ("status" in keys and row["status"] == "private" and "private_to" not in keys):
        raise HTTPException(404, "photo not found")
    if locked or ("private_to" in keys and row["private_to"] is not None):
        # Shown, but never kept: not by the browser, not by the phone's offline copy.
        notes = _response_notes.get()
        if notes is not None:
            notes["no_store"] = True


def current_user_id() -> int | None:
    """The signed-in account's id, or None for a library without accounts."""
    u = _current_user.get()
    return int(u["id"]) if u else None
