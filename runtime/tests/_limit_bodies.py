"""Stand-in bodies for the device-limit tests (issue #22). All of them are the simulated quadruped
underneath: nothing here drives hardware. What differs is what the manifest says about the body."""
from __future__ import annotations

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped


class RealQuadruped(SimQuadruped):
    """A body that does not call itself simulated: a person has to fill in its limits."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_calls = 0

    def manifest(self) -> dict:
        return {**super().manifest(), "simulated": False}

    def stop(self) -> None:
        self.stop_calls += 1
        super().stop()


class UndeclaredQuadruped(SimQuadruped):
    """A simulated body whose base.velocity declares no max_vx / max_wz, like the ROS 2 bridge
    started without limits in its configuration."""

    def manifest(self) -> dict:
        manifest = super().manifest()
        for cap in manifest["capabilities"]:
            if cap["id"] == "base.velocity":
                cap["params"] = {"max_vy": 0.0, "holonomic": False}
        manifest["safety"].pop("max_linear_speed")
        return manifest


class ChunkQuadruped(RealQuadruped):
    """Also declares policy.action_chunk in the base_velocity action space."""

    def manifest(self) -> dict:
        manifest = super().manifest()
        manifest["capabilities"] = manifest["capabilities"] + [
            {"id": "policy.action_chunk", "params": {"action_space": "base_velocity", "dim": 3, "rate_hz": 10.0}}]
        return manifest
