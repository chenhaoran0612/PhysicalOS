# 设备档案与 ROS 2 读取（issue #27）

真机通过 SSH 接入后，可以读一份**设备档案**：系统、硬件、ROS 2、资源占用四部分合成一条记录，每部分带自己的读取时间。前三项以外的内容都不是新读取：硬件与系统来自硬件扫描（[HARDWARE_INIT.md](HARDWARE_INIT.md)），资源占用来自真机状态页的读取（[ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md#跳板机与真机实时状态)）。新增的只有 ROS 2：发行版、正在运行的节点、话题与消息类型、动作、服务。

全程只读，不调用模型，不会让机器人运动。

## 界面

- “我的机器人”里，有 SSH 连接的真机卡片在“实时状态”旁边多了“设备档案”。
- 第一次打开自动读取（通常二三十秒：先读硬件和资源占用，再读 ROS 2）；之后打开显示保存的那一份和它的读取时间，点“重新读取”才会再登录设备。
- 上方是系统信息，下面三个页签：
  - **ROS 2**：一行结论（发行版 + “支持”/“不支持”/“没有读到 ROS 2”），域 ID、RMW、环境变量来源、`/opt/ros` 下已安装的发行版，再是话题、节点、动作、服务四份清单，可按名称或类型筛选。
  - **硬件**：和状态页同一份设备表（设备、类别、接口、驱动、工作状态）。
  - **资源占用**：开机时长、负载、核心数、温度、内存、根分区，以及占用最高的进程（只显示程序名）。
- 没读到的清单显示“没读到”，不显示成空表；这一次没读到 ROS 2 而以前读到过时，显示以前那份、它自己的时间和这次的原因。
- 前端代码在 `web/device_profile.js`、`web/device_profile.css`，用到 `web/live_status.js` 的格式化函数和样式，通过包装 `engineRobotCard`、`engineAction`、`dialog` 接入，不改这些共享函数。

## ROS 2 的只读命令

命令是 `brainos/ros2_profile.py` 里的常量 `ROS2_COMMAND`，以 `sh -c` 执行，模型和用户的文本都不会拼进去（请求里多带的字段不会到达设备，有测试）。它**不并进硬件扫描**：它要加载设备上的一个文件、运行一个会加入机器人 ROS 网络的工具，这两件事硬件扫描从来不做。`hardware_scan.SCAN_COMMAND` 和 `device_software.SOFTWARE_COMMAND` 没有改，“不出现 `ros2`、不加载文件”的两条测试照旧。

它依次做这些事，输出按 `@@NX-ROS:<段名>@@` 分段：

1. **install**：列出 `/opt/ros/*/` 下带 `setup.sh` 的目录，各自有没有 `bin/ros2`（ROS 2）或 `bin/roscore`（ROS 1）。
2. **env**：取 ROS 设置。机器人自己的节点启动时带的设置（`ROS_DOMAIN_ID`、所用的 RMW、DDS 配置）SSH 会话里没有，不取就会在另一个域里读，什么也看不到。按这个顺序找（档案的 `environment.source` 记下是哪一种）：
   1. `process`：一个加载了 ROS 2 客户端库的进程（`/proc/<pid>/maps` 里有 `librcl`，不管命令行长什么样都是节点）。`ros2` 工具自己的守护进程不算：它带的是当初启动它的那个 shell 的环境，可能和节点不在一个域。没有这样的进程时，任何环境里带 `ROS_DISTRO` 的进程。
   2. `session`：都没有时，SSH 会话自己的环境里有 ROS 设置就用它。
   3. `default`：都没有，按默认设置读（域 0），档案里写明清单可能不全。

   只取 `ENVIRONMENT_NAMES` 里的 11 个变量名，别的一概不读；打印出来的只有发行版、ROS 版本、域 ID、RMW、是否只在本机通信这五项，以及“采用了哪些变量名”，值由解析器按格式校验。只读得到同一账号（或以 root 登录时所有账号）的进程。
3. **setup**：选定发行版并加载 `/opt/ros/<发行版>/setup.sh`。这是命令加载的唯一一个文件；发行版名字来自设备，用进路径之前先校验只含 `a-z`、`0-9`、`_`。运行中的进程说了自己是哪个发行版，就只加载那一个；它不在 `/opt/ros` 下时什么也不加载、不读清单，档案里写明。**不会拿旁边装着的 Humble 去读、再把这台设备报成 Humble。** 没有运行中的进程时才依次选 Humble、`/opt/ros` 下第一个 ROS 2。
4. **nodes / topics / services**：三次 `ros2 … list`：
   - `ros2 node list --no-daemon --spin-time 3`
   - `ros2 topic list -t --include-hidden-topics --no-daemon --spin-time 3`
   - `ros2 service list -t --no-daemon --spin-time 3`

命令**不做**的事（`tests/test_ros2_profile.py` 按词检查，并限定 `ros2` 只出现这三种调用）：发布话题、调用服务、发送动作目标、读写参数、`ros2 run` / `launch` / `bag` / `lifecycle`，`sudo`、安装、删除文件，把任何东西放到后台。命令自己不写文件：重定向只有 `2>&1` 和到 `/dev/null` 两种。

**`ros2` 工具自己会留下的东西**（在 Humble 容器里核对过，和在设备上手工执行一次 `ros2 topic list` 留下的一样，命令改不了）：

- 每次调用在 SSH 账号的 `~/.ros/log` 下留一个**空的日志文件**（`python3_<进程号>_<时间>.log`，0 字节），一次读取三个。Humble 的这几条列表命令没有关掉它的开关（`--disable-external-lib-logs` 传不进去，`RCL_LOGGING_IMPLEMENTATION` 在运行时不起作用，把 `ROS_LOG_DIR` 指到不能写的地方工具会直接失败）。
- Fast DDS 可能在 `/dev/shm` 留下一个端口文件和它的信号量（内存里，重启后消失，同一端口会被复用，不会越积越多）。
- 工具按机器人自己的 DDS 配置运行（上面取来的 `CYCLONEDDS_URI`、Fast DDS 配置文件路径）。那份配置如果打开了写文件的调试跟踪，工具也会照着写。

几点设计：

- **不启动 ros2 守护进程。** `ros2` 的列表命令默认会在设备上拉起一个常驻的守护进程；每次调用都带 `--no-daemon`，读完后设备上不留任何进程。代价是每次调用要自己等 3 秒（`--spin-time`）来发现其他节点。
- **动作不调用 `ros2 action list`。** Humble 的这条命令不接受 `--no-daemon`，没有守护进程时会拉起一个。动作改从话题清单里读：拥有 `…/_action/feedback` 话题的就是一个动作，类型是话题类型去掉 `_FeedbackMessage`。这和 ROS 客户端库找动作的办法相同，结果与 `ros2 action list -t` 一致。所以话题用 `--include-hidden-topics` 读全，再由解析器按 `ros2 topic list` 的规则去掉隐藏话题（名称里有以 `_` 开头的一段）。
- **超时与上限。** 每次 `ros2` 调用在 `timeout -k 1 10` 之下运行，卡住的工具会在设备上被结束；三次全卡住也在 `TIMEOUT_SECONDS`（45 秒）内结束。设备上没有 `timeout` 命令时不运行 `ros2`，档案里写明。每份清单限行数（节点 600、话题 2400、服务 3200 行）和每行宽度（300 / 400 / 400 个字符），整体输出上限 512 KB（`OUTPUT_LIMIT`）；达到任何一个上限都记为不完整并写明。解析后的清单上限：节点 500、话题 2000、服务 3000、动作 500。
- **名称按语法校验。** 节点、话题、服务的名称和类型逐个按 ROS 名称的语法检查后才进入档案；工具混在输出里的警告不会被当成名称。某次调用失败时，只引用它最后一行输出（最多 300 个字符，过连接凭据的过滤；里面可能有设备上的路径）。

## 档案的结构

`brainos/device_profile.py` 把一次完整的状态读取和 ROS 2 读取合成一条记录（`device_profiles`，每台机器人一条，`id` 为 `device-profile-<robot_id>`）。issue #23（设备接口表）和 #28（运控驱动检查）在它上面做，键名只随 `schema` 改变。

| 键 | 内容 |
| --- | --- |
| `schema` | `device.profile.v1` |
| `robot_id` / `project_id` / `connection_id` / `connection_revision` | 属于谁；`connection_revision` 是读取时连接的 `updated_at` |
| `created_at` / `duration_ms` | 这条记录写下的时间（ISO）和整次读取的用时 |
| `system` | `checked_at`、`hostname`、`model`、`os`、`kernel`、`arch`、`jetson`、`cpu`、`cores` |
| `hardware` | 状态页的硬件部分原样：`checked_at`、`duration_ms`、`complete`、`problem`、`summary`、`categories`、`devices`、`issues`、`notes` |
| `resources` | `checked_at`、`uptime_seconds`、`load`、`memory`、`disk_root`、`temperature_c`、`cores`、`process_count`、`top`、`processes_checked_at`、`problem` |
| `ros2` | 见下 |

所有 `checked_at` 都是 Unix 秒，是那一部分自己的读取时间。`problem` 不为空表示这一次没读到这一部分，里面的内容是上一次的（时间也是上一次的）或全部未知。

`ros2` 部分：

| 键 | 内容 |
| --- | --- |
| `status` | `ok`：Humble，三份清单都读到；`incomplete`：Humble，但有清单没读到或被截断；`unsupported`：不是 Humble（含 ROS 1）；`absent`：设备上没有读到 ROS；`not_read`：从未读到（看 `problem`） |
| `found` | 设备上有没有 ROS 的迹象；`not_read` 时为 `null` |
| `distro` / `generation` | 发行版名（小写）和 1 或 2；读不出时为 `null`。有进程在运行时是它们用的那个，否则是加载的那个 |
| `supported` | 只有 `humble` 为 `true`（`ros2_profile.SUPPORTED_DISTROS`）；没有 ROS 时为 `null` |
| `installed` | `/opt/ros` 下的发行版：`[{name, generation}]` |
| `environment` | `source`（`process` / `session` / `default`，见上）、`domain_id`、`rmw`、`localhost_only`、`adopted`（采用的变量名） |
| `nodes` | `[{name}]`，按名称排序 |
| `topics` / `services` / `actions` | `[{name, types}]`，按名称排序；`types` 是列表（同一名称上可能有多种类型） |
| `notes` | 给人看的说明：哪条命令失败及原因、清单被截断、环境用了默认值、有重名节点等 |
| `complete` | 输出完整、没有任何清单被截断 |
| `checked_at` / `duration_ms` / `problem` | 同上 |

**没读到一律是 `null`，不是空列表**：`[]` 表示工具回答了、网络上确实没有。不支持的发行版只做标记：ROS 2 的其他发行版照样读出清单供参考，ROS 1 不读。没有 ROS 的设备 `status` 为 `absent`，不算失败，档案的其他部分照常。

## 读取的过程与保存

- `POST /api/engineering/profile/read`，`{project_id, robot_id}` → 读取并返回档案。先做一次完整的状态读取（基本状态、软件、硬件扫描，走状态页的 SSH 会话和它的规则：这台机器人正在别处扫描硬件或有修复会话在进行时，硬件部分保留之前的并写明原因），再另开一个 SSH 会话执行 ROS 2 命令。先后顺序是有意的：ROS 2 的工具不该出现在资源占用的数字里。
- `POST /api/engineering/profile`，`{project_id, robot_id}` → `{profile, reading}`：保存的那一份（没有则为 `null`）和此刻是否正在读取。不联系设备。
- 档案里的硬件表和硬件扫描记录一样，带机器人自己各网卡的地址；不带登录信息，也不带怎么连到它（目标机和跳板机的地址、端口、用户名在连接记录里，不在档案里）。
- 设备连不上时返回错误，保存的档案不动。ROS 2 没读到（超时、会话中断、输出无法解析）不会让整份档案失败：沿用上一份的 `ros2` 并写明原因，从未读到过则全部未知。
- 同一台机器人同时只读一份；状态页正在读这台机器人时请稍后再读。
- 连接被修改过（issue #9：可能换了路线、登录方式或机器）之后，之前读到的档案不再当作这台机器人的显示，也不会被沿用。只改“关注的服务”不算。
- 每次读取写一条审计 `device.profile_read`（机器人、档案 id、ROS 2 的状态和发行版，不含读到的内容）。状态快照不带档案。
- 删除机器人（issue #11）时档案一起删除（`robot_remove.ROBOT_RECORDS`）；读取途中机器人被删除时不会留下档案。页面状态里带一份哪台机器人有档案的清单（`device_profiles`，只有 `id`、`project_id`、`robot_id`，没有档案内容和时间，所以重新读取不会让页面重画），删除机器人的确认框据此列出“读取到的设备档案”。
- 其他功能要在已打开的 SSH 会话上只读 ROS 2 时用 `device_profile.read_ros2(device)`。

## 验证情况

- `tests/test_ros2_profile.py`（45 项）：命令只读（禁用词、只有 `2>&1` 和到 `/dev/null` 两种重定向、没有后台任务、只加载一个文件、`ros2` 只有三种列表调用、每次都带 `--no-daemon`、时限与上限、没有 `timeout` 时不运行、只取指定的环境变量并按节点优先的顺序、不拿别的发行版顶替），脚本能被 `sh -n` 解析；解析 Humble 机器人的替身输出，**话题清单与 `ros2 topic list` 的输出逐行一致**、动作与 `ros2 action list -t` 一致；其他发行版和 ROS 1 标为不支持，运行的发行版和装着的不一样时以运行的为准；没有 ROS、装了但没运行、不在 `/opt/ros` 下；单条命令超时或失败、警告混在输出里、重名节点、清单或单行过长、输出被截断；恶意输出不会进入档案。
- `tests/test_device_profile.py`（28 项，SSH 协议替身，含经跳板机）：一次读取合成四部分并各带时间；到达设备的只有四条固定命令，跳板机上没有命令；请求里的多余字段到不了设备；档案里没有登录信息、SSH 路线（目标机和跳板机的地址、端口、用户名）和命令行；保存、覆盖、读回、审计；随机器人删除，页面状态里只列出哪台机器人有档案；连接改过后不沿用；ROS 2 超时、无法解析、输出超限、设备连不上、硬件正在别处扫描时的表现；同一台机器人不并发读取。
- `tests/device_profile_logic.cjs`（51 项）：各种状态下页面的结论、清单、筛选、转义和卡片按钮。
- `tests/device_profile_browser.cjs`（替身 `tests/device_profile_browser_fixture.py`）：从卡片打开、首次自动读取、话题清单与 `ros2 topic list` 一致、四份清单与筛选、硬件与资源占用、再次打开不重新读取、ROS 2 读取未完成时保留之前的清单、工具报错、其他发行版标为不支持、没有 ROS 2 时不报警；1024 到 320 px 六个宽度下三个页签都不横向溢出；键盘操作。在 Chromium 与 WebKit 上通过。
- **在真实的 ROS 2 Humble 上执行过命令本身**：`ros:humble-ros-base` 容器（`/bin/sh` 是 dash）里起了两个节点（一个订阅、一个发布、一个服务、一个动作服务器，域 ID 42），在没有任何 ROS 环境变量的 shell 里以产品实际发送的形式执行。结果：话题、动作、服务、节点四份清单与同一容器里 `ros2 topic list`、`ros2 action list -t`、`ros2 service list -t`、`ros2 node list` 的输出逐行相同；域 ID 和 RMW 取自节点进程；约 10 秒；执行后没有 ros2 守护进程，也没有残留进程（对照：那几条普通的 `ros2` 命令执行后留下了一个守护进程）。同样在容器里跑过的还有：换一个账号执行（读不到节点进程的环境，按默认域读取，档案里有相应说明）；节点的环境说自己是另一个发行版、旁边装着 Humble（报那个发行版、不支持、不读清单）；先有一个默认域上的 ros2 守护进程、节点在域 42（取到的是节点的环境）；没有节点、会话自己带 ROS 环境（记为 `session`）。留下的文件也是在这里核对的，见上面“`ros2` 工具自己会留下的东西”。
- **在一台真机上执行过一次 ROS 2 的命令**（2026-11-19，Jetson Orin NX，JetPack 6 / Ubuntu 22.04，ROS 2 Humble，经跳板机，以产品实际发送的 `sh -c` 形式手工发出）：`status` 为 `ok`，读到 9 个节点、26 个话题、79 个服务、0 个动作，解析器没有丢弃任何一行；**话题清单与随后在设备上执行的 `ros2 topic list` 逐行相同**。这台机器的节点运行在非默认的域上、带 Fast DDS 配置文件，而账号的 `.bashrc` 两样都没有设：环境是从节点进程取到的，不取的话读到的会是另一个域。连登录在内共 19 秒，三次 `ros2` 调用各约 4 秒（当时 8 核的负载在 4 以上）；读完没有 ros2 守护进程和残留进程；`~/.ros/log` 多了三个空文件（对比用的那一次再多一个），那个目录原本已有三万多个。
- **真机上没有验证的**：整条产品路径（应用自己的 SSH 会话、状态读取、合成与保存、界面）没有对着真机跑过，真机上只手工执行了 ROS 2 这一条命令；动作（那台机器上没有）、服务和节点清单没有在真机上和 `ros2` 的输出对比（容器里对比过）；节点很多或跨机器的网络上 3 秒的发现时间够不够；不是 Humble 的真机；SSH 账号和运行节点的账号不同的真机。

## 已知限制

- 只认 `/opt/ros` 下的安装。从源码构建在别处的 ROS 2：能从运行中的进程读出发行版，但不读清单（档案里写明）。
- 读取不是完全不留痕迹：`ros2` 工具每次调用留一个空日志文件，见上面“`ros2` 工具自己会留下的东西”。
- SSH 账号和运行节点的账号不同时，读不到节点进程的环境，只能按会话自己的环境或默认设置读；机器人设置了 `ROS_DOMAIN_ID` 或自己的 DDS 配置时清单会不全。这种情况下还可能读到话题却读不到节点（DDS 的共享内存不能跨账号），档案里会提示。
- 环境变量的值如果有多行（例如写成多行的内联 DDS 配置），只取到第一行，`ros2` 会因配置不完整而失败，档案里显示它的报错。
- 清单是读取那一刻的，节点启动或退出后要重新读取；没有定时刷新。
- 不读话题上的数据（传感器读数、底盘状态），不读参数、QoS、发布方与订阅方的对应关系。
- “支持”只表示发行版是 Humble，不表示接口已经对得上；那是 issue #23、#28 的事。
