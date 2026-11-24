"""Helpers for the cloud-skills tests (issue #43): made-up skill packages, a clock the tests move,
and a pretend robot that talks to ``POST /device-link/sync``."""
from __future__ import annotations

import json

import support  # noqa: F401
from support import CloudTestCase, REPO

from neuxmind_cloud import skill_calls

EXAMPLES = REPO / "runtime" / "examples" / "skills"
LINK_SCOPE, INVOKE_SCOPE = "device:link", "skills:invoke"
SKILLS_READ, SKILLS_WRITE = "skills:read", "skills:write"
START = 1_800_000_000.0
DIGEST = "ab" * 32


def example_package(name: str = "goto_place") -> dict:
    """An example skill of the repository as an installable package (``skill.json`` + its text files)."""
    root = EXAMPLES / name
    package = json.loads((root / "skill.json").read_text(encoding="utf-8"))
    package["files"] = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                        for path in sorted(root.rglob("*"))
                        if path.is_file() and path.name != "skill.json" and "__pycache__" not in path.parts}
    return package


def package(skill_id: str = "demo.wave", version: str = "0.1.0", **over) -> dict:
    """A small made-up package that needs no motion."""
    base = {"schema": "skill.package.v1", "id": skill_id, "version": version, "title": "演示",
            "description": "测试用的技能", "entry": "skill.py:run", "requires": [{"id": "system.health"}],
            "optional": [], "parameters": [{"name": "times", "type": "int", "required": False, "min": 1, "max": 5}],
            "timeout_s": 60, "files": {"skill.py": "def run(ctx, times=1):\n    return {'times': times}\n"}}
    base.update(over)
    return base


def device_skill(skill_id: str = "goto_place", allowed: bool = True, **over) -> dict:
    """One entry of the ``skills`` list a robot sends: an installed skill and its remote switch."""
    entry = {"id": skill_id, "version": "0.1.0", "digest": DIGEST, "title": "去指定地点", "description": "走到一个地点",
             "parameters": [{"name": "place", "type": "str", "required": True,
                             "options": [{"value": "会议室", "label": "会议室"}, {"value": "二楼", "label": "二楼"}]},
                            {"name": "max_speed", "type": "float", "required": False, "min": 0.1, "max": 1.5}],
             "requires": ["base.navigate_to_place", "safety.stop"], "motion": True, "timeout_s": 1800,
             "remote": {"allowed": allowed, "set_by": "owner@example.com", "set_at": START - 100}}
    entry.update(over)
    return entry


class Clock:
    def __init__(self, at: float = START):
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> float:
        self.at += seconds
        return self.at


class SkillCase(CloudTestCase):
    """An organisation with a robot key (``device:link``) and a third-party key (``skills:invoke``),
    on a clock the test moves."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self._real_clock = skill_calls.clock
        skill_calls.clock = self.clock
        self.addCleanup(self._restore_clock)
        self.owner = self.register("owner@example.com")
        self.robot_key = self.api_key(self.owner, [LINK_SCOPE])
        self.caller = self.api_key(self.owner, [INVOKE_SCOPE])

    def _restore_clock(self):
        skill_calls.clock = self._real_clock

    def delete(self, path: str, user=None, **kwargs):
        headers = kwargs.pop("headers", None) or (user["headers"] if user else {})
        return self.client.delete(support.API + path, headers=headers, **kwargs)

    def post_text(self, path: str, body, headers: dict):
        """POST ``body`` written out as ASCII JSON: half a surrogate pair or NaN travel as text, which
        is how a hostile or sloppy client would send them (the test client's own encoder refuses them)."""
        return self.client.post(support.API + path, content=json.dumps(body, allow_nan=True),
                                headers={**headers, "Content-Type": "application/json"})

    # ------------------------------------------------------------ the pretend robot
    def sync_raw(self, *, instance_id: str = "dog-1", skills=None, calls=None, stops=None, state=None, key=None,
                 **over):
        body = {"schema": "neuxmind.device_link.v1",
                "device": {"instance_id": instance_id, "name": "Dog", "family": "quadruped", "simulated": True,
                           "runtime_version": "0.5.0"},
                "state": {"estop_latched": False, "busy": False, "motion_blocked": False},
                "skills": [device_skill()] if skills is None else skills,
                "calls": [] if calls is None else calls, "stops": [] if stops is None else stops}
        if state is not None:
            body["state"] = {**body["state"], **state} if isinstance(state, dict) else state
        body.update(over)
        return self.post("/device-link/sync", body, headers=(key or self.robot_key)["headers"])

    def sync(self, **kwargs) -> dict:
        return self.ok(self.sync_raw(**kwargs))

    def online(self, **kwargs) -> str:
        """The robot's first sync; returns its ``device_id``."""
        return self.sync(**kwargs)["device_id"]

    # ------------------------------------------------------------ the third party
    def call_raw(self, device_id: str, skill_id: str = "goto_place", request_id: str = "req-1", parameters=None,
                 key=None, **extra):
        body = {"request_id": request_id, "parameters": {"place": "会议室"} if parameters is None else parameters,
                **extra}
        return self.post(f"/open/robots/{device_id}/skills/{skill_id}/calls", body,
                         headers=(key or self.caller)["headers"])

    def call(self, device_id: str, **kwargs) -> dict:
        return self.ok(self.call_raw(device_id, **kwargs), 202)["call"]

    def seen(self, call_id: str, key=None) -> dict:
        return self.ok(self.get(f"/open/calls/{call_id}", headers=(key or self.caller)["headers"]))["call"]

    def audits(self, action: str) -> list:
        from sqlalchemy import select
        from neuxmind_cloud.models import AuditLog
        with self.db.transaction() as s:
            return [(row.actor, row.target, row.detail) for row in
                    s.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).scalars()]

