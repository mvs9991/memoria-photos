/**
 * Read-only links to one album, and export of an album as a static web gallery.
 *
 * A link opens that album and nothing else — no search, people, places or other
 * albums. It only works for people who can reach this computer, so the dialog says
 * so rather than pretending it is a public URL.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Download, FolderOutput, Link2, Trash2, X } from "lucide-react";
import { Portal } from "./Portal";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";

const EXPIRY: [string, number | null][] = [["never", null], ["1 day", 1], ["1 week", 7], ["30 days", 30]];

export function ShareDialog({ albumId, albumName, onClose }: { albumId: number; albumName: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [download, setDownload] = useState(false);
  const [contribute, setContribute] = useState(false);
  const [expiry, setExpiry] = useState<number | null>(7);
  const [copied, setCopied] = useState<string | null>(null);
  const shares = useQuery({ queryKey: ["shares", albumId], queryFn: () => api.shares(albumId) });
  const create = useMutation({
    mutationFn: () => api.shareAlbum(albumId, download, expiry, contribute),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["shares", albumId] });
      copy(s.token);
    },
  });
  const revoke = useMutation({
    mutationFn: (token: string) => api.revokeShare(token),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["shares", albumId] }),
  });

  const urlFor = (token: string) => `${window.location.origin}/s/${token}`;
  const copy = (token: string) => {
    navigator.clipboard?.writeText(urlFor(token)).then(() => setCopied(token), () => setCopied(null));
  };
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

  return (
    <Portal>
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Share album">
          <div className="modal-head">
            <h3><Link2 size={16} /> Share “{albumName}”</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <div className="modal-body share-body">
            <p className="dim">
              Anyone with the link can view this album — and only this album. It works for people who can reach
              this computer{local ? "; Memoria is only listening on this machine right now, so start it with " : "."}
              {local && <code>serve --host 0.0.0.0</code>}{local ? " (with a password set) to share on your network." : ""}
            </p>
            <div className="share-options">
              <label className="xmp-auto">
                <input type="checkbox" checked={download} onChange={(e) => setDownload(e.target.checked)} />
                allow downloading originals
              </label>
              <label className="xmp-auto" title="Visitors can add their photos to this album (smart albums excluded)">
                <input type="checkbox" checked={contribute} onChange={(e) => setContribute(e.target.checked)} />
                let people add photos
              </label>
              <label className="dim">expires
                <select className="field field-sm" value={expiry ?? ""} aria-label="Link expiry"
                  onChange={(e) => setExpiry(e.target.value ? Number(e.target.value) : null)}>
                  {EXPIRY.map(([label, d]) => <option key={label} value={d ?? ""}>{label}</option>)}
                </select>
              </label>
              <button className="btn btn-primary btn-sm" onClick={() => create.mutate()} disabled={create.isPending}>
                <Link2 size={14} /> Create link
              </button>
            </div>
            {(shares.data?.shares.length ?? 0) > 0 && (
              <ul className="share-list">
                {shares.data!.shares.map((s) => {
                  const expired = s.expires_at !== null && s.expires_at * 1000 < Date.now();
                  return (
                    <li key={s.token} className={expired ? "is-expired" : ""}>
                      <code className="ellipsis" title={urlFor(s.token)}>{urlFor(s.token)}</code>
                      <span className="dim share-meta">
                        {s.allow_download ? <><Download size={11} /> downloads · </> : ""}
                        {s.allow_upload ? "adds photos · " : ""}
                        {expired ? "expired" : s.expires_at ? `until ${new Date(s.expires_at * 1000).toLocaleDateString()}` : "no expiry"}
                        {s.last_used_at ? ` · opened ${relativeTime(s.last_used_at)}` : " · not opened yet"}
                      </span>
                      <button className="btn btn-quiet btn-icon btn-sm" onClick={() => copy(s.token)} aria-label="Copy link">
                        {copied === s.token ? <Check size={14} /> : <Copy size={14} />}
                      </button>
                      <button className="btn btn-quiet btn-icon btn-sm" onClick={() => revoke.mutate(s.token)}
                        aria-label="Revoke link" title="Revoke — the link stops working at once"><Trash2 size={14} /></button>
                    </li>
                  );
                })}
              </ul>
            )}
            {create.error && <p className="danger-text">{(create.error as Error).message}</p>}
          </div>
        </div>
      </div>
    </Portal>
  );
}

export function HtmlExportDialog({ albumId, albumName, onClose }: { albumId: number; albumName: string; onClose: () => void }) {
  const [folder, setFolder] = useState("");
  const run = useMutation({ mutationFn: () => api.exportAlbumHtml(albumId, folder.trim()) });
  return (
    <Portal>
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Export album as a web page">
          <div className="modal-head">
            <h3><FolderOutput size={16} /> Export “{albumName}” as a web gallery</h3>
            <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
          </div>
          <form className="modal-body fix-body" onSubmit={(e) => { e.preventDefault(); if (folder.trim()) run.mutate(); }}>
            <p className="dim">
              A folder with an <code>index.html</code>, resized photos and playable videos. It opens offline in any
              browser, and can be copied to a USB stick or uploaded to any web host.
            </p>
            <input className="field" autoFocus placeholder="Export folder, e.g. D:\Exports\Goa trip" value={folder}
              onChange={(e) => setFolder(e.target.value)} aria-label="Export folder" />
            <p className="dim fix-hint">Must be outside your photo folders. Your originals are only read.</p>
            {run.data && (
              <p>Exported {run.data.exported.toLocaleString()} items to <code>{run.data.folder}</code>
                {run.data.skipped ? ` (${run.data.skipped} could not be read)` : ""}.</p>
            )}
            {run.error && <p className="danger-text">{(run.error as Error).message}</p>}
            <div className="modal-foot" style={{ padding: 0 }}>
              <button type="button" className="btn btn-quiet" onClick={onClose}>{run.data ? "Done" : "Cancel"}</button>
              <button type="submit" className="btn btn-primary" disabled={!folder.trim() || run.isPending}>
                {run.isPending ? "Exporting…" : "Export"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Portal>
  );
}
