"""Issue #19 with the real install package: the real runtime/deploy/device_install.sh, the two real wheels,
and the real runtime it starts. Linux only (the install script needs flock, setsid, GNU realpath and
/proc); run in a throwaway container, never on a robot:

    docker run --rm -v "$PWD":/src:ro -v <package dir>:/package:ro -e NEUXMIND_TEST_REAL_PACKAGE=/package \\
      <python 3.10+ image with paramiko> sh -c 'cp -r /src /work && cd /work && \\
      PYTHONPATH=.:spec:runtime:sdk:tests python -m unittest tests.test_runtime_install_real -v'

``NEUXMIND_TEST_REAL_PACKAGE`` is a package built by ``scripts/build_device_package.py`` (on a computer
with setuptools); without it the test builds one if it can. The "robot" is ``install_fixture.InstallDevice``
in real mode: its SSH service runs the product's scripts with this machine's tools in a temporary home
directory, and the runtime the install script starts listens on this machine's 127.0.0.1."""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest

from brainos import connector, runtime_install, runtime_package
from runtime_install_support import InstallCase

REAL = sys.platform.startswith('linux') and all(shutil.which(tool) for tool in ('bash', 'flock', 'setsid', 'realpath'))
_BUILT = {}


def real_package():
    if os.environ.get('NEUXMIND_TEST_REAL_PACKAGE'):
        return runtime_package.load(os.environ['NEUXMIND_TEST_REAL_PACKAGE'])
    if 'package' not in _BUILT:
        try:
            import setuptools  # noqa: F401
        except ImportError:
            raise unittest.SkipTest('no NEUXMIND_TEST_REAL_PACKAGE and no setuptools to build one') from None
        _BUILT['dir'] = tempfile.TemporaryDirectory()
        _BUILT['package'] = runtime_package.build(os.path.join(_BUILT['dir'].name, 'package'))
    return _BUILT['package']


def listen(port):
    """Another program listening on this machine's 127.0.0.1:<port>."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen()
    return listener


@unittest.skipUnless(REAL, 'Linux with bash, flock, setsid and GNU realpath only')
class RealInstallTests(InstallCase):
    real = True

    def setUp(self):
        super().setUp()
        self.installs.budgets.update(install=300, start=120, remove=120, link=60, clean=120)

    def make_package(self, directory):
        return real_package()

    def listening(self, port):
        """Addresses this machine listens on for ``port`` (IPv4, from /proc/net/tcp)."""
        found = []
        with open('/proc/net/tcp') as table:
            for line in list(table)[1:]:
                local, state = line.split()[1], line.split()[3]
                address, hex_port = local.split(':')
                if state == '0A' and int(hex_port, 16) == port:
                    found.append('.'.join(str(int(address[i:i + 2], 16)) for i in (6, 4, 2, 0)))
        return found

    def assert_real_layer(self, connection):
        robot = self.assert_online_and_linked(connection)
        manifest = connector.call(robot['endpoint'], '/v1/manifest', robot['token'])
        self.assertTrue(manifest['simulated'])
        self.assertEqual(manifest['family'], 'quadruped')
        self.assertEqual(self.listening(8790), ['127.0.0.1'])
        token_file = self.device.local('neuxmind/edge-token')
        self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
        return robot

    def test_the_real_install_script_installs_starts_and_the_console_reaches_the_real_runtime(self):
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        robot = self.assert_real_layer(connection)
        self.assertEqual(robot['token'], self.device.token())
        self.assertEqual(self.device.read('neuxmind/.console-package').decode().strip(), self.package.id)
        log = (self.device.read(self.package_dir() + '/install.log') or b'').decode()
        self.assertIn('token file:', log)
        self.assertNotIn(self.device.token(), log)
        self.assert_no_token_anywhere(self.device.token(), view)

    def test_through_a_jump_host(self):
        connection = self.connect_through_jump(self.jump_host())
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assert_real_layer(connection)

    def test_a_dropped_connection_during_the_real_install_does_not_stop_it(self):
        connection = self.connect_directly()
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] == 'install', seconds=120)
        self.installing_on_the_robot(seconds=120)
        self.device.drop_sessions()
        view = self.wait(connection, seconds=400)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        log = self.device.read(self.package_dir() + '/install.log').decode()
        self.assertNotIn('replacing an unfinished install', log)
        self.assert_real_layer(connection)

    def test_a_layer_installed_by_hand_with_the_real_script_is_only_linked(self):
        os.makedirs(self.device.local('neuxmind-pkg'))
        for entry in self.package.files:
            shutil.copy2(self.package.path(entry['role']), self.device.local('neuxmind-pkg/' + entry['name']))
        before = self.device.by_hand()
        token = self.device.token()
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual([o['id'] for o in view['choice']['options']], ['use', 'replace'], json.dumps(view, ensure_ascii=False))
        self.assertTrue(view['choice']['found']['running'])
        self.assertEqual(view['choice']['found']['running_port'], 8790)
        view = self.install(connection, existing='use')
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertEqual(self.assert_real_layer(connection)['token'], token)
        self.assertEqual({name: self.device.read('neuxmind-pkg/' + name) for name in before}, before)
        self.assertEqual(self.device.names('neuxmind-pkg/console'), [])

    def test_a_runtime_that_does_not_start_is_reported_and_cleaned_up_with_the_real_script(self):
        # The check found the port free; another program takes it while the package is sent and installed, so
        # the real install succeeds and the real start refuses (an unknown body no longer gets past install).
        connection = self.connect_directly()
        self.start(connection)
        self.wait(connection, until=lambda v: v['stage'] in ('upload', 'install'), seconds=120)
        listener = listen(8790)
        view = self.wait(connection, seconds=400)
        listener.close()
        self.assertEqual((view['state'], view['failure']['stage']), ('failed', 'start'), json.dumps(view, ensure_ascii=False))
        self.assertIn('127.0.0.1:8790 is already in use', '\n'.join(view['failure']['log']))
        self.assertEqual([item['path'] for item in view['left']], [runtime_install.PACKAGE_DIR % self.package.id, '~/neuxmind'])
        self.assertEqual(self.listening(8790), [])
        view = self.clean(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertEqual(self.device.names('neuxmind'), [])
        self.assertEqual(self.device.names('neuxmind-pkg'), [])

    def test_the_real_script_starts_the_body_the_install_recorded_not_the_card_s(self):
        connection = self.connect_directly()            # a quadruped card: the console's body is sim_quadruped
        self.assertEqual(self.install(connection)['state'], 'succeeded')
        code, out, err = self.device._run(['bash', self.device.local(self.package_dir() + '/device_install.sh'), 'stop'],
                                          seconds=120)
        self.assertEqual(code, 0, (out, err))
        self.device.write('neuxmind/adapter', b'sim_arm\n')   # what issue #21's install records for an arm
        view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', json.dumps(view, ensure_ascii=False))
        self.assertEqual([s['id'] for s in view['stages']], ['check', 'upload', 'start', 'link'])
        robot = self.assert_online_and_linked(connection)
        manifest = connector.call(robot['endpoint'], '/v1/manifest', robot['token'])
        self.assertEqual((manifest['family'], manifest['simulated']), ('manipulator', True))
        self.assertIn('adapter sim_arm', self.device.read(self.package_dir() + '/start.log').decode())

    def test_another_program_on_the_port_is_refused_before_anything_is_sent(self):
        listener = listen(8790)
        self.addCleanup(listener.close)
        connection = self.connect_directly()
        view = self.install(connection)
        self.assertEqual((view['stage'], view['failure']['code']), ('check', 'install_port_busy'))
        self.assertEqual(self.device.names('neuxmind-pkg'), [])


if __name__ == '__main__':
    unittest.main()
