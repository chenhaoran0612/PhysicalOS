"""Walk around without a map for a while, with the standard base.explore capability.

Portable: the body's own program does the walking and the obstacle avoidance; the skill only says
for how long and how far, and checks the body's declared ceilings first so the refusal is readable.
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


def run(ctx, duration_s=60.0, radius=None, max_distance=None, max_speed=None):
    longest = ctx.param("base.explore", "max_duration_s")
    if longest is not None and duration_s > longest:
        raise ValueError(f"这台机器人一次最多自由走 {longest:g} 秒，请求的是 {duration_s:g} 秒")
    widest = ctx.param("base.explore", "max_radius")
    if radius is not None and widest is not None and radius > widest:
        raise ValueError(f"这台机器人的活动半径最大 {widest:g} 米，请求的是 {radius:g} 米")
    args = {"duration_s": float(duration_s)}
    for name, value in (("radius", radius), ("max_distance", max_distance), ("max_speed", max_speed)):
        if value is not None:
            args[name] = float(value)
    before = battery_percent(ctx)
    ctx.log("开始自由走", **args)
    result = ctx.call("base.explore", **args)
    if not result.get("completed"):
        ctx.call("safety.stop", reason="free_walk: 没有正常结束")
        raise RuntimeError("自由走没有正常结束")
    after = battery_percent(ctx)
    ctx.progress(1.0, "自由走结束")
    return {"completed": True, "travelled": result.get("travelled"), "final_pose": result.get("final_pose"),
            "battery_before": before, "battery_after": after}
