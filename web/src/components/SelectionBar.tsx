/** Actions for photos selected in a grid: add to album, tag, plus page-specific extras. */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookImage, Tag, X } from "lucide-react";
import { api } from "../lib/api";
import { AlbumPicker } from "./AlbumPicker";

export function SelectionBar({ selected, onClear, extra }: {
  selected: Set<number>;
  onClear: () => void;
  extra?: React.ReactNode;
}) {
  const qc = useQueryClient();
  const [picker, setPicker] = useState(false);
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
      {extra}
      {note && <span className="dim selection-note">{note}</span>}
      <button className="btn btn-quiet btn-sm" onClick={() => { setNote(null); onClear(); }}>
        <X size={14} /> Clear
      </button>
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
