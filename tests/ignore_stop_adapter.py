"""Test-only simulated quadrupeds for the "stop not obeyed" browser checks (ux05-fix, Q7-03).

Based on the round-07 reviewer's stop-ignoring adapter (``r8_ignore_adapter``): while the flag file
named by ``NX_FIXTURE_IGNORE_FLAG`` exists, 0.8 s after every ``stop()`` the body resumes walking at
0.05 m/s -- a driver that "forgets" the stop. The real runtime's latch guard then re-stops it and,
after >= 10 re-stops over >= 30 s, reports ``safety.stop_not_obeyed`` (INTERFACES §9).

* ``IgnoreStopQuadruped`` -- the desktop's simulated robot (``simulated: true``, no hardware e-stop).
* ``IgnoreStopRealQuadruped`` -- the same simulated physics, but its manifest *claims* to be a real body
  with a hardware e-stop, so the desktop treats it as a real robot connected by address and token.
  Test fixture only: no hardware is involved.
* ``CreepRealQuadruped`` (ux05-fix3) -- obeys stops but slides 9 cm after each latch while
  ``NX_FIXTURE_CREEP_FLAG`` exists (the desktop's simulated robot does the same): the drift notice.
* ``RelaxedRealQuadruped`` -- obeys stops; its manifest loosens the latch-guard calibration to the upper
  bounds (``safety.latch_guard``), for the "calibration loosened" warning.

Started by ``tests/ux01_browser_fixture.py`` with ``NX_FIXTURE_IGNORE_STOP=1``.
"""
import os

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped


def _on():
    flag = os.environ.get('NX_FIXTURE_IGNORE_FLAG', '')
    return bool(flag) and os.path.exists(flag)


class _Creep:
    """ux05-fix3: while the file named by ``NX_FIXTURE_CREEP_FLAG`` exists, every latch (the first ``stop()``
    after a reset) is followed -- after ``CREEP_DELAY_S`` -- by the body sliding ``CREEP_M`` along x at ``CREEP_V``
    m/s whatever it is told (pushed / on a slope): real creep while latched, which the runtime's latch guard
    reports once per latch as ``safety.latched_drift_notice`` (>5 cm). Then it rests. ``reset()`` arms the next
    latch."""

    CREEP_DELAY_S = 2.0
    CREEP_V = 0.03
    CREEP_M = 0.09
    _creep_at = None
    _creep_left = 0.0
    _armed = True

    def stop(self):
        super().stop()
        flag = os.environ.get('NX_FIXTURE_CREEP_FLAG', '')
        if self._armed and flag and os.path.exists(flag):
            self._armed = False
            self._creep_at = self._t + self.CREEP_DELAY_S
            self._creep_left = self.CREEP_M

    def reset(self):
        self._creep_at, self._creep_left, self._armed = None, 0.0, True
        return super().reset()

    def _step(self, dt):
        super()._step(dt)
        if self._creep_at is not None and self._t >= self._creep_at and self._creep_left > 0:
            d = min(self._creep_left, self.CREEP_V * dt)
            self.x += d
            self._creep_left -= d


class IgnoreStopQuadruped(_Creep, SimQuadruped):
    _resume_at = None

    def stop(self):
        super().stop()
        if _on():
            self._resume_at = self._t + 0.8

    def reset(self):
        self._resume_at = None
        return super().reset()

    def _step(self, dt):
        if self._resume_at is not None and self._t >= self._resume_at:
            self._resume_at = None
            if _on():
                self.cmd = (0.05, 0.0, 0.0)
        super()._step(dt)


def _real(manifest, name, latch_guard=None, reset_expected_s=None):
    manifest = dict(manifest)
    manifest.update(simulated=False, name=name, vendor='fixture', model='fixture-quadruped')
    caps = []
    for cap in manifest['capabilities']:
        if cap.get('id') == 'safety.stop':
            cap = {**cap, 'params': {**cap.get('params', {}), 'hardware_estop': True}}
        caps.append(cap)
    manifest['capabilities'] = caps
    safety = {**manifest.get('safety', {}), 'hardware_estop': True}
    if latch_guard:
        safety['latch_guard'] = latch_guard
    if reset_expected_s is not None:
        safety['reset_expected_s'] = reset_expected_s
    manifest['safety'] = safety
    return manifest


class IgnoreStopRealQuadruped(IgnoreStopQuadruped):
    def manifest(self):
        return _real(super().manifest(), '测试夹具四足（按真机处理）')


class RelaxedRealQuadruped(SimQuadruped):
    def manifest(self):
        return _real(super().manifest(), '测试夹具四足（守护已放宽）',
                     latch_guard={'pos_m': 0.2, 'yaw_rad': 0.2, 'joint_rad': 0.2})


class MixedRealQuadruped(SimQuadruped):
    """ux05-fix2 (Q7b-07): obeys stops; mixed calibration -- ``pos_m`` *stricter* than the default, ``yaw_drift_rad_s``
    and ``escalate_after`` looser. Only the looser two may be called "放宽"."""

    def manifest(self):
        return _real(super().manifest(), '测试夹具四足（混合标定）',
                     latch_guard={'pos_m': 0.03, 'yaw_drift_rad_s': 0.002, 'escalate_after': 30})


RESET_EXPECTED_S = 7


class CreepRealQuadruped(_Creep, SimQuadruped):
    """ux05-fix3: obeys stops; slides while latched when ``/fixture/creep?on=1`` (see ``_Creep``)."""

    def manifest(self):
        return _real(super().manifest(), '测试夹具四足（急停后滑移）')


def _hang_reset_class():
    from ux04_hang_adapter import HangResetQuadruped

    class HangResetRealQuadruped(HangResetQuadruped):
        """ux05-fix2 (Q7b-05/08): a "real" robot whose reset hangs while ``NX_FIXTURE_HANG_FLAG`` exists.
        ux05-fix3: its manifest declares ``safety.reset_expected_s = 7``, so the desktop sends
        ``expire_s = max(8, 7 + 5) = 12`` and waits 16 s."""

        def manifest(self):
            return _real(super().manifest(), '测试夹具四足（复位会卡住）', reset_expected_s=RESET_EXPECTED_S)
    return HangResetRealQuadruped


try:
    HangResetRealQuadruped = _hang_reset_class()
except ImportError:          # ux04_hang_adapter is importable only with tests/ on the path (the fixture does that)
    HangResetRealQuadruped = None
