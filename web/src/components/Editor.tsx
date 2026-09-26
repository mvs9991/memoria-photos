/**
 * Edit a photo or trim a video — the result is always a new file next to your uploads;
 * the original is never changed. The preview is rendered by the server with the same code
 * that saves, so what you see is what you get (the crop is shown as a frame on top).
 */
import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Crop, FlipHorizontal, RotateCcw, RotateCw, Save, Scissors, Wand2, X } from "lucide-react";
import { Portal } from "./Portal";
import { videoUrl } from "../lib/api";
import { clock } from "../lib/format";

type Box = [number, number, number, number];
interface Spec {
  rotate: number; flip: boolean; crop: Box | null; brightness: number; contrast: number;
  saturation: number; warmth: number; filter: string; auto: boolean;
}
const EMPTY: Spec = { rotate: 0, flip: false, crop: null, brightness: 0, contrast: 0, saturation: 0, warmth: 0,
  filter: "none", auto: false };
const FILTERS = ["none", "vivid", "warm", "cool", "fade", "mono", "sepia"];
const ASPECTS: [string, number | null][] = [["free", null], ["1:1", 1], ["4:3", 4 / 3], ["3:2", 3 / 2], ["16:9", 16 / 9]];

async function post(url: string, body: unknown) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
  return res;
}

export function Editor({ photoId, video, duration, onClose }: {
  photoId: number; video: boolean; duration?: number | null; onClose: () => void;
}) {
  return (
    <Portal>
      <div className="editor" role="dialog" aria-label={video ? "Trim video" : "Edit photo"}>
        {video ? <Trim photoId={photoId} duration={duration ?? 0} onClose={onClose} /> : <PhotoEditor photoId={photoId} onClose={onClose} />}
      </div>
    </Portal>
  );
}

function PhotoEditor({ photoId, onClose }: { photoId: number; onClose: () => void }) {
  const [spec, setSpec] = useState<Spec>(EMPTY);
  const [cropping, setCropping] = useState(false);
  const [aspect, setAspect] = useState<number | null>(null);
  const [imgRatio, setImgRatio] = useState(1);          // preview width / height, for square-in-pixels crops
  const [src, setSrc] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const set = (patch: Partial<Spec>) => setSpec((s) => ({ ...s, ...patch }));

  // Server preview of everything except the crop (the crop is drawn as a frame), debounced.
  const look = JSON.stringify({ ...spec, crop: null });
  useEffect(() => {
    let alive = true;
    setBusy(true);
    const t = window.setTimeout(async () => {
      try {
        const res = await post(`/api/photos/${photoId}/edit/preview`, JSON.parse(look));
        const url = URL.createObjectURL(await res.blob());
        if (alive) { setSrc((old) => { if (old) URL.revokeObjectURL(old); return url; }); setPreviewError(null); }
      } catch (e) {
        if (alive) setPreviewError((e as Error).message);
      } finally {
        if (alive) setBusy(false);
      }
    }, 220);
    return () => { alive = false; clearTimeout(t); };
  }, [look, photoId]);

  // A new preview can have a different shape (after a turn): keep a fixed-aspect crop true to it.
  useEffect(() => {
    if (aspect) setSpec((s) => (s.crop ? { ...s, crop: fitAspect(s.crop, aspect, imgRatio) } : s));
  }, [imgRatio, aspect]);

  const save = useMutation({ mutationFn: () => post(`/api/photos/${photoId}/edit`, spec).then((r) => r.json()) });
  const turn = (d: number) => set({ rotate: (spec.rotate + d + 360) % 360, crop: null });

  return (
    <>
      <div className="editor-top">
        <button className="btn btn-quiet btn-icon" onClick={onClose} aria-label="Close editor"><X size={19} /></button>
        <span className="editor-title">Edit — saved as a copy; the original stays as it is</span>
        {save.data ? (
          <span className="ok-text editor-saved">Saved as {String(save.data.path).split(/[\\/]/).pop()}</span>
        ) : (
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
            <Save size={15} /> Save as a copy
          </button>
        )}
      </div>
      <div className="editor-body">
        <div className="editor-stage">
          {src && (
            <CropFrame src={src} crop={spec.crop} active={cropping} aspect={aspect}
              onChange={(crop) => set({ crop })} onRatio={setImgRatio} />
          )}
          {busy && <span className="editor-busy dim">updating…</span>}
          {previewError && <span className="danger-text editor-busy">{previewError}</span>}
        </div>
        <aside className="editor-panel">
          <div className="editor-row">
            <button className="btn btn-ghost btn-sm" onClick={() => turn(-90)} aria-label="Rotate left"><RotateCcw size={15} /></button>
            <button className="btn btn-ghost btn-sm" onClick={() => turn(90)} aria-label="Rotate right"><RotateCw size={15} /></button>
            <button className={`btn btn-ghost btn-sm${spec.flip ? " is-on" : ""}`} onClick={() => set({ flip: !spec.flip, crop: null })}
              aria-label="Flip"><FlipHorizontal size={15} /></button>
            <button className={`btn btn-ghost btn-sm${cropping ? " is-on" : ""}`}
              onClick={() => { setCropping((v) => !v); if (!spec.crop) set({ crop: [0.05, 0.05, 0.95, 0.95] }); }}>
              <Crop size={15} /> Crop
            </button>
            <button className={`btn btn-ghost btn-sm${spec.auto ? " is-on" : ""}`} onClick={() => set({ auto: !spec.auto })}>
              <Wand2 size={15} /> Auto
            </button>
          </div>
          {cropping && (
            <div className="editor-row">
              {ASPECTS.map(([label, a]) => (
                <button key={label} className={`chip chip-button${aspect === a ? " chip-accent" : ""}`}
                  onClick={() => { setAspect(a); set({ crop: fitAspect(spec.crop ?? [0, 0, 1, 1], a, imgRatio) }); }}>{label}</button>
              ))}
              <button className="chip chip-button" onClick={() => { set({ crop: null }); setCropping(false); }}>no crop</button>
            </div>
          )}
          {([["brightness", "Light"], ["contrast", "Contrast"], ["saturation", "Colour"], ["warmth", "Warmth"]] as const).map(([k, label]) => (
            <label key={k} className="editor-slider">
              <span>{label}</span>
              <input type="range" min={-100} max={100} value={spec[k]} onChange={(e) => set({ [k]: Number(e.target.value) } as Partial<Spec>)}
                onDoubleClick={() => set({ [k]: 0 } as Partial<Spec>)} aria-label={label} />
              <span className="tnum dim">{spec[k] > 0 ? `+${spec[k]}` : spec[k]}</span>
            </label>
          ))}
          <div className="editor-filters">
            {FILTERS.map((f) => (
              <button key={f} className={`chip chip-button${spec.filter === f ? " chip-accent" : ""}`} onClick={() => set({ filter: f })}>{f}</button>
            ))}
          </div>
          <button className="btn btn-quiet btn-sm" onClick={() => { setSpec(EMPTY); setCropping(false); setAspect(null); }}>Reset</button>
          {save.error && <p className="danger-text">{(save.error as Error).message}</p>}
          {save.data && <p className="dim">The copy is being added to your library — it appears next to the original.</p>}
        </aside>
      </div>
    </>
  );
}

function fitAspect(box: Box, aspect: number | null, imgRatio = 1): Box {
  if (!aspect) return box;
  const [x1, y1, x2, y2] = box;
  const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
  let w = x2 - x1, h = (w / aspect) * imgRatio;
  if (h > 1) { h = 1; w = (h * aspect) / imgRatio; }
  const nx1 = Math.min(Math.max(cx - w / 2, 0), 1 - w), ny1 = Math.min(Math.max(cy - h / 2, 0), 1 - h);
  return [nx1, ny1, nx1 + w, ny1 + h];
}

/** The preview with a draggable crop frame: drag inside to move, drag a corner to resize. */
function CropFrame({ src, crop, active, aspect, onChange, onRatio }: {
  src: string; crop: Box | null; active: boolean; aspect: number | null; onChange: (c: Box) => void;
  onRatio: (r: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ mode: string; x: number; y: number; box: Box } | null>(null);
  const [ratio, setRatio] = useState(1);
  const box = crop ?? [0, 0, 1, 1];

  const down = (mode: string) => (e: React.PointerEvent) => {
    if (!active) return;
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { mode, x: e.clientX, y: e.clientY, box: [...box] as Box };
  };
  const move = (e: React.PointerEvent) => {
    const d = drag.current, el = ref.current;
    if (!d || !el) return;
    const r = el.getBoundingClientRect();
    const dx = (e.clientX - d.x) / r.width, dy = (e.clientY - d.y) / r.height;
    let [x1, y1, x2, y2] = d.box;
    if (d.mode === "move") {
      const w = x2 - x1, h = y2 - y1;
      x1 = Math.min(Math.max(x1 + dx, 0), 1 - w); y1 = Math.min(Math.max(y1 + dy, 0), 1 - h);
      x2 = x1 + w; y2 = y1 + h;
    } else {
      if (d.mode.includes("l")) x1 = Math.min(Math.max(x1 + dx, 0), x2 - 0.05);
      if (d.mode.includes("r")) x2 = Math.max(Math.min(x2 + dx, 1), x1 + 0.05);
      if (d.mode.includes("t")) y1 = Math.min(Math.max(y1 + dy, 0), y2 - 0.05);
      if (d.mode.includes("b")) y2 = Math.max(Math.min(y2 + dy, 1), y1 + 0.05);
      if (aspect) {                                  // keep the chosen shape: height follows width
        const h = ((x2 - x1) / aspect) * ratio;
        if (d.mode.includes("t")) y1 = Math.max(0, y2 - h); else y2 = Math.min(1, y1 + h);
      }
    }
    onChange([x1, y1, x2, y2]);
  };
  const [x1, y1, x2, y2] = box;
  return (
    <div className="crop-wrap" ref={ref} onPointerMove={move} onPointerUp={() => (drag.current = null)}>
      <img src={src} alt="" draggable={false} onLoad={(e) => {
        const r = e.currentTarget.naturalWidth / e.currentTarget.naturalHeight;
        setRatio(r);
        onRatio(r);
      }} />
      {(crop || active) && (
        <div className={`crop-box${active ? " is-active" : ""}`} onPointerDown={down("move")}
          style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }}>
          {active && ["tl", "tr", "bl", "br"].map((c) => (
            <span key={c} className={`crop-handle ${c}`} onPointerDown={down(c)} />
          ))}
        </div>
      )}
    </div>
  );
}

function Trim({ photoId, duration, onClose }: { photoId: number; duration: number; onClose: () => void }) {
  const vid = useRef<HTMLVideoElement>(null);
  const [len, setLen] = useState(duration || 0);
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(duration || 0);
  const save = useMutation({ mutationFn: () => post(`/api/photos/${photoId}/trim`, { start, end }).then((r) => r.json()) });
  return (
    <>
      <div className="editor-top">
        <button className="btn btn-quiet btn-icon" onClick={onClose} aria-label="Close"><X size={19} /></button>
        <span className="editor-title">Trim — saved as a copy; the original stays as it is</span>
        {save.data ? <span className="ok-text editor-saved">Saved as {String(save.data.path).split(/[\\/]/).pop()}</span> : (
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending || end <= start}>
            <Scissors size={15} /> Save trimmed copy
          </button>
        )}
      </div>
      <div className="editor-body editor-trim">
        <video ref={vid} src={videoUrl(photoId)} controls playsInline
          onLoadedMetadata={(e) => { const d = e.currentTarget.duration; if (Number.isFinite(d)) { setLen(d); if (!end) setEnd(d); } }} />
        <label className="editor-slider"><span>Start</span>
          <input type="range" min={0} max={len} step={0.1} value={start} aria-label="Start"
            onChange={(e) => { const v = Math.min(Number(e.target.value), end - 0.2); setStart(v); if (vid.current) vid.current.currentTime = v; }} />
          <span className="tnum dim">{clock(start)}</span></label>
        <label className="editor-slider"><span>End</span>
          <input type="range" min={0} max={len} step={0.1} value={end} aria-label="End"
            onChange={(e) => { const v = Math.max(Number(e.target.value), start + 0.2); setEnd(v); if (vid.current) vid.current.currentTime = v; }} />
          <span className="tnum dim">{clock(end)}</span></label>
        <p className="dim">The copy starts at the key frame at or just before the start you choose, so it may begin a moment early.
          Nothing is re-encoded, so quality is unchanged.</p>
        {save.error && <p className="danger-text">{(save.error as Error).message}</p>}
      </div>
    </>
  );
}
