# NeuxMind 端侧运行时（`runtime/`）

装在机器人（或旁边的 Linux/Jetson 计算机）上的运行时：把本体映射为标准能力，安全地执行可移植技能，并通过 HTTP 网关对桌面控制台（v1 兼容）和 SDK/云（v2）开放。

> 状态：第 04 轮（能力一致性测试套件、适配器脚手架；第 03 轮：Python 3.10 与非 root 运行、端侧模型安装、遥测持久化；第 02 轮：技能进程隔离、异步现场测试、可安装包）。**全部验证均在仿真本体与协议替身上完成，未在任何真机上验证。** ROS 2 桥接只在 rclpy 测试替身上跑通过一致性测试，从未接触真实 ROS 2。

- 仅依赖 **Python 3.10+** 标准库（Jetson JetPack 6 / Ubuntu 22.04 自带 3.10）；数据结构以 `spec/neuxmind_spec/` 为唯一权威。
- 测试：`python3 -m unittest discover -s runtime/tests`（仓库根目录）或在 `runtime/` 下 `python3 -m unittest discover -s tests`。
- 新本体接入：[§3](#3-写适配器把本体接进来)（脚手架 → 实现 → 一致性测试）；一致性测试：[§3.1](#31-能力一致性测试conformance)；脚手架：[§3.2](#32-适配器脚手架scaffold)。
- 安装：见 [§0](#0-安装)；非 root 部署：见 [§0.2](#02-以普通用户运行尽力而为模式)；技能沙箱：见 [§2.2](#22-技能进程隔离沙箱)；异步现场测试：见 [§5.1](#51-异步现场测试)；端侧模型安装：见 [§5.2](#52-端侧模型安装modelinstall)；遥测持久化：见 [§5.3](#53-遥测持久化与回放)。

## 0. 安装

```bash
pip install -e spec -e runtime          # 仓库根目录；包名 neuxmind-spec、neuxmind-runtime
neuxmind-runtime serve --adapter sim_quadruped --state-dir /var/lib/neuxmind
neuxmind-runtime check runtime/examples/skills/status_report --adapter sim_arm
```

### 0.1 Python 3.10（Jetson / Ubuntu 22.04）

`requires-python = ">=3.10"`。运行时与测试不使用 3.11+ 的标准库或语法（`tomllib` 只在测试里用，3.10 下回退到 `tomli` 或内置小解析器；没有 `ExceptionGroup`/`except*`、`typing.Self`、`datetime.UTC`、`TaskGroup` 等）。第 03 轮在 3.10.20 与 3.13 上、root 与普通用户下各跑全部 `runtime/tests`。

```bash
python3.10 -m venv /opt/neuxmind/venv
/opt/neuxmind/venv/bin/pip install -e spec -e runtime
```

**root 运行时的解释器必须对技能用户可执行**：技能子进程降权为 `neuxmind-skill`/`nobody`，它要能执行解释器并读到标准库。系统 `/usr/bin/python3.10` 没问题；uv/pyenv 装在 `/root/.local/...`（0700）下的解释器 `nobody` 执行不了。运行时按"虚拟环境路径 → 真实路径 → 同版本系统解释器（`/usr/bin`、`/usr/local/bin` 下的 `python3.X`）"依次选择，都不行时 `/v1/status` 的 `sandbox.python_usable=false`、`sandbox.level="best-effort"` 并给出原因，启动时告警，技能运行明确失败（不会卡住）。用 `--sandbox-python` 指定一个技能用户可执行的同版本解释器。测试套件在这种环境下会以该原因跳过沙箱测试（可设 `NEUXMIND_TEST_SANDBOX_PYTHON`）。

### 0.2 以普通用户运行（尽力而为模式）

普通用户运行时**无法降权到独立用户，通常也没有可写的 pids cgroup**。此时：

| 方面 | root 运行时 | 普通用户运行时（尽力而为） |
|---|---|---|
| 技能用户 | 降权为 `neuxmind-skill`/`nobody`，清空附加组 | 与运行时**同一用户** |
| 状态目录、技能包 | 技能读不到、改不了 | 技能**可以**读写（可改锁存记录、影响下次启动）；运行时设 non-dumpable，技能读不到运行时 `/proc` 内存/环境、不能 ptrace |
| 进程树收回 | 可写 cgroup 时立即、确定 | 运行时设 `PR_SET_CHILD_SUBREAPER`；运行结束时对本运行时**全部**未跟踪的、不在运行时会话中的同 uid 后代**先 SIGSTOP 再 SIGKILL**（停住的进程不能再 fork，fork 炸弹追不上），反复直到稳定；之后 30 s 内监视线程每 0.5 s 再扫一次 |
| 模型目录 | 技能（另一 uid）无法修改 | 文件 0444、目录 0555，技能不先 `chmod` 就改不了；但同一用户可以 `chmod`，只防误操作 |
| `/v1/status` | `sandbox.level="full"`（有 cgroup 时） | `sandbox.level="best-effort"`、`privilege_drop=false`、`best_effort_reasons=[…]` |

识别"技能后代"的规则（第 03 轮审查 Q3-05 后）：目标必须同 uid、不在运行时自己的会话中，**并且**有它来自技能进程的正面证据——它的会话是某个技能进程启动时的会话，或者它的环境里带技能标记 `NEUXMIND_SKILL_SANDBOX=<本进程随机值>`（技能子进程的环境里设置，所有后代继承；不是机密）。运行时或适配器启动的辅助进程（无论是否新会话）两者都没有，**永远不会被发信号或 `waitpid`**，其退出状态仍归启动它的代码（此前会被杀掉并被收割者拿走退出状态，适配器看到"正常退出 0"）。剩余缺口：同用户技能若既清空环境又 `setsid`，可逃过这一尽力而为的扫描——提供 cgroup 或以 root 降权运行即可确定收回。

停止路径、安全监督、停止代数在两种模式下完全相同；"尽力而为"只涉及技能与运行时之间的隔离强度。生产环境请以 root 启动（或给技能配置独立的 `--sandbox-user`，这需要 root）并提供 cgroup（systemd `Delegate=yes`）。

测试：`runtime/tests` 在普通用户下全部通过，2 个需要 root/cgroup 的用例带原因跳过（"运行时不是 root：技能与运行时同用户…"、"无可写的 pids cgroup"）。第 02 轮用户机器上"test_sandbox 在非 root + 3.10 下 E 并卡住、test_gateway 1 个失败"的根因：(1) 收割者当时只在 root 降权时工作，fork 炸弹测试留下的 setsid 逃逸进程在测试结束后继续 fork，把机器负载压满，后续测试纷纷"运行超时"；(2) `test_skill_process_dies_with_the_runtime` 读子进程输出没有超时，子运行时起不来时永远阻塞。两者都已修复（见 EVOLUTION_ENTRY 第 03 轮）。

### 0.3 不安装直接运行

不安装也能用：`python3 -m neuxmind_runtime ...`（在 `runtime/` 下）或 `python3 -m runtime.neuxmind_runtime ...`（仓库根目录）；`neuxmind_runtime/_paths.py` 会按相对路径（或 `NEUXMIND_SPEC_PATH`）找到 `spec/`。运行时仍只用标准库；唯一依赖 `neuxmind-spec` 也是纯标准库。

## 1. 架构

```
           桌面控制台 brainos (v1)        SDK / nx / 云 (v2)
                        │  HTTP + Bearer token  │
┌───────────────────────▼───────────────────────▼──────────────────────────┐
│ gateway.py   有界连接 HTTP 服务；/v1/* /v2/*；停止专用名额；先停车再读体   │
├──────────────────────────────────────────────────────────────────────────┤
│ core.py      EdgeRuntime：v1 应用 ↔ 技能、v2 业务、重启恢复、锁存持久化     │
│ ledger.py    request_id 幂等账本（sqlite）   evaluations.py 异步现场测试    │
│ runner.py    运行管理：每个运行一个线程；超时、取消、宽限期后撤销能力访问     │
│ skills.py    技能包存储、参数校验、SkillContext（技能唯一 API，主进程侧）     │
│ sandbox.py   技能子进程宿主：启动/降权/限额、代理 ctx 请求、停止通知与强杀    │
│   └─ JSON 行协议（socketpair）──► sandbox_child.py：技能子进程（每次运行一个）│
├──────────────────────────────────────────────────────────────────────────┤
│ bus.py       能力总线：spec 输入校验 → 安全监督 → 适配器 → spec 输出校验     │
│ safety.py    安全监督：授权、限幅/拒绝、看门狗、急停锁存、停止代数、清单复核 │
├──────────────────────────────────────────────────────────────────────────┤
│ hal.py       RobotAdapter / ActionHandle 契约                              │
│ adapters/    SimQuadruped · SimArm（仿真）· ros2（Ros2Bridge）· module:Class │
└──────────────────────────────────────────────────────────────────────────┘
  store.py (sqlite)   events.py (无锁发事件 + 持久化 SQLite 环形存储)   models.py (原生 model.install)
  clock.py (RealClock/time_scale, ManualClock)
  conformance.py (能力一致性测试，在 EdgeRuntime 之上执行)   scaffold.py (适配器脚手架)
```

一次技能里的 `ctx.call("base.velocity", vx=2.0, wz=0)`：
技能子进程里的代理 `ctx` 把请求经 socketpair 发给主进程（`sandbox.py` 严格校验消息）→ 主进程的
`SkillContext`（检查停止/撤销/停止代数/子进程仍存活、是否声明了该能力）→ `CapabilityBus.invoke`（`validate_payload` 输入）→ `SafetySupervisor.check`（锁存、停止代数、授权、把 vx 裁剪到 `max_vx` 并记 `safety.limited` 事件；限幅异常一律拒绝；结果须全为有限值）→ 下发前最后检查（运行仍有效、停止代数未变）→ `adapter.invoke` → `after_dispatch`（武装看门狗；若在途期间急停触发、或发生过 stop+reset，则再次停车）→ `validate_payload` 输出。

## 2. 安全模型与信任边界

**不变式（对应 INTERFACES_V1）**

| # | 规则 | 实现 |
|---|---|---|
| 1 | request_id 幂等 | `ledger.py`：同 id 同内容 → 原结果（被拒的请求也原样返回拒绝）；同 id 不同内容 → 409 `request_id_reused`；重启时 `running` 记录改为 `unknown`，再次提交返回 409 `outcome_unknown`，绝不重放 |
| 2 | 运动需授权并经安全监督 | 所有 `safety=motion` 能力与所有技能运行都要求 `hardware_authorized=true`；所有能力调用只能经 `CapabilityBus` |
| 3 | 停止路径不阻塞 | 见下 |
| 4 | 仿真可见 | manifest `simulated=true`（v1 视图也携带 `simulated`/`vendor`/`model`），`/v1/status`、`/v2/manifest`、技能 `ctx.simulated`、`/v1/evaluate` 的 `simulated`/`evidence_kind="simulation"`/`simulation://` 证据引用。按契约，调用方不得把它当现场证据（桌面端负责拒绝）；ROS 2 适配器的 `simulated` 来自配置 `simulated`/`use_sim_time` 或节点参数 `use_sim_time` |
| 5 | 停止之前发起的指令永不再下发 | 停止代数（见下） |
| 6 | 停止在连接被占满时仍可用 | 停止专用名额（见“连接上限与停止保障路径”） |

**停止路径**（`/v1/stop`、`/v2/stop`、`safety.stop` 能力、技能内 `ctx.call("safety.stop")`）：
1. 认证（`compare_digest`，无锁）→ 2. 停止代数 +1 → 3. 设置锁存 `threading.Event` → 4. 直接调用 `adapter.stop()` → 5. 给每个运行置停止事件并取消其动作句柄，**然后**给技能子进程发一次非阻塞 `SIGTERM`（不等待它）→ 6. 发事件 → 7. 置“待持久化”标志（常驻持久化线程负责写盘；停止路径**不创建线程**，线程耗尽时 stop 仍成功）→ 返回。子进程的强杀（`SIGKILL`）由监视线程在宽限期后执行，不在停止路径上。
请求体在停车**之后**才读取，格式错误也不影响停车；其中的 `reason` 会记入锁存记录。不经过账本、数据库、运行管理锁、队列、模型或网络。响应 `{accepted, stopped, latched, stopped_runs, stop_count}`（`stop_count` 为本次停止的序号，第 05 轮新增）。
`keep_reason: true`（再停，例如一致性测试 `--mode client`）是**比较交换**（第 05 轮 R4c-02）：停止照常执行并计数；之后只有当本次停止仍是最新一次停止、且锁存原因仍是本次停止写入的占位值 `"stop"` 时，才把原因恢复为停止前的原因（响应 `latch_reason_kept: true|false`）。因此请求体很慢的再停不会把操作员在此期间写入的更新原因改回旧原因。普通 `reason` 照旧按到达顺序覆盖。

**停止代数（epoch）**：监督器维护一个只增不减的计数，每次 stop 与每次 reset 都 +1。HTTP 请求（`invoke`、技能运行、`/v1/start`、`/v1/evaluate`）在**到达时**（写账本之前）记下代数，运行准入时再比对一次；因此在锁存期间到达、又被慢存储拖到 reset 之后的请求一律作废（复审 N-02）。监督器检查、下发前最后检查（在任何可能阻塞的状态读取之后）、下发后检查、`SkillContext` 的每次 `call`/`read` 都要求代数不变，否则拒绝（409 `stale_after_stop`，事件 `safety.stale_rejected`）；若 stop 与 reset 都发生在一次适配器调用期间，下发后立即再停车。因此被停止/撤销的运行，即使 reset 之后其在途调用返回，也**永远**不能再驱动本体。

**锁存**：停止后所有 motion 指令与含运动能力的技能被拒（409 `estop_latched`），只读能力与非运动技能（如 `status_report`）仍可用。`POST /v1/reset`（需 `request_id`）或 `POST /v2/reset`，请求体 `{"hardware_authorized": true}` 解除，且要求没有未结束的运行。复位先写盘再清内存标志；与 stop 竞争时 stop 获胜（保持锁存）。带 `request_id` 的复位重放时返回**当前真实**锁存状态。
- **复位互斥**（第 05 轮 R4c-01）：同一时刻只允许一个复位。前一次复位的 `adapter.reset()` 仍在执行（包括被调用方放弃等待的，例如一致性测试复位超时后中止）时，新的复位立即返回 409 `reset_in_progress`（“适配器上一次复位尚未返回…”），**绝不并发进入 `adapter.reset()`**；`/v1/status` 的 `reset_in_progress` 可见。该拒绝在写账本之前给出，同一 `request_id` 稍后重试有效。被放弃的复位返回时若期间发生过停止，按竞态处理：保持锁存（内存与磁盘一致）、再停一次，`safety.reset_raced` 带 `latched` 字段，文案与事实一致。**复位过期**（第 07 轮修复 Q7-10）：`adapter.reset()` 用时超过 `SafetySupervisor.reset_expire_s`（30 s，墙钟）才返回的复位作废——即使期间没人按停止也**不解锁**：保持锁存（内存与磁盘）、再调用一次 `adapter.stop()`（驱动可能在复位时重新使能/重放设定值），发 `safety.reset_expired {took_s, expire_s, adapter_stopped, reason, latched, note}`，该复位返回 409 `estop_latched`、`cause: "reset_expired"`（正文 `took_s`、`expire_s`）；确认现场后可重新复位。调用方可在复位请求里带 `expire_s`（秒，正数；按 [1, 30] 取用）声明自己愿意等多久——桌面 8 s 后把复位记为“结果未知”，带上 `"expire_s": 8` 后，8 s 之后才返回的 `adapter.reset()` 不会再自行解锁（契约变更请求）。**客户端怎么知道**（第 07 轮第二次修复 Q7b-05/06）：`/v1/status.capabilities = {reset_expire: true, reset_expire_max_s: 30, reset_expire_min_s: 1, reset_expected_s, reset_expected_note}`——没有这个字段的旧运行时忽略 `expire_s`，迟到的复位在那里仍可能解锁，桌面应据此选择措辞。清单可声明 `safety.reset_expected_s`（`adapter.reset()` 的典型用时，例如四足起立 10 s、机械臂慢速回零 15 s；只作提示，不改变运行时接受什么）：由 spec 清单校验（`normalize_reset_expected`）检查，须为 **(0, 25]** 秒内的有限数值（第 07 轮第三次修复 Q7c-07：桌面取 `expire_s = reset_expected_s + 5`，上限 30 s，25 保证总有 5 s 余量；原为 (0, 30]），否则清单无效、拒绝启动（fail closed）；同时以 `/v1/status.reset_expected_s` 给出，桌面据此取 `expire_s = max(8, reset_expected_s + 5) ≤ 30`，慢复位的本体不再每次都被作废。
- 持久化：锁存记录按（启动序号, 停止代数, 阶段）单调写入，迟到的旧写入不会覆盖新状态。
- 重启后保持锁存；**上次未正常关闭（崩溃、断电、`kill -9`）时，启动即默认锁存**，原因“上次运行时未正常关闭…”。`serve` 收到 SIGINT/SIGTERM 视为正常关闭。

**复位卡住时的操作员出路**（第 06 轮 Q5-03）：`adapter.reset()` 一直不返回（驱动卡死）时，运行时的行为是安全的——急停保持锁存，停止始终可用，新的复位一律 409。为了让操作员知道发生了什么、怎么办：

- `/v1/status` 增 `reset_in_progress_s`（复位已在途秒数，无复位时 `null`）与 `reset_hint`（中文提示；超过 10 s 后为“适配器复位长时间未返回…出路：确认现场安全后重启机器人上的运行时服务（锁存会保留），然后重新“解除急停”；仿真机器人可直接停止再启动仿真”）。
- 409 正文固定为 `{"error": <中文原因>, "code": "reset_in_progress", "message": <中文原因 + 提示，可直接显示>, "reset_in_progress_s": <秒>, "hint": <中文提示>}`。桌面应按 `code` 判断、显示 `message`。
- 操作步骤：① 确认现场安全（必要时按硬件急停）；② 重启机器人上的运行时服务（`systemctl restart <服务>` 或停止再启动 `neuxmind-runtime serve`）——锁存持久化在状态目录，重启后仍为锁存、`reset_in_progress` 为 false；③ 再次“解除急停”（`/v2/reset` + `hardware_authorized`）。若重启后复位仍卡住，问题在驱动，需排查适配器的 `reset()`。

**清单限值复核**：spec 的 `validate_manifest_v2` 已校验限值；`SafetySupervisor` 初始化时再校验一次（防止适配器绕过）：`max_vx`/`max_wz`/`max_velocity`/`reach`/`max_force`/`rate_hz`/`safety.max_linear_speed` 须为大于 0 的有限数，`max_vy` 非负有限，`dof`/`dim` 正整数，`lower`/`upper` 长度等于 `dof` 且逐项 `lower < upper`，`joint_position` 动作块的 `dim` 等于关节数，`safety.workspace` 每轴 `[min, max]` 有限且 min<max。不合格则**拒绝启动**。运行中若限值被改坏，限幅器异常、`low > high` 或任何非有限值一律拒绝（409 `safety_rejected`），绝不下发。

**锁存守护**（第 05 轮，R4b-04 根治；`latch_guard.py`）：独立线程 `nx-latch-guard`，未锁存时什么都不做。锁存期间每 0.1 s 读一次本体声明的反馈——`base.odometry`（速度，以及两次采样间的位姿变化）与 `joint.state`（速度若有，以及位置变化）——发现运动（线速度 > 0.02 m/s、角速度/关节速度 > 0.05 rad/s，或位移同时超过 3 mm / 0.005 rad 且超过对应速度×间隔）就**再调用一次 `adapter.stop()`**，两次至少间隔 0.5 s（机器人时钟），每次发事件 `safety.motion_while_latched {seen, restops, adapter_stopped, evidence, reason}`；`/v1/status` 的 `latched_motion` 为 `{enabled, sources, active, seen, restops, last_at, last_restop_at, last_evidence, seen_during_reset, read_errors, restop_interval_s}`。它**从不**解锁、不改锁存原因、不计入 `stop_count`、不发任何运动指令（连零速度也不发）、不持有停止路径会用的锁；反馈读取卡住只卡住它自己的线程，`close()` 时线程退出（最多等 1 s）。计数的停止之后给本体 1 s 刹停（`settle_s`），期间不判运动。操作员复位执行期间（`adapter.reset()` 里腿足起立等运动是正常的）只观察、不再停（计入 `seen_during_reset`），但复位在途超过 30 s 时照常再停（复位返回后按竞态保持锁存）。阈值面向真机保守取值，可经 `EdgeRuntime(latch_guard=GuardConfig(...))` 调整；未声明 `base.odometry`/`joint.state` 的本体无法守护（`enabled:false`）。效果：每次停止后 6 s / 9 s 又恢复上一条速度的驱动，锁存后 20 s 内总位移 ≤ 0.1 m（无守护时 > 3 m）。一致性测试的中止后核对同时读取守护的 `seen` 计数（`Target.latched_motion()`，进程内直接读、`--mode client` 读 `/v1/status`，每 0.25 s 一次）：守护在观察窗内发现过运动即视为“不静止”（`final_motion.guard_motion_seen`），因为守护的再停可能快于套件的采样，套件自己看不到那段运动。

**锁存守护第 06 轮补强**（Q5-01/02/06/07/08）：

- **累计位移**：计数停止刹停结束后，守护记下锁存快照（底盘 x/y/偏航、每个关节**按名字**的位置）。离开快照超过 `max_linear_drift`（默认 0.05 m）或 `max_angular_drift`（默认 0.05 rad，底盘偏航或任一关节）即视为运动、再停，不论速度多慢；之后本体处于“已偏离”状态，只要相对**上一次再停时的位置**又移动超过采样噪声下限（3 mm / 0.005 rad）就再停（不会每次再给 5 cm 额度）。仿真复验：停后 2 s 恢复 0.015 / 0.019 m/s 的蠕动，5.4 / 4.7 s 内首次再停（位移 0.051 m），600 s 总位移 1.08 / 1.00 m（第 05 轮 8.97 / 11.36 m，未被发现）；静止与 ±2.5 mm 抖动的本体 600 s 再停 0 次。驱动完全不理会 `stop()` 时软件停不住，守护按限速每 0.5 s 再停并持续告警——请用硬件急停。
- **反馈无效 = 无法确认静止**：读取异常、NaN/无穷、数组长度不对、`base.odometry.velocity: null`、关节名缺失/重复、或关节名**集合**与快照不同，持续 `invalid_grace_s`（0.25 s，机器人时钟）即按“可能在动”再停（同样限速 0.5 s），发 `safety.feedback_invalid_while_latched {problems, feedback_invalid, invalid_restops, restops, adapter_stopped, reason, note}`。单次读坏不触发。关节一律按名字比较：每次把 `names` 循环移位的驱动既藏不住运动，也不会误报。反馈读取**卡住**超过 `read_stall_s`（1 s）由监视线程发现，同样再停并发事件（守护线程本身卡在读取里）。
- **适配器声明的停止稳定时间**：若 `stop()` 本身是受控动作（足式阻尼趴下/坐下/StandDown），适配器设属性 `stop_settle_s`（秒），守护在停止后这段时间内不判运动、之后再取快照；取值被限制在 `[settle_s, max_settle_s]` = [1, 5] s，非法值按 1 s。**不声明时**，超过 1 s 的坐下会被当作锁存后运动反复再停（会重规划坐下的驱动可能因此一直坐不完），事件文案会提示声明 `stop_settle_s`。
- **复位被守护中止时说实话**：`adapter.reset()` 超过 `reset_grace_s` 仍在动、守护再停导致复位失败时，409 `estop_latched` 的文字为“复位动作（adapter.reset()）超过允许时间仍在进行且本体仍在运动，锁存守护已再次停止；保持锁存…”，正文带 `cause: "latch_guard_reset_grace"`（真有人按停止时为 `stop_during_reset`，文字不变），`safety.reset_raced` 带 `by_latch_guard`。复位尝试结束而仍锁存时，守护重新刹停并重取快照（起立到一半的位置不算锁存后运动）。
- **关闭**：`close()` 对运行时自己的线程总共最多等 `CLOSE_JOIN_S`（1 s）；仍卡在适配器调用里的线程（例如守护卡在一次永不返回的反馈读取里）是 daemon 线程，列在 `EdgeRuntime.lingering_threads`，读取返回后不再调用适配器、立即退出，进程退出时随之结束。
- `/v1/status.latched_motion` 新增：`settle_s, drift_limit_m, drift_limit_rad, max_drift_m, max_drift_rad, displaced, feedback_ok, feedback_problems, feedback_invalid, invalid_restops, last_invalid, read_in_progress_s`（原字段不变）。
- **固有限制**：守护依赖反馈真实。冻结但看似合理的反馈（位姿恒定、速度为零）与静止无法区分；守护不能替代硬件急停。

**锁存守护第 07 轮：真实噪声/漂移、可标定、事件合并、升级告警**（Q6-01/04/05）：

- **噪声与漂移不再误报**：判据改用稳健统计。① 上报速度：单个采样超过阈值 3 倍立即算运动，否则取最近 1 s 的**中位数**与阈值比较；② 两次采样之间的跳变：须同时超过 `max(3 mm, 8×噪声)`（角度 `max(0.005 rad, 8×噪声)`）与“阈值速度×间隔”，噪声取声明的 `noise_m`/`noise_rad` 与**在反馈上实测**（二阶差分的稳健估计，匀速蠕动不会抬高它；上限 1 cm / 0.01 rad，说谎的驱动不能借噪声藏身）中较大者，至少 20 个采样之后才启用；③ 位移：对最近 3 个采样取中位数后与参考比较，超过 `pos_m`（底盘）/ `yaw_rad + yaw_drift_rad_s×时间`（偏航）/ `joint_rad`（关节）即算运动；**无运动证据满 60 s，参考重新取在当前位置**（IMU 偏航慢漂、腿式里程计随机游走不是运动），但离开锁存快照超过 4 倍限值（底盘位置与关节；偏航没有总界限，因为 IMU 偏航漂移本身无界）始终算运动，所以慢到单个窗口看不出的蠕动最终也会被抓到；④ 再停之后的“已偏离”状态：相对上次再停时的（滤波）位置再动超过 `max(3 mm, 5×噪声)` 即再停，噪声不可忽略时须连续 2 个采样；无运动证据满 60 s 退出“已偏离”。
- **实测（`ManualClock`，静止本体 1 h，`r7rt/noise07.py`）**：位姿噪声 σ 1 mm / 2 mm（偏航 0.5/1 mrad、速度 5/8 mm/s）、IMU 偏航漂移 0.5°/min 与 0.1°/min、里程计随机游走 0.5 mm/√s、以及“2 mm 噪声 + 0.5°/min 漂移 + 随机游走”组合、机械臂编码器 σ 1/2 mrad：**再停均为 0 次/小时**（第 06 轮分别为 2270、5494、95、11、51、68、4742 次）。蠕动：0.015 / 0.019 m/s 停后 2 s 恢复，首次再停 5.5 / 4.8 s、位移 0.053 m，600 s 总位移 1.08 / 1.00 m（与第 06 轮相同）；叠加 2 mm 位姿噪声时 0.015 m/s 仍在 7 s 内、0.07 m 内首次再停（每次再停之间的位移因噪声下限变大，120 s 内 < 0.8 m，无守护 1.8 m）。
- **有界的是什么**（Q6-04 更正）：守护保证的是**每两次再停之间**的位移有界（约为阈值加滤波延迟），**不是**总位移：驱动每次停后又恢复运动时，总位移随时间增长（上例 600 s 约 1 m）；驱动完全不理会 `stop()` 时软件停不住。这种情况升级告警：同一锁存期间因运动再停达到 `escalate_after`（默认 10）次且距第一次此类再停已 ≥ 30 s，发一次 `safety.stop_not_obeyed {ts, message, note, restops, motion_restops, displacement_m, since_s, max_drift_m, max_drift_rad, evidence, adapter_stopped, reason}`，`message`/`note` 同为中文“驱动不服从停止…请立即使用硬件急停或切断动力电…”，`displacement_m` 为锁存快照以来的最大底盘位移；`latched_motion.stop_not_obeyed` 为布尔值，`stop_not_obeyed_at` 为发生时刻（Unix 秒），`stop_not_obeyed_detail` 为上述详情（与桌面 master `b220885` 的红色横幅约定一致）。新的计数停止或解除锁存后重新计数。受控坐下（会重规划，约 15 s 内坐完）不会触发。
- **事件合并**：同类事件（`safety.motion_while_latched`、`safety.feedback_invalid_while_latched`）第一次再停立即发，之后每 10 s（机器人时钟）最多一条，正文新增 `count`（这条事件代表的再停次数），合并发出的汇总事件带 `summary: true`；锁存周期结束时补发未报告的计数。完全不服从的驱动 1 h 再停 6000+ 次，事件 < 400 条（第 06 轮每次一条，42 分钟冲满 5000 条的事件环）。`latched_motion.events_suppressed` 为只在汇总事件里报告的再停次数。
- **现场标定**：本体清单 `safety.latch_guard`（spec `normalize_latch_guard` 校验；越界按范围限幅并在启动输出与 `config_notes` 中说明；非数值/NaN/未知键 → 清单无效、拒绝启动）：

  | 键 | 默认 | 范围 | 含义 |
  |---|---|---|---|
  | `pos_m` | 0.05 | 0.01–0.2 | 底盘位移阈值（m） |
  | `yaw_rad` | 0.05 | 0.01–0.2 | 底盘偏航阈值（rad） |
  | `yaw_total_rad` | 0.05（第 07 轮第二次修复前 0.2） | 0.05–1.0 | 偏航相对锁存快照的硬上界，以及每次偏航再停之后的步长上限（rad，从不重置；Q7-01/Q7b-01）；IMU 会漂移的本体把它调大 |
  | `joint_rad` | 0.05 | 0.01–0.2 | 关节位移阈值（rad） |
  | `noise_m` | 0 | 0–0.01 | 位置噪声下限 σ（m；0 = 只用实测） |
  | `noise_rad` | 0 | 0–0.01 | 偏航/关节噪声下限 σ（rad） |
  | `yaw_drift_rad_s` | 0.0003 | 0–0.005 | 容许的偏航漂移速率（rad/s，0.0003 ≈ 1°/min） |
  | `stop_settle_s`（别名 `settle_s`） | 1 | 1–5 | `stop()` 本身是受控动作时的刹停宽限（s） |
  | `escalate_after` | 10 | 3–30（整数；第 07 轮修复前上限 100） | 升级为“驱动不服从停止”所需的连续运动再停次数 |

  阈值低于 6 倍声明噪声时抬到 6 倍。运行时覆盖（优先于清单，同一校验）：`neuxmind-runtime serve --latch-guard pos_m=0.08 --latch-guard stop_settle_s=3`（或一个 JSON 对象），嵌入时 `EdgeRuntime(..., latch_guard_overrides={...})`；非法值 `serve` 退出码 2。`serve` 启动时打印生效的标定与来源。ROS 2 适配器在配置 JSON 的 `latch_guard` 里写同样的键（例如 `{"stop_settle_s": 3.0, "noise_m": 0.002}`），进入清单 `safety.latch_guard`（Q6-05）；适配器类属性 `stop_settle_s` 仍然有效（清单/运行时未声明时使用）。
- `/v1/status.latched_motion` 新增：`config`（上表生效值）、`config_source`（每键 `default`/`manifest`/`runtime`/`adapter`）、`config_notes`、`noise_used {m, yaw_rad, joint_rad}`（当前采用的噪声）、`rebases`、`motion_restops`、`events_suppressed`、`event_interval_s`、`stop_not_obeyed`（bool）、`stop_not_obeyed_at`、`stop_not_obeyed_detail`（原字段不变）。

**锁存守护第 07 轮修复（发布前，Q7-01/02/05/06/08）**——第 07 轮的“60 s 参考重置”带来两个相对 0.13.0 的退化，0.14.0 不带着它们发布：

- **偏航硬上界，从不重置**（Q7-01）：离锁存快照的偏航超过 `min(max(yaw_total_rad, yaw_rad), yaw_rad + yaw_drift_rad_s×锁存时长)` 即算运动（默认 0.2 rad）。比容许漂移（默认 0.0003 rad/s ≈ 1°/min）快的真实转动早早被抓到，更慢的到 `yaw_total_rad` 时被抓到；按 60 s 重置的参考规则不再额外加漂移容许（重置本身已吸收 `yaw_rad/60 s`，不重复计）。实测（`ManualClock`，不服从停止的本体，真实转角）：1.1 / 0.8 / 0.5 / 0.4 / 0.3 mrad/s 首次再停在 0.052 / 0.081 / 0.126 / 0.201 / 0.200 rad（0.13.0：0.051 rad；修复前 0.8、1.1 mrad/s 1 h 0 次再停）。**取舍**：真实 IMU 漂移与同速的慢转无法区分。漂移 0.5°/min 的 IMU 在默认上界下锁存约 23 min 时被再停 **1 次**（之后按棘轮不再重复，不升级）；这种本体请在清单/`--latch-guard` 标定 `yaw_total_rad`（例如 0.6 → 1 h 内 0 次），代价是慢于 1°/min 的真实转动要转到该值才报。我们选择默认偏严（宁可多停一次，不沉默）。
- **棘轮：停在别处的本体不再每 60 s 被再停**（Q7-02）：位置/关节的 4× 总界与偏航硬上界都相对快照、从不重置；某通道在位移 R 处再停后，该通道下一次硬界为 `R + 限值`（偏航另加此后的漂移容许）。受控坐下（未声明 `stop_settle_s`）、锁存中被人推开、里程计随机游走越过总界之后都安静下来；继续移动的本体每多走一个限值仍被再停，“已偏离”状态下仍按 3 mm 级灵敏度再停。“已偏离”状态的再停门槛额外容许实测的里程计随机游走（`hypot(5×噪声, 4×游走×√t)`，游走在反馈上用多滞后方差估计、上限 1.5 mm/√s；窗口内有净趋势或有直接运动证据的再停时不采信，停停走走的驱动不会被当成游走）。
- **升级只看“再停之后仍在动”**（Q7-02/Q7-06）：一次运动再停只有在距上次计入的再停 ≤ `escalate_gap_s`（180 s）且其间本体确实又动了（速度/跳变证据，或滤波位置超出噪声、游走、漂移）时才延长“连续不服从”计数；连续 `escalate_after` 次且跨度 ≥ 30 s，**或**底盘离快照已 ≥ 0.5 m 且连续 ≥ 3 次（不等 30 s），发一次 `safety.stop_not_obeyed`（正文新增 `streak_restops`、`streak_s`、`trigger: restops|displacement`）。静止在别处的本体不会累计。实测：0.05 m/s 不服从 11.7 s 升级（修复前 31.4 s、已走 1.57 m）；1 h 受控坐下 3 s 重规划 / 不重规划 / 2 s 不重规划 再停 19 / 5 / 3 次、无升级（修复前 78 / 64 / 62 次并在 75–424 s 误升级；0.13.0 为 20 / 5 / 3）；被推开 0.3 m 后静止 7 次/h（修复前 66 次、204 s 误升级；0.13.0 7 次）；推开 0.3 m 再叠加 1 mm/√s 里程计游走 9–12 次/h、无升级；1 mm/√s 游走静置 8 h 8 个种子、0.5 mm/√s 2 个种子：再停 0–4 次、**0 次升级**（修复前种子 2 为 2901 次并升级）。
- **放宽的标定要大声说**（Q7-05）：任何键比默认更宽松（数值大于默认）时，启动时发一条 `safety.latch_guard_relaxed {ts, message, keys:[{key, value, default, source}], notes}`，`serve` 启动输出一行“警告：锁存守护标定比默认宽松…”，`/v1/status.latched_motion` 新增 `relaxed`（bool）与 `relaxed_keys`（`[{key, value, default, source}]`，与桌面 master `5ccbd26` 约定一致）；`escalate_after` 上限降为 30（spec 与 `GuardConfig` 都限幅）。
- **关闭时补发合并计数**（Q7-08）：`close()` 补发挂起的 `safety.motion_while_latched`/`feedback_invalid_while_latched` 汇总，Σ`count` = 再停次数。
- `/v1/status.latched_motion` 新增：`relaxed`、`relaxed_keys`、`hard_limits {pos_m, yaw_rad, joint_rad}`、`walk_used {m_per_sqrt_s, rad_per_sqrt_s}`、`escalation_streak {restops, span_s}`（原字段不变）。
- 仍未解决（见下一轮；第一项已在第二次修复中解决，见下）：位置/关节的首次总界仍为 4×限值（0.2 m / 0.2 rad；未改）；说谎的驱动冻结反馈仍无法发现；在锁存状态下把机器人搬走 > 0.5 m（或持续搬动 > 30 s）会按“不服从停止”升级——软件从反馈上无法区分“人在搬”与“驱动在走”，搬动前请先断电或解除急停。

**锁存守护第 07 轮第二次修复（发布前，Q7b-01/02/03/09）**——发布规则：在复审 `r8/`、`r9fix/`、`r10/` 的**每一个**场景里都不比 0.13.0 更不安全，同时保留第 07 轮的误报修复。

- **偏航：默认 0.05 rad，步长封顶**（Q7b-01）：`yaw_total_rad` 默认 0.2 → **0.05**（= `yaw_rad`，与 0.13.0 相同：任何转动在 0.05 rad 被抓）；某次偏航再停于 R 之后，下一界为 `R + min(max(yaw_total_rad, yaw_rad), yaw_rad + yaw_drift_rad_s×Δt)`（复审建议的公式，封顶，不再随时间无限增长）。于是慢于容许漂移的真实转动每多转 0.05 rad 就再停一次（0.29 mrad/s：1 h 20 次、最长间隔 174 s；0.1 mrad/s：每 500 s 一次）。**代价，明说**：真实 IMU 漂移与同速慢转无法区分——0.5°/min 的 IMU 漂移默认每 ≈345 s 再停一次（1 h 10 次；0.1°/min 1 h 2 次），从不升级（偏航漂移不计入“不服从”）；0.13.0 同情形 3519–5925 次/h。现场标定使其归零：清单/`--latch-guard` 声明 `yaw_total_rad`（例如 1.0，可再加实测的 `yaw_drift_rad_s`）→ 0.5°/min 1 h **0 次**再停（8 h 约 4 次）；此标定比默认宽松，启动时有 `safety.latch_guard_relaxed`。
- **“已偏离”不再结束**（Q7b-02）：再停之后本体在本次锁存期间一直处于“已偏离”，参考点是上次再停（或上次 `stop()`）时的位置，无运动证据满 60 s 只把这个参考点挪到当前位置（让游走容许重新从小开始），**不再**换回 5 cm 的窗口——“停 ≥ 60 s 再走”的驱动每周期只能走约 3–4 mm（`resume_0.019_65s` 1 h 0.235 m，0.13.0 0.237 m；修复前 2.70 m）。噪声相对 `min_*_delta` 可忽略时直接比原始采样（无中值滤波延迟，与 0.13.0 同样紧）。
- **位置与关节从快照起算，不再按 60 s 重置**（Q7b-09）：慢于 0.83 mm/s 的蠕动不再要走到 0.2 m 才被发现，与 0.13.0 一样在 0.05 m / 0.05 rad 被抓（0.5 mm/s：101 s、0.051 m；机械臂 0.8 mrad/s：64 s、0.051 rad）。腿式里程计随机游走靠实测游走容许 `max(pos_m, hypot(5×噪声, 4×游走×√t))` 吸收；只有偏航参考仍按 60 s 重置（IMU 漂移）。
- **游走容许只在它解释得了所见时才采信**（Q7-02/Q7b-02/Q7b-03）：① 停停走走不是游走——随机游走在每一步里都看得见（每步方差 ≥ 游走²·dt），匀速段每步相同、暂停段为零的蠕动被判为 0（修复前 0.8 mm/s 停后 185 s 恢复被测成 0.8 mm/√s 游走，1 h 0.81 m）；② 从锚点（快照，或直接运动证据窗口结束处）起位移超过 5×游走×√t 即不再采信（伪造游走掩盖不了“去某处”）；③ 再停持续发生时（间隔 < 60 s）游走估计不得上涨；④ 估计取 300 s 内峰值（20 s 估计的起伏不让合法游走误报）。
- **预防性停止（静默；第 07 轮第三次修复 Q7c-01 改为逐条复刻 0.13.0）**：守护内带一份 0.13.0 再停规则的**精确复刻**（`V013Replica`）：同样的原始采样（不滤波）、同样的证据——上报速度超过 2 cm/s / 0.05 rad/s、两次采样间跳变超过 3 mm / 5 mrad（且超过阈值×Δt）、“偏离”前离原始快照超过 `pos_m`/`yaw_rad`/`joint_rad`、“偏离”后离 0.13.0 上次再停处超过 3 mm / 0.005 rad——**没有**噪声、游走、IMU 漂移容许，**没有**连续样本要求，按它自己的节奏（自己上次再停后 ≥ `restop_interval_s` = 0.5 s）、在它自己再停时移动参考与置“偏离”。凡复刻规则此刻会再停、而本版守护在这个采样没有调用 `stop()`（游走/漂移/噪声容许把它解释掉了）的地方，守护**静默**再调用一次 `adapter.stop()`（HAL 契约要求 `stop()` 幂等）：不发事件、不计 `restops`/`motion_restops`、不参与升级，计数在 `latched_motion.precautionary_stops`；`precaution_interval_s` ≤ 0 时关闭（默认 0.5；节奏不再由它决定，而是复刻的 `restop_interval_s`，原为每秒最多一次）。于是 `stop()` 的调用时刻覆盖 0.13.0 的全部再停时刻：“每次服从一段时间再恢复”的驱动（含伪造或真实的里程计游走）走得不比 0.13.0 远（终审 `extra.py`/`extra2.py` 全部场景，见 `EVOLUTION_ENTRY.md`）。代价：0.13.0 会计数再停的地方（游走、噪声、IMU 漂移的静止本体）现在是同样频度的静默 `stop()`——ROS 2 适配器每次发零速 Twist + 取消目标，最多 2 次/s，与 0.13.0 的再停频度相同。
- **守护只有一个 `stop()` 节奏（第 07 轮第四次修复 Q7d-01/02）**：守护发出的**所有** `adapter.stop()`——计数再停、预防性停止、反馈无效、监视线程发现读取阻塞——共用一个节奏，相邻两次至少隔 `restop_interval_s`（0.5 s），即 **≤ 2 次/s**（修复前计数再停与复刻各按自己的 0.5 s 节奏交错，间隔可短到 0.1 s、最多 4 次/s；“0.5 s 内重复的 `stop()` 视为同一次、丢弃”的驱动因此把整串停止都吞掉，1 h 真实位移是 0.13.0 的 1.5–8.9 倍）。预防性停止开着时（默认），守护线程的 `stop()` 调用时刻**就是**复刻（0.13.0）的再停时刻：计数再停只在 0.13.0 的规则在同一采样也看到运动/反馈无效时才调用 `stop()`，否则**合并**进复刻的停止序列——照样计数、发事件、参与升级，只是不再单独调用（`latched_motion.merged_stops`）；这类事件（`safety.motion_while_latched`、`safety.feedback_invalid_while_latched`、`safety.stop_not_obeyed`）带 `merged: true`、`adapter_stopped: false`（本次没有调用 `stop()`；第 07 轮第五次修复 Q7e-02，原为“取最近一次 `stop()` 的结果”），单独调用了 `stop()` 的为 `merged: false`、`adapter_stopped` = 该次调用结果。复刻到期而守护（读取阻塞）在 0.5 s 内刚调用过 `stop()` 时，把那一次当作复刻在那一刻的再停（移动其节奏，参考取那次时的位置，不带证据、不置“已偏离”，与 0.13.0 的读取阻塞再停相同），不另发。守护记下最近一次 `stop()` 由谁发出（守护线程的第几拍 / 监视线程的读取阻塞再停），**不以机器人时刻相等**判断“本采样已停”——适配器时钟分辨率粗（≥ 20 ms，例如按控制周期计数）时，阻塞再停与阻塞后第一个采样可能同刻（第 07 轮第五次修复 Q7e-01）。`latched_motion.stop_calls` = 守护发出的 `stop()` 总数。操作员的 `/v1/stop`、`/v2/stop` 不受此节奏限制，总是立即执行。**对驱动的要求**（Q7e-04，`hal.py` 同述）：守护的重复 `stop()` 相邻至少 0.5 s；驱动必须把每一次 `stop()` 都当作停止，**不得**在超过 0.5 s 的窗口内去抖/丢弃 `stop()`（0.8 s 以上的滑动去抖会把整串停止全吞掉，0.13.0 同样按不住）；适配器的 `clock` 应为高分辨率单调时钟。
- **升级不能靠拉长间隔躲开**（Q7b-03）：锁存以来底盘离快照 ≥ `escalate_displacement_m`（0.5 m）且本次锁存已有 ≥ 3 次运动再停，即发 `safety.stop_not_obeyed`（`trigger: "displacement"`），**不论**再停之间隔了多久（修复前须 180 s 内连续 3 次）。
- **噪声只信到 3 mm / 2 mrad，速度门限不再无谓推迟**：反馈上实测的噪声上限由 1 cm / 0.01 rad 降为 3 mm / 2 mrad（更吵的传感器请在清单声明 `noise_m`/`noise_rad`，属放宽、有警告）；噪声还没测出来之前（前 20 个采样），两次采样之间的跳变按“上限噪声”判断——于是注入 ±1 cm / ±10 mrad 抖动的说谎驱动在 1.2 s 被抓（与 0.13.0 相同；修复前 35–48 s、已走 4 cm）。单个速度采样：上报速度一直很安静（近 64 个采样稳健离散度 ×8 低于阈值）时，超过阈值本身即算运动（与 0.13.0 相同，0.05 m/s 不服从 1.2 s 被抓），速度本身有噪声时仍按 3 倍阈值/1 s 中位数（噪声 5–8 mm/s 不误报）。
- **漂移提示（每次锁存一条）**：锁存以来离快照的位移第一次超过首次发现界（`pos_m` / `yaw_rad` / `joint_rad`，按噪声抬高后的值）时——不论随后算作运动、还是被归为游走/漂移只做预防性停止——发**一条**不升级的 `safety.latched_drift_notice {ts, message, channel: base_position|base_yaw|joint:<名字>, displacement, unit}`，`message` 为“急停期间机器人离停下位置已超过 X（…），可能是里程计漂移，也可能在移动，请到现场确认”；`latched_motion.drift_notice`（bool）、`drift_notice_at`、`drift_notice_detail`。提示属于**一次锁存**（Q7c-02，第三次修复）：每次复位成功即作废（监督器 `reset_count` 加一），即使复位后一个守护周期内又被停止、守护从没看到“未锁存”，新锁存越界时也会再发一条；`drift_notice*` 只在锁存期间、且只报告当前锁存的提示（复位成功后立即为 false/null，不必等守护下一拍）。合法游走的本体每次锁存恰好一条（总指挥决定可接受）；复位进行中与刹停宽限内不发。
- `/v1/status.latched_motion` 新增：`stop_calls`、`merged_stops`（第四次修复）、`drift_notice`、`drift_notice_at`、`drift_notice_detail`、`precautionary_stops`、`walk_measured {m_per_sqrt_s, rad_per_sqrt_s}`（`walk_used` 现为实际采用值）、`hard_limits.pos_step_m / yaw_step_rad / joint_step_rad`。
- A/B 全表与仍比 0.13.0 差的场景见 `EVOLUTION_ENTRY.md`。

**看门狗**：独立线程 `nx-watchdog`，只做“到期发零速度”，不碰数据库/文件；运行超时与宽限期结束（会写盘）在另一个监视线程里执行，存储卡顿不会延误看门狗。

**停车通道（复审 N-03）**：看门狗平时在速度锁内发零速度；如果设定值已过期而速度锁被一条卡在适配器里的普通指令占着，看门狗**不等锁**：直接调用 `adapter.stop()`，并在独立的短线程里发送零速度（事件 `safety.watchdog`，`unlocked=true`）。因此适配器契约要求 `stop()` 与零速度 `invoke("base.velocity", 0)` 可以与正在进行的 `invoke` 并发调用且不等待它（见 `hal.py`；厂商 SDK 只能单线程时，走厂商独立的急停/刹车通道）。

**限值冻结与清单变化（复审 N-04）**：启动时把适配器清单深拷贝、校验后冻结（列表变 tuple、字典只读），监督器与适配器不共享任何对象，适配器运行中修改自己的列表不影响限位。限位比较对 NaN 安全（NaN 一律拒绝）。监视线程每秒比对一次 `adapter.manifest()` 与启动时的内容，一旦不同（或读取失败）：发 `safety.manifest_changed` 事件、停车、拒绝一切运动（409 `manifest_changed`），`/v1/status` 的 `manifest_changed` 给出原因，直到重启运行时。技能 `ctx.param()` 返回普通列表副本。

**限幅规则**（参数来自 manifest，未声明则拒绝运动）：

| 能力 | 处理 |
|---|---|
| `base.velocity` | vx/vy/wz 裁剪到 `max_*`；非全向底盘 vy 置 0；`safety.max_linear_speed` 合速度裁剪；`timeout_s` 默认 0.5 s、限制在 0.05–2 s；看门狗到期发零速度 |
| `arm.joint_position` | 关节数 ≠ `dof` 拒绝；任一关节超出 `lower`/`upper` 拒绝；`max_velocity` 裁剪到声明值 |
| `gripper.position` | opening 不在 0–1 拒绝；`max_force` 裁剪 |
| `legged.posture` | 不在 `supported` 列表中拒绝 |
| `base.navigate_to` | 四元数须归一化；`safety.workspace` 声明时目标须在范围内 |
| `arm.ee_pose` | 目标超出 `reach` 拒绝 |
| `policy.action_chunk` | 提交时（第 05 轮 R4c-05）：`joint_position` 空间而本体未声明 `joint.state` 或 `arm.joint_position.params.joint_names` → **同步** 400（`joint_state_required` / `joint_names_required`），不返回 run id；每步：`joint_position` 空间裁剪到限位并限制单步变化 ≤ `max_velocity·dt`（以当前 `joint.state` 为起点）——本体未声明 `joint.state`、读取失败、长度 ≠ `dim` 或含非有限值时**拒绝**（fail closed）；`base_velocity` 空间按底盘限速裁剪；其他动作空间拒绝。时间约束（复审 N-06）：`dt` 不小于控制周期 `1/rate_hz`，且不大于 `2/rate_hz`（未声明 `rate_hz` 时不大于 0.5 s）；步数 ≤ 1000；总时长 `步数×dt` ≤ 10 s；超出拒绝 |
| 任意 | 参数含 NaN/∞ 拒绝（HTTP 层的 JSON 本身也拒绝 `NaN`/`Infinity`）；超大整数拒绝（400/409，不再是 502 unknown）；厂商扩展 `x.*` 视为运动能力且无法限幅，拒绝 |

运行被取消、超时或失败时，若它用过运动能力，运行时会非锁存地 `adapter.stop()`。

**信任边界（重要）**
- 技能默认在**独立子进程**中运行（§2.2），拿不到适配器对象、运行时内存或令牌，只能经代理 `ctx` 请求主进程，所有能力调用仍经能力总线与安全监督。剩余的信任边界见 §2.2 末尾——最重要的是**网络与本机设备未隔离**：能直接通过网络/串口/ROS 2 DDS 驱动本体的系统上，恶意技能仍可绕开运行时。第三方技能上线前须补齐网络隔离（见 §7）。
- `--sandbox inprocess`（仅调试，须显式开启，`/v1/status` 的 `sandbox.mode` 可见）恢复第 01 轮的进程内执行：技能可直接 `import` 适配器绕过一切检查。
- 适配器是厂商/集成商的可信代码，安全监督只能执行 manifest 声明的限值；声明错了，限幅就错了。
- 软件安全层**不能替代硬件急停**。
- 进程隔离模式下，不配合停止的技能进程在 1 s 宽限期后被 `SIGKILL`；inprocess 模式下只能撤销线程的能力访问（宽限期 2 s 后强制结束运行记录），无法杀死 Python 线程。
- Bearer token 是唯一凭据，HTTP 明文。默认只监听 127.0.0.1；跨网络请放在 TLS/VPN 之后。软件 stop 端点只应在受信网段开放，**硬件急停才是主通道**。

## 2.1 连接上限与停止保障路径

| 机制 | 默认值 | 说明 |
|---|---|---|
| 普通连接上限 | 32（`--max-connections`） | 超出后新连接立即收到 `503 {"code": "busy"}`，不为其创建线程 |
| 请求读取总期限 | 10 s | 请求行 + 请求头 + 请求体必须在 10 s 内读完，否则断开（防 slowloris：逐字节慢发也会被断开） |
| 单次 socket 读写超时 | 5 s | 包括写响应 |
| 停止专用名额 | 4（`--stop-reserved`） | 普通名额满时，新连接进入专用名额，须在 1 s 内发出请求行；只服务 `POST /v1/stop`、`POST /v2/stop`（仍需 token），其他路径 503 |
| 专用名额驱逐 | — | 专用名额也满时，驱逐最早的、尚未表明自己是 stop 的连接；正常 stop 客户端一次发完整个请求，几微秒内就被识别，因此空闲/半截连接无法挤掉它 |
| 监听队列 | 128 | 突发连接不因 backlog 过小被内核丢弃 |

**计数规则（第 03 轮查明，INTERFACES §5.0）**：普通名额只在连接**结束**时释放——响应写完、客户端关闭、或请求读取期限（默认 10 s）到期断开空闲/半截连接；停止专用名额里的驱逐**从不**释放或占用普通名额（`ReservedEvictionTests`）。所以"连接已满"是一个瞬时状态：最早的空闲连接一到期，下一条普通请求就会拿到名额并得到 200。第 02 轮 `test_stop_within_one_second_under_many_half_open_connections` 约 1/15 偶发"连接满时得到 200"的根因正是这个：打开 300 个连接本身要 1–2 s（监听队列满、SYN 重传），测试里 1.5 s 的期限恰好在最后检查前后到期。实现没有错，测试改为在确定的满状态下检查（等泛洪连接到期 → 恰好补满普通名额 → 在其期限内请求，断言 503 <1 s；同时再验一次满状态下 stop <1 s），后台负载下连续 30 次通过。"连接满时普通请求立即 503、stop 始终 <1 s"的语义不变。

选择“同端口保留名额”而不是独立端口或 unix socket：桌面端只认识一个 endpoint，且独立端口同样可被泛洪。实测（`test_gateway.ConnectionLimitTests`）：300 个未认证半截连接下，`/v2/stop` 与 `/v1/stop` 都在 1 s 内返回并锁存，线程数有界。这只是软件层的保障，不能防御网络层 DoS（SYN 泛洪、带宽耗尽）；那需要防火墙与硬件急停。

## 2.2 技能进程隔离（沙箱）

默认 `--sandbox process`（INTERFACES_V1 §4.2，ADR-010 的后续）。每次技能运行（v1 应用的每个步骤也是）启动一个新的子进程，运行完即销毁。

```
运行时主进程                                              技能子进程（python3 -I -B child.py）
 run 线程: SandboxHost ──── socketpair，JSON 行协议 ────►  代理 ctx（sandbox_child.SkillContext）
   │  校验每条消息 → 限流 → 真正的 SkillContext                  │  call/read/sleep/log/progress/
   │  → CapabilityBus → SafetySupervisor → adapter                │  check_stop/stopped/time_left
   │  ◄── {"type":"reply","id":n,"ok":...} ─────────────────────── │
 监视线程: 宽限期后 killpg(SIGKILL) + cgroup 全员 SIGKILL      技能代码（看不到适配器/运行时/令牌）
 停止路径: 先停车 → 再 kill(SIGTERM)（非阻塞，不等待）
```

**为什么用 subprocess + 自定义协议而不是 multiprocessing**：multiprocessing 默认 pickle 任意对象、会把父进程模块状态带进子进程，协议面不可控；自定义 JSON 行协议只有 8 种请求、每种字段固定，主进程逐字段校验，可测、可审计（`sandbox_child.py` 顶部有完整协议）。

**子进程的启动**（不用 `preexec_fn`，运行时是多线程的）

| 项 | 做法 |
|---|---|
| 解释器 | `python3 -I -B`：忽略 `PYTHON*` 环境变量与用户 site，脚本目录不进 `sys.path`；启动时检查技能用户能否执行该解释器，不能则退回真实解释器并在 `/v1/status` 提示（`--sandbox-python` 可指定） |
| 环境变量 | 从零构造：`PATH`、`LANG`、`LC_ALL`、`HOME`=`TMPDIR`=私有临时目录、`PYTHONDONTWRITEBYTECODE`。**不继承**任何变量，`BRAIN_EDGE_TOKEN` 不会出现 |
| 文件描述符 | `close_fds`，只传协议 socket；stdin/stdout/stderr 为 /dev/null（`print` 进入有界尾部缓冲，结束时作为 `sandbox.output` 事件） |
| 用户 | 运行时为 root 时降权为 `neuxmind-skill`（若存在）或 `nobody`，清空附加组（`--sandbox-user` 可指定，不允许 root）；非 root 时与运行时同用户，此时运行时把自己设为 non-dumpable（`PR_SET_DUMPABLE=0`），子进程读不到 `/proc/<运行时>/environ`、`mem`，也不能 ptrace |
| 会话 | `start_new_session`：子进程自成会话与进程组，停止时整组 `SIGKILL` |
| 工作目录 | 技能包的**只读副本**（目录 0555、文件 0444，root 运行时属主为 root）+ 私有临时目录（0700，属技能用户）；已安装的包与状态目录（0700）都不在其中。沙箱根目录：与运行时同用户时在 `<state-dir>/sandbox`（0711，运行时私有树内）；降权运行时放在系统临时目录下的 `neuxmind-sandbox-<euid>`（运行时所有、0711，技能用户只能穿越、不能创建条目，因而无法植入符号链接；降权子进程无法穿越 0700 的状态目录，故不放其中）。启动时清理本运行时已死实例遗留的 `rt-<pid>-*` 工作区：只用 `lstat`、不跟随符号链接、只删本 uid 所有的真实目录、`fchmod`/`rmtree` 基于 fd（Q2-01 修复：旧实现放在全局可写的 `/tmp` 且跟随符号链接，技能可诱使 root 递归改任意目录权限） |
| 资源限制 | 子进程在运行任何技能代码之前对自己设置（软=硬，技能无法再提高）：`RLIMIT_AS` 1 GiB（`--skill-memory-mb`）、`RLIMIT_CPU` = 技能超时（墙钟）+10 s（30 s–3600 s）、`RLIMIT_NOFILE` 64、`RLIMIT_NPROC` = 技能用户当前任务数 + 32、`RLIMIT_FSIZE` 64 MiB、`RLIMIT_CORE` 0；另设 `PR_SET_NO_NEW_PRIVS`、`PR_SET_PDEATHSIG=SIGKILL`（运行时死亡时技能进程随之死亡） |
| pids cgroup（尽力而为） | 运行时对 cgroup v1 `pids` 或已委派的 v2 子树有写权限时，每个技能进程树放入独立 cgroup（`pids.max`=32）：调用 `setsid()` 逃出进程组的孙进程也会被统计并立即杀掉。`/v1/status` 的 `sandbox.pids_cgroup` 显示是否启用。**无可写 cgroup 时**（常见于未加 `Delegate=yes` 的 systemd cgroup v2）：运行时设 `PR_SET_CHILD_SUBREAPER`，`setsid()` 逃逸的后代在其直接父进程退出后会重挂到运行时，运行结束时由孤儿收割者按 `/proc` 扫描（技能用户 uid 且重挂到本运行时、且不在在途子进程表中）`SIGKILL` 清理——从逃逸到重挂有一个短暂窗口，期间该进程**无法驱动本体**（在途请求被守卫作废），但网络未隔离时仍可经网络/DDS 直接控制。`sandbox.process_containment` 在无 cgroup 时为 `best-effort`。真机部署请提供 cgroup（systemd `Delegate=yes`，或为每个技能用 `systemd-run --scope`） |
| 导入守卫 | 子进程里 `import neuxmind_runtime.*` 一律 `ImportError`，只提供替身 `neuxmind_runtime`/`neuxmind_runtime.skills`（`Skill` 与异常类），所以现有技能与 SDK 模板（`from neuxmind.skill import Skill` 或回退）不改即可运行。**守卫只是护栏**：真正的边界是进程——绕过守卫也只能拿到一份没连接任何东西的源码 |

**协议防线（fail closed）**：子进程的每条消息须为一行 ≤1 MiB 的 JSON 对象（拒绝 NaN/Infinity、无效 UTF-8、过深嵌套、超长整数）；请求编号必须从 1 严格递增；`op` 只能是 8 种之一，字段集合固定且逐个校验类型（如 `args` 须为对象，多出 `hardware_authorized`/`epoch` 之类字段即违规）；子进程不能发 `reply` 或其他类型；`done` 消息字段固定；启动后 15 s 内须完成握手。任何违规：立即整组 `SIGKILL`、运行失败（“技能进程违反沙箱协议”）、发 `sandbox.violation` 事件；若该运行用过运动能力，收尾时停车。授权、停止代数、声明的能力、限值只存在于主进程，子进程说什么都不能改变它们。
另有：每秒最多 2000 个请求（令牌桶，突发 200，超出时主进程放慢服务，限制技能能让主进程消耗的 CPU）；每个技能进程最多记录 500 条 log/progress 事件，其余计数后丢弃（`sandbox.log_suppressed`/`sandbox.log_dropped`），防止刷屏把安全事件挤出遥测环形缓冲；log 数据 >4 KiB 截断；会覆盖事件字段的键（`type`/`ts`/`seq`/`run_id`/`message`）改名为 `skill_<键>`。

**停止与在途请求**
1. 停止路径先停车、置运行停止事件、取消动作句柄，再非阻塞地给子进程发 `SIGTERM`；取消（cancel）与超时同样“先停车、后通知”。
2. 子进程的 `SIGTERM` 处理器只置一个标志：此后代理 `ctx.call/read/check_stop/sleep` 在子进程本地立即抛 `SkillStopped`（`log`/`progress` 仍可在清理代码中使用）；不会在技能代码的任意位置抛异常。子进程启动期间（处理器尚未装好）到达的通知在握手后补发。
3. 监视线程在停止请求 1 s（`kill_grace_s`）后对整个进程组（及 cgroup 全员）`SIGKILL`；运行收尾时也总是强杀。
4. 子进程一旦退出，主进程侧 `SkillContext` 的守卫失效：正在为它服务的请求在下发适配器之前被作废（总线下发前会再检查一次），进行中的动作（如导航）被取消，之后到达的消息一律不处理。
5. 停止本身仍只依赖 §2 的停止路径；实测（`test_sandbox`）在死循环、fork 炸弹、日志刷屏期间 HTTP `/v2/stop` 均 <1 s 返回并立即停车。

**剩余信任边界（未隔离，须知）**
- **网络未隔离**：子进程可以发起任意网络连接。能直接通过网络驱动本体的系统（ROS 2 DDS 话题、厂商 UDP/HTTP SDK、同网段的电机控制器）上，恶意技能可以绕开运行时直接驱动机器人。运行时网关本身需要令牌，子进程拿不到。第三方技能上线前需要网络命名空间/防火墙（列入下一轮）。
- **本机设备与文件系统**：技能用户能读的文件（系统文件、其他用户可读的目录）都能读；能访问的设备节点（如串口，取决于设备权限和组）都能访问——降权后清空了附加组（`dialout` 等），但非 root 部署时技能与运行时同用户，能访问运行时能访问的一切，**包括运行时的状态目录**（可改写锁存记录、技能包，影响下次启动）。生产环境应以 root 启动运行时（自动降权）或用专用用户运行运行时并为技能配置独立的 `--sandbox-user`。
- 无可写 cgroup 时，`setsid()` 逃逸的孙进程由孤儿收割者在其直接父进程退出后清理（见上，有短暂窗口）；这些进程受继承的 rlimit 约束、看不到运行时、无法驱动本体，但在被清理前网络未隔离。要立即、确定性地收回整个进程树，请提供 cgroup。
- **并发技能之间未隔离**（第 02 轮复审 Q2R-01/02，已实证）：多个技能同时运行且共用同一个降权用户时，一个技能可以通过 `/proc/<对端>/cwd` 找到另一个技能的工作区，读取并篡改它的私有临时文件，也能看到对端进程的路径与环境；技能也能在全局可写的 `/tmp` 留下文件。这不影响运行时、安全监督与停止路径，但第三方技能上线前必须做到"每次运行独立 uid + 文件系统/mount 隔离"（列入第 05 轮，见 ADR-018）。
- `RLIMIT_NPROC` 按用户计数：多个并发技能共用同一技能用户时互相占用名额；`nobody` 也可能被其他系统服务使用，推荐创建专用用户 `neuxmind-skill`。
- 未做 seccomp 系统调用过滤；CPU 时间（`RLIMIT_CPU`）而非墙钟限制计算量，墙钟由运行超时保证。
- 隔离只针对技能代码；适配器、运行时自身、spec 仍是可信代码。

## 3. 写适配器：把本体接进来

第 04 轮起的流程（INTERFACES_V1 §6）：**本体档案 → 能力映射 → 脚手架生成适配器 → 实现可实现的能力 → 一致性测试 static + dry（替身/仿真）→ 真机 motion（现场确认）→ 适配报告**。

1. **能力映射**：对照 `spec/neuxmind_spec/capabilities.py`，为本体真实具备的每项能力写出：参数（**就是安全限值**，必须来自规格书/档案，如 `max_vx`、关节 `lower/upper`、`max_velocity`）、实现方式（`ros2:/cmd_vel`、`sdk:<名>`、`serial:<设备>`、`http:<地址>`、`todo`）和证据 ID。`safety.stop`、`system.health` 必选；没有标准能力对应的功能用 `x.<vendor>.<name>`（运行时一律当运动能力并拒绝执行）。
2. **生成适配器包**（§3.2）：`neuxmind-runtime scaffold --spec mapping.json --out ./acme_rover`。`ros2:` 映射（底盘速度、里程计、电池、IMU、关节状态、AMCL 定位、Nav2 导航、停止、健康）直接得到可工作的实现；其余能力是抛 `NotImplementedError` 的方法，**不出现在 manifest 中**。
3. **实现剩余能力**（`hal.py` 契约）：
   ```python
   from neuxmind_runtime.hal import RobotAdapter, ActionHandle, ActionStatus

   class AcmeDog(RobotAdapter):
       def manifest(self): return {...}                  # 固定的 robot.adapter.v2
       def read(self, cap_id): ...                       # state/stream → outputs dict
       def invoke(self, cap_id, args): ...               # command：发设定值后立即返回
       def start_action(self, cap_id, args): ...         # action：立即返回 ActionHandle
       def stop(self): ...                               # 必须快、幂等、线程安全、不等锁/网络
   ```
   - 入参已经过 spec 校验和安全限幅；返回值必须符合能力 `outputs`（运行时会校验，不符记 `adapter_fault`）。
   - `ActionHandle.poll()` 返回 `ActionStatus(state=running|succeeded|failed|cancelled, progress, result, error, feedback)`；`cancel()` 必须快。
   - `stop()` 与零速度 `base.velocity` 必须能与进行中的普通指令并发调用，不能等待任何可能被长调用持有的锁。若厂商 SDK 只能单线程访问，`stop()` 里也要走厂商提供的独立急停通道。
   - **机械臂 `joint_names`**（R4c-03）：有 `arm.joint_position` 时在其 `params.joint_names` 按**指令顺序**（`position` 分量顺序）列出关节名——**取自 `ros2_control` 控制器配置的 `joints:` 列表**（如 `controllers.yaml` 里 `joint_trajectory_controller.ros__parameters.joints`），不要照抄 `/joint_states` 的顺序（常按字母序/URDF 序，顺序错会驱动错误的关节）；名称须与 `joint.state.names` 一致。未声明时 joint_position 动作块被拒（400 `joint_names_required`）、一致性机械臂用例 `blocked`；dry 不验证顺序，只有 motion 级会发现声明错误。
   - **反馈要真实**：`base.odometry` / `joint.state` 除供技能使用外，还被锁存守护用来发现“锁存后仍在动”（见 §2）；`reset()` 可以让本体运动（如起立），但须在合理时间内返回（运行时同一时刻只允许一个复位，在途时新的复位 409 `reset_in_progress`）。若 `stop()` 本身是受控的坐下/阻尼动作，在清单 `safety.latch_guard.stop_settle_s` 或适配器类属性 `stop_settle_s` 上声明（1–5 s）；反馈噪声/IMU 漂移较大的本体在 `safety.latch_guard` 标定 `noise_m`/`noise_rad`/`yaw_drift_rad_s`（见 §2“锁存守护第 07 轮”）。
   - 可选：`reset()`（解除锁存时调用）、`status()`（并入 `/v1/status`）、`install_model()`、`close()`；`clock` 属性（真机保持 `RealClock()`）。
   - 脚手架生成的包里：实现方法 → 把能力 ID 加入 `IMPLEMENTED` → 运行包内 `tests/test_adapter.py`（一致性 static 级）。
4. **一致性测试**（§3.1）：`neuxmind-runtime conformance --adapter acme_rover.adapter:AcmeRoverAdapter --level dry --out report.json`（接替身/仿真或已连好的本体；dry 不产生运动）。
5. **启动**：`BRAIN_EDGE_TOKEN=<≥24 字符> neuxmind-runtime serve --adapter acme_rover.adapter:AcmeRoverAdapter --host 0.0.0.0 --port 8790 --state-dir /var/lib/neuxmind`；离线检查技能兼容：`neuxmind-runtime check examples/skills/status_report --adapter ...`。
6. **现场验证**（必须真机、有人值守、硬件急停在手）：`conformance --level motion --authorize`（或 `POST /v2/conformance`），再按现场验收流程人工确认。**一致性报告不等于现场验收证据。**

参考实现：`adapters/sim_quadruped.py`、`adapters/sim_arm.py`；ROS 2：`adapters/ros2.py`（`Ros2Adapter` + 可复用的 `Ros2Bridge` 与消息转换函数，文件顶部列出上线前 TODO）。

### 3.1 能力一致性测试（`conformance`）

`neuxmind_runtime.conformance` 对任意 `RobotAdapter` 实例或运行中的运行时执行三级测试，输出 `conformance.report.v1`。测试总是**经过 `EdgeRuntime`** 执行（适配器实例会被包进一个临时运行时：临时状态目录、内存遥测、不跑技能），所以参数经 spec 校验、运动经安全监督器，与生产路径相同。

```bash
neuxmind-runtime conformance --adapter sim_quadruped --level dry --out report.json        # 适配器实例
neuxmind-runtime conformance --adapter mypkg.acme:AcmeDog --level motion --authorize      # 真机须现场确认
neuxmind-runtime conformance --url http://robot:8790 --token $TOKEN --level dry           # 运行时在进程内自测
neuxmind-runtime conformance --url ... --mode client --level dry                          # 黑盒（旧运行时）
```
退出码：0 = 所请求级别通过，1 = 未通过，2 = 用法错误/被拒绝（如 motion 未加 `--authorize`）。Python：`conformance.run_adapter(adapter, "dry")`、`conformance.run(RuntimeTarget(runtime), ...)`、`conformance.check_manifest(fn)`（只做 static，不碰硬件）。

| 级别 | 用例（`<级别>.<能力>.<名称>`） | 判定 |
|---|---|---|
| static | `manifest.schema` `json` `fixed_body` `mandatory` `simulated` `model_runtimes` `safety_limits`；每项声明能力 `declaration` | v2 校验；严格 JSON；两次 `manifest()` 相同；必备能力；`simulated` 显式布尔；`model_runtimes` = `model.install.params.runtimes`（未声明 model.install 时须为空）；安全监督器接受限值；能力在 spec 中、主版本一致、参数已知/类型正确/**必填参数齐全**、关节限位长度与顺序、`arm.joint_position.joint_names`（v1.1 可选：互不相同、长度 = dof；未声明时 static 仍通过，但 evidence 注明动态机械臂用例将被阻断）、姿态子集、joint_position 动作块需 joint.state 与限位 |
| dry | 每个 state/stream：`read` | 读 3 次：spec 输出校验、单次 < 1 s；**每一次读数**都做物理合理性检查（电量 0–100、四元数归一（IMU/位姿/末端）、夹爪开度 0–1、关节名唯一且与位置等长、关节数 = dof、映射明确时在限位内、图像尺寸 > 0、时间戳不倒退、关节名顺序不变）；`system.health.ok` 每次都必须为 true |
| dry | `safety.stop.idempotent_latched` `latch_blocks_motion` `stop_epoch` | 连续两次停止都 `stopped:true`、各 < 1 s、锁存、底盘静止；锁存期间零值运动指令被拒（`estop_latched`）、未授权 reset 403 且仍锁存、授权 reset 解锁；停止前记录的代数在 stop+reset 后下发被拒（`stale_after_stop` + 事件） |
| dry | `model.install.refuses_bad_requests` | 未授权（403）、非法 sha、元数据地址 `https://169.254.169.254/...`、`file:///etc/passwd` 一律被拒（同步 4xx 或运行失败），已安装模型列表不变；适配器自带 model.install 时无法列出已装模型，只验证拒绝 |
| dry | `legged.posture.refused_without_motion` | 规范没有姿态读取能力，“重发当前姿态”无法验证且可能让趴下的机器人站起，所以不做；改为：非标准值 400，套件自己锁存期间合法姿态被拒（`estop_latched`），之后 `system.health` 仍 ok——两者都到不了适配器 |
| dry | `base.velocity.zero_setpoint`、`base.navigate_to.rejects_invalid_goal`、`legged.posture.rejects_unsupported`、`arm.joint_position.zero_displacement` / `rejects_out_of_limits`、`arm.ee_pose.rejects_out_of_reach`、`gripper.position.rejects_out_of_range`、`policy.action_chunk.zero_chunk` / `rejects_bad_dt`、`audio.speak.empty_text` | 零速度被接受且不武装看门狗、之后静止；`arm.joint_position.zero_displacement` 与 joint_position 空间的 `policy.action_chunk.zero_chunk` **不下发**，记 `not_applicable`（evidence `dry_skipped: true`，原因写明由 motion 级 `min_move` / `min_chunk` 验证；未声明 `joint_names` 时仍为 `blocked`）——dry 只证明请求校验与拒绝（R4b-02）；base_velocity 空间的零动作块照常执行；非法目标（四元数、工作区外 1 cm、限位外 0.01 rad、半径外 1 cm、开度、dt、不支持的姿态）必须被运行时拒绝——这类“依赖运行时拒绝”的探测只在进程内（有 dry 守卫）执行，黑盒模式下记 `blocked` |
| motion | `base.velocity.watchdog` / `turn` / `lateral` / `stop_latency`；`base.navigate_to.min_goal` / `cancel` / `stop_latency`；`legged.posture.stand_noop`；`arm.joint_position.min_move` / `cancel` / `stop_latency`；`gripper.position.min_move` / `cancel` / `stop_latency`；`arm.ee_pose.min_move`；`policy.action_chunk.min_chunk`；`safety.stop.final_still` | 需 `hardware_authorized=true`。**幅度（真机与仿真相同，不随倍速放大）**：速度 ≤ min(0.1 m/s, 25 % max_vx)，看门狗用例约 0.03 m、停止用例约 0.05–0.1 m；导航最小目标 0.1 m（本体行驶超过 0.25 m 即停车中止），中断用例目标 0.2 m（均向前）；腕关节 0.05 rad（中断用例目标 0.1 rad，0.2 rad/s）；夹爪先张到 1.0（若原本闭合这一步是全程）再到 0.9，中断用例目标 0.4；末端 +2 cm（z）。**判定**：看门狗在 `timeout_s + 0.5 s` 内归零；里程计峰值速度在指令的 ±50 % 内、速度不反向、机体系前向位移 > 0、侧向位移 ≤ 20 % 前向 + 5 mm 且 vy 峰值 ≤ 20 % vx；`turn`：wz ≤ min(0.2 rad/s, 25 % max_wz)、0.3 s 后交给看门狗，wz 峰值 ±50 %、不反向、偏航变化 > 0、平移 ≤ 2 cm（R4-04）；`lateral`（只对 `holonomic:true` 且 `max_vy>0`，否则 not_applicable）：vy ≤ min(0.1 m/s, 25 % max_vy)，同样核对符号/幅度、侧向位移 > 0、前向位移 ≤ 20 % 侧向；`watchdog`（vx）与 `lateral`（vy）另查偏航：wz 峰值 ≤ 0.2 rad/s ×（v / 0.1 m/s）、偏航变化 ≤ 0.05 rad（R4b-05）；导航到达且位移方向一致；机械臂指令（`min_move`、复原、中断、joint_position 动作块）执行期间以约 50 Hz 按 `joint_names` 读 `joint.state`，任何关节超出 [起点, 目标] ± 0.02 rad（`arm_envelope_rad`）→ 立即停止、锁存、中止（`joint_names` 声明错误时在约 0.02 rad 内停下，复审中曾转 1.57 rad）；关节/末端/夹爪以 `joint.state`/`arm.ee_state`/`gripper.state` 核对（不只看动作自报结果）；动作中取消/停止后 **< 1 s 静止**，之后**持续静止 2 s**（覆盖看门狗周期与延迟恢复）；每次复位后持续静止 1 s；测试结束时解除套件自己的锁存并持续静止 2 s（`final_still`）。**blocked**：无 `localization.pose` 时导航（不用 odom 冒充 map）、无 `gripper.state` 时夹爪中断、无 `arm.ee_state` 时末端、未声明 `joint_names` 或 `joint.state` 中找不到声明的关节时机械臂与 joint_position 动作块。lie/sit 等大幅姿态需现场人工验证 |

报告字段：`schema`、`conformance_id`、`target`、`robot`、`manifest_digest`（与云端 `catalog.manifest_digest` 相同：经 `validate_manifest_v2` 规范化后的 v2 清单——即 `/v2/manifest`，含运行时原生追加的 `model.install`——做 `json.dumps(m, sort_keys=True, ensure_ascii=False)`，UTF-8 编码后 SHA-256，供云端精确绑定）、`manifest_v1_digest`（桌面端 brainos `digest(manifest_v1_view(m))`：`json.dumps(v1, sort_keys=True)`（ASCII 转义）的 SHA-256，与 `/v1/evaluate` 的 `robot_manifest_digest` 相同）、`time_scale`（目标声称的倍速，只记录）、`executed_time_scale`（实际执行倍速，恒为 1）、`trust`（`self_reported: true`、`same_process_as_adapter`、`note`：信任边界）、`dry_guard`（`active_during_dry`、`violations`）、`external_motion_refused`（server 模式）、`simulated`、`evidence_kind`（仿真为 `simulation`，否则 `conformance`，**从不是 `device_run`**）、`field_acceptance: false`、`disclaimer`、`level`、`hardware_authorized`、`static_ok`/`dry_ok`/`motion_ok`（未请求为 `null`）、`levels.<级别>.{requested, ran, ok, passed, failed, skipped, blocked, blocked_reasons}`、`capabilities[]`（**规范中的每项能力**都有 `declared` 和每级 `passed|failed|skipped(+reason, skip_kind)`；未声明的能力 = skipped "不支持或尚未实现"）、`cases[]`（证据：读数样本、时延、停止调用时延、静止用时、位移、看门狗归零用时）、`thresholds`、`warmup_s`、`prelatched`、`aborted`（中止原因 + 结束时状态的一句话）、`final_latched`、`final_motion`（`{checked, still, restops, motion_seen, held_still_s, ...}`：结束时观察到的真实状态，`still` 为 `true`/`false`/`null`=无法核对）、`end_state`（同上的一句话；未能确认静止时以“警告：…请立即使用硬件急停”开头，CLI 摘要另加醒目提示）、`restops`（套件发出的追加停止次数）、`call_timeout`（第一次超时的调用，无则 `null`）。

规则：跳过分 `not_applicable`（无可测内容）与 `blocked`（无法测试，如测试前已锁存、static 未通过、未声明 `joint_names`、黑盒模式测不了停止代数）；**某级别 `ok` 当且仅当它运行了、没有 failed、没有 blocked，且更低级别都 ok**。动态级别开始前最多等 5 s 让 `system.health` 变为 ok（ROS 2 话题刚订阅时尚无数据），不作判定。

**测试自身的安全规则（第 04 轮审查后重写）**：

1. **锁存归属只看停止计数**（Q4-01）：运行时 `/v1/status` 报告 `stop_count`（只增不减）与 `stop_epoch`。套件开始时记下计数，自己每停一次，计数必须恰好 +1；任何时刻（每个用例前后、每次停止前后、每次复位前）计数与套件自己的停止次数不符 = 有其他来源的停止 → **立即中止全部后续用例、保持锁存、绝不再复位**，报告 `aborted`。复位一律是**条件复位** `reset {hardware_authorized:true, expect_stop_count:N}`：计数不是 N 时运行时返回 409 `stop_count_mismatch` 并保持锁存（检查与复位之间到达的停止由运行时的复位竞态保护再次锁存）。测试开始前已锁存 → 所有动态用例 `blocked`，套件不发停止、不复位。运行时不报告 `stop_count`（旧版本）→ 套件不复位，需要解锁的用例 `blocked`。
2. **dry 不运动的保证**（Q4-02 / R4-01 / R4b-02）：能到达适配器的运动类调用只有零速度与 base_velocity 空间的零动作块。**dry 不向适配器发送任何机械臂目标或 joint_position 动作块**（`joint_names` 声明错误时，“目标=当前关节角”会变成真实转动：复审中按字母序声明的本体在 dry 级被转了 1.57 rad，倒序 3.0 rad），守卫对它们一律拒绝；超限/超长 dt 等拒绝探测仍发给运行时，必须在运行时被拒。另外 dry 开始时记下本体位置（底盘位姿、按名称的关节角、夹爪开度），**每个 dry 用例之后**都与之比较：底盘 > 1 cm 或偏航 > 0.01 rad、任一关节 > 0.01 rad、夹爪 > 0.02（`dry_displacement_m/_rad`）→ 该用例失败、停止、保持锁存、中止，后续用例一律不发送。**机械臂指令顺序只来自清单**：`arm.joint_position.params.joint_names`（能力标准 v1.1 新增的可选参数，按 `position` 分量顺序列出关节名，与 `joint.state.names` 同一套名称）。套件**从不**用 `joint.state` 的顺序推断指令顺序（ROS 的 `/joint_states` 常按字母序或 URDF 序发布，与控制器顺序不同：复审中这样的诚实本体在 dry 级被转了 1.57 rad）。未声明 `joint_names`、或 `joint.state` 中缺少任一声明的关节、关节名重复、两次读取顺序不同 → 所有用到当前关节角的用例（机械臂 dry/motion、joint_position 动作块、只能用机械臂探测时的 `stop_epoch`）`blocked`、**不发送**；声明后按名称取值，`joint.state` 多出的关节（夹爪指节等）与顺序变化都不影响，测试中声明的关节消失 → 中止。进程内执行时套件还在适配器前装一个 **dry 级适配器运动调用守卫**：任何运动类 `invoke/start_action` 若不能证明零位移（速度非零、任何机械臂目标或 joint_position 动作块、导航/姿态/夹爪/末端），守卫拒绝下发并把用例判为“套件缺陷”（`dry_guard.violations`）。`stop()` 与看门狗的零速度不经守卫。motion 级开始前卸下。黑盒模式没有守卫，依赖运行时拒绝的探测一律 `blocked`。运行时的 joint_position 动作块逐步限速同样按 `joint_names` 取起点；**未声明 `joint_names` 时运行时拒绝 joint_position 动作块**（fail closed，错误信息提示按 `ros2_control` 的 `joints:` 顺序声明；R4b-03），`joint.state` 关节名重复或缺少声明的关节同样拒绝。
3. **任何失败都停车**（Q4-03）：motion 级任一用例失败（判定不符、适配器异常、调用被拒、超时）→ 立即停止、保持锁存、中止后续用例；dry 级用例异常时若仍有运行未结束同样处理。
3a. **锁存不等于已停**（R4-02 / R4b-01 / R4b-04）：中止时**无论是否已锁存**都再发一次停止；持续静止检查中看到运动也一样。**中止时的停止、以及任何调用超时之后的所有再停，都是计数的急停**（`emergency_stop(keep_reason=True)`：停止计数 +1，已锁存时保留原锁存原因，例如操作员的）——被放弃的 `adapter.reset()` 之后返回时，监督器因此能识别竞态、保持锁存并再停一次。其余已锁存时的再停用 `halt_motion`（不改锁存原因、不增加停止计数）；纵深防御：监督器把 `adapter.reset()` 执行期间到达的**任何**适配器停止（包括不计数的 `halt_motion`、看门狗停车）都当作竞态。黑盒 `--mode client` 的再停发 `POST /v2/stop {"reason", "keep_reason": true}`，保留操作员的锁存原因（R4b-08；计数 +1，旧运行时忽略该字段）。中止后套件再观察本体：要求持续静止 **5 s**，其间每看到运动就再停一次（间隔 ≥ 0.3 s、最多 20 次、总时长 ≤ 12 s）；还有超时后未返回的复位/指令调用时，持续观察到其返回或总时长用完。报告定稿前**再核对一次**（`final_motion.final_recheck`），此时看到运动就再停（计数）并如实写入。`final_latched` 一律在最后**读回**运行时的真实锁存状态，不做推断；仍未返回的调用列在 `final_motion.calls_in_flight` 与 `end_state` 中。结果原样写进 `final_motion` / `end_state`：只有观察到持续静止才写“本体在 N s 观察窗内持续静止（观察窗之后才恢复的运动无法由本报告发现）”，最后看到的是运动就写“警告：已发出停止 N 次，仍观察到运动…请立即使用硬件急停”，无法读取反馈时写“未能核对”。注意：每次停止后都会自行恢复的驱动（复审 `stop_resume_velocity`）任何软件停止都停不住，报告会如实给出警告，现场只能用硬件急停。
3b. **调用超时**（R4-05）：套件对目标的每次调用（读取、指令、动作、运行查询、取消、停止、复位等）都在独立线程执行并限时（默认 5 s，复位 15 s；`Thresholds.adapter_call_timeout_s` / `reset_call_timeout_s`）。超时 → 该用例失败、发出停止（计数）、保持锁存、中止；超时的是读取时不再依赖该适配器的反馈（`final_motion.still = null`），超时的是复位/指令等时仍按 3a 观察，但 `end_state` 始终以“警告：适配器调用曾超时…请现场确认并使用硬件急停”开头（`call_timeout` 记录调用）；套件随即返回，server 模式的运动租约立即释放（复审：读取卡死 60 s 时测试从 65 s 缩短到约 5.5 s 结束）。卡住的调用线程被放弃（daemon），停止路径不取锁、不受影响，测试期间随时可停。
4. **坐标系**（Q4-04）：导航目标只用 `localization.pose`（地图坐标系）计算；没有就 `blocked`，不发目标。
5. **倍速**（Q4-05）：只有运行时自带的仿真类（`SimQuadruped`/`SimArm` 本身，且 `simulated:true`、进程内）才被识别；它们的动态级别临时以 1× 执行（`RealClock.set_time_scale`，机器人时间连续），结束后恢复。其他一律按 1× 处理，适配器或 `/v1/status.adapter` 自报的倍速只记录不采信；所有目标有绝对上限，不随倍速放大。
6. **运动租约**（Q4-15）：`POST /v2/conformance` 运行期间，运行时拒绝其他来源的运动类调用、技能运行、`/v1/start`、`/v1/evaluate` 与复位（409 `conformance_running`，计入 `external_motion_refused`）；停止永远可用（操作员停止会让套件在下一次检查时中止并释放租约；适配器卡住时最迟在调用超时后释放，见 3b）。
6a. **复位与停止交错**（R4-03 / R4b-01，运行时安全监督器）：适配器 `reset()` 执行期间到达的停止（急停，或任何不锁存的 `halt_motion`/适配器停止）会让监督器保持锁存，并在 `reset()` 返回后**再调用一次 `adapter.stop()`**（发 `safety.reset_raced` 事件），防止驱动在复位完成时恢复上一条指令或完成起身。
7. 有运行未结束时拒绝开始（409 `runs_active`）。

**信任边界**（Q4-13）：报告是**自报**的。`--adapter` 与 `POST /v2/conformance`（server 模式）下，套件与适配器代码在同一进程，适配器代码可以篡改判定（例如替换套件函数）；任何基于适配器自身反馈的测试都发现不了“读数与动作同时伪造”（滑行但读数静止、看门狗失效但读数伪装）或“识别出正在被测而表现不同”；`hardware_estop` 等声明无法验证。报告的 `trust` 字段与免责声明写明这一点。只测试可信的适配器代码；一致性报告永远不能替代现场人工验收。长期方案是套件与适配器分进程、运行时签名报告（第 05 轮候选）。

dry 级别的授权说明：运行时要求所有运动类调用带 `hardware_authorized=true`，即使是零值；dry 级别只对零速度、零位移的 base_velocity 动作块、必须被运行时拒绝的探测，以及解除自己造成的锁存（条件复位）带该标志（`_assert_zero` 与 dry 守卫保证零位移），因此不要求 `--authorize`。

### 3.2 适配器脚手架（`scaffold`）

```bash
neuxmind-runtime scaffold --spec mapping.json --out ./acme_rover
neuxmind-runtime scaffold --out ./bot --name acme_bot --vendor Acme --model B1 --family mobile \
    --cap safety.stop,system.health,base.velocity --impl safety.stop=ros2 --impl system.health=ros2 \
    --impl base.velocity=ros2:/cmd_vel --param base.velocity.max_vx=0.5 --param base.velocity.max_vy=0 \
    --param base.velocity.max_wz=0.7 --param base.velocity.holonomic=false --param safety.stop.hardware_estop=true
```
输入 `neuxmind.adapter_scaffold.v1`（`name`、`vendor`、`model`、`family`、`instance_id?`、`simulated?`、`safety?`、`ros2?{node_name, stale_after_s, map_frame}`、`capabilities[{id, params, implementation, evidence_id?, notes?}]`）。参数按 spec 校验，**必填参数缺失直接报错**（脚手架不猜限值）；必备能力缺失时自动加入为待实现。

**输入白名单与原子生成**（第 04 轮审查 Q4-09）：`evidence_id`/`instance_id` 只允许 `[A-Za-z0-9_.:-]`；`vendor`/`model`/`display_name` 为单行可打印文本，不含引号、反斜杠、反引号、`<>|$`；`adapter_version` 为 PEP 440 子集；实现提示目标只允许 `[A-Za-z0-9_./:@%+=,~?&#-]`，ROS 2 名称、节点名、坐标系名按 ROS 规则；`ros2.stale_after_s` 0.05–10 s；厂商扩展 ID 须为 `x.<厂商>.<名称>`（小写）。生成的代码里不出现任何外部原文：身份只写进 `adapter.json`，代码中的提示与证据一律 `repr()`。所有文件先在内存中生成并校验（`compile`、`adapter.py` 的 AST 只能含预期结构、JSON、TOML），再写进同级临时目录后原子改名；任何失败都不留下文件。

输出：`pyproject.toml`（`neuxmind-adapter-<name>`，依赖 `neuxmind-runtime`）、`README.md`（映射表、证据、TODO）、`<name>/adapter.py`（`class <Name>Adapter(RobotAdapter)`；`IMPLEMENTED` 是唯一开关；`offline_manifest()` 不碰硬件）、`<name>/adapter.json`（身份、限值、ROS 2 名称、映射与证据；可用 `NEUXMIND_<NAME>_CONFIG` 指向覆盖 JSON）、`tests/test_adapter.py`（一致性 static、未实现能力未声明且抛 `NotImplementedError`、有 ROS 2 时对实例再跑 static）。加载：`serve --adapter <name>.adapter:<Name>Adapter`。

**机械臂 `joint_names`**（第 05 轮 R4c-03）：映射含 `arm.joint_position` 时，生成的 `adapter.py` 带 `ARM_JOINT_NAMES = None   # TODO(joint_names)` 占位与注释，生成的 README 醒目提示：关节名按**指令顺序**填写，取自 `ros2_control` 控制器配置（如 `controllers.yaml` 中 `joint_trajectory_controller` 的 `joints:` 列表），**不要照抄 `/joint_states` 的顺序**（常按字母序或 URDF 序）；名称须与 `joint.state` 的 `names` 一致。保持 `None` 时运行时拒绝 joint_position 动作块（400 `joint_names_required`），一致性测试机械臂用例 `blocked`；输入里已给出 `joint_names` 时写进 `adapter.json`，代码中只留核对顺序的注释。声明错误 dry 照样通过，要到 motion 级由运动包络发现。

ROS 2 模板：`base.velocity`（Twist 发布）、`base.odometry`（Odometry）、`sensor.battery`（BatteryState，0..1→%）、`sensor.imu`（Imu）、`joint.state`（JointState）、`localization.pose`（PoseWithCovarianceStamped，如 `/amcl_pose`）、`base.navigate_to`（Nav2 NavigateToPose，取消与 cancel-all）、`safety.stop`（零 Twist + 取消全部 Nav2 目标，**不是硬件急停**）、`system.health`（所订阅话题新鲜度）。相机、激光、机械臂、夹爪等 `ros2:` 提示生成待实现方法并附建议接口。`safety.stop` 或 `system.health` 未实现时 `manifest()` 抛 `NotImplementedError`：运行时拒绝加载、生成的测试失败——有意如此。

ROS 2 测试替身：`tests/ros2_double/`（纯 Python 的 rclpy/消息包替身 + 内存话题图 + 一台假差速机器人，发布 `/odom` `/battery_state` `/imu` `/joint_states`，提供 Nav2 式 `navigate_to_pose`），`use_sim_time=true` 因此清单为 `simulated:true`。只在测试中放进 `sys.path`；**通过替身上的一致性测试不说明任何真实 ROS 2 机器人的行为**。

## 4. 写技能

技能包 = `skill.json`（`skill.package.v1`）+ 文本文件。示例见 `examples/skills/`：

| 技能 | requires | 说明 |
|---|---|---|
| `patrol` | `base.navigate_to`, `safety.stop` | 依次到达航点；有 `legged.posture` 时先站立 |
| `wave` | `arm.joint_position`, `gripper.position`（约束 dof ≥ 6） | 类式技能，摆臂开合夹爪 |
| `status_report` | `system.health` | 任何本体可运行；有什么读什么（跨本体演示） |

```python
def run(ctx, waypoints, loops=1):          # entry: "skill.py:run"；也可以是类（实例的 run 方法）
    if ctx.has("legged.posture"):          # 可选能力先判断
        ctx.call("legged.posture", posture="stand")
    for wp in waypoints:
        ctx.check_stop()                   # 停止/取消/超时时抛出
        ctx.call("base.navigate_to", goal=[wp[0], wp[1], 0, 0, 0, 0, 1])   # action：阻塞直到完成
        ctx.progress(0.5, "到达航点")
    return {"done": True}                  # JSON 可序列化
```

SkillContext：`call(cap, **args)`、`read(cap)`、`check_stop()`、`sleep(s)`（机器人时间，可被停止打断）、`log(msg, **data)`、`progress(f, msg)`、`params`、`has(cap)`、`param(cap, name)`、`manifest`、`simulated`、`time_left()`。

在沙箱中运行时（默认）还须知道：当前目录是技能包的只读副本，可写的只有 `$TMPDIR`（运行结束即删除）；`print` 只保留最后 4000 字符（运行结束时作为 `sandbox.output` 事件）；不能 `import neuxmind_runtime` 的内部模块（`neuxmind_runtime.skills` 中的 `Skill` 与异常类可以）；需要的第三方包须对技能用户可读；`ctx` 方法是到运行时的同步请求（每秒最多 2000 次）。收到停止后 `ctx.call/read/check_stop/sleep` 立即抛 `SkillStopped`，不退出的进程 1 s 后被强杀。

规则：只能调用 `requires`/`optional` 中声明的能力（`safety.stop`、`system.health` 总可用）；参数按 `parameters` 声明在**设备端**校验（第 06 轮 Q5-05，所有入口：`/v2/skills/{id}/run`、`/v1/start` 的每个应用步骤（准入前一次查完，任何一步不合格整个运行不启动）、`/v1/deploy`/`/v1/evaluate`/`/v1/validate` 的“参数有效”检查）：未知参数拒绝、支持 `default`；类型；`min`/`max`（数值，`vector`/`floats`/`pose` 逐项，`object` 对其中**每个数值**——例如航点坐标 ±5 m 时 `[[100,0]]` 被拒；第 07 轮 Q6-02；声明了 `min`/`max` 的 `object` 里出现非数值标量（数字字符串 `"1e3"`、布尔、`null`）→ 400 `param_type`，第 07 轮修复 Q7-07；对象的**键**不受约束），`pose` 的 `min`/`max` 只约束位置 x/y/z、不约束四元数（第 07 轮修复 Q7-07：以前声明 0–10 时偏航 -90° 被拒），`minimum`/`maximum` 是 `min`/`max` 的别名（桌面表单用这种写法，设备同样执行；两种写法同时出现且不一致 → `param_declaration_invalid`）；`choices`（enum，或任何类型）/ `options`（值或 `{value, label}`），**按类型严格比较**（第 07 轮 Q6-06：`true`、`1.0` 都不等于 `1`）——例外（第 07 轮第二次修复 Q7b-04）：声明为实数的参数（`float`，以及 `vector`/`floats`/`pose` 的元素）里 JSON 整数就是那个浮点数，`1 == 1.0`（浏览器 `JSON.parse`/`JSON.stringify` 会把 `1.0` 变成 `1`），技能收到的是 `1.0`；布尔与数值、字符串与数值仍严格区分，`int`/`enum` 不变；NaN/无穷大一律拒绝；参数值嵌套超过 64 层 → 400 `param_type`（请求 JSON 本身深到解析器递归溢出 → 400 `bad_request`（“请求 JSON 嵌套过深”），不再是 500，Q6-08）；`length`（`vector`/`floats` 及数组）/ `min_length` / `max_length`（数组、字符串）。不合格 → 400，正文（与桌面 ux04 约定）`{error, message（中文，可直接显示）, code, param, min, max, choices（未声明为 null）, field, problem, constraint?, value?, step?（应用第几步，从 0 起）}`，`code` 为 `param_out_of_range` / `param_choice` / `param_length` / `param_required` / `param_type`；未知参数为兼容保留 `code: bad_request`（`problem: param_unknown`，`param` 为第一个未知名）。声明本身不合格（`min` 非有限数、`min > max`（含别名）、`min`/`max` 写在设备无法执行的类型上（`str`/`bool`/`enum`/`blob_ref`，字符串长度请用 `min_length`/`max_length`）、`choices`/`default` 含 NaN、长度为负、`default` 不满足声明等）→ 安装时拒绝，已安装的在运行时 400 `param_declaration_invalid`（fail closed）；超时取包内 `timeout_s`（运行请求可以更短，不能更长）；停止异常继承 `BaseException`，`except Exception` 不会误吞；`ActionFailed` 可捕获。同一 `id@version` 内容不可变，改代码请升版本。安装前做 `check_compatibility`，不兼容拒绝。

## 5. HTTP API

认证 `Authorization: Bearer $BRAIN_EDGE_TOKEN`；错误 `{"error", "code", ...}`。标 ★ 的需要 `request_id`（幂等）。

| 方法 | 路径 | 请求 → 响应 |
|---|---|---|
| GET | `/v1/manifest` | `manifest_v1_view(v2)` |
| GET | `/v1/status` | `{instance_id, name, family, simulated, runtime_version, health, mode, estop_latched, latch_reason, stop_count, stop_epoch（第 04 轮审查后：停止计数只增不减，供一致性测试判断锁存归属）, manifest_changed, reset_in_progress, reset_in_progress_s, reset_hint（第 06 轮，见 §2“复位卡住时的操作员出路”）, capabilities, reset_expected_s（第 07 轮第二次修复，见 §2“复位过期”）, latched_motion（第 05/06 轮，见 §2“锁存守护”）, active_runs, motion_owner, adapter, sandbox, models, telemetry}`；`sandbox.mode` 为 `process`/`inprocess`，并给出用户、限额、`pids_cgroup`、`network_isolated:false`，以及（第 03 轮）`runtime_user`、`privilege_drop`、`python_usable`、`level: full|best-effort`、`best_effort_reasons`；`models` 见 §5.2，`telemetry` 见 §5.3 |
| POST | `/v1/validate` | `{application}` → `{accepted, passed, cases:[{name, passed, detail}], simulated}` |
| POST ★ | `/v1/evaluate` | `{application, hardware_authorized:true}` → 同步运行应用 → `{passed, status, error, application_digest, robot_manifest_digest, evidence_ref, evidence_kind, simulated, run_id, cases, precheck}`；加 `async:true` → 立即返回 `{accepted, evaluation_id, status, run_id, simulated}`（§5.1） |
| GET | `/v1/evaluations/{id}` | `{evaluation_id, status: running|completed|unknown, run_id, result?（与同步响应同结构）, error?}`；未知 id 404 `evaluation_not_found` |
| POST ★ | `/v1/deploy` | `{application, report_id}` → `{accepted, release_id, pinned:[id@version]}`（技能版本在此固定） |
| POST ★ | `/v1/start` | `{release_id, hardware_authorized:true}` → `{accepted, state, run_id}`（后台依次运行 steps） |
| POST | `/v1/stop` | 永远可用（含停止专用名额）→ `{accepted, stopped, latched, stopped_runs, stop_count}`；请求体 `{reason?, keep_reason?}`（`keep_reason` 为比较交换，见 §2） |
| POST ★ | `/v1/reset` | `{request_id, hardware_authorized:true}` → `{latched}`（同 `/v2/reset`；重放返回当前真实状态；上一次复位仍在适配器中 409 `reset_in_progress`，正文含 `message`/`hint`/`reset_in_progress_s`） |
| POST ★ | `/v1/models/install` | 启用 `--models-dir` 时走运行时原生实现（§5.2）→ `{accepted, run_id, status, model_id}`；适配器自带 `model.install` 时转给适配器；都没有时 400 `capability_not_provided` |
| GET | `/v2/manifest` | `robot.adapter.v2` |
| GET | `/v2/capabilities/registry` | spec 注册表 |
| GET | `/v2/state?ids=a,b` | `{id: outputs}`；不带 ids 返回全部可读能力 |
| POST ★ | `/v2/capabilities/{id}/invoke` | `{args, hardware_authorized, timeout_s?}` → command：outputs；action：`{run_id, status}`，有能力级默认超时（`base.navigate_to` 600 s、`arm.*` 60 s、`gripper.position` 30 s、`policy.action_chunk` = 步数×dt+5 s、其他 120 s），`timeout_s` 只能缩短；`safety.stop` 无需 request_id；`policy.action_chunk`（joint_position 空间）在本体未声明 `joint.state`/`joint_names` 时同步 400 `joint_state_required`/`joint_names_required` |
| GET | `/v2/skills` | `{skills:[{id, version, ref, requires, optional, parameters, digest, compatibility, ...}]}` |
| POST ★ | `/v2/skills/install` | `{package}`（含 `files`）→ `{installed, id, version, digest, compatibility, ...}`；不兼容 409 `incompatible` + `compatibility` |
| POST ★ | `/v2/skills/{id}/run` | `{parameters, hardware_authorized, version?, timeout_s?}` → `{run_id, status}` |
| GET | `/v2/runs` / `/v2/runs/{run_id}` | 列表 / `{status, progress, progress_detail, result, error, events, stop_reason, capability_access_revoked, ...}`；直接调用的动作（`kind:"capability"`，如 `model.install`）的 `progress` 跟随动作进度，`progress_detail = {capability, progress, feedback?}`（第 04 轮修复） |
| POST | `/v2/runs/{run_id}/cancel` | `{accepted, status}` |
| POST | `/v2/stop` | 同 `/v1/stop` |
| POST | `/v2/reset` | `{hardware_authorized:true, request_id?, expect_stop_count?, expire_s?}` → `{latched}`；带 `expect_stop_count` 时为条件复位：停止计数不符 409 `stop_count_mismatch` 并保持锁存；一致性测试运行期间其他来源 409 `conformance_running`；上一次复位的 `adapter.reset()` 尚未返回 409 `reset_in_progress`；`adapter.reset()` 超过 `expire_s`（默认/上限 30 s）才返回 → 保持锁存，409 `estop_latched` `cause: reset_expired`（第 07 轮修复 Q7-10；`/v1/reset` 同） |
| GET | `/v2/telemetry?since=<seq>` | `{events:[{seq, ts, type, ...}], last_seq}`；跨重启连续（§5.3） |
| GET | `/v2/runs/{run_id}/events?since=&limit=` | 回放一次运行的全部事件（含重启前）→ `{run_id, events, last_seq, more, oldest_seq}`；未知运行 404 |
| GET | `/v2/models` | 已安装模型 `{models:[{sha256, model_id（目录 ID，可空）, name, runtime, source, file, size, installed_at, path}]}`（§5.2） |
| POST ★ | `/v2/conformance` | `{level: static|dry|motion, hardware_authorized}` → `{accepted, conformance_id, run_id, status:"running", level, simulated}`；在运行时进程内异步执行一致性测试（§3.1）；motion 未授权 403；同时只允许一个（409 `conformance_busy`）；dry/motion 有运行未结束 409 `runs_active`；`level` 非法 400 `invalid_level` |
| GET | `/v2/conformance` / `/v2/conformance/{id}` | 列表 `{conformance:[{id, status, level, static_ok, dry_ok, motion_ok, simulated}]}` / `{id, status: running|completed|failed|unknown, level, progress, report?（conformance.report.v1）, error?}`；重启时未结束的为 `unknown`（不重跑）；未知 id 404 `conformance_not_found` |
| DELETE ★ | `/v2/models/{sha256 或唯一的目录 ID}` | `{hardware_authorized:true, request_id?}` → `{deleted, model_id: <sha256>}`；目录 ID 对应多个版本时 409 `model_ambiguous`；运行中技能用着时 409 `model_in_use`（带 `runs`）；未安装 404 `model_not_found` |

### 5.1 异步现场测试

桌面端 connector 超时 8 s，长的现场测试改用 `async:true`（INTERFACES_V1 §4.1）：

- 立即返回 `{accepted:true, evaluation_id, status:"running"}`，运行在后台进行；`GET /v1/evaluations/{id}` 轮询。`result` 与同步 `/v1/evaluate` 的响应结构完全相同（含 `simulated`、`evidence_kind`、`simulation://` 证据引用）。
- 幂等：同 `request_id` 同内容返回同一 `evaluation_id`；同 id 不同内容（包括同步/异步之别）409 `request_id_reused`。
- 结果由运行结束钩子写入（不依赖轮询），只写一次；重启后仍可查询已完成的结果。
- **停止/急停/取消/超时**：评估立即结束为 `completed`、`passed:false`、`status:"stopped"`，`cases` 中追加失败项“现场测试未被停止”（含急停原因）。停止后第一次轮询就返回，不等技能进程的宽限期；之后运行收尾也不会改写它。
- 提交时急停已锁存（预检中唯一失败的是“急停未锁存”）：同样接受并立即 `completed`、`passed:false`，不启动运行；其他预检失败与同步模式一样 409 `validation_failed`。预检通过但准入时遇到停止（`estop_latched`/`stale_after_stop`）也记为失败的已完成评估。
- 运行时重启时未完成的评估为 `unknown`（`error` 说明不会重放），不重放；同一请求再次提交返回同一 `evaluation_id`。
- `async` 须为布尔值；不带该字段保持原同步行为（旧桌面端兼容）。

### 5.2 端侧模型安装（`model.install`）

`--models-dir DIR` 启用运行时原生实现（INTERFACES §5.1）：运行时把 `{"id": "model.install", "params": {"runtimes": [...]}}` 加进 `/v2/manifest`（`--model-runtimes onnxruntime,tensorrt`），仿真本体也可用。适配器清单里自带 `model.install` 时以适配器为准（运行时不加、`/v1/models/install` 转给 `adapter.install_model`）。漂移检测仍只比较适配器自己的 `manifest()`，追加的能力不会触发"清单变化"停车。

```bash
neuxmind-runtime serve --adapter sim_quadruped --state-dir /var/lib/neuxmind \
    --models-dir /var/lib/neuxmind-models --model-runtimes onnxruntime \
    [--model-max-mb 2048] [--model-timeout 30] [--model-file-root /media/usb/models] [--model-ca-file ca.pem]
curl -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
    -d '{"request_id":"m1","hardware_authorized":true,"args":{"package_ref":"https://…/det.onnx","sha256":"<64 hex>","runtime":"onnxruntime"}}' \
    http://127.0.0.1:8790/v2/capabilities/model.install/invoke      # → {run_id}；GET /v2/runs/{run_id}
```

- **输入**严格按 spec：`package_ref`、`sha256`、`runtime`，可选 `model_id`（目录 ID）与 `name`（各 1–200 个可打印字符；多余字段 400）。已安装模型的**标识是包的 SHA-256**（目录名、删除路径、`model.install` 输出里的 `model_id`）；可选的目录 `model_id`、`name` 作为元数据记入 `model.json`，`/v2/models` 返回 `sha256`、`model_id`（目录 ID，可空）、`name`。v1 与 v2 记录方式相同（v1 参数可放在顶层或 `args` 中，`sha256` 不分大小写，响应 `{accepted, run_id, status, model_id: <sha256>, sha256, catalog_model_id}`）。同一 sha 已安装时重装是空操作，保留第一次的元数据。0.3.0 首版写的 `model.json`（`model_id` 为 sha）读作"无目录 ID"。
- **清单**：运行时原生提供时，`/v2/manifest`（及 v1 视图）顶层 `model_runtimes` 由运行时填充，与 `model.install` 的 `params.runtimes` 一致，云端据此过滤端侧模型包。
- **地址策略**（第 03 轮审查 Q3-09）：https 主机名解析到内网、本机、链路本地、CGNAT、保留地址或云元数据地址时默认拒绝（私有镜像用 `allow_private_hosts` 显式放行）；SHA 不符的错误信息不再包含实际摘要（否则可作为读取不可读文件/内网资源的预言机）。剩余：解析与连接之间的 DNS 重绑定未防护；技能若声明了 `model.install` 仍可在沙箱里调用它（列入第 05 轮的技能网络隔离一起处理）。`name`/`model_id` 拒绝控制字符、双向覆盖符与零宽字符；sha 用 `fullmatch`（不再接受结尾换行）。
- **同步拒绝**（400/403，在创建运行之前）：未授权 403；sha 格式错；`runtime` 不在配置里（`runtime_unsupported`）；`package_ref` 不合规（`package_ref_refused`）：只允许 `https://`，`file://` 只允许 `--model-file-root` 目录内（按真实路径判断，符号链接指到外面也拒绝）的普通文件；`http://` 只允许配置的 `allow_http_hosts`（仅开发/测试，CLI 不开放）。
- **下载**：流式写到 `DIR/.partial/`（0700），边下边算 SHA-256；`Content-Length` 超上限立即失败，没有长度时边下边数；单次连接/读取超时；重定向最多 5 跳，**同协议**才跟随且每跳复检，跨协议（→ `file://`、→ `http`、→ `https`）一律拒绝；本机镜像不走代理。
- **校验与原子安装**：sha 不符删除临时文件并失败；通过后在 `.partial/` 里暂存 `{<文件名>, model.json}`、fsync，再 **`rename` 到 `DIR/<sha256>/`**（原子），然后设为只读（文件 0444、目录 0555，模型根目录平时也是 0555，只在安装/删除的瞬间打开）。`model.json`：`model_id, sha256, name, runtime, source（去掉查询串，签名令牌不落盘）, file, size, installed_at`。同一 sha 重复安装幂等，第二次直接成功（事件 `model.install_skipped`）。**同一 sha 正在下载时**，新的请求合并到那个运行（响应同一 `run_id` 与 `merged:true`，技能 `ctx.call` 路径则跟随同一下载），只下载一次；**同时下载的安装最多 `max_concurrent`（默认 2）个**，超出 409 `install_busy`；下载前与每 8 MiB 检查模型所在文件系统的可用空间，须保留 `min_free_bytes`（默认 256 MiB，状态目录、锁存与遥测通常在同一块盘上）；整个下载另有墙钟总时限（默认 3600 s）。`model.install` 运行的超时按墙钟计（仿真倍速不再压缩它）。启动时清掉 `.partial/` 残留（中断的安装从不对外可见），无效条目在 `/v1/status` 的 `models.invalid_entries` 中列出。不解压归档：包就是一个文件。
- **安全级 `system`**：需要 `hardware_authorized=true`（"现场确认可以变更端侧软件"，总线层对 `safety=system` 统一检查），**不是运动**：不经限幅、不占运动资源（可与运动技能并行）、急停锁存期间也可安装。
- **失败原因可回放**：任何失败（含磁盘满、权限、服务器提前断开的短读）都发 `model.install_failed`（带 `run_id`，磁盘错误映射为中文原因）。
- **停止**：全局 stop / cancel 会取消进行中的安装——`cancel()` 只置一个事件并 `shutdown` 下载套接字（非阻塞），停止路径从不等待它；工作线程随后删除临时文件，被取消的安装绝不发布（发布前再查一次）。即使下载线程卡在一个永不完成的连接上，stop 也立即返回（测试：<0.2 s）。
- **技能可见**：`ctx.models` → `{sha256: {model_id（目录 ID）, name, runtime, sha256, size, file, path}}`（副本，改了不影响运行时）。技能直接按 `path` 只读打开；沙箱中写入、删除、新建、改名都被拒绝（root 运行时技能是另一个 uid；普通用户运行时靠 0444/0555，见 §0.2）。降权运行时要求 `--models-dir` 的各级父目录对技能用户可穿越（不要放在 0700 的状态目录里）。
- **删除**：`DELETE /v2/models/{model_id}`（需授权）。运行中的技能/应用若在启动时的 `ctx.models` 里拿到过该模型，返回 409 `model_in_use`（技能启动时的可见列表与删除检查在同一把锁下，没有竞态）；技能启动后才安装的模型不算被占用。删除是"改名到 `.partial/` 后再删"，原子地从目录与列表中消失。
- `/v1/status.models`：`{enabled, dir, runtimes, installed, installing, max_bytes, file_roots}`。事件：`model.install_started/progress/installed/install_skipped/install_failed(cancelled)/deleted`，安装事件都带 `run_id`（执行安装的运行）、`sha256` 与目录 `model_id`，所以 `/v2/runs/{run_id}/events` 能回放下载进度与失败原因。运行记录 `/v2/runs/{run_id}` 的 `progress` 在下载过程中实时更新（已下载字节 / `Content-Length`，安装完成前 ≤ 0.99；无 `Content-Length` 时保持 0，以 `progress_detail.feedback.bytes` 为准），`progress_detail.feedback = {bytes, total}`；下载按到达的数据读取（`read1`），慢速镜像上进度与取消都及时。第 03 轮遗留“运行中 progress 不更新”已修，SDK 可不再绕过。

### 5.3 遥测持久化与回放

INTERFACES §5.2。`emit()` 仍然无锁、无 IO（停止路径照旧先执行）；常驻线程 `nx-telemetry` 每 0.1 s 把待写队列批量写入 `<state-dir>/telemetry.sqlite3`（WAL、0600），这是一个**有界环形存储**（默认 100 000 条，`--telemetry-capacity`，写入时删掉最旧的）。

- **seq 跨重启单调、绝不重复**（第 03 轮审查 Q3-02 修复）：正常关闭时全部落盘并记下"干净关闭 + 最后 seq"，下次从 `最后 seq + 1` 继续——**没有缺口**。其他任何启动（崩溃、断电、上次写盘失败、遥测降级、库被重建）从 `max(库中最大 seq, 磁盘预留, 墙钟微秒数) + 1` 开始并发 `telemetry.seq_floor` 事件：写线程在**写入每批之前**先把预留推进到领先 1 万，磁盘写不进去时预留跟不上，微秒下限保证新起点大于上一进程可能发出的任何 seq（那需要自 1970 年起每微秒超过一个事件）。代价是崩溃后 seq 跳到约 1.8×10^15，客户端只会看到缺口。唯一剩余情形：墙钟被拨回**且**库不可读。
- **写失败不丢批**：写入失败的批次保留下来下个周期重试（与新事件合计上限 20 万条，超出丢最旧并计入 `dropped_unpersisted`）。
- **遥测永远不阻止启动**（Q3-01，P1）：库损坏、同名路径是目录、预留值越界（>2^62）→ 改名为 `telemetry.sqlite3.corrupt-<时间>-<pid>`（连同 `-wal`/`-shm`，**不删除**）后重建，事件 `telemetry.quarantined`；被其他进程锁住（如有人用 `sqlite3` 命令行查看）或同一状态目录已有另一个运行时实例（`telemetry.lock` 上的 `flock`，Q3-17）→ 降级为内存遥测，事件 `telemetry.persist_failed`。启动时锁等待 0.1 s，其余 SQLite 等待 0.5 s；安全监督、停止、锁存持久化都不依赖遥测（测试：四种状态下启动后 `/v1/stop` <1 s）。
- `/v1/status` 只读遥测的缓存值、不碰数据库（Q3-08：库被锁时此前要等约 5 s）；`/v2/telemetry`、回放用独立的读连接，库忙时退回内存中的事件。
- `GET /v2/telemetry?since=<seq>`：比内存缓冲（5000 条）更早时从环形存储读，最新尚未落盘的部分从内存补齐；早于环形存储最旧一条的事件已被淘汰（`/v1/status.telemetry.oldest_seq`）。
- `GET /v2/runs/{run_id}/events`：某次运行的全部事件（`run.*`、`sandbox.*`、带 `run_id` 的其他事件），跨重启，分页 `since`/`limit`（最多 1 万），`more` 表示还有。运行记录里内嵌的 `events`（每个运行最多 300 条）不变。
- 磁盘卡住时：事件仍在内存中可读，stop 不受影响（测试：持有数据库锁期间 HTTP stop <1 s，恢复后事件落盘）；待写队列上限 20 万条，超出丢最旧并计数。
- `/v1/status`：顶层 `telemetry_degraded`；`telemetry` = `{persistent, degraded, degraded_reason?, quarantined?, start: clean|after_unclean|degraded, capacity, path, last_seq, first_seq_this_boot, unflushed, write_errors, last_write_error?, dropped_unpersisted, oldest_seq}`。`telemetry_capacity=None`（嵌入使用）时只在内存。

v1 应用中 step 的 `entrypoint` 解析为已安装技能 `id`（取最高版本）或 `id@version`；`deploy` 时固定为 `id@version` 并记录 digest。

常见错误码：`unauthorized` 401、`hardware_not_authorized` 403、`not_found`/`capability_not_provided`/`skill_not_installed`/`evaluation_not_found` 404、`estop_latched`/`stale_after_stop`/`manifest_changed`/`safety_rejected`/`busy`/`incompatible`/`request_id_reused`/`outcome_unknown`/`validation_failed`/`runs_active` 409、`adapter_fault` 502、连接满 `busy` 503。同一时刻只允许一个使用运动能力的运行（`busy`）。

幂等账本：只有在可能已产生副作用（适配器调用、运行线程已启动、技能文件已写入）之后失败，才记为 `unknown`（重试返回 409 `outcome_unknown`）；在此之前的失败（参数类型错误等）记为拒绝，重试得到同样的错误。技能参数先检查是否为对象，`float`/`vector` 拒绝非有限值；`/v1/validate` 把步骤里的类型错误（如 `parameters` 是列表、`requires` 是字符串）报告为失败的检查项，而不是 500。锁存时“急停未锁存”检查项会提示“请在桌面端解除急停（POST /v1/reset）”。

## 6. 运行仿真本体

```bash
cd runtime
export BRAIN_EDGE_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python3 -m neuxmind_runtime serve --adapter sim_quadruped --port 8790 --state-dir /tmp/nx-dog
# 已 pip install -e runtime 时等价于：neuxmind-runtime serve --adapter sim_quadruped ...
# 技能隔离：默认 --sandbox process；调试可用 --sandbox inprocess（不隔离）
# 仓库根目录：python3 -m runtime.neuxmind_runtime serve ...   （或 PYTHONPATH=runtime python3 -m neuxmind_runtime ...）
# 加速仿真：--time-scale 10（只有仿真本体接受）

python3 - <<'EOF'
import json, os, urllib.request, sys
sys.path.insert(0, "."); from neuxmind_runtime.skills import load_package_dir
def call(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8790" + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["BRAIN_EDGE_TOKEN"], "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())
print(call("/v2/skills/install", {"request_id": "i1", "package": load_package_dir("examples/skills/patrol")}))
print(call("/v2/skills/patrol/run", {"request_id": "r1", "hardware_authorized": True, "parameters": {"waypoints": [[2, 0], [2, 2]]}}))
EOF
```

仿真模型：四足为平面运动学（全向底盘、原地转向后直行的"导航器"、姿态非 stand 时不动、电量随时间和里程下降）；机械臂为关节空间同步线性插值 + 夹爪开度插值，没有物体，从不报告抓取成功。积分是惰性的（每次读/调用时积分到当前时刻），没有动力学、碰撞、噪声或障碍物。

## 7. 已知限制

- 未在任何真机上运行；ROS 2 桥接（含脚手架生成的 ROS 2 适配器）只在 rclpy 测试替身上运行过（内存话题图、同步投递、假机器人），真实 rclpy/DDS/QoS/Nav2 完全未运行。
- 一致性测试：报告自报、与适配器同进程可被篡改（见 §3.1 信任边界）；读数与动作同时伪造、识别测试模式、`hardware_estop` 声明无法发现；适配器仍可从调用栈等识别出正在被测（临时目录已改为通用前缀 `nx-rt-`）。机械臂关节对应只来自清单的 `arm.joint_position.params.joint_names`（v1.1 可选参数）；未声明的本体所有机械臂/joint_position 动作块动态用例 `blocked`。声明本身无法验证：dry 不发机械臂目标；motion 级按声明执行，声明错误时由运动包络在约 0.02 rad 内停下并中止（不是 0）。未声明 `joint_names` 时运行时拒绝 joint_position 动作块（旧适配器须补声明）。每次停止后都会自行恢复运动的驱动，软件停止停不住（报告给出警告）；中止后的观察窗为 5 s，窗外才恢复的驱动报告无法发现——但第 05 轮起运行时的锁存守护在整个锁存期间监视反馈并周期性再停（只能对声明了 `base.odometry`/`joint.state` 的本体生效，且反馈本身可能说谎；每次恢复仍会在被发现前运动约一个采样周期 0.1 s）。dry 通过不说明 `joint_names` 声明正确：机械臂映射只在 motion 级验证（R4c-03）。大幅姿态变化没有自动用例。黑盒 `--mode client` 没有 dry 守卫，依赖拒绝的探测与停止代数 `blocked`，dry 不会判为通过。若操作员的停止与套件自己的停止在同一瞬间发生，锁存原因可能显示为套件的原因（锁存本身与中止不受影响）。
- 技能沙箱不隔离网络和本机设备，未做 seccomp；非 root 部署时技能与运行时同用户（见 §2.2 剩余信任边界）。每次技能运行多一次 Python 启动（实测约 40 ms）。
- `base.navigate_to` 的速度由适配器的规划器决定，运行时无法限幅，只能检查目标与工作区；仿真导航忽略目标朝向。
- `/v1/evaluate` 的同步模式仍受桌面端 8 s 超时限制；桌面端应使用 `async:true`（§5.1）。
- 仿真本体的 `/v1/evaluate` 如实返回 `passed`，并标注 `simulated=true`、`evidence_kind="simulation"`、`simulation://` 引用；按契约由桌面端拒绝把它当现场证据（桌面端修复由桌面集成 Agent 负责）。
- `/v1/stop` 会锁存急停；运行时已提供 `/v1/reset`，桌面端“解除急停”入口由桌面集成 Agent 接入。
- 动作块的单步限速假设适配器按 `dt` 节奏执行；`dt` 与总时长已设上限（见限幅规则），但适配器若忽略 `dt` 一次性执行，仍只能靠逐步限位兜底。厂商扩展 `x.*` 在 spec 与 runtime 中是否算运动能力的判定仍不一致（runtime 一律视为运动并拒绝）。
- Nav2 停止语义只用替身对象测过，未在真实 ROS 2 上验证。
- `model.install` 原生实现只做"下载 + 校验 + 原子安装"，不解压、不验证模型能否被推理运行时加载，也没有签名验证（只有 SHA-256，对得上来源由云端授权保证）；https 的真实 CDN/对象存储未验证，测试只用本机 http 镜像与 file://。
- 遥测环形存储默认 10 万条，更早的事件被淘汰；崩溃时最后约 0.1 s 未落盘的事件会丢失（seq 留缺口）。
- 普通用户部署是"尽力而为"隔离（§0.2）：同用户技能可以 `chmod` 并替换已安装的模型文件，`/v2/models` 仍列出原 sha（第 03 轮审查 Q3-14，未修；建议加载模型前按 sha 复核）。
- 下载线程在"只发响应头、永不结束"的服务器上，停止后仍可能存活到 http.client 读完头部或逐操作超时（运行本身立即 `stopped`，不会发布任何内容；Q3-10 只加了总时限）。
- HTTP 无 TLS。
