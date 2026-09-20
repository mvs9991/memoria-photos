/** Fullscreen photo viewer: zoom/pan, keyboard navigation, metadata and people. */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Aperture, Calendar, Camera, ChevronLeft, ChevronRight, Copy, Download, EyeOff, Heart, Info,
  MapPin, Maximize2, Minus, Plus, Tag, Users, X, Sparkles, HardDrive,
} from "lucide-react";
import { api, downloadUrl, faceUrl, originalUrl, thumbUrl } from "../lib/api";
import { exposureLabel, formatBytes, formatDateTime, megapixels } from "../lib/format";

interface Props {
  ids: number[];
  index: number;
  onIndex: (i: number) => void;
  onClose: () => void;
}

export function PhotoViewer({ ids, index, onIndex, onClose }: Props) {
  const id = ids[index];
  const qc = useQueryClient();
  const [showInfo, setShowInfo] = useState(() => localStorage.getItem("viewer-info") !== "0");
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [hoverFaces, setHoverFaces] = useState(false);
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const imgWrapRef = useRef<HTMLDivElement>(null);

  const { data: photo } = useQuery({ queryKey: ["photo", id], queryFn: () => api.photo(id), enabled: !!id });
  const { data: similar } = useQuery({
    queryKey: ["similar", id],
    queryFn: () => api.similar(id, 14),
    enabled: !!id && showInfo,
  });

  useEffect(() => localStorage.setItem("viewer-info", showInfo ? "1" : "0"), [showInfo]);

  const go = useCallback((delta: number) => {
    const next = index + delta;
    if (next < 0 || next >= ids.length) return;
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    onIndex(next);
  }, [index, ids.length, onIndex]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { zoom > 1 ? (setZoom(1), setOffset({ x: 0, y: 0 })) : onClose(); }
      else if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "i") setShowInfo((v) => !v);
      else if (e.key === "f") favorite.mutate();
      else if (e.key === "+" || e.key === "=") setZoom((z) => Math.min(6, z * 1.4));
      else if (e.key === "-") setZoom((z) => Math.max(1, z / 1.4));
      else if (e.key === "0") { setZoom(1); setOffset({ x: 0, y: 0 }); }
      else if (e.key === "Home") onIndex(0);
      else if (e.key === "End") onIndex(ids.length - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Preload neighbours so paging feels instant.
  useEffect(() => {
    [index - 1, index + 1, index + 2].forEach((i) => {
      if (i >= 0 && i < ids.length) {
        const img = new Image();
        img.src = thumbUrl(ids[i], "l");
      }
    });
  }, [index, ids]);

  const favorite = useMutation({
    mutationFn: () => api.setFlags(id, { favorite: !photo?.favorite }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["photo", id] });
      qc.invalidateQueries({ queryKey: ["photos"] });
    },
  });
  const hide = useMutation({
    mutationFn: () => api.setFlags(id, { hidden: true }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["photos"] });
      go(1);
    },
  });

  const onWheel = (e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey && zoom === 1) return;
    e.preventDefault();
    setZoom((z) => Math.min(6, Math.max(1, z * (e.deltaY < 0 ? 1.15 : 0.87))));
  };

  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label="Photo viewer">
      <div className="viewer-top">
        <div className="viewer-top-left">
          <button className="btn btn-quiet btn-icon" onClick={onClose} aria-label="Close viewer (Esc)">
            <X size={19} />
          </button>
          <span className="viewer-position tnum dim">
            {index + 1} / {ids.length}
          </span>
        </div>
        <div className="viewer-top-right">
          <button className={`btn btn-quiet btn-icon${photo?.favorite ? " is-on" : ""}`}
            onClick={() => favorite.mutate()} title="Favourite (F)" aria-label="Favourite">
            <Heart size={18} fill={photo?.favorite ? "currentColor" : "none"} />
          </button>
          <a className="btn btn-quiet btn-icon" href={downloadUrl(id)} download title="Download original"
            aria-label="Download original">
            <Download size={18} />
          </a>
          <button className="btn btn-quiet btn-icon" onClick={() => hide.mutate()} title="Hide from library"
            aria-label="Hide photo">
            <EyeOff size={18} />
          </button>
          <button className={`btn btn-quiet btn-icon${showInfo ? " is-on" : ""}`}
            onClick={() => setShowInfo((v) => !v)} title="Details (I)" aria-label="Toggle details">
            <Info size={18} />
          </button>
        </div>
      </div>

      <div className="viewer-body">
        <div
          className={`viewer-stage${zoom > 1 ? " is-zoomed" : ""}`}
          ref={imgWrapRef}
          onWheel={onWheel}
          onDoubleClick={() => (zoom > 1 ? (setZoom(1), setOffset({ x: 0, y: 0 })) : setZoom(2.5))}
          onMouseDown={(e) => {
            if (zoom <= 1) return;
            dragRef.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
          }}
          onMouseMove={(e) => {
            const d = dragRef.current;
            if (!d) return;
            setOffset({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) });
          }}
          onMouseUp={() => (dragRef.current = null)}
          onMouseLeave={() => (dragRef.current = null)}
          onClick={(e) => {
            if (e.target === e.currentTarget && zoom === 1) onClose();
          }}
        >
          <div className="viewer-img-wrap" style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }}>
            <img key={id} src={zoom > 1.2 ? originalUrl(id) : thumbUrl(id, "l")} alt={photo?.filename ?? ""}
              className="viewer-img" draggable={false} />
            {hoverFaces && photo?.faces?.map((f) => (
              <span key={f.id} className="viewer-face"
                style={{ left: `${f.box[0] * 100}%`, top: `${f.box[1] * 100}%`,
                  width: `${(f.box[2] - f.box[0]) * 100}%`, height: `${(f.box[3] - f.box[1]) * 100}%` }}>
                {f.label && <span className="viewer-face-label">{f.label}</span>}
              </span>
            ))}
          </div>

          {index > 0 && (
            <button className="viewer-nav prev" onClick={(e) => { e.stopPropagation(); go(-1); }}
              aria-label="Previous photo">
              <ChevronLeft size={26} />
            </button>
          )}
          {index < ids.length - 1 && (
            <button className="viewer-nav next" onClick={(e) => { e.stopPropagation(); go(1); }}
              aria-label="Next photo">
              <ChevronRight size={26} />
            </button>
          )}

          <div className="viewer-zoom">
            <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setZoom((z) => Math.max(1, z / 1.4))}
              aria-label="Zoom out"><Minus size={14} /></button>
            <span className="tnum dim">{Math.round(zoom * 100)}%</span>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setZoom((z) => Math.min(6, z * 1.4))}
              aria-label="Zoom in"><Plus size={14} /></button>
            {photo?.faces && photo.faces.length > 0 && (
              <button className={`btn btn-quiet btn-icon btn-sm${hoverFaces ? " is-on" : ""}`}
                onClick={() => setHoverFaces((v) => !v)} title="Show faces" aria-label="Show faces">
                <Users size={14} />
              </button>
            )}
          </div>
        </div>

        {showInfo && photo && <InfoPanel photo={photo} similar={similar?.photos ?? []} onOpenSimilar={(sid) => {
          const pos = ids.indexOf(sid);
          if (pos >= 0) onIndex(pos);
          else onIndex(index);
        }} />}
      </div>
    </div>
  );
}

function InfoPanel({ photo, similar, onOpenSimilar }: {
  photo: NonNullable<Awaited<ReturnType<typeof api.photo>>>;
  similar: { id: number; score: number }[];
  onOpenSimilar: (id: number) => void;
}) {
  const cam = photo.camera;
  const camText = [cam.make, cam.model].filter(Boolean).join(" ");
  const settings = [
    cam.focal_length ? `${Math.round(cam.focal_length)}mm` : null,
    cam.aperture ? `ƒ/${cam.aperture.toFixed(1)}` : null,
    exposureLabel(cam.exposure_time),
    cam.iso ? `ISO ${cam.iso}` : null,
  ].filter(Boolean).join(" · ");

  return (
    <aside className="viewer-info">
      <div className="vi-block">
        <h3 className="vi-title">{photo.filename}</h3>
        <div className="vi-sub dim">
          {photo.folder || "/"} · {formatBytes(photo.size)} · {photo.width}×{photo.height}
          {photo.width && photo.height ? ` · ${megapixels(photo.width, photo.height)}` : ""}
        </div>
      </div>

      <div className="vi-row">
        <Calendar size={15} className="dim" />
        <div>
          <div>{formatDateTime(photo.taken_ts)}</div>
          <div className="dim vi-small">
            from {photo.date_source ?? "unknown"}
            {photo.date_confidence && photo.date_confidence !== "high" && (
              <span className="chip chip-sm" style={{ marginLeft: 6 }}>{photo.date_confidence} confidence</span>
            )}
          </div>
        </div>
      </div>

      {photo.place && (
        <div className="vi-row">
          <MapPin size={15} className="dim" />
          <div>
            <Link to={`/places/${photo.place.id}`} className="link">{photo.place.label}</Link>
            {photo.landmark && <div className="vi-small">{photo.landmark}</div>}
            <div className="dim vi-small">
              {photo.place.source === "gps" ? "from GPS" : `inferred (${photo.place.confidence})`}
            </div>
          </div>
        </div>
      )}

      {photo.event && (
        <div className="vi-row">
          <Sparkles size={15} className="dim" />
          <div>
            <Link to={`/events/${photo.event.id}`} className="link">{photo.event.title}</Link>
            {photo.trip && (
              <div className="vi-small">
                part of <Link to={`/events/${photo.trip.id}`} className="link">{photo.trip.title}</Link>
              </div>
            )}
          </div>
        </div>
      )}

      {camText && (
        <div className="vi-row">
          <Camera size={15} className="dim" />
          <div>
            <div>{camText}</div>
            {settings && <div className="dim vi-small">{settings}</div>}
            {cam.lens && <div className="dim vi-small">{cam.lens}</div>}
          </div>
        </div>
      )}

      {photo.faces.length > 0 && (
        <div className="vi-block">
          <div className="vi-head"><Users size={14} /> People</div>
          <div className="vi-faces">
            {photo.faces.map((f) => (
              f.person_id ? (
                <Link key={f.id} to={`/people/${f.person_id}`} className="vi-face" title={f.label ?? ""}>
                  <img src={faceUrl(f.id, 96)} alt={f.label ?? "face"} />
                  <span>{f.label}</span>
                </Link>
              ) : (
                <div key={f.id} className="vi-face is-unknown" title="Not assigned to a person">
                  <img src={faceUrl(f.id, 96)} alt="unidentified face" />
                  <span className="dim">Unknown</span>
                </div>
              )
            ))}
          </div>
        </div>
      )}

      {photo.tags.length > 0 && (
        <div className="vi-block">
          <div className="vi-head"><Tag size={14} /> What's in this photo</div>
          <div className="vi-tags">
            {photo.tags.slice(0, 10).map((t) => (
              <Link key={t.name} to={`/search?q=${encodeURIComponent(t.name)}`} className="chip chip-button"
                title={`confidence ${(t.confidence * 100).toFixed(0)}%`}>
                {t.name}
              </Link>
            ))}
          </div>
        </div>
      )}

      <div className="vi-block">
        <div className="vi-head"><Sparkles size={14} /> Description</div>
        {photo.caption ? (
          <p className="vi-caption">{photo.caption}</p>
        ) : (
          <DescribeButton photoId={photo.id} />
        )}
      </div>

      {photo.duplicates.length > 0 && (
        <div className="vi-block">
          <div className="vi-head"><Copy size={14} /> Duplicates</div>
          {photo.duplicates.map((d) => (
            <Link key={d.group_id} to="/duplicates" className="vi-dup">
              <span className={`chip chip-${d.kind}`}>{d.kind}</span>
              <span className="dim">{d.count} copies · this one is {d.relation}</span>
            </Link>
          ))}
        </div>
      )}

      <div className="vi-block">
        <div className="vi-head"><Aperture size={14} /> Quality</div>
        <div className="vi-quality">
          <QualityBar label="Overall" value={(photo.quality.score ?? 0) / 100} />
          {photo.quality.aesthetic != null && <QualityBar label="Aesthetic" value={photo.quality.aesthetic} />}
        </div>
      </div>

      {similar.length > 0 && (
        <div className="vi-block">
          <div className="vi-head">Similar photos</div>
          <div className="vi-similar">
            {similar.map((s) => (
              <button key={s.id} className="vi-similar-item" onClick={() => onOpenSimilar(s.id)}
                title={`${Math.round(s.score * 100)}% similar`}>
                <img src={thumbUrl(s.id, "sm")} alt="" loading="lazy" />
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="vi-block vi-path">
        <div className="vi-head"><HardDrive size={14} /> File</div>
        <code className="vi-code">{photo.root}\{photo.path.replace(/\//g, "\\")}</code>
      </div>
    </aside>
  );
}

function DescribeButton({ photoId }: { photoId: number }) {
  const qc = useQueryClient();
  const describe = useMutation({
    mutationFn: () => api.describe(photoId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["photo", photoId] }),
  });
  if (describe.isError) {
    return <p className="dim vi-small">Local description model unavailable.</p>;
  }
  return (
    <button className="btn btn-ghost btn-sm" onClick={() => describe.mutate()} disabled={describe.isPending}>
      {describe.isPending ? "Looking at the photo…" : "Describe this photo"}
    </button>
  );
}

function QualityBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="qbar">
      <span className="qbar-label dim">{label}</span>
      <span className="qbar-track">
        <span className="qbar-fill" style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }} />
      </span>
      <span className="qbar-value tnum dim">{Math.round(value * 100)}</span>
    </div>
  );
}
