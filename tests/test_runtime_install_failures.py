"""Issue #19: a failed install says which step, what it left on the robot and how to clean it up;
an install whose connection drops goes on, or can be done again cleanly."""
import json
import secrets
import threading
import unittest
from unittest import mock

from brainos import device_access, device_transfer, runtime_package
from brainos import runtime_install_device as dev
from runtime_install_support import InstallCase


class CheckRefusals(InstallCase):
    """Refused before anything is written to the robot."""

    def refused(self, code, **fields):
        connection = self.connect_directly()
        view = self.install(connection, **fields)
        self.assertEqual((view['state'], view['stage'], view['failure']['code']), ('failed', 'check', code), view)
        self.assertEqual((view['left'], view['can_clean']), ([], False))
        self.assertIn('没有留下', view['cleanup'])
        self.assertEqual(self.device.names('neuxmind-pkg'), [])
        self.assertEqual(self.device.names('neuxmind'), [])
        self.assertEqual(self.device.writes, [])
        self.assertEqual({kind for kind, _ in self.device.install_runs}, {'probe'})
        self.assertEqual(self.robot(connection)['runtime_install']['failure']['code'], code)
        return view

    def test_another_program_on_the_port(self):
        self.device.listen_elsewhere(8790)
        self.assertIn('8790', self.refused('install_port_busy')['failure']['message'])

    def test_an_ssh_service_that_does_not_forward(self):
        self.device.forwarding = False
        self.refused('install_forwarding_off')

    def test_not_linux(self):
        self.device.replace_tool('uname', 'echo Darwin\n')
        self.refused('install_device_unsupported')

    def test_no_python_310(self):
        self.device.replace_tool('python3', 'case "$2" in *version_info*) echo 3.8;; *) exit 1;; esac\n')
        self.assertIn('3.8', self.refused('install_device_unsupported')['failure']['message'])

    def test_no_venv(self):
        self.device.replace_tool('python3', 'case "$2" in *version_info*) echo 3.10;; *) exit 1;; esac\n')
        self.assertIn('python3-venv', self.refused('install_device_unsupported')['failure']['message'])

    def test_a_missing_tool(self):
        self.device.replace_tool('flock')
        self.assertIn('flock', self.refused('install_device_unsupported')['failure']['message'])

    def test_too_little_space(self):
        self.device.replace_tool('df', 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
                                       'echo "/dev/root 1000000 990000 10000 99% /"\n')
        self.refused('install_no_space')

    def test_a_refused_login_is_not_repeated(self):
        connection = self.connect_directly()
        self.device.password = secrets.token_hex(8)              # changed on the robot meanwhile
        view = self.install(connection)
        self.assertEqual((view['stage'], view['failure']['code']), ('check', 'install_login_refused'))
        self.assertEqual(self.device.names('neuxmind-pkg'), [])


class StepFailures(InstallCase):
    def test_a_failed_install_step_shows_its_log_and_clean_takes_the_package_away(self):
        before = self.device.by_hand(start=False)          # someone's own copy in ~/neuxmind-pkg stays as it is
        self.device._run(['bash', self.device.local('neuxmind-pkg/device_install.sh'), 'remove'])
        self.device.control('fail-install')
        connection = self.connect_directly()
        view = self.install(connection)
        failure = view['failure']
        self.assertEqual((view['state'], failure['stage'], failure['code']), ('failed', 'install', 'install_step_failed'))
        self.assertIn('ERROR: Could not install packages', '\n'.join(failure['log']))
        self.assertEqual([s['state'] for s in view['stages']], ['done', 'done', 'failed', 'pending', 'pending'])
        self.assertEqual([item['path'] for item in view['left']], ['~/neuxmind-pkg/console/' + self.package.id])
        self.assertTrue(view['can_clean'])
        self.assertIn('清理', view['cleanup'])
        self.assertEqual(self.device.names('neuxmind'), [])           # the install script removed what it began
        view = self.clean(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.names('neuxmind-pkg'), sorted(before))
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)
        self.assertEqual(device_transfer.robot_unfinished_transfers(self.platform, connection['id']), [])
        self.assertFalse(self.robot(connection)['runtime_install']['can_clean'])

    def test_a_failed_start_leaves_the_install_which_clean_removes_because_this_console_made_it(self):
        self.device.control('fail-start')
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual((view['failure']['stage'], view['failure']['code']), ('start', 'install_step_failed'))
        self.assertIn('the adapter did not start', '\n'.join(view['failure']['log']))
        self.assertEqual([item['path'] for item in view['left']], ['~/neuxmind-pkg/console/' + self.package.id, '~/neuxmind'])
        self.assertIn('这次装的', view['left'][1]['what'])
        self.assertIn('停掉并删除这次装的 ~/neuxmind', view['cleanup'])
        view = self.clean(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.names('neuxmind-pkg'), [])
        self.assertIsNone(self.device.read('neuxmind/.neuxmind-device-install'))
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start', 'remove'])

    def test_a_failed_link_takes_the_link_away_when_cleaning(self):
        connection = self.connect_directly()
        self.device.control('hold-start')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'start')
        self.device.forwarding = False                   # the robot's SSH service stops forwarding meanwhile
        self.device.control('hold-start', False)
        view = self.wait(connection)
        self.assertEqual((view['failure']['stage'], view['failure']['code']), ('link', 'install_link_failed'))
        self.assertIn('正在运行', view['left'][1]['what'])
        view = self.clean(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        robot = self.robot(connection)
        self.assertNotIn('runtime_link', robot)
        self.assertEqual(robot['endpoint'], '')
        self.assertIsNone(self.device.runtime_port())

    def test_cleaning_never_removes_a_layer_this_console_did_not_install(self):
        before = self.device.by_hand()
        token = self.device.token()
        connection = self.connect_directly()
        self.device.forwarding = False
        self.assertEqual(self.install(connection)['failure']['code'], 'install_forwarding_off')
        with self.assertRaises(ValueError) as caught:
            self.clean(connection)
        self.assertEqual(caught.exception.code, 'install_nothing_to_clean')
        self.assertEqual(self.device.token(), token)
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)

    def test_an_edited_connection_stops_the_install(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        saved = self.store.get('device_connections', connection['id'])
        self.store.put('device_connections', {**saved, 'updated_at': '2099-01-01T00:00:00+00:00'})
        self.device.control('hold-install', False)
        view = self.wait(connection)
        self.assertEqual(view['failure']['code'], 'install_connection_changed')


class ReviewFindings(InstallCase):
    """What an independent review of the install code found (2026-11-22), each made into a check."""

    def test_a_package_file_that_changed_after_the_check_is_not_installed(self):
        connection = self.connect_directly()
        real = device_transfer.send_to_robot

        def changed(*args, **kwargs):
            return {**real(*args, **kwargs), 'sha256': '0' * 64}
        with mock.patch.object(device_transfer, 'send_to_robot', changed):
            view = self.install(connection)
        self.assertEqual((view['stage'], view['failure']['code']), ('upload', 'install_upload_failed'))
        self.assertEqual(self.device.runs(), [])

    def test_the_job_script_refuses_a_package_directory_reached_through_a_link(self):
        connection = self.connect_directly()
        self.device.write('elsewhere/' + self.package.id + '/device_install.sh', b'echo should-not-run > ran\n')
        self.device.link('neuxmind-pkg/console', 'elsewhere')
        session = self.session(connection)
        word, _, _ = session.run('job', self.package.id, 'install', dev.new_job_id(), 8790, 'sim_quadruped', seconds=20)
        self.assertEqual(word, 'nopackage')
        self.assertIsNone(self.device.read('elsewhere/' + self.package.id + '/ran'))
        self.assertEqual(session.run('clean', self.package.id, seconds=20)[0], 'moved')

    def test_a_start_cut_before_it_began_is_started_when_asked_again(self):
        connection = self.connect_directly()
        self.device.write(self.package_dir() + '/device_install.sh', self.package.path('script').read_bytes())
        job = dev.new_job_id()
        self.device.write(self.package_dir() + '/install.job', (job + '\n').encode())     # written, then the answer was cut
        session = self.session(connection)
        self.assertEqual(session.run('job', self.package.id, 'install', job, 8790, 'sim_quadruped', seconds=20)[0], 'started')
        self.assertEqual(session.run('job', self.package.id, 'install', job, 8790, 'sim_quadruped', seconds=20)[0], 'already')

    def test_saved_logins_in_what_the_robot_printed_are_hidden(self):
        self.device.control('fail-start')
        self.device.write('.fixture/fail-start', b'')
        connection = self.connect_directly()
        script = self.package.path('script')
        script.write_text(script.read_text().replace('the adapter did not start', 'login ' + self.device.password))
        self.package = runtime_package.write_manifest(self.package.directory, '0.4.0', '1.1.0', 'fixture')
        view = self.install(connection)
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(self.device.password, text)
        self.assertIn('[已隐藏]', '\n'.join(view['failure']['log']))

    def session(self, connection):
        login = lambda: device_access.SSHDevice(*self.platform.ssh_edit.saved_login(connection['id']))
        session = dev.DeviceSession(login, threading.Event())
        self.addCleanup(session.close)
        return session


class Interruptions(InstallCase):
    def test_a_connection_that_drops_during_the_upload_resumes_without_sending_it_all_again(self):
        connection = self.connect_directly()
        self.device.drops = [30000]
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        in_flight = max(entry['bytes'] for entry in self.package.files)     # at most the file under way is sent again
        self.assertLessEqual(self.device.received, self.package.total_bytes + in_flight)
        self.assert_online_and_linked(connection)

    def test_a_connection_that_drops_while_the_script_installs_does_not_stop_it(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        self.installing_on_the_robot()
        self.device.drop_sessions()
        self.device.control('hold-install', False)
        view = self.wait(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start'])   # installed once
        self.assert_online_and_linked(connection)

    def test_a_robot_gone_for_longer_than_the_install_waits_is_picked_up_again_later(self):
        self.installs.budgets['install'] = 2
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        self.installing_on_the_robot()
        self.device.offline = True
        self.device.drop_sessions()
        view = self.wait(connection)
        self.assertEqual((view['state'], view['stage']), ('failed', 'install'))
        self.assertEqual(view['failure']['code'], 'install_unreachable')
        self.assertIn('连不上设备', view['left'][0]['what'])
        self.device.control('hold-install', False)           # the robot finished the install meanwhile
        self.device.offline = False
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'start', 'link'])
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start'])
        self.assert_online_and_linked(connection)

    def test_stopping_leaves_the_robot_installing_and_the_next_install_waits_for_that(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        self.installing_on_the_robot()
        result = self.installs.stop({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.assertEqual(result['install']['state'], 'stopped')
        self.assertIn('~/neuxmind', [item['path'] for item in result['install']['left']])
        self.start(connection)
        view = self.wait(connection, until=lambda v: v['stage'] == 'install' and v['state'] == 'running')
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'wait_install', 'start', 'link'])
        self.device.control('hold-install', False)
        self.assertEqual(self.wait(connection)['state'], 'succeeded')
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start'])


if __name__ == '__main__':
    unittest.main()
