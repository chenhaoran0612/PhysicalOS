"""ux01: one-click simulated robots, example skill gallery, live status, running a skill, cloud probe.

These start real ``neuxmind_runtime serve`` child processes with the built-in
simulated bodies (sim_quadruped / sim_arm). Nothing touches real hardware.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import robot_caps, runtime_bridge, sim_robots  # noqa: E402
from brainos.service import Platform  # noqa: E402
from test_product import DeviceFixture, Server  # noqa: E402
from brainos.edge import Gateway, make_handler as edge_handler  # noqa: E402


def wait_for(predicate, seconds=20.0, step=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                   # a zombie (not yet reaped) counts as gone
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split(')')[-1].split()[0] != 'Z'
    except OSError:
        return True


class SimRobotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux01-test-')
        self.root = self.tmp.name + '/ws'
        self.app = Platform(self.root)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def reopen(self):
        self.app.close()
        self.app = Platform(self.root)

    def add(self, kind='quadruped', **extra):
        return self.app.add_sim_robot({'kind': kind, **extra})

    def install(self, robot_id, example, request_id):
        preview = self.app.preview_skill_package({'robot_id': robot_id, 'example': example})
        self.assertTrue(preview['can_install'], preview['compatibility'])
        return self.app.install_skill_package({'robot_id': robot_id, 'example': example, 'request_id': request_id,
                                               'package_digest': preview['package_digest']})

    def run_skill(self, robot_id, skill, request_id, parameters=None, authorized=True):
        return self.app.run_robot_skill({'robot_id': robot_id, 'skill_id': skill, 'request_id': request_id,
                                         'parameters': parameters or {}, 'hardware_authorized': authorized})

    # ------------------------------------------------------------ lifecycle
    def test_one_click_quadruped_is_connected_labelled_and_token_free(self):
        robot = self.add()
        self.assertEqual(robot['status'], 'connected')
        self.assertEqual(robot['family'], 'quadruped')
        self.assertEqual(robot['sim']['state'], 'running')
        self.assertTrue(robot['endpoint'].startswith('http://127.0.0.1:'))
        self.assertNotIn('token', robot)
        # A first-time user has no project: one is created for them.
        project = self.app.project(robot['project_id'])
        self.assertEqual(project['name'], sim_robots.DEFAULT_PROJECT['name'])
        stored = self.app.store.get('robots', robot['id'])
        self.assertGreaterEqual(len(stored['token']), 24)
        self.assertTrue(stored['manifest_v2']['simulated'])
        snapshot = self.app.snapshot()
        self.assertNotIn(stored['token'], json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(snapshot['robots'][0]['sim']['state'], 'running')
        self.assertEqual({k['kind'] for k in snapshot['sim_kinds']}, {'quadruped', 'arm'})
        # Address and token are managed by the app.
        with self.assertRaisesRegex(ValueError, '自动管理'):
            self.app.configure_connection({'kind': 'robots', 'id': robot['id'], 'endpoint': 'http://127.0.0.1:1'})

    def test_second_sim_gets_its_own_port_name_and_process(self):
        a = self.add()
        b = self.add('quadruped', project_id=a['project_id'])
        c = self.add('arm', project_id=a['project_id'])
        self.assertEqual(len({a['endpoint'], b['endpoint'], c['endpoint']}), 3)
        self.assertEqual(b['name'], '仿真四足机器人 2')
        self.assertEqual(c['family'], 'manipulator')
        self.assertEqual(len({p.pid for p in self.app.sims.procs.values()}), 3)
        with self.assertRaisesRegex(ValueError, '四足或机械臂'):
            self.add('humanoid')

    def test_close_stops_children_and_restart_brings_back_robot_skills_and_latch(self):
        robot = self.add()
        self.assertEqual(self.install(robot['id'], 'status_report', 'i1')['status'], 'succeeded')
        self.assertEqual(self.app.stop_robot({'robot_id': robot['id']})['status'], 'accepted')
        pid = self.app.sims.procs[robot['id']].pid
        self.reopen()
        self.assertTrue(wait_for(lambda: not alive(pid), 10), '仿真进程在应用关闭后仍在运行')
        record = self.app.store.get('robots', robot['id'])
        self.assertIn(record['sim']['state'], ('starting', 'running'))
        wait_for(lambda: self.app.store.get('robots', robot['id'])['sim']['state'] == 'running', 30)
        record = self.app.store.get('robots', robot['id'])
        self.assertEqual(record['status'], 'connected')
        detail = self.app.robot_detail({'robot_id': robot['id']})
        self.assertEqual([s['id'] for s in detail['skills']['items']], ['status_report'])
        live = self.app.robot_live({'robot_id': robot['id']})
        self.assertTrue(live['estop']['latched'], '急停锁存应在重启后保留')

    def test_stopped_sim_stays_off_after_restart_and_can_be_started_or_removed(self):
        robot = self.add()
        pid = self.app.sims.procs[robot['id']].pid
        off = self.app.sims.api_stop({'robot_id': robot['id']})
        self.assertEqual(off['sim']['state'], 'stopped')
        self.assertTrue(wait_for(lambda: not alive(pid), 10))
        self.assertIn('关闭', self.app.robot_live({'robot_id': robot['id']})['error'])
        self.reopen()
        time.sleep(0.5)
        self.assertEqual(self.app.store.get('robots', robot['id'])['sim']['state'], 'stopped')
        self.assertEqual(self.app.sims.api_start({'robot_id': robot['id']})['status'], 'connected')
        pid = self.app.sims.procs[robot['id']].pid
        state_dir = Path(self.root) / 'sims' / robot['id']
        self.assertTrue(state_dir.is_dir())
        self.app.sims.remove({'robot_id': robot['id']})
        self.assertTrue(wait_for(lambda: not alive(pid), 10))
        self.assertFalse(state_dir.exists())
        with self.assertRaises(ValueError):
            self.app.store.get('robots', robot['id'])

    def test_sim_dies_with_the_app_even_when_the_app_is_killed(self):
        script = ('import sys,time;sys.path.insert(0,%r)\n'
                  'from brainos.service import Platform\n'
                  'p=Platform(%r)\n'
                  'r=p.add_sim_robot({"kind":"arm"})\n'
                  'print(p.sims.procs[r["id"]].pid,flush=True)\n'
                  'time.sleep(120)\n') % (str(ROOT), self.tmp.name + '/killed')
        proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
        try:
            child = int(proc.stdout.readline())
            self.assertTrue(alive(child))
            proc.send_signal(signal.SIGKILL)       # no cleanup code runs in the app
            proc.wait(10)
            self.assertTrue(wait_for(lambda: not alive(child), 15), '应用被强制结束后仿真进程仍在运行')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()

    def test_failed_start_is_reported_on_the_robot(self):
        original = runtime_bridge.child_command
        sim_robots.runtime_bridge.child_command = lambda args: [sys.executable, '-c', 'print("boom");import sys;sys.exit(3)']
        try:
            with self.assertRaisesRegex(ValueError, '没有启动成功'):
                self.add()
        finally:
            sim_robots.runtime_bridge.child_command = original
        robot = self.app.store.list('robots')[0]
        self.assertEqual(robot['sim']['state'], 'failed')
        self.assertIn('boom', robot['sim']['error'])

    # ------------------------------------------------------------ gallery + install
    def test_gallery_lists_examples_with_compatibility_per_robot(self):
        quad = self.add()
        arm = self.add('arm', project_id=quad['project_id'])
        gallery = self.app.skill_examples({'project_id': quad['project_id'], 'robot_id': quad['id']})
        self.assertTrue(gallery['available'])
        items = {x['example']: x for x in gallery['items']}
        self.assertEqual(sorted(items), ['patrol', 'status_report', 'wave'])
        compat = {name: {c['robot_id']: c['compatible'] for c in x['compatibility']} for name, x in items.items()}
        self.assertEqual(compat['patrol'], {quad['id']: True, arm['id']: False})
        self.assertEqual(compat['wave'], {quad['id']: False, arm['id']: True})
        self.assertEqual(compat['status_report'], {quad['id']: True, arm['id']: True})
        wave_on_quad = next(c for c in items['wave']['compatibility'] if c['robot_id'] == quad['id'])
        self.assertTrue(any('arm.joint_position' in r for r in wave_on_quad['reasons']))
        self.assertTrue(items['patrol']['moves'])
        self.assertFalse(items['status_report']['moves'])
        self.assertIn('waypoints', items['patrol']['preset'])

    def test_install_example_without_a_path_and_refuse_unknown_examples(self):
        quad = self.add()
        result = self.install(quad['id'], 'patrol', 'p1')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['example'], 'patrol')
        self.assertIn('patrol', [s['id'] for s in result['skills']['items']])
        preview = self.app.preview_skill_package({'robot_id': quad['id'], 'example': 'wave'})
        self.assertFalse(preview['can_install'])
        for bad in ('../patrol', 'nope', '', '/etc'):
            with self.assertRaises(ValueError):
                self.app.preview_skill_package({'robot_id': quad['id'], 'example': bad or None, 'path': bad})

    # ------------------------------------------------------------ run + live + stop
    def test_run_patrol_watch_it_move_then_stop_latches(self):
        quad = self.add()
        self.install(quad['id'], 'patrol', 'p1')
        with self.assertRaisesRegex(ValueError, '授权'):
            self.run_skill(quad['id'], 'patrol', 'r0', {'waypoints': [[1, 0]]}, authorized=False)
        op = self.run_skill(quad['id'], 'patrol', 'r1', {'waypoints': [[2, 0], [2, 2]]})
        self.assertEqual(op['status'], 'accepted', op)
        # Same request id: the recorded outcome, nothing sent twice.
        self.assertEqual(self.run_skill(quad['id'], 'patrol', 'r1')['run_id'], op['run_id'])
        first = wait_for(lambda: (lambda v: v if v.get('run', {}).get('status') == 'running'
                                  and v['state']['pose']['x'] > 0.05 else None)(self.app.robot_live({'robot_id': quad['id']})))
        self.assertTrue(first, '巡逻开始后位置没有变化')
        self.assertEqual(first['run']['id'], op['run_id'])
        self.assertEqual(first['run']['target'], 'patrol@0.1.0')
        self.assertTrue(first['simulated'])
        self.assertFalse(first['estop']['latched'])
        self.assertEqual(self.app.stop_robot({'robot_id': quad['id']})['status'], 'accepted')
        stopped = wait_for(lambda: (lambda v: v if v['run']['status'] != 'running' else None)(
            self.app.robot_live({'robot_id': quad['id']})))
        self.assertTrue(stopped['estop']['latched'])
        x = stopped['state']['pose']['x']
        time.sleep(1.0)
        self.assertAlmostEqual(self.app.robot_live({'robot_id': quad['id']})['state']['pose']['x'], x, places=3)
        self.assertTrue(self.app.store.get('robots', quad['id'])['estop_latched'])
        with self.assertRaisesRegex(ValueError, '急停已锁存'):
            self.run_skill(quad['id'], 'patrol', 'r2', {'waypoints': [[0, 0]]})
        # Reset keeps its on-site confirmation, then the robot can run again.
        with self.assertRaises(ValueError):
            self.app.reset_robot({'robot_id': quad['id']})
        self.assertEqual(self.app.reset_robot({'robot_id': quad['id'], 'hardware_authorized': True,
                                               'request_id': 'reset-1'})['status'], 'accepted')
        self.assertEqual(self.run_skill(quad['id'], 'patrol', 'r3', {'waypoints': [[0, 0]]})['status'], 'accepted')

    def test_arm_live_view_has_joints(self):
        arm = self.add('arm')
        self.install(arm['id'], 'wave', 'w1')
        self.assertEqual(self.run_skill(arm['id'], 'wave', 'w-run', {'cycles': 1})['status'], 'accepted')
        moving = wait_for(lambda: (lambda v: v if any(abs(j['position']) > 0.05 for j in v['state']['joints']) else None)(
            self.app.robot_live({'robot_id': arm['id']})))
        self.assertTrue(moving, '挥手运行后关节没有变化')
        self.assertEqual(len(moving['state']['joints']), 6)
        self.assertIn('opening', moving['state']['gripper'])

    def test_run_shortcut_is_for_simulated_robots_only(self):
        gateway = Gateway(DeviceFixture(), 'test-only-token-123456789000', self.tmp.name + '/edge')
        edge = Server(edge_handler(gateway))
        try:
            project = self.app.add_project({'name': 'P', 'objective': 'O'})
            real = self.app.add_robot({'project_id': project['id'], 'name': 'real', 'family': 'mobile',
                                       'endpoint': edge.url, 'token': 'test-only-token-123456789000'})
            with self.assertRaisesRegex(ValueError, '只对仿真机器人开放'):
                self.run_skill(real['id'], 'patrol', 'x1')
        finally:
            edge.close()
            gateway.store.close()

    # ------------------------------------------------------------ plain language
    def test_capability_rows_say_what_the_robot_can_do(self):
        row = robot_caps.capability_row('base.navigate_to')
        self.assertEqual(row['title'], '导航到目标')
        self.assertIn('走到', row['does'])
        from brainos.spec_bridge import capabilities as spec_caps
        for cap_id in spec_caps.CAPABILITIES:
            self.assertTrue(robot_caps.capability_row(cap_id)['does'])
        self.assertTrue(robot_caps.capability_row('x.vendor.thing')['does'])


class CloudProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Platform(self.tmp.name + '/ws')

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_default_address_is_prefilled_and_unreachable_cloud_is_explained(self):
        summary = self.app.cloud.summary()
        self.assertEqual(summary['default_base_url'], 'http://127.0.0.1:8800')
        server = Server(BaseHTTPRequestHandler)
        url = server.url
        server.close()                                  # nothing listens there any more
        result = self.app.cloud.probe({'base_url': url})
        self.assertFalse(result['reachable'])
        self.assertIn('连不上', result['message'])
        self.assertFalse(self.app.cloud.summary()['configured'], '探测不应保存地址')

    def test_reachable_cloud(self):
        class Cloud(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b'{"capabilities": []}' if self.path == '/api/v1/capabilities/registry' else b'{}'
                self.send_response(200 if self.path == '/api/v1/capabilities/registry' else 404)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = Server(Cloud)
        try:
            result = self.app.cloud.probe({'base_url': server.url})
            self.assertTrue(result['reachable'], result)
        finally:
            server.close()


class PackagingTests(unittest.TestCase):
    def test_bundle_contents_check_passes(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_macos.py'), '--check'],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['examples'], ['patrol', 'status_report', 'wave'])
        self.assertIn('runtime/examples/skills', report['data'])
        self.assertIn('neuxmind_runtime', report['packages'])

    def test_pyinstaller_arguments_bundle_runtime_spec_and_examples(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import build_macos
        finally:
            sys.path.pop(0)
        args = [str(a) for a in build_macos.pyinstaller_args('d', 'w', 's')]
        joined = ' '.join(args)
        for needle in ('--collect-submodules neuxmind_runtime', '--collect-submodules neuxmind_spec',
                       'runtime/examples/skills' + os.pathsep + 'runtime/examples/skills',
                       'web' + os.pathsep + 'web', str(ROOT / 'runtime')):
            self.assertIn(needle, joined)
        self.assertTrue(args[-1].endswith('desktop/launcher.py'))

    def test_launcher_dispatches_the_sim_child(self):
        text = (ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
        self.assertIn("'--nx-sim-child'", text)
        self.assertIn('brainos.sim_child', text)


if __name__ == '__main__':
    unittest.main()
