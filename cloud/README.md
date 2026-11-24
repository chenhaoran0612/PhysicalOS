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
| `device_alerts` | 设备告警 | (org, instance_id, alert_id) 唯一 = 重复上报不重复保存；级别、设备上的时间 `ts`、收到的时间、原因、建议、`detail`；`source` device / cloud（云端生成的“设备离线”）；`handled_at` / `handled_by` |
| `device_reports` | 设备最近一次上报 | (org, instance_id) 唯一；`last_report_at` 用来判定离线；`pending` 是设备说它还压着没发的条数；`offline_alert_id` 指向当前这次失联的告警，设备在线时为空 |
| `robots` | 设备群 | (org, instance_id) 唯一；规范化后的 manifest、摘要、能力 ID、`simulated`；`conformance`（JSON，可空，第 04 轮：最新一致性报告摘要，旧库启动时自动加列） |
| `audit_logs` | 审计 | 注册/登录失败/Key 创建与吊销/购买/支付/webhook 拒绝/调额/核对/改价/机器人登记等 |
| `library_materials` | 资料库里的一份资料（issue #33） | `org_id`、显示用文件名、`kind`（document / package）、`format`、本体类型标识、厂家、型号、版本、`size`、`sha256`、`status`（parsing / parsed / usable / failed，issue #34）、`device_id` / `device_name`（所属设备，issue #34）。同组织内（标注 + 设备 + 内容）唯一、（标注 + 设备 + 文件名）唯一 |
| `library_material_parses` | 一份资料的解析任务和结果（issue #34，`material_parse_models.py`） | `material_id`（不设外键，资料删除后由清理删掉）、`stage`、租约、WeKnora 里副本的编号、解析出的 `extracted` 和人改过的 `values`、`not_applicable`、`warnings`、`error`、`revision`、修改人和确认人及时间 |
| `library_uploads` / `library_upload_chunks` | 进行中的上传与已收到的分片 | 上传属于发起它的用户；`expires_at` 之后连同已收到的数据一起清除 |

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
| GET `/me` | 登录会话 → `{user, email_verified, platform_operator, auth:"session", current_org_id, role, permissions[], organizations[{id,name,personal,role}]}`。API Key → 只有 `{auth:"api_key", current_org_id, role, permissions[]}`：Key 所在的组织、创建者在这个组织的角色、Key 实际的权限，不含创建者的邮箱、姓名、邮箱验证状态、运营员标志和其他组织（Key 可能放在设备上，见“设备告警”）；`X-NeuxMind-Org` 对 Key 不起作用 |
| POST `/org/members` | `{email, role: admin|developer|operator}`（owner 不能通过接口授予或修改；只有 owner 能授予 admin、修改 admin 的角色） |
| GET `/org/members` | `{members:[{user_id, email, name, role}]}` |
| DELETE `/org/members/{user_id}` | 修复新增：移除成员。owner 不能被移除（也不能自己退出）；owner/admin 可移除 developer/operator；只有 owner 能移除 admin；任何人可以移除自己（退出）。被移除成员创建的 API Key 立即失效 |
| POST `/api-keys` | `{name?, scopes[]}` → 201 `{id, name, prefix, scopes, org_id, key}`（**`key` 明文只返回这一次**）；只能用会话创建 |
| GET `/api-keys` | → `{api_keys:[{id,name,prefix,scopes,org_id,created_at,last_used_at,revoked}]}` |
| DELETE `/api-keys/{id}` | 吊销 |

API Key 的 scopes：`gateway`、`catalog:read`、`robots`、`wallet:read`、`usage:read`。Key 实际拥有的权限 = scopes ∩ 创建者当前角色的权限（创建者被降级或移出组织后，Key 立即收窄或失效；有测试 `test_demoting_creator_narrows_key_scopes`）。API Key 不能管理 Key、不能购买、不能访问 `/admin`；通过 Key 调用网关时检查的是**创建者**的邮箱是否已验证。

另有 `alerts:report`（issue #26）：给放在设备上的 Key 用，凭它只能调 `POST /device-reports`；不在默认范围里；四种角色都持有它，所以上面“创建者被降级后收窄”对它不起作用。见“设备告警”。

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

### 资料库（issue #33，`library.py`）

机器人的手册、参数表、URDF 和驱动安装包存在云端，按组织隔离；每份资料标明本体类型（先存类型的标识）、厂家、型号和版本，以及它属于哪台设备（`device_id`、`device_name`，issue #34，可以不填）。文件存在服务器的磁盘卷上（`NEUXMIND_LIBRARY_DIR`），不进数据库。上传完成的资料是 `status: "parsing"`（解析中），由解析线程接着处理，见下一节“资料解析”。

权限：`library:read`（列表、详情、取回文件）四种组织角色都有；`library:write`（上传、取消、删除）owner / admin / developer 有，operator（现场操作员）没有，并且要求邮箱已验证。两者也是 API Key 的权限范围。

| 方法 路径 | 请求 → 响应 |
|---|---|
| GET `/library/formats` | → `{formats: [{id, label, kind, extensions[]}], extensions[], supported, limits: {document_bytes, package_bytes, org_quota_bytes, chunk_bytes}}` |
| POST `/library/uploads` | `{filename, size, sha256, body_type, vendor, model, version, description?, device_id?, device_name?}` → 201 上传 `{id, status, resumed, filename, kind, format, size, sha256, chunk_bytes, chunk_count, received[], received_count, received_bytes, expires_at, …}`。同一个人对同一个文件、同一组标注已有未完成的上传时返回 **200** 和那一次上传（`resumed: true`，`received` 是云端已有的分片）：中断后续传就是再发一次这个请求，然后只补缺的分片。已经完成全部分片但卡在最后一步的上传也这样接回来（`status: "assembling"`，直接再调 complete）。拒绝：415 `unsupported_type`（带 `supported` 扩展名列表，文字里写明支持的类型）、413 `file_too_large`（`max_bytes`、`kind`）、409 `material_exists` / `version_exists`（`material_id`）、409 `library_quota_exceeded`（空间：`quota_bytes`、`used_bytes`；份数：`max_materials`）、409 `too_many_uploads`、429 `rate_limited`（被拒绝的请求也计数）、507 `storage_full` |
| GET `/library/uploads/{id}` | → 上传（同上） |
| PUT `/library/uploads/{id}/chunks/{n}` | 请求体是这一片的原始字节，`Content-Type: application/octet-stream`；除最后一片外每片恰好 `chunk_bytes`。可选 `X-Chunk-SHA256`（与请求体不符时 400 `chunk_digest_mismatch`）→ `{upload_id, index, sha256, received_count, chunk_count, received_bytes}`。分片可以乱序、可以重发。凭据不存在或已失效时，在读取请求体之前就回答 401。400 `invalid_chunk`、415 `octet_stream_required`、429 `chunk_attempts_exceeded`（这次上传不能再继续；再发一次开始请求会换一个新的上传）、507 `storage_full`、413（超过 `chunk_bytes`） |
| POST `/library/uploads/{id}/complete` | → 201 资料（见下）。核对总长度、SHA-256 和内容类型后才保存。409 `chunks_missing`（`missing[]`，上传仍然开着）、503 `library_busy`（同时核对的文件数到了上限，带 `retry_after`，上传仍然开着）、409 `digest_mismatch`、415 `unsupported_type`、409 `material_exists` / `version_exists`、409 `upload_lost`；后五种会丢弃这次上传。核对要读完整个文件，2 GB 的安装包可能要几十秒到几分钟，前置代理的超时要放够 |
| DELETE `/library/uploads/{id}` | 取消并释放已收到的数据 → `{cancelled, id}`；已经完成入库的上传回答 404 |
| GET `/library/materials` | 过滤：`body_type`、`vendor`、`model`、`version`（后三个不分大小写）、`kind`、`format`、`status`、`sha256`、`device_id`、`q`（文件名、厂家、型号里的子串）；`limit`（1–200，默认 50）、`offset` → `{materials[], total, limit, offset}`，新的在前 |
| GET `/library/materials/{id}` | → `{id, filename, kind, format, body_type, vendor, model, version, description, size, sha256, status, status_detail, device_id, device_name, created_by, created_at, updated_at}` |
| GET `/library/materials/{id}/content` | 文件内容，支持 `Range`。永远是 `application/octet-stream` + `Content-Disposition: attachment`，带 `X-Checksum-SHA256`（整个文件的摘要）、`X-Content-Type-Options: nosniff`、`Content-Security-Policy: default-src 'none'; sandbox` |
| DELETE `/library/materials/{id}` | → `{deleted, id}`；同组织里没有别的资料引用这份内容时，文件一并删除 |

接收哪些类型（`library_types.py`，文件名的后缀只表示“声称是什么”，是否接收看内容）：

| 类型 | 后缀 | 内容上的判定 |
|---|---|---|
| Word / Excel | `.docx` `.xlsx` | ZIP 结构，目录里有 `[Content_Types].xml` 和 `word/document.xml` / `xl/workbook.xml`；目录超过 5000 项或声明 ZIP64 的拒绝 |
| Word / Excel 97–2003 | `.doc` `.xls` | 复合文档头，目录里有 `WordDocument` / `Workbook`（或 `Book`）流 |
| PDF | `.pdf` | 前 1024 字节里有 `%PDF-` |
| 文本 | `.md` `.markdown` `.txt` `.rst` `.csv` `.tsv` `.log` `.ini` `.cfg` `.conf` `.toml` `.yaml` `.yml` `.json` `.xml` | 全文没有控制字符，并且能按 UTF-8、否则按 GB 18030 完整解码，或者带 BOM 的 UTF-16；`.json` 以 `{` 或 `[` 开头，`.xml` 以 `<` 开头。其他编码（Shift-JIS、Big5、不带 BOM 的 UTF-16 等）要先转成 UTF-8 |
| URDF | `.urdf` `.xacro` | 文本、以 `<` 开头，开头 4 MiB 里有 `<robot` 元素 |
| 驱动安装包 | `.zip` `.tar.gz` `.tgz` `.tar.xz` `.txz` `.tar.bz2` `.tbz2` `.tar` `.deb` | ZIP：本地文件头和目录结束记录；压缩的 tar：对应的魔数，并且解出的前 512 字节是 tar 头（只解这 512 字节）；tar：头部校验和；deb：`ar` 头且第一个成员是 `debian-binary` |

其他类型（可执行文件、脚本、HTML、SVG、带宏的 Office 文档、图片、rar / 7z 等）一律拒绝。云端不解压、不执行、不用 XML 解析器读上传的内容。

这些判定只是**认出**文件是不是声称的类型，不是对内容的担保：压缩包只看开头，后面可以是任何东西。之后真正打开这些文件的地方（解析 #34、把驱动安装包送到设备 #31）仍然要把内容当作不可信的输入处理。

“同一份资料同一版本不重复收”的含义：同一组织内，标注（本体类型、厂家、型号、版本，后三项不分大小写）相同且内容相同的，拒绝（`material_exists`，文件名不同也算同一份）；标注相同、文件名相同但内容不同的，拒绝并提示改版本号（`version_exists`）。同一内容换一个版本或型号可以再收，磁盘上只存一份。资料属于一台设备（issue #34）：同一个文件给另一台设备（另一个 `device_id`）上传不算重复。

### 资料解析（issue #34，`material_parse.py`）

上传完成的资料由云平台的解析线程自动解析；结果给人看、可以改，人确认后才是“可用”。做法、状态、提取的项目和不可信内容的处理写在 [docs/MATERIAL_PARSING.md](../docs/MATERIAL_PARSING.md)，这里只列接口和要点。

- 四个状态：`parsing`（解析中）→ `parsed`（待确认）→ `usable`（可用，人确认后）；`failed`（没解析出来，`status_detail` 写明原因或缺哪一项，人填好后确认也变 `usable`）。只有 `usable` 的资料能被后面的流程使用（#31、#30）。
- 文档（Word、Excel、PDF、文本）交给 WeKnora（`weknora.py`）解析和存储，再经它的问答接口问模型、只限这一份文档。**文档的内容会发给 WeKnora 里配置的模型服务。** 没配 `NEUXMIND_WEKNORA_URL` / `_API_KEY` / `_KB_ID` 时文档标“没解析出来”，可以手工填写后确认。
- URDF（关节、连杆、每个关节的位置 / 速度 / 力矩上限）和驱动安装包（名称、版本、架构、校验值；内容不解析）在子进程里用规则读（`material_extract.py`，由 `material_sandbox.py` 启动，CPU、内存、时间、输出都有上限）。
- 权限沿用资料库：读要 `library:read`；改、确认、重新解析要 `library:write` 和已验证的邮箱。

| 方法 路径 | 请求 → 响应 |
|---|---|
| GET `/library/materials/{id}/parse` | → `{material, kind (document / urdf / package), fields[], note, stage, status, status_detail, values, not_applicable[], missing[], warnings[], error, retrying, parser, revision, edited_by, edited_at, confirmed_by, confirmed_at, editable}` |
| PUT `/library/materials/{id}/parse` | `{revision, values, not_applicable?}` → 同上；400 `invalid_parse_values`、409 `parse_changed`（`revision` 不是最新的）、409 `parse_running` |
| POST `/library/materials/{id}/parse/confirm` | 同上，并确认 → `usable`；409 `parse_incomplete`（`missing[]`） |
| POST `/library/materials/{id}/parse/retry` | → `parsing`，从头解析 |

### 运营（`/admin/*`，需要登录会话且是平台运营员）
`GET/POST /admin/models`、`PATCH /admin/models/{id}`（改价不影响已有预留的价格快照和已结算的账本）、`POST /admin/models/{id}/status {status: published|unlisted}`、`POST /admin/credit-packs`、`POST /admin/wallets/{org_id}/adjust {delta, reason, reference?}`、`GET /admin/reservations?status=&older_than_seconds=`（默认 reconciliation_required；`status=held|settled|released|all`；每项带 `age_seconds`）、`POST /admin/reservations/{id}/resolve {action: settle|release, credits?, evidence_ref?, reason}`（只处理 reconciliation_required，held 返回 409 `reservation_in_flight`）、`POST /admin/maintenance/sweep`（立即清理，返回 `{lost_calls, sessions_closed, orphan_holds}`）、`GET /admin/overages?status=open|waived|charged|all`、`POST /admin/overages/{id}/resolve {action: waive|charge, credits?, reason}`、`GET /admin/orders?status=`、`POST /admin/orders/{id}/confirm {payment_reference, reason}`（manual 渠道）、`GET /admin/audit?action=`。

### 设备告警

issue #26，需求 R7-08。设备上的管理层每 5 分钟主动上报一次（见 [`runtime/README.md`](../runtime/README.md) §2.3），云端接收、保存、给人看；设备多久没上报算离线也在这里判定。代码在 `alerts.py`、`alert_models.py`、`routes/alerts.py`。

| 接口 | 说明 |
|---|---|
| POST `/device-reports` | 设备上报。`{schema: "neuxmind.device_report.v1", device: {instance_id, name?, family?, simulated?, runtime_version?}, alerts: [neuxmind.alert.v1…], pending?}`（`alerts` 可以为空，表示“设备还在”；`pending` 是设备上还压着没发的条数）→ `{accepted[], duplicates[], rejected[{index, id, error}], received_at, device: {instance_id, robot_id, state}}`。只接受带权限范围 `alerts:report` 的 API Key，登录会话不行（403 `api_key_required`） |
| GET `/alerts?instance_id=&level=&handled=true\|false&limit=&before=` | 本组织的告警，新的在前；`limit` 1–200（默认 50），`more` 为真时带 `before=<next_before>` 取下一页。需要 `robots` 权限 |
| POST `/alerts/{id}/handled` | `{handled: true\|false}`（默认 true）标“已处理”或取消 → 这条告警。需要 `robots` 权限；其他组织的告警返回 404 |
| GET `/alerts/devices` | 上报过的设备：`{devices: [{instance_id, robot_id, name, family, simulated, runtime_version, state: online\|offline, first_report_at, last_report_at, reports, offline_after_s, pending_alerts, unhandled_alerts}], report_interval_s, offline_after_missed}`，离线的排在前面。需要 `robots` 权限 |
| DELETE `/alerts/devices?instance_id=` | 不再盯一台已经撤走的设备（它的告警保留；它要是再上报，就又回到列表里）→ `{forgotten: true, instance_id}`。需要 `robots` 权限 |

一条告警：`{id, alert_id, source: device|cloud, instance_id, robot_id, device_name, kind, level: critical|warning|info, ts, received_at, reason, advice, detail, handled, handled_at, handled_by}`。`ts` 是设备上的时间，`received_at` 是云端收到的时间；`robot_id` 是收到时这个 `instance_id` 登记的机器人，没有登记过就是 `null`（告警照收，不因为登记的先后而丢）。

- **权限。** 给设备用的 Key 只带 `alerts:report`：能调的只有 `/device-reports`（以及所有 Key 都能调的 `GET /me`：用 Key 调用时只回这把 Key 的组织 id、创建者的角色和 Key 的权限，拿到设备上这把 Key 的人读不到创建者的邮箱、姓名和创建者所在的其他组织），读不了告警和机器人，调不了别的写接口。这个范围不在新建 Key 的默认范围里，要显式指定（`POST /api-keys {name, scopes: ["alerts:report"]}`）；四种角色都持有它，所以创建者被降级不会收窄这把 Key，创建者被移出组织或 Key 被吊销才失效。人看告警、标已处理、撤掉设备用已有的 `robots` 权限（登录会话或带 `robots` 的 Key）。
- **重复上报不重复保存。** 设备给每条告警一个自己的 `id`，云端按（组织, `instance_id`, `id`）只存一次；再次收到同一个 `id` 时放在 `duplicates` 里答复，内容以第一次为准。设备没有收到答复就会随下一次上报重发，所以这条是必需的。同一台设备的两次上报不会同时处理（PostgreSQL 上用 advisory lock，SQLite 本来就是串行的）。
- **坏的告警单条拒绝。** 级别不是三种之一、时间不是 0 到 1e11 之间的数、原因为空或超过 1000 个字符、`id` 或 `kind` 的字符不合要求、`detail` 不是对象或超过 4000 字节、文字里有数据库存不了的字符（空字符、半个代理对）或其他控制字符（换行和制表符可以）的，放进 `rejected` 并说明原因，同一次上报里其余的照收。整份上报格式不对（`schema`、`device.instance_id`、`alerts` 不是列表或超过 200 条）返回 400 `invalid_report`。设备的名称、类型、版本只是显示用的，里面的这类字符直接替换掉。
- **离线。** 设备最后一次上报距今超过 `NEUXMIND_DEVICE_OFFLINE_MISSED`（默认 3）× `NEUXMIND_DEVICE_REPORT_INTERVAL_SECONDS`（默认 300 秒）再加 `NEUXMIND_DEVICE_OFFLINE_GRACE_SECONDS`（默认 60 秒）即 16 分钟，就是 `offline`。多出的 1 分钟是给第 3 次上报在路上的时间，不然它晚到几秒就会被算成没收到。云端为这一次失联生成一条告警（`kind: device_offline`，`level: warning`，`source: cloud`），不重复生成；设备再次上报后恢复 `online`，那条告警的 `detail.recovered_at` 记下恢复时间，之后再失联是新的一条。判定在后台清理（`sweep`，结果里多一项 `devices_offline`）里做，有人查看 `/alerts`、`/alerts/devices` 时也会先做一次。只对上报过的设备判定：从没上报过的机器人不算离线。
- **上限。** 每台设备每 5 分钟最多 `NEUXMIND_DEVICE_REPORT_MAX_PER_WINDOW`（默认 60）次上报，整个组织最多 `NEUXMIND_DEVICE_REPORT_ORG_MAX_PER_WINDOW`（默认 3000）次，超出 429 `rate_limited`；每个组织最多 `NEUXMIND_DEVICE_REPORT_MAX_DEVICES`（默认 1000）个上报过的 `instance_id`，超出 409 `too_many_devices`（用 DELETE `/alerts/devices` 腾位置）；每台设备最多保留 `NEUXMIND_DEVICE_ALERT_KEEP`（默认 2000）条告警，整个组织最多 `NEUXMIND_DEVICE_ALERT_ORG_KEEP`（默认 200000）条。超出时先删已处理的，再按 info、warning、critical 的顺序删最早的：没人处理过的最高级别告警最后才动。
- **审计。** `device.first_report`、`device.alerts`（收下几条、拒收几条、各级别几条，带来源地址；空的上报不记）、`device.offline`、`device.forgotten`、`alert.handled` / `alert.unhandled`。
- **限制。** 设备自报 `instance_id`，Key 是组织级的：持有一把上报 Key 就能以本组织任意设备的名义上报（进不了别的组织），包括报假告警、替一台不出声的设备发空上报让它看起来在线、改显示的设备名称、用大量告警把较早的告警挤出保留上限。每台设备单独的凭证还没有做。收到告警之后怎么通知人（飞书、短信、客户的系统）没有做，目前只能查。没有删除单条告警的接口。告警的原因、建议、设备名称是设备给的文字，展示它们的界面要自己转义。`ts` 取决于设备的时钟。在 SQLite 和 PostgreSQL 16 上都跑过测试（含并发上报）；没有在真实设备和部署好的云平台之间验证过。

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
- 放在设备上的 Key 只给 `alerts:report`：设备丢失或被攻破时，这把 Key 读不到告警和机器人、调不了别的写接口，能做的是以本组织设备的名义上报（假告警、冒充在线、挤占保留条数，见“设备告警”的限制；有上报频率、设备数、保留条数的上限）；吊销它不影响别的 Key。上报只认 API Key，登录会话不能上报。
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
- 资料库（issue #33）按文件上传的最严做法处理：
  - 类型由内容决定，后缀不在清单里的在发送任何字节之前拒绝；第一片到达时先核对一次，整份文件收齐后再核对一次，不符合的不留任何数据。
  - 文件名只作显示：去掉路径、控制字符和不可见字符、文件系统的特殊字符，最长 150 个字符。文件从不以上传的名字落盘：路径只由服务端生成的编号和服务端算出的 SHA-256 拼成（`library_store.py`），所以不存在路径穿越。
  - 每个请求都鉴权并检查权限，每一片也一样；上传只有发起它的人能继续；跨组织一律 404。
  - 每片有自己的请求体上限（`NEUXMIND_LIBRARY_CHUNK_BYTES`）：只对路径完全符合 `PUT /api/v1/library/uploads/<上传编号>/chunks/<n>` 的请求生效，并且只给凭据确实存在的调用方；没有凭据或凭据失效的，在读取请求体之前就回答 401，其他路由仍是 1 MB。
  - 单个文件、组织合计的字节数和份数、同时进行的上传数、每小时开始上传的次数（被拒绝的也算）、每次上传的分片请求数、一次上传从开始到完成的最长时间（7 天）、同时核对的文件数都有上限。
  - 卷的剩余空间按“现在的剩余 − 所有组织已经登记但还没写完的字节”来算，低于下限时不再接收新的上传，写每一片之前再看一次。
  - 保存前核对整份文件的 SHA-256；分片写入持共享文件锁、完成步骤持独占锁，并且在移动文件的前后核对“名字下面的文件就是被核对的那个文件”（inode），被核对的字节就是被保存的字节。
  - 文件所在目录不由任何 Web 服务器提供，文件没有执行位（目录 0700、文件 0440）；取回时固定为二进制下载，带 `nosniff` 和 `sandbox`，不会被当成网页或脚本。
  - 上传开始、入库、拒绝、取消、删除都写审计（`library.*`）。
  - 清理（`maintenance.sweep`，后台线程、`sweep` 命令）：过期的上传连同数据一起清除；没有上传认领的临时文件、没有记录的已存文件（进程在两步之间崩溃留下的）满 1 小时后清除，后一种每个进程最多每小时扫一次。
- 资料解析（issue #34，细节见 docs/MATERIAL_PARSING.md）把文件、WeKnora 的答复和模型的回答都当作不可信的输入：
  - 云端进程从不解析上传的文件：URDF 和安装包在子进程里读（`python -I -S`、只用标准库、文件作为标准输入、几乎空的环境），子进程先给自己设 CPU、内存（Linux）、写文件（0）和打开文件数的上限，父进程另有墙钟时间和输出上限，超限杀掉整个进程组；失败只影响这一份资料。
  - 压缩包只读成员名和必要的一小段，不解压到磁盘，解出的字节、xz 的内存、ZIP 目录的大小都有上限；名字指向包外的成员和链接只报告。XML 一律拒绝 DOCTYPE（没有外部实体，也没有实体膨胀），深度和元素个数有上限。
  - 有外部引用的 Office 文档不送 WeKnora（关系部件多得不正常的也不送）；有宏的照常送去读文字，结果里注明。zstd 压缩的 deb 由镜像里的 `dpkg-deb` 在同样受限的子进程里读。
  - 读 WeKnora 的答复有字节和总时间上限；任何一步出了没预料到的错，只让这一份资料变成“没解析出来”。
  - 模型的回答只当数据：按项目校验类型、长度和条数，去掉控制字符，人确认后才可用；安装步骤从不被执行。
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
| `NEUXMIND_DEVICE_REPORT_INTERVAL_SECONDS` / `NEUXMIND_DEVICE_OFFLINE_MISSED` / `NEUXMIND_DEVICE_OFFLINE_GRACE_SECONDS` | `300` / `3` / `60` | 设备上报的间隔（和设备上的固定值一致）；连续几次没收到算离线；最后一次允许晚到多久（“设备告警”） |
| `NEUXMIND_DEVICE_REPORT_MAX_PER_WINDOW` / `NEUXMIND_DEVICE_REPORT_ORG_MAX_PER_WINDOW` / `NEUXMIND_DEVICE_REPORT_MAX_DEVICES` | `60` / `3000` / `1000` | 每个间隔里每台设备、每个组织收的上报数；每个组织上报过的设备数 |
| `NEUXMIND_DEVICE_ALERT_KEEP` / `NEUXMIND_DEVICE_ALERT_ORG_KEEP` | `2000` / `200000` | 每台设备、每个组织保留的告警数 |
| `NEUXMIND_STREAM_MAX_SECONDS` | `0`（自动） | 流式会话调用的最长时长；自动 = `STALE_CALL_SECONDS − UPSTREAM_TIMEOUT_SECONDS − 30`（≥10），显式值也不会超过它 |
| `NEUXMIND_STREAM_IDLE_SECONDS` | `30` | 客户端这么久不读数据按断开处理（网关继续排空上游并结算） |
| `NEUXMIND_SCRYPT_LOG_N` | `17` | 低于 17 只允许 dev/test |
| `NEUXMIND_CORS_ORIGINS` | 空 | 逗号分隔的允许来源 |
| `NEUXMIND_MAX_BODY_BYTES` | `1000000` | |
| `NEUXMIND_SESSION_MAX_BODY_BYTES` | `8000000` | 只用于会话模型调用（`/gateway/sessions/{id}/v1/responses`、`/chat/completions`；审查 Q3-07）：智能体运行时每次都发送完整历史。字节仍全部计入输入上界与会话预算。超限 413 `payload_too_large`，带 `max_bytes` 与“会话历史过长，请开启新会话继续” |
| `NEUXMIND_LIBRARY_DIR` | SQLite 时在数据库文件旁的 `neuxmind_library/`，否则 `./neuxmind_library`；镜像里是 `/var/lib/neuxmind/library` | 资料库（issue #33）存文件的目录，应当是一个持久的磁盘卷 |
| `NEUXMIND_LIBRARY_CHUNK_BYTES` | `4194304` | 上传的分片大小，也是分片路由的请求体上限（64 KiB–64 MiB） |
| `NEUXMIND_LIBRARY_MAX_DOCUMENT_BYTES` / `NEUXMIND_LIBRARY_MAX_PACKAGE_BYTES` | `209715200` / `2147483648` | 单个文档 200 MB、单个驱动安装包 2 GB |
| `NEUXMIND_LIBRARY_ORG_QUOTA_BYTES` | `21474836480` | 每个组织合计 20 GB（已保存的加上进行中的上传） |
| `NEUXMIND_LIBRARY_UPLOAD_TTL_SECONDS` | `86400` | 上传这么久没有新分片就连同数据一起清除 |
| `NEUXMIND_LIBRARY_MAX_OPEN_UPLOADS` / `NEUXMIND_LIBRARY_UPLOADS_PER_HOUR` | `5` / `120` | 每个组织同时进行的上传数；每个用户每小时开始上传的次数（被拒绝的也算） |
| `NEUXMIND_LIBRARY_ORG_MAX_MATERIALS` | `20000` | 每个组织的资料份数上限 |
| `NEUXMIND_LIBRARY_MAX_CONCURRENT_COMPLETIONS` | `2` | 每个进程同时核对（完成）的上传数；超过时回答 503 `library_busy` |
| `NEUXMIND_LIBRARY_MIN_FREE_BYTES` | `1073741824` | 扣掉所有已登记上传还没写完的字节后，卷上至少还要剩这么多，否则 507 `storage_full` |
| `NEUXMIND_WEKNORA_URL` / `NEUXMIND_WEKNORA_API_KEY` / `NEUXMIND_WEKNORA_KB_ID` | 空 | 解析文档用的 WeKnora（issue #34）：地址、API Key、知识库编号；三项都配了才把文档送去。平台 Key 另配 `NEUXMIND_WEKNORA_TENANT_ID` |
| `NEUXMIND_WEKNORA_MAX_BYTES` | `52428800` | 超过就不送 WeKnora（与它的 `MAX_FILE_SIZE_MB` 一致） |
| `NEUXMIND_WEKNORA_PARSE_SECONDS` / `NEUXMIND_WEKNORA_ANSWER_SECONDS` | `1800` / `180` | 等 WeKnora 解析完一份文档 / 一个回答的最长时间 |
| `NEUXMIND_LIBRARY_PARSE_INTERVAL_SECONDS` / `NEUXMIND_LIBRARY_PARSE_THREADS` | `5` / `2` | 解析线程多久找一次活、几个线程（0 不启动） |
| `NEUXMIND_LIBRARY_PARSE_WALL_SECONDS` / `_CPU_SECONDS` / `_MEMORY_BYTES` / `_UNPACKED_BYTES` | `120` / `60` / `1073741824` / `8589934592` | 规则解析子进程的墙钟时间、CPU 时间、地址空间（Linux）、解出字节的上限 |
| `NEUXMIND_SESSION_TTL_SECONDS` | `43200` | |
| `NEUXMIND_SESSION_REMEMBER_TTL_SECONDS` | `2592000` | 登录或注册时带 `remember: true` 的会话有效期（桌面应用的“保持登录”，issue #36） |
| `NEUXMIND_LOGIN_MAX_FAILURES` / `NEUXMIND_LOGIN_WINDOW_SECONDS` | `8` / `60` | 前者现在是每（邮箱, IP）组合的上限 |
| `NEUXMIND_WEBHOOK_SECRET_<PROVIDER>` | 空 | 例如 `NEUXMIND_WEBHOOK_SECRET_SANDBOX`；sandbox 没有密钥时不可用 |
| `NEUXMIND_WEBHOOK_TOLERANCE_SECONDS` | `300` | |
| `NEUXMIND_UPSTREAM_TIMEOUT_SECONDS` | `60` | |
| `NEUXMIND_ALLOW_INSECURE_UPSTREAM` | 不设 | 设为 `1` 且环境是 dev/test 时，允许非回环的 http 上游 |
| `NEUXMIND_SPEC_PATH` | 仓库里的 `spec/` | `neuxmind_spec` 所在目录 |
| 模型卡 `credential_ref` 指向的变量 | — | 上游 API Key，只在运营端设置 |

## 部署

见 `cloud/Dockerfile`（在仓库根目录构建：`docker build -f cloud/Dockerfile -t neuxmind-cloud .`）。生产环境要用 PostgreSQL（安装 `psycopg[binary]`），前面放 TLS 反向代理并配置 `NEUXMIND_TRUSTED_PROXIES`，配置 SMTP（否则用户只能由运营 `verify-email`），**不要配置 `NEUXMIND_OPERATOR_EMAILS`**，运营员用 `create-operator` 命令创建。表在启动时用 `create_all` 自动创建，也可以执行 `python3 -m neuxmind_cloud init-db`；对旧库只做“补列”（`db.ADDED_COLUMNS`），不是完整迁移。

资料库（issue #33）需要一个持久的磁盘卷：镜像里的目录是 `/var/lib/neuxmind/library`（属主是运行用户，权限 0700），部署时把卷挂到这里，建议带 `noexec,nosuid,nodev`。数据库和这个卷要一起备份、一起恢复：数据库里是资料的记录，卷里是文件。多个云平台进程可以共用同一个卷（文件锁在本机文件系统上有效；网络文件系统上要先确认 `flock` 可用）。

管理命令：`init-db`、`create-operator <email> [--name]`、`verify-email <email> [--force]`、`grant-operator <email>`、`revoke-operator <email>`、`sweep`。

### 本地 Docker（演示用，不是生产部署）

`cloud/compose.yaml` 在本机 Docker 里启动 PostgreSQL 16 和云平台，并自动写入演示数据（演示账号、运营账号、4 个模型、50000 点）。在仓库根目录执行：

```bash
docker compose -f cloud/compose.yaml up -d --build --wait   # 启动：http://127.0.0.1:8800
docker compose -f cloud/compose.yaml logs seed              # 查看演示账号与密码
docker compose -f cloud/compose.yaml down                   # 停止，数据库保留
docker compose -f cloud/compose.yaml down -v                # 停止并删除数据库
```

- 运行在 `NEUXMIND_ENV=dev`：开启 Echo 替身模型、运营员邮箱白名单和公开的演示密码，所以端口只发布在 `127.0.0.1`，数据库不对外发布。**不要把这份配置放到公网。**
- 数据库在名为 `neuxmind-cloud_db-data` 的卷里，重启后数据还在；写入演示数据的 `seed` 任务每次启动都会运行，已有数据时什么都不改。`up --wait` 返回后还要等几秒，`seed` 任务才结束（首次启动时演示账号在那之后才能登录）。
- 资料库的文件在名为 `neuxmind-cloud_library-data` 的卷里（issue #33）：`down` 保留，`down -v` 连同数据库一起删除。
- 可选环境变量：`NEUXMIND_CLOUD_PORT`（默认 8800）、`NEUXMIND_DB_PASSWORD`（本地默认值，只在 compose 网络内可达）。数据库密码只能用字母、数字、`-`、`_`（它被原样拼进 `DATABASE_URL`），并且只在卷第一次创建时生效，之后要改先 `down -v`。
- `db` 和 `cloud` 设了 `restart: unless-stopped`：Docker 重启后它们会自己起来，不想要时执行 `down`。
- `NX_DEMO_CLOUD=docker python3 scripts/demo.py up` 用这套容器代替本机进程里的云平台，其余（仿真机器人、控制台）不变；`scripts/demo.py down` 一并停止容器。

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
11. 资料库（issue #33）：存储只有磁盘卷一种实现（`library_store.LibraryStore`，换对象存储是再写一个同样接口的类）；没有病毒扫描；能力库还没有，权限先用组织角色；本体类型只校验标识的格式，没有对照本体类型清单（issue #32）；没有修改标注的接口，标错了要删除后重传；别人开的上传只有本人能取消（组织管理员不能代为取消，最迟 7 天后过期）；配额按资料条数计字节，同一内容标成两个版本算两次；取回文件没有频率限制；下载时文件恰好被删除会得到 500；`.zip` 和压缩的 tar 只看开头，不检查完整性。
12. 资料解析（issue #34）：除了 WeKnora 的替身，只在本机 Docker 里的 WeKnora 0.6.3 上跑通过一次（2026-11-23，模型经 TokenFans；要注意的几点见 docs/MATERIAL_PARSING.md）；文档的内容会发给 WeKnora 里配置的模型服务；旧格式 Office 文档（`.doc`、`.xls`）按字段代码和函数名查外部引用，不认 Excel 97–2003 的外部工作簿链接；没有 `dpkg-deb` 的环境里读不了 zstd 压缩的 deb（名称、版本、架构由人填）；资料不能改挂到另一台设备；修复助手按需检索文档全文是 issue #30 的事。
