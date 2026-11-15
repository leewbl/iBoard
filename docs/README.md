# iBoard 文档

iBoard 是面向产品开发自动化的 Agent Harness 工作台，组织智能体完成需求实现、故障修复、隔离验证、独立验收和 PR 提交。当前以 Claude Code、GLM 和 Python 工作流实现，自动合并由任务交付策略约束。

## 最新报告与架构

- **在线入口：[报告](https://leewbl.github.io/iBoard/) · [当前架构](https://leewbl.github.io/iBoard/reports/2026-11-12/paper/architecture/iboard_as_is.archify.html) · [目标架构](https://leewbl.github.io/iBoard/reports/2026-11-12/paper/architecture/iboard_to_be.archify.html) · [PDF](https://leewbl.github.io/iBoard/reports/2026-11-12/iboard-experiment-report.pdf)**
- [实验报告（GitHub可直接阅读）](reports/2026-11-12/iboard-experiment-report.md)
- [论文式网页版](reports/2026-11-12/iboard-experiment-report.html) · [10页PDF](reports/2026-11-12/iboard-experiment-report.pdf)
- [当前架构：Archify](reports/2026-11-12/paper/architecture/iboard_as_is.archify.html) · [SVG预览](reports/2026-11-12/paper/architecture/iboard_as_is.svg)
- [目标架构：Archify](reports/2026-11-12/paper/architecture/iboard_to_be.archify.html) · [SVG预览](reports/2026-11-12/paper/architecture/iboard_to_be.svg)
- [技术附件与证据范围](reports/2026-11-12/technical-notes.md) · [指标数据](reports/2026-11-12/data/metrics.json)

报告修订于2026年11月12日，本次于11月15日整理入仓库；实验证据截至11月7日。十题故障评测得80/100，八题满足固定验收，不能解释为需求开发成功率或无人值守交付率。需求分支已有代码，真实需求至PR的完整实验仍需补充。

GitHub Pages 从 `main` 分支的 `/docs` 目录发布，`docs/.nojekyll` 使现有HTML和SVG按静态文件提供，首页指向新版报告。无需运行模型、Docker或产品测试即可部署文档。后续推送到发布分支后由GitHub更新站点。

也可下载仓库后用浏览器打开HTML，或在仓库根目录运行 `python3 -m http.server 8000 --directory docs/reports/2026-11-12 --bind 127.0.0.1`，访问 `http://127.0.0.1:8000/iboard-experiment-report.html`。
