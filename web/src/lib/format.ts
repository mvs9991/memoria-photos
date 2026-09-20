/** Formatting helpers. Timestamps from the API are wall-clock seconds (see schema notes). */

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Photo timestamps are stored as wall-clock encoded in UTC, so read them back in UTC. */
export const toDate = (ts: number) => new Date(ts * 1000);
const g = (d: Date) => ({
  y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate(),
  wd: d.getUTCDay(), h: d.getUTCHours(), min: d.getUTCMinutes(),
});

export function formatDay(ts: number): string {
  const p = g(toDate(ts));
  const now = g(new Date());
  const sameYear = p.y === now.y;
  return `${DAYS[p.wd]}, ${p.d} ${MONTHS[p.m]}${sameYear ? "" : " " + p.y}`;
}

export function formatMonth(ts: number): string {
  const p = g(toDate(ts));
  return `${MONTHS[p.m]} ${p.y}`;
}

export function formatDate(ts: number | null | undefined): string {
  if (!ts) return "Unknown date";
  const p = g(toDate(ts));
  return `${p.d} ${MONTHS[p.m]} ${p.y}`;
}

export function formatDateTime(ts: number | null | undefined): string {
  if (!ts) return "Unknown date";
  const p = g(toDate(ts));
  return `${p.d} ${MONTHS[p.m]} ${p.y} · ${String(p.h).padStart(2, "0")}:${String(p.min).padStart(2, "0")}`;
}

export function formatTime(ts: number | null | undefined): string {
  if (!ts) return "";
  const p = g(toDate(ts));
  return `${String(p.h).padStart(2, "0")}:${String(p.min).padStart(2, "0")}`;
}

export function formatRange(a: number, b: number): string {
  const s = g(toDate(a));
  const e = g(toDate(b));
  if (s.y === e.y && s.m === e.m && s.d === e.d) return `${s.d} ${MONTHS[s.m]} ${s.y}`;
  if (s.y === e.y && s.m === e.m) return `${s.d}–${e.d} ${MONTHS[s.m]} ${s.y}`;
  if (s.y === e.y) return `${s.d} ${MONTHS_SHORT[s.m]} – ${e.d} ${MONTHS_SHORT[e.m]} ${s.y}`;
  return `${MONTHS_SHORT[s.m]} ${s.y} – ${MONTHS_SHORT[e.m]} ${e.y}`;
}

export const monthName = (m: number) => MONTHS[m - 1] ?? "";
export const monthShort = (m: number) => MONTHS_SHORT[m - 1] ?? "";

export function formatCount(n: number | null | undefined, one: string, many?: string): string {
  const v = n ?? 0;
  return `${v.toLocaleString()} ${v === 1 ? one : many ?? one + "s"}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  const b = bytes ?? 0;
  if (b < 1024) return `${b} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = b / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function relativeTime(ts: number | null | undefined): string {
  if (!ts) return "never";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} d ago`;
  return formatDate(ts);
}

export function exposureLabel(t: number | null): string {
  if (!t) return "";
  if (t >= 1) return `${t.toFixed(1)}s`;
  return `1/${Math.round(1 / t)}s`;
}

export const megapixels = (w?: number | null, h?: number | null) =>
  w && h ? `${((w * h) / 1e6).toFixed(1)} MP` : "";
