/** Actions for photos selected in a grid: add to album, tag, plus page-specific extras. */
import { useCallback, useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, Sparkles, BookImage, CalendarClock, Columns2, FolderOutput, Lock, MapPin, RotateCw, Tag, Trash2, X } from "lucide-react";
import { useRole } from "../lib/hooks";
import { api } from "../lib/api";
import { AlbumPicker } from "./AlbumPicker";
import { CompareView } from "./CompareView";
import { CorrectionDialog } from "./CorrectionDialog";
import { StarRating } from "./StarRating";
import { ExportDialog } from "./ExportDialog";
import { CreateDialog } from "./CreateDialog";
import { TrashDialog, useAllowDelete } from "./TrashDialog";

export function SelectionBar({ selected, onClear, extra, allIds, onSelectAll }: {
  selected: Set<number>;
  onClear: () => void;
  extra?: React.ReactNode;
  /** every photo in the grid, for "Select all" */
  allIds?: number[];
  onSelectAll?: (ids: number[]) => void;
}) {
  const qc = useQueryClient();
  const [picker, setPicker] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [fixing, setFixing] = useState<"date" | "place" | null>(null);
  const [exporting, setExporting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [trashing, setTrashing] = useState(false);
  const canDelete = useAllowDelete();
  const role = useRole();
  const archive = useMutation({
    mutationFn: () => api.archive(ids, true),
    onSuccess: () => { setNote(`Archived ${ids.length.toLocaleString()} — find them in Collections → Archive`);
      qc.invalidateQueries({ queryKey: ["photos"] }); onClear(); },
  });
  const lock = useMutation({
    mutationFn: () => api.lock(ids),
    onSuccess: () => { qc.invalidateQueries(); onClear(); },
    onError: (e: Error) => setNote(e.message),
  });
  const [stars, setStars] = useState(0);
  const [tag, setTag] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const ids = [...selected];
  const addTag = useMutation({
    mutationFn: () => api.addTag(ids, tag.trim()),
    onSuccess: () => {
      setNote(`Tagged ${ids.length.toLocaleString()} “${tag.trim()}”`);
      setTag("");
      qc.invalidateQueries({ queryKey: ["tags"] });
      ids.forEach((p) => qc.invalidateQueries({ queryKey: ["photo", p] }));
    },
  });

  const rotate = useMutation({
    mutationFn: () => api.rotate(ids, 90),
    onSuccess: () => {
      setNote(`Rotated ${ids.length.toLocaleString()} — only in Memoria, the files are unchanged`);
      qc.invalidateQueries({ queryKey: ["photos"] });
      ids.forEach((p) => qc.invalidateQueries({ queryKey: ["photo", p] }));
    },
  });

  const rate = useMutation({
    mutationFn: (r: number) => api.rate(ids, r).then(() => r),
    onSuccess: (r) => {
      setStars(r);
      setNote(r ? `Rated ${ids.length.toLocaleString()} ${"★".repeat(r)}` : "Ratings cleared");
      qc.invalidateQueries({ queryKey: ["photos"] });
      ids.forEach((p) => qc.invalidateQueries({ queryKey: ["photo", p] }));
    },
  });

  if (selected.size === 0) return null;
  return (
    <div className="review-bar selection-bar" role="toolbar" aria-label="Selected photos">
      <span className="tnum"><strong>{selected.size.toLocaleString()}</strong> selected</span>
      {allIds && onSelectAll && selected.size < allIds.length && (
        <button className="btn btn-quiet btn-sm" onClick={() => onSelectAll(allIds)} title="Ctrl+A">
          Select all {allIds.length.toLocaleString()}
        </button>
      )}
      <button className="btn btn-primary btn-sm" onClick={() => setPicker(true)}>
        <BookImage size={14} /> Add to album
      </button>
      <form className="input-inline" onSubmit={(e) => { e.preventDefault(); if (tag.trim()) addTag.mutate(); }}>
        <Tag size={13} className="dim" />
        <input value={tag} onChange={(e) => { setTag(e.target.value); setNote(null); }} placeholder="Tag them…"
          maxLength={60} aria-label="Tag selected photos" />
      </form>
      <StarRating value={stars} size={14} onChange={(r) => rate.mutate(r)} label="Rate selected photos" />
      {selected.size >= 2 && selected.size <= 4 && (
        <button className="btn btn-ghost btn-sm" onClick={() => setComparing(true)} title="Side by side, to pick the best">
          <Columns2 size={14} /> Compare
        </button>
      )}
      <button className="btn btn-ghost btn-sm" onClick={() => rotate.mutate()} title="Turn 90° clockwise (the files are not changed)">
        <RotateCw size={14} /> Rotate
      </button>
      <button className="btn btn-ghost btn-sm" onClick={() => setFixing("date")}><CalendarClock size={14} /> Fix date</button>
      <button className="btn btn-ghost btn-sm" onClick={() => setFixing("place")}><MapPin size={14} /> Set place</button>
      {role !== "guest" && (
        <button className="btn btn-ghost btn-sm" onClick={() => setCreating(true)} title="Collage, animation or memory movie">
          <Sparkles size={14} /> Create
        </button>
      )}
      <button className="btn btn-ghost btn-sm" onClick={() => setExporting(true)} title="Copy the original files somewhere">
        <FolderOutput size={14} /> Export
      </button>
      {extra}
      {role !== "guest" && (
        <button className="btn btn-ghost btn-sm" onClick={() => archive.mutate()}
          title="Out of the timeline; still in search, albums and people">
          <Archive size={14} /> Archive
        </button>
      )}
      {role === "owner" && (
        <button className="btn btn-ghost btn-sm" onClick={() => lock.mutate()} title="Move to the PIN-protected Locked folder">
          <Lock size={14} /> Lock
        </button>
      )}
      {canDelete && (
        <button className="btn btn-quiet btn-sm selection-trash" onClick={() => setTrashing(true)}
          title="Move the files to the Trash (restorable for 30 days)">
          <Trash2 size={14} /> Delete
        </button>
      )}
      {note && <span className="dim selection-note">{note}</span>}
      <button className="btn btn-quiet btn-sm" onClick={() => { setNote(null); onClear(); }}>
        <X size={14} /> Clear
      </button>
      {creating && <CreateDialog photoIds={ids} onClose={() => setCreating(false)} />}
      {exporting && <ExportDialog spec={{ photo_ids: ids }} onClose={() => setExporting(false)}
        title={`Export ${ids.length.toLocaleString()} selected`} />}
      {trashing && <TrashDialog photoIds={ids} onClose={() => setTrashing(false)}
        onDone={(r) => { if (r.trashed && !r.skipped.length) onClear(); }} />}
      {comparing && <CompareView ids={ids} onClose={() => setComparing(false)} />}
      {fixing && <CorrectionDialog photoIds={ids} mode={fixing} onClose={() => setFixing(null)} />}
      {picker && <AlbumPicker photoIds={ids} onClose={() => setPicker(false)}
        onDone={() => setNote(`Added ${ids.length.toLocaleString()} to the album`)} />}
    </div>
  );
}

export function useSelection() {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const toggle = useCallback((id: number) => setSelected((s) => {
    const next = new Set(s);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  }), []);
  const addMany = useCallback((ids: number[]) => setSelected((s) => new Set([...s, ...ids])), []);
  const setAll = useCallback((ids: number[]) => setSelected(new Set(ids)), []);
  const clear = useCallback(() => setSelected(new Set()), []);
  return { selected, toggle, addMany, setAll, clear };
}

/** Ctrl/Cmd+A selects every photo in the grid while select mode is on. */
export function useSelectAllShortcut(active: boolean, ids: number[], setAll: (ids: number[]) => void) {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        setAll(ids);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, ids, setAll]);
}
