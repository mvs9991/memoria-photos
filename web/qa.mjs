import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";
const B = process.argv[2] || "http://127.0.0.1:8822";
const PAGES = ["/","/photos","/people","/events","/places","/timeline","/duplicates","/albums","/collections","/folders","/insights","/search?q=beach","/settings","/trash","/map"];
const browser = await chromium.launch();

// --- 1. accessibility, both themes
let a11y = 0;
for (const theme of ["light","dark"]) {
  const page = await (await browser.newContext({viewport:{width:1600,height:1000},colorScheme:theme})).newPage();
  await page.bringToFront();
  for (const p of PAGES) {
    await page.goto(B+p,{waitUntil:"domcontentloaded",timeout:60000}).catch(()=>{});
    await page.waitForTimeout(1200);
    const r = await new AxeBuilder({page}).withTags(["wcag2a","wcag2aa"]).analyze();
    for (const v of r.violations) { a11y += v.nodes.length; console.log(`  a11y ${theme} ${p}: ${v.id} (${v.nodes.length})`); }
  }
  await page.context().close();
}
console.log(`ACCESSIBILITY violations: ${a11y}`);

// --- 2. mobile layout: clipped content and small targets
for (const [label,vp] of [["phone 390",{width:390,height:844}],["small 320",{width:320,height:640}]]) {
  const page = await (await browser.newContext({viewport:vp,hasTouch:true,isMobile:true,deviceScaleFactor:2})).newPage();
  await page.bringToFront();
  let clipped=0, tiny=0; const egs=[];
  for (const p of PAGES) {
    await page.goto(B+p,{waitUntil:"domcontentloaded",timeout:60000}).catch(()=>{});
    await page.waitForTimeout(1000);
    const r = await page.evaluate((W)=>{
      let c=0,t=0; const eg=[];
      for (const el of document.querySelectorAll("*")) {
        const b=el.getBoundingClientRect(); if(!b.width||!b.height) continue;
        if (b.right>W+1){ let a=el.parentElement,ok=false;
          while(a){const cs=getComputedStyle(a); if((cs.overflowX==="auto"||cs.overflowX==="scroll")&&a.scrollWidth>a.clientWidth+1){ok=true;break;} a=a.parentElement;}
          if(!ok){ c++; if(eg.length<2) eg.push("clip:"+((el.className||el.tagName)+"").slice(0,26)); } }
        const tag=el.tagName.toLowerCase();
        if ((tag==="button"||tag==="input"||el.getAttribute("role")==="button") && (b.height<24||b.width<24)) {
          t++; if(eg.length<4) eg.push("small:"+tag+"."+((el.className||"")+"").split(" ")[0]+` ${Math.round(b.width)}x${Math.round(b.height)}`);
        }
      }
      return {c,t,eg};
    }, vp.width);
    clipped+=r.c; tiny+=r.t; if(r.eg.length && egs.length<6) egs.push(`${p}: ${r.eg.join(", ")}`);
  }
  console.log(`${label}: clipped ${clipped}, below-24px targets ${tiny}`);
  egs.forEach(e=>console.log("   "+e));
  await page.context().close();
}
await browser.close();
