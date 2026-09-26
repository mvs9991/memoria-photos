"""Shared per-process state for the API."""
from __future__ import annotations

import sqlite3
import threading

from ..context import AppContext
from ..db import ThreadLocalDB, get_meta
from ..search.engine import SearchEngine
from ..vectors import IndexCache


class ApiState:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.db = ThreadLocalDB(ctx.paths.db)
        self.index_cache = IndexCache()
        self.search = SearchEngine(ctx, self.index_cache)
        self.lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        return self.db.get()

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
