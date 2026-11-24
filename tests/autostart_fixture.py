"""A stand-in for a robot on which the console turns autostart on and off (issue #20). Test only;
nothing here reaches a real machine, and nothing here runs as root.

``AutostartDevice`` is issue #19's ``InstallDevice`` plus:

* the product's two account scripts (``runtime_autostart_device.SCRIPTS``, compared byte for byte, plain
  arguments only) run for real with ``sh -s``;
* the product's two root scripts are recognised only in the exact form ``root_command`` builds
  (``LC_ALL=C [sudo -n | sudo -S -p ''] sh -c 'exec </dev/null; <script>' nx-autostart <words>``). sudo is
  played by the fixture: ``sudo`` = 'password' (the default: a password is needed, ``sudo_password``,
  different from the SSH login's), 'nopasswd', 'none' (no sudo at all), 'denied' (not in sudoers),
  'tty' (sudo wants a terminal) or 'root' (the login is root). What each root command got on standard
  input is kept in ``sudo_inputs``. The script then runs with ``sh`` as this computer's user, with
  ``id -u`` saying 0 and the two system directories moved into the fixture (``/etc/systemd/system`` →
  ``etc``, ``/run/systemd/system`` → ``run_systemd``); that path is the only change made to it;
* stand-ins for ``id``, ``getent``, ``sudo -n true`` and ``systemctl``: the last keeps its state in
  ``~/.fixture/systemd`` and, for ``start`` / ``restart``, runs the service file's ``ExecStartPre`` and
  ``ExecStart`` (with its ``Environment`` and ``ConditionPathExists``) the way systemd would, as the
  login account; every call is appended to ``~/.fixture/systemd/calls``;
* ``reboot()``: the runtime ends, every SSH session drops, and the "boot" starts the service when it is
  enabled, as systemd does at boot.
"""
import os
import secrets
import shlex
import shutil
import time

from brainos import runtime_autostart_device as auto
from install_fixture import SCRIPT_SECONDS, InstallDevice, _shim
from upload_fixture import SHELL

UID = 1000
_ACCOUNT_SCRIPTS = {text: kind for kind, text in auto.SCRIPTS.items()}
_ROOT_SCRIPTS = {'exec </dev/null; ' + text: kind for kind, text in auto.ROOT_SCRIPTS.items()}
WRONG_PASSWORD = b'Sorry, try again.\n\nsudo: no password was provided\nsudo: 1 incorrect password attempt\n'

SYSTEMCTL = r'''S=$HOME/.fixture/systemd
U=ETC/neuxmind-runtime.service
W=ETC/multi-user.target.wants/neuxmind-runtime.service
mkdir -p "$S"
echo "$*" >> "$S/calls"
get() { if [ -f "$S/$1" ]; then cat "$S/$1"; else echo "$2"; fi; }
put() { printf '%s\n' "$2" > "$S/$1"; }
state() { put active "$1"; put sub "$2"; put result "$3"; }
as_root() { [ -n "${NX_FIXTURE_ROOT:-}" ] || { echo "Failed: Access denied" >&2; exit 4; }; }
field() { sed -n "s/^$1=//p" "$U" | head -n 1; }
start_unit() {
  [ -f "$U" ] || { echo "Failed to start neuxmind-runtime.service: Unit neuxmind-runtime.service not found." >&2; return 5; }
  cond=$(field ConditionPathExists)
  if [ -n "$cond" ] && [ ! -e "$cond" ]; then state inactive dead success; return 0; fi
  env_line=$(field Environment); pre=$(field ExecStartPre); main=$(field ExecStart); pid_file=$(field PIDFile)
  state activating start success
  # shellcheck disable=SC2086  # the fields are plain words
  if (unset NX_FIXTURE_ROOT; env $env_line $pre && env $env_line $main) >> "$S/journal" 2>&1 </dev/null; then
    put pid "$(cat "$pid_file" 2>/dev/null)"; state active running success
  else
    put restarts $(( $(get restarts 0) + 1 )); state failed failed exit-code
  fi
}
stop_unit() {
  pid=$(get pid 0)
  case $pid in ''|0|*[!0-9]*) ;; *) kill -TERM "$pid" 2>/dev/null; while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done;; esac
  put pid 0
  state inactive dead success
}
block=yes
if [ "$1" = --no-block ]; then block=no; shift; fi
command=$1; shift
case $command in
  daemon-reload) as_root ;;
  enable) as_root; mkdir -p "$(dirname "$W")"; ln -sf "$U" "$W"; echo "Created symlink $W -> $U." ;;
  disable)
    as_root
    if [ "$1" = --now ]; then shift; rm -f "$W"; stop_unit; else rm -f "$W"; fi ;;
  is-enabled)
    if [ -L "$W" ] && [ -f "$U" ]; then echo enabled; elif [ -f "$U" ]; then echo disabled; exit 1;
    else echo "Failed to get unit file state for neuxmind-runtime.service: No such file or directory" >&2; exit 1; fi ;;
  is-active) a=$(get active inactive); echo "$a"; [ "$a" = active ] ;;
  show)
    printf 'ActiveState=%s\nSubState=%s\nResult=%s\nNRestarts=%s\nMainPID=%s\n' \
      "$(get active inactive)" "$(get sub dead)" "$(get result success)" "$(get restarts 0)" "$(get pid 0)" ;;
  start|restart)
    as_root
    [ "$command" = restart ] && stop_unit
    if [ "$block" = no ]; then (start_unit) >/dev/null 2>&1 </dev/null & else start_unit; fi ;;
  stop) as_root; stop_unit ;;
  reset-failed) as_root; put restarts 0 ;;
  *) echo "fixture systemctl: $command is not played here" >&2; exit 1 ;;
esac
'''


class AutostartDevice(InstallDevice):
    def __init__(self, sudo='password', systemd=True, **options):
        super().__init__(**options)
        self.sudo_mode, self.systemd = sudo, systemd
        self.sudo_password = secrets.token_urlsafe(18)
        self.etc, self.run_systemd = os.path.join(self.base, 'etc'), os.path.join(self.base, 'run-systemd')
        os.makedirs(self.etc)
        if systemd:
            os.makedirs(self.run_systemd)
        self.root_runs, self.sudo_inputs, self.account_runs = [], [], []
        for tool in ('mktemp', 'ln'):               # the root script and the systemctl stand-in use them
            found = shutil.which(tool)
            if found and not os.path.lexists(os.path.join(self.bin, tool)):
                os.symlink(found, os.path.join(self.bin, tool))
        user = 'root' if sudo == 'root' else self.username
        uid = 0 if sudo == 'root' else UID
        _shim(self.bin, 'id', 'if [ -n "${NX_FIXTURE_ROOT:-}" ] || [ %d = 0 ]; then u=0; n=root; else u=%d; n=%s; fi\n'
              'case "$1" in -u) echo "$u";; -un) echo "$n";; *) echo "uid=$u($n)";; esac\n' % (uid, uid, user))
        _shim(self.bin, 'getent', '[ "$1" = passwd ] || exit 2\ncase "$2" in\n'
              '  %s) echo "%s:x:%d:%d:Robot:%s:/bin/bash";;\n  *) exit 2;;\nesac\n' % (user, user, uid, uid, self.local('')))
        if sudo != 'none':
            _shim(self.bin, 'sudo', '[ "$1" = -n ] && [ "$2" = true ] || { echo "fixture sudo: only -n true here" >&2; exit 1; }\n' +
                  ('exit 0\n' if sudo in ('nopasswd', 'root') else 'echo "sudo: a password is required" >&2\nexit 1\n'))
        if systemd:
            _shim(self.bin, 'systemctl', SYSTEMCTL.replace('ETC', self.etc))
        self.user = user

    # --- what a test reads and steers ---------------------------------------------------------

    def unit(self):
        path = os.path.join(self.etc, auto.UNIT_NAME)
        try:
            with open(path) as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def write_unit(self, text):
        with open(os.path.join(self.etc, auto.UNIT_NAME), 'w') as handle:
            handle.write(text)

    def enabled(self):
        return os.path.islink(os.path.join(self.etc, 'multi-user.target.wants', auto.UNIT_NAME))

    def service(self, name, default=''):
        data = self.read('.fixture/systemd/' + name)
        return data.decode().strip() if data else default

    def systemctl_calls(self):
        data = self.read('.fixture/systemd/calls')
        return data.decode().splitlines() if data else []

    def journal(self):
        data = self.read('.fixture/systemd/journal')
        return data.decode() if data else ''

    def wait_service(self, state='active', seconds=20):
        deadline = time.monotonic() + seconds
        while self.service('active', 'inactive') != state:
            if time.monotonic() > deadline:
                raise AssertionError('the service did not become %s: %s' % (state, self.journal()[-500:]))
            time.sleep(0.05)

    def reboot(self):
        """Power off and on again: the runtime and every SSH session end; at boot the service starts when it
        is enabled (and its condition holds), as systemd does."""
        marker = self.local('neuxmind/.fake-running')
        pid = self.read('neuxmind/runtime.pid')
        if os.path.exists(marker):
            os.remove(marker)
        if pid and pid.strip().isdigit():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(int(pid.strip()), 0)
                except OSError:
                    break
                time.sleep(0.05)
        self.drop_sessions()
        for name in ('active', 'sub', 'pid'):
            path = self.local('.fixture/systemd/' + name)
            if os.path.exists(path):
                os.remove(path)
        if self.enabled():
            code, out, err = self._run(['sh', os.path.join(self.bin, 'systemctl'), 'start', auto.UNIT_NAME],
                                       env={'NX_FIXTURE_ROOT': '1'})
            if code:
                raise AssertionError('boot: %r %r' % (out, err))

    # --- the device side ----------------------------------------------------------------------

    def handle(self, command, script):
        root = self._root(command, script)
        if root is not None:
            return root
        kind = _ACCOUNT_SCRIPTS.get(script.decode('utf-8', 'replace'))
        if kind is None:
            return super().handle(command, script)
        words = command.split(' ')
        try:
            ok = auto.command(kind, *words[3:]) == command
        except ValueError:
            ok = False
        if not ok:
            self.refused.append(command)
            return 126, b'', b'fixture: not an autostart script command\n'
        self.account_runs.append((kind, tuple(words[3:])))
        return self._run([*SHELL, '-s', '--', *words[3:]], self._moved(script.decode()).encode())

    def _moved(self, text):
        """The script with the two system directories moved into the fixture; nothing else changes."""
        text = text.replace('/etc/systemd/system', self.etc)
        return text.replace('/run/systemd/system', self.run_systemd if self.systemd else os.path.join(self.base, 'no-systemd'))

    def _root(self, command, stdin):
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if 'sh' not in parts or parts.index('sh') + 2 >= len(parts):
            return None
        at = parts.index('sh')
        kind = _ROOT_SCRIPTS.get(parts[at + 2]) if parts[at + 1] == '-c' else None
        if kind is None:
            return None
        mode = {('LC_ALL=C',): 'root', ('LC_ALL=C', 'sudo', '-n'): 'nopasswd',
                ('LC_ALL=C', 'sudo', '-S', '-p', ''): 'password'}.get(tuple(parts[:at]))
        words = parts[at + 4:]
        try:
            ok = mode is not None and parts[at + 3] == auto.ROOT_NAME and auto.root_command(kind, mode, *words) == command
        except ValueError:
            ok = False
        if not ok:
            self.refused.append(command)
            return 126, b'', b'fixture: not an autostart root command\n'
        self.root_runs.append((kind, mode, tuple(words)))
        self.sudo_inputs.append(stdin)
        refusal = self._sudo(mode, stdin)
        if refusal is not None:
            return refusal
        text = self._moved(parts[at + 2])
        env = {'NX_FIXTURE_ROOT': '1'}
        if self.sudo_mode != 'root':
            env['SUDO_USER'] = self.username
        return self._run([*SHELL, '-c', text, auto.ROOT_NAME, *words], b'', env=env, seconds=SCRIPT_SECONDS)

    def _sudo(self, mode, stdin):
        """What sudo answers before running anything, or None when it runs the script."""
        if mode == 'root':
            return None if self.sudo_mode == 'root' else (2, b'', b'fixture: this login is not root\n')
        if self.sudo_mode == 'none':
            return 127, b'', b'sh: 1: sudo: not found\n'
        if self.sudo_mode == 'tty':
            return 1, b'', b'sudo: a terminal is required to read the password; either use the -S option to read from standard input or configure an askpass helper\n'
        if self.sudo_mode in ('nopasswd', 'root'):
            return None
        if mode == 'nopasswd':
            return 1, b'', b'sudo: a password is required\n'
        typed = stdin.split(b'\n', 1)[0].decode('utf-8', 'replace')
        if typed != self.sudo_password:
            return 1, b'', WRONG_PASSWORD
        if self.sudo_mode == 'denied':
            return 1, b'', ('%s is not in the sudoers file.  This incident will be reported.\n' % self.username).encode()
        return None
