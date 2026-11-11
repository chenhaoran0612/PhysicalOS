## 第 01 轮 · 2026-11-04 · 云平台 cloud/ 第一版：多租户账户、token 账本、模型市场、推理网关

- 目标：把桌面端放在本机 SQLite 里的注册和计费（`brainos/auth.py`、`brainos/commerce.py`）换成独立的多租户云服务，实现 `INTERFACES_V1.md` 第 2 节。
- 关键决策：
  - 继承 `brainos/commerce.py` 的账本设计：点数全部是整数；余额只来自只追加的账本；调用前先预留；按服务端计量证据结算；结算幂等（`settle:<rsv>` 是唯一引用）；结果不明时保留预留，标记 `reconciliation_required`。
  - 并发：每次动钱都先锁住组织的钱包行。PostgreSQL 用 `SELECT ... FOR UPDATE`，SQLite 用 `BEGIN IMMEDIATE`。
  - token 套餐用法币付款，走 PaymentProvider；模型授权用钱包点数付款，扣点和发放授权在同一事务里。所有网关模型和端侧包下载都要先有授权（按量计价和免费模型的授权是 0 点）。
  - 推理预留上限 = max_tokens + 输入字节数 + 64×消息条数（字节级 BPE 下这是 prompt token 的安全上界）。实际用量超出上限时转人工核对，不自动扣费。
  - `/gateway/sessions` 的字段与桌面端 `/v1/agent-sessions` 一致，预留和结算在云端完成。
  - 平台运营员只能通过环境变量或管理命令授予；组织内的 `operator` 角色是现场操作员，两者不是一回事。
  - （待总指挥写入 DECISIONS.md）
- 分工：云平台 Agent → `cloud/neuxmind_cloud/`（服务）、`cloud/tests/`（73 个测试）、`cloud/README.md`、`cloud/Dockerfile`、`cloud/requirements.txt`
- 改动：新增 `cloud/` 目录（没有修改其他目录）。包括 auth、ledger、catalog、payments、gateway、upstream、audit 模块，5 个路由模块，app 工厂和 CLI（serve / init-db / grant-operator）。
- 验证：`python3 -m unittest discover -s cloud/tests` → 73 个测试全部通过（SQLite）。仓库根 `tests/` 仍然通过（72 个，跳过 20）。实际启动 `python3 -m neuxmind_cloud --port 8800` 并做了冒烟检查。
  - **测试替身通过**：sandbox 支付（HMAC webhook、重复只入账一次、金额不符时拒绝）；EchoProvider（推理全流程、超时 / 报错 / 缺 usage / 超额四种路径）；本机假 OpenAI 兼容 HTTP 服务（chat / responses 的协议解析、凭据注入、错误分类）；用 20 个线程并发预留，验证不超卖；用 8 个线程并发结算，验证只记一次账。
  - **未验证**：真实支付渠道（Stripe / 支付宝 / 微信支付没有接入，只返回 `provider_not_configured`）；真实模型供应商；真实 PostgreSQL 上的并发（只做了编译期检查，确认生成 FOR UPDATE）；桌面端真实切换到云网关（还需要支持流式）。
- 遗留：真实支付渠道验签与接入；网关会话支持 Responses 流式（SSE）；Alembic 迁移；清理过期会话和卡住的预留；授权吊销、订阅与退款；端侧模型包托管和签名下载 URL；注册按 IP 限速与邮箱验证；在真实 Postgres 上做并发测试。

## 第 01 轮修复 · 2026-11-05 · 云平台：关闭两个 P0（运营员抢注、会话免费用量）与审查的其余 cloud 问题

- 目标：处理 `docs/evolution/reviews/2026-11-05-iter01-review.md` 中全部 cloud 发现（C-01…C-11）以及两个存活变异（C4、C10）。
- 关键决策：
  - **邮箱验证**（C-01）：注册后 `email_verified=false`，一次性令牌只存 SHA-256、24h 过期、单次使用、重发作废旧令牌。`EmailSender` 抽象：dev/test 默认记录型发送器（不发邮件，响应带 `dev_verification_token`，标注 `simulated`，在 prod/staging 启动即拒绝）；SMTP 发送器；prod 未配置时重发接口返回 503 `email_sender_not_configured`，服务端运营人员可用 `verify-email` 命令。
  - **平台运营员 = 已验证邮箱 且（`grant-operator` 命令 或 白名单）**。生产推荐只用 `grant-operator`（README、Dockerfile 已改，Dockerfile 去掉示例运营员邮箱）。旧库补列后所有用户为未验证，已有运营员需重新验证。
  - **未验证邮箱的用户可以注册、登录、浏览，但购买点数/模型授权、发起支付、调用网关（infer、开会话）需要已验证邮箱**（403 `email_unverified`；API Key 看创建者）。
  - **会话调用参数白名单**（C-02）：未知参数 400 `parameter_not_allowed`；`n` 只能为 1 且下发 `n=1`；输出上限（`max_tokens` / `max_completion_tokens` / `max_output_tokens`）一律由网关按剩余预算写入，客户端值只能调小，chat 下发哪个字段由模型卡 `upstream.output_param` 决定；只接受文本内容与函数工具；工具定义等所有转发字节计入输入上界。
  - **超额计费新规则（推翻 ADR-007 中"超额不自动扣费"，请总指挥另立新 ADR；原定的 ADR-011 编号已被“现场证据失败即关闭”占用）**：上游报告用量超过本次预留时，按预留上限计费（infer：结算整笔预留；会话：本次计入 hold），输出不返回给用户，超出部分写入 `overage_records`（open）待运营核对（免除，或在可用额度内补扣，账本引用 `overage:<id>`）；有超额调用的会话锁定，只能关闭。平台从不在没有运营操作时扣超过预留的点数。
  - 登录限速改为（邮箱, IP）硬限、IP 硬限、邮箱逐步延迟（≤30 s）三维度，计数在 Postgres advisory lock 内完成；只信任 `NEUXMIND_TRUSTED_PROXIES` 的 `X-Forwarded-For`；注册按 IP 限速（C-03）。
  - 上游错误：请求未发出（DNS、拒绝连接、TLS 证书、缺凭据）与 4xx（408 除外）→ 释放不扣费；5xx、408、超时、中断 → 保留待核对（C-04）。
  - 清理任务 `maintenance.py`（C-05）：pending 超时的调用判定丢失（infer 预留转核对；会话释放占用并计为未知），过期会话自动关闭结算，孤儿 held 预留转核对；后台线程（默认 60 s）+ 网关请求惰性触发 + `/admin/maintenance/sweep` + `sweep` 命令；迟到的结果不重复记账。
  - P3：用户可见错误只给通用文案（C-06）；scrypt N=2^17，登录时自动升级旧哈希，同时最多 4 个哈希（C-07）；prod 关闭 OpenAPI 文档（C-08）；prod 拒绝上架 echo（C-09）；`GET/DELETE /org/members`，只有 owner 能改/移除 admin（C-10）；运营不能处理 held 预留（C-11）。
- 分工：云平台修复 Agent → 只改 `cloud/`。
- 改动：新增 `mailer.py`、`session_params.py`、`maintenance.py`；改 auth、gateway、upstream、catalog、config、db（补列 + advisory lock）、security、models（3 张新表、5 个新列）、routes、CLI（verify-email / revoke-operator / sweep）、Dockerfile、README。新增测试模块 test_email_verification、test_session_hardening、test_rate_limits、test_maintenance、test_p3_hardening，并在 test_auth 补 C4 测试。
- 按新规则修改的旧测试（行为是有意改变的，不是放宽）：`test_error_classification`（500 由 Rejected 改为 Unknown，并补 4xx/408/5xx/DNS/TLS）、`test_missing_usage_and_overuse_go_to_reconciliation` 拆为"缺 usage 转核对"+"超额按上限计费"、`test_echo_upstream_refused_in_prod`（上架即拒绝，另以直接入库的卡片保留调用时拒绝的检查）；测试夹具 `register()` 默认走真实验证流程，`operator()` 不再"注册白名单邮箱即成为运营员"。
- 验证：
  - `python3 -m unittest discover -s cloud/tests` → **112 个通过**（原 73 → 74 → 112；SQLite）。同一套 112 个测试在本机临时起的 **PostgreSQL 16** 上也全部通过（每个测试重建 schema，psycopg 3.2.10）。
  - Postgres 并发：20 个并发错误密码登录（上限 3）→ 3 个 401、17 个 429；12 个同 IP 并发注册（上限 3）→ 3 个成功；把 advisory lock 换成空操作作对照 → 17 个 401、12 个成功（证明锁是必要的且有效）；30 个并发预留（余额 100、每次 10）→ 10 个成功。
  - 审查脚本复验：`cloud_checks.py` 原样运行，第 1 节抢注白名单邮箱 → `platform_operator: False`、自调额 403；其余小节依赖抢注做准备，改用验证流程后（同一攻击）：`n=50` → 400 `n_unsupported`、上游未被调用；`max_completion_tokens=100000` → 下发 `{n:1, max_tokens:891}`，会话用量 911/1000，关闭扣 10 点；用户看到的错误为"上游服务暂不可用"，不含凭据变量名；攻击者地址被限速时受害者从自己的地址正常登录。`cloud_concurrency.py` 原样运行结果与基线一致。
  - 变异：审查的 C1–C12 中 C4 原先存活，现被 `test_demoting_creator_narrows_key_scopes` 杀掉；C10 的代码行已改写，按新代码做同义变异，被 `test_output_limit_is_capped_by_remaining_budget` 杀掉；其余 C 变异仍被杀掉。另对本轮新增的 27 处防线做变异，首轮 26 个被杀、1 个存活（登录成功只清除本地址计数），补测试后全部被杀。
  - `tests/`（82，跳过 20）、`spec/tests`（11）、`runtime/tests`（109）不受影响。`sdk/tests`：`CloudTests` 的 setUpClass 因"白名单邮箱需验证才是运营员"失败（预期，见契约变更请求）；在不改 `sdk/` 的前提下用运行时补丁模拟"注册后提交 `dev_verification_token`"，SDK 28 个测试全部通过。
- 遗留：真实 SMTP 未验证（只用 mock）；无找回密码/改邮箱；会话流式（SSE）；Alembic 迁移（目前只有补列）；"丢失调用"按时间阈值判定，慢速吐字节的上游可能被误判（迟到结果不会重复记账，只留证据）；超额补扣受可用额度限制，不会让钱包变负；API Key `last_used_at` 每次写库；授权吊销/退款。

## 第 01 轮复审修复 · 2026-11-05 · 云平台：N-01 预注册劫持（以及 N-07、N-08、N-11）

- 目标：`docs/evolution/reviews/2026-11-05-iter01-rereview.md` 的 N-01（P1，已复现）：攻击者先用运营员邮箱注册并设密码，主人点开不需登录的验证令牌，验证的是攻击者账户，攻击者会话成为运营员；主人重注册 409、无找回密码；`verify-email` / `grant-operator` 命令同样会把权限交给攻击者。
- 关键决策：
  - **验证在本账户会话内完成**：`POST /auth/verify-email` 需要会话，令牌所属用户必须等于会话用户（否则 403 `verification_wrong_account`，令牌不消耗）。验证成功后删除该用户其他会话、吊销验证前创建的全部 API Key。
  - **认领**：邮箱被未验证账户占着时，注册返回 202 `claim_sent` 并向邮箱发认领令牌；`POST /auth/claim` 为持令牌者新建已验证账户，旧账户停用（邮箱改名、密码不可用、会话删除、Key 吊销、运营标志清除）。认领令牌不互相替换，占用者无法作废主人的令牌；占用账户若已被其本人验证，认领令牌失效。
  - **找回密码**：`/auth/password-reset/request`（永远 202，不暴露账户存在）+ `/auth/password-reset`（重设后删除全部会话；API Key 保留，README 说明）。未验证账户申请重设得到的是认领令牌。令牌按用途区分（verify / reset / claim），互不通用。
  - **运营员只由管理命令产生**：新增 `create-operator <email>`（服务器上创建已验证的运营员账户，密码不可用，输出一次性设密令牌），README 推荐流程改为它；`grant-operator` 拒绝未验证账户并打印公开注册时间与 IP；`verify-email` 命令在账户有会话或 Key 时必须 `--force`，并且总是把密码设为不可用、吊销会话和 Key、输出设密令牌交给核实过的主人。
  - **白名单在 prod/staging 默认不生效**，需 `NEUXMIND_OPERATOR_ALLOWLIST=1`，README 写明风险；dev/test 默认启用。
  - 顺带修复复审的 cloud P3：N-07（折算 0 点的超额只能 waive）、N-08（处理超额时对记录行 `FOR UPDATE`）、N-11（惰性清理在认证之后才触发）。
- 改动：新增 `recovery.py`；改 auth（会话绑定验证、`create_account`、`revoke_credentials`、grant 拒绝未验证）、models（令牌 `purpose` 列，旧库自动补列）、config（白名单开关）、mailer（认领/重设邮件）、routes/account（202 认领、三个新接口）、routes/admin（N-07/N-08）、routes/gateway_routes（N-11）、CLI、README。测试夹具：验证改为带会话提交；prod 环境的运营员走 `create-operator` + 设密令牌。
- 验证：
  - `cloud/tests` **125 个通过**（112 → 125），同一套在 PostgreSQL 16 上也全部通过。
  - 复审脚本 `cloud_attack.py`（指向当前代码；只把"合法用户自己验证"的准备步骤改为带本人会话，攻击步骤原样）：主人不登录提交邮箱里的令牌 → `[401, 401]`；攻击者会话 `platform_operator=False`、自调额 403；主人重注册 → 202 `email_pending_claim`；认领后主人是运营员、攻击者会话 401。脚本 SUMMARY 中唯一的"bad"（N-07）修复后不再出现。
  - 变异：针对新防线的 17 个变异首轮 16 个被杀，"停用账户密码不可用"存活 → 补断言后被杀，全部被杀。
  - `tests/`（82，跳过 20）、`spec/tests`（11）、`runtime/tests`（126）通过。`sdk/tests`：`CloudTests.setUpClass` 失败（SDK 的 `verify_email` 不带会话，预期，见交付报告）；用运行时补丁让 `verify_email` 带会话后 29 个中 28 个通过，剩下 1 个断言的正是被取消的"未登录也能验证"。
- 遗留：N-05（会话并发在途调用都可超额）、N-09（tool_choice / tool_calls 嵌套键未按白名单重建）留到第 02 轮；被停用账户及其个人组织保留在库中未清理；SMTP 仍只用 mock 测过；没有修改邮箱。

## 第 02 轮 · 2026-11-08 · 云平台：网关会话 SSE 流式、同会话在途调用串行化（N-05）、嵌套参数白名单（N-09）

- 目标：契约 §4.3——会话 `{base_url}/chat/completions` 与 `/responses` 支持 `stream: true`，以上游 usage 计量、已转发输出作下界、超上限截断、usage 前中断按预留上限转核对；复审 N-05、N-09。
- 关键决策：
  - **流式架构**：预留事务（短）→ 在请求线程里打开上游流（拿到状态码；此前的失败仍是普通 JSON 错误）→ 独立生产线程逐事件解析、计量、透传，经有界通道（64 事件，反压到上游）交给事件循环 → 结算事务（短）→ 网关自己的唯一终止事件（`neuxmind.metering` 或 `event: error`）。流式期间不持有任何事务或钱包锁。没有用 Starlette 的 StreamingResponse：自写 ASGI 响应，在所有 ASGI 版本上都监听 `http.disconnect`，并且无论以何种方式结束都通知生产线程。
  - **计量**：上游 usage 为准；已转发的非空输出增量个数为下界（假设：上游按 token 逐块输出），本次 completion = max(两者)；转发前若下界将超过本次输出上限 → 截断、断开上游、`output_limit_reached`；usage 之前中断（含截断、超时、上游 error 事件、无 usage、超最长时长）→ 本次按 hold 计入会话用量并计为未知，关闭时转核对（ADR-012 精神：扣费永不超过预留，计入 hold 防止剩余预算被重复使用）；usage 超过 hold → 按 hold 计、记超额、锁会话（输出已送达，无法撤回，README 写明）。
  - **客户端断开**：不立即放弃，而是继续排空上游拿到 usage 精确结算（用户点"停止"不会被按预留上限计费）；排空同样受截断与时长上限约束，失败则按中断处理。
  - **最长流式时长**自动取 `stale_call_seconds − upstream_timeout − 30`，保证流先于清理任务的"丢失判定"结束。
  - **N-05 选择串行化**（409 `session_busy`），理由见 README「同一会话的在途调用」：串行化后每个会话最多一次调用可能超出 hold，"每个在途调用单独预留"只保证预算不超、仍允许 N 次并发超额；工程助手天然串行；判定点单一、在钱包锁内，SQLite/PG 同一保证。（请总指挥写入 ADR）
  - **N-09**：`tool_choice`、`tools[]`/`tools[].function`、`messages[].tool_calls[]`（含 `function`）、内容片段（按类型的精确键）、`response_format`、`text`/`text.format`、responses 的 `function_call`/`function_call_output` 项逐层白名单，多余键 400 `parameter_not_allowed`（带完整路径），类型错 400 `invalid_parameter`。
  - OpenAI 兼容上游的流用 `read1()` 读：`readline()` 经 `peek()` 会吞掉 chunked 断流的 `IncompleteRead`，断流会被误当成正常结束（只会落到 `usage_missing`，计费结果相同，但证据不准）。
- 分工：云平台 Agent → 只改 `cloud/`。
- 改动：新增 `streaming.py`；改 gateway（会话调用拆为开始/结算、N-05、流式结算）、upstream（`open_stream`、Echo 流式替身、`_post` 改用共享的 `_open`）、session_params（N-09、`stream`、`stream_options` 由服务端写入）、catalog（Echo 新行为与 `stream_delay`）、config（`stream_max_seconds`、`stream_idle_seconds`）、routes/gateway_routes（会话调用路由改 async，网关逻辑仍在线程池）、README。测试：新增 `test_streaming.py`（23）、`test_session_hardening.NestedParamsTest`（2，另继承原 7 个）；`support.py` 增加 `NEUXMIND_TEST_DATABASE_URL`（整套跑 PostgreSQL）与 `LiveServer`（真实 uvicorn，用于断开与并发）。
- 按契约修改的旧测试：`test_budget_is_a_hard_ceiling` 中"`stream:true` → 400 `stream_unsupported`"改为"流式同样受预算约束（402）、非布尔 stream → 400"。
- 验证：见 PROGRESS.md 第 02 轮与交付报告（SQLite / PostgreSQL 测试数、worktree 变异结果）。
- 遗留：真实模型供应商的流式未验证（只有本机假 SSE 服务）；下界假设"按 token 逐块输出"；流式输出无法撤回（prompt 侧超额只能事后记账）；非流式"结果未知"不计 hold、流式中断计 hold，两者不一致（保留旧行为，待定）；`stream_idle_seconds` 的"客户端不读"路径没有自动化测试（TCP 缓冲使其难以稳定复现）。

## 第 02 轮审查修复 · 2026-11-08 · 云平台：流式墙钟截止（Q2-02）、网关事件命名空间与 SSE 规范化（Q2-03）、下界补全（Q2-06）

- 目标：`docs/evolution/reviews/2026-11-08-iter02-review.md` 的 cloud 条目，以及 SDK 侧提出的 Echo 非流式 `/responses` 形状问题。
- 关键决策：
  - **Q2-02**：截止时间改为墙钟。每个流起一个看门狗线程，到点（或上游连续 `upstream_timeout_seconds` 一行都没有）就中断上游连接；OpenAI 兼容上游用 `socket.shutdown`，阻塞中的读立即返回。另在行级（`_guarded`）检查：每一行（含保活注释、空行）都记为活动并检查绝对截止，注释不延长总时长。
  - **Q2-03**：网关保留命名空间。上游 `event: error` → `event: upstream.error`；上游 `neuxmind.*`（事件名不区分大小写，或 data 的 `type`）丢弃、不计量。SSE 切行按 WHATWG（CRLF / LF / 单独 CR，含跨块的 CRLF），重新编码时值里不可能有 CR/LF，杜绝注入。客户端看到的 `error` / `neuxmind.metering` 只可能来自网关，且恰好一个、在最后。
  - 修复中发现我自己的一个缺陷：上游发了 error 事件后正常结束（无 usage）时，被归为 `usage_missing` 而不是 `upstream_stream_error`，已修正。
  - **Q2-06**：responses 流里所有非空 `*.delta` 事件都计入下界。
  - Echo 非流式 `/responses` 返回 Responses 形状（`output[]`、`usage.input_tokens/output_tokens`）；chat 形状去掉了多余的 `output_text`。
- 验证：cloud SQLite 174、PostgreSQL 16 174 全过；审查复现脚本 `cloud_deadline.py` 原样运行：`elapsed 0.5s (max_seconds=0.5) termination=deadline`（修复前 3.0 s）；新增攻击测试（CR 注入伪造计量、保留名与大小写变体、data 内 `type: neuxmind.*`、上游同名 error、多行 data、纯 CR 换行、逐字节 CRLF）；worktree 变异 18 个，首轮 6 个存活，删 1 处死代码、补测试后剩 1 个等价变异（行级截止与看门狗的 `_abort_reason` 检查重叠，作为纵深防御保留）；sdk/tests 48 个通过。
- 遗留：契约 §4.3 需写入上游事件名映射；SDK 建议只认带 `neuxmind` 计量对象的终止事件（当前 SDK 无需修改即可工作）。

## 第 03 轮（桌面 Agent 兼容改动）· 2026-11-08 · 云平台：会话白名单兼容内置工程运行时（Codex 0.146.1）、终止事件结算后转发、Echo 工具循环替身

- 目标：契约 §5.3「云网关需确认与内置工程运行时（Codex app-server Responses 流式 + 函数工具）兼容；不兼容的参数在云端白名单内明确报错」。由桌面集成 Agent 完成（第 03 轮先后两个 Agent，后者接手审查并提交），只改 `cloud/`。
- 依据：用真实 Codex 0.146.1 app-server（npm `@openai/codex@0.146.1-linux-x64`，sha512 已核对）经 `brainos.agent_runtime` 对本机 Responses SSE 服务录制的 3 个请求体，存于 `tests/fixtures/codex-0.146.1-responses.json`（逐字，去掉 Authorization）。顶层字段：`model instructions input tools tool_choice parallel_tool_calls reasoning store stream include prompt_cache_key client_metadata`；工具 31 个函数 + 3 个 `namespace`；输入项含 message / function_call / function_call_output / reasoning。
- 放行（每项都有理由，见 README 白名单表）：
  - `store` 只接受 `false`（`true` 会在平台上游账户下存数据）；
  - `include` 只接受 `["reasoning.encrypted_content"]`（只是本次响应的加密推理；其他取值返回存储文件、检索结果等）；
  - `prompt_cache_key`：网关改写为 `nx-` + sha256(org_id ‖ key)[:40] 后下发（单一上游账户服务所有租户，防跨租户缓存路由）；输入上界 +64；
  - `client_metadata`：校验大小后**丢弃**，不下发（设备/安装标识不交给供应商），不计入输入上界；
  - `tools[]` 的 `namespace`（函数工具分组，不可嵌套；函数合计 ≤128，逐个计模板开销）；`function_call.namespace`；
  - `function_call_output.output` 可为只含 `input_text` 的片段列表（图片结果仍拒绝：token 成本与字节无关）；
  - `reasoning` 输入项（工具循环回传；summary/content/encrypted_content 字节全部计入输入上界，若供应商计费高于上界按 ADR-012 超额处理）。
  - 保持拒绝：`previous_response_id`、`conversation`、`prompt`、`background`、`metadata`、`service_tier`、内置工具、图片/文件输入等（测试 `test_refusals_with_a_safety_reason_are_kept`）。
- 流式：上游终止事件（`response.completed`/`incomplete`/`failed`、chat `[DONE]`）改为**结算完成后**再转发。原因：Codex 读到 `response.completed` 立即发工具循环的下一次调用，旧实现在结算前转发，下一次调用会撞上 409 `session_busy`（ADR-016 串行化）。事件顺序与"网关终止事件恰好一个且在最后"不变。测试 `TerminalEventAfterSettlementTest`。
- Echo 替身新增行为 `tools`（仅流式 `/responses`）与模型卡 `upstream.tool_name` / `tool_arguments`：完整 Responses 事件流（含 output_item），首轮调用函数、拿到 `function_call_output` 后回答；所有事件带 `simulated: true`。仅 dev/test 可上架。
- 改动：`session_params.py`、`gateway.py`（下发前重键 `prompt_cache_key`）、`streaming.py`、`upstream.py`、`catalog.py`、README；新增 `tests/test_codex_compat.py`（12）与录制夹具。
- 验证：cloud/tests SQLite 186 通过；PostgreSQL 16（本机临时库，用后删除）186 通过；桌面端经真实网关 + 真实 Codex 的工具循环、调查、超额、预算不足测试通过（见 brainos/EVOLUTION_ENTRY.md 第 03 轮）。
- 遗留：只录制了 Codex 0.146.1；升级运行时须重新录制并跑 `test_codex_compat`。真实供应商对回传 `reasoning`/`encrypted_content` 的计费与字节上界的关系未经真实账单验证（超额走 ADR-012）。cloud 依赖（SQLAlchemy 2.1）要求 Python ≥ 3.11，云端不在 §5.0 的 3.10 要求范围内。

## 第 03 轮审查修复（桌面 Agent）· 2026-11-08 · 云平台：会话调用体积上限（Q3-07）、缓存键始终按组织写入（Q3-15）、补测试（C4/C5）

- 依据：`docs/evolution/reviews/2026-11-08-iter03-review.md`。
- Q3-07：Codex 在 `store=false` 下每轮发送完整历史，长会话越过 1 MB 全局请求体上限后中途 413。会话模型调用（`POST /gateway/sessions/{id}/v1/responses|chat/completions`，路径严格匹配）单独上限 `session_max_body_bytes`（默认 8 MB，`NEUXMIND_SESSION_MAX_BODY_BYTES`），其他接口不变；所有字节仍计入输入上界并在钱包锁内从会话预算预留（测试：1.2 MB 请求在 20 万 Token 会话里 402、在 200 万 Token 会话里通过）。413 响应带 `max_bytes`，会话调用另附"会话历史过长，请开启新会话继续"。responses `input` 上限 1000 → 4096 项（每项仍加 64 模板开销）。权衡：未认证的大请求在鉴权前最多缓冲 8 MB（仅这两个路径）。
- Q3-15：`/responses` 调用一律写入 `scoped_cache_key(org_id, key or "default")`，不带键的请求也不再与其他租户共享默认缓存路由；输入上界对 responses 恒 +64。README 理由改为准确表述（依赖供应商按该键隔离路由）。
- 测试补齐（变异 C4、C5 存活项）：上界 +64 的精确断言（带键/不带键）、命名空间合计恰好 128 通过 / 129 拒绝；新增 `IterThreeReviewFixesTest`（5）与 `SessionBodyLimitTest`（1）；`test_session_hardening` 中 responses 下发字段集合加入 `prompt_cache_key`。变异复验：会话上限回退为全局上限、只在带键时改写、去掉 +64、上限 128→129，4 个均被杀死。
- 验证：cloud/tests SQLite 192、PostgreSQL 16 192（临时库用后删除）。

## 第 04 轮 · 2026-11-08 · 云平台：设备群保存一致性报告摘要、按一致性级别过滤模型与技能（契约 §6.4）

- 目标：设备群记录可上传并保存最新一致性报告摘要；`/catalog/models/compatible` 与技能兼容判定增加可选 `require_level=dry`（仅把 dry 已通过的能力视为可用）。由 SDK + 云平台 Agent 完成，只改 `cloud/`。
- 关键决策：
  - **云端不复核证据，只校验格式与一致性**（`conformance.py`）：schema、各级 `*_ok` 与 `levels` 一致、某级 ok ⇒ 更低级别 ok 且无失败/受阻、每项能力恰好含已请求的级别、未声明能力只能是 skipped、`simulated` ⇔ `evidence_kind=simulation`、motion 报告必须带授权。**拒绝** `evidence_kind:"device_run"`（400 `device_run_refused`）与 `field_acceptance` 非 false（400 `field_acceptance_refused`，缺失也拒绝）。
  - **绑定到登记的 manifest**：报告里没有 manifest 摘要，所以用 `instance_id` + `simulated` + **声明能力集合**与登记记录比对（409 `conformance_robot_mismatch` / `conformance_manifest_mismatch`），并在摘要里记下上传时的 `manifest_digest`；机器人重新登记、manifest 变了 → 摘要 `stale`，`require_level` 不再采信。较旧的报告不能覆盖同一 manifest 的较新报告（409 `conformance_outdated`）。
  - **只存摘要**（级别结论 + 每项能力各级状态，原因截断 300 字），不存 `cases`；摘要标 `self_reported:true` 与免责声明（ADR-011：不是现场验收）。
  - **`require_level` 严格按能力**：能力在 L 及更低级别都 `passed` 才可用；dry 没有测试项（"不适用"）的能力不算通过；`safety.stop` 不可用 ⇒ 全部不可用（失败即关闭）；`model.install` 不可用 ⇒ `model_runtimes` 视为空。只给 manifest 时必须能找到本组织同 `instance_id`、同摘要的登记记录。
  - 技能兼容判定：云端此前没有技能接口，新增 `POST /robots/{id}/skills/check`（`spec.check_compatibility`，`files` 忽略）。
  - 权限：上传与技能判定需要 `robots`；模型过滤沿用 `catalog:read`（与已有 `?robot_id=` 相同）；全部按 org 隔离（他组织 404）。
- 改动：新增 `neuxmind_cloud/conformance.py`、`tests/test_conformance.py`（16）；`models.py`（`robots.conformance` JSON 列）、`db.py`（旧库自动加列）、`routes/fleet.py`（`POST /robots/{id}/conformance`、`POST /robots/{id}/skills/check`、视图带摘要/简表）、`routes/commerce.py`（`require_level`、`robot_id`）、`catalog.py`（`manifest_digest`）、README。
- 验证：报告由**真实运行时一致性套件**在仿真四足/机械臂上生成后经 HTTP 上传；`cloud/tests` SQLite 208、PostgreSQL 16（本机临时集群，用后删除）208 全过；旧库缺列升级测试（SQLite 与 PG）；worktree 变异 19 个（拒绝 device_run、field_acceptance、实例/能力集合/仿真绑定、过期、级别不够、stop 失败即关闭、model.install、权限 scope、跨组织、manifest 摘要比对、未声明却通过、ok 却失败、较旧覆盖、旧库加列等）全部被杀死；SDK 端到端（真实运行时进程 + 云端）通过。
- 遗留：报告是自报的（无运行时签名）；绑定按能力集合而非精确 manifest 摘要（见变更请求）；`require_level` 只支持 static/dry。

## 第 04 轮审查修复 · 2026-11-09 · 云平台：测试跟进姿态 dry 用例（Q4-08）、报告 manifest_digest 精确绑定（Q4-12）、拒绝未来时间戳（Q4-17）、自报说明（Q4-13）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-review.md`。
- Q4-08（P1）：runtime b0e53c7 给 `legged.posture`、`model.install` 加了 dry 用例后，`test_conformance` 4 个用例仍按"姿态 dry 不适用"断言。测试改为：真实报告里姿态 dry 通过 → `posture-coach` 在 `require_level=dry` 下兼容；"未通过 dry 的能力不可用"改用显式构造的"姿态未验证"报告；`model.install` dry 通过 → 端侧包保留。README 同步。修复后复验审查变异 C4（`safety.stop` 失败即关闭被去掉）：新断言"stop 受阻时即使速度与姿态 dry 通过也没有任何模型/技能可用"，worktree 中变异被杀死。
- Q4-12（P2）：报告带 `manifest_digest` 时必须等于登记记录的 digest，否则 409 `conformance_manifest_mismatch`（附两边 digest）——manifest 变更后重传旧报告、伪造 digest 都被拒；不带 digest 的旧运行时报告可保存，但 `digest_verified:false`，`require_level` 不采信（409 `conformance_required` 并说明原因）；格式错误 400。
- Q4-17（P2）：`generated_at` 须为 `YYYY-MM-DDTHH:MM:SSZ` 且不晚于服务器时间 5 分钟，`9999-…` 这类时间戳不能再让之后的真实报告都被判 `conformance_outdated`。
- Q4-13：免责声明与 README 写明"报告由适配器所在进程自报、可被篡改，摘要只供参考（`self_reported`）"。
- 未改（留给契约）：Q4-22 `model.install` 的 dry 通过只表示"会拒绝非法请求"，按现行契约仍算可用，README 写明语义。
- 验证：见交付报告（在 HEAD 的 worktree 中测试，因为共享工作区的 runtime 有并行中的未提交改动）。
