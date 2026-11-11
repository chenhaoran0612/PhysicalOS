# NeuxMind 接口契约 v1

> 维护者：总指挥。各模块 Agent 必须按本文件实现；需要改契约时先在交付报告中提出，由总指挥合并后再改代码。
> 数据结构的唯一权威来源是 `spec/neuxmind_spec/`（能力、manifest、技能包）。本文件规定传输层接口。

## 1. 端侧运行时 HTTP API（`runtime/`，默认端口 8790）

认证：`Authorization: Bearer <BRAIN_EDGE_TOKEN>`（≥24 字符）。所有请求/响应为 JSON；错误 `{"error": "...", "code": "..."}`。

### 1.1 兼容层 v1（现有桌面端 `brainos/service.py` 在用，必须保持行为一致）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/manifest` | 返回 `manifest_v1_view(v2)` |
| GET | `/v1/status` | 至少含 `instance_id` |
| POST | `/v1/validate` | `{application}` → `{accepted, passed, cases}`（桌面端判断 `accepted`） |
| POST | `/v1/evaluate` | 现场测试；需 `request_id`、`hardware_authorized=true` |
| POST | `/v1/deploy` | `{request_id, application, report_id}` → `{accepted, release_id}` |
| POST | `/v1/start` | `{request_id, release_id, hardware_authorized:true}` → `{accepted}` |
| POST | `/v1/stop` | 永远可用，不排队 → `{accepted:true}` |
| POST | `/v1/models/install` | `{request_id, ...}` |
| GET | `/v1/evaluations/{id}` | 异步现场测试状态（§4.1） |
| POST | `/v1/reset` | 第 01 轮修复新增：`{request_id, hardware_authorized:true}` → `{latched}`，解除急停锁存（与 `/v2/reset` 同语义，重放返回当前真实锁存状态），供桌面端“解除急停”使用 |

v1 应用（`robot.application.v1`）的每个 step 有 `entrypoint`：运行时把它解析为已安装技能包的 `id`（或 `id@version`）。

v1 manifest 视图携带 `simulated`、`vendor`、`model`（参与桌面端 manifest 摘要）。桌面端规则：`simulated=true` 的本体或 `evidence_kind != "device_run"` 的现场测试结果，存为 `scope:"simulation"`，**不得**用于发布/启动；界面明确标注“仿真”。

### 1.2 原生 v2

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v2/manifest` | `robot.adapter.v2` |
| GET | `/v2/capabilities/registry` | `capabilities.registry()` |
| GET | `/v2/state?ids=a,b` | 读取 state/stream 能力 → `{id: outputs}` |
| POST | `/v2/capabilities/{id}/invoke` | `{request_id, args, hardware_authorized, timeout_s?}` → command 立即返回 outputs；action 返回 `{run_id}`。action 默认超时：导航 600 s、机械臂 60 s、夹爪 30 s、动作块 步数×dt+5 s、其他 120 s；`timeout_s` 只能缩短 |
| GET | `/v2/runs` | 运行列表；被撤销运行带 `capability_access_revoked` |
| GET | `/v2/skills` | 已安装技能包列表 |
| POST | `/v2/skills/install` | `{request_id, package}`（package = skill.package.v1 + `files: {path: text}`）→ 兼容性结果；不兼容拒绝安装 |
| POST | `/v2/skills/{id}/run` | `{request_id, parameters, hardware_authorized}` → `{run_id}` |
| GET | `/v2/runs/{run_id}` | `{status: queued|running|succeeded|failed|stopped|unknown, progress, result, events}` |
| POST | `/v2/runs/{run_id}/cancel` | 取消 |
| POST | `/v2/stop` | 全局停止；绕过所有锁与队列；停止后急停锁存 |
| POST | `/v2/reset` | 解除急停锁存；需 `hardware_authorized=true` |
| GET | `/v2/telemetry?since=<seq>` | 事件流（轮询） |

不变式（审查 Agent 必查）：
1. `request_id` 幂等：相同 id + 相同内容返回同一结果；相同 id 不同内容拒绝；状态未知不自动重放。
2. motion 类能力与技能运行需 `hardware_authorized=true`，并经安全监督器（限速、限位、看门狗、急停锁存）。
3. 停止路径不经过任何会阻塞的锁、队列、模型调用或网络。
4. `simulated=true` 的本体在所有 UI/API 中可见，仿真结果永不作为现场证据。仿真本体的 `/v1/evaluate` 返回 `simulated: true`、`evidence_kind: "simulation"`、证据引用以 `simulation://` 开头；**调用方必须据此拒绝把它记为现场报告**（第 01 轮发现桌面端尚未检查，列入第 02 轮必修）。
5. 急停锁存：`stop` 后所有运动被拒（409 `estop_latched`），直到 reset。**停止代数**：每次 stop 代数 +1，stop 之前发起的调用即使在 reset 之后返回也一律作废（409 `stale_after_stop`）。上次异常关机时，启动即锁存。
7. 传输层保障停止（第 01 轮修复；第 03 轮补充：普通名额只在连接结束时释放——响应写完、客户端关闭或读取期限到期；停止专用名额内的驱逐不影响普通名额计数）：普通连接上限 32（超出 503 `busy`）；请求须 10 s 内读完，单次读写 5 s；另保留 4 个停止专用名额；有运行中任务时 reset 返回 409 `runs_active`。
8. 厂商扩展能力 `x.*` 一律视为运动类，需现场授权。
9. 复审修复补充：reset 也推进停止代数；请求的代数在**到达时**记录（写账本之前），锁存期间到达的请求即使拖到 reset 之后也作废。适配器契约：`stop()` 与零速度必须能与进行中的普通指令并发调用（看门狗不等待普通指令的锁）。安全监督器在启动时深拷贝并冻结限值；运行中适配器清单若变化，立即停车并拒绝一切运动（409 `manifest_changed`），直到重启。动作块：`dt ≤ 2/rate_hz`（未声明时 ≤ 0.5 s）、步数 ≤ 1000、总时长 ≤ 10 s。
6. 已知限制：`/v1/evaluate` 同步执行，超过桌面 connector 8 秒超时会被记为 unknown；第 02 轮改为异步现场测试。

## 2. 云平台 HTTP API（`cloud/`，默认端口 8800，前缀 `/api/v1`）

认证：用户会话 `Authorization: Bearer <session>` 或开发者 API Key `Authorization: Bearer nx_live_...`（只存哈希）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` `/auth/login` `/auth/logout` | 邮箱+密码；返回 session token |
| GET | `/me` | 用户、所属组织、角色 |
| POST/GET/DELETE | `/api-keys` | 创建（明文只返回一次）、列表、吊销 |
| GET | `/wallet` | `{balance, reserved, available}`，单位：token 点（整数） |
| GET | `/wallet/ledger` | 账本 |
| GET | `/catalog/models` | 模型市场：`{id, name, source: native|open|closed, kind: llm|vla|vision|speech|policy, pricing, package?, requires_capabilities?}` |
| GET | `/catalog/models/{id}` | 详情 |
| POST | `/purchases` | 购买模型授权或 token 套餐 → 订单 `pending` |
| POST | `/payments/{provider}/checkout` | 返回支付跳转；provider 不可用时明确报错，绝不伪造成功 |
| POST | `/payments/{provider}/webhook` | 验签后入账（幂等） |
| POST | `/gateway/infer` | `{model, input, max_tokens}`：预留 → 调用上游 → 按实际用量结算 |
| POST | `/gateway/sessions` `/gateway/sessions/{id}/close` | 兼容桌面端 reasoning 网关协议（见 ENGINEERING_ENGINE.md） |
| GET | `/usage` | 用量记录 |
| POST/GET | `/robots` | 云端设备登记（fleet），存 manifest v2 摘要 |
| GET | `/capabilities/registry` | 同 spec |
| 运营 `/admin/*` | 上架模型/资源、调整额度、核对预留 | 需 `operator` 角色 |

不变式：余额只来自账本；预留先于调用；结算需服务端计量证据；webhook 幂等；API Key 与密码只存哈希；多租户按 org_id 隔离。

### 2.1 第 01 轮实现后补充（已实现，权威细节见 `cloud/README.md`）

- 组织切换头 `X-NeuxMind-Org: <org_id>`（会话用户；默认个人组织）。
- API Key scopes：`gateway`、`catalog:read`、`robots`、`wallet:read`、`usage:read`；只能由登录会话创建，scope 不得超过创建者角色。
- 新增路径：`/org/members`、`/wallet/reservations`、`/catalog/models/compatible`（`{manifest}` → 可用模型）、`/catalog/models/{id}/package`（需授权，`install.args` 可直接作为 `model.install` 参数）、`/catalog/credit-packs`、`/entitlements`、`/purchases/{id}`、`/gateway/sessions/{id}`、会话令牌调用 `{base_url}/chat/completions` 与 `{base_url}/responses`（v1 非流式）。
- `/purchases`：`{kind:"credit_pack", pack_id}` 走支付渠道；`{kind:"model_license", model_id}` 直接扣钱包点数并发放授权。所有模型（含免费/按量，0 点授权）调用网关或下载端侧包前都需授权。
- **两种 operator 不可混用**：组织内角色 `operator` = 现场操作员（可操作机器人）；**平台运营员** 仅能由环境变量 `NEUXMIND_OPERATOR_EMAILS` 或 `grant-operator` 命令授予，可访问 `/admin/*`。
- **第 01 轮修复补充**（cloud 118c54f）：
  - 邮箱验证：`POST /auth/verify-email {token}`、`POST /auth/verify-email/resend`；`user`/`/me` 增加 `email_verified`。未验证可注册、登录、浏览；购买、支付、`/gateway/infer`、开会话返回 403 `email_unverified`（API Key 以创建者邮箱为准）。dev/test 环境注册响应带 `dev_verification_token`（标注 `simulated: true`），prod 禁止使用该发送器。
  - 平台运营员 = 已验证邮箱 **且**（`grant-operator` 命令 或 白名单）；生产环境只用命令授予。新命令：`verify-email`、`revoke-operator`、`sweep`。
  - 会话参数白名单（详见 cloud/README），未知参数 400 `parameter_not_allowed`；`n` 仅允许 1（否则 `n_unsupported`）；输出上限由服务端按剩余预算写入，模型卡新增 `upstream.output_param`；只允许文本与函数工具。
  - 用量超预留：按预留上限扣费，不返回输出（502 `usage_exceeds_reservation`），超出部分进 `/admin/overages` 待运营处理；该会话后续调用 409 `session_overage_locked`（见 ADR-012）。
  - 新路径：`GET/DELETE /org/members`、`/admin/overages`、`/admin/overages/{id}/resolve`、`/admin/maintenance/sweep`；错误码 `reservation_in_flight`。
  - 上游错误分类：未发出（DNS/拒连/TLS/缺凭据）与 4xx（除 408）= 拒绝并释放；5xx、408、超时、断连 = 未知待核对。
- **复审修复补充**（cloud 48999d2）：`POST /auth/verify-email` 必须带本人会话，令牌须属于该会话用户（否则 401 / 403 `verification_wrong_account`）；验证后吊销其他会话与验证前创建的 API Key。邮箱被未验证账户占用时注册返回 202 `claim_sent`，`POST /auth/claim` 认领（占用账户被停用）。找回密码：`/auth/password-reset/request`（恒 202）、`/auth/password-reset`。运营员：生产环境用 `create-operator` 命令创建；`grant-operator` 拒绝未验证账户；prod/staging 下白名单默认关闭（`NEUXMIND_OPERATOR_ALLOWLIST=1` 才启用）。
- 待办（第 03 轮）：桌面端工程助手切到云网关后必须停止本地记账，避免重复计费。（网关会话流式已于第 02 轮实现，见 §4.3；`stream_unsupported` 错误码已废弃。）

## 3. SDK / CLI（`sdk/`）

包名 `neuxmind`，命令 `nx`：

```
nx login | nx whoami
nx robot add <url> --token ... | nx robot list | nx robot caps <robot>
nx skill new <id> --template patrol|pick_place|blank
nx skill check <dir> --robot <robot>        # 兼容性检查（spec.check_compatibility）
nx skill install <dir> --robot <robot>
nx skill run <id> --robot <robot> --param k=v --authorize
nx stop <robot>
nx models list | nx models buy <id> | nx wallet
nx sim up --body quadruped|arm             # 启动仿真参考本体
```

技能 Python API：

```python
from neuxmind.skill import Skill
class Patrol(Skill):
    def run(self, ctx, waypoints):
        for wp in waypoints:
            ctx.call("base.navigate_to", goal=wp)   # 统一能力调用
            ctx.check_stop()
```

---

## 4. 第 02 轮新增契约（2026-11-08，总指挥）

### 4.1 异步现场测试（runtime ↔ 桌面端）
- `POST /v1/evaluate` 请求体新增可选 `async: true`。带该字段时立即返回 `{accepted: true, evaluation_id, status: "running"}`（幂等规则不变：同 `request_id` 同内容返回同一 `evaluation_id`）。不带该字段保持原同步行为（兼容旧桌面端）。
- `GET /v1/evaluations/{evaluation_id}` → `{evaluation_id, status: "running"|"completed"|"unknown", progress?: 0–1, result?}`；对外**只**返回这三种状态（内部状态须映射到其中之一）；`result` 与同步 `/v1/evaluate` 的响应结构完全相同（含 `simulated`、`evidence_kind`、证据引用）。运行时重启后未完成的评估为 `unknown`，不重放。
- 停止/急停期间评估立即结束为 `completed` 且 `passed=false`（原因写入 cases），不得变成"通过"。
- 实现补充（runtime 149ede7）：异步响应另带 `run_id`、`simulated`；若唯一失败的前置检查是急停锁存，请求被接受并**立即**返回 `status:"completed"`、`passed:false`（调用方须处理"POST 即完成"）；其他前置检查失败仍为 409 `validation_failed`；被停止的测试额外带失败项"现场测试未被停止"；未知 id 返回 404 `evaluation_not_found`。
- 桌面端：发起时用 `async:true`，轮询直到 `completed`/`unknown`；界面显示"现场测试进行中"，可随时"请求停止"。旧设备（返回体无 `evaluation_id`）回退到同步模式。

### 4.2 技能进程隔离（runtime）
- 默认 `sandbox: "process"`：每个技能运行在独立子进程中，`SkillContext` 的 `call/read/log/check_stop/params` 通过管道代理到运行时主进程，**所有能力调用仍经主进程的能力总线与安全监督**。子进程无法直接访问适配器对象或运行时内存。
- 停止：主进程先停车（与现有路径相同，不等待子进程），再通知子进程；宽限期后 `SIGKILL`。
- 资源限制（Linux 尽力而为）：CPU 时间、内存（RLIMIT_AS）、文件描述符数；工作目录为技能包目录的只读副本 + 独立临时目录。网络隔离本轮不强制（文档写明）。
- `sandbox: "inprocess"` 仅供调试，需在运行时配置中显式开启；沙箱模式在 `/v1/status` 的 `sandbox` 字段和 `runtime.started` 事件中可见（manifest v2 保持纯 spec，不加字段）。
- 停止路径对子进程只发一次非阻塞 `kill(SIGTERM)`，从不等待，且总在本体停车之后。
- 技能 API 不变：现有示例技能与 SDK 模板无需修改即可运行。

### 4.3 网关会话流式（cloud ↔ SDK / 桌面端）
- 会话调用 `{base_url}/responses` 与 `{base_url}/chat/completions` 允许 `stream: true`，以 SSE 透传上游事件。
- 计量：以流中上游 usage 事件为准；服务端同时统计已转发的输出量作为下界。若输出将超过本次调用的服务端输出上限，服务端截断流并发送 `event: error` / `code: output_limit_reached` 后结束。流在 usage 事件前中断 → 本次调用按预留上限计入待核对（规则同 ADR-012）。
- N-05（已定，ADR-016）：同一会话**串行化**——同一时刻至多一个在途调用（流式在结算完成后才算结束），并发的第二个调用返回 409 `session_busy`，不到达上游。
- 已实现的流式细节（cloud 3e7f2f7，权威细节见 cloud/README）：
  - 流开始前的错误仍是 JSON + HTTP 状态码；只有 200 才是 `text/event-stream`，响应头 `X-NeuxMind-Session`、`X-NeuxMind-Output-Limit`、`X-NeuxMind-Simulated`。客户端不得传 `stream_options`（服务端写入）。
  - 上游事件原样透传；网关在最后**恰好发出一个**终止事件，然后关闭连接：成功为 `event: neuxmind.metering`（`status:"succeeded"`、`call_tokens`、`used_tokens`、`max_tokens`、`output_limit`、`hold_tokens`、`usage`、`output_lower_bound`、`client_disconnected`、`simulated` 等）；失败为 `event: error`（`code` + `neuxmind` 计量对象）。chat 的计量事件在 `[DONE]` 之后，客户端须读到连接关闭。
  - 流内错误码：`output_limit_reached`、`upstream_interrupted`、`upstream_stream_error`、`usage_missing`、`stream_duration_exceeded`（这五种按 hold 计入并待核对）、`usage_exceeds_reservation`（会话锁定）、`call_marked_lost`、`internal_error`。
  - 客户端中途断开：服务端继续读完上游并按实际 usage 结算；客户端应查询 `GET /gateway/sessions/{id}` 获取最终用量。
  - 事件命名空间（第 02 轮审查修复 Q2-03）：上游 `event: error` 以 `event: upstream.error` 转发；事件名或 data `type` 以 `neuxmind.` 开头（不分大小写）的上游事件一律丢弃；SSE 按 WHATWG 规则切行（CRLF/LF/单独 CR 均为换行），转发字段不含 CR/LF，上游无法伪造网关事件。客户端只应把带 `neuxmind` 计量对象的 `error` 或 `neuxmind.metering` 视为终止事件。
  - 时限（Q2-02）：总时长 `stream_max_seconds` 按墙钟执行（看门狗线程中断上游），注释行算作活动但不延长总时长；上游完全静默超过 `upstream_timeout_seconds` 即中断。
  - 会话 `base_url` 由服务端配置的 `public_base_url` 生成；SDK 以客户端实际连接的地址构造 `openai_base_url`，服务端给出的地址另存为 `server_base_url`。用 openai 库时它读到 `[DONE]` 即停，计量需用 `session.status()` 查询。
- N-09：`tool_choice`、`tools[].function`、消息内 `tool_calls` 的嵌套字段做严格白名单校验。

### 4.4 可安装包
- `spec/` 与 `runtime/` 提供 `pyproject.toml`（包名 `neuxmind-spec`、`neuxmind-runtime`），可 `pip install -e`；保留仓库内相对路径引导以兼容现有测试。

### 4.5 桌面端接入新三层（brainos / web）
- 机器人详情显示标准能力表（优先读 `/v2/manifest`，失败回退 v1），标出 `simulated`、各能力参数与安全级。
- 技能包：从本地文件夹选择 skill.package.v1 → 预检兼容性（spec.check_compatibility）→ 安装到机器人（`/v2/skills/install`）→ 已安装技能列表。
- 云平台账户：设置云平台地址、登录（会话保存在本机凭据存储，权限 600）、显示邮箱验证状态、钱包余额、按当前机器人 manifest 过滤的可用模型（`/catalog/models/compatible`）。本轮**不**把工程助手切到云网关（避免重复计费，留到第 03 轮）。
- 急停锁存时隐藏/禁用"准备启动工作"主按钮（第 01 轮 P3）。

---

## 5. 第 03 轮契约（2026-11-08，总指挥）

### 5.0 平台基线（第 03 轮首项）
- **Python 3.10 支持**：spec、runtime、sdk 必须在 Python 3.10 上安装并通过全部测试（NVIDIA Jetson JetPack 6 / Ubuntu 22.04 自带 3.10）。`requires-python = ">=3.10"`；不得使用 3.11+ 才有的标准库（如 `tomllib`，需回退）或语法。
- **解释器可执行**：root 运行时降权后，技能用户必须能执行所用的 Python 解释器（Jetson 上用系统 python3.10；不要用 /root 下的私有解释器），否则 `/v1/status` 显示 `python_usable=false`。
- **非 root 运行**：runtime 以普通用户运行时（无法降权到独立用户、无 cgroup），沙箱以"尽力而为"模式工作并在 `/v1/status` 标明；测试套件在非 root 下必须全部通过或明确 skip（带原因），不得挂起。
- **连接上限计数**：查明 stop 驱逐空闲连接后普通请求偶尔获得 200 的根因（第 02 轮已知问题），修复计数或在契约中准确描述行为。

### 5.1 端侧模型安装（runtime）
- `model.install` 由**运行时原生提供**（不依赖适配器）：运行时配置 `--models-dir` 后自动把 `model.install` 加入 manifest（参数 `runtimes` 来自配置，如 `["onnxruntime"]`），仿真本体也可用。适配器若自带实现，以适配器为准。
- 安装流程：`package_ref`（https、或运行时配置允许的 `file://` 目录内路径）→ 流式下载到临时文件（大小上限可配，默认 2 GiB；超时；不跟随跨协议重定向）→ 校验 SHA-256 → 原子移动到 `models/<sha256>/` → 写 `model.json`（model_id、runtime、来源、安装时间）。校验失败删除临时文件并返回失败。同一 sha 重复安装幂等。
- 安全级 `system`：需要 `hardware_authorized=true`（语义为"现场确认可以变更端侧软件"），不触发任何运动；安装期间运动能力不受影响；停止不会被安装阻塞。
- 新端点：`GET /v2/models`（已安装列表）、`DELETE /v2/models/{model_id}`（需授权；正在被运行中技能使用时 409 `model_in_use`）。技能在 `ctx.models` 中只读获得已安装模型路径（沙箱内以只读方式可见）。
- `/v1/models/install` 走同一实现，且为**异步**：返回 `{accepted, run_id, status, model_id}`，轮询 `/v2/runs/{run_id}`。
- manifest 顶层 `model_runtimes` 必须与 `model.install` 能力的 `params.runtimes` 一致（运行时原生提供时由运行时填充），云端按它过滤端侧模型包；`model.install_*` 事件带 `run_id`；`/v2/models` 返回 `model_id`（目录 ID，可空）、`name` 与 `sha256`。
- 已实现细节（runtime 996d40b）：已安装模型的标识即包的 SHA-256；`model.install` 输入可带可选 `model_id`/`name`（目录 ID，用于展示与查找）；急停锁存期间允许安装（不是运动）；重定向仅同协议跟随、每跳复查，跨协议一律拒绝。

### 5.2 遥测持久化与回放（runtime）
- 事件写入有界 SQLite 环形存储（默认 100k 条，可配），`GET /v2/telemetry?since=<seq>` 跨重启连续；`GET /v2/runs/{run_id}/events` 回放某次运行的全部事件。写入不得进入停止路径（停止先执行，事件异步落盘）。

### 5.3 工程助手切到云网关（desktop ↔ cloud）
- 桌面端已登录云平台时，工程助手的"平台模型"改为调用云网关会话：`POST /gateway/sessions` 开会话 → 把 `base_url` 与 `access_token` 交给内置工程运行时（Responses 流式工具调用）→ 结束时 `close`。计量与扣费**只在云端**发生：该路径不得调用本地 `commerce.reserve/settle`（避免重复计费）；本地只记录会话 id 与云端返回的计量。未登录云平台时保持原有本地资源路径不变。
- 云网关需确认与内置工程运行时（Codex app-server Responses 流式 + 函数工具）兼容；不兼容的参数在云端白名单内明确报错。
- 已实现（cloud 3564d9e / desktop a8d1625，依据真实 Codex 0.146.1 录制的请求体）：白名单新增 `tools[].namespace`（仅含函数工具、不嵌套、合计 ≤128）、`function_call.namespace`、`function_call_output.output` 纯文本片段列表、`reasoning` 输入项（全部字节计入输入上界）；`store` 只接受 `false`；`include` 只接受 `["reasoning.encrypted_content"]`；`prompt_cache_key` 由网关按组织重写（防跨租户缓存）；`client_metadata` 校验后丢弃。继续拒绝 `previous_response_id`、`conversation`、`prompt`、`background`、内置工具、图片/文件输入。
- 流式：上游终止事件（`response.completed/incomplete/failed`、chat `[DONE]`）在**结算完成后**才转发，事件顺序不变——保证工具循环的下一次调用不会撞上 `session_busy`。
- 桌面端云平台模型的 Token 预算下限 80000（当前 Codex 单次请求输入上界约 5 万）。Codex 升级时必须重新录制 `cloud/tests/fixtures/` 并重跑兼容测试。

### 5.4 SDK
- `nx models install <model_id> --robot R --authorize`：从云端获取授权模型包信息（`/catalog/models/{id}/package`）→ 调用机器人 `model.install`；`nx models installed --robot R`；`nx telemetry tail --robot R [--run ID]`。

### 5.5 云平台
- Echo 替身非流式 `/responses` 补 `output` 字段（SDK P3）。
- 模型包下载：`/catalog/models/{id}/package` 可返回短时签名下载地址（运营配置对象存储时）；未配置时返回原始 `package_ref`，并标明需要设备可直接访问。

---

## 6. 第 04 轮契约（草案，2026-11-08，总指挥）：AI 本体适配助手 v2 与能力一致性测试

目标：用户拿到一台新机器人 → SSH 调查生成带证据的本体档案（已有）→ **AI 依据档案与能力标准生成 RobotAdapter 初稿** → **能力一致性测试套件自动验证** → 产出"适配报告"，说明哪些标准能力已可用、哪些未实现、哪些需要现场验证。这是"快速配置到任何机器人本体"的核心路径。

### 6.1 能力一致性测试套件（runtime：`neuxmind_runtime.conformance`）
- 输入：任意 `RobotAdapter` 实例（或运行中运行时的 URL + 令牌）、测试级别 `static | dry | motion`。
  - `static`：manifest v2 校验、声明能力与 spec 一致、参数合法、必备能力存在、`model_runtimes` 一致性。
  - `dry`：所有 `state`/`stream` 能力可读且输出通过 `validate_payload`；`safety.stop` 可调用且幂等；看门狗、急停锁存、停止代数在适配器上生效（用零速度/零位移的命令验证，不产生运动）。
  - `motion`：每个运动能力执行最小幅度动作并验证反馈（需要 `hardware_authorized=true`；仿真本体可自动运行；真机只能在现场确认后运行）。
- 输出：`conformance.report.v1` JSON：每项能力 `passed | failed | skipped(reason)`、证据（读数、时延、停止响应时间）、`simulated` 标记、总体结论（`static_ok`、`dry_ok`、`motion_ok`）。报告不等于现场验收证据（ADR-011 不变）。
- 两个仿真本体必须 100% 通过三个级别；ROS 2 适配器在 rclpy 替身上通过 `static` 与 `dry`。

- 已实现（runtime 09c1297）补充：
  - 报告字段：`levels.{static,dry,motion}`、`capabilities[].levels`、`skip_kind`（`not_applicable` | `blocked`）、`thresholds`、`prelatched`、`aborted`、`final_latched`、`warmup_s`；`field_acceptance:false`；`evidence_kind:"conformance"`（仿真为 `"simulation"`），从不为 `device_run`。
  - 判定：某级别 ok ⇔ 已运行、无 failed、无 blocked、且下级全部 ok。
  - 端点：`POST /v2/conformance {request_id, level, hardware_authorized}`（异步）、`GET /v2/conformance`、`GET /v2/conformance/{id}`；错误码 `conformance_busy`、`runs_active`、`invalid_level`、`conformance_not_found`，状态可为 `unknown`。dry 级的零值指令与解除自身锁存会带 `hardware_authorized=true`（运行时要求所有运动类调用授权），但不产生运动。
  - 测试安全规则：有运行未结束则拒绝；只解除自己造成的锁存；测试前已锁存则保持；中途有人停止即中止并保持锁存；motion 中 1 s 内未停下即中止并保持锁存。
  - CLI：`neuxmind-runtime conformance --adapter … | --url … --token … [--mode server|client] --level … [--authorize] --out …`，退出码 0 通过 / 1 未通过 / 2 用法错误。
  - 已知限制：在运行中的运行时上测试时不阻止其他客户端同时下发运动。
  - 报告绑定（runtime b0e53c7）：`manifest_digest` = SHA-256(UTF-8(`json.dumps(validate_manifest_v2(m), sort_keys=True, ensure_ascii=False)`))，m 为 `/v2/manifest`（含运行时原生追加的 `model.install`），与云端 `catalog.manifest_digest` 一致；另带 `manifest_v1_digest` = 桌面端 `digest(manifest_v1_view)`（即 `/v1/evaluate` 的 `robot_manifest_digest`），两者不可互换。
  - dry 新增：`model.install.refuses_bad_requests`（未授权、非法 sha、元数据地址、`file:///etc/passwd` 均被拒且已安装列表不变）；`legged.posture.refused_without_motion`（非标准值 400、锁存期间被拒、之后 health 正常，均不到达适配器）。仿真倍速下可中断窗口按真实时间等长。

### 6.2 适配器生成（desktop 工程助手 + runtime 模板）
- runtime 提供适配器脚手架 `neuxmind_runtime.scaffold`：输入能力选择与参数（来自档案），生成可安装的适配器包（`adapter.py` + `adapter.json` + 测试），未实现的能力方法抛 `NotImplementedError` 并在 manifest 中不声明。
- 桌面端新增工作流模板"本体适配"：阶段 1 读档案并映射到标准能力（输出映射表：每项能力 → 证据 evidence_id → 实现方式，如 ROS 2 话题 / 厂商 SDK / 串口协议 / 不支持）；阶段 2 用脚手架生成适配器并实现可实现的能力；阶段 3 运行一致性测试 `static`+`dry`（对接替身或仿真）；阶段 4 生成适配报告。每阶段人工验收（沿用工作流验收门）。
- 生成的适配器默认部署前必须在真机上跑 `motion` 级别并现场确认（不在本轮自动进行）。
- 已实现：输入 `neuxmind.adapter_scaffold.v1`；实现提示种类 `ros2 | sdk | serial | http | todo`；生成包含 `pyproject.toml`、`adapter.py`（`IMPLEMENTED` 为唯一开关）、`adapter.json`、`tests/`、README；缺必填参数直接报错不猜限值；`safety.stop` 或 `system.health` 未实现时拒绝加载。ROS 2 模板覆盖 `/cmd_vel`、odom、battery、imu、joint_states、amcl、Nav2、stop、health。
- 能力运行（如 `model.install`）的 `/v2/runs/{id}.progress` 随动作实时更新，`progress_detail.feedback = {bytes, total}`。
- 能力标准 v1.1：新增 `gripper.state`、`arm.ee_state`（只读状态），使夹爪与末端位姿的停止/最小动作可被自动验证。

### 6.3 SDK（已实现 sdk ead00a8）
- `nx adapter new`（`--from-manifest` 生成的新本体不继承 `instance_id`、`simulated=false`；`--caps` 必须用 `--param` 给出必填限值）、`nx adapter test <path>`（进程内，static 走 `offline_manifest()`）、`nx robot conformance <robot> [--level] [--authorize] [--wait] [--upload] [--json]`（机器人目标由此命令覆盖）、`nx models list --require-level dry`。旧运行时无 `/v2/conformance` 时报 `conformance_unsupported`。

### 6.3（原草案）
- `nx adapter new <name> --from-manifest|--caps base.velocity,...`、`nx adapter test <path|robot> --level static|dry|motion [--authorize]`、`nx robot conformance <robot>`。

### 6.4 云平台（已实现 cloud f0eacde）
- `POST /robots/{id}/conformance`：只保存摘要并标记 `self_reported:true`；报告须与登记 manifest 的 `instance_id`、`simulated`、能力集合一致；manifest 变更后旧摘要标为 stale；较旧报告不得覆盖较新报告。拒绝 `evidence_kind:"device_run"` 与 `field_acceptance` 非 false（含缺失）。
- `require_level=static|dry`（`/catalog/models/compatible`、`GET /catalog/models`，可传 `robot_id`）：能力在该级及更低级都 passed 才算可用；`safety.stop` 不可用则全部不可用；`model.install` 不可用则 `model_runtimes` 视为空；无摘要时 409 `conformance_required`。`POST /robots/{id}/skills/check`。
- 错误码：`invalid_conformance_report`、`device_run_refused`、`field_acceptance_refused`、`conformance_robot_mismatch`、`conformance_manifest_mismatch`、`conformance_outdated`、`conformance_required`、`robot_required`、`invalid_level`、`invalid_skill`。
- 已知限制：报告为运行时自报、未签名，可被适配器代码影响（ADR-019）。报告的 `manifest_digest` 必须等于登记记录的 digest（否则 409 `conformance_manifest_mismatch`）；不带 digest 的旧报告保存为 `digest_verified:false`，`require_level` 不采信；`generated_at` 不得晚于服务器时间 5 分钟以上。
- Q4-22 决定：`model.install` 的 dry 通过只证明"非法请求被拒绝"，不证明能成功安装；在 `require_level=dry` 下端侧模型包仍列为可用，但云端与客户端必须标注"安装能力未经实际验证"。真正的安装验证留待真机现场。

### 6.4（原草案）
- 设备群记录保存最新一致性报告摘要；模型市场/技能兼容性可选择"仅显示已通过 dry 级别的能力"。

### 6.5 第 04 轮复审修复后的契约（已实现 runtime df7600e，2026-11-09）

- `/v1/status` 增加只增不减的 `stop_count` 与 `stop_epoch`；`/v2/reset` 可带 `expect_stop_count`，不符返回 409 `stop_count_mismatch`。
- 一致性测试运行期间，其他来源的运动、技能运行、`/v1/start`、`/v1/evaluate` 与复位返回 409 `conformance_running`；停止始终可用。
- 测试安全规则以 `runtime/README.md` §3.1 为准（不清除他人锁存；dry 永不位移；任何 motion 失败即停车锁存中止；停止后要求持续静止并在见到运动时再停；幅度上限：导航 0.2 m、关节 0.1 rad；自报倍速不采信）。
- 以下情况记 blocked、不下发：无 `localization.pose` 时的导航；无 `gripper.state` 时的夹爪中断；无 `arm.ee_state` 时的末端；未声明 `joint_names` 时的机械臂与动作块；黑盒模式的探测。
- `conformance.report.v1` 新增字段：`trust{self_reported, same_process_as_adapter, note}`、`executed_time_scale`、`time_scale_rebased`、`dry_guard`、`external_motion_refused`、`final_motion{checked, still, restops, motion_seen, held_still_s}`、`end_state`、`restops`、`call_timeout`。新用例 `safety.stop.final_still`、`motion.base.velocity.turn`、`motion.base.velocity.lateral`。阈值新增 `turn_rate`、`cross_axis_tolerance`、`adapter_call_timeout_s`（5 s）、`reset_call_timeout_s`（15 s）、`restop_*`。
- 新事件 `safety.reset_raced`：复位期间收到停止时，复位返回后再停一次并保持锁存。
- 报告未能确认本体静止时，必须写明警告并提示使用硬件急停，不得写"已停车"。
- 脚手架输入白名单：标识符只允许 `[a-z0-9_]`，拒绝与标准库或常用包同名；显示文本禁止换行、反引号、`[`、`]`；生成代码只通过 `repr()` 嵌入外部字符串。
- `model.install` 的 dry 通过只证明会拒绝非法请求，不证明能安装（Q4-22），UI 须标注"安装能力未经实际验证"。

### 6.6 第 04 轮复审 2 修复后的契约（已实现 runtime fbd9f37，2026-11-09）

- 套件中止、调用超时之后的所有再停都是**计数的停止**（停止计数 +1，已锁存时保留原锁存原因）。`/v2/stop` 新增可选字段 `keep_reason: bool`。
- 监督器在 `adapter.reset()` 执行期间收到的任何停止（含不计数的停车、看门狗停车）都视为竞态：保持锁存，复位返回后再停一次，发出 `safety.reset_raced`。
- dry 级不向适配器发送任何机械臂目标或 joint_position 动作块，相关用例记 `not_applicable`（证据含 `dry_skipped: true`）；每个 dry 用例前后比较本体位置，超过 `dry_displacement_m`（1 cm）/`dry_displacement_rad`（0.01 rad）即停止、锁存、中止。
- motion 级机械臂指令执行时检查关节包络（`arm_envelope_rad` 0.02 rad），超出即停止中止。
- 中止后要求持续静止 `restop_hold_s` = 5 s，总时长 `restop_deadline_s` ≤ 12 s；报告定稿前再核对一次（`final_motion.final_recheck`），未返回的调用记入 `final_motion.calls_in_flight`。`final_latched` 一律从运行时读回。

## 7. 第 05 轮契约（已实现 runtime b96677f，2026-11-09）

- **锁存守护**：锁存期间运行时每 0.1 s 读取 `base.odometry` 与 `joint.state`；见到运动只调用 `adapter.stop()`（间隔 ≥ 0.5 s），不解锁、不改原因、不计入 `stop_count`、不发运动指令。计数停止后 1 s 刹车宽限；操作员复位进行中只观察（超过 30 s 照常再停）。
  - `/v1/status` 新增 `latched_motion{enabled, sources, active, seen, restops, last_at, last_restop_at, last_evidence, seen_during_reset, read_errors, restop_interval_s}` 与 `reset_in_progress`。
  - 新事件 `safety.motion_while_latched`；`safety.reset_raced` 新增 `latched`。
  - 适配器契约：`base.odometry`/`joint.state` 也被守护使用；同一时刻只允许一个 `reset()`。
- **复位串行化**：`/v1/reset`、`/v2/reset` 在上一次 `adapter.reset()` 未返回（含已被放弃等待的）时返回 409 `reset_in_progress`。内存与磁盘锁存状态始终一致。
- **停止**：`/v1/stop`、`/v2/stop` 响应新增 `stop_count`；`keep_reason` 改为比较交换（仅当本次停止仍是最新且原因仍为本次占位值时才恢复旧原因），响应新增 `latch_reason_kept`。
- **动作块**：joint_position 空间未声明 `joint.state` 或 `joint_names` 时，提交即返回 400 `joint_state_required` / `joint_names_required`。
- **一致性报告**：`final_motion.guard_motion_seen`；黑盒模式读取 `/v1/status.latched_motion`。

## 8. 第 06 轮契约（已实现 runtime abab971、desktop 637d63d，2026-11-09）

- **锁存守护（续）**：刹停后记锁存快照（底盘 x/y/偏航、各关节按名字的位置）；离开快照超过 0.05 m / 0.05 rad 即再停，之后每再动 3 mm / 0.005 rad 再停一次（每两次再停之间的位移有界；总位移随驱动反复恢复而增长，见 §9 升级告警）。反馈不可用（NaN/无穷、长度不符、`velocity:null`、读取异常、关节名重复或集合变化）持续 0.25 s 即按"可能在动"再停（≤ 2 次/s），发 `safety.feedback_invalid_while_latched`；读取卡住超过 1 s 由监视线程再停。`latched_motion` 新增 `feedback_ok, feedback_problems, feedback_invalid, invalid_restops, last_invalid, read_in_progress_s, max_drift_*, displaced, settle_s`。
- 适配器可声明 `stop_settle_s`（限 1–5 s）作为受控坐下的刹停宽限；`safety.reset_raced` 新增 `by_latch_guard`；守护导致的复位失败带 `cause: latch_guard_reset_grace`，人工停止导致的带 `cause: stop_during_reset`。
- **复位卡住**：`/v1/status` 新增 `reset_in_progress_s`、`reset_hint`；409 正文 `{error, code:"reset_in_progress", message, hint, reset_in_progress_s}`。操作员出路：确认现场安全 → 重启运行时（锁存保留）→ 解除急停。
- **技能参数设备端校验**：直接运行、`/v1/start`（准入前检查全部步骤，含旧 release）、deploy/evaluate/validate 都按 skill.json 检查 `min/max`、`choices/options`、`length/min_length/max_length`；安装时校验声明本身与 `default`。400 正文 `{code, param, min, max, choices, message, field, problem, constraint, value, step?}`，`code` ∈ `param_out_of_range | param_choice | param_length | param_required | param_type | param_declaration_invalid`；未知参数仍为 `bad_request`（`problem: param_unknown`）。`/v1/deploy` 越界仍为 409 `validation_failed`。
- **桌面错误分类**：先按 `code`，再按设备/云端 HTTP 状态，最后才看文字；安全拒绝永不归为"服务出错"或"网络超时"。桌面接口错误体带 `code, device_status` 与字段信息。

## 9. 第 07 轮契约（已实现 runtime 6a91a6e、desktop b220885，2026-11-09）

- **锁存守护的稳健判据**：速度看单样本（> 3× 阈值）或 1 s 中位数；采样跳变门槛 max(3 mm, 8×噪声)，噪声取声明值与实测值（上限 1 cm）较大者；位移用 3 点中位数，60 s 无运动证据则参考点重置（偏航慢漂与里程计随机游走不算运动）；离开锁存快照超过 4× 限值始终算运动。
- **现场标定**：清单 `safety.latch_guard{pos_m, yaw_rad, joint_rad, noise_m, noise_rad, yaw_drift_rad_s, stop_settle_s(别名 settle_s), escalate_after}`，spec 校验（非数值/未知键拒绝，越界限幅）；运行时覆盖 `serve --latch-guard k=v`；优先级 运行时 > 清单 > 适配器属性 > 默认。ROS 2 配置 JSON 可写 `latch_guard`。
- **事件合并**：同类守护事件首条立即发出，之后每类每 10 s 最多一条，带 `count`/`summary`。
- **升级告警**：同一次锁存内因运动再停 ≥ 10 次且跨度 ≥ 30 s，发一次 `safety.stop_not_obeyed{ts, message, note, restops, displacement_m, …}`；`latched_motion.stop_not_obeyed: bool`、`stop_not_obeyed_at`、`stop_not_obeyed_detail`。桌面据此显示"请立即使用硬件急停"横幅，见到 `safety.reset` 或未锁存即清除。
- `latched_motion` 另增 `config, config_source, config_notes, noise_used, rebases, motion_restops, events_suppressed, event_interval_s`。
- **参数约束**：`minimum`/`maximum` 为 `min`/`max` 别名（不一致拒绝安装）；`object` 内每个数值、`pose`/`vector` 逐项受 `min`/`max` 约束；`str`/`bool`/`enum` 上声明范围、`min > max`、声明含 NaN 均安装即拒（`param_declaration_invalid`）；枚举按类型严格比较；参数嵌套上限 64 层；解析器溢出返回 400 `bad_request`。
- **桌面**：`options:[{value,label}]`/`choices` 渲染为带名称的下拉框，提交原始带类型值；参数拒绝按 `param_type / param_choice / param_length / param_required / param_declaration_invalid` 分别说明；桌面自身 500 带 `desktop_internal_error`，提示先核对机器人状态而非重试；复位类错误透传 `reset_in_progress_s, cause, hint`。

### 9.1 第 07 轮发布前修复（runtime a0c2fce，desktop 5ccbd26 及其后）

- **偏航硬上界**：相对锁存快照、从不重置：`min(max(yaw_total_rad, yaw_rad), yaw_rad + yaw_drift_rad_s × 锁存时长)`；新标定键 `yaw_total_rad`（默认 0.2 rad，0.05–1.0）。参考重置不再叠加漂移容许。漂移大的本体须现场声明 `yaw_total_rad`。
- **棘轮**：某通道在位移 R 处再停后，下一硬界为 R + 限值；"已偏离"状态容许实测里程计游走（≤ 1.5 mm/√s，有净趋势或刚有运动证据时不采信）。
- **升级**：只计"距上次 ≤ 180 s 且其间确实又动了"的再停。触发条件之一：连续 ≥ `escalate_after`（3–30）次且跨度 ≥ 30 s；或连续 ≥ 3 次且离快照 ≥ 0.5 m。事件新增 `streak_restops, streak_s, trigger`。
- **放宽告警**：任何标定键比默认宽松时，启动发 `safety.latch_guard_relaxed{message, keys:[{key, value, default, source}], notes, ts}`；`latched_motion.relaxed`、`relaxed_keys`（同结构）、`hard_limits`、`walk_used`、`escalation_streak`。桌面在机器人详情显示琥珀色警告。
- **过期复位**：`/v1/reset`、`/v2/reset` 接受 `expire_s`（1–30，默认上限 30 s）；`adapter.reset()` 超时后才返回时保持锁存（内存与磁盘）、再停一次、发 `safety.reset_expired`，返回 409 `cause: reset_expired`。桌面发送 `expire_s`，取值见 §9.2。
- **参数**：带范围的 `object` 只接受数值（字符串/bool/null → 400 `param_type`）；`pose` 的范围只约束 x/y/z。
- **桌面**：硬件急停横幅对真机全局可见（`robots/safety` 只读轮询，锁存时 ≤ 1 次/2 s）；浮点选项由桌面后端按声明恢复类型后发送；仿真机器人不提"硬件急停"。

### 9.2 第 07 轮第二次修复（runtime abe0650 + 69bcbff，desktop 1bc90f2 + b080be6）

- **发布规则**：锁存守护在所有复审场景中，真实位移与首次发现位移都不得大于 v0.13.0；仅允许告警频度不同（见 `reviews/2026-11-09-iter07-rereview.md` 与修复报告 A/B 表）。
- **偏航**：`yaw_total_rad` 默认 **0.05 rad**，含义为"每次偏航再停后的步长上限"；再停后下一界 `R + min(max(yaw_total_rad, yaw_rad), yaw_rad + 漂移×Δt)`。IMU 漂移 0.5°/min 在默认标定下约 10 次/小时再停，声明 `yaw_total_rad: 1.0` 后为 0。
- **"已偏离"状态贯穿整个锁存期**；位置与关节从锁存快照起算、不再重置；只有偏航参考为吸收漂移而重置。
- **预防性停止**：v0.13.0 会再停、而本版因游走/漂移容许不计为运动之处，静默再调用 `stop()`（HAL 要求幂等），不发事件、不计再停、不升级，≤ 1 次/s，计数 `latched_motion.precautionary_stops`；另有 `walk_measured`、`hard_limits.*_step`。
- **漂移提示**：每次锁存最多一次，离快照首次超过首次发现界时发 `safety.latched_drift_notice{message, channel, displacement, unit, ts}`，不升级、不额外再停；`latched_motion.drift_notice`、`drift_notice_at`、`drift_notice_detail`。桌面在锁存守护面板显示。
- **升级**：新增"离快照 ≥ 0.5 m 且 ≥ 3 次运动再停（不论间隔）"。
- **噪声**：实测噪声采信上限 3 mm / 2 mrad，更吵的传感器须在清单声明（触发"标定放宽"）。
- **复位**：`/v1/status.capabilities{reset_expire: true, reset_expire_max_s: 30, reset_expire_min_s: 1, reset_expected_s, reset_expected_note}`、顶层 `reset_expected_s`；清单 `safety.reset_expected_s` 由 spec 校验，(0, 30] 秒，非法即清单无效。桌面 `expire_s = clamp(max(8, reset_expected_s + 5), 8, 30)`，对机器人的 HTTP 超时 = `expire_s + 4`；仅当 `capabilities.reset_expire === true` 时才说"超时复位会作废"。
- **参数**：`float` 及 `vector/floats/pose` 元素的范围与 choices 按数值双向比较（`1 == 1.0`）；bool 与数值、str 与数值仍严格区分；桌面按声明类型序列化所有发送路径。

### 9.3 第 07 轮第三次修复（runtime a90828f，2026-11-09）

- **预防性停止 = 逐条复刻 v0.13.0**：守护内 `V013Replica` 用与 v0.13.0（dbab711）相同的原始采样与判据（速度、相邻跳变、离快照超过 `pos_m/yaw_rad/joint_rad`、"已偏离"后离上次再停超过 3 mm / 5 mrad），无噪声/游走/漂移容许，无连续样本要求，自身节奏 ≥ 0.5 s。复刻会再停而本版未调用 `stop()` 时，静默调用一次 `stop()`（不发事件、不计再停、不升级，计入 `precautionary_stops`）。`precaution_interval_s` 默认 0.5，≤ 0 关闭。代价：v0.13.0 会再停的静止噪声本体，现在以同频度静默 `stop()`，不再产生事件（节奏见 §9.4）。
- **总指挥接受**：伪造游走 + 蠕动/停后再走的少数场景中，"计数的"首次再停晚于 v0.13.0，但首个 `stop()` 调用与真实位移与 v0.13.0 相同（Q7c-06 同类）。
- **漂移提示**：每次复位成功即作废；`drift_notice*` 仅在锁存期间报告、只报当前锁存；复位后立即再停的新锁存会发自己的提示。
- **`safety.reset_expected_s` 取值范围 (0, 25]**（桌面 `expire_s = reset_expected_s + 5` 恒 ≤ 30）。

### 9.4 统一停止节奏（runtime 08fdf61，2026-11-10）

- 守护发出的所有 `stop()`（计数再停、预防性停止、反馈无效、读取阻塞）**共用一个节奏**：相邻两次 ≥ `restop_interval_s`（0.5 s），即合并后 ≤ 2 次/s。
- 预防性停止开启时（默认），守护的 `stop()` 时刻与 v0.13.0 的再停时刻逐条相同：计数再停只在 v0.13.0 规则于同一采样也看到运动或反馈无效时调用 `stop()`，否则**合并**进复刻的停止序列（照样计数、发事件、参与升级，不单独调用）。复刻到期而守护 0.5 s 内刚因读取阻塞调用过 `stop()` 时，视为复刻在那一刻的再停。
- 操作员停止（`/v1/stop`、`/v2/stop`）不受此节奏限制，总是立即执行。
- `latched_motion` 新增 `stop_calls`、`merged_stops`。
- HAL：守护重复发出的 `stop()` 间隔不小于 0.5 s；驱动对 `stop()` 做去抖时，窗口不应超过 0.5 s。

### 9.5 读取阻塞再停的合并（runtime 132fd76，2026-11-10）

- 守护记录每次 `stop()` 的来源（守护线程第几拍 / 监视线程的读取阻塞再停）；"本采样已停"只认守护线程本拍发出，不再用机器人时刻相等推断。最近一次为读取阻塞再停时，复刻到期一律合并进那一次、参考取那次停止时的位置（与 v0.13.0 一致），粗时钟（≥ 20 ms）与高分辨率时钟行为相同。
- 合并的计数再停事件带 `merged: true, adapter_stopped: false`；单独调用了 `stop()` 的为 `merged: false`。
- HAL（`hal.py`）：守护重复 `stop()` 相邻 ≥ 0.5 s；驱动不得在超过 0.5 s 的窗口内去抖或丢弃 `stop()`；适配器 `clock` 应为高分辨率单调时钟。
