/**
 * When a password is set, nothing but this screen renders until the browser has a
 * valid session. The server enforces it on every /api route regardless; this only
 * decides what to draw.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { api } from "../lib/api";
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
  if (status.data?.protected && !status.data.logged_in) return <Login />;
  return <>{children}</>;
}

function Login() {
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => api.login(password),
    onSuccess: () => qc.resetQueries(),
  });
  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={(e) => { e.preventDefault(); if (password) login.mutate(); }}>
        <span className="brand-mark login-mark" aria-hidden><Lock size={20} /></span>
        <h1 className="display">Memoria</h1>
        <p className="dim">This library is protected. Enter its password.</p>
        <input className="field" type="password" autoFocus value={password} autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)} aria-label="Password" />
        {login.error && <p className="danger-text">Wrong password.</p>}
        <button className="btn btn-primary" type="submit" disabled={!password || login.isPending}>Unlock</button>
      </form>
    </div>
  );
}
