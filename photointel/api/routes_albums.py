"""Albums: user-made and imported from Google Takeout."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..engine import albums as albums_mod
from .deps import get_state
from .routes_library import columnar

router = APIRouter()

_GRID_COLS = ("id, width, height, taken_ts, face_count, favorite, media_type, duration, "
              "live_video_id, motion_offset")


@router.get("/albums")
def list_albums():
    return {"albums": albums_mod.list_albums(get_state().conn())}


class AlbumCreate(BaseModel):
    name: str
    description: str | None = None
    photo_ids: list[int] = []


@router.post("/albums")
def create_album(body: AlbumCreate):
    if not body.name.strip():
        raise HTTPException(400, "an album needs a name")
    conn = get_state().conn()
    aid = albums_mod.create_album(conn, body.name, body.photo_ids, body.description)
    return {"id": aid}


@router.get("/albums/{album_id}")
def album_detail(album_id: int):
    conn = get_state().conn()
    a = conn.execute("SELECT * FROM albums WHERE id = ? AND hidden = 0", (album_id,)).fetchone()
    if a is None:
        raise HTTPException(404, "album not found")
    ids = albums_mod.album_photo_ids(conn, album_id)
    rows = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows += conn.execute(f"SELECT {_GRID_COLS} FROM photos WHERE id IN ({','.join('?' * len(chunk))})",
                             chunk).fetchall()
    order = {pid: n for n, pid in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    return {"id": a["id"], "name": a["name"], "description": a["description"], "source": a["source"],
            "photo_count": len(ids), "cover_photo_id": a["cover_photo_id"], "photos": columnar(rows)}


class AlbumUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    cover_photo_id: int | None = None


@router.post("/albums/{album_id}")
def update_album(album_id: int, body: AlbumUpdate):
    conn = get_state().conn()
    if conn.execute("SELECT 1 FROM albums WHERE id = ? AND hidden = 0", (album_id,)).fetchone() is None:
        raise HTTPException(404, "album not found")
    albums_mod.rename_album(conn, album_id, body.name, body.description)
    if body.cover_photo_id is not None:
        conn.execute("UPDATE albums SET cover_photo_id = ? WHERE id = ?", (body.cover_photo_id, album_id))
        conn.commit()
    return {"ok": True}


@router.delete("/albums/{album_id}")
def delete_album(album_id: int):
    albums_mod.delete_album(get_state().conn(), album_id)
    return {"ok": True, "note": "The album was removed; its photos were not touched."}


class AlbumPhotos(BaseModel):
    photo_ids: list[int]


@router.post("/albums/{album_id}/photos")
def add_photos(album_id: int, body: AlbumPhotos):
    conn = get_state().conn()
    if conn.execute("SELECT 1 FROM albums WHERE id = ? AND hidden = 0", (album_id,)).fetchone() is None:
        raise HTTPException(404, "album not found")
    return {"added": albums_mod.add_photos(conn, album_id, body.photo_ids)}


@router.post("/albums/{album_id}/photos/remove")
def remove_photos(album_id: int, body: AlbumPhotos):
    if not body.photo_ids:
        return {"removed": 0}
    return {"removed": albums_mod.remove_photos(get_state().conn(), album_id, body.photo_ids)}
