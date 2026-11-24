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
# The body (issue #21): "install" takes NX_ADAPTER = sim_quadruped (the default), sim_arm or ros2 and
# records it; "start" runs what was recorded (NX_ADAPTER there still overrides, but ros2 only on a
# ros2 install). With ros2 the runtime is connected to this device's own ROS 2:
#   * install refuses, before it creates anything, unless NX_ROS_SETUP (default
#     /opt/ros/humble/setup.bash, may be a workspace on top of it) is ROS 2 Humble, and says what it found;
#   * the venv sees the system's packages (ROS 2's Python messages need the system numpy), and install
#     fails and cleans up unless rclpy and the message packages import there;
#   * the ROS settings given to install (ROS_DOMAIN_ID, ROS_LOCALHOST_ONLY, RMW_IMPLEMENTATION,
#     FASTRTPS_DEFAULT_PROFILES_FILE, CYCLONEDDS_URI, ZENOH_SESSION_CONFIG_URI) are checked and kept in ros2.env; start runs the
#     runtime with that setup file and those settings only, checks for Humble again, and passes on
#     ros2-config.json (topic names, QoS, nav2, limits; README §3) when the operator put one into the
#     install directory. The device's nodes often run on a domain the login shell does not set.
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
#   * The runtime sends nothing out by itself, with one exception that is off until someone turns it
#     on: when the install directory holds cloud-report.json (mode 0600; README §2.3), "start" passes
#     it on and the runtime reports this device's alerts to the cloud named there, over HTTPS, every
#     five minutes. Without that file the robot needs no network at all.
# install, start, stop and remove run one at a time per copy of this script.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SELF="$HERE/$(basename "$0")"
PORT="${NX_PORT:-8790}"
ADAPTER="${NX_ADAPTER:-}"                # empty: install takes sim_quadruped, start what install recorded
PY="${NX_PYTHON:-python3}"
ROS_SETUP_DEFAULT=/opt/ros/humble/setup.bash
ROS_KEYS="ROS_DOMAIN_ID ROS_LOCALHOST_ONLY RMW_IMPLEMENTATION FASTRTPS_DEFAULT_PROFILES_FILE CYCLONEDDS_URI ZENOH_SESSION_CONFIG_URI"
ROS_CLEARED="AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION
  RMW_FASTRTPS_USE_QOS_FROM_XML RMW_FASTRTPS_PUBLICATION_MODE ROS_AUTOMATIC_DISCOVERY_RANGE ROS_STATIC_PEERS"
MAX_DOMAIN_ID=232                         # the largest ROS_DOMAIN_ID ROS 2 accepts on Linux
ROS_SETUP_TIMEOUT_S=60                    # install: how long sourcing the setup file may take
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

# ------------------------------------------------------------------ the body (issue #21)
built_in() {  # built_in <name>: a body this script installs
  case "$1" in
    sim_quadruped|sim_arm|ros2) return 0 ;;
    *) return 1 ;;
  esac
}

recorded_adapter() {  # prints the body this install was made for; an install from before the choice is simulated
  local name=sim_quadruped
  if [ -f "$ADAPTER_FILE" ]; then
    read -r name < "$ADAPTER_FILE" || true
  fi
  built_in "$name" || fail 2 "refusing: $ADAPTER_FILE names '$name', not sim_quadruped, sim_arm or ros2"
  echo "$name"
}

only_ours_can_write() {  # only_ours_can_write <path> [dir]: owned by root or this user, and no one else can write it
  local owner mode                     # (a directory may be world-writable if sticky, like /tmp: others cannot move its entries)
  read -r owner mode <<< "$(stat -L -c '%u %a' -- "$1" 2>/dev/null)" || return 1
  [ "$owner" = 0 ] || [ "$owner" = "$(id -u)" ] || return 1
  [ $(( 8#${mode:-777} & 8#022 )) -eq 0 ] && return 0
  [ "${2:-}" = dir ] && [ $(( 8#${mode} & 8#1000 )) -ne 0 ]
}

setup_file_ok() {  # setup_file_ok <file>: the file and every directory above it can be changed only by root or this user
  local path
  path="$(realpath -e -- "$1" 2>/dev/null)" || return 1
  only_ours_can_write "$path" || return 1
  while [ "$path" != / ]; do
    path="$(dirname "$path")"
    only_ours_can_write "$path" dir || return 1
  done
}

descendants() {  # descendants <pid>: the pids of its children, their children, ... (reads /proc, signals nothing)
  local queue=("$1") parent entry pid stat fields
  while [ "${#queue[@]}" -gt 0 ]; do
    parent="${queue[0]}"
    queue=("${queue[@]:1}")
    for entry in /proc/[0-9]*; do
      pid="${entry#/proc/}"
      read -r stat 2>/dev/null < "$entry/stat" || continue
      read -r -a fields <<< "${stat##*) }"              # [0] state, [1] parent
      if [ "${fields[1]:-}" = "$parent" ]; then
        echo "$pid"
        queue+=("$pid")
      fi
    done
  done
}

ros_value_ok() {  # ros_value_ok <key> <value>: a ROS setting this script passes on, holding what it may hold
  [[ "$2" != *$'\n'* ]] || return 1
  case "$1" in
    ROS_SETUP) [[ "$2" == /* ]] && [ -f "$2" ] && setup_file_ok "$2" ;;
    ROS_DOMAIN_ID) [[ "$2" =~ ^[0-9]{1,3}$ ]] && [ "$((10#$2))" -le "$MAX_DOMAIN_ID" ] ;;
    ROS_LOCALHOST_ONLY) [[ "$2" =~ ^[01]$ ]] ;;
    RMW_IMPLEMENTATION) [[ "$2" =~ ^rmw_[a-z0-9_]+$ ]] ;;
    FASTRTPS_DEFAULT_PROFILES_FILE) [[ "$2" == /* ]] && [ -f "$2" ] && [ -r "$2" ] ;;
    CYCLONEDDS_URI) [ -n "$2" ] ;;
    ZENOH_SESSION_CONFIG_URI) [[ "$2" == /* ]] && [ -f "$2" ] && [ -r "$2" ] ;;   # issue #38: a body on rmw_zenoh_cpp
    *) return 1 ;;
  esac
}

ros_distro_of() {  # ros_distro_of <setup file>: prints "<ROS_VERSION> <ROS_DISTRO>" it sets, in a clean shell
  env -i HOME="$HOME" PATH="$PATH" timeout -k 5 "$ROS_SETUP_TIMEOUT_S" bash --noprofile --norc -c \
    'set +u; . "$1" > /dev/null 2>&1; printf "%s %s\n" "${ROS_VERSION:-?}" "${ROS_DISTRO:-?}"' _ "$1"
}

ros2_precheck() {  # sets ROS_SETUP; refuses (nothing created yet) unless it is ROS 2 Humble and the settings are usable
  local version distro key value candidate others=""
  ROS_SETUP="${NX_ROS_SETUP:-$ROS_SETUP_DEFAULT}"
  [[ "$ROS_SETUP" == /* ]] || fail 2 "refusing: NX_ROS_SETUP must be an absolute path, got '$ROS_SETUP'"
  if [ ! -f "$ROS_SETUP" ]; then
    shopt -s nullglob
    for candidate in "$(dirname "$(dirname "$ROS_SETUP")")"/*/setup.bash; do
      others="$others $(basename "$(dirname "$candidate")")"
    done
    shopt -u nullglob
    [ -z "$others" ] || fail 2 "refusing: ROS 2 Humble not found ($ROS_SETUP does not exist); found${others}, but only ROS 2 Humble is supported"
    fail 2 "refusing: ROS 2 Humble not found ($ROS_SETUP does not exist)"
  fi
  ros_value_ok ROS_SETUP "$ROS_SETUP" \
    || fail 2 "refusing: $ROS_SETUP or a directory above it is writable by others than root and $(id -un); it would run as $(id -un)"
  read -r version distro <<< "$(ros_distro_of "$ROS_SETUP")" || true
  version="${version:-?}"
  distro="${distro:-?}"
  if [ "$version" != 2 ] || [ "$distro" != humble ]; then
    fail 2 "refusing: $ROS_SETUP sets up ROS $version $distro; only ROS 2 Humble is supported"
  fi
  for key in $ROS_KEYS; do
    value="${!key-}"
    [ -n "$value" ] || continue
    ros_value_ok "$key" "$value" || fail 2 "refusing: $key='$value' is not a usable value"
  done
}

ros2_settings() {  # the ROS settings given to install, one KEY=VALUE per line
  local key
  for key in $ROS_KEYS; do
    if [ -n "${!key-}" ]; then
      echo "$key=${!key}"
    fi
  done
}

enter_ros2() {  # in a subshell: this install's ROS 2 Humble environment, nothing inherited; messages to stderr
  local line key value setup="" settings=()
  # shellcheck disable=SC2086  # word lists
  unset NEUXMIND_ROS2_CONFIG $ROS_CLEARED $ROS_KEYS
  while IFS= read -r line || [ -n "$line" ]; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$line" in
      *=*) ;;
      *) echo "refusing: $ROS_ENV holds a line that is not KEY=VALUE" >&2; exit 1 ;;
    esac
    case " ROS_SETUP $ROS_KEYS " in
      *" $key "*) ;;
      *) echo "refusing: $ROS_ENV holds an unknown line" >&2; exit 1 ;;
    esac
    if ! ros_value_ok "$key" "$value"; then
      echo "refusing: $ROS_ENV: $key='$value' is not usable (a setup file must exist and be writable only by root or $(id -un))" >&2
      exit 1
    fi
    if [ "$key" = ROS_SETUP ]; then setup="$value"; else settings+=("$key=$value"); fi
  done < "$ROS_ENV"
  [ -n "$setup" ] || { echo "refusing: $ROS_ENV names no ROS setup file" >&2; exit 1; }
  set +eu +o pipefail                     # the ROS setup files are not written for errexit or nounset
  # shellcheck disable=SC1090
  . "$setup" > /dev/null
  set -eu -o pipefail
  if [ "${ROS_VERSION:-}" != 2 ] || [ "${ROS_DISTRO:-}" != humble ]; then
    echo "refusing: $setup now sets up ROS ${ROS_VERSION:-?} ${ROS_DISTRO:-?}; only ROS 2 Humble is supported" >&2
    exit 1
  fi
  if [ "${#settings[@]}" -gt 0 ]; then
    export "${settings[@]}"
  fi
  if [ -f "$ROS2_CONFIG" ] && [ ! -L "$ROS2_CONFIG" ]; then
    export NEUXMIND_ROS2_CONFIG="$ROS2_CONFIG"
  fi
}

ros2_record() {  # install, ros2: keep the setup file and settings, then check rclpy imports in that environment
  ( umask 077; { echo "ROS_SETUP=$ROS_SETUP"; ros2_settings; } > "$ROS_ENV" ) \
    && ( enter_ros2; cd "$ROOT"; exec "$VENV/bin/python" - <<'PY'
import importlib
import os
import sys

missing = []
for name in ("rclpy", "rclpy.qos", "geometry_msgs.msg", "nav_msgs.msg", "sensor_msgs.msg"):
    try:
        importlib.import_module(name)
    except Exception as error:            # ImportError, or a compiled module built for another Python
        missing.append(f"{name}: {error}")
if missing:
    sys.stderr.write(f"rclpy and the message packages do not import with Python {sys.version.split()[0]} "
                     "in this ROS environment (ROS 2 Humble is built for the system python3):\n  "
                     + "\n  ".join(missing) + "\n")
    sys.exit(1)
from neuxmind_runtime.adapters.ros2 import check_distro

check_distro(os.environ)
try:
    importlib.import_module("nav2_msgs.action")
    print("navigation: nav2_msgs found, base.navigate_to is declared (ros2-config.json \"nav2\": false turns it off)")
except ImportError:
    print("navigation: nav2_msgs not installed, so there is no base.navigate_to; everything else works")
PY
    )
}

ros2_config_check() {  # start, ros2: the operator's device configuration, when there is one, is a plain file here
  if [ -L "$ROS2_CONFIG" ]; then
    fail 1 "refusing: $ROS2_CONFIG is a symbolic link; put the file itself into the install directory"
  fi
  [ -e "$ROS2_CONFIG" ] || return 0
  [ -f "$ROS2_CONFIG" ] || fail 1 "refusing: $ROS2_CONFIG is not a regular file"
  # A simulated body is never device evidence and skips the safety-limit requirement (issue #22): an
  # install on a device is for its real ROS 2. (Other mistakes in the file stop the adapter itself.)
  "$VENV/bin/python" -I - "$ROS2_CONFIG" <<'PY' || exit 1
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        config = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)                          # the adapter reports it, with the reason, when the runtime starts
flags = [key for key in ("simulated", "use_sim_time") if isinstance(config, dict) and config.get(key) is True]
if flags:
    sys.stderr.write(f"refusing: {sys.argv[1]} sets {' and '.join(flags)}: a simulated body skips the safety limits "
                     "and is not device evidence; this install is for the device's own ROS 2\n")
    sys.exit(1)
PY
  echo "ROS 2 device configuration: $ROS2_CONFIG"
}

populate() {  # populate <spec wheel> <runtime wheel>; every step must succeed; the marker says "installed" last
  local venv_options=()
  if [ "$ADAPTER" = ros2 ]; then
    venv_options=(--system-site-packages)  # ROS 2's Python messages import the system numpy
  fi
  echo "state=installing" > "$MARKER" \
    && mkdir -m 700 -- "$STATE" \
    && "$PY" -m venv ${venv_options[@]+"${venv_options[@]}"} "$VENV" \
    && "$VENV/bin/pip" install --quiet --isolated --no-cache-dir --no-index --no-deps --disable-pip-version-check "$1" "$2" \
    && ( umask 077; "$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(36))' > "$TOKEN_FILE" ) \
    && echo "$ADAPTER" > "$ADAPTER_FILE" \
    && { [ "$ADAPTER" != ros2 ] || ros2_record; } \
    && "$VENV/bin/python" -c 'import sys, neuxmind_runtime; print("python", sys.version.split()[0]); print("installed", neuxmind_runtime.__file__)' \
    && echo "state=installed" > "$MARKER.next" \
    && mv -f -- "$MARKER.next" "$MARKER"
}

install() {
  local spec runtime parent settings
  ADAPTER="${ADAPTER:-sim_quadruped}"
  built_in "$ADAPTER" || fail 2 "refusing: NX_ADAPTER=$ADAPTER; install takes sim_quadruped, sim_arm or ros2"
  if [ "$ADAPTER" = ros2 ]; then
    ros2_precheck
    settings="$(ros2_settings | tr '\n' ' ')"
    echo "body: ros2, ROS 2 humble ($ROS_SETUP)"
    echo "ROS settings: ${settings:-none given (ROS defaults: domain 0, the default RMW)}"
    if [ -z "${ROS_LOCALHOST_ONLY-}" ]; then
      echo "note: ROS_LOCALHOST_ONLY is not set, so the runtime's ROS node is discoverable on the device's networks, like its other nodes"
    fi
  fi
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
  local pid child installed_as nohup_bin setsid_bin
  if pid="$(running_pid)"; then
    echo "already running: pid $pid"
    return 0
  fi
  is_install || fail 2 "not installed: run '$SELF install' first"
  installed_as="$(recorded_adapter)" || exit 2
  ADAPTER="${ADAPTER:-$installed_as}"
  if [ "$ADAPTER" = ros2 ] && [ "$installed_as" != ros2 ]; then
    fail 2 "refusing: this install is for $installed_as; a ROS 2 body needs an install of its own (NX_ADAPTER=ros2 $SELF install)"
  fi
  if [ "$ADAPTER" = ros2 ]; then
    ros2_config_check
  fi
  rm -f -- "$PID_FILE"
  if port_in_use; then
    fail 1 "127.0.0.1:$PORT is already in use; the runtime was not started"
  fi
  unset NEUXMIND_CLOUD_CONFIG            # only this install's own file decides whether alerts are reported
  if [ -e "$CLOUD_CONFIG" ] || [ -L "$CLOUD_CONFIG" ]; then   # the runtime checks it (0600, https, no link) and starts either way
    export NEUXMIND_CLOUD_CONFIG="$CLOUD_CONFIG"
    echo "alert reports: $CLOUD_CONFIG"
  fi
  cd "$ROOT"
  nohup_bin="$(command -v nohup)"         # looked up here: the ROS setup file changes PATH
  setsid_bin="$(command -v setsid)"
  # A subshell that enters the ROS 2 environment when the body is ros2, then becomes the runtime (exec
  # keeps its pid); a refusal on the way ends up in the log, which a failed start prints.
  ( if [ "$ADAPTER" = ros2 ]; then enter_ros2; fi
    BRAIN_EDGE_TOKEN="$(cat "$TOKEN_FILE")" exec "$nohup_bin" "$setsid_bin" "$ENTRY" serve \
      --adapter "$ADAPTER" --host 127.0.0.1 --port "$PORT" --state-dir "$STATE" \
      --log-file "$LOG"
  ) >> "$LOG" 2>&1 < /dev/null 9<&- &
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
  # Still the subshell (sourcing the ROS setup took too long): it would become the runtime later, unseen
  # by stop. It is this script's own child and was alive a moment ago (bash reaps it only once it has
  # exited), so the pid is still its. Freeze it first (no exec, no new children), then end it together
  # with whatever the setup file started (found through /proc, children of children included).
  if still_there "$child" && ! is_runtime "$child"; then
    kill -s STOP -- "$child" 2>/dev/null || true
    for pid in $(descendants "$child") "$child"; do
      kill -s KILL -- "$pid" 2>/dev/null || true
    done
    echo "the ROS 2 environment was not ready within $(( START_POLLS / 2 )) s" >> "$LOG"
  fi
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
    report = status.get("alert_report") or {}
    summary["alert_report"] = {key: report.get(key) for key in ("enabled", "url", "unsent", "last_error", "reason")}
    link = status.get("cloud_link") or {}                                                   # issue #43
    summary["cloud_link"] = {key: link.get(key) for key in ("enabled", "state", "host", "last_error", "reason")}
    summary["motion_blocked"] = (status.get("safety_limits") or {}).get("motion_blocked")   # issue #22
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
CLOUD_CONFIG="$ROOT/cloud-report.json"   # optional, written by the operator: where alerts are reported
ADAPTER_FILE="$ROOT/adapter"             # the body install was made for (issue #21)
ROS_ENV="$ROOT/ros2.env"                 # ros2: the setup file and ROS settings start uses, written by install
ROS2_CONFIG="$ROOT/ros2-config.json"     # ros2, optional, written by the operator: topics, QoS, nav2, limits

case "$1" in
  install) lock; install ;;
  start) lock; start ;;
  stop) lock; stop ;;
  remove) lock; remove ;;
  status) status ;;
  manifest) is_install || fail 2 "not installed: run '$SELF install' first"; call /v1/manifest ;;
esac
