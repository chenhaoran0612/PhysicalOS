"""Inspection round: visit named places in order, then return, with base.navigate_to_place.

Portable: only place names and a standard capability. Every place is checked against the body's
manifest before the robot takes its first step, so a typo does not strand it halfway.
"""
import re


def battery_percent(ctx):
    """Battery percent for the record, or None. Never a reason to fail a walk: the reading is
    optional, and a topic that is briefly stale says nothing about whether the robot arrived."""
    if not ctx.has("sensor.battery"):
        return None
    try:
        return ctx.read("sensor.battery")["percent"]
    except Exception as exc:
        ctx.log("电量读不到，继续", reason=str(exc))
        return None


def split_places(text):
    return [name.strip() for name in re.split(r"[、,，;；\n]+", text or "") if name.strip()]


def run(ctx, places="会议室、二楼", return_to="出发点", loops=1, max_speed=None):
    stops = split_places(places)
    if not stops:
        raise ValueError("至少要有一个巡检地点")
    home = (return_to or "").strip()
    known = list(ctx.param("base.navigate_to_place", "places", []) or [])
    unknown = [name for name in stops + ([home] if home else []) if name not in known]
    if unknown:
        raise ValueError(f"这台机器人不认识地点：{'、'.join(unknown)}；它认识：{'、'.join(known) or '（没有声明）'}")
    route = stops * int(loops) + ([home] if home else [])
    visited = []
    for index, place in enumerate(route):
        ctx.check_stop()
        args = {"place": place}
        if max_speed is not None:
            args["max_speed"] = float(max_speed)
        ctx.log("前往", place=place, index=index + 1, of=len(route))
        result = ctx.call("base.navigate_to_place", **args)
        if not result.get("reached"):
            ctx.call("safety.stop", reason=f"patrol_places: 没有到达 {place}")
            raise RuntimeError(f"没有到达 {place}（已到过：{'、'.join(visited) or '无'}）")
        battery = battery_percent(ctx)
        visited.append(place)
        ctx.log("到达", place=place, battery=battery)
        ctx.progress((index + 1) / len(route), f"到达 {place}")
    return {"visited": visited, "loops": int(loops), "returned_to": home or None}
