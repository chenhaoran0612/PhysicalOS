"""Issue #43: a robot's programs and scene data go to the cloud library and come back.

The robot is ``bundle_fixture.BundleDevice``: a local SSH server whose files live in a private
temporary directory and which runs only the product's own scripts, for real, inside that directory.
The cloud is ``skill_cloud_fixture.FakeSkillCloud``. Every program and map here is made up. Nothing
reaches a real server or a real machine.
"""
import gzip
import io
import os
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from brainos import robot_bundle
from brainos import robot_bundle_device as on_device
from brainos.cloud_account import CloudError
from brainos.errors import CodedError
from brainos.service import Platform
from bundle_fixture import BundleDevice
from package_cloud_fixture import wait_until
from skill_cloud_fixture import FakeSkillCloud, sha
from test_device_transfer import quiet_paramiko

ROBOT, PROJECT, CONNECTION = 'robot-fixture', 'project-fixture', 'connection-fixture'
HOME = '/home/robot'
STORE = HOME + '/neuxmind-bundles'
QUICK = {'fetch': {'retry_waits': (0, 0, 0), 'block_bytes': 8192}, 'transfer': {'retry_waits': (0, 0, 0), 'chunk_bytes': 8192},
         'retry_waits': (0, 0, 0)}
PROGRAM = {'walk.py': b'print("walk")\n', 'maps/office.yaml': 'places:\n  会议室: [1.0, 2.0]\n'.encode(),
           'runs/2026-11-01/log.txt': b'a run log', 'lib/__pycache__/walk.pyc': b'\x00cache', 'walk.py.bak': b'old'}
KEPT = ['lib', 'maps', 'maps/office.yaml', 'walk.py']        # lib/ held only __pycache__: the empty directory stays


def archive(members):
    """A tar.gz of name → content (bytes) or ('link', target)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz', format=tarfile.GNU_FORMAT) as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(content, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, content[1]
                tar.addfile(info)
            else:
                info.size, info.mode = len(content), 0o644
                tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


def names(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return sorted(m.name.removeprefix('./') for m in tar.getmembers() if m.name not in ('.', './'))


class BundleCase(unittest.TestCase):
    def setUp(self):
        quiet_paramiko(self)
        self.device = BundleDevice()
        self.addCleanup(self.device.close)
        self.cloud = FakeSkillCloud(chunk_bytes=4096)
        self.addCleanup(self.cloud.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.store, self.bundles = self.platform.store, self.platform.robot_bundle
        self.bundles.tuning = QUICK
        connection, secret = self.device.login()
        self.store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'family': 'quadruped',
                                  'connection_id': CONNECTION})
        self.store.put('device_connections', {**connection, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': PROJECT})
        self.store.put('agent_secrets', {**secret, 'id': CONNECTION})
        self.cloud.sign_in(self.platform)

    def program(self, directory='walk_programs', files=PROGRAM):
        for name, data in files.items():
            self.device.write('%s/%s/%s' % (HOME, directory, name), data)

    def collect(self, directory='~/walk_programs', **options):
        return self.bundles.collect(ROBOT, PROJECT, directory, **{'authorized': True, **options})

    def install(self, material_id, **options):
        return self.bundles.install(ROBOT, PROJECT, material_id, **{'authorized': True, **options})

    def refused(self, call, kind=CodedError):
        with self.assertRaises(kind) as caught:
            call()
        return caught.exception

    def cache(self):
        directory = os.path.join(self.store.root, 'bundle-cache')
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def events(self, action):
        return [event for event in self.store.list('events') if event['action'] == action]

    def on_device(self, path):
        return self.device.read(path)


class Collect(BundleCase):
    def test_a_directory_on_the_robot_becomes_a_bundle_of_this_robot_in_the_library(self):
        self.program()
        seen = []
        result = self.collect(on_progress=seen.append)

        material = self.cloud.materials[result['material_id']]
        view = material['view']
        self.assertEqual((view['kind'], view['device_id'], view['device_name'], view['body_type']),
                         ('bundle', ROBOT, '测试机器人', 'quadruped'))
        self.assertTrue(view['filename'].startswith('walk_programs-') and view['filename'].endswith('.tar.gz'))
        self.assertEqual((result['sha256'], result['bytes']), (sha(material['data']), len(material['data'])))
        self.assertEqual(names(material['data']), KEPT)                 # runs/, __pycache__/ and *.bak* stayed behind
        self.assertEqual((result['name'], result['files'], result['outside_links']), ('walk_programs', 2, 0))
        # The robot was asked to pack, nothing else; its directory is as it was; what was packed is gone again.
        self.assertEqual([kind for kind, _ in self.device.bundle_scripts], ['pack'])
        self.assertEqual((self.device.scripts, self.device.refused), ([], []))
        self.assertEqual(self.on_device(HOME + '/walk_programs/runs/2026-11-01/log.txt'), b'a run log')
        self.assertEqual(self.device.names(STORE + '/.outgoing'), [])
        self.assertEqual(self.cache(), [])
        phases = [event['phase'] for event in seen]
        self.assertEqual((phases[0], phases[-1]), ('packing', 'done'))
        self.assertLess(phases.index('fetching'), phases.index('uploading'))
        self.assertEqual(self.events('bundle.collected')[0]['details']['sha256'], result['sha256'])

    def test_what_a_person_typed_reaches_the_robot_only_as_hexadecimal_digits(self):
        directory = "it's $(touch pwned); `id` 程序"
        self.program(directory, {'a.txt': b'data'})
        result = self.collect('~/' + directory)
        self.assertEqual(names(self.cloud.materials[result['material_id']]['data']), ['a.txt'])
        self.assertEqual(result['name'], 'it_s_touch_pwned_id')
        (kind, arguments), = self.device.bundle_scripts
        self.assertTrue(all(on_device._ARGUMENT.fullmatch(word) for word in arguments), arguments)
        self.assertEqual(bytes.fromhex(arguments[0]).decode(), '~/' + directory)
        self.assertEqual([n for n in self.device.names(HOME) if 'pwned' in n and 'touch' not in n], [])

    def test_nothing_is_asked_of_the_robot_without_consent_a_valid_directory_and_a_cloud_login(self):
        self.program()
        for authorized in (False, None, 'yes', 1):
            self.assertEqual(self.refused(lambda: self.collect(authorized=authorized)).code, 'bundle_not_authorized')
        for directory in ('walk_programs', '~', '~/a/../b', '~/.ssh', '~/a\nb', '', '~/neuxmind-bundles/x'):
            self.assertEqual(self.refused(lambda: self.collect(directory)).code, 'bundle_directory_invalid')
        self.platform.cloud._drop_token()
        self.assertEqual(self.refused(self.collect).code, 'cloud_login_required')
        self.cloud.sign_in(self.platform)
        self.store.put('robots', {**self.store.get('robots', ROBOT), 'connection_id': None})
        self.assertEqual(self.refused(self.collect).code, 'bundle_no_ssh')
        self.assertEqual((self.device.connections, self.cloud.materials), (0, {}))

    def test_what_the_robot_refuses_to_pack_is_told_in_plain_words(self):
        self.program()
        self.assertEqual(self.refused(lambda: self.collect('~/not-there')).code, 'bundle_directory_missing')
        self.assertEqual(self.refused(lambda: self.collect(HOME)).code, 'bundle_directory_refused')
        os.mkfifo(self.device.local(HOME + '/walk_programs/control'))
        error = self.refused(self.collect)
        self.assertEqual(error.code, 'bundle_directory_refused')
        self.assertIn('control', str(error))
        self.assertEqual(self.cloud.materials, {})

    def test_a_directory_over_the_limit_is_not_collected(self):
        self.program(files={'big.bin': os.urandom(60_000)})
        with mock.patch.object(on_device, 'MAX_ARCHIVE_BYTES', 20_000):
            self.assertEqual(self.refused(self.collect).code, 'bundle_too_large')
        self.assertEqual((self.device.names(STORE + '/.outgoing'), self.cloud.materials), ([], {}))

    def test_a_connection_that_breaks_while_reading_back_costs_only_what_was_in_flight(self):
        self.program(files={'big.bin': os.urandom(300_000)})
        self.device.read_cuts = [60_000, 150_000]
        result = self.collect()
        self.assertEqual(sha(self.cloud.materials[result['material_id']]['data']), result['sha256'])
        self.assertLess(self.device.read_bytes, 2 * result['bytes'])      # it went on, it did not start over twice

    def test_a_robot_that_is_unreachable_for_a_moment_while_reading_back_is_asked_again(self):
        self.program(files={'big.bin': os.urandom(200_000)})

        def cut_then_vanish(kind, arguments):
            self.device.read_cuts = [50_000]

        self.device.before_bundle_script = cut_then_vanish
        original = self.device.reading

        def reading(handle, offset, data):
            cutting = bool(self.device.read_cuts) and self.device.read_bytes + len(data) >= self.device.read_cuts[0]
            if cutting:
                self.device.refuse_next = 2             # after this cut the next two connections get no answer
            return original(handle, offset, data)
        self.device.reading = reading
        result = self.collect()
        self.assertEqual(sha(self.cloud.materials[result['material_id']]['data']), result['sha256'])
        self.assertEqual(self.device.refuse_next, 0)

    def test_bundle_names_come_from_the_label_or_the_file_name(self):
        name = robot_bundle.bundle_name
        self.assertEqual(name({'model': 'walk_programs', 'filename': 'x.tar.gz'}), 'walk_programs')
        self.assertEqual(name({'model': '程序', 'filename': 'walk_programs-20261124-010203.tar.gz'}), 'walk_programs')
        self.assertEqual(name({'model': '', 'filename': 'my-prog-v2.tgz'}), 'my-prog-v2')
        self.assertEqual(name({'filename': '../../etc.tar.gz'}), 'etc')
        self.assertEqual(name({'model': 'bundle-1a2b3c4d', 'filename': 'x.tar.gz'}), 'bundle-1a2b3c4d')
        self.assertTrue(name({}).startswith('bundle-'))

    def test_a_link_that_never_carries_anything_gives_up_and_leaves_nothing_here(self):
        self.program(files={'big.bin': os.urandom(100_000)})
        self.device.read_cuts = [1] * 10
        self.assertEqual(self.refused(self.collect).code, 'bundle_fetch_interrupted')
        self.assertEqual((self.cache(), self.cloud.materials), ([], {}))

    def test_bytes_that_arrive_wrong_are_not_sent_to_the_cloud(self):
        self.program(files={'big.bin': os.urandom(100_000)})
        self.device.corrupt_read_at = 50_000
        self.assertEqual(self.refused(self.collect).code, 'bundle_checksum_mismatch')
        self.assertEqual((self.cache(), self.cloud.materials, self.cloud.uploads), ([], {}, {}))

    def test_the_same_content_again_is_not_stored_a_second_time(self):
        self.program(files={'a.txt': b'data'})
        first = self.collect()
        self.assertFalse(first['already_in_library'])
        asked = len(self.cloud.requests)
        again = self.collect()
        self.assertEqual(again['sha256'], first['sha256'])              # the same files pack to the same bytes
        self.assertTrue(again['already_in_library'])
        self.assertEqual((again['material_id'], len(self.cloud.materials)), (first['material_id'], 1))
        later = [request for request in self.cloud.requests[asked:] if request[0] in ('POST', 'PUT')]
        self.assertEqual(later, [])                                     # nothing was uploaded, nothing was read back
        self.assertEqual(self.device.names(STORE + '/.outgoing'), [])
        self.device.write(HOME + '/walk_programs/a.txt', b'other data')
        self.assertFalse(self.collect()['already_in_library'])
        self.assertEqual(len(self.cloud.materials), 2)

    def test_what_stayed_behind_is_told_with_the_result(self):
        self.program(files={'walk.py': b'x', 'id_ecdsa': b'k', '.netrc': b'n', 'cert.pem': b'c', '.git/config': b'g',
                            'secrets_loader/__init__.py': b'p', 'runs/1/log': b'l', 'old.bak': b'o'})
        result = self.collect()
        self.assertEqual(names(self.cloud.materials[result['material_id']]['data']), ['secrets_loader', 'secrets_loader/__init__.py', 'walk.py'])
        self.assertEqual((result['guarded']['count'], sorted(result['guarded']['names'])),
                         (4, ['./.git', './.netrc', './cert.pem', './id_ecdsa']))
        self.assertEqual((result['skipped']['count'], sorted(result['skipped']['names'])), (2, ['./old.bak', './runs']))

    def test_a_directory_that_is_a_place_of_credentials_under_another_name_is_refused(self):
        self.device.write(HOME + '/.ssh/authorized_keys', b'ssh-ed25519 AAAA')
        self.device.link(HOME + '/tools', HOME + '/.ssh')
        error = self.refused(lambda: self.collect('~/tools'))
        self.assertEqual(error.code, 'bundle_directory_refused')
        self.assertIn('凭据', str(error))
        self.assertEqual(self.cloud.materials, {})

    def test_directories_without_a_plain_name_do_not_share_one(self):
        from brainos import device_access
        self.assertEqual(on_device.CHANNEL_REFUSES.pattern, device_access.SENSITIVE.pattern)
        results = []
        for directory in ('我的程序', '地图 数据', 'secrets_nav'):
            self.program(directory, {'a.txt': directory.encode()})
            results.append(self.collect('~/' + directory))
        found = [result['name'] for result in results]
        self.assertEqual(len(set(found)), 3, found)
        self.assertTrue(all(name.startswith('bundle-') for name in found), found)
        for result, directory in zip(results, ('我的程序', '地图 数据', 'secrets_nav')):
            view = self.cloud.materials[result['material_id']]['view']
            self.assertIn('~/' + directory, view['description'])         # the list can say which directory it was
            installed = self.install(result['material_id'])
            self.assertEqual(installed['name'], result['name'])
        self.assertEqual(len(self.device.names(STORE)), 4)               # three bundles and .outgoing

    def test_an_unfinished_upload_of_the_other_kind_is_cancelled_and_the_collection_goes_on(self):
        self.program(files={'a.txt': b'data'})

        def conflict(event, armed=[True]):
            if event['phase'] == 'fetching' and armed:       # the next thing asked of the cloud is the start of the upload
                armed.clear()
                self.cloud.failures.append((409, {'error': 'kind', 'code': 'upload_kind_conflict', 'upload_id': 'upl_' + '1' * 20, 'kind': 'package'}))
                self.cloud.uploads['upl_' + '1' * 20] = {'id': 'upl_' + '1' * 20, 'fields': {'sha256': 'x'}, 'chunks': {}, 'count': 1}
        result = self.collect(on_progress=conflict)
        self.assertTrue(result['cancelled_other_upload'])
        self.assertIn(('DELETE', '/api/v1/library/uploads/upl_' + '1' * 20), self.cloud.requests)
        self.assertIn(result['material_id'], self.cloud.materials)

    def test_a_chunk_the_cloud_fails_on_is_sent_again_and_a_refusal_ends_it(self):
        self.program(files={'big.bin': os.urandom(30_000)})

        def fail_once(event, armed=[True]):
            if event['phase'] == 'uploading' and armed:
                armed.clear()
                self.cloud.failures.append((503, {'error': 'busy', 'code': 'fixture_failure'}))
        result = self.collect(on_progress=fail_once)
        self.assertIn(result['material_id'], self.cloud.materials)

        self.program('other', {'x.bin': os.urandom(30_000)})

        def refuse(event, armed=[True]):
            if event['phase'] == 'uploading' and armed:
                armed.clear()
                self.cloud.failures.append((409, {'error': 'no room', 'code': 'library_quota_exceeded'}))
        error = self.refused(lambda: self.collect('~/other', on_progress=refuse), CloudError)
        self.assertEqual(error.code, 'library_quota_exceeded')
        self.assertEqual(self.cache(), [])


class Install(BundleCase):
    MEMBERS = {'walk.py': b'print("walk")\n', 'maps/office.yaml': b'places: {}\n', 'current-map': ('link', 'maps/office.yaml')}

    def add(self, members=None, **more):
        data = archive(members or self.MEMBERS)
        return self.cloud.add_bundle('walk_programs-20261124-010203.tar.gz', data, **{'device_id': ROBOT, 'model': 'walk_programs', **more}), data

    def test_a_bundle_comes_from_the_library_and_is_unpacked_next_to_the_original(self):
        self.program()
        material_id, data = self.add()
        digits = sha(data)[:16]
        seen = []
        result = self.install(material_id, on_progress=seen.append)

        folder = STORE + '/walk_programs'
        self.assertEqual((result['device_dir'], result['current'], result['archive']),
                         ('~/neuxmind-bundles/walk_programs/' + digits, '~/neuxmind-bundles/walk_programs/current',
                          '~/neuxmind-bundles/walk_programs/%s.tar.gz' % digits))
        self.assertEqual(self.on_device('%s/%s/walk.py' % (folder, digits)), b'print("walk")\n')
        self.assertEqual(self.on_device(folder + '/current/maps/office.yaml'), b'places: {}\n')
        self.assertEqual(os.readlink(self.device.local(folder + '/current')), digits)
        self.assertEqual(os.readlink(self.device.local('%s/%s/current-map' % (folder, digits))), 'maps/office.yaml')
        self.assertEqual(self.on_device(folder + '/%s.tar.gz' % digits), data)
        self.assertEqual(self.device.mode(folder + '/%s.tar.gz' % digits), 0o600)
        self.assertEqual((result['files'], result['already_unpacked'], result['sha256']), (2, False, sha(data)))
        self.assertIn('ros2-config.json', result['note'])
        # The original directory is as it was, and the robot ran the transfer's two scripts and the unpacking, nothing else.
        self.assertEqual(self.on_device(HOME + '/walk_programs/walk.py'), PROGRAM['walk.py'])
        self.assertEqual([kind for kind, _ in self.device.scripts] + [kind for kind, _ in self.device.bundle_scripts],
                         ['probe', 'finalize', 'extract'])
        self.assertEqual(self.device.refused, [])
        self.assertEqual(self.cache(), [])
        phases = [event['phase'] for event in seen]
        self.assertEqual(phases[-1], 'done')
        self.assertLess(phases.index('fetching'), phases.index('sending'))
        self.assertLess(phases.index('sending'), phases.index('unpacking'))
        self.assertEqual(self.events('bundle.installed')[0]['details']['device_dir'], result['device_dir'])

    def test_a_directory_changed_on_the_robot_is_set_aside_and_the_cloud_copy_is_unpacked_again(self):
        material_id, data = self.add()
        digits = sha(data)[:16]
        self.install(material_id)
        self.device.write('%s/walk_programs/%s/walk.py' % (STORE, digits), b'print("changed on the robot")')
        result = self.install(material_id)
        self.assertFalse(result['already_unpacked'])
        self.assertTrue(result['set_aside'].startswith('~/neuxmind-bundles/walk_programs/%s.changed-' % digits), result)
        self.assertEqual(self.on_device(STORE + '/walk_programs/current/walk.py'), b'print("walk")\n')
        aside = HOME + result['set_aside'][1:]
        self.assertEqual(self.on_device(aside + '/walk.py'), b'print("changed on the robot")')

    def test_installing_again_sends_nothing_and_unpacks_nothing(self):
        material_id, _ = self.add()
        self.install(material_id)
        sent = self.device.received
        again = self.install(material_id)
        self.assertTrue(again['already_unpacked'])
        self.assertIsNone(again['set_aside'])
        self.assertEqual(self.device.received, sent)

    def test_nothing_is_fetched_or_sent_without_consent_or_for_what_is_not_this_robots_bundle(self):
        material_id, data = self.add()
        self.assertEqual(self.refused(lambda: self.install(material_id, authorized=False)).code, 'bundle_not_authorized')
        cases = {'bundle_not_bundle': {'kind': 'package'}, 'bundle_other_device': {'device_id': 'robot-other', 'device_name': '别的机器人'},
                 'bundle_not_usable': {'status': 'parsing'}}
        for code, more in cases.items():
            other, _ = self.add({'x.txt': code.encode()}, **more)
            self.assertEqual(self.refused(lambda: self.install(other)).code, code)
        with mock.patch.object(on_device, 'MAX_ARCHIVE_BYTES', 10):
            self.assertEqual(self.refused(lambda: self.install(material_id)).code, 'bundle_too_large')
        with self.assertRaises(ValueError):
            self.install('../../etc')
        self.assertEqual((self.device.connections, self.cache()), (0, []))
        self.assertEqual([path for _, path in self.cloud.requests if path.endswith('/content')], [])

    def test_an_archive_that_would_leave_its_directory_is_not_unpacked(self):
        hostile = {'a link out': {'ok.txt': b'fine', 'out': ('link', '../../../walk_programs')},
                   'an absolute link': {'ok.txt': b'fine', 'etc': ('link', '/etc')},
                   'a path with ..': {'ok.txt': b'fine', '../escape.txt': b'escaped'}}
        self.program()
        for name, members in hostile.items():
            with self.subTest(name):
                material_id, data = self.add(members)
                error = self.refused(lambda: self.install(material_id))
                self.assertEqual(error.code, 'bundle_unsafe')
                left = self.device.names(STORE + '/walk_programs')        # the verified archives, nothing unpacked, no "current"
                self.assertIn(sha(data)[:16] + '.tar.gz', left)
                self.assertTrue(all(name.endswith('.tar.gz') for name in left), left)
        self.assertEqual(sorted(self.device.names(HOME + '/walk_programs')), ['lib', 'maps', 'runs', 'walk.py', 'walk.py.bak'])
        self.assertIsNone(self.on_device(STORE + '/escape.txt'))
        self.assertEqual(self.events('bundle.installed'), [])

    def test_a_file_that_is_not_an_archive_is_refused_on_the_robot(self):
        material_id = self.cloud.add_bundle('walk_programs-1.tar.gz', gzip.compress(b'not a tar'), ROBOT, model='walk_programs')
        self.assertIn(self.refused(lambda: self.install(material_id)).code, ('bundle_unsafe', 'bundle_extract_failed'))

    def test_what_was_collected_can_be_installed_again(self):
        self.program()
        collected = self.collect()
        result = self.install(collected['material_id'])
        folder = STORE + '/walk_programs/' + collected['sha256'][:16]
        self.assertEqual(result['device_dir'], '~/neuxmind-bundles/walk_programs/' + collected['sha256'][:16])
        self.assertEqual(self.on_device(folder + '/maps/office.yaml'), PROGRAM['maps/office.yaml'])
        self.assertEqual(sorted(self.device.names(folder)), ['lib', 'maps', 'walk.py'])


class FromThePage(BundleCase):
    def ask(self, **more):
        return {'robot_id': ROBOT, 'project_id': PROJECT, **more}

    def finished(self, job):
        self.assertTrue(wait_until(lambda: self.bundles.status(self.ask(job_id=job['id']))['status'] != 'running', 30))
        return self.bundles.status(self.ask(job_id=job['id']))

    def test_collect_and_install_run_in_the_background_and_the_overview_shows_them(self):
        self.program()
        job = self.finished(self.bundles.start_collect(self.ask(directory='~/walk_programs', authorized=True)))
        self.assertEqual((job['status'], job['kind'], job['result']['name']), ('done', 'collect', 'walk_programs'))
        foreign = self.cloud.add_bundle('walk_programs-20260101-000000.tar.gz', archive({'x.txt': b'someone else put this here'}), ROBOT,
                                        description='来自别处', created_by='usr_someone_else')
        overview = self.bundles.overview(self.ask())
        listed = {b['id']: b for b in overview['bundles']}
        self.assertEqual(sorted(listed), sorted([job['result']['material_id'], foreign]))
        self.assertEqual((overview['ssh'], overview['logged_in']), (True, True))
        mine = listed[job['result']['material_id']]
        self.assertEqual((mine['collected_here'], mine['directory']), (True, '~/walk_programs'))
        self.assertEqual((listed[foreign]['collected_here'], listed[foreign]['directory'], listed[foreign]['created_by']),
                         (False, '', 'usr_someone_else'))
        del self.cloud.materials[foreign]

        installed = self.finished(self.bundles.start_install(self.ask(material_id=job['result']['material_id'], authorized=True)))
        self.assertEqual((installed['status'], installed['kind']), ('done', 'install'))
        overview = self.bundles.overview(self.ask())
        self.assertEqual([entry['kind'] for entry in overview['history']], ['install', 'collect'])
        self.assertEqual(len(overview['jobs']), 2)

    def test_what_can_be_refused_at_once_is_refused_before_a_job_exists(self):
        for bad in ({'directory': '~/walk_programs'}, {'directory': '~/walk_programs', 'authorized': 'true'},
                    {'directory': 'x', 'authorized': True}):
            with self.assertRaises(CodedError):
                self.bundles.start_collect(self.ask(**bad))
        with self.assertRaises(CodedError):
            self.bundles.start_install(self.ask(material_id='mat_' + '0' * 20))
        with self.assertRaises(ValueError):
            self.bundles.overview({'robot_id': ROBOT, 'project_id': 'another-project'})
        self.assertEqual(self.bundles.overview(self.ask())['jobs'], [])

    def test_one_at_a_time_per_robot_and_a_stop_ends_it(self):
        self.program()
        entered, release = threading.Event(), threading.Event()

        def hold(kind, arguments):
            entered.set()
            release.wait(20)
        self.device.before_bundle_script = hold
        job = self.bundles.start_collect(self.ask(directory='~/walk_programs', authorized=True))
        self.assertTrue(entered.wait(20))
        with self.assertRaises(CodedError) as caught:
            self.bundles.start_collect(self.ask(directory='~/walk_programs', authorized=True))
        self.assertEqual(caught.exception.code, 'bundle_busy')
        self.bundles.stop(self.ask(job_id=job['id']))
        release.set()
        ended = self.finished(job)
        self.assertEqual((ended['status'], ended['error']), ('stopped', None))
        self.assertEqual(self.cloud.materials, {})
        # What the robot packed meanwhile is not left there. The script on the robot outlives the closed
        # session by a moment and removes its own side files when it notices: wait for that, do not race it.
        deadline = time.monotonic() + 10
        while self.device.names(STORE + '/.outgoing') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.device.names(STORE + '/.outgoing'), [])

    def test_a_failure_is_told_with_its_code_and_a_job_of_another_project_is_not_found(self):
        job = self.finished(self.bundles.start_collect(self.ask(directory='~/not-there', authorized=True)))
        self.assertEqual((job['status'], job['code']), ('failed', 'bundle_directory_missing'))
        with self.assertRaises(ValueError):
            self.bundles.status({'job_id': job['id'], 'project_id': 'another-project'})

    def test_without_a_cloud_login_the_overview_says_so_and_asks_nothing(self):
        self.platform.cloud._drop_token()
        overview = self.bundles.overview(self.ask())
        self.assertEqual((overview['logged_in'], overview['bundles']), (False, []))
        self.assertEqual(self.cloud.requests, [])


class PageScript(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, 'tests/robot_bundle_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'robot bundle logic checks passed: \d+')


if __name__ == '__main__':
    unittest.main()
