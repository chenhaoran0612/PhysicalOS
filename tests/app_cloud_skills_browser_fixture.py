"""Browser fixture for issue #73: the create form offers what 「技能发布」 published for the brand and model.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/app_cloud_skills_browser_fixture.py [info.json]``, then
``node tests/app_cloud_skills_browser.cjs info.json [screenshot dir]``.

The console on a temporary state directory and ``skill_cloud_fixture.FakeSkillCloud`` as the cloud. The robots the test
adds have no device behind them: none of them is ever connected, as in the issue (an L2-U that is switched off). The
browser's workspace is signed in to the stand-in with ``GET /fixture/cloud-login`` and out with ``/fixture/cloud-logout``;
``GET /fixture/state`` answers which releases the stand-in holds. Nothing reaches a real server or a real machine.
"""
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

# No cloud of this computer is ever asked: the default cloud address is a port nothing listens on.
if 'NEUXMIND_DEFAULT_CLOUD_URL' not in os.environ:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        os.environ['NEUXMIND_DEFAULT_CLOUD_URL'] = 'http://127.0.0.1:%d' % probe.getsockname()[1]

from brainos.httpd import FastBindHTTPServer  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from skill_cloud_fixture import FakeSkillCloud  # noqa: E402


def main():
    tmp = tempfile.TemporaryDirectory(prefix='nx-issue73-ui-')
    cloud, app, api = FakeSkillCloud(), None, None
    try:
        app = Application(tmp.name + '/app')

        class Handler(make_handler(app)):
            def do_GET(self):
                what = urlparse(self.path).path
                if not what.startswith('/fixture/'):
                    return super().do_GET()
                user = self.user()
                if not user:
                    return None
                platform = app.workspace(user)
                if what == '/fixture/cloud-login':
                    cloud.sign_in(platform)
                elif what == '/fixture/cloud-logout':
                    platform.cloud._drop_token()
                elif what != '/fixture/state':
                    return self.respond({'error': 'unknown fixture request'}, 404)
                return self.respond({'releases': sorted('%s@%s %s' % (key[0], key[1], (held['release']['product'] or {}).get('model', '-'))
                                                         for key, held in cloud.releases.items())})

        api = FastBindHTTPServer(('127.0.0.1', 0), Handler)
        api.daemon_threads = True
        threading.Thread(target=api.serve_forever, daemon=True).start()
        info = {'url': f'http://127.0.0.1:{api.server_port}'}
        with open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(tmp.name, 'info.json'), 'w') as f:
            json.dump(info, f)
        print(info['url'], flush=True)

        def terminate(_signum, _frame):
            raise KeyboardInterrupt           # stopped with SIGTERM: clean up as on Ctrl-C

        signal.signal(signal.SIGTERM, terminate)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if api:
            api.shutdown()
            api.server_close()
        if app:
            app.close()
        cloud.close()
        tmp.cleanup()


if __name__ == '__main__':
    main()
