"""Issue #43: the owner's remote switches over the gateway (``GET /v2/remote``,
``POST /v2/remote/skills``; ``remote_access.py``): who may turn one on, that turning it off needs
nothing, what it is bound to, what survives a restart, and that a stored switch nobody wrote this
way counts as off."""
from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401
from _support import RuntimeFixture, custom_package, example, http

from neuxmind_runtime.alert_report import ConfigError, load_config
from neuxmind_runtime.remote_access import SCHEMA, STORE_KIND, SWITCHES_ID, checked_entry
from neuxmind_runtime.sandbox import INPROCESS

ON = {"skill_id": "goto_place", "allowed": True, "set_by": "王工", "hardware_authorized": True}


class OverTheGateway(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.boot()

    def boot(self):
        self.fx = RuntimeFixture("quadruped", time_scale=40.0, http=True, sandbox=INPROCESS, state_dir=self._tmp.name)

    def tearDown(self):
        self.fx.close()

    def on(self, skill_id: str = "goto_place") -> dict:
        """The request that turns ``skill_id`` on, with the digest a person would have been shown."""
        digest = {s["id"]: s["digest"] for s in self.fx.runtime.skills.list()}.get(skill_id)
        return {**ON, "skill_id": skill_id, "digest": digest}

    def install(self, *names):
        for name in names:
            self.fx.install(example(name))

    def test_the_view_lists_every_installed_skill_with_its_switch_off(self):
        self.install("goto_place", "status_report")
        status, body = http(self.fx.url, "/v2/remote")
        self.assertEqual((status, body["schema"]), (200, "neuxmind.remote_access.v1"))
        self.assertEqual(body["link"], {**body["link"], "configured": False, "enabled": False, "state": "off",
                                        "host": None, "last_sync_at": None, "last_error": None, "device_id": None})
        self.assertEqual(sorted(body["skills"]), ["goto_place", "status_report"])
        digest = {s["id"]: s["digest"] for s in self.fx.runtime.skills.list()}["goto_place"]
        self.assertEqual(body["skills"]["goto_place"],
                         {"allowed": False, "digest": digest, "set_by": None, "set_at": None, "invalidated": False,
                          "version": "0.1.0", "title": "去指定地点", "motion": True})
        self.assertIs(body["skills"]["status_report"]["motion"], False)
        self.assertEqual(body["calls"], [])
        self.assertEqual(http(self.fx.url, "/v2/remote", token="wrong-" * 5)[0], 401)

    def test_turning_it_on_needs_a_person_and_a_name(self):
        self.install("goto_place")
        for payload, expected in (
                ({**self.on(), "hardware_authorized": False}, (403, "hardware_not_authorized")),
                ({k: v for k, v in self.on().items() if k != "hardware_authorized"}, (403, "hardware_not_authorized")),
                ({**self.on(), "hardware_authorized": "true"}, (403, "hardware_not_authorized")),
                ({**self.on(), "set_by": ""}, (400, "set_by_required")),
                ({**self.on(), "set_by": None}, (400, "set_by_required")),
                ({**self.on(), "set_by": "x" * 500}, (400, "bad_request")),
                ({**self.on(), "set_by": "王工\n管理员"}, (400, "bad_request")),
                ({**self.on(), "allowed": "yes"}, (400, "bad_request")),
                ({k: v for k, v in self.on().items() if k != "allowed"}, (400, "bad_request")),
                ({**self.on(), "skill_id": ""}, (400, "bad_request")),
                ({**self.on(), "skill_id": "patrol"}, (404, "skill_not_found")),
                ({k: v for k, v in self.on().items() if k != "digest"}, (409, "skill_changed")),
                ({**self.on(), "digest": "0" * 64}, (409, "skill_changed")),
                ({**self.on(), "allowed": False, "skill_id": "patrol"}, (404, "skill_not_found"))):
            with self.subTest(payload=payload):
                status, body = http(self.fx.url, "/v2/remote/skills", payload)
                self.assertEqual((status, body["code"]), expected)
        self.assertIs(http(self.fx.url, "/v2/remote")[1]["skills"]["goto_place"]["allowed"], False)

    def test_on_then_off_and_both_are_recorded(self):
        self.install("goto_place")
        status, body = http(self.fx.url, "/v2/remote/skills", {**self.on(), "request_id": "on-1"})
        self.assertEqual((status, body["accepted"], body["schema"]), (200, True, SCHEMA))
        switch = body["skills"]["goto_place"]
        self.assertEqual((switch["allowed"], switch["set_by"]), (True, "王工"))
        self.assertGreater(switch["set_at"], 1_600_000_000)
        status, body = http(self.fx.url, "/v2/remote/skills", {"skill_id": "goto_place", "allowed": False})
        self.assertEqual((status, body["skills"]["goto_place"]["allowed"]), (200, False))
        self.fx.runtime.records.flush()
        records = [r for r in self.fx.runtime.records.query("operation")["records"] if r["operation"] == "remote.switch"]
        self.assertEqual([(r["target"], r["allowed"], r["operator"]) for r in records],
                         [("goto_place", True, "王工"), ("goto_place", False, None)])

    def test_a_repeated_request_id_does_not_turn_it_back_on(self):
        self.install("goto_place")
        self.assertEqual(http(self.fx.url, "/v2/remote/skills", {**self.on(), "request_id": "on-1"})[0], 200)
        http(self.fx.url, "/v2/remote/skills", {"skill_id": "goto_place", "allowed": False})
        status, body = http(self.fx.url, "/v2/remote/skills", {**self.on(), "request_id": "on-1"})     # a late retry
        self.assertEqual((status, body["skills"]["goto_place"]["allowed"]), (200, False))
        status, body = http(self.fx.url, "/v2/remote/skills", {**self.on(), "request_id": "on-2"})
        self.assertEqual((status, body["skills"]["goto_place"]["allowed"]), (200, True))
        status, body = http(self.fx.url, "/v2/remote/skills", {"skill_id": "goto_place", "allowed": False,
                                                               "request_id": "on-2"})
        self.assertEqual((status, body["skills"]["goto_place"]["allowed"]), (200, False), "off is never a replay")

    def test_the_switch_survives_a_restart_and_is_bound_to_the_content(self):
        self.fx.install(custom_package("hello", "def run(ctx):\n    return {'v': 1}\n"))
        self.install("goto_place")
        for skill in ("hello", "goto_place"):
            self.assertEqual(http(self.fx.url, "/v2/remote/skills", {**self.on(skill)})[0], 200)
        self.fx.close()
        self.boot()
        skills = http(self.fx.url, "/v2/remote")[1]["skills"]
        self.assertEqual([(skills[s]["allowed"], skills[s]["set_by"]) for s in ("hello", "goto_place")],
                         [(True, "王工"), (True, "王工")])
        newer = {**custom_package("hello", "def run(ctx):\n    return {'v': 2}\n"), "version": "0.2.0"}
        self.fx.install(newer)
        skills = http(self.fx.url, "/v2/remote")[1]["skills"]
        self.assertEqual((skills["hello"]["allowed"], skills["hello"]["invalidated"], skills["hello"]["version"]),
                         (False, True, "0.2.0"))
        self.assertIs(skills["goto_place"]["allowed"], True)

    def test_a_stored_switch_nobody_wrote_this_way_is_off(self):
        self.install("goto_place", "status_report")
        self.assertEqual(http(self.fx.url, "/v2/remote/skills", self.on())[0], 200)     # switches have been set here
        digest = {s["id"]: s["digest"] for s in self.fx.runtime.skills.list()}
        good = {"allowed": True, "digest": digest["status_report"], "set_by": "王工", "set_at": 1_800_000_000.0}
        self.fx.runtime.store.put(STORE_KIND, {"id": SWITCHES_ID, "schema": SCHEMA, "skills": {
            "goto_place": {"allowed": True, "digest": digest["goto_place"], "set_by": "", "set_at": 1.0},
            "status_report": good}})
        self.fx.close()
        self.boot()
        skills = http(self.fx.url, "/v2/remote")[1]["skills"]
        self.assertEqual((skills["goto_place"]["allowed"], skills["status_report"]["allowed"]), (False, True))
        self.fx.runtime.store.put(STORE_KIND, {"id": SWITCHES_ID, "schema": "something-else", "skills": {"status_report": good}})
        self.fx.close()
        self.boot()
        body = http(self.fx.url, "/v2/remote")[1]
        self.assertIs(body["skills"]["status_report"]["allowed"], False)
        self.assertTrue(body["invalid"])

    def test_checked_entry(self):
        good = {"allowed": True, "digest": "d" * 64, "set_by": "王工", "set_at": 1.0}
        self.assertEqual(checked_entry(good), good)
        for bad in (None, [], {}, {**good, "allowed": 1}, {**good, "digest": ""}, {**good, "digest": 5},
                    {**good, "set_at": "now"}, {**good, "set_at": True}, {**good, "set_by": None},
                    {**good, "set_by": "a‮b"}):
            with self.subTest(bad=bad):
                self.assertIsNone(checked_entry(bad))
        self.assertEqual(checked_entry({**good, "allowed": False, "set_by": None})["set_by"], None)

    def test_a_write_that_does_not_reach_the_disk_does_not_turn_it_on(self):
        from unittest import mock
        self.install("goto_place")
        with mock.patch.object(self.fx.runtime.store, "put", side_effect=OSError(28, "No space left on device")):
            status, body = http(self.fx.url, "/v2/remote/skills", self.on())
            self.assertEqual((status, body["code"]), (500, "remote_not_saved"))
            self.assertIs(http(self.fx.url, "/v2/remote")[1]["skills"]["goto_place"]["allowed"], False)
        self.assertEqual(http(self.fx.url, "/v2/remote/skills", self.on())[0], 200)
        with mock.patch.object(self.fx.runtime.store, "put", side_effect=OSError(28, "No space left on device")):
            status, body = http(self.fx.url, "/v2/remote/skills", {"skill_id": "goto_place", "allowed": False})
            self.assertEqual((status, body["code"]), (500, "remote_not_saved"))
            self.assertIs(http(self.fx.url, "/v2/remote")[1]["skills"]["goto_place"]["allowed"], False,
                          "off in memory all the same")

    def test_status_carries_the_link(self):
        view = http(self.fx.url, "/v1/status")[1]["cloud_link"]
        self.assertEqual((view["configured"], view["enabled"], view["state"]), (False, False, "off"))


class ConfigurationFile(unittest.TestCase):
    def write(self, content: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "cloud-report.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(path, 0o600)
        return path

    def test_link_is_on_unless_the_file_says_false(self):
        base = '"url": "https://cloud.example.com", "key": "nxk_0123456789abcdef0123"'
        self.assertIs(load_config(self.write("{" + base + "}")).link, True)
        self.assertIs(load_config(self.write("{" + base + ', "link": true}')).link, True)
        self.assertIs(load_config(self.write("{" + base + ', "link": false}')).link, False)
        for bad in ('"no"', "0", "null"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_config(self.write("{" + base + ', "link": ' + bad + "}"))


if __name__ == "__main__":
    unittest.main()
