"""Issue #20: turning autostart on later from the card, turning it off, and taking it away whenever the
console removes the install it belongs to (issue #19's "清理" and "删除后重新安装"). Stand-ins only; see
``test_runtime_autostart.py``."""
import json
import unittest
from unittest import mock

from brainos import runtime_autostart_device as auto
from brainos.errors import CodedError
from runtime_autostart_support import AutostartCase


class CardTests(AutostartCase):
    def test_turning_it_on_later_for_an_install_this_console_made(self):
        connection = self.connect_directly()
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        before = self.robot(connection)['runtime_install']
        view = self.autostart(connection)
        self.assertEqual((view['mode'], [stage['id'] for stage in view['stages']]), ('autostart', ['autostart', 'link']))
        view = self.until_asked(connection)
        self.assertEqual(view['autostart']['plan']['unit'], self.expected_unit())
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assert_autostart_on(connection)
        self.assert_online_and_linked(connection)
        self.assertEqual(self.robot(connection)['runtime_install'], before)   # the install's own outcome stays as it was
        self.assert_secret_nowhere(self.device.sudo_password, view)

    def test_asking_again_when_it_is_on_changes_nothing(self):
        connection = self.connect_directly()
        self.install_with_autostart(connection)
        restarts = len([call for call in self.device.systemctl_calls() if 'restart' in call])
        view = self.autostart(connection)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(view['autostart']['state'], 'on')
        self.assertEqual(len(self.device.root_runs), 1)                 # nothing asked, nothing run as root
        self.assertEqual(len([call for call in self.device.systemctl_calls() if 'restart' in call]), restarts)
        self.assert_autostart_on(connection)

    def test_a_management_layer_this_console_did_not_install_is_refused(self):
        self.device.by_hand()
        connection = self.connect_directly()
        self.start(connection)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'needs_choice')
        self.assertEqual(self.install(connection, existing='use')['state'], 'succeeded')
        with self.assertRaises(CodedError) as caught:
            self.autostart(connection)
        self.assertEqual(caught.exception.code, 'autostart_not_console')
        self.assertEqual((self.device.root_runs, self.device.account_runs), ([], []))
        self.assertIsNone(self.device.unit())

    def test_and_so_is_one_whose_install_on_the_robot_is_not_the_one_the_console_made(self):
        connection = self.connect_directly()
        self.install(connection)
        self.device.write('neuxmind/.console-package', b'runtime-someone-else\n')
        self.autostart(connection)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.saved(connection)['reason'], 'not_console')
        self.assertEqual(self.device.root_runs, [])

    def test_a_robot_without_an_install_is_told_to_install_first(self):
        connection = self.connect_directly()
        with self.assertRaises(CodedError) as caught:
            self.autostart(connection)
        self.assertEqual(caught.exception.code, 'autostart_not_installed')

    def test_the_service_starts_the_body_the_install_recorded(self):
        # Issue #21's install script records the body in ~/neuxmind/adapter and its start uses it; the service
        # passes that same body, so it never starts an install as another one. Only known names are taken.
        connection = self.connect_directly()
        self.install(connection)
        for recorded, used in (('sim_arm', 'sim_arm'), ('evil;reboot', 'sim_quadruped')):
            with self.subTest(recorded=recorded):
                self.device.write('neuxmind/adapter', (recorded + '\n').encode())
                self.autostart(connection)
                view = self.until_asked(connection)
                self.assertIn('NX_ADAPTER=%s' % used, view['autostart']['plan']['unit'])
                self.answer(connection, view, password=self.device.sudo_password)
                view = self.wait(connection)
                self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
                self.assertEqual(self.device.unit(), self.expected_unit(adapter=used))
                self.assertEqual(self.device.read('neuxmind/.autostart').decode().split()[2], used)
                self.assertEqual(self.saved(connection)['adapter'], used)
                self.assertEqual(self.device.runs()[-1], ('start', '8790', used))

    def test_one_job_at_a_time(self):
        connection = self.connect_directly()
        self.install(connection)
        self.autostart(connection)
        self.until_asked(connection)
        with self.assertRaises(CodedError) as caught:
            self.autostart(connection)
        self.assertEqual(caught.exception.code, 'install_in_progress')
        self.answer(connection, self.until_asked(connection), decision='decline')
        self.wait(connection)


class TurningOffTests(AutostartCase):
    def test_turning_it_off_takes_the_service_away_and_the_runtime_runs_as_before(self):
        connection = self.connect_directly()
        self.install_with_autostart(connection)
        view = self.autostart_off(connection)
        self.assertEqual([stage['id'] for stage in view['stages']], ['autostart_off', 'start', 'link'])
        view = self.until_asked(connection)
        plan = view['autostart']['plan']
        self.assertEqual(plan['kind'], 'off')
        self.assertEqual([step['command'] for step in plan['commands']],
                         ['sudo systemctl disable --now ' + auto.UNIT_NAME, 'sudo rm ' + auto.UNIT_PATH, 'sudo systemctl daemon-reload'])
        self.assertEqual(plan['script'], auto.OFF_SCRIPT)
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assert_autostart_off(connection, 'removed')
        self.assertEqual(self.device.root_runs[-1], ('autostart_off', 'password', ('robot',)))
        self.assertEqual(self.device.runtime_port(), 8790)
        self.assertEqual(self.device.service('active'), 'inactive')
        self.assert_online_and_linked(connection)
        self.device.reboot()
        self.assertIsNone(self.device.runtime_port())

    def test_an_install_removed_by_hand_leaves_a_service_that_starts_nothing_until_it_is_taken_away(self):
        connection = self.connect_directly()
        self.install_with_autostart(connection)
        self.device._run(['sh', self.device.local(self.package_dir() + '/device_install.sh'), 'remove'])
        self.device.reboot()
        self.assertIsNone(self.device.runtime_port())                    # ~/neuxmind/.autostart went with the install
        self.assertEqual(self.device.service('active', 'inactive'), 'inactive')
        self.autostart_off(connection)
        view = self.until_asked(connection)
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertEqual([stage['id'] for stage in view['stages']], ['autostart_off'])   # nothing left to start
        self.assertIsNone(self.device.unit())
        self.assertEqual(self.saved(connection)['reason'], 'removed')

    def test_declining_to_turn_it_off_leaves_it_on(self):
        connection = self.connect_directly()
        self.install_with_autostart(connection)
        self.autostart_off(connection)
        view = self.until_asked(connection)
        self.answer(connection, view, decision='decline')
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_autostart_on(connection)
        self.assertEqual(len(self.device.root_runs), 1)


class RemovingTheInstallTests(AutostartCase):
    def failed_with_autostart_on(self):
        """An install whose last step failed after autostart was turned on: what "清理" is for."""
        connection = self.connect_directly()
        self.start(connection, autostart=True)
        view = self.until_asked(connection)
        with mock.patch.object(self.platform.runtime_link, 'attach', side_effect=ValueError('接入失败（测试）')):
            self.answer(connection, view, password=self.device.sudo_password)
            view = self.wait(connection)
        self.assertEqual((view['state'], view['failure']['stage']), ('failed', 'link'), view)
        self.assertTrue(view['can_clean'])
        self.assertEqual(self.saved(connection)['state'], 'on')
        return connection

    def test_cleaning_up_also_takes_the_service_away(self):
        connection = self.failed_with_autostart_on()
        self.installs.clean({'project_id': self.pid, 'robot_id': connection['robot_id']})
        view = self.until_asked(connection)
        self.assertEqual(view['autostart']['plan']['kind'], 'off')
        self.assertEqual([stage['id'] for stage in view['stages']], ['autostart_off', 'clean'])
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertIsNone(self.device.unit())
        self.assertFalse(self.device.enabled())
        self.assertEqual(self.device.names('neuxmind'), [])
        self.assertEqual(self.saved(connection)['reason'], 'removed')

    def test_cleaning_up_without_the_password_leaves_a_service_that_starts_nothing(self):
        connection = self.failed_with_autostart_on()
        self.installs.clean({'project_id': self.pid, 'robot_id': connection['robot_id']})
        view = self.until_asked(connection)
        self.answer(connection, view, decision='decline')
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.names('neuxmind'), [])
        self.assertEqual(self.device.unit(), self.expected_unit())       # still there, and said so
        self.assertIn(auto.UNIT_PATH, ' '.join(view['notes']))
        self.device.reboot()
        self.assertIsNone(self.device.runtime_port())                    # its condition no longer holds
        self.assertEqual(self.device.service('active', 'inactive'), 'inactive')

    def test_replacing_an_install_that_autostarts_asks_to_take_the_service_away_first(self):
        connection = self.connect_directly()
        self.install_with_autostart(connection)
        self.device.write('neuxmind/.console-package', b'runtime-older-console\n')   # now it reads as another console's install
        self.start(connection, existing='replace', confirm_replace=True)
        view = self.until_asked(connection)
        self.assertEqual(view['autostart']['plan']['kind'], 'off')
        self.assertEqual(view['stage'], 'autostart_off')
        self.answer(connection, view, password=self.device.sudo_password)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertIsNone(self.device.unit())
        self.assert_online_and_linked(connection)


if __name__ == '__main__':
    unittest.main()
