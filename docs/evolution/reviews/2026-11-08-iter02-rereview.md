# 第 02 轮发布闸门·复审报告（实证沙箱攻击）

- 复审对象：提交 `34ea61c`（master）的技能进程沙箱——`runtime/neuxmind_runtime/{sandbox,sandbox_child,runner,skills,bus,safety}.py`，以及上一份审查 `docs/evolution/reviews/2026-11-08-iter02-review.md` 列出的修复（Q2-01…Q2-10）。
- 方法：**以真实攻击者身份编写可运行的恶意技能包**，经真实 HTTP 网关安装（`POST /v2/skills/install`）并运行（`POST /v2/skills/{id}/run`，`hardware_authorized=true`），对真实运行时的技能进程沙箱做逃逸/越权/阻断实验。沙箱边界（子进程、降权、socketpair 协议、能力总线、安全监督、停止通道）全部为真；仅 HTTP 客户端与**仿真**适配器观测在测试进程内。另以真实 CLI 子进程 `python3 -m neuxmind_runtime serve --adapter sim_quadruped` 做端到端 HTTP 安装/运行/停止确认。
- 运行环境：`/home/claude/robot-os`，Linux，root 启动（技能降权为 `nobody`，uid 65534），Python 3.13，可写 pids cgroup v1 可用。
- 主工作区零改动：所有攻击只是经 HTTP 安装/运行技能，不改动任何产品代码；未改动产品源码，故未在 worktree 中做变异（本轮无需变异测试）。攻击脚本与技能包在 `scratchpad/rereview02/`（仓库外）。本报告是唯一新增文件。
- 这弥补了上一份审查如实声明的缺口：A 部分此前只有"代码推理"，未编写可工作的攻击技能。本轮为实证。

## 基线测试

| 套件 | 结果 |
|---|---|
| `spec/tests` | 11 通过 |
| `runtime/tests` | 180 通过（约 61 s）※ |
| `cloud/tests`（SQLite） | 174 通过 |
| `sdk/tests` | 48 通过 |
| `tests/` | 123 通过，20 跳过 |

数量较上一份审查上升（runtime 171→180、cloud 160→174、tests 113→123），为本轮新增的回归测试（Q2-01/Q2-04/Q2-05/Q2-08 等）。
※ 五套件首次串行全跑时 `runtime/tests` 偶发 1 例失败；随后单独重跑 3 次均 180 全过，判为负载下计时敏感断言的偶发抖动，非缺陷（见"建议"4）。

## 总评：可否发布 0.9.0

**可以按 0.9.0 发布（仍限仿真本体 + 受控云端 + 第一方技能），不存在 P0/P1。** 六类攻击的核心安全不变式在实证下全部成立：

- **无未受监督/旧代数/超限的运动指令到达适配器**：所有伪造管道消息（10 种）均致 0 条运动指令、进程被杀、运行判 `failed`；超限速度被安全监督器限幅到 `max_vx=1.0` 后才下发，NaN 被拒；停止后旧代数调用 0 条到达适配器。
- **停止永不被延迟或阻断**：死循环屏蔽 SIGTERM、不读回复灌管道、fork 炸弹+setsid 逃逸三种恶意技能运行期间，HTTP `/v2/stop` 均在 6–91 ms 返回并锁存，机器人在停止返回前已置零速度，子进程在 `kill_grace_s` 后被 SIGKILL，运行时保持健康。
- **机密与提权**：技能读不到 `BRAIN_EDGE_TOKEN`（环境无此项）、读不到运行时 `/proc/<pid>/{environ,mem,maps}`、读不到状态目录与 sqlite（均 PermissionError）；`setuid(0)`、`ptrace(runtime)`、抬高 RLIMIT 全部被拒；fd 表仅 `{0,1,2,协议套接字}`；`NoNewPrivs=1`。
- **Q2-01 与 Q2-04 修复实证通过**；无进程/cron/受保护目录的持久化。

**唯一新发现 Q2R-01（P2）**：当运行时把所有技能降权到**同一个共享账户**（root 部署下的 `nobody`，或 `neuxmind-skill`——同样是单一共享账户）时，**两个并发技能可互相读取并篡改对方的私有工作区**（`tmp` 为 0700 但属主即该共享 uid）。已端到端复现：攻击技能无需任何外部信息，经 `/proc/<对端pid>/cwd` 还原对端工作区精确路径，读走对端私有 `TMPDIR` 中的机密，并将其覆盖（受害技能随后读回被篡改的值）。

该缺陷**不**触及运行时、令牌、安全监督或停止通道，且 0.9.0 范围内所有技能均为第一方，故非发布阻断项。但它正是 ADR-017 设立进程沙箱所针对的**不可信/第三方技能**场景中的多租户隔离失败，必须在第三方技能市场上线前修复——与已推迟到第 05 轮的"网络未隔离"属同一闸门。

## 攻击结果表

| # | 类别 | 攻击 | 实证结果 | 结论 | 严重度 |
|---|---|---|---|---|---|
| 1a | 驱动绕监督 | 丢弃 import guard、指向运行时源码、`gc.get_objects` 找活对象/适配器方法 | 仅得源码副本；`live_objects=[]`，无任何适配器/总线活对象 | 被阻止 | — |
| 1b | 驱动绕监督 | dup 协议 fd，注入 10 种伪造帧（伪造回复、错号、重放 id、未知 op、夹带 `hardware_authorized/epoch`、`args` 非对象、NaN、超长、假 done、假 grant） | 全部 `failed`+`sandbox.violation`，**0 条运动指令到达适配器**，进程被杀；其后 `/v2/stop` 6 ms | 被阻止 | — |
| 1c/1e | 驱动绕监督 | 经合法 `ctx.call` 下 `vx=999 / wz=999 / vx=NaN` | 适配器只收到限幅后的 `vx=1.0`（=`max_vx`），NaN 被 `ValueError` 拒 | 被阻止（限幅） | — |
| 1d | 旧代数 | 技能循环驱动，驱动中 stop，再 reset，继续驱动 | stop 7 ms；stop 之后 0 条运动指令到达适配器；运行中 reset 返回 409 `runs_active`；运行判 `stopped` | 被阻止 | — |
| 2a | 阻断停止 | 屏蔽 SIGTERM + 先驱动再死循环占 CPU | stop 17 ms 返回并锁存；机器人在 stop 返回前置零速度；子进程被 SIGKILL | 被阻止 | — |
| 2b | 阻断停止 | 不读回复、持续灌协议管道 | stop 7 ms；子进程被杀；`/v1/status` 200 | 被阻止 | — |
| 2c | 阻断停止 | fork 炸弹 + `setsid()` 逃逸 | stop 70 ms；并发任务封顶在 `extra_tasks`（峰值 32）后回落；子进程树被杀；运行时健康 | 被阻止 | — |
| 3 | 机密/提权 | 读 token、运行时 `/proc`、状态目录/DB、继承 fd；`setuid(0)`、`ptrace`、抬 rlimit | token=None；运行时 environ/mem/maps、状态目录/DB 均 PermissionError；setuid/ptrace 拒绝、rlimit 拒绝；fd 仅 4 个 | 运行时隔离：被阻止 | — |
| 3′ | 跨技能 | 并发对端技能的私有工作区读/写（见 Q2R-01） | 经 `/proc/<对端>/cwd` 还原路径 → 读走对端 `tmp` 机密 → 覆盖成功 | **部分成功** | **P2** |
| 4 | Q2-01 复验 | 技能在各可写位置植入 `rt-<死PID>` 符号链接；另起运行时触发启动清理 | 技能无法在 base/root/rt/run 目录创建条目（0711，仅本进程 tmp 可写）；清理不跟随植入的符号链接，受害目录权限与内容不变 | 被阻止（已修复） | — |
| 5 | Q2-04 复验 | 模拟无可写 cgroup（`PidsCgroup.base→None`），`setsid` 逃逸后睡眠 | `pids_cgroup=false`、`child_subreaper=true`、`process_containment=best-effort`；逃逸进程在运行结束后被收割，无僵尸 | 被阻止（如设计） | — |
| 6 | 持久化 | 写系统目录/cron；fork setsid 守护进程 | `/var/lib`、`/etc`、cron 目录均 PermissionError；守护进程被收割；无遗留进程/僵尸；工作区清空；仅在**全局可写** `/tmp` 留下一个文件（预期，见下） | 基本被阻止 | P3（见 Q2R-02） |

端到端 CLI 子进程（真实 `serve`）复测：`install` 200、`run` 200、`stop` 200 锁存、停止延迟 6 ms，运行后无遗留 `nobody` 进程。

## 新发现

### Q2R-01 · P2 · runtime · 并发技能共用降权账户，可互读互写对方私有工作区（已端到端复现）

- **位置**：`runtime/neuxmind_runtime/sandbox.py`
  - `resolve_user`（113–131）：root 部署时所有技能统一降权到单一账户（`neuxmind-skill` 或 `nobody`）；
  - `_prepare_workspace`（764–780）：每运行私有 `tmp` 以 `mode=0o700` 创建后 `os.chown(tmp, user[0], user[1])`——属主即该共享 uid；`pkg` 为 0555；
  - `_make_root`/`_base_dir`（375–423）：base 0711、run 目录 0711——**依赖路径保密**来隔离对端。
- **问题（根因）**：进程间隔离只靠 uid，而所有技能 uid 相同；run 目录虽 0711（不可列目录）使路径看似不可枚举，但：
  1. 攻击技能可遍历 `/proc`，对同 uid 的对端进程 `readlink /proc/<pid>/cwd` 直接拿到对端工作区 `.../run-*/pkg` 的**精确路径**（`mkdtemp` 的随机后缀被泄露）；
  2. 由此推出同级 `tmp/`（0700，属主为同一 `nobody`）——攻击者即 `nobody`，故**可读可写**；`pkg` 为 0555 本就可读。
- **实证**（`scratchpad/rereview02/{enum_probe,xskill_exploit}.py`）：受害技能把机密写入自己的 `TMPDIR`；攻击技能（无任何外部输入）：
  ```
  stole    {".../run-2027fc308520-.../tmp/secret.txt": "PEER-PRIVATE-9F3A-CONFIDENTIAL"}
  tampered {".../run-2027fc308520-.../tmp/secret.txt": true}
  受害技能读回: "TAMPERED-BY-PEER-SKILL"
  ```
  可达性：运动类运行被 runner 串行化（同一时刻仅一个），但**非运动技能可并发**，且非运动技能可与运动技能并发，故两技能并存是正常可达状态。
- **影响**：并发技能间的机密泄露与完整性破坏。攻击技能可窃取对端在运行期拉取/暂存于 `TMPDIR` 的机密（如技能自云端取得的令牌、模型工件、处理中的 PII），并篡改对端工作数据。被篡改的数据仍须经安全监督器，无法越限，但可在安全范围内改变对端行为（如改导航目标、替换姿态/模型输出）。不触及运行时、令牌、安全监督、停止通道。
- **严重度**：P2。0.9.0 范围（第一方技能、仿真本体、受控云端）内所有技能可信，非阻断项；但**第三方技能市场上线前必须修复**，与"网络未隔离"同闸门。高于 P3 是因为有可工作的端到端泄露+篡改实证，而非文档问题。
- **修复建议**：
  1. 真正修复是**每并发运行独立 uid**：预建 `neuxmind-skill-0..N` 账户池或动态分配 uid；或为每运行加 mount namespace + 独立 tmpfs（与第 05 轮网络命名空间一并做）。单纯隐藏路径不够——即便去掉 `/proc` 泄露，同 uid 下 0700 的 `tmp` 仍互相可读。
  2. 把第 05 轮的"网络隔离"闸门**扩展为"网络 + 文件系统/uid 隔离"**；
  3. 在 `runtime/README.md` §2.2「剩余信任边界」**立即补记**：并发技能共用降权账户、彼此工作区不隔离（当前仅就 NPROC 配额提了共用账户，未就机密/完整性提及）。

### Q2R-02 · P3 · runtime · 降权子进程对同 uid 对端可被 dump（`/proc` 路径与环境泄露）；`/tmp` 残留

- **位置**：`sandbox_child.py:main`（380–397）只对运行时父进程调用 `make_parent_non_dumpable`（`sandbox.py:172`），**子进程自身未设 `PR_SET_DUMPABLE=0`**。
- **问题**：实测同 uid 对端可读 `/proc/<子进程>/{cwd,environ}`（`environ="readable:64"`），这正是 Q2R-01 的路径发现手段。另外技能可在全局可写 `/tmp` 留文件（实测 `/tmp/nx_attack_persist.txt`），运行结束不清理——这是无 mount namespace 的预期后果。
- **严重度**：P3（纵深防御 / 与 Q2R-01 同根因）。
- **修复建议**：子进程在降权后 `prctl(PR_SET_DUMPABLE, 0)` 关闭 `/proc` 泄露（可消除路径发现，但不替代 Q2R-01 的独立 uid/命名空间修复）；`/tmp` 残留随 mount namespace 修复一并解决，或为技能提供私有 `/tmp` 绑定。

## 上一份审查条目复验（Q2-01…Q2-10）

- **Q2-01 · P1 · 已修复（实证）**：base 改为运行时自有 0711 目录，技能无法在其中创建条目（实测 base/root/rt/run 植入符号链接均 PermissionError）；`_remove_tree`/`_sweep_dead_workspaces` 用 `lstat`+`fwalk(follow_symlinks=False)`，不跟随植入的 `rt-<死PID>` 符号链接；受害目录权限与内容不变。
- **Q2-02 · P2 · cloud · 代码已加绝对 deadline**：`streaming.py` 引入 `_guarded(chunks, deadline)` 与 `_Channel` 空闲上限、`deadline` 终止类型；cloud 测试全过。本轮为运行时沙箱实证，未对云流式做实证攻击，建议云侧单独复验行级/空闲截止。
- **Q2-03 · P2 · cloud↔SDK · 代码有改动**：终止事件处理已重构；cloud/sdk 测试全过；未实证复验（超出本轮沙箱范围）。
- **Q2-04 · P2 · 已按设计修复（实证）**：无 cgroup 时 `child_subreaper=true`、状态 `process_containment=best-effort`、`/v1/status` 暴露 `pids_cgroup:false`；`setsid` 逃逸进程在其父退出后由孤儿收割者收割，无僵尸。真机部署仍建议强制 cgroup（systemd `Delegate=yes`）。
- **Q2-05 · P3 · 已修复**：`evaluations.get()` 对非三态（如 `rejected` 孤儿）抛 `evaluation_not_found`，`public()` 只返回 `running|completed|unknown`（代码显式标注 Q2-05）。
- **Q2-06 · P3 · cloud · 未单独复验**：下界统计仅云侧计量时机，低危，超出本轮范围。
- **Q2-07 · P3 · 桌面端 · 未复验**：技能文件夹中间符号链接属 `brainos/`，非运行时沙箱，超出本轮范围。
- **Q2-08 · P3 · 已修复（实证）**：日志预算按 `RunState` 累计；刷屏技能与三步应用实测日志封顶在 `max_log_events`（100），安全事件不被挤出遥测。
- **Q2-09 · P3 · 已修复**：`git ls-files` 无 `egg-info`；根 `.gitignore` 含 `*.egg-info/`、`build/`。
- **Q2-10 · P3 · 桌面端 · 未复验**：回环 http 例外属 `brainos/cloud_account.py`，超出本轮范围。

## 建议

1. **把 Q2R-01 并入第 05 轮多租户隔离闸门**，并将该闸门从"网络隔离"扩为"网络 + 文件系统/uid 隔离"；第三方技能市场上线前必须完成。
2. **立即在 `runtime/README.md` §2.2 补记剩余信任边界**：并发技能共用降权账户、彼此工作区（含私有 `tmp` 机密）不隔离；子进程当前对同 uid 对端可 dump。
3. 为并发技能分配不同 uid（账户池/动态 uid）或 per-run mount namespace + 独立 tmpfs；过渡期至少对子进程 `PR_SET_DUMPABLE(0)`（仅消除 `/proc` 路径泄露，不替代独立 uid 修复）。
4. 云侧 Q2-02/Q2-03 在接真实模型供应商流式前单独实证复验（本轮未覆盖云流式）。
5. 基线 `runtime/tests` 有一次负载下偶发失败、重跑 3 次均过；建议排查易抖动的沙箱计时断言（如停止/收割时限），把裕量从"刚好"调为更稳。

---

附：攻击技能与脚本位于 `scratchpad/rereview02/`（仓库外，不随发布）：`attacks.py`（类别 1–3）、`attacks2.py`（类别 4–6）、`enum_probe.py`/`xskill_exploit.py`（Q2R-01 端到端）、`peer_probe.py`，及其输出 `*.out`、`baseline.txt`。
