/**
 * Export copies of the original files — never moves or changes them. Either copied to a
 * folder on the computer running Memoria (a tracked job, resumable), or downloaded as a
 * .zip, which is the way to get them onto a phone or another computer.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, FolderOutput, X } from "lucide-react";
import { Portal } from "./Portal";
import { api, downloadZip, type ExportSpec } from "../lib/api";
import { formatBytes } from "../lib/format";

const LAYOUTS: [NonNullable<ExportSpec["layout"]>, string, string][] = [
  ["date", "By date", "2024/07/IMG_1234.jpg"],
  ["original", "Original folders", "Photos/Trips/Goa/IMG_1234.jpg"],
  ["flat", "One folder", "IMG_1234.jpg"],
];
const ZIP_WARN = 4 * 1024 ** 3;

export function ExportDialog({ spec, title, onClose, footer }: {
  spec: ExportSpec;
  title: string;
  onClose: () => void;
  footer?: React.ReactNode;
}) {
  const people = spec.person_ids?.length ?? 0;
  const [layout, setLayout] = useState<NonNullable<ExportSpec["layout"]>>("date");
  const [mode, setMode] = useState<NonNullable<ExportSpec["person_mode"]>>("each");
  const [xmp, setXmp] = useState(false);
  const [frames, setFrames] = useState(false);
  const [live, setLive] = useState(true);
  const [folder, setFolder] = useState("");
  const [jobId, setJobId] = useState<number | null>(null);
  const full: ExportSpec = { ...spec, layout, person_mode: mode, xmp, include_stack_frames: frames, include_live: live };

  const preview = useQuery({
    queryKey: ["export-preview", JSON.stringify({ ...full, folder: undefined })],
    queryFn: () => api.exportPreview(full),
  });
  const start = useMutation({
    mutationFn: () => api.startExport({ ...full, folder: folder.trim() }),
    onSuccess: (r) => setJobId(r.job_id),
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
    enabled: jobId !== null,
    refetchInterval: 1000,
  });
  const job = jobId !== null ? (jobs.data?.jobs ?? []).find((j: any) => j.id === jobId) ?? null : null;
  const finished = job && ["done", "failed", "cancelled", "interrupted"].includes(job.status);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const remote = !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const n = preview.data?.items ?? 0;
  const pct = job?.progress_total ? Math.round((job.progress_done / job.progress_total) * 100) : 0;

  return (
    <Portal>
      <div className="modal-backdrop modal-top" onClick={onClose}>
        <div className="modal export-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Export">
          <div className="modal-head">
            <h3><FolderOutput size={16} /> {title}</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <div className="modal-body fix-body">
            <p className="export-summary">
              {preview.isLoading ? "Counting…" : preview.error ? <span className="danger-text">{(preview.error as Error).message}</span> : (
                <><strong className="tnum">{n.toLocaleString()}</strong> {n === 1 ? "file" : "files"} ·{" "}
                  <span className="tnum">{formatBytes(preview.data!.bytes)}</span></>
              )}
            </p>
            {people > 1 && (
              <div className="segmented export-mode" role="group" aria-label="Several people">
                <button className={mode === "each" ? "on" : ""} onClick={() => setMode("each")}>A folder for each</button>
                <button className={mode === "together" ? "on" : ""} onClick={() => setMode("together")}>Only together</button>
                <button className={mode === "any" ? "on" : ""} onClick={() => setMode("any")}>Any of them</button>
              </div>
            )}
            {people > 1 && mode === "each" && (preview.data?.groups.length ?? 0) > 1 && (
              <ul className="export-groups dim">
                {preview.data!.groups.map((g) => (
                  <li key={g.label}><span className="ellipsis">{g.label}/</span>
                    <span className="tnum">{g.count.toLocaleString()} · {formatBytes(g.bytes)}</span></li>
                ))}
              </ul>
            )}
            <div className="export-layouts" role="radiogroup" aria-label="Folder layout">
              {LAYOUTS.map(([key, label, example]) => (
                <label key={key} className={`export-layout${layout === key ? " on" : ""}`}>
                  <input type="radio" name="layout" checked={layout === key} onChange={() => setLayout(key)} />
                  <span>{label}</span>
                  <code>{example}</code>
                </label>
              ))}
            </div>
            <div className="export-options">
              <label><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Live photo videos</label>
              <label><input type="checkbox" checked={frames} onChange={(e) => setFrames(e.target.checked)} /> every frame of stacks (RAW + bursts)</label>
              <label title="People, tags, stars and corrected dates in a .xmp beside each file, for Lightroom, digiKam, darktable">
                <input type="checkbox" checked={xmp} onChange={(e) => setXmp(e.target.checked)} /> .xmp with people &amp; tags
              </label>
            </div>

            <div className="export-targets">
              <form className="export-target" onSubmit={(e) => { e.preventDefault(); if (folder.trim()) start.mutate(); }}>
                <span className="export-target-title">Copy to a folder{remote ? " on the Memoria computer" : ""}</span>
                <div className="export-row">
                  <input className="field" placeholder="e.g. E:\Backup\Priya" value={folder}
                    onChange={(e) => setFolder(e.target.value)} aria-label="Export folder" disabled={!!job && !finished} />
                  <button className="btn btn-primary" type="submit" disabled={!folder.trim() || !n || start.isPending || (!!job && !finished)}>
                    <FolderOutput size={14} /> Copy
                  </button>
                </div>
                {job && (
                  <div className="export-progress">
                    {!finished && <span className="job-pill-bar"><span style={{ width: `${pct}%` }} /></span>}
                    <span className={job.status === "failed" ? "danger-text" : "dim"}>
                      {finished ? (job.error ? `${job.message}: ${job.error}` : job.message) : `${job.message || "Starting…"}`}
                    </span>
                  </div>
                )}
                {start.error && <p className="danger-text">{(start.error as Error).message}</p>}
              </form>
              <div className="export-target">
                <span className="export-target-title">Download to this device</span>
                <button className="btn btn-ghost" onClick={() => downloadZip(full)} disabled={!n}>
                  <Download size={14} /> Download .zip
                </button>
                {(preview.data?.bytes ?? 0) > ZIP_WARN && (
                  <span className="dim export-note">Over 4 GB — copying to a folder is more reliable.</span>
                )}
              </div>
            </div>
            <p className="dim fix-hint">Copies only. Your originals are read, never moved or changed; a folder inside your
              photo library is refused, and re-running into the same folder skips files already there.</p>
            {footer}
          </div>
        </div>
      </div>
    </Portal>
  );
}
