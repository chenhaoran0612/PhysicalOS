"""Issue #38: a robot's body for the management layer. A real body (the robot's own ROS 2) is installed with
the ROS settings a person saved, its device configuration (the places it knows) reaches the install
directory, a changed configuration is put there with a restart, and an install made for the other kind
of body is only ever replaced. The robot is the stand-in of ``install_fixture``; nothing reaches a machine."""
import json
import os
import stat
import unittest

from brainos import robot_body, runtime_install_plan as planning
from brainos.errors import CodedError
from runtime_install_support import InstallCase

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, '..', 'runtime', 'examples', 'ros2', 'onbody-programs.example.json'), encoding='utf-8') as handle:
    EXAMPLE = handle.read()
SETTINGS = {'ROS_DOMAIN_ID': '24', 'ROS_LOCALHOST_ONLY': '0', 'RMW_IMPLEMENTATION': 'rmw_zenoh_cpp',
            'ZENOH_SESSION_CONFIG_URI': '/etc/robot/zenoh_session.json5'}


def with_place(text, name):
    """The example configuration with one more place the robot knows."""
    config = json.loads(text)
    places = config['programs']['places']
    places[name] = json.loads(json.dumps(next(iter(places.values()))))
    return json.dumps(config, ensure_ascii=False)


class BodyRulesTests(unittest.TestCase):
    def test_settings_keep_only_what_the_install_script_takes(self):
        cleaned = robot_body.clean_settings({'ROS_DOMAIN_ID': ' 024 ', 'RMW_IMPLEMENTATION': 'rmw_zenoh_cpp',
                                             'ROS_LOCALHOST_ONLY': '', 'CYCLONEDDS_URI': None})
        self.assertEqual(cleaned, {'ROS_DOMAIN_ID': '24', 'RMW_IMPLEMENTATION': 'rmw_zenoh_cpp'})
        for bad in ({'ROS_DOMAIN_ID': '233'}, {'ROS_DOMAIN_ID': '-1'}, {'ROS_LOCALHOST_ONLY': '2'},
                    {'RMW_IMPLEMENTATION': 'zenoh; rm -rf ~'}, {'ZENOH_SESSION_CONFIG_URI': 'relative.json5'},
                    {'ZENOH_SESSION_CONFIG_URI': '/opt/a b.json5'}, {'FASTRTPS_DEFAULT_PROFILES_FILE': '/x\n/y'},
                    {'CYCLONEDDS_URI': '<CycloneDDS/>'}, {'LD_PRELOAD': '/tmp/x.so'}, ['ROS_DOMAIN_ID']):
            with self.assertRaises(CodedError, msg=bad) as raised:
                robot_body.clean_settings(bad)
            self.assertEqual(raised.exception.code, 'body_settings_invalid')

    def test_the_setup_file_is_an_absolute_plain_path(self):
        self.assertEqual(robot_body.clean_setup(''), '/opt/ros/humble/setup.bash')
        self.assertEqual(robot_body.clean_setup(' /opt/robot/install/setup.bash '), '/opt/robot/install/setup.bash')
        for bad in ('setup.bash', '/opt/ros/$(id)/setup.bash', '/opt/ros/../etc/passwd', '/opt/ros/a b/setup.bash'):
            with self.assertRaises(CodedError, msg=bad):
                robot_body.clean_setup(bad)

    def test_a_configuration_is_checked_with_the_adapters_own_rules(self):
        config, offered = robot_body.clean_config(EXAMPLE)
        self.assertEqual(offered['places'], ['出发点', '会议室', '二楼'])
        self.assertIn('base.navigate_to_place', offered['capabilities'])
        self.assertTrue(offered['explore'])
        self.assertEqual(robot_body.clean_config(robot_body.config_text(config))[0], config)
        self.assertEqual(robot_body.clean_config('  '), (None, None))
        for bad in ('{not json', '[]', '{"programs": {"places": {}}}', '{"topics": {"cmd_vel": 7}}',
                    '{"simulated": true}', '{"use_sim_time": true}',
                    '{"programs": {"workdir": "/w", "places": {"a": {"steps": [{"title": "t", "argv": ["x"]}]}}}}'):
            with self.assertRaises(CodedError, msg=bad) as raised:
                robot_body.clean_config(bad)
            self.assertEqual(raised.exception.code, 'body_config_invalid')
            self.assertIn('本体配置有问题', str(raised.exception))
        with self.assertRaises(CodedError):
            robot_body.clean_config('{"name": "%s"}' % ('x' * robot_body.CONFIG_MAX_BYTES))

    def test_the_body_file_names_the_setup_the_settings_and_whether_a_configuration_comes(self):
        body = {'setup': '/opt/ros/humble/setup.bash', 'settings': {'RMW_IMPLEMENTATION': 'rmw_zenoh_cpp', 'ROS_DOMAIN_ID': '24'},
                'config': None}
        self.assertEqual(robot_body.body_env(body), 'NX_ROS_SETUP=/opt/ros/humble/setup.bash\nROS_DOMAIN_ID=24\n'
                                                    'RMW_IMPLEMENTATION=rmw_zenoh_cpp\nNX_BODY_CONFIG=no\n')
        self.assertTrue(robot_body.body_env({**body, 'config': {}}).endswith('NX_BODY_CONFIG=yes\n'))

    def test_how_the_saved_body_relates_to_the_robot(self):
        body = {'kind': 'ros2', 'setup': '/opt/ros/humble/setup.bash', 'settings': {'ROS_DOMAIN_ID': '24'}, 'config': {'name': 'a'}}
        marks = robot_body.digests(robot_body.of({'body': body}))
        self.assertEqual(robot_body.state_of({}), 'sim')
        self.assertEqual(robot_body.state_of({'body': body}), 'unset')
        self.assertEqual(robot_body.state_of({'body': body, 'body_applied': {'kind': 'sim'}}), 'reinstall')
        self.assertEqual(robot_body.state_of({'body': body, 'body_applied': {'kind': 'ros2', **marks}}), 'current')
        self.assertEqual(robot_body.state_of({'body': {**body, 'config': {'name': 'b'}},
                                              'body_applied': {'kind': 'ros2', **marks}}), 'config_changed')
        self.assertEqual(robot_body.state_of({'body': {**body, 'settings': {'ROS_DOMAIN_ID': '25'}},
                                              'body_applied': {'kind': 'ros2', **marks}}), 'reinstall')
        self.assertEqual(robot_body.state_of({'body': body, 'body_applied': {'kind': 'ros2', 'install': None, 'config': None}}),
                         'unknown')
        self.assertEqual(robot_body.state_of({'body': {'kind': 'sim'}, 'body_applied': {'kind': 'ros2', **marks}}), 'reinstall')

    def test_an_install_for_the_other_kind_of_body_differs(self):
        self.assertFalse(planning.body_differs({'adapter': 'sim_quadruped'}, 'sim_arm'))
        self.assertFalse(planning.body_differs({'adapter': ''}, 'sim_quadruped'))
        self.assertFalse(planning.body_differs({'adapter': 'ros2'}, 'ros2'))
        self.assertTrue(planning.body_differs({'adapter': ''}, 'ros2'))
        self.assertTrue(planning.body_differs({'adapter': 'ros2'}, 'sim_quadruped'))
        self.assertFalse(planning.body_differs({'adapter': 'ros2'}, None))

    def test_what_the_robot_reported_is_only_used_when_it_is_usable(self):
        keys = {'setups': '/opt/ros/humble/setup.bash /opt/ros/$(x)/setup.bash', 'run_count': '37',
                'run_ros_domain_id': '24', 'run_rmw_implementation': 'rmw_zenoh_cpp', 'run_cyclonedds_uri': '<xml/>',
                'root': 'installed', 'adapter': 'ros2', 'rec_ros_setup': '/opt/ros/humble/setup.bash',
                'rec_ros_domain_id': '24', 'config_bytes': '12', 'config_hex': '{"name":"狗"}'.encode().hex()}
        found = robot_body.read_probe(keys)
        self.assertEqual(found['setups'], ['/opt/ros/humble/setup.bash'])
        self.assertEqual(found['running'], {'count': 37, 'settings': {'ROS_DOMAIN_ID': '24', 'RMW_IMPLEMENTATION': 'rmw_zenoh_cpp'}})
        self.assertEqual(found['installed']['adapter'], 'ros2')
        self.assertEqual(found['installed']['settings'], {'ROS_DOMAIN_ID': '24'})
        self.assertEqual(found['installed']['config_text'], '{"name":"狗"}')
        broken = robot_body.read_probe({**keys, 'config_hex': '{"programs": 3}'.encode().hex()})
        self.assertIsNone(broken['installed']['config_text'])
        self.assertIsNone(robot_body.read_probe({'root': 'none'})['installed'])


class BodyInstallTests(InstallCase):
    def save(self, connection, kind='ros2', settings=SETTINGS, config=EXAMPLE, **more):
        return self.platform.robot_body.save({'project_id': self.pid, 'robot_id': connection['robot_id'], 'kind': kind,
                                              'settings': settings, 'config_text': config, **more})

    def apply(self, connection):
        self.installs.apply_body({'project_id': self.pid, 'robot_id': connection['robot_id']})
        return self.wait(connection)

    def state(self, connection):
        return robot_body.state_of(self.robot(connection))

    def install_env(self):
        text = (self.device.read('.fixture/install-env') or b'').decode()
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def device_config(self):
        return json.loads(self.device.read('neuxmind/ros2-config.json').decode())

    def test_a_real_body_is_installed_with_the_saved_settings_and_its_configuration(self):
        connection = self.connect_directly()
        saved = self.save(connection)
        self.assertEqual(saved['declares']['places'], ['出发点', '会议室', '二楼'])
        self.assertEqual(saved['body']['state'], 'unset')
        self.assertEqual(self.device.install_runs, [])                     # saving reaches no robot
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(view['adapter'], 'ros2')
        self.assertEqual(self.device.runs(), [('install', '8790', 'ros2'), ('start', '8790', 'ros2')])
        self.assertEqual(self.install_env(), {'NX_ROS_SETUP': '/opt/ros/humble/setup.bash', **SETTINGS})
        self.assertEqual(self.device_config(), json.loads(EXAMPLE))
        for name in ('neuxmind/ros2-config.json', self.package_dir() + '/body.env', self.package_dir() + '/ros2-config.json'):
            self.assertEqual(stat.S_IMODE(os.stat(self.device.local(name)).st_mode), 0o600, name)
        self.assertEqual(self.device.read('neuxmind/adapter'), b'ros2\n')
        self.assert_online_and_linked(connection)
        self.assertEqual(self.state(connection), 'current')
        self.assertEqual(self.robot(connection)['runtime_install']['adapter'], 'ros2')

    def test_a_real_body_without_a_configuration_sends_none(self):
        connection = self.connect_directly()
        self.save(connection, settings={}, config='')
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        self.assertEqual(self.install_env(), {'NX_ROS_SETUP': '/opt/ros/humble/setup.bash'})
        self.assertIsNone(self.device.read('neuxmind/ros2-config.json'))
        self.assertIsNone(self.device.read(self.package_dir() + '/ros2-config.json'))
        self.assertEqual(self.state(connection), 'current')

    def test_the_settings_of_the_login_do_not_reach_a_real_install(self):
        connection = self.connect_directly()
        self.save(connection, settings={'ROS_DOMAIN_ID': '7'}, config='')
        environment = self.device.environment

        def with_login_settings():
            return {**environment(), 'ROS_DOMAIN_ID': '99', 'RMW_IMPLEMENTATION': 'rmw_other_cpp', 'CYCLONEDDS_URI': '/tmp/x.xml'}
        self.device.environment = with_login_settings
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        self.assertEqual(self.install_env(), {'NX_ROS_SETUP': '/opt/ros/humble/setup.bash', 'ROS_DOMAIN_ID': '7'})

    def test_a_changed_configuration_is_put_on_the_robot_with_a_restart(self):
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        changed = with_place(EXAMPLE, '前台')
        self.assertEqual(self.save(connection, config=changed)['body']['state'], 'config_changed')
        self.assertEqual(self.device_config(), json.loads(EXAMPLE))           # nothing on the robot yet
        before = len(self.device.runs())
        view = self.apply(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(view['mode'], 'body')
        self.assertEqual([stage['id'] for stage in view['stages']], ['check', 'upload', 'restart', 'link'])
        self.assertEqual(self.device.runs()[before:], [('stop', '8790', 'ros2'), ('start', '8790', 'ros2')])
        self.assertIn('前台', self.device_config()['programs']['places'])
        self.assertEqual(self.state(connection), 'current')
        robot = self.assert_online_and_linked(connection)
        self.assertEqual(robot['runtime_install']['mode'], 'install')          # the install's own outcome is kept
        self.assertEqual(robot['body_apply']['state'], 'succeeded')

    def test_changed_ros_settings_need_a_new_install(self):
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        self.assertEqual(self.save(connection, settings={**SETTINGS, 'ROS_DOMAIN_ID': '25'})['body']['state'], 'reinstall')
        with self.assertRaises(CodedError) as raised:
            self.installs.apply_body({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.assertEqual(raised.exception.code, 'body_apply_reinstall')
        view = self.install(connection)                                        # the install for the same kind is our own: linked
        self.assertEqual(view['state'], 'succeeded')
        self.assertEqual(self.state(connection), 'reinstall')                  # and still made with the old settings

    def test_a_simulated_install_is_only_replaced_by_a_real_one_after_a_choice(self):
        connection = self.connect_directly()
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        self.assertEqual(self.device.read('neuxmind/adapter'), b'sim_quadruped\n')
        self.save(connection)
        self.assertEqual(self.state(connection), 'reinstall')
        runs = len(self.device.runs())
        view = self.install(connection)
        self.assertEqual(view['state'], 'needs_choice', view)
        self.assertEqual([option['id'] for option in view['choice']['options']], ['replace'])
        self.assertTrue(view['choice']['found']['other_body'])
        self.assertEqual(len(self.device.runs()), runs)                        # nothing ran
        self.assertEqual(self.install(connection, existing='replace')['failure']['code'], 'install_replace_unconfirmed')
        view = self.install(connection, existing='replace', confirm_replace=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.runs()[runs:], [('remove', '8790', 'ros2'), ('install', '8790', 'ros2'), ('start', '8790', 'ros2')])
        self.assertEqual(self.device.read('neuxmind/adapter'), b'ros2\n')
        self.assertEqual(self.state(connection), 'current')

    def test_a_real_install_is_only_replaced_by_a_simulated_one_after_a_choice(self):
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        self.save(connection, kind='sim')
        view = self.install(connection)
        self.assertEqual(view['state'], 'needs_choice', view)
        self.assertEqual([option['id'] for option in view['choice']['options']], ['replace'])
        view = self.install(connection, existing='replace', confirm_replace=True)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertEqual(self.device.read('neuxmind/adapter'), b'sim_quadruped\n')
        self.assertEqual(self.state(connection), 'sim')

    def test_a_cleared_configuration_is_taken_off_the_robot(self):
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        self.assertEqual(self.save(connection, config='')['body']['state'], 'config_changed')
        self.assertEqual(self.apply(connection)['state'], 'succeeded')
        self.assertIsNone(self.device.read('neuxmind/ros2-config.json'))       # the one this console put there
        self.assertEqual(self.state(connection), 'current')
        self.assertFalse([name for name in self.device.names('neuxmind') if '.nx' in name])

    def test_a_configuration_someone_else_put_there_is_not_taken_away(self):
        connection = self.connect_directly()
        self.save(connection, config='')
        self.install(connection)
        self.device.write('neuxmind/ros2-config.json', b'{"name": "by hand"}\n')
        self.assertEqual(self.apply(connection)['state'], 'succeeded')
        self.assertEqual(self.device.read('neuxmind/ros2-config.json'), b'{"name": "by hand"}\n')

    def test_a_restart_that_fails_goes_back_to_the_configuration_before(self):
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        self.save(connection, config=with_place(EXAMPLE, '前台'))
        self.device.control('fail-start-once')
        view = self.apply(connection)
        self.assertEqual((view['state'], view['failure']['stage'], view['failure']['code']), ('failed', 'restart', 'install_step_failed'), view)
        self.assertIn('going back to the earlier one', '\n'.join(view['failure']['log']))
        self.assertEqual(self.device_config(), json.loads(EXAMPLE))            # the old file is back
        self.assertEqual(self.device.runtime_port(), 8790)                     # and the layer runs with it
        self.assertEqual(self.state(connection), 'config_changed')             # nothing is claimed to be applied
        self.assertFalse([name for name in self.device.names('neuxmind') if '.nx' in name])
        self.assertEqual(self.apply(connection)['state'], 'succeeded')         # the next try puts it there
        self.assertIn('前台', self.device_config()['programs']['places'])
        self.assertEqual(self.state(connection), 'current')

    def test_an_install_made_with_other_ros_settings_is_not_given_a_configuration(self):
        """What the robot recorded counts, not what this console remembers: a link-only install is compared too."""
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        recorded = self.device.read('neuxmind/ros2.env').decode().replace('ROS_DOMAIN_ID=24', 'ROS_DOMAIN_ID=30')
        self.device.write('neuxmind/ros2.env', recorded.encode())
        robot = self.robot(connection)
        robot.pop('body_applied')
        self.store.put('robots', robot)
        self.assertEqual(self.install(connection)['state'], 'succeeded')       # linked as it is
        self.assertEqual(self.state(connection), 'reinstall')
        robot = self.robot(connection)
        robot.pop('body_applied')                                              # even a console that knows nothing of it
        self.store.put('robots', robot)
        self.save(connection, config=with_place(EXAMPLE, '前台'))
        runs = len(self.device.runs())
        view = self.apply(connection)
        self.assertEqual(view['failure']['code'], 'body_apply_reinstall', view)
        self.assertIn('从机器人读取', view['failure']['message'])
        self.assertEqual(len(self.device.runs()), runs)
        self.assertEqual(self.device_config(), json.loads(EXAMPLE))

    def test_applying_needs_a_real_install_of_this_console(self):
        connection = self.connect_directly()
        with self.assertRaises(CodedError) as raised:
            self.installs.apply_body({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.assertEqual(raised.exception.code, 'body_apply_not_real')
        self.save(connection)
        view = self.apply(connection)
        self.assertEqual(view['state'], 'failed')
        self.assertEqual(view['failure']['code'], 'body_apply_no_install')
        self.assertEqual(self.device.runs(), [])
        self.assertIsNone(self.device.read(self.package_dir() + '/body.env'))  # the check only reads
        self.assertNotIn('runtime_install', self.robot(connection))

    def test_a_real_install_of_another_console_package_takes_a_configuration_too(self):
        """After the console was upgraded its package has another id; the layer on the robot is the same version."""
        connection = self.connect_directly()
        self.save(connection)
        self.install(connection)
        self.device.write('neuxmind/.console-package', b'runtime-0.4.0-000000000000\n')
        self.save(connection, config=with_place(EXAMPLE, '前台'))
        view = self.apply(connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertIn('前台', self.device_config()['programs']['places'])
        # ... but not one of another runtime version: its install script and wheels are not this console's.
        robot = self.robot(connection)
        info = next(name for name in os.listdir(self.device.local('neuxmind/venv/lib/python3.10/site-packages')))
        os.rename(self.device.local('neuxmind/venv/lib/python3.10/site-packages/' + info),
                  self.device.local('neuxmind/venv/lib/python3.10/site-packages/neuxmind_runtime-0.3.0.dist-info'))
        runs = len(self.device.runs())
        view = self.apply(connection)
        self.assertEqual(view['failure']['code'], 'body_apply_no_install', view)
        self.assertIn('0.3.0', view['failure']['message'])
        self.assertEqual(len(self.device.runs()), runs)
        self.assertEqual(self.robot(connection)['runtime_install'], robot['runtime_install'])

    def test_a_real_install_found_on_the_robot_is_linked_and_what_it_runs_with_is_unknown(self):
        """The by-hand case: someone installed the real body on the robot; the console adopts what is there."""
        first = self.connect_directly()
        self.save(first)
        self.install(first)
        found = self.platform.robot_body.detect({'project_id': self.pid, 'robot_id': first['robot_id']})
        self.assertEqual(found['installed']['adapter'], 'ros2')
        self.assertEqual(found['installed']['setup'], '/opt/ros/humble/setup.bash')
        self.assertEqual(found['installed']['settings'], SETTINGS)
        self.assertEqual(json.loads(found['installed']['config_text']), json.loads(EXAMPLE))
        self.assertEqual(found['setups'], [])
        # A record that knows nothing of what was put there (another console, or by hand): linked, state unknown.
        robot = self.robot(first)
        robot.pop('body_applied')
        self.store.put('robots', robot)
        runs = len(self.device.runs())
        self.assertEqual(self.install(first)['state'], 'succeeded')
        self.assertEqual(len(self.device.runs()), runs)
        self.assertEqual(self.state(first), 'unknown')
        self.assertEqual(self.apply(first)['state'], 'succeeded')              # "应用到机器人" makes it known
        self.assertEqual(self.state(first), 'current')

    def test_detect_reads_and_changes_nothing(self):
        connection = self.connect_directly()
        found = self.platform.robot_body.detect({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.assertEqual(found, {'robot_id': connection['robot_id'], 'setups': [], 'running': {'count': 0, 'settings': {}},
                                 'installed': None})
        self.assertEqual([kind for kind, _ in self.device.install_runs], ['rosprobe'])
        self.assertEqual(self.device.runs(), [])
        self.assertFalse(os.path.exists(self.device.local('neuxmind')))
        self.assertFalse(os.path.exists(self.device.local('neuxmind-pkg')))

    def test_a_bad_body_is_refused_and_nothing_is_saved(self):
        connection = self.connect_directly()
        for fields, code in (({'kind': 'real'}, 'body_settings_invalid'), ({'settings': {'ROS_DOMAIN_ID': 'x'}}, 'body_settings_invalid'),
                             ({'setup': 'setup.bash'}, 'body_settings_invalid'), ({'config': '{"programs": 1}'}, 'body_config_invalid')):
            with self.assertRaises(CodedError, msg=fields) as raised:
                self.save(connection, **fields)
            self.assertEqual(raised.exception.code, code)
        self.assertNotIn('body', self.robot(connection))
        checked = self.platform.robot_body.check({'project_id': self.pid, 'robot_id': connection['robot_id'], 'config_text': EXAMPLE})
        self.assertEqual(checked['declares']['places'], ['出发点', '会议室', '二楼'])
        self.assertNotIn('body', self.robot(connection))


if __name__ == '__main__':
    unittest.main()
