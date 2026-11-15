import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';import {fileURLToPath,pathToFileURL} from 'node:url';
import {dependency} from './runtime.mjs';
const {chromium}=await dependency('playwright','index.mjs');
const here=path.dirname(fileURLToPath(import.meta.url));const root=path.dirname(here);const target=path.join(root,'iboard-experiment-report.html');const output=path.join(here,'review');fs.mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.IBOARD_DOCS_BROWSER?{executablePath:process.env.IBOARD_DOCS_BROWSER}:{})});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));const checks=[];
try{
 for(const width of [1440,1920,390]){
  await page.setViewportSize({width,height:width===390?1000:1000});await page.goto(pathToFileURL(target).href);await page.evaluate(()=>document.fonts.ready);await page.evaluate(async()=>{await Promise.all([...document.images].map(im=>im.decode().catch(()=>{})));});
  const data=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,h2:[...document.querySelectorAll('article h2')].map(x=>x.textContent),figures:document.querySelectorAll('figure').length,brokenImages:[...document.images].filter(i=>!i.complete||!i.naturalWidth).map(i=>i.src),tables:document.querySelectorAll('table').length,abstractParagraphs:document.querySelectorAll('.abstract p').length}));checks.push(data);
  await page.screenshot({path:path.join(output,`report-top-${width}.png`)});
  if(width===1440){for(const [name,id] of [['framework','fig-1'],['architecture','fig-2'],['results','fig-3'],['evidence','fig-4'],['future','fig-5']]){await page.locator('#'+id).evaluate(x=>x.scrollIntoView({block:'start',behavior:'instant'}));await page.screenshot({path:path.join(output,`report-${name}-1440.png`)});}}
 }
 await page.setViewportSize({width:1440,height:1000});await page.goto(pathToFileURL(target).href);await page.evaluate(()=>document.fonts.ready);await page.evaluate(async()=>{await Promise.all([...document.images].map(im=>im.decode()));});
 await page.pdf({path:path.join(root,'iboard-experiment-report.pdf'),format:'A4',printBackground:true,preferCSSPageSize:true,displayHeaderFooter:true,headerTemplate:'<span></span>',footerTemplate:'<div style="width:100%;padding:0 17mm;display:flex;justify-content:space-between;font:8px Arial;color:#8391a8"><span>iBoard · Engineering Experiment / 2026.11.12</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'});
 const result={status:checks.every(x=>x.width===x.scrollWidth&&x.brokenImages.length===0&&x.figures===5&&x.h2.length===6)&&errors.length===0?'pass':'fail',checks,errors,htmlSha256:crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex')};fs.writeFileSync(path.join(output,'browser-check.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
}finally{await browser.close();}
