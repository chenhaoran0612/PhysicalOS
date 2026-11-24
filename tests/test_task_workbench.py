"""Issue #44: 任务工作台 — a dashboard of the account's tasks; a task can be created and deleted.

A task is what the console has called a 任务项目 (kind ``projects``). Tasks live in the workspace of
the account that is signed in. Deleting one is new (brainos/task_remove.py); the page is
web/task_workbench.js, whose pure functions are checked in tests/task_workbench_logic.cjs.
"""
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import re
import secrets
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from brainos.engineering import ACTIVE
from brainos.server import Application, make_handler
from brainos.service import Platform, now
from brainos.task_remove import JOB_ACTIVE, KEPT, METERING_OPEN
from brainos.workflows import RUNNING
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
LINE = '<link rel="stylesheet" href="/task_workbench.css"><script src="/task_workbench.js" defer></script>'


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = Platform(self.tmp.name + '/workspace')
        self.addCleanup(self.platform.close)
        self.store = self.platform.store
        self.pid = self.task('仓库巡检')['id']

    def task(self, name='Task'):
        return self.platform.add_project({'name': name, 'objective': 'What the robot should get done'})

    def remove(self, project_id=None):
        return self.platform.task_remove.remove({'project_id': project_id or self.pid})

    def put(self, kind, project_id=None, **fields):
        record = {'id': f'{kind}-{secrets.token_hex(4)}', 'project_id': project_id or self.pid, 'created_at': now(), **fields}
        return self.store.put(kind, record)

    def ids(self, kind):
        return {x['id'] for x in self.store.list(kind)}


class RemoveTests(Case):
    def test_an_empty_task_goes_and_the_other_tasks_stay(self):
        other = self.task('Other')

        result = self.remove()

        self.assertEqual(result['removed'], self.pid)
        self.assertEqual(self.ids('projects'), {other['id']})

    def test_everything_the_task_holds_goes_with_it(self):
        kinds = ('services', 'models', 'datasets', 'skills', 'applications', 'jobs', 'evaluations', 'deployments',
                 'operations', 'scenes', 'model_deployments', 'agent_sessions', 'workflow_runs', 'engineering_workflows',
                 'agent_skills', 'agent_skill_versions', 'robot_inventories', 'device_evidence')
        made = [(kind, self.put(kind, status='completed')['id']) for kind in kinds]

        result = self.remove()

        for kind, key in made:
            self.assertNotIn(key, self.ids(kind), kind)
            self.assertEqual(result['deleted'][kind], 1, kind)

    def test_a_kind_nobody_listed_goes_too(self):
        # A later feature's records need no entry anywhere: whatever carries the task's id goes.
        record = self.put('records_of_a_later_feature')

        self.remove()

        self.assertNotIn(record['id'], self.ids('records_of_a_later_feature'))

    def test_records_of_other_tasks_are_not_touched(self):
        other = self.task('Other')
        kept = [(kind, self.put(kind, project_id=other['id'])['id']) for kind in ('models', 'applications', 'agent_sessions')]
        self.put('models')

        self.remove()

        for kind, key in kept:
            self.assertIn(key, self.ids(kind), kind)

    def test_saved_keys_go_with_what_they_belong_to(self):
        model = self.put('agent_models', billing='provider')
        server = self.put('agent_mcp_servers')
        other = self.put('agent_models', project_id=self.task('Other')['id'], billing='provider')
        for owner in (model, server, other):
            self.store.put('agent_secrets', {'id': owner['id'], 'api_key': 'k-' + secrets.token_hex(8)})

        self.remove()

        self.assertEqual(self.ids('agent_secrets'), {other['id']})

    def test_raw_scan_output_goes_with_its_scan(self):
        scan = self.put('hardware_scans')
        self.store.put('hardware_scan_raw', {'id': scan['id'], 'stdout': 'raw'})

        self.remove()

        self.assertEqual(self.ids('hardware_scan_raw'), set())

    def test_files_of_the_task_s_sessions_are_deleted(self):
        session = self.put('agent_sessions', status='completed')
        folder = self.store.root / 'engineering' / session['id']
        (folder / 'workspace').mkdir(parents=True)
        (folder / 'workspace' / 'NOTES.md').write_text('written by a run')
        other = self.store.root / 'engineering' / 'agent-session-of-another-task'
        other.mkdir(parents=True)

        self.remove()

        self.assertFalse(folder.exists())
        self.assertTrue(other.exists())

    def test_a_session_id_cannot_lead_out_of_the_sessions_folder(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        self.store.put('agent_sessions', {'id': '../../outside', 'project_id': self.pid, 'status': 'completed'})

        self.remove()

        self.assertTrue(outside.exists())

    def test_the_log_keeps_its_lines_and_says_what_went(self):
        self.put('models')
        self.assertIn('events', KEPT)
        before = len(self.store.list('events'))

        self.remove()

        events = self.store.list('events')
        self.assertEqual(len(events), before + 1)
        last = events[0]
        self.assertEqual(last['action'], 'project.removed')
        self.assertEqual(last['details']['name'], '仓库巡检')
        self.assertEqual(last['details']['deleted']['models'], 1)
        self.assertIsNone(last['project_id'])

    def test_a_task_that_is_gone_is_said_so_in_plain_words(self):
        self.remove()

        with self.assertRaisesRegex(ValueError, '任务已经不在'):
            self.remove()
        with self.assertRaisesRegex(ValueError, '任务已经不在'):
            self.platform.task_remove.remove({})


class RefusalTests(Case):
    def assert_kept(self):
        self.assertIn(self.pid, self.ids('projects'))

    def test_a_task_with_robots_is_not_deleted_and_names_them(self):
        self.platform.add_robot({'project_id': self.pid, 'name': '巡检狗', 'family': 'quadruped'})
        model = self.put('models')

        with self.assertRaisesRegex(ValueError, '还有 1 台机器人（巡检狗）.*我的机器人'):
            self.remove()

        self.assert_kept()
        self.assertIn(model['id'], self.ids('models'))

    def test_many_robots_are_counted_and_three_are_named(self):
        for n in range(5):
            self.platform.add_robot({'project_id': self.pid, 'name': f'R{n}', 'family': 'quadruped'})

        with self.assertRaisesRegex(ValueError, '还有 5 台机器人（.*等）'):
            self.remove()

    def test_robots_of_other_tasks_do_not_count(self):
        other = self.task('Other')
        self.platform.add_robot({'project_id': other['id'], 'name': 'Elsewhere', 'family': 'quadruped'})

        self.remove()

        self.assertEqual(self.ids('projects'), {other['id']})

    def test_a_running_session_keeps_the_task(self):
        for status in ACTIVE:
            session = self.put('agent_sessions', status=status)
            with self.assertRaisesRegex(ValueError, '助手会话正在运行'):
                self.remove()
            self.assert_kept()
            self.store.delete('agent_sessions', session['id'])

    def test_a_session_the_engine_still_holds_keeps_the_task(self):
        session = self.put('agent_sessions', status='completed')
        self.platform.engineering.live[session['id']] = object()
        self.addCleanup(self.platform.engineering.live.clear)

        with self.assertRaisesRegex(ValueError, '助手会话正在运行'):
            self.remove()
        self.assertIn(session['id'], self.ids('agent_sessions'))

    def test_a_running_flow_keeps_the_task(self):
        for status in RUNNING:
            run = self.put('workflow_runs', status=status)
            with self.assertRaisesRegex(ValueError, '协作流程正在运行'):
                self.remove()
            self.assert_kept()
            self.store.delete('workflow_runs', run['id'])

    def test_a_job_that_is_out_at_a_service_keeps_the_task(self):
        for status in JOB_ACTIVE:
            job = self.put('jobs', status=status)
            with self.assertRaisesRegex(ValueError, '任务还在执行'):
                self.remove()
            self.assert_kept()
            self.store.delete('jobs', job['id'])

    def test_a_job_whose_submission_got_no_answer_keeps_the_task(self):
        # While the request is out, and when it may have been accepted: service.submit_job writes the job again.
        self.assertIn("dispatching", JOB_ACTIVE)
        self.assertIn("unknown", JOB_ACTIVE)

    def test_credits_still_held_for_a_job_keep_the_task(self):
        class Records:
            def __init__(self, status):
                self.status = status

            def get(self, kind, key):
                if (kind, key) != ("reservations", "reservation-1"):
                    raise ValueError("missing")
                return {"id": key, "status": self.status}

        class Commerce:
            def __init__(self, status):
                self.store = Records(status)

        self.put("jobs", status="failed", name="夜间训练", reservation_id="reservation-1")
        self.put("jobs", status="failed", reservation_id="reservation-that-is-gone")
        self.platform.commerce = Commerce("held")
        with self.assertRaisesRegex(ValueError, "预留额度还没有结算（夜间训练）"):
            self.remove()
        self.assert_kept()

        self.platform.commerce = Commerce("settled")
        self.remove()
        self.assertEqual(self.ids("projects"), set())

    def test_a_cloud_session_that_is_not_closed_keeps_the_task(self):
        for status in METERING_OPEN:
            session = self.put("agent_sessions", status="completed", metering_status=status, name="调研")
            with self.assertRaisesRegex(ValueError, "云端会话还没有关闭.*调研"):
                self.remove()
            self.assert_kept()
            self.store.delete("agent_sessions", session["id"])
        self.put("agent_sessions", status="completed", metering_status="cloud_settled")
        self.remove()

    def test_finished_work_does_not_keep_it(self):
        self.put('agent_sessions', status='completed')
        self.put('workflow_runs', status='completed')
        self.put('jobs', status='succeeded')
        self.put('jobs', status='draft')

        self.remove()

        self.assertEqual(self.ids('projects'), set())

    def test_nothing_goes_when_a_session_s_files_cannot_be_deleted(self):
        session = self.put('agent_sessions', status='completed')
        model = self.put('models')
        folder = self.store.root / 'engineering' / session['id']
        folder.mkdir(parents=True)
        real = shutil.rmtree
        try:
            shutil.rmtree = lambda *a, **k: None          # as when another program holds the folder
            with self.assertRaisesRegex(ValueError, '没能从这台电脑上删掉'):
                self.remove()
        finally:
            shutil.rmtree = real

        self.assert_kept()
        self.assertIn(model['id'], self.ids('models'))
        self.assertIn(session['id'], self.ids('agent_sessions'))


class AccountTests(unittest.TestCase):
    """Tasks belong to the account that is signed in: over HTTP, as the page does it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(self.tmp.name)
        self.addCleanup(self.app.close)
        self.server = Server(make_handler(self.app))
        self.addCleanup(self.server.close)
        self.host = self.server.url.removeprefix('http://')

    def call(self, path, payload=None, cookie=None):
        connection = http.client.HTTPConnection(self.host, timeout=20)
        headers = {'Content-Type': 'application/json', **({'Cookie': cookie} if cookie else {})}
        connection.request('POST' if payload is not None else 'GET', '/api/' + path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        set_cookie = response.getheader('Set-Cookie')
        connection.close()
        return response.status, body, set_cookie

    def account(self, email):
        form = {'name': email.split('@')[0], 'email': email, 'password': 'Aa1-' + secrets.token_hex(8)}
        _, sent, _ = self.call('register/start', form)
        status, body, cookie = self.call('register', {**form, 'code': sent['dev_code']})
        self.assertEqual(status, 200, body)
        return cookie.split(';')[0]

    def names(self, cookie):
        return sorted(p['name'] for p in self.call('state', cookie=cookie)[1]['projects'])

    def test_each_account_sees_and_deletes_only_its_own_tasks(self):
        first, second = self.account('first@example.test'), self.account('second@example.test')
        _, mine, _ = self.call('projects', {'name': '甲的任务', 'objective': '巡检'}, first)
        _, theirs, _ = self.call('projects', {'name': '乙的任务', 'objective': '搬运'}, second)

        self.assertEqual(self.names(first), ['甲的任务'])
        self.assertEqual(self.names(second), ['乙的任务'])

        status, body, _ = self.call('projects/remove', {'project_id': theirs['id']}, first)
        self.assertEqual(status, 400)
        self.assertIn('任务已经不在', body['error'])
        self.assertEqual(self.names(second), ['乙的任务'])

        status, body, _ = self.call('projects/remove', {'project_id': mine['id']}, first)
        self.assertEqual((status, body['removed']), (200, mine['id']))
        self.assertEqual(self.names(first), [])
        self.assertEqual(self.names(second), ['乙的任务'])

    def test_deleting_needs_a_signed_in_account(self):
        status, _, _ = self.call('projects/remove', {'project_id': 'project-x'})

        self.assertEqual(status, 401)

    def test_the_page_files_are_served_and_loaded_once(self):
        for path, mime in (('/task_workbench.js', 'text/javascript'), ('/task_workbench.css', 'text/css')):
            with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
                self.assertTrue(response.headers.get('Content-Type', '').startswith(mime), path)
                self.assertEqual(response.read(), (ROOT / 'web' / path[1:]).read_bytes())
        with urllib.request.urlopen(self.server.url + '/', timeout=10) as response:
            index = response.read().decode('utf-8')
        self.assertEqual(index.count(LINE), 1)
        self.assertGreater(index.index(LINE), index.index('/style.css'))
        # The page is taken over once every script has run, so the place of the line does not matter.
        script = (ROOT / 'web' / 'task_workbench.js').read_text(encoding='utf-8')
        self.assertIn("document.addEventListener('DOMContentLoaded', twInstall)", script)


class PageTests(unittest.TestCase):
    def test_the_page_names_the_same_states_of_work_as_the_server(self):
        # The dialog explains beforehand what the server will refuse: the two lists must not drift apart.
        script = (ROOT / "web" / "task_workbench.js").read_text(encoding="utf-8")

        def listed(name):
            return tuple(re.findall(r"'([a-z_]+)'", re.search(rf"const {name} = \[(.*?)\];", script).group(1)))

        self.assertEqual(listed("TW_SESSION_ACTIVE"), tuple(ACTIVE))
        self.assertEqual(listed("TW_FLOW_RUNNING"), tuple(RUNNING))
        self.assertEqual(listed("TW_JOB_ACTIVE"), tuple(JOB_ACTIVE))

    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/task_workbench_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'task_workbench_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
