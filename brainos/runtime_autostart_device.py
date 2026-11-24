"""What the console runs on a robot to start its management layer at boot (issue #20, R1-03, R1-08).

The install itself (issue #19) needs no administrator rights. Starting at boot does: a system service
``/etc/systemd/system/neuxmind-runtime.service`` that runs the management layer as the SSH login account,
through the same install script the console installed it with. This module holds every text that runs
on the robot for it, all fixed:

* ``autostart_check`` (read only, as the login account): the account, its home directory, whether the
  robot runs systemd and whether the account can use sudo (``sudo -n true``), the install at
  ``~/neuxmind`` and the body it recorded (issue #21's ``adapter`` file; the service starts that body),
  the service file if there is one (made by this console for this account, or not),
  its state, the digest of the service file the console would write, and the end of the runtime's log.
* ``autostart_mark`` (as the login account): writes or removes ``~/neuxmind/.autostart``. The service
  only starts while that file is there (``ConditionPathExists``), so removing or replacing the install
  leaves a service file that starts nothing.
* ``autostart_on`` (as root): renders the service file again from the account's passwd entry and the
  plain arguments, refuses unless its digest is the one the person confirmed, refuses a service file it
  did not make, writes it, reloads systemd, enables it and restarts it (``ExecStartPre`` stops the
  runtime that runs detached, then systemd starts it).
* ``autostart_off`` (as root): stops and disables the service, deletes the service file this console
  made, reloads systemd. Nothing else.

The two account scripts go to ``sh -s --`` on standard input like issue #19's. The two root scripts
cannot: standard input carries the sudo password. They go on the command line instead,
``sudo -S -p '' sh -c 'exec </dev/null; <script>' nx-autostart <words>``, the way the repair commands do
(``hardware_init._quote_root``): the script never reads standard input, so the password is read by sudo
or by nobody, never by a shell. It is never part of a command line.
"""
import hashlib
import re
import shlex
import time

from . import hardware_init
from . import runtime_install_device as dev

UNIT_NAME = 'neuxmind-runtime.service'
UNIT_PATH = '/etc/systemd/system/' + UNIT_NAME
WANTS_PATH = '/etc/systemd/system/multi-user.target.wants/' + UNIT_NAME
BASH = '/bin/bash'
MARK_FILE = '.autostart'
ADAPTERS = ('sim_quadruped', 'sim_arm', 'ros2')
OWNER_LINE = '# The console replaces or removes this file. nx-autostart user=%s package=%s'
SUDO_MODES = ('root', 'nopasswd', 'password', 'none')
PASSWORD_LIMIT = 256
_USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}')
_HOME = re.compile(r'(/[A-Za-z0-9_][A-Za-z0-9._-]*)+')
_PACKAGE = re.compile(r'runtime-[A-Za-z0-9._+-]{1,72}')
_DIGEST = re.compile(r'[0-9a-f]{64}')


def render_unit(user, home, package_id, port, adapter):
    """The service file, byte for byte as ``autostart_on`` writes it (``_UNIT`` renders the same text)."""
    root, script = home + '/neuxmind', '%s/neuxmind-pkg/console/%s/device_install.sh' % (home, package_id)
    lines = ['# Made by the NeuxMind console (issue #20): starts the management layer of %s when the device boots.' % user,
             OWNER_LINE % (user, package_id),
             '[Unit]', 'Description=NeuxMind management layer (%s)' % user,
             'Wants=network-online.target', 'After=network-online.target',
             'ConditionPathExists=%s/%s' % (root, MARK_FILE), 'StartLimitIntervalSec=0', '',
             '[Service]', 'Type=forking', 'User=%s' % user,
             'Environment=NX_ROOT=%s NX_PORT=%d NX_ADAPTER=%s' % (root, port, adapter),
             'ExecStartPre=%s %s stop' % (BASH, script), 'ExecStart=%s %s start' % (BASH, script),
             'PIDFile=%s/runtime.pid' % root, 'Restart=on-failure', 'RestartSec=15', 'TimeoutStartSec=180', '',
             '[Install]', 'WantedBy=multi-user.target']
    return '\n'.join(lines) + '\n'


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def valid_inputs(user, home, package_id, port, adapter):
    """Whether these may go into a service file: plain names, a plain absolute path, a port, a known body."""
    return bool(isinstance(user, str) and _USER.fullmatch(user) and isinstance(home, str) and _HOME.fullmatch(home)
                and isinstance(package_id, str) and _PACKAGE.fullmatch(package_id)
                and isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535 and adapter in ADAPTERS)


# ------------------------------------------------------------------ the texts that run on the robot

# Shared by autostart_check (as the account) and autostart_on (as root): the same checks, the same file.
_UNIT = r'''UNIT=/etc/systemd/system/neuxmind-runtime.service
plain_home() {  # an absolute path of plain names, none of them hidden
  case $1 in /*) ;; *) return 1;; esac
  case $1 in *[!A-Za-z0-9._/-]*|*//*|*/.*|*/) return 1;; esac
  return 0
}
plain_user() {
  case $1 in ''|[!A-Za-z0-9_]*|*[!A-Za-z0-9_.-]*) return 1;; esac
  [ "${#1}" -le 32 ]
}
home_of() {  # the account's home directory as the passwd database has it
  getent passwd "$1" 2>/dev/null | head -n 1 | cut -d: -f6
}
render_unit() {  # render_unit <user> <home> <package id> <port> <adapter>
  nx_root=$2/neuxmind
  nx_script=$2/neuxmind-pkg/console/$3/device_install.sh
  printf '%s\n' \
    "# Made by the NeuxMind console (issue #20): starts the management layer of $1 when the device boots." \
    "# The console replaces or removes this file. nx-autostart user=$1 package=$3" \
    '[Unit]' "Description=NeuxMind management layer ($1)" \
    'Wants=network-online.target' 'After=network-online.target' \
    "ConditionPathExists=$nx_root/.autostart" 'StartLimitIntervalSec=0' '' \
    '[Service]' 'Type=forking' "User=$1" \
    "Environment=NX_ROOT=$nx_root NX_PORT=$4 NX_ADAPTER=$5" \
    "ExecStartPre=/bin/bash $nx_script stop" "ExecStart=/bin/bash $nx_script start" \
    "PIDFile=$nx_root/runtime.pid" 'Restart=on-failure' 'RestartSec=15' 'TimeoutStartSec=180' '' \
    '[Install]' 'WantedBy=multi-user.target'
}
unit_owner() {  # unit_owner <user>: none | ours (made by this console for <user>) | other (for another account) | foreign
  if [ ! -e "$UNIT" ] && [ ! -L "$UNIT" ]; then echo none; return; fi
  if [ -L "$UNIT" ] || [ ! -f "$UNIT" ]; then echo foreign; return; fi
  owner=$(grep -m 1 '^# The console replaces or removes this file\. nx-autostart user=' "$UNIT" 2>/dev/null)
  case $owner in
    '') echo foreign;;
    *" user=$1 package="*) echo ours;;
    *) echo other;;
  esac
}
good_inputs() {  # good_inputs <user> <home> <package id> <port> <adapter>
  plain_user "$1" && plain_home "$2" || return 1
  case $3 in runtime-?*) ;; *) return 1;; esac
  case $3 in *[!A-Za-z0-9._+-]*) return 1;; esac
  case $4 in ''|*[!0-9]*) return 1;; esac
  [ "$4" -ge 1024 ] && [ "$4" -le 65535 ] || return 1
  case $5 in sim_quadruped|sim_arm|ros2) return 0;; esac
  return 1
}
'''

# $1 package id, $2 port, $3 adapter. Read only. Keys, the final marker, then the end of the runtime's log.
CHECK_SCRIPT = dev._PRELUDE + _UNIT + r'''port=$2; adapter=$3
# Values pass as plain characters only: files the login account writes (.console-package, .autostart) must not
# be able to add lines (a carriage return is a line break to the console's parser) and so forge keys.
say() { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | LC_ALL=C tr -cd 'A-Za-z0-9._+/ :=-')"; }
echo '@@NX-INSTALL:begin@@'
user=$(id -un 2>/dev/null); uid=$(id -u 2>/dev/null)
say user "$user"
say uid "$uid"
home=
if command -v getent >/dev/null 2>&1; then home=$(home_of "$user"); fi
say home "$home"
here=$(cd "$HOME" 2>/dev/null && pwd -P)
there=$(cd "$home" 2>/dev/null && pwd -P)
if [ -n "$home" ] && [ "$here" = "$there" ] && plain_home "$home" && plain_user "$user"; then say home_ok yes; else say home_ok no; fi
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then say systemd yes; else say systemd no; fi
if [ "$uid" = 0 ]; then sudo=root
elif ! command -v sudo >/dev/null 2>&1; then sudo=none
elif sudo -n true </dev/null >/dev/null 2>&1; then sudo=nopasswd
else sudo=password; fi
say sudo "$sudo"
missing=
[ -x /bin/bash ] || missing="$missing /bin/bash"
for tool in getent sha256sum cut grep; do command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"; done
say missing "${missing# }"
if [ -f "$root/.neuxmind-device-install" ] && [ ! -L "$root/.neuxmind-device-install" ]; then
  if [ "$(first "$root/.neuxmind-device-install")" = state=installed ]; then say install installed; else say install unfinished; fi
elif [ -e "$root" ] || [ -L "$root" ]; then say install foreign
else say install none; fi
say console "$(first "$root/.console-package")"
if pkg_in_place && [ -f "$pkg/device_install.sh" ] && [ ! -L "$pkg/device_install.sh" ]; then say script yes; else say script no; fi
running=no; running_port=
pid=$(first "$root/runtime.pid")
entry=$(cd "$root" 2>/dev/null && pwd -P)/venv/bin/neuxmind-runtime
case $pid in
  ''|*[!0-9]*) ;;
  *) if [ "$pid" -gt 1 ] && holds "$pid" "$entry"; then
       running=yes
       if [ -r "/proc/$pid/cmdline" ]; then running_port=$(tr '\0' '\n' < "/proc/$pid/cmdline" | sed -n '/^--port$/{n;p;q;}'); fi
     fi;;
esac
say running "$running"
say running_port "$running_port"
say mark "$(first "$root/.autostart")"
# The body the install recorded (issue #21's install script; its start uses it): the service starts that one.
recorded=$(first "$root/adapter")
case $recorded in sim_quadruped|sim_arm|ros2) adapter=$recorded; say adapter_source recorded;; *) say adapter_source console;; esac
say adapter "$adapter"
owner=$(unit_owner "$user")
say unit "$owner"
say unit_package "$(grep -m 1 '^# The console replaces or removes this file\. nx-autostart user=' "$UNIT" 2>/dev/null | sed -n 's/.* package=\([A-Za-z0-9._+-]*\)$/\1/p')"
text=
if good_inputs "$user" "$home" "$pkg_id" "$port" "$adapter"; then
  text=$(render_unit "$user" "$home" "$pkg_id" "$port" "$adapter")
  if command -v sha256sum >/dev/null 2>&1; then say digest "$(printf '%s\n' "$text" | sha256sum | cut -c 1-64)"; fi
fi
same=no
if [ "$owner" = ours ] && [ -n "$text" ] && [ "$(cat "$UNIT" 2>/dev/null)" = "$text" ]; then same=yes; fi
say unit_same "$same"
if command -v systemctl >/dev/null 2>&1; then
  say enabled "$(systemctl is-enabled neuxmind-runtime.service 2>/dev/null | head -n 1)"
  systemctl show neuxmind-runtime.service -p ActiveState -p SubState -p Result -p NRestarts -p MainPID 2>/dev/null \
    | sed -n 's/^\([A-Za-z]*\)=\([A-Za-z0-9-]*\)$/\1=\2/p' | tr 'A-Z' 'a-z' | sed 's/^/svc_/'
fi
echo '@@NX-INSTALL:checked@@'
if [ -f "$root/runtime.log" ] && [ ! -L "$root/runtime.log" ]; then tail -n LINES "$root/runtime.log" | cut -c 1-300 | sed 's/^/| /'; fi
'''.replace('LINES', str(dev.LOG_LINES))

# $1 package id, $2 port, $3 adapter, $4 on|off. Writes or removes ~/neuxmind/.autostart, nothing else.
MARK_SCRIPT = dev._PRELUDE + r'''port=$2; adapter=$3; want=$4
echo '@@NX-INSTALL:begin@@'
case $want in
  off)
    if [ -f "$root/.autostart" ] && [ ! -L "$root/.autostart" ]; then rm -f -- "$root/.autostart"; fi
    echo '@@NX-INSTALL:unmarked@@'; exit 0;;
  on) ;;
  *) echo '@@NX-INSTALL:refused@@'; exit 2;;
esac
if [ -L "$root" ] || [ ! -f "$root/.neuxmind-device-install" ] || [ -L "$root/.neuxmind-device-install" ] \
   || [ "$(first "$root/.neuxmind-device-install")" != state=installed ] || [ "$(first "$root/.console-package")" != "$pkg_id" ]; then
  echo '@@NX-INSTALL:notours@@'; exit 3
fi
if [ -L "$root/.autostart" ] || { [ -e "$root/.autostart" ] && [ ! -f "$root/.autostart" ]; }; then echo '@@NX-INSTALL:taken@@'; exit 4; fi
printf '%s %s %s\n' "$pkg_id" "$port" "$adapter" > "$root/.autostart.tmp" && mv -f -- "$root/.autostart.tmp" "$root/.autostart" \
  || { rm -f -- "$root/.autostart.tmp"; echo '@@NX-INSTALL:unwritable@@'; exit 5; }
echo '@@NX-INSTALL:marked@@'
'''

_ROOT_PRELUDE = r'''echo '@@NX-INSTALL:begin@@'
refuse() { echo "@@NX-INSTALL:$1@@"; exit "${2:-2}"; }
[ "$(id -u)" = 0 ] || refuse notroot
user=$1
invoker=${SUDO_USER:-$(id -un)}
[ "$invoker" = "$user" ] || refuse notyou
plain_user "$user" || refuse refused
[ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 || refuse nosystemd
'''

# $1 user, $2 package id, $3 port, $4 adapter, $5 the digest of the service file the person confirmed.
ON_SCRIPT = _UNIT + _ROOT_PRELUDE + r'''pkg_id=$2; port=$3; adapter=$4; want=$5
command -v getent >/dev/null 2>&1 && command -v sha256sum >/dev/null 2>&1 && [ -x /bin/bash ] || refuse unsupported
home=$(home_of "$user")
good_inputs "$user" "$home" "$pkg_id" "$port" "$adapter" && [ -d "$home" ] || refuse refused
text=$(render_unit "$user" "$home" "$pkg_id" "$port" "$adapter")
[ "$(printf '%s\n' "$text" | sha256sum | cut -c 1-64)" = "$want" ] || refuse changed 3
owner=$(unit_owner "$user")
[ "$owner" = none ] || [ "$owner" = ours ] || refuse taken 4
same=no
if [ "$owner" = ours ] && [ "$(cat "$UNIT")" = "$text" ]; then same=yes; fi
if [ "$same" = no ]; then
  tmp=$(mktemp "$UNIT.nx-XXXXXX") || refuse unwritable 5
  if ! { printf '%s\n' "$text" > "$tmp" && chmod 0644 "$tmp" && mv -f -- "$tmp" "$UNIT"; }; then rm -f -- "$tmp"; refuse unwritable 5; fi
  systemctl daemon-reload || refuse reloadfailed 6
fi
systemctl enable neuxmind-runtime.service >/dev/null 2>&1 || refuse enablefailed 6
if [ "$same" = yes ] && [ "$(systemctl is-active neuxmind-runtime.service 2>/dev/null)" = active ]; then
  echo '@@NX-INSTALL:already@@'; exit 0
fi
systemctl --no-block restart neuxmind-runtime.service || refuse startfailed 6
echo '@@NX-INSTALL:enabled@@'
'''

# $1 user. Stops and disables the service this console made for this account, deletes its file.
OFF_SCRIPT = _UNIT + _ROOT_PRELUDE + r'''owner=$(unit_owner "$user")
[ "$owner" = none ] && { echo '@@NX-INSTALL:absent@@'; exit 0; }
[ "$owner" = ours ] || refuse taken 4
systemctl disable --now neuxmind-runtime.service >/dev/null 2>&1 || refuse disablefailed 6
rm -f -- "$UNIT" || refuse unwritable 5
systemctl daemon-reload || refuse reloadfailed 6
systemctl reset-failed neuxmind-runtime.service >/dev/null 2>&1
echo '@@NX-INSTALL:disabled@@'
'''

SCRIPTS = {'autostart_check': CHECK_SCRIPT, 'autostart_mark': MARK_SCRIPT}
ROOT_SCRIPTS = {'autostart_on': ON_SCRIPT, 'autostart_off': OFF_SCRIPT}
ARITY = {'autostart_check': 3, 'autostart_mark': 4, 'autostart_on': 5, 'autostart_off': 1}
ROOT_NAME = 'nx-autostart'               # $0 of the root scripts: what a process list on the robot shows
ROOT_PREFIX = {'root': 'LC_ALL=C ', 'nopasswd': 'LC_ALL=C sudo -n ', 'password': "LC_ALL=C sudo -S -p '' "}


def _words(kind, arguments):
    words = [str(argument) for argument in arguments]
    if len(words) != ARITY[kind] or not all(dev._ARGUMENT.fullmatch(word) for word in words):
        raise ValueError('autostart script argument is not a plain word')
    return words


def command(kind, *arguments):
    """``sh -s --`` and the arguments of one of the account scripts."""
    return ' '.join([dev.COMMAND, *_words(kind, arguments)])


def root_command(kind, sudo, *arguments):
    """The command line of a root script for this robot's way to become root. The password, when one is
    needed, goes to standard input, never here."""
    if sudo not in ROOT_PREFIX:
        raise ValueError('no way to become root on this robot')
    inner = 'exec </dev/null; ' + ROOT_SCRIPTS[kind]
    return ROOT_PREFIX[sudo] + 'sh -c ' + shlex.quote(inner) + ' ' + ' '.join([ROOT_NAME, *_words(kind, arguments)])


def password_ok(password):
    """A password sudo can read from one line of standard input (``PASSWORD_LIMIT`` bytes of UTF-8 at most)."""
    if not isinstance(password, str) or not password or any(ch in password for ch in '\n\r\0'):
        return False
    try:
        return len(password.encode('utf-8')) <= PASSWORD_LIMIT
    except UnicodeEncodeError:           # a lone surrogate: not something sudo could be sent
        return False


def sudo_refusal(stderr):
    """Why sudo did not run the script, from its own (LC_ALL=C) words: 'wrong_password', 'not_permitted',
    'password_required', 'needs_tty' or 'failed'."""
    text = (stderr or '').lower()
    if 'incorrect password' in text or 'sorry, try again' in text or 'no password was provided' in text:
        return 'wrong_password'
    if 'not in the sudoers' in text or 'is not allowed to' in text or 'may not run sudo' in text:
        return 'not_permitted'
    if 'a password is required' in text:
        return 'password_required'
    if 'terminal is required' in text or 'no tty present' in text or 'must have a tty' in text:
        return 'needs_tty'
    return 'failed'


def run(session, line, stdin, seconds, deadline):
    """(word, keys, log, stderr, exit status) of one script on the robot, run again after a dropped
    connection until ``deadline`` like ``DeviceSession.run``. Every script here may be run again."""
    while True:
        device = session._connected(deadline)
        try:
            result = hardware_init.run_command(device, line, seconds, stdin=stdin, keep='head', limit=dev.OUTPUT_LIMIT,
                                               redact=False)
        except dev._NETWORK:
            result = None
        if session.stop.is_set():
            raise dev.Stopped()
        if result is not None and not result['timed_out']:
            word, keys, log = dev.parse(result['stdout'])
            if word or result['exit_code'] not in (None, -1):
                session.failures = 0
                return word, keys, log, result['stderr'], result['exit_code']
        session.close()
        if not session._pause(deadline):
            raise dev.Unreachable('设备在规定时间内没有答复', 'timeout')


def run_script(session, kind, *arguments, seconds=60, deadline=None):
    deadline = deadline or time.monotonic() + seconds * 3
    return run(session, command(kind, *arguments), SCRIPTS[kind], seconds, deadline)


def run_root(session, kind, sudo, password, *arguments, seconds=150, deadline=None):
    deadline = deadline or time.monotonic() + seconds * 3
    stdin = password + '\n' if sudo == 'password' else None
    return run(session, root_command(kind, sudo, *arguments), stdin, seconds, deadline)
