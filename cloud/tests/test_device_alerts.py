"""Issue #26 (R7-08): the cloud receives the alerts devices report and shows them to the organisation.

A device holds an API key that can do one thing, report (scope ``alerts:report``); people read the
alerts with the ``robots`` permission they already have. Covered: receiving (idempotent by the device's
own alert id), who may do what, listing with level / time / device / reason / advice / handled, the
"device offline" rule (three five-minute reports missed), and the limits on what one key can store.
"""
import json
import time

import support  # noqa: F401
from support import API, CloudTestCase, LiveServer, quadruped_manifest

from sqlalchemy import select, update

from neuxmind_cloud import alerts, maintenance
from neuxmind_cloud.alert_models import DeviceAlert, DeviceReport
from neuxmind_cloud.models import AuditLog

REPORT_SCOPE = "alerts:report"
INTERVAL_S = 300
GRACE_S = 60
OFFLINE_AFTER_S = 3 * INTERVAL_S + GRACE_S          # three reports missed, and the third given a minute to be late


def alert(alert_id: str = "a-1", **over) -> dict:
    item = {"schema": "neuxmind.alert.v1", "id": alert_id, "kind": "estop_latched", "level": "critical",
            "ts": time.time(), "device": {"instance_id": "ignored"}, "reason": "急停已锁定：操作员停止",
            "advice": "到现场确认安全后解除急停", "detail": {"stop_count": 1}}
    item.update(over)
    return item


class AlertsCase(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("fleet@example.com")
        self.device = self.api_key(self.user, [REPORT_SCOPE])

    def report(self, *items, instance_id: str = "dog-1", headers=None, **over):
        body = {"schema": "neuxmind.device_report.v1",
                "device": {"instance_id": instance_id, "name": "Dog", "family": "quadruped", "simulated": True,
                           "runtime_version": "0.4.0"},
                "sent_at": time.time(), "interval_s": INTERVAL_S, "trigger": "interval", "alerts": list(items),
                "pending": 0}
        body.update(over)
        return self.post("/device-reports", body, headers=headers or self.device["headers"])

    def listed(self, query: str = "", user=None) -> list[dict]:
        return self.ok(self.get("/alerts" + query, user or self.user))["alerts"]

    def devices(self, user=None) -> list[dict]:
        return self.ok(self.get("/alerts/devices", user or self.user))["devices"]

    def age_last_report(self, seconds: float, instance_id: str = "dog-1") -> None:
        """Pretend the device's last report arrived ``seconds`` ago."""
        with self.db.transaction() as s:
            s.execute(update(DeviceReport).where(DeviceReport.instance_id == instance_id)
                      .values(last_report_at=time.time() - seconds))


class Receiving(AlertsCase):
    def test_a_device_reports_and_the_organisation_sees_level_time_device_reason_advice(self):
        raised = time.time() - 4
        answer = self.ok(self.report(alert("a-1", ts=raised)))
        self.assertEqual((answer["accepted"], answer["duplicates"], answer["rejected"]), (["a-1"], [], []))
        (seen,) = self.listed()
        self.assertEqual((seen["level"], seen["kind"], seen["instance_id"], seen["device_name"]),
                         ("critical", "estop_latched", "dog-1", "Dog"))
        self.assertEqual((seen["reason"], seen["advice"]), ("急停已锁定：操作员停止", "到现场确认安全后解除急停"))
        self.assertAlmostEqual(seen["ts"], raised, places=3)
        self.assertGreaterEqual(seen["received_at"], raised)
        self.assertEqual((seen["handled"], seen["source"], seen["alert_id"], seen["detail"]),
                         (False, "device", "a-1", {"stop_count": 1}))

    def test_the_same_alert_sent_again_is_stored_once(self):
        self.ok(self.report(alert("a-1")))
        again = self.ok(self.report(alert("a-1", reason="改写过的原因"), alert("a-2")))
        self.assertEqual((again["accepted"], again["duplicates"]), (["a-2"], ["a-1"]))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["a-1", "a-2"])
        self.assertEqual([a["reason"] for a in self.listed() if a["alert_id"] == "a-1"], ["急停已锁定：操作员停止"])
        # the id is the device's own: another device, or another organisation, may use the same one
        self.assertEqual(self.ok(self.report(alert("a-1"), instance_id="dog-2"))["accepted"], ["a-1"])
        other = self.register("other@example.com")
        other_key = self.api_key(other, [REPORT_SCOPE])
        self.assertEqual(self.ok(self.report(alert("a-1"), headers=other_key["headers"]))["accepted"], ["a-1"])
        self.assertEqual(len(self.listed()), 3)

    def test_a_report_with_no_alerts_is_fine(self):
        answer = self.ok(self.report())
        self.assertEqual((answer["accepted"], answer["duplicates"], answer["rejected"]), ([], [], []))
        self.assertEqual(self.listed(), [])
        (device,) = self.devices()
        self.assertEqual((device["instance_id"], device["state"]), ("dog-1", "online"))

    def test_alerts_that_make_no_sense_are_refused_one_by_one(self):
        bad = [alert("b-level", level="urgent"), alert("b-ts", ts="yesterday"), alert("b-inf", ts=float("inf")),
               alert("b-reason", reason="   "), alert("b-kind", kind="Bad Kind!"), alert("bad id with spaces"),
               alert("b-detail", detail={"blob": "x" * 10_000}), alert("b-long", reason="因" * 3000), "not an object",
               alert("b-detail-type", detail=["a"]), alert("b-advice", advice=7)]
        body = {"schema": "neuxmind.device_report.v1", "device": {"instance_id": "dog-1"},
                "alerts": [alert("ok-1"), *bad, alert("ok-2")]}
        # sent as text: a JSON library on the device might write Infinity, the test client refuses to
        answer = self.ok(self.client.post(f"{API}/device-reports", content=json.dumps(body, allow_nan=True),
                                          headers={**self.device["headers"], "Content-Type": "application/json"}))
        self.assertEqual(answer["accepted"], ["ok-1", "ok-2"])
        self.assertEqual(len(answer["rejected"]), len(bad))
        self.assertTrue(all(r["error"] for r in answer["rejected"]))
        self.assertEqual({r["id"] for r in answer["rejected"] if r["id"]} >= {"b-level", "b-ts", "b-inf"}, True)
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["ok-1", "ok-2"])

    def test_a_report_that_is_not_a_report_is_refused(self):
        for over in ({"schema": "something.else"}, {"device": None}, {"device": {"instance_id": ""}},
                     {"device": {"instance_id": "x" * 300}}, {"device": {"instance_id": "a\nb"}}, {"alerts": "none"},
                     {"alerts": [alert(f"a-{i}") for i in range(201)]}):
            with self.subTest(over=str(over)[:60]):
                self.error(self.report(**over), 400, "invalid_report")
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.devices(), [])

    def test_an_alert_is_linked_to_the_robot_registered_under_that_instance(self):
        robot = self.ok(self.post("/robots", {"manifest": quadruped_manifest("dog-1")}, self.user), 201)
        self.ok(self.report(alert("a-1")))
        self.ok(self.report(alert("a-2"), instance_id="never-registered"))
        by_id = {a["alert_id"]: a for a in self.listed()}
        self.assertEqual(by_id["a-1"]["robot_id"], robot["id"])
        self.assertIsNone(by_id["a-2"]["robot_id"])
        states = {d["instance_id"]: d["robot_id"] for d in self.devices()}
        self.assertEqual(states, {"dog-1": robot["id"], "never-registered": None})


class WhoMayDoWhat(AlertsCase):
    def test_the_device_key_reports_and_can_do_nothing_else(self):
        self.ok(self.report(alert("a-1")))
        robot = self.ok(self.post("/robots", {"manifest": quadruped_manifest("dog-1")}, self.user), 201)
        (seen,) = self.listed()
        device = self.device["headers"]
        self.assertEqual(set(self.ok(self.get("/me", headers=device))["permissions"]), {REPORT_SCOPE})
        for response in (self.get("/alerts", headers=device), self.get("/alerts/devices", headers=device),
                         self.post(f"/alerts/{seen['id']}/handled", {}, headers=device),
                         self.client.delete(f"{API}/alerts/devices", params={"instance_id": "dog-1"}, headers=device),
                         self.get("/robots", headers=device), self.get(f"/robots/{robot['id']}", headers=device),
                         self.post("/robots", {"manifest": quadruped_manifest("x-1")}, headers=device),
                         self.client.delete(f"{API}/robots/{robot['id']}", headers=device),
                         self.get("/wallet", headers=device), self.get("/api-keys", headers=device)):
            self.error(response, 403)
        self.assertEqual(len(self.ok(self.get("/robots", self.user))["robots"]), 1)
        self.assertIs(self.listed()[0]["handled"], False)

    def test_reporting_needs_the_report_scope(self):
        self.error(self.client.post(f"{API}/device-reports", json={}), 401)
        self.error(self.report(alert(), headers={"Authorization": "Bearer nx_live_" + "0" * 40}), 401, "invalid_api_key")
        robots_only = self.api_key(self.user, ["robots"])
        self.error(self.report(alert(), headers=robots_only["headers"]), 403, "scope_missing")
        self.ok(self.client.delete(f"{API}/api-keys/{self.device['id']}", headers=self.user["headers"]))
        self.error(self.report(alert()), 401)                 # the key was revoked
        self.assertEqual(self.listed(), [])

    def test_people_read_with_the_robots_permission_they_already_have(self):
        self.ok(self.report(alert("a-1")))
        reader = self.api_key(self.user, ["robots"])
        self.assertEqual(len(self.ok(self.get("/alerts", headers=reader["headers"]))["alerts"]), 1)
        no_robots = self.api_key(self.user, ["gateway"])
        self.error(self.get("/alerts", headers=no_robots["headers"]), 403, "scope_missing")
        self.error(self.client.get(f"{API}/alerts"), 401)

    def test_another_organisation_sees_nothing_and_cannot_mark(self):
        self.ok(self.report(alert("a-1")))
        (mine,) = self.listed()
        stranger = self.register("stranger@example.com")
        self.assertEqual(self.listed(user=stranger), [])
        self.assertEqual(self.devices(user=stranger), [])
        self.error(self.post(f"/alerts/{mine['id']}/handled", {}, stranger), 404)
        self.assertIs(self.listed()[0]["handled"], False)

    def test_who_reported_and_who_marked_is_on_record(self):
        self.ok(self.report(alert("a-1")))
        (seen,) = self.listed()
        self.ok(self.post(f"/alerts/{seen['id']}/handled", {}, self.user))
        with self.db.transaction() as s:
            actions = {row.action: row for row in s.execute(select(AuditLog)).scalars()}
        self.assertEqual(actions["device.alerts"].actor, f"key:{self.device['id']}")
        self.assertEqual(actions["device.alerts"].target, "dog-1")
        self.assertEqual(actions["alert.handled"].target, seen["id"])


class Listing(AlertsCase):
    def test_marking_handled_and_taking_it_back(self):
        self.ok(self.report(alert("a-1"), alert("a-2", kind="stop_not_obeyed")))
        first = next(a for a in self.listed() if a["alert_id"] == "a-1")
        marked = self.ok(self.post(f"/alerts/{first['id']}/handled", {}, self.user))
        self.assertIs(marked["handled"], True)
        self.assertEqual(marked["handled_by"], f"user:{self.user['user']['id']}")
        self.assertIsInstance(marked["handled_at"], float)
        self.assertEqual([a["alert_id"] for a in self.listed("?handled=false")], ["a-2"])
        self.assertEqual([a["alert_id"] for a in self.listed("?handled=true")], ["a-1"])
        back = self.ok(self.post(f"/alerts/{first['id']}/handled", {"handled": False}, self.user))
        self.assertEqual((back["handled"], back["handled_at"], back["handled_by"]), (False, None, None))
        self.error(self.post(f"/alerts/{first['id']}/handled", {"handled": "yes"}, self.user), 400)
        self.error(self.post("/alerts/alr_missing/handled", {}, self.user), 404)

    def test_newest_first_in_pages(self):
        for index in range(5):
            self.ok(self.report(alert(f"a-{index}")))
        page = self.ok(self.get("/alerts?limit=2", self.user))
        self.assertEqual([a["alert_id"] for a in page["alerts"]], ["a-4", "a-3"])
        self.assertIs(page["more"], True)
        rest = self.ok(self.get(f"/alerts?limit=10&before={page['next_before']}", self.user))
        self.assertEqual([a["alert_id"] for a in rest["alerts"]], ["a-2", "a-1", "a-0"])
        self.assertEqual((rest["more"], rest["next_before"]), (False, None))
        for bad in ("limit=0", "limit=1000", "limit=x", "before=x", "level=urgent", "handled=maybe"):
            self.error(self.get("/alerts?" + bad, self.user), 400)

    def test_by_device_and_by_level(self):
        self.ok(self.report(alert("a-1"), alert("a-2", level="warning", kind="disk_low")))
        self.ok(self.report(alert("b-1", level="info", kind="note"), instance_id="dog-2"))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed("?instance_id=dog-1")), ["a-1", "a-2"])
        self.assertEqual([a["alert_id"] for a in self.listed("?level=critical")], ["a-1"])
        self.assertEqual([a["alert_id"] for a in self.listed("?instance_id=dog-2&level=info")], ["b-1"])
        self.assertEqual(self.listed("?instance_id=nobody"), [])


class Offline(AlertsCase):
    def test_three_missed_reports_make_the_device_offline_and_raise_one_alert(self):
        self.ok(self.report())
        self.age_last_report(2 * INTERVAL_S + 240)            # two reports missed, the third not yet due
        (device,) = self.devices()
        self.assertEqual((device["state"], device["offline_after_s"]), ("online", OFFLINE_AFTER_S))
        self.age_last_report(3 * INTERVAL_S + 5)              # the third is due this second: it may just be late
        self.assertEqual(self.devices()[0]["state"], "online")
        self.assertEqual(self.listed(), [])
        self.age_last_report(OFFLINE_AFTER_S + 5)
        (device,) = self.devices()
        self.assertEqual(device["state"], "offline")
        (offline,) = self.listed()
        self.assertEqual((offline["kind"], offline["source"], offline["instance_id"], offline["handled"]),
                         ("device_offline", "cloud", "dog-1", False))
        self.assertEqual(offline["level"], "warning")
        self.assertIn("3 次", offline["reason"])
        self.assertTrue(offline["advice"])
        self.assertEqual(len(self.listed()), 1, "looking again does not raise it again")
        self.assertEqual(device["unhandled_alerts"], 1)

    def test_a_device_that_reports_again_is_online_and_a_new_silence_is_a_new_alert(self):
        self.ok(self.report())
        self.age_last_report(OFFLINE_AFTER_S + 5)
        self.assertEqual(len(self.listed()), 1)
        self.ok(self.report(alert("a-1")))                    # back, with what it kept while cut off
        self.assertEqual(self.devices()[0]["state"], "online")
        offline = next(a for a in self.listed() if a["kind"] == "device_offline")
        self.assertIsInstance(offline["detail"]["recovered_at"], float)
        self.age_last_report(OFFLINE_AFTER_S + 5)
        self.assertEqual(sorted(a["kind"] for a in self.listed()), ["device_offline", "device_offline", "estop_latched"])

    def test_the_background_sweep_notices_without_anyone_looking(self):
        self.ok(self.report())
        self.ok(self.report(instance_id="dog-2"))
        self.assertEqual(maintenance.sweep(self.db, self.settings)["devices_offline"], 0)
        self.age_last_report(OFFLINE_AFTER_S + 5)
        self.assertEqual(maintenance.sweep(self.db, self.settings)["devices_offline"], 1)
        self.assertEqual(maintenance.sweep(self.db, self.settings)["devices_offline"], 0)
        with self.db.transaction() as s:
            kinds = [(a.instance_id, a.kind) for a in s.execute(select(DeviceAlert)).scalars()]
        self.assertEqual(kinds, [("dog-1", "device_offline")])
        self.assertEqual(alerts.sweep_offline(self.db, self.settings, at=time.time() + OFFLINE_AFTER_S + 5), 1)

    def test_offline_devices_come_first(self):
        for name in ("dog-a", "dog-b", "dog-c"):
            self.ok(self.report(instance_id=name))
        self.age_last_report(OFFLINE_AFTER_S + 5, "dog-b")
        self.assertEqual([(d["instance_id"], d["state"]) for d in self.devices()],
                         [("dog-b", "offline"), ("dog-a", "online"), ("dog-c", "online")])


class Limits(AlertsCase):
    settings_overrides = {"device_report_max_per_window": 3, "device_alert_keep": 3, "device_report_max_devices": 2}

    def test_one_device_cannot_report_without_end(self):
        for _ in range(3):
            self.ok(self.report())
        self.error(self.report(alert("a-1")), 429, "rate_limited")
        self.assertEqual(self.listed(), [])
        self.ok(self.report(alert("b-1"), instance_id="dog-2"))    # another device is not held back by it

    def test_only_the_newest_alerts_of_a_device_are_kept(self):
        self.ok(self.report(*[alert(f"a-{i}") for i in range(2)]))
        self.ok(self.report(*[alert(f"a-{i}") for i in range(2, 5)]))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["a-2", "a-3", "a-4"])

    def test_one_organisation_cannot_invent_devices_without_end(self):
        self.ok(self.report(instance_id="dog-1"))
        self.ok(self.report(instance_id="dog-2"))
        self.error(self.report(alert("a-1"), instance_id="dog-3"), 409, "too_many_devices")
        self.ok(self.report(alert("a-1"), instance_id="dog-2"))
        self.assertEqual(sorted(d["instance_id"] for d in self.devices()), ["dog-1", "dog-2"])


class TextAndTime(AlertsCase):
    def raw(self, body: dict):
        """As a device's own JSON library would write it (escapes kept, numbers of any size)."""
        return self.client.post(f"{API}/device-reports", content=json.dumps(body, allow_nan=True),
                                headers={**self.device["headers"], "Content-Type": "application/json"})

    def test_text_no_database_can_hold_is_refused_alert_by_alert(self):
        half_emoji, nul = "\ud83d", "\x00"
        bad = [alert("b-reason", reason="围栏" + half_emoji), alert("b-advice", advice="去" + nul + "现场"),
               alert("b-detail", detail={"note": "a" + nul}), alert("b-detail-key", detail={half_emoji: 1}),
               alert("b-bell", reason="响\x07了")]
        body = {"schema": "neuxmind.device_report.v1",
                "device": {"instance_id": "dog-1", "name": "狗" + half_emoji + nul, "family": "quad" + nul},
                "alerts": [alert("ok-1"), *bad, alert("ok-2", reason="两行\n原因\t可以")]}
        answer = self.ok(self.raw(body))
        self.assertEqual(answer["accepted"], ["ok-1", "ok-2"])
        self.assertEqual(sorted(r["id"] for r in answer["rejected"]), sorted(a["id"] for a in bad))
        (device,) = self.devices()
        json.dumps(device, ensure_ascii=False).encode("utf-8")           # stored clean
        self.assertNotIn(nul, device["name"] + device["family"])
        self.assertEqual(self.ok(self.raw({**body, "alerts": []}))["accepted"], [])
        self.error(self.raw({**body, "device": {"instance_id": "dog" + half_emoji}}), 400, "invalid_report")

    def test_text_that_only_looks_like_an_escape_is_just_text(self):
        written_out = "日志里写着 \\u0000 这六个字符"                # a backslash and "u0000", not a NUL
        answer = self.ok(self.raw({"schema": "neuxmind.device_report.v1", "device": {"instance_id": "dog-1"},
                                   "alerts": [alert("ok-1", detail={"note": written_out, "deep": {"list": [written_out]}})]}))
        self.assertEqual((answer["accepted"], answer["rejected"]), (["ok-1"], []))
        self.assertEqual(self.listed()[0]["detail"]["note"], written_out)

    def test_values_in_the_address_that_no_database_takes_get_an_answer_not_a_crash(self):
        self.ok(self.report(alert("a-1")))
        (seen,) = self.listed()
        self.error(self.get("/alerts?instance_id=dog%00x", self.user), 400)
        self.error(self.get("/alerts?instance_id=" + "x" * 300, self.user), 400)
        self.error(self.client.delete(f"{API}/alerts/devices", params={"instance_id": "dog\x00x"},
                                      headers=self.user["headers"]), 400)
        for strange in ("alr_%00", seen["id"] + "%00", "x" * 300, "alr_" + "g" * 20):
            self.error(self.post(f"/alerts/{strange}/handled", {}, self.user), 404)
        self.assertIs(self.listed()[0]["handled"], False)

    def test_a_time_no_clock_shows_is_refused_alert_by_alert(self):
        body = {"schema": "neuxmind.device_report.v1", "device": {"instance_id": "dog-1"},
                "alerts": [alert("ok-1"), alert("b-huge", ts=10 ** 400), alert("b-far", ts=1e15),
                           alert("b-neg", ts=-5), alert("b-nan", ts=float("nan")), alert("b-bool", ts=True)]}
        answer = self.ok(self.raw(body))
        self.assertEqual(answer["accepted"], ["ok-1"])
        self.assertEqual(len(answer["rejected"]), 5)

    def test_several_alerts_of_one_report_page_in_order(self):
        self.ok(self.report(*[alert(f"a-{i}") for i in range(5)]))
        seen, query = [], "?limit=2"
        while True:
            page = self.ok(self.get("/alerts" + query, self.user))
            seen += [a["alert_id"] for a in page["alerts"]]
            if not page["more"]:
                break
            query = f"?limit=2&before={page['next_before']}"
        self.assertEqual(seen, ["a-4", "a-3", "a-2", "a-1", "a-0"])

    def test_how_many_alerts_the_device_still_holds_is_shown(self):
        self.ok(self.report(alert("a-1"), pending=7))
        self.assertEqual(self.devices()[0]["pending_alerts"], 7)
        for strange in (-3, "many", 10 ** 30, None, True):
            self.ok(self.report(pending=strange))
            self.assertEqual(self.devices()[0]["pending_alerts"], 0, strange)


class Sessions(AlertsCase):
    def test_a_login_session_cannot_report_as_a_device(self):
        self.error(self.report(alert("a-1"), headers=self.user["headers"]), 403, "api_key_required")
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.devices(), [])

    def test_reports_are_on_record_with_where_they_came_from(self):
        self.ok(self.report(alert("a-1"), alert("b-1", level="urgent")))
        with self.db.transaction() as s:
            rows = {row.action: row for row in s.execute(select(AuditLog)).scalars()}
        self.assertTrue(rows["device.alerts"].ip)
        self.assertEqual((rows["device.alerts"].detail["accepted"], rows["device.alerts"].detail["rejected"]), (1, 1))
        self.assertTrue(rows["device.first_report"].ip)


class Forgetting(AlertsCase):
    def forget(self, instance_id: str, **kwargs):
        headers = kwargs.pop("headers", None) or self.user["headers"]
        return self.client.delete(f"{API}/alerts/devices", params={"instance_id": instance_id}, headers=headers)

    def test_a_device_that_is_gone_for_good_can_be_taken_off_the_list(self):
        self.ok(self.report(alert("a-1")))
        self.age_last_report(OFFLINE_AFTER_S + 5)
        self.assertEqual(self.devices()[0]["state"], "offline")
        self.assertEqual(self.ok(self.forget("dog-1")), {"forgotten": True, "instance_id": "dog-1"})
        self.assertEqual(self.devices(), [])
        self.assertEqual(sorted(a["kind"] for a in self.listed()), ["device_offline", "estop_latched"],
                         "its alerts stay; it is only no longer watched")
        self.assertEqual(maintenance.sweep(self.db, self.settings)["devices_offline"], 0)
        self.error(self.forget("dog-1"), 404)
        self.ok(self.report())                                # should it report again, it is back
        self.assertEqual([(d["instance_id"], d["state"]) for d in self.devices()], [("dog-1", "online")])

    def test_only_people_of_the_organisation_can_forget_a_device(self):
        self.ok(self.report())
        stranger = self.register("stranger@example.com")
        self.error(self.forget("dog-1", headers=stranger["headers"]), 404)
        self.error(self.forget("dog-1", headers=self.device["headers"]), 403, "scope_missing")
        self.error(self.client.delete(f"{API}/alerts/devices", headers=self.user["headers"]), 400)
        self.assertEqual(len(self.devices()), 1)


class KeepOrder(AlertsCase):
    settings_overrides = {"device_alert_keep": 3}

    def test_handled_and_minor_alerts_make_room_before_an_unhandled_critical_one(self):
        self.ok(self.report(alert("a-critical"), alert("a-info", level="info", kind="note"),
                            alert("a-warning", level="warning", kind="disk_low")))
        handled = next(a for a in self.listed() if a["alert_id"] == "a-warning")
        self.ok(self.post(f"/alerts/{handled['id']}/handled", {}, self.user))
        self.ok(self.report(alert("b-critical"), alert("c-critical")))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["a-critical", "b-critical", "c-critical"])

    def test_an_alert_someone_has_dealt_with_goes_before_a_minor_one_nobody_has_seen(self):
        self.ok(self.report(alert("c-handled"), alert("i-1", level="info", kind="note"),
                            alert("i-2", level="info", kind="note")))
        handled = next(a for a in self.listed() if a["alert_id"] == "c-handled")
        self.ok(self.post(f"/alerts/{handled['id']}/handled", {}, self.user))
        self.ok(self.report(alert("i-3", level="info", kind="note")))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["i-1", "i-2", "i-3"])

    def test_a_flood_of_minor_alerts_does_not_push_out_what_nobody_has_looked_at(self):
        self.ok(self.report(alert("real-1"), alert("real-2", kind="stop_not_obeyed")))
        for batch in range(3):
            self.ok(self.report(*[alert(f"flood-{batch}-{i}", level="info", kind="note") for i in range(50)]))
        kept = sorted(a["alert_id"] for a in self.listed())
        self.assertEqual(len(kept), 3)
        self.assertEqual([k for k in kept if k.startswith("real")], ["real-1", "real-2"])


class OrganisationLimits(AlertsCase):
    settings_overrides = {"device_alert_org_keep": 4, "device_report_org_max_per_window": 6}

    def test_one_organisation_cannot_store_alerts_without_end(self):
        self.ok(self.report(*[alert(f"a-{i}") for i in range(3)], instance_id="dog-1"))
        self.ok(self.report(*[alert(f"b-{i}") for i in range(3)], instance_id="dog-2"))
        self.assertEqual(sorted(a["alert_id"] for a in self.listed()), ["a-2", "b-0", "b-1", "b-2"])

    def test_one_key_cannot_report_as_many_devices_without_end(self):
        for index in range(6):
            self.ok(self.report(instance_id=f"dog-{index}"))
        self.error(self.report(instance_id="dog-6"), 429, "rate_limited")
        self.assertEqual(len(self.devices()), 6)


class AtTheSameTime(AlertsCase):
    """Real requests in parallel (uvicorn + threads): a device that reports the same alerts twice at
    once -- an immediate report crossing the five-minute one -- and many devices reporting for the
    first time together. On PostgreSQL this is what the advisory locks and the row lock are for."""

    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.app)
        self.addCleanup(self.live.stop)

    def in_parallel(self, bodies: list[dict]) -> list:
        import concurrent.futures
        import httpx

        def send(body):
            with httpx.Client(trust_env=False, timeout=60) as client:
                return client.post(f"{self.live.url}{API}/device-reports", json=body, headers=self.device["headers"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            return list(pool.map(send, bodies))

    def body(self, instance_id: str, *items) -> dict:
        return {"schema": "neuxmind.device_report.v1", "device": {"instance_id": instance_id, "name": instance_id},
                "alerts": list(items)}

    def test_the_same_alerts_arriving_twice_at_once_are_stored_once(self):
        items = [alert(f"a-{i}") for i in range(5)]
        answers = self.in_parallel([self.body("dog-1", *items) for _ in range(8)])
        self.assertEqual([a.status_code for a in answers], [200] * 8, [a.text for a in answers])
        accepted = sorted(i for a in answers for i in a.json()["accepted"])
        self.assertEqual(accepted, [f"a-{i}" for i in range(5)], "each alert is accepted by exactly one of them")
        self.assertEqual(sum(len(a.json()["duplicates"]) for a in answers), 35)
        self.assertEqual(len(self.listed()), 5)
        (device,) = self.devices()
        self.assertEqual(device["reports"], 8)

    def test_many_devices_reporting_for_the_first_time_at_once(self):
        answers = self.in_parallel([self.body(f"dog-{n}", alert("a-1")) for n in range(12)])
        self.assertEqual([a.status_code for a in answers], [200] * 12, [a.text for a in answers])
        self.assertEqual(len(self.devices()), 12)
        self.assertEqual(len(self.listed()), 12)


class Scope(CloudTestCase):
    def test_the_report_scope_can_be_put_on_a_key_and_is_off_by_default(self):
        owner = self.register("owner@example.com")
        default = self.ok(self.post("/api-keys", {"name": "default"}, owner), 201)
        self.assertNotIn(REPORT_SCOPE, default["scopes"])
        device = self.ok(self.post("/api-keys", {"name": "dog-1", "scopes": [REPORT_SCOPE]}, owner), 201)
        self.assertEqual(device["scopes"], [REPORT_SCOPE])
        self.error(self.post("/api-keys", {"name": "x", "scopes": ["alerts:read-everything"]}, owner), 400, "invalid_scope")
