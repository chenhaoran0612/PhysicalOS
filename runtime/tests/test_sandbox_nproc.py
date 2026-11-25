"""RLIMIT_NPROC calibration in the sandbox child (``sandbox_child._kernel_task_count`` /
``_calibrated``) against a fake kernel, so every search path runs on any OS.

The kernel refuses fork/clone with EAGAIN when the uid's task count is >= the soft
RLIMIT_NPROC. The real-kernel behaviour is covered by ``test_sandbox.HiddenTasksTests``.
"""
import math
import unittest
from unittest import mock

from neuxmind_runtime import sandbox_child as child

HEADROOM = 32


class FakeKernel:
    """Stands in for the ``resource`` module and ``os.fork``/``os.waitpid``.

    ``tasks``: the uid's tasks as the kernel counts them, the probing process included.
    ``spurious``: indices of fork attempts refused with EAGAIN whatever the limit
    (a pids cgroup, pid_max, ...). ``refuse_all``: every fork is refused that way.
    """

    RLIMIT_NPROC = 6
    RLIM_INFINITY = -1

    def __init__(self, tasks, hard=RLIM_INFINITY, spurious=(), refuse_all=False,
                 fail_setrlimit=False, fail_restore=False, fork_error=None):
        self.tasks = tasks
        self.limit = (self.RLIM_INFINITY, hard)
        self.original = self.limit
        self.attempts = []                   # soft limit at each fork attempt
        self.spurious = set(spurious)
        self.refuse_all = refuse_all
        self.fail_setrlimit = fail_setrlimit
        self.fail_restore = fail_restore
        self.fork_error = fork_error

    def getrlimit(self, which):
        assert which == self.RLIMIT_NPROC
        return self.limit

    def setrlimit(self, which, value):
        assert which == self.RLIMIT_NPROC
        soft, hard = value
        assert hard == self.original[1], "the probes must never change the hard limit"
        if self.fail_setrlimit and value != self.original:       # probe values only
            raise OSError(1, "Operation not permitted")
        if self.fail_restore and value == self.original:
            raise OSError(1, "Operation not permitted")
        self.limit = value

    def fork(self):
        if self.fork_error is not None:
            raise self.fork_error
        soft = self.limit[0]
        index = len(self.attempts)
        self.attempts.append(soft)
        if self.refuse_all or index in self.spurious or (soft != self.RLIM_INFINITY and self.tasks >= soft):
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return 4242                          # the parent side; the child side is never simulated

    def waitpid(self, pid, options):
        assert pid == 4242
        return pid, 0

    def count(self, hint):
        with mock.patch.object(child.os, "fork", self.fork), mock.patch.object(child.os, "waitpid", self.waitpid):
            return child._kernel_task_count(self, hint)

    def calibrate(self, limits):
        with mock.patch.object(child.os, "fork", self.fork), mock.patch.object(child.os, "waitpid", self.waitpid):
            return child._calibrated(self, limits)


def search_bound(hidden: int) -> int:
    """Fork attempts allowed for ``hidden`` tasks outside the parent's view: two per probe
    (a refusal is retried once) for the first probe, the ceiling check and both searches."""
    return 2 * (2 + 2 * (math.ceil(math.log2(hidden + 1)) + 1))


class KernelTaskCountTests(unittest.TestCase):
    def test_parent_count_confirmed_with_one_fork(self):
        kernel = FakeKernel(tasks=11)        # the parent saw 10 tasks; the probing child is the 11th
        self.assertEqual(kernel.count(10), 11)
        self.assertEqual(kernel.attempts, [12])
        self.assertEqual(kernel.limit, kernel.original)

    def test_parent_overcount_returns_the_upper_bound(self):
        kernel = FakeKernel(tasks=4)         # tasks exited between the parent's count and the probe
        self.assertEqual(kernel.count(10), 11)
        self.assertEqual(len(kernel.attempts), 1)

    def test_hidden_tasks_are_counted_exactly(self):
        for hidden in (1, 2, 3, 7, 75, 128, 1000, 70000):
            with self.subTest(hidden=hidden):
                kernel = FakeKernel(tasks=11 + hidden)
                self.assertEqual(kernel.count(10), 11 + hidden)
                self.assertLessEqual(len(kernel.attempts), search_bound(hidden))
                self.assertEqual(kernel.limit, kernel.original)

    def test_finite_hard_limit_caps_the_search(self):
        kernel = FakeKernel(tasks=40, hard=50)
        self.assertEqual(kernel.count(10), 40)
        self.assertLessEqual(max(kernel.attempts), 50)

    def test_count_at_the_hard_limit_is_undetermined(self):
        kernel = FakeKernel(tasks=60, hard=50)
        self.assertIsNone(kernel.count(10))
        self.assertLessEqual(max(kernel.attempts), 50)
        self.assertEqual(kernel.limit, kernel.original)

    def test_hint_beyond_the_ceiling_is_undetermined_when_refused(self):
        kernel = FakeKernel(tasks=60, hard=50)
        self.assertIsNone(kernel.count(70))
        self.assertEqual(set(kernel.attempts), {50})

    def test_eagain_that_no_limit_explains_is_undetermined_quickly(self):
        # A pids cgroup or --pids-limit refuses every fork: not RLIMIT_NPROC.
        kernel = FakeKernel(tasks=11, refuse_all=True)
        self.assertIsNone(kernel.count(10))
        self.assertLessEqual(len(kernel.attempts), 4)
        self.assertEqual(kernel.attempts[-1], child.NPROC_PROBE_CEILING)
        self.assertEqual(kernel.limit, kernel.original)

    def test_one_spurious_refusal_does_not_change_the_count(self):
        for hidden in (0, 75):
            clean = FakeKernel(tasks=11 + hidden)
            expected = clean.count(10)
            for index in range(len(clean.attempts)):
                with self.subTest(hidden=hidden, refused_attempt=index):
                    kernel = FakeKernel(tasks=11 + hidden, spurious={index})
                    self.assertEqual(kernel.count(10), expected)

    def test_probe_errors_are_undetermined(self):
        cases = {"setrlimit refused": FakeKernel(tasks=11, fail_setrlimit=True),
                 "fork out of memory": FakeKernel(tasks=11, fork_error=OSError(12, "Cannot allocate memory"))}
        for name, kernel in cases.items():
            with self.subTest(name):
                self.assertIsNone(kernel.count(10))

    def test_a_limit_that_cannot_be_restored_is_not_ignored(self):
        kernel = FakeKernel(tasks=11 + 75, fail_restore=True)
        with self.assertRaises(OSError):
            kernel.count(10)


class CalibratedLimitsTests(unittest.TestCase):
    def limits(self, nproc=10 + HEADROOM):
        return {"RLIMIT_NOFILE": 64, "RLIMIT_NPROC": nproc, child.NPROC_HEADROOM: HEADROOM}

    def test_hidden_tasks_raise_the_limit_by_what_the_parent_missed(self):
        limits = self.limits()
        out = FakeKernel(tasks=11 + 75).calibrate(limits)
        self.assertEqual(out, {"RLIMIT_NOFILE": 64, "RLIMIT_NPROC": 10 + 75 + HEADROOM})
        self.assertEqual(limits, self.limits(), "the input must not be modified")

    def test_parent_value_is_kept_when_the_kernel_agrees(self):
        out = FakeKernel(tasks=11).calibrate(self.limits())
        self.assertEqual(out["RLIMIT_NPROC"], 10 + HEADROOM)

    def test_never_lowered_when_the_parent_overcounted(self):
        out = FakeKernel(tasks=3).calibrate(self.limits())
        self.assertEqual(out["RLIMIT_NPROC"], 10 + HEADROOM)

    def test_undetermined_count_keeps_the_parent_value(self):
        out = FakeKernel(tasks=11, refuse_all=True).calibrate(self.limits())
        self.assertEqual(out["RLIMIT_NPROC"], 10 + HEADROOM)

    def test_nothing_is_probed_without_both_entries(self):
        cases = {"root skill user (no NPROC)": {"RLIMIT_NOFILE": 64},
                 "parent without headroom": {"RLIMIT_NPROC": 42},
                 "headroom without NPROC": {child.NPROC_HEADROOM: HEADROOM},
                 "bool headroom": {"RLIMIT_NPROC": 42, child.NPROC_HEADROOM: True},
                 "negative headroom": {"RLIMIT_NPROC": 42, child.NPROC_HEADROOM: -1}}
        for name, limits in cases.items():
            with self.subTest(name):
                kernel = FakeKernel(tasks=11 + 75)
                out = kernel.calibrate(limits)
                self.assertEqual(kernel.attempts, [])
                self.assertNotIn(child.NPROC_HEADROOM, out)
                self.assertEqual(out.get("RLIMIT_NPROC"), limits.get("RLIMIT_NPROC"))


if __name__ == "__main__":
    unittest.main()
