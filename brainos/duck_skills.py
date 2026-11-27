"""Every trained skill model of the Microduck is a skill of the robot (issue #67, docs/TRAINED_SKILLS.md).

A model trained in simulation is what the robot can do. So each packaged show is also a skill package of
the runtime (``skill.json`` and a few lines that call the standard capability ``policy.run`` with the
model's name), and a simulated duck comes with all of them:

* they are installed on it when it starts (``/v2/skills/install``; the runtime keeps them, a second
  install of the same version is "already installed");
* they are registered as skills of the brand and model, so they are listed with the other skills and can
  be put into a robot application.

Nothing here holds a weight or a mesh: a skill package names its model, the model is in the kit.
"""
import hashlib
import json

from . import connector, robot_model
from .errors import CodedError

PREFIX = 'duck.'
TEMPLATE = 0                    # the patch number of every skill version: raise it when the text below changes
CAPABILITY = 'policy.run'
REQUIRES = (CAPABILITY, 'safety.stop')
TIMEOUT_S = 120
MAX_SEED = 2 ** 32 - 1
SUCCESS = '这个技能模型从头到尾做完，并且通过它自己的裁判'
STOP = '机器人摔倒或裁判判定失败、被停止，或者超过时限'
SEED = {'name': 'seed', 'type': 'int', 'required': False, 'title': '起姿编号',
        'description': '同一个编号从同一个起姿开始。不填时用验收录像的那一回合', 'min': 0, 'max': MAX_SEED}

CODE = '''"""{title}: a skill model trained for this robot. The robot runs it itself (policy.run)."""
MODEL = {model!r}
MODEL_SHA256 = {sha256!r}       # the model package this skill was made for


def run(ctx, seed=None):
    have = ctx.param("policy.run", "policies") or []
    if MODEL not in have:
        raise ValueError("这台机器人上没有这个技能模型：" + MODEL)
    args = {{"name": MODEL}}
    if seed is not None:
        args["seed"] = int(seed)
    ctx.log("开始运行技能模型", **args)
    result = ctx.call("policy.run", **args)
    if not result.get("completed"):
        raise RuntimeError("技能模型没有做完：" + str(result.get("reason") or "没有说明原因"))
    ctx.progress(1.0, "做完了")
    return {{"completed": True, "model": MODEL, "steps": result.get("steps"), "duration_s": result.get("duration_s")}}
'''


def skill_id(show):
    return PREFIX + show['id']


def version(show):
    """Round and update of the training run that made the model, so a retrained model is a new version."""
    origin = show.get('origin') if isinstance(show.get('origin'), dict) else {}
    numbers = [origin.get('round'), origin.get('update')]
    if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in numbers):
        return f'{numbers[0]}.{numbers[1]}.{TEMPLATE}'
    return f'1.0.{TEMPLATE}'


def describe(show):
    """What the page and the records say about the skill of ``show`` (no package is read for this)."""
    return {'id': skill_id(show), 'version': version(show), 'title': show['title']}


def package(show):
    """The installable skill package of a show that has a model package."""
    accepted = show.get('acceptance') if isinstance(show.get('acceptance'), dict) else {}
    scores = '，'.join(f'{name} {pair[0]}/{pair[1]}' for name, pair in (('固定组', accepted.get('fixed')), ('独立组', accepted.get('independent')))
                      if isinstance(pair, (list, tuple)) and len(pair) == 2)
    words = [show['summary'], '由仿真里训练出的模型完成，机器人按这个模型自己做完整个动作。']
    if scores:
        words.append(f'验收成绩：{scores}。')
    if show.get('note'):
        words.append(show['note'])
    return {'schema': 'skill.package.v1', **describe(show), 'description': ''.join(words)[:2000],
            'entry': 'skill.py:run', 'requires': [{'id': cap} for cap in REQUIRES], 'parameters': [dict(SEED)],
            'timeout_s': TIMEOUT_S,
            'files': {'skill.py': CODE.format(title=show['title'], model=show['id'], sha256=show.get('sha256', ''))}}


def install(robot, show):
    """Install the skill of ``show`` on a running simulated duck; ``''`` or why it is not there."""
    made = package(show)
    # The request is named after the content: a package whose words changed is a new request, not a replay of the old answer.
    content = hashlib.sha256(json.dumps(made, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    request = f'duck-skill-{content}-{made["id"]}-{made["version"]}'[:100]
    try:
        answer = connector.call(robot['endpoint'], '/v2/skills/install', robot.get('token', ''),
                                {'request_id': request, 'package': made}, timeout=15)
    except connector.DeviceError as exc:
        if exc.code == 'version_immutable':
            # This version is on the robot with other words (a description from an earlier release). A version
            # is never replaced; it runs the same model by the same name, so it is the skill.
            return ''
        return f'没有装上：{str(exc)[:200]}'
    return '' if answer.get('installed') is True else '机器人没有确认装上'


def register(platform, robot, show):
    """The skill of ``show`` as a skill of the robot's brand and model (once per brand, model and version)."""
    made, product = package(show), robot_model.of(robot)
    for known in platform.store.list('skills'):
        same = known.get('entrypoint') == made['id'] and known.get('version') == made['version']
        theirs = robot_model.of(known)
        if same and (product is None or (theirs is not None and robot_model.key(theirs) == robot_model.key(product))):
            return known
    return platform.add_skill({'project_id': robot['project_id'], 'robot_id': robot['id'], 'name': made['title'],
                               'version': made['version'], 'entrypoint': made['id'], 'requires': list(REQUIRES),
                               'success_condition': SUCCESS, 'stop_condition': STOP, 'parameters': made['parameters']})


def bind(platform, robot, shows):
    """Install and register the skills of ``shows`` for a running simulated duck. Returns what the robot
    record keeps: ``{show id: {"skill", "version", "installed", "error"}}``. Never raises: a skill that
    could not be bound is said there, and the duck is usable without it."""
    bound = {}
    for show in shows:
        row = {'skill': skill_id(show), 'version': version(show), 'installed': False, 'error': ''}
        try:
            row['error'] = install(robot, show)
            row['installed'] = not row['error']
            if row['installed']:
                try:
                    register(platform, robot, show)
                except (ValueError, CodedError, OSError) as exc:
                    row['error'] = f'已经装上，但没有登记到技能列表：{str(exc)[:200]}'
        except (ValueError, CodedError, OSError) as exc:
            row['error'] = row['error'] or str(exc)[:200]
        bound[show['id']] = row
    return bound


def text(show):
    """The package as it would be written to a folder (for the docs and a person who wants to read it)."""
    made = package(show)
    files = made.pop('files')
    return {'skill.json': json.dumps(made, ensure_ascii=False, indent=2) + '\n', **files}
