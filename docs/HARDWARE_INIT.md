# 硬件初始化（issue #2）

真机通过 SSH 接入后，产品先做一次只读硬件扫描，把处理器、GPU、存储、网卡、相机、串口、CAN、USB/PCI 设备及其驱动状态整理成表格，并列出发现的问题。用户可以把选中的问题交给工程助手修复：助手按内置 Skill `hardware-init` 诊断，只做软件层面的修复，修复后重新扫描验证，最后保存修复报告。修改设备的命令默认通过产品检查后直接执行（“默认允许执行”，可以改成逐条批准）；软件修不了的问题只做标注，等人现场处理并标注后才能再交给助手（issue #13，见下文）。

## 流程

1. **连接后自动扫描**：SSH 指纹确认、连接成功后，界面立即打开“硬件初始化”并执行扫描（`POST /api/engineering/hardware/scan`，`source=connect`）。之后可在机器人卡片的“硬件初始化”入口查看或重新扫描。
2. **查看结果**：系统信息、指标、问题列表（默认勾选可远程处理的问题）、按类别筛选的硬件清单、修复记录。
3. **修复**：勾选问题（被标注为等待现场处理的不能勾选，issue #13）→ “让助手修复所选问题” → 选择模型（只列 TokenFans 模型，默认选中“云平台账户”里设置的 TokenFans 默认模型，见 issue #4）→ 两项授权和“默认允许执行”（默认勾选）→ 启动修复会话（`POST /api/engineering/hardware/repair`）。表单不填写 Token 限额和最长运行时间（issue #10，见下文“不限 Token 和运行时间”）。
4. **执行命令**：助手通过 `device_fix` 提出命令。默认允许执行时，命令通过产品检查后直接执行，会话记录里写明执行了什么；取消勾选时逐条批准：界面显示原命令、对应问题、风险、管理员权限方式、回退方法、产品识别出的附加风险，用户批准一次执行一次。
5. **验证与报告**：助手调用 `hardware_rescan`（重新登录后扫描），再调用 `save_repair_report`。报告保存到 `hardware_repairs`，在硬件初始化页和会话页显示每个问题的处理结果。

## 只读扫描

- 命令是 `brainos/hardware_scan.py` 中的常量 `SCAN_COMMAND`，以 `sh -c` 执行（不依赖账号的登录 shell），模型和用户的文本都不会拼进去；内核日志在设备端先按关键字过滤，只取最近 80 行；只读 sysfs/procfs 与 `lscpu`、`lsblk`、`lspci`、`lsusb`、`ip`、`nvidia-smi`、`dmesg`/`journalctl`、`systemctl --failed` 等，缺少工具时跳过。输出按 `@@NX-HW:<段名>@@` 分段。
- 与 SSH 调查一样经 `device_access.SSHDevice` 连接（包括跳板机），输出会隐藏连接凭据；完整读取输出（上限 256 KB），超时、输出超限或缺少结束标记时记为不完整（`complete=false`，重新扫描的工具结果也带这个字段，不完整时不报告“已消失”的问题）；原始输出与扫描记录分开存放（`hardware_scan_raw`），只通过 `engineering/hardware/detail` 按需读取，每 4 秒刷新的状态快照不会解析它。
- 每台机器人保留最近 20 次扫描（被修复会话或报告引用的扫描不删除）；状态快照只带每台机器人最近 3 次扫描。
- 真机状态页（issue #8，见 [ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md#跳板机与真机实时状态)）每分钟用同一条扫描命令和同一个解析器读一次硬件，只显示、不保存：不产生扫描记录，不写审计，也不占用“正在扫描”的标记（`HardwareInit.scanning`），所以不会挡住这里的手动扫描；这台机器人正在扫描，或它的修复会话正在进行时，状态页那一轮不扫描硬件。两处共用 `hardware_init.parse_scan(result)`（超时或输出超限时记为不完整）；`run_command(..., redact=False)` 是给状态页读取软件状态里的数字用的，扫描和修复命令仍然隐藏凭据。
- 设备档案（issue #27，见 [DEVICE_PROFILE.md](DEVICE_PROFILE.md)）的硬件和系统两部分就是状态页的这次读取：同一条扫描命令、同一个解析器，同样不产生扫描记录。

### 识别的问题

| 问题 id | 含义 | 远程可处理 |
| --- | --- | --- |
| `pci-driver:<槽位>` | PCI 设备没有绑定驱动（网卡、GPU、存储、USB 控制器等；桥接器等不需要驱动的类别不报） | 是 |
| `nvidia-driver` | 有 NVIDIA GPU 但 `nvidia-smi` 不可用或失败，或在用 nouveau | 是 |
| `usb-driver:<端口>` | USB 设备的标准类接口（视频、音频、HID、CDC、存储、无线）没有绑定驱动；厂商自定义接口只标“未确认” | 是 |
| `net-down` / `net-carrier` / `net-noip:<网卡>` | 网卡未启用 / 没有链路信号 / 没有 IP 地址 | 未启用、无地址：是；无信号（另有网口联网的空网口除外）、CAN 未启用：现场 |
| `net-route` / `net-dns` | 没有默认路由 / 没有 DNS | 是（需用户确认网关、DNS） |
| `serial-perm` / `video-perm:<设备>` | SSH 登录用的账号无权读写串口或相机节点（只说明这个账号，不说明设备） | 是 |
| `rootfs-full` | 根分区使用率 ≥ 90%（待确认）；≥ 99% 是故障 | 是 |
| `unit:<服务>` | systemd 服务处于失败状态 | 是 |
| `log-*` | 本次启动以来的内核日志：固件缺失、驱动探测失败、USB 枚举失败、过流、欠压、存储 I/O 错误、NVRM | 视类型 |
| `motion-driver:<能力编号>` | 按本体类型应有的运控接口（ROS 2 话题或动作）不在设备档案的 ROS 2 清单里，或消息类型不对。**不是扫描命令读出来的**：来自保存的设备档案，读过档案后的扫描记录才有；这类问题是否消失由重新读取 ROS 2 判定，不由扫描判定（issue #28，见 [MOTION_DRIVER_CHECK.md](MOTION_DRIVER_CHECK.md)） | 是（这一轮助手只做只读排查，结论多为需要现场处理） |

内核日志是历史：日志提到的设备如果现在有问题，日志并入该问题；如果设备存在且正常，日志被略过（在说明中计数）；只有设备不存在或无法识别时才单列。所以修好设备后重新扫描，不会因为旧日志冒出“新问题”。

问题 id 在多次扫描之间保持稳定，修复是否成功用“重新扫描后该 id 是否消失”判断。

### 什么算故障（issue #47）

一台能走的机器人，卡片上曾经写着“运控驱动缺 2 项”和“硬件 24 项 · 10 个问题”，都是红色。那 12 条里没有一条证明了什么东西不能用。现在的规则：

| 级别 | 条件 | 现在有哪些 | 界面 |
| --- | --- | --- | --- |
| 故障（`error`） | 两条都满足：扫描**直接读到**它现在不能用；并且**说得出它阻塞了什么**（问题的 `blocks` 字段，和依据一起显示） | 根分区已用 ≥ 99%（阻塞：往设备上安装管理层、技能和驱动，设备自己写日志） | 红色；卡片上“硬件故障 N 项” |
| 待确认（`warning`） | 读到了它现在不能用或缺着，但这台机器人用不用得到它，扫描看不出来 | 设备没有绑定内核驱动（PCI、USB 标准类接口、NVIDIA）、CAN 接口没有启用、没有默认路由或 DNS、根分区已用 90%–98%、本次启动里的欠压 / 过流 / 存储 I/O 错误 | 对话框里黄色；卡片上是普通标签“硬件 N 项 · M 项待确认”，在「更多功能」里 |
| 提示（`info`） | 只是推断，或者是一种配置 | SSH 账号不在设备的用户组里（机器人自己的程序可能由别的账号或 root 运行）、网口关着 / 没接线 / 没有 IP、开机日志里的固件缺失和驱动探测失败、失败的 systemd 服务、部分接口已被驱动的 USB 设备 | 只展示，不计数，不默认勾选 |

- 没有故障、也没有待确认项时，卡片上是“硬件 N 项 · 未发现故障”。“未发现故障”不等于通过了功能验证。
- **没有 `blocks` 就成不了故障**：`hardware_scan._Builder.issue` 遇到没写阻塞了什么的 `error` 会降成 `warning`。以后要新增一种故障，先回答“读到了什么”和“它挡住了什么”。
- 设备行的状态跟着走：只有提示的设备行是“正常”（相机、串口本身在工作）或“未确认”（关着的网口），不再是“需处理”。
- **“需要现场处理”不跟着级别走。** 每个问题多一个 `onsite` 字段：软件处理不了、要人到机器人旁边（网线没接、USB 设备枚举失败、欠压、过流、存储 I/O 错误、CAN 接口没有启用）。issue #13 把这些挡在助手之外，靠的是这个字段（`repair_policy.needs_onsite`），不再靠“不是提示”。所以网线没接降成了提示，仍然是“等待现场处理”、不能直接勾给助手。旧记录没有这个字段时按原来的算法（不可远程处理且当时不是提示）。
- **以前保存的扫描**在工作区打开时换成按现在的规则定级的记录（`HardwareInit._regrade_saved` → `hardware_scan.regraded`，只做一次）：每类问题不超过它能证明的级别（`GRADE_CAPS`），计数和设备行状态一起重算，记录带 `regraded: true` 和 `rules: 2`，页面上写明“说明文字还是当时的，重新扫描后更新”。页面、修复流程和交给助手的 `HARDWARE_SCAN.json` 读的是同一份，不会一边说可以勾选、一边拒绝。旧记录里出不了故障，因为“阻塞了什么”当时没有读；当时的级别不保留。新扫描带 `rules: 2`。
- 修复流程里变了一点：提示不默认勾选，所以“SSH 账号不在用户组里”这类以前默认勾给助手的，现在要人自己勾。待确认的仍默认勾选；需要现场处理的照旧不能勾。
- 实时状态和设备档案的硬件页（issue #8、#27）：设备行的“需处理”现在只剩读到了不能用的（没有驱动、CAN 关着），“需要处理”那一格没有故障时是黄色，不是红色。

Jetson 的 USB 设备模式网口（`usb0`/`usb1`，等电脑接入才有链路）不报问题。

## 修复会话

- 会话 `mode='repair'`，复用工程引擎的调查会话机制（运行时、预算、停止、继续、云端/平台计量）。设备只读工具（`device_overview`、`device_probe`、`device_evidence`、`device_config`、`device_exec`、`device_request_scope`）照常可用，`save_inventory` 不提供；新增 `hardware_rescan`、`device_fix`、`save_repair_report`。
- 内置 Skill 位于 `brainos/skills/hardware-init/`（`SKILL.md` 与 `references/playbooks.md`），随应用版本发布，会话记录其名称、版本和摘要；调查会话仍不能加载项目 Skill 或 MCP，只放行产品内置 Skill（`agent_runtime.AgentProcess` 中的 `builtin` 标记）。外部资料只有一条路（issue #30）：资料库里这台设备「可用」的资料，只取人确认过的解析结果，作为参考材料交给修复助手；没有可用的驱动资料时，会话提示先上传。会话仍然不联网、不能网页搜索。见 [REPAIR_MATERIALS.md](REPAIR_MATERIALS.md)。
- 工作目录中写入 `HARDWARE_SCAN.json`（基线扫描、待处理问题）、`DEVICE_CONTEXT.json` 与 `LIBRARY_MATERIALS.json`（资料库里这台设备可用的资料，issue #30）。
- 模型：选择的模型；不选时用工作空间默认模型 `Engineering.resolve_model(project_id, None)`（issue #4 TokenFans）。没有连接 TokenFans 或没有默认模型时，直接显示 #4 给出的提示。修复表单的模型选择框默认选中 TokenFans 默认模型。
- **不限 Token 和运行时间（issue #10）**：“让助手修复硬件问题”表单不再有“Token 观察限额”“最长运行时间”两项，请求里也不带 `max_tokens`、`max_minutes`；会话的这两个字段为空，运行不会因用量或时间被停止，由用户在会话中停止；是否逐条批准由“默认允许执行”决定（issue #13，见下）。规则与调查（issue #5）相同：接口调用方（测试、脚本）仍可传入限额（1000–1000000 Tokens、1–120 分钟），照旧校验，达到后照旧停止；按会话预留点数的网关模型（云平台 `cloud`、平台付费 `platform`，界面已不提供）没有预算无法开启会话，未传限额时仍按修复一直使用的 200000 Tokens、30 分钟。规则在 `brainos/hardware_init.py` 的 `repair_limits`（复用 `brainos/engineering.py` 的 `discovery_limits`、`run_deadline`、`over_token_limit`），测试为 `tests/test_repair_limits.py`。

### `device_fix` 的产品限制

- 逐条批准的会话里，用户批准前不执行；被拒绝的命令不会执行。批准绑定在已展示的那一条命令上，执行的就是展示的内容。默认允许执行的会话见下文 issue #13：产品限制照旧，只是不再等人批准。参数校验失败或命中禁止规则的命令直接退回给模型，并在会话记录“工具反馈”中显示。
- **产品会拒绝常见的危险命令，但不能替代用户的逐条审批**：shell 写法太多，规则不可能穷尽。规则在原命令和去掉引号/反斜杠后的命令上各匹配一次，同样作用于修复会话里的 `device_exec`。
- 拒绝的类别：命令里写 `sudo`/`pkexec`；重启、关机、`telinit`、`systemctl isolate/rescue/emergency`；格式化、改分区、写块设备（重定向、`cp`/`tee`/`dd of=`）；sysrq；递归删除根目录、系统目录或主目录；修改 `/boot`、引导配置、切换启动分区或刷写固件；修改或停止 SSH 服务（`ssh`/`sshd` 两种服务名，`status` 等只读查询除外）、`/etc/ssh`、`authorized_keys`；修改密码、删除/锁定账号或设置过期；修改防火墙（只允许查看）；整体升级或自动清理软件包；下载或解码后直接执行脚本；`eval`、`sh -c`、`python -c` 等内联脚本、`source`、以变量开头拼出命令；发布运动/控制指令。
- issue #29（[运控驱动的安装](REPAIR_DRIVER_INSTALL.md)）：命令里出现 `cmd_vel` 不再一律拒绝，只拒绝发布、调用服务、发送动作目标、改参数或生命周期等控制动作，`ros2` 只能做只读查询或在标明 `starts_motion_driver` 的命令里启动节点；碰设备上 NeuxMind 运行时（急停锁存）的命令一律拒绝；ROS 2 Humble 的环境由产品加（`ros2_env`、`ros2_workspace`）；`build_step` 的编译或安装命令最长 3600 秒；启动运控驱动的命令要有人在现场确认，默认允许执行时也停下等人。
- 网络只允许“启用”方向的改动：关闭网卡（CAN 接口除外，改波特率需要先关闭）、删除地址或路由、`nmcli … down/disconnect/delete`、`rfkill block` 一律拒绝；不依赖能否识别出 SSH 所用网卡。
- 命令只能是可见的普通文本：拒绝控制字符、零宽字符和双向排版字符，最多 10 行。审批框完整展开命令，并显示行数和字符数。
- 附加风险提示：重启网络服务（SSH 可能短暂中断）、安装或移除软件包（需要软件源）、用户组变更（新登录生效）、关闭网络接口或连接、包含命令替换/eval/编码内容。
- 管理员权限：`needs_root=true` 时由连接层提权。以 root 登录直接执行；密码认证用 `sudo -S` 并把已保存的 SSH 密码写入标准输入；私钥/Agent 认证用 `sudo -n`（需要免密 sudo）。命令本身在 `exec </dev/null` 之后运行，读不到密码；设备上有 `timeout` 时以 `timeout -k 5 <秒>` 运行，超时的命令在设备上也会被终止；输出中的连接凭据被隐藏；密码不进入模型上下文、事件、证据或界面。
- 执行结果（保留最后 48 KB 输出，不因输出量中途截断安装过程）保存为 `device_evidence`（`tool='device_fix'`），报告可以引用。
- 修复会话里批准执行的 `device_exec` 也按一次改动计数（审批框写明它可能修改设备）：之后必须重新扫描，才能把问题标为已修复。
- 网络改动导致会话的 SSH 断开时，下一条命令先重新登录；重新登录失败会明确提示，让助手把剩余问题记为需要现场处理。

### 报告规则

- 每个待处理问题必须有一条结果：`fixed`、`pending_reboot`、`not_reproduced`、`not_fixed`、`needs_user`、`skipped`。
- `fixed` 要求：执行过针对该问题、且退出码为 0 的 `device_fix`；在最后一次修复（无论成败）之后调用过 `hardware_rescan`；该次扫描完整且问题已消失。问题消失但没有成功执行修复只能是 `not_reproduced`（同样要求扫描完整）。
- 会话第一条指令只列问题 id；问题标题与详情来自设备，只在 `HARDWARE_SCAN.json` 中作为数据提供。
- 证据只能引用本会话的证据。一轮结束而没有保存报告，或报告之后又执行了修复或重新扫描（报告已过时），会话状态为“待继续”。
- 扫描清理时保留会话与报告引用的基线扫描、最近一次重新扫描和最终扫描。

## 接口

| 路径 | 说明 |
| --- | --- |
| `engineering/hardware/scan` | `{project_id, robot_id, source: connect|manual}` → 扫描记录（不含原始输出） |
| `engineering/hardware/detail` | `{project_id, scan_id}` → 含原始输出的完整记录 |
| `engineering/hardware/repair` | `{project_id, robot_id, scan_id, issue_ids, model_id?, max_tokens?, max_minutes?, data_authorized, modify_authorized, auto_approve?}` → 修复会话；不传或传 `null` 的限额即不设上限（issue #10）；`auto_approve` 为 `true` 时默认允许执行，被标注的问题不能选（issue #13） |
| `engineering/hardware/mark` | `{project_id, robot_id, issue_id, handled}` → 人工标注一个被标注的问题已现场处理，或撤销（issue #13） |
| `engineering/hardware/auto_approve` | `{project_id, session_id, enabled}` → 修复会话在默认允许执行和逐条批准之间切换（issue #13） |
| `engineering/hardware/skill` | `{project_id}` → 内置 Skill 的文件、版本、摘要 |

状态快照新增 `hardware_scans`、`hardware_repairs`、`hardware_skill`，以及 issue #13 的 `hardware_blocks`。前端代码在 `web/hardware.js`、`web/hardware.css`，通过包装 `engineApproval`、`engineSessionBody`、`engineRobotCard`、`engineRefresh`、`dialog` 接入已有页面，不改动这些共享函数本身。

## 验证情况

- `tests/test_hardware_scan.py`：解析 Jetson 机器人、x86 + NVIDIA、Debian 容器真实输出等样例，扫描命令只读检查。
- `tests/test_hardware_init.py`：经 SSH 协议替身执行扫描、凭据隐藏、并发与清理、修复命令校验与提权包装、报告规则，经跳板机（issue #3 替身）的扫描与修复命令，以及一次用随包工程运行时 + 本机脚本模型完成的修复会话（批准、拒绝重启、重新扫描、报告）。
- `tests/ssh_jump_browser.cjs`（issue #3 的浏览器验收）：每次 SSH 接入后先经过硬件扫描再进入调查；经跳板机接入时，界面抬头显示跳板机路线，表格来自经跳板机执行的扫描。`tests/hardware_browser_fixture.py` 是本页面的手动检查替身。
- issue #10：`tests/test_repair_limits.py`（限额规则；表单发起的修复不带限额；一次报告用量 2100 万 Tokens 的修复照常批准、重新扫描并保存报告；显式限额仍会停止）与 `tests/hardware_repair_browser.cjs`（浏览器验收，替身为 `tests/hardware_browser_fixture.py`，每次运行前重启替身）：修复表单没有限额字段、请求不带限额、会话无限额、修复命令仍需批准、修复完成并保存报告；在 Chromium 与 WebKit 上运行。只在本机替身上验证，没有在真机或真实模型上运行。
- 扫描命令在 Debian 与 Alpine（BusyBox）容器中实际执行过；在一台真机（Jetson Orin NX，JetPack 6 / L4T R36.4.3，经跳板机）上以产品实际发送的 `sh -c` 形式执行过一次只读扫描：7 秒、约 9 KB，结果完整，未出现误报（据此修正了 Jetson GPU 重复、USB 设备模式网口、部分接口占用、失败服务默认勾选等问题，并加入回归测试）。
- 修复会话只在本机替身上运行过（随包工程运行时 + 脚本模型 + SSH 协议替身）；没有在真机上执行过修复命令，也没有使用真实模型（TokenFans）跑过修复会话。

## 尚未包含

- 不做重启、刷机、BIOS/安全启动设置；需要重启才能生效的改动由用户安排后再扫描。
- 没有串口/CAN 总线上的设备级探测（只看到串口设备节点与 CAN 网卡）。
- ROS 2 不在这条扫描命令的范围内：`SCAN_COMMAND` 不加载 ROS 环境、不运行 `ros2`（`tests/test_hardware_scan.py` 断言如此，issue #27 没有改它）。ROS 2 的发行版、正在运行的节点、话题与消息类型、动作、服务由**设备档案**用另一条固定的只读命令读取，并和这里的硬件、系统信息合成一份记录，见 [DEVICE_PROFILE.md](DEVICE_PROFILE.md)。话题上的数据（传感器读数、底盘状态）仍然不读。
- 运控驱动检查（issue #28，见 [MOTION_DRIVER_CHECK.md](MOTION_DRIVER_CHECK.md)）也没有改扫描命令。保存的设备档案显示缺运控接口时，扫描（手动、连接后、修复会话的 `hardware_rescan`）在扫描命令之后再执行一次设备档案的那条 ROS 2 只读命令，用它判定 `motion-driver:*` 问题是否还在；没有这类问题的机器人，到达设备的仍然只有扫描命令。内置 Skill 为此升到 1.2.0（`references/playbooks.md` 多了一节）。
- 内核日志需要权限（`dmesg` 受限且账号不在 systemd-journal 组时读不到），这时会在说明中提示。

## 默认允许执行与等待现场处理的问题（issue #13）

两件事，都是为了让硬件问题能被正常处理：助手修软件能修的，不用人守着逐条批准；软件修不了的只做标注，等人现场处理。规则在 `brainos/repair_policy.py`，界面在 `web/repair_policy.js`、`web/repair_policy.css`（问题列表、表单和审批仍在 `web/hardware.js`、`web/investigation.js` 里画，调用前者的函数）。

### 默认允许执行

- “让助手修复硬件问题”和对话修复的开始表单多了一个勾选框“默认允许执行”，**默认勾选**。勾选时请求带 `auto_approve: true`，记在会话的 `hardware.auto_approve`。
- 开启后，助手提出的设备命令（`device_fix` 和修复会话里的 `device_exec`）在排进审批队列的那一刻就被接受，不再等人。命令在这之前已经过产品的硬性规则（上面“`device_fix` 的产品限制”），规则没有变：重启、刷写固件、格式化、修改 SSH / 账号 / 防火墙、发布运动指令等照旧被拒绝，不会执行。
- **没有人逐条看命令时，产品的规则就是仅有的检查，而它们覆盖不了所有写法。** 所以下面三种情况即使开启了也停下来等人批准，审批卡片上写明原因（会话的 `hardware.auto_hold`）：
  1. 产品从文字上判断不了的命令（`repair_policy.ASK_FIRST`）：命令替换（`$(…)`、反引号）、编码内容、续行、自定义变量；`curl`、`wget`、`scp`、`nc` 等下载或网络传输；运行脚本或程序文件、把内容交给 `sh` / `python` 执行；递归删除或批量改权限（`rm -r`、`chmod -R`、`find … -delete`）；定时任务、开机脚本与 `sudoers`；密钥或凭据所在的位置（`.ssh`、`.netrc`、`/etc/shadow`、`*.pem` 等）。常见的修复命令（`modprobe`、`apt-get install`、`usermod`、`nmcli … up`、`systemctl restart`、写 udev 规则等）不在其中。这张表和硬性规则一样不可能穷尽。
  2. 连续直接执行了 60 条设备命令（`AUTO_RUN_LIMIT`）之后的下一条：防止会话在没人看的情况下一直执行下去。用户批准并保持勾选“之后默认允许执行”，计数重新开始。
  3. 不是修复命令的请求：申请读取新目录（`device_request_scope`，它扩大的是读取范围）、助手在用户这台电脑上的命令或文件修改申请额外权限（`item/commandExecution/requestApproval` 等），以及助手向用户提出的问题。设备上的内容是不可信数据，不让它借这个开关在用户电脑上执行东西。
- 没有人看过的命令不记成“用户批准”：会话记录里是一条“默认允许执行，未询问：……”，带完整命令（超过 1500 字符时截断并注明看依据），之后照旧是 `fix` 事件和证据。
- 用户回答了排在前面的请求（上面第 3 类或一个问题）之后，排在它后面的设备命令接着按同样的规则处理，不会卡住。
- 取消勾选（`auto_approve: false`）就和以前一样逐条批准。每张设备命令的审批卡片上有“之后默认允许执行”：会话已经开启时默认勾选，没有开启时默认不勾。批准时勾着，这次会话后面的设备命令就不再逐条询问；在已开启的会话里取消勾选再批准，会话改回逐条批准。会话页和对话修复页有一行当前方式和切换按钮（`POST /api/engineering/hardware/auto_approve`，`{project_id, session_id, enabled}`），随时可以切换；切到开启时，队列里已经在等的设备命令按上面的规则处理。
- 接口调用方不传 `auto_approve` 时不开启（值为 `null`），所以原有的测试和脚本照旧逐条批准。
- 模型收到的规则多一段（`repair_policy.AUTO_ALLOW`）：用户可能不在场，命令要更保守，没有把握的不执行、记为 `needs_user`，不要调用提问工具等用户，并列出上面第 1 类命令让它尽量不用。这段规则在一轮开始时给出：会话中途切换到开启，要到下一轮（继续会话或下一条消息）模型才收到。
- 顺带修了硬性规则的一个漏洞：命令里用反斜杠加换行把关键字拆开（`re\` 换行 `boot`）以前能绕过规则，现在按接起来的文字匹配。

### 软件修不了的问题：只标注

- 所有修复会话的模型规则多一段（`repair_policy.SOFTWARE_ONLY`），内置 Skill `hardware-init` 升到 1.1.0 并写了同样的要求：只做软件层面的修复；判断为软件修不了的（硬件损坏、线缆或接口松动、供电不足、设备没接上、需要现场操作）不反复尝试，在报告里标 `needs_user`，需要重启或重新插拔的标 `pending_reboot`，试过没解决的标 `not_fixed`，写明依据和现场要做什么。
- 一个问题在两种情况下**被标注、不能勾选**（硬件初始化页和调研报告的“问题与修复”都是）：
  1. 扫描本身判定需要现场处理：`fixable` 不是 `auto` 且不是提示（`info`），也就是界面上一直写着“需要现场处理”的那些（网线没有信号、USB 过流、欠压、存储 I/O 错误、CAN 接口未启用等）。
  2. 修复报告里它的结果是 `needs_user`、`not_fixed` 或 `pending_reboot`，标注里带助手写的处理过程。
- 被标注的问题下面有一条说明和一个按钮“标注为已现场处理”（等待重启的是“标注为已重启”）。人工标注后它可以重新勾选（不会替用户勾上），说明变成“已标注处理完成”，可以“撤销标注”。接口 `POST /api/engineering/hardware/mark`，`{project_id, robot_id, issue_id, handled}`；记录在 `hardware_issue_marks`，写审计 `device.hardware_issue_marked`。
- 标注过“已处理”的问题，助手再修一次仍然修不了，会重新被标注。问题在一次完整扫描里消失，它的标注记录就删掉：以后再出现时按新问题算（扫描判定需要现场处理的仍然先被标注）。不完整的扫描不删。
- 服务端同样拒绝：`engineering/hardware/repair` 和 `engineering/investigation/fix` 的 `issue_ids` 里有被标注的问题时报错，不开始会话。所选问题全部被标注时，“让助手修复所选问题”按钮不可点。会话进行中也一样：`device_fix` 的 `issue_ids` 里有被标注的问题时，这条命令直接退回给模型，不执行也不进审批。
- 报告把一个问题记为修不了，但最近一次完整扫描里它已经不在了：不标注（问题已经消失）。
- 状态快照新增 `hardware_blocks`：每台机器人最近一次扫描里被标注（`blocked`）或已人工标注（`handled`）的问题，带原因（`onsite` / `needs_user` / `not_fixed` / `pending_reboot`）和助手的说明。
- 提示级别的问题（“由你判断”，例如失败的 systemd 服务）不受影响，仍然由用户决定是否勾选。

### 验证

- `tests/test_repair_policy.py` 41 项：扫描判定的标注、人工标注与撤销及其校验、报告结果产生的标注、再次修不了重新标注、问题消失后清除（包括报告说修不了但重新扫描里已经没有的）、被标注的问题不能开始修复或对话修复、修复中也不能对它执行 `device_fix`、续行不能绕过硬性规则、`auto_approve` 的保存与校验、模型规则与 Skill、排队即接受、常见修复命令直接执行、产品判断不了的命令和读取新目录等人批准并只说一次原因、连续执行达到上限后等人、本机命令不自动接受、排在用户请求后面的设备命令在用户回答后继续、中途切换，以及经工程运行时替身跑通的两次完整修复（默认允许执行时无人批准即完成并拒绝重启；逐条批准的会话中途切换后自己跑完）。
- `tests/repair_policy_browser.cjs`（替身 `tests/hardware_browser_fixture.py`）：需要现场处理的问题不可勾选且服务端拒绝、标注与撤销并保留其他勾选、助手说明的显示、表单默认勾选“默认允许执行”、请求带上它、修复无人批准完成、各种会话里审批卡片的样子（被留下的命令写明原因、没有开启的会话不预先勾选、读取新目录不提供这个选项）、会话页切回逐条批准、修复后回到硬件页；900 px 与 700 px 不横向溢出。在 Chromium 与 WebKit 上通过。
- `tests/hardware_repair_browser.cjs`（issue #10）和 `tests/investigation_browser.cjs`（issue #6）验的是逐条批准，所以在表单里取消了“默认允许执行”；各加了一段 #13 的检查：审批表单和对话里的审批卡片上勾选“之后默认允许执行”后批准，会话切到默认允许执行；调研报告的“问题与修复”里标注、撤销标注，已勾选的问题和已输入的文字保留。两个脚本都在 Chromium 与 WebKit 上通过。
- 只在本机替身上验证（SSH 协议替身、脚本模型、工程运行时或其替身）。没有在真机上、也没有用真实模型跑过：真实模型是否按“软件修不了就标注”的规则给出结果、会不会经常写出要等人批准的命令，需要用真实 Key 在真机上验收。
- 没有做的：`device_access.redact` 识别的凭据写法没有扩充（例如 `nmcli … password xxx` 这种不带等号的写法不会被隐藏），这是所有设备输出共用的规则，改动面大，留作单独的事。

## 对话修复（issue #6）

调研报告里的“问题与修复”可以开始一个对话修复：它是同一种修复会话（`mode='repair'`），多了 `hardware.chat = true`，基于这台机器人最近一次扫描，可以不勾选问题、只写用户自己的要求。与上面的修复会话的区别：

- 用户的要求不在扫描问题里时，`device_fix` 与修复报告用 issue id `user-request`（`hardware_init.USER_REQUEST`，只在对话修复会话中允许）；其余产品限制和报告规则不变，`fixed` 仍要求成功的修复命令和之后的重新扫描。
- 每轮结束状态为“已完成”，等用户下一条消息（`finish` 不要求每轮保存报告），模型规则多了 `CHAT_INSTRUCTIONS`。
- 开始表单同样有“默认允许执行”（默认勾选），被标注为等待现场处理的问题同样不能勾选（issue #13，见上一节）。
- 接口、界面和测试见 [ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md#工程助手调研三种状态重新调研与对话修复issue-6)。重新调研的硬件扫描记为 `source='investigation'`，硬件初始化页显示为“重新调研时扫描”。
