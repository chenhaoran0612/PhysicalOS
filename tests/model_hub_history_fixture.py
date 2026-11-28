"""Desktop backend for the browser check of issue #76 (a round's candidate chosen from the model's history).

The duck fixture of issue #63 (a made-up kit: shows nod, long, fall; the packages are 第 1 轮 · 第 4 次更新)
plus a made-up training dashboard on a loopback port: its ``/api/evolution`` (gzip when asked) lists evaluated
model files of ``nod``, and ``/files/...`` serves them. One of them (round 5, update 2) is served with other
content than its checksum says, so choosing it is refused. That one and round 4, update 8 are served after
1.5 s, so the browser check can redraw the dialog while a choice is under way.

Usage: python3 tests/model_hub_history_fixture.py [config.json]
"""
import gzip
import hashlib
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIXED = list(range(1_700_003_000, 1_700_003_010))
# (round, update, passes out of ten); 1-4 is the package that came with the app, 5-2 is served wrong.
FILES = [(1, 4, 10), (2, 2, 3), (3, 6, 7), (4, 8, 10), (5, 2, 10)] + [(6, n, n % 11) for n in range(1, 25)]
TAMPERED = (5, 2)
SLOW = {(4, 8): 1.5, (5, 2): 1.5}          # seconds: the page is redrawn while these are fetched


def dashboard(folder):
    """Start the made-up dashboard; returns its address."""
    events, served = [], {}
    for number, (round_number, update, passed) in enumerate(FILES):
        content = f'nod round {round_number} update {update} '.encode() * 64
        path = Path(folder) / f'round{round_number}-{update}.pt'
        path.write_bytes(content)
        url = f'/files/round{round_number}/nod/checkpoints/update_{update:06d}.pt?v={hashlib.sha256(content).hexdigest()}'
        served[url] = (path if (round_number, update) != TAMPERED else None, SLOW.get((round_number, update), 0))
        events.append({'id': f'round{round_number}:{update}', 'round_number': round_number, 'iteration': update, 'total_steps': 1024 * update,
                       'training_backend': 'cuda' if round_number % 2 else 'cpu', 'successes': passed, 'episodes': 10,
                       'seeds': [{'seed': s, 'success': True} for s in FIXED], 'report_mtime': 1795760000.0 + number * 600,
                       'checkpoint_url': url, 'duration': {'successes': 0, 'episodes': 10}})
    plain = json.dumps({'required_successes': 10, 'seeds': FIXED, 'tasks': [{'id': 'nod', 'title': '节目 nod', 'events': events}]}).encode()
    packed = gzip.compress(plain)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/evolution':
                if 'gzip' in self.headers.get('Accept-Encoding', ''):
                    return self.reply(packed, {'Content-Encoding': 'gzip'})
                return self.reply(plain)
            if self.path in served:
                path, wait = served[self.path]
                time.sleep(wait)
                return self.reply(path.read_bytes() if path else b'not the file the checksum is for')
            self.send_error(404)
            return None

        def reply(self, body, extra=None):
            self.send_response(200)
            for key, value in {'Content-Length': str(len(body)), **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):                          # no reverse lookup of 127.0.0.1 (slow on some Macs)
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = '127.0.0.1', self.server_address[1]

    server = Server(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{server.server_address[1]}'


def main():
    with tempfile.TemporaryDirectory(prefix='nx-dashboard-') as folder:
        os.environ['NEUXMIND_DUCK_DASHBOARD'] = dashboard(folder)
        import duck_browser_fixture
        duck_browser_fixture.main()


if __name__ == '__main__':
    main()
