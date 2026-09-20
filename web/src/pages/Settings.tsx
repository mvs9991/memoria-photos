import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Check, ChevronRight, Cpu, Database, FolderPlus, HardDrive, Loader2, Play, RefreshCw,
  ShieldCheck, Square, Trash2, X,
} from "lucide-react";
import { api } from "../lib/api";
import { ErrorState, SectionHeader, Spinner } from "../components/States";
import { formatBytes, formatDuration, relativeTime } from "../lib/format";
import { useTitle } from "../lib/hooks";

export default function Settings() {
  useTitle("Settings");
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 20_000 });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
    refetchInterval: (q) => (q.state.data?.active ? 1200 : 10_000),
  });
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const errors = useQuery({ queryKey: ["errors"], queryFn: api.errors });
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats });
  const [picker, setPicker] = useState(false);

  const update = useMutation({
    mutationFn: (body: any) => api.updateSettings(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
  const startJob = useMutation({
    mutationFn: (body: any) => api.startJob(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const cancelJob = useMutation({
    mutationFn: (id: number) => api.cancelJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const removeRoot = useMutation({
    mutationFn: (id: number) => api.removeRoot(id),
    onSuccess: () => qc.invalidateQueries(),
  });
  const clearCache = useMutation({
    mutationFn: (kind: string) => api.clearCache(kind),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["health"] }),
  });

  if (settings.isError) return <ErrorState error={settings.error} onRetry={() => settings.refetch()} />;
  if (settings.isLoading || !settings.data) return <Spinner full label="Loading settings" />;

  const s = settings.data.settings;
  const active = jobs.data?.active;
  const pct = active?.progress_total ? Math.round((active.progress_done / active.progress_total) * 100) : null;

  return (
    <div className="page settings-page">
      <div className="page-head">
        <div>
          <h1 className="display">Settings</h1>
          <p className="dim">Everything runs on this machine. Data lives in {settings.data.data_dir}</p>
        </div>
      </div>

      <section className="card setting-card">
        <SectionHeader title="Photo folders" sub="Memoria reads these folders and never writes to them." />
        <div className="root-list">
          {settings.data.roots.map((r: any) => (
            <div key={r.id} className="root-row">
              <HardDrive size={16} className="dim" />
              <div className="root-info">
                <code>{r.path}</code>
                <span className="dim">last scanned {relativeTime(r.last_scan_at)}</span>
              </div>
              <button className="btn btn-quiet btn-sm" onClick={() => {
                if (confirm(`Stop tracking ${r.path}?\n\nThis removes its photos from the Memoria index. Your files are not touched.`))
                  removeRoot.mutate(r.id);
              }}>
                <Trash2 size={14} /> Remove
              </button>
            </div>
          ))}
          {settings.data.roots.length === 0 && (
            <p className="dim">No folders yet. Add one to start indexing.</p>
          )}
        </div>
        <button className="btn btn-ghost" onClick={() => setPicker(true)}>
          <FolderPlus size={15} /> Add folder
        </button>
        {picker && <FolderPicker onClose={() => setPicker(false)} onPicked={() => {
          setPicker(false);
          qc.invalidateQueries({ queryKey: ["settings"] });
        }} />}
      </section>

      <section className="card setting-card">
        <SectionHeader title="Indexing" sub="Scan for new photos and analyse them locally." />
        {active ? (
          <div className="job-active">
            <div className="job-active-head">
              <Loader2 size={16} className="spin" />
              <strong>{active.stage ?? "Working"}</strong>
              <span className="dim">{active.message}</span>
            </div>
            {pct !== null && (
              <div className="progress">
                <span style={{ width: `${pct}%` }} />
                <em className="tnum">{active.progress_done.toLocaleString()} / {active.progress_total.toLocaleString()}</em>
              </div>
            )}
            <button className="btn btn-danger btn-sm" onClick={() => cancelJob.mutate(active.id)}>
              <Square size={13} /> Stop (progress is kept)
            </button>
          </div>
        ) : (
          <div className="job-actions">
            <button className="btn btn-primary" onClick={() => startJob.mutate({ kind: "index" })}>
              <Play size={15} /> Scan &amp; index new photos
            </button>
            <button className="btn btn-ghost" onClick={() => startJob.mutate({ kind: "index", post_only: true })}>
              <RefreshCw size={15} /> Rebuild people, events &amp; duplicates
            </button>
            <button className="btn btn-ghost" onClick={() => startJob.mutate({ kind: "index", retry_errors: true })}>
              Retry failed photos
            </button>
            <button className="btn btn-ghost" onClick={() => startJob.mutate({ kind: "caption", limit: 500 })}
              title="Runs a local vision model over your best photos (about 2 photos per second)">
              Describe photos locally
            </button>
          </div>
        )}
        {stats.data && (
          <div className="stat-row">
            <Stat label="Indexed" value={stats.data.photos.toLocaleString()} />
            <Stat label="Pending" value={stats.data.pending.toLocaleString()} />
            <Stat label="Errors" value={stats.data.errors.toLocaleString()} />
            <Stat label="Missing files" value={stats.data.missing.toLocaleString()} />
            <Stat label="Faces" value={stats.data.faces.toLocaleString()} />
          </div>
        )}
        {(jobs.data?.jobs ?? []).length > 0 && (
          <details className="job-history">
            <summary className="dim">Recent jobs</summary>
            <table className="mini-table">
              <tbody>
                {jobs.data.jobs.slice(0, 8).map((j: any) => (
                  <tr key={j.id}>
                    <td className="tnum dim">#{j.id}</td>
                    <td>{j.kind}</td>
                    <td><span className={`chip chip-sm chip-${j.status}`}>{j.status}</span></td>
                    <td className="dim">{relativeTime(j.finished_at ?? j.created_at)}</td>
                    <td className="dim tnum">
                      {j.started_at && j.finished_at ? formatDuration(j.finished_at - j.started_at) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </section>

      <section className="card setting-card">
        <SectionHeader title="Privacy" sub="Nothing leaves this machine unless you switch it on here." />
        <Toggle
          label="Detailed online map tiles"
          hint="Loads street maps from OpenStreetMap. That tells their servers which areas you are looking at."
          checked={!!s.allow_online_map_tiles}
          onChange={(v) => update.mutate({ allow_online_map_tiles: v })}
        />
        <Toggle
          label="Use Claude for complex searches"
          hint="Sends only your search text plus the names of people/places in your library — never photos."
          checked={!!s.llm_enabled}
          onChange={(v) => update.mutate({ llm_enabled: v })}
        />
        {s.llm_enabled && (
          <div className="setting-row">
            <label className="setting-label">Anthropic API key</label>
            <input className="field" type="password" placeholder={s.anthropic_api_key ? "•••••••• (saved)" : "sk-ant-…"}
              onBlur={(e) => e.target.value && update.mutate({ anthropic_api_key: e.target.value })} />
          </div>
        )}
        <Toggle
          label="Allow sending images to Claude"
          hint="Off by default. Only affects the optional description feature, and only for photos you ask about."
          checked={!!s.llm_send_images}
          onChange={(v) => update.mutate({ llm_send_images: v })}
          disabled={!s.llm_enabled}
        />
        <p className="privacy-note">
          <ShieldCheck size={14} /> Face recognition, search and grouping all run locally with open models.
          Your photos are never uploaded, modified or deleted.
        </p>
      </section>

      <section className="card setting-card">
        <SectionHeader title="Models" sub="What is doing the understanding, and which version produced your data." />
        <table className="mini-table">
          <thead>
            <tr><th>Kind</th><th>Model</th><th>Version</th><th>Dim</th><th>Outputs</th><th /></tr>
          </thead>
          <tbody>
            {(models.data?.models ?? []).map((m: any) => (
              <tr key={m.id}>
                <td>{m.kind}</td>
                <td className="ellipsis" title={m.name}>{m.name}</td>
                <td className="dim">{m.version}</td>
                <td className="dim tnum">{m.dim ?? "—"}</td>
                <td className="tnum">{m.usage.toLocaleString()}</td>
                <td>{m.active && <span className="chip chip-sm chip-accent">active</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="dim model-note">
          <Cpu size={13} /> Running on {models.data?.device === "cuda" ? "GPU (CUDA)" : "CPU"}.
          Embeddings from different models are never mixed — changing a model re-analyses the affected photos.
        </div>
      </section>

      <section className="card setting-card">
        <SectionHeader title="Storage" />
        {health.data && (
          <div className="stat-row">
            <Stat label="Database" value={formatBytes(health.data.db_bytes)} />
            <Stat label="Thumbnail cache" value={formatBytes(health.data.cache_bytes)} />
            <Stat label="Free space" value={formatBytes(health.data.free_bytes)} />
          </div>
        )}
        <div className="job-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => clearCache.mutate("previews")}>
            Clear preview cache
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => clearCache.mutate("faces")}>
            Clear face crop cache
          </button>
        </div>
        <p className="dim">Caches regenerate automatically. Your originals are never touched.</p>
      </section>

      {(errors.data?.total ?? 0) > 0 && (
        <section className="card setting-card">
          <SectionHeader title="Problem files" count={errors.data.total}
            sub="Files that could not be read. They stay listed so nothing silently disappears." />
          <table className="mini-table">
            <tbody>
              {errors.data.errors.slice(0, 12).map((e: any) => (
                <tr key={e.id}>
                  <td className="ellipsis" style={{ maxWidth: 280 }} title={e.path}>{e.filename ?? e.path}</td>
                  <td className="dim">{e.stage}</td>
                  <td className="dim ellipsis" style={{ maxWidth: 360 }} title={e.error}>{e.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-cell">
      <span className="stat-cell-value tnum">{value}</span>
      <span className="dim">{label}</span>
    </div>
  );
}

function Toggle({ label, hint, checked, onChange, disabled }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <label className={`toggle-row${disabled ? " is-disabled" : ""}`}>
      <span className="toggle-text">
        <span className="setting-label">{label}</span>
        {hint && <span className="dim toggle-hint">{hint}</span>}
      </span>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        className={`switch${checked ? " on" : ""}`} onClick={() => onChange(!checked)}>
        <span />
      </button>
    </label>
  );
}

function FolderPicker({ onClose, onPicked }: { onClose: () => void; onPicked: () => void }) {
  const [path, setPath] = useState<string | undefined>(undefined);
  const { data, isLoading } = useQuery({ queryKey: ["browse", path], queryFn: () => api.browse(path) });
  const add = useMutation({
    mutationFn: (p: string) => api.addRoot(p),
    onSuccess: onPicked,
  });

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>Choose a photo folder</h3>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="modal-path">
          <code>{data?.path ?? "This computer"}</code>
          {data?.parent && (
            <button className="btn btn-quiet btn-sm" onClick={() => setPath(data.parent)}>Up</button>
          )}
        </div>
        <div className="modal-body">
          {isLoading ? <Spinner /> : (
            <ul className="browse-list">
              {(data?.entries ?? []).map((e: any) => (
                <li key={e.path}>
                  <button className="browse-row" onClick={() => setPath(e.path)}>
                    <span className="ellipsis">{e.name}</span>
                    <ChevronRight size={14} className="dim" />
                  </button>
                </li>
              ))}
              {(data?.entries ?? []).length === 0 && <li className="dim" style={{ padding: 12 }}>No subfolders</li>}
            </ul>
          )}
        </div>
        <div className="modal-foot">
          {add.isError && (
            <span className="danger-text"><AlertTriangle size={13} /> {(add.error as Error).message}</span>
          )}
          <button className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={!data?.path || add.isPending}
            onClick={() => data?.path && add.mutate(data.path)}>
            <Check size={15} /> Use this folder
          </button>
        </div>
      </div>
    </div>
  );
}
