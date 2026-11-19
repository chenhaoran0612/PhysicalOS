"""The runtime's own log file, rotated by size (``serve --log-file``).

The log used to be whatever the start script appended stdout and stderr to, without limit.
:class:`LogFile` points the process's descriptors 1 and 2 at the log file itself; when the file
has reached ``max_bytes`` it is renamed to ``<log>.1`` (older ones move to ``.2`` ... ``.keep``,
the oldest is removed) and a fresh file takes over the same descriptors, so no line is lost and
nothing else has to reopen anything. About ``(keep + 1) x max_bytes`` are used: the size is
looked at once per housekeeping period, so a file can pass the limit by what is written in one.

Only the log itself and ``<log>.1`` ... ``<log>.<keep>`` are ever renamed or removed, and only
while each of them is a regular file. :meth:`check` runs in the ``nx-housekeeping`` thread and
never raises.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_MAX_BYTES = 10 << 20
DEFAULT_KEEP = 3
MAX_KEEP = 20


def check_limits(max_bytes, keep) -> None:
    """``ValueError`` unless ``max_bytes`` is a positive integer and ``keep`` is 1 ... MAX_KEEP."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("日志大小上限须为正整数")
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= MAX_KEEP:
        raise ValueError(f"保留的旧日志个数须在 1 到 {MAX_KEEP} 之间")


class LogFile:
    def __init__(self, path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP,
                 fds: tuple[int, ...] = (1, 2)):
        check_limits(max_bytes, keep)
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep = keep
        self.fds = tuple(fds)
        self.rotations = 0
        self.error: str | None = None

    def _old(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def attach(self) -> None:
        """Send the descriptors to the log file (appending). Raises ``OSError`` if it cannot be
        opened -- for example when the path is a symbolic link."""
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{self.path} 不是普通文件")
            os.fchmod(fd, 0o600)          # also when the start script created it with the default mode
            for target in self.fds:
                os.dup2(fd, target)
        finally:
            os.close(fd)

    def check(self) -> bool:
        """Rotate if the log has reached the limit. Returns whether it rotated; never raises."""
        try:
            if os.fstat(self.fds[0]).st_size < self.max_bytes:
                return False
            self.rotate()
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        self.error = None
        return True

    def rotate(self) -> None:
        if not os.path.lexists(self.path):
            self.attach()                 # an earlier rotation got as far as the rename: just reopen
            return
        olds = [self._old(index) for index in range(1, self.keep + 1)]
        for path in (self.path, *olds):
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise OSError(f"{path} 不是普通文件，不轮转")
        try:
            os.unlink(olds[-1])
        except FileNotFoundError:
            pass
        for index in range(self.keep - 1, 0, -1):
            try:
                os.rename(self._old(index), self._old(index + 1))
            except FileNotFoundError:
                pass
        os.rename(self.path, olds[0])
        try:
            self.attach()
        except OSError:
            try:
                os.rename(olds[0], self.path)     # output still goes to that file: give it its name back
            except OSError:
                pass
            raise
        self.rotations += 1

    def describe(self) -> dict:
        return {"path": str(self.path), "max_bytes": self.max_bytes, "keep": self.keep,
                "rotations": self.rotations, "error": self.error}
