"""Issue #5: an investigation of the robot body runs without a Token or time limit.

The "让 Agent 调查本体" form no longer asks for a Token limit or a maximum run
time. A run started without them is not stopped by usage or by the clock; the
user stops it. Explicit limits sent by API callers are still honoured, and
models billed through a reserving gateway (cloud / platform) keep the old
default budget because the gateway cannot open a session without one.
Uses the local Responses fixture and the Codex stand-in, never a paid API.
"""
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_binary  # noqa: F401  (optional NEUXMIND_TEST_AGENT_BINARY)
from brainos import agent_runtime
from brainos.engineering import ACTIVE, discovery_limits, over_token_limit, run_deadline
from brainos.service import Platform
from codex_standin import CodexStandIn
from ssh_fixture import SSHFixture
from test_engineering import ResponsesFixture
from test_product import Server

# Each fixture model call reports 30 tokens; the runtime below scales that so a
# three-call investigation reports 9,000,000 tokens, far above the old maximum
# accepted limit (1,000,000).
USAGE_SCALE = 100_000
OLD_MAXIMUM_TOKENS = 1_000_000
# Throwaway credentials for the local fixtures, generated per run.
SSH_LOGIN = secrets.token_hex(12)
MODEL_KEY = secrets.token_hex(12)


class HeavyUsageRuntime(CodexStandIn):
    """The Codex stand-in, reporting very large token usage."""

    def _emit(self, method, params, request_id=None):
        if method == 'thread/tokenUsage/updated':
            usage = params['tokenUsage']
            params = {**params, 'tokenUsage': {
                'total': {'totalTokens': usage['total']['totalTokens'] * USAGE_SCALE},
                'last': {'totalTokens': usage['last']['totalTokens'] * USAGE_SCALE}}}
        super()._emit(method, params, request_id)


class InvestigationModel(ResponsesFixture):
    requests = []


class LimitRulesTests(unittest.TestCase):
    def test_no_limits_given_means_no_limits(self):
        self.assertEqual(discovery_limits({}, 'tokenfans'), (None, None))

    def test_null_limits_from_the_form_mean_no_limits(self):
        # The form has no limit fields; the browser sends Number(undefined) -> null.
        self.assertEqual(discovery_limits({'max_tokens': None, 'max_minutes': None}, 'provider'), (None, None))

    def test_explicit_limits_are_still_validated_and_kept(self):
        self.assertEqual(discovery_limits({'max_tokens': 5000, 'max_minutes': 2}, 'tokenfans'), (5000, 2))
        with self.assertRaises(ValueError):
            discovery_limits({'max_tokens': 5}, 'tokenfans')
        with self.assertRaises(ValueError):
            discovery_limits({'max_minutes': 0}, 'tokenfans')

    def test_reserving_gateways_keep_the_default_budget(self):
        for billing in ('cloud', 'platform'):
            with self.subTest(billing=billing):
                self.assertEqual(discovery_limits({}, billing), (200000, 15))
                self.assertEqual(discovery_limits({'max_tokens': None, 'max_minutes': None}, billing), (200000, 15))

    def test_run_without_a_time_limit_has_no_deadline(self):
        self.assertEqual(run_deadline(100.0, None), float('inf'))
        self.assertEqual(run_deadline(100.0, 2), 220.0)

    def test_token_usage_only_stops_a_run_that_has_a_limit(self):
        self.assertFalse(over_token_limit({'totalTokens': 9_000_000}, None))
        self.assertFalse(over_token_limit({'totalTokens': 999}, 1000))
        self.assertTrue(over_token_limit({'totalTokens': 1000}, 1000))
        self.assertFalse(over_token_limit({}, 1000))


class UnlimitedInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Platform(self.tmp.name)
        self.e = self.p.engineering
        self.pid = self.p.add_project({'name': 'Investigation', 'objective': 'Inspect the robot body'})['id']
        self.ssh = SSHFixture(files={'/opt/ros/readme.txt': b'fixture'}, password=SSH_LOGIN)
        scan = self.e.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': self.ssh.port, 'username': 'robot'})
        connection = self.e.connect({'project_id': self.pid, 'host_check_id': scan['id'], 'trust_fingerprint': scan['fingerprint'],
                                     'auth': 'password', 'password': SSH_LOGIN, 'name': 'Fixture', 'read_roots': ['/opt/ros']})
        self.robot = connection['robot_id']
        InvestigationModel.requests = []
        self.model_server = Server(InvestigationModel)
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': self.model_server.url + '/v1', 'api_key': MODEL_KEY})
        available = agent_runtime.available
        self.patches = [patch.object(agent_runtime, 'AgentProcess', HeavyUsageRuntime),
                        patch.object(agent_runtime, 'available', lambda: {**available(), 'available': True})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.p.close()
        self.model_server.close()
        self.ssh.close()
        self.tmp.cleanup()

    def finished(self, sid, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = self.p.store.get('agent_sessions', sid)
            if s['status'] not in ACTIVE:
                return s
            time.sleep(.05)
        self.fail('Investigation did not finish: ' + s['status'])

    def start(self, **limits):
        return self.e.start({'robot_id': self.robot, 'model_id': self.model['id'], 'data_authorized': True, **limits})

    def test_investigation_started_from_the_form_has_no_limits(self):
        s = self.start(max_tokens=None, max_minutes=None)
        self.assertIsNone(s['max_tokens'])
        self.assertIsNone(s['max_minutes'])
        self.finished(s['id'])

    def test_unlimited_investigation_runs_past_the_old_maximum_and_saves_its_inventory(self):
        s = self.finished(self.start()['id'])
        self.assertEqual(s['status'], 'completed', s.get('error'))
        self.assertTrue(s['inventory_id'])
        self.assertGreater(s['usage'].get('totalTokens', 0), OLD_MAXIMUM_TOKENS)
        self.assertNotIn('budget', [event['kind'] for event in s['events']])

    def test_explicit_token_limit_still_stops_an_api_started_run(self):
        s = self.finished(self.start(max_tokens=1000, max_minutes=1)['id'])
        self.assertEqual(s['status'], 'interrupted')
        self.assertIn('budget', [event['kind'] for event in s['events']])

    def test_resume_of_an_unlimited_investigation_stays_unlimited(self):
        first = self.finished(self.start()['id'])
        resumed = self.e.resume({'session_id': first['id'], 'message': '继续核对驱动', 'data_authorized': True})
        self.assertIsNone(resumed['max_tokens'])
        s = self.finished(first['id'])
        self.assertEqual(s['status'], 'completed', s.get('error'))
        self.assertGreater(s['usage'].get('totalTokens', 0), OLD_MAXIMUM_TOKENS)
        self.assertNotIn('budget', [event['kind'] for event in s['events']])


if __name__ == '__main__':
    unittest.main()
