"""Issue #19: a robot that already has a management layer, or something else at ~/neuxmind.

Nothing of an install the console did not make is touched until a person chooses, and a directory the
install script did not make is never touched (``runtime_install_plan``)."""
import os
import unittest

from runtime_install_support import InstallCase


class ExistingTests(InstallCase):
    def untouched(self, before, token, running=True):
        """The install made by hand is as it was: its package files, its token, its runtime."""
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)
        self.assertEqual(self.device.token(), token)
        self.assertEqual(self.device.runtime_port(), 8790 if running else None)

    def test_a_running_layer_installed_by_hand_waits_for_a_choice_and_nothing_is_sent(self):
        before = self.device.by_hand()
        token, runs = self.device.token(), self.device.runs()
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['state'], 'needs_choice', view)
        self.assertEqual([o['id'] for o in view['choice']['options']], ['use', 'replace'])
        self.assertEqual(view['choice']['found'], {'root': 'installed', 'version': '0.4.0', 'console_package': '',
                                                    'running': True, 'running_port': 8790})
        self.assertEqual(self.device.names('neuxmind-pkg/console'), [])
        self.assertEqual([kind for kind, _ in self.device.install_runs], ['probe'])
        self.assertEqual(self.device.runs(), runs)
        self.untouched(before, token)
        self.assertNotIn('runtime_install', self.robot(connection))      # a question is not an outcome

    def test_using_it_links_it_and_changes_nothing_on_the_robot(self):
        before = self.device.by_hand()
        token, runs = self.device.token(), self.device.runs()
        connection = self.connect_directly()
        view = self.install(connection, existing='use')
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'link'])
        robot = self.assert_online_and_linked(connection)
        self.assertEqual(robot['token'], token)
        self.assertFalse(robot['runtime_install']['created_install'])
        self.assertEqual(self.device.names('neuxmind-pkg/console'), [])
        self.assertEqual(self.device.runs(), runs)
        self.untouched(before, token)

    def test_a_stopped_one_of_the_same_version_can_be_started_with_the_package_of_the_console(self):
        before = self.device.by_hand(start=False)
        token = self.device.token()
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual([o['id'] for o in view['choice']['options']], ['start', 'replace'])
        view = self.install(connection, existing='start')
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'start', 'link'])
        self.assert_online_and_linked(connection)
        self.assertEqual(self.device.runs()[-1], ('start', '8790', 'sim_quadruped'))
        self.assertEqual([command for command, *_ in self.device.runs()], ['install', 'start'])
        self.untouched(before, token)

    def test_a_stopped_one_of_another_version_can_only_be_replaced(self):
        self.device.by_hand(start=False, version='0.3.9')
        connection = self.connect_directly()
        self.assertEqual([o['id'] for o in self.install(connection)['choice']['options']], ['replace'])
        for choice in ('use', 'start'):
            view = self.install(connection, existing=choice)
            self.assertEqual((view['state'], view['failure']['code']), ('failed', 'install_choice_invalid'))
            self.assertEqual(view['left'], [])
        self.assertEqual(self.device.names('neuxmind-pkg/console'), [])

    def test_replacing_needs_a_confirmation_and_then_deletes_it_and_installs_the_package(self):
        before = self.device.by_hand()
        old_token = self.device.token()
        self.device.write('neuxmind/state/skills.db', b'what the old layer kept')
        connection = self.connect_directly()
        view = self.install(connection, existing='replace')
        self.assertEqual((view['state'], view['failure']['code']), ('failed', 'install_replace_unconfirmed'))
        self.untouched(before, old_token)
        view = self.install(connection, existing='replace', confirm_replace=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'remove', 'install', 'start', 'link'])
        robot = self.assert_online_and_linked(connection)
        self.assertNotEqual(self.device.token(), old_token)
        self.assertEqual(robot['token'], self.device.token())
        self.assertIsNone(self.device.read('neuxmind/state/skills.db'))
        self.assertEqual(self.device.read('neuxmind/.console-package').decode().strip(), self.package.id)
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start', 'remove', 'install', 'start'])
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)   # its own package stays

    def test_a_directory_the_install_script_did_not_make_is_never_touched(self):
        self.device.write('neuxmind/notes.txt', b'somebody else\'s files')
        connection = self.connect_directly()
        for fields in ({}, {'existing': 'replace', 'confirm_replace': True}):
            view = self.install(connection, **fields)
            self.assertEqual((view['state'], view['stage'], view['failure']['code']), ('failed', 'check', 'install_target_taken'))
            self.assertEqual((view['left'], view['can_clean']), ([], False))
        self.assertEqual(self.device.names('neuxmind'), ['notes.txt'])
        self.assertEqual(self.device.names('neuxmind-pkg'), [])

    def test_a_symbolic_link_in_its_place_is_not_followed(self):
        self.device.link('neuxmind', 'elsewhere/neuxmind')
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['failure']['code'], 'install_target_taken')
        self.assertTrue(os.path.islink(self.device.local('neuxmind')))
        self.assertEqual(self.device.names('elsewhere/neuxmind'), [])

    def test_an_earlier_install_of_this_console_is_picked_up_without_a_question(self):
        connection = self.connect_directly()
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        self.device._run(['bash', self.device.local(self.package_dir() + '/device_install.sh'), 'stop'])
        self.assertIsNone(self.device.runtime_port())
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'start', 'link'])
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start', 'stop', 'start'])
        view = self.install(connection)                     # running: only linked again
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'link'])
        self.assert_online_and_linked(connection)

    def test_an_unfinished_install_this_console_did_not_start_is_replaced_only_when_chosen(self):
        # Someone may be installing by hand right now: nothing is wiped without a choice and a confirmation.
        self.device.write('neuxmind/.neuxmind-device-install', b'state=installing\n')
        self.device.write('neuxmind/venv/half', b'interrupted')
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['state'], 'needs_choice', view)
        self.assertEqual([o['id'] for o in view['choice']['options']], ['replace'])
        self.assertEqual(self.device.read('neuxmind/venv/half'), b'interrupted')
        self.assertEqual(self.device.names('neuxmind-pkg'), [])
        self.assertEqual(self.install(connection, existing='replace')['failure']['code'], 'install_replace_unconfirmed')
        view = self.install(connection, existing='replace', confirm_replace=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertTrue(any('没装完' in note for note in view['notes']), view['notes'])
        self.assertIsNone(self.device.read('neuxmind/venv/half'))
        self.assert_online_and_linked(connection)

    def test_an_unfinished_install_of_this_package_is_done_again_without_a_question(self):
        connection = self.connect_directly()
        self.device.control('fail-install')
        self.install(connection)                        # this package's files and an install job are on the robot
        self.device.control('fail-install', False)
        # ... and the job was killed half way (the robot lost power): no exit file, its process is gone.
        directory = self.package_dir()
        os.remove(self.device.local(directory + '/install.exit'))
        self.device.write(directory + '/install.pid', b'999999\n')
        self.device.write('neuxmind/.neuxmind-device-install', b'state=installing\n')
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertTrue(any('没装完' in note for note in view['notes']), view['notes'])


if __name__ == '__main__':
    unittest.main()
