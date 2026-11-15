import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {dependency} from './runtime.mjs';
const {marked}=await dependency('marked','lib/marked.esm.js');

const here=path.dirname(fileURLToPath(import.meta.url));
const root=path.dirname(here);
const md=fs.readFileSync(path.join(root,'iboard-experiment-report.md'),'utf8');
let body=marked.parse(md);
let title='';
body=body.replace(/<h1>(.*?)<\/h1>/,(_,text)=>{title=text;return '';});
body=body.replace(/<p>从需求与故障输入，到可验证的实现和 PR 交付<\/p>/,'');
body=body.replace(/<div class="paper-meta">.*?<\/div>/,'');
const nav=[];let i=0;
body=body.replace(/<h2>(.*?)<\/h2>/g,(_,text)=>{const id=`section-${++i}`;nav.push(`<a href="#${id}"><span>${String(i).padStart(2,'0')}</span>${text.replace(/^[一二三四五六]、/,'')}</a>`);return `<h2 id="${id}"><span class="section-no">${String(i).padStart(2,'0')}</span>${text.replace(/^[一二三四五六]、/,'')}</h2>`;});
body=body.replace(/<table>/g,'<div class="table-scroll"><table>').replace(/<\/table>/g,'</table></div>');
body=body.replace(/(<div class="table-caption">表1[\s\S]*?<\/table><\/div>)/,'<div class="keep-together">$1</div>');
body=body.replace(/(<h3>6\.4 结论<\/h3>[\s\S]*?)(?=<div class="references")/,'<div class="keep-together">$1</div>');
body=body.replace(/\[(\d+(?:,\d+)*)\]/g,(_,n)=>`<sup class="cite"><a href="#references">[${n}]</a></sup>`);
body=body.replace(/href="paper\/figures\/([^"]+)" target="_blank"/g,'href="paper/figures/$1" target="_blank" rel="noopener"');
const css=fs.readFileSync(path.join(here,'paper.css'),'utf8');
const html=`<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="iBoard面向产品开发自动化的Agent Harness：产品定义、当前架构、OpenClaw实验与后续验证。"><title>${title}</title><style>${css}</style></head><body>
<header class="sitebar"><a class="brand" href="#"><span class="logo">iB</span>iBoard<span class="brand-sub">RESEARCH</span></a><nav><a href="#section-1">报告正文</a><a href="paper/architecture/iboard_as_is.archify.html">当前架构 ↗</a><a href="paper/architecture/iboard_to_be.archify.html">目标架构 ↗</a><a href="technical-notes.html">技术附件</a><a class="pdf-link" href="iboard-experiment-report.pdf">下载 PDF ↓</a></nav></header>
<div id="reading-progress"></div>
<section class="hero"><div class="hero-label"><span class="status-dot"></span> ENGINEERING EXPERIMENT · 01 <span class="date">2026.11.12</span></div><h1><span class="product-name">iBoard</span><br>面向产品开发自动化的<br><em>Agent Harness</em> 工作台</h1><p class="subtitle">从需求与故障输入，到可验证的实现和 PR 交付</p><div class="hero-rule"></div><div class="scope-strip"><div><small>产品范围 / SCOPE</small><b>需求实现 · 故障修复 · PR交付</b></div><div><small>本轮实验 / EVIDENCE</small><b>十题故障评测 + 独立交付试点</b></div><div><small>结论边界 / LIMIT</small><b>需求端到端能力待专门验证</b></div></div></section>
<div class="paper-layout"><aside class="contents"><p>CONTENTS</p>${nav.join('')}<a href="#references"><span>↗</span>材料与引用</a><div class="aside-note">阅读提示<br>主文解释问题与结论。<br>图中点开可查看大图，<br>架构图支持交互浏览。</div></aside><main><article>${body}</article><div class="endnote"><span class="logo">iB</span><div><b>以可验证的工程交付为完成单位。</b><p>本次修订产品定义、图形与排版；实验数据沿用已核对快照，未补跑产品实验。</p></div></div></main></div>
<footer><span>iBoard · 自动化产品开发实验</span><a href="iboard-experiment-report.md">Markdown 原文</a><a href="paper/README.md">图源、方法与验证记录</a><span>实验材料截至2026.11.07</span></footer>
<script>const sections=[...document.querySelectorAll('article h2')];const links=[...document.querySelectorAll('.contents a')];function update(){const span=document.documentElement.scrollHeight-innerHeight;document.querySelector('#reading-progress').style.width=(span>0?scrollY/span*100:0)+'%';let current=sections[0]?.id;for(const s of sections){if(s.getBoundingClientRect().top<180)current=s.id;}for(const a of links)a.classList.toggle('active',a.hash==='#'+current);}addEventListener('scroll',update,{passive:true});update();</script></body></html>`;

fs.writeFileSync(path.join(root,'iboard-experiment-report.html'),html);
let appendix=marked.parse(fs.readFileSync(path.join(root,'technical-notes.md'),'utf8'));let ai=0;const anav=[];
appendix=appendix.replace(/<h1>.*?<\/h1>/,'').replace(/<h2>(.*?)<\/h2>/g,(_,t)=>{const id='appendix-'+ ++ai;anav.push(`<a href="#${id}">${t}</a>`);return `<h2 id="${id}">${t}</h2>`;}).replace(/<table>/g,'<div class="table-scroll"><table>').replace(/<\/table>/g,'</table></div>').replaceAll('href="iboard-experiment-report.md"','href="iboard-experiment-report.html"');
fs.writeFileSync(path.join(root,'technical-notes.html'),`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>iBoard 实验报告技术附件</title><style>${css}</style></head><body><header class="sitebar"><a class="brand" href="iboard-experiment-report.html"><span class="logo">iB</span>iBoard</a><nav><a href="iboard-experiment-report.html">返回主报告</a><a href="technical-notes.md">Markdown原文</a></nav></header><section class="hero"><div class="hero-label">TECHNICAL APPENDIX · 实验记录与证据</div><h1>iBoard 实验报告技术附件</h1><p class="subtitle">完整版本、指标定义与逐题验证限制</p></section><div class="paper-layout"><aside class="contents">${anav.join('')}</aside><main><article>${appendix}</article></main></div><footer>iBoard · 实验材料截至2026.11.07 · 本次未补跑实验</footer></body></html>`);
console.log(JSON.stringify({sections:i,title,htmlBytes:Buffer.byteLength(html)}));
