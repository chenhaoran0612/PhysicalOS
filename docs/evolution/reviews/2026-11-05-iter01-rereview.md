# 第 01 轮修复复审报告（runtime/、cloud/、spec/、brainos/）

- 复审对象：修复提交 `166171f`…`6c47f01`（spec、桌面端、runtime、cloud、docs 五组修复）。复审期间主分支又多了 `5ea8150`（版本号 0.8.0、根 README）和 `61055b3`（SDK 邮箱验证支持）。这两个提交没有改 runtime/、cloud/、spec/ 的代码，所以本报告的结论同样适用于 `61055b3`；测试在两个提交上都跑过。
- 方法：
  - 先用上一轮审查的原复现脚本逐条复验；脚本的前置条件因修复而失效时（例如需要邮箱验证），改用新流程，攻击本身不变。
  - 再针对修复引入的新机制写攻击脚本：停止代数、有界连接与停止名额、清单再校验、邮箱验证、会话白名单、超额规则、清理任务、桌面端仿真判定与解除急停。
  - 最后在 git worktree 里做了 15 处变异抽查。
- 隔离（协作协议第 7 条）：
  - 变异测试和所有实验都在 `git worktree`（scratchpad `rereview-wt`）中进行：实验和变异基于 `6c47f01`，终测时切到 `61055b3`。主工作区的产品代码一个字节都没动。复审结束时，主工作区 `git status` 里只有本报告，worktree 已删除。
  - PostgreSQL 用的是本机临时起的 16 版集群（独立端口和目录），验证后已删除。
- 临时脚本都在 scratchpad 的 `rereview/` 目录：
  - 原脚本副本：`orig/`
  - runtime：`rt_epoch_stress.py`、`rt_action_reset.py`、`rt_conn_attack.py`、`rt_evict_flood.py`、`rt_manifest_mutation.py`
  - cloud：`cloud_attack.py`、`pg_race.py`、`pg_login_race.py`、`pg_suite.py`
  - 桌面端：`desktop_attack.py`
  - 其他：`misc_checks.py`、`mutate_rereview.py`

## 总评

**能否作为第 01 轮正式版本同步给用户：否。满足下面两个条件后可以发布。**

1. **修复 N-01（P1，已复现）**：平台运营员的邮箱验证存在"预注册劫持"。
   - 攻击者抢先注册白名单里的邮箱。系统发给这个邮箱的验证链接会落进真正主人的邮箱。
   - 主人点击这个链接时不需要登录，验证的却是攻击者的账户。点击之后，攻击者手里已有的会话立刻获得平台运营员权限，可以给自己加额度（实测 200）。
   - 同时，真正的主人再也注册不了这个邮箱（409），而系统没有找回密码的功能。
   - 生产环境默认没有发信器，运营人员只能用 `verify-email` / `grant-operator` 命令处理这个"已被注册"的邮箱，结果同样是把运营员身份交给攻击者。
   - 这是 C-01 的新路径。C-01 原来的攻击路径已经关闭。
2. **发布用的版本必须包含 `61055b3`（SDK 修复）**。在 `6c47f01` 上，`sdk/tests` 的 `CloudTests` 会失败（`operator_required`）；在 `61055b3` 上，SDK 29 个测试全部通过。

其他方面：

- 上一轮的 **2 个 P0 和 8 个 P1 全部按原复现方式验证为已修复**，P2/P3 也全部修复。
- 修复的质量总体很高：
  - 停止代数在 2.3 万次并发 stop/reset/invoke 压力下 0 违例。
  - 被撤销的技能吞掉所有中断之后，再也碰不到本体。
  - 停止专用名额在每秒约 1000 个空闲连接的泛洪下，真停止 197/197 成功。
  - 在 PostgreSQL 上，清理任务与迟到的结果竞争 80 轮，没有双扣也没有漏扣。
  - 桌面端的仿真判定在大小写、空白、伪造 `device_run` 等变体下都"失败即关闭"。
- 新发现：1 个 P1、5 个 P2、5 个 P3，没有新的 P0。P2/P3 可以进入第 02 轮。
- 变异抽查：15 处里杀死 10 处，存活 5 处。其中 1 处属于冗余检查，另外 4 处是测试缺口，见"变异抽查"。

---

## 测试结果

| 测试集 | 提交 | 结果 |
|---|---|---|
| `runtime/tests` | 6c47f01 | 109 通过（约 33 s） |
| `cloud/tests`（SQLite） | 6c47f01 | 112 通过 |
| `cloud/tests`（PostgreSQL 16，每个测试重建 schema） | 6c47f01 | 112 通过 |
| `spec/tests` | 6c47f01 | 11 通过 |
| `tests/` | 6c47f01 | 82 通过（20 跳过，原因与基线相同：缺少打包的运行时或 agent 二进制） |
| `sdk/tests` | 6c47f01 | **26 个里 1 个出错**：`test_cloud.CloudTests` 的 setUpClass 报 `operator_required`。23:46 跑在 worktree 上 |
| `sdk/tests` | 主工作区（当时 SDK Agent 的改动未提交，后来提交为 61055b3） | 29 通过（23:46:49） |
| 全部重跑 | 61055b3 | 全部通过：runtime 109、cloud 112、spec 11、tests 82（跳过 20）、sdk 29。见文末"终测" |

---

## 复验表

结论分三档：已修复、部分修复、未修复。"证据"一列给出所用的脚本和实测输出。

### P0

| 编号 | 结论 | 证据 |
|---|---|---|
| C-01 白名单邮箱抢注即运营员 | **已修复**（原路径）；同一目标存在新路径，见 **N-01** | `orig/cloud_checks.py` 第 1 节原样运行：注册 `"  OPS@Example.com "` 后 `platform_operator: False`，自调额 403。`cloud_attack.py`：未验证时抢注白名单地址 → `False`/403 |
| C-02 会话透传任意参数，超额照常返回、不扣费 | **已修复**；有并发残余风险，见 **N-05** | `orig/fix_checks.py` 第 2 节：`n=50` → 400，上游未被调用；`max_completion_tokens=100000` → 下发 `{n:1, max_tokens:891}`，用量 911/1000，关闭扣 10 点。`cloud_attack.py` 逐项攻击：`stream`、`stream_options`、`n=2`、图片 part、text part 夹带 `image_url`、`modalities`、`service_tier`、`prediction`、`web_search`、`max_completion_tokens=10^7`、`store`、Responses 的 `item_reference`/`previous_response_id`/`include` 全部 400；工具描述大于剩余预算 → 402；超额 → 502 `usage_exceeds_reservation`，不返回输出 |

### P1

| 编号 | 结论 | 证据 |
|---|---|---|
| R-01 stop+reset 后，被撤销运行的在途调用仍能驱动本体 | **已修复**；相关的新问题见 **N-02** | `orig/rt_stale_after_reset.py`：`stopped run reached adapter after reset = False`。`rt_epoch_stress.py` A 部分：两轮各 6 s，共 23 110 次调用、7 494 次 stop。"客户端发出后才发生停止、却下发且未被撤销"的指令为 0；锁存期间下发的为 0。`rt_action_reset.py`：技能捕获所有 BaseException，等到 reset 之后再调 velocity、navigate、sleep、read，全部 `SkillStopped`；reset 之后适配器只收到合法新运行的一次 `start_action`，合法运行成功 |
| R-02 清单参数的符号和有限性不校验 | **已修复**（spec 层 + 监督器复核）；残余见 **N-04** | `orig/rt_limits.py` 原样运行：spec 直接拒绝 `max_vx=-1.0`。`orig/rt_limits_bypass.py`（绕过 spec 直接构造监督器）：负数、NaN、字符串一律拒绝启动 |
| R-03 关节限位比自由度短 | **已修复** | `rt_limits_bypass.py`：`short limits, joint5=100rad -> R`，`upper shorter -> R` |
| R-04 读不到 joint.state 时跳过单步限速 | **已修复** | `rt_limits_bypass.py`：`chunk no ctx` 和 `ctx shorter than dim` 都返回 SafetyRejected |
| R-05 网关没有连接超时和线程上限 | **已修复** | `orig/rt_slowloris.py`：400 个半截连接时线程数 5→41，10 s 后回到 5。`rt_conn_attack.py`：逐字节慢发 10.5 s 后被断开；普通名额被 32 个慢连接占满时真停止 5/5 成功；8 个线程发送约 7 700 个未认证的 `POST /v2/stop` 时真停止 20/20 成功。`rt_evict_flood.py`（服务端独立进程）：每秒 50、200、1000 个已建立的空闲连接，加上普通名额全满，真停止 197/197、197/197、197/197。失败只出现在"非阻塞 connect 每秒数十万次"把双核机器的 CPU 和 backlog 打满时（真停止 1/4 成功），属于网络层 DoS，修复 Agent 已列为遗留（防火墙或硬件急停） |
| R-06 ROS 2 目标发送中，stop 取消不了 | **已修复**（替身验证；真实 Nav2 未验证） | 代码复核 `ros2.py:190-265`：`cancel()` 先置标志再看 handle，`_on_goal_sent` 先发布 handle 再看标志，两边至少有一边发出取消。`stop_adapter` 同时调用 Nav2 的"取消全部目标"；`on_done` 清理已完成的 handle。`runtime/tests/test_ros2_and_cli.py` 覆盖 |
| R-07 v1 stop 锁存，桌面端没有解锁入口 | **已修复** | `desktop_attack.py`，真实 runtime + HTTP，补上了修复 Agent 遗留的那条集成测试：start → stop（运行时锁存）→ 锁存时 start 在本地被拒 → 不带授权的 reset 被拒 → reset 成功、运行时解锁 → 相同 request_id 返回记录、不重发 → start 成功。`/api/robots/reset`：未登录 401；用户 B 拿用户 A 的 robot_id → 400（工作空间隔离），运行时保持锁存 |
| R-08 仿真结果被记为现场证据 | **已修复**；残余见 **N-10** | `desktop_attack.py`：`Simulation://`、前导空格、`Device_Run`、缺少 `evidence_kind`、"清单 simulated=true 但结果声称 device_run" 都存为 simulation，部署被拒。本体改为声明仿真后，旧的真机报告不能部署。清单里 `simulated:"false"`（字符串）时连接被拒。仿真体隐藏 `simulated` 字段时，摘要与运行时不一致，被拒。ROS 2 的 `simulated` 来自配置或 `use_sim_time`；`test_v1_compat` 已改为断言拒绝 |

### P2 / P3

| 编号 | 结论 | 证据 |
|---|---|---|
| R-09 看门狗与 SQLite 写入共用线程 | **已修复**；新问题见 **N-03** | `orig/rt_watchdog_store.py`：0.2 s 触发（修复前 3.08 s） |
| R-10 锁存持久化异步，且与 reset 竞争 | **已修复** | `misc_checks.py`：进程 `os._exit` 之后重启默认锁存，并给出原因；正常 close 后不锁存；stop 请求体里的原因被持久化。`rt_stop_locks.py`：持久化键为 `[boot, epoch, phase]` |
| R-11 v2 直接调用的 action 没有超时 | **已修复** | `rt_misc.py`：deadline 600 s |
| R-12 参数校验缺口 | **已修复** | `rt_misc.py`：`"ab"`、列表、NaN 都返回 400（重试同一 id 结果相同，不再变成 unknown）；validate 返回失败项 |
| R-13 `x.*` 运动判定不一致 | **已修复** | `spec.is_motion('x.unitree.dance') = True` |
| R-14 reset 重放返回缓存状态 | **已修复** | `misc_checks.py`：重新锁存后重放同一 reset，返回 `{'latched': True}` |
| R-15 超大整数变成 502 | **已修复** | `10**400` 返回 SafetyRejected |
| R-16 路径前缀冲突 | **已修复** | `a.py` 与 `a.py/b.py` 同时存在时返回 400 |
| C-03 登录限速 | **已修复** | `orig/cloud_checks.py` 第 4 节：攻击者地址被限速时，受害者从自己的地址登录 200。`pg_login_race.py`（PG16）：20 次并发错误密码 → 3 个 401、17 个 429；12 次同 IP 注册 → 3 个成功；把 advisory lock 换成空操作作对照 → 17 个 401、12 个成功 |
| C-04 上游错误分类 | **已修复** | 400/401/429 为 Rejected；408/500/502/504 为 Unknown |
| C-05 预留泄漏 | **已修复** | `pg_race.py`，见下文"清理任务竞态" |
| C-06 错误文本泄露凭据变量名 | **已修复** | 用户看到的是"上游服务暂不可用" |
| C-07 scrypt 参数偏低 | **已修复** | 默认 `scrypt_log_n=17`；生产环境设得更低会拒绝启动 |
| C-08 prod 暴露 API 文档 | **已修复** | prod 下 `/api/v1/docs` 和 `openapi.json` 都返回 404 |
| C-09 prod 允许上架 echo | **已修复** | 上架返回 400 `upstream_not_allowed`；prod 注册响应不带 `dev_verification_token` |
| C-10 成员管理 | **已修复** | admin 移除 owner → 403；admin 移除 developer → 200；admin 改 admin → 403 |
| C-11 运营可处理 held 预留 | **已修复** | 409 `reservation_in_flight` |

---

## 新机制攻击：没有攻破的部分

这些攻击都没有成功，列在这里作为修复有效的证据。

- **停止代数**
  - 6 个调用线程、1 个 stop 线程、1 个 reset 线程跑 6 s 压力测试，分无延迟和适配器 1 ms 延迟两种，各一轮，0 违例（`rt_epoch_stress.py` A）。
  - 技能吞掉中断之后调用 `progress`/`log` 会被静默忽略，运行记录不再变化。被撤销的运行在 finalize 之后不再改动记录。
- **连接限制**
  - v1 网关以 HTTP/1.0 应答，每个请求一个连接，所以"401 之后保持连接慢滴"行不通（实测连接被关闭）。
  - 未认证的 stop 请求被识别为 stop 之后立即返回 401 并关闭，占不住停止专用名额。
  - 503 响应只有通用的"连接数已满"文案，不泄露任何信息。它在认证之前返回，所以攻击者可以借此探测名额是否已满，这一点影响可以忽略。
- **清单再校验**
  - 适配器在运行期间让 `manifest()` 返回更宽的限位或 `simulated=false`，不起作用：运行时只用启动时校验过的副本，50 rad 的目标仍被拒绝。
  - 但是这个副本是浅拷贝，见 N-04。
- **邮箱验证**
  - 令牌重放、过期、重发之后用旧令牌，都返回 400。
  - 用别人的令牌只会验证令牌的主人，不会验证提交令牌的会话。
  - 大小写和首尾空白的变体返回 409；第 6 次重发返回 429。
  - 未验证用户无论用会话还是用 API Key，都不能购买、调用 infer 或开会话（403 `email_unverified`）。
  - 在验证之前创建的 Key，在创建者验证之后可用。这与"Key 的权限跟随创建者"的设计一致。
  - 已验证组织里的未验证成员被拒。developer 被降级为 operator 之后，其 Key 立即返回 403 `scope_missing`。
- **超额处理（resolve）的权限**：非运营员 403；用运营员的 API Key 处理 403 `session_required`；补扣点数超过记录的超额 400。
- **清理任务竞态**（`pg_race.py`，PostgreSQL 16，`stale_call_seconds=0`，迟到的结果与 sweep 用 barrier 同时放行）
  - infer 40 轮：14 轮 sweep 先判为丢失、迟到的结果再正常结算（这是设计行为），26 轮正常结算。每笔预留最多结算一次。
  - 会话 40 轮：27 轮计入用量，13 轮判为丢失（`call_marked_lost`，关闭时转核对）。没有出现"既计入又判丢"或"都没算"的情况，inflight 都回到 0。
  - 每轮之后都核对：钱包余额等于账本合计；reserved 不为负。

---

## 新发现

### P1

#### N-01 · P1 · 运营员邮箱可被"预注册劫持"（已复现）

- **位置**：
  - `cloud/neuxmind_cloud/auth.py:190-209`：`verify_email` 不需要登录，"possession of the token is the proof"。
  - `auth.py:102-106`：`is_platform_operator` 判定为"已验证邮箱"且（命令授予或白名单）。
  - `auth.py:212-221, 458-468`：`mark_email_verified` 和 `grant_operator` 只按邮箱查账户。
  - 系统没有找回密码或重置密码的功能。
- **问题**：
  - 攻击者先用白名单里的地址（或者将来要用 `grant-operator` 授予的地址）注册，密码由攻击者设定。
  - 系统把验证邮件发到这个地址，也就是真正主人的邮箱。
  - 主人点击链接时不需要登录，验证的是**攻击者的账户**；攻击者在注册时拿到的会话会立即获得平台运营员权限。
  - 主人自己去注册时得到 409"此邮箱已注册，请登录"，而且没有找回密码可用。
  - 生产环境默认没有发信器。README 要求运营人员用 `verify-email` 和 `grant-operator` 命令处理，这两个命令同样会把一个"别人设了密码"的账户提升为运营员。
- **复现**（`cloud_attack.py`，测试环境，记录型发信器充当"主人的邮箱"）：
  ```
  attacker registers " OPS2@NEUXMIND.TEST " (password chosen by attacker) -> 201, platform_operator=False
  real owner registers ops2@neuxmind.test                                   -> 409 email_taken
  owner clicks the link found in the ops2 mailbox (no login)                -> 200
  attacker's existing session: platform_operator=True, POST /admin/wallets/{own}/adjust 1e6 -> 200
  ```
- **修复建议**：
  1. 验证必须在**该账户自己的登录会话**中完成（`/auth/verify-email` 要求会话，且会话用户等于令牌用户），或者在验证页要求输入该账户的密码。主人点击别人注册的账户的链接，验证不会成功。
  2. 验证成功时吊销此前签发的所有会话。
  3. 平台运营员账户只通过管理命令创建：命令自己设置初始密码，或者要求首次登录时改密码。`grant-operator` 遇到"通过公开注册创建"的账户时，打印注册时间和来源 IP，并且要求显式加上 `--i-registered-this-account`。
  4. 增加找回密码。
  5. 补反向测试：攻击者抢注 → 主人凭邮件令牌验证 → 攻击者的会话仍然**不是**运营员。
  6. 白名单在 prod 下可以考虑完全不生效（只用命令授予）。

### P2

#### N-02 · P2 · 急停锁存期间到达的运动请求，被慢存储拖到 reset 之后仍会执行（已复现，注入慢存储）

- **位置**：
  - `runtime/neuxmind_runtime/core.py:256`：在进入账本之前记录代数。
  - `core.py:272` → `ledger.py:60-66`：账本写盘。锁存检查发生在账本之后的 `do()` 里。
  - `runner.py:162`：运行的代数在 `_admit` 时才记录，而 `_admit` 本身也在账本之后。
  - `safety.py:488-511`：reset 不推进代数。
- **问题**：
  - 一个请求在急停锁存期间到达，例如客户端的自动重试，或者网络里排队的旧指令。
  - 如果它在账本写盘（SD 卡 fsync 卡顿，或者账本锁被其他请求占住）期间被拖住，而操作员恰好在这时完成了 reset，那么它记录的代数和当前代数相同、锁存已解除，于是被正常下发。
  - command（`base.velocity`）、action（`base.navigate_to`）和技能运行都会受影响；action 和技能的代数在 `_admit` 时才取，所以窗口更大。
  - ADR-009 设锁存的原因正是"防止停止后被排队中的旧指令或自动重试重新带动机器人"，这里属于部分失效。
- **复现**（`rt_epoch_stress.py` B 部分。存储代理让这一个 request_id 的 RUNNING 记录写盘阻塞，直到 reset 完成）：
  ```
  stop -> latched=True epoch=1
  base.velocity 请求在锁存期间发出（卡在写盘）；reset -> {'latched': False}
  -> 响应 {'accepted': True}；reset 之后适配器收到 ('invoke', 'base.velocity')
  base.navigate_to 同上 -> {'run_id': ..., 'status': 'running'}；适配器收到 start_action
  ```
- **修复建议**：
  - 在请求到达时（`core.invoke` / `run_skill` / `v1_start` 的入口，进入账本之前）同时记录代数和"当时是否锁存"。锁存期间到达的运动请求直接返回 409 `estop_latched`，不进入账本。
  - `_admit` 改用到达时记录的代数。
  - 补测试：用慢存储替身模拟"锁存期间到达、reset 之后才执行"，断言适配器零下发。

#### N-03 · P2 · 一个卡在适配器里的速度指令会让看门狗失效（已复现）

- **位置**：`bus.py:137-141`（`adapter.invoke` 在 `velocity_lock` 内执行）、`safety.py:445-446`（看门狗 `acquire(timeout=0.01)` 失败就直接返回）。
- **问题**：
  - 上一条速度设定值的超时是 0.2 s。下一条速度指令在 `adapter.invoke` 里阻塞（串口重传、驱动卡死）时，一直持有 `velocity_lock`；看门狗每次拿锁失败就跳过，零速度和 `adapter.stop()` 都不会发出。
  - 实测上一条设定值保持了 3 s 以上，直到阻塞解除。
  - R-09 修好了"存储 IO 拖慢看门狗"，这里是同一类问题的另一个入口。
- **复现**（`rt_epoch_stress.py` C 部分）：`watchdog zero-velocity first sent after None s`，即阻塞的 3 s 内一次也没有发。
- **修复建议**：
  - 看门狗发现已过期又拿不到锁时，不经锁直接调用 `adapter.stop()`（停止路径本来就不经锁）。
  - 或者把适配器调用移到锁外，只在锁内更新 deadline。
  - 补"阻塞适配器"测试。

#### N-04 · P2 · 监督器的限位与适配器共享嵌套列表，运行期原地修改会绕过启动校验（已复现）

- **位置**：
  - `spec/neuxmind_spec/manifest.py:95`：`{"params": params}` 原样引用适配器的对象。
  - `safety.py:160`：`dict(c.get("params", {}))` 只做浅拷贝。
  - `adapters/sim_arm.py:13-14, 41`：返回模块级的 `LOWER` / `UPPER` 列表。
  - `adapters/ros2.py:326-327`：每次返回同一个 `self._manifest`。
  - `safety.py:290`：`q < lo - eps or q > hi + eps` 遇到 NaN 恒为假。
- **问题**：
  - 适配器在运行中原地修改自己的限位列表（例如 ROS 2 适配器从参数服务器或 URDF 刷新限位）时，监督器直接使用未经校验的新值。
  - NaN 限位会让任意目标通过，因为比较恒为假。放宽的限位也被直接采用。
  - R-02 的"启动时复核"因此只在启动那一刻成立。
- **复现**（`rt_manifest_mutation.py`，SimArm）：
  ```
  supervisor lower list is the adapter's module list object: True
  LOWER[5]=UPPER[5]=NaN (in place) -> target joint6=50 rad -> succeeded
  in place ±1e6                    -> target 1000 rad 下发到适配器
  ```
- **修复建议**：
  - `validate_manifest_v2` 返回深拷贝，监督器再把限位冻结成 tuple。
  - 限位比较改写成 `not (lo - eps <= q <= hi + eps)`，NaN 时也拒绝。
  - 补测试：启动后原地修改适配器的清单，断言没有影响。

#### N-05 · P2 · 超额锁定只拦"之后"的调用，同一会话的并发在途调用可以全部超额（已复现，不合规上游替身）

- **位置**：`gateway.py:312-313`（只在调用入口检查 `overage_calls`）、`gateway.py:367-379`。
- **问题**：
  - 同一会话里 6 个调用同时在途，上游每次都报告超出 hold 的用量时，6 次都按各自的 hold 计费，记下 6 条超额记录（每条 49 点未收）。
  - ADR-012 写的"会话因此锁定"对并发调用无效。每个会话能拿到的免费超额约为"并发数 × 单次超额"。
  - 风险的前提是上游不遵守服务端写入的输出上限，例如推理 token 不受 `output_param` 约束的上游、`output_param` 配错的模型卡，或者上游额外注入的系统提示。对于遵守上限的上游，正常情况下不会超额。
  - 超额能否追回取决于可用余额（ADR-012），攻击者可以把余额花光，让平台追不回。
- **复现**（`cloud_attack.py`）：6 个并发调用全部返回 502 `usage_exceeds_reservation`，产生 6 条超额记录，`overage_credits=[49]*6`。
- **修复建议**：
  - 会话内的并发在途调用数设上限（建议 1–4）。
  - 模型卡显式声明"上游保证遵守输出上限"；没有声明的模型不允许开会话。
  - 运营后台按组织汇总超额，超过阈值自动冻结该组织的网关。

#### N-06 · P2 · （遗留，未修）动作块的 `dt` 没有上限，单步限速可被放大到失效

- 修复 Agent 自己的脚本 `fixcheck/rt_limits_bypass.py` 已经发现：`dt=1e6` 时 `max_step = vmax·dt`，`[[3,-3]]` 原样下发。他们把它列入了遗留，复审确认它仍然存在。
- 适配器如果按自己的控制周期执行动作块、不按 `dt` 节奏执行，单步限速就完全失效。
- **建议**：`dt` 不得大于 `1/rate_hz` 的若干倍，或者设一个绝对上限（例如 1 s）；动作块的步数也要设上限。

### P3

| 编号 | 位置 | 问题（已复现，除非另注） | 建议 |
|---|---|---|---|
| N-07 | `routes/admin.py:232` | 超额只有 1 个 token、换算成点数为 0 时，`max(row.overage_credits, 1)` 仍允许补扣 1 点（实测 200） | 上界用 `row.overage_credits`，为 0 时只允许 waive |
| N-08 | `routes/admin.py:219-239` | 处理超额时没有锁住该行。PG 上并发"waive + charge"两个都返回 200，最终状态是 charged，waive 被覆盖（3/3）；并发"charge + charge"两个都返回 200，审计记了两次扣费，但账本只有 1 条（引用唯一，所以没有双扣） | 对 `OverageRecord` 加 `FOR UPDATE`，或者用条件更新 `status='open'` |
| N-09 | `session_params.py:78-83, 170-174` | `tool_choice`（dict）和 `messages[].tool_calls[]` 里的未知嵌套键原样转发给上游，例如 `tool_choice.max_completion_tokens`。字节会计入输入上界，但是否影响计费取决于上游 | 嵌套结构也按白名单重建 |
| N-10 | `brainos/contracts.py:79` | 设备结果里 `simulated` 不是布尔值（`"true"` 或 `1`），而其他字段像真机时，记为 onsite（实测部署成功）。与 ADR-011 的"失败即关闭"不一致。前提是设备同时声明 `device_run`、清单未声明仿真 | 结果中 `simulated` 存在但不是 `False` 时，一律按仿真处理 |
| N-11 | `cloud/routes/gateway_routes.py:22,29,51` | 惰性清理在认证之前、在请求线程里同步执行。任何未认证的请求都能触发（每 30 s 最多一次），触发时让这次请求变慢（代码推理） | 认证之后再触发，或者只依赖后台线程 |

---

## 变异抽查（worktree，`mutate_rereview.py`）

每次只改一处，跑对应目录的全部测试，然后逐字节恢复。结束时 worktree 的 `git status` 为空。

| 变异 | 结果 | 说明 |
|---|---|---|
| M1 `ensure_epoch` 永不拒绝 | 杀掉 | `test_call_from_before_stop_is_refused_after_reset`、`test_in_flight_call_dropped_after_stop_and_reset` |
| M2 `bus._pre_dispatch` 去掉代数复核 | **存活** | 冗余检查：`check()` 就在它前面一行，而且也比对代数；技能调用另有 `guard` 兜底。两者之间只有一个极小的窗口，属于近似等价的变异，可以接受 |
| M3 `after_dispatch` 只看锁存、不看代数 | 杀掉 | `test_stop_and_reset_while_command_in_flight_is_undone` |
| M4 `reset` 清除锁存后不再复核代数 | **存活** | 没有覆盖"stop 恰好落在 reset 的检查和清除之间"的测试。这是 R-10 修复的核心竞态，建议在适配器的 `reset()` 里注入一次 `emergency_stop` 来稳定复现 |
| M5 停止专用名额永不驱逐 | 杀掉 | `test_stop_with_reserved_slots_held_by_idle_clients` 等 |
| M6 停止专用名额使用完整 10 s 期限（不缩短为 1 s） | **存活** | 现有测试只用"完全不发数据"的空闲连接，驱逐机制就足够了；没有测"慢发、但不是 stop"的连接要在 1 s 内释放名额 |
| M7 监督器不检查 lower/upper 长度 = dof | 杀掉 | `test_bad_limits_refuse_to_start` |
| M8 锁存持久化忽略顺序键 | 杀掉 | `test_stale_latch_write_cannot_overwrite_newer_state` |
| M9 会话接受未知参数 | 杀掉 | `test_parameters_outside_the_allow_list_are_refused` |
| M10 未验证邮箱也能是运营员 | 杀掉 | `OperatorSquattingProdTest` 等 3 个 |
| M11 验证令牌可重复使用 | 杀掉 | `test_register_verify_single_use_and_hash_only` |
| M12 清理任务不检查 `status == pending`，会重复处理已结算的调用 | **存活** | 没有"迟到的结果先结算、sweep 后到"的测试。变异后的代码会把已结算的调用再标为丢失，`unknown_calls+1`、inflight 再减一次（被 `max(0, …)` 掩盖）。`pg_race.py` 证明当前代码是正确的，但测试守不住它 |
| M13 会话超额时仍返回输出 | 杀掉 | `test_overage_in_session_charges_the_hold_withholds_output_and_locks` |
| M14 桌面端 `simulation://` 判断区分大小写 | **存活** | 没有 `Simulation://` 或带空白的测试。实际防线仍成立（`desktop_attack.py`），但测试守不住 |
| M15 桌面端忽略结果中的 `simulated=true` | 杀掉 | `test_simulation_markers_on_a_non_simulated_manifest_still_mean_simulation` |

**结果：15 处杀死 10 处，存活 5 处。** M2 属于冗余；M4、M6、M12、M14 是测试缺口，建议第 02 轮补测试。

另外：第一次（中途中止的）变异运行时，本机同时在跑其他压力实验，`test_stop_within_one_second_under_many_half_open_connections` 失败过一次。之后在 2 个满载 CPU 进程的干扰下，单独连跑 3 次都通过；完整套件又连跑 3 次也都通过。所以目前判断它只在极端负载下偶发失败，暂不定级，留意即可。

---

## 对第 02 轮的建议优先级

1. **发布前（第 01 轮收尾）**
   - N-01：验证必须由本账户的会话完成；验证后吊销旧会话；运营员只由命令创建；补反向测试。
   - 用 `61055b3`（或之后的提交）作为发布版本，并在发布提交上重跑全部测试。
2. **第 02 轮内**
   - N-02：在请求到达时判定锁存，代数在请求入口记录。
   - N-03：看门狗拿不到锁时直接停车。
   - N-04：清单深拷贝并冻结，限位比较对 NaN 安全。
   - N-06：动作块的 `dt` 和步数设上限。
   - N-05：会话并发上限；模型卡声明是否遵守输出上限。
   - 补齐存活变异 M4、M6、M12、M14 对应的测试。
3. **随后**
   - N-07 到 N-11。
   - 真实 SMTP、ROS 2 和 Nav2 实机验证。
   - 网络层 DoS（SYN 泛洪）的部署指引：防火墙，加上硬件急停作为主通道。

## 终测（61055b3，worktree，00:14–00:16）

| 测试集 | 结果 |
|---|---|
| `runtime/tests` | 109 通过（另外又连跑 2 次，都通过） |
| `cloud/tests` | 112 通过 |
| `spec/tests` | 11 通过 |
| `tests/` | 82 通过（20 跳过） |
| `sdk/tests` | 29 通过 |
