"""Patrol: visit waypoints in order using the standard base.navigate_to capability.

Portable: it only depends on capability IDs, never on a vendor SDK.
"""


def _to_pose(waypoint):
    """Accept [x, y] or a full 7-element pose [x, y, z, qx, qy, qz, qw]."""
    if isinstance(waypoint, list) and len(waypoint) == 2:
        return [float(waypoint[0]), float(waypoint[1]), 0.0, 0.0, 0.0, 0.0, 1.0]
    if isinstance(waypoint, list) and len(waypoint) == 7:
        return [float(v) for v in waypoint]
    raise ValueError(f"航点格式无效：{waypoint!r}")


def run(ctx, waypoints, loops=1, tolerance=0.15):
    poses = [_to_pose(w) for w in waypoints]
    if not poses:
        raise ValueError("至少需要一个航点")
    if ctx.has("legged.posture"):
        ctx.call("legged.posture", posture="stand")
    total = len(poses) * loops
    reached = 0
    for loop in range(loops):
        for index, pose in enumerate(poses):
            ctx.check_stop()
            ctx.log("前往航点", loop=loop, index=index, goal=pose[:2])
            result = ctx.call("base.navigate_to", goal=pose, tolerance=tolerance)
            if not result.get("reached"):
                ctx.call("safety.stop", reason="patrol: 航点未到达")
                raise RuntimeError(f"航点 {index} 未到达")
            reached += 1
            ctx.progress(reached / total, f"到达航点 {index}")
    final = ctx.read("base.odometry")["pose"] if ctx.has("base.odometry") else None
    return {"reached": reached, "loops": loops, "final_pose": final}
