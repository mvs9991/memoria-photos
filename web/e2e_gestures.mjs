// Press-and-hold, tap and drag selection with real mouse and touch input, desktop and phone size.
// Point it at the DISPOSABLE e2e library (eval/e2e_reset.ps1 starts it on 8768): the People part hides
// people and undoes it. It selects but never deletes. Usage: PW_CHANNEL=msedge node web/e2e_gestures.mjs
import { chromium } from "playwright";

const B = process.argv[2] || "http://127.0.0.1:8768";
const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

async function open(ctx, path, ready) {
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("PAGEERROR", String(e).slice(0, 160)));
  page.on("console", (m) => m.type() === "error" && console.log("CONSOLE", m.text().slice(0, 160)));
  await page.bringToFront();
  await page.goto(B + path, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(ready, { timeout: 60000 });
  await page.waitForTimeout(1200);
  return page;
}

// Boxes of the first N fully visible tiles, in index order.
async function tileBoxes(page, sel = "[data-sel-index]", n = 40, root = null) {
  return page.evaluate(([sel, n, root]) => {
    const out = [];
    const scope = root ? document.querySelector(root) : document;
    for (const el of scope.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.top > 130 && r.bottom < innerHeight - 20 && r.width > 20)
        out.push({ i: Number(el.dataset.selIndex), x: r.x + r.width / 2, y: r.y + r.height / 2 });
    }
    return out.sort((a, b) => a.i - b.i).slice(0, n);
  }, [sel, n, root]);
}
const selected = async (page) => {
  const el = page.locator(".selection-bar strong, .merge-bar strong").first();
  return (await el.count()) ? Number((await el.innerText()).replace(/\D/g, "")) : 0;
};

// ------------------------------------------------------------------ desktop, mouse
const desk = await browser.newContext({ viewport: { width: 1500, height: 1500 } });
let page = await open(desk, "/photos", ".tile-wrap");
let t = await tileBoxes(page);
console.log(`(${t.length} tiles visible, first indexes ${t.slice(0, 3).map((x) => x.i)})`);

// 1. a quick click still opens the viewer
await page.mouse.click(t[2].x, t[2].y);
await page.waitForTimeout(900);
check("quick click opens the viewer (no regression)", (await page.locator(".viewer").count()) > 0);
await page.keyboard.press("Escape");
await page.waitForTimeout(400);

// 2. hold still ~0.6s: selection starts with that photo, viewer does NOT open
await page.mouse.move(t[2].x, t[2].y);
await page.mouse.down();
await page.waitForTimeout(900);
await page.mouse.up();
await page.waitForTimeout(300);
check("press-and-hold selects that photo", (await selected(page)) === 1, `selected=${await selected(page)}`);
check("…and does not open the viewer", (await page.locator(".viewer").count()) === 0);
check("…and switches the page into select mode", (await page.getByRole("button", { name: "Done" }).count()) > 0);

// 3. tapping another photo while selecting toggles it
await page.mouse.click(t[6].x, t[6].y);
await page.waitForTimeout(200);
check("tap while selecting adds a photo", (await selected(page)) === 2, `selected=${await selected(page)}`);
await page.mouse.click(t[6].x, t[6].y);
await page.waitForTimeout(200);
check("tap again removes it", (await selected(page)) === 1);

// 4. Esc leaves selection
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
check("Esc clears the selection and leaves select mode",
  (await selected(page)) === 0 && (await page.getByRole("button", { name: "Select" }).count()) > 0);

// 5. hold, then drag across a range
t = await tileBoxes(page);
const a = t.find((x) => x.i === 1), b = t.reduce((m, x) => (x.i > m.i ? x : m), t[0]);
await page.mouse.move(a.x, a.y);
await page.mouse.down();
await page.waitForTimeout(900);
await page.mouse.move(b.x, b.y, { steps: 14 });
await page.waitForTimeout(250);
const midDrag = await selected(page);
check("hold then drag selects the whole range while dragging", midDrag === b.i, `selected=${midDrag}, wanted ${b.i} (tiles 1..${b.i})`);
// drag back to shrink it
const c = t.find((x) => x.i === 4);
await page.mouse.move(c.x, c.y, { steps: 8 });
await page.waitForTimeout(250);
check("dragging back shrinks the range", (await selected(page)) === 4, `selected=${await selected(page)}, wanted 4 (tiles 1..4)`);
await page.mouse.up();
await page.waitForTimeout(300);
check("releasing after a drag does not open the viewer", (await page.locator(".viewer").count()) === 0);
check("the selection bar offers Delete", (await page.getByRole("button", { name: /Delete/ }).count()) > 0);

// 6. already selecting: a mouse drag starts straight away, no hold needed
await page.mouse.move(t.find((x) => x.i === 5).x, t.find((x) => x.i === 5).y);
await page.mouse.down();
await page.mouse.move(t.find((x) => x.i === 7).x, t.find((x) => x.i === 7).y, { steps: 10 });
await page.mouse.up();
await page.waitForTimeout(250);
check("when already selecting, dragging adds a range with no hold", (await selected(page)) === 7, `selected=${await selected(page)}, wanted 4+3=7`);

// 7. starting a drag on a selected photo deselects
await page.mouse.move(t.find((x) => x.i === 2).x, t.find((x) => x.i === 2).y);
await page.mouse.down();
await page.mouse.move(t.find((x) => x.i === 4).x, t.find((x) => x.i === 4).y, { steps: 8 });
await page.mouse.up();
await page.waitForTimeout(250);
check("dragging from a selected photo deselects the range", (await selected(page)) === 4, `selected=${await selected(page)}, wanted 7-3=4`);
await page.keyboard.press("Escape");

// 8. edge auto-scroll: hold, then park the pointer at the bottom edge
const scrollTop0 = await page.evaluate(() => document.querySelector("[data-scroll-root]").scrollTop);
t = await tileBoxes(page);
await page.mouse.move(t[0].x, t[0].y);
await page.mouse.down();
await page.waitForTimeout(900);
await page.mouse.move(t[0].x + 4, 1490, { steps: 6 });
await page.waitForTimeout(2500);
const scrollTop1 = await page.evaluate(() => document.querySelector("[data-scroll-root]").scrollTop);
const bigCount = await selected(page);
await page.mouse.up();
const scrollable = await page.evaluate(() => { const r = document.querySelector("[data-scroll-root]"); return r.scrollHeight - r.clientHeight; });
if (scrollable > 600) {
  check("holding near the bottom edge auto-scrolls", scrollTop1 > scrollTop0 + 400, `scrollTop ${scrollTop0} -> ${scrollTop1}`);
  check("…and the selection grows past what was on screen", bigCount > t.length, `selected=${bigCount}`);
} else console.log("SKIP  auto-scroll: this library is too short to scroll", scrollable, "px");
await page.keyboard.press("Escape");
await page.close();

// ------------------------------------------------------------------ phone, touch
const phone = await browser.newContext({ viewport: { width: 390, height: 800 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
page = await open(phone, "/photos", ".tile-wrap");
const cdp = await phone.newCDPSession(page);
const touch = (type, x, y) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1 }] });
t = await tileBoxes(page);

// a scroll-like swipe must NOT start selection
await touch("touchStart", t[1].x, t[1].y);
for (let k = 1; k <= 6; k++) { await touch("touchMove", t[1].x, t[1].y - k * 25); await page.waitForTimeout(40); }
await touch("touchEnd");
await page.waitForTimeout(700);
check("phone: a swipe is a scroll, not a selection", (await selected(page)) === 0 && (await page.getByRole("button", { name: "Done" }).count()) === 0);

// hold, then drag
t = await tileBoxes(page);
const ta = t[1], tb = t[Math.min(5, t.length - 1)];
await touch("touchStart", ta.x, ta.y);
await page.waitForTimeout(900);
check("phone: holding selects the photo", (await selected(page)) === 1, `selected=${await selected(page)}`);
const sy0 = await page.evaluate(() => document.querySelector("[data-scroll-root]").scrollTop);
for (let k = 1; k <= 12; k++) { await touch("touchMove", ta.x + (tb.x - ta.x) * k / 12, ta.y + (tb.y - ta.y) * k / 12); await page.waitForTimeout(40); }
await page.waitForTimeout(200);
const sy1 = await page.evaluate(() => document.querySelector("[data-scroll-root]").scrollTop);
await touch("touchEnd");
await page.waitForTimeout(300);
check("phone: dragging after the hold selects the range", (await selected(page)) === tb.i - ta.i + 1, `selected=${await selected(page)}, wanted ${tb.i - ta.i + 1}`);
check("phone: the page did not scroll under the finger", Math.abs(sy1 - sy0) < 30, `scrollTop ${sy0} -> ${sy1}`);
check("phone: releasing does not open the viewer", (await page.locator(".viewer").count()) === 0);
await page.close();

// ------------------------------------------------------------------ People page
const peopleCount = async () => (await (await fetch(`${B}/api/people?sort=photos`)).json()).people.length;
const before = await peopleCount();
if (before < 3) {
  console.log(`SKIP  people gestures: this library has ${before} people (the e2e library has no faces)`);
  const failed = results.filter((ok) => !ok).length;
  console.log(`
${results.length - failed}/${results.length} passed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
}
page = await open(desk, "/people", ".person-tile");
let pt = await tileBoxes(page, ".person-tile[data-sel-index]", 40, ".people-grid");
console.log(`(people: ${pt.length} tiles visible, ${before} people visible in total)`);
await page.mouse.move(pt[2].x, pt[2].y);
await page.mouse.down();
await page.waitForTimeout(900);
await page.mouse.up();
await page.waitForTimeout(300);
check("people: press-and-hold selects that person and does not open them",
  (await selected(page)) === 1 && page.url().endsWith("/people"), `selected=${await selected(page)} url=${page.url()}`);
pt = await tileBoxes(page, ".person-tile[data-sel-index]", 40, ".people-grid");
// Start on an UNselected tile: starting on a selected one deliberately deselects the range.
await page.mouse.move(pt[3].x, pt[3].y);
await page.mouse.down();
await page.mouse.move(pt[8].x, pt[8].y, { steps: 12 });
await page.mouse.up();
await page.waitForTimeout(300);
check("people: dragging across tiles adds the range to what was selected", (await selected(page)) === 7, `selected=${await selected(page)}, wanted 1 + tiles 3..8 = 7`);
const n = await selected(page);
const hideBtn = page.getByRole("button", { name: new RegExp(`^Hide ${n}`) });
check("people: the bar offers Hide N", (await hideBtn.count()) === 1);
await hideBtn.click();
await page.waitForTimeout(1500);
const afterHide = await peopleCount();
check("people: Hide removes exactly those people from the page", afterHide === before - n, `${before} -> ${afterHide} (hid ${n})`);
const undo = page.getByRole("button", { name: "Undo" });
check("people: an Undo is offered", (await undo.count()) === 1);
await undo.click();
await page.waitForTimeout(1500);
const afterUndo = await peopleCount();
check("people: Undo brings every one of them back", afterUndo === before, `${afterUndo} vs original ${before}`);
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
check("people: Esc leaves select mode", (await page.getByRole("button", { name: "Select" }).count()) > 0);

// the original complaint: clicking a person must actually show their photos, promptly
pt = await tileBoxes(page, ".person-tile[data-sel-index]", 40, ".people-grid");
const t0 = Date.now();
await page.mouse.click(pt[0].x, pt[0].y);
await page.waitForSelector(".tile-wrap", { timeout: 30000 }).catch(() => {});
const took = ((Date.now() - t0) / 1000).toFixed(1);
check("people: clicking a person opens them and their photos appear", /\/people\/\d+/.test(page.url()) && (await page.locator(".tile-wrap").count()) > 0, `${took}s`);
await page.close();

// ------------------------------------------------------------------ the other pages have it too
const ids = await (await fetch(`${B}/api/people?sort=photos`)).json();
const personId = ids.people.find((p) => p.photo_count > 12)?.id;
const events = await (await fetch(`${B}/api/events?limit=5`)).json();
const places = await (await fetch(`${B}/api/places`)).json();
for (const [name, path, ready] of [
  ["person page", `/people/${personId}`, ".tile-wrap"],
  ["event page", `/events/${events.events[0].id}`, ".tile-wrap"],
  ["place page", `/places/${places.places[0].id}`, ".tile-wrap"],
  ["search results", `/search?q=video`, ".tile-wrap"],
  ["folders page", `/folders?root=3`, ".tile-wrap"],
]) {
  const p = await open(desk, path, ready);
  const hasToggle = (await p.getByRole("button", { name: "Select" }).count()) > 0;
  let held = "n/a";
  const tiles = await tileBoxes(p);
  if (tiles.length) {
    await p.mouse.move(tiles[0].x, tiles[0].y);
    await p.mouse.down();
    await p.waitForTimeout(900);
    await p.mouse.up();
    await p.waitForTimeout(250);
    held = (await selected(p)) === 1;
  }
  check(`${name}: has a Select button and press-and-hold selects`, hasToggle && (held === true || held === "n/a"),
    `button=${hasToggle} hold=${held}`);
  await p.close();
}

await browser.close();
const bad = results.filter((r) => !r).length;
console.log(`\n${results.length - bad}/${results.length} passed`);
process.exit(bad ? 1 : 0);
