# OpenClaw 单队列容量验证

当队列容量为 1 时，正在运行的命令未结束，第二条命令就不能进入回调。否则，同一会话的两个命令可能同时更改共享状态。

示例在 OpenClaw 源码提交 `3c3dbe4beff053a48d2a442e299c0edb94f73076` 上使用 Node v24.22.1。驱动调用真实的 `enqueueCommandInLane`。它在同一条队列中枚举三次入队和三次结束的五种合法顺序，每次结束可成功或拒绝，共 40 种。每个事件后运行一个事件循环轮次。覆盖范围限于固定容量、同优先级的队列。

准备与该提交一致的本地源码镜像，镜像中的 `/app` 需要安装 OpenClaw 的依赖。从 iBoard 根目录运行。

```sh
python3 -m workbench.formal_verify create --repo "$OPENCLAW_REPO" \
  --revision 3c3dbe4beff053a48d2a442e299c0edb94f73076 --image "$OPENCLAW_IMAGE" \
  --manifest examples/formal/openclaw-command-lane/manifest.json \
  --helpers examples/formal/openclaw-command-lane --out "$EVIDENCE_DIR"
```

`verification.json` 应显示 40 种调度均通过。临时把容量判断从小于改为小于等于后，`mutation.json` 应报告反例：放入两个命令，第一条未结束，两条却都已启动。独立脚本用队列公开的活跃计数重放同一顺序。随后 `unmodified-replay.json` 应再次通过 40 种调度。工作台删掉临时工作树，并在输出目录生成可重跑的项目技能。

重启、动态容量、组合队列、取消与超时需要各自声明属性和边界。
