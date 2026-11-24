"""Issue #43: the two scripts the console runs on a robot for programs and scene data, run for real.

``brainos/robot_bundle_device.py`` holds the script texts. Here they are fed to ``/bin/sh -s --``
exactly as the console sends them, with ``HOME`` moved into a temporary directory, and what they
leave on disk is read back. Hostile archives are built with ``tarfile``.

This file needs only Python and a shell, so it also runs inside a Linux container (GNU tar, the
target) without the rest of the product's dependencies:

    docker run --rm --network none -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src:/src/tests \
        -e PYTHONDONTWRITEBYTECODE=1 python:3.13-slim python -m unittest test_robot_bundle_scripts
"""
import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

from brainos import robot_bundle_device as device

TOKEN = '01234567' + '89abcdef'
GNU_TAR = 'GNU tar' in subprocess.run(['tar', '--version'], capture_output=True, text=True).stdout
SHELL = os.environ.get('NX_BUNDLE_TEST_SHELL', '/bin/sh')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    """Every path below ``root`` with what it is: file content, link target or 'dir'."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                found[rel] = 'link:' + os.readlink(path)
            elif os.path.isdir(path):
                found[rel] = 'dir'
            else:
                with open(path, 'rb') as handle:
                    found[rel] = handle.read()
    return found


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix='nx-bundle-scripts-'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, 'home', 'robot')
        os.makedirs(self.home)
        self.store = os.path.join(self.home, 'neuxmind-bundles')

    def run_script(self, kind, *arguments):
        """(word, checksum, note, key=value fields) of one script run the way the console runs it."""
        line = device.command(*arguments).split(' ')
        self.assertEqual(line[:3], ['sh', '-s', '--'])
        done = subprocess.run([SHELL, *line[1:]], input=device.SCRIPTS[kind].encode(), capture_output=True,
                              env={'HOME': self.home, 'PATH': os.environ['PATH']}, cwd=self.base, timeout=120)
        text = self.last_text = done.stdout.decode('utf-8', 'replace')
        word, checksum, note = device.verdict(text)
        return word, checksum, note, device.fields(text)

    def run_detached(self, kind, *arguments):
        """Run a script whose answer nobody reads any more: the SSH session was closed (a stop, a
        dropped link), so its standard output is a pipe without a reader."""
        line = device.command(*arguments).split(' ')
        process = subprocess.Popen([SHELL, *line[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env={'HOME': self.home, 'PATH': os.environ['PATH']}, cwd=self.base)
        process.stdout.close()
        process.stdin.write(device.SCRIPTS[kind].encode())
        process.stdin.close()
        return process.wait(timeout=120)

    def write(self, path, data=b'x', mode=None):
        full = os.path.join(self.home, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(full, mode)
        return full

    def program(self, name='walk_programs'):
        """A made-up program directory with the things the rules are about."""
        self.write(name + '/walk.py', b'print("walk")\n', 0o755)
        self.write(name + '/maps/office.yaml', 'places:\n  会议室: [1.0, 2.0]\n'.encode())
        self.write(name + '/runs/2026-11-01/log.txt', b'a run log')
        self.write(name + '/lib/__pycache__/walk.cpython-310.pyc', b'\x00cache')
        self.write(name + '/lib/util.pyc', b'\x00compiled')
        self.write(name + '/lib/util.py', b'def util():\n    return 1\n')
        self.write(name + '/walk.py.bak', b'old')
        self.write(name + '/maps/office.yaml.bak2', b'older')
        self.write(name + '/.env', b'TOKEN=not-for-the-cloud')
        self.write(name + '/keys/id_rsa', b'not a real key')
        os.symlink('maps/office.yaml', os.path.join(self.home, name, 'current-map'))
        return name

    def pack(self, directory, limit=device.MAX_ARCHIVE_BYTES, token=TOKEN):
        return self.run_script('pack', device.hex_argument(directory), token, limit)

    def outgoing(self, token=TOKEN):
        return os.path.join(self.store, '.outgoing', token + '.tar.gz')

    def members(self, path):
        with tarfile.open(path) as archive:
            return {member.name.removeprefix('./'): member for member in archive.getmembers() if member.name not in ('.', './')}


class Pack(ScriptCase):
    def test_a_directory_becomes_one_archive_without_what_is_to_be_left_out(self):
        self.program()
        before = tree(os.path.join(self.home, 'walk_programs'))
        word, checksum, _, fields = self.pack('~/walk_programs')
        self.assertEqual(word, 'packed', fields)
        with open(self.outgoing(), 'rb') as handle:
            data = handle.read()
        self.assertEqual((checksum, int(fields['size'])), (sha(data), len(data)))
        members = self.members(self.outgoing())
        self.assertEqual(sorted(members), ['current-map', 'keys', 'lib', 'lib/util.py', 'maps', 'maps/office.yaml', 'walk.py'])
        self.assertEqual(int(fields['files']), 4)                       # three files and one link
        self.assertTrue(members['current-map'].issym())
        self.assertEqual(members['current-map'].linkname, 'maps/office.yaml')
        self.assertTrue(members['walk.py'].mode & stat.S_IXUSR)
        self.assertEqual(fields['outside'], '0')
        self.assertEqual(tree(os.path.join(self.home, 'walk_programs')), before)        # the directory is only read
        self.assertEqual(stat.S_IMODE(os.stat(self.outgoing()).st_mode) & 0o077, 0)   # private to the account
        self.assertEqual(os.listdir(os.path.dirname(self.outgoing())), [TOKEN + '.tar.gz'])

    def test_a_link_is_stored_as_a_link_and_what_it_points_to_outside_does_not_travel(self):
        self.write('prog/a.txt', b'inside')
        self.write('private/notes.txt', b'OUTSIDE CONTENT')
        os.symlink('../private/notes.txt', os.path.join(self.home, 'prog', 'notes'))
        os.symlink(os.path.join(self.home, 'private'), os.path.join(self.home, 'prog', 'all'))
        word, _, _, fields = self.pack('~/prog')
        self.assertEqual(word, 'packed')
        members = self.members(self.outgoing())
        self.assertTrue(members['notes'].issym() and members['all'].issym())
        self.assertEqual(sorted(members), ['a.txt', 'all', 'notes'])
        with open(self.outgoing(), 'rb') as handle:
            import gzip
            self.assertNotIn(b'OUTSIDE CONTENT', gzip.decompress(handle.read()))
        self.assertEqual(fields['outside'], '2')                        # said, so that the person knows before installing

    def test_a_directory_with_any_characters_is_never_read_as_a_command(self):
        names = ['prog with spaces', '程序 和 场景', "it's; touch pwned-a", '$(touch pwned-b)', '`touch pwned-c`',
                 'a"b\'c', '-rf', 'x|y&z>out', '%s%n']
        for index, name in enumerate(names):
            with self.subTest(name):
                self.write(name + '/data.txt', name.encode())
                token = '%016x' % index
                word, _, _, fields = self.pack('~/' + name, token=token)
                self.assertEqual(word, 'packed', fields)
                self.assertEqual(sorted(self.members(self.outgoing(token))), ['data.txt'])
        leftovers = [path for path in tree(self.base) if 'pwned' in os.path.basename(path) and 'touch' not in path]
        self.assertEqual(leftovers, [])
        self.assertFalse(os.path.exists(os.path.join(self.base, 'out')))

    def test_an_absolute_path_and_a_linked_directory_are_resolved_on_the_device(self):
        self.write('prog/a.txt', b'inside')
        os.symlink('prog', os.path.join(self.home, 'alias'))
        for index, directory in enumerate((os.path.join(self.home, 'prog'), '~/alias')):
            word, _, _, _ = self.pack(directory, token='%016x' % index)
            self.assertEqual(word, 'packed')
            self.assertEqual(sorted(self.members(self.outgoing('%016x' % index))), ['a.txt'])

    def test_what_may_not_be_packed_is_refused_and_leaves_nothing(self):
        self.write('prog/a.txt', b'inside')
        os.makedirs(os.path.join(self.store, 'walk', 'abc'))
        cases = {'~/nothing-here': 'missing', '~/prog/a.txt': 'missing', os.path.dirname(self.home): 'toowide', '/': 'toowide',
                 '~/neuxmind-bundles/walk': 'inside', 'relative/path': 'refused'}
        for directory, expected in cases.items():
            with self.subTest(directory):
                self.assertEqual(self.pack(directory)[0], expected)
        self.assertEqual(self.run_script('pack', device.hex_argument('~/prog')[:-1], TOKEN, 1000)[0], 'refused')    # not whole bytes
        self.assertEqual(self.run_script('pack', 'zz', TOKEN, 1000)[0], 'refused')
        home_itself = subprocess.run([SHELL, '-s', '--', device.hex_argument(self.home), TOKEN, '1000'],
                                     input=device.PACK_SCRIPT.encode(), capture_output=True, env={'HOME': self.home, 'PATH': os.environ['PATH']})
        self.assertIn('@@NX-BUNDLE:toowide@@', home_itself.stdout.decode())
        self.assertFalse(os.path.exists(self.outgoing()))

    def test_a_directory_with_a_pipe_in_it_is_refused(self):
        self.write('prog/a.txt', b'inside')
        os.mkfifo(os.path.join(self.home, 'prog', 'control'))
        word, _, note, _ = self.pack('~/prog')
        self.assertEqual(word, 'special')
        self.assertIn('control', note)
        self.assertFalse(os.path.exists(self.outgoing()))

    def test_an_archive_over_the_limit_is_removed_again(self):
        self.write('prog/big.bin', os.urandom(200_000))
        self.assertEqual(self.pack('~/prog', limit=50_000)[0], 'toolarge')
        self.assertEqual(os.listdir(os.path.join(self.store, '.outgoing')), [])
        self.assertEqual(self.pack('~/prog', limit=400_000)[0], 'packed')

    def test_credentials_stay_behind_program_files_with_similar_names_travel_and_all_of_it_is_said(self):
        secret = ['id_ecdsa', 'id_rsa.pub', '.netrc', '.git-credentials', 'token.json', '.envrc', '.env.local', 'server.key', 'cert.pem',
                  '.config/app.ini', '.git/config', '.ssh/authorized_keys', 'deep/down/.aws/credentials', 'deep/credentials']
        program = ['walk.py', 'secrets_loader/__init__.py', 'credentials_helper.py', 'secrets.yaml', 'maps.bak.d/site.map',
                   'id_card_reader.py', 'keys/layout.json', 'env.py', 'runs.py', 'tokenizer/token.json.tmpl']
        routine = ['notes.bak', 'old/walk.py.bak2', 'runs/1/log.txt', 'lib/__pycache__/x.pyc', 'lib/y.pyc']
        for name in secret + program + routine:
            self.write('prog/' + name, name.encode())
        word, _, _, fields = self.pack('~/prog')
        self.assertEqual(word, 'packed', fields)
        members = self.members(self.outgoing())
        files = sorted(name for name, member in members.items() if member.isreg())
        self.assertEqual(files, sorted(program))                       # nothing of a program is dropped for looking like a secret
        for name in secret:
            self.assertNotIn(name, members)
        text = self.last_text
        guarded, skipped = device.listed(text, 'guard'), device.listed(text, 'skip')
        self.assertEqual(sorted(guarded), sorted(['./id_ecdsa', './id_rsa.pub', './.netrc', './.git-credentials', './token.json', './.envrc',
                                                 './.env.local', './server.key', './cert.pem', './.config', './.git', './.ssh',
                                                 './deep/down/.aws', './deep/credentials']))
        self.assertEqual(int(fields['guarded']), len(guarded))
        self.assertEqual(sorted(skipped), sorted(['./notes.bak', './old/walk.py.bak2', './runs', './lib/__pycache__', './lib/y.pyc']))
        self.assertEqual(int(fields['skipped']), len(skipped))

    def test_only_the_first_names_of_what_was_left_out_are_listed_but_all_are_counted(self):
        for index in range(40):
            self.write('prog/key%d.pem' % index, b'k')
        word, _, _, fields = self.pack('~/prog')
        self.assertEqual((word, fields['guarded']), ('packed', '40'))
        self.assertEqual(len(device.listed(self.last_text, 'guard')), device.LISTED)

    def test_a_directory_that_resolves_to_a_place_of_credentials_is_refused(self):
        self.write('.ssh/authorized_keys', b'ssh-ed25519 AAAA')
        self.write('work/.git/config', b'[remote]')
        os.symlink('.ssh', os.path.join(self.home, 'tools'))
        os.symlink('work/.git', os.path.join(self.home, 'repo-data'))
        for directory in ('~/tools', '~/repo-data'):
            self.assertEqual(self.pack(directory)[0], 'sensitive', directory)
        self.assertFalse(os.path.exists(self.outgoing()))

    def test_a_store_that_is_a_link_elsewhere_is_not_written_through(self):
        self.write('prog/a.txt', b'inside')
        os.symlink('prog', self.store)
        before = tree(os.path.join(self.home, 'prog'))
        self.assertEqual(self.pack('~/prog')[0], 'moved')
        self.assertEqual(tree(os.path.join(self.home, 'prog')), before)        # no .outgoing was made in the program directory
        os.remove(self.store)
        os.makedirs(self.store)
        os.symlink(os.path.join(self.home, 'prog'), os.path.join(self.store, '.outgoing'))
        self.assertEqual(self.pack('~/prog')[0], 'moved')
        self.assertEqual(tree(os.path.join(self.home, 'prog')), before)

    def test_a_session_that_is_gone_leaves_no_archive_behind(self):
        self.write('prog/a.txt', b'inside')
        self.run_detached('pack', device.hex_argument('~/prog'), TOKEN, device.MAX_ARCHIVE_BYTES)
        self.assertEqual(os.listdir(os.path.join(self.store, '.outgoing')), [])

    @unittest.skipUnless(GNU_TAR, 'hard links are stored as files only with GNU tar (the target)')
    def test_hard_links_travel_as_plain_files(self):
        first = self.write('prog/a.txt', b'same content')
        os.link(first, os.path.join(self.home, 'prog', 'b.txt'))
        self.assertEqual(self.pack('~/prog')[0], 'packed')
        members = self.members(self.outgoing())
        self.assertTrue(members['a.txt'].isreg() and members['b.txt'].isreg())


class Extract(ScriptCase):
    NAME = 'walk_programs'

    def place(self, data):
        """Put an archive where the upload channel would: ``~/neuxmind-bundles/<name>/<16 digits>.tar.gz``."""
        digits = sha(data)[:device.DIGITS]
        folder = os.path.join(self.store, self.NAME)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, digits + '.tar.gz'), 'wb') as handle:
            handle.write(data)
        return digits

    def extract(self, digits, limit=device.MAX_UNPACKED_BYTES):
        return self.run_script('extract', self.NAME, digits, limit)

    def archive(self, *members):
        """A tar.gz of (TarInfo or name, content) pairs, in that order."""
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w:gz', format=tarfile.GNU_FORMAT) as archive:
            for member, content in members:
                info = member if isinstance(member, tarfile.TarInfo) else tarfile.TarInfo(member)
                if info.isreg():
                    info.size = len(content)
                    info.mode = info.mode or 0o644
                archive.addfile(info, io.BytesIO(content) if info.isreg() else None)
        return out.getvalue()

    @staticmethod
    def special(name, kind, linkname='', mode=0o644):
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.mode = kind, linkname, mode
        return info

    def refused(self, data, word, reason=None):
        digits = self.place(data)
        before = tree(self.base)
        got, _, note, _ = self.extract(digits)
        self.assertEqual(got, word, note)
        if reason:
            self.assertEqual(note.splitlines()[0], reason, note)
        self.assertEqual(tree(self.base), before)       # nothing was written anywhere, nothing is left behind
        return note

    def test_what_was_packed_comes_back_next_to_the_original_which_is_not_touched(self):
        Pack.program(self)
        original = os.path.join(self.home, 'walk_programs')
        before = tree(original)
        self.assertEqual(Pack.pack(self, '~/walk_programs')[0], 'packed')
        with open(Pack.outgoing(self), 'rb') as handle:
            digits = self.place(handle.read())

        word, _, _, fields = self.extract(digits)
        self.assertEqual((word, fields['already'], fields['files']), ('placed', 'no', '3'))
        dest = os.path.join(self.store, self.NAME, digits)
        unpacked = tree(dest)
        self.assertEqual(unpacked['walk.py'], b'print("walk")\n')
        self.assertEqual(unpacked['current-map'], 'link:maps/office.yaml')
        self.assertEqual(sorted(unpacked), ['current-map', 'keys', 'lib', 'lib/util.py', 'maps', 'maps/office.yaml', 'walk.py'])
        self.assertTrue(os.stat(os.path.join(dest, 'walk.py')).st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(os.path.join(self.store, self.NAME, 'current')), digits)
        self.assertEqual(tree(original), before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.store, self.NAME))), sorted(['current', digits, digits + '.tar.gz']))

    def test_running_it_again_finds_the_directory_and_only_points_current_at_it(self):
        first = self.place(self.archive(('a.txt', b'one')))
        second = self.place(self.archive(('a.txt', b'two')))
        self.assertEqual(self.extract(first)[0], 'placed')
        self.assertEqual(self.extract(second)[0], 'placed')
        current = os.path.join(self.store, self.NAME, 'current')
        self.assertEqual(os.readlink(current), second)
        word, _, _, fields = self.extract(first)
        self.assertEqual((word, fields['already']), ('placed', 'yes'))
        self.assertEqual(os.readlink(current), first)
        with open(os.path.join(current, 'a.txt'), 'rb') as handle:
            self.assertEqual(handle.read(), b'one')

    def test_a_directory_that_was_changed_since_is_set_aside_and_the_cloud_copy_is_unpacked_again(self):
        data = self.archive(('main.py', b'print("from the cloud")\n'), ('maps/a.yaml', b'a: 1\n'))
        digits = self.place(data)
        self.assertEqual(self.extract(digits)[0], 'placed')
        dest = os.path.join(self.store, self.NAME, digits)
        with open(os.path.join(dest, 'main.py'), 'w') as handle:           # a program running from ``current`` changed it
            handle.write('print("changed on the robot")\n')
        with open(os.path.join(dest, 'extra.log'), 'w') as handle:
            handle.write('written by a run')

        word, _, _, fields = self.extract(digits)
        self.assertEqual((word, fields['already']), ('placed', 'no'))
        with open(os.path.join(self.store, self.NAME, 'current', 'main.py'), 'rb') as handle:
            self.assertEqual(handle.read(), b'print("from the cloud")\n')
        self.assertEqual(sorted(tree(dest)), ['main.py', 'maps', 'maps/a.yaml'])
        aside = os.path.join(self.store, self.NAME, fields['aside'])        # what was there is kept, under another name
        self.assertTrue(fields['aside'].startswith(digits + '.changed-'))
        with open(os.path.join(aside, 'extra.log')) as handle:
            self.assertEqual(handle.read(), 'written by a run')
        again = self.extract(digits)
        self.assertEqual((again[0], again[3]['already'], again[3].get('aside', '')), ('placed', 'yes', ''))

    def test_a_session_that_is_gone_does_not_switch_current(self):
        first = self.place(self.archive(('a.txt', b'one')))
        self.assertEqual(self.extract(first)[0], 'placed')
        second = self.place(self.archive(('a.txt', b'two')))
        before = tree(self.base)
        self.run_detached('extract', self.NAME, second, device.MAX_UNPACKED_BYTES)
        self.assertEqual(tree(self.base), before)                           # current still points at the first, nothing new is left
        self.assertEqual(os.readlink(os.path.join(self.store, self.NAME, 'current')), first)

    def test_current_is_switched_in_one_step_and_never_becomes_a_link_inside_the_old_directory(self):
        first = self.place(self.archive(('a.txt', b'one')))
        second = self.place(self.archive(('a.txt', b'two')))
        self.extract(first)
        self.extract(second)
        folder = os.path.join(self.store, self.NAME)
        self.assertEqual(os.readlink(os.path.join(folder, 'current')), second)
        self.assertEqual(sorted(os.listdir(os.path.join(folder, first))), ['a.txt'])
        self.assertEqual([n for n in os.listdir(folder) if n.startswith('.')], [])

    def test_paths_that_leave_the_directory_are_refused_before_anything_is_unpacked(self):
        for name in ('/tmp/nx-bundle-absolute.txt', '../escape.txt', 'a/../../escape.txt', 'a/..'):
            with self.subTest(name):
                self.refused(self.archive(('ok.txt', b'fine'), (name, b'escaped')), 'unsafe', 'path')
        self.assertFalse(os.path.exists('/tmp/nx-bundle-absolute.txt'))

    def test_devices_pipes_and_hard_links_are_refused_before_anything_is_unpacked(self):
        kinds = {'a pipe': self.special('pipe', tarfile.FIFOTYPE), 'a character device': self.special('tty', tarfile.CHRTYPE),
                 'a block device': self.special('disk', tarfile.BLKTYPE),
                 'a hard link': self.special('hard', tarfile.LNKTYPE, 'ok.txt'),
                 'a hard link out': self.special('passwd', tarfile.LNKTYPE, '/etc/passwd')}
        for name, member in kinds.items():
            with self.subTest(name):
                self.refused(self.archive(('ok.txt', b'fine'), (member, b'')), 'unsafe', 'kind')

    def test_links_that_lead_out_of_the_directory_are_refused(self):
        os.makedirs(os.path.join(self.home, 'victim'))
        targets = {'absolute': '/etc', 'to the parent': '..', 'far up': '../../../..', 'to a sibling': '../../../victim',
                   'up and down again': 'sub/../../../../victim', 'into the original': os.path.join(self.home, 'victim')}
        for name, target in targets.items():
            with self.subTest(name):
                note = self.refused(self.archive(('sub/ok.txt', b'fine'), (self.special('link', tarfile.SYMTYPE, target), b'')),
                                    'unsafe', 'link')
                self.assertIn('link', note)

    def test_a_link_that_reaches_out_through_another_link_is_refused(self):
        data = self.archive(('ok.txt', b'fine'), (self.special('here', tarfile.SYMTYPE, '.'), b''),
                            (self.special('out', tarfile.SYMTYPE, 'here/../../x'), b''))
        self.refused(data, 'unsafe', 'link')

    def test_a_file_written_through_a_link_does_not_land_outside(self):
        victim = os.path.join(self.home, 'victim')
        os.makedirs(victim)
        data = self.archive((self.special('evil', tarfile.SYMTYPE, '../../../victim'), b''), ('evil/pwned.txt', b'written outside'))
        digits = self.place(data)
        word = self.extract(digits)[0]
        self.assertIn(word, ('unsafe', 'failed'))
        self.assertEqual(os.listdir(victim), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.store, self.NAME))), [digits + '.tar.gz'])

    def test_links_that_stay_inside_are_kept_also_one_whose_target_was_left_out(self):
        data = self.archive(('maps/office.yaml', b'map'), (self.special('current-map', tarfile.SYMTYPE, 'maps/office.yaml'), b''),
                            (self.special('maps/up', tarfile.SYMTYPE, '../maps'), b''),
                            (self.special('latest-run', tarfile.SYMTYPE, 'runs/2026-11-01'), b''))
        digits = self.place(data)
        self.assertEqual(self.extract(digits)[0], 'placed')
        unpacked = tree(os.path.join(self.store, self.NAME, digits))
        self.assertEqual(unpacked['latest-run'], 'link:runs/2026-11-01')
        self.assertEqual(unpacked['maps/up'], 'link:../maps')

    def test_special_permission_bits_do_not_survive(self):
        member = tarfile.TarInfo('tool')
        member.mode = 0o6777
        digits = self.place(self.archive((member, b'#!/bin/sh\n')))
        self.assertEqual(self.extract(digits)[0], 'placed')
        mode = stat.S_IMODE(os.stat(os.path.join(self.store, self.NAME, digits, 'tool')).st_mode)
        self.assertEqual(mode & 0o7022, 0, oct(mode))
        self.assertTrue(mode & stat.S_IXUSR)

    def test_what_is_not_an_archive_or_unpacks_to_too_much_is_refused(self):
        self.refused(b'this is not a tar.gz at all', 'damaged')
        digits = self.place(self.archive(('zeros.bin', b'\0' * 2_000_000)))
        before = tree(self.base)
        self.assertEqual(self.extract(digits, limit=1_000_000)[0], 'toolarge')
        self.assertEqual(tree(self.base), before)
        self.assertEqual(self.extract(digits, limit=3_000_000)[0], 'placed')

    def test_nothing_in_the_way_is_overwritten(self):
        data = self.archive(('a.txt', b'one'))
        digits = self.place(data)
        folder = os.path.join(self.store, self.NAME)
        os.makedirs(os.path.join(folder, 'current'))              # a real directory someone made
        with open(os.path.join(folder, 'current', 'mine.txt'), 'w') as handle:
            handle.write('keep me')
        self.assertEqual(self.extract(digits)[0:1] + (self.extract(digits)[2],), ('occupied', 'current'))
        self.assertTrue(os.path.exists(os.path.join(folder, 'current', 'mine.txt')))
        self.assertFalse(os.path.exists(os.path.join(folder, digits)))
        shutil.rmtree(os.path.join(folder, 'current'))
        with open(os.path.join(folder, digits), 'w') as handle:   # a file where the directory would go
            handle.write('in the way')
        self.assertEqual(self.extract(digits)[0], 'occupied')

    def test_a_bundle_directory_that_is_a_link_elsewhere_is_not_used(self):
        data = self.archive(('a.txt', b'one'))
        elsewhere = os.path.join(self.home, 'elsewhere')
        os.makedirs(elsewhere)
        os.makedirs(self.store)
        os.symlink(elsewhere, os.path.join(self.store, self.NAME))
        digits = self.place(data)
        self.assertEqual(self.extract(digits)[0], 'moved')
        self.assertEqual(os.listdir(elsewhere), [digits + '.tar.gz'])

    def test_a_missing_archive_and_arguments_that_are_not_plain_words_never_run(self):
        os.makedirs(os.path.join(self.store, self.NAME))
        self.assertEqual(self.extract('0' * 16)[0], 'missing')
        for bad in ('a b', 'x;y', '$(id)', '../x', "a'b", ''):
            with self.assertRaises(ValueError):
                device.command(bad, '0' * 16, 1)


class DirectoryRule(unittest.TestCase):
    def test_what_a_person_may_type(self):
        for good, kept in (('~/walk_programs', '~/walk_programs'), (' ~/walk_programs/ ', '~/walk_programs'), ('/opt/robot/nav', '/opt/robot/nav'),
                           ('~/程序 和 场景/v2', '~/程序 和 场景/v2'), ("~/it's $(x)", "~/it's $(x)")):
            self.assertEqual(device.directory(good), kept)

    def test_names_that_only_look_like_secrets_are_ordinary_directories(self):
        for good in ('~/secrets', '~/secrets_nav', '~/credentials_helper', '~/my.github', '~/environment'):
            self.assertEqual(device.directory(good), good)

    def test_what_is_refused_before_any_connection(self):
        for bad in ('', '   ', None, 7, 'walk_programs', './x', '~', '~/', '/', '~/a/../b', '/a/./b', '~/a//b', '~/a\nb', '~/a\x00b',
                    '~/a' + chr(0x202e) + 'b', '~/.ssh', '~/x/.ssh/keys', '/home/robot/.env', '~/work/.git', '~/.config/app', '~/keys/id_ecdsa', '~/neuxmind-bundles', '~/neuxmind-bundles/walk',
                    '~/' + 'x' * 400):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValueError):
                    device.directory(bad)

    def test_the_hexadecimal_argument_and_the_plain_name(self):
        self.assertEqual(device.hex_argument('~/a b'), '7e2f612062')
        self.assertTrue(device._ARGUMENT.fullmatch(device.hex_argument("~/it's $(x); 程序")))
        for text, name in (('walk_programs', 'walk_programs'), ('my prog (v2)', 'my_prog_v2'), ('.hidden', 'hidden'),
                           ('--rf', 'rf'), ('a' * 200, 'a' * 60)):
            self.assertEqual(device.plain_name(text), name)
        # A name of which nothing plain is left gets a few digits of its own, so two of them do not share a directory.
        fallbacks = [device.plain_name(text) for text in ('程序 和 场景', '我的程序', 'id_rsa', '', None)]
        self.assertTrue(all(device.NAME.fullmatch(name) and name.startswith('bundle-') for name in fallbacks), fallbacks)
        self.assertEqual(len(set(fallbacks[:3])), 3)
        self.assertEqual(device.plain_name('我的程序'), device.plain_name('我的程序'))
        self.assertNotEqual(device.plain_name('程序', seed='~/a/程序'), device.plain_name('程序', seed='~/b/程序'))


if __name__ == '__main__':
    unittest.main()
