"""Install the management layer on a robot from the console (issue #19, R1-03).

Before this a person built the two wheels on a development machine, copied them to the robot, logged
in, ran ``runtime/deploy/device_install.sh``, opened a tunnel and typed the address and the token.
Now the robot's card has "安装管理层": once its SSH connection works, the console

1. checks the robot (read only, ``runtime_install_device.PROBE_SCRIPT``): system, Python, tools, free
   space, what is at ``~/neuxmind``, whether the port is free and the SSH service forwards TCP;
2. sends its own install package (``runtime_package``: the script and the two wheels) to
   ``~/neuxmind-pkg/console/<package id>/`` over the upload channel of issue #18 (compressed where
   that helps, resumed after a break, checked on the robot);
3. runs the script's ``install`` and then ``start`` there, each as a job detached from the SSH session,
   so a dropped connection does not break it (the console logs in again and reads its result);
4. links the management layer (issue #17's ``attach``): the token the script generated is read from
   its token file over SSH and saved, the channel goes to the robot's own 127.0.0.1:<port>, and the
   connection check runs. The robot is then "connected", its link up and its heartbeat online.

Rules it keeps:

* **No sudo, no system files, no autostart.** Everything happens in the SSH login's home directory;
  the runtime listens on the robot's loopback only. Autostart needs administrator rights and a person
  who agrees to exactly what runs (issue #20): ``STAGES`` is where that step will go.
* **An install that is already there is not touched without a person's choice** (``runtime_install_plan``):
  linking it changes nothing on the robot; deleting and reinstalling it needs an explicit confirmation.
  Something at ``~/neuxmind`` that the install script did not make is never touched.
* **The simulated body by default.** The adapter follows the robot's body type (``ADAPTERS``): a
  quadruped gets ``sim_quadruped``, a manipulator ``sim_arm``. Neither opens a device or moves anything.
  The ROS 2 adapter (issue #21) is chosen at install time too once its install script is in the package.
* **The token** is never returned, logged or put into a job's view; it lives where issue #17 saves it.
* **A failure says which step, what is left on the robot and how to clean it up**, and "清理" removes
  exactly what this install put there (``clean``).

The progress of an install changes all the time, so it is not part of the workspace state the page
redraws itself from: the page asks for it apart (``status``). Only the outcome is saved, on the robot.
"""
import logging
import threading
import time

from . import connector, device_access, device_transfer, runtime_package
from . import runtime_install_device as dev
from . import runtime_install_plan as planning
from .errors import CodedError
from .runtime_link import BAD_PORT, DEFAULT_PORT, ONLY_SSH_ROBOTS
from .runtime_link_sessions import _valid_port
from .service import now

LOG = logging.getLogger(__name__)

STAGES = (('check', '检查设备'), ('upload', '上传安装包'), ('remove', '删除原有的管理层'), ('install', '安装'),
          ('start', '启动'), ('link', '接入并检查'))
LABELS = dict(STAGES, wait_install='安装', clean='清理')
BUDGETS = {'check': 180, 'upload': 2700, 'remove': 180, 'install': 1200, 'start': 300, 'link': 300, 'clean': 180,
           'look': 45}                     # the last read-only look after a failure: what is left
POLL_SECONDS = 2
RETRY_WAITS = (2, 5, 10, 20, 30)
ADAPTERS = planning.ADAPTERS
# Issue #18's TransferError: ``resumable`` says a partial file is left to go on from, not whether trying again can help.
UPLOAD_REFUSED = ('auth', 'key', 'fingerprint', 'forbidden')     # a refused login: never repeated
UPLOAD_AGAIN = ('interrupted', 'no_sftp')     # the link broke (an SFTP request that timed out reads as no_sftp)
UPLOAD_DAMAGED = ('checksum', 'damaged')      # removed on the robot; sent once more, not again and again
PACKAGE_DIR = '~/neuxmind-pkg/console/%s'
INSTALL_DIR = '~/neuxmind'
OUTCOME = 'runtime_install'                # the field of the robot record that keeps the last outcome
ACTIVE = ('running',)
BUSY = '这台机器人正在安装管理层；请等它结束，或先停止'
INSTALLING = '这台机器人正在安装管理层；请等安装结束，或先停止安装，再删除'
BODY = '本体类型“{family}”没有对应的仿真本体，管理层装不上。请先在卡片上选择四足或机械臂'
NOTHING_TO_CLEAN = '这台机器人没有需要清理的安装记录'
CHANGED = '这台机器人的 SSH 连接在安装途中被修改或删除了；安装已停止，请重新开始'
REMOVED = '这台机器人在安装途中被删除了'


class Failure(Exception):
    def __init__(self, stage, code, message, log=()):
        super().__init__(message)
        self.stage, self.code, self.log = stage, code, list(log)


class Changed(Exception):
    pass


def _revision(connection):
    return connection.get('updated_at') or connection.get('created_at')


def _clean_text(value, limit=300):
    text = ''.join(ch for ch in str(value) if ch == '\n' or ch >= ' ')
    return text[:limit]


class Job:
    """One install (or clean-up) of one robot, on a thread of its own."""

    def __init__(self, service, robot, connection, package, port, adapter, existing=None, confirm=False,
                 mode='install', previous=None):
        self.service, self.platform = service, service.platform
        self.robot_id, self.project_id, self.connection_id = robot['id'], robot['project_id'], connection['id']
        self.revision, self.package, self.port, self.adapter = _revision(connection), package, port, adapter
        self.existing, self.confirm, self.mode, self.previous = existing, confirm, mode, previous or {}
        self.id, self.stop_event = dev.new_job_id(), threading.Event()
        self.state, self.stage, self.progress = 'running', 'clean' if mode == 'clean' else 'check', None
        self.steps = ['clean'] if mode == 'clean' else ['check', *planning.FRESH]
        self.done, self.failure, self.choice, self.notes = set(), None, None, []
        self.left, self.cleanup, self.can_clean = [], '', False
        self.created_install = bool(self.previous.get('created_install')) if mode == 'clean' else False
        self.uploaded = False
        self.started_at, self.finished_at = now(), None
        self.session = dev.DeviceSession(self._login, self.stop_event, waits=service.waits)
        self.thread = threading.Thread(target=self.run, daemon=True, name='runtime-install-' + self.robot_id)

    # ------------------------------------------------------------------ what the page sees

    def view(self):
        stages = []
        for step in self.steps:
            state = 'done' if step in self.done else 'pending'
            if step == self.stage:
                state = {'running': 'running', 'failed': 'failed', 'stopped': 'stopped'}.get(self.state, state)
            stages.append({'id': step, 'label': LABELS[step], 'state': state})
        return {'robot_id': self.robot_id, 'job_id': self.id, 'mode': self.mode, 'state': self.state,
                'stopping': self.state == 'running' and self.stop_event.is_set(),
                'stage': self.stage, 'stage_label': LABELS.get(self.stage, ''), 'stages': stages,
                'progress': dict(self.progress) if self.progress else None, 'port': self.port, 'adapter': self.adapter,
                'package': self.package.summary(), 'package_dir': PACKAGE_DIR % self.package.id,
                'choice': self.choice, 'failure': self.failure, 'notes': list(self.notes), 'left': list(self.left),
                'cleanup': self.cleanup, 'can_clean': self.can_clean, 'created_install': self.created_install,
                'started_at': self.started_at, 'finished_at': self.finished_at}

    def outcome(self, state):
        keep = ('stage', 'failure', 'left', 'cleanup', 'can_clean', 'created_install', 'port', 'adapter',
                'started_at', 'finished_at', 'notes', 'job_id', 'mode')
        view = self.view()
        result = {key: view[key] for key in keep}
        result.update(state=state, package_id=self.package.id, runtime_version=self.package.runtime_version,
                      package_files=[entry['name'] for entry in self.package.files])
        return result

    # ------------------------------------------------------------------ running

    def _login(self):
        try:
            connection, secret = self.platform.ssh_edit.saved_login(self.connection_id)
        except ValueError:
            raise Changed() from None
        if _revision(connection) != self.revision:
            raise Changed()
        return device_access.SSHDevice(connection, secret)

    def _deadline(self, stage):
        return time.monotonic() + self.service.budgets[stage]

    def _enter(self, stage):
        if self.stop_event.is_set():
            raise dev.Stopped()
        try:
            robot = self.platform.store.get('robots', self.robot_id)
        except ValueError:
            raise Failure(stage, 'install_robot_removed', REMOVED) from None
        if robot.get('connection_id') != self.connection_id:
            raise Changed()
        try:
            if _revision(self.platform.store.get('device_connections', self.connection_id)) != self.revision:
                raise Changed()                    # edited: it may lead to another machine now
        except ValueError:
            raise Changed() from None
        self.stage = stage

    def run(self):
        final = 'failed'
        try:
            final = self._clean() if self.mode == 'clean' else self._install()
        except Failure as failure:
            self._failed(failure)
        except Changed:
            self._failed(Failure(self.stage, 'install_connection_changed', CHANGED))
        except dev.Stopped:
            final = 'stopped'
            self._leftovers()
        except dev.Unreachable as exc:
            code = 'install_login_refused' if exc.refused else 'install_unreachable'
            self._failed(Failure(self.stage, code, _clean_text(exc)))
        except Exception:                          # a bug here must still end the job and say so
            LOG.exception('installing the management layer on %s failed', self.robot_id)
            self._failed(Failure(self.stage, 'install_internal', '控制台内部出错，安装没有完成'))
        finally:
            try:
                self.session.close()
                self.finished_at = now()
                self.service._ended(self, final)
            except Exception:                      # saving the outcome failed: the job still ends
                LOG.exception('saving the outcome of the install on %s failed', self.robot_id)
            finally:
                self.state = final                 # last: whoever waits for the end gets all of it, saved

    def _hidden(self):
        """What must never be shown from a robot's output: its saved logins and the token saved for it."""
        values = []
        try:
            values += device_access.secret_values(self.platform.ssh_edit.saved_login(self.connection_id)[1])
            values.append(self.platform.store.get('robots', self.robot_id).get('token') or '')
        except (ValueError, TypeError, AttributeError):
            pass
        return [value for value in values if isinstance(value, str) and len(value) >= 4]

    def _shown(self, text, limit=300):
        text = device_access.redact(text)
        for value in self._hidden():
            text = text.replace(value, '[已隐藏]')
        return _clean_text(text, limit)

    def _failed(self, failure):
        self.failure = {'stage': failure.stage, 'stage_label': LABELS.get(failure.stage, ''), 'code': failure.code,
                        'message': self._shown(failure, 1000), 'log': [self._shown(line) for line in failure.log]}
        self._leftovers()

    def _install(self):
        self._enter('check')
        deadline = self._deadline('check')
        word, probe, _ = self.session.run('probe', self.package.id, seconds=60, deadline=deadline)
        if word != 'probed':
            raise Failure('check', 'install_probe_failed', '设备没有完成检查（%s）' % (word or '没有答复'))
        port_state = self.session.port_state(self.port, deadline)
        self.service._heard(self.connection_id)
        decision = planning.plan(probe, port_state, self.package, self.port, self.existing, self.confirm)
        self.done.add('check')
        if decision.kind == 'refuse':
            raise Failure('check', decision.code, decision.message)
        if decision.kind == 'choice':
            self.steps = ['check']
            self.choice = {'options': [{'id': option, 'label': planning.CHOICES[option]} for option in decision.options],
                           'found': decision.found}
            return 'needs_choice'
        self.port, self.notes = decision.port, decision.notes
        self.steps = ['check', *decision.steps]
        self.created_install = decision.creates_install
        steps = {'upload': self._upload, 'remove': self._remove, 'install': self._install_step,
                 'wait_install': self._wait_install, 'start': self._start, 'link': self._link}
        for step in decision.steps:
            steps[step]()
            self.done.add(step)
        return 'succeeded'

    def _upload(self):
        self._enter('upload')
        deadline, total, sent, damaged = self._deadline('upload'), self.package.total_bytes, 0, 0
        for entry in self.package.files:
            self._enter('upload')
            remote = (PACKAGE_DIR % self.package.id) + '/' + entry['name']
            mode = 0o755 if entry['role'] == 'script' else 0o644

            def progress(event, before=sent, name=entry['name']):
                self.progress = {'sent': before + int(event.get('sent') or 0), 'total': total, 'file': name}
            while True:
                try:
                    sent_file = device_transfer.send_to_robot(
                        self.platform, self.connection_id, str(self.package.path(entry['role'])), remote, mode=mode,
                        on_progress=progress, stop=self.stop_event, **self.service.transfer_options)
                    self.uploaded = True
                    if sent_file.get('sha256') != entry['sha256']:    # the file changed on this computer after the check
                        raise Failure('upload', 'install_upload_failed', '控制台自带的安装包文件 %s 在发送前被改动了，'
                                      '设备上的这份不能用；请重新安装桌面应用' % entry['name'])
                    break
                except device_transfer.TransferError as exc:
                    self.uploaded = self.uploaded or exc.sent > 0 or exc.stage in ('send', 'verify')
                    if self.stop_event.is_set():
                        raise dev.Stopped() from None
                    if exc.reason in UPLOAD_REFUSED:
                        raise Failure('upload', 'install_login_refused', _clean_text(exc, 1000)) from None
                    damaged += exc.reason in UPLOAD_DAMAGED
                    again = exc.stage == 'connect' or exc.reason in UPLOAD_AGAIN or (exc.reason in UPLOAD_DAMAGED and damaged < 2)
                    if not again or time.monotonic() + 5 > deadline:
                        raise Failure('upload', 'install_upload_failed', _clean_text(exc, 1000)) from None
                    if self.stop_event.wait(self.service.upload_retry_seconds):
                        raise dev.Stopped() from None
            sent += entry['bytes']
            self.progress = {'sent': sent, 'total': total, 'file': entry['name']}

    def _remove(self):
        self._enter('remove')
        self._job('remove')

    def _install_step(self):
        self._enter('install')
        self._job('install')

    def _wait_install(self):
        self._enter('install')
        word, keys, _ = self.session.run('poll', self.package.id, 'install', seconds=30, deadline=self._deadline('install'))
        self._wait('install', dev.job_state(keys.get('job'))[0])

    def _start(self):
        self._enter('start')
        self._job('start')

    def _job(self, step):
        """Run one step of the install script on the robot, detached, and wait for its end."""
        deadline, job_id = self._deadline(step), dev.new_job_id()
        word, _, _ = self.session.run('job', self.package.id, step, job_id, self.port, self.adapter,
                                      seconds=60, deadline=deadline)
        if word == 'busy':                          # one of an earlier attempt still runs: its result counts
            _, keys, _ = self.session.run('poll', self.package.id, step, seconds=30, deadline=deadline)
            job_id = dev.job_state(keys.get('job'))[0]
        elif word not in ('started', 'already'):
            raise Failure(step, 'install_step_refused', '设备没有开始这一步（%s）' % (word or '没有答复'))
        self._wait(step, job_id, deadline)

    def _wait(self, step, job_id, deadline=None):
        deadline, gone = deadline or self._deadline(step), 0
        while True:
            _, keys, log = self.session.run('poll', self.package.id, step, seconds=30, deadline=deadline)
            current, code, alive = dev.job_state(keys.get('job'))
            if job_id is None or current != job_id:
                raise Failure(step, 'install_job_lost', '设备上这一步的记录不见了（被别的操作替换或删除）', log)
            if code == 0:
                return
            if code is not None:
                raise Failure(step, 'install_step_failed', '设备上的安装脚本在“%s”这一步失败了（退出码 %d）' % (LABELS[step], code), log)
            gone = 0 if alive else gone + 1
            if gone >= 2:                          # twice, a poll apart: not the moment between its end and its exit file
                raise Failure(step, 'install_job_lost', '设备上这一步的进程没有结束就不见了（设备可能重启过）', log)
            if time.monotonic() > deadline:
                raise Failure(step, 'install_timeout', '设备上的“%s”在 %d 秒内没有结束，可能还在进行' %
                              (LABELS[step], self.service.budgets[step]), log)
            if self.stop_event.wait(self.service.poll_seconds):
                raise dev.Stopped()

    def _link(self):
        self._enter('link')
        deadline = self._deadline('link')
        while True:
            try:
                self.platform.runtime_link.attach({'project_id': self.project_id, 'robot_id': self.robot_id, 'port': self.port})
                break
            except connector.DeviceError as exc:
                if exc.code in ('device_link_down', 'device_link_slow') and time.monotonic() + 5 < deadline:
                    if self.stop_event.wait(5):
                        raise dev.Stopped() from None
                    continue
                raise Failure('link', 'install_link_failed', '管理层装好了，但接入时出错：' + _clean_text(exc)) from None
            except ValueError as exc:
                raise Failure('link', 'install_link_failed', '管理层装好了，但接入时出错：' + _clean_text(exc)) from None
        robot = self.platform.store.get('robots', self.robot_id)
        if robot.get('status') != 'connected':
            raise Failure('link', 'install_link_failed', '管理层装好了，但连接检查没有通过：' + _clean_text(robot.get('error') or ''))
        self.service._heard(self.connection_id)

    # ------------------------------------------------------------------ what is left, and taking it away

    def _leftovers(self):
        """What this attempt left on the robot and how to take it away, from one last read-only look."""
        try:
            self._look()
        except Exception:                          # whatever the robot answered, the job ends with a report
            LOG.exception('looking at what an install left on %s failed', self.robot_id)
            self.left, self.can_clean = [{'path': None, 'what': '没能确认设备上留下了什么'}], self.uploaded or self.created_install
            self.cleanup = '设备连得上之后再点「清理」，或者重新点「安装管理层」接着装。'

    def _look(self):
        package_dir, left = PACKAGE_DIR % self.package.id, []
        if self.mode == 'install' and self.stage == 'check':      # the check only reads
            self.left, self.can_clean, self.cleanup = [], False, '设备上没有留下任何东西。'
            return
        probe = None
        try:
            look = dev.DeviceSession(self._login, threading.Event(), waits=(2,))
            try:
                word, probe, _ = look.run('probe', self.package.id, seconds=30, deadline=self._deadline('look'))
            finally:
                look.close()
            probe = probe if word == 'probed' else None
        except (dev.Unreachable, dev.Stopped, Changed, ValueError):
            probe = None
        if probe is None:
            self.left = [{'path': None, 'what': '连不上设备，没能确认设备上留下了什么'}]
            if self.uploaded:
                self.left.append({'path': package_dir, 'what': '可能有这次上传的安装包'})
            self.cleanup = '设备连得上之后再点「清理」，或者重新点「安装管理层」接着装。'
            self.can_clean = self.uploaded or self.created_install
            return
        found = planning.found_of(probe)
        files = [name for name in probe.get('pkg_files', '').split() if name]
        if files:
            left.append({'path': package_dir, 'what': '这个控制台的安装包和日志（%d 个文件）' % len(files)})
        mine = found['console_package'] == self.package.id and self.created_install
        if found['root'] in ('installed', 'unfinished'):
            what = '%s的管理层%s' % ('这次装的' if mine else '原有的', '，正在运行' if found['running'] else '，没有运行')
            if found['root'] == 'unfinished':
                what = '一份没装完的管理层' + ('（这次装的）' if mine or self.created_install else '')
            left.append({'path': INSTALL_DIR, 'what': what})
        self.left = left
        removes = mine or (found['root'] == 'unfinished' and self.created_install)
        self.can_clean = bool(files) or removes
        if not left:
            self.cleanup = '设备上没有留下任何东西。'
        elif self.can_clean:
            parts = ['删除 %s' % package_dir] if files else []
            if removes:
                parts.insert(0, '停掉并删除这次装的 %s' % INSTALL_DIR)
            kept = '' if removes or found['root'] not in ('installed', 'unfinished') else '；原有的 %s 不会动' % INSTALL_DIR
            self.cleanup = '点「清理」会%s%s。重新点「安装管理层」会接着用已经传过去的安装包。' % ('、'.join(parts), kept)
        else:
            self.cleanup = '这些不是这次安装留下的，「清理」不会动它们。'

    def _clean(self):
        self._enter('clean')
        deadline = self._deadline('clean')
        word, probe, _ = self.session.run('probe', self.package.id, seconds=60, deadline=deadline)
        if word != 'probed':
            raise Failure('clean', 'install_probe_failed', '设备没有完成检查（%s）' % (word or '没有答复'))
        found = planning.found_of(probe)
        files = [name for name in probe.get('pkg_files', '').split() if name]
        ours = found['console_package'] == self.package.id or found['root'] == 'unfinished'
        if self.created_install and ours and found['root'] in ('installed', 'unfinished'):
            if 'device_install.sh' not in files:
                raise Failure('clean', 'install_clean_failed', '设备上找不到这次的安装脚本，没法删除 %s；请在设备上运行 '
                              'device_install.sh remove' % INSTALL_DIR)
            self.service._unlink(self)
            self._job('remove')
        word, keys, _ = self.session.run('clean', self.package.id, seconds=60, deadline=deadline)
        if word == 'busy':
            raise Failure('clean', 'install_clean_failed', '设备上这个安装包的安装还在进行；等它结束后再清理')
        if word not in ('cleaned', 'kept'):
            raise Failure('clean', 'install_clean_failed', '设备没有完成清理（%s）' % (word or '没有答复'))
        for entry in self.package.files:
            remote = (PACKAGE_DIR % self.package.id) + '/' + entry['name']
            if device_transfer.robot_transfer_status(self.platform, self.connection_id, remote) is not None:
                device_transfer.discard_robot_transfer(self.platform, self.connection_id, remote)
        self.done.add('clean')
        self.created_install = False
        kept = keys.get('kept', '')
        self.notes = ['设备上 %s 里还有不是这个控制台放进去的文件，没有删：%s' % (PACKAGE_DIR % self.package.id, kept[:200])] if kept else []
        return 'succeeded'


class RuntimeInstall:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.budgets, self.waits, self.poll_seconds = dict(BUDGETS), RETRY_WAITS, POLL_SECONDS
        self.transfer_options = {}                   # issue #18's own defaults (retry waits, stall time)
        self.upload_retry_seconds = 5                # between two calls of the upload channel when one gave up
        self._jobs, self._guard = {}, threading.Lock()
        self._wrap_removal(platform.robot_remove)
        holds = getattr(getattr(platform, 'connection_verify', None), 'holds', None)
        if isinstance(holds, list):                  # issue #16: "验证 SSH 连接" waits while the robot is being set up
            holds.append(lambda robot: '正在安装管理层；结束后再验证连接' if self.running(robot.get('id')) else '')

    def _wrap_removal(self, removal):
        """A robot is not deleted while its management layer is being installed. The check runs inside the
        removal's own locks, from its checks to its last delete (``RobotRemoval.remove``)."""
        check = removal._refuse_while_in_use

        def refuse_while_in_use(robot):
            if self.running(robot['id']):
                raise ValueError(INSTALLING)
            return check(robot)
        removal._refuse_while_in_use = refuse_while_in_use

    def running(self, robot_id):
        with self._guard:
            job = self._jobs.get(robot_id)
            return job is not None and job.state in ACTIVE

    def _robot(self, p):
        robot, connection = self.platform.engineering._ssh_connection(p)
        if robot.get('sim') or robot.get('managed_resource_id'):
            raise ValueError(ONLY_SSH_ROBOTS)
        return robot, connection

    def start(self, p):
        """``robots/runtime-install/start``: install the management layer on this robot. ``port`` (8790),
        ``existing`` ('use', 'start' or 'replace' once the robot was found to have one) and
        ``confirm_replace`` (true to delete an existing one). Returns the job's view at once."""
        port = DEFAULT_PORT if p.get('port') in (None, '') else p.get('port')
        if not _valid_port(port):
            raise ValueError(BAD_PORT)
        existing = p.get('existing')
        if existing is not None and (not isinstance(existing, str) or existing not in planning.CHOICES):
            raise CodedError(planning.CHOICE_INVALID.format(options='、'.join(planning.CHOICES.values())), 'install_choice_invalid')
        package = runtime_package.bundled()
        with self.platform.engineering.lock:       # a robot that is being deleted is either gone here or not yet
            robot, connection = self._robot(p)
            adapter = ADAPTERS.get(robot.get('family'))
            if adapter is None:
                raise CodedError(BODY.format(family=robot.get('family') or '未设置'), 'install_body_unsupported')
            with self._guard:
                if self._jobs.get(robot['id']) is not None and self._jobs[robot['id']].state in ACTIVE:
                    raise CodedError(BUSY, 'install_in_progress')
                job = Job(self, robot, connection, package, port, adapter, existing, p.get('confirm_replace') is True)
                self._jobs[robot['id']] = job
        self.platform.audit('robot.runtime_install_started', {'robot_id': robot['id'], 'package': package.id, 'port': port,
                                                              'adapter': adapter, 'existing': existing}, robot['project_id'])
        job.thread.start()
        return job.view()

    def clean(self, p):
        """``robots/runtime-install/clean``: take away what the last install of this robot left there."""
        with self.platform.engineering.lock:
            robot, connection = self._robot(p)
            outcome = robot.get(OUTCOME) or {}
            if not outcome.get('can_clean'):
                raise CodedError(NOTHING_TO_CLEAN, 'install_nothing_to_clean')
            try:
                package = runtime_package.bundled()
            except runtime_package.PackageError:
                package = None
            if package is None or package.id != outcome.get('package_id'):
                package = _Remembered(outcome)
            with self._guard:
                if self._jobs.get(robot['id']) is not None and self._jobs[robot['id']].state in ACTIVE:
                    raise CodedError(BUSY, 'install_in_progress')
                job = Job(self, robot, connection, package, outcome.get('port') or DEFAULT_PORT,
                          outcome.get('adapter') or 'sim_quadruped', mode='clean', previous=outcome)
                self._jobs[robot['id']] = job
        self.platform.audit('robot.runtime_install_clean', {'robot_id': robot['id'], 'package': package.id}, robot['project_id'])
        job.thread.start()
        return job.view()

    def stop(self, p):
        """``robots/runtime-install/stop``: stop waiting. What the robot does already goes on there. Returns within
        ten seconds; a login or a write the link holds up can take up to about two minutes to give up, and until
        then the view says ``stopping``."""
        robot, _ = self._robot(p)
        with self._guard:
            job = self._jobs.get(robot['id'])
        if job is None or job.state not in ACTIVE:
            return {'robot_id': robot['id'], 'install': job.view() if job else None}
        job.stop_event.set()
        job.thread.join(10)
        return {'robot_id': robot['id'], 'install': job.view()}

    def status(self, p):
        """``robots/runtime-install/status``: the view of every install of this workspace since the app started."""
        with self._guard:
            jobs = list(self._jobs.values())
        known = {robot['id'] for robot in self.store.list('robots')}
        return {'installs': {job.robot_id: job.view() for job in jobs if job.robot_id in known}}

    # ------------------------------------------------------------------ called by a job

    def _heard(self, connection_id):
        """An answer over the robot's SSH connection is a heartbeat too (issue #24)."""
        try:
            self.platform.heartbeat.heard(self.store.get('device_connections', connection_id))
        except (ValueError, AttributeError):
            pass

    def _unlink(self, job):
        """Before an install this console made is removed, the robot's link to it goes (issue #17)."""
        try:
            robot = self.store.get('robots', job.robot_id)
        except ValueError:
            return
        if robot.get('runtime_link'):
            self.platform.runtime_link.detach({'project_id': job.project_id, 'robot_id': job.robot_id})

    def _ended(self, job, state):
        """Save the outcome on the robot (the page shows it after a restart too)."""
        if state == 'needs_choice':
            return
        try:
            with self.platform.runtime_link._robot_lock(job.robot_id), self.store.atomic():
                robot = self.store.get('robots', job.robot_id)
                if robot.get('connection_id') != job.connection_id:
                    return
                robot[OUTCOME] = job.outcome(state)
                self.store.put('robots', robot)
        except ValueError:                           # the robot was deleted meanwhile
            return
        self.platform.audit('robot.runtime_install_' + state, {'robot_id': job.robot_id, 'package': job.package.id,
                                                                    'stage': job.stage, 'mode': job.mode,
                                                                    'code': (job.failure or {}).get('code')}, job.project_id)

    def close(self):
        with self._guard:
            jobs = [job for job in self._jobs.values() if job.state in ACTIVE]
        for job in jobs:
            job.stop_event.set()
        for job in jobs:
            job.thread.join(2)


class _Remembered:
    """The package an earlier install used, as its outcome remembers it: enough to clean up after it."""

    def __init__(self, outcome):
        self.id, self.runtime_version = outcome.get('package_id'), outcome.get('runtime_version')
        self.files = [{'name': name} for name in outcome.get('package_files') or [] if isinstance(name, str)
                      and runtime_package._NAME.fullmatch(name)]
        if not isinstance(self.id, str) or not runtime_package._ID.fullmatch(self.id):
            raise CodedError(NOTHING_TO_CLEAN, 'install_nothing_to_clean')

    def summary(self):
        return {'id': self.id, 'runtime_version': self.runtime_version, 'bytes': 0, 'files': []}
