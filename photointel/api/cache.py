"""Answers kept until the database changes.

Pages like Places, Timeline, Insights and the sidebar's numbers are computed from the whole library (100-300 ms
each on a 31k-photo library on a hard drive), are asked for again and again, and change only when the database
does. Each answer is kept under a token that moves whenever anything commits: `PRAGMA data_version` read on a
connection of its own that never writes, which changes when *any* other connection commits (this process's
request threads, and the index jobs, which are separate processes). The token is read before the answer is
computed, so a commit that lands meanwhile only makes the next request recompute; an answer is never kept past a
change.

The key also holds everything else an answer depends on: the arguments, the account, its role, whether the
Locked folder is open (and, where a route asks for it, the day).
"""
from __future__ import annotations

import functools
import sqlite3
import threading
import time
from collections import OrderedDict

from fastapi.responses import JSONResponse, Response

MAX_ENTRIES = 512

_lock = threading.Lock()
_watch: dict[str, sqlite3.Connection] = {}
_entries: OrderedDict = OrderedDict()


def db_token(db_path) -> int:
    """A number that changes whenever anything commits to the database (any connection, any process)."""
    key = str(db_path)
    with _lock:
        conn = _watch.get(key)
        if conn is None:
            conn = sqlite3.connect(key, check_same_thread=False, timeout=30)
            _watch[key] = conn
        return int(conn.execute("PRAGMA data_version").fetchone()[0])


def clear() -> None:
    with _lock:
        _entries.clear()
        _values.clear()


def until_db_changes(by_day: bool = False):
    """Keep a GET route's answer until the database changes. `by_day`: also until the date changes."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from .deps import current_role, current_user_id, get_state, locked_open

            state = get_state()
            token = db_token(state.ctx.paths.db)
            key = (fn.__module__, fn.__qualname__, str(state.ctx.paths.db), args,
                   tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in sorted(kwargs.items())),
                   current_user_id(), current_role(), locked_open(), time.strftime("%Y-%m-%d") if by_day else None,
                   repr(state.ctx.settings))      # e.g. who "me" is lives in settings.json, not the database
            with _lock:
                hit = _entries.get(key)
                if hit is not None and hit[0] == token:
                    _entries.move_to_end(key)
                    return _replay(hit[1])
            out = fn(*args, **kwargs)
            from .deps import _response_notes

            notes = _response_notes.get()
            if notes and notes.get("no_store"):
                return out        # it showed a locked or private photo: never kept, here or anywhere
            kept = _keep(out)
            if kept is not None:
                with _lock:
                    _entries[key] = (token, kept)
                    _entries.move_to_end(key)
                    while len(_entries) > MAX_ENTRIES:
                        _entries.popitem(last=False)
            return out
        return wrapper
    return deco


def _keep(out):
    """What to store: the encoded body of a JSON response, or a plain dict/list (never a Response object,
    which carries per-request state)."""
    if isinstance(out, JSONResponse):
        return (bytes(out.body), out.status_code)
    if isinstance(out, (dict, list)):
        # Encoded once, as FastAPI would have: a shared dict handed to every later request could be changed by one.
        from fastapi.encoders import jsonable_encoder

        return (bytes(JSONResponse(jsonable_encoder(out)).body), 200)
    return None


def _replay(kept):
    return Response(content=kept[0], status_code=kept[1], media_type="application/json")


def close_all() -> None:
    """Forget every answer and close the watching connections (tests, and a library switched in place)."""
    with _lock:
        _entries.clear()
        _values.clear()
        for conn in _watch.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _watch.clear()


_values: dict = {}


def value_until_db_changes(name: str, compute):
    """`compute()`'s result, kept until the database changes (for lookups many requests share, such as the photo
    count of every tag and place behind the search box's suggestions)."""
    from .deps import get_state

    db_path = str(get_state().ctx.paths.db)
    token = db_token(db_path)
    key = (name, db_path)
    with _lock:
        hit = _values.get(key)
        if hit is not None and hit[0] == token:
            return hit[1]
    value = compute()
    with _lock:
        _values[key] = (token, value)
    return value
