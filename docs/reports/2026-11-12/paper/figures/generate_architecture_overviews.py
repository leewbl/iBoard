"""Paper overview SVGs, manually composed from the shared Archify inventory.
These are not native Archify exports. Existing figures 1–3 remain untouched.
"""
from pathlib import Path
from html import escape
import ast, hashlib, json
OUT=Path(__file__).resolve().parent
B='#3b6bdc';D='#162a4b';S='#61718c';L='#d9e4f7';G='#278a71';P='#7757c9';O='#c57a28'
# Reuse only the vector drawing class; do not execute its original figure builders.
source=(OUT/'generate_figures.py').read_text()
cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='SVG')
exec(compile(ast.Module(body=[cls],type_ignores=[]),str(OUT/'generate_figures.py'),'exec'))
ARCH=OUT.parent/'architecture'
inputs=[ARCH/'architecture-inventory.json',ARCH/'iboard_as_is.archify.json',ARCH/'iboard_to_be.archify.json']
for p in inputs:json.loads(p.read_text())
frozen={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('fig[123]-*')}

def box(s,x,y,w,h,title,lines,fill,col,icon=None,title_size=24):
 s.rect(x,y,w,h,fill,col,12)
 if icon:s.icon(x+16,y+19,icon,col);s.text(x+62,y+44,title,title_size,col,600)
 else:s.text(x+w/2,y+43,title,title_size,col,600,'middle')
 s.lines(x+w/2,y+82,lines,20,S,29,anchor='middle')

def build(target=False):
 s=SVG('iBoard 建议目标架构（论文概览）' if target else 'iBoard 当前架构（论文概览）',860)
 s.text(40,40,'(b) 建议目标架构｜所有调整均待实现与验证' if target else '(a) 当前架构｜公开执行骨架与本地实验分开',28,weight=650)
 s.text(40,80,'保留执行与交付基础；先解决终审、状态与恢复，再验证需求开发和可迁移性。' if target else '需求与故障采用不同验收分支，共用模型执行、隔离测试、证据审查与 PR 交付。',21,S)
 box(s,40,130,210,150,'任务输入',['指定需求 / 故障','Issue + 输入材料'],'#f0f5fe',B)
 box(s,310,130,330,150,'任务协议与项目适配' if target else '任务协议与阶段控制',['建议调整：项目规则分离','保留 feature / bug 分支'] if target else ['feature → 实现','bug → 先复现'],'#efeafb',P)
 box(s,770,130,290,150,'有界独立审查' if target else '独立复验与终审',['建议：分项审查与限额','完整覆盖清单'] if target else ['按任务契约核对证据','大差异与恢复仍受限'],'#eaf6ef',G)
 box(s,1170,130,230,150,'GitHub PR',['保留门禁与远端核对','按任务策略交付'] if target else ['验收后创建 PR','SQLite 试点已合并'],'#fff4e5',O)
 s.arrow('M256 205H300');s.arrow('M646 205H760');s.text(703,189,'阶段调度',20,B,anchor='middle');s.arrow('M1066 205H1160');s.text(1115,189,'验收后',20,B,anchor='middle')
 # Host model interfaces. Grouping merges components A03–A04; both paths remain explicit.
 s.rect(40,340,490,250,'#f9fbff',B,16,'7 6');s.text(65,378,'模型调用｜宿主机',24,B,600)
 s.rect(65,408,210,83,'#eaf5ee',G,11);s.text(170,440,'Claude Code',24,G,600,'middle');s.text(170,473,'错误分类 / 用量回执*' if target else '实现 / 修复代码',20,S,anchor='middle')
 s.rect(65,519,210,50,'#f0f4fc',B,9);s.text(170,552,'HTTP 审查 / 视觉',20,B,550,'middle')
 s.rect(350,427,155,123,'#edf3fc',B,11);s.text(427.5,470,'GLM',26,B,600,'middle');s.text(427.5,510,'模型服务',22,S,anchor='middle')
 s.arrow('M281 450H340');s.text(310,431,'HTTPS',20,B,anchor='middle')
 s.arrow('M281 544H307V510H340')
 # Controller dispatches model execution and target verification; paper overview omits low-level state IO routes.
 s.arrow('M390 286V309H285V331');s.text(165,318,'工具 / 模型调用',20,B,anchor='middle')
 s.arrow('M575 286V309H770V331');s.text(672,301,'目标验证',20,B,anchor='middle')
 s.rect(590,340,360,250,'#f8fcf9',G,16,'7 6')
 s.text(770,382,'可迁移目标验证' if target else 'Docker 目标验证',24,G,600,'middle')
 s.lines(770,432,['建议：环境契约 / 资源配置','保留 Docker 目标隔离','第二阶段：干净环境复跑'] if target else ['OpenClaw · 基线 / 候选','需求：功能与用例检查','故障：复现与候选恢复'],21,S,36,anchor='middle')
 s.text(770,564,'仅隔离目标程序，不隔离宿主模型调用',20,G,anchor='middle')
 s.rect(1010,340,390,250,'#f9fbff',B,16,'7 6')
 s.text(1205,382,'一致状态与证据账' if target else '任务状态与证据',24,B,600,'middle')
 s.lines(1205,432,['建议：修订身份 / 检查点 / 终态','按候选版本核对证据','明确旧证据失效与恢复条件'] if target else ['提交 / 验收摘要 / 日志 / 截图','本地文件存储','状态与证据一致性仍需完善'],21,S,36,anchor='middle')
 s.arrow('M956 475H1000');s.text(980,462,'证据',20,B,anchor='middle')
 s.arrow('M1145 334V311H1005V287');s.text(1250,317,'提交与证据绑定',20,B,anchor='middle')
 s.rect(40,640,1360,156,'#fcfaff',P,16,'7 6')
 s.text(65,678,'建议建设｜尚未执行' if target else '本地实验｜不作为公开版自主能力',24,P,600)
 s.line(477,699,477,779,'#d6cbed');s.line(941,699,941,779,'#d6cbed')
 if target:
  s.text(65,722,'恢复与停止控制',24,P,600);s.lines(65,754,['优先：检查点恢复 / 拒绝不安全重试'],20,S,29)
  s.text(503,722,'需求 + 故障评测',24,P,600);s.lines(503,754,['后续：功能验收 / 流程完成 / PR交付'],20,S,29)
  s.text(968,722,'自动证据归档',24,P,600);s.lines(968,754,['后续：可迁移路径 / 报告生成'],20,S,29)
 else:
  s.text(65,722,'外部 Codex 协调与恢复',24,P,600);s.lines(65,754,['通过明确入口恢复；有本地恢复修改'],20,S,29)
  s.text(503,722,'固定十题 + 补充审定',24,P,600);s.lines(503,754,['仅故障样本；80/100 不等于自主交付'],20,S,29)
  s.text(968,722,'按题证据归档',24,P,600);s.lines(968,754,['复现 / 验证关闭 / 测试结果'],20,S,29)
 s.text(40,834,'* 建议补充，接口保留不表示改进已完成。概览合并了组件；完整关系见对应 Archify 图。' if target else '概览合并了组件；完整调用、状态读写与实验关系见对应 Archify 图。需求入口存在，不等于已有需求开发成绩。',20,S)
 s.finish('fig5-target-architecture.svg' if target else 'fig4-current-architecture.svg')

build(False);build(True)
assert frozen=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('fig[123]-*')},'Original figures changed'
manifest={'kind':'Manually composed paper overview, not native Archify SVG export','sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs],'component_mapping':{'task_input':['A01'],'protocol':['A02'],'host_model_group':['A03','A04'],'docker':['A05'],'evidence':['A06'],'review':['A07'],'PR':['A08'],'bottom_scope':['A09','A10','A11','A12']},'relationship_scope':'Overview preserves core control, both model-service interfaces, target isolation, evidence binding and public/local boundaries; detailed 13-edge topology remains in Archify. Bottom experimental/planned functions are grouped rather than claiming a new service.','minimum_font_size':20,'frozen_original_figures':frozen,'outputs':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('fig[45]-*.svg')}}
(OUT/'architecture-overview-data.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(manifest['outputs']))
