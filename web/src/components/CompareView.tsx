/**
 * Side-by-side culling: 2–4 photos with zoom and pan kept in sync, their quality signals
 * underneath, and "keep this one" to hide the rest. Hiding never touches the files.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Check, EyeOff, Minus, Plus, X } from "lucide-react";
import { Portal } from "./Portal";
import { api, originalUrl, thumbUrl, type PhotoDetail } from "../lib/api";
import { formatBytes, formatDateTime, megapixels } from "../lib/format";
import { StarRating } from "./StarRating";

export function CompareView({ ids, onClose }: { ids: number[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const details = useQueries({ queries: ids.map((id) => ({ queryKey: ["photo", id], queryFn: () => api.photo(id) })) });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "+" || e.key === "=") setZoom((z) => Math.min(8, z * 1.5));
      else if (e.key === "-") setZoom((z) => Math.max(1, z / 1.5));
      else if (e.key === "0") { setZoom(1); setPan({ x: 0, y: 0 }); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const refresh = (id?: number) => {
    qc.invalidateQueries({ queryKey: ["photos"] });
    qc.invalidateQueries({ queryKey: ["album"] });
    if (id !== undefined) qc.invalidateQueries({ queryKey: ["photo", id] });
  };
  const keepOnly = useMutation({
    mutationFn: async (keep: number) => {
      const others = ids.filter((i) => i !== keep && !hidden.has(i));
      await Promise.all(others.map((i) => api.setFlags(i, { hidden: true })));
      return others;
    },
    onSuccess: (others) => { setHidden((h) => new Set([...h, ...others])); refresh(); },
  });
  const hideOne = useMutation({
    mutationFn: (id: number) => api.setFlags(id, { hidden: true }).then(() => id),
    onSuccess: (id) => { setHidden((h) => new Set([...h, id])); refresh(); },
  });
  const rate = useMutation({
    mutationFn: ({ id, rating }: { id: number; rating: number }) => api.rate([id], rating).then(() => id),
    onSuccess: (id) => refresh(id),
  });

  const bestScore = Math.max(...details.map((d) => d.data?.quality.score ?? 0));

  return (
    <Portal>
    <div className="compare" role="dialog" aria-modal="true" aria-label="Compare photos">
      <div className="compare-top">
        <strong>Compare {ids.length} photos</strong>
        <span className="dim compare-hint">Scroll or +/− to zoom together · drag to pan · “Keep” hides the others (files are never touched)</span>
        <div className="compare-zoom">
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setZoom((z) => Math.max(1, z / 1.5))} aria-label="Zoom out"><Minus size={14} /></button>
          <span className="tnum dim">{Math.round(zoom * 100)}%</span>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setZoom((z) => Math.min(8, z * 1.5))} aria-label="Zoom in"><Plus size={14} /></button>
        </div>
        <button className="btn btn-quiet btn-icon" onClick={onClose} aria-label="Close compare"><X size={18} /></button>
      </div>
      <div className="compare-grid" style={{ gridTemplateColumns: `repeat(${Math.min(ids.length, 4)}, minmax(0, 1fr))` }}>
        {ids.map((id, n) => {
          const d = details[n]?.data as PhotoDetail | undefined;
          const gone = hidden.has(id);
          return (
            <div key={id} className={`compare-cell${gone ? " is-hidden" : ""}`}>
              <div className="compare-stage"
                onWheel={(e) => setZoom((z) => Math.min(8, Math.max(1, z * (e.deltaY < 0 ? 1.15 : 0.87))))}
                onMouseDown={(e) => { if (zoom > 1) drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; }}
                onMouseMove={(e) => {
                  const g = drag.current;
                  if (g) setPan({ x: g.px + (e.clientX - g.x) / zoom, y: g.py + (e.clientY - g.y) / zoom });
                }}
                onMouseUp={() => (drag.current = null)} onMouseLeave={() => (drag.current = null)}>
                <img src={zoom > 1.3 ? originalUrl(id) : thumbUrl(id, "l")} alt={d?.filename ?? ""} draggable={false}
                  style={{ transform: `scale(${zoom}) translate(${pan.x}px, ${pan.y}px)` }} />
                {gone && <span className="compare-gone"><EyeOff size={16} /> Hidden</span>}
              </div>
              <div className="compare-info">
                <div className="compare-name ellipsis" title={d?.filename}>{d?.filename ?? "…"}</div>
                {d && (
                  <>
                    <div className="dim compare-meta tnum">
                      {formatDateTime(d.taken_ts)} · {d.width}×{d.height} {megapixels(d.width, d.height)} · {formatBytes(d.size)}
                    </div>
                    <div className="compare-scores tnum">
                      <span className={(d.quality.score ?? 0) === bestScore ? "compare-best" : ""}>
                        Quality {Math.round(d.quality.score ?? 0)}
                      </span>
                      <span className="dim">sharpness {Math.round(d.quality.blur ?? 0)}</span>
                      <span className="dim">faces {d.faces.length}</span>
                    </div>
                    <div className="compare-actions">
                      <StarRating value={d.rating} size={14} onChange={(r) => rate.mutate({ id, rating: r })} />
                      <button className="btn btn-primary btn-sm" disabled={gone || keepOnly.isPending}
                        onClick={() => keepOnly.mutate(id)}><Check size={13} /> Keep this</button>
                      <button className="btn btn-quiet btn-sm" disabled={gone} onClick={() => hideOne.mutate(id)}>
                        <EyeOff size={13} /> Hide
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
    </Portal>
  );
}
