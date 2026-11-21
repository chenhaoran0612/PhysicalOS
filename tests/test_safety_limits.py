"""Issue #22 (R1-06): safety limits are filled in per device by a person; a device without them cannot move.

The console reads what a device holds, sends what a person filled in, and records who did it. The
device is the authority: it keeps the values, checks them against the platform's and the body's
maximum, and refuses motion while they are missing. The device here is a protocol double
(``tests/safety_limits_fixture.py``): nothing reaches a real machine.
"""
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from brainos import connector
from brainos.errors import CodedError, error_payload
from brainos.httpd import FastBindHTTPServer
from brainos.server import Application, make_handler
from brainos.service import Platform
from safety_limits_fixture import GROUP, MISSING, SCHEMA, LimitsDevice

ROOT = Path(__file__).resolve().parents[1]
FILLED = {'max_linear_speed': 0.5, 'max_angular_speed': 0.8, 'command_timeout_s': 0.5}
PERSON = {'id': 'user-1', 'name': '张工', 'email': 'zhang@example.test'}


class Case(unittest.TestCase):
    """A workspace with one project and one device double registered as a robot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.platform.close)
        self.pid = self.platform.add_project({'name': 'Limits', 'objective': 'fill in safety limits'})['id']
        self.device = self.make_device()
        self.limits = self.platform.safety_limits

    def make_device(self, **options):
        device = LimitsDevice(**options)
        self.addCleanup(device.close)
        return device

    def robot(self, device=None, connect=False, project_id=None):
        device = device or self.device
        robot = self.platform.add_robot({'project_id': project_id or self.pid, 'name': 'Dog', 'family': 'quadruped',
                                         'endpoint': device.url, 'token': device.token})
        if connect:
            self.platform.connect_robot({'robot_id': robot['id']})
        return robot['id']

    def read(self, robot_id, project_id=None):
        return self.limits.read({'robot_id': robot_id, 'project_id': project_id or self.pid})

    def save(self, robot_id, values=None, user=None, **extra):
        return self.limits.save({'robot_id': robot_id, 'project_id': self.pid,
                                 'limits': {GROUP: dict(FILLED if values is None else values)}, **extra},
                                PERSON if user is None else user)

    def stored_summary(self, robot_id):
        return (self.platform.store.get('robots', robot_id).get('device_status') or {}).get('safety_limits')

    def events(self, action):
        return [e for e in self.platform.store.list('events') if e['action'] == action]


class ReadTests(Case):
    def test_a_device_without_limits_says_it_cannot_move(self):
        robot_id = self.robot()

        view = self.read(robot_id)

        self.assertEqual((view['robot_id'], view['supported']), (robot_id, True))
        self.assertEqual((view['required'], view['filled'], view['motion_blocked']), (True, False, True))
        self.assertEqual(view['reason'], MISSING)
        self.assertEqual((view['limits'], view['set_by'], view['set_at'], view['effective']), (None, None, None, None))
        self.assertEqual((view['revision'], view['history'], view['groups'], view['invalid']), (0, [], [GROUP], None))
        linear = view['bounds'][GROUP]['max_linear_speed']
        self.assertEqual(linear, {'unit': 'm/s', 'min': 0.0, 'min_exclusive': True, 'max': 1.0,
                                  'platform_max': 3.0, 'body_max': 1.0})
        self.assertEqual(view['bounds'][GROUP]['command_timeout_s']['body_max'], None)

    def test_a_device_with_limits_says_who_filled_them_and_when(self):
        self.device.fill(2.0, 0.8, 0.5, '李工')
        robot_id = self.robot()

        view = self.read(robot_id)

        self.assertEqual((view['filled'], view['motion_blocked'], view['reason']), (True, False, None))
        self.assertEqual(view['limits'], {GROUP: {'max_linear_speed': 2.0, 'max_angular_speed': 0.8, 'command_timeout_s': 0.5}})
        self.assertEqual(view['set_by'], '李工')
        self.assertAlmostEqual(view['set_at'], self.device.set_at)
        # The body itself declares 1.0 m/s: that, not the 2.0 that was filled in, is what is enforced.
        self.assertEqual(view['effective'][GROUP]['max_linear_speed'], 1.0)
        self.assertEqual([(h['revision'], h['set_by']) for h in view['history']], [(1, '李工')])

    def test_a_stored_record_the_device_did_not_use_counts_as_not_filled(self):
        self.device.invalid = '填写时间无效'
        robot_id = self.robot()

        view = self.read(robot_id)

        self.assertEqual((view['filled'], view['motion_blocked'], view['invalid']), (False, True, '填写时间无效'))
        self.assertRegex(view['reason'], '设备上保存的安全限值无效（填写时间无效）')       # the device's own words, as they are

    def test_a_simulated_body_is_not_blocked_without_limits(self):
        robot_id = self.robot(self.make_device(required=False))

        view = self.read(robot_id)

        self.assertEqual((view['required'], view['filled'], view['motion_blocked']), (False, False, False))

    def test_an_older_device_is_reported_as_unsupported_not_as_an_error(self):
        robot_id = self.robot(self.make_device(supported=False))

        view = self.read(robot_id)

        self.assertEqual(set(view), {'robot_id', 'supported', 'reason'})
        self.assertEqual((view['robot_id'], view['supported']), (robot_id, False))
        self.assertRegex(view['reason'], '版本较旧')
        self.assertRegex(view['reason'], '升级')

    def test_an_unreachable_device_is_an_error_that_says_so(self):
        robot_id = self.robot()
        self.device.close()

        with self.assertRaises(connector.DeviceError) as caught:
            self.read(robot_id)

        self.assertEqual(caught.exception.code, 'device_unreachable')
        self.assertRegex(str(caught.exception), '无法连接')
        self.assertEqual(error_payload(caught.exception)['code'], 'device_unreachable')

    def test_a_robot_without_a_service_address_cannot_be_read(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'No address', 'family': 'quadruped'})

        with self.assertRaisesRegex(ValueError, '服务地址'):
            self.read(robot['id'])

    def test_a_robot_of_another_project_is_refused_before_the_device_is_asked(self):
        robot_id = self.robot()
        other = self.platform.add_project({'name': 'Other', 'objective': 'another project'})['id']

        with self.assertRaisesRegex(ValueError, '不属于当前项目'):
            self.read(robot_id, other)
        with self.assertRaisesRegex(ValueError, '不属于当前项目'):
            self.limits.read({'robot_id': robot_id})

        self.assertEqual(self.device.calls, [])

    def test_a_wrong_token_is_the_devices_refusal(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped',
                                         'endpoint': self.device.url, 'token': secrets.token_hex(8)})

        with self.assertRaises(connector.DeviceError) as caught:
            self.read(robot['id'])

        self.assertEqual(caught.exception.status, 401)

    def test_an_answer_that_is_not_the_limits_view_is_not_shown_as_one(self):
        robot_id = self.robot()
        for broken in ({'schema': 'something.else.v9', 'required': True, 'filled': False},
                       {'schema': SCHEMA, 'required': 'yes', 'filled': False},
                       {'schema': SCHEMA, 'required': True}):
            self.device.view = lambda answer=broken: answer
            with self.assertRaisesRegex(ValueError, '无法识别'):
                self.read(robot_id)

    def test_only_the_known_parts_of_an_answer_are_passed_on(self):
        self.device.fill(0.5, 0.8, 0.5, '李工')
        whole = self.device.view()
        junk = {**whole, 'limits': {GROUP: {**whole['limits'][GROUP], 'extra': '<b>x</b>'}, 'arm': {'max_joint_speed': 1}},
                'set_by': 'x' * 900, 'set_at': 'yesterday', 'revision': 'three', 'internal': {'secret': 1},
                'invalid': ['x'], 'groups': [GROUP, 'arm', 7], 'history': [{'revision': 1, 'limits': None, 'set_by': 5, 'set_at': None}, 'x'],
                'bounds': {GROUP: {'max_linear_speed': {'unit': 'm/s', 'min': '0', 'max': 1.0}}, 'arm': {}}}
        self.device.view = lambda: junk
        robot_id = self.robot()

        view = self.read(robot_id)

        self.assertNotIn('internal', view)
        self.assertEqual(view['limits'], {GROUP: FILLED})
        self.assertEqual((len(view['set_by']), view['set_at'], view['revision']), (200, None, 0))
        self.assertEqual((view['groups'], view['invalid']), ([GROUP, 'arm'], None))
        self.assertEqual(view['history'], [{'revision': 1, 'limits': None, 'set_by': None, 'set_at': None}])
        self.assertEqual(view['bounds'], {GROUP: {'max_linear_speed': {
            'unit': 'm/s', 'min': None, 'min_exclusive': False, 'max': 1.0, 'platform_max': None, 'body_max': None}}})

    def test_the_connection_check_keeps_the_summary_for_the_card(self):
        robot_id = self.robot(connect=True)

        robot = next(r for r in self.platform.snapshot()['robots'] if r['id'] == robot_id)

        self.assertEqual(robot['device_status']['safety_limits']['motion_blocked'], True)

    def test_reading_brings_a_stale_card_up_to_date(self):
        robot_id = self.robot(connect=True)
        self.device.fill(0.5, 0.8, 0.5, '李工')          # filled in from somewhere else since the connection check

        self.read(robot_id)

        summary = self.stored_summary(robot_id)
        self.assertEqual((summary['filled'], summary['motion_blocked'], summary['set_by']), (True, False, '李工'))
        self.assertEqual(summary['limits'], {GROUP: FILLED})
        self.assertEqual(self.platform.store.get('robots', robot_id)['device_status']['instance_id'], 'limits-double')


class SaveTests(Case):
    def test_saving_fills_the_limits_and_records_the_signed_in_person(self):
        robot_id = self.robot(connect=True)

        view = self.save(robot_id, request_id='request-1')

        self.assertEqual((view['robot_id'], view['supported'], view['accepted']), (robot_id, True, True))
        self.assertEqual((view['filled'], view['motion_blocked'], view['set_by']), (True, False, '张工'))
        self.assertEqual(view['limits'], {GROUP: FILLED})
        self.assertEqual(self.device.posts, [{'limits': {GROUP: FILLED}, 'set_by': '张工', 'request_id': 'request-1'}])
        self.assertEqual((self.device.set_by, self.device.limits), ('张工', {GROUP: FILLED}))

    def test_who_filled_them_in_is_never_taken_from_the_request(self):
        robot_id = self.robot()

        self.limits.save({'robot_id': robot_id, 'project_id': self.pid, 'set_by': '冒名', 'user': {'name': '冒名'},
                          'limits': {GROUP: {**FILLED, 'set_by': '冒名'}, 'set_by': '冒名'}}, PERSON)

        self.assertEqual(self.device.posts, [{'limits': {GROUP: FILLED}, 'set_by': '张工'}])
        self.assertEqual(self.device.set_by, '张工')

    def test_an_account_without_a_name_is_recorded_by_its_email(self):
        robot_id = self.robot()

        self.save(robot_id, user={'id': 'user-2', 'name': '  ', 'email': 'li@example.test'})

        self.assertEqual(self.device.set_by, 'li@example.test')

    def test_nothing_is_sent_without_a_signed_in_person(self):
        robot_id = self.robot()

        for nobody in (None, {}, {'id': 'user-3', 'name': '', 'email': ''}):
            with self.assertRaisesRegex(ValueError, '登录'):
                self.limits.save({'robot_id': robot_id, 'project_id': self.pid, 'limits': {GROUP: FILLED}}, nobody)

        self.assertEqual(self.device.posts, [])

    def test_a_value_above_the_bodys_maximum_is_the_devices_refusal_passed_on(self):
        robot_id = self.robot(connect=True)

        with self.assertRaises(connector.DeviceError) as caught:
            self.save(robot_id, {**FILLED, 'max_linear_speed': 1.2})

        refusal = caught.exception
        self.assertEqual((refusal.code, refusal.status), ('limits_out_of_range', 400))
        self.assertEqual({k: refusal.body[k] for k in ('field', 'value', 'min', 'max', 'bound')},
                         {'field': 'max_linear_speed', 'value': 1.2, 'min': 0.0, 'max': 1.0, 'bound': 'body'})
        answer = error_payload(refusal)
        self.assertEqual({k: answer[k] for k in ('code', 'device_status', 'field', 'value', 'min', 'max')},
                         {'code': 'limits_out_of_range', 'device_status': 400, 'field': 'max_linear_speed',
                          'value': 1.2, 'min': 0.0, 'max': 1.0})
        # Nothing changed anywhere: the device, the card, the audit log.
        self.assertIsNone(self.device.limits)
        self.assertEqual(self.stored_summary(robot_id)['motion_blocked'], True)
        self.assertEqual(self.events('robot.limits_set'), [])

    def test_a_value_above_the_platforms_maximum_names_the_platform(self):
        robot_id = self.robot()

        with self.assertRaises(connector.DeviceError) as caught:
            self.save(robot_id, {**FILLED, 'command_timeout_s': 5})

        self.assertEqual((caught.exception.body['field'], caught.exception.body['bound'], caught.exception.body['max']),
                         ('command_timeout_s', 'platform', 2.0))

    def test_what_is_not_a_number_is_refused_before_the_device_is_asked(self):
        robot_id = self.robot()
        cases = [('max_linear_speed', '0.5'), ('max_angular_speed', None), ('command_timeout_s', float('nan')),
                 ('max_linear_speed', float('inf')), ('max_angular_speed', True), ('command_timeout_s', [0.5]),
                 ('max_linear_speed', 10 ** 400)]              # an int too large for a float
        for name, value in cases:
            with self.assertRaises(CodedError) as caught:
                self.save(robot_id, {**FILLED, name: value})
            self.assertEqual((caught.exception.code, caught.exception.extra), ('limits_invalid', {'field': name}))
            self.assertEqual(error_payload(caught.exception)['field'], name)
        missing = {k: v for k, v in FILLED.items() if k != 'max_angular_speed'}
        with self.assertRaises(CodedError) as caught:
            self.save(robot_id, missing)
        self.assertEqual(caught.exception.extra, {'field': 'max_angular_speed'})
        for shape in (None, [], {'arm': FILLED}, {GROUP: 3}):
            with self.assertRaises(CodedError) as caught:
                self.limits.save({'robot_id': robot_id, 'project_id': self.pid, 'limits': shape}, PERSON)
            self.assertEqual(caught.exception.code, 'limits_invalid')

        self.assertEqual(self.device.calls, [])

    def test_the_range_is_the_devices_decision(self):
        robot_id = self.robot()

        with self.assertRaises(connector.DeviceError) as caught:
            self.save(robot_id, {**FILLED, 'max_linear_speed': 0})      # the console does not judge values

        self.assertEqual((caught.exception.code, caught.exception.body['bound']), ('limits_out_of_range', 'platform'))
        self.assertEqual(len(self.device.posts), 1)

    def test_a_request_id_must_be_usable(self):
        robot_id = self.robot()

        for bad in ('', '   ', 'x' * 201, 7):
            with self.assertRaisesRegex(ValueError, '请求标识'):
                self.save(robot_id, request_id=bad)

        self.assertEqual(self.device.posts, [])

    def test_the_same_request_again_is_answered_by_the_device_not_written_twice(self):
        robot_id = self.robot()

        first = self.save(robot_id, request_id='request-7')
        again = self.save(robot_id, request_id='request-7')

        self.assertEqual((first['revision'], again['revision'], self.device.revision), (1, 1, 1))

    def test_another_value_under_a_used_request_id_is_the_devices_refusal(self):
        robot_id = self.robot()
        self.save(robot_id, request_id='request-8')

        with self.assertRaises(connector.DeviceError) as caught:
            self.save(robot_id, {**FILLED, 'max_linear_speed': 0.3}, request_id='request-8')

        self.assertEqual((caught.exception.code, caught.exception.status), ('request_id_reused', 409))
        self.assertEqual(self.device.limits, {GROUP: FILLED})

    def test_a_running_skill_is_the_devices_refusal(self):
        robot_id = self.robot()
        for flag, code in (('busy', 'runs_active'), ('conformance', 'conformance_running')):
            setattr(self.device, flag, True)
            with self.assertRaises(connector.DeviceError) as caught:
                self.save(robot_id)
            setattr(self.device, flag, False)
            self.assertEqual((caught.exception.code, caught.exception.status), (code, 409))
            self.assertEqual(error_payload(caught.exception)['code'], code)

    def test_a_write_is_never_sent_twice(self):
        robot_id = self.robot()
        self.device.fail_status = 503

        with self.assertRaises(connector.DeviceError) as caught:
            self.save(robot_id)

        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.device.posts), 1)
        self.assertEqual(self.events('robot.limits_set'), [])

    def test_an_older_device_cannot_take_limits(self):
        robot_id = self.robot(self.make_device(supported=False))

        with self.assertRaisesRegex(ValueError, '版本较旧') as caught:
            self.save(robot_id)

        self.assertNotIsInstance(caught.exception, connector.DeviceError)

    def test_an_answer_that_does_not_confirm_is_not_taken_as_saved(self):
        robot_id = self.robot(connect=True)
        self.device.apply = lambda values, who: {**self.device.view(), 'accepted': False}

        with self.assertRaisesRegex(ValueError, '没有确认'):
            self.save(robot_id)
        self.device.apply = lambda values, who: {**self.device.view(), 'accepted': True, 'schema': 'something.else.v9'}
        with self.assertRaisesRegex(ValueError, '无法识别'):
            self.save(robot_id)

        self.assertEqual(self.events('robot.limits_set'), [])
        self.assertEqual(self.stored_summary(robot_id)['motion_blocked'], True)

    def test_a_robot_of_another_project_is_refused_before_the_device_is_asked(self):
        robot_id = self.robot()
        other = self.platform.add_project({'name': 'Other', 'objective': 'another project'})['id']

        with self.assertRaisesRegex(ValueError, '不属于当前项目'):
            self.limits.save({'robot_id': robot_id, 'project_id': other, 'limits': {GROUP: FILLED}}, PERSON)

        self.assertEqual(self.device.calls, [])

    def test_a_save_is_written_to_the_audit_log(self):
        robot_id = self.robot()

        view = self.save(robot_id, request_id='request-9')

        [event] = self.events('robot.limits_set')
        self.assertEqual(event['project_id'], self.pid)
        self.assertEqual(event['details'], {'robot_id': robot_id, 'limits': {GROUP: FILLED}, 'set_by': '张工',
                                            'set_at': view['set_at'], 'revision': 1, 'request_id': 'request-9'})

    def test_a_save_updates_what_the_robot_card_shows(self):
        robot_id = self.robot(connect=True)
        self.assertEqual(self.stored_summary(robot_id)['motion_blocked'], True)

        view = self.save(robot_id)

        robot = next(r for r in self.platform.snapshot()['robots'] if r['id'] == robot_id)
        self.assertEqual(robot['device_status']['safety_limits'],
                         {'required': True, 'filled': True, 'motion_blocked': False, 'reason': None,
                          'limits': {GROUP: FILLED}, 'set_by': '张工', 'set_at': view['set_at']})
        self.assertEqual(robot['device_status']['instance_id'], 'limits-double')       # the rest of the status stays
        self.assertEqual(robot['status'], 'connected')

    def test_a_robot_that_was_never_checked_gets_the_summary_too(self):
        robot_id = self.robot()

        self.save(robot_id)

        self.assertEqual(self.stored_summary(robot_id)['filled'], True)
        self.assertEqual(self.platform.store.get('robots', robot_id)['status'], 'unconnected')


class HttpTests(unittest.TestCase):
    """The two routes of the console, with a real session cookie."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.tmp.name), '')
        cls.httpd = FastBindHTTPServer(('127.0.0.1', 0), make_handler(cls.app))
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.host = f'127.0.0.1:{cls.httpd.server_port}'
        cls.device = LimitsDevice()
        login = 'A1' + secrets.token_hex(8)                      # made up for this run
        account = {'email': f'limits-{secrets.token_hex(4)}@example.test', 'password': login, 'name': '王工'}
        session, cls.user, _ = cls.app.identity.sign_in(account, register=True)
        cls.cookie = 'brain_session=' + session
        workspace = cls.app.workspace(cls.user)
        cls.pid = workspace.add_project({'name': 'Limits', 'objective': 'over HTTP'})['id']
        cls.robot_id = workspace.add_robot({'project_id': cls.pid, 'name': 'Dog', 'family': 'quadruped',
                                            'endpoint': cls.device.url, 'token': cls.device.token})['id']

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.close()
        cls.device.close()
        cls.tmp.cleanup()

    def call(self, path, payload=None, cookie=None):
        headers = {'Content-Type': 'application/json', 'Host': self.host, 'Cookie': self.cookie if cookie is None else cookie}
        request = urllib.request.Request(f'http://{self.host}{path}', headers=headers,
                                         data=None if payload is None else json.dumps(payload).encode())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.headers.get('Content-Type'), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get('Content-Type'), exc.read()

    def api(self, path, payload, cookie=None):
        status, _, body = self.call('/api/' + path, payload, cookie)
        return status, json.loads(body)

    def test_1_both_routes_need_a_signed_in_session(self):
        for path in ('robots/limits', 'robots/limits/save'):
            status, body = self.api(path, {'robot_id': self.robot_id, 'project_id': self.pid}, cookie='')
            self.assertEqual((status, body.get('code')), (401, 'unauthorized'), path)
        self.assertEqual(self.device.calls, [])

    def test_2_reading_over_http(self):
        status, body = self.api('robots/limits', {'robot_id': self.robot_id, 'project_id': self.pid})

        self.assertEqual(status, 200)
        self.assertEqual((body['supported'], body['motion_blocked'], body['robot_id']), (True, True, self.robot_id))

    def test_3_a_refusal_reaches_the_browser_with_its_field(self):
        status, body = self.api('robots/limits/save', {'robot_id': self.robot_id, 'project_id': self.pid,
                                                       'limits': {GROUP: {**FILLED, 'max_angular_speed': 9}}})

        self.assertEqual(status, 400)
        self.assertEqual({k: body[k] for k in ('code', 'device_status', 'field', 'value', 'min', 'max')},
                         {'code': 'limits_out_of_range', 'device_status': 400, 'field': 'max_angular_speed',
                          'value': 9, 'min': 0.0, 'max': 1.5})
        self.assertRegex(body['error'], '[一-鿿]')
        status, body = self.api('robots/limits/save', {'robot_id': self.robot_id, 'project_id': self.pid,
                                                       'limits': {GROUP: {**FILLED, 'max_linear_speed': 'fast'}}})
        self.assertEqual((status, body['code'], body['field']), (400, 'limits_invalid', 'max_linear_speed'))
        # Whatever the request looks like, the answer is a readable refusal, never an internal error.
        huge = json.loads('{"max_linear_speed": 1' + '0' * 400 + ', "max_angular_speed": 0.8, "command_timeout_s": 0.5}')
        for limits in ({GROUP: huge}, 'fast', [FILLED], {GROUP: None}, None):
            status, body = self.api('robots/limits/save', {'robot_id': self.robot_id, 'project_id': self.pid, 'limits': limits})
            self.assertEqual((status, body['code']), (400, 'limits_invalid'), limits if not isinstance(limits, dict) else 'huge')
        status, body = self.api('robots/limits/save', {'robot_id': self.robot_id, 'project_id': self.pid,
                                                       'limits': {GROUP: FILLED}, 'request_id': {'id': 1}})
        self.assertEqual(status, 400)
        self.assertRegex(body['error'], '请求标识')
        self.assertEqual(self.device.posts[1:], [])              # of all of these only the out-of-range one reached the device

    def test_4_saving_over_http_records_the_account_not_what_the_request_claims(self):
        status, body = self.api('robots/limits/save', {'robot_id': self.robot_id, 'project_id': self.pid,
                                                       'limits': {GROUP: FILLED}, 'set_by': '冒名',
                                                       'request_id': 'http-request-1'})

        self.assertEqual(status, 200)
        self.assertEqual((body['accepted'], body['set_by'], body['motion_blocked']), (True, '王工', False))
        self.assertEqual(self.device.set_by, '王工')
        _, state = self.api('robots/limits', {'robot_id': self.robot_id, 'project_id': self.pid})
        self.assertEqual(state['history'][0]['set_by'], '王工')

    def test_5_the_page_files_are_served_and_loaded_last(self):
        for path, mime in (('/safety_limits.js', 'text/javascript'), ('/safety_limits.css', 'text/css')):
            status, content_type, body = self.call(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(content_type.startswith(mime), content_type)
            self.assertEqual(body, (ROOT / 'web' / path[1:]).read_bytes())
        index = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        line = '<link rel="stylesheet" href="/safety_limits.css"><script src="/safety_limits.js" defer></script>'
        self.assertIn(line, index)
        self.assertGreater(index.index(line), index.index('/robot_remove.js'))


class PageTests(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/safety_limits_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'safety_limits logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
