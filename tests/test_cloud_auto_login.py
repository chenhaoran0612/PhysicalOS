"""Issue #36: signing into the app signs into the cloud (the material library) too.

The app account and the cloud account are two e-mail + password accounts. With the automatic sign-in on,
the app uses the e-mail and password of an app sign-in for the cloud as well, at that moment only, and
registers the cloud account when there is none. A session from before this version has no cloud sign-in
and no password to make one with, so it is signed out once and the user signs in again.

The cloud here is the real cloud application (test environment) on a loopback port.
"""
import hashlib
import http.client
import json
import os
import secrets
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response

from brainos import cloud_account, cloud_auto_login  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from round02_support import CloudServer  # noqa: E402

LONG = 30 * 24 * 3600
SHORT = 12 * 3600


def throwaway():
    """A password made at run time: at least 10 characters with a letter and a digit."""
    return 'pw1-' + secrets.token_hex(8)


def dead_url():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return f'http://127.0.0.1:{s.getsockname()[1]}'


class Case(unittest.TestCase):
    auto_login = True

    @classmethod
    def setUpClass(cls):
        cls.cloud = CloudServer()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.default_url = patch.object(cloud_account, 'DEFAULT_BASE_URL', self.cloud.url)
        self.default_url.start()
        self.app = Application(self.tmp.name)
        self.app.cloud_auto_login = self.auto_login
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.email = f'u{secrets.token_hex(4)}@example.test'
        self.password = throwaway()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.app.close()
        self.default_url.stop()
        self.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    def request(self, path, payload=None, cookie=''):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        headers = {'Cookie': cookie}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None, headers)
        r = conn.getresponse()
        result = (r.status, json.loads(r.read()), r.getheader('Set-Cookie'))
        conn.close()
        return result

    def app_register(self, **extra):
        form = {'name': 'Wen', 'email': self.email, 'password': self.password, **extra}
        code = self.request('/api/register/start', form)[1]['dev_code']
        status, body, cookie = self.request('/api/register', {**form, 'code': code})
        self.assertEqual(status, 200, body)
        return cookie.split(';')[0]

    def app_login(self, **extra):
        status, body, cookie = self.request('/api/login', {'email': self.email, 'password': self.password, **extra})
        self.assertEqual(status, 200, body)
        return cookie.split(';')[0]

    def cloud_register(self, password=None, verify=True):
        status, body = self.cloud.api('POST', '/auth/register', {'email': self.email,
                                                                 'password': password or self.password})
        self.assertEqual(status, 201, body)
        if verify:
            self.cloud.api('POST', '/auth/verify-email', {'token': body['dev_verification_token']}, body['token'])
        return body

    def cloud_view(self, cookie):
        status, state, _ = self.request('/api/state', cookie=cookie)
        self.assertEqual(status, 200, state)
        return state['cloud_account']

    def workspace(self, cookie):
        return self.app.workspace(self.app.identity.user(cookie.split('=', 1)[1]))

    def cloud_session_lifetime(self, cookie):
        from neuxmind_cloud.auth import now, token_hash
        from neuxmind_cloud.models import AuthSession
        token = self.workspace(cookie).cloud._token()
        with self.cloud.app.state.ctx.db.transaction() as s:
            return s.get(AuthSession, token_hash(token)).expires_at - now()

    def expire_cloud_sessions(self):
        from sqlalchemy import update
        from neuxmind_cloud.models import AuthSession
        with self.cloud.app.state.ctx.db.transaction() as s:
            s.execute(update(AuthSession).values(expires_at=0))


class SignInTest(Case):
    def test_app_sign_in_signs_into_the_cloud_account_with_the_same_password(self):
        self.cloud_register()

        cookie = self.app_register()

        view = self.cloud_view(cookie)
        self.assertTrue(view['logged_in'])
        self.assertEqual(view['email'], self.email)
        self.assertTrue(view['email_verified'])
        self.assertEqual(view['auto_login']['status'], 'logged_in')
        self.assertFalse(view['auto_login']['relogin'])

    def test_a_missing_cloud_account_is_registered_and_verified_by_the_development_cloud(self):
        cookie = self.app_register()

        view = self.cloud_view(cookie)
        self.assertTrue(view['logged_in'])
        self.assertEqual(view['email'], self.email)
        self.assertEqual(view['name'], 'Wen')
        self.assertTrue(view['email_verified'])
        self.assertEqual(view['auto_login']['status'], 'registered')

    def test_a_later_app_login_signs_into_the_cloud_again(self):
        first = self.app_register()
        self.request('/api/logout', {}, first)
        self.expire_cloud_sessions()

        cookie = self.app_login()

        view = self.cloud_view(cookie)
        self.assertTrue(view['logged_in'])
        self.assertEqual(view['auto_login']['status'], 'logged_in')
        self.assertEqual(self.request('/api/me', cookie=cookie)[1]['user']['email'], self.email)

    def test_keep_me_signed_in_makes_the_cloud_session_last_as_long_as_the_app_session(self):
        self.app_register()

        remembered = self.app_login(remember=True)
        self.assertAlmostEqual(self.cloud_session_lifetime(remembered), LONG, delta=120)

        short = self.app_login(remember=False)
        self.assertAlmostEqual(self.cloud_session_lifetime(short), SHORT, delta=120)

    def test_a_cloud_account_with_another_password_is_left_alone_and_the_reason_is_shown(self):
        self.cloud_register(password=throwaway())

        cookie = self.app_register()

        view = self.cloud_view(cookie)
        self.assertFalse(view['logged_in'])
        self.assertEqual(view['auto_login']['status'], 'password_mismatch')
        self.assertIn('密码', view['auto_login']['message'])
        self.assertFalse(view['auto_login']['relogin'])

    def test_an_unverified_cloud_account_with_another_password_is_reported_not_claimed(self):
        self.cloud_register(password=throwaway(), verify=False)

        cookie = self.app_register()

        view = self.cloud_view(cookie)
        self.assertFalse(view['logged_in'])
        self.assertIn(view['auto_login']['status'], ('claim_sent', 'password_mismatch'))
        self.assertTrue(view['auto_login']['message'])

    def test_the_app_sign_in_succeeds_when_no_cloud_answers(self):
        with patch.object(cloud_account, 'DEFAULT_BASE_URL', dead_url()):
            cookie = self.app_register()

            view = self.cloud_view(cookie)
        self.assertFalse(view['logged_in'])
        self.assertFalse(view['configured'])
        self.assertEqual(view['auto_login']['status'], 'cloud_unreachable')

    def test_a_cloud_account_the_user_signed_into_by_hand_is_kept(self):
        cookie = self.app_register()
        other, other_password = f'o{secrets.token_hex(4)}@example.test', throwaway()
        self.assertEqual(self.request('/api/cloud/logout', {}, cookie)[0], 200)
        status, body, _ = self.request('/api/cloud/register', {'email': other, 'password': other_password}, cookie)
        self.assertEqual(status, 200, body)

        again = self.app_login()

        view = self.cloud_view(again)
        self.assertTrue(view['logged_in'])
        self.assertEqual(view['email'], other)
        self.assertEqual(view['auto_login']['status'], 'manual')

    def test_the_password_and_the_cloud_token_are_not_stored_or_shown(self):
        cookie = self.app_register()
        token = self.workspace(cookie).cloud._token()
        self.assertTrue(token)

        state = json.dumps(self.request('/api/state', cookie=cookie)[1])
        self.assertNotIn(self.password, state)
        self.assertNotIn(token, state)
        for path in Path(self.tmp.name).rglob('*'):
            if not path.is_file():
                continue
            data = path.read_bytes()
            self.assertNotIn(self.password.encode(), data, path)
            if path.name != cloud_account.CREDENTIAL_FILE:
                self.assertNotIn(token.encode(), data, path)

    def test_password_reset_tries_the_cloud_with_the_new_password(self):
        self.app_register()
        new = throwaway()
        code = self.request('/api/password/forgot', {'email': self.email})[1]['dev_code']

        status, body, cookie = self.request('/api/password/reset', {'email': self.email, 'code': code, 'password': new})

        self.assertEqual(status, 200, body)
        view = self.cloud_view(cookie.split(';')[0])
        # The cloud account still has the old password: the app cannot change it, and says so.
        self.assertEqual(view['auto_login']['status'], 'password_mismatch')


class OldSessionTest(Case):
    def old_session(self):
        """A session made before this version: the automatic sign-in did not exist."""
        self.app.cloud_auto_login = False
        cookie = self.app_register()
        self.app.cloud_auto_login = True
        return cookie

    def cloud_sign_in_by_hand(self, cookie):
        """What the user did on the cloud account page of the old version."""
        for path, payload in (('/api/cloud/configure', {'base_url': self.cloud.url}),
                              ('/api/cloud/register', {'email': self.email, 'password': self.password})):
            status, body, _ = self.request(path, payload, cookie)
            self.assertEqual(status, 200, body)
        self.assertTrue(self.cloud_view(cookie)['logged_in'])

    def test_a_session_from_before_this_version_is_signed_out_once(self):
        cookie = self.old_session()

        status, body, set_cookie = self.request('/api/me', cookie=cookie)

        self.assertEqual(status, 200)
        self.assertIsNone(body['user'])
        self.assertEqual(body['signed_out']['code'], 'cloud_login_required')
        self.assertIn('重新登录', body['signed_out']['message'])
        self.assertIn('Max-Age=0', set_cookie)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)

        fresh = self.app_login()
        me = self.request('/api/me', cookie=fresh)[1]
        self.assertEqual(me['user']['email'], self.email)
        self.assertNotIn('signed_out', me)
        self.assertTrue(self.cloud_view(fresh)['logged_in'])

    def test_an_old_session_is_kept_while_no_cloud_answers(self):
        cookie = self.old_session()

        with patch.object(cloud_account, 'DEFAULT_BASE_URL', dead_url()):
            body = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(body['user']['email'], self.email)
        # ... and is asked again once a cloud is there.
        self.assertIsNone(self.request('/api/me', cookie=cookie)[1]['user'])

    def test_an_old_session_whose_cloud_sign_in_still_works_is_kept(self):
        cookie = self.old_session()
        self.cloud_sign_in_by_hand(cookie)

        me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        self.assertTrue(self.cloud_view(cookie)['logged_in'])

    def test_an_old_session_whose_user_signed_out_of_the_cloud_by_hand_is_kept(self):
        cookie = self.old_session()
        self.cloud_sign_in_by_hand(cookie)
        self.assertEqual(self.request('/api/cloud/logout', {}, cookie)[0], 200)

        me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        self.assertFalse(self.cloud_view(cookie)['logged_in'])

    def test_an_old_session_with_a_dead_cloud_token_is_signed_out(self):
        cookie = self.old_session()
        self.cloud_sign_in_by_hand(cookie)
        self.expire_cloud_sessions()

        body = self.request('/api/me', cookie=cookie)[1]

        self.assertIsNone(body['user'])
        self.assertEqual(body['signed_out']['code'], 'cloud_login_required')


class LaterSessionTest(Case):
    def test_when_the_cloud_sign_in_made_at_login_has_lapsed_the_app_asks_to_sign_in_again(self):
        cookie = self.app_register()
        self.expire_cloud_sessions()

        body = self.request('/api/me', cookie=cookie)[1]

        self.assertIsNone(body['user'])
        self.assertEqual(body['signed_out']['code'], 'cloud_session_expired')

    def test_a_lapsed_cloud_sign_in_noticed_while_working_offers_to_sign_in_again(self):
        cookie = self.app_register()
        self.workspace(cookie).cloud._drop_token()

        view = self.cloud_view(cookie)

        self.assertFalse(view['logged_in'])
        self.assertTrue(view['auto_login']['relogin'])

    def test_a_failed_cloud_sign_in_does_not_sign_the_app_out_again_and_again(self):
        self.cloud_register(password=throwaway())
        cookie = self.app_register()
        self.assertEqual(self.cloud_view(cookie)['auto_login']['status'], 'password_mismatch')

        for _ in range(2):
            self.assertEqual(self.request('/api/me', cookie=cookie)[1]['user']['email'], self.email)

    def test_signing_out_of_the_cloud_by_hand_is_respected(self):
        cookie = self.app_register()
        self.assertEqual(self.request('/api/cloud/logout', {}, cookie)[0], 200)

        me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        view = self.cloud_view(cookie)
        self.assertFalse(view['logged_in'])
        self.assertEqual(view['auto_login']['status'], 'signed_out_by_hand')
        self.assertFalse(view['auto_login']['relogin'])

    def test_after_an_app_password_change_a_lapsed_cloud_sign_in_does_not_sign_the_app_out(self):
        cookie = self.app_register()
        status, body, _ = self.request('/api/account/password', {'current_password': self.password,
                                                                 'new_password': throwaway()}, cookie)
        self.assertEqual(status, 200, body)
        self.assertTrue(self.cloud_view(cookie)['logged_in'])       # the cloud session it had stays

        self.expire_cloud_sessions()
        me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        view = self.cloud_view(cookie)
        self.assertEqual(view['auto_login']['status'], 'password_mismatch')
        self.assertFalse(view['auto_login']['relogin'])
        self.assertIn('密码', view['auto_login']['message'])

    def test_a_cloud_that_only_gives_short_sessions_does_not_sign_the_app_out_at_every_start(self):
        settings = self.cloud.app.state.ctx.settings
        with patch.object(settings, 'session_remember_ttl_seconds', SHORT):     # a cloud from before ``remember``
            cookie = self.app_register()
        self.assertTrue(self.cloud_view(cookie)['logged_in'])
        self.expire_cloud_sessions()

        me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        self.assertTrue(self.cloud_view(cookie)['auto_login']['relogin'])   # the page offers to sign in again

    def test_a_cloud_that_does_not_answer_at_start_up_changes_nothing(self):
        cookie = self.app_register()
        workspace = self.workspace(cookie)

        with patch.object(workspace.cloud, '_request', side_effect=cloud_account.CloudError('无法连接云平台')):
            me = self.request('/api/me', cookie=cookie)[1]

        self.assertEqual(me['user']['email'], self.email)
        self.assertTrue(self.cloud_view(cookie)['logged_in'])

    def test_session_marks_are_kept_as_hashes_and_die_with_the_session(self):
        cookie = self.app_register()
        token = cookie.split('=', 1)[1]
        rows = [r[0] for r in self.app.identity.db.execute('SELECT hash FROM cloud_login_sessions')]
        self.assertEqual(rows, [hashlib.sha256(token.encode()).hexdigest()])

        self.request('/api/logout', {}, cookie)
        self.app_login()

        rows = [r[0] for r in self.app.identity.db.execute('SELECT hash FROM cloud_login_sessions')]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), rows)


class SwitchedOffTest(Case):
    auto_login = False

    def test_without_the_switch_an_app_sign_in_does_not_touch_the_cloud(self):
        cookie = self.app_register()

        view = self.cloud_view(cookie)
        self.assertFalse(view['logged_in'])
        self.assertFalse(view['configured'])
        self.assertIsNone(view['auto_login'])
        self.assertEqual(self.request('/api/me', cookie=cookie)[1]['user']['email'], self.email)
        self.assertEqual(self.cloud.api('POST', '/auth/login', {'email': self.email, 'password': self.password})[0], 401)


class SwitchTest(unittest.TestCase):
    def test_a_new_application_has_it_off_so_test_servers_never_reach_a_real_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Application(tmp)
            try:
                self.assertFalse(app.cloud_auto_login)
            finally:
                app.close()

    def test_the_launcher_turns_it_on_unless_told_not_to(self):
        self.assertTrue(cloud_auto_login.enabled_by_environment({}))
        self.assertTrue(cloud_auto_login.enabled_by_environment({'NEUXMIND_CLOUD_AUTO_LOGIN': '1'}))
        self.assertFalse(cloud_auto_login.enabled_by_environment({'NEUXMIND_CLOUD_AUTO_LOGIN': '0'}))


if __name__ == '__main__':
    unittest.main()
