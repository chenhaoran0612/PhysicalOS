# 内置工程引擎与自主本体接入

更新：2026-11-03，产品 0.7.0。源码、产品名称与运行时来源分开管理。

## 产品流程

用户创建项目，选择平台工程模型或配置自有模型 API，然后提供机器人 SSH 地址、用户名和认证方式。连接前显示 SSH 主机指纹，认证成功后创建本体资产。Agent 通过受控工具调查系统、计算硬件、总线、传感器、网络、进程服务、软件、机器人中间件和容器；允许目录内的文本配置与源码可分页读取；发现额外目录或需要专用 SSH 命令时，向用户展示具体请求，批准后继续同一会话。

Agent 将发现分类到系统、计算硬件、传感器、执行器、接口、软件、中间件、服务、模型与配置。每个条目必须引用当前会话保存的设备证据；缺失信息列为未知。档案不可变、按版本留存，归属于项目中的本体。连接成功、调查完成均不会授予控制或部署资格。

工程任务读取项目和本体档案，在专属工程目录开发适配器、技能、仿真配置或 Harness，调用文件编辑、命令执行与本地测试工具。会话保存计划、流式消息、命令输出、文件差异、审批、问题、Token 报告及产物清单；用户可以停止、继续和导出。工程任务生成代码不等于已完成厂商驱动或实机验收。

## 已接入能力与后续范围

| 能力 | 当前状态 |
| --- | --- |
| 原生开源工程运行时 | Codex Rust app-server 0.146.1 随 macOS 安装包分发，stdio JSON RPC |
| 持续会话 | thread/start、thread/resume、turn/start、turn/interrupt |
| 工具执行 | 批量基础体检、单项复查、完整证据分页、SFTP 读文件；单次审批的 SSH 命令与会话目录扩展；本地原生命令、文件修改与分析 |
| 交互 | 流式事件、任务计划、差异、提问和单次权限审批；并发请求排队 |
| 多 Agent | 产品级串行角色编排、阶段验收与交付快照已实现；运行时子 Agent 功能保留，但尚未做真实模型协作验收；并行与分支流程未实现 |
| 工程模型 | 自有 Responses 工具调用 API；平台网关模型目录、预算预留、会话令牌、关停计量结算 |
| SSH 接入 | 密码、私钥、本机 SSH Agent；钉住主机指纹；只读调查和限定目录 SFTP；直连或经一台跳板机；真机只读实时状态 |
| 本体档案 | 按机器人保存版本、原始证据、未知项、适配建议 |
| Skills | 项目内创建、导入 SKILL.md/文本技能包、导出、停用与不可变版本；任务明确选择并原生加载 |
| MCP | HTTP 服务配置、Bearer 认证、工具发现、允许列表、原生调用与逐次审批；OAuth、本地进程和插件市场未实现 |
| 浏览器/桌面控制、网络工具 | 当前工程沙箱默认关闭；尚未提供对应产品权限与执行环境 |
| 云端执行、托管连接器、账号订阅 | 不包含在开源 harness 中；需要独立服务与授权，未复制 Codex Desktop 云服务 |
| 串口、USB、厂商专有发现 | 后续连接插件；现有 RobotAdapter HTTP 接入保留 |
| 实机写入与自主上岗 | 当前 SSH 仅调查；安装驱动、修改设备、动作验证仍经专门执行与现场验收流程 |

“集成全部能力”作为产品路线，不把随包携带二进制等同于全部功能已经产品化。后续按本体发现、工程执行、工具与 Skills/MCP、远端隔离执行、模型运营的顺序扩展，复用同一运行时与会话模型。

## 隔离与凭据

每个会话有独立 runtime-home/workspace；不复用安装机器上的 Codex 登录或个人配置。SSH 密码/私钥、模型 API Key 保存在权限收紧的本机用户存储，不进入普通快照、PROJECT.json 或调查工具的上下文。模型令牌通过子进程环境注入，不写入启动参数和配置文件。当前本地秘密存储依赖操作系统账户和文件权限，尚未迁移到系统钥匙串。

调查模式启用专属本地工作目录的原生命令、文件修改、计划与运行时协作工具；本机和机器人明确区分。基础探针和授权目录的 SFTP 读取自动执行；任意 SSH 命令必须逐次展示原命令、目标、用途、时限并由用户批准，目录扩展仅限当前会话。任意远端 shell 并没有只读沙箱，不能把模型的“只读用途”当成权限保证；SSH 账号应按现场要求配置权限。额外本机网络/文件系统权限在调查模式仍被拒绝。配置读取通过规范路径检查，拒绝凭据目录、敏感文件、越界链接、设备与管道；长文本和目录支持分页，输出过滤敏感字段。工程模式默认仅允许专属目录写入、最小系统与所选 Skill 目录读取，禁止 shell 工具网络；MCP 连接由独立客户端经明确配置访问。额外权限由用户审核；用户批准的权限可能扩大边界。外部设备内容视为数据，不能改变产品策略。

## 模型与费用

自有 API 必须支持 Responses 流式工具调用；并非任意模型名称都保证兼容。非回环地址要求 HTTPS。Token 上限是观察限额，收到用量事件后中止，单次请求可能超额；展示运行时报告的可见主/子会话用量，以供应商账单为准。

平台资源 kind=reasoning，unit=1k_tokens。服务清单需声明 schema=platform.service.v1、protocol=responses、models 数组和 metered_sessions=true。运营凭据只供后端调用：

- POST /v1/agent-sessions：request_id、model、max_tokens、max_units、max_minutes；返回 session_id、同来源 base_url 和限额 access_token。
- 运行时使用该会话令牌调用模型。网关必须执行 Token、时间、额度硬上限，不能只信客户端计数。
- POST /v1/agent-sessions/{id}/close：幂等 request_id；返回 status=closed、usage_units、usage_evidence_ref，服务端据此结算。
- 网关创建结果不明、关闭失败或计量缺失时保留预留，标记 reconciliation_required，等待运营核对；不猜测费用或伪造退款。

### 云平台模型：云网关路径（第 03 轮，契约 §5.3）

模型配置 `billing=cloud`（`POST /api/engineering/models/cloud`，记录模型 ID 与当时的云平台地址 `cloud_base_url`；列表来自 `POST /api/cloud/gateway-models` = 云端 `/catalog/models?kind=llm` 中 `gateway=true` 且计价为 `per_1k_tokens`/`free` 的模型）。每次运行：

1. 开始前（`_cloud_ready`）：已登录、云地址与模型记录一致、邮箱未被标为未验证、预算 ≥ `CLOUD_MIN_TOKENS`=80000（Codex 0.146.1 每次请求约 50 KB，网关按请求字节保守预留输入上界）。不满足即拒绝启动。
2. `_model`：若本会话上次的云端会话处于 `cloud_close_pending`/`cloud_close_failed`，先尝试关闭；然后 `POST {cloud}/api/v1/gateway/sessions`（`request_id`=`nx-desktop-run-…`、`model`、`max_tokens`、`max_minutes`）。运行时得到的 `base_url` 由**本机配置的云地址**拼出（`…/gateway/sessions/{id}/v1`，会话 ID 严格校验），不用服务端返回的地址，令牌只发往用户选择的云平台；服务端地址另存为 `server_base_url` 仅供核对。会话令牌只放进运行时环境，不写入存储、快照、事件或日志（测试逐文件检查）。
3. 运行时（Codex app-server）以 Responses 流式 + 函数工具调用 `{base_url}/responses`；网关白名单对其请求的兼容性见 `cloud/README.md` 与 `cloud/tests/test_codex_compat.py`。
4. `_run` 的 `finally`：无论完成、停止、超时、Token 观察限额、运行时异常，都 `POST …/close`（幂等 `request_id`=`close-` + 开会话的 request_id；云端仍在排空上一流式调用时对 409 `session_busy` 最多重试 20 s）。`status=closed` → `metering_status=cloud_settled`，否则 `cloud_reconciliation_required`；关闭失败 → `cloud_close_failed`（同时记入 `cloud_runs`，不会被下一次运行覆盖丢失），可用 `POST /api/engineering/cloud/close` 重试。应用重启时 `cloud_open` 的会话改为 `cloud_close_pending`。
5. 云端返回 `overage_calls>0` 时，以云端超额结论为准（`error_details` 以 `usage_exceeds_reservation` 开头，运行时自身的错误附在后面）。

这条路径**不调用** `commerce.reserve/settle/release`，本机账本不变（`tests/test_cloud_engineering.py` 用会抛错的替身守住三个方法并比较账本前后快照）；会话记录只保存云端会话 ID、预留点数与云端 close 返回的计量（`usage_tokens`、`credits_charged`、`usage_evidence_ref`、`overage_*`）。工作流把 `cloud_reconciliation_required` 与本地 `reconciliation_required` 同样视为“费用待核对”。

Token 观察限额对云模型仍然生效（运行时自报用量达到即停止），但计费上限由云端会话硬性执行。停止时若轮次恰好已结束，运行时返回的 “no active turn to interrupt” 不再当作失败。

**长会话与请求体积（审查 Q3-07）。** Codex 在 `store=false` 下每次请求都发送完整会话历史（工具输出每条最多约 6000 Token）。云端会话模型调用的请求体上限单独为 8 MB（`NEUXMIND_SESSION_MAX_BODY_BYTES`，其他接口仍为 1 MB），`input` 最多 4096 项；每个字节仍计入输入上界并从会话预算中预留——请求越长，每次调用需要的剩余预算越多（约等于请求字节数），所以长任务需要相应更高的 Token 预算。超过体积上限时云端返回 413 `payload_too_large`（本次未计费），桌面端提示“会话历史过长，请新建工程任务（新会话）继续”。

**引擎的本地落盘与网络行为（审查 Q3-03 / Q3-04）。** 每个会话的 `runtime-home`（`CODEX_HOME`，目录 0700）在 `config.toml` 中关闭了 `shell_snapshot`（否则 Codex 会把 app-server 的环境——包括模型密钥/会话令牌、MCP 令牌——以 `declare -x` 写进 `shell_snapshots/*.sh`）、`plugins`、`remote_plugin`、`plugin_sharing`、`in_app_updates`、`skill_mcp_dependency_install` 与 `check_for_update_on_startup`（否则 app-server 启动后会从 GitHub 克隆约 25 MB 插件市场，`runtime-home` 约 86 MB）。每次运行结束再删除 `shell_snapshots/`、`.tmp/`、`plugins/` 作为第二道防线。关闭后 `runtime-home` 约 2 MB（Codex 自己的 SQLite 状态，供继续会话使用），引擎只连接配置的模型地址与已配置的 MCP 服务。真实 Codex 回归测试：任务完成后对整个桌面数据目录扫描会话令牌与自有 API Key，零命中（自有 Key 只在 0600 的本机存储中）；app-server 空闲 6 s 后不存在 `.tmp/plugins`、`runtime-home` < 5 MB。

测试：`tests/test_cloud_engineering.py`——协议层（回放录制的 Codex 请求体，始终运行）与真实运行时（`NEUXMIND_TEST_AGENT_BINARY` 指向 Codex 0.146.1 时运行）两组，均对接本机真实 cloud 应用（SQLite、Echo 替身、`simulated:true`）；浏览器验收 `tests/cloud03_browser_fixture.py` + `tests/cloud03_browser.cjs`。

当前不包含真实模型供应商运营网关、支付渠道或货币兑换。自动回归使用本机协议服务；用户授权的真实模型验收单独记录，不混同协议回归。

## 来源与分发

官方源码： https://github.com/openai/codex/tree/rust-v0.146.1

官方平台说明： https://developers.openai.com/blog/codex-as-a-platform

协议参考： https://learn.chatgpt.com/docs/app-server

版本、来源提交、npm 归档完整性和两个二进制摘要固定于 runtime-lock.json。scripts/prepare_agent_runtime.py 获取并验证上游归档；构建脚本必须先验证二进制。产品界面统一使用 NeuxMind 工程助手；开源许可证、版权和 NOTICE 保留于安装包 Resources/licenses，并通过菜单“开源许可…”访问。产品集成部分为本项目新增模块，上游二进制未修改。

## 验证范围

单元/协议测试使用真实 app-server 二进制和本地 Responses SSE 服务：自主工具调用、带证据档案、真实 SSH 协议握手、执行本地命令生成文件、预算预留/结算。浏览器测试覆盖模型配置、SSH 指纹、调查会话、档案和证据、工程任务产物与导航固定。以上证明接入和协议链路，尚不证明真实大模型的调查覆盖、代码质量或具体机器人驱动可用性。

## 0.4.0 工程扩展

新增“工程扩展”页面；工程 Skills 与机器人端侧技能分开存储。Skill 包包含 SKILL.md 和最多 31 个辅助文本文件，总计不超过 500 KB。使用上游原生解析器验证元数据，不在导入时执行脚本；拒绝绝对路径、隐藏目录、路径跳转与二进制内容。名称限小写英文、数字和连字符。发布新版本保留旧版本、摘要和会话绑定，任务只加载明确选择的版本。导入/导出 JSON 格式为 schema=neuxmind.engineering-skill.v1、version、files（相对路径到文本内容）。

MCP 仅支持 Streamable HTTP 与可选 Bearer Token，非回环地址要求 HTTPS。保存连接、原生握手、选择工具、启用、任务选择分别处理。连接检查无需模型 API，不执行工具。实际任务启用工具允许列表与逐次审批，并记录 mcpToolCall 结果。资源读取是服务独立能力，不受工具名允许列表约束，UI 明示须使用权限适当的服务。调用前校验工具定义摘要，发现变化即中止，要求重新检查与新建任务。

MCP 凭据只通过运行时环境注入，不进入配置、模型上下文、项目快照或会话导出。运行时每次仅加载所选 Skills/MCP；自动发现的其他 Skills 被禁用，未复用本机已有用户配置。工程 Skill 目录只读，产物仍写入专属 workspace。停用 Skill/MCP 或收回正在使用的工具权限会请求中断相关会话；已经发生的远端操作无法自动撤销。旧会话续接仍检查启用与权限状态。普通 MCP 表单和 OAuth 请求暂时拒绝并记录原因，不自动接受。

本轮基于固定 0.146.1 schema 与实际 app-server 行为接入。官方参考：[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Skills](https://learn.chatgpt.com/docs/build-skills)、[App Server](https://learn.chatgpt.com/docs/app-server)。验证使用本机 MCP/模型协议替身，无实际供应商调用和机器人动作。

## 0.5.0 多角色工程协作

入口：Agent Studio → 协作流程。默认四阶段为本体分析、适配器开发、Harness 验证和交付审查；用户可调整为 1–8 个阶段，配置角色、任务、模型、Skills/MCP、必须交付的文件、时间和 Token 限额。定义保存后不可变，运行保留完整定义；模型凭据仍留在模型配置中，不进入定义导出。

每阶段创建独立会话和 workspace。系统写入 PROJECT.json、WORKFLOW.json 和 UPSTREAM.json，之前已验收的文件复制到 UPSTREAM/stage-N/，每次复制前校验原始摘要。Agent 生成约定产物和 HANDOFF.json：

```json
{
  "schema": "neuxmind.stage-result.v1",
  "outcome": "ready_for_review",
  "summary": "完成内容、检查结果和未验证事项",
  "artifacts": ["src/adapter.py", "harness/report.md"],
  "checks": [
    {"name": "接口测试", "status": "passed", "command_id": "本会话原生命令调用 ID", "detail": "测试命令及覆盖范围"}
  ]
}
```

outcome 也可为 blocked/failed；checks 的状态允许 passed、failed、not_run、manual。passed 必须引用当前会话真实 commandExecution 的成功退出记录；要求命令验证的阶段必须至少有一项这样的检查。真实成功命令只能证明命令执行过，不能证明测试充分或机器人可上岗；交付内容仍须人工检查。缺少文件、伪造命令引用或报告失败都会阻止验收和下阶段启动。

阶段完成后，系统冻结交付文件及普通工程产物，保存 SHA-256 摘要；自动收集排除隐藏文件、依赖及 Python 测试缓存。单文件最多 10 MB、阶段最多 200 文件/20 MB，大型资产应保存引用。路径跳转和符号链接拒绝。后续修改原工程文件不会改动交付快照；快照被外部修改会导致预览、验收或下游交接失败。快照不是对本机管理员的写保护。

用户查看产物、命令和未验证项后，可以通过或退回。通过即启动下一阶段，最后一阶段通过则完成工程流程；退回或执行失败需补充要求并手动重试，不自动重新计费。每阶段最多三次尝试，重试开启新会话，保留历史会话和已验收上游。暂停在验收阶段可直接恢复；执行中停止、应用退出或重启后保留记录，由用户决定重试。工作流会话不能从普通会话入口续接，以免绕过预算和验收状态。

总 Token 分配上限是保守的启动控制：每次尝试先计入整个阶段限额，启动失败也保留该次分配，不将未用部分自动归还。它与真实费用不同；实际报告用量单独累计，平台费用仍按网关证据结算。自有 API 的观察限额可能被单次请求超过；平台硬上限由网关执行。收到计量待核对状态会阻止阶段验收。定义修改、并行执行、条件分支、自动修复循环和工程到实机自动发布不在本次实现范围。


## 0.7.0：自适应本体调查

输入是 SSH 地址、认证信息和用户选择的工程模型；输出是可续接会话、按本体版本化的档案、原始证据、调查覆盖范围、未知项及本地分析产物。

调查复用同一原生 app-server 的模型会话和工程工具，不另写一个只会执行固定流程的聊天代理。device_overview 将九项基础探针合并为一次模型工具调用；每项仍单独保存证据。device_probe 定向复查；device_evidence 分页读取被摘要的完整结果；device_config 分页读取授权目录内文本及源码。device_exec 允许模型针对发现提出 SSH 命令，device_request_scope 允许申请新的厂商工程目录，两者均经产品待审批队列执行，拒绝、取消、过期审批不会执行该请求。

DEVICE_CONTEXT.json 是产品写入的本地资料，排除本体控制令牌。旧档案与本会话证据保留。新会话沿原生 thread 继续；0.6.x 旧调查线程因动态工具集合不可变，会在首次续接时创建新线程并从已存证据恢复上下文，previous_thread_ids 保留原线程引用。工具契约版本为 3。

覆盖数由后端按真实探针证据计算，区分未调查、已读取、读取有限制。它不是机器人能力分数；一个已结束的模型回合不等于设备被完全认识。无证据类别始终列为待确认，报告不能授予部署或动作资格。SSH 连接关闭不保证已发出的任意远端进程终止，现场操作仍应按实际控制状态核验。

默认调查观察限额为 200000 Tokens、15 分钟，可由用户调整；本地协议测试使用较低限额。观察限额包含运行时报告的输入/输出与缓存用量，不等于费用金额；平台硬限额由模型网关执行。

Mac 包增加同一已固定上游归档中的 ripgrep 15.2.0（e89fff89ac），摘要写入 runtime-lock.json，独立保留 MIT 与 PCRE2 许可。命令工具获得固定的最小 PATH，包含随包搜索工具和系统命令，不依赖用户安装开发工具，不继承 API/SSH 密钥。回环模型和 MCP 地址绕过系统代理，外部模型保留正常代理路径。

“Codex 等效”在本功能中按原生运行时、真实模型工具链、SSH 自适应调查、连续上下文和可验证产物验收；不据此声称所有 Codex Desktop 云服务、浏览器能力和任意机器人实机效果已等效。

## 第 04 轮：本体适配工作流（契约 §6.2）

实现：`brainos/adaptation.py` + `brainos/workflows.py`（模板阶段与产品执行阶段）；入口 `POST /api/engineering/adaptation/start`（`robot_id`、`inventory_id`＝必须是最新档案、`model_id`、`max_tokens`、`max_minutes`、`data_authorized`）。定义带 `template=robot_adaptation` 与固定的 `inventory_id`；同一本体同时只允许一个进行中的适配流程。用户自建流程不能声明产品执行阶段。

| 阶段 | 执行者 | 产物 | 产品侧检查 |
| --- | --- | --- | --- |
| 1 能力映射 | 工程助手（会话） | `mapping.json`（`neuxmind.capability-mapping.v1`）、`mapping.md` | 注册表每项能力恰好一行；`implementation` ∈ ros2_topic / ros2_action / vendor_sdk / serial / other / unsupported / needs_confirmation；`evidence_ids` 必须属于该档案；`params` 只能是该能力标准里的参数且逐项按 spec 校验；**无证据的行、或可实现但缺少必填参数（限值不猜测）的行改为 needs_confirmation 并记 `downgraded`**；规范化映射存入 `stage.adaptation.mapping` |
| 2 生成适配器 | 运行时脚手架 + 工程助手 | `adapter/README.md`、`adapter/IMPLEMENTATION.md`（以及脚手架生成的 `adapter/<模块>/adapter.py`、`adapter.json`、`pyproject.toml`、`tests/test_adapter.py`） | 会话开始前（工作流/引擎锁之外）写 `neuxmind.adapter_scaffold.v1` 并运行真实 `neuxmind-runtime scaffold --spec FILE --out DIR`（运行时 09c1297），能力＝映射为可实现的项 + 必备项，`family` 不在 spec `FAMILIES` 时用 `custom`，实现提示按 `scaffold.HINT_KINDS` 转换（`ros2[:/名称]`、`sdk`、`serial[:/dev/…]`、`http[:url]`、其余 `todo`），带首个 `evidence_id`；exit 0 时从输出 JSON 取入口 `<模块>.adapter:<类>` 与 `implemented/todo/mandatory_todo`，exit 2（如缺必填参数）阶段停下并显示运行时的原因；要求真实成功命令；校验快照中有 `adapter/<模块>/adapter.py`、`adapter.json`（`adapter.json` 是脚手架计划，真正声明的能力以 `adapter.py` 的 `IMPLEMENTED` 与阶段 3 报告为准） |
| 3 一致性测试 | 产品（不调用模型，0 Token） | `conformance/{static,dry}.report.json`、`conformance/summary.json` | 从已验收快照（逐文件校验摘要）复制适配器包，依次运行 `neuxmind-runtime conformance --adapter <模块>.adapter:<类> --level static|dry --out FILE`；退出码 0 通过 / 1 未通过（有报告，进入验收）/ 2 被拒绝或适配器无法加载（无报告，如必备能力未实现，阶段停下并显示原因）；报告结论 `<level>_ok` 必须与退出码一致。后端：`PYTHONPATH`＝包根目录 + 运行时的 rclpy 测试替身 `runtime/tests/ros2_double`（遮蔽已安装的 rclpy，ROS 2 模板连到替身里的假差速机器人，清单因 `use_sim_time` 为 `simulated:true`、`evidence_kind:"simulation"`）+ `NEUXMIND_CONFORMANCE_PYTHONPATH`；环境 `NEUXMIND_ADAPTER_BACKEND=double` 给工程助手写的 SDK/串口代码。**从不运行 motion**（`LEVELS=('static','dry')`）。摘要写入 `conformance_reports` 集合（按本体），验收/退回时记 `review` |
| 4 适配报告 | 产品 | `ADAPTATION_REPORT.md`、`adaptation.report.json`（`neuxmind.adaptation-report.v1`） | 可用＝可实现且 static、dry 均通过；未实现＝不支持 / 未声明 / 测试失败；需现场验证＝需确认、dry 未覆盖、全部运动能力；风险含失败项、降级项、替身限制、motion 未运行、AI 代码需审查、档案未知项 |

产品执行阶段与工程会话阶段共用尝试次数（≤3）、交付快照（SHA-256）、验收门、停止与重试；交接报告带 `product_stage: true`，检查项带 `product: true`（界面显示“产品执行：通过/失败”）。

**运行时工具调用。** `run_tool` 以独立子进程运行 `python -m neuxmind_runtime`（`PYTHONPATH`＝仓库 `runtime/`、`spec/`；测试替身用 `adaptation.RUNTIME_TOOL` 或 `NEUXMIND_RUNTIME_TOOL`）：最小环境（只有 PATH、LANG、临时 HOME、PYTHONPATH，不含模型密钥、SSH 凭据、云令牌）、独立进程组、RLIMIT_AS 2 GiB / CPU 240 s / 256 个文件描述符、墙钟时限（脚手架 60 s、每级一致性 180 s），超时或停止时 SIGKILL 整个进程组。生成的适配器代码从不导入桌面进程。**未做网络隔离**：AI 生成的代码在一致性测试中可以访问网络，写入报告风险项。

**报告解析（第 04 轮第二版，与运行时 09c1297 对齐）。** `parse_report` 只接受运行时实际写出的 `conformance.report.v1`：`schema`、`level` 与请求一致、`<level>_ok` 为布尔、`levels.{static,dry,motion}` 齐全、`capabilities[]` 每项有 `levels.<level>.status ∈ passed|failed|skipped`；`field_acceptance` 不是 `false`（含缺失）或 `evidence_kind` 不是 `conformance|simulation`（如 `device_run`）时拒绝。`cases[]` 按 `capability`+`level` 归到能力行作为证据（截断）。不再做旧版的“列表/映射两种布局”猜测。

**摘要字段（`neuxmind.conformance-summary.v1`）。** 字段名与报告一致：`levels.<级别>.{requested, ran, ok, passed, failed, skipped, blocked, blocked_reasons}`（static 取 static 运行，dry/motion 取 dry 运行）；原样带 `conformance_id`（另有 `conformance_ids.{static,dry}`）、`runtime_version`、`spec_version`、`simulated`、`evidence_kind`、`field_acceptance:false`、`disclaimer`、`hardware_authorized`、`prelatched`、`aborted`、`final_latched`、`warmup_s`，报告带 `manifest_digest` 时一并保存与显示；`exit_codes`；每项能力 `declared`、`levels.<级别>.{status, reason, skip_kind}`、`skip_kind`（`not_applicable` 无可测内容 / `blocked` 无法测试）、`evidence`（用例）。`verdict.{static,dry,motion}` 是界面用的一词结论（passed / failed / blocked / not_run，motion 恒为 not_run）。第一版的字符串 `levels` 记录在界面上仍可读。

**与运行时的契约状态。** 桌面端默认调用真实 `python -m neuxmind_runtime scaffold|conformance`（`PYTHONPATH`＝仓库 `runtime/`、`spec/`）。`tests/runtime_double.py` 只在运行时无法运行时使用（`NEUXMIND_TEST_RUNTIME_DOUBLE=1` 强制），其 CLI、退出码与报告字段与真实实现一致，并由同一个严格解析器检查。阶段 3 使用 `runtime/tests/ros2_double` 是有意的：契约 §6.2 要求在替身或仿真上跑 static+dry；运行时 README 说明“通过替身上的一致性测试不说明任何真实 ROS 2 机器人的行为”，摘要与报告都带此限定。替身目录不存在时（如只安装了运行时包）ROS 2 适配器会以 exit 2“需要 ROS 2”失败，可用 `NEUXMIND_CONFORMANCE_ROS2_DOUBLE` 指向替身目录。

**第 04 轮审查修复（Q4-13、Q4-14、D4、aborted）。**
- 阶段 2 验收门对 `adapter/`（不含 `tests/` 与 `__pycache__`）做静态篡改检查 `tamper_findings`：禁止引用运行时内部模块（`conformance`、`safety`、`core`、`gateway`、`runner` 等）、整体导入 `neuxmind_runtime`、包外相对导入、`importlib`/`__import__`/`sys.modules`/帧与 gc 自省/`builtins`、`exec`/`eval`/`compile`，禁止 `.so/.pyd/.pyc/.pth` 与遮蔽 `neuxmind_runtime`/`neuxmind_spec`/`rclpy`/`sitecustomize` 的文件；命中即阻塞阶段。这是静态筛查而不是沙箱，报告与界面写明"报告由适配器所在进程产生，可被适配器代码影响"。阶段 3 只复制源文件，不复制字节码缓存。
- 阶段 3 子进程断网：Linux 用 `unshare -n`（非 root 为 `unshare -r -n`），macOS 用 `sandbox-exec` 拒绝网络；启动时探测一次，不可用时摘要 `network_isolated:false` 并写入风险；`NEUXMIND_ADAPT_NETWORK=allow` 仅供诊断。仍以桌面用户身份运行、文件系统未隔离（如实写在注释与报告中）。
- 每级运行抽成 `conformance_level`：报告 `<level>_ok` 与退出码不一致时阶段停下（有单元测试，变异确认）。
- 运行时安全规则重写后的新结论：报告 `aborted`（字符串）时，未通过的级别 `verdict='aborted'`（界面"测试中止"，红色提示中止原因与"结束时急停仍锁存"），`prelatched` 时提示无法测试（blocked）；`aborted` 非字符串或"中止却 ok=true"的报告被拒绝；阶段 4 把中止、预先锁存、未断网写入风险。

测试：`tests/test_adaptation.py` 20 项（映射校验；用真实运行时在 sim_quadruped 上生成的报告做解析与拒绝 `field_acceptance`/`device_run`/缺字段；真实脚手架 + rclpy 替身上的 static/dry、缺限值 exit 2、必备能力未实现 exit 2；替身 CLI 与严格解析器一致；子进程环境/超时/停止；真实 Codex 0.146.1 + 本地 Responses 替身 + **真实运行时**跑通四阶段、Agent 改坏 `system.health` 后 dry exit 1 进入风险、一致性阶段停止有界（适配器导入挂起）并可无模型重试）；浏览器 `tests/adaptation_browser_fixture.py` + `tests/adaptation_browser.cjs`。

## 第 04 轮：停止的产品侧上界（G-2）

第 03 轮遗留：会话循环只在事件之间检查停止，阻塞在同步 RPC（`thread/start`、`turn/start`、`turn/interrupt`，每个最长 25 s）或工具调用里时看不到停止，无响应的运行时可以无限期拖住停止。现在：

1. `AgentProcess.request(..., cancel=Event)` 以 0.1 s 为步长等待，停止事件置位即抛出 `StopRequested`；会话的停止事件作为默认取消事件（含构造期的 `initialize`/`skills/list`）。`turn/interrupt` 是停止本身发出的，用 `cancel=False`。
2. `turn/interrupt` 在 5 s 内无答复（且不是“no active turn”）→ `_force_stop`：关闭运行时进程组（SIGTERM + SIGCONT，3 s 后 SIGKILL）与 SSH 连接，会话记 `stop_forced`。
3. `stop()` 启动一次看门狗（重复停止不再重复写状态）：`STOP_DEADLINE_SECONDS=10` 秒后运行线程仍在 → 强制终止运行时；再过 3 s 线程仍未退出 → 释放并发名额、会话标记 `interrupted` + `stop_forced`，未结算计量按 `reconciliation_required` / `cloud_close_pending` 处理；线程若稍后结束，其 `finally` 保持 `interrupted`。
4. 停止后出现的运行时异常（取消、被强制关闭的管道）按停止处理，不再显示为“失败”。会话记录 `stop_requested_at`、`stop_latency_ms`。最终状态写入与释放在同一临界区，避免并发 `stop()` 写回 `cancelling`。

测试：`tests/test_stop_bound.py`（真实 Codex 0.146.1；用 SIGSTOP 冻结真实运行时进程制造“无响应”）：`thread/start` 被冻结时停止立即生效且不触发强制终止；回合中运行时无响应时 10 s 内强制终止；运行线程卡在不看停止的调用中时看门狗释放并标记；重复停止只有一个看门狗。变异（去掉 RPC 取消、去掉看门狗）各使一项测试失败。

## 跳板机与真机实时状态

**经跳板机接入。** 机器人在内网、要先登录一台中转机器才能到达时，连接表单的“连接方式”选“经跳板机”（一跳，相当于 `ssh -J 跳板机 目标机`）。跳板机有自己的地址、端口、用户名和认证方式（密码、私钥、本机 SSH Agent）。

**粘贴 SSH 命令。** 表单最上面可以直接粘贴平时在终端里用的命令，例如 `ssh -J ops@203.0.113.10:2222 robot@192.168.1.100`，目标机的地址、端口、用户名和跳板机会自动填好，认证方式设为“本机 SSH Agent”，可以再改。解析在页面里完成（`web/ssh_command.js`），命令不会被执行，填好的值仍由后端校验。能识别 `-p`、`-l`、`-J`、`-o Port/User/HostName/ProxyJump`、`ssh://` 地址和带方括号的 IPv6；多跳、`ProxyCommand` 和不认识的选项会说明原因并拒绝；命令里的密钥文件（`-i`）、`~/.ssh/config` 里的别名和末尾的远程命令不会被采用，页面会提示。

接入分两步确认指纹，仍然遵守“先确认指纹，再发送认证信息”：

1. `POST /api/engineering/ssh/scan`（带 `jump`）只读取跳板机的主机密钥，返回 `stage: "jump"`，此时不向任何一端发送凭据，也不接触目标机。
2. 用户确认跳板机指纹后，再次调用 `scan`（带 `jump_check_id`、`trust_jump_fingerprint` 和跳板机凭据）：登录跳板机，通过它读取目标机的主机密钥。
3. 用户确认目标机指纹后调用 `connect`，两跳的指纹都保存在连接记录里；之后每次连接都按这两个指纹校验，任何一跳不一致即拒绝。

读取指纹和连接失败时都会说明是哪一跳、什么原因（`ConnectError.hop` 为 `jump` 或 `device`；`reason` 为 `unreachable`（连不上）、`timeout`（应答太慢）、`no_ssh`（地址能连上但不是 SSH 服务）、`auth`、`key`、`fingerprint`，或跳板机关闭了 TCP 转发时的 `forbidden`）。实时状态里会话中途断开、分不清是哪一跳时，`hop` 为 `connection`。只支持一跳；不读取本机 SSH 配置文件，不做 Agent 转发。跳板机凭据与设备凭据一样只保存在本机用户存储，不进入快照和模型上下文。

**真机实时状态。** 通过 SSH 接入的机器人（直连或经跳板机）在卡片上有“实时状态”。`POST /api/engineering/ssh/status` 在设备上执行一条产品固定的只读命令（`brainos/device_status.py` 的 `status_command`），不经过模型，不接受用户输入的命令文本；页面每 3 秒读取一次。显示：

- 是否可达、往返延迟、连接路径（直连或经哪台跳板机）
- 主机名、开机时长、负载、内存、根分区、最高温度
- 失败的 systemd 服务（最多列 20 个；设备没有可用的 systemd 时明确显示读不到，而不是“没有失败的服务”）
- 用户指定关注的服务是否在运行（最多 12 个；服务名只允许字母、数字和 `. _ @ : -`，用 `POST /api/engineering/ssh/services` 修改）

后端为每个连接复用一个 SSH 会话，60 秒没有读取就由定期清理关闭，重新接入机器人时旧连接的会话立即关闭；状态命令 6 秒超时。设备没有报告的项目显示为“—”，不填零。这里的状态只来自操作系统，不包含电量、位姿等机器人层数据（那需要适配器），也不代表机器人可以运动。

**连接各阶段的时限。** 建立 TCP 连接 10 秒、握手 20 秒、认证 20 秒、打开通道 15 秒（`brainos/device_access.py` 的 `TCP_TIMEOUT` 等常量）。经公网跳板机时每个来回都要 1 秒以上，SSH Agent 里有几把密钥就要试几次；最初每个阶段 8 秒的时限在真实跳板机上会偶发超时，并被误报为“连不上”。

测试：`tests/test_ssh_jump.py`（两端都是本机 SSH 协议替身；密码、私钥和真实 `ssh-agent` 三种登录；慢链路超时）；命令解析 `node tests/ssh_command_logic.cjs`；浏览器验收 `tests/ssh_jump_browser_fixture.py` + `tests/ssh_jump_browser.cjs`。

真机验证（2026-11-12）：经一台公网跳板机连接一台四足机器人的机载电脑，两跳都用本机 SSH Agent 登录，两跳指纹都与本机 `known_hosts` 的记录一致；只执行了上面那条只读状态命令。首次读取（新建两跳会话）约 13 秒，之后复用会话每次约 1 秒，读到的主机名、负载、内存、磁盘、温度、失败的服务和关注的服务状态与人工排查一致。页面上的操作流程（粘贴命令、两步确认指纹）没有在真机上走过，只在协议替身上验收过。
