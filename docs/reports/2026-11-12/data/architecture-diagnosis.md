# iBoard 当前架构诊断与执行轨迹

本次只读检查于 2026-11-07 完成，没有运行模型、Docker、产品测试或远端写入。以下代码行号来自公开主工作树 `effc347df42f45037dad0a4bbe2a2cb7e53d363a`；所有未来调整均为建议。

## 版本边界

| 对象 | 观察到的状态 | 可用范围 |
|---|---|---|
| 公开主工作树 | 分支 `main`，HEAD `effc347df42f45037dad0a4bbe2a2cb7e53d363a`；`README.md`、`HANDOFF.md` 有既有本地修改；`docs/`、`.DS_Store` 未跟踪 | 本轮当前运行模块没有未提交修改；新报告是文档输出，不能视为公开发行更新。 |
| 厂商停止实验 | `fix/provider-limit-signal`，`79aada0a8ffb2c53c7f792feee133a3aa71dc9f6` | 本地实现，非公开主提交。 |
| 证据修订实验 | detached HEAD，`502794d6acef56d687b7aa39241abfa8949402f5` | 本地实现，非公开主提交。 |
| 差异审查实验 | detached HEAD，`9a07449127bdd5d0c8edb5f5b91ddf25c4389671` | 本地实现，非公开主提交。 |
| SQLite gate 任务 | 使用 iBoard `3107dad1c8100015b4e54a82b1d961011691e9e6` | 已有候选的接纳、真实复验和个人 fork 交付；并非 gate 任务从零实现补丁。 |

## 真实组件边界

Harness 在本工程中指模型外侧的任务控制、工具限制、状态记录和验收程序。它不是模型本身，也不是独立部署的服务集群。

| 组件 | 实际职责和依赖 | 精确代码位置 | 当前状态和限制 |
|---|---|---|---|
| 本地插件与启动入口 | 插件给 Claude Code 提供调用方法；Python `start` 接收一个指定任务，校验材料和本地镜像，准备工作树，再同步调用执行器 | [start.start](evidence-index.md#local-evidence)、[插件清单](evidence-index.md#local-evidence)、[插件技能](evidence-index.md#local-evidence) | 有实现和局部真实运行。完整插件交互体验未据本轮记录全面验证；不自动领取全仓 Issue，附件需单独输入。 |
| 阶段执行器 | 单个 Python 调用链串联 plan、reproduce、implement、verify、delivery、close-issue；模型决定计划与补丁，代码校验合同和阶段跳转 | [autopilot.run](evidence-index.md#local-evidence)、[_run_locked](evidence-index.md#local-evidence)、[规划](evidence-index.md#local-evidence)、[修复循环](evidence-index.md#local-evidence) | 已实现并有试点。多主体是分角色的顺序执行与独立模型调用，不能据此画成持续并行的 Agent 服务。 |
| 任务状态和锁 | `task.json` 保存输入、验收、预算、事件、证据；`autopilot.json` 保存流程阶段和候选；`input-stage.json` 保存输入准备和执行设置；单文件临时写、fsync 后 replace；目录文件锁协调协作进程 | [cli.write](evidence-index.md#local-evidence)、[cli.init](evidence-index.md#local-evidence)、[task_lock](evidence-index.md#local-evidence)、[input-stage](evidence-index.md#local-evidence) | 单文件原子替换已实现；跨 JSON、Git、容器、远端操作不构成一个事务。锁只约束遵循同一机制的本地进程。 |
| 模型适配 | 宿主启动受限 Claude Code CLI；实现允许编辑，审查为只读；模型配置读取本地 Claude 设置；支持两个 GLM HTTPS 域；每次不保留 Claude 会话 | [claude_glm.execute](evidence-index.md#local-evidence)、[工具与CLI参数](evidence-index.md#local-evidence)、[终审路由](evidence-index.md#local-evidence) | 已实际调用。模型会话不承担持久任务状态；上下文由编排器重建。当前返回退出码、超时和文本，没有可审计 token/cost 字段。 |
| 独立工作树与验证镜像 | 从明确提交创建独立分支/工作树；镜像源码逐项匹配版本控制文件；候选镜像可叠加源码层 | [workflow.prepare](evidence-index.md#local-evidence)、[_verified_image](evidence-index.md#local-evidence)、[build_source_image](evidence-index.md#local-evidence) | 已验证；源码一致不保证预构建产物一致，不自动安装新依赖。 |
| Docker 执行器 | 本地容器、断网、只读根目录、可写任务工作区挂载、CPU/内存/PID限制；运行后保存退出、输出和清理结果 | [run_sandbox](evidence-index.md#local-evidence)、[容器参数](evidence-index.md#local-evidence)、[清理](evidence-index.md#local-evidence) | 真实运行已验证。`--read-only` 不表示 `/workspace` 挂载只读；需要任务工作区不含生产数据。没有全局资源队列。 |
| 证据与独立审查 | 保存真实失败后再判断；报告绑定提交、镜像、命令、执行者和 SHA-256；模型对每个验收条件引用原始证据，审查测试断言是否被削弱 | [run_check](evidence-index.md#local-evidence)、[先保存失败](evidence-index.md#local-evidence)、[审查](evidence-index.md#local-evidence)、[记录判定](evidence-index.md#local-evidence) | 已在试点使用；哈希证明文件身份，不能证明语义正确或统计独立。相同模型家族的分角色调用不等同于独立模型族验证。 |
| 恢复与计划修订 | paused 任务可校验输入后续跑；旧任务接纳创建新记录；已有完整接纳记录缺 stage 可受限修复；未产生成功证据的非 UI 接纳任务可替换计划 | [start.resume_task](evidence-index.md#local-evidence)、[adopt.create](evidence-index.md#local-evidence)、[adoption_stage_repair.repair](evidence-index.md#local-evidence)、[replan.replace](evidence-index.md#local-evidence) | 局部实现。有明确拒绝条件，不支持任意中断无条件恢复；不是全无恢复能力。 |
| 发布和合并 | 本地证据与远端候选头/基线/分支策略校验；仅登录用户个人 fork 的 main；本地门禁要求 Actions 关闭且无分支保护或活动规则 | [delivery.guarded_merge](evidence-index.md#local-evidence)、[远端身份](evidence-index.md#local-evidence)、[写后确认](evidence-index.md#local-evidence) | SQLite 个人 fork 已真实交付；受保护分支、UI证据远端发布和真实目标 Issue 自动关闭另有验证缺口。 |
| 十题汇总和分目录归档 | 固定题集评分器检查阶段、哈希与审定；本地 `results/` 是按题整理的证据副本与索引 | [collect_scores.py](evidence-index.md#local-evidence)、[题目目录](evidence-index.md#local-evidence) | 本轮实验已执行；不能画成公开主版本已自动生成每题三目录的内置模块。 |

## 控制、异常与预算的实际规则

- 计划是受限制的 JSON：复现、解决、测试命令及界面截图脚本；验收已存在时不得替换。它不是任意动态任务图。[规划合同](evidence-index.md#local-evidence)
- 问题先执行两个角色的基线检查，再由只读模型判断是否确为报告中的故障。候选阶段也顺序执行实现者和独立验证者检查。角色不同并不意味着容器同时运行。[两次复现](evidence-index.md#local-evidence)、[候选检查](evidence-index.md#local-evidence)
- 验证失败可进入修复循环；环境阻塞、预算耗尽、工作树漂移、普通模型调用失败等会暂停，不把全部错误交给模型无限重试。CI 失败修复有单独身份约束，公开说明仅有模拟测试证明该路径。[循环分流](evidence-index.md#local-evidence)、[CI修复](evidence-index.md#local-evidence)
- 实现或修复调用前写入 `implementing/repairing`；中断后保留工作树并拒绝盲目续跑。这是在不确定是否已修改/提交时有意停止，不等同于证明现有恢复足够自动。[中断检查](evidence-index.md#local-evidence)
- 时间按 `started_at` 实际流逝计算，暂停也计时；迭代数单独检查；公开 `enforce_budget` 没有金额计量。`cost: provider-limit` 是策略值，不是已测量费用。明确额度拒绝的分类和统一停止来自本地 `79aada0`。[预算](evidence-index.md#local-evidence)、[本地厂商分类](evidence-index.md#local-evidence)
- 一个任务有本地锁，不代表有跨任务调度、内存准入或分布式锁。当前十题串行无需先引入服务集群。

## 五条问题链及最小调整

| 编号与分类 | 当前实现与触发 | 问题证据 | 实际影响 | 建议最小调整 | 建议验收目标 |
|---|---|---|---|---|---|
| D1 恢复局部实现与状态语义债 | 多个文件和 Git 步骤分开提交；准备期间先建目录/工作树，再写完整任务；实现中断留在 implementing；完成后预算状态仍可为 running | `workflow.prepare` 322–343；`resume` 拒绝缺 input-stage；真实试点 `autopilot.phase=complete` 与 `task.status=running` 并存；README 明确缺完整事务恢复 | 恢复需要辨认各文件语义与残留资源，跨环境自动化难以只看一个状态决定下一步；部分边界明确停下 | 保留单机文件方案，增加统一状态查询和阶段迁移记录；每个阶段保存意图、外部操作身份、完成回执；对既有接纳修复入口统一入口和错误分类 | 在准备、模型修改、证据写入、发布前后各注入中断；恢复不丢证据、不重复写远端；无法判定时给可定位阻塞原因。不要把所有中断都自动重试。 |
| D2 已观察到终审阻塞 | 公开 diff 超过 30000 字节直接拒绝；提示超过 100000 字符拒绝；本地改成完整证据分片仍需模型完成审查 | `delivery.candidate_diff` 24–32；`autopilot._bounded_review_prompt` 435–438；#180815 补充完整审查 420.0189 秒超时、空 stdout；独立审计明确 native terminal review/delivery=false | 产品修复证据可成立，但原生交付链仍无法结束；单纯放大限制没有证明闭环恢复 | 将差异、命令、断言、验收条件分为有覆盖清单的审查单元，每单元保存可复用结果；总审查必须覆盖全部单元，失败保持未通过；统一总预算与单次预算 | 同一 #180815 候选在统一版本完成全部覆盖项，生成原生最终回执；故意遗漏分片必须拒绝；不得仅靠 PASS 或摘要替代源码和日志。 |
| D3 证据与计划修订范围不足 | 公开 replan 仅支持暂停非UI、无成功证据的接纳任务；后基线的UI脚本修订靠本地新链和协调 | `replan.replace` 87–103；本地 `artifact_revisions.register` 315；#180815 native-v9 与补充 lint-repair 采用不同记录链 | 新的验证脚本不能直接沿用旧基线成功证据，需要额外基线重放和出处检查；恢复复杂度转移到协调层 | 合入统一的验收不变、技术验证脚本可版本化修订机制；明确失效哪些证据并重新运行，不覆盖旧记录 | 改验证脚本后原基线证据自动失效；同命令重新做基线失败和候选成功；改验收条件须单独依据，不能冒充技术修订。 |
| D4 通用层夹带 OpenClaw 试点规则 | 日志摘要硬编码 `IBOARD_LOCK_*`；本地门禁是 OpenClaw Vitest JSON；CI守卫有 OpenClaw特定逻辑；模型端点和本地配置固定 | `_review_report` 412–419、`local_gate`、`delivery._openclaw_checks_pass` 533、`claude_glm.execute` 46–66 | 目前对试点有效；增加第二个项目/模型时可能同时触及编排、审查、交付，属于已可见的扩展耦合，尚未测量改造耗时 | 保留核心顺序阶段，把项目测试命令、报告解析、特定CI规则变成小型配置/适配接口；在第二个真实项目中验证抽取，不先设计插件市场 | 第二项目的测试规则接入只改项目适配和配置，原OpenClaw验收回归不变；记录实际改动文件和迁移工作量。 |
| D5 本地运行配置与资源依赖 | 任务记录和证据使用绝对路径；Docker镜像须本地存在；CLI依赖Claude设置和密钥文件；镜像叠源码不重建依赖；无全局资源准入 | `start.start` 218–226；`claude_glm.execute` 46–63；`workflow.prepare` 301–308；试点4GiB OOM、有效对照7168MiB；十题正式3072MiB串行 | 换机器或直接扩大并行可能先遭路径、依赖、内存问题；不能把它归因于模型不会修代码 | 先做版本固定的环境清单和预检、相对任务路径解析、可复现镜像构建；第二机器完成非UI+UI各一题后，再加入本机有限并发与内存排队 | 干净第二环境仅凭文档准备和执行，生成同结构证据；两题并发分别停止/恢复不串题且总内存受限。 |

以上 D1 的跨文件损坏风险没有本轮注入复现实验，不能写成已发生数据丢失；已发生的是受限恢复、外部协调与状态解释负担。D2 是明确观察到的终审阻塞，超时底层原因还不能仅凭 stderr 中的 `unrecognized_model` 提示断言是模型不兼容或服务故障。

## 值得保留与暂不建设

有效设计是先复现后修复、同一提交和镜像对照、失败日志先保存、独立角色重新执行、验收修订留依据、交付前后回读。它们已经支持特定任务的证据闭环。需要保留简单的本机 Python 顺序阶段和 Git 工作树；当前没有因分布式服务不足而失败的证据。

当前不应优先建设：大规模 Agent 编队、通用动态工作流引擎、跨机器消息总线、向量长期记忆、全仓自动领取 Issue。这些是未来需求候选，不能作为现有架构缺陷。先让原生终审、恢复和第二环境复跑成立。

## SQLite #186875 的真实执行轨迹

依据 [原始任务](evidence-index.md#local-evidence)、[流程状态](evidence-index.md#local-evidence)、[运行设置](evidence-index.md#local-evidence)、[公开摘要](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/evidence/openclaw-186875-local-validation.json)。下表 UTC 时间来自原始记录。该 gate 任务是**接纳旧候选后重新验证和交付**，`iterations=0`；旧实现阶段不应补写成此任务新执行的步骤。

| 步骤与时间 | 输入与状态 | 实际动作和产物 | 判定方式与边界 |
|---|---|---|---|
| 创建接纳任务 19:22:47 起 | 旧任务、原基线 `edca4aee…`、现存候选 `54710b1…` | 19:22:52 `legacy-adoption-created`；暂停等待显式本地门禁设置 | 保留旧任务，不导入旧PASS；不是模型新生成补丁。 |
| 配置与恢复 19:23:49–51 | 四个指定Vitest防护文件，验收摘要，7168MiB镜像设置 | `local-gate-selected` 后 `resume` | 时间/迭代上限null，单次操作仍限时；不借此改验收。 |
| 基线复现 19:24:31、19:25:52 | 同一基线镜像、真实外部 SQLite `BEGIN IMMEDIATE` 锁 | 两个角色分别运行43.631秒、41.622秒；真实Gateway锁竞争后自行退出1；测试脚本确认故障所以报告exit0 | 脚本成功确认故障不等于产品成功；后续baseline-review模型exit0。 |
| 接纳候选 19:29:31 | 基线复现已获语义确认、候选为基线后继 | `candidate-adopted`，进入候选验证 | 代码控制快进和提交一致性。 |
| 解决行为 19:31:38、19:39:22 | 源码核对后的候选镜像 | 两个角色分别检查短暂锁释放后恢复、持久锁仍失败、现存恢复日志与关停保留；另有重复运行日志 | 短暂场景第3次尝试恢复，`/readyz`200只在锁释放后；持久锁4次后真实失败，未伪装成功。 |
| 双角色防护 19:44:20、19:47:36 | 精确四文件109用例 | 两份JSON，各109通过；19:50:08登记 `local-gate-run` | 不代表全平台完整CI；模型审查还逐项核对10个改变的测试/辅助文件。 |
| 只读终审 | 原问题、验收、差异、报告和脚本 | `final-review-model.json` exit0，scope=`read-only workspace inspection and supplied evidence`；独立审查绑定候选 | 这是有运行回执的原生终审，不是只有本地评分。 |
| 交付 20:01:01 | PR #2、个人fork main、关闭Actions、本地门禁 | 自动发布PR并确认合并；`delivery.merged=true`；最终`phase=complete` | `issue.closed=false`，因为原Issue属于上游OpenClaw而非目标个人fork。`task.status=running`仍只是现有预算控制字段。 |

没有并行执行证据；时间表展示的是顺序运行。此轨迹证明限定的 Linux 新状态 Gateway、真实SQLite竞争、无生产账号、个人fork交付，不证明生产实例或全平台回归。

## #180815 的成功与阻塞必须同时保留

[独立最终审计](evidence-index.md#local-evidence) 记录同命令基线缺少键盘绕过路径、候选有该路径、焦点和窄屏行为与回归证据，支持冻结评分的10分。原生保留头是 `dd0e84b9…`，补充候选为 `72ef11ed…`；补充 Claude Code/GLM 改动和独立审定不能写成原生流程终审完成。

[补充终审回执](evidence-index.md#local-evidence) 为 `timed_out=true`、`exit_code=null`、空stdout、420.018873875秒。审计记录完整差异126468字节，超过公开30000字节上限。完整差异分片能保证材料被完整装入证据包，但没有证明模型实际审完所有片段。应把“材料完整”“原生最终审查通过”“评测独立审定”“远端交付”分列。

此题最直接的改进实验是：保持同一基线、候选、验收与验证证据，比较统一版分段审查能否在固定总预算内产出完整覆盖的原生最终回执。该实验检验审查闭环，不重新计算修复质量提升，也不能通过降低验收条件或少读材料取得成功。

## TODO 与 Mock 检查范围

对 `workbench/`、`model/`、`runner/` 与相关说明检索 TODO/FIXME/占位词，未发现可据此认定主链路是空壳的生产占位函数；异常类型的 `pass` 和异常清理中的 `pass` 不是缺失实现。测试里的 mocks 是隔离外部依赖的单元测试结构，不能单独当成产品运行造假或缺陷。明确未接入主自动流程的另有 `run-claude-sandbox.sh`：公开README将其定位为早期隔离试验入口。实际OpenClaw UI实验的模拟Gateway与合成聊天数据应保留实验边界，不能外推为真实模型/外部频道集成。

## 最小目标架构建议

1. **保留**单机入口、顺序阶段、工作树、Docker运行器、证据哈希和交付前后核对；不拆为微服务。
2. **调整**状态层：统一对外状态和恢复动作，内部仍可保存多个小文件；记录阶段操作身份，拒绝不确定副作用的盲重放。
3. **调整**审查层：完整证据清单、分项覆盖与有界审查回执；将三处本地实验合入一个可固定版本后重验。
4. **调整**项目适配：隔离 OpenClaw 日志/门禁/CI 规则与通用编排，不改变原验收。
5. **新增最小发行能力**：版本与环境清单、预检、证据路径可迁移性；第二环境真题验证。
6. **中期条件项**：确认上述闭环成立后再增加有限并发、内存准入和项目队列；真实用量接口可用后才建设成本计量。

本节建议没有新增实现；报告和目标图必须标成未来方案。
