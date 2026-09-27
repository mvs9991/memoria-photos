/**
 * When a password is set, nothing but this screen renders until the browser has a
 * valid session. The server enforces it on every /api route regardless; this only
 * decides what to draw.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { Spinner } from "./States";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });

  useEffect(() => {
    const onDenied = () => qc.invalidateQueries({ queryKey: ["auth"] });
    window.addEventListener("memoria:login-required", onDenied);
    return () => window.removeEventListener("memoria:login-required", onDenied);
  }, [qc]);

  if (status.isLoading) return <Spinner full label="Opening Memoria" />;
  if (status.data?.protected && !status.data.logged_in) return <Login accounts={!!status.data.accounts} />;
  return <>{children}</>;
}

function Login({ accounts }: { accounts: boolean }) {
  const qc = useQueryClient();
  const [name, setName] = useState(() => {
    try { return localStorage.getItem("last-user") ?? ""; } catch { return ""; }
  });
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => api.login(password, accounts ? name.trim() : undefined),
    onSuccess: () => {
      try { if (accounts) localStorage.setItem("last-user", name.trim()); } catch { /* private mode */ }
      qc.resetQueries();
    },
  });
  const ready = !!password && (!accounts || !!name.trim());
  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={(e) => { e.preventDefault(); if (ready) login.mutate(); }}>
        <span className="brand-mark login-mark" aria-hidden><Lock size={20} /></span>
        <h1 className="display">Memoria</h1>
        <p className="dim">{accounts ? "Sign in to your family's photo library." : "This library is protected. Enter its password."}</p>
        {accounts && (
          <input className="field" autoFocus={!name} value={name} autoComplete="username" placeholder="Your name"
            onChange={(e) => setName(e.target.value)} aria-label="Name" />
        )}
        <input className="field" type="password" autoFocus={!accounts || !!name} value={password}
          autoComplete="current-password" placeholder="Password"
          onChange={(e) => setPassword(e.target.value)} aria-label="Password" />
        {login.error && <p className="danger-text">{(login.error as ApiError).status === 429
          ? `Too many wrong tries — ${(login.error as Error).message.replace(/^.*try again/, "try again")}.`
          : accounts ? "Wrong name or password." : "Wrong password."}</p>}
        <button className="btn btn-primary" type="submit" disabled={!ready || login.isPending}>
          {accounts ? "Sign in" : "Unlock"}
        </button>
      </form>
    </div>
  );
}
