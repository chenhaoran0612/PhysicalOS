"""Live status of a real machine (issue #8): every piece of hardware with its driver and working
state, the software that is running, read once a minute.

Parsing is tested on captured-shape outputs; the reads run against local SSH protocol fixtures.
Nothing here reaches a real machine.
"""
import json
import re
import time
import unittest
from unittest import mock

from brainos import device_software, device_status, hardware_scan
from hardware_fixture import JETPACK6, jetson
from software_fixture import (CONTAINERS_DENIED, CONTAINERS_STOPPED, LEAKY_ARGUMENT, ROS1_ROBOT, ROS2_ROBOT, Process,
                              software)
from test_ssh_jump import SERVICES, STATUS_OUTPUT, SshCase


def by(rows, key='name'):
    return {row[key]: row for row in rows}


class SoftwareCommandTests(unittest.TestCase):
    def test_command_only_reads(self):
        command = device_software.SOFTWARE_COMMAND
        for word in ('sudo', 'modprobe', 'apt', 'pip', 'tee', 'rm ', 'mv ', 'cp ', 'chmod', 'chown', 'kill', 'systemctl restart',
                     'systemctl start', 'systemctl stop', 'docker run', 'docker exec', 'docker start', 'docker stop', 'docker rm',
                     'ros2', 'rostopic', 'reboot', 'shutdown', 'source ', 'eval'):
            self.assertNotIn(word, command, word)
        # Output redirections go only to /dev/null; nothing is written on the device.
        for match in re.finditer(r'(\d?)>(?!&)\s*([^\s;)"|&]+)', command):
            self.assertEqual(match.group(2), '/dev/null', match.group(0))
        self.assertNotRegex(command, r'(^|\s)\.\s+/', 'never sources a file from the device')

    def test_tools_that_can_hang_run_under_a_time_limit(self):
        # A wedged systemd or docker daemon must not leave one stuck process behind on the robot for every read.
        lines = [line for line in device_software.SOFTWARE_COMMAND.splitlines() if re.search(r'\b(systemctl|docker ps)\b', line)]

        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r'\$T (systemctl|docker) ', line)
        limit = int(re.search(r"T='timeout (\d+)'", device_software.SOFTWARE_COMMAND).group(1))
        self.assertLess(3 * limit + 1, device_software.TIMEOUT_SECONDS)      # all three stuck, plus the second between the samples

    def test_command_is_a_constant_run_under_sh(self):
        self.assertTrue(device_software.SOFTWARE_EXEC.startswith('sh -c '))
        for section in ('clock', 'sample_a', 'services', 'systemd', 'containers', 'processes', 'arguments', 'sample_b', 'end'):
            self.assertIn(f"echo '@@NX-SW:{section}@@'", device_software.SOFTWARE_COMMAND)


class SoftwareParsingTests(unittest.TestCase):
    def setUp(self):
        self.software = device_software.parse(software())

    def test_services_that_run_fail_or_keep_restarting(self):
        services = by(self.software['services'])

        self.assertEqual(len(services), 10)
        self.assertEqual(services['nav-stack.service'],
                         {'name': 'nav-stack.service', 'description': 'Navigation stack', 'state': 'running', 'watched': False})
        self.assertEqual(services['robot-idle-start.service']['state'], 'failed')
        self.assertEqual(services['map-upload.service']['state'], 'restarting')
        self.assertIn('headless box', services['wifi-autoconnect.service']['description'])   # punctuation survives

    def test_a_device_without_systemd_reports_unknown_not_an_empty_list(self):
        parsed = device_software.parse(software(services='', systemd=False))

        self.assertIsNone(parsed['services'])

    def test_containers_with_their_state(self):
        containers = self.software['containers']

        self.assertEqual(containers['state'], 'ok')
        rows = by(containers['items'])
        self.assertEqual(rows['nav-bridge'], {'name': 'nav-bridge', 'image': 'registry.example.com/robot/nav-bridge:1.4',
                                              'state': 'running', 'status': 'Up 3 hours (healthy)'})
        self.assertEqual(rows['perception']['state'], 'unhealthy')
        self.assertEqual(rows['map-server']['state'], 'exited_error')
        self.assertEqual(rows['calibration']['state'], 'exited')
        self.assertEqual(rows['recorder']['state'], 'restarting')
        self.assertEqual(rows['staging']['state'], 'created')

    def test_docker_that_cannot_be_read_is_not_reported_as_no_containers(self):
        for output, state in ((CONTAINERS_DENIED, 'denied'), (CONTAINERS_STOPPED, 'stopped'), ('exit=missing\n', 'missing'),
                              ('exit=124\n', 'timeout'), ('exit=2\nsomething else went wrong\n', 'failed'), ('', 'failed')):
            with self.subTest(state=state):
                containers = device_software.parse(software(containers=output))['containers']

                self.assertEqual(containers['state'], state)
                self.assertEqual(containers['items'], [])

    def test_ros2_nodes_launches_and_agents_are_recognised_from_the_process_list(self):
        middleware = by(self.software['middleware'], 'pid')

        self.assertEqual(sorted(middleware), [1500, 1510, 1511, 1512, 1521, 1530, 1540, 1600])
        self.assertEqual(middleware[1500]['kind'], 'ros2_launch')
        self.assertEqual(middleware[1500]['name'], 'nav_bringup robot.launch.py')
        self.assertEqual({k: middleware[1510][k] for k in ('kind', 'name', 'package', 'program')},
                         {'kind': 'ros2_node', 'name': '/sensors/lidar', 'package': 'lidar_driver', 'program': 'lidar_driver_node'})
        self.assertEqual((middleware[1511]['name'], middleware[1511]['package']), ('nav_container', 'rclcpp_components'))
        # No --ros-args on the command line: the install path still says it is a node.
        self.assertEqual({k: middleware[1512][k] for k in ('kind', 'name', 'package')},
                         {'kind': 'ros2_node', 'name': 'static_transform_publisher', 'package': 'tf2_ros'})
        self.assertEqual((middleware[1521]['kind'], middleware[1521]['package']), ('ros2_node', 'chassis_bridge'))
        self.assertEqual(middleware[1530]['kind'], 'ros2_daemon')
        self.assertEqual((middleware[1540]['kind'], middleware[1540]['name']), ('ros2_tool', 'ros2 bag record'))
        self.assertEqual((middleware[1600]['kind'], middleware[1600]['program']), ('agent', 'MicroXRCEAgent'))

    def test_a_ros2_run_wrapper_is_not_listed_next_to_the_node_it_started(self):
        self.assertNotIn(1520, by(self.software['middleware'], 'pid'))

        alone = [p for p in ROS2_ROBOT if p.pid != 1521]              # the wrapper without its node is still worth showing
        middleware = by(device_software.parse(software(alone))['middleware'], 'pid')
        self.assertEqual((middleware[1520]['kind'], middleware[1520]['name']), ('ros2_tool', 'ros2 run chassis_bridge bridge_node'))

    def test_ros1_master_launch_and_nodes(self):
        middleware = by(device_software.parse(software(ROS1_ROBOT))['middleware'], 'pid')

        self.assertEqual({pid: row['kind'] for pid, row in middleware.items()},
                         {2000: 'ros1_master', 2001: 'ros1_master', 2002: 'ros1_node', 2010: 'ros1_launch', 2011: 'ros1_node',
                          2012: 'ros1_node', 2020: 'agent', 2021: 'agent'})
        self.assertEqual(middleware[2010]['name'], 'chassis_bringup base.launch')
        self.assertEqual((middleware[2011]['name'], middleware[2011]['package']), ('base_driver', 'chassis_driver'))
        self.assertEqual((middleware[2012]['name'], middleware[2012]['package']), ('camera_manager', 'nodelet'))

    def test_each_process_has_its_state_run_time_cpu_and_memory(self):
        middleware = by(self.software['middleware'], 'pid')

        self.assertEqual({k: middleware[1510][k] for k in ('state', 'user', 'memory_kb', 'cpu_percent', 'elapsed_seconds')},
                         {'state': 'running', 'user': 'robot', 'memory_kb': 91200, 'cpu_percent': 50.0, 'elapsed_seconds': 190952})
        self.assertEqual(middleware[1511]['cpu_percent'], 101.0)       # more than one core's worth is possible

    def test_top_processes_by_cpu_then_memory_without_kernel_threads(self):
        top = self.software['top']

        self.assertEqual([row['pid'] for row in top], [1511, 1510, 1700, 1521, 812, 1500, 1512, 1520, 1530, 1540])
        self.assertEqual({k: top[2][k] for k in ('name', 'user', 'cpu_percent', 'memory_kb', 'state')},
                         {'name': 'python3 · uploader.py', 'user': 'robot', 'cpu_percent': 20.0, 'memory_kb': 250000, 'state': 'running'})
        self.assertEqual(top[0]['name'], 'component_container')        # the whole name, not the 15 characters of comm
        self.assertNotIn(37, [row['pid'] for row in top])              # a busy kernel thread is not software the robot runs
        self.assertEqual(self.software['process_count'], 15)           # user-space processes still there at the second sample
        self.assertNotIn(3001, [row['pid'] for row in top])            # the command's own ps had already gone

    def test_the_command_does_not_list_itself(self):
        busy_probe = [p._replace(second=(0, 90)) if p.pid == 2990 else p for p in ROS2_ROBOT]   # its shell, using CPU

        top = device_software.parse(software(busy_probe))['top']

        self.assertNotIn(2990, [row['pid'] for row in top])

    def test_names_that_daemons_rewrite_are_shown_plainly(self):
        renamed = [Process(748, 1, 'message+', 900000, 'dbus-daemon', 'S', (0, 0), (50, 0), 900, '@dbus-daemon --system'),
                   Process(900, 1, 'root', 800000, 'nginx', 'S', (0, 0), (40, 0), 900, 'nginx: master process /usr/sbin/nginx'),
                   Process(950, 1, 'robot', 700000, 'bash', 'S', (0, 0), (30, 0), 900, '-bash'),
                   Process(960, 1, 'robot', 600000, 'python3', 'S', (0, 0), (20, 0), 900, 'python3 -m http.server 8000')]

        top = device_software.parse(software(renamed))['top']

        self.assertEqual([row['name'] for row in top], ['dbus-daemon', 'nginx', 'bash', 'python3 · http.server'])

    def test_a_memory_heavy_idle_process_is_listed_after_the_busy_ones(self):
        busy = [Process(100 + index, 1, 'robot', 1000, f'worker{index}', 'R', (0, 0), (10 + index, 0), 5000, f'/opt/robot/bin/worker{index}')
                for index in range(10)]
        idle_hog = Process(1750, 1, 'robot', 900000, 'map_cache', 'S', (10, 10), (10, 10), 15000, '/opt/robot/bin/map_cache')

        top = device_software.parse(software(busy + [idle_hog]))['top']

        self.assertEqual([row['pid'] for row in top], [109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 1750])
        self.assertEqual((top[-1]['name'], top[-1]['cpu_percent']), ('map_cache', 0.0))

    def test_without_cpu_samples_processes_are_still_listed_with_unknown_cpu(self):
        text = software().split('@@NX-SW:sample_b@@')[0] + '@@NX-SW:sample_b@@\n@@NX-SW:end@@\n'
        parsed = device_software.parse(text)

        row = by(parsed['middleware'], 'pid')[1510]
        self.assertEqual((row['state'], row['cpu_percent'], row['elapsed_seconds'], row['memory_kb']), ('unknown', None, None, 91200))
        self.assertEqual(parsed['top'][0]['pid'], 1700)                # by memory when there is no CPU figure

    def test_stopped_and_defunct_processes_are_told_apart_from_running_ones(self):
        # A defunct process has no command line left (ps prints "[name] <defunct>"): it can be named, not classified.
        few = [Process(1, 0, 'root', 9000, 'systemd', 'S', (10, 10), (10, 10), 0, '/sbin/init'),
               Process(1801, 1, 'robot', 0, 'ros_helper <defunct>', 'Z', (5, 5), (5, 5), 16000, '[ros_helper] <defunct>'),
               Process(1901, 1, 'robot', 3000, 'paused_node', 'T', (0, 0), (0, 0), 17000, '/opt/ros/humble/lib/demo/paused_node --ros-args')]
        parsed = device_software.parse(software(few))

        self.assertEqual([(row['pid'], row['state']) for row in parsed['middleware']], [(1901, 'stopped')])
        top = by(parsed['top'], 'pid')
        self.assertEqual((top[1801]['name'], top[1801]['state']), ('ros_helper', 'zombie'))
        self.assertEqual(top[1]['state'], 'running')

    def test_cpu_is_measured_on_a_device_without_getconf(self):
        text = software().replace('tick=100\n', 'tick=\n')            # busybox: the kernel reports these times in 1/100 s anyway

        row = by(device_software.parse(text)['middleware'], 'pid')[1510]

        self.assertEqual((row['cpu_percent'], row['elapsed_seconds']), (50.0, 190952))

    def test_a_process_list_that_hit_the_limit_says_so(self):
        many = [Process(10 + index, 1, 'robot', 100, f'job{index}', 'S', (0, 0), (0, 0), 5000, f'/opt/robot/bin/job{index}')
                for index in range(device_software.MAX_PROCESSES)]

        notes = device_software.parse(software(many))['notes']

        self.assertTrue(any('进程' in note and '不完整' in note for note in notes), notes)
        self.assertEqual(device_software.parse(software())['notes'], [])

    def test_command_lines_never_reach_the_status(self):
        text = json.dumps(self.software, ensure_ascii=False)

        self.assertNotIn(LEAKY_ARGUMENT, text)
        self.assertNotIn('--api-key', text)
        self.assertNotIn('launch_params', text)
        for section in ('middleware', 'top'):
            for row in self.software[section]:
                self.assertNotIn('args', row)

    def test_text_fields_pass_through_the_credential_filter(self):
        parsed = device_software.parse(software(), clean=lambda text: text.replace('nav', '[hidden]'))

        self.assertIn('[hidden]-stack.service', by(parsed['services']))
        self.assertIn('[hidden]-bridge', by(parsed['containers']['items']))
        self.assertEqual(by(parsed['middleware'], 'pid')[1511]['name'], '[hidden]_container')
        self.assertEqual(by(parsed['middleware'], 'pid')[1510]['cpu_percent'], 50.0)     # numbers are not filtered

    def test_a_device_whose_ps_gives_nothing_reports_processes_as_unknown(self):
        parsed = device_software.parse(software([]))

        self.assertIsNone(parsed['middleware'])
        self.assertIsNone(parsed['top'])
        self.assertEqual(len(parsed['services']), 10)                  # the rest is still there

    def test_cut_off_output_is_marked_incomplete(self):
        self.assertTrue(self.software['complete'])
        self.assertFalse(device_software.parse(software(end=False))['complete'])
        self.assertFalse(device_software.parse('')['complete'])

    def test_hostile_output_never_breaks_the_status(self):
        hostile = ('@@NX-SW:clock@@\ntick=nan\ncores=inf\n@@NX-SW:sample_a@@\nnan nan\n1 S nan inf 0\n7 S 1 1 0\n'
                   '@@NX-SW:services@@\n' + 'x' * 400 + '.service loaded active running ' + 'd' * 900 + '\n'
                   'ok.service loaded <script> running fine\n; rm -rf /.service loaded active running bad\n'
                   '@@NX-SW:systemd@@\nok\n@@NX-SW:containers@@\nexit=0\n' + 'n' * 500 + '|' + 'i' * 500 + '|Up\nno-separators\n'
                   '@@NX-SW:processes@@\nPID PPID USER RSS COMMAND\n7 1 ' + 'u' * 90 + ' 99999999999999999999 ' + 'c' * 300 + '\n'
                   '-5 x y z w\n@@NX-SW:arguments@@\n7 /opt/ros/humble/lib/' + 'p' * 300 + '/' + 'e' * 300 + ' __node:=' + 'n' * 600 + '\n'
                   '@@NX-SW:sample_b@@\ninf\n7 S inf nan 0\n@@NX-SW:end@@\n')

        parsed = device_software.parse(hostile)

        json.dumps(parsed, allow_nan=False)                           # the API refuses NaN / Infinity
        self.assertLessEqual(max(len(row['name']) for row in parsed['services']), 128)
        self.assertLessEqual(max(len(row['description']) for row in parsed['services']), 200)
        self.assertEqual([row['state'] for row in parsed['services']], ['running'])     # odd states and names are dropped
        self.assertLessEqual(len(parsed['containers']['items'][0]['name']), 128)
        for row in parsed['middleware'] + parsed['top']:
            self.assertLessEqual(len(row['name']), 160)
            self.assertIsNone(row['cpu_percent'])

    def test_long_lists_are_capped_and_say_so(self):
        many = ''.join(f'unit-{index}.service loaded active running Unit {index}\n' for index in range(400))
        parsed = device_software.parse(software(services=many))

        self.assertEqual(len(parsed['services']), device_software.MAX_SERVICES)
        self.assertTrue(any('只显示' in note for note in parsed['notes']))


class WatchedServiceTests(unittest.TestCase):
    def test_watched_services_are_marked_and_the_ones_not_running_are_added(self):
        parsed = device_software.parse(software())
        watched = [{'name': 'nav-stack', 'state': 'active'}, {'name': 'robot-idle-start.service', 'state': 'failed'},
                   {'name': 'recorder', 'state': 'inactive'}, {'name': 'ghost', 'state': 'unknown'}]

        merged = device_software.mark_watched(parsed, watched)

        services = by(merged['services'])
        self.assertTrue(services['nav-stack.service']['watched'])
        self.assertTrue(services['robot-idle-start.service']['watched'])
        self.assertFalse(services['ssh.service']['watched'])
        self.assertEqual(services['recorder'], {'name': 'recorder', 'description': '', 'state': 'inactive', 'watched': True})
        self.assertEqual(services['ghost']['state'], 'unknown')
        self.assertEqual(len(merged['services']), 12)
        self.assertFalse(by(parsed['services'])['nav-stack.service']['watched'])        # the parsed result is not changed

    def test_without_systemd_the_watched_services_are_still_shown(self):
        parsed = device_software.parse(software(services='', systemd=False))

        merged = device_software.mark_watched(parsed, [{'name': 'bridge', 'state': 'unknown'}])

        self.assertEqual(merged['services'], [{'name': 'bridge', 'description': '', 'state': 'unknown', 'watched': True}])
        self.assertIsNone(device_software.mark_watched(parsed, [])['services'])


class HardwareViewTests(unittest.TestCase):
    def view(self, text=None):
        return device_status.hardware_view(hardware_scan.parse(text or jetson()))

    def test_every_device_says_whether_its_driver_is_loaded_and_whether_it_works(self):
        view = self.view()
        devices = by(view['devices'], 'id')

        self.assertEqual(len(view['devices']), len(hardware_scan.parse(jetson())['devices']))     # all of them
        wifi = devices['pci:0008:01:00.0']                            # Wi-Fi card without firmware: no driver bound
        self.assertEqual((wifi['driver_state'], wifi['status']), ('missing', 'error'))
        self.assertIn('未加载驱动', wifi['issue'])
        ethernet = devices['net:eth0']
        self.assertEqual((ethernet['driver_state'], ethernet['driver'], ethernet['status'], ethernet['issue']),
                         ('loaded', 'r8168', 'ok', ''))
        self.assertEqual((devices['net:can0']['driver_state'], devices['net:can0']['status']), ('loaded', 'warning'))
        self.assertIn('未启用', devices['net:can0']['issue'])
        self.assertEqual(devices['serial:/dev/ttyUSB0']['status'], 'warning')        # driver bound, but no permission
        self.assertEqual(devices['serial:/dev/ttyUSB0']['driver_state'], 'loaded')
        self.assertIn('无权', devices['serial:/dev/ttyUSB0']['issue'])
        self.assertEqual(devices['compute:cpu']['driver_state'], 'none')             # nothing to load for a processor
        self.assertEqual(devices['bus:i2c']['driver_state'], 'none')

    def test_driver_states_cover_every_wording_of_the_scan(self):
        states = {}
        unnamed = jetson().replace('|dialout|660|serial-tegra|', '|dialout|660||')      # a serial port whose driver sysfs does not name
        for text in (jetson(), jetson(fixed=True), JETPACK6, unnamed):
            for device in self.view(text)['devices']:
                states.setdefault(device['driver_state'], set()).add(device['driver'])

        self.assertEqual(set(states), {'loaded', 'missing', 'unknown', 'none'})
        self.assertEqual(states['missing'], {'未加载'})
        self.assertEqual(states['unknown'], {'未知'})
        self.assertEqual(states['none'] - {'', '—'}, set())
        for name in states['loaded']:
            self.assertRegex(name, r'^[A-Za-z0-9_, -]+$')              # a driver name, never one of the placeholders

    def test_summary_counts(self):
        view = self.view()
        statuses = [device['status'] for device in view['devices']]

        self.assertEqual(view['summary'], {'devices': len(statuses), 'ok': statuses.count('ok'),
                                           'attention': statuses.count('warning') + statuses.count('error'),
                                           'unknown': statuses.count('unknown'),
                                           'driver_missing': sum(1 for d in view['devices'] if d['driver_state'] == 'missing')})
        self.assertGreater(view['summary']['attention'], 0)
        self.assertGreater(view['summary']['driver_missing'], 0)

    def test_issues_keep_what_the_page_shows_and_nothing_else(self):
        view = self.view()

        self.assertEqual({issue['id'] for issue in view['issues']}, {i['id'] for i in hardware_scan.parse(jetson())['issues']})
        self.assertEqual(set(view['issues'][0]), {'id', 'severity', 'category', 'title', 'detail', 'device_id'})
        self.assertEqual(set(view['system']), {'model', 'os', 'kernel', 'arch', 'jetson', 'cpu', 'cores'})   # no account details
        self.assertEqual(sum(category['count'] for category in view['categories']), len(view['devices']))
        self.assertIn({'key': 'serial', 'label': '串口', 'count': 2}, view['categories'])
        self.assertEqual([category['key'] for category in view['categories']],
                         [key for key in hardware_scan.CATEGORIES if any(d['category'] == key for d in view['devices'])])
        self.assertTrue(view['complete'])
        self.assertIsNone(view['problem'])

    def test_a_fixed_robot_has_nothing_to_handle_on_those_devices(self):
        devices = by(self.view(jetson(fixed=True))['devices'], 'id')

        self.assertEqual((devices['serial:/dev/ttyUSB0']['status'], devices['serial:/dev/ttyUSB0']['issue']), ('ok', ''))


SOFTWARE_OUTPUT = software().encode()


class FullStatusCase(SshCase):
    def setUp(self):
        super().setUp()
        self.target.command_outputs.update({device_software.SOFTWARE_EXEC: SOFTWARE_OUTPUT,
                                            hardware_scan.SCAN_EXEC: jetson().encode()})

    def status(self, connection, **extra):
        return self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id'], **extra})


class FullStatusTests(FullStatusCase):
    def test_one_read_lists_hardware_and_software_through_the_jump_host(self):
        connection = self.connect_through_jump(watch_services=SERVICES)

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertEqual(view['status']['hostname'], 'dog-fixture')
        hardware, running = view['hardware'], view['software']
        self.assertTrue(hardware['complete'])
        self.assertIsNone(hardware['problem'])
        self.assertEqual(by(hardware['devices'], 'id')['pci:0008:01:00.0']['driver_state'], 'missing')
        self.assertGreaterEqual(hardware['duration_ms'], 0)
        self.assertAlmostEqual(hardware['checked_at'], time.time(), delta=30)
        self.assertEqual(by(running['middleware'], 'pid')[1510]['name'], '/sensors/lidar')
        self.assertEqual(running['containers']['state'], 'ok')
        self.assertIsNone(running['problem'])
        services = by(running['services'])
        self.assertTrue(services['nav-stack.service']['watched'])     # "nav-stack" is on the watch list
        self.assertEqual(services['recorder'], {'name': 'recorder', 'description': '', 'state': 'inactive', 'watched': True})
        self.assertEqual(view['refresh_seconds'], 60)
        json.dumps(view, allow_nan=False)

    def test_only_the_three_fixed_commands_reach_the_robot_and_none_the_jump_host(self):
        connection = self.connect_through_jump(watch_services=SERVICES)

        self.status(connection)

        self.assertEqual(self.target.commands, [device_status.status_command(SERVICES), device_software.SOFTWARE_EXEC,
                                                hardware_scan.SCAN_EXEC])
        self.assertEqual(self.jump.commands, [])

    def test_a_quick_read_runs_only_the_basic_command(self):
        connection = self.connect_directly(watch_services=SERVICES)

        view = self.status(connection, quick=True)

        self.assertTrue(view['reachable'], view)
        self.assertEqual(self.target.commands, [device_status.status_command(SERVICES)])
        self.assertIsNone(view['hardware'])                           # not read yet: the page shows it as loading
        self.assertIsNone(view['software'])

    def test_a_quick_read_after_a_full_one_keeps_showing_what_was_read(self):
        connection = self.connect_directly(watch_services=SERVICES)
        full = self.status(connection)

        view = self.status(connection, quick=True)

        self.assertEqual(view['hardware']['checked_at'], full['hardware']['checked_at'])
        self.assertEqual(len(view['hardware']['devices']), len(full['hardware']['devices']))
        self.assertEqual(view['software']['checked_at'], full['software']['checked_at'])

    def test_reading_the_status_stores_no_scan_and_writes_no_audit_entry(self):
        connection = self.connect_directly(watch_services=SERVICES)
        audits = len(self.store.list('events'))

        self.status(connection)
        self.status(connection)

        self.assertEqual(self.store.list('hardware_scans'), [])
        self.assertEqual(len(self.store.list('events')), audits)
        self.assertEqual(self.platform.hardware.scanning, set())

    def test_a_hardware_scan_running_elsewhere_is_not_doubled(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.status(connection)
        self.platform.hardware.scanning.add(connection['robot_id'])   # the hardware page or an investigation is scanning
        self.addCleanup(self.platform.hardware.scanning.discard, connection['robot_id'])
        del self.target.commands[:]

        view = self.status(connection)

        self.assertNotIn(hardware_scan.SCAN_EXEC, self.target.commands)
        self.assertIn(device_software.SOFTWARE_EXEC, self.target.commands)         # the rest is still read
        self.assertEqual(view['hardware']['checked_at'], first['hardware']['checked_at'])     # the earlier result, with its time
        self.assertEqual(len(view['hardware']['devices']), len(first['hardware']['devices']))
        self.assertIn('正在扫描', view['hardware']['problem'])

    def repair_session(self, connection, status='running', mode='repair'):
        self.store.put('agent_sessions', {'id': 'agent-session-fixture', 'project_id': self.pid, 'robot_id': connection['robot_id'],
                                          'connection_id': connection['id'], 'mode': mode, 'status': status, 'events': []})

    def test_the_hardware_is_not_scanned_while_a_repair_session_changes_the_robot(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.status(connection)
        for status in ('starting', 'running', 'waiting_input', 'cancelling'):
            with self.subTest(status=status):
                self.repair_session(connection, status)
                del self.target.commands[:]

                view = self.status(connection)

                self.assertNotIn(hardware_scan.SCAN_EXEC, self.target.commands)
                self.assertIn(device_software.SOFTWARE_EXEC, self.target.commands)
                self.assertEqual(view['hardware']['checked_at'], first['hardware']['checked_at'])
                self.assertIn('修复', view['hardware']['problem'])

    def test_the_hardware_is_scanned_again_once_the_repair_session_has_ended(self):
        connection = self.connect_directly(watch_services=SERVICES)
        for status in ('completed', 'failed', 'interrupted', 'needs_followup'):
            self.repair_session(connection, status)
            self.assertIsNone(self.status(connection)['hardware']['problem'], status)
        self.repair_session(connection, 'running', mode='discovery')   # an investigation only reads; its own scan is in `scanning`
        self.assertIsNone(self.status(connection)['hardware']['problem'])

    def test_a_repair_session_on_another_robot_does_not_hold_this_one(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.repair_session({**connection, 'robot_id': 'robot-elsewhere'})

        self.assertIsNone(self.status(connection)['hardware']['problem'])

    def test_a_session_that_died_between_two_reads_is_reopened_for_the_full_read(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.status(connection)
        for transport in list(self.target.transports):
            transport.close()                                         # e.g. the robot restarted its SSH server
        del self.target.commands[:]

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertIsNone(view['hardware']['problem'])
        self.assertIsNone(view['software']['problem'])
        self.assertGreater(view['hardware']['checked_at'], first['hardware']['checked_at'])
        # The basic command is the one that found the session dead; the two slow ones ran once, on the new session.
        self.assertEqual(self.target.commands, [device_status.status_command(SERVICES), device_software.SOFTWARE_EXEC,
                                                hardware_scan.SCAN_EXEC])

    def test_output_that_cannot_be_parsed_is_not_blamed_on_the_connection(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.status(connection)
        sessions = len(self.target.transports)
        with mock.patch.object(device_software, 'parse', side_effect=RuntimeError('parser bug')), \
                self.assertLogs(device_status.LOG, 'ERROR'):
            view = self.status(connection)

        self.assertIn('没能解析', view['software']['problem'])
        self.assertNotIn('会话', view['software']['problem'])
        self.assertEqual(view['software']['checked_at'], first['software']['checked_at'])
        self.assertIsNone(view['hardware']['problem'])               # the scan after it still ran
        self.status(connection)
        self.assertEqual(len(self.target.transports), sessions)       # and the SSH session was kept

    def test_a_quick_read_does_not_show_parts_that_were_read_long_ago(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.status(connection)
        self.assertIsNotNone(self.status(connection, quick=True)['hardware'])

        with mock.patch.object(device_status, 'CACHE_SECONDS', -1):   # as if the full read had been long ago
            view = self.status(connection, quick=True)

        self.assertTrue(view['reachable'])
        self.assertIsNone(view['hardware'])                           # shown as loading until the full read arrives
        self.assertIsNone(view['software'])

    def test_a_scan_running_elsewhere_before_the_first_read_shows_no_made_up_hardware(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.platform.hardware.scanning.add(connection['robot_id'])
        self.addCleanup(self.platform.hardware.scanning.discard, connection['robot_id'])

        view = self.status(connection)

        self.assertIsNone(view['hardware']['devices'])
        self.assertIsNone(view['hardware']['checked_at'])
        self.assertIn('正在扫描', view['hardware']['problem'])
        self.assertTrue(view['reachable'])

    def test_a_scan_that_does_not_finish_leaves_the_rest_of_the_status_in_place(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.status(connection)
        self.target.unanswered.add(hardware_scan.SCAN_EXEC)
        with mock.patch.object(hardware_scan, 'SCAN_TIMEOUT_SECONDS', 1):
            view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertEqual(view['status']['hostname'], 'dog-fixture')
        self.assertIsNone(view['software']['problem'])
        self.assertEqual(view['hardware']['checked_at'], first['hardware']['checked_at'])
        self.assertIn('没有完成', view['hardware']['problem'])

        self.target.unanswered.clear()                                # the next minute reads it again, on a fresh session
        again = self.status(connection)
        self.assertIsNone(again['hardware']['problem'])
        self.assertGreater(again['hardware']['checked_at'], first['hardware']['checked_at'])

    def test_software_that_cannot_be_read_does_not_hide_the_hardware(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.target.unanswered.add(device_software.SOFTWARE_EXEC)
        with mock.patch.object(device_software, 'TIMEOUT_SECONDS', 1):
            view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertIsNone(view['software']['middleware'])
        self.assertIsNone(view['software']['containers'])
        self.assertIn('没有返回', view['software']['problem'])
        # The watched services come with the basic status, so they are still shown.
        self.assertEqual([(s['name'], s['state']) for s in view['software']['services']],
                         [('bridge', 'active'), ('nav-stack', 'failed'), ('recorder', 'inactive')])
        self.assertTrue(view['hardware']['complete'])
        self.assertIsNone(view['hardware']['problem'])

    def test_one_read_never_takes_longer_than_its_budget(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.target.unanswered.update({device_software.SOFTWARE_EXEC, hardware_scan.SCAN_EXEC})
        with mock.patch.object(device_status, 'READ_BUDGET_SECONDS', 2):
            started = time.monotonic()
            view = self.status(connection)
            waited = time.monotonic() - started

        self.assertLess(waited, 6)                                    # not 15 s + 40 s, the two commands' own limits
        self.assertTrue(view['reachable'])
        self.assertIsNotNone(view['software']['problem'])
        self.assertIsNotNone(view['hardware']['problem'])

    def test_a_short_password_does_not_corrupt_the_software_numbers(self):
        self.target.password = '5'                                    # a digit that occurs all over the samples
        connection = self.connect_directly(watch_services=SERVICES, password='5')

        view = self.status(connection)

        self.assertEqual(by(view['software']['middleware'], 'pid')[1510]['cpu_percent'], 50.0)
        self.assertEqual(by(view['software']['middleware'], 'pid')[1510]['memory_kb'], 91200)

    def test_an_unreachable_robot_shows_no_stale_hardware_or_software(self):
        connection = self.connect_through_jump(watch_services=SERVICES)
        self.status(connection)
        self.jump.forward_to.clear()
        for transport in list(self.target.transports) + list(self.jump.transports):
            transport.close()

        view = self.status(connection)

        self.assertFalse(view['reachable'])
        self.assertEqual(view['error']['hop'], 'device')
        self.assertIsNone(view['hardware'])
        self.assertIsNone(view['software'])


class RefreshRateTests(unittest.TestCase):
    def test_the_page_is_told_to_read_once_a_minute(self):
        self.assertEqual(device_status.REFRESH_SECONDS, 60)

    def test_the_ssh_session_outlives_the_gap_between_two_reads(self):
        # Polled once a minute: a session closed after 60 idle seconds would log in again for every read.
        self.assertGreaterEqual(device_status.IDLE_CLOSE_SECONDS, 2 * device_status.REFRESH_SECONDS)

    def test_a_quick_read_keeps_showing_the_last_full_read_for_a_few_minutes(self):
        self.assertGreaterEqual(device_status.CACHE_SECONDS, 2 * device_status.REFRESH_SECONDS)
        self.assertLessEqual(device_status.CACHE_SECONDS, 600)

    def test_one_read_fits_into_the_interval(self):
        self.assertLess(device_status.READ_BUDGET_SECONDS, device_status.REFRESH_SECONDS)
        self.assertGreaterEqual(device_status.READ_BUDGET_SECONDS,
                                device_software.TIMEOUT_SECONDS + 20)   # room left for the hardware scan


if __name__ == '__main__':
    unittest.main()
