"""Browser fixture for hardware initialisation (issue #2): the console, an SSH robot that answers
the hardware scan like a Jetson robot, and a scripted model for the repair session.

Run from the repository root: ``python tests/hardware_browser_fixture.py [info.json]``.
Connect in the console with host 127.0.0.1, the printed SSH port, user ``robot`` and password
``fixture-password``; the scan opens right after the fingerprint is confirmed.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos import agent_runtime, hardware_scan  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_hardware_init import RepairModel  # noqa: E402
from test_product import Server  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    ssh = SSHFixture(command_outputs={hardware_scan.SCAN_EXEC: jetson().encode()})
    RepairModel.requests, RepairModel.ssh = [], ssh
    model = Server(RepairModel)
    info = {'url': api.url, 'model': model.url + '/v1', 'ssh_port': ssh.port}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-hardware-ui.json', 'w') as f:
        json.dump(info, f)
    print(json.dumps(info), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close()
        app.close()
        model.close()
        ssh.close()
        tmp.cleanup()
