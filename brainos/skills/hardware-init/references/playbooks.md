# 常见硬件问题的检查与修复

问题 id 的前缀决定问题类型。以下命令是示例，执行前先用只读命令确认设备、发行版和实际路径。

## pci-driver:<槽位> — PCI 设备未加载驱动

- 检查：扫描结果里的“可用内核模块”；`lspci -nnk -s <槽位>`、`modinfo <模块>`、`lsmod | grep <模块>`、`dmesg | grep -i <模块>`。
- 修复：模块存在但未加载 → `modprobe <模块>`（needs_root，回退 `modprobe -r <模块>`）。需要开机自动加载时写入 `/etc/modules-load.d/<模块>.conf`（回退：删除该文件）。
- 模块不存在：内核缺少驱动，通常需要安装匹配内核版本的驱动包（如 `linux-modules-extra-$(uname -r)`）；找不到来源时记为 `needs_user`。

## usb-driver:<端口> — USB 设备标准接口未绑定驱动

- 检查：`lsusb -v -s <总线>:<设备号>` 的接口类别；视频类（0e）需要 `uvcvideo`，CDC 串口需要 `cdc_acm`，CH34x/CP210x/FTDI 转串口需要 `ch341`/`cp210x`/`ftdi_sio`。
- 修复：`modprobe <模块>`；模块存在但设备 ID 不在驱动表中时，可通过 `/sys/bus/usb-serial/drivers/<驱动>/new_id` 临时绑定（回退：重新插拔）。
- 厂商自定义接口（类别 ff）通常由用户态 SDK（libusb）驱动，没有内核驱动是正常的，不要强行绑定。

## nvidia-driver — NVIDIA GPU 驱动未就绪

- 先区分平台：`scan.system.jetson` 非空表示 Jetson（L4T）。**Jetson 上绝不能安装桌面版 NVIDIA 驱动**；GPU 驱动随 JetPack 提供，缺失时检查 `nvidia-l4t-*` 包，无法确定时记为 `needs_user`。
- x86 桌面 GPU：`ubuntu-drivers devices`（Ubuntu）查看推荐驱动；安装 `nvidia-driver-<版本>` 后需要重启 → `pending_reboot`。正在使用 nouveau 时，安装官方驱动会自动禁用 nouveau，也需要重启。
- 安全启动开启（`mokutil --sb-state`）时第三方模块可能无法加载，需要用户在现场登记密钥 → `needs_user`。

## log-firmware:<文件> — 固件缺失

- 检查：`ls -l /lib/firmware/<文件>`；用 `apt-file search <文件>` 或发行版文档确认提供该文件的包（常见为 `linux-firmware`、`firmware-realtek`、`firmware-iwlwifi`）。
- 修复：安装对应固件包，然后重新加载驱动（`modprobe -r <模块> && modprobe <模块>`，注意不要卸载承载 SSH 的网卡驱动）或记为 `pending_reboot`。内核日志里的旧记录在重启前不会消失。

## log-probe:<设备> — 驱动探测失败

- 检查：`dmesg | grep -i <设备>` 的上下文，常见原因是缺固件、设备树配置或供电。找到缺失固件按上一条处理；设备树或硬件问题记为 `needs_user`。

## log-usb-enum / log-usb-overcurrent / log-undervoltage / log-io-error

- 线缆、接口接触、供电或存储硬件问题，不能远程修复。记录日志证据，记为 `needs_user`，在 followups 写清现场检查项。

## net-down:<网卡> — 网卡未启用

- 先确认它不是 `ssh_interface`。检查是否由 NetworkManager 管理：`nmcli -t -f DEVICE,STATE,CONNECTION device`。
- 修复：有对应连接 → `nmcli connection up <连接名>`；未被管理 → `ip link set <网卡> up`（回退：`ip link set <网卡> down`）。
- CAN 接口（`can` 类别）启用需要知道总线波特率，扫描里没有这个信息：向用户确认后再用 `ip link set <网卡> up type can bitrate <波特率>`，否则记为 `needs_user`。

## net-carrier:<网卡> — 没有链路信号

- 网线未连接或对端无电，需要现场处理 → `needs_user`。

## net-noip:<网卡> — 没有 IP 地址

- 检查：`nmcli device show <网卡>`、`journalctl -u NetworkManager -n 80 --no-pager`；无线网卡用 `nmcli device wifi list` 看是否关联。
- 修复：已有连接配置 → `nmcli connection up <连接名>`；无线网络需要 SSID 和密码时向用户确认，不要猜测 → 否则 `needs_user`。

## net-route / net-dns — 默认路由或 DNS 缺失

- 检查上行网卡的连接配置（`nmcli connection show <连接名>` 中的 ipv4.gateway / ipv4.dns）。
- 修复需要知道正确的网关与 DNS，向用户确认后再改，改动标为 high 风险；设备没有外网时，后续安装包类修复都要记为 `needs_user`。

## serial-perm:<设备> / video-perm:<设备> — 当前用户无权访问

- 修复：把 SSH 用户加入设备所属组：`usermod -aG <组> <用户>`（回退：`gpasswd -d <用户> <组>`）。组变更对新登录生效，`hardware_rescan` 会重新登录验证；已在运行的机器人程序需要重启。
- 需要固定设备名或权限时，可以写 udev 规则到 `/etc/udev/rules.d/99-neuxmind-<名称>.rules`，再 `udevadm control --reload-rules && udevadm trigger`（回退：删除规则文件并重新加载）。

## unit:<服务> — 系统服务启动失败

- 检查：`systemctl status <服务> --no-pager`、`journalctl -u <服务> -b -n 120 --no-pager`。
- 只处理设备守护进程类的服务（相机守护进程如 `nvargus-daemon`、网络、蓝牙、GPU）。根因是缺设备或驱动时先修复设备，再 `systemctl restart <服务>`（回退：`systemctl stop <服务>`）。
- **机器人自身的应用、启动或导航服务（可能带起底盘、电机或运动程序）一律不要启动或重启**，记为 `needs_user` 并说明失败原因；与硬件无关的服务记为 `skipped`。
- 不要 `systemctl disable` 或 `mask` 服务来“消除”失败。

## motion-driver:<能力编号> — 缺少运控接口（运控驱动没有安装，或没有在运行）

- 这类问题不是硬件扫描发现的：产品按本体类型列出运控驱动应提供的 ROS 2 话题或动作，和只读读取到的 ROS 2 清单对照。`selected_issues` 里这一条的 `expected` 写着应有的接口：`kind`（`topic` 话题 / `action` 动作）、`name`（惯用名，设备上可以不同）、`types`（消息类型，必须对得上）。
- 检查（只读）：`ls /opt/ros` 看装了哪个 ROS 发行版；`dpkg -l` 的输出里找 `ros-humble-` 开头的包和厂商的驱动包；`systemctl list-units --type=service --all --no-pager` 找本体的驱动或启动服务，再用 `systemctl status <服务> --no-pager`、`journalctl -u <服务> -n 80 --no-pager` 看它是没有安装、没有启动还是启动失败。
- 不需要自己运行 `ros2` 命令：会话里加载不了 ROS 环境（`source` 会被产品拒绝）。要知道接口现在在不在，调用 `hardware_rescan`：它会重新读取 ROS 2 的清单，结果里的 `motion_drivers.reverified` 为 true 表示这次读到了，`resolved_since_baseline` 里有这个问题才算接口已经出现。
- **不要自己安装、启动或重启运控驱动**：它会带起底盘或电机（见“必须遵守”）。也不要用发布话题、调用服务、发送动作目标的办法去试接口。
- 结果：把查到的情况写进 `actions`（ROS 2 是否安装、驱动包是否安装、服务是否存在、为什么没有运行），标为 `needs_user`；`followups` 写明需要用户做什么（提供厂商的驱动安装包、在现场安装并启动驱动）。用户处理后接口出现在清单里，问题才会消失。

## rootfs-full — 根分区空间不足

- 检查：`du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -n 20`、`journalctl --disk-usage`。
- 修复：只清理可再生成的内容，例如 `journalctl --vacuum-size=200M`、`apt-get clean`。不要删除用户数据、模型或日志目录中的业务文件 → 需要时记为 `needs_user`。
