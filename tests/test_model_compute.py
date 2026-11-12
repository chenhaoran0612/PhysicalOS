"""Models & compute (issue #4): TokenFans as the workspace's default LLM. Uses a local stand-in, never the paid API."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tokenfans_fixture as tkf
from brainos.service import Platform


def workspace_files(root):
    return [p for p in Path(root).rglob('*') if p.is_file()]


class ModelComputeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tkf.TokenFansFixture()
        self.env = patch.dict(os.environ, {'NEUXMIND_TOKENFANS_URL': self.fixture.url})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name + '/platform'
        self.app = Platform(self.root)
        self.mc = self.app.model_compute
        self.pid = self.app.add_project({'name': 'Before', 'objective': 'Existing project'})['id']

    def tearDown(self):
        self.app.close()
        self.env.stop()
        self.fixture.close()
        self.tmp.cleanup()

    def tokenfans_profiles(self):
        return [m for m in self.app.store.list('agent_models') if m.get('billing') == 'tokenfans']

    def snapshot_text(self):
        return json.dumps(self.app.snapshot(), ensure_ascii=False)

    def assert_secret_only_in_credentials(self, secret):
        hits = [p for p in workspace_files(self.root) if secret.encode() in p.read_bytes()]
        self.assertEqual(hits, [Path(self.root) / 'credentials' / 'tokenfans.json'])

    def assert_nowhere(self, secret):
        hits = [p for p in workspace_files(self.root) if secret.encode() in p.read_bytes()]
        self.assertEqual(hits, [])

    # ------------------------------------------------------------ connecting
    def test_connect_with_key_lists_models_and_never_exposes_key(self):
        summary = self.mc.connect_key({'api_key': '  ' + tkf.API_KEY + '\n'})
        self.assertTrue(summary['connected'])
        self.assertEqual(summary['models'], tkf.MODELS)
        self.assertEqual(summary['default_model'], 'gpt-5.5-codex')
        self.assertEqual(summary['engine_base_url'], self.fixture.url + '/codex/v1')
        self.assertNotIn(tkf.API_KEY, json.dumps(summary))
        self.assertEqual(summary['key_hint'], 'sk-tkf-…' + tkf.API_KEY[-4:])
        # The key was verified against the Codex model catalog with a Bearer header.
        calls = [r for r in self.fixture.requests if r['path'] == '/codex/v1/models']
        self.assertEqual(calls[-1]['authorization'], 'Bearer ' + tkf.API_KEY)
        # Stored only in the 600 credential file, never in the registry or the snapshot.
        cred = Path(self.root) / 'credentials' / 'tokenfans.json'
        self.assertEqual(stat.S_IMODE(cred.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cred.parent.stat().st_mode), 0o700)
        self.assertNotIn(tkf.API_KEY, self.snapshot_text())
        self.assert_secret_only_in_credentials(tkf.API_KEY)
        self.assertEqual(self.app.snapshot()['model_compute']['default_model'], 'gpt-5.5-codex')

    def test_rejected_key_is_not_saved(self):
        with self.assertRaisesRegex(ValueError, 'API Key'):
            self.mc.connect_key({'api_key': 'sk-tkf-wrong0123456789abcdef'})
        self.assertFalse(self.mc.summary()['connected'])
        self.assertFalse((Path(self.root) / 'credentials' / 'tokenfans.json').exists())
        self.assertEqual(self.tokenfans_profiles(), [])

    def test_malformed_key_is_refused_before_any_request(self):
        for bad in ('', 'short', 'sk-tkf-has space 0123456789', 'sk-tkf-0123456789\nInjected: 1', 7):
            with self.assertRaises(ValueError):
                self.mc.connect_key({'api_key': bad})
        self.assertEqual(self.fixture.requests, [])

    def test_login_fetches_the_account_key_and_balance_and_keeps_no_password(self):
        summary = self.mc.login({'email': ' Robot@Example.TEST ', 'password': tkf.PASSWORD})
        self.assertTrue(summary['connected'])
        self.assertEqual(summary['email'], tkf.EMAIL)
        self.assertEqual(summary['balance'], {'minor': 12345, 'currency': 'CNY'})
        self.assertTrue(summary['balance_at'])
        login = next(r for r in self.fixture.requests if r['path'] == '/auth/login')
        self.assertEqual(login['body'], {'email': tkf.EMAIL, 'password': tkf.PASSWORD})
        key_call = next(r for r in self.fixture.requests if r['path'] == '/auth/my-key')
        self.assertEqual(key_call['authorization'], 'Bearer ' + tkf.CONSOLE_TOKEN)
        # The console session is ended after the exchange.
        self.assertIn('/auth/logout', self.fixture.paths())
        self.assertEqual(self.mc.api_key(), tkf.API_KEY)
        self.assert_nowhere(tkf.PASSWORD)
        self.assert_nowhere(tkf.CONSOLE_TOKEN)
        self.assert_secret_only_in_credentials(tkf.API_KEY)
        self.assertNotIn(tkf.PASSWORD, self.snapshot_text())

    def test_wrong_password_saves_nothing(self):
        with self.assertRaisesRegex(ValueError, '邮箱或密码不正确'):
            self.mc.login({'email': tkf.EMAIL, 'password': 'not-the-password'})
        self.assertFalse(self.mc.summary()['connected'])
        self.assertFalse((Path(self.root) / 'credentials').exists() and any((Path(self.root) / 'credentials').iterdir()))

    def test_account_without_a_key_is_explained(self):
        with self.assertRaisesRegex(ValueError, 'API Key'):
            self.mc.login({'email': tkf.KEYLESS_EMAIL, 'password': tkf.PASSWORD})
        self.assertFalse(self.mc.summary()['connected'])
        self.assertIn('/auth/logout', self.fixture.paths())

    def test_login_needs_email_and_password(self):
        for p in ({'email': '', 'password': 'x'}, {'email': 'robot@example.test', 'password': ''}, {'email': 'not-an-email', 'password': 'x'}):
            with self.assertRaises(ValueError):
                self.mc.login(p)
        self.assertEqual(self.fixture.requests, [])

    # --------------------------------------------------------- default model
    def test_default_model_follows_every_project(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        later = self.app.add_project({'name': 'After', 'objective': 'Created after connecting'})['id']
        profiles = {m['project_id']: m for m in self.tokenfans_profiles()}
        self.assertEqual(set(profiles), {self.pid, later})
        for m in profiles.values():
            self.assertEqual(m['model'], 'gpt-5.5-codex')
            self.assertEqual(m['base_url'], self.fixture.url + '/codex/v1')
            self.assertEqual(m['protocol'], 'responses')
            self.assertNotIn('api_key', m)
        self.mc.set_default({'model': 'kimi-k2.6'})
        self.assertEqual({m['model'] for m in self.tokenfans_profiles()}, {'kimi-k2.6'})
        self.assertEqual(len(self.tokenfans_profiles()), 2)
        with self.assertRaisesRegex(ValueError, '模型'):
            self.mc.set_default({'model': 'not-listed'})
        self.assertEqual(self.mc.summary()['default_model'], 'kimi-k2.6')

    def test_task_without_a_model_uses_the_tokenfans_default(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        for empty in (None, ''):
            chosen = self.app.engineering.resolve_model(self.pid, empty)
            self.assertEqual(chosen['billing'], 'tokenfans')
        own = self.app.engineering.model_add({'project_id': self.pid, 'name': 'Own', 'model': 'own-model',
                                              'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'own-key-123'})
        self.assertEqual(self.app.engineering.resolve_model(self.pid, own['id'])['id'], own['id'])
        # The engine gets the TokenFans Codex route and the key at run time only.
        session = {'id': 'agent-session-fixture', 'model_id': chosen['id'], 'model': chosen['model']}
        profile, meter = self.app.engineering._model(session)
        self.assertIsNone(meter)
        self.assertEqual(profile, {'model': 'gpt-5.5-codex', 'base_url': self.fixture.url + '/codex/v1', 'api_key': tkf.API_KEY})

    def test_a_session_keeps_its_model_when_the_default_changes(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        chosen = self.app.engineering.resolve_model(self.pid, None)
        session = {'id': 'agent-session-fixture', 'model_id': chosen['id'], 'model': chosen['model']}
        self.mc.set_default({'model': 'kimi-k2.6'})
        self.assertEqual(self.app.engineering._model(session)[0]['model'], 'gpt-5.5-codex')

    def test_workflow_stage_without_a_model_uses_the_default(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        definition = self.app.workflows.create({'project_id': self.pid, 'name': 'Default model flow', 'objective': 'Use the default',
            'stages': [{'name': 'Stage 1', 'role': 'Engineer', 'objective': 'Create the result', 'required_outputs': ['result.txt'],
                        'max_tokens': 1000, 'max_minutes': 1}], 'max_total_tokens': 2000})
        stage = definition['stages'][0]
        self.assertEqual(stage['billing'], 'tokenfans')
        self.assertEqual(stage['model'], 'gpt-5.5-codex')

    def test_without_tokenfans_a_model_must_be_chosen(self):
        with self.assertRaisesRegex(ValueError, 'TokenFans'):
            self.app.engineering.resolve_model(self.pid, None)
        self.assertEqual(self.app.snapshot()['model_compute']['connected'], False)

    # ------------------------------------------------- refresh and disconnect
    def test_refresh_drops_a_default_that_is_no_longer_offered(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        self.fixture.models = ['kimi-k2.6']
        summary = self.mc.refresh_models({})
        self.assertEqual(summary['models'], ['kimi-k2.6'])
        self.assertIsNone(summary['default_model'])
        with self.assertRaisesRegex(ValueError, '默认模型'):
            self.app.engineering.resolve_model(self.pid, None)
        self.assertEqual([m for m in self.app.snapshot()['agent_models'] if m.get('billing') == 'tokenfans'], [])
        self.mc.set_default({'model': 'kimi-k2.6'})
        self.assertEqual(self.app.engineering.resolve_model(self.pid, None)['model'], 'kimi-k2.6')

    def test_disconnect_removes_the_key_and_hides_the_default(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        chosen = self.app.engineering.resolve_model(self.pid, None)
        summary = self.mc.disconnect({})
        self.assertFalse(summary['connected'])
        self.assertFalse((Path(self.root) / 'credentials' / 'tokenfans.json').exists())
        self.assert_nowhere(tkf.API_KEY)
        self.assertEqual([m for m in self.app.snapshot()['agent_models'] if m.get('billing') == 'tokenfans'], [])
        with self.assertRaisesRegex(ValueError, 'TokenFans'):
            self.app.engineering.resolve_model(self.pid, None)
        # A session or workflow that picked TokenFans earlier fails clearly instead of calling with no key.
        with self.assertRaisesRegex(ValueError, 'TokenFans'):
            self.app.engineering.resolve_model(self.pid, chosen['id'])
        with self.assertRaisesRegex(ValueError, 'TokenFans'):
            self.app.engineering._model({'id': 'agent-session-fixture', 'model_id': chosen['id'], 'model': chosen['model']})

    def test_upstream_errors_are_plain_words(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        self.fixture.close()
        with self.assertRaisesRegex(ValueError, '无法连接 TokenFans'):
            self.mc.refresh_models({})
        # The saved connection survives a network failure.
        self.assertTrue(self.mc.summary()['connected'])
        self.fixture = tkf.TokenFansFixture()   # tearDown closes it

    def test_default_llm_is_the_entry_point_for_other_modules(self):
        with self.assertRaisesRegex(ValueError, 'TokenFans'):
            self.mc.default_llm()
        self.mc.connect_key({'api_key': tkf.API_KEY})
        self.assertEqual(self.mc.default_llm(), {'provider': 'tokenfans', 'model': 'gpt-5.5-codex', 'base_url': self.fixture.url + '/v1',
                                                 'engine_base_url': self.fixture.url + '/codex/v1', 'api_key': tkf.API_KEY})
        self.fixture.models = ['kimi-k2.6']
        self.mc.refresh_models({})
        with self.assertRaisesRegex(ValueError, '默认模型'):
            self.mc.default_llm()

    def test_refresh_does_not_resurrect_a_connection_changed_meanwhile(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        real = self.mc._fetch_models
        def disconnect_midway(key):
            models = real(key)
            self.mc.disconnect({})
            return models
        with patch.object(self.mc, '_fetch_models', side_effect=disconnect_midway):
            with self.assertRaisesRegex(ValueError, '变化'):
                self.mc.refresh_models({})
        self.assertNotIn('models', self.app.store.get('settings', 'tokenfans'))

    def test_a_broken_http_response_is_a_connection_error(self):
        import http.client
        with patch('urllib.request.OpenerDirector.open', side_effect=http.client.IncompleteRead(b'')):
            with self.assertRaisesRegex(ValueError, '无法连接 TokenFans'):
                self.mc.connect_key({'api_key': tkf.API_KEY})

    def test_absurdly_nested_json_is_an_invalid_response_not_a_crash(self):
        self.fixture.deep_logout = True
        summary = self.mc.login({'email': tkf.EMAIL, 'password': tkf.PASSWORD})
        self.assertTrue(summary['connected'])        # logout is best effort
        failed = [e for e in self.app.store.list('events') if e['action'] == 'model_compute.logout_failed']
        self.assertIn('有效 JSON', failed[0]['details']['reason'])

    def start_work(self, **kwargs):
        from brainos import agent_runtime
        with patch.object(agent_runtime, 'available', lambda: {'available': True}), \
             patch.object(self.app.engineering, '_launch', lambda *a: None):
            return self.app.engineering.start_work({'project_id': self.pid, 'name': 'Pinned', 'objective': 'Check the model',
                                                    'data_authorized': True, 'max_tokens': 1000, 'max_minutes': 1}, **kwargs)

    def test_a_workflow_stage_keeps_the_model_it_was_defined_with(self):
        self.mc.connect_key({'api_key': tkf.API_KEY})
        self.mc.set_default({'model': 'kimi-k2.6'})
        self.assertEqual(self.start_work(pinned_model='gpt-5.5-codex')['model'], 'gpt-5.5-codex')
        self.assertEqual(self.start_work()['model'], 'kimi-k2.6')
        self.fixture.models = ['kimi-k2.6']
        self.mc.refresh_models({})
        with self.assertRaisesRegex(ValueError, '已不在可用列表中'):
            self.start_work(pinned_model='gpt-5.5-codex')

    def test_a_failed_key_write_leaves_no_temporary_copy(self):
        with patch('os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.mc.connect_key({'api_key': tkf.API_KEY})
        self.assertEqual(list((Path(self.root) / 'credentials').iterdir()), [])

    def test_key_hint_never_shows_more_than_four_characters_of_a_short_key(self):
        from brainos.model_compute import key_hint
        self.assertEqual(key_hint('abcdefghijklmnop'), '…mnop')

    def test_service_address_override_must_be_https_or_loopback(self):
        with patch.dict(os.environ, {'NEUXMIND_TOKENFANS_URL': 'http://tokenfans.example.test'}):
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                self.mc.connect_key({'api_key': tkf.API_KEY})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NEUXMIND_TOKENFANS_URL')
            from brainos import model_compute
            self.assertEqual(model_compute.origin(), 'https://tokenfans.ai')


if __name__ == '__main__':
    unittest.main()
