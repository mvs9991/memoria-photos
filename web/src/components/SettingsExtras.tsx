/** Settings cards: access password, GPS tracks, and the photo-frame / slideshow URLs. */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Frame as FrameIcon, Lock, LogOut, Route, Upload } from "lucide-react";
import { api, randomImageUrl } from "../lib/api";
import { formatRange } from "../lib/format";
import { SectionHeader } from "./States";

export function SecurityCard() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const protectedNow = !!status.data?.protected;
  const save = useMutation({
    mutationFn: (value: string) => api.setPassword(current, value),
    onSuccess: (r) => {
      setCurrent(""); setNext(""); setAgain("");
      setNote(r.protected ? "Password saved. Other browsers must log in again." : "Password removed.");
      qc.invalidateQueries({ queryKey: ["auth"] });
    },
  });
  const logout = useMutation({ mutationFn: api.logout, onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }) });
  const mismatch = next !== again;

  return (
    <section className="card setting-card">
      <SectionHeader title="Access" sub={protectedNow
        ? "A password is required to open this library in a browser."
        : "No password: anyone who can reach this address can see every photo. Fine on this machine only; set one before serving it to your network."} />
      <form className="password-form" onSubmit={(e) => { e.preventDefault(); if (next && !mismatch) save.mutate(next); }}>
        {protectedNow && (
          <input className="field" type="password" placeholder="Current password" value={current} autoComplete="current-password"
            onChange={(e) => setCurrent(e.target.value)} aria-label="Current password" />
        )}
        <input className="field" type="password" placeholder={protectedNow ? "New password" : "Password (6+ characters)"}
          value={next} autoComplete="new-password" onChange={(e) => { setNext(e.target.value); setNote(null); }} aria-label="New password" />
        <input className="field" type="password" placeholder="Again" value={again} autoComplete="new-password"
          onChange={(e) => setAgain(e.target.value)} aria-label="Repeat new password" />
        <button className="btn btn-primary" type="submit" disabled={!next || mismatch || next.length < 6 || save.isPending}>
          <Lock size={14} /> {protectedNow ? "Change" : "Set password"}
        </button>
      </form>
      <div className="job-actions">
        {protectedNow && (
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => {
              if (confirm("Remove the password? Anyone who can reach this address will see your library.")) save.mutate("");
            }} disabled={!current} title={current ? undefined : "Enter the current password first"}>Remove password</button>
            <button className="btn btn-quiet btn-sm" onClick={() => logout.mutate()}><LogOut size={14} /> Log out</button>
          </>
        )}
      </div>
      {mismatch && again && <p className="danger-text">The two passwords differ.</p>}
      {note && <p className="dim">{note}</p>}
      {save.error && <p className="danger-text">{(save.error as Error).message}</p>}
    </section>
  );
}

export function GpxCard() {
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const tracks = useQuery({ queryKey: ["gpx-tracks"], queryFn: () => api.gpxTracks() });
  const [note, setNote] = useState<string | null>(null);
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      for (const f of files) await api.uploadGpx(f);
      return files.length;
    },
    onSuccess: (n) => {
      setNote(`Added ${n} track${n === 1 ? "" : "s"}. Use “Place photos by tracks” to place photos by them.`);
      qc.invalidateQueries({ queryKey: ["gpx-tracks"] });
    },
  });
  const place = useMutation({
    mutationFn: () => api.startJob({ kind: "index", post_only: true, stages: ["gpx", "geocode", "events", "locations", "search-index"] }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const list = tracks.data?.tracks ?? [];
  return (
    <section className="card setting-card">
      <SectionHeader title="GPS tracks" count={list.length || undefined}
        sub="A .gpx from a phone, watch or bike computer places camera photos that have no GPS, by matching times. Tracks in your photo folders are found automatically." />
      <div className="job-actions">
        <input ref={input} type="file" accept=".gpx" multiple hidden
          onChange={(e) => { const f = [...(e.target.files ?? [])]; if (f.length) upload.mutate(f); e.target.value = ""; }} />
        <button className="btn btn-ghost btn-sm" onClick={() => input.current?.click()} disabled={upload.isPending}>
          <Upload size={14} /> Add .gpx files
        </button>
        {list.length > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={() => place.mutate()} disabled={place.isPending}>
            <Route size={14} /> Place photos by tracks
          </button>
        )}
      </div>
      {list.length > 0 && (
        <ul className="gpx-list">
          {list.slice(0, 8).map((t) => (
            <li key={t.id}><Route size={13} className="dim" /> <span className="ellipsis">{t.name}</span>
              <span className="dim">{formatRange(t.start_ts, t.end_ts)} · {t.points.length.toLocaleString()} points</span></li>
          ))}
        </ul>
      )}
      {note && <p className="dim">{note}</p>}
      {(upload.error || place.error) && <p className="danger-text">{((upload.error || place.error) as Error).message}</p>}
      <p className="dim">Photos with real GPS, a Google Takeout location or a place you set are never moved. A camera clock that
        is off shifts the match — fix the time first (“Fix date” → shift) if needed.</p>
    </section>
  );
}

export function FrameCard() {
  const origin = window.location.origin;
  return (
    <section className="card setting-card">
      <SectionHeader title="Photo frame" sub="Turn a spare tablet or TV into a frame, or feed a dashboard." />
      <ul className="frame-links">
        <li><FrameIcon size={13} className="dim" /> <a className="link" href="/frame" target="_blank" rel="noreferrer">{origin}/frame</a>
          <span className="dim"> — random photos with a clock. Add <code>?album=ID</code>, <code>?person=ID</code> or <code>?rating=4</code>.</span></li>
        <li><FrameIcon size={13} className="dim" /> <code>{origin}{randomImageUrl()}</code>
          <span className="dim"> — one random photo per request, for Home Assistant and similar. Same filters.</span></li>
      </ul>
    </section>
  );
}
