"""The runtime's own log rotates by size (issue #25): it used to be appended to forever.

``LogFile`` points the given descriptors (1 and 2 in ``serve``) at the log file and, when the file
has grown past the limit, moves it aside and opens a fresh one. The tests use descriptors of their
own, never the test runner's stdout.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401  (path bootstrap)
from _support import RUNTIME_DIR

from neuxmind_runtime.logrotate import LogFile


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "runtime.log"
        self.fds = []
        for name in ("out", "err"):                               # stand-ins for stdout and stderr
            self.fds.append(os.open(self.dir / f"original-{name}", os.O_WRONLY | os.O_CREAT, 0o600))

    def tearDown(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._tmp.cleanup()

    def log(self, max_bytes: int = 1000, keep: int = 2) -> LogFile:
        log = LogFile(self.path, max_bytes=max_bytes, keep=keep, fds=tuple(self.fds))
        log.attach()
        return log

    def write(self, text: str, which: int = 0) -> None:
        os.write(self.fds[which], text.encode())

    def test_output_goes_to_the_log_file(self):
        self.log()
        self.write("started\n")
        self.write("warning\n", which=1)
        self.assertEqual(self.path.read_text(), "started\nwarning\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_it_appends_to_an_existing_log(self):
        self.path.write_text("from the last start\n")
        os.chmod(self.path, 0o644)                                # as a shell redirect creates it
        self.log()
        self.write("again\n")
        self.assertEqual(self.path.read_text(), "from the last start\nagain\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)  # ... and it is made private

    def test_nothing_happens_below_the_limit(self):
        log = self.log(max_bytes=1000)
        self.write("x" * 999)
        self.assertFalse(log.check())
        self.assertEqual(sorted(p.name for p in self.dir.glob("runtime.log*")), ["runtime.log"])

    def test_it_rotates_at_the_limit_and_output_continues_in_the_new_file(self):
        log = self.log(max_bytes=1000)
        self.write("a" * 1000)
        self.assertTrue(log.check())
        self.write("after\n")
        self.write("after-err\n", which=1)
        self.assertEqual(self.path.read_text(), "after\nafter-err\n")
        self.assertEqual((self.dir / "runtime.log.1").read_text(), "a" * 1000)
        self.assertEqual((self.dir / "runtime.log.1").stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.describe()["rotations"], 1)

    def test_only_the_newest_files_are_kept(self):
        log = self.log(max_bytes=100, keep=2)
        for generation in "abcd":
            self.write(generation * 100)
            self.assertTrue(log.check())
        names = sorted(p.name for p in self.dir.glob("runtime.log*"))
        self.assertEqual(names, ["runtime.log", "runtime.log.1", "runtime.log.2"])
        self.assertEqual((self.dir / "runtime.log.1").read_text(), "d" * 100)
        self.assertEqual((self.dir / "runtime.log.2").read_text(), "c" * 100)
        total = sum(p.stat().st_size for p in self.dir.glob("runtime.log*"))
        self.assertLessEqual(total, 3 * 100)                      # bounded: (keep + 1) x max_bytes

    def test_other_files_are_not_touched(self):
        for name in ("runtime.log.old", "runtime.log.10", "runtime.pid", "runtime.log.1.bak"):
            (self.dir / name).write_text("keep me")
        log = self.log(max_bytes=10, keep=1)
        for _ in range(3):
            self.write("x" * 10)
            log.check()
        for name in ("runtime.log.old", "runtime.log.10", "runtime.pid", "runtime.log.1.bak"):
            self.assertEqual((self.dir / name).read_text(), "keep me", name)

    def test_a_failure_while_rotating_keeps_logging(self):
        log = self.log(max_bytes=10)
        self.write("x" * 10)
        (self.dir / "runtime.log.1").mkdir()                      # the old name is in the way and cannot be replaced
        (self.dir / "runtime.log.1" / "inside").write_text("keep me")
        self.assertFalse(log.check())                             # never raises
        self.write("still here\n")
        self.assertIn("still here", self.path.read_text())
        self.assertIsNotNone(log.describe()["error"])
        self.assertEqual((self.dir / "runtime.log.1" / "inside").read_text(), "keep me")

    def test_a_rotation_that_cannot_open_the_new_file_recovers(self):
        log = self.log(max_bytes=10)
        self.write("x" * 10)
        real_attach, failures = log.attach, {"left": 1}

        def attach_fails_once():
            if failures["left"]:
                failures["left"] -= 1
                raise OSError(24, "Too many open files")
            return real_attach()

        log.attach = attach_fails_once
        self.assertFalse(log.check())                             # renamed, could not reopen: name given back
        self.assertEqual(sorted(p.name for p in self.dir.glob("runtime.log*")), ["runtime.log"])
        self.write("y")
        self.assertEqual(self.path.read_text(), "x" * 10 + "y")   # still the live file, under its own name
        self.assertTrue(log.check())                              # the next period rotates normally
        self.write("fresh\n")
        self.assertEqual(self.path.read_text(), "fresh\n")
        self.assertEqual((self.dir / "runtime.log.1").read_text(), "x" * 10 + "y")

    def test_a_log_that_lost_its_name_is_reopened_not_rotated_away(self):
        log = self.log(max_bytes=10, keep=1)
        self.write("x" * 10)
        os.rename(self.path, self.dir / "runtime.log.1")          # as after a rotation that died half way
        log.check()
        self.write("back\n")
        self.assertEqual(self.path.read_text(), "back\n")
        self.assertEqual((self.dir / "runtime.log.1").read_text(), "x" * 10)

    def test_a_symlink_in_place_of_the_log_is_refused(self):
        target = self.dir / "elsewhere"
        target.write_text("not a log\n")
        os.symlink(target, self.path)
        log = LogFile(self.path, max_bytes=1000, keep=2, fds=tuple(self.fds))
        with self.assertRaises(OSError):
            log.attach()
        self.assertEqual(target.read_text(), "not a log\n")

    def test_limits_must_be_sane(self):
        for kwargs in ({"max_bytes": 0}, {"max_bytes": -1}, {"keep": 0}, {"keep": 100}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                LogFile(self.path, **{"max_bytes": 1000, "keep": 2, **kwargs})


class ServeLogFileTests(unittest.TestCase):
    def test_serve_refuses_bad_log_and_disk_settings_before_starting(self):
        env = {**os.environ, "BRAIN_EDGE_TOKEN": "t" * 32, "PYTHONPATH": str(RUNTIME_DIR)}
        with tempfile.TemporaryDirectory() as tmp:
            base = [sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_arm", "--port", "0",
                    "--state-dir", str(Path(tmp) / "state")]
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.write_text("not a log\n")
            os.symlink(elsewhere, Path(tmp) / "link.log")
            for extra in (["--log-max-mb", "0"], ["--log-keep", "0"], ["--disk-full-percent", "30"],
                          ["--disk-full-percent", "100"], ["--telemetry-retention-s", "-5"],
                          ["--telemetry-retention-s", "inf"], ["--telemetry-retention-s", "nan"],
                          ["--log-file", str(Path(tmp) / "link.log")],
                          ["--log-file", str(Path(tmp) / "no-such-dir" / "runtime.log")]):
                result = subprocess.run(base + extra, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 2, extra)
                self.assertIn("错误", result.stderr, extra)
                self.assertFalse((Path(tmp) / "state").exists(), extra)   # nothing was created
                self.assertNotIn("Traceback", result.stderr, extra)
            self.assertEqual(elsewhere.read_text(), "not a log\n")


if __name__ == "__main__":
    unittest.main()
