/* Memoria's offline copy: the photos you looked at recently, on this phone.
 *
 * What it keeps (all in this browser only, never anywhere else):
 *   - the app itself, so it opens without the server;
 *   - thumbnails, viewer previews and face crops you have seen: the IMAGE_LIMIT most recently
 *     fetched (the oldest go first);
 *   - the lists behind the pages you have opened (timeline, albums, people...), refreshed from
 *     the server whenever it can be reached.
 * What it never keeps: originals and downloads, videos, anything the server marks no-store
 * (every Locked-folder photo), sign-in, shared links, the Locked folder, accounts, uploads.
 * Signing out, or the server saying "sign in", wipes the photos and lists.
 *
 * A response served from here because the server could not be reached carries
 * X-Memoria-Offline: 1, so the app can say it is showing the offline copy.
 */
const SHELL = "memoria-shell-v1";
const IMAGES = "memoria-images";
const DATA = "memoria-data";
const IMAGE_LIMIT = 2000;
const DATA_LIMIT = 300;
const SHELL_LIMIT = 150;

// Never stored, always straight to the server.
const NEVER = [
  /^\/api\/auth\/(?!status$)/, /^\/api\/share\//, /^\/api\/locked/, /^\/api\/accounts/, /^\/api\/audit/,
  /^\/api\/browse/, /^\/api\/errors/, /^\/api\/jobs/, /^\/api\/upload/, /^\/api\/export/, /^\/api\/backup/,
  /\/original$/, /\/download$/, /\/video$/, /\/motion$/, /\/stream/, /^\/dav\//,
];
const IMAGE = /^\/api\/(thumb\/\d+|faces\/\d+\/crop)$/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.add("/")).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("memoria-shell") && name !== SHELL) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data === "memoria:forget") event.waitUntil(forget());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === "POST" && /^\/api\/photos\/lock$/.test(url.pathname)) {
    event.respondWith(lockThenForget(req));
    return;
  }
  if (req.method !== "GET" || req.headers.has("range")) return;
  const path = url.pathname;
  if (NEVER.some((re) => re.test(path))) return;
  if (req.mode === "navigate") event.respondWith(page(req));
  else if (IMAGE.test(path)) event.respondWith(image(req));
  else if (path.startsWith("/api/")) event.respondWith(data(req));
  else event.respondWith(shell(req));
});

function offline(res) {
  const headers = new Headers(res.headers);
  headers.set("X-Memoria-Offline", "1");
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

function keepable(res) {
  const cc = (res.headers.get("Cache-Control") || "").toLowerCase();
  return res.ok && res.type === "basic" && !cc.includes("no-store");
}

async function trim(name, limit) {
  const cache = await caches.open(name);
  const keys = await cache.keys();                  // oldest first
  for (let i = 0; i < keys.length - limit; i++) await cache.delete(keys[i]);
}

let puts = 0;
async function keep(name, req, res, limit) {
  const cache = await caches.open(name);
  await cache.delete(req);                          // re-inserting keeps the newest at the end
  await cache.put(req, res);
  if (++puts % 25 === 0) await trim(name, limit);
}

/** The app's page: the server's when it answers, else the kept one. */
async function page(req) {
  try {
    const res = await fetch(req);
    if (res.ok) {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put("/", copy));
    }
    return res;
  } catch (e) {
    const kept = await caches.match("/", { cacheName: SHELL });
    if (kept) return offline(kept);
    throw e;
  }
}

/** Scripts, styles, icons. Build files (/assets/) are named by their content, so a kept one is
 * never stale and is served first; the rest (manifest, icons, map outline) are fetched first. */
async function shell(req) {
  const hashed = new URL(req.url).pathname.startsWith("/assets/");
  const kept = await caches.match(req, { cacheName: SHELL });
  if (kept && hashed) return kept;
  let res;
  try {
    res = await fetch(req);
  } catch (e) {
    if (kept) return kept;
    throw e;
  }
  if (keepable(res)) {
    const copy = res.clone();
    background(keep(SHELL, req, copy, SHELL_LIMIT));
  }
  return res;
}

/** A thumbnail's address names its content (id, size, turn), so a kept one is served first. */
async function image(req) {
  const kept = await caches.match(req, { cacheName: IMAGES });
  if (kept) return kept;
  const res = await fetch(req);
  const cc = (res.headers.get("Cache-Control") || "").toLowerCase();
  if (keepable(res) && cc.includes("immutable")) {
    const copy = res.clone();
    background(keep(IMAGES, req, copy, IMAGE_LIMIT));
  }
  return res;
}

/** Lists and details: always the server's when it answers; the kept copy only without it. */
async function data(req) {
  let res;
  try {
    res = await fetch(req);
  } catch (e) {
    const kept = await caches.match(req, { cacheName: DATA });
    if (kept) return offline(kept);
    return new Response(JSON.stringify({ detail: "offline: this page has not been opened on this device yet" }),
      { status: 503, headers: { "Content-Type": "application/json", "X-Memoria-Offline": "1" } });
  }
  if (res.status === 401) {
    background(forget());                           // signed out elsewhere, or the session ran out
  } else if (keepable(res) && (res.headers.get("Content-Type") || "").includes("json")) {
    const copy = res.clone();
    background(keep(DATA, req, copy, DATA_LIMIT));
  }
  return res;
}

/** Photos just moved to the Locked folder leave the offline copy too. */
async function lockThenForget(req) {
  let ids = [];
  try { ids = (await req.clone().json()).photo_ids || []; } catch { /* not JSON: nothing to match */ }
  const res = await fetch(req);
  if (res.ok) {
    const gone = new Set(ids.map(String));
    const images = await caches.open(IMAGES);
    for (const key of await images.keys()) {
      const path = new URL(key.url).pathname;
      const m = path.match(/^\/api\/thumb\/(\d+)$/);
      // A face crop does not say whose photo it is: drop them all (they come back on the next visit).
      if ((m && gone.has(m[1])) || path.startsWith("/api/faces/")) await images.delete(key);
    }
    await caches.delete(DATA);
  }
  return res;
}

async function forget() {
  await caches.delete(IMAGES);
  await caches.delete(DATA);
}

// Background writes must not hold up the response; errors (quota) are ignored.
function background(p) {
  p.catch(() => {});
}
