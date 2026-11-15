# 外部方案核验与比较素材

检索日期：2026-11-07。以下是公开源码和作者文档核验，未安装或运行 pstack、DeepSeek Super-agent，也没有将二者放进 iBoard 十题测试集。技术机制以固定提交为边界；所有相同任务的处理过程均为基于公开机制的推演。

## 身份与版本

| 对象 | 核验结果 | 本次使用版本与日期 | 证据 |
|---|---|---|---|
| Lauren Tan 的 pstack | 官方 `cursor/plugins/pstack`。插件元数据作者为 Lauren Tan，README 以 poteto 自述，最新目录提交作者账号为 `poteto`。它是工程技能、工作手册和宿主适配的插件集合，可以归入 Agent Harness 的工作规程层；不应据此推断它自己实现了完整独立执行平台。 | `0.15.36`；固定提交 `c742dd61706fbacc1b95f1a20b7b988cf096ce6c`，2026-10-27 23:12:40 UTC。 | [P1](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/.cursor-plugin/plugin.json) · [P2](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/README.md) · [P9](https://github.com/cursor/plugins/commit/c742dd61706fbacc1b95f1a20b7b988cf096ce6c) |
| DeepSeek Super Agent | 对象可明确为 DeepSeek Harness Team 在官方组织发布的 `dsh-superagent-prototype`。它是面向一个 GitHub 仓库、可接 Feishu 的常驻编程 Agent 编排原型。发布组织是官方，但 README 明确将其限定为研究参考，并非正式 DeepSeek 产品或可直接视为生产就绪的软件。 | 包版本 `0.1.0`；固定提交 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`，2026-10-31 12:06:31 UTC。 | [D1](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/README.md) · [D9](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/package.json) |
| Barnaby Robson 博客 | 2026-10-05 的二手分析提供研究入口；不能替代 Lauren Tan 的源码。文中清点的是 `0.15.9 / e43c7ee`，与本次源码快照不同。 | 博客引用的完整旧提交 `e43c7ee26e0038c6c1fa8380dd34ce86ff94cb2a`。 | [B1](https://barnabyrobson.org/on-pstack/) |

版本差异会影响架构判断：当前 pstack 的 Orchestrate 手册使用宿主的 `Task`、Project tracker（`notes.md`）、Project store 和可用的 PR/CI 事件订阅。2026-10-27 的提交已删除旧 `orch` 脚本；不能把旧脚本编排画成当前实现。Claude Code 适配又将 Cursor 云执行映射为 worktree，Cursor 专属功能按适配规则跳过，因此两种宿主上的实际能力并不等同。[P4](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/orchestrate.md) · [P8](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/docs/harness/claude-code.md) · [P9](https://github.com/cursor/plugins/commit/c742dd61706fbacc1b95f1a20b7b988cf096ce6c)

DeepSeek README 同时声明：公开版经过适配，与内部版本、设计文章并不相同，文章中部分设计尚未进入公开原型。本报告只比较已固定的公开原型，不用媒体对“内部系统持续工作”的描述证明源码能力。[D1](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/README.md)

## 十个维度的可用比较

这里的 iBoard 指公开 `effc347`；十题结果及本地恢复补丁另行注明。iBoard 依据：[现有工程报告](evidence-index.md#local-evidence)。

| 维度 | 当前 iBoard | DeepSeek Super-agent 原型 | pstack 0.15.36 | 实际影响与证据 |
|---|---|---|---|---|
| 目标与交付 | 指定工程任务，经复现、修复、验证，再按策略交付个人 fork。 | 为一个仓库接受授权工作，持续回应原会话；PR 是一种产物。 | 给编程 Agent 提供严格的任务手册和验证习惯。 | 定位分别偏确定任务执行、常驻事件协调、工程工作规程。[D1](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/README.md) · [P2](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/README.md) |
| 任务入口与权限 | 明确指定 Issue 或本地材料；没有自动承接全部 Issue 的已启用队列。 | 来源事件先过宿主确定性准入，再做模型研判；策略版本与授权范围进入工单。 | `poteto-mode` 根据请求选手册，依赖宿主权限和工具。 | 不应把 pstack 的提示规程与 DeepSeek 的宿主授权检查等同。[D3](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/002-authorization.md) · [P2](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/README.md) |
| 规划与执行 | Python 固定阶段控制，模型生成计划、补丁和审查判断。 | 主 Session 负责路由；Executor 读原始上下文并作技术决策。 | bug-fix 规定调查、复现、设计、实现、同表面验证，再开 PR。 | 执行决策的分工不同，不能只按 Agent 数量评价。[D5](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/004-prompts.md) · [P3](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/bug-fix.md) |
| 调度与并行 | 十题正式串行；通用多任务资源调度尚未证实。 | 工单持有执行槽；配置并发限制，并为人的后续消息留额外槽。 | Orchestrate 规定每 PR 一名负责人、独立任务新 worker，先跑通一个再扩展。 | DeepSeek 有槽位机制；pstack 编排依赖宿主 Agent 能力；iBoard 并行仍属计划。[D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) · [P4](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/orchestrate.md) |
| 工具与隔离 | Claude Code 受限工具在宿主机；OpenClaw 测试在断网 Docker。 | 每工单独立 Session/目录，命令按操作系统用户权限执行；目录和账户包装不是安全边界。 | Cursor 云环境或本机工具；Claude Code 适配为独立工作树。 | 会话隔离、工作树隔离与进程/网络隔离不是同一件事。[D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) · [P8](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/docs/harness/claude-code.md) |
| 状态与持久化 | 任务 JSON、阶段记录、文件锁和证据文件；本地证据修订链未统一发布。 | SQLite 保存领域对象；Effects 将待执行操作持久化后调度，Outbox 负责消息回执。 | Orchestrate 使用宿主 tracker/store；记录机制随宿主而变。 | 可以借鉴操作意图先落盘，但不必为当前单任务直接引入大型调度平台。[D6](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/007-architecture.md) · [D7](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/store.ts) · [D8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/effects.ts) · [P4](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/orchestrate.md) |
| 验证与完成 | 提交、命令、镜像和证据绑定，独立验收后过代码门禁；评分与交付分离。 | 工单状态、Report 与 PR 关闭事件协调完成；证据要求属于团队可配置提示内容。 | 验证技能要求驱动实际应用和功能地图；Shipping 要求非作者验证者的当前变更结论。 | pstack 的验证知识值得复用；DeepSeek 的工单终态不自动证明问题语义已解决。[D2](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/001-domain.md) · [D5](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/004-prompts.md) · [P5](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/create-verification-skill/SKILL.md) · [P6](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/shipping.md) |
| 失败与恢复 | 保留失败记录、受限续跑；十题仍需外部协调与本地补丁。额度明确报错后按用户策略被动停止。 | 持久待办恢复；短暂提供商/网络错误含 429 可按规定重试，失败状态抑制自动输入直到解锁。 | 手册指导按原因换范围、换模型或重试；长期可靠性依赖宿主执行面。 | 不应照搬 DeepSeek 的 429 重试到 iBoard 的手动恢复策略。[D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) · [D8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/effects.ts) · [P4](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/orchestrate.md) |
| 合并与人参与 | Python 校验目标、PR 头、证据和适用检查；真实关闭、保护分支等仍须单独验证。 | 自动合并规则在 Delivery requirements 提示词中，宿主没有专门合并决策逻辑；默认无另一合并账户时由人合并。 | Shipping 要求独立判定、检查变更是否仍匹配，逐个合并并回读结果。 | iBoard 可以保留确定性门禁，不把“代理认为已就绪”直接作为写入许可。[D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) · [P6](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/shipping.md) |
| 评测与扩展 | 固定十题、分阶段计分、原始证据和独立复核；非无干预的统一版本对照实验。 | 来源、执行器、状态端口分离；有测试与开发检查，不等于提供同一 bug 集的成功率。 | Eval 手册要求候选环境脱敏、盲评、同尺度评分和轨迹核对。 | 当前只能比较机制，不能按 80/100、PR 数或宣传吞吐直接排名。[D6](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/007-architecture.md) · [P7](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/eval.md) |

## 同一 SQLite 启动锁问题的流程推演

场景固定为“SQLite 短暂锁竞争导致 Gateway 启动失败，修复后应自动恢复，并保留持续锁、关闭中行为的防护”。

| 阶段 | iBoard 的已留证路径 | DeepSeek 原型的机制推演 | pstack 的机制推演 |
|---|---|---|---|
| 接受任务 | 用户明确指定 #186875，固定代码和验收条件。 | GitHub 来源将请求转换为事件，准入与研判接受后建立 Ticket。 | 用户交给 `poteto-mode`，选 bug-fix 手册。 |
| 准备与复现 | 工作树与目标镜像固定，真实 SQLite 写锁制造故障。 | 准备命令建立目录/工作树，worker 自行调查。需要为此项目另配安全测试环境和锁竞争脚本。 | 先用匹配的 CLI/服务控制技能复现；若缺脚本能力，先建立项目验证技能。 |
| 修复与反馈 | Claude Code/GLM 实现，Python 控制阶段和预算；失败保留记录。 | Worker 实现并响应订阅的反馈；运行故障由宿主恢复机制与 worker 分担。 | 子代理调查和实现，负责人追踪证据；只保留运行证据支持的变更。 |
| 验证 | 基线和候选同路径比较，独立验证者复跑防护测试。 | 需要把上述行为写入团队验证要求；不能从 Ticket ready 推导已经完成双角色复验。 | 同表面重新验证；进入 Shipping 时非作者验证者检查变更，功能地图帮助覆盖可观察结果。 |
| 交付 | #186875 已有个人 fork PR #2 合并回执。 | 根据配置后的交付提示和 GitHub 规则开 PR、等待或合并；这里没有实跑结果。 | 按获授权的 Shipping 路径逐个合并并回读；这里没有实跑结果。 |

推演依据分别是 DeepSeek 的领域、执行和提示文档，以及 pstack 的 Bug fix、验证技能和 Shipping 手册。它描述“可以按哪些机制处理”，不声称二者已成功修复这道题。[D2](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/001-domain.md) · [D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) · [D5](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/004-prompts.md) · [P3](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/bug-fix.md) · [P5](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/create-verification-skill/SKILL.md) · [P6](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/shipping.md)

## 可借鉴机制与实施边界

1. **优先借鉴项目验证技能与功能地图。** 将每题临时脚本沉淀成可驱动、可诊断、能留证据的项目适配层，保留 iBoard 的提交与证据绑定。代价是维护功能入口和固定数据；先选择一题 CLI 与一题 UI，不一次性描述全部 OpenClaw。依据 pstack 验证技能；这是改进建议，不属于当前统一实现。[P5](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/create-verification-skill/SKILL.md)
2. **用持久操作记录修补实际恢复缺口。** 借鉴 DeepSeek Effects 的“先持久化意图，再执行外部动作”，给发布、合并、任务阶段写入增加可回读标识和恢复试验。是否需要 SQLite 应由并发和事务需求决定，单任务可先使用现有文件记录的最小改进。此处属于根据源码机制作出的设计推断。[D7](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/store.ts) · [D8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/effects.ts)
3. **把来源路由与任务执行解耦，延后常驻监听。** 未来新增 Issue 自动接单或多任务调度时，可借鉴 DeepSeek 来源端口、准入、工单/执行分离；当前用户只授权指定任务，因此无需先建“全天候自动找活”系统。[D3](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/002-authorization.md) · [D6](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/007-architecture.md)
4. **先做盲评与可比运行，再谈效果优劣。** pstack 的脱敏候选、同尺度盲评可以用于下一轮对照；统一模型、固定基线、预算、外部介入规则，另记评审一致性。当前 80/100 不能证明任一机制单独带来提升，也不能与外部作者月 PR 数比较。[P7](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/eval.md)

## 来源索引与未决范围

下表中的 pstack 文件均固定于 `c742dd61706fbacc1b95f1a20b7b988cf096ce6c`（2026-10-27），DeepSeek 文件均固定于 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`（2026-10-31）。日期表示所用源码快照；不把目录最近提交时间说成每个文件的首次发布日期。

| 编号 | 原始材料 | 支持的主要判断 |
|---|---|---|
| P1 | [P1](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/.cursor-plugin/plugin.json) | pstack 名称、作者、版本。 |
| P2 | [P2](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/README.md) | 插件定位、入口、宿主安装范围。 |
| P3 | [P3](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/bug-fix.md) | bug 修复先复现、同表面验证、证据约束。 |
| P4 | [P4](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/orchestrate.md) | 当前原生工具编排、工作归属、委派与监督。 |
| P5 | [P5](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/create-verification-skill/SKILL.md) | 项目验证技能、功能地图、实际驱动与证据。 |
| P6 | [P6](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/shipping.md) | 独立非作者验收、变更有效性和合并回读。 |
| P7 | [P7](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/skills/poteto-mode/playbooks/eval.md) | 盲评实验流程和轨迹核对。 |
| P8 | [P8](https://github.com/cursor/plugins/blob/c742dd61706fbacc1b95f1a20b7b988cf096ce6c/pstack/docs/harness/claude-code.md) | Claude Code 映射与 Cursor 专属功能边界。 |
| P9 | [P9](https://github.com/cursor/plugins/commit/c742dd61706fbacc1b95f1a20b7b988cf096ce6c) | 0.15.36 删除旧 orch 脚本的版本证据。 |
| D1 | [D1](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/README.md) | 研究原型身份、与内部版及文章的区别。 |
| D2 | [D2](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/001-domain.md) | 来源、授权、工单、执行与状态概念。 |
| D3 | [D3](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/002-authorization.md) | 准入、授权范围、上下文与权限分离。 |
| D4 | [D4](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/003-execution.md) | 工单执行、并发、合并策略、失败恢复。 |
| D5 | [D5](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/004-prompts.md) | 路由/执行器职责、可编辑交付要求。 |
| D6 | [D6](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/007-architecture.md) | Runtime、Engine、Store、Effects 与外部接口。 |
| D7 | [D7](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/store.ts) | SQLite 状态与事务代码。 |
| D8 | [D8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/effects.ts) | 持久操作、并发限制、重试、序号与代际匹配代码。 |
| D9 | [D9](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/package.json) | 包版本和运行依赖。 |
| B1 | [B1](https://barnabyrobson.org/on-pstack/) | 用户研究起点及旧快照，非当前机制的主要证据。 |

未决范围：两种外部方案在同一 OpenClaw 题集上的成功率、成本、时延和恢复效果均未测量；DeepSeek 内部实现与设计文章实验未纳入公开原型结论；pstack 在用户当前 Claude Code/GLM 配置下的兼容效果未运行验证。公开文档未提供与 iBoard 四阶段计分相同的结果，故不做性能或质量排名。
