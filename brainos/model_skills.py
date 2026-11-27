"""Issue #68: an application is made for a task and a brand and model, and that model's skills are ready.

The owner's words: 「点击创建，需要绑定给对应任务和机器人的品牌型号，然后编排技能。技能默认不用安装，
已经是安装好的状态（对应品牌型号）。」 Decided with the owner (2026-11-27):

* **Every skill of the brand and model can be chosen.** The skills on its connected robots, and the
  skills that come with the app and fit it, are kept as skill records when the form asks
  (:meth:`ModelSkills.ready`). Nobody installs a skill or adds it to anything first.
* **The task is required.** The application is created and bound to the task in one request
  (:meth:`ModelSkills.create`); a task that is for another brand and model refuses it as a whole.
* **What the robot lacks is installed when it is needed.** Before an on-site test, a skill of the
  application that the robot does not have, and that comes with the app, is installed on it
  (:meth:`ModelSkills.install_missing`). A skill the robot has in any version is left alone.

A skill record is what it was before (``Platform.add_skill``): nothing is added to it, so digests of
skills and applications are computed as before. Which package a record came from is found again by
the skill's id and version when it is needed.

The three functions below the class reach the robots and the skill library; the tests replace them.
"""
import secrets

from . import robot_caps, robot_model, robot_scope, task_apps
from .errors import CodedError

robot_kind = task_apps.robot_kind
DONE = '技能运行结束，设备报告成功'
STOPS = '设备报告失败、急停或超过时限时停止并报告'
NO_TASK = '创建应用要先选一个任务：应用是为任务准备的'


def _fits(skill, robot):
    """Whether a skill record can be used in an application for ``robot`` (web/robot_model.js rmSkillFits)."""
    product = robot_model.of(skill)
    return product is None or robot_model.key(product) == robot_model.key(robot_model.of(robot))


class ModelSkills:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    # ------------------------------------------------------------------ the skills of a brand and model
    def _robot(self, key):
        robot = self.store.get('robots', key)
        if not robot_scope.is_home(robot.get('project_id')):
            raise ValueError('该机器人不属于这个账号')
        return robot

    def _peers(self, robot):
        """The connected robots of ``robot``'s brand and model (itself among them when connected), oldest first."""
        kind = robot_kind(robot)
        peers = [r for r in self.store.list('robots') if robot_scope.is_home(r.get('project_id'))
                 and robot_kind(r) == kind and r.get('status') == 'connected' and r.get('endpoint')]
        return sorted(peers, key=lambda r: (r.get('created_at', ''), r['id']))

    def _records(self, robot):
        mine = [s for s in self.store.list('skills') if robot_scope.is_home(s.get('project_id')) and _fits(s, robot)]
        return sorted(mine, key=lambda s: (s.get('created_at', ''), s['id']))

    def ready(self, p):
        """Make every skill of the brand and model of ``robot_id`` a skill record, and return them.

        Read from the robots, nothing is sent that changes one. A robot that cannot be read is named
        in ``unread``; without a connected robot only the records there already are returned."""
        robot = self._robot(p.get('robot_id'))
        peers, found, unread, skipped = self._peers(robot), {}, [], []
        if robot_model.of(robot) is None:
            # A record made for a robot with no brand and model would belong to none and be offered to
            # every other robot. Such a robot keeps what it has until its brand and model is filled in.
            return {'skills': self._records(robot), 'added': 0, 'connected': len(peers), 'unread': [], 'skipped': [],
                    'product': None}
        for peer in peers:
            items = installed_on(peer)
            if items is None:
                unread.append(peer['name'])
                continue
            for item in items:
                found.setdefault((item['id'], item['version']), item)
        for item in (library_for(peers[0]) if peers else []):
            found.setdefault((item['id'], item['version']), item)
        added = 0
        with self.store.atomic():
            have = {(s['entrypoint'], s['version']) for s in self._records(robot)}
            for (key, version), item in found.items():
                if (key, version) in have or not key or not version:
                    continue
                try:
                    self.platform.add_skill({
                        'project_id': robot_scope.HOME, 'robot_id': robot['id'], 'name': item.get('title') or key, 'version': version,
                        'entrypoint': key, 'requires': list(item.get('requires') or []), 'model_ids': [],
                        'parameters': item.get('parameters') or None, 'success_condition': DONE, 'stop_condition': STOPS})
                except ValueError:
                    skipped.append(str(item.get('title') or key)[:100])     # what a device says about one skill does not stop the others
                    continue
                added += 1
            skills = self._records(robot)
        return {'skills': skills, 'added': added, 'connected': len(peers), 'unread': unread, 'skipped': skipped,
                'product': robot_model.of(robot)}

    # ------------------------------------------------------------------ an application is made for a task
    def create(self, p):
        """Create the application and bind it to the task ``task_id``; all of it, or nothing."""
        key = p.get('task_id')
        if not isinstance(key, str) or not key:
            raise CodedError(NO_TASK, 'app_task_required')
        apps = self.platform.task_apps
        with self.store.atomic():
            task = apps._task(key)
            robot = self._robot(p.get('robot_id'))
            wanted, label = apps._for(task)
            if wanted and wanted != robot_kind(robot):
                raise CodedError(f"这个任务用的是「{label}」的应用，不能为它创建「{apps._robot_label(robot)}」的应用；"
                                 '请换一个任务，或选这个任务的品牌型号', 'task_other_model')
            application = self.platform.add_application({**p, 'project_id': robot_scope.HOME})
            try:
                apps.bind({'project_id': task['id'], 'application_id': application['id']})
            except Exception:
                self.store.delete('applications', application['id'])       # all of it, or nothing
                raise
            return application

    # ------------------------------------------------------------------ what the robot lacks
    def install_missing(self, application, robot_id=None):
        """In an on-site test, once the application has passed its check: install the skills of ``application`` that the robot does not have
        and that come with the app. Returns the names of what was installed.

        Nothing is done when the robot cannot be found or read: the check that follows says why."""
        try:
            robot = self.platform.robot_models.target(application, robot_id)
        except ValueError:
            return []
        if robot.get('status') != 'connected' or not robot.get('endpoint'):
            return []
        items = installed_on(robot)
        if items is None:
            return []
        have = {item['id'] for item in items}
        wanted = [s.get('entrypoint') for s in application.get('steps', []) if isinstance(s, dict)]
        lacking = [key for key in dict.fromkeys(wanted) if key and key not in have]
        if not lacking:
            return []
        library = {item['id']: item for item in library_for(robot)}
        done = []
        for key in lacking:
            item = library.get(key)
            if not item:
                continue                            # not ours to install: the device says what it lacks
            name = item.get('title') or key
            outcome = install_example(self.platform, robot, item)
            if outcome.get('status') != 'succeeded' and key in {x['id'] for x in installed_on(robot) or []}:
                outcome = {'status': 'succeeded'}       # the answer was lost, the skill is there
            if outcome.get('status') != 'succeeded':
                raise CodedError(f"技能「{name}」还没有装到「{robot['name']}」上，自动安装没有成功："
                                 f"{outcome.get('error') or '设备没有确认安装'}", 'skill_auto_install_failed')
            done.append(name)
        if done:
            self.platform.audit('skill.auto_installed', {'robot_id': robot['id'], 'application_id': application['id'],
                                                         'skills': done}, application.get('project_id'))
        return done


# ---------------------------------------------------------------------- reaching the robots and the library

def _plain(item):
    """A skill as this module handles it, from a robot's list or a package's view."""
    return {'id': str(item.get('id') or ''), 'version': str(item.get('version') or ''), 'title': str(item.get('title') or ''),
            'requires': [r['id'] for r in item.get('requires') or [] if isinstance(r, dict) and isinstance(r.get('id'), str)],
            'parameters': item.get('parameters') or []}


def installed_on(robot):
    """The skills installed on a connected robot, or None when it cannot be read."""
    result = robot_caps.installed_skills(robot)
    if not result.get('available'):
        return None
    return [_plain(item) for item in result['items']]


def library_for(robot):
    """The skills that come with the app and can run on ``robot``, each with the package it is in."""
    from . import runtime_bridge, skill_packages
    root = runtime_bridge.examples_dir()
    if root is None:
        return []
    try:
        manifest = robot_caps.fetch_manifest_v2(robot)
    except ValueError:
        manifest = robot.get('manifest_v2')
    if not isinstance(manifest, dict):
        return []
    items = []
    for folder in sorted(root.iterdir()):
        if not (folder / 'skill.json').is_file():
            continue
        try:
            package, _ = skill_packages.read_folder(str(folder), bundled=True)
            skill = skill_packages.validate(package)
        except ValueError:
            continue
        if not skill_packages.explain(skill, manifest)['compatible']:
            continue
        items.append({**_plain(skill_packages._skill_view(skill)), 'example': folder.name,
                      'package_digest': skill_packages.package_digest(package)})
    return items


def install_example(platform, robot, item):
    """Install the package ``item`` names on ``robot``; the operation record, as kept."""
    from . import skill_packages
    try:
        return skill_packages.install(platform, {'robot_id': robot['id'], 'project_id': robot.get('project_id'),
                                                 'example': item['example'], 'package_digest': item['package_digest'],
                                                 'request_id': 'auto-' + secrets.token_hex(12)})
    except ValueError as error:
        return {'status': 'refused', 'error': str(error)}
