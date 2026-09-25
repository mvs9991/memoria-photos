/** Actions for photos selected in a grid: add to album, tag, plus page-specific extras. */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookImage, CalendarClock, Columns2, MapPin, Tag, X } from "lucide-react";
import { api } from "../lib/api";
import { AlbumPicker } from "./AlbumPicker";
import { CompareView } from "./CompareView";
import { CorrectionDialog } from "./CorrectionDialog";
import { StarRating } from "./StarRating";

export function SelectionBar({ selected, onClear, extra }: {
  selected: Set<number>;
  onClear: () => void;
  extra?: React.ReactNode;
}) {
  const qc = useQueryClient();
  const [picker, setPicker] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [fixing, setFixing] = useState<"date" | "place" | null>(null);
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
      <button className="btn btn-ghost btn-sm" onClick={() => setFixing("date")}><CalendarClock size={14} /> Fix date</button>
      <button className="btn btn-ghost btn-sm" onClick={() => setFixing("place")}><MapPin size={14} /> Set place</button>
      {extra}
      {note && <span className="dim selection-note">{note}</span>}
      <button className="btn btn-quiet btn-sm" onClick={() => { setNote(null); onClear(); }}>
        <X size={14} /> Clear
      </button>
      {comparing && <CompareView ids={ids} onClose={() => setComparing(false)} />}
      {fixing && <CorrectionDialog photoIds={ids} mode={fixing} onClose={() => setFixing(null)} />}
      {picker && <AlbumPicker photoIds={ids} onClose={() => setPicker(false)}
        onDone={() => setNote(`Added ${ids.length.toLocaleString()} to the album`)} />}
    </div>
  );
}

export function useSelection() {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const toggle = (id: number) => setSelected((s) => {
    const next = new Set(s);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  return { selected, toggle, clear: () => setSelected(new Set()) };
}
