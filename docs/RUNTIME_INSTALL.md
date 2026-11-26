# 在控制台里安装管理层（issue #19，需求 R1-03）

以前要在开发机上打出两个安装包，拷到机器人上，登录上去运行 `runtime/deploy/device_install.sh`，再开隧道、填地址和令牌。现在机器人经 SSH 接入以后，在它的卡片上点「安装管理层」，剩下的都在控制台里完成：检查设备、上传安装包、安装、生成令牌、启动、接入（issue #17 的通道）。成功后卡片上连接状态是「在线」，「管理层」一行是「已连通」，控制通道已连接。

代码：`brainos/runtime_install.py`（服务和后台任务）、`brainos/runtime_install_device.py`（在设备上执行的四段固定脚本）、`brainos/runtime_install_plan.py`（按检查结果决定怎么做）、`brainos/runtime_package.py`（随控制台发布的安装包）、`scripts/build_device_package.py`；页面 `web/runtime_install.js`、`web/runtime_install.css`。

## 怎么用

1. 「通过 SSH 调查真机」把机器人接进来（直连或经多级跳板机），选好本体类型。
2. 卡片上点「安装管理层」：表单写明会经哪条 SSH 连接、往哪里写、不做什么；只需要确认端口（默认 8790）。
3. 「开始安装」后窗口里逐步显示：检查设备 → 上传安装包（带进度）→ 安装 → 启动 → 接入并检查。窗口可以关掉，卡片上有一行「管理层安装」，点「查看」再打开。
4. 设备上已经有管理层时，检查完就停下来问（见下文），什么都不上传。
5. 没装完时窗口写明停在哪一步、设备上的输出（最后 40 行）、设备上留下了什么、怎么清理；「清理」要点两次（第二次是「确认清理」）。

## 安装包随控制台一起发布

- 安装包是一个目录：`device_install.sh`、`neuxmind_spec-*.whl`、`neuxmind_runtime-*.whl` 和 `package.json`（每个文件的角色、大小、SHA-256）。编号 `runtime-<版本>-<校验值前 12 位>` 由三个文件的校验值算出，代码不同的两次构建不会同号。
- `scripts/build_device_package.py` 从源码构建：把 `spec/`、`runtime/` 拷到临时目录，用 `pip wheel --no-deps --no-index --no-build-isolation` 打包（不联网，构建用的 Python 要有 setuptools），源码目录里不留构建目录。默认输出到 `.build/device-package`。
- `scripts/build_macos.py` 在冻结后端之前先运行它，把结果作为数据放进应用（`device-package/`），`--check` 也检查它能不能加载。这是对打包脚本的改动，只插入了几行。
- 控制台按顺序找：环境变量 `NEUXMIND_DEVICE_PACKAGE`（测试、开发）→ 应用里的 `device-package/` → 源码里的 `.build/device-package`。发出去之前每个文件都和清单核对，对不上就不发（`install_package_damaged`）；找不到就说明怎么构建（`install_package_missing`）。

## 在设备上做什么、不做什么

- **只在登录账号的主目录里。** 安装包传到 `~/neuxmind-pkg/console/<安装包编号>/`（#18 的上传通道：压缩、断点续传、设备端核对 SHA-256，目标位置只在核对一致后才出现）；管理层装在 `~/neuxmind`（安装脚本的默认位置，#17 也从这里读令牌）。设备上 `~/neuxmind-pkg` 里原有的文件（例如手工安装时放的脚本和包）不动。
- **安装不用 sudo，不改系统文件。** 开机自启要管理员权限，是安装之外单独的一步（issue #20，[RUNTIME_AUTOSTART.md](RUNTIME_AUTOSTART.md)）：表单里勾选“装好后设置开机自启”时，`runtime_install.STAGES` 在「启动」之后、「接入并检查」之前多一步「开机自启」，先列出要以管理员身份执行的内容，人确认后才做；不勾选就没有这一步。
- **管理层只监听机器人本机。** 安装脚本以 `--host 127.0.0.1` 启动；平台经 SSH 的 direct-tcpip 通道访问它。
- **默认是仿真本体。** 适配器按本体类型选：四足 `sim_quadruped`，机械臂 `sim_arm`。两者都不打开设备、不会让机器人动。ROS 2 适配器（#21 的「安装时选择」，`NX_ADAPTER=ros2`）等它的安装脚本进了安装包后，在 `runtime_install_plan.ADAPTERS` 和表单里加一项即可；默认仍是仿真，和 #21 一致。
- **启动按安装时记下的本体。** #21 的安装脚本把本体记在 `~/neuxmind/adapter`。控制台启动（装完后的启动、「启动这个已有的管理层并接入」）时，这个文件写的是 `sim_quadruped` / `sim_arm` / `ros2` 之一就按它启动；卡片上的本体类型只决定安装时装哪个，以及没有这个记录的旧安装（#21 之前）启动时用哪个。记录不认识或是符号链接时不跟随。和 #20 的自启动一致。
- 设备上只执行 `runtime_install_device.SCRIPTS` 里的四段固定脚本（检查、启动一步、查看一步、清理），文本经标准输入交给 `sh -s`，命令行上只有 `sh -s --` 和由字母、数字、`. _ + -` 组成的参数（安装包编号、步骤名、16 位十六进制任务号、端口、适配器名）。安装本身是设备上那份 `device_install.sh` 的 `install` / `start` / `remove`，它守自己的规则（只动带标记文件的安装目录、只给自己启动的运行时发信号）。

## 断开、弱网、再来一次

- 安装和启动在设备上作为**脱离 SSH 会话的任务**运行（`setsid nohup`），输出写进安装包目录的 `<步骤>.log`，结束时写 `<步骤>.exit`（任务号和退出码）。连接断了不影响它；控制台重新登录（间隔 2、5、10、20、30 秒）后接着看结果。同一个任务号只会启动一次：答复在路上丢了，再发一次也不会装两遍。
- 检查、查看、清理都是可以重复执行的；一次答复只有在带着脚本自己的结束标记时才算数，通道中途断开、没有标记的答复按「答复丢了」处理，重连后再问。
- 上传由 #18 的通道续传；它放弃之后，只要原因在连接上（包括第一个字节还没确认时的断开、弱网下超时的 SFTP 请求），安装在时限内（`BUDGETS['upload']`，45 分钟）继续重试。登录被拒、设备拒绝写入（磁盘满）、本机的安装包文件被改动不重试；设备上校验不一致的文件再传一次。#18 的 `TransferError.resumable` 只说明设备上有没有可以接着传的临时文件，不说明该不该重试，所以这里按原因判断。每个文件传完后再和安装包清单的校验值比一次。
- 中途放弃（时限到了、设备连不上、人点了「停止」）之后，再点「安装管理层」会从设备上的现状接着做：
  - 这个安装包的安装任务还在设备上运行 → 等它的结果；
  - 设备上已经是这个控制台用同一个安装包装好的 → 不再安装，没运行就启动，然后接入；
  - 只留下一份这个安装包开始过、没有结束的安装（任务被杀掉、设备断电）→ 安装脚本先把它清掉再装，干净地重来。
- 需求的弱网验收线（1 Mbit/s、单向 300 ms、丢包 30% 时最终完成；60% 时不要求完成，只要求不留下损坏的东西、恢复后能继续）用 `tests/runtime_install_weak_network.py` 在 Linux 容器里以真实丢包检查，结果见「测试」。

### 弱网实测（2026-11-22，Linux 容器里 `tc netem`，只限制控制台到机器人的 SSH，真实的安装脚本、包和运行时）

安装包 370 KB（脚本 12.7 KB、两个包 13.5 KB 和 344 KB），带宽 1 Mbit/s、单向时延 300 ms：

- 丢包 30%：从检查到「在线」一次完成，用时 42 分钟（2532 秒），几乎全部花在上传上（连接断开后 #18 的通道改用 16 KB 小块，每块等一次往返）；设备上每个文件都和清单一致，机器人状态「已连接」。
- 一开始就是丢包 60%：3 分钟内一次 SSH 登录也没完成，安装停在只读的「检查设备」（`install_unreachable`），设备上什么都没有写；网络恢复后再装，10 秒完成。
- 上传进行中变成丢包 60%（已到约 100 KB 时切换）：10 分钟没有任何进展，点「停止」后等这一步放弃（「正在停止」，网络很差时最多约两分钟）；设备上只有两个已核对的完整文件和 #18 的隐藏临时文件，没有损坏的文件，`~/neuxmind` 没有出现。网络恢复后再装，10 秒完成：已完成的两个文件没有再传，第三个包从 #18 最后一次确认的位置接着传——那次它的第一块（256 KB）还没被确认就断了，所以这个包整个又传了一遍。
- 在 Docker Desktop 的绑定挂载上，文件的 inode 号会隔一两分钟变一次，#18 会把它当成“本地文件被改了”而停止；做这类长时间的检查时先把安装包拷进容器。应用里的安装包不受影响。

## 设备上已经有管理层时

检查之后、上传之前决定（`runtime_install_plan.plan`，纯函数，逐项有测试）：

| 设备上的 `~/neuxmind` | 怎么做 |
| --- | --- |
| 没有，或是空目录 | 上传、安装、启动、接入 |
| 不是安装脚本建的目录，或者是符号链接 | 拒绝，什么都不动（`install_target_taken`） |
| 没装完的安装（标记还是 `installing`） | 这个安装包的安装任务还在跑就等它；这个安装包的任务开始过、没有结束（被杀掉、断电）就重新安装（安装脚本先清掉它）；不是这个安装包开始的（可能有人正在手工安装）就先问，只能选「删除后重新安装」并确认 |
| 这个控制台用同一个安装包装好的 | 不问：运行中就直接接入，没运行就启动再接入 |
| 别的方式装好的（手工安装、别的版本的控制台） | **先问，不上传任何东西**：「接入这个已有的管理层（设备上什么都不改）」（运行中才有）；「启动这个已有的管理层并接入」（没运行、版本和安装包相同才有）；「删除它（连同它的状态目录、日志和令牌）后重新安装」（要勾选确认） |

原地升级（保留状态目录）现在不提供：安装脚本没有这个功能。删除后重装会删掉原有的状态目录、日志和令牌，窗口里写明这一点并要求确认。

另外在上传之前拒绝：不是 Linux；没有 Python 3.10 或更新的版本；Python 不能建虚拟环境（Ubuntu 上缺 `python3-venv`，要管理员装，控制台不替你做）；缺少安装脚本要用的命令（`bash`、`flock`、`setsid`、`realpath`、`nohup`）；主目录剩余空间不到 300 MB；端口上已经有别的程序在监听；SSH 服务不允许 TCP 转发（装了也接不上）。这些情况设备上什么都不会留下。

## 令牌

由设备上的安装脚本生成（0600 的 `~/neuxmind/edge-token`），接入时由 #17 的 `attach` 经 SSH 读取并保存在机器人记录里，和手工填写的令牌存在同一个位置。安装任务的进度、结果、留在机器人记录里的结果（`runtime_install`）、审计事件、`/api/state` 快照和日志里都没有令牌（测试逐一检查）。

## 失败时

窗口和机器人记录里的结果都写明：停在哪一步（`failure.stage`）、原因（`failure.code`、`failure.message`）、设备上那一步的输出（`failure.log`，最后 40 行，去掉控制字符）、设备上留下了什么（`left`，失败后再只读看一次设备得出；连不上设备时如实说明）和怎么清理（`cleanup`）。

「清理」只拿走这次安装放到设备上的东西：

- 安装包目录里这个控制台放进去的文件（脚本、两个包、各步骤的任务文件和日志、没传完的临时文件），然后删掉这个目录和空了的 `~/neuxmind-pkg/console`；目录里别的文件不删，会列出来。
- 这次安装装的 `~/neuxmind`（标记文件里是这个安装包）：先移除 #17 的接入，再用这份安装包里的脚本 `remove`。原来就有的管理层不动；不是这次安装留下东西的结果没有「清理」（`install_nothing_to_clean`）。
- 本机关于这些文件的续传记录。

| 步骤 | `failure.code` | 设备上可能留下的 |
| --- | --- | --- |
| 检查设备 | `install_device_unsupported`、`install_no_space`、`install_port_busy`、`install_forwarding_off`、`install_target_taken`、`install_choice_invalid`、`install_replace_unconfirmed`、`install_login_refused`、`install_unreachable` | 什么都没有（检查只读） |
| 上传安装包 | `install_upload_failed`、`install_login_refused`、`install_unreachable` | 安装包目录里已传完的文件、一个没传完的临时文件（再装会续传） |
| 删除原有的管理层 / 安装 / 启动 | `install_step_failed`（带输出）、`install_job_lost`、`install_timeout`、`install_unreachable` | 安装包目录和日志；安装失败时安装脚本自己删掉 `~/neuxmind`，启动失败时留下装好但没运行的 `~/neuxmind` |
| 接入并检查 | `install_link_failed` | 装好并在运行的 `~/neuxmind`；接入可能已保存（之后点「连接检查」即可） |
| 任何一步 | `install_connection_changed`（安装途中 SSH 连接被修改或删除）、`install_robot_removed`、`install_internal` | 视停在哪一步 |

安装进行中不能删除这台机器人，也不能同时再开一次安装（`install_in_progress`）；SSH 连接被修改后，安装在下一步开始前停下。

## 接口

- `POST /api/robots/runtime-install/start` `{project_id, robot_id, port?, existing?, confirm_replace?}`：立刻返回任务的视图；`existing` 是 `use` / `start` / `replace`（检查后要求选择时）。
- `POST /api/robots/runtime-install/status` `{}`：本次启动以来这个工作区每台机器人的安装视图 `{installs: {robot_id: view}}`。进度不在 `/api/state` 里。
- `POST /api/robots/runtime-install/stop` `{project_id, robot_id}`：停止等待；设备上已经开始的那一步在设备上照常做完。最多等 10 秒就返回；被网络卡住的登录或写入要等它自己放弃（最多约两分钟），这期间视图里 `stopping` 为真。
- `POST /api/robots/runtime-install/clean` `{project_id, robot_id}`：按上一次结果清理。
- 视图：`state`（`running` / `needs_choice` / `succeeded` / `failed` / `stopped`）、`stage`、`stages`（每步 `id`、`label`、`state`）、`progress`（上传的 `sent` / `total` 字节）、`package`、`package_dir`、`port`、`adapter`、`choice`（`options`、`found`）、`failure`、`left`、`cleanup`、`can_clean`、`notes`。结束后的结果（不含进度）存在机器人记录的 `runtime_install` 字段里。
- 进程内：`platform.runtime_install`（`start`、`status`、`stop`、`clean`、`running(robot_id)`）。

## 测试

- `tests/test_runtime_install.py`：从添加设备到「在线」（直连、经跳板机、另一个端口和机械臂）、进度、快照不变、并发和删除被拒、只接受 SSH 机器人和已知本体、没有安装包、接入后其余控制台功能可用；令牌不出现在快照、视图、日志、审计里。
- `tests/test_runtime_install_existing.py`：已有管理层的每一种情况（手工安装运行中 / 停止 / 别的版本、确认后重装、不是安装脚本建的目录、符号链接、这个控制台装的、没装完的）。
- `tests/test_runtime_install_failures.py`：检查阶段的每种拒绝（设备上什么都没写）、安装 / 启动 / 接入失败与清理、清理不碰别人装的、SSH 连接被修改、上传中断开续传、安装中断开不重装、设备长时间连不上后再装接着装、停止后再装等原来的任务。
- `tests/test_runtime_install_network.py`：经一个不断切断连接的中继安装能完成且只装一次；连接差到登录不上时不留下损坏的文件，恢复后续传完成。
- `tests/test_runtime_install_plan.py`：决策表；`tests/test_runtime_package.py`：安装包清单、篡改检测、查找顺序、真实构建和应用打包里带上它。
- `tests/test_runtime_install_real.py`（只在 Linux 上运行）：真实的 `device_install.sh`、真实的包和运行时——直连、经跳板机、真实安装途中断开、手工安装的只接入、真实启动失败后清理、端口被占。
- `tests/test_runtime_install_adapter.py`：启动按安装时记下的本体（四足卡片上记下 sim_arm 的起成 sim_arm，ros2 不被起成仿真，没有记录的旧机械臂安装仍起成 sim_arm，不认识的记录和链接不跟随，替换时安装控制台选的本体）。
- 浏览器：`tests/runtime_install_browser.cjs` + `tests/runtime_install_browser_fixture.py`（Chromium 和 WebKit）；逻辑：`tests/runtime_install_logic.cjs`。
- 弱网：`tests/runtime_install_weak_network.py`（只能在 Linux 容器里以 NET_ADMIN 手动运行，不在任何套件里）。

设备替身 `tests/install_fixture.py` 在 #18 的上传替身上加了三个插入点（`tests/upload_fixture.py`：别的命令、direct-tcpip 转发）；它真实执行产品的脚本，只把安装脚本换成一个同样行为的替身（Linux 上的真实测试用真的）。

## 还没有做的、没有验证的

- 没有在真机上安装过。真机（一台 Jetson Orin NX）上现在有一份手工安装的管理层，按上面的规则控制台只会停下来问，不会动它；在真机上试之前要用户同意。
- 需求里「机器人状态从『启动自检』开始」依赖 R4 的机器人状态机，代码里还没有这个状态机；安装完成后卡片显示的是连接状态和管理层报告的状态。
- 原地升级（保留状态目录）。真实本体（ROS 2 适配器）在 issue #38 里加上了：[ROBOT_BODY.md](ROBOT_BODY.md)。开机自启见 [RUNTIME_AUTOSTART.md](RUNTIME_AUTOSTART.md)（#20）。
- 安装包里的运行时是控制台构建时的源码版本；控制台升级后，设备上旧安装包装的管理层会被识别为「别的方式装好的」，要人选择接入或删除重装。
