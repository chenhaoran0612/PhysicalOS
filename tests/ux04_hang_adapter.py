"""Test-only simulated quadruped whose ``reset()`` can be made to hang (ux04 browser check, Q5-03).

While the file named by ``NX_FIXTURE_HANG_FLAG`` exists, ``reset()`` does not return -- like a vendor
driver whose stand-up never finishes. The runtime then answers a second reset with 409
``reset_in_progress`` and keeps the e-stop latched. Removing the file lets ``reset()`` return.
Simulated body only; never used outside tests.
"""
import os
import time

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped


class HangResetQuadruped(SimQuadruped):
    def reset(self):
        flag = os.environ.get('NX_FIXTURE_HANG_FLAG', '')
        deadline = time.monotonic() + 300          # never outlive a test run
        while flag and os.path.exists(flag) and time.monotonic() < deadline:
            time.sleep(0.05)
        return super().reset()
