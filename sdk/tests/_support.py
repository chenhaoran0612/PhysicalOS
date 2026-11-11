"""Shared fixtures for SDK integration tests.

Everything here is real: an ``EdgeRuntime`` with a simulated body served by
the runtime's own ``GatewayServer`` on a random port, and the cloud FastAPI
app served by uvicorn on a random port. Only the robot bodies (simulated) and
the cloud model upstream (the cloud's EchoProvider test double) are stand-ins.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

SDK_DIR = Path(__file__).resolve().parents[1]
REPO = SDK_DIR.parent
for path in (SDK_DIR, REPO / "runtime", REPO / "spec", REPO / "cloud"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import neuxmind_runtime  # noqa: E402,F401  (bootstraps neuxmind_spec)
from neuxmind_runtime.adapters.sim_arm import SimArm  # noqa: E402
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped  # noqa: E402
from neuxmind_runtime.core import EdgeRuntime  # noqa: E402
from neuxmind_runtime.gateway import GatewayServer  # noqa: E402
from neuxmind_runtime.models import ModelConfig  # noqa: E402

from neuxmind import cli  # noqa: E402

RUNTIME_EXAMPLES = REPO / "runtime" / "examples" / "skills"
OPERATOR_EMAIL = "ops@sdk.test"
PASSWORD = "sdk-test-password-123"


def _rmtree(path) -> None:
    """rmtree that also works for read-only trees (installed models are 0555/0444, non-root too)."""
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


class RuntimeServer:
    """A simulated robot behind the real HTTP gateway (random port)."""

    def __init__(self, body: str = "quadruped", time_scale: float = 20.0, *, models: bool = False):
        """``models=True``: the runtime-native ``model.install`` (``--models-dir``), with
        plain http allowed for 127.0.0.1 only (the runtime's dev/test switch) so that
        :class:`PackageServer` can serve packages."""
        self.token = "sdk-test-token-" + os.urandom(12).hex()
        self._tmp = tempfile.mkdtemp(prefix="nx-sdk-rt-")
        self.adapter = SimQuadruped(time_scale=time_scale) if body == "quadruped" else SimArm(time_scale=time_scale)
        self.models_dir = Path(self._tmp) / "models" if models else None
        config = (ModelConfig(models_dir=str(self.models_dir), runtimes=("onnxruntime",),
                              allow_http_hosts=("127.0.0.1",), progress_interval_s=0.05) if models else None)
        self.runtime = EdgeRuntime(self.adapter, str(Path(self._tmp) / "state"), models=config)
        self.server = GatewayServer(self.runtime, self.token, port=0).start()
        self.url = self.server.url

    def close(self) -> None:
        self.server.close()
        self.runtime.runner.stop_all("test teardown")
        self.runtime.supervisor.halt_motion("test teardown")
        self.runtime.close()
        _rmtree(self._tmp)


class PackageServer:
    """A local HTTP server for model packages (counts GETs per path)."""

    def __init__(self):
        import functools
        import http.server

        self.dir = Path(tempfile.mkdtemp(prefix="nx-sdk-pkgs-"))
        self.hits: dict[str, int] = {}
        self.slow: dict[str, float] = {}           # path → seconds to sleep per 64 KiB chunk
        hits, slow = self.hits, self.slow

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                hits[self.path] = hits.get(self.path, 0) + 1
                super().do_GET()

            def copyfile(self, source, outputfile):
                delay = slow.get(self.path)
                if not delay:
                    return super().copyfile(source, outputfile)
                while True:                        # a slow link: the runtime reports progress meanwhile
                    chunk = source.read(65536)
                    if not chunk:
                        return
                    outputfile.write(chunk)
                    outputfile.flush()
                    time.sleep(delay)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                     functools.partial(Handler, directory=str(self.dir)))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def add(self, name: str, size: int = 200_000, slow: float = 0.0) -> tuple[str, str]:
        """Write a random package file → (url, sha256); ``slow``: seconds per 64 KiB chunk."""
        import hashlib

        data = os.urandom(size)
        (self.dir / name).write_bytes(data)
        if slow:
            self.slow["/" + name] = slow
        return f"{self.url}/{name}", hashlib.sha256(data).hexdigest()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


class CloudServer:
    """The cloud app (test env, operator e-mail configured) served by uvicorn."""

    def __init__(self):
        import uvicorn
        from neuxmind_cloud.app import create_app
        from neuxmind_cloud.config import Settings

        self._tmp = tempfile.mkdtemp(prefix="nx-sdk-cloud-")
        settings = Settings(env="test", database_url=f"sqlite:///{self._tmp}/cloud.sqlite3",
                            operator_emails=[OPERATOR_EMAIL])
        self.app = create_app(settings)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning",
                                                    lifespan="off"))
        self.server.install_signal_handlers = lambda: None   # older uvicorn; harmless otherwise
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("cloud server did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.app.state.ctx.db.engine.dispose()
        shutil.rmtree(self._tmp, ignore_errors=True)


class CliHome:
    """A temporary NEUXMIND_HOME and an in-process ``nx`` runner."""

    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="nx-sdk-home-"))

    def run(self, *argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"NEUXMIND_HOME": str(self.path)}))
            for key in ("NEUXMIND_API_KEY", "NEUXMIND_CLOUD_URL", "NEUXMIND_PASSWORD", "NEUXMIND_ROBOT_TOKEN"):
                os.environ.pop(key, None)      # restored when patch.dict exits
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def json(self, *argv: str, expect: int = 0) -> dict:
        code, out, err = self.run(*argv, "--json")
        if code != expect:
            raise AssertionError(f"nx {' '.join(argv)} → {code}（预期 {expect}）\nstdout:{out}\nstderr:{err}")
        return json.loads(out)

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def make_skill(root: Path, skill_id: str, code: str, requires=("system.health",), parameters=(),
               timeout_s: float = 60, optional=()) -> Path:
    """Write a small skill directory for tests."""
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    meta = {"schema": "skill.package.v1", "id": skill_id, "version": "0.1.0", "entry": "skill.py:run",
            "requires": [{"id": r} for r in requires], "optional": [{"id": o} for o in optional],
            "parameters": list(parameters), "timeout_s": timeout_s}
    (directory / "skill.json").write_text(json.dumps(meta), encoding="utf-8")
    (directory / "skill.py").write_text(code, encoding="utf-8")
    return directory


SLEEPER = """
def run(ctx):
    ctx.call("base.velocity", vx=0.1, wz=0.0)
    while True:
        ctx.sleep(0.5)
        ctx.call("base.velocity", vx=0.1, wz=0.0)
"""


class CuttingProxy:
    """A TCP proxy in front of a test server that **drops the connection in the
    middle of an SSE response**, the way a broken network would: every other
    response is forwarded untouched; for a ``text/event-stream`` response it
    forwards the headers plus ``cut_after`` body bytes and then closes both
    sides (the server sees a client disconnect, the client an unexpected EOF).
    """

    def __init__(self, target_port: int, cut_after: int = 200):
        import socket

        self.target_port = target_port
        self.cut_after = cut_after
        self.cuts = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        import socket

        while not self._closed:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self.target_port))
            threading.Thread(target=self._pump_up, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pump_down, args=(upstream, client), daemon=True).start()

    @staticmethod
    def _shut(*socks) -> None:
        import socket

        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _pump_up(self, client, upstream) -> None:
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass

    def _pump_down(self, upstream, client) -> None:
        head, sse, sent = b"", None, 0
        try:
            while True:
                data = upstream.recv(65536)
                if not data:
                    break
                if sse is None:
                    head += data
                    end = head.find(b"\r\n\r\n")
                    if end < 0:
                        continue
                    sse = b"text/event-stream" in head[:end].lower()
                    data, head = head, b""
                    if sse:
                        client.sendall(data[:end + 4])
                        data = data[end + 4:]
                if sse:
                    room = self.cut_after - sent
                    if len(data) >= room:
                        client.sendall(data[:room])
                        self.cuts += 1
                        break
                    sent += len(data)
                client.sendall(data)
        except OSError:
            pass
        finally:
            self._shut(client, upstream)

    def close(self) -> None:
        self._closed = True
        self._shut(self._sock)
