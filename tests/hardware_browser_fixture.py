"""Browser fixture for hardware initialisation (issue #2): the console, an SSH robot that answers
the hardware scan like a Jetson robot, and a scripted model for the repair session.

Run from the repository root: ``python tests/hardware_browser_fixture.py [info.json]``.
Connect in the console with host 127.0.0.1, the printed SSH port, user ``robot`` and password
``fixture-password``; the scan opens right after the fingerprint is confirmed.

Models run only through TokenFans (issue #4): before starting a repair, open 云平台账户 from the
sidebar and connect with the printed ``tokenfans_key`` (a fake key, random per run). The stand-in
offers one model, ``fixture-repair``, which forwards to the scripted repair model. That model
reports on the two serial-port issues and the Orbbec camera only: untick the Wi-Fi driver issue in
the repair form, or the session ends 待继续 because that issue has no result.
"""
import json
import os
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
import tokenfans_fixture as tkf  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

if __name__ == '__main__':
    ssh = SSHFixture(command_outputs={hardware_scan.SCAN_EXEC: jetson().encode()})
    RepairModel.requests, RepairModel.ssh = [], ssh
    model = Server(RepairModel)
    tokenfans = tkf.TokenFansFixture(routes={'fixture-repair': model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    info = {'url': api.url, 'model': model.url + '/v1', 'ssh_port': ssh.port,
            'tokenfans': tokenfans.url, 'tokenfans_key': tkf.API_KEY}
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
        tokenfans.close()
        tmp.cleanup()
