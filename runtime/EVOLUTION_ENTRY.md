## 第 01 轮 · 2026-11-04 · 端侧运行时：从转发网关到真正的端侧 OS（runtime/）

- 目标：实现 INTERFACES_V1 §1 的端侧运行时——HAL、安全监督、可移植技能运行时、v1 兼容 + v2 网关、两个仿真参考本体、ROS 2 桥接骨架，并证明同一技能可跨本体复用。
- 关键决策：
  - 所有能力调用只走 `CapabilityBus`：spec 输入校验 → 安全监督 → 适配器 → spec 输出校验；技能只能调用自己在 requires/optional 中声明的能力。
  - 停止路径 = 锁存 Event + 直接 `adapter.stop()` + 置各运行停止事件；不经账本/数据库/锁/队列/网络；HTTP stop 先停车再读请求体。指令在途时若急停触发，`after_dispatch` 再次停车（消除检查-执行竞态）。
  - 急停锁存持久化、跨重启保持；解锁需新增的 `/v2/reset` + `hardware_authorized`（需总指挥确认，见变更请求）。
  - 技能是可信代码、进程内无沙箱；不配合停止的技能在 2 s 宽限后被撤销能力访问。
  - 仿真结果诚实标注：`simulated=true`、`evidence_kind="simulation"`、`simulation://` 证据引用。
  - 同一时刻只允许一个使用运动能力的运行；技能 `id@version` 不可变。
- 分工：端侧运行时 Agent → `runtime/neuxmind_runtime/`（hal、safety、bus、skills、runner、ledger、core、gateway、cli、store、events、clock、adapters/{sim_quadruped, sim_arm, ros2}）、`runtime/examples/skills/{patrol, wave, status_report}`、`runtime/tests/`（6 个文件）、`runtime/README.md`。
- 改动：仅新增 `runtime/` 目录；未修改 spec/、brainos/、docs/ 或其他目录。
- 验证：
  - **仿真通过**：`python3 -m unittest discover -s runtime/tests` 71 个测试全部通过（约 13.5 s，连续 3 次稳定）。覆盖 manifest 校验；限幅/拒绝/看门狗/急停锁存与 reset/在途急停；action_chunk 逐步限幅；技能安装兼容与不兼容；跨本体（status_report 在 SimQuadruped 与 SimArm 都运行成功，patrol 装到 SimArm 被拒，wave 反之）；运行成功/取消/超时/失败/未声明能力；SimQuadruped 长距离导航中 HTTP `/v2/stop` 0.5 s 内返回且位姿不再变化；Bearer 认证；request_id 幂等三种情形；重启后 running→unknown 且不重放；v1 端点字段。
  - **协议集成通过（仿真本体）**：未修改的 `brainos/service.py` Platform 通过真实 HTTP 完成 connect_robot → onsite_test → deploy → operate start/stop 全流程。
  - 根目录原有测试 72 个（20 跳过）、spec 测试 7 个仍全部通过。
  - **真机未验证**：没有在任何物理机器人上运行；限幅、看门狗和停止延迟只在仿真本体上测过。
  - **ROS 2 未验证**：只用替身对象测试了消息转换函数；rclpy 节点、QoS、Nav2 动作客户端从未运行。
- 遗留：→ 下一轮候选
  - 契约：`/v2/reset` 入契约；`/v1/validate` 实际以 `accepted` 判定（契约写 `passed`），两者都返回；`/v2/capabilities/{id}/invoke` command 返回裸 outputs（限幅信息只在遥测）。
  - 桌面端：识别 `simulated`/`evidence_kind`，不把仿真当现场证据；增加"解除急停"入口；`/v1/evaluate` 超过 8 s 会在桌面端记为 unknown，需要异步现场测试协议。
  - 技能沙箱（独立进程 + 能力代理）；`model.install` 真实实现；ROS 2 桥接在真实 ROS 2 + Nav2 上验证；首台真机适配器与现场安全验证（看门狗、停止延迟、限幅）。

## 第 01 轮修复 · 2026-11-04 · 端侧运行时：按独立审查修复安全与健壮性问题（runtime/）

- 目标：修复审查报告 `docs/evolution/reviews/2026-11-05-iter01-review.md` 中 runtime 的 P1（R-01…R-08）与 P2（R-09…R-12），顺手修 R-14/R-15/R-16，补齐存活变异 R1、R7 的测试。
- 关键决策：
  - **停止代数（epoch）**：每次 stop +1；运行在准入时、HTTP 直接调用在请求到达时记录代数；监督器检查、下发前最后检查、下发后检查和 `SkillContext` 每次调用都比对，不一致即拒绝（`stale_after_stop`）。被停止的运行即使在 reset 之后也永远不能再下发。
  - **安全层不信任清单**：`SafetySupervisor` 初始化时复核限值（有限、正数、限位长度=dof、lower<upper 等），不合格拒绝启动；限幅器任何异常 fail closed；下发值必须全为有限值。动作块缺少可用 `joint.state` 时拒绝。
  - **停止保障路径**：网关改为有界连接（32 个普通名额，超限立即 503）、请求读取总期限 10 s；另保留 4 个只服务 `/v1/stop`、`/v2/stop` 的名额，满时驱逐尚未表明是 stop 的连接。选同端口保留名额而非独立端口：桌面端只有一个 endpoint，独立端口同样可被泛洪。
  - **停止路径不创建线程**：锁存由常驻线程持久化；锁存记录按（启动序号, 代数, 阶段）单调写入；复位先写盘再清内存且 stop 获胜；异常关机后启动默认锁存（SIGTERM 视为正常关闭）。
  - 看门狗独立线程、不做存储 IO；v2 直接调用的 action 有能力级默认超时；账本只在可能已产生副作用后才记 `unknown`。
  - 新增 `/v1/reset`（需 `request_id` 与 `hardware_authorized`），重放返回当前真实锁存状态。
  - ROS 2：目标发送中被取消时，在 future 回调里立即取消；stop 同时调用 Nav2 取消全部目标；`simulated` 来自配置或 `use_sim_time`；配置拒绝 NaN。
- 分工：端侧运行时修复 Agent → `runtime/neuxmind_runtime/`（safety、bus、skills、runner、ledger、core、gateway、cli、新增 effects、adapters/ros2）、`runtime/tests/`、`runtime/README.md`、`runtime/PROGRESS.md`。
- 改动：只改 `runtime/`。
- 验证：
  - **仿真通过**：`runtime/tests` 共 107 个测试全部通过（原 71 个 + 新增 36 个）。按新语义更新了 3 个预期失败测试；`test_full_delivery_flow` 改为 `test_simulated_body_flow`，不再断言桌面端接受仿真证据。
  - **审查脚本复验**：`rt_stale_after_reset` 的结果为 False（在途指令未到达本体）；`rt_limits`（绕过 spec）的全部非法清单都被拒绝启动，动作块无上下文/长度不符时被拒绝；`rt_slowloris` 用 400 个半截连接测试，线程数从 5 只涨到 41，10 s 后回到 5；`rt_stop_locks` 在所有锁被占住时 stop 仍在 5–7 ms 内完成；`rt_watchdog_store` 显示看门狗 0.21 s 触发（修复前 3.08 s）；`rt_misc` 中参数错误记为拒绝、validate 返回失败项、action 默认 deadline 为 600 s。
  - **变异验证**：R1（去掉锁存主检查）被 `test_latched_motion_never_reaches_adapter` 杀死；R7（去掉 finalize 时的撤销）被 `test_revoked_run_cannot_call_or_mutate_its_record` 杀死；其余 runtime 变异的结果见交付报告。
  - **真机未验证**；ROS 2 停止语义只用替身对象验证过。
- 遗留：→ 下一轮候选
  - 动作块的长度上限和 `dt` 上限；`x.*` 的运动判定统一到 spec（R-13，变更请求）；技能沙箱；异步现场测试协议。
  - 运行记录的持久化仍是同步的（已与看门狗分离，但存储卡顿仍会延误宽限期结束处理）。
  - 网络层 DoS（SYN 泛洪、带宽耗尽）超出软件层的处理范围，依赖防火墙和硬件急停。

## 第 01 轮复审修复 · 2026-11-05 · 端侧运行时：复审 P2（N-02/N-03/N-04/N-06）与存活变异（runtime/）

- 目标：修复复审报告 `docs/evolution/reviews/2026-11-05-iter01-rereview.md` 中与停止、限幅直接相关的 P2，并补测试杀掉 runtime 的存活变异 M2、M4、M6。
- 关键决策：
  - N-02：reset 也推进停止代数；请求的代数在到达时（写账本之前）记录，运行准入时再比对。锁存期间到达、被慢存储拖到 reset 之后的请求，返回 `stale_after_stop`，不会下发。另设"停止计数"专门判断 reset 是否与 stop 竞争；复位中止时写入一条更新的锁存记录。
  - N-03：看门狗拿不到速度锁时不再跳过，改为不等锁直接 `adapter.stop()`，并在短线程里发零速度。适配器契约（`hal.py`）写明：`stop()` 和零速度必须能与进行中的 `invoke` 并发调用。
  - N-04：清单在启动时深拷贝、校验、冻结（tuple / 只读映射）。限位比较对 NaN 安全。监视线程每秒比对适配器清单，发生变化就停车、拒绝运动（`manifest_changed`），直到重启。`ctx.param` 返回普通副本。
  - N-06：动作块 `dt ≤ 2/rate_hz`（未声明 `rate_hz` 时 ≤ 0.5 s），步数 ≤ 1000，总时长 ≤ 10 s。
- 分工：端侧运行时修复 Agent → `runtime/neuxmind_runtime/{safety,bus,runner,core,skills,hal}.py`，新增 `runtime/tests/test_rereview.py`。
- 改动：只改 `runtime/`。
- 验证：
  - `runtime/tests` 共 125 个测试全部通过（新增 16 个）。
  - 复审脚本复验：`rt_epoch_stress` B 部分的 command 和 action 都返回 `stale_after_stop`，适配器零下发；C 部分看门狗在指令卡住期间 0.21 s 发出零速度（修复前从未发出）；A 部分两轮压力测试 0 违例。`rt_manifest_mutation` 显示原地改成 NaN 或 ±1e6 都被拒绝，监督器的列表不再与适配器共享。`rt_action_reset`、`rt_conn_attack`、`rt_evict_flood`（泛洪期间 189/189 次 stop 成功）均通过。
  - 变异测试在 `git worktree` 中进行，结果见 PROGRESS.md。
  - 真机未验证。
- 遗留：适配器如果不按 `dt` 执行动作块，仍只有限位兜底；`x.*` 判定统一（R-13）。

## 第 02 轮 · 2026-11-08 · 端侧运行时：技能进程隔离、异步现场测试、可安装包（runtime/）

- 目标：实现 INTERFACES_V1 §4.1（异步现场测试）、§4.2（技能进程隔离，本轮重点）、§4.4（runtime 可安装包）；推翻 ADR-010（"技能在运行时进程内，本轮不做沙箱"）。
- 关键决策：
  - **subprocess + socketpair 上的 JSON 行协议**，不用 multiprocessing：后者 pickle 任意对象并把父进程模块状态带入子进程，协议面不可控。协议只有 8 种请求（call/read/check_stop/stopped/time_left/sleep/log/progress），字段固定、逐个校验；子进程的 `ctx` 是代理，主进程用真正的 `SkillContext` 服务每个请求，所以能力总线、安全监督、停止代数、撤销检查一项不少。授权、代数、声明能力、限值只存在于主进程。
  - **每次运行一个子进程**（应用的每个步骤也是），`python3 -I -B`，环境从零构造（无令牌），只传协议 socket，自成会话；root 运行时降权为 `neuxmind-skill`/`nobody` 并清空附加组；非 root 时运行时自设 non-dumpable。工作目录 = 技能包只读副本 + 私有 tmp。子进程在技能代码之前对自己施加 rlimit（AS/CPU/NOFILE/NPROC/FSIZE/CORE，软=硬）、NO_NEW_PRIVS、PDEATHSIG。不用 `preexec_fn`（多线程下不安全）。
  - **可选 pids cgroup**：rlimit + 进程组抓不到 `setsid()` 的孙进程；运行时对 pids cgroup 有写权限时每个技能进程树一个 cgroup。每个运行时有独立子目录，只清理 PID 已死的遗留目录。
  - **停止**：停止路径先停车，再非阻塞 `SIGTERM`（不等待）；`SIGTERM` 在子进程中只置标志，代理 `ctx.call/read/check_stop/sleep` 随后本地抛 `SkillStopped`（不在技能代码任意位置抛异常——实测那样会逃出技能自身代码）；启动期间到达的通知握手后补发；监视线程在 1 s 宽限期后整组（及 cgroup 全员）`SIGKILL`；子进程退出后主进程侧守卫让在途请求在下发前作废、进行中的动作被取消。
  - **fail closed**：畸形/超大/乱序/未知/多字段消息、握手超时、意外退出 → 整组强杀、运行失败、发 `sandbox.violation`。另有请求限流（2000/s）、每进程 500 条日志上限、日志数据截断、保留键改名（防伪造遥测事件类型）。
  - 导入守卫 + 替身 `neuxmind_runtime.skills`（`Skill`、异常类）让现有技能和 SDK 模板不改即可运行；守卫明确只是护栏，边界是进程。
  - `inprocess` 仅调试、须显式 `--sandbox inprocess`，在 `/v1/status` 的 `sandbox` 与 `runtime.started` 事件中可见；`/v2/manifest` 不变（它是 spec 校验的 `robot.adapter.v2`，见变更请求）。
  - **异步现场测试**：`async:true` 立即返回 `evaluation_id`；结果由运行结束钩子写入且只写一次；停止/取消/超时后第一次轮询即 `completed`/`passed:false`（cases 追加"现场测试未被停止"），不等子进程宽限期；提交时已锁存也接受并立即判失败；重启后未完成为 `unknown` 不重放；同步模式不变。
  - **可安装包**：`runtime/pyproject.toml`（`neuxmind-runtime` 0.2.0，依赖 `neuxmind-spec`，入口 `neuxmind-runtime`）；相对路径引导保留。root + 私有 venv 时技能用户执行不了解释器 → 启动时检查并退回真实解释器，`--sandbox-python` 可指定。
- 分工：端侧运行时 Agent → 新增 `neuxmind_runtime/{sandbox.py, sandbox_child.py, evaluations.py}`、`pyproject.toml`、`tests/{test_sandbox.py, test_async_evaluate.py, test_packaging.py}`；修改 `runner.py`、`skills.py`、`core.py`、`gateway.py`、`cli.py`、`__init__.py`、`tests/_support.py`（可选 sandbox 参数）、`tests/test_v1_compat.py`（桌面端已改用 async，跟随 `evaluation_id` 后断言不变）、README、PROGRESS。
- 改动：只改 `runtime/`。
- 验证（全部仿真本体，Linux、root、Python 3.13）：
  - `runtime/tests` 171 个全部通过（原有 126 个在默认 process 沙箱下不改语义通过；`test_v1_compat` 1 处改为跟随 `evaluation_id`；新增 test_sandbox 27、test_async_evaluate 13、test_packaging 5）；spec 11、sdk 48（含 SDK 模板生成技能的测试）、cloud 160、tests/ 113（20 跳过）全过。
  - 沙箱攻击：导入运行时内部模块全部被拒，绕过守卫后进程内无运行时活对象；令牌、父进程 environ/mem、状态目录读取、包目录写入、提高 rlimit 均被拒，3 GiB 分配 MemoryError；18 种伪造/畸形/超大消息全部 fail closed 且适配器零调用；忽略 SIGTERM 的死循环、fork 炸弹（含 setsid 逃逸）、日志/print 刷屏期间 HTTP stop 均 <1 s 并立即停车，子进程约 1 s 后被强杀；子进程在导航中崩溃 → 导航被取消；运行时被 SIGKILL → 技能进程随之死亡。
  - pip：独立 venv 中 `pip install -e spec -e runtime` 成功，`neuxmind-runtime serve` 经 HTTP 跑通沙箱技能与异步现场测试。
  - 变异测试（git worktree，主工作区未改动）：43 个变异，39 杀死，4 个等价（见 PROGRESS.md）。
  - 真机未验证。
- 遗留：网络未隔离（能经网络/DDS/串口直接驱动本体的系统上恶意技能仍可绕过运行时）；无 seccomp；非 root 部署时技能与运行时同用户（可写状态目录）；每次运行约 40 ms 启动开销；真机未验证。

## 第 02 轮审查修复 · 2026-11-08 · 端侧运行时：按独立审查修复沙箱与评估问题（runtime/）

- 目标：修复审查报告 `docs/evolution/reviews/2026-11-08-iter02-review.md` 中 runtime 的 P1（Q2-01）与 P2/P3（Q2-04、Q2-05、Q2-08）。只改 `runtime/`。
- 关键决策：
  - **Q2-01（P1，符号链接跟随 → root 递归改权限）**：沙箱根目录不再放在全局可写的系统 `/tmp`。与运行时同用户时放 `<state-dir>/sandbox`（运行时 0700 私有树内）；降权运行时放系统临时目录下的 `neuxmind-sandbox-<euid>`（运行时所有、0711，技能用户只能穿越不能创建条目 → 无法植入符号链接；降权子进程无法穿越 0700 状态目录，故该场景不放其中）。清理改为 `lstat` 判定、不跟随符号链接、只删本 uid 所有的真实目录、`os.fwalk(follow_symlinks=False)` + `fchmod`、`rmtree` 基于 `dir_fd`。预创建为他人所有或为符号链接的共享基目录一律拒用并退回私有 `mkdtemp`；系统临时目录的 sticky 位阻止替换基目录。
  - **Q2-04（P2，无 cgroup 时逃逸后代存活）**：无可写 pids cgroup 时设 `PR_SET_CHILD_SUBREAPER`，`setsid()` 逃逸的后代在直接父进程退出后重挂到运行时；运行结束时按 `/proc` 扫描（技能 uid、重挂到本运行时、不在在途子进程表）`SIGKILL`，并按**显式 pid** `waitpid` 回收（变异测试中发现：成为 subreaper 后被杀的孤儿变成运行时的僵尸，原实现不回收会泄漏；不用 `waitpid(-1)` 以免抢走 subprocess 子进程的退出状态）。维护在途子进程表避免误杀。`/v1/status` 增 `sandbox.child_subreaper` 与 `process_containment:"best-effort"`；启动输出与 README 写明局限（短暂窗口、建议提供 cgroup）。保留 `allow_no_cgroup` 配置位。
  - **Q2-05（P3）**：评估内部 `rejected`（提交失败丢弃的孤儿记录）不再对外泄露，`get_evaluation` 对非三态返回 404 `evaluation_not_found`。
  - **Q2-08（P3）**：技能日志/进度配额从“每子进程”改为“每运行（RunState）累计”，多步应用的各步共享同一配额，无法逐步新开进程挤掉遥测中的 `safety.*` 事件。
- 分工：端侧运行时审查修复 Agent → `neuxmind_runtime/{sandbox.py, core.py, runner.py, evaluations.py, cli.py}`、`tests/test_sandbox.py`、`tests/test_async_evaluate.py`、README、EVOLUTION_ENTRY、PROGRESS。
- 改动：只改 `runtime/`。
- 验证（仿真本体，Linux root）：runtime/tests 180 全过（新增 9 个：沙箱基目录归属与非全局可写、清理不跟随顶层/嵌套符号链接、清理跳过符号链接与他人目录、植入 `rt-<死PID>` 链接后受害目录权限与内容不变、无 cgroup 下 setsid 逃逸被收割、收割者只杀未跟踪且重挂到本进程的技能 uid 进程并按 pid 回收、多步应用日志按运行封顶、评估 rejected 不外泄）；spec 11、sdk 48、tests/ 123（20 跳过）不受影响。变异测试（git worktree，主工作区未改动）：新防线 14 个变异全部杀死；第一遍 12 个中 7 个存活（防线层层冗余互相掩盖、收割测试过粗），逐层补单元测试后全杀。真机未验证。
- 说明（与审查建议的差异）：审查建议沙箱根目录放进 0700 状态目录。实测（sdk `nx sim up` 集成测试）降权子进程无法穿越 0700 的状态目录及其私有祖先目录，技能无法启动；因此只在“与运行时同用户”时放状态目录，降权时放临时目录下运行时所有的 0711 目录——这同样消除了植入向量（技能用户不能在其中创建条目），且不需要放宽状态目录权限。
- 遗留：无 cgroup 时逃逸后代到被收割有短暂窗口（期间无法驱动本体，但网络未隔离）；Q2-09（`spec/neuxmind_spec.egg-info` 入库）在 `spec/`、根 `.gitignore`，不在本目录，报告总指挥处理。

## 第 03 轮 · 2026-11-08 · 端侧运行时：Python 3.10、非 root 运行、连接计数根因、原生 model.install、遥测持久化（runtime/）

- 目标：INTERFACES_V1 §5.0（平台基线）、§5.1（端侧模型安装）、§5.2（遥测持久化与回放）。只改 `runtime/`。
- 关键决策：
  - **Python 3.10**：`requires-python = ">=3.10"`。产品代码本就没有 3.11+ 依赖；唯一的 `tomllib` 在测试里，回退到 `tomli`/内置小解析器。3.10 下暴露的真问题不在语法，而在环境：root 运行时的解释器若装在 `/root` 下（uv/pyenv），降权后的技能用户执行不了 → 解释器选择增加"同版本系统解释器"回退，仍不可用时 `/v1/status` 标 `python_usable=false`、`level=best-effort` 并给出处理方法，测试带原因跳过而不是 80 个失败。
  - **非 root = 尽力而为模式**：`/v1/status.sandbox` 增 `runtime_user`、`privilege_drop`、`level`、`best_effort_reasons`；CLI 启动告警。复现了用户机器上"非 root + 3.10 test_sandbox 出 E 并卡住"：根因是收割者只在 root 降权时工作，fork 炸弹测试的 setsid 逃逸进程在测试后继续 fork，负载升到 37，后续测试超时；另有一个无超时的 `readline()`。收割者改为对本运行时全部未跟踪的、**不在运行时会话中**的同 uid 后代**先 SIGSTOP 再 SIGKILL**（技能进程总在新会话中启动，`setsid` 只能建新会话，所以会话是同用户时可靠的判据；停住的进程不能再 fork），收尾时反复到稳定，监视线程在技能退出后 30 s 内每 0.5 s 再扫；在途子进程表改为进程级（一个进程内多个运行时互不误杀）。
  - **连接计数根因**：不是 stop 驱逐——驱逐只发生在停止专用名额内，从不改变普通计数（新增单元测试锁定）；而是最早的空闲普通连接在请求期限到期被断开、普通名额随之空出，打开 300 个连接本身要 1–2 s（监听队列满、SYN 重传），期限恰好落在最后检查附近。实现正确、行为可描述："满"是瞬时状态，普通名额只在连接结束（响应完、客户端关、期限到）时释放。测试改为在确定的满状态下检查，语义不放宽，并在满状态下再验一次 stop。
  - **model.install 原生**：`models.py`。内容寻址（`model_id` = SHA-256，spec 输入里没有目录 id，见变更请求）；流式下载 + 双重大小检查 + SHA-256 + 暂存 fsync + 原子 `rename`；同协议重定向逐跳复检、跨协议拒绝；`file://` 按真实路径限定在配置目录；本机镜像不走代理；签名查询串不落盘。`cancel()` = 事件 + 套接字 `shutdown`，停止路径不等待；发布前再查一次取消。总线对 `safety=system` 统一要求授权；它不是运动，锁存时也可装、可与运动并行。`ctx.models` 只读副本，模型文件 0444、目录 0555、模型根目录平时也 0555（非 root 下同用户也不能顺手改名，root 下技能是另一 uid 完全不可改）。删除需授权，运行中技能启动时看到过的模型 409 `model_in_use`（可见列表与删除检查同锁）。非 root 时目录必须可写才能 `rename`（root 不受此限）——实测发现后改为先移动再设只读。
  - **遥测持久化**：`emit()` 不变（无锁、无 IO）；写线程 0.1 s 批量写 SQLite 环形存储（默认 10 万条）；seq 用磁盘预留量保证跨重启单调、正常关闭无缺口、崩溃只留缺口不复用；`/v2/runs/{id}/events` 回放。
- 分工：端侧运行时 Agent → 新增 `neuxmind_runtime/models.py`、`tests/test_models.py`（26）、`tests/test_telemetry.py`（9）；改 `events.py`（重写）、`sandbox.py`、`sandbox_child.py`、`bus.py`、`core.py`、`runner.py`、`skills.py`、`gateway.py`、`cli.py`、`__init__.py`/`pyproject.toml`（0.3.0、>=3.10）、`tests/{_support,test_sandbox,test_gateway,test_packaging,test_ros2_and_cli}.py`、README、PROGRESS。
- 改动：只改 `runtime/`。
- 验证（仿真本体）：见 PROGRESS 第 03 轮与交付报告——runtime/tests 180 → 223 个，在 3.13 root、3.10 root、3.13 普通用户、3.10 普通用户四种环境全过（普通用户 2 个带原因跳过：需要 root 降权的状态目录隔离、需要可写 pids cgroup），无进程残留；连接满测试连续 30 次通过（另一轮在后台负载下 30/30）；spec 11（3.13 与 3.10）、sdk 48、tests/ 141（23 跳过）不受影响；worktree 变异测试 32 个：第一遍 27 杀死、4 存活、1 未应用，补 4 个测试（重定向两层各自单测、发布前取消、带查询串的来源、模型根目录启动即只读）后 31/32 杀死，剩 1 个（S7：收尾时只扫一遍而非扫到稳定）被监视线程的周期扫描掩盖，属冗余防线。真机、真实 https CDN、真实推理运行时均未验证。
- 变更请求（总指挥）：(1) spec `model.install` 输入增加可选 `model_id`/`name`（目录 id），否则 v2 只能以 sha 标识模型、SDK `nx models install <model_id>` 的名字无处可放（v1 路径已接受）；(2) INTERFACES §1.1 第 7 条补一句计数规则："普通名额只在连接结束（响应完成、客户端关闭、请求读取期限到期）时释放；停止专用名额的驱逐不影响普通名额"；(3) §5.1 写明 `model_id` = SHA-256、`/v1/models/install` 返回 `{accepted, run_id, status, model_id}`（异步，轮询 `/v2/runs/{id}`）、安装在急停锁存时也允许；(4) §5.0 写明 root 运行时的解释器必须对技能用户可执行（Jetson 用系统 python3.10）。
- 遗留：模型签名验证与推理运行时加载自检；非 root 部署的隔离仍是尽力而为（同用户可改状态目录、可 chmod 模型）；适配器若在运行时下启动带新会话的守护进程会被无 cgroup 收割者清理（文档已写）；遥测崩溃时丢最后约 0.1 s；真机未验证。

### 第 03 轮补充 · SDK Agent 发现的三个问题（runtime/）
- v2 `model.install` 的可选输入 `model_id`/`name`（spec 已加）此前在 `ModelStore.start_action` 被丢弃 → v1、v2 同一记录方式：`model.json` 与 `/v2/models` 返回 `sha256`、`model_id`（目录 ID，可空）、`name`。已安装模型的标识仍是 sha256（目录名、删除路径、`model.install` 输出与 DELETE 响应里的 `model_id` 保持 0.3.0 含义）；DELETE 也接受唯一的目录 ID（多版本时 409 `model_ambiguous`）；兼容首版 `model.json`（`model_id`=sha 读作无目录 ID）。
- `model.install_*` 事件带 `run_id`（总线把执行运行的 id 传给原生能力），另带 `sha256`/目录 `model_id`；`/v2/runs/{id}/events` 可回放进度与失败原因（含 precheck 失败）。
- 运行时原生提供 `model.install` 时由运行时填充清单顶层 `model_runtimes` = `params.runtimes`（v1 视图同样携带）；漂移检测仍只比较适配器自己的清单。
- 验证：runtime/tests 227 个，3.13 root 全过、普通用户 nxtest 全过（2 个带原因 skip）；新增 5 个测试。sdk/tests 57 个中 2 个失败：它们断言 `/v2/models` 记录的 `model_id` 等于 sha256（`test_install_from_cloud_api` 第 207 行、`test_cli_install_reinstall_mismatch_remove` 第 271 行），与本轮契约"`/v2/models` 的 `model_id` 为目录 ID"相反，需 SDK Agent 改为比较 `sha256`。

## 第 03 轮审查修复 · 2026-11-08 · 端侧运行时：遥测永不阻止启动、seq 绝不重复、收割者不误伤、安装限流（runtime/）

- 依据：`docs/evolution/reviews/2026-11-08-iter03-review.md`。修 Q3-01（P1）、Q3-02/05/06/08（P2）、G（测试缺陷）与 runtime P3：Q3-09/10/11/12/13/17；Q3-14 只写入已知限制。只改 `runtime/`。
- 关键决策：
  - **Q3-01 遥测降级而不是阻止启动**：`EventBus.persist()` 不再抛异常。损坏、同名目录、预留越界（>2^62）→ 改名 `telemetry.sqlite3.corrupt-<时间>-<pid>`（连同 WAL/SHM，不删除）后重建；被他进程锁住、同一状态目录已有另一个实例（新增 `telemetry.lock` 的 `flock`，顺带修 Q3-17）→ 内存遥测。启动时所有 SQLite 等待 ≤0.1 s。`/v1/status` 顶层 `telemetry_degraded` 与 `telemetry.degraded_reason/quarantined/start`。
  - **Q3-02 seq 绝不复用**：写失败的批次保留重试（有界、丢弃计数）；预留在**写入之前**推进；元数据区分"干净关闭"——只有干净关闭才从 `最后 seq + 1` 无缺口继续，其余启动一律从 `max(库最大 seq, 预留, 墙钟微秒) + 1` 开始（`telemetry.seq_floor` 事件）。审查场景（锁住期间发 15000 条后崩溃）现在下次起点 > 客户端见过的最大 seq；审查存活的 R5（flush 后不推进预留）由"墙钟被拨回时预留必须覆盖未落盘事件"测试杀死。
  - **Q3-08**：`/v1/status` 只读缓存值；读路径用独立的读连接与短等待，库忙时退回内存事件。
  - **Q3-05 收割者**：同用户模式下目标必须带技能标记环境变量 `NEUXMIND_SKILL_SANDBOX`（技能子进程设置、后代继承）或处于技能进程启动时的会话中；运行时/适配器的辅助进程（含新会话）从不被发信号或 `waitpid`。真实进程测试：辅助进程存活，终止后退出状态仍是 `-SIGTERM`；setsid 逃逸的技能进程仍被收割。
  - **Q3-06 安装限流**：同 sha 在途请求合并为同一个运行（`merged:true`；技能 `ctx.call` 路径跟随同一下载）；同时下载上限 `max_concurrent=2`（409 `install_busy`）；下载前与每 8 MiB 检查可用空间（保留 256 MiB）。
  - P3：https 默认拒绝内网/本机/链路本地/CGNAT/元数据地址（解析后判定，`allow_private_hosts` 放行），SHA 错误不再返回实际摘要（Q3-09）；下载总时限（Q3-10）；`safety=system` 能力按墙钟超时（Q3-11）；`fullmatch`、拒绝控制/双向覆盖/零宽字符（Q3-12）；任何失败都有 `model.install_failed`，磁盘错误映射中文原因，短读报"下载不完整"（Q3-13、R3）。
  - G：`test_async_evaluate` 只看本次启动的事件（`first_seq` 之后）。
- 改动：`events.py`（重写持久化部分）、`models.py`、`sandbox.py`、`core.py`、`runner.py`、`tests/{test_telemetry,test_models,test_sandbox,test_async_evaluate}.py`、README、PROGRESS。
- 遗留：DNS 重绑定（解析与连接之间）未防护；技能声明 `model.install` 仍可自行调用（随第 05 轮网络隔离处理）；只发头部永不结束的服务器使下载线程在停止后存活到逐操作超时；非 root 下同用户技能清空环境并 setsid 可逃过尽力而为的收割；Q3-14。

## 第 04 轮 · 2026-11-08 · 端侧运行时：能力一致性测试套件、适配器脚手架、ROS 2 替身、model.install 进度（runtime/）

- 目标：INTERFACES_V1 §6.1（`neuxmind_runtime.conformance`）、§6.2（`neuxmind_runtime.scaffold`）与第 03 轮遗留（`model.install` 运行中 progress、用户机器上 test_packaging 失败）。只改 `runtime/`。
- 关键决策：
  - **一致性测试总在 `EdgeRuntime` 之上执行**：适配器实例被包进临时运行时（临时状态、内存遥测、不跑技能），运行中的运行时在进程内执行（`POST /v2/conformance`），`--url --mode client` 走公开 API 黑盒。这样 spec 校验、安全监督、看门狗、停止代数都是生产路径，测的是"适配器 + 运行时"整体，而不是绕过安全层直接调适配器。
  - **dry 不产生运动**：能到达适配器的只有零速度（代码断言）、目标=当前关节角、零位移动作块；其余 dry 用例是"安全监督器必须拒绝"的非法目标，目标位置都取当前位置，即便被错误接受也不会平移。运行时要求运动类调用一律带授权，因此 dry 只对这些零值调用与解除自己造成的锁存带 `hardware_authorized`；motion 必须显式授权（CLI `--authorize`，否则退出码 2；HTTP 403）。
  - **测试自身的安全**：有运行未结束拒绝开始；只解除自己造成的锁存（锁存原因核对），测试前已锁存则保持并沿用操作员原因（相关用例 `blocked`）；中途有人按停止 → 中止并保持锁存；motion 中 1 s 内未静止 → 中止并保持急停锁存。
  - **判定**：skip 分 `not_applicable` 与 `blocked`；级别 `ok` = 运行了、无 failed、无 blocked、且低级别 ok。黑盒模式测不了停止代数 → dry 不判通过（宁可保守）。报告 `field_acceptance:false`，`evidence_kind` 只有 `simulation`/`conformance`，从不是 `device_run`；`capabilities[]` 覆盖规范中全部能力（未声明 = 不支持或未实现），直接可做"适配报告"。
  - **脚手架**：`IMPLEMENTED` 是唯一开关；未实现能力抛 `NotImplementedError` 且不进清单；必填参数缺失直接报错（不猜限值）；必备能力未实现时 `manifest()` 抛错，运行时拒绝加载、生成的测试失败（有意）。`ros2:` 映射复用从 `adapters/ros2.py` 抽出的 `Ros2Bridge`（私有 rclpy context、执行器线程、带新鲜度的订阅、Nav2 客户端与 cancel-all）。
  - **ROS 2 替身**：`tests/ros2_double/` 纯 Python 的 rclpy 与消息包替身 + 假差速机器人 + Nav2 式动作服务；`use_sim_time=true` 使清单 `simulated:true`；只在测试的 `with ros2_double()` 中进入 `sys.path`，退出即清除。顺带修了 `Ros2NavHandle` 把 Nav2 CANCELED(5) 报为 failed 的问题（现为 cancelled）。
  - **model.install 进度**：`kind:"capability"` 的运行，其 `progress` 跟随动作的 `ActionStatus.progress`；`progress_detail.feedback = {bytes, total}`；下载改用 `read1`（按到达的数据读），慢镜像上进度与取消都及时。
  - **test_packaging**：根因是用户机器 `TMPDIR` 在 `/sessions/...` 下、上级目录 0700，`nobody` 不能穿越，测试里"0755 就可执行"的假设不成立。测试改用 `_support.public_tempdir()`（选一个整条路径 nobody 可穿越的临时根，没有就带原因 skip），并新增"0700 父目录下判定为不可执行"的用例；已用 0700 父目录的 TMPDIR 复现旧失败、验证修复。`test_models`、`test_sandbox` 中同类公共目录一并改用。
- 分工：端侧运行时 Agent → 新增 `neuxmind_runtime/conformance.py`、`scaffold.py`、`tests/test_conformance.py`、`tests/test_scaffold.py`、`tests/ros2_double/`；改 `adapters/ros2.py`（Ros2Bridge、imu/joint_state/amcl 转换、require 顺序）、`core.py`、`gateway.py`、`cli.py`、`skills.py`、`models.py`、`__init__.py`/`pyproject.toml`（0.4.0）、`tests/{_support,test_models,test_packaging,test_sandbox}.py`、README（§3 新流程、§3.1、§3.2、API、已知限制）、PROGRESS。
- 验证（仿真本体 + rclpy 测试替身）：runtime/tests 245 → 304 个。四环境全量：3.13 root 302 OK（启动时尚未加入最后 2 个用例，之后单跑 test_conformance 46 OK）、3.10 root 303 OK、nxtest 3.13 303 OK（2 skip）、nxtest 3.10 303 OK（2 skip）；最终 test_conformance 在 3.13/3.10 root 与 nxtest 3.10 再跑通过；无 nxtest/nobody 残留进程。sdk/tests 59、spec/tests 通过。两个仿真本体 static/dry/motion 全部用例 passed 或 not_applicable；Ros2Adapter 与脚手架生成的 ROS 2 适配器在替身上 static/dry/motion 全过。worktree 变异（`git worktree`，主工作区未改动，已删除）：conformance 判定逻辑 40 个变异。第 1 遍 35/40——M01/M11 存活是因为测试的故障本体按套件阈值构造（阈值放宽，故障也随之放宽），改为按契约常量 1.0 s 构造并断言报告阈值；M34 存活是变异脚本缺陷（同秒同尺寸写入命中旧 `.pyc`），脚本改为每次删 `__pycache__` 并 `-B`。第 2 遍 38/40；M40（套件自己的输出校验）补"不校验输出的目标"用例后杀死；M06（static 未过时 dry 不得为 ok 的链式规则）被"dry 用例全部 blocked"掩盖，属冗余防线。最终 39/40。
- 变更请求（总指挥）：见交付报告"契约变更请求"（§6.1 报告结构与判定规则、`/v2/conformance` 端点与错误码、dry 授权说明；§6.2 脚手架输入/输出与 ROS 2 模板表；§5.1/§1.2 capability 运行的 progress 语义；CAPABILITIES 缺少夹爪开度与末端位姿读取）。
- 遗留：真机与真实 ROS 2 均未验证；一致性测试在运行中的运行时上不阻止其他客户端同时下发运动；`arm.ee_pose`、大幅姿态变化无自动 motion 用例；夹爪停止只能以动作结束为证据；导航最小目标在无 `localization.pose` 时假定 odom≈map；黑盒模式无法测停止代数；脚手架不生成相机/激光/机械臂/夹爪的 ROS 2 实现；第 03 轮遗留 G-2、DNS 重绑定、Q3-14 未处理。

### 第 04 轮补充 · 总指挥转来的 SDK/云端端到端发现（runtime/）
- 报告新增 `manifest_digest`（与云端 `catalog.manifest_digest` 同一算法：`validate_manifest_v2` 后的 v2 清单 `json.dumps(sort_keys=True, ensure_ascii=False)` UTF-8 的 SHA-256；brainos 的 digest 针对 v1 视图且 ASCII 转义，两者对中文名称的仿真本体结果不同）和 `manifest_v1_digest`（= brainos/`/v1/evaluate` 的 `robot_manifest_digest`）、`time_scale`。
- dry 新增 `model.install.refuses_bad_requests`（未授权 403、非法 sha、元数据地址、file:// 全部被拒且已装模型不变）与 `legged.posture.refused_without_motion`（非标准值 400 + 锁存期间被拒 + 健康不变；规范无姿态读取，不做"重发当前姿态"）。两者在 require_level=dry 下可用。
- time_scale 10/20 下 sim_arm 的夹爪取消/停止失败（固定 0.1 s 等待内动作已完成）：套件读取 time_scale，机械臂速度 ÷ k、导航中断距离 × k、停止前重发速度、夹爪在动作执行中立即中断（再等 0.1/k s）。1×/10×/20× × 两本体 × 10 轮 = 60 次 motion 全过。
- 变异（worktree）：新增判定 10 个，第一遍 5/10（姿态错误码、安装探测、倍速缩放三项无用例或冗余）→ 补用例（错误码视图、伪造安装成功视图、按 k 断言证据），夹爪目标随 k 改变被证明非必要已撤回；最终 9/9。

## 第 04 轮审查修复 · 2026-11-09 · 端侧运行时：一致性测试过程安全与判定（Q4-01..07、Q4-09..11、Q4-13、Q4-15/16/19/21/22）（runtime/）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-review.md`（判定 0.11.0 不可发布）。只改 `runtime/`。每一条先用审查复现脚本（`scratchpad/review04/`，复制到 `scratchpad/runtime04/rev/` 运行，未改动审查目录）复现，修复后复验，并在 `tests/test_conformance_review.py` 加回归。
- 关键决策：
  - **Q4-01 锁存归属只看停止计数**：`SafetySupervisor.stop_count`（只增不减）进 `/v1/status`（`stop_count`、`stop_epoch`）；`reset` 增 `expect_stop_count` 条件复位（不符 409 `stop_count_mismatch`，复位过程中到达的停止由原有竞态保护再锁存）。套件在每个用例前后、每次停止前后、每次复位前核对计数，任何不符 → 中止、保持锁存、绝不再复位；已被别人锁存时不再发停止；测试开始前已锁存 → 全部动态用例 blocked。停止代数探测改为"套件自己停 + 条件复位 + 带旧代数下发"。
  - **Q4-02 dry 不运动**：机械臂当前角只按关节名取（`joint.state` 恰好只含机械臂 dof 个、名称唯一且两次读取相同），否则 blocked、不发送；进程内装 dry 级适配器运动调用守卫（遮蔽适配器实例的 `invoke/start_action`），非零位移一律拒绝并记为套件缺陷；motion 前卸下。黑盒模式没有守卫，依赖运行时拒绝的探测 blocked（顺带 Q4-16，工作区探测改为界外 1 cm）。
  - **Q4-03**：motion 级任一失败 → 停车、锁存、中止；dry 级异常且仍有运行 → 同样处理。
  - **Q4-04**：导航目标只用 `localization.pose`，没有就 blocked；最小目标行驶超过 0.25 m 即停车中止。`SimQuadruped` 增 `localization.pose`（仿真 map = odom，如实标注）；ROS 2 替身发布 `/amcl_pose`。
  - **Q4-05**：只认运行时自带的仿真类（精确类型 + `simulated:true` + 进程内），其动态级别用 `RealClock.set_time_scale(1.0)` 以 1× 执行（机器人时间连续），结束恢复；其他一律 1×，自报倍速只记录；中断用例改为绝对小幅度（导航 0.2 m 向前、关节 0.1 rad），不随倍速放大（Q4-19 一并）。报告 `time_scale`/`executed_time_scale`/`time_scale_rebased`。
  - **Q4-06**：速度用例采样里程计：峰值速度在指令 ±50 %、不反向、机体系前向位移 > 0、侧向小；导航核对位移方向。
  - **Q4-07**：停止/取消后持续静止 2 s、每次复位后 1 s、结束时 2 s（`safety.stop.final_still`），任一违反 → 停车锁存中止。
  - **Q4-09 脚手架**：外部字符串白名单（ID `[A-Za-z0-9_.:-]`、显示文本无引号/反斜杠/反引号/`<>|$`、版本 PEP 440 子集、提示目标/ROS 名称/节点名/坐标系、`stale_after_s` 0.05–10 s（Q4-21）、`x.*` ID）；代码里不嵌入原文（身份只在 adapter.json，提示与能力 ID 用 `repr()`）；全部文件在内存中生成并校验（compile、adapter.py AST 结构白名单、JSON、TOML）后写临时目录再原子改名，失败不留文件。审查的 9 个注入载荷 + 3 个新增载荷全部拒绝。
  - **Q4-10**：每次读数都做合理性检查，health 每次都须 ok；IMU 四元数归一补故障用例（审查 R6）。
  - **Q4-11**：`SimArm` 声明 `gripper.state`；夹爪以 `gripper.state` 核对，没有时中断用例 blocked；`arm.ee_pose` 用 `arm.ee_state` 做最小位移，没有时 blocked。
  - **Q4-13**：报告 `trust {self_reported, same_process_as_adapter, note}`、免责声明与 README 写明信任边界（同进程可篡改、读数与动作同时伪造与识别测试模式不可发现）；临时状态目录改为通用前缀。
  - **Q4-15**：server 模式运动租约，其他来源运动/技能/`/v1/start`/`/v1/evaluate`/复位 409 `conformance_running`，报告 `external_motion_refused`。
  - **Q4-22**：仿真本体、ROS 2 适配器、脚手架清单 `spec_version` 1.1；model.install dry 证据注明"只证明拒绝"。
- 复验（审查脚本，详见 PROGRESS）：operator_stop dry/motion → 中止、锁存、操作员停止后 0 次复位；dry_audit → 多关节排前的机械臂 0 条机械臂指令；midfault（中断目标改为 0.1 rad 后的适配版）→ 中止锁存、腕关节未越过 0.05 rad；frame_offset → 撒谎的定位下 0.26 m 即中止；liars：velocity_x5 / reversed / nav_cancel_resume / arm_cancel_resume / reset_resumes_velocity / coast_lying / gripper_fake_result（SimArm 自带 gripper.state 后的适配版）/ health jitter 全部判失败且中止锁存，fake_time_scale_real_body 目标 ≤ 0.2 m；conc_client → 其他客户端 58 次全部 409，本体位移 0；scaffold_inject → 全部拒绝、无残留、无执行。仍"通过"的 watchdog_lying、test_mode_detect、manifest_lies_limits 属固有限制，写入信任边界。
- 变异（worktree）：新防线 40 个（F01–F40），第一遍 34/40，补字段与用例后 38/40；F02、F09 被停止计数/关节数量检查覆盖，属冗余防线。
- 契约变更请求：见交付报告。
- 遗留：套件与适配器分进程 + 报告签名（Q4-13 根治）；机械臂关节名进能力标准（Q4-02 根治）；云端/桌面/SDK 侧 Q4-08/12/14/17/18/20 不在 runtime。

## 第 04 轮复审修复 · 2026-11-09 · 端侧运行时：关节按名称对应、锁存不等于已停、复位竞态再停车、wz/vy、调用超时（R4-01..08）（runtime/ + spec 最小变更）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-rereview.md`。复现脚本复制到 `scratchpad/fix2/` 运行（`NX_TREE` 指向工作区），未改动复审目录。
- 关键决策：
  - **R4-01（P0）根治**：spec v1.1 给 `arm.joint_position.params` 增可选 `joint_names`（字符串列表，长度 = dof，互不相同；`validate_manifest_v2` 校验），`SimArm` 声明之。套件只按清单声明的名称从 `joint.state` 取当前角，**从不**按 `joint.state` 顺序推断；未声明或缺少声明的关节 → 全部机械臂/joint_position 动作块用例（及只能用机械臂探测的 `stop_epoch`）blocked、不发送。dry 守卫的"当前角"直接读适配器并按运行时已校验清单的 `joint_names` 映射，独立于套件。`_joints_still` 按名称比较。运行时动作块逐步限速在声明 `joint_names` 时按名称取起点。
  - **R4-02（P1）**：中止时无论是否锁存都再停一次；已锁存时用 `halt_motion`（不改锁存原因与停止计数）。中止后观察：要求持续静止 2 s，见到运动就再停（≥0.3 s 间隔、≤20 次、≤8 s），结果写入 `final_motion`/`end_state`/`aborted`，未确认静止时为"警告…请立即使用硬件急停"，CLI 摘要醒目提示；文案不再写"已停车"。
  - **R4-03（P1）**：`SafetySupervisor.reset` 在 `adapter.reset()` 执行期间收到停止时（两处交错检查及 reset 抛异常时），复位返回后再调 `adapter.stop()`、保持锁存，发 `safety.reset_raced`。
  - **R4-04（P2）**：新增 `base.velocity.turn`（wz）与 `lateral`（全向底盘 vy），核对符号/幅度/偏离轴；vx 侧向容差收紧到 20 %。
  - **R4-05（P2）**：套件对目标的每次调用在独立线程限时（5 s，复位 15 s），超时 → 用例失败、停止、中止，`call_timeout` 记录；server 模式租约随套件返回即释放。
  - **R4-06/07（P3）**：脚手架拒绝与标准库/常用包同名的包名；显示文本禁止 `[]`。**R4-08**：新回归文件 `tests/test_conformance_rereview04.py`。
- 遗留：运行时"锁存期间监视反馈并周期性重发停止"守护；`joint_names` 在 v2 改为必填、未声明时运行时动作块限速 fail closed；`POST /v2/conformance/{id}/cancel`。

## 第 04 轮二次复审修复 · 2026-11-09 · 端侧运行时：复位超时不再静默解锁、dry 不发机械臂目标、动作块限速 fail closed（R4b-01..05、R4b-08、R4b-10）（runtime/）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-rereview2.md`（R4b-01..03 为 P1）。复现脚本复制到 `scratchpad/fix3-rr/`（`NX_TREE` 指向 fix3 worktree），未改动复审目录。只改 `runtime/`。
- 关键决策：
  - **R4b-01**：中止时的停止、以及任一调用超时之后的所有再停，改为计数的急停 `emergency_stop(keep_reason=True)`（计数 +1、保留原锁存原因）；纵深防御：`SafetySupervisor` 新增 `_halt_seq`（每次适配器停止都递增），`reset()` 跨 `adapter.reset()` 比较，执行期间有任何停止（含不计数的 `halt_motion`）→ 按竞态处理：保持锁存、复位返回后再停、`safety.reset_raced`。套件记录超时后仍未返回的调用（`final_motion.calls_in_flight`），复位/指令类调用未返回时继续观察；`final_latched` 最后读回。
  - **R4b-02**：dry 不向适配器发送任何机械臂目标或 joint_position 动作块：`zero_displacement` / joint_position `zero_chunk` 记 `not_applicable`（`dry_skipped: true`，未声明 `joint_names` 时仍 blocked），守卫一律拒绝此类调用。dry 开始记下本体位置，每个 dry 用例后比较，超过 1 cm / 0.01 rad / 夹爪 0.02 → 停止、锁存、中止。另加 motion 级机械臂运动包络：指令执行中任一关节超出 [起点, 目标] ± 0.02 rad → 停止中止（声明错误的 motion 位移 1.57 rad → 0.021 rad）。
  - **R4b-03**：未声明 `joint_names` 时运行时拒绝 joint_position 动作块（fail closed），`joint.state` 关节名重复也拒绝。
  - **R4b-04**：中止后观察窗 2 s → 5 s（总上限 8 → 12 s），报告定稿前再核对一次（`final_recheck`），`end_state` 写明“观察窗之后才恢复的运动无法发现”。
  - **R4b-05**：vx / vy 用例检查 wz 峰值与偏航变化。**R4b-08**：`/v2/stop` 接受 `keep_reason: true`（停止先执行，之后恢复原锁存原因），HttpTarget 再停时携带。**R4b-10**：新回归文件 `tests/test_conformance_rereview04b.py`（15 例）；旧用例中“dry 发送零位移机械臂目标”的断言改为“不发送”。
- 遗留：运行时锁存守护（第 05 轮）；观察窗外的延迟恢复；R4b-06（腿足 turn 阈值）、R4b-07（cloud 截断）、R4b-09（线程亲和性）未改；`joint_names` v2 必填。

## 第 05 轮 · 2026-11-09 · 端侧运行时：锁存守护（R4b-04 根治）、复位互斥、keep_reason 比较交换、动作块同步拒绝、脚手架 joint_names 提示（runtime/）

- 依据：`docs/evolution/reviews/2026-11-09-iter04-rereview3.md` 与 `docs/evolution/iterations/2026-11-09-iter04.md`（遗留）。在 git worktree 中开发（规则 7），复审脚本复制到 `scratchpad/rr5/`（`NX_TREE` 指向本 worktree），未改动复审目录。只改 `runtime/`。
- 关键决策：
  - **锁存守护（R4b-04 根治）**：新模块 `latch_guard.py`，线程 `nx-latch-guard`。锁存期间每 0.1 s 读 `base.odometry`（速度 + 采样间位姿变化）与 `joint.state`（速度 + 位置变化），发现运动 → `SafetySupervisor.latched_restop()`（只调 `adapter.stop()`），两次至少间隔 0.5 s（机器人时钟），发 `safety.motion_while_latched {seen, restops, evidence…}`；`/v1/status.latched_motion` 暴露计数。不解锁、不改原因、不计入 `stop_count`、不发任何运动指令（连零速度也不发）、不取停止路径的锁；读卡住只卡自己的线程；`close()` 退出。计数停止后 1 s 刹停期不判运动；操作员复位在途时只观察（腿足起立属正常），在途超过 30 s 照常再停。阈值保守（0.02 m/s、0.05 rad/s、3 mm / 0.005 rad 且超过速度×间隔），`GuardConfig` 可调。选择“只发 stop()”而非零速度：零速度是运动类指令，且 stop() 是契约中唯一保证可并发、不等锁的通道。
  - **套件与守护的配合**（全量第 1 次运行发现）：守护在自己的读取里积分并再停了驱动的恢复运动，套件 30 ms 的采样对看不到，`StopResumeVelocity`（每次停后 1.3 s 恢复）被报告为“静止”。这样会低估缺陷，所以套件中止后的核对与定稿前的复核同时读取守护的 `seen` 计数（`Target.latched_motion()`），守护见到运动即视为不静止，`final_motion.guard_motion_seen` 和 `end_state` 写明。
  - **R4c-01 复位互斥**：监督器 `_reset_lock` 覆盖整个 `reset()`（含被调用方放弃等待的 `adapter.reset()`），第二个复位非阻塞获取失败 → 409 `reset_in_progress`；`core.reset` 在写账本前先查一次（同一 `request_id` 之后重试有效）。`_abort_reset` 先显式置内存锁存再写盘，内存与磁盘永远一致；`safety.reset_raced` 增 `latched` 字段，文案按事实二选一。`/v1/status.reset_in_progress`。
  - **R4c-02 keep_reason 比较交换**：`/v2/stop` 停止结果带 `stop_count`；`keep_reason` 只在“本次停止仍是最新停止且原因仍是本次写入的占位 `"stop"`”时恢复旧原因（`SafetySupervisor.set_reason_if`，写后复核停止序号，被新停止抢先时恢复新停止的原因）；响应 `latch_reason_kept`。普通 `reason` 仍按到达顺序覆盖（操作员原因始终可写）。没有引入客户端字段 `expect_reason`：服务端用停止序号即可判定，客户端无需改动。
  - **R4c-05 动作块同步拒绝**：`CapabilityBus.chunk_static_error()` 只凭清单判定（不调适配器）；joint_position 空间而未声明 `joint.state` / `joint_names` → 提交时同步 400 `joint_state_required` / `joint_names_required`，不发 run id；运行内仍复查（`joint.state` 名称重复/缺失仍在运行中异步失败，因为需要读适配器）。
  - **R4c-03 脚手架**：有 `arm.joint_position` 时生成 `ARM_JOINT_NAMES = None   # TODO(joint_names)` 与注释（取自 `ros2_control` 的 `joints:`，不要照抄 `/joint_states`），`build_manifest` 在非 None 时注入 `params.joint_names`；生成的 README 醒目提示；输入已给出 `joint_names` 时只留核对注释。runtime/README §3 第 3 步与 §3.2 同步说明。
- 新回归：`runtime/tests/test_iter05_runtime.py`（23 例）。
- 复验（`scratchpad/rr5/`，复审脚本副本；`restop5.py` = `restop2.py` 把套件返回后的观察从 5 s 延长到 12 s 并读守护计数）：
  - `restop every 6`：套件返回后 5 s / 12 s 位移 **0.010 / 0.014 m**（复审时 5 s 0.41 m），守护再停 2 次，保持锁存；`every 9`：**0.017 / 0.024 m**（复审时 0.41 m），再停 5 次；`every 2.5`：0.049 / 0.10 m，报告“警告…其中运行时锁存守护发现并再停 4 次”；`twice 6`：0.010 / 0.010 m；`ignore`（stop() 无效）：软件停不住（0.50 / 1.20 m），守护 12 s 内只再停 48 次（≥ 0.5 s 间隔），报告警告。
  - `op_reset_inflight 4 25 0.5`：操作员复位 409 `reset_in_progress`（“…尚未返回（已 14.5 s）”），被放弃的复位返回后内存锁存 **true**、磁盘 **true**（复审时内存 false / 磁盘 true），之后位移 0.009 m（复位重放速度，再停前的一个采样周期）；`4 12 0.5`：旧复位在观察期内返回，操作员复位成功，内存/磁盘均 false。
  - `chunk_refuse none`：同步 `BadRequest joint_names_required`、0 个 run、0 步到达适配器；`dup` 仍为异步失败（需要读适配器）。
- 遗留：守护只对声明了 `base.odometry`/`joint.state` 的本体有效，且依赖反馈真实；每次恢复在被发现前仍会运动约一个采样周期；`gripper.state`、`arm.ee_state` 未纳入；`joint.state` 名称重复/缺失的动作块拒绝仍是异步；R4c-04（阈值按 family 现场标定）、R4c-06（ux01）、R4b-06/07/09 未改。

## 第 06 轮 · 2026-11-09 · 端侧运行时：锁存守护看见慢速蠕动与“蒙住的”反馈、设备端技能参数范围校验、复位卡住的出路（Q5-01/02/03/05/06/07/08）（runtime/）

- 依据：`docs/evolution/reviews/2026-11-09-iter05-review.md`。在 git worktree（分支 `iter06-runtime`）中开发（规则 7）；审查脚本 `scratchpad/r5/` 复制到 `scratchpad/r6/` 运行（未改动审查目录），新增复验脚本 `scratchpad/r6/verify06.py`。只改 `runtime/`。
- 关键决策：
  - **Q5-01 累计位移**：计数停止刹停结束后取锁存快照（底盘 x/y/偏航，关节按名字）；离开快照 > `max_linear_drift` 0.05 m / `max_angular_drift` 0.05 rad 即运动、再停；之后进入“已偏离”状态，相对**上一次再停位置**再动超过采样噪声下限（3 mm / 0.005 rad）就再停。不选“每次再停后重取快照”：那样每次恢复都再给 5 cm，总位移无界（每次停后恢复的驱动 600 s 约 5.6 m）。快照对抖动免疫（±2.5 mm 抖动 600 s 再停 0 次），对蠕动敏感。
  - **Q5-02 反馈无效**：解析函数 `parse_odometry`/`parse_joint_state` 明确什么算“可用”（7 维有限位姿、3 维有限速度；关节名非空、不重复、位置/速度与名字等长，`joint.state.velocity` 按规范可省略）；不可用持续 0.25 s → 按“可能在动”再停（同一 0.5 s 限速）并发 `safety.feedback_invalid_while_latched`；关节名集合与快照不同也算不可用。关节比较全部改为按名字的字典（第 05 轮按顺序比较，名字一变就跳过）。读取卡住由 `nx-monitor` 线程调用 `check_stalled_read()` 发现（> 1 s），同样限速再停——守护线程卡在读取里时自己做不到。
  - **Q5-05 参数声明**：`check_parameters` 读取 `min`/`max`/`choices`/`options`/`length`/`min_length`/`max_length`。错误正文按总指挥转来的桌面约定（master `637d63d`）：`{code, param, min, max, choices, message, …}`，`code` 为 `param_out_of_range`、`param_choice`、`param_length`、`param_required`、`param_type`、`param_declaration_invalid`；未知参数保留 `bad_request`（SDK CLI 测试依赖），`problem: param_unknown`。类型错误保留原文前缀“技能参数 X 需要 T”，新文案避免“需要”二字以免被桌面文本规则当成类型错误。`submit_application` 在准入前检查每一步（覆盖旧版本部署的 release 与 `/v1/start`），安装时校验声明本身（含 `default`）。
  - **Q5-03**：`/v1/status.reset_in_progress_s`、`reset_hint`；409 `reset_in_progress` 正文加 `message`（可直接显示的中文）、`hint`、`reset_in_progress_s`，`code` 不变。超过 10 s 提示改为“重启运行时服务（锁存会保留）后再解除急停”。README §2 写操作员步骤。
  - **Q5-06**：适配器可声明 `stop_settle_s`（HAL 文档化，`RobotAdapter.stop_settle_s = None`），守护取 `[1, 5] s` 内的值；脚手架未加该属性（生成代码校验器不允许类体赋值，改为 README §3 说明）。
  - **Q5-07**：`latched_restop()` 在复位在途时记标志；`_abort_reset` 据此给出“锁存守护已再次停止”的文案与 `cause: latch_guard_reset_grace`，真有人停止时 `cause: stop_during_reset`、原文不变；`safety.reset_raced.by_latch_guard`。复位尝试结束仍锁存时守护重新刹停、重取快照。
  - **Q5-08**：`close()` 对自身线程的等待改为总计 1 s（原来每线程 1 s），留下的线程名写入 `lingering_threads`；守护线程是 daemon，读取返回后检查关闭标志、不再调用适配器即退出（测试断言 0 次）。
  - **套件与守护的配合（全量第 1 次运行发现）**：`test_conformance_review…test_delayed_resume_after_stop` 偶发失败——导航停后 1.3 s 恢复，守护先于套件 `_hold_still` 的采样再停，套件判“静止”。在 v0.12.0（master）上同一用例 4 次也失败 1 次，属既有竞态；修法与第 05 轮的中止后核对一致：`_hold_still` 每 0.25 s 读守护 `seen`，增加即视为运动并中止（文案注明“由运行时锁存守护发现并再停”）。修后单跑 6/6 通过。
  - **Q5-11**：运行时包版本 `0.4.0` 独立于产品版本，`runtime/` 中没有 0.11.0/0.12.0 字样；产品版本字符串在 `brainos/`（桌面负责）。
- 新回归：`runtime/tests/test_iter06_runtime.py`（24 例）。
- 全量（规则中的五个目录，顺序执行，`scratchpad/r6/run3_*`）：spec 12 OK / runtime **419 OK**（1417 s）/ cloud 211 OK / sdk 84 OK / tests 213 OK（skipped 39）。第 1 次全量 runtime 1 例失败（见上“套件与守护的配合”），修后第 3 次全绿（第 2 次因对齐桌面参数错误体中途停止）。
- 复验（`scratchpad/r6/attack_a.py`、`attack_sit.py`、`verify06.py`，ManualClock 每 0.1 s 一次）：
  - Q5-01 蠕动（停后 2 s 恢复）：0.015 m/s 首次再停 5.4 s、位移 0.051 m，600 s 总位移 **1.08 m**（第 05 轮 8.97 m、`seen=0`），再停 262 次；0.019 m/s 4.7 s / 0.051 m，600 s **1.00 m**（第 05 轮 11.36 m）；0.03 m/s 不变（0.73 m）。机械臂关节 0.04 rad/s：3.3 s、0.052 rad 首次再停。静止四足 600 s 再停 0 次。始终无视 `stop()` 的驱动：软件停不住（600 s 9.0 m），守护每 0.5 s 再停（1124 次）并持续告警。
  - Q5-02（停后 0.3 m/s，30 s）：NaN / 3 维位姿 / `velocity:null` 位移 **0.0 m**（第 05 轮 8.4 m），各再停 54 次、事件同数；冻结反馈仍 8.4 m（固有限制，README 写明）；关节名循环移位 + 不给速度：`seen` 289（第 05 轮 0），漂移由读取内部产生、停不住，但持续再停告警。读取卡住：3.5 s 内再停 5 次、事件 5 条，`close()` 1.0 s，遗留 `nx-latch-guard` 读取返回后退出、0 次适配器调用。
  - Q5-05：`loops: 99` / `0`（声明 1–3）直接运行与 HTTP 均 400 `param_out_of_range`（`param: loops, min: 1, max: 3, choices: null`，`message` 中文），0 次适配器调用；`/v1/deploy`、`/v1/evaluate` 409 `validation_failed`（检查项写明超出范围）；旧 release 的 `/v1/start` 400（`step: 0`），不准入。
  - Q5-06：声明 `stop_settle_s = 坐下时长 + 0.5 s`，2 s / 3 s 坐下（是否重规划）再停 **0** 次（第 05 轮 2–8 次）；声明 60 s 被限为 5 s。未声明时 3 s 重规划坐下比第 05 轮更差（再停 20 次、15 s 内坐不完）——累计位移让守护对慢速收尾更敏感，见遗留。
  - Q5-07：起立 3 s、`reset_grace_s=1`：409 `estop_latched`，`cause: latch_guard_reset_grace`，文案“…锁存守护已再次停止；保持锁存…”。
  - Q5-03（HTTP）：0.5 s 时 409 正文含 `message`/`hint`（“请稍候”）/`reset_in_progress_s: 0.5`，`/v1/status` 同；超过阈值后 hint 为重启出路；重启后锁存、复位成功。
- 遗留：未声明 `stop_settle_s` 的受控坐下（尤其每次 stop 都重规划的驱动）会被反复再停甚至坐不完——需各 family 适配器声明（与 R4c-04 同根）；冻结反馈无法发现；驱动无视 `stop()` 时软件停不住；`gripper.state`/`arm.ee_state` 未纳入守护；卡住的反馈读取无法中断（只能发现并再停）；示例技能 `patrol.loops` 等未声明 `min`/`max`（未改示例以免改变包摘要，建议示例负责人补上）；Q5-09（`_stop_record` 写序）未改。

## 第 07 轮 · 2026-11-09 · 端侧运行时：锁存守护抗真实噪声/漂移并可现场标定、事件合并与“驱动不服从停止”升级、参数约束对所有类型 fail closed、仿真运行时干净退出（Q6-01/02/04/05/06/08/09）（runtime/ + spec/ 最小改动）

- 依据：`docs/evolution/reviews/2026-11-09-iter06-review.md`。在 git worktree（分支 `iter07-runtime`，基于 `v0.13.0`）中开发（规则 7）；审查脚本 `scratchpad/r7/` 复制到 `scratchpad/r7rt/` 运行（未改审查目录），新增 `r7rt/noise07.py`（噪声/漂移组合）、`r7rt/creep_grid.py`、`r7rt/q609/`。
- 关键决策：
  - **Q6-01 判据改用稳健统计**（`latch_guard.py`）：速度＝单样本 >3×阈值立即算，否则 1 s 中位数；两样本跳变下限＝max(3 mm, 8×噪声)，噪声取声明值与反馈上实测值（二阶差分 MAD，匀速蠕动不抬高；上限 1 cm/0.01 rad；≥20 个样本才启用）；位移比较用 3 点中位数；参考每 60 s 无运动证据重取（IMU 偏航慢漂、里程计随机游走不算运动），离开锁存快照 4×限值（位置、关节）始终算运动；偏航另有漂移速率容许 `yaw_drift_rad_s`（默认 ≈1°/min）。“已偏离”状态的再动下限＝max(3 mm, 5×噪声)，噪声不可忽略时需连续 2 样本；60 s 无运动证据退出。选 3 点而非 5 点中位数、并在噪声可忽略时不要求连续样本：5 点 + 2 样本使 0.019 m/s 停后恢复的 600 s 总位移从 1.00 m 升到 1.86 m（`r7rt/creep_grid.py`）。
  - **可标定**：spec `manifest.normalize_latch_guard` + `LATCH_GUARD_FIELDS`（`pos_m, yaw_rad, joint_rad, noise_m, noise_rad, yaw_drift_rad_s, stop_settle_s`（别名 `settle_s`）, `escalate_after`），`validate_manifest_v2` 校验 `safety.latch_guard`：非数值/NaN/未知键拒绝，越界限幅（不拒绝：标定笔误不应让机器人起不来），阈值不低于 6×声明噪声。运行时覆盖 `EdgeRuntime(latch_guard_overrides=)` / `serve --latch-guard k=v`（同一函数校验，非法退出码 2），优先级 运行时 > 清单 > 适配器属性 > 默认；`latched_motion.config/config_source/config_notes/noise_used`。ROS 2 配置 `latch_guard` 进入清单（Q6-05）。
  - **事件合并**：同类事件首个立即发，之后每 10 s（机器人时钟）最多一条，带 `count`；汇总事件 `summary: true`；周期结束补发。
  - **Q6-04 升级**：同一锁存期间运动再停 ≥ `escalate_after`（10）且跨度 ≥ 30 s → 一次 `safety.stop_not_obeyed {ts, message, note, restops, displacement_m, …}`（“驱动不服从停止…硬件急停/断电”），`latched_motion.stop_not_obeyed`（bool）+ `stop_not_obeyed_at` + `stop_not_obeyed_detail`——按总指挥转来的桌面约定（master `b220885`）。30 s 跨度是为了不把会重规划的受控坐下（约 15 s 内 19 次再停）当成不服从。README 改正：有界的是“每两次再停之间”的位移，不是总位移。
  - **Q6-02/06/08 参数**：`minimum`/`maximum` 作为 `min`/`max` 别名在设备端执行（两者不一致 → `param_declaration_invalid`）；`min`/`max` 在 `object`（每个数值）、`pose`、`vector`/`floats`（逐项）上执行，写在 `str`/`bool`/`enum`/`blob_ref` 上安装即拒；`choices`/`options` 按类型严格比较；`choices`/`default` 含 NaN 拒绝；值遍历改为迭代，嵌套 >64 层 400 `param_type`；网关 JSON 递归溢出 400 `bad_request`“请求 JSON 嵌套过深”（原 500；不新增错误码，桌面 ux04 分类表要求每个码都有白话说明）。
  - **Q6-09**：根因在 `brainos/sim_child.py` 的父进程监视线程用 `sys.stdin.buffer.read` 阻塞，持有 BufferedReader 锁，解释器退出时 `_enter_buffered_busy` abort（-6）。改为 `os.read(fd)`（3 行，桌面目录，单独提交，桌面负责人可取舍）。runtime 侧无法单独修。
  - **顺带修复**（全量第 1 次运行发现）：`runtime/neuxmind_runtime/_paths.py` 优先导入已安装的 `neuxmind_spec`（本机 editable 安装指向 master），worktree 中子进程（CLI/一致性测试）因此加载旧 spec；改为 `NEUXMIND_SPEC_PATH` / 同一检出的 `spec/` 优先，已安装的作后备。
- 新回归：`runtime/tests/test_iter07_runtime.py`（20 例）、`spec/tests/test_spec.py` +3 例；`test_iter05/06` 中“每次再停一条事件”的断言按合并语义改为 `Σcount + 未报告 = restops`。
- 复验（ManualClock 每 0.1 s，本体真实静止、只有上报反馈带噪声/漂移，1 h；`r7rt/noise07.py`，第 06 轮数字取自审查表）：

  | 上报反馈 | 本轮再停/小时 | 第 06 轮 |
  |---|---|---|
  | 位姿 σ 1 mm、偏航 0.5 mrad、速度 5 mm/s | **0** | 2270 |
  | 位姿 σ 2 mm、偏航 1 mrad、速度 8 mm/s | **0** | 5494 |
  | IMU 偏航漂移 0.5°/min / 0.1°/min | **0 / 0** | 95 / 11 |
  | 里程计随机游走 0.5 mm/√s | **0** | 51 |
  | σ 1 mm + 0.5°/min 漂移；σ 2 mm + 0.5°/min 漂移；再加随机游走 | **0 / 0 / 0** | — |
  | 机械臂编码器 σ 1 mrad/5 mrad/s；σ 2 mrad/10 mrad/s | **0 / 0** | 68 / 4742 |

  蠕动（`r7rt/verify06.py creep`）：0.015 / 0.019 m/s 停后 2 s 恢复——首次再停 5.5 / 4.8 s、位移 0.053 m，600 s 总位移 1.08 / 1.00 m、再停 262 / 271 次（与第 06 轮相同）；完全不服从 4.8 / 4.1 s、0.07 / 0.08 m 首次再停（总位移 9.0 / 11.4 m，固有上限）；机械臂 0.04 rad/s 3.4 s、0.056 rad；静止仿真 600 s 0 次；叠加 2 mm 位姿噪声的 0.015 m/s 蠕动 7 s 内、0.07 m 内首次再停。升级：0.019 m/s 恢复型驱动约 35 s 时发一次 `safety.stop_not_obeyed`；完全不服从的驱动 1 h 再停 6000+ 次、事件 < 400 条（第 06 轮每次一条）。受控坐下（未声明 `stop_settle_s`，`r7rt/sit60.py`）：2/3 s、重规划 11 / 19 次再停（第 06 轮 13 / 20），事件 2–3 条，不升级；声明后 0 次。
  参数（`r7rt/params07.py`）：`waypoints` 声明 ±5 时 `[[100,0],[0,-100]]` → 400 `param_out_of_range`；`str` 上的 min/max 安装 400 `param_declaration_invalid`；`pose` ±1 时 `[50,…]` → 400；`minimum/maximum: 0.1/0.5` 时 `9.0` → 400（`min/max` 字段 0.1/0.5）；enum `[1,2]` 的 `true`/`1.0` 与 object `choices [0,1]` 的 `false` → 400 `param_choice`；5000 层嵌套 → 400 `param_type`（原 500）；20 万元素合法数组 0.3 s。
  Q6-09（`r7rt/q609/run2.py`，桌面 `SimProcess` 启动→查状态→停止，10 次）：退出码 0、无 `Fatal Python error`（修前 5/5 次 -6）；关闭 stdin（桌面退出）仍 3/3 次干净退出；`r7/restart.py` 复跑 `old_pid_rc: 0`（修前 -6），锁存保留。
- 全量（五个目录，顺序执行，`scratchpad/r7rt/full/`）：spec **15 OK** / runtime **439 OK**（1421 s）/ cloud **211 OK** / sdk **84 OK** / tests **226 OK**（skipped 39，结束时 1 条 sqlite `ResourceWarning`，与第 06 轮相同）。第 1 次全量因 `_paths.py` 让子进程加载 master 的旧 spec 而中止（已修）；第 2 次 tests/ 1 例失败：新错误码 `json_too_deep` 在桌面 ux04 分类表里没有白话说明——改用已有的 `bad_request`；第 3 次（含总指挥转来的 `stop_not_obeyed` 字段约定）全绿。
- 契约变更请求（INTERFACES_V1 §8，总指挥负责）：① “之后每再动 3 mm / 0.005 rad 再停一次（总位移有界）”改为“每两次再停之间的位移有界；驱动停后又恢复时总位移随时间增长，达到升级条件发 `safety.stop_not_obeyed`”；② 记入清单 `safety.latch_guard`（键与范围见 README §2）、`serve --latch-guard`；③ 守护事件新增 `count`/`summary`、每类 ≤1 条/10 s；④ `latched_motion` 新字段（`config, config_source, config_notes, noise_used, rebases, motion_restops, events_suppressed, event_interval_s, stop_not_obeyed(bool), stop_not_obeyed_at, stop_not_obeyed_detail`）与 `safety.stop_not_obeyed` 事件；⑤ 参数：`minimum`/`maximum` 别名、`object`/`pose` 逐数值范围、非数值类型上的 min/max 安装拒绝、choices 按类型比较、嵌套上限 64 层。
- 遗留：守护依赖反馈真实（冻结反馈仍无法发现）；驱动不服从时软件停不住，只能升级告警；比单个 60 s 窗口还慢的蠕动要到 4×限值（默认 0.2 m）才被抓到，偏航慢于 `yaw_rad/60 s + yaw_drift_rad_s` 的转动不报（对 IMU 漂移的有意取舍）；噪声大的本体在“已偏离”状态下每次再停之间允许的位移随噪声变大（2 mm 噪声时 120 s 0.55 m，无噪声 0.3 m 量级）；速度噪声没有实测估计（阈值固定 0.02 m/s / 0.05 rad/s，1 s 中位数）；`r7/` 中 Q6-03（迟到复位自行解锁）、Q6-07（桌面 options 显示）、Q6-08 的桌面部分、Q6-10（版本号）不在本轮 runtime 范围；脚手架生成的 `adapter.json` 尚未带 `safety.latch_guard` 模板；一致性报告未提示“停止是否为受控动作”。桌面需对齐：`minimum/maximum` 现在设备端也执行（表单无需改）；`stop_not_obeyed` 字段按总指挥约定。

## 第 07 轮发布前修复 · 2026-11-09 · 端侧运行时：锁存守护的两个退化（偏航慢转、停在别处被周期性再停/误升级）与 P3（runtime/ + spec 最小变更）

- 背景：总指挥决定不带着相对 0.13.0 的已知退化发布 0.14.0。按 `docs/evolution/reviews/2026-11-09-iter07-review.md` 修 Q7-01、Q7-02（P2 退化）与 Q7-05/06/07/08/10（P3）。分支 `iter07-fix-runtime`（基于 `0df6555`，未合并 master）。复验脚本为审查脚本的副本：`scratchpad/r9fix/`（`adv.py` 加了 0.3/0.4/0.5 mrad/s 偏航、50/20 mm/s 升级、推开+游走、8 个游走种子；`noise07.py` 加 `NX_OVR` 标定开关），未改审查目录 `r8/`。
- 改动（`latch_guard.py`）：
  - **Q7-01 偏航硬上界，从不重置**：`|偏航−快照| > min(max(yaw_total_rad, yaw_rad), yaw_rad + yaw_drift_rad_s×锁存时长)` 算运动（`base_drift_yaw_total_rad`）；60 s 重置的参考规则不再叠加漂移容许（重置已吸收 `yaw_rad/60 s`，只加超出部分）。新标定键 `yaw_total_rad`（默认 0.2，0.05–1.0）。
  - **Q7-02 棘轮 + 游走容许 + 升级看“再停后仍在动”**：位置/关节 4× 总界与偏航硬界都相对快照、从不重置；某通道在位移 R 处再停后下一次硬界为 `R + 限值`（偏航再加此后的漂移容许）。“已偏离”状态的门槛为 `max(3 mm, hypot(5×噪声, 4×游走×√t))`，游走用多滞后方差（`var_L − var_1`）在反馈上实测，上限 1.5 mm/√s；窗口有净趋势、或 20 s 内有直接运动证据（速度/跳变/离开参考）的再停时不采信（停停走走的驱动不是游走）。升级改为“连续不服从”计数：距上次计入 ≤ 180 s 且其间确有运动（直接证据或滤波位移超出噪声/游走/漂移）才 +1；≥ `escalate_after` 且跨度 ≥ 30 s 升级。
  - **Q7-06**：连续 ≥ 3 次且离快照 ≥ 0.5 m 立即升级（`trigger: displacement`），正文新增 `streak_restops`、`streak_s`、`trigger`。
  - **Q7-05**：比默认宽松的任何键 → 启动时 `safety.latch_guard_relaxed {ts, message, keys:[{key, value, default, source}], notes}`（桌面 `5ccbd26` 约定）、`serve` 警告行、`latched_motion.relaxed`/`relaxed_keys`；`escalate_after` 上限 30（spec 限幅，`GuardConfig` 同样限幅）。
  - **Q7-08**：`flush()`，`EdgeRuntime.close()` 在关闭事件总线前补发挂起的合并计数。
  - `latched_motion` 新增 `relaxed, relaxed_keys, hard_limits, walk_used, escalation_streak`。
- 改动（其他）：
  - **Q7-10**（`safety.py`/`core.py`）：`adapter.reset()` 超过 `reset_expire_s`（30 s）或调用方的 `expire_s`（复位请求新字段，取 [1, 30]）才返回 → 不解锁：保持锁存（内存+磁盘）、再停一次、`safety.reset_expired {took_s, expire_s, adapter_stopped, reason, latched, note}`、409 `estop_latched` `cause: reset_expired`。期间有停止仍按原竞态规则（`stop_during_reset`）。
  - **Q7-07**（`skills.py`）：声明了 `min`/`max` 的 `object` 中出现非数值标量（数字字符串、bool、null）→ 400 `param_type`；`pose` 的范围只约束 x/y/z。
  - spec（最小变更，需总指挥知悉）：`LATCH_GUARD_FIELDS` 新增 `yaw_total_rad`，`escalate_after` 上限 100 → 30。
- 复验（`ManualClock` 0.1 s，真实位移取仿真内部状态；“再停/首次再停时真实位移/升级时刻”；0.13.0 与修复前（0.14-pre，`0df6555`）用同一脚本）：

  | 场景 | 0.13.0 | 0.14-pre | 本次 |
  |---|---|---|---|
  | 偏航 1.1 mrad/s 1 h | 773 / 0.051 rad / — | **0 / 不报** | 565 / 0.052 rad / 108 s |
  | 偏航 0.8 mrad/s | 562 / 0.051 / — | **0 / 不报** | 350 / 0.081 / 191 s |
  | 偏航 0.5 / 0.4 / 0.3 mrad/s | 0.051 / 0.051 / 0.050 rad | 不报 / 不报 / 不报 | 0.126 / 0.201 / 0.200 rad（0.3：1 h 只 1 次，≤ 漂移容许） |
  | 标定上限（MAXCAL）偏航 8 mrad/s | — | **不报** | 0.211 rad、514 s 升级 |
  | 受控坐下 3 s 重规划 / 3 s / 2 s 不重规划（未声明 stop_settle_s，1 h） | 20 / 5 / 3、无升级 | 78 / 64 / 62、75–424 s 误升级 | **19 / 5 / 3、无升级** |
  | 被推开 0.3 m 后静止 1 h | 7 | 66、204 s 误升级 | **7、无升级** |
  | 推开 0.3 m + 里程计游走 1 mm/√s 1 h（2 种子） | 701 / 702 | 650 / 621、53 / 60 s 误升级 | **9 / 12、无升级** |
  | 游走 1 mm/√s 静置 8 h（种子 1–8） | 5509/5349/5656（1–3） | 0/2901/0（种子 2 升级） | **0/2/0/1/1/1/2/4、0 次升级**；0.5 mm/√s 2 种子 0/0 |
  | 0.05 m/s 不服从：升级时刻 | 无升级机制 | 31.4 s（已走 1.57 m） | **11.7 s（≈0.58 m）**；0.02 m/s 34.0 → 26.5 s |
  | 停后 2 s 恢复 0.019 / 0.010 m/s（1 h 总位移、升级） | 5.67 / 5.74 m | 5.67 / 5.74 m、36 / 38 s | 5.67 / 5.74 m、36 / 38 s（不变） |
  | 蠕动 0.5 / 0.8 mm/s 首次再停 | 0.051 m | 0.200 m | 0.200 m（未改，见遗留） |
  | 噪声注水 ±1 cm + 0.8 mm/s（说谎） | 6271 次 | 58、681 s 升级 | 48、652 s 升级 |

  噪声/漂移（`noise07.py`，本体静止 1 h，10 组）：位姿/编码器噪声、0.1°/min 漂移、0.5 mm/√s 游走全部 **0**；**0.5°/min IMU 漂移的 4 组各 1 次**（约 23 min 漂到 0.2 rad 时，之后棘轮不再重复、0 升级；0.13.0 95–5925 次，0.14-pre 0 次）——这是有意取舍：同速的真实慢转与 IMU 漂移无法区分，默认取“宁可多停一次不沉默”；声明 `yaw_total_rad: 0.6` 后 10 组全为 **0**。`verify06 creep`（0.015/0.019 m/s 停后 2 s 恢复）1.08 / 1.00 m、262 / 271 次，与 0.14-pre 相同；`sit60` 前 60 s 3/11/5/19 次（同 0.14-pre）。
- 全量（run2，改名前）：spec 15 OK / runtime 453 OK（1449 s）/ cloud 211 OK / sdk 84 OK / tests 246 OK（skipped 39，结束时 1 条 sqlite `ResourceWarning`，与以往相同）；按桌面约定改 `relaxed_keys`/事件字段名后 run3 复跑：spec 15 / runtime 453（1443 s）/ cloud 211 / sdk 84 / tests 246（skipped 39）全部 OK。
- 新回归：`runtime/tests/test_iter07_fix_runtime.py`（14 例：慢转、硬界、IMU 漂移声明、坐下安静、推开+游走、持续蠕动仍升级、快速升级、放宽事件与上限、关闭补发、复位过期 ×2、参数 ×2）；`test_iter07_runtime` 中“object 内字符串/bool 通过”的断言改为 400 `param_type`。
- 契约变更请求（INTERFACES_V1 §9，总指挥负责）：① 清单 `safety.latch_guard` 新键 `yaw_total_rad`（0.05–1.0，默认 0.2），`escalate_after` 范围 3–30；② 新事件 `safety.latch_guard_relaxed`、`safety.reset_expired`；`latched_motion.relaxed/relaxed_keys/hard_limits/walk_used/escalation_streak`；`safety.stop_not_obeyed` 新增 `streak_restops/streak_s/trigger`，升级条件改为“连续不服从（再停后仍在动）≥ N 次且跨度 ≥ 30 s，或离快照 ≥ 0.5 m 且 ≥ 3 次”；③ `/v1|v2/reset` 新字段 `expire_s`、新 `cause: reset_expired`——**建议桌面带 `"expire_s": 8`**（与其 8 s 记“未知”一致），并把 ux04 的“一旦完成急停可能自动解除”改为“超过 N s 才完成的复位作废、急停保持”；④ `object` 带范围时只接受数值；`pose` 范围只约束位置。
- 遗留：① 位置/关节首次总界仍为 4×限值（0.2 m / 0.2 rad；Q7-05 前半未改），慢于 0.83 mm/s 的蠕动 0.2 m 才首次再停；② 慢于 `yaw_drift_rad_s`（1°/min）的真实转动在首次到 `yaw_total_rad` 再停后不再重复（与漂移同速不可区分）；③ 锁存中搬动本体 > 0.5 m 或持续 > 30 s 会升级（无法与驱动行走区分）；④ 游走容许可被说谎的驱动利用到上限 1.5 mm/√s（60 s 约 4.6 cm，且须无净趋势）；⑤ Q7-04（整数值浮点选项被 `_same` 严格类型拒绝）不在本次任务内，仍需修（建议 `float`/数值 `enum` 按“非 bool 数值相等”比较）；⑥ 冻结反馈仍无法发现。

## 第 07 轮第二次修复 · 2026-11-09 · 端侧运行时：锁存守护在复审的每个场景都不比 0.13.0 更不安全（Q7b-01/02/03/09）、浮点参数 1 == 1.0（Q7b-04）、复位过期能力与 `reset_expected_s`（Q7b-05/06）（runtime/ + spec 一处默认值）

- 背景：`docs/evolution/reviews/2026-11-09-iter07-rereview.md` 判 0.14.0 不可发布（Q7b-01、Q7b-02 两个 P1）。总指挥的发布规则：新运行时在**每一个**场景里至少与 v0.13.0 一样安全，同时保留误报修复。分支 `iter07-fix2-runtime`（基于 `8c891d3`，未合并 master）。A/B：`git archive v0.13.0` 与 `aab8a26` 解在 `scratchpad/fix2rt/ab/`，审查脚本（`r10/` 的 `adv.py`、`adv2.py` 等，`r8/`、`r9fix/` 复制，未改）对三份同跑，真实位移取仿真内部状态。
- 改动（`latch_guard.py`）：
  - **Q7b-01 偏航**：棘轮后的偏航界封顶为 `R + min(max(yaw_total_rad, yaw_rad), yaw_rad + yaw_drift_rad_s×Δt)`（复审公式）；`yaw_total_rad` 默认 0.2 → **0.05**（spec `LATCH_GUARD_FIELDS`，唯一的 spec 改动）：慢转与 0.13.0 一样在 0.05 rad 首次被抓，之后每 0.05 rad 一次。IMU 漂移 0.5°/min 默认每 ≈345 s 再停一次（1 h 10 次，不升级），声明 `yaw_total_rad: 1.0` → 1 h 0 次。
  - **Q7b-02**：“已偏离”在本次锁存内不再结束（无运动满 60 s 只把再停参考挪到当前位置）；噪声可忽略时比原始采样（无滤波延迟）；快照在刹停宽限结束后的第一个采样就取（不再晚 2 个采样）；停停走走不再被测成随机游走（随机游走在每一步里都看得见：`rate²·dt ≤ 2×var1`）。
  - **Q7b-09（顺带，发布规则要求）**：位置、关节从快照起算、不再按 60 s 重置（腿式里程计游走由实测游走容许吸收，游走取 300 s 峰值）；只有偏航参考仍按 60 s 重置。慢蠕动/慢关节在 0.05 m / 0.05 rad 被抓（修复前 0.2）。
  - **Q7b-03**：游走容许只在解释得了所见时采信（离锚点 > 5×游走×√t 不采信；再停持续发生时不得上涨）；锁存以来底盘离快照 ≥ 0.5 m 且已有 ≥ 3 次运动再停即升级（`trigger: displacement`），不论间隔。
  - **预防性停止（新增）**：0.13.0 会再停、而本版因游走/漂移容许不算运动的地方，静默再调一次 `adapter.stop()`（HAL 要求幂等）：无事件、不计再停、不升级、≤1 次/s，`latched_motion.precautionary_stops` 计数。使“服从一会儿再借伪造游走恢复”的驱动像在 0.13.0 下一样被按住。
  - 噪声/速度：实测噪声上限 1 cm / 10 mrad → 3 mm / 2 mrad（更吵须声明 `noise_m`/`noise_rad`）；噪声未测出前跳变按上限噪声判断；上报速度安静时单采样超过阈值即算（0.13.0 同），有噪声时仍 3 倍/中位数。
  - `latched_motion` 新增 `precautionary_stops`、`walk_measured`、`hard_limits.{pos,yaw,joint}_step_*`；`walk_used` 为实际采用值。
- 改动（其他）：
  - **Q7b-04**（`skills.py`）：声明为实数（`float`，`vector`/`floats`/`pose` 元素）的 `choices`/`options` 按数值比较，双向 `1 == 1.0`；整数以 `1.0` 交给技能；布尔/字符串与数值仍不等；`int`/`enum` 不变。（总指挥补充：桌面 `1bc90f2` 对 float 参数一律发浮点，手工登记 `[1, 2]` 也能匹配——已加测。）
  - **Q7b-05/06**（`core.py`）：`/v1/status.capabilities = {reset_expire: true, reset_expire_max_s: 30, reset_expire_min_s: 1, reset_expected_s, reset_expected_note}`，顶层 `/v1/status.reset_expected_s`；清单 `safety.reset_expected_s`（仅提示；正有限数，>30 按 30，非法忽略并说明）。命名与桌面 `1bc90f2` 的读取顺序一致（首选 `/v1/status.reset_expected_s`），无偏差；未提供 `/v1/status.safety.reset_expected_s`（桌面的第二候选，不需要）。
- 复验（1 h，真实位移；v0.13.0 / aab8a26 / 本次）：偏航 0.29 mrad/s 首停 0.050 / 0.200 / 0.050 rad、再停 199 / 1 / 20；0.29 mrad/s × 8 h 1655 / 1 / 166；`resume_0.019_65s` 0.237 / 2.70 / 0.235 m；停后 185 s 以 1.9 cm/s 恢复 0.113 / 0.966 / 0.113 m；以 0.8 mm/s 恢复 0.102 / 0.808 / 0.102 m；停后 61 s 0.247 / 2.85 / 0.245 m；伪造游走 + 停后 61 s 0.049 / 1.357 / 0.049 m；蠕动 0.5 / 0.8 mm/s 首停 0.051 / 0.200 / 0.051 m；机械臂 0.8 mrad/s 0.051 / 0.201 / 0.051 rad；±1 cm 注水说谎 1.2 s / 47.5 s / 1.2 s；往返 ±0.19 m 再停 688 / 0 / 688。误报：噪声/游走/坐下/推开/8 h 游走与 aab8a26 相同（0–4 次、0 升级；坐下 20/5/3 = 0.13.0），IMU 0.5°/min 10 次/h（aab8a26 1，0.13.0 95–5925），标定 `yaw_total_rad: 1.0` 后 0。全表：`scratchpad/fix2rt/ab/out/cmp2_cand6.md`。仍比 0.13.0 差的只剩“告警频度”类：说谎驱动伪造随机游走、或真实慢转与 IMU 漂移同速时，**真实位移与 0.13.0 相同**，但两次运动事件之间更久（0.13.0 每次都报，是因为它把游走/漂移本身也当运动——即那批误报）。
- 新回归：`runtime/tests/test_iter07_fix2_runtime.py`（Q7b-01 ×3、Q7b-02 ×2、Q7b-03 ×3、Q7b-04 ×3、Q7b-05/06 ×2；除标注 “(kept)” 的保底例外，在 `aab8a26` 上全部失败）。改了两条旧断言以反映新默认值：`test_iter07_runtime.NoisyStillBody`（0.5°/min 漂移 15 min 内 ≤3 次、标定后 0 次）、`test_iter07_fix_runtime.SlowYaw.test_imu_drift_declared_bound`（1 h ≤ 11 次）。
- 契约变更请求（INTERFACES_V1 §9/§9.1，总指挥负责）：① `yaw_total_rad` 默认 0.05、含义扩展为“每次偏航再停后的步长上限”；② “已偏离”贯穿锁存期；位置/关节从快照起算；③ 预防性停止与 `precautionary_stops`；④ 升级新增“离快照 ≥0.5 m 且 ≥3 次运动再停，不论间隔”；⑤ 实测噪声上限 3 mm / 2 mrad；⑥ `/v1/status.capabilities`、`reset_expected_s`、清单 `safety.reset_expected_s`（建议 spec 在清单校验中加入此键）；⑦ 实数参数的数值相等。
- 追加（总指挥对 §5 的决定）：① 接受告警频度差异；② 新增 `safety.latched_drift_notice`（每次锁存一条、不升级，位移首次超过首次发现界时发，含被归为游走/漂移的情形）与 `latched_motion.drift_notice/_at/_detail`；③ `safety.reset_expected_s` 进入 spec 清单校验（`normalize_reset_expected`：(0, 30] 有限数值，否则清单无效）——spec 第二处改动。回归：`LatchedDriftNotice` ×4、`spec/tests` `ResetExpected` ×2、`ResetCapabilities` 改为非法值拒绝启动。守护的再停/预防性停止逻辑未变（提示只发事件），A/B 结果不受影响。

## 第 07 轮第三次修复 · 2026-11-09 · 端侧运行时：预防性停止逐条复刻 0.13.0（Q7c-01）、漂移提示属于一次锁存（Q7c-02）、`reset_expected_s` ≤ 25 s（Q7c-07）（runtime/ + spec 一处）

- 背景：`docs/evolution/reviews/2026-11-09-iter07-final-review.md` 判 0.14.0 不可发布：游走被采信时，“停后服从一段时间再走”的驱动 1 h 真实位移比 v0.13.0 多 8%–63%（Q7c-01，P1）。分支 `iter07-fix3-runtime`（基于 `d498d79`，未合并 master）。版本号未动（总指挥负责）。
- 改动（`latch_guard.py`）：
  - **Q7c-01**：新增 `V013Replica`——0.13.0（`dbab711`）再停规则的逐条复刻：同样的原始采样（不滤波）、同样的证据（上报速度 > 2 cm/s / 0.05 rad/s；相邻采样跳变 > 3 mm / 5 mrad 且 > 阈值×Δt；“偏离”前离原始快照 > `pos_m`/`yaw_rad`/`joint_rad`；“偏离”后离它自己上次再停处 > 3 mm / 5 mrad），无噪声/游走/漂移容许、无连续样本要求、自己的 0.5 s 节奏（`restop_interval_s`，自它上次再停起算），只在它自己的再停时移动参考、置“偏离”。复刻会再停、而本守护在该采样没有调用 `stop()` 时，静默调用一次 `adapter.stop()`（不发事件、不计再停、不升级，`precautionary_stops` 计数）。原“相对上次任意 `stop()`、滤波值、2 个连续样本、≤1 次/s”的预防性规则删除；`precaution_interval_s` 默认 1.0 → 0.5，只作开关（≤0 关闭）。
  - **Q7c-02**：监督器新增 `reset_count`（每次复位成功、在清锁存之前加一）；提示记下所属锁存（`reset_count`），守护每拍若发现复位过（即使复位后一个周期内又被停、从未看到“未锁存”）就作废旧提示，新锁存越界时再发一条；`describe()` 的 `drift_notice*` 只在锁存期间、且只报告当前锁存的提示（复位成功后立刻为 false/null）。
- 改动（spec，总指挥预先批准）：**Q7c-07** `normalize_reset_expected` 上限 30 → **25**（`RESET_EXPECTED_MAX_S = RESET_EXPIRE_CAP_S(30) - RESET_EXPECTED_MARGIN_S(5)`），保证桌面 `expire_s = reset_expected_s + 5` 总在 30 s 上限内；运行时 `core.RESET_EXPECTED_MAX_S` 同步为 25。
- 复验（`scratchpad/fix3ab/`，终审脚本原样复制，`git archive` 的 v0.13.0 与 d498d79 输出取自 `final07/out/`）：
  - `extra2.py`（4 种子 × 6 族）：24/24 `ok`（d498d79：24/24 WORSE/first）。例：伪造游走 + 固定停 61 s 0.287/0.298/0.289/0.262 m（v0.13.0）→ 0.287/0.298/0.289/0.247（d498d79 0.42–0.47）；真实游走 1 mm/√s + 固定停 30 s 0.539/0.512/0.513/0.474 → 完全相同（d498d79 0.53–0.63）；停后 0.6 s 17.0–17.4 m → 16.5–16.7 m（d498d79 24.1–24.3）。
  - `extra.py`（52 个）：42 `ok`、1 WORSE、3 first，均非真实位移退化：`fyw1.5_yaw10mrad_fixedpause3.0` 的“终点”是**卷绕后的**偏航（两版都转过 π），展开后的真实转角 v0.13.0 5.708 rad、d498d79 5.345、本次 4.873（0.6/1.5/10 s：17.10/9.70/1.99 → 14.06/7.90/1.88）；`fw1.5_creep0.7_pause3.0`、`fw1.5_creep0.7_fixedpause61`、`fw1.5_resume19mm_fixedpause185` 的首个 `stop()` 与 v0.13.0 同刻同位移、最大位移相同或更小，只是**计数的**首次再停更晚或没有（游走容许内只做静默停止 + 漂移提示，与终审已列的 Q7c-06 同类）。
  - `adv.py`（43）/`adv2.py`（19）：仅 `sit_3s_declared_1h`、`alt_x_amp0.19_0.6mm_s_fakewalk1.5` 标 first（终审 §1.2 已核对、非退化），其余 `ok`；8 h 游走 8+2 个种子计数再停 0/2/0/1/1/1/2/4、0/0，均不升级（与 d498d79 相同）。
  - 误报（计数再停/事件/升级）相对 d498d79：`noise07`、`noise07_cal`、`sit60`、`r8_attack_sit`、`flush`、`grid`、`drift_report`、`hung500`、`restart`、`params*`、`expire` 逐项相同；推开/坐下/IMU/游走等合法场景计数再停、升级、提示条数全部相同；`verify06`/`attack_a` 仅 `creep 0.015 m/s 停后 2 s 恢复` 再停 262 → 250、600 s 位移 1.08 → 1.035 m（更少）。
  - 代价：0.13.0 会计数再停的静止本体，现在以同样频度静默 `stop()`：`noise07` 位置噪声 1–2 mm 的四足 2270–5923 次/h（d498d79 0），机械臂关节噪声 68/4742 次/h，游走 1 mm/√s 8 h 约 5000–5650 次（与 d498d79 相当）；即最多 2 次/s 的零速 Twist + 取消目标（ROS 2），与 0.13.0 的再停频度相同、但不再有事件。
- 新回归：`runtime/tests/test_iter07_fix3_runtime.py`（Q7c-01 ×2 + 2 条保底、Q7c-02 ×2 + 1 条保底；参考值为同一驱动/种子在 v0.13.0 与 d498d79 上的实测）；`spec/tests` `ResetExpected` 与 `test_iter07_fix2_runtime.ResetCapabilities` 收紧为 (0, 25]（25.0001/26/30 拒绝）。非保底项在 d498d79 上全部失败。
- 改了一条旧断言（未放宽）：`test_iter07_runtime.NoisyStillBody.test_creep_still_caught_under_noise`——2 mm 位置噪声 + 每次停止 2 s 后恢复 1.5 cm/s 的驱动，现在被复刻的 0.13.0 噪声再停（静默）每 0.5 s 按住，**一步都没走**（120 s 内 0 m、静默停止 180 次；0.13.0 同样 0 m、计数再停 180 次；d498d79 5.4 s/0.0525 m 首次计数，120 s 内最远 0.54 m）；原断言（7 s 内计数再停、< 7 cm）改在关闭预防性停止时对守护自身的计数规则照旧检查，并新增默认配置下位移 < 3 mm 的断言。
- 契约变更请求（INTERFACES_V1 §9.2，总指挥负责）：① 预防性停止改为“逐条复刻 0.13.0 的再停判据与节奏（原始采样、3 mm/5 mrad、无持续样本、0.5 s），仅静默”，删去“≤ 1 次/s”；② 漂移提示“每次锁存一次”明确为“每次复位成功即作废，`drift_notice*` 只在锁存期间报告”，`SafetySupervisor` 新增只读属性 `reset_count`（运行时内部使用，未进 `/v1/status`）；③ 清单 `safety.reset_expected_s` 取值 (0, 25]。

## 第 07 轮第四次修复 · 2026-11-09 · 端侧运行时：锁存守护所有 `stop()` 共用一个节奏（Q7d-01 P1、Q7d-02 P2）（runtime/）

- 背景：`docs/evolution/reviews/2026-11-09-iter07-final-rereview.md` 判 0.14.0 不可发布：守护的计数再停与 `V013Replica` 预防性停止各按自己的 0.5 s 节奏、互不知会，`stop()` 交错到 0.1–0.4 s 一次（最多 4 次/s，Q7d-02）；“距上一次 `stop()` 不足 0.5 s 的重复 `stop()` 忽略”的驱动把整串停止吞掉，1 h 真实位移是 v0.13.0 的 1.5–8.9 倍（Q7d-01）。分支 `iter07-fix4-runtime`（基于 `643549a`，未合并 master）。版本号未动（总指挥负责，Q7d-04）。
- 改动（`latch_guard.py`）：
  - **一个节奏**：守护发出的所有 `adapter.stop()`（计数再停、预防性停止、反馈无效、监视线程的读取阻塞）都经 `_slot_open`：距守护上一次 `stop()` ≥ `restop_interval_s`（0.5 s）才发，即 ≤ 2 次/s；`_last_call` 跨锁存保留（同复刻的 `last_restop`）。操作员 `/v1/stop`、`/v2/stop` 不经此节奏，总是立即执行。
  - **计数再停跟复刻的拍子**：预防性停止开着时（默认），守护线程上的计数再停只在 0.13.0 规则（复刻）在**同一采样**也看到运动或反馈无效时才调用 `stop()`（复刻随即把它当作自己的再停）；否则这次再停**合并**进复刻的停止序列——照样计数（`restops`/`motion_restops`）、发事件（`adapter_stopped` = 最近一次 `stop()` 的结果）、参与升级，只是不单独调用 `stop()`（`merged_stops` +1）。于是守护线程的 `stop()` 调用时刻与 v0.13.0 的再停时刻逐条相同。读取阻塞的再停（复刻看不到）不受此限，只受节奏约束。`precaution_interval_s` ≤ 0（关闭预防性停止）时，计数再停按节奏照常调用。
  - **复刻合并**：复刻到期而守护在 0.5 s 内刚调用过 `stop()`（只可能是读取阻塞的那一次）时，`V013Replica.restopped(t_那次, at=那次时的位置)`——把那一次当作复刻在那一刻的再停（参考与节奏都移过去），不另发。
  - `latched_motion` 新增 `stop_calls`（守护发出的 `stop()` 总数）、`merged_stops`。
  - 为什么不选“计数再停在节奏槽内照常调用”：试过合并窗口 0.5/1/2 s（计数再停只在距上次 `stop()` ≥ W 时调用），去抖场景从 3–9 倍降到 0–2%，但仍有 7–10 个场景比 v0.13.0 多 0.1–2%（`accepted0.5_fw1.5_19mm_fixedpause10.0` 1.598 → 1.669 m、`fw1.5_resume19mm_fixedpause1.5` 9.016 → 9.161 m 等）：守护的滤波/持续样本让它比复刻晚 0.3–0.7 s 才确认同一段运动，那一次 `stop()` 落在驱动的保持期里（无效），却把复刻下一次真正需要的停止推后最多 0.5 s。只让计数再停跟复刻的拍子，才使 `stop()` 序列与 v0.13.0 逐条一致。
- 复验（`scratchpad/fix4ab/`：`git archive v0.13.0`（与 `rr07/v013` 逐文件一致）对本分支；复审脚本 `rr07/sc/` 原样复制，统一用 `wrap3.py` 记录每一次 `stop()`；`cmp3.py` 判据同复审：最大/终点真实位移、首个 `stop()` 位移不得大于 v0.13.0，容差 1.5 mm/rad）：
  - `adv`（43）、`adv2`（19）、`adv3`（33）、`adv4`（31）、`adv5`（14，偏航展开真值）、`extra`（46）、`extra2`（24）：**210 个场景全部 `ok`**，唯一例外 `sit_3s_declared_1h` 标 first（清单声明 `stop_settle_s`、合法坐下，两版对比的是 0.13.0 的误报，复审已认定不计）。`adv.py` 的 `maxcal_*` 6 个不参与比较（运行时声明最大放宽，0.13.0 不支持覆盖，`wrap3` 下 0.13.0 以默认配置运行），相对 `643549a`：5 个逐项相同，`maxcal_resume_0.019_2s` 5.773 → 5.803 m（+0.5%，放宽本身有告警）。
  - `stop()` 频度：所有场景任意 1 s 内 ≤ 2 次、最小间隔 0.5 s（`643549a`：4 次/s、0.1 s）；209/210 个场景 `stop()` 总数与 v0.13.0 **完全相同**（8 h 游走 1 mm/√s 8 种子 5006–5656、0.5 mm/√s 1256/924，与 v0.13.0 逐种子相同；`643549a` 多 0–4 次）。
  - Q7d-01 关键行（1 h 真实位移，v0.13.0 / `643549a` / 本次）：真实游走 + 滑动去抖 0.5 s，停后固定 0.6 / 10 / 30 s：22.20 / 67.04 / **22.20**、13.04 / 60.98 / **13.04**、6.15 / 54.71 / **6.15** m；伪造游走固定 3 s：18.19 / 65.26 / **18.19**；去抖 0.3 s：4.94 / 60.04 / **4.94**；非滑动去抖固定 3 s：5.167 / 5.225 / **5.167**。
  - 误报不变：`noise07` 计数再停与 `643549a` 逐项相同（静止噪声本体 0，IMU 漂移 0.5°/min 10 次/h），不升级。
  - 计数再停/升级相对 `643549a`：驱动不服从的场景里轨迹不同（停止时刻变了），计数再停次数有增有减；升级无一丢失、无一新增；有 10 个升级晚 > 1 s（最多 `realyw1_yaw10mrad_fixedpause10.0` 1214 → 1249 s、`fw1.5_resume19mm_pause1.5` 37.0 → 42.2 s），也有提前的（`realyw1_yaw10mrad_fixedpause3.0` 92.7 → 59.6 s）。v0.13.0 无升级，不涉及发布规则。
- 全量测试：spec 17、runtime 482、cloud 211、sdk 84、tests 283（skipped 39）全部 OK。
- 新回归：`runtime/tests/test_iter07_fix4_runtime.py`——48 个随机对抗驱动（停后再走/跳变/白噪/游走/偏航噪声/NaN 与 `velocity: null`/混合，含 0.3/0.5/0.6 s 去抖）任意两次守护 `stop()` 间隔 ≥ 0.5 s、任意 1 s ≤ 2 次、`stop_calls` 等于实际调用数；读取阻塞路径同节奏；去抖驱动 6 种（10 min）位移不大于 v0.13.0 同驱动同种子实测、且没有一次 `stop()` 被去抖吞掉；合并的计数再停仍计数并升级；操作员停止不受节奏限制。除最后一项（保底）外在 `643549a` 上全部失败（22/48 个随机驱动间隔 0.1–0.3 s；去抖 9.92 / 9.17 / 6.64 m 对 v0.13.0 2.85 / 0.83 / 0.82 m）。
- 契约变更请求（INTERFACES_V1 §9.3，总指挥负责）：① “代价……（ROS 2 上最多 2 次/s 零速 + 取消目标）”改为“守护发出的所有 `stop()`（计数再停、预防性停止、反馈无效、读取阻塞）共用一个节奏：相邻两次 ≥ `restop_interval_s`（0.5 s），**≤ 2 次/s 合并节奏**；预防性停止开启时，守护线程的 `stop()` 时刻与 v0.13.0 的再停时刻逐条相同，计数再停只在 0.13.0 规则同时看到运动时调用 `stop()`，否则合并（计数、事件、升级照旧）；操作员停止不受此限”；② `latched_motion` 新增 `stop_calls`、`merged_stops`；③（可选，HAL 文档）驱动可以假定守护的重复 `stop()` 间隔 ≥ 0.5 s。

## 第 07 轮第五次修复 · 2026-11-10 · 端侧运行时：读取阻塞再停与复刻（Q7e-01 P1、Q7e-02、Q7e-04）（runtime/）

- 背景：`docs/evolution/reviews/2026-11-10-iter07-release-review.md` 判不可发布：`_precaution` 以 `last == now`（机器人时刻相等）判断“本采样守护线程已停”；适配器时钟分辨率粗（≥ 监视周期 20 ms）时，监视线程的读取阻塞再停与阻塞后第一个采样同刻，复刻参考被移到阻塞**后**的位置（0.13.0 是阻塞前），之后再停变晚，3 个场景真实位移多 2.6–4.6%。
- 改动（`latch_guard.py`，按复审 §2.3 方向）：
  - **Q7e-01**：`_mark_call(now, src)` 记下最近一次守护 `stop()` 的来源——`"tick"`（守护线程，记第几拍 `_tick_seq`）或 `"stall"`（监视线程读取阻塞再停）。“本采样已停”= 来源是守护线程且是**本拍**，不再看时刻是否相等；最近一次是读取阻塞再停时，复刻到期一律合并进它：`restopped(那次时刻, {}, at=那次时的位置)`（不带证据、不置“已偏离”，与 0.13.0 的读取阻塞再停相同）。粗分辨率时钟与真实单调钟下行为一致。
  - **Q7e-02**：合并的计数再停（不单独调用 `stop()`）发出的 `safety.motion_while_latched` / `safety.feedback_invalid_while_latched` / `safety.stop_not_obeyed` 带 `merged: true`、`adapter_stopped: false`；单独调用的带 `merged: false`、`adapter_stopped` = 该次结果。
  - **Q7e-04**（`hal.py` 文档）：`stop()` 说明写明守护的重复调用相邻 ≥ 0.5 s、驱动不得在 > 0.5 s 的窗口内去抖/丢弃 `stop()`；`clock` 应为高分辨率单调时钟。README 同步。
- 复验（`scratchpad/fix5ab/`：`git archive v0.13.0` 对本分支；`rel07/sc/`（含 `adv6.py`）原样复制，`wrap3.py` + `cmp4.py`，第三列为复审时的 HEAD 输出）：`adv`/`adv2`/`adv3`/`adv4`/`adv5`/`adv6`/`extra`/`extra2` 共 472 个可比场景**无一差于 v0.13.0**（唯一 `first` 为合法坐下 `sit_3s_declared_1h`；`maxcal_*` 6 个不可比，与复审 HEAD 逐项相同）。`adv6` 262/262 `ok`，含 `hang*` 26 个与 `hangE*` 26 个：复审的 3 个 WORSE 回到 v0.13.0（1.2297 → **1.1761** m、1.3281 → **1.2897**、2.1842 → **2.1291**），全部 `hang*` 真实位移与 v0.13.0 相同；`stop()` 总数 468/472 与 v0.13.0 相同（不同：`sit_3s_declared_1h` 0 次、3 个读取阻塞场景 ±1–5 次），任意 1 s ≤ 2 次、最小间隔 0.5 s。8 h 游走 10 个种子与 v0.13.0 逐项相同（真实位移 0）。非读取阻塞场景与复审 HEAD 逐项相同。
- 全量测试：spec 17、runtime 485、cloud 211、sdk 84、tests 283（skipped 39）全部 OK。
- 新回归：`runtime/tests/test_iter07_fix5_runtime.py`——`ManualClock` 下读取阻塞、阻塞结束采样与监视线程再停同刻：复刻参考 = 阻塞前位置、“已偏离”不变；14 个读取阻塞场景（粗时钟/1 ms 后返回 × 阻塞 1.5/3/10 s × 停后 0.6/3 s/不服从）的真实位移与 `stop()` 次数 = v0.13.0（`dbab711` 上同驱动实测）；合并再停事件 `merged`/`adapter_stopped`。在 6d8af00 上 3 项全部失败。
