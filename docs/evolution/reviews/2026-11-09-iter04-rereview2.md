# 第 04 轮二次复审：fix2（df7600e / 7a696c4）

- 复审对象：HEAD `7a696c4`（runtime 修复 `df7600e`：R4-01..R4-08；契约文档 `7a696c4`）。上一次复审：`2026-11-09-iter04-rereview.md`。
- 复审方式：只做实验，不采信修复报告。原复现脚本复制到新目录后改指本 worktree（未在原处改动）；新增脚本只在审查 worktree 外的草稿目录。物理位移取自仿真内部状态（`_step` 积分），**不取自适配器上报的读数**。
- 脚本目录：`/tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/rr2/`（下文简称 `rr2/`）。`grid.py` 以子进程并行跑网格，每例 `timeout 200`。
- 所有长命令带 `timeout`；后台进程均为本会话启动且已结束。进度写在 `REREVIEW2_PROGRESS.md`。

## 结论

**不可发布 0.11.0。** 原 P0（R4-01）对“诚实本体 + 正确声明 / 未声明 joint_names”已修复，R4-02、R4-03 已修复。但 fix2 引入了一个新的 P1，另外还有两个 P1 未关闭：

| ID | 严重度 | 一句话 |
|---|---|---|
| R4b-01 | **P1（fix2 新引入）** | 套件的 `reset()` 调用超时（R4-05）后，中止时的“再停一次”走 `halt_motion`，不增加停止计数（R4-02）。被放弃的 `adapter.reset()` 之后返回，监督器没有发现竞态，**静默解除锁存**。报告写着 `final_latched: true`、“急停保持锁存”。复位后会重放上一条指令的驱动：套件返回后 3 s 解锁，以 0.1 m/s 一直行驶（默认阈值下观察到 1.9 m，且不会自己停） |
| R4b-02 | **P1** | `joint_names` 声明错误（例如照抄 `/joint_states` 的字母序）：dry 级（未授权）机械臂转 **1.57 rad**（倒序声明 **3.0 rad**）。套件在 15–30 s 内不监视、不停止；`zero_displacement` 失败后 dry 不中止，又发出第二个动作块再转一次（路径 6.28 rad）。守卫 0 次拦截。README 只写了“按错误声明执行”，没有写会不受监视地转动并连续发两次 |
| R4b-03 | **P1（既有，未改）** | 运行时 joint_position 动作块的逐步限速：未声明 `joint_names` 时仍按 `joint.state` 顺序取起点。诚实的字母序本体上，限速被绕过：肩关节一步（0.1 s）跳 **1.5 rad**（上限 0.1 rad），肘关节被拉 1.47 rad。声明 `joint_names` 后限速正确。属于授权运动路径上的安全限幅失效 |

R4b-01 的最小修复很小：超时或中止时用会计数的停止（见修复建议）。R4b-02、R4b-03 也都可以只在运行时/套件里修，不需要改 spec（建议见下文）。

---

## 1. 全量测试

命令（worktree 根目录，五个模块并行，每个 `timeout 1500`，`rr2/run_full.sh`）：

```
PYTHONPATH=$PWD/spec:$PWD/runtime:$PWD/cloud:$PWD/sdk:$PWD timeout 1500 python3 -B -m unittest discover -s <t>
```

| 模块 | Python 3.13 结果 |
|---|---|
| `spec/tests` | 12 OK |
| `runtime/tests` | **358 OK**（1099 s，与复审实验并行） |
| `cloud/tests` | 211 OK |
| `sdk/tests` | 84 OK |
| `tests` | 184 OK（skipped 39） |

另在 Python 3.10.20 上跑 `runtime/tests/test_conformance_rereview04.py`：21 OK（211 s）。

全绿。cloud / sdk / desktop（`brainos`）/ web 对报告的消费没有回归：`brainos.parse_report` 与 cloud 摘要只按白名单取字段，新增的 `final_motion`/`end_state`/`restops`/`call_timeout` 不会被拒绝（但也没有被带过去，见 R4b-07）。`joint_names` 校验只在声明时生效，现有清单（未声明）全部仍可通过 static。

---

## 2. 原问题逐项复验

| 原编号 | 复验结论 | 关键数字 |
|---|---|---|
| R4-01 P0（dry 按 joint.state 顺序发目标） | **已修复（声明正确或未声明时）**；声明错误 → R4b-02；运行时动作块限速 → R4b-03 | 63 例网格：未声明 `joint_names` 时 9 种 `joint.state` 排列 × dry/motion 全部 blocked，**到达适配器的机械臂/动作块目标 0 个，位移 0**；声明正确时 dry 位移 0、motion 通过且最大偏离 0.05 rad（只动腕关节）；`joint.state` 缺关节或名称不同 → blocked |
| R4-02 P1（锁存时发现运动不再停车） | **已修复** | 原复现 `restop.py stop_resume_velocity_1.3`：中止后又停 5 次，报告“警告：…仍观察到运动…请立即使用硬件急停”（此前写“已停车”）。27 例网格中，每次只恢复一两次的驱动全部被停住；套件返回后位移为 0。重停循环有上限：最多 20 次、8.05 s；报告从未在最后观察到运动时写“已停”。剩余的时间窗问题见 R4b-04 |
| R4-03 P1（复位竞态） | **已修复** | 57 例网格（第 1–5 次条件复位 × 按停偏移 0/0.05/0.15/0.29/0.31/0.5 s，复位耗时 0.3 s / 2.0 s，驱动在被竞争的那次复位完成时重放速度，dry/motion）：按停全部落在复位执行中的例子都发出 `safety.reset_raced`，复位返回后再停一次；**套件返回后位移全部 0**，锁存原因全部保持“operator: person in path”，操作员停止后 0 次复位；`emergency_stop()` 耗时 0.1–0.2 ms，无死锁 |
| R4-04 P2（wz/vy 未测） | **已修复**（残留见 R4b-05/06） | wz ×5、反向、×0.4、被忽略、wz 带平移，vy ×5、反向，vx 混入 0.4·vx 的 vy：全部判失败并中止，物理路径 ≤ 0.19 m。按设计通过的有：wz ×1.4、vy 混入 0.15·vx（< 20 %）、非全向底盘的 vy 反向（lateral = not_applicable） |
| R4-05 P2（适配器调用卡住） | **已修复**；但引出 R4b-01 | server 模式里程计读取卡 60 s：一致性测试 **5.52 s** 结束（此前 65.3 s），租约释放；操作员 6.0 s 复位成功。`read_hangs`（motion）8.8 s 结束（此前约 40 s）。Ctrl-C 6 个时刻：6/6 本体静止，之后 3 s 位移 0 |
| R4-06/07 P3（脚手架） | **已修复** | 26 个注入探针：包名 `json` 被拒（“与标准库或常用包同名”），`vendor` 含 `[]` 被拒；标记文件 0、残留文件 0、临时目录 0 |
| R4-08（用例盲点） | **已补**（新盲点见 R4b-10） | 新增 `test_conformance_rereview04.py` 21 例 |

### 2.1 R4-01：机械臂映射网格

命令：`python3 -B grid.py arm_names.py arm_grid.txt arm_grid.jsonl 4 200`；单例：`python3 -B arm_names.py <joint.state 顺序> <声明> <dry|motion> chunk`（`summary`：`sum_arm.py`）。

本体：SimArm 子类，物理关节按控制器顺序 `[pan, lift, elbow, w1, w2, w3]`，UR 常见位姿 `[0,-1.57,1.57,-1.57,-1.57,0]`；同时声明 `policy.action_chunk`（joint_position，dim 6）。

| `joint.state` 顺序 | 声明 `joint_names` | dry：到达适配器的目标 | dry 物理位移 | 守卫拦截 | 判定 |
|---|---|---|---|---|---|
| 控制器序 / 字母序 / 倒序 / 乱序 / 夹爪在前 / 夹爪在后 / 中途换序 | 未声明 | 0 | 0 | 0 | 5 个 dry 用例 blocked（含 `stop_epoch`） |
| 同上 7 种 | 正确（控制器序） | 2（零位移目标 + 零动作块） | **0** | 0 | dry 通过；motion 通过，最大偏离 0.05 rad |
| 缺一个关节 / 名称体系不同 | 任意 | 0 | 0 | 0 | blocked |
| 任意 7 种 | **字母序（错误）** | 2 | **1.57 rad**（路径 6.28 rad） | **0** | `zero_displacement`、`zero_chunk` 事后 failed，**未中止** |
| 任意 7 种 | **倒序（错误）** | 1 | **3.0 rad** | **0** | 30 s 动作超时后才中止 |

详见 R4b-02。

### 2.2 R4-02：恢复运动的驱动

命令：`python3 -B grid.py restop2.py restop_grid.txt restop_grid.jsonl 4 200`；单例 `python3 -B restop2.py <every|once|twice|ignore|navresume> <延迟 s> [level]`。“结束后 5 s”是套件返回后再推进仿真 5 s 的物理路径。

| 驱动行为 | 恢复延迟 | 中止后再停次数 | `final_motion.still` | end_state | 结束后 5 s |
|---|---|---|---|---|---|
| 每次停止后都恢复 | 0.05 / 0.3 s | 20（上限），观察 8.05 s | false | 警告…请立即使用硬件急停 | 0.49 m（任何软件停止都停不住，如实报告） |
| 每次停止后都恢复 | 0.8 / 1.3 s | 9 / 5 | false | 警告 | 0.49 m |
| 每次停止后都恢复 | **2.5 / 4 / 9 s** | 0 | **true** | “本体持续静止 2.02 s；急停保持锁存” | **0.45 / 0.30 / 0.11 m** → R4b-04 |
| 只恢复一次 | 0.05–9 s（7 档） | 0 | true | 持续静止 | **0** |
| 恢复两次 | 0.05–1.3 s | 1 | true（期间再停 1 次） | 如实写“观察到运动并再次停止 1 次” | **0** |
| 恢复两次 | **2.5 / 4 / 9 s** | 0 | true | 持续静止 | **0.45 / 0.30 / 0.11 m** → R4b-04 |
| 忽略停止 | — | 20，8.04 s | false | 警告 | 0.49 m |
| 导航取消/停止后恢复目标 | 1.3 / 2.5 / 4 s | 0 / 1 / 1 | true | 如实 | 0 |
| dry 级（上述两种） | 0.3 s | 0 | — | 持续静止 2 s | 0 |

结论：中止后无条件再停、观察中按需再停、有上限、如实报告，都按契约工作；没有无限循环，也没有刷屏（单次运行到达适配器的 `stop()` 最多 29 次，其中重停 ≤ 20 次、间隔 ≥ 0.3 s）。

### 2.3 32 个撒谎适配器场景（上轮 `liars2.py` 全量重跑）

命令：`python3 -B grid.py liars2.py liars_grid.txt liars_new.jsonl 3 200`。与上轮对比的变化：

- `stop_resume_velocity_1.3`：仍判失败；结束后 3 s 仍走 0.29 m（驱动每次停止都恢复），但报告现在写“警告：中止后已发出停止 5 次，仍观察到运动…请立即使用硬件急停”。
- `nav_cancel_resume_2.5`：结束后位移 0.171 m → **0**。
- `wz_reversed_x5`、`vy_leak`：通过 → **失败并中止**（`turn` 用例 / 侧向容差 20 %）。
- `watchdog_lying`：通过 → **失败**（被 `turn` 用例的“只给 wz 却平移 0.206 m”抓到）。
- `read_hangs`：约 40 s → 8.8 s。
- 机械臂 `arm_*` dry 场景：全部 blocked、0 目标、0 位移（这些本体用的关节名与 SimArm 声明的 `joint1..6` 不同）。
- 其余（Q4-03..Q4-11 各项）与上轮一致；`coast_short_lying`、`velocity_x1.4` 仍通过（固有限制 / 按设计）。

---

## 3. 新发现

严重度：P0 物理危险/越权；P1 功能错误或可被利用；P2 健壮性/契约偏差；P3 建议。

### R4b-01 · P1 · runtime（fix2 引入）· 复位调用超时后，被放弃的 `adapter.reset()` 返回时运行时静默解锁；报告却写“急停保持锁存”

- **位置**：`conformance.py` `_TimedTarget`（R4-05：超时后放弃线程）、`_abort` → `_restop` → `RuntimeTarget.restop`（429–441：已锁存时走 `supervisor.halt_motion`，**不增加 `_stop_seq`**）；`safety.py` `SafetySupervisor.reset`（581–617：只靠 `_stop_seq` 变化识别竞态）。
- **根因**：适配器 `reset()` 卡在里面时，监督器仍处于锁存（锁存在 `adapter.reset()` 返回后才清除）。套件超时中止，按 R4-02 的设计“锁存时用 `halt_motion` 再停一次、不改计数”。但这正好让监督器看不出复位期间有过停止：被放弃的 `adapter.reset()` 返回后，`_stop_seq` 没变，于是写盘“未锁存”、清锁存、发 `safety.reset`。R4-03 的竞态保护只对**计数**的停止有效，套件自己的“再停”恰好绕开了它。
- **复现**：`python3 -B reset_hang.py <第 n 次复位> <卡住 s> <复位后重放 1|0> [level]`（阈值 `reset_call_timeout_s=2`，以便快速复现；`RT=15` 用默认值）。

| 场景 | 报告 | 实际 |
|---|---|---|
| 第 1/2/3 次复位卡 6 s（dry 内）、motion 级 | `final_latched: true`，aborted“…已发出停止并保持锁存…急停保持锁存” | 套件返回后 **4 s 运行时解锁**（事件序列 `safety.halt` → `safety.reset`），无人操作 |
| 第 2 次复位卡 6 s、dry 级 | 同上 | 同样解锁 |
| 第 4/5 次复位卡 6 s（motion，已下发过速度），驱动复位时重放上一条速度 | 同上 | 解锁后本体以 0.1 m/s 行驶，9 s 内 **0.6 m**，`cmd=[0.1,0,0]`，不会自己停 |
| 第 5 次复位卡 6 s，不重放 | 同上 | 解锁，本体静止 |
| **默认阈值** `RT=15`，第 4 次复位卡 18 s + 重放 | `final_latched: true`，end_state“警告：适配器调用曾超时…请现场确认并使用硬件急停；急停保持锁存” | 套件返回后 **3 s 解锁**，之后 19 s 内行驶 **1.9 m** 且不停 |

- **影响**：违反“只有人工授权复位才能解除锁存”的基本约束。server 模式（`POST /v2/conformance` 在常驻运行时上）解锁后，技能、`/v1/evaluate`、操作员的运动都会被放行；报告、桌面端和云端摘要都显示“锁存中”。“驱动卡死十几秒后自己恢复”正是 R4-05 要处理的那类缺陷；腿足本体的复位/起身本身就可能很慢。
- **修复建议**：
  1. 套件：任何 `CallTimeout`（至少 `reset` 超时），以及中止时**无法排除有调用仍在适配器里**的情况，一律用**计数的停止**（`runtime.emergency_stop`），不用 `halt_motion`。监督器的复位竞态保护随后会在被放弃的复位返回时重新锁存，再停一次（R4-03 路径）。要保留操作员的锁存原因，可以给 `emergency_stop` 加 `keep_reason=True`。
  2. 监督器（纵深防御）：`reset()` 执行期间调用 `halt_motion` 也算“复位期间有停止”（例如设一个 `_reset_in_progress` 标志，`halt_motion` 时递增 `_stop_seq` 或置 `_reset_poisoned`）。
  3. 补用例：适配器 `reset()` 卡住超过 `reset_call_timeout_s` 后返回并重放速度，断言运行时仍锁存、最后一个适配器调用是 `stop()`、套件返回后位移为 0。

### R4b-02 · P1 · runtime · `joint_names` 声明错误时，dry 级不受监视地转动机械臂 1.57–3.0 rad，且失败后不中止、再发第二个动作块

- **位置**：`conformance.py` `_dry_arm_zero`（1800–1816：发出目标后只 `_wait_run` 等结果，期间不读 `joint.state`）；dry 级失败处理（`_on_failure` 841–851：dry 只在“仍有运行未结束”时中止）；`_dry_chunk_zero`（1835–1852）；守卫 `_arm_current_direct`（1448）与套件用同一份声明。
- **复现**（§2.1）：`python3 -B arm_names.py cmd alpha dry chunk`，以及 `… cmd reversed dry chunk`。
  - 声明按字母序（`elbow, shoulder_lift, shoulder_pan, …`），控制器真实顺序是 `pan, lift, elbow, …`：套件向适配器发出 `[1.57,-1.57,0.0,-1.57,-1.57,0.0]`，本体肩关节 0→1.57、肘关节 1.57→0（**1.57 rad**）。`zero_displacement` 事后判 failed，**dry 没有中止**；接着 `zero_chunk` 又按同一错误映射发出动作块，把两个关节转回去（物理路径 6.28 rad）。运行 21.5 s，`aborted: null`，`end_state: "测试结束：本体持续静止 2 s"`，`dry_guard.violations: []`。
  - 声明倒序：发出的目标让 elbow/w1 各需转 3.14 rad。套件等满 30 s 动作超时，期间本体转了 **3.0 rad**，之后才中止。
  - 9 种 `joint.state` 排列下结果相同（错误只取决于声明）。
- **现实性**：README §3.1 自己写了“ROS 的 `/joint_states` 常按字母序发布”。集成者最自然的写法就是 `ros2 topic echo /joint_states` 后照抄名字顺序，而 FollowJointTrajectory 控制器用的是 `ros2_control` 的 `joints:` 顺序。脚手架对 `arm.joint_position` 只生成 TODO，不生成也不提示 `joint_names`。dry 级的定位是“未授权、不会动”的第一次运行，用来发现的正是这类适配器错误。
- **修复建议**（任选其一即可关闭 P1，建议 1+2）：
  1. dry 级“零位移”动作执行期间以 ≥ 20 Hz 读 `joint.state`（按声明映射），任一关节变化 > `joint_tolerance_rad` 就立即停止并中止。这样即使声明错误，位移也只有毫弧度级（0.1 rad/s × 一个轮询周期）。
  2. dry 级任何“发出后观察到位移”的失败一律停止并中止（与 motion 一致），不再继续发后续的运动类用例。
  3. 或者按上次复审的最小修复：dry 级不向适配器发送任何机械臂/joint_position 动作块目标（`zero_displacement`/`zero_chunk` 改为 motion 级用例）。
  4. 脚手架 TODO 与 README 写明 `joint_names` 必须取自控制器配置（`ros2_control` `joints:`），并让 ROS 2 适配器用它填 `FollowJointTrajectory.trajectory.joint_names`，使声明与指令按名称自洽。
  5. 补用例：声明为字母序/倒序的本体，断言 dry 级位移 ≤ 0.01 rad 且中止。

### R4b-03 · P1 · runtime/safety（既有，fix2 未改）· 未声明 `joint_names` 时，动作块逐步限速按 `joint.state` 顺序取起点，限速被绕过

- **位置**：`bus.py` 240–254（未声明时 `context["joint_position"] = state["position"]`）、`safety.py` 432–456（`previous[i]` 按下标取）。README §7 写明“未声明时沿用旧行为”。
- **复现**：`python3 -B chunk_limiter.py none` / `right`。诚实本体按字母序发布 6 个关节，位姿 `[0,-1.57,1.57,-1.57,-1.57,0]`，`max_velocity=1 rad/s`、`dt=0.1 s`（每步上限 0.1 rad），授权调用 `policy.action_chunk`，第一步要求 shoulder_pan 0→1.5：
  - 未声明：到达适配器的第一步是 `[1.5, -1.57, 0.1, -1.57, -1.57, 0.0]`：肩关节一步跳 **1.5 rad**（15 倍上限），肘关节被限速器自己从 1.57 拉到 0.1（1.47 rad）。
  - 声明正确：`[0.1, -1.57, 1.57, …]`，限速正确。
- **说明**：这是授权运动路径上监督器的最后一道限幅，面向 VLA 动作块，正是产品的目标用法。它不是 fix2 引入的，但套件已经承认“未声明时不得假定 `joint.state` 的顺序就是指令顺序”，运行时却仍按这个假定放行。
- **修复建议**：未声明 `joint_names` 时 joint_position 动作块 **fail closed**（拒绝，提示声明 `joint_names`）；至少要求 `joint.state.names` 恰好 dof 个且与上一次读取相同，并在报告/事件中警告。补用例。

### R4b-04 · P2 · runtime · 恢复延迟超过 2 s 观察窗的驱动：报告“本体持续静止 2 s”，随后本体继续行驶

- **复现**：`python3 -B restop2.py every 2.5`（以及 `twice 2.5/4/9`、`every 4/9`）。套件中止后观察到持续静止 2.02 s，写“中止后核对：本体持续静止 2.02 s；急停保持锁存”；套件返回 0.5 s 后驱动恢复上一条速度，5 s 内 0.45 m，`cmd=[0.1,0,0]` 不停。`twice 2.5` 说明这不只是“每次停止都恢复”的驱动：恢复只发生两次，第二次恰好落在观察窗之后。
- **评价**：有限观察窗的固有限制，报告文字字面上也属实；README §7 已列“运行时没有锁存期间监视反馈并周期性重发停止的守护（第 05 轮候选）”。但 end_state 容易被读成“已安全”。
- **建议**：end_state 写成“在 2 s 观察窗内静止（窗外的延迟恢复无法发现）”；第 05 轮优先做运行时锁存守护（锁存期间里程计/关节有速度 → 周期性 `adapter.stop()` + 事件），这样能覆盖套件结束之后的时间。

### R4b-05 · P3 · runtime · vx 用例不检查偏航：直行时以 0.5 rad/s 打转的本体通过

- **复现**：`python3 -B vel2.py wz_when_vx`（每给 vx 就附加 wz = 5·vx）：`motion_ok=true`。`_check_base_motion` 的 axis 0 只查侧向位移与 vy 峰值，不查 `peak_wz` / `turned_rad`。
- **建议**：axis 0、1 增加 `|peak_wz| ≤ 0.2 rad/s·(v/0.1)` 或 `|turned| ≤ 0.05 rad` 一类的检查。

### R4b-06 · P3 · runtime · `turn` 用例阈值对腿足本体偏紧

- “只给 wz，平移 ≤ 2 cm、线速度峰值 ≤ 0.05 m/s”：四足原地踏步转向时，机体速度估计常有 ±0.05 m/s 的振荡，里程计也会漂移几厘米，真机上可能误判失败并中止（安全侧）。仿真无此问题。建议按 `family=quadruped` 放宽，或改为“平移 ≤ 30 % 弧长 + 3 cm”，并在现场验收中确认。

### R4b-07 · P3 · cloud / desktop · “请立即使用硬件急停”的警告可能到不了云端

- 警告拼在 `aborted` 字符串**末尾**；cloud 摘要 `aborted[:300]` 截断（`cloud/neuxmind_cloud/conformance.py` 217）。motion 失败原因最长 200 字，再加上前缀与 end_state（约 80–100 字），总长可超过 300，警告会被截掉。cloud 与 `brainos.REPORT_FIELDS` 都不带 `final_motion`/`end_state`。桌面端按原样显示 `aborted`，不受影响。
- 建议：警告放在 `aborted` 开头，或 cloud/desktop 白名单加上 `end_state` 与 `final_motion.still`。

### R4b-08 · P3 · runtime · 黑盒 `--mode client` 下，“再停”会覆盖操作员的锁存原因并增加停止计数

- 契约 3a 说“已锁存时…不改锁存原因、不增加停止计数”，这只对进程内 `RuntimeTarget` 成立。`HttpTarget` 没有 `restop`，默认走 `stop()` → `POST /v2/stop` → `emergency_stop` + `annotate_stop_reason(reason)`（`core.py` 267–271），操作员的原因被改成 `conformance:<id>:abort` / `motion-after-stop`，每次 +1 计数（最多 20 余次）。fix2 之前 `_abort` 在已锁存时不停，所以这是小回归。属于审计信息问题，锁存本身不受影响（代码审阅，未单独实验）。
- 建议：`/v2/stop` 支持 `{"keep_reason": true}`，或 HttpTarget 的 restop 不带 reason。

### R4b-09 · P3 · runtime · `_TimedTarget` 的线程模型

- 每次调用都新开一个 daemon 线程：对有线程亲和性的厂商 SDK（要求同一线程调用、asyncio 绑定循环），所有调用都会在不同线程上执行。超时后被放弃的线程会和之后的 `stop()`/读取并发进入适配器（契约允许 stop 并发，但读取并发未声明）。`start_runtime()`（构建 EdgeRuntime，会调适配器）与 `close()` 不在限时范围内，适配器在这两处卡住时 CLI 仍会挂住。
- 建议：README 写明“适配器方法可能在任意线程被调用”；`start_runtime`/`close` 也设上限；超时后不再发起非停止的调用（目前已基本如此）。

### R4b-10 · P3 · runtime/tests · 新盲点

- 没有“声明错误的 `joint_names`”用例（R4b-02）。
- 没有“复位调用超时后迟到返回”的用例（R4b-01）。
- 没有“未声明 `joint_names` 的运行时动作块限速”用例（R4b-03；`test_runtime_chunk_limiter_maps_by_name` 只覆盖已声明）。
- `_restop`/`final_motion` 的用例都只用 `restop_hold_s` 内恢复的驱动，没有覆盖窗外恢复（R4b-04 的文字）。

---

## 4. fix2 新机制专项检查

| 关注点 | 检查方法 | 结论 |
|---|---|---|
| `halt_motion` 绕过锁存计数 | `reset_hang.py` 7 例 + 默认阈值 1 例；代码审阅 `RuntimeTarget.restop` / `SafetySupervisor.reset` | **有问题**：与复位超时叠加时静默解锁（R4b-01）。其他情况下（操作员锁存后的再停）锁存原因与计数都保持（57 例竞态网格、27 例重停网格） |
| 重停循环是否刷屏 / 无界 | `restop2.py` 27 例 | 有界：≤ 20 次、≤ 8.05 s、间隔 ≥ 0.3 s；单次运行到达适配器的 `stop()` 最多 29 次 |
| 超时线程导致并发调用 | 代码审阅 + `server_lease.py hang` + `read_hangs` | 卡住的读取与之后的 `stop()` 并发进入适配器，仿真无害；迟到返回的运动指令被 `after_dispatch`（锁存/代数变化 → 取消 + 再停）兜住；**迟到返回的 `reset()` 没被兜住**（R4b-01） |
| `joint_names` 校验是否拒绝现有清单 | spec 测试、全量测试、63 例网格（含 `joint.state` 多出夹爪关节、dof=7） | 不拒绝：只在声明时校验（非空唯一字符串、长度 = dof）；未声明的清单 static 仍通过并附说明 |
| 报告消费方（desktop/cloud/sdk/web） | 代码审阅 + 全量测试 | 无回归；新字段被忽略；警告可能在 cloud 被截断（R4b-07） |
| 停止是否总能快速返回 | 57 例竞态网格中测 `emergency_stop()` 墙钟 | 0.1–0.2 ms；无死锁 |
| 机械臂动作块路径（套件） | 63 例网格（全部声明 `policy.action_chunk` joint_position） | 未声明 → blocked、0 目标；声明正确 → 0 位移；声明错误 → R4b-02 |
| 机械臂动作块路径（运行时） | `chunk_limiter.py` | 声明 → 正确；未声明 → 限速失效（R4b-03） |

## 5. 复现所用脚本（`rr2/`）

| 脚本 | 用途 | 结果文件 |
|---|---|---|
| `rr_common.py`、`liars2.py`、`restop.py`、`race.py`、`server_lease.py`、`ctrlc.py`、`scaf_probe.py` | 上轮脚本（复制后改指本 worktree，逻辑未改） | `liars_new.jsonl`、`ctrlc.jsonl`、`scaf_new.out` |
| `grid.py` | 子进程并行网格，每例限时 | — |
| `arm_names.py`、`sum_arm.py` | R4-01 / R4b-02 机械臂映射网格 | `arm_grid.jsonl` |
| `restop2.py` | R4-02 / R4b-04 恢复驱动网格 | `restop_grid.jsonl` |
| `race2.py`、`sum_race.py` | R4-03 复位竞态网格 | `race_grid.jsonl` |
| `reset_hang.py` | R4b-01 复位超时后静默解锁 | `rh_grid.jsonl` |
| `vel2.py` | R4-04 / R4b-05 wz/vy 场景 | `vel_grid.jsonl` |
| `chunk_limiter.py` | R4b-03 运行时动作块限速 | — |
| `run_full.sh`、`run310.sh` | 全量测试 / Python 3.10 | `full_*.log`、`py310_rr04.log` |

## 6. 给总指挥的处理建议

1. 发布前必须修：
   - **R4b-01**：中止/超时时用会计数的停止（保留原因），并在监督器里让“复位期间的 halt”也算竞态；补用例。改动约十行。
   - **R4b-02**：dry 零位移动作期间监视关节并在首次变化时停止中止，dry 失败且观察到位移即中止（或 dry 不发机械臂目标）；补“声明错误”用例。
   - **R4b-03**：未声明 `joint_names` 时运行时拒绝 joint_position 动作块（fail closed）；补用例。
2. 复验时重跑 `rr2/reset_hang.py`、`arm_names.py`（`cmd alpha dry chunk`、`cmd reversed dry chunk`）、`chunk_limiter.py none`，断言：解锁 0 次、dry 位移 ≤ 0.01 rad、限速后单步 ≤ 0.1 rad。
3. 下一轮：R4b-04（运行时锁存守护）；R4b-05..R4b-09 随手修。
