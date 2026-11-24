"""Issue #39: a password that works in a terminal must work here too.

``ssh`` answers a host that asks for the password itself (the "keyboard-interactive" method, what PAM
uses: the prompt reads ``(user@host) Password:``) before it tries the plain password method. Some hosts
offer both and accept the password only when asked. The console used the plain method alone, and such a
host turned it away with "SSH 认证失败" although the same password logged in from a terminal.
Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import io
import os
import secrets
import unittest
from unittest import mock

import paramiko

from brainos import device_access
from ssh_fixture import SSHFixture

USER = 'robot'


class PromptLoginTests(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_hex(8)
        self.host = SSHFixture(username=USER, password=self.secret)
        self.addCleanup(self.host.close)
        self.opened = []
        self.addCleanup(lambda: [client.close() for client in self.opened])

    def login(self, typed=None, hop='device', auth='password', secret=None, **where):
        endpoint = {'host': '127.0.0.1', 'port': self.host.port, 'username': USER}
        pinned = device_access.fingerprint(self.host.key)
        client = device_access.open_client(endpoint, auth, secret or {'password': typed or self.secret}, pinned, hop, **where)
        self.opened.append(client)
        return client

    def assert_refused(self, hop='device', **login):
        with self.assertRaises(device_access.ConnectError) as raised:
            self.login(hop=hop, **login)
        self.assertEqual((raised.exception.hop, raised.exception.reason), (hop, 'auth'))
        return raised.exception

    def test_host_that_only_takes_the_password_when_it_asks_lets_us_in(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False

        client = self.login()

        self.assertTrue(client.get_transport().is_authenticated())
        self.assertEqual(self.host.prompt_answers, [[self.secret]])

    def test_host_without_the_plain_password_method_lets_us_in(self):
        self.host.prompts = [('Password: ', False)]
        self.host.password_method = None                    # not even offered: see allowed_auths below
        self.host_offers('keyboard-interactive')

        client = self.login()

        self.assertTrue(client.get_transport().is_authenticated())

    def test_session_opened_by_answering_the_prompt_runs_commands(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False

        _, stdout, _ = self.login().exec_command('uname -a', timeout=5)

        self.assertIn(b'test-fixture', stdout.read())

    def test_plain_password_login_does_not_go_through_the_prompt(self):
        self.host.prompts = [('Password: ', False)]

        self.login()

        self.assertEqual(self.host.methods_tried, ['password'])

    def test_wrong_password_is_still_refused_as_an_authentication_failure(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False

        error = self.assert_refused(typed=secrets.token_hex(8))

        self.assertIn('认证失败', str(error))
        self.assertEqual(self.host.methods_tried, ['password', 'keyboard-interactive'])     # one try each, no more

    def test_host_without_prompts_refuses_a_wrong_password_as_before(self):
        self.assert_refused(typed=secrets.token_hex(8))

        self.assertEqual(self.host.methods_tried, ['password'])         # a host that does not ask is not asked

    def test_wrong_password_is_tried_once_where_the_plain_method_is_not_offered(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False
        self.host_offers('keyboard-interactive')

        self.assert_refused(typed=secrets.token_hex(8))

        self.assertEqual(self.host.methods_tried.count('keyboard-interactive'), 1)

    def test_slow_host_is_reported_as_slow_and_not_asked_again(self):
        self.host.prompts, self.host.auth_delay = [('Password: ', False)], 1.5

        with mock.patch.object(device_access, 'AUTH_TIMEOUT', .5), self.assertRaises(device_access.ConnectError) as raised:
            self.login()

        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertNotIn('keyboard-interactive', self.host.methods_tried)

    def test_password_is_not_typed_into_what_the_host_asks_after_it(self):
        # After the password a host may ask for a second factor or a new password, hidden as well.
        self.host.prompts, self.host.password_method = [('Password: ', False)], False
        self.host.later_prompts = [[('New password: ', False), ('Retype new password: ', False)]]

        self.assert_refused()

        self.assertEqual(self.host.prompt_answers, [[self.secret], ['', '']])

    def test_refused_login_leaves_no_connection_open(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False

        self.assert_refused(typed=secrets.token_hex(8))

        self.assertFalse(any(t.is_active() and t.is_authenticated() for t in self.host.transports))

    def test_jump_host_that_asks_for_the_password_lets_us_in(self):
        self.host.prompts, self.host.password_method = [('Password: ', False)], False

        client = self.login(hop='jump', level=1, total=1)

        self.assertTrue(client.get_transport().is_authenticated())

    def test_password_is_not_typed_into_a_question_the_host_shows_on_screen(self):
        # A question whose answer is echoed is not a password prompt (a one-time code, a ticket number).
        self.host.prompts, self.host.password_method = [('Verification code: ', True)], False

        self.assert_refused()

        self.assertEqual(self.host.prompt_answers, [['']])

    def test_every_hidden_question_of_one_login_gets_the_password(self):
        self.host.prompts, self.host.password_method = [('Password: ', False), ('Password again: ', False)], False

        self.login()

        self.assertEqual(self.host.prompt_answers, [[self.secret, self.secret]])

    # A refusal says which login was refused: the form can be on "本机 SSH Agent" without the user having
    # chosen it (pasting an ssh command sets it), and "check the password or key" then points nowhere.
    def test_refused_password_is_named_as_the_password(self):
        error = self.assert_refused(typed=secrets.token_hex(8))

        self.assertIn('认证失败', str(error))
        self.assertIn('不接受这个密码', str(error))
        self.assertNotIn('Agent', str(error))

    def test_refused_agent_login_says_no_password_was_used(self):
        with mock.patch.dict(os.environ, {'SSH_AUTH_SOCK': ''}):          # an agent without keys; never the user's own
            error = self.assert_refused(auth='agent', secret={})

        self.assertIn('认证失败', str(error))
        self.assertIn('本机 SSH Agent', str(error))
        self.assertIn('没有用密码', str(error))
        self.assertIn('改为“密码”', str(error))

    def test_refused_agent_login_of_a_jump_host_names_the_jump_host(self):
        with mock.patch.dict(os.environ, {'SSH_AUTH_SOCK': ''}):
            error = self.assert_refused(hop='jump', auth='agent', secret={}, level=2, total=3)

        self.assertIn('第 2 级跳板机拒绝登录', str(error))
        self.assertIn('本机 SSH Agent', str(error))

    def test_refused_private_key_is_named_as_the_key(self):
        text = io.StringIO()
        paramiko.RSAKey.generate(2048).write_private_key(text)

        error = self.assert_refused(auth='key', secret={'private_key': text.getvalue()})

        self.assertIn('不接受这把私钥', str(error))

    def host_offers(self, methods):
        """Make the fixture announce exactly ``methods`` (it refuses the plain password either way)."""
        self.host.allowed_auths = methods


if __name__ == '__main__':
    unittest.main()
