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

iBoard 原生的[阶段提示词](../workbench/prompt_profiles.py)已接入 bug/feature 规划、基线审查、实现、修复和终审。模型回执的 `workflow_guidance` 记录来源路径和实际所用提示词的哈希。目录用于查询，上游文档和脚本不会被直接交给执行器。

[`/agent-workbench:poteto-help`](../skills/poteto-help/SKILL.md) 或 `python3 -m workbench.pstack_help` 可查询场景、技能和工作流程。查询结果给出本地命令、指南、所需证据和适用条件。只纳入阶段提示词的条目标成 `guidance-only`，缺少经核对的本地入口时显示 `catalog-only`。

[`workbench.refactor`](../skills/refactor/SKILL.md) 在重构前冻结源提交、预期行为、驱动和允许改动的路径。它在断网 Docker 中使用同一份驱动，逐提交比较退出码和 stdout、stderr 原始字节。证据不完整或同一版本两次运行不一致时，结果记为未确定。`workbench.start --kind refactor` 将对照报告送入独立终审，创建、更新、合并 PR 前会重新核对报告、原始回执和同一份终审。

[故障根因验证](../skills/bug-cause/SKILL.md) 可随新任务启用。工作台把故障触发与单参数对照绑定到同一提交的驱动，在编辑前独立判断对照能否排除一个竞争解释。修复后在相同入口重跑两组检查，原故障信号仍出现、驱动改变或回执缺失都会阻断交付。

只读调研可用 [`workbench.investigate`](../skills/investigate/SKILL.md) 或 Claude Code 的 `/agent-workbench:investigate` 启动。指定提交和文件即可运行 `how`、`why`、`teach` 或 `investigation`。合成模式分别向源码与历史阶段提供证据，再汇总为统一说明，保留两边的未知项。报告记录实际引用的源码行和提交、模型调用时间及提示词哈希。历史阶段可用 `--pr` 纳入用户明确选定、能与所选 Git 历史关联的 PR 正文和有界讨论摘录。

接续或查看已留存任务时，使用 [`workbench.pickup`](../skills/pickup/SKILL.md) 或 `workbench.resume --inspect`。工具观察现有任务锁，核对工作树、输入和证据哈希，区分已记录的发布与合并结果。可用 `--since` 查看指定时间后的事件，用 `--check-pr` 查询对应 PR 的当前提交和检查状态。该入口提供接续判断，执行仍经过原有恢复和交付门禁。

[`workbench.run_control hold`](../skills/pause-safely/SKILL.md) 可在另一进程暂停指定任务。已经开始的编辑步骤可以留下正常提交和检查点，后续验证、依赖派发和 GitHub 写入会检查暂停记录。查看确认停点后，显式解除才能按原任务预算恢复。

[`workbench.autonomous_run`](../skills/autonomous-run/SKILL.md) 固定选定任务的验收、发布或合并目标，沿用原输入、预算和交付授权。CI 未结束时保留 PR、候选和基线，释放任务执行锁。下一次观察只查询该检查点，发现明确结果后再交给原有验收或修复流程。工作台保留每次重新启动、等待及最终证据。

[`workbench.autopilot_full`](../skills/autopilot-full/SKILL.md) 把已冻结的独立任务加入队列，并发推进各自的验收、PR 或合并目标。整组暂停在下一次操作前生效，正在执行的步骤可留下提交和检查点，解除后按各任务的原预算恢复。队列保留每项结果，同一仓库的写入按顺序通过原门禁。对于基线前移，可用 `--reverify-stale-main` 触发任务重放和复验。状态回执绑定远端 `main`、原任务、补丁和新的独立报告。使用 `--deliver-replayed` 可按原授权创建 PR 或更新原 PR，通过绑定到新提交的 CI 或原 OpenClaw 本地门禁后再合并。重启时用交付日志核对远端结果。发布后若主分支再次前移，队列暂停该项。

[并行验收](../skills/swarm/SKILL.md) 在独立工作树和断网容器中运行固定提交的检查。覆盖模式要求每条路径都有通过证据，竞选模式按事先选定的规则比较候选。用 `--swarm-manifest` 可把固定路径纳入正常任务的交付门禁。审计逐项核对收据哈希、提交、命令、结果和清理。 同一技能另提供限定源码范围的并行模型调研。Claude Code + GLM 或 Codex 各自接收批准片段，引用须与 Git 原文一致。竞选可采用首个合格结果，或按冻结评分表独立盲评。模型调研结果独立于运行时验收门禁。

长任务的阶段决定保存在任务目录的 `decisions.tsv`。每行记录时间、阶段、决定、理由、证据和结果。[`workbench.decisions audit`](../skills/show-me-your-work/SKILL.md) 核对保留文件的哈希和 Git 提交，显示哪些链接仍需在线查看。新的运行会列出继承记录的时间范围，人工可追加说明或纠正。

Claude Code 执行时会为该次运行保留可见回复、工具调用名称和结果状态，也保留直接 GLM 评审的回执。用 `workbench.decision_review` 选择一轮运行和可用的 OpenAI 模型，逐条对照决策、回执和哈希绑定的报告。受限的 Codex CLI 会话只接收该轮的材料。结果必须引用实际证据，运行或报告在审查期间发生变化则放弃本轮结论。已导入的外部会话单独注明来源。跨供应商复核由技能显式发起，OpenClaw 发布门禁仍使用其独立的断网验收。

使用 [`workbench.pr_watch`](../skills/babysit/SKILL.md) 查看明确指定的 GitHub PR。支持一次检查、只看评审讨论，以及在限时内等待 CI 或审批。可以从一个已选 PR 识别开放的依赖栈，也可以按父 PR 在前的顺序指定成员。工具核对父子分支和提交，并按冲突或基线变化、未解决讨论、检查失败的顺序定位阻断。观察结果同时记录 head 和 base。仓库中没有观察到检查时会明确显示，iBoard 的独立复验和合并仍有各自的门禁。

[`workbench.verify_pr`](../skills/verify-pr/SKILL.md) 可为已存在的 GitHub PR 创建只用于复验的任务。它在新工作树中固定原始条件和两个提交，在父提交运行基线场景，再切到已存在的候选提交运行断网检查和独立终审。验证失败时保留报告和候选现场。`workbench.stack_verify` 对照当前 PR、原始输入、证据哈希和独立报告，从根 PR 开始找出连续通过的范围。

[`workbench.stack_ship`](../skills/shipping/SKILL.md) 可把一条已验证的依赖路径固化为交付计划，并在获授权的个人 fork 上自下而上执行。每次写入前重新核对任务记录和合并门禁，随后读取 GitHub PR 和主分支确认结果。父 PR 快进合入时，子 PR 可改为指向同一提交的主分支。若主分支提交不同，`workbench.stack_rebase verify` 在独立工作树重放下一项，保留其原始请求、验收历史与本地门禁，对新父提交和候选重新取证。明确授权的 `update` 命令核对主分支和旧 head 后更新原 PR。两阶段均保留可审计的回执。

[`workbench.stack_autopilot`](../skills/autopilot-stack/SKILL.md) 从本地清单冻结一组已选 PR 的请求、验收条件、head 和 base。工作台按父子顺序独立复验，也能复用当前版本的已验收任务。交接前逐项核对当前 CI 或本地门禁，记录缺口。恢复时沿用冻结的输入和从首次启动起计算的总时限。此入口停在交接。

[`workbench.arena`](../skills/arena/SKILL.md) 在同一基线生成多个代码方案。独立工作树并发改动，断网沙箱按顺序运行同一条冻结检查。GLM 与 OpenAI 按公开评分标准评阅去掉身份信息的候选补丁，并记录分歧处理和取舍。综合候选通过新一轮沙箱和独立终审后才导出补丁。

通过 [Feature 交接](../skills/feature-handoff/SKILL.md) 启动的新任务可固定已选设计、允许改动的文件、四项拆分检查、原型结论和验收条件。给出已经批准的两种架构时，可由 `workbench.start` 运行 Arena，对照相同的验收命令盲评补丁，复验并采用胜出提交。原报告有分歧时，发布还要求 [Interrogate](../skills/interrogate/SKILL.md) 的探针回执。两份独立运行和两家模型评审必须指向同一候选，评审时仍需逐项核对最终运行条件。

通过 [多阶段派发](../skills/multi-phase-plan/SKILL.md) 显式授权计划后，iBoard 先测原始主线，再按依赖创建各自的任务和工作树。计划可授权最多四个阶段并发实现。每个阶段保留独立的记录，基线测量和候选验证逐项运行。同一阶段的单元测试、真实入口和候选性能绑定到同一个镜像。交互改动需要对具体提交和证据审批，才能释放依赖阶段。`workbench.phase_ship` 核对声明的个人 fork、具体 PR 和前一阶段落地提交后，继续交付下一阶段。需要更新基线时，`workbench.phase_reverify` 保留补丁和原验收条件，在独立工作树重新取证。交互审批随候选提交重新确认。

[`workbench.project_verify create`](../skills/create-verification-skill/SKILL.md) 从源码锚点、用户路径和现有驱动生成项目验证技能与功能地图。固定 Git 版本和本地镜像，启动 doctor 和一条真实场景后才导出可交接的技能。[维护入口](../skills/maintain-verification-skill/SKILL.md) 核对地图与源码，逐项推动真实场景，发现失败后重新运行 doctor。修订版要经过全图复验才导出差异。OpenClaw 地图涵盖 Gateway、技能资格和配置校验，配置相关代码变更还要求将配置验证的双份断网证据纳入终审。

[`workbench.formal_verify create`](../skills/create-formal-verification-skill/SKILL.md) 可生成绑定源码提交、属性、验证工具、探索边界和反例的项目技能。创建和[维护](../skills/maintain-formal-verification-skill/SKILL.md)都会在临时工作树故意打破一项属性，检查验证器能否抓到反例，随后复验未改动的源码。维护保留旧验证器结果、变更与移动记录。修改已确认的属性和边界需要明确的意图与批准证据。OpenClaw [队列示例](../examples/formal/openclaw-command-lane/README.md) 穷举容量为一时三条命令的 40 种调度，并提供同一属性的有限 TLA+ 模型。

[`workbench.statechart check`](../skills/statechart/SKILL.md) 在断网 Docker 中运行固定 pstack 引擎和 TLC，导出最短违规路径、原始输出、Mermaid 和 Pele SVG。OpenClaw 模型在正常配置下验证了 16 个状态，放宽容量限制后，两次入队即触发违规。[模型检查契约](../examples/formal/openclaw-command-lane/manifest-model.json) 将这条容量判断绑定到源码，再用独立 Node 驱动重放实际队列。证明助手的实际接入仍待验证。

[OpenClaw 连接态验证](../skills/verify-openclaw/SKILL.md) 从固定镜像启动临时 Gateway，用浏览器登录 Control UI，再核对 `sessions.list` 与截图。会话改名场景还检查取消时没有写入、保存时 Gateway 确认写入，以及 Gateway 重启后从新浏览器读回同一会话的新名称。涉及会话界面的改动必须提交两套通过证据。发出 PR 前，门禁会再次核对镜像、提交、独立执行身份、Gateway 重启记录和截图哈希。

| 能力 | iBoard 状态 | 等效能力的验收条件 |
| --- | --- | --- |
| 项目验证和功能地图 | 可按源码和驱动生成项目验证技能。OpenClaw 覆盖 Gateway、技能资格、配置校验，以及 Control UI 会话的读取、取消改名和重启后回读。 | 扩到其他界面动作、PTY 和其他项目。对每条用户路径验证可见结果、必要副作用和清理。 |
| 功能地图维护 | 巡检逐条核对来源与实际运行。已绑定的命令注册可查出缺项和过时项，技能修订经全场景验证后导出。对声明了失败信号的场景可用双驱动和同一运行时区分脚本与产品问题。 | 继续为更多场景补独立探针和失败信号，覆盖动态命令与其他类型的用户入口，并接入实证后的远端交付。 |
| Bug 修复 | 有双份基线复现、修复、候选复验及 PR 门禁。 | 用同一场景复现并对照候选，确认故障机制，检查影响范围，缺失复现或必要证据时暂停。 |
| 需求、原型与架构 | 可把选定设计及原型结论绑定到正式 Feature 任务。两种方案可独立实现和盲评，胜出提交纳入静态评审、断网验收和边界探针。可按多阶段计划派发本地任务。 | 串联多阶段验收与远端 PR 栈落地，让评审能主动提出新的功能边界，并覆盖更多实际用户入口和副作用。 |
| 性能、运行诊断与形式化验证 | 基准可固定版本、负载和证据。逐次优化可按性能和独立正确性保留或回退。属性验证支持有界真实代码检查及 TLC 模型检查，保留源码同步历史、变异反例、状态图及独立重放。 | 配齐业务负载、发布构建和自动交付。继续验证其他证明工具和业务属性。 |
| 影响范围、代码约束与持续纠错 | 新任务的独立复验记录受影响入口、需要保持的行为和候选源码位置，并引用独立沙箱报告。发布和合并再次核对位置、提交与报告哈希。 | 继续补跨模块的选测和回归设计。对重复错误新增能抓住旧错误的可执行约束，并把验证保留给后续任务。 |
| 自主运行、暂停与恢复 | 有按任务预算、锁、恢复入口和修复循环。 | 在受控失败、工具超时及中断后安全恢复或明确阻断，回放不会重复写入远端，任务状态与产物一致。 |
| 并行研究、方案比较和任务分派 | Arena 支持独立工作树并发生成代码候选、跨供应商评分、综合和复验。 | 扩到多类产物、一般任务分派和大范围方案比较，存在共享写入时可安全排序。 |
| PR 创建、跟踪、合并和 PR 栈 | 已有单 PR 创建与个人 fork 合并门禁。交付入口可逐项落地已验证的快进链，也可在不同合并结果后重放并复验下一项。 | 在真实 GitHub 栈完成写入演练，并串联多层基线变更后的连续交付。 |
| 调研、复盘、交接和工作流生成 | 已有只读调研和任务接续入口。可关联指定 GitHub PR，并从任务记录和实际状态还原接续点。 | 扩展到明确指定的会话材料，并补复盘后生成技能。 |
| 宿主集成 | iBoard 提供 Claude Code 插件和 Python 入口。 | 对照 [pstack 的 Claude Code 适配说明](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/docs/harness/claude-code.md)，对依赖 Cursor Automations、云代理或 Bugbot 的能力提供独立实现，或者记录具体的外部依赖及真实运行证据。 |

公开功能目录是核对入口。每个功能仍需单独记录指令、测试环境、通过条件和运行证据。Lauren Tan 的生产案例用于理解工作方式，交付规模与团队内部环境另需实际测量。

[`workbench.worktree_cleanup`](../skills/worktree-cleanup/SKILL.md) 可先只读盘点 Git 工作树，再冻结明确批准的路径。删除前逐条复核原始基线、任务记录、当前使用清单、开放 PR 和文件状态，保留分支并记录实际删除和磁盘观测。宿主会话自动盘点、IDE 与 Xcode 资源回收仍待接入。

[`workbench.blind_eval`](../skills/blind-eval/SKILL.md) 给各方案同一个任务，在独立目录运行 Claude Code + GLM。评审通过匿名代码、成功的实际文件读取和断网 Docker 运行结果给分。两个独立 OpenAI 会话使用同一份评分标准。审计可离线重建评审输入并核对回执。此入口已支持固定文本源文件的对比，其他宿主轨迹和媒体产物待接入。

[`workbench.correct`](../skills/correct/SKILL.md) 将重复错误对应到实际历史提交与所选防护层级。在断网 Docker 中，用同一探针核对每次错误、对应修复和当前实现。iBoard 的 OpenClaw 发布门禁现在为每条必测路径核对两个独立 Docker 容器；历史回放还检验了未登记回执和缺失 Gateway 重启两类防护。

[`workbench.figure_it_out`](../skills/figure-it-out/SKILL.md) 为没有现成流程的任务固定目标、变更范围和退出条件。实施者逐项尝试有上限的假设。只有当前功能、上一阶段行为和回归护栏都经双次断网执行及独立评审确认，才保留该提交。最后再复验整个产品行为，导出已验证的补丁序列及离线审计结果。

[`workbench.model_profiles`](../skills/setup-pstack/SKILL.md) 在调用后确认指定的模型与 CLI 推理配置，解析角色与预算。显式批准的配置在任务启动时固定，规划、实施、修复及独立评审按各自角色调用 Claude Code + GLM。恢复沿用任务快照。回执记录 CLI 确认的模型和请求的推理档位，服务端实际推理量未测量。

[Arena](../skills/arena/SKILL.md) 与 [Swarm 模型调研](../skills/swarm/SKILL.md) 可载入同一配置。Arena 从配置固定候选席位和不同供应商的评委，并把实际调用绑定到盲评和独立复验。Swarm 默认继承工作者角色，竞选可选择预算内已探测的不同模型，评委须来自批准的跨供应商池并在参赛模型之外。两者保留运行期配置快照，后续修改安装文件不会更换已批准的路由。

[Interrogate](../skills/interrogate/SKILL.md) 也可固定跨供应商评审席位。功能检查可以从批准的目录中按变更行挑选，保留用户指定的必测项。对同一候选用不同的断网容器复演，并核对原始输出。候选是否通过由可重复的实际观测和对观测的独立评审共同决定。未提交改动可以用 `workbench.interrogate_snapshot` 固定。它按明确选定的文件或工作区差异捕获当前内容，保留原分支与暂存状态。只读评审与断网复验共用这份快照，两个独立工作树会先核对文件字节和模式。

通过 `workbench.start` 新建的任务采用 `impact_review: runtime-v1`。独立复验者可以只读检查未修改的调用方，并在终审 JSON 的 `impact` 中记录入口、需要保持的行为、候选文件行号、观测结果和报告引用。发布门禁会拒绝只有代码差异、基线结果或实施者报告的影响证明。报告身份由程序核对，证据是否覆盖所述行为仍由独立复验者判断。既有任务沿用创建时的门禁和记录格式。
