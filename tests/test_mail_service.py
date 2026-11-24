"""Issue #37: verification codes go out as real, formal e-mails through an SMTP account configured on this computer."""
import contextlib
import io
import json
import os
import secrets
import smtplib
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brainos import mailer as mailer_module
from brainos.mail_templates import verification_mail
from brainos.mailer import MailError, Mailer

SMTP_ENV = ('HOST', 'PORT', 'USER', 'PASSWORD', 'FROM', 'FROM_NAME', 'SSL')


class FakeSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what the mailer did with it."""
    instances = []
    fail = {}

    def __init__(self, host, port, timeout=None, context=None):
        if 'connect' in self.fail:
            raise self.fail['connect']
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self.messages = [], []
        FakeSmtp.instances.append(self)

    def _step(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append('quit')
        return False

    def starttls(self, context=None):
        self._step('starttls')

    def login(self, user, password):
        self.user = user
        self._step('login')

    def noop(self):
        self._step('noop')
        return 250, b'OK'

    def send_message(self, msg):
        self._step('send')
        msg.as_bytes()                      # what a real server would be handed must serialize
        self.messages.append(msg)


class FakeSmtpSsl(FakeSmtp):
    pass


class MailCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        FakeSmtp.instances, FakeSmtp.fail = [], {}
        clean = {k: v for k, v in os.environ.items() if not k.startswith('NEUXMIND_SMTP_') and k != 'NEUXMIND_MAIL_DEV_ECHO'}
        for patcher in (mock.patch.dict(os.environ, clean, clear=True),
                        mock.patch.object(smtplib, 'SMTP', FakeSmtp), mock.patch.object(smtplib, 'SMTP_SSL', FakeSmtpSsl)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self, **over):
        cfg = {'host': 'smtp.example.test', 'port': 587, 'user': 'no-reply@example.test', 'password': secrets.token_hex(4), **over}
        (self.root / 'mail.json').write_text(json.dumps(cfg))
        return cfg


class TemplateTest(unittest.TestCase):
    def test_registration_mail_reads_like_a_real_service_mail(self):
        subject, text, html = verification_mail('register', '048213', 10)
        self.assertEqual(subject, 'NeuxMind 注册验证码：048213')
        for part in (text, html):
            self.assertIn('048213', part)
            self.assertIn('NeuxMind Robot OS', part)
            self.assertIn('10 分钟', part)
            self.assertIn('请勿直接回复', part)
            self.assertIn('如果不是你本人操作', part)
        self.assertTrue(html.lstrip().lower().startswith('<!doctype html>'))
        self.assertIn('lang="zh-CN"', html)
        self.assertNotIn('<script', html.lower())
        self.assertNotIn('http', html.replace('http-equiv', ''))  # nothing is loaded from the network, nothing to click

    def test_reset_mail_says_the_password_is_unchanged_until_the_code_is_used(self):
        subject, text, html = verification_mail('reset', '551200', 10)
        self.assertEqual(subject, 'NeuxMind 重置密码验证码：551200')
        for part in (text, html):
            self.assertIn('重置', part)
            self.assertIn('你的密码不会改变', part)

    def test_unknown_purpose_is_refused(self):
        with self.assertRaises(ValueError):
            verification_mail('other', '000000', 10)


class SendTest(MailCase):
    def test_starttls_submission_sends_a_formal_two_part_message(self):
        self.configure()
        result = Mailer(self.root).send('user@example.org', '主题', '正文 048213', html='<p>正文 048213</p>')
        self.assertEqual(result, {'delivery': 'smtp'})
        (server,) = FakeSmtp.instances
        self.assertNotIsInstance(server, FakeSmtpSsl)
        self.assertEqual((server.host, server.port, server.user), ('smtp.example.test', 587, 'no-reply@example.test'))
        self.assertEqual(server.calls, ['starttls', 'login', 'send', 'quit'])
        (msg,) = server.messages
        self.assertEqual(msg['From'], 'NeuxMind Robot OS <no-reply@example.test>')
        self.assertEqual(msg['To'], 'user@example.org')
        self.assertTrue(msg['Date'])
        self.assertTrue(msg['Message-ID'].endswith('@example.test>'))
        self.assertEqual(msg['Auto-Submitted'], 'auto-generated')
        self.assertEqual(msg.get_content_type(), 'multipart/alternative')
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()], ['text/plain', 'text/html'])
        self.assertIn('048213', msg.get_body(('plain',)).get_content())
        self.assertIn('048213', msg.get_body(('html',)).get_content())

    def test_port_465_uses_implicit_tls_and_a_custom_sender_name(self):
        self.configure(port=465, **{'from': 'robot@example.test', 'from_name': '示例团队'})
        Mailer(self.root).send('user@example.org', '主题', '正文')
        (server,) = FakeSmtp.instances
        self.assertIsInstance(server, FakeSmtpSsl)
        self.assertIsInstance(server.context, ssl.SSLContext)
        self.assertEqual(server.calls, ['login', 'send', 'quit'])
        (msg,) = server.messages
        self.assertIn('robot@example.test', msg['From'])
        self.assertNotIn('NeuxMind', msg['From'])
        self.assertEqual(msg.get_content_type(), 'text/plain')

    def test_environment_wins_over_the_file(self):
        self.configure()
        os.environ.update({'NEUXMIND_SMTP_HOST': 'smtp.env.test', 'NEUXMIND_SMTP_PORT': '465', 'NEUXMIND_SMTP_USER': 'env@env.test'})
        cfg = Mailer(self.root).config()
        self.assertEqual((cfg['host'], cfg['ssl'], cfg['from']), ('smtp.env.test', True, 'env@env.test'))

    def test_without_settings_the_code_stays_on_this_computer(self):
        m = Mailer(self.root)
        result = m.send('user@example.org', '主题', '正文 048213', html='<p>048213</p>')
        self.assertEqual((m.mode, result['delivery']), ('outbox', 'outbox'))
        self.assertEqual(FakeSmtp.instances, [])
        self.assertIn('正文 048213', m.outbox.read_text())
        self.assertNotIn('<p>', m.outbox.read_text())

    def test_failures_say_what_to_fix_and_keep_the_detail_in_the_log(self):
        cases = {'login': (smtplib.SMTPAuthenticationError(535, b'5.7.3 Authentication unsuccessful'), '账号'),
                 'connect': (ConnectionRefusedError(61, 'refused'), '连接不上'),
                 'starttls': (ssl.SSLError('handshake'), '加密连接'),
                 'send': (smtplib.SMTPSenderRefused(550, b'not allowed', 'no-reply@example.test'), '发件地址')}
        self.configure()
        for step, (error, words) in cases.items():
            with self.subTest(step=step):
                FakeSmtp.fail = {step: error}
                with self.assertLogs('brainos.mail', level='WARNING') as logs, self.assertRaises(MailError) as caught:
                    Mailer(self.root).send('user@example.org', '主题', '正文')
                self.assertIn(words, str(caught.exception))
                self.assertIn(type(error).__name__, logs.output[0])
        more = ((smtplib.SMTPRecipientsRefused({'user@example.org': (550, b'no such user')}), '收件地址被'),
                (smtplib.SMTPNotSupportedError('addresses require internationalized email support'), '非英文字符'),
                (smtplib.SMTPNotSupportedError('STARTTLS extension not supported by server.'), '不支持加密连接'),
                (smtplib.SMTPServerDisconnected('Connection unexpectedly closed'), '稍后再试'),
                (TimeoutError('timed out'), '超时'),
                (UnicodeEncodeError('ascii', '用户', 0, 2, 'ordinal not in range'), '不接受的字符'))
        for error, words in more:
            with self.subTest(error=type(error).__name__):
                FakeSmtp.fail = {'send': error}
                with self.assertLogs('brainos.mail', level='WARNING'), self.assertRaises(MailError) as caught:
                    Mailer(self.root).send('user@example.org', '主题', '正文')
                self.assertIn(words, str(caught.exception))

    def test_broken_settings_are_reported_not_treated_as_unconfigured(self):
        for over, words in (({'port': 'abc'}, 'port'), ({'user': '', 'from': ''}, 'from'),
                            ({'from': 'NeuxMind <robot@example.test>'}, '不带名称'), ({'from_name': 'A\nBcc: x@y.z'}, 'from_name'),
                            ({'user': 'no-reply＠example.test', 'from': 'robot@example.test'}, '非英文字符')):
            with self.subTest(over=over):
                self.configure(**over)
                m = Mailer(self.root)
                self.assertEqual(m.mode, 'smtp')      # a typo must not silently fall back to the local file
                with self.assertRaises(MailError) as caught:
                    m.send('user@example.org', '主题', '正文')
                self.assertIn(words, str(caught.exception))
                self.assertEqual(FakeSmtp.instances, [])
        (self.root / 'mail.json').write_text('{not json')
        m = Mailer(self.root)
        self.assertEqual(m.mode, 'smtp')
        with self.assertRaises(MailError) as caught:
            m.send('user@example.org', '主题', '正文')
        self.assertIn('mail.json', str(caught.exception))

    def test_check_signs_in_without_sending(self):
        self.configure()
        info = Mailer(self.root).check()
        self.assertEqual((info['host'], info['port'], info['from']), ('smtp.example.test', 587, 'no-reply@example.test'))
        (server,) = FakeSmtp.instances
        self.assertEqual(server.calls, ['starttls', 'login', 'noop', 'quit'])
        self.assertNotIn('password', info)


class CommandTest(MailCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = mailer_module.main(['--state-dir', str(self.root), *args])
        return code, out.getvalue()

    def test_init_writes_a_private_template_once(self):
        code, out = self.run_cli('init')
        path = self.root / 'mail.json'
        self.assertEqual(code, 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        template = json.loads(path.read_text())
        self.assertEqual(set(template), {'host', 'port', 'user', 'password', 'from', 'from_name'})
        self.assertEqual(template['password'], '')
        self.assertIn(str(path), out)
        path.write_text(json.dumps({**template, 'host': 'smtp.example.test'}))
        code, out = self.run_cli('init')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(path.read_text())['host'], 'smtp.example.test')

    def test_check_and_test_report_in_plain_words(self):
        code, out = self.run_cli('check')
        self.assertEqual(code, 1)
        self.assertIn('未配置', out)
        cfg = self.configure()
        (self.root / 'mail.json').chmod(0o644)
        code, out = self.run_cli('check')
        self.assertEqual(code, 0)
        self.assertIn('smtp.example.test', out)
        self.assertIn('chmod 600', out)            # other accounts on this computer could read the password
        self.assertNotIn(cfg['password'], out)
        code, out = self.run_cli('test', 'me@example.org')
        self.assertEqual(code, 0)
        msg = FakeSmtp.instances[-1].messages[0]
        self.assertEqual(msg['To'], 'me@example.org')
        self.assertEqual(msg.get_content_type(), 'multipart/alternative')
        FakeSmtp.fail = {'login': smtplib.SMTPAuthenticationError(535, b'no')}
        with self.assertLogs('brainos.mail', level='WARNING'):
            code, out = self.run_cli('check')
        self.assertEqual(code, 1)
        self.assertIn('账号', out)


class SendCodeTest(MailCase):
    def setUp(self):
        super().setUp()
        from brainos.server import Application
        self.configure()
        self.app = Application(self.root, '')
        self.addCleanup(self.app.close)

    def test_a_code_leaves_as_the_formal_mail_and_nothing_about_it_reaches_the_page(self):
        os.environ['NEUXMIND_MAIL_DEV_ECHO'] = '1'
        result = self.app.send_code('user@example.org', 'register', '048213')
        self.assertEqual(result['delivery'], 'smtp')
        self.assertNotIn('outbox', result)
        self.assertNotIn('dev_code', result)
        (msg,) = FakeSmtp.instances[-1].messages
        self.assertEqual(msg['Subject'], 'NeuxMind 注册验证码：048213')
        self.assertIn('048213', msg.get_body(('html',)).get_content())
        self.assertFalse((self.root / 'mail-outbox.txt').exists())

    def test_a_failed_send_frees_the_resend_timer(self):
        form = {'email': 'user@example.org', 'password': secrets.token_hex(3) + '-a1b2', 'name': 'U'}
        email, code = self.app.identity.start_registration(form)
        FakeSmtp.fail = {'login': smtplib.SMTPAuthenticationError(535, b'no')}
        with self.assertLogs('brainos.mail', level='WARNING'), self.assertRaises(MailError):
            self.app.send_code(email, 'register', code)
        FakeSmtp.fail = {}
        email, code = self.app.identity.start_registration(form)   # no "wait 60 seconds"
        self.assertEqual(self.app.send_code(email, 'register', code)['delivery'], 'smtp')


if __name__ == '__main__':
    unittest.main()
