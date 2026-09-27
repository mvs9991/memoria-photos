/**
 * The phone's offline copy (public/sw.js): registering it, knowing when the app is showing
 * it instead of the server, and wiping it on sign-out.
 */
import { useSyncExternalStore } from "react";

let offline = false;
const listeners = new Set<() => void>();

/** Called with every API response: the service worker marks the ones it served from its copy. */
export function noteResponse(res: Response) {
  const now = res.headers.get("X-Memoria-Offline") === "1";
  if (now !== offline) {
    offline = now;
    listeners.forEach((l) => l());
  }
}

export function useOffline(): boolean {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l); },
    () => offline,
  );
}

/** Service workers need https (or this computer itself); over plain http the app just works online. */
export function registerOfflineCopy() {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator) || !window.isSecureContext) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").then(() => {
      // Fetch every page's code once, so any screen opens without the server later.
      const warm = () => Object.values(import.meta.glob("../pages/*.tsx")).forEach((load) => load().catch(() => {}));
      setTimeout(warm, 4000);
    }).catch(() => { /* no offline copy; nothing else changes */ });
  });
}

/** Signing out: the photos and lists kept on this device go too. */
export async function forgetOfflineCopy() {
  try {
    if (!("caches" in window)) return;
    await Promise.all(["memoria-images", "memoria-data"].map((name) => caches.delete(name)));
  } catch { /* nothing kept */ }
}
