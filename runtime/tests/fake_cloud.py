"""A stand-in cloud for the alert-report tests (issue #26).

It speaks HTTPS with a certificate made for the test run (a throw-away CA and a server certificate
signed by it, both generated with the ``openssl`` command into the test's temp directory -- no key
material lives in the repository) and records every request it receives, so a test can check what
left the device and what did not.
"""
from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPORT_PATH = "/api/v1/device-reports"
OPENSSL_SKIP_REASON = None if shutil.which("openssl") else "没有 openssl 命令，无法为测试生成证书"

_CA_CONFIG = """[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = NeuxMind Test CA
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""
_SERVER_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = 127.0.0.1
"""
_SERVER_EXTENSIONS = """basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def _openssl(directory: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True, timeout=120)


def make_certificates(directory: Path) -> dict:
    """``{"ca": ca.pem, "cert": server.pem, "key": server-key.pem}`` in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ca.cnf").write_text(_CA_CONFIG, encoding="utf-8")
    (directory / "server.cnf").write_text(_SERVER_CONFIG, encoding="utf-8")
    (directory / "server.ext").write_text(_SERVER_EXTENSIONS, encoding="utf-8")
    _openssl(directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", "ca.cnf",
             "-keyout", "ca-key.pem", "-out", "ca.pem")
    _openssl(directory, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", "server.cnf",
             "-keyout", "server-key.pem", "-out", "server.csr")
    _openssl(directory, "x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca-key.pem",
             "-CAcreateserial", "-days", "2", "-extfile", "server.ext", "-out", "server.pem")
    return {"ca": directory / "ca.pem", "cert": directory / "server.pem", "key": directory / "server-key.pem"}


class FakeCloud:
    """Records requests; ``mode`` chooses the answer.

    ``ok``         200, new ids under ``accepted``, ids seen before under ``duplicates``
    ``error``      500
    ``denied``     401
    ``lost``       stores the alerts like ``ok``, then drops the connection without answering
    ``hang``       holds the request until :meth:`release` (or the test ends)
    ``trickle``    answers 200 and then sends its body one byte every 0.1 s
    ``redirect``   307 to ``redirect_to``
    ``huge``       200 with a body far above what the device accepts
    ``garbage``    200 with a body that is not JSON
    """

    def __init__(self, certificates: dict | None):
        self.requests: list[dict] = []
        self.mode = "ok"
        self.redirect_to = ""
        self.stored: dict[str, dict] = {}
        self._gate = threading.Event()
        self._lock = threading.Lock()
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _answer(self, status: int, body: bytes, headers: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:                 # the device hung up (an answer above its limit): expected
                    pass
                self.close_connection = True

            def do_GET(self):
                with cloud._lock:
                    cloud.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers.items()),
                                           "body": b"", "json": None})
                self._answer(404, b'{"error": "not found", "code": "not_found"}')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                with cloud._lock:
                    cloud.requests.append({"method": "POST", "path": self.path,
                                           "headers": {k.lower(): v for k, v in self.headers.items()},
                                           "body": raw, "json": parsed})
                mode = cloud.mode
                if mode == "error":
                    return self._answer(500, b'{"error": "boom", "code": "internal_error"}')
                if mode == "denied":
                    return self._answer(401, b'{"error": "no", "code": "invalid_api_key"}')
                if mode == "redirect":
                    return self._answer(307, b"{}", {"Location": cloud.redirect_to + self.path})
                if mode == "huge":
                    return self._answer(200, b'{"accepted": ["' + b"x" * 1_000_000 + b'"]}')
                if mode == "garbage":
                    return self._answer(200, b"<html>not json</html>")
                if mode == "hang":
                    cloud._gate.wait(60)
                    self.close_connection = True
                    return None
                if mode == "trickle":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "60000")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for _ in range(600):        # one byte every 0.1 s: no single read ever times out
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            if cloud._gate.wait(0.1):
                                break
                    except OSError:
                        pass
                    return None
                alerts = parsed.get("alerts", []) if isinstance(parsed, dict) else []
                accepted, duplicates = [], []
                with cloud._lock:
                    for alert in alerts:
                        if alert["id"] in cloud.stored:
                            duplicates.append(alert["id"])
                        else:
                            cloud.stored[alert["id"]] = alert
                            accepted.append(alert["id"])
                if mode == "lost":
                    self.close_connection = True
                    self.connection.close()
                    return None
                return self._answer(200, json.dumps({"accepted": accepted, "duplicates": duplicates,
                                                     "rejected": []}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *_args: None     # refused handshakes and hang-ups are the tests' point
        self.scheme = "http"
        if certificates is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificates["cert"]), str(certificates["key"]))
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        self._thread = threading.Thread(target=self.server.serve_forever, name="fake-cloud", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.server.server_address[1]}"

    def reports(self) -> list[dict]:
        with self._lock:
            return [r["json"] for r in self.requests if r["method"] == "POST" and r["path"] == REPORT_PATH]

    def alerts(self) -> list[dict]:
        with self._lock:
            return list(self.stored.values())

    def release(self) -> None:
        self._gate.set()

    def close(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()
