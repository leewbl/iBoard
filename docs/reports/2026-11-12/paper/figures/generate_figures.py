from pathlib import Path
from html import escape
import json, hashlib

OUT=Path(__file__).resolve().parent
METRICS=OUT.parents[1]/'data/metrics.json'
M=json.loads(METRICS.read_text())
B='#3b6bdc'; D='#162a4b'; S='#61718c'; L='#d9e4f7'; G='#278a71'; P='#7757c9'; O='#c57a28'
class SVG:
 def __init__(self,title,h=820):
  self.h=h;self.p=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="{h}" viewBox="0 0 1440 {h}" role="img" aria-labelledby="title"><title id="title">{escape(title)}</title><defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M0 1 L7 4.5 L0 8" fill="none" stroke="{B}" stroke-width="1.5"/></marker><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse"><path d="M-2 2L2-2 M0 8L8 0 M6 10L10 6" stroke="#dde3ec" stroke-width="1"/></pattern></defs><rect width="1440" height="{h}" fill="white"/><g font-family="PingFang SC, Microsoft YaHei, Arial, sans-serif" fill="{D}">']
 def add(self,s):self.p.append(s)
 def rect(self,x,y,w,h,fill='white',stroke=L,r=14,dash=None):self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"'+(f' stroke-dasharray="{dash}"'if dash else'')+'/>')
 def text(self,x,y,t,size=20,color=D,weight=400,anchor='start'):
  self.add(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(t))}</text>')
 def lines(self,x,y,ss,size=20,color=D,gap=29,weight=400,anchor='start'):
  for i,s in enumerate(ss):self.text(x,y+i*gap,s,size,color,weight,anchor)
 def arrow(self,pts,color=B,dash=None):
  self.add(f'<path d="{pts}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#arrow)"'+(f' stroke-dasharray="{dash}"'if dash else'')+'/>')
 def line(self,x1,y1,x2,y2,color=L,width=1,dash=None):self.add(f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{color}" stroke-width="{width}"'+(f' stroke-dasharray="{dash}"'if dash else'')+'/>')
 def icon(self,x,y,kind,color=B):
  self.add(f'<g transform="translate({x},{y})" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">')
  icons={'doc':'<path d="M4 1H18L26 9V31H4Z M18 1V9H26 M9 16H21 M9 22H21"/>','code':'<path d="M10 6L1 16L10 26 M22 6L31 16L22 26 M19 3L13 29"/>','check':'<circle cx="16" cy="16" r="14"/><path d="M8 16L14 22L24 10"/>','clock':'<circle cx="16" cy="16" r="14"/><path d="M16 7V16L23 20"/>','git':'<circle cx="8" cy="6" r="4"/><circle cx="8" cy="27" r="4"/><circle cx="26" cy="10" r="4"/><path d="M8 10V23 M8 21C8 15 26 19 26 14"/>','layers':'<path d="M1 9L16 1L31 9L16 17Z M1 17L16 25L31 17 M1 24L16 32L31 24"/>','shield':'<path d="M16 1L29 6V16Q29 26 16 32Q3 26 3 16V6Z M9 16L14 21L24 11"/>'}
  self.add(icons[kind]+'</g>')
 def finish(self,name):self.p.append('</g></svg>');(OUT/name).write_text('\n'.join(self.p))

def f1():
 s=SVG('iBoard：面向自动化产品开发的 Harness 概念框架',820)
 s.text(40,40,'(a) 面向自动化产品开发的 Harness 工作台',27,weight=650)
 s.text(40,73,'把需求或故障描述组织为可执行、可验证、可追查的工程交付过程。',20,S)
 s.rect(275,105,910,290,'#f9fbff',B,20,'7 6')
 s.text(300,139,'iBoard 任务控制',20,B,600)
 s.text(1160,139,'单机 · 顺序执行',18,S,anchor='end')
 nodes=[(40,190,200,'输入任务',['需求 / GitHub Issue','截图 · 日志 · 文档'],'doc','#f1f5fe',B),
 (300,190,240,'定义与编排',['固定验收条件','阶段 · 工作区 · 预算'],'layers','#eeeafb',P),
 (590,190,250,'代码实现',['Claude Code → GLM','代码 · 测试 · 功能文档'],'code','#eaf5ee',G),
 (890,190,270,'验证与独立审查',['Docker 内运行目标代码','日志 · 测试 · 截图证据'],'shield','#eaf2fd',B),
 (1220,190,180,'交付产物',['PR + 验证证据','风险说明'],'git','#fff4e5',O)]
 for x,y,w,title,ls,ic,fill,col in nodes:
  s.rect(x,y,w,132,fill,col,12);s.icon(x+14,y+17,ic,col);s.text(x+56,y+39,title,22,col,600);s.lines(x+w/2,y+80,ls,18,S,27,anchor='middle')
 for x1,x2 in [(240,300),(540,590),(840,890),(1160,1220)]:s.arrow(f'M{x1+5} 255H{x2-8}')
 s.arrow('M1025 329V368H715V330',B,'6 5');s.text(870,359,'不通过时，在预算内修复与重验',18,B,anchor='middle')
 s.text(40,432,'(b) 需求验收：证明所需功能已经实现',24,P,600)
 s.text(750,432,'(c) 故障验收：证明原问题已经消失',24,G,600)
 s.rect(40,454,650,252,'#fcfaff',P,15,'6 5');s.rect(750,454,650,252,'#f9fdfa',G,15,'6 5')
 for x,title,ls in [(60,'功能说明',['用户行为','边界与约束']),(280,'验收用例',['预期结果','可重复步骤']),(500,'行为证据',['测试结果','截图 / 响应 / 日志'])]:
  s.rect(x,508,170,122,'#f2ecfc','#c2b3e5',10);s.text(x+85,541,title,22,P,600,'middle');s.lines(x+85,578,ls,18,S,26,anchor='middle')
 s.arrow('M236 569H271');s.arrow('M456 569H491')
 for x,title,ls in [(772,'基线复现',['原版本失败']),(928,'候选验证',['相同检查通过']),(1084,'回归测试',['保护既有行为']),(1240,'独立复验',['证据支持结论'])]:
  s.rect(x,508,138,122,'#edf7ef','#9eccb5',10);s.text(x+69,543,title,21,G,600,'middle');s.lines(x+69,585,ls,18,S,26,anchor='middle')
 for x in [910,1066,1222]:s.arrow(f'M{x+3} 569H{x+12}')
 s.text(365,679,'已具备需求验收入口；本轮没有专门的新需求任务集',18,P,anchor='middle')
 s.text(1075,679,'十题故障评测是先行验证切片，不能替代需求开发实验',18,G,anchor='middle')
 s.line(40,738,1400,738)
 s.lines(40,770,['概念框架，不表示所有分支均已充分验证。Docker 隔离目标测试；宿主机运行 Claude Code 和模型调用。','PR 交付受独立验收和任务策略约束；本轮十题保留本地，SQLite 试点另行验证真实合并。'],18,S,27)
 s.finish('fig1-harness-overview.svg')

def f2():
 s=SVG('OpenClaw 十题冻结验收得分与有效耗时',850)
 s.text(40,44,'(a) 冻结验收得分',27,weight=650);s.text(790,44,'(b) 有效耗时',27,weight=650)
 s.text(40,85,'80 / 100 分 · 8 / 10 题满足既定验收',24,B,600)
 s.text(790,85,'中位数 3:03:21 · 最长 8:37:52',22,B,600)
 s.text(40,128,'问题编号 / 主题',18,S)
 xs=[382,450,518,586]
 for x,label in zip(xs,['复现','修复','回归','复验']):s.text(x+29,126,label,18,S,anchor='middle')
 s.text(696,126,'总分',18,S,anchor='middle')
 plotx=800;plotw=460
 for h in [0,3,6,9]:
  x=plotx+plotw*h/9;s.text(x,126,str(h),18,S,anchor='middle');s.line(x,145,x,684,'#e4eaf3',1,'3 4')
 s.text(1325,126,'小时',18,S)
 short={149994:'审批按钮对比度',180815:'键盘跳转输入框*',180379:'父子任务答案一致性',183389:'会话忙碌状态',186914:'Slack 回复路由',185317:'模型策略拒绝提示',183700:'记忆插件诊断',159790:'重复加载 Lit',185079:'纯图片恢复上下文',185534:'Gemini 参数校验'}
 for i,c in enumerate(M['cases']):
  y=153+i*52;solved=c['points']==10
  if i%2==0:s.rect(30,y-1,1380,49,'#f7f9fc','none',5)
  s.text(43,y+30,f"#{c['issue']}",20,D,500);s.text(160,y+30,short[c['issue']],19,D)
  for j,(x,n) in enumerate(zip(xs,[2,4,2,2])):
   fill= ['#e4edf9','#e9e5fa','#e3f2ec','#d9ece8'][j] if solved else 'url(#hatch)'
   s.rect(x,y+2,58,42,fill,'#b5c9e2' if solved else '#d4dce7',7)
   s.text(x+29,y+30,n if solved else 0,21,B if solved else S,600,'middle')
  s.text(696,y+30,c['points'],23,B if solved else S,650,'middle')
  w=c['active_seconds']/3600/9*plotw
  col=B if solved else '#aab5c7'
  if c['issue']==180815:col=P
  s.rect(plotx,y+10,w,27,col,'none',5)
  s.text(1383,y+31,c['active_hms'],19,D,500,'end')
 s.line(40,697,1400,697)
 s.lines(40,735,['评分按 2 + 4 + 2 + 2 分解；0分表示未获得修复信用，不等同于全部测试失败。','两道0分题仍在分母：#183389 原版本已正确；#159790 在本轮 Linux 环境未复现。'],18,S,29)
 s.lines(40,801,['* #180815 通过补充验收，但工作台自身终审未完成。耗时包含准备、失败和中断调用，按记录排除用户暂停。'],18,S,28)
 s.finish('fig2-benchmark-results.svg')

def f3():
 s=SVG('功能验收与流程交付：两个独立研究的证据链',830)
 s.text(40,42,'(a) #180815：功能证据成立，工作台自身流程未完成',26,P,650)
 s.text(40,78,'十题评测中的一题 · 补充验收候选 72ef11ed · 取得10分',20,S)
 s.rect(40,103,1360,270,'#fcfaff','#a896d8',16,'7 6')
 xvals=[65,405,745,1085];w=290
 labels=[('原版本故障',['键盘无法按预期','跳过记录直达输入框'],'doc',B,'#edf3fc'),('补充候选验证',['156项单测 + 54项浏览器测试','独立补充审计支持冻结验收'],'check',G,'#eaf6ef'),('终审未完成',['补充 GLM 审查约420秒超时','没有有效审查输出'],'clock',O,'#fff4e5'),('尚未交付',['未发布或合并该题 PR','不能记为工作台自主完成'],'git',P,'#f1ecfa')]
 for x,(title,ls,ic,col,fill) in zip(xvals,labels):
  s.rect(x,142,w,141,fill,col,12);s.icon(x+18,161,ic,col);s.text(x+63,185,title,22,col,600);s.lines(x+w/2,228,ls,18,S,27,anchor='middle')
 for x in xvals[:-1]:s.arrow(f'M{x+w+7} 212H{x+329}')
 s.text(720,331,'原流程候选 dd0e84b9 与补充候选不同：补充验收通过不能写回为原流程终审通过。',19,P,anchor='middle')
 s.text(40,432,'(b) SQLite #186875：接纳已有补丁后，实际走通独立验收与 PR 合并',26,G,650)
 s.text(40,468,'另一个交付试点 · 与十题评分分开报告 · 采用不同内存配置',20,S)
 s.rect(40,493,1360,254,'#f9fdfa','#7ab49b',16,'7 6')
 labels=[('真实锁竞争',['接纳已有候选补丁','原版本在锁竞争下启动失败'],'doc',B,'#edf3fc'),('候选恢复行为',['短暂占锁：第3次尝试恢复','持续占锁：4次后明确失败'],'check',G,'#eaf6ef'),('独立复验与终审',['两名验证角色各109项用例','工作台自身终审通过'],'shield',G,'#eaf6ef'),('真实 PR 交付',['个人仓库 PR #2 已合并','上游 Issue 未关闭'],'git',B,'#edf3fc')]
 for x,(title,ls,ic,col,fill) in zip(xvals,labels):
  s.rect(x,530,w,141,fill,col,12);s.icon(x+18,549,ic,col);s.text(x+63,573,title,22,col,600);s.lines(x+w/2,616,ls,18,S,27,anchor='middle')
 for x in xvals[:-1]:s.arrow(f'M{x+w+7} 600H{x+329}')
 s.text(720,719,'该次运行验证已有候选的复验和交付，不表示从零生成新补丁；不计入十题80分。',19,G,anchor='middle')
 s.text(40,795,'分析含义：应分别记录功能验收、工作台流程完成和实际交付，避免将其中一个结果替代另外两个。',20,D,550)
 s.finish('fig3-evidence-chain.svg')

f1();f2();f3()
manifest={'source':str(METRICS),'sha256':hashlib.sha256(METRICS.read_bytes()).hexdigest(),'figures':{},'score_breakdown_note':'The frozen scoring maximum is 2+4+2+2. Accepted cases have 10/10, therefore every stage receives its full award; zero-score cases receive no repair credit. This visualization does not assert that zero-credit cases failed every executed test.','effective_time_note':M['duration_policy']['solver_active_policy'],'case_values':[{'issue':c['issue'],'points':c['points'],'active_seconds':c['active_seconds'],'active_hms':c['active_hms'],'outcome':c['outcome']} for c in M['cases']]}
for p in OUT.glob('fig*.svg'):manifest['figures'][p.name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'vector':True,'text_editable':True}
(OUT/'figure-data.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({p.name:p.stat().st_size for p in OUT.glob('fig*.svg')}))
