/**
 * Screenshot every screen for visual review.
 *   node shots.mjs [outDir] [baseUrl]
 */
import { chromium } from "playwright";
import { mkdirSync } from "fs";

const OUT = process.argv[2] || "D:/pi_cache/shots";
const BASE = process.argv[3] || "http://127.0.0.1:8765";

const PAGES = [
  ["home", "/"],
  ["photos", "/photos"],
  ["people", "/people"],
  ["events", "/events"],
  ["places", "/places"],
  ["map", "/map"],
  ["timeline", "/timeline"],
  ["duplicates", "/duplicates"],
  ["search", "/search?q=beach+photos"],
  ["settings", "/settings"],
];

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const errors = [];

/**
 * Wait until the page is actually worth photographing: every visible image has
 * decoded, and any count-up/reveal animation has stopped changing the text. A
 * fixed sleep is not enough on a cold server — an early capture produced blank
 * cover tiles and half-counted stats ("279 photos" for a 1,489-photo library),
 * which looks exactly like a rendering bug that isn't there.
 */
async function settle(page, name) {
  try {
    await page.evaluate(async () => {
      const visible = [...document.querySelectorAll("img")].filter((i) => {
        const r = i.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top < innerHeight * 2;
      });
      // decode() never settles for an image whose request stalls, so cap it.
      const capped = (i) => Promise.race([
        i.decode().catch(() => {}),
        new Promise((res) => setTimeout(res, 4000)),
      ]);
      await Promise.all(visible.map(capped));
    }, { timeout: 15000 });
    let prev = null;
    for (let i = 0; i < 12; i++) {
      const now = await page.evaluate(() => document.body.innerText.slice(0, 4000));
      if (now === prev) return;
      prev = now;
      await page.waitForTimeout(250);
    }
    errors.push(`[${name}] page never stopped animating`);
  } catch (e) {
    errors.push(`[${name}] settle failed: ${String(e).slice(0, 120)}`);
  }
}

/**
 * The photo grid must mount only the rows near the viewport. This depends on
 * .content actually being the scroll container, which is pure CSS and silently
 * broke once: the shell grew to fit its content, the grid measured a viewport
 * the height of the whole library, and every tile mounted (12,303 of them on a
 * real library). A small library hides this, so it is asserted rather than eyeballed.
 */
async function checkVirtualisation(page, name) {
  try {
    const n = await page.evaluate(() => {
      const c = document.querySelector("[data-scroll-root]");
      if (!c) return null;
      // A scroll root taller than the window is not scrolling anything.
      if (c.clientHeight > window.innerHeight + 200) return -1;
      return document.querySelectorAll(".tile").length;
    });
    if (n === -1) errors.push(`[${name}] scroll root is not constrained — grid virtualisation is off`);
    else if (n !== null && n > 400) errors.push(`[${name}] ${n} tiles mounted — grid is not virtualising`);
  } catch {
    /* page navigated away; not worth failing the run over */
  }
}

async function shoot(ctx, name, path, opts = {}) {
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`[${name}] console: ${m.text().slice(0, 200)}`);
  });
  page.on("pageerror", (e) => errors.push(`[${name}] pageerror: ${String(e).slice(0, 200)}`));
  page.on("requestfailed", (r) => {
    // Images used to be exempt here, which hid the one failure mode that matters
    // most in a photo app. Only aborts are ignored: those are the app correctly
    // cancelling off-screen thumbnail loads while scrolling.
    if (r.failure()?.errorText?.includes("ERR_ABORTED")) return;
    errors.push(`[${name}] request failed: ${r.url().slice(0, 120)} ${r.failure()?.errorText}`);
  });
  page.on("response", (r) => {
    if (r.url().includes("/api/") && r.status() >= 400) {
      errors.push(`[${name}] HTTP ${r.status()} ${r.url().slice(0, 120)}`);
    }
  });
  // Headless Chromium only runs requestAnimationFrame for the foreground page.
  // Without this, every page after the first freezes mid-animation and is
  // photographed with half-finished count-ups and un-revealed images.
  await page.bringToFront();
  try {
    await page.goto(BASE + path, { waitUntil: "networkidle", timeout: 45000 });
  } catch {
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(opts.wait ?? 1800);
  await settle(page, name);
  await checkVirtualisation(page, name);
  if (opts.action) {
    try {
      await opts.action(page);
    } catch (e) {
      errors.push(`[${name}] action failed: ${String(e).slice(0, 120)}`);
    }
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: !!opts.full });
  await page.close();
}

for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    deviceScaleFactor: 1,
  });
  await ctx.addInitScript((t) => localStorage.setItem("theme", t), theme);
  for (const [name, path] of PAGES) {
    if (theme === "light" && !["home", "photos", "people", "events"].includes(name)) continue;
    await shoot(ctx, theme === "dark" ? name : `${name}-light`, path);
  }
  await ctx.close();
}

// detail pages + viewer + mobile
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const res = await fetch(`${BASE}/api/people?sort=photos`).then((r) => r.json());
const personId = res.people?.[0]?.id;
const ev = await fetch(`${BASE}/api/events?limit=5`).then((r) => r.json());
const eventId = ev.events?.find((e) => e.kind === "trip")?.id ?? ev.events?.[0]?.id;
const pl = await fetch(`${BASE}/api/places`).then((r) => r.json());
const placeId = pl.places?.[0]?.id;

if (personId) await shoot(ctx, "person", `/people/${personId}`);
if (personId) await shoot(ctx, "person-review", `/people/${personId}`, {
  action: async (p) => {
    await p.getByRole("button", { name: /Review faces/i }).click();
    await p.waitForTimeout(2200);
  },
});
if (eventId) await shoot(ctx, "event", `/events/${eventId}`);
if (placeId) await shoot(ctx, "place", `/places/${placeId}`);

await shoot(ctx, "viewer", "/photos", {
  action: async (p) => {
    await p.locator(".tile").first().click();
    await p.waitForTimeout(2600);
  },
});
await ctx.close();

const mobile = await browser.newContext({ viewport: { width: 414, height: 900 }, deviceScaleFactor: 2 });
for (const [name, path] of [["home", "/"], ["photos", "/photos"], ["people", "/people"], ["events", "/events"]]) {
  await shoot(mobile, `mobile-${name}`, path);
}
await mobile.close();

await browser.close();
console.log(`screenshots in ${OUT}`);
if (errors.length) {
  console.log("\n--- issues ---");
  [...new Set(errors)].slice(0, 30).forEach((e) => console.log(e));
} else {
  console.log("no console/page errors");
}
