"""Browser acceptance fixture for issue #43: the 云端技能 page and the two new sections of a robot's page.

Serves the desktop app on a fresh temporary state directory together with three stand-ins, all on
this computer only:

* ``skill_cloud_fixture.FakeSkillCloud``: the cloud's skill library and the part of the material
  library a bundle needs. ``GET /fixture/cloud-login`` signs the browser's workspace in to it;
* ``bundle_fixture.BundleDevice``: a robot reached over SSH whose files live in a private temporary
  directory and which runs only the product's own scripts. ``~/walk_programs`` there is a made-up
  program directory;
* ``remote_runtime_fixture.RemoteRuntime``: a management layer with ``/v2/remote``, for what the real
  simulated robot cannot show (a link that is online, recent calls, an older version without it).

``GET /fixture/attach?project=<id>`` adds one robot to the browser's workspace that has both the SSH
device and that management layer. The simulated robot the test adds itself is a real
``neuxmind_runtime`` child process. ``GET /fixture/state`` answers what the stand-ins hold, so the
test can check that things really arrived. Nothing reaches a real server or a real machine.

Usage: python3 tests/cloud_skills_browser_fixture.py [config.json]
"""
import json
import os
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import socket  # noqa: E402

# No cloud of this computer is ever asked: the default cloud address is a port nothing listens on.
if 'NEUXMIND_DEFAULT_CLOUD_URL' not in os.environ:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        os.environ['NEUXMIND_DEFAULT_CLOUD_URL'] = 'http://127.0.0.1:%d' % probe.getsockname()[1]

from brainos.server import Application, make_handler  # noqa: E402
from bundle_fixture import BundleDevice  # noqa: E402
from remote_runtime_fixture import TOKEN, RemoteRuntime  # noqa: E402
from skill_cloud_fixture import FakeSkillCloud  # noqa: E402
from test_product import Server  # noqa: E402

ROBOT, CONNECTION = 'robot-ssh-fixture', 'connection-ssh-fixture'
HOME = '/home/robot'
MARKUP = '<img src=x onerror="window.__markupRan=1">'
PROGRAM = {'walk.py': b'print("walk")\n', 'maps/office.yaml': 'places:\n  会议室: [1.0, 2.0]\n'.encode(),
           'runs/2026-11-01/log.txt': b'a run log', 'lib/__pycache__/walk.pyc': b'\x00cache', 'walk.py.bak': b'old'}


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.mkdtemp(prefix='nx-issue43-ui-'), 'config.json')
    tmp = tempfile.mkdtemp(prefix='nx-issue43-state-')
    cloud, device, runtime = FakeSkillCloud(chunk_bytes=8192), BundleDevice(), RemoteRuntime()
    for name, data in PROGRAM.items():
        device.write('%s/walk_programs/%s' % (HOME, name), data)
    app = Application(tmp + '/app')
    config = {'robot_id': ROBOT, 'markup': MARKUP,
              'examples': os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'runtime', 'examples', 'skills')}

    class Handler(make_handler(app)):
        def do_GET(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith('/fixture/'):
                return super().do_GET()
            user = self.user()
            if not user:
                return None
            platform, query = app.workspace(user), parse_qs(parsed.query)
            return self.respond(fixture(parsed.path[len('/fixture/'):], platform, query))

    def fixture(what, platform, query):
        if what == 'cloud-login':
            cloud.sign_in(platform)
            return {'ok': True}
        if what == 'attach':
            return attach(platform, query['project'][0])
        if what == 'remote':
            runtime.supports_remote = query['supports'][0] == '1'
            return {'supports': runtime.supports_remote}
        if what == 'skill-change':           # the skill was installed again with other content
            runtime.skills[1]['digest'] = 'f' * 64
            return {'digest': runtime.skills[1]['digest']}
        if what == 'remote-calls':
            runtime.calls = [
                {'call_id': 'cal_fixture_1', 'skill_id': 'goto_place', 'status': 'succeeded', 'run_id': 'run_1',
                 'started_at': time.time() - 30, 'finished_at': time.time() - 21, 'error': None},
                {'call_id': 'cal_fixture_2', 'skill_id': MARKUP, 'status': 'rejected', 'run_id': None, 'started_at': None,
                 'finished_at': time.time() - 5, 'error': {'code': 'remote_not_allowed', 'message': MARKUP + '主人没有打开'}}]
            runtime.link['last_sync_at'] = time.time()
            return {'calls': len(runtime.calls)}
        if what == 'folder':                 # a local skill folder for the publish form: an example with another version and a title to escape
            return {'path': skill_folder()}
        if what == 'folder-change':
            with open(os.path.join(skill_folder(), 'skill.py'), 'a', encoding='utf-8') as handle:
                handle.write('\n# changed after it was published\n')
            return {'changed': True}
        if what == 'tamper':
            def slip_in(package):
                package['files']['skill.py'] += '\n# slipped in on the way\n'
            cloud.tamper = slip_in if query['on'][0] == '1' else None
            return {'tamper': cloud.tamper is not None}
        if what == 'state':
            return {'releases': sorted('%s@%s' % key for key in cloud.releases),
                    'products': [held['release'].get('product') for held in cloud.releases.values()],   # issue #48
                    'materials': [m['view'] for m in cloud.materials.values()],
                    'switches': runtime.switches, 'posted': runtime.posted,
                    'bundle_scripts': device.bundle_scripts, 'refused': device.refused,
                    'store': tree(device.local(HOME + '/neuxmind-bundles')),
                    'program': sorted(tree(device.local(HOME + '/walk_programs')))}
        return {'error': 'unknown fixture call'}

    def attach(platform, project_id):
        connection, secret = device.login()
        platform.store.put('robots', {'id': ROBOT, 'project_id': project_id, 'name': '巡检机器狗（测试替身）',
                                      'family': 'quadruped', 'endpoint': runtime.url, 'token': TOKEN, 'status': 'connected',
                                      'capabilities': [], 'connection_id': CONNECTION, 'created_at': '2026-11-24T00:00:00+00:00'})
        platform.store.put('device_connections', {**connection, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': project_id})
        platform.store.put('agent_secrets', {**secret, 'id': CONNECTION})
        return {'robot_id': ROBOT}

    def skill_folder():
        target = os.path.join(os.path.realpath(tmp), 'skills', 'status_report')
        if not os.path.isdir(target):
            shutil.copytree(os.path.join(config['examples'], 'status_report'), target)
            meta_path = os.path.join(target, 'skill.json')
            with open(meta_path, encoding='utf-8') as handle:
                meta = json.load(handle)
            meta.update(version='0.2.0', title=MARKUP + '状态报告')
            with open(meta_path, 'w', encoding='utf-8') as handle:
                json.dump(meta, handle, ensure_ascii=False)
        return target

    def tree(root):
        found = {}
        for folder, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, root)
                found[rel] = 'link:' + os.readlink(path) if os.path.islink(path) else 'dir' if os.path.isdir(path) else os.path.getsize(path)
        return found

    server = Server(Handler)
    config.update(url=server.url, cloud=cloud.url)
    with open(config_path, 'w') as handle:
        json.dump(config, handle)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        for stand_in in (cloud, device, runtime):
            stand_in.close()


if __name__ == '__main__':
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    main()
