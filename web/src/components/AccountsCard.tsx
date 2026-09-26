/** Settings: family accounts (the owner) and one's own password (everyone). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LogOut, UserPlus, Users } from "lucide-react";
import { api, type Role } from "../lib/api";
import { relativeTime } from "../lib/format";
import { SectionHeader } from "./States";

const ROLE_HELP: Record<Role, string> = {
  owner: "everything, including settings, deleting and the Locked folder",
  family: "browse, upload, albums, favourites, downloads — no deleting or settings",
  guest: "look and download only",
};

export function AccountsCard() {
  const qc = useQueryClient();
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  const list = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [role, setRole] = useState<Role>("family");
  const refresh = () => { qc.invalidateQueries({ queryKey: ["accounts"] }); qc.invalidateQueries({ queryKey: ["auth"] }); };
  const enable = useMutation({ mutationFn: () => api.enableAccounts(owner.trim()), onSuccess: refresh });
  const add = useMutation({
    mutationFn: () => api.createAccount(name.trim(), pw, role),
    onSuccess: () => { setName(""); setPw(""); refresh(); },
  });
  const change = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { role?: Role; disabled?: boolean } }) => api.updateAccount(id, body),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (id: number) => api.removeAccount(id), onSuccess: refresh });
  const err = (enable.error || add.error || change.error || remove.error) as Error | null;
  if (!list.data || !auth.data) return null;

  return (
    <section className="card setting-card">
      <SectionHeader title="People who can sign in"
        sub="One shared library, a login for each person. Each role decides what they can do." />
      {!list.data.enabled ? (
        <>
          <p className="dim">Accounts are off: whoever knows the library password can do everything.
            {auth.data.protected ? "" : " Set a password under Access first."}</p>
          <form className="xmp-form" onSubmit={(e) => { e.preventDefault(); if (owner.trim()) enable.mutate(); }}>
            <input className="field" placeholder="Your name (you become the owner)" value={owner}
              onChange={(e) => setOwner(e.target.value)} aria-label="Your name" disabled={!auth.data.protected} />
            <button className="btn btn-primary" type="submit" disabled={!owner.trim() || !auth.data.protected}>
              <Users size={14} /> Turn on accounts
            </button>
          </form>
          <p className="dim">You keep the current password; family members get their own.</p>
        </>
      ) : (
        <>
          <ul className="account-list">
            {list.data.accounts.map((a) => (
              <li key={a.id} className={a.disabled ? "is-off" : ""}>
                <span className="account-name">{a.username}{auth.data.user?.id === a.id ? " (you)" : ""}</span>
                <select className="field field-sm" value={a.role} aria-label={`Role of ${a.username}`}
                  onChange={(e) => change.mutate({ id: a.id, body: { role: e.target.value as Role } })}>
                  <option value="owner">owner</option>
                  <option value="family">family</option>
                  <option value="guest">guest</option>
                </select>
                <span className="dim account-seen">{a.last_login_at ? `seen ${relativeTime(a.last_login_at)}` : "never signed in"}</span>
                {auth.data.user?.id !== a.id && (
                  <>
                    <button className="btn btn-quiet btn-sm" onClick={() => change.mutate({ id: a.id, body: { disabled: !a.disabled } })}>
                      {a.disabled ? "Turn on" : "Turn off"}
                    </button>
                    <button className="btn btn-quiet btn-sm" onClick={() => {
                      if (confirm(`Remove ${a.username}'s account? Their photos stay in the library.`)) remove.mutate(a.id);
                    }}>Remove</button>
                  </>
                )}
              </li>
            ))}
          </ul>
          <form className="password-form" onSubmit={(e) => { e.preventDefault(); if (name.trim() && pw.length >= 6) add.mutate(); }}>
            <input className="field" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} aria-label="New person's name" />
            <input className="field" type="password" placeholder="Password (6+)" value={pw} autoComplete="new-password"
              onChange={(e) => setPw(e.target.value)} aria-label="New person's password" />
            <select className="field" value={role} onChange={(e) => setRole(e.target.value as Role)} aria-label="New person's role">
              <option value="family">family</option>
              <option value="guest">guest</option>
              <option value="owner">owner</option>
            </select>
            <button className="btn btn-primary" type="submit" disabled={!name.trim() || pw.length < 6}>
              <UserPlus size={14} /> Add
            </button>
          </form>
          <p className="dim">{role}: {ROLE_HELP[role]}.</p>
        </>
      )}
      {err && <p className="danger-text">{err.message}</p>}
    </section>
  );
}

export function MyAccountCard() {
  const qc = useQueryClient();
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);
  const save = useMutation({
    mutationFn: () => api.changeMyPassword(current, next),
    onSuccess: () => { setCurrent(""); setNext(""); setDone(true); },
  });
  const logout = useMutation({ mutationFn: api.logout, onSuccess: () => qc.resetQueries() });
  const user = auth.data?.user;
  if (!auth.data?.accounts || !user?.username) return null;
  return (
    <section className="card setting-card">
      <SectionHeader title={`Signed in as ${user.username}`} sub={`Role: ${user.role} — ${ROLE_HELP[user.role]}.`} />
      <form className="password-form" onSubmit={(e) => { e.preventDefault(); if (current && next.length >= 6) save.mutate(); }}>
        <input className="field" type="password" placeholder="Current password" value={current} autoComplete="current-password"
          onChange={(e) => { setCurrent(e.target.value); setDone(false); }} aria-label="Current password" />
        <input className="field" type="password" placeholder="New password (6+)" value={next} autoComplete="new-password"
          onChange={(e) => setNext(e.target.value)} aria-label="New password" />
        <button className="btn btn-ghost" type="submit" disabled={!current || next.length < 6}><KeyRound size={14} /> Change</button>
        <button className="btn btn-quiet" type="button" onClick={() => logout.mutate()}><LogOut size={14} /> Sign out</button>
      </form>
      {done && <p className="dim">Password changed. Your other devices will ask you to sign in again.</p>}
      {save.error && <p className="danger-text">{(save.error as Error).message}</p>}
    </section>
  );
}
