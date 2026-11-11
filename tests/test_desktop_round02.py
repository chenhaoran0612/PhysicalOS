"""Round 02 desktop integration (INTERFACES §4.1 client, §4.5).

Real components wherever possible:
* runtime: the simulated quadruped served by the real runtime gateway;
* cloud: the real cloud app (test environment) on a uvicorn thread.
The §4.1 asynchronous on-site test is exercised against ``AsyncDevice`` (a
test-only protocol double) and, once the runtime implements §4.1, also
against the real runtime (``RealRuntimeAsyncEvaluationTests``).
"""
import json
import os
import socket
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brainos import connector
from brainos.cloud_account import cloud_url
from brainos.edge import Gateway, make_handler as edge_handler
from brainos.service import Platform
from brainos.skill_packages import read_folder
from round02_support import (AsyncDevice, CloudServer, PASSWORD, copy_example, runtime_support,
                             runtime_supports_async_evaluation, write_skill)
from test_product import DeviceFixture, Server

LEGACY_TOKEN = 'round02-legacy-token-0123456789'


def new_platform(tmp):
    platform = Platform(tmp + '/platform')
    pid = platform.add_project({'name': '第 02 轮', 'objective': '桌面端接入新三层'})['id']
    return platform, pid


def connect(platform, pid, url, token, family='quadruped'):
    robot = platform.add_robot({'project_id': pid, 'name': '测试机器人', 'family': family,
                                'endpoint': url, 'token': token})
    return platform.connect_robot({'robot_id': robot['id']})


class RuntimeCase(unittest.TestCase):
    """A connected real runtime simulated quadruped."""

    def setUp(self):
        try:
            self.support = runtime_support()
            self.fx = self.support.RuntimeFixture(http=True, time_scale=20.0)
        except Exception as exc:          # pragma: no cover - depends on runtime state
            self.skipTest(f'runtime 仿真本体不可用：{exc}')
        self.tmp = tempfile.TemporaryDirectory()
        self.platform, self.pid = new_platform(self.tmp.name)
        self.robot = connect(self.platform, self.pid, self.fx.url, self.support.TOKEN)

    def tearDown(self):
        self.platform.close()
        self.fx.close()
        self.tmp.cleanup()


# ------------------------------------------------------------------ 1. capability table
class CapabilityTableTests(RuntimeCase):
    def test_v2_manifest_table_with_spec_names_kinds_safety_and_params(self):
        detail = self.platform.robot_detail({'robot_id': self.robot['id'], 'project_id': self.pid})
        caps = detail['capabilities']
        self.assertEqual(caps['source'], 'v2')
        self.assertTrue(caps['simulated'])
        rows = {r['id']: r for r in caps['rows']}
        self.assertEqual(rows['base.velocity']['title'], '底盘速度')
        self.assertEqual(rows['base.velocity']['kind_name'], '即时指令')
        self.assertEqual(rows['base.velocity']['safety'], 'motion')
        self.assertIn('需现场授权', rows['base.velocity']['safety_name'])
        params = {p['name']: p for p in rows['base.velocity']['params']}
        self.assertEqual(params['max_vx']['text'], '1.0 m/s')
        self.assertEqual(rows['system.health']['safety_name'], '不产生运动')
        self.assertTrue(rows['safety.stop']['mandatory'])
        self.assertEqual(rows['base.navigate_to']['kind_name'], '持续动作')
        # Installed skills come from GET /v2/skills.
        self.assertTrue(detail['skills']['available'])
        self.assertNotIn('token', detail['robot'])

    def test_falls_back_to_v1_list_for_legacy_devices(self):
        tmp = tempfile.TemporaryDirectory()
        gateway = Gateway(DeviceFixture(), LEGACY_TOKEN, tmp.name + '/edge')
        server = Server(edge_handler(gateway))
        try:
            robot = connect(self.platform, self.pid, server.url, LEGACY_TOKEN, family='mobile')
            detail = self.platform.robot_detail({'robot_id': robot['id']})
            caps = detail['capabilities']
            self.assertEqual(caps['source'], 'v1')
            self.assertIn('v1 能力清单', caps['note'])
            rows = {r['id']: r for r in caps['rows']}
            self.assertEqual(rows['localization.pose']['title'], '定位')
            self.assertFalse(rows['navigation.goal']['standard'])     # not a standard ID, shown honestly
            self.assertEqual(rows['navigation.goal']['title'], '未在能力标准中')
            self.assertFalse(detail['skills']['available'])
        finally:
            server.close()
            gateway.store.close()
            tmp.cleanup()

    def test_unconnected_robot_has_no_table_and_does_not_touch_network(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': '未连接', 'family': 'mobile'})
        detail = self.platform.robot_detail({'robot_id': robot['id']})
        self.assertIsNone(detail['capabilities']['source'])
        self.assertFalse(detail['skills']['available'])


# ------------------------------------------------------------------ 2. skill packages
class SkillFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_text_files_and_skips_tests_hidden_and_bytecode(self):
        path = write_skill(self.root, files={'skill.py': 'def run(ctx):\n    return 1\n', 'lib/util.py': 'X = 1\n',
                                             'tests/test_x.py': 'assert True\n', '.secret': 'no', 'a.pyc': 'x'})
        package, info = read_folder(path)
        self.assertEqual(sorted(package['files']), ['lib/util.py', 'skill.py'])
        self.assertEqual(package['id'], 'desk.hello')
        self.assertEqual(info['sizes']['lib/util.py'], 6)

    def test_refuses_symlinks_inside_and_as_the_folder_itself(self):
        path = write_skill(self.root)
        outside = self.root / 'outside.txt'
        outside.write_text('secret')
        os.symlink(outside, Path(path) / 'link.py')
        with self.assertRaisesRegex(ValueError, '符号链接 link.py'):
            read_folder(path)
        os.unlink(Path(path) / 'link.py')
        os.symlink(self.root, Path(path) / 'loop')
        with self.assertRaisesRegex(ValueError, '符号链接 loop'):
            read_folder(path)
        os.unlink(Path(path) / 'loop')
        os.symlink(path, self.root / 'alias')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            read_folder(str(self.root / 'alias'))

    def test_skill_json_symlink_is_refused_even_when_target_is_valid(self):
        # Review D1: skill.json is read directly, not through the directory walk.
        path = Path(write_skill(self.root))
        real = self.root / 'elsewhere.json'
        real.write_text((path / 'skill.json').read_text())
        (path / 'skill.json').unlink()
        os.symlink(real, path / 'skill.json')
        with self.assertRaisesRegex(ValueError, 'skill.json 是符号链接'):
            read_folder(str(path))

    def test_special_files_are_refused(self):
        # Review D2: a FIFO as skill.json or inside the package fails closed, without blocking.
        path = Path(write_skill(self.root))
        os.mkfifo(path / 'pipe.py')
        with self.assertRaisesRegex(ValueError, '不是普通文件'):
            read_folder(str(path))
        (path / 'pipe.py').unlink()
        (path / 'skill.json').unlink()
        os.mkfifo(path / 'skill.json')
        with self.assertRaisesRegex(ValueError, 'skill.json 不是普通文件'):
            read_folder(str(path))

    def test_file_reader_itself_fails_closed(self):
        # The per-file reader is a second layer behind the directory walk (and the
        # only layer between a check and a concurrent swap): test it directly.
        from brainos.skill_packages import _read_file
        os.mkfifo(self.root / 'pipe')
        os.symlink(self.root / 'pipe', self.root / 'link')
        (self.root / 'ok.txt').write_text('ok')
        fd = os.open(self.root, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(ValueError, 'pipe 不是普通文件'):
                _read_file('pipe', fd, 100, 'pipe')
            with self.assertRaisesRegex(ValueError, 'link 是符号链接'):
                _read_file('link', fd, 100, 'link')
            self.assertEqual(_read_file('ok.txt', fd, 100, 'ok.txt'), 'ok')
        finally:
            os.close(fd)

    def test_device_file_as_skill_json_is_refused(self):
        path = Path(write_skill(self.root))
        (path / 'skill.json').unlink()
        try:
            os.mknod(path / 'skill.json', 0o600 | stat.S_IFCHR, os.makedev(1, 3))   # like /dev/null
        except (PermissionError, AttributeError, OSError) as exc:
            self.skipTest(f'不能创建设备文件：{exc}')
        with self.assertRaisesRegex(ValueError, 'skill.json 不是普通文件'):
            read_folder(str(path))

    def test_symlink_in_an_ancestor_of_the_folder_is_refused(self):
        # Review Q2-07: intermediate path components are not followed either.
        write_skill(self.root / 'real')
        os.symlink(self.root / 'real', self.root / 'via')
        with self.assertRaisesRegex(ValueError, '路径中的 .*/via 是符号链接'):
            read_folder(str(self.root / 'via' / 'desk.hello'))
        self.assertEqual(read_folder(str(self.root / 'real' / 'desk.hello'))[0]['id'], 'desk.hello')

    def test_hard_links_are_refused(self):
        # Review Q2-07: a hard link could carry a file from outside the folder into the package.
        path = Path(write_skill(self.root))
        secret = self.root / 'secret.txt'
        secret.write_text('token=abc')
        os.link(secret, path / 'notes.py')
        with self.assertRaisesRegex(ValueError, 'notes.py 是硬链接'):
            read_folder(str(path))
        (path / 'notes.py').unlink()
        os.link(path / 'skill.json', self.root / 'skill-copy.json')
        with self.assertRaisesRegex(ValueError, 'skill.json 是硬链接'):
            read_folder(str(path))

    def test_path_rules_and_size_limits(self):
        for bad, message in [('relative/path', '完整路径'), ('', '请选择'), ('/x/.ssh/skill', '隐藏目录'),
                             (str(self.root / 'missing'), '找不到')]:
            with self.assertRaisesRegex(ValueError, message):
                read_folder(bad)
        path = write_skill(self.root, files={'skill.py': 'x' * (512 * 1024 + 1)})
        with self.assertRaisesRegex(ValueError, '超过上限'):
            read_folder(path)
        path = write_skill(self.root, 'desk.binary', files={'skill.py': 'ok'})
        (Path(path) / 'blob.py').write_bytes(b'\xff\xfe\x00')
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            read_folder(path)
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, '没有 skill.json'):
            read_folder(str(empty))


class SkillInstallTests(RuntimeCase):
    def preview(self, path):
        return self.platform.preview_skill_package({'robot_id': self.robot['id'], 'path': path, 'project_id': self.pid})

    def install(self, preview, request_id='req-1', path=None):
        return self.platform.install_skill_package({'robot_id': self.robot['id'], 'path': path or preview['path'],
                                                    'package_digest': preview['package_digest'],
                                                    'request_id': request_id, 'project_id': self.pid})

    def test_compatible_package_installs_idempotently_and_is_listed(self):
        path = copy_example(self.tmp.name, 'patrol')
        preview = self.preview(path)
        self.assertTrue(preview['can_install'])
        self.assertEqual(preview['skill']['id'], 'patrol')
        self.assertEqual(preview['compatibility']['summary'], '兼容，可以安装')
        self.assertIn('仿真本体', ' '.join(preview['compatibility']['notes']))
        result = self.install(preview)
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        self.assertEqual([s['id'] for s in result['skills']['items']], ['patrol'])
        # Same request again: the recorded outcome, nothing re-sent.
        again = self.install(preview)
        self.assertEqual(again['id'], result['id'])
        self.assertEqual(again['status'], 'succeeded')
        with self.assertRaisesRegex(ValueError, '另一次安装'):
            self.platform.install_skill_package({'robot_id': self.robot['id'], 'path': path,
                                                 'package_digest': 'other', 'request_id': 'req-1'})
        detail = self.platform.robot_detail({'robot_id': self.robot['id']})
        self.assertEqual(detail['skills']['items'][0]['title'], '巡逻')

    def test_incompatible_package_explained_and_never_sent(self):
        path = copy_example(self.tmp.name, 'wave')
        preview = self.preview(path)
        self.assertFalse(preview['can_install'])
        texts = [m['text'] for m in preview['compatibility']['missing_detail']]
        self.assertTrue(any('「机械臂关节位置」（arm.joint_position）' in t for t in texts), texts)
        self.assertTrue(any('「夹爪」' in t for t in texts), texts)
        with self.assertRaisesRegex(ValueError, '不兼容，不能安装'):
            self.install(preview)
        self.assertFalse(any(o.get('kind') == 'skill_install' for o in self.platform.store.list('operations')))
        status, body = self.support.http(self.fx.url, '/v2/skills')
        self.assertEqual(body['skills'], [])

    def test_parameter_constraint_violation_in_plain_words(self):
        path = write_skill(self.tmp.name, 'desk.fast', requires=('base.velocity',),
                           constraints={'base.velocity': {'max_vx': {'min': 2.0}}, 'sensor.imu': {'rate_hz': {'min': 1}}})
        preview = self.preview(path)
        self.assertFalse(preview['can_install'])
        self.assertEqual(preview['compatibility']['violation_texts'],
                         ['「底盘速度」（base.velocity）的参数 max_vx：技能需要至少 2.0 m/s，这台机器人声明为 1.0 m/s'])

    def test_folder_changed_after_preview_must_be_previewed_again(self):
        path = write_skill(self.tmp.name, 'desk.change')
        preview = self.preview(path)
        Path(path, 'skill.py').write_text('def run(ctx):\n    return {"changed": True}\n')
        with self.assertRaisesRegex(ValueError, '重新预检'):
            self.install(preview)

    def test_device_refusal_is_recorded_as_rejected_not_unknown(self):
        path = write_skill(self.tmp.name, 'desk.immutable')
        first = self.install(self.preview(path), 'req-a')
        self.assertEqual(first['status'], 'succeeded')
        Path(path, 'skill.py').write_text('def run(ctx):\n    return 2\n')   # same id@version, other content
        second = self.install(self.preview(path), 'req-b')
        self.assertEqual(second['status'], 'rejected')
        self.assertIn('设备拒绝安装', second['error'])

    def test_legacy_device_without_v2_cannot_preview(self):
        tmp = tempfile.TemporaryDirectory()
        gateway = Gateway(DeviceFixture(), LEGACY_TOKEN, tmp.name + '/edge')
        server = Server(edge_handler(gateway))
        try:
            robot = connect(self.platform, self.pid, server.url, LEGACY_TOKEN, family='mobile')
            with self.assertRaisesRegex(ValueError, '/v2/manifest'):
                self.platform.preview_skill_package({'robot_id': robot['id'], 'path': write_skill(tmp.name)})
        finally:
            server.close()
            gateway.store.close()
            tmp.cleanup()


# ------------------------------------------------------------------ 3. cloud account
class CloudUrlTests(unittest.TestCase):
    def test_https_required_except_loopback(self):
        self.assertEqual(cloud_url('https://cloud.neuxmind.example/'), 'https://cloud.neuxmind.example')
        self.assertEqual(cloud_url('http://127.0.0.1:8800/api/v1'), 'http://127.0.0.1:8800')
        self.assertEqual(cloud_url('http://[::1]:8800'), 'http://[::1]:8800')
        for bad in ('http://cloud.neuxmind.example', 'http://192.168.1.5:8800', 'ftp://x', 'https://u:p@x.example',
                    'https://x.example/?a=1', 'cloud.example', 'http://localhost.example:8800'):
            with self.assertRaises(ValueError, msg=bad):
                cloud_url(bad)

    def test_localhost_is_pinned_to_the_literal_loopback_ip(self):
        # Review Q2-10: no name lookup decides where a clear-text token goes.
        self.assertEqual(cloud_url('http://localhost:8800'), 'http://127.0.0.1:8800')
        self.assertEqual(cloud_url('https://localhost'), 'https://127.0.0.1')

    def test_private_link_local_and_metadata_addresses(self):
        for bad in ('https://10.0.0.5', 'https://192.168.1.5:8443', 'https://172.16.0.9', 'https://100.64.1.1',
                    'https://[fe80::1]', 'https://169.254.10.10', 'https://[fd00::5]'):
            with self.assertRaisesRegex(ValueError, '内网或链路本地', msg=bad):
                cloud_url(bad)
        for bad in ('https://169.254.169.254', 'https://[fd00:ec2::254]', 'https://[::ffff:169.254.169.254]'):
            with self.assertRaisesRegex(ValueError, '元数据', msg=bad):
                cloud_url(bad)
        with self.assertRaisesRegex(ValueError, '不是可连接'):
            cloud_url('https://0.0.0.0')
        with patch.dict(os.environ, {'NEUXMIND_CLOUD_ALLOW_PRIVATE': '1'}):
            self.assertEqual(cloud_url('https://10.0.0.5'), 'https://10.0.0.5')
            with self.assertRaisesRegex(ValueError, '元数据'):
                cloud_url('https://169.254.169.254')
            with self.assertRaisesRegex(ValueError, 'https'):
                cloud_url('http://10.0.0.5')            # opt-in never allows clear text

    def test_host_names_are_checked_against_the_address_actually_connected(self):
        # A DNS answer that turns private after the first lookup (rebinding) is refused
        # before any connection is made.
        answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 443))]])
        connects = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return next(answers)

        tmp = tempfile.TemporaryDirectory()
        platform, _ = new_platform(tmp.name)
        try:
            platform.cloud.configure({'base_url': 'https://cloud.rebind.example'})
            with patch('socket.getaddrinfo', fake_getaddrinfo), \
                    patch('socket.socket.connect', lambda self, addr: connects.append(addr)):
                with self.assertRaisesRegex(ValueError, '内网或链路本地地址 10.1.2.3'):
                    platform.cloud.register({'email': 'a@b.example', 'password': PASSWORD})
            self.assertEqual(connects, [])
        finally:
            platform.close()
            tmp.cleanup()


class CloudAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cloud = CloudServer()
        except Exception as exc:          # pragma: no cover - depends on cloud dependencies
            raise unittest.SkipTest(f'云平台应用不可用：{exc}')
        cls.cloud.seed_models()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform, self.pid = new_platform(self.tmp.name)
        self.account = self.platform.cloud
        self.account.configure({'base_url': self.cloud.url})
        self.email = f'user-{time.monotonic_ns()}@desktop.test'

    def tearDown(self):
        self.platform.close()
        self.tmp.cleanup()

    def token_file(self):
        return Path(self.tmp.name) / 'platform' / 'credentials' / 'cloud-session.json'

    def test_register_verify_wallet_and_token_stays_in_credential_file(self):
        result = self.account.register({'email': self.email, 'password': PASSWORD, 'name': '桌面用户'})
        self.assertEqual(result['status'], 'registered')
        self.assertTrue(result['account']['logged_in'])
        self.assertFalse(result['account']['email_verified'])
        verification = result['verification']
        self.assertTrue(verification['simulated'])          # dev/test sender only
        self.assertTrue(verification['dev_token'].startswith('nxv_'))
        # Credential file: 600 in a 700 directory; the token appears nowhere else.
        path = self.token_file()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        token = json.loads(path.read_text())['session_token']
        self.assertTrue(token)
        snapshot = json.dumps(self.platform.snapshot(), ensure_ascii=False)
        self.assertNotIn(token, snapshot)
        self.assertNotIn(PASSWORD, snapshot)
        db = sqlite3.connect(Path(self.tmp.name) / 'platform' / 'registry.sqlite3')
        dump = '\n'.join(db.iterdump())
        db.close()
        self.assertNotIn(token, dump)
        self.assertNotIn(PASSWORD, dump)
        self.assertNotIn(verification['dev_token'], dump)
        self.assertNotIn(token, json.dumps(result, ensure_ascii=False))

        overview = self.account.overview()
        self.assertEqual(overview['wallet']['available'], 0)
        self.assertFalse(overview['account']['email_verified'])
        with self.assertRaisesRegex(ValueError, '另一个账户|无效'):
            self.account.verify({'token': 'nxv_wrong'})
        verified = self.account.verify({'token': verification['dev_token']})
        self.assertTrue(verified['account']['email_verified'])
        self.assertTrue(self.account.overview()['account']['email_verified'])

    def test_login_logout_and_expired_session(self):
        self.account.register({'email': self.email, 'password': PASSWORD})
        self.account.logout()
        self.assertFalse(self.token_file().exists())
        self.assertFalse(self.account.summary()['logged_in'])
        with self.assertRaisesRegex(ValueError, '邮箱或密码不正确'):
            self.account.login({'email': self.email, 'password': 'wrong-password-123'})
        self.assertTrue(self.account.login({'email': self.email, 'password': PASSWORD})['account']['logged_in'])
        # The cloud revokes the session: the desktop forgets it and asks to log in again.
        token = json.loads(self.token_file().read_text())['session_token']
        self.cloud.api('POST', '/auth/logout', {}, token)
        overview = self.account.overview()
        self.assertFalse(overview['account']['logged_in'])
        self.assertIn('重新登录', overview['error'])
        self.assertFalse(self.token_file().exists())

    def test_changing_cloud_address_drops_the_session(self):
        self.account.register({'email': self.email, 'password': PASSWORD})
        self.account.configure({'base_url': 'https://other-cloud.example'})
        self.assertFalse(self.account.summary()['logged_in'])
        with self.assertRaisesRegex(ValueError, 'https'):
            self.account.configure({'base_url': 'http://cloud.example'})

    def test_models_filtered_by_robot_manifest(self):
        self.account.register({'email': self.email, 'password': PASSWORD})
        try:
            support = runtime_support()
            fx = support.RuntimeFixture(http=True, time_scale=20.0)
        except Exception as exc:          # pragma: no cover
            self.skipTest(f'runtime 仿真本体不可用：{exc}')
        try:
            robot = connect(self.platform, self.pid, fx.url, support.TOKEN)
            self.platform.robot_detail({'robot_id': robot['id']})          # reads /v2/manifest
            result = self.account.models({'robot_id': robot['id'], 'project_id': self.pid})
            ids = sorted(m['id'] for m in result['models'])
            self.assertEqual(ids, ['dog-nav-vla', 'echo-llm'])           # arm-only policy filtered out
            self.assertTrue(result['robot']['simulated'])
            self.assertEqual(result['robot']['manifest_source'], 'v2')
        finally:
            fx.close()

    def test_not_logged_in_is_explained(self):
        with self.assertRaisesRegex(ValueError, '请先登录'):
            self.account.resend()
        self.assertIsNone(self.account.overview()['wallet'])


# ------------------------------------------------------------------ 4. async on-site test (§4.1)
class AsyncOnsiteBase(unittest.TestCase):
    simulated = False
    supports_async = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform, self.pid = new_platform(self.tmp.name)
        self.device = AsyncDevice(simulated=self.simulated, supports_async=self.supports_async)
        self.robot = connect(self.platform, self.pid, self.device.url, AsyncDevice.TOKEN)
        skill = self.platform.add_skill({'project_id': self.pid, 'name': '巡逻', 'version': '0.1.0',
                                         'requires': ['base.navigate_to'], 'entrypoint': 'patrol',
                                         'success_condition': '到达', 'stop_condition': '超时', 'model_ids': []})
        self.app = self.platform.add_application({'project_id': self.pid, 'name': '巡检', 'version': '1.0',
                                                  'robot_id': self.robot['id'],
                                                  'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})

    def tearDown(self):
        self.device.close()
        self.platform.close()
        self.tmp.cleanup()

    def start(self):
        return self.platform.onsite_test({'application_id': self.app['id'], 'hardware_authorized': True})

    def poll(self, op):
        return self.platform.poll_onsite({'operation_id': op['id'], 'project_id': self.pid})

    def read(self):
        return self.platform.delivery_readiness({'application_id': self.app['id'], 'project_id': self.pid})


class AsyncOnsiteTests(AsyncOnsiteBase):
    def test_async_test_runs_then_completes_as_onsite_evidence(self):
        op = self.start()
        self.assertEqual((op['status'], op['mode']), ('running', 'async'))
        self.assertTrue(op['evaluation_id'].startswith('eval-'))
        evaluate = [c for c in self.device.calls if c == 'POST /v1/evaluate']
        self.assertEqual(len(evaluate), 1)
        d = self.read()
        self.assertEqual(d['next_step'], 'testing')
        self.assertEqual(d['onsite_running']['id'], op['id'])
        self.assertFalse(d['can_test'])
        self.assertTrue(d['can_stop_robot'])
        self.assertEqual(self.poll(op)['status'], 'running')
        self.device.finish(passed=True)
        done = self.poll(op)
        self.assertEqual(done['status'], 'succeeded')
        report = self.platform.store.get('evaluations', done['report_id'])
        self.assertEqual((report['scope'], report['passed']), ('onsite', True))
        self.assertEqual(self.read()['next_step'], 'publish')
        # Finished: further polls read nothing from the device.
        reads = len([c for c in self.device.calls if c.startswith('GET /v1/evaluations/')])
        self.assertEqual(self.poll(op)['status'], 'succeeded')
        self.assertEqual(len([c for c in self.device.calls if c.startswith('GET /v1/evaluations/')]), reads)

    def test_publish_is_refused_while_a_newer_test_runs(self):
        first = self.start()
        self.device.finish(True)
        report_id = self.poll(first)['report_id']
        self.start()
        with self.assertRaisesRegex(ValueError, '现场测试进行中'):
            self.platform.deploy({'application_id': self.app['id'], 'report_id': report_id})

    def test_stop_during_test_ends_it_failed_and_latches(self):
        op = self.start()
        stop = self.platform.stop_robot({'robot_id': self.robot['id'], 'application_id': self.app['id'],
                                         'project_id': self.pid})
        self.assertEqual(stop['status'], 'accepted')
        done = self.poll(op)
        report = self.platform.store.get('evaluations', done['report_id'])
        self.assertFalse(report['passed'])
        self.assertIn('停止', json.dumps(report['details'], ensure_ascii=False))
        d = self.read()
        self.assertTrue(d['estop']['latched'])
        self.assertEqual(d['start_blocked'], 'estop_latched')
        self.assertIn('stop', [o['kind'] for o in d['operations']])

    def test_unknown_result_is_recorded_and_never_retried(self):
        op = self.start()
        self.device.lose_all()
        result = self.poll(op)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('不会自动重试', result['error'])
        calls = list(self.device.calls)
        self.assertEqual(self.poll(op)['status'], 'unknown')
        self.assertEqual(self.device.calls, calls)                  # no read, no re-send
        self.assertEqual(self.read()['next_step'], 'reconcile')

    def test_status_read_failure_keeps_the_test_running(self):
        op = self.start()
        self.device.poll_failures = 1
        result = self.poll(op)
        self.assertEqual(result['status'], 'running')
        self.assertIn('HTTP 503', result['poll_error'])
        self.device.finish(True)
        self.assertEqual(self.poll(op)['status'], 'succeeded')

    def test_restart_keeps_running_test_pollable(self):
        op = self.start()
        self.platform.close()
        self.platform = Platform(self.tmp.name + '/platform')
        self.assertEqual(self.platform.store.get('operations', op['id'])['status'], 'running')
        self.device.finish(True)
        self.assertEqual(self.poll(op)['status'], 'succeeded')


class NonContractStatusTests(AsyncOnsiteBase):
    def test_status_outside_the_three_states_is_unknown_and_never_retried(self):
        # Review Q2-05: e.g. an internal "rejected" leaking from the device.
        op = self.start()
        self.device.force_status = 'rejected'
        result = self.poll(op)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('“rejected”', result['error'])
        calls = list(self.device.calls)
        self.poll(op)
        self.assertEqual(self.device.calls, calls)


class AsyncSimulatedOnsiteTests(AsyncOnsiteBase):
    simulated = True

    def test_async_simulated_result_stays_simulation_evidence(self):
        op = self.start()
        self.device.finish(True)
        done = self.poll(op)
        report = self.platform.store.get('evaluations', done['report_id'])
        self.assertEqual(report['scope'], 'simulation')
        self.assertTrue(report['passed'])                             # kept as reported, ADR-011
        with self.assertRaisesRegex(ValueError, '仿真本体的测试结果不能作为现场证据'):
            self.platform.deploy({'application_id': self.app['id'], 'report_id': report['id']})


class LegacySyncOnsiteTests(AsyncOnsiteBase):
    supports_async = False

    def test_device_without_evaluation_id_falls_back_to_sync(self):
        op = self.start()
        self.assertEqual((op['status'], op['mode']), ('succeeded', 'sync'))
        report = self.platform.store.get('evaluations', op['report_id'])
        self.assertEqual(report['scope'], 'onsite')
        self.assertFalse(any(c.startswith('GET /v1/evaluations/') for c in self.device.calls))


# ------------------------------------------------------------------ 5. e-stop blocks start (P3)
class EstopStartTests(AsyncOnsiteBase):
    def test_latched_estop_blocks_start_in_readiness_and_backend(self):
        op = self.start()
        self.device.finish(True)
        report_id = self.poll(op)['report_id']
        release = self.platform.deploy({'application_id': self.app['id'], 'report_id': report_id})
        self.assertEqual(release['status'], 'deployed')
        d = self.read()
        self.assertEqual((d['next_step'], d['start_blocked']), ('start', None))
        self.platform.stop_robot({'robot_id': self.robot['id']})
        d = self.read()
        self.assertEqual(d['start_blocked'], 'estop_latched')
        self.assertTrue(d['can_start'])                               # published; only the latch blocks
        with self.assertRaisesRegex(ValueError, '急停已锁存'):
            self.platform.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.assertNotIn('POST /v1/start', self.device.calls)
        self.platform.reset_robot({'robot_id': self.robot['id'], 'hardware_authorized': True, 'request_id': 'r1'})
        self.assertIsNone(self.read()['start_blocked'])


class RobotStopTests(unittest.TestCase):
    def test_stop_without_endpoint_is_explained_and_unreachable_is_unknown(self):
        tmp = tempfile.TemporaryDirectory()
        platform, pid = new_platform(tmp.name)
        try:
            robot = platform.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile'})
            with self.assertRaisesRegex(ValueError, '硬件急停'):
                platform.stop_robot({'robot_id': robot['id']})
            platform.configure_connection({'kind': 'robots', 'id': robot['id'], 'endpoint': 'http://127.0.0.1:9'})
            result = platform.stop_robot({'robot_id': robot['id']})
            self.assertEqual(result['status'], 'unknown')
            self.assertIn('现场确认', result['error'])
        finally:
            platform.close()
            tmp.cleanup()


class ConnectorCallTests(unittest.TestCase):
    def test_refused_connection_is_marked_not_sent(self):
        with self.assertRaises(connector.DeviceError) as ctx:
            connector.call('http://127.0.0.1:9', '/v2/manifest')
        self.assertIs(ctx.exception.sent, False)


# ------------------------------------------------------------------ real runtime §4.1
@unittest.skipUnless(runtime_supports_async_evaluation(), 'runtime 尚未实现 §4.1 异步现场测试')
class RealRuntimeAsyncEvaluationTests(RuntimeCase):
    def test_real_runtime_async_evaluation_round_trip(self):
        self.fx.install(self.support.example('status_report'))
        p = self.platform
        skill = p.add_skill({'project_id': self.pid, 'name': '状态报告', 'version': '0.1.0',
                             'requires': ['system.health'], 'entrypoint': 'status_report',
                             'success_condition': '报告生成', 'stop_condition': '超时', 'model_ids': []})
        app = p.add_application({'project_id': self.pid, 'name': '状态', 'version': '1.0', 'robot_id': self.robot['id'],
                                 'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})
        op = p.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertEqual(op.get('mode'), 'async', op)
        deadline = time.monotonic() + 20
        while op['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.1)
            op = p.poll_onsite({'operation_id': op['id']})
        self.assertEqual(op['status'], 'succeeded', op.get('error'))
        report = p.store.get('evaluations', op['report_id'])
        self.assertEqual(report['scope'], 'simulation')
        self.assertTrue(report['evidence_ref'].startswith('simulation://'))

    def test_real_runtime_stop_during_async_evaluation_fails_the_test(self):
        code = ('def run(ctx):\n'
                '    for _ in range(400):\n'
                '        ctx.check_stop()\n'
                '        ctx.sleep(0.5)\n'
                '    return {"done": True}\n')
        self.fx.install(self.support.custom_package('desk.slow', code, timeout_s=600))
        p = self.platform
        skill = p.add_skill({'project_id': self.pid, 'name': '慢技能', 'version': '0.1.0', 'requires': ['system.health'],
                             'entrypoint': 'desk.slow', 'success_condition': '完成', 'stop_condition': '超时',
                             'model_ids': []})
        app = p.add_application({'project_id': self.pid, 'name': '慢', 'version': '1.0', 'robot_id': self.robot['id'],
                                 'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 600}]})
        op = p.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertEqual(op['status'], 'running', op)
        self.assertEqual(p.stop_robot({'robot_id': self.robot['id']})['status'], 'accepted')
        deadline = time.monotonic() + 20
        while op['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.1)
            op = p.poll_onsite({'operation_id': op['id']})
        self.assertEqual(op['status'], 'succeeded', op.get('error'))
        self.assertFalse(p.store.get('evaluations', op['report_id'])['passed'])


if __name__ == '__main__':
    unittest.main()
