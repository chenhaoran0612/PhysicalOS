"""Browser fixture for issue #6: investigation states, a full re-run and a fix chat.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/investigation_browser_fixture.py [info.json]``, then
``node tests/investigation_browser.cjs info.json [screenshot dir]``.

The console, an SSH robot that answers the hardware scan like a Jetson robot, and a scripted
model behind the TokenFans stand-in (issue #4). The model: an investigation asks for one SSH
command (so the run waits for approval and its progress can be seen), probes, saves a report;
a fix chat proposes one device_fix, then answers each message.
The SSH password and the TokenFans key are random per run and written to the info file.
"""
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_binary  # noqa: E402,F401  (optional NEUXMIND_TEST_AGENT_BINARY)
from brainos import agent_runtime, hardware_scan  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_engineering import ResponsesFixture  # noqa: E402
from test_product import Server  # noqa: E402
import tokenfans_fixture as tkf  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

MODEL = 'fixture-investigation'
COMMAND = 'systemctl show robot-stack -p ExecStart'
FIX = {'command': 'usermod -aG dialout robot', 'purpose': '让 SSH 用户可以读写底盘串口', 'issue_ids': ['user-request'], 'risk': 'low',
       'rollback': 'gpasswd -d robot dialout', 'needs_root': True, 'timeout_seconds': 30}
FIRST_ANSWER = '已把 robot 加入 dialout 组，重新登录后生效。'
SECOND_ANSWER = 'eth1 没有链路信号，需要现场检查网线。'


def call(name, args, n):
    return {'id': f'fc_{n}', 'type': 'function_call', 'call_id': f'call_{name}_{n}', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}


def say(text, n):
    return {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


class InvestigationUIModel(ResponsesFixture):
    """Decides from the conversation it is sent, so investigations, re-runs and chats can interleave."""
    requests = []

    def select_item(self, data, fallback):
        n = len(type(self).requests)
        text = json.dumps(data, ensure_ascii=False)
        items = [i for i in data.get('input') or [] if isinstance(i, dict)]
        called = [i.get('name') for i in items if i.get('type') == 'function_call']
        if '对话修复' in text:
            if 'device_fix' not in called:
                return call('device_fix', FIX, n)
            # Codex sends the whole conversation each time; the second user message is in it from then on.
            return say(SECOND_ANSWER if '再看看' in text else FIRST_ANSWER, n)
        evidence = list(dict.fromkeys(re.findall(r'evidence-[a-f0-9]{12}', text)))
        if 'device_exec' not in called:
            return call('device_exec', {'command': COMMAND, 'purpose': '查明本体服务的实际启动入口', 'timeout_seconds': 10}, n)
        if 'device_probe' not in called:
            return call('device_probe', {'probe': 'system'}, n)
        if 'save_inventory' not in called:
            return call('save_inventory', {'summary': 'Jetson 机载电脑，运行 robot-stack 服务', 'unknowns': ['底盘串口权限待确认'],
                                           'recommendations': ['给 SSH 用户串口权限后再开发适配器'],
                                           'components': [{'category': 'system', 'name': 'Linux', 'details': 'Ubuntu on Jetson', 'evidence_ids': evidence[-1:]},
                                                          {'category': 'services', 'name': 'robot-stack', 'details': '厂商启动服务', 'evidence_ids': evidence[:1]}]}, n)
        return say('已保存本体档案。', n)


if __name__ == '__main__':
    password = secrets.token_hex(10)
    ssh = SSHFixture(command_outputs={hardware_scan.SCAN_EXEC: jetson().encode(), COMMAND: b'ExecStart=/opt/vendor/robot_stack\n'}, password=password)
    model = Server(InvestigationUIModel)
    tokenfans = tkf.TokenFansFixture(routes={MODEL: model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    info = {'url': api.url, 'ssh_port': ssh.port, 'ssh_password': password, 'tokenfans': tokenfans.url, 'tokenfans_key': tkf.API_KEY, 'model': MODEL,
            'answers': [FIRST_ANSWER, SECOND_ANSWER]}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-investigation-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
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
