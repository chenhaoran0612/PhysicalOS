## 第 01 轮修复 · 2026-11-04 · 桌面端拒绝仿真证据，增加“解除急停”（brainos/、web/）

- 目标：修复审查 R-08（仿真结果被记为现场证据）和 R-07（`/v1/stop` 锁存后桌面端没有解锁入口），对齐 INTERFACES §1.1 第 01 轮修复条款。
- 关键决策：
  - `validate_manifest` 保留 v1 清单的 `simulated`（须为布尔值）、`vendor`、`model`，仅在设备声明时保留，旧清单摘要不变；其他未知字段仍丢弃。`simulated` 参与 manifest 摘要，仿真体与真机的证据因此不会互通。
  - 现场证据采用“失败即关闭”：本体声明仿真、设备返回 `simulated=true`、`evidence_kind != "device_run"`（含缺失）、证据引用以 `simulation://` 开头，任一成立即存为 `scope:"simulation"`，`passed` 原样保留并附判定依据。deploy、start 和 readiness 只接受 `scope=="onsite"` 且非仿真的报告；旧版本以 onsite 入库、但详情显示为仿真的记录也一并拒绝。
  - 解除急停 `reset_robot`：需要 `hardware_authorized=true`（界面上是现场确认复选框）；先刷新设备身份，再调用 `/v1/reset`。`request_id` 幂等，同一 id 返回已记录的结果，不重新发送；结果不明记为 unknown，不重试；写入审计 `robot.reset`。
  - 急停状态的判断：以设备 `/v1/status.estop_latched` 和 `/v1/validate` 的“急停”检查项为准；设备没有报告时，最近一次操作是 stop 就提示可能已锁存。设备报告锁存时，启动在本地拒绝，不发往设备。stop 不受任何影响。
- 分工：桌面集成 Agent → `brainos/contracts.py`、`delivery.py`、`service.py`、`server.py`（`/api/robots/reset`）、`edge.py`（参考网关转发 `/v1/reset`）、`web/app.js`、`delivery.js`、`engineering.js`，`tests/test_sim_evidence_and_reset.py`、`tests/sim_reset_browser{.cjs,_fixture.py}`，`docs/USER_EXPERIENCE.md`。
- 改动：后端新增仿真证据判定、急停状态与 reset 操作；前端在机器人卡片、交付清单、报告列表和请求记录中标注“仿真”，显示“急停已锁存”和“解除急停”确认流程。测试替身 `DeviceFixture.evaluate` 补充声明 `evidence_kind:"device_run"`（原有断言不变）。
- 验证：
  - **协议替身通过**：根目录 `python3 -m unittest discover -s tests` 共 82 个测试，全部通过（20 个跳过，与修复前相同；新增 10 个）。
  - **运行时仿真本体通过**：通过真实 HTTP 驱动 `runtime/` 的 SimQuadruped，现场测试被存为 simulation，deploy 被拒绝。
  - **浏览器验收（协议替身 + Chromium）**：`tests/sim_reset_browser.cjs` 覆盖仿真标记、部署被拒提示、急停锁存显示、未勾选确认时不发送请求、解除后再次启动，JS 异常为 0。原 `delivery_browser.cjs` 和 `journey_browser.cjs` 仍通过。
  - **未验证**：真机；runtime 侧 `/v1/reset` 由 runtime 修复 Agent 实现，本轮的 reset 只在 brainos.edge 协议替身上测过。
- 遗留：→ 下一轮候选
  - 旧适配器未返回 `evidence_kind` 的结果现在记为仿真，需要通知适配器开发者补上该字段（或由总指挥决定是否放宽）。
  - `runtime/tests/test_v1_compat.py::BrainosPlatformTests` 仍断言“仿真报告 → 部署成功”，与本修复冲突，需由 runtime 修复 Agent 改为断言拒绝。
  - runtime `/v1/reset` 落地后，补一条桌面端对真实运行时的 stop → reset → start 集成测试。
  - `/v1/evaluate` 同步执行、8 s 超时的问题仍在（第 02 轮改为异步）。

## 第 02 轮 · 2026-11-08 · 桌面端接入新三层（§4.5）与异步现场测试客户端（§4.1）（brainos/、web/、tests/）

- 目标：在桌面控制台里看到标准能力表、从本地文件夹安装技能包、接入云平台账户；现场测试改为异步并可随时停止；急停锁存时禁用“准备启动工作”（第 01 轮 P3）。
- 关键决策：
  - 能力表优先读 `/v2/manifest`（spec `validate_manifest_v2` 校验，且 `instance_id` 必须与已连接的 v1 身份一致），失败回退 v1 能力名称；名称、类型、安全级只用本机 spec 注册表；不在标准中的名称如实标出，`x.*` 按运动类显示。只发 GET，不改 manifest 摘要（v2 清单另存 `manifest_v2`，不参与证据摘要）。
  - 技能包读取：所选文件夹本身及其内部的目录与文件都以父目录 fd + `O_NOFOLLOW` 打开并拒绝符号链接（审查 Q2-07 指出路径中间各级当时仍会被跟随，已在审查修复中改为逐级拒绝）；不读隐藏目录中的路径；跳过 tests/、`__pycache__`、隐藏文件和字节码；文件数 200、单文件 512 KB、总量 1.5 MB、条目 2000、层级 8；只收 UTF-8 文本。限值与 SDK/运行时一致。
  - 兼容性唯一依据 `spec.check_compatibility`；桌面端只补人话说明（缺少能力带中文名，参数不满足带要求值、实际值和单位），说明条数少于 spec 判定时补上 spec 原文，不隐藏任何判定。
  - 安装两步确认：预检返回内容摘要；安装时重新读取文件夹、摘要必须一致、对实时 `/v2/manifest` 再判一次兼容，不兼容在本地拒绝，不发往设备。`request_id` 由确认页生成，同 id 返回已记录结果不重发；设备 4xx 记为 rejected，未发出记为 failed，超时/5xx/`outcome_unknown` 记为 unknown 不重试。
  - 云平台：地址 https，回环地址允许 http；不跟随重定向，https 校验证书；会话令牌只在 `<workspace>/credentials/cloud-session.json`（目录 700、文件 600、原子写），并绑定签发它的地址；快照只含账户摘要。401 时删除本地会话。dev 验证令牌只在云平台标注 `simulated` 时转给页面，不落盘。工程助手不切到云网关。
  - 异步现场测试：`/v1/evaluate` 带 `async:true`；有 `evaluation_id` 记为 running 并轮询 `GET /v1/evaluations/{id}`，无则按旧同步结果处理。结果写入复用同一函数（摘要匹配、ADR-011 仿真判定不变）。轮询读失败保持 running 并显示原因；unknown 只记录不重试；已结束的记录不再访问设备；重启后 running 仍可继续轮询。running 计入“未决现场测试”，阻止用旧报告发布/启动。
  - 新增机器人级停止 `robots/stop`（`/v1/stop`）：不先刷新身份、不做任何前置检查，结果不明提示现场确认与硬件急停；记为 stop 操作，参与急停锁存推断。
  - readiness 新增 `onsite_running`、`next_step:"testing"`、`can_stop_robot`、`start_blocked`；`can_start` 语义不变（仍表示已发布），锁存只通过 `start_blocked` 禁用启动，后端 `operate(start)` 原有锁存拒绝不变。
- 改动：后端新增 `robot_caps.py`、`skill_packages.py`、`cloud_account.py`、`evaluations.py`、`spec_bridge.py`，`connector.call`（保留 JSON 错误体、区分未发出）；`service.py`（异步现场测试、`stop_robot`、云账户摘要进快照）、`delivery.py`、`server.py`（13 个新接口与 3 个前端文件）。前端新增 `robot_detail.js`、`cloud.js`、`onsite.js`，修改 `app.js`、`delivery.js`、`engineering.js`（机器人卡片“能力与技能”入口）、`index.html`、`style.css`。
- 验证：
  - 根目录 `python3 -m unittest discover -s tests`：113 个测试通过，20 个跳过（与基线相同；新增 31 个。原写“22 个跳过”有误，已在审查修复中更正：2 个真实 runtime §4.1 测试在交付时已能对 runtime 工作区运行）。
  - 新测试使用真实 runtime 仿真四足（能力表、技能包兼容/不兼容/参数不满足/设备拒绝）、真实云平台应用（注册、dev 令牌验证、钱包、令牌文件权限与不外泄、会话失效、按 manifest 过滤模型）、§4.1 协议替身 `AsyncDevice`（进行中、完成、停止、unknown 不重试、轮询失败、重启后继续、仿真结果、旧设备同步回退、锁存禁用启动）。
  - 真实 runtime §4.1：用 runtime Agent 尚未提交的工作区实现跑通两项（完成、运行中停止→未通过）；提交后需再跑一次。
  - 浏览器验收 `tests/desktop02_browser{.cjs,_fixture.py}`（真实 runtime + 真实云平台 + 协议替身）：能力表、技能包兼容与不兼容、云端注册/验证/钱包/模型、现场测试进行中与停止、锁存时启动禁用，0 JS 异常；900×640 与 1440×930 截图已逐张检查。原 `sim_reset_browser`、`delivery_browser`、`journey_browser` 仍通过。
  - **未验证**：真机；macOS 打包（PyInstaller 需能找到 `neuxmind_spec`）；真实 SMTP 环境下的验证邮件。
- 遗留：→ 下一轮候选
  - 原生文件夹选择器：当前需要粘贴完整路径；需 `desktop/main.swift` 提供选择面板并把路径交给页面。
  - 安装后的技能还不能一键登记为平台“技能”供应用编排（需手工“注册技能”，entrypoint 填技能 ID）。
  - 云平台：邮箱认领（`/auth/claim`）、找回密码、购买授权/点数、组织切换未接入；工程助手切换云网关留到第 03 轮。
  - `engineering_browser.cjs` / `discovery_browser.cjs` 在本环境因“工程引擎未包含在当前安装版本中”无法运行（环境未准备 agent runtime），与本轮改动无关。

## 第 02 轮审查修复 · 2026-11-08 · 技能包读取与云平台地址（brainos/、web/、tests/）

- 依据：`docs/evolution/reviews/2026-11-08-iter02-review.md` 中的 D1/D2（变异存活）、Q2-07、Q2-10、Q2-05（桌面端一侧）与“文档不一致”第 4 条。
- 技能包读取（Q2-07、D1/D2）：
  - 所选路径从根目录起逐级 `fstatat(AT_SYMLINK_NOFOLLOW)` + `openat(O_NOFOLLOW)` 打开，**路径上任何一级**是符号链接都拒绝，并指出是哪一级（macOS 的 `/tmp`、`/var` 本身是链接，需填写 `/private/...` 实际路径）。
  - 每个文件（含 `skill.json`）先 `lstat` 再 `O_NOFOLLOW|O_NONBLOCK` 打开、再 `fstat`：符号链接、FIFO、设备等非普通文件一律拒绝，打开 FIFO 也不会阻塞。
  - **硬链接（`st_nlink > 1`）一律拒绝**，避免把包外文件（例如家目录里的凭据）带进技能包；提示复制成独立文件。
  - 新测试：`skill.json` 是指向合法内容的符号链接、FIFO、字符设备（root 下运行）、硬链接；包内 FIFO、硬链接；祖先目录是符号链接；并直接测试单文件读取层。变异复验（scratchpad 副本）：去掉符号链接检查与 `O_NOFOLLOW`、去掉两处普通文件检查、去掉硬链接检查、去掉祖先链接检查，均被测试杀死。只去掉 `O_NOFOLLOW` 而保留 `lstat` 检查的变异仍存活：两层检查之间只差竞态窗口，测试无法稳定复现，按纵深防御保留。
- 云平台地址（Q2-10）：
  - http 只接受字面回环 IP（127.0.0.0/8、`::1`）；`localhost` 保存时改写为 `127.0.0.1`，明文令牌的去向不再由名字解析决定。
  - https 的地址策略：公网与回环允许；私网（10/8、172.16/12、192.168/16、fc00::/7）、共享地址（100.64/10）和链路本地（169.254/16、fe80::/10）**默认拒绝**，本机设置环境变量 `NEUXMIND_CLOUD_ALLOW_PRIVATE=1` 才允许；云元数据地址（169.254.169.254、fd00:ec2::254、100.100.100.200）、未指定、组播、保留地址始终拒绝；IPv4 映射的 IPv6 地址按 IPv4 判定。私网放行不会放行 http。
  - 防 DNS 重绑定：字面 IP 在保存时校验；主机名在**每次连接时**解析，校验全部候选地址，并连接到被校验的那个地址（自定义 `HTTPConnection`/`HTTPSConnection.connect`，TLS 仍按主机名做 SNI 与证书校验）。
  - 代价：云平台请求不再走系统 HTTP 代理（代理会让“实际连接的地址”无法校验）；需要代理的网络留作遗留。
  - 新测试：localhost 改写、私网/共享/链路本地拒绝、元数据始终拒绝（含开启私网后）、开启私网后允许 https 但仍拒绝 http、主机名解析到私网时在连接前拒绝且没有发起连接。
- 现场测试（Q2-05 桌面端一侧）：设备返回契约三态之外的状态（如内部 `rejected`）时，记为“结果未知”并写明收到的状态，不再无限显示“进行中”，也不重试。新测试覆盖。
- 文档：本文件第 02 轮条目的“22 个跳过”更正为 20，符号链接一句改为与实现一致；`docs/USER_EXPERIENCE.md` 补充路径与云地址规则。
- 验证：根目录 `python3 -m unittest discover -s tests`：123 个测试通过，20 个跳过（新增 10 个）；`tests/desktop02_browser.cjs` 仍通过，0 JS 异常。
- 遗留：云平台请求的系统代理支持（需在代理场景下另行确定地址校验方式）；硬链接拒绝对确有硬链接需求的仓库（如 git worktree 硬链接对象）可能过严，需要时再加“复制后再选”的引导。

## 第 03 轮 · 2026-11-08 · 工程助手切到云网关（契约 §5.3）（brainos/、web/、tests/）

- 分工与过程：第 03 轮桌面集成 Agent 实现后在浏览器验收阶段卡住超过 3 小时被总指挥停止；接手的 Agent 通读未提交工作区、修复 3 个问题、完成验收与文档并提交。
- 实现（上一个 Agent）：
  - `cloud_account.py`：`gateway_models`、`open_gateway_session`（令牌只在返回值里）、`close_gateway_session`（幂等，`session_busy` 重试 ≤20 s）、`gateway_session`、`session_base_url`（由本机配置的云地址拼出，会话 ID 严格校验）；`gateway_enabled` = 已登录；网关错误码的中文提示。
  - `engineering.py`：模型 `billing=cloud`（`use_cloud_model`）；`_cloud_ready` 启动前检查（登录、云地址一致、邮箱、预算 ≥80000）；`_model` 开云端会话并把 base_url/令牌交给运行时，**不调用本地 commerce**；`_run` 的 `finally` 关闭云端会话并记录云端计量（`cloud_session`、`cloud_runs`、`metering_status` = `cloud_open`/`cloud_settled`/`cloud_reconciliation_required`/`cloud_close_failed`/`cloud_close_pending`）；重启时遗留会话标为待关闭，`engineering/cloud/close` 手动关闭；网关错误映射为用户能懂的说明。
  - `workflows.py`：`cloud_reconciliation_required` 视同费用待核对。`server.py`：新路由 `engineering/models/cloud`、`engineering/cloud/close`、`cloud/gateway-models`。
  - `web/engineering.js`：模型选择对话框（登录/未登录两种提示）、云模型列表（计价、未授权）、云模型预算说明与最低值、会话详情“云端计量”块与“关闭云端会话并获取计量”按钮。
  - 测试：`tests/test_cloud_engineering.py`（本机真实 cloud 应用 + 回放录制的 Codex 请求；另有真实 Codex 组）、`tests/cloud_fixture.py`、`tests/agent_binary.py`（`NEUXMIND_TEST_AGENT_BINARY`）、浏览器 `tests/cloud03_browser*.{py,cjs}`。
- 接手后的修复：
  1. **关闭失败后继续会话会丢失旧云端会话**：`cloud_session` 被新会话覆盖，旧会话只能等云端到期关闭、本地无记录。现在关闭失败也写入 `cloud_runs`（`close_failed`），且开新会话前先重试关闭上一个。新测试 `test_failed_close_is_retried_before_resume_and_never_lost`（变异：去掉重试关闭 → 失败）。
  2. **真实 Codex 下超额结论被掩盖**（浏览器验收第 1 次发现）：超额替身报告的用量先触发 Token 观察限额，桌面端发 `turn/interrupt` 时轮次已结束，运行时返回 “no active turn to interrupt” 被当作失败，`not failure` 条件使云端超额结论被跳过，用户只看到一条引擎报错。现在 interrupt 对已结束轮次容错；云端 `overage_calls>0` 时以云端结论为准（运行时错误附后）。新测试 `test_overage_from_the_real_runtime_is_explained`（真实 Codex；变异：恢复旧逻辑 → 失败）。
  3. 云模型达到观察限额的提示原为“自有模型以供应商账单为准”，改为“费用以云平台计量为准”。
- 验证：
  - 根目录 `tests/`（Python 3.13）：设置 `NEUXMIND_TEST_AGENT_BINARY`（真实 Codex 0.146.1）143 通过、0 跳过；不设置 143 通过、24 跳过。
  - Python 3.10（`/opt/nx-py310` 新建 venv，装 `requirements-runtime.txt`）：138 通过、21 跳过（跳过的是需要 cloud 依赖的测试——cloud 依赖 SQLAlchemy 2.1 要求 ≥3.11，桌面端在 3.10 上只作为云平台的客户端）。三套并行高负载时 `test_workflows.test_stop_interrupts_stage_without_launching_next` 曾超时一次（25 s 等待），单独与整套重跑均通过。
  - cloud/tests SQLite 186、PostgreSQL 16 186；sdk/tests 59；runtime/tests 227（抽查全量）。
  - 浏览器 `tests/cloud03_browser.cjs`（真实 Codex 0.146.1 + 真实 cloud 应用）：第 1 次在超额步骤失败（即修复 2），修复后第 2 次通过，0 JS 异常，14 张截图（1440×930 与 900×640）已逐张查看；本机账本 0 条、0 预留，云端钱包扣除 2032 点与各会话记录之和一致，云端预留归零。
- 遗留：
  - 超额运行在真实运行时下状态为“已停止”（观察限额先触发），在回放运行时下为“失败”，提示文字一致；是否统一为一种状态待定。
  - 只对 Codex 0.146.1 录制并验证；升级运行时需重录 `cloud/tests/fixtures/` 并重跑。
  - 云端会话最低预算 80000 来自当前 Codex 请求体大小（约 5 万 Token 输入上界），工具与说明变多时需要上调。
  - `engineering_browser.cjs` / `discovery_browser.cjs` 等旧浏览器脚本本轮未重跑。

## 第 03 轮审查修复 · 2026-11-08 · 引擎落盘与网络（Q3-03、Q3-04）、长会话 413（Q3-07 桌面侧）、工作流费用待确认（Q3-16）

- 依据：`docs/evolution/reviews/2026-11-08-iter03-review.md`。
- Q3-03（令牌写入 shell 快照）：用 Codex 0.146.1 的 `codex features list` 确认开关名，在生成的 `config.toml` 中设 `shell_snapshot = false`；`AgentProcess.close()` 之后再删除 `runtime-home` 下的 `shell_snapshots/`、`.tmp/`、`plugins/`（第二道防线）；`runtime-home` 仍为 0700。密钥仍经 app-server 进程环境传入（`env_key`），子 shell 用 `inherit = "none"` 不继承（审查已核实）。
- Q3-04（插件市场克隆）：`plugins`、`remote_plugin`、`plugin_sharing`、`in_app_updates`、`skill_mcp_dependency_install` 设为 false，`check_for_update_on_startup = false`。实测 app-server 空闲 8 s：修复前 `runtime-home` 86 042 356 字节且有 `.tmp/plugins` 完整克隆；修复后 1 924 594 字节、无 `.tmp`。
- Q3-07（桌面侧）：`payload_too_large` 映射为"云端会话历史过长……请新建工程任务（新会话）继续"，未计费。
- Q3-16：工作流把 `cloud_close_failed` / `cloud_close_pending` 视为"费用待确认"阻塞阶段；补变异 B4 的测试（四种未结算计量状态都阻塞）。
- 新测试（真实 Codex，`NEUXMIND_TEST_AGENT_BINARY`）：`test_session_token_is_never_written_to_disk_by_the_real_runtime`、`test_own_provider_key_is_never_written_to_disk_by_the_real_runtime`（全盘扫描零命中；自有 Key 只在 0600 存储中）、`test_runtime_home_stays_small_and_no_plugin_marketplace_is_cloned`、`test_idle_app_server_does_not_sync_plugins`、`RealRuntimeOversizedHistoryTest.test_payload_too_large_is_explained`（云端会话上限设为 20 kB）；`tests/test_workflows.py::test_unsettled_model_costs_block_the_stage`。`tests/cloud_fixture.CloudServer` 可传入云端设置。
- 变异复验：开快照但保留清理 → 令牌测试仍通过（清理生效），空闲测试失败；开快照且去掉清理 → 3 项失败；开插件 → 空闲测试失败；工作流去掉 `cloud_reconciliation_required` → 失败。
- 验证：`tests/` 3.13（真实 Codex）149 通过、0 跳过；3.10 venv 144 通过、26 跳过（cloud 依赖需 ≥3.11）；cloud/tests SQLite 192、PostgreSQL 192；sdk/tests 59；浏览器 `cloud03_browser.cjs` 复验通过、0 JS 异常。
- 文档更正：本文件第 03 轮条目及 `tests/test_cloud_engineering.py` 中"令牌永不落盘"此前只在替身下成立，现由真实 Codex 测试覆盖。
- 遗留：G-2（`test_workflows` 高负载超时）未处理——`thread/start`/`turn/start`/`turn/interrupt` 同步 RPC 期间看不到停止事件，停止延迟无产品侧上界（P3，第 04 轮带插桩复现）；Codex 其他本地状态（`state_5.sqlite` 等约 2 MB）为继续会话保留，未清理。

## 第 04 轮 · 2026-11-08 · 本体适配工作流（契约 §6.2）与停止上界（G-2）（brainos/、web/、tests/）

- 目标：用户从本体档案一键启动“本体适配”——AI 依据档案与能力标准写映射表、运行时脚手架生成适配器并由工程助手实现、产品运行一致性测试 static+dry、产出适配报告；每阶段沿用验收门。处理第 03 轮遗留 G-2（停止无产品侧上界）。
- 关键决策：
  - 模板建在现有工作流上：阶段 1、2 是工程会话；阶段 3、4 是新的**产品执行阶段**（`kind=product`，不调用模型、0 Token），与会话阶段共用尝试次数、SHA-256 交付快照、验收、停止与重试。用户自建流程不能声明产品阶段。
  - 证据规则由产品执行，不靠模型自觉：映射表必须覆盖注册表全部能力，证据必须属于固定的那一版档案，**无证据的行改为“需确认”**并保留 Agent 原写。
  - 生成代码只在独立子进程里运行（运行时 CLI），最小环境、进程组、资源上限、墙钟时限与停止；从不导入桌面进程。网络未隔离，写入报告风险。
  - motion 级在代码层面不可达（`LEVELS=('static','dry')`），界面与报告处处标注“一致性报告不等于现场验收证据”“motion 需现场确认并显式授权、只对真机执行，本轮不自动执行”。
  - 运行时的 scaffold/conformance 与本轮并行开发：桌面端按 CLI 形状编程，测试用显式标注的 `tests/runtime_double.py`；报告解析对列表/映射两种布局宽容。
  - G-2：同步 RPC 等待可被停止取消；interrupt 无答复即杀进程组；`stop()` 的看门狗 10 s 后强制终止、再 3 s 释放并标记 `stop_forced`。
- 改动：
  - `brainos/adaptation.py`（新）：模板定义、上下文 `ADAPTATION.json`（档案、证据摘录、注册表、映射契约）、映射校验、脚手架、一致性运行与摘要、适配报告、`run_tool`。
  - `brainos/workflows.py`：`create(..., template=)`、产品阶段 `_run_product`、模板校验钩子、脚手架在锁外执行、`conformance_reports` 进快照、验收/退回写回报告；`ADAPTATION.json` 为保留名。`brainos/engineering.py`：停止上界（看门狗、`_force_stop`、`stop_forced`、`stop_latency_ms`、原子的最终状态）；`brainos/agent_runtime.py`：可取消的 `request`、`StopRequested`、关闭时 SIGCONT。`brainos/server.py`：`engineering/adaptation/start`、`/adaptation.js`。
  - `web/adaptation.js`（新）：启动表单、映射表、一致性摘要、报告卡片；`web/workflows.js`（模板阶段内容、产品检查标签、模板流程宽对话框）、`web/inventory.js`（档案页入口与适配状态）、`web/robot_detail.js`（摘要与能力表并列）、`web/engineering.js`（有档案的本体也能进详情）、`web/style.css`。
  - 测试：`tests/test_adaptation.py`（14 项，其中 5 项真实 Codex）、`tests/test_stop_bound.py`（4 项真实 Codex）、`tests/adaptation_support.py`、`tests/runtime_double.py`、浏览器 `tests/adaptation_browser_fixture.py` + `tests/adaptation_browser.cjs`。
- 验证：见交付报告（3.13 含真实 Codex、3.10 venv、变异、浏览器）。
- 遗留：
  - 真实运行时 scaffold/conformance 提交后需以 `NEUXMIND_TEST_REAL_RUNTIME_TOOLS=1` 复验，CLI 参数与报告字段可能需要对齐（见契约变更请求）。
  - 浏览器验收第 2 次在“本体详情并列显示”一步失败（SSH 本体卡片无详情入口，已修复并有接口测试），按第 9 条未跑第 3 次，该界面待复验。
  - 一致性测试子进程未做网络隔离；适配器替身后端开关 `NEUXMIND_ADAPTER_BACKEND=double` 需要运行时脚手架约定。
  - 适配报告由产品按规则生成，不含 AI 的定性分析；映射表的 `params` 未按 spec 字段逐项校验（交给脚手架/一致性 static）。

## 第 04 轮补充 · 2026-11-08 · 本体适配工作流对齐运行时一致性测试与脚手架（brainos/、web/、tests/）

- 背景：第一版（d8c0157）按运行时进行中的 CLI 形状编程、默认用替身测试；上一个桌面 Agent 在对齐最终接口时停止，本条由接手者完成。
- 关键决策：
  - 阶段 2、3 默认调用真实 `neuxmind-runtime scaffold|conformance`（运行时 09c1297）；`tests/runtime_double.py` 降为“运行时无法运行时”的后备，并改成与真实 CLI、退出码、报告字段一致，由同一严格解析器检查。
  - 退出码按契约区分：0 通过、1 未通过（有报告，进入验收并写入风险）、2 被拒绝/无法加载（无报告，阶段停下并显示运行时原因）；报告的 `<level>_ok` 必须与退出码一致。
  - 报告解析从“宽容猜测”改为严格：只认 conformance.report.v1 的真实字段；`field_acceptance` 非 false 或 `evidence_kind` 为 `device_run` 时拒绝（与云平台规则一致）。
  - 摘要字段与报告同名：`levels.<级别>` 计数与 blocked 原因、`skip_kind`、`simulated`、`evidence_kind`、`field_acceptance:false`、`disclaimer`、`conformance_id`、版本与 `manifest_digest`（有则显示）；`verdict` 只是界面的一词结论。
  - 阶段 3 的 ROS 2 后端是运行时的 rclpy 测试替身（遮蔽已安装 rclpy，不会连到真实 ROS 2 图）；报告因此为 `simulated:true`、`evidence_kind:simulation`。
  - 脚手架实现提示按运行时 `HINT_KINDS` 转换（新增 `serial:/dev/…`、`http:url`），本体形态不在 spec `FAMILIES` 时用 `custom`；`adapter.json` 视为脚手架计划，实际声明以 `IMPLEMENTED` 与报告为准。
- 改动：`brainos/adaptation.py`（`parse_report`、`conformance_env`、`run_conformance`、`summarize`、`scaffold_hint`、`scaffold`、`validate_adapter`、阶段 2 目标、阶段 4 使用 `verdict`）；`web/adaptation.js`（级别计数、报告字段、skip_kind、脚手架实现/待实现与必备能力提示）、`web/inventory.js`、`web/style.css`；`tests/test_adaptation.py`（20 项）、`tests/adaptation_support.py`、`tests/runtime_double.py`、`tests/adaptation_browser_fixture.py`、`tests/adaptation_browser.cjs`；文档 ENGINEERING_ENGINE、USER_EXPERIENCE。
- 验证：3.13 全量 173 项（真实 Codex）172 通过 0 跳过、1 项失败为 `test_cloud_engineering.test_session_busy_is_explained`（见遗留）；3.10 venv（真实 Codex）168 项 142 通过 26 跳过（cloud 依赖需 ≥3.11），其中 test_adaptation 20 项全部运行；替身后备模式 test_adaptation 20 项（5 项跳过真实运行时用例）通过；浏览器验收一次通过、截图已逐张查看。
- 遗留：
  - `test_cloud_engineering.ProtocolLevelTest.test_session_busy_is_explained` 在全量中两次失败、单文件 3 次中 1 次失败：替身里两个“并发”调用未必与云网关处理重叠（f0eacde 之后出现，不在本轮桌面改动范围），需要 cloud/桌面任一方把替身改成确定性重叠。
  - 阶段 3 依赖仓库内 `runtime/tests/ros2_double`；打包分发时需要运行时提供正式的“替身后端”入口（契约变更请求）。
  - 一致性测试子进程仍未网络隔离；工程助手写的 SDK/串口代码的 `NEUXMIND_ADAPTER_BACKEND=double` 是桌面约定，脚手架模板尚未生成该开关。
  - 运行时正在追加的 `model.install`/`legged.posture` dry 用例与 `manifest_digest` 不改变报告结构；摘要已原样保存 `manifest_digest`（当前运行时工作区已输出该字段，截图可见）。

## 第 04 轮审查修复 · 2026-11-09 · 桌面端（brainos/、web/、tests/）

依据 docs/evolution/reviews/2026-11-09-iter04-review.md。
- Q4-20：`test_session_busy_is_explained` 改为确定性重叠：`UpstreamGate` 把第一次流式调用停在 Echo 上游（`threading.Event`），确认在途后才发第二次，再放行；单测连续 20/20 通过。
- D4（变异存活）：每级运行抽成 `conformance_level`，新增 `ExitCodeAgreementTests`（一致、矛盾的两个方向、exit 2、无报告）；worktree 中去掉检查后测试失败。
- aborted/blocked：`verdict` 增加 `aborted`（中止的级别永不为 passed），解析拒绝非字符串 `aborted` 与"中止却 ok=true"；界面红色"测试中止"+原因+急停仍锁存，`prelatched` 提示；阶段 4 写入风险。
- Q4-13：阶段 2 静态篡改检查（运行时内部模块、动态导入/自省/exec、二进制与遮蔽模块），阶段 3 不复制 `.pyc`；真实 Codex 写入 `from neuxmind_runtime import conformance` 的端到端用例被阶段门阻塞；报告与界面写明"报告由适配器所在进程产生"。
- Q4-14：阶段 3 断网（Linux `unshare -n`/`-r -n`，macOS `sandbox-exec`），摘要 `network_isolated`，不可用时写入风险；注释改为如实描述（约定而非保证、文件系统未隔离）；阶段 2 卡片提示"验收后将在本机运行这些代码"。
- 变异（worktree）：退出码一致性、aborted 当通过、aborted+ok 被接受、篡改检查关闭、阶段门忽略检查、去掉断网——6 个全部被杀。
- 验证：主工作区的运行时正被运行时 Agent 修改（当时 `conformance.py` 报 `Suite` 无属性 `k`），因此在 git worktree（HEAD 0d765f1 + 本修复）中验收：3.13 真实 Codex 184 项全部通过、0 跳过；3.10 venv 179 项 153 通过 26 跳过（cloud 依赖 ≥3.11）；浏览器验收 2 次运行均通过（第 2 次加入中止摘要的渲染断言）。
- 遗留：中止摘要只做了 DOM 文本断言，截图被对话框遮挡，未目视确认；文件系统隔离仍未做（只断网）；静态篡改检查可被足够隐蔽的代码绕过，长期需把套件与适配器分进两个进程（审查 Q4-13 建议 3，属运行时）；运行时安全规则重写合入后需在主工作区复跑 test_adaptation。


## ux01 · 2026-11-09 · 双击打开就能得到一台会动的机器人（brainos/、web/、desktop/、scripts/build_macos.py、tests/）

- 目标：创始人以真实用户身份试用打包后的 Mac 应用时，必须开终端运行 `scripts/demo.py`、从终端复制十六进制令牌、手工输入技能文件夹绝对路径。本轮让用户在应用内完成：添加仿真机器人 → 看能力 → 从示例库安装技能 → 授权运行 → 看实时状态 → 停止。
- 关键决策：
  - 仿真机器人 = 运行时自己的 `neuxmind_runtime serve`（未改 runtime/），作为桌面后端的**子进程**启动：`--port 0`，从其首行输出读取实际地址；令牌 `secrets.token_urlsafe(32)` 只存在工作空间注册表（快照里不出现）。不在桌面进程内运行，是因为 process 沙箱会把调用进程设为 subreaper / non-dumpable，不应影响桌面后端。
  - 子进程入口 `brainos/sim_child.py`：桌面端保持其 stdin 打开，stdin 关闭（桌面退出、崩溃或被 SIGKILL）即向自己发 SIGTERM——运行时 CLI 把它当作正常关机（停止运动、不留“异常关机”锁存）。桌面后端 `server.py` 新增 SIGTERM → 正常关闭（Swift 壳退出时发 SIGTERM），关闭工作空间时停止所有仿真进程。
  - 持久化：机器人记录 `sim = {kind, adapter, enabled, state}`，运行时状态目录 `<workspace>/sims/<robot id>/runtime`。应用重启时 `enabled` 的仿真机器人在后台自动恢复（新端口，重新连接检查）；已装技能和急停锁存一并保留。用户“关闭仿真”后不随重启恢复；“删除”会删记录和状态目录。
  - 打包后的应用没有 Python：子进程用后端二进制自身 `brainos-server --nx-sim-child serve …`（`desktop/launcher.py` 分发），技能隔离为 `inprocess`（在仿真机器人自己的进程内，与桌面进程隔开）；源码 + Linux 下用 `process`。
  - 示例技能库：`skills/examples` 列出随包的 runtime/examples/skills，并对项目内每台已连接机器人用同一条 `check_compatibility` 给出可用/不可用与原因；预检与安装接受 `example`（服务端解析路径，用户不输入路径）。示例目录只放宽“隐藏目录”一条规则（开发检出可能在 `.claude/…` 下），符号链接、硬链接、大小等检查不变。原路径输入保留为“从文件夹安装（高级）”；Swift 壳提供原生文件夹选择（`WKScriptMessageHandler` + `NSOpenPanel`，只接受本机控制台页面的请求）。
  - 运行技能 `robots/run-skill` → `POST /v2/skills/<id>/run`：必须 `hardware_authorized=true`（页面复选框）；**只对仿真机器人开放**，真机仍走“机器人应用 → 现场测试 → 部署”；急停锁存时本地拒绝；`request_id` 幂等，结果不明不重试。
  - 实时状态 `robots/live`：只读 GET（`/v1/status`、`/v2/state`、`/v2/runs`、最新运行详情），每秒刷新；四足显示俯视图 + 走过的路线 + 坐标/朝向/速度，机械臂显示 6 个关节值与夹爪；“停止”沿用 `robots/stop`（`/v1/stop`，立即发送、无确认、锁存急停），解除急停沿用原有现场确认表单，完成后回到实时状态。
  - 文案：“我的机器人”的两个入口改为“通过 SSH 调查真机”“连接已在运行的机器人”，并与“添加仿真机器人”一起各配一句用户能懂的用途说明；能力卡片先写中文名和一句“这能让机器人做什么”（`robot_caps.USER_DOES`，覆盖能力标准全部 21 项），能力 ID 与规范描述降为次要小字。
  - 云平台页：未设置地址时自动探测（`cloud/probe`，只读、不保存），找到本机云平台则一键使用，否则平实说明“这台电脑上没有运行云平台……不登录也可以使用仿真机器人和技能”；地址输入框预填 `http://127.0.0.1:8800`（与 cloud/、sdk/、demo 一致，可用 `NEUXMIND_DEFAULT_CLOUD_URL` 覆盖）。
  - `scripts/build_macos.py`：PyInstaller 增加 `--paths runtime`、`--collect-submodules brainos/neuxmind_spec/neuxmind_runtime`、示例技能数据 `runtime/examples/skills`；新增 `--check`（任何系统：内容存在、可导入、launcher 分发）与 `--freeze-check`（当前系统用同一组参数冻结后端并冒烟）。版本仍为 0.10.0。
- 验证：
  - `tests/test_ux01_sim.py` 17 项：一键添加/令牌不外露/自动建项目、多台独立端口、关闭后子进程退出且重启恢复技能与急停锁存、关闭后不自动恢复/再启动/删除、**应用被 SIGKILL 后仿真进程自行退出**、启动失败写回机器人、示例库按机器人判定兼容、按示例安装与非法示例拒绝、巡逻运行时位置变化→停止锁存→位置不再变化→未授权不能解除→解除后可再运行、机械臂关节变化、真机拒绝快捷运行、能力白话描述全覆盖、云探测（连不上/连得上/不保存）、打包检查。
  - 浏览器（Chromium 1440×900，`tests/ux01_browser{.cjs,_fixture.py}`，全新工作空间）：注册 → 添加仿真机器人（四足）→ 能力表 → 示例库安装巡逻（挥手显示不适用及原因、按钮禁用）→ 未勾选授权不能运行 → 授权运行 → 位置与路线变化 → 停止（急停锁存、运行技能按钮禁用、位置不再变化）→ 解除急停 → 再添加机械臂、安装挥手、关节值变化 → 两台仿真进程存活 → 云平台页说明连不上且地址已预填；JS 异常 0。第 02 轮 `desktop02_browser.cjs` 回归通过（技能对话框先展开“从文件夹安装”）。
  - Linux 冻结冒烟：PyInstaller 6.22.3（requirements-build 固定版本）按 Mac 构建同一组参数冻结后端，脱离源码树运行：添加仿真四足（`--nx-sim-child`）、示例库 3 项、安装并运行 status_report → succeeded；后端退出后无残留子进程。
  - **未验证**：Swift 部分（`swiftc`/codesign 不能在 Linux 运行），原生文件夹选择器未编译、未实机点击；macOS 上 `inprocess` 技能模式与冻结后端未在 Mac 实测。
- 遗留：
  - 在 Mac 上构建并实测：双击启动 → 添加仿真机器人 → 运行巡逻 → 退出应用后确认无残留 `brainos-server --nx-sim-child` 进程；文件夹选择器。
  - 真机的“直接运行已装技能”未开放（有意），需要产品决定是否在现场测试流程之外提供。
  - 仿真机器人运行在 inprocess 技能模式（Mac）时，第三方从文件夹安装的技能与仿真运行时同进程；仍与桌面进程隔离，但不是 process 沙箱。
  - 示例技能参数用 JSON 文本框编辑；可改为按 skill.json 参数类型生成表单。

## ux04 · 2026-11-09 · 第 06 轮桌面：错误按结构分类、复位卡死的出路、参数越界在字段上拦住（brainos/、web/、tests/）

- 目标：审查 Q5-03/04/05（P2）与 Q5-10/12（P3）。安全拒绝不能再被说成“服务内部出错/网络超时”；复位卡死时操作员拿到真实原因和出路；skill.json 声明的 min/max/choices 在表单与“高级 JSON”两条路上都先拦住；单位只来自 skill.json；首次用户 8 个困惑点。
- 关键决策：
  - **错误先按结构分类**（`web/ux04_errors.js`，纯函数）：① 运行时/云/桌面的 `code`（码表覆盖 runtime、cloud、sdk、brainos 源码里出现的全部错误码，`param_*` 前缀兜底）；② 无码时按设备/云返回的 HTTP 状态（桌面自己的 400 不算）；③ 最后才用窄的文本规则，安全词在前，“HTTP 5xx”只认明确写法，网络超时只认 `timed out`/“未在时限内响应”等，不再命中 `x=500.0`、`512 步`、`timeout_s`。安全/状态/参数类拒绝都带“下一步”，绝不写“稍后重试”。
  - 桌面 API 错误体增加 `code`、`device_status` 与字段信息（`param/min/max/choices…`，`brainos/errors.py`）；HTTP 状态仍为 400（兼容旧调用）。桌面自己的拒绝带码：`authorization_required`、`simulated_only`、`estop_latched`、`simulated_evidence`。`connector.request()` 改走 `call()`：保留设备 JSON 错误体（`code`），超时/连不上分别为 `device_timeout`/`device_unreachable`，409 不再显示“无法连接/请检查地址、凭据与协议”。
  - **复位**：设备带错误码的 4xx（如 409 `reset_in_progress`、`conformance_running`）记为 `rejected`（未执行、急停保持锁存，之后重试有效）；无码 4xx（旧网关的“执行未确认”）仍为 `unknown`。复位超时后读一次 `/v1/status`：`reset_in_progress` 时报“上一次复位一直没有完成”。界面给出结果对话框：原因 + 等待/重启运行时（真机写明重启方式，锁存保留）+ 仿真机器人“重启仿真机器人”（`sims/restart` = 停止并重启子进程，**不解除锁存**）；实时状态也显示“复位还没有完成”。
  - **参数**：表单与高级 JSON 用同一 `uxCheckValue`（类型、min/max、choices、长度）；数字输入不再带浏览器 min/max 属性（原生气泡说不清是哪个参数）；设备拒绝（`param_out_of_range` 等，运行时 v0.12.0 尚不返回，用桩响应验证）按 `param`（或运行时措辞里的参数名）标到对应字段。
  - 单位只取 skill.json `unit`，内置对照只给中文名；未声明单位的数值写“单位未声明”。
  - Q5-12：安装预检用与运行表单相同的参数名/类型/单位/范围；技能说明里的能力 ID 换成中文名（`robot_caps.plain_text`，`summary` 字段）；机器人详情第一屏去掉 /v2/manifest、能力 ID、rate_hz 等（收进“技术细节”）；自动建的试用项目带 `auto_created`，工作台不再算“明确任务 · 已有记录”；未勾授权时表单内中文说明为什么必须勾；操作记录“对象”列写机器人名/应用名；运行编号收进“技术细节”；变灰的“运行技能”旁写原因。
  - `sim_reset`/`desktop02` 浏览器夹具自行设置导入路径（同 ux01 夹具）。
- 验证：`tests/test_ux04.py`（码表 + 连接器 + 复位 409/卡住 + 错误体 + 能力名替换 + 重启仿真保留锁存）；`tests/ux04_errors.cjs` 表驱动；`tests/ux04_browser.cjs`（ux01 夹具 + `NX_FIXTURE_HANG_RESET=1`，测试专用 `tests/ux04_hang_adapter.py` 让仿真四足的 reset() 卡住）；回归 ux01/ux02/ux03/sim_reset/desktop02 浏览器验收与五套全量单测。
- 遗留：运行时按声明检查 min/max 并返回 `param_out_of_range`（运行时 Agent 负责；桌面已按 `param/min/max/choices` 字段对接，字段名以运行时契约为准）；`runtime/README.md` 的操作员恢复流程（运行时 Agent）。

## ux05 · 2026-11-09 · 第 07 轮桌面：选项带名称与类型、参数拒绝各说各的、锁存守护说人话、首次使用引导（brainos/、web/、tests/）

- 目标：审查 Q6-07、Q6-08、Q6-11（P3）；实时状态显示锁存守护（INTERFACES §7–§8 `latched_motion`）与 `safety.stop_not_obeyed`；任务工作台给全新用户一个三步引导。
- 关键决策：
  - **Q6-07**：`robot_caps.parameter_view` 输出 `options: [{value, label}]`（`choices`/`options` 两种写法都归一），`choices` 改为原始值（保留 JSON 类型，不再 `str()`）；对象、列表、非有限数的选项丢弃。表单里凡有选项的参数（含 int/str/enum）都是下拉框，`<option>` 存序号，提交 `options[i].value` 原值；`uxCheckValue` 按 `===` 严格比较（`"2"` ≠ `2`，`true` ≠ `1`），与运行时一致；预检、字段说明、字段旁错误都显示 label。
  - **Q6-08**：`PASS_FIELDS` 改用运行时字段名 `reset_in_progress_s`（去掉 `in_flight_s`），并透传 `cause`、`hint`（400 字）、`problem`、`constraint`（小对象）、`expected`、`step`、`value_length`；复位 409 与“复位卡住”记录都带上这些字段。分类：`param_type` / `param_choice` / `param_length` / `param_required` 各有说法，`param_declaration_invalid` 归为“技能包有误”（不标红字段、提示联系技能作者）；`bad_request + problem: param_unknown` 说成“不认识的参数”；`estop_latched` 按 `cause` 区分 `latch_guard_reset_grace`（守护所致，不是操作有误）与 `stop_during_reset`；复位类附“已经执行了 N 秒”和“机器人的提示”。桌面自身 500 带 `code: desktop_internal_error`，说“操作有没有执行无法确认，先核对机器人状态”，不再说“稍后重试”；`RecursionError` → 400 `payload_too_deep`。
  - **Q6-11**：卡住的复位按请求结果分开写——待核对：标题“解除急停还在等上一次复位完成”、类别“还没有结果”、不出现“拒绝”；409：照旧“机器人拒绝了这次解除急停”。真机卡住给 5 个编号步骤（确认安全/硬件急停 → 找到运行时电脑（显示设备地址的主机名）→ `sudo systemctl restart neuxmind-runtime` → 连接检查确认仍锁存 → 解除急停），真机也提供“停止（取消那次复位）”。实时状态“模式”改为“状态：站立 · 正在前往目标点”等中文（原字串放在提示里）；被停止的运行在解除急停后显示为“上一次技能 … 之前被停止中断，急停已经解除”，不再挂“已收到停止指令”警告；参数类型“结构化数据”改为“多项内容”/“坐标点列表”。
  - **锁存守护**（`sim_robots.latch_view`）：计数是累计的，桌面以“上一次看到未锁存时”的计数为基线，只报本次急停的再停次数；打开时已在锁存中则注明“自机器人运行时启动以来”。白话：“急停期间检测到机器人仍在移动，已再次停止 N 次”“反馈数据异常，按可能在动处理”“读取机器人反馈已卡住 N 秒”“急停后离开停下时的位置约 N 厘米”；无事时“锁存守护正在看着”。**stop_not_obeyed**：只在锁存时读 `/v2/telemetry`（游标来自 `/v1/status.telemetry.last_seq`，首次回看 500 条，之后增量，每次最多 3 页），见到 `safety.stop_not_obeyed` 即在实时状态最上方显示红色“请立即使用硬件急停”；`latched_motion.stop_not_obeyed` 为真也算；之后出现 `safety.reset` 或看到未锁存即清除；读遥测失败不影响实时状态。
  - **首次使用引导**（`web/ux05.js`）：没有真机且未关闭时，在“现在做这一步”上方显示三步；完成与否只看工作空间记录（仿真机器人存在 / 仿真机器人上有成功的 `skill_install` / 有被接受的 `skill_run`），所以从别处完成也会打勾；可关闭（浏览器本地记忆，存储不可用时本次会话内有效）。
- 验证：`tests/test_ux05.py`（选项类型与 label、字段透传、复位字段、桌面 500 码与深嵌套、锁存基线、遥测游标/清除/出错）；`tests/ux04_errors.cjs` 新增参数五类、cause、复位时长/提示、桌面 500 不说重试；`tests/ux05_browser.cjs`（ux01 夹具 + `NX_FIXTURE_HANG_RESET=1`，18 张截图）。
- 遗留：`safety.stop_not_obeyed` 的事件字段以运行时本轮实现为准（桌面只取标量字段，显示 `note`/`message`/`ts`），合并后需用真实事件复验；实时状态里技能名仍是 `patrol@0.1.0` 这类编号。

## ux05-fix · 2026-11-09 · 第 07 轮审查修复（桌面）：真机也有“请立即使用硬件急停”、整数值浮点选项可选、仿真措辞（brainos/、web/、tests/）

- 目标：审查 Q7-03、Q7-04（P2）与 Q7-11（P3）；顺带让 Q7-05 的标定放宽在桌面可见。
- 关键决策：
  - **Q7-03 全局安全监视**（`brainos/safety_watch.py`，`POST /api/robots/safety`）：对项目里每台已连接（或已知锁存）的机器人只读 `GET /v1/status`，锁存时再按 ux05 的游标读 `/v2/telemetry`。限频在服务端：锁存或告警中的机器人每 2 s 最多一次，其余每 10 s（发现别处造成的锁存与标定状态），正在看的那台每 1 s；并行请求、2 s 超时，一台连不上不拖慢其他。告警来源：`safety.stop_not_obeyed` 事件或 `latched_motion.stop_not_obeyed`（取 `stop_not_obeyed_detail`/`_at`）；见到未锁存（含桌面自己复位成功）即清除；机器人读不到时**保留**告警并注明“读不到状态”。仿真机器人关闭后不再询问。
  - **界面**（`web/safety.js`）：顶栏下方常驻红色横幅，逐台写出机器人名、再停次数、离停下位置的距离、机器人报告时间，按钮“查看这台机器人”（仿真：“重启仿真机器人”“关闭仿真”“实时状态”）；任何对话框打开时在对话框顶部重复同样横幅（模态对话框使页面其余部分不可点），当前对话框本身就是那台机器人的实时状态/详情时不重复。机器人列表与卡片加“停止后仍在移动”标记。**真机详情页**新增“急停与锁存守护”：锁存状态与原因、告警、锁存守护白话（与仿真实时状态同一套）、解除急停按钮；未锁存时说明守护已就绪。浏览器每 2–3 s 轮询一次，同一时刻只有一个请求。
  - **Q7-05**：运行时报告 `latched_motion.relaxed`（或 `latch_guard_relaxed`、`status.safety.latch_guard_relaxed`、遥测 `safety.latch_guard_relaxed`）时，详情页显示琥珀色“锁存守护的标定被放宽了”（哪些项、数值、来源：机器人清单/运行时配置/适配器）并在列表加“锁存守护已放宽”；运行时未给该标志时，非默认来源的标定也以一行“锁存守护按现场标定运行：位置（pos_m = 0.2，来源：机器人清单）”显示。
  - **Q7-04 浮点选项**：浏览器无法区分 `1.0` 与 `1`，所以在桌面后端发给设备前按已安装技能的声明恢复类型（`robot_caps.typed_parameters`）：数值等于某个声明选项时发送该选项的原样值（`1.0` 仍是浮点，`2` 仍是整数，兼容 Q7-07 的 `float` + `choices:[1,2]`），无选项的 `float` 参数整数发成浮点；字符串、布尔与不匹配任何选项的值原样交给设备判断。仿真“运行技能”（读设备 `/v2/skills` 的声明）与应用步骤（项目里登记的技能声明）都走这一步。运行时仍按声明类型严格比较，无需放宽。
  - **Q7-11**：仿真机器人的横幅、实时状态、锁存守护说明不再出现“硬件急停”，改为“仿真机器人：点“重启仿真机器人”或关闭仿真（重启后急停仍保持锁存）”，并直接给两个按钮；外部连接的仿真本体说“关闭它的运行时进程”；真机声明 `hardware_estop: false` 时说“切断动力”。
- 验证：`tests/test_ux05_fix.py`；`tests/ux05_fix_browser.cjs`（ux01 夹具 + `NX_FIXTURE_IGNORE_STOP=1`：仿真四足用 `tests/ignore_stop_adapter.py`（据审查的不服从停止适配器改写），夹具另起两个真实 `neuxmind_runtime serve` 作为“真机”按地址与令牌接入——一台同样不服从停止、清单声明真机与硬件急停，一台服从停止但清单放宽标定），9 张截图，约 57 s，JS 错误 0；真实 `safety.stop_not_obeyed` 约 33 s 后横幅出现在“模型库”页，复位后数秒内消失；10 s 内 `robots/safety` 请求 3 次）；`tests/test_ux05_fix.py` 19 项（类型恢复、放宽/标定、监视限频、告警保留与清除、只问本项目已连接机器人）；回归 ux01–ux05、sim_reset、desktop02、ux04_errors 与五套全量单测。
- 遗留：运行时本轮尚未提供 `relaxed` 标志时，浏览器验收对“放宽”警告用桩数据验证（标定行用真实运行时验证），合并运行时后应复验；切到其他项目时不显示别的项目里机器人的告警（监视按当前项目）；实时状态之外的地方没有声音/系统通知。

## ux05-fix2 · 2026-11-09 · 第 07 轮复审修复（桌面）：浮点选项全路径、复位时限按机器人能力说话、放宽只列更宽松的键、跨项目安全监视（brainos/、web/、tests/）

- 目标：复审 Q7b-04、Q7b-05、Q7b-06（P2）与 Q7b-07、Q7b-08、Q7b-10（P3）。
- 关键决策：
  - **Q7b-04**：“登记技能”表单只在浏览器里检查参数说明是否为合法 JSON，**发送原文**，由桌面后端解析（Python 保留 `1.0`）；`add_skill` 存储时按声明类型规范数值（`robot_caps.parameter_view(typed=True)`：`float` 的选项/默认/范围一律浮点，`int` 的整值一律整数）。发送时按声明类型序列化（`typed_parameters`）：`float`/`int` 参数一律按声明类型；只有读自设备本身的声明（仿真“运行技能”读 `/v2/skills`）在等于某个声明选项时发设备原样字面量（兼容旧运行时的 `float` + `choices:[1,2]`）。应用的 validate / evaluate / deploy 发送前都经 `typed_application` 按项目里技能声明重排类型——修复前保存的技能声明（`[0.5, 1]`）和应用（`speed: 1`）也发 `1.0`；存储的应用与摘要不变（运行时只回显 `digest`）。不依赖运行时放宽 int/float。
  - **Q7b-05/06**：`service.reset_policy(status, manifest)`：`expire_s = clamp(max(8, reset_expected_s + 5), 8, 30)`，`reset_expected_s` 取 `/v1/status.reset_expected_s`（或 `status.safety.reset_expected_s`、清单 `safety.reset_expected_s`），缺省 8；`/v1/reset` 的 HTTP 超时 = `expire_s + 4`，桌面不会先于机器人放弃。“超时才完成的复位会被作废”只在 `/v1/status.capabilities.reset_expire === true` 时说；否则如实说“这台机器人的运行时不会作废超时的复位：它一旦完成，急停可能自动解除……请保持远离；按‘停止’可以取消它”。连接检查把策略存到机器人记录（`reset_policy`），复位表单写“桌面最多等待 N 秒”与该运行时是否作废；复位记录带 `expire_s`、`reset_expire`。运行时的 409 `estop_latched` + `cause: reset_expired` 有自己的白话。
  - **Q7b-08**：本次请求超过 `expire_s` 才失败时，记录标 `own_reset`/`expired`，标题改为“这次解除急停已超时作废，急停保持锁存”（支持时）或“这次解除急停超时了，复位还在执行”（不支持时），正文说“这次复位已经超过 N 秒的时限，现在在机器人上已作废”，不再出现“上一次复位”“如果它超过时限才完成”，也不再转述运行时的“请稍候”提示。仿真实时状态与真机详情页的“复位还在执行”同样按能力措辞（`reset` 事实：`reset_expire`、`expire_s`、`in_progress_s`）；真机详情页新增此提示和“停止（取消那次复位）”。
  - **Q7b-07**：`safety_watch.relaxed_view` 优先用运行时 `latched_motion.relaxed_keys`（键、值、默认、来源）；没有时只列已知比默认更宽松的键（所有锁存守护键都是越大越宽松，默认值取 spec `LATCH_GUARD_FIELDS`），等于或更严格的不算。放宽警告写“偏航漂移（yaw_drift_rad_s = 0.002，默认 0.0003）”；其余非默认标定另起一行“其余现场标定（不比默认宽松）”；`yaw_total_rad` 有中文名“偏航总量上限”。
  - **Q7b-10**：`robots/safety` 新增 `all_projects: true`：监视所有项目的机器人（当前项目在前、锁存的在前，上限 64 台；每台限频不变），条目带 `project_id`/`project_name`。横幅里别的项目的机器人写“（项目：X）”，按钮“切换到该项目查看”。窗口隐藏时：有锁存或告警则照常轮询；全部安静时每 30 s 看一次（不再完全暂停）。
- 验证：`tests/test_ux05_fix2.py` 17 项（手填声明保留浮点、validate/evaluate/deploy 三条发送路径、修复前数据、设备声明字面量、复位策略取值与能力、超时请求记为“这次”且无“请稍候”、仿真实时事实、`relaxed_keys`/回退比较、跨项目监视）；`tests/ux04_errors.cjs` 新增迟到复位措辞（125 项）；`tests/ux05_fix2_browser.cjs`（ux01 夹具 + `NX_FIXTURE_IGNORE_STOP=1 NX_FIXTURE_FIX2=1`，夹具另起“混合标定”与“复位会卡住”两个真实运行时）：手工登记 1.0 选项技能 → 应用步骤选“快” → 真实运行时 validate/evaluate/deploy 全部接受；混合标定只列两键；卡住的复位 12 s 后显示“这次”且如实说旧运行时不作废，放开后真实运行时因 `expire_s` 保持锁存；`reset_expire` 用浏览器桩验证支持时措辞；B 项目机器人不服从停止时在 A 项目横幅出现并可切换；隐藏窗口锁存时 8 s 内轮询 4 次、安静时 0 次。
- 遗留：`capabilities.reset_expire` 与 `reset_expected_s` 由运行时 Agent 本轮加入，合并后需用真实字段复验（目前浏览器用桩、单测用桩）；手工登记的 `float` 参数若写成整数选项（`choices:[1,2]`），应用步骤会发 `1.0`——旧的严格运行时会拒绝，需运行时按数值相等比较（本轮运行时修复项）。

## ux05-fix3 · 2026-11-09 · 第 07 轮复审修复（桌面）：浏览器验收改用真实运行时的 reset_expire / reset_expected_s / 漂移提示（tests/）

- 背景：运行时 69bcbff 已真实报告 `capabilities.reset_expire: true`、`reset_expected_s` 与 `latched_motion.drift_notice*`（`safety.latched_drift_notice`，每次锁存一次）。ux05-fix2 的浏览器验收当时用桩，合并后在“桌面最多等待 12 秒 / 不会作废超时的复位”处失败。桌面代码无需改动。
- 夹具：`tests/ignore_stop_adapter.py` 中“复位会卡住”的真机清单声明 `safety.reset_expected_s = 7`（桌面发 `expire_s = 12`、等待 16 s）；新增 `_Creep`（`NX_FIXTURE_CREEP_FLAG` 存在时，每次锁存后 2 s 本体沿 x 滑移 9 cm，不受停止影响，然后静止；复位后重新布防），桌面仿真四足（`IgnoreStopQuadruped`）与新真机 `CreepRealQuadruped` 都带它；`tests/ux01_browser_fixture.py` 在 `NX_FIXTURE_FIX2=1` 时另起 `creep` 真实运行时，`GET /fixture/creep?on=1|0` 开关。
- 浏览器验收 `tests/ux05_fix2_browser.cjs`：主断言改为真实运行时——机器人记录 `reset_policy = {reset_expire: true, reset_expected_s: 7, expire_s: 12, timeout_s: 16}`；表单“桌面最多等待 16 秒。复位超过 12 秒才完成时，机器人会把它作废、急停保持锁存”；≥15 s 后“这次解除急停已超时作废，急停保持锁存”“超过 12 秒的时限，现在在机器人上已作废”；详情页 `data-reset-expire="supported"`；迟到的复位返回后急停仍锁存。旧运行时措辞改用 `page.route('**/api/**')` 把 `reset_expire` 改成 false / 删除：表单与结果都说“不会作废超时的复位”“急停可能自动解除”，不出现任何“作废”承诺。漂移提示：仿真实时视图连续两次锁存各出现一次、真机详情页原样显示运行时 `drift_notice_detail.message`、每秒检查 5–6 s 只出现一次，复位后消失（运行时 `drift_notice` 也回到 false）；未声明 `reset_expected_s` 的机器人表单写 12 秒 / 8 秒。
- 单测：`tests/ux04_errors.cjs` 新增 `reset_expire` 缺失或非 true 时绝不说“作废”（126 项）。`tests/ux05_browser.cjs` 的卡住复位（Q6-11 #1）同样因真运行时支持作废而改断言：桌面这次请求超过 8 s 后标题与说明都说“这次……已作废”。
- 验证：浏览器 ux01–ux05、ux05_fix、ux05_fix2、sim_reset、desktop02 各用独立夹具全部通过；五套全量单测全绿（spec 17、runtime 470、cloud 211、sdk 84、tests 283/跳过 39）。
