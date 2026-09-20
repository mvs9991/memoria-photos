import { chromium } from "playwright";
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
await p.goto("http://127.0.0.1:8765/", { waitUntil: "networkidle", timeout: 45000 });
await p.waitForTimeout(2500);
await p.screenshot({ path: "D:/pi_cache/shots_final/probe_home_a.png" });
// force decode before capture
await p.evaluate(async () => { await Promise.all([...document.querySelectorAll("img")].map(i => i.decode().catch(()=>{}))); });
await p.waitForTimeout(500);
await p.screenshot({ path: "D:/pi_cache/shots_final/probe_home_b.png" });
console.log("done");
await b.close();
