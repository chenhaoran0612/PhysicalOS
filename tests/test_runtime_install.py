"""Issue #19 (R1-03): from adding a robot to its connection state "在线", all in the console.

The robot is ``install_fixture.InstallDevice``: SSH, SFTP and the product's scripts run for real in a
temporary directory; its install script is a stand-in (``fake_package``). The real install script and
runtime are covered by ``test_runtime_install_real.py`` (Linux)."""
import unittest
from unittest import mock

from brainos import connector, runtime_install
from runtime_install_support import InstallCase


class InstallTests(InstallCase):
    def test_a_robot_added_over_ssh_gets_its_management_layer_and_is_online(self):
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual([stage['id'] for stage in view['stages']], ['check', 'upload', 'install', 'start', 'link'])
        self.assertTrue(all(stage['state'] == 'done' for stage in view['stages']), view['stages'])
        robot = self.assert_online_and_linked(connection)
        # The token the install script made on the robot is the one the console saved and sends.
        token = self.device.token()
        self.assertEqual(robot['token'], token)
        layer = self.device.layer(8790)
        self.assertIn('GET /v1/manifest', layer.calls)
        self.assertEqual(set(layer.tokens), {'Bearer ' + token})
        self.assertEqual(connector.call(robot['endpoint'], '/v1/status', robot['token'])['instance_id'], layer.instance)
        # What is on the robot: the package where the console put it, the install the script made.
        self.assertEqual(self.device.names(self.package_dir()),
                         sorted([*(entry['name'] for entry in self.package.files), 'install.exit', 'install.job',
                                 'install.log', 'install.pid', 'start.exit', 'start.job', 'start.log', 'start.pid']))
        self.assertEqual(self.device.read(self.package_dir() + '/device_install.sh'), self.package.path('script').read_bytes())
        self.assertEqual(self.device.mode(self.package_dir() + '/device_install.sh'), 0o755)
        self.assertEqual(self.device.read('neuxmind/.console-package').decode().strip(), self.package.id)
        self.assertEqual(self.device.runs(), [('install', '8790', 'sim_quadruped'), ('start', '8790', 'sim_quadruped')])
        # The outcome stays with the robot; the progress does not go into the workspace state.
        outcome = robot['runtime_install']
        self.assertEqual((outcome['state'], outcome['package_id'], outcome['created_install']), ('succeeded', self.package.id, True))
        self.assertEqual(outcome['left'], [])
        self.assertNotIn('progress', outcome)
        self.assert_no_token_anywhere(token, view)

    def test_through_a_jump_host_too(self):
        jump = self.jump_host()
        connection = self.connect_through_jump(jump)
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_online_and_linked(connection)
        self.assertEqual(self.device.runs(), [('install', '8790', 'sim_quadruped'), ('start', '8790', 'sim_quadruped')])
        self.assertTrue(jump.forwarded, 'every SSH session to the robot went through the jump host')

    def test_another_port_and_a_manipulator_get_their_own_port_and_simulated_arm(self):
        connection = self.connect_directly(family='manipulator')
        view = self.install(connection, port=8795)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assert_online_and_linked(connection, port=8795)
        self.assertEqual(self.device.runs(), [('install', '8795', 'sim_arm'), ('start', '8795', 'sim_arm')])

    def test_the_progress_of_the_upload_is_reported_while_it_runs(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        view = self.wait(connection, until=lambda v: v['stage'] == 'install')
        self.assertEqual(view['progress']['sent'], self.package.total_bytes)
        self.assertEqual(view['progress']['total'], self.package.total_bytes)
        self.assertEqual([s['state'] for s in view['stages']], ['done', 'done', 'running', 'pending', 'pending'])
        self.device.control('hold-install', False)
        self.assertEqual(self.wait(connection)['state'], 'succeeded')

    def test_the_workspace_state_is_not_redrawn_by_the_progress(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        first = self.platform.snapshot()['robots']
        second = self.platform.snapshot()['robots']
        self.assertEqual(first, second)
        self.assertNotIn('runtime_install', first[0])          # only an ended install leaves its outcome
        self.device.control('hold-install', False)
        self.wait(connection)

    def test_a_second_install_while_one_runs_is_refused_and_so_is_deleting_the_robot(self):
        connection = self.connect_directly()
        self.device.control('hold-install')
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install')
        with self.assertRaises(ValueError) as caught:
            self.start(connection)
        self.assertEqual(caught.exception.code, 'install_in_progress')
        with self.assertRaises(ValueError) as caught:
            self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.assertEqual(str(caught.exception), runtime_install.INSTALLING)
        self.device.control('hold-install', False)
        self.assertEqual(self.wait(connection)['state'], 'succeeded')
        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

    def test_only_robots_reached_over_ssh_and_known_body_types(self):
        sim = self.platform.add_robot({'project_id': self.pid, 'name': 'Typed address', 'family': 'quadruped'})
        with self.assertRaises(ValueError):
            self.installs.start({'project_id': self.pid, 'robot_id': sim['id']})
        connection = self.connect_directly()
        with mock.patch.dict(runtime_install.ADAPTERS, {}, clear=True):    # a body type without a simulated body
            with self.assertRaises(ValueError) as caught:
                self.start(connection)
        self.assertEqual(caught.exception.code, 'install_body_unsupported')
        for port in (80, 70000, '8790', True):
            with self.assertRaises(ValueError):
                self.start(connection, port=port)
        with self.assertRaises(ValueError) as caught:
            self.start(connection, existing='upgrade')
        self.assertEqual(caught.exception.code, 'install_choice_invalid')
        self.assertEqual(self.device.install_runs, [])

    def test_without_its_package_the_console_says_so_and_contacts_nobody(self):
        connection = self.connect_directly()
        (self.package.directory / 'package.json').unlink()
        with self.assertRaises(ValueError) as caught:
            self.start(connection)
        self.assertEqual(caught.exception.code, 'install_package_missing')
        self.assertEqual(self.device.install_runs, [])

    def test_the_link_of_issue_17_can_be_used_by_the_rest_of_the_console_afterwards(self):
        connection = self.connect_directly()
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        robot = self.robot(connection)
        self.platform.connect_robot({'robot_id': robot['id']})          # "连接检查" on the card
        self.assertEqual(self.robot(connection)['status'], 'connected')
        self.assertEqual(self.platform.runtime_link.overview({})['links'][robot['id']]['state'], 'up')


if __name__ == '__main__':
    unittest.main()
