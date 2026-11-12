"""Local-only UI fixture for issue #4: the desktop app talking to a TokenFans stand-in (never the paid service).

Usage: python tests/tokenfans_browser_fixture.py [config.json]
Prints {"url": <app>, "tokenfans": <stand-in>, "api_key", "email", "password"} -- all test-only values.
"""
import json
import os
import sys
import tempfile
import time

import tokenfans_fixture as tkf

if __name__ == '__main__':
    tokenfans = tkf.TokenFansFixture()
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    from brainos.server import Application, make_handler
    from test_product import Server
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    config = {'url': api.url, 'tokenfans': tokenfans.url, 'api_key': tkf.API_KEY, 'email': tkf.EMAIL, 'password': tkf.PASSWORD}
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w') as f:
            json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close()
        app.close()
        tokenfans.close()
        tmp.cleanup()
