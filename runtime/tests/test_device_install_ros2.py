"""``device_install.sh`` with a ROS 2 body (issue #21, R1-03): ``NX_ADAPTER=ros2 ... install``.

The install takes the simulated body unless told otherwise (``test_device_install``). With ``ros2`` it
first checks that the ROS environment it will run in is ROS 2 Humble and refuses anything else, saying
what it found; then it makes a venv that sees the system's packages (ROS 2's Python messages need the
system numpy), checks that rclpy imports there, and records the body and the ROS settings it was given.
``start`` runs the runtime in that recorded environment only.

ROS 2 itself is not here: the "ROS installations" below are setup files that export what a sourced
ROS sets and put the rclpy test double on PYTHONPATH (NOT ROS 2, see ros2_double/README.md). The same
script against a real ROS 2 Humble: ``tests/ros2_humble``. Linux only, like ``test_device_install``.
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from _support import ROS2_DOUBLE, http, wait_until

from test_device_install import SKIP_REASON, Device, alive, arguments, installed_device, snapshot

ROS_KEYS = ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "RMW_IMPLEMENTATION", "FASTRTPS_DEFAULT_PROFILES_FILE",
            "CYCLONEDDS_URI", "ZENOH_SESSION_CONFIG_URI")
DEFAULT_SETUP = Path("/opt/ros/humble/setup.bash")

# What a sourced ROS sets, as far as the script and the runtime care. The unguarded variable is there
# on purpose: the real setup files are not ``set -u`` safe either.
SETUP = """\
# stand-in for /opt/ros/{distro}/setup.bash (NOT ROS)
if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then echo "tracing"; fi
export ROS_VERSION={version}
export ROS_DISTRO={distro}
export PYTHONPATH="{pythonpath}${{PYTHONPATH:+:$PYTHONPATH}}"
"""


# The runtime makes itself non-dumpable (its token is in its environment), so /proc/<pid>/environ cannot be
# read. This probe, on the stand-in ROS's PYTHONPATH, writes down what the runtime started with instead.
PROBE = """\
import json, os, sys
if sys.argv[1:2] == ["serve"]:
    keys = {keys!r}
    with open({out!r}, "w") as fh:
        json.dump({{k: os.environ[k] for k in keys if k in os.environ}}, fh)
"""
PROBED = ("ROS_DISTRO", "ROS_VERSION", *ROS_KEYS, "NEUXMIND_ROS2_CONFIG", "PYTHONPATH")


class RosDevice(Device):
    """A device with stand-in ROS installations next to its home directory (``<base>/opt/ros/<distro>``)."""

    def __init__(self, wheels: bool = True):
        super().__init__(wheels=wheels)
        self.ros = self.home.parent / "opt" / "ros"
        self.probe_file = self.home.parent / "runtime-env.json"

    def runtime_env(self) -> dict[str, str]:
        """The environment the last started runtime began with (see PROBE)."""
        return json.loads(self.probe_file.read_text())

    def fake_ros(self, distro: str = "humble", version: int = 2, rclpy: bool = True, nav2: bool = True) -> Path:
        probe = self.home.parent / "probe"
        probe.mkdir(exist_ok=True)
        (probe / "sitecustomize.py").write_text(PROBE.format(keys=PROBED, out=str(self.probe_file)))
        paths = [str(probe)]
        if not nav2:                       # a package that fails to import, ahead of the double's nav2_msgs
            blocker = self.home.parent / "no-nav2" / "nav2_msgs"
            blocker.mkdir(parents=True, exist_ok=True)
            (blocker / "__init__.py").write_text('raise ImportError("nav2_msgs is not installed")\n')
            paths.append(str(blocker.parent))
        if rclpy:
            paths.append(str(ROS2_DOUBLE))
        else:
            empty = self.home.parent / "no-rclpy"
            empty.mkdir(exist_ok=True)
            paths.append(str(empty))
        setup = self.ros / distro / "setup.bash"
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_text(SETUP.format(distro=distro, version=version, pythonpath=":".join(paths)))
        return setup

    def profiles_file(self) -> Path:
        path = self.home.parent / "dds" / "profiles.xml"
        path.parent.mkdir(exist_ok=True)
        path.write_text("<profiles/>\n")
        return path


def ros_install(nav2: bool = True, device: RosDevice | None = None, **env: str) -> RosDevice:
    device = device or RosDevice()
    setup = device.fake_ros(nav2=nav2)
    installed = device.run("install", NX_ADAPTER="ros2", NX_ROS_SETUP=str(setup), **env)
    if installed.returncode != 0:
        device.close()
        raise AssertionError("ros2 install failed: " + installed.stdout + installed.stderr)
    device.installed = installed
    device.setup = setup
    return device


def manifest_ids(device: Device) -> list[str]:
    status, body = http(device.url, "/v2/manifest", token=device.token)
    assert status == 200, body
    return [c["id"] for c in body["capabilities"]]


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class Ros2InstallRefusalTests(unittest.TestCase):
    """A refused install says why and leaves nothing behind."""

    def device(self) -> RosDevice:
        device = RosDevice()
        self.addCleanup(device.close)
        return device

    def refused(self, device: RosDevice, **env: str):
        before = snapshot(device.home, device.package)
        result = device.run("install", NX_ADAPTER="ros2", **env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(device.root.exists())
        self.assertEqual(snapshot(device.home, device.package), before)
        return result

    def test_no_ros2_found_is_refused(self):
        device = self.device()

        result = self.refused(device, NX_ROS_SETUP=str(device.ros / "humble" / "setup.bash"))

        self.assertIn("ROS 2 Humble", result.stderr)
        self.assertIn("not found", result.stderr)

    def test_another_installed_distribution_is_named(self):
        device = self.device()
        device.fake_ros("jazzy")

        result = self.refused(device, NX_ROS_SETUP=str(device.ros / "humble" / "setup.bash"))

        self.assertIn("jazzy", result.stderr)
        self.assertIn("only ROS 2 Humble", result.stderr)

    def test_a_setup_file_of_another_distribution_is_refused(self):
        device = self.device()

        result = self.refused(device, NX_ROS_SETUP=str(device.fake_ros("jazzy")))

        self.assertIn("jazzy", result.stderr)
        self.assertIn("only ROS 2 Humble", result.stderr)

    def test_ros1_is_refused(self):
        device = self.device()

        result = self.refused(device, NX_ROS_SETUP=str(device.fake_ros("noetic", version=1)))

        self.assertIn("noetic", result.stderr)

    def test_the_setup_file_must_be_an_absolute_path(self):
        device = self.device()
        device.fake_ros()

        result = self.refused(device, NX_ROS_SETUP="opt/ros/humble/setup.bash")

        self.assertIn("absolute", result.stderr)

    def test_a_setup_file_others_can_write_is_refused(self):
        device = self.device()
        setup = device.fake_ros()
        opt = device.ros.parent                  # <base>/opt: an ancestor, two levels up
        for target, mode in ((setup, 0o666), (setup.parent, 0o777), (opt, 0o777)):
            with self.subTest(target=target.name):
                target.chmod(mode)
                try:
                    result = self.refused(device, NX_ROS_SETUP=str(setup))
                finally:
                    target.chmod(0o644 if target == setup else 0o755)

                self.assertIn("writable", result.stderr)

    def test_a_sticky_world_writable_ancestor_is_accepted(self):
        device = self.device()
        setup = device.fake_ros()
        opt = device.ros.parent
        opt.chmod(0o1777)                        # like /tmp: others cannot move what is in it
        self.addCleanup(opt.chmod, 0o755)

        result = device.run("install", NX_ADAPTER="ros2", NX_ROS_SETUP=str(setup))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ros_settings_that_could_be_anything_are_refused(self):
        device = self.device()
        setup = str(device.fake_ros())
        for key, value in (("ROS_DOMAIN_ID", "77 x"), ("ROS_DOMAIN_ID", "233"), ("ROS_DOMAIN_ID", "-1"),
                           ("ROS_LOCALHOST_ONLY", "yes"), ("RMW_IMPLEMENTATION", "rmw_x;id"),
                           ("FASTRTPS_DEFAULT_PROFILES_FILE", "profiles.xml"),
                           ("FASTRTPS_DEFAULT_PROFILES_FILE", str(device.home.parent / "missing.xml")),
                           ("ZENOH_SESSION_CONFIG_URI", "session.json5"),                 # issue #38
                           ("ZENOH_SESSION_CONFIG_URI", str(device.home.parent / "missing.json5"))):
            with self.subTest(key=key, value=value):
                result = self.refused(device, NX_ROS_SETUP=setup, **{key: value})

                self.assertIn(key, result.stderr)

    def test_only_built_in_bodies_can_be_installed(self):
        device = self.device()
        before = snapshot(device.home, device.package)

        result = device.run("install", NX_ADAPTER="vendor.module:Body")

        self.assertEqual(result.returncode, 2)
        self.assertIn("sim_quadruped, sim_arm or ros2", result.stderr)
        self.assertEqual(snapshot(device.home, device.package), before)

    def test_rclpy_that_does_not_import_fails_the_install_and_cleans_up(self):
        device = self.device()
        setup = device.fake_ros(rclpy=False)
        before = snapshot(device.home, device.package)

        result = device.run("install", NX_ADAPTER="ros2", NX_ROS_SETUP=str(setup))

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rclpy", result.stderr)
        self.assertFalse(device.root.exists())
        self.assertEqual(snapshot(device.home, device.package), before)

    @unittest.skipIf(DEFAULT_SETUP.exists(), "这台机器装着 ROS 2 Humble：默认路径会被找到")
    def test_without_a_setup_file_it_looks_for_humble_under_opt_ros(self):
        device = self.device()

        result = self.refused(device)

        self.assertIn(str(DEFAULT_SETUP), result.stderr)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class Ros2InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        device = RosDevice()
        cls.profiles = device.profiles_file()
        cls.device = ros_install(device=device, ROS_DOMAIN_ID="42", ROS_LOCALHOST_ONLY="1",
                                 FASTRTPS_DEFAULT_PROFILES_FILE=str(cls.profiles),
                                 ZENOH_SESSION_CONFIG_URI=str(cls.profiles))      # issue #38 (any readable file)

    @classmethod
    def tearDownClass(cls):
        cls.device.close()

    def tearDown(self):
        self.device.run("stop")
        self.device.probe_file.unlink(missing_ok=True)

    def test_install_records_the_body_and_the_ros_settings(self):
        root = self.device.root

        self.assertEqual((root / "adapter").read_text(), "ros2\n")
        recorded = (root / "ros2.env").read_text().splitlines()
        self.assertEqual(recorded, [f"ROS_SETUP={self.device.setup}", "ROS_DOMAIN_ID=42", "ROS_LOCALHOST_ONLY=1",
                                    f"FASTRTPS_DEFAULT_PROFILES_FILE={self.profiles}",
                                    f"ZENOH_SESSION_CONFIG_URI={self.profiles}"])
        self.assertEqual((root / "ros2.env").stat().st_mode & 0o077, 0)

    def test_install_says_what_it_found(self):
        out = self.device.installed.stdout

        self.assertIn(f"ROS 2 humble ({self.device.setup})", out)
        self.assertIn("ROS_DOMAIN_ID=42", out)
        self.assertIn("navigation: nav2_msgs found", out)

    def test_the_venv_sees_the_system_packages(self):
        cfg = (self.device.root / "venv" / "pyvenv.cfg").read_text()

        self.assertIn("include-system-site-packages = true", cfg)

    def test_start_runs_the_ros2_body_in_the_recorded_environment_only(self):
        elsewhere = self.device.home.parent / "elsewhere.json"
        elsewhere.write_text(json.dumps({"instance_id": "not-this-one"}))

        self.device.start(ROS_DOMAIN_ID="5", RMW_IMPLEMENTATION="rmw_cyclonedds_cpp",
                          NEUXMIND_ROS2_CONFIG=str(elsewhere), PYTHONPATH="/somewhere/else")

        env = self.device.runtime_env()
        status, body = http(self.device.url, "/v1/status", token=self.device.token)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["mode"], body["instance_id"]), ("ros2", "ros2-robot"))
        self.assertEqual((env["ROS_DISTRO"], env["ROS_DOMAIN_ID"], env["ROS_LOCALHOST_ONLY"]), ("humble", "42", "1"))
        self.assertEqual(env["FASTRTPS_DEFAULT_PROFILES_FILE"], str(self.profiles))
        self.assertEqual(env["ZENOH_SESSION_CONFIG_URI"], str(self.profiles))
        self.assertNotIn("RMW_IMPLEMENTATION", env)
        self.assertNotIn("NEUXMIND_ROS2_CONFIG", env)
        self.assertNotIn("/somewhere/else", env["PYTHONPATH"])
        self.assertIn("base.navigate_to", manifest_ids(self.device))

    def test_the_install_directory_holds_the_device_configuration(self):
        config = self.device.root / "ros2-config.json"
        config.write_text(json.dumps({"instance_id": "ros2-test-01", "nav2": False}))
        self.addCleanup(config.unlink)

        started = self.device.start()

        status, body = http(self.device.url, "/v1/status", token=self.device.token)
        self.assertEqual(body["instance_id"], "ros2-test-01")
        self.assertNotIn("base.navigate_to", manifest_ids(self.device))
        self.assertEqual(self.device.runtime_env()["NEUXMIND_ROS2_CONFIG"], str(config))
        self.assertIn(str(config), started.stdout)

    def test_a_linked_device_configuration_is_refused(self):
        target = self.device.home.parent / "linked.json"
        target.write_text("{}")
        link = self.device.root / "ros2-config.json"
        link.symlink_to(target)
        self.addCleanup(link.unlink)

        result = self.device.run("start")

        self.assertEqual(result.returncode, 1)
        self.assertIn("ros2-config.json", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])

    def test_start_checks_the_distribution_again(self):
        original = self.device.setup.read_text()
        self.addCleanup(self.device.setup.write_text, original)
        self.device.setup.write_text(original.replace("ROS_DISTRO=humble", "ROS_DISTRO=jazzy"))

        result = self.device.run("start")

        self.assertEqual(result.returncode, 1)
        self.assertIn("jazzy", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])
        self.assertFalse((self.device.root / "runtime.pid").exists())

    def test_a_device_configuration_that_says_simulated_is_refused(self):
        # A simulated body skips the safety-limit requirement (issue #22) and is never device evidence;
        # an install on a device is for its real ROS 2.
        config = self.device.root / "ros2-config.json"
        self.addCleanup(config.unlink, missing_ok=True)
        for key in ("simulated", "use_sim_time"):
            with self.subTest(key=key):
                config.write_text(json.dumps({key: True}))

                result = self.device.run("start")

                self.assertEqual(result.returncode, 1)
                self.assertIn(key, result.stderr)
                self.assertEqual(self.device.runtime_pids(), [])

    def test_a_line_without_a_value_in_the_recorded_settings_is_refused(self):
        recorded = self.device.root / "ros2.env"
        kept = recorded.read_text()
        self.addCleanup(recorded.write_text, kept)
        recorded.write_text(kept + "CYCLONEDDS_URI\n")

        result = self.device.run("start")

        self.assertEqual(result.returncode, 1)
        self.assertIn("ros2.env", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])

    def test_a_setup_file_others_can_write_is_not_sourced_at_start(self):
        self.device.setup.chmod(0o666)
        self.addCleanup(self.device.setup.chmod, 0o644)

        result = self.device.run("start")

        self.assertEqual(result.returncode, 1)
        self.assertIn("writable", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])

    def test_status_says_motion_is_blocked_until_the_limits_are_filled(self):
        self.device.start()

        result = self.device.run("status")

        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["mode"], "ros2")
        self.assertIs(report["motion_blocked"], True)      # the double declares no limits (issue #22)

    def test_stop_and_a_second_stop(self):
        self.device.start()
        pid = self.device.pid()

        stopped, again = self.device.run("stop"), self.device.run("stop")

        self.assertEqual((stopped.returncode, again.returncode), (0, 0))
        self.assertFalse(alive(pid))
        self.assertIn("not running", again.stdout)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class SlowRosSetupTests(unittest.TestCase):
    """Found in review: a setup file that takes longer than start waits must not leave a runtime behind."""

    START_WAIT_S = 60                      # device_install.sh: START_POLLS x 0.5 s

    def test_a_start_that_gives_up_leaves_no_runtime_behind(self):
        device = ros_install()
        self.addCleanup(device.close)
        flag = device.home.parent / "slow-setup"
        stall = self.START_WAIT_S + 15
        helper = 307                     # a background process the setup file starts (second review round)
        device.setup.write_text(f'if [ -e "{flag}" ]; then sleep {helper} & sleep {stall}; fi\n'
                                + device.setup.read_text())
        flag.touch()

        result = device.run("start")
        survivors = []
        watch_until = time.monotonic() + stall - self.START_WAIT_S + 10      # past the moment the setup returns
        while time.monotonic() < watch_until and not survivors:
            survivors = device.runtime_pids()
            time.sleep(0.5)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(survivors, [])
        self.assertFalse((device.root / "runtime.pid").exists())
        left = [p.name for p in Path("/proc").iterdir() if p.name.isdigit()
                and arguments(int(p.name))[:2] == ["sleep", str(helper)] and alive(int(p.name))]
        self.assertEqual(left, [])       # what the setup file started went with it
        flag.unlink()
        device.start()                   # and the next start works
        self.assertEqual(device.runtime_pids(), [device.pid()])
        device.run("stop")


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class Ros2WithoutNav2Tests(unittest.TestCase):
    def test_a_device_without_nav2_installs_and_starts_without_navigation(self):
        device = ros_install(nav2=False)
        self.addCleanup(device.close)

        device.start()
        self.addCleanup(device.run, "stop")

        self.assertIn("navigation: nav2_msgs not installed", device.installed.stdout)
        ids = manifest_ids(device)
        self.assertNotIn("base.navigate_to", ids)
        self.assertIn("base.odometry", ids)
        self.assertTrue(wait_until(lambda: http(device.url, "/v2/state?ids=base.odometry",
                                                token=device.token)[0] == 200, 10.0))


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class SimulatedInstallTests(unittest.TestCase):
    """The simulated body stays the default and keeps ROS 2 out."""

    @classmethod
    def setUpClass(cls):
        cls.device = installed_device()

    @classmethod
    def tearDownClass(cls):
        cls.device.close()

    def tearDown(self):
        self.device.run("stop")

    def test_the_default_install_records_the_simulated_body(self):
        self.assertEqual((self.device.root / "adapter").read_text(), "sim_quadruped\n")
        self.assertFalse((self.device.root / "ros2.env").exists())
        self.assertIn("include-system-site-packages = false",
                      (self.device.root / "venv" / "pyvenv.cfg").read_text())

    def test_a_ros2_start_on_a_simulated_install_is_refused(self):
        result = self.device.run("start", NX_ADAPTER="ros2")

        self.assertEqual(result.returncode, 2)
        self.assertIn("NX_ADAPTER=ros2", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])

    def test_an_install_from_before_the_choice_starts_the_simulated_body(self):
        recorded = self.device.root / "adapter"
        kept = recorded.read_text()
        recorded.unlink()
        self.addCleanup(recorded.write_text, kept)

        self.device.start()

        status, body = http(self.device.url, "/v1/status", token=self.device.token)
        self.assertIs(body["simulated"], True)

    def test_a_recorded_body_that_is_not_built_in_is_refused(self):
        recorded = self.device.root / "adapter"
        kept = recorded.read_text()
        recorded.write_text("vendor.module:Body\n")
        self.addCleanup(recorded.write_text, kept)

        result = self.device.run("start")

        self.assertEqual(result.returncode, 2)
        self.assertIn("adapter", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])


if __name__ == "__main__":
    unittest.main()
