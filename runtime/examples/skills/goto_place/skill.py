"""Go to a place the body knows by name, with the standard base.navigate_to_place capability.

Portable: the skill names a place, the body decides how to get there (its own navigation,
stairs included). The names a body knows are in its manifest (``places``).
"""


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


def known_places(ctx):
    return list(ctx.param("base.navigate_to_place", "places", []) or [])


def go(ctx, place, max_speed=None):
    """Walk to ``place``; returns the capability's result or raises."""
    args = {"place": place}
    if max_speed is not None:
        args["max_speed"] = float(max_speed)
    result = ctx.call("base.navigate_to_place", **args)
    if not result.get("reached"):
        ctx.call("safety.stop", reason=f"goto_place: 没有到达 {place}")
        raise RuntimeError(f"没有到达 {place}")
    return result


def run(ctx, place, max_speed=None):
    places = known_places(ctx)
    if place not in places:
        raise ValueError(f"这台机器人不认识地点「{place}」；它认识：{'、'.join(places) or '（没有声明）'}")
    battery = battery_percent(ctx)
    ctx.log("出发", place=place, battery=battery)
    result = go(ctx, place, max_speed)
    ctx.progress(1.0, f"到达 {place}")
    return {"reached": True, "place": place, "final_pose": result.get("final_pose")}
