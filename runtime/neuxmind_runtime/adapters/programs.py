"""On-body programs as actions (issue #38).

Some bodies bring their own navigation: programs that already run on the robot's computer, read its
sensors and drive it ("go to the meeting room", "climb the stairs", "walk around without a map").
This module lets an adapter offer them as the standard actions ``base.navigate_to_place`` and
``base.explore``: it starts the program, watches it, decides from its exit code and output whether
it did what was asked, and ends it when the action is cancelled or the robot is stopped.

It is plain process handling: no ROS, no vendor SDK, standard library only. What the programs are
is configuration that lives on the robot (the ``programs`` section of the adapter's configuration),
not in this repository::

    "programs": {
      "workdir": "/home/robot/walk",            # the programs' directory (their cwd)
      "env_file": "/opt/robot/env.bash",        # optional: sourced by bash before every program
      "stop_grace_s": 5.0,                      # SIGTERM, then SIGKILL after this long
      "start_region": "一楼",                    # only with regions, see below
      "state_file": "/home/robot/neuxmind/programs-state.json",
      "places": {
        "会议室": {"region": "一楼", "routes": {
          "一楼": [{"title": "走到会议室", "argv": ["python3", "walk.py", "--goto", "meeting",
                                                 "--cruise", "{max_speed}"],
                    "speed_range": [0.25, 0.6], "ok": "route done", "timeout_s": 260,
                    "retry": {"when": "sensors not ready", "times": 2}}],
          "二楼": [{"title": "下楼", "argv": ["python3", "stairs.py", "down"], "speed": 0.3,
                    "enters": "一楼", "exit_code_only": true, "timeout_s": 300},
                   {"title": "走到会议室", "argv": ["..."], "ok": "route done", "timeout_s": 260}]}},
        "二楼": {"region": "二楼", "routes": {"一楼": ["..."], "二楼": []}}
      },
      "explore": {"regions": ["一楼"], "max_duration_s": 600, "max_radius": 10, "default_radius": 5,
                  "steps": [{"argv": ["python3", "walk.py", "--wander", "--seconds", "{duration_s}",
                                      "--leash", "{radius}", "--cruise", "{max_speed}"],
                             "args_if": {"max_distance": ["--max-dist", "{max_distance}"]},
                             "speed_range": [0.25, 0.4], "ok": "exit (time|segment done)",
                             "timeout_margin_s": 30}]}
    }

Steps
-----
A place is reached by running its steps in order; the first one that does not succeed ends the
action as failed and nothing after it is started. A step succeeded when its program exited with
code 0 **and** its output matches ``ok`` (a regular expression, searched in the last
:data:`OUTPUT_KEEP` bytes of stdout + stderr). A program that tells success only by its exit code
says so with ``"exit_code_only": true``; one of the two is required, so that nobody forgets that a
program which always exits 0 proves nothing. ``retry`` starts the same step again (at most
:data:`MAX_RETRIES` times) when its output matches ``when`` or it exited with ``exit_code``: use it
only for a refusal the program makes before it has moved anything. ``exit_code`` is for a program
that reserves an exit code for exactly that ("not ready, nothing sent"); see "Regions" for what it
is also used for.

Values from a request reach a program only through the tokens ``{max_speed}``, ``{duration_s}``,
``{radius}``, ``{max_distance}`` (numbers the capability standard has validated and the safety
supervisor has bounded) and ``{run}`` (a label made here). The place name selects an entry of the
configuration and is never passed on. Programs are started without a shell: every ``argv`` element
is one argument.

Speed
-----
``max_speed`` is the supervisor's bound for this request. A step with ``speed_range: [low, high]``
gets ``min(max_speed, high)`` for ``{max_speed}`` and is refused when the bound is below ``low``; a
step with a fixed ``speed`` is refused when the bound is below it. Every step of a route is checked
before the first one starts, so a route is not abandoned halfway for a reason known in advance. A
step with neither is taken not to move the robot (a sound, a gait switch that the program itself
declares safe): only the person who wrote the configuration can know that.

Regions
-------
Where a route depends on which part of the site the robot is in (downstairs / upstairs), places
name their ``region`` and give one route per region the robot may come from. A step that takes the
robot into another region says ``"enters": "<region>"``. This module remembers the region in
``state_file``: it becomes *unknown* the moment such a step starts and the new region when it
succeeds. One exception: when every attempt of the step ended with the step's ``retry.exit_code``
(the code the program reserves for "refused before any motion"), the robot is still where it was
and the region is kept. Text in the output is never taken as proof of that: a program can print a
refusal after it has moved. While it is unknown every place and the free walk are refused: a robot left on a
staircase needs a person, not a route planned from the wrong floor. After someone has brought the
robot back to ``start_region``, delete ``state_file`` and restart the runtime. The file is only as
true as the last program's report; a robot that was carried elsewhere is not noticed here.

Stopping
--------
``cancel()`` and :meth:`ProgramRunner.stop_all` send SIGTERM to the program's process group and
return at once; a watcher sends SIGKILL after ``stop_grace_s``. They take one short lock that is
only ever held around a ``poll`` or a ``kill`` system call, never across a wait. A program gets
SIGTERM **once**, however many times it is cancelled or the robot is stopped (the runtime stops an
adapter repeatedly while its e-stop is latched): a program whose own stop is a controlled motion
must not have it interrupted by a second signal. When a program ends in any way other than
success, ``on_halt`` is called (the adapter publishes zero velocity there): a program that was
killed cannot be trusted to have stopped the body itself.

If the runtime itself dies (crash, SIGKILL), the kernel sends the running program SIGTERM (Linux
``PR_SET_PDEATHSIG``, set by the small launcher that starts every program), so a program that stops
the body on SIGTERM does not go on walking unsupervised. A program that ignores SIGTERM is not
ended in that case, and a restarted runtime does not know about it: programs must honour SIGTERM.
``stop_grace_s`` is at most :data:`MAX_STOP_GRACE_S`, below the time the device's own stop script
gives the runtime before it kills it.

What a program leaves running in its own process group is killed when the program ends. A helper
it put into another session is not ours to signal; if such a helper keeps the program's output
open, reading stops once the program has ended and the pipe is quiet (at most
:data:`OUTPUT_SETTLE_S` and :data:`OUTPUT_AFTER_END` bytes later) and our end of the pipe is
closed: a helper that goes on writing to it gets SIGPIPE. Give ``argv`` the program itself, not a
wrapper that starts it: when the runtime dies the kernel's SIGTERM goes to the started process
only, and a wrapper would die and leave the program walking.

The environment file is sourced with nothing on its command line, its output is dropped, and the
program starts in ``workdir`` whatever the file did; its return status does not matter. If it ends
the shell (``exit``), the step fails. It can still replace the shell (``exec``) or take the
launcher's exit trap away: it is as trusted as the rest of the configuration. Functions named
``cd`` / ``exec`` / ``trap`` and the options ``-e`` ``-u`` ``-x`` it leaves behind do not reach the
launcher's remaining commands.

Not covered here: a velocity command that is not zero, sent while a program drives, is not
published, but the runtime's velocity watchdog still fires for it once and puts a single zero Twist
on the topic the program is driving on.
"""
from __future__ import annotations

import collections
import decimal
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable

from ..hal import ActionHandle, ActionStatus, CANCELLED, FAILED, RUNNING, SUCCEEDED

OUTPUT_KEEP = 64 * 1024          # bytes of a program's output kept for the ``ok`` / ``retry`` patterns
ERROR_TAIL = 300                 # characters of output quoted in a failure
MAX_STEPS = 20
MAX_RETRIES = 5
MAX_TIMEOUT_S = 3600.0
MAX_STOP_GRACE_S = 8.0           # device_install.sh kills the runtime 10 s after asking it to stop
OUTPUT_SETTLE_S = 1.0            # how long output is still read after a program has ended, at most
OUTPUT_AFTER_END = 1024 * 1024   # and how much of it: a full pipe of the program's own last output (Linux
                                 # pipes hold 64 KiB, 1 MiB at most) must fit, a helper's endless chatter not
MAX_ARGV = 64
MAX_ARG_LEN = 1000
NAME_MAX = 100
TOKENS = ("max_speed", "duration_s", "radius", "max_distance", "run")
REQUEST_TOKENS = ("max_speed", "duration_s", "radius", "max_distance")
_TOKEN_RE = re.compile(r"\{([^{}]*)\}")
# $1 is the environment file ("" for none), the rest is the program. No request data is ever part
# of this script text. The file is sourced inside a function that was called without arguments, so
# it does not see the program's command line and cannot change it (``set --``, ``shift``); what it
# prints is dropped, so it cannot stand in for a program's own success message. Its return status is
# ignored (many end with a test that is false); a file that cannot be read, or that ends the shell
# before the program starts (``exit``), gives 97 instead of passing for the program's own exit code.
# The program starts in the directory the launcher was started in, whatever the file did.
_LAUNCH = ('nx_env_file=$1; shift; readonly nx_dir=$PWD; if [ -n "$nx_env_file" ]; then '
           '{ [ -f "$nx_env_file" ] && [ -r "$nx_env_file" ]; } || exit 97; trap "exit 97" EXIT; '
           'nx_source() { . "$nx_env_file"; }; nx_source >/dev/null 2>&1 || :; set +eux; builtin trap - EXIT DEBUG; '
           'builtin unset -f nx_source; builtin cd "$nx_dir" || builtin exit 97; fi; '
           'builtin exec -- "$@"')
ENV_FILE_FAILED = 97
# Runs between the runtime and bash: asks the kernel to send this process SIGTERM when the runtime
# dies (Linux; kept across the exec of bash and of the program), then becomes bash. If the runtime
# is already gone by the time the request is in place, it does not start anything.
_PARENT_WATCH = (
    "import os, signal, sys\n"
    "parent = int(sys.argv[1])\n"
    "try:\n"
    "    import ctypes\n"
    "    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)\n"
    "except Exception:\n"
    "    pass\n"
    "if os.getppid() != parent:\n"
    "    sys.exit(98)\n"
    "os.execv('/bin/bash', ['/bin/bash'] + sys.argv[2:])\n")

_PLACE_KEYS = {"region", "routes", "steps", "title"}
_STEP_KEYS = {"title", "argv", "args_if", "ok", "exit_code_only", "timeout_s", "timeout_margin_s", "retry",
              "speed", "speed_range", "enters"}
_TOP_KEYS = {"workdir", "env_file", "stop_grace_s", "start_region", "state_file", "places", "explore"}
_EXPLORE_KEYS = {"regions", "max_duration_s", "max_radius", "default_radius", "steps"}


class ProgramRefused(RuntimeError):
    """The action cannot be started (busy, region unknown, too slow a speed bound...)."""


# ---------------------------------------------------------------- configuration
def _name(value: Any, where: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > NAME_MAX
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError(f"{where} 须为不超过 {NAME_MAX} 个字符的一行文字")
    return value


def _number(value: Any, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} 须为数值")
    if not low < value <= high:
        raise ValueError(f"{where} 须大于 {low:g} 且不超过 {high:g}，收到 {value!r}")
    return float(value)


def _pattern(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError(f"{where} 须为不超过 500 个字符的正则表达式")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{where} 不是合法的正则表达式：{exc}") from None
    return value


def _args(value: Any, where: str, allowed: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_ARGV:
        raise ValueError(f"{where} 须为 1–{MAX_ARGV} 个参数的列表")
    for arg in value:
        if not isinstance(arg, str) or not arg or len(arg) > MAX_ARG_LEN or "\x00" in arg:
            raise ValueError(f"{where} 的每个参数须为非空字符串")
        for token in _TOKEN_RE.findall(arg):
            if token not in allowed:
                raise ValueError(f"{where} 里有不能用的占位符 {{{token}}}；这里可用 {['{%s}' % t for t in allowed]}")
    return list(value)


def _step(raw: Any, where: str, regions: set[str], tokens: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 须为对象")
    unknown = sorted(set(raw) - _STEP_KEYS)
    if unknown:
        raise ValueError(f"{where} 里有不认识的项 {unknown}")
    step: dict[str, Any] = {"argv": _args(raw.get("argv"), f"{where}.argv", tokens)}
    step["title"] = _name(raw.get("title", os.path.basename(step["argv"][0])), f"{where}.title")
    step["args_if"] = {}
    extra = raw.get("args_if", {})
    if not isinstance(extra, dict):
        raise ValueError(f"{where}.args_if 须为对象")
    for key, value in extra.items():
        if key not in REQUEST_TOKENS or key not in tokens:
            raise ValueError(f"{where}.args_if 的键须为 {[t for t in REQUEST_TOKENS if t in tokens]} 之一")
        step["args_if"][key] = _args(value, f"{where}.args_if.{key}", tokens)
    exit_only = raw.get("exit_code_only", False)
    if not isinstance(exit_only, bool):
        raise ValueError(f"{where}.exit_code_only 须为 true/false")
    if ("ok" in raw) == exit_only:
        raise ValueError(f"{where} 须写明怎样算成功：给出 ok（输出里要出现的内容），或者写 exit_code_only: true"
                         "（只看退出码），二者取一")
    step["ok"] = None if exit_only else _pattern(raw["ok"], f"{where}.ok")
    if ("timeout_s" in raw) == ("timeout_margin_s" in raw):
        raise ValueError(f"{where} 须给出 timeout_s，或者（时长由请求决定的步骤）timeout_margin_s，二者取一")
    step["timeout_s"] = step["timeout_margin_s"] = None
    if "timeout_s" in raw:
        step["timeout_s"] = _number(raw["timeout_s"], f"{where}.timeout_s", 0.0, MAX_TIMEOUT_S)
    else:
        if "duration_s" not in tokens:
            raise ValueError(f"{where}.timeout_margin_s 只能用在无图自由走的步骤里")
        step["timeout_margin_s"] = _number(raw["timeout_margin_s"], f"{where}.timeout_margin_s", 0.0, 600.0)
    step["retry"] = None
    if "retry" in raw:
        retry = raw["retry"]
        if not isinstance(retry, dict) or set(retry) - {"when", "exit_code", "times", "delay_s"}:
            raise ValueError(f"{where}.retry 须为对象 {{when, exit_code, times, delay_s}}")
        if "when" not in retry and "exit_code" not in retry:
            raise ValueError(f"{where}.retry 须给出 when（输出里的内容）或 exit_code（程序保留的退出码）")
        code = retry.get("exit_code")
        if code is not None and (isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 255
                                 or code == ENV_FILE_FAILED):
            raise ValueError(f"{where}.retry.exit_code 须为 1–255 的整数（{ENV_FILE_FAILED} 除外）")
        times = retry.get("times", 1)
        if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= MAX_RETRIES:
            raise ValueError(f"{where}.retry.times 须为 1–{MAX_RETRIES} 的整数")
        step["retry"] = {"when": _pattern(retry["when"], f"{where}.retry.when") if "when" in retry else None,
                         "exit_code": code, "times": times,
                         "delay_s": _number(retry.get("delay_s", 1.0), f"{where}.retry.delay_s", 0.0, 30.0)}
    if "speed" in raw and "speed_range" in raw:
        raise ValueError(f"{where} 的 speed 与 speed_range 只能写一个")
    step["speed"] = step["speed_range"] = None
    if "speed" in raw:
        step["speed"] = _number(raw["speed"], f"{where}.speed", 0.0, 10.0)
    if "speed_range" in raw:
        pair = raw["speed_range"]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{where}.speed_range 须为 [最低, 最高]")
        low = _number(pair[0], f"{where}.speed_range[0]", 0.0, 10.0)
        high = _number(pair[1], f"{where}.speed_range[1]", 0.0, 10.0)
        if low > high:
            raise ValueError(f"{where}.speed_range 的最低不能高于最高")
        step["speed_range"] = (low, high)
    uses_speed = any("{max_speed}" in arg for arg in step["argv"] + sum(step["args_if"].values(), []))
    if uses_speed and step["speed_range"] is None:
        raise ValueError(f"{where} 用了 {{max_speed}}，须同时给出 speed_range（这个程序能接受的速度范围）")
    step["enters"] = None
    if "enters" in raw:
        step["enters"] = _name(raw["enters"], f"{where}.enters")
        if step["enters"] not in regions:
            raise ValueError(f"{where}.enters={step['enters']!r} 不是任何地点所在的区域 {sorted(regions)}")
    return step


def _steps(raw: Any, where: str, regions: set[str], tokens: tuple[str, ...], allow_empty: bool) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > MAX_STEPS or (not raw and not allow_empty):
        raise ValueError(f"{where} 须为步骤列表（最多 {MAX_STEPS} 步）")
    return [_step(item, f"{where}[{index}]", regions, tokens) for index, item in enumerate(raw)]


def normalize_config(raw: Any) -> dict:
    """Check a ``programs`` section and return it in the shape :class:`ProgramRunner` uses.
    Raises ``ValueError`` (a message for the person who wrote the file) on anything unclear."""
    if not isinstance(raw, dict):
        raise ValueError("programs 须为对象")
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ValueError(f"programs 里有不认识的项 {unknown}")
    workdir = raw.get("workdir")
    if not isinstance(workdir, str) or not os.path.isabs(workdir):
        raise ValueError("programs.workdir 须为绝对路径")
    env_file = raw.get("env_file")
    if env_file is not None and (not isinstance(env_file, str) or not os.path.isabs(env_file)):
        raise ValueError("programs.env_file 须为绝对路径（或不写）")
    out: dict[str, Any] = {"workdir": workdir, "env_file": env_file,
                           "stop_grace_s": _number(raw.get("stop_grace_s", 5.0), "programs.stop_grace_s", 0.0,
                                                   MAX_STOP_GRACE_S)}
    places_raw = raw.get("places", {})
    if not isinstance(places_raw, dict):
        raise ValueError("programs.places 须为对象：{地点名: {...}}")
    explore_raw = raw.get("explore")
    if not places_raw and explore_raw is None:
        raise ValueError("programs 里既没有 places 也没有 explore")
    for name, place in places_raw.items():
        _name(name, "programs.places 的地点名")
        if not isinstance(place, dict):
            raise ValueError(f"programs.places.{name} 须为对象")
        unknown = sorted(set(place) - _PLACE_KEYS)
        if unknown:
            raise ValueError(f"programs.places.{name} 里有不认识的项 {unknown}")
    regions = {_name(p["region"], f"programs.places.{n}.region") for n, p in places_raw.items() if "region" in p}
    out["regional"] = bool(regions)
    out["regions"] = sorted(regions)
    out["start_region"] = out["state_file"] = None
    if regions:
        missing = sorted(n for n, p in places_raw.items() if "region" not in p)
        if missing:
            raise ValueError(f"有的地点写了 region，那么每个地点都要写；缺少：{missing}")
        if raw.get("start_region") not in regions:
            raise ValueError(f"programs.start_region 须为 {sorted(regions)} 之一（机器人开始时所在的区域）")
        state_file = raw.get("state_file")
        if not isinstance(state_file, str) or not os.path.isabs(state_file):
            raise ValueError("用了区域就要给出 programs.state_file（绝对路径）：记住机器人现在在哪个区域")
        out["start_region"], out["state_file"] = raw["start_region"], state_file
    elif raw.get("start_region") is not None or raw.get("state_file") is not None:
        raise ValueError("programs.start_region / state_file 只在地点写了 region 时使用")
    place_tokens = ("max_speed", "run")
    out["places"] = {}
    for name, place in places_raw.items():
        where = f"programs.places.{name}"
        if regions:
            if "steps" in place or not isinstance(place.get("routes"), dict) or not place["routes"]:
                raise ValueError(f"{where} 须给出 routes：{{出发区域: [步骤...]}}")
            routes = {}
            for origin, steps in place["routes"].items():
                if origin not in regions:
                    raise ValueError(f"{where}.routes 的出发区域 {origin!r} 不是任何地点所在的区域 {sorted(regions)}")
                routes[origin] = _steps(steps, f"{where}.routes.{origin}", regions, place_tokens, allow_empty=True)
                end = origin
                for step in routes[origin]:
                    end = step["enters"] or end
                if end != place["region"]:
                    raise ValueError(f"{where}.routes.{origin} 走完后在区域 {end!r}，但这个地点在 {place['region']!r}："
                                     "进入另一个区域的步骤要写 enters")
                if not routes[origin] and origin != place["region"]:
                    raise ValueError(f"{where}.routes.{origin} 不能为空")
            out["places"][name] = {"region": place["region"], "routes": routes}
        else:
            if "routes" in place:
                raise ValueError(f"{where} 没有 region，请用 steps 而不是 routes")
            out["places"][name] = {"region": None,
                                   "routes": {None: _steps(place.get("steps"), f"{where}.steps", regions,
                                                           place_tokens, allow_empty=False)}}
    out["explore"] = None
    if explore_raw is not None:
        if not isinstance(explore_raw, dict):
            raise ValueError("programs.explore 须为对象")
        unknown = sorted(set(explore_raw) - _EXPLORE_KEYS)
        if unknown:
            raise ValueError(f"programs.explore 里有不认识的项 {unknown}")
        explore: dict[str, Any] = {
            "max_duration_s": _number(explore_raw.get("max_duration_s"), "programs.explore.max_duration_s",
                                      0.0, MAX_TIMEOUT_S),
            "max_radius": None, "default_radius": None, "regions": None,
            "steps": _steps(explore_raw.get("steps"), "programs.explore.steps", regions,
                            ("max_speed", "duration_s", "radius", "max_distance", "run"), allow_empty=False)}
        for key in ("max_radius", "default_radius"):
            if explore_raw.get(key) is not None:
                explore[key] = _number(explore_raw[key], f"programs.explore.{key}", 0.0, 1000.0)
        uses_radius = any("{radius}" in arg for step in explore["steps"] for arg in step["argv"])
        if uses_radius and (explore["default_radius"] is None or explore["max_radius"] is None):
            raise ValueError("programs.explore 的步骤用了 {radius}，须给出 default_radius（请求没说半径时用它）"
                             "和 max_radius（请求最多能要多大）")
        if explore["default_radius"] is not None and explore["max_radius"] is not None \
                and explore["default_radius"] > explore["max_radius"]:
            raise ValueError("programs.explore.default_radius 不能大于 max_radius")
        for step in explore["steps"]:
            if any("{max_distance}" in arg for arg in step["argv"]):
                raise ValueError("programs.explore 里 {max_distance} 只能写在 args_if.max_distance 里（请求不一定给出它）")
            if step["enters"]:
                raise ValueError("programs.explore 的步骤不能进入另一个区域")
        if "regions" in explore_raw:
            if not regions:
                raise ValueError("programs.explore.regions 只在地点写了 region 时使用")
            allowed = explore_raw["regions"]
            if not isinstance(allowed, list) or not allowed or any(r not in regions for r in allowed):
                raise ValueError(f"programs.explore.regions 须为 {sorted(regions)} 中的若干个")
            explore["regions"] = list(allowed)
        out["explore"] = explore
    return out


def manifest_capabilities(config: dict) -> list[dict]:
    """The capability entries a normalized configuration adds to the adapter's manifest."""
    entries = []
    if config["places"]:
        entries.append({"id": "base.navigate_to_place", "params": {"places": list(config["places"])}})
    if config["explore"] is not None:
        params = {"max_duration_s": config["explore"]["max_duration_s"]}
        if config["explore"]["max_radius"] is not None:
            params["max_radius"] = config["explore"]["max_radius"]
        entries.append({"id": "base.explore", "params": params})
    return entries


def _format_number(value: float, where: str = "数值") -> str:
    """A request's number as a command-line argument: up to six decimals, never rounded to zero (a
    program may read 0 as "no limit")."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProgramRefused(f"{where}={value!r} 不是有限的数值")
    text = format(float(value), ".6f").rstrip("0").rstrip(".")
    if not text or float(text) <= 0.0:
        raise ProgramRefused(f"{where}={value!r} 太小，写不成程序的参数")
    return text


def _format_speed(value: float) -> str:
    """A speed bound as an argument: three decimals, rounded **down**, so what the program is told
    never exceeds the bound."""
    floored = decimal.Decimal(repr(float(value))).quantize(decimal.Decimal("0.001"), rounding=decimal.ROUND_FLOOR)
    if floored <= 0:
        raise ProgramRefused(f"允许的速度 {value!r} m/s 太小，写不成程序的参数")
    return format(floored, "f").rstrip("0").rstrip(".")


def render(args: list[str], values: dict[str, str]) -> list[str]:
    """Fill the tokens of ``args``; every token must have a value (the configuration check
    guarantees the names, the caller the values)."""
    return [_TOKEN_RE.sub(lambda m: values[m.group(1)], arg) for arg in args]


def plan_steps(steps: list[dict], request: dict, run: str) -> list[dict]:
    """Turn configured steps into concrete commands for one request, or raise
    :class:`ProgramRefused`. Checks the whole route against the speed bound before anything runs."""
    bound = request.get("max_speed")
    if bound is None or isinstance(bound, bool) or not isinstance(bound, (int, float)) \
            or not math.isfinite(bound) or bound <= 0:
        raise ProgramRefused("这次请求没有带速度上限（max_speed），拒绝启动本体上的程序")
    planned = []
    for step in steps:
        values = {"run": run}
        for key in ("duration_s", "radius", "max_distance"):
            if request.get(key) is not None:
                values[key] = _format_number(request[key], key)
        if step["speed"] is not None and bound < step["speed"]:
            raise ProgramRefused(f"步骤「{step['title']}」按固定速度 {step['speed']:g} m/s 行走，"
                                 f"高于这次允许的 {bound:g} m/s")
        if step["speed_range"] is not None:
            low, high = step["speed_range"]
            if bound < low:
                raise ProgramRefused(f"步骤「{step['title']}」最慢也要 {low:g} m/s，高于这次允许的 {bound:g} m/s")
            values["max_speed"] = _format_speed(min(bound, high))
            if float(values["max_speed"]) < low:     # written with three decimals it would fall below the minimum
                raise ProgramRefused(f"步骤「{step['title']}」最慢也要 {low:g} m/s，这次允许的 {bound:g} m/s 写成"
                                     "三位小数后低于它")
        argv = list(step["argv"])
        for key, extra in step["args_if"].items():
            if request.get(key) is not None:
                argv += extra
        try:
            command = render(argv, values)
        except KeyError as exc:
            raise ProgramRefused(f"步骤「{step['title']}」需要 {exc.args[0]}，这次请求没有给出") from None
        timeout = step["timeout_s"]
        if timeout is None:
            timeout = float(request["duration_s"]) + step["timeout_margin_s"]
        planned.append({"title": step["title"], "argv": command, "ok": step["ok"], "retry": step["retry"],
                        "timeout_s": timeout, "enters": step["enters"]})
    return planned


# ---------------------------------------------------------------- one program
class _Process:
    """One started program: its own session (so the whole group can be signalled), output kept in a
    bounded buffer by a reader thread. ``poll`` and ``signal`` share one lock that is held only around
    the system call, so a signal can never go to a pid that was already reaped and reused."""

    def __init__(self, argv: list[str], workdir: str, env_file: str | None):
        self._lock = threading.Lock()
        self._chunks: collections.deque[bytes] = collections.deque()
        self._kept = 0
        self.returncode: int | None = None
        self._term_sent = False
        self._ended = threading.Event()          # the program is gone: the reader may stop waiting
        self._proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PARENT_WATCH, str(os.getpid()),
             "-c", _LAUNCH, "neuxmind-program", env_file or "", *argv],
            cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True, bufsize=0)
        self.pid = self._proc.pid
        self._reader = threading.Thread(target=self._read, name="nx-program-output", daemon=True)
        try:
            self._reader.start()
        except BaseException:                    # no thread to be had: do not leave the program unwatched
            self.signal(signal.SIGKILL)
            self._proc.wait()
            self._proc.stdout.close()
            raise

    def _read(self) -> None:
        """Keep the newest output. Never blocks in a read: it waits with ``select`` and gives up
        :data:`OUTPUT_SETTLE_S` after the program ended, even if someone else (a helper the program
        started in another session) still holds the pipe open."""
        fd = self._proc.stdout.fileno()
        deadline, budget = None, OUTPUT_AFTER_END
        try:
            waiter = select.poll()               # not select(): that fails for descriptors above 1023
            waiter.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            while True:
                ended = self._ended.is_set()
                if deadline is None and ended:
                    deadline = time.monotonic() + OUTPUT_SETTLE_S
                if deadline is not None and (time.monotonic() >= deadline or budget <= 0):
                    return
                if not waiter.poll(50):
                    if ended:                    # the program is gone and the pipe is quiet: that was all of it
                        return
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    return
                if ended:
                    budget -= len(chunk)
                self._chunks.append(chunk)
                self._kept += len(chunk)
                while self._kept > OUTPUT_KEEP and len(self._chunks) > 1:
                    self._kept -= len(self._chunks.popleft())
        except (OSError, ValueError):
            return

    def poll(self) -> int | None:
        """Exit code once the program has ended, else ``None``. Whatever the program left running in
        its session is killed first, while the ended leader is still unreaped: its pid (the group id)
        cannot be given to anyone else before the reap, so the signal cannot reach a stranger."""
        with self._lock:
            if self._proc.returncode is None:
                if self._leader_exited():
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                self._proc.poll()
            self.returncode = self._proc.returncode
            if self.returncode is not None:
                self._ended.set()
            return self.returncode

    def _leader_exited(self) -> bool:
        """True when the leader has ended and is waiting to be reaped. Where the system cannot tell
        without reaping (no ``waitid``), False: leftovers are then not cleaned up, nothing else changes."""
        waitid = getattr(os, "waitid", None)
        if waitid is None:
            return False
        try:
            return waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except (ChildProcessError, OSError):
            return False

    def terminate(self) -> None:
        """Ask the program to stop: SIGTERM to its group, **once** however often this is called."""
        with self._lock:
            if self._term_sent or self._proc.returncode is not None:
                return
            self._term_sent = True
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def signal(self, signum: int) -> bool:
        """Signal the program's process group if it has not been reaped. Never blocks."""
        with self._lock:
            if self._proc.returncode is not None:
                return False
            try:
                os.killpg(self.pid, signum)
            except (ProcessLookupError, PermissionError):
                return False
            return True

    def output(self) -> str:
        self._reader.join(OUTPUT_SETTLE_S + 1.0)     # the reader ends by itself once the program has
        data = b"".join(list(self._chunks))[-OUTPUT_KEEP:]
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        """Release the pipe. If the program is somehow still there (an unexpected error on the way),
        it is killed first: nothing started here is ever left running unwatched."""
        if self.poll() is None:
            self.signal(signal.SIGKILL)
            try:
                self._proc.wait(5.0)
            except subprocess.TimeoutExpired:
                pass
            self.poll()
        self._ended.set()
        self._reader.join(OUTPUT_SETTLE_S + 1.0)
        if self._reader.is_alive():                  # cannot happen with the select loop; never block on it
            return
        try:
            self._proc.stdout.close()
        except (OSError, ValueError):
            pass


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-ERROR_TAIL:]


class ProgramHandle(ActionHandle):
    """A place route or a free walk in progress. All fields are written by the worker thread with
    single assignments and read by ``poll``; ``cancel`` only sets a flag and signals."""

    def __init__(self, runner: "ProgramRunner", label: str, steps: list[dict], result: dict):
        self._runner = runner
        self.label = label
        self._steps = steps
        self._result = result
        self._status = ActionStatus(state=RUNNING, feedback={"program": label})
        self._cancel = threading.Event()
        self._current: _Process | None = None
        self.done = threading.Event()

    def poll(self) -> ActionStatus:
        return self._status

    def cancel(self) -> None:
        self._cancel.set()
        process = self._current
        if process is not None:
            process.terminate()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


class RefusedHandle(ActionHandle):
    """An action that was refused before anything started. A refusal is an answer, not an adapter
    fault: raising from ``start_action`` would make the runtime stop the adapter, and with it a
    program that is running for someone else."""

    def __init__(self, reason: str):
        self._status = ActionStatus(state=FAILED, error=reason)

    def poll(self) -> ActionStatus:
        return self._status

    def cancel(self) -> None:
        pass


class ProgramRunner:
    """Starts, watches and ends on-body programs for one adapter. One action at a time."""

    def __init__(self, config: dict, on_halt: Callable[[], None] | None = None,
                 on_takeover: Callable[[], None] | None = None):
        self.config = config
        self._on_halt = on_halt
        # Called when an action has been accepted, before its first program can exist: the adapter
        # zeroes the velocity topic there, so that zero can never land after the program's own commands.
        self._on_takeover = on_takeover
        self._active: ProgramHandle | None = None
        self._start_lock = threading.Lock()       # start-up bookkeeping only, never held while waiting
        self._region: str | None = None
        self._region_known = True
        if config["regional"]:
            self._load_region()

    # ------------------------------------------------------------ region
    def _load_region(self) -> None:
        path = self.config["state_file"]
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            self._region = self.config["start_region"]
            return
        except (OSError, ValueError):
            saved = None
        region = saved.get("region") if isinstance(saved, dict) else None
        if region in self.config["regions"]:
            self._region = region
        else:                                   # unreadable, or saved as unknown: stay unknown
            self._region, self._region_known = None, False

    def _set_region(self, region: str | None) -> None:
        path = self.config["state_file"]
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"region": region, "at": time.time()}, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:                                     # the rename itself must survive a power cut
            directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
        self._region, self._region_known = region, region is not None

    @property
    def region(self) -> str | None:
        return self._region

    def _require_region(self) -> str:
        if not self._region_known:
            raise ProgramRefused(
                "不知道机器人现在在哪个区域（上一次跨区域的行走没有走完）。请到现场确认，把机器人带回"
                f"「{self.config['start_region']}」，删除 {self.config['state_file']} 后重启管理层")
        return self._region

    # ------------------------------------------------------------ starting
    @property
    def busy(self) -> bool:
        active = self._active
        return active is not None and not active.done.is_set()

    def status(self) -> dict:
        active = self._active
        out: dict[str, Any] = {"running": active.label if active is not None and not active.done.is_set() else None}
        if self.config["regional"]:
            out["region"] = self._region if self._region_known else None
            out["region_known"] = self._region_known
        return out

    def start_place(self, place: str, request: dict) -> ProgramHandle:
        entry = self.config["places"].get(place)
        if entry is None:
            raise ProgramRefused(f"配置里没有地点 {place}")
        with self._start_lock:
            self._refuse_if_busy()
            origin = self._require_region() if self.config["regional"] else None
            steps = entry["routes"].get(origin)
            if steps is None:
                raise ProgramRefused(f"没有从「{origin}」去「{place}」的路线")
            planned = plan_steps(steps, request, self._label())
            return self._launch(f"place:{place}", planned, {"reached": True, "place": place})

    def start_explore(self, request: dict) -> ProgramHandle:
        explore = self.config["explore"]
        if explore is None:
            raise ProgramRefused("配置里没有无图自由走的程序")
        with self._start_lock:
            self._refuse_if_busy()
            if self.config["regional"]:
                region = self._require_region()
                if explore["regions"] is not None and region not in explore["regions"]:
                    raise ProgramRefused(f"机器人在「{region}」，无图自由走只允许在 {explore['regions']}")
            request = dict(request)
            if request.get("radius") is None and explore["default_radius"] is not None:
                request["radius"] = explore["default_radius"]
            planned = plan_steps(explore["steps"], request, self._label())
            return self._launch("explore", planned, {"completed": True})

    def start(self, cap_id: str, args: dict) -> ActionHandle:
        """``base.navigate_to_place`` / ``base.explore`` for an adapter's ``start_action``: the running
        action's handle, or a handle that has already failed with the reason it was refused."""
        try:
            if cap_id == "base.navigate_to_place":
                return self.start_place(args["place"], args)
            if cap_id == "base.explore":
                return self.start_explore(args)
        except ProgramRefused as exc:
            return RefusedHandle(str(exc))
        except (ArithmeticError, TypeError) as exc:      # a number no program argument can be written from
            return RefusedHandle(f"请求里的数值写不成程序的参数：{type(exc).__name__}")
        raise ValueError(f"没有对应 {cap_id} 的本体程序")

    def _refuse_if_busy(self) -> None:
        if self.busy:
            raise ProgramRefused(f"本体上的程序还在运行（{self._active.label}），请先等它结束或停止")

    @staticmethod
    def _label() -> str:
        return "nx-" + uuid.uuid4().hex[:8]

    def _launch(self, label: str, planned: list[dict], result: dict) -> ProgramHandle:
        if not os.path.isdir(self.config["workdir"]):
            raise ProgramRefused(f"程序目录不存在：{self.config['workdir']}")
        handle = ProgramHandle(self, label, planned, result)
        previous = self._active
        self._active = handle
        if self._on_takeover is not None:
            try:
                self._on_takeover()
            except Exception as exc:
                self._active = previous
                raise ProgramRefused(f"接手前清零速度话题失败：{exc}") from None
        try:
            threading.Thread(target=self._work, args=(handle,), name="nx-program", daemon=True).start()
        except BaseException as exc:             # nothing was started: do not stay "busy" for ever
            self._active = previous
            raise ProgramRefused(f"启动不了看护线程：{exc}") from None
        return handle

    # ------------------------------------------------------------ running
    def _work(self, handle: ProgramHandle) -> None:
        state, error = FAILED, "未知原因"
        try:
            state, error = self._run_steps(handle)
        except Exception as exc:                 # a bug here must not leave the action running forever
            state, error = FAILED, f"运行程序时出错：{type(exc).__name__}: {exc}"
        finally:
            if state != SUCCEEDED:
                self._halt()
            if state == SUCCEEDED:
                handle._status = ActionStatus(state=SUCCEEDED, progress=1.0, result=dict(handle._result),
                                              feedback={"program": handle.label})
            else:
                handle._status = ActionStatus(state=state, progress=handle._status.progress, error=error,
                                              feedback=dict(handle._status.feedback))
            handle.done.set()

    def _halt(self) -> None:
        if self._on_halt is None:
            return
        try:
            self._on_halt()
        except Exception:
            pass

    def _run_steps(self, handle: ProgramHandle) -> tuple[str, str | None]:
        total = len(handle._steps)
        for index, step in enumerate(handle._steps):
            if handle.cancelled:
                return CANCELLED, "已取消"
            handle._status = ActionStatus(state=RUNNING, progress=index / total,
                                          feedback={"program": handle.label, "step": step["title"],
                                                    "index": index + 1, "of": total})
            before = self._region
            if step["enters"] is not None:
                self._set_region(None)           # from here on the robot is between regions
            attempts = 1 + (step["retry"]["times"] if step["retry"] else 0)
            retry = step["retry"]
            # Still where it was: only if every attempt ended with the exit code the program reserves for
            # "refused before any motion". Output text is no proof (review: a program can print it after moving).
            never_moved = retry is not None and retry["exit_code"] is not None
            for attempt in range(attempts):
                state, error, output, code = self._run_one(handle, step)
                if state == SUCCEEDED or state == CANCELLED:
                    never_moved = False
                    break
                by_code = retry is not None and retry["exit_code"] is not None and code == retry["exit_code"]
                by_text = bool(retry is not None and retry["when"] is not None and code is not None
                               and re.search(retry["when"], output))
                never_moved = never_moved and by_code
                if not (by_code or by_text) or attempt + 1 >= attempts:
                    break
                if handle._cancel.wait(step["retry"]["delay_s"]):
                    state, error = CANCELLED, "已取消"
                    break
            if state != SUCCEEDED:
                if step["enters"] is not None and never_moved and state == FAILED:
                    # the robot is still where it was (see above)
                    self._set_region(before)
                return state, f"步骤「{step['title']}」{error}"
            if step["enters"] is not None:
                self._set_region(step["enters"])
        return SUCCEEDED, None

    def _run_one(self, handle: ProgramHandle, step: dict) -> tuple[str, str | None, str, int | None]:
        """``(state, error, output, exit code)``; the code is ``None`` unless the program ended by itself."""
        try:
            process = _Process(step["argv"], self.config["workdir"], self.config["env_file"])
        except OSError as exc:
            return FAILED, f"启动不了：{exc}", "", None
        handle._current = process
        try:
            if handle.cancelled:                 # cancelled between the check and the start
                process.terminate()
            deadline = time.monotonic() + step["timeout_s"]
            kill_at = None
            timed_out = False
            while process.poll() is None:
                now = time.monotonic()
                if not timed_out and not handle.cancelled and now >= deadline:
                    timed_out = True
                    process.terminate()
                if (timed_out or handle.cancelled) and kill_at is None:
                    kill_at = now + self.config["stop_grace_s"]
                if kill_at is not None and now >= kill_at:
                    process.signal(signal.SIGKILL)
                    kill_at = now + 1.0          # again, in case the group changed
                time.sleep(0.02)
            output = process.output()
            code = process.returncode
            if handle.cancelled:
                return CANCELLED, "已取消", output, None
            if timed_out:
                return FAILED, f"超过 {step['timeout_s']:g} 秒没有结束，已终止", output, None
            if code == ENV_FILE_FAILED and not output.strip() and self.config["env_file"]:
                return FAILED, f"环境文件 {self.config['env_file']} 读不了，或者它自己结束了 shell", output, None
            if code != 0:
                reason = f"被信号 {-code} 结束" if code < 0 else f"退出码 {code}"
                return FAILED, f"没有完成（{reason}）：{_tail(output)}", output, code
            if step["ok"] is not None and not re.search(step["ok"], output):
                return FAILED, f"结束了，但输出里没有表示成功的内容：{_tail(output)}", output, code
            return SUCCEEDED, None, output, code
        finally:
            handle._current = None
            process.close()

    # ------------------------------------------------------------ stopping
    def stop_all(self) -> None:
        """End whatever is running. Returns at once; the worker escalates to SIGKILL."""
        active = self._active
        if active is not None:
            active.cancel()

    def close(self, wait_s: float = 2.0) -> None:
        active = self._active
        if active is not None:
            active.cancel()
            active.done.wait(wait_s + self.config["stop_grace_s"])
