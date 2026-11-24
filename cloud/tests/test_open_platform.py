"""Issue #43, part 4: third parties call a skill on a robot through the cloud (``/open/*``).

A pretend robot drives every call through ``POST /device-link/sync``, on a clock the test moves.
Covered: the life of a call (queued → dispatched → sent again until reported → running → succeeded;
rejected; cancelled while queued, while dispatched and while running; stopped; expired; lost), every
refusal of the contract, idempotency by ``request_id``, the rate and storage bounds, hostile input and
who may use the API at all.
"""
import support  # noqa: F401
from support import API
from skill_support import INVOKE_SCOPE, SkillCase, device_skill

from neuxmind_cloud import maintenance, skill_calls

HEALTH = dict(requires=["system.health"], motion=False)


class OpenCase(SkillCase):
    def setUp(self):
        super().setUp()
        self.device_id = self.online()

    def report(self, call_id: str, status: str, **fields) -> dict:
        return self.sync(calls=[{"call_id": call_id, "status": status, **fields}])

    def started(self, **kwargs) -> str:
        """A call the robot has fetched and reports as running."""
        call_id = self.call(self.device_id, **kwargs)["id"]
        self.sync()
        self.report(call_id, "running", run_id="run-1")
        return call_id

    def cancel(self, call_id: str, key=None) -> dict:
        return self.ok(self.post(f"/open/calls/{call_id}/cancel", {}, headers=(key or self.caller)["headers"]))["call"]

    def stop_raw(self, device_id=None, body=None, key=None):
        return self.post(f"/open/robots/{device_id or self.device_id}/stop", body, headers=(key or self.caller)["headers"])

    def stop(self, **kwargs) -> dict:
        return self.ok(self.stop_raw(**kwargs), 202)

    def stop_seen(self, stop_id: str) -> dict:
        return self.ok(self.get(f"/open/stops/{stop_id}", headers=self.caller["headers"]))["stop"]

    def listed(self, query: str = "") -> dict:
        return self.ok(self.get("/open/calls" + query, headers=self.caller["headers"]))


class LifeOfACall(OpenCase):
    def test_queued_dispatched_running_succeeded(self):
        call = self.call(self.device_id, parameters={"place": "会议室"})
        self.assertRegex(call["id"], r"^cal_[0-9a-f]{20}$")
        self.assertEqual((call["status"], call["device_id"], call["skill_id"], call["version"], call["parameters"]),
                         ("queued", self.device_id, "goto_place", "0.1.0", {"place": "会议室"}))
        self.assertEqual((call["progress"], call["result"], call["error"], call["dispatched_at"], call["finished_at"]),
                         (None, None, None, None, None))
        self.assertEqual((call["request_id"], call["created_by"], call["created_at"]),
                         ("req-1", f"key:{self.caller['id']}", self.clock.at))

        self.clock.advance(2)
        answer = self.sync()
        self.assertEqual([c["call_id"] for c in answer["calls"]], [call["id"]])
        self.assertEqual((answer["poll_after_s"], answer["cancel"], answer["settled"]), (1, [], []))
        seen = self.seen(call["id"])
        self.assertEqual((seen["status"], seen["dispatched_at"]), ("dispatched", self.clock.at))

        # the answer may not have arrived: until the robot mentions the call it is sent again, unchanged
        self.clock.advance(2)
        self.assertEqual(self.sync()["calls"], answer["calls"])
        self.assertEqual(self.seen(call["id"])["dispatched_at"], seen["dispatched_at"])

        self.clock.advance(2)
        answer = self.report(call["id"], "running", run_id="run-7", progress=0.4, detail="正在走")
        self.assertEqual((answer["calls"], answer["settled"], answer["poll_after_s"]), ([], [], 1))
        seen = self.seen(call["id"])
        self.assertEqual((seen["status"], seen["progress"], seen["detail"], seen["started_at"]),
                         ("running", 0.4, "正在走", self.clock.at))

        self.clock.advance(5)
        answer = self.report(call["id"], "succeeded", run_id="run-7", result={"arrived": True, "place": "会议室"})
        self.assertEqual((answer["settled"], answer["calls"], answer["poll_after_s"]), ([call["id"]], [], 2))
        done = self.seen(call["id"])
        self.assertEqual((done["status"], done["result"], done["error"], done["progress"], done["finished_at"]),
                         ("succeeded", {"arrived": True, "place": "会议室"}, None, 1.0, self.clock.at))

        # the robot repeats the final state until it reads "settled"; nothing changes any more
        self.clock.advance(2)
        again = self.report(call["id"], "failed", error={"code": "late", "message": "不该生效"})
        self.assertEqual(again["settled"], [call["id"]])
        self.assertEqual(self.seen(call["id"]), done)
        self.assertEqual(self.sync()["settled"], [])

        (created,) = self.audits("skill.call.create")
        self.assertEqual((created[0], created[1]), (f"key:{self.caller['id']}", call["id"]))
        self.assertEqual((created[2]["device_id"], created[2]["skill_id"], created[2]["motion"]),
                         (self.device_id, "goto_place", True))
        self.assertNotIn("parameters", created[2])                         # a digest of them, not the values
        self.assertEqual([detail["status"] for _a, _t, detail in self.audits("skill.call.finished")], ["succeeded"])

    def test_the_robot_refuses_a_call(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()
        answer = self.report(call_id, "rejected", error={"code": "remote_not_allowed", "message": "主人刚刚关掉了开关"})
        self.assertEqual(answer["settled"], [call_id])
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"], seen["started_at"]),
                         ("rejected", {"code": "remote_not_allowed", "message": "主人刚刚关掉了开关"}, None))

    def test_a_failure_keeps_a_bounded_clean_error(self):
        call_id = self.started()
        self.report(call_id, "failed", error={"code": "Not A Code!", "message": "坏\x00了" + "x" * 2000},
                    detail="d" * 2000, progress=7)
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"]["code"], len(seen["error"]["message"]), len(seen["detail"]),
                          seen["progress"]), ("failed", "failed", 500, 500, 1.0))
        self.assertNotIn("\x00", seen["error"]["message"])

    def test_a_result_that_is_too_large_is_not_kept(self):
        call_id = self.started()
        self.report(call_id, "succeeded", result={"blob": "x" * 20_000})
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["result"]), ("succeeded", None))
        self.assertIn("结果没有保存", seen["detail"])

    def test_one_call_at_a_time_per_robot(self):
        first = self.call(self.device_id)["id"]
        refused = self.error(self.call_raw(self.device_id, request_id="req-2"), 409, "call_in_progress")
        self.assertEqual(refused["call_id"], first)
        self.sync()
        self.error(self.call_raw(self.device_id, request_id="req-2"), 409, "call_in_progress")
        self.report(first, "succeeded")
        self.call(self.device_id, request_id="req-2")
        other = self.online(instance_id="dog-2")                           # another robot is not held up
        self.call(other, request_id="req-3")


class Cancelling(OpenCase):
    def test_a_queued_call_is_cancelled_at_once_and_never_handed_over(self):
        call_id = self.call(self.device_id)["id"]
        seen = self.cancel(call_id)
        self.assertEqual((seen["status"], seen["finished_at"], seen["cancel_requested"]),
                         ("cancelled", self.clock.at, True))
        answer = self.sync()
        self.assertEqual((answer["calls"], answer["cancel"]), ([], []))
        self.assertEqual(self.cancel(call_id), seen)                       # again: nothing changes
        self.assertEqual(len(self.audits("skill.call.cancel")), 1)
        # should a robot ever claim to run it: it was never handed over, so the claim changes nothing
        self.assertEqual(self.report(call_id, "running")["settled"], [call_id])
        self.assertEqual(self.seen(call_id)["status"], "cancelled")

    def test_a_running_call_is_cancelled_by_the_robot(self):
        call_id = self.started()
        seen = self.cancel(call_id)
        self.assertEqual((seen["status"], seen["cancel_requested"]), ("running", True))
        answer = self.report(call_id, "running")
        self.assertEqual((answer["cancel"], answer["calls"]), ([call_id], []))
        self.assertEqual(self.sync()["cancel"], [call_id])                 # told again until it is over
        answer = self.report(call_id, "cancelled", error={"code": "cancelled", "message": "已取消"})
        self.assertEqual((answer["cancel"], answer["settled"]), ([], [call_id]))
        self.assertEqual(self.seen(call_id)["status"], "cancelled")

    def test_a_call_cancelled_on_its_way_that_the_robot_received(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()                                                        # handed over ...
        self.assertEqual(self.cancel(call_id)["status"], "dispatched")     # ... cancelled before the robot spoke of it
        answer = self.report(call_id, "running")                           # it did arrive
        self.assertEqual((answer["calls"], answer["cancel"]), ([], [call_id]))
        self.report(call_id, "cancelled")
        self.assertEqual(self.seen(call_id)["status"], "cancelled")

    def test_a_call_cancelled_on_its_way_that_never_arrived_is_not_sent_again(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()                                                        # this answer is lost
        self.cancel(call_id)
        answer = self.sync()                                               # the robot knows nothing of the call
        self.assertEqual((answer["calls"], answer["cancel"]), ([], [call_id]))        # never sent again, only cancelled
        self.assertEqual(self.seen(call_id)["status"], "dispatched")       # not settled on one sync's silence
        self.error(self.call_raw(self.device_id, request_id="req-2"), 409, "call_in_progress")
        self.clock.advance(29)
        self.assertEqual(self.sync()["cancel"], [call_id])
        self.clock.advance(2)
        answer = self.sync()
        self.assertEqual((answer["calls"], answer["cancel"]), ([], []))
        self.assertEqual(self.seen(call_id)["status"], "cancelled")
        self.call(self.device_id, request_id="req-2")                      # the robot is free again

    def test_what_the_robot_reports_replaces_what_the_cloud_assumed(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()
        self.cancel(call_id)
        self.clock.advance(31)
        self.sync()
        self.assertEqual(self.seen(call_id)["status"], "cancelled")        # assumed: never arrived
        waiting = self.call(self.device_id, request_id="req-2")["id"]
        answer = self.report(call_id, "running")                           # ... but it had
        self.assertEqual((self.seen(call_id)["status"], answer["cancel"]), ("running", [call_id]))
        self.assertEqual(answer["calls"], [])                              # and no second call is started beside it
        self.assertEqual(self.seen(waiting)["status"], "queued")
        answer = self.report(call_id, "cancelled")
        self.assertEqual(self.seen(call_id)["status"], "cancelled")
        self.assertEqual([c["call_id"] for c in answer["calls"]], [waiting])

    def test_cancelling_a_finished_call_changes_nothing(self):
        call_id = self.started()
        self.report(call_id, "succeeded", result=1)
        seen = self.cancel(call_id)
        self.assertEqual((seen["status"], seen["cancel_requested"], seen["result"]), ("succeeded", False, 1))
        self.assertEqual(self.audits("skill.call.cancel"), [])


class Stopping(OpenCase):
    def test_a_stop_is_delivered_until_the_robot_acknowledges_it(self):
        asked = self.stop(body={"reason": "有人走近"})
        self.assertRegex(asked["stop_id"], r"^stp_[0-9a-f]{20}$")
        self.assertFalse(asked["delivered"])
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "pending")
        self.clock.advance(1)
        self.assertEqual(self.sync()["stop"], {"stop_id": asked["stop_id"], "reason": "有人走近"})
        seen = self.stop_seen(asked["stop_id"])
        self.assertEqual((seen["status"], seen["delivered"], seen["delivered_at"]), ("delivered", True, self.clock.at))
        self.assertEqual(self.sync()["stop"]["stop_id"], asked["stop_id"])           # again until acknowledged
        self.clock.advance(1)
        answer = self.sync(stops=[asked["stop_id"]], state={"estop_latched": True})
        self.assertIsNone(answer["stop"])
        seen = self.stop_seen(asked["stop_id"])
        self.assertEqual((seen["status"], seen["acknowledged_at"]), ("acknowledged", self.clock.at))
        self.assertIsNone(self.sync(stops=[asked["stop_id"]], state={"estop_latched": True})["stop"])
        (request,) = self.audits("skill.stop.request")
        self.assertEqual((request[0], request[1], request[2]["reason"]),
                         (f"key:{self.caller['id']}", asked["stop_id"], "有人走近"))
        self.assertEqual(len(self.audits("skill.stop.acknowledged")), 1)

    def test_asking_again_while_a_stop_is_on_its_way_is_the_same_stop(self):
        first = self.stop()
        self.assertEqual(self.stop()["stop_id"], first["stop_id"])
        self.assertEqual(len(self.audits("skill.stop.request")), 1)        # the same stop: one record
        self.sync()
        again = self.stop()
        self.assertEqual((again["stop_id"], again["delivered"]), (first["stop_id"], True))
        self.sync(stops=[first["stop_id"]])
        self.assertNotEqual(self.stop()["stop_id"], first["stop_id"])      # a new one after the first was carried out

    def test_a_stop_ends_what_waits_and_reaches_what_runs(self):
        running = self.started()
        asked = self.stop()
        answer = self.report(running, "running")
        self.assertEqual((answer["stop"]["stop_id"], answer["cancel"]), (asked["stop_id"], [running]))
        self.report(running, "stopped", error={"code": "stopped", "message": "远程停止"})
        self.assertEqual(self.seen(running)["status"], "stopped")

        other = self.online(instance_id="dog-2")
        waiting = self.call(other, request_id="req-2")["id"]
        self.stop(device_id=other)
        seen = self.seen(waiting)
        self.assertEqual((seen["status"], seen["error"]["code"]), ("stopped", "stopped"))
        self.assertEqual(self.sync(instance_id="dog-2")["calls"], [])      # it is not handed over afterwards

    def test_no_new_call_while_a_stop_is_on_its_way_or_the_robot_is_latched(self):
        asked = self.stop()
        self.error(self.call_raw(self.device_id), 409, "stop_pending")
        self.sync()
        self.error(self.call_raw(self.device_id), 409, "stop_pending")
        self.sync(stops=[asked["stop_id"]], state={"estop_latched": True})
        self.error(self.call_raw(self.device_id), 409, "estop_latched")
        self.sync()                                                        # someone released it on site
        self.call(self.device_id)

    def test_a_stop_the_robot_never_fetched_is_dropped(self):
        asked = self.stop()
        self.clock.advance(121)
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "expired")
        self.assertIsNone(self.sync()["stop"])                             # an old stop does not latch a robot that comes back
        self.call(self.device_id)

    def test_a_stop_that_was_handed_over_stays_in_force_until_the_robot_says_it_stopped(self):
        asked = self.stop()
        self.sync()                                                        # handed over; no acknowledgement follows
        self.clock.advance(600)
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "delivered")
        self.assertEqual(self.sync()["stop"]["stop_id"], asked["stop_id"])
        self.error(self.call_raw(self.device_id), 409, "stop_pending")
        self.assertEqual(self.stop()["stop_id"], asked["stop_id"])
        self.sync(stops=[asked["stop_id"]])
        self.call(self.device_id)

    def test_a_stop_is_accepted_for_a_robot_that_seems_offline(self):
        self.clock.advance(90)
        asked = self.stop()
        self.assertFalse(asked["online"])
        self.clock.advance(5)
        self.assertEqual(self.sync()["stop"]["stop_id"], asked["stop_id"])

    def test_what_is_not_a_stop_request(self):
        self.error(self.stop_raw(body=["reason"]), 400, "invalid_request")
        self.error(self.stop_raw(body={"reason": 7}), 400, "invalid_request")
        self.error(self.stop_raw(device_id="dlk_" + "0" * 20), 404, "device_not_found")
        self.error(self.stop_raw(device_id="nonsense"), 404, "device_not_found")
        self.error(self.get("/open/stops/stp_" + "0" * 20, headers=self.caller["headers"]), 404, "stop_not_found")
        long_reason = self.stop(body={"reason": "因\x00为" + "x" * 500})
        self.assertEqual(len(self.stop_seen(long_reason["stop_id"])["reason"]), 200)


class TimeDecides(OpenCase):
    def test_a_call_nobody_fetched_expires_and_is_never_handed_over(self):
        call_id = self.call(self.device_id)["id"]
        self.clock.advance(29)
        self.assertEqual(self.seen(call_id)["status"], "queued")
        self.clock.advance(2)
        self.assertEqual(self.sync()["calls"], [])                         # the robot is back: too late
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"]["code"], seen["finished_at"]), ("expired", "expired", self.clock.at))
        self.assertEqual(self.report(call_id, "running")["settled"], [call_id])
        self.assertEqual(self.seen(call_id)["status"], "expired")

    def test_expiry_does_not_wait_for_the_robot(self):
        call_id = self.call(self.device_id)["id"]
        self.clock.advance(31)
        self.assertEqual(self.seen(call_id)["status"], "expired")

    def test_a_call_of_a_robot_gone_silent_is_lost_not_failed(self):
        call_id = self.started()
        self.clock.advance(119)
        self.assertEqual(self.seen(call_id)["status"], "running")
        self.clock.advance(2)
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"]["code"]), ("lost", "lost"))
        self.assertEqual(len(self.audits("skill.call.lost")), 1)
        # the robot comes back and knows how it ended: that replaces "unknown"
        answer = self.report(call_id, "succeeded", result={"arrived": True})
        self.assertEqual(answer["settled"], [call_id])
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"], seen["result"]), ("succeeded", None, {"arrived": True}))

    def test_the_sweep_marks_lost_calls_too(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()
        self.clock.advance(121)
        counts = maintenance.sweep(self.db, self.settings)
        self.assertEqual((counts["calls_lost"], counts["calls_expired"]), (1, 0))
        self.assertEqual(skill_calls.sweep(self.db, self.settings)["calls_lost"], 0)
        with self.db.transaction() as s:
            from neuxmind_cloud.skill_models import SkillCall
            self.assertEqual(s.get(SkillCall, call_id).status, "lost")

    def test_a_call_the_robot_no_longer_mentions_is_lost(self):
        call_id = self.started()
        for _ in range(4):
            self.clock.advance(30)
            self.sync()
            self.assertEqual(self.seen(call_id)["status"], "running")
        self.clock.advance(30)
        self.sync()
        self.assertEqual(self.seen(call_id)["status"], "lost")
        self.call(self.device_id, request_id="req-2")

    def test_a_call_on_its_way_is_sent_for_as_long_as_the_robot_syncs(self):
        call_id = self.call(self.device_id)["id"]
        for _ in range(4):                                                 # 0, 10, 20, 30 s after the hand-over
            answer = self.sync()
            self.assertEqual(([c["call_id"] for c in answer["calls"]], answer["cancel"]), ([call_id], []))
            self.clock.advance(10)
        # past the pick-up time it is never sent again: a call does not start late. The robot is told
        # to cancel it in case it did get it, and without a word from the robot the outcome is unknown.
        for _ in range(9):
            answer = self.sync()
            self.assertEqual((answer["calls"], answer["cancel"]), ([], [call_id]))
            self.assertEqual(self.seen(call_id)["status"], "dispatched")
            self.clock.advance(10)
        answer = self.sync()
        self.assertEqual((answer["calls"], answer["cancel"]), ([], []))
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"]["code"]), ("lost", "lost"))
        self.call(self.device_id, request_id="req-2")

    def test_an_answer_that_was_lost_is_not_made_up_for_two_minutes_later(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()                                                        # the answer with the call is lost ...
        self.clock.advance(110)                                            # ... and the robot is gone for a while
        answer = self.sync()
        self.assertEqual((answer["calls"], answer["cancel"]), ([], [call_id]))
        self.assertEqual(self.seen(call_id)["status"], "dispatched")
        self.clock.advance(11)
        self.assertEqual(self.sync()["calls"], [])
        self.assertEqual(self.seen(call_id)["status"], "lost")

    def test_a_robot_that_did_take_the_call_and_says_so_late_is_believed(self):
        call_id = self.call(self.device_id)["id"]
        self.sync()
        self.clock.advance(60)
        self.assertEqual(self.sync()["cancel"], [call_id])
        answer = self.report(call_id, "running")
        self.assertEqual((answer["calls"], answer["cancel"], self.seen(call_id)["status"]), ([], [], "running"))

    def test_a_call_far_past_its_time_limit_is_cancelled_by_the_cloud(self):
        call_id = self.started(timeout_s=60)
        for _ in range(6):
            self.clock.advance(30)
            self.assertEqual(self.report(call_id, "running")["cancel"], [])
        self.clock.advance(30)                                             # 60 s limit + 120 s grace are over
        self.assertEqual(self.report(call_id, "running")["cancel"], [call_id])
        self.assertTrue(self.seen(call_id)["cancel_requested"])
        self.assertEqual(len(self.audits("skill.call.timeout")), 1)
        self.report(call_id, "cancelled")
        self.assertEqual(self.seen(call_id)["status"], "cancelled")

    def test_finished_calls_and_vanished_robots_are_dropped_after_thirty_days(self):
        old = self.started()
        self.report(old, "succeeded")
        self.clock.advance(29 * 24 * 3600)
        self.sync(instance_id="dog-2")
        counts = skill_calls.sweep(self.db, self.settings)
        self.assertEqual((counts["calls_deleted"], counts["devices_forgotten"]), (0, 0))
        self.clock.advance(2 * 24 * 3600)
        counts = skill_calls.sweep(self.db, self.settings)
        self.assertEqual((counts["calls_deleted"], counts["devices_forgotten"]), (1, 1))
        self.error(self.get(f"/open/calls/{old}", headers=self.caller["headers"]), 404, "call_not_found")
        robots = self.ok(self.get("/open/robots", headers=self.caller["headers"]))["robots"]
        self.assertEqual([r["instance_id"] for r in robots], ["dog-2"])


class Refusals(OpenCase):
    def test_no_such_robot(self):
        for device_id in ("dlk_" + "0" * 20, "nonsense", "dlk_" + "g" * 20):
            with self.subTest(device_id=device_id):
                self.error(self.call_raw(device_id), 404, "device_not_found")
                self.error(self.get(f"/open/robots/{device_id}/skills", headers=self.caller["headers"]), 404,
                           "device_not_found")

    def test_the_robot_is_offline(self):
        self.clock.advance(61)
        self.error(self.call_raw(self.device_id), 409, "device_offline")
        self.assertEqual(self.listed()["calls"], [])

    def test_the_skill_is_not_installed(self):
        self.error(self.call_raw(self.device_id, skill_id="patrol"), 409, "skill_not_installed")
        self.error(self.call_raw(self.device_id, skill_id="Not%20A%20Skill"), 409, "skill_not_installed")

    def test_the_owner_has_not_switched_remote_calls_on(self):
        self.sync(skills=[device_skill(allowed=False)])
        self.error(self.call_raw(self.device_id), 403, "remote_not_allowed")
        self.sync(skills=[device_skill(allowed=True)])
        call_id = self.call(self.device_id)["id"]
        # switched off again while the call waits: the sync that says so does not get the call
        self.assertEqual(self.sync(skills=[device_skill(allowed=False)])["calls"], [])
        seen = self.seen(call_id)
        self.assertEqual((seen["status"], seen["error"]["code"], seen["dispatched_at"]),
                         ("rejected", "remote_not_allowed", None))
        self.error(self.call_raw(self.device_id, request_id="req-2"), 403, "remote_not_allowed")

    def test_a_waiting_call_is_not_handed_over_when_the_robot_has_changed_meanwhile(self):
        changes = {"estop_latched": dict(state={"estop_latched": True}),
                   "skill_not_installed": dict(skills=[]),
                   "version_mismatch": dict(skills=[device_skill(digest="cd" * 32)]),
                   "remote_not_allowed": dict(skills=[device_skill(allowed=False)])}
        for index, (code, change) in enumerate(changes.items()):
            with self.subTest(code=code):
                self.sync()
                call_id = self.call(self.device_id, request_id=f"req-{index}")["id"]
                self.assertEqual(self.sync(**change)["calls"], [])
                seen = self.seen(call_id)
                self.assertEqual((seen["status"], seen["error"]["code"]), ("rejected", code))
        self.assertEqual(self.sync(skills=[device_skill(version="0.2.0")])["calls"], [])

    def test_the_emergency_stop_is_latched(self):
        self.sync(state={"estop_latched": True})
        self.error(self.call_raw(self.device_id), 409, "estop_latched")

    def test_another_version_than_the_installed_one(self):
        refused = self.error(self.call_raw(self.device_id, version="9.9.9"), 409, "version_mismatch")
        self.assertEqual(refused["installed"], "0.1.0")
        self.call(self.device_id, version="0.1.0")

    def test_parameters_are_checked_against_what_the_skill_declares(self):
        cases = {
            "param_unknown": {"place": "会议室", "speed": 3},
            "param_required": {},
            "param_type": {"place": 7},
            "param_choice": {"place": "仓库"},
            "param_out_of_range": {"place": "会议室", "max_speed": 9.5},
        }
        for problem, parameters in cases.items():
            with self.subTest(problem=problem):
                refused = self.error(self.call_raw(self.device_id, parameters=parameters), 400, "invalid_parameters")
                self.assertIn(problem, [p["problem"] for p in refused["problems"]])
                self.assertTrue(all(set(p) >= {"name", "problem"} for p in refused["problems"]))
        refused = self.error(self.call_raw(self.device_id, parameters={"place": 7, "max_speed": "fast", "x": 1}), 400,
                             "invalid_parameters")
        self.assertEqual(sorted((p["name"], p["problem"]) for p in refused["problems"]),
                         [("max_speed", "param_type"), ("place", "param_type"), ("x", "param_unknown")])
        self.error(self.call_raw(self.device_id, parameters={"place": "会议室", "max_speed": True}), 400,
                   "invalid_parameters")
        self.assertEqual(self.listed()["calls"], [])
        self.call(self.device_id, parameters={"place": "二楼", "max_speed": 1})      # an integer is a float

    def test_more_kinds_of_declared_parameters(self):
        declared = [{"name": "mode", "type": "enum", "choices": ["slow", "fast"], "default": "slow"},
                    {"name": "count", "type": "int", "minimum": 1, "maximum": 3},
                    {"name": "goal", "type": "pose"}, {"name": "path", "type": "vector", "length": 2, "max": 10},
                    {"name": "label", "type": "str", "max_length": 5}, {"name": "flag", "type": "bool"},
                    {"name": "extra", "type": "object"}]
        self.sync(skills=[device_skill("many", parameters=declared, **HEALTH)])
        bad = {"mode": {"mode": "warp"}, "count": {"count": 4}, "goal": {"goal": [0, 0, 0, 0, 0, 1]},
               "path": {"path": [1, 11]}, "label": {"label": "too long"}, "flag": {"flag": 1},
               "count ": {"count": 1.5}, "path ": {"path": [1, 2, 3]}}
        for name, parameters in bad.items():
            with self.subTest(name=name):
                refused = self.error(self.call_raw(self.device_id, skill_id="many", parameters=parameters), 400,
                                     "invalid_parameters")
                self.assertEqual([p["name"] for p in refused["problems"]], [name.strip()])
        good = {"mode": "fast", "count": 3, "goal": [0, 0, 0, 0, 0, 0, 1], "path": [1, 2.5], "label": "ok",
                "flag": False, "extra": {"anything": [1, "two", None]}}
        self.assertEqual(self.call(self.device_id, skill_id="many", parameters=good)["parameters"], good)

    def test_a_timeout_may_only_shorten_the_skill_s_own(self):
        self.error(self.call_raw(self.device_id, timeout_s=1801), 400, "invalid_request")
        for bad in (0, -5, "soon", True, 10 ** 9):
            self.error(self.call_raw(self.device_id, timeout_s=bad), 400, "invalid_request")
        self.call(self.device_id, timeout_s=60)
        self.assertEqual(self.sync()["calls"][0]["timeout_s"], 60)

    def test_the_order_of_the_checks(self):
        """Offline before the skill, the skill before the switch, the switch before the latch, the latch before parameters."""
        self.sync(skills=[device_skill(allowed=False)], state={"estop_latched": True})
        self.error(self.call_raw(self.device_id, skill_id="nope", parameters={"bad": 1}), 409, "skill_not_installed")
        self.error(self.call_raw(self.device_id, parameters={"bad": 1}), 403, "remote_not_allowed")
        self.sync(state={"estop_latched": True})
        self.error(self.call_raw(self.device_id, parameters={"bad": 1}), 409, "estop_latched")
        self.clock.advance(61)
        self.error(self.call_raw(self.device_id, skill_id="nope"), 409, "device_offline")


class Idempotency(OpenCase):
    def test_the_same_request_id_is_the_same_call(self):
        first = self.call(self.device_id)
        again = self.ok(self.call_raw(self.device_id), 200)["call"]
        self.assertEqual(again["id"], first["id"])
        self.sync()
        self.report(first["id"], "succeeded", result="done")
        later = self.ok(self.call_raw(self.device_id), 200)["call"]        # after it ended: still that call, not a new run
        self.assertEqual((later["id"], later["status"], later["result"]), (first["id"], "succeeded", "done"))
        self.assertEqual(len(self.listed()["calls"]), 1)
        self.assertEqual(len(self.audits("skill.call.create")), 1)
        self.clock.advance(61)                                             # even when the robot has gone offline since
        self.assertEqual(self.ok(self.call_raw(self.device_id), 200)["call"]["id"], first["id"])

    def test_a_request_id_used_for_something_else_is_refused(self):
        first = self.call(self.device_id)
        refused = self.error(self.call_raw(self.device_id, parameters={"place": "二楼"}), 409, "request_id_reused")
        self.assertEqual(refused["call_id"], first["id"])
        other = self.online(instance_id="dog-2")
        self.error(self.call_raw(other), 409, "request_id_reused")

    def test_what_is_not_a_request_id(self):
        for request_id in ("", "x" * 101, "with space", "a\nb", 7, None, "-leading"):
            with self.subTest(request_id=str(request_id)[:12]):
                self.error(self.call_raw(self.device_id, request_id=request_id), 400, "invalid_request")


class HostileCallers(OpenCase):
    def test_what_is_not_a_call(self):
        headers = self.caller["headers"]
        path = f"/open/robots/{self.device_id}/skills/goto_place/calls"
        self.error(self.post(path, ["req-1"], headers=headers), 400, "invalid_request")
        self.error(self.post(path, {"parameters": {}}, headers=headers), 400, "invalid_request")
        for parameters in ("place=会议室", ["会议室"], 7):
            self.error(self.call_raw(self.device_id, parameters=parameters), 400, "invalid_parameters")
        self.error(self.call_raw(self.device_id, version=7), 400, "invalid_request")

    def test_huge_deep_and_unstorable_parameters(self):
        deep = "leaf"
        for _ in range(40):
            deep = {"d": deep}
        for name, parameters in {"huge": {"place": "x" * 20_000}, "deep": {"place": deep},
                                 "many": {f"k{i}": i for i in range(3000)}, "nul": {"place": "会\x00议室"},
                                 "nan": {"place": "会议室", "max_speed": float("nan")},
                                 "surrogate": {"place": "\ud800"}, "long name": {"n" * 200: 1},
                                 "a number no float can hold": {"place": "会议室", "max_speed": 10 ** 400}}.items():
            with self.subTest(name):
                path = f"/open/robots/{self.device_id}/skills/goto_place/calls"
                response = self.post_text(path, {"request_id": "req-1", "parameters": parameters}, self.caller["headers"])
                self.error(response, 400, "invalid_parameters")
        self.assertEqual(self.listed()["calls"], [])

    def test_a_body_over_the_general_limit_is_refused(self):
        path = f"{API}/open/robots/{self.device_id}/skills/goto_place/calls"
        response = self.client.post(path, content=b'{"request_id": "r", "parameters": {"place": "' + b"x" * 300_000 + b'"}}',
                                    headers={**self.caller["headers"], "Content-Type": "application/json"})
        self.error(response, 413, "payload_too_large")


class WhoMayCall(OpenCase):
    def test_only_an_api_key_with_the_invoke_scope(self):
        paths = ["/open/robots", f"/open/robots/{self.device_id}/skills", "/open/calls",
                 "/open/calls/cal_" + "0" * 20, "/open/stops/stp_" + "0" * 20]
        other_key = self.api_key(self.owner, ["robots", "skills:read", "skills:write", "gateway"])
        for path in paths:
            with self.subTest(path=path):
                self.error(self.get(path, self.owner), 403, "api_key_required")       # the owner's own login session
                self.error(self.get(path, headers=other_key["headers"]), 403, "scope_missing")
                self.error(self.get(path, headers=self.robot_key["headers"]), 403, "scope_missing")
                self.error(self.client.get(API + path), 401)
        for key, code in ((self.owner, "api_key_required"), (other_key, "scope_missing"), (self.robot_key, "scope_missing")):
            self.error(self.call_raw(self.device_id, key=key), 403, code)
            self.error(self.stop_raw(key=key), 403, code)
            self.error(self.post("/open/calls/cal_" + "0" * 20 + "/cancel", {}, headers=key["headers"]), 403, code)
        self.assertEqual(self.listed()["calls"], [])
        self.assertEqual(self.audits("skill.stop.request"), [])

    def test_a_key_of_an_unverified_account_does_not_place_calls(self):
        fresh = self.register("fresh@example.com", verify=False)
        key = self.api_key(fresh, [INVOKE_SCOPE])
        self.error(self.call_raw(self.device_id, key=key), 403, "email_unverified")

    def test_a_key_whose_creator_was_demoted_to_field_operator_loses_the_scope(self):
        member = self.register("dev@example.com")
        self.ok(self.post("/org/members", {"email": "dev@example.com", "role": "developer"}, self.owner))
        member["headers"] = {**member["headers"], "X-NeuxMind-Org": self.owner["org_id"]}
        key = self.api_key(member, [INVOKE_SCOPE])
        self.call(self.device_id, key=key)
        self.ok(self.post("/org/members", {"email": "dev@example.com", "role": "operator"}, self.owner))
        self.error(self.call_raw(self.device_id, request_id="req-2", key=key), 403, "scope_missing")
        self.error(self.stop_raw(key=key), 403, "scope_missing")


class RateLimit(OpenCase):
    settings_overrides = {"skill_call_rate_per_minute": 3}

    def test_calls_per_key_per_minute_refused_ones_included(self):
        self.error(self.call_raw(self.device_id, skill_id="nope"), 409, "skill_not_installed")
        self.error(self.call_raw(self.device_id, parameters={}), 400, "invalid_parameters")
        self.call(self.device_id)
        refused = self.error(self.call_raw(self.device_id, request_id="req-2"), 429, "rate_limited")
        self.assertEqual(refused["retry_after"], 60)
        self.error(self.call_raw(self.device_id), 429, "rate_limited")     # a repeat counts as well
        # reading, cancelling and stopping are not held back, and another key has its own count
        self.ok(self.get("/open/calls", headers=self.caller["headers"]))
        self.stop()
        other = self.api_key(self.owner, [INVOKE_SCOPE])
        self.error(self.call_raw(self.device_id, request_id="req-3", key=other), 409)


class StorageBound(OpenCase):
    settings_overrides = {"skill_call_org_keep": 3}

    def test_the_oldest_finished_calls_make_room(self):
        ids = []
        for index in range(5):
            self.clock.advance(1)
            call_id = self.call(self.device_id, request_id=f"req-{index}")["id"]
            ids.append(call_id)
            self.sync()
            self.report(call_id, "succeeded")
        self.assertEqual([c["id"] for c in self.listed()["calls"]], ids[:1:-1])


class Reading(OpenCase):
    def test_calls_are_listed_newest_first_filtered_and_paged(self):
        other = self.online(instance_id="dog-2")
        ids = []
        for index in range(5):
            self.clock.advance(1)
            device_id = other if index == 4 else self.device_id
            call_id = self.call(device_id, request_id=f"req-{index}")["id"]
            ids.append(call_id)
            if index < 3:
                self.sync()
                self.report(call_id, "succeeded" if index else "failed")
            elif index == 3:
                self.cancel(call_id)
        self.assertEqual([c["id"] for c in self.listed()["calls"]], ids[::-1])
        self.assertEqual([c["id"] for c in self.listed(f"?device_id={other}")["calls"]], [ids[4]])
        self.assertEqual([c["id"] for c in self.listed("?status=succeeded")["calls"]], [ids[2], ids[1]])
        self.assertEqual([c["id"] for c in self.listed(f"?status=queued&device_id={self.device_id}")["calls"]], [])
        first = self.listed("?limit=2")
        self.assertEqual(([c["id"] for c in first["calls"]], first["more"]), (ids[:2:-1], True))
        second = self.listed(f"?limit=2&before={first['next_before']}")
        self.assertEqual([c["id"] for c in second["calls"]], [ids[2], ids[1]])
        last = self.listed(f"?limit=2&before={second['next_before']}")
        self.assertEqual(([c["id"] for c in last["calls"]], last["more"], last["next_before"]), ([ids[0]], False, None))

    def test_what_is_not_a_filter(self):
        for query in ("?status=exploded", "?limit=0", "?limit=201", "?limit=many", "?before=yesterday",
                      "?device_id=nonsense"):
            with self.subTest(query=query):
                self.error(self.get("/open/calls" + query, headers=self.caller["headers"]), 400, "invalid_request")
        self.error(self.get("/open/calls/nonsense", headers=self.caller["headers"]), 404, "call_not_found")
        self.error(self.post("/open/calls/nonsense/cancel", {}, headers=self.caller["headers"]), 404, "call_not_found")


class Outage(OpenCase):
    """A long skill runs, the link is down for more than two minutes (the call is ``lost``), and the
    third party wants the robot stopped. Whatever it asks for must reach the robot when it is back."""

    def setUp(self):
        super().setUp()
        self.call_id = self.started()
        self.clock.advance(121)
        self.assertEqual(self.seen(self.call_id)["status"], "lost")

    def test_a_cancel_of_a_lost_call_is_delivered_when_the_robot_is_back(self):
        seen = self.cancel(self.call_id)
        self.assertEqual((seen["status"], seen["cancel_requested"]), ("lost", True))
        self.assertEqual(len(self.audits("skill.call.cancel")), 1)
        self.assertEqual(self.cancel(self.call_id), seen)
        self.assertEqual(len(self.audits("skill.call.cancel")), 1)
        self.clock.advance(600)
        answer = self.report(self.call_id, "running")
        self.assertEqual((answer["cancel"], self.seen(self.call_id)["status"]), ([self.call_id], "running"))
        self.report(self.call_id, "cancelled")
        self.assertEqual(self.seen(self.call_id)["status"], "cancelled")

    def test_a_stop_for_a_robot_with_a_lost_call_waits_for_it_however_long(self):
        asked = self.stop(body={"reason": "失联前它在走"})
        self.assertTrue(self.seen(self.call_id)["cancel_requested"])
        self.assertEqual(self.audits("skill.stop.request")[0][2]["calls"], [self.call_id])
        self.clock.advance(3600)                                           # far beyond the life of an unfetched stop
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "pending")
        answer = self.report(self.call_id, "running")
        self.assertEqual((answer["stop"], answer["cancel"]),
                         ({"stop_id": asked["stop_id"], "reason": "失联前它在走"}, [self.call_id]))
        self.report(self.call_id, "stopped", error={"code": "stopped", "message": "远程停止"})
        self.sync(stops=[asked["stop_id"]], state={"estop_latched": True})
        self.assertEqual((self.seen(self.call_id)["status"], self.stop_seen(asked["stop_id"])["status"]),
                         ("stopped", "acknowledged"))

    def test_a_stop_asked_for_before_the_call_was_lost_does_not_expire_either(self):
        other = self.online(instance_id="dog-2")
        call_id = self.call(other, request_id="req-2")["id"]
        self.sync(instance_id="dog-2")
        self.sync(instance_id="dog-2", calls=[{"call_id": call_id, "status": "running"}])
        asked = self.stop(device_id=other)                                 # the robot never fetches it: it has gone
        self.clock.advance(500)
        self.assertEqual(self.seen(call_id)["status"], "lost")
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "pending")
        answer = self.sync(instance_id="dog-2", calls=[{"call_id": call_id, "status": "running"}])
        self.assertEqual((answer["stop"]["stop_id"], answer["cancel"]), (asked["stop_id"], [call_id]))

    def test_once_the_robot_has_told_how_the_lost_call_ended_an_unfetched_stop_expires_as_usual(self):
        self.report(self.call_id, "succeeded")
        self.sync()
        asked = self.stop()
        self.clock.advance(121)
        self.assertEqual(self.stop_seen(asked["stop_id"])["status"], "expired")

    def test_cancelling_a_call_that_has_meanwhile_expired_leaves_it_expired(self):
        self.report(self.call_id, "succeeded")
        waiting = self.call(self.device_id, request_id="req-2")["id"]
        self.clock.advance(31)
        seen = self.cancel(waiting)
        self.assertEqual((seen["status"], seen["cancel_requested"]), ("expired", False))
