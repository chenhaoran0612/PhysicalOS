"""Browser fixture: the console plus a "robot" and "jump host" SSH protocol fixtures.

    python tests/ssh_jump_browser_fixture.py [info.json]

Writes the console URL, the SSH ports and the fixture logins to the JSON file and
serves until interrupted. Nothing here reaches a real machine.

Issue #15 adds a chain of three jump hosts (``chain``: only the last one reaches the robot, each has
its own login, made up on every run) and a jump host that is not typed anywhere (``inner``): the
first jump host's ssh config names it as the ProxyJump of the name "pup".
"""
import json
import secrets
import sys
import tempfile
import time

from brainos import device_access, device_software, device_status, hardware_scan
from brainos.server import Application, make_handler
from ssh_fixture import SSHFixture
from test_product import Server
from test_ssh_jump import JUMP_PASSWORD, JUMP_USER, SERVICES, STATUS_OUTPUT
from hardware_fixture import jetson
from software_fixture import SERVICES as RUNNING_SERVICES, software

if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    watched = (SERVICES, ['bridge'], [])        # every watch list the browser test sets
    # Issue #8: the status page also lists the running software. Its services agree with STATUS_OUTPUT:
    # "bridge" runs, "nav-stack" failed, "recorder" is not running (so it is not in this list at all).
    running = RUNNING_SERVICES.replace('nav-stack.service              loaded active     running ',
                                       'nav-stack.service              loaded failed     failed  ')
    running += 'bridge.service                 loaded active     running      Chassis bridge\n'
    target = SSHFixture(command_outputs={**{device_status.status_command(names): STATUS_OUTPUT for names in watched},
                                         device_software.SOFTWARE_EXEC: software(services=running).encode(),
                                         hardware_scan.SCAN_EXEC: jetson().encode()})      # issue #2: scan after connecting
    # What `ssh dog` would connect to from the jump host: the answer its `ssh -G dog` gives.
    alias = {device_access.resolve_command('dog'): f'user robot\nhostname 127.0.0.1\nport {target.port}\n'.encode()}
    # Issue #15: a jump host the user never typed. "pup" is the robot again, but the jump host's config sends
    # it through "inner" first (ProxyJump).
    inner = SSHFixture(username='inner', password=secrets.token_hex(6), forward_to=[('127.0.0.1', target.port)])
    alias[device_access.resolve_command('pup')] = f'user robot\nhostname 127.0.0.1\nport {target.port}\nproxyjump inner\n'.encode()
    alias[device_access.resolve_command('inner')] = f'user inner\nhostname 127.0.0.1\nport {inner.port}\n'.encode()
    jump = SSHFixture(username=JUMP_USER, password=JUMP_PASSWORD,
                      forward_to=[('127.0.0.1', target.port), ('127.0.0.1', inner.port)], command_outputs=alias)
    # Issue #15: three jump hosts in a row. This machine reaches the first, each one the next, the last the robot.
    chain, reach = [], target
    for level in (3, 2, 1):
        reach = SSHFixture(username=f'gate{level}', password=secrets.token_hex(6), forward_to=[('127.0.0.1', reach.port)])
        chain.insert(0, reach)
    info = {'url': api.url, 'target_port': target.port, 'target_user': 'robot', 'target_password': 'fixture-password',
            'jump_port': jump.port, 'jump_user': JUMP_USER, 'jump_password': JUMP_PASSWORD, 'services': SERVICES,
            'chain': [{'port': gate.port, 'user': gate.username, 'password': gate.password} for gate in chain],
            'inner': {'port': inner.port, 'user': inner.username, 'password': inner.password}}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-ssh-jump-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close(); app.close(); target.close(); jump.close(); inner.close()
        for gate in chain:
            gate.close()
        tmp.cleanup()
