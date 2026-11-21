"""Outputs of ``ros2_profile.ROS2_COMMAND`` for the device profile tests (issue #27).

Shaped like what the command prints on a robot running ROS 2 Humble: a chassis bridge, a lidar,
a colour camera and a navigation stack (names made up). ``TOPIC_LIST`` and ``ACTION_LIST`` are
written out by hand as ``ros2 topic list`` and ``ros2 action list -t`` print them for the same
graph, so that the parser is checked against the tool and not against itself.
"""

# Everything on the graph, as `ros2 topic list -t --include-hidden-topics` prints it.
TOPICS = '''/camera/color/camera_info [sensor_msgs/msg/CameraInfo]
/camera/color/image_raw [sensor_msgs/msg/Image]
/cmd_vel [geometry_msgs/msg/Twist]
/diagnostics [diagnostic_msgs/msg/DiagnosticArray]
/dock/_action/feedback [robot_interfaces/action/Dock_FeedbackMessage]
/dock/_action/status [action_msgs/msg/GoalStatusArray]
/imu/data [sensor_msgs/msg/Imu]
/joint_states [sensor_msgs/msg/JointState]
/navigate_to_pose/_action/feedback [nav2_msgs/action/NavigateToPose_FeedbackMessage]
/navigate_to_pose/_action/status [action_msgs/msg/GoalStatusArray]
/odom [nav_msgs/msg/Odometry]
/parameter_events [rcl_interfaces/msg/ParameterEvent]
/rosout [rcl_interfaces/msg/Log]
/scan [sensor_msgs/msg/LaserScan]
/sensors/lidar/points [sensor_msgs/msg/PointCloud2]
/status/text [std_msgs/msg/String, std_msgs/msg/Header]
/tf [tf2_msgs/msg/TFMessage]
/tf_static [tf2_msgs/msg/TFMessage]
'''

# `ros2 topic list` on the same graph: hidden topics (the actions' own) are not listed.
TOPIC_LIST = '''/camera/color/camera_info
/camera/color/image_raw
/cmd_vel
/diagnostics
/imu/data
/joint_states
/odom
/parameter_events
/rosout
/scan
/sensors/lidar/points
/status/text
/tf
/tf_static
'''

# `ros2 action list -t` on the same graph.
ACTION_LIST = '''/dock [robot_interfaces/action/Dock]
/navigate_to_pose [nav2_msgs/action/NavigateToPose]
'''

NODES = '''/bt_navigator
/camera/color_driver
/chassis_bridge
/controller_server
/robot_state_publisher
/sensors/lidar
'''

SERVICES = '''/bt_navigator/describe_parameters [rcl_interfaces/srv/DescribeParameters]
/bt_navigator/get_parameters [rcl_interfaces/srv/GetParameters]
/bt_navigator/set_parameters [rcl_interfaces/srv/SetParameters]
/chassis_bridge/get_parameters [rcl_interfaces/srv/GetParameters]
/chassis_bridge/set_mode [robot_interfaces/srv/SetMode]
/sensors/lidar/start_motor [std_srvs/srv/Empty]
/sensors/lidar/stop_motor [std_srvs/srv/Empty]
'''

# What the command found in the environment of a running node.
NODE_ENV = {'source': 'process', 'distro': 'humble', 'version': '2', 'domain': '42', 'rmw': 'rmw_cyclonedds_cpp',
            'localhost': '0', 'adopted': ' ROS_DOMAIN_ID RMW_IMPLEMENTATION ROS_LOCALHOST_ONLY CYCLONEDDS_URI'}
# No process with a ROS environment could be read: the defaults are used.
DEFAULT_ENV = {'source': 'default', 'distro': '', 'version': '', 'domain': '', 'rmw': '', 'localhost': '', 'adopted': ''}

TRACEBACK = '''Traceback (most recent call last):
  File "/opt/ros/humble/bin/ros2", line 33, in <module>
rclpy._rclpy_pybind11.RCLError: failed to initialize rcl: rmw implementation 'rmw_cyclonedds_cpp' is not installed
'''


def listing(output, code=0):
    """One section of a `ros2 ... list` call: its exit status, then what it printed."""
    return f'exit={code}\n{output}'


def ros2(installed=(('humble', 1, 0),), env=NODE_ENV, distro='humble', sourced='0', cli='ok',
         nodes=listing(NODES), topics=listing(TOPICS), services=listing(SERVICES), end=True):
    """The command's output. ``installed``: (name, has the ros2 command, has roscore) per directory of /opt/ros.
    ``distro``: the distribution whose setup file was chosen; ``sourced`` / ``cli`` / the three listings are
    None when the command did not get that far (no ROS 2 to load)."""
    lines = ['@@NX-ROS:install@@'] + [f'{name}|{ros2_cli}|{roscore}' for name, ros2_cli, roscore in installed]
    lines += ['@@NX-ROS:env@@'] + [f'{key}={value}' for key, value in env.items()]
    lines += ['@@NX-ROS:setup@@', f'distro={distro}']
    if sourced is not None:
        lines.append(f'sourced={sourced}')
    if cli is not None:
        lines.append(f'cli={cli}')
    text = '\n'.join(lines) + '\n'
    for name, section in (('nodes', nodes), ('topics', topics), ('services', services)):
        if section is not None:
            text += f'@@NX-ROS:{name}@@\n{section}'
            text += '' if text.endswith('\n') else '\n'
    return text + ('@@NX-ROS:end@@\n' if end else '')


def absent():
    """A device without ROS: nothing under /opt/ros, no process with a ROS environment."""
    return ros2(installed=(), env=DEFAULT_ENV, distro='', sourced=None, cli=None, nodes=None, topics=None, services=None)


def ros1_only():
    return ros2(installed=(('noetic', 0, 1),), env={**DEFAULT_ENV, 'source': 'process', 'distro': 'noetic', 'version': '1'},
                distro='noetic', sourced=None, cli=None, nodes=None, topics=None, services=None)
