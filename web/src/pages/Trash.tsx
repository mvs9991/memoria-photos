/**
 * The Trash: everything the user deleted, with the days left before it is erased.
 * Restore puts files back where they were. "Delete permanently" and "Empty trash" are
 * the only irreversible actions in Memoria, and both ask for the count to be typed.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Trash2, X } from "lucide-react";
import { api, FLAG } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { Portal } from "../components/Portal";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { SelectToggle, useGridSelect } from "../components/SelectionBar";
import { useViewer } from "../components/ViewerContext";
import { formatBytes } from "../lib/format";
import { useTitle } from "../lib/hooks";

export default function Trash() {
  useTitle("Trash");
  const qc = useQueryClient();
  const viewer = useViewer();
  const [erase, setErase] = useState<{ ids: number[] | null } | null>(null);
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ["trash"], queryFn: api.trash });

  const items = useMemo(() => (data?.items ?? []).map((t) => ({
    id: t.photo_id, ratio: t.ratio, ts: t.trashed_at, flags: t.video ? FLAG.video : 0, dur: t.duration ?? 0,
  })), [data]);
  const allIds = useMemo(() => items.map((i) => i.id), [items]);
  const gs = useGridSelect(allIds);
  // The shared hook gives this page hold-and-drag, per-day Select all and Esc, like every other page.
  const selection = { selected: gs.selected, clear: gs.clear };

  const restore = useMutation({
    mutationFn: (ids: number[]) => api.restoreFromTrash(ids),
    onSuccess: () => { selection.clear(); qc.invalidateQueries(); },
  });

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading || !data) return <Spinner full label="Opening the Trash" />;
  const soonest = data.items.reduce((m, t) => Math.min(m, t.expires_at), Infinity);
  const daysLeft = (ts: number) => Math.max(0, Math.ceil((ts * 1000 - Date.now()) / 86_400_000));
  const sel = [...selection.selected];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Trash</h1>
          <p className="dim">
            {data.items.length
              ? `${data.items.length.toLocaleString()} ${data.items.length === 1 ? "file" : "files"} · ${formatBytes(data.bytes)}`
                + ` · each is deleted for good ${data.days} days after it was moved here`
                + (Number.isFinite(soonest) ? ` (next in ${daysLeft(soonest)} ${daysLeft(soonest) === 1 ? "day" : "days"})` : "")
              : `Deleted files wait here for ${data.days} days before they are erased`}
          </p>
        </div>
        {data.items.length > 0 && (
          <div className="toolbar">
            <SelectToggle sel={gs} />
            <button className="btn btn-ghost btn-sm" onClick={() => restore.mutate(allIds)} disabled={restore.isPending}>
              <RotateCcw size={14} /> Restore all
            </button>
            <button className="btn btn-danger btn-sm" onClick={() => setErase({ ids: null })}>
              <Trash2 size={14} /> Empty trash
            </button>
          </div>
        )}
      </div>

      {sel.length > 0 && (
        <div className="review-bar selection-bar" role="toolbar" aria-label="Selected files">
          <span className="tnum"><strong>{sel.length.toLocaleString()}</strong> selected</span>
          {sel.length < allIds.length && (
            <button className="btn btn-quiet btn-sm" onClick={gs.selectAll}>Select all {allIds.length}</button>
          )}
          <button className="btn btn-primary btn-sm" onClick={() => restore.mutate(sel)} disabled={restore.isPending}>
            <RotateCcw size={14} /> Restore
          </button>
          <button className="btn btn-danger btn-sm" onClick={() => setErase({ ids: sel })}>
            <Trash2 size={14} /> Delete permanently
          </button>
          <button className="btn btn-quiet btn-sm" onClick={selection.clear}><X size={14} /> Clear</button>
        </div>
      )}
      {restore.data && restore.data.failed.length > 0 && (
        <p className="danger-text">{restore.data.failed.length} could not be restored: {restore.data.failed[0].reason}</p>
      )}

      {erase && (
        <EraseDialog count={erase.ids ? erase.ids.length : data.items.length} ids={erase.ids}
          bytes={erase.ids ? data.items.filter((t) => erase.ids!.includes(t.photo_id)).reduce((a, t) => a + t.size, 0) : data.bytes}
          onClose={() => setErase(null)} onDone={() => { setErase(null); selection.clear(); qc.invalidateQueries(); }} />
      )}

      <PhotoGrid items={items} grouping="none" targetHeight={190} scrubber={false}
        onOpen={(_, index) => viewer.open(allIds, index)}
        {...gs.gridProps}
        emptyState={<EmptyState icon={<Trash2 size={26} />} title="The Trash is empty"
          hint={data.allow_delete ? "Files you delete — from a selection, the viewer or Duplicates — wait here before they are erased."
            : "Deleting is turned off in Settings."} />} />
    </div>
  );
}

function EraseDialog({ count, ids, bytes, onClose, onDone }: {
  count: number; ids: number[] | null; bytes: number; onClose: () => void; onDone: () => void;
}) {
  const [typed, setTyped] = useState("");
  const run = useMutation({
    mutationFn: () => (ids ? api.eraseFromTrash(ids, count) : api.emptyTrash(count)),
    onSuccess: onDone,
  });
  return (
    <Portal>
      <div className="modal-backdrop modal-top" onClick={onClose}>
        <div className="modal trash-modal" onClick={(e) => e.stopPropagation()} role="alertdialog" aria-label="Delete permanently">
          <div className="modal-head">
            <h3><Trash2 size={16} /> Delete {count.toLocaleString()} {count === 1 ? "file" : "files"} permanently?</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <div className="modal-body fix-body">
            <p>This frees {formatBytes(bytes)} and <strong>cannot be undone</strong> — Memoria keeps no other copy.</p>
            <label className="fix-row">
              <span className="dim">Type <strong>{count}</strong> to confirm</span>
              <input className="field" autoFocus inputMode="numeric" value={typed} onChange={(e) => setTyped(e.target.value)}
                aria-label="Type the number of files to confirm" />
            </label>
            {run.error && <p className="danger-text">{(run.error as Error).message}</p>}
          </div>
          <div className="modal-foot">
            <button className="btn btn-quiet" onClick={onClose}>Cancel</button>
            <button className="btn btn-danger" onClick={() => run.mutate()}
              disabled={typed.trim() !== String(count) || run.isPending}>
              <Trash2 size={14} /> Delete permanently
            </button>
          </div>
        </div>
      </div>
    </Portal>
  );
}
