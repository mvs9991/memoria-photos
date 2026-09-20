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
