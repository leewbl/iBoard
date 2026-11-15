# 图源与文档生成

本目录包含论文图、Archify原生规格、静态导出和HTML/PDF文档生成脚本。

## 图形来源

五张论文概览与结果图位于 `figures/`：

1. `fig1-harness-overview.svg`：产品框架及需求／故障验收。
2. `fig4-current-architecture.svg`：当前架构概览。
3. `fig2-benchmark-results.svg`：十题阶段评分与有效耗时，数据为 `../data/metrics.json`。
4. `fig3-evidence-chain.svg`：#180815与SQLite试点的结果边界。
5. `fig5-target-architecture.svg`：建议目标架构概览。

概览图由Python生成，保留文本和独立矢量元素；不是Archify原生导出。`architecture/`中的两张完整HTML由Archify 2.17生成，JSON为原生输入，SVG/PNG来自其导出菜单；两份交付收据记录9/9 showcase、零错误零警告。HTML和JSON保持原交付字节，收据的输入／输出路径改为相对文件名，原有产物哈希保持有效。公开SVG仅去除行末空格，图形内容保持不变；其哈希以本包检查清单为准。

图形采用用户参考图中的白底、彩色虚线分组和多面板形式，内容来自iBoard实现与记录。没有复用参考图的研究结论。写作采用oh-my-writing的wenzhi-humanize规则，保留数据、范围和不确定性。

## 重建文档

阅读不需要任何生成依赖。编辑源为上级目录的 `iboard-experiment-report.md` 与 `technical-notes.md`；`paper.css`负责网页和A4打印排版。

若环境已有Node.js、marked和Playwright，可运行：

```sh
node paper/render-paper.mjs
node paper/check-paper.mjs
```

上述命令从报告目录运行。依赖默认由Node解析，也可用 `IBOARD_DOCS_NODE_MODULES` 指向已有node_modules；`IBOARD_DOCS_BROWSER`可指定已安装的Chrome/Chromium。未指定浏览器时使用Playwright自己的Chromium。生成器不安装产品依赖，也不会调用模型或启动Docker。

论文图可用 `python3 paper/figures/generate_figures.py` 和 `python3 paper/figures/generate_architecture_overviews.py` 重建；前者仅读取已公开指标。修改完整Archify需使用兼容工具重新校验与交付，不应只修改HTML中的图形。

本次仓库包的文件、链接、指标与收据检查保存在 [publication-checks.json](publication-checks.json)。生成后的浏览器截图属于本地检查产物，不随仓库分发。
