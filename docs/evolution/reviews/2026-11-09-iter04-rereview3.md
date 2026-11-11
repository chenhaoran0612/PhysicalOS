# 第 04 轮三次复审：fix3（fbd9f37 / 4e92686）与 ux01（701da82）

- 复审对象：HEAD `4e92686`（runtime 修复 `fbd9f37`：R4b-01..R4b-10；契约 `4e92686`；桌面 ux01 `701da82`）。上一次复审：`2026-11-09-iter04-rereview2.md`。
- 方式：只做实验，不采信修复报告。上轮脚本从 `rr2/`、`fix3-rr/` **复制**到新目录（原处未改），通过 `NX_TREE` 指向本 worktree；物理位移取自仿真内部状态（`_step` 积分），不取适配器上报读数。
- 脚本目录：`/tmp/claude-0/-home-claude/7af0c949-418d-5e22-8b63-3856a96d5359/scratchpad/rr3/`（下文 `rr3/`）。网格由 `grid.py` 以子进程并行（2 路，本机 2 核），每例 `timeout 240`。
- 所有长命令带 `timeout`；后台进程均为本会话启动且已结束（另有两个 `neuxmind_runtime serve --port 8790/8791` 属于其他会话的 demo，未触碰）。进度写在 `REREVIEW3_PROGRESS.md`。

## 结论

**可以发布 0.11.0。** 上轮三个 P1（R4b-01/02/03）经实验全部关闭；fix3 没有引入 P0/P1。ux01 桌面功能在修复后的运行时上单测 17/17、浏览器验收通过，越权/跨站/孤儿进程探测均未突破。剩余问题均为 P2/P3，可进入第 05 轮。

| ID | 严重度 | 一句话 |
|---|---|---|
| R4b-01 | 已关闭 | 复位调用超时：22 例网格（第 1–5 次复位卡 2.5–20 s，重放/不重放，dry/motion，默认阈值 15 s 与 2 s）**解锁 0 次**、套件返回后位移 0 |
| R4b-02 | 已关闭 | dry 到达适配器的机械臂/动作块目标 **0 个**、位移 0（25 例）；motion 级错误声明（字母序/倒序）在 **≤ 0.022 rad** 内停下并中止（14 例） |
| R4b-03 | 已关闭 | 未声明 `joint_names` 的 joint_position 动作块被拒、0 步到达适配器；声明正确时第一步 0.1 rad（限速正确）；`joint.state` 名称重复也拒绝 |
| R4b-04 | P2（保留，固有） | 观察窗 2 s → 5 s：恢复延迟 2.5/4 s 现在被抓到并如实警告；延迟 ≥ 6 s 仍在窗外（报告已写明“观察窗之后才恢复的运动无法由本报告发现”）。根治仍需第 05 轮运行时锁存守护 |
| **R4c-01** | P2（fix3 新） | 被放弃的 `adapter.reset()` 仍在适配器里时，操作员可以再次复位（并发进入 `adapter.reset()`）；旧调用返回后写盘“锁存”而内存“未锁存”，`safety.reset_raced` 事件和报告都写“保持锁存”，与事实不符（安全侧） |
| R4c-02 | P3 | `/v2/stop keep_reason` 先读旧原因、读完请求体再写回，非比较交换：慢请求体可把操作员更新的原因改回旧原因（不比现有“任何持令牌客户端都能改原因”更强） |
| R4c-03 | P3 | 声明错误的 `joint_names` 现在 dry 照样“通过”（相关用例 not_applicable），错误要到 motion 才被发现；脚手架/README 仍未提示从 `ros2_control joints:` 取名（R4b-02 建议 4 未做） |
| R4c-04 | P3 | 新阈值面向仿真：dry 位移 0.01 rad 比较 `joint.state` 全部关节（含腿足 12 关节的站立抖动）与里程计偏航漂移；vx 偏航峰值 ≤ 0.2 rad/s 对步态晃动；机械臂包络 ±0.02 rad 对廉价舵机。都是误中止（安全侧），需现场标定 |
| R4c-05 | P3 | 动作块拒绝以“运行失败”异步返回（先 202/running，再 failed），不是同步 4xx；调用方需看运行结果 |
| R4c-06 | P3（ux01） | “直接运行技能”只凭桌面存储里的 `sim` 标记放行，没有再核对设备清单 `simulated: true`；目前没有任何接口能设置该标记或改仿真机器人的地址，属纵深防御 |
| R4b-05 | 已关闭 | `vel2.py wz_when_vx`：motion 失败并中止（“wz 峰值 0.500 rad/s 过大（上限 0.200）”） |
| R4b-06/07/09 | P3（保留） | 未改动：`turn` 阈值、cloud `aborted[:300]` 截断与白名单、`_TimedTarget` 线程模型 |
| R4b-08 | 已关闭（见 R4c-02） | `HttpTarget.restop` 发 `keep_reason: true` |

---

## 1. 全量测试

命令（worktree 根目录，顺序执行，`rr3/run_full.sh`）：

```
PYTHONPATH=$PWD/spec:$PWD/runtime:$PWD/cloud:$PWD/sdk:$PWD timeout 1800 python3 -m unittest discover -s <t>
```

| 模块 | Python 3.13 结果 |
|---|---|
| `spec/tests` | 12 OK |
| `runtime/tests` | **372 OK**（1346 s，与复审网格并行） |
| `cloud/tests` | 211 OK |
| `sdk/tests` | 84 OK |
| `tests` | 201 OK（skipped 39） |

全绿。

另：`tests/test_ux01_sim.py` 单独 17/17 OK（22.4 s）；`tests/ux01_browser.cjs` 通过（见 §4）。

---

## 2. 原问题逐项复验

### 2.1 R4b-01：复位调用超时后不再静默解锁

命令：`NX_TREE=<worktree> python3 -B grid.py reset_hang.py rh3_grid.txt rh3.jsonl 2 240`（阈值 `reset_call_timeout_s=2`），以及 `RT=15`（默认阈值）的 `rh3_rt15.txt`。时间线是套件返回后逐秒推进仿真 `hang+4` s。

| 场景 | 例数 | 报告 `final_latched` | 套件返回后解锁 | 位移 | 末尾事件 |
|---|---|---|---|---|---|
| 第 1/2/3/4/5 次复位卡 3 s / 8 s，复位返回时重放上一条速度，motion | 10 | true | **0** | 0 | `safety.estop` → `safety.reset_raced` |
| 第 2/5 次卡 6 s，不重放 | 2 | true | 0 | 0 | 同上 |
| 第 1/2/3 次卡 6 s + 重放，dry | 3 | true | 0 | 0 | 同上 |
| 刚超过阈值：第 1 次卡 2.5 s、第 4 次卡 2.2 s | 2 | true | 0 | 0 | 同上 |
| 默认阈值 15 s：第 4 次卡 18 s、第 2 次卡 20 s，重放 | 2 | true | 0 | 0 | 同上；end_state“警告：适配器调用曾超时（调用 reset() 超过 15 s 未返回）…急停保持锁存” |
| 对照：第 6 次（motion 只有 5 次复位，从未卡住） | 3 | false | 套件按设计解除自己的锁存 | 0 | `safety.reset` |

上轮同一脚本：默认阈值下套件返回 3 s 后解锁、行驶 1.9 m。本轮 **22 例卡住场景解锁 0 次**；停止计数每例比卡住前多 1–2（计数的再停）。中止时的再停改为计数停止（`keep_reason=True`），监督器同时把复位期间的 `_halt_seq` 变化当作竞态，两道都生效。

### 2.2 R4b-02：dry 不再发机械臂目标；motion 有关节包络

命令：`python3 -B grid.py arm_names.py arm3_grid.txt arm3.jsonl 2 240`。`joint.state` 顺序 5 种（控制器序/字母序/倒序/乱序/夹爪在后）× 声明 5 种（未声明/正确/字母序/倒序/名称体系不同）× dry/motion，全部带 `policy.action_chunk`（joint_position）；另加 4 例无动作块的 motion。

| 声明 | dry：到达适配器的机械臂/动作块目标 | dry 物理位移 | dry_ok | motion：到达的目标 | motion 最大偏离 | motion 结果 |
|---|---|---|---|---|---|---|
| 未声明 / 名称不同 | 0 | 0 | false（blocked） | 0 | 0 | blocked |
| 正确 | 0 | 0 | true（`dry_skipped`，not_applicable） | 8 | 0.05 rad（只动腕关节） | 通过 |
| **字母序（错误）** | **0** | **0** | true | 1 | **≤ 0.0221 rad** | 包络触发：停止、锁存、中止，套件后 2 s 位移 0 |
| **倒序（错误）** | **0** | **0** | true | 1 | **≤ 0.0209 rad** | 同上 |

上轮：字母序 dry 转 1.57 rad，倒序 3.0 rad。25 个 dry 例 `dry_guard.violations` 共 0。注意 dry_ok=true 不再说明声明正确（R4c-03）。

### 2.3 R4b-03：运行时动作块 fail closed

`chunk_limiter.py none|right`、`chunk_refuse.py none|right|dup`：

- 未声明：运行失败“SafetyRejected: …清单未声明 arm.joint_position.params.joint_names…请按机械臂指令顺序（ros2_control 的 joints:）声明 joint_names”，**0 步到达适配器**（上轮第一步跳 1.5 rad）。
- 声明正确：第一步 `[0.1, -1.57, 1.57, …]`，单步 0.1 rad 上限正确。
- `joint.state` 名称重复：拒绝，0 步到达。
- 现有合法用户：仓库内没有任何组件在未声明 `joint_names` 时使用 joint_position 动作块——`SimArm` 声明了 `joint_names` 且不声明 `policy.action_chunk`；`wave` 示例、SDK 模板只用 `arm.joint_position`；`ros2` 适配器不声明动作块。ux01 浏览器验收中 `wave` 正常运行。无回归。

### 2.4 R4b-04：延迟恢复

命令：`python3 -B grid.py restop2.py rs3_grid.txt rs3.jsonl 2 240`。“结束后 5 s”是套件返回后推进仿真 5 s 的路径。

| 驱动 | 延迟 | final_motion.still | 中止后再停 | 结束后 5 s | end_state |
|---|---|---|---|---|---|
| 每次停后恢复 | 2.5 s | false | 4 | 0.44 m | 警告…请立即使用硬件急停（**上轮写“持续静止”**） |
| 每次停后恢复 | 4 s | false | 2 | 0.49 m | 警告（上轮“持续静止”） |
| 每次停后恢复 | 6 / 9 s | true | 0 | 0.41 m | “在 5 s 观察窗内持续静止（观察窗之后才恢复的运动无法由本报告发现）” |
| 恢复两次 | 2.5 / 4 s | true | 1 | **0**（上轮 0.45 / 0.30 m） | 如实写“期间再停 1 次” |
| 恢复两次 | 6 / 9 s | true | 0 | 0.41 m | 同上“观察窗”措辞 |
| 恢复一次 | 2.5 / 6 s | true | 0 | 0 | — |
| dry 两种 | — | true | 0 | 0 | — |

结论：窗内的恢复全部被抓到；窗外恢复属有限观察窗的固有限制，措辞已不再被误读为“已安全”。保持 P2，待第 05 轮运行时锁存守护（锁存期间反馈有运动 → 周期 `adapter.stop()`）。

---

## 3. fix3 新机制专项（找新问题）

| 关注点 | 方法 | 结论 |
|---|---|---|
| 计数再停 × `stop_count` / `expect_stop_count`：操作员复位会不会永远失败？ | 代码审阅（`expect_stop_count` 只有套件使用：`conformance.py` 1062；桌面 brainos、SDK、cloud 都不发送）；`op_reset_inflight.py` | **不会。** 操作员复位不带 `expect_stop_count`，计数增加不影响它；套件返回后没有残留的再停循环。套件内部：任何 `CallTimeout` 直接中止，之后不再做条件复位，计数增长不会让套件卡在“预期计数不符”的循环里 |
| 套件会不会让机器人永久无法复位？ | `op_reset_inflight.py 4 12 0.5`、`4 25 0.5` | 不会：套件返回 0.5 s 后操作员复位成功（`latched:false`）。但见 R4c-01（并发复位与盘/内存不一致） |
| `keep_reason` 滥用 | 代码审阅 `gateway._stop` | `/v1/stop`、`/v2/stop` 都要令牌（`_authorized` 在分派前）。`keep_reason` 只在**已锁存**时生效，且只把原因写回为本次停止前的原因；停止本身照常计数、锁存。不能借此隐藏锁存或解锁。比较交换缺失见 R4c-02 |
| `_halt_seq` 竞态 | 代码审阅 `safety.py` 560–620 | `next(itertools.count)` 在 GIL 下原子；赋值乱序只会让值“不等于快照”（多中止一次复位，安全侧），不可能恰好回到快照值而漏检。复位期间能产生 `_adapter_stop` 的来源（迟到的 `after_dispatch`、`_restop_after_raced_reset`）都应当中止复位；看门狗在锁存期间 deadline 为 None 不会触发；复位要求无运行，正常操作员复位不会被误中止 |
| 机械臂包络误中止 | 54 例网格 + 代码审阅 | 仿真中正确声明 0 误中止；动作块逐行单调插值，`[start, final]` 包络成立。真机标定见 R4c-04 |
| joint_position 动作块拒绝是否破坏现有用户 | §2.3 | 无（仓库内无此类用户；`wave` 通过） |
| 最终复核 `_final_recheck` | 代码审阅 + `rs3` | 只在“已检查”时执行；看到运动用计数停止，如实降级 `still`；未中止的正常结束若看到运动会让套件在结束时锁存（安全侧） |

### R4c-01 · P2 · runtime · 被放弃的复位仍在适配器里时，操作员复位并发进入 `adapter.reset()`；旧调用返回后盘/内存锁存状态不一致

- **复现**：`python3 -B op_reset_inflight.py 4 25 0.5`（第 4 次复位卡 25 s，阈值 2 s；套件返回 0.5 s 后操作员授权复位）。
  - 操作员复位立即成功，`latched:false`（合理：人工授权）。
  - 约 11 s 后被放弃的 `adapter.reset()` 返回（并重放速度）：监督器发现 `_halt_seq` 变化 → `_abort_reset` → 立即 `adapter.stop()`（仿真里重放只积分到一个推进周期，显示 0.046 m，真实只有微秒级），然后 `persist(True, reason=None)`，**但不设置内存锁存**。
  - 结果：内存 `latched:false`、磁盘 `latched:true, reason:null`；事件 `safety.reset_raced` 的 note 是“复位返回后已再次停止适配器，保持锁存”；报告 end_state 写“运行时会在其返回后保持锁存并再次停止”。三者都与事实不符。重启后运行时会以“重启前已锁存”启动（安全侧但莫名其妙）。
  - 同时 `adapter.reset()` 被两个线程并发调用，适配器契约未声明这种并发。
- **影响**：不造成运动（迟到的复位之后监督器总会再停），但操作员在不知情时被停车、状态显示不一致。`hang=12`（旧调用在套件观察期内返回）时一切一致。
- **建议**：监督器记录“`adapter.reset()` 在途”，在途期间新的复位返回 409 `reset_in_progress`（报告/桌面提示“适配器上一次复位尚未返回”）；`_abort_reset` 只在仍锁存或本次复位确实是当前复位时写盘；事件区分“保持锁存”与“已被其他复位解锁，仅再次停止”。补用例。

### R4c-02 · P3 · runtime · `keep_reason` 不是比较交换

- `gateway._stop`：停止前读 `prior`，停止后读请求体（最长受请求超时约束），再 `annotate_stop_reason(prior)`。窗口内到达的操作员停止原因（如“person in path”）会被改回 `prior`。任何持令牌的客户端本来就能用普通 `/v2/stop {"reason"}` 覆盖原因，所以不是新权限；但套件黑盒再停本意是“保留操作员原因”，这里反而可能抹掉操作员更新的原因。
- 建议：只在当前原因仍等于本次停止写入的占位值（`"stop"`）时写回 `prior`。

### R4c-03 · P3 · runtime/docs · dry 不再能发现错误的 `joint_names`

- 网格中字母序/倒序声明 dry_ok=true（`dry_skipped`）。这是 R4b-02 选项 3 的代价，可接受；但桌面/云端把“dry 通过”显示为合格时，用户可能以为映射已验证。
- 建议：报告与 UI 写明“机械臂映射只在 motion 级验证”；脚手架 TODO 与 README 加上“`joint_names` 取自控制器配置（`ros2_control` 的 `joints:`），不要照抄 `/joint_states`”。

### R4c-04 · P3 · runtime · 新阈值需要现场标定

- dry 位移检查比较 `joint.state` **全部**关节（不只机械臂）与里程计偏航：腿足站立平衡时关节抖动、IMU 偏航漂移可能超过 0.01 rad → dry 中止。R4b-05 的偏航峰值 ≤ 0.2 rad/s（v=0.1 m/s）对小跑步态的偏航晃动偏紧；`arm_envelope_rad` 0.02 rad 对舵机机械臂的静差/抖动偏紧。全部是误中止（安全侧），仿真无此问题。
- 建议：按 `family` 给默认值、允许在清单/命令行覆盖，并在第一次真机现场验收中记录实测余量。

### R4c-05 · P3 · runtime · 动作块拒绝是异步的

- 未声明 `joint_names` 时 `policy.action_chunk` 先返回 run（`running`），1 s 内变 `failed` 带原因；不是同步 4xx。对 VLA 调用方，错误只在轮询运行时可见。建议提交时同步拒绝（400 `joint_names_required`）。

---

## 4. ux01（701da82）在修复后运行时上的验证与攻击

| 检查 | 方法 | 结果 |
|---|---|---|
| 单元测试 | `rr3/run_ux01.sh unit`（`unittest discover -s tests -p test_ux01_sim.py`） | **17/17 OK**（22.4 s） |
| 浏览器验收 | `rr3/run_ux01.sh browser`：`tests/ux01_browser_fixture.py` + `node tests/ux01_browser.cjs` | `{"ok":true}`：一键添加四足与机械臂、示例技能库、运行 patrol/wave 并观察、停止锁存；fixture SIGTERM 后 **0 个残留 `sim_child`** |
| 在真机上运行技能 | `ux01_probe.py`：注册一台**非仿真**机器人（地址指向同一仿真端点），`robots/run-skill` + `hardware_authorized:true` | 400“直接运行技能目前只对仿真机器人开放…” |
| 绕过授权 | 不带 `hardware_authorized`；`"hardware_authorized":"true"`（字符串） | 均 400“需要先勾选现场安全确认并授权” |
| 借 `request_id` 重放到另一台机器人 | 同一 `request_id` 先仿真后真机 | 400“请求标识已用于其他操作” |
| 把真机当仿真操作 | `sims/start`、`sims/remove` 用真机 id | 400“这不是仿真机器人” |
| 改仿真机器人地址指向真机 | `connections/configure` | 400“仿真机器人的地址和令牌由应用自动管理” |
| 设置 `sim` 标记 | 代码审阅：只有 `SimRobots.add` 写入；无通用合并接口（`add_robot`、`configure_connection`、engineering SSH 接入都不写 `sim`/`endpoint`） | 无入口（纵深防御见 R4c-06） |
| 跨站 | `Origin: http://evil.example`、`Origin: null`、`Sec-Fetch-Site: cross-site`、`Host: evil.example:<port>`（DNS 重绑定） | 全部 403“不允许跨站请求”；另有 `application/json` 强制与 `SameSite=Strict` 会话 Cookie |
| 孤儿进程 | 运行中 `kill -9` 桌面后端 | 仿真子进程 ≤ 2 s 退出（stdin 断开） |
| 原生文件夹选择器 | 代码审阅 `desktop/main.swift`（Linux 无法编译） | 不是 HTTP 接口，而是 `WKScriptMessageHandler`；只接受 `frameInfo.securityOrigin` 的 host/port 与本地控制台一致的消息；WebView 导航策略把非本地 URL 交给系统浏览器。跨站不可达；结果只回写到本页面 `window.nxFolderPicked` |
| 小隐患（未成实例） | 代码审阅 | `SimRobots.close()` 与进行中的 `start()` 竞争时，启动完成的子进程会在 close 之后登记；它仍随桌面进程退出（stdin 断开）而退出，不会成为真正的孤儿 |

---

## 5. 复现脚本（`rr3/`）

| 脚本 | 用途 | 结果文件 |
|---|---|---|
| `rr_common.py`、`reset_hang.py`、`arm_names.py`、`restop2.py`、`vel2.py`、`chunk_limiter.py`、`grid.py` | 上轮脚本（复制，逻辑未改，`NX_TREE` 指向本 worktree） | `rh3.jsonl`、`rh3_rt15.jsonl`、`arm3.jsonl`、`rs3.jsonl` |
| `op_reset_inflight.py` | R4c-01 | 命令行输出 |
| `chunk_refuse.py` | R4b-03 拒绝方式 / 名称重复 | 命令行输出 |
| `ux01_probe.py`、`run_probe.sh`、`run_ux01.sh` | ux01 攻击探测、孤儿进程、单测与浏览器验收 | `probe.out`、`ux01_unit.log`、`ux01_browser.log` |
| `run_full.sh` | 全量测试 | `full_*.log`、`full_summary.txt` |

## 6. 给总指挥的建议

1. 0.11.0 可以发布（无未关闭的 P0/P1）。
2. 第 05 轮优先：运行时锁存守护（R4b-04）；复位在途互斥与事件措辞（R4c-01）；`keep_reason` 比较交换（R4c-02）；现场阈值按 family 标定（R4c-04）。
3. 随手修：R4c-03 文案/脚手架、R4c-05 同步拒绝、R4c-06 运行前核对清单 `simulated`、R4b-06/07/09。
