"""Issue #43, part 3 (cloud side): a robot keeps a link to the cloud with ``POST /device-link/sync``.

Covered here: who may sync, what a sync must look like, what the cloud keeps of what the robot says
(bounded and cleaned), the per-organisation and per-robot bounds, online / offline, and that a robot
key of one organisation can neither see nor settle anything of another. The life of a call, driven
through the same endpoint, is in ``test_open_platform.py``.
"""
import json

import support  # noqa: F401
from support import API
from skill_support import DIGEST, INVOKE_SCOPE, LINK_SCOPE, SkillCase, device_skill


class Syncing(SkillCase):
    def robots(self, key=None) -> list[dict]:
        return self.ok(self.get("/open/robots", headers=(key or self.caller)["headers"]))["robots"]

    def skills(self, device_id: str) -> list[dict]:
        return self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["skills"]

    def test_the_first_sync_makes_the_robot_known(self):
        answer = self.sync()
        self.assertRegex(answer["device_id"], r"^dlk_[0-9a-f]{20}$")
        self.assertEqual((answer["accepted"], answer["poll_after_s"], answer["calls"], answer["cancel"], answer["stop"],
                          answer["settled"], answer["rejected_skills"]), (True, 2, [], [], None, [], []))
        (robot,) = self.robots()
        self.assertEqual(robot, {"device_id": answer["device_id"], "instance_id": "dog-1", "name": "Dog",
                                 "family": "quadruped", "simulated": True, "online": True, "estop_latched": False,
                                 "busy": False, "motion_blocked": False, "runtime_version": "0.5.0",
                                 "last_seen_at": self.clock.at, "skills": 1})
        (skill,) = self.skills(answer["device_id"])
        self.assertEqual((skill["id"], skill["version"], skill["title"], skill["motion"], skill["timeout_s"],
                          skill["remote_allowed"]), ("goto_place", "0.1.0", "去指定地点", True, 1800, True))
        self.assertEqual(skill["requires"], ["base.navigate_to_place", "safety.stop"])
        self.assertEqual([p["name"] for p in skill["parameters"]], ["place", "max_speed"])
        self.assertEqual(len(self.audits("device_link.first_sync")), 1)

    def test_a_robot_keeps_its_id_and_another_robot_gets_another(self):
        first = self.online()
        self.assertEqual(self.online(), first)
        self.assertNotEqual(self.online(instance_id="dog-2"), first)
        self.assertEqual(sorted(r["instance_id"] for r in self.robots()), ["dog-1", "dog-2"])
        self.assertEqual(len(self.audits("device_link.first_sync")), 2)

    def test_what_the_robot_says_replaces_what_it_said_before(self):
        device_id = self.online()
        self.clock.advance(2)
        self.sync(skills=[device_skill("wave", allowed=False, motion=False, requires=["system.health"])],
                  state={"estop_latched": True, "busy": True, "motion_blocked": True})
        (robot,) = self.robots()
        self.assertEqual((robot["estop_latched"], robot["busy"], robot["motion_blocked"], robot["last_seen_at"]),
                         (True, True, True, self.clock.at))
        (skill,) = self.skills(device_id)
        self.assertEqual((skill["id"], skill["remote_allowed"], skill["motion"]), ("wave", False, False))
        self.sync(skills=[])
        self.assertEqual(self.skills(device_id), [])

    def test_a_change_of_what_may_be_called_remotely_is_recorded(self):
        self.online(skills=[device_skill("a", allowed=True), device_skill("b", allowed=False)])
        self.sync(skills=[device_skill("a", allowed=True), device_skill("b", allowed=False)])
        self.sync(skills=[device_skill("a", allowed=False), device_skill("b", allowed=True)])
        self.sync(skills=[device_skill("a", allowed=False, title="改了名字"), device_skill("b", allowed=True)])
        changes = [(detail["opened"], detail["closed"]) for _actor, _target, detail in
                   self.audits("device_link.remote_changed")]
        self.assertEqual(changes, [(["a"], []), (["b"], ["a"])])

    def test_a_robot_that_stops_syncing_is_offline_after_a_minute(self):
        device_id = self.online()
        self.clock.advance(60)
        self.assertTrue(self.robots()[0]["online"])
        self.clock.advance(1)
        self.assertFalse(self.robots()[0]["online"])
        self.assertFalse(self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["online"])
        self.sync()
        self.assertTrue(self.robots()[0]["online"])


class WhoMaySync(SkillCase):
    def test_only_an_api_key_with_the_scope(self):
        self.error(self.sync_raw(key=self.owner), 403, "api_key_required")       # a login session, even the owner's
        self.error(self.sync_raw(key=self.caller), 403, "scope_missing")
        self.error(self.sync_raw(key=self.api_key(self.owner, ["alerts:report"])), 403, "scope_missing")
        self.error(self.sync_raw(key=self.api_key(self.owner, ["robots", "gateway"])), 403, "scope_missing")
        self.error(self.client.post(f"{API}/device-link/sync", json={}), 401)
        self.assertEqual(self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"], [])

    def test_one_key_may_report_alerts_and_keep_the_link(self):
        both = self.api_key(self.owner, ["alerts:report", LINK_SCOPE])
        self.sync(key=both)
        report = {"schema": "neuxmind.device_report.v1", "device": {"instance_id": "dog-1"}, "alerts": []}
        self.ok(self.post("/device-reports", report, headers=both["headers"]))

    def test_the_link_key_does_nothing_else(self):
        for path in ("/open/robots", "/skills", "/alerts", "/robots", "/wallet", "/library/materials"):
            with self.subTest(path=path):
                self.error(self.get(path, headers=self.robot_key["headers"]), 403, "scope_missing")
        self.error(self.post("/open/robots/dlk_00000000000000000000/stop", {}, headers=self.robot_key["headers"]), 403,
                   "scope_missing")

    def test_every_role_may_hold_the_link_scope_but_a_field_operator_not_the_invoke_scope(self):
        operator = self.register("op@example.com")
        self.ok(self.post("/org/members", {"email": "op@example.com", "role": "operator"}, self.owner))
        operator["headers"] = {**operator["headers"], "X-NeuxMind-Org": self.owner["org_id"]}
        self.error(self.post("/api-keys", {"name": "k", "scopes": [INVOKE_SCOPE]}, operator), 403)
        self.error(self.post("/api-keys", {"name": "k", "scopes": ["skills:write"]}, operator), 403)

    def test_an_unverified_account_s_key_does_not_sync(self):
        fresh = self.register("fresh@example.com", verify=False)
        self.error(self.sync_raw(key=self.api_key(fresh, [LINK_SCOPE])), 403, "email_unverified")

    def test_a_revoked_key_stops_working(self):
        self.sync()
        self.ok(self.delete(f"/api-keys/{self.robot_key['id']}", self.owner))
        self.error(self.sync_raw(), 401)


class Hygiene(SkillCase):
    def test_a_sync_that_is_not_a_sync_is_refused(self):
        for over in ({"schema": "something.else"}, {"device": None}, {"device": {"instance_id": ""}},
                     {"device": {"instance_id": "x" * 201}}, {"device": {"instance_id": "a\nb"}},
                     {"device": {"instance_id": "a\x00b"}}, {"device": {"instance_id": 7}}, {"skills": "none"},
                     {"skills": [device_skill(f"s{i}") for i in range(201)]}, {"calls": "none"},
                     {"calls": [{"call_id": "cal_" + "0" * 20, "status": "running"}] * 51}, {"stops": {}},
                     {"stops": ["stp_" + "0" * 20] * 51}, {"state": "fine"}):
            with self.subTest(over=str(over)[:50]):
                self.error(self.sync_raw(**over), 400, "invalid_sync")
        self.error(self.post("/device-link/sync", ["not", "an", "object"], headers=self.robot_key["headers"]), 400,
                   "invalid_sync")
        self.assertEqual(self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"], [])

    def test_two_hundred_skills_fit_although_they_exceed_the_general_body_limit(self):
        skills = [device_skill(f"skill-{i:03d}", description="d" * 1500) for i in range(200)]
        self.assertGreater(len(json.dumps(skills)), self.settings.max_body_bytes)
        answer = self.sync(skills=skills)
        self.assertEqual(answer["rejected_skills"], [])
        (robot,) = self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"]
        self.assertEqual(robot["skills"], 200)

    def test_the_sync_body_has_its_own_bound_and_only_for_a_live_credential(self):
        huge = json.dumps({"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"},
                           "padding": "x" * (2 * 1024 * 1024)})
        headers = {"Content-Type": "application/json"}
        self.error(self.client.post(f"{API}/device-link/sync", content=huge,
                                    headers={**headers, **self.robot_key["headers"]}), 413, "payload_too_large")
        self.error(self.client.post(f"{API}/device-link/sync", content=huge, headers=headers), 401)
        self.error(self.client.post(f"{API}/device-link/sync", content=huge,
                                    headers={**headers, "Authorization": "Bearer nx_live_" + "0" * 40}), 401)

    def test_bad_skill_entries_are_refused_one_by_one(self):
        bad = ["not an object", device_skill("Bad Id"), device_skill("no-version", version="one"),
               device_skill("no-digest", digest="abc"), device_skill("params", parameters="none"),
               device_skill("param-type", parameters=[{"name": "n"}]),
               device_skill("param-range", parameters=[{"name": "n", "type": "int", "min": 5, "max": 1}]),
               device_skill("param-twice", parameters=[{"name": "n", "type": "int"}, {"name": "n", "type": "int"}]),
               device_skill("requires", requires="base.velocity"), device_skill("requires-item", requires=[7]),
               device_skill("timeout", timeout_s=-1), device_skill("timeout-type", timeout_s="soon"),
               device_skill("too-big", parameters=[{"name": "n", "type": "str", "options": ["x" * 9000]}]),
               device_skill("nul", parameters=[{"name": "n", "type": "str", "description": "a\x00b"}]),
               device_skill("nan", parameters=[{"name": "n", "type": "float", "max": float("nan")}])]
        body = {"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"},
                "skills": [device_skill("ok-1"), *bad, device_skill("ok-2")]}
        answer = self.ok(self.post_text("/device-link/sync", body, self.robot_key["headers"]))
        self.assertEqual(len(answer["rejected_skills"]), len(bad))
        self.assertTrue(all(r["error"] for r in answer["rejected_skills"]))
        self.assertEqual([r["index"] for r in answer["rejected_skills"]], list(range(1, len(bad) + 1)))
        self.assertIn("no-digest", {r["id"] for r in answer["rejected_skills"]})
        skills = self.ok(self.get(f"/open/robots/{answer['device_id']}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual([s["id"] for s in skills], ["ok-1", "ok-2"])

    def test_descriptive_text_is_cleaned_not_trusted(self):
        body = {"schema": "neuxmind.device_link.v1",
                "device": {"instance_id": "dog-1", "name": "Do\x00g\x07 \ud800" + "n" * 300, "family": 7,
                           "simulated": "yes", "runtime_version": "v" * 100},
                "state": {"estop_latched": "no", "busy": 1},
                "skills": [device_skill(title="标\x00题", description="说\x1b明" + "d" * 3000,
                                        remote={"allowed": True, "set_by": "a\x00b" + "x" * 500, "set_at": "noon"})]}
        answer = self.ok(self.post_text("/device-link/sync", body, self.robot_key["headers"]))
        (robot,) = self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"]
        self.assertNotIn("\x00", robot["name"])
        self.assertNotIn("\x07", robot["name"])
        self.assertEqual((len(robot["name"]), robot["family"], robot["simulated"], len(robot["runtime_version"])),
                         (200, "", False, 40))
        # fail closed: a flag that is not a boolean, or missing, counts as set
        self.assertEqual((robot["estop_latched"], robot["busy"], robot["motion_blocked"]), (True, True, True))
        (skill,) = self.ok(self.get(f"/open/robots/{answer['device_id']}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual(skill["title"], "标 题")
        self.assertEqual(len(skill["description"]), 2000)
        self.assertNotIn("\x1b", skill["description"])

    def test_remote_calls_are_allowed_only_by_a_literal_true(self):
        skills = [device_skill("a", remote={"allowed": "true"}), device_skill("b", remote={"allowed": 1}),
                  device_skill("c", remote=None), device_skill("d", remote={"allowed": True}),
                  {k: v for k, v in device_skill("e").items() if k != "remote"}]
        device_id = self.online(skills=skills)
        listed = self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual({s["id"]: s["remote_allowed"] for s in listed},
                         {"a": False, "b": False, "c": False, "d": True, "e": False})

    def test_motion_is_not_taken_on_the_robot_s_word_alone(self):
        skills = [device_skill("says-no", motion=False),                                   # needs base.navigate_to_place
                  device_skill("unknown", motion=False, requires=["made.up.capability"]),
                  device_skill("vendor", motion=False, requires=["x.vendor.dance"]),
                  device_skill("reads", motion=False, requires=["system.health"]),
                  device_skill("says-yes", motion=True, requires=["system.health"])]
        device_id = self.online(skills=skills)
        listed = self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual({s["id"]: s["motion"] for s in listed},
                         {"says-no": True, "unknown": True, "vendor": True, "reads": False, "says-yes": True})

    def test_of_two_versions_of_one_skill_the_newer_is_kept(self):
        device_id = self.online(skills=[device_skill(version="0.9.0"), device_skill(version="0.10.0"),
                                        device_skill(version="0.2.0")])
        listed = self.ok(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]))["skills"]
        self.assertEqual([(s["id"], s["version"]) for s in listed], [("goto_place", "0.10.0")])

    def test_reports_that_make_no_sense_are_ignored(self):
        answer = self.sync(calls=["x", {"call_id": 7}, {"call_id": "cal_nope", "status": "running"},
                                  {"status": "succeeded"}, {"call_id": "cal_" + "a" * 20, "status": "exploded"}],
                           stops=[7, "stp_nope", None, "stp_" + "b" * 20])
        self.assertEqual(answer["settled"], ["cal_" + "a" * 20])           # not a call of this robot: stop reporting it
        self.assertIsNone(answer["stop"])
        # an id where a report object belongs, and numbers no float can hold, are not a server error
        device_id = answer["device_id"]
        call_id = self.call(device_id)["id"]
        self.sync(calls=[call_id, "cal_" + "c" * 20])
        self.assertEqual(self.seen(call_id)["status"], "dispatched")
        body = {"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"},
                "state": {"estop_latched": False, "busy": False, "motion_blocked": False},
                "skills": [device_skill(), device_skill("huge", parameters=[{"name": "n", "type": "int", "max": 10 ** 400}],
                                                        timeout_s=10 ** 400)],
                "calls": [{"call_id": call_id, "status": "running", "progress": 10 ** 400}]}
        answer = self.ok(self.post_text("/device-link/sync", body, self.robot_key["headers"]))
        self.assertEqual([r["id"] for r in answer["rejected_skills"]], ["huge"])
        self.assertEqual((self.seen(call_id)["status"], self.seen(call_id)["progress"]), ("running", 1.0))

    def test_a_missing_state_is_not_a_clean_bill(self):
        device_id = self.online()
        body = {"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"}, "skills": [device_skill()]}
        self.ok(self.post("/device-link/sync", body, headers=self.robot_key["headers"]))
        self.error(self.call_raw(device_id), 409, "estop_latched")

    def test_a_login_session_gets_no_larger_body_for_a_sync(self):
        huge = json.dumps({"schema": "neuxmind.device_link.v1", "device": {"instance_id": "dog-1"}, "p": "x" * 300_000})
        response = self.client.post(f"{API}/device-link/sync", content=huge,
                                    headers={**self.owner["headers"], "Content-Type": "application/json"})
        self.error(response, 413, "payload_too_large")


class Bounds(SkillCase):
    settings_overrides = {"device_link_max_devices": 2, "device_link_max_syncs_per_minute": 3}

    def test_robots_per_organisation(self):
        self.sync(instance_id="dog-1")
        self.sync(instance_id="dog-2")
        self.error(self.sync_raw(instance_id="dog-3"), 409, "too_many_devices")
        self.sync(instance_id="dog-1")                                    # the ones that are known keep syncing
        other = self.register("other@example.com")
        self.sync(instance_id="dog-3", key=self.api_key(other, [LINK_SCOPE]))   # another organisation has its own room

    def test_syncs_per_robot_per_minute(self):
        for _ in range(3):
            self.sync()
        refused = self.error(self.sync_raw(), 429, "rate_limited")
        self.assertGreaterEqual(refused["retry_after"], 1)
        self.sync(instance_id="dog-2")                                    # per robot, not per key
        self.clock.advance(61)
        self.sync()


class OtherOrganisations(SkillCase):
    """A robot key speaks for robots of its own organisation only, whatever ids it names."""

    def setUp(self):
        super().setUp()
        self.device_id = self.online()
        self.call_id = self.call(self.device_id)["id"]
        self.stop_id = None
        self.other = self.register("other@example.com")
        self.other_robot = self.api_key(self.other, [LINK_SCOPE])
        self.other_caller = self.api_key(self.other, [INVOKE_SCOPE])

    def test_the_same_instance_id_in_another_organisation_is_another_robot(self):
        theirs = self.sync(key=self.other_robot)
        self.assertNotEqual(theirs["device_id"], self.device_id)
        self.assertEqual(theirs["calls"], [])                              # our queued call is not handed to them
        self.assertEqual(self.seen(self.call_id)["status"], "queued")
        robots = self.ok(self.get("/open/robots", headers=self.other_caller["headers"]))["robots"]
        self.assertEqual([r["device_id"] for r in robots], [theirs["device_id"]])

    def test_another_organisation_s_robot_cannot_settle_our_call(self):
        self.assertEqual([c["call_id"] for c in self.sync()["calls"]], [self.call_id])
        forged = [{"call_id": self.call_id, "status": "succeeded", "result": {"forged": True}}]
        answer = self.sync(key=self.other_robot, calls=forged)
        self.assertEqual(answer["settled"], [self.call_id])                # "not yours", as for any unknown id
        call = self.seen(self.call_id)
        self.assertEqual((call["status"], call["result"]), ("dispatched", None))
        # and neither can another robot of our own organisation
        self.sync(instance_id="dog-2", calls=forged)
        self.assertEqual(self.seen(self.call_id)["status"], "dispatched")
        self.assertEqual([c["call_id"] for c in self.sync()["calls"]], [self.call_id])    # still ours to run

    def test_another_organisation_s_robot_cannot_acknowledge_our_stop(self):
        stop = self.ok(self.post(f"/open/robots/{self.device_id}/stop", {"reason": "测试"},
                                 headers=self.caller["headers"]), 202)
        self.sync(key=self.other_robot, stops=[stop["stop_id"]])
        self.sync(instance_id="dog-2", stops=[stop["stop_id"]])
        seen = self.ok(self.get(f"/open/stops/{stop['stop_id']}", headers=self.caller["headers"]))["stop"]
        self.assertEqual(seen["status"], "pending")
        self.assertEqual(self.sync()["stop"], {"stop_id": stop["stop_id"], "reason": "测试"})

    def test_another_organisation_s_caller_sees_and_touches_nothing_of_ours(self):
        headers = self.other_caller["headers"]
        self.assertEqual(self.ok(self.get("/open/robots", headers=headers))["robots"], [])
        self.assertEqual(self.ok(self.get("/open/calls", headers=headers))["calls"], [])
        self.error(self.get(f"/open/robots/{self.device_id}/skills", headers=headers), 404, "device_not_found")
        self.error(self.call_raw(self.device_id, request_id="theirs", key=self.other_caller), 404, "device_not_found")
        self.error(self.get(f"/open/calls/{self.call_id}", headers=headers), 404, "call_not_found")
        self.error(self.post(f"/open/calls/{self.call_id}/cancel", {}, headers=headers), 404, "call_not_found")
        self.error(self.post(f"/open/robots/{self.device_id}/stop", {}, headers=headers), 404, "device_not_found")
        self.assertEqual(self.seen(self.call_id)["status"], "queued")
        # the same request_id in another organisation is that organisation's own
        theirs = self.online(key=self.other_robot)
        self.assertNotEqual(self.call(theirs, key=self.other_caller)["id"], self.call_id)


class Digest(SkillCase):
    def test_the_dispatched_call_names_the_installed_skill(self):
        device_id = self.online()
        call = self.call(device_id, parameters={"place": "二楼", "max_speed": 1})
        answer = self.sync()
        (sent,) = answer["calls"]
        self.assertEqual(sent, {"call_id": call["id"], "skill_id": "goto_place", "version": "0.1.0", "digest": DIGEST,
                                "parameters": {"place": "二楼", "max_speed": 1}, "timeout_s": 1800,
                                "created_at": call["created_at"]})
        # the robot judges the call's age from these two, both on the cloud's clock
        self.assertGreaterEqual(answer["server_time"], sent["created_at"])


class AnswerSize(SkillCase):
    """The robot discards an answer of more than 64 KB -- and with it a stop. The answer stays small
    whatever the robot and the caller send."""

    def test_at_most_twenty_rejected_skills_are_explained_briefly(self):
        bad = [device_skill(f"bad-{i:03d}", parameters=[{"name": "参" * 100, "type": "int", "min": 5, "max": 1}])
               for i in range(150)]
        answer = self.sync(skills=[device_skill(), *bad])
        self.assertEqual((len(answer["rejected_skills"]), answer["rejected_skills_total"]), (20, 150))
        self.assertTrue(all(len(r["error"]) <= 120 for r in answer["rejected_skills"]))

    def test_the_worst_answer_stays_well_under_the_robot_s_limit(self):
        declared = [{"name": "blob", "type": "object"}]
        good = device_skill("carry", parameters=declared, requires=["system.health"], motion=False)
        bad = [device_skill("b" * 97 + f"{i:03d}", parameters=[{"name": "参" * 100, "type": "int", "min": 5, "max": 1}])
               for i in range(150)]
        device_id = self.online(skills=[good])
        text = "字" * 5000                                                 # close to the 16 KiB a call may carry
        call = self.call(device_id, skill_id="carry", parameters={"blob": {"text": text, "pad": "x" * 1300}})
        self.ok(self.post(f"/open/robots/{device_id}/stop", {"reason": "因" * 200}, headers=self.caller["headers"]), 202)
        self.ok(self.post(f"/open/calls/{call['id']}/cancel", {}, headers=self.caller["headers"]))
        unknown = [{"call_id": "cal_" + f"{i:020x}", "status": "running"} for i in range(50)]
        response = self.sync_raw(skills=[good, *bad], calls=unknown)
        answer = self.ok(response)
        self.assertEqual((len(answer["settled"]), len(answer["rejected_skills"])), (50, 20))
        self.assertIsNotNone(answer["stop"])
        self.assertLess(len(response.content), 48_000)

    def test_an_answer_carrying_a_call_of_the_largest_size_stays_under_the_limit_too(self):
        declared = [{"name": "blob", "type": "object"}]
        good = device_skill("carry", parameters=declared, requires=["system.health"], motion=False)
        bad = [device_skill("b" * 97 + f"{i:03d}", parameters="none") for i in range(150)]
        device_id = self.online(skills=[good])
        parameters = {"blob": {"text": "字" * 5000, "pad": "x" * 1300}}
        self.assertGreater(len(json.dumps(parameters, ensure_ascii=False).encode()), 16_000)
        call = self.call(device_id, skill_id="carry", parameters=parameters)
        unknown = [{"call_id": "cal_" + f"{i:020x}", "status": "running"} for i in range(50)]
        response = self.sync_raw(skills=[good, *bad], calls=unknown)
        answer = self.ok(response)
        self.assertEqual([c["call_id"] for c in answer["calls"]], [call["id"]])
        self.assertEqual(len(answer["calls"]), 1)
        self.assertLess(len(response.content), 48_000)
