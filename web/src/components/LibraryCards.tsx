/** Settings cards: looking for new photos on a schedule, the upload folder, and backup. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HardDriveDownload } from "lucide-react";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useRole } from "../lib/hooks";
import { SectionHeader } from "./States";

const EVERY: [number, string][] = [[0, "only when I press Index"], [15, "every 15 minutes"], [60, "every hour"],
  [360, "every 6 hours"], [1440, "once a day"]];

export function ScheduleCard() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const save = useMutation({
    mutationFn: (body: Record<string, any>) => api.updateSettings(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
  const s = settings.data?.settings;
  const [folder, setFolder] = useState<string | null>(null);
  if (!s) return null;
  return (
    <section className="card setting-card">
      <SectionHeader title="New photos"
        sub="While Memoria is running it looks for new and changed photos by itself; only what is new is analysed." />
      <div className="trash-settings">
        <label className="dim">look for new photos
          <select className="field field-sm" value={s.auto_index_minutes} aria-label="How often to look for new photos"
            onChange={(e) => save.mutate({ auto_index_minutes: Number(e.target.value) })}>
            {EVERY.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            {!EVERY.some(([v]) => v === s.auto_index_minutes) && (
              <option value={s.auto_index_minutes}>every {s.auto_index_minutes} min</option>
            )}
          </select>
        </label>
      </div>
      <form className="xmp-form" onSubmit={(e) => {
        e.preventDefault();
        if (folder !== null) save.mutate({ upload_folder: folder.trim() });
        setFolder(null);
      }}>
        <input className="field" value={folder ?? s.upload_folder}
          placeholder={`${settings.data?.data_dir ?? "data"}\\uploads (default)`}
          onChange={(e) => setFolder(e.target.value)} aria-label="Upload folder" />
        <button className="btn btn-ghost" type="submit" disabled={folder === null}>Save</button>
      </form>
      <p className="dim">Photos uploaded from a phone are stored in this folder, sorted by the date they were taken. Point it
        at your main photo drive to keep everything together. <Link to="/upload" className="link">Upload now</Link></p>
    </section>
  );
}

export function BackupCard() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const status = useQuery({ queryKey: ["backup"], queryFn: api.backupStatus, refetchInterval: 15_000 });
  const [folder, setFolder] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: Record<string, any>) => api.updateSettings(body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["backup"] }); },
  });
  const run = useMutation({
    mutationFn: () => api.startBackup(folder ?? undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const s = settings.data?.settings;
  if (!s) return null;
  const last = status.data?.last;
  const target = folder ?? s.backup_folder;
  return (
    <section className="card setting-card" id="backup">
      <SectionHeader title="Backup"
        sub="Without a cloud, this computer holds the only copy of your photos. Copy them to a second drive — an external disk or a NAS." />
      <form className="xmp-form" onSubmit={(e) => {
        e.preventDefault();
        save.mutate({ backup_folder: (folder ?? "").trim() });
        setFolder(null);
      }}>
        <input className="field" value={target} placeholder="e.g. E:\ (an external drive)" aria-label="Backup folder"
          onChange={(e) => setFolder(e.target.value)} />
        <button className="btn btn-ghost" type="submit" disabled={folder === null}>Save</button>
        <button className="btn btn-primary" type="button" disabled={!target || run.isPending} onClick={() => run.mutate()}>
          <HardDriveDownload size={14} /> Back up now
        </button>
      </form>
      <div className="trash-settings">
        <label className="dim">while Memoria runs, back up
          <select className="field field-sm" value={s.backup_every_days} aria-label="How often to back up"
            onChange={(e) => save.mutate({ backup_every_days: Number(e.target.value) })}>
            <option value={0}>only when I press the button</option>
            <option value={1}>every day</option>
            <option value={7}>every week</option>
            <option value={30}>every month</option>
          </select>
        </label>
      </div>
      {last ? (
        <p className={last.failed || last.hash_mismatches?.length ? "danger-text" : "dim"}>
          Last backup {relativeTime(last.finished_at)}: {last.files.toLocaleString()} files checked,{" "}
          {last.copied.toLocaleString()} copied{last.failed ? `, ${last.failed} failed` : ""}
          {last.hash_mismatches?.length
            ? ` — ${last.hash_mismatches.length} files did not match the copy Memoria indexed; check that disk` : ""}.
        </p>
      ) : <p className="dim">No backup yet.</p>}
      {run.error && <p className="danger-text">{(run.error as Error).message}</p>}
      <p className="dim">Copy-only: new and changed files are added; nothing in the backup is ever deleted, so a photo you
        delete here is still there. Memoria’s own database — people, albums, corrections — is saved alongside.</p>
    </section>
  );
}

/** A reminder on the home screen while there is no recent backup (dismissable for two weeks). */
export function BackupReminder() {
  return useRole() === "owner" ? <OwnerBackupReminder /> : null;
}

function OwnerBackupReminder() {
  const status = useQuery({ queryKey: ["backup"], queryFn: api.backupStatus, staleTime: 300_000 });
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats });
  const [hidden, setHidden] = useState(() => {
    try { return Number(localStorage.getItem("backup-reminder-until") || 0) > Date.now(); } catch { return false; }
  });
  if (hidden || !status.data || !stats.data?.photos) return null;
  const last = status.data.last?.finished_at as number | undefined;
  if (last && Date.now() / 1000 - last < 30 * 86400) return null;
  return (
    <div className="notice backup-reminder">
      <HardDriveDownload size={16} />
      <span>{last ? `Your last backup was ${relativeTime(last)}.` : "Your photos are only on this computer."}{" "}
        <Link to="/settings#backup" className="link">Set up a backup to another drive</Link></span>
      <button className="btn btn-quiet btn-sm" onClick={() => {
        try { localStorage.setItem("backup-reminder-until", String(Date.now() + 14 * 86400_000)); } catch { /* private mode */ }
        setHidden(true);
      }}>Later</button>
    </div>
  );
}
