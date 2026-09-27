/** How to point a phone's backup app at Memoria's WebDAV folder, for automatic backup. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Smartphone } from "lucide-react";
import { api } from "../lib/api";

export function BackupAppCard() {
  const auth = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  const [copied, setCopied] = useState(false);
  const url = `${window.location.origin}/dav/`;
  const name = auth.data?.user?.username;
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  return (
    <section className="card backup-app">
      <h2><Smartphone size={17} /> Back up your phone automatically</h2>
      <p className="dim">A backup app on your phone can send every new photo here by itself, over your Wi-Fi. Photos already in
        the library are skipped, and deleting a photo on the phone never deletes it here.</p>
      <div className="backup-app-url">
        <code>{url}</code>
        <button className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(url).then(() => setCopied(true))}>
          {copied ? <Check size={14} /> : <Copy size={14} />} Copy address
        </button>
      </div>
      <p className="dim">Sign in with {name ? <><strong>{name}</strong> and your password</> : "your Memoria password (any name)"}.
        {local ? " This page is open on the Memoria computer itself: on the phone, use this computer's network address instead of localhost." : ""}</p>
      <div className="backup-app-steps">
        <div>
          <strong>Android — FolderSync (free)</strong>
          <ol>
            <li>Add an account → WebDAV → the address above, your name and password.</li>
            <li>Add a folder pair: local <code>DCIM/Camera</code> → remote <code>/Camera</code>, sync type “To remote folder”.</li>
            <li>Turn on scheduled sync (e.g. every hour on Wi-Fi) and leave “delete source files” off.</li>
          </ol>
        </div>
        <div>
          <strong>iPhone — PhotoSync</strong>
          <ol>
            <li>Settings → Configure → WebDAV → the address above, your name and password.</li>
            <li>Turn on Auto-Transfer to that WebDAV target (trigger: when on home Wi-Fi).</li>
            <li>In PhotoSync, choose “Original” format so photos keep their full quality and dates.</li>
          </ol>
        </div>
      </div>
      <p className="dim">The computer has to be on and reachable; new photos appear in the library about a minute after they
        arrive. Other WebDAV apps work the same way.</p>
    </section>
  );
}
