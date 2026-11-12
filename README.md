# NeuxMind Robot OS

> **0.8.0（第 01 轮进化）起，本项目从"Mac 桌面控制台"升级为跨本体机器人操作系统与开发平台。** 目标架构见 [docs/TARGET_ARCHITECTURE.md](docs/TARGET_ARCHITECTURE.md)，进化记录见 [docs/evolution/](docs/evolution/)，全部文档的索引见 [docs/README.md](docs/README.md)。
>
> 当前发布版本 **0.14.0**（第 07 轮）。需求从第 08 轮起以 GitHub issue 下达、每个 issue 一个分支并行开发，由总指挥合并到 `integration` 分支、全量测试并打包后交用户测试，流程见 [docs/AGENT_COLLABORATION.md](docs/AGENT_COLLABORATION.md#issue-驱动的并行开发2026-11-12-起)，进度见 [docs/evolution/live/STATUS.md](docs/evolution/live/STATUS.md)。

## 0.8.0 组成

| 目录 | 是什么 | 运行在 | 文档 |
|---|---|---|---|
| `spec/` | 能力接口标准 v1、manifest v2、技能包 v1、兼容性判定（跨本体的唯一契约） | 各处共用 | [CAPABILITIES_V1](docs/specs/CAPABILITIES_V1.md) · [INTERFACES_V1](docs/specs/INTERFACES_V1.md) |
| `runtime/` | 端侧运行时：HAL、安全监督（限幅/看门狗/急停锁存/停止代数）、可移植技能运行时、v1+v2 网关、仿真四足与机械臂、ROS 2 桥接骨架 | 机器人 / Jetson / 任意 Linux | [runtime/README](runtime/README.md) |
| `cloud/` | 云平台：账户/组织/API Key、邮箱验证、token 钱包与账本、模型市场、推理网关、支付接口 | 服务器 | [cloud/README](cloud/README.md) |
| `sdk/` | Python SDK 与 `nx` 命令行、技能模板与本地测试工具 | 开发者电脑 | [sdk/README](sdk/README.md) |
| `brainos/` `web/` `desktop/` | Mac 桌面控制台（原有功能保留；新增仿真证据隔离与"解除急停"） | Mac | 下文 |

### 5 分钟体验跨本体（仿真）

```sh
pip install -e sdk/                                   # 安装 nx（需 Python 3.10+）
nx sim up --body quadruped                            # 启动仿真四足
nx skill new my-patrol --template patrol              # 从模板创建技能
nx skill check my-patrol && nx skill install my-patrol
nx skill run my-patrol --param 'waypoints=[[1,0],[1,1]]' --authorize --wait
nx sim up --body arm --port 8791                      # 再起一台仿真机械臂
nx skill check my-patrol --all                        # 机械臂 → 不兼容：缺少 base.navigate_to
nx stop sim-quadruped                                 # 停止永远可用
nx sim down
```

详细步骤以 [sdk/README](sdk/README.md) 为准。所有仿真结果都明确标注，**不能作为真机现场证据**；真机、真实模型、真实支付均尚未验证，见 [ROADMAP](docs/ROADMAP.md)。

### 测试

```sh
python3 -m unittest discover -s spec/tests
python3 -m unittest discover -s runtime/tests
pip install -r cloud/requirements.txt && python3 -m unittest discover -s cloud/tests
python3 -m unittest discover -s sdk/tests
python3 -m unittest discover -s tests      # 桌面端（含 tests/test_version.py：各处版本号一致）
```

- `runtime/` 与 `sdk/` 的技能进程沙箱只支持 Linux（`os.waitid`、降权用户），在 macOS 上这两套测试会失败或超时，**不能用 Mac 上的结果判断它们**。在 Linux 容器里跑：仓库只读挂载进 `python:3.13-slim`，复制到容器内，`pip install -r requirements-runtime.txt -r cloud/requirements.txt && pip install -e spec -e runtime -e sdk`，再以非 root 用户运行上面的命令。Mac 上的 Docker Desktop 容器里 `runtime/tests/test_sandbox.py` 有几项会因进程数上限或计时而不稳定（main 上同样如此），判断沙箱改动请以真 Linux 主机或 Jetson 上的结果为准。
- 桌面端有 39 项测试需要内置工程引擎（`.build/agent-runtime/bin/codex`，由 `scripts/prepare_agent_runtime.py` 准备，或设置 `NEUXMIND_TEST_AGENT_BINARY`），缺少时自动跳过；发布前的全量测试必须带上它。
- 浏览器验收脚本是 `tests/*_browser.cjs` + 对应的 `*_browser_fixture.py`，用法写在各脚本开头。

---

## 桌面控制台（0.7.x 原有说明）

面向机器人使用者、开发者和集成商的 macOS 桌面平台框架。核心流程是：创建项目 → 接入本体 → 选用模型与技能 → 开发应用 → 训练 / 仿真 / 测试 → 部署 → 运行维护。

本项目没有预设用户的机器人型号、个人训练服务器或模型供应商。平台可以提供自研端侧模型、开源模型、闭源模型网关、训练与仿真算力，并通过额度与用量框架运营；也允许用户接入自有服务。

## 安装与运行

- macOS 安装包：`dist/NeuxMind-Robot-OS-<版本>-arm64.dmg` 或同名 `.pkg`（当前发布版本 0.14.0；`dist/` 不进 Git，由 `scripts/build_macos.py` 生成）。
- 原生宿主使用 Cocoa + WKWebView，内置独立 Python 后端，最终用户不需要安装开发环境。
- 当前构建为 Apple Silicon、macOS 13+ 开发预览。Intel Mac、Windows 尚未构建验证。
- 安装包采用 ad-hoc 本地签名，尚未进行 Developer ID 签名和 Apple 公证。
- 数据存放在 `~/Library/Application Support/NeuxMind Robot OS/state/`。应用更新不覆盖用户数据。
- 菜单“连接平台服务…”可连接运营方部署的 HTTPS 工作台；本机工作空间保留。当前没有默认的已上线云平台地址。

源码开发（在仓库根目录执行）：

```sh
python3 -m brainos.server --port 8788
```

首次从 Git 获取源码后，先安装运行依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-runtime.txt
.venv/bin/python scripts/prepare_agent_runtime.py
.venv/bin/python -m brainos.server --port 8788
```

构建 Apple Silicon 安装包需要 macOS、Python 3.12 和 Xcode Command Line Tools（提供 Swift 编译器与系统签名/打包工具）：

```sh
python3.12 -m venv .build/packaging-venv
.build/packaging-venv/bin/python -m pip install -r requirements-build.txt
.build/packaging-venv/bin/python scripts/prepare_agent_runtime.py
.build/packaging-venv/bin/python scripts/build_macos.py
```

源码包含后端、前端、原生宿主、测试、打包脚本、运行时摘要与许可证。上游运行时按 `runtime-lock.json` 下载并校验，不依赖开发者个人登录。用户数据、模型凭据、测试产物、安装包与构建缓存不进入 Git。

后端使用 Python 3.9+；SSH 依赖见 requirements-runtime.txt。工程引擎由 scripts/prepare_agent_runtime.py 按版本和摘要获取。前端无需构建依赖。默认仅监听回环地址；此服务器是本地开发与桌面运行时，不是已加固的公网 SaaS 部署。

## 当前已实现的框架

| 领域 | 实际实现 |
| --- | --- |
| 工作空间 | 注册、登录、密码哈希、会话、账号数据隔离、项目隔离、持久化 |
| 本体 | SSH 自主调查、带证据的软硬件档案、多形态登记、适配器连接与能力发现；SSH 可直连或经一台跳板机（两跳指纹都确认并固定），可粘贴一条 ssh 命令自动填表；真机只读实时状态（主机、负载、内存、磁盘、温度、失败的服务、关注的服务），见 [工程引擎说明](docs/ENGINEERING_ENGINE.md#跳板机与真机实时状态) |
| 模型 | 自有推理服务接入、平台模型目录、来源分类、契约、调用、端侧模型包部署接口；“平台模型与算力”页可连接 TokenFans（粘贴 API Key，或用账号登录一次自动读取 Key）作为工作空间默认大模型：没有单独选模型的本体调查、工程任务、协作流程和本体适配都用它，费用从 TokenFans 余额扣除，见 [工程引擎说明](docs/ENGINEERING_ENGINE.md#tokenfans工作空间默认模型issue-4) |
| 技能与应用 | 技能定义、模型绑定、参数化顺序编排、超时、成功 / 停止条件、不可变应用版本 |
| Agent | 内置工程运行时、持续会话、文件修改、命令与测试、审批、产物；仍可接入远程执行器 |
| 协作流程 | 自定义角色和阶段、独立模型与扩展、串行执行、交付快照、逐阶段验收、重试与总预算 |
| 训练 | 平台或自有执行器，声明式任务、提交、同步、取消、检查点与指标引用 |
| 仿真 | 场景注册、引擎与版本引用、仿真执行器和任务、记录与产物 |
| Harness | 能力、依赖、版本和停止条件静态检查；外部测试任务；设备端现场证据 |
| 部署 | 现场证据与精确应用 / 本体版本匹配，设备端发布、启动和停止请求 |
| 平台运营 | 资源上架与实际服务握手、模型上架、平台私有凭据隔离 |
| 用量 | 整数额度点定价、用量预算预留、服务端计量结算、账本、额度申请 |
| 桌面 | 原生窗口、菜单、独立后端、平台地址设置、数据目录、安装包脚本 |

## 必须区分的未完成项

- 不随产品提供假模型或假机器人。真实自研权重、开源部署包、闭源服务凭据需要运营方配置。
- 没有内置任何厂商机器人驱动；`RobotAdapter` 协议和 Edge Agent 网关已经提供，实际驱动与现场资格需要按型号实现。
- 训练器、仿真执行器与平台模型网关由服务协议连接。当前未连接实际训练集群或物理仿真后端，也未启动真实训练。
- 内置工程 Agent 已支持自主调查、工程工具、Skills、HTTP MCP 和串行多角色协作。真实模型 API 由用户或平台提供；OAuth、本地 MCP 进程、浏览器控制、云端执行、并行与分支流程尚未完成，详见 [工程引擎说明](docs/ENGINEERING_ENGINE.md)。
- 仿真目前包括场景与运行管理，没有完成嵌入式 3D 编辑器、实时仿真画面或 Continuum 引擎集成。
- 端侧模型“一键部署”需要兼容设备提供 `model.install` 能力、运行时声明并校验实际包摘要。安装模型不等于已经通过任务验收。
- 当前额度以平台点计量；未设定人民币 / 美元汇率，未接入真实支付渠道、支付回调、发票或自动采购。额度申请不会伪造支付成功。
- 本地账号 / 运营存储是可运行框架；多节点 SaaS、团队 RBAC、数据库迁移体系、强隔离、云端 IAM、支付安全审计和生产服务 SLA 仍需要建设。

## TokenFans 默认模型

- 在“平台模型与算力”页连接。API Key 只保存在 `<工作空间>/credentials/tokenfans.json`（文件权限 0600），绑定校验时的 TokenFans 地址；账号登录用的密码不保存。页面、状态快照和接口返回里都没有完整 Key。
- 环境变量 `NEUXMIND_TOKENFANS_URL` 可把地址指向测试替身或自建的兼容服务（必须是 HTTPS，只有本机地址允许 HTTP）。
- 接口：`/api/model-compute/{key,login,models,default,disconnect}`；状态快照里的 `model_compute` 只有摘要。其他模块直接调用大模型时用 `platform.model_compute.default_llm()`，走工程引擎时不传 `model_id` 即可。
- 目前只在本机 TokenFans 替身上验证过（`tests/test_model_compute.py`、`tests/tokenfans_browser_fixture.py`），没有调用过真实的 TokenFans 服务。

## 运营配置

本地运营账号通过启动时显式指定邮箱授权，不允许用户自行提升权限：

```sh
python3 -m brainos.server --operator-email operator@example.com
```

注册该邮箱后，在“平台资源 → 运营管理”上架资源和模型。资源上架必须先通过 `platform.service.v1` 服务握手。供应商凭据保存在运营目录，普通工作空间只能看到公开资源信息。

平台作业创建时预留额度，提交时传递 `max_units` 与 `max_minutes`。执行器必须实施硬预算。成功、失败或取消后的可信 `usage_units` 与 `usage_evidence_ref` 用于结算；网络超时、未知结果或缺少计量证据保留预留，等待核对，不自动重复执行。定价由资源版本固定，旧任务保留原价格。

## 云平台本地运行

云平台（`cloud/`）可以在本机 Docker 里运行（PostgreSQL 16 + 云 API，自动写入演示数据），这是演示配置，不是生产部署：

```sh
docker compose -f cloud/compose.yaml up -d --build --wait   # http://127.0.0.1:8800
NX_DEMO_CLOUD=docker python3 scripts/demo.py up             # 或：演示脚本改用 Docker 里的云平台
```

细节与注意事项见 [cloud/README.md](cloud/README.md#本地-docker演示用不是生产部署)。

## 工程结构

```text
brainos/
  auth.py         工作空间身份与会话
  recipes.py      服务方案、可视化参数与任务确认
  contracts.py    机器人、模型与执行器协议
  connector.py    显式接入地址的 HTTP 调用
  service.py      项目、资产、任务、测试与部署
  commerce.py     平台目录、模型目录、预算与账本
  model_compute.py TokenFans 连接、凭据文件与工作空间默认模型
  engineering.py  自主调查、档案、工程会话和模型计量
  extensions.py   工程 Skill 版本与 MCP 工具管理
  workflows.py    多角色阶段执行、交接快照与验收
  agent_runtime.py 内置 app-server 生命周期与协议
  device_access.py SSH 探针、主机指纹与受控配置读取（直连或经一台跳板机）
  device_status.py 真机只读实时状态（固定命令，不经过模型）
  httpd.py        控制台与设备网关共用的 HTTP 服务器（绑定时不做反向 DNS）
  edge.py         设备端网关与幂等请求记录
  store.py        本地 SQLite 持久化
  server.py       桌面 API 与静态资源
web/              产品工作台
desktop/          原生 macOS 宿主与安装说明
scripts/          可重复打包脚本、演示脚本、版本号统一改写（set_version.py）
tests/            测试专用协议替身与验收
docs/             系统架构、接入契约和交接
```

## 验证与打包

```sh
.build/packaging-venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
node --check web/engineering.js
node --check web/extensions.js
node --check web/workflows.js
node --check web/journey.js
node --check web/planner.js
for f in web/*.js tests/*.cjs; do node --check "$f" || echo "FAIL $f"; done   # 全部前端文件
# 发版时先统一改写版本号（构建脚本、后端、引擎客户端、关于窗口、Info.plist、侧栏、安装说明、冒烟测试）：
python3 scripts/set_version.py 0.15.0        # 不带参数则列出各处当前版本
# 在独立构建环境安装 PyInstaller 和 requirements-runtime.txt 后：
.build/packaging-venv/bin/python scripts/prepare_agent_runtime.py
.build/packaging-venv/bin/python scripts/build_macos.py
```

`scripts/build_macos.py` 会先删除再重建 `dist/NeuxMind Robot OS.app`：从某个工作区打包前，先退出从那里启动的 App。

测试设备与模型仅位于 `tests/`，不会包含在产品模型库或正常启动流程中。测试通过证明平台协议和门禁，不证明真实机器人的动作、安全或任务可靠性。

用户路径与页面设计见 [用户体验约定](docs/USER_EXPERIENCE.md)。

训练与仿真任务支持服务声明的可视化方案，协议见 [任务配方](docs/JOB_RECIPES.md)。未声明配方的服务继续使用高级 JSON 配置。

0.6.3：训练完成后可保存产物到模型库，保留来源与指标；模型详情提供配置调用、调用测试和来源任务入口。保存产物不会自动加载权重或生成端侧部署包。详见 docs/USER_EXPERIENCE.md 与 docs/JOB_RECIPES.md。

0.6.3：新增应用交付清单，将工作步骤、配置检查、现场测试、发布和启动衔接到同一个应用版本；较新的失败或未确认现场测试会阻止旧报告用于发布/启动，停止请求保持可用。

0.6.3：应用支持复制修改、步骤重排、新旧变更预览与版本历史。保存新版本保留旧应用及证据，重新开始配置检查和现场验证。


0.7.0：Mac 本体调查升级为持续的原生 Agent 会话。基础体检、任意 SSH 调查命令审批、新目录读取授权、长文件/证据分页、本地分析、带证据档案和覆盖缺口已接通。新增搜索工具随包分发；用户可选择平台模型或自有 Responses API。真实模型验收与协议/安装包验证分别记录在 docs/HANDOFF.md。

0.7.1：本体档案支持分类筛选、关键词搜索、历史版本阅读、与上一版对比及可读证据。历史条目缺席仅标为“本版未记录”，不推断设备移除。可从当前档案预填未知项，确认后继续原调查会话。
