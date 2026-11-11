# 第 07 轮发布前复验（0.14.0）：Q7e-01 / Q7e-02 / Q7e-04 修复

- 日期：2026-11-10；独立复验 Agent；被验：master `faedb03`（runtime `132fd76` + 契约 §9.5），基线 `v0.13.0`（`dbab711`）。
- 依据：`2026-11-10-iter07-release-review.md` 只因 Q7e-01（P1）阻塞，另 Q7e-02/04（P3）。
- 方法：从 `rel07/sc/` **复制**脚本到本 Agent 草稿目录 `rv07v/sc/`（未改原件）；`git archive v0.13.0` / `git archive HEAD` 各解出一份独立副本 `rv07v/v013`、`rv07v/head`；两版同脚本同种子，经 `wrap3.py` 记录每一次 `adapter.stop()`，`cmp4.py` 判定（最大真实位移 / 终点位移 / 首个 `stop()` 位移，容差 1.5 mm）。本轮新增 `adv6x.py`（42 个：读取在监视线程最后一次检查后 20 / 50 ms 返回，即中等粒度时钟；以及 1 h 周期阻塞）与 `my/ev_check.py`（Q7e-02 事件逐拍核对）。
- 协作规则 7–9：不改产品代码；所有命令带 `timeout`；每个进程用独立 `TMPDIR`；只结束本 Agent 记录的进程组；草稿按里程碑提交。

## 结论

**可以发布 0.14.0。** Q7e-01（P1）已修复：`adv6` 全部 262 个场景（含 26 个 `hang*`、26 个 `hangE*`）HEAD 与 v0.13.0 的真实位移**逐项相同**，上轮 3 个 `WORSE` 回到 v0.13.0 的值；补充的 42 个读取阻塞变体同样逐项相同。`stop()` 处处 ≤ 2 次/s、相邻 ≥ 0.5 s。Q7e-02、Q7e-04 抽查通过。单元测试全部通过（见 §3，首轮失败系本 Agent 的 `TMPDIR` 过长所致，已定位并以短路径复跑）。

| 项 | 场景数 | HEAD vs v0.13.0 | `stop()` 每 1 s 最多 / 最小间隔（HEAD） |
|---|---|---|---|
| `adv6`（去抖窗口、节拍边界、`hang*`/`hangE*`） | 262 | 262 `ok`，0 `WORSE`/`FIRST`；真实位移 262/262 相同；`stop()` 次数 259/262 相同 | 2 / 0.5 s |
| `adv6x`（本轮新增：阻塞返回滞后 20/50 ms、1 h 阻塞） | 42 | 42 `ok`；真实位移 42/42 相同 | 2 / 0.5 s |
| `adv3` | 33 | 33 `ok`；位移与 `stop()` 次数逐项相同 | 2 / 0.5 s |
| `adv4` | 31 | 31 `ok`；同上 | 2 / 0.5 s |
| `extra2` | 24 | 24 `ok`；同上 | 2 / 0.5 s |
| Q7e-02 事件 | 4 场景 / 约 540 条事件 | 全部带 `merged`；`merged:true` ⇒ `adapter_stopped:false`；非汇总的 `merged:false` 事件均在同一拍确有 `stop()` 调用且 `adapter_stopped:true` | — |
| Q7e-04 文档 | — | `hal.py` 模块说明与 `stop()` 文档串、README、契约 §9.5 均写明 ≥ 0.5 s 间隔、不得 > 0.5 s 去抖、高分辨率时钟 | — |
| `runtime/tests` | 485 | 见 §3 | — |
| `tests/` | 283 | 见 §3 | — |

## 1. A/B 复跑

### 1.1 Q7e-01 原失败行

| 场景 | v0.13.0 | 上轮 HEAD（`08fdf61`） | 本次 HEAD（`faedb03`） |
|---|---|---|---|
| `hang1.5_every20_p3.0_fw1.5` | 1.1761 m | 1.2297 m（WORSE） | **1.1761 m** |
| `hang3.0_every20_p3.0_fw1.5` | 1.2897 m | 1.3281 m（WORSE） | **1.2897 m** |
| `hang10.0_every20_p3.0_fw1.5` | 2.1291 m | 2.1842 m（WORSE） | **2.1291 m** |

`stop()` 次数仍与 v0.13.0 略有差异的 3 个（`hang1.5_every20_p3.0_fw1.5` 414→419、`hang10.0_every20_p3.0_fw1.5` 1432→1431、`hangE1.2_every20_p3.0_fw1.5` 384→386）及 `adv6x` 的 2 个（`hangC0.02/0.05_1.2_every20_p3.0_fw1.5` 多 2 次），真实位移、终点、首个 `stop()` 位移均与 v0.13.0 完全相同：差别只在合并到读取阻塞再停的那一次是否另算一次调用，不影响按住效果，也都在 0.5 s 节奏内。

### 1.2 粗时钟 / 中等时钟

- `hang*`（阻塞再停与阻塞后第一个采样**同一机器人时刻**，即 Q7e-01 的粗时钟情形）26 个、`hangE*`（滞后 1 ms，高分辨率时钟）26 个、`hangC0.02*`/`hangC0.05*`（滞后 20 / 50 ms）40 个：全部与 v0.13.0 位移相同。
- 1 h 周期阻塞（每 20 s 阻塞 1.5 s，伪造游走，停后 0.6 s 再走）：同刻 21.4483 m、滞后 1 ms 21.5813 m，均 = v0.13.0；`stop()` 4813 / 4827 次与 v0.13.0 相同。

### 1.3 `stop()` 节奏

五组共 392 个场景，HEAD 任意 1 s 窗口内 `stop()` 最多 **2** 次，相邻最小间隔 **0.5 s**（粗时钟 `hangC0.05_*_none` 中为 0.55 s，受 50 ms 量化）。

## 2. Q7e-02 / Q7e-04 抽查

- **Q7e-02**：`my/ev_check.py` 逐拍运行（IMU 漂移 3 倍容许 1 h、伪造游走停后 1.5 s 再走 10 min、3 mm/s 蠕动 10 min、5 cm/s 不服从 2 min），检查 `safety.motion_while_latched` / `safety.feedback_invalid_while_latched` / `safety.stop_not_obeyed`：缺 `merged` 0 条；`merged:true` 而 `adapter_stopped` 非 false 0 条；非汇总事件 `merged:false` 而该拍没有 `stop()` 调用 0 条；`merged:false` 而 `adapter_stopped` 非 true 0 条。`web/`、`desktop/`、`cloud/`、`sdk/` 无代码以 `adapter_stopped` 判定告警，语义变更无下游破坏。
  - 备注（不计问题）：限频汇总事件（`summary: true`，`count` > 1）的 `merged`/`adapter_stopped` 取被汇总的最后一次再停，与其它载荷字段一致。
- **Q7e-04**：`runtime/neuxmind_runtime/hal.py` 模块说明新增 “Repeated stop() calls” 段（≥ 0.5 s、最多 2 次/s、驱动不得在 > 0.5 s 窗口内去抖/丢弃）与 `clock` 高分辨率要求；`RobotAdapter.stop()` 文档串同述；`runtime/README.md` “对驱动的要求”、`INTERFACES_V1.md` §9.5 一致。ROS 2 适配器 `stop()` 无去抖、`clock = RealClock()`，符合。

## 3. 单元测试

HEAD 副本 `rv07v/head`，`PYTHONPATH=$PWD/spec:$PWD/runtime:$PWD/cloud:$PWD/sdk:$PWD timeout 1800 python3 -m unittest discover -s <dir>`，两目录并行：

| 目录 | 结果 | 用时 |
|---|---|---|
| `runtime/tests` | **485 个 OK**（含新增 `test_iter07_fix5_runtime`） | 1593 s |
| `tests` | **283 个 OK**（skipped=39） | 89 s |

首轮（与 A/B 并行）`runtime/tests` 69 失败 + 1 错误（全部是子进程沙箱类：`test_sandbox` 47、`test_skills` 13、`test_async_evaluate` 6 等，“技能进程启动失败（退出码 2）”），`tests/` 2 失败（`test_ux01_sim` 两条“位置/关节没有变化”）。定位：本 Agent 把 `TMPDIR` 设到草稿目录下的长路径；同一用例 `TMPDIR` 改为短路径（`/tmp/rv07v_*`）即通过。属测试环境问题，非产品缺陷；以短路径复跑结果为准（上表）。

## 进度

- 19:47 两版 A/B 后台启动；19:49 Q7e-02 抽查完成；20:13 A/B 全部完成；20:21 首轮单元测试（失败，`TMPDIR` 过长）并定位；20:21 短路径复跑启动；20:48 全部完成，进程组已清空，临时目录已删除。
