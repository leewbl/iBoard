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

只读调研可用 [`workbench.investigate`](../skills/investigate/SKILL.md) 或 Claude Code 的 `/agent-workbench:investigate` 启动。指定提交和文件即可运行 `how`、`why`、`teach` 或 `investigation`。合成模式分别向源码与历史阶段提供证据，再汇总为统一说明，保留两边的未知项。报告记录实际引用的源码行和提交、模型调用时间及提示词哈希。历史阶段可用 `--pr` 纳入用户明确选定、能与所选 Git 历史关联的 PR 正文和有界讨论摘录。

接续或查看已留存任务时，使用 [`workbench.pickup`](../skills/pickup/SKILL.md) 或 `workbench.resume --inspect`。工具观察现有任务锁，核对工作树、输入和证据哈希，区分已记录的发布与合并结果。可用 `--since` 查看指定时间后的事件，用 `--check-pr` 查询对应 PR 的当前提交和检查状态。该入口提供接续判断，执行仍经过原有恢复和交付门禁。

长任务的阶段决定保存在任务目录的 `decisions.tsv`。每行记录时间、阶段、决定、理由、证据和结果。[`workbench.decisions audit`](../skills/show-me-your-work/SKILL.md) 核对保留文件的哈希和 Git 提交，显示哪些链接仍需在线查看。新的运行会列出继承记录的时间范围，人工可追加说明或纠正。

Claude Code 执行时会为该次运行保留可见回复、工具调用名称和结果状态，也保留直接 GLM 评审的回执。用 `workbench.decision_review` 选择一轮运行和可用的 OpenAI 模型，逐条对照决策、回执和哈希绑定的报告。受限的 Codex CLI 会话只接收该轮的材料。结果必须引用实际证据，运行或报告在审查期间发生变化则放弃本轮结论。已导入的外部会话单独注明来源。跨供应商复核由技能显式发起，OpenClaw 发布门禁仍使用其独立的断网验收。

使用 [`workbench.pr_watch`](../skills/babysit/SKILL.md) 查看明确指定的 GitHub PR。支持一次检查、只看评审讨论，以及在限时内等待 CI 或审批。可以从一个已选 PR 识别开放的依赖栈，也可以按父 PR 在前的顺序指定成员。工具核对父子分支和提交，并按冲突或基线变化、未解决讨论、检查失败的顺序定位阻断。观察结果同时记录 head 和 base。仓库中没有观察到检查时会明确显示，iBoard 的独立复验和合并仍有各自的门禁。

[`workbench.verify_pr`](../skills/verify-pr/SKILL.md) 可为已存在的 GitHub PR 创建只用于复验的任务。它在新工作树中固定原始条件和两个提交，在父提交运行基线场景，再切到已存在的候选提交运行断网检查和独立终审。验证失败时保留报告和候选现场。`workbench.stack_verify` 对照当前 PR、原始输入、证据哈希和独立报告，从根 PR 开始找出连续通过的范围。

[`workbench.stack_ship`](../skills/shipping/SKILL.md) 可把一条已验证的依赖路径固化为交付计划，并在获授权的个人 fork 上自下而上执行。每次写入前重新核对任务记录和合并门禁，随后读取 GitHub PR 和主分支确认结果。父 PR 快进合入时，子 PR 可改为指向同一提交的主分支。若主分支提交不同，`workbench.stack_rebase verify` 在独立工作树重放下一项，保留其原始请求、验收历史与本地门禁，对新父提交和候选重新取证。明确授权的 `update` 命令核对主分支和旧 head 后更新原 PR。两阶段均保留可审计的回执。

[`workbench.stack_autopilot`](../skills/autopilot-stack/SKILL.md) 从本地清单冻结一组已选 PR 的请求、验收条件、head 和 base。工作台按父子顺序独立复验，也能复用当前版本的已验收任务。交接前逐项核对当前 CI 或本地门禁，记录缺口。恢复时沿用冻结的输入和从首次启动起计算的总时限。此入口停在交接。

[`workbench.arena`](../skills/arena/SKILL.md) 在同一基线生成多个代码方案。独立工作树并发改动，断网沙箱按顺序运行同一条冻结检查。GLM 与 OpenAI 按公开评分标准评阅去掉身份信息的候选补丁，并记录分歧处理和取舍。综合候选通过新一轮沙箱和独立终审后才导出补丁。

[`workbench.project_verify create`](../skills/create-verification-skill/SKILL.md) 从源码锚点、用户路径和现有驱动生成项目验证技能与功能地图。固定 Git 版本和本地镜像，启动 doctor 和一条真实场景后才导出可交接的技能。[维护入口](../skills/maintain-verification-skill/SKILL.md) 核对地图与源码，逐项推动真实场景，发现失败后重新运行 doctor。修订版要经过全图复验才导出差异。OpenClaw 地图涵盖 Gateway、技能资格和配置校验，配置相关代码变更还要求将配置验证的双份断网证据纳入终审。

| 能力 | iBoard 状态 | 等效能力的验收条件 |
| --- | --- | --- |
| 项目验证和功能地图 | 可按源码和驱动生成项目验证技能。OpenClaw 已覆盖 Gateway、技能资格和配置校验。 | 补充有状态浏览器、PTY 和其他项目的运行证据。对每条用户路径验证可见结果、必要副作用和清理。 |
| 功能地图维护 | 巡检逐条核对来源与实际运行。已绑定的命令注册可查出缺项和过时项，技能修订经全场景验证后导出。对声明了失败信号的场景可用双驱动和同一运行时区分脚本与产品问题。 | 继续为更多场景补独立探针和失败信号，覆盖动态命令与其他类型的用户入口，并接入实证后的远端交付。 |
| Bug 修复 | 有双份基线复现、修复、候选复验及 PR 门禁。 | 用同一场景复现并对照候选，确认故障机制，检查影响范围，缺失复现或必要证据时暂停。 |
| 需求、原型与架构 | 可生成并复核复杂设计，编译有先后依赖的多 PR 计划，以提交和运行证据核对单元、真实场景和性能。交互变化保留操作者审阅。 | 将计划纳入实际任务派发、原型与 PR 交付，在真实用户入口验收可见行为和副作用。 |
| 性能、运行诊断与形式化验证 | 基准可固定版本、负载和证据。逐次优化可调用模型实现单项假设，按性能和独立正确性保留或回退，复测保留链后导出。 | 配齐实际业务负载、发布构建、持续生成性能假设与自动交付。运行诊断与形式化检查仍需扩展。 |
| 影响范围、代码约束与持续纠错 | 新任务的独立复验记录受影响入口、需要保持的行为和候选源码位置，并引用独立沙箱报告。发布和合并再次核对位置、提交与报告哈希。 | 继续补跨模块的选测和回归设计。对重复错误新增能抓住旧错误的可执行约束，并把验证保留给后续任务。 |
| 自主运行、暂停与恢复 | 有按任务预算、锁、恢复入口和修复循环。 | 在受控失败、工具超时及中断后安全恢复或明确阻断，回放不会重复写入远端，任务状态与产物一致。 |
| 并行研究、方案比较和任务分派 | Arena 支持独立工作树并发生成代码候选、跨供应商评分、综合和复验。 | 扩到多类产物、一般任务分派和大范围方案比较，存在共享写入时可安全排序。 |
| PR 创建、跟踪、合并和 PR 栈 | 已有单 PR 创建与个人 fork 合并门禁。交付入口可逐项落地已验证的快进链，也可在不同合并结果后重放并复验下一项。 | 在真实 GitHub 栈完成写入演练，并串联多层基线变更后的连续交付。 |
| 调研、复盘、交接和工作流生成 | 已有只读调研和任务接续入口。可关联指定 GitHub PR，并从任务记录和实际状态还原接续点。 | 扩展到明确指定的会话材料，并补复盘后生成技能。 |
| 宿主集成 | iBoard 提供 Claude Code 插件和 Python 入口。 | 对照 [pstack 的 Claude Code 适配说明](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/docs/harness/claude-code.md)，对依赖 Cursor Automations、云代理或 Bugbot 的能力提供独立实现，或者记录具体的外部依赖及真实运行证据。 |

公开功能目录是核对入口。每个功能仍需单独记录指令、测试环境、通过条件和运行证据。Lauren Tan 的生产案例用于理解工作方式，交付规模与团队内部环境另需实际测量。

通过 `workbench.start` 新建的任务采用 `impact_review: runtime-v1`。独立复验者可以只读检查未修改的调用方，并在终审 JSON 的 `impact` 中记录入口、需要保持的行为、候选文件行号、观测结果和报告引用。发布门禁会拒绝只有代码差异、基线结果或实施者报告的影响证明。报告身份由程序核对，证据是否覆盖所述行为仍由独立复验者判断。既有任务沿用创建时的门禁和记录格式。
