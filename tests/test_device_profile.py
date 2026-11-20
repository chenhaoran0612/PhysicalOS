"""The device profile of a real machine (issue #27): hardware, system, ROS 2 and resource usage in
one record, read over SSH with fixed read-only commands and kept per robot.

Both ends are local SSH protocol fixtures: nothing here reaches a real machine.
"""
import json
import threading
import time
import unittest
from unittest import mock

from brainos import device_profile, device_software, device_status, hardware_scan, ros2_profile
from brainos.robot_remove import ROBOT_RECORDS
from brainos.service import now
from hardware_fixture import jetson
from ros2_fixture import TOPIC_LIST, absent, ros2
from software_fixture import LEAKY_ARGUMENT, software
from ssh_fixture import DEFAULT_LOGIN
from test_ssh_jump import JUMP_PASSWORD, JUMP_USER, SERVICES, SshCase

PARTS = ('system', 'hardware', 'resources', 'ros2')


class ProfileCase(SshCase):
    def setUp(self):
        super().setUp()
        self.target.command_outputs.update({device_software.SOFTWARE_EXEC: software().encode(),
                                            hardware_scan.SCAN_EXEC: jetson().encode(),
                                            ros2_profile.ROS2_EXEC: ros2().encode()})
        self.profiles = self.platform.device_profile

    def read(self, connection, **extra):
        return self.profiles.read({'project_id': self.pid, 'robot_id': connection['robot_id'], **extra})

    def saved(self, connection):
        return self.profiles.saved({'project_id': self.pid, 'robot_id': connection['robot_id']})


class ReadTests(ProfileCase):
    def test_one_read_makes_a_profile_of_hardware_system_ros2_and_resources(self):
        connection = self.connect_through_jump(watch_services=SERVICES)

        profile = self.read(connection)

        self.assertEqual(profile['schema'], device_profile.SCHEMA)
        self.assertEqual((profile['robot_id'], profile['connection_id'], profile['project_id']),
                         (connection['robot_id'], connection['id'], self.pid))
        self.assertEqual({key: profile['system'][key] for key in ('hostname', 'model', 'os', 'arch', 'cpu', 'cores')},
                         {'hostname': 'dog-fixture', 'model': 'NVIDIA Orin NX Developer Kit', 'os': 'Ubuntu 20.04.6 LTS',
                          'arch': 'aarch64', 'cpu': 'Cortex-A78AE', 'cores': '8'})
        hardware = profile['hardware']
        self.assertTrue(hardware['complete'])
        self.assertEqual(len(hardware['devices']), len(hardware_scan.parse(jetson())['devices']))
        self.assertGreater(hardware['summary']['attention'], 0)
        resources = profile['resources']
        self.assertEqual(resources['memory'], {'total_kb': 16000000, 'available_kb': 4000000, 'used_percent': 75})
        self.assertEqual(resources['disk_root']['used_percent'], 25)
        self.assertEqual((resources['load'], resources['temperature_c'], resources['uptime_seconds']), ([0.52, 0.61, 0.4], 61.3, 372))
        self.assertEqual((resources['cores'], resources['process_count']), (8, 15))
        self.assertEqual(resources['top'][0]['name'], 'component_container')
        ros = profile['ros2']
        self.assertEqual((ros['status'], ros['distro'], ros['supported']), ('ok', 'humble', True))
        self.assertEqual([row['name'] for row in ros['topics']], TOPIC_LIST.split())
        json.dumps(profile, allow_nan=False)

    def test_every_part_says_when_it_was_read(self):
        connection = self.connect_directly(watch_services=SERVICES)

        profile = self.read(connection)

        for part in PARTS:
            self.assertAlmostEqual(profile[part]['checked_at'], time.time(), delta=30, msg=part)
        self.assertAlmostEqual(profile['resources']['processes_checked_at'], time.time(), delta=30)
        self.assertGreaterEqual(profile['ros2']['duration_ms'], 0)
        self.assertGreaterEqual(profile['duration_ms'], profile['ros2']['duration_ms'])
        self.assertRegex(profile['created_at'], r'^\d{4}-\d\d-\d\dT')

    def test_only_the_four_fixed_commands_reach_the_robot_and_none_the_jump_host(self):
        connection = self.connect_through_jump(watch_services=SERVICES)

        self.read(connection)

        self.assertEqual(self.target.commands, [device_status.status_command(SERVICES), device_software.SOFTWARE_EXEC,
                                                hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC])
        self.assertEqual(self.jump.commands, [])

    def test_nothing_of_the_request_reaches_the_command(self):
        connection = self.connect_directly(watch_services=SERVICES)

        self.read(connection, command='ros2 topic pub /cmd_vel', distro='$(reboot)', topics=['; reboot'])

        self.assertEqual(self.target.commands[-1], ros2_profile.ROS2_EXEC)
        self.assertEqual(len(self.target.commands), 4)

    def test_the_profile_holds_no_login_no_ssh_route_and_no_command_line(self):
        # The addresses of the robot's own network interfaces are part of the hardware table, as in a hardware
        # scan; how the robot is reached (the SSH endpoints, the jump host) and the logins are not in the profile.
        connection = self.connect_through_jump(watch_services=SERVICES)

        text = json.dumps(self.read(connection), ensure_ascii=False)

        for leaked in (DEFAULT_LOGIN[1], JUMP_PASSWORD, LEAKY_ARGUMENT, '--api-key', f':{self.target.port}', f':{self.jump.port}',
                       'robot@', JUMP_USER + '@'):
            self.assertNotIn(leaked, text)
        self.assertNotIn('"target"', text)
        self.assertNotIn('"jump"', text)

    def test_a_short_password_does_not_corrupt_the_names(self):
        self.target.password = '5'                                    # a digit: hiding it would rewrite names that hold one
        ros = ros2().replace('/scan [', '/scan5 [')
        self.target.command_outputs[ros2_profile.ROS2_EXEC] = ros.encode()
        connection = self.connect_directly(watch_services=SERVICES, password='5')

        profile = self.read(connection)

        self.assertIn('/scan5', [row['name'] for row in profile['ros2']['topics']])


class WithoutRos2Tests(ProfileCase):
    def test_a_device_without_ros2_gets_a_profile_that_says_so(self):
        self.target.command_outputs[ros2_profile.ROS2_EXEC] = absent().encode()
        connection = self.connect_directly(watch_services=SERVICES)

        profile = self.read(connection)

        ros = profile['ros2']
        self.assertEqual((ros['status'], ros['found'], ros['supported'], ros['topics'], ros['problem']),
                         ('absent', False, None, None, None))            # not read is not a failure
        self.assertTrue(profile['hardware']['complete'])
        self.assertEqual(self.saved(connection)['profile']['ros2']['status'], 'absent')

    def test_another_distribution_is_marked_unsupported(self):
        foxy = ros2(installed=(('foxy', 1, 0),), distro='foxy').replace('distro=humble', 'distro=foxy')
        self.target.command_outputs[ros2_profile.ROS2_EXEC] = foxy.encode()
        connection = self.connect_directly(watch_services=SERVICES)

        ros = self.read(connection)['ros2']

        self.assertEqual((ros['status'], ros['distro'], ros['supported']), ('unsupported', 'foxy', False))


class KeptTests(ProfileCase):
    def test_the_last_profile_is_kept_and_read_back(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.assertEqual(self.saved(connection), {'profile': None, 'reading': False})

        profile = self.read(connection)

        self.assertEqual(self.saved(connection), {'profile': profile, 'reading': False})
        self.assertEqual(self.store.list(device_profile.KIND), [profile])

    def test_a_second_read_replaces_the_first(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.read(connection)

        second = self.read(connection)

        self.assertEqual(second['id'], first['id'])
        self.assertGreater(second['ros2']['checked_at'], first['ros2']['checked_at'])
        self.assertEqual(len(self.store.list(device_profile.KIND)), 1)

    def test_the_workspace_state_names_the_robots_with_a_profile(self):
        # 0.20.0, issue #11 × #27: the deletion's confirmation says the profile goes with the robot. The state
        # carries only which robot has one: the profile itself is asked for apart, and a second read of it
        # changes nothing here, so it does not redraw the page.
        connection = self.connect_directly(watch_services=SERVICES)
        self.assertEqual(self.platform.snapshot()['device_profiles'], [])

        profile = self.read(connection)
        named = [{'id': profile['id'], 'project_id': self.pid, 'robot_id': connection['robot_id']}]
        self.assertEqual(self.platform.snapshot()['device_profiles'], named)
        self.read(connection)
        self.assertEqual(self.platform.snapshot()['device_profiles'], named)

    def test_reading_back_touches_no_machine(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.read(connection)
        del self.target.commands[:]

        self.saved(connection)

        self.assertEqual(self.target.commands, [])

    def test_a_read_is_audited_without_what_was_read(self):
        connection = self.connect_directly(watch_services=SERVICES)

        self.read(connection)

        event = next(e for e in self.store.list('events') if e['action'] == 'device.profile_read')
        self.assertEqual(event['details'], {'robot_id': connection['robot_id'], 'profile_id': self.saved(connection)['profile']['id'],
                                            'ros2': 'ok', 'distro': 'humble'})

    def test_the_profile_goes_with_the_robot_when_it_is_deleted(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.read(connection)
        self.assertIn(device_profile.KIND, ROBOT_RECORDS)

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertEqual(self.store.list(device_profile.KIND), [])

    def test_a_profile_read_before_the_connection_was_changed_is_not_shown_as_this_one_s(self):
        # Issue #9: the robot's connection may now lead to another machine.
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.read(connection)
        self.store.put('device_connections', {**self.store.get('device_connections', connection['id']), 'updated_at': now()})

        self.assertIsNone(self.saved(connection)['profile'])
        self.target.unanswered.add(ros2_profile.ROS2_EXEC)
        with mock.patch.object(ros2_profile, 'TIMEOUT_SECONDS', 1):
            ros = self.read(connection)['ros2']

        self.assertEqual((ros['status'], ros['topics']), ('not_read', None))     # not the list of the earlier machine
        self.assertNotEqual(first['ros2']['topics'], None)

    def test_changing_the_watched_services_keeps_the_profile(self):
        connection = self.connect_directly(watch_services=SERVICES)
        profile = self.read(connection)

        self.engine.watch_services({'project_id': self.pid, 'robot_id': connection['robot_id'], 'services': ['bridge']})

        self.assertEqual(self.saved(connection)['profile'], profile)

    def test_a_robot_deleted_during_the_read_leaves_no_profile_behind(self):
        connection = self.connect_directly(watch_services=SERVICES)
        real = device_profile.read_ros2

        def deleted_meanwhile(device):
            part = real(device)
            self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})
            return part

        with mock.patch.object(device_profile, 'read_ros2', deleted_meanwhile), self.assertRaises(ValueError):
            self.read(connection)

        self.assertEqual(self.store.list(device_profile.KIND), [])


class NotReadTests(ProfileCase):
    def test_a_ros2_read_that_does_not_finish_keeps_the_earlier_one_and_says_so(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.read(connection)
        self.target.unanswered.add(ros2_profile.ROS2_EXEC)

        with mock.patch.object(ros2_profile, 'TIMEOUT_SECONDS', 1):
            profile = self.read(connection)

        ros = profile['ros2']
        self.assertIn('没有完成', ros['problem'])
        self.assertEqual(ros['checked_at'], first['ros2']['checked_at'])     # the earlier result, with its own time
        self.assertEqual(ros['topics'], first['ros2']['topics'])
        self.assertGreater(profile['hardware']['checked_at'], first['hardware']['checked_at'])   # the rest was read again

    def test_a_first_ros2_read_that_does_not_finish_shows_nothing_made_up(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.target.unanswered.add(ros2_profile.ROS2_EXEC)

        with mock.patch.object(ros2_profile, 'TIMEOUT_SECONDS', 1):
            ros = self.read(connection)['ros2']

        self.assertEqual((ros['status'], ros['found'], ros['topics'], ros['checked_at']), ('not_read', None, None, None))
        self.assertIn('没有完成', ros['problem'])

    def test_output_that_cannot_be_parsed_is_reported_as_our_fault(self):
        connection = self.connect_directly(watch_services=SERVICES)

        with mock.patch.object(ros2_profile, 'parse', side_effect=RuntimeError('parser bug')), \
                self.assertLogs(device_profile.LOG, 'ERROR'):
            ros = self.read(connection)['ros2']

        self.assertIn('没能解析', ros['problem'])
        self.assertEqual(ros['status'], 'not_read')

    def test_output_cut_at_the_limit_is_marked_incomplete(self):
        connection = self.connect_directly(watch_services=SERVICES)

        with mock.patch.object(ros2_profile, 'OUTPUT_LIMIT', len(ros2()) - 40):
            ros = self.read(connection)['ros2']

        self.assertFalse(ros['complete'])
        self.assertEqual(ros['status'], 'incomplete')

    def test_an_unreachable_robot_gives_an_error_and_keeps_the_saved_profile(self):
        connection = self.connect_directly(watch_services=SERVICES)
        first = self.read(connection)
        for transport in list(self.target.transports):
            transport.close()
        self.target.close()

        with self.assertRaises(ValueError) as refused:
            self.read(connection)

        self.assertIn('SSH', str(refused.exception))
        self.assertEqual(self.saved(connection)['profile'], first)

    def test_a_hardware_scan_running_elsewhere_does_not_stop_the_profile(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.platform.hardware.scanning.add(connection['robot_id'])
        self.addCleanup(self.platform.hardware.scanning.discard, connection['robot_id'])

        profile = self.read(connection)

        self.assertIsNone(profile['hardware']['devices'])
        self.assertIn('正在扫描', profile['hardware']['problem'])
        self.assertEqual(profile['ros2']['status'], 'ok')
        self.assertEqual(profile['system']['hostname'], 'dog-fixture')


class RefusedTests(ProfileCase):
    def test_two_reads_of_one_robot_do_not_run_at_once(self):
        connection = self.connect_directly(watch_services=SERVICES)
        entered, release = threading.Event(), threading.Event()
        real = device_profile.read_ros2

        def slow(device):
            entered.set()
            release.wait(10)
            return real(device)

        with mock.patch.object(device_profile, 'read_ros2', slow):
            first = threading.Thread(target=self.read, args=(connection,))
            first.start()
            self.assertTrue(entered.wait(20))
            try:
                self.assertTrue(self.saved(connection)['reading'])
                with self.assertRaises(ValueError) as refused:
                    self.read(connection)
            finally:
                release.set()
                first.join(20)

        self.assertIn('正在读取', str(refused.exception))
        self.assertFalse(self.saved(connection)['reading'])
        self.assertEqual(self.target.commands.count(ros2_profile.ROS2_EXEC), 1)

    def test_only_a_robot_of_this_project_is_read(self):
        connection = self.connect_directly(watch_services=SERVICES)
        other = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']
        del self.target.commands[:]

        for call in (self.profiles.read, self.profiles.saved):
            with self.assertRaises(ValueError):
                call({'project_id': other, 'robot_id': connection['robot_id']})

        self.assertEqual(self.target.commands, [])

    def test_a_robot_without_an_ssh_connection_has_no_profile_to_read(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Not connected', 'family': 'quadruped'})

        with self.assertRaises(ValueError) as refused:
            self.profiles.read({'project_id': self.pid, 'robot_id': robot['id']})

        self.assertIn('SSH', str(refused.exception))


class ComposeTests(unittest.TestCase):
    def view(self, **changes):
        status = {'hostname': 'robot-a', 'uptime_seconds': 90, 'load': [0.1, 0.2, 0.3], 'memory': None, 'disk_root': None,
                  'temperature_c': None, 'failed_units': None, 'services': []}
        return {'checked_at': 1000.0, 'reachable': True, 'latency_ms': 12, 'via_jump': False, 'target': 'robot@192.0.2.10:22',
                'jump': None, 'status': status, 'error': None, 'hardware': None, 'software': None, 'refresh_seconds': 60, **changes}

    def test_parts_that_were_never_read_are_unknown_not_empty(self):
        profile = device_profile.compose(self.view(), ros2_profile.NOT_READ)

        self.assertEqual(set(profile), set(PARTS))
        self.assertEqual(profile['system'], {'checked_at': None, 'hostname': 'robot-a', 'model': '', 'os': '', 'kernel': '',
                                             'arch': '', 'jetson': '', 'cpu': '', 'cores': ''})
        self.assertIsNone(profile['hardware']['devices'])
        self.assertEqual((profile['resources']['checked_at'], profile['resources']['uptime_seconds']), (1000.0, 90))
        self.assertEqual((profile['resources']['top'], profile['resources']['processes_checked_at']), (None, None))

    def test_the_parts_keep_the_shape_other_features_read(self):
        # Issues #23 (interface table) and #28 (motion driver check) build on these keys.
        profile = device_profile.compose(self.view(), {**ros2_profile.parse(ros2()), 'checked_at': 1001.0, 'duration_ms': 5,
                                                       'problem': None})

        self.assertEqual(set(profile['ros2']), {'status', 'found', 'distro', 'generation', 'supported', 'installed', 'environment',
                                                'nodes', 'topics', 'services', 'actions', 'notes', 'complete', 'checked_at',
                                                'duration_ms', 'problem'})
        self.assertEqual(set(profile['resources']), {'checked_at', 'uptime_seconds', 'load', 'memory', 'disk_root', 'temperature_c',
                                                     'cores', 'process_count', 'top', 'processes_checked_at', 'problem'})
        self.assertEqual(set(profile['hardware']), {'checked_at', 'duration_ms', 'complete', 'problem', 'summary', 'categories',
                                                    'devices', 'issues', 'notes'})


if __name__ == '__main__':
    unittest.main()
