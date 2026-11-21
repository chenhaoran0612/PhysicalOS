"""The ROS 2 binding of the standard capabilities (issue #28): one table for every component."""
import json
import re
import unittest

from neuxmind_spec import capabilities, ros2_interfaces

ROS_TYPE = re.compile(r'[a-z][a-z0-9_]*/(msg|action)/[A-Z][A-Za-z0-9]*')
ROS_NAME = re.compile(r'(?:/[a-z][a-z0-9_]*)+')


class TableTests(unittest.TestCase):
    def test_every_entry_binds_a_standard_capability_to_a_typed_topic_or_action(self):
        self.assertTrue(ros2_interfaces.INTERFACES)
        for cap_id, interface in ros2_interfaces.INTERFACES.items():
            with self.subTest(cap_id):
                self.assertEqual(interface.capability, cap_id)
                self.assertIn(cap_id, capabilities.CAPABILITIES)
                self.assertIn(interface.kind, ros2_interfaces.KINDS)
                self.assertIn(interface.role, ros2_interfaces.ROLES)
                self.assertTrue(interface.types)
                self.assertEqual(len(set(interface.types)), len(interface.types))
                for ros_type in interface.types:
                    self.assertRegex(ros_type, ROS_TYPE)
                    self.assertEqual(ros_type.split('/')[1], 'action' if interface.kind == 'action' else 'msg')
                self.assertRegex(interface.name, ROS_NAME)

    def test_the_role_follows_from_what_the_capability_is(self):
        for cap_id, interface in ros2_interfaces.INTERFACES.items():
            capability = capabilities.get(cap_id)
            with self.subTest(cap_id):
                if interface.kind == 'action':
                    self.assertEqual((capability.kind, interface.role), ('action', 'serves'))
                elif capability.kind in ('state', 'stream'):
                    self.assertEqual(interface.role, 'publishes', 'the driver sends what the body senses')
                else:
                    self.assertEqual(interface.role, 'subscribes', 'the driver receives a command')

    def test_the_interfaces_both_built_in_body_types_are_driven_through(self):
        velocity = ros2_interfaces.get('base.velocity')
        self.assertEqual((velocity.kind, velocity.role, velocity.types, velocity.name),
                         ('topic', 'subscribes', ('geometry_msgs/msg/Twist',), '/cmd_vel'))
        odometry = ros2_interfaces.get('base.odometry')
        self.assertEqual((odometry.kind, odometry.role, odometry.types, odometry.name),
                         ('topic', 'publishes', ('nav_msgs/msg/Odometry',), '/odom'))
        joints = ros2_interfaces.get('joint.state')
        self.assertEqual((joints.kind, joints.types, joints.name), ('topic', ('sensor_msgs/msg/JointState',), '/joint_states'))
        arm = ros2_interfaces.get('arm.joint_position')
        self.assertEqual((arm.kind, arm.role, arm.types), ('action', 'serves', ('control_msgs/action/FollowJointTrajectory',)))

    def test_a_capability_the_standard_binds_to_no_ros2_interface_has_no_entry(self):
        # A stop is carried by the motion interfaces (zero velocity, cancelled goals); a posture is a vendor's own service.
        for cap_id in ('safety.stop', 'system.health', 'legged.posture', 'arm.ee_pose'):
            self.assertIsNone(ros2_interfaces.get(cap_id), cap_id)
        self.assertIsNone(ros2_interfaces.get('no.such_capability'))
        self.assertIsNone(ros2_interfaces.get(None))

    def test_entries_cannot_be_changed_by_a_caller(self):
        interface = ros2_interfaces.get('base.velocity')
        with self.assertRaises(Exception):
            interface.name = '/other'
        with self.assertRaises(TypeError):
            ros2_interfaces.INTERFACES['base.velocity'] = interface


class TypeTests(unittest.TestCase):
    def test_one_spelling_for_a_type_however_it_was_written(self):
        for written in ('geometry_msgs/msg/Twist', 'geometry_msgs.msg:Twist', 'geometry_msgs/Twist', ' geometry_msgs/msg/Twist '):
            self.assertEqual(ros2_interfaces.canonical_type(written), 'geometry_msgs/msg/Twist', written)
        self.assertEqual(ros2_interfaces.canonical_type('nav2_msgs.action:NavigateToPose'), 'nav2_msgs/action/NavigateToPose')
        self.assertEqual(ros2_interfaces.canonical_type('control_msgs/action/FollowJointTrajectory'),
                         'control_msgs/action/FollowJointTrajectory')

    def test_what_is_not_a_type_name_is_refused(self):
        for written in ('', 'Twist', 'geometry_msgs/msg/', 'geometry_msgs/srv/Twist/extra', '/cmd_vel', 'a b/msg/C', None, 7,
                        'geometry_msgs/msg/Twist; reboot'):
            with self.subTest(written), self.assertRaises(ValueError):
                ros2_interfaces.canonical_type(written)

    def test_accepts_says_whether_a_topic_of_that_type_can_carry_the_capability(self):
        self.assertTrue(ros2_interfaces.accepts('base.velocity', 'geometry_msgs/msg/Twist'))
        self.assertTrue(ros2_interfaces.accepts('base.velocity', 'geometry_msgs.msg:Twist'))
        self.assertFalse(ros2_interfaces.accepts('base.velocity', 'geometry_msgs/msg/TwistStamped'))
        self.assertFalse(ros2_interfaces.accepts('base.velocity', 'nav_msgs/msg/Odometry'))
        self.assertTrue(ros2_interfaces.accepts('sensor.camera.rgb', 'sensor_msgs/msg/CompressedImage'))
        self.assertFalse(ros2_interfaces.accepts('legged.posture', 'std_msgs/msg/String'), 'no binding accepts nothing')
        self.assertFalse(ros2_interfaces.accepts('base.velocity', 'not a type'))


class RegistryTests(unittest.TestCase):
    def test_the_registry_is_plain_data_with_every_entry(self):
        registry = ros2_interfaces.registry()
        json.dumps(registry)
        self.assertEqual(registry['schema'], 'neuxmind.ros2_interfaces.v1')
        self.assertEqual(registry['spec_version'], capabilities.SPEC_VERSION)
        self.assertEqual(registry['distro'], 'humble')
        self.assertEqual([row['capability'] for row in registry['interfaces']], list(ros2_interfaces.INTERFACES))
        row = next(row for row in registry['interfaces'] if row['capability'] == 'base.velocity')
        self.assertEqual(row, {'capability': 'base.velocity', 'kind': 'topic', 'role': 'subscribes',
                               'types': ['geometry_msgs/msg/Twist'], 'name': '/cmd_vel'})


if __name__ == '__main__':
    unittest.main()
