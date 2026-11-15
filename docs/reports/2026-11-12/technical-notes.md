# iBoard 实验报告技术附件

本附件保留2026年11月7日整理材料中的详细版本、指标和证据范围，供查证使用。11月12日只重写报告，没有重新执行产品实验。先读[六部分正文](iboard-experiment-report.md)。

## 固定条件

| 项目 | 已核对内容 |
|---|---|
| 目标源码基线 | `54710b123fe94a2139930c1d7c038e7623345a5e` |
| 题集提交 | `81498d1e391a6cbdf0f5ef92eb46419a8de51bb5` |
| 题集 SHA-256 | `16c3705cd1e0b669bda60b8a764ada24005c86afcfa87e107e2ded3451ffae03`；当前文件及固定提交中的文件均一致 |
| 初始 iBoard | `effc347df42f45037dad0a4bbe2a2cb7e53d363a`；后续按题使用不同恢复版本，并非同一不变发行版 |
| 赛道 | `pr-assisted`，允许参考/接纳已有补丁，不能作为 fresh 无答案修复成绩 |
| 模型与工具 | `glm-5.3[1M]` 经 Claude Code 2.1.285；Node v24.22.1、pnpm 12.15.0 |
| 正式资源策略 | 串行；容器 3072 MiB；Docker VM 4096 MiB；容器断网。只读根文件系统不等于任务工作区挂载只读 |
| 预算 | 时间和迭代上限为 null；费用按厂商报错被动停止；文本 token 和费用记录为空值 |
| 变化与混杂 | 编排器、恢复脚本、验证补充和提示输入随题调整；root 镜像和提示哈希不是所有实际调用同一镜像/提示的证明 |
| 边界 | 3584 MiB 额外运行属于诊断；固定题集含两道不适用/未复现题，仍保留分母 |

证据：[运行元数据](data/evidence-index.md#local-evidence)、[最终快照](data/evidence-index.md#local-evidence)、[冻结协议](data/evidence-index.md#local-evidence)。

## 逐题完整结果与限制

| 题目 | 结论/分数 | 有效时间 | 容器次数 | 候选提交 | 分类证据 | 验证限制 |
|---|---|---:|---:|---|---|---|
| #149994 浅色主题审批按钮文字对比度 | 完整通过 10/10 | 3:55:37 | 46 | `c03fd1b4` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 保留本地；有协调和独立审核，不等于完全无人值守。定向类型检查使用正式生成数据库类型；非全量CI。 |
| #180815 键盘跳过聊天记录到输入框 | 完整通过 10/10 | 8:37:52 | 33 | `72ef11ed` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 156项单测、54项浏览器测试和补充独立复核支持评分；原生流程未完成终审，补充GLM审查420秒超时，未交付。截图网关模拟，gpt-5.5只是标签。 |
| #180379 agent.wait 与父任务答案文本一致性 | 完整通过 10/10 | 1:56:14 | 55 | `e68aa31c` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 双角色77项原生与22项补充测试；Codex恢复协调和补强验证导致保守自主指标排除，业务修复由Claude Code/GLM完成；未全量CI或交付。 |
| #183389 提升为顶层会话后的忙碌状态 | 基线行为正确 0/10 | 6:40:26 | 37 | `54710b12` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 固定基线两次3072 MiB行为断言正确，未获得修复分。原始退出86保留；独立外层监控归因于esbuild辅助进程并确认退出和容器清理。 |
| #186914 Slack 延迟回复的顶层/线程路由 | 完整通过 10/10 | 2:57:35 | 54 | `3d224b81` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 路由回放、模拟Gateway ACK/wait/peer及实际SDK到本地HTTP对端；没有真实Slack或完整模型回合，不能外推真实端到端渠道集成。 |
| #185317 子任务模型策略拒绝的修复提示 | 完整通过 10/10 | 4:08:40 | 99 | `fe500a55` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 真实spawn/session/registry配模拟Gateway ACK；没有在线模型推理或全量CI。补充旧例、策略和metadata检查保留原条件。 |
| #183700 Doctor 检查跨会话记忆插件可用性 | 完整通过 10/10 | 3:09:08 | 74 | `17eb3923` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 实际collector/loader与Doctor CLI；36叶只读发现遮蔽不等于实际卸载/在线Gateway/供应商链路。外部冻结测试补回产品测试丢失的metadata断言。 |
| #159790 UI 测试重复加载 Lit | 未复现 0/10 | 0:07:26 | 2 | `54710b12` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 固定Linux共享配置15项通过且无Lit警告；隔离配置未选择目标测试并退出1。历史macOS环境未重建，未复现、计0分。 |
| #185079 纯图片历史消息的恢复上下文投影 | 完整通过 10/10 | 2:48:36 | 56 | `a2fc0d90` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 真实SQLite复现、双角色150项及冻结控制。定向类型检查基线/候选同报既有TS2561，不属于类型检查通过或全量CI通过。 |
| #185534 Gemini 参数转换与本地输入校验 | 完整通过 10/10 | 1:39:45 | 18 | `aa14e9d5` | [复现](data/evidence-index.md#local-evidence) · [验收](data/evidence-index.md#local-evidence) · [测试](data/evidence-index.md#local-evidence) | 离线运行与生成声明验证；没有在线Gemini和Code Mode guest执行。Codex终止越界find子进程被保守计为干预，未编辑目标业务源码。 |

“验证关闭”是证据目录名称，不是远端 Issue 关闭声明。8个10分候选均与固定基线独立比较；两道0分题并非两个待修复的新bug被工作台修复失败的同一类型。

## 指标定义与计算

| 指标 | 无工作台或最简方案基线 | 本轮 iBoard | 绝对/相对变化 | 计算与边界 |
|---|---|---|---|---|
| 冻结评分 | 未测量 | 80/100 | 无法计算 | 每题复现2、修复4、回归2、独立复验2，逐级依赖 |
| 评测验收完整通过率 | 未测量 | 8/10＝80% | 无法计算 | 冻结评分全部成立；不代表远端交付率或任意项目成功率 |
| 固定基线复现比例 | 不适用 | 8/10＝80% | 不适用 | 另有1题基线已有正确行为、1题未复现 |
| 保守自主完成指标 | 未测量 | 2/10＝20% | 无法计算 | 10分且human_intervention=false，#149994、#186914；仍可有协调与独立审核 |
| 外部干预标记 | 未测量 | 6/10＝60% | 无法计算 | 含Codex恢复、补强验证、运行干预；不是人手写代码比例 |
| 十题有效时间中位数 | 未测量 | 3:03:21 | 无法计算 | 11001.369秒；含准备和中断调用，按记录排除用户暂停 |
| 十题有效时间P95 | 未测量 | 8:37:52 | 无法计算 | nearest-rank，ceil(0.95×10)=10，恰为最大值 |
| 八道通过题有效时间中位数 | 未测量 | 3:03:21 | 无法计算 | 与十题中位数相同是这组数据的巧合，n=8 |
| 八道通过题有效时间P95 | 未测量 | 8:37:52 | 无法计算 | nearest-rank，ceil(0.95×8)=8，恰为最大值 |
| 容器执行记录 | 未测量 | 474次 | 无法计算 | 含失败、诊断、复跑；不是模型调用数 |
| 容器观测时长合计 | 未测量 | 16916.599秒，约4:41:57 | 无法计算 | 已在相应有效时间中，不与其相加；不是整轮墙钟时长 |
| 本轮远端交付状态 | 不适用 | 0/10候选发布/合并 | 不适用 | 本轮保留本地；不能用0%评价交付能力，另有SQLite试点真实交付 |
| 首次成功率 | 未测量 | 现有材料无法计算 | 无法计算 | iterations含恢复/修订，不是统一首次尝试边界 |
| 真实token、成本、单位成功成本 | 未测量 | 未测量 | 无法计算 | 不由时间或容器次数推算 |
| 受控恢复成功率、并行效率、全轮重复波动 | 未测量 | 未测量 | 无法计算 | 没有统一故障注入分母、并发对照或重复完整实验 |

P95 和中位数仅描述这批经过选择的固定样本。十题不足以支持对一般软件任务的总体分位数、稳定性或统计显著性判断。表中有效时间不是“收到任务到通过验收”的统一端到端墙钟时延；后者需要每题排队、暂停、验收结束等一致边界。历史约52小时8分钟是此前目标计时报告，不应将逐题时间相加来重建，也不纳入本次机器复算的时延指标。

## 容易误读的记录

- `human_intervention` 是保守的外部介入标记。#180379 和 #183700 明确记录业务代码由 Claude Code/GLM 编写，外部 Codex 补强验证和恢复仍让指标排除；#185534 仅终止越界 find 子进程也被计入。6/10 不能写成“六题由人修复”。
- 2/10 是冻结记录的筛选结果。#149994 和 #186914 也记有 coordinator-assisted，因此不能称作“完全没有外部协调的无人值守率”。
- 原始 `run.json` 的 #185534 notes 仍有旧句 “Ten-case run remains incomplete”。该自由文本已被结构化 `run_status=completed` 和最终快照覆盖，不据此把最终评测判断为未完成。
- `workflow_phase=delivery` 只表明记录停在该阶段，不能单凭字段认定已创建PR、合并或关闭Issue。
- 题目修复前后红绿对照支持目标代码行为改变；没有“去掉iBoard”或机制消融对照，因此无法单独估计Harness带来的提升幅度。
- 固定题集沿用十题并保留不适用样本，有利于审计；适用子集8/8只可作附加统计，不能替换8/10。

## 模型调用与版本边界

公开源码以 `effc347df42f45037dad0a4bbe2a2cb7e53d363a` 为分析基准。十题运行期间使用了本地恢复等版本，不能用根记录中的一个版本代表全部执行。SQLite试点的gate使用 `3107dad1c8100015b4e54a82b1d961011691e9e6`，接纳已有候选后重新验证；容器内存7168 MiB，与十题正式3072 MiB配置分开。

模型调用有两条路径：

- Claude Code CLI根据设置调用GLM，执行代码修改或只读检查。
- Python通过HTTP直接请求GLM，进行适合给定上下文的终审、输入截图描述和独立视觉检查。

终审先检查基础提示是否超过100000字符；超过即暂停。基础提示未超限，但附加完整改动文件后的材料过大，或需要检查命令来源时，转只读Claude Code检查。两条路径都受任务剩余时间约束。[终审分流](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/workbench/autopilot.py#L442) · [视觉调用](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/workbench/autopilot.py#L927) · [输入截图处理](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/workbench/start.py#L48)

公开差异材料上限为30000字节，和提示字符数限制是两回事。#180815补充候选完整差异126468字节；补充终审记录超时420.018873875秒，输出为空。正文将它四舍五入写为“约420秒”，不据此确定供应商或模型故障的具体原因。[差异限制](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/workbench/delivery.py#L24) · [终审回执](data/evidence-index.md#local-evidence)

#183389的行为检查结论与原始进程退出状态同时保留：行为断言正确，原始exit86没有被改为exit0。独立外层监控将退出归因于esbuild辅助进程，并确认退出和容器清理。#159790在共享配置中15项测试通过、没有出现原警告；隔离配置未选择目标测试、退出1。这些记录不能相互替代。

## 架构和外部对比的查阅入口

- [更新后的当前架构 Archify](paper/architecture/iboard_as_is.archify.html) · [更新后的目标架构 Archify](paper/architecture/iboard_to_be.archify.html)
- [11月7日历史图形：Mermaid、SVG和Archify](data/evidence-index.md#local-evidence) · [新版论文图与图源](paper/README.md)
- [实际代码与架构诊断](data/architecture-diagnosis.md)
- [pstack和DeepSeek对比](data/external-sources.md) · [原始来源清单](data/evidence-index.md#local-evidence)

外部对比沿用11月7日固定的快照：Lauren Tan的pstack为0.15.36，提交 `c742dd61706fbacc1b95f1a20b7b988cf096ce6c`；DeepSeek官方公开研究原型为0.1.0，提交 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`。这些是所分析的版本，不表示11月12日重新核对了最新发布。其他方案没有在本轮十题上实跑，只能比较机制。

## 证据位置

- [十题复现、验证关闭、测试结果](data/evidence-index.md#local-evidence)
- [最终评分和时间快照](data/evidence-index.md#local-evidence)
- [运行记录](data/evidence-index.md#local-evidence)
- [机器可读指标](data/metrics.json)
- [原指标复算脚本](data/evidence-index.md#local-evidence)
- [SQLite试点摘要](https://github.com/leewbl/iBoard/blob/effc347df42f45037dad0a4bbe2a2cb7e53d363a/evidence/openclaw-186875-local-validation.json)

11月7日的检查记录表明，30份分类清单中的319份证据副本与所登记原文件哈希一致。该检查证明文件身份一致，不代替行为和验收判断；本次重写没有把它描述成新跑出的产品测试结果。
