"""Read-only live status of a real machine over its SSH connection.

One product-owned command: no model, no user-supplied shell text. The names of
watched services are the only user input, and they are validated before they
reach the command. Nothing here moves a robot or changes the device.
"""
import logging
import math
import re
import threading
import time

from . import device_access

LOG = logging.getLogger(__name__)

MAX_SERVICES = 12
MAX_FAILED_UNITS = 20
COMMAND_TIMEOUT_SECONDS = 6
IDLE_CLOSE_SECONDS = 60          # a session nobody polled for this long is closed
SWEEP_SECONDS = 30               # how often idle sessions are looked for while any session is open
SERVICE_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,127}')
SERVICE_STATE = re.compile(r'[a-z-]{1,32}')
FAILED_UNITS = '--type=service --state=failed --no-legend --plain --no-pager'
BASE_COMMAND = (
    "echo '##host';hostname;"
    "echo '##uptime';cat /proc/uptime 2>/dev/null;"
    "echo '##load';cat /proc/loadavg 2>/dev/null;"
    "echo '##mem';grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null;"
    "echo '##disk';df -Pk / 2>/dev/null | tail -n 1;"
    "echo '##temp';cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null;"
    f"echo '##failed';systemctl list-units {FAILED_UNITS} 2>/dev/null | head -n {MAX_FAILED_UNITS};"
    # Without this marker an empty list would read as "nothing failed" on a device that has no usable systemd.
    f"echo '##systemd';systemctl list-units {FAILED_UNITS} >/dev/null 2>&1 && echo ok;"
)


def validate_services(services):
    if not isinstance(services, list):
        raise ValueError('请提供要关注的服务名列表')
    if len(services) > MAX_SERVICES:
        raise ValueError(f'关注的服务最多 {MAX_SERVICES} 个')
    for name in services:
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            raise ValueError('服务名只能包含字母、数字和 . _ @ : -，例如 nav-stack.service')
    return list(services)


def status_command(services):
    names = validate_services(services)
    watched = f"echo '##services';systemctl is-active -- {' '.join(names)} 2>/dev/null;" if names else ''
    return BASE_COMMAND + watched + "echo '##end'"


def _sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith('##'):
            current = sections.setdefault(line[2:].strip(), [])
        elif current is not None and line.strip():
            current.append(line.strip())
    return sections


def _number(value, cast=float):
    """A finite number, or None: the device's output is not trusted to be numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return cast(number) if math.isfinite(number) else None


def _memory(lines):
    values = {}
    for line in lines:
        fields = line.split()
        if len(fields) >= 2:
            values[fields[0].rstrip(':')] = _number(fields[1], int)
    total, available = values.get('MemTotal'), values.get('MemAvailable')
    if not total or available is None:
        return None
    return {'total_kb': total, 'available_kb': available, 'used_percent': round((total - available) * 100 / total)}


def _disk(lines):
    fields = lines[-1].split() if lines else []
    if len(fields) < 5:
        return None
    total, used, percent = _number(fields[1], int), _number(fields[2], int), _number(fields[4].rstrip('%'), int)
    if total is None or used is None or percent is None:
        return None
    return {'total_kb': total, 'used_kb': used, 'used_percent': percent}


def _temperature(lines):
    # Thermal zones report millidegrees; a few boards report whole degrees.
    readings = [value / 1000 if value >= 1000 else value for value in (_number(line) for line in lines) if value]
    plausible = [value for value in readings if 0 < value < 150]
    return round(max(plausible), 1) if plausible else None


def _services(names, states):
    return [{'name': name, 'state': states[index] if index < len(states) and SERVICE_STATE.fullmatch(states[index])
             else 'unknown'} for index, name in enumerate(names)]


def parse_status(text, services):
    """Fields of the status command. Anything the device did not report is None, never a made-up zero."""
    sections = _sections(text)

    def first(name):
        return (sections.get(name) or [''])[0]

    load = [_number(value) for value in first('load').split()[:3]]
    has_systemd = sections.get('systemd') == ['ok']
    return {
        'hostname': first('host')[:255] or None,
        'uptime_seconds': _number((first('uptime').split() or [''])[0], int),
        'load': load if len(load) == 3 and None not in load else None,
        'memory': _memory(sections.get('mem') or []),
        'disk_root': _disk(sections.get('disk') or []),
        'temperature_c': _temperature(sections.get('temp') or []),
        'failed_units': [line.split()[0][:128] for line in sections.get('failed') or []][:MAX_FAILED_UNITS]
                        if has_systemd else None,
        'services': _services(services, sections.get('services') or []),
    }


def _session_lost(connection):
    """The session broke without saying where. Through a jump host either hop may have dropped it."""
    if connection.get('jump'):
        return {'hop': 'connection', 'reason': 'unreachable', 'message': 'SSH 会话中断；请确认跳板机和设备都在线'}
    return {'hop': 'device', 'reason': 'unreachable', 'message': 'SSH 会话中断；请确认设备在线'}


def _endpoint(endpoint):
    return f"{endpoint['username']}@{endpoint['host']}:{endpoint['port']}"


def _view(connection, error, status=None, latency_ms=None):
    jump = connection.get('jump')
    return {'checked_at': time.time(), 'reachable': status is not None, 'latency_ms': latency_ms,
            'via_jump': bool(jump), 'target': _endpoint(connection), 'jump': _endpoint(jump) if jump else None,
            'status': status, 'error': error}


class StatusSessions:
    """One reusable SSH session per connection, so that polling does not log in every few seconds.

    While any session is open a sweeper closes those nobody polled for ``idle_seconds``.
    A cached session that has died is reopened once before the device is reported unreachable.
    """

    def __init__(self, idle_seconds=IDLE_CLOSE_SECONDS, sweep_seconds=SWEEP_SECONDS):
        self.idle_seconds, self.sweep_seconds = idle_seconds, sweep_seconds
        self._guard = threading.Lock()
        self._entries = {}
        self._sweeper = None
        self._closed = False

    def read(self, connection, secret):
        entry = self._claim(connection['id'])
        if entry is None:
            # Another poll is still connecting: show the last result instead of queueing up.
            last = self._entry(connection['id'])['view']
            return {**(last or {**_view(connection, None), 'reachable': None}), 'refreshing': True}
        try:
            entry['view'] = self._read(entry, connection, secret)
            entry['used'] = time.monotonic()
            return entry['view']
        finally:
            if entry['gone']:                         # forgotten or closed while this poll was running
                self._drop(entry)
            entry['lock'].release()
            self._ensure_sweeper()

    def _entry(self, key):
        with self._guard:
            if self._closed:
                raise ValueError('应用正在退出，无法读取设备状态')
            return self._entries.setdefault(key, {'lock': threading.Lock(), 'device': None, 'used': 0.0,
                                                  'view': None, 'gone': False})

    def _claim(self, key):
        """The locked entry of a connection, or None while another poll holds it."""
        while True:
            entry = self._entry(key)
            if not entry['lock'].acquire(blocking=False):
                return None
            if not entry['gone']:                     # forgotten between the lookup and the lock: take a fresh one
                return entry
            entry['lock'].release()

    def _read(self, entry, connection, secret):
        services = connection.get('watch_services') or []
        command = status_command(services)
        reused = entry['device'] is not None
        view = self._attempt(entry, connection, secret, command, services)
        if view is None and reused:                   # the cached session had died: once more with a fresh one
            view = self._attempt(entry, connection, secret, command, services)
        return view or _view(connection, _session_lost(connection))

    def _attempt(self, entry, connection, secret, command, services):
        """One status read. None means the SSH session died under us."""
        try:
            if entry['device'] is None:
                entry['device'] = device_access.SSHDevice(connection, secret)
            started = time.monotonic()
            # Unredacted: the output is this module's fixed fields, and replacing a short
            # password inside them (a digit, say) would corrupt every number.
            result = entry['device'].execute(command, COMMAND_TIMEOUT_SECONDS, redact=False)
            latency_ms = round((time.monotonic() - started) * 1000)
        except device_access.ConnectError as exc:
            return _view(connection, {'hop': exc.hop, 'reason': exc.reason, 'message': str(exc)})
        except Exception as exc:                      # paramiko / EOF / socket errors of a dead session
            LOG.warning('SSH status session of %s failed: %s', connection['id'], type(exc).__name__)
            self._drop(entry)
            return None
        if result['timed_out']:
            self._drop(entry)
            return _view(connection, {'hop': 'device', 'reason': 'timeout',
                                      'message': f'设备在 {COMMAND_TIMEOUT_SECONDS} 秒内没有返回状态'})
        return _view(connection, None, parse_status(result['stdout'], services), latency_ms)

    @staticmethod
    def _drop(entry):
        device, entry['device'] = entry['device'], None
        if device is not None:
            try:
                device.close()
            except Exception as exc:                  # closing a dead session must not hide the real error
                LOG.warning('closing an SSH status session failed: %s', type(exc).__name__)

    def open_connections(self):
        with self._guard:
            return sorted(key for key, entry in self._entries.items() if entry['device'] is not None)
        # Sessions of forgotten entries are not listed: their own poll closes them (see read).

    def forget(self, key):
        """Close and drop the session of a connection that no longer exists (e.g. it was replaced).

        Never waits: a poll that is still running closes its own session when it ends."""
        with self._guard:
            entry = self._entries.pop(key, None)
        if entry is not None:
            self._retire(entry)

    def _retire(self, entry, wait=False):
        entry['gone'] = True
        if entry['lock'].acquire(blocking=wait):
            try:
                self._drop(entry)
            finally:
                entry['lock'].release()

    def _ensure_sweeper(self):
        with self._guard:
            if self._closed or self._sweeper is not None:
                return
            self._sweeper = threading.Timer(self.sweep_seconds, self._sweep)
            self._sweeper.daemon = True
            self._sweeper.start()

    def _sweep(self):
        self.close_idle()
        with self._guard:
            self._sweeper = None
        if self.open_connections():
            self._ensure_sweeper()

    def close_idle(self):
        with self._guard:
            entries = list(self._entries.values())
        for entry in entries:
            if entry['lock'].acquire(blocking=False):
                try:
                    if entry['device'] is not None and time.monotonic() - entry['used'] >= self.idle_seconds:
                        self._drop(entry)
                finally:
                    entry['lock'].release()

    def close(self):
        with self._guard:
            self._closed = True
            if self._sweeper is not None:
                self._sweeper.cancel()
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._retire(entry, wait=True)
