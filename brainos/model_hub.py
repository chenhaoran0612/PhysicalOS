"""The model library of an account (issue #71, docs/MODEL_HUB.md).

What is kept (all for the account, none for one task):

* **models** by category (``model_hub_catalog``); a model for a body names its brand and model. The
  Microduck's skill models (``duck_kit.SHOWS``) are motion models that are always there: they are read
  from that list, not saved, so the library follows it.
* **versions**: the model files of a model (``model_hub_files``). The package that came with the app is
  a version too.
* **sets**: the evaluation library and the training library. An evaluation set of a motion model is a
  list of start poses; any other set says how many items it has and may carry a file.
* **evaluations**: the measures of one version on one evaluation set. Run here for a packaged skill
  model of the duck on a simulated duck (``model_hub_eval``), imported for everything else.
* **rounds** of improvement: baseline → diagnose → training (outside the app) → candidate → review →
  accepted or rejected. An accepted candidate is the model's current version.
* which **skill** a model has become (a skill record of the same brand and model).
"""
import re
import secrets
import threading

from . import duck_kit, model_hub_catalog as catalog, robot_model
from .contracts import required_text
from .errors import CodedError
from .model_hub_eval import Runner
from .model_hub_files import LocalFiles

MODELS, VERSIONS, SETS, EVALS, ROUNDS, LINKS = 'hub_models', 'hub_versions', 'hub_sets', 'hub_evaluations', 'hub_rounds', 'hub_skill_links'
DUCK_MODEL, DUCK_VERSION, DUCK_EVAL = 'hubm-duck-', 'hubv-duck-', 'hube-duck-'
FIXED, INDEPENDENT = 'hubs-duck-fixed', 'hubs-duck-independent'
# The start poses the training project evaluates on, and a second, pre-registered set (the packages name both).
# Issue #79: every version has its own independent set (``independent_from`` in duck_kit.SHOWS); the one issue
# #71 knew (that of 鞠躬鞠过头 round 40) keeps its id, the others are ``hubs-duck-independent-<first seed>``.
FIXED_SEEDS = tuple(range(1_700_003_000, 1_700_003_010))
INDEPENDENT_SEEDS = tuple(range(1_991_724_000, 1_991_724_010))
ACCEPTANCE = (('fixed', FIXED), ('independent', INDEPENDENT))


def _independent_id(first):
    return INDEPENDENT if first in (None, INDEPENDENT_SEEDS[0]) else f'{INDEPENDENT}-{first}'
ACCEPTANCE_SOURCE = '训练项目的验收记录'
EVALUATION, TRAINING = 'evaluation', 'training'
PURPOSES = {EVALUATION: '评测集', TRAINING: '训练集'}
MAX_SEEDS, MAX_COUNT, MAX_FILES = 200, 1_000_000_000, 20
OPEN = ('baseline', 'diagnose', 'training', 'candidate', 'review')
ACCEPTED, REJECTED = 'accepted', 'rejected'
STATUS_NAMES = {'delivered': '已交付', 'candidate': '候选', 'training': '训练中', 'empty': '还没有模型文件',
                'unrated': '待评测', 'below': '未达标', 'qualified': '已达标', 'archived': '已归档'}
NOT_RUNNABLE = '只有随这个安装带来的小黄鸭技能模型包可以在仿真里评测；它们的旧版本、上传的模型文件和其他分类的模型，请导入评测结果'


def _id(prefix):
    return prefix + secrets.token_hex(8)


def _text(value, name, limit):
    return required_text(value, name, limit) if isinstance(value, str) and value.strip() else ''


def _whole(value, name, high):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise ValueError(f'{name}需要是 0 到 {high} 之间的整数')
    return value


def _product(p):
    """The brand and model a model or a set is for: both or neither."""
    vendor, model = _text(p.get('vendor'), '品牌', 80), _text(p.get('model'), '型号', 80)
    if bool(vendor) != bool(model):
        raise ValueError('品牌和型号要一起填写，或者都不填')
    return {'vendor': vendor, 'model': model} if vendor else None


def _seeds(value):
    """Start poses as a person typed them (numbers apart by commas or spaces), each once, in order."""
    parts = value if isinstance(value, list) else re.split(r'[\s,，、]+', value.strip()) if isinstance(value, str) else []
    seeds = []
    for part in parts:
        if part == '':
            continue
        if isinstance(part, bool) or not (isinstance(part, int) or (isinstance(part, str) and part.isdigit())) or not 0 <= int(part) <= duck_kit.MAX_SEED:
            raise ValueError(f'起姿编号需要是 0 到 {duck_kit.MAX_SEED} 之间的整数，用逗号或空格分开')
        if int(part) not in seeds:
            seeds.append(int(part))
    if not 1 <= len(seeds) <= MAX_SEEDS:
        raise ValueError(f'请填写 1 到 {MAX_SEEDS} 个起姿编号')
    return seeds


class ModelHub:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.files = LocalFiles(platform)
        self.lock = threading.RLock()
        self.runner = Runner(self)
        # What an evaluation asks of a simulated duck; the tests put a made-up duck here.
        self.perform = lambda p: platform.duck.perform(p)
        self.status = lambda p: platform.duck.status(p)
        self.halt = lambda p: platform.duck.stop(p)
        self.poll_s = 0.2
        self.files.sweep(self._claimed())
        from .model_hub_history import History
        self.history = History(self)                # issue #76: a round's candidate chosen from the model's history

    @staticmethod
    def _now():
        from .service import now
        return now()

    # ------------------------------------------------------------ what came with the app
    def runnable_show(self, show):
        """Whether this installation has the package of a duck's skill model."""
        found = duck_kit.BY_ID.get(show)
        return bool(found and found.get('package') and self.platform.duck._zip(found))

    def known_duck(self, robot_id):
        """Whether ``robot_id`` is a simulated duck of this account (asked before an evaluation is recorded)."""
        try:
            return bool((self.store.get('robots', robot_id).get('sim') or {}).get('duck'))
        except ValueError:
            return False

    @staticmethod
    def _duck_model(show):
        return {'id': DUCK_MODEL + show['id'], 'name': show['title'], 'category': catalog.MOTION, 'summary': show['summary'],
                'product': {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL}, 'builtin': True, 'show': show['id'],
                'note': show.get('note', ''), 'created_at': ''}

    @staticmethod
    def _duck_version(show, version=None):
        """A version of a duck's skill model: the package in the kit, or (issue #79) one that came with an earlier release."""
        version = version or show
        origin, current = version.get('origin') or {}, version is show
        return {'id': f"{DUCK_VERSION}{show['id']}-{version['sha256'][:12]}", 'model_id': DUCK_MODEL + show['id'],
                'label': f"第 {origin.get('round', '?')} 轮 · 第 {origin.get('update', '?')} 次更新", 'builtin': True, 'round_id': '',
                'superseded': not current,
                'files': [{'id': '', 'filename': version['package'], 'size': None, 'sha256': version['sha256']}],
                'origin': {k: origin[k] for k in ('round', 'update', 'steps', 'device') if k in origin},
                'note': '随应用带来的模型包' if current else duck_kit.OLD_RULES, 'created_at': ''}

    def _duck_versions(self, show):
        return [self._duck_version(show, version) for version, _current in duck_kit.versions(show)]

    @staticmethod
    def _duck_sets():
        base = {'purpose': EVALUATION, 'category': catalog.MOTION, 'product': {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL},
                'targets': {}, 'source': '训练项目登记的起姿', 'file': None, 'builtin': True, 'created_at': ''}
        found = {FIXED: {**base, 'id': FIXED, 'name': '固定起姿组', 'seeds': list(FIXED_SEEDS), 'count': len(FIXED_SEEDS),
                         'description': '训练时每次评估都用的十个起姿，所有模型和版本都一样。'}}
        for show in duck_kit.SHOWS:
            for version, _current in duck_kit.versions(show):
                key = _independent_id(version.get('independent_from'))
                if key in found:
                    continue
                seeds = duck_kit.seed_group(version['independent_from']) if version.get('independent_from') else INDEPENDENT_SEEDS
                origin = version.get('origin') or {}
                found[key] = {**base, 'id': key, 'name': f"独立起姿组 · {show['title']} 第 {origin.get('round', '?')} 轮",
                              'seeds': list(seeds), 'count': len(seeds),
                              'description': '为这一版在训练前登记好、训练中从不使用的十个起姿，用来确认模型不是只会固定的那十个。'}
        if INDEPENDENT not in found:                 # the id issue #71 gave it stays usable for what refers to it
            found[INDEPENDENT] = {**base, 'id': INDEPENDENT, 'name': '独立起姿组', 'seeds': list(INDEPENDENT_SEEDS), 'count': len(INDEPENDENT_SEEDS),
                                  'description': '训练前就登记好、训练中从不使用的十个起姿，用来确认模型不是只会固定的那十个。'}
        return list(found.values())

    def _duck_records(self, show):
        """Issue #79: the accepted episodes of the package in the kit, by group (``{}`` when it has none)."""
        try:
            found = self.platform.duck.evidence({'show': show['id']}).get('records') or {}
        except (CodedError, ValueError, OSError):
            return {}
        return {group['id']: group for group in found.get('groups', [])}

    @staticmethod
    def _duck_metrics(passed, total, cases):
        metrics = {'success_rate': round(100 * passed / total, 2)}
        if cases and len(cases) == total:
            for name, key in (('mean_duration_s', 'duration_s'), ('mean_steps', 'steps'), ('mean_return', 'return')):
                values = [c[key] for c in cases if c.get(key) is not None]
                if len(values) == total:
                    metrics[name] = round(sum(values) / total, 4)
        return metrics

    def _duck_evaluations(self, show):
        """The training project's acceptance of each version, as two imported evaluations; for the package in
        the kit with every episode (issue #79). A version recorded before issue #79 keeps the ids it had."""
        results = []
        for version, current in duck_kit.versions(show):
            groups = self._duck_records(show) if current else {}
            version_id = self._duck_version(show, version)['id']
            legacy = version.get('legacy') or 'independent_from' not in version
            for key, set_id in ((key, FIXED if key == 'fixed' else _independent_id(version.get('independent_from'))) for key, _old in ACCEPTANCE):
                passed, total = (version.get('acceptance') or {}).get(key) or (None, None)
                if not total:
                    continue
                cases = [{**{k: c.get(k) for k in ('seed', 'success', 'steps', 'duration_s', 'return', 'recording')},
                          'reason': '' if c.get('success') else c.get('reason', '')}            # a passed episode needs no reason
                         for c in (groups.get(key) or {}).get('cases', [])]
                results.append({'id': f"{DUCK_EVAL}{show['id']}-{key}" if legacy else f"{DUCK_EVAL}{show['id']}-{version['sha256'][:12]}-{key}",
                                'model_id': DUCK_MODEL + show['id'], 'version_id': version_id, 'set_id': set_id,
                                'mode': 'imported', 'status': 'done', 'metrics': self._duck_metrics(passed, total, cases),
                                'passed': passed, 'total': total, 'source': ACCEPTANCE_SOURCE, 'round_id': '', 'error': '', 'created_at': '', 'finished_at': '',
                                'cases': cases})
        return results

    # ------------------------------------------------------------ finding
    def _model(self, key):
        if isinstance(key, str) and key.startswith(DUCK_MODEL) and key[len(DUCK_MODEL):] in duck_kit.BY_ID:
            return self._duck_model(duck_kit.BY_ID[key[len(DUCK_MODEL):]])
        try:
            if not isinstance(key, str):
                raise ValueError('model')
            return self.store.get(MODELS, key)
        except ValueError:
            raise CodedError('没有找到这个模型', 'hub_model_unknown') from None

    def _of(self, kind, model_id):
        return [x for x in self.store.list(kind) if x.get('model_id') == model_id]

    def _versions(self, model):
        """Newest first; the package that came with the app last."""
        builtin = duck_kit.BY_ID.get(model.get('show') or '')
        return self._of(VERSIONS, model['id']) + (self._duck_versions(builtin) if builtin else [])

    def _version(self, model, key):
        found = next((v for v in self._versions(model) if v['id'] == key), None)
        if found is None:
            raise CodedError('没有找到这个模型的这一版', 'hub_version_unknown')
        return found

    def _sets(self):
        return self.store.list(SETS) + self._duck_sets()

    def _set(self, key, purpose=None, category=None):
        found = next((s for s in self._sets() if s['id'] == key), None)
        if found is None or (purpose and found['purpose'] != purpose) or (category and found['category'] != category):
            raise CodedError('没有找到这个分类的' + PURPOSES.get(purpose, '评测集或训练集'), 'hub_set_unknown')
        return found

    def _evaluations(self, model):
        builtin = duck_kit.BY_ID.get(model.get('show') or '')
        mine = sorted(self._of(EVALS, model['id']), key=lambda e: e.get('created_at', ''), reverse=True)        # newest first
        return mine + (self._duck_evaluations(builtin) if builtin else [])

    def _round(self, key):
        try:
            if not isinstance(key, str):
                raise ValueError('round')
            return self.store.get(ROUNDS, key)
        except ValueError:
            raise CodedError('没有找到这一轮改进', 'hub_round_unknown') from None

    def _claimed(self):
        """The files a version or a set holds."""
        ids = {f['id'] for v in self.store.list(VERSIONS) for f in v.get('files', [])}
        return ids | {s['file']['id'] for s in self.store.list(SETS) if s.get('file')}

    def _take(self, file_ids):
        """The records of freshly uploaded files that nothing holds yet."""
        if not isinstance(file_ids, list) or not file_ids or len(file_ids) > MAX_FILES or not all(isinstance(key, str) for key in file_ids) \
                or len(set(file_ids)) != len(file_ids):
            raise CodedError('请先上传文件', 'hub_file_unknown')
        claimed = self._claimed()
        if any(key in claimed for key in file_ids):
            raise CodedError('这个文件已经用在别处了，请重新上传', 'hub_file_unknown')
        return [self.files.view(key) for key in file_ids]

    # ------------------------------------------------------------ as the pages show it
    def _evaluation_view(self, record, cases=False):
        view = {k: record.get(k, '') for k in ('id', 'model_id', 'version_id', 'set_id', 'mode', 'status', 'source', 'round_id', 'error', 'created_at', 'finished_at')}
        view.update(metrics=dict(record.get('metrics') or {}), passed=record.get('passed'), total=record.get('total'), robot_id=record.get('robot_id', ''))
        if view['status'] == 'running' and not self.runner.going(record['id']):
            try:                                            # its thread may have ended between the read and the question
                record = self.store.get(EVALS, record['id'])
            except ValueError:
                pass
            if record.get('status') == 'running':
                view.update(status='interrupted', error='应用关闭时这次评测还没有跑完')
            else:
                return self._evaluation_view(record, cases)
        view['done'] = len(record.get('cases') or [])
        view['judgement'] = None
        if view['status'] == 'done':
            evaluated = self._set(record['set_id'])
            view['judgement'] = catalog.judge(evaluated['category'], view['metrics'], evaluated.get('targets'))
        if cases:
            view['cases'] = list(record.get('cases') or [])
        return view

    def _current(self, model, versions, rounds):
        """The version a model stands at: the newest of the accepted candidates and the versions added outside a round."""
        events = [(v.get('created_at', ''), v['id']) for v in versions if not v.get('round_id') and not v.get('superseded')]
        events += [(r.get('closed_at', ''), r['candidate_version_id']) for r in rounds if r['status'] == ACCEPTED]
        return max(events)[1] if events else ''

    def _status(self, model, current, evaluations):
        if not current:
            if model.get('builtin'):                        # issue #79: a show the training project no longer delivers
                return 'archived' if duck_kit.BY_ID[model['show']]['status'] == duck_kit.ARCHIVED else 'training'
            return 'empty'
        if current.startswith(DUCK_VERSION):
            return duck_kit.BY_ID[model['show']]['status']
        latest = next((e for e in evaluations if e['version_id'] == current and e['status'] == 'done'), None)
        met = latest['judgement']['met'] if latest else None
        return 'unrated' if met is None else 'qualified' if met else 'below'

    def _round_view(self, model, record, evaluations):
        view = {k: record.get(k, '') for k in ('id', 'model_id', 'number', 'status', 'version_id', 'set_id', 'goal', 'weaknesses', 'plan',
                                                'base_evaluation_id', 'candidate_version_id', 'candidate_evaluation_id', 'note', 'created_at', 'closed_at')}
        view['training_set_ids'] = list(record.get('training_set_ids') or [])
        done = {e['id']: e for e in evaluations if e['status'] == 'done'}
        base, candidate = done.get(view['base_evaluation_id']), done.get(view['candidate_evaluation_id'])
        view['comparison'] = catalog.compare(model['category'], base['metrics'], candidate['metrics']) if base and candidate else None
        view['base_met'] = base['judgement']['met'] if base else None
        view['candidate_met'] = candidate['judgement']['met'] if candidate else None
        return view

    def _skill(self, model, skills, links):
        """The skill a model has become: the one it was bound to, else (a duck's skill model) the skill made from it."""
        bound = next((s for s in skills if s['id'] == links.get(model['id'])), None)
        made = None if bound or not model.get('show') else next((s for s in skills if s.get('entrypoint') == 'duck.' + model['show']), None)
        found = bound or made
        return {'id': found['id'], 'name': found.get('name', ''), 'version': found.get('version', ''), 'auto': bound is None} if found else None

    def _model_view(self, model, skills=None, links=None):
        skills = self.store.list('skills') if skills is None else skills
        links = {x['id']: x['skill_id'] for x in self.store.list(LINKS)} if links is None else links
        versions = self._versions(model)
        evaluations = [self._evaluation_view(e) for e in self._evaluations(model)]
        records = sorted(self._of(ROUNDS, model['id']), key=lambda r: -r['number'])
        current = self._current(model, versions, records)
        status = self._status(model, current, evaluations)
        return {**{k: model.get(k) for k in ('id', 'name', 'category', 'summary', 'created_at')}, 'product': model.get('product'),
                'builtin': bool(model.get('builtin')), 'show': model.get('show') or '', 'note': model.get('note', ''),
                'status': status, 'status_name': STATUS_NAMES[status], 'current_version_id': current,
                'versions': [{**v, 'runnable': bool(v.get('builtin')) and not v.get('superseded') and self.runnable_show(model.get('show'))} for v in versions],
                'evaluations': evaluations, 'rounds': [self._round_view(model, r, evaluations) for r in records],
                'skill': self._skill(model, skills, links)}

    def _set_view(self, record, used=()):
        return {**{k: record.get(k) for k in ('id', 'purpose', 'name', 'category', 'product', 'description', 'count', 'source', 'file', 'created_at')},
                'seeds': list(record['seeds']) if record.get('seeds') else None, 'targets': dict(record.get('targets') or {}),
                'builtin': bool(record.get('builtin')), 'runnable': bool(record.get('seeds')) and record['purpose'] == EVALUATION,
                'used': sum(1 for key in used if key == record['id'])}

    def overview(self, p=None):
        """Everything the three pages draw (route ``model-hub/overview``)."""
        skills, links = self.store.list('skills'), {x['id']: x['skill_id'] for x in self.store.list(LINKS)}
        models = [self._model_view(m, skills, links) for m in self.store.list(MODELS) + [self._duck_model(s) for s in duck_kit.SHOWS]]
        used = [e['set_id'] for e in self.store.list(EVALS)] + [key for r in self.store.list(ROUNDS) for key in (r['set_id'], *r.get('training_set_ids', []))]
        used += [e['set_id'] for show in duck_kit.SHOWS for e in self._duck_evaluations(show)]
        problem = self.platform.duck.problem()
        return {'categories': [{**c, 'measures': list(c['measures'])} for c in catalog.CATEGORIES], 'status_names': dict(STATUS_NAMES),
                'models': models, 'sets': [self._set_view(s, used) for s in self._sets()],
                'duck': {'available': not problem, 'problem': problem, 'profile_id': duck_kit.PROFILE_ID},
                'limits': {'file_bytes': self.files_limit()}}

    @staticmethod
    def files_limit():
        from .model_hub_files import MAX_FILE_BYTES
        return MAX_FILE_BYTES

    # ------------------------------------------------------------ models and versions
    def add_model(self, p):
        """A model of one's own (route ``model-hub/models``)."""
        record = {'id': _id('hubm-'), 'name': required_text(p.get('name'), '模型名称', 100), 'category': catalog.category(p.get('category'))['id'],
                  'summary': _text(p.get('summary'), '说明', 1000), 'product': _product(p), 'created_at': self._now()}
        self.store.put(MODELS, record)
        self.platform.audit('hub.model_added', {'model_id': record['id'], 'category': record['category']})
        return self._model_view(record)

    def remove_model(self, p):
        """Remove a model of one's own with its versions, files, evaluations and rounds (route ``model-hub/models/remove``)."""
        model = self._model(p.get('model_id'))
        if model.get('builtin'):
            raise CodedError('随应用带来的模型不能删除', 'hub_builtin')
        if p.get('confirm') is not True:
            raise ValueError('请确认删除：模型文件、评测记录和改进记录会一起删除')
        with self.lock:
            if any(r['status'] in OPEN for r in self._of(ROUNDS, model['id'])) or any(self.runner.going(e['id']) for e in self._of(EVALS, model['id'])):
                raise CodedError('这个模型有一轮改进或一次评测正在进行，结束后才能删除', 'hub_in_use')
            versions = self._of(VERSIONS, model['id'])
            for kind in (VERSIONS, EVALS, ROUNDS):
                for record in self._of(kind, model['id']):
                    self.store.delete(kind, record['id'])
            self.store.delete(LINKS, model['id'])
            self.store.delete(MODELS, model['id'])
            self.files.remove([f['id'] for v in versions for f in v.get('files', [])])
        self.platform.audit('hub.model_removed', {'model_id': model['id']})
        return {'removed': model['id']}

    def add_version(self, p):
        """Uploaded files become a version of a model (route ``model-hub/versions``); in a round, its candidate."""
        model = self._model(p.get('model_id'))
        label = required_text(p.get('label'), '版本名', 80)
        origin = {k: v for k, v in (('round', _whole(p.get('origin_round'), '训练轮次', 10 ** 6)), ('update', _whole(p.get('origin_update'), '更新次数', 10 ** 9)),
                                    ('steps', _whole(p.get('origin_steps'), '训练步数', 10 ** 15))) if v is not None}
        if _text(p.get('origin_device'), '训练设备', 80):
            origin['device'] = _text(p.get('origin_device'), '训练设备', 80)
        with self.lock:
            training = self._round(p['round_id']) if p.get('round_id') else None
            # In a round the upload is its candidate; one uploaded by mistake is replaced by the next (and evaluated anew).
            if training and (training['model_id'] != model['id'] or training['status'] not in ('training', 'candidate', 'review')):
                raise CodedError('这一轮现在不是上传新模型的时候：先完成基线评测并写下短板', 'hub_round_state')
            if any(v['label'] == label for v in self._versions(model)):
                raise ValueError('这个模型已经有一个同名的版本')
            record = {'id': _id('hubv-'), 'model_id': model['id'], 'label': label, 'files': self._take(p.get('file_ids')), 'origin': origin,
                      'note': _text(p.get('note'), '说明', 1000), 'round_id': training['id'] if training else '', 'builtin': False, 'created_at': self._now()}
            self.store.put(VERSIONS, record)
            if training:
                self.store.put(ROUNDS, {**training, 'candidate_version_id': record['id'], 'candidate_evaluation_id': '', 'status': 'candidate'})
        self.platform.audit('hub.version_added', {'model_id': model['id'], 'version_id': record['id'], 'sha256': [f['sha256'] for f in record['files']]})
        return record

    def bind_skill(self, p):
        """Say which skill a model has become, or (empty ``skill_id``) that it has none (route ``model-hub/skill``)."""
        model = self._model(p.get('model_id'))
        if not p.get('skill_id'):
            self.store.delete(LINKS, model['id'])
            return self._model_view(model)
        skill = next((s for s in self.store.list('skills') if s['id'] == p['skill_id']), None)
        if skill is None:
            raise CodedError('没有找到这个技能', 'hub_skill_unknown')
        theirs, mine = robot_model.key(robot_model.of(skill)), robot_model.key(model.get('product'))
        if theirs and mine and theirs != mine:
            raise CodedError(f'这个技能是「{robot_model.label(robot_model.of(skill))}」的，模型是「{robot_model.label(model["product"])}」的', 'hub_skill_other_model')
        self.store.put(LINKS, {'id': model['id'], 'skill_id': skill['id']})
        self.platform.audit('hub.skill_bound', {'model_id': model['id'], 'skill_id': skill['id']})
        return self._model_view(model)

    # ------------------------------------------------------------ the evaluation library and the training library
    def add_set(self, p):
        """An evaluation set or a training set (route ``model-hub/sets``)."""
        if p.get('purpose') not in PURPOSES:
            raise ValueError('请选择是评测集还是训练集')
        kind = catalog.category(p.get('category'))['id']
        poses = p['purpose'] == EVALUATION and kind == catalog.MOTION
        if p.get('seeds') and not poses:
            raise ValueError('起姿编号只用于动作类强化模型的评测集')
        if p.get('targets') and p['purpose'] != EVALUATION:
            raise ValueError('达标线只用于评测集')
        seeds = _seeds(p.get('seeds')) if poses else None
        count = len(seeds) if poses else _whole(p.get('count'), '条数', MAX_COUNT)
        if not count:
            raise ValueError('请填写这个集有多少条')
        with self.lock:
            record = {'id': _id('hubs-'), 'purpose': p['purpose'], 'name': required_text(p.get('name'), '名称', 100), 'category': kind,
                      'product': _product(p), 'description': required_text(p.get('description'), '说明', 2000), 'seeds': seeds, 'count': count,
                      'source': _text(p.get('source'), '来源', 500), 'targets': catalog.clean_values(kind, p.get('targets'), '达标线'),
                      'file': self._take([p['file_id']])[0] if p.get('file_id') else None, 'builtin': False, 'created_at': self._now()}
            self.store.put(SETS, record)
        self.platform.audit('hub.set_added', {'set_id': record['id'], 'purpose': record['purpose']})
        return self._set_view(record)

    def remove_set(self, p):
        """Remove a set of one's own that nothing used (route ``model-hub/sets/remove``)."""
        record = self._set(p.get('set_id'))
        if record.get('builtin'):
            raise CodedError('随应用带来的评测集不能删除', 'hub_builtin')
        with self.lock:
            if any(e['set_id'] == record['id'] for e in self.store.list(EVALS)) or \
                    any(record['id'] in (r['set_id'], *r.get('training_set_ids', [])) for r in self.store.list(ROUNDS)):
                raise CodedError('已经有评测或改进用了这个集，它要留着才能说明那些结果', 'hub_in_use')
            self.store.delete(SETS, record['id'])
            if record.get('file'):
                self.files.remove([record['file']['id']])
        return {'removed': record['id']}

    # ------------------------------------------------------------ evaluations
    def _slot(self, model, version, evaluated, round_id):
        """The round an evaluation is for, checked: its baseline (the round's version) or its candidate, on the round's set."""
        if not round_id:
            return None
        record = self._round(round_id)
        base = record['status'] in ('baseline', 'diagnose') and version['id'] == record['version_id']
        candidate = record['status'] in ('candidate', 'review') and version['id'] == record.get('candidate_version_id')
        if record['model_id'] != model['id'] or evaluated['id'] != record['set_id'] or not (base or candidate):
            raise CodedError('这一轮现在要的不是这次评测：基线评的是这一轮的起点版本，候选评的是新上传的版本，都用这一轮选定的评测集', 'hub_round_state')
        return record

    def finished(self, record):
        """An evaluation has its result: the round it is for moves on."""
        with self.lock:
            if record.get('round_id') and record['status'] == 'done':
                found = self._round(record['round_id'])
                if found['status'] in ('baseline', 'diagnose') and record['version_id'] == found['version_id']:
                    self.store.put(ROUNDS, {**found, 'base_evaluation_id': record['id'], 'status': 'diagnose' if found['status'] == 'baseline' else found['status']})
                elif found['status'] in ('candidate', 'review') and record['version_id'] == found.get('candidate_version_id'):
                    self.store.put(ROUNDS, {**found, 'candidate_evaluation_id': record['id'], 'status': 'review'})

    def _imported(self, model, version, evaluated, metrics, passed, total, source, round_id):
        with self.lock:
            self._slot(model, version, evaluated, round_id)
            record = {'id': _id('hube-'), 'model_id': model['id'], 'version_id': version['id'], 'set_id': evaluated['id'], 'mode': 'imported',
                      'status': 'done', 'metrics': metrics, 'passed': passed, 'total': total, 'source': source, 'round_id': round_id or '',
                      'error': '', 'cases': [], 'created_at': self._now(), 'finished_at': self._now()}
            self.store.put(EVALS, record)
            self.finished(record)
        self.platform.audit('hub.evaluation_imported', {'model_id': model['id'], 'evaluation_id': record['id']})
        return self._evaluation_view(record)

    def import_evaluation(self, p):
        """The measures of a version on an evaluation set, as measured elsewhere (route ``model-hub/evaluations/import``)."""
        model = self._model(p.get('model_id'))
        version = self._version(model, p.get('version_id'))
        evaluated = self._set(p.get('set_id'), EVALUATION, model['category'])
        metrics = catalog.clean_results(model['category'], p.get('metrics'))
        passed, total = _whole(p.get('passed'), '通过条数', MAX_COUNT), _whole(p.get('total'), '评测条数', MAX_COUNT)
        if (passed is None) != (total is None) or (total is not None and not 0 <= passed <= total):
            raise ValueError('通过条数和评测条数要一起填写，通过的不能比评测的多')
        return self._imported(model, version, evaluated, metrics, passed, total, required_text(p.get('source'), '结果出处', 500), p.get('round_id'))

    def import_training(self, p):
        """The training project's own evaluation of a version of a duck's skill model, read from its
        training progress by the round and update the version was made at (route ``model-hub/evaluations/import-training``)."""
        model = self._model(p.get('model_id'))
        version = self._version(model, p.get('version_id'))
        origin = version.get('origin') or {}
        task = next((t for t in self.platform.duck.training().get('tasks', []) if t.get('id') == model.get('show')), None)
        rows = [r for r in (task or {}).get('series', []) if r[0] == origin.get('round') and r[1] == origin.get('update') and r[3]]
        kept = version.get('training_result') or {}         # issue #76: the dashboard's result, kept when the file was chosen from it
        rows = rows or ([[origin.get('round'), origin.get('update'), kept['passed'], kept['total'], None]] if kept.get('total') else [])
        if not model.get('show') or 'round' not in origin or 'update' not in origin or not rows:
            raise CodedError('训练项目的评估记录里没有这一版（按它的训练轮次和更新次数找）。可以手动导入评测结果', 'hub_training_missing')
        _, _, passed, total, _ = rows[-1]
        source = f"训练项目的评估记录：第 {origin['round']} 轮第 {origin['update']} 次更新"
        return self._imported(model, version, self._set(FIXED), {'success_rate': round(100 * passed / total, 2)}, passed, total, source, p.get('round_id'))

    def run_evaluation(self, p):
        """Run a packaged skill model of the duck once per start pose of a set on a simulated duck (route ``model-hub/evaluations/run``)."""
        model = self._model(p.get('model_id'))
        version = self._version(model, p.get('version_id'))
        evaluated = self._set(p.get('set_id'), EVALUATION, model['category'])
        if not (version.get('builtin') and not version.get('superseded') and evaluated.get('seeds') and self.runnable_show(model.get('show'))):
            raise CodedError(NOT_RUNNABLE, 'hub_eval_not_runnable')
        if not isinstance(p.get('robot_id'), str) or not self.known_duck(p['robot_id']):
            raise CodedError('请先选择一个正在运行的仿真小鸭', 'hub_duck_needed')
        with self.lock:
            self._slot(model, version, evaluated, p.get('round_id'))
            if self.runner.busy(p['robot_id']):
                raise CodedError('这个仿真小鸭正在跑另一次评测，等它结束或先停止它', 'hub_eval_busy')
            record = {'id': _id('hube-'), 'model_id': model['id'], 'version_id': version['id'], 'set_id': evaluated['id'], 'mode': 'run',
                      'status': 'running', 'metrics': {}, 'passed': 0, 'total': len(evaluated['seeds']), 'seeds': list(evaluated['seeds']),
                      'source': '在这台电脑的仿真里运行', 'round_id': p.get('round_id') or '', 'robot_id': p['robot_id'], 'error': '',
                      'cases': [], 'created_at': self._now(), 'finished_at': ''}
            self.store.put(EVALS, record)
            self.runner.start(record, model['show'])
        self.platform.audit('hub.evaluation_started', {'model_id': model['id'], 'evaluation_id': record['id'], 'robot_id': p['robot_id']})
        return self._evaluation_view(record)

    def _evaluation(self, key):
        try:
            if not isinstance(key, str):
                raise ValueError('evaluation')
            return self.store.get(EVALS, key)
        except ValueError:
            raise CodedError('没有找到这次评测', 'hub_eval_unknown') from None

    def evaluation(self, p):
        """One evaluation with its runs (route ``model-hub/evaluations/detail``; asked while one is running)."""
        key = p.get('evaluation_id')
        if isinstance(key, str) and key.startswith(DUCK_EVAL):
            found = next((e for show in duck_kit.SHOWS for e in self._duck_evaluations(show) if e['id'] == key), None)
            if found:
                return self._evaluation_view(found, cases=True)
        return self._evaluation_view(self._evaluation(key), cases=True)

    def stop_evaluation(self, p):
        """End a running evaluation after the run that is going (route ``model-hub/evaluations/stop``)."""
        record = self._evaluation(p.get('evaluation_id'))
        self.runner.stop(record['id'])
        return self._evaluation_view(record, cases=True)

    # ------------------------------------------------------------ rounds of improvement
    def open_round(self, p):
        """Begin a round for a model: from one of its versions, judged on one evaluation set (route ``model-hub/rounds``)."""
        model = self._model(p.get('model_id'))
        version = self._version(model, p.get('version_id'))
        evaluated = self._set(p.get('set_id'), EVALUATION, model['category'])
        goal = required_text(p.get('goal'), '这一轮的目标', 500)
        with self.lock:
            rounds = self._of(ROUNDS, model['id'])
            if any(r['status'] in OPEN for r in rounds):
                raise CodedError('这个模型有一轮改进还没有结束：先采纳、不采纳或放弃它', 'hub_round_open')
            known = next((e for e in self._evaluations(model) if e['version_id'] == version['id'] and e['set_id'] == evaluated['id']
                          and e['status'] == 'done'), None)
            record = {'id': _id('hubr-'), 'model_id': model['id'], 'number': max([r['number'] for r in rounds], default=0) + 1,
                      'status': 'diagnose' if known else 'baseline', 'version_id': version['id'], 'set_id': evaluated['id'], 'goal': goal,
                      'weaknesses': '', 'plan': '', 'training_set_ids': [], 'base_evaluation_id': known['id'] if known else '',
                      'candidate_version_id': '', 'candidate_evaluation_id': '', 'note': '', 'created_at': self._now(), 'closed_at': ''}
            self.store.put(ROUNDS, record)
        self.platform.audit('hub.round_opened', {'model_id': model['id'], 'round_id': record['id'], 'number': record['number']})
        return self._round_view(model, record, [self._evaluation_view(e) for e in self._evaluations(model)])

    def _changed(self, record):
        self.store.put(ROUNDS, record)
        model = self._model(record['model_id'])
        return self._round_view(model, record, [self._evaluation_view(e) for e in self._evaluations(model)])

    def diagnose(self, p):
        """Write down what the baseline showed and what the training will change (route ``model-hub/rounds/diagnose``)."""
        with self.lock:
            record = self._round(p.get('round_id'))
            if record['status'] != 'diagnose':
                raise CodedError('先有基线评测的结果，才能写短板和做法', 'hub_round_state')
            model = self._model(record['model_id'])
            chosen = p.get('training_set_ids') or []
            if not isinstance(chosen, list) or len(chosen) > 20:
                raise ValueError('训练集选得太多了')
            return self._changed({**record, 'weaknesses': required_text(p.get('weaknesses'), '短板', 2000), 'plan': required_text(p.get('plan'), '改进做法', 2000),
                                  'training_set_ids': [self._set(key, TRAINING, model['category'])['id'] for key in chosen], 'status': 'training'})

    def close_round(self, p):
        """Accept the candidate as the model's current version, or end the round without one (route ``model-hub/rounds/close``)."""
        if p.get('decision') not in ('accept', 'reject'):
            raise ValueError('请选择采纳还是不采纳')
        with self.lock:
            record = self._round(p.get('round_id'))
            if record['status'] not in OPEN or (p['decision'] == 'accept' and record['status'] != 'review'):
                raise CodedError('候选版本有了评测结果才能采纳；已经结束的一轮不能再改', 'hub_round_state')
            closed = self._changed({**record, 'status': ACCEPTED if p['decision'] == 'accept' else REJECTED,
                                    'note': _text(p.get('note'), '说明', 1000), 'closed_at': self._now()})
        self.platform.audit('hub.round_closed', {'round_id': record['id'], 'decision': p['decision']})
        return closed

    def close(self):
        self.runner.close()
