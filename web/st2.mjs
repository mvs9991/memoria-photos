import { chromium } from "playwright";
const browser = await chromium.launch();
const page = await (await browser.newContext({viewport:{width:320,height:640},hasTouch:true,isMobile:true,deviceScaleFactor:2})).newPage();
await page.bringToFront();
await page.goto("http://127.0.0.1:8822/settings",{waitUntil:"domcontentloaded",timeout:60000});
await page.waitForTimeout(2000);
const r = await page.evaluate((W) => {
  const out=[];
  for (const el of document.querySelectorAll("*")) {
    const b=el.getBoundingClientRect(); if(!b.width||!b.height||b.right<=W+1) continue;
    let a=el.parentElement,ok=false;
    while(a){const cs=getComputedStyle(a); if((cs.overflowX==="auto"||cs.overflowX==="scroll")&&a.scrollWidth>a.clientWidth+1){ok=true;break;} a=a.parentElement;}
    if(ok) continue;
    const chain=[]; let n=el.parentElement;
    for(let i=0;i<3&&n;i++){ chain.push(((n.className||n.tagName)+"").slice(0,26)); n=n.parentElement; }
    out.push({cls:((el.className||el.tagName)+"").slice(0,34), w:Math.round(b.width), over:Math.round(b.right-W), chain});
  }
  return out;
},320);
console.log(JSON.stringify(r,null,1));
await browser.close();
