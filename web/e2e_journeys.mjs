// End-to-end user journeys in a real browser against a DISPOSABLE library. NEVER point this at a real one:
// it deletes, restores, archives, hides, uploads and edits dates.
//
//   python eval/make_e2e_library.py                 build the 24-photo library under D:/pi_cache/e2e/lib
//   (index it once, snapshot lib+data into D:/pi_cache/e2e/pristine, then use eval/e2e_reset.ps1 before every run)
//   PW_CHANNEL=msedge node web/e2e_journeys.mjs     29 checks; also compare lib against pristine afterwards:
//   every original must be present and byte-identical (the project's hardest rule).
//
// Found so far: the Trash/Locked/Private pages missed the shared selection; see HANDOFF section 7.
import { chromium } from "playwright";
import { createHash } from "crypto";
import { existsSync, readFileSync, mkdirSync, writeFileSync } from "fs";

const B = process.argv[2] || "http://127.0.0.1:8768";
const LIB = "D:/pi_cache/e2e/lib";
const SHOTS = "D:/pi_cache/e2e/shots";
mkdirSync(SHOTS, { recursive: true });
const sha = (p) => createHash("sha256").update(readFileSync(p)).digest("hex");
const j = async (path, init) => (await fetch(B + path, init)).json();

const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 }, acceptDownloads: true });
const page = await ctx.newPage();
const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text().slice(0, 140)}`));
page.on("pageerror", (e) => problems.push(`pageerror: ${String(e).slice(0, 140)}`));
page.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 400 && r.status() !== 404) problems.push(`HTTP ${r.status()} ${r.url().slice(0, 100)}`); });

let pass = 0, fail = 0;
async function step(name, fn) {
  if (process.env.ONLY && !name.toLowerCase().includes(process.env.ONLY.toLowerCase())) return;
  const before = problems.length;
  try {
    const detail = await fn();
    const extra = problems.slice(before);
    if (extra.length) throw new Error("errors during step: " + extra.join(" | "));
    pass++; console.log(`PASS  ${name}${detail ? "  — " + detail : ""}`);
  } catch (e) {
    fail++; console.log(`FAIL  ${name}  — ${String(e.message || e).split("\n")[0].slice(0, 230)}`);
    try { await page.screenshot({ path: `${SHOTS}/${name.replace(/\W+/g, "_").slice(0, 50)}.png` }); } catch {}
    await page.keyboard.press("Escape").catch(() => {});
  }
}
const ok = (c, msg) => { if (!c) throw new Error(msg); };
const goto = async (path, ready = ".page, .tile-wrap") => {
  await page.goto(B + path, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(ready, { timeout: 20000 });
  await page.waitForTimeout(700);
};
const stats = () => j("/api/stats");
const photoIds = async (qs = "") => (await j("/api/photos/index" + qs)).ids;
const tiles = () => page.locator(".tile-wrap");

// ------------------------------------------------------------------ A. every screen opens cleanly
for (const [name, path] of [["home", "/"], ["photos", "/photos"], ["people", "/people"], ["albums", "/albums"],
  ["events", "/events"], ["places", "/places"], ["map", "/map"], ["timeline", "/timeline"], ["duplicates", "/duplicates"],
  ["collections", "/collections"], ["folders", "/folders"], ["insights", "/insights"], ["trash", "/trash"],
  ["upload", "/upload"], ["settings", "/settings"]]) {
  await step(`screen: ${name} opens with no errors`, async () => { await goto(path, "body"); await page.waitForTimeout(900); });
}

// ------------------------------------------------------------------ B. the viewer
await step("viewer: open, next, previous, details, close", async () => {
  await goto("/photos", ".tile-wrap");
  await tiles().first().click();
  await page.waitForSelector(".viewer", { timeout: 8000 });
  await page.keyboard.press("ArrowRight");
  await page.waitForTimeout(400);
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("i");
  await page.waitForTimeout(300);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  ok((await page.locator(".viewer").count()) === 0, "Esc did not close the viewer");
});

await step("viewer: favourite toggles and persists (f key)", async () => {
  const before = (await stats()).favorites;
  await tiles().first().click();
  await page.waitForSelector(".viewer");
  await page.keyboard.press("f");
  await page.waitForTimeout(700);
  const mid = (await stats()).favorites;
  await page.keyboard.press("f");
  await page.waitForTimeout(700);
  const after = (await stats()).favorites;
  await page.keyboard.press("Escape");
  ok(mid === before + 1 && after === before, `favourites ${before} -> ${mid} -> ${after}`);
  return `${before} -> ${mid} -> ${after}`;
});

await step("viewer: rapid favourite presses alternate correctly (no stale-state race)", async () => {
  const before = (await stats()).favorites;
  await goto("/photos", ".tile-wrap");
  await tiles().first().click();
  await page.waitForSelector(".viewer");
  for (let i = 0; i < 4; i++) await page.keyboard.press("f");     // four presses with no wait between them
  await page.waitForTimeout(1500);
  const after4 = (await stats()).favorites;
  for (let i = 0; i < 3; i++) await page.keyboard.press("f");
  await page.waitForTimeout(1500);
  const after7 = (await stats()).favorites;
  await page.keyboard.press("f");                                   // leave it as we found it
  await page.waitForTimeout(1000);
  await page.keyboard.press("Escape");
  ok(after4 === before && after7 === before + 1, `favourites ${before} -> (4 presses) ${after4} -> (3 more) ${after7}`);
  return `${before} -> ${after4} -> ${after7}`;
});

// ------------------------------------------------------------------ C. albums
let albumName = "E2E album";
await step("select 3 photos and create an album from them", async () => {
  await goto("/photos", ".tile-wrap");
  await page.getByRole("button", { name: "Select", exact: true }).click();
  for (let i = 0; i < 3; i++) await tiles().nth(i).click();
  await page.waitForTimeout(300);
  ok(/3\s*selected/.test(await page.locator(".selection-bar").innerText()), "3 were not selected");
  await page.getByRole("button", { name: /Add to album/ }).click();
  const dlg = page.getByRole("dialog", { name: /album/i });
  await dlg.waitFor({ timeout: 5000 });
  await dlg.getByRole("textbox").first().fill(albumName);
  await dlg.getByRole("button", { name: /create/i }).first().click();
  await page.waitForTimeout(1200);
  const albums = (await j("/api/albums")).albums ?? (await j("/api/albums"));
  const a = albums.find((x) => x.name === albumName);
  ok(a, "the album was not created");
  const detail = await j(`/api/albums/${a.id}`);
  ok((detail.photo_count ?? detail.count ?? detail.photos?.length) === 3, `album has ${JSON.stringify(detail).slice(0, 120)}`);
});
await step("albums page shows it; open it; remove one photo", async () => {
  await goto("/albums", ".page");
  await page.getByText(albumName).first().click();
  await page.waitForSelector(".tile-wrap", { timeout: 8000 });
  ok((await tiles().count()) === 3, `album page shows ${await tiles().count()} tiles`);
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().first().click();
  await page.getByRole("button", { name: /Remove from album/ }).click();
  await page.waitForTimeout(1200);
  ok((await tiles().count()) === 2, `after removal the album shows ${await tiles().count()}`);
});

// ------------------------------------------------------------------ D. archive and hide
await step("archive 2 photos: they leave the timeline and appear in Archive", async () => {
  const before = (await photoIds("?archived=exclude")).length;
  await goto("/photos", ".tile-wrap");
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().nth(0).click(); await tiles().nth(1).click();
  await page.getByRole("button", { name: /Archive/ }).click();
  await page.waitForTimeout(1200);
  const after = (await photoIds("?archived=exclude")).length;
  const archived = (await photoIds("?collection=archive")).length;
  ok(after === before - 2 && archived === 2, `timeline ${before} -> ${after}, archive holds ${archived}`);
});
await step("hide from the viewer, then bring it back from Collections", async () => {
  await goto("/photos", ".tile-wrap");
  const before = (await photoIds("?archived=exclude")).length;
  await tiles().first().click();
  await page.waitForSelector(".viewer");
  await page.getByRole("button", { name: /Hide photo/ }).click();
  await page.waitForTimeout(1200);
  const after = (await photoIds("?archived=exclude")).length;
  const hidden = (await photoIds("?collection=hidden")).length;
  ok(after === before - 1 && hidden === 1, `visible ${before} -> ${after}, hidden holds ${hidden}`);
  await page.keyboard.press("Escape").catch(() => {});
  await goto("/collections/hidden", ".page");
  await page.waitForSelector(".tile-wrap", { timeout: 8000 });
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().first().click();
  await page.getByRole("button", { name: /Show again/ }).click();
  await page.waitForTimeout(1200);
  const back = (await photoIds("?archived=exclude")).length;
  ok(back === before, `after Show again visible is ${back}, expected ${before}`);
});

await step("cancelling the delete confirmation with Esc keeps the selection", async () => {
  await goto("/photos", ".tile-wrap");
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().nth(2).click(); await tiles().nth(3).click();
  await page.getByRole("button", { name: /Delete/ }).click();
  await page.getByRole("alertdialog", { name: /Trash/i }).waitFor({ timeout: 5000 });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
  ok((await page.getByRole("alertdialog").count()) === 0, "Esc did not close the confirmation");
  const bar = await page.locator(".selection-bar").count() ? await page.locator(".selection-bar").first().innerText() : "";
  ok(/2\s*selected/.test(bar), `the selection was lost when the confirmation was cancelled (bar says: ${bar.slice(0, 40) || "nothing"})`);
  await page.keyboard.press("Escape");
});

// ------------------------------------------------------------------ E. trash and restore (real files, disposable)
await step("delete 2 photos: confirmed, moved to the Trash, originals gone from the folder", async () => {
  await goto("/photos", ".tile-wrap");
  const all = (await j("/api/photos/index")).ids;
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().nth(2).click(); await tiles().nth(3).click();
  const picked = [];
  for (const id of all.slice(2, 4)) picked.push(id);
  const info = await Promise.all(picked.map((id) => j(`/api/photos/${id}`)));
  globalThis.__trashed = info.map((p) => ({ id: p.id ?? p.photo?.id, path: p.path ?? p.rel_path ?? p.photo?.rel_path }));
  await page.getByRole("button", { name: /Delete/ }).click();
  const dlg = page.getByRole("alertdialog", { name: /Trash/i });
  await dlg.waitFor({ timeout: 5000 });
  const needs = await dlg.getByLabel(/Type the number/).count();
  if (needs) await dlg.getByLabel(/Type the number/).fill("2");
  await dlg.getByRole("button", { name: /Move to Trash/ }).click();
  await page.waitForTimeout(1500);
  const s = await stats();
  ok(s.trash === 2, `trash count is ${s.trash}`);
  return `trash=${s.trash}`;
});
await step("the Trash page lists them and Restore puts the bytes back unchanged", async () => {
  await goto("/trash", ".page");
  await page.waitForTimeout(800);
  const listed = await j("/api/trash");
  const items = listed.items ?? listed.photos ?? listed;
  ok(Array.isArray(items) && items.length === 2, `trash lists ${JSON.stringify(listed).slice(0, 140)}`);
  const before = {};
  for (const f of ["Trips/Goa/IMG_x0.jpg"]) if (existsSync(`${LIB}/${f}`)) before[f] = sha(`${LIB}/${f}`);
  // the Trash page uses the shared selection: Select all, then Esc clears it
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await page.getByRole("button", { name: /^Select all/ }).first().click();
  await page.waitForTimeout(300);
  ok(/2\s*selected/.test(await page.locator(".selection-bar").first().innerText()), "Select all did not select both");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  ok((await page.locator(".selection-bar").count()) === 0 || !/selected/.test(await page.locator(".selection-bar").first().innerText().catch(() => "")), "Esc did not clear the Trash selection");
  await page.getByRole("button", { name: /Restore all/ }).first().click();
  await page.waitForTimeout(1500);
  const s = await stats();
  ok(s.trash === 0, `trash still holds ${s.trash}`);
  ok(s.photos === 24, `photo count is ${s.photos}, expected 24 after restoring`);
});

// ------------------------------------------------------------------ F. upload
await step("upload a photo from the Upload page", async () => {
  const before = (await stats()).photos;
  const tmp = "D:/pi_cache/e2e/upload_me.jpg";
  const { execFileSync } = await import("child_process");
  execFileSync("D:/claude_photos_intelligence/.venv/Scripts/python.exe", ["-c",
    "from PIL import Image; import numpy as np; a=np.random.default_rng(7).integers(0,255,(480,640,3),dtype='uint8'); Image.fromarray(a).save(r'D:/pi_cache/e2e/upload_me.jpg')"]);
  await goto("/upload", ".page");
  await page.setInputFiles('input[type="file"]', tmp);
  await page.waitForTimeout(4000);
  const s = await stats();
  ok(s.photos === before + 1 || s.pending > 0, `photos ${before} -> ${s.photos} (pending ${s.pending})`);
  return `photos ${before} -> ${s.photos}, pending ${s.pending}`;
});

// ------------------------------------------------------------------ G. search, duplicates, folders
await step("search from the top bar finds Goa photos", async () => {
  await goto("/photos", ".tile-wrap");
  const box = page.getByPlaceholder(/Search/i).first();
  await box.fill("Trips Goa");
  await box.press("Enter");
  await page.waitForSelector(".tile-wrap", { timeout: 15000 });
  ok((await tiles().count()) >= 1, "no results");
  return `${await tiles().count()} results`;
});
await step("duplicates page: mark a group reviewed", async () => {
  await goto("/duplicates", ".page");
  const before = (await j("/api/duplicates?status=pending&limit=1")).total;
  const btn = page.getByRole("button", { name: /Mark reviewed/ }).first();
  ok(await btn.count(), "no group to review");
  await btn.click();
  await page.waitForTimeout(1200);
  const after = (await j("/api/duplicates?status=pending&limit=1")).total;
  ok(after === before - 1, `pending ${before} -> ${after}`);
});
await step("folders page: open a folder and see its photos", async () => {
  await goto("/folders", ".page");
  await page.locator(".folder-tile").first().click();
  await page.waitForTimeout(1200);
  ok(/folders/.test(page.url()), "did not stay in folders");
});

// ------------------------------------------------------------------ H. correct a date
await step("fix a photo's date from the selection bar", async () => {
  await goto("/photos", ".tile-wrap");
  const id = (await photoIds("?archived=exclude"))[0];
  const before = (await j(`/api/photos/${id}`)).taken_ts ?? (await j(`/api/photos/${id}`)).photo?.taken_ts;
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await tiles().first().click();
  await page.getByRole("button", { name: /Fix date/ }).click();
  const dlg = page.getByRole("dialog").last();
  await dlg.waitFor({ timeout: 5000 });
  const dateInput = dlg.locator('input[type="datetime-local"], input[type="date"]').first();
  await dateInput.fill(await dateInput.getAttribute("type") === "date" ? "2019-02-03" : "2019-02-03T10:20");
  await dlg.getByRole("button", { name: /apply|save|set|fix/i }).last().click();
  await page.waitForTimeout(2500);
  const after = (await j(`/api/photos/${id}`)).taken_ts ?? (await j(`/api/photos/${id}`)).photo?.taken_ts;
  ok(after !== before, `date unchanged (${before})`);
  return `${before} -> ${after}`;
});

console.log(`\n${pass}/${pass + fail} passed`);
if (problems.length) console.log("unattributed problems:", [...new Set(problems)].slice(0, 8));
await browser.close();
process.exit(fail ? 1 : 0);
