/** Make a collage, an animation or a memory movie from the selected photos (a new file; the photos are only read). */
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Clapperboard, Film, LayoutGrid, Sparkles, X } from "lucide-react";
import { Portal } from "./Portal";
import { api } from "../lib/api";

type Kind = "collage" | "animation" | "movie";
const KINDS: { kind: Kind; icon: typeof LayoutGrid; title: string; hint: string; min: number; max: number }[] = [
  { kind: "collage", icon: LayoutGrid, title: "Collage", hint: "2–9 photos in one picture", min: 2, max: 9 },
  { kind: "animation", icon: Film, title: "Animation", hint: "a looping GIF — made for bursts", min: 2, max: 60 },
  { kind: "movie", icon: Clapperboard, title: "Memory movie", hint: "a video that drifts from photo to photo", min: 2, max: 150 },
];

async function post(url: string, body: unknown) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ?? res.statusText);
  return data;
}

export function CreateDialog({ photoIds, onClose }: { photoIds: number[]; onClose: () => void }) {
  const n = photoIds.length;
  const [kind, setKind] = useState<Kind>(n <= 9 ? "collage" : "movie");
  const [seconds, setSeconds] = useState(3);
  const run = useMutation({ mutationFn: () => post(`/api/create/${kind}`, { photo_ids: photoIds, seconds_each: seconds }) });
  const jobId: number | undefined = kind === "movie" ? run.data?.job_id : undefined;
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.jobs, enabled: !!jobId, refetchInterval: 1000 });
  const job = jobId ? (jobs.data?.jobs ?? []).find((j: any) => j.id === jobId) : null;
  const spec = KINDS.find((k) => k.kind === kind)!;
  const fits = n >= spec.min && n <= spec.max;
  const name = run.data?.path ? String(run.data.path).split(/[\\/]/).pop() : null;

  return (
    <Portal>
      <div className="modal-backdrop modal-top" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Create">
          <div className="modal-head">
            <h3><Sparkles size={16} /> Make something from {n} {n === 1 ? "photo" : "photos"}</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <div className="modal-body fix-body">
            <div className="export-layouts" role="radiogroup" aria-label="What to make">
              {KINDS.map(({ kind: k, icon: Icon, title, hint, min, max }) => (
                <label key={k} className={`export-layout${kind === k ? " on" : ""}${n < min || n > max ? " is-off" : ""}`}>
                  <input type="radio" name="kind" checked={kind === k} onChange={() => { setKind(k); run.reset(); }} />
                  <span><Icon size={14} /> {title}</span>
                  <code>{hint}</code>
                </label>
              ))}
            </div>
            {kind === "movie" && (
              <label className="editor-slider"><span>Each photo</span>
                <input type="range" min={1.5} max={8} step={0.5} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))}
                  aria-label="Seconds per photo" />
                <span className="tnum dim">{seconds}s</span></label>
            )}
            {!fits && <p className="danger-text">A {spec.title.toLowerCase()} takes {spec.min}–{spec.max} photos.</p>}
            {name && <p className="ok-text">Saved as {name} — it appears in your library in a moment.</p>}
            {job && job.status !== "done" && job.status !== "failed" && (
              <p className="dim">Making the movie… {job.progress_total ? `${job.progress_done} of ${job.progress_total} photos` : ""}</p>
            )}
            {job?.status === "done" && <p className="ok-text">{job.message} — it appears in your library in a moment.</p>}
            {job?.status === "failed" && <p className="danger-text">{job.error ?? job.message}</p>}
            {run.error && <p className="danger-text">{(run.error as Error).message}</p>}
            <p className="dim fix-hint">A new file in your upload folder under Creations; the photos themselves are only read.
              Movies have no soundtrack — Memoria works offline and bundles no music.</p>
          </div>
          <div className="modal-foot">
            <button className="btn btn-quiet" onClick={onClose}>Close</button>
            <button className="btn btn-primary" disabled={!fits || run.isPending || !!name || !!jobId} onClick={() => run.mutate()}>
              <Sparkles size={14} /> Make it
            </button>
          </div>
        </div>
      </div>
    </Portal>
  );
}
