"""Status report: reads whatever standard state the body offers. No motion."""

OPTIONAL = ("sensor.battery", "base.odometry", "joint.state", "sensor.imu")


def run(ctx):
    report = {"health": ctx.read("system.health"), "simulated": ctx.simulated,
              "family": ctx.manifest["family"], "available": []}
    for cap in OPTIONAL:
        if ctx.has(cap):
            report[cap] = ctx.read(cap)
            report["available"].append(cap)
    ctx.log("状态报告完成", available=report["available"])
    return report
