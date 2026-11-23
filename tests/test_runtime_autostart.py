"""Issue #20 (R1-03, R1-08): the management layer starts when the robot boots, as a system service the
console registers only after a person saw exactly what runs with administrator rights and agreed.

The robot is ``autostart_fixture.AutostartDevice``: SSH, SFTP and the product's scripts run for real in a
temporary directory; sudo and systemd are stand-ins (nothing runs as root). The real sudo, systemd, install
script and runtime are covered by ``tests/runtime_autostart_systemd_check.py`` in a Linux container."""
import json
import secrets
import unittest
from unittest import mock

from brainos import runtime_autostart_device as auto
from brainos.errors import CodedError
from runtime_autostart_support import AutostartCase


class InstallWithAutostartTests(AutostartCase):
    def test_it_lists_what_runs_waits_for_the_person_and_then_the_robot_starts_it_at_boot(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        # Installed and started without any administrator rights; now it waits, nothing has run as root.
        self.assertEqual(view['state'], 'running')
        self.assertEqual(view['stage'], 'autostart')
        self.assertEqual([stage['id'] for stage in view['stages']], ['check', 'upload', 'install', 'start', 'autostart', 'link'])
        self.assertEqual(self.device.root_runs, [])
        self.assertIsNone(self.device.unit())
        self.assertEqual(self.device.runtime_port(), 8790)
        plan = view['autostart']['plan']
        self.assertEqual(plan['kind'], 'on')
        self.assertEqual((plan['user'], plan['how'], plan['needs_password']), ('robot', 'password', True))
        self.assertEqual((plan['unit_path'], plan['unit']), (auto.UNIT_PATH, self.expected_unit()))
        commands = [step['command'] for step in plan['commands']]
        self.assertEqual(commands, ['sudo tee ' + auto.UNIT_PATH, 'sudo systemctl daemon-reload',
                                    'sudo systemctl enable ' + auto.UNIT_NAME, 'sudo systemctl --no-block restart ' + auto.UNIT_NAME])
        self.assertTrue(all(step['what'] for step in plan['commands']))
        self.assertIn('.autostart', ' '.join(step['what'] for step in plan['account_steps']))
        self.assertEqual(plan['script'], auto.ON_SCRIPT)
        self.assertIn('robot', plan['does_not'])
        password = self.device.sudo_password
        self.answer(connection, view, password=password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertTrue(all(stage['state'] == 'done' for stage in view['stages']), view['stages'])
        self.assertEqual(view['autostart']['state'], 'on')
        self.assertIsNone(view['autostart'].get('plan'))
        # Exactly one root script ran, the confirmed one, with the typed password on sudo's standard input only.
        digest = auto.digest(self.expected_unit())
        self.assertEqual(self.device.root_runs, [('autostart_on', 'password', ('robot', self.package.id, '8790', 'sim_quadruped', digest))])
        self.assertEqual(self.device.sudo_inputs, [(password + '\n').encode()])
        self.assert_autostart_on(connection)
        # The service took over the runtime: the install script stopped the detached one and started it again.
        self.assertEqual(self.device.runs()[-2:], [('stop', '8790', 'sim_quadruped'), ('start', '8790', 'sim_quadruped')])
        self.assertEqual(self.device.read('neuxmind/runtime.pid').decode().strip(), self.device.service('pid'))
        self.assert_online_and_linked(connection)
        self.assert_secret_nowhere(password, view)
        self.assertNotIn(self.device.password, json.dumps(self.device.sudo_inputs, default=str))   # not the SSH login's

        self.device.reboot()
        self.device.wait_service('active')
        self.assertEqual(self.device.runtime_port(), 8790)
        robot = self.back_online(connection)
        self.assertEqual(robot['runtime_link']['port'], 8790)
        self.assertEqual(robot['token'], self.device.token())

    def test_through_a_jump_host_too(self):
        jump = self.jump_host()
        connection = self.connect_through_jump(jump)
        view = self.install_with_autostart(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assert_autostart_on(connection)
        self.assert_online_and_linked(connection)
        self.device.reboot()
        self.device.wait_service('active')
        self.back_online(connection)
        self.assertTrue(jump.forwarded, 'every SSH session to the robot went through the jump host')

    def test_another_port_and_a_manipulator(self):
        connection = self.connect_directly(family='manipulator')
        view = self.install_with_autostart(connection, port=8795)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_on(connection, port=8795, adapter='sim_arm')
        self.assert_online_and_linked(connection, port=8795)

    def test_without_asking_for_it_nothing_runs_as_root_and_the_card_says_why(self):
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertNotIn('autostart', [stage['id'] for stage in view['stages']])
        self.assertEqual(self.device.root_runs, [])
        self.assertEqual(self.device.account_runs, [])
        self.assertIsNone(self.device.unit())
        self.assertIsNone(self.saved(connection))       # the page says "安装时没有选择" from the install alone
        self.device.reboot()
        self.assertIsNone(self.device.runtime_port())   # without autostart the runtime stays down after a power cut

    def test_declining_runs_nothing_as_root_and_the_install_still_ends_online(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        self.answer(connection, view, decision='decline')
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual((view['autostart']['state'], view['autostart']['reason']), ('off', 'declined'))
        self.assertEqual(self.device.root_runs, [])
        self.assert_autostart_off(connection, 'declined')
        self.assert_online_and_linked(connection)
        self.assertEqual(self.device.runtime_port(), 8790)

    def test_no_answer_in_time_is_not_a_yes(self):
        self.installs.budgets['confirm'] = 1
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'not_confirmed')
        self.assertEqual(self.device.root_runs, [])
        self.assert_online_and_linked(connection)

    def test_stopping_while_it_waits_runs_nothing_as_root(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        self.until_asked(connection)
        self.installs.stop({'project_id': self.pid, 'robot_id': connection['robot_id']})
        view = self.wait(connection)
        self.assertEqual(view['state'], 'stopped', view)
        self.assertEqual(self.device.root_runs, [])
        self.assertIsNone(self.device.unit())
        self.assertIsNone(self.device.read('neuxmind/.autostart'))

    def test_a_typed_password_is_hidden_from_what_the_robot_says_however_short(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        self.until_asked(connection)
        job = self.installs._jobs[connection['robot_id']]
        job.autostart._secret = 'xy'
        with mock.patch.object(self.platform.ssh_edit, 'saved_login', side_effect=ValueError('gone')):
            self.assertIn('xy', job._hidden())
            self.assertEqual(job._shown('sudo said xy'), 'sudo said [已隐藏]')
        job.autostart._secret = None
        self.assertNotIn('xy', job._hidden())
        self.answer(connection, self.until_asked(connection), decision='decline')
        self.wait(connection)

    def test_answers_that_are_not_for_what_waits_are_refused(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        for extra, code in (({'plan': 'not-this-plan'}, 'autostart_plan_changed'),
                            ({'job_id': '0' * 16}, 'autostart_not_waiting'),
                            ({'decision': 'maybe'}, 'autostart_answer_invalid')):
            with self.subTest(extra=extra):
                request = {'project_id': self.pid, 'robot_id': connection['robot_id'], 'job_id': view['job_id'],
                           'plan': view['autostart']['plan']['id'], 'decision': 'confirm',
                           'sudo_password': self.device.sudo_password, **extra}
                with self.assertRaises(CodedError) as caught:
                    self.installs.answer(request)
                self.assertEqual(caught.exception.code, code)
        for password, code in ((None, 'autostart_password_missing'), ('', 'autostart_password_missing'),
                               ('two\nlines', 'autostart_password_invalid'), (7, 'autostart_password_invalid'),
                               ('x' * 300, 'autostart_password_invalid')):
            with self.subTest(password=password):
                with self.assertRaises(CodedError) as caught:
                    self.answer(connection, view, password=password)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.device.root_runs, [])
        self.answer(connection, view, decision='decline')
        self.assertEqual(self.wait(connection)['state'], 'succeeded')
        with self.assertRaises(CodedError) as caught:
            self.answer(connection, view, password=self.device.sudo_password)
        self.assertEqual(caught.exception.code, 'autostart_not_waiting')


class SudoTests(AutostartCase):
    def test_a_wrong_password_asks_again_and_the_right_one_then_works(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        wrong = 'wrong-' + secrets.token_hex(6)
        self.answer(connection, view, password=wrong)
        view = self.wait(connection, until=lambda v: v['state'] != 'running' or (((v['autostart'] or {}).get('plan') or {}).get('error')))
        self.assertEqual(view['autostart']['state'], 'waiting', view)
        self.assertIn('密码', view['autostart']['plan']['error'])
        self.assertEqual(len(self.device.root_runs), 1)
        self.assertIsNone(self.device.unit())
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_on(connection)
        self.assert_secret_nowhere(wrong, view)

    def test_three_wrong_passwords_end_it_without_autostart(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        for attempt in range(3):
            view = self.wait(connection, until=lambda v, n=attempt: v['state'] != 'running' or
                             ((v['autostart'] or {}).get('state') == 'waiting' and (v['autostart']['plan'].get('attempt') or 0) == n))
            self.answer(connection, view, password='wrong-%d' % attempt)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'wrong_password')
        self.assertEqual(len(self.device.root_runs), 3)
        self.assert_online_and_linked(connection)

    def test_without_a_password_when_sudo_needs_none(self):
        self.device = self.robot_device(sudo='nopasswd')
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        self.assertEqual((view['autostart']['plan']['how'], view['autostart']['plan']['needs_password']), ('nopasswd', False))
        self.answer(connection, view)
        self.assertEqual(self.wait(connection)['state'], 'succeeded')
        self.assertEqual(self.device.root_runs[0][1], 'nopasswd')
        self.assertEqual(self.device.sudo_inputs, [b''])
        self.assert_autostart_on(connection)

    def test_a_root_login_needs_no_sudo(self):
        self.device = self.robot_device(sudo='root')
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        plan = view['autostart']['plan']
        self.assertEqual((plan['how'], plan['needs_password'], plan['user']), ('root', False, 'root'))
        self.assertEqual(plan['commands'][0]['command'], 'tee ' + auto.UNIT_PATH)
        self.answer(connection, view)
        self.assertEqual(self.wait(connection)['state'], 'succeeded')
        self.assertEqual(self.device.root_runs[0][:2], ('autostart_on', 'root'))
        self.assertIn('User=root', self.device.unit())

    def test_no_sudo_at_all_asks_nothing(self):
        self.device = self.robot_device(sudo='none')
        connection = self.connect_directly()
        view = self.install(connection, autostart=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'no_sudo')
        self.assertEqual(self.device.root_runs, [])
        self.assert_online_and_linked(connection)

    def test_an_account_that_may_not_use_sudo(self):
        self.device = self.robot_device(sudo='denied')
        connection = self.connect_directly()
        view = self.install_with_autostart(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'no_sudo')
        self.assertIn('sudoers', self.saved(connection)['message'])
        self.assert_online_and_linked(connection)

    def test_sudo_that_wants_a_terminal(self):
        self.device = self.robot_device(sudo='tty')
        connection = self.connect_directly()
        view = self.install_with_autostart(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'no_sudo')
        self.assertIn('终端', self.saved(connection)['message'])


class DeviceStateTests(AutostartCase):
    def test_a_robot_without_systemd(self):
        self.device = self.robot_device(systemd=False)
        connection = self.connect_directly()
        view = self.install(connection, autostart=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'no_systemd')
        self.assertEqual(self.device.root_runs, [])

    def test_a_service_of_that_name_the_console_did_not_make_is_left_alone(self):
        foreign = '[Unit]\nDescription=someone else\n[Service]\nExecStart=/usr/bin/true\n'
        self.device.write_unit(foreign)
        connection = self.connect_directly()
        view = self.install(connection, autostart=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.unit(), foreign)
        record = self.saved(connection)
        self.assertEqual((record['state'], record['reason']), ('off', 'unit_taken'))
        self.assertEqual(self.device.root_runs, [])

    def test_one_made_for_another_account_is_left_alone_too(self):
        other = self.expected_unit().replace('user=robot ', 'user=someone ')
        self.device.write_unit(other)
        connection = self.connect_directly()
        self.install(connection, autostart=True)
        self.assertEqual(self.device.unit(), other)
        self.assertEqual(self.saved(connection)['reason'], 'unit_taken')

    def test_the_service_that_does_not_come_up_fails_the_step_and_says_what_is_left(self):
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        self.device.control('fail-start')
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'failed', view)
        self.assertEqual((view['failure']['stage'], view['failure']['code']), ('autostart', 'autostart_service_failed'))
        left = [item['path'] for item in view['left']]
        self.assertIn(auto.UNIT_PATH, left)
        self.assertIn('取消开机自启', view['cleanup'])
        self.assertEqual(self.saved(connection)['state'], 'failed')
        # "取消开机自启" takes the service away again, and the runtime runs detached as after any install.
        self.device.control('fail-start', False)
        self.autostart_off(connection)
        view = self.until_asked(connection)
        self.assertEqual(view['autostart']['plan']['kind'], 'off')
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_off(connection, 'removed')
        self.assertEqual(self.device.runtime_port(), 8790)
        self.assert_online_and_linked(connection)


if __name__ == '__main__':
    unittest.main()
