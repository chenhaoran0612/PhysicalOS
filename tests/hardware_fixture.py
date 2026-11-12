"""Scan outputs for the hardware initialisation tests (issue #2).

Shaped like ``hardware_scan.SCAN_COMMAND`` output on real machines: a Jetson Orin NX
robot (chassis on a CH340 serial adapter, USB camera, Orbbec depth
camera, Intel Wi-Fi card without firmware, CAN bus down) and an x86 computer whose
NVIDIA driver is not loaded. ``CONTAINER`` is a capture from a Debian container.
"""
from pathlib import Path

JETSON_SYSTEM = '''@@NX-HW:system@@
hostname=robot-orin
kernel=5.10.120-tegra
arch=aarch64
os=Ubuntu 20.04.6 LTS
model=NVIDIA Orin NX Developer Kit
vendor=
product=
tegra=# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023
user=robot
uid=1000
groups={groups}
ssh=192.168.1.20 52344 192.168.1.50 22
'''

JETSON_BODY = '''@@NX-HW:cpu@@
Architecture:                    aarch64
CPU(s):                          8
Vendor ID:                       ARM
Model name:                      Cortex-A78AE
nproc=8
@@NX-HW:memory@@
MemTotal:       15655944 kB
MemAvailable:   12093112 kB
SwapTotal:       7827952 kB
@@NX-HW:disks@@
NAME="nvme0n1" TYPE="disk" SIZE="238.5G" MODEL="SAMSUNG MZVL2256HCHQ-00B00" TRAN="nvme"
NAME="zram0" TYPE="disk" SIZE="1.9G" MODEL="" TRAN=""
NAME="mmcblk0boot0" TYPE="disk" SIZE="4M" MODEL="" TRAN=""
@@NX-HW:disk_paths@@
nvme0n1|/sys/devices/platform/14160000.pcie/pci0004:00/0004:00:00.0/0004:01:00.0/nvme/nvme0
mmcblk0boot0|/sys/devices/platform/3400000.sdhci/mmc_host/mmc0/mmc0:0001
@@NX-HW:rootfs@@
/dev/nvme0n1p1 245000000 120000000 113000000  52% /
@@NX-HW:gpu@@
exit=missing
@@NX-HW:pci@@
0001:00:00.0|0x060400|0x10de|0x229e|pcieport
0001:01:00.0|0x020000|0x10ec|0x8168|r8168
0004:00:00.0|0x060400|0x10de|0x229c|pcieport
0004:01:00.0|0x010802|0x144d|0xa80a|nvme
0008:00:00.0|0x060400|0x10de|0x229c|pcieport
0008:01:00.0|0x028000|0x8086|0x2723|
@@NX-HW:pci_names@@
0001:00:00.0 PCI bridge: NVIDIA Corporation Device 229e (rev a1)
\tKernel driver in use: pcieport
0001:01:00.0 Ethernet controller: Realtek Semiconductor Co., Ltd. RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller (rev 15)
\tKernel driver in use: r8168
\tKernel modules: r8168
0004:01:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller 980 (rev 00)
\tKernel driver in use: nvme
\tKernel modules: nvme
0008:01:00.0 Network controller: Intel Corporation Wi-Fi 6 AX200 (rev 1a)
\tKernel modules: iwlwifi
@@NX-HW:usb@@
usb1|1d6b|0002|09|480|Linux 5.10.120-tegra xhci-hcd|xHCI Host Controller| 09:hub
1-1|05e3|0610|09|480|GenesysLogic|USB2.1 Hub| 09:hub
1-2|1a86|7523|ff|12||USB Serial| ff:ch341
1-3|0bda|5830|ef|480|Generic|HD Web Camera| 0e:uvcvideo 0e:uvcvideo 01:snd-usb-audio 01:snd-usb-audio
1-4|2bc5|0614|00|480|Orbbec|Astra| {orbbec}
1-1.2|046d|c21f|00|12|Logitech|Gamepad F710| ff:xpad
@@NX-HW:usb_names@@
Bus 001 Device 003: ID 1a86:7523 QinHeng Electronics CH340 serial converter
Bus 001 Device 004: ID 0bda:5830 Realtek Semiconductor Corp. HD Web Camera
@@NX-HW:net@@
eth0|up|1|0x1003|1|48:b0:2d:11:22:33|r8168|0|1000|/sys/devices/platform/14100000.pcie/pci0001:00/0001:00:00.0/0001:01:00.0
can0|down||0x80|280||mttcan|0||/sys/devices/platform/c310000.mttcan
docker0|down|0|0x1003|1|02:42:ac:11:00:01||0||/sys/devices/virtual/net/docker0
l4tbr0|up|0|0x1003|1|6e:2c:0a:00:00:01||0||/sys/devices/virtual/net/l4tbr0
@@NX-HW:addr@@
2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global dynamic noprefixroute eth0\\       valid_lft 85000sec preferred_lft 85000sec
2: eth0    inet6 fe80::4ab0:2dff:fe11:2233/64 scope link noprefixroute \\       valid_lft forever preferred_lft forever
5: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
@@NX-HW:route@@
default via 192.168.1.1 dev eth0 proto dhcp metric 100
@@NX-HW:dns@@
nameserver 127.0.0.53
@@NX-HW:video@@
video0|HD Web Camera: HD Web Camera|uvcvideo|video|660|/sys/devices/platform/3610000.xhci/usb1/1-3/1-3:1.0
video1|HD Web Camera: HD Web Camera|uvcvideo|video|660|/sys/devices/platform/3610000.xhci/usb1/1-3/1-3:1.0
@@NX-HW:serial@@
/dev/ttyUSB0|dialout|660|ch341-uart|/sys/devices/platform/3610000.xhci/usb1/1-2/1-2:1.0/ttyUSB0
/dev/ttyTHS0|dialout|660|serial-tegra|/sys/devices/platform/3100000.serial
@@NX-HW:serial_ids@@
usb-1a86_USB_Serial-if00-port0|ttyUSB0
@@NX-HW:buses@@
/dev/i2c-0
/dev/i2c-1
/dev/i2c-2
/dev/spidev0.0
@@NX-HW:iio@@
@@NX-HW:input@@
js0|Logitech Gamepad F710
@@NX-HW:audio@@
 0 [HDWebCamera    ]: USB-Audio - HD Web Camera
                      Generic HD Web Camera at usb-3610000.xhci-3, high speed
 1 [APE            ]: tegra-ape - NVIDIA Jetson Orin NX APE
@@NX-HW:power@@
@@NX-HW:kernel@@
source=dmesg
[    5.123456] iwlwifi 0008:01:00.0: Direct firmware load for iwlwifi-cc-a0-72.ucode failed with error -2
[    5.123999] iwlwifi 0008:01:00.0: no suitable firmware found!
[    5.124100] iwlwifi: probe of 0008:01:00.0 failed with error -2
[   12.000001] usb 1-4: device descriptor read/64, error -71
[   12.400001] usb 1-4: device descriptor read/64, error -71
@@NX-HW:failed_units@@
nvargus-daemon.service loaded failed failed Argus daemon
@@NX-HW:end@@
'''


def jetson(fixed=False):
    groups = 'robot adm cdrom sudo audio dip video plugdev i2c gpio' + (' dialout' if fixed else '')
    orbbec = '0e:uvcvideo 0e:uvcvideo ff:-' if fixed else '0e:- 0e:- ff:-'
    return JETSON_SYSTEM.format(groups=groups) + JETSON_BODY.replace('{orbbec}', orbbec)


JETSON_ISSUES = {
    'pci-driver:0008:01:00.0', 'usb-driver:1-4', 'net-down:can0', 'serial-perm:/dev/ttyUSB0', 'serial-perm:/dev/ttyTHS0',
    'unit:nvargus-daemon.service',
}

X86_NVIDIA = '''@@NX-HW:system@@
hostname=lab-pc
kernel=6.5.0-41-generic
arch=x86_64
os=Ubuntu 22.04.4 LTS
model=
vendor=Micro-Star International Co., Ltd.
product=MS-7C02
tegra=
user=robot
uid=1000
groups=robot sudo dialout video
ssh=10.0.0.9 50000 10.0.0.20 22
@@NX-HW:cpu@@
Architecture:                       x86_64
CPU(s):                             16
Vendor ID:                          AuthenticAMD
Model name:                         AMD Ryzen 7 3700X 8-Core Processor
nproc=16
@@NX-HW:memory@@
MemTotal:       32768000 kB
MemAvailable:   20000000 kB
SwapTotal:       2097148 kB
@@NX-HW:disks@@
NAME="sda" TYPE="disk" SIZE="465.8G" MODEL="Samsung SSD 860" TRAN="sata"
@@NX-HW:rootfs@@
/dev/sda2 480000000 460000000 20000000  96% /
@@NX-HW:gpu@@
exit=9
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA driver is installed and running.
@@NX-HW:pci@@
0000:00:00.0|0x060000|0x1022|0x1480|
0000:01:00.0|0x030000|0x10de|0x2504|
0000:01:00.1|0x040300|0x10de|0x228e|snd_hda_intel
0000:03:00.0|0x020000|0x8086|0x15b8|e1000e
@@NX-HW:pci_names@@
@@NX-HW:usb@@
@@NX-HW:net@@
enp3s0|up|1|0x1003|1|00:d8:61:aa:bb:cc|e1000e|0|1000|/sys/devices/pci0000:00/0000:00:01.3/0000:03:00.0
@@NX-HW:addr@@
2: enp3s0    inet 10.0.0.20/24 brd 10.0.0.255 scope global enp3s0\\       valid_lft forever preferred_lft forever
@@NX-HW:route@@
@@NX-HW:dns@@
nameserver 10.0.0.1
@@NX-HW:video@@
@@NX-HW:serial@@
@@NX-HW:serial_ids@@
@@NX-HW:buses@@
@@NX-HW:iio@@
@@NX-HW:input@@
@@NX-HW:audio@@
@@NX-HW:power@@
BAT0|Battery|9|Discharging
@@NX-HW:kernel@@
source=unavailable
@@NX-HW:failed_units@@
@@NX-HW:end@@
'''

# Patterns seen on a real Jetson Orin NX robot with JetPack 6 (addresses and names replaced):
# nvidia-smi on Jetson, USB device-mode (gadget) interfaces, a spare unplugged Ethernet port,
# a microphone array whose second HID interface is held by an application, and failed services.
JETPACK6 = jetson().replace('exit=missing', 'exit=0\nOrin (nvgpu), 540.4.0, [N/A]').replace(
    'tegra=# R35 (release), REVISION: 4.1', 'tegra=# R36 (release), REVISION: 4.3').replace(
    'eth0|up|1|0x1003|1|48:b0:2d:11:22:33|r8168|0|1000|', 'eth0|up|1|0x1003|1|48:b0:2d:11:22:33|r8168|0|1000|').replace(
    'can0|down||0x80|280||mttcan|0||/sys/devices/platform/c310000.mttcan',
    'can0|down||0x80|280||mttcan|0||/sys/devices/platform/c310000.mttcan\n'
    'usb0|down|0|0x1303|1|aa:bb:cc:00:00:01||0||/sys/devices/platform/bus@0/3550000.usb/gadget\n'
    'eth1|down|0|0x1003|1|aa:bb:cc:00:00:02|r8152|0||/sys/devices/platform/3610000.xhci/usb2/2-1/2-1:1.0').replace(
    '1-1.2|046d|c21f|00|12|Logitech|Gamepad F710| ff:xpad',
    '1-1.2|046d|c21f|00|12|Logitech|Gamepad F710| ff:xpad\n1-1.4|ff00|0002|00|12|Generic|USB Mic Array| 01:snd-usb-audio 01:snd-usb-audio 03:usbhid 03:-\n'
    '2-1|0bda|8153|00|5000|Realtek|USB 10/100/1000 LAN| ff:r8152').replace(
    'nvargus-daemon.service loaded failed failed Argus daemon', 'robot-idle-start.service loaded failed failed robot idle startup')

CONTAINER = (Path(__file__).resolve().parent / 'fixtures' / 'hardware-scan-debian-container.txt').read_text()
