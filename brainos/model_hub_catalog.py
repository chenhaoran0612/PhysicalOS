"""The kinds of model the model library keeps, and the measures each kind is judged by (issue #71, docs/MODEL_HUB.md).

A model belongs to one category. A category binds the measures: an evaluation of a model reports values
for its category's measures and for no others, an evaluation set may set its own target per measure, and
one measure of each category is the one a round of improvement is decided on (``primary``).

Everything here is data and pure functions; nothing reads the store.
"""
import math

HIGHER, LOWER = 'higher', 'lower'


def _measure(key, name, unit, better=HIGHER, target=None, primary=False, low=0.0, high=100.0):
    return {'id': key, 'name': name, 'unit': unit, 'better': better, 'target': target, 'primary': primary, 'min': low, 'max': high}


BIG = 1_000_000.0
CATEGORIES = (
    {'id': 'motion_rl', 'name': '动作类强化模型', 'en': 'MOTION POLICY', 'embodied': True,
     'summary': '在仿真里用强化学习训练出的整段动作或步态。',
     'measures': (_measure('success_rate', '成功率', '%', target=100.0, primary=True),
                  _measure('mean_duration_s', '平均用时', '秒', LOWER, high=BIG),
                  _measure('mean_steps', '平均控制步数', '步', LOWER, high=BIG),
                  _measure('mean_return', '平均回报', '', low=-BIG, high=BIG))},
    {'id': 'navigation', 'name': '导航模型', 'en': 'NAVIGATION', 'embodied': True,
     'summary': '定位、建图、路径规划和避障。',
     'measures': (_measure('reach_rate', '到达成功率', '%', target=95.0, primary=True),
                  _measure('path_efficiency', '路径效率', '%'),
                  _measure('collision_rate', '碰撞率', '%', LOWER, target=0.0),
                  _measure('position_error_m', '到点误差', '米', LOWER, target=0.2, high=BIG))},
    {'id': 'intent', 'name': '意图识别模型', 'en': 'INTENT', 'embodied': False,
     'summary': '把人说的话或做的手势判成机器人要做的事。',
     'measures': (_measure('accuracy', '准确率', '%', target=95.0, primary=True),
                  _measure('macro_f1', '宏平均 F1', '%'),
                  _measure('false_trigger_rate', '误触发率', '%', LOWER, target=1.0),
                  _measure('latency_ms', '响应时延', '毫秒', LOWER, high=BIG))},
    {'id': 'llm', 'name': '大语言模型（LLM）', 'en': 'LLM', 'embodied': False,
     'summary': '理解任务、分解步骤、调用技能和工具。',
     'measures': (_measure('pass_rate', '任务通过率', '%', target=90.0, primary=True),
                  _measure('tool_call_accuracy', '工具调用正确率', '%'),
                  _measure('first_token_ms', '首字时延', '毫秒', LOWER, high=BIG),
                  _measure('tokens_per_s', '生成速度', '字/秒', high=BIG))},
    {'id': 'vlm', 'name': '视觉语言模型（VLM）', 'en': 'VLM', 'embodied': False,
     'summary': '看图或视频回答问题、描述场景、指出目标。',
     'measures': (_measure('vqa_accuracy', '问答准确率', '%', target=85.0, primary=True),
                  _measure('grounding_iou', '目标定位 IoU', '%'),
                  _measure('hallucination_rate', '幻觉率', '%', LOWER, target=5.0),
                  _measure('latency_ms', '响应时延', '毫秒', LOWER, high=BIG))},
    {'id': 'vla', 'name': '视觉-语言-动作模型（VLA）', 'en': 'VLA', 'embodied': True,
     'summary': '从画面和指令直接输出动作的端到端模型。',
     'measures': (_measure('task_success_rate', '任务成功率', '%', target=80.0, primary=True),
                  _measure('subgoal_rate', '子目标完成率', '%'),
                  _measure('takeover_rate', '人工接管率', '%', LOWER, target=5.0),
                  _measure('control_hz', '控制频率', 'Hz', high=BIG))},
    {'id': 'perception', 'name': '感知模型', 'en': 'PERCEPTION', 'embodied': False,
     'summary': '检测、分割、跟踪和位姿估计。',
     'measures': (_measure('map50', 'mAP@0.5', '%', target=80.0, primary=True),
                  _measure('recall', '召回率', '%'),
                  _measure('fps', '帧率', '帧/秒', high=BIG),
                  _measure('latency_ms', '单帧时延', '毫秒', LOWER, high=BIG))},
    {'id': 'speech', 'name': '语音模型', 'en': 'SPEECH', 'embodied': False,
     'summary': '唤醒、语音识别和语音合成。',
     'measures': (_measure('cer', '字错率', '%', LOWER, target=5.0, primary=True),
                  _measure('wake_rate', '唤醒率', '%', target=95.0),
                  _measure('false_wake_per_day', '误唤醒', '次/天', LOWER, high=BIG),
                  _measure('latency_ms', '响应时延', '毫秒', LOWER, high=BIG))},
    {'id': 'other', 'name': '其他模型', 'en': 'OTHER', 'embodied': False,
     'summary': '上面几类之外的模型。',
     'measures': (_measure('score', '综合得分', '分', primary=True),)},
)
BY_ID = {c['id']: c for c in CATEGORIES}
MOTION = 'motion_rl'
EPSILON = 1e-9


def category(key):
    """The category ``key``; ``ValueError`` in words when there is none."""
    found = BY_ID.get(key) if isinstance(key, str) else None
    if found is None:
        raise ValueError('请选择模型分类')
    return found


def measures(key):
    return category(key)['measures']


def primary(key):
    return next(m for m in measures(key) if m['primary'])


def _number(value, measure, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f'{what}「{measure["name"]}」需要是数字')
    if not measure['min'] <= value <= measure['max']:                  # also stops an integer too large to be a float
        raise ValueError(f'{what}「{measure["name"]}」需要在 {measure["min"]:g} 到 {measure["max"]:g} 之间')
    return round(float(value), 4)


def clean_values(key, values, what='指标'):
    """``values`` ({measure id: number}) for a category: known measures only, each a number in its range."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError(f'{what}需要按指标填写')
    known = {m['id']: m for m in measures(key)}
    cleaned = {}
    for name, value in values.items():
        if name not in known:
            raise ValueError(f'这个分类没有「{str(name)[:40]}」这个指标')
        if value is None or value == '':
            continue
        cleaned[name] = _number(value, known[name], what)
    return cleaned


def clean_results(key, values):
    """The measured values of an evaluation: as :func:`clean_values`, and the primary measure is there."""
    cleaned = clean_values(key, values)
    if primary(key)['id'] not in cleaned:
        raise ValueError(f'请填写「{primary(key)["name"]}」：这个分类的模型按它判断是否达标')
    return cleaned


def targets(key, overrides=None):
    """The target of every measure that has one: the category's, replaced by an evaluation set's own."""
    result = {m['id']: m['target'] for m in measures(key) if m['target'] is not None}
    result.update(overrides or {})
    return result


def meets(measure, value, target):
    return value >= target - EPSILON if measure['better'] == HIGHER else value <= target + EPSILON


def judge(key, values, overrides=None):
    """Each measure of ``values`` against its target, and whether every measure that has a target meets it.

    ``met`` is ``None`` while a measure with a target has no value: nothing can be said yet."""
    wanted, rows, met = targets(key, overrides), [], True
    for measure in measures(key):
        value, target = values.get(measure['id']), wanted.get(measure['id'])
        ok = None if value is None or target is None else meets(measure, value, target)
        rows.append({'id': measure['id'], 'value': value, 'target': target, 'met': ok})
        if target is not None:
            met = None if value is None or met is None else met and ok
    return {'rows': rows, 'met': met}


def compare(key, before, after):
    """How ``after`` differs from ``before`` per measure: the change, and whether it is for the better."""
    rows = []
    for measure in measures(key):
        old, new = before.get(measure['id']), after.get(measure['id'])
        if old is None or new is None:
            rows.append({'id': measure['id'], 'before': old, 'after': new, 'change': None, 'direction': 'unknown'})
            continue
        change = round(new - old, 4)
        better = change > EPSILON if measure['better'] == HIGHER else change < -EPSILON
        rows.append({'id': measure['id'], 'before': old, 'after': new, 'change': change,
                     'direction': 'same' if abs(change) <= EPSILON else 'better' if better else 'worse'})
    main = next(r for r in rows if r['id'] == primary(key)['id'])
    return {'rows': rows, 'primary': main['direction']}
