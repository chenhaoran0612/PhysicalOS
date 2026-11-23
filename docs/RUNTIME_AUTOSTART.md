# 开机自启（issue #20，需求 R1-03、R1-08）

管理层装好以后（[RUNTIME_INSTALL.md](RUNTIME_INSTALL.md)，issue #19），机器人断电重启后要它自己起来，连接状态回到「在线」。安装不用管理员权限，开机自启要用，所以它是安装之外单独的一步：先列出要在设备上以管理员身份执行的每一件事，人确认后才做。

代码：`brainos/runtime_autostart.py`（这一步、两个单独的任务、等人确认、结果）、`brainos/runtime_autostart_device.py`（在设备上执行的四段固定脚本、服务文件、sudo 的用法）；`brainos/runtime_install.py`、`runtime_install_plan.py` 里插入了接入点；页面 `web/runtime_autostart.js`、`web/runtime_autostart.css`。

## 定下来的

- **管理层以 SSH 登录的这个账号运行，不是 root**（用户 2026-11-22 选定）。服务文件里是 `User=<登录账号>`；安装目录、令牌还在这个账号的 `~/neuxmind`，issue #17 照旧经 SSH 读令牌，安装照旧不用 sudo。以 root 运行要把安装目录和令牌移到 root 的位置、安装和读令牌也要用 sudo，没有做。
- **sudo 密码每次在控制台里输入**，只用这一次，不保存（用户选定）。
- **不是这个控制台装的管理层不给它设开机自启**（例如真机上手工装的那份；用户选定）：卡片写明原因，要开机自启先「安装管理层」选「删除后重新安装」。

## 怎么用

1. 「安装管理层」的表单里勾选「装好后设置开机自启」；或者装好以后，点卡片上「开机自启」一行的「设置」→「设置开机自启」。
2. 控制台先只读检查设备（`autostart_check`）。有挡着的就到此为止，什么都不以管理员身份执行，卡片写明原因（见「为什么没有开机自启」）。
3. 否则弹出「开机自启：确认要执行的内容」：在哪台机器上、以什么方式提权（root 登录 / 免密 sudo / 要 sudo 密码）；以登录账号身份做的一件事、以管理员身份执行的四条命令和每条做什么；要写的服务文件全文（和设备上将写入的一字不差）；会执行的完整脚本（可展开）；不做什么。需要密码时才有密码框。勾选「我看过上面的内容，确认以管理员身份执行」后点「确认执行」；或点「不设开机自启」。关掉窗口不算回答：卡片一直提示「等你确认」，15 分钟没有回答按「不执行」处理。
4. 确认后：写标记文件、执行管理员脚本、等 systemd 报告服务在运行、重新接入。成功后卡片上是「已开机自启」。
5. 「取消开机自启」（卡片「设置」里）同样先列出、再确认；之后管理层像安装后一样以脱离会话的进程运行，并重新接入。

## 在设备上做什么

以登录账号的身份（不用 sudo）：

- 在 `~/neuxmind` 里写 `.autostart`（内容：安装包编号、端口、本体）。只写进这个控制台用这个安装包装好的安装里；那里是符号链接就拒绝。取消时删掉它。

以管理员身份（`autostart_on`，一段固定脚本）：

1. 核对是 root、调用 sudo 的账号就是要写进服务的账号（`SUDO_USER`）、设备用 systemd；
2. 按账号的 passwd 记录（`getent`）取主目录，用同样的规则重新生成服务文件，算 SHA-256，和人确认的那份不一样就什么都不做（`changed`）；
3. 已有同名服务文件、但不是这个控制台为这个账号建的，就不动（`taken`）；
4. 写 `/etc/systemd/system/neuxmind-runtime.service`（先写临时文件再改名，root 所有，0644），`systemctl daemon-reload`；
5. `systemctl enable neuxmind-runtime.service`（在 `/etc/systemd/system/multi-user.target.wants/` 建链接）；
6. `systemctl --no-block restart neuxmind-runtime.service`：服务的 `ExecStartPre` 先用安装脚本 `stop` 停掉正在脱离会话运行的那个管理层，再由 systemd 用 `start` 启动；中间几秒钟连不上，控制台随后重新接入。

服务文件（`runtime_autostart_device.render_unit`，设备端 `render_unit` 生成同样的字节）：

```ini
# Made by the NeuxMind console (issue #20): starts the management layer of robot when the device boots.
# The console replaces or removes this file. nx-autostart user=robot package=runtime-<版本>-<校验值>
[Unit]
Description=NeuxMind management layer (robot)
Wants=network-online.target
After=network-online.target
ConditionPathExists=/home/robot/neuxmind/.autostart
StartLimitIntervalSec=0

[Service]
Type=forking
User=robot
Environment=NX_ROOT=/home/robot/neuxmind NX_PORT=8790 NX_ADAPTER=sim_quadruped
ExecStartPre=/bin/bash /home/robot/neuxmind-pkg/console/<安装包编号>/device_install.sh stop
ExecStart=/bin/bash /home/robot/neuxmind-pkg/console/<安装包编号>/device_install.sh start
PIDFile=/home/robot/neuxmind/runtime.pid
Restart=on-failure
RestartSec=15
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
```

- **经安装脚本启动**，所以和手工 `start` 完全一样：令牌只经环境变量给运行时、只监听 127.0.0.1、告警上报只认安装目录里的配置文件。安装脚本的锁也是同一把（同一份脚本文件）。
- **ROS 2 的环境**不写在服务里：issue #21 的安装脚本在 `install` 时把 ROS 2 Humble 的 setup 文件和 `ROS_DOMAIN_ID` 等写进 `~/neuxmind/ros2.env`，`start` 只按它进入 ROS 环境。systemd 给服务的环境是干净的，正好和 #21 的做法一致。服务传的 `NX_ADAPTER` 是这份安装记下的本体（#21 的安装脚本写在 `~/neuxmind/adapter`，检查时读它，只认 `sim_quadruped` / `sim_arm` / `ros2`；没有这个文件时用控制台安装时传的本体），所以服务不会把一份安装起成另一种本体；取消开机自启后重新启动它时也用这个本体。控制台现在只装仿真本体（`runtime_install_plan.ADAPTERS`），选 ROS 2 要等 #21 进了安装包。
- **网络和重试**：`After=network-online.target` 等网络就绪（设备有 `NetworkManager-wait-online` 或 `systemd-networkd-wait-online` 时才真的等）；管理层本身只监听回环，不需要网络。启动失败（端口被占、ROS 环境还没好等）时 `Restart=on-failure` 每 15 秒重试，`StartLimitIntervalSec=0` 不放弃。安装脚本 `stop` 发的 SIGTERM 让运行时正常退出（退出码 0），不会被当成失败重启。
- **`ConditionPathExists`**：服务只在 `~/neuxmind/.autostart` 存在时启动。这份安装被删掉（`remove`、「清理」）或换掉（「删除后重新安装」）时这个文件跟着没了，留下的服务文件不再启动任何东西。
- **没有 `Delegate=pids`**：2026-11-22 在容器里试过，systemd 把 pids cgroup 交给登录账号，但现在的运行时只认“子树已经开了 pids 的 cgroup”，`/v1/status` 仍是 `pids_cgroup: false`。所以技能沙箱和以前一样是尽力而为（README §0.2）；要用上它，运行时得先把自己移进一个子 cgroup，是另外的改动。

以管理员身份（`autostart_off`）：只对这个控制台为这个账号建的服务文件做 `systemctl disable --now`、删掉文件、`daemon-reload`、`reset-failed`。别的不动。

## sudo 和密码

- 检查时只跑 `sudo -n true`（不要密码就能用，就是“免密 sudo”）。登录的就是 root 时不经 sudo。
- 管理员脚本放在命令行上：`LC_ALL=C sudo -S -p '' sh -c 'exec </dev/null; <脚本>' nx-autostart <普通参数>`（免密时 `sudo -n`，root 时没有 sudo）。脚本是固定文本，参数只有账号名、安装包编号、端口、本体名、64 位十六进制的服务文件摘要。不用 `sh -s` 是因为标准输入要给 sudo 读密码；脚本开头 `exec </dev/null`，所以密码只会被 sudo 读到，不会被任何 shell 读到或执行（和修复命令的 `hardware_init._quote_root` 同一个做法）。
- 密码只在 `robots/runtime-install/answer` 这一次请求里从页面发到控制台，在任务里留到这条管理员命令跑完，随标准输入交给 sudo，然后丢掉。不保存、不进日志、不进审计、不进 `/api/state`、不出现在任何命令行里；设备的输出在显示之前会把它替换掉（`Job._hidden`）。页面不留它（不进浏览器存储）。测试逐一检查这些地方，浏览器验收也检查只发了一次、没有返回到页面。
- 密码不对：sudo 不执行任何东西，窗口重新列出并写明「sudo 密码不对」，还能再试（一共 3 次）。账号不在 sudoers 里、sudo 要求终端（`requiretty`）：不再问，记为“这个账号不能用 sudo”，原因里带 sudo 的原话。

## 为什么没有开机自启（卡片上的原因）

| 原因（`runtime_autostart.reason`） | 卡片 | 什么时候 |
| --- | --- | --- |
| （没有记录，有安装） | 安装时没有选择 | 装的时候没勾选 |
| （没有记录，没有安装） | 还没有安装管理层 | |
| `not_console` | 不是这个控制台装的管理层 | 手工装的，或设备上的不是控制台记录的那次安装 |
| `declined` / `not_confirmed` | 你没有确认 / 没有确认（等待超时） | |
| `no_sudo` | 这个账号不能用 sudo | 设备上没有 sudo、不在 sudoers、要求终端 |
| `no_systemd` | 设备没有 systemd | |
| `unit_taken` | 设备上已有别人建的同名服务 | 不是这个控制台为这个账号建的 `neuxmind-runtime.service` |
| `unsupported` | 设备缺少需要的东西 | 没有 `/bin/bash`、`getent`、`sha256sum`；账号名或主目录里有不写进服务文件的字符 |
| `wrong_password` | sudo 密码不对 | 连续 3 次 |
| `removed` | 已取消 | |
| 状态 `failed` | 开机自启没有成功 | 服务登记了，但 systemd 没能把管理层启动起来 |

这些都不是安装的失败：管理层照常运行、照常接入。

## 失败时

- 服务登记了但起不来（`autostart_service_failed`，或 300 秒内没运行起来 `autostart_timeout`）：这一步失败，窗口写明 systemd 的状态和管理层日志的最后 40 行；「设备上留下的」列出服务文件和标记文件，「取消开机自启」把它们拿走（之后管理层像安装后一样启动并接入）。
- 管理员脚本中途停下（写不进、`enable` 失败等，`autostart_failed`）：同上。
- 停止（「停止」按钮）发生在等人确认时：什么都没有以管理员身份执行，标记文件也还没写。

## 卸载和清理

- issue #19 的「清理」（会删掉这次装的 `~/neuxmind`）和「删除后重新安装」（会删掉原有的 `~/neuxmind`）在删之前，如果这台机器人有过这个控制台的开机自启（记录是“已开机自启”或“没有成功”，或者这次任务刚登记过），先只读看一眼设备：有这个控制台为这个账号建的服务文件，就先走「取消开机自启」（同样列出、确认）。不确认也照样删安装，结果里写明服务文件还在、它不会再启动任何东西、怎么手工删掉。没有过开机自启的机器人不会多收到任何命令。
- 在设备上手工 `device_install.sh remove`：只删安装目录，不碰系统文件；服务文件留下但不再启动任何东西。之后在控制台点「取消开机自启」会把它删掉（这时没有可启动的管理层，只做取消这一步）。

## 接口

- `POST /api/robots/runtime-install/start` 多一个可选的 `autostart: true`。
- `POST /api/robots/runtime-install/autostart` `{project_id, robot_id}`：给这个控制台装的管理层设开机自启（任务的步骤：开机自启、接入并检查）。没有安装记录 `autostart_not_installed`；不是这个控制台装的 `autostart_not_console`。
- `POST /api/robots/runtime-install/autostart-off` `{project_id, robot_id}`：取消（步骤：取消开机自启、启动、接入并检查）。
- `POST /api/robots/runtime-install/answer` `{project_id, robot_id, job_id, plan, decision: 'confirm'|'decline', sudo_password?}`：回答正在等的那份内容。`plan` 是窗口里那份内容的编号，内容变了（比如密码不对后重新列出）旧的编号不再算数（`autostart_plan_changed`）。
- 任务视图多一个 `autostart`：`state`（`checking` / `waiting` / `running` / `on` / `off` / `left` / `failed`）、`reason`、`message`；等人确认时 `plan`：`kind`（`on`/`off`）、`user`、`host`、`how`、`how_label`、`needs_password`、`unit_path`、`unit`、`commands`、`account_steps`、`script`、`does_not`、`error`、`attempt`、`id`。进度仍只在 `robots/runtime-install/status`，不在 `/api/state`。
- 机器人记录多一个 `runtime_autostart`：`state`（`on`/`off`/`failed`）、`reason`、`message`、`unit`、`package_id`、`port`、`adapter`、`job_id`、`at`；`runtime_install` 多一个 `console_install`（设备上的 `~/neuxmind` 是不是这个控制台的安装）。
- 页面：卡片上「开机自启」一行（在「管理层安装」一行之下）；实时故障里“真机复位卡住”的第 3 步按机器人给命令：已开机自启给 `sudo systemctl restart neuxmind-runtime.service`，控制台装的给安装脚本的 `stop` 和带端口、本体的 `start`，别的不给命令（以前这里写的 `neuxmind-runtime` 服务并不存在）。

## 测试

- `tests/test_runtime_autostart.py`（替身）：安装时选开机自启，列出的内容、等人、确认、只执行那一段管理员脚本（密码只在 sudo 的标准输入里）、服务文件和确认的一字不差、接管、断电重启后自己起来并重新接上；经跳板机；另一个端口和机械臂；不选就什么都不执行；拒绝、超时、停止；回答不对应、缺密码、密码多行；密码不对再问、三次不对；免密、root 登录、没有 sudo、不在 sudoers、要求终端；没有 systemd、别人建的同名服务、为别的账号建的服务；服务起不来时留下什么、怎么取消。密码不出现在快照、视图、机器人记录、审计、日志、任何命令行和设备上的服务日志里。
- `tests/test_runtime_autostart_modes.py`（替身）：卡片上之后再开、已经开着时什么都不做、手工装的拒绝、设备上不是记录的那次安装、没装、同时只能一个任务；取消（服务没了，管理层照常运行、接入；断电后不再起来）、不确认取消；安装被手工删掉后取消；「清理」连服务一起去掉、不确认时留下不会启动的服务文件、「删除后重新安装」前先取消。
- `tests/test_runtime_autostart_device.py`：设备端和控制台生成的服务文件一字不差（四种端口和本体）、服务文件的内容、只有普通值进服务文件、命令行的样子（密码不在命令行）、sudo 原话的分类、管理员脚本的拒绝（摘要不对、别人的文件、不是调用 sudo 的账号、密码不对、没有 systemd）、标记文件、检查的输出。
- `tests/runtime_autostart_logic.cjs`、浏览器 `tests/runtime_autostart_browser.cjs` + `tests/runtime_autostart_browser_fixture.py`（Chromium 和 WebKit）。
- 替身 `tests/autostart_fixture.py`：在 #19 的设备替身上加 sudo、systemd（`systemctl` 替身按服务文件运行 `ExecStartPre`/`ExecStart`）、`id`、`getent` 和“断电重启”；管理员脚本只认 `root_command` 生成的那几种形式，执行时只把两个系统目录换到临时目录里。
- 真实的 Linux：`tests/runtime_autostart_systemd_check.py`（手动，不在套件里）：容器里 systemd 是 1 号进程、真 sudo（要密码）、sshd、真实的安装包和运行时；控制台在本机。结果见下。

### 容器里的实测

2026-11-22，`tests/runtime_autostart_systemd_check.py`：Docker 里的 Ubuntu 22.04（ROS 2 Humble 的基础镜像）加 systemd、sshd、sudo（1.9.9，要密码）、python3-venv；真实的安装包（`scripts/build_device_package.py` 构建）、安装脚本和运行时；控制台（`Platform`）在本机经容器的 SSH 端口操作，心跳间隔调成 5 秒、15 秒算离线。断电 = `docker kill`，停 30 秒后 `docker start`。24 项全部通过：

- 安装时选开机自启：等人确认、输入 sudo 密码后 8.6 秒装完（检查到接入）；设备上的服务文件和确认的一字不差，root 所有、0644；`enabled`、`active/running`，服务的主进程就是 `runtime.pid` 里的运行时，以 `robot` 运行；控制台「已连接」、心跳「在线」。
- 断电重启：服务启动了运行时（没有重试）；控制台的心跳先显示「离线」，通电后自己回到「在线」、通道回到「已连通」（两次运行分别是通电后 1.6 秒和 30.2 秒，取决于心跳重试排到哪一刻），「连接检查」读到管理层。运行时报告急停已锁存：它在非正常结束（断电）后锁存急停，是运行时本来的安全设计，要人确认现场后在控制台「解除急停」。
- 开机时端口被别的程序占着 40 秒（测试用的服务）：systemd 重试 3 次，通电后 46.1 秒运行起来；控制台随后读到管理层。
- 「取消开机自启」：服务文件没了，运行时继续以脱离会话的进程运行并接入；再断电，运行时不再起来。
- 卡片上再「设置开机自启」（这时运行时没在运行）：服务把它启动起来，接入。
- 手工 `device_install.sh remove` 之后断电重启：服务没有启动任何东西（`ConditionResult=no`）；「取消开机自启」只做取消这一步，删掉了留下的服务文件。

ROS 2（2026-11-22，手工，不在脚本里）：用 #21 已提交的安装脚本（29f39a1）和从它构建的两个包，在同一个 Ubuntu 22.04 + ROS 2 Humble + systemd 的容器里以 `robot` 账号 `ROS_DOMAIN_ID=42 NX_ADAPTER=ros2 … install`，再写入 `render_unit(…, 'ros2')` 生成的服务文件并 `restart`：服务 active，`/v1/status` 报 `mode: ros2`、`simulated: false`；运行时的 ROS 节点 `/neuxmind_runtime` 出现在 42 号域上、0 号域上没有，说明 systemd 的干净环境下它仍按 `ros2.env` 进了 Humble 的环境、用的是安装时的域；断电重启后 2 秒内服务又起来。

## 还没有做的、没有验证的

- 没有在真机上开过自启，也没有在真机上断电重启验证过。真机上现在那份管理层是手工装的，按定下来的规则控制台不给它设开机自启；要在真机上验证，先要用户同意用控制台删除重装，再单独同意开自启，断电重启要用户在现场。
- ROS 2：服务经安装脚本进入 #21 的 `ros2.env`。控制台还不能选 ROS 2 本体（#21 的安装脚本还没进控制台的安装包），所以经控制台走过整条路的只有仿真本体；ROS 2 只在容器里手工装、再用这份服务文件启动验证过（见上）。
- 以 root 运行、`Delegate` 让技能沙箱用上 cgroup：见上，没有做。
