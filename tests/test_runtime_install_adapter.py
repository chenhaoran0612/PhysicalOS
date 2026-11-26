"""Issue #19 with issue #21's install script: the body the install recorded (``~/neuxmind/adapter``) is the one
the console's start runs; the console's guess from the card's body type only fills in for an install without that
record (from before #21), so an arm installed earlier is never started as a quadruped. Install itself always gets
the console's body: replacing an install gives the console's own one. The stand-in install script of
``install_fixture`` writes down the body each command was given; the real script is in
``test_runtime_install_real``."""
import os
import unittest

from runtime_install_support import InstallCase


class RecordedBodyTests(InstallCase):
    def stop_runtime(self):
        code, out, err = self.device._run(['sh', self.device.local(self.package_dir() + '/device_install.sh'), 'stop'])
        self.assertEqual(code, 0, (out, err))
        self.assertIsNone(self.device.runtime_port())

    def installed_and_stopped(self, family='quadruped', recorded=None):
        connection = self.connect_directly(family=family)
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.stop_runtime()
        if recorded is not None:
            self.device.write('neuxmind/adapter', recorded)
        return connection

    def started_again(self, connection):
        """The console's own install, stopped, is started again: the body its start was given."""
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'start', 'link'])
        self.assert_online_and_linked(connection)
        command, port, body = self.device.runs()[-1]
        self.assertEqual((command, port), ('start', '8790'))
        return body

    def test_an_arm_recorded_at_install_is_started_as_an_arm_from_a_quadruped_card(self):
        connection = self.installed_and_stopped(recorded=b'sim_arm\n')
        self.assertEqual(self.started_again(connection), 'sim_arm')

    def test_a_ros2_install_is_started_with_ros2_not_the_simulated_body(self):
        connection = self.installed_and_stopped(recorded=b'ros2\n')
        self.assertEqual(self.started_again(connection), 'ros2')

    def test_a_quadruped_recorded_at_install_wins_over_an_arm_card(self):
        connection = self.installed_and_stopped(family='manipulator', recorded=b'sim_quadruped\n')
        self.assertEqual(self.started_again(connection), 'sim_quadruped')

    def test_an_arm_installed_before_the_record_existed_is_still_started_as_an_arm(self):
        connection = self.installed_and_stopped(family='manipulator')
        os.remove(self.device.local('neuxmind/adapter'))       # an install from before issue #21 has no record
        self.assertIsNone(self.device.read('neuxmind/adapter'))
        self.assertEqual(self.started_again(connection), 'sim_arm')

    def test_a_quadruped_without_a_record_gets_the_card_s_body(self):
        connection = self.installed_and_stopped()
        os.remove(self.device.local('neuxmind/adapter'))
        self.assertEqual(self.started_again(connection), 'sim_quadruped')

    def test_a_record_that_names_no_known_body_or_is_a_link_is_not_followed(self):
        connection = self.installed_and_stopped(recorded=b'no_such_adapter\n')
        self.assertEqual(self.started_again(connection), 'sim_quadruped')
        self.stop_runtime()
        for name in ('sim_arm ', 'sim_arm\r', 'SIM_ARM', ''):
            with self.subTest(record=name):
                self.device.write('neuxmind/adapter', name.encode() + b'\n')
                self.assertEqual(self.started_again(connection), 'sim_quadruped')
                self.stop_runtime()
        self.device.write('elsewhere', b'ros2\n')
        os.remove(self.device.local('neuxmind/adapter'))
        os.symlink(self.device.local('elsewhere'), self.device.local('neuxmind/adapter'))
        self.assertEqual(self.started_again(connection), 'sim_quadruped')

    def test_starting_a_stopped_install_made_by_hand_runs_its_recorded_body(self):
        before = self.device.by_hand(start=False)
        self.device.write('neuxmind/adapter', b'sim_arm\n')
        connection = self.connect_directly()
        self.assertEqual([o['id'] for o in self.install(connection)['choice']['options']], ['start', 'replace'])
        view = self.install(connection, existing='start')
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.runs()[-1], ('start', '8790', 'sim_arm'))
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)

    def test_replacing_an_install_gives_the_console_s_body_not_the_old_record(self):
        self.device.by_hand(start=False)
        self.device.write('neuxmind/adapter', b'ros2\n')
        connection = self.connect_directly()
        view = self.install(connection, existing='replace', confirm_replace=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.runs()[-3:], [('remove', '8790', 'sim_quadruped'), ('install', '8790', 'sim_quadruped'),
                                                   ('start', '8790', 'sim_quadruped')])


if __name__ == '__main__':
    unittest.main()
