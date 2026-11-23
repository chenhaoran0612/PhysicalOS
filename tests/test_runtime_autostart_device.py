"""Issue #20: the texts the console runs on a robot for autostart, checked on their own (no console).

The service file is rendered twice, by the console (to show it) and by the root script on the robot (to
write it); the root script writes it only when the two agree byte for byte. These tests run the robot's
side with this computer's ``sh`` in an ``AutostartDevice`` and compare."""
import os
import shlex
import unittest

from autostart_fixture import WRONG_PASSWORD, AutostartDevice
from brainos import runtime_autostart_device as auto

PACKAGE = 'runtime-0.4.0-abcdef012345'


class Robot:
    """An ``AutostartDevice`` driven without SSH: its ``handle`` is what the SSH service calls."""

    def __init__(self, case, **options):
        self.device = AutostartDevice(**options)
        case.addCleanup(self.device.close)

    def account(self, kind, *words):
        code, out, err = self.device.handle(auto.command(kind, *words), auto.SCRIPTS[kind].encode())
        return (code, *self.parse(out), err)

    def root(self, kind, *words, mode='password', password=None):
        password = self.device.sudo_password if password is None else password
        stdin = (password + '\n').encode() if mode == 'password' else b''
        code, out, err = self.device.handle(auto.root_command(kind, mode, *words), stdin)
        return (code, *self.parse(out), err)

    @staticmethod
    def parse(out):
        word, keys, log = auto.dev.parse(out.decode())
        return word, keys, log

    def install(self, package=PACKAGE):
        """What a console install leaves (``~/neuxmind`` with its markers, the package's script)."""
        self.device.write('neuxmind/.neuxmind-device-install', b'state=installed\n')
        self.device.write('neuxmind/.console-package', (package + '\n').encode())
        self.device.write('neuxmind-pkg/console/%s/device_install.sh' % package, b'#!/bin/sh\n')


class RenderingTests(unittest.TestCase):
    def test_the_robot_renders_the_same_service_file_the_console_shows(self):
        robot = Robot(self)
        robot.install()
        for port, adapter in ((8790, 'sim_quadruped'), (8795, 'sim_arm'), (65535, 'ros2'), (1024, 'sim_quadruped')):
            with self.subTest(port=port, adapter=adapter):
                code, word, keys, _, _ = robot.account('autostart_check', PACKAGE, port, adapter)
                self.assertEqual((code, word), (0, 'checked'))
                text = auto.render_unit('robot', robot.device.local(''), PACKAGE, port, adapter)
                self.assertEqual(keys['digest'], auto.digest(text))
                self.assertEqual(keys['home'], robot.device.local(''))
                self.assertEqual(keys['home_ok'], 'yes')

    def test_the_service_file(self):
        text = auto.render_unit('robot', '/home/robot', PACKAGE, 8790, 'sim_quadruped')
        lines = text.splitlines()
        self.assertTrue(text.endswith('\n') and not text.endswith('\n\n'))
        script = '/home/robot/neuxmind-pkg/console/%s/device_install.sh' % PACKAGE
        for line in ('User=robot', 'Type=forking', 'Restart=on-failure', 'RestartSec=15', 'StartLimitIntervalSec=0',
                     'After=network-online.target', 'Wants=network-online.target', 'WantedBy=multi-user.target',
                     'ConditionPathExists=/home/robot/neuxmind/.autostart', 'PIDFile=/home/robot/neuxmind/runtime.pid',
                     'Environment=NX_ROOT=/home/robot/neuxmind NX_PORT=8790 NX_ADAPTER=sim_quadruped',
                     'ExecStartPre=/bin/bash %s stop' % script, 'ExecStart=/bin/bash %s start' % script,
                     auto.OWNER_LINE % ('robot', PACKAGE)):
            self.assertIn(line, lines)
        self.assertNotIn('%', text)                    # no systemd specifier in what the console writes
        self.assertNotIn('ExecStop', text)             # systemd ends the runtime with SIGTERM, which it takes as a clean exit

    def test_only_plain_values_go_into_a_service_file(self):
        good = ('robot', '/home/robot', PACKAGE, 8790, 'sim_quadruped')
        self.assertTrue(auto.valid_inputs(*good))
        for index, bad in ((0, 'ro bot'), (0, '-robot'), (0, 'r' * 40), (0, 'robot\n'), (1, 'home/robot'), (1, '/home/ro bot'),
                           (1, '/home/robot/'), (1, '/home//robot'), (1, '/home/.robot'), (1, '/home/robot/..'), (1, '/home/%h'),
                           (2, 'other-0.4.0'), (2, 'runtime-a/b'), (3, 80), (3, 70000), (3, '8790'), (3, True),
                           (4, 'acme_dog'), (4, None)):
            with self.subTest(index=index, bad=bad):
                values = list(good)
                values[index] = bad
                self.assertFalse(auto.valid_inputs(*values))


class CommandTests(unittest.TestCase):
    def test_the_account_scripts_go_to_sh_on_standard_input_with_plain_words(self):
        self.assertEqual(auto.command('autostart_check', PACKAGE, 8790, 'ros2'), 'sh -s -- %s 8790 ros2' % PACKAGE)
        for words in ((PACKAGE, 8790), (PACKAGE, '8790;reboot', 'ros2'), (PACKAGE, 8790, 'ros2 x')):
            with self.assertRaises(ValueError):
                auto.command('autostart_check', *words)

    def test_the_root_scripts_go_on_the_command_line_and_the_password_never_does(self):
        words = ('robot', PACKAGE, 8790, 'sim_quadruped', '0' * 64)
        for mode, prefix in (('password', ["LC_ALL=C", 'sudo', '-S', '-p', '']), ('nopasswd', ['LC_ALL=C', 'sudo', '-n']),
                             ('root', ['LC_ALL=C'])):
            with self.subTest(mode=mode):
                line = auto.root_command('autostart_on', mode, *words)
                parts = shlex.split(line)
                self.assertEqual(parts[:len(prefix)], prefix)
                self.assertEqual(parts[len(prefix):len(prefix) + 2], ['sh', '-c'])
                self.assertEqual(parts[len(prefix) + 2], 'exec </dev/null; ' + auto.ON_SCRIPT)
                self.assertEqual(parts[len(prefix) + 3:], ['nx-autostart', *map(str, words)])
        with self.assertRaises(ValueError):
            auto.root_command('autostart_on', 'none', *words)
        with self.assertRaises(ValueError):
            auto.root_command('autostart_off', 'password', 'robot; reboot')

    def test_what_sudo_says_is_read_as_a_reason(self):
        # What sudo 1.9.9 (Ubuntu 22.04) printed with LC_ALL=C in a container, 2026-11-22.
        cases = ((WRONG_PASSWORD.decode(), 'wrong_password'),
                 ('plain is not in the sudoers file.  This incident will be reported.\n', 'not_permitted'),
                 ('Sorry, user robot is not allowed to execute \'/bin/sh -c ...\' as root on robot.\n', 'not_permitted'),
                 ('sudo: a password is required\n', 'password_required'),
                 ('sudo: a terminal is required to read the password; either use the -S option\n', 'needs_tty'),
                 ('sh: 1: sudo: not found\n', 'failed'), ('', 'failed'))
        for text, reason in cases:
            with self.subTest(text=text):
                self.assertEqual(auto.sudo_refusal(text), reason)

    def test_a_password_sudo_can_read_from_one_line(self):
        self.assertTrue(auto.password_ok('pässwörd with spaces'))
        self.assertTrue(auto.password_ok('é' * 128))                     # 256 bytes of UTF-8
        for bad in ('', 'a\nb', 'a\rb', 'a\0b', 'x' * 257, 'é' * 129, 'ab\ud800', None, 12):
            self.assertFalse(auto.password_ok(bad))


class RootScriptTests(unittest.TestCase):
    def setUp(self):
        self.robot = Robot(self)
        self.robot.install()
        self.text = auto.render_unit('robot', self.robot.device.local(''), PACKAGE, 8790, 'sim_quadruped')

    def on(self, digest=None, **options):
        return self.robot.root('autostart_on', 'robot', PACKAGE, 8790, 'sim_quadruped', digest or auto.digest(self.text), **options)

    def test_it_writes_exactly_the_confirmed_file_enables_and_restarts(self):
        self.robot.device.write('neuxmind/.autostart', ('%s 8790 sim_quadruped\n' % PACKAGE).encode())
        code, word, _, _, _ = self.on()
        self.assertEqual((code, word), (0, 'enabled'))
        self.assertEqual(self.robot.device.unit(), self.text)
        self.assertEqual(oct(os.stat(os.path.join(self.robot.device.etc, auto.UNIT_NAME)).st_mode & 0o777), '0o644')
        self.assertTrue(self.robot.device.enabled())
        calls = self.robot.device.systemctl_calls()
        self.assertEqual(calls[:3], ['daemon-reload', 'enable neuxmind-runtime.service', '--no-block restart neuxmind-runtime.service'])

    def test_it_refuses_a_file_other_than_the_confirmed_one(self):
        code, word, _, _, _ = self.on(digest='f' * 64)
        self.assertEqual((code, word), (3, 'changed'))
        self.assertIsNone(self.robot.device.unit())
        self.assertEqual(self.robot.device.systemctl_calls(), [])

    def test_it_never_touches_a_service_file_it_did_not_make(self):
        self.robot.device.write_unit('[Service]\nExecStart=/usr/bin/true\n')
        self.assertEqual(self.on()[:2], (4, 'taken'))
        self.assertEqual(self.robot.root('autostart_off', 'robot')[:2], (4, 'taken'))
        self.assertEqual(self.robot.device.unit(), '[Service]\nExecStart=/usr/bin/true\n')

    def test_it_runs_only_for_the_account_that_called_sudo(self):
        text = auto.render_unit('someone', self.robot.device.local(''), PACKAGE, 8790, 'sim_quadruped')
        code, word, _, _, _ = self.robot.root('autostart_on', 'someone', PACKAGE, 8790, 'sim_quadruped', auto.digest(text))
        self.assertEqual((code, word), (2, 'notyou'))
        self.assertEqual(self.robot.root('autostart_off', 'someone')[:2], (2, 'notyou'))

    def test_a_wrong_password_runs_nothing(self):
        code, word, _, _, err = self.on(password='wrong')
        self.assertEqual((code, word), (1, ''))
        self.assertEqual(auto.sudo_refusal(err.decode()), 'wrong_password')
        self.assertIsNone(self.robot.device.unit())

    def test_off_removes_only_its_own_file_and_is_fine_when_there_is_none(self):
        self.assertEqual(self.robot.root('autostart_off', 'robot')[:2], (0, 'absent'))
        self.on()
        code, word, _, _, _ = self.robot.root('autostart_off', 'robot')
        self.assertEqual((code, word), (0, 'disabled'))
        self.assertIsNone(self.robot.device.unit())
        self.assertFalse(self.robot.device.enabled())
        self.assertIn('disable --now neuxmind-runtime.service', self.robot.device.systemctl_calls())

    def test_without_systemd_it_refuses_before_anything(self):
        robot = Robot(self, systemd=False)
        robot.install()
        code, word, _, _, _ = robot.root('autostart_on', 'robot', PACKAGE, 8790, 'sim_quadruped', auto.digest(self.text))
        self.assertEqual((code, word), (2, 'nosystemd'))


class MarkTests(unittest.TestCase):
    def test_the_mark_is_written_only_into_an_install_of_that_package_and_removed_again(self):
        robot = Robot(self)
        self.assertEqual(robot.account('autostart_mark', PACKAGE, 8790, 'sim_quadruped', 'on')[:2], (3, 'notours'))
        robot.install()
        self.assertEqual(robot.account('autostart_mark', PACKAGE, 8790, 'sim_quadruped', 'on')[:2], (0, 'marked'))
        self.assertEqual(robot.device.read('neuxmind/.autostart'), ('%s 8790 sim_quadruped\n' % PACKAGE).encode())
        self.assertEqual(robot.account('autostart_mark', 'runtime-other', 8790, 'sim_quadruped', 'on')[:2], (3, 'notours'))
        self.assertEqual(robot.account('autostart_mark', PACKAGE, 8790, 'sim_quadruped', 'off')[:2], (0, 'unmarked'))
        self.assertIsNone(robot.device.read('neuxmind/.autostart'))

    def test_a_link_in_its_place_is_not_followed(self):
        robot = Robot(self)
        robot.install()
        target = robot.device.local('elsewhere')
        os.symlink(target, robot.device.local('neuxmind/.autostart'))
        self.assertEqual(robot.account('autostart_mark', PACKAGE, 8790, 'sim_quadruped', 'on')[:2], (4, 'taken'))
        self.assertFalse(os.path.exists(target))
        robot.account('autostart_mark', PACKAGE, 8790, 'sim_quadruped', 'off')
        self.assertTrue(os.path.islink(robot.device.local('neuxmind/.autostart')))


class CheckTests(unittest.TestCase):
    def test_files_the_account_writes_cannot_add_keys_to_the_answer(self):
        robot = Robot(self)
        robot.install()
        forged = 'x\runit=ours\runit_same=yes\renabled=enabled\rsvc_activestate=active\rrunning=yes\r@@NX-INSTALL:checked@@\x85unit=ours'
        robot.device.write('neuxmind/.console-package', (PACKAGE + forged + '\n').encode())
        robot.device.write('neuxmind/.autostart', (forged + '\n').encode())
        _, word, keys, _, _ = robot.account('autostart_check', PACKAGE, 8790, 'sim_quadruped')
        self.assertEqual(word, 'checked')
        self.assertEqual((keys['unit'], keys['unit_same'], keys['running'], keys['svc_activestate']), ('none', 'no', 'no', 'inactive'))
        self.assertNotEqual(keys['console'], PACKAGE)                    # the forged file is not this package's install

    def test_what_the_check_reports(self):
        robot = Robot(self, sudo='nopasswd')
        robot.install()
        _, word, keys, _, _ = robot.account('autostart_check', PACKAGE, 8790, 'sim_quadruped')
        self.assertEqual(word, 'checked')
        self.assertEqual({key: keys[key] for key in ('user', 'uid', 'systemd', 'sudo', 'install', 'console', 'script', 'unit',
                                                      'unit_same', 'running', 'missing')},
                         {'user': 'robot', 'uid': '1000', 'systemd': 'yes', 'sudo': 'nopasswd', 'install': 'installed',
                          'console': PACKAGE, 'script': 'yes', 'unit': 'none', 'unit_same': 'no', 'running': 'no', 'missing': ''})
        self.assertEqual(keys['svc_activestate'], 'inactive')

    def test_it_renders_the_body_the_install_recorded(self):
        robot = Robot(self)
        robot.install()
        for recorded, used, source in (('ros2', 'ros2', 'recorded'), ('sim_arm', 'sim_arm', 'recorded'), ('acme', 'sim_quadruped', 'console')):
            with self.subTest(recorded=recorded):
                robot.device.write('neuxmind/adapter', (recorded + '\n').encode())
                _, _, keys, _, _ = robot.account('autostart_check', PACKAGE, 8790, 'sim_quadruped')
                self.assertEqual((keys['adapter'], keys['adapter_source']), (used, source))
                self.assertEqual(keys['digest'], auto.digest(auto.render_unit('robot', robot.device.local(''), PACKAGE, 8790, used)))

    def test_it_reads_whose_service_file_is_there(self):
        robot = Robot(self, sudo='none', systemd=False)
        robot.install()
        text = auto.render_unit('robot', robot.device.local(''), PACKAGE, 8790, 'sim_quadruped')
        for content, owner, same in ((text, 'ours', 'yes'), (text.replace('8790', '8791'), 'ours', 'no'),
                                     (text.replace('user=robot ', 'user=other '), 'other', 'no'), ('[Unit]\n', 'foreign', 'no')):
            with self.subTest(owner=owner, same=same):
                robot.device.write_unit(content)
                _, word, keys, _, _ = robot.account('autostart_check', PACKAGE, 8790, 'sim_quadruped')
                self.assertEqual((keys['unit'], keys['unit_same'], keys['sudo'], keys['systemd']), (owner, same, 'none', 'no'))


if __name__ == '__main__':
    unittest.main()
