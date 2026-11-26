"""A task binds robot applications; it does not make them (issue #50).

The owner's rule: 任务工作台 binds an application, or sends the person to the application page to make
one; nothing is developed inside a task. Applications are bound to a brand and model of robot and
are not interchangeable between models (issue #48). What was decided with the owner (2026-11-26):

* **An application belongs to the account**, as a robot does (issue #42): it is made and changed on
  「机器人应用」 and every task sees it. With it go the skills it is put together from and what was
  made with it: test reports, releases and their operation records.
* **A task binds applications, many to many.** A binding is a record of its own (:data:`KIND`);
  unbinding removes it and nothing else. Deleting a task takes its bindings and leaves the applications.
* **A task is for one brand and model.** The first application bound to it says which; after that
  only applications of the same one can be bound. A task with no application is free again.

How it is kept:

* Applications and what goes with them are kept at the account's home (``robot_scope.HOME``, where
  the robots are), and requests about them are answered for the home whichever task is open
  (the routes are listed in brainos/robot_scope.py, next to those of the robots).
* A binding names the task and the *first* version of the application; every later version is the
  same application, and the newest one is the one a task uses (:meth:`TaskApps.applications`).
* The brand and model of a task is not saved: it is that of its oldest binding (:meth:`TaskApps.product`),
  so it follows when an application gets its brand and model later (issue #48, a robot from before).
  An application without one is in a class of the robot it was made for, as there.
* **Data from before** is moved once when a workspace is opened (:func:`migrate`): every application
  goes to the home and is bound to the task it was in. An application's digest covers its
  ``project_id`` and a device checks the digest of what it is sent, so a moved application remembers
  the task it was made in (:data:`MADE_IN`) and is sent as it was made (:func:`as_made`): its digest
  and the evidence kept for it stay valid.

Not moved: models. A skill of the account may name a model that was chosen in a task; when that
task is deleted such a model is kept for the account instead of deleted (:meth:`TaskApps.keep_models`).
"""
import json

from . import robot_model, robot_scope
from .errors import CodedError

KIND = 'task_applications'
MADE_IN = 'made_in_project_id'
# What moves to the account with the applications. Operation records: those of an application or a release.
WITH_APPLICATION = ('evaluations', 'deployments')
MISSING = '找不到这个应用，它可能已经不在了；请刷新页面'
NO_TASK = '找不到这个任务，它可能已经被删除；请刷新页面'


def root_of(application):
    """The id every version of an application shares: that of its first version."""
    return application.get('root_application_id') or application['id']


def kind_of(application):
    """What two applications are compared by when a task takes one: the brand and model, or, without
    one, the robot it was made for (issue #48: such a robot is in a class of its own)."""
    product = robot_model.of(application)
    return ('product', robot_model.key(product)) if product else ('robot', application.get('robot_id'))


def as_made(application):
    """The application with the ``project_id`` its digest was computed with (see the module's text)."""
    if not isinstance(application, dict) or MADE_IN not in application:
        return application
    return {**{k: v for k, v in application.items() if k != MADE_IN}, 'project_id': application[MADE_IN]}


def _update(store, kind, record):
    """Save a changed record where it is: the lists keep their order (``Store.put`` moves a record to the front)."""
    store.db.execute('UPDATE objects SET payload=? WHERE kind=? AND id=?',
                     (json.dumps(record, ensure_ascii=False, allow_nan=False), kind, record['id']))


def _binding(project_id, application):
    root = root_of(application)
    return {'id': f'taskapp-{project_id}-{root}', 'project_id': project_id, 'application_id': root}


def migrate(store):
    """Move every application, and what goes with it, to the account, and bind each to the task it
    was in. Returns how many records moved per kind (bindings made under :data:`KIND`).

    Does nothing once it has been done. All or nothing: one transaction, written in place."""
    home = robot_scope.HOME
    with store.lock:
        robot_scope.ensure_home(store)
        away = lambda record: isinstance(record, dict) and isinstance(record.get('id'), str) and record.get('project_id') != home
        applications = [a for a in store.list('applications') if away(a)]
        releases = {d['id'] for d in store.list('deployments') if isinstance(d, dict)}
        changes = [('applications', {**a, MADE_IN: a.get(MADE_IN, a.get('project_id'))}) for a in applications]
        changes += [('skills', s) for s in store.list('skills') if away(s)]
        changes += [(kind, r) for kind in WITH_APPLICATION for r in store.list(kind) if away(r) and r.get('application_id')]
        changes += [('operations', o) for o in store.list('operations')
                    if away(o) and (o.get('application_id') or o.get('release_id') in releases)]
        if not changes:
            return {}
        tasks = {p['id'] for p in robot_scope.tasks(store.list('projects'))}
        bound = {b['id'] for b in store.list(KIND)}
        bindings = {}
        for application in reversed(applications):          # oldest first: the first version of each says when
            binding = _binding(application.get('project_id'), application)
            if application.get('project_id') in tasks and binding['id'] not in bound:
                bindings.setdefault(binding['id'], {**binding, 'created_at': application.get('created_at', '')})
        moved = {}
        try:
            for kind, record in changes:
                _update(store, kind, {**record, 'project_id': home})
                moved[kind] = moved.get(kind, 0) + 1
            for binding in bindings.values():
                store.db.execute('INSERT INTO objects VALUES (?,?,?)', (KIND, binding['id'], json.dumps(binding, ensure_ascii=False)))
                moved[KIND] = moved.get(KIND, 0) + 1
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise
        return moved


class TaskApps:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        try:
            migrate(self.store)
        except Exception as error:                 # nothing was moved: the workspace opens as it was, and the log says so
            platform.audit('task_apps.migration_failed', {'error': str(error)[:500]})

    def snapshot(self):
        return {KIND: self.store.list(KIND)}

    # ------------------------------------------------------------------ what a task has
    def _task(self, key):
        if not isinstance(key, str) or robot_scope.is_home(key):
            raise ValueError(NO_TASK)
        try:
            return self.platform.project(key)
        except ValueError:
            raise ValueError(NO_TASK) from None

    def _application(self, key):
        try:
            if not isinstance(key, str):
                raise ValueError(MISSING)
            return self.store.get('applications', key)
        except ValueError:
            raise CodedError(MISSING, 'task_app_missing') from None

    def bindings(self, project_id):
        """The bindings of a task, oldest first."""
        mine = [b for b in self.store.list(KIND) if b.get('project_id') == project_id]
        return sorted(mine, key=lambda b: b.get('created_at', ''))

    def _newest(self, root, applications=None):
        versions = [a for a in (self.store.list('applications') if applications is None else applications) if root_of(a) == root]
        return max(versions, key=lambda a: a.get('created_at', ''), default=None)

    def applications(self, project_id):
        """The applications a task has bound, each as its newest version, in the order they were bound."""
        every = self.store.list('applications')
        found = (self._newest(b['application_id'], every) for b in self.bindings(project_id))
        return [a for a in found if a]

    def product(self, project_id):
        """The brand and model the task is for: that of its oldest binding. None while it has no
        application, or when that application has no brand and model."""
        first = next(iter(self.applications(project_id)), None)
        return robot_model.of(first) if first else None

    # ------------------------------------------------------------------ binding
    def bind(self, p):
        """Bind the application ``application_id`` (any version of it) to the task ``project_id``."""
        with self.store.atomic():
            task = self._task(p.get('project_id'))
            application = self._application(p.get('application_id'))
            binding = _binding(task['id'], application)
            saved = next((b for b in self.store.list(KIND) if b['id'] == binding['id']), None)
            if saved:
                return saved
            first = next(iter(self.applications(task['id'])), None)
            newest = self._newest(binding['application_id']) or application
            if first and kind_of(first) != kind_of(newest):
                raise CodedError(f"这个任务用的是「{self._label(first)}」的应用，只能再绑定同一品牌型号的应用；"
                                 f"「{newest['name']}」属于「{self._label(newest)}」", 'task_other_model')
            from .service import now
            binding = self.store.put(KIND, {**binding, 'created_at': now()})
            self.platform.audit('task.application_bound', {'application_id': binding['application_id'], 'name': newest['name']}, task['id'])
            return binding

    def unbind(self, p):
        """Take the application off the task. The application, its tests and releases stay."""
        with self.store.atomic():
            task = self._task(p.get('project_id'))
            key = p.get('application_id')
            try:
                key = root_of(self.store.get('applications', key)) if isinstance(key, str) else key
            except ValueError:
                pass                                # the application is gone: its binding can still be removed
            binding = next((b for b in self.store.list(KIND) if b['project_id'] == task['id'] and b['application_id'] == key), None)
            if binding:
                self.store.delete(KIND, binding['id'])
                self.platform.audit('task.application_unbound', {'application_id': key}, task['id'])
            return {'project_id': task['id'], 'application_id': key, 'unbound': bool(binding)}

    def _label(self, application):
        product = robot_model.of(application)
        if product:
            return robot_model.label(product)
        try:
            return f"{self.store.get('robots', application.get('robot_id'))['name']}（未填写品牌型号）"
        except ValueError:
            return robot_model.label(None)

    # ------------------------------------------------------------------ when a task is deleted
    def keep_models(self, project_id):
        """Before the task ``project_id`` is deleted: the models in it that a skill of the account
        names are kept for the account. Returns their ids."""
        used = {model_id for skill in self.store.list('skills') if skill.get('project_id') == robot_scope.HOME
                for model_id in skill.get('model_ids') or []}
        kept = []
        with self.store.atomic():
            for model in self.store.list('models'):
                if model.get('project_id') == project_id and model.get('id') in used:
                    _update(self.store, 'models', {**model, 'project_id': robot_scope.HOME})
                    kept.append(model['id'])
            self.store.db.commit()
        return kept
