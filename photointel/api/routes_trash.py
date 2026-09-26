"""Trash endpoints. Every destructive call carries `confirm`, the number of photos the
person agreed to in the dialog; a mismatch is refused, so a client bug or a stale
selection can never delete more than was shown."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..engine import trash
from .deps import get_state

router = APIRouter()


class TrashBody(BaseModel):
    photo_ids: list[int]
    confirm: int


class RestoreBody(BaseModel):
    photo_ids: list[int]


class EmptyBody(BaseModel):
    confirm: int


def _check(photo_ids: list[int], confirm: int) -> list[int]:
    ids = list(dict.fromkeys(photo_ids))
    if not ids:
        raise HTTPException(400, "nothing selected")
    if confirm != len(ids):
        raise HTTPException(400, f"confirmation is for {confirm} photos but {len(ids)} were sent; nothing was deleted")
    return ids


@router.post("/trash")
def move_to_trash(body: TrashBody):
    state = get_state()
    ids = _check(body.photo_ids, body.confirm)
    try:
        return trash.move_to_trash(state.ctx, state.conn(), ids)
    except trash.TrashError as exc:
        raise HTTPException(400, str(exc))


@router.get("/trash")
def list_trash():
    state = get_state()
    items = trash.list_trash(state.conn())
    return {"items": items, "bytes": sum(i["size"] for i in items), "days": state.ctx.settings.trash_days,
            "allow_delete": state.ctx.settings.allow_delete}


@router.post("/trash/restore")
def restore(body: RestoreBody):
    state = get_state()
    return trash.restore(state.ctx, state.conn(), body.photo_ids)


@router.post("/trash/erase")
def erase(body: TrashBody):
    """Delete permanently, now, the chosen photos that are already in the trash."""
    state = get_state()
    ids = _check(body.photo_ids, body.confirm)
    return trash.purge(state.ctx, state.conn(), photo_ids=ids)


@router.post("/trash/empty")
def empty(body: EmptyBody):
    state = get_state()
    conn = state.conn()
    n = len(trash.list_trash(conn))
    if body.confirm != n:
        raise HTTPException(400, f"the trash holds {n} items, not {body.confirm}; nothing was erased")
    return trash.purge(state.ctx, conn)
