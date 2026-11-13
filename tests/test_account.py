"""账号：邮箱验证码注册、登录、记住登录、忘记密码重置、改名、改密码（会退出其他登录）、/api/me。"""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

from brainos import auth
from brainos.auth import Identity, SESSION_REMEMBER_S, SESSION_SHORT_S


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.idn = Identity(Path(self.tmp.name))

    def tearDown(self):
        self.idn.close()
        self.tmp.cleanup()

    def test_register_login_and_validation(self):
        token, user, life = self.idn.sign_in({'email': ' ZY@Example.com ', 'password': 'password-123', 'name': '测试用户'}, register=True)
        self.assertEqual(user['email'], 'zy@example.com')
        self.assertEqual(life, SESSION_REMEMBER_S)
        self.assertIsNotNone(user['created_at'])
        self.assertEqual(self.idn.user(token)['name'], '测试用户')
        with self.assertRaisesRegex(ValueError, '已注册'):
            self.idn.sign_in({'email': 'zy@example.com', 'password': 'password-123', 'name': 'x'}, register=True)
        with self.assertRaisesRegex(ValueError, '不正确'):
            self.idn.sign_in({'email': 'zy@example.com', 'password': 'wrong-pass-1'})
        _, _, short = self.idn.sign_in({'email': 'zy@example.com', 'password': 'password-123', 'remember': False})
        self.assertEqual(short, SESSION_SHORT_S)
        for bad in ({'email': 'nope', 'password': 'password-123'}, {'email': 'a@b.c', 'password': 'short'}):
            with self.assertRaises(ValueError):
                self.idn.sign_in(bad)
        with self.assertRaisesRegex(ValueError, '名称'):
            self.idn.sign_in({'email': 'b@b.c', 'password': 'password-123', 'name': '  '}, register=True)
        self.assertEqual(self.idn.authenticate({'email': 'zy@example.com', 'password': 'password-123'})[1]['email'], 'zy@example.com')

    def test_profile_and_password_change_signs_out_others(self):
        t1, user, _ = self.idn.sign_in({'email': 'a@b.c', 'password': 'password-123', 'name': 'A'}, register=True)
        t2, _, _ = self.idn.sign_in({'email': 'a@b.c', 'password': 'password-123'})
        self.assertEqual(self.idn.update_profile(user['id'], {'name': ' 新名字 '})['name'], '新名字')
        with self.assertRaisesRegex(ValueError, '当前密码不正确'):
            self.idn.change_password(user['id'], t1, {'current_password': 'nope-nope-1', 'new_password': 'newpass-4567'})
        with self.assertRaisesRegex(ValueError, '不能与当前密码相同'):
            self.idn.change_password(user['id'], t1, {'current_password': 'password-123', 'new_password': 'password-123'})
        r = self.idn.change_password(user['id'], t1, {'current_password': 'password-123', 'new_password': 'newpass-4567'})
        self.assertEqual(r['signed_out_sessions'], 1)
        self.assertIsNotNone(self.idn.user(t1))
        self.assertIsNone(self.idn.user(t2))
        with self.assertRaises(ValueError):
            self.idn.sign_in({'email': 'a@b.c', 'password': 'password-123'})
        self.assertTrue(self.idn.sign_in({'email': 'a@b.c', 'password': 'newpass-4567'})[0])

    def test_registration_needs_the_emailed_code(self):
        form = {'email': 'c@d.e', 'password': 'password-123', 'name': 'C'}
        email, code = self.idn.start_registration(form)
        self.assertEqual((email, len(code)), ('c@d.e', 6))
        with self.assertRaisesRegex(ValueError, '验证码'):
            self.idn.sign_in(form, register=True, require_code=True)
        wrong = '000000' if code != '000000' else '111111'
        with self.assertRaisesRegex(ValueError, '验证码不正确'):
            self.idn.sign_in({**form, 'code': wrong}, register=True, require_code=True)
        with self.assertRaisesRegex(ValueError, '秒后'):
            self.idn.start_registration(form)
        token, user, _ = self.idn.sign_in({**form, 'code': code}, register=True, require_code=True)
        self.assertEqual(self.idn.user(token)['email'], 'c@d.e')
        with self.assertRaisesRegex(ValueError, '已注册'):
            self.idn.start_registration(form)

    def test_code_expiry_attempt_limit_and_hourly_cap(self):
        form = {'email': 'x@y.z', 'password': 'password-123', 'name': 'X'}
        _, code = self.idn.start_registration(form)
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(auth.CODE_MAX_ATTEMPTS):
            with self.assertRaises(ValueError):
                self.idn.sign_in({**form, 'code': wrong}, register=True, require_code=True)
        with self.assertRaisesRegex(ValueError, '次数过多'):
            self.idn.sign_in({**form, 'code': code}, register=True, require_code=True)
        now = time.time()
        with mock.patch('brainos.auth.time.time', return_value=now + auth.CODE_TTL_S + 1):
            with self.assertRaisesRegex(ValueError, '过期'):
                self.idn.sign_in({**form, 'code': code}, register=True, require_code=True)
        for i in range(1, auth.CODE_HOURLY_MAX):
            with mock.patch('brainos.auth.time.time', return_value=now + i * (auth.CODE_RESEND_S + 1)):
                self.idn.start_registration(form)
        with mock.patch('brainos.auth.time.time', return_value=now + auth.CODE_HOURLY_MAX * (auth.CODE_RESEND_S + 1)):
            with self.assertRaisesRegex(ValueError, '一小时'):
                self.idn.start_registration(form)

    def test_password_reset_with_code_signs_out_everywhere(self):
        t1, user, _ = self.idn.sign_in({'email': 'r@s.t', 'password': 'password-123', 'name': 'R'}, register=True)
        self.assertEqual(self.idn.request_reset({'email': 'nobody@s.t'}), ('nobody@s.t', None))
        email, code = self.idn.request_reset({'email': ' R@S.T '})
        self.assertEqual(email, 'r@s.t')
        with self.assertRaisesRegex(ValueError, '验证码不正确'):
            self.idn.reset_password({'email': 'r@s.t', 'code': '000000' if code != '000000' else '111111', 'password': 'brand-new-pass-1'})
        token, u, _ = self.idn.reset_password({'email': 'r@s.t', 'code': code, 'password': 'brand-new-pass-1'})
        self.assertEqual(u['id'], user['id'])
        self.assertIsNone(self.idn.user(t1))
        self.assertIsNotNone(self.idn.user(token))
        with self.assertRaisesRegex(ValueError, '过期'):
            self.idn.reset_password({'email': 'r@s.t', 'code': code, 'password': 'another-pass-1'})
        with self.assertRaises(ValueError):
            self.idn.sign_in({'email': 'r@s.t', 'password': 'password-123'})
        self.assertTrue(self.idn.sign_in({'email': 'r@s.t', 'password': 'brand-new-pass-1'})[0])

    def test_new_password_policy_and_login_leniency(self):
        for bad, why in (('abcdefghijk', '字母和数字'), ('12345678901', '字母和数字'), ('short1a', '10 个字符'),
                         ('Password123', '太常见'), ('tester12345', '不能与邮箱相同')):
            with self.assertRaisesRegex(ValueError, why):
                self.idn.start_registration({'email': 'tester12345@x.io', 'password': bad, 'name': 'T'})
        for bad_email in ('a@b', 'a@@b.c', '@b.c', 'a@b.', 'a b@c.d', 'a@.c'):
            with self.assertRaisesRegex(ValueError, '有效的邮箱'):
                self.idn.start_registration({'email': bad_email, 'password': 'good-pass-123', 'name': 'T'})
        with self.assertRaisesRegex(ValueError, '不正确'):  # sign-in does not apply the new-password policy
            self.idn.sign_in({'email': 'nobody@x.io', 'password': 'short'})

    def test_builtin_test_account(self):
        from brainos.auth import TEST_EMAIL, TEST_PASSWORD
        self.idn.enable_test_account(True)
        self.idn.enable_test_account(True)  # idempotent
        token, user, _ = self.idn.sign_in({'email': TEST_EMAIL, 'password': TEST_PASSWORD})
        self.assertEqual(self.idn.user(token)['email'], TEST_EMAIL)
        self.idn.enable_test_account(False)
        with self.assertRaisesRegex(ValueError, '测试账号已停用'):
            self.idn.sign_in({'email': TEST_EMAIL, 'password': TEST_PASSWORD})

    def test_old_database_gets_new_columns(self):
        import sqlite3
        d = Path(self.tmp.name) / 'old'
        d.mkdir()
        db = sqlite3.connect(d / 'identity.sqlite3')
        db.execute('CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, salt TEXT, password TEXT)')
        db.execute("INSERT INTO users VALUES ('u1','old@x.y','旧用户','00','00')")
        db.commit(); db.close()
        idn = Identity(d)
        self.assertEqual(idn._user_by_id('u1')['name'], '旧用户')
        idn.close()


class AccountHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from brainos.server import Application, make_handler
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.tmp.name), '')
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.app))
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if hasattr(cls.app, 'close'):
            cls.app.close()
        cls.tmp.cleanup()

    def call(self, path, payload=None, cookie=''):
        host = f'127.0.0.1:{self.port}'
        req = urllib.request.Request(f'http://{host}/api/{path}', data=None if payload is None else json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json', 'Host': host, 'Cookie': cookie})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers.get('Set-Cookie', '')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), ''

    def test_flow(self):
        self.assertEqual(self.call('me')[1], {'user': None})
        form = {'email': 'h@t.p', 'password': 'password-123', 'name': 'H'}
        self.assertEqual(self.call('register', form)[0], 400)  # no code, no account
        with mock.patch.dict(os.environ, {'NEUXMIND_MAIL_DEV_ECHO': '1'}):
            st, sent, _ = self.call('register/start', form)
        self.assertEqual((st, sent['delivery'], sent['resend_after']), (200, 'outbox', 60))
        outbox = Path(sent['outbox']).expanduser()
        self.assertIn(sent['dev_code'], outbox.read_text())
        self.assertEqual(outbox.stat().st_mode & 0o777, 0o600)
        st, body, set_cookie = self.call('register', {**form, 'code': sent['dev_code']})
        self.assertEqual(st, 200)
        self.assertIn(f'Max-Age={SESSION_REMEMBER_S}', set_cookie)
        c = SimpleCookie(); c.load(set_cookie); cookie = 'brain_session=' + c['brain_session'].value
        st, me, _ = self.call('me', cookie=cookie)
        self.assertEqual(me['user']['email'], 'h@t.p')
        self.assertEqual(self.call('account/profile', {'name': 'H2'}, cookie)[1]['user']['name'], 'H2')
        self.assertEqual(self.call('account/profile', {'name': 'x'})[0], 401)
        self.assertEqual(self.call('account/password', {'current_password': 'password-123', 'new_password': 'password-456'}, cookie)[0], 200)
        self.assertEqual(self.call('logout', {}, cookie)[0], 200)
        self.assertEqual(self.call('me', cookie=cookie)[1], {'user': None})
        _, _, short = self.call('login', {'email': 'h@t.p', 'password': 'password-456', 'remember': False})
        self.assertIn(f'Max-Age={SESSION_SHORT_S}', short)

    def test_forgot_password_does_not_reveal_accounts(self):
        st, unknown, _ = self.call('password/forgot', {'email': 'ghost@t.p'})
        self.assertEqual(st, 200)
        self.assertNotIn('dev_code', unknown)  # dev echo is off here; and unknown addresses never get a code
        with mock.patch.dict(os.environ, {'NEUXMIND_MAIL_DEV_ECHO': '1'}):
            form = {'email': 'f@t.p', 'password': 'password-123', 'name': 'F'}
            self.call('register', {**form, 'code': self.call('register/start', form)[1]['dev_code']})
            st, known, _ = self.call('password/forgot', {'email': 'f@t.p'})
            _, ghost, _ = self.call('password/forgot', {'email': 'ghost2@t.p'})
        self.assertEqual(set(known) - {'dev_code'}, set(ghost))
        self.assertNotIn('dev_code', ghost)
        st, body, set_cookie = self.call('password/reset', {'email': 'f@t.p', 'code': known['dev_code'], 'password': 'reset-pass-789'})
        self.assertEqual(st, 200)
        self.assertIn('brain_session=', set_cookie)
        self.assertEqual(self.call('login', {'email': 'f@t.p', 'password': 'reset-pass-789'})[0], 200)

    def test_test_account_signs_in_over_http_without_mail_server(self):
        from brainos.auth import TEST_EMAIL, TEST_PASSWORD
        st, body, _ = self.call('login', {'email': TEST_EMAIL, 'password': TEST_PASSWORD})
        self.assertEqual((st, body['user']['email']), (200, TEST_EMAIL))

    def test_smtp_settings_from_state_dir(self):
        from brainos.mailer import Mailer
        d = Path(self.tmp.name) / 'mailcfg'
        d.mkdir()
        m = Mailer(d)
        self.assertEqual(m.mode, 'outbox')
        (d / 'mail.json').write_text(json.dumps({'host': 'smtp.example.test', 'port': 465, 'user': 'no-reply@example.test', 'password': 'x'}))
        cfg = m.config()
        self.assertEqual((m.mode, cfg['ssl'], cfg['from']), ('smtp', True, 'no-reply@example.test'))
        with mock.patch.dict(os.environ, {'NEUXMIND_MAIL_DEV_ECHO': '1'}):
            self.assertFalse(m.dev_echo())  # never echo codes when real mail is configured

if __name__ == '__main__':
    unittest.main()
