# NeuxMind SDK 与 `nx` 命令行（`sdk/`）

开发者用来"几分钟让任意机器人开始工作"的工具：Python SDK（端侧运行时客户端、云平台客户端、技能开发与本地测试）和 `nx` 命令行。

> 状态：第 04 轮（本体适配：`nx adapter new|test`、`nx robot conformance [--upload]`、`nx models list --require-level dry`，见 §5.7；安装进度改读运行记录）。第 03 轮（Python 3.10 支持；端侧模型安装 `nx models install/installed/remove`，见 §5.5；遥测 `nx telemetry tail`，见 §5.6；`nx robot status` 显示沙箱等级）。第 02 轮：网关计量会话与流式、`nx chat`（§5.4）。**所有机器人验证都在运行时的仿真本体（SimQuadruped / SimArm，`simulated=true`）上完成，没有在真机上验证。** 云端测试用的是云平台真实实现，唯一的替身是云平台自带的 EchoProvider（不是模型，结果带 `simulated: true`）。

- 核心只依赖 **Python 3.10+** 标准库（HTTP 用 `urllib`，SSE 自己解析）和共享规范包 `neuxmind-spec`、端侧运行时包 `neuxmind-runtime`；**不依赖 `openai`**，但会话可以直接给 openai 库用（§5.4）。
- 共享规则（能力标准、manifest v2、技能包 v1、`check_compatibility`）直接调用 `spec/neuxmind_spec/`，不复制逻辑。
- 本地技能测试与 `nx sim` 直接使用 `runtime/neuxmind_runtime/`（进程内或子进程），与机器人上跑的是同一份代码。

## 1. 安装

```bash
pip install -e spec -e runtime -e sdk      # 在仓库根目录；提供 nx 命令与 neuxmind 包（系统 Python 加 --break-system-packages）
nx --version
```

`sdk/pyproject.toml`：`requires-python = ">=3.10"`，依赖 `neuxmind-spec`、`neuxmind-runtime`（契约 §4.4）。两者还没有发布到 PyPI，所以要在同一条 `pip install` 里给出三个本地目录（pip 优先用本地的）；单独 `pip install -e sdk` 会因为找不到依赖而失败。

### 1.1 Python 3.10（Jetson JetPack 6 / Ubuntu 22.04）

SDK 不使用 3.11+ 才有的标准库或语法（没有 `tomllib`、`ExceptionGroup`/`except*`、`typing.Self`、`datetime.UTC`、`TaskGroup` 等；所有模块用 `from __future__ import annotations`，`X | None` 只出现在注解里）。第 03 轮在 Python 3.10.20 venv 与 3.13 上各跑全部 `sdk/tests`：

```bash
python3.10 -m venv ~/nx310 && ~/nx310/bin/pip install -e spec -e runtime -e sdk
~/nx310/bin/nx --version
# 只在跑 SDK 自身测试时需要云平台依赖；cloud/requirements.txt 固定的 SQLAlchemy 2.1.8 需要 3.11+，
# 3.10 上请改用 2.0.x（测试实测 SQLAlchemy 2.0.57 + 其余版本与 requirements.txt 相同）：
~/nx310/bin/pip install fastapi==0.162.0 starlette==1.7.0 pydantic==2.13.5 "SQLAlchemy<2.1" uvicorn==0.54.0 httpx==0.28.1
~/nx310/bin/python -m unittest discover -s sdk/tests
```

SDK 通过以下顺序找到 `neuxmind_spec` 与 `neuxmind_runtime`：已可导入（已安装）→ 环境变量 `NEUXMIND_SPEC_PATH` / `NEUXMIND_RUNTIME_PATH`（指向**包含**该包的目录）→ SDK 所在仓库的 `spec/`、`runtime/`。仓库内的相对路径引导保留，现有测试与 `PYTHONPATH=sdk` 用法不受影响。

不安装也能用：`PYTHONPATH=sdk python3 -m neuxmind ...` 等同于 `nx ...`。

## 2. 5 分钟上手

以下命令在本仓库实际跑通过（第 01 轮，Linux，Python 3.13；输出为实际输出节选）。

```bash
# ① 启动仿真四足：生成随机令牌、自动添加为机器人 sim-quadruped（并设为默认机器人）
nx sim up --body quadruped
#   仿真本体已启动：SimQuadruped（仿真四足）（simulated=true）
#     地址：http://127.0.0.1:8790    令牌：…    已添加为机器人 sim-quadruped（默认机器人）

nx robot caps sim-quadruped          # 能力与参数表，标出 simulated=true

# ② 从模板创建技能，并先在进程内的仿真本体上跑它自带的测试
nx skill new my-patrol --template patrol
python3 -m unittest discover -s my-patrol/tests          # OK

# ③ 兼容性检查（spec.check_compatibility）→ 安装 → 授权运行并等待结束
nx skill check my-patrol
#   技能 my-patrol@0.1.0 → sim-quadruped（…，quadruped，仿真本体）
#     结果：兼容
nx skill install my-patrol
nx skill run my-patrol --param 'waypoints=[[1,0],[1,1]]' --authorize --wait
#     [progress]  50% 到达航点 0
#     [progress]  100% 到达航点 1
#   运行 run-…：成功（succeeded）  进度 100%

# ④ 换一个本体：启动仿真机械臂，看跨本体检查结果
nx sim up --body arm --port 8791
nx skill check my-patrol --all
#   技能 my-patrol@0.1.0 → sim-arm（SimArm（仿真六轴机械臂），manipulator，仿真本体）
#     结果：不兼容
#       - 缺少必需能力 base.navigate_to（导航到目标）

# ⑤ 停止（永远可用，无需授权；之后急停锁存，需 nx reset <robot> --authorize 才能再动）
nx stop sim-quadruped
nx sim down                          # 结束全部仿真本体
```

没加 `--authorize` 时 `nx skill run` 在本地直接拒绝，**不会向机器人发送任何请求**。

### 云端部分（模型市场与网关）

```bash
# 本地开发用云平台（dev 环境：邮件只记录不发送；EchoProvider 是测试替身）
NEUXMIND_ENV=dev NEUXMIND_OPERATOR_EMAILS=ops@example.com python3 -m neuxmind_cloud --port 8800 &   # 在 cloud/ 目录

nx config set cloud_url http://127.0.0.1:8800
nx register --email me@example.com
#   已注册并登录：me@example.com（组织 …）
#   邮箱尚未验证：验证前可以浏览目录、钱包和设备，但不能购买、支付或调用网关。
#   【仅开发环境】服务端没有真实发信（simulated），验证令牌：nxv_…
nx verify-email nxv_…                # 必须在本人登录状态下执行；生产环境令牌在验证邮件里，没收到用 --resend
nx whoami                            # 邮箱：已验证
nx wallet
nx models list --robot sim-quadruped # 只列出这台机器人能用的模型
nx models buy <模型 ID>
nx infer <模型 ID> "你好"
nx chat <模型 ID> "讲个故事" --stream    # 开计量会话 → 流式逐字输出 → 关闭 → 打印计量
#   echo: 讲个故事
#   —— 计量：本次 4 token，会话已用 4/1024，已计量；测试替身（simulated=true）
#   —— 会话 gws_… 已关闭：用量 4 token（1 千 token 单位），扣费 1 点
nx sessions show gws_…               # 会话状态与用量
```

验证必须在**收到邮件的那个账户自己的登录会话**里完成（云端复审修复 N-01）；验证成功后，该账户的其他登录会话和**验证前创建的 API Key 会被吊销**，需要重新创建。

如果 `nx register` 提示"该邮箱已被一个未验证的账户占用，认领邮件已发送"，说明有人（也可能是你自己早先）用这个邮箱注册但没验证：用邮件里的令牌 `nx claim <令牌>` 设置新密码，得到一个已验证的新账户，原占用账户被停用。忘记密码：`nx password-reset request --email <邮箱>` → `nx password-reset confirm <令牌>`。

邮箱验证是云端第 01 轮修复引入的：**未验证邮箱可以注册、登录、浏览，但购买、支付、调用网关会得到 `email_unverified`**，`nx` 会提示如何验证。`dev_verification_token` 只有 dev/test 服务端才会返回（`verification.simulated=true`），生产环境不会出现。

## 3. CLI 参考

所有命令都支持 `--json`（输出一个 JSON 文档；错误时为 `{"ok": false, "error", "code"}`）。退出码：0 成功；1 失败（错误、不兼容、运行未成功）；2 用法错误。`--robot` 省略时用默认机器人（`nx robot list` 里带 `*`）。

| 命令 | 说明 |
|---|---|
| `nx config set cloud_url <url>` | 设置云平台地址（可设键：`cloud_url`、`org_id`、`default_robot`） |
| `nx config show [key]` / `nx config get <key>` | 显示配置（令牌已遮盖） |
| `nx register --email E` | 注册并登录 |
| `nx login [--email E]` | 登录，会话令牌存入配置文件；密码来源：交互输入 / `NEUXMIND_PASSWORD` / `--password-stdin` / `--password`（不推荐） |
| `nx logout` / `nx whoami` | 退出 / 当前用户、组织、角色、邮箱是否已验证 |
| `nx verify-email <令牌>` / `nx verify-email --resend` | 在本人登录会话里验证邮箱（令牌属于别的账户 → `verification_wrong_account`，令牌不消耗）；验证后其他会话与验证前的 API Key 被吊销 / 重发验证邮件（旧令牌作废；dev 环境输出 `dev_verification_token`）。购买、支付、调用网关前必须验证 |
| `nx register --email E`（邮箱被未验证账户占用时） | 服务端返回 202 `claim_sent`：不建账户、不登录，认领令牌发到该邮箱（dev 环境直接显示，注明仅开发环境） |
| `nx claim <令牌> [--name N]` | 用认领令牌新建已验证账户并登录；原占用账户停用 |
| `nx password-reset request --email E` / `nx password-reset confirm <令牌>` | 申请重设密码（永远显示"已发送"，不暴露邮箱是否注册；未验证账户收到认领令牌）/ 设新密码并登录（注销其他会话，API Key 保留） |
| `nx robot add <name> <url> --token T [--default] [--no-verify]` | 添加机器人（默认会连一次读取 manifest 验证令牌）；令牌也可用 `NEUXMIND_ROBOT_TOKEN` |
| `nx robot list [--check]` | 列出机器人；`--check` 检查在线与急停锁存 |
| `nx robot caps [name]` | 能力表：ID、名称、类型、安全等级、manifest 参数；标出 simulated |
| `nx robot status [name]` | 健康、急停锁存、运行中任务；技能沙箱（`mode`、隔离等级 `level: full\|best-effort`、运行时/技能用户与是否降权、技能解释器及 `python_usable`——不可用时给出 `--sandbox-python` 修复建议、pids cgroup、网络未隔离、逐条 `best_effort_reasons`）；端侧模型数量；遥测最新 seq 与是否持久化 |
| `nx robot register [name]` | 把 manifest 登记到云端设备群 |
| `nx robot conformance [name] [--level static\|dry\|motion] [--authorize] [--wait] [--id CF] [--upload]` | 能力一致性测试（§5.7）：`POST /v2/conformance`（运行时在自身进程内测试），轮询并显示每级结论与每项能力 static/dry/motion 结果（通过 / 失败 / 跳过（不适用 / 受阻））、未声明能力、"不等于现场验收证据"。默认 `dry`；`motion` 本地强制 `--authorize`（未加时零请求）。不加 `--wait` 只启动并给出 `--id`；`--upload` 隐含 `--wait`，先按当前 manifest 登记/刷新云端设备群记录再上传摘要；报告带的 `manifest_digest` 与机器人当前 manifest 不同（如 `--id <旧测试> --upload`）时本地拒绝 `conformance_manifest_mismatch`、不发云端请求。退出码：所请求级别通过 0，否则 1。`--json`：原始 `conformance.report.v1`（`--upload` 时为 `{report, upload}`） |
| `nx adapter new <name> --from-manifest <file\|robot> \| --caps a,b,... [--family F] [--vendor V] [--model M] [--impl cap=hint]... [--param cap.p=JSON]... [--out D] [--force]` | 调用运行时脚手架（`neuxmind_runtime.scaffold`，进程内）生成适配器包；`--from-manifest` 复制 manifest JSON 文件（或 `{"manifest": …}`）或已添加机器人 `/v2/manifest` 的能力与限值（实现方式默认 `todo`，新本体不继承 `instance_id`、`simulated=false`）；`--caps` 时必填限值必须用 `--param` 给出（脚手架不猜）。实现提示 `ros2[:话题/动作]`、`sdk[:名]`、`serial[:设备]`、`http[:地址]`、`todo`。厂商、型号、`instance_id` 与参数中的文本先按白名单校验（单行，厂商/型号 ≤100 字、只含字母数字汉字空格与 `_ . + , ( ) / : -`；参数文本只含字母数字与 `_ . / : -`），不符 → `unsafe_scaffold_input`、不写任何文件（机器人提供的 manifest 不可信，审查 Q4-09）；脚手架中途失败 → `scaffold_failed` 并删除本次新建的输出目录 |
| `nx adapter test <path> [--level static\|dry\|motion] [--authorize] [--time-scale K] [--out report.json]` | 本机进程内一致性测试（`neuxmind_runtime.conformance.run_adapter`）。`path` = 生成的包目录（或其模块目录）、`sim_quadruped`/`sim_arm`/`ros2` 或 `module:Class`。`static` 对生成包用 `offline_manifest()`（不碰硬件、不需要 ROS 2）；`motion` 本地强制 `--authorize`，未加时不加载适配器。ROS 2 适配器缺 rclpy → `ros2_unavailable`；必备能力未实现 → `adapter_incomplete`。**信任边界**：导入适配器包即以你的权限执行它的全部代码（static 也一样，网络与文件不隔离）；报告由同一进程产生，适配器代码可以篡改判定——只测试你信任的适配器 |
| `nx robot remove <name>` | 从本地配置移除 |
| `nx skill new <id> --template blank\|patrol\|pick_place\|status [--dir D]` | 生成 `skill.json`、`skill.py`、`tests/test_skill.py`、`README.md` |
| `nx skill check <dir> [--robot R]... [--body quadruped\|arm]... [--all]` | 兼容性检查，逐项说明缺少的能力与不满足的参数；`--body` 离线对照仿真本体清单 |
| `nx skill install <dir> [--robot R]` | 本地校验 + 兼容性预检 → `POST /v2/skills/install` |
| `nx skill list [--robot R]` | 已安装技能 |
| `nx skill run <id> [--robot R] [--param k=v]... [--params-json J] --authorize [--wait] [--timeout-s S]` | 运行；`--param` 值先按 JSON 解析，失败按字符串（技能声明为 str 的参数始终按字符串）；`--wait` 轮询并打印日志/进度直到结束 |
| `nx skill status [run_id] [--robot R]` | 某次运行详情；不带 run_id 列出最近运行 |
| `nx skill cancel <run_id> [--robot R]` | 取消（不锁存急停） |
| `nx stop [robot] [--reason]` | 全局停止，永远可用、无需授权，之后急停锁存 |
| `nx reset [robot] --authorize` | 解除急停锁存 |
| `nx wallet [--ledger]` | 余额 / 预留 / 可用（token 点），可附账本 |
| `nx models list [--robot R] [--kind K] [--require-level static\|dry]` | 模型市场；`--robot` 取该机器人 manifest，经云端 `/catalog/models/compatible` 过滤；`--require-level dry` 只把云端设备群里该机器人最新一致性报告中 dry 已通过的能力视为可用（先按当前 manifest 登记/刷新；没有对应报告 → `conformance_required` 并提示 `nx robot conformance … --upload`），同时列出已验证可用与未验证的能力 |
| `nx models buy <id>` | 用钱包点数购买模型授权 |
| `nx models install <id> [--robot R] --authorize [--wait-timeout S]` | 端侧模型安装（§5.5）：云端取授权包（未授权 → 提示 `nx models buy <id>`、价格与钱包可用点数，不向机器人发任何请求）→ 机器人 `model.install`（v2 invoke，带 `model_id`/`name`=目录 ID）→ 轮询运行并显示进度。同一 SHA-256 已安装时直接成功并注明"未重复下载"；SHA-256 不符 → `install_failed`（附运行时原因），退出码 1 |
| `nx models installed [--robot R]` | 机器人上已安装的端侧模型（`GET /v2/models`）：SHA-256、目录 ID（`model_id`）、名称、运行时、大小、安装时间、文件 |
| `nx models remove <id\|sha256> [--robot R] --authorize` | 删除（`DELETE /v2/models/{sha256 或目录 ID}`）。目录 ID 直接交给运行时解析（不需要登录云平台）；同一目录 ID 装了多个版本 → `model_ambiguous`，列出各版本的 `nx models remove <sha256>` 命令；只有旧运行时记录里没有目录 ID 时才回退到云端模型卡的包 SHA-256。正被运行中技能使用 → `model_in_use`，列出占用的运行并提示先 `nx skill cancel`/`nx stop` |
| `nx chat <model> "<prompt>" [--stream] [--max-tokens N] [--api chat\|responses] [--max-minutes M]` | 开网关计量会话（`--max-tokens` = 会话预算，输入 + 输出，默认 1024，开会话时按它预留）→ 调用一次（`--stream` 逐字打印）→ 关闭会话 → 打印本次计量与关闭结果。流内错误（如 `output_limit_reached`）或连接中断时仍会关闭会话并打印计量，退出码 1。`--json` 输出 `{ok, session_id, text, metering, close, error?}` |
| `nx sessions show <id>` | `GET /gateway/sessions/{id}`：状态、已用/上限、在途、调用数、结果未知数、超额、关闭结果 |
| `nx infer <model> "<prompt>" [--max-tokens N] [--option k=v]...` | 通过云端网关推理（预留 → 调用 → 按实际用量结算）。只支持 `max_tokens`（服务端按预留再限制，只能调小）；`--option` 里的其他参数在本地明确拒绝：`n≠1` → `n_unsupported`，其他 → `parameter_not_allowed` |
| `nx telemetry tail [--robot R] [--run ID] [--since N] [-n N] [-f\|--follow] [--interval S] [--duration S]` | 遥测（§5.6）：默认最近 20 条（`-n`）；`--since` 从该 seq 之后增量拉取 `/v2/telemetry?since=`（自动翻页）；`--run` 回放该运行的全部事件（`/v2/runs/{id}/events`，跨重启）；`--follow` 持续轮询，带 `--run` 时运行结束、最后一条事件输出后自动退出，否则到 `--duration` 或 Ctrl-C。`--follow --json` 每行输出一个 JSON 事件（NDJSON），其他情况 `--json` 输出 `{events, last_seq}` |
| `nx sim up --body quadruped\|arm [--port 8790] [--name N] [--time-scale K] [--foreground] [--default]` | 调用 runtime 自己的 CLI 启动仿真本体（后台进程，日志在 `$NEUXMIND_HOME/sim/<name>.log`），生成随机令牌并自动 `nx robot add`；`--port 0` 自动选端口；每次 up 都是全新状态 |
| `nx sim down [name]` / `nx sim list` | 结束（先 SIGINT 让运行时停车）/ 列出 |

环境变量：`NEUXMIND_HOME`（配置目录）、`NEUXMIND_CLOUD_URL`、`NEUXMIND_API_KEY`（用 API Key 代替登录会话）、`NEUXMIND_PASSWORD`、`NEUXMIND_ROBOT_TOKEN`、`NEUXMIND_SPEC_PATH`、`NEUXMIND_RUNTIME_PATH`。

## 4. 配置文件

`~/.neuxmind/config.json`（`NEUXMIND_HOME` 可改目录，测试与 CI 用）。文件含云会话令牌与机器人令牌，以原子方式写入，权限 `600`，目录 `700`。

```json
{"cloud_url": "http://127.0.0.1:8800", "cloud_token": "nxs_…", "cloud_email": "me@example.com",
 "robots": {"sim-quadruped": {"url": "http://127.0.0.1:8790", "token": "…", "instance_id": "sim-quadruped-01",
                              "manifest_name": "SimQuadruped（仿真四足）", "family": "quadruped", "simulated": true}},
 "default_robot": "sim-quadruped"}
```

## 5. Python API

### 5.1 `neuxmind.robot.RobotClient(url, token, timeout=15)`

| 方法 | 对应接口 |
|---|---|
| `manifest(refresh=False)` / `manifest_v1()` / `simulated` | `GET /v2/manifest`（缓存）/ `GET /v1/manifest` |
| `status()` / `registry()` / `state(*ids)` | `/v1/status` / `/v2/capabilities/registry` / `/v2/state?ids=` |
| `invoke(cap, args, authorize=False, request_id=None)` | `POST /v2/capabilities/{id}/invoke`；command 返回 outputs，action 返回 `{run_id}` |
| `skills()` / `install_skill(dir_or_package)` | `GET /v2/skills` / `POST /v2/skills/install`（先本地校验） |
| `run_skill(id, params, authorize=False, version=None, timeout_s=None)` | `POST /v2/skills/{id}/run` → `{run_id, status}` |
| `runs()` / `run(run_id)` / `wait(run_id, timeout=300, interval=0.2, on_update=None)` / `cancel(run_id)` | `/v2/runs…` |
| `stop(reason=None)` | `POST /v2/stop`（永远可用） |
| `reset(authorize=False)` | `POST /v2/reset` |
| `telemetry(since=0)` / `run_events(run_id, since=0, limit=1000)` | `GET /v2/telemetry`（每次至多 500 条）/ `GET /v2/runs/{id}/events` |
| `events_since(since, run_id=None)` → `(events, last_seq)` / `latest_seq()` | 自动翻页直到追上；`last_seq` 作为下次的游标 / `/v1/status.telemetry.last_seq` |
| `tail(since=None, lines=None, run_id=None, follow=False, interval=0.5, duration=None, should_stop=None)` | 生成器，语义同 `nx telemetry tail`（§5.6） |
| `start_conformance(level, authorize=False)` / `conformance(id)` / `conformance_runs()` / `wait_conformance(id, timeout=600, on_update=None)` / `run_conformance(level, authorize=False, timeout=600)` | `POST /v2/conformance` → `{conformance_id, status:"running"}` / `GET /v2/conformance/{id}` / `GET /v2/conformance` / 轮询到非 running / 启动并等待 → `conformance.report.v1`（运行 `failed` → `conformance_failed`，重启后 `unknown` → `conformance_unknown`；已完成但未通过的报告照常返回，看 `report[f"{level}_ok"]`）。`motion` 未 `authorize=True` → 本地 `authorization_required`、零请求；旧运行时（无该端点，404）→ `conformance_unsupported` |
| `models()` / `find_models(ref)` / `find_model(ref)` / `supports_model_install()` | `GET /v2/models`（`sha256` 是安装标识，`model_id` 是目录 ID，可空）/ 按 SHA-256（不分大小写）、目录 ID（可能多个版本）或 `name` 查找；`find_model` 只在唯一时返回 / manifest 是否有 `model.install` |
| `install_model(package_ref, sha256, runtime, model_id=None, name=None, authorize=False)` | `POST /v2/capabilities/model.install/invoke` → `{run_id}`，再 `wait(run_id)` |
| `remove_model(sha256_or_catalog_id, authorize=False)` | `DELETE /v2/models/{…}` → `{deleted, model_id: <sha256>}`；目录 ID 不唯一时 409 `model_ambiguous`（`details.sha256`） |

规则：
- **运动在本地把关**：spec 中 `safety=motion` 的能力、所有 `x.*` 扩展能力、所有技能运行（运行时对每个技能运行都要求授权）以及 `reset`，必须显式传 `authorize=True`，否则抛 `NeuxMindError(code="authorization_required")`，且不发出任何请求。`stop()` 永远不需要授权。
- `request_id` 自动生成（`uuid4`），也可自己传。SDK **从不自动重试**会改变状态的请求：发送后没收到响应时抛 `outcome_unknown`，`details["request_id"]` 带上原 id——用同一个 id 重发（运行时幂等）或先查状态，不要用新 id 重放运动指令。
- 所有错误统一为 `neuxmind.NeuxMindError`：`code`（服务端错误码原样保留，如 `estop_latched`、`incompatible`、`hardware_not_authorized`、`request_id_reused`；本地错误码见 `neuxmind/errors.py`）、`status`、`details`。
- 回环地址（127.0.0.1/localhost）不走环境里的 HTTP(S) 代理，其他地址照常走代理。
- `install_model` / `remove_model` 不会使机器人运动，但会变更端侧软件（安全级 `system`），同样必须 `authorize=True`，否则本地拒绝、零请求。
- 运行时错误码 `model_in_use`、`model_ambiguous`、`model_not_found`、`runtime_unsupported`、`package_ref_refused`、`capability_not_provided`、`models_by_adapter`、`conformance_busy`、`runs_active`、`conformance_not_found` 在 `details["hint"]`（并附在消息里）给出中文处理建议。

### 5.2 `neuxmind.cloud.CloudClient(base_url, token=None, org_id=None)`

`base_url` 可带或不带 `/api/v1`。以下服务端错误码会在 `NeuxMindError.details["hint"]`（同时附在消息里）给出中文处理建议：`email_unverified`、`parameter_not_allowed`、`n_unsupported`、`session_overage_locked`、`verification_token_invalid`、`verification_token_expired`、`verification_wrong_account`、`email_sender_not_configured`、`already_verified`、`email_taken`、`email_pending_verification`、`recovery_token_invalid`、`recovery_token_expired`、`session_required`。`token` 为登录会话或 `nx_live_` API Key；`org_id` 作为 `X-NeuxMind-Org`。

新增中文提示：`entitlement_required`（先 `nx models buy`；免费/按量模型也需要 0 点授权）、`no_package`（只能经云端网关调用）；第 04 轮：`conformance_required`、`conformance_manifest_mismatch`、`conformance_robot_mismatch`、`conformance_outdated`、`device_run_refused`、`field_acceptance_refused`。

第 04 轮（§6.4）：`compatible_models(manifest=None, *, robot_id=None, require_level=None)`（二选一；`require_level="dry"` 时响应多 `usable_capabilities`、`not_verified`、`conformance`）、`upload_conformance(robot_id, report)` → `{robot_id, conformance}`、`check_skill(robot_id, skill, require_level=None)`（云端 `spec.check_compatibility`，`files` 不上传）。

网关会话：`gateway.open_session(model, max_tokens, *, max_minutes, max_units, request_id, call_timeout=300, stream_timeout=120, recover_timeout=60)` → `GatewaySession`（§5.4）、`gateway.session(id)`（`GET /gateway/sessions/{id}`）、`gateway.close_session(id, request_id)`。新增中文提示的会话错误码：`session_busy`、`session_closed`、`session_budget_exhausted`、`session_expired`、`invalid_session_token`。

`register(email, password, name)`（返回 `verification` 与仅 dev/test 才有的 `dev_verification_token`）、`verify_email(token)`（需本人会话；返回 `sessions_revoked`、`api_keys_revoked`）、`resend_verification()`、`claim(token, password, name)`、`request_password_reset(email)`、`reset_password(token, password)`（后两者与 claim 返回新会话并保存在客户端上；邮箱被未验证账户占用时 `register` 返回 `{status:"claim_sent", dev_claim_token?}` 且不设置令牌）、`login(email, password)`（令牌保存在客户端上）、`logout()`、`me()`；`create_api_key(scopes, name)`（明文 key 只返回一次）、`api_keys()`、`revoke_api_key(id)`；`wallet()`、`ledger(limit)`、`reservations(status)`、`usage(model, limit)`；`capability_registry()`、`models(kind, source, capability, robot_id)`、`model(id)`、`compatible_models(manifest)`、`model_package(id)`、`model_install_args(id)`（→ `{package_ref, sha256, runtime}`：优先用包信息里的 `install.args`——云端配置对象存储时那里是短时签名下载地址——否则回退到 `ref`；缺字段本地报 `bad_response`）、`credit_packs()`、`entitlements()`；`purchase_model(id)`、`purchase_credit_pack(pack_id)`、`purchases()`、`purchase(id)`、`checkout(provider, order_id)`（未接入的支付渠道返回 `provider_not_configured`，不伪造成功）；`infer(model, input, max_tokens, request_id, **options)` 与等价的 `gateway.infer(...)`（`options` 只允许 `n=1`，其余本地拒绝为 `parameter_not_allowed`，不发请求）；`register_robot(manifest)`、`robots()`、`robot(id)`、`delete_robot(id)`。

### 5.3 `neuxmind.package`

`build_package(dir)`（`skill.json` + 文本 `files`，排除 `tests/`、隐藏文件、`__pycache__`、`.pyc`）、`validate_package(pkg)`（spec 校验、entry 格式与入口文件存在、路径安全、与运行时一致的大小上限、参数默认值类型用 `check_field` 校验）、`load_skill(dir) → (package, meta)`、`explain_compatibility(meta, manifest)`（`check_compatibility` 的结论 + 中文原因：缺哪项能力、哪个参数要求多少而本体声明多少）、`format_compatibility(report)`。

### 5.4 网关计量会话与流式（`neuxmind.gateway`）

会话 = 先按 `max_tokens`（这一会话里所有调用的输入 + 输出 token 上限）预留点数，之后每次调用都由网关计量并计入会话，关闭时按实际用量结算（契约 §2.1、§4.3，细节见 `cloud/README.md`）。`GatewaySession` 是上下文管理器：离开 `with` 时自动关闭，结果在 `session.close_result`。

```python
from neuxmind.cloud import CloudClient
from neuxmind.gateway import GatewayStreamError, StreamInterrupted

cloud = CloudClient("http://127.0.0.1:8800", token)          # 登录会话或带 gateway scope 的 API Key
with cloud.gateway.open_session("echo-llm", max_tokens=2000) as session:
    # 非流式：返回上游 JSON（OpenAI 形状）+ "neuxmind" 计量对象
    reply = session.chat([{"role": "user", "content": "你好"}], temperature=0.2)
    print(reply["choices"][0]["message"]["content"])
    print(session.last_metering.describe())                   # 计量：本次 N token，会话已用 …

    # 流式：逐个产出上游事件；读到连接关闭为止（chat 的 [DONE] 只是普通事件）
    stream = session.chat([{"role": "user", "content": "讲个故事"}], stream=True)
    try:
        for text in stream.text_deltas():                     # 或 for ev in stream: ev.event / ev.json / ev.text
            print(text, end="", flush=True)
        print("\n", stream.metering)                          # 最后的 neuxmind.metering 事件（Metering 对象）
    except GatewayStreamError as exc:                         # 流内 event: error
        print(exc.code, exc.metering)                         # 如 output_limit_reached；已收到的文本在 stream.text
    except StreamInterrupted as exc:                          # 连接在终止事件之前断了
        print(exc.code, exc.metering)                         # stream_interrupted + 查询会话得到的最终用量 / outcome_unknown

    session.responses("一句话介绍 NeuxMind", stream=True).collect()   # Responses API 同样支持
print(session.close_result)    # {status: closed, usage_tokens, credits_charged, …} 或 reconciliation_required
```

| 情况 | SDK 行为 |
|---|---|
| 流开始前的错误（参数、预算、`session_busy`、上游 4xx/5xx） | 服务端是 JSON + 状态码 → `NeuxMindError`（`code`/`status`/`details`，带中文 `hint`、`session_id`），与非流式一样 |
| 正常结束 | 依次产出上游事件（含 chat 的 `[DONE]`），最后产出 `neuxmind.metering` 事件，并设置 `stream.metering`、`session.last_metering`（`source="stream"`） |
| 流内 `event: error` | 读到连接关闭后抛 `GatewayStreamError`：`code`（`output_limit_reached`、`upstream_interrupted`、`upstream_stream_error`、`usage_missing`、`stream_duration_exceeded`、`usage_exceeds_reservation`、`call_marked_lost`、`internal_error`）、`metering`（网关计量对象）、`event`（整个错误事件）；`stream.text` 保留已收到的文本 |
| 连接意外中断（没收到终止事件：EOF、读超时、chunked 断流） | 自动 `GET /gateway/sessions/{id}`，等服务端读完上游并结算（`inflight_tokens` 归零，最多 `recover_timeout` 秒），然后抛 `StreamInterrupted(code="stream_interrupted")`，`metering.source="session_query"`，`call_tokens` = 会话 `used_tokens` 的增量、`status` 由 `calls`/`unknown_calls`/`overage_calls` 的变化判定。等不到结算或查询失败 → `code="outcome_unknown"`、`metering=None`，提示稍后 `nx sessions show <id>`。**不会自动重发调用** |
| 提前离开循环（`break` / `stream.close()`） | 关闭连接；网关继续读完上游并按实际用量结算（期间会话仍"在途"） |
| `close()` 遇到 409 `session_busy` | 关闭被拒绝（没有执行），等在途调用结算后用**同一个** `request_id` 再发（关闭是幂等的），最多等 `wait`（默认 30）秒 |

- **不要传 `stream_options`**：网关自己写入（计量需要上游报告 usage）；SDK 本地拒绝为 `parameter_not_allowed`，不发请求。其余参数按网关白名单由服务端校验（见 cloud/README），不在白名单的参数 400 `parameter_not_allowed`，不会被悄悄忽略。
- **同一会话不能并发**（ADR-016）：同一时刻只允许一个在途调用，第二个调用得到 409 `session_busy` 和中文提示；需要并行请开多个会话，每个会话各自预留。
- **SDK 不自动重试**改变状态的请求：非流式调用发出后没收到响应 → `outcome_unknown`（带 `session_id`，用 `session.status()` 查看）。
- `Metering` 字段：`session_id, status, call_tokens, used_tokens, max_tokens, remaining_tokens, output_limit, hold_tokens, usage, output_lower_bound, upstream_ref, client_disconnected, simulated, source`；`describe()` 给出中文一行。`stream.output_limit` / `stream.simulated` 来自响应头 `X-NeuxMind-Output-Limit` / `X-NeuxMind-Simulated`。
- 同一 `request_id` 重放 `open_session` 会得到同一会话，但 `access_token` 只返回一次（`session.replayed=True`，调用报 `access_token_unavailable`）。

**用 openai Python 库**（SDK 不依赖 openai；以下在 openai 3.46 上手工验证过 chat 非流式/流式与 responses 流式，对象是 Echo 替身）：

```python
import openai
with cloud.gateway.open_session("echo-llm", max_tokens=2000) as session:
    client = openai.OpenAI(base_url=session.openai_base_url, api_key=session.access_token, max_retries=0)
    reply = client.chat.completions.create(model="echo-llm", messages=[{"role": "user", "content": "你好"}])
    for chunk in client.chat.completions.create(model="echo-llm", messages=[...], stream=True):
        ...                                   # openai 读到 [DONE] 就停，看不到 neuxmind.metering
    print(session.status()["used_tokens"])    # 用量以网关为准：查询会话
```

- `openai_base_url` = `{云平台地址}/api/v1/gateway/sessions/{id}/v1`（用客户端实际连接的地址；服务端按自己配置的公网地址给出的是 `session.server_base_url`）。
- 建议 `max_retries=0`：openai 库默认会自动重试超时/5xx，可能在结果未知时重发一次调用。
- 流内错误事件带 `error.code`，openai 库会抛 `APIError`；参数被拒是 `BadRequestError`（400）。

### 5.5 端侧模型安装（`neuxmind.models`，契约 §5.1/§5.4）

```bash
nx models list --robot dog                  # 看模型与价格（纯端侧包要求机器人声明对应的推理运行时）
nx models install edge-det --robot dog --authorize
#   错误：尚未获得模型 edge-det 的授权，不能下载端侧部署包。请先购买授权：nx models buy edge-det（一次性 30 点，钱包可用 100 点），然后重新安装。本次没有向机器人发送任何请求（代码 entitlement_required）
nx models buy edge-det
nx models install edge-det --robot dog --authorize
#   正在把 edge-det 安装到 dog【仿真】（运行 run-…：下载 → 校验 SHA-256 → 原子安装）…
#     下载与校验中 0%
#     下载与校验中 37%（1.1 MiB / 2.9 MiB）
#   已安装 edge-det 到 dog
#     SHA-256：f325a9f1…    运行时：onnxruntime  大小：2.9 MiB
#     路径：/…/models/f325a9f1…/det.onnx（技能中通过 ctx.models 只读访问）
nx models install edge-det --robot dog --authorize    # 再装一次：已在 dog 上（同一 SHA-256 已安装，未重复下载）
nx models installed --robot dog
nx models remove edge-det --robot dog --authorize     # 按目录 ID；正被技能使用时：model_in_use，列出运行 ID
nx telemetry tail --robot dog --run run-…             # 回放该次安装：model.install_started/progress/installed（或 install_failed 及原因）
```

```python
from neuxmind.models import install_from_cloud, resolve_model
result = install_from_cloud(cloud, robot, "edge-det", authorize=True,
                            on_start=lambda info: print(info["run_id"]),
                            on_progress=lambda run: print(run["progress"]))
result["sha256"], result["model"]["path"], result["already_installed"]
robot.remove_model("edge-det", authorize=True)     # 目录 ID 由运行时解析；多个版本时 model_ambiguous
```

| 步骤 | 做法 |
|---|---|
| 授权确认 | `--authorize`/`authorize=True` 在本地检查，未加时不向云端或机器人发出任何请求 |
| 机器人能力 | manifest 里没有 `model.install` → `capability_not_provided`（运行时需 `--models-dir`）；不向云端取包 |
| 取包 | `GET /catalog/models/{id}/package`（需授权）。403 `entitlement_required` → 转成购买提示：`nx models buy <id>`、模型价格、钱包可用点数（`details.pricing`、`details.available_credits`）；404 `no_package` → 该模型只能经网关调用 |
| 安装 | `model.install` v2 invoke，`args = {package_ref, sha256, runtime, model_id, name}`（`model_id`=`name`=目录 ID，运行时记入 `/v2/models`），之后只轮询 `/v2/runs/{id}`：进度 = 运行记录的 `progress`（第 04 轮起随下载实时更新，安装完成前 ≤ 0.99），字节数 = `progress_detail.feedback.{bytes,total}`；旧运行时（运行记录没有 `progress_detail`）回退到该运行的 `model.install_progress` 事件（`neuxmind.models.install_progress`）；同步拒绝（`runtime_unsupported`、`package_ref_refused`、403）直接报错并附提示 |
| 结果 | 运行 `succeeded` → 读 `/v2/models` 返回安装记录（`model_id` 为目录 ID；`model.install` 输出与 DELETE 响应里的 `model_id` 仍是 SHA-256）；`failed`（如 SHA-256 不符，运行时已删除临时文件、不发布）→ `install_failed`，`details.run` 为运行记录；超时 → `wait_timeout`（安装在机器人上继续，不会被取消） |
| 幂等 | 已安装的模型 ID 就是包的 SHA-256；同一 sha 再装由运行时直接判为成功（`model.install_skipped`，不再下载），SDK 用安装前的 `/v2/models` 标出 `already_installed` |

`model.install` 的安全级是 `system`：不会运动，急停锁存期间也能安装；`nx stop` 会取消进行中的安装。

### 5.7 本体适配与能力一致性（`neuxmind.adapters`，契约 §6.3/§6.4）

```bash
# ① 生成适配器包（运行时脚手架，进程内）：能力映射 + 限值来自本体档案/规格书
nx adapter new acme_rover --family mobile --vendor Acme --model R1 \
    --caps safety.stop,system.health,base.velocity,base.odometry,sensor.battery,sensor.camera.rgb \
    --impl safety.stop=ros2 --impl system.health=ros2 --impl base.velocity=ros2:/cmd_vel \
    --impl base.odometry=ros2:/odom --impl sensor.battery=ros2 \
    --param base.velocity.max_vx=0.5 --param base.velocity.max_vy=0 --param base.velocity.max_wz=0.7 \
    --param base.velocity.holonomic=false --param safety.stop.hardware_estop=true --param base.odometry.frame=odom \
    --param sensor.camera.rgb.name=front --param sensor.camera.rgb.frame=cam --param sensor.camera.rgb.rate_hz=15
#   已生成适配器包 acme_rover：/…/acme_rover
#     加载方式：acme_rover.adapter:AcmeRoverAdapter
#     已实现：safety.stop, system.health, sensor.battery, base.odometry, base.velocity
#     待实现（抛 NotImplementedError，不出现在 manifest 中）：sensor.camera.rgb
nx adapter new dog_twin --from-manifest sim-quadruped        # 或 --from-manifest manifest.json：复制能力与限值，实现方式 todo

# ② 本机进程内测试（static 不碰硬件；dry 不运动；motion 需 --authorize）
nx adapter test ./acme_rover                                 # static（清单）：通过（通过 12，失败 0，跳过 16）
nx adapter test ./acme_rover --level dry                     # 无 ROS 2：错误 … rclpy …（代码 ros2_unavailable）
PYTHONPATH=runtime/tests/ros2_double nx adapter test ./acme_rover --level dry     # rclpy 测试替身（不是 ROS 2）
#   dry（不运动）：通过（通过 7，失败 0，跳过 0）   … 【仿真本体】（替身 use_sim_time → simulated）

# ③ 启动后在机器人上测试并上传摘要到云端设备群
BRAIN_EDGE_TOKEN=… PYTHONPATH=./acme_rover neuxmind-runtime serve --adapter acme_rover.adapter:AcmeRoverAdapter
nx robot add rover http://127.0.0.1:8790 --token …
nx robot conformance rover --level dry --upload
#   已在 rover【仿真】 上开始一致性测试 cf-2928b372f81b（级别 dry）
#   一致性测试 cf-2928b372f81b：rover（Acme R1）级别 dry 【仿真本体】
#     static（清单）：通过（通过 12，失败 0，跳过 16）
#     dry（不运动）：通过（通过 7，失败 0，跳过 0）
#     结论：dry 级别通过
#   能力            名称      static  dry   motion  说明
#   safety.stop     停止      通过    通过  -
#   …
#   未声明（不支持或尚未实现，16 项）：sensor.imu, sensor.camera.rgb, …
#   注意：一致性测试结果不等于现场验收证据（field_acceptance=false，evidence_kind=simulation）。…
#   已上传摘要到云端设备群（robot rbt_…）：static_ok=True dry_ok=True motion_ok=None；…（不是现场验收）
nx models list --robot rover --require-level dry
#   适用于 rover 的模型（只算一致性报告 cf-2928b372f81b 中 dry 级别已通过的能力，仿真本体）：
#     已验证可用：base.odometry, base.velocity, safety.stop, sensor.battery, system.health
nx robot conformance rover --level motion                    # 错误：… 加 --authorize。本次没有向机器人发送任何请求
```

```python
from neuxmind import adapters
from neuxmind.robot import RobotClient
summary = adapters.new_adapter("acme_rover", "./acme_rover", caps=[...], impls=[...], params=[...], family="mobile")
report = adapters.test_adapter("./acme_rover", "static")                     # conformance.report.v1（+ "adapter"）
report = RobotClient(url, token).run_conformance("dry")                      # POST /v2/conformance + 轮询
cloud.upload_conformance(cloud.register_robot(robot.manifest())["id"], report)
cloud.compatible_models(robot_id=..., require_level="dry")
```

规则与边界：
- **都调用运行时自己的代码**：`nx adapter new` = `neuxmind_runtime.scaffold.generate`，`nx adapter test` = `neuxmind_runtime.conformance.run_adapter`（适配器被包进临时 `EdgeRuntime`，参数经 spec 校验、运动经安全监督器），`nx robot conformance` = 运行时 `/v2/conformance`。SDK 只做输入整理、本地授权把关与展示，不复制判定。
- **信任边界**（审查 Q4-13/Q4-18）：`nx adapter test` 在**本进程**里导入并实例化适配器包——导入即以当前用户权限执行包里的全部模块级代码（static 级也一样），网络与文件系统都不隔离；一致性套件与适配器在同一进程，**适配器代码可以篡改报告**（例如替换套件的判定函数）。只测试你信任的适配器代码。`nx robot conformance` 的报告同样由运行时进程（含适配器）自行产生。
- `--from-manifest <robot>` 时厂商、型号与参数文本来自机器人，SDK 在调用脚手架前按白名单校验，可疑值拒绝（`unsafe_scaffold_input`），合法值也会在输出中提示"来自机器人，请对照规格书核对"。
- motion 级别（本地与机器人上）会做真实动作：最小动作约 0.1 m / 0.05 rad / 10% 夹爪开度，但中断与停止用例的目标更大（导航约 0.5 m、关节约 0.4 rad、夹爪可能全程张开，以 runtime README §3.1 为准，中断失效时会走满；审查 Q4-19），CLI 的 motion 提示照此说明。**本地强制** `--authorize`/`authorize=True`，未加时不加载适配器、不发请求。dry 不运动（运行时对零值指令和测试自身锁存的解除带授权标志，契约 §6.1）。
- 报告**不等于现场验收证据**：`field_acceptance:false`，`evidence_kind` 为 `conformance`（真机）或 `simulation`（仿真/替身），从不是 `device_run`。CLI 每次都写明。
- `--upload`：云端只保存摘要（级别结论 + 每项能力各级状态），标注 `self_reported:true`，**只供参考**：报告是适配器同进程自报的、可被篡改，云端无法复核。报告必须对应设备群里**当前登记的 manifest**：报告的 `manifest_digest` 必须等于登记记录的摘要（运行时 b0e53c7 起报告都带；没有它的旧报告会保存但 `digest_verified:false`，不用于 `--require-level`），声明能力集合、`instance_id`、`simulated` 也须一致，所以 SDK 先用当前 manifest 登记/刷新。manifest 变了后旧摘要变为 `stale`，旧报告也不能再绑定到新 manifest。
- `--require-level dry`：只有 dry 级别 `passed` 的能力算可用（dry 跳过/不适用的能力不算通过）；运行时 b0e53c7 起 `legged.posture`（非标准值与锁存期间被拒）和 `model.install`（拒绝非法请求）都有不运动的 dry 用例，通过后算可用——注意 `model.install` 的 dry 通过只说明它会拒绝非法请求，不说明能成功安装；`safety.stop` 未通过时一律无可用能力。

### 5.6 遥测（契约 §5.2）

```bash
nx telemetry tail --robot dog -n 4
#   [5] 13:58:59 run.finished run_id=run-6977… status=succeeded error=null note=null
#   [6] 13:58:59 run.started run_id=run-dde8… kind=capability target=model.install
#   [7] 13:58:59 model.install_skipped model_id=f325a9f1… reason=already_installed
#   [8] 13:58:59 run.finished run_id=run-dde8… status=succeeded error=null note=null
nx telemetry tail --robot dog --since 1200          # 增量：seq > 1200 的全部事件（自动翻页）
nx telemetry tail --robot dog --run run-… --follow  # 回放并跟踪一次运行，运行结束后退出
nx telemetry tail --robot dog -f --json | jq .      # 每行一个 JSON 事件
```

- 游标是 `seq`：运行时的 seq 跨重启连续、可能有缺口（崩溃时丢失的未落盘事件），**从不重复**；SDK 只取 `seq > 游标` 的事件并推进游标，所以跟踪过程中不会重复输出。
- `/v2/telemetry` 每次至多返回 500 条，`events_since` 一直翻页直到追上；早于环形存储最旧一条（`/v1/status.telemetry.oldest_seq`）的事件已被淘汰。
- `--run` 用 `/v2/runs/{id}/events`（含重启前的事件，分页 `more`）；跟踪时先查运行状态再拉事件，运行进入终止状态后再拉最后一次再退出，最后一条 `run.finished` 不会漏掉。重启前的运行（`/v2/runs/{id}` 404）只回放。
- 跟踪是轮询（默认 0.5 s）；运行时还没有推送接口。

## 6. 写技能

技能包 = `skill.json`（`skill.package.v1`）+ 文本文件。`entry` 为 `"<文件>.py:<名字>"`，名字是函数 `run(ctx, **参数)`，或一个类——运行时用无参构造实例化后调用 `run(self, ctx, **参数)`（见 `runtime/neuxmind_runtime/skills.py`）。

```python
try:
    from neuxmind.skill import Skill, Context
except ImportError:                       # 机器人上没装 SDK 时，运行时自带的基类等价
    from neuxmind_runtime.skills import Skill
    Context = object

class Patrol(Skill):
    def run(self, ctx: Context, waypoints, loops=1):
        if ctx.has("legged.posture"):                      # 可选能力先判断
            ctx.call("legged.posture", posture="stand")
        for wp in waypoints:
            ctx.check_stop()                               # 停止/取消/超时时抛出
            ctx.call("base.navigate_to", goal=[wp[0], wp[1], 0, 0, 0, 0, 1])   # action：阻塞到完成
        return {"done": True}                              # 须可 JSON 序列化
```

- `neuxmind.skill.Skill` 继承运行时的 `neuxmind_runtime.skills.Skill`；`ActionFailed`、`SkillStopped`、`SkillCancelled`、`SkillTimeout`、`SkillInterrupted`、`SkillError` 直接就是运行时的类，所以 `except ActionFailed` 捕获的正是运行时抛出的异常。停止类异常继承 `BaseException`，`except Exception` 不会误吞，**不要捕获 `SkillInterrupted`**。
- `neuxmind.skill.Context` 是 `Protocol`，只用于类型提示与编辑器补全：`call / read / check_stop / sleep / log / progress / has / param / time_left / manifest / simulated / stopped / params / run_id`。运行时不做任何包装。
- 只能调用 `requires`/`optional` 声明过的能力（`safety.stop`、`system.health` 总可用）；参数按 `parameters` 校验，未知参数拒绝；同一 `id@version` 内容不可变，改代码要升版本号。
- 技能默认在运行时的独立子进程沙箱中执行（runtime/README §2.2；隔离等级见 `nx robot status`），但网络与本机设备**未隔离**，非 root 部署为"尽力而为"模式：只装你信任的技能。已安装的端侧模型在 `ctx.models` 中只读可见（`{sha256: {name, runtime, path, ...}}`）。

模板：

| 模板 | 必需能力 | 跨本体结果 |
|---|---|---|
| `blank` | `system.health` | 任何本体；不运动 |
| `status` | `system.health`（可选电池/里程计/关节/IMU） | 任何本体；不运动 |
| `patrol` | `base.navigate_to`, `safety.stop` | 四足兼容，机械臂缺 `base.navigate_to` |
| `pick_place` | `arm.joint_position`, `gripper.position`，约束 `dof ≥ 6` | 机械臂兼容，四足缺两项能力；仿真机械臂从不报告抓取，模板如实返回 `grasped: null` |

## 7. 测技能

`neuxmind.skill.testing` 在**当前进程**里用运行时的 `EdgeRuntime` + 仿真适配器（临时状态目录、不走 HTTP）安装并运行技能，经过与机器人上相同的运行管理、SkillContext 与安全监督器。

```python
from pathlib import Path
from neuxmind.skill.testing import SkillTestCase, SkillHarness

class PatrolTest(SkillTestCase):
    skill_dir = Path(__file__).resolve().parents[1]
    time_scale = 20.0                       # 仿真时间倍速（默认 20）

    def test_quadruped(self):
        result = self.run_skill("quadruped", waypoints=[[1, 0]])
        self.assertSucceeded(result)        # 失败时打印错误与技能异常堆栈
        self.assertTrue(result.simulated)

    def test_arm(self):
        self.assertIncompatible("arm", missing=["base.navigate_to"])

# 也可以手动控制：
with SkillHarness("my-patrol", "quadruped") as h:
    h.run(waypoints=[[1, 0]])
    print(h.read("base.odometry"))
    h.stop(); h.reset()
```

`RunResult`：`status / succeeded / result / error / progress / events / logs / simulated / describe()`。`SkillHarness.run(authorize=False)` 会得到运行时的 `hardware_not_authorized`，`stop()` 后运动技能得到 `estop_latched`。**仿真通过只证明逻辑与协议，不是现场证据。**

## 8. SDK 自身的测试

```bash
python3 -m unittest discover -s sdk/tests      # 仓库根目录；84 个测试，约 80 秒（3.10 与 3.13 相同）
```

全部是真集成：运行时网关（`GatewayServer` + 仿真四足/机械臂，随机端口）、云平台（`create_app` + uvicorn 线程，随机端口，SQLite 临时库，运营员上架 Echo 替身模型并给钱包调额）；CLI 用 `main(argv)` 与真实子进程（`python3 -m neuxmind`，包括 `nx sim up` 真正拉起运行时进程）；每个模板生成后在子进程里跑它自己的测试。

| 文件 | 覆盖 |
|---|---|
| `test_robot_client.py` | v2 全流程、v1 manifest、错误码映射、未授权运动本地拒绝（断言零请求）、限幅、幂等与 `request_id_reused`、取消、`wait_timeout`、stop → `estop_latched` → reset 恢复、锁存时非运动技能仍可运行、不兼容安装被运行时拒绝 |
| `test_package_and_templates.py` | 打包规则、本地校验错误、跨本体与参数约束的解释、Skill/异常与运行时同源、harness 的授权/停止/复位、4 个模板自测通过与跨本体预期 |
| `test_gateway_sessions.py` | 真实云平台 + Echo 流式替身：非流式 chat/responses 与上下文管理器关闭（含块内抛异常时也关闭）、流开始前错误为 JSON 错误（预算不足 402、白名单 400、本地拒绝 `stream_options` 且零请求）、chat 流读过 `[DONE]` 拿到计量事件且请求体不带 `stream_options`、responses 流、截断 `output_limit_reached`（按 hold 计、会话关闭待核对）、`upstream_interrupted`、`usage_exceeds_reservation`（会话锁定）、同一会话并发 409 `session_busy`（三种调用）而另开会话可并行、断流 TCP 代理中途掐断 → 查询会话得到精确用量、来不及结算 → `outcome_unknown` 且 `close()` 等待后结算、提前 break 后 `with` 退出仍正确关闭、重放开会话无令牌；CLI `nx chat --stream`（文本、计量、关闭结果、`nx sessions show`）、`--api responses`/`--json`、流内错误仍关闭并打印计量（退出码 1）、真实子进程里先读到首个字再等进程结束（证明逐字输出） |
| `test_models_telemetry.py` | （第 03 轮补充：目录 ID 由运行时记录与解析、无云端按目录 ID 删除、同一目录 ID 多版本 → `model_ambiguous`、`nx models list --robot` 看到 onnxruntime 端侧包而不含 tensorrt 包、慢速包服务下安装进度与 `--run` 回放 `model.install_progress`/`install_failed` 原因）真实运行时原生 `model.install`（`EdgeRuntime(models=ModelConfig(...))` = `--models-dir`，仅对 127.0.0.1 开放 http 的测试开关）+ 本地 HTTP 包服务（统计下载次数）+ 真实云平台（运营员上架带 `package` 的模型卡：license_once、免费、sha 不符、只支持 tensorrt、无包的网关模型）：未授权本地拒绝且零请求；安装（sha 大小写不敏感）、重复安装幂等且不重复下载、sha 不符失败且无残留、`runtime_unsupported`/`package_ref_refused` 带提示、删除与 `model_not_found`；`install_from_cloud`：未购买 → 价格与购买提示且机器人无变化、v2 invoke 请求体含 `model_id`/`name`、目录 ID → sha（云端回退）、`no_package`、无 `--models-dir` 的机器人只读 manifest 就拒绝、包信息缺字段 `bad_response`、签名地址优先；CLI `nx models install/installed/remove`（授权、购买提示、进度文字、已安装提示、按目录 ID 删除）；运行中技能在 `ctx.models` 里拿到模型时删除 → `model_in_use`（带运行 ID），取消后删除成功；`nx telemetry tail`：1200 条事件翻页无重复、`--since` 增量、默认/`-n` 最近 N 条、事件行格式、`--run` 回放与 `--since`、未知运行 404、`--run --follow` 跟踪到运行结束自动退出、全局 `--follow` 看到新事件且连续不重复、`--follow --json` NDJSON；`nx robot status` 的沙箱/模型/遥测显示，降级沙箱（best-effort、解释器不可用、无 cgroup）的文字 |
| `test_adapters_conformance.py` | （第 04 轮）`nx adapter new`：`--caps`（ROS 2 实现提示 → 已实现 / 相机 → 待实现、绝对路径、非空目录拒绝、缺必填限值 `scaffold_invalid` 且不生成目录、二者皆无 `bad_request`）、`--from-manifest` 文件（含 `{"manifest"}` 包装）与已添加机器人（能力集合相同、限值复制、`--impl`/`--param` 覆盖、不继承 `instance_id`、`simulated=false`、必备能力待实现）、无效 manifest、未知机器人；`nx adapter test`：生成包 static（offline、模块目录也可）、无 ROS 2 → `ros2_unavailable`、rclpy 替身上 dry 通过且最后指令为零速度、`--out`、必备能力未实现 static 失败与 dry `adapter_incomplete`、motion 未授权不加载适配器、非法级别、内置仿真本体三级全过；`nx robot conformance`：dry `--wait` 文字（每项能力各级、不适用、未声明、免责声明）与 `--json` 原报告、不等待 + `--id`、motion 本地拒绝（spy 断言零请求）、motion 授权后通过且锁存已解除、测试前已锁存 → 退出码 1 且保持锁存、`conformance_busy` 提示、旧运行时 404 → `conformance_unsupported`、`wait_timeout`、`unknown`；`--upload`：上传前 `--require-level` → `conformance_required`、上传后云端摘要、`--require-level dry` 只剩 dry 已验证的模型、`{report, upload}` JSON、较新的 static 报告使 dry 要求失效；`device_run` 伪造被拒、他机报告 `conformance_robot_mismatch`、`check_skill`；安装进度：只读运行记录（spy 断言不请求 `/events`）、单调且 ≤ 0.99、旧运行时回退到事件、`install_progress` 规则 |
| `test_e2e_adapter.py` | （第 04 轮）端到端：`nx adapter new` → `nx adapter test`（static；rclpy 替身上 dry）→ 独立进程 `neuxmind_runtime serve` 加载生成的适配器 → `nx robot conformance --level motion --authorize --upload` → 云端摘要 → `nx models list --require-level dry` |
| `test_cli.py` | 配置文件权限、robot add/list/caps、skill new/check（`--robot` 多个、`--all`、`--body`）/install/run（无授权拒绝、`--param`、`--wait`）/status/cancel、stop/reset、用法错误、`nx sim up/list/down` 子进程 |
| `test_cloud.py` | 运营员与开发者注册后在本人会话里用 `dev_verification_token` 验证邮箱；未登录验证被拒、他人会话 `verification_wrong_account`（令牌不消耗）、验证后旧会话与旧 API Key 失效；认领（客户端与 CLI：202 `claim_sent`、`nx claim`、占用账户会话失效、令牌不可重用）与找回密码（不暴露账户、重设后旧会话失效、旧密码失效、`nx password-reset request/confirm`）；未验证用户购买/推理得到带提示的 `email_unverified`；无效/被重发作废的令牌、重发、`already_verified`；infer 不允许参数本地拒绝；注册/登录/me、钱包与账本、按 manifest 过滤模型（四足 vs 机械臂）、授权门槛、购买、余额不足、网关推理与幂等重放、API Key scope 与吊销、端侧包、设备登记、支付渠道未配置；CLI 的 config/register（打印仅开发环境令牌）/login(stdin)/verify-email（令牌、--resend、错误令牌）/whoami/wallet/models list --robot/buy/infer（含 `--option` 拒绝）/robot register |

## 9. 已知限制

- 没有在真机上验证；`nx sim` 只启动仿真本体。
- `neuxmind-spec`、`neuxmind-runtime` 已声明为依赖，但都未发布到 PyPI，需与 `spec/`、`runtime/` 一起安装。
- 运行时只有轮询式遥测，`wait`/`--wait`/`telemetry tail --follow` 都是轮询。
- 端侧模型安装只在仿真本体 + 本机 http 包服务上验证（运行时的 http 只对配置的测试主机开放）；真实 https CDN / 对象存储签名地址、真实推理运行时加载模型都未验证。模型文件在测试里是随机字节。
- 运行时 0.3.0 首版（07e0d0c 之前）写的模型记录没有目录 ID：`nx models installed` 该列为 `-`，`nx models remove <目录 ID>` 对它们回退到云端模型卡的包 SHA-256（需已登录），否则请用 SHA-256。
- 安装进度读运行记录（第 04 轮运行时修复）；旧运行时回退到 `model.install_progress` 事件。
- 一致性测试只在仿真本体与 rclpy 测试替身上验证（报告都是 `evidence_kind: simulation`）；真机 `conformance` 级报告、真实 ROS 2 都没有验证过。云端摘要是**自报**的（没有运行时签名，适配器同进程可篡改），云端只能校验格式、一致性与 manifest 绑定。
- `nx adapter test` 在本进程导入适配器；同名模块在同一进程里第二次测试时会先从 `sys.modules` 移除（适配器若在别处缓存了自身模块，需另开进程）。
- 不封装支付 webhook；`checkout` 只是透传。
- 网关会话只在 Echo 替身上验证过（含 openai 库手工验证），没有接真实模型供应商；Echo 的非流式 responses 响应没有 `output` 字段，openai 库的 `response.output_text` 对它不可用（云端替身的限制）。
- 断流恢复按"本对象看到的上一次调用之后"的会话计数推算本次用量；如果同一会话同时被别的客户端（如 openai 库）使用，推算不出时给 `outcome_unknown`，不会猜。
- 技能包只支持文本文件（运行时契约如此）。
- 配置文件明文保存令牌（仅靠文件权限 600 保护），未接入系统钥匙串。
