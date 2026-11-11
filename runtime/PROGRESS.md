# 端侧运行时 Agent 进度日志（只追加）

- 22:07 读完协作协议、INTERFACES_V1、TARGET_ARCHITECTURE、spec、brainos/edge.py·service.py；基线根目录测试 72 通过（20 跳过）/ 确认 v1 调用方实际期望：/v1/validate 看 `accepted`（契约写的是 passed/cases，两者都返回）、/v1/evaluate 要回显 application_digest·robot_manifest_digest·evidence_ref·passed / 下一步写基础模块
- 22:07 已完成 _paths（spec 路径引导）、clock（RealClock 支持 time_scale、ManualClock）、errors、events（无锁事件流）、store（sqlite）、hal（RobotAdapter/ActionHandle）、safety（限幅/拒绝/看门狗/急停锁存/action_chunk 逐步限幅，含指令在途时触发急停的二次停止）/ 下一步写能力总线、技能运行时、仿真本体
- 22:07 设计取舍：急停锁存后 v1 /v1/start 不会自动解锁，新增 /v2/reset（需 hardware_authorized）——桌面端暂无解锁入口，将写入契约变更请求；仿真本体的 /v1/evaluate 如实返回 passed 并标注 simulated=true、evidence_ref 用 simulation:// 前缀，但 brainos 目前不检查该标志，作为风险上报
- 22:13 完成 bus（能力总线）、skills（技能包存储/加载/SkillContext）、runner（运行管理、超时、取消、宽限期撤销）、ledger（request_id 幂等）、core（v1/v2 业务）、gateway（HTTP）、cli、SimQuadruped/SimArm / 尚未运行 / 下一步写示例技能并做冒烟测试
- 22:16 冒烟测试通过（四足 patrol 3 航点成功、status_report 成功、wave 装到四足被拒）；写完 ROS2 骨架、test_manifest、test_safety / 25 个测试全过 / 下一步写技能运行、HTTP 网关、重启恢复与 brainos 集成测试
- 22:18 写完 test_skills（兼容/跨本体/成功/取消/超时/失败/未声明能力/宽限期撤销）与 test_gateway（认证/幂等三情形/重启 running→unknown/锁存跨重启/导航中 stop<0.5s）/ 首跑 1 个失败：测试里 timeout_s 超过 spec 上限 86400，改测试后 56 个全过（8.6s）/ 下一步 v1 兼容与 brainos Platform 集成测试
- 22:21 写完 test_v1_compat（含真实 brainos Platform：connect_robot→onsite_test→deploy→operate start/stop 全流程通过）与 test_ros2_and_cli；71 个测试连续 3 次全过（约 13.5s），根目录 72 个、spec 7 个测试不受影响 / 下一步写 README 与进化记录
- 22:23 写完 README（架构/安全模型与信任边界/适配器教程/技能教程/API/仿真运行/已知限制，并实测 README 快速上手脚本可用）与 EVOLUTION_ENTRY（第 01 轮）/ 71 测试仍全过 / 下一步只 add runtime/ 并提交
- 22:23 已提交 b42b0fc（仅 runtime/ 38 个文件）；核对期间合入的契约变更只涉及云平台（§2）与新增 CAPABILITIES_V1.md 文档，spec/ 代码未变，运行时不受影响 / 完成，提交交付报告

## 第 01 轮修复（依据 docs/evolution/reviews/2026-11-05-iter01-review.md）

- 22:55 读完协作协议、INTERFACES_V1（新增 /v1/reset、仿真证据规则）、审查报告与复现脚本；基线 runtime/tests 71 个中 3 个按预期失败（spec 新语义）/ 下一步逐条修复
- 23:00 R-01 已修：安全监督器引入单调"停止代数(epoch)"，每次 stop +1；运行在准入时记录代数，HTTP 直接调用在请求到达时记录；supervisor.check、下发前最后检查（读完 joint.state 之后）、after_dispatch、SkillContext 每次 call/read 都比对，不一致即拒绝（code=stale_after_stop），被撤销的运行永远不能再下发；reset 采用比较后清除，与 stop 竞争时保持锁存
- 23:00 R-02/R-03 已修：SafetySupervisor 初始化时再校验清单限值（有限、正数/非负、lower/upper 长度=dof 且 lower<upper、动作块 dim 与关节数一致、workspace/max_linear_speed），不合格拒绝启动；_clamp 断言 low<=high 且全部有限；限幅器任何异常 fail closed 为 safety_rejected；下发值再查 isfinite；关节限位长度不一致拒绝（不再 zip 截断）；ROS2 配置解析拒绝 NaN/Infinity
- 23:00 R-04 已修：joint_position 动作块在本体未声明 joint.state、读取失败、长度≠dim 或含非有限值时拒绝
- 23:02 R-05 已修：网关改为有界连接（默认 32 个普通名额，超限立即 503 busy，不建线程）；整个请求（请求行+头+体）10 s 总期限、单次 socket 操作 5 s；保留 4 个停止专用名额（只服务 POST /v1/stop、/v2/stop，1 s 内须发出请求行，满时驱逐最老的未识别连接）；stop 路径不再新建线程（常驻持久化线程）
- 23:03 R-06 已修：Nav2 目标发送中被取消时，在 send_future 完成回调里立即 cancel_goal_async；stop 同时取消已知目标并调用 Nav2 取消全部目标服务（各步互不影响）；完成的 handle 自动清理；simulated 由配置 simulated / use_sim_time 或节点参数 use_sim_time 决定
- 23:03 R-07 已修：实现 POST /v1/reset（需 request_id 幂等与 hardware_authorized，同 /v2/reset 语义）；/v1/validate 的"急停未锁存"失败项提示"请在桌面端解除急停（POST /v1/reset）"
- 23:04 R-08（runtime 部分）：仿真 evaluate 继续如实标注；test_full_delivery_flow 改为 test_simulated_body_flow——只断言运行时返回的 simulated/evidence_kind/simulation:// 正确，以及不变式 4（仿真证据不被当作现场证据、不能部署），不再断言桌面端接受仿真证据
- 23:05 P2 已修：R-09 看门狗独立线程（不碰存储）、runner.tick 先处理全部超时停车再做落盘；R-10 锁存记录按（启动序号, 代数, 阶段）单调写入、reset 先落盘再清内存、stop 原因持久化、异常关机后启动默认锁存（SIGTERM 视为正常关闭）；R-11 v2 直接调用 action 有能力级默认超时（导航 600 s 等），请求 timeout_s 只能缩短；R-12 参数先判类型、非有限值拒绝、v1 validate 把类型错误变成失败项、账本只在可能发生副作用后才记 unknown；顺手修 R-14（reset 重放返回当前真实锁存状态）、R-15（超大整数→400）、R-16（文件/目录路径冲突→400）
- 23:07 测试：更新 3 个预期失败测试（NaN 被拒即可、v1 视图携带 simulated/vendor/model、仿真流程）；新增锁存零下发、代数、在途调用跨 stop+reset、撤销冻结、非法清单、动作块 fail closed、慢连接下 stop<1 s、503、请求期限、NaN JSON、无线程可建时 stop、锁存写入顺序、异常关机、看门狗独立、ROS2 取消竞态等测试；runtime/tests 全部通过 / 下一步：审查脚本复验、变异验证、跑其他模块测试、更新 README 与进化记录
- 23:25 审查脚本复验全部通过：rt_stale_after_reset → 在途指令未到达本体（False）；rt_limits（绕过 spec 跑运行时自身防线）→ 所有非法清单拒绝启动、动作块缺 joint.state/长度不符被拒、超大整数被拒；rt_slowloris → 400 个半截连接线程 5→41（有界），期限到后回到 5；rt_stop_locks → 所有锁被占时 stop 仍 5–7 ms；rt_watchdog_store → 看门狗 0.21 s 触发（修复前 3.08 s）；rt_misc → 参数错误记为拒绝、validate 返回失败项、action 默认 deadline 600 s
- 23:30 变异验证（每次临时改坏一处、跑全量 runtime 测试、按哈希恢复）：审查中存活的 R1（锁存主检查）被 test_latched_motion_never_reaches_adapter 杀死、R7（finalize 撤销）被 test_revoked_run_cannot_call_or_mutate_its_record 杀死；其余审查变异 R2–R12（3 处按新代码改写）全部杀死；新增机制的 15 个变异（停止代数、下发前检查、异常关机锁存、停止名额、请求期限、清单复核、账本分类、锁存写入顺序等）除 1 个等价变异外全部杀死
- 23:31 变异过程中发现并修复 1 个真实问题：stop 原因写入与锁存写入同代数时被当作"旧写入"丢弃（偶发）；同键现在允许覆盖，只拒绝更旧的状态
- 23:33 测试：runtime/tests 109 个，连续 3 次全过（约 32 s）；tests/ 82 个通过（20 跳过）、spec/tests 10 个、cloud/tests 112 个全过；sdk/tests 26 个中与运行时相关的 test_cli/test_robot_client/test_package_and_templates 全过，test_cloud.setUpClass 1 个错误来自云修复 Agent 尚未提交的 C-01 改动（白名单邮箱不再自动成为运营员），与 runtime 无关，已报告总指挥 / 下一步只 add runtime/ 并提交

## 第 01 轮复审修复（依据 docs/evolution/reviews/2026-11-05-iter01-rereview.md）

- 读完复审报告与 runtime 相关复审脚本（rt_epoch_stress / rt_manifest_mutation / rt_action_reset / rt_conn_attack / rt_evict_flood）；本轮修 N-02、N-03、N-04、N-06，补存活变异 M2、M4、M6 的测试
- N-02 已修：reset 也推进停止代数；invoke / 技能运行 / v1 start / v1 evaluate 在请求到达时（写账本前）记录代数，准入时比对；锁存期间到达、被慢存储拖到 reset 之后的请求返回 stale_after_stop，零下发（复审脚本 B：command 与 action 均 stale_after_stop、reset 后适配器零运动调用）；另设停止计数判定 reset 竞争，复位中止时写入更新的锁存记录
- N-03 已修：看门狗拿不到速度锁（有指令卡在适配器里）时不等锁，直接 adapter.stop() 并在短线程发零速度；hal.py 契约写明 stop()/零速度须可与进行中的 invoke 并发（复审脚本 C：指令卡住 3 s 期间 0.21 s 发出零速度，修复前从未发出）
- N-04 已修：清单启动时深拷贝→校验→冻结（tuple/只读映射），bus 自己的清单也是深拷贝；限位比较 NaN 安全；监视线程每秒比对 adapter.manifest()，变化即停车、拒绝运动（manifest_changed，/v1/status 可见）直到重启；ctx.param 返回普通副本（rt_manifest_mutation：不再共享列表，原地改成 NaN/±1e6 均被拒）
- N-06 已修：动作块 dt ≤ 2/rate_hz（无 rate_hz 时 ≤ 0.5 s）、步数 ≤ 1000、总时长 ≤ 10 s，README 已写明
- 复审脚本其余项复验：rt_epoch_stress A 两轮共约 2.7 万次调用、1.4 万次 stop，0 违例；rt_action_reset、rt_conn_attack 通过；rt_evict_flood 泛洪期间真实 stop 189/189 成功
- 变异测试（在 git worktree 中进行，主工作区未改动，结束后已删除 worktree）：复审存活的 M2、M4、M6 全部杀死；本轮新机制 10 个变异先杀 8 个，补 2 个测试（bus 清单不共享、适配器 reset 期间 stop 后磁盘仍锁存）后 13/13 全部杀死
- 测试：runtime/tests 126 个（新增 17 个），连续 2 次全过；tests/ 82 个（20 跳过）、spec/tests、sdk/tests、cloud/tests 全过（cloud/sdk 在其 fix2 提交落地后复跑） / 下一步只 add runtime/ 并提交

## 第 02 轮（异步现场测试、技能进程隔离、可安装包）

- 09:00 读完协作协议（第 7/8 条）、INTERFACES_V1 §1 与 §4.1/§4.2/§4.4、ADR-009/010/015、第 01 轮记录、runtime README/EVOLUTION_ENTRY；基线 runtime/tests 126 个全过（39 s）；环境为 root、Python 3.13，spec 与 sdk 已 pip -e 安装 / 方案：subprocess + socketpair 上的 JSON 行协议（不用 multiprocessing：它会 pickle 对象且子进程默认继承父进程模块状态，不易控制）；root 运行时子进程降权为 nobody / 下一步写沙箱子进程引导与主进程代理
- 09:40 写完 sandbox_child.py（子进程引导：自施加 rlimit、NO_NEW_PRIVS、PDEATHSIG、导入守卫+替身模块、SIGTERM→SkillStopped）、sandbox.py（主进程宿主：最小环境、-I、新会话、降权 nobody、只读包副本+私有 tmp、严格协议校验、限流、日志上限）、evaluations.py（异步现场测试），并接入 runner/core/gateway/cli / 冒烟：status_report 与 patrol 在子进程中成功（单次运行开销约 43 ms），/v1/status 显示 sandbox / 下一步跑全量 runtime 测试
- 10:05 runtime/tests 126 个在默认 process 沙箱下全过；过程中修了 2 处：(1) 子进程启动期间收到 SIGTERM 会被默认动作杀死，导致运行结束路径与运行中不一致 → 握手完成（处理器已装好）后才补发 SIGTERM；(2) SIGTERM 处理器在任意位置抛 SkillStopped 会逃出技能自身代码（退出码 5）→ 改为只置标志，此后 ctx.call/read/check_stop/sleep 在子进程本地立即抛 SkillStopped，log/progress 仍可用于清理代码；不理会的技能宽限期后 SIGKILL / test_v1_compat 适配桌面端已改用 async 的现场测试（跟随 evaluation_id 后断言不变）/ 下一步跑 sdk/tests 与其他套件
- 10:50 新增可选 pids cgroup（可写时启用，v1 或已委派的 v2；本环境 v1 可用）：rlimit+进程组抓不到 setsid() 的孙进程，cgroup 能；新增 tests/test_sandbox.py 18 个测试全过：示例技能与 4 个 SDK 模板原样在子进程中运行（含无 SDK 时回退替身基类）；导入运行时内部 5 个模块全部被拒，绕过守卫后进程内无任何运行时活对象；令牌/父进程 environ·mem/状态目录/包目录写入均被拒，RLIMIT_AS 下 3 GiB 分配 MemoryError，rlimit 不能提高；17 种伪造/畸形/超大消息全部 fail closed（运行失败、子进程被杀、适配器零调用），之后正常技能与 stop 不受影响；忽略 SIGTERM 的死循环：HTTP stop <1 s、立即停车、约 1 s 后 SIGKILL；fork 炸弹（含 setsid 逃逸）：任务数受限、stop <1 s、全部被清理；日志/print 刷屏有上限；子进程在导航进行中崩溃→导航被取消 / 下一步写异步现场测试测试
- 11:25 §4.4：runtime/pyproject.toml（neuxmind-runtime 0.2.0，依赖 neuxmind-spec，入口 neuxmind-runtime=neuxmind_runtime.cli:main，相对路径引导保留）；在 scratchpad 独立 venv 中 pip install -e spec -e runtime 成功，neuxmind-runtime serve/check 可用，经 HTTP 跑通沙箱 patrol 与异步现场测试 / 实测发现：root 运行时若解释器在私有目录（0700）里，nobody 无法执行 → 启动时检查，退回真实解释器并在 /v1/status 与启动输出中提示，新增 --sandbox-python；新增 test_async_evaluate.py（13）与 test_packaging.py（5）/ 下一步在 git worktree 中做变异测试
- 11:55 补强测试（为变异测试预先堵漏）：SIGKILL 须在 kill_grace+0.6 s 内由监视线程发出（不只靠 2 s 后的收尾）；通知后循环技能不再发请求（本地短路）；限流；会话首领与 NoNewPrivs；运行时被 SIGKILL 时技能进程随之死亡（PDEATHSIG）；启动期 stop 不被 SIGTERM 默认动作杀死 / 修 2 处：运行时被杀后遗留空 cgroup → 每个运行时独立 cgroup 子目录，启动时清理空遗留（rmdir 只对空组生效，不影响其他运行时）；子进程退出码偶发 None（EOF 先于退出状态）→ 短暂等待 / runtime/tests 共 165 个全过（126 原有 + 39 新增）
- 12:40 变异测试第 1 轮（git worktree，43 个变异，主工作区未改动）：34 杀死、9 存活。存活分析：S21 close_fds=False（Python 在 pass_fds 时强制 close_fds）、S29 不检查握手（hello 由可信引导代码在技能代码之前发出）、E11 passed 忽略停止说明（停止时状态必被改为 stopped）为等价变异；其余 6 个补测试：NaN/Infinity 进入 log 数据、运行时 non-dumpable 直接检查、只去掉替身模块时导入守卫仍拒绝、SIGTERM 握手前延后（单元）、收尾即强杀（不等宽限期）、守卫在下发前生效（单元）/ 补测试过程中发现并修复 1 个真实缺陷：第二个运行时启动时的清理会 rmdir 掉第一个运行时当时为空的 cgroup 目录，使 fork 炸弹的 setsid 子进程逃出（实测残留 32 个 nobody 进程并耗尽其进程名额，连带其他技能无法启动）→ 只清理 PID 已不存在的运行时目录，自身目录缺失时重建，新增回归测试 / 下一步在 worktree 重跑全部变异
- 13:35 变异测试第 2 轮（worktree，全部 43 个按最新代码与测试重跑）：38 杀死；S10 存活原因是 worktree 位于 0700 目录、nobody 读不到源码导致导入失败与守卫无关 → 测试改为提供可读源码副本并断言绕过守卫才能导入，复跑后杀死。最终 39/43 杀死，4 个等价：S21（pass_fds 强制 close_fds）、S29（hello 由可信引导代码发出）、S32（回复值都是 spec 校验过的输出，序列化检查仅为防御）、E11（有停止说明时状态必为 stopped）；worktree 已删除，主工作区未被变异改动 / 下一步全量回归
- 13:55 收尾：/tmp 下的沙箱工作区按运行时 PID 命名，启动时清理已死运行时的遗留（新增测试）；全量：runtime 171、spec 11、sdk 48、cloud 160、tests/ 113（20 跳过）全过；无残留 nobody 进程与 cgroup / README（安装、沙箱架构与剩余信任边界、异步现场测试）、EVOLUTION_ENTRY 第 02 轮已写 / 下一步只 add runtime/ 并提交

## 第 02 轮审查修复（依据 docs/evolution/reviews/2026-11-08-iter02-review.md）

- 14:30 读审查报告与 scratchpad/review02 脚本；基线 runtime/tests 171 通过；本轮修 Q2-01(P1)、Q2-04/05/08 / 先修 Q2-01
- 15:10 Q2-01：沙箱根目录移出全局可写 /tmp（同用户→<state>/sandbox；降权→临时目录下运行时所有的 0711 neuxmind-sandbox-<euid>，技能用户不能创建条目故无法植入符号链接）；清理用 lstat/不跟随链接/只删本 uid 真实目录/fwalk follow_symlinks=False+fchmod/rmtree 基于 fd；预创建为他人或符号链接的基目录拒用退回私有 mkdtemp。实测：sim-up SDK 集成测试暴露“降权子进程无法穿越 0700 状态目录”，据此分场景放置（同用户放状态目录、降权放临时目录）
- 15:30 Q2-04：无可写 cgroup 时设 CHILD_SUBREAPER + 结束时按 /proc 收割重挂的技能 uid 后代（维护在途子进程表防误杀）；status 增 child_subreaper/process_containment；CLI 启动告警；README 写明局限。测试：强制 cgroup.base→None，fork+setsid 技能，运行结束后逃逸孙进程 6 s 内被清理
- 15:40 Q2-05：get_evaluation 对非三态返回 404；Q2-08：日志配额移到 RunState 按运行累计（多步应用共享），新增三步 flood 应用测试封顶 100
- 16:00 runtime/tests 177 全过；sdk 48、spec 11、tests/ 回归；无残留 nobody 进程/cgroup / 下一步 worktree 变异测试、提交
- 16:40 worktree 变异第 1 遍（12 个新防线变异）：5 杀 7 存活——符号链接两层防护互相掩盖、收割者测试过粗、无 subreaper 时测试被 skip。补逐层单元测试（_remove_tree 顶层/嵌套链接、清理跳过链接与他人目录、收割者用 mock 的进程表）；无 cgroup 测试改为断言 subreaper 已设置而不是跳过
- 16:55 补测试时发现真实缺陷：运行时成为 child subreaper 后，被杀的逃逸孤儿成为运行时的僵尸而无人回收（原实现在无 cgroup 部署上会泄漏僵尸；测试中表现为 nobody 任务数不回落）→ 收割者按显式 pid waitpid（不用 -1，避免抢走 subprocess 子进程的退出状态），新增断言
- 17:10 worktree 变异第 2 遍（14 个，含新增“不回收僵尸”“收割者忽略 uid”）：14/14 杀死，各由对应测试杀死；worktree 已删除。runtime/tests 180 全过，无残留 nobody 进程/僵尸/cgroup
- 17:25 全量：runtime 180、sdk 48、spec 11、tests/ 123（20 跳过）全过 / 提交（只 add runtime/）

## 第 03 轮（§5.0 平台基线、§5.1 端侧模型安装、§5.2 遥测持久化）

- 12:00 读完协作协议、INTERFACES_V1 §1/§4/§5.0–5.2、ADR-015/017/018、第 02 轮记录"已知问题"、README/EVOLUTION_ENTRY；建了三套测试环境：3.13（root，系统解释器）、3.10（root，uv 的 3.10 复制到 /opt/nx-py310 当"系统解释器"，venv 在 scratchpad/runtime-agent/）、非 root（临时用户 nxtest，3.13 与 3.10）
- 12:10 §5.0 Python 3.10：requires-python 改为 >=3.10，pip install -e spec -e runtime 在 3.10 venv 中成功；排查 3.11+ 依赖：产品代码没有 tomllib/ExceptionGroup/Self/datetime.UTC，只有 test_packaging 用了 tomllib → 改为 tomllib → tomli → 内置小解析器
- 12:15 复现"3.10 下 test_sandbox 卡住"：(1) root + uv 解释器在 /root（0700）下，nobody 执行不了解释器，技能全部起不来；(2) test_skill_process_dies_with_the_runtime 的 stdout.readline() 没有超时，子运行时起不来就永远阻塞 → 改为限时读取（ros2/cli 测试同样处理）；解释器选择增加"同版本系统解释器"回退，/v1/status 新增 sandbox.python_usable，测试在解释器不可用时带原因 skip（可用 NEUXMIND_TEST_SANDBOX_PYTHON 指定）
- 12:20 复现非 root 问题（3.13 非 root：6 失败 1 错误）：根因是 fork 炸弹测试在非 root 下留下 setsid 逃逸进程无人收割（收割者此前只在 root 降权时工作），它们一直 fork，把机器负载拉到 37，后续测试纷纷"运行超时"/卡住——即用户机器上的现象。修复：收割者在同用户模式下也工作（按"同 uid + 不在运行时会话中"识别技能后代，逃逸进程连同其进程组一起杀），运行结束时反复收割直到稳定，监视线程在技能退出后 30 s 内每 0.5 s 再扫一次；在途子进程表改为进程级共享（一个进程内多个运行时互不误杀）
- 12:25 /v1/status 的 sandbox 新增 runtime_user、privilege_drop、level（full / best-effort）、best_effort_reasons；CLI 启动时对非 root 给出警告
- 12:25 §5.0 连接计数根因已查明（实验时间线见交付报告）：不是 stop 驱逐造成——驱逐只发生在停止专用名额内，从不释放普通名额；而是最早占住普通名额的半截连接在请求读取期限（测试中 1.5 s）到期被断开，普通名额随之空出；建 300 个连接本身要 1.3–2 s（监听队列满、SYN 重传），所以测试最后的 GET 有时正好落在"期限到期之后、补满之前/之中"的窗口里 / 下一步：把测试改成在确定的满状态下检查，补单元测试，文档写清"满"是瞬时状态
- 12:40 非 root 根治：收割者改为"先 SIGSTOP 再 SIGKILL"本运行时的全部未跟踪后代（被停住的进程不能再 fork，fork 炸弹追不上收割），不再只看直接重挂的孤儿；fork 炸弹测试新增断言"无 cgroup 时运行结束后不留任何技能进程"。导入守卫测试的可读源码副本同时带上 spec（3.10 venv 中技能解释器看不到 venv 的 site-packages）。三种环境 runtime/tests：3.13 root 180 全过；3.10 root 180 全过；非 root 3.13 / 3.10 各 180 全过（2 个带原因 skip：需要 root 降权的状态目录隔离断言等），无进程残留
- 12:50 §5.0 连接计数：test_stop_within_one_second_under_many_half_open_connections 改为在确定的满状态下检查（等泛洪的普通连接到期断开 → 恰好补满 8 个新的半截连接 → 在其期限内请求，断言 503 <1 s，并补一次满状态下 stop <1 s）；新增 ReservedEvictionTests（驱逐永不改变普通计数、已识别的 stop 连接不被驱逐、只有结束的普通连接释放名额）与"空闲普通连接到期才释放名额"测试。该测试在后台负载下连续 30 次通过 / 下一步 §5.1 model.install
- 13:30 §5.1 model.install 运行时原生实现（新增 neuxmind_runtime/models.py）：--models-dir 启用后运行时把 model.install 加入清单（runtimes 来自配置；适配器自带时以适配器为准；漂移检测仍只比对适配器自己的清单）；https / 配置允许的 file:// 根目录 /（仅测试或开发）配置允许主机的 http；流式下载 + 大小上限（Content-Length 与流式双检查）+ SHA-256 + 暂存目录 fsync 后原子 rename 到 models/<sha256>/（文件 0444、目录 0555）；同 sha 幂等（含并发）；同协议重定向逐跳复检、跨协议（→ file://、→ https）拒绝；签名查询串不入 model.json；cancel 只置事件并 shutdown 套接字，停止路径从不等待安装；safety=system：需 hardware_authorized，锁存期间也可安装、不占运动资源。GET /v2/models、DELETE /v2/models/{id}（需授权；运行中技能曾在 ctx.models 中看到该模型则 409 model_in_use）；ctx.models（进程内与沙箱子进程都有，返回副本）；/v1/models/install 走同一实现。新增 tests/test_models.py 23 个
- 13:45 §5.2 遥测持久化（重写 events.py）：emit 仍无锁无 IO；常驻写线程每 0.1 s 批量落盘到 <state>/telemetry.sqlite3 有界环形（默认 100k，可配 --telemetry-capacity）；seq 跨重启连续（正常关闭无缺口；磁盘上保留 seq 预留量，崩溃丢失未落盘事件时只留缺口、绝不复用客户端见过的 seq）；/v2/telemetry?since 早于内存时读环形存储；新增 GET /v2/runs/{id}/events?since&limit 回放；/v1/status 新增 telemetry。新增 tests/test_telemetry.py 9 个（含"遥测数据库卡住时 stop 仍 <1 s"）
- 14:10 文档：README（§0.1 Python 3.10、§0.2 非 root 尽力而为模式对照表、§2.1 连接计数规则、§5.2 端侧模型安装、§5.3 遥测持久化、API 表、已知限制）、EVOLUTION_ENTRY 第 03 轮已写；版本 0.3.0；CLI 冒烟（普通用户 + 3.10，--models-dir）：status 显示 level=best-effort 与原因、manifest 含 model.install、SIGTERM 正常退出 / 正在 worktree 中做变异测试
- 14:50 变异测试（git worktree scratchpad/runtime-agent/wt，主工作区未被改动，结束后已删除）：32 个新防线变异（连接计数 2、收割者/尽力而为状态/解释器 10、模型安装 14、遥测 6），按需在 root 或普通用户下跑。第一遍 27 杀死；存活 M5（重定向第二层检查被第一层掩盖）、M7（发布前取消检查被下载后检查掩盖）、M13（测试的签名串只在重定向目标里）、S7（收尾只扫一遍：被周期扫描掩盖），M12 模式未匹配。补测试：两层重定向各自单测、直接对 _publish 取消、来源 URL 自带查询串、模型根目录启动即 0555；第二遍 M5/M7/M12/M13 全杀。最终 31/32，S7 记为冗余防线（周期扫描兜底）。T4（emit 同步落盘）以测试挂起超时被杀——说明"写入不进停止路径"有测试守护
- 15:00 最终验证：runtime/tests 223 个——3.13 root OK；3.10 root OK；3.13 普通用户 OK（2 skip）；3.10 普通用户 OK（2 skip）；无 nxtest/nobody 进程残留。连接满测试连续 30/30。spec 11（3.13、3.10）、sdk 48、tests/ 141（23 skip）全过 / 下一步只 add runtime/ 并提交
- 16:00 SDK Agent 的三个发现已修：v2 install 记录目录 model_id/name（/v2/models 返回 sha256 + model_id(目录 ID) + name；DELETE 也接受唯一目录 ID，响应保持 {deleted, model_id: sha}）；model.install_* 事件带 run_id（/v2/runs/{id}/events 可回放进度与失败原因）；原生 model.install 时清单顶层 model_runtimes = params.runtimes。runtime/tests 227：3.13 root OK、nxtest OK（2 skip）；sdk/tests 57 中 2 个按旧语义断言 /v2/models.model_id == sha256，需 SDK 跟随契约改为 sha256

## 第 03 轮审查修复（docs/evolution/reviews/2026-11-08-iter03-review.md）

- 读完审查报告与协作规则第 7、9 条（长命令一律带 timeout、每 20 分钟一行进度）。本轮修 Q3-01（P1）、Q3-02、Q3-05、Q3-06、Q3-08（P2）、G（测试缺陷），以及能修的 runtime P3：Q3-09（内网地址、摘要预言机）、Q3-10（下载总时限）、Q3-11（墙钟超时）、Q3-12（输入校验）、Q3-13（失败事件）、Q3-17（同一状态目录两个实例）；Q3-14 属已声明的尽力而为，只写文档 / 先做遥测（Q3-01/02/08/17）
- 遥测完成：打开失败（损坏/同名目录/预留越界/被锁/同一状态目录第二个实例）一律降级为内存模式或改名隔离后重建（不删除），/v1/status 新增 telemetry_degraded、telemetry.degraded_reason/quarantined/start；打开时锁等待 0.1 s、其余 0.5 s；status 只读缓存值；写失败的批次保留重试、预留先于写入推进、非正常关闭后 seq 从 max(库, 预留, 墙钟微秒) 继续；新增 8 个测试（含审查的 10011–15010 场景与 R5 场景）；test_async_evaluate 的 since(0)[0] 缺陷已改 / 下一步 Q3-05 收割者
- 模型安装审查项完成：Q3-06 同 sha 在途合并为同一运行（技能 ctx.call 路径跟随同一下载）、全局并发上限 max_concurrent=2（超出 409 install_busy）、下载前与每 8 MiB 检查可用空间（保留 256 MiB）；Q3-09 https 默认拒绝内网/本机/链路本地/元数据地址（解析后判断，allow_private_hosts 放行），错误信息不再返回实际摘要；Q3-10 下载总时限；Q3-11 system 能力按墙钟超时；Q3-12 fullmatch 与控制/双向覆盖字符；Q3-13 任何失败都有 model.install_failed（磁盘满映射为中文原因）；R3 提前断开的短读测试。Q3-05 收割者只认带技能标记或技能会话的后代（真实进程测试：运行时以新会话启动的辅助进程存活且退出状态未被拿走）/ 下一步三环境全量 + 变异
- 变异测试（git worktree scratchpad/runtime-agent/wt2，主工作区未改动，已删除）：新防线 22 个（遥测 10、收割者 2、安装 10）。第一遍 E1 写法本身语法无效、E4（启动锁等待放大到 5 s）存活——查明启动时第一次写入前已把等待调回 0.5 s，启动锁等待实际是 0.5 s 而非 0.1 s；已改为启动期全部写入都用 0.1 s，E1 重写后两者均被杀。最终 20/22 杀死；E2（OperationalError 时改名隔离）与 M10（技能路径的第一处跟随分支）被前置检查/第二处分支覆盖，属冗余防线。审查存活的 R5 对应本轮 E7，已杀死
- 最终验证：runtime/tests 245 个——3.13 root OK、3.10 root OK、nxtest 3.13 OK（2 skip）、nxtest 3.10 OK（2 skip），无残留进程；sdk/tests 59 OK / 提交（只 add runtime/）

## 第 04 轮（§6.1 一致性测试套件、§6.2 适配器脚手架、第 03 轮遗留）

- 17:27 读完协作协议（第 7、9 条）、INTERFACES_V1 §1/§5/§6、CAPABILITIES_V1、spec、iter03 遗留、hal/bus/safety/core/runner/gateway/cli、三个适配器、test_packaging / 设计：一致性套件在 EdgeRuntime 之上执行（适配器实例包一层临时运行时；运行中运行时在进程内执行；--url 默认让远端运行时自测，可选黑盒客户端模式），dry 只发零值指令与被安全监督器拒绝的指令 / 下一步写 conformance.py
- 17:39 conformance.py 完成（static/dry/motion、四种目标、ConformanceRuns）；core/gateway 接入 POST/GET /v2/conformance；model.install 进度修复（capability 运行的 progress 跟随动作，progress_detail.feedback 带 bytes/total）；ROS 2 适配器抽出 Ros2Bridge（私有 context）+ imu/joint_state/amcl 转换；tests/ros2_double（rclpy 替身 + 假机器人）。试跑：两个仿真本体与 ROS 2 替身三级全部通过 / 下一步 CLI 与脚手架
- 17:43 scaffold.py + CLI（conformance / scaffold）完成；ROS 2 映射生成的适配器在替身上 static/dry/motion 全过、生成的 tests 通过；版本 0.4.0 / 下一步写测试（一致性、脚手架、ROS 2 替身、HTTP、进度、test_packaging）
- 17:51 测试：test_conformance 39（两个仿真本体三级、每项判定的故障本体/故障视图、ROS 2 替身、HTTP、CLI）、test_scaffold 11、test_models 进度用例、test_packaging 可穿越临时目录（已用 0700 父目录 TMPDIR 复现旧失败并验证修复）/ 下一步全量三环境
- 17:59 3.13 root 全量 297：1 个错误（Ros2Adapter 重构后缺 rclpy 时先报限值错误而非 ROS 2 缺失）→ 已恢复先检查 ROS 2；补 6 个判定用例（看门狗过慢、无反馈运动、导航谎报到达、零速度被拒、停止代数两层分别、非仿真报告）；README §3/§3.1/§3.2/API/已知限制 / 后台跑四环境全量（脚本 scratchpad/runtime04/run_all.sh）
- 18:01 EVOLUTION_ENTRY 第 04 轮初稿；变异脚本 scratchpad/runtime04/mutate04.py（40 个，conformance 判定逻辑）待四环境全量结束后在 worktree 中运行
- 18:13 四环境全量：3.13 root 302（启动时尚未加最后 1 个用例）OK、3.10 root 303 OK、nxtest 3.13 303 OK（2 skip）、nxtest 3.10 303 OK（2 skip）；无 nxtest/nobody 残留进程 / 下一步 worktree 变异
- 18:15 变异第 1 遍 35/40；查明存活的 M01/M11 是测试的故障本体按套件阈值构造（阈值放宽时故障也随之放宽）→ 改为按契约常量 1.0 s 构造并断言报告阈值；M34 存活是变异脚本缺陷（同秒同尺寸写入命中旧 .pyc），脚本改为每次删 __pycache__ 并 -B；重跑全部 40 个；sdk/tests 59、spec/tests OK
- 18:21 M40 补用例杀死；变异最终 39/40（M06 冗余防线）；worktree 已删除；EVOLUTION_ENTRY 完成 / 提交（只 add runtime/）
- 19:07 总指挥后续三项：报告 manifest_digest（采用云端 catalog.manifest_digest 算法）、model.install 与 legged.posture 的 dry 用例、time_scale 10/20 下 motion 稳健 / 开始
- 19:16 三项完成：manifest_digest/manifest_v1_digest、dry 用例 model.install/posture、倍速稳健（60 次 motion 0 失败）；变异 9/9；README/EVOLUTION 补充 / 下一步 3.13 root + nxtest 全量、sdk
- 19:25 全量：3.13 root 307 OK、nxtest 3.13 307 OK（2 skip）、无残留进程；sdk/tests 81 中 2 个失败，均为按旧行为断言“legged.posture 在 dry 级为不适用/不可用”（test_adapters_conformance 第 237、384 行），与本次要求的变更相符，需 SDK Agent 更新断言 / 提交
- 19:58 第 04 轮审查修复开始：读 reviews/2026-11-09-iter04-review.md 与 scratchpad/review04/
- 20:02 进行中：Target 增 stop_count/epoch/stale_call/条件复位/dry 守卫/受信仿真识别；Suite 录制改为任何失败在 motion 中停车+锁存+中止、停止计数判归属、复位后持续静止；运行时：supervisor.stop_count、reset(expect_stop_count)、/v1/status stop_count/stop_epoch、RealClock.set_time_scale、SimArm 加 gripper.state、spec_version 1.1
- 20:08 conformance 动态部分重写完成（停止计数判归属+条件复位、dry 守卫、关节名映射、无定位导航 blocked、受信仿真按 1× 执行、速度方向幅度核对、持续静止/复位后静止/结束静止、gripper.state/arm.ee_state、黑盒不发依赖拒绝的探测、运动租约、trust 字段）；SimQuadruped 加 localization.pose；两仿真三级通过 / 下一步用审查脚本复验
- 20:11 复验：operator_stop dry/motion → aborted、final_latched=true、操作员停止后 0 次 reset；dry_audit → ArmJointStateExtra 不再发任何机械臂指令（blocked）、两仿真只有零值调用；midfault（适配为 0.1 rad 中断目标）→ 中止并锁存；frame_offset → 撒谎的定位下 0.26 m 即中止锁存；scaffold_inject 9 个注入全部拒绝、无残留文件、无执行
- 20:24 新增 tests/test_conformance_review.py（审查 23 种场景中可回归的部分 + 新机制单测）；更新 test_conformance 期望（测试前锁存全部 blocked、黑盒模式 blocked 集合、ROS 2 无定位导航 blocked、倍速按 1× 执行）/ 跑测试
- 20:38 修正：两个测试替身命名冲突（SimAdapterBase 的 _t/_last）；黑盒 posture N/A 判定顺序；ROS 2 替身发布 /amcl_pose（生成的适配器带 localization.pose，三级全过）；test_conformance/test_scaffold/test_conformance_review 全过 / 下一步变异 + 四环境
- 20:40 README §3.1 安全规则重写（7 条 + 信任边界）、§3.2 输入白名单与原子生成、API 表、已知限制；变异 F01–F40 在 worktree 后台运行中
- 20:53 变异 F01–F40（新防线）：第 1 遍 34/40；F16/F17 补报告字段 time_scale_rebased 与断言，F21 补“停止后延迟恢复”用例，F39 发现脚手架停止注释与 docstring 仍原样嵌入能力 ID → 改 repr；重跑后 38/40，F02（外来锁存检查）与 F09（dof 不等检查）分别被停止计数与关节名数量检查覆盖，属冗余防线；worktree 已删除 / 下一步四环境全量 + sdk
- 21:04 EVOLUTION_ENTRY 第 04 轮审查修复已写；四环境全量运行中（单环境约 15 min）
- 21:06 3.13 root 336：1 失败 test_manifest 固定了仿真本体能力集合（本轮有意新增 localization.pose / gripper.state）→ 更新期望集合（仍是精确断言）；其余环境继续
- 21:16 等待 3.10 root / nxtest 全量
- 21:18 3.10 root 336：同一个 test_manifest 失败（该环境启动早于修正）；修正后 test_manifest 在 3.13/3.10 root 单跑通过
- 21:31 nxtest 3.13：336 OK（2 skip）
- 21:47 全量结果：nxtest 3.13 336 OK（2 skip）、nxtest 3.10 336 OK（2 skip）、3.13/3.10 root 336 中唯一失败为 test_manifest（运行早于其更新，更新后单跑通过）；sdk/tests 84 OK；下游抽查 cloud test_conformance 19、tests/ test_sim_evidence_and_reset 10、test_adaptation 31 通过；无 nxtest/nobody 残留 / 提交
- 22:18 iter04-fix2 开始：读复审、复制复现脚本到 scratchpad/fix2
- 22:22 spec joint_names、SimArm 声明、safety R4-03、bus 动作块按名映射 已改；下一步 conformance.py
- 22:27 conformance 改完首轮；sim 四组全绿；跑复现脚本
- 22:34 新增 test_conformance_rereview04.py，后台运行（bgtm8qf7u）；改 README §3.1
- 22:37 全量测试启动（scratchpad/fix2/run_full.sh，PID 记录于 full.pids）
- 22:38 spec 12 OK；等 runtime/cloud/sdk/tests；已写 EVOLUTION_ENTRY
- 22:40 spec 12 / cloud 211 / sdk 84 / tests 184(39 skip) OK；等 runtime
- 22:56 修复审查用例措辞/夹具；worktree 变异检查开始（scratchpad/fix2/wt）
- 23:07 变异 M1/M3/M4 被杀，M2 冗余；续跑 M5-M9
- 23:13 变异 9 个：7 杀 + 2 冗余(M2/M9)，M6 冗余；补 StopResumeBounded；worktree 已删；全量复跑
- 23:23 全量复跑进行中
- 23:34 全量复跑全绿（spec 12 / runtime 358 / cloud 211 / sdk 84 / tests 184 skip 39），复现脚本复验完成；提交
2026-11-09T00:01:04-08:00 fix3 runtime agent: worktree /tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/fix3-wt created, starting R4b-01..03
2026-11-09T00:06:21-08:00 fix3: R4b-01..05/08 code patched (safety/bus/core/gateway/conformance); running repros
2026-11-09T00:11:40-08:00 fix3: new tests 14 OK; full suites started (logs fix3-logs/)
2026-11-09T00:15:14-08:00 fix3: all edits done (README/EVOLUTION_ENTRY updated); full suites run 2 started
2026-11-09T00:17:40-08:00 fix3: waiting on runtime/sdk suites; spec/cloud/tests OK
2026-11-09T00:40:00-08:00 fix3: runtime suite run2 had 3 test-only failures (assertion text/fixture), fixed; rerunning runtime/tests
2026-11-09T00:43:01-08:00 fix3: final repro grids done (arm 63: dry 0 arm cmds/0 rad; reset_hang 11+RT15: 0 unlatch/0 m); waiting runtime run3
2026-11-09T01:35:25-08:00 iter05 runtime agent: worktree /tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/iter05-rt-wt created (branch iter05-runtime), starting R4b-04 guard / R4c-01/02/03/05
2026-11-09T01:42:49-08:00 iter05 runtime: safety/core/gateway/bus patched, latch_guard.py added, tests/test_iter05_runtime.py written; running
2026-11-09T01:47:11-08:00 iter05 runtime: README/EVOLUTION_ENTRY updated; full suites running (logs scratchpad/iter05-rt-logs); rr5 repro scripts prepared
2026-11-09T01:47:23-08:00 iter05 runtime: waiting on runtime/tests full run
2026-11-09T02:07:35-08:00 iter05 runtime: runtime/tests still running (~20+ min expected)
2026-11-09T02:16:39-08:00 iter05 runtime: full run1 spec12/runtime395(1F: suite blind to guard restops -> conformance reads guard seen; rereview04* 35 OK)/cloud211/sdk84/tests OK; running rr5 grids
2026-11-09T02:19:46-08:00 iter05 runtime: rr5 done (every6 12s 0.014m, every9 0.024m, op_reset 409+disk/mem agree); full run2 started
2026-11-09T02:29:53-08:00 iter05 runtime: full run2 in progress (spec OK)
2026-11-09T02:43:20-08:00 iter05 runtime: run2 runtime/tests 395 OK; waiting cloud/sdk/tests
2026-11-09T02:48:58-08:00 iter05 runtime: full run2 all green (spec 12 / runtime 395 / cloud 211 / sdk 84 / tests OK skip 39); committing
2026-11-09T04:21:10-08:00 iter06 runtime: started in worktree iter06-runtime; reading review + latch_guard/skills
2026-11-09T04:28:35-08:00 iter06 runtime: Q5-01/02/03/05/06/07/08 implemented (latch_guard drift+invalid feedback, param constraints, reset hint); verify06.py green on first pass; next: unit tests + full suites
2026-11-09T04:32:07-08:00 iter06 runtime: test_iter06_runtime.py 23 OK; starting full suites run1
2026-11-09T04:33:26-08:00 iter06 runtime: README/EVOLUTION_ENTRY drafted; full run1 in progress (spec OK)
2026-11-09T04:33:40-08:00 iter06 runtime: full run1 waiting on runtime/tests
2026-11-09T04:43:46-08:00 iter06 runtime: full run1 still in runtime/tests
2026-11-09T05:01:18-08:00 iter06 runtime: run1 runtime/tests 1F (test_delayed_resume_after_stop, pre-existing race also on master 1/4) -> conformance _hold_still reads guard seen; 6/6 OK; cloud/sdk OK; waiting tests/ then run2
2026-11-09T05:01:46-08:00 iter06 runtime: run1 = spec 12 OK / runtime 418 1F / cloud 211 OK / sdk 84 OK / tests 213 OK(skip39); run2 started after conformance fix
2026-11-09T05:11:54-08:00 iter06 runtime: run2 runtime/tests in progress
2026-11-09T05:17:40-08:00 iter06 runtime: aligned param error body with desktop contract (param/min/max/choices/message, param_choice/required/type); run3 full suites starting
2026-11-09T05:27:46-08:00 iter06 runtime: run3 runtime/tests in progress
2026-11-09T05:41:30-08:00 iter06 runtime: run3 runtime/tests 419 OK; waiting cloud/sdk/tests
2026-11-09T05:47:11-08:00 iter06 runtime: run3 all green (spec 12 / runtime 419 / cloud 211 / sdk 84 / tests 213 skip39); committing
2026-11-09T06:26:32-08:00 iter07 runtime: worktree created at v0.13.0, reading review/guard; starting Q6-01 guard rework
2026-11-09T06:45:40-08:00 iter07 runtime: Q6-01/02/04/05/06/08 implemented + tests (test_iter07_runtime 20 OK, spec 15 OK); noise 0/h all cases; full suites run1 started
2026-11-09T06:48:01-08:00 iter07 runtime: guard rework (robust stats, rebase, aggregation, escalation, calibration) — iter05/06 tests green; noise 0/h
2026-11-09T06:49:14-08:00 iter07 runtime: run1 aborted (runtime _paths preferred installed master spec in subprocesses; fixed), run2 full suites starting
2026-11-09T07:08:31-08:00 iter07 runtime: run2 runtime/tests in progress
2026-11-09T07:10:02-08:00 iter07 runtime: run2 runtime/tests still running, 0 failures so far
2026-11-09T07:13:24-08:00 iter07 runtime: run2 runtime/tests 439 OK; cloud/sdk/tests running
2026-11-09T07:21:43-08:00 iter07 runtime: run2 spec15/runtime439/cloud211/sdk84 OK, tests 1 fail (new json_too_deep code unknown to desktop table) -> use bad_request; applied commander contract (stop_not_obeyed bool/_at, event message/restops/displacement_m); run3 full suites starting
2026-11-09T07:31:48-08:00 iter07 runtime: run3 runtime/tests in progress
2026-11-09T07:41:42-08:00 iter07 runtime: run3 runtime/tests in progress
2026-11-09T07:51:07-08:00 iter07 runtime: run3 all green (spec 15 / runtime 439 / cloud 211 / sdk 84 / tests 226 skip39); committing
2026-11-09T08:28:34-08:00 iter07-fix runtime: start (worktree iter07-fix-runtime @0df6555); reading review, designing Q7-01/02 guard changes
2026-11-09T08:36:38-08:00 iter07-fix runtime: Q7-01/02/05/06/07/08/10 implemented; re-measuring adv/noise sets (r9fix/)
2026-11-09T08:40:06-08:00 iter07-fix runtime: new tests test_iter07_fix_runtime 13 OK; final re-measure running (r9fix/all3, noise3)
2026-11-09T08:44:59-08:00 iter07-fix runtime: docs (README, docstring, CLI) updated; full suites run1 started; adv all3 finishing
2026-11-09T08:47:39-08:00 iter07-fix runtime: Q7-10 expire_s added; suites run2 running; v013/pre comparison runs for new scenarios
2026-11-09T08:48:32-08:00 iter07-fix runtime: EVOLUTION_ENTRY written; waiting on suites run2 (runtime/tests)
2026-11-09T09:08:22-08:00 iter07-fix runtime: suites run2 runtime/tests still running
2026-11-09T09:17:19-08:00 iter07-fix runtime: run2 all green; aligned relaxed_keys/event with desktop 5ccbd26; run3 full suites started
2026-11-09T09:27:35-08:00 iter07-fix runtime: run3 runtime/tests in progress
2026-11-09T09:37:28-08:00 iter07-fix runtime: run3 runtime/tests done: 
2026-11-09T09:37:32-08:00 iter07-fix runtime: run3 runtime/tests still in progress
2026-11-09T09:46:59-08:00 iter07-fix runtime: run3 all green (spec15/runtime453/cloud211/sdk84/tests246 skip39); committing
2026-11-09T10:30:35-08:00 iter07-fix2 runtime: worktree iter07-fix2-runtime @8c891d3; A/B baselines v0.13.0/aab8a26 running; designing Q7b-01/02/03
2026-11-09T10:44:00-08:00 iter07-fix2 runtime: guard redesign (yaw cap 0.05 default, permanent displaced + raw sample, position/joints from snapshot, walk consistency/trend/hold, displacement escalation); cand2 full A/B running
2026-11-09T10:52:19-08:00 iter07-fix2 runtime: added silent precautionary stop (v0.13.0 shadow rule); cand3 full A/B running
2026-11-09T11:06:27-08:00 iter07-fix2 runtime: cand4 (velocity gate + noise caps 3mm/2mrad) full A/B running
2026-11-09T11:19:03-08:00 iter07-fix2 runtime: cand5 (jump rule before noise known) full A/B running; unit suites to rerun on final
2026-11-09T11:34:15-08:00 iter07-fix2 runtime: cand6 (raw snapshot w/o filter delay) full A/B + full suites run2 running
2026-11-09T11:56:47-08:00 iter07-fix2 runtime: cand6 A/B done (only alarm-cadence cases remain vs v0.13.0); full suites run2 in progress
2026-11-09T12:05:15-08:00 iter07-fix2 runtime: full suites green (spec15/runtime466/cloud211/sdk84/tests265); committing
2026-11-09T12:09:05-08:00 iter07-fix2 runtime: drift notice + spec reset_expected_s validation; rerunning spec/runtime/tests suites
2026-11-09T12:29:00-08:00 iter07-fix2 runtime: run3 runtime/tests in progress
2026-11-09T12:36:06-08:00 iter07-fix2 runtime: run3 green (spec17/runtime470/tests265 skip39); committing
2026-11-09T22:16:59Z iter07-fix3 start: worktree created on d498d79
2026-11-09T22:30:23Z iter07-fix3 runtime: V013Replica precaution + notice latch id + reset_expected_s (0,25]; extra2 24/24 ok; running full A/B
2026-11-09T22:33:40Z iter07-fix3 runtime: regression tests added (fail on d498d79, pass now); full A/B running
2026-11-09T22:34:32Z iter07-fix3 runtime: A/B extra running, full suites running in background
2026-11-09T22:54:25Z iter07-fix3 runtime: full A/B done (extra2 24/24 ok; extra 42 ok + 4 non-regressions; adv/adv2 as final review); suites running
2026-11-09T23:05:26Z iter07-fix3 runtime: runtime suite 1 failure (NoisyStillBody creep: precautions now hold the driver at 0 m); test extended; rerunning
2026-11-09T23:16:07Z iter07-fix3 runtime: spec/cloud/sdk/tests green; runtime suite rerun in progress
2026-11-09T23:31:34Z iter07-fix3 runtime: all suites green (spec17/runtime477/cloud211/sdk84/tests283 skip39); final commit
2026-11-10T00:10:25Z iter07-fix4 runtime: start (Q7d-01/02 unified stop() cadence); worktree wt-fix4 branch iter07-fix4-runtime
2026-11-10T00:19:14Z iter07-fix4 runtime: unified stop() cadence implemented (variants under A/B: merge window W 0.5/1/2/inf); v0.13.0 A/B baseline done
2026-11-10T00:32:37Z iter07-fix4 runtime: A/B W=1.0/2.0 running (adv/adv2 all ok excl. maxcal non-comparable); fix3 regression passes for W>=1
2026-11-10T00:54:31Z iter07-fix4 runtime: W=1/2 variants leave 0.1-2% WORSE rows; testing 'counted re-stop calls stop() only with replica evidence' variant
2026-11-10T01:05:53Z iter07-fix4 runtime: chosen: counted re-stops call stop() only on the replica's beat; A/B all ok; WIP commit; final A/B + suites next
2026-11-10T01:21:51Z iter07-fix4 runtime: final A/B 210/210 ok, max 2 stop()/s, min gap 0.5 s; docs written; full suites running
2026-11-10T01:41:55Z iter07-fix4 runtime: runtime suite running
2026-11-10T01:52:40Z iter07-fix4 runtime: all suites green (spec17/runtime482/cloud211/sdk84/tests283 skip39); final commit
2026-11-09T18:58:23-08:00 fix5: worktree created, latch_guard Q7e-01/02 edits in progress
2026-11-09T19:03:11-08:00 fix5: Q7e-01/02 code + tests committed (bfa9ac4), HAL/README docs (Q7e-04); A/B v013 vs mine running in scratchpad/fix5ab
2026-11-09T19:15:26-08:00 fix5: A/B adv6 mine 155/263, runtime suite running
2026-11-09T19:34:56-08:00 fix5: A/B adv6/adv3-5/extra/extra2 done (no WORSE), adv2/adv running; spec/runtime/cloud suites OK
2026-11-09T19:46:33-08:00 fix5: done — A/B 472 comparable no WORSE vs v0.13.0; all suites OK; final commit
