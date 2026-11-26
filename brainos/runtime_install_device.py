"""What the console runs on a robot while it installs the management layer (issue #19, R1-03).

Four fixed POSIX sh scripts, sent to ``sh -s --`` on standard input with plain arguments only (a
package id, a step name, a job id, a port, an adapter name), the way the upload channel of issue #18
runs its own. Nothing else is ever run for an install. The install itself is the robot's copy of
``runtime/deploy/device_install.sh``, which keeps to its own rules: only the install directory
``~/neuxmind``, no sudo, no system files, loopback only.

* ``probe`` (read only): the system, Python and the tools the install script needs, the free space
  in the home directory, what is at ``~/neuxmind`` (nothing, an empty directory, a directory the
  script did not make, an unfinished or a finished install, which package made it), whether its
  runtime runs and on which port, and the state of this package's jobs.
* ``job``: starts one step of ``device_install.sh`` (install, start, remove) detached from the SSH
  session, with its output in ``<step>.log`` and its exit status in ``<step>.exit`` next to the
  package. A dropped connection does not stop it; the console reads the result when it is back.
  Started twice with the same job id it starts once: an answer lost on the way is not a second run.
* ``poll`` (read only): the state of one step's job and the end of its log.
* ``clean``: deletes the files this console put in the package's directory (the script, the two
  wheels, the jobs' files, unfinished uploads) and the directory, nothing else.

Where things are on the robot: the package in ``~/neuxmind-pkg/console/<package id>/``, the install
in ``~/neuxmind`` (``device_install.sh``'s default, where issue #17 reads the token). An install this
console made holds ``.console-package`` with the package id.
"""
import re
import secrets
import time

import paramiko

from . import device_access, hardware_init

MARK = '@@NX-INSTALL:'
BEGIN = MARK + 'begin@@'
_WORD = re.compile(r'^@@NX-INSTALL:([a-z]+)@@$')
_KEY = re.compile(r'^([a-z_]+)=(.*)$')
STEPS = ('install', 'start', 'remove', 'restart')     # restart (issue #38): stop, then start, as one job
# Issue #38, a real body (device_install.sh's NX_ADAPTER=ros2). The console sends two more files into the
# package's directory, over the upload channel like the wheels: BODY_FILE (the ROS 2 settings the install is
# made with, one KEY=VALUE per line, and whether a device configuration comes with it) and CONFIG_FILE (the
# device configuration, which a start puts into the install directory as ros2-config.json).
BODY_FILE, CONFIG_FILE = 'body.env', 'ros2-config.json'
CONFIG_MARK = '.console-body-config'    # in the install directory: its device configuration was put there by a console
ROS_KEYS = ('ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION', 'FASTRTPS_DEFAULT_PROFILES_FILE',
            'CYCLONEDDS_URI', 'ZENOH_SESSION_CONFIG_URI')
BODY_KEYS = ('NX_ROS_SETUP', *ROS_KEYS)
CONFIG_READ_LIMIT = 24 * 1024       # a device configuration larger than this is not read back (``rosprobe``)
JOB_FILES = tuple('%s.%s' % (step, kind) for step in STEPS for kind in ('job', 'exit', 'exit.tmp', 'log', 'pid'))
COMMAND = 'sh -s --'
_ARGUMENT = re.compile(r'[A-Za-z0-9._+-]{1,80}')
_CODE = re.compile(r'[0-9]{1,3}', re.ASCII)
_JOB_ID = re.compile(r'[0-9a-f]{16}')
OUTPUT_LIMIT = 64 * 1024
LOG_LINES = 40
_NETWORK = (OSError, EOFError, paramiko.SSHException, AttributeError)

_PRELUDE = r'''pkg_id=$1
case $pkg_id in runtime-*) ;; *) echo '@@NX-INSTALL:refused@@'; exit 2;; esac
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || { echo '@@NX-INSTALL:nohome@@'; exit 2; }
root=$HOME/neuxmind
pkg=$HOME/neuxmind-pkg/console/$pkg_id
first() {
  line=
  if [ -f "$1" ] && [ ! -L "$1" ]; then IFS= read -r line < "$1"; fi
  printf '%s' "$line"
}
holds() {  # holds <pid> <word>: that process runs and one of its arguments is <word>
  if [ -r "/proc/$1/cmdline" ]; then
    tr '\0' '\n' < "/proc/$1/cmdline" | grep -Fqx -- "$2"
  elif [ ! -d /proc/self ]; then
    case " $(ps -p "$1" -o args= 2>/dev/null) " in *" $2 "*) return 0;; esac
    return 1
  else
    return 1
  fi
}
pkg_in_place() {  # the package's directory is itself, below this home directory, with no link on the way
  [ -d "$pkg" ] && [ ! -L "$pkg" ] && [ "$(cd "$pkg" 2>/dev/null && pwd -P)" = "$(cd "$HOME" && pwd -P)/neuxmind-pkg/console/$pkg_id" ]
}
job_state() {  # job_state <step>: "<job id> <exit status> <alive>", "-" for what is not there
  job=$(first "$pkg/$1.job"); ended=$(first "$pkg/$1.exit"); pid=$(first "$pkg/$1.pid"); alive=no; code=-
  case $pid in
    ''|*[!0-9]*) ;;
    *) if [ "$pid" -gt 1 ] && kill -0 "$pid" 2>/dev/null && holds "$pid" "$pkg"; then alive=yes; fi;;
  esac
  if [ -n "$job" ]; then
    case $ended in "$job "*) code=${ended#"$job "};; esac
  fi
  case $code in ''|*[!0-9]*) code=-;; esac
  printf '%s %s %s\n' "${job:--}" "$code" "$alive"
}
'''

# Read only. Prints key=value lines between the begin marker and the final one.
PROBE_SCRIPT = _PRELUDE + r'''say() { printf '%s=%s\n' "$1" "$2"; }
echo '@@NX-INSTALL:begin@@'
say os "$(uname -s 2>/dev/null)"
say arch "$(uname -m 2>/dev/null)"
py=none; venv=no
if command -v python3 >/dev/null 2>&1; then
  py=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || py=none
  if python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then venv=yes; fi
fi
say python "$py"
say venv "$venv"
missing=
for tool in bash flock setsid realpath nohup; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
say missing "${missing# }"
say df "$(df -Pk "$HOME" 2>/dev/null | tail -n 1)"
if [ -L "$root" ]; then state=link
elif [ ! -e "$root" ]; then state=none
elif [ ! -d "$root" ]; then state=foreign
elif [ -z "$(ls -A "$root" 2>/dev/null)" ]; then state=empty
elif [ -f "$root/.neuxmind-device-install" ] && [ ! -L "$root/.neuxmind-device-install" ]; then
  if [ "$(first "$root/.neuxmind-device-install")" = state=installed ]; then state=installed; else state=unfinished; fi
else state=foreign; fi
say root "$state"
console=; version=; running=no; port=
if [ "$state" = installed ] || [ "$state" = unfinished ]; then
  console=$(first "$root/.console-package")
  say adapter "$(first "$root/adapter")"
  for found in "$root"/venv/lib/python*/site-packages/neuxmind_runtime-*.dist-info; do
    [ -d "$found" ] || continue
    version=${found##*/neuxmind_runtime-}; version=${version%.dist-info}
  done
  pid=$(first "$root/runtime.pid")
  entry=$(cd "$root" 2>/dev/null && pwd -P)/venv/bin/neuxmind-runtime
  case $pid in
    ''|*[!0-9]*) ;;
    *) if [ "$pid" -gt 1 ] && holds "$pid" "$entry"; then
         running=yes
         if [ -r "/proc/$pid/cmdline" ]; then
           port=$(tr '\0' '\n' < "/proc/$pid/cmdline" | sed -n '/^--port$/{n;p;q;}')
         else
           port=$(ps -p "$pid" -o args= 2>/dev/null | sed -n 's/.* --port \([0-9][0-9]*\).*/\1/p')
         fi
       fi;;
  esac
fi
say console "$console"
say version "$version"
say running "$running"
say running_port "$port"
for step in install start remove; do say "job_$step" "$(job_state "$step")"; done
say pkg_files "$(ls -A "$pkg" 2>/dev/null | head -n 40 | tr '\n' ' ')"
echo '@@NX-INSTALL:probed@@'
'''

# $2 step, $3 job id, $4 port, $5 adapter (the console's, from the body type; start prefers the recorded one).
# Starts the step once per job id, detached.
_JOB_BODY = ('cd "$1" || exit 1\n'
             'c=$NX_ROOT/CONFIGFILE\n'
             'if [ "$2" = restart ]; then\n'
             '  { bash ./device_install.sh stop && bash ./device_install.sh start; } >"$2.log" 2>&1 </dev/null\n'
             '  code=$?\n'
             # Issue #38: a restart that does not come up with the device configuration it was given goes back to
             # the one before (or to none) and starts again, so that a bad file does not leave the robot without
             # its management layer. The step still fails: the configuration was not applied.
             '  back=\n'
             '  if [ "$code" -ne 0 ]; then\n'
             '    if [ -f "$c.nxprev" ]; then mv -f -- "$c.nxprev" "$c" && back=earlier\n'
             '    elif [ -f "$c.nxnone" ]; then rm -f -- "$c" "$NX_ROOT/CONFIGMARK"; back=none; fi\n'
             '    if [ -n "$back" ]; then\n'
             '      { echo "the management layer did not start with the new device configuration; going back to the $back one";\n'
             '        bash ./device_install.sh start; } >>"$2.log" 2>&1 </dev/null\n'
             '    fi\n'
             '  fi\n'
             'else\n'
             '  bash ./device_install.sh "$2" >"$2.log" 2>&1 </dev/null\n'
             '  code=$?\n'
             'fi\n'
             'if [ "$2" != remove ] && [ -d "$NX_ROOT" ]; then rm -f -- "$c.nxprev" "$c.nxnone"; fi\n'
             'if [ "$2" = install ] && [ "$code" -eq 0 ]; then printf "%s\\n" "$4" >"$NX_ROOT/.console-package" || code=90; fi\n'
             'printf "%s %s\\n" "$3" "$code" >"$2.exit.tmp" && mv -f "$2.exit.tmp" "$2.exit"\n'
             'exit 0\n')     # not the mv: sh would become it and no longer name the package (job_state: alive)
JOB_SCRIPT = _PRELUDE + r'''step=$2; job=$3; port=$4; adapter=$5
case $step in install|start|remove|restart) ;; *) echo '@@NX-INSTALL:refused@@'; exit 2;; esac
if ! pkg_in_place || [ ! -f "$pkg/device_install.sh" ] || [ -L "$pkg/device_install.sh" ]; then echo '@@NX-INSTALL:nopackage@@'; exit 3; fi
read -r cur_job cur_code cur_alive <<EOF
$(job_state "$step")
EOF
if [ "$cur_job" = "$job" ]; then      # asked before: started already, unless the first try was cut before it began
  if [ "$cur_alive" = yes ] || [ "$cur_code" != - ] || [ -e "$pkg/$step.pid" ]; then echo '@@NX-INSTALL:already@@'; exit 0; fi
fi
if [ "$cur_alive" = yes ]; then echo '@@NX-INSTALL:busy@@'; exit 4; fi
# The body the install recorded (issue #21's install script keeps it in ~/neuxmind/adapter) is the one start runs; the
# console's guess from the card would override it. An install without that record (before #21) gets the console's.
if [ "$step" = start ] || [ "$step" = restart ]; then
  recorded=$(first "$root/adapter")
  case $recorded in sim_quadruped|sim_arm|ros2) adapter=$recorded;; esac
fi
# A real body (issue #38): install is made with the ROS 2 settings of the body file and nothing the login brought;
# a start puts the device configuration that came with it into the install directory. Checked before the job is
# recorded, so a refusal here leaves the earlier job's files as they were.
# A start of an install that came without a body file (made by hand) runs it as it is.
body=$pkg/BODYFILE; config=no
if [ "$adapter" = ros2 ] && [ "$step" = install ]; then
  if [ ! -f "$body" ] || [ -L "$body" ]; then echo '@@NX-INSTALL:nobody@@'; exit 6; fi
fi
if [ "$adapter" = ros2 ] && [ "$step" != remove ] && [ -f "$body" ] && [ ! -L "$body" ]; then
  unset BODYKEYS
  while IFS= read -r line || [ -n "$line" ]; do
    key=${line%%=*}; value=${line#*=}
    case $line in *=*) ;; *) echo '@@NX-INSTALL:badbody@@'; exit 6;; esac
    case $key in
      BODYCASE) if [ "$step" = install ]; then export "$key=$value"; fi;;
      NX_BODY_CONFIG) config=$value;;
      *) echo '@@NX-INSTALL:badbody@@'; exit 6;;
    esac
  done < "$body"
  # The device configuration is this console's while its mark is there: one it put there is replaced or, when
  # the body has none any more, taken away; a file someone else put there is only ever replaced. What was there
  # is kept until the job ends (.nxprev, or .nxnone for "nothing"): a restart that fails goes back to it.
  if [ "$step" != install ] && [ -d "$root" ] && [ ! -L "$root" ]; then
    cfg=$root/CONFIGFILE; mark=$root/CONFIGMARK
    rm -f -- "$cfg.nxprev" "$cfg.nxnone" "$cfg.nxtmp"
    if [ "$config" = yes ]; then
      src=$pkg/CONFIGFILE
      if [ ! -f "$src" ] || [ -L "$src" ]; then echo '@@NX-INSTALL:noconfig@@'; exit 6; fi
      if [ -f "$cfg" ] && [ ! -L "$cfg" ]; then
        ( umask 077; cat "$cfg" > "$cfg.nxprev" ) || { rm -f -- "$cfg.nxprev"; echo '@@NX-INSTALL:unwritable@@'; exit 5; }
      else
        : > "$cfg.nxnone" || { echo '@@NX-INSTALL:unwritable@@'; exit 5; }
      fi
      if ! ( umask 077; cat "$src" > "$cfg.nxtmp" ) || ! mv -f -- "$cfg.nxtmp" "$cfg" || ! : > "$mark"; then
        rm -f -- "$cfg.nxtmp" "$cfg.nxprev" "$cfg.nxnone"; echo '@@NX-INSTALL:unwritable@@'; exit 5
      fi
    elif [ -f "$mark" ] && [ -f "$cfg" ] && [ ! -L "$cfg" ]; then
      mv -f -- "$cfg" "$cfg.nxprev" || { echo '@@NX-INSTALL:unwritable@@'; exit 5; }
    fi
  elif [ "$step" != install ] && [ "$config" = yes ]; then
    echo '@@NX-INSTALL:noconfig@@'; exit 6
  fi
fi
rm -f -- "$pkg/$step.job" "$pkg/$step.exit" "$pkg/$step.exit.tmp" "$pkg/$step.log" "$pkg/$step.pid"
printf '%s\n' "$job" > "$pkg/$step.job" || { echo '@@NX-INSTALL:unwritable@@'; exit 5; }
detach=
if command -v setsid >/dev/null 2>&1; then detach=setsid; fi
body='JOBSTEPS'
NX_ROOT=$root NX_PORT=$port NX_ADAPTER=$adapter nohup $detach sh -c "$body" nx-install-job "$pkg" "$step" "$job" "$pkg_id" >/dev/null 2>&1 </dev/null &
printf '%s\n' "$!" > "$pkg/$step.pid"
echo '@@NX-INSTALL:started@@'
'''.replace('JOBSTEPS', _JOB_BODY).replace('BODYFILE', BODY_FILE).replace('CONFIGFILE', CONFIG_FILE) \
    .replace('CONFIGMARK', CONFIG_MARK).replace('BODYKEYS', ' '.join(BODY_KEYS)).replace('BODYCASE', '|'.join(BODY_KEYS))

# $2 step. Read only: the job's state, then the end of its log, each line after "| ".
POLL_SCRIPT = _PRELUDE + r'''step=$2
case $step in install|start|remove|restart) ;; *) echo '@@NX-INSTALL:refused@@'; exit 2;; esac
echo '@@NX-INSTALL:begin@@'
printf 'job=%s\n' "$(job_state "$step")"
echo '@@NX-INSTALL:polled@@'
if [ -f "$pkg/$step.log" ] && [ ! -L "$pkg/$step.log" ]; then tail -n LINES "$pkg/$step.log" | cut -c 1-300 | sed 's/^/| /'; fi
'''.replace('LINES', str(LOG_LINES))

# Deletes only names this console puts in the package's directory, then the directory.
CLEAN_SCRIPT = _PRELUDE + r'''echo '@@NX-INSTALL:begin@@'
if [ ! -e "$pkg" ] && [ ! -L "$pkg" ]; then echo '@@NX-INSTALL:cleaned@@'; exit 0; fi
if ! pkg_in_place; then echo '@@NX-INSTALL:moved@@'; exit 8; fi
for step in install start remove restart; do
  read -r j c a <<EOF
$(job_state "$step")
EOF
  if [ "$a" = yes ]; then echo '@@NX-INSTALL:busy@@'; exit 4; fi
done
kept=
for f in "$pkg"/* "$pkg"/.[!.]* "$pkg"/..?*; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  name=${f##*/}
  case $name in
    device_install.sh|neuxmind_spec-*.whl|neuxmind_runtime-*.whl|BODYFILE|CONFIGFILE|JOBFILES) ;;
    .*.nxpart-*|.*.nxtmp-*) ;;
    *) kept="$kept $name"; continue;;
  esac
  if [ -d "$f" ] && [ ! -L "$f" ]; then kept="$kept $name"; continue; fi
  rm -f -- "$f"
done
rmdir -- "$pkg" 2>/dev/null
rmdir -- "$HOME/neuxmind-pkg/console" 2>/dev/null
if [ -n "$kept" ]; then printf 'kept=%s\n' "${kept# }"; echo '@@NX-INSTALL:kept@@'; else echo '@@NX-INSTALL:cleaned@@'; fi
'''.replace('JOBFILES', '|'.join(JOB_FILES)).replace('BODYFILE', BODY_FILE).replace('CONFIGFILE', CONFIG_FILE)

# Read only (issue #38): what the robot says about its own ROS 2, for a person who fills in a real body. The setup
# files below /opt/ros; the ROS settings of the programs of this login that run with ROS 2 (the set with the most
# settings: a robot's nodes often run on a domain the login shell does not set); and, when ~/neuxmind is an install,
# the body it was made for, the settings it recorded and its device configuration (as hexadecimal, when small).
ROSPROBE_SCRIPT = _PRELUDE + r'''say() { printf '%s=%s\n' "$1" "$2"; }
lower() { printf '%s' "$1" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'; }
echo '@@NX-INSTALL:begin@@'
setups=
for f in /opt/ros/*/setup.bash; do
  if [ -f "$f" ]; then setups="$setups $f"; fi
done
say setups "${setups# }"
count=0; best=; best_n=0
for d in /proc/[0-9]*; do
  case ${d#/proc/} in "$$"|"$PPID") continue;; esac      # not this read itself: a login may set ROS variables too
  [ -r "$d/environ" ] || continue
  vals=$(tr '\0' '\n' < "$d/environ" 2>/dev/null | grep -E '^(ROS_DISTRO|ROSKEYS)=') || continue
  case "
$vals" in *"
ROS_DISTRO="*) ;; *) continue;; esac
  count=$((count + 1))
  n=$(printf '%s\n' "$vals" | grep -c .)
  if [ "$n" -gt "$best_n" ]; then best=$vals; best_n=$n; fi
done
say run_count "$count"
if [ -n "$best" ]; then
  printf '%s\n' "$best" | while IFS= read -r line; do say "run_$(lower "${line%%=*}")" "${line#*=}"; done
fi
state=none
if [ -f "$root/.neuxmind-device-install" ] && [ ! -L "$root/.neuxmind-device-install" ] && [ ! -L "$root" ]; then
  if [ "$(first "$root/.neuxmind-device-install")" = state=installed ]; then state=installed; fi
fi
say root "$state"
if [ "$state" = installed ]; then
  say adapter "$(first "$root/adapter")"
  if [ -f "$root/ros2.env" ] && [ ! -L "$root/ros2.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case ${line%%=*} in ROS_SETUP|ROSCASE) say "rec_$(lower "${line%%=*}")" "${line#*=}";; esac
    done < "$root/ros2.env"
  fi
  c=$root/CONFIGFILE
  if [ -f "$c" ] && [ ! -L "$c" ]; then
    size=$(wc -c < "$c" | tr -d ' ')
    say config_bytes "$size"
    case $size in ''|*[!0-9]*) ;; *) if [ "$size" -le READLIMIT ]; then say config_hex "$(od -An -v -tx1 "$c" | tr -d ' \n')"; fi;; esac
  fi
fi
echo '@@NX-INSTALL:rosprobed@@'
'''.replace('ROSKEYS', '|'.join(ROS_KEYS)).replace('ROSCASE', '|'.join(ROS_KEYS)).replace('CONFIGFILE', CONFIG_FILE) \
    .replace('READLIMIT', str(CONFIG_READ_LIMIT))

SCRIPTS = {'probe': PROBE_SCRIPT, 'job': JOB_SCRIPT, 'poll': POLL_SCRIPT, 'clean': CLEAN_SCRIPT, 'rosprobe': ROSPROBE_SCRIPT}
ARITY = {'probe': 1, 'job': 5, 'poll': 2, 'clean': 1, 'rosprobe': 1}


def command(kind, *arguments):
    """``sh -s --`` and the arguments of one of ``SCRIPTS``. An argument that is not a plain word is a
    mistake in this product, never something to quote and send."""
    words = [str(argument) for argument in arguments]
    if len(words) != ARITY[kind] or not all(_ARGUMENT.fullmatch(word) for word in words):
        raise ValueError('install script argument is not a plain word')
    return ' '.join([COMMAND, *words])


def parse(text):
    """(final marker word, keys, log lines) of a script's answer. A login script may print before ours
    does: only what follows the last begin marker counts."""
    lines = text.splitlines()
    begins = [index for index, line in enumerate(lines) if line == BEGIN]
    start = begins[-1] + 1 if begins else 0
    keys, word, rest = {}, '', []
    for index in range(start, len(lines)):
        found = _WORD.match(lines[index])
        if found:
            word, rest = found.group(1), lines[index + 1:]
            break
        pair = _KEY.match(lines[index])
        if pair:
            keys[pair.group(1)] = pair.group(2)
    if not word:                                  # no begin marker in this script: the last marker line counts
        for line in reversed(lines):
            found = _WORD.match(line)
            if found:
                word = found.group(1)
                break
    log = [line[2:] for line in rest if line.startswith('| ')][-LOG_LINES:]
    return word, keys, log


def job_state(text):
    """``job_state`` of the scripts: (job id or None, exit status or None, alive)."""
    parts = (text or '').split()
    if len(parts) != 3:
        return None, None, False
    job, code, alive = parts
    return (job if _JOB_ID.fullmatch(job) else None), (int(code) if _CODE.fullmatch(code) else None), alive == 'yes'


def new_job_id():
    return secrets.token_hex(8)


class Unreachable(Exception):
    """The robot could not be reached in time, or refused the login (``refused``)."""

    def __init__(self, message, reason, refused=False, hop=None, level=None):
        super().__init__(message)
        self.reason, self.refused, self.hop, self.level = reason, refused, hop, level


class Stopped(Exception):
    pass


class DeviceSession:
    """The install's own SSH session to the robot: logs in again after a drop, within a deadline.

    ``login()`` returns a new ``device_access.SSHDevice``. A refused login (password, key, host key) is
    not repeated. Every script here may be run again after a lost answer."""

    def __init__(self, login, stop, waits=(2, 5, 10, 20, 30), sleep=time.sleep):
        self.login, self.stop, self.waits, self.sleep = login, stop, tuple(waits), sleep
        self.device, self.failures, self.logins = None, 0, 0

    def close(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:                     # closing a session that is already gone
                pass
            self.device = None

    def _pause(self, deadline):
        wait = self.waits[min(self.failures, len(self.waits) - 1)] if self.waits else 0
        self.failures += 1
        if time.monotonic() + wait > deadline:
            return False
        if self.stop.wait(wait):
            raise Stopped()
        return True

    def _connected(self, deadline):
        while self.device is None:
            if self.stop.is_set():
                raise Stopped()
            try:
                self.device = self.login()
                self.logins += 1
                self.device.stop_event = self.stop
            except device_access.ConnectError as exc:
                refused = exc.reason in ('auth', 'key', 'fingerprint')
                if refused or not self._pause(deadline):
                    raise Unreachable(str(exc), exc.reason, refused, getattr(exc, 'hop', None), getattr(exc, 'level', None)) from None
        return self.device

    def run(self, kind, *arguments, seconds=60, deadline=None):
        """(word, keys, log lines) of one script, run again after a drop until ``deadline``."""
        deadline = deadline or time.monotonic() + seconds * 3
        line = command(kind, *arguments)
        while True:
            device = self._connected(deadline)
            try:
                result = hardware_init.run_command(device, line, seconds, stdin=SCRIPTS[kind], keep='head',
                                                   limit=OUTPUT_LIMIT, redact=False)
            except _NETWORK:
                result = None
            if self.stop.is_set():
                raise Stopped()
            if result is not None and not result['timed_out']:
                answer = parse(result['stdout'])
                # A channel that closed with no exit status and no final marker is a connection that dropped:
                # the answer was lost on the way, and every script here may be run again.
                if answer[0] or result['exit_code'] not in (None, -1):
                    self.failures = 0
                    return answer
            self.close()
            if not self._pause(deadline):
                raise Unreachable('设备在规定时间内没有答复', 'timeout')

    def port_state(self, port, deadline):
        """What answers on the robot's own 127.0.0.1:<port>, seen through a direct-tcpip channel the way
        issue #17 reaches the management layer: 'free', 'busy' or 'forbidden' (the SSH service does
        not forward)."""
        while True:
            device = self._connected(deadline)
            try:
                channel = device.client.get_transport().open_channel('direct-tcpip', ('127.0.0.1', port),
                                                                    ('127.0.0.1', 0), timeout=15)
            except paramiko.ChannelException as exc:
                if exc.code == paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED:
                    return 'forbidden'
                return 'free'
            except _NETWORK:
                self.close()
                if not self._pause(deadline):
                    raise Unreachable('设备在规定时间内没有答复', 'timeout') from None
                continue
            channel.close()
            return 'busy'
