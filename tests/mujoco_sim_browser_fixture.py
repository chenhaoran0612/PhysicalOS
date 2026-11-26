"""Browser fixture for issue #51: the library by brand and model, the robot model, and MuJoCo simulation.

Serves the desktop app together with the real cloud app (tests/cloud_fixture: local uvicorn, SQLite, a
temporary directory, its parse worker running every second). The workspace starts with a task, one robot
of a made-up brand and model (not reachable: nothing is contacted), a skill and an application for it.
The engineering assistant is the deterministic stand-in (or the pinned runtime when
``NEUXMIND_TEST_AGENT_BINARY`` is set) talking to a scripted model behind a TokenFans stand-in: its one
command writes a made-up scene. The simulated robot is the real edge runtime with the MuJoCo body, in a
child process (needs the ``mujoco`` package).

``GET /fixture/dog.zip`` and ``/fixture/dog.urdf`` hand out the made-up robot model the test uploads.

Usage: python3 tests/mujoco_sim_browser_fixture.py CONFIG.json
Writes {url, cloud, email, password, cloud_email, cloud_password, application_id, robot_id} to CONFIG.json.
"""
import json
import os
import signal
import sys
import tempfile
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_binary  # noqa: F401,E402
import cloud_fixture  # noqa: E402
import mujoco_support as ms  # noqa: E402
import tokenfans_fixture as tkf  # noqa: E402
from brainos import agent_runtime  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from codex_standin import CodexStandIn  # noqa: E402
from test_mujoco_sim import SceneModel, writes  # noqa: E402
from test_product import Server  # noqa: E402

EMAIL = 'mujoco-ui@example.test'
CLOUD_EMAIL = 'mujoco-cloud@example.test'
PRODUCT = {'vendor': 'Acme Robotics', 'model': 'Go-7'}

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), 'neuxmind-mujoco-ui.json')
    if not agent_runtime.binary_path():
        real_available = agent_runtime.available
        agent_runtime.AgentProcess = CodexStandIn
        agent_runtime.available = lambda: {**real_available(), 'available': True}
    tmp = tempfile.TemporaryDirectory(prefix='neuxmind-mujoco-ui-')
    cloud = cloud_fixture.CloudServer(models=(), library_parse_interval_seconds=1, library_parse_threads=1)
    cloud.register(CLOUD_EMAIL)
    app = Application(tmp.name + '/app')
    password = 'mujoco-ui-' + os.urandom(6).hex() + '-7'       # a throwaway account of this run
    _, user = app.identity.authenticate({'email': EMAIL, 'password': password, 'name': '仿真验收'}, register=True)
    workspace = app.workspace(user)
    pid = workspace.add_project({'name': '仓库巡检', 'objective': '从门口走到货架，把纸箱推到墙边。全部为测试资料。'})['id']
    robot = workspace.add_robot({'project_id': pid, 'name': '一号狗', 'family': 'quadruped', **PRODUCT})
    skill = workspace.add_skill({'project_id': pid, 'name': '走到门口', 'version': '1.0', 'requires': ['navigation.goal'],
                                 'entrypoint': 'navigate', 'success_condition': '到达门口', 'stop_condition': '超时',
                                 'model_ids': [], 'robot_id': robot['id']})
    application = workspace.add_application({'project_id': pid, 'name': '送货到门口', 'version': '1.0', 'robot_id': robot['id'],
                                             'steps': [{'skill_id': skill['id'], 'parameters': {'place': '门口'}, 'timeout_s': 30}]})
    SceneModel.command = writes(ms.scene())
    model = Server(SceneModel)
    tokenfans = tkf.TokenFansFixture(routes={'fixture-scene': model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    workspace.model_compute.connect_key({'api_key': tkf.API_KEY})

    class Handler(make_handler(app)):
        def do_GET(self):
            where = urlparse(self.path).path
            if where == '/fixture/dog.zip':
                return self.respond(ms.dog_archive(), mime='application/zip')
            if where == '/fixture/dog.urdf':
                return self.respond(ms.dog_urdf(None), mime='application/octet-stream')
            return super().do_GET()

    server = Server(Handler)
    with open(config_path, 'w') as f:
        json.dump({'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'password': password, 'cloud_email': CLOUD_EMAIL,
                   'cloud_password': cloud_fixture.PASSWORD, 'application_id': application['id'], 'robot_id': robot['id'],
                   'project_id': pid, 'pid': os.getpid()}, f)
    print(server.url, flush=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    try:
        while not stop:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        model.close()
        tokenfans.close()
        cloud.stop()
        tmp.cleanup()
