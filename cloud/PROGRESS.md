# 云平台 Agent 进度日志（只追加）

- 22:06 读完协作规则、INTERFACES_V1 §2、目标架构、brainos/commerce.py、auth.py、桌面端 agent-sessions 协议与 spec / 结果：装好 fastapi 0.162.0、SQLAlchemy 2.1.8、httpx 0.28.1；写完 config、db（SQLite 用 BEGIN IMMEDIATE，Postgres 用钱包行 SELECT FOR UPDATE）、models（全部表）、errors、security（scrypt + SHA-256 令牌哈希）、ledger（预留/结算/释放/核对/调额）、catalog（模型卡校验，能力 ID 用 spec 校验）、upstream（OpenAI 兼容 + 标注为测试替身的 EchoProvider） / 下一步：写支付、认证、网关、路由和 app
- 22:06 设计取舍：token 套餐用 PaymentProvider 付法币；模型授权直接用钱包点数付（license_once 扣点，按量/免费模型授权为 0 点）；所有网关模型和模型包下载都要先有授权。预留上限 = max_tokens + 输入字节数 + 每条消息 64（字节级 BPE 下这是 prompt token 的上界），超出走人工核对，不自动扣费
- 22:08 写完 payments（manual / sandbox 仅 dev+test / stripe·alipay·wechatpay 骨架返回 provider_not_configured，HMAC 验签 + 按 event_id 与 order 引用双重幂等）和 auth（scrypt、会话哈希 + 过期、按邮箱限速、nx_live_ Key 的 scope 不能超过创建者角色、运营员只能由环境变量/管理命令授予） / 下一步：网关 infer + sessions
- 22:12 写完 gateway（infer 三段事务：预留→上游→结算/释放/待核对；sessions 兼容桌面端 agent-sessions：一次性预留全部额度、会话令牌调 /v1/chat/completions 和 /v1/responses、close 按服务端计量结算）、全部路由（account/commerce/gateway/fleet/admin）、app（统一错误格式、请求体上限、CORS 白名单）、CLI / 下一步：冒烟测试，然后写单元测试
- 22:14 认证/API Key/运营权限/账本/多租户测试共 27 个全部通过；把 BEGIN IMMEDIATE 改成普通 BEGIN 做反向验证，两个并发测试按预期失败（database is locked），已恢复 / 下一步：写支付、网关、目录、设备群、安全测试
- 22:16 支付测试 10 个、网关测试 20 个通过（含本机假 OpenAI 兼容 HTTP 服务的协议测试：超时/500/缺 usage 的分类）。修了一个 bug：ApiError 的额外字段名 status 跟构造参数冲突，改名为 request_status / 下一步：目录过滤、价格变更不影响历史、设备群、安全基础测试
- 22:17 目录/价格历史/设备群/安全测试写完，修了两处：(1) 测试里价格变更的预期值算错（应为 401 点，代码正确）；(2) 分块传输的超大请求体被 FastAPI 包成 400，改成中间件先有上限地缓冲请求体再交给应用，现返回 413。全部 72 个测试通过 / 下一步：真实启动服务 + 跑仓库根测试 + 写 README/Dockerfile/进化记录
- 22:19 实际启动 python3 -m neuxmind_cloud --port 8800 正常（healthz/注册/能力注册表/grant-operator 命令）；仓库根 tests/ 72 个通过（跳过 20），未受影响；补了 Postgres 方言下 FOR UPDATE 的编译期测试，并处理会话期间模型下架的边界情况；cloud 共 73 个测试通过 / 下一步：写 README、进化记录，然后提交
- 22:20 README（架构/表/API/计费与支付流程/安全/配置/部署/已知限制）、Dockerfile、requirements（版本已固定）、EVOLUTION_ENTRY 写完；最终全量 73 个测试通过 / 下一步：只提交 cloud/
- 22:21 已提交 9b63fe9（只包含 cloud/，共 35 个文件；没有动 runtime/ 和其他目录） / 下一步：把交付报告发给总指挥

## 第 01 轮修复（云平台修复 Agent）

- 开始：读完协作规则、INTERFACES §2/§2.1、ADR-006..008、审查报告全文、审查脚本（cloud_checks.py / cloud_concurrency.py / mutate.py）和 cloud/ 全部代码 / 计划：C-01 邮箱验证 + 运营员资格、C-02 会话参数白名单 + 超额按上限计费、C-03 限速、C-04 错误分类、C-05 清理任务、P3 C-06..C-11、补 C4/C10 存活变异的测试
- 23:20 C-01 代码完成：users 增加 email_verified；一次性验证令牌（只存 SHA-256、24h 过期、单次使用、重发作废旧令牌、每小时 5 次）；EmailSender 抽象（dev/test 用记录型 LogEmailSender，响应里带 `dev_verification_token` 且标注 simulated；SMTP 发送器；prod 未配置时重发接口 503 `email_sender_not_configured`）；运营员 = 已验证邮箱 且（grant-operator 或白名单）；新增管理命令 verify-email / revoke-operator / sweep；购买、checkout、网关 infer/开会话要求已验证邮箱（403 `email_unverified`）；Dockerfile 去掉示例运营员邮箱；旧库自动补列（已有用户默认未验证）
- 23:20 C-02 代码完成：新模块 session_params.py，会话调用参数白名单（未知参数 400 `parameter_not_allowed`），n 只能为 1 并强制下发 n=1，输出上限由服务端按剩余预算写入（chat 按模型卡 `upstream.output_param` 写 max_tokens / max_completion_tokens / both，responses 写 max_output_tokens），只接受文本内容与函数工具，输入上界把 tools/instructions 等全部字节计入；超额：按预留上限计费、不返回输出、写 overage_records、会话锁定；/infer 同规则
- 23:20 C-04/C-05/C-06/C-07/C-08/C-09/C-10/C-11 代码完成（错误分类、清理任务 maintenance.py、错误文本只给通用文案、scrypt N=2^17 + 登录时自动升级 + 并发上限 4、prod 关闭 docs、prod 拒绝上架 echo、移除成员接口、运营不能处理 held 预留）。改了 3 个按旧规则写的测试（500 归为 Rejected、超额不扣费、prod 能上架 echo）为新规则；全部 74 个通过 / 下一步：补新测试
- 23:45 新增测试 38 个（test_email_verification 11、test_session_hardening 7、test_rate_limits 6、test_maintenance 8、test_p3_hardening 5、test_auth +1 C4），cloud 共 112 个通过
- 23:50 审查脚本复验：cloud_checks.py 第 1 节（抢注）→ platform_operator False、自调额 403；改用验证流程的同攻击版：n=50 → 400，max_completion_tokens=100000 → 下发 max_tokens=891、用量 911/1000、扣 10 点；凭据变量名不再出现在用户错误里；受害者可从自己的地址登录。cloud_concurrency.py 与基线一致
- 00:10 变异：审查的 C1–C9、C11、C12 均被杀；C4 被新测试杀掉；C10（代码改写后的同义变异）被新测试杀掉；本轮新增 27 个防线变异中 F03d（登录成功只清本地址计数）存活 → 补断言后被杀，全部 29 个被杀
- 00:20 本机有 PostgreSQL 16：临时集群（postgres 用户主目录下）+ pip 安装 psycopg[binary]==3.2.10，cloud 112 个测试在 Postgres 上全部通过；并发：登录 20 并发上限 3 → 3 个 401，注册 12 并发上限 3 → 3 个成功；去掉 advisory lock 的对照组 → 17 / 12（被绕过）；钱包 30 并发预留 → 10 成功。测完已停止并删除临时集群
- 00:25 全量：cloud 112 OK；spec 11 OK；tests 82 OK（跳过 20）；runtime 109 OK（第一次跑时 1 个连接上限计时测试因 runtime Agent 同时在跑变异而失败，重跑通过，与 cloud 无关）；sdk：CloudTests.setUpClass 失败（白名单 ops 用户未验证邮箱不是运营员，预期）；在不改 sdk/ 的前提下用补丁模拟"注册后提交 dev_verification_token"，SDK 28 个全部通过 → 写入契约变更请求
- 00:25 注意：DECISIONS.md 里 ADR-011 已被"现场证据失败即关闭"占用，新计费规则需总指挥另编号；README / EVOLUTION_ENTRY 已更新 / 下一步：提交 cloud/

## 第 01 轮复审修复（N-01）

- 读复审报告 N-01 与 rereview/cloud_attack.py。方案：验证必须在本账户会话内完成并吊销其他会话/旧 Key；未验证账户占用的邮箱可"认领"（新建已验证账户、停用旧账户）；找回密码；create-operator；grant-operator 拒绝未验证；verify-email 命令需 --force 且重设密码；prod 白名单默认关闭
- 代码完成（recovery.py 新增；auth / models / config / mailer / routes / CLI 修改）；夹具改为带会话验证、prod 运营员走 create-operator；重写 test_email_verification（N-01 原攻击路径、认领、找回密码、命令），新增 test_cli_commands；顺带修 N-07 / N-08 / N-11 → cloud 125 个通过
- 复审脚本复验（指向当前代码）：主人无会话提交令牌 [401, 401]；攻击者会话 platform_operator=False、自调额 403；主人重注册 202 → 认领后主人是运营员、攻击者会话 401；N-07 不再出现
- 变异 17 个：首轮 G11（停用账户密码不可用）存活 → 补断言后被杀；全部被杀
- PostgreSQL 16 临时集群：cloud 125 个全部通过，测完已删除
- 全量：spec 11、tests 82（跳过 20）、runtime 126 通过；sdk：CloudTests.setUpClass 因 verify_email 不带会话失败（预期）；补丁模拟"verify_email 带会话"后 28/29 通过，剩下 1 个断言的正是被取消的"未登录验证" → 写入 SDK 调整

## 第 02 轮（云平台 Agent）

- 开始：读完协作规则（第 7 条：变异测试在 git worktree 中做）、INTERFACES §2/§2.1/§4.3、ADR-006/007/012/013/014、复审 N-05/N-09、cloud README 与进化记录；基线 cloud 125、sdk 31 通过。复审其余 cloud P3（N-07/N-08/N-11）上轮已修，N-10 属 brainos，本轮 cloud 只剩 N-09
- 方案：N-05 选"串行化"——同一会话同一时刻至多一个在途调用（含流式），第二个 409 `session_busy`；流式 = 预留事务（短）→ 独立生产线程读上游 SSE、逐事件计量并透传（不持有任何事务/锁）→ 结算事务（短）；客户端断开后生产线程继续"排空"上游拿到 usage 再精确结算；输出下界 = 已转发的非空输出增量个数，下界将超过本次输出上限即截断并发 `event: error` `output_limit_reached`；usage 前中断按本次 hold 计入会话用量并转核对
- 代码完成：新模块 `streaming.py`（SSE 解析、StreamMeter 计量/截断、生产线程 + 有界通道、自写 ASGI 响应：始终监听 http.disconnect、结束时必定通知生产线程）；upstream 增加 `open_stream`（OpenAI 兼容用 `read1()` 读——`readline()` 会吞掉 chunked 断流的 IncompleteRead，看起来像正常结束；Echo 替身流式，每个事件带 `simulated: true`）；gateway 会话调用拆为"开始/结算"，N-05 `session_busy`；session_params N-09 嵌套白名单（tool_choice、tools[].function、tool_calls、内容片段、response_format、text.format、function_call 项）+ 允许 `stream`（布尔）；会话流式路由改为 async（网关逻辑仍在线程池）
- 自测发现并修复：我加的 responses 公共字段检查截断了 elif 链，function_call 项校验被跳过——新测试抓到
- 旧测试按契约改动 1 处：`test_budget_is_a_hard_ceiling` 原断言 `stream:true` → 400 `stream_unsupported`，改为流式同样先受预算约束（402）、非布尔 stream → 400
- SQLite：cloud 157 个通过（125 → 157；新增 test_streaming 23 个、N-09 2 个、其余为原有）
- PostgreSQL 16 临时集群（端口 55432，trust 认证）：`NEUXMIND_TEST_DATABASE_URL` 模式下 cloud 全套通过；确认测试确实落在 PG（库里 21 张表）。流式测试 SQLite ×5、PG ×3 重复运行均稳定
- 变异（git worktree + 当前 cloud/ 副本，未动共享工作区）：首轮 37 个——S01 模式匹配到 2 处（重写为 S01b）、S12 变异本身无效（泄漏的事务对象在函数返回时即被回收回滚，重写为 S12b 让事务在流式期间存活）；存活 S14（[DONE] 后不结束）、S15（被清理任务判定丢失后重复计量）、S19（通道无反压）、N10（response_format 外层多余键）→ 补 4 个测试；第二轮 S01b、S12b（挂起超时被杀）、S14、S15、S19、N10 全部被杀；S01b/S02/N01 在 PG 上也被杀。合计 39 个有效变异全部被杀
- 最终：cloud SQLite 160 OK、PostgreSQL 160 OK；sdk 31 OK（5 次中 2 次 `test_robot_client.test_cancel_and_wait_timeout` 偶发失败——SDK 对端侧运行时的计时测试，runtime Agent 正同时改 runtime/，与 cloud 无关）；spec OK；仓库根 tests OK（跳过 20）
- 清理：临时 PG 集群已停止并删除；worktree 已移除

## 第 02 轮审查修复

- 读审查报告 Q2-02/03/06 与 cloud_deadline.py；方案：墙钟看门狗 + 行级截止；保留命名空间（error→upstream.error、neuxmind.* 丢弃）+ WHATWG 切行；`*.delta` 全计；Echo responses 形状
- 代码完成后 SQLite 160 仍通过；补测试时发现自己的缺陷（上游 error 事件后正常结束 → 误归 usage_missing），已修
- SQLite 174、PostgreSQL 16（临时集群 55433）174 通过；cloud_deadline.py 0.5 s 结束（原 3.0 s）
- worktree 变异 18 个：F01/F08/F09/F13/F14/F18 首轮存活 → F18 是死代码（已删）；F08/F09/F13/F14 补测试后被杀；F01 等价（看门狗的 abort_reason 行级检查覆盖），保留为纵深防御
- sdk/tests 48 通过 ×2；临时 PG 与 worktree 已清理
