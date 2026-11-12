# 文档索引

> 由总指挥维护。新增、改名或停更文档时同步更新本页。

## 先读这几份

| 你想… | 读 |
|---|---|
| 知道项目是什么、怎么运行、怎么测试、怎么打包 | [根 README](../README.md) |
| 演示（全部仿真，不连真机、不扣费） | [DEMO.md](DEMO.md) |
| 知道现在做到哪、下一步做什么 | [ROADMAP.md](ROADMAP.md)、[evolution/live/STATUS.md](evolution/live/STATUS.md) |
| 参与开发（分工、分支、交付格式、合并流程） | [AGENT_COLLABORATION.md](AGENT_COLLABORATION.md) |

## 架构与契约

| 文档 | 内容 |
|---|---|
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | 当前目标架构：三层 + 一个契约、跨本体方式、安全原则 |
| [specs/CAPABILITIES_V1.md](specs/CAPABILITIES_V1.md) | 能力接口标准 v1（由 `spec/neuxmind_spec/capabilities.py` 生成，冲突时以源码为准） |
| [specs/INTERFACES_V1.md](specs/INTERFACES_V1.md) | 模块之间的传输接口契约（含各轮新增的 §5–§9） |
| [evolution/DECISIONS.md](evolution/DECISIONS.md) | 架构决策记录 ADR（只追加） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 0.2 时期的平台分层与证据原则；分层仍有效，具体组成以 TARGET_ARCHITECTURE 为准 |
| `product-architecture-20261103/`、`system-architecture-20261102/` | 产品与系统架构图快照（含生成脚本），不随版本更新 |

## 桌面控制台（`brainos/`、`web/`、`desktop/`）

| 文档 | 内容 |
|---|---|
| [USER_EXPERIENCE.md](USER_EXPERIENCE.md) | 用户目的、页面如何服务用户、各版本的交互约定 |
| [ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md) | 内置工程引擎（Codex app-server）、SSH 接入与本体调查、工程扩展、云网关计费、本体适配 |
| [HARDWARE_INIT.md](HARDWARE_INIT.md) | 连接后的硬件扫描、内置 Skill `hardware-init`、修复命令的审批与限制 |
| [JOB_RECIPES.md](JOB_RECIPES.md) | 训练与仿真任务的可视化配方协议 |
| [HANDOFF.md](HANDOFF.md) | 0.2.0–0.7.1 桌面版交接记录（历史，不再更新；0.8.0 起见进化记录） |

## 各模块

| 模块 | 设计与用法 | 进度与进化条目 |
|---|---|---|
| `runtime/` 端侧运行时 | [runtime/README.md](../runtime/README.md) | `runtime/PROGRESS.md`、`runtime/EVOLUTION_ENTRY.md` |
| `cloud/` 云平台 | [cloud/README.md](../cloud/README.md)（含本地 Docker） | `cloud/PROGRESS.md`、`cloud/EVOLUTION_ENTRY.md` |
| `sdk/` Python SDK 与 `nx` | [sdk/README.md](../sdk/README.md) | `sdk/PROGRESS.md`、`sdk/EVOLUTION_ENTRY.md` |
| `brainos/` 桌面后端 | 根 README「桌面控制台」、ENGINEERING_ENGINE | `brainos/PROGRESS.md`、`brainos/EVOLUTION_ENTRY.md` |
| ROS 2 测试替身 | [runtime/tests/ros2_double/README.md](../runtime/tests/ros2_double/README.md) | — |

## 进化记录（只追加，不改写历史）

| 文档 | 内容 |
|---|---|
| [evolution/README.md](evolution/README.md) | 记录机制与格式 |
| [evolution/EVOLUTION_LOG.md](evolution/EVOLUTION_LOG.md) | 每轮一条，按时间倒序 |
| `evolution/iterations/` | 每轮的详细记录：目标、决策、分工、改动、验证、遗留 |
| `evolution/reviews/` | 独立审查报告 |
| [evolution/AUTOPILOT.md](evolution/AUTOPILOT.md) | 自主进化模式的规则与轮次队列 |
| [evolution/live/STATUS.md](evolution/live/STATUS.md) | 当前这一轮各会话的实时状态（会被覆盖，不是历史） |
