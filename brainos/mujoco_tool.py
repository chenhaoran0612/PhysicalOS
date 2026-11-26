"""Check a simulation before it is used, in a process of its own (issue #51, docs/MUJOCO_SIM.md).

    python -m brainos.mujoco_tool check CONFIG.json OUT_DIR

``CONFIG.json`` holds ``model_dir``, ``scene`` and ``family`` (as ``mujoco_body`` reads them). The tool
builds the MuJoCo model, runs it for a second, and writes ``OUT_DIR/preview.png``. It prints one line of
JSON: ``{"ok": true, "summary": {...}, "preview": true|false, "render_error": "..."}`` or
``{"ok": false, "error": "..."}``.

The console never imports MuJoCo itself: a robot model comes from a file somebody uploaded, and whatever
that file does to the loader happens here, in a child the console can time out and kill. Inside the
packaged app the backend binary is started with ``--nx-mujoco-tool`` and the same arguments
(``desktop/launcher.py``).
"""
import json
import sys
from pathlib import Path

PREVIEW_SIZE = (480, 640)


def _preview(world, out_dir):
    import mujoco
    from .mujoco_body import png
    renderer = mujoco.Renderer(world.model, PREVIEW_SIZE[0], PREVIEW_SIZE[1])
    camera = mujoco.MjvCamera()
    camera.type = mujoco.mjtCamera.mjCAMERA_FREE
    x, y, _yaw, height = world.base_pose()
    points = [(x, y)] + [(pose[0], pose[1]) for pose in world.object_poses().values()] \
        + [(p['x'], p['y']) for p in world.places]
    cx = (min(p[0] for p in points) + max(p[0] for p in points)) / 2.0
    cy = (min(p[1] for p in points) + max(p[1] for p in points)) / 2.0
    spread = max(max(abs(p[0] - cx), abs(p[1] - cy)) for p in points)
    camera.lookat[:] = [cx, cy, max(0.3, height)]
    camera.distance = max(2.5, 2.6 * spread + 1.5)
    camera.azimuth, camera.elevation = 135.0, -35.0
    renderer.update_scene(world.data, camera)
    (out_dir / 'preview.png').write_bytes(png(renderer.render()))


def check(config_path, out_dir):
    from . import runtime_bridge            # noqa: F401  (puts runtime + spec on sys.path)
    from . import mujoco_build, mujoco_scene
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        scene = mujoco_scene.validate(config['scene'])
        # The body itself, as the simulated robot will build it, with the capability table the runtime will check:
        # what passes here starts.
        from neuxmind_runtime.clock import ManualClock
        from neuxmind_spec.manifest import validate_manifest_v2
        from . import mujoco_body
        settings = {'vendor': 'check', 'model': 'check', 'instance_id': 'mujoco-check', **config, 'scene': scene}
        cls = mujoco_body.MujocoArm if config['family'] in mujoco_build.FIXED_BASE else mujoco_body.MujocoBase
        body = cls(settings, clock=ManualClock())
        validate_manifest_v2(body.manifest())
        world = body.world
        summary = mujoco_build.settle(world)
    except (mujoco_build.BuildError, mujoco_scene.SceneError) as exc:
        return {'ok': False, 'error': str(exc)}
    except ValueError as exc:                # the body or its capability table: e.g. a joint whose lower limit is not below its upper
        return {'ok': False, 'error': f'这个机器人模型不能作为仿真机器人：{exc}'}
    result = {'ok': True, 'summary': summary, 'preview': False, 'render_error': ''}
    try:
        _preview(world, out_dir)
        result['preview'] = True
    except Exception as exc:                # no OpenGL on this computer: the scene is still valid
        result['render_error'] = str(exc)[:300]
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] != 'check':
        print(json.dumps({'ok': False, 'error': '用法：mujoco_tool check CONFIG.json OUT_DIR'}, ensure_ascii=False))
        return 2
    from . import runtime_bridge            # noqa: F401  (puts runtime + spec on sys.path)
    try:
        result = check(argv[1], argv[2])
    except Exception as exc:
        result = {'ok': False, 'error': f'检查仿真环境时出错：{type(exc).__name__}: {str(exc)[:400]}'}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
