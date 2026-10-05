"""Duplicate review."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import db
from .cache import until_db_changes
from .deps import get_state

router = APIRouter()

RELATION_LABEL = {
    "original": "Original", "exact": "Exact copy", "resized": "Resized copy", "compressed": "Recompressed",
    "edited": "Edited copy", "cropped": "Cropped copy", "screenshot": "Screenshot of photo",
    "duplicate": "Duplicate", "similar": "Similar shot",
}


@router.get("/duplicates")
@until_db_changes()
def list_groups(kind: str | None = Query(None, pattern="^(exact|near|likely|similar)$"),
                status: str = Query("pending", pattern="^(pending|reviewed|all)$"),
                limit: int = Query(200, le=1000), offset: int = 0):
    conn = get_state().conn()
    where, args = ["1=1"], []
    if kind:
        where.append("g.kind = ?")
        args.append(kind)
    if status != "all":
        where.append("g.review_status = ?")
        args.append(status)
    rows = conn.execute(
        f"""SELECT g.* FROM dup_groups g WHERE {' AND '.join(where)}
            ORDER BY CASE g.kind WHEN 'exact' THEN 0 WHEN 'near' THEN 1 WHEN 'likely' THEN 2 ELSE 3 END,
                     g.member_count DESC, g.id LIMIT ? OFFSET ?""", (*args, limit, offset)).fetchall()
    groups = []
    for g in rows:
        members = []
        for m in conn.execute(
                """SELECT m.photo_id, m.relation, m.similarity, m.hamming, p.width, p.height, p.size,
                          p.taken_ts, p.filename, p.folder, p.source_kind, p.quality_score
                   FROM dup_members m JOIN photos p ON p.id = m.photo_id WHERE m.group_id = ?
                     AND p.status NOT IN ('locked', 'private', 'trashed', 'deleted')
                   ORDER BY (m.photo_id = ?) DESC, p.size DESC""", (g["id"], g["keep_photo_id"])):
            members.append({
                "photo_id": m["photo_id"], "relation": m["relation"],
                "relation_label": RELATION_LABEL.get(m["relation"], m["relation"]),
                "similarity": round(m["similarity"] or 0, 3), "hamming": m["hamming"],
                "width": m["width"], "height": m["height"], "size": m["size"], "taken_ts": m["taken_ts"],
                "filename": m["filename"], "folder": m["folder"], "source_kind": m["source_kind"],
                "quality_score": m["quality_score"], "is_keeper": m["photo_id"] == g["keep_photo_id"],
            })
        hidden_members = conn.execute(
            "SELECT COUNT(*) FROM dup_members m JOIN photos p ON p.id = m.photo_id "
            "WHERE m.group_id = ? AND p.status IN ('locked', 'private')", (g["id"],)).fetchone()[0]
        if hidden_members:
            # Some members are locked or private. A group they leave with fewer than two copies is not a
            # duplicate to anyone who cannot open them, and the rest must not count them or name one as the
            # copy to keep.
            if len(members) < 2:
                continue
            if g["keep_photo_id"] not in {m["photo_id"] for m in members}:
                members[0]["is_keeper"] = True
        keep_id = next((m["photo_id"] for m in members if m["is_keeper"]), g["keep_photo_id"])
        reclaimable = sum(m["size"] or 0 for m in members if not m["is_keeper"])
        groups.append({"id": g["id"], "kind": g["kind"],
                       "member_count": len(members) if hidden_members else g["member_count"],
                       "keep_photo_id": keep_id, "review_status": g["review_status"],
                       "reclaimable_bytes": reclaimable, "members": members})
    counts = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM dup_groups GROUP BY kind")}
    # Reclaimable space is a headline figure for the whole library, so it is summed
    # over every matching group, not just the page being returned (which understated
    # it ~19x on a real library). "similar" groups are excluded: those are different
    # photographs that merely look alike, so deleting them reclaims nothing you
    # actually had twice.
    total_bytes = conn.execute(
        f"""SELECT COALESCE(SUM(p.size), 0) FROM dup_members m
             JOIN dup_groups g ON g.id = m.group_id
             JOIN photos p ON p.id = m.photo_id
             WHERE {' AND '.join(where)} AND g.kind != 'similar'
               AND m.photo_id != g.keep_photo_id""", args).fetchone()[0]
    return JSONResponse({"groups": groups, "counts": counts, "reclaimable_bytes": total_bytes,
            "total": conn.execute(f"SELECT COUNT(*) FROM dup_groups g WHERE {' AND '.join(where)}", args).fetchone()[0]})


class ReviewBody(BaseModel):
    status: Literal["reviewed", "not_duplicate", "pending"] | None = None
    keep_photo_id: int | None = None


def _members(conn, group_id: int) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT photo_id FROM dup_members WHERE group_id = ?", (group_id,))}


@router.post("/duplicates/{group_id}/review")
def review(group_id: int, body: ReviewBody):
    conn = get_state().conn()
    g = conn.execute("SELECT * FROM dup_groups WHERE id=?", (group_id,)).fetchone()
    if g is None:
        raise HTTPException(404, "group not found")
    if body.keep_photo_id and body.keep_photo_id not in _members(conn, group_id):
        raise HTTPException(400, "the copy to keep must be one of this group's photos")
    sets, args = [], []
    if body.status:
        sets.append("review_status=?")
        args.append(body.status)
    if body.keep_photo_id:
        sets.append("keep_photo_id=?")
        args.append(body.keep_photo_id)
    if sets:
        args.append(group_id)
        conn.execute(f"UPDATE dup_groups SET {', '.join(sets)}, updated_at=strftime('%s','now') WHERE id=?", args)
        db.audit(conn, "duplicate_reviewed", "dup_group", group_id,
                 {"status": body.status, "keep": body.keep_photo_id})
        conn.commit()
    return {"ok": True}


class HideBody(BaseModel):
    photo_ids: list[int]


@router.post("/duplicates/{group_id}/hide-copies")
def hide_copies(group_id: int, body: HideBody):
    """Hide duplicate copies from the library view. Files are never touched."""
    conn = get_state().conn()
    g = conn.execute("SELECT keep_photo_id FROM dup_groups WHERE id = ?", (group_id,)).fetchone()
    if g is None:
        raise HTTPException(404, "group not found")
    # Only this group's copies, and never the one marked to keep: the page promises that, and the server used
    # to hide whatever ids it was sent (the keeper too, which made the photo vanish from the library).
    ids = [i for i in dict.fromkeys(body.photo_ids) if i in _members(conn, group_id) and i != g["keep_photo_id"]]
    if not ids:
        return {"hidden": 0}
    marks = ",".join("?" * len(ids))
    n = conn.execute(f"UPDATE photos SET hidden=1 WHERE id IN ({marks})", ids).rowcount
    conn.execute("UPDATE dup_groups SET review_status='reviewed' WHERE id=?", (group_id,))
    db.audit(conn, "duplicates_hidden", "dup_group", group_id, {"photos": ids})
    conn.commit()
    from ..engine import visibility

    visibility.refresh(conn, ids)      # a hidden stack cover hands over its stack
    return {"hidden": n, "note": "Photos were hidden from the library; the original files were not modified."}
