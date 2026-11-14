"""Outputs of ``device_software.SOFTWARE_COMMAND`` for the live status tests (issue #8).

Shaped like what the command prints on real machines: the services section follows a capture
from a Jetson robot, the process sections one from a ROS 2 container (names and addresses
replaced). ``Process`` rows build the three process sections so that their numbers agree.
"""
import os
from collections import namedtuple

# pid, parent, user, resident KiB, comm (15 characters at most), state, (utime, stime) ticks at the
# first and second sample (None: not there at that sample), start time in ticks since boot, command line
Process = namedtuple('Process', 'pid ppid user rss comm state first second start args')

TICK = 100
UPTIME = (191072.56, 191073.56)          # one second between the two samples
# A command line with a credential in it. Made up on every run; it must never reach the status.
LEAKY_ARGUMENT = 'k' + os.urandom(8).hex()

ROS2_ROBOT = [
    Process(1, 0, 'root', 12224, 'systemd', 'S', (23011, 27202), (23011, 27202), 0, '/sbin/init splash'),
    Process(2, 0, 'root', 0, 'kthreadd', 'S', (0, 5), (0, 5), 0, '[kthreadd]'),
    Process(37, 2, 'root', 0, 'kworker/3:1H-kblockd', 'I', (0, 900), (0, 990), 7, '[kworker/3:1H-kblockd]'),
    Process(812, 1, 'root', 61200, 'dockerd', 'S', (5000, 3000), (5002, 3001), 1500,
            '/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock'),
    Process(1500, 1, 'robot', 48200, 'ros2', 'S', (900, 200), (900, 200), 12000,
            '/usr/bin/python3 /opt/ros/humble/bin/ros2 launch nav_bringup robot.launch.py use_sim:=false'),
    Process(1510, 1500, 'robot', 91200, 'lidar_driver_no', 'S', (40000, 9000), (40040, 9010), 12100,
            '/opt/robot_ws/install/lidar_driver/lib/lidar_driver/lidar_driver_node --ros-args -r __node:=lidar '
            '-r __ns:=/sensors --params-file /tmp/launch_params_ab12'),
    Process(1511, 1500, 'robot', 143000, 'component_conta', 'R', (80000, 1000), (80101, 1000), 12100,
            '/opt/ros/humble/lib/rclcpp_components/component_container --ros-args -r __node:=nav_container'),
    Process(1512, 1500, 'robot', 30100, 'static_transfor', 'S', (10, 5), (10, 5), 12150,
            '/opt/ros/humble/lib/tf2_ros/static_transform_publisher --frame-id base --child-frame-id lidar'),
    Process(1520, 1, 'robot', 22800, 'ros2', 'S', (30, 10), (30, 10), 13000,
            '/usr/bin/python3 /opt/ros/humble/bin/ros2 run chassis_bridge bridge_node'),
    Process(1521, 1520, 'robot', 35600, 'bridge_node', 'S', (700, 300), (705, 300), 13050,
            '/opt/robot_ws/install/chassis_bridge/lib/chassis_bridge/bridge_node'),
    Process(1530, 1, 'robot', 20400, 'python3', 'S', (50, 20), (50, 20), 13100,
            '/usr/bin/python3 -c from ros2cli.daemon.daemonize import main; main() --name ros2-daemon '
            '--ros-domain-id 0 --rmw-implementation rmw_fastrtps_cpp'),
    Process(1540, 1, 'robot', 18100, 'ros2', 'S', (20, 5), (20, 5), 13200,
            '/usr/bin/python3 /opt/ros/humble/bin/ros2 bag record -a -o /data/bags/run-7'),
    Process(1600, 1, 'root', 5200, 'MicroXRCEAgent', 'S', (100, 100), (100, 100), 9000,
            '/usr/local/bin/MicroXRCEAgent serial --dev /dev/ttyUSB0 -b 921600'),
    Process(1700, 1, 'robot', 250000, 'python3', 'R', (100, 0), (120, 0), 15000,
            'python3 /home/robot/tools/uploader.py --api-key=' + LEAKY_ARGUMENT),
    Process(1800, 1, 'robot', 0, 'old_tool', 'Z', (5, 5), (5, 5), 16000, '[old_tool] <defunct>'),
    Process(1900, 1, 'robot', 3000, 'sleep', 'T', (0, 0), (0, 0), 17000, 'sleep 3600'),
    Process(748, 1, 'message+', 6240, 'dbus-daemon', 'S', (300, 100), (300, 100), 900,
            '@dbus-daemon --system --address=systemd: --nofork --nopidfile --systemd-activation --syslog-only'),
    # The command itself: its shell is there throughout, its ps has gone by the second sample.
    Process(2990, 2980, 'robot', 1400, 'sh', 'S', (0, 0), (0, 1), 19107200,
            "sh -c export LC_ALL=C T=; command -v timeout >/dev/null 2>&1 && T='timeout 6' echo '@@NX-SW:clock@@'"),
    Process(3001, 2990, 'robot', 2452, 'ps', 'R', None, None, 19107350, 'ps -eo pid=,ppid=,user=,rss=,comm='),
]

ROS1_ROBOT = [
    Process(1, 0, 'root', 9000, 'systemd', 'S', (10, 10), (10, 10), 0, '/sbin/init'),
    Process(2000, 1, 'robot', 30000, 'roscore', 'S', (10, 10), (10, 10), 5000, '/usr/bin/python3 /opt/ros/noetic/bin/roscore'),
    Process(2001, 2000, 'robot', 29000, 'rosmaster', 'S', (10, 10), (10, 10), 5010,
            '/usr/bin/python3 /opt/ros/noetic/bin/rosmaster --core -p 11311 -w 3 __log:=/home/robot/.ros/log/master.log'),
    Process(2002, 2000, 'robot', 8000, 'rosout', 'S', (10, 10), (10, 10), 5020,
            '/opt/ros/noetic/lib/rosout/rosout __name:=rosout __log:=/home/robot/.ros/log/rosout-1.log'),
    Process(2010, 1, 'robot', 28000, 'roslaunch', 'S', (10, 10), (10, 10), 6000,
            '/usr/bin/python3 /opt/ros/noetic/bin/roslaunch chassis_bringup base.launch port:=/dev/ttyUSB0'),
    Process(2011, 2010, 'robot', 41000, 'base_driver', 'S', (10, 10), (10, 10), 6010,
            '/opt/robot_ws/devel/lib/chassis_driver/base_driver __name:=base_driver __log:=/home/robot/.ros/log/base_driver-2.log'),
    Process(2012, 2010, 'robot', 52000, 'nodelet', 'S', (10, 10), (10, 10), 6020,
            '/opt/ros/noetic/lib/nodelet/nodelet manager __name:=camera_manager __log:=/home/robot/.ros/log/camera_manager-3.log'),
    Process(2020, 1, 'root', 7000, 'zenohd', 'S', (10, 10), (10, 10), 7000, '/usr/bin/zenohd -c /etc/zenoh/config.json5'),
    Process(2021, 1, 'mosquitto', 6000, 'mosquitto', 'S', (10, 10), (10, 10), 7100,
            '/usr/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf'),
]

SERVICES = '''accounts-daemon.service        loaded active     running      Accounts Service
chrony.service                 loaded active     running      chrony, an NTP client/server
docker.service                 loaded active     running      Docker Application Container Engine
map-upload.service             loaded activating auto-restart Upload finished maps (keeps restarting)
nav-stack.service              loaded active     running      Navigation stack
robot-idle-start.service       loaded failed     failed       Robot measured idle startup, without a navigation mission
serial-getty@ttyTCU0.service   loaded active     running      Serial Getty on ttyTCU0
ssh.service                    loaded active     running      OpenBSD Secure Shell server
systemd-journald.service       loaded active     running      Journal Service
wifi-autoconnect.service       loaded active     running      Keep "Wi-Fi connection 1" up (headless box: NetworkManager blocks autoconnect)
'''

CONTAINERS = '''exit=0
nav-bridge|registry.example.com/robot/nav-bridge:1.4|Up 3 hours (healthy)
perception|perception:2.1|Up 3 hours (unhealthy)
map-server|map-server:2.1|Exited (1) 2 days ago
recorder|recorder:latest|Restarting (127) 41 seconds ago
calibration|calibration:0.3|Exited (0) 5 weeks ago
staging|staging:dev|Created
'''
# What the docker client says when the SSH account is not in the docker group (seen on a real robot).
CONTAINERS_DENIED = 'exit=1\npermission denied while trying to connect to the docker API at unix:///var/run/docker.sock\n'
CONTAINERS_STOPPED = ('exit=1\nCannot connect to the Docker daemon at unix:///var/run/docker.sock. '
                      'Is the docker daemon running?\n')


def _sample(processes, index):
    rows = [f'{p.pid} {p.state} {p[6 + index][0]} {p[6 + index][1]} {p.start}' for p in processes if p[6 + index]]
    return f'{UPTIME[index]:.2f} 1453015.30\n' + '\n'.join(rows) + '\n'


def software(processes=ROS2_ROBOT, services=SERVICES, containers=CONTAINERS, systemd=True, end=True, cores=8):
    names = ''.join(f'{p.pid:>7} {p.ppid:>7} {p.user:<8} {p.rss:>6} {p.comm}\n' for p in processes)
    arguments = ''.join(f'{p.pid:>7} {p.args}\n' for p in processes)
    return (f'@@NX-SW:clock@@\ntick={TICK}\ncores={cores}\n'
            '@@NX-SW:sample_a@@\n' + _sample(processes, 0) +
            '@@NX-SW:services@@\n' + services +
            '@@NX-SW:systemd@@\n' + ('ok\n' if systemd else '') +
            '@@NX-SW:containers@@\n' + containers +
            '@@NX-SW:processes@@\n' + names +
            '@@NX-SW:arguments@@\n' + arguments +
            '@@NX-SW:sample_b@@\n' + _sample(processes, 1) +
            ('@@NX-SW:end@@\n' if end else ''))
