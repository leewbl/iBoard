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

## 用 TLC 生成状态图

[CommandLane.tla](CommandLane.tla) 把入队、成功结束和拒绝结束映射为三个动作。在这个有限模型中，结束一个命令与立即补充同优先级的等待命令视为一步。常规配置要求同一时刻只运行一个命令。故障配置 [Overlap.cfg](Overlap.cfg) 放宽容量判断，保留同一个不变量。

从官方来源准备 [TLC 1.7.4 JAR](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)、[Temurin JRE 25.0.5.1+2](https://github.com/adoptium/temurin25-binaries/releases/tag/jdk-25.0.5.1%2B2) 对应 Docker 的 Linux arm64 或 amd64 压缩包，以及 [Pele 1.9.0 压缩包](https://registry.npmjs.org/pele/-/pele-1.9.0.tgz)。本机 Docker 需要已有基于 glibc 的 Node 24 镜像，例如 `node:24-slim`。准备命令先核对三个文件的哈希，再离线构建。

```sh
python3 -m workbench.statechart prepare --base-image "$LOCAL_NODE_IMAGE_ID" \
  --java-archive "$JRE_TAR" --tlc-jar "$TLC_JAR" --pele-archive "$PELE_TAR"
```

用输出中的镜像 ID 运行验证。常规配置下，TLC 应验证 16 个不同状态并生成 Mermaid、SVG、报告和原始日志。把配置换成 `Overlap.cfg` 并使用新的证据目录，TLC 应返回 1，报告违反 `SingleActive` 的最短路径为连续两次入队。

```sh
model=examples/formal/openclaw-command-lane
python3 -m workbench.statechart check "$model/CommandLane.tla" \
  --view "$model/view.mjs" --image "$RUNTIME_IMAGE" --out "$EVIDENCE_DIR"
```

[模型验证契约](manifest-model.json) 还可用于 `workbench.formal_verify create`。用同一个准备命令在 OpenClaw 源码镜像上叠加工具，再将上面的有界检查示例改为 `--manifest "$model/manifest-model.json"`。适配器读实际的容量判断来选择模型配置。修改判断后由 TLC 找出反例，再由独立的 Node 驱动操作真实队列复现。这个映射只覆盖该判断和示例声明的行为，其他队列实现变化仍需检查模型是否同步。

## 用 Z3 证明容量约束

[证明契约](manifest-proof.json) 检查固定容量的单条队列。`proof.mjs` 从源码读取接纳比较符，并核对空队列初始化、接纳先记账和同一 generation 结算释放的顺序。Z3 对数学整数检查初始化、接纳和结算三个归纳义务。在任务 ID 各不相同、容量和 generation 固定的前提下，队列执行任意有限步，活跃计数始终在零和容量之间。调整容量、重置、组合队列和 JavaScript 任务 ID 精度另行验证。

将 [Debian Z3 4.13.3-1](https://packages.debian.org/trixie/z3) 安装到与 OpenClaw 提交 `54710b123fe94a2139930c1d7c038e7623345a5e` 一致的 Debian trixie 源码镜像中。以安装后镜像的不可变 ID 运行验证。联网只用于准备工具镜像，后续求解和回放由 iBoard 在断网容器执行。

```sh
model=examples/formal/openclaw-command-lane
python3 -m workbench.formal_verify create --repo "$OPENCLAW_REPO" \
  --revision 54710b123fe94a2139930c1d7c038e7623345a5e --image "$OPENCLAW_Z3_IMAGE_ID" \
  --manifest "$model/manifest-proof.json" --helpers "$model" --out "$EVIDENCE_DIR"
```

正常版本的三个反例查询应为 `unsat`。把 `<` 改成 `<=` 后，接纳义务应为 `sat`。求解器另查容量为 1 且已有一条命令占用时的具体反例。独立 Node 驱动提交两条暂不结束的真实命令，核对活动计数和回调同时运行的峰值。工作台删除临时工作树后再证明未改动版本，并在输出目录保留项目技能和每次原始回执。
