"""Wave: a joint-space demo for 6+ DoF arms with a gripper.

Class-style skill. Works with the SDK base class when installed, and falls
back to the runtime's own base class (or plain object) otherwise.
"""
try:
    from neuxmind.skill import Skill
except ImportError:
    try:
        from neuxmind_runtime.skills import Skill
    except ImportError:
        Skill = object


class Wave(Skill):
    def run(self, ctx, cycles=2, amplitude=0.6):
        dof = ctx.param("arm.joint_position", "dof")
        lower = ctx.param("arm.joint_position", "lower")
        upper = ctx.param("arm.joint_position", "upper")

        def pose(*first):
            q = list(first) + [0.0] * (dof - len(first))
            # Stay inside the declared joint limits on any body.
            return [max(lo, min(hi, v)) for v, lo, hi in zip(q, lower, upper)]

        raised = pose(0.0, -0.5, 1.0, 0.0, 0.8)
        ctx.call("arm.joint_position", position=raised)
        for i in range(cycles):
            ctx.check_stop()
            ctx.call("arm.joint_position", position=pose(amplitude, -0.5, 1.0, 0.0, 0.8))
            ctx.call("gripper.position", opening=0.0)
            ctx.call("arm.joint_position", position=pose(-amplitude, -0.5, 1.0, 0.0, 0.8))
            ctx.call("gripper.position", opening=1.0)
            ctx.progress((i + 1) / (cycles + 1), f"第 {i + 1} 次挥手")
        final = ctx.call("arm.joint_position", position=pose())
        return {"cycles": cycles, "final_position": final["position"]}
