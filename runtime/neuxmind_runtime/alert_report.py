"""The device reports its alerts to the cloud by itself (issue #26, requirement R7-08).

Whether a console is connected or not, the runtime sends a report to the configured cloud every
five minutes: the alerts the cloud does not have yet, or an empty report that says "still here"
(the cloud calls a device offline after three missed reports). What could not be sent stays in the
alert book and goes out with the next report. The two alerts of the highest level the runtime can
judge today -- e-stop latched, stop not obeyed -- do not wait: they go out within seconds, at most
once per :data:`IMMEDIATE_MIN_GAP_S` per kind, and join the next five-minute report if that fails.

Nothing here can hold up anything else:

* The reporter has its own thread (``nx-alert-report``). It only *reads* the safety state; the stop
  path never calls it, takes none of its locks and waits for neither its disk nor its network.
* Every failure -- no network, a cloud that hangs or drips its answer, a disk that is full or does
  not answer, a bad configuration -- ends in "not sent yet" and a line in
  ``/v1/status.alert_report``. ``tick()`` never raises. One request has an overall deadline
  (:data:`TOTAL_TIMEOUT_FACTOR` x the socket timeout); while it runs the reporter does nothing else,
  so an alert raised in that time waits for it.
* A cloud that answers but does not take a report (HTTP 4xx / 5xx) still gets an empty report right
  after, so the device does not look offline. The device never gives up an alert on its own: only
  when that empty report is accepted -- the cloud takes reports from this device, so the refusal was
  about the alerts -- does it count. The alerts then go one per report, the highest level first;
  one that is refused on its own is left out until the next five-minute report, and from the third
  time on for :data:`HOLD_S` (it stays in the book, unsent, and is tried again alone) while the
  others go on. A wrong address or an old cloud (every report refused, the empty one too) changes
  nothing but "not sent yet".

What leaves the device, and where to:

* Only to the address in the configuration file, only over HTTPS with the certificate and host
  name verified (a private CA through ``ca_file``). No redirect is followed and no proxy from the
  environment is used, so the key goes to that host and nowhere else.
* The request carries the cloud key (``Authorization: Bearer``) and the report: this device's
  identity from its manifest and the alerts. Not the gateway token, not the state directory.
* Without a configuration file the device sends nothing at all and works fully offline; alerts
  are still recorded and are sent once an address is configured.

Configuration (``serve --cloud-config FILE``): a JSON file readable by the runtime's user only
(mode 0600), ``{"url": "https://...", "key": "<API key with scope alerts:report>", "ca_file": "..."}``.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, alerts

REPORT_SCHEMA = "neuxmind.device_report.v1"
REPORT_PATH = "/api/v1/device-reports"
INTERVAL_S = 300.0                  # R7-08: every five minutes
IMMEDIATE_MIN_GAP_S = 30.0          # the same kind of highest-level alert goes out at once at most this often
POLL_S = 1.0                        # how often the safety state is looked at
TIMEOUT_S = 10.0                    # connect and every socket read / write
TOTAL_TIMEOUT_FACTOR = 3            # one whole request may take this many socket timeouts (30 s)
STRIKES_BEFORE_LONG_HOLD = 3        # refusals of one alert on its own before it is left out for HOLD_S
MAX_REFUSED_PER_ROUND = 3           # alerts refused on their own in a row before the rest waits for the next report
HOLD_S = 3600.0                     # an alert refused on its own again and again is left out this long
MAX_SUSPECTS = 500                  # alerts remembered as refused on their own
QUIET_STATUSES = (401, 403, 407, 408, 429)    # no follow-up request: the key or the pace is the problem
MIN_KEY_CHARS, MAX_KEY_CHARS = 16, 500
MAX_ALERTS_PER_REPORT = 100
MAX_REPORT_BYTES = 100_000          # alerts in one report (the cloud's default body limit is 1 MB)
MAX_ANSWER_BYTES = 65_536
MAX_CONFIG_BYTES = 16_384
MAX_URL = 500
CLOSE_JOIN_S = 1.0
NOT_CONFIGURED = "未配置云平台地址（serve --cloud-config）：设备不向外发送任何数据，告警只记在设备上"

_PATH = re.compile(r"[A-Za-z0-9._~/%-]*")
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*\.?")                  # a DNS name or an IPv4 address
_CONFIG_KEYS = ("url", "key", "ca_file")
_CONFIG_KEYS += ("link",)       # issue #43: "link": false keeps cloud_link.py off; alert reports ignore it


class ConfigError(ValueError):
    """The cloud address, key or configuration file cannot be used. Never contains the key."""


class SendError(Exception):
    """A report did not reach the cloud, or its answer could not be used. Never contains the key.
    ``status`` is the HTTP status when the cloud answered with one that is not 200."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def check_url(value) -> str:
    """The cloud address in canonical form. HTTPS only: the report carries the cloud key."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        raise ConfigError("云平台地址须为 https:// 开头的地址（不超过 500 个字符）")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConfigError("云平台地址不能包含空白或控制字符")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ConfigError("云平台地址无效（https://主机[:端口]）") from None
    if parts.scheme.lower() != "https":
        raise ConfigError("云平台地址只接受 https://：上报带着云平台的 key，不走明文")
    if not host or not _is_host(host):
        raise ConfigError("云平台地址的主机名无效（https://主机[:端口]）")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("云平台地址的端口无效")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("云平台地址不能包含用户名、密码、参数或片段")
    path = parts.path.rstrip("/")
    if not _PATH.fullmatch(path):
        raise ConfigError("云平台地址的路径里有不允许的字符")
    netloc = f"[{host}]" if ":" in host else host
    return f"https://{netloc}{'' if port is None else f':{port}'}{path}"


def _is_host(host: str) -> bool:
    if ":" not in host:
        return bool(_HOST.fullmatch(host))
    try:                                                   # an IPv6 literal, without a zone
        return "%" not in host and bool(ipaddress.IPv6Address(host))
    except ValueError:
        return False


def check_key(value) -> str:
    if (not isinstance(value, str) or not MIN_KEY_CHARS <= len(value) <= MAX_KEY_CHARS
            or any(not 0x21 <= ord(ch) <= 0x7E for ch in value)):
        raise ConfigError(f"云平台 key 无效：须为 {MIN_KEY_CHARS}–{MAX_KEY_CHARS} 个不含空白的 ASCII 字符")
    return value


def check_ca_file(value) -> str:
    """The CA decides who the key is sent to: a file only its owner (the runtime's user or root) can change."""
    try:
        info = os.stat(value) if isinstance(value, str) else None
    except OSError:
        info = None
    if info is None or not stat.S_ISREG(info.st_mode):
        raise ConfigError("ca_file 须为一个存在的 CA 证书文件（PEM）的路径")
    if info.st_uid not in (os.geteuid(), 0) or info.st_mode & 0o022:
        raise ConfigError("ca_file 须属于运行管理层的用户或 root，并且别人不可写（chmod go-w）")
    return value


def _positive(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{what} 须为正数（秒）")
    return float(value)


@dataclass(frozen=True)
class ReportConfig:
    """Where and how this device reports. ``repr`` never shows the key."""

    url: str
    key: str = field(repr=False)
    ca_file: str | None = None
    interval_s: float = INTERVAL_S
    timeout_s: float = TIMEOUT_S
    link: bool = True               # issue #43: whether cloud_link.py may use this configuration

    def __post_init__(self):
        object.__setattr__(self, "url", check_url(self.url))
        check_key(self.key)
        if self.ca_file is not None:
            check_ca_file(self.ca_file)
        object.__setattr__(self, "interval_s", _positive(self.interval_s, "上报间隔"))
        object.__setattr__(self, "timeout_s", _positive(self.timeout_s, "上报超时"))


def load_config(path: str | Path) -> ReportConfig:
    """Read ``serve --cloud-config``. The file holds the cloud key, so it must be a regular file
    (not a link) owned by the runtime's user or root and readable by nobody else."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(f"读不到上报配置文件：{exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("上报配置须为普通文件")
        if info.st_uid not in (os.geteuid(), 0):
            raise ConfigError("上报配置文件的属主须为运行管理层的用户")
        if info.st_mode & 0o077:
            raise ConfigError("上报配置文件权限过宽（里面有云平台的 key）：请 chmod 600")
        if info.st_size > MAX_CONFIG_BYTES:
            raise ConfigError("上报配置文件过大")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ConfigError("上报配置不是 JSON") from None
    if not isinstance(data, dict):
        raise ConfigError('上报配置须为对象：{"url": "https://…", "key": "…"}')
    unknown = sorted(k for k in data if k not in _CONFIG_KEYS)
    if unknown:
        shown = "、".join(k if re.fullmatch(r"[A-Za-z_]{1,30}", k) else "…" for k in unknown[:5])
        raise ConfigError(f"上报配置里有未知的项：{shown}（只认 url、key、ca_file）")
    if "link" in data:              # issue #43
        if not isinstance(data["link"], bool):
            raise ConfigError("上报配置里的 link 须为 true 或 false")
        return ReportConfig(url=data.get("url"), key=data.get("key"), ca_file=data.get("ca_file"), link=data["link"])
    return ReportConfig(url=data.get("url"), key=data.get("key"), ca_file=data.get("ca_file"))


class HttpsTransport:
    """One report = one HTTPS request to the configured host. Nothing else is ever contacted."""

    def __init__(self, config: ReportConfig):
        parts = urlsplit(config.url)
        self._host, self._port = parts.hostname, parts.port or 443
        self._path = parts.path + REPORT_PATH
        self._timeout = config.timeout_s
        self._deadline = config.timeout_s * TOTAL_TIMEOUT_FACTOR
        # Verifies the certificate chain and the host name; there is no switch to turn that off.
        # Built by hand: ssl.create_default_context() would honour SSLKEYLOGFILE and write the
        # session secrets (and with them the key) to a file.
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        if config.ca_file is not None:
            self._context.load_verify_locations(cafile=config.ca_file)
        else:
            self._context.load_default_certs()
        self._headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                         "Authorization": "Bearer " + config.key, "Connection": "close",
                         "User-Agent": f"neuxmind-runtime/{__version__} alert-report"}

    def send(self, report: dict) -> dict:
        try:
            body = json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise SendError(f"上报内容无法编码：{type(exc).__name__}") from None
        # http.client frames the request and the answer: no redirects, no proxy from the environment.
        # The connection is made here, so that the handshake is under the deadline as well.
        conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout, context=self._context)
        live: list = []                        # the socket in use, for the watchdog
        expired = threading.Event()

        def give_up() -> None:                 # the socket timeout bounds one read; this bounds the request
            expired.set()
            _hang_up(live)
        watchdog = threading.Timer(self._deadline, give_up)
        watchdog.daemon = True
        try:
            watchdog.start()
            conn.sock = self._connect(live)
            conn.request("POST", self._path, body=body, headers=self._headers)
            response = conn.getresponse()
            status = response.status
            data = response.read(MAX_ANSWER_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:   # TLS, time-outs, resets
            if expired.is_set():
                raise SendError(f"云平台在 {self._deadline:g} 秒内没有答复完") from None
            raise SendError(f"连不上云平台：{type(exc).__name__}: {str(exc)[:200]}") from None
        finally:
            watchdog.cancel()
            _hang_up(live)
            conn.close()
        if expired.is_set():
            raise SendError(f"云平台在 {self._deadline:g} 秒内没有答复完")
        if status != 200:
            raise SendError(f"云平台答复 {status}（{_error_code(data)}）", status=status)
        if len(data) > MAX_ANSWER_BYTES:
            raise SendError("云平台的答复过大")
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            raise SendError("云平台的答复不是 JSON") from None

    def _connect(self, live: list):
        """TCP, then TLS with the certificate and the host name checked. ``live`` always names the
        socket that holds the connection, so the watchdog can end a handshake that drags on.
        (Looking up the name and the TCP connect are the system's: each address gets the socket timeout.)"""
        raw = socket.create_connection((self._host, self._port), self._timeout)
        live.append(raw)
        tls = self._context.wrap_socket(raw, server_hostname=self._host, do_handshake_on_connect=False)
        live[0] = tls                          # wrapping moved the descriptor into tls
        tls.do_handshake()
        return tls


def _hang_up(live: list) -> None:
    """End the connection whose socket is in ``live``; a thread blocked on it wakes with an error.
    Called by the watchdog's thread, and once more when the request is over."""
    sock = live[0] if live else None
    if sock is None:
        return
    for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            action()
        except (OSError, ValueError):
            pass


def _error_code(data: bytes) -> str:
    """The ``code`` of an error answer, if it has one that is safe to show."""
    try:
        code = json.loads(data).get("code")
    except (ValueError, AttributeError):
        code = None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code) else "无说明"


def _ids(value) -> set | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return set(value)


def read_answer(answer, ours: set) -> tuple[set, set]:
    """``(sent, rejected)`` among ``ours`` from the cloud's answer; ``SendError`` if it is not one.
    Only ids that were in this report count: the cloud cannot settle anything else."""
    if not isinstance(answer, dict):
        raise SendError("云平台的答复格式不对")
    accepted, duplicates = _ids(answer.get("accepted")), _ids(answer.get("duplicates", []))
    rejected = answer.get("rejected", [])
    if accepted is None or duplicates is None or not isinstance(rejected, list):
        raise SendError("云平台的答复格式不对")
    sent = (accepted | duplicates) & ours
    refused = {item["id"] for item in rejected if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return sent, (refused & ours) - sent


class AlertReporter:
    """Watches, records and reports. ``tick()`` is one round; ``start()`` runs it on a thread.

    ``clock`` (monotonic seconds) paces the reports and ``wall`` stamps them; tests pass their own.
    ``transport`` replaces the HTTPS client (tests). With ``config=None`` nothing is ever sent.
    """

    def __init__(self, runtime, config: ReportConfig | None = None, *, clock=time.monotonic, wall=time.time,
                 transport=None, disabled_reason: str | None = None, poll_s: float = POLL_S,
                 capacity: int = alerts.CAPACITY):
        self.config = config
        self._clock, self._wall, self._poll_s = clock, wall, poll_s
        self._transport, self._reason = None, None
        if config is None:
            self._reason = disabled_reason or NOT_CONFIGURED
        elif alerts.device_problem(runtime.manifest) is not None:
            self._reason = alerts.device_problem(runtime.manifest)
        elif transport is not None:
            self._transport = transport
        else:
            try:
                self._transport = HttpsTransport(config)
            except Exception as exc:                       # e.g. a CA file that is not a certificate
                text = str(exc).replace(config.ca_file or "\0", "ca_file")[:200]
                self._reason = f"上报没有开启：{type(exc).__name__}: {text}"
        self._device = alerts.device_view(runtime.manifest)
        self._book = alerts.AlertBook(Path(runtime.state_dir) / alerts.BOOK_NAME, capacity, clock)
        self._watch = alerts.LatchWatch(runtime, self._book, self._emit)
        self._lock = threading.Lock()                      # the little state raise_alert() shares with tick()
        self._urgent: set[str] = set()
        self._last_immediate: dict[str, float] = {}
        self._next_at: float | None = None
        self._drain = False                                # more is waiting and the cloud is taking it
        self._heartbeat = False                            # the last report was refused: say "still here"
        self._refused: tuple = ()                          # ... and these are the alerts it carried (4xx)
        self._refused_run = 0                              # alerts refused on their own since the last success
        self._narrow = False                               # one alert per report, to find the one not taken
        self._suspects: dict[str, dict] = {}               # alerts refused on their own: {"strikes", "until"}
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self.reports_sent = 0
        self.consecutive_failures = 0
        self.last_attempt_at: float | None = None
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self.faults = 0

    # ------------------------------------------------------------ alerts in
    def _emit(self, kind, level, reason, advice, detail=None, ts=None) -> dict:
        alert = alerts.make_alert(kind, level, reason, advice, self._device,
                                  ts=self._wall() if ts is None else ts, detail=detail)
        self._book.add(alert)                              # memory only: the reporter's thread writes it
        if level == alerts.CRITICAL:
            with self._lock:
                self._urgent.add(kind)
        return alert

    def raise_alert(self, kind: str, level: str, reason: str, advice: str, detail: dict | None = None) -> dict:
        """Record an alert; it is reported with the next report (at once for ``critical``).
        Any thread may call it: it works on memory and never waits for the disk or the network.
        Still not for the stop path, which calls nothing it does not need."""
        return self._emit(kind, level, reason, advice, detail)

    # ------------------------------------------------------------ one round
    def tick(self) -> None:
        """One round: open the book (the first time), look at the safety state, write behind, report
        if a report is due. Each step stands on its own -- one that fails never keeps the next from
        running -- and nothing is raised."""
        if self._closing.is_set():
            return
        for step in (self._book.open, self._watch.poll, self._book.sync, self._report_if_due):
            try:
                step()
            except Exception as exc:
                self._fault(exc)

    def _fault(self, exc: Exception) -> None:
        self.faults += 1
        self.last_error = self._short(exc)

    def _short(self, exc: Exception) -> str:
        text = str(exc) if isinstance(exc, SendError) else f"{type(exc).__name__}: {exc}"
        if self.config is not None:
            text = text.replace(self.config.key, "***")
        return text[:300]

    def _report_if_due(self) -> None:
        if self._transport is None:
            return
        now = self._clock()
        trigger = None
        if self._next_at is None:
            trigger = "start"
        elif now >= self._next_at:
            trigger = "interval"
        with self._lock:
            if trigger is not None:
                self._next_at = now + self.config.interval_s
                self._urgent.clear()                       # everything unsent goes with this report
            else:
                ready = {kind for kind in self._urgent
                         if now - self._last_immediate.get(kind, -math.inf) >= IMMEDIATE_MIN_GAP_S}
                if ready:
                    trigger = "immediate"
                    for kind in ready:
                        self._last_immediate[kind] = now
                    self._urgent -= ready                  # one try; after a failure the 5-minute report has it
        if trigger is None and self._heartbeat:
            trigger = "heartbeat"
        elif trigger is None and self._drain:
            trigger = "backlog"
        if trigger is not None:
            self._send(trigger)

    def _next_batch(self) -> list[dict]:
        """The alerts of the next report. Alerts the cloud has refused on their own before never ride
        with others (they would get the whole report refused again): they go one at a time, alone,
        and only when nothing else is waiting."""
        limit = 1 if self._narrow else MAX_ALERTS_PER_REPORT
        batch = self._book.unsent(limit, MAX_REPORT_BYTES, frozenset(self._suspects))
        return batch or self._book.unsent(1, MAX_REPORT_BYTES, self._held())

    def _send(self, trigger: str) -> None:
        heartbeat = trigger == "heartbeat"
        self._heartbeat = False
        if not heartbeat:
            self._drain = False
            self._refused = ()                             # a verdict that was never asked for is forgotten
        if trigger in ("start", "interval"):
            self._refused_run = 0
        batch: list[dict] = []
        try:
            if not heartbeat:
                batch = self._next_batch()
            report = {"schema": REPORT_SCHEMA, "device": {**self._device, "runtime_version": __version__},
                      "sent_at": self._wall(), "interval_s": self.config.interval_s, "trigger": trigger,
                      "alerts": batch, "pending": self._book.counts()[alerts.UNSENT] - len(batch)}
            self.last_attempt_at = report["sent_at"]
            sent, rejected = read_answer(self._transport.send(report), {a["id"] for a in batch})
        except Exception as exc:
            self._failed(exc, batch, heartbeat)
            return
        self.reports_sent += 1
        self.last_success_at = self._wall()
        if heartbeat:
            self._refusal_confirmed()                      # the cloud takes reports: it was about the alerts
            return                                         # "still here"; the alerts and their error stand
        self._book.settle(sent, rejected)
        self._suspects = {i: s for i, s in self._suspects.items() if i not in sent and i not in rejected}
        self.consecutive_failures = 0
        self.last_error = None
        self._refused_run = 0
        waiting = self._book.unsent_count(frozenset(self._suspects))
        if waiting == 0:
            self._narrow = False                           # the queue is through: whole reports again
        # A backlog follows at once, as long as the cloud keeps taking it. An alert that was left out
        # and is due again counts: with the others through, it gets its turn, alone -- alerts that keep
        # coming never push it back for ever.
        self._drain = bool(sent or rejected) and self._book.unsent_count(self._held()) > 0
        self._book.sync()

    def _failed(self, exc: Exception, batch: list[dict], heartbeat: bool) -> None:
        self.consecutive_failures += 1
        self.last_error = self._short(exc)
        if heartbeat:
            self._refused = ()                             # no word on whether the cloud takes reports at all
            return
        status = getattr(exc, "status", None)
        if not batch or not isinstance(status, int) or status < 400 or status in QUIET_STATUSES:
            return                                         # not reached, or a follow-up would fare no better
        self._heartbeat = True                             # the cloud is there: tell it the device is, too
        if status < 500:                                   # 5xx is its failure, not this batch's
            self._refused = tuple(a["id"] for a in batch)

    def _held(self) -> frozenset:
        """Alerts left out of the reports for now (each refused on its own; see ``_refusal_confirmed``)."""
        now = self._clock()
        return frozenset(i for i, suspect in self._suspects.items() if suspect["until"] > now)

    def _refusal_confirmed(self) -> None:
        """The report before this accepted, alert-less one was refused with a 4xx: the cloud takes
        reports from this device, so the refusal was about those alerts. The device never gives one
        up for that.

        Several alerts: from now on they go one per report, the highest level first, until the queue
        is through -- the one the cloud will not take is found within a few reports and the others
        are not kept waiting. One alert on its own: it is left out until the next five-minute
        report, and from its ``STRIKES_BEFORE_LONG_HOLD``-th refusal on for ``HOLD_S`` at a time; it
        stays in the book, unsent. After ``MAX_REFUSED_PER_ROUND`` refusals in a row the rest waits
        for the next five-minute report: a cloud that takes no alert at all is not asked every second."""
        refused, self._refused = self._refused, ()
        if not refused:
            return
        if len(refused) > 1:
            self._narrow = True
        else:
            strikes = self._suspects.get(refused[0], {}).get("strikes", 0) + 1
            patient = strikes >= STRIKES_BEFORE_LONG_HOLD or self._next_at is None
            until = self._clock() + HOLD_S if patient else self._next_at
            kept = self._suspects if len(self._suspects) < MAX_SUSPECTS else {}     # a bound, never reached in practice
            self._suspects = {**kept, refused[0]: {"strikes": strikes, "until": until}}
            self._refused_run += 1
        self._drain = (self._refused_run < MAX_REFUSED_PER_ROUND
                       and self._book.unsent_count(frozenset(self._suspects)) > 0)

    # ------------------------------------------------------------ thread
    def start(self) -> "AlertReporter":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="nx-alert-report", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._closing.wait(self._poll_s):
            self.tick()

    def close(self) -> None:
        """Stop reporting. Never raises and never waits long: a request in flight gets ``CLOSE_JOIN_S``,
        a disk call that does not return gets ``alerts.CLOSE_WAIT_S``, then both are left behind."""
        self._closing.set()
        thread, self._thread = self._thread, None
        try:
            if thread is not None and thread is not threading.current_thread():
                thread.join(CLOSE_JOIN_S)
            self._book.close()
        except Exception as exc:
            self._fault(exc)

    # ------------------------------------------------------------ status
    def _held_count(self) -> int:
        """Unsent alerts that are being left out of the reports right now."""
        held = self._held()
        return self._book.unsent_count() - self._book.unsent_count(held) if held else 0

    def describe(self) -> dict:
        """``/v1/status.alert_report``: cached values only -- no lock, no disk, no network, no key."""
        config = self.config
        enabled = self._transport is not None
        return {"enabled": enabled, "reason": None if enabled else self._reason,
                "url": config.url if config is not None else None,
                "interval_s": config.interval_s if config is not None else INTERVAL_S,
                **self._book.counts(), "held": self._held_count(), "reports_sent": self.reports_sent,
                "last_attempt_at": self.last_attempt_at, "last_success_at": self.last_success_at,
                "last_error": self.last_error, "consecutive_failures": self.consecutive_failures,
                "storage": self._book.storage, "storage_note": self._book.note}


def for_serve(runtime, config_path: str | Path | None) -> AlertReporter:
    """The reporter of a served runtime (not started yet). A missing or unusable configuration file
    never stops the runtime: reporting is off, ``describe()["reason"]`` says why."""
    config, reason = None, None
    if config_path is not None:
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            reason = f"上报配置不能用，上报没有开启：{exc}"
    reporter = AlertReporter(runtime, config, disabled_reason=reason)
    runtime.alert_report = reporter
    return reporter
