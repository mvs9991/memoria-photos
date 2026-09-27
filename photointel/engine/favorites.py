"""Favourites: the library's own, or — with accounts — each person's.

Without accounts there is one set, photos.favorite. With accounts every person has their
own (user_favorites); turning accounts on hands the existing set to the owner, and a
favourite that arrives with an import (Google Takeout, iCloud) goes to every owner.
Queries ask `expr(alias, user_id)` for "is this a favourite of whoever is looking".
"""
from __future__ import annotations

import sqlite3


def expr(alias: str, user_id: int | None) -> str:
    """SQL that is 1 when the photo is a favourite of `user_id` (the library's when None)."""
    if user_id is None:
        return f"{alias}.favorite"
    return (f"EXISTS (SELECT 1 FROM user_favorites uf WHERE uf.photo_id = {alias}.id "
            f"AND uf.user_id = {int(user_id)})")


def set_favorite(conn: sqlite3.Connection, photo_ids: list[int], on: bool, user_id: int | None) -> None:
    if user_id is None:
        conn.executemany("UPDATE photos SET favorite = ? WHERE id = ?", [(int(on), int(p)) for p in photo_ids])
    elif on:
        conn.executemany("INSERT OR IGNORE INTO user_favorites(user_id, photo_id) VALUES (?, ?)",
                         [(int(user_id), int(p)) for p in photo_ids])
    else:
        conn.executemany("DELETE FROM user_favorites WHERE user_id = ? AND photo_id = ?",
                         [(int(user_id), int(p)) for p in photo_ids])


def is_favorite(conn: sqlite3.Connection, photo_id: int, user_id: int | None) -> bool:
    return bool(conn.execute(f"SELECT {expr('p', user_id)} FROM photos p WHERE p.id = ?", (photo_id,)).fetchone()[0])


def count(conn: sqlite3.Connection, user_id: int | None) -> int:
    if user_id is None:
        return conn.execute("SELECT COUNT(*) FROM photos WHERE favorite = 1 AND status = 'ok'").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM user_favorites uf JOIN photos p ON p.id = uf.photo_id "
                        "WHERE uf.user_id = ? AND p.status = 'ok'", (int(user_id),)).fetchone()[0]


def hand_to(conn: sqlite3.Connection, user_id: int) -> None:
    """Accounts turned on: the library's favourites become this (owner) account's."""
    conn.execute("INSERT OR IGNORE INTO user_favorites(user_id, photo_id) SELECT ?, id FROM photos WHERE favorite = 1",
                 (int(user_id),))


def imported(conn: sqlite3.Connection, photo_id: int) -> None:
    """An import said "favourite": the library's flag, and every owner's when there are accounts."""
    conn.execute("UPDATE photos SET favorite = 1 WHERE id = ?", (photo_id,))
    conn.execute("INSERT OR IGNORE INTO user_favorites(user_id, photo_id) "
                 "SELECT id, ? FROM users WHERE role = 'owner'", (photo_id,))
