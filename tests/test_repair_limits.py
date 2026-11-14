"""Issue #10: a hardware repair runs without a Token or time limit.

The "让助手修复硬件问题" form no longer asks for a Token limit or a maximum run
time. A repair started without them is not stopped by usage or by the clock; the
user stops it, and still approves every command. The rule is the one
investigations follow since issue #5: explicit limits sent by API callers are
validated and enforced as before, and models billed through a reserving gateway
(cloud / platform) keep the budget a repair always had, because the gateway
cannot open a session without one.
Uses the local Responses fixture and the Codex stand-in, never a paid API.
"""
import secrets
import time
import unittest
from unittest import mock

from brainos import agent_runtime, hardware_init
from brainos.engineering import ACTIVE
from test_hardware_init import SERIAL, USERMOD, Base, RepairModel
from test_investigation_limits import OLD_MAXIMUM_TOKENS, HeavyUsageRuntime
from test_product import Server

# Throwaway model key for the local fixture, generated per run.
MODEL_KEY = secrets.token_hex(12)
ISSUES = SERIAL + ['usb-driver:1-4']        # what the scripted repair model reports on


class RepairLimitRulesTests(unittest.TestCase):
    def test_no_limits_given_means_no_limits(self):
        for billing in ('tokenfans', 'provider'):
            with self.subTest(billing=billing):
                self.assertEqual(hardware_init.repair_limits({}, billing), (None, None))

    def test_null_limits_mean_no_limits(self):
        self.assertEqual(hardware_init.repair_limits({'max_tokens': None, 'max_minutes': None}, 'tokenfans'), (None, None))

    def test_explicit_limits_are_still_validated_and_kept(self):
        self.assertEqual(hardware_init.repair_limits({'max_tokens': 5000, 'max_minutes': 2}, 'tokenfans'), (5000, 2))
        self.assertEqual(hardware_init.repair_limits({'max_minutes': 45}, 'tokenfans'), (None, 45))
        for bad in ({'max_tokens': 5}, {'max_tokens': 1000001}, {'max_tokens': '5000'}, {'max_tokens': True},
                    {'max_minutes': 0}, {'max_minutes': 121}, {'max_minutes': 1.5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hardware_init.repair_limits(bad, 'tokenfans')

    def test_reserving_gateways_keep_the_budget_a_repair_always_had(self):
        for billing in ('cloud', 'platform'):
            with self.subTest(billing=billing):
                self.assertEqual(hardware_init.repair_limits({}, billing), (200000, 30))
                self.assertEqual(hardware_init.repair_limits({'max_tokens': None, 'max_minutes': None}, billing), (200000, 30))
                self.assertEqual(hardware_init.repair_limits({'max_tokens': 90000, 'max_minutes': 10}, billing), (90000, 10))
                self.assertEqual(hardware_init.repair_limits({'max_tokens': 90000}, billing), (90000, 30))
                self.assertEqual(hardware_init.repair_limits({'max_minutes': 10}, billing), (200000, 10))

    def test_the_request_is_not_changed(self):
        request = {'max_tokens': None}
        hardware_init.repair_limits(request, 'cloud')
        self.assertEqual(request, {'max_tokens': None})


class StartRepairLimitTests(Base):
    """What start_repair stores, without running the engine."""

    def setUp(self):
        super().setUp()
        self.baseline = self.scan()
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': 'http://127.0.0.1:9/v1', 'api_key': MODEL_KEY})
        self.launched = []
        for patcher in (mock.patch.object(self.e, '_launch', lambda s, message, resume: self.launched.append(s['id'])),
                        mock.patch.object(agent_runtime, 'available', lambda: {'available': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, **extra):
        return self.hw.start_repair({'project_id': self.pid, 'robot_id': self.robot, 'scan_id': self.baseline['id'], 'issue_ids': SERIAL,
                                     'model_id': self.model['id'], 'data_authorized': True, 'modify_authorized': True, **extra})

    def test_repair_started_from_the_form_has_no_limits(self):
        s = self.start()                    # the form sends neither field
        self.assertIsNone(s['max_tokens'])
        self.assertIsNone(s['max_minutes'])
        self.assertEqual(self.launched, [s['id']])

    def test_null_limits_are_no_limits(self):
        s = self.start(max_tokens=None, max_minutes=None)
        self.assertEqual((s['max_tokens'], s['max_minutes']), (None, None))

    def test_explicit_limits_from_an_api_caller_are_kept(self):
        s = self.start(max_tokens=20000, max_minutes=2)
        self.assertEqual((s['max_tokens'], s['max_minutes']), (20000, 2))

    def test_invalid_explicit_limits_start_nothing(self):
        for extra in ({'max_tokens': 999}, {'max_tokens': 1000001}, {'max_minutes': 0}, {'max_minutes': 121}, {'max_tokens': '200000'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.start(**extra)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.store.list('agent_sessions'), [])

    def test_reserving_gateway_model_starts_with_a_budget(self):
        gateway = {**self.model, 'billing': 'cloud'}
        checked = []          # the budget the gateway readiness check is given
        with mock.patch.object(self.hw, '_model', lambda project_id, model_id: gateway), \
                mock.patch.object(self.e, '_cloud_ready', lambda model, tokens: checked.append(tokens)):
            s = self.start()
        self.assertEqual((s['billing'], s['max_tokens'], s['max_minutes']), ('cloud', 200000, 30))
        self.assertEqual(checked, [200000])


class UnlimitedRepairRunTests(Base):
    """One whole repair through the engine: scan file, approved fix, rescan, report."""

    def setUp(self):
        super().setUp()
        self.baseline = self.scan(source='connect')
        RepairModel.requests, RepairModel.ssh = [], self.ssh
        self.addCleanup(setattr, RepairModel, 'ssh', None)          # shared class state: never leave a closed fixture behind
        self.addCleanup(setattr, RepairModel, 'requests', [])
        self.model_server = Server(RepairModel)
        self.addCleanup(self.model_server.close)
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': self.model_server.url + '/v1', 'api_key': MODEL_KEY})
        available = agent_runtime.available
        for patcher in (mock.patch.object(agent_runtime, 'AgentProcess', HeavyUsageRuntime),
                        mock.patch.object(agent_runtime, 'available', lambda: {**available(), 'available': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, **limits):
        return self.hw.start_repair({'project_id': self.pid, 'robot_id': self.robot, 'scan_id': self.baseline['id'], 'issue_ids': ISSUES,
                                     'model_id': self.model['id'], 'data_authorized': True, 'modify_authorized': True, **limits})

    def poll(self, sid, predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = self.store.get('agent_sessions', sid)
            if predicate(s):
                return s
            time.sleep(.05)
        self.fail('Timed out in status ' + s['status'] + ': ' + str(s.get('error')))

    def finished(self, sid):
        return self.poll(sid, lambda s: s['status'] not in ACTIVE)

    def test_unlimited_repair_runs_past_the_old_maximum_and_saves_its_report(self):
        sid = self.start()['id']
        waiting = self.poll(sid, lambda s: bool(s.get('pending_approval')) or s['status'] not in ACTIVE)
        self.assertEqual(waiting['status'], 'waiting_input', [event['message'] for event in waiting['events']][-3:])
        pending = waiting['pending_approval']
        self.assertEqual(pending['method'], 'device_fix/requestApproval', 'every command still waits for the user')
        self.e.approve({'session_id': sid, 'approval_id': pending['id'], 'decision': 'accept'})
        s = self.finished(sid)
        self.assertEqual(s['status'], 'completed', s.get('error'))
        self.assertTrue(s['repair_report_id'])
        self.assertGreater(s['usage'].get('totalTokens', 0), OLD_MAXIMUM_TOKENS)
        self.assertNotIn('budget', [event['kind'] for event in s['events']])

    def test_explicit_token_limit_still_stops_an_api_started_repair(self):
        s = self.finished(self.start(max_tokens=1000, max_minutes=1)['id'])
        self.assertEqual(s['status'], 'interrupted')
        self.assertIn('budget', [event['kind'] for event in s['events']])
        self.assertIsNone(s.get('repair_report_id'))
        self.assertNotIn(USERMOD, self.ssh.commands, 'nothing was changed on the device')


if __name__ == '__main__':
    unittest.main()
