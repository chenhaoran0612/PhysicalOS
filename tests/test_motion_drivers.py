"""The motion driver check (issue #28, R1-10): does a device have the motion-control interfaces its
body type should have, what is missing, and how that reaches the hardware repair flow.

What a body type should have comes from the platform's list of body types (issue #32), what the
device has from the ROS 2 part of its device profile (issue #27), which interface carries which
capability from the standard (``neuxmind_spec.ros2_interfaces``). Both ends are local SSH protocol
fixtures: nothing here reaches a real machine.
"""
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from brainos import (agent_runtime, device_access, device_profile, device_software, device_status, hardware_init, hardware_scan, heartbeat,
                     motion_drivers, ros2_profile)
from brainos.body_types import built_in
from brainos.spec_bridge import ros2_interfaces
from hardware_fixture import jetson
from motion_drivers_fixture import ARM_ACTION, arm, chassis, idle
from ros2_fixture import absent, listing, ros2, ros1_only
from software_fixture import software
from test_engineering import ResponsesFixture
from test_product import Server
from test_ssh_jump import SERVICES, SshCase

QUADRUPED, MANIPULATOR = built_in()
VELOCITY, ODOMETRY, ARM = 'motion-driver:base.velocity', 'motion-driver:base.odometry', 'motion-driver:arm.joint_position'
READ_ONLY = {hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC, device_software.SOFTWARE_EXEC, device_status.status_command(SERVICES),
             heartbeat.HEARTBEAT_COMMAND}


def part(text):
    """The ``ros2`` part of a device profile as a read of ``text`` leaves it."""
    return {**ros2_profile.parse(text), 'checked_at': 1763700000.0, 'duration_ms': 12, 'problem': None}


def by_capability(result):
    return {item['capability']: item for item in result['items']}


class ExpectedInterfaceTests(unittest.TestCase):
    """What a body type should have, in ROS 2 terms."""

    def test_a_quadruped_is_driven_by_velocity_and_reports_odometry(self):
        items = {item['capability']: item for item in motion_drivers.expected(QUADRUPED)}

        self.assertEqual(list(items), QUADRUPED['required_capabilities'])
        self.assertEqual({key: items['base.velocity'][key] for key in ('kind', 'role', 'name', 'types', 'title')},
                         {'kind': 'topic', 'role': 'subscribes', 'name': '/cmd_vel', 'types': ['geometry_msgs/msg/Twist'],
                          'title': '底盘速度'})
        self.assertEqual((items['base.odometry']['name'], items['base.odometry']['types']), ('/odom', ['nav_msgs/msg/Odometry']))

    def test_an_arm_reports_its_joints_and_serves_a_trajectory_action(self):
        items = {item['capability']: item for item in motion_drivers.expected(MANIPULATOR)}

        self.assertEqual((items['joint.state']['kind'], items['joint.state']['types']), ('topic', ['sensor_msgs/msg/JointState']))
        self.assertEqual((items['arm.joint_position']['kind'], items['arm.joint_position']['types']),
                         ('action', ['control_msgs/action/FollowJointTrajectory']))

    def test_an_interface_the_standard_binds_to_no_ros2_interface_is_named_as_such(self):
        items = {item['capability']: item for item in motion_drivers.expected(QUADRUPED)}

        for cap_id in ('safety.stop', 'legged.posture'):
            self.assertIsNone(items[cap_id]['kind'], cap_id)
            self.assertIsNone(ros2_interfaces.get(cap_id))
            self.assertTrue(items[cap_id]['note'], 'why the platform cannot check it')


class AssessTests(unittest.TestCase):
    """The comparison itself: a body type against the ROS 2 part of a profile."""

    def test_a_device_with_its_motion_driver_running_lacks_nothing(self):
        result = motion_drivers.assess(QUADRUPED, part(ros2()))

        self.assertEqual(result['status'], 'ok')
        items = by_capability(result)
        self.assertEqual((items['base.velocity']['state'], items['base.velocity']['found']),
                         ('present', [{'name': '/cmd_vel', 'types': ['geometry_msgs/msg/Twist']}]))
        self.assertEqual(items['base.odometry']['state'], 'present')
        self.assertEqual(motion_drivers.issues(result), [])

    def test_a_quadruped_without_its_motion_driver_is_told_which_interfaces_are_missing(self):
        result = motion_drivers.assess(QUADRUPED, part(chassis('/cmd_vel', '/odom')))

        self.assertEqual(result['status'], 'missing')
        self.assertEqual([item['capability'] for item in result['items'] if item['state'] == 'missing'],
                         ['base.velocity', 'base.odometry'])
        issues = {issue['id']: issue for issue in motion_drivers.issues(result)}
        self.assertEqual(list(issues), [VELOCITY, ODOMETRY])
        velocity = issues[VELOCITY]
        self.assertEqual(velocity['expected'], {'kind': 'topic', 'role': 'subscribes', 'name': '/cmd_vel', 'types': ['geometry_msgs/msg/Twist']})
        self.assertEqual((velocity['capability'], velocity['state']), ('base.velocity', 'missing'))
        for said in ('底盘速度', 'base.velocity'):
            self.assertIn(said, velocity['title'])
        for said in ('四足机器人', '/cmd_vel', 'geometry_msgs/msg/Twist', '话题'):
            self.assertIn(said, velocity['detail'])
        self.assertRegex(velocity['detail'], '没有安装.*没有在运行|没有在运行.*没有安装')

    def test_only_what_is_missing_is_reported(self):
        result = motion_drivers.assess(QUADRUPED, part(chassis('/odom')))

        self.assertEqual([issue['id'] for issue in motion_drivers.issues(result)], [ODOMETRY])
        self.assertEqual(by_capability(result)['base.velocity']['state'], 'present')

    def test_an_arm_without_its_trajectory_controller_is_told_the_action_is_missing(self):
        result = motion_drivers.assess(MANIPULATOR, part(arm(ARM_ACTION)))

        self.assertEqual(result['status'], 'missing')
        issue, = motion_drivers.issues(result)
        self.assertEqual(issue['id'], ARM)
        self.assertEqual((issue['expected']['kind'], issue['expected']['types']), ('action', ['control_msgs/action/FollowJointTrajectory']))
        for said in ('机械臂', '动作', 'control_msgs/action/FollowJointTrajectory'):
            self.assertIn(said, issue['detail'])
        self.assertEqual(by_capability(result)['joint.state']['state'], 'present')

    def test_an_arm_with_joint_states_and_the_trajectory_action_lacks_nothing(self):
        result = motion_drivers.assess(MANIPULATOR, part(arm()))

        self.assertEqual((result['status'], motion_drivers.issues(result)), ('ok', []))
        self.assertEqual(by_capability(result)['arm.joint_position']['found'],
                         [{'name': ARM_ACTION, 'types': ['control_msgs/action/FollowJointTrajectory']}])

    def test_a_chassis_robot_registered_as_an_arm_misses_the_arm_action(self):
        result = motion_drivers.assess(MANIPULATOR, part(ros2()))          # /joint_states is there, the action is not

        self.assertEqual([issue['id'] for issue in motion_drivers.issues(result)], [ARM])

    def test_the_interface_counts_under_another_name_when_the_type_is_right(self):
        # Robots namespace and rename their topics; which topic a device really uses is the interface table's business.
        renamed = chassis(replace=(('/cmd_vel [', '/base/cmd_vel_safe ['), ('/odom [', '/odometry/filtered [')))

        result = motion_drivers.assess(QUADRUPED, part(renamed))

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(by_capability(result)['base.velocity']['found'][0]['name'], '/base/cmd_vel_safe')

    def test_the_usual_topic_with_another_message_type_is_reported_with_the_type_it_has(self):
        stamped = chassis(replace=(('/cmd_vel [geometry_msgs/msg/Twist]', '/cmd_vel [geometry_msgs/msg/TwistStamped]'),))

        result = motion_drivers.assess(QUADRUPED, part(stamped))

        item = by_capability(result)['base.velocity']
        self.assertEqual((item['state'], item['found']), ('wrong_type', [{'name': '/cmd_vel', 'types': ['geometry_msgs/msg/TwistStamped']}]))
        issue, = motion_drivers.issues(result)
        self.assertEqual((issue['id'], issue['state']), (VELOCITY, 'wrong_type'))
        for said in ('geometry_msgs/msg/TwistStamped', 'geometry_msgs/msg/Twist', '/cmd_vel'):
            self.assertIn(said, issue['detail'])

    def test_a_topic_with_two_types_counts_when_one_of_them_is_right(self):
        both = chassis(replace=(('/cmd_vel [geometry_msgs/msg/Twist]', '/cmd_vel [geometry_msgs/msg/TwistStamped, geometry_msgs/msg/Twist]'),))

        self.assertEqual(motion_drivers.assess(QUADRUPED, part(both))['status'], 'ok')

    def test_what_the_platform_cannot_check_is_never_reported_as_missing(self):
        result = motion_drivers.assess(QUADRUPED, part(chassis('/cmd_vel', '/odom')))

        items = by_capability(result)
        for cap_id in ('safety.stop', 'legged.posture'):
            self.assertEqual(items[cap_id]['state'], 'unverified', cap_id)
        self.assertNotIn('motion-driver:legged.posture', {issue['id'] for issue in motion_drivers.issues(result)})

    def test_a_device_without_ros2_misses_every_interface_and_is_told_why(self):
        result = motion_drivers.assess(QUADRUPED, part(absent()))

        self.assertEqual(result['status'], 'missing')
        issues = motion_drivers.issues(result)
        self.assertEqual([issue['id'] for issue in issues], [VELOCITY, ODOMETRY])
        self.assertIn('没有读到 ROS 2', issues[0]['detail'])

    def test_ros2_installed_with_nothing_running_says_that_nothing_runs(self):
        result = motion_drivers.assess(QUADRUPED, part(idle()))

        self.assertEqual(result['status'], 'missing')
        detail = motion_drivers.issues(result)[0]['detail']
        self.assertIn('没有读到正在运行的节点', detail)
        self.assertIn('默认设置', detail, 'the read used the defaults: nodes of another account or domain would not show')

    def test_another_distribution_is_not_judged(self):
        foxy = ros2(installed=(('foxy', 1, 0),), distro='foxy').replace('distro=humble', 'distro=foxy')

        for text, named in ((foxy, 'foxy'), (ros1_only(), 'noetic')):
            result = motion_drivers.assess(QUADRUPED, part(text))
            self.assertEqual((result['status'], motion_drivers.issues(result)), ('unknown', []), named)
            self.assertIn(named, result['reason'])
            self.assertIn('Humble', result['reason'])

    def test_a_listing_that_was_not_read_in_full_proves_nothing(self):
        cut = part(ros2(topics=listing('', code=124)))                  # the topic listing timed out on the robot
        truncated = part(ros2(end=False))
        never = {**ros2_profile.NOT_READ, 'checked_at': None, 'duration_ms': None, 'problem': 'SSH 会话中断'}

        for ros in (cut, truncated, never):
            result = motion_drivers.assess(QUADRUPED, ros)
            self.assertEqual((result['status'], motion_drivers.issues(result)), ('unknown', []), ros['status'])
            self.assertTrue(result['reason'])
        self.assertIn('SSH 会话中断', motion_drivers.assess(QUADRUPED, never)['reason'])

    def test_a_type_outside_the_list_is_not_guessed(self):
        result = motion_drivers.assess(None, part(chassis('/cmd_vel')), family='mobile')

        self.assertEqual((result['status'], result['items'], motion_drivers.issues(result)), ('unlisted_type', [], []))
        self.assertIn('本体类型清单', result['reason'])
        self.assertIn('改成清单里的类型', result['reason'])

    def test_no_profile_yet_means_not_checked(self):
        result = motion_drivers.assess(QUADRUPED, None)

        self.assertEqual((result['status'], motion_drivers.issues(result)), ('not_read', []))
        self.assertIn('设备档案', result['reason'])

    def test_a_type_somebody_added_is_checked_against_its_own_interfaces(self):
        custom = {'id': 'bodytype-x', 'name': '轮式底盘', 'required_capabilities': ['safety.stop', 'base.velocity', 'sensor.imu'], 'builtin': False}

        result = motion_drivers.assess(custom, part(chassis('/imu/data')))

        issue, = motion_drivers.issues(result)
        self.assertEqual((issue['id'], issue['expected']['types']), ('motion-driver:sensor.imu', ['sensor_msgs/msg/Imu']))
        self.assertIn('轮式底盘', issue['detail'])

    def test_a_type_with_nothing_the_platform_can_check_says_so(self):
        custom = {'id': 'bodytype-y', 'name': '只会停', 'required_capabilities': ['safety.stop'], 'builtin': False}

        for ros in (part(ros2()), part(ros2(end=False)), None):
            result = motion_drivers.assess(custom, ros)
            self.assertEqual((result['status'], motion_drivers.issues(result)), ('nothing_to_check', []))
            self.assertIn('核对不了', result['reason'])
            self.assertNotIn(result['status'], motion_drivers.UNDECIDED, 'a verdict about the type, not about the data')

    def test_an_issue_has_the_shape_of_a_scan_issue_and_can_be_handed_to_the_assistant(self):
        scan_issue = hardware_scan.parse(jetson())['issues'][0]
        result = motion_drivers.assess(QUADRUPED, part(chassis('/cmd_vel')))

        issue, = motion_drivers.issues(result)

        self.assertLessEqual(set(scan_issue), set(issue))
        self.assertEqual((issue['severity'], issue['category'], issue['fixable'], issue['source'], issue['device_id']),
                         ('error', 'motion', 'auto', 'ros2_profile', None))
        self.assertLessEqual(len(issue['title']), 200)
        self.assertLessEqual(len(issue['detail']), 800)
        self.assertLessEqual(len(issue['hint']), 600)
        json.dumps(issue, allow_nan=False)

    def test_names_read_from_a_device_are_quoted_as_data_and_bounded(self):
        flood = ''.join(f'/cmd_vel_{n} [geometry_msgs/msg/Twist]\n' for n in range(50))
        result = motion_drivers.assess(QUADRUPED, part(ros2(topics=listing(flood + '/odom [nav_msgs/msg/Odometry]\n'))))

        found = by_capability(result)['base.velocity']['found']
        self.assertLessEqual(len(found), motion_drivers.FOUND_MAX)
        self.assertEqual(by_capability(result)['base.velocity']['found_total'], 50)


class DriverCase(SshCase):
    def setUp(self):
        super().setUp()
        self.target.command_outputs.update({device_software.SOFTWARE_EXEC: software().encode(),
                                            hardware_scan.SCAN_EXEC: jetson().encode()})
        self.hw, self.drivers = self.platform.hardware, self.platform.motion_drivers

    def robot_with(self, ros_output, **extra):
        """A robot connected over SSH whose fixed ROS 2 command prints ``ros_output``."""
        self.ros(ros_output)
        return self.connect_directly(watch_services=SERVICES, **extra)

    def ros(self, output):
        self.target.command_outputs[ros2_profile.ROS2_EXEC] = output.encode()

    def sent_since(self, count):
        """Commands the robot received after the first ``count``, without issue #24's heartbeat (it reads nothing)."""
        return [command for command in self.target.commands[count:] if command != heartbeat.HEARTBEAT_COMMAND]

    def read_profile(self, connection):
        return self.platform.device_profile.read({'project_id': self.pid, 'robot_id': connection['robot_id']})

    def scan(self, connection, **extra):
        return self.hw.scan({'project_id': self.pid, 'robot_id': connection['robot_id'], **extra})

    def check(self, connection):
        return next((c for c in self.platform.snapshot()['motion_driver_checks'] if c['robot_id'] == connection['robot_id']), None)

    def driver_issues(self, scan):
        return [issue for issue in scan['issues'] if issue['id'].startswith(motion_drivers.ISSUE_PREFIX)]

    def session(self, connection, scan, issue_ids):
        s = {'id': 'agent-session-drivers', 'project_id': self.pid, 'robot_id': connection['robot_id'], 'connection_id': connection['id'],
             'mode': 'repair', 'status': 'running', 'events': [],
             'hardware': {'scan_id': scan['id'], 'issue_ids': list(issue_ids), 'skill': hardware_init.skill_info(), 'fixes': 0,
                          'fixed_issue_ids': [], 'rescan_id': None, 'rescan_after_fixes': None, 'ssh_interface': 'eth0'}}
        self.store.put('agent_sessions', s)
        return s

    def rescan(self, s):
        return self.hw.tool(s['id'], s, 'hardware_rescan', {}, None)


class OnboardingTests(DriverCase):
    """The acceptance of the issue: a device without its motion driver is told what is missing."""

    def test_a_quadruped_without_its_motion_driver_is_told_what_is_missing_once_its_profile_is_read(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.assertEqual(self.check(connection)['status'], 'not_read', 'nothing is claimed before the profile is read')

        self.read_profile(connection)

        check = self.check(connection)
        self.assertEqual((check['status'], check['project_id'], check['body_type']),
                         ('missing', self.pid, {'id': 'quadruped', 'name': '四足机器人'}))
        missing = [item for item in check['items'] if item['state'] == 'missing']
        self.assertEqual([(item['capability'], item['kind'], item['name'], item['types'], item['issue_id']) for item in missing],
                         [('base.velocity', 'topic', '/cmd_vel', ['geometry_msgs/msg/Twist'], VELOCITY),
                          ('base.odometry', 'topic', '/odom', ['nav_msgs/msg/Odometry'], ODOMETRY)])
        self.assertAlmostEqual(check['checked_at'], self.read_profile(connection)['ros2']['checked_at'], delta=30)

    def test_an_arm_without_its_trajectory_controller_is_told_what_is_missing(self):
        connection = self.robot_with(arm(ARM_ACTION), family='manipulator', name='Arm on the bench')

        self.read_profile(connection)

        check = self.check(connection)
        self.assertEqual((check['status'], check['body_type']['name']), ('missing', '机械臂'))
        self.assertEqual([(item['capability'], item['kind'], item['types']) for item in check['items'] if item['state'] == 'missing'],
                         [('arm.joint_position', 'action', ['control_msgs/action/FollowJointTrajectory'])])

    def test_a_device_with_its_drivers_gets_no_entry(self):
        for family, output in (('quadruped', ros2()), ('manipulator', arm())):
            with self.subTest(family):
                connection = self.robot_with(output, family=family, name='Complete ' + family)
                self.read_profile(connection)

                check = self.check(connection)
                scan = self.scan(connection)

                self.assertEqual(check['status'], 'ok')
                self.assertFalse([item for item in check['items'] if item['state'] in ('missing', 'wrong_type')])
                self.assertEqual(self.driver_issues(scan), [])

    def test_a_device_of_a_type_outside_the_list_is_asked_to_change_its_type_first(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        robot = self.store.get('robots', connection['robot_id'])
        self.store.put('robots', {**robot, 'family': 'mobile'}, owner=True)       # registered before the list existed
        self.read_profile(connection)

        check = self.check(connection)
        scan = self.scan(connection)

        self.assertEqual((check['status'], check['body_type'], check['family'], check['items']), ('unlisted_type', None, 'mobile', []))
        self.assertIn('改成清单里的类型', check['reason'])
        self.assertEqual(self.driver_issues(scan), [], 'nothing is guessed for a type the list does not know')
        self.assertEqual(scan['motion_drivers']['status'], 'unlisted_type')

        self.platform.body_types.assign({'project_id': self.pid, 'robot_id': connection['robot_id'], 'family': 'quadruped'})

        self.assertEqual(self.check(connection)['status'], 'missing', 'the kept profile is judged as soon as the type is one of the list')

    def test_simulated_robots_and_robots_without_ssh_are_not_checked(self):
        other = self.platform.add_robot({'project_id': self.pid, 'name': 'No SSH', 'family': 'quadruped'})

        self.assertNotIn(other['id'], {c['robot_id'] for c in self.platform.snapshot()['motion_driver_checks']})

    def test_a_profile_read_over_a_connection_that_was_changed_since_is_not_judged(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)
        self.assertEqual(self.check(connection)['status'], 'missing')
        saved = self.store.get('device_connections', connection['id'])
        self.store.put('device_connections', {**saved, 'updated_at': '2031-01-01T00:00:00+00:00'})

        self.assertIsNone(self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': connection['robot_id']})['profile'])
        self.assertEqual(self.check(connection)['status'], 'not_read')

    def test_the_snapshot_stays_the_same_between_reads(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)

        first, second = (json.dumps(self.platform.snapshot()['motion_driver_checks'], sort_keys=True) for _ in range(2))

        self.assertEqual(first, second, 'nothing in it changes on a timer: the page is not redrawn')

    def test_the_check_goes_with_the_robot(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertEqual(self.platform.snapshot()['motion_driver_checks'], [])


class ScanIssueTests(DriverCase):
    """A missing interface is a problem of a new kind in the hardware scan record: that is how it reaches the repair flow."""

    def test_a_scan_after_the_profile_lists_the_missing_interfaces_as_problems(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.read_profile(connection)
        hardware_only = hardware_scan.parse(jetson())

        scan = self.scan(connection)

        issues = self.driver_issues(scan)
        self.assertEqual([issue['id'] for issue in issues], [VELOCITY, ODOMETRY])
        self.assertEqual({issue['source'] for issue in issues}, {'ros2_profile'})
        self.assertEqual(len(scan['issues']), len(hardware_only['issues']) + 2, 'the hardware problems are all still there')
        self.assertEqual(scan['summary']['errors'], hardware_only['summary']['errors'] + 2)
        self.assertEqual([issue['severity'] for issue in scan['issues']],
                         sorted((issue['severity'] for issue in scan['issues']), key=lambda s: -hardware_scan.SEVERITY_ORDER[s]))
        self.assertEqual(scan['motion_drivers']['status'], 'missing')
        self.assertEqual(self.store.get('hardware_scans', scan['id'])['issues'], scan['issues'])

    def test_the_scan_command_itself_has_no_ros2_in_it(self):
        self.assertNotIn('ros2', hardware_scan.SCAN_COMMAND)
        self.assertNotIn('source', hardware_scan.SCAN_COMMAND.split())

    def test_a_scan_of_a_robot_without_driver_problems_runs_no_ros2_command(self):
        connection = self.robot_with(ros2())
        self.read_profile(connection)
        sent = len(self.target.commands)

        scan = self.scan(connection)
        self.scan(connection, source='connect')

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC] * 2, 'the interfaces come from the kept profile')
        self.assertEqual((scan['motion_drivers']['status'], scan['motion_drivers']['reverified']), ('ok', False))

    def test_a_scan_before_any_profile_claims_nothing_about_drivers(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))

        scan = self.scan(connection, source='connect')

        self.assertEqual(self.driver_issues(scan), [])
        self.assertEqual(scan['motion_drivers']['status'], 'not_read')
        self.assertEqual(scan['issues'], hardware_scan.parse(jetson())['issues'])
        self.assertNotIn(ros2_profile.ROS2_EXEC, self.target.commands)

    def test_a_scan_of_a_robot_with_known_driver_problems_reads_ros2_again(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)
        sent = len(self.target.commands)

        scan = self.scan(connection)

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC],
                         'the scan, then the fixed read-only ROS 2 command: whether a driver problem is gone is not for the scan to say')
        self.assertEqual((scan['motion_drivers']['status'], scan['motion_drivers']['reverified']), ('missing', True))
        self.assertEqual([issue['id'] for issue in self.driver_issues(scan)], [VELOCITY])

    def test_scanning_again_sees_a_driver_that_was_started_on_site(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        before = self.read_profile(connection)['ros2']['checked_at']
        self.assertEqual([issue['id'] for issue in self.driver_issues(self.scan(connection))], [VELOCITY])

        self.ros(ros2())
        scan = self.scan(connection)

        self.assertEqual(self.driver_issues(scan), [])
        self.assertEqual(self.check(connection)['status'], 'ok')
        kept = self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': connection['robot_id']})['profile']
        self.assertGreater(kept['ros2']['checked_at'], before, 'the kept profile shows what the scan read')
        self.assertIn('/cmd_vel', [row['name'] for row in kept['ros2']['topics']])
        audit = [e for e in self.store.list('events') if e['action'] == 'device.motion_drivers_reverified']
        self.assertEqual((audit[0]['details']['status'], audit[0]['details']['missing']), ('ok', []))

    def test_a_ros2_read_that_fails_during_a_scan_keeps_the_known_problems(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)
        self.ros(ros2())                                # the driver is there, but the read does not get through
        failures = ((device_profile._NotRead(device_profile.ROS2_NOT_FINISHED), device_profile.ROS2_NOT_FINISHED.rstrip('。')),
                    (OSError('connection reset'), device_profile.ROS2_NO_SESSION.rstrip('。')))

        for failure, said in failures:
            with self.subTest(said), mock.patch.object(device_profile, 'read_ros2', side_effect=failure):
                scan = self.scan(connection)

                self.assertEqual([issue['id'] for issue in self.driver_issues(scan)], [VELOCITY], 'not read is not the same as gone')
                self.assertEqual((scan['motion_drivers']['reverified'], scan['motion_drivers']['problem']), (False, said))
                self.assertTrue(any('运控驱动' in note and said in note for note in scan['notes']))
                self.assertNotIn('connection reset', json.dumps(scan, ensure_ascii=False))
        self.assertEqual(self.check(connection)['status'], 'missing')

    def test_the_problem_can_be_handed_to_the_assistant(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.read_profile(connection)
        scan = self.scan(connection)
        request = {'project_id': self.pid, 'robot_id': connection['robot_id'], 'scan_id': scan['id'], 'issue_ids': [VELOCITY, ODOMETRY]}

        self.hw.policy.check_selectable(connection['robot_id'], scan, [VELOCITY, ODOMETRY])
        self.assertNotIn(VELOCITY, {block['issue_id'] for block in self.platform.snapshot()['hardware_blocks']})
        # The request gets past the choice of problems: what stops it next is the consent the form asks for.
        with self.assertRaisesRegex(ValueError, '请授权读取设备信息'):
            self.hw.start_repair(request)
        with self.assertRaisesRegex(ValueError, '请选择本次扫描中要处理的问题'):
            self.hw.start_repair({**request, 'issue_ids': ['motion-driver:gripper.position']})

    def test_the_assistant_is_given_the_expected_interface_as_data(self):
        connection = self.robot_with(arm(ARM_ACTION), family='manipulator')
        self.read_profile(connection)
        scan = self.scan(connection)
        s = self.session(connection, scan, [ARM])

        with tempfile.TemporaryDirectory() as work:
            self.hw.prepare_workspace(s, Path(work))
            handed = json.loads((Path(work) / 'HARDWARE_SCAN.json').read_text())

        issue, = handed['selected_issues']
        self.assertEqual((issue['id'], issue['expected']), (ARM, {'kind': 'action', 'role': 'serves',
                                                                    'name': ros2_interfaces.get('arm.joint_position').name,
                                                                    'types': ['control_msgs/action/FollowJointTrajectory']}))
        self.assertEqual(handed['scan']['motion_drivers']['status'], 'missing')


class UndecidedTests(DriverCase):
    """When the kept profile cannot be judged, known problems are not dropped: not knowing is not the same as gone."""

    def setUp(self):
        super().setUp()
        self.connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.read_profile(self.connection)
        self.first = self.scan(self.connection)
        self.assertEqual([issue['id'] for issue in self.driver_issues(self.first)], [VELOCITY, ODOMETRY])

    def mark(self):
        s = self.session(self.connection, self.first, [VELOCITY, ODOMETRY])
        self.hw.tool(s['id'], s, 'hardware_rescan', {}, None)
        results = [{'issue_id': x, 'outcome': 'needs_user', 'actions': '只读排查', 'verification': '重新读取 ROS 2', 'evidence_ids': []}
                   for x in (VELOCITY, ODOMETRY)]
        self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '运控驱动', 'results': results, 'followups': []}, None)
        self.store.delete('agent_sessions', s['id'])
        return {m['issue_id'] for m in self.store.list('hardware_issue_marks')}

    def test_an_incomplete_profile_read_does_not_drop_the_problems_from_the_next_scan(self):
        self.assertEqual(self.mark(), {VELOCITY, ODOMETRY})
        self.ros(ros2(end=False))                       # the profile is read again and the ROS 2 listing comes back cut
        self.read_profile(self.connection)
        self.assertEqual(self.check(self.connection)['status'], 'unknown')
        sent = len(self.target.commands)

        scan = self.scan(self.connection)

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC], 'ROS 2 is read again')
        self.assertEqual([issue['id'] for issue in self.driver_issues(scan)], [VELOCITY, ODOMETRY])
        self.assertFalse(scan['motion_drivers']['reverified'])
        self.assertTrue(any('运控驱动的问题按之前的结果保留' in note for note in scan['notes']))
        self.assertEqual({m['issue_id'] for m in self.store.list('hardware_issue_marks')}, {VELOCITY, ODOMETRY}, 'the marks stay')

    def test_a_scan_that_reads_no_ros2_keeps_the_problems_of_the_last_scan(self):
        # A re-investigation's scan saves through the same path and reads no ROS 2.
        self.ros(ros2(end=False))
        self.read_profile(self.connection)
        robot = self.store.get('robots', self.connection['robot_id'])
        saved = self.store.get('device_connections', self.connection['id'])

        record = self.hw._save(robot, saved, hardware_scan.parse(jetson()), {'stdout': ''}, 1, 'investigation')

        self.assertEqual([issue['id'] for issue in self.driver_issues(record)], [VELOCITY, ODOMETRY])
        self.assertEqual(record['motion_drivers']['status'], 'unknown')
        self.assertTrue(any(note.startswith(motion_drivers.KEPT_FROM_SCAN) for note in record['notes']))

    def test_after_the_connection_was_changed_the_next_scan_reads_ros2_over_the_new_one(self):
        saved = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**saved, 'updated_at': '2031-01-01T00:00:00+00:00'})
        self.assertEqual(self.check(self.connection)['status'], 'not_read')
        self.ros(ros2())
        sent = len(self.target.commands)

        scan = self.scan(self.connection)

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC])
        self.assertEqual((self.driver_issues(scan), scan['motion_drivers']['reverified']), ([], True))
        self.assertIsNone(self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': self.connection['robot_id']})['profile'],
                          'a read over the changed connection is not written into the profile of the old one')

    def test_a_scan_that_has_used_up_its_time_does_not_start_a_ros2_read(self):
        robot = self.store.get('robots', self.connection['robot_id'])
        saved = self.store.get('device_connections', self.connection['id'])
        kept = self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': robot['id']})['profile']['ros2']
        self.ros(ros2())
        sent = len(self.target.commands)

        parsed = self.platform.motion_drivers.recheck(robot, saved, hardware_scan.parse(jetson()), time.monotonic() + 1)

        self.assertEqual(self.sent_since(sent), [], 'no SSH session, no command')
        self.assertEqual([issue['id'] for issue in self.driver_issues(parsed)], [VELOCITY, ODOMETRY])
        self.assertEqual(parsed['motion_drivers']['problem'], motion_drivers.NO_TIME_LEFT)
        now = self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': robot['id']})['profile']['ros2']
        self.assertEqual(now, kept, 'a read that was never tried is not recorded in the profile as a failed one')

    def test_time_that_runs_out_while_logging_in_starts_no_ros2_command(self):
        robot = self.store.get('robots', self.connection['robot_id'])
        saved = self.store.get('device_connections', self.connection['id'])
        deadline = time.monotonic() + motion_drivers.READ_MIN_SECONDS + 0.5
        real = device_access.SSHDevice

        def slow_login(*args, **kwargs):
            device = real(*args, **kwargs)
            time.sleep(1)                               # the login took what was left of the request's time
            return device
        sent = len(self.target.commands)
        with mock.patch.object(device_access, 'SSHDevice', side_effect=slow_login):
            parsed = self.platform.motion_drivers.recheck(robot, saved, hardware_scan.parse(jetson()), deadline)

        self.assertEqual(self.sent_since(sent), [])
        self.assertEqual(parsed['motion_drivers']['problem'], motion_drivers.NO_TIME_LEFT)
        self.assertEqual([issue['id'] for issue in self.driver_issues(parsed)], [VELOCITY, ODOMETRY])

    def test_a_new_type_that_asks_for_nothing_checkable_drops_the_old_problems(self):
        self.assertEqual(self.mark(), {VELOCITY, ODOMETRY})
        stop_only = self.platform.body_types.add({'name': '只会停', 'state_machine_template': 'custom.stop',
                                                  'required_capabilities': ['safety.stop']})
        self.platform.body_types.assign({'project_id': self.pid, 'robot_id': self.connection['robot_id'], 'family': stop_only['id']})
        self.assertEqual(self.check(self.connection)['status'], 'nothing_to_check')
        sent = len(self.target.commands)

        scan = self.scan(self.connection)

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC], 'nothing to read ROS 2 for')
        self.assertEqual(self.driver_issues(scan), [], 'the problems the old type asked for are gone')
        self.assertEqual([m for m in self.store.list('hardware_issue_marks') if m['issue_id'].startswith(motion_drivers.ISSUE_PREFIX)], [])

    def test_a_read_that_runs_into_the_time_of_the_request_counts_as_not_done(self):
        self.target.unanswered.add(ros2_profile.ROS2_EXEC)        # the robot never answers the ROS 2 command
        started = time.monotonic()
        with mock.patch.object(motion_drivers, 'SCAN_REQUEST_SECONDS', 12):
            scan = self.scan(self.connection)

        self.assertLess(time.monotonic() - started, 25, 'the scan answered within its time, not the read time of 45 seconds')
        self.assertEqual([issue['id'] for issue in self.driver_issues(scan)], [VELOCITY, ODOMETRY])
        self.assertEqual(scan['motion_drivers']['problem'], device_profile.ROS2_NOT_FINISHED.rstrip('。'))

    def test_an_older_read_does_not_overwrite_a_newer_one_in_the_profile(self):
        robot = self.store.get('robots', self.connection['robot_id'])
        saved = self.store.get('device_connections', self.connection['id'])
        kept = self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': robot['id']})['profile']['ros2']
        stale = {**part(ros2()), 'checked_at': kept['checked_at'] - 60}

        self.platform.motion_drivers._keep(robot, saved, lambda earlier: stale, stale['checked_at'])

        now = self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': robot['id']})['profile']['ros2']
        self.assertEqual(now['checked_at'], kept['checked_at'])
        self.assertNotIn('/cmd_vel', [row['name'] for row in now['topics']])


class ReverifyTests(DriverCase):
    """After a repair the interfaces are checked with a fresh ROS 2 read, not by scanning the hardware again alone."""

    def setUp(self):
        super().setUp()
        self.connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.read_profile(self.connection)
        self.baseline = self.scan(self.connection)
        self.s = self.session(self.connection, self.baseline, [VELOCITY, ODOMETRY])
        self.sent = len(self.target.commands)

    def profile(self):
        return self.platform.device_profile.saved({'project_id': self.pid, 'robot_id': self.connection['robot_id']})['profile']

    def report(self, outcomes):
        results = [{'issue_id': issue_id, 'outcome': outcome, 'actions': '查了 ROS 2 的安装和驱动服务', 'verification': '重新读取 ROS 2',
                    'evidence_ids': []} for issue_id, outcome in outcomes.items()]
        return self.hw.tool(self.s['id'], self.s, 'save_repair_report', {'summary': '运控驱动', 'results': results, 'followups': []}, None)

    def test_the_rescan_reads_ros2_again_and_sees_the_interfaces_that_appeared(self):
        before = self.profile()['ros2']['checked_at']
        self.ros(ros2())                                # somebody installed and started the driver

        rescan = self.rescan(self.s)

        self.assertEqual(self.sent_since(self.sent), [hardware_scan.SCAN_EXEC, ros2_profile.ROS2_EXEC],
                         'the hardware scan, then the fixed read-only ROS 2 command, on the same fresh login')
        self.assertEqual(set(rescan['resolved_since_baseline']), {VELOCITY, ODOMETRY})
        self.assertEqual((rescan['motion_drivers']['reverified'], rescan['motion_drivers']['status']), (True, 'ok'))
        record = self.store.get('hardware_scans', rescan['scan_id'])
        self.assertEqual(self.driver_issues(record), [])
        self.assertEqual(record['source'], 'repair')
        self.assertGreater(self.profile()['ros2']['checked_at'], before, 'the kept profile shows what was just read')
        self.assertEqual(self.check(self.connection)['status'], 'ok')
        self.assertEqual(self.driver_issues(self.scan(self.connection)), [], 'a later scan does not bring the old problem back')

    def test_the_interfaces_are_still_missing_after_the_rescan(self):
        rescan = self.rescan(self.s)

        self.assertEqual(rescan['resolved_since_baseline'], [])
        self.assertEqual({issue['id'] for issue in rescan['issues']} & {VELOCITY, ODOMETRY}, {VELOCITY, ODOMETRY})
        with self.assertRaisesRegex(ValueError, '仍然存在'):
            self.report({VELOCITY: 'not_reproduced', ODOMETRY: 'not_reproduced'})

    def test_one_of_two_interfaces_appeared(self):
        self.ros(chassis('/odom'))

        rescan = self.rescan(self.s)

        self.assertEqual(rescan['resolved_since_baseline'], [VELOCITY])
        self.assertEqual(rescan['motion_drivers']['status'], 'missing')

    def test_a_ros2_read_that_fails_proves_nothing(self):
        self.ros(ros2())                                # the interfaces are there, but this read does not get through
        before = self.profile()['ros2']
        for broken in (ros2(end=False), ros2(topics=listing('', code=124)), 'garbage that is no listing at all'):
            with self.subTest(broken[-40:]):
                self.ros(broken)

                rescan = self.rescan(self.s)

                self.assertEqual(rescan['resolved_since_baseline'], [], 'not read is not the same as gone')
                self.assertEqual({issue['id'] for issue in rescan['issues']} & {VELOCITY, ODOMETRY}, {VELOCITY, ODOMETRY})
                self.assertFalse(rescan['motion_drivers']['reverified'])
                self.assertTrue(rescan['motion_drivers']['problem'])
                self.assertEqual(self.profile()['ros2']['problem'], rescan['motion_drivers']['problem'])
                self.assertTrue(any('运控驱动' in note for note in rescan['notes']))
                with self.assertRaisesRegex(ValueError, '仍然存在'):
                    self.report({VELOCITY: 'not_reproduced', ODOMETRY: 'not_reproduced'})
        self.assertEqual(self.profile()['ros2']['topics'], before['topics'], 'the kept listing is the last one that was read')

    def test_a_ros2_command_that_never_returns_proves_nothing(self):
        self.ros(ros2())
        with mock.patch.object(device_profile, 'read_ros2', side_effect=device_profile._NotRead(device_profile.ROS2_NOT_FINISHED)):
            rescan = self.rescan(self.s)

        said = device_profile.ROS2_NOT_FINISHED.rstrip('。')
        self.assertEqual(rescan['resolved_since_baseline'], [])
        self.assertEqual((rescan['motion_drivers']['problem'], self.profile()['ros2']['problem']), (said, said))

    def test_a_session_about_hardware_problems_only_reads_no_ros2(self):
        self.ros(ros2())
        self.read_profile(self.connection)              # the drivers are there now
        clean = self.scan(self.connection)
        s = self.session(self.connection, clean, ['serial-perm:/dev/ttyUSB0'])
        sent = len(self.target.commands)

        rescan = self.rescan(s)

        self.assertEqual(self.sent_since(sent), [hardware_scan.SCAN_EXEC])
        self.assertFalse(rescan['motion_drivers']['reverified'])

    def test_fixed_needs_a_repair_command_like_any_other_problem(self):
        self.ros(ros2())
        self.rescan(self.s)

        with self.assertRaisesRegex(ValueError, 'not_reproduced'):
            self.report({VELOCITY: 'fixed', ODOMETRY: 'fixed'})
        saved = self.report({VELOCITY: 'not_reproduced', ODOMETRY: 'not_reproduced'})

        report = self.store.get('hardware_repairs', saved['repair_id'])
        self.assertEqual([result['title'] for result in report['results']],
                         [issue['title'] for issue in self.driver_issues(self.baseline)])

    def test_what_the_assistant_could_not_do_waits_for_a_person(self):
        # With the rules of this round the assistant may not install or start a motion driver: it reports needs_user.
        self.rescan(self.s)
        self.report({VELOCITY: 'needs_user', ODOMETRY: 'needs_user'})

        blocks = {block['issue_id']: block for block in self.platform.snapshot()['hardware_blocks']}
        self.assertEqual({blocks[VELOCITY]['state'], blocks[ODOMETRY]['state']}, {'blocked'})
        self.assertEqual(blocks[VELOCITY]['reason'], 'needs_user')
        latest = self.store.get('hardware_scans', self.store.get('agent_sessions', self.s['id'])['hardware']['rescan_id'])
        with self.assertRaisesRegex(ValueError, '现场处理'):
            self.hw.policy.check_selectable(self.connection['robot_id'], latest, [VELOCITY])

        self.ros(ros2())                                # a person installed and started the driver on site
        self.scan(self.connection)                      # "重新扫描" on the hardware page

        blocked = {block['issue_id'] for block in self.platform.snapshot()['hardware_blocks']}
        self.assertFalse(blocked & {VELOCITY, ODOMETRY}, 'gone in a complete scan: the mark goes with it')
        self.assertEqual([m for m in self.store.list('hardware_issue_marks') if m['issue_id'] in (VELOCITY, ODOMETRY)], [])


class SafetyTests(DriverCase):
    """The whole check is read-only: nothing that moves the robot is sent, and nothing unlocks it."""

    def test_only_the_fixed_read_only_commands_reach_the_robot(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        robot = self.store.get('robots', connection['robot_id'])
        self.read_profile(connection)
        scan = self.scan(connection)
        self.rescan(self.session(connection, scan, [VELOCITY, ODOMETRY]))

        self.assertLessEqual(set(self.target.commands), READ_ONLY)
        self.assertEqual(self.store.get('robots', connection['robot_id']), robot, 'no authorisation, no state of the robot changes')
        for kind in ('operations', 'deployments', 'model_deployments', 'jobs'):
            self.assertEqual(self.store.list(kind), [], kind)

    def test_the_ros2_command_cannot_publish_call_or_send_a_goal(self):
        for word in ('pub', 'call', 'send_goal', 'param set', 'run', 'launch'):
            self.assertNotRegex(ros2_profile.ROS2_COMMAND, r'\bros2\s+\S+\s+' + word + r'\b')

    def test_a_repair_of_a_driver_problem_still_cannot_publish_motion(self):
        connection = self.robot_with(chassis('/cmd_vel'))
        self.read_profile(connection)
        s = self.session(connection, self.scan(connection), [VELOCITY])
        saved = self.store.get('device_connections', connection['id'])
        fix = {'purpose': '试一下底盘', 'issue_ids': [VELOCITY], 'risk': 'low', 'rollback': '无', 'needs_root': False, 'timeout_seconds': 10}

        for command in ('ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.1}}"', 'ros2 topic pub /go std_msgs/msg/Empty',
                        'reboot'):
            with self.subTest(command), self.assertRaisesRegex(ValueError, '产品拒绝'):
                self.hw.fix_request(s, {**fix, 'command': command}, saved)
        accepted = self.hw.fix_request(s, {**fix, 'command': 'dpkg -l ros-humble-ros-base'}, saved)
        self.assertEqual(accepted['issue_ids'], [VELOCITY], 'a command about the driver problem is checked like any other')


class DriverRepairModel(ResponsesFixture):
    """Scripted model for a repair of motion driver problems, within the rules of this round: read what
    is expected, try to publish on the velocity topic (the product refuses), rescan, report needs_user."""
    requests = []
    issue_ids = [VELOCITY, ODOMETRY]

    def select_item(self, data, fallback):
        step = len(type(self).requests) - 1
        results = [{'issue_id': issue_id, 'outcome': 'needs_user', 'actions': '设备上有 ROS 2 Humble，没有找到这台本体的运控驱动包和服务',
                    'verification': '重新扫描时重新读取了 ROS 2 清单，接口仍然没有出现', 'evidence_ids': []} for issue_id in type(self).issue_ids]
        calls = [('exec_command', {'cmd': "grep -c 'geometry_msgs/msg/Twist' HARDWARE_SCAN.json > seen.txt"}),
                 ('device_fix', {'command': 'ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist "{}"', 'purpose': '试一下底盘是否响应',
                                 'issue_ids': type(self).issue_ids[:1], 'risk': 'low', 'rollback': '无', 'needs_root': False, 'timeout_seconds': 10}),
                 ('hardware_rescan', {}),
                 ('save_repair_report', {'summary': '运控驱动没有安装，需要你提供驱动安装包并在现场安装、启动', 'results': results,
                                         'followups': ['提供这台本体的运控驱动安装包', '在现场安装并启动运控驱动，然后重新核对']})]
        if step >= len(calls):
            return {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': '运控驱动需要你在现场处理，已保存报告。', 'annotations': []}]}
        name, args = calls[step]
        return {'id': f'fc_{step}', 'type': 'function_call', 'call_id': f'call_{step}', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}


class RepairSessionTests(DriverCase):
    """One repair session on motion driver problems through the bundled engine runtime and a scripted model."""

    def poll(self, sid, predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = self.store.get('agent_sessions', sid)
            if predicate(s):
                return s
            time.sleep(.05)
        self.fail('Timed out: ' + json.dumps(s, ensure_ascii=False)[:3000])

    @unittest.skipUnless(agent_runtime.binary_path(), 'Bundled runtime required')
    def test_the_assistant_is_handed_the_problem_rescans_with_ros2_and_leaves_it_to_a_person(self):
        connection = self.robot_with(chassis('/cmd_vel', '/odom'))
        self.read_profile(connection)
        baseline = self.scan(connection)
        DriverRepairModel.requests, DriverRepairModel.issue_ids = [], [VELOCITY, ODOMETRY]
        server = Server(DriverRepairModel)
        self.addCleanup(server.close)
        model = self.engine.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent', 'base_url': server.url + '/v1',
                                       'api_key': secrets.token_hex(12)})
        sent = len(self.target.commands)

        s = self.hw.start_repair({'project_id': self.pid, 'robot_id': connection['robot_id'], 'scan_id': baseline['id'],
                                  'issue_ids': [VELOCITY, ODOMETRY], 'model_id': model['id'], 'data_authorized': True,
                                  'modify_authorized': True, 'auto_approve': True, 'max_tokens': 20000, 'max_minutes': 2})
        s = self.poll(s['id'], lambda s: s['status'] not in ('starting', 'running', 'waiting_input', 'cancelling'))

        self.assertEqual(s['status'], 'completed', s.get('error'))
        report = self.store.get('hardware_repairs', s['repair_report_id'])
        self.assertEqual({r['issue_id']: r['outcome'] for r in report['results']}, {VELOCITY: 'needs_user', ODOMETRY: 'needs_user'})
        self.assertEqual(report['fix_count'], 0, 'nothing was changed on the device')
        final = self.store.get('hardware_scans', report['final_scan_id'])
        self.assertEqual((final['source'], final['motion_drivers']['reverified'], final['motion_drivers']['status']), ('repair', True, 'missing'))
        reached = self.sent_since(sent)
        self.assertIn(ros2_profile.ROS2_EXEC, reached, 'the rescan read ROS 2 again')
        self.assertFalse([command for command in reached if 'pub' in command.split() or 'cmd_vel' in command and command != ros2_profile.ROS2_EXEC],
                         'no motion command reached the robot')
        errors = [e['message'] for e in s['events'] if e['kind'] == 'tool_error']
        self.assertTrue(any('不能发布运动或控制指令' in message for message in errors), errors)
        self.assertGreater(int(self.engine.artifact({'session_id': s['id'], 'path': 'seen.txt'})['content']), 0,
                           'the expected interface was in HARDWARE_SCAN.json')
        self.assertIn(VELOCITY, json.dumps(DriverRepairModel.requests[0], ensure_ascii=False), 'the first instruction names the problem')
        blocks = {block['issue_id']: block for block in self.platform.snapshot()['hardware_blocks']}
        self.assertEqual((blocks[VELOCITY]['state'], blocks[VELOCITY]['reason']), ('blocked', 'needs_user'))
        self.assertEqual(self.check(connection)['status'], 'missing', 'the entry stays until the interfaces are there')


class SkillTests(unittest.TestCase):
    def test_the_built_in_skill_says_what_to_do_with_a_missing_motion_interface(self):
        playbooks = hardware_init.load_skill()['references/playbooks.md']

        section = playbooks.split('## motion-driver:')[1].split('\n## ')[0]
        for said in ('needs_user', 'hardware_rescan', 'expected'):
            self.assertIn(said, section)
        self.assertRegex(section, '不要.*(启动|安装)')
        self.assertEqual(hardware_init.skill_info()['version'], '1.3.0', 'the Skill text changed: so does its version')

    def test_the_rules_of_the_repair_still_refuse_motion(self):
        # Issue #29 allows installing and starting the motion driver (tests/test_repair_driver_rules.py); motion stays refused.
        text = hardware_init.REPAIR_INSTRUCTIONS
        self.assertIn('发布运动或控制指令', text)
        skill = hardware_init.load_skill()['SKILL.md']
        self.assertIn('不发布运动或控制指令', skill)


class TemplateTests(unittest.TestCase):
    def test_the_standard_and_the_runtime_scaffold_name_the_same_interfaces(self):
        from neuxmind_runtime import scaffold

        compared = 0
        for cap_id, (role, message_type, name) in scaffold.ROS2_TEMPLATES.items():
            if message_type is None:
                self.assertIsNone(ros2_interfaces.get(cap_id), cap_id)
                continue
            interface = ros2_interfaces.get(cap_id)
            self.assertEqual(interface.types[0], ros2_interfaces.canonical_type(message_type), cap_id)
            self.assertEqual(interface.name, '/' + name.lstrip('/'), cap_id)
            self.assertEqual(interface.kind, 'action' if role == 'action' else 'topic', cap_id)
            # The scaffold speaks for the runtime (it publishes commands); the standard for the body's driver.
            self.assertEqual(interface.role, {'publish': 'subscribes', 'subscribe': 'publishes', 'action': 'serves'}[role], cap_id)
            compared += 1
        self.assertGreaterEqual(compared, 7)


if __name__ == '__main__':
    unittest.main()
