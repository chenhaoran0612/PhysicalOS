"""Outputs of the fixed ROS 2 command for the motion driver check (issue #28).

Built on ``ros2_fixture``: the same chassis robot with or without the topics of its motion driver,
and an arm whose trajectory controller does or does not run. Names are made up.
"""
from ros2_fixture import DEFAULT_ENV, TOPICS, listing, ros2

ARM_ACTION = '/arm_controller/follow_joint_trajectory'
ARM_TOPICS = '''/arm_controller/follow_joint_trajectory/_action/feedback [control_msgs/action/FollowJointTrajectory_FeedbackMessage]
/arm_controller/follow_joint_trajectory/_action/status [action_msgs/msg/GoalStatusArray]
/joint_states [sensor_msgs/msg/JointState]
/parameter_events [rcl_interfaces/msg/ParameterEvent]
/robot_description [std_msgs/msg/String]
/rosout [rcl_interfaces/msg/Log]
/tf [tf2_msgs/msg/TFMessage]
/tf_static [tf2_msgs/msg/TFMessage]
'''
ARM_NODES = '''/arm_controller
/controller_manager
/joint_state_broadcaster
/robot_state_publisher
'''
ARM_SERVICES = '''/controller_manager/list_controllers [controller_manager_msgs/srv/ListControllers]
'''
# What the listing tool itself puts on an otherwise empty network.
TOOL_ONLY = '''/parameter_events [rcl_interfaces/msg/ParameterEvent]
/rosout [rcl_interfaces/msg/Log]
'''


def drop(topics, *names):
    """The listing without the named topics; an action's name removes its hidden topics too."""
    kept = [line for line in topics.splitlines()
            if not any(line.split(' ')[0] == name or line.startswith(name + '/') for name in names)]
    return '\n'.join(kept) + '\n'


def chassis(*missing, replace=()):
    """The chassis robot of ``ros2_fixture`` without the named topics; ``replace`` is (old text, new text) pairs."""
    topics = drop(TOPICS, *missing)
    for old, new in replace:
        topics = topics.replace(old, new)
    return ros2(topics=listing(topics))


def arm(*missing):
    return ros2(nodes=listing(ARM_NODES), topics=listing(drop(ARM_TOPICS, *missing)), services=listing(ARM_SERVICES))


def idle():
    """ROS 2 Humble is installed and nothing of it runs: no node, only the tool's own topics."""
    return ros2(env=DEFAULT_ENV, nodes=listing(''), topics=listing(TOOL_ONLY), services=listing(''))
