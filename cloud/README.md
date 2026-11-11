# NeuxMind Cloud（`cloud/`）

多租户云端商业平台第一版：账户与组织、API Key、token 点钱包（只追加账本 + 预留）、模型市场、购买与支付接口、推理网关（兼容桌面端计量会话协议）、设备群登记、运营后台。实现契约 `docs/specs/INTERFACES_V1.md` 第 2 节。

> 诚实声明：**真实支付渠道（Stripe / 支付宝 / 微信支付）没有接入**，只有接口骨架，调用时明确返回 `provider_not_configured`。**真实模型供应商没有验证过**：OpenAI 兼容 provider 只在本机假服务上做过协议测试。`sandbox` 支付和 `echo` 上游是测试替身，只在 `NEUXMIND_ENV=dev|test` 下可用，返回结果都带 `simulated: true`。

## 快速开始

```bash
pip install --break-system-packages -r cloud/requirements.txt
cd cloud
NEUXMIND_ENV=dev python3 -m neuxmind_cloud --port 8800
# 平台运营员（推荐，生产同样适用）：在服务器上直接创建已验证的运营员账户，输出一次性设密令牌
python3 -m neuxmind_cloud create-operator you@example.com
# 然后：POST /api/v1/auth/password-reset {"token": "<令牌>", "password": "<新密码>"}
# OpenAPI 文档（仅 dev/test）：http://127.0.0.1:8800/api/v1/docs
```

测试（用 unittest，在仓库根目录执行）：

```bash
python3 -m unittest discover -s cloud/tests      # 211 个测试（SQLite，每个测试一个临时库文件）
# 同一套测试跑在 PostgreSQL 上（每个测试先清空 public schema，请用专门的测试库）：
NEUXMIND_TEST_DATABASE_URL=postgresql+psycopg://user@127.0.0.1/nxtest python3 -m unittest discover -s cloud/tests
```

> 第 01 轮修复（审查 C-01…C-11）改变了几条对外行为，见下文标注“修复”的段落与 `EVOLUTION_ENTRY.md`：
> 购买和网关需要**已验证邮箱**；平台运营员需要**已验证邮箱**；会话调用只接受**白名单参数**；上游用量超过预留时**按预留上限计费**、不返回输出、超出部分待核对。
>
> 第 02 轮：会话调用支持 **SSE 流式**（`stream: true`，见「会话流式」）；**同一会话同一时刻只允许一个在途调用**（第二个 409 `session_busy`，复审 N-05）；`tool_choice`、`tools[].function`、消息里的 `tool_calls` 等**嵌套字段也按白名单校验**（复审 N-09）。

## 架构

```
            HTTP /api/v1  (FastAPI, 同步处理函数)
  ┌──────────────────────────────────────────────────────────────────┐
  │ routes/account   auth · me · org members · api-keys · wallet · usage · capabilities
  │ routes/commerce  catalog · entitlements · purchases · payments(checkout/webhook)
  │ routes/gateway   /gateway/infer · /gateway/sessions(+/v1/chat/completions|responses, close)
  │ routes/fleet     /robots
  │ routes/admin     /admin/models · credit-packs · wallets/adjust · reservations · orders · audit
  └───────┬──────────────────────────────────────────────────────────┘
          │ 服务层（纯函数，传入 SQLAlchemy Session）
  auth.py  ledger.py  catalog.py  payments.py  gateway.py  streaming.py  upstream.py  audit.py
          │                                   │
  db.py（引擎、事务、钱包锁）                 spec_bridge.py → spec/neuxmind_spec（能力与 manifest 规则）
          │
  SQLite（开发/测试） / PostgreSQL（DATABASE_URL）
```

中间件：请求体大小上限（声明的和分块传输的都会检查，超出返回 413）、CORS 白名单（默认关闭）。所有错误都是 `{"error": "...", "code": "..."}`，有时附带额外字段（如 `reservation_id`、`available`、`needed`）。

### 并发与锁

所有动钱的操作（预留、结算、释放、入账、调额、用钱包点数买授权）都先调用 `db.lock_wallet(org_id)`：

- **PostgreSQL**：`SELECT ... FROM wallets WHERE org_id=? FOR UPDATE`，是真正的行锁。同一组织的并发事务排队，"检查可用额度 → 插入预留"这一步原子执行；不同组织互不阻塞。
- **SQLite**：没有行锁（SQLAlchemy 会去掉 `FOR UPDATE`）。所以每个 SQLite 事务都以 `BEGIN IMMEDIATE` 开头，一开始就拿数据库级写锁（`busy_timeout` 30 秒）。这样做是正确的，但所有写入串行执行，只适合开发和测试。

网关调用分三段事务（预留 / 调上游时不持锁 / 结算），调用上游期间不占数据库锁。流式调用同样如此：整个流式响应期间没有打开的事务、不持钱包锁（测试 `test_no_transaction_or_wallet_lock_is_held_while_streaming` 在流停在中途时对同一组织调额、开新会话，均立即完成）。

## 数据模型（表）

| 表 | 作用 | 要点 |
|---|---|---|
| `users` | 用户 | `password_hash` 为 scrypt（N=2^17, r=8, p=1；旧的 2^14 哈希登录时自动升级）；`email_verified`（只能由一次性令牌或 `verify-email` 命令置真）；`is_platform_operator` 只能用管理命令设置 |
| `email_verification_tokens` | 邮箱验证令牌 | 只存 SHA-256；绑定发送时的邮箱；`expires_at`（默认 24h）；`used_at`（单次使用）；重发会作废旧令牌 |
| `rate_events` | 通用限速计数 | 注册（按 IP）、验证邮件发送（按用户） |
| `organizations` | 组织 | 注册时自动建个人组织（`personal=true`） |
| `memberships` | 成员关系 | `role`: owner / admin / developer / operator（组织内的现场操作员，**不是**平台运营员） |
| `auth_sessions` | 登录会话 | 只存 SHA-256(token)，有 `expires_at` |
| `login_attempts` | 登录失败记录 | 按（邮箱, IP）、IP、邮箱三个维度限速，存在库里，多实例共享 |
| `api_keys` | API Key | `nx_live_` 前缀，只存哈希 + 前 12 位用于显示；`scopes`；`revoked_at` |
| `wallets` | 钱包锁锚点 | **不存余额** |
| `ledger_entries` | 账本（只追加） | 整数 `delta`；`reference` 唯一 = 幂等键（`order:<id>`、`settle:<rsv>`、`adjust:<ref>`） |
| `reservations` | 预留 | held / settled / released / reconciliation_required；`pricing` 为预留时的价格快照 |
| `model_cards` | 模型卡 | `upstream` 只对运营可见，只存 `credential_ref`（环境变量名），不存密钥 |
| `credit_packs` | token 套餐 | `credits` + `price_minor`（最小货币单位）+ `currency`，由运营定义 |
| `entitlements` | 模型授权 | (org, model) 唯一 |
| `orders` | 订单 | credit_pack / model_license；pending → awaiting_manual_confirmation → paid |
| `webhook_events` | 已处理的 webhook | (provider, event_id) 唯一 |
| `usage_records` | 用量 | (org, request_id) 唯一 = 网关幂等；含上游 usage、`upstream_ref` 证据、`simulated`；`hold_tokens`（本次占用，清理任务用）；状态 pending / succeeded / failed / reconciliation_required / overage |
| `gateway_sessions` | 计量会话 | 访问令牌只存哈希；`used_tokens`、`inflight_tokens`、`unknown_calls`、`overage_calls`、`overage_tokens`、证据列表 |
| `overage_records` | 超额记录 | 上游报告用量超过预留时写入：已按预留上限计费，超出部分 open → 运营 waived / charged |
| `robots` | 设备群 | (org, instance_id) 唯一；规范化后的 manifest、摘要、能力 ID、`simulated`；`conformance`（JSON，可空，第 04 轮：最新一致性报告摘要，旧库启动时自动加列） |
| `audit_logs` | 审计 | 注册/登录失败/Key 创建与吊销/购买/支付/webhook 拒绝/调额/核对/改价/机器人登记等 |

余额 = `SUM(ledger_entries.delta)`；可用 = 余额 − 状态为 held 或 reconciliation_required 的预留之和。

## API（前缀 `/api/v1`）

认证头：`Authorization: Bearer <nxs_会话 | nx_live_Key>`。会话用户可以加 `X-NeuxMind-Org: <org_id>` 切换组织（必须是成员），默认用个人组织。API Key 固定属于一个组织。

### 账户
| 方法 路径 | 请求 → 响应 |
|---|---|
| POST `/auth/register` | `{email, password(10–256), name?}` → 201 `{token, token_type, expires_at, user{…, email_verified:false}, org_id, verification:{status: sent|unavailable|failed, sender?, simulated}, dev_verification_token?}`（请求里的角色/运营/验证字段一律忽略）；按 IP 限速（默认每小时 20 次）。邮箱已被**未验证**账户占用时：不建账户、不发会话，**202** `{status:"claim_sent", code:"email_pending_claim", dev_claim_token?}`，认领令牌发到该邮箱（没有发信器时 409 `email_pending_verification`）；已被已验证账户占用：409 `email_taken` |
| POST `/auth/verify-email` | **需要该账户自己的登录会话**（复审 N-01）。`{token}` → `{email_verified:true, user, sessions_revoked, api_keys_revoked}`：验证成功后删除该用户其他所有会话、吊销验证前创建的全部 API Key。401（未登录）；403 `verification_wrong_account`（令牌属于别的账户，令牌不会被消耗）；400 `verification_token_invalid` / `verification_token_expired` |
| POST `/auth/password-reset/request` | `{email}` → 永远 202 `{status:"accepted"}`（不暴露账户是否存在）；已验证账户收到重设令牌，未验证账户收到认领令牌；按 IP、按账户限速；503 `email_sender_not_configured` |
| POST `/auth/password-reset` | `{token, password}`：重设令牌（也是 `create-operator` / `verify-email` 命令输出的设密令牌）→ 设新密码、**删除该账户全部会话**，返回新会话；API Key 保留（机器人依赖它们），请在 `/api-keys` 自查。认领令牌也可用于此接口 |
| POST `/auth/claim` | `{token, password, name?}`：认领令牌 → 为持令牌者新建一个**已验证**账户（新密码、新个人组织）；原来占着这个邮箱的未验证账户被停用：邮箱改为 `claimed+<id>@invalid`、密码不可用、会话全删、API Key 全吊销、运营标志清除。400 `recovery_token_invalid` / `recovery_token_expired`；409（占用账户已被其本人验证） |
| POST `/auth/verify-email/resend` | 需要会话 → `{email_verified:false, verification, dev_verification_token?}`；409 `already_verified`；429 `rate_limited`（每用户每小时 5 封）；**503 `email_sender_not_configured`**（服务端没有配置邮件发送） |
| POST `/auth/login` | `{email, password}` → `{token, token_type, expires_at, user}`；401 `invalid_credentials`；429 `rate_limited` |
| POST `/auth/logout` | → `{logged_out}` |
| GET `/me` | → `{user, email_verified, platform_operator, auth, current_org_id, role, permissions[], organizations[{id,name,personal,role}]}` |
| POST `/org/members` | `{email, role: admin|developer|operator}`（owner 不能通过接口授予或修改；只有 owner 能授予 admin、修改 admin 的角色） |
| GET `/org/members` | `{members:[{user_id, email, name, role}]}` |
| DELETE `/org/members/{user_id}` | 修复新增：移除成员。owner 不能被移除（也不能自己退出）；owner/admin 可移除 developer/operator；只有 owner 能移除 admin；任何人可以移除自己（退出）。被移除成员创建的 API Key 立即失效 |
| POST `/api-keys` | `{name?, scopes[]}` → 201 `{id, name, prefix, scopes, org_id, key}`（**`key` 明文只返回这一次**）；只能用会话创建 |
| GET `/api-keys` | → `{api_keys:[{id,name,prefix,scopes,org_id,created_at,last_used_at,revoked}]}` |
| DELETE `/api-keys/{id}` | 吊销 |

API Key 的 scopes：`gateway`、`catalog:read`、`robots`、`wallet:read`、`usage:read`。Key 实际拥有的权限 = scopes ∩ 创建者当前角色的权限（创建者被降级或移出组织后，Key 立即收窄或失效；有测试 `test_demoting_creator_narrows_key_scopes`）。API Key 不能管理 Key、不能购买、不能访问 `/admin`；通过 Key 调用网关时检查的是**创建者**的邮箱是否已验证。

### 邮箱验证（修复 C-01）

- 注册后 `email_verified=false`。服务端生成一次性令牌（`nxv_` 前缀，只存 SHA-256，默认 24 小时过期，单次使用，重发作废旧令牌），通过 `EmailSender` 发给注册邮箱。令牌有用途：`verify` / `reset` / `claim`，互不通用。
- **复审修复 N-01（预注册劫持）**：第 01 轮修复里验证不需要登录，于是攻击者先用别人的邮箱（例如运营员邮箱）注册并设好密码，主人点开邮件里的令牌，验证的却是攻击者的账户，攻击者已有的会话随即成为运营员；主人重注册得到 409，也无法找回密码。现在：
  1. 验证必须在**令牌所属账户自己的登录会话**里提交；别人的会话或未登录一律失败。只有知道该账户密码的人能完成验证，而主人拿到的令牌对攻击者的账户无效。
  2. 验证成功后删除该用户其他会话、吊销验证前创建的全部 API Key（之后新建的 Key 正常）。
  3. 邮箱被未验证账户占着时，重新注册会向邮箱发**认领令牌**；持令牌者得到一个新的已验证账户，占用者的账户被停用。占用者再次触发注册不会作废主人已收到的认领令牌（认领令牌不互相替换，按邮箱每小时 5 封）。验证完成前，占用账户没有任何特权（不能购买、不能调用网关、不会成为运营员）。
  4. 找回密码：`/auth/password-reset/request` + `/auth/password-reset`，重设后吊销全部会话。
- **未验证邮箱的用户可以注册、登录、浏览**（目录、钱包、用量、设备群、创建 API Key），但**购买点数/模型授权、发起支付、调用网关（`/gateway/infer`、开会话）都需要已验证邮箱**，否则 403 `email_unverified`。关闭已有会话不需要。
- 发送器（`mailer.py`）：
  - `log`：**仅 dev/test，不发送任何邮件**。消息记录在内存（`app.state.ctx.email_sender.outbox`）和 `neuxmind_cloud.mail` 日志里；为了让本地工具和测试能走完 HTTP 流程，注册/重发响应会带 `dev_verification_token`，并标注 `verification.simulated: true`。在 prod/staging 配置 `log` 会直接启动失败。dev/test 默认就是它。
  - `smtp`：`smtplib` + STARTTLS（`NEUXMIND_SMTP_*`，密码放在 `NEUXMIND_SMTP_PASSWORD_REF` 指定的环境变量里）。**只用 mock 测过，没有在真实邮件服务器上验证。**
  - 不配置（prod/staging 默认）：注册照常，`verification.status="unavailable"`；重发、找回密码接口返回 503 `email_sender_not_configured`。服务端运营人员核实身份后可执行 `python3 -m neuxmind_cloud verify-email <email> [--force]`：它**同时把该账户密码设为不可用**、删除会话、吊销 API Key（账户有会话或 Key 时必须加 `--force`），并输出一次性设密令牌，交给核实过的主人——因为账户的密码可能是别人设的。

### 钱包与用量
| GET `/wallet` | `{org_id, unit:"credits", balance, reserved, available}` |
|---|---|
| GET `/wallet/ledger?limit=` | `{entries:[{id, delta, kind, reference, reason, actor, detail, created_at}]}` |
| GET `/wallet/reservations?status=` | `{reservations:[{id, amount, status, purpose, model_id, pricing, settled_amount, evidence_ref, …}]}` |
| GET `/usage?model=&limit=` | `{usage:[{id, request_id, model, status, reservation_id, session_id, usage:{prompt_tokens, completion_tokens, total_tokens}, credits, upstream_ref, simulated, error, created_at}], totals}` |

### 能力标准
| GET `/capabilities/registry` | 不需要认证，原样返回 `capabilities.registry()` |
|---|---|

### 模型市场
| 方法 路径 | 说明 |
|---|---|
| GET `/catalog/models?kind=&source=&capability=&robot_id=&require_level=` | `{models:[ModelView]}`；`capability` 接受标准 ID 或旧别名；`require_level=static\|dry`（需同时给 `robot_id`，否则 400 `robot_required`）见下文「按一致性级别过滤」 |
| POST `/catalog/models/compatible` | `{manifest: robot.adapter.v2}` 或 `{robot_id}`，可选 `require_level` → `{instance_id, capabilities[], robot_id?, models[]}`；带 `require_level` 时另有 `require_level, usable_capabilities[], not_verified[], conformance`（摘要简表） |
| GET `/catalog/models/{id}` | `ModelView` |
| GET `/catalog/models/{id}/package` | 需要授权 → `{model_id, version, ref, sha256, runtime, install:{capability:"model.install", args}}` |
| GET `/catalog/credit-packs` | `{credit_packs:[{id, name, credits, price_minor, currency}]}` |
| GET `/entitlements` | `{entitlements:[{id, model_id, order_id, created_at}]}` |

`ModelView = {id, name, version, source: native|open|closed, kind: llm|vla|vision|speech|policy, description, license, pricing:{type: per_1k_tokens|per_call|license_once|free, credits}, requires_capabilities[], families[], gateway: bool, status, package?:{sha256, runtime}, entitled: bool}`。`upstream` 和包的 `ref` 不会出现在公开视图里。

兼容判定（`catalog.compatible_with`）：`requires_capabilities ⊆ manifest 能力集合`（集合用 `spec.manifest.capability_ids` 算）；`families` 非空时机器人的 family 必须在其中；纯端侧包（没有上游）还要求 `package.runtime ∈ manifest.model_runtimes`。

### 购买与支付
| 方法 路径 | 说明 |
|---|---|
| POST `/purchases` | `{kind:"credit_pack", pack_id}` → 201 `{order}`（状态 pending，**不入账**）；`{kind:"model_license", model_id}` → 201 `{order(provider:"wallet", status:"paid"), entitlement}`，同一事务里从钱包扣点（license_once 扣 `pricing.credits`，其他计价方式为 0 点）。402 `insufficient_credits`，409 `already_entitled`。只能用会话购买 |
| GET `/purchases`、`/purchases/{id}` | 订单 |
| POST `/payments/{provider}/checkout` | `{order_id}` → manual：`{status:"awaiting_manual_confirmation", instructions}`；sandbox：`{checkout_url, simulated:true, notice}`；stripe/alipay/wechatpay：503 `provider_not_configured`；prod 环境下的 sandbox：403 `sandbox_disabled` |
| POST `/payments/{provider}/webhook` | 请求头 `X-NeuxMind-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`，时间容差 300 秒；内容 `{event_id, type:"payment.succeeded", order_id, amount_minor, currency, provider_reference?}` → `{received, credited}` / `{received, duplicate:true}` |

### 推理网关
| 方法 路径 | 说明 |
|---|---|
| POST `/gateway/infer` | `{model, input: string | [{role, content}], max_tokens, request_id?}` → `{id, request_id, model, output:{text}, usage:{prompt_tokens, completion_tokens, total_tokens}, credits_charged, credits_reserved, reservation_id, simulated}`。错误：403 `entitlement_required`、402 `insufficient_credits`、502 `upstream_error`（已释放，未扣费）、502 `upstream_unknown` / `usage_exceeds_reservation`（预留保留待核对，带 `reservation_id`）、409 `request_id_reused` / `request_not_replayable` |
| POST `/gateway/sessions` | `{request_id, model, max_tokens, max_units?, max_minutes?(1–120, 默认 15)}` → 201 `{session_id, base_url, access_token, expires_at, max_tokens, reservation_id, reserved_credits, status}`；相同 request_id 重放时返回同一会话，`access_token: null` |
| POST `{base_url}/chat/completions`、`{base_url}/responses` | 认证头 `Authorization: Bearer <access_token>`；OpenAI 兼容请求，**只接受白名单参数**（见下）；输出上限由网关按剩余预算写入。非流式：响应为上游 JSON 加 `neuxmind:{session_id, used_tokens, max_tokens, output_limit, simulated}`。`stream: true`：200 `text/event-stream`，见「会话流式」。错误（JSON，流开始之前）：400 `parameter_not_allowed`（带 `parameters`，嵌套字段写成 `tools[0].function.x` 这样的路径）/ `n_unsupported` / `content_not_allowed` / `tool_not_allowed` / `invalid_parameter`；402 `session_budget_exhausted`；409 `session_busy`（该会话已有在途调用）/ `session_overage_locked` / `session_closed`；502 `upstream_error` / `upstream_unknown` / `usage_exceeds_reservation`（不返回输出） |
| GET `/gateway/sessions/{id}` | 会话状态与用量（含 `inflight_tokens`、`overage_calls`、`overage_tokens`、`close_result`） |
| POST `/gateway/sessions/{id}/close` | `{request_id}` → `{status:"closed", usage_tokens, usage_units, usage_evidence_ref, credits_charged, metering_status:"settled", overage_calls, overage_tokens}`，或 `{status:"reconciliation_required", credits_charged:null}`；幂等；过期未关闭的会话由清理任务自动关闭（`close_request_id="auto:expired"`） |

#### 会话调用参数白名单（修复 C-02，`session_params.py`）

不在下表里的参数一律 400 `parameter_not_allowed`（不会悄悄丢掉，以免客户端以为生效了）。

| 参数 | chat | responses | 处理 | 理由 |
|---|---|---|---|---|
| `messages` / `input` | ✓ | ✓ | 只接受文本：chat 的 content 为字符串或 `text`/`refusal` 片段，可带函数 `tool_calls`、`tool` 角色；responses 的 input 为字符串或 message / `function_call` / `function_call_output` 项 | 预留按请求字节数算 prompt 上界；图片、音频、文件的 token 成本与字节数无关 |
| `model` | ✓ | ✓ | 接受但忽略，网关写入模型卡的上游模型名 | — |
| `max_tokens`、`max_completion_tokens` | ✓ | — | 只能调小；网关写入 `min(客户端值, 剩余预算 − 输入上界)`，字段名按模型卡 `upstream.output_param`（`max_tokens` 默认 / `max_completion_tokens` / `both`），另一个字段不下发 | 审查复现：只覆盖 `max_tokens` 时 `max_completion_tokens` 绕过上限 |
| `max_output_tokens` | — | ✓ | 同上，网关写入 | — |
| `n` | ✓ | — | 只能缺省或 1，下发时固定 `n=1` | 审查复现：`n=50` 换来 50 倍用量 |
| `stream` | ✓ | ✓ | 布尔；`true` 走 SSE 流式（第 02 轮）。chat 由网关写入 `stream_options:{include_usage:true}`，客户端不能传 `stream_options` | 计量需要上游在流里报告 usage |
| `temperature`、`top_p` | ✓ | ✓ | 数值范围校验 | 不影响用量 |
| `stop`、`seed`、`presence_penalty`、`frequency_penalty`、`logprobs`、`top_logprobs`、`reasoning_effort` | ✓ | — | 类型/范围校验 | 不增加计费项；推理 token 计入 completion，受输出上限约束 |
| `response_format` | ✓ | — | 严格结构（N-09）：`{type:text}` / `{type:json_object}` / `{type:json_schema, json_schema:{name, description?, schema?, strict?}}` | 同上 |
| `instructions`、`reasoning{effort,summary}` | — | ✓ | 类型校验；`instructions` 字节计入输入上界 | 同上 |
| `text` | — | ✓ | 严格结构（N-09）：`{format?, verbosity?: low\|medium\|high}`，`format` 为 `{type:text}` / `{type:json_object}` / `{type:json_schema, name, description?, schema?, strict?}` | 同上 |
| `tools`、`tool_choice`、`parallel_tool_calls` | ✓ | ✓ | 只允许函数工具，嵌套字段严格白名单（N-09，见下）；工具定义的字节 + 每个工具 64 计入输入上界 | 内置工具（web_search、file_search、code_interpreter…）供应商另行计费 |
| `user` | ✓ | ✓ | ≤256 字符 | — |
| `store` | — | ✓ | 第 03 轮：**只接受 `false`**，其他值 400 `parameter_not_allowed` | `true` 会在平台的上游账户下保存请求/响应（并配合被拒绝的 `previous_response_id` 使用） |
| `include` | — | ✓ | 第 03 轮：只接受 `["reasoning.encrypted_content"]`，其他值 400 `parameter_not_allowed` | 只返回本次响应自己的加密推理（其 token 已是输出 token）；其他取值会返回存储的文件、检索结果、图片地址或 logprobs |
| `prompt_cache_key` | — | ✓ | 第 03 轮：1–256 字符；网关改写为 `nx-` + sha256(org_id ‖ key) 前 40 位后下发；客户端不带时也写入（key 取 `default`，审查 Q3-15）；responses 输入上界恒 +64 | 平台一个上游账户服务所有租户；按组织重键后，带键与不带键的请求都只会命中本组织的缓存路由（前提是供应商按 `prompt_cache_key` 隔离路由） |
| `client_metadata` | — | ✓ | 第 03 轮：≤32 项、键 ≤64 字符、值为 ≤4096 字符的字符串；**校验后丢弃，不下发、不计入输入上界** | Codex 的客户端遥测（安装/线程/轮次 ID）；不影响生成，也不把设备标识交给上游 |
| 拒绝：`metadata`、`service_tier`、`logit_bias`、`audio`、`modalities`、`prediction`、`stream_options`、`web_search_options`、`previous_response_id`、`conversation`、`prompt`、`background`、`truncation` 等 | | | 400 | 会改变价格档位、引入输入字节以外的 token（上游存储的历史、模板、文件）、或在平台账户下存数据 |

输入上界 = 转发体（去掉 model/stream/n/输出上限/`client_metadata`）JSON 字节数 + 64 ×（输入项数 + 函数工具数，命名空间内的函数逐个计）（responses 另 + 64：网关写入的 `prompt_cache_key`）。responses `input` 最多 4096 项（审查 Q3-07，原 1000）。

**嵌套字段白名单（复审 N-09）**：第 01 轮只检查了顶层参数，`tool_choice`（对象）和消息里的 `tool_calls[]` 等嵌套结构原样转发，例如 `tool_choice.max_completion_tokens` 能到达上游。现在每一层都按下表重建校验，多出的键一律 400 `parameter_not_allowed`（`parameters` 里给出完整路径），类型不对 400 `invalid_parameter`；JSON Schema（`parameters` / `schema`）本身是自由结构，只要求是对象，字节计入输入上界。

| 位置 | 允许的键 |
|---|---|
| chat `tool_choice` | `"none"`/`"auto"`/`"required"`，或 `{type:"function", function:{name}}` |
| responses `tool_choice` | 同上字符串，或 `{type:"function", name}` |
| chat `tools[]` | `{type:"function", function:{name, description?, parameters?, strict?}}` |
| responses `tools[]` | `{type:"function", name, description?, parameters?, strict?}`；第 03 轮：或命名空间 `{type:"namespace", name, description?, tools:[1–128 个上面的函数工具]}`（不可嵌套；顶层与命名空间内的函数合计 ≤128） |
| chat `messages[]` | `role, content, name?, tool_calls?, tool_call_id?, refusal?`；`name` 为函数名格式 |
| chat `messages[].tool_calls[]` | `{id, type:"function", function:{name, arguments: string}}`（全部必填） |
| 内容片段 | `text`/`input_text`：`{type, text}`；`output_text`：`{type, text, annotations?: []}`（只接受空列表）；`refusal`：`{type, refusal}` |
| responses `input[]` | message `{type?, role, content, id?, status?}`；`function_call` `{type, call_id, name, arguments, id?, status?, namespace?}`；`function_call_output` `{type, call_id, output, id?, status?}`，`output` 为字符串或只含 `input_text` 片段的列表（第 03 轮）；`reasoning` `{type, id?, summary?:[{type:"summary_text", text}], content?:[{type:"reasoning_text", text}]\|null, encrypted_content?, status?}`（第 03 轮，工具循环中回传上一响应的推理项；全部字节计入输入上界，若供应商按高于上界计费则按 ADR-012 超额处理）；`status` ∈ in_progress/completed/incomplete |

函数名：1–64 个字母、数字或 `_-.:`。

#### 会话流式（第 02 轮，契约 §4.3，`streaming.py`）

请求：在会话调用的请求体里加 `"stream": true`（chat 与 responses 都支持），其余规则（白名单、输出上限、预算）与非流式完全相同。**流开始之前**的一切错误（参数、预算、`session_busy`、上游 4xx/5xx/拒连）仍是普通 JSON 错误和 HTTP 状态码；一旦返回 200，后面的结果全部用 SSE 事件表达。

响应：`200`，`Content-Type: text/event-stream; charset=utf-8`，响应头 `X-NeuxMind-Session`、`X-NeuxMind-Output-Limit`（本次服务端输出上限）、`X-NeuxMind-Simulated`（`true` = Echo 测试替身）。

1. **上游事件透传**（重新按 SSE 编码；注释行、`id:`、`retry:` 被去掉）。**网关保留命名空间（审查 Q2-03）**：
   - 上游 `event: error` → 改名为 **`event: upstream.error`** 转发（data 原样）；
   - 上游事件名以 `neuxmind.` 开头（不区分大小写），或 data 的 JSON `type` 以 `neuxmind.` 开头 → **丢弃**，不转发也不计量（证据里记录丢弃数）；
   - 行结束符按 WHATWG 规则识别 `\r\n`、`\n`、单独的 `\r`（含跨读取块的 CRLF）；转发时只用 `\n`，任何字段值里都不会出现 CR/LF，因此上游无法注入新的字段或事件。
   - 结果：客户端看到的 `event: error` 与 `event: neuxmind.metering` 只可能来自网关，且恰好一个、在最后。
   具体透传内容：
   - chat：`data: {chat.completion.chunk}` …… 最后一个是网关要求的 usage 块（`choices: []`、`usage`），然后 `data: [DONE]`；
   - responses：`event: response.created` …… `event: response.output_text.delta` …… `event: response.completed`（或 `incomplete` / `failed`，`response.usage` 为计量依据）。
2. **上游的终止事件（responses `response.completed` / `incomplete` / `failed`，chat `data: [DONE]`）在结算完成之后才转发**（第 03 轮）：工具循环客户端（Codex app-server）一读到它就发下一次调用；先转发会留下一个窗口，下一次调用撞上 409 `session_busy`（串行化，ADR-016）。顺序不变，只是时间点移到结算之后。
3. **网关自己的终止事件，恰好一个，永远是流的最后一个事件**，之后连接关闭：
   - 成功：`event: neuxmind.metering`，`data: {"type":"neuxmind.metering","status":"succeeded", ...}`；
   - 失败：`event: error`，`data: {"type":"error","code":"<code>","message":"…","error":{"type":"neuxmind_gateway_error","code":"<code>","message":"…"},"neuxmind":{计量}}`（同时满足普通 SSE 客户端读 `code`、OpenAI SDK 读 `error.code` 抛 APIError）。
   - 计量对象：`{type, session_id, status: succeeded|overage|reconciliation_required|call_marked_lost, call_tokens（本次计入会话的 token；call_marked_lost 时为 null）, used_tokens, max_tokens, output_limit, hold_tokens, usage（上游 usage 或 null）, output_lower_bound, upstream_ref, client_disconnected, simulated}`。
   - chat 的 `neuxmind.metering` 在 `[DONE]` **之后**：标准 OpenAI 客户端读到 `[DONE]` 就停，不受影响；NeuxMind SDK 读到流结束。

流内错误码（都在 `event: error` 里）：

| code | 何时 | 本次计入会话 | 会话关闭时 |
|---|---|---|---|
| `output_limit_reached` | 已转发输出的下界即将超过本次输出上限（上游不遵守上限），网关截断、断开上游 | hold（预留上限） | `reconciliation_required` |
| `upstream_interrupted` | usage 之前上游连接中断 / 超时 | hold | `reconciliation_required` |
| `upstream_stream_error` | 上游在流里发了 error 事件且没有 usage | hold | `reconciliation_required` |
| `usage_missing` | 上游流正常结束但没有 usage | hold | `reconciliation_required` |
| `stream_duration_exceeded` | 超过最长流式时长（见配置） | hold | `reconciliation_required` |
| `usage_exceeds_reservation` | 上游 usage 超过本次 hold（输出已送达，流式无法撤回） | hold，超出部分进 `overage_records`，会话锁定 | 正常结算（ADR-012） |
| `call_marked_lost` | 清理任务已把这次调用判定为丢失（正常不会发生：流式时长上限保证流先结束） | 不再计量 | 由清理任务的结果决定 |
| `internal_error` | 网关计量失败（调用留在 pending，由清理任务转核对） | — | — |

**计量规则**：

- 以上游 usage 事件为准；同时统计**已转发输出的下界** = 转发出去的非空输出增量个数（chat：`delta.content/refusal/reasoning_content/reasoning` 每个非空字段、每个带非空 name/arguments 的 `tool_calls` 片段各计 1；responses：凡是 `type` 以 `.delta` 结尾且 `delta` 为非空字符串的事件各计 1，含 `reasoning_summary_text.delta` 等（审查 Q2-06））。依据：OpenAI 及主流兼容服务按 token 逐块输出，非空增量至少对应 1 个 token。本次 completion = max(上游 completion, 下界)。
- **截断**：转发一个事件之前，若下界（或流中途报告的 completion）将超过本次输出上限，就不转发它，断开上游，发送 `output_limit_reached` 后结束。遵守上限的上游永远不会触发（下界 ≤ 真实 token ≤ 上限）。
- **usage 之前中断**（含截断、超时、上游错误、无 usage）：本次调用按 **hold（= 输入上界 + 输出上限）** 计入会话 `used_tokens`，并计为结果未知（`unknown_calls`）→ 关闭时整笔预留转 `reconciliation_required` 由运营核对（ADR-012）。计入 hold 保证剩余预算不会被重复使用；扣费永远不超过预留。
- **客户端中途断开**：网关停止转发，但**继续读完上游**（仍然计量、仍然会截断、仍受时长上限约束）拿到 usage，按实际用量精确结算，审计 `gateway.stream_client_disconnected`；如果读完之前上游也断了，按上一条处理。客户端 `stream_idle_seconds`（默认 30 s）不读数据也按断开处理。排空期间会话仍然"在途"，新调用 409 `session_busy`、关闭 409 `session_busy`。
- **不持锁**：预留事务与结算事务都很短；流式期间由独立的生产线程读上游，不持有任何数据库事务或钱包锁；通道有界（64 个事件），客户端读得慢时反压到上游读取，不占用线程池。
- **最长时长**：`stream_max_seconds`（默认 0 = 自动取 `stale_call_seconds − upstream_timeout_seconds − 30`，至少 10 s；默认配置下 510 s），保证流先于清理任务的"丢失判定"结束。**按墙钟执行（审查 Q2-02）**：每个流有一个看门狗线程，到点即中断上游连接（`socket.shutdown`，阻塞中的读立即返回），与上游是否发数据事件无关；另外每读到一行（保活注释、空行都算）也检查一次绝对截止时间。注释行算作"活动"，但不会延长总时长。
- **上游静默上限**：连续 `upstream_timeout_seconds` 收不到任何一行（连保活注释都没有）→ 中断，按 `upstream_interrupted` 处理。
- **OpenAI 兼容上游**：请求带 `Accept: text/event-stream`；用 `read1()` 读取（`readline()` 会把 chunked 断流的 `IncompleteRead` 吞成正常 EOF）；单行上限 1 MB、单事件上限 4 MB，超出按中断处理。
- **Echo 测试替身**（仅 dev/test）也支持流式：每个词一个增量，**每个事件都带 `"simulated": true`**，chat 块的 `object` 为 `simulated.echo.chunk`，计量事件 `simulated: true`，响应头 `X-NeuxMind-Simulated: true`。非流式 `/responses` 返回 Responses 形状（`object:"response"`、`output[].content[].type:"output_text"`、`usage.input_tokens/output_tokens`），openai 库的 `output_text` 可用。新增行为 `stream_cut`（usage 前断流）、`usage_overrun`（增量不超上限但 usage 远超）和 `stream_delay`（每个事件延迟，测试时长上限用）。第 03 轮新增 `tools`（只用于流式 `/responses`）：完整的 Responses 事件流与输出项——输入里还没有 `function_call_output` 且提供了函数工具时返回一个 `function_call`（函数名取模型卡 `upstream.tool_name`，默认第一个顶层函数工具；参数取 `upstream.tool_arguments`，默认 `{}`），否则返回一条回显最后输入文本的 assistant 消息；用于桌面端工程助手经网关跑真实工具循环的测试。

#### 同一会话的在途调用（复审 N-05）

**选择：串行化**——同一会话同一时刻至多一个在途调用（流式与非流式都算，流式直到结算完成），并发的第二个调用在预留事务里（钱包锁内）被拒绝：409 `session_busy`，不会到达上游。理由：

1. 超额锁定（ADR-012）只拦"之后"的调用；串行化后"之前"与"之后"就是全部，**每个会话最多只有一次调用可能超出其 hold**，与客户端并发数无关。若改为"每个在途调用单独预留"，预算本身不会超（每个 hold 都从剩余额度里扣），但 N 个并发调用仍可各自超额一次，平台可能承担 N 倍的未收超额。
2. 会话的用途是桌面工程助手这类"一问一答 / 工具循环"，天然串行，串行化不损失功能；需要并行的客户端可以开多个会话（每个会话各自预留）。
3. 实现简单、可证明：判定点只有一个（`inflight_tokens > 0`，在钱包锁内读取），SQLite（`BEGIN IMMEDIATE`）与 PostgreSQL（钱包行 `FOR UPDATE`）上都由同一把锁保证原子性。测试 `test_concurrent_calls_in_one_session_cannot_exceed_the_budget`：6 个并发调用、上游每次都超额报告 → 只有 1 个到达上游、5 个 409、1 条超额记录、会话用量 ≤ 预算（两种数据库都跑）。

和桌面端协议的对应关系（`docs/ENGINEERING_ENGINE.md`「模型与费用」）：`/v1/agent-sessions` ⇒ `/api/v1/gateway/sessions`，请求和响应字段名相同（`request_id, model, max_tokens, max_units, max_minutes` → `session_id, base_url, access_token`；close → `status, usage_units, usage_evidence_ref`）。不同点：桌面端用自己的账本预留和结算，云端版本在服务端预留和结算，桌面端切到云端后应当不再自己记账。

**第 03 轮（契约 §5.3）**：桌面端已切到云端会话——工程助手的“云平台模型”经 `POST /gateway/sessions` 开会话、把**按桌面端配置的云地址拼出的** `base_url` 与会话令牌交给内置工程运行时（Codex 0.146.1 app-server，Responses 流式 + 函数工具），结束（成功、停止、失败）时 close；本地账本完全不参与。Codex 实际发送的请求体录制在 `tests/fixtures/codex-0.146.1-responses.json`，`tests/test_codex_compat.py` 验证全部被白名单接受、下发内容符合上表、原有安全拒绝保持。

### 设备群
| POST `/robots` | `{manifest: robot.adapter.v2}`（用 `spec.manifest.validate_manifest_v2` 校验）→ 201 `{id, instance_id, name, family, simulated, manifest_digest, capabilities[], model_runtimes[], manifest, conformance}`；同组织内同一 instance_id 再次登记会覆盖更新（已保存的一致性摘要保留，但 manifest 变了就标 `stale`） |
|---|---|
| GET `/robots`、GET/DELETE `/robots/{id}` | 按组织隔离，访问其他组织的机器人返回 404。列表里 `conformance` 是简表 `{conformance_id, level, static_ok, dry_ok, motion_ok, simulated, evidence_kind, generated_at, digest_verified, stale}`；`GET /robots/{id}` 是完整摘要（见下），没有报告时为 `null` |
| POST `/robots/{id}/conformance` | （第 04 轮，契约 §6.4）`{report: conformance.report.v1}` → `{robot_id, conformance}`，保存为该机器人的**最新一致性报告摘要**（完整报告与 `cases` 不保存）。需要 `robots` 权限 |
| POST `/robots/{id}/skills/check` | （第 04 轮）`{skill: skill.package.v1 元数据（files 会被忽略）, require_level?}` → `{robot_id, skill, require_level, usable_capabilities[], not_verified[], compatible, missing[], violations[], optional_missing[]}`（`spec.manifest.check_compatibility`）；需要 `robots` 权限；`invalid_skill` 400 |

#### 一致性报告摘要（`conformance.py`）

运行时的一致性测试（契约 §6.1）产出 `conformance.report.v1`；云端**不运行测试，也无法复核证据**，只校验报告的格式与内部一致性、把它绑定到登记的机器人，并保存摘要。**摘要只供参考**：报告由适配器所在进程自行产生（套件与适配器同进程，适配器代码可以篡改判定，审查 Q4-13），再由持有 `robots` 权限的人上传，云端无法证明它真实——摘要因此标 `self_reported: true`，免责声明也写明这一点：

`{conformance_id, report_manifest_digest, digest_verified, level, static_ok, dry_ok, motion_ok, simulated, evidence_kind, field_acceptance:false, hardware_authorized, instance_id, generated_at, runtime_version, spec_version, aborted, final_latched, levels.{static,dry,motion}.{requested, ok, passed, failed, skipped, blocked}, capabilities.{<能力 ID>}.{declared, levels.{<级别>}.{status: passed|failed|skipped, skip_kind?, reason?（≤300 字）}}, manifest_digest, uploaded_at, uploaded_by, self_reported:true, stale, disclaimer}`

| 拒绝 | 错误码 |
|---|---|
| 不是对象、`schema` ≠ `conformance.report.v1`、`conformance_id` 格式、`level` 非法、`*_ok` 类型/与级别不符（未请求的级别必须为 null）、某级别 ok 但更低级别不 ok、`levels.*` 与 `*_ok` 不一致或标 ok 却有失败/受阻、`capabilities` 为空/超过 200 项/有重复/未知能力 ID/旧别名、每项能力的 `levels` 不恰好是已请求的级别、状态或 `skip_kind` 非法、**未声明的能力却不是 skipped**、级别标 ok 却有能力 failed/blocked、`simulated` 与 `evidence_kind` 不一致（仿真 ⇔ `simulation`）、motion 报告却 `hardware_authorized=false` | 400 `invalid_conformance_report` |
| `evidence_kind: "device_run"`（一致性报告不是现场运行证据） | 400 `device_run_refused` |
| `field_acceptance` 不是 `false`（含缺失） | 400 `field_acceptance_refused` |
| `manifest_digest` 不是 64 位小写十六进制；`generated_at` 不是 `YYYY-MM-DDTHH:MM:SSZ` 或晚于服务器时间 5 分钟以上（防止用未来时间戳让之后的真实报告都被判"过期"，审查 Q4-17） | 400 `invalid_conformance_report` |
| 报告带的 `manifest_digest` ≠ 登记记录的 `manifest_digest`（对另一份 manifest 运行的报告，含 manifest 变更后重传旧报告，审查 Q4-12）；附 `report_manifest_digest`、`registered_manifest_digest` | 409 `conformance_manifest_mismatch` |
| `robot.instance_id` 或 `simulated` 与登记的机器人不同 | 409 `conformance_robot_mismatch` |
| 报告里声明的能力集合 ≠ 登记 manifest 的能力集合（附 `only_in_report`、`only_in_manifest`） | 409 `conformance_manifest_mismatch` |
| 已保存同一 manifest 的、`generated_at` 更新的另一份报告（同一 `conformance_id` 重传幂等；摘要已 stale 时任何报告都可替换） | 409 `conformance_outdated` |

审计：`robot.conformance`（`conformance_id`、级别与结论、`simulated`）。上传者是持有该组织 `robots` 权限的人（与登记机器人的人相同）；报告是自报的，云端在摘要中标 `self_reported: true` 并附 `disclaimer`：**一致性报告摘要不等于现场验收证据**（ADR-011）。

#### 按一致性级别过滤（`require_level`）

`require_level` 取 `static` 或 `dry`（其他值 400 `invalid_level`）。某能力在级别 L **可用** ⇔ 最新摘要未过期（`manifest_digest` 等于登记的 manifest）、报告自带的 `manifest_digest` 已核对（`digest_verified`；不带它的旧运行时报告会保存，但不用于 `require_level`）、摘要的级别 ≥ L、且该能力在 L 及所有更低级别的结论都是 `passed`（"跳过（不适用）"**不算通过**）。运行时 b0e53c7 起 `legged.posture` 与 `model.install` 都有不运动的 dry 用例（姿态：非标准值与锁存期间被拒；安装：拒绝非法请求），通过后即算可用——`model.install` 的 dry 通过只说明会拒绝非法请求，不说明能成功安装（审查 Q4-22，是否收紧留给契约决定）。`safety.stop` 不可用时一律视为没有可用能力（失败即关闭）；`model.install` 不可用时 `model_runtimes` 视为空（纯端侧包不再兼容）。然后用这份"缩减后的 manifest"走原有的 `catalog.compatible_with` / `spec.check_compatibility`。
没有摘要、摘要过期或级别不够 → 409 `conformance_required`（附提示：`nx robot conformance <robot> --level dry --upload`）。`/catalog/models/compatible` 只给 `manifest` 时，必须能在本组织找到同 `instance_id` 且 manifest 完全相同（摘要相同）的登记记录，否则同样 409 `conformance_required`。

### 运营（`/admin/*`，需要登录会话且是平台运营员）
`GET/POST /admin/models`、`PATCH /admin/models/{id}`（改价不影响已有预留的价格快照和已结算的账本）、`POST /admin/models/{id}/status {status: published|unlisted}`、`POST /admin/credit-packs`、`POST /admin/wallets/{org_id}/adjust {delta, reason, reference?}`、`GET /admin/reservations?status=&older_than_seconds=`（默认 reconciliation_required；`status=held|settled|released|all`；每项带 `age_seconds`）、`POST /admin/reservations/{id}/resolve {action: settle|release, credits?, evidence_ref?, reason}`（只处理 reconciliation_required，held 返回 409 `reservation_in_flight`）、`POST /admin/maintenance/sweep`（立即清理，返回 `{lost_calls, sessions_closed, orphan_holds}`）、`GET /admin/overages?status=open|waived|charged|all`、`POST /admin/overages/{id}/resolve {action: waive|charge, credits?, reason}`、`GET /admin/orders?status=`、`POST /admin/orders/{id}/confirm {payment_reference, reason}`（manual 渠道）、`GET /admin/audit?action=`。

## 计费与支付流程

```
token 套餐（法币）
  用户 POST /purchases {credit_pack} ─► 订单 pending（不入账）
  用户 POST /payments/<p>/checkout
     ├─ manual    ─► awaiting_manual_confirmation ─► 运营 /admin/orders/{id}/confirm(凭证号, 原因)
     ├─ sandbox   ─► （仅 dev/test）签名 webhook
     └─ stripe/alipay/wechatpay ─► 503 provider_not_configured（不伪造成功）
  入账：锁钱包 → 核对金额与币种 → 账本 +credits（reference=order:<id>，唯一）→ 订单 paid
        重复 webhook：同一 event_id 直接返回 duplicate；不同 event_id 但订单已 paid → 不重复入账

模型授权（钱包点数）
  POST /purchases {model_license} ─► 锁钱包 → 可用额度 ≥ 价格？→ 账本 −price → 写入授权（同一事务）

网关调用（/gateway/infer）
  T1 锁钱包：已授权？ → 上限 = max_tokens + 输入字节数 + 64×消息条数 → 预留 cost(上限) → usage 记为 pending
  （不持锁）调用上游
  T2 锁钱包：
     成功且 cost(实际用量) ≤ 预留     → 结算实际用量（证据 = 上游响应 id），差额释放
     请求未发出（DNS 失败/拒绝连接/TLS 证书错误/缺运营凭据）或上游 4xx（408 除外，含 429）
                                      → 释放，不扣费（upstream_error）
     5xx / 408 / 超时 / 连接中断 / 缺 usage / 缺 id
                                      → reconciliation_required，预留保留（upstream_unknown）
     实际用量超过预留（修复，新计费 ADR 待总指挥编号）
                                      → 按预留上限结算（扣满预留），输出不返回，
                                        超出部分写 overage_records（open）待运营核对（usage_exceeds_reservation）
     预留已被运营处理（进程曾被判定丢失） → 不再记账，保留迟到的证据（reservation_already_resolved）
  运营 /admin/reservations/{id}/resolve 只处理 reconciliation_required（held 返回 409 reservation_in_flight）
  运营 /admin/overages/{id}/resolve：waive（免除）或 charge（≤ 记录的超额点数、≤ 可用额度，账本 reference = overage:<id>）

计量会话（/gateway/sessions）
  打开：预留 cost(max_tokens)，发放 nxgw_ 访问令牌（只存哈希），有过期时间
  每次调用：参数白名单 → 锁 → 已有在途调用？→ 409 session_busy（N-05）→ 剩余额度 = max − 已用；输出上限 = min(客户端值, 剩余 − 输入上界)，由网关写入；
            本次占用 hold = 输入上界 + 输出上限 → 调上游 →
            用量 ≤ hold：记入 used_tokens，返回结果
            用量 > hold：used_tokens += hold（按上限计），不返回结果，写 overage_records，会话锁定（之后调用 409）
            流式（stream:true）：生产线程透传 SSE 并计量（不持锁）→ 结算事务：usage ≤ hold 记实际用量；
                                 usage > hold 同上（输出已送达）；usage 之前中断/截断 → used_tokens += hold、计为未知
  关闭：有结果未知的调用 → reconciliation_required；否则按预留时的价格快照结算 used_tokens（≤ max_tokens ≤ 预留）
  清理（maintenance.py）：pending 超过 stale_call_seconds 的调用 → 判定丢失（infer：预留转核对；会话：释放占用、计为未知）；
            过期会话自动关闭；超过最长会话时长仍 held 的孤儿预留 → 转核对
```

为什么预留上限按"输入字节数"算：字节级 BPE 分词器（GPT / Llama / Qwen 系列）产生的 token 数不会多于输入字节数，所以字节数是 prompt token 数的安全上界。不满足这一点的分词器、或忽略输出上限的上游会触发"超额"：用户只付预留上限，输出不返回，超出部分进 `overage_records` 由运营决定免除或补扣（补扣不能超过可用额度）。平台从不在没有运营操作的情况下扣超过预留的点数。

## 安全模型

- 密码用 scrypt 加盐哈希；会话令牌、API Key、网关会话令牌都只存 SHA-256；API Key 明文只在创建时返回一次。
- 登录限速（修复 C-03），失败记录存在库里，计数与写入在 Postgres advisory lock（SQLite 下是 `BEGIN IMMEDIATE`）内完成，并发请求不能一起绕过：
  - （邮箱, IP）：窗口内 `NEUXMIND_LOGIN_MAX_FAILURES`（默认 8）次失败后，只拒绝这个组合。别人从自己的地址猜你的密码，锁不住你。
  - IP：窗口内 `NEUXMIND_LOGIN_IP_MAX_FAILURES`（默认 50）次失败（不论邮箱）后拒绝该 IP（撞库）。
  - 邮箱（所有 IP）：超过 `NEUXMIND_LOGIN_EMAIL_SOFT_LIMIT`（默认 20）次后不硬锁，而是逐步延迟（2^k 秒，上限 `NEUXMIND_LOGIN_MAX_DELAY_SECONDS`=30）：分布式猜测被压到每 30 秒最多一次，账户主人最多等 30 秒。429 带 `retry_after`。
  - 登录成功只清除本（邮箱, IP）的失败记录。未注册邮箱的处理方式相同，并用假哈希保持耗时一致。
  - IP 取连接对端地址；只有对端在 `NEUXMIND_TRUSTED_PROXIES` 里时才采信 `X-Forwarded-For`（取最右侧不受信任的一跳），客户端自己带的头不会生效。
- 注册按 IP 限速（默认每小时 20 次）；验证邮件每用户每小时 5 封。
- scrypt N=2^17（OWASP 建议值，约 128 MiB/次），同时最多 4 个哈希在计算，防止登录洪水耗尽内存；dev/test 可以用 `NEUXMIND_SCRYPT_LOG_N` 调低以加快测试，其他环境低于 17 拒绝启动。
- 多租户：每个业务查询都带 `org_id`；跨组织访问返回 404；`X-NeuxMind-Org` 要求是该组织成员。
- **平台运营员 = 邮箱已验证 且（管理命令授予 或 白名单且白名单已启用）**。
  - **生产环境推荐流程：`python3 -m neuxmind_cloud create-operator <email>`**。命令在服务器上直接创建一个已验证、已授予运营员的账户，密码不可用，并输出一次性设密令牌；把令牌通过可信渠道交给本人，本人 `POST /auth/password-reset` 设密码。这样运营员账户的密码从来没有经过公开注册，不存在"别人先注册、先设密码"的问题（复审 N-01）。
  - `grant-operator <email>` 只用于本人已在自己会话里验证过邮箱的已有账户；**未验证账户一律拒绝**，并打印该账户的公开注册时间和来源 IP 供核对。`revoke-operator` 撤销。
  - **白名单 `NEUXMIND_OPERATOR_EMAILS` 在 prod/staging 默认不生效**，必须显式设置 `NEUXMIND_OPERATOR_ALLOWLIST=1`（设置了白名单但没开开关时，启动日志会警告）。风险：白名单把"谁是运营员"交给了"谁能收到这个邮箱的邮件"——邮箱被盗、域名过期被抢注、邮件转发配置错误，都会直接变成平台最高权限；同时它鼓励先配地址、后注册，给预注册攻击留下窗口。dev/test 默认启用以便本地开发。
  - 已有数据库升级后所有用户（包括已有运营员）都是未验证状态，需要重新验证或 `verify-email`。
  - 注册时的角色/运营/验证字段一律忽略；owner 不能通过接口授予；`/admin` 只接受登录会话，不接受 API Key。
- 上游凭据不进数据库：模型卡里写凭据会被拒绝（`secret_in_card`），只能用 `credential_ref` 引用运营端的环境变量；公开视图不包含 upstream。非回环地址的上游必须用 HTTPS。
- Webhook：HMAC-SHA256、常量时间比较、时间戳容差；签名失败会记审计。
- 用户可见的上游错误只给通用文案（例如“上游服务暂不可用”）；凭据环境变量名、主机名、TLS 细节只写进审计 `detail` 和服务端日志（修复 C-06）。
- prod/staging 关闭 `/api/v1/docs` 和 `openapi.json`（修复 C-08）；prod/staging 上架 `echo` 测试替身直接 400 `upstream_not_allowed`（修复 C-09），调用时仍会再拒绝一次。
- 请求体上限（默认 1 MB）、CORS 白名单（默认不开）、统一错误格式、未处理异常返回 500 `internal_error`，不泄露堆栈。
- TLS 由前置的反向代理或负载均衡终止。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NEUXMIND_ENV` | `prod` | `prod` / `staging` / `dev` / `test`；只有 dev/test 能用 sandbox 和 echo |
| `DATABASE_URL` | `sqlite:///./neuxmind_cloud.sqlite3` | 例如 `postgresql+psycopg://user:pass@host/db` |
| `NEUXMIND_PUBLIC_BASE_URL` | `http://127.0.0.1:8800` | 用来生成会话 `base_url` 和 sandbox 跳转地址 |
| `NEUXMIND_OPERATOR_EMAILS` | 空 | 逗号分隔；只对**已验证邮箱**生效；prod/staging 下还需 `NEUXMIND_OPERATOR_ALLOWLIST=1` 才生效；生产不推荐，用 `create-operator` |
| `NEUXMIND_OPERATOR_ALLOWLIST` | dev/test 开，其他关 | `1` 显式启用 / `0` 显式关闭 |
| `NEUXMIND_EMAIL_SENDER` | dev/test：`log`；其他：不发送 | `log`（仅 dev/test）/ `smtp` / `none` |
| `NEUXMIND_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD_REF` / `_FROM` / `_STARTTLS` | 空 / 587 / 空 / 空 / 空 / 1 | `_PASSWORD_REF` 是存放密码的环境变量名 |
| `NEUXMIND_EMAIL_VERIFY_TTL_SECONDS` / `NEUXMIND_EMAIL_VERIFY_MAX_SENDS_PER_HOUR` | `86400` / `5` | |
| `NEUXMIND_TRUSTED_PROXIES` | 空 | 逗号分隔的反向代理 IP；只有它们的 `X-Forwarded-For` 被采信 |
| `NEUXMIND_LOGIN_IP_MAX_FAILURES` / `NEUXMIND_LOGIN_EMAIL_SOFT_LIMIT` / `NEUXMIND_LOGIN_MAX_DELAY_SECONDS` | `50` / `20` / `30` | |
| `NEUXMIND_REGISTER_IP_MAX_PER_HOUR` | `20` | |
| `NEUXMIND_MAINTENANCE_INTERVAL_SECONDS` | `60`（服务进程） | 后台清理间隔，0 关闭（网关请求上的惰性清理仍会运行） |
| `NEUXMIND_STALE_CALL_SECONDS` | `600` | pending 超过这么久的网关调用判定为丢失 |
| `NEUXMIND_STREAM_MAX_SECONDS` | `0`（自动） | 流式会话调用的最长时长；自动 = `STALE_CALL_SECONDS − UPSTREAM_TIMEOUT_SECONDS − 30`（≥10），显式值也不会超过它 |
| `NEUXMIND_STREAM_IDLE_SECONDS` | `30` | 客户端这么久不读数据按断开处理（网关继续排空上游并结算） |
| `NEUXMIND_SCRYPT_LOG_N` | `17` | 低于 17 只允许 dev/test |
| `NEUXMIND_CORS_ORIGINS` | 空 | 逗号分隔的允许来源 |
| `NEUXMIND_MAX_BODY_BYTES` | `1000000` | |
| `NEUXMIND_SESSION_MAX_BODY_BYTES` | `8000000` | 只用于会话模型调用（`/gateway/sessions/{id}/v1/responses`、`/chat/completions`；审查 Q3-07）：智能体运行时每次都发送完整历史。字节仍全部计入输入上界与会话预算。超限 413 `payload_too_large`，带 `max_bytes` 与“会话历史过长，请开启新会话继续” |
| `NEUXMIND_SESSION_TTL_SECONDS` | `43200` | |
| `NEUXMIND_LOGIN_MAX_FAILURES` / `NEUXMIND_LOGIN_WINDOW_SECONDS` | `8` / `60` | 前者现在是每（邮箱, IP）组合的上限 |
| `NEUXMIND_WEBHOOK_SECRET_<PROVIDER>` | 空 | 例如 `NEUXMIND_WEBHOOK_SECRET_SANDBOX`；sandbox 没有密钥时不可用 |
| `NEUXMIND_WEBHOOK_TOLERANCE_SECONDS` | `300` | |
| `NEUXMIND_UPSTREAM_TIMEOUT_SECONDS` | `60` | |
| `NEUXMIND_ALLOW_INSECURE_UPSTREAM` | 不设 | 设为 `1` 且环境是 dev/test 时，允许非回环的 http 上游 |
| `NEUXMIND_SPEC_PATH` | 仓库里的 `spec/` | `neuxmind_spec` 所在目录 |
| 模型卡 `credential_ref` 指向的变量 | — | 上游 API Key，只在运营端设置 |

## 部署

见 `cloud/Dockerfile`（在仓库根目录构建：`docker build -f cloud/Dockerfile -t neuxmind-cloud .`）。生产环境要用 PostgreSQL（安装 `psycopg[binary]`），前面放 TLS 反向代理并配置 `NEUXMIND_TRUSTED_PROXIES`，配置 SMTP（否则用户只能由运营 `verify-email`），**不要配置 `NEUXMIND_OPERATOR_EMAILS`**，运营员用 `create-operator` 命令创建。表在启动时用 `create_all` 自动创建，也可以执行 `python3 -m neuxmind_cloud init-db`；对旧库只做“补列”（`db.ADDED_COLUMNS`），不是完整迁移。

管理命令：`init-db`、`create-operator <email> [--name]`、`verify-email <email> [--force]`、`grant-operator <email>`、`revoke-operator <email>`、`sweep`。

## 已知限制

1. **真实支付渠道没有接入**：Stripe / 支付宝 / 微信支付只有骨架。接入时要实现各渠道自己的验签方式（Stripe-Signature、RSA2、APIv3 AEAD），不能沿用 sandbox 的 HMAC 格式。
2. **真实模型上游没有验证**：OpenAI 兼容 provider（含流式）只测过本机假服务（chat chunk / responses 事件 / chunked 断流 / 4xx / 5xx）。流式计量的"下界"依赖"上游按 token 逐块输出"这一假设：若某上游把多个 token 合成一个增量，下界会偏小（仍然是下界，只是截断更晚触发；最终以 usage 为准，超出 hold 走超额规则）。
3. 没有数据库迁移工具（只用 `create_all` + 补列），上线前要引入 Alembic。PostgreSQL：第 01 轮修复起整套测试在本机临时 PostgreSQL 16 上跑过（第 02 轮审查修复后：174 个全过，含会话并发与流式不持锁测试），但没有在生产规模下压测。
9. 流式输出一旦送达客户端无法撤回：上游 usage 超过 hold（例如分词器不是字节级、prompt 侧超出）时，非流式会扣下输出，流式只能事后按 hold 计费、记录超额并锁定会话。截断只防输出侧。
10. 非流式调用结果未知（5xx/超时）时，仍沿用第 01 轮规则：不把 hold 计入 `used_tokens`，只计 `unknown_calls`（会话关闭时转核对）；流式中断则计入 hold。两者不一致是有意保留旧行为，是否统一留给总指挥决定。
4. 清理任务（修复 C-05）判定“丢失”靠时间阈值：`urllib` 的超时是单次读写的超时，一个按字节慢慢返回的上游可能超过 `stale_call_seconds` 仍在进行。这种迟到的结果不会被重复记账（会话：409 `call_marked_lost`；infer：预留仍在核对中则正常结算，已被运营处理则 409 `reservation_already_resolved`），只保留证据。
5. 端侧模型包只返回 `ref + sha256`，云端不托管文件，也不签发下载 URL。
6. 授权没有吊销接口，也没有订阅到期机制；退款流程没有实现。
7. 没有修改邮箱；SMTP 发送器只用 mock 测过；验证、认领、重设令牌都通过 API 提交（没有网页落地页）；被认领停用的旧账户行和它的个人组织保留在库里（不能再登录），没有清理。
8. API Key 的 `last_used_at` 每次请求都写库，高 QPS 时需要改成批量更新。
