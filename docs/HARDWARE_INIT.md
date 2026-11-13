# 硬件初始化（issue #2）

真机通过 SSH 接入后，产品先做一次只读硬件扫描，把处理器、GPU、存储、网卡、相机、串口、CAN、USB/PCI 设备及其驱动状态整理成表格，并列出发现的问题。用户可以把选中的问题交给工程助手修复：助手按内置 Skill `hardware-init` 诊断，每一条修改设备的命令都要用户单独批准，修复后重新扫描验证，最后保存修复报告。

## 流程

1. **连接后自动扫描**：SSH 指纹确认、连接成功后，界面立即打开“硬件初始化”并执行扫描（`POST /api/engineering/hardware/scan`，`source=connect`）。之后可在机器人卡片的“硬件初始化”入口查看或重新扫描。
2. **查看结果**：系统信息、指标、问题列表（默认勾选可远程处理的问题）、按类别筛选的硬件清单、修复记录。
3. **修复**：勾选问题 → “让助手修复所选问题” → 选择模型（只列 TokenFans 模型，默认选中“云平台账户”里设置的 TokenFans 默认模型，见 issue #4）→ 两项授权 → 启动修复会话（`POST /api/engineering/hardware/repair`）。
4. **逐条批准**：助手通过 `device_fix` 提出命令；界面显示原命令、对应问题、风险、管理员权限方式、回退方法、产品识别出的附加风险，用户批准一次执行一次。
5. **验证与报告**：助手调用 `hardware_rescan`（重新登录后扫描），再调用 `save_repair_report`。报告保存到 `hardware_repairs`，在硬件初始化页和会话页显示每个问题的处理结果。

## 只读扫描

- 命令是 `brainos/hardware_scan.py` 中的常量 `SCAN_COMMAND`，以 `sh -c` 执行（不依赖账号的登录 shell），模型和用户的文本都不会拼进去；内核日志在设备端先按关键字过滤，只取最近 80 行；只读 sysfs/procfs 与 `lscpu`、`lsblk`、`lspci`、`lsusb`、`ip`、`nvidia-smi`、`dmesg`/`journalctl`、`systemctl --failed` 等，缺少工具时跳过。输出按 `@@NX-HW:<段名>@@` 分段。
- 与 SSH 调查一样经 `device_access.SSHDevice` 连接（包括跳板机），输出会隐藏连接凭据；完整读取输出（上限 256 KB），超时、输出超限或缺少结束标记时记为不完整（`complete=false`，重新扫描的工具结果也带这个字段，不完整时不报告“已消失”的问题）；原始输出与扫描记录分开存放（`hardware_scan_raw`），只通过 `engineering/hardware/detail` 按需读取，每 4 秒刷新的状态快照不会解析它。
- 每台机器人保留最近 20 次扫描（被修复会话或报告引用的扫描不删除）；状态快照只带每台机器人最近 3 次扫描。

### 识别的问题

| 问题 id | 含义 | 远程可处理 |
| --- | --- | --- |
| `pci-driver:<槽位>` | PCI 设备没有绑定驱动（网卡、GPU、存储、USB 控制器等；桥接器等不需要驱动的类别不报） | 是 |
| `nvidia-driver` | 有 NVIDIA GPU 但 `nvidia-smi` 不可用或失败；使用 nouveau 时为警告 | 是 |
| `usb-driver:<端口>` | USB 设备的标准类接口（视频、音频、HID、CDC、存储、无线）没有绑定驱动；厂商自定义接口只标“未确认” | 是 |
| `net-down` / `net-carrier` / `net-noip:<网卡>` | 网卡未启用 / 没有链路信号 / 没有 IP 地址 | 未启用、无地址：是；无信号、CAN 未启用：现场 |
| `net-route` / `net-dns` | 没有默认路由 / 没有 DNS | 是（需用户确认网关、DNS） |
| `serial-perm` / `video-perm:<设备>` | SSH 用户无权读写串口或相机节点 | 是 |
| `rootfs-full` | 根分区使用率 ≥ 90% | 是 |
| `unit:<服务>` | systemd 服务处于失败状态 | 是 |
| `log-*` | 本次启动以来的内核日志：固件缺失、驱动探测失败、USB 枚举失败、过流、欠压、存储 I/O 错误、NVRM | 视类型 |

内核日志是历史：日志提到的设备如果现在有问题，日志并入该问题；如果设备存在且正常，日志被略过（在说明中计数）；只有设备不存在或无法识别时才单列。所以修好设备后重新扫描，不会因为旧日志冒出“新问题”。

问题 id 在多次扫描之间保持稳定，修复是否成功用“重新扫描后该 id 是否消失”判断。

严重程度分三级：错误、警告计入“需要处理的问题”；提示（`info`）只展示、不默认勾选，例如：已有其他网口联网时空着的网口、部分接口已被驱动的 USB 设备（另一接口常被应用程序通过 libusb 占用）、失败的 systemd 服务（可能是机器人自身的应用或启动服务，不让助手直接启动或重启）。Jetson 的 USB 设备模式网口（`usb0`/`usb1`，等电脑接入才有链路）不报问题。

## 修复会话

- 会话 `mode='repair'`，复用工程引擎的调查会话机制（运行时、预算、停止、继续、云端/平台计量）。设备只读工具（`device_overview`、`device_probe`、`device_evidence`、`device_config`、`device_exec`、`device_request_scope`）照常可用，`save_inventory` 不提供；新增 `hardware_rescan`、`device_fix`、`save_repair_report`。
- 内置 Skill 位于 `brainos/skills/hardware-init/`（`SKILL.md` 与 `references/playbooks.md`），随应用版本发布，会话记录其名称、版本和摘要；调查会话仍不能加载项目 Skill 或 MCP，只放行产品内置 Skill（`agent_runtime.AgentProcess` 中的 `builtin` 标记）。
- 工作目录中写入 `HARDWARE_SCAN.json`（基线扫描、待处理问题）与 `DEVICE_CONTEXT.json`。
- 模型：选择的模型；不选时用工作空间默认模型 `Engineering.resolve_model(project_id, None)`（issue #4 TokenFans）。没有连接 TokenFans 或没有默认模型时，直接显示 #4 给出的提示。修复表单的模型选择框默认选中 TokenFans 默认模型。

### `device_fix` 的产品限制

- 用户批准前不执行；被拒绝的命令不会执行。批准绑定在已展示的那一条命令上，执行的就是展示的内容。参数校验失败或命中禁止规则的命令直接退回给模型，并在会话记录“工具反馈”中显示。
- **产品会拒绝常见的危险命令，但不能替代用户的逐条审批**：shell 写法太多，规则不可能穷尽。规则在原命令和去掉引号/反斜杠后的命令上各匹配一次，同样作用于修复会话里的 `device_exec`。
- 拒绝的类别：命令里写 `sudo`/`pkexec`；重启、关机、`telinit`、`systemctl isolate/rescue/emergency`；格式化、改分区、写块设备（重定向、`cp`/`tee`/`dd of=`）；sysrq；递归删除根目录、系统目录或主目录；修改 `/boot`、引导配置、切换启动分区或刷写固件；修改或停止 SSH 服务（`ssh`/`sshd` 两种服务名，`status` 等只读查询除外）、`/etc/ssh`、`authorized_keys`；修改密码、删除/锁定账号或设置过期；修改防火墙（只允许查看）；整体升级或自动清理软件包；下载或解码后直接执行脚本；`eval`、`sh -c`、`python -c` 等内联脚本、`source`、以变量开头拼出命令；发布运动/控制指令。
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
| `engineering/hardware/repair` | `{project_id, robot_id, scan_id, issue_ids, model_id?, max_tokens, max_minutes, data_authorized, modify_authorized}` → 修复会话 |
| `engineering/hardware/skill` | `{project_id}` → 内置 Skill 的文件、版本、摘要 |

状态快照新增 `hardware_scans`、`hardware_repairs`、`hardware_skill`。前端代码在 `web/hardware.js`、`web/hardware.css`，通过包装 `engineApproval`、`engineSessionBody`、`engineRobotCard`、`engineRefresh`、`dialog` 接入已有页面，不改动这些共享函数本身。

## 验证情况

- `tests/test_hardware_scan.py`：解析 Jetson 机器人、x86 + NVIDIA、Debian 容器真实输出等样例，扫描命令只读检查。
- `tests/test_hardware_init.py`：经 SSH 协议替身执行扫描、凭据隐藏、并发与清理、修复命令校验与提权包装、报告规则，经跳板机（issue #3 替身）的扫描与修复命令，以及一次用随包工程运行时 + 本机脚本模型完成的修复会话（批准、拒绝重启、重新扫描、报告）。
- `tests/ssh_jump_browser.cjs`（issue #3 的浏览器验收）：每次 SSH 接入后先经过硬件扫描再进入调查；经跳板机接入时，界面抬头显示跳板机路线，表格来自经跳板机执行的扫描。`tests/hardware_browser_fixture.py` 是本页面的手动检查替身。
- 扫描命令在 Debian 与 Alpine（BusyBox）容器中实际执行过；在一台真机（Jetson Orin NX，JetPack 6 / L4T R36.4.3，经跳板机）上以产品实际发送的 `sh -c` 形式执行过一次只读扫描：7 秒、约 9 KB，结果完整，未出现误报（据此修正了 Jetson GPU 重复、USB 设备模式网口、部分接口占用、失败服务默认勾选等问题，并加入回归测试）。
- 修复会话只在本机替身上运行过（随包工程运行时 + 脚本模型 + SSH 协议替身）；没有在真机上执行过修复命令，也没有使用真实模型（TokenFans）跑过修复会话。

## 尚未包含

- 不做重启、刷机、BIOS/安全启动设置；需要重启才能生效的改动由用户安排后再扫描。
- 没有串口/CAN 总线上的设备级探测（只看到串口设备节点与 CAN 网卡）；ROS 层的传感器话题与底盘状态不在扫描范围内。
- 内核日志需要权限（`dmesg` 受限且账号不在 systemd-journal 组时读不到），这时会在说明中提示。

## 对话修复（issue #6）

调研报告里的“问题与修复”可以开始一个对话修复：它是同一种修复会话（`mode='repair'`），多了 `hardware.chat = true`，基于这台机器人最近一次扫描，可以不勾选问题、只写用户自己的要求。与上面的修复会话的区别：

- 用户的要求不在扫描问题里时，`device_fix` 与修复报告用 issue id `user-request`（`hardware_init.USER_REQUEST`，只在对话修复会话中允许）；其余产品限制和报告规则不变，`fixed` 仍要求成功的修复命令和之后的重新扫描。
- 每轮结束状态为“已完成”，等用户下一条消息（`finish` 不要求每轮保存报告），模型规则多了 `CHAT_INSTRUCTIONS`。
- 接口、界面和测试见 [ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md#工程助手调研三种状态重新调研与对话修复issue-6)。重新调研的硬件扫描记为 `source='investigation'`，硬件初始化页显示为“重新调研时扫描”。
