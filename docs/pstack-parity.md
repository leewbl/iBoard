# iBoard 与 pstack 的功能对标

对标版本为 [公开 pstack 0.15.48](https://github.com/cursor/plugins/tree/41e400932eca14f289e7db717a70455256b6d388/pstack)。工程先以 OpenClaw 验证实际可用性，执行端沿用 Claude Code + GLM。验证使用断网容器和独立复验。以每个入口实际产出的结果验收，记录运行条件、版本与未覆盖项。

[逐项源码目录](pstack/catalog.json) 收录 118 份文件，包括 62 个技能、22 个工作流程、4 份代理和工作者提示词、3 个自动化模板、13 份参考和 14 份指南。条目含固定版本、内容哈希、标题结构、可解析的内部文档引用，以及同路径的 Claude Code 生成版本哈希。上游的状态图测试快照属于测试产物。

- [技能和工作流程](pstack/workflows.md) 记录主技能、工作流程的触发条件、产物、工具依赖和接入差距。
- [原则、代理提示词和 Benny](pstack/principles-agents.md) 记录工程原则和协作契约，以及从事件入口到外部服务的适配范围。
- [提示词参考和指南](pstack/references.md) 追溯示例、使用方法与宿主差异。

```sh
python3 -m workbench.pstack_catalog list --role skill
python3 -m workbench.pstack_catalog show skills/poteto-mode/playbooks/bug-fix.md
python3 scripts/update_pstack_catalog.py /path/to/pinned-cursor-plugins --check
```

iBoard 原生的[阶段提示词](../workbench/prompt_profiles.py)将 24 条工程原则分配到规划、基线审查、实现、修复和终审。四份代理提示词按职责接入多阶段委派、并行调研、注释审查和 Benny。模型回执的 `workflow_guidance` 记录固定上游提交、实际使用的来源和分段提示词哈希。运行时只使用 iBoard 编写并审查的对应规则。

[`/agent-workbench:poteto-help`](../skills/poteto-help/SKILL.md) 或 `python3 -m workbench.pstack_help` 可查询场景、技能和工作流程。查询结果给出本地命令、指南、所需证据和适用条件。只纳入阶段提示词的条目标成 `guidance-only`，缺少经核对的本地入口时显示 `catalog-only`。

[`workbench.refactor`](../skills/refactor/SKILL.md) 在重构前冻结源提交、预期行为、驱动和允许改动的路径。它在断网 Docker 中使用同一份驱动，逐提交比较退出码和 stdout、stderr 原始字节。证据不完整或同一版本两次运行不一致时，结果记为未确定。`workbench.start --kind refactor` 将对照报告送入独立终审，创建、更新、合并 PR 前会重新核对报告、原始回执和同一份终审。

[故障根因验证](../skills/bug-cause/SKILL.md) 可随新任务启用。工作台把故障触发与单参数对照绑定到同一提交的驱动，在编辑前独立判断对照能否排除一个竞争解释。修复后在相同入口重跑两组检查，原故障信号仍出现、驱动改变或回执缺失都会阻断交付。

[重复失败诊断](../workbench/repair_premise.py) 读取两个未通过同一门禁的提交、具体补丁和原始报告。支持本地沙箱检查、必需 GitHub CI 的同一个 job，以及终审引用证据后重复拒绝的同一验收条件。共享前提由只读模型说明，并在当前候选的只读断网容器中执行可区分的探针。可调用仓库已跟踪的脚本，也可将新的 Node 探针作为只读输入。探针无结论时暂停。新候选仍须通过原验收、独立复验及对应提交上的必需 CI。GitHub 取证、按原 head 更新 PR 和等待新 head 的 CI 已用接口夹具验证，探针与候选复验在真实 Docker 中运行。GitHub Actions 端到端写入待验收。

只读调研可用 [`workbench.investigate`](../skills/investigate/SKILL.md) 或 Claude Code 的 `/agent-workbench:investigate` 启动。指定提交和文件即可运行 `how`、`why`、`teach` 或 `investigation`。合成模式分别向源码与历史阶段提供证据，再汇总为统一说明，保留两边的未知项。报告记录实际引用的源码行和提交、模型调用时间及提示词哈希。历史阶段可用 `--pr` 纳入用户明确选定、能与所选 Git 历史关联的 PR 正文和有界讨论摘录。

[`teach`](../skills/teach/SKILL.md) 也可以用 `--lesson` 输出离线 HTML。OpenClaw 会话投递恢复实例用 Claude Code 和 GLM 生成四幅逐步展开的图，逐图对照固定提交中的投递、标记、重启扫描和结算代码。没有相关 Git 历史可说明作者动机时，报告保留该缺口。实际浏览器在断网下复验了桌面、手机宽度和深色主题的展开操作及图中文字。

接续或查看已留存任务时，使用 [`workbench.pickup`](../skills/pickup/SKILL.md) 或 `workbench.resume --inspect`。工具观察现有任务锁，核对工作树、输入和证据哈希，区分已记录的发布与合并结果。可用 `--since` 查看指定时间后的事件，用 `--check-pr` 查询对应 PR 的当前提交和检查状态。该入口提供接续判断，执行仍经过原有恢复和交付门禁。

[`workbench.run_control hold`](../skills/pause-safely/SKILL.md) 可在另一进程暂停指定任务。已经开始的编辑步骤可以留下正常提交和检查点，后续验证、依赖派发和 GitHub 写入会检查暂停记录。查看确认停点后，显式解除才能按原任务预算恢复。

[`workbench.autonomous_run`](../skills/autonomous-run/SKILL.md) 固定选定任务的验收、发布或合并目标，沿用原输入、预算和交付授权。CI 未结束时保留 PR、候选和基线，释放任务执行锁。下一次观察只查询该检查点，发现明确结果后再交给原有验收或修复流程。工作台保留每次重新启动、等待及最终证据。

[`workbench.autopilot_full`](../skills/autopilot-full/SKILL.md) 把已冻结的独立任务加入队列，并发推进各自的验收、PR 或合并目标。整组暂停在下一次操作前生效，正在执行的步骤可留下提交和检查点，解除后按各任务的原预算恢复。队列保留每项结果，同一仓库的写入按顺序通过原门禁。对于基线前移，可用 `--reverify-stale-main` 触发任务重放和复验。状态回执绑定远端 `main`、原任务、补丁和新的独立报告。使用 `--deliver-replayed` 可按原授权创建 PR 或更新原 PR，通过绑定到新提交的 CI 或原 OpenClaw 本地门禁后再合并。重启时用交付日志核对远端结果。发布后若主分支再次前移，队列暂停该项。

[并行验收](../skills/swarm/SKILL.md) 在独立工作树和断网容器中运行固定提交的检查。覆盖模式要求每条路径都有通过证据，竞选模式按事先选定的规则比较候选。用 `--swarm-manifest` 可把固定路径纳入正常任务的交付门禁。审计逐项核对收据哈希、提交、命令、结果和清理。 同一技能另提供限定源码范围的并行模型调研。Claude Code + GLM 或 Codex 各自接收批准片段，引用须与 Git 原文一致。竞选可采用首个合格结果，或按冻结评分表独立盲评。模型调研结果独立于运行时验收门禁。

长任务的阶段决定保存在任务目录的 `decisions.tsv`。每行记录时间、阶段、决定、理由、证据和结果。[`workbench.decisions audit`](../skills/show-me-your-work/SKILL.md) 核对保留文件的哈希和 Git 提交，显示哪些链接仍需在线查看。新的运行会列出继承记录的时间范围，人工可追加说明或纠正。

Claude Code 执行时会为该次运行保留可见回复、工具调用名称和结果状态，也保留直接 GLM 评审的回执。用 `workbench.decision_review` 选择一轮运行和可用的 OpenAI 模型，逐条对照决策、回执和哈希绑定的报告。受限的 Codex CLI 会话只接收该轮的材料。结果必须引用实际证据，运行或报告在审查期间发生变化则放弃本轮结论。已导入的外部会话单独注明来源。跨供应商复核由技能显式发起，OpenClaw 发布门禁仍使用其独立的断网验收。

使用 [`workbench.pr_watch`](../skills/babysit/SKILL.md) 查看明确指定的 GitHub PR。支持一次检查、只看评审讨论，以及在限时内等待 CI 或审批。可以从一个已选 PR 识别开放的依赖栈，也可以按父 PR 在前的顺序指定成员。工具核对父子分支和提交，并按冲突或基线变化、未解决讨论、检查失败的顺序定位阻断。观察结果同时记录 head 和 base。仓库中没有观察到检查时会明确显示，iBoard 的独立复验和合并仍有各自的门禁。

[`workbench.verify_pr`](../skills/verify-pr/SKILL.md) 可为已存在的 GitHub PR 创建只用于复验的任务。它在新工作树中固定原始条件和两个提交，在父提交运行基线场景，再切到已存在的候选提交运行断网检查和独立终审。验证失败时保留报告和候选现场。`workbench.stack_verify` 对照当前 PR、原始输入、证据哈希和独立报告，从根 PR 开始找出连续通过的范围。

[Benny 已有修复复验](../skills/reproduce-and-fix-issues/SKILL.md) 从指定 GitHub PR 或已合入的提交固定版本，在隔离环境从基线镜像派生候选。主驱动和独立探针各在两版运行两次并保留实际输出。只有基线重复出现原症状、候选重复达成预期且 GitHub 工件仍一致时，才出具确认结果。过程保留原 PR 归属，只有读取权限。

[Benny 部署及事件入口](../skills/setup-benny/SKILL.md#install-and-verify-the-project-package) 可预览并安装两项流程，从新的 Git 检出核对提交、清单和本地依赖。回环 HTTP 接收器按 Slack 签名及配置范围接收指定报告的事件，只保存消息坐标和部署摘要。独立工作进程复读原线程，再进行分诊、复现或既有修复验证。已授权的写入继续走工单、原线程或草稿 PR 门禁，重复投递复用同一证据。已用本机签名回调、真实 GLM 和断网 Docker 验证入口；真实 Slack 订阅、Cursor 编辑器与外部交付仍待实测。

分诊交接写入前保存已审定的计划和截止时间。工作进程在写入后中断时，可用原事件接续，并由原交接日志核对回执。本机测试模拟了回帖已确认、事件尚未结算时中断，恢复后分诊和回帖各只执行一次。复现已落盘时，重启会核对原事件、部署提交和探针证据。已用签名回调和真实断网 Docker 验证重启沿用原探针回执。修复任务已准备完成时，重启只接续原任务和工作树，沿用原预算。草稿交付会用原日志及远端记录核对已有分支和草稿 PR，本机模拟中确认只创建一次。复现结果未落盘或任务输入不全时，保留原记录供核查。

配置独立的运维频道和写入身份后，每个进入复现流程的报告有一条运维状态消息。协调者就地更新复现、修复与草稿状态，确认草稿后在同一线程回帖附链接。模拟回帖提交后中断，重启只核对原消息，复现和草稿交付都保持一次。

已结算的运行可启用固定时长的运维追问。工作台从经核验的回执生成事实，Claude Code + GLM 只选择与问题相关的引文，协调者发送对应的完整事实。重启沿用原截止时间，响应丢失时回读原回复。已用真实 GLM、断网 Docker 和本机 Slack 模拟验证从提问到停止；真实 Slack 工作区仍待验收。

可为原报告线程启用[复现通知](../skills/setup-benny/SKILL.md#optional-reproduction-reply-in-the-report-thread)。主探针和独立探针各两次观察到原症状后，协调者复读原线程并回复一次，可附运维线程和工单链接。修复与草稿门禁核对本次通知回执、新出现的权属变化和原文。已用本机签名回调、真实断网 Docker 和 Slack HTTP 模拟验证丢失响应后的回执恢复，以及通知后人工认领阻断。

报告人可以在原等待窗口内点名选择一项[事前批准的复现设置](../skills/setup-benny/SKILL.md#approve-a-corrected-setup-before-the-run)，也可描述复现步骤哪里需要改。为设置提供适用说明后，Claude Code + GLM 可从已批准的选项中选择，并保存报告人原话和设置说明的精确引文。无法确认时保留解释记录并暂停。窗口结束后按确认的设置在同一提交和镜像上只复跑一次，修复和草稿门禁会复核原始证据与更正证据。

已结算的确认复现也可在原运维窗口内接收同一报告人的一次更正。工作台只复跑验证，结果与原修复或草稿记录分别留存。回复和后续提问引用复跑结论前会复核原始与更正后的探针回执。已用真实 GLM、断网 Docker 和本机 Slack 模拟验证；真实 Slack 到 GitHub 的完整交付仍待验收。

[Slack 来源入口](../skills/setup-benny/SKILL.md) 读取一条指定报告。它固定工作区、原频道和根线程，核对分诊作者与最后一行标记，并保留显式认领和修复链接。已有修复可直接进入只读复验。线程在复验前后都需匹配原快照，变化时保留运行结果并暂缓确认。Slack 读协议已用本机测试服务联调，真实工作区读取和回帖仍待实测或接入。

[Benny 新故障复现](../skills/reproduce-and-fix-issues/SKILL.md#reproduce-an-unclaimed-fault) 从可信分诊的未认领报告开始，把原线程、Git 基线、镜像和两份检查固定下来。两份检查各复现两次才能继续，之后使用同一套检查验证候选。认领窗口结束并排除已有 GitHub 修复后可生成暂停任务，任务内记录根因验证要求。界面故障还需要固定两条画面条件，并复审两次基线录制。已用 OpenClaw #202294 的历史候选、断网 Docker、真实 GitHub 读取和本地 Slack 模拟确认前后差异，并验证已有 PR 会阻止重复建任务。[`workbench.benny_repair`](../skills/reproduce-and-fix-issues/SKILL.md#run-the-prepared-repair) 启动暂停的修复任务。协调者在每次变更前核对认领和已有 PR。Claude Code + GLM 在独立工作树中分析和修改，协调者先做根因对照，再在修复后执行原来的两条探针并进行独立终审。验证通过后保留候选与证据，停在本地交付阶段。[`workbench.benny_draft`](../skills/reproduce-and-fix-issues/SKILL.md#submit-the-verified-repair-as-a-draft) 接着核对任务、断网证据和个人 fork，将该报告的固定分支推送到验证时的基线并创建草稿 PR。两次 GitHub 写入前重新查认领、已有修复和原始证据。返回不明确时查询原操作回执。界面修复会重建候选浏览器产物，独立检查两次录制和最终截图，要求终审引用第二次截图的原始回执，再在草稿写入前复核完整证据。真实 Slack 到 GitHub 的端到端交付仍待验收。

[Benny 分诊](../skills/triage-issue-reports/SKILL.md) 将同一份报告、固定提交的源码与历史、指定 GitHub 工单搜索结果交给 Claude Code + GLM。核对结论引用的原文和工单，保留无法判断的事实与附件。目录不全、发现来源已登记或复验前报告发生改变时，不提出新建工单。分诊本身输出本地建议。

[Benny 工单交接](../skills/triage-issue-reports/SKILL.md#apply-a-reviewed-handoff) 将批准的建议绑定到来源线程和工单目标，允许创建工单或为现有重复工单追加来源评论。协调者在回帖前读取原线程，提交后复读。已发送但无明确结果的请求只核对回执。收到明确拒绝且回读证明回帖缺失时，关闭本次创建的新工单并核对状态。GitHub 和 Slack 均已用本机 HTTP 模拟联调，真实账号权限待验。

[Benny 跟进窗口](../skills/triage-issue-reports/SKILL.md#watch-one-follow-up-window) 从已确认的回帖计时。协调者只处理直接问它的问题和报告人的具体更正。前者附原文依据，后者在原工单追加具名字段及来源链接。重复运行只核对未知回执，不延长窗口。已用本机线程、工单模拟与真实 GLM 验证定向回复、更正和停止。

[附件取证](../skills/triage-issue-reports/SKILL.md#attachments) 根据原线程中的文件 ID 读取私有 Slack 文件，记录原件哈希、文字引文和图片观察。MP4 取三个时间点的帧，单独记录观察及证据限制。已用本机 Slack 协议测试服务、真实 FFmpeg 和 GLM 视觉模型验证。抽样和未读取的附件仍算证据缺口，保存的附件或帧变更会阻止确认分诊建议。

[`workbench.stack_ship`](../skills/shipping/SKILL.md) 把已验证的依赖路径和 PR 上限固定在交付计划中，在获授权的个人 fork 上从下往上执行。父 PR squash 或主分支再次前移时，`workbench.stack_rebase continue` 从原始补丁重放，沿用原请求、验收历史和本地门禁，对新基线和新候选重新取证。推送以最近一次确认的 PR head 为条件。执行日志保留历次独立复验和发布回执。外部修改 PR head 或改写主分支历史时停止写入。本地三层样例用裸 Git 模拟 GitHub 写入，验证了推送成功但回执丢失、主分支同时前移时的续跑，复验实际运行了 Claude Code、GLM 和断网 Docker。真实 GitHub 栈仍待验收。

[`workbench.stack_autopilot`](../skills/autopilot-stack/SKILL.md) 从本地清单冻结一组已选 PR 的请求、验收条件、head 和 base。工作台按父子顺序独立复验，也能复用当前版本的已验收任务。交接前逐项核对当前 CI 或本地门禁，记录缺口。恢复时沿用冻结的输入和从首次启动起计算的总时限。此入口停在交接。

[`workbench.arena`](../skills/arena/SKILL.md) 在同一基线生成多个代码方案。独立工作树并发改动，断网沙箱按顺序运行同一条冻结检查。GLM 与 OpenAI 按公开评分标准评阅去掉身份信息的候选补丁，并记录分歧处理和取舍。综合候选通过新一轮沙箱和独立终审后才导出补丁。

通过 [Feature 交接](../skills/feature-handoff/SKILL.md) 启动的新任务可固定已选设计、允许改动的文件、四项拆分检查、原型结论和验收条件。给出已经批准的两种架构时，可由 `workbench.start` 运行 Arena，对照相同的验收命令盲评补丁，复验并采用胜出提交。原报告有分歧时，发布还要求 [Interrogate](../skills/interrogate/SKILL.md) 的探针回执。两份独立运行和两家模型评审必须指向同一候选，评审时仍需逐项核对最终运行条件。

通过 [多阶段派发](../skills/multi-phase-plan/SKILL.md) 显式授权计划后，iBoard 先测原始主线，再按依赖创建各自的任务和工作树。计划可授权最多四个阶段并发实现。每个阶段保留独立的记录，基线测量和候选验证逐项运行。同一阶段的单元测试、真实入口和候选性能绑定到同一个镜像。交互改动需要对具体提交和证据审批，才能释放依赖阶段。`workbench.phase_ship` 核对声明的个人 fork、具体 PR 和前一阶段落地提交后，继续交付下一阶段。需要更新基线时，`workbench.phase_reverify` 保留补丁和原验收条件，在独立工作树重新取证。交互审批随候选提交重新确认。

[`workbench.project_verify create`](../skills/create-verification-skill/SKILL.md) 从源码锚点、用户路径和现有驱动生成项目验证技能与功能地图。固定 Git 版本和本地镜像，启动 doctor 和一条真实场景后才导出可交接的技能。[维护入口](../skills/maintain-verification-skill/SKILL.md) 核对地图与源码，逐项推动真实场景，发现失败后重新运行 doctor。修订版要经过全图复验才导出差异。OpenClaw 地图收录五条 CLI 路径和五条真实浏览器路径。浏览器巡检从运行中的页面核对编译版本，并将截图绑定到报告。配置相关代码变更还要求将配置验证的双份断网证据纳入终审。

[`workbench.formal_verify create`](../skills/create-formal-verification-skill/SKILL.md) 可生成绑定源码提交、属性、验证工具、探索边界和反例的项目技能。创建和[维护](../skills/maintain-formal-verification-skill/SKILL.md)都会在临时工作树故意打破一项属性，检查验证器能否抓到反例，随后复验未改动的源码。维护保留旧验证器结果、变更与移动记录。修改已确认的属性和边界需要明确的意图与批准证据。OpenClaw [队列示例](../examples/formal/openclaw-command-lane/README.md) 穷举容量为一时三条命令的 40 种调度，并提供同一属性的有限 TLA+ 模型。

[`workbench.statechart check`](../skills/statechart/SKILL.md) 在断网 Docker 中运行固定 pstack 引擎和 TLC，导出最短违规路径、原始输出、Mermaid 和 Pele SVG。OpenClaw 模型在正常配置下验证了 16 个状态，放宽容量限制后，两次入队即触发违规。[模型检查契约](../examples/formal/openclaw-command-lane/manifest-model.json) 将这条容量判断绑定到源码，再用独立 Node 驱动重放实际队列。证明助手的实际接入仍待验证。

[OpenClaw 连接态验证](../skills/verify-openclaw/SKILL.md) 从固定镜像启动临时 Gateway，用浏览器登录 Control UI，再核对 `sessions.list` 与截图。会话改名场景还检查取消时没有写入、保存时 Gateway 确认写入，以及 Gateway 重启后从新浏览器读回同一会话的新名称。会话删除场景先取消并由新的浏览器上下文确认两条会话都还在，再确认删除其中一条。Gateway 重启后重新登录，查询活动与归档会话，确认只剩原 ID 的未选中会话。涉及会话界面的改动必须提交两套通过证据。发出 PR 前，门禁会再次核对镜像、提交、独立执行身份、Gateway 重启记录和截图哈希。

[会话归档与恢复](../skills/verify-openclaw/SKILL.md#session-archive-and-restore) 由浏览器对选中的空闲会话执行两次真实写入。每次都核对 Gateway 的成功响应，重启后通过新浏览器确认会话 ID 和列表状态。未选中的会话保留原 ID 与活动状态。归档路径、共享会话菜单和相关 Gateway 文件变更触发该门禁。

| 能力 | iBoard 状态 | 等效能力的验收条件 |
| --- | --- | --- |
| 项目验证和功能地图 | 可按源码和驱动生成项目验证技能。OpenClaw 覆盖 Gateway、技能资格、配置校验，以及 Control UI 会话的读取、状态筛选、改名、单选删除、归档和恢复。改名与删除检查取消，所有写入场景都检查重启后读回。 | 扩到其他界面动作、PTY 和其他项目。对每条用户路径验证可见结果、必要副作用和清理。 |
| 功能地图维护 | [并行源码审查](../skills/maintain-verification-skill/SKILL.md#review-the-feature-source) 为每个功能生成有源码引用的说明与运行建议。巡检逐条核对来源与实际运行，可发现已绑定命令和 JSX 控件选项的漏项或过时项。固定数组中的 Commander 注册可展开为具体命令，无法解析的注册保留源码位置并阻断绿色结论。OpenClaw 的 Workshop 归档恢复与会话状态筛选均有独立运行路径。对声明了失败信号的场景可用双驱动和同一运行时区分脚本与产品问题。 | [维护交付入口](../skills/maintain-verification-skill/SKILL.md#ship-a-verified-correction) 将已提交的单技能修订与全图证据绑定到现有 PR 门禁。真实账号的维护 PR、间接注册、更多界面控件和路由仍待验证。 |
| Bug 修复 | 有双份基线复现、修复、候选复验及 PR 门禁。 | 用同一场景复现并对照候选，确认故障机制，检查影响范围，缺失复现或必要证据时暂停。 |
| 需求、原型与架构 | 可把选定设计及原型结论绑定到正式 Feature 任务。两种方案可独立实现和盲评，胜出提交纳入静态评审、断网验收和边界探针。可按多阶段计划派发本地任务。 | 串联多阶段验收与远端 PR 栈落地，让评审能主动提出新的功能边界，并覆盖更多实际用户入口和副作用。 |
| 性能、运行诊断与形式化验证 | 基准可固定版本、负载和证据。逐次优化可按性能和独立正确性保留或回退。属性验证支持有界真实代码检查及 TLC 模型检查，保留源码同步历史、变异反例、状态图及独立重放。 | 配齐业务负载、发布构建和自动交付。继续验证其他证明工具和业务属性。 |
| 影响范围、代码约束与持续纠错 | 新任务的独立复验记录受影响入口、需要保持的行为和候选源码位置，并引用独立沙箱报告。发布和合并再次核对位置、提交与报告哈希。 | 继续补跨模块的选测和回归设计。对重复错误新增能抓住旧错误的可执行约束，并把验证保留给后续任务。 |
| 自主运行、暂停与恢复 | 有按任务预算、锁、恢复入口和修复循环。 | 在受控失败、工具超时及中断后安全恢复或明确阻断，回放不会重复写入远端，任务状态与产物一致。 |
| 并行研究、方案比较和任务分派 | Arena 支持独立工作树并发生成代码候选、跨供应商评分、综合和复验。 | 扩到多类产物、一般任务分派和大范围方案比较，存在共享写入时可安全排序。 |
| PR 创建、跟踪、合并和 PR 栈 | 已有单 PR 创建与个人 fork 合并门禁。三层本地集成覆盖单次调用等待检查、逐层重放、主分支前移后的独立复验及写入确认。 | 在真实 GitHub 栈验证写入、检查与合并队列流程。 |
| 调研、复盘、交接和工作流生成 | 任务接续会核对实时状态。指定 Claude Code 会话可用于恢复上下文、复盘技能和生成个人模式草稿。 | 增加 Cursor 会话和外部服务的发现适配。按实际运行验证不同宿主的交接流程。 |
| 宿主集成 | iBoard 提供 Claude Code 插件和 Python 入口。 | 对照 [pstack 的 Claude Code 适配说明](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/docs/harness/claude-code.md)，对依赖 Cursor Automations、云代理或 Bugbot 的能力提供独立实现，或者记录具体的外部依赖及真实运行证据。 |

公开功能目录是核对入口。每个功能仍需单独记录指令、测试环境、通过条件和运行证据。Lauren Tan 的生产案例用于理解工作方式，交付规模与团队内部环境另需实际测量。

[`workbench.worktree_cleanup`](../skills/worktree-cleanup/SKILL.md) 可先只读盘点 Git 工作树，再冻结明确批准的路径。它自动检查当前用户的进程工作目录、打开文件及已观察到进程对应的 Claude Code 工作区，删除前再次检查占用。macOS 和断网 Linux 容器均用真实 Git 工作树验证了批准后出现占用会阻止删除。进程退出后同一份批准可继续，Git 分支保留。

开启近期会话扫描后，盘点会从 Codex、Claude Code 和 Cursor 本地记录中列出关联会话及对应证据摘要。真实 Codex 会话从主工作树启动并在兄弟工作树实际执行命令，退出后产生的会话记录拦下了原清理批准。重新核对该记录及批准指定路径后，临时工作树才移除，分支保留。Claude Code 子会话和 Cursor 路径识别已有格式夹具验证。

使用本机 Codex 桌面置顶观察时，工作台按会话 ID 追踪旧记录和子代理路径，包括从其他项目启动的会话。批准后必须刷新同一宿主的观察。真实桌面两次读取得到空置顶列表，验证了旧观察阻断、刷新后放行。非空置顶、归档父会话、跨项目子代理和同名前缀用原生格式的夹具验证。其他宿主的置顶及休眠会话使用手工保留清单。

开发资源盘点覆盖 Xcode `DerivedData` 与 `iOS DeviceSupport`、Cursor 工作区快照与备份，以及 `simctl` 的测试设备、不可用设备和运行时镜像。清理只处理明确批准的路径或原生 UUID。缓存文件的占用和内部变更阻断了真实临时目录的删除，进程退出后可以沿用批准。模拟器已有协议夹具验证精确命令、异步移除和读取失败后的回执。本机只读盘点未找到完整 Xcode，真实 `simctl` 删除仍待验收。

[包管理器缓存维护](../skills/worktree-cleanup/SKILL.md#package-caches) 需要确认具体路径，再记录操作前后的缓存大小和磁盘可用空间。临时目录验证覆盖 uv 原生修剪、pnpm 同时绑定存储与元数据缓存、Homebrew 单个已下载文件，以及 Yarn Classic 和现代 Yarn 的全局缓存。各场景都检查了范围外的文件仍然存在。

[`workbench.blind_eval`](../skills/blind-eval/SKILL.md) 给各方案同一个任务，在独立目录运行 Claude Code + GLM。评审通过匿名代码、成功的实际文件读取和断网 Docker 运行结果给分。两个独立 OpenAI 会话使用同一份评分标准。审计可离线重建评审输入并核对回执。此入口已支持固定文本源文件的对比，其他宿主轨迹和媒体产物待接入。

[`workbench.correct`](../skills/correct/SKILL.md) 将重复错误对应到实际历史提交与所选防护层级。在断网 Docker 中，用同一探针核对每次错误、对应修复和当前实现。iBoard 的 OpenClaw 发布门禁现在为每条必测路径核对两个独立 Docker 容器；历史回放还检验了未登记回执和缺失 Gateway 重启两类防护。

[`workbench.figure_it_out`](../skills/figure-it-out/SKILL.md) 为没有现成流程的任务固定目标、变更范围和退出条件。实施者逐项尝试有上限的假设。只有当前功能、上一阶段行为和回归护栏都经双次断网执行及独立评审确认，才保留该提交。最后再复验整个产品行为，导出已验证的补丁序列及离线审计结果。

[`workbench.model_profiles`](../skills/setup-pstack/SKILL.md) 在调用后确认指定的模型与 CLI 推理配置，解析角色与预算。显式批准的配置在任务启动时固定，规划、实施、修复及独立评审按各自角色调用 Claude Code + GLM。恢复沿用任务快照。回执记录 CLI 确认的模型和请求的推理档位，服务端实际推理量未测量。

[Arena](../skills/arena/SKILL.md) 与 [Swarm 模型调研](../skills/swarm/SKILL.md) 可载入同一配置。Arena 从配置固定候选席位和不同供应商的评委，并把实际调用绑定到盲评和独立复验。Swarm 默认继承工作者角色，竞选可选择预算内已探测的不同模型，评委须来自批准的跨供应商池并在参赛模型之外。两者保留运行期配置快照，后续修改安装文件不会更换已批准的路由。

[Interrogate](../skills/interrogate/SKILL.md) 也可固定跨供应商评审席位。功能检查可以从批准的目录中按变更行挑选，保留用户指定的必测项。对同一候选用不同的断网容器复演，并核对原始输出。候选是否通过由可重复的实际观测和对观测的独立评审共同决定。未提交改动可以用 `workbench.interrogate_snapshot` 固定。它按明确选定的文件或工作区差异捕获当前内容，保留原分支与暂存状态。只读评审与断网复验共用这份快照，两个独立工作树会先核对文件字节和模式。

通过 `workbench.start` 新建的任务采用 `impact_review: runtime-v1`。独立复验者可以只读检查未修改的调用方，并在终审 JSON 的 `impact` 中记录入口、需要保持的行为、候选文件行号、观测结果和报告引用。发布门禁会拒绝只有代码差异、基线结果或实施者报告的影响证明。报告身份由程序核对，证据是否覆盖所述行为仍由独立复验者判断。既有任务沿用创建时的门禁和记录格式。

## 需求澄清和改写

[`workbench.intent_review`](../skills/sharpen/SKILL.md) 提供需求重述、通俗改写、术语重述和任务深化四种入口。回执保留原请求，源码依据固定到提交。[术语重述](../skills/reframe/SKILL.md) 可用单独提供的公开主题查找一手资料，也可使用指定链接。搜索回执保留查询和每个候选的核验状态，独立读取的网页原文用于复核每个术语。依据不足时只保留已核实的术语。获批准的澄清可附在准备完成的任务上，进入规划和实现提示词，原始范围和验收条件继续生效。改换基线后的澄清复用仍待接入。

以 OpenClaw 会话投递队列的崩溃恢复为例，实时搜索找到并打开八份公开资料。两轮引文检查最终核实九个术语，改写说明了接收方无法去重时的交付保证边界。预备任务接受了完整报告，并拒绝了首次缺少精确引文的报告。

## 技术写作和原文核对

[`workbench.technical_writing`](../skills/technical-writing/SKILL.md) 可按用途审阅、改写或起草文档、README、RFC、PR 正文和提交说明。文章采用一种 Diátaxis 结构，PR 和提交说明按工程交付格式处理。[`unslop`](../skills/unslop/SKILL.md) 复用同一流程，并保留上游规则编号。改写先检查命令、数字、代码和链接，再由独立会话核对事实、范围、原意和读者需求。通过后导出独立文件。写作流程可作为提交前的单独步骤。

## TypeScript 类型和边界校验

规划、实现、修复和复验的提示词带有 [TypeScript 规则](../skills/typescript-best-practices/SKILL.md)。`workbench.typescript_discipline` 可用本地镜像中的 Compiler API 对照 Git 基线和候选。断网容器检查项目配置、编译报错、所选文件是否真实进入编译，以及候选是否放宽原有检查。候选在独立容器再次编译，可额外双跑指定的真实调用测试。有新增类型断言、无条件谓词等迹象时，可固定源码片段与运行回执发起独立复核。发布继续走项目原有门禁。

## 统一选择和执行工作流

[`poteto-mode`](../skills/poteto-mode/SKILL.md) 覆盖上游 22 条 playbook 和复杂任务的 `figure-it-out`。可由用户指定流程，也可由只读模型根据原请求推荐，并记录依据。运行使用固定的原生命令、已批准的参数及证据摘要。前一步的文件可以带着路径和哈希供下一步使用。计划指定的结果字段必须在原生回执中出现。中断后保留日志，继续运行已有阶段所需的恢复入口由原生工作流提供。

## 会话进展摘要

[`ketchup`](../skills/ketchup/SKILL.md) 可用 Claude Code 或 Codex 提供的会话 ID 读当前本地记录，也可指定 Cursor 会话 ID 或回放导出。逐条读取选定会话，记录这次读取的字节数和哈希。新增日志留到下一次读取，已读字节变化会阻止本次摘要。时间戳缺失时按消息顺序划定范围，选定附件的时序会标注为无法确认。命令可直接渲染确认的 PR 状态、已有素材和待决事项。已在 Claude Code 插件运行中和真实 Codex CLI 会话验证消息定位。云会话和 Cursor 编辑器中的直接调用仍待联调。

[`ketchup-video`](../skills/ketchup-video/SKILL.md) 与文字版共享会话定位。已选回执、截图或录屏帧提供画面证据，视频以同一张图说明变化，并用独立底栏显示字幕。渲染后逐帧核对正文和字幕，转场单独计时。等待用户决定的事项放在视频之外。

## Bot 操作面板

[`make-bot-ui`](../skills/make-bot-ui/SKILL.md) 将按钮关联到明确选择的任务或固定哈希的工作流。面板服务器带着服务端密钥转发小型 JSON。原生接收端保存正文摘要和唤醒记录，同一请求只派发一次。失败请求保留原数据供单独唤醒处理。支持连接已有 Cursor webhook，并可使用已登录的 Tailscale 节点访问。真实 Cursor 账户、私网跨设备访问及其审批过程仍需单独实测。

[`workbench.automate_me`](../skills/automate-me/SKILL.md) 从明确选定的 Claude Code 会话生成个人模式技能草稿。收集阶段生成带会话行号的本地证据，模型只收到批准范围内的用户文本。重复习惯须有不同周和不同会话的依据。草稿纳入确认的建议和直接规则，可在保留其他条目的前提下更新已有技能。

[`workbench.reflect`](../skills/reflect/SKILL.md) 把指定的一次会话复盘转成技能修订及待办。正文修订绑定会话中使用的技能，漏触发场景调整描述。应用前核对批准的行、原技能和现有副本，再在 Claude 项目或用户技能目录写入。

[`workbench.recall`](../skills/recall/SKILL.md) 可按 Git 工作区、时间和主题查找本地 Claude Code、Cursor 和 Codex 会话，也可指定文件。证据记录宿主、原始行号和内容哈希，区分 Cursor 推定时间，并排除当前会话、子代理和宿主注入记录。简报结合共享笔记、Git 和实际 GitHub 状态，最后给出一个行动。Cursor 云代理与其他外部资料来源仍需接入。

[`workbench.pickup_history`](../skills/pickup/SKILL.md) 接收选定的本地分支、GitHub 远端分支或 PR，可附前一轮 `recall` 简报和当前 iBoard 任务回执。它保留旧会话的决策，重新核对实际提交、工作树和 PR 状态，按原目标给出接续检查点。接续盘点本身只读，不重新执行已留存的测试。

[`workbench.skill_authoring`](../skills/author-skill/SKILL.md) 支持编写技能并对固定候选检查元数据、相对链接和跨技能引用。可在两个独立断网容器中运行已提交结构用例。任务关联验证回执后，发布和合并会复核其提交、引用和原始运行记录。

[`workbench.pr_brief`](../skills/opening-a-pr/SKILL.md) 为已独立复验的任务生成少于 40 行的 PR 正文及约定格式标题。简报绑定候选、基线、目标仓库和复验摘要。支持同一 fork 中明确指定的父 PR，并在远端写入前复核父 PR 与基线提交。发布后回读标题、正文和 ready 状态。原始日志保留在任务记录，现有栈交付流程继续负责依赖顺序。

[`workbench.perf_issue`](../skills/perf-issue/SKILL.md) 在修改前冻结基准、负载脚本、同提交对照和热点。自动复验按同一契约交替运行原版与候选，核对完成的工作量、结果摘要、原始数值和相应源码。改进未达到冻结阈值、端到端退化或证据更改均阻断交付，PR 正文引用实际前后数值与证据摘要。

[`workbench.blast_radius`](../skills/blast-radius/SKILL.md) 使用精确 diff 和指定下游源码提出影响假设，再将批准的探针绑定到具体前提。只有两次不同断网容器和独立评审支持的事实才标为已证明。重复失败和未检验项分别保留。任务关联报告后，发布再次核对原任务、提交、探针和原始证据。

[`workbench.no_comments`](../skills/no-comments/SKILL.md) 从 Git 工作差异和指定文件收集真实注释，通过独立审查给出删除选项、保留依据和后续行动。应用只改获选普通注释，并复核语法树、工作区和暂存区。支持 Python、JavaScript 与 TypeScript。抑制检查的根因修复和把约束编码成测试属于后续验证任务。
