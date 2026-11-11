"""Round 03, contract §5.3: the engineering assistant on a cloud-platform model.

Desktop (brainos) → real cloud app (cloud/, local uvicorn, SQLite, Echo
upstream — every model reply is ``simulated: true``) → engineering runtime.

* Protocol level (always runs when the cloud's dependencies are installed):
  :class:`ReplayAppServer` stands in for the Codex app-server process and sends
  the **request bodies recorded from the real Codex 0.146.1 binary**
  (cloud/tests/fixtures/codex-0.146.1-responses.json) to the session
  ``base_url`` with the session token, exactly like the runtime would, and
  reports errors in the runtime's recorded format.
* Real runtime (when a Codex 0.146.1 binary is available, see
  tests/agent_binary.py): the actual app-server drives a tool loop through
  the gateway.

What is proven: metering and charging happen only in the cloud — the local
ledger (``brainos.commerce``) is never called and does not change; the cloud
session is closed after success, stop and failure; errors are shown in words
a user understands; the access token is never stored.
"""
import copy
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import agent_binary  # noqa: F401
from brainos import agent_runtime, engineering
from brainos.cloud_account import CloudError
from brainos.commerce import Commerce
from brainos.service import Platform

try:
    import cloud_fixture
except ImportError as exc:  # cloud dependencies not installed in this interpreter
    cloud_fixture = None
    SKIP = f'cloud dependencies not installed: {exc}'
else:
    SKIP = ''

ACTIVE = ('starting', 'running', 'waiting_input', 'cancelling')
CODE_LOOP = {'id': 'echo-code', 'name': 'Echo 编码模型（测试替身）', 'source': 'closed', 'kind': 'llm',
             'license': 'test only', 'pricing': {'type': 'per_1k_tokens', 'credits': 10},
             'upstream': {'provider': 'echo', 'behavior': 'tools', 'tool_name': 'exec_command',
                          'tool_arguments': {'cmd': "printf '# cloud adapter\\n' > adapter.py"}}}
DISCOVERY_LOOP = {**CODE_LOOP, 'id': 'echo-discover', 'name': 'Echo 调查模型（测试替身）',
                  'upstream': {'provider': 'echo', 'behavior': 'tools', 'tool_name': 'device_overview'}}
OVERRUN = {**CODE_LOOP, 'id': 'echo-overrun', 'name': 'Echo 超额模型（测试替身）', 'upstream': {'provider': 'echo', 'behavior': 'usage_overrun'}}


class UpstreamGate:
    """Holds the first streamed upstream call open until the test releases it
    (``entered`` is set once a call is inside the Echo upstream)."""
    entered = threading.Event()
    release = threading.Event()

    @classmethod
    def reset(cls):
        cls.entered, cls.release = threading.Event(), threading.Event()

    @classmethod
    def patch(cls):
        from neuxmind_cloud import upstream
        original = upstream.EchoProvider.open_stream

        def gated(provider, path, body):
            if not cls.entered.is_set():
                cls.entered.set()
                cls.release.wait(15)
            return original(provider, path, body)
        return patch.object(upstream.EchoProvider, 'open_stream', gated)


class ReplayAppServer:
    """Protocol-level stand-in for ``agent_runtime.AgentProcess``.

    On ``turn/start`` it POSTs the recorded Codex requests to
    ``profile['base_url'] + '/responses'`` with ``Authorization: Bearer
    <profile['api_key']>`` and ``Accept: text/event-stream`` (as recorded), reads
    the SSE stream to its end, and turns the outcome into app-server
    notifications. Errors use the runtime's recorded wording
    (``unexpected status 402 Payment Required: {body}, url: ...``)."""

    instances = []
    mode = 'loop'            # loop | hold (wait for interrupt after the first call) | concurrent

    def __init__(self, home, work, profile, engineering=False, extensions=None, analysis_tools=False, cancel=None):
        self.profile, self.events, self.loaded_skills = profile, queue.Queue(), []
        self.interrupted, self.closed, self.statuses = threading.Event(), False, []
        Path(work).mkdir(parents=True, exist_ok=True)
        type(self).instances.append(self)

    def request(self, method, params, timeout=25, cancel=None):   # cancel: round 04 (G-2) signature
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'thread-replay'}}
        if method == 'turn/start':
            threading.Thread(target=self._turn, daemon=True).start()
            return {'turn': {'id': 'turn-replay'}}
        if method == 'turn/interrupt':
            self.interrupted.set()
            return {}
        raise ValueError('unsupported ' + method)

    def respond(self, request_id, result):
        pass

    def send(self, value):
        pass

    def close(self):
        self.closed = True
        self.interrupted.set()

    # ------------------------------------------------------------ HTTP
    def post(self, body):
        req = urllib.request.Request(self.profile['base_url'] + '/responses', data=json.dumps(body).encode(),
                                     method='POST', headers={'Authorization': 'Bearer ' + self.profile['api_key'],
                                                             'Content-Type': 'application/json',
                                                             'Accept': 'text/event-stream'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=20) as r:
                self.statuses.append(r.status)
                return r.read().decode()
        except urllib.error.HTTPError as exc:
            self.statuses.append(exc.code)
            raise ValueError(f'unexpected status {exc.code} {exc.reason}: {exc.read().decode()}, '
                             f"url: {self.profile['base_url']}/responses") from None

    def _turn(self):
        bodies = [r['body'] for r in cloud_fixture.RECORDED['requests']]
        first = copy.deepcopy(bodies[0])
        first['input'] = [i for i in first['input'] if i['type'] == 'message']
        try:
            if self.mode == 'concurrent':
                # Review Q4-20: the second call is sent only while the first one
                # is provably in flight (held inside the upstream by UpstreamGate),
                # so the cloud's session lock must answer 409 — no timing luck.
                results = []
                first_call = threading.Thread(target=lambda: results.append(self._safe(first)))
                first_call.start()
                try:
                    if not UpstreamGate.entered.wait(10):
                        raise ValueError('test gate: the first call never reached the upstream')
                    results.append(self._safe(first))
                finally:
                    UpstreamGate.release.set()
                    first_call.join(20)
                failed = [r for r in results if isinstance(r, Exception)]
                if failed:
                    raise failed[0]
            stream = self.post(first)
            if 'event: error' in stream:
                code = json.loads(stream.rsplit('event: error\ndata: ', 1)[1].split('\n')[0])['code']
                raise ValueError(f'stream error: {code}')
            self.events.put({'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': 'thread-replay', 'turnId': 'turn-replay',
                'tokenUsage': {'total': {'totalTokens': 30}, 'last': {'totalTokens': 30}}}})
            if self.mode == 'hold':
                self.interrupted.wait(20)
                self.events.put({'method': 'turn/completed', 'params': {
                    'threadId': 'thread-replay', 'turn': {'id': 'turn-replay', 'status': 'interrupted'}}})
                return
            self.post(bodies[0])
            self.events.put({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'done'}}})
            self.events.put({'method': 'turn/completed', 'params': {
                'threadId': 'thread-replay', 'turn': {'id': 'turn-replay', 'status': 'completed'}}})
        except Exception as exc:
            self.events.put({'method': 'turn/completed', 'params': {
                'threadId': 'thread-replay', 'turn': {'id': 'turn-replay', 'status': 'failed',
                                                      'error': {'message': str(exc), 'codexErrorInfo': 'other'}}}})

    def _safe(self, body):
        try:
            return self.post(body)
        except Exception as exc:  # noqa: BLE001
            return exc


class Device:
    def __init__(self, *args):
        pass

    def probe(self, name):
        return {'probe': name, 'stdout': 'Linux fixture', 'stderr': '', 'exit_code': 0}

    def config(self, path, listing=False):
        return {'path': path, 'text': 'fixture: true'}

    def close(self):
        pass


@unittest.skipIf(cloud_fixture is None, SKIP)
class CloudEngineeringBase(unittest.TestCase):
    models = (CODE_LOOP, DISCOVERY_LOOP, OVERRUN)

    @classmethod
    def setUpClass(cls):
        cls.cloud = cloud_fixture.CloudServer(models=cls.models)

    @classmethod
    def tearDownClass(cls):
        cls.cloud.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = Platform(self.tmp.name)
        # The local ledger: any call is a test failure (§5.3: no double billing).
        self.commerce = Commerce(self.tmp.name + '/commerce')
        self.platform.commerce = self.commerce
        self.platform.user_id = 'customer'
        self.commerce.grant({'user_id': 'customer', 'credits': 100, 'reason': 'test'}, 'operator')
        self.ledger_before = self.local_ledger()
        for name in ('reserve', 'settle', 'release'):
            if hasattr(Commerce, name):
                p = patch.object(self.commerce, name, side_effect=AssertionError(f'local commerce.{name} called'))
                setattr(self, 'mock_' + name, p.start())
                self.addCleanup(p.stop)
        self.engine = self.platform.engineering
        self.store = self.platform.store
        self.pid = self.platform.add_project({'name': 'Cloud', 'objective': 'Use cloud models'})['id']
        self._n = getattr(type(self), '_n', 0) + 1
        type(self)._n = self._n
        self.email = f'eng{self._n}-{id(self)}@example.com'

    def tearDown(self):
        self.platform.close()
        self.commerce.close()
        self.tmp.cleanup()

    def local_ledger(self):
        return json.dumps({'balance': self.commerce.balance('customer'), 'ledger': self.commerce.store.list('ledger'),
                           'reservations': self.commerce.store.list('reservations')}, sort_keys=True, default=str)

    def assert_local_ledger_untouched(self):
        for name in ('reserve', 'settle', 'release'):
            mock = getattr(self, 'mock_' + name, None)
            if mock is not None:
                mock.assert_not_called()
        self.assertEqual(self.local_ledger(), self.ledger_before)

    def login(self, credits=10_000, entitle=('echo-code', 'echo-discover', 'echo-overrun'), verify=True):
        self.user = self.cloud.customer(self.email, credits=credits, entitle=entitle, verify=verify)
        self.platform.cloud.configure({'base_url': self.cloud.url})
        self.platform.cloud.login({'email': self.email, 'password': cloud_fixture.PASSWORD})
        return self.user

    def cloud_model(self, model='echo-code'):
        return self.engine.use_cloud_model({'project_id': self.pid, 'model': model})

    def work(self, model, tokens=200_000, **extra):
        return self.engine.start_work({'project_id': self.pid, 'model_id': model['id'], 'name': '云端工程任务',
                                       'objective': 'Create adapter.py', 'data_authorized': True,
                                       'max_tokens': tokens, 'max_minutes': 2, **extra})

    def wait(self, sid, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.store.get('agent_sessions', sid)
            if s['status'] not in ACTIVE and sid not in self.engine.live:
                return s
            time.sleep(0.05)
        self.fail('session did not finish: ' + json.dumps(self.store.get('agent_sessions', sid), ensure_ascii=False))

    def cloud_state(self, session):
        return self.cloud.call('GET', '/gateway/sessions/' + session['cloud_session']['session_id'], None, self.user)


class ProtocolLevelTest(CloudEngineeringBase):
    """The app-server's HTTP requests (recorded from Codex 0.146.1) against the cloud session endpoint."""

    def setUp(self):
        super().setUp()
        ReplayAppServer.instances, ReplayAppServer.mode = [], 'loop'
        real_available = agent_runtime.available
        for target, value in (('AgentProcess', ReplayAppServer),
                              ('available', lambda: {**real_available(), 'available': True})):
            p = patch.object(agent_runtime, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_engineering_task_is_metered_only_in_the_cloud(self):
        self.login()
        wallet_before = self.cloud.wallet(self.user)
        model = self.cloud_model()
        self.assertEqual((model['billing'], model['model']), ('cloud', 'echo-code'))
        s = self.wait(self.work(model)['id'])
        self.assertEqual(s['status'], 'completed', s)
        self.assertEqual(ReplayAppServer.instances[-1].statuses, [200, 200])
        self.assertEqual(s['metering_status'], 'cloud_settled', s)
        metering = s['cloud_session']['metering']
        self.assertEqual(metering['status'], 'closed')
        self.assertGreater(metering['usage_tokens'], 0)
        wallet_after = self.cloud.wallet(self.user)
        self.assertEqual(wallet_before['balance'] - wallet_after['balance'], metering['credits_charged'])
        self.assertEqual(wallet_after['reserved'], 0)
        self.assertEqual(self.cloud_state(s)['status'], 'closed')
        self.assertIn('云平台扣除', s['events'][-1]['message'])
        self.assertEqual(len(s['cloud_runs']), 1)
        self.assertIsNone(s.get('reservation_id'))          # no local reservation was made
        self.assert_local_ledger_untouched()

    def test_access_token_is_never_stored(self):
        self.login()
        tokens = []
        original = self.platform.cloud.open_gateway_session

        def spy(*args):
            grant = original(*args)
            tokens.append(grant['access_token'])
            return grant
        with patch.object(self.platform.cloud, 'open_gateway_session', spy):
            s = self.wait(self.work(self.cloud_model())['id'])
        self.assertEqual(s['status'], 'completed', s)
        self.assertEqual(len(tokens), 1)
        self.assertGreater(len(tokens[0]), 20)
        stored = json.dumps(self.platform.snapshot(), ensure_ascii=False)
        self.assertNotIn(tokens[0], stored)
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file():
                self.assertNotIn(tokens[0].encode(), path.read_bytes(), str(path))

    def test_session_base_url_comes_from_the_configured_address(self):
        self.login()
        s = self.wait(self.work(self.cloud_model())['id'])
        cloud = s['cloud_session']
        self.assertTrue(cloud['base_url'].startswith(self.cloud.url + '/api/v1/gateway/sessions/'))
        # the server's own public_base_url points elsewhere (default port 8800)
        self.assertNotEqual(cloud['server_base_url'].split('/api/')[0], self.cloud.url)

    def test_discovery_uses_the_same_cloud_path(self):
        self.login()
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Cloud robot', 'family': 'custom'})
        self.store.put('device_connections', {'id': 'conn', 'project_id': self.pid, 'robot_id': robot['id'],
                                              'host': '127.0.0.1', 'port': 22, 'username': 't', 'auth': 'password',
                                              'fingerprint': 'SHA256:t', 'read_roots': ['/opt/ros']})
        self.store.put('agent_secrets', {'id': 'conn', 'password': 'x'})
        r = self.store.get('robots', robot['id'])
        r['connection_id'] = 'conn'
        self.store.put('robots', r)
        model = self.cloud_model('echo-discover')
        with patch('brainos.device_access.SSHDevice', Device):
            s = self.engine.start({'robot_id': robot['id'], 'model_id': model['id'], 'data_authorized': True,
                                   'max_tokens': 200_000, 'max_minutes': 2})
            s = self.wait(s['id'])
        self.assertEqual(s['metering_status'], 'cloud_settled', s)
        self.assertEqual(s['status'], 'needs_followup', s)     # replay saves no inventory
        self.assert_local_ledger_untouched()

    def test_stop_closes_the_cloud_session(self):
        self.login()
        ReplayAppServer.mode = 'hold'
        s = self.work(self.cloud_model())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (ReplayAppServer.instances and ReplayAppServer.instances[-1].statuses):
            time.sleep(0.05)
        self.engine.stop({'session_id': s['id']})
        s = self.wait(s['id'])
        self.assertEqual(s['status'], 'interrupted', s)
        self.assertEqual(s['metering_status'], 'cloud_settled', s)
        self.assertEqual(self.cloud_state(s)['status'], 'closed')
        self.assert_local_ledger_untouched()

    def test_runtime_failure_closes_the_cloud_session(self):
        self.login()
        with patch.object(agent_runtime, 'AgentProcess', side_effect=ValueError('工程引擎启动失败')):
            s = self.wait(self.work(self.cloud_model())['id'])
        self.assertEqual(s['status'], 'failed')
        self.assertIn('工程引擎启动失败', s['error'])
        self.assertEqual(s['metering_status'], 'cloud_settled')
        self.assertEqual(s['cloud_session']['metering']['credits_charged'], 0)
        self.assertEqual(self.cloud_state(s)['status'], 'closed')
        self.assertEqual(self.cloud.wallet(self.user)['reserved'], 0)
        self.assert_local_ledger_untouched()

    def test_resume_opens_a_new_cloud_session_and_keeps_both_records(self):
        self.login()
        s = self.wait(self.work(self.cloud_model())['id'])
        first = s['cloud_session']['session_id']
        self.engine.resume({'session_id': s['id'], 'message': '继续', 'data_authorized': True})
        s = self.wait(s['id'])
        self.assertEqual(s['status'], 'completed', s)
        self.assertNotEqual(s['cloud_session']['session_id'], first)
        self.assertEqual([r['status'] for r in s['cloud_runs']], ['closed', 'closed'])
        self.assert_local_ledger_untouched()

    def test_failed_close_is_retried_before_resume_and_never_lost(self):
        self.login()
        account = self.platform.cloud
        original = account.close_gateway_session
        with patch.object(account, 'close_gateway_session',
                          side_effect=CloudError('云平台暂时不可达')):
            s = self.wait(self.work(self.cloud_model())['id'])
        self.assertEqual(s['metering_status'], 'cloud_close_failed', s)
        first = s['cloud_session']['session_id']
        self.assertEqual([(r['session_id'], r['status']) for r in s['cloud_runs']], [(first, 'close_failed')])
        self.assertEqual(self.cloud_state(s)['status'], 'open')
        with patch.object(account, 'close_gateway_session', side_effect=original) as spy:
            self.engine.resume({'session_id': s['id'], 'message': '继续', 'data_authorized': True})
            s = self.wait(s['id'])
        self.assertEqual(s['status'], 'completed', s)
        self.assertEqual(spy.call_args_list[0].args[0], first)       # the stale session is closed first
        self.assertEqual([r['status'] for r in s['cloud_runs']], ['closed', 'closed'])
        self.assertNotEqual(s['cloud_session']['session_id'], first)
        self.assertEqual(self.cloud.call('GET', '/gateway/sessions/' + first, None, self.user)['status'], 'closed')
        self.assertEqual(self.cloud.wallet(self.user)['reserved'], 0)
        self.assert_local_ledger_untouched()

    # ------------------------------------------------------------ errors
    def test_wallet_too_small_is_explained(self):
        self.login(credits=100)                       # 200k tokens at 10/1k = 2000 credits needed
        s = self.wait(self.work(self.cloud_model())['id'])
        self.assertEqual(s['status'], 'failed')
        self.assertIn('云平台钱包可用点数不足', s['error'])
        self.assertIn('需要 2000 点，可用 100 点', s['error'])
        self.assertIsNone(s.get('cloud_session'))
        self.assert_local_ledger_untouched()

    def test_missing_entitlement_is_explained(self):
        self.login(entitle=())
        model = {'id': 'agent-model-x', 'project_id': self.pid, 'name': '云平台 · x', 'model': 'echo-code',
                 'billing': 'cloud', 'cloud_base_url': self.platform.cloud.settings()['base_url'],
                 'protocol': 'responses'}
        self.store.put('agent_models', model)
        s = self.wait(self.work(model)['id'])
        self.assertIn('尚未获得该云平台模型的使用授权', s['error'])

    def test_unverified_email_and_logged_out_fail_before_starting(self):
        self.login(verify=False, entitle=())
        model = self.cloud_model()
        with self.assertRaisesRegex(ValueError, '邮箱尚未验证'):
            self.work(model)
        self.platform.cloud.logout()
        with self.assertRaisesRegex(ValueError, '云平台未登录'):
            self.work(model)
        with self.assertRaisesRegex(ValueError, '请先登录云平台账户'):
            self.cloud_model()

    def test_budget_below_the_cloud_minimum_is_refused_up_front(self):
        self.login()
        with self.assertRaisesRegex(ValueError, f'至少 {engineering.CLOUD_MIN_TOKENS}'):
            self.work(self.cloud_model(), tokens=30_000)

    def test_session_busy_is_explained(self):
        self.login()
        ReplayAppServer.mode = 'concurrent'
        UpstreamGate.reset()
        with UpstreamGate.patch():
            s = self.wait(self.work(self.cloud_model())['id'])
        self.assertTrue(UpstreamGate.entered.is_set())
        self.assertIn(409, ReplayAppServer.instances[-1].statuses)
        self.assertEqual(s['status'], 'failed')
        self.assertIn('同一会话同一时刻只允许一个调用', s['error'])
        self.assertIn('session_busy', s['error_details'])
        self.assertEqual(self.cloud_state(s)['status'], 'closed')
        self.assert_local_ledger_untouched()

    def test_overage_lock_is_explained(self):
        self.login()
        s = self.wait(self.work(self.cloud_model('echo-overrun'))['id'])
        self.assertEqual(s['status'], 'failed', s)
        self.assertIn('超过了预留', s['error'])
        state = self.cloud_state(s)
        self.assertEqual(state['overage_calls'], 1)
        self.assertEqual(s['cloud_session']['metering']['overage_calls'], 1)
        self.assert_local_ledger_untouched()

    def test_logged_out_desktop_keeps_the_original_paths(self):
        self.assertFalse(self.platform.cloud.summary()['gateway_enabled'])
        own = self.engine.model_add({'project_id': self.pid, 'name': 'Own', 'model': 'm',
                                     'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'k'})
        profile, meter = self.engine._model({'id': 'x', 'model_id': own['id'], 'max_tokens': 1000})
        self.assertEqual((profile['base_url'], meter), ('http://127.0.0.1:9/v1', None))

    def test_crash_left_session_is_offered_for_closing(self):
        self.login()
        model = self.cloud_model()
        grant = self.platform.cloud.open_gateway_session('crash-run', 'echo-code', 100_000, 5)
        cloud = {k: grant[k] for k in ('session_id', 'request_id', 'reservation_id', 'reserved_credits')}
        self.store.put('agent_sessions', {'id': 'agent-session-crash', 'project_id': self.pid, 'mode': 'engineering',
                                          'model_id': model['id'], 'status': 'running', 'events': [],
                                          'metering_status': 'cloud_open', 'cloud_session': cloud})
        restarted = engineering.Engineering(self.platform)
        s = self.store.get('agent_sessions', 'agent-session-crash')
        self.assertEqual((s['status'], s['metering_status']), ('interrupted', 'cloud_close_pending'))
        s = restarted.cloud_close({'session_id': s['id']})
        self.assertEqual(s['metering_status'], 'cloud_settled')
        self.assertEqual(self.cloud_state(s)['status'], 'closed')
        with self.assertRaisesRegex(ValueError, '没有待关闭的云端会话'):
            restarted.cloud_close({'session_id': s['id']})
        self.assert_local_ledger_untouched()


@unittest.skipIf(cloud_fixture is None, SKIP)
@unittest.skipUnless(agent_runtime.binary_path(), 'Packaged agent binary required (or NEUXMIND_TEST_AGENT_BINARY)')
class RealRuntimeThroughCloudTest(CloudEngineeringBase):
    """The real Codex 0.146.1 app-server, through the real cloud gateway (Echo tool loop)."""

    def test_engineering_tool_loop_through_the_gateway(self):
        self.login()
        wallet_before = self.cloud.wallet(self.user)
        s = self.wait(self.work(self.cloud_model())['id'], timeout=60)
        self.assertEqual(s['status'], 'completed', json.dumps(s, ensure_ascii=False)[:3000])
        self.assertTrue(any(a['path'] == 'adapter.py' for a in s.get('artifacts', [])), s.get('artifacts'))
        self.assertIn('cloud adapter', self.engine.artifact({'session_id': s['id'], 'path': 'adapter.py'})['content'])
        self.assertEqual(s['metering_status'], 'cloud_settled', s)
        metering = s['cloud_session']['metering']
        self.assertEqual(self.cloud.wallet(self.user)['balance'], wallet_before['balance'] - metering['credits_charged'])
        self.assertGreater(metering['usage_tokens'], 0)
        self.assertTrue(metering['usage_evidence_ref'].endswith('calls=2'))
        self.assert_local_ledger_untouched()

    def test_discovery_through_the_gateway(self):
        self.login()
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Cloud robot', 'family': 'custom'})
        self.store.put('device_connections', {'id': 'conn', 'project_id': self.pid, 'robot_id': robot['id'],
                                              'host': '127.0.0.1', 'port': 22, 'username': 't', 'auth': 'password',
                                              'fingerprint': 'SHA256:t', 'read_roots': ['/opt/ros']})
        self.store.put('agent_secrets', {'id': 'conn', 'password': 'x'})
        r = self.store.get('robots', robot['id'])
        r['connection_id'] = 'conn'
        self.store.put('robots', r)
        with patch('brainos.device_access.SSHDevice', Device):
            s = self.engine.start({'robot_id': robot['id'], 'model_id': self.cloud_model('echo-discover')['id'],
                                   'data_authorized': True, 'max_tokens': 200_000, 'max_minutes': 2})
            s = self.wait(s['id'], timeout=60)
        self.assertEqual(s['status'], 'needs_followup', json.dumps(s, ensure_ascii=False)[:3000])
        self.assertTrue([e for e in self.store.list('device_evidence') if e['session_id'] == s['id']])
        self.assertEqual(s['metering_status'], 'cloud_settled')
        self.assert_local_ledger_untouched()

    # ---------------------------------------------- Q3-03 / Q3-04 (iter03 review)
    def scan_for(self, secret):
        """Every file under the desktop data dir (store, runtime-home, workspace) holding ``secret``."""
        hits = []
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file() and not path.is_symlink():
                try:
                    if secret.encode() in path.read_bytes():
                        hits.append(str(path.relative_to(self.tmp.name)))
                except OSError:
                    pass
        return hits

    def runtime_home(self, s):
        return self.store.root / 'engineering' / s['id'] / 'runtime-home'

    def test_session_token_is_never_written_to_disk_by_the_real_runtime(self):
        self.login()
        tokens = []
        original = self.platform.cloud.open_gateway_session

        def spy(*args):
            grant = original(*args)
            tokens.append(grant['access_token'])
            return grant
        with patch.object(self.platform.cloud, 'open_gateway_session', spy):
            s = self.wait(self.work(self.cloud_model())['id'], timeout=60)
        self.assertEqual(s['status'], 'completed', json.dumps(s, ensure_ascii=False)[:2000])
        self.assertEqual(len(tokens), 1)
        self.assertEqual(self.scan_for(tokens[0]), [])
        self.assertNotIn(tokens[0], json.dumps(self.platform.snapshot(), ensure_ascii=False))
        home = self.runtime_home(s)
        self.assertFalse((home / 'shell_snapshots').exists() and any((home / 'shell_snapshots').iterdir()))
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_own_provider_key_is_never_written_to_disk_by_the_real_runtime(self):
        # billing=provider: the user's own API key takes the same path into the
        # runtime. Point it at a cloud session so the run really completes.
        self.login()
        grant = self.platform.cloud.open_gateway_session('own-key-run', 'echo-code', 200_000, 5)
        own = self.engine.model_add({'project_id': self.pid, 'name': 'Own', 'model': 'echo-code',
                                     'base_url': grant['base_url'], 'api_key': grant['access_token']})
        s = self.wait(self.work(own)['id'], timeout=60)
        self.assertEqual(s['status'], 'completed', json.dumps(s, ensure_ascii=False)[:2000])
        hits = [h for h in self.scan_for(grant['access_token']) if not h.endswith(('registry.sqlite3', '-wal'))]
        self.assertEqual(hits, [])   # only the 0600 store keeps the user's key
        self.platform.cloud.close_gateway_session(grant['session_id'], 'close-own-key-run')

    def test_runtime_home_stays_small_and_no_plugin_marketplace_is_cloned(self):
        self.login()
        s = self.wait(self.work(self.cloud_model())['id'], timeout=60)
        self.assertEqual(s['status'], 'completed')
        home = self.runtime_home(s)
        self.assertFalse((home / '.tmp' / 'plugins').exists(), sorted(str(p) for p in (home / '.tmp').rglob('*'))[:20]
                         if (home / '.tmp').exists() else None)
        self.assertFalse(list(home.rglob('*.pack')))
        size = sum(p.stat().st_size for p in home.rglob('*') if p.is_file() and not p.is_symlink())
        self.assertLess(size, 5 * 1024 * 1024, size)

    def test_idle_app_server_does_not_sync_plugins(self):
        # The reviewer saw the ~25 MB marketplace clone appear within 4 s of
        # start-up; check while the process is still alive (before the scrub).
        home, work = Path(self.tmp.name) / 'probe-home', Path(self.tmp.name) / 'probe-work'
        process = agent_runtime.AgentProcess(home, work, {'model': 'm', 'base_url': 'http://127.0.0.1:9/v1',
                                                          'api_key': 'probe-secret-key-0123456789'}, engineering=True)
        try:
            time.sleep(6)
            self.assertFalse((home / '.tmp' / 'plugins').exists())
            size = sum(p.stat().st_size for p in home.rglob('*') if p.is_file() and not p.is_symlink())
            self.assertLess(size, 5 * 1024 * 1024, size)
            config = (home / 'config.toml').read_text()
            for line in ('shell_snapshot = false', 'plugins = false', 'remote_plugin = false'):
                self.assertIn(line, config)
        finally:
            process.close()
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_overage_from_the_real_runtime_is_explained(self):
        # The real runtime sees the over-reported usage first and hits the
        # observation limit; the cloud's overage verdict still decides the message.
        self.login()
        s = self.wait(self.work(self.cloud_model('echo-overrun'))['id'], timeout=60)
        self.assertIn('超过了预留', s['error'] or '', json.dumps(s, ensure_ascii=False)[:3000])
        self.assertTrue(s['error_details'].startswith('usage_exceeds_reservation'), s['error_details'])
        self.assertNotIn('no active turn', s['error_details'])
        self.assertEqual(s['cloud_session']['metering']['overage_calls'], 1)
        self.assertEqual(s['metering_status'], 'cloud_settled')
        self.assertTrue(any('费用以云平台计量为准' in e['message'] for e in s['events']))
        self.assert_local_ledger_untouched()

    def test_small_budget_error_from_the_real_runtime_is_explained(self):
        self.login()
        model = self.cloud_model()
        with patch.object(engineering, 'CLOUD_MIN_TOKENS', 1000):
            s = self.wait(self.work(model, tokens=30_000)['id'], timeout=60)
        self.assertEqual(s['status'], 'failed')
        self.assertIn('云端会话剩余预算不足', s['error'])
        self.assertIn('402', s['error_details'])
        self.assertEqual(s['cloud_session']['metering']['credits_charged'], 0)



@unittest.skipIf(cloud_fixture is None, SKIP)
@unittest.skipUnless(agent_runtime.binary_path(), 'Packaged agent binary required (or NEUXMIND_TEST_AGENT_BINARY)')
class RealRuntimeOversizedHistoryTest(CloudEngineeringBase):
    """Review Q3-07: the cloud refuses a session call whose body is above its
    limit (413); the desktop says why and what to do. The limit is set below a
    single Codex request here so the first call already hits it."""

    @classmethod
    def setUpClass(cls):
        cls.cloud = cloud_fixture.CloudServer(models=cls.models, session_max_body_bytes=20_000)

    def test_payload_too_large_is_explained(self):
        self.login()
        s = self.wait(self.work(self.cloud_model())['id'], timeout=60)
        self.assertEqual(s['status'], 'failed', json.dumps(s, ensure_ascii=False)[:2000])
        self.assertIn('会话历史过长', s['error'])
        self.assertIn('新建工程任务', s['error'])
        self.assertIn('413', s['error_details'])
        self.assertEqual(s['cloud_session']['metering']['credits_charged'], 0)
        self.assert_local_ledger_untouched()


if __name__ == '__main__':
    unittest.main()
