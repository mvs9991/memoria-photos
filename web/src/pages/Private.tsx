/**
 * My private photos: seen by me alone (engine/private.py). The server decides who sees them;
 * this page only lists mine and lets me give them back to the family.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeOff, Users, X } from "lucide-react";
import { api, gridItems } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, Spinner } from "../components/States";
import { SelectToggle, useGridSelect } from "../components/SelectionBar";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";
import { FloatingBar } from "../components/SelectionBar";

export default function Private() {
  useTitle("Private");
  const qc = useQueryClient();
  const viewer = useViewer();
  const photos = useQuery({ queryKey: ["private-photos"], queryFn: api.privatePhotos });
  const items = useMemo(() => gridItems(photos.data), [photos.data]);
  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  const gs = useGridSelect(allIds);
  // The shared hook gives this page hold-and-drag, per-day Select all and Esc, like every other page.
  const selection = { selected: gs.selected, clear: gs.clear };
  const share = useMutation({
    mutationFn: (ids: number[]) => api.shareWithFamily(ids),
    onSuccess: () => { qc.invalidateQueries(); selection.clear(); },
  });
  const sel = [...selection.selected];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Private</h1>
          <p className="dim">Only you see these. They are not in the family's timeline, people, places or search — and
            not in yours either, so they stay out of what everyone shares. Their files are on the Memoria computer as usual.</p>
        </div>
        <div className="toolbar">
          <SelectToggle sel={gs} />
        </div>
      </div>
      {sel.length > 0 && (
        <FloatingBar>
          <div className="review-bar selection-bar" role="toolbar" aria-label="Selected">
            <span className="tnum"><strong>{sel.length}</strong> selected</span>
            <button className="btn btn-ghost btn-sm" disabled={share.isPending} onClick={() => share.mutate(sel)}>
              <Users size={14} /> Share with family
            </button>
            <button className="btn btn-quiet btn-sm" onClick={selection.clear}><X size={14} /> Clear</button>
          </div>
        </FloatingBar>
      )}
      {photos.isLoading ? <Spinner /> : (
        <PhotoGrid items={items} grouping="month" targetHeight={200}
          onOpen={(_, index) => viewer.open(allIds, index)}
          {...gs.gridProps}
          emptyState={<EmptyState icon={<EyeOff size={26} />} title="Nothing private yet"
            hint="Turn on “Keep photos from my phone private” in Settings, or select photos you uploaded and choose “Make private”."
            action={<Link to="/settings" className="btn btn-ghost btn-sm">Settings</Link>} />} />
      )}
    </div>
  );
}
