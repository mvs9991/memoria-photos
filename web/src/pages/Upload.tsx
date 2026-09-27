/**
 * Add photos from this device — on a phone, the camera roll. Each file is sent on its own
 * (two at a time) so one failure never loses the rest; the server keeps it, skips it as
 * already in the library, or says why not. When everything is in, only the upload folder
 * is indexed, so new photos appear within moments.
 */
import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleSlash, Copy, ImagePlus, Loader2, Upload as UploadIcon, XCircle } from "lucide-react";
import { api } from "../lib/api";
import { formatBytes } from "../lib/format";
import { useRole, useTitle } from "../lib/hooks";
import { BackupAppCard } from "../components/BackupAppCard";
import { uploadAll, type UploadStatus } from "../lib/upload";

type Status = UploadStatus;
interface Item { key: string; file: File; status: Status; progress: number; reason?: string }

export default function Upload() {
  useTitle("Upload");
  const role = useRole();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [running, setRunning] = useState(false);
  const [indexing, setIndexing] = useState<number | null>(null);
  const [drag, setDrag] = useState(false);
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });

  const update = (key: string, patch: Partial<Item>) =>
    setItems((cur) => cur.map((it) => (it.key === key ? { ...it, ...patch } : it)));

  const start = useCallback(async (files: File[]) => {
    if (!files.length) return;
    const fresh: Item[] = files.map((f, i) => ({ key: `${Date.now()}-${i}-${f.name}`, file: f, status: "waiting", progress: 0 }));
    setItems((cur) => [...fresh, ...cur]);
    setRunning(true);
    await uploadAll("/api/upload", fresh.map((f) => f.file), (i, patch) => update(fresh[i].key, patch));
    setRunning(false);
    try {
      const job = await api.finishUpload();
      setIndexing(job.job_id);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    } catch { /* indexing can also be started from Settings */ }
  }, [qc]);

  const counts = items.reduce((m, it) => ({ ...m, [it.status]: (m[it.status] ?? 0) + 1 }), {} as Record<string, number>);
  const total = items.reduce((a, it) => a + it.file.size, 0);
  // Only the owner is shown the server's folders (see the settings API).
  const folder = settings.data?.data_dir
    ? settings.data.settings?.upload_folder || `${settings.data.data_dir}\\uploads` : "";

  return (
    <div className="page upload-page">
      <div className="page-head">
        <div>
          <h1 className="display">Upload</h1>
          <p className="dim">Add photos and videos from this device. Anything already in your library is skipped.</p>
        </div>
      </div>

      <div className={`upload-drop${drag ? " is-over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); start([...e.dataTransfer.files]); }}>
        <ImagePlus size={34} />
        <button className="btn btn-primary" onClick={() => input.current?.click()} disabled={running}>
          <UploadIcon size={15} /> Choose photos &amp; videos
        </button>
        <span className="dim">or drop them here · on a phone, pick from your camera roll</span>
        <input ref={input} type="file" multiple hidden accept="image/*,video/*,.heic,.heif,.dng,.cr2,.cr3,.nef,.arw"
          onChange={(e) => { start([...(e.target.files ?? [])]); e.target.value = ""; }} />
      </div>

      {items.length > 0 && (
        <>
          <div className="upload-summary">
            <span><strong className="tnum">{items.length.toLocaleString()}</strong> files · {formatBytes(total)}</span>
            {counts.added ? <span className="ok-text"><CheckCircle2 size={14} /> {counts.added} added</span> : null}
            {counts.duplicate ? <span className="dim"><Copy size={14} /> {counts.duplicate} already there</span> : null}
            {counts.rejected ? <span className="dim"><CircleSlash size={14} /> {counts.rejected} not photos</span> : null}
            {counts.failed ? <span className="danger-text"><XCircle size={14} /> {counts.failed} failed</span> : null}
            {running && <span className="dim"><Loader2 size={14} className="spin" /> uploading…</span>}
            {!running && indexing !== null && (
              <span className="dim">Adding them to your library — <Link to="/photos?order=added" className="link">see what’s new</Link></span>
            )}
          </div>
          <ul className="upload-list">
            {items.slice(0, 400).map((it) => (
              <li key={it.key} className={`upload-item is-${it.status}`}>
                <span className="ellipsis">{it.file.name}</span>
                <span className="dim tnum">{formatBytes(it.file.size)}</span>
                {it.status === "uploading" ? (
                  <span className="upload-bar"><span style={{ width: `${Math.round(it.progress * 100)}%` }} /></span>
                ) : (
                  <span className="upload-status">{({ waiting: "waiting", added: "added", duplicate: it.reason ?? "already there",
                    rejected: it.reason ?? "skipped", failed: it.reason ?? "failed" } as Record<string, string>)[it.status]}</span>
                )}
              </li>
            ))}
          </ul>
          {counts.failed ? (
            <button className="btn btn-ghost btn-sm" disabled={running}
              onClick={() => { const again = items.filter((i) => i.status === "failed").map((i) => i.file);
                setItems((cur) => cur.filter((i) => i.status !== "failed")); start(again); }}>
              Try the failed ones again
            </button>
          ) : null}
        </>
      )}
      <p className="dim upload-note">Saved on the Memoria computer{folder ? <> in <code>{folder}</code></> : ""}, in folders by
        the date each photo was taken. Nothing already there is overwritten. For automatic backup of a phone, a sync app
        such as Syncthing into a folder Memoria indexes works alongside this.</p>
      {role !== "guest" && <BackupAppCard />}
    </div>
  );
}
