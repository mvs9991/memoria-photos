"""Export an album as a self-contained static web gallery.

One folder: index.html (inline CSS/JS, no external requests), thumbs/ and photos/ with
resized copies, and videos/ with copies of browser-playable videos. It opens offline and
can be put on any web host. Output inside a photo root is refused — Memoria never writes
next to originals — and the originals themselves are only read.
"""
from __future__ import annotations

import html
import json
import shutil
import sqlite3
import time
from pathlib import Path

from .. import db, imaging
from ..metadata import ts_to_naive
from ..video import playable_in_browser
from ..rotation import rotate_image
from .places import place_label

THUMB = 480
PHOTO = 2048


class ExportError(ValueError):
    pass


def export_album(conn: sqlite3.Connection, photo_ids: list[int], title: str, out_dir: str | Path,
                 description: str | None = None) -> dict:
    out = Path(out_dir).resolve()
    for (root,) in conn.execute("SELECT path FROM roots"):
        r = Path(root).resolve()
        if out == r or r in out.parents:
            raise ExportError(f"{out} is inside the photo folder {r}; choose a folder outside your library")
    if not photo_ids:
        raise ExportError("nothing to export")
    t0 = time.time()
    for sub in ("thumbs", "photos", "videos"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    rows = {r["id"]: r for chunk, q in db.chunks(photo_ids) for r in conn.execute(
        f"""SELECT p.*, rt.path AS root FROM photos p JOIN roots rt ON rt.id = p.root_id
            WHERE p.id IN ({q})""", chunk)}
    items, skipped = [], 0
    for n, pid in enumerate(photo_ids):
        r = rows.get(pid)
        if r is None:
            continue
        src = Path(r["root"]) / r["rel_path"]
        try:
            dec = imaging.decode(src, max_side=PHOTO)
        except (imaging.DecodeError, OSError):
            skipped += 1
            continue
        if r["rotation"]:
            dec.image = rotate_image(dec.image, r["rotation"])
        stem = f"{n:05d}"
        imaging.save_thumbnail(dec.image, out / "thumbs" / f"{stem}.jpg", THUMB, quality=80)
        imaging.save_thumbnail(dec.image, out / "photos" / f"{stem}.jpg", PHOTO, quality=86)
        video = None
        if r["media_type"] == "video" and playable_in_browser(r["video_codec"], r["ext"]):
            video = f"videos/{stem}{r['ext']}"
            shutil.copy2(src, out / video)
        place = conn.execute("SELECT * FROM places WHERE id = ?", (r["place_id"],)).fetchone() if r["place_id"] else None
        items.append({
            "thumb": f"thumbs/{stem}.jpg", "photo": f"photos/{stem}.jpg", "video": video,
            "w": dec.image.width, "h": dec.image.height,
            "date": ts_to_naive(r["taken_ts"]).strftime("%d %B %Y") if r["taken_ts"] else "",
            "place": place_label(place) if place is not None else "",
            "text": r["description"] or "",
        })
    (out / "index.html").write_text(_page(title, description, items), encoding="utf-8")
    return {"exported": len(items), "skipped": skipped, "folder": str(out), "seconds": round(time.time() - t0, 1)}


def _page(title: str, description: str | None, items: list[dict]) -> str:
    t = html.escape(title)
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    desc = f"<p class=d>{html.escape(description)}</p>" if description else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t}</title>
<style>
:root{{color-scheme:light dark;--bg:#faf9f7;--fg:#16161a;--dim:#6b6a70}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0b0d;--fg:#f4f3f1;--dim:#9a99a0}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
header{{padding:28px 20px 12px;max-width:1400px;margin:auto}}h1{{margin:0;font:600 30px Georgia,serif}}
.d,.n{{color:var(--dim);margin:6px 0 0}}
main{{display:flex;flex-wrap:wrap;gap:6px;padding:12px 20px 40px;max-width:1400px;margin:auto}}
main a{{flex-grow:1;height:220px;position:relative;display:block;border-radius:4px;overflow:hidden}}
main img{{height:100%;width:100%;object-fit:cover;display:block}}
main a.v::after{{content:"▶";position:absolute;left:8px;bottom:6px;color:#fff;text-shadow:0 1px 3px #000}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.94);display:none;align-items:center;justify-content:center;z-index:9}}
#lb.on{{display:flex}}#lb img,#lb video{{max-width:94vw;max-height:84vh}}
#cap{{position:fixed;bottom:14px;left:0;right:0;text-align:center;color:#ddd;font-size:13px}}
#lb button{{position:fixed;background:none;border:0;color:#fff;font-size:34px;cursor:pointer;padding:18px}}
#x{{top:4px;right:8px}}#p{{left:4px;top:45%}}#nx{{right:4px;top:45%}}
@media (max-width:600px){{main a{{height:130px}}}}
</style></head><body>
<header><h1>{t}</h1>{desc}<p class=n>{len(items)} items</p></header>
<main id=g></main>
<div id=lb><button id=x aria-label=Close>×</button><button id=p aria-label=Previous>‹</button>
<div id=m></div><button id=nx aria-label=Next>›</button><div id=cap></div></div>
<script>
const I={data};let c=0;const g=document.getElementById('g'),lb=document.getElementById('lb'),m=document.getElementById('m'),cap=document.getElementById('cap');
I.forEach((it,i)=>{{const a=document.createElement('a');a.href=it.photo;a.style.flexBasis=(220*it.w/it.h)+'px';if(it.video)a.className='v';
const im=document.createElement('img');im.src=it.thumb;im.loading='lazy';im.alt=it.text||it.date;a.appendChild(im);
a.onclick=e=>{{e.preventDefault();show(i)}};g.appendChild(a)}});
function show(i){{c=(i+I.length)%I.length;const it=I[c];m.innerHTML='';const el=document.createElement(it.video?'video':'img');
el.src=it.video||it.photo;if(it.video){{el.controls=true;el.autoplay=true;el.poster=it.photo}}m.appendChild(el);
cap.textContent=[it.date,it.place,it.text].filter(Boolean).join(' · ');lb.classList.add('on')}}
function hide(){{lb.classList.remove('on');m.innerHTML=''}}
document.getElementById('x').onclick=hide;document.getElementById('p').onclick=()=>show(c-1);document.getElementById('nx').onclick=()=>show(c+1);
document.addEventListener('keydown',e=>{{if(!lb.classList.contains('on'))return;if(e.key==='Escape')hide();if(e.key==='ArrowLeft')show(c-1);if(e.key==='ArrowRight')show(c+1)}});
</script>
<p class=n style="text-align:center;padding-bottom:24px">Made with Memoria</p>
</body></html>
"""
