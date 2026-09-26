/** Fullscreen photo viewer: zoom/pan, keyboard navigation, metadata and people. */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Trash2, Aperture, BookImage, Calendar, Camera, Check, ChevronLeft, ChevronRight, Copy, Download, EyeOff, Heart, Info,
  Layers, MapPin, Minus, Pencil, Plus, ScanText, Tag, Users, X, Sparkles, HardDrive,
} from "lucide-react";
import { TrashDialog, useAllowDelete } from "./TrashDialog";
import { api, downloadUrl, faceUrl, motionUrl, originalUrl, thumbUrl, videoUrl } from "../lib/api";
import { clock, exposureLabel, formatBytes, formatDateTime, megapixels } from "../lib/format";
import { AlbumPicker } from "./AlbumPicker";
import { CorrectionDialog } from "./CorrectionDialog";
import { StarRating } from "./StarRating";
import { useViewer } from "./ViewerContext";

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
  const [picker, setPicker] = useState(false);
  const [trashing, setTrashing] = useState(false);
  const canDelete = useAllowDelete();
  const [playingLive, setPlayingLive] = useState(false);
  useEffect(() => setPlayingLive(false), [id]);
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
      if (["INPUT", "TEXTAREA"].includes((e.target as HTMLElement)?.tagName)) return;   // typing a tag
      if (picker || trashing) return;
      if (e.key === "Delete" && canDelete && photo && photo.status !== "trashed") { setTrashing(true); return; }
      if (e.key === "Escape") { zoom > 1 ? (setZoom(1), setOffset({ x: 0, y: 0 })) : onClose(); }
      else if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "i") setShowInfo((v) => !v);
      else if (e.key === "f") favorite.mutate();
      else if (e.key === "l" && photo?.live) setPlayingLive(true);
      else if (/^[1-5]$/.test(e.key) && photo) {
        const n = Number(e.key);
        rate.mutate(photo.rating === n ? 0 : n);
      }
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
  const rate = useMutation({
    mutationFn: (rating: number) => api.rate([id], rating),
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
          {photo && <StarRating value={photo.rating} onChange={(r) => rate.mutate(r)} size={16} />}
          <button className={`btn btn-quiet btn-icon${photo?.favorite ? " is-on" : ""}`}
            onClick={() => favorite.mutate()} title="Favourite (F)" aria-label="Favourite">
            <Heart size={18} fill={photo?.favorite ? "currentColor" : "none"} />
          </button>
          <a className="btn btn-quiet btn-icon" href={downloadUrl(id)} download title="Download original"
            aria-label="Download original">
            <Download size={18} />
          </a>
          <button className="btn btn-quiet btn-icon" onClick={() => setPicker(true)} title="Add to album"
            aria-label="Add to album">
            <BookImage size={18} />
          </button>
          <button className="btn btn-quiet btn-icon" onClick={() => hide.mutate()} title="Hide from library"
            aria-label="Hide photo">
            <EyeOff size={18} />
          </button>
          {canDelete && photo && photo.status !== "trashed" && (
            <button className="btn btn-quiet btn-icon" onClick={() => setTrashing(true)}
              title="Move to Trash (Delete) — restorable for 30 days" aria-label="Move to Trash">
              <Trash2 size={18} />
            </button>
          )}
          {trashing && <TrashDialog photoIds={[id]} onClose={() => setTrashing(false)}
            onDone={(r) => { if (r.trashed) onClose(); }} />}
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
            {photo?.media_type === "video" ? (
              <video key={id} src={videoUrl(id)} poster={thumbUrl(id, "l")} controls autoPlay playsInline
                className="viewer-img" onClick={(e) => e.stopPropagation()} />
            ) : (
              <img key={id} src={zoom > 1.2 ? originalUrl(id) : thumbUrl(id, "l")} alt={photo?.filename ?? ""}
                className="viewer-img" draggable={false} />
            )}
            {playingLive && (
              <video key={`live-${id}`} src={motionUrl(id)} autoPlay muted playsInline
                className="viewer-img viewer-motion" onEnded={() => setPlayingLive(false)}
                onError={() => setPlayingLive(false)} />
            )}
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
            {photo?.live && (
              <button className={`btn btn-quiet btn-sm viewer-live${playingLive ? " is-on" : ""}`}
                onClick={() => setPlayingLive(true)} title="Play the moment (L)" aria-label="Play live photo">
                LIVE
              </button>
            )}
            {photo?.faces && photo.faces.length > 0 && (
              <button className={`btn btn-quiet btn-icon btn-sm${hoverFaces ? " is-on" : ""}`}
                onClick={() => setHoverFaces((v) => !v)} title="Show faces" aria-label="Show faces">
                <Users size={14} />
              </button>
            )}
          </div>
        </div>

        {picker && <AlbumPicker photoIds={[id]} onClose={() => setPicker(false)} />}
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
  const [fixing, setFixing] = useState<"date" | "place" | null>(null);
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
        {photo.media_type === "video" && (
          <div className="vi-sub dim">Video · {clock(photo.duration)}{photo.video_codec ? ` · ${photo.video_codec}` : ""}</div>
        )}
        {photo.live && <div className="vi-sub dim">Live photo — press L or LIVE to play the moment</div>}
      </div>

      <DescriptionEditor photoId={photo.id} value={photo.description} />

      <div className="vi-row">
        <Calendar size={15} className="dim" />
        <div>
          <div>{formatDateTime(photo.taken_ts)}</div>
          <div className="dim vi-small">
            {photo.corrected.date ? "corrected by you" : `from ${photo.date_source ?? "unknown"}`}
            {photo.date_confidence && photo.date_confidence !== "high" && (
              <span className="chip chip-sm" style={{ marginLeft: 6 }}>{photo.date_confidence} confidence</span>
            )}
          </div>
        </div>
        <button className="btn btn-quiet btn-sm vi-fix" onClick={() => setFixing("date")} title="Fix the date">
          <Pencil size={12} /> Fix
        </button>
      </div>
      {!photo.place && (
        <div className="vi-row">
          <MapPin size={15} className="dim" />
          <div className="dim">No location</div>
          <button className="btn btn-quiet btn-sm vi-fix" onClick={() => setFixing("place")}><Pencil size={12} /> Set place</button>
        </div>
      )}
      {fixing && <CorrectionDialog photoIds={[photo.id]} mode={fixing} onClose={() => setFixing(null)} />}

      {photo.place && (
        <div className="vi-row">
          <MapPin size={15} className="dim" />
          <div>
            <Link to={`/places/${photo.place.id}`} className="link">{photo.place.label}</Link>
            {photo.landmark && <div className="vi-small">{photo.landmark}</div>}
            <div className="dim vi-small">
              {photo.place.source === "gps" ? "from GPS" : photo.place.source === "user" ? "set by you"
                : photo.place.source === "takeout" ? "from Google Photos" : `inferred (${photo.place.confidence})`}
            </div>
          </div>
          <button className="btn btn-quiet btn-sm vi-fix" onClick={() => setFixing("place")} title="Change the place">
            <Pencil size={12} /> Fix
          </button>
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
                  <span>{f.label}{f.age != null ? `, ${f.age}` : ""}</span>
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

      {photo.stack && <StackStrip photoId={photo.id} stack={photo.stack} />}

      <TagEditor photoId={photo.id} tags={photo.tags} />

      {photo.albums.length > 0 && (
        <div className="vi-block">
          <div className="vi-head"><BookImage size={14} /> In albums</div>
          <div className="vi-tags">
            {photo.albums.map((a) => (
              <Link key={a.id} to={`/albums/${a.id}`} className="chip chip-button">{a.name}</Link>
            ))}
          </div>
        </div>
      )}

      {photo.ocr_text && (
        <div className="vi-block">
          <div className="vi-head"><ScanText size={14} /> Text in this photo</div>
          <p className="vi-caption vi-ocr">{photo.ocr_text}</p>
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

function DescriptionEditor({ photoId, value }: { photoId: number; value: string | null }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(value ?? "");
  useEffect(() => { setText(value ?? ""); setEditing(false); }, [photoId, value]);
  const save = useMutation({
    mutationFn: () => api.setDescription(photoId, text.trim() || null),
    onSuccess: () => { setEditing(false); qc.invalidateQueries({ queryKey: ["photo", photoId] }); },
  });
  if (editing) {
    return (
      <form className="vi-block vi-desc-form" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <textarea className="field vi-desc-input" autoFocus rows={3} value={text} maxLength={2000}
          placeholder="What was happening?" onChange={(e) => setText(e.target.value)} aria-label="Description" />
        <div className="vi-desc-actions">
          <button className="btn btn-quiet btn-sm" type="button" onClick={() => setEditing(false)}>Cancel</button>
          <button className="btn btn-primary btn-sm" type="submit"><Check size={13} /> Save</button>
        </div>
      </form>
    );
  }
  return value ? (
    <div className="vi-block">
      <p className="vi-desc">{value}
        <button className="btn btn-quiet btn-icon btn-sm" onClick={() => setEditing(true)} title="Edit description"
          aria-label="Edit description"><Pencil size={12} /></button>
      </p>
    </div>
  ) : (
    <div className="vi-block">
      <button className="btn btn-quiet btn-sm" onClick={() => setEditing(true)}><Pencil size={13} /> Add a description</button>
    </div>
  );
}

function TagEditor({ photoId, tags }: {
  photoId: number;
  tags: { name: string; confidence: number; by_user: boolean }[];
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["photo", photoId] });
    qc.invalidateQueries({ queryKey: ["tags"] });
  };
  const add = useMutation({ mutationFn: (n: string) => api.addTag([photoId], n), onSuccess: () => { setName(""); refresh(); } });
  const remove = useMutation({ mutationFn: (n: string) => api.removeTag([photoId], n), onSuccess: refresh });
  const shown = [...tags.filter((t) => t.by_user), ...tags.filter((t) => !t.by_user).slice(0, 10)];
  return (
    <div className="vi-block">
      <div className="vi-head"><Tag size={14} /> Tags</div>
      <div className="vi-tags">
        {shown.map((t) => (
          <span key={t.name} className={`chip vi-tag${t.by_user ? " chip-accent" : ""}`}
            title={t.by_user ? "Your tag" : `Found automatically · confidence ${(t.confidence * 100).toFixed(0)}%`}>
            <Link to={`/search?q=${encodeURIComponent(t.name)}`}>{t.name}</Link>
            <button className="vi-tag-x" onClick={() => remove.mutate(t.name)} aria-label={`Remove tag ${t.name}`}
              title="Remove — it will not be added back automatically"><X size={11} /></button>
          </span>
        ))}
      </div>
      <form className="vi-tag-add" onSubmit={(e) => { e.preventDefault(); if (name.trim()) add.mutate(name.trim()); }}>
        <input className="field field-sm" placeholder="Add a tag" value={name} maxLength={60}
          onChange={(e) => setName(e.target.value)} aria-label="Add a tag" />
      </form>
    </div>
  );
}

function StackStrip({ photoId, stack }: { photoId: number; stack: { id: number; members: number[] } }) {
  const qc = useQueryClient();
  const viewer = useViewer();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["photos"] });
    stack.members.forEach((m) => qc.invalidateQueries({ queryKey: ["photo", m] }));
  };
  const cover = useMutation({ mutationFn: () => api.stackCover(stack.id, photoId), onSuccess: refresh });
  const split = useMutation({ mutationFn: () => api.unstack(stack.id), onSuccess: refresh });
  return (
    <div className="vi-block">
      <div className="vi-head"><Layers size={14} /> Stack of {stack.members.length}</div>
      <div className="vi-similar">
        {stack.members.map((m, i) => (
          <button key={m} className={`vi-similar-item${m === photoId ? " is-current" : ""}`}
            onClick={() => viewer.open(stack.members, i)} title={m === stack.id ? "Cover" : "Open"}>
            <img src={thumbUrl(m, "sm")} alt="" loading="lazy" />
          </button>
        ))}
      </div>
      <div className="vi-stack-actions">
        {photoId !== stack.id && (
          <button className="btn btn-quiet btn-sm" onClick={() => cover.mutate()}>Use this as the cover</button>
        )}
        <button className="btn btn-quiet btn-sm" onClick={() => split.mutate()}
          title="Show every file separately — remembered for next time">Unstack</button>
      </div>
    </div>
  );
}
