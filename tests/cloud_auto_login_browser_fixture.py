"""Browser acceptance fixture for issue #36: an app sign-in signs into the cloud (the material library) too.

Serves the desktop app with the automatic cloud sign-in switched on, together with the real cloud app
(tests/cloud_fixture: local uvicorn, SQLite, ``NEUXMIND_ENV=test``). The cloud is the *default* cloud
address of this app, as the cloud on port 8800 is for the installed app.

Test controls (the page calls them; they do what time or an older version would have done):

* ``GET /fixture/auto-login?on=0|1`` — the switch. Off, a sign-in is one of the version before this one.
* ``GET /fixture/expire-cloud-sessions`` — every cloud session has lapsed.

Usage: python3 tests/cloud_auto_login_browser_fixture.py [config.json]
"""
import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

import cloud_fixture

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-cloud-auto-login-ui.json'
    cloud = cloud_fixture.CloudServer(models=())
    os.environ['NEUXMIND_DEFAULT_CLOUD_URL'] = cloud.url      # read when brainos.cloud_account is imported
    os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')
    from brainos.server import Application, make_handler
    from test_product import Server

    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name + '/app')
    app.cloud_auto_login = False

    class Handler(make_handler(app)):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/fixture/auto-login':
                app.cloud_auto_login = parse_qs(url.query).get('on') == ['1']
                return self.respond({'on': app.cloud_auto_login})
            if url.path == '/fixture/expire-cloud-sessions':
                from sqlalchemy import update
                from neuxmind_cloud.models import AuthSession
                with cloud.app.state.ctx.db.transaction() as s:
                    s.execute(update(AuthSession).values(expires_at=0))
                return self.respond({'expired': True})
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'cloud': cloud.url}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        cloud.stop()
        tmp.cleanup()
