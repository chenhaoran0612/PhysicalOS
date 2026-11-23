# The ROS 2 bridge on a real ROS 2 Humble (issue #21)

`run.sh` starts the image `ros:humble-ros-base-jammy` without network, copies `runtime/` and `spec/`
into it, sources `/opt/ros/humble/setup.bash` and runs `check.py` with the image's Python 3.10:

```bash
runtime/tests/ros2_humble/run.sh <output directory>      # default ./artifacts/ros2_humble (git-ignored)
```

It prints one line per check and writes `results.json`, `runtime-main.log` and `runtime-qos.log` to the
output directory; the exit code is 1 when a check failed. About a minute.

**What runs.** A real rclpy node (`fake_base`) stands in for a base driver: it publishes `/odom` with the
sensor-data QoS (best effort, as many drivers do) and `/battery_state` reliably, and records every Twist
it receives on `/cmd_vel`. The runtime is started as its own process, `python3 -m neuxmind_runtime serve
--adapter ros2`, with a configuration file (`NEUXMIND_ROS2_CONFIG`), and talks to that node over DDS
(Fast DDS, the Humble default). Nothing here is a robot: the "base" moves nothing, and its `use_sim_time`
is false, so the runtime treats it like a real body (`simulated: false`).

**What it checks.**

- The environment: Humble, Python 3.10, the RMW, and that the image has no Nav2.
- Refusals on real rclpy: `ROS_DISTRO=jazzy` and `nav2: true` without Nav2 stop the adapter with the reason.
- The installer (`deploy/device_install.sh`) against the image's real setup file: `NX_ADAPTER=ros2`
  passes the Humble check and reads the ROS settings; the image has no `ensurepip`, so the install stops
  at the venv and removes what it made; a setup file of another distribution is refused before anything
  is created. The full install (venv, imports, start in the recorded environment) is covered with a
  stand-in ROS in `tests/test_device_install_ros2.py`.
- One runtime with the default QoS: not simulated, mode `ros2`, motion blocked until limits are filled
  (issue #22); no `base.navigate_to` without Nav2; odometry and battery read from the real topics;
  health ok; on the DDS graph the runtime's `/odom` subscription is best effort and its `/cmd_vel`
  publisher reliable and volatile (discovery does not carry history or depth); a velocity command is
  refused with `safety_limits_missing` and nothing is published; `/v1/stop` answers and a zero Twist
  reaches the base; after filling the limits and a reset, a velocity command arrives clamped to them;
  the watchdog sends a zero Twist within the command timeout; SIGTERM ends the process cleanly.
- A second runtime configured with a reliable `/odom` subscription: the QoS reaches DDS, the reader gets
  nothing from the best-effort publisher (health names `odom`, the battery stays fresh), and ROS logs the
  incompatible QoS.

**Result** (2026-11-21, branch `feat/ros2-humble-adapter`): 31 passed, 0 failed.

**Not covered here:** a real Nav2 stack, other RMWs, a robot. A container is not a robot: reports
based on this say "not run on the real machine".
