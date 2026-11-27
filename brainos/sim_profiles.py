"""The built-in profiles of robots that can be simulated (issue #54, docs/SIM_PROFILES.md).

A profile is what the app knows about one brand and model before any device is connected: its type,
its picture, where its robot model (URDF) comes from and the joint angles it stands in. 「添加仿真机器人」
offers type → brand → model from this list, and a simulated robot of a profile is a MuJoCo robot built
from that profile's model package on an empty floor (docs/MUJOCO_SIM.md describes that simulation).

The model packages are not in this repository: ``scripts/robot_models.py`` builds one ``<id>.zip`` per
profile (public ones from their GitHub repository at the pinned commit, the two that are not public
from a folder on the build computer) and the app is packaged with them. A profile whose package is not
there is listed as not available instead of being offered.

The two simulated robots the app always had (a quadruped and an arm with made-up bodies, no model
file, no MuJoCo) stay as the brand 「NeuxMind 示例」.
"""
import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import sys
import threading
from pathlib import Path

from . import mujoco_model, mujoco_scene, robot_scope
from .contracts import required_text
from .errors import CodedError

MODELS_ENV = 'NEUXMIND_ROBOT_MODELS_DIR'
PACKAGED_DIR = 'robot_models'
SIM_KIND = 'mujoco'
SETTING = 'sim_profiles'                 # settings record: {'old_sims_removed': time}
START_TIMEOUT_S = 240

# The types 「添加仿真机器人」 offers, in the order of the issue. `family` is the standard body family a
# simulated robot of that type reports; a wheel-legged dog moves like a quadruped in this simulation.
TYPES = (
    {'id': 'manipulator', 'name': '机械臂', 'family': 'manipulator'},
    {'id': 'wheel_legged', 'name': '轮足机器狗', 'family': 'quadruped'},
    {'id': 'quadruped', 'name': '四足机械狗', 'family': 'quadruped'},
    {'id': 'biped', 'name': '双足机器人', 'family': 'humanoid'},
    {'id': 'other', 'name': '其他', 'family': 'custom'},
)
TYPE_BY_ID = {t['id']: t for t in TYPES}

BSD3 = 'BSD-3-Clause'
_UNITREE = {'repo': 'unitreerobotics/unitree_ros', 'commit': '157ff950351e5f535e03c5d85a16b7d594b3dbcc',
            'licence': 'LICENSE', 'licence_name': BSD3}
_DEEP = {'repo': 'DeepRoboticsLab/deep_robotics_model', 'commit': 'd1a3900e7faf7eda9e28f20715b42e871dbfd295',
         'licence': 'LICENSE.txt', 'licence_name': BSD3}
_AGIBOT_A3 = {'repo': 'AgibotTech/A3-A3U-robot-model', 'commit': '589f508ff357447c610a3f3004419035ddc8f153',
              'licence': 'LICENSE.txt', 'licence_name': 'MulanPSL-2.0'}

# `pose`: joint name patterns (fnmatch, first match wins) → the angle the robot stands in. A dog's URDF
# zero is legs straight, often outside the knee's own limits. `stand_in_shapes`: see scripts/robot_models.py.
PROFILES = (
    {'id': 'unitree-go2', 'vendor': '宇树科技', 'model': 'Go2', 'type': 'quadruped', 'image': 'unitree-go2',
     'pose': {'F?_thigh_joint': 0.8, 'R?_thigh_joint': 1.0, '*_calf_joint': -1.5, '?L_hip_joint': 0.1, '?R_hip_joint': -0.1},
     'source': {**_UNITREE, 'urdf': 'robots/go2_description/urdf/go2_description.urdf',
                'paths': ['robots/go2_description/urdf/go2_description.urdf', 'robots/go2_description/dae/*']}},
    {'id': 'unitree-g1', 'vendor': '宇树科技', 'model': 'G1', 'type': 'biped', 'image': 'unitree-g1',
     'pose': {'*_hip_pitch_joint': -0.1, '*_knee_joint': 0.3, '*_ankle_pitch_joint': -0.2},
     'source': {**_UNITREE, 'urdf': 'robots/g1_description/g1_23dof.urdf',
                'paths': ['robots/g1_description/g1_23dof.urdf', 'robots/g1_description/meshes/*']}},
    {'id': 'unitree-z1', 'vendor': '宇树科技', 'model': 'Z1', 'type': 'manipulator', 'image': 'unitree-z1',
     'source': {**_UNITREE, 'urdf': 'robots/z1_description/xacro/z1.urdf',
                'paths': ['robots/z1_description/xacro/z1.urdf', 'robots/z1_description/meshes/visual/*']}},
    {'id': 'deeprobotics-lite3', 'vendor': '云深处科技', 'model': '绝影 Lite3', 'type': 'quadruped', 'image': 'deeprobotics-lite3',
     'pose': {'*_HipY_joint': -0.65, '*_Knee_joint': 1.3},
     'source': {**_DEEP, 'urdf': 'Lite3/urdf/Lite3.urdf', 'paths': ['Lite3/urdf/*']}},
    {'id': 'deeprobotics-m20', 'vendor': '云深处科技', 'model': '山猫 M20', 'type': 'wheel_legged', 'image': 'deeprobotics-m20',
     'pose': {'f?_hipy_joint': -0.3, 'h?_hipy_joint': 0.3, 'f?_knee_joint': 0.6, 'h?_knee_joint': -0.6},
     'source': {**_DEEP, 'urdf': 'M20/urdf/M20.urdf', 'paths': ['M20/urdf/*']}},
    {'id': 'agibot-a3', 'vendor': '智元机器人', 'model': 'A3', 'type': 'biped', 'image': 'agibot-a3',
     'source': {**_AGIBOT_A3, 'urdf': 'a3_t2d5/urdf/model.urdf', 'paths': ['a3_t2d5/urdf/model.urdf', 'a3_t2d5/meshes/*']}},
    {'id': 'zsibot-l2u', 'vendor': '智身科技', 'model': '钢镚 L2-U', 'type': 'wheel_legged', 'image': 'zsibot-l2u',
     'source': {'private': 'zsibot-l2u', 'urdf': 'urdf/xg2_wheel_ULTRA.urdf', 'licence_name': '厂家文件，不公开'}},
    {'id': 'xingji-cs1', 'vendor': '星玑科技', 'model': 'CS1', 'type': 'quadruped', 'image': 'xingji-cs1',
     'pose': {'*_thigh_joint': 0.35},          # estimated from the URDF: the feet come under the hips
     'source': {'private': 'xingji-cs1', 'urdf': 'robot_description.urdf', 'stand_in_shapes': True,
                'licence_name': '厂家文件，不公开'}},
    # Issue #63: the duck is not built from a URDF. Its model, skill models and physics engine are a kit of
    # their own (brainos/duck_kit.py); `duck` sends 「添加仿真机器人」 there. Issue #65: it is listed under 「其他」
    # (a body of that type has a stop and its health, which is what the duck's body offers), not with the humanoids.
    {'id': 'microduck-adp102', 'vendor': 'Microduck', 'model': 'AD-P102', 'type': 'other', 'image': 'microduck-adp102', 'duck': True,
     'source': {'licence_name': '厂家文件与署名非商用模型，不公开'}},
    {'id': 'neuxmind-quadruped', 'vendor': 'NeuxMind 示例', 'model': '通用四足', 'type': 'quadruped', 'image': '', 'builtin': 'quadruped'},
    {'id': 'neuxmind-arm', 'vendor': 'NeuxMind 示例', 'model': '通用六轴机械臂', 'type': 'manipulator', 'image': '', 'builtin': 'arm'},
)
BY_ID = {p['id']: p for p in PROFILES}

# Other spellings of a brand or a model, as a device or an earlier version wrote them. Two products that
# differ only in these are the same brand and model (robot_model.key).
VENDOR_ALIASES = {
    '宇树科技': ('宇树', 'unitree', 'unitree robotics'),
    '云深处科技': ('云深处', 'deep robotics', 'deeprobotics'),
    '智元机器人': ('智元', 'agibot'),
    '智身科技': ('智身', 'zs', 'zsibot', 'genisom', 'genisom ai'),
    '星玑科技': ('星玑', 'xingji', 'xinji'),
    'Microduck': ('小鸭',),
}
MODEL_ALIASES = {
    ('智身科技', '钢镚 L2-U'): ('钢镚 L2U', 'L2U', 'L2-U', 'L2 Ultra', 'L2W Ultra'),
    ('星玑科技', 'CS1'): ('CS-DOG1', 'csdog1'),
    ('Microduck', 'AD-P102'): ('ADP102', 'AD-P102 二版'),
    ('云深处科技', '绝影 Lite3'): ('Lite3',),
    ('云深处科技', '山猫 M20'): ('M20',),
}


def _fold(text):
    return ''.join(ch for ch in str(text).casefold() if ch not in ' \t-_')


_VENDORS = {_fold(alias): vendor for vendor, aliases in VENDOR_ALIASES.items() for alias in (vendor, *aliases)}
_MODELS = {(vendor, _fold(alias)): model for (vendor, model), aliases in MODEL_ALIASES.items() for alias in (model, *aliases)}


def canonical(vendor, model):
    """``(vendor, model)`` in the spelling of the profiles when they are a known brand (and model); else unchanged."""
    known = _VENDORS.get(_fold(vendor))
    if known is None:
        return vendor, model
    return known, _MODELS.get((known, _fold(model)), model)


def aliases():
    """The same table for the pages (web/sim_profiles.js `spCanon`): folded spelling → the profiles' spelling."""
    models = {}
    for (vendor, folded), model in _MODELS.items():
        models.setdefault(vendor, {})[folded] = model
    return {'vendors': dict(_VENDORS), 'models': models}


def pose_of(profile, joint_names):
    """``{joint: angle}`` for the joints of ``joint_names`` that the profile's standing pose names."""
    out = {}
    for name in joint_names:
        for pattern, angle in (profile.get('pose') or {}).items():
            if fnmatch.fnmatchcase(name, pattern):
                out[name] = angle
                break
    return out


# ---------------------------------------------------------------- where the model packages are
def models_dir():
    """The folder with ``<profile id>.zip``: named by the environment, inside the packaged app, or the build folder."""
    named = os.environ.get(MODELS_ENV)
    if named:
        return Path(named)
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / PACKAGED_DIR
    return Path(__file__).resolve().parents[1] / '.build' / 'robot-models'


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


class SimProfiles:
    """The profiles as the console uses them: what can be added, adding one, and the one-time removal of
    the simulated robots from before the profiles."""

    def __init__(self, platform, profiles=PROFILES, models=None):
        self.platform, self.store = platform, platform.store
        self.profiles = tuple(profiles)
        self.models = Path(models) if models else None
        self.prepared_dir = Path(self.store.root) / 'robot-models'      # shared with mujoco_sim: one unpacked copy per content
        # One table of locks with mujoco_sim: both unpack into the same folder, by content.
        self.lock, self.model_locks = platform.mujoco.lock, platform.mujoco.model_locks

    # ------------------------------------------------------------ what can be added
    def _package(self, profile):
        path = (self.models or models_dir()) / (profile['id'] + '.zip')
        return path if path.is_file() else None

    @staticmethod
    def _engine():
        from . import mujoco_sim
        return mujoco_sim.engine_available()

    def _problem(self, profile, engine):
        """Why this profile cannot be added on this computer ('' when it can)."""
        if profile.get('builtin'):
            return ''
        if profile.get('duck'):
            return self.platform.duck.problem()
        if not engine:
            return '这个安装里没有 MuJoCo 仿真引擎'
        if self._package(profile) is None:
            return '这个安装包里没有它的机器人模型'
        return ''

    def catalog(self):
        """Types and profiles for 「添加仿真机器人」 (part of the state the page reads)."""
        engine = self._engine()
        rows = []
        for profile in self.profiles:
            problem = self._problem(profile, engine)
            rows.append({'id': profile['id'], 'vendor': profile['vendor'], 'model': profile['model'], 'type': profile['type'],
                         'image': profile.get('image') or '', 'builtin': bool(profile.get('builtin')),
                         'kit': bool(profile.get('duck')),          # issue #65: simulated from a kit of its own, not from a URDF
                         'licence': (profile.get('source') or {}).get('licence_name', ''),
                         'available': not problem, 'problem': problem})
        return {'types': [{'id': t['id'], 'name': t['name']} for t in TYPES], 'profiles': rows}

    def _profile(self, profile_id):
        for profile in self.profiles:
            if profile['id'] == profile_id:
                return profile
        raise CodedError('请选择要添加的类型、品牌和型号', 'sim_profile_unknown')

    # ------------------------------------------------------------ adding one
    def _model_dir(self, profile):
        """The unpacked model of the profile's package (unpacked once per content)."""
        package = self._package(profile)
        sha256 = _sha256(package)
        target = self.prepared_dir / sha256
        with self.lock:
            one = self.model_locks.setdefault(sha256, threading.Lock())
        with one:
            meta = mujoco_model.load(target)
            if meta is None:
                try:
                    self.prepared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    meta = mujoco_model.prepare(package, 'zip', target)
                except (mujoco_model.ModelError, OSError) as exc:
                    raise CodedError(f'「{profile["vendor"]} {profile["model"]}」的机器人模型不能用于仿真：{exc}', 'sim_model_invalid') from None
        return target, meta

    def _task(self, p):
        """``(task, owner)``: the task the trial goes on in, and where the robot is kept (as ``SimRobots.add``)."""
        from .sim_robots import DEFAULT_PROJECT
        at_home = robot_scope.is_home(p.get('project_id'))
        task_id = p.get(robot_scope.TASK_FIELD) if at_home else p.get('project_id')
        if task_id:
            project = self.platform.project(task_id)
        else:
            existing = robot_scope.tasks(self.store.list('projects'))
            if existing:
                project = existing[0]
            elif at_home:
                return None, robot_scope.HOME       # issue #56: a robot needs no task, and none is made for it
            else:
                project = self.platform.add_project(dict(DEFAULT_PROJECT))
                project['auto_created'] = True
                self.store.put('projects', project)
        return project, (robot_scope.HOME if at_home else project['id'])

    def add(self, p):
        """Add and start a simulated robot of the profile ``profile_id`` (route ``sims``)."""
        from .service import now, uid
        profile = self._profile(p.get('profile_id'))
        if profile.get('builtin'):
            return self.platform.sims.add({**p, 'kind': profile['builtin']})
        if profile.get('duck'):
            return self.platform.duck.add(self, profile, p)
        problem = self._problem(profile, self._engine())
        if problem:
            raise CodedError(f'「{profile["vendor"]} {profile["model"]}」现在不能添加：{problem}', 'sim_profile_unavailable')
        family = TYPE_BY_ID[profile['type']]['family']
        model_dir, meta = self._model_dir(profile)
        project, owner = self._task(p)
        taken = {r['name'] for r in self.store.list('robots') if r.get('project_id') == owner}
        name = required_text(p.get('name') or f'{profile["vendor"]} {profile["model"]} 仿真', '机器人名称', 100)
        base, n = name, 2
        while name in taken:
            name, n = f'{base} {n}', n + 1
        sims = self.platform.sims
        # Issue #58: the robot is of the profile's type (a type of the body type list); the simulation keeps `family`.
        robot = {'id': uid('robot'), 'project_id': owner, 'name': name, 'family': profile['type'], 'endpoint': '',
                 'token': secrets.token_urlsafe(32), 'status': 'unconnected', 'capabilities': [],
                 'sim': {'kind': SIM_KIND, 'adapter': 'brainos.mujoco_body:body', 'enabled': True, 'state': 'starting',
                         'profile_id': profile['id'], 'start_timeout_s': START_TIMEOUT_S},
                 'created_at': now()}
        state_dir = sims.root / robot['id']
        state_dir.mkdir(parents=True, exist_ok=True)
        config = {'model_dir': str(model_dir), 'scene': mujoco_scene.validate(mujoco_scene.EMPTY), 'family': family,
                  'vendor': profile['vendor'], 'model': profile['model'], 'name': name,
                  'pose': pose_of(profile, [j['name'] for j in meta.get('joints', [])]),
                  'instance_id': 'mujoco-' + robot['id'][-12:], 'view_dir': str(state_dir / 'view')}
        (state_dir / 'mujoco.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        self.store.put('robots', robot)
        self.platform.audit('robot.sim_added', {'robot_id': robot['id'], 'kind': SIM_KIND, 'profile_id': profile['id']}, owner)
        return {**sims.start(robot['id']), 'project_id': project['id'] if project else ''}

    def align_types(self):
        """Issue #58: simulated robots added before the body type list had the profile types were saved with
        the standard value (a wheel-legged dog as a quadruped). Give each the type of its profile; returns
        the ids that changed. Robots that are not simulated, or not from a profile, are not touched."""
        changed, known = [], {p['id']: p for p in self.profiles}
        for robot in self.store.list('robots'):
            profile = known.get(robot['sim'].get('profile_id')) if isinstance(robot.get('sim'), dict) else None
            if profile and not profile.get('builtin') and robot.get('family') != profile['type']:
                self.store.put('robots', {**robot, 'family': profile['type']}, owner=True)
                changed.append(robot['id'])
        return changed

    # ------------------------------------------------------------ the simulated robots from before
    def remove_old_sims(self):
        """Once per workspace: delete the simulated robots that were added before the profiles, with the
        skills installed on them and their operation records. Robots that are not simulated are not touched.
        Returns the ids that were removed (``None`` when this workspace was done before)."""
        try:
            record = self.store.get('settings', SETTING)
        except ValueError:
            record = {}
        if record.get('old_sims_removed'):
            return None
        from .service import now
        old = {r['id']: r for r in self.store.list('robots') if r.get('sim') and not r['sim'].get('profile_id')}
        # Which ones go is written down before anything is deleted: a removal that was interrupted (the app was
        # closed, the computer lost power) is finished the next time, also for robots whose record is already gone.
        earlier = [i for i in record.get('removing', []) if isinstance(i, str)] if isinstance(record.get('removing'), list) else []
        removing = sorted(set(earlier) | set(old))
        self.store.put('settings', {'id': SETTING, 'removing': removing})
        gone = set(removing)
        for operation in self.store.list('operations') if gone else []:
            if operation.get('robot_id') in gone:
                self.store.delete('operations', operation['id'])
        for robot_id in removing:
            shutil.rmtree(self.platform.sims.root / robot_id, ignore_errors=True)
            if robot_id in old:
                self.store.delete('robots', robot_id)
                self.platform.audit('robot.sim_removed', {'robot_id': robot_id, 'reason': 'issue-54'}, old[robot_id].get('project_id'))
        self.store.put('settings', {'id': SETTING, 'old_sims_removed': now(), 'removed': len(removing)})
        return removing
