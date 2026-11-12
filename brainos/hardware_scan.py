"""Hardware initialisation scan (issue #2): one fixed, read-only command and its parser.

The command is a product-owned constant: no model or user text is ever added to it.
It reads sysfs/procfs and the standard listing tools, and changes nothing on the device.
``parse`` turns its output into table rows (``devices``) and detected problems
(``issues``). Issue ids are stable across scans, so a repair can be verified by
scanning again and checking that the id is gone.
"""
import re
import shlex

SCAN_TIMEOUT_SECONDS = 40
MARK = re.compile(r'^@@NX-HW:([a-z_]+)@@$')

# POSIX sh; every probe tolerates a missing tool or file. ``r`` reads one sysfs
# attribute on a single line with the column separator removed.
SCAN_COMMAND = r'''export LC_ALL=C
r(){ cat "$1" 2>/dev/null | tr -d '\n|'; }
lnk(){ [ -L "$1" ] && basename "$(readlink "$1")"; }
T=; command -v timeout >/dev/null 2>&1 && T='timeout 8'
echo '@@NX-HW:system@@'
echo "hostname=$(hostname 2>/dev/null)"
echo "kernel=$(uname -r 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
echo "os=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"')"
echo "model=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0\n|')"
echo "vendor=$(r /sys/class/dmi/id/sys_vendor)"
echo "product=$(r /sys/class/dmi/id/product_name)"
echo "tegra=$(head -n 1 /etc/nv_tegra_release 2>/dev/null)"
echo "user=$(id -un 2>/dev/null)"
echo "uid=$(id -u 2>/dev/null)"
echo "groups=$(id -nG 2>/dev/null)"
echo "ssh=$SSH_CONNECTION"
echo '@@NX-HW:cpu@@'
lscpu 2>/dev/null | grep -E '^(Model name|Architecture|CPU\(s\)|Vendor ID):'
grep -m 1 -E '^(model name|Hardware|Processor|cpu model)' /proc/cpuinfo 2>/dev/null
echo "nproc=$(nproc 2>/dev/null)"
echo '@@NX-HW:memory@@'
grep -E '^(MemTotal|MemAvailable|SwapTotal):' /proc/meminfo 2>/dev/null
echo '@@NX-HW:disks@@'
lsblk -d -n -P -o NAME,TYPE,SIZE,MODEL,TRAN 2>/dev/null
echo '@@NX-HW:disk_paths@@'
for b in /sys/block/*; do [ -e "$b/device" ] && echo "${b##*/}|$(readlink -f "$b/device" 2>/dev/null)"; done
echo '@@NX-HW:rootfs@@'
df -P / 2>/dev/null | tail -n 1
echo '@@NX-HW:gpu@@'
if command -v nvidia-smi >/dev/null 2>&1; then out=$($T nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1); echo "exit=$?"; printf '%s\n' "$out" | head -n 8; else echo 'exit=missing'; fi
echo '@@NX-HW:pci@@'
for d in /sys/bus/pci/devices/*; do [ -e "$d" ] || continue; echo "${d##*/}|$(r "$d/class")|$(r "$d/vendor")|$(r "$d/device")|$(lnk "$d/driver")"; done
echo '@@NX-HW:pci_names@@'
command -v lspci >/dev/null 2>&1 && lspci -Dk 2>/dev/null | head -n 400
echo '@@NX-HW:usb@@'
for d in /sys/bus/usb/devices/*; do [ -f "$d/idVendor" ] || continue; n=${d##*/}; ifs=; for i in "$d/$n":*; do [ -d "$i" ] || continue; x=$(lnk "$i/driver"); ifs="$ifs $(r "$i/bInterfaceClass"):${x:--}"; done; echo "$n|$(r "$d/idVendor")|$(r "$d/idProduct")|$(r "$d/bDeviceClass")|$(r "$d/speed")|$(r "$d/manufacturer")|$(r "$d/product")|$ifs"; done
echo '@@NX-HW:usb_names@@'
command -v lsusb >/dev/null 2>&1 && lsusb 2>/dev/null | head -n 120
echo '@@NX-HW:net@@'
for d in /sys/class/net/*; do [ -d "$d" ] || continue; n=${d##*/}; [ "$n" = lo ] && continue; w=0; { [ -d "$d/wireless" ] || [ -L "$d/phy80211" ]; } && w=1; echo "$n|$(r "$d/operstate")|$(r "$d/carrier")|$(r "$d/flags")|$(r "$d/type")|$(r "$d/address")|$(lnk "$d/device/driver")|$w|$(r "$d/speed")|$(readlink -f "$d/device" 2>/dev/null)"; done
echo '@@NX-HW:addr@@'
ip -o addr show 2>/dev/null | head -n 200
echo '@@NX-HW:route@@'
ip route show default 2>/dev/null
echo '@@NX-HW:dns@@'
grep -E '^nameserver' /etc/resolv.conf 2>/dev/null
echo '@@NX-HW:video@@'
for d in /sys/class/video4linux/*; do [ -e "$d" ] || continue; n=${d##*/}; echo "$n|$(r "$d/name")|$(lnk "$d/device/driver")|$(stat -c '%G|%a' "/dev/$n" 2>/dev/null)|$(readlink -f "$d/device" 2>/dev/null)"; done
echo '@@NX-HW:serial@@'
for f in /dev/ttyUSB* /dev/ttyACM* /dev/ttyTHS* /dev/ttyAMA* /dev/ttyCH*; do [ -e "$f" ] || continue; n=${f##*/}; echo "$f|$(stat -c '%G|%a' "$f" 2>/dev/null)|$(lnk "/sys/class/tty/$n/device/driver")|$(readlink -f "/sys/class/tty/$n/device" 2>/dev/null)"; done
echo '@@NX-HW:serial_ids@@'
for f in /dev/serial/by-id/*; do [ -L "$f" ] && echo "${f##*/}|$(basename "$(readlink "$f")")"; done
echo '@@NX-HW:buses@@'
for f in /dev/i2c-* /dev/spidev*; do [ -e "$f" ] && echo "$f"; done
echo '@@NX-HW:iio@@'
for d in /sys/bus/iio/devices/iio:device*; do [ -e "$d" ] && echo "${d##*/}|$(r "$d/name")"; done
echo '@@NX-HW:input@@'
for d in /sys/class/input/js*; do [ -e "$d" ] && echo "${d##*/}|$(r "$d/device/name")"; done
echo '@@NX-HW:audio@@'
cat /proc/asound/cards 2>/dev/null | head -n 40
echo '@@NX-HW:power@@'
for d in /sys/class/power_supply/*; do [ -e "$d" ] && echo "${d##*/}|$(r "$d/type")|$(r "$d/capacity")|$(r "$d/status")"; done
echo '@@NX-HW:kernel@@'
K='firmware|probe of|descriptor read|unable to enumerate|not accepting address|cannot reset|over-current|under-voltage|I/O error|NVRM'
if out=$(dmesg --level=emerg,alert,crit,err,warn 2>/dev/null); then echo 'source=dmesg'; printf '%s\n' "$out" | grep -iE "$K" | tail -n 80 | cut -c1-500; elif out=$(journalctl -k -p 4 -b --no-pager -q 2>/dev/null) && [ -n "$out" ]; then echo 'source=journal'; printf '%s\n' "$out" | grep -iE "$K" | tail -n 80 | cut -c1-500; else echo 'source=unavailable'; fi
echo '@@NX-HW:failed_units@@'
systemctl --failed --no-legend --plain --no-pager 2>/dev/null | head -n 40
echo '@@NX-HW:end@@'
'''

# What is sent over SSH: the script runs under sh whatever the account's login shell is (fish, csh...).
SCAN_EXEC = 'sh -c ' + shlex.quote(SCAN_COMMAND)
SCAN_OUTPUT_LIMIT = 262144

CATEGORIES = {
    'compute': '处理器与内存', 'gpu': 'GPU 与加速器', 'storage': '存储', 'network': '网络',
    'camera': '相机与视频', 'serial': '串口', 'can': 'CAN 总线', 'usb': 'USB 设备', 'pci': 'PCI 设备',
    'sensor': '传感器与总线', 'audio': '音频', 'input': '输入设备', 'power': '电源与电池', 'service': '系统服务',
}
SEVERITY_ORDER = {'error': 3, 'warning': 2, 'info': 1}

# PCI classes (first byte of the class code) that work without a kernel driver
# often enough that a missing one is only noted: bridges, memory controllers,
# system peripherals, signal processing, processors.
PCI_DRIVERLESS_OK = {'05', '06', '08', '0b', '11', 'ff'}
PCI_KIND = {'01': ('storage', '存储控制器'), '02': ('network', '网络控制器'), '03': ('gpu', '显示 / GPU'),
            '04': ('audio', '多媒体控制器'), '07': ('pci', '通信控制器'), '09': ('input', '输入控制器'),
            '0c': ('usb', '串行总线控制器'), '0d': ('network', '无线控制器'), '12': ('gpu', '处理加速器')}
# USB interface classes whose devices are expected to bind a kernel driver.
USB_STANDARD = {'01': '音频', '02': '通信（CDC）', '03': '人机输入（HID）', '06': '图像', '07': '打印机',
                '08': '大容量存储', '0a': 'CDC 数据', '0e': '视频（UVC）', 'e0': '无线'}
VIRTUAL_NET = re.compile(r'^(?:veth|docker|br-|virbr|vmnet|tun|tap|wg|tailscale|zt|dummy|bond|team|vxlan|flannel|cni|cali|kube|lxc|sit|ip6tnl|gre)')

KERNEL_PATTERNS = [
    (re.compile(r'Direct firmware load for (\S+) failed|firmware: failed to load (\S+)', re.I), 'firmware',
     '固件缺失：{0}', '驱动需要的固件文件不存在，相关硬件可能不能工作。通常安装对应的固件包（如 linux-firmware）后需要重新加载驱动或重启。', 'auto'),
    (re.compile(r'probe of (\S+) failed with error (-?\d+)', re.I), 'probe',
     '驱动加载失败：{0}', '内核驱动探测该设备失败（错误 {1}）。可能是驱动与硬件不匹配、缺少固件或供电/接线问题。', 'auto'),
    (re.compile(r'usb (\S+?):.*(?:device descriptor read|unable to enumerate|device not accepting address|cannot reset)', re.I), 'usb-enum',
     'USB 设备枚举失败：端口 {0}', 'USB 设备连接后无法完成识别，常见原因是线缆、接口接触不良或供电不足，需要现场检查。', 'needs_user'),
    (re.compile(r'over-current', re.I), 'usb-overcurrent',
     'USB 端口过流', '某个 USB 端口检测到过流，设备可能被断电。需要现场检查外设功耗与供电。', 'needs_user'),
    (re.compile(r'Under-voltage detected', re.I), 'undervoltage',
     '供电电压不足', '主板检测到欠压，外设和处理器可能不稳定。需要现场检查电源。', 'needs_user'),
    (re.compile(r'I/O error, dev (\w+)', re.I), 'io-error',
     '存储 I/O 错误：{0}', '存储设备读写出错，可能存在硬件或文件系统问题。', 'needs_user'),
    (re.compile(r'NVRM: (.{0,80})'), 'nvrm',
     'NVIDIA 驱动报错', 'NVIDIA 内核驱动在本次启动中报告了错误：{0}', 'auto'),
]
KERNEL_CATEGORY = {'firmware': 'pci', 'probe': 'pci', 'usb-enum': 'usb', 'usb-overcurrent': 'usb',
                   'undervoltage': 'power', 'io-error': 'storage', 'nvrm': 'gpu'}
SINGLE_LOG_ISSUES = {'usb-overcurrent', 'undervoltage', 'nvrm'}     # one issue however many log lines
MAX_LOG_ISSUES = 10
MAX_DEVICES = 300
SKIP_DISK = re.compile(r'^(?:loop|zram|ram|nbd|mmcblk\d+boot\d+|mmcblk\d+rpmb)')
# Names for a PCI device when lspci (pciutils) is not installed.
PCI_VENDORS = {'0x10de': 'NVIDIA', '0x8086': 'Intel', '0x10ec': 'Realtek', '0x14e4': 'Broadcom', '0x1af4': 'Virtio',
               '0x1b4b': 'Marvell', '0x144d': 'Samsung', '0x1e0f': 'KIOXIA', '0x15b7': 'SanDisk / WD', '0x1d6a': 'Aquantia',
               '0x168c': 'Qualcomm Atheros', '0x17cb': 'Qualcomm', '0x1987': 'Phison', '0x126f': 'Silicon Motion',
               '0x1c5c': 'SK hynix', '0x1022': 'AMD', '0x1002': 'AMD / ATI', '0x10b5': 'PLX', '0x1912': 'Renesas', '0x1b21': 'ASMedia',
               '0x14c3': 'MediaTek', '0x1344': 'Micron', '0x1cc1': 'ADATA', '0x2646': 'Kingston', '0x1d97': 'Shenzhen Longsys'}


def sections(text):
    result, name = {}, None
    for line in str(text).splitlines():
        match = MARK.match(line.strip())
        if match:
            name = match.group(1)
            result.setdefault(name, [])
        elif name:
            result[name].append(line.rstrip('\r'))
    return result


def _pairs(lines):
    values = {}
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and re.fullmatch(r'[a-z_]+', key):
            values[key] = value.strip()
    return values


def _fields(line, count):
    parts = line.split('|')
    return (parts + [''] * count)[:count]


def _usb_owner(path):
    """The USB device id (e.g. ``1-2.3``) a sysfs device path belongs to."""
    found = re.findall(r'/(\d+-[\d.]+):\d+\.\d+', path or '')
    return found[-1] if found else None


def _pci_owner(path):
    found = re.findall(r'/([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])(?=/|$)', path or '')
    return found[-1] if found else None


def _kib(text):
    match = re.search(r'([0-9]+)', text or '')
    return int(match.group(1)) if match else None


def _gib(kib):
    return f'{kib / 1048576:.1f} GiB' if kib else '未知'


def _accessible(group, mode, system):
    """Whether the SSH user can read and write a device node with this group/mode."""
    if system.get('uid') == '0':
        return True
    try:
        bits = int(mode, 8)
    except (TypeError, ValueError):
        return True     # unknown permissions are not reported as a problem
    if bits & 0o006 == 0o006:
        return True
    return group in system.get('groups', '').split() and bits & 0o060 == 0o060


class _Builder:
    def __init__(self):
        self.devices, self.issues, self.notes = [], [], []
        self.history = 0
        self._issue_ids = set()

    def device(self, id, category, name, bus='', driver='', detail='', status='ok'):
        row = {'id': id, 'category': category, 'name': name[:200] or '未命名设备', 'bus': bus[:120],
               'driver': driver[:80], 'detail': detail[:600], 'status': status}
        if len(self.devices) < MAX_DEVICES:
            self.devices.append(row)
        return row

    def issue(self, id, severity, category, title, detail, hint='', device_id=None, fixable='auto', source='scan'):
        if id in self._issue_ids:
            return
        self._issue_ids.add(id)
        self.issues.append({'id': id[:200], 'severity': severity, 'category': category, 'title': title[:200],
                            'detail': detail[:800], 'hint': hint[:600], 'device_id': device_id,
                            'fixable': fixable, 'source': source})


def _system(b, s):
    system = _pairs(s.get('system', []))
    cpu_lines = s.get('cpu', [])
    cpu = {}
    for line in cpu_lines:
        key, sep, value = line.partition(':')
        if sep:
            cpu[key.strip()] = value.strip()
    named = [cpu.get(k, '') for k in ('Model name', 'model name', 'Hardware', 'Processor', 'cpu model')]
    cpu_name = next((x for x in named if x and x != '-'), '')
    if not cpu_name and cpu.get('Vendor ID'):
        cpu_name = (cpu['Vendor ID'] + ' ' + system.get('arch', '')).strip()
    cores = cpu.get('CPU(s)') or _pairs(cpu_lines).get('nproc') or ''
    memory = {k: _kib(v) for k, v in (line.split(':', 1) for line in s.get('memory', []) if ':' in line)}
    model = system.get('model') or ' '.join(x for x in (system.get('vendor'), system.get('product')) if x)
    if system.get('tegra'):
        release = re.search(r'R(\d+)\s*\(release\)\s*,\s*REVISION:\s*([\d.]+)', system['tegra'])
        system['jetson'] = f'L4T R{release.group(1)}.{release.group(2)}' if release else system['tegra'][:80]
    b.device('compute:cpu', 'compute', cpu_name or '处理器', system.get('arch', ''), '',
             f'{cores} 个逻辑核心' if cores else '核心数未知', 'ok' if cpu_name else 'unknown')
    b.device('compute:memory', 'compute', '内存 ' + _gib(memory.get('MemTotal')), '', '',
             f"可用 {_gib(memory.get('MemAvailable'))} · 交换 {_gib(memory.get('SwapTotal'))}", 'ok' if memory.get('MemTotal') else 'unknown')
    return {'hostname': system.get('hostname', ''), 'os': system.get('os', ''), 'kernel': system.get('kernel', ''),
            'arch': system.get('arch', ''), 'model': model[:200], 'jetson': system.get('jetson', ''),
            'cpu': cpu_name[:200], 'cores': cores, 'memory_kib': memory.get('MemTotal'),
            'user': system.get('user', ''), 'uid': system.get('uid', ''), 'groups': system.get('groups', ''),
            'ssh_server_ip': (system.get('ssh', '').split() + ['', '', ''])[2]}


def _storage(b, s, usb_by_port, pci_by_slot, claimed):
    paths = dict(_fields(line, 2) for line in s.get('disk_paths', []) if '|' in line)
    for line in s.get('disks', []):
        pairs = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if not pairs.get('NAME') or pairs.get('TYPE') not in ('disk', 'rom') or SKIP_DISK.match(pairs['NAME']) or pairs.get('SIZE') in ('0', '0B'):
            continue
        path = paths.get(pairs['NAME'], '')
        owner_usb, owner_pci = _usb_owner(path), _pci_owner(path)
        owner = usb_by_port.get(owner_usb) or pci_by_slot.get(owner_pci) or {}
        claimed.update(x for x in (owner_usb, owner_pci) if x and owner)
        driver = owner.get('driver') or ', '.join(sorted({i['driver'] for i in owner.get('interfaces', []) if i['driver']}))
        name = pairs.get('MODEL') or owner.get('name', '').split(': ', 1)[-1] or pairs['NAME']
        bus = ('USB ' + owner_usb) if owner_usb and owner else ('PCIe ' + owner_pci) if owner_pci and owner else (pairs.get('TRAN') or '').upper() or pairs['NAME']
        b.device('disk:' + pairs['NAME'], 'storage', name.strip(), bus, driver, f"/dev/{pairs['NAME']} · {pairs.get('SIZE') or '容量未知'}")
    root = (s.get('rootfs') or [''])[0].split()
    if len(root) >= 5 and re.fullmatch(r'[0-9]{1,3}%', root[4]):
        used = int(root[4][:-1])
        if used >= 90:
            b.issue('rootfs-full', 'warning' if used < 97 else 'error', 'storage', f'根分区空间不足（已用 {used}%）',
                    f'根分区 {root[0]} 已使用 {used}%，安装驱动或软件包可能失败。',
                    '清理日志、缓存或不再使用的软件包后重新扫描。', fixable='auto')


def _gpu(b, s, pci, system):
    gpu = s.get('gpu', [])
    state = _pairs(gpu[:1]).get('exit', 'missing')
    output = [line for line in gpu[1:] if line.strip()]
    nvidia = [d for d in pci if d['vendor'] == '0x10de' and d['class'][2:4] in ('03', '12')]
    if state == '0' and output:
        for index, line in enumerate(output):
            name, _, rest = line.partition(',')
            b.device(f'gpu:nvidia:{index}', 'gpu', name.strip(), 'NVIDIA', 'nvidia', '驱动 / 显存：' + rest.strip(' ,'))
    if system.get('jetson') and not (state == '0' and output):
        b.device('gpu:tegra', 'gpu', 'NVIDIA Jetson 集成 GPU', 'SoC', 'nvgpu', 'JetPack / ' + system['jetson'])
    if nvidia and not (state == '0' and output):
        driver = nvidia[0]['driver']
        failing = state not in ('0', 'missing')
        detail = ('nvidia-smi 无法与驱动通信：' + ' '.join(output)[:300]) if failing else (
            '检测到 NVIDIA GPU，当前使用开源 nouveau 驱动，CUDA 与 GPU 推理不可用。' if driver == 'nouveau' else
            '检测到 NVIDIA GPU，但没有可用的 NVIDIA 驱动（未找到 nvidia-smi）。')
        b.issue('nvidia-driver', 'error' if driver != 'nouveau' else 'warning', 'gpu', 'NVIDIA GPU 驱动未就绪', detail,
                '确认发行版与 GPU 型号后安装匹配的 NVIDIA 驱动（安装后通常需要重启）。', device_id='pci:' + nvidia[0]['slot'])


def _pci(b, s):
    names, modules, slot = {}, {}, None
    for line in s.get('pci_names', []):
        head = re.match(r'^([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])\s+(.+)$', line)
        if head:
            slot = head.group(1)
            names[slot] = re.sub(r'\s*\(rev [0-9a-f]+\)$', '', head.group(2))
        elif slot and 'Kernel modules:' in line:
            modules[slot] = line.split(':', 1)[1].strip()
    pci = []
    for line in s.get('pci', []):
        slot, cls, vendor, device, driver = _fields(line, 5)
        if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]', slot) or not cls.startswith('0x'):
            continue
        pci.append({'slot': slot, 'class': cls, 'vendor': vendor, 'device': device, 'driver': driver,
                    'name': names.get(slot) or f"{PCI_VENDORS.get(vendor, 'PCI')} 设备 {vendor[2:]}:{device[2:]}", 'modules': modules.get(slot, '')})
    return pci


def _pci_rows(b, pci, claimed):
    for d in pci:
        if d['slot'] in claimed:
            continue
        base = d['class'][2:4]
        if base == '06':
            continue        # bridges and host bridges are infrastructure, not devices to set up
        category, kind = PCI_KIND.get(base, ('pci', 'PCI 设备'))
        did = 'pci:' + d['slot']
        status = 'ok' if d['driver'] else ('unknown' if base in PCI_DRIVERLESS_OK else 'error')
        detail = kind + (' · 可用内核模块：' + d['modules'] if d['modules'] and not d['driver'] else '')
        b.device(did, category, d['name'].split(': ', 1)[-1], 'PCIe ' + d['slot'], d['driver'] or '未加载', detail, status)
        if status == 'error' and not (base == '03' and d['vendor'] == '0x10de'):
            b.issue('pci-driver:' + d['slot'], 'error', category, f"未加载驱动：{d['name'].split(': ', 1)[-1][:80]}",
                    f"{kind}（{d['slot']}，ID {d['vendor'][2:]}:{d['device'][2:]}）没有绑定内核驱动，设备当前不可用。",
                    ('内核已提供候选模块 ' + d['modules'] + '，可尝试加载。') if d['modules'] else '需要确认设备型号并安装对应驱动。',
                    device_id=did)


def _usb(b, s):
    names = {}
    for line in s.get('usb_names', []):
        match = re.search(r'ID ([0-9a-f]{4}):([0-9a-f]{4})\s+(.*)$', line)
        if match:
            names[match.group(1) + ':' + match.group(2)] = match.group(3).strip()
    usb = []
    for line in s.get('usb', []):
        port, vendor, product, cls, speed, maker, title, interfaces = _fields(line, 8)
        if not re.fullmatch(r'\d+-[\d.]+|usb\d+', port):
            continue
        ifaces = [tuple(x.split(':', 1)) for x in interfaces.split() if ':' in x]
        usb.append({'port': port, 'id': f'{vendor}:{product}', 'class': cls, 'speed': speed,
                    'name': ' '.join(x for x in (maker, title) if x) or names.get(f'{vendor}:{product}') or f'USB {vendor}:{product}',
                    'interfaces': [{'class': c, 'driver': '' if drv == '-' else drv} for c, drv in ifaces]})
    return usb


def _usb_rows(b, usb, claimed):
    for d in usb:
        if d['port'].startswith('usb') or d['class'] == '09' or d['port'] in claimed:
            continue        # root hubs and hubs are infrastructure
        did = 'usb:' + d['port']
        drivers = sorted({i['driver'] for i in d['interfaces'] if i['driver']})
        unbound = [i for i in d['interfaces'] if not i['driver']]
        standard = [USB_STANDARD[i['class']] for i in unbound if i['class'] in USB_STANDARD]
        # Some interfaces bound: the rest are often claimed by an application through libusb on purpose.
        partial = bool(drivers)
        status = 'ok' if not unbound or (standard and partial) else 'warning' if standard else 'unknown'
        speed = f" · {d['speed']} Mb/s" if d['speed'] else ''
        detail = f"ID {d['id']}{speed}" + ('' if not unbound else ' · 部分接口未绑定驱动' if drivers else ' · 未绑定内核驱动')
        if status == 'unknown':
            detail += '（厂商自定义接口，可能由用户态 SDK 通过 libusb 驱动）'
        b.device(did, 'usb', d['name'], 'USB ' + d['port'], ', '.join(drivers) or '未加载', detail, status)
        if standard:
            b.issue('usb-driver:' + d['port'], 'info' if partial else 'warning', 'usb', f"USB 设备未绑定驱动：{d['name'][:80]}",
                    f"{d['name']}（ID {d['id']}，端口 {d['port']}）的{'、'.join(sorted(set(standard)))}接口没有绑定内核驱动。" +
                    ('该设备的其他接口已由 ' + '、'.join(drivers) + ' 驱动，未绑定的接口可能被应用程序直接占用。' if partial else ''),
                    '确认内核模块是否已加载（例如视频类需要 uvcvideo），或安装厂商驱动。', device_id=did,
                    fixable='needs_user' if partial else 'auto')


def _network(b, s, system, usb_by_port, pci_by_slot, claimed):
    addresses = {}
    for line in s.get('addr', []):
        match = re.match(r'^\d+:\s+(\S+?)(?:@\S+)?\s+(inet6?)\s+(\S+)', line)
        if match:
            addresses.setdefault(match.group(1), []).append((match.group(2), match.group(3), 'scope global' in line))
    ssh_ip = system.get('ssh_server_ip', '')
    ssh_iface = next((name for name, items in addresses.items() if any(a.split('/')[0] == ssh_ip for _, a, _ in items)), '') if ssh_ip else ''
    physical = 0
    uplink = bool(s.get('route')) and any(family == 'inet' for items in addresses.values() for family, _, glob in items if glob)
    for line in s.get('net', []):
        name, oper, carrier, flags, kind, mac, driver, wireless, speed, path = _fields(line, 10)
        if not name or not re.fullmatch(r'[\w.:@-]{1,32}', name):
            continue
        is_can = kind == '280'
        gadget = '/gadget' in path      # USB device mode (e.g. Jetson l4tbr0): no link until a computer is plugged in
        if not path.startswith('/sys/devices/') or '/virtual/' in path or VIRTUAL_NET.match(name):
            if not is_can:
                continue
        physical += 1
        owner_usb, owner_pci = _usb_owner(path), _pci_owner(path)
        hardware = usb_by_port.get(owner_usb, {}).get('name') or pci_by_slot.get(owner_pci, {}).get('name', '').split(': ', 1)[-1]
        claimed.update(x for x in (owner_usb, owner_pci) if x)
        try:
            admin_up = bool(int(flags, 16) & 1)
        except ValueError:
            admin_up = oper == 'up'
        v4 = [a for family, a, glob in addresses.get(name, []) if family == 'inet']
        v6 = [a for family, a, glob in addresses.get(name, []) if family == 'inet6' and glob]
        category = 'can' if is_can else 'network'
        label = 'CAN 接口' if is_can else 'USB 设备模式网口' if gadget else ('无线网卡' if wireless == '1' else '有线网卡')
        did = 'net:' + name
        bus = 'USB ' + owner_usb if owner_usb else 'PCIe ' + owner_pci if owner_pci else ''
        detail = ' · '.join(x for x in (label, hardware, ('IP ' + ', '.join(v4)) if v4 else '', f'{speed} Mb/s' if re.fullmatch(r'[0-9]{1,7}', speed) and int(speed) > 0 else '',
                                         '承载当前 SSH 连接' if name == ssh_iface else '') if x)
        row = b.device(did, category, name, bus, driver or ('—' if is_can else 'USB gadget' if gadget else '未知'), detail)
        if gadget:
            row['status'] = 'ok' if oper == 'up' else 'unknown'
            row['detail'] += ' · 用 USB 线连接电脑时使用'
        elif not admin_up:
            row['status'] = 'warning'
            b.issue('net-down:' + name, 'warning', category, f'{label} {name} 未启用',
                    f'{name} 处于关闭状态（operstate={oper or "unknown"}）。',
                    '启用接口；CAN 接口需要先确认总线波特率。' if is_can else '启用接口，或在网络管理器中启用对应连接。',
                    device_id=did, fixable='needs_user' if is_can else 'auto')
        elif not is_can and carrier == '0' and wireless != '1':
            spare = uplink and name != ssh_iface      # another interface already connects the robot
            row['status'] = 'unknown' if spare else 'warning'
            b.issue('net-carrier:' + name, 'info' if spare else 'warning', category, f'{name} 没有连接信号',
                    f'有线网卡 {name} 已启用，但检测不到链路（网线未连接或对端未通电）。' + ('设备已通过其他网口联网，如果这个网口本来就不用，可以忽略。' if spare else ''),
                    '需要现场检查网线和交换机端口。', device_id=did, fixable='needs_user')
        elif not is_can and oper == 'up' and not v4 and not v6:
            row['status'] = 'warning'
            b.issue('net-noip:' + name, 'warning', category, f'{name} 没有获取到 IP 地址',
                    f'{label} {name} 已连接但没有 IP 地址' + ('（无线网卡可能没有关联到网络）。' if wireless == '1' else '（DHCP 可能失败）。'),
                    '检查网络管理器连接配置并重新获取地址。', device_id=did)
    system['ssh_interface'] = ssh_iface
    if physical and not s.get('route'):
        b.issue('net-route', 'warning', 'network', '没有默认路由', '设备没有默认网关，无法访问外部网络（安装驱动和软件包会失败）。',
                '检查上行网卡的地址与网关配置。')
    elif physical and not any(line.strip() for line in s.get('dns', [])):
        b.issue('net-dns', 'warning', 'network', '没有配置 DNS 服务器', '/etc/resolv.conf 中没有 nameserver，域名解析会失败。',
                '在网络管理器中为上行连接配置 DNS。')


def _video(b, s, system, usb_by_port, claimed):
    cameras = {}
    for line in s.get('video', []):
        node, name, driver, group, mode, path = _fields(line, 6)
        if not re.fullmatch(r'video\d+', node):
            continue
        owner = _usb_owner(path) or _pci_owner(path) or path
        camera = cameras.setdefault(owner, {'nodes': [], 'name': name, 'driver': driver, 'usb': _usb_owner(path), 'denied': []})
        camera['nodes'].append('/dev/' + node)
        if not _accessible(group, mode, system):
            camera['denied'].append(('/dev/' + node, group))
    for owner, cam in cameras.items():
        if cam['usb']:
            claimed.add(cam['usb'])
        name = usb_by_port.get(cam['usb'], {}).get('name') or cam['name'] or '视频设备'
        did = 'camera:' + (cam['usb'] or cam['nodes'][0])
        row = b.device(did, 'camera', name, 'USB ' + cam['usb'] if cam['usb'] else '板载 / CSI', cam['driver'] or '未知',
                       '设备节点 ' + '、'.join(cam['nodes']))
        if cam['denied']:
            row['status'] = 'warning'
            node, group = cam['denied'][0]
            b.issue('video-perm:' + cam['nodes'][0], 'warning', 'camera', f'当前用户无权访问相机 {node}',
                    f'{node} 属于用户组 {group or "未知"}，SSH 用户 {system.get("user") or ""} 不在该组中，程序将无法读取画面。',
                    f'把该用户加入 {group or "video"} 组（重新登录后生效）。', device_id=did)


def _serial(b, s, system, usb_by_port, claimed):
    ids = {}
    for line in s.get('serial_ids', []):
        label, target = _fields(line, 2)
        if target:
            ids.setdefault(target, label)
    for line in s.get('serial', []):
        path, group, mode, driver, sys_path = _fields(line, 5)
        if not path.startswith('/dev/tty'):
            continue
        owner = _usb_owner(sys_path)
        if owner:
            claimed.add(owner)
        name = usb_by_port.get(owner, {}).get('name') or ids.get(path.rsplit('/', 1)[-1], '') or ('板载串口' if 'THS' in path or 'AMA' in path else '串口设备')
        did = 'serial:' + path
        row = b.device(did, 'serial', path.rsplit('/', 1)[-1] + ' · ' + name, 'USB ' + owner if owner else '板载 UART', driver or '未知',
                       f'{path} · 用户组 {group or "未知"} · 权限 {mode or "未知"}')
        if not _accessible(group, mode, system):
            row['status'] = 'warning'
            b.issue('serial-perm:' + path, 'warning', 'serial', f'当前用户无权读写串口 {path}',
                    f'{path} 属于用户组 {group or "未知"}（权限 {mode}），SSH 用户 {system.get("user") or ""} 不在该组中，底盘/传感器程序将无法打开串口。',
                    f'把该用户加入 {group or "dialout"} 组（重新登录后生效）。', device_id=did)


def _small(b, s):
    buses = [line.strip() for line in s.get('buses', []) if line.startswith('/dev/')]
    i2c, spi = [x for x in buses if 'i2c' in x], [x for x in buses if 'spidev' in x]
    if i2c:
        b.device('bus:i2c', 'sensor', f'I2C 总线 × {len(i2c)}', 'I2C', '', '、'.join(i2c[:12]) + (' …' if len(i2c) > 12 else ''))
    if spi:
        b.device('bus:spi', 'sensor', f'SPI 设备节点 × {len(spi)}', 'SPI', '', '、'.join(spi[:12]))
    for line in s.get('iio', []):
        node, name = _fields(line, 2)
        if node:
            b.device('iio:' + node, 'sensor', name or node, 'IIO', '', node)
    for line in s.get('input', []):
        node, name = _fields(line, 2)
        if node:
            b.device('input:' + node, 'input', name or node, '输入', '', '/dev/input/' + node)
    for line in s.get('audio', []):
        match = re.match(r'^\s*(\d+)\s+\[[^\]]*\]:\s*(.+?)\s+-\s+(.+)$', line)
        if match:
            b.device('audio:' + match.group(1), 'audio', match.group(3).strip(), 'ALSA', match.group(2).strip(), '声卡 ' + match.group(1))
    for line in s.get('power', []):
        node, kind, capacity, state = _fields(line, 4)
        if node and kind in ('Battery', 'UPS', 'Mains', 'USB'):
            detail = ' · '.join(x for x in (f'电量 {capacity}%' if capacity else '', state) if x)
            b.device('power:' + node, 'power', node, kind, '', detail or kind,
                     'warning' if kind == 'Battery' and re.fullmatch(r'[0-9]{1,3}', capacity) and int(capacity) < 15 else 'ok')


def _attach_log(b, line, kind, groups):
    """Kernel log lines are history since boot. A line about a device that has a problem now joins that
    problem; a line about a device that is present without problems is dropped (counted in ``b.history``);
    only a line about a device that is not present (or not identifiable) stands alone."""
    slot = re.search(r'\b([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])\b', line)
    port = re.search(r'\busb (\d+-[\d.]+)', line)
    if not slot and not port:
        return False
    row_id, bus = ('pci:' + slot.group(1), 'PCIe ' + slot.group(1)) if slot else ('usb:' + port.group(1), 'USB ' + port.group(1))
    rows = {r['id'] for r in b.devices if r['id'] == row_id or r['bus'] == bus}
    if not rows:
        return False
    issue = next((i for i in b.issues if i['device_id'] in rows and i['source'] == 'scan'), None)
    if not issue:
        b.history += 1
        return True
    if issue['detail'].count('\n内核日志：') < 3:
        issue['detail'] = (issue['detail'] + '\n内核日志：' + line.strip()[:300])[:1200]
    if kind == 'firmware' and '缺少固件' not in issue['hint']:
        issue['hint'] = f'缺少固件 {groups[0]}：安装提供该文件的固件包后重新加载驱动。' + issue['hint']
    return True


def _kernel(b, s):
    lines = s.get('kernel', [])
    source = _pairs(lines[:1]).get('source', 'unavailable')
    if source == 'unavailable':
        b.notes.append('无法读取内核日志（该账号没有权限），驱动与固件错误可能未被发现。')
        return
    count = 0
    for line in lines[1:]:
        line = line[:500]
        if count >= MAX_LOG_ISSUES:
            break
        for pattern, kind, title, detail, fixable in KERNEL_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            groups = [g for g in match.groups() if g] or ['']
            if _attach_log(b, line, kind, groups):
                break
            key = f'log-{kind}:' + (kind if kind in SINGLE_LOG_ISSUES else groups[0])
            before = len(b.issues)
            b.issue(key, 'warning', KERNEL_CATEGORY[kind],
                    title.format(*groups, ''), detail.format(*(groups + [''])) + '\n内核日志：' + line.strip()[:300],
                    '内核日志记录的是本次启动以来的历史；修复后通常需要重新加载驱动、重新插拔或重启才能确认。',
                    fixable=fixable, source='kernel_log')
            count += len(b.issues) > before
            break


def _units(b, s):
    for line in s.get('failed_units', []):
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].endswith(('.service', '.mount', '.socket', '.device', '.target')):
            unit = parts[0]
            b.issue('unit:' + unit, 'info', 'service', f'系统服务启动失败：{unit}',
                    f'{unit} 处于失败状态' + (f'（{parts[4]}）' if len(parts) > 4 else '') + '。如果它负责相机、网络或传感器，相应硬件会不可用。',
                    '先确认它是否与硬件有关；机器人自身的应用或启动服务可能会带起底盘或运动程序，不要让助手直接启动或重启。',
                    fixable='needs_user')


def _finish(b):
    worst = {}
    for issue in b.issues:
        if issue['device_id']:
            current = worst.get(issue['device_id'])
            if not current or SEVERITY_ORDER[issue['severity']] > SEVERITY_ORDER[current]:
                worst[issue['device_id']] = issue['severity']
    for row in b.devices:
        severity = worst.get(row['id'])
        if severity == 'error':
            row['status'] = 'error'
        elif severity == 'warning' and row['status'] != 'error':
            row['status'] = 'warning'
    order = list(CATEGORIES)
    b.devices.sort(key=lambda row: (order.index(row['category']) if row['category'] in order else len(order)))
    b.issues.sort(key=lambda issue: -SEVERITY_ORDER[issue['severity']])


def parse(text):
    """Parse ``SCAN_COMMAND`` output. Unknown or missing sections are tolerated."""
    s = sections(text)
    b = _Builder()
    system = _system(b, s)
    pci = _pci(b, s)
    usb = _usb(b, s)
    usb_by_port = {d['port']: d for d in usb}
    pci_by_slot = {d['slot']: d for d in pci}
    claimed = set()
    _gpu(b, s, pci, system)
    _storage(b, s, usb_by_port, pci_by_slot, claimed)
    _network(b, s, system, usb_by_port, pci_by_slot, claimed)
    _video(b, s, system, usb_by_port, claimed)
    _serial(b, s, system, usb_by_port, claimed)
    for d in pci:
        if d['vendor'] == '0x10de' and d['class'][2:4] in ('03', '12') and any(r['id'].startswith('gpu:nvidia') for r in b.devices):
            claimed.add(d['slot'])
    _pci_rows(b, pci, claimed)
    _usb_rows(b, usb, claimed)
    _small(b, s)
    _kernel(b, s)
    _units(b, s)
    if b.history:
        b.notes.append(f'已略过 {b.history} 条与当前工作正常的设备有关的历史内核日志（本次启动以来记录）。')
    if 'end' not in s:
        b.notes.append('扫描输出不完整（命令超时或输出被截断），表格可能缺少部分设备。')
    _finish(b)
    counts = {key: sum(1 for row in b.devices if row['category'] == key) for key in CATEGORIES}
    summary = {'devices': len(b.devices), 'ok': sum(1 for row in b.devices if row['status'] == 'ok'),
               'attention': sum(1 for row in b.devices if row['status'] in ('warning', 'error')),
               'errors': sum(1 for i in b.issues if i['severity'] == 'error'),
               'warnings': sum(1 for i in b.issues if i['severity'] == 'warning'),
               'categories': {k: v for k, v in counts.items() if v}}
    return {'system': system, 'devices': b.devices, 'issues': b.issues, 'notes': b.notes, 'summary': summary, 'complete': 'end' in s}
