/** Settings cards for running Memoria as the family's photo service: what needs attention,
 * keeping it running, HTTPS from anywhere, and the encrypted off-site copy. Owner only. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Archive, BellRing, CircleAlert, CircleCheck, Copy, Eye, EyeOff, Globe, HeartPulse, Power, RefreshCw,
} from "lucide-react";
import { api, type Problem } from "../lib/api";
import { formatBytes, relativeTime } from "../lib/format";
import { useRole } from "../lib/hooks";
import { SectionHeader } from "./States";
import { Toggle } from "./Toggle";

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e ?? ""));

/** Wait for the server to come back after a restart, then reload the page. */
async function reloadWhenBack(url?: string) {
  await new Promise((r) => setTimeout(r, 2500));
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch("/api/auth/status", { cache: "no-store" });
      if (res.ok) break;
    } catch { /* still restarting */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  if (url) window.location.assign(url);
  else window.location.reload();
}

function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, any>) => api.updateSettings(body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["alerts"] }); },
  });
}

// ---------------------------------------------------------------- health

function ProblemRow({ p, onSnooze }: { p: Problem; onSnooze: () => void }) {
  return (
    <li className={`problem problem-${p.level}`}>
      {p.level === "error" ? <CircleAlert size={16} aria-label="Problem" /> : <AlertTriangle size={16} aria-label="Warning" />}
      <div>
        <strong>{p.title}</strong>
        <p className="dim">{p.detail}</p>
        {p.fix && <p className="problem-fix">{p.fix}</p>}
      </div>
      <button className="btn btn-quiet btn-sm" onClick={onSnooze} title="Hide this for a week">Later</button>
    </li>
  );
}

export function HealthCard() {
  const qc = useQueryClient();
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: api.alerts, refetchInterval: 60_000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const save = useSaveSettings();
  const snooze = useMutation({
    mutationFn: (key: string) => api.snoozeAlert(key),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
  const test = useMutation({ mutationFn: api.testAlert });
  const [hook, setHook] = useState<string | null>(null);
  const problems = alerts.data?.problems ?? [];
  const current = settings.data?.settings?.alert_webhook ?? "";
  return (
    <section className="card setting-card" id="health">
      <SectionHeader title="Health" count={problems.length || undefined}
        sub="What quietly goes wrong when one computer holds the family's photos: drives, space, backups, phones." />
      {alerts.isLoading ? <p className="dim">Checking…</p> : problems.length === 0 ? (
        <p className="all-well"><CircleCheck size={16} /> Nothing needs attention.</p>
      ) : (
        <ul className="problem-list">
          {problems.map((p) => <ProblemRow key={p.key} p={p} onSnooze={() => snooze.mutate(p.key)} />)}
        </ul>
      )}
      {!!alerts.data?.snoozed && <p className="dim">{alerts.data.snoozed} hidden for now.</p>}
      <h3 className="setting-subhead"><BellRing size={14} /> Tell my phone</h3>
      <form className="xmp-form" onSubmit={(e) => {
        e.preventDefault();
        if (hook !== null) save.mutate({ alert_webhook: hook.trim() });
        setHook(null);
      }}>
        <input className="field" value={hook ?? current} placeholder="https://ntfy.sh/your-secret-topic (optional)"
          aria-label="Alert address" onChange={(e) => setHook(e.target.value)} />
        <button className="btn btn-ghost" type="submit" disabled={hook === null}>Save</button>
        <button className="btn btn-ghost" type="button" disabled={!current || test.isPending} onClick={() => test.mutate()}>
          Send a test
        </button>
      </form>
      {save.error && <p className="danger-text">{errorText(save.error)}</p>}
      {test.isSuccess && <p className="dim">Sent — it should arrive in a moment.</p>}
      {test.error && <p className="danger-text">{errorText(test.error)}</p>}
      <p className="dim">Off unless you set it. New problems are sent to this address — an{" "}
        <a className="link" href="https://ntfy.sh" target="_blank" rel="noreferrer">ntfy</a> topic (its phone app shows them as
        notifications; ntfy.sh is someone else's server, or run your own) or a Discord webhook. Only the words above are sent,
        never photos.</p>
    </section>
  );
}

/** Home screen: only real problems, and only for the owner. */
export function HealthNotice() {
  return useRole() === "owner" ? <OwnerHealthNotice /> : null;
}

function OwnerHealthNotice() {
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: api.alerts, staleTime: 300_000 });
  const errors = (alerts.data?.problems ?? []).filter((p) => p.level === "error");
  if (!errors.length) return null;
  return (
    <div className="notice notice-danger">
      <CircleAlert size={16} />
      <span>{errors.length === 1 ? errors[0].title : `${errors.length} things need attention`}.{" "}
        <Link to="/settings#health" className="link">See what to do</Link></span>
    </div>
  );
}

// ---------------------------------------------------------------- keep running

export function KeepRunningCard() {
  const qc = useQueryClient();
  const svc = useQuery({ queryKey: ["service"], queryFn: api.service, refetchInterval: 30_000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const save = useSaveSettings();
  const auto = useMutation({
    mutationFn: (on: boolean) => api.setAutostart(on),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["service"] }),
  });
  const restart = useMutation({ mutationFn: api.restartServer, onSuccess: () => reloadWhenBack() });
  const s = settings.data?.settings;
  const d = svc.data;
  if (!s || !d) return null;
  const network = s.serve_host === "0.0.0.0";
  return (
    <section className="card setting-card" id="keep-running">
      <SectionHeader title="Keep Memoria running" sub="So phones can reach it, and backups happen, without anyone starting it." />
      <p className={d.supervised ? "dim" : "warn-text"}>
        <Power size={14} />{" "}
        {d.supervised
          ? <>Running under its keeper{d.keeper.started_at ? ` since ${relativeTime(d.keeper.started_at)}` : ""}: if it
            stops, it starts again by itself{d.keeper.restarts_today ? ` (${d.keeper.restarts_today} ${d.keeper.restarts_today === 1 ? "restart" : "restarts"} today)` : ""}.</>
          : <>Started by hand: it stops when that window closes. Turn on starting with the computer below.</>}
      </p>
      <Toggle label="Start with this computer" checked={d.autostart.sign_in || d.autostart.boot} disabled={auto.isPending}
        hint={d.autostart.boot ? "Starts at boot, before anyone signs in (Task Scheduler)."
          : "Starts when you sign in. To start before anyone signs in, run  python -m photointel autostart on --at-boot  in an administrator terminal."}
        onChange={(on) => auto.mutate(on)} />
      {auto.error && <p className="danger-text">{errorText(auto.error)}</p>}
      {d.keep_awake_supported && (
        <Toggle label="Keep this computer awake" checked={!!s.keep_awake} onChange={(v) => save.mutate({ keep_awake: v })}
          hint="While Memoria runs, the computer does not go to sleep (the screen still turns off). A sleeping computer can't serve phones or back up." />
      )}
      <Toggle label="Phones on the home network can connect" checked={network}
        onChange={(v) => save.mutate({ serve_host: v ? "0.0.0.0" : "127.0.0.1" })}
        hint={network ? `Listening on port ${s.serve_port}. Takes effect after a restart.`
          : "Off: only this computer can open Memoria. Needs a password or accounts first. Takes effect after a restart."} />
      {save.error && <p className="danger-text">{errorText(save.error)}</p>}
      {d.supervised && (
        <button className="btn btn-ghost btn-sm" disabled={restart.isPending} onClick={() => restart.mutate()}>
          <RefreshCw size={14} className={restart.isPending ? "spin" : ""} /> {restart.isPending ? "Restarting…" : "Restart Memoria"}
        </button>
      )}
      {restart.error && <p className="danger-text">{errorText(restart.error)}</p>}
    </section>
  );
}

// ---------------------------------------------------------------- access from anywhere (HTTPS)

export function AccessCard() {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["https"], queryFn: api.https });
  const setup = useMutation({
    mutationFn: api.httpsSetup,
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["https"] }); if (r.restarting) reloadWhenBack(); },
  });
  const off = useMutation({
    mutationFn: api.httpsOff,
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["https"] }); if (r.restarting) reloadWhenBack(); },
  });
  const d = st.data;
  const ts = d?.tailscale ?? {};
  const [copied, setCopied] = useState(false);
  return (
    <section className="card setting-card" id="access">
      <SectionHeader title="Access from anywhere"
        sub="A private, encrypted address for your own devices — at home or away — without opening your router. Also what the phone's offline copy needs." />
      {!d ? <p className="dim">Checking…</p> : d.enabled && d.url ? (
        <>
          <div className="backup-app-url">
            <code>{d.url}</code>
            <button className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(d.url).then(() => setCopied(true))}>
              <Copy size={14} /> {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="dim">Open this on each phone that has Tailscale, then "Add to Home Screen".
            {d.cert && <> Certificate valid until {new Date(d.cert.not_after * 1000).toLocaleDateString()} — renewed by itself.</>}
            {!d.supervised && " Restart Memoria for a change here to take effect."}</p>
          <button className="btn btn-quiet btn-sm" disabled={off.isPending} onClick={() => off.mutate()}>Turn HTTPS off</button>
        </>
      ) : (
        <ol className="setup-steps">
          <li className={ts.installed ? "done" : ""}>
            Install <a className="link" href="https://tailscale.com/download" target="_blank" rel="noreferrer">Tailscale</a> on
            this computer and on each phone (free for personal use), and sign in to the same account on all of them.
          </li>
          <li className={ts.running ? "done" : ""}>Sign in to Tailscale on this computer{ts.name ? ` (it is ${ts.name})` : ""}.</li>
          <li className={ts.https_ready ? "done" : ""}>
            In the Tailscale admin console, under DNS, turn on <strong>MagicDNS</strong> and <strong>HTTPS certificates</strong>.
          </li>
          <li className={d.protected ? "done" : ""}>Set a password or turn on accounts (Security, above).</li>
          <li>
            <button className="btn btn-primary btn-sm" disabled={!ts.https_ready || !d.protected || setup.isPending}
              onClick={() => setup.mutate()}>
              <Globe size={14} /> {setup.isPending ? "Getting the certificate…" : "Get the certificate and turn on HTTPS"}
            </button>
          </li>
        </ol>
      )}
      {(setup.error || off.error) && <p className="danger-text">{errorText(setup.error || off.error)}</p>}
      {setup.data && !setup.data.restarting && setup.data.url && (
        <p className="dim">Done. Restart Memoria, then open {setup.data.url}.</p>
      )}
      <p className="dim">Tailscale makes a private network of your own devices; nothing is reachable from the internet. Plain
        http on the home Wi-Fi keeps working beside it.</p>
    </section>
  );
}

// ---------------------------------------------------------------- the off-site copy

export function OffsiteCard() {
  const qc = useQueryClient();
  const st = useQuery({
    queryKey: ["offsite"], queryFn: api.offsite,
    refetchInterval: (q) => (q.state.data?.running || q.state.data?.checking ? 3000 : 30_000),
  });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const save = useSaveSettings();
  const [repo, setRepo] = useState("");
  const [own, setOwn] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const [made, setMade] = useState<string | null>(null);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["offsite"] }); qc.invalidateQueries({ queryKey: ["alerts"] }); };
  const setup = useMutation({
    mutationFn: () => api.offsiteSetup(repo.trim(), own),
    onSuccess: (r) => { setMade(r.password); setRepo(""); setOwn(""); refresh(); qc.invalidateQueries({ queryKey: ["settings"] }); },
  });
  const run = useMutation({ mutationFn: api.offsiteRun, onSuccess: refresh });
  const verify = useMutation({ mutationFn: api.offsiteVerify, onSuccess: refresh });
  const reveal = useMutation({ mutationFn: api.offsitePassword, onSuccess: (r) => setShown(r.password) });
  const d = st.data;
  const s = settings.data?.settings;
  if (!d || !s) return null;
  const last = d.last;
  return (
    <section className="card setting-card" id="offsite">
      <SectionHeader title="Off-site copy"
        sub="The backup drive sits next to this computer; a fire, flood or burglary can take both. This keeps an encrypted copy somewhere else." />
      {made && (
        <div className="password-reveal">
          <p><strong>Write this password down now</strong> and keep it away from this computer — with a relative, in a
            password manager, on paper in a drawer at work. Without it the copy can never be opened; the copy of it on this
            computer burns with the house.</p>
          <code className="password-code">{made}</code>
          <button className="btn btn-primary btn-sm" onClick={() => setMade(null)}>I have written it down</button>
        </div>
      )}
      {!d.restic ? (
        <div className="notice notice-quiet">
          <Archive size={16} />
          <span>The copy is made by restic, a free, open-source backup program that can also restore without Memoria.
            Install it on this computer: <code>{d.install_hint}</code>, then reload this page.</span>
        </div>
      ) : !d.repo ? (
        <form className="offsite-setup" onSubmit={(e) => { e.preventDefault(); if (repo.trim()) setup.mutate(); }}>
          <label className="dim">Where should the copy go?
            <input className="field" value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Where the copy goes"
              placeholder="F:\Memoria off-site   or   sftp:me@relatives-pc:/backups/memoria" />
          </label>
          <label className="dim">Password (leave empty and Memoria makes a strong one for you)
            <input className="field" type="password" value={own} autoComplete="new-password" onChange={(e) => setOwn(e.target.value)}
              aria-label="Password for the copy" placeholder="at least 12 characters" />
          </label>
          <button className="btn btn-primary" type="submit" disabled={!repo.trim() || setup.isPending}>
            {setup.isPending ? "Setting up…" : "Set up the off-site copy"}
          </button>
          {setup.error && <p className="danger-text">{errorText(setup.error)}</p>}
          <p className="dim">A drive you keep at work or with family (bring it home now and then), a relative's computer over SSH,
            or — if you choose — a cloud bucket: it is encrypted before it leaves, so they see only noise.</p>
        </form>
      ) : (
        <>
          <p className="dim">Copying to <code>{d.repo}</code>.</p>
          {d.running ? (
            <p className="dim"><RefreshCw size={14} className="spin" /> {d.running.message || "Copying…"}</p>
          ) : last ? (
            <p className="dim">Last copy {relativeTime(last.finished_at)}: {last.files.toLocaleString()} files,{" "}
              {last.files_new.toLocaleString()} new, {formatBytes(last.bytes_added)} added
              {last.unreadable?.length ? `; ${last.unreadable.length} could not be read` : ""}.</p>
          ) : <p className="dim">No copy made yet.</p>}
          {d.last_check && (
            <p className={d.last_check.ok ? "dim" : "danger-text"}>
              {d.last_check.ok ? <CircleCheck size={14} /> : <CircleAlert size={14} />}{" "}
              {d.last_check.ok ? `Read back and checked ${relativeTime(d.last_check.at)} (${d.last_check.subset} of it).`
                : `The last check failed: ${d.last_check.detail}`}
            </p>
          )}
          <div className="job-actions">
            <button className="btn btn-primary btn-sm" disabled={!!d.running || run.isPending} onClick={() => run.mutate()}>
              <Archive size={14} /> Copy now
            </button>
            <button className="btn btn-ghost btn-sm" disabled={d.checking || verify.isPending || !last} onClick={() => verify.mutate()}>
              <HeartPulse size={14} /> {d.checking ? "Checking…" : "Check the copy"}
            </button>
            <button className="btn btn-quiet btn-sm" onClick={() => (shown ? setShown(null) : reveal.mutate())}>
              {shown ? <EyeOff size={14} /> : <Eye size={14} />} {shown ? "Hide password" : "Show password"}
            </button>
            <label className="dim">copy
              <select className="field field-sm" value={s.offsite_every_days} aria-label="How often to copy"
                onChange={(e) => save.mutate({ offsite_every_days: Number(e.target.value) })}>
                <option value={0}>only when I press the button</option>
                <option value={1}>every day</option>
                <option value={7}>every week</option>
                <option value={30}>every month</option>
              </select>
            </label>
          </div>
          {shown && <code className="password-code">{shown}</code>}
          {(run.error || verify.error) && <p className="danger-text">{errorText(run.error || verify.error)}</p>}
          <p className="dim">Never pruned: every earlier copy is kept, so a photo deleted here can still be brought back. To
            restore after a disaster, on any computer with restic and this password:{" "}
            <code>python -m photointel offsite restore &lt;an empty folder&gt;</code> (or restic itself).</p>
        </>
      )}
    </section>
  );
}
