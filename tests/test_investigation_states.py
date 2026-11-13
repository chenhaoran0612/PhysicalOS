"""Issue #6: the engineering assistant's investigation of a robot has three states.

1. not started  -> the start form;
2. running      -> progress (hardware scan / basic probes / deeper reads / report);
3. finished     -> the report.

An investigation can be run again from scratch ("重新调研"): a fresh hardware scan,
a new investigation that does not reuse the old reports, and a new report version.
The report hands driver and network problems to a fix chat: a conversation with
the assistant (a repair session from issue #2) in which every command that changes
the device is approved by the user.

Runs on the local SSH and Responses fixtures; the Codex stand-in replaces the
runtime when no pinned binary is available. No paid API and no real robot.
"""
import contextlib
import json
import re
import secrets
import tempfile
import threading
import time
import unittest
from unittest import mock

import agent_binary  # noqa: F401  (optional NEUXMIND_TEST_AGENT_BINARY)
from brainos import agent_runtime, hardware_init, hardware_scan, investigation
from brainos.engineering import ACTIVE
from brainos.service import Platform
from codex_standin import CodexStandIn
from hardware_fixture import jetson
from ssh_fixture import SSHFixture
from test_engineering import ResponsesFixture
from test_product import Server

SCAN = hardware_scan.SCAN_EXEC
# Throwaway credentials for the local fixtures, generated per run.
SSH_LOGIN = secrets.token_hex(12)
MODEL_KEY = secrets.token_hex(12)
SERIAL = 'serial-perm:/dev/ttyUSB0'
USERMOD = hardware_init._quote_root('usermod -aG dialout robot', 'sudo_password', 30)
REAL_AVAILABLE = agent_runtime.available


def engine_available():
    """Starting a session needs the engine; unit tests replace the launch, so pretend it is installed."""
    return mock.patch.object(agent_runtime, 'available', lambda: {**REAL_AVAILABLE(), 'available': True})


def runtime():
    """The pinned runtime when present, else the deterministic stand-in."""
    if agent_runtime.binary_path():
        return contextlib.nullcontext()
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(agent_runtime, 'AgentProcess', CodexStandIn))
    stack.enter_context(engine_available())
    return stack


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Platform(self.tmp.name)
        self.e, self.hw, self.inv, self.store = self.p.engineering, self.p.hardware, self.p.investigation, self.p.store
        self.pid = self.p.add_project({'name': 'Investigation', 'objective': 'Know the robot'})['id']
        self.ssh = SSHFixture(command_outputs={SCAN: jetson().encode()}, password=SSH_LOGIN)
        scan = self.e.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': self.ssh.port, 'username': 'robot'})
        self.connection = self.e.connect({'project_id': self.pid, 'host_check_id': scan['id'], 'trust_fingerprint': scan['fingerprint'],
                                          'auth': 'password', 'password': SSH_LOGIN, 'name': 'Orin robot'})
        self.robot = self.connection['robot_id']
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': 'http://127.0.0.1:9/v1', 'api_key': MODEL_KEY})

    def tearDown(self):
        self.p.close()
        self.ssh.close()
        self.tmp.cleanup()

    def session(self, sid, mode='discovery', status='completed', created='2026-11-12T10:00:00', **extra):
        s = {'id': sid, 'project_id': self.pid, 'robot_id': self.robot, 'connection_id': self.connection['id'], 'mode': mode,
             'status': status, 'events': [], 'created_at': created, 'updated_at': created, 'name': 'fixture', **extra}
        self.store.put('agent_sessions', s)
        return s

    def report(self, iid, sid, created='2026-11-12T10:05:00'):
        self.store.put('robot_inventories', {'id': iid, 'project_id': self.pid, 'robot_id': self.robot, 'session_id': sid,
                                             'summary': '档案', 'components': [], 'unknowns': [], 'recommendations': [], 'created_at': created})
        robot = self.store.get('robots', self.robot)
        robot['inventory_id'] = iid
        self.store.put('robots', robot)
        s = self.store.get('agent_sessions', sid)
        s['inventory_id'] = iid
        self.store.put('agent_sessions', s)

    def state(self):
        return next(x for x in self.p.snapshot()['investigations'] if x['robot_id'] == self.robot)

    def phases(self, state):
        return [(p['id'], p['state']) for p in state['progress']['phases']]

    def scan(self):
        return self.hw.scan({'project_id': self.pid, 'robot_id': self.robot})


class StateTests(Base):
    def test_a_robot_never_investigated_is_not_started(self):
        state = self.state()
        self.assertEqual(state['state'], 'not_started')
        self.assertEqual(state['project_id'], self.pid)
        self.assertIsNone(state['session_id'])
        self.assertIsNone(state['inventory_id'])
        self.assertIsNone(state['progress'])

    def test_robots_without_ssh_have_no_investigation_state(self):
        other = self.p.add_robot({'project_id': self.pid, 'name': 'HTTP robot', 'family': 'custom'})
        self.assertNotIn(other['id'], [x['robot_id'] for x in self.p.snapshot()['investigations']])

    def test_a_running_investigation_shows_its_progress(self):
        started = [{'id': 'event-1', 'kind': 'started', 'message': '工程助手开始任务', 'at': '2026-11-12T10:00:01'}]
        self.session('agent-session-a', status='running', events=started, coverage={'checked': 3, 'total': 9, 'evidence_count': 3, 'probes': []})
        state = self.state()
        self.assertEqual(state['state'], 'running')
        self.assertEqual(state['session_id'], 'agent-session-a')
        self.assertEqual(self.phases(state), [('connect', 'done'), ('probes', 'active'), ('deep', 'pending'), ('report', 'pending')])
        self.assertEqual(state['progress']['phases'][1]['detail'], '3 / 9')
        self.assertIsNone(state['progress']['waiting'])

    def test_reads_beyond_the_basic_probes_move_progress_to_the_deeper_investigation(self):
        started = [{'id': 'event-1', 'kind': 'started', 'message': '工程助手开始任务', 'at': '2026-11-12T10:00:01'}]
        self.session('agent-session-a', status='waiting_input', events=started, pending_approval={'id': 'x', 'method': 'device_exec/requestApproval'},
                     coverage={'checked': 4, 'total': 9, 'evidence_count': 7, 'probes': []})
        state = self.state()
        self.assertEqual(self.phases(state), [('connect', 'done'), ('probes', 'done'), ('deep', 'active'), ('report', 'pending')])
        self.assertEqual(state['progress']['phases'][2]['detail'], '3 条深入证据')
        self.assertEqual(state['progress']['waiting'], 'approval')

    def test_a_saved_report_finishes_the_investigation(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        state = self.state()
        self.assertEqual(state['state'], 'finished')
        self.assertEqual(state['inventory_id'], 'inventory-a')
        self.assertIsNone(state['session_id'])
        self.assertIsNone(state['last_run'])

    def test_a_run_that_ended_without_a_report_is_unfinished(self):
        for status, phase in (('failed', 'failed'), ('interrupted', 'stopped'), ('needs_followup', 'incomplete')):
            with self.subTest(status=status):
                self.session('agent-session-a', status=status, error='模型调用失败' if status == 'failed' else None,
                             events=[{'id': 'event-1', 'kind': 'started', 'message': '开始', 'at': '2026-11-12T10:00:01'}],
                             coverage={'checked': 9, 'total': 9, 'evidence_count': 9, 'probes': []})
                state = self.state()
                self.assertEqual(state['state'], 'unfinished')
                self.assertEqual(state['last_session_id'], 'agent-session-a')
                self.assertEqual(self.phases(state), [('connect', 'done'), ('probes', 'done'), ('deep', phase), ('report', 'pending')])

    def test_a_rerun_in_progress_is_running_and_keeps_the_old_report(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        self.session('agent-session-b', status='starting', created='2026-11-12T11:00:00', investigation={'full_flow': True, 'rerun_of': 'agent-session-a'})
        state = self.state()
        self.assertEqual((state['state'], state['session_id'], state['inventory_id']), ('running', 'agent-session-b', 'inventory-a'))
        self.assertEqual(state['progress']['phases'][0]['id'], 'scan')

    def test_a_rerun_that_ended_without_a_report_keeps_the_old_report_and_says_so(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        self.session('agent-session-b', status='failed', error='模型调用失败', created='2026-11-12T11:00:00')
        state = self.state()
        self.assertEqual((state['state'], state['inventory_id']), ('finished', 'inventory-a'))
        self.assertEqual(state['last_run'], {'session_id': 'agent-session-b', 'status': 'failed', 'error': '模型调用失败'})

    def test_a_fix_chat_leaves_the_state_alone_but_marks_the_robot_busy(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        self.session('agent-session-fix', mode='repair', status='running', created='2026-11-12T11:00:00', hardware={'chat': True})
        state = self.state()
        self.assertEqual(state['state'], 'finished')
        self.assertEqual(state['busy_session_id'], 'agent-session-fix')
        self.assertEqual(state['fix_session_ids'], ['agent-session-fix'])

    def test_a_full_run_starts_with_the_hardware_scan(self):
        self.session('agent-session-a', status='running', investigation={'full_flow': True, 'scan_id': None, 'scan_error': None})
        self.assertEqual(self.phases(self.state())[0], ('scan', 'active'))
        record = self.scan()
        s = self.store.get('agent_sessions', 'agent-session-a')
        s['investigation']['scan_id'] = record['id']
        self.store.put('agent_sessions', s)
        first = self.state()['progress']['phases'][0]
        self.assertEqual(first['state'], 'done')
        self.assertIn(f"{len(record['issues'])} 个问题", first['detail'])

    def test_a_failed_scan_is_shown_and_the_run_goes_on(self):
        self.session('agent-session-a', status='running', investigation={'full_flow': True, 'scan_id': None, 'scan_error': '无法连接机器人'})
        first = self.state()['progress']['phases'][0]
        self.assertEqual((first['state'], first['detail']), ('skipped', '无法连接机器人'))


class RestartTests(Base):
    def restart(self, **extra):
        return self.inv.restart({'project_id': self.pid, 'robot_id': self.robot, 'model_id': self.model['id'], 'data_authorized': True, **extra})

    def test_restart_starts_a_fresh_full_investigation(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            s = self.restart()
        self.assertEqual(s['mode'], 'discovery')
        self.assertEqual(s['investigation'], {'full_flow': True, 'rerun_of': 'agent-session-a', 'scan_id': None, 'scan_error': None})
        self.assertTrue(s['name'].endswith('重新调研'))
        self.assertIn('重新', s['objective'])
        self.assertIsNone(s['max_tokens'], 'issue #5: investigations have no Token limit')
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1:], (s['objective'], False))
        self.assertEqual(self.state()['state'], 'running')
        events = [e for e in self.store.list('events') if e['action'] == 'device.investigation_restarted']
        self.assertEqual(events[0]['details']['session_id'], s['id'])

    def test_restart_keeps_the_users_objective(self):
        with engine_available(), mock.patch.object(self.e, '_launch'):
            s = self.restart(objective='重点核对相机与 CAN 总线')
        self.assertEqual(s['objective'], '重点核对相机与 CAN 总线')
        self.assertIsNone(s['investigation']['rerun_of'], 'nothing to re-run: the first investigation')

    def test_restart_is_refused_while_anything_runs_on_the_robot(self):
        for mode in ('discovery', 'repair'):
            with self.subTest(mode=mode):
                self.session('agent-session-busy', mode=mode, status='running')
                with engine_available(), mock.patch.object(self.e, '_launch') as launch, self.assertRaisesRegex(ValueError, '正在执行'):
                    self.restart()
                launch.assert_not_called()

    def test_restart_needs_consent_and_an_ssh_robot_of_the_project(self):
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            with self.assertRaisesRegex(ValueError, '授权'):
                self.restart(data_authorized=False)
            other = self.p.add_project({'name': 'Other', 'objective': 'x'})['id']
            with self.assertRaisesRegex(ValueError, '不属于当前项目'):
                self.inv.restart({'project_id': other, 'robot_id': self.robot, 'data_authorized': True})
            plain = self.p.add_robot({'project_id': self.pid, 'name': 'HTTP robot', 'family': 'custom'})
            with self.assertRaisesRegex(ValueError, 'SSH'):
                self.restart(robot_id=plain['id'])
        launch.assert_not_called()

    def test_investigation_options_are_checked(self):
        self.session('agent-session-fix', mode='repair')
        bad = ['yes', {'full_flow': 'yes'}, {'full_flow': True, 'rerun_of': 'agent-session-unknown'},
               {'full_flow': True, 'rerun_of': 'agent-session-fix'}]
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            for options in bad:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.e.start({'robot_id': self.robot, 'model_id': self.model['id'], 'data_authorized': True, 'investigation': options})
        launch.assert_not_called()
        self.assertFalse([s for s in self.store.list('agent_sessions') if s['mode'] == 'discovery'], 'nothing was stored')

    def test_a_plain_start_is_unchanged(self):
        with engine_available(), mock.patch.object(self.e, '_launch'):
            s = self.e.start({'robot_id': self.robot, 'model_id': self.model['id'], 'data_authorized': True})
        self.assertNotIn('investigation', s)
        self.assertTrue(s['name'].endswith('本体调查'))


class FlowScanTests(Base):
    def full_session(self):
        return self.session('agent-session-a', status='starting', investigation={'full_flow': True, 'rerun_of': None, 'scan_id': None, 'scan_error': None})

    def test_a_full_run_scans_the_hardware_once_and_records_it(self):
        self.full_session()
        handle = {'stop': threading.Event()}
        self.inv.flow_scan('agent-session-a', handle)
        s = self.store.get('agent_sessions', 'agent-session-a')
        scan = self.store.get('hardware_scans', s['investigation']['scan_id'])
        self.assertEqual((scan['source'], scan['session_id'], scan['robot_id']), ('investigation', 'agent-session-a', self.robot))
        self.assertEqual(self.ssh.commands.count(SCAN), 1)
        self.assertEqual([e['kind'] for e in s['events']], ['hardware_scan'])
        self.assertIn('个问题', s['events'][0]['message'])
        self.inv.flow_scan('agent-session-a', handle)
        self.assertEqual(self.ssh.commands.count(SCAN), 1, 'a resumed run does not scan again')

    def test_a_failed_scan_does_not_stop_the_investigation(self):
        self.full_session()
        with mock.patch.object(self.hw, '_execute_scan', side_effect=ValueError('无法连接机器人完成硬件扫描：timeout')):
            self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        s = self.store.get('agent_sessions', 'agent-session-a')
        self.assertIsNone(s['investigation']['scan_id'])
        self.assertIn('无法连接机器人', s['investigation']['scan_error'])
        self.assertIn('硬件扫描未完成', s['events'][-1]['message'])

    def test_a_stopped_run_skips_the_scan(self):
        self.full_session()
        handle = {'stop': threading.Event()}
        handle['stop'].set()
        self.inv.flow_scan('agent-session-a', handle)
        self.assertNotIn(SCAN, self.ssh.commands)
        self.assertIsNone(self.store.get('agent_sessions', 'agent-session-a')['investigation']['scan_id'])

    def test_an_error_after_the_scan_does_not_stop_the_investigation(self):
        self.full_session()
        with mock.patch.object(self.hw, '_save', side_effect=ValueError('磁盘已满')):
            self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        s = self.store.get('agent_sessions', 'agent-session-a')
        self.assertIsNone(s['investigation']['scan_id'])
        self.assertIn('磁盘已满', s['investigation']['scan_error'])
        self.assertNotIn(self.robot, self.hw.scanning)

    def test_a_stop_during_the_scan_is_not_a_scan_failure(self):
        self.full_session()
        handle = {'stop': threading.Event()}

        def stopped(*args):
            handle['stop'].set()
            raise ValueError('SSH 会话异常，硬件扫描未完成：channel closed')
        with mock.patch.object(self.hw, '_execute_scan', side_effect=stopped):
            self.inv.flow_scan('agent-session-a', handle)
        s = self.store.get('agent_sessions', 'agent-session-a')
        self.assertEqual((s['investigation']['scan_id'], s['investigation']['scan_error']), (None, None), 'a resume scans again')
        self.assertEqual(s['events'], [])

    def test_a_scan_already_running_is_waited_for(self):
        self.full_session()
        self.hw.scanning.add(self.robot)
        threading.Timer(.3, lambda: self.hw.scanning.discard(self.robot)).start()
        self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        self.assertIsNotNone(self.store.get('agent_sessions', 'agent-session-a')['investigation']['scan_id'])
        self.assertEqual(self.ssh.commands.count(SCAN), 1)

    def test_a_scan_that_never_ends_elsewhere_is_given_up(self):
        self.full_session()
        self.hw.scanning.add(self.robot)
        with mock.patch.object(investigation, 'SCAN_WAIT_SECONDS', .2):
            self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        self.assertIn('正在进行', self.store.get('agent_sessions', 'agent-session-a')['investigation']['scan_error'])
        self.assertNotIn(SCAN, self.ssh.commands)
        self.assertIn(self.robot, self.hw.scanning, 'the other scan still owns the robot')

    def test_the_scan_of_a_run_is_kept_when_old_scans_are_pruned(self):
        self.full_session()
        self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        kept = self.store.get('agent_sessions', 'agent-session-a')['investigation']['scan_id']
        with mock.patch.object(hardware_init, 'KEEP_SCANS', 1):
            self.scan()
            self.scan()
        self.store.get('hardware_scans', kept)
        s = self.store.get('agent_sessions', 'agent-session-a')
        s['investigation']['scan_id'] = 'hardware-scan-gone'
        context = self.inv.device_context(s, {'inventories': []})
        self.assertNotIn('hardware_scan', context, 'a missing scan record is left out, not an error')

    def test_plain_investigations_do_not_scan(self):
        self.session('agent-session-a', status='starting')
        self.inv.flow_scan('agent-session-a', {'stop': threading.Event()})
        self.assertNotIn(SCAN, self.ssh.commands)

    def test_a_fresh_run_reads_no_old_report(self):
        record = self.scan()
        s = self.session('agent-session-b', status='running', investigation={'full_flow': True, 'scan_id': record['id'], 'scan_error': None})
        context = {'robot': {'id': self.robot}, 'inventories': [{'id': 'inventory-a', 'summary': '旧档案'}], 'evidence': []}
        fresh = self.inv.device_context(s, context)
        self.assertEqual(fresh['inventories'], [])
        self.assertEqual(fresh['previous_inventory_ids'], ['inventory-a'])
        self.assertEqual(fresh['hardware_scan']['scan_id'], record['id'])
        self.assertEqual({i['id'] for i in fresh['hardware_scan']['issues']}, {i['id'] for i in record['issues']})
        self.assertEqual(context['inventories'][0]['id'], 'inventory-a', 'the given context is not changed')
        plain = self.session('agent-session-c', status='running')
        self.assertEqual(self.inv.device_context(plain, context), context)


class FixChatTests(Base):
    def start(self, **extra):
        p = {'project_id': self.pid, 'robot_id': self.robot, 'model_id': self.model['id'], 'issue_ids': [SERIAL],
             'message': '底盘串口打不开，帮我修好', 'data_authorized': True, 'modify_authorized': True, **extra}
        return self.inv.start_fix(p)

    def test_a_fix_chat_needs_a_hardware_scan(self):
        with engine_available(), mock.patch.object(self.e, '_launch') as launch, self.assertRaisesRegex(ValueError, '扫描硬件'):
            self.start()
        launch.assert_not_called()

    def test_a_fix_chat_from_the_report(self):
        self.scan()
        latest = self.scan()
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            s = self.start()
        self.assertEqual(s['mode'], 'repair')
        hw = s['hardware']
        self.assertTrue(hw['chat'])
        self.assertEqual((hw['scan_id'], hw['issue_ids'], hw['extra_issue_ids']), (latest['id'], [SERIAL], [investigation.USER_REQUEST]))
        self.assertTrue(s['name'].endswith('对话修复'))
        self.assertIsNone(s['max_tokens'])
        self.assertEqual([(e['kind'], e['message']) for e in s['events']], [('user', '底盘串口打不开，帮我修好')])
        self.assertIn('底盘串口打不开，帮我修好', s['objective'])
        self.assertIn(SERIAL, s['objective'])
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1:], (s['objective'], False))

    def test_credentials_typed_into_the_chat_do_not_reach_the_model(self):
        self.scan()
        leak = secrets.token_hex(8)
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            s = self.start(message=f'串口打不开\npassword: {leak}')
        self.assertNotIn(leak, s['objective'])
        self.assertNotIn(leak, launch.call_args.args[1])
        self.assertNotIn(leak, json.dumps(self.store.get('agent_sessions', s['id']), ensure_ascii=False))
        s.update(status='completed', thread_id='thread-fixture')
        self.store.put('agent_sessions', s)
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            self.inv.fix_message({'project_id': self.pid, 'session_id': s['id'], 'message': f'再试一次\napi_key = {leak}'})
        self.assertNotIn(leak, launch.call_args.args[1])
        self.assertNotIn(leak, json.dumps(self.store.get('agent_sessions', s['id']), ensure_ascii=False))

    def test_the_first_message_names_the_conversation(self):
        self.scan()
        with engine_available(), mock.patch.object(self.e, '_launch'):
            s = self.start()
        self.assertEqual(s['chat']['first_message'], '底盘串口打不开，帮我修好')

    def test_a_request_alone_or_issues_alone_is_enough(self):
        self.scan()
        with engine_available(), mock.patch.object(self.e, '_launch'):
            s = self.start(issue_ids=[])
            self.assertEqual(s['hardware']['issue_ids'], [])
            s['status'] = 'completed'
            self.store.put('agent_sessions', s)
            s = self.start(message='')
        self.assertEqual(s['hardware']['issue_ids'], [SERIAL])
        self.assertEqual(s['events'][0]['kind'], 'user')
        self.assertIn(SERIAL, s['events'][0]['message'])

    def test_fix_chat_input_checks(self):
        self.scan()
        bad = [({'issue_ids': [], 'message': ''}, '描述'), ({'issue_ids': ['net-dns:unknown']}, '扫描'), ({'issue_ids': [SERIAL, SERIAL]}, '扫描'),
               ({'issue_ids': 'all'}, '扫描'), ({'modify_authorized': False}, '修改设备'), ({'data_authorized': False}, '授权'),
               ({'message': 'x' * 4001}, '修复要求')]
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            for extra, message in bad:
                with self.subTest(extra=str(extra)[:60]), self.assertRaisesRegex(ValueError, message):
                    self.start(**extra)
        launch.assert_not_called()

    def test_a_fix_chat_waits_for_other_work_on_the_robot(self):
        self.scan()
        self.session('agent-session-busy', status='running')
        with engine_available(), mock.patch.object(self.e, '_launch') as launch, self.assertRaisesRegex(ValueError, '正在执行'):
            self.start()
        launch.assert_not_called()

    def chat(self, status='completed'):
        self.scan()
        with engine_available(), mock.patch.object(self.e, '_launch'):
            s = self.start()
        s.update(status=status, thread_id='thread-fixture')
        self.store.put('agent_sessions', s)
        return s

    def test_messages_continue_the_same_conversation(self):
        s = self.chat()
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            result = self.inv.fix_message({'project_id': self.pid, 'session_id': s['id'], 'message': '再看看 eth1 为什么没有网'})
        self.assertEqual(result['id'], s['id'])
        users = [e['message'] for e in self.store.get('agent_sessions', s['id'])['events'] if e['kind'] == 'user']
        self.assertEqual(users, ['底盘串口打不开，帮我修好', '再看看 eth1 为什么没有网'])
        self.assertEqual(launch.call_args.args[1:], ('再看看 eth1 为什么没有网', True))

    def test_messages_are_refused_while_the_assistant_works_or_outside_a_chat(self):
        busy = self.chat(status='running')
        repair = self.session('agent-session-repair', mode='repair', hardware={'scan_id': None, 'issue_ids': []})
        discovery = self.session('agent-session-discovery')
        cases = [(busy['id'], '还在处理'), (repair['id'], '对话修复'), (discovery['id'], '对话修复')]
        with engine_available(), mock.patch.object(self.e, '_launch') as launch:
            for sid, message in cases:
                with self.subTest(sid=sid), self.assertRaisesRegex(ValueError, message):
                    self.inv.fix_message({'project_id': self.pid, 'session_id': sid, 'message': '继续'})
            busy.update(status='completed')
            self.store.put('agent_sessions', busy)
            with self.assertRaisesRegex(ValueError, '消息'):
                self.inv.fix_message({'project_id': self.pid, 'session_id': busy['id'], 'message': '   '})
            other = self.p.add_project({'name': 'Other', 'objective': 'x'})['id']
            with self.assertRaisesRegex(ValueError, '不属于当前项目'):
                self.inv.fix_message({'project_id': other, 'session_id': busy['id'], 'message': '继续'})
        launch.assert_not_called()

    def test_a_refused_message_leaves_no_trace(self):
        s = self.chat()
        self.e.closing = True
        try:
            with engine_available(), mock.patch.object(self.e, '_launch') as launch, self.assertRaises(ValueError):
                self.inv.fix_message({'project_id': self.pid, 'session_id': s['id'], 'message': '继续修'})
        finally:
            self.e.closing = False
        launch.assert_not_called()
        users = [e['message'] for e in self.store.get('agent_sessions', s['id'])['events'] if e['kind'] == 'user']
        self.assertEqual(users, ['底盘串口打不开，帮我修好'])


class ChatRuleTests(Base):
    def repair(self, chat):
        record = self.scan()
        hardware = {'scan_id': record['id'], 'issue_ids': [], 'skill': hardware_init.skill_info(), 'fixes': 0, 'fixed_issue_ids': [],
                    'rescan_id': None, 'rescan_after_fixes': None, 'ssh_interface': 'eth0'}
        if chat:
            hardware.update(chat=True, extra_issue_ids=[investigation.USER_REQUEST])
        return self.session('agent-session-r', mode='repair', status='running', hardware=hardware)

    def fix(self, issue_ids):
        return {'command': 'usermod -aG dialout robot', 'purpose': '让 SSH 用户可以读写串口', 'issue_ids': issue_ids, 'risk': 'low',
                'rollback': 'gpasswd -d robot dialout', 'needs_root': True, 'timeout_seconds': 30}

    def test_the_users_own_request_can_be_fixed_only_in_a_chat(self):
        s = self.repair(chat=True)
        request = self.hw.fix_request(s, self.fix([investigation.USER_REQUEST]), self.connection)
        self.assertEqual(request['issue_ids'], [investigation.USER_REQUEST])
        with self.assertRaisesRegex(ValueError, '不允许重启'):
            self.hw.fix_request(s, {**self.fix([investigation.USER_REQUEST]), 'command': 'reboot'}, self.connection)
        plain = self.repair(chat=False)
        with self.assertRaisesRegex(ValueError, 'issue_ids'):
            self.hw.fix_request(plain, self.fix([investigation.USER_REQUEST]), self.connection)

    def test_a_chat_report_can_cover_the_users_request(self):
        s = self.repair(chat=True)
        result = self.hw.tool(s['id'], s, 'save_repair_report', {
            'summary': '串口需要现场确认接线', 'followups': [],
            'results': [{'issue_id': investigation.USER_REQUEST, 'outcome': 'needs_user', 'actions': '检查了串口节点权限',
                         'verification': '需要现场确认', 'evidence_ids': []}]}, None)
        report = self.store.get('hardware_repairs', result['repair_id'])
        self.assertEqual(report['results'][0]['title'], '用户在对话中提出的修复要求')
        with self.assertRaisesRegex(ValueError, '最后一次修复之后'):
            self.hw.tool(s['id'], s, 'save_repair_report', {'summary': 'x', 'followups': [], 'results': [
                {'issue_id': investigation.USER_REQUEST, 'outcome': 'fixed', 'actions': 'x', 'verification': 'x', 'evidence_ids': []}]}, None)

    def test_a_chat_turn_ends_waiting_for_the_user(self):
        s = self.repair(chat=True)
        self.assertEqual(self.hw.finish(s['id']), 'completed')
        self.assertNotIn('incomplete', [e['kind'] for e in self.store.get('agent_sessions', s['id'])['events']])
        self.repair(chat=False)
        self.assertEqual(self.hw.finish('agent-session-r'), 'needs_followup')

    def test_chat_sessions_are_told_they_are_a_conversation(self):
        chat = self.repair(chat=True)
        text = hardware_init.instructions(chat, 'default')
        self.assertTrue(text.startswith(hardware_init.REPAIR_INSTRUCTIONS))
        self.assertIn('对话', text[len(hardware_init.REPAIR_INSTRUCTIONS):])
        self.assertIn(investigation.USER_REQUEST, text)
        self.assertEqual(hardware_init.instructions(self.repair(chat=False), 'default'), hardware_init.REPAIR_INSTRUCTIONS)


class RerunModel(ResponsesFixture):
    """Scripted model for a re-investigation: copy the context it was given, probe, save a report."""
    requests = []

    def select_item(self, data, fallback):
        step = len(type(self).requests) - 1
        evidence = list(dict.fromkeys(re.findall(r'evidence-[a-f0-9]{12}', json.dumps(data))))
        calls = [('exec_command', {'cmd': 'cp DEVICE_CONTEXT.json context-copy.json'}),
                 ('device_probe', {'probe': 'system'}),
                 ('save_inventory', {'summary': '重新调研：Jetson 机载电脑', 'recommendations': ['给 SSH 用户串口权限'], 'unknowns': [],
                                     'components': [{'category': 'system', 'name': 'Linux', 'details': '重新读取', 'evidence_ids': evidence[-1:]}]})]
        if step >= len(calls):
            return {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': '已重新调研并保存档案。', 'annotations': []}]}
        name, args = calls[step]
        return {'id': f'fc_{step}', 'type': 'function_call', 'call_id': f'call_{step}', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}


class ChatModel(ResponsesFixture):
    """Scripted model for a fix chat: one approved command in the first turn, a plain answer in the second."""
    requests = []

    def select_item(self, data, fallback):
        step = len(type(self).requests) - 1
        answers = {1: '已把 robot 加入 dialout 组，重新登录后生效。', 2: 'eth1 没有链路信号，需要现场检查网线。'}
        if step == 0:
            fix = {'command': 'usermod -aG dialout robot', 'purpose': '让 SSH 用户可以读写底盘串口', 'issue_ids': [investigation.USER_REQUEST],
                   'risk': 'low', 'rollback': 'gpasswd -d robot dialout', 'needs_root': True, 'timeout_seconds': 30}
            return {'id': 'fc_0', 'type': 'function_call', 'call_id': 'call_0', 'name': 'device_fix', 'arguments': json.dumps(fix, ensure_ascii=False)}
        return {'id': f'msg_{step}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': answers.get(step, '好的。'), 'annotations': []}]}


class RunTests(Base):
    def poll(self, sid, predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = self.store.get('agent_sessions', sid)
            if predicate(s):
                return s
            time.sleep(.05)
        self.fail('Timed out: ' + json.dumps(s, ensure_ascii=False)[:3000])

    def served_model(self, handler):
        handler.requests = []
        server = Server(handler)
        self.addCleanup(server.close)
        return self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent', 'base_url': server.url + '/v1', 'api_key': MODEL_KEY})

    def test_a_rerun_goes_through_scan_investigation_and_a_new_report(self):
        self.session('agent-session-a')
        self.report('inventory-a', 'agent-session-a')
        model = self.served_model(RerunModel)
        with runtime():
            s = self.inv.restart({'project_id': self.pid, 'robot_id': self.robot, 'model_id': model['id'], 'data_authorized': True})
            s = self.poll(s['id'], lambda s: s['status'] not in ACTIVE)
        self.assertEqual(s['status'], 'completed', s.get('error'))
        self.assertNotEqual(s['inventory_id'], 'inventory-a')
        scan = self.store.get('hardware_scans', s['investigation']['scan_id'])
        self.assertEqual((scan['source'], scan['session_id']), ('investigation', s['id']))
        self.assertLess(self.ssh.commands.index(SCAN), len(self.ssh.commands) - 1, 'the scan ran before the investigation probes')
        state = self.state()
        self.assertEqual((state['state'], state['inventory_id']), ('finished', s['inventory_id']))
        self.assertEqual([p['state'] for p in self.inv.progress(s)['phases']], ['done'] * 4)
        context = json.loads(self.e.artifact({'session_id': s['id'], 'path': 'context-copy.json'})['content'])
        self.assertEqual(context['inventories'], [])
        self.assertEqual(context['previous_inventory_ids'], ['inventory-a'])
        self.assertEqual(context['hardware_scan']['scan_id'], scan['id'])
        self.assertIn('hardware_scan', [e['kind'] for e in s['events']])

    def test_a_fix_chat_with_an_approved_command_and_a_second_message(self):
        self.scan()
        model = self.served_model(ChatModel)
        with runtime():
            s = self.inv.start_fix({'project_id': self.pid, 'robot_id': self.robot, 'model_id': model['id'], 'issue_ids': [],
                                    'message': '底盘串口打不开，帮我修好', 'data_authorized': True, 'modify_authorized': True})
            sid = s['id']
            pending = self.poll(sid, lambda s: bool(s.get('pending_approval')))['pending_approval']
            self.assertEqual(pending['method'], 'device_fix/requestApproval')
            self.assertEqual(pending['request']['issue_ids'], [investigation.USER_REQUEST])
            self.assertNotIn(USERMOD, self.ssh.commands, 'nothing runs before the user approves')
            self.e.approve({'session_id': sid, 'approval_id': pending['id'], 'decision': 'accept'})
            s = self.poll(sid, lambda s: s['status'] not in ACTIVE)
            self.assertEqual(s['status'], 'completed', s.get('error'))
            self.assertIn(USERMOD, self.ssh.commands)
            self.inv.fix_message({'project_id': self.pid, 'session_id': sid, 'message': '再看看 eth1 为什么没有网'})
            s = self.poll(sid, lambda s: s['status'] not in ACTIVE and [e['kind'] for e in s['events']].count('message') >= 2)
        self.assertEqual(s['status'], 'completed', s.get('error'))
        talk = [(e['kind'], e['message']) for e in s['events'] if e['kind'] in ('user', 'message')]
        self.assertEqual(talk, [('user', '底盘串口打不开，帮我修好'), ('message', '已把 robot 加入 dialout 组，重新登录后生效。'),
                                ('user', '再看看 eth1 为什么没有网'), ('message', 'eth1 没有链路信号，需要现场检查网线。')])
        self.assertIn('fix', [e['kind'] for e in s['events']])
        transcript = json.dumps(ChatModel.requests, ensure_ascii=False)
        self.assertIn('再看看 eth1 为什么没有网', json.dumps(ChatModel.requests[-1], ensure_ascii=False))
        self.assertIn(investigation.USER_REQUEST, transcript, 'the chat rules reach the model')
        for secret in (SSH_LOGIN, MODEL_KEY):
            self.assertNotIn(secret, transcript)


if __name__ == '__main__':
    unittest.main()
