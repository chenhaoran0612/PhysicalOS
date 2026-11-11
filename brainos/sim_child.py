"""Child-process entry for a simulated robot started by the desktop app.

It runs the edge runtime's own command line (``neuxmind_runtime serve``)
unchanged, plus one guard: the desktop keeps this process's stdin open, and
when it closes (the desktop exited, crashed or was killed) the runtime gets
SIGTERM, which its CLI treats as a clean shutdown (stop motion, close state,
no "abnormal shutdown" latch on the next start). So a simulated robot never
outlives the app that started it.

Usage (source tree)::

    python3 -m brainos.sim_child serve --adapter sim_quadruped --port 0 --state-dir DIR

Inside the packaged app the backend binary is started with ``--nx-sim-child``
followed by the same arguments (see ``desktop/launcher.py``).
"""
import os
import signal
import sys
import threading


def _parent_watch():
    try:
        # os.read on the raw fd, not sys.stdin.buffer.read: a daemon thread blocked inside the
        # BufferedReader holds its lock, and interpreter shutdown then aborts with "Fatal Python
        # error: _enter_buffered_busy" (exit -6) on every stop/restart (review Q6-09).
        fd = sys.stdin.fileno()
        while os.read(fd, 65536):
            pass
    except Exception:
        pass
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError:
        pass


def _terminate(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    signal.signal(signal.SIGTERM, _terminate)          # the runtime CLI installs the same handler
    from . import runtime_bridge                        # noqa: F401  (puts runtime + spec on sys.path)
    threading.Thread(target=_parent_watch, name='nx-parent-watch', daemon=True).start()
    from neuxmind_runtime.cli import main as runtime_main
    try:
        return runtime_main(argv)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
