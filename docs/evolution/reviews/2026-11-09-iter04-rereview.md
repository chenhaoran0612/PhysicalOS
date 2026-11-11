# 第 04 轮复审：一致性测试套件修复（ea81a96 / caba7ef）

- 复审对象：`d4e121f`（含 runtime 修复 `ea81a96`、SDK 修复 `caba7ef`、cloud `eec83dd`、desktop `a75dc6e`）
- 复审方式：只做实验，不采信修复报告。所有"撒谎适配器"、竞态注入、脚手架注入都在审查 worktree 中、以 `SimQuadruped`/`SimArm` 子类进行；物理位移取自仿真内部状态（`_step` 积分），**不取自适配器上报的读数**。
- 脚本目录：`/tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/rereview04/`（下文简称 `rr/`）。`rr_common.py` 的 `truth()` 记录：到达适配器的每个调用（在 dry 守卫之后）、物理路径长度、最大偏离、dry 期间路径、指定时刻之后的路径。
- 所有长命令带 `timeout`；后台进程均为本会话启动并已结束（复审结束时无残留 `liars2/op_stop/race/unittest` 进程）。

## 结论

**不可发布 0.11.0。** 阻断项：

| ID | 严重度 | 一句话 |
|---|---|---|
| R4-01 | **P0** | Q4-02 未修完：`joint.state` 顺序被当成机械臂指令顺序。诚实本体只要按字母序/URDF 序发布关节，dry 级（未授权）就让肩关节、肘关节各转 **1.57 rad**，dry 守卫一次也没拦（`violations: []`） |
| R4-02 | P1 | 套件在**已锁存**时发现本体又动了，只中止、不再停车；本体在测试结束后继续以 0.1 m/s 行驶（5 s 内 0.5 m，且不会停），报告却写"已停车并保持锁存"、`final_latched: true` |
| R4-03 | P1 | 运行时复位竞态：操作员在适配器 `reset()` 执行期间按停，监督器重新锁存，但适配器的复位在 `stop()` 之后才完成。驱动若在复位时恢复上一条指令（Q4-07 认定的真实缺陷类），本体在"操作员急停、锁存中"的状态下继续行驶 |

若要先发不含 motion 级的版本：R4-01 是 dry 级问题，仍须先处理（最小修复：dry 级不向适配器发送任何机械臂/关节动作块目标，见 R4-01 修复建议）；R4-03 在 dry 级同样存在（dry 级四足 3 次、机械臂 2 次条件复位；腿足本体的复位本身就是起身动作）。

---

## 1. 全量测试

命令（worktree 根目录，五个模块并行，每个 `timeout 1500`）：

```
PYTHONPATH=$PWD/spec:$PWD/runtime:$PWD/cloud:$PWD/sdk:$PWD python3 -B -m unittest discover -s <t>
```

| 模块 | Python 3.13 结果 |
|---|---|
| `spec/tests` | 11 OK |
| `runtime/tests` | 336 OK（748 s） |
| `cloud/tests` | 211 OK（Q4-08 的 4 个失败已消失） |
| `sdk/tests` | 84 OK |
| `tests` | 184 OK（skipped 39） |

另在 Python 3.10.20（`/root/.local/bin/python3.10`）上跑 `runtime/tests/test_conformance*.py`：78 OK（593 s）；`test_scaffold.py`：11 OK。

全绿，但见 §5：现有用例没有覆盖 R4-01、R4-02、R4-03。

---

## 2. 原 P0/P1 逐项复验

| 原编号 | 复验结论 | 关键数字 |
|---|---|---|
| Q4-01 操作员急停被套件复位 | **已修复（复现后确认）**；另见 R4-03（运行时层面的复位竞态） | 网格 48 例中 47 例在套件运行期间按停：**操作员停止后 0 次 `reset`、0 条运动调用、物理位移 0**，结束时 47/47 `final_latched=true`、`latch_reason="operator: person in path"`、`aborted` 非空。4 类定点竞态同样 0 次复位 |
| Q4-02 dry 级让机械臂运动 | **部分修复** → R4-01（P0） | 原用例（夹爪关节排第一、`joint.state.dof=7`）：blocked，到达适配器的机械臂目标 0 个，位移 0；但同 dof、不同顺序的诚实本体：dry 位移 0.2 / 0.7 / **1.57 rad** |
| Q4-03 中途异常不停车 | **已修复** | `arm_read_raises`：中止、锁存，最大关节偏离 0.05 rad（只有 min_move 的步长；修复前 0.4 rad） |
| Q4-04 无定位时按 odom 发 map 目标 | **已修复** | 无 `localization.pose`：导航目标 0 个，三个导航用例 blocked；定位坐标系错误（报 odom 当 map，偏移 (4,3) m）：行驶到 0.25 m 被判失败、停车、中止，最大偏离 0.227 m（修复前 4.87 m） |
| Q4-05 倍速放大真机目标 | **已修复** | 非仿真本体时钟自称 20×：报告 `time_scale=20.0`、`executed_time_scale=1.0`，目标为当前位置前方 0.1/0.2 m（绝对坐标 x=0.35/0.52/0.53）；只在 `status()` 自报 20× 时同样为 1× |
| Q4-06 速度 ×5 / 反向仍通过 | **已修复（vx）**；wz、vy 未覆盖 → R4-04（P2） | ×5：峰值 0.500 m/s 判失败、中止、锁存，物理路径 0.152 m；反向：判失败，0.031 m |
| Q4-07 延迟恢复 / 复位重放 | **部分修复** → R4-02（P1） | 7 种变体全部被检测到并中止；但其中 2 种在测试结束后本体仍在动（停止后 1.3 s 恢复：测试结束后 5 s 内又走 0.50 m 且不停；取消后 2.5 s 恢复：结束后又走 0.17 m） |
| Q4-08 cloud 测试红 | **已修复** | cloud 211 OK |
| Q4-09 脚手架注入 | **已修复** | 26 个注入探针：20 个被拒绝且**不留任何文件、不留临时目录**；6 个接受的输入均无害；标记文件 0 个（无代码执行）。两个 P3 见 R4-07、R4-08 |

### 2.1 Q4-01 详细数据

命令：

```
cd rr && bash sweep_op.sh          # = 对每个 (本体, 级别, 时刻) 运行
python3 -B op_stop.py <quad|arm> <dry|motion> <按停时刻 s>
```

做法：`AdapterTarget` 的临时运行时一建好，操作员线程在给定时刻调用 `runtime.emergency_stop("operator: person in path", source="api")`。时刻网格：dry 0–5 s 共 12 个点，motion 0.5–17 s 共 12 个点，四足与机械臂各一遍。基线时长：四足 dry 5.7 s / motion 19.3 s，机械臂 dry 4.5 s / motion 18.0 s。

覆盖到的阶段（按停时套件正在执行的用例）：`idempotent_latched`、`latch_blocks_motion`、`stop_epoch`、`zero_setpoint`、`refused_without_motion`、`final_still`（dry）；`watchdog`、`velocity.stop_latency`、`navigate_to.min_goal/cancel/stop_latency`、`arm.min_move/cancel/stop_latency`、`gripper.cancel/stop_latency`、`final_still`（motion）。

| 统计 | 结果 |
|---|---|
| 按停发生在套件运行期间 | 47 / 48（机械臂 dry 5.0 s 那一例套件已结束） |
| 操作员停止之后的 `adapter.reset()` 次数 | **全部 0** |
| 操作员停止之后到达适配器的非零运动调用 | **全部 0** |
| 操作员停止之后的物理位移 | **全部 0.0** |
| 结束时锁存 / 原因 | 47/47 锁存，原因均为 `operator: person in path` |

定点竞态（`python3 -B race.py <mode> <第 n 次>`）：

| 注入点 | 结果 |
|---|---|
| 套件第 1、4 次 `stop()` 返回后、计数核对前 | 计数 2≠1、5≠4 → 中止，0 次复位，锁存，原因 = 操作员 |
| `_check_external()` 之后、条件复位之前（第 4 次 reset） | 运行时返回 409 `stop_count_mismatch` → 中止，锁存 |
| 适配器 `reset()` 执行中（第 1、3 次） | 监督器重新锁存（`estop_latched`）→ 中止，锁存，原因 = 操作员 |
| 适配器 `reset()` 执行中 + 驱动复位时恢复上一条指令（第 4 次） | **套件判定正确，但本体在锁存状态下继续行驶**，见 R4-03 |

server 模式（`python3 -B server_lease.py normal`，`POST /v2/conformance` 等价调用）：dry 运行中 1.5 s 按停 → 套件中止（"停止计数 4，套件预期 3"）、`final_latched=true`；运行中操作员 5 次零速度/复位尝试均得 409 `conformance_running`（计入 `external_motion_refused=5`）；套件结束后，操作员在下一次轮询（≤0.5 s）即复位成功（租约已释放）。

### 2.2 Q4-02 详细数据

命令：`python3 -B liars2.py <场景>`（默认 dry 级、不授权），`python3 -B ur_pose.py`。

| 场景 | joint.state 的样子 | 到达适配器的机械臂目标 | dry 期间物理位移 | 守卫 violations | 判定 |
|---|---|---|---|---|---|
| `arm_extra_first`（原 Q4-02 本体） | 7 个关节，夹爪排第一，声明 dof 7 | 无 | 0 | [] | blocked（正确） |
| `arm_extra_first_doflie` | 同上，但声明 dof 6 | 无 | 0 | [] | read 失败 + blocked（正确） |
| `arm_permuted_alpha`（诚实本体） | 恰好 6 个机械臂关节，按名称字母序（elbow, shoulder_lift, shoulder_pan, wrist_1..3） | `[0.5,-0.4,0.3,-0.2,0.1,0.0]`（真实 `[0.3,-0.4,0.5,…]`） | **0.2 rad**（路径 0.4 rad） | **[]** | zero_displacement failed（事后） |
| `arm_permuted_reversed`（诚实本体） | 6 个关节，倒序 | `[0.0,0.1,-0.2,0.5,-0.4,0.3]` | **0.7 rad**（路径 3.0 rad） | **[]** | failed（事后） |
| `arm_names_flip`（诚实本体） | 前 3 次按指令序，之后驱动重新订阅、交换前两个关节 | `[-0.4,0.3,0.5,…]` | **0.7 rad** | **[]** | failed（事后） |
| `ur_pose.py`（同 `arm_permuted_alpha`，UR 常见初始位姿 `[0,-1.57,1.57,-1.57,-1.57,0]`） | 字母序 | `[1.57,-1.57,0.0,-1.57,-1.57,0.0]` | **1.57 rad**（肩关节 0→1.57，肘关节 1.57→0） | **[]** | failed（事后），`hardware_authorized=false` |

详见 R4-01。

### 2.3 Q4-03 ～ Q4-07 的撒谎适配器结果

命令：`cd rr && bash run_liars.sh`（= 对每个场景 `timeout 200 python3 -B liars2.py <场景>`，motion 级 `authorized=True`）。"结束后 3 s"是套件返回后再推进仿真 3 s 的物理路径。

| 场景（适配器行为） | 判定 | 中止 / 结束时锁存 | 物理路径 | 结束后 3 s | 评价 |
|---|---|---|---|---|---|
| `velocity_x5`（实际 ×5） | motion 失败（峰值 0.500 m/s） | 是 / 是 | 0.152 m | 0 | 修复有效 |
| `velocity_reversed`（vx 反向） | motion 失败（速度反向） | 是 / 是 | 0.031 m | 0 | 修复有效 |
| `velocity_x1.4` | **通过** | 否 | 0.136 m | 0 | 在 ±50 % 阈值内，按设计 |
| `wz_reversed_x5`（角速度反向且 ×5） | **通过** | 否 | 0.119 m | 0 | **R4-04**：从不下发 wz |
| `vy_leak`（全向底盘 vx 混入 0.4·vx 的 vy） | **通过** | 否 | 0.121 m | 0 | **R4-04**：侧向容差 0.5·前向 |
| `nav_cancel_resume_1.3` | 失败（取消后 1.32 s 又动） | 是 / 是 | 0.140 m | 0 | 有效 |
| `nav_cancel_resume_2.5` | 失败（在下一个用例中发现） | 是 / 是 | 0.298 m | **0.171 m** | **R4-02** |
| `stop_resume_velocity_1.3`（停止 1.3 s 后恢复上一条速度） | 失败（停止后 1.32 s 又动） | 是 / 是 | 0.351 m | **0.296 m** | **R4-02** |
| `reset_replay_0`（复位立即重放速度） | 失败（复位后 0.03 s 又动） | 是 / 是 | 0.055 m | 0 | 有效 |
| `reset_replay_1.5` | 失败（final_still 中发现） | 是 / 是 | 0.127 m | 0 | 有效 |
| `reset_replay_2.5` | 失败（到达后又动） | 是 / 是 | 0.126 m | 0 | 有效 |
| `reset_replay_nav`（复位重放旧导航目标） | 失败（复位后 0.03 s） | 是 / 是 | 0.140 m | 0 | 有效 |
| `arm_cancel_resume` | 失败（取消后 1.32 s） | 是 / 是 | 最大偏离 0.05 rad | 0 | 有效 |
| `arm_read_raises`（动作中 joint.state 抛异常） | 失败（adapter_fault） | 是 / 是 | 最大偏离 0.05 rad | 0 | Q4-03 有效 |
| `arm_succeed_no_move` | 失败 | 是 / 是 | 0 | 0 | 有效 |
| `gripper_fake_result`（Q4-11） | 失败（gripper.state 1.0≠0.9） | 是 / 是 | — | 0 | 有效 |
| `no_localization`（map=odom+(4,3)） | motion 未通过（导航 3 例 blocked） | 否 / 否 | 0.048 m（只有速度用例） | 0 | Q4-04 有效 |
| `localization_wrong_frame` | 失败（行驶 0.25 m） | 是 / 是 | 最大偏离 0.227 m | 0 | 有效（有上限） |
| `fake_time_scale_real` / `fake_time_scale_status` / `sim_subclass_fast` | 通过 | 否 | 0.33 / 0.12 / 0.34 m | 0 | Q4-05 有效：目标均为 0.1/0.2 m |
| `health_jitter`（3 次读有 2 次不健康，Q4-10） | dry 失败 | — | 0 | 0 | 有效 |
| `stop_slow_0.95`（stop 耗时 0.95 s） | 失败（导航运行状态 failed） | 是 / 是 | 0.118 m | 0 | 与上轮一致，合理 |
| `read_hangs`（运动中里程计读取阻塞 40 s） | 失败（看门狗归零 40.0 s） | 是 / 是 | 0.031 m | 0 | 运行时看门狗独立生效；套件本身被卡 40 s，见 R4-05 |
| `coast_lying`（停止后滑行 1.5 s，读数冻结） | 失败（冻结结束后又动） | 是 / 是 | 0.194 m（滑行 0.148 m） | 0 | 偶然抓到 |
| `coast_short_lying`（滑行 0.5 s，读数冻结） | **通过** | 否 | 0.442 m（滑行 0.324 m） | 0 | 固有限制（已在 `trust` 写明） |
| `watchdog_lying`（忽略零速度、读数伪装静止） | **通过** | 否 | 0.327 m | 0 | 固有限制（已写明） |

Ctrl-C（`python3 -B ctrlc.py <quad|arm> <时刻>`，motion 中 6 个时刻发 `KeyboardInterrupt`）：`run_adapter` 的 `finally` 关闭运行时后，6/6 本体静止，之后 3 s 位移 0。

---

## 3. 新发现

严重度：P0 物理危险/越权；P1 功能错误或可被利用；P2 健壮性/契约偏差；P3 建议。

### R4-01 · P0 · runtime · dry 级按 `joint.state` 的顺序给机械臂发"零位移"目标，诚实本体也会动 1.57 rad（Q4-02 未修完）

- **位置**：`conformance.py` `_arm_joint_map`（1244–1267：`self._arm_names = list(a["names"])`，每次调用都按最新一次读数重新确定）、`_arm_current`（1269–1276）、`_arm_current_direct`（1229–1242，守卫用的"当前角"）、`_guard_problem`（1207–1227）、`_dry_arm_zero`（1578–1594）；同样的取值也用于 `_dry_chunk_zero`（joint_position 动作块）和 motion 级全部机械臂用例。
- **根因**：
  1. 能力标准里 `arm.joint_position` 没有关节名（README 也承认）。套件因此只能**假定 `joint.state` 的顺序就是机械臂指令的顺序**。"两次读取名称唯一且相同、数量 = dof"只证明了读数稳定，不证明顺序与指令一致。README 写的"按关节名对应"并不成立。
  2. dry 守卫用的是同一套假定（`_arm_current_direct` 用 `self._arm_names` 映射），等于拿套件自己的推断去核对套件自己的推断，所以对这类错误永远不报（实测 `violations: []`）。
  3. 映射不是固定的：`_arm_current` 每次都调 `_arm_joint_map` 重新读，驱动中途换了顺序，新顺序会被直接当成指令顺序（`arm_names_flip`）。现有用例 `test_unstable_names_are_blocked` 只覆盖"每次读都翻转"，没有覆盖"稳定地换了顺序"。
- **复现**（§2.2）：`python3 -B ur_pose.py`。dry 级、未授权，SimArm 子类诚实地按字母序发布 6 个关节，初始位姿 `[0,-1.57,1.57,-1.57,-1.57,0]`。套件向适配器发出 `[1.57,-1.57,0.0,-1.57,-1.57,0.0]`，肩关节转 1.57 rad，肘关节转 1.57 rad，本体真的走到了那里。报告只在事后给出 `zero_displacement failed`。
- **现实性**：ROS 的 `/joint_states` 顺序由驱动或 `joint_state_broadcaster` 决定，常见的是 URDF 序或字母序，不保证等于控制器的关节顺序（有的 UR ROS 驱动就按字母序发布）。MoveIt 等上层总是按名称匹配。这正是第 04 轮审查时"不是刻意构造的极端情形"的那一类本体，只是这次关节数恰好相等。
- **修复建议**：
  1. 最小修复（可在本轮完成）：**dry 级不向适配器发送任何机械臂目标或 joint_position 动作块**。`zero_displacement` 与 `zero_chunk`（joint_position）改为 `blocked`（"能力标准没有机械臂关节名，无法证明零位移"），只保留依赖运行时拒绝的探测（它们到不了适配器）。
  2. 根本修复（变更请求，走 spec）：`arm.joint_position.params.joint_names`（必填，长度 = dof），套件与运行时都按名称映射；`joint.state` 缺少其中任一名称就 blocked。
  3. 首次验证后把映射固定下来，之后顺序变化一律 `_Abort`（不重新推断）。
  4. dry 守卫必须独立于套件的推断：至少比较"目标向量"与"适配器原始 `joint.state.position` 向量"（不经名称映射）；两者不同就拒绝。
  5. 补用例：稳定的字母序、倒序本体，断言 dry 级到达适配器的机械臂目标为 0 个、位移为 0。

### R4-02 · P1 · runtime · 已锁存时发现本体又动，套件不再停车；测试结束后本体继续行驶，报告却写"已停车"（Q4-07 未修完）

- **位置**：`conformance.py` `_hold_still`（823–837：`if not self.target.latched(): self._stop("hold-violated")`）、`_abort`（735–744：`if not self.target.latched(): stop`）。
- **根因**：两处都把"运行时已锁存"当成"本体已停"。延迟恢复、驱动自行重放这类缺陷，恰恰发生在运行时已经锁存之后：适配器不知道运行时的锁存，运行时也不会再给适配器下发停止。套件正确地发现了运动，却跳过停止，直接以"已停车并保持锁存"的文字中止。
- **复现**：`python3 -B restop.py stop_resume_velocity_1.3`
  - 适配器：`stop()` 生效，1.3 s 后驱动恢复上一条速度 0.1 m/s。
  - 套件：`motion.base.velocity.stop_latency` 判"停止后 1.32 s 本体又动了…已停车并保持锁存"，`final_latched=true`。
  - 事实：套件最后一次 `adapter.stop()` 在结束前 1.35 s，之后没有任何停止。测试结束后 0/1/2/3/4/5 s 本体 x = 0.056/0.156/0.256/0.356/0.456/0.556 m，`cmd=[0.1,0,0]`，**不会自己停**。
  - `python3 -B restop.py nav_cancel_resume_2.5`：结束后又走 0.171 m（目标近，自己到达后才停）。
- **影响**：操作员看到"中止、锁存、已停车"，本体实际仍在行驶。现有用例 `test_delayed_resume_after_stop` 只断言 `aborted`，不检查结束后是否静止，所以没有发现。
- **修复建议**：
  1. 发现运动时**无条件**再发一次停止（停止永远无害；锁存属于他人时也应停，只是不做计数归属，随后照常中止）。`_abort` 同理：中止原因是"又动了"时必须停止，不能看锁存状态。
  2. 停止后再验证静止；仍在动则在报告里如实写"已发出停止但本体仍在运动，请立即使用硬件急停"，CLI 退出码与摘要突出显示。
  3. 运行时侧可加一层防护：锁存期间若里程计/关节反馈显示运动，周期性重发 `adapter.stop()` 并发事件。
  4. 补用例：对 `stop_resume`、`nav_cancel_resume_2.5` 断言套件返回后 3 s 内本体静止。

### R4-03 · P1 · runtime/safety · 操作员在适配器 `reset()` 期间按停：监督器重新锁存，但适配器的复位在停止之后才完成

- **位置**：`safety.py` `SafetySupervisor.reset`（第 606、609 行两处 `self._stop_seq != stops`）与 `_abort_reset`：检测到 `_stop_seq` 变化后只重新置锁存、写盘、抛 `EStopLatched`，**不再调用 `adapter.stop()`**。
- **复现**：`python3 -B race.py in_adapter_reset_replay 4`
  - 本体：`reset()` 耗时 0.3 s（真机电机使能、腿足起身通常更久）。复位完成时驱动恢复上一条速度（与 Q4-07 `reset_resumes_velocity` 同类）。
  - 套件第 4 次条件复位期间（复位开始后 0.1 s），操作员按停。
  - 结果：套件判定正确（`estop_latched` → 中止），`final_latched=true`，原因 = `operator: person in path`，之后 0 次复位、0 条运动调用；**但本体 `cmd=[0.1,0,0]`，套件结束后 3 s 内又走 0.316 m 且不会停**。
  - 对照：同一驱动、无操作员按停（`race.py in_adapter_reset_replay 5..8`）时，套件在复位后 0.03 s 发现运动并停车，结束后位移 0。
- **说明**：这是运行时的问题，不只是套件的：任何"复位与停止交错"都一样（两名操作员、技能停止与人工复位）。修复后的套件每轮 dry 有 2–3 次（实测 SimArm 2 次、SimQuadruped 3 次）、motion 有更多次条件复位，暴露窗口比以前大。对腿足本体，"复位"本身可能就是起身动作，即使驱动不重放指令，也会在急停之后完成起身。
- **修复建议**：`reset()` 发现停止与复位交错时（两处 `self._stop_seq != stops`），在重新锁存后**再调用一次 `adapter.stop()`**（异常也要吞掉），并发 `safety.reset_raced` 事件。补用例：适配器 `reset()` 中途触发停止，断言复位返回后适配器最后一个调用是 `stop()`。

### R4-04 · P2 · runtime · `base.velocity` 的 motion 结论只验证了 +vx；角速度与侧向速度从不下发，方向反了、×5 也算通过

- **位置**：`_velocity_args`（1695–1699：`{"vx": v, "wz": 0.0}`）、`_check_base_motion`（侧向容差 `0.5·|forward| + 5 mm`）。
- **复现**：`liars2.py wz_reversed_x5`（wz 反向且 ×5）、`liars2.py vy_leak`（全向底盘 vy 混入 0.4·vx）均 `motion_ok=true`，报告里 `base.velocity` 为 passed。
- **建议**：增加小幅 wz 用例（如 0.1 rad/s、0.3 s，核对 IMU/里程计角速度符号与幅度）；全向底盘（`holonomic:true`）增加 vy 用例；侧向容差收紧到 0.2·前向。在补齐之前，报告里写明"base.velocity 只验证了 vx"。

### R4-05 · P2 · runtime · 适配器调用卡住时套件没有超时，server 模式的运动租约跟着一直占用，且无法取消

- **位置**：`Suite` 对适配器调用没有超时；`ConformanceRuns` 没有取消接口；`_work` 只在套件返回后才释放 `conformance_lease`。
- **复现**：`python3 -B server_lease.py hang`。dry 运行中里程计读取阻塞 60 s → 一致性测试 65.3 s 后才结束；期间操作员 131 次复位/零速度调用全部 409 `conformance_running`。停止始终可用（符合设计），但除了重启运行时，没有办法取消这次测试。驱动真正死锁时，会无限期锁住复位、技能、`/v1/start`、`/v1/evaluate`。
- **建议**：每次适配器调用设看门狗超时（如 5 s，超时即中止并锁存）；增加 `POST /v2/conformance/{id}/cancel`（或停止即取消一致性运行），取消后立即释放租约；租约设硬性上限（如 level 预计时长 × 3）。

### R4-06 · P3 · runtime/sdk · 脚手架接受与标准库同名的包名

- **复现**：`rr/scaf_probe.py` 中 `name="json"` 生成成功，生成包自带的 `tests` 失败（failures=1, errors=2）：包目录 `json/` 遮蔽了标准库 `json`，放进 `PYTHONPATH` 后运行时本身也会被破坏。
- **建议**：用 `sys.stdlib_module_names` 加常见第三方包名做拒绝表。

### R4-07 · P3 · runtime · 生成的 README 可被注入 Markdown 链接

- **复现**：`vendor="Acme [官方驱动下载](http://evil.example/x)"` 被 runtime 脚手架接受，链接原样出现在生成的 README 中（SDK 侧的 `_SAFE_TEXT` 不允许 `[`，会拒绝；直接用 `neuxmind-runtime scaffold --spec` 则会通过）。
- **建议**：`_TEXT_FORBIDDEN` 增加 `[]()`，或在 README 中把这些字段放进代码段。

### R4-08 · P3 · runtime/tests · 修复配套用例的盲点

- `test_unstable_names_are_blocked` 只覆盖"每次读都翻转"，没有"稳定但顺序不同"（R4-01）。
- `test_delayed_resume_after_stop`、`test_delayed_resume_after_cancel` 只断言 `aborted`，不断言套件返回后本体静止（R4-02）。
- 没有"停止与复位在适配器层交错"的用例（R4-03）。
- 建议在 `test_conformance_review.py` 中统一加一个断言助手：套件返回后再推进仿真 3 s，物理位移必须为 0。

---

## 4. 修复引入的新机制：专项检查结论

| 关注点 | 检查方法 | 结论 |
|---|---|---|
| `stop_count` 竞态（停止与计数核对之间、核对与复位之间、复位执行中） | `race.py` 四种注入点 | 套件侧全部正确中止、不复位；适配器层的交错见 R4-03 |
| `conformance_running` 租约是否会挡住真实操作员的停止 | `server_lease.py normal/hang` | 停止从不被挡；复位、运动在测试期间被挡（409），测试结束后下一次轮询（≤0.5 s）即恢复；适配器卡住时无限期挡住且无法取消（R4-05） |
| 租约在异常时是否释放 | 代码审阅（`_work` 的 `finally`）+ hang 实验结束后 `lease_now=false` | 释放 |
| `final_still` / `_hold_still` 是否可能无限循环 | 代码审阅 + 全部实验时长 | 两者都有时长上限（2 s / 1 s / 2 s）；唯一的无界等待来自适配器调用本身（R4-05） |
| dry 守卫 | 全部 dry 实验的 `dry_guard.violations` | 守卫在位（`active_during_dry=true`），motion 前已卸下；但对机械臂没有独立判断能力（R4-01） |
| 时钟重设 `set_time_scale` | `sim_subclass_fast`、内置仿真 20× 的既有用例 | 只对内置仿真类重设；子类、非仿真一律 1× 且目标有绝对上限 |
| Ctrl-C 中断 | `ctrlc.py` 6 个时刻 | 6/6 安全 |
| 固有限制（读数与动作同时伪造） | `coast_short_lying`、`watchdog_lying` | 仍通过（滑行 0.32 m、看门狗失效 0.33 m），报告 `trust` 与免责声明已写明，不另立缺陷 |

## 5. 脚手架注入（Q4-09）探针明细

命令：`cd rr && timeout 600 python3 -B scaf_probe.py`（输出 `rr/scaf_probe.json`）。

- **拒绝（20）**：`display_name`/`vendor` 含 `"""`+换行、`model` 含换行、`vendor` 含 U+2028、含 U+202E（双向覆盖）、`evidence_id` 含换行或 `#`、`adapter_version` TOML 注入、实现提示含引号/换行/`#;`、ROS 2 节点名与坐标系名注入、`stale_after_s=1e9`、包名 `../../evil` / `__init__` / `import`、`instance_id` 注入、厂商扩展 ID 含换行、`family` 注入。全部**未留下任何文件或 `.<name>.scaffold-*` 临时目录**。
- **接受（6）**：Markdown 链接（R4-07）、全角引号、`notes` 含 `"""`+NUL（只进 JSON 且已过滤不可打印字符）、字符串参数含代码、厂商扩展参数含代码、包名 `json`（R4-06）。生成的 `adapter.py` 中不出现任何外部原文；生成包自带的 `tests` 除 `json` 外全部通过；**标记文件 0 个**。
- SDK（`caba7ef`）：`check_scaffold_input` 对来自机器人 manifest 的字段再做一层白名单（不允许 `[`、引号等），属于纵深防御，审阅无问题。

## 6. 复现所用脚本

全部在 `/tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/rereview04/`：

| 脚本 | 用途 | 结果文件 |
|---|---|---|
| `rr_common.py` | 物理真值记录、调用审计 | — |
| `op_stop.py`、`sweep_op.sh` | Q4-01 按停时刻网格 | `op_sweep.jsonl` |
| `race.py`、`run_race.sh` | 定点竞态、R4-03 | `race.jsonl` |
| `liars2.py`、`run_liars.sh` | 34 个撒谎/异常适配器场景 | `liars.jsonl` |
| `restop.py` | R4-02 结束后轨迹 | — |
| `ur_pose.py` | R4-01 真实位姿 | — |
| `server_lease.py` | server 模式租约、R4-05 | — |
| `ctrlc.py` | Ctrl-C 中断 | — |
| `scaf_probe.py` | Q4-09 注入 | `scaf_probe.json` |
| `run_full.sh`、`run310.sh` | 全量测试 / Python 3.10 | `full_*.log` |

## 7. 给总指挥的处理建议

1. 本轮必须修复：R4-01（最小修复即可：dry 级不发机械臂目标）、R4-02（发现运动时无条件停车）、R4-03（交错复位后再停一次）。三者各补一个"套件返回后本体静止 / dry 级零机械臂目标"的断言用例，然后复验。
2. 下一轮：R4-04、R4-05；spec 变更请求 `arm.joint_position.params.joint_names`。
3. P3（R4-06、R4-07、R4-08）随手修。
