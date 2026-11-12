"""Browser fixture: the console plus a "robot" and a "jump host" SSH protocol fixture.

    python tests/ssh_jump_browser_fixture.py [info.json]

Writes the console URL, both SSH ports and the fixture logins to the JSON file and
serves until interrupted. Nothing here reaches a real machine.
"""
import json
import sys
import tempfile
import time

from brainos import device_access, device_status, hardware_scan
from brainos.server import Application, make_handler
from ssh_fixture import SSHFixture
from test_product import Server
from test_ssh_jump import JUMP_PASSWORD, JUMP_USER, SERVICES, STATUS_OUTPUT
from hardware_fixture import jetson

if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    watched = (SERVICES, ['bridge'], [])        # every watch list the browser test sets
    target = SSHFixture(command_outputs={**{device_status.status_command(names): STATUS_OUTPUT for names in watched},
                                         hardware_scan.SCAN_EXEC: jetson().encode()})      # issue #2: scan after connecting
    # What `ssh dog` would connect to from the jump host: the answer its `ssh -G dog` gives.
    alias = {device_access.resolve_command('dog'): f'user robot\nhostname 127.0.0.1\nport {target.port}\n'.encode()}
    jump = SSHFixture(username=JUMP_USER, password=JUMP_PASSWORD, forward_to=[('127.0.0.1', target.port)],
                      command_outputs=alias)
    info = {'url': api.url, 'target_port': target.port, 'target_user': 'robot', 'target_password': 'fixture-password',
            'jump_port': jump.port, 'jump_user': JUMP_USER, 'jump_password': JUMP_PASSWORD, 'services': SERVICES}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-ssh-jump-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close(); app.close(); target.close(); jump.close(); tmp.cleanup()
