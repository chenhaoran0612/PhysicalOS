# Agent 实时进度 · 第 04 轮

> 总指挥约每 15 分钟同步。生成时间：2026-11-09 22:24 PST

| Agent | 任务 | 状态 |
|---|---|---|
| 审查 | 第 04 轮独立审查 | ❌ 不可发布：2 P0 / 7 P1 / 8 P2 / 5 P3（一致性测试会放过不合格本体，且测试过程可造成危险运动） |
| 端侧运行时 | 修 Q4-01/02（P0）、Q4-03..07、Q4-09 脚手架注入 | 🟡 修复中（最高优先级） |
| SDK + 云端 | 修 Q4-08（cloud 测试回归）、SDK 注入纵深防御 | ✅ 已修复（cloud 211 SQLite+PG、sdk 84） |
| 桌面集成 | 修 Q4-20（测试竞态）、报告结论一致性测试、中止状态显示 | ✅ 已修复（184 测试，含真实 Codex） |

## 提交记录（第 04 轮）
```
7d9ac32 docs: correct the iter07 release-verify note on the skill launch failure (0700 parent, not path length)
afd77af iter08(runtime): skill workspace in a temp dir the skill user can traverse
fc4a6d7 iter08(runtime): WIP test_models redirect fixture percent-encodes non-ASCII file:// Location
0d8de8b iter08(runtime): WIP docs for skill workspace temp-dir selection
9b1a643 iter08(runtime): WIP tests for long/non-ASCII/private TMPDIR and state dir
efbe26e iter08(runtime): WIP skill workspace base must be traversable by the skill user
34064dc release 0.14.0: round 07 — latch guard survives real sensor noise and is never weaker than 0.13.0
faedb03 iter07(docs): contract §9.5 (read-hang restop merge, HAL clock/debounce requirements)
132fd76 iter07-fix5(runtime): Q7e-01 read-hang restop never inferred from equal robot times; Q7e-02 merged events; Q7e-04 HAL docs
3ac218d iter07-fix5(runtime): WIP HAL/README docs for Q7e-04 (stop() debounce <= 0.5 s, high-resolution clock), Q7e-02 event semantics
bfa9ac4 iter07-fix5(runtime): WIP Q7e-01 read-hang restop source, Q7e-02 merged events
6d8af00 review(iter07-release): not releasable — read-hang restop vs replica on coarse adapter clocks (Q7e-01 P1)
dc4d52d iter07(docs): contract §9.4 (one shared stop cadence for all guard stops)
5a0b9bd Merge branch 'iter07-fix4-runtime'
08fdf61 iter07-fix4(runtime): one stop() cadence for every latch-guard stop (Q7d-01 P1, Q7d-02 P2)
9cae6a0 iter07-fix4(runtime): WIP docs (README, EVOLUTION_ENTRY) and A/B results
717bae0 iter07-fix4(runtime): WIP one stop() cadence for the latch guard (Q7d-01/02) + tests
e8855fb review(iter07-final-rereview): not releasable — guard and replica stop() cadences interleave (Q7d-01 P1, Q7d-02 P2)
643549a iter07(docs): contract §9.3 (precautionary stop replicates v0.13.0, drift notice per latch, reset_expected_s ≤ 25)
a90828f iter07-fix3(runtime): precautionary stops replicate v0.13.0 exactly (Q7c-01), drift notice per latch (Q7c-02), reset_expected_s (0, 25] (Q7c-07)
b7429ef iter07-fix3(runtime): WIP noisy creeper test covers held-still and counted rule
563208f iter07-fix3(runtime): WIP docs (README, EVOLUTION_ENTRY)
bafd073 iter07-fix3(runtime): WIP regression tests Q7c-01/02/07
1fd224e iter07-fix3(runtime): WIP v0.13.0 replica precautionary stops, drift notice per latch, reset_expected_s (0, 25]
d498d79 review(iter07-final): final review draft — not releasable: precautionary stop lets stop-then-resume drivers go further than v0.13.0 when walk is credited (Q7c-01)
ea21613 ux05-fix3(desktop): browser acceptance on the runtime's real reset_expire / reset_expected_s and drift notice
d98e3fe iter07(docs): contract §9.2 (release rule vs v0.13.0, yaw step, precautionary stop, drift notice, reset_expected_s)
b080be6 desktop: show the runtime's one-shot latched drift notice in the latch-guard panel
0075c61 Merge branch 'iter07-fix2-runtime'
69bcbff iter07-fix2(runtime): once-per-latch safety.latched_drift_notice; spec validates safety.reset_expected_s
abe0650 iter07-fix2(runtime): latch guard at least as safe as v0.13.0 in every reviewed scenario (Q7b-01/02/03/09), float params 1 == 1.0 (Q7b-04), reset capabilities + reset_expected_s (Q7b-05/06)
dd1f657 ux05-fix2(desktop): float options typed on every send path (Q7b-04); reset limit per robot and truthful late-reset wording (Q7b-05/06/08); relaxed_keys (Q7b-07); safety watch across projects, no pause while latched (Q7b-10)
8c891d3 review(iter07-fix): re-review of round-07 fixes — not releasable: yaw ratchet unbounded drift allowance (Q7b-01), 5 cm per stop-wait-resume cycle (Q7b-02)
aab8a26 iter07-fix: desktop sends expire_s so a late reset is void (Q7-10); truthful hung-reset wording; contract §9.1
165fe1d Merge branch 'iter07-fix-runtime'
a0c2fce iter07-fix(runtime): latch guard yaw hard bound, ratchet + streak escalation, relaxed calibration, reset expiry, param gaps
5ccbd26 ux05-fix(desktop): global hardware e-stop banner for real robots, typed float options, simulated wording (Q7-03, Q7-04, Q7-11)
0df6555 review(iter07): independent review of round 07 (robust latch guard, calibration, stop_not_obeyed, ux05) — releasable 0.14.0, 4 P2
cdbb772 iter07(docs): contract §9 (robust latch guard, field calibration, stop_not_obeyed, parameter constraints)
90eeda7 Merge branch 'iter07-runtime'
de873b0 iter07(runtime): Q6-09 simulated runtime exits cleanly (sim_child parent watch reads the raw fd)
6a91a6e iter07(runtime): latch guard survives real sensor noise/IMU drift, field calibration, aggregated events, stop_not_obeyed escalation; parameter constraints fail closed for all types (Q6-01/02/04/05/06/08)
b220885 ux05(desktop): typed options with labels, distinct parameter refusals, latch guard in plain words, first-run guide (Q6-07, Q6-08, Q6-11)
dbab711 release 0.13.0: round 06 — truthful safety refusals, latch guard sees creep and blinded feedback
5da149a ux04: a hung reset is still running — say so, warn it may unlatch, offer stop to cancel (Q6-03)
0bac90b review(iter06): independent review of round 06 (latch guard drift/invalid feedback, device-side parameter ranges, ux04) — releasable 0.13.0, 3 P2
f369b56 iter06: contract §8; example skills declare parameter ranges
48a07a7 Merge branch 'iter06-runtime'
abab971 iter06(runtime): latch guard sees slow creep and blinded feedback, device-side skill parameter ranges, reset way out
637d63d ux04(desktop): structured error classification, reset way out, parameter refusals on the field
67a44f7 release 0.12.0: round 05 — user-path audit and fixes, latched-state guard
40455a9 review(iter05): independent review of round 05 (runtime latch guard, ux02/ux03) — releasable 0.12.0, 5 P2
7ea2e3c iter05(examples): plain-language titles, descriptions and units for example skill parameters
6f89bba ux03(desktop): close the ux02 audit leftovers; make legacy browser tests deterministic
c613fe0 iter05(docs): contract for latch guard, serialised reset, keep_reason CAS
d9fb465 Merge branch 'iter05-runtime'
b96677f iter05(runtime): latched-state guard, serialised resets, keep_reason CAS, sync chunk refusal, scaffold joint_names
cdfc8e4 ux02(desktop): user-path audit and fixes for a first-time user with a simulated robot
eafb6e5 release 0.11.0: round 04 — conformance suite safe under lying adapters, desktop works out of the box
8567e85 iter04(review): third re-review of fix3 (fbd9f37) and ux01 (701da82): releasable
4e92686 iter04(docs): contract for fix3 (counted re-stop, keep_reason, dry sends no arm targets)
fbd9f37 iter04-fix3(runtime): timed-out reset never unlatches, dry sends no arm targets, joint chunks fail closed without joint_names
701da82 ux01(desktop): one-click simulated robots, example skill gallery, run and watch a skill, all inside the app
58e8612 iter04(review): second re-review of fix2 (df7600e / 7a696c4)
7a696c4 iter04(docs): contract for re-review fixes (joint_names, stop_count, report fields)
df7600e iter04-fix2(runtime): arm joints mapped only by declared joint_names, stop again whenever motion follows a stop, re-stop after a raced reset, wz/vy velocity checks, bounded adapter calls
57bad6c iter04(review): independent re-review of the conformance fixes
d4e121f demo: one-command demo launcher and Chinese demo guide
ea81a96 iter04-fix(runtime): conformance suite never clears a foreign e-stop, dry never moves, every motion failure stops and latches; scaffold injection-proof
1767b59 iter04(docs): conformance digest binding, Q4-22 decision on model.install dry semantics
caba7ef iter04-fix(sdk): validate scaffold input from robots (Q4-09), refuse uploading old reports to a new manifest (Q4-12), trust boundary and motion amplitude notes (Q4-13/18/19)
eec83dd iter04-fix(cloud): tests follow posture dry check (Q4-08), exact manifest_digest binding (Q4-12), no future generated_at (Q4-17), self-report disclaimer (Q4-13)
a75dc6e iter04-fix(desktop): deterministic session-busy test, exit-code agreement tests, aborted verdicts, tamper screen, offline conformance
0d765f1 docs: ADR-019 conformance testing must never be a hazard
5f78153 iter04(review): independent review
e8f67fe iter04(docs): manifest digest binding and new dry checks
f4a703f iter04(sdk): tests follow posture dry check from runtime b0e53c7
b0e53c7 iter04(runtime): manifest digest in report, dry checks for model.install and posture, time-scale robust motion checks
a5005d3 iter04(desktop): align adaptation workflow with runtime conformance and scaffold
4025376 iter04(docs): SDK and cloud conformance contract details
bcd166a iter04(sdk): progress log (final regression)
f0eacde iter04(cloud): fleet conformance summaries, require_level for model and skill compatibility
ead00a8 iter04(sdk): nx adapter new|test, nx robot conformance [--upload], require_level, install progress from the run
83f8577 iter04: capability standard v1.1 (gripper.state, arm.ee_state); contract §6 implementation details
09c1297 iter04(runtime): capability conformance suite, adapter scaffold, ROS 2 test double, model.install progress
d8c0157 iter04(desktop): robot adaptation workflow template (§6.2) and a product-side stop bound (G-2)
```

## runtime/PROGRESS.md（最近 10 条）
2026-11-09T19:34:56-08:00 fix5: A/B adv6/adv3-5/extra/extra2 done (no WORSE), adv2/adv running; spec/runtime/cloud suites OK
2026-11-09T19:46:33-08:00 fix5: done — A/B 472 comparable no WORSE vs v0.13.0; all suites OK; final commit
2026-11-09T20:51:23-08:00 iter08-paths runtime: start; worktree wt-iter08 branch iter08-paths; reproducing long-TMPDIR failure
2026-11-09T20:56:06-08:00 iter08-paths runtime: root cause = privilege-dropped workspace base under TMPDIR with 0700 ancestor (not length/AF_UNIX); fix + tests committed; tests/ green with long 0700 non-ASCII TMPDIR; runtime suite running
2026-11-09T21:06:15-08:00 iter08-paths runtime: long-TMPDIR runtime suite still running, 0 failures so far
2026-11-09T21:21:11-08:00 iter08-paths runtime: long-TMPDIR runtime 485 run: 484 ok + 1 flaky (test_models redirect, local server RST under load; passes on rerun incl. long TMPDIR); tests/ 283 ok; starting normal-path full suites
2026-11-09T21:23:51-08:00 iter08-paths runtime: normal-path 5 suites running
2026-11-09T21:50:35-08:00 iter08-paths runtime: normal suites green (spec17/runtime491/cloud211/sdk84/tests285 skip39); long: tests285, cloud211, spec17, sdk84 (venv preferring worktree runtime) green; final long runtime rerun started
2026-11-09T22:00:38-08:00 iter08-paths runtime: long runtime rerun in progress, 0 failures so far
2026-11-09T22:17:25-08:00 iter08-paths runtime: done — long non-ASCII private TMPDIR: runtime491/tests285/cloud211/spec17/sdk84 OK; normal paths all OK; final commit

## cloud/PROGRESS.md（最近 10 条）
- 最终：cloud SQLite 160 OK、PostgreSQL 160 OK；sdk 31 OK（5 次中 2 次 `test_robot_client.test_cancel_and_wait_timeout` 偶发失败——SDK 对端侧运行时的计时测试，runtime Agent 正同时改 runtime/，与 cloud 无关）；spec OK；仓库根 tests OK（跳过 20）
- 清理：临时 PG 集群已停止并删除；worktree 已移除

## 第 02 轮审查修复

- 读审查报告 Q2-02/03/06 与 cloud_deadline.py；方案：墙钟看门狗 + 行级截止；保留命名空间（error→upstream.error、neuxmind.* 丢弃）+ WHATWG 切行；`*.delta` 全计；Echo responses 形状
- 代码完成后 SQLite 160 仍通过；补测试时发现自己的缺陷（上游 error 事件后正常结束 → 误归 usage_missing），已修
- SQLite 174、PostgreSQL 16（临时集群 55433）174 通过；cloud_deadline.py 0.5 s 结束（原 3.0 s）
- worktree 变异 18 个：F01/F08/F09/F13/F14/F18 首轮存活 → F18 是死代码（已删）；F08/F09/F13/F14 补测试后被杀；F01 等价（看门狗的 abort_reason 行级检查覆盖），保留为纵深防御
- sdk/tests 48 通过 ×2；临时 PG 与 worktree 已清理

## brainos/PROGRESS.md（最近 10 条）
- 10:45 ux05-fix2：浏览器 9 项 + ux04_errors 125 项通过，无遗留进程；开始五套全量单测（后台）
- 11:16 ux05-fix2：五套全量单测全绿（spec 15、runtime 453、cloud 211、sdk 84、tests 282/跳过 39）；浏览器 9 项 + ux04_errors 通过；无遗留进程；提交
- 12:39 ux05-fix3：开始（夹具 reset_expected_s、真运行时 reset_expire 主断言、漂移提示浏览器验收）
- 12:46 ux05-fix3：夹具加 reset_expected_s=7 与急停后滑移的机器人（creep）；ux05_fix2 浏览器验收改为真运行时主断言 + 旧运行时打桩，漂移提示段落调试中
- 12:51 ux05-fix3：ux05_fix2 浏览器验收通过（真运行时作废 + 旧运行时打桩 + 漂移提示）；九项浏览器回归后台运行中
- 12:56 ux05-fix3：浏览器 9 项通过（ux05 迟到复位断言改为真运行时措辞）；五套全量单测运行中
- 13:06 ux05-fix3：全量单测进行中
- 13:16 ux05-fix3：全量单测进行中（spec/runtime 已过的见日志）
- 13:26 ux05-fix3：全量单测进行中（runtime 套件较长）
- 13:27 ux05-fix3：五套全量单测全绿（spec 17、runtime 470、cloud 211、sdk 84、tests 283/跳过 39）；浏览器 9 项通过；无遗留进程；提交

## sdk/PROGRESS.md（最近 10 条）
- 18:55 变异 38 个：35 杀死；S11（首次上传不登记）、S19（未声明能力不入表）补测试后杀死；S02（CLI 本地 motion 检查）与 RobotClient 的检查重复，等价；worktree 已删除 / 进行中 / 最终全量回归
- 18:57 cloud 最终：SQLite 208、PG16 208 通过；临时 PG 集群已停止并删除 / 进行中 / sdk 3.13+3.10、runtime/spec 回归
- 19:00 sdk 最终：3.13 81、3.10 81 通过 / 进行中 / runtime 回归（后台）
- 19:04 已提交 ead00a8 iter04(sdk)、f0eacde iter04(cloud)；runtime 304、spec 11 通过；tests/ 回归运行中 / 进行中
- 19:05 tests/ 167 个通过（跳过 38，桌面 Agent 工作区状态）/ 完成 / 交付（PROGRESS 这一行未提交，留作工作区记录）
- 19:58 第 04 轮审查修复开始：读 reviews/2026-11-09-iter04-review.md（Q4-08/09/13 及 sdk/cloud P2/P3）/ 进行中
- 20:02 修复代码完成（cloud：报告 manifest_digest 必须匹配/缺失不采信、未来 generated_at 拒绝、测试跟进 posture dry；sdk：脚手架输入白名单+失败清理、--id --upload digest 检查、motion 提示、信任边界说明）；共享工作区 runtime 有 runtime Agent 半成品（conformance.py 未提交，Suite.k/stop_count 报错），sdk 上传类测试因此失败 → 改在 HEAD 的 worktree 中验证 / 进行中
- 20:03 worktree scratchpad/sdk04/wt2（HEAD 0d765f1 的已提交 runtime + 本次 sdk/cloud 改动）中跑全量 / 进行中
- 20:03 临时 PG16 集群重建（/tmp/nx-sdk04-pg :55434），wt2 全量后台运行
- 20:11 wt2（HEAD 已提交 runtime）：cloud SQLite 211、PG16 211、sdk 3.13 84、3.10 84 全过；变异 11 个（C04 复验 + 10 个新）全部杀死；worktree 与临时 PG 已清理。共享工作区 runtime 仍有未提交半成品，sdk 上传类测试在共享区会因此失败，待 runtime 提交后重跑 / 进行中 / 提交
