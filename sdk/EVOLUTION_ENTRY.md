## 第 01 轮 · 2026-11-04 · 开发者 SDK 与 `nx` 命令行：几分钟让机器人开始工作（sdk/）

- 目标：实现 INTERFACES_V1 §3——Python SDK（端侧运行时客户端、云平台客户端、技能开发 API 与本地测试工具、技能包工具、模板）和 `nx` 命令行，并让"5 分钟上手"在仓库里真实跑通。
- 关键决策：
  - **规则不复制**：兼容性只调用 `spec.manifest.check_compatibility`，参数默认值用 `spec.capabilities.check_field`；SDK 只在其上加中文原因（缺哪项能力、哪个参数要求多少而本体声明多少）。
  - **运动在本地把关**：spec 中 `safety=motion` 的能力、`x.*` 扩展、所有技能运行和 `reset` 必须显式 `authorize=True` / `--authorize`，否则 SDK 本地拒绝且零请求；`stop` 永远可用。
  - **不自动重放**：`request_id` 自动生成（uuid4），SDK 从不自动重试会改变状态的请求；响应丢失时报 `outcome_unknown` 并带回原 `request_id`。
  - **技能 API 与运行时同源**：`neuxmind.skill.Skill` 继承 `neuxmind_runtime.skills.Skill`，异常类直接复用运行时的类；`Context` 只是类型提示用的 Protocol，不包装 SkillContext。entry 约定与运行时一致（`<文件>.py:<函数或类>`）。
  - **本地测试 = 真运行时**：`neuxmind.skill.testing` 在进程内构造 `EdgeRuntime` + 仿真适配器，经过与机器人相同的运行管理与安全监督；结果标注 `simulated`。
  - `nx sim up` 调用运行时自己的 CLI（子进程），随机令牌，每次全新状态，自动 `robot add`；第一个仿真本体成为默认机器人。
  - 配置 `~/.neuxmind/config.json`（`NEUXMIND_HOME` 可覆盖），原子写入，权限 600/目录 700；回环地址不走环境代理。
- 分工：SDK Agent → `sdk/neuxmind/`（errors、_http、_bootstrap、robot、cloud、package、templates、config、sim、cli、skill/{__init__, testing}）、`sdk/tests/`（4 个测试文件 + 夹具）、`sdk/README.md`、`sdk/pyproject.toml`（console script `nx`）、`sdk/PROGRESS.md`。
- 改动：仅新增 `sdk/` 目录；未修改其他目录。
- 验证：
  - **协议/仿真通过**：`python3 -m unittest discover -s sdk/tests` 28 个测试全部通过（约 16 s，连续 3 次稳定）。真集成：运行时 `GatewayServer` + SimQuadruped/SimArm（随机端口）；云平台 `create_app` + uvicorn（随机端口、SQLite 临时库，运营员上架 EchoProvider 替身模型并调额）；CLI 既用 `main(argv)` 也用真实子进程（含 `nx sim up` 拉起运行时进程）；4 个模板生成后各自的测试在子进程里通过。
  - 覆盖：RobotClient v2 全流程与 v1 manifest；未授权运动本地拒绝（断言零请求）；stop 后 `estop_latched`、reset 后恢复；跨本体 check（patrol 对四足兼容、对机械臂缺 `base.navigate_to`；参数约束 `dof ≥ 6` 的解释）；云注册→登录→钱包→按 manifest 过滤模型→授权→推理→账本扣费。
  - **5 分钟上手实跑**：用 `pip install -e sdk/` 安装后的 `nx` 在全新 `NEUXMIND_HOME` 下逐条执行 README 第 2 节，全部按预期输出。
  - 其他目录测试不受影响：tests/ 72（20 跳过）、spec/tests 7、runtime/tests 71、cloud/tests 73 全部通过（安装 SDK 后 runtime 的 `wave` 示例改为导入 SDK 的 `Skill`，仍通过）。
  - 旁证：开发期间审查 Agent 的变异测试临时改动了 runtime 的账本指纹比较与 stop 路径，SDK 测试在这些时刻失败，说明 SDK 测试对这两条不变式敏感。
  - **真机未验证**；**真实模型与支付未验证**（只用 Echo 替身；支付只验证了"未配置渠道明确报错"）。
- 遗留：→ 下一轮候选
  - 契约：把 `spec/` 与 `runtime/` 做成可安装包（目前 SDK 靠仓库相对路径或 `NEUXMIND_SPEC_PATH`/`NEUXMIND_RUNTIME_PATH` 定位）；运行时增加事件推送（SSE/WebSocket）以替代 `wait` 轮询；`/v2/skills/install` 支持"仅检查不安装"（dry-run）以便远端 check 与安装用同一判定点；明确 `invoke` 中 `hardware_authorized` 对非运动 command 是否必需。
  - SDK：封装网关计量会话 `/gateway/sessions` 与 OpenAI 兼容调用；`nx models install`（取授权包 → 机器人 `model.install`，需运行时先实现）；令牌接入系统钥匙串；`nx skill publish` 到云端技能市场（需云端接口）；桌面端集成可直接复用 `RobotClient`/`explain_compatibility`。

## 第 01 轮修复 · 2026-11-05 · SDK：支持云端邮箱验证与网关参数白名单（sdk/）

- 目标：跟上云端 118c54f（邮箱验证、会话参数白名单），修复 `sdk/tests` 中 `CloudTests.setUpClass` 因运营员未验证而失败的问题。
- 关键决策：
  - `CloudClient.verify_email(token)`（不需要登录）、`resend_verification()`；`register()` 原样返回 `verification` 与仅 dev/test 才有的 `dev_verification_token`。
  - 服务端错误码 `email_unverified`、`parameter_not_allowed`、`n_unsupported`、`session_overage_locked`、`verification_token_invalid/expired`、`email_sender_not_configured`、`already_verified` 统一附加中文处理建议（`details["hint"]`，同时写进消息）。
  - `/gateway/infer` 只有 `max_tokens` 一个生成参数：`infer(**options)` / `nx infer --option k=v` 中 `n≠1` 本地拒绝为 `n_unsupported`，其他参数本地拒绝为 `parameter_not_allowed`（不发请求，不让参数被悄悄忽略）。
  - `nx verify-email <令牌>`、`nx verify-email --resend`；`nx register` 打印验证提示，dev 环境显示令牌并注明"仅开发环境"；`nx login`/`nx whoami` 显示是否已验证。
- 改动：`sdk/neuxmind/cloud.py`、`sdk/neuxmind/cli.py`、`sdk/tests/test_cloud.py`、`sdk/README.md`（5 分钟上手新增云端部分与验证步骤、CLI/API 参考）。
- 验证（协议级，云端 dev/test 记录型发信器，未发真实邮件）：`sdk/tests` 29 个全部通过（新增 1 个测试方法，并在 2 个云端测试中增加验证相关断言：运营员/开发者用 `dev_verification_token` 验证；未验证用户购买/推理得到带提示的 `email_unverified`；无效令牌、重发后旧令牌作废、`already_verified`；infer 参数本地拒绝；CLI verify-email 全流程）。其他目录：tests 82（跳过 20）、spec 11、runtime 109、cloud 112 全部通过。
- 遗留：SDK 仍未封装网关计量会话（OpenAI 兼容调用），会话白名单只由服务端执行，SDK 仅做错误提示映射；真实 SMTP 投递未验证。

## 第 01 轮复审修复 · 2026-11-05 · SDK：会话绑定的邮箱验证、邮箱认领、找回密码（sdk/）

- 目标：跟上云端 48999d2（复审 N-01 预注册劫持修复）。
- 关键决策：
  - `CloudClient.verify_email` 改为带会话提交（未登录本地报 `not_logged_in`）；`nx verify-email` 用已登录客户端，并提示验证后其他会话与验证前的 API Key 已被吊销（显示 `sessions_revoked` / `api_keys_revoked`）。
  - 新增 `claim(token, password, name)`、`request_password_reset(email)`、`reset_password(token, password)`，对应 `nx claim <令牌>`、`nx password-reset request --email E`、`nx password-reset confirm <令牌>`；claim/reset 返回的新会话保存到配置。
  - `register` 遇到 202 `claim_sent` 不设置令牌、不登录，CLI 提示"该邮箱已被未验证账户占用，认领邮件已发送"，dev 环境显示 `dev_claim_token` 并注明仅开发环境。
  - 新错误码中文提示：`verification_wrong_account`、`email_taken`、`email_pending_verification`、`recovery_token_invalid/expired`、`session_required`。
- 改动：`sdk/neuxmind/cloud.py`、`sdk/neuxmind/cli.py`、`sdk/tests/test_cloud.py`、`sdk/README.md`。
- 验证（协议级，dev/test 记录型发信器）：`sdk/tests` 31 个全部通过（新增 2 个：认领与找回密码的客户端路径、CLI 路径；原测试改为本人会话验证，并断言未登录验证被拒、他人会话 `verification_wrong_account` 且令牌不消耗、验证后旧会话与旧 API Key 401）。其他目录：tests 82（跳过 20）、spec 11、runtime 126、cloud 125 全部通过（runtime 修复 Agent 当时有未提交改动，SDK 测试在其工作区状态下同样通过）。
- 遗留：SDK 无"修改邮箱"（云端也没有）；SMTP 真实投递未验证；网关计量会话仍未封装。

## 第 02 轮 · 2026-11-08 · SDK：网关计量会话与流式、`nx chat`、可安装依赖、计时测试加固（sdk/）

- 目标：按契约 §2.1/§4.3/§4.4 与 ADR-012/016，在 SDK 中封装云网关计量会话（非流式 + SSE 流式），提供 OpenAI 兼容入口与 `nx chat`；声明 `neuxmind-spec` 依赖；修 `test_cancel_and_wait_timeout` 计时不稳。
- 关键决策：
  - **会话对象即上下文管理器**：`CloudClient.gateway.open_session(model, max_tokens, ...)` → `GatewaySession`；离开 `with` 自动关闭结算（结果在 `close_result`），块内抛异常时也关闭且不掩盖原异常（关闭错误存 `close_error`）。
  - **流读到连接关闭**：自写 SSE 解析（`read1()` 分块，不用 `readline()`），chat 的 `[DONE]` 作为普通事件产出，最后产出 `neuxmind.metering` 并设置 `stream.metering`；流内 `event: error` → `GatewayStreamError(code, metering)`；流开始前的错误仍是 `NeuxMindError`（JSON + 状态码）。
  - **断流恢复不靠猜**：没收到终止事件 → 轮询 `GET /gateway/sessions/{id}` 直到 `inflight_tokens` 归零（服务端读完上游并结算），用"本对象看到的上一次调用之后"的会话计数差推算本次 `call_tokens` 与状态 → `StreamInterrupted(stream_interrupted)`；等不到或推算不出 → `outcome_unknown`，不重发。
  - **不自动重试**改变状态的请求；唯一例外是 `close()` 收到确定的 409 `session_busy`（关闭被拒、未执行）后，等在途调用结算再用**同一 request_id** 重发（关闭幂等）。
  - 不传 `stream_options`（本地拒绝，零请求）；其余参数白名单只由服务端执行（规则不复制），错误附中文提示；`session_busy` 提示"同一会话不能并发，需要并行请开多个会话"。
  - `openai_base_url` 用客户端实际连接的地址拼出（服务端给的 `base_url` 取自其配置的公网地址，另存 `server_base_url`）；SDK 不依赖 openai，README 给出 `openai.OpenAI(base_url=..., api_key=access_token, max_retries=0)` 用法。
- 改动：新增 `sdk/neuxmind/gateway.py`、`sdk/tests/test_gateway_sessions.py`；修改 `_http.py`（`open_stream`、错误解析抽出共用，原有语义不变）、`cloud.py`（`gateway.open_session/session/close_session`、`add_hint`、会话错误码提示）、`cli.py`（`nx chat`、`nx sessions show`）、`pyproject.toml`（依赖 `neuxmind-spec`）、`tests/_support.py`（`CuttingProxy` 断流代理）、`tests/test_robot_client.py`（计时加固）、README。
- 计时测试根因与修法：SDK 测试的仿真本体以 `time_scale=20` 运行，**机器人时间的窗口在墙钟上缩小 20 倍**（速度看门狗 0.5 s = 25 ms 墙钟），而 runtime 本轮默认把每个技能运行放进新的子进程（启动一个新解释器），负载下启动与取消/回收路径从几十毫秒变成秒级。在并发跑 runtime + cloud 测试的压力下复现到同文件的 `test_invoke_command_and_action_with_authorize`（5 轮中 1 次 `0.0 != 1.0`：断言读 `adapter.cmd` 时看门狗已清零）；`test_cancel_and_wait_timeout` 在本机 50+ 次压力运行中未复现，按同一机制加固：先同步到"技能进程真正发出第一条运动指令"再测 `wait_timeout`，并新增断言超时时运行仍为 `running`、`stop_reason` 为空、之后仍在继续发指令（证明 wait 不取消运行），取消后断言 `stopped` + `stop_reason=cancelled` + 未锁存；"等到终止状态"的活性上限由 10 s 改为 60 s（这不是被验证的语义）。限幅断言改为记录到达适配器的指令（`[1.0]`），不再与看门狗竞速。
- 验证（协议级；模型为云端 Echo 测试替身，`simulated=true`）：`sdk/tests` 48 个全部通过，连续 3 次稳定（新增 17 个会话测试）；变异测试在独立 git worktree 中做，23 个变异（流式读取、断流恢复、基线、close 等待、CLI 逐字输出、wait 不取消运行、取消不锁存等）全部被杀死。其他目录：spec 11、runtime 171（runtime Agent 未提交的工作区状态）、cloud 160、tests 113（跳过 20）全部通过。openai 3.46 手工验证：chat 非流式/流式、responses 流式、参数被拒 400 均正常（未纳入自动测试，SDK 不依赖 openai）。
- 遗留：`neuxmind-runtime` 待其 pyproject 提交后再声明依赖；Echo 替身非流式 responses 没有 `output` 字段（openai 的 `output_text` 不可用，云端替身问题）；断流恢复在同一会话被多个客户端同时使用时只能给 `outcome_unknown`；未接真实模型供应商。

## 第 03 轮 · 2026-11-08 · SDK：Python 3.10、端侧模型安装、遥测 tail、沙箱状态（sdk/）

- 目标：契约 §5.0（Python 3.10 基线）与 §5.4（`nx models install/installed/remove`、`nx telemetry tail`），并让 `nx robot status` 显示运行时第 03 轮的沙箱等级。
- 关键决策：
  - **3.10**：`requires-python = ">=3.10"`；SDK 本来就没用 3.11+ 的标准库/语法（逐文件用 `ast.parse(feature_version=(3,10))` 复核），48 个旧测试在 3.10.20 venv 里直接通过。唯一障碍在测试依赖：`cloud/requirements.txt` 固定的 SQLAlchemy 2.1.8 需要 3.11+，3.10 测试环境用 2.0.57（云平台代码在其上可导入并通过 SDK 的云端测试），README 写明。
  - **规则不复制**：下载、校验、原子安装、幂等、`model_in_use` 都由运行时判定；SDK 只负责编排（云端取包 → v2 invoke → 轮询）和中文提示。唯一的本地判断是"授权确认"与"机器人有没有 `model.install`"（都发生在任何请求之前）。
  - **授权确认**：`model.install`/删除不运动，但安全级是 `system`（变更端侧软件），SDK 与运动一样要求显式 `authorize=True`/`--authorize`，否则本地拒绝、零请求。
  - **未授权给购买提示**：云端 403 `entitlement_required` 转成"请先 `nx models buy <id>`（价格，钱包可用点数）"，机器人上什么都不发。
  - **目录 ID ↔ SHA-256**：已安装模型的 ID 是 SHA-256；invoke 时带 `model_id`/`name`=目录 ID，删除时先按机器人记录的 `name` 匹配，再回退到云端模型卡的包 SHA-256（不需要授权）——这样即使运行时没记录 name 也能按目录 ID 删除。
  - **取包优先 `install.args`**：为 §5.5 的短时签名下载地址留好路径，缺字段本地 `bad_response`。
  - **遥测游标**：只取 `seq > 游标`、自动翻页（`/v2/telemetry` 每次 500 条），跟踪不重复；`--run` 跟踪"先查状态再拉事件"，运行结束后再拉一次才退出，不漏最后的 `run.finished`。`--follow --json` 输出 NDJSON（唯一的"不是单个 JSON 文档"的情况）。
- 改动：新增 `sdk/neuxmind/models.py`、`sdk/tests/test_models_telemetry.py`；修改 `robot.py`（`models/find_model/supports_model_install/install_model/remove_model/run_events/events_since/latest_seq/tail`、运行时错误码中文提示 `ROBOT_HINTS`、`is_sha256`）、`cloud.py`（`model_install_args`、`entitlement_required`/`no_package` 提示）、`cli.py`（`nx models install/installed/remove`、`nx telemetry tail`、`nx robot status` 沙箱/模型/遥测）、`tests/_support.py`（`RuntimeServer(models=True)`、`PackageServer`、只读树清理）、`pyproject.toml`、README。
- 验证（协议级；仿真四足 `simulated=true`；模型文件是随机字节，不是真模型）：
  - `sdk/tests` 57 个（新增 9 个）在 Python 3.13 与 3.10.20 venv 下各连续 3 次全部通过（约 60 s）；以普通用户 `nxtest`（3.13）跑一次也全部通过。
  - 真实运行时原生 `model.install`（与 `--models-dir` 相同的 `ModelConfig`，http 只对 127.0.0.1 开放）+ 本地 HTTP 包服务（统计下载次数）+ 真实云平台（运营员上架带 `package` 的模型卡、开发者购买授权）：安装、重复安装幂等且不重复下载、SHA-256 不符失败且 `.partial/` 无残留、删除、运行中技能使用时删除 → `model_in_use`（带运行 ID）、未授权购买提示且机器人无变化、无包模型、无 `--models-dir` 的机器人、遥测 tail（翻页、增量、最近 N 条、运行回放、跟踪到运行结束、全局跟踪、NDJSON）、`robot status` 沙箱显示。
  - 变异测试在独立 git worktree 中做：30 个变异 27 个被杀死；3 个存活均为等价变异（seq 连续时"最近 N 条"的二次裁剪冗余；运行时在任何安装请求前就以 404 拒绝无 `--models-dir` 的机器人；云端已把 sha 规范为小写）。首轮发现 `model_in_use` 文案与"包信息缺字段"未被覆盖，已补测试。
  - 其他目录未改动；回归结果见交付报告。
- 变更请求（→ runtime）：
  1. v2 `model.install` invoke 的 `model_id`/`name`（spec 已在 5fed105 加为可选输入）被运行时忽略：`ModelStore.start_action` 没把它们传给 `start_install`，`/v2/models` 的 `name` 为 `null`（只有 `/v1/models/install` 记录）。建议与 v1 一样存为 `name`。
  2. `model.install_*` 事件不带 `run_id`，`/v2/runs/{id}/events` 回放里看不到下载进度/失败原因；建议带上 `run_id`。
  3. 运行时原生 `model.install` 时，manifest 的 `model_runtimes` 仍为空（运行时只写在能力 `params.runtimes`），云端 `compatible_with` 因此把纯端侧包过滤掉；建议同步填 `model_runtimes`，或由总指挥在契约里明确以哪一个为准。
- 遗留：真实 https/签名下载与真实推理运行时未验证；遥测仍是轮询；非 root + 3.10 组合没有单独跑（nxtest 无法访问本会话 scratchpad 里的 3.10 venv），3.10 与非 root 分别验证过。

### 第 03 轮补充 · 采用运行时的目录 ID（runtime 07e0d0c）

- 背景：runtime 07e0d0c 处理了上面 3 条变更请求：`/v2/models` 记录 = `sha256`（安装标识）+ `model_id`（目录 ID，可空）+ `name`；`model.install` 输出与 DELETE 响应里的 `model_id` 仍是 sha；DELETE 接受唯一的目录 ID（多版本 409 `model_ambiguous`）；安装事件带 `run_id`；manifest 顶层 `model_runtimes` 已填。
- SDK 改动：`find_models/find_model` 按 `sha256`/目录 ID/`name` 匹配（目录 ID 可能多个版本）；`nx models installed` 增加目录 ID 与名称两列；`nx models remove <目录 ID>` 直接交给运行时（不再需要登录云端），`model_ambiguous` 给出每个版本的 `nx models remove <sha256>` 命令，旧记录才回退到云端包 sha；安装进度改从该运行的 `model.install_progress` 事件计算（运行记录的 `progress` 在下载期间不更新），CLI 显示"37%（1.1 MiB / 2.9 MiB）"；`ROBOT_HINTS` 增加 `model_ambiguous`。
- 测试：原先按旧语义断言"记录的 `model_id` == sha"的两处改为比较 `sha256`，并断言目录 ID 被记录；新增 2 个测试（无云端配置按目录 ID 删除、同一目录 ID 两个版本 → `model_ambiguous` 且什么都没删、按 sha 删一个后目录 ID 唯一即可删除；`nx models list --robot dog` 列出 onnxruntime 端侧包、不含 tensorrt 包；`PackageServer` 慢速服务 1 MiB 包时 CLI 显示递增进度，`nx telemetry tail --run` 回放出带 `run_id`/`sha256`/目录 ID 的 `model.install_progress`，失败安装的回放里有 `install_failed` 原因）。`sdk/tests` 59 个在 3.13 与 3.10 各连续 3 次全过。
- 新发现（→ runtime，P3）：能力型运行（`model.install`）的运行记录 `progress` 在下载期间不更新（`handle._progress` 没有同步到 run），SDK 已绕开。

## 第 04 轮 · 2026-11-08 · SDK：本体适配 `nx adapter new|test`、`nx robot conformance [--upload]`、按一致性级别选模型（sdk/）

- 目标：契约 §6.3（脚手架、本地一致性测试、机器人一致性测试、Python API、motion 本地强制授权）、§6.4 的 SDK 侧（`--upload`），并去掉 §5.1 的安装进度绕过。
- 关键决策：
  - **全部调用运行时自己的代码**：`nx adapter new` = `neuxmind_runtime.scaffold.generate`（进程内），`nx adapter test` = `conformance.run_adapter`（适配器包进临时 `EdgeRuntime`），`nx robot conformance` = 运行时 `POST /v2/conformance` + 轮询；SDK 不复制任何判定。
  - `--from-manifest <文件|机器人>`：复制能力与限值，实现方式默认 `todo`（`--impl` 覆盖），**不继承 `instance_id`、`simulated=false`**（适配的是另一台真实本体）；`--caps` 时必填限值必须用 `--param` 给（脚手架不猜）。
  - `nx adapter test <包目录>` 的 static 走生成包的 `offline_manifest()`，不碰硬件、不需要 ROS 2；dry/motion 实例化适配器（缺 rclpy → `ros2_unavailable`，必备能力未实现 → `adapter_incomplete`）。
  - **motion 本地强制授权**：`nx adapter test`、`nx robot conformance`、`RobotClient.start_conformance` 未授权时零请求（`adapter test` 连适配器都不加载）。
  - 展示：每级结论、每项**已声明**能力的 static/dry/motion（通过/失败/跳过（不适用/受阻）及失败原因）、未声明能力列表、测试前已锁存/中止/结束时仍锁存的提示，每次都写"不等于现场验收证据（field_acceptance、evidence_kind）"。退出码按所请求级别；`--json` 输出原始 `conformance.report.v1`（`--upload` 时 `{report, upload}`）。
  - `--upload` 先用**当前 manifest** 登记/刷新云端设备群记录再上传（云端要求报告与登记 manifest 一致），并记下 `cloud_id`；`nx models list --robot R --require-level dry` 同样先刷新再查询。
  - 安装进度：只读 `/v2/runs/{id}` 的 `progress` 与 `progress_detail.feedback.{bytes,total}`（运行时第 04 轮修复），完成前封顶 0.99；运行记录没有 `progress_detail`（旧运行时）才回退到 `model.install_progress` 事件。
- 改动：新增 `neuxmind/adapters.py`、`tests/test_adapters_conformance.py`（21）、`tests/test_e2e_adapter.py`（1）；`robot.py`（`start_conformance/conformance/conformance_runs/wait_conformance/run_conformance`、`conformance_unsupported`、新提示）、`cloud.py`（`compatible_models(robot_id, require_level)`、`upload_conformance`、`check_skill`、新提示）、`models.py`（`install_progress`）、`cli.py`（`nx adapter new|test`、`nx robot conformance`、`models list --require-level`）、README §3/§5.1/§5.2/§5.5/§5.7/§8/§9。
- 验证（仿真本体与 rclpy 测试替身；不是真机、不是 ROS 2）：
  - `sdk/tests` 81 个在 Python 3.13 与 3.10.20（scratchpad 内 venv，SQLAlchemy 2.0.57）全过。
  - 端到端：`nx adapter new`（ROS 2 映射）→ `nx adapter test`（static；替身上 dry）→ 独立进程 `neuxmind-runtime serve` 加载生成的适配器 → `nx robot conformance --level motion --authorize --upload` → 云端摘要 → `nx models list --require-level dry`；另用 `nx sim up` 的仿真四足手工演示 dry/motion 上传与过滤。
  - worktree 变异 38 个（SDK 19 个）：SDK 首轮 3 个存活——"首次上传不登记"与"未声明能力入表"补测试后被杀死；CLI 的 motion 本地检查与 `RobotClient` 的检查重复（等价，保留以给出更明确的提示）。
- 变更请求：见交付报告（报告带 `manifest_digest`/签名、`model.install`/`legged.posture` 的 dry 用例、仿真机械臂倍速下夹爪用例、契约补录新端点）。
- 遗留：真机 `evidence_kind: conformance` 报告与真实 ROS 2 未验证；`nx skill check` 未接 `--require-level`（云端 `check_skill` 已有 Python API）。

## 第 04 轮审查修复 · 2026-11-09 · SDK：脚手架输入纵深防御（Q4-09）、旧报告不上传到新 manifest（Q4-12）、信任边界与 motion 幅度说明（Q4-13/18/19）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-review.md`。
- Q4-09（P1，SDK 部分）：`nx adapter new` 在调用运行时脚手架**之前**校验所有会写进生成代码的文本（`adapters.check_scaffold_input`）：厂商/型号单行 ≤100 字、只含字母数字汉字空格与 `_ . + , ( ) / : -`；`instance_id` 按 ID 规则；参数中的文本只含字母数字与 `_ . / : -`；`display_name`/`adapter_version`/`class_name`/`ros2` 一律不传。`--from-manifest <robot>` 的可疑值 → `unsafe_scaffold_input`（注明"来自机器人 X，可能被篡改"），不写任何文件；合法值也提示"来自机器人，请核对"。脚手架中途抛任何异常（如生成代码 `SyntaxError`）→ `scaffold_failed`，并删除本次新建的输出目录（用户已有的目录保留）。回归测试用审查 payload（docstring 逃逸 + 写标记文件），覆盖机器人 manifest 的 vendor/model、参数文本、manifest 文件、命令行参数，断言无目录、无标记文件。
- Q4-12（SDK 部分）：`nx robot conformance --upload`（含 `--id <旧测试>`）在报告 `manifest_digest` 与机器人当前 manifest 不同时本地拒绝 `conformance_manifest_mismatch`，不发云端请求；`adapters.manifest_digest()` 与运行时/云端算法相同。
- Q4-13/Q4-18：README 与 `nx adapter test --help` 写明：导入适配器即以当前用户权限执行其全部代码（static 也一样），网络与文件不隔离；报告由同进程产生、可被适配器篡改；云端摘要只供参考。
- Q4-19：motion 提示（`nx adapter test`、`nx robot conformance`）如实说明中断/停止用例的较大目标与中断失效时会走满。
- Q4-08（文档）：README 中"`legged.posture`、`model.install` 在 dry 不适用"改为 b0e53c7 后的实际语义。
- 验证：见交付报告。
