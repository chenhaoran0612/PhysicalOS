"""Read-only live status of a real machine over its SSH connection.

Product-owned commands only: no model, no user-supplied shell text. The names of
watched services are the only user input, and they are validated before they
reach the command. Nothing here moves a robot or changes the device.

A basic read is one quick command (host, load, memory, disk, temperature, watched
services). A full read (issue #8) adds two more over the same session: the running
software (``device_software``) and the hardware with its drivers, which is the
hardware initialisation's own scan (``hardware_scan``), shown here without being stored.
"""
import logging
import math
import re
import threading
import time

from . import device_access, device_software, hardware_init, hardware_scan

LOG = logging.getLogger(__name__)

MAX_SERVICES = 12
MAX_FAILED_UNITS = 20
COMMAND_TIMEOUT_SECONDS = 6
REFRESH_SECONDS = 60             # how often the status page reads (issue #8: once a minute)
READ_BUDGET_SECONDS = 50         # the software and hardware reads of one full status end within this, before the next is due
IDLE_CLOSE_SECONDS = 150         # a session nobody polled for this long is closed: it outlives two missed reads
CACHE_SECONDS = 5 * REFRESH_SECONDS   # a quick read shows the hardware and software of the last full read no longer than this
SWEEP_SECONDS = 30               # how often idle sessions are looked for while any session is open
SCAN_BUSY = '另一处正在扫描这台机器人的硬件（硬件初始化或调研），这一次没有重复扫描'
REPAIR_BUSY = '这台机器人的修复会话正在进行，状态页暂不扫描硬件，以免影响修复'
UNPARSED = '设备返回的内容没能解析，这一部分没有更新'
SCAN_NOT_FINISHED = '硬件扫描没有完成：设备在限定时间内没有返回结果'
SOFTWARE_NOT_READ = '设备在限定时间内没有返回软件状态'
SESSION_LOST = 'SSH 会话中断，这一部分没有读到'
# How the hardware scan words a device without a driver name; anything else is the name of a loaded driver.
DRIVER_STATES = {'未加载': 'missing', '未知': 'unknown', '': 'none', '—': 'none'}
# A part of the full status that has never been read: every field unknown.
NO_HARDWARE = {'checked_at': None, 'duration_ms': None, 'complete': False, 'problem': None, 'summary': None,
               'system': None, 'categories': None, 'devices': None, 'issues': None, 'notes': []}
NO_SOFTWARE = {'checked_at': None, 'duration_ms': None, 'complete': False, 'problem': None, 'services': None,
               'containers': None, 'middleware': None, 'top': None, 'process_count': None, 'cores': None, 'notes': []}
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


def hardware_view(parsed, duration_ms=None):
    """A hardware scan (``hardware_scan.parse``) as the status page shows it: every device with whether
    a driver is loaded (``driver_state``) and, if it does not work, the reason (``issue``)."""
    reasons = {}
    for issue in parsed['issues']:                    # most severe first
        if issue['device_id']:
            reasons.setdefault(issue['device_id'], issue['title'])
    devices = [{**device, 'driver_state': DRIVER_STATES.get(device['driver'], 'loaded'), 'issue': reasons.get(device['id'], '')}
               for device in parsed['devices']]
    statuses = [device['status'] for device in devices]
    counts = {key: sum(1 for device in devices if device['category'] == key) for key in hardware_scan.CATEGORIES}
    return {'checked_at': time.time(), 'duration_ms': duration_ms, 'complete': parsed['complete'], 'problem': None,
            'summary': {'devices': len(devices), 'ok': statuses.count('ok'),
                        'attention': statuses.count('warning') + statuses.count('error'), 'unknown': statuses.count('unknown'),
                        'driver_missing': sum(1 for device in devices if device['driver_state'] == 'missing')},
            # Not the account, its groups or the address of the SSH connection: the page has no use for them.
            'system': {key: parsed['system'].get(key, '') for key in ('model', 'os', 'kernel', 'arch', 'jetson', 'cpu', 'cores')},
            'categories': [{'key': key, 'label': label, 'count': counts[key]}
                           for key, label in hardware_scan.CATEGORIES.items() if counts[key]],
            'devices': devices,
            'issues': [{key: issue[key] for key in ('id', 'severity', 'category', 'title', 'detail', 'device_id')}
                       for issue in parsed['issues']],
            'notes': list(parsed['notes'])}


class _NotRead(Exception):
    """A part of the full status was not read. The message is shown to the user."""

    def __init__(self, message, session_usable=False):
        super().__init__(message)
        self.session_usable = session_usable          # False: the command may still be running on that session


def _understood(parse, *args):
    """What the parser makes of a command's output. A parser that fails is a fault of ours, not of the
    connection: it is logged in full and reported as such, and the session stays."""
    try:
        return parse(*args)
    except Exception:
        LOG.exception('the output of a status command could not be parsed')
        raise _NotRead(UNPARSED, session_usable=True) from None


def _read_software(device):
    started = time.monotonic()
    # Unredacted like the basic status: CPU ticks are read from it. The parser filters the text it returns.
    result = hardware_init.run_command(device, device_software.SOFTWARE_EXEC, device_software.TIMEOUT_SECONDS,
                                       keep='head', limit=device_software.OUTPUT_LIMIT, redact=False)
    if result['timed_out']:
        raise _NotRead(SOFTWARE_NOT_READ)
    parsed = _understood(device_software.parse, result['stdout'], device.clean)
    return {**parsed, 'complete': parsed['complete'] and not result['output_head_dropped'],
            'checked_at': time.time(), 'duration_ms': round((time.monotonic() - started) * 1000), 'problem': None}


def _read_hardware(device):
    started = time.monotonic()
    result = hardware_init.run_command(device, hardware_scan.SCAN_EXEC, hardware_scan.SCAN_TIMEOUT_SECONDS,
                                       keep='head', limit=hardware_scan.SCAN_OUTPUT_LIMIT)
    if result['timed_out']:
        raise _NotRead(SCAN_NOT_FINISHED)             # half a device table would read as "that is all there is"
    duration = round((time.monotonic() - started) * 1000)
    return _understood(lambda: hardware_view(hardware_init.parse_scan(result)[0], duration))


def _kept(previous, never_read, problem):
    """What a part shows when this read did not get it: the earlier result with its own time, and why."""
    return {**(previous or never_read), 'problem': problem}


def _recent(part):
    """The part for a quick read: not a result from long ago, which would look like the present state
    until the full read replaces it. (A part never read has no time: its reason is always shown.)"""
    if part and part['checked_at'] is not None and time.time() - part['checked_at'] > CACHE_SECONDS:
        return None
    return part


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
            'status': status, 'error': error, 'hardware': None, 'software': None, 'refresh_seconds': REFRESH_SECONDS}


class StatusSessions:
    """One reusable SSH session per connection, so that polling does not log in for every read.

    While any session is open a sweeper closes those nobody polled for ``idle_seconds``.
    A cached session that has died is reopened once before the device is reported unreachable.
    The hardware and software of the last full read are kept with the session's entry: a read
    that does not get them again (a basic read, a part that timed out) shows them with their time.
    """

    def __init__(self, idle_seconds=IDLE_CLOSE_SECONDS, sweep_seconds=SWEEP_SECONDS):
        self.idle_seconds, self.sweep_seconds = idle_seconds, sweep_seconds
        self._guard = threading.Lock()
        self._entries = {}
        self._sweeper = None
        self._closed = False
        # Why the hardware of this robot must not be scanned right now, '' when it may (set by the engine):
        # another scan of it is running, or a repair session is changing it.
        self.scan_hold = lambda robot_id: ''

    def read(self, connection, secret, full=False):
        """One status read. ``full`` also reads the running software and the hardware (two more fixed commands)."""
        entry = self._claim(connection['id'])
        if entry is None:
            # Another poll is still connecting: show the last result instead of queueing up.
            last = self._entry(connection['id'])['view']
            return {**(last or {**_view(connection, None), 'reachable': None}), 'refreshing': True}
        try:
            entry['view'] = self._read(entry, connection, secret, full)
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
                                                  'view': None, 'gone': False, 'hardware': None, 'software': None})

    def _claim(self, key):
        """The locked entry of a connection, or None while another poll holds it."""
        while True:
            entry = self._entry(key)
            if not entry['lock'].acquire(blocking=False):
                return None
            if not entry['gone']:                     # forgotten between the lookup and the lock: take a fresh one
                return entry
            entry['lock'].release()

    def _read(self, entry, connection, secret, full):
        services = connection.get('watch_services') or []
        command = status_command(services)
        reused = entry['device'] is not None
        view = self._attempt(entry, connection, secret, command, services, full)
        if view is None and reused:                   # the cached session had died: once more with a fresh one
            view = self._attempt(entry, connection, secret, command, services, full)
        return view or _view(connection, _session_lost(connection))

    def _attempt(self, entry, connection, secret, command, services, full):
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
        status = parse_status(result['stdout'], services)
        if full:
            self._details(entry, connection)
        hardware, software = (entry['hardware'], entry['software']) if full else (_recent(entry['hardware']), _recent(entry['software']))
        return {**_view(connection, None, status, latency_ms), 'hardware': hardware,
                'software': device_software.mark_watched(software, status['services']) if software else None}

    def _details(self, entry, connection):
        """The two slower reads of a full status, on the session the basic status just used. Neither can
        fail the status: a part that did not arrive keeps what it showed before, and says why."""
        device = entry['device']
        device.deadline = time.monotonic() + READ_BUDGET_SECONDS      # run_command stops at it
        try:
            entry['software'], software_alive = self._part(_read_software, device, entry['software'], NO_SOFTWARE)
            hold = self.scan_hold(connection.get('robot_id'))
            if hold:
                entry['hardware'], hardware_alive = _kept(entry['hardware'], NO_HARDWARE, hold), True
            else:
                entry['hardware'], hardware_alive = self._part(_read_hardware, device, entry['hardware'], NO_HARDWARE)
        finally:
            device.deadline = float('inf')
        if not (software_alive and hardware_alive):   # a command that timed out may still be running: start afresh next time
            self._drop(entry)

    @staticmethod
    def _part(read, device, previous, never_read):
        """(what this part shows, whether the session is still good for the next read)."""
        try:
            return read(device), True
        except _NotRead as exc:
            return _kept(previous, never_read, str(exc)), exc.session_usable
        except Exception as exc:                      # paramiko / EOF / socket errors of a dead session
            LOG.warning('reading a part of the SSH status failed: %s', type(exc).__name__)
            return _kept(previous, never_read, SESSION_LOST), False

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
