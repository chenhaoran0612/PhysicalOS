# ROS 2 test double (NOT ROS 2)

A minimal, pure-Python stand-in for the parts of `rclpy` and the message
packages used by `neuxmind_runtime.adapters.ros2` and by generated ROS 2
adapters. It exists so the conformance suite can drive the ROS 2 bridge in
CI. It is put on `sys.path` by the tests only and is never installed.

What it simulates: an in-memory topic graph (synchronous delivery), and one
fake differential-drive robot (`fake_robot.py`) that integrates `/cmd_vel`,
publishes `/odom`, `/battery_state`, `/imu`, `/joint_states` and `/amcl_pose` (map frame = odom) at 25 Hz and
serves a Nav2-like `navigate_to_pose` action (straight-line driving,
cancellable, plus the cancel-all-goals service). `use_sim_time` is reported as
`true`, so manifests built on it are `simulated: true`.

What it does NOT simulate: DDS, QoS, executors' threading model, lifecycle
nodes, TF, real Nav2 behaviour trees, message type checking. Passing the
conformance suite on this double says nothing about a real ROS 2 robot.
