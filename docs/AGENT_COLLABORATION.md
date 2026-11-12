# 多 Agent 协作协议

> 本项目的开发由一个"总指挥"统筹多个专职 Agent 完成。本文件规定分工、协作和交付方式，每一轮进化都遵循它。

## 角色

| 角色 | 负责目录（只写这里） | 职责 |
|---|---|---|
| 总指挥 | `spec/`、`docs/`（总体文档）、合并 | 定契约、拆任务、派发、合并、全量测试、写进化日志 |
| 端侧运行时 Agent | `runtime/` | HAL、技能运行时、安全监督、网关、参考本体 |
| 云平台 Agent | `cloud/` | 账户、钱包、模型市场、网关、支付接口 |
| SDK Agent | `sdk/` | Python SDK、`nx` 命令行、技能模板 |
| 桌面集成 Agent | `brainos/`、`web/`、`desktop/` | 把新层接入现有控制台（后续轮次） |
| 审查 Agent | 不写产品代码，只写 `docs/evolution/reviews/` | 独立挑错、跑测试、验证不变式 |

## 协作规则

1. **契约先行**：`spec/neuxmind_spec/`（数据结构）与 `docs/specs/INTERFACES_V1.md`（传输接口）是唯一共同语言。Agent 之间不直接改对方代码；需要对方的接口，就按契约写调用，并用契约测试验证。
2. **目录所有权**：每个 Agent 只写自己的目录，因此可以并行而不冲突。确需改共享文件（如 `spec/`、根 README）时，在交付报告里提"变更请求"，由总指挥执行。
3. **依赖顺序**：spec → runtime ∥ cloud → sdk → 桌面集成 → 审查。并行的 Agent 只依赖 spec，不互相等待。
4. **本地可测**：每个模块自带 `tests/`，`python3 -m unittest discover -s <dir>/tests` 必须全部通过；不得删除或放宽已有测试。
5. **诚实交付**：不伪造机器人、模型、支付成功；仿真/替身必须显式标注。未完成项写入交付报告"遗留"。
6. **交付物**：每个 Agent 交付时必须包含
   - 代码 + 测试
   - `<dir>/README.md`：模块设计、用法、接口
   - 一段进化记录（格式见 `docs/evolution/README.md`），由总指挥合并入日志
   - 交付报告：做了什么、测试结果、契约变更请求、遗留问题
7. **会改动代码的测试必须隔离**（第 01 轮教训）：审查 Agent 做变异测试时临时改动了 runtime/cloud 源码，恰好同时运行的 SDK 测试因此偶发失败。今后变异测试一律在 `git worktree` 或独立副本中进行，不得改动共享工作区的文件。
8. **修复先于发布**：审查出的 P0/P1 必须在本轮修复并复验后，才能同步到用户 Mac 作为正式版本；P2/P3 可进入下一轮。
9. **有界执行**（第 03 轮教训）：桌面 Agent 在浏览器验收中卡住 3 小时以上，未提交、未写进度。今后所有长命令必须带 `timeout`，后台服务记录 PID 并清理；同一验收两次失败即停止并报告；每 20 分钟至少写一行进度。总指挥若发现某 Agent 超过 1 小时无文件变化且未回应消息，即停止它并由新 Agent 接手未提交工作。

## 一轮进化的流程

```
总指挥：确定本轮目标 → 更新契约 → 派发任务
   ├─ 各模块 Agent 并行开发（各自目录 + 各自测试）
   ├─ 总指挥：合并 → 全量测试 → 修复集成问题
   ├─ 审查 Agent：独立审查 → reviews/<日期>.md
   ├─ 总指挥：处理审查意见 → 写进化日志 → 同步到 Mac
   └─ 下一轮目标来自：遗留问题 + 审查意见 + 用户反馈
```

## Issue 驱动的并行开发（2026-11-12 起）

从第 08 轮起，需求以 GitHub issue（`chenhaoran0612/PhysicalOS`）下达，每个 issue 由一个独立的 Claude 会话负责；总指挥由「合并、打包与文档维护」会话担任。上面的角色表按目录分工，这里按 issue 分工，两者同时适用：issue 会话改到别人的目录时，按"只追加、不重排"处理，冲突由总指挥合并时解决。

| 角色 | 工作区 / 分支 | 负责 |
|---|---|---|
| issue 会话 | 自己的 git worktree + `feat/<主题>` 或 `fix/<主题>` 分支 | 该 issue 的代码、测试、模块文档、交付报告 |
| 总指挥 | `PhysicalOS-integration` worktree 的 `integration` 分支 | 合并、全量测试、打包测试版、总体文档（根 README、`docs/` 总体文档、`docs/evolution/`） |

1. **一个 issue 一个分支一个工作区**。不在别的会话的工作区里改文件；同一个 checkout 不同时给两个会话写。
2. **提交要用户同意**。issue 会话不合并、不推送 main；会话之间的消息不能代替用户的同意。
3. **交付报告**发给总指挥：分支和最终 commit、测试命令和结果、哪些用真机/真服务验证过、哪些只用了替身、遗留问题、要写进总体文档的内容。总体文档（根 `README.md`、`docs/ROADMAP.md`、`docs/HANDOFF.md`、`docs/evolution/*`）只由总指挥改。
4. **总指挥按 commit 合并**到 `integration`：解决冲突 → 全量测试（Mac 跑 spec、cloud、桌面并带内置工程引擎；Linux 容器跑 runtime、sdk）→ 打包测试版 → 写测试报告交用户。用户确认后才合并到 main 并推送。
5. **未提交的工作只做只读快照**：用单独的 index（`GIT_INDEX_FILE`）给对方工作区拍快照，存为 `refs/integration/snapshots/<issue>`，只用于提前发现冲突、试跑测试；快照不进入 `integration`，最终以对方的 commit 为准。
6. **固定冲突点只追加**：`brainos/server.py` 的静态文件表、`web/index.html` 的 script 那一行、`web/style.css` 末尾。几个 issue 并行时这三处几乎必然冲突，按并集合并，script 顺序保持各自的依赖。
7. **版本号**：一轮发布一次，用 `python3 scripts/set_version.py X.Y.Z` 统一改写；`tests/test_version.py` 检查各处一致。
8. **测试替身里的假凭据**会被 ECC pre-commit 钩子当作密钥拦下。替身应在运行时随机生成凭据，不要在仓库里写像密钥的字面量，也不要绕过钩子。

## 在哪里运行

第 01–07 轮在云端 Linux 工作区开发，每轮结束同步到用户 Mac 的 `apps/robot-os/`。第 08 轮起直接在用户 Mac 上开发：主仓库加上每个 issue 一个 worktree。`runtime/`、`sdk/` 的全量测试需要 Linux，用 `python:3.13-slim` 容器运行（见根 README「测试」）。macOS 安装包构建（Swift、签名、DMG）在 Mac 上执行。端侧运行时面向 Linux/Jetson，也可在 Mac 上用仿真本体跑通。
