# iBoard：面向产品开发自动化的 Agent Harness 工作台

[报告正文](iboard-experiment-report.md) · [论文式HTML](iboard-experiment-report.html) · [PDF](iboard-experiment-report.pdf) · [技术附件](technical-notes.md)

[在线报告](https://leewbl.github.io/iBoard/) · [在线当前架构](https://leewbl.github.io/iBoard/reports/2026-11-12/paper/architecture/iboard_as_is.archify.html) · [在线目标架构](https://leewbl.github.io/iBoard/reports/2026-11-12/paper/architecture/iboard_to_be.archify.html)

报告采用“摘要—问题定义—问题分析—解决方案—实验结果—结果分析—下一步改进验证—结论”的总分总结构。覆盖产品定义、当前实现、十题故障实验、独立交付试点及下一阶段真实需求验证，明确区分公开原型、本地实验和建议设计。

## 架构图与论文图

| 内容 | 交互图 | 静态图／原生规格 |
|---|---|---|
| As-Is 当前架构 | [Archify HTML](paper/architecture/iboard_as_is.archify.html) | [SVG](paper/architecture/iboard_as_is.svg) · [JSON](paper/architecture/iboard_as_is.archify.json) |
| To-Be 建议目标 | [Archify HTML](paper/architecture/iboard_to_be.archify.html) | [SVG](paper/architecture/iboard_to_be.svg) · [JSON](paper/architecture/iboard_to_be.archify.json) |

正文包括五张可编辑SVG：产品与验收框架、当前架构概览、评分与耗时、两个案例的证据链、建议架构概览。概览与完整Archify使用相同架构事实；两类图的生成方式在[图源说明](paper/README.md)中区分。

## 阅读与证据

HTML与PDF及图形保留在本目录，可在线查看GitHub Pages或下载离线阅读。代码引用指向分析基准的固定提交，实测指标保存在 [data/metrics.json](data/metrics.json)。原始实验日志留在本地，公开包提供[来源目录与范围](data/evidence-index.md)和记录哈希，不把缺少原始文件表述为公开可重复的完整实验。

本次整理于2026年11月15日，报告内容日期为11月12日，实验证据截至11月7日。没有补跑产品实验或模型调用。原有80/100与8/10的口径保持不变，需求端到端仍待专门验证。
