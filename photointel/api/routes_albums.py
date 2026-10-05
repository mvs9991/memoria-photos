"""Albums: user-made and imported from Google Takeout."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..engine import albums as albums_mod
from ..engine import favorites
from .cache import until_db_changes
from .deps import current_user_id, get_state
from .routes_library import columnar

router = APIRouter()

def _grid_cols() -> str:
    """The grid's columns, with "favourite" meaning the viewer's own (with accounts)."""
    return ("id, width, height, rotation, taken_ts, face_count, "
            f"{favorites.expr('photos', current_user_id())} AS favorite, media_type, duration, "
            "live_video_id, motion_offset, rating")


SMART_LIMIT = 5000


def _resolver(conn):
    state = get_state()
    return lambda query: state.search.search(conn, query, limit=SMART_LIMIT).photo_ids


@router.get("/albums")
@until_db_changes()
def list_albums():
    conn = get_state().conn()
    return {"albums": albums_mod.list_albums(conn, resolve=_resolver(conn), user_id=current_user_id())}


class AlbumCreate(BaseModel):
    name: str
    description: str | None = None
    photo_ids: list[int] = []
    query: str | None = None       # set = a smart album (saved search)


@router.post("/albums")
def create_album(body: AlbumCreate):
    if not body.name.strip():
        raise HTTPException(400, "an album needs a name")
    conn = get_state().conn()
    uid = current_user_id()
    if body.query and body.query.strip():
        return {"id": albums_mod.create_smart_album(conn, body.name, body.query, owner_user_id=uid)}
    aid = albums_mod.create_album(conn, body.name, body.photo_ids, body.description, owner_user_id=uid)
    return {"id": aid}


@router.get("/albums/{album_id}")
@until_db_changes()
def album_detail(album_id: int):
    conn = get_state().conn()
    a = albums_mod.can_see(conn, album_id, current_user_id())
    if a is None:
        raise HTTPException(404, "album not found")
    if a["kind"] == "smart":
        ids = _resolver(conn)(a["query"])
    else:
        ids = albums_mod.album_photo_ids(conn, album_id)
    rows = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows += conn.execute(f"SELECT {_grid_cols()} FROM photos WHERE id IN ({','.join('?' * len(chunk))})",
                             chunk).fetchall()
    order = {pid: n for n, pid in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    # The same cover the album list shows: the chosen one only while it is still one of the album's visible
    # photos (it could be hidden, trashed or locked since), else the best of them.
    cover = a["cover_photo_id"] if a["cover_photo_id"] in order else (albums_mod._best_photo(conn, ids) if ids else None)
    return {"id": a["id"], "name": a["name"], "description": a["description"], "source": a["source"],
            "kind": a["kind"], "query": a["query"], "photo_count": len(ids),
            "cover_photo_id": cover, "photos": columnar(rows), "private": bool(a["private"]),
            "mine": current_user_id() is not None and a["owner_user_id"] == current_user_id()}


class AlbumUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    cover_photo_id: int | None = None
    query: str | None = None
    private: bool | None = None       # only the account that made the album may change this


@router.post("/albums/{album_id}")
def update_album(album_id: int, body: AlbumUpdate):
    conn = get_state().conn()
    uid = current_user_id()
    a = albums_mod.can_see(conn, album_id, uid)
    if a is None:
        raise HTTPException(404, "album not found")
    if body.private is not None:
        if uid is None or a["owner_user_id"] != uid:
            raise HTTPException(403, "only the person who made an album can make it private")
        conn.execute("UPDATE albums SET private = ? WHERE id = ?", (int(body.private), album_id))
        conn.commit()
    albums_mod.rename_album(conn, album_id, body.name, body.description)
    if body.query is not None and body.query.strip():
        conn.execute("UPDATE albums SET query = ? WHERE id = ? AND kind = 'smart'", (body.query.strip(), album_id))
        conn.commit()
    if body.cover_photo_id is not None:
        conn.execute("UPDATE albums SET cover_photo_id = ? WHERE id = ?", (body.cover_photo_id, album_id))
        conn.commit()
    return {"ok": True}


@router.delete("/albums/{album_id}")
def delete_album(album_id: int):
    conn = get_state().conn()
    if albums_mod.can_see(conn, album_id, current_user_id()) is None:
        raise HTTPException(404, "album not found")
    albums_mod.delete_album(conn, album_id)
    return {"ok": True, "note": "The album was removed; its photos were not touched."}


class AlbumPhotos(BaseModel):
    photo_ids: list[int]


@router.post("/albums/{album_id}/photos")
def add_photos(album_id: int, body: AlbumPhotos):
    conn = get_state().conn()
    a = albums_mod.can_see(conn, album_id, current_user_id())
    if a is None:
        raise HTTPException(404, "album not found")
    if a["kind"] == "smart":
        raise HTTPException(400, "a smart album follows its search; change the search instead")
    return {"added": albums_mod.add_photos(conn, album_id, body.photo_ids)}


@router.post("/albums/{album_id}/photos/remove")
def remove_photos(album_id: int, body: AlbumPhotos):
    if not body.photo_ids:
        return {"removed": 0}
    conn = get_state().conn()
    if albums_mod.can_see(conn, album_id, current_user_id()) is None:
        raise HTTPException(404, "album not found")
    return {"removed": albums_mod.remove_photos(conn, album_id, body.photo_ids)}
