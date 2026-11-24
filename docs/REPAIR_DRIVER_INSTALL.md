# 修复助手安装并启动运控驱动（issue #29，R1 F15）

需求 R1-10：设备上缺运控驱动时由修复助手装上；要管理员权限的命令逐条列出，人批准后执行；装完启动驱动，重新读取档案核对；整个过程不发运动指令，安全状态保持锁定。

issue #28 让平台认出“缺运控驱动”（问题 `motion-driver:<能力编号>`，见 [运控驱动检查](MOTION_DRIVER_CHECK.md)），但修复助手拿到它只能排查：内置 Skill 不许启动会驱动电机的程序，产品拒绝 `source`（加载不了 ROS 2），命令里出现 `cmd_vel` 就拒绝（只读查询也不行），每条命令最长 600 秒。这一轮把这些规则改成下面的样子。规则在 `brainos/driver_repair.py`，`hardware_init` 在原有规则的位置调用它。

## 用户定下的（2026-11-22）

- 启动运控驱动“走默认允许执行”：不对所有启动服务的命令另加拦截。
- 驱动启动后本体可能自己上电或站立：“要求在现场”。
- 两条合在一起：“每次启动都现场勾选”。助手标明为启动运控驱动的那一条命令，即使开了默认允许执行也停下来，等人勾选“我在现场”后批准；其他命令（包括其他服务的启动）照旧。
- 资料库里的安装包用 issue #31 的传送：#31 的分支合进了这个分支。

## 修复助手现在怎么处理缺运控驱动

1. 只读排查：ROS 发行版、驱动包、驱动服务、日志。要用 `ros2` 命令时 `ros2_env=true`。
2. 驱动装了但没运行：直接到第 4 步。
3. 没装：`driver_packages` 查资料库 → `driver_package_deliver` 把“可用”的安装包送到设备（在设备上核对校验值）→ `device_fix` 安装（`.deb` 用 `apt-get install -y '<路径>'`；源码包解压后 `colcon build`，`build_step=true`）。资料库里没有可用的安装包：标 `needs_user`，请用户上传到资料库或在现场安装。
4. 启动：`systemctl start <驱动服务>`，或没有服务的源码驱动 `nohup ros2 launch … &`。这一条 `starts_motion_driver=true`，等人在现场确认。
5. `hardware_rescan` 重新读取 ROS 2（#28），接口出现才算修好；结果写进设备档案的 ROS 2 部分。报告 `fixed` / `not_fixed` / `needs_user`。

整个过程不发运动指令、不解除急停（见下面的规则）。

## 规则：放开了什么，仍然拒绝什么

| 放开 | 仍然拒绝 |
| --- | --- |
| 只读的 `ros2` 查询：`topic list/info/echo/hz/bw/type/find/delay`、`node list/info`、`service list/type/find`、`action list/info`、`interface …`、`pkg list/prefix/executables/xml`、`param list/get/describe/dump`、`lifecycle get/list/nodes`、`control list_*`/`view_controller_chains`、`daemon`、`bag info`、`doctor`；命令里出现 `cmd_vel` 本身不再拒绝 | 发布话题、调用服务、发送动作目标、改参数（`set/load/delete`）、切换生命周期、切换或加载控制器、回放数据包、遥控（`teleop`），ROS 1 的同类命令；不管程序怎么写（完整路径、通配符、`python -m ros2cli`），只认动作的字样。`ros2` 作为程序出现时后面必须是上面的只读子命令或 `launch`/`run`，所以 `xargs ros2 …`、`alias r=ros2` 也拒绝 |
| ROS 2 Humble 的环境由产品加：`ros2_env=true` 在命令前加 `. /opt/ros/humble/setup.sh`，`ros2_workspace`（绝对路径，只含字母、数字和 `._+-`）再加 `<工作空间>/install/setup.sh`；设备上没有这个文件时命令不执行（退出码 127，写明原因） | 助手自己写 `source`、`.`、`bash -c` 等（原有规则不变） |
| 安装并启动运控驱动：启动 ROS 2 节点（`ros2 launch`/`ros2 run`，ROS 1 的 `roslaunch`/`rosrun`）或名字像驱动的服务（`systemctl start/restart/try-restart/enable --now`、`service … start`，名字里有 ros、driver、bringup、motion、chassis、base、legged、quadruped、locomotion、controller、robot、sdk、motor、servo、walk、dog；也认 `/etc/init.d/<名字> start`、`supervisorctl`、`pm2`）的命令要 `starts_motion_driver=true` | 没有标明就启动上面这些；`device_exec` 里不能启动（它没有这个标记） |
| 编译或安装驱动 `build_step=true` 时最长 3600 秒：命令里每一段都必须是 `colcon build`、`catkin`、`make`、`cmake`、`ninja`、`apt-get install`、`dpkg -i`、`pip install`、`rosdep install`（中间可以 `cd`、`mkdir` 到工作空间，输出可以重定向到文件或经过 `tail`、`head`、`tee`） | 其他命令仍最长 600 秒；夹着别的命令、命令替换、或同时标明启动驱动的，不给更长的时限；仍最多 10 行 |
| 用运行时的 HTTP 接口查看状态（`/v1/status`）、让它停止（`/v1/stop`）；`~/neuxmind-pkg/` 下的安装包、源码和日志 | 碰运行时就拒绝：HTTP 接口的 reset、start、deploy、evaluate、validate、runs、capabilities、conformance、safety、skills、models（URL 转义也解开来查）；命令里出现 `neux` 开头的名字（`~/neuxmind-pkg` 除外：运行时的目录、命令行工具、进程名都在内，查看状态的命令行也拒绝）、`edge-token`、`runtime.pid`、`device_install.sh`，或用通配符写出的这些名字（`~/n?uxmind`）；任何“急停 + release/reset/clear/unlock/disable”的命令 |
|  | 直接向 CAN 总线或串口设备发数据：`cansend`、`cangen`、`canplayer` 等，写入 `/dev/tty…`、`/dev/serial/…`（电机控制器常挂在这里，绕过 ROS 2 也能驱动电机）；读取和设置波特率可以 |
|  | 改 ROS 2 的安装目录 `/opt/ros` 或任何 `setup.sh`、`setup.bash`、`local_setup.*`（写入、复制、移动、链接、删除、`sed -i`、`tar -C`、`unzip -d`、`touch`、`mount` 等；`/opt//ros`、`/opt/./ros`、`/opt/r*` 也算；`git` 也算；`cd /opt/ros…` 之后只能读，执行别的程序也拒绝）：产品加载环境时会执行这些文件 |

标明 `starts_motion_driver` 不会放开任何别的规则。这些规则在原命令、去掉引号、反斜杠和续行之后的命令、再把 `$IFS` 当空格、别的变量当空、`{a,b}` 当 `a b`、URL 转义解开之后的命令上都查一遍；`ros2` 写成通配符（`ros?`、`r*2`）也认作 `ros2`。修复会话的 `device_fix` 和 `device_exec` 都适用。

默认允许执行时，在 issue #13 的“先问再执行”名单之外，这些也停下来等人批准：多行命令和 `if`、`for`、`while` 这类结构；程序名里有通配符、花括号或变量；改变程序查找路径或预先加载代码的变量（`PATH`、`LD_*`、`PYTHON*`、`export` 等）；含有 `$USER`、`$HOME` 以外的变量；让 shell 读入脚本（文件、标准输入、here-document，带任何选项）；用解释器运行代码或模块（只有 `--version` 和 `-m pip install` 不问）；按路径运行 `/usr/bin`、`/usr/sbin`、`/bin`、`/sbin` 以外的程序（`/opt`、`/usr/local`、工作空间、相对路径、带 `..` 的路径）；`env -…`、`systemd-run`、`setsid`、`nsenter`、`chroot`、`unshare`、`runuser`、`busybox`、`doas`、`ssh`、调试和跟踪工具（`gdb`、`strace`、`ltrace`、`perf`、`valgrind`）、容器（`docker`、`podman`、`lxc` 等）、`supervisorctl`、`pm2`；读 `/proc/…/environ`（运行时的令牌可能在环境变量里）；`kill`、`pkill`、`killall`；带 `-o` 选项的 `apt`（可以挂安装时执行的程序）；对读以外的命令用通配符选中文件；加载工作空间（`ros2_workspace`）的命令，因为产品看不到它的 `setup.sh`。两轮独立安全审查（第二轮之后又让审查者用同一批脚本复查了两次）找到的写法都在 `tests/test_repair_driver_rules.py` 里有测试。

## 现场确认

- 助手标明 `starts_motion_driver=true` 的命令，审批框写明本体可能上电、站立或收腿，并有一个勾选框“我在现场：我在机器人旁边，周围的人和障碍物已经清开，能随时按下硬件急停”。
- 不勾不能批准：页面先拦；直接调接口也一样，`engineering/approve` 对这条命令的批准在没有现场确认时被拒绝。
- 勾选后页面先发 `engineering/hardware/onsite`（`{project_id, session_id, approval_id, onsite: true}`），再发批准。确认只对这一条待批准的命令有效（按请求里的 `nonce` 认），下一次启动要重新勾。
- 执行前产品再查一次：这条命令的 `nonce` 没有现场确认就不执行。
- 默认允许执行时，这条命令不直接执行，停下来写明原因（“这条命令没有直接执行：启动运控驱动的命令要有人在现场确认……”）；再打开默认允许执行也不会让它通过。
- 记录：会话的 `hardware.onsite_confirmations`（最近 20 条：`nonce`、`approval_id`、命令、用途、`confirmed_at`、`confirmed_by` 为账号编号），会话记录里一行“已确认有人在现场：…”，审计事件 `device.motion_driver_onsite_confirmed`。
- 产品核实不了人是否真的在现场：这是人的声明。

## 资料库里的安装包

- `driver_packages`（只读，不联系设备）：列出资料库里的驱动安装包（最多 50 个），每个带标注、格式、大小、校验值、是否“可用”、本体类型是否对得上、是否已经送到这台设备。可用且类型对得上的排在前面。没登录云平台或云平台不可用时返回 `available: false` 和原因，并提示标 `needs_user`。结果存成一条证据。
- `driver_package_deliver`：和 `device_fix` 一样进入审批（默认允许执行时直接执行）。提出时就查这份资料（必须是安装包、“可用”、不属于另一台设备，否则直接退回给助手），审批框显示文件名、标注、大小、完整校验值和设备上的位置。批准后调用 #31 的 `deliver`；送之前核对保存的连接现在通向的仍是这次会话的那台机器（主机指纹），不是就不送。返回设备上的路径和 sha256，存成证据，按一次改动计数（之后要重新扫描才能标修好）。
- 只送文件，不解压、不安装；安装是之后的 `device_fix`。
- 文件名和标注是上传的人填的，交给助手时写明是数据不是指令。
- issue #30（修复会话能查资料库的文档）接在 `driver_repair.DriverRepair._packages` 旁边：这一轮只列安装包。#34 合并后，资料可以按所属设备过滤（`device_id`），现在列出的包里可能有别的设备的，送的时候会被拒绝。

## 接口和数据

- `device_fix` 多四个参数：`ros2_env`、`ros2_workspace`、`starts_motion_driver`（工具说明里列为必填）、`build_step`；`timeout_seconds` 的上限写成 3600（没有 `build_step` 时仍按 600 校验）。请求多 `environment`（产品加的行）、`starts_motion_driver`、`build_step`，启动驱动的请求多 `nonce`。
- 修复会话的 `device_exec` 多 `ros2_env`、`ros2_workspace`。
- 新工具 `driver_packages`、`driver_package_deliver`；审批方法 `driver_package_deliver/requestApproval`，加进了默认允许执行的方法表。
- 新路由 `engineering/hardware/onsite`。
- 内置 Skill `hardware-init` 升到 1.3.0：`SKILL.md` 的“必须遵守”和 `references/playbooks.md` 的 `motion-driver` 一节按上面改写；`unit:<服务>` 一节写明启动运控驱动是唯一的例外。
- #28 的页面文字跟着改了：交给助手前的说明、硬件页上这类问题的标签（“助手可安装，启动时要人在现场”，仍然不默认勾选）、问题的 `hint`。
- 没有新错误码，没有新的按机器人保存的记录种类，`/api/state` 没有变。

## 界面

审批框（`web/repair_driver.js`、`web/repair_driver.css`，包装 `hardwareFixApproval`、`engineApproval`、`engineSubmit`）：

- 产品加的 ROS 2 行显示在命令正下方。
- 启动运控驱动：红色的一块写明会发生什么，加勾选框；没勾就批准时勾选框标红，表单写“启动运控驱动前请先勾选‘我在现场’”。
- 送安装包：文件名、标注、大小、完整校验值、设备上的位置、对应问题，写明只送文件。

## 验证情况

- `tests/test_repair_driver_rules.py`（43 项，许多带多个子用例）：每条放开的规则有放行和仍拒绝的例子；ROS 2 环境（包括在 `sh` 下真的执行加了环境的命令，设备上没有 Humble 时退出 127）；编译时限；启动驱动的标记；现场确认（默认允许执行时停下、不确认不能批准、确认只对一条有效、执行前再查、只能确认待批准的启动命令）；默认允许执行时停下等人的写法；两轮安全审查复现过的每一种写法；Skill 和会话指令。
- `tests/test_repair_driver_install.py`（9 项，验收）：替身设备 `tests/driver_install_fixture.py`（在 #31 的 `UploadDevice` 上，安装和启动只按命令文字模拟，不真的执行）和带列表接口的资料库替身。人逐条批准：查资料库、送包、安装、不勾现场时启动被拒、勾了之后启动，重新扫描后两个运控问题消失、设备档案里出现 `/cmd_vel`、`/odom`，报告 `fixed`；设备收到的命令里没有任何运动或解除锁定的命令，设备自己的急停一直锁定，平台上这台机器人一直显示急停锁存，没有运行、部署等记录。另一项经随包工程运行时和脚本模型、开着默认允许执行跑完整个会话：送包和安装直接执行，只有启动停下等现场确认。
- `tests/repair_driver_logic.cjs`（33 项）和 `tests/repair_driver_browser.cjs`（替身 `tests/repair_driver_browser_fixture.py`，Chromium 与 WebKit）：整个页面流程，包括不勾现场时的拒绝、键盘操作勾选框、1024/768/375/320 宽度下审批框不横向滚动。
- 跟着改的测试：`tests/test_hardware_init.py`（修复会话的工具表）、`tests/test_repair_policy.py`、`tests/test_motion_drivers.py`（Skill 版本，#28 的“规则没有放开”改成“仍然不发运动指令”）、`tests/motion_drivers_logic.cjs`、`tests/motion_drivers_browser.cjs`（页面文字）。
- **没有在真机上运行任何一步**：没有在真机上装或启动过驱动，现场确认也没有在真机旁做过。

## 已知限制

- 文字规则不可能穷尽 shell 的写法。逐条批准时由人把关；默认允许执行时，规则之外还有“先问再执行”名单（见上）。已知漏不掉的例子：按进程号 `kill` 掉运行时；厂家自带的、装在 `/usr/bin` 一类位置的命令行工具让本体站立或使能（产品不认识这些工具）；以 root 执行的命令可以改系统里任何文件。真正的保证要靠运行时自己拒绝来自修复会话的解锁和运动请求，这一轮没有做。
- 启动驱动的命令要助手自己标明。名字不像驱动的服务（`systemctl start <别的名字>`）被启动时，产品认不出来：逐条批准时由人看，默认允许执行时只靠模型遵守 Skill。这是用户选择“走默认允许执行”的结果。
- `make` 可以带任何目标，所以 `build_step` 里的 `make <目标>` 会得到更长的时限；影响只是时限。
- 默认允许执行时，`ros2 node …` 的只读查询也会停下来等人：issue #13 的解释器规则把 `node list` 读成运行 Node.js，这一轮没有改 #13 的那一行。
- 送安装包之前核对了机器人当前的连接和主机指纹，但 #31 的 `deliver` 会再读一次机器人的连接：两次读取之间连接被改到别的机器，包会送到新的那台，送完才报错（记录不会交给助手用）。
- 默认允许执行时，资料库里的 `.deb` 会不经人看直接安装，安装脚本以 root 运行。依靠的是资料库里那份安装包已经由人确认为“可用”（R1-13）；校验值只说明文件和云端记录一致，没有签名。
- 只读的命令用通配符指向运行时的目录（`cat ~/n*/state/…`、`cat ~/*/state/…`）不拒绝也不停下：只读，不改锁存。
- 现场确认是人的声明，产品核实不了。
- 只支持 ROS 2 Humble；`ros2_env` 加载的是默认设置，节点用了别的 `ROS_DOMAIN_ID` 时助手自己的 `ros2` 查询看不到，以 `hardware_rescan` 的结果为准（它采用正在运行的节点的设置）。
- “运控接口出现”仍按 #28 的方式判断：只看话题在不在，不看谁在发布或订阅。
