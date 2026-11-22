"""Issue #29 (R1-10): the rules a hardware repair follows when it installs and starts a motion driver.

What is let through, and what is still refused next to it:

* ROS 2 Humble's environment is loaded by the product (``ros2_env``), never by a ``source`` the
  assistant writes; a workspace built from source is loaded after it (``ros2_workspace``).
* ``cmd_vel`` and the other motion topics may be read; nothing may be published, called, sent as a
  goal, set as a parameter or switched in a lifecycle, however the command is spelled.
* The motion driver may be installed and started. The command that starts it is marked
  (``starts_motion_driver``) and waits for a person who says they are on site, even when the session
  runs without asking (the user's choice, 2026-11-22). The e-stop latch and the runtime that keeps it
  stay out of reach.
* A long build gets a limit of its own (``build_step``).

Everything runs against the local SSH fixture. Nothing reaches a real machine.
"""
import os
import queue
import subprocess
import tempfile
import unittest
from unittest import mock

from brainos import device_access, driver_repair, hardware_init, repair_policy
from test_hardware_init import SERIAL, Base

FIX = {'purpose': '安装并启动底盘的运控驱动', 'issue_ids': SERIAL, 'risk': 'medium', 'rollback': '卸载驱动包', 'needs_root': True,
       'timeout_seconds': 60}
MOTION = '不能发布运动或控制指令'
RUNTIME = '急停'
DRIVER_START = 'starts_motion_driver'


class RuleCase(Base):
    def setUp(self):
        super().setUp()
        self.s = self.session(self.scan())

    def request(self, command, **extra):
        return self.hw.fix_request(self.s, {**FIX, 'command': command, **extra}, self.connection)

    def refused(self, command, words='产品拒绝', **extra):
        with self.assertRaisesRegex(ValueError, words, msg=command):
            self.request(command, **extra)


class MotionRuleTests(RuleCase):
    """``cmd_vel`` may be read; nothing that moves or controls the robot may be sent (R1-10)."""

    def test_read_only_queries_of_the_motion_interfaces_are_allowed(self):
        for command in ('ros2 topic info /cmd_vel', 'ros2 topic echo --once /cmd_vel', 'ros2 topic list -t', 'ros2 topic hz /odom',
                        'ros2 topic type /cmd_vel', 'ros2 node list', 'ros2 node info /base_driver', 'ros2 service list -t',
                        'ros2 action list -t', 'ros2 action info /navigate_to_pose', 'ros2 interface show geometry_msgs/msg/Twist',
                        'ros2 pkg list', 'ros2 pkg prefix vendor_motion_driver', 'ros2 pkg executables vendor_motion_driver',
                        'ros2 param list /base_driver', 'ros2 param get /base_driver max_speed', 'ros2 lifecycle get /base_driver',
                        'ros2 control list_controllers', 'ros2 doctor --report', 'ros2 daemon status',
                        'grep -rn cmd_vel /opt/vendor/config', 'ros2 topic info /cmd_vel && ros2 topic info /odom'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command, ros2_env=True, needs_root=False)['command'], command)

    def test_publishing_calling_sending_goals_and_setting_parameters_are_still_refused(self):
        for command in ('ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.1}}"', 'ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist "{}"',
                        'ros2 topic pub -r 10 /go std_msgs/msg/Empty', 'ros2 action send_goal /navigate_to_pose nav2_msgs/action/NavigateToPose "{}"',
                        'ros2 service call /stand_up std_srvs/srv/Trigger', 'ros2 service call /estop/reset std_srvs/srv/Trigger',
                        'ros2 param set /base_driver max_speed 2.0', 'ros2 param load /base_driver fast.yaml', 'ros2 param delete /base_driver limit',
                        'ros2 lifecycle set /base_driver activate', 'ros2 control switch_controllers --activate diff_drive_controller',
                        'ros2 control set_controller_state diff_drive_controller active', 'ros2 bag play recorded_drive',
                        'rostopic pub /cmd_vel geometry_msgs/Twist "{}"', 'rosservice call /stand_up', 'rosparam set /max_speed 2'):
            with self.subTest(command=command):
                self.refused(command, MOTION, ros2_env=True)

    def test_other_spellings_of_the_same_are_refused_too(self):
        for command in ('/opt/ros/humble/bin/ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{}"', 'ros""2 topic pub /cmd_vel x/y',
                        "ros2 topic 'pub' /cmd_vel x/y", 'ros2 topic \\\npub /cmd_vel x/y', 'ros2  topic   pub /cmd_vel x/y',
                        'echo pub | xargs ros2 topic', 'xargs -a args.txt ros2', 'alias r=ros2', 'cd /tmp; ros2',
                        '/opt/ros/*/bin/r*2 topic pub /cmd_vel x/y', 'python3 -m ros2cli.cli topic pub /cmd_vel x/y',
                        'ros2 multicast send', 'ros2 pkg create vendor_hack', 'ros2 component load /container pkg plugin'):
            with self.subTest(command=command):
                self.refused(command, ros2_env=True)

    def test_words_hidden_by_variables_braces_or_wildcards_are_found(self):
        # Security review round 1: these passed the first version of the rules and ran unasked.
        for command in ('ros2${IFS}topic${IFS}pub${IFS}/cmd_vel${IFS}geometry_msgs/msg/Twist${IFS}{}',
                        '/opt/ros/humble/bin/ros? topic pu${E}b /cmd_vel geometry_msgs/msg/Twist {}', '{ros2,topic,pub,/cmd_vel,x/y}',
                        'ros[2] topic pub /cmd_vel x/y', 'ros2 topic pu$@b /cmd_vel x/y', 'ros2 service ca${X}ll /stand_up x/y'):
            with self.subTest(command=command):
                self.refused(command, MOTION, ros2_env=True)
        for command in ('ros2${IFS}launch${IFS}vendor_driver${IFS}base.launch.py', 'r*2 launch vendor_driver base.launch.py',
                        'roslaunch vendor_driver base.launch', 'rosrun vendor_driver base_node'):
            with self.subTest(command=command):
                self.refused(command, DRIVER_START, ros2_env=True)

    def test_frames_and_bytes_to_motor_buses_are_refused(self):
        # Below ROS 2: CAN frames and serial bytes can drive a motor controller directly.
        for command in ('cansend can0 123#0102030405060708', 'canplayer -I drive.log', 'cangen can0', "printf 'M1 100\\r' > /dev/ttyUSB0",
                        'echo go | tee /dev/ttyACM0', 'cat frames.bin > /dev/ttyTHS1', 'cp frames.bin /dev/ttyUSB0',
                        'echo go > /dev/serial/by-id/usb-motor-if00'):
            with self.subTest(command=command):
                self.refused(command, 'CAN')
        self.refused('dd if=frames.bin of=/dev/ttyUSB0')        # already refused by issue #2's rule on writing devices
        for command in ('ip link set can0 up type can bitrate 500000', 'candump -n 5 can0', 'stty -F /dev/ttyUSB0 115200', 'chmod 660 /dev/ttyUSB0',
                        'ls -l /dev/serial/by-id/', 'cat /dev/ttyUSB0 > /tmp/read.txt'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command)['command'], command)

    def test_package_names_and_paths_with_ros2_in_them_are_not_the_tool(self):
        for command in ('DEBIAN_FRONTEND=noninteractive apt-get install -y ros-humble-ros2-control', 'ls /home/robot/ros2_ws/src',
                        'systemctl status ros2-bridge.service --no-pager', 'dpkg -l ros-humble-ros2cli'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command)['command'], command)

    def test_teleoperation_is_refused_even_as_the_driver(self):
        for command in ('ros2 run teleop_twist_keyboard teleop_twist_keyboard', 'ros2 launch teleop_twist_joy teleop-launch.py'):
            with self.subTest(command=command):
                self.refused(command, MOTION, ros2_env=True, starts_motion_driver=True)

    def test_the_old_blanket_rule_is_gone_but_its_refusal_reads_the_same(self):
        self.refused('ros2 topic pub /cmd_vel geometry_msgs/Twist "{linear: {x: 0.5}}"', '修复命令不能发布运动或控制指令')
        self.assertEqual(self.request('cat /opt/vendor/config/cmd_vel_mux.yaml')['command'], 'cat /opt/vendor/config/cmd_vel_mux.yaml')


class SafetyLockTests(RuleCase):
    """The e-stop latch stays where it is: nothing reaches the runtime that keeps it (R1-10)."""

    def test_the_runtime_on_the_device_cannot_be_reset_driven_or_changed(self):
        for command in ('curl -X POST http://127.0.0.1:8790/v1/reset', 'curl -X POST http://127.0.0.1:8790/v2/reset',
                        'curl -X POST http://127.0.0.1:8790/v2/capabilities/base.velocity/invoke -d "{}"',
                        'curl -X POST http://127.0.0.1:8790/v2/runs -d "{}"', 'curl -X POST http://127.0.0.1:8790/v1/start',
                        'curl -X PUT http://127.0.0.1:8790/v2/safety/limits -d "{}"', 'wget -qO- --post-data= http://127.0.0.1:8790/v1/reset',
                        'neuxmind-runtime conformance --level motion', 'python3 -m neuxmind_runtime.cli serve',
                        '~/neuxmind-pkg/device_install.sh stop', 'rm ~/neuxmind/state/store.sqlite3', 'cat ~/neuxmind/edge-token',
                        'pkill -f neuxmind', 'killall neuxmind-runtime', 'vendor-estop --release', 'estop_tool reset'):
            with self.subTest(command=command):
                self.refused(command, RUNTIME)

    def test_other_ways_to_the_runtime_are_refused_too(self):
        # Security review round 1: the runtime's folder, token and process by other spellings, an escaped URL.
        for command in ('cd ~/neuxmind && cat edge-token', 'cat ~/neux*/edge-token', 'cd ~/neuxmind; rm -f state/latch.json',
                        'pkill -f venv/bin/neux', 'pkill -f edge-token', 'cat runtime.pid', 'curl -X POST http://127.0.0.1:8790/v%32/reset',
                        'neuxmind-runtime status', 'ls ~/neuxmind-pkg/library/../../neuxmind/state'):
            with self.subTest(command=command):
                self.refused(command, RUNTIME)

    def test_the_runtime_named_by_a_wildcard_is_refused(self):
        # Security review round 2.
        for command in ('rm -f ~/n?uxmind/state/latch', 'rm -f ~/ne[u]xmind/state/estop', 'mv ~/n*xmind/state /tmp/x', 'systemctl restart n*xmind',
                        'killall n?uxmind', 'cat ~/neuxmind/e*-token', 'ln -s ~/n?uxmind ~/neuxmind-pkg/r'):
            with self.subTest(command=command):
                self.refused(command, RUNTIME)
        for command in ('ls ~/neuxmind-pkg', 'du -sh ~/neuxmind-pkg', '[ -d ~/neuxmind-pkg ] && echo ready', 'ls /dev/ttyUSB*'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command, needs_root=False)['command'], command)

    def test_reading_the_runtimes_status_and_stopping_stay_possible(self):
        for command in ('curl -s http://127.0.0.1:8790/v1/status', 'curl -s -X POST http://127.0.0.1:8790/v1/stop',
                        'journalctl -u vendor-motion-driver -n 80 --no-pager', 'ls -l ~/neuxmind-pkg/library/',
                        'tar -xzf ~/neuxmind-pkg/library/0123/vendor_src.tar.gz -C ~/neuxmind-pkg/build/vendor_src'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command, needs_root=False)['command'], command)

    def test_the_files_the_product_loads_cannot_be_changed(self):
        # Security review round 1: an earlier command wrote the workspace's setup.sh, a later one loaded it.
        for command in ("printf 'ro' > /tmp/ws/install/setup.sh", "printf 's2 topic p' >> /tmp/ws/install/setup.sh",
                        'cp evil.sh /opt/ros/humble/setup.sh', 'tee -a /opt/ros/humble/setup.sh < extra',
                        'sed -i s/a/b/ /home/robot/ws/install/local_setup.bash', 'ln -sf /tmp/x /opt/ros/humble/setup.sh',
                        'rm /opt/ros/humble/local_setup.sh', 'cd /opt/ros/humble && cat x > setup.sh',
                        # security review round 2: other tools and other spellings of the path
                        'tar xf ~/neuxmind-pkg/library/a.tar -C /opt/ros/humble', 'unzip a.zip -d /opt/ros/humble',
                        'sed -Ei s/a/b/ /opt/ros/humble/share/x/local_setup.sh', 'sed --in-place s/a/b/ setup.sh', 'touch /opt/ros/humble/setup.sh',
                        'truncate -s0 /opt/ros/humble/setup.sh', 'awk -i inplace 1 /opt/ros/humble/setup.sh', 'mount --bind /tmp/x /opt/ros',
                        'cp x /opt//ros/humble/_local_setup_util_sh.py', 'cp x /opt/./ros/humble/x', 'cp x /opt/r*/humble/x',
                        'cd /opt/ros/humble && cp x _local_setup_util_sh.py', 'cd /opt/ros/humble && tar xf /tmp/a.tar', 'cd /opt && cp -r x ros',
                        # round 2 re-check
                        'cd /opt/ros/humble && git apply /tmp/p.diff', 'cd /opt/ros/humble; vi setup.sh', 'git -C /opt/ros/humble apply p'):
            with self.subTest(command=command):
                self.refused(command, 'setup.sh')
        self.assertEqual(self.request('cd /opt/ros/humble && ls share | head', needs_root=False)['command'], 'cd /opt/ros/humble && ls share | head')
        for command in ('ls -l /opt/ros/humble/setup.sh', 'grep -rl cmd_vel /opt/ros/humble/share > /tmp/found.txt'):
            with self.subTest(command=command):
                self.assertEqual(self.request(command, needs_root=False)['command'], command)


class EnvironmentTests(RuleCase):
    """The product loads ROS 2 Humble; the assistant never writes ``source`` itself."""

    def test_the_product_loads_ros2_humble_when_asked(self):
        request = self.request('ros2 pkg list', ros2_env=True, needs_root=False)
        self.assertEqual(request['command'], 'ros2 pkg list', 'the command is shown as the assistant wrote it')
        self.assertEqual(request['environment'], ['. ' + driver_repair.ROS2_SETUP])
        wrapped = driver_repair.wrap(request)
        self.assertTrue(wrapped.endswith('\nros2 pkg list'))
        self.assertIn('. /opt/ros/humble/setup.sh', wrapped)
        plain = self.request('lsmod', needs_root=False)
        self.assertEqual((plain['environment'], driver_repair.wrap(plain)), ([], 'lsmod'), 'nothing is added when not asked')

    def test_loading_the_environment_by_hand_is_still_refused(self):
        for command in ('source /opt/ros/humble/setup.bash && ros2 topic list', '. /opt/ros/humble/setup.sh; ros2 node list',
                        "bash -c 'source /opt/ros/humble/setup.bash; ros2 topic list'", 'sh -c ros2'):
            with self.subTest(command=command):
                self.refused(command, ros2_env=True)

    def test_a_workspace_built_from_source_is_loaded_after_humble(self):
        request = self.request('ros2 pkg prefix vendor_motion_driver', ros2_env=True, ros2_workspace='/home/robot/vendor_ws', needs_root=False)
        self.assertEqual(request['environment'], ['. /opt/ros/humble/setup.sh', ". '/home/robot/vendor_ws/install/setup.sh'"])
        self.assertEqual(request['ros2_workspace'], '/home/robot/vendor_ws')
        for bad in ('vendor_ws', '~/vendor_ws', '$HOME/vendor_ws', '/home/robot/../root/ws', '/home/robot/my ws', "/home/robot/ws'x",
                    '/home/robot/ws;reboot', '/home/robot/ws/', '/' + 'a' * 300, '/home/robot//ws', ''):
            with self.subTest(workspace=bad):
                self.refused('ros2 pkg list', '工作空间', ros2_env=True, ros2_workspace=bad)
        self.refused('ros2 pkg list', 'ros2_env', ros2_workspace='/home/robot/vendor_ws')

    def test_the_choices_must_be_yes_or_no(self):
        for field in ('ros2_env', 'starts_motion_driver', 'build_step'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.request('lsmod', **{field: 'yes'})

    def test_the_wrapped_command_runs_under_sh_with_the_environment_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = os.path.join(tmp, 'setup.sh')
            with open(setup, 'w') as f:
                f.write('export NX_ROS_LOADED=humble\n')
            request = self.request('echo "loaded=$NX_ROS_LOADED"', ros2_env=True, needs_root=False)
            with mock.patch.object(driver_repair, 'ROS2_SETUP', setup):
                wrapped = driver_repair.wrap({**request, 'environment': driver_repair.environment_lines(True, None)})
            done = subprocess.run(hardware_init._quote_root(wrapped, 'none', 10), shell=True, capture_output=True, timeout=20)
            self.assertEqual((done.returncode, done.stdout), (0, b'loaded=humble\n'))
            os.remove(setup)
            done = subprocess.run(hardware_init._quote_root(wrapped, 'none', 10), shell=True, capture_output=True, timeout=20)
            self.assertEqual(done.returncode, 127, 'a device without Humble says so instead of running the command')
            self.assertIn('没有找到'.encode(), done.stderr)

    def test_the_approved_fix_reaches_the_robot_with_the_environment_in_front(self):
        request = self.request('ros2 pkg list', ros2_env=True)
        device = device_access.SSHDevice(self.connection, {'password': 'fixture-password'})
        try:
            self.hw.tool(self.s['id'], self.s, 'device_fix', request, device, approved=True)
        finally:
            device.close()
        sent = [c for c in self.ssh.commands if 'ros2 pkg list' in c]
        self.assertEqual(len(sent), 1)
        self.assertIn('. /opt/ros/humble/setup.sh', sent[0])

    def test_device_exec_in_a_repair_session_can_have_the_environment_too(self):
        request = hardware_init.exec_request(self.s, {'command': 'ros2 topic info /cmd_vel', 'purpose': '看底盘话题', 'timeout_seconds': 20,
                                                      'ros2_env': True})
        self.assertEqual(request['environment'], ['. /opt/ros/humble/setup.sh'])
        self.assertTrue(driver_repair.exec_command(request).startswith('sh -c '))
        for command in ('ros2 launch vendor_motion_driver bringup.launch.py', 'ros2 run vendor_motion_driver base_node',
                        'ros2 topic pub /cmd_vel x/y'):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, '产品拒绝'):
                hardware_init.exec_request(self.s, {'command': command, 'purpose': '试试', 'timeout_seconds': 20, 'ros2_env': True})
        discovery = hardware_init.exec_request({'mode': 'discovery'}, {'command': 'lsmod', 'purpose': '看', 'timeout_seconds': 20})
        self.assertNotIn('environment', discovery, 'investigations keep their own request shape')

    def test_the_tool_descriptions_offer_the_new_fields(self):
        tools = {t['name']: t for t in hardware_init.session_tools(self.s, [{'name': 'device_exec', 'inputSchema': {
            'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command'], 'additionalProperties': False}}])}
        fix = tools['device_fix']['inputSchema']
        for field in ('ros2_env', 'ros2_workspace', 'starts_motion_driver', 'build_step'):
            self.assertIn(field, fix['properties'])
        self.assertIn('starts_motion_driver', fix['required'])
        self.assertEqual(fix['properties']['timeout_seconds']['maximum'], driver_repair.BUILD_TIMEOUT_SECONDS)
        self.assertIn('ros2_env', tools['device_exec']['inputSchema']['properties'])
        self.assertIn('driver_packages', tools)
        self.assertIn('driver_package_deliver', tools)


class BuildTimeTests(RuleCase):
    """A long build or install has a limit of its own; anything else keeps 600 seconds."""

    def test_a_build_may_run_for_up_to_an_hour(self):
        for command in ('colcon build --symlink-install', 'make -j4', 'DEBIAN_FRONTEND=noninteractive apt-get install -y ros-humble-ros2-control',
                        'dpkg -i /home/robot/neuxmind-pkg/library/0123/vendor.deb', 'rosdep install --from-paths src -y --ignore-src'):
            with self.subTest(command=command):
                request = self.request(command, ros2_env=True, build_step=True, timeout_seconds=driver_repair.BUILD_TIMEOUT_SECONDS)
                self.assertEqual((request['timeout_seconds'], request['build_step']), (3600, True))
        self.assertIn('timeout -k 5 3600 sh -c', hardware_init._quote_root(driver_repair.wrap(request), 'sudo_password', request['timeout_seconds']))

    def test_other_commands_keep_the_old_limit(self):
        self.refused('colcon build', '600', timeout_seconds=601, ros2_env=True)
        self.refused('sleep 3000', '编译', build_step=True, timeout_seconds=3000)
        self.refused('colcon build', '3600', build_step=True, timeout_seconds=3601, ros2_env=True)
        self.assertEqual(self.request('colcon build', ros2_env=True, timeout_seconds=600)['timeout_seconds'], 600)

    def test_a_build_word_somewhere_in_the_command_is_not_enough(self):
        # Security review round 1: these got 3600 seconds in the first version.
        for command in ('echo make; sleep 3500', 'sleep 3500 # make', 'ros2 topic echo /make', 'make $(sleep 3500)', 'cd /tmp && sleep 3500',
                        'make && sleep 3500', 'apt-get install -y x\nsleep 3500'):
            with self.subTest(command=command):
                self.refused(command, '编译', ros2_env=True, build_step=True, timeout_seconds=3600)
        self.refused('ros2 launch vendor_motion_driver bringup.launch.py', '编译', ros2_env=True, build_step=True, timeout_seconds=3600,
                     starts_motion_driver=True)
        for command in ('make.sh', 'make-evil all', 'cmake-gui', 'ninja-evil'):      # security review round 2
            with self.subTest(command=command):
                self.refused(command, '编译', ros2_env=True, build_step=True, timeout_seconds=3600)
        for command in ('cd /home/robot/neuxmind-pkg/build/vendor_src && colcon build --symlink-install', 'MAKEFLAGS=-j4 make',
                        'mkdir -p build && cd build && cmake .. && make -j4', 'colcon build 2>&1 | tail -n 50', 'make -j4 > build.log 2>&1',
                        'colcon build &> build.log'):
            with self.subTest(command=command):
                self.assertTrue(self.request(command, ros2_env=True, build_step=True, timeout_seconds=3600)['build_step'])

    def test_ten_lines_are_still_the_most(self):
        self.refused('\n'.join(['true'] * 12), '10 行', build_step=True, timeout_seconds=1200)


class DriverStartTests(RuleCase):
    """Starting the motion driver is allowed, and is always marked as such."""

    def test_starting_ros2_nodes_needs_the_mark(self):
        for command in ('ros2 launch vendor_motion_driver bringup.launch.py',
                        'nohup ros2 launch vendor_motion_driver bringup.launch.py > /home/robot/neuxmind-pkg/driver.log 2>&1 &',
                        'ros2 run vendor_motion_driver base_node'):
            with self.subTest(command=command):
                self.refused(command, DRIVER_START, ros2_env=True)
                request = self.request(command, ros2_env=True, starts_motion_driver=True)
                self.assertTrue(request['starts_motion_driver'])
                self.assertTrue(request['nonce'])

    def test_a_service_that_looks_like_the_driver_needs_the_mark(self):
        for command in ('systemctl start vendor-motion-driver', 'systemctl restart legged-bringup.service', 'systemctl enable --now ros-driver',
                        'systemctl --now enable chassis_base', 'service quadruped-locomotion start', 'systemctl try-restart base_controller',
                        'systemctl start vendor-base.service', 'systemctl start robot-base', 'systemctl enable --now vendor-sdk',
                        'service robot-base start', 'systemctl restart servo_bus', '/etc/init.d/robot-base start', 'supervisorctl start motion_driver',
                        'pm2 start base-driver'):
            with self.subTest(command=command):
                self.refused(command, DRIVER_START)
                self.assertTrue(self.request(command, starts_motion_driver=True)['starts_motion_driver'])

    def test_other_services_follow_the_usual_rules(self):
        # The user's choice: starting a service is not held back as such, only the motion driver's start is marked.
        for command in ('systemctl restart nvargus-daemon', 'systemctl start bluetooth', 'systemctl status vendor-motion-driver --no-pager',
                        'systemctl stop vendor-motion-driver', 'journalctl -u ros-driver -n 50 --no-pager'):
            with self.subTest(command=command):
                self.assertFalse(self.request(command)['starts_motion_driver'])

    def test_marking_a_command_does_not_loosen_any_other_rule(self):
        for command in ('reboot', 'ros2 topic pub /cmd_vel x/y', 'curl -X POST http://127.0.0.1:8790/v1/reset', 'sudo systemctl start vendor-motion-driver'):
            with self.subTest(command=command):
                self.refused(command, starts_motion_driver=True)

    def test_the_approval_shows_what_the_robot_may_do(self):
        request = self.request('systemctl start vendor-motion-driver', starts_motion_driver=True)
        self.assertTrue(any('上电' in w or '站立' in w for w in request['warnings']), request['warnings'])


class OnsiteCase(RuleCase):
    def setUp(self):
        super().setUp()
        self.answers = queue.Queue()
        self.e.live[self.s['id']] = {'answers': self.answers}

    def tearDown(self):
        self.e.live.pop(self.s['id'], None)        # a stand-in, not a running session: nothing to stop
        super().tearDown()

    def set_auto(self, enabled):
        hardware = self.store.get('agent_sessions', self.s['id'])['hardware']
        self.e.update(self.s['id'], hardware={**hardware, 'auto_approve': enabled})

    def queue(self, request, request_id=7):
        self.e._queue_request(self.s['id'], 'approval', {'id': request_id, 'method': 'device_fix/requestApproval', 'request': request})
        self.hw.policy.answer_waiting(self.s['id'])
        return self.store.get('agent_sessions', self.s['id'])

    def start(self):
        return self.request('systemctl start vendor-motion-driver', starts_motion_driver=True)

    def confirm(self, request_id=7, **extra):
        return self.hw.driver.confirm_onsite({'project_id': self.pid, 'session_id': self.s['id'], 'approval_id': request_id, 'onsite': True, **extra})


class OnsiteTests(OnsiteCase):
    """The start of the motion driver waits for a person on site, every time (the user's choice, 2026-11-22)."""

    def test_with_auto_approve_on_the_start_still_waits_for_a_person(self):
        self.set_auto(True)
        s = self.queue(self.start())
        self.assertTrue(self.answers.empty(), 'ran without anybody on site')
        self.assertEqual((s['status'], s['pending_approval']['id']), ('waiting_input', 7))
        self.assertIn('现场', s['hardware']['auto_hold']['reason'])
        self.hw.policy.set_auto({'project_id': self.pid, 'session_id': self.s['id'], 'enabled': True})
        self.assertTrue(self.answers.empty(), 'switching on again does not wave it through')

    def test_other_commands_still_run_unasked(self):
        self.set_auto(True)
        self.queue(self.request('systemctl restart nvargus-daemon'))
        self.assertEqual(self.answers.get_nowait(), (7, {'decision': 'accept'}))

    def test_approving_the_start_needs_the_on_site_confirmation(self):
        self.queue(self.start())
        with self.assertRaisesRegex(ValueError, '现场'):
            self.e.approve({'session_id': self.s['id'], 'approval_id': 7, 'decision': 'accept'})
        self.assertTrue(self.answers.empty())
        self.confirm()
        self.e.approve({'session_id': self.s['id'], 'approval_id': 7, 'decision': 'accept'})
        self.assertEqual(self.answers.get_nowait(), (7, {'decision': 'accept'}))
        s = self.store.get('agent_sessions', self.s['id'])
        confirmed = s['hardware']['onsite_confirmations'][-1]
        self.assertEqual((confirmed['approval_id'], confirmed['command']), (7, 'systemctl start vendor-motion-driver'))
        self.assertTrue(confirmed['confirmed_at'])
        self.assertTrue(any('现场' in e['message'] for e in s['events']))
        events = [e for e in self.store.list('events') if e['action'] == 'device.motion_driver_onsite_confirmed']
        self.assertEqual(events[0]['details']['session_id'], self.s['id'])

    def test_declining_needs_no_confirmation(self):
        self.queue(self.start())
        self.e.approve({'session_id': self.s['id'], 'approval_id': 7, 'decision': 'decline'})
        self.assertEqual(self.answers.get_nowait(), (7, {'decision': 'decline'}))

    def test_a_confirmation_is_for_that_one_command(self):
        self.queue(self.start())
        self.confirm()
        self.e.approve({'session_id': self.s['id'], 'approval_id': 7, 'decision': 'accept'})
        self.answers.get_nowait()
        self.queue(self.start(), request_id=8)
        with self.assertRaisesRegex(ValueError, '现场'):
            self.e.approve({'session_id': self.s['id'], 'approval_id': 8, 'decision': 'accept'})

    def test_only_a_pending_driver_start_can_be_confirmed(self):
        with self.assertRaisesRegex(ValueError, '失效'):
            self.confirm()
        self.queue(self.request('modprobe uvcvideo'))
        with self.assertRaisesRegex(ValueError, '不是启动运控驱动'):
            self.confirm()
        self.store.put('agent_sessions', {**self.store.get('agent_sessions', self.s['id']), 'pending_approval': None, 'pending_requests': []})
        self.queue(self.start(), request_id=9)
        for request_id, extra in ((7, {}), ('9', {}), (9.0, {}), (True, {}), (9, {'onsite': False}), (9, {'onsite': 'yes'}),
                                  (9, {'project_id': 'project-other'})):
            with self.subTest(request_id=request_id, extra=extra), self.assertRaises(ValueError):
                self.confirm(request_id, **extra)
        self.confirm(9)

    def test_the_product_does_not_run_a_start_nobody_confirmed(self):
        request = self.start()
        sent = len(self.ssh.commands)
        device = device_access.SSHDevice(self.connection, {'password': 'fixture-password'})
        try:
            with self.assertRaisesRegex(ValueError, '现场'):
                self.hw.tool(self.s['id'], self.s, 'device_fix', request, device, approved=True)
        finally:
            device.close()
        self.assertEqual(self.ssh.commands[sent:], [], 'nothing reached the robot')

    def test_the_hold_is_said_in_the_policy_words(self):
        self.assertIn('现场', repair_policy.ask_first({'command': 'systemctl start vendor-motion-driver', 'starts_motion_driver': True}))
        self.assertEqual(repair_policy.ask_first({'command': 'systemctl start vendor-motion-driver'}), '')


class AutoHoldTests(unittest.TestCase):
    """With 默认允许执行 on, what the product cannot judge from the text waits for a person (security review round 1)."""

    def test_what_hides_the_program_it_runs_waits(self):
        for command in ('python3 - <<EOF\nimport urllib.request\nurllib.request.urlopen("http://127.0.0.1:8790/v2/"+"reset")\nEOF',
                        'bash -s < steps.txt', 'cat steps | python3 -', 'python3 -m ros2cli.cli launch vendor_driver base.launch.py',
                        'ro?2 topic list', 'printf x | xargs r*2', '{ros2,topic,list}', 'echo $IFS', 'nohup ${R} launch x y &',
                        '/opt/vendor/lib/vendor_driver/base_node', 'nohup /opt/vendor/lib/vendor_driver/base_node &',
                        '/home/robot/ws/install/vendor/lib/vendor/base_node', 'docker start vendor_base', 'podman run vendor/driver',
                        # security review round 2
                        'python3 -W ignore -c "import rclpy"', 'python3 -X dev -m foo', 'python3 -mfoo', 'python3 -Bm foo', 'perl -E 1', 'php -r 1',
                        'node -p 1', '/usr/bin/python3 -c 1', 'cd ws && install/lib/drv/node', 'cd ws && build/drv/node', 'nohup bin/x &',
                        'exec bin/x', 'env -C ws bin/x', 'bash -e ~/neuxmind-pkg/run.sh', 'bash -x /opt/x.sh', 'sh -e ~/x.sh',
                        'systemd-run /usr/bin/x', 'setsid -f x', 'pkill -f n.uxmind', "pkill -f 'n[e]uxmind'", 'kill 1234', 'rm -f ~/*/state/x',
                        'docker -H x run y', 'docker container run x', 'lxc exec c -- x', 'chroot /x', 'nsenter -t 1 -m',
                        'apt-get -o DPkg::Post-Invoke::=/opt/robot/start install -y foo', '/etc/init.d/vendor start', 'supervisorctl start x',
                        # round 2 re-check
                        'echo hi\nbin/driver', 'echo hi\npython3 -W ignore -c x', 'if true; then python3 -W ignore -c x; fi',
                        'for i in 1; do bin/driver; done', '! bin/driver', '{ bin/driver; }', '/bin/sh -e x.sh', '/usr/bin/../../opt/robot/drv',
                        '/bin/../home/u/ws/install/lib/p/n', '/usr/local/bin/vendor_driver', '/usr/lib/vendor/drv_node', 'PATH=bin driver',
                        'export PATH=bin; driver', 'LD_PRELOAD=/home/robot/neuxmind-pkg/x.so true', 'PYTHONSTARTUP=x python3 --version',
                        'pypy3 -c x', "awk 'BEGIN{system(1)}'", 'gawk -f x.awk', 'expect -c x', 'fish x', 'tclsh x', 'csh x',
                        'sed -i s/1/0/ ~/*/state/estop.json', 'touch ~/*/state/x', 'shred ~/*/state/x', 'dd of=~/*/state/x', 'tar xf x -C ~/*/state',
                        'busybox x', 'doas x', 'ssh localhost x', 'gdb x', 'strace x', 'ltrace x', 'perf record x', 'valgrind x', 'cat /proc/*/environ',
                        'cat /proc/1234/environ'):
            with self.subTest(command=command):
                self.assertTrue(repair_policy.ask_first({'command': command}), 'ran unasked')

    def test_loading_a_workspace_waits(self):
        self.assertIn('工作空间', repair_policy.ask_first({'command': 'ros2 pkg list', 'ros2_env': True, 'ros2_workspace': '/home/robot/ws'}))
        self.assertEqual(repair_policy.ask_first({'command': 'ros2 pkg list', 'ros2_env': True, 'ros2_workspace': None}), '')

    def test_the_steps_of_a_driver_install_still_run_unasked(self):
        for command in ("DEBIAN_FRONTEND=noninteractive apt-get install -y '/home/robot/neuxmind-pkg/library/0123/vendor.deb'",
                        'cd /home/robot/neuxmind-pkg/build/vendor_src && colcon build --symlink-install', 'usermod -aG dialout $USER',
                        'ls /dev/ttyUSB*', 'python3 -m pip install pyserial', 'ros2 topic info /cmd_vel', 'systemctl restart nvargus-daemon',
                        "sha256sum '/home/robot/neuxmind-pkg/library/0123/vendor.deb'", '[ -d /opt/ros/humble ] && echo yes',
                        'cat /opt/ros/humble/setup.sh | head -n 5', 'python3 --version', 'colcon build 2>&1 | tail -n 50',
                        'tar -xzf /home/robot/neuxmind-pkg/library/0123/vendor_src.tar.gz -C /home/robot/neuxmind-pkg/build/vendor_src'):
            with self.subTest(command=command):
                self.assertEqual(repair_policy.ask_first({'command': command}), '')


class SkillTests(unittest.TestCase):
    def test_the_skill_allows_installing_and_starting_the_driver_within_the_rules(self):
        files = hardware_init.load_skill()
        skill, playbooks = files['SKILL.md'], files['references/playbooks.md']
        section = playbooks.split('## motion-driver:')[1].split('\n## ')[0]
        for said in ('driver_packages', 'driver_package_deliver', 'ros2_env', 'starts_motion_driver', 'build_step', 'hardware_rescan',
                     'needs_user', 'fixed', '现场'):
            self.assertIn(said, section)
        for said in ('starts_motion_driver', 'ros2_env', 'driver_packages', '不发布运动或控制指令', '急停'):
            self.assertIn(said, skill)
        self.assertNotIn('不启动会驱动电机的程序', skill)
        self.assertEqual(hardware_init.skill_info()['version'], '1.3.0', 'the Skill text changed: so does its version')

    def test_the_session_instructions_keep_the_motion_rule(self):
        text = hardware_init.REPAIR_INSTRUCTIONS
        self.assertIn('发布运动或控制指令', text)
        self.assertIn('starts_motion_driver', text)


if __name__ == '__main__':
    unittest.main()
