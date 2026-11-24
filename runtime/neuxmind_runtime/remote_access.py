"""The owner's switch for remote calls, one per installed skill (issue #43).

A third party can call a skill on this robot through the cloud only while the robot's owner has
turned "allow remote calls" on for that skill, here, on the device. The switch is what stands in for
``hardware_authorized`` when such a call runs (``remote_calls.py``), so:

* Turning it **on** needs ``hardware_authorized: true`` (a person confirmed it in the console) and a
  name (``set_by``). Turning it **off** needs neither and works at once: it is set in memory first,
  a remote call of that skill that is running is cancelled, and only then is the disk written.
* It is bound to the skill's ``digest``. The switch of a skill means its newest installed version; a
  newer version (other content) makes the switch void until a person turns it on again.
* It lives in the state directory (the runtime's store). A record that cannot be read, or an entry
  that is not exactly what this module writes, counts as "off".

Nothing here is on the stop path: stop takes none of these locks and waits for none of this IO.

``GET /v2/remote`` and ``POST /v2/remote/skills`` are :meth:`RemoteAccess.view` and
:meth:`RemoteAccess.set`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import effects
from .device_limits import clean_set_by
from .errors import BadRequest, Conflict, NotAuthorized, NotFound, RuntimeApiError
from .safety import is_motion
from .skills import _version_key

SCHEMA = "neuxmind.remote_access.v1"
STORE_KIND = "remote"
SWITCHES_ID = "switches"
MAX_SKILL_ID = 200
MAX_SWITCHES = 500              # entries kept; more installed skills than this cannot all be switched on
MAX_REPORTED_SKILLS = 200       # skills in one sync with the cloud
MAX_SKILL_ENTRY_BYTES = 7_500    # one skill in a sync, written out (the cloud takes 8 KiB per entry)
RECENT_CALLS = 50               # remote calls shown by GET /v2/remote
NOT_SAVED = "远程调用开关没有写进设备的存储（磁盘满或不可写）"
OFF_NOT_SAVED = ("已经关闭（现在起远程调用被拒绝），但这次关闭还没能写进设备的存储；设备会继续重试，"
                 "重启后在写进去之前这个开关仍按关闭处理")
ARMED_NAME = "remote-switches.armed"         # exists while the stored switches may be trusted
TOMBSTONE_NAME = "remote-switches.off.json"  # skills turned off whose "off" is not in the store yet
DISARMED = "上一次关闭远程调用没有写进存储，重启后无法确认哪些开关还应当开着：所有开关按关闭处理，请重新打开需要的"
SKILL_CHANGED = "这个技能在机器人上的内容和确认时看到的不一样（可能刚装了新版本）；请刷新，看过现在的内容后再打开"


def write_tombstone(path: Path, skill_ids) -> None:
    """Record, outside the store, which skills are off although the store may still say on."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, json.dumps({"off": sorted(skill_ids)}, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_tombstone(path: Path) -> list[str]:
    """The skills a tombstone turns off (none without one). ``ValueError`` when it cannot be read:
    which switches it meant is then unknown."""
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1_000_000).decode("utf-8"))
        skills = data["off"]
        if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
            raise ValueError("格式不对")
        return skills
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ValueError(DISARMED) from None


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value and abs(value) < 1e12


def checked_entry(raw: Any) -> dict | None:
    """One stored switch, or ``None`` when it is not exactly what :meth:`RemoteAccess.set` writes."""
    if not isinstance(raw, dict) or not isinstance(raw.get("allowed"), bool):
        return None
    digest, set_by, set_at = raw.get("digest"), raw.get("set_by"), raw.get("set_at")
    if not isinstance(digest, str) or not 1 <= len(digest) <= 128 or not _number(set_at):
        return None
    if raw["allowed"]:
        try:
            set_by = clean_set_by(set_by)
        except RuntimeApiError:
            return None                                    # on, but nobody stands behind it: off
    elif not isinstance(set_by, str):
        set_by = None
    return {"allowed": raw["allowed"], "digest": digest, "set_by": set_by, "set_at": float(set_at)}


def package_motion(package: dict, capability_ids) -> bool:
    """Whether running this package can move the body (same rule as the runner's motion lease)."""
    wanted = [item["id"] for item in (*package.get("requires", ()), *package.get("optional", ()))]
    return any(is_motion(cap) for cap in wanted if cap in capability_ids and cap != "safety.stop")


def _entry_bytes(entry: dict) -> int:
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))


def fit_entry(entry: dict) -> tuple[dict, bool]:
    """``(entry, trimmed)``: one skill of a sync within :data:`MAX_SKILL_ENTRY_BYTES`. Text meant for
    people is shortened step by step (the description, then the parameters' descriptions, then
    their titles and units); what a call is checked against (names, types, ranges, options) stays."""
    if _entry_bytes(entry) <= MAX_SKILL_ENTRY_BYTES:
        return entry, False
    steps = (lambda e: {**e, "description": e["description"][:200]},
             lambda e: {**e, "parameters": [{**p, "description": str(p.get("description", ""))[:60]}
                                            if isinstance(p, dict) and "description" in p else p
                                            for p in e["parameters"]]},
             lambda e: {**e, "description": "", "parameters": [
                 {k: v for k, v in p.items() if k not in ("description", "title", "unit")} if isinstance(p, dict) else p
                 for p in e["parameters"]]})
    for step in steps:
        entry = step(entry)
        if _entry_bytes(entry) <= MAX_SKILL_ENTRY_BYTES:
            break
    return entry, True          # still too large after the last step: the cloud refuses it and says so


class RemoteAccess:
    """The switches of one runtime. ``calls`` is set by the runtime to its ``RemoteCalls`` book and
    ``link`` (by ``cloud_link.for_serve``) to the thread that talks to the cloud; both may be absent."""

    def __init__(self, runtime, wall=time.time):
        self._rt = runtime
        self._wall = wall
        self._lock = threading.Lock()          # one writer at a time; never taken by stop or by a read
        self._switches: dict[str, dict] = {}   # replaced as a whole, so readers need no lock
        self._closing_off: set[str] = set()    # skills whose switch is being turned off at this moment
        self.trimmed: tuple = ()               # skills whose text was shortened for the last sync
        self.invalid: str | None = None        # why the stored switches were not used, if they were not
        self.unsaved = False                   # an "off" is in force in memory and not in the store yet
        self._armed = Path(runtime.state_dir) / ARMED_NAME
        self._tombstone = Path(runtime.state_dir) / TOMBSTONE_NAME
        self.calls = None
        self.link = None
        self._load()
        runtime.housekeeping.append(self.flush)     # an unsaved "off" is written as soon as the disk takes it

    def _load(self) -> None:
        try:
            stored = self._rt.store.get(STORE_KIND, SWITCHES_ID)
            raw = stored.get("skills") if isinstance(stored, dict) else {}
            if stored is not None and (stored.get("schema") != SCHEMA or not isinstance(raw, dict)):
                raise ValueError("记录格式不对")
            kept = {}
            for skill_id, entry in list(raw.items())[:MAX_SWITCHES]:
                checked = checked_entry(entry)
                if isinstance(skill_id, str) and 0 < len(skill_id) <= MAX_SKILL_ID and checked is not None:
                    kept[skill_id] = checked
            # An "off" that never reached the store must not come back as "on": the tombstone names
            # the skills it was about; without the marker nothing stored as "on" can be trusted.
            for skill_id in read_tombstone(self._tombstone):
                self.unsaved = True
                if skill_id in kept:
                    kept[skill_id] = {**kept[skill_id], "allowed": False, "set_by": None}
            if any(entry["allowed"] for entry in kept.values()) and not self._armed.exists():
                raise ValueError(DISARMED)
            self._switches = kept
        except Exception as exc:                # unreadable: every switch is off (fail closed)
            self.invalid = (str(exc) or type(exc).__name__)[:200]
            self._switches = {}
            self._rt.events.emit("remote.switches_invalid", error=self.invalid)

    # ------------------------------------------------------------ reads (memory and the store; no lock)
    def installed(self) -> dict[str, dict]:
        """``{skill_id: record}``: the newest installed version of every skill."""
        newest: dict[str, dict] = {}
        for record in self._rt.store.list("skills"):
            current = newest.get(record["skill_id"])
            if current is None or _version_key(record["version"]) > _version_key(current["version"]):
                newest[record["skill_id"]] = record
        return newest

    def entry(self, skill_id: str, digest: str) -> dict:
        """The switch of ``skill_id`` as it counts for the installed ``digest``."""
        stored = self._switches.get(skill_id)
        if stored is None:
            return {"allowed": False, "digest": digest, "set_by": None, "set_at": None, "invalidated": False}
        bound = stored["digest"] == digest
        if stored["allowed"] and not bound:     # reinstalled with other content: off until set again
            return {"allowed": False, "digest": digest, "set_by": None, "set_at": None, "invalidated": True}
        return {"allowed": stored["allowed"], "digest": digest, "set_by": stored["set_by"],
                "set_at": stored["set_at"], "invalidated": False}

    def allowed_by(self, skill_id: str, digest: str) -> str | None:
        """Who turned remote calls on for exactly this content, or ``None`` when they are off."""
        stored = self._switches.get(skill_id)
        if stored is None or stored["allowed"] is not True or stored["digest"] != digest:
            return None
        if skill_id in self._closing_off:      # being turned off right now
            return None
        return stored["set_by"]

    def skills_report(self) -> list[dict]:
        """The installed skills with their switches, as one sync tells the cloud."""
        out, trimmed = [], []
        capability_ids = self._rt.bus.capability_ids
        for skill_id, record in sorted(self.installed().items())[:MAX_REPORTED_SKILLS]:
            package, switch = record["package"], self.entry(skill_id, record["digest"])
            entry, shortened = fit_entry({"id": skill_id, "version": record["version"], "digest": record["digest"],
                        "title": package["title"], "description": package["description"],
                        "parameters": package["parameters"],
                        "requires": [item["id"] for item in package["requires"]],
                        "motion": package_motion(package, capability_ids), "timeout_s": package["timeout_s"],
                        "remote": {"allowed": switch["allowed"] is True, "set_by": switch["set_by"],
                                   "set_at": switch["set_at"]}})
            out.append(entry)
            if shortened:
                trimmed.append(skill_id)
        self.trimmed = tuple(trimmed)
        return out

    def view(self) -> dict:
        """``GET /v2/remote``."""
        capability_ids = self._rt.bus.capability_ids
        skills = {}
        for skill_id, record in sorted(self.installed().items()):
            package = record["package"]
            skills[skill_id] = {**self.entry(skill_id, record["digest"]), "version": record["version"],
                                "title": package["title"], "motion": package_motion(package, capability_ids)}
        calls = self.calls.recent(RECENT_CALLS) if self.calls is not None else []
        return {"schema": SCHEMA, "link": self.link_view(), "skills": skills, "calls": calls,
                "invalid": self.invalid, "unsaved": self.unsaved}

    def link_view(self) -> dict:
        """The ``link`` of :meth:`view`: cached values of the link thread; without one it is off."""
        link = self.link
        if link is not None:
            try:
                return link.describe()
            except Exception as exc:
                return {**OFF_VIEW, "last_error": f"连接状态读不到：{str(exc)[:200]}"}
        return dict(OFF_VIEW)

    # ------------------------------------------------------------ the one way to change a switch
    def set(self, payload: dict) -> dict:
        """``POST /v2/remote/skills`` ``{skill_id, allowed, digest, set_by, hardware_authorized,
        request_id?}``; ``digest`` (what the person saw) is needed to turn it on only."""
        skill_id, allowed = payload.get("skill_id"), payload.get("allowed")
        if not isinstance(skill_id, str) or not 0 < len(skill_id) <= MAX_SKILL_ID:
            raise BadRequest("需要技能 ID（skill_id）")
        if not isinstance(allowed, bool):
            raise BadRequest("allowed 须为 true 或 false")
        record = self.installed().get(skill_id)
        if record is None:
            raise NotFound(f"未安装技能 {skill_id[:80]}", code="skill_not_found")
        if not allowed:
            # Off is never held up: no authorisation, no request id (a replayed id must not turn a
            # later "on" back off or be answered from a record without acting).
            self._turn_off(skill_id, record, payload.get("set_by"))
            return {"accepted": True, **self.view()}
        if payload.get("hardware_authorized") is not True:
            raise NotAuthorized("打开远程调用后第三方可以让这台机器人执行这个技能，需要 hardware_authorized=true（人在控制台里确认）")
        set_by = self._clean_name(payload.get("set_by"))
        # Bound to the content the person was shown, not to whatever is newest at this moment.
        if not isinstance(payload.get("digest"), str) or payload["digest"] != record["digest"]:
            raise Conflict(SKILL_CHANGED, code="skill_changed")
        if "request_id" in payload:
            self._rt.ledger.execute("remote.skills", payload, lambda: self._turn_on(skill_id, record, set_by))
        else:
            self._turn_on(skill_id, record, set_by)
        return {"accepted": True, **self.view()}    # always the state as it is now, also for a replay

    @staticmethod
    def _clean_name(value: Any) -> str:
        try:
            return clean_set_by(value)
        except RuntimeApiError as exc:
            if exc.code == "set_by_required":
                raise BadRequest("需要填写是谁打开的（set_by）：设备会记下来", code="set_by_required") from None
            raise BadRequest(str(exc)) from None

    def _turn_on(self, skill_id: str, record: dict, set_by: str) -> dict:
        entry = {"allowed": True, "digest": record["digest"], "set_by": set_by, "set_at": self._wall()}
        with self._lock:
            if self.unsaved:                    # an earlier "off" goes to the disk first, or nothing does
                self._save(self._switches)
            switches = {**self._switches, skill_id: entry}
            if len(switches) > MAX_SWITCHES:
                raise Conflict("远程调用开关太多，请先关掉不用的")
            self._save(switches)                # disk first: what is on is what a restart will find
            effects.mark()
            # Merged into the map as it is now: an "off" that came in while the disk was written stays.
            self._switches = {**self._switches, skill_id: entry}
            self.invalid = None                 # a person has set a switch since: the store is in order again
        self._note(skill_id, record, entry)
        return {"accepted": True}

    def _turn_off(self, skill_id: str, record: dict, set_by: Any) -> None:
        try:
            name = clean_set_by(set_by)
        except RuntimeApiError:
            name = None
        entry = {"allowed": False, "digest": record["digest"], "set_by": name, "set_at": self._wall()}
        self._closing_off.add(skill_id)         # off from this line on, whatever a writer does meanwhile
        self._switches = {**self._switches, skill_id: entry}
        failure: Exception | None = None
        try:
            with self._lock:
                self._switches = {**self._switches, skill_id: entry}    # a writer may have replaced the map
                try:
                    self._save(self._switches)
                except RuntimeApiError:
                    # Fail closed: off stays in force here, is written again on every later round, and
                    # a restart before that finds it off as well.
                    self.unsaved = True
                    self._fail_closed()
                    failure = RuntimeApiError(OFF_NOT_SAVED, code="remote_not_saved", status=500)
        finally:
            self._closing_off.discard(skill_id)     # the map holds the "off" by now
        try:                                    # side effects last: none of them can keep the off from the disk
            calls = self.calls
            if calls is not None:
                calls.switched_off(skill_id)    # a remote call of this skill that is running is cancelled
        except Exception as exc:
            self._rt.events.emit("remote.switch_cancel_failed", skill_id=skill_id, error=str(exc)[:200])
            failure = failure or exc
        self._note(skill_id, record, entry)
        if failure is not None:
            raise failure

    def _fail_closed(self) -> None:
        """An "off" could not be written to the store. Leave a tombstone naming what is off; if that
        cannot be written either, remove the marker (removing needs no space), so that after a
        restart no stored switch counts. Never raises."""
        off = [skill_id for skill_id, entry in self._switches.items() if not entry["allowed"]]
        try:
            write_tombstone(self._tombstone, off)
            how = "tombstone"
        except Exception:
            try:
                self._armed.unlink(missing_ok=True)
                how = "disarmed"
            except Exception:
                how = "nothing"                 # nothing on this disk can be changed: off holds until a restart
        self._rt.events.emit("remote.switches_not_saved", fallback=how, off=off[:50])

    def flush(self) -> None:
        """Write an "off" that is not in the store yet (housekeeping and every round of the link call
        this). Never raises, never waits for another writer."""
        if not self.unsaved or not self._lock.acquire(blocking=False):
            return
        try:
            self._save(self._switches)
        except Exception:
            pass                                # still not saved: the tombstone or the missing marker stands
        finally:
            self._lock.release()

    def _save(self, switches: dict) -> None:
        """Store ``switches`` and mark them as trustworthy. The caller holds ``_lock``."""
        try:
            self._rt.store.put(STORE_KIND, {"id": SWITCHES_ID, "schema": SCHEMA, "skills": switches})
            self._tombstone.unlink(missing_ok=True)      # everything it said is in the store now
            if not self._armed.exists():
                os.close(os.open(self._armed, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        except Exception:
            raise RuntimeApiError(NOT_SAVED, code="remote_not_saved", status=500) from None
        self.unsaved = False

    def _note(self, skill_id: str, record: dict, entry: dict) -> None:
        """An event and an operation record: who allowed or disallowed what, and when."""
        self._rt.events.emit("remote.switch_set", skill_id=skill_id, allowed=entry["allowed"],
                             set_by=entry["set_by"], digest=record["digest"])
        self._rt.records.operation({"operation": "remote.switch", "target": skill_id, "operator": entry["set_by"],
                                    "allowed": entry["allowed"], "version": record["version"],
                                    "digest": record["digest"], "hardware_authorized": entry["allowed"],
                                    "outcome": "ok", "ts": entry["set_at"]})


OFF_VIEW = {"configured": False, "enabled": False, "state": "off", "host": None, "last_sync_at": None,
            "last_error": None, "device_id": None,
            "reason": "和云平台的连接随 serve --cloud-config 启动；这个运行时没有连接"}
