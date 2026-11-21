"""Issue #18: where the upload channel may write, what it accepts, and how other features call it.

The device is a local stand-in (``upload_fixture.UploadDevice``). Nothing here reaches a real machine.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_device_transfer as base
from brainos import device_transfer
from brainos.service import Platform

HOME, DEST, ARRIVED, NOISE, CHUNK = base.HOME, base.DEST, base.ARRIVED, base.NOISE, base.CHUNK
ALLOWED = HOME + '/neuxmind-pkg'

# Refused from the text alone: no connection is opened for any of these.
REFUSED_PATHS = [
    '', 'payload.bin', 'neuxmind-pkg/payload.bin', '~', '~/', '~/neuxmind-pkg', '~/neuxmind-pkg/',
    '~/neuxmind-pkg/../.ssh/authorized_keys', '~/neuxmind-pkg/a/../../b', '~/neuxmind-pkg//x', '~/neuxmind-pkg/./x',
    '~other/neuxmind-pkg/x', '~/neuxmind-pkg/x\ny', '~/neuxmind-pkg/x\x00', '~/neuxmind-pkg/.hidden', '~/neuxmind-pkg/.ssh/config',
    '~/neuxmind-pkg/keys/id_rsa', '~/neuxmind-pkg/' + 'n' * 250, '~/elsewhere/payload.bin', '~/neuxmind-pkg-other/payload.bin',
    '~/.bashrc', 42, None,
    # one way to write a place: absolute paths are not taken, wherever they point
    '/etc/robot/payload.bin', '/home/robot/neuxmind-pkg/payload.bin', '/tmp/payload.bin',
    # nothing a shell could read as more than a name
    '~/neuxmind-pkg/a b', "~/neuxmind-pkg/q'uote", '~/neuxmind-pkg/$(reboot)', '~/neuxmind-pkg/a;b', '~/neuxmind-pkg/a`b`',
    '~/neuxmind-pkg/back\\', '~/neuxmind-pkg/a*b', '~/neuxmind-pkg/[x]', '~/neuxmind-pkg/ünï', '~/neuxmind-pkg/a\ud800.whl',
    '~/neuxmind-pkg/~x', '~/neuxmind-pkg/a!b', '~/neuxmind-pkg/a&b', '~/neuxmind-pkg/a|b', '~/neuxmind-pkg/a>b', '~/neuxmind-pkg/a#b',
    # names the channel keeps for its own temporary files
    '~/neuxmind-pkg/pkg.nxpart-1', '~/neuxmind-pkg/pkg.nxtmp-0123456789abcdef.7',
]
BAD_ROOTS = [(), ('/',), ('/etc',), ('/tmp/nx',), ('~',), ('~/',), ('~/.ssh',), ('~/.config/nx',), ('~/a/../b',), ('~/a//b',),
             'neuxmind-pkg', ('neuxmind-pkg',), ('~/ok', 7), ('~/secrets',), ('~/a b',)]


class PathLimits(base.TransferCase):
    def assert_nothing_arrived(self):
        self.assertEqual(self.device.writes, [])
        self.assertNotIn('finalize', [kind for kind, _ in self.device.scripts])
        self.assertEqual(self.device.names(ALLOWED), [])
        self.assertEqual(self.record_files(), [])                              # and nothing is kept to resume

    def test_a_path_that_is_malformed_hidden_outside_or_not_a_plain_name_is_refused_before_anything_is_contacted(self):
        local = self.source(NOISE)
        for remote in REFUSED_PATHS:
            with self.subTest(remote=remote):
                error = self.refused(local, remote)
                self.assertEqual(error.stage, 'request')
                self.assertFalse(error.resumable)
        self.assertEqual(self.connects, 0)
        self.assert_nothing_arrived()

    def test_plain_names_with_the_allowed_marks_are_accepted(self):
        name = 'neuxmind_runtime-0.4.0+local-py3-none-any.whl'
        self.send(self.source(NOISE), '~/neuxmind-pkg/' + name)
        self.assertEqual(self.device.read(ALLOWED + '/' + name), NOISE)

    def test_a_link_inside_the_allowed_directory_cannot_lead_out_of_it(self):
        self.device.link(ALLOWED + '/out', '/etc/robot')
        error = self.refused(self.source(NOISE), '~/neuxmind-pkg/out/payload.bin')
        self.assertEqual((error.stage, error.reason), ('request', 'outside'))
        self.assertEqual(self.device.names('/etc/robot'), [])
        self.assertEqual(self.device.writes, [])

    def test_an_allowed_directory_that_is_itself_a_link_is_refused(self):
        self.device.link(ALLOWED, '/srv/elsewhere')
        error = self.refused(self.source(NOISE))
        self.assertEqual((error.stage, error.reason), ('request', 'outside'))
        self.assertEqual(self.device.names('/srv/elsewhere'), [])

    def test_a_link_or_a_directory_in_the_place_of_the_file_is_not_overwritten(self):
        local = self.source(NOISE)
        self.device.link(ARRIVED, '/etc/robot/target')
        self.assertEqual(self.refused(local).reason, 'occupied')
        self.assertEqual(self.device.names('/etc/robot/target'), [])
        os.makedirs(self.device.local(ALLOWED + '/logs/payload.bin'))
        self.assertEqual(self.refused(local, '~/neuxmind-pkg/logs/payload.bin').reason, 'occupied')
        self.device.write('/etc/robot/settings', b'not ours to replace')
        self.device.link(ALLOWED + '/settings', '/etc/robot/settings')
        self.assertEqual(self.refused(local, '~/neuxmind-pkg/settings').reason, 'occupied')
        self.assertEqual(self.device.read('/etc/robot/settings'), b'not ours to replace')
        self.assertEqual(self.device.writes, [])

    def test_a_file_where_a_directory_is_needed_is_refused(self):
        self.device.write(ALLOWED + '/runtime', b'a file, not a directory')
        error = self.refused(self.source(NOISE))
        self.assertEqual((error.stage, error.reason), ('request', 'occupied'))
        self.assertEqual(self.device.read(ALLOWED + '/runtime'), b'a file, not a directory')

    def test_only_plain_directories_under_the_login_home_can_be_allowed(self):
        local = self.source(NOISE)
        for roots in BAD_ROOTS:
            with self.subTest(roots=roots):
                error = self.refused(local, roots=roots)
                self.assertEqual((error.stage, error.reason), ('request', 'roots'))
        self.assertEqual(self.connects, 0)

    def test_a_caller_can_allow_another_directory_and_then_only_that_one(self):
        local = self.source(NOISE)
        self.send(local, '~/drivers/camera/payload.bin', roots=('~/drivers',))
        self.assertEqual(self.device.read(HOME + '/drivers/camera/payload.bin'), NOISE)
        self.assertEqual(self.refused(local, DEST, roots=('~/drivers',)).reason, 'outside')

    def test_missing_directories_are_created_on_the_way_down_and_nowhere_else(self):
        self.send(self.source(NOISE), '~/neuxmind-pkg/a/b/payload.bin')
        self.assertEqual(self.device.read(ALLOWED + '/a/b/payload.bin'), NOISE)
        self.assertEqual(self.device.names(HOME), ['neuxmind-pkg'])
        self.assertEqual(self.device.mode(ALLOWED + '/a'), 0o755)

    def test_a_mode_that_opens_the_file_to_others_for_writing_is_refused(self):
        local = self.source(NOISE)
        for mode in (0o666, 0o777, 0o4755, 0o020, -1, '644', True):
            with self.subTest(mode=mode):
                self.assertEqual(self.refused(local, mode=mode).stage, 'request')
        self.assertEqual(self.connects, 0)


class OddHome(unittest.TestCase):
    def send_to(self, home):
        base.quiet_paramiko(self)
        device = base.UploadDevice(home=home)
        self.addCleanup(device.close)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        local = Path(work.name) / 'payload.bin'
        local.write_bytes(NOISE)
        return device, lambda: device_transfer.upload(lambda: base.device_access.SSHDevice(*device.login()), local, DEST,
                                                      Path(work.name) / 'records', device_key=base.ROBOT, retry_waits=())

    def test_a_home_directory_that_is_not_plain_names_is_refused(self):
        device, send = self.send_to('/home/ro bot')
        with self.assertRaises(device_transfer.TransferError) as caught:
            send()
        self.assertEqual((caught.exception.stage, caught.exception.reason), ('request', 'outside'))
        self.assertEqual((device.writes, device.scripts), ([], []))

    def test_a_home_directory_named_after_an_account_in_a_domain_works(self):
        device, send = self.send_to('/home/operator@plant.example')
        self.assertEqual(send()['path'], '/home/operator@plant.example/neuxmind-pkg/runtime/payload.bin')
        self.assertEqual(device.read('/home/operator@plant.example/neuxmind-pkg/runtime/payload.bin'), NOISE)


class ChangedUnderway(base.TransferCase):
    """The device's directory was checked over SFTP; the scripts look again before they touch anything."""

    def at_the_final_check(self, change):
        def hook(kind, arguments):
            if kind == 'finalize' and not self.changed:
                self.changed = True
                change(arguments)

        self.changed = False
        self.device.before_script = hook

    def test_a_directory_that_became_a_link_is_not_written_through(self):
        def swap(arguments):
            real = self.device.local(base.DIRECTORY)
            os.rename(real, real + '-moved')
            os.symlink(real + '-moved', real)

        self.at_the_final_check(swap)
        error = self.refused(self.source(NOISE))
        self.assertEqual((error.stage, error.reason), ('verify', 'outside'))
        self.assertNotIn('payload.bin', self.device.names(base.DIRECTORY + '-moved'))
        self.assertEqual(self.device.connections, 1)
        self.assertIsNotNone(self.status())                                    # what is left there stays on record

    def test_a_directory_that_took_the_place_of_the_file_gets_nothing_put_inside(self):
        self.at_the_final_check(lambda arguments: os.makedirs(self.device.local(ARRIVED)))
        error = self.refused(self.source(NOISE))
        self.assertEqual((error.stage, error.reason, error.resumable), ('verify', 'unplaced', True))
        self.assertEqual(self.device.names(ARRIVED), [])

    def test_a_link_in_the_place_of_the_temporary_file_is_not_moved_into_place(self):
        def swap(arguments):
            part = self.device.local(arguments[0])
            os.rename(part, part + '.copy')
            os.symlink(part + '.copy', part)

        self.at_the_final_check(swap)
        error = self.refused(self.source(NOISE))
        self.assertEqual(error.reason, 'occupied')
        self.assertIsNone(self.device.read(ARRIVED))
        self.assertFalse(os.path.lexists(self.device.local(ARRIVED)))

    def test_text_a_device_sends_back_is_cleaned_before_it_is_shown(self):
        self.device.break_unpacking()
        tool = os.path.join(self.device.bin, 'gzip')
        with open(tool) as handle:
            script = handle.read()
        with open(tool, 'w') as handle:      # an escape sequence, a right-to-left override, a line separator, a zero-width mark, the saved login
            handle.write(script.replace('echo "gzip: write error: No space left on device"',
                                        'printf "gzip: no room\\033[31m here\\342\\200\\256\\342\\200\\250\\357\\273\\277 %s\\n" "'
                                        + self.device.password + '"'))
        error = self.refused(self.source(base.TEXT))
        self.assertEqual(error.reason, 'no_space')
        self.assertIn('gzip: no room', str(error))
        self.assertIn('[已隐藏连接凭据]', str(error))
        self.assertNotRegex(str(error), '[\x00-\x1f\x7f-\x9f‮ ﻿]')
        self.assertNotIn(self.device.password, str(error))
        self.assertNotIn(self.device.password, self.record_files()[0].read_text())


class LocalFile(base.TransferCase):
    def test_what_is_not_a_readable_regular_file_is_refused(self):
        folder = self.records.parent / 'folder'
        folder.mkdir()
        link = self.records.parent / 'link.bin'
        os.symlink(self.source(NOISE), link)
        for local in (self.records.parent / 'missing.bin', folder, link, 42):
            with self.subTest(local=local):
                error = self.refused(local)
                self.assertEqual((error.stage, error.reason), ('request', 'source'))
        self.assertEqual(self.connects, 0)

    def test_a_file_over_the_size_limit_is_refused(self):
        limit = device_transfer.MAX_FILE_BYTES
        try:
            device_transfer.MAX_FILE_BYTES = 1000
            error = self.refused(self.source(NOISE))
        finally:
            device_transfer.MAX_FILE_BYTES = limit
        self.assertEqual((error.stage, error.reason), ('request', 'source'))
        self.assertEqual(self.connects, 0)


class ThisComputer(base.TransferCase):
    """Trouble on this side is reported as such, not as something the device refused."""

    def test_a_local_file_that_can_no_longer_be_read_stops_the_transfer_where_it_is(self):
        local = self.source(NOISE)

        def remove(event):
            if event['stage'] == 'sending' and event['sent'] == 3 * CHUNK:
                local.unlink()

        error = self.refused(local, on_progress=remove)
        self.assertEqual((error.stage, error.reason, error.sent, error.resumable), ('send', 'source', 3 * CHUNK, True))
        self.assertEqual(self.device.connections, 1)
        self.assertEqual(self.send(self.source(NOISE))['resumed_from'], 3 * CHUNK)     # the same file again: continued
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_a_record_that_cannot_be_written_stops_the_transfer(self):
        local = self.source(NOISE)

        def block(event):
            if event['stage'] == 'sending' and event['sent'] == 3 * CHUNK:
                shutil.rmtree(self.records)
                self.records.write_text('a file where the folder was')

        error = self.refused(local, on_progress=block)
        self.assertEqual((error.stage, error.reason), ('send', 'records'))
        self.assertIn(str(self.records), str(error))
        self.assertEqual(self.device.connections, 1)


class Housekeeping(base.TransferCase):
    def test_one_destination_takes_one_transfer_at_a_time(self):
        local, nested = self.source(NOISE), []

        def again(event):
            if event['stage'] == 'sending' and not nested:
                nested.append(self.refused(local).reason)

        self.send(local, on_progress=again)
        self.assertEqual(nested, ['busy'])
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_a_transfer_whose_process_ended_midway_is_shown_as_interrupted(self):
        self.interrupt(self.source(NOISE))
        record = self.record_files()[0]
        record.write_text(record.read_text().replace('"status": "interrupted"', '"status": "sending"'))
        self.assertEqual(self.status()['status'], 'interrupted')

    def test_a_record_that_was_tampered_with_is_not_trusted(self):
        local = self.source(NOISE)
        self.interrupt(local)
        record = self.record_files()[0]
        record.write_text(record.read_text().replace('"sent": %d' % (6 * CHUNK), '"sent": -5'))
        self.assertIsNone(self.status())
        self.assertEqual(self.send(local)['resumed_from'], 0)
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_the_record_of_a_transfer_holds_no_login_and_is_private(self):
        self.interrupt(self.source(NOISE))
        record = self.record_files()[0]
        self.assertNotIn(self.device.password, record.read_text())
        self.assertEqual(os.stat(record).st_mode & 0o077, 0)
        self.assertEqual(os.stat(record.parent).st_mode & 0o077, 0)

    def test_the_records_of_a_device_that_is_removed_can_be_dropped(self):
        self.interrupt(self.source(NOISE))
        self.assertEqual(device_transfer.records.forget_device(self.records, 'another-robot'), 0)
        self.assertEqual(device_transfer.records.forget_device(self.records, base.ROBOT), 1)
        self.assertEqual(self.record_files(), [])
        self.assertIsNone(self.status())

    def test_an_error_in_the_callers_progress_handler_is_not_mistaken_for_a_broken_link(self):
        def broken(event):
            if event['stage'] == 'sending':
                raise OSError('the caller made a mistake')

        with self.assertRaises(device_transfer.ProgressHandlerError):
            self.send(self.source(NOISE), on_progress=broken)
        self.assertEqual(self.device.connections, 1)

    def test_a_handler_that_fails_at_the_very_end_does_not_hide_the_result(self):
        def broken(event):
            if event['stage'] == 'done':
                raise RuntimeError('the caller made a mistake')

        with self.assertRaises(device_transfer.ProgressHandlerError) as caught:
            self.send(self.source(NOISE), on_progress=broken)
        self.assertTrue(caught.exception.result['verified'])
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_asking_about_a_path_that_cannot_be_a_destination_is_an_error(self):
        for remote in (None, 42, '/etc/passwd'):
            with self.subTest(remote=remote), self.assertRaises(device_transfer.TransferError):
                self.status(remote)


class LoginShells(unittest.TestCase):
    """The command line is read first by the account's login shell, whatever that is."""

    def test_every_shell_here_hands_the_same_words_and_the_script_to_sh(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        fake = Path(work.name) / 'sh'
        fake.write_text('#!/bin/sh\nfor word in "$@"; do printf "%s\\n" "$word"; done\ncat\n')      # words, then standard input
        fake.chmod(0o755)
        sha = hashlib.sha256(b'x').hexdigest()
        words = ['/home/operator@plant.example/neuxmind-pkg/.pkg_1.0+b-1.whl.nxpart-' + sha[:16],
                 '/home/operator@plant.example/neuxmind-pkg/-pkg_1.0+b-1.whl', sha, 'gzip', '644', '-']
        command = device_transfer.target.command(*words)
        script = device_transfer.SCRIPTS['finalize']
        tried = []
        for shell, options in (('sh', []), ('bash', ['--norc']), ('zsh', ['-f']), ('csh', ['-f']), ('tcsh', ['-f']),
                               ('fish', ['--no-config']), ('dash', []), ('ksh', [])):
            path = shutil.which(shell)
            if path is None:
                continue
            with self.subTest(shell=shell):
                done = subprocess.run([path, *options, '-c', command], input=script.encode(), capture_output=True, timeout=20,
                                      env={'PATH': work.name + os.pathsep + '/usr/bin' + os.pathsep + '/bin', 'HOME': work.name})
                self.assertEqual(done.stdout.decode(), '\n'.join(['-s', '--', *words]) + '\n' + script)
                tried.append(shell)
        self.assertGreaterEqual(len(tried), 2)


class FixtureLimits(base.TransferCase):
    """The stand-in runs scripts on this computer: only the product's, only on its own files."""

    def exec(self, command, script):
        device = self.connect()
        self.addCleanup(device.close)
        channel = device.client.get_transport().open_session(timeout=5)
        channel.settimeout(10)
        channel.exec_command(command)
        channel.sendall(script.encode())
        channel.shutdown_write()
        return channel.recv_exit_status()

    def test_anything_but_a_product_script_on_its_own_files_is_refused(self):
        scripts, sha = device_transfer.SCRIPTS, hashlib.sha256(b'x').hexdigest()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / 'victim').write_bytes(b'not the fixture\'s')
        os.symlink(outside.name, self.device.local(HOME + '/way-out'))
        inside = HOME + '/neuxmind-pkg/x'
        refused = [
            ('sh -s -- ' + inside, 'echo unknown script\n'),
            ('sh -s -- ../../../victim', scripts['clean']),
            ('sh -s -- ' + HOME + '/../../../victim', scripts['clean']),
            ('sh -s -- ' + HOME + '/way-out/victim', scripts['clean']),
            ('sh -s -- victim', scripts['clean']),
            ('sh -s -- ' + inside + ' extra', scripts['clean']),
            ('sh -s -- ' + inside + ' ' + inside + ' ' + sha + ' identity 644 ../../x', scripts['finalize']),
            ('sh -s -- ' + inside + ' ' + inside + ' ' + sha + ' identity rwx ' + inside, scripts['finalize']),
            ('sh -s -- ' + inside + ' 12;id ' + inside, scripts['probe']),
            ('sh -c ' + "'" + scripts['clean'] + "' sh " + inside, ''),
            ('cat /etc/passwd', ''),
        ]
        for command, script in refused:
            with self.subTest(command=command[:60]):
                self.assertEqual(self.exec(command, script), 126)
        self.assertEqual(len(self.device.refused), len(refused))
        self.assertEqual(self.device.scripts, [])
        self.assertEqual((Path(outside.name) / 'victim').read_bytes(), b'not the fixture\'s')

    def test_a_login_other_than_the_one_made_up_for_this_device_is_turned_away(self):
        connection, secret = self.device.login()
        with self.assertRaises(base.device_access.ConnectError):
            base.device_access.SSHDevice(connection, {'password': 'fixture-password'})
        self.assertNotEqual(self.device.password, base.UploadDevice.__init__.__defaults__)
        self.assertGreaterEqual(len(secret['password']), 32)


class PlatformEntry(unittest.TestCase):
    """What the features built on this channel call: a saved connection by its id."""

    def setUp(self):
        base.quiet_paramiko(self)
        self.device = base.UploadDevice()
        self.addCleanup(self.device.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        connection, secret = self.device.login()
        self.platform.store.put('device_connections', {**connection, 'id': 'connection-fixture'})
        self.platform.store.put('agent_secrets', {**secret, 'id': 'connection-fixture'})
        self.local = os.path.join(self.work.name, 'payload.bin')
        with open(self.local, 'wb') as handle:
            handle.write(NOISE)

    def test_a_file_goes_to_the_robot_of_a_saved_connection(self):
        result = device_transfer.send_to_robot(self.platform, 'connection-fixture', self.local, DEST)
        self.assertTrue(result['verified'])
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertIsNone(device_transfer.robot_transfer_status(self.platform, 'connection-fixture', DEST))

    def test_an_unfinished_transfer_of_a_saved_connection_can_be_found_given_up_or_forgotten(self):
        self.device.drops, self.device.offline_after_drop = [50_000], True
        with self.assertRaises(device_transfer.TransferError):
            device_transfer.send_to_robot(self.platform, 'connection-fixture', self.local, DEST, retry_waits=(0,), chunk_bytes=CHUNK)
        self.device.offline = False
        listed = device_transfer.robot_unfinished_transfers(self.platform, 'connection-fixture')
        self.assertEqual([entry['remote_path'] for entry in listed], [DEST])
        self.assertTrue((Path(self.platform.store.root) / 'transfers').is_dir())

        device_transfer.discard_robot_transfer(self.platform, 'connection-fixture', DEST)
        self.assertEqual(self.device.names(base.DIRECTORY), [])
        self.assertEqual(device_transfer.robot_unfinished_transfers(self.platform, 'connection-fixture'), [])
        self.assertEqual(device_transfer.forget_robot_transfers(self.platform, 'connection-fixture'), 0)

    def test_an_unknown_connection_is_an_error_before_anything_is_contacted(self):
        with self.assertRaises(ValueError):
            device_transfer.send_to_robot(self.platform, 'connection-missing', self.local, DEST)
        self.assertEqual(self.device.connections, 0)


if __name__ == '__main__':
    unittest.main()
