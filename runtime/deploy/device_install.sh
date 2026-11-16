#!/usr/bin/env bash
# Put the NeuxMind edge runtime on a robot, in the current user's home directory (README §0.4).
#
# No sudo, no system files, no network: only the two wheels that sit next to this script
# (neuxmind_spec-*.whl and neuxmind_runtime-*.whl). Removing the install directory undoes it.
#
#   device_install.sh install    create the install directory (venv, state, token file) from the wheels
#   device_install.sh start      start the runtime on 127.0.0.1:$NX_PORT, detached from this shell
#   device_install.sh status     process and a short /v1/status summary (exit 1 when not running)
#   device_install.sh manifest   print /v1/manifest
#   device_install.sh stop       stop the runtime
#   device_install.sh remove     stop the runtime and delete the install directory
#
# Environment: NX_ROOT (install directory, default ~/neuxmind), NX_PORT (8790),
# NX_ADAPTER (sim_quadruped), NX_PYTHON (python3).
#
# A robot runs other software under the same user, so the script keeps to these rules:
#   * It creates, changes and deletes nothing outside the install directory, which must resolve
#     (symlinks followed) to a place below $HOME. A directory counts as made by this script only when
#     it holds the script's marker file (.neuxmind-device-install); "stop" and "remove" leave any
#     other directory, and whatever runs from it, alone.
#   * It only ever signals a process whose arguments name this install's runtime; a pid file left
#     over from before a reboot is never trusted on its own.
#   * The default adapter is the simulated body: it opens no device and cannot move the robot.
#   * The gateway is plain HTTP, so it listens on loopback only; reach it through an SSH tunnel.
# install, start, stop and remove run one at a time per copy of this script.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SELF="$HERE/$(basename "$0")"
PORT="${NX_PORT:-8790}"
ADAPTER="${NX_ADAPTER:-sim_quadruped}"
PY="${NX_PYTHON:-python3}"
START_POLLS=120           # x 0.5 s: how long start waits for the gateway to answer
STOP_POLLS=20             # x 0.5 s: how long stop waits before SIGKILL
KILL_POLLS=10             # x 0.2 s: how long stop waits after SIGKILL
LOCK_WAIT_S=120           # how long a command waits for another one to finish
CALL_TIMEOUT_S=5          # one request to the gateway

fail() {  # fail <exit status> <message>
  echo "$2" >&2
  exit "$1"
}

usage() {
  fail 2 "usage: $SELF install|start|status|manifest|stop|remove"
}

resolve_root() {  # sets ROOT to the physical install directory, or refuses anything not below $HOME
  local home
  [ -n "${HOME:-}" ] || fail 2 "refusing: HOME is not set"
  home="$(realpath -e -- "$HOME" 2>/dev/null)" || fail 2 "refusing: HOME ($HOME) is not an existing directory"
  ROOT="$(realpath -m -- "${NX_ROOT:-$HOME/neuxmind}" 2>/dev/null)" || fail 2 "refusing: cannot resolve the install directory"
  case "$ROOT" in
    "$home"/?*) ;;
    *) fail 2 "refusing: the install directory ($ROOT) must be below $home" ;;
  esac
}

lock() {  # one command at a time; the lock is on this script file, so nothing is created
  exec 9<"$SELF"
  flock -w "$LOCK_WAIT_S" 9 || fail 2 "another device_install.sh command is still running"
}

one_wheel() {  # one_wheel <name prefix>  → path of the single matching wheel
  local found=()
  shopt -s nullglob
  found=("$HERE"/"$1"-*.whl)
  shopt -u nullglob
  [ "${#found[@]}" -eq 1 ] || fail 2 "need exactly one $1 wheel next to this script, found ${#found[@]}"
  echo "${found[0]}"
}

ours() {  # this script made the directory: its marker file is there (state=installing, then state=installed)
  [ -f "$MARKER" ]
}

is_install() {  # a finished install made by this script
  local state=""
  ours || return 1
  read -r state < "$MARKER" || true
  [ "$state" = "state=installed" ] && [ -x "$ENTRY" ] && [ -f "$TOKEN_FILE" ]
}

is_empty_directory() {
  [ -d "$1" ] && [ -z "$(ls -A -- "$1" 2>/dev/null)" ]
}

wipe_root() {  # delete our install directory; the marker goes last, so an interrupted wipe is still ours
  local entry
  shopt -s nullglob dotglob
  for entry in "$ROOT"/*; do
    [ "$entry" = "$MARKER" ] || rm -rf -- "$entry"
  done
  shopt -u nullglob dotglob
  rm -f -- "$MARKER"
  rmdir -- "$ROOT"
}

is_runtime() {  # is_runtime <pid>: a live process whose arguments name this install's runtime
  local argument
  [ -r "/proc/$1/cmdline" ] || return 1
  while IFS= read -r -d '' argument; do
    [ "$argument" != "$ENTRY" ] || return 0
  done < "/proc/$1/cmdline"
  return 1
}

running_pid() {  # prints the pid of this install's running runtime; fails when there is none
  local pid=""
  ours || return 1                                # a runtime in a directory we did not make is not ours to touch
  [ -f "$PID_FILE" ] || return 1
  read -r pid < "$PID_FILE" || true
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  [ "$pid" -gt 1 ] || return 1
  is_runtime "$pid" || return 1
  echo "$pid"
}

process_fields() {  # process_fields <pid>: sets FIELDS to /proc/<pid>/stat after the command name
  local stat                                      # FIELDS[0] state, [1] parent, [2] process group
  FIELDS=()
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  read -r -a FIELDS <<< "${stat##*) }"
}

still_there() {  # still_there <pid>: exists and is not a zombie; reads /proc, signals nothing
  process_fields "$1" && [ "${FIELDS[0]:-Z}" != Z ]
}

signal_runtime() {  # signal_runtime <signal> <pid>: its process group when it leads one, else the process
  is_runtime "$2" || return 0
  process_fields "$2" || return 0
  if [ "${FIELDS[2]:-}" = "$2" ]; then
    kill -s "$1" -- "-$2" 2>/dev/null || true
  else
    kill -s "$1" -- "$2" 2>/dev/null || true
  fi
}

call() {  # call <path>  → response body; authenticated, loopback only, the token is never printed
  "$VENV/bin/python" - "$PORT" "$TOKEN_FILE" "$1" "$CALL_TIMEOUT_S" <<'PY'
import sys
import urllib.request

port, token_file, path, timeout = sys.argv[1:5]
try:
    token = open(token_file).read().strip()
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Authorization': 'Bearer ' + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=float(timeout)) as response:
        body = response.read().decode()
except Exception as error:               # refused, timed out, unreadable token: one line, no traceback
    sys.stderr.write(f'the gateway on 127.0.0.1:{port} did not answer: {error}\n')
    sys.exit(1)
sys.stdout.write(body)
PY
}

port_in_use() {
  "$VENV/bin/python" - "$PORT" <<'PY'
import socket
import sys

with socket.socket() as probe:
    probe.settimeout(2)
    sys.exit(0 if probe.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)
PY
}

populate() {  # populate <spec wheel> <runtime wheel>; every step must succeed; the marker says "installed" last
  echo "state=installing" > "$MARKER" \
    && mkdir -m 700 -- "$STATE" \
    && "$PY" -m venv "$VENV" \
    && "$VENV/bin/pip" install --quiet --isolated --no-cache-dir --no-index --no-deps --disable-pip-version-check "$1" "$2" \
    && ( umask 077; "$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(36))' > "$TOKEN_FILE" ) \
    && "$VENV/bin/python" -c 'import sys, neuxmind_runtime; print("python", sys.version.split()[0]); print("installed", neuxmind_runtime.__file__)' \
    && echo "state=installed" > "$MARKER.next" \
    && mv -f -- "$MARKER.next" "$MARKER"
}

install() {
  local spec runtime parent
  spec="$(one_wheel neuxmind_spec)"
  runtime="$(one_wheel neuxmind_runtime)"
  if [ -e "$ROOT" ]; then
    if ours && ! is_install; then
      echo "replacing an unfinished install in $ROOT"
      wipe_root
    elif is_empty_directory "$ROOT"; then      # an install interrupted before it wrote its marker
      rmdir -- "$ROOT"
    else
      fail 2 "refusing: $ROOT already exists"
    fi
  fi
  parent="$(dirname "$ROOT")"
  [ -d "$parent" ] || fail 2 "refusing: $parent does not exist"
  mkdir -m 700 -- "$ROOT" || fail 2 "cannot create $ROOT"
  if ! populate "$spec" "$runtime"; then
    wipe_root                            # a failed install leaves nothing behind
    fail 1 "install failed; $ROOT was removed again"
  fi
  echo "token file: $TOKEN_FILE (mode $(stat -c %a "$TOKEN_FILE"))"
}

start() {
  local pid child
  if pid="$(running_pid)"; then
    echo "already running: pid $pid"
    return 0
  fi
  is_install || fail 2 "not installed: run '$SELF install' first"
  rm -f -- "$PID_FILE"
  if port_in_use; then
    fail 1 "127.0.0.1:$PORT is already in use; the runtime was not started"
  fi
  cd "$ROOT"
  BRAIN_EDGE_TOKEN="$(cat "$TOKEN_FILE")" nohup setsid "$ENTRY" serve \
    --adapter "$ADAPTER" --host 127.0.0.1 --port "$PORT" --state-dir "$STATE" \
    >> "$LOG" 2>&1 < /dev/null 9<&- &
  child=$!
  echo "$child" > "$PID_FILE"
  # Right after the fork the child is still this script, not yet the runtime, so only its presence is
  # checked while waiting; success needs both: the child has become the runtime and the gateway answers.
  for _ in $(seq 1 "$START_POLLS"); do
    still_there "$child" || break
    if is_runtime "$child" && call /v1/status >/dev/null 2>&1; then
      echo "started: pid $child, 127.0.0.1:$PORT, adapter $ADAPTER"
      return 0
    fi
    sleep 0.5
  done
  stop >/dev/null
  echo "runtime did not come up; last log lines:" >&2
  tail -n 30 "$LOG" >&2
  exit 1
}

status() {
  local pid body
  if pid="$(running_pid)"; then
    echo "running: pid $pid"
  else
    echo "not running"
    return 1
  fi
  body="$(call /v1/status)" || return 1
  printf '%s' "$body" | "$VENV/bin/python" -c '
import json
import sys

try:
    status = json.load(sys.stdin)
    sandbox = status.get("sandbox") or {}
    summary = {key: status.get(key) for key in ("name", "simulated", "mode", "estop_latched", "runtime_version")}
    summary["sandbox"] = {key: sandbox.get(key) for key in ("level", "mode", "privilege_drop", "python_usable", "pids_cgroup")}
except (ValueError, AttributeError) as error:     # something else answered on this port
    sys.stderr.write(f"the answer was not a runtime status: {error}\n")
    sys.exit(1)
print(json.dumps(summary, ensure_ascii=False))' || return 1
}

stop() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "not running"
    if ours; then
      rm -f -- "$PID_FILE"               # only ever our own stale pid file
    fi
    return 0
  fi
  signal_runtime TERM "$pid"
  for _ in $(seq 1 "$STOP_POLLS"); do
    is_runtime "$pid" || break
    sleep 0.5
  done
  if is_runtime "$pid"; then
    signal_runtime KILL "$pid"
    for _ in $(seq 1 "$KILL_POLLS"); do
      is_runtime "$pid" || break
      sleep 0.2
    done
  fi
  if is_runtime "$pid"; then
    fail 1 "could not stop pid $pid"
  fi
  rm -f -- "$PID_FILE"
  echo "stopped pid $pid"
}

remove() {
  if [ ! -e "$ROOT" ]; then
    echo "not installed: $ROOT does not exist"
    return 0
  fi
  if is_empty_directory "$ROOT"; then
    echo "not installed: $ROOT is empty"
    return 0
  fi
  ours || fail 2 "refusing: $ROOT was not made by this script"
  stop
  wipe_root
  echo "removed $ROOT"
}

[ "$#" -eq 1 ] || usage
case "$1" in
  install|start|status|manifest|stop|remove) ;;
  *) usage ;;
esac

resolve_root
VENV="$ROOT/venv"
ENTRY="$VENV/bin/neuxmind-runtime"
STATE="$ROOT/state"
TOKEN_FILE="$ROOT/edge-token"
PID_FILE="$ROOT/runtime.pid"
LOG="$ROOT/runtime.log"
MARKER="$ROOT/.neuxmind-device-install"

case "$1" in
  install) lock; install ;;
  start) lock; start ;;
  stop) lock; stop ;;
  remove) lock; remove ;;
  status) status ;;
  manifest) is_install || fail 2 "not installed: run '$SELF install' first"; call /v1/manifest ;;
esac
