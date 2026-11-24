"""Issue #43, defects an independent review reproduced in the runtime's link, each pinned here:

1. a remote run could start after the link was closed (a sync in flight during shutdown);
2. a switch turned off whose write failed was on again after a restart;
3. the retry back-off ignored a running remote call, so a stop or cancel could be 30 s late;
4. turning a switch on bound it to whatever was newest at that moment, not to what the person saw;
5. the robot had no age check of its own for a delivered call.
"""
from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from link_support import LinkCase, linger

from _support import wait_until
from neuxmind_runtime import remote_access
from neuxmind_runtime.alert_report import SendError
from neuxmind_runtime.errors import RuntimeApiError


def disk_full(store):
    """``store.put`` that refuses the switches (and only them), as a full disk would."""
    real = store.put

    def put(kind, obj):
        if kind == remote_access.STORE_KIND and obj.get("id") == remote_access.SWITCHES_ID:
            raise OSError(28, "No space left on device")
        return real(kind, obj)
    return mock.patch.object(store, "put", put)


class ClosedLink(LinkCase):
    def in_flight(self, answer=None):
        """Start a sync whose answer is held back; returns ``(thread, release)``."""
        gate, entered = threading.Event(), threading.Event()
        honest = self.cloud.send

        def slow(request):
            entered.set()
            gate.wait(10)
            reply = honest(request)
            return answer(reply) if answer is not None else reply
        self.cloud.send = slow
        self.clock.advance(31)
        self.wall.advance(31)
        thread = threading.Thread(target=self.link.tick)
        thread.start()
        self.assertTrue(entered.wait(5))
        return thread, gate.set

    def test_an_answer_that_arrives_after_close_starts_nothing(self):
        self.install("linger")
        self.allow("linger")
        self.sync()
        call_id = self.cloud.call("linger")
        thread, release = self.in_flight()
        self.link.close()                                        # shutdown: "no new remote call"
        self.runtime.runner.stop_all("runtime shutdown")
        release()
        thread.join(10)
        self.assertEqual(self.runtime.runner.list(), [], "no run during teardown")
        self.assertEqual(self.access.calls.recent(5), [], "and the call was not taken: the cloud keeps it")
        self.assertNotIn(call_id, self.runtime.store.get("remote", "calls") or {"seen": {}})

    def test_a_stop_in_that_answer_is_still_carried_out(self):
        self.sync()
        thread, release = self.in_flight(lambda reply: {**reply, "stop": {"stop_id": "stp_late01", "reason": "叫停"}})
        self.link.close()
        release()
        thread.join(10)
        self.assertTrue(self.runtime.supervisor.latched)

    def test_the_book_refuses_a_call_for_a_closed_link_and_cancels_one_that_slipped_through(self):
        self.install("linger")
        self.allow("linger")
        book = self.access.calls
        call = {"call_id": "cal_closed01", "skill_id": "linger", "version": None, "digest": None, "parameters": {},
                "timeout_s": None, "problem": None}
        book.deliver(call, lambda: False)
        self.assertEqual((book.recent(5), self.runtime.runner.list()), ([], []))
        answers = iter([True, False])                            # open at the door, closed once the run started
        book.deliver({**call, "call_id": "cal_closed02"}, lambda: next(answers, False))
        (run,) = self.runtime.runner.list()
        wait_until(lambda: self.runtime.runner.get(run["id"])["status"] == "stopped")

    def test_tick_after_close_does_nothing(self):
        self.link.close()
        self.clock.advance(31)
        self.link.tick()
        self.assertEqual(self.cloud.requests, [])


class SwitchOffThatCannotBeSaved(LinkCase):
    def setUp(self):
        super().setUp()
        self.install("linger", "status_report")
        self.allow("linger")
        self.allow("status_report")

    def off_fails(self) -> None:
        with self.assertRaises(RuntimeApiError) as caught:
            self.allow("linger", False)
        self.assertEqual((caught.exception.code, caught.exception.status), ("remote_not_saved", 500))
        self.assertIs(self.access.view()["skills"]["linger"]["allowed"], False, "off in memory all the same")

    def allowed(self) -> dict:
        return {skill: entry["allowed"] for skill, entry in self.access.view()["skills"].items()}

    def test_it_is_still_off_after_a_restart_and_the_others_are_untouched(self):
        with disk_full(self.runtime.store):
            self.off_fails()
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": True})
        call_id = self.cloud.call("linger")
        self.sync()
        self.assertEqual(self.sync_until(call_id, "rejected")["error"]["code"], "remote_not_allowed")
        self.assertEqual(self.runtime.runner.list(), [])

    def test_when_nothing_at_all_can_be_written_every_switch_is_off_after_a_restart(self):
        with disk_full(self.runtime.store), \
                mock.patch.object(remote_access, "write_tombstone", side_effect=OSError(28, "No space left on device")):
            self.off_fails()
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": False})
        self.assertIn("没有写进", self.access.view()["invalid"])
        self.allow("status_report")                              # a person turns it on again: back to normal
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": True})
        self.assertIsNone(self.access.view()["invalid"])

    def test_the_write_is_tried_again_until_it_works(self):
        with disk_full(self.runtime.store):
            self.off_fails()
            self.sync()                                          # still full: tried, still not saved
            self.assertTrue(self.access.unsaved)
        self.sync()                                              # the link's round tries again
        self.assertFalse(self.access.unsaved)
        stored = self.runtime.store.get(remote_access.STORE_KIND, remote_access.SWITCHES_ID)["skills"]
        self.assertEqual((stored["linger"]["allowed"], stored["status_report"]["allowed"]), (False, True))
        self.assertFalse(os.path.exists(Path(self.fx.state_dir) / remote_access.TOMBSTONE_NAME))
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": True})
        self.assertIsNone(self.access.view()["invalid"])

    def test_it_cannot_be_turned_on_again_before_the_off_is_on_disk(self):
        with disk_full(self.runtime.store):
            self.off_fails()
            with self.assertRaises(RuntimeApiError) as caught:
                self.allow("linger")
            self.assertEqual(caught.exception.code, "remote_not_saved")
            self.assertIs(self.access.view()["skills"]["linger"]["allowed"], False)
            self.assertIs(self.access.view()["unsaved"], True)
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": True})

    def test_housekeeping_retries_as_well_without_a_link(self):
        with disk_full(self.runtime.store):
            self.off_fails()
        self.runtime.housekeeping_tick()
        self.assertFalse(self.access.unsaved)

    def test_a_failing_cancel_does_not_skip_the_save(self):
        with mock.patch.object(self.access.calls, "switched_off", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.allow("linger", False)
        self.assertIs(self.access.view()["skills"]["linger"]["allowed"], False)
        stored = self.runtime.store.get(remote_access.STORE_KIND, remote_access.SWITCHES_ID)["skills"]
        self.assertIs(stored["linger"]["allowed"], False)
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": True})

    def test_switches_whose_marker_is_gone_do_not_count(self):
        os.unlink(Path(self.fx.state_dir) / remote_access.ARMED_NAME)
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": False})

    def test_a_tombstone_that_cannot_be_read_turns_everything_off(self):
        (Path(self.fx.state_dir) / remote_access.TOMBSTONE_NAME).write_text("{not json", encoding="utf-8")
        self.restart()
        self.assertEqual(self.allowed(), {"linger": False, "status_report": False})


class BackOffWhileACallRuns(LinkCase):
    def test_no_wait_is_longer_than_two_seconds_while_a_remote_call_runs(self):
        self.install("linger")
        self.allow("linger")
        call_id = self.cloud.call("linger")
        self.sync()
        self.sync_until(call_id, "running")
        for failure in (SendError("连不上云平台"), SendError("云平台答复 403（scope_missing）", status=403),
                        SendError("云平台答复 401（invalid_api_key）", status=401)):
            self.cloud.fail = failure
            waits = []
            for _ in range(7):
                self.sync()
                waits.append(self.link._next_at - self.clock())
            self.assertLessEqual(max(waits), 2.0, failure)
        self.runtime.runner.cancel(self.cloud.states[call_id]["run_id"])
        wait_until(lambda: self.runtime.runner.active_runs() == [])
        self.cloud.fail = SendError("连不上云平台")
        for _ in range(6):
            self.sync()
        self.assertEqual(self.link._next_at - self.clock(), 30.0, "without a running call the back-off is as before")


class TheContentThePersonSaw(LinkCase):
    def on(self, **fields) -> dict:
        return self.access.set({"skill_id": "linger", "allowed": True, "set_by": "王工", "hardware_authorized": True,
                                **fields})

    def refused(self, **fields) -> None:
        with self.assertRaises(RuntimeApiError) as caught:
            self.on(**fields)
        self.assertEqual((caught.exception.code, caught.exception.status), ("skill_changed", 409))
        self.assertIs(self.access.view()["skills"]["linger"]["allowed"], False)

    def test_turning_on_needs_the_digest_that_was_shown(self):
        self.install("linger")
        shown = self.access.view()["skills"]["linger"]["digest"]
        self.refused()
        for wrong in (None, "", 5, "0" * 64, shown.upper(), [shown]):
            with self.subTest(wrong=wrong):
                self.refused(digest=wrong)
        self.assertIs(self.on(digest=shown)["skills"]["linger"]["allowed"], True)

    def test_a_version_installed_between_looking_and_clicking_is_not_allowed_by_that_click(self):
        self.install("linger")
        shown = self.access.view()["skills"]["linger"]["digest"]
        self.fx.install(linger("0.2.0", "def run(ctx, seconds=1.0):\n    return {'other': True}\n"))
        self.refused(digest=shown, request_id="on-1")
        newer = self.access.view()["skills"]["linger"]["digest"]
        self.assertNotEqual(newer, shown)
        self.assertIs(self.on(digest=newer, request_id="on-2")["skills"]["linger"]["allowed"], True)

    def test_turning_off_needs_no_digest(self):
        self.install("linger")
        self.allow("linger")
        self.assertIs(self.access.set({"skill_id": "linger", "allowed": False})["skills"]["linger"]["allowed"], False)


class AgeOfACall(LinkCase):
    def deliver(self, server_time, **fields) -> str:
        self.install("status_report")
        self.allow("status_report")
        call_id = self.cloud.call("status_report", **fields)
        self.cloud.reply = lambda _request, answer: {**answer, "server_time": server_time}
        self.sync()
        return call_id

    def test_a_call_older_than_the_limit_by_the_clouds_own_clock_is_refused(self):
        for field in ("created_at", "dispatched_at"):
            with self.subTest(field=field):
                call_id = self.deliver(5_000.0, **{field: 5_000.0 - 151})
                report = self.sync_until(call_id, "rejected")
                self.assertEqual(report["error"]["code"], "expired")
                self.assertEqual(self.runtime.runner.list(), [])

    def test_a_younger_call_runs_and_the_robots_own_clock_plays_no_part(self):
        call_id = self.deliver(5_000.0, created_at=5_000.0 - 149)     # the robot's clock says 2027
        self.sync_until(call_id, "succeeded")

    def test_without_usable_times_nothing_is_refused_for_age(self):
        for index, (server_time, fields) in enumerate(((None, {"created_at": 1.0}), (5_000.0, {}),
                                                       ("now", {"created_at": 1.0}), (5_000.0, {"created_at": "old"}),
                                                       (float("nan"), {"created_at": 1.0}), (True, {"created_at": False}))):
            with self.subTest(index=index):
                call_id = self.deliver(server_time, **fields)
                self.sync_until(call_id, "succeeded")


class WhatTheCloudNowSends(LinkCase):
    def test_a_cancel_for_a_call_that_never_arrived_is_harmless(self):
        self.install("linger")
        self.allow("linger")
        self.cloud.cancel.append("cal_neverseen0001")            # the cloud gave up sending it and says so
        self.sync()
        request = self.sync()
        self.assertEqual([(r["call_id"], r["status"], r["run_id"]) for r in request["calls"]],
                         [("cal_neverseen0001", "cancelled", None)])
        self.assertEqual(self.sync()["calls"], [], "settled")
        self.cloud.cancel.clear()
        self.cloud.queue.append({"call_id": "cal_neverseen0001", "skill_id": "linger", "parameters": {}})
        self.cloud.states.clear()                                # ... and should it turn up later, it does not run
        self.sync()
        self.sync()
        self.assertEqual((self.cloud.status("cal_neverseen0001"), self.runtime.runner.list()), ("cancelled", []))
        self.assertEqual(self.link.describe()["state"], "online")

    def test_the_total_of_refused_skills_is_shown_next_to_the_capped_list(self):
        refused = [{"index": i, "id": f"s{i}", "error": "太大"} for i in range(20)]
        for total, shown in ((57, 57), (3, 20), ("many", 20), (None, 20), (True, 20), (-1, 20)):
            with self.subTest(total=total):
                self.cloud.reply = lambda _r, answer, total=total: {**answer, "rejected_skills": refused,
                                                                    "rejected_skills_total": total}
                self.sync()
                view = self.link.describe()
                self.assertEqual((view["state"], len(view["skills_rejected"]), view["skills_rejected_total"]),
                                 ("online", 20, shown))

    def test_the_first_answer_after_an_outage_stops_and_cancels_before_it_starts_anything(self):
        self.install("linger", "status_report")
        self.allow("linger")
        self.allow("status_report")
        running = self.cloud.call("linger")
        self.sync()
        run_id = self.sync_until(running, "running")["run_id"]
        self.cloud.fail = SendError("连不上云平台")              # the outage: the call goes on by itself
        for _ in range(4):
            self.sync()
        self.assertEqual(self.runtime.runner.get(run_id)["status"], "running")
        self.cloud.fail = None                                   # meanwhile: a stop, a cancel and a new call
        self.cloud.stop = {"stop_id": "stp_outage01", "reason": "失联期间叫停"}
        self.cloud.cancel.append(running)
        fresh = self.cloud.call("status_report")
        request = self.sync()
        self.assertEqual(request["calls"][0]["status"], "running", "what the cloud may have called lost is reported")
        self.assertTrue(self.runtime.supervisor.latched)
        wait_until(lambda: self.runtime.runner.get(run_id)["status"] == "stopped")
        self.assertEqual(self.sync_until(fresh, "rejected")["error"]["code"], "estop_latched")
        self.assertIn(self.cloud.status(running), ("stopped", "cancelled"))
        self.assertEqual(len(self.runtime.runner.list()), 1, "the new call never ran")


if __name__ == "__main__":
    unittest.main()
