/**
 * The confirmation before files go to the Trash. It states exactly how many files and
 * how much space, that they can be restored for N days, and — for a large selection —
 * asks for the number to be typed, so a stray click or keypress cannot delete much.
 * The server independently refuses a request whose count does not match.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, X } from "lucide-react";
import { Portal } from "./Portal";
import { api } from "../lib/api";
import { formatBytes } from "../lib/format";
import { useRole } from "../lib/hooks";

export const TYPE_TO_CONFIRM_AT = 25;

/** Delete buttons only appear when deleting is allowed (Settings → Trash). */
export function useAllowDelete(): boolean {
  const role = useRole();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 60_000,
    enabled: role === "owner" });
  return role === "owner" && (settings.data?.settings?.allow_delete ?? false);
}

export function TrashDialog({ photoIds, onClose, onDone }: {
  photoIds: number[];
  onClose: () => void;
  onDone?: (result: { trashed: number; skipped: { photo_id: number; reason: string }[] }) => void;
}) {
  const qc = useQueryClient();
  const n = photoIds.length;
  const [typed, setTyped] = useState("");
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 60_000 });
  const size = useQuery({
    queryKey: ["export-preview", "trash", photoIds.join(",")],
    queryFn: () => api.exportPreview({ photo_ids: photoIds }),
  });
  const days = settings.data?.settings?.trash_days ?? 30;
  const allowed = settings.data?.settings?.allow_delete ?? true;
  const needsTyping = n >= TYPE_TO_CONFIRM_AT;
  const ready = allowed && (!needsTyping || typed.trim() === String(n));

  const run = useMutation({
    mutationFn: () => api.moveToTrash(photoIds, n),
    onSuccess: (r) => {
      qc.invalidateQueries();
      onDone?.(r);
      if (!r.skipped.length) onClose();
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const what = n === 1 ? "this file" : `${n.toLocaleString()} files`;
  return (
    <Portal>
      <div className="modal-backdrop modal-top" onClick={onClose}>
        <div className="modal trash-modal" onClick={(e) => e.stopPropagation()} role="alertdialog"
          aria-label="Move to Trash">
          <div className="modal-head">
            <h3><Trash2 size={16} /> Move {what} to the Trash?</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <div className="modal-body fix-body">
            {!allowed ? (
              <p className="danger-text">Deleting is turned off in Settings → Trash.</p>
            ) : (
              <>
                <p>
                  The {n === 1 ? "file is" : "files are"} removed from {n === 1 ? "its folder" : "their folders"}
                  {size.data ? <> ({formatBytes(size.data.bytes)})</> : null} and kept in Memoria’s Trash for{" "}
                  <strong>{days} days</strong>. Until then you can restore {n === 1 ? "it" : "them"} to exactly where
                  {n === 1 ? " it was" : " they were"}. After that {n === 1 ? "it is" : "they are"} deleted for good.
                </p>
                <p className="dim fix-hint">A Live photo’s video goes with it. Albums and people update automatically.</p>
                {needsTyping && (
                  <label className="fix-row">
                    <span className="dim">Type <strong>{n}</strong> to confirm</span>
                    <input className="field" autoFocus inputMode="numeric" value={typed}
                      onChange={(e) => setTyped(e.target.value)} aria-label="Type the number of files to confirm" />
                  </label>
                )}
              </>
            )}
            {run.data && run.data.skipped.length > 0 && (
              <div className="danger-text">
                Moved {run.data.trashed.toLocaleString()}; {run.data.skipped.length} could not be moved and were left
                where they are: {run.data.skipped.slice(0, 3).map((s) => s.reason).join("; ")}
              </div>
            )}
            {run.error && <p className="danger-text">{(run.error as Error).message}</p>}
          </div>
          <div className="modal-foot">
            <button className="btn btn-quiet" onClick={onClose}>{run.data ? "Close" : "Cancel"}</button>
            {!run.data && (
              <button className="btn btn-danger" onClick={() => run.mutate()} disabled={!ready || run.isPending}>
                <Trash2 size={14} /> Move to Trash
              </button>
            )}
          </div>
        </div>
      </div>
    </Portal>
  );
}
