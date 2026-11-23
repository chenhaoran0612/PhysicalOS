"""Start the management layer when the robot boots (issue #20, R1-03, R1-08).

Installing (issue #19) needs no administrator rights and changes nothing outside the login account's home
directory. Starting at boot is a step of its own that does need them: a systemd service
``neuxmind-runtime.service`` that runs the management layer as the SSH login account (the person chose
that over root, 2026-11-22), through the install script the console installed it with.

How it goes, as the stage "开机自启" of an install the person asked for it in (``runtime_install.STAGES``,
after "启动", before "接入并检查"), or as a job of its own from the card:

1. A read-only check as the login account (``runtime_autostart_device.CHECK_SCRIPT``): systemd, sudo
   (``sudo -n true``), the install, a service file that may already be there.
2. If anything stands in the way, nothing runs as root and the card says why (``REASONS``): no systemd,
   no sudo, an install this console did not make (the person's choice: refused, never touched), a
   service of that name someone else made.
3. Otherwise the job waits for the person (``Step._ask``) and shows exactly what will run: the service
   file, the four commands, the fixed root script itself, what it does not do, whether a password is
   needed. Nothing happens until they confirm; declining, or no answer in ``BUDGETS['confirm']``
   seconds, leaves the robot as it was.
4. On confirmation: ``~/neuxmind/.autostart`` is written as the login account, then the root script runs
   with sudo. A typed password goes to sudo's standard input for this one command and is then dropped:
   it is never saved, logged, shown, put into ``/api/state`` or onto a command line. A wrong password
   asks again (three tries).
5. The service takes the running runtime over (its ``ExecStartPre`` stops the detached one) and the job
   waits until systemd reports it running, then links it again (issue #17).

Taking it away (``autostart_off`` on the card, and before issue #19's "清理" or "删除后重新安装" removes the
install) needs the same confirmation. Declined there, the service file stays but starts nothing: its
``ConditionPathExists`` is the ``.autostart`` file inside the install, which goes with it.

The outcome is saved on the robot as ``runtime_autostart``; the page shows "已开机自启" or "未开机自启"
and why from it.
"""
import logging
import secrets
import threading
import time

from . import runtime_autostart_device as auto
from . import runtime_install_device as dev
from .errors import CodedError
from .service import now

LOG = logging.getLogger(__name__)

ON, OFF = 'autostart', 'autostart_off'
LABELS = {ON: '开机自启', OFF: '取消开机自启'}
BUDGETS = {ON: 300, OFF: 240, 'confirm': 900}
MODES = {'autostart': (ON, 'link'), 'autostart_off': (OFF, 'start', 'link')}
RECORD = 'runtime_autostart'                    # the field of the robot record that keeps the last outcome
TRIES = 3                                       # wrong passwords, or plans that changed under the person
FAILING_RESTARTS = 2                            # systemd restarted it this often without success: it will not come up
HOW = {'root': '以 root 登录，直接执行', 'nopasswd': '用 sudo 执行（这个账号不需要密码）',
       'password': '用 sudo 执行（需要这个账号的 sudo 密码）'}
REASONS = {
    'not_asked': '安装时没有选择开机自启',
    'declined': '你没有确认要执行的内容；控制台没有以管理员身份执行任何东西',
    'not_confirmed': '等了 {minutes} 分钟没有确认；控制台没有以管理员身份执行任何东西',
    'no_sudo': '这个账号不能用 sudo（设备上没有 sudo）',
    'no_systemd': '设备不是用 systemd 管理服务的（找不到 systemctl 或 /run/systemd/system），控制台没法登记开机自启',
    'not_console': '设备上的 ~/neuxmind 不是这个控制台装的（或者不是控制台记录的那一次安装），控制台不给它设开机自启。'
                   '要开机自启，先点「安装管理层」，选「删除后重新安装」',
    'not_installed': '设备上没有装好的管理层（或者装它的安装包不在了）；先点「安装管理层」',
    'unit_taken': '设备上已经有一个不是这个控制台为这个账号建的 ' + auto.UNIT_NAME + '，控制台不动它',
    'unsupported': '设备缺少登记服务要用的东西：{what}',
    'wrong_password': 'sudo 密码连续 %d 次不对，没有执行' % TRIES,
    'removed': '已取消开机自启：服务已停用，服务文件已删除',
    'failed': '{what}',
}
SUDO_WHY = {'not_permitted': '这个账号不在 sudoers 里，不能用 sudo（sudo 原话：{said}）',
            'needs_tty': '设备的 sudo 要求在终端里输入密码（requiretty），控制台没法代输（sudo 原话：{said}）'}
NOT_WAITING = '现在没有在等你确认开机自启（可能已经超时、被停止，或者已经确认过）'
PLAN_CHANGED = '要执行的内容已经变了，请按窗口里最新列出的内容确认'
ANSWER_INVALID = '回答只能是确认或不设开机自启'
PASSWORD_MISSING = '这台机器人的 sudo 需要密码，请输入这个账号的 sudo 密码'
PASSWORD_INVALID = 'sudo 密码要是一行、不超过 %d 个字符' % auto.PASSWORD_LIMIT
NOT_INSTALLED = '这台机器人还没有用控制台安装管理层；先点「安装管理层」'
NOT_CONSOLE = REASONS['not_console']
NOTHING_TO_REMOVE = '没有找到这台机器人开机自启的记录，也没有这个控制台装的管理层'
LEFT_UNIT = ('设备上还留着开机自启的服务文件 %s：它只在 ~/neuxmind/.autostart 存在时启动，这份安装删掉以后不会再启动任何东西。'
             '要删掉它，在设备上执行 sudo systemctl disable %s，再 sudo rm %s 和 sudo systemctl daemon-reload')


class Answer:
    def __init__(self, decision, password):
        self.decision, self.password = decision, password


def facts_of(keys, log):
    """What ``CHECK_SCRIPT`` found, in plain values. Everything here came from the robot: data, never a command."""
    def number(key):
        value = keys.get(key, '')
        return int(value) if value.isascii() and value.isdigit() and len(value) < 10 else 0
    sudo = keys.get('sudo', '')
    return {'user': keys.get('user', '')[:64], 'home': keys.get('home', '')[:200], 'home_ok': keys.get('home_ok') == 'yes',
            'systemd': keys.get('systemd') == 'yes', 'sudo': sudo if sudo in auto.SUDO_MODES else 'none',
            'missing': keys.get('missing', '').split()[:10], 'install': keys.get('install', ''),
            'console': keys.get('console', '')[:80], 'script': keys.get('script') == 'yes', 'running': keys.get('running') == 'yes',
            'mark': keys.get('mark', '')[:200], 'unit': keys.get('unit', ''), 'unit_same': keys.get('unit_same') == 'yes',
            'adapter': keys.get('adapter', '') if keys.get('adapter') in auto.ADAPTERS else None,
            'enabled': keys.get('enabled', '')[:40], 'digest': keys.get('digest', '')[:64],
            'active': keys.get('svc_activestate', '')[:40], 'sub': keys.get('svc_substate', '')[:40],
            'restarts': number('svc_nrestarts'), 'log': list(log)}


class Step:
    """The autostart part of one job of ``runtime_install`` (the stage, or the whole job in ``MODES``)."""

    def __init__(self, job):
        self.job = job
        self.wanted = False                 # the install was asked to turn autostart on
        self.state = None                   # what the page sees; never a password
        self.record = None                  # saved on the robot when the job ends; None: nothing changed
        self.root_ran = False               # a root script may have changed the robot's system files
        self.confirmed, self.notes = None, []   # the plan the person last confirmed; what a removal left
        self.facts = None                   # what the last check of "取消开机自启" found
        self._lock, self._answered, self._answer, self._plan = threading.Lock(), threading.Event(), None, None
        self._secret = None                 # the typed password, only while the root script runs

    # ------------------------------------------------------------------ what the page sees

    def view(self):
        with self._lock:
            if self.state is None:
                return None
            view = dict(self.state)
            if view.get('plan'):
                view['plan'] = {**view['plan'], 'commands': [dict(step) for step in view['plan']['commands']],
                                'account_steps': [dict(step) for step in view['plan']['account_steps']]}
            return view

    def _set(self, state, **fields):
        with self._lock:
            self.state = {'wanted': self.wanted, 'state': state, 'reason': None, 'message': '', 'plan': None, **fields}

    def hidden(self):
        return [self._secret] if self._secret else []

    # ------------------------------------------------------------------ on

    def turn_on(self):
        """Stage "开机自启". True when autostart is on at its end; False when it is not (the record says why)."""
        job = self.job
        job._enter(ON)
        self._set('checking')
        error, tries, changes = None, 0, 0
        while True:
            facts = self._check(ON)
            reason = self._refusal(facts)
            if reason is not None:
                return self._off(*reason)
            if self._is_on(facts):
                self._on(facts, 'already')
                return True
            answer = self._ask(self._plan_on(facts, error, tries), ON)
            if answer is None:
                return self._off('not_confirmed')
            if answer.decision == 'decline':
                return self._off('declined')
            result = self._root_on(facts, answer)
            if result == 'ok':
                break
            if result == 'wrong_password':
                tries += 1
                if tries >= TRIES:
                    self._mark('off')
                    return self._off('wrong_password')
                error = 'sudo 密码不对，没有执行；请重新输入（还可以试 %d 次）' % (TRIES - tries)
                continue
            if result in ('changed', 'password_required'):
                changes += 1
                if changes >= TRIES:
                    raise self._failure(ON, 'autostart_failed', '设备上的情况一直在变，没有执行开机自启')
                error = '设备上的情况在你确认之后变了，没有执行；请按下面最新的内容再确认一次'
                continue
            self._mark('off')
            return self._off(*result)
        # Registered: until systemd reports it running, that is what the robot record says (also after a stop).
        self._record('failed', 'failed', '服务已经登记，但还没有确认它把管理层运行起来')
        facts = self._wait_running()
        self._on(facts, 'enabled')
        return True

    def _refusal(self, facts):
        """(reason, message details) when autostart cannot be turned on here, or None."""
        job = self.job
        if not facts['systemd']:
            return ('no_systemd',)
        if facts['install'] != 'installed' or not facts['script']:
            return ('not_installed',) if facts['console'] in ('', job.package.id) else ('not_console',)
        if facts['console'] != job.package.id:
            return ('not_console',)
        if facts['unit'] in ('foreign', 'other'):
            return ('unit_taken',)
        if facts['sudo'] == 'none':
            return ('no_sudo',)
        if facts['missing']:
            return ('unsupported', '、'.join(facts['missing']))
        if not facts['home_ok'] or not auto.valid_inputs(facts['user'], facts['home'], job.package.id, job.port, job.adapter):
            return ('unsupported', '账号名或主目录里有控制台不写进服务文件的字符，或者主目录和登录时的不一致（%s，%s）' %
                    (facts['user'] or '?', facts['home'] or '?'))
        if facts['digest'] != auto.digest(self._unit(facts)):
            return ('unsupported', '设备和控制台生成的服务文件不一致')
        return None

    def _is_on(self, facts):
        job = self.job
        return (facts['unit'] == 'ours' and facts['unit_same'] and facts['enabled'] == 'enabled' and facts['active'] == 'active'
                and facts['running'] and facts['mark'].split() == [job.package.id, str(job.port), job.adapter])

    def _unit(self, facts):
        job = self.job
        return auto.render_unit(facts['user'], facts['home'], job.package.id, job.port, job.adapter)

    def _plan_on(self, facts, error, attempt):
        job, user = self.job, facts['user']
        sudo = '' if facts['sudo'] == 'root' else 'sudo '
        text = self._unit(facts)
        return {'kind': 'on', 'user': user, 'how': facts['sudo'], 'how_label': HOW[facts['sudo']],
                'needs_password': facts['sudo'] == 'password', 'unit_path': auto.UNIT_PATH, 'unit': text,
                'digest': auto.digest(text), 'script': auto.ON_SCRIPT, 'error': error, 'attempt': attempt,
                'account_steps': [{'command': 'printf ... > ~/neuxmind/.autostart',
                                   'what': '以 %s 的身份在 ~/neuxmind 里写一个标记文件 .autostart（内容：安装包编号、端口、本体）。'
                                           '服务只在它存在时启动；这份安装被删掉或换掉时它跟着消失，服务就不再启动任何东西' % user}],
                'commands': [
                    {'command': sudo + 'tee ' + auto.UNIT_PATH,
                     'what': '写入下面这个服务文件（属于 root，权限 0644）。脚本先在设备上按同样的规则重新生成一遍，和这里显示的一字不差才写'},
                    {'command': sudo + 'systemctl daemon-reload', 'what': '让 systemd 读到这个服务文件'},
                    {'command': sudo + 'systemctl enable ' + auto.UNIT_NAME,
                     'what': '登记开机自启：在 ' + auto.WANTS_PATH.rsplit('/', 1)[0] + '/ 里建一个指向它的链接'},
                    {'command': sudo + 'systemctl --no-block restart ' + auto.UNIT_NAME,
                     'what': '把正在运行的管理层交给系统服务：先用安装脚本停掉它，再由 systemd 按这个文件启动。中间几秒钟连不上，控制台随后重新接入'}],
                'does_not': '不改别的系统文件，不装软件，不改 sudo 配置。管理层仍以 %s 的身份运行，只监听机器人本机 127.0.0.1:%d；'
                            '断电重启后由 systemd 在网络就绪后启动，起不来时每 15 秒重试一次' % (user, job.port)}

    def _root_on(self, facts, answer):
        """Write the mark, run the root script. 'ok', 'wrong_password', 'changed', 'password_required', or the
        (reason, details) of why autostart stays off. Raises a failure when the robot is left half way."""
        job = self.job
        job._enter(ON)
        if not self._mark('on'):
            return ('not_installed',)
        self.root_ran = True
        word, _, log, stderr, code = self._root('autostart_on', facts, answer, facts['user'], job.package.id, job.port,
                                               job.adapter, self.confirmed['digest'])
        if word in ('enabled', 'already'):
            return 'ok'
        if word == 'changed':
            return 'changed'
        if word == 'taken':
            return ('unit_taken',)
        if word == 'nosystemd':
            return ('no_systemd',)
        if word:
            raise self._failure(ON, 'autostart_failed', '设备上的开机自启脚本停在「%s」（退出码 %s）' % (word, code), log)
        return self._sudo_refused(stderr)

    def _sudo_refused(self, stderr):
        reason = auto.sudo_refusal(stderr)
        said = (stderr.strip().splitlines() or [''])[-1][:200]
        if reason in ('wrong_password', 'password_required'):
            return reason
        if reason in SUDO_WHY:
            return ('no_sudo', SUDO_WHY[reason].format(said=said))
        raise self._failure(self.job.stage, 'autostart_failed', 'sudo 没有执行开机自启的脚本：' + (said or '没有说明'))

    def _wait_running(self):
        """After the restart: until systemd reports the service running and the runtime is up."""
        job = self.job
        deadline = job._deadline(ON)
        while True:
            facts = self._check(ON, deadline)
            if facts['active'] == 'active' and facts['sub'] == 'running' and facts['running']:
                return facts
            if facts['active'] == 'failed' or facts['restarts'] >= FAILING_RESTARTS:
                self._record('failed', 'failed', '服务登记好了，但 systemd 没能把管理层启动起来（%s/%s，重试 %d 次）' %
                             (facts['active'], facts['sub'], facts['restarts']))
                raise self._failure(ON, 'autostart_service_failed', self.record['message'], facts['log'])
            if time.monotonic() > deadline:
                self._record('failed', 'failed', '服务登记好了，但 %d 秒内没有运行起来（%s/%s）' %
                             (job.service.budgets[ON], facts['active'], facts['sub']))
                raise self._failure(ON, 'autostart_timeout', self.record['message'], facts['log'])
            if job.stop_event.wait(job.service.poll_seconds):
                raise dev.Stopped()

    # ------------------------------------------------------------------ off

    def turn_off(self):
        """Stage "取消开机自启". True when no service of this console is left for this account. When it stays
        (declined, no sudo), the saved "on" stays as it is: the robot still starts it at boot."""
        job = self.job
        job._enter(OFF)
        self._set('checking')
        error, tries = None, 0
        while True:
            facts = self.facts = self._check(OFF)
            if facts['unit'] == 'none' or not facts['systemd']:
                self._mark('off')
                self._off('removed')
                return True
            if facts['unit'] != 'ours':
                self._off('unit_taken')
                return False
            if facts['sudo'] == 'none':
                return self._left('no_sudo', REASONS['no_sudo'])
            answer = self._ask(self._plan_off(facts, error, tries), OFF)
            if answer is None:
                return self._left('not_confirmed', '没有确认，开机自启没有取消')
            if answer.decision == 'decline':
                return self._left('declined', '你没有确认，开机自启没有取消')
            job._enter(OFF)
            self.root_ran = True
            word, _, log, stderr, code = self._root('autostart_off', facts, answer, facts['user'])
            if word in ('disabled', 'absent'):
                break
            if word == 'taken':
                self._off('unit_taken')
                return False
            if word:
                raise self._failure(OFF, 'autostart_failed', '设备上取消开机自启的脚本停在「%s」（退出码 %s）' % (word, code), log)
            result = self._sudo_refused(stderr)
            if result in ('wrong_password', 'password_required'):
                tries += 1
                if tries >= TRIES:
                    return self._left('wrong_password', REASONS['wrong_password'])
                error = ('sudo 密码不对，没有执行；请重新输入（还可以试 %d 次）' % (TRIES - tries) if result == 'wrong_password'
                         else '设备上的 sudo 现在要密码了，没有执行；请输入 sudo 密码再确认')
                continue
            return self._left(*result)
        self._mark('off')
        self._off('removed')
        return True

    def _left(self, reason, message):
        self._set('left', reason=reason, message=message)
        return False

    def _plan_off(self, facts, error, attempt):
        user, sudo = facts['user'], '' if facts['sudo'] == 'root' else 'sudo '
        return {'kind': 'off', 'user': user, 'how': facts['sudo'], 'how_label': HOW[facts['sudo']],
                'needs_password': facts['sudo'] == 'password', 'unit_path': auto.UNIT_PATH, 'unit': None, 'digest': None,
                'script': auto.OFF_SCRIPT, 'error': error, 'attempt': attempt,
                'commands': [{'command': sudo + 'systemctl disable --now ' + auto.UNIT_NAME,
                              'what': '停掉这个服务（管理层随之停止）并取消开机自启'},
                             {'command': sudo + 'rm ' + auto.UNIT_PATH, 'what': '删除这个控制台为 %s 建的服务文件；别人建的同名服务不动' % user},
                             {'command': sudo + 'systemctl daemon-reload', 'what': '让 systemd 忘掉它'}],
                'account_steps': [{'command': 'rm ~/neuxmind/.autostart', 'what': '以 %s 的身份删掉标记文件 .autostart' % user}],
                'does_not': '不改别的系统文件，不删 ~/neuxmind 里的其他东西'}

    def off_first(self, before):
        """Before issue #19 removes an install (``before``: 'remove' or 'clean'): take its service away too.
        Only for a robot that had autostart from this console (saved, or turned on by this job): other robots
        see no extra command. A service file that stays starts nothing once the install is gone."""
        job = self.job
        try:
            saved = job.platform.store.get('robots', job.robot_id).get(RECORD) or {}
        except ValueError:
            return
        if not self.root_ran and saved.get('state') not in ('on', 'failed'):
            return
        facts = self._check(before)
        if not facts['systemd'] or facts['unit'] != 'ours':
            return
        if OFF not in job.steps:
            job.steps.insert(job.steps.index(before) if before in job.steps else 0, OFF)
        removed = self.turn_off()
        job.done.add(OFF)
        if not removed:
            self.notes.append(LEFT_UNIT % (auto.UNIT_PATH, auto.UNIT_NAME, auto.UNIT_PATH))
            job.notes = [*job.notes, *self.notes]
        job._enter(before)

    # ------------------------------------------------------------------ the person

    def _ask(self, plan, stage):
        """Show ``plan`` and wait for the person's answer. None when none came in time."""
        job = self.job
        plan = {**plan, 'id': secrets.token_hex(8), 'host': self._host()}
        with self._lock:
            self._answered.clear()
            self._answer, self._plan = None, plan
        self._set('waiting', plan=plan)
        job.session.close()                          # nothing runs on the robot while a person reads
        end = time.monotonic() + job.service.budgets['confirm']
        while not self._answered.wait(0.1):
            if job.stop_event.is_set() or time.monotonic() > end:
                break
        with self._lock:                             # from here on no answer is taken for this plan
            answer, self._answer, self._plan = self._answer, None, None
        self.confirmed = plan if answer is not None else None
        self._set('running')
        if answer is None and job.stop_event.is_set():
            raise dev.Stopped()
        return answer

    def answer(self, p):
        """The person's answer to the plan the job waits on: 'confirm' (with ``sudo_password`` when needed) or 'decline'."""
        decision = p.get('decision')
        if decision not in ('confirm', 'decline'):
            raise CodedError(ANSWER_INVALID, 'autostart_answer_invalid')
        with self._lock:
            plan = self._plan
            waiting = self.state is not None and self.state.get('state') == 'waiting' and plan is not None
            if not waiting or self._answer is not None:
                raise CodedError(NOT_WAITING, 'autostart_not_waiting')
            if p.get('plan') != plan['id']:
                raise CodedError(PLAN_CHANGED, 'autostart_plan_changed')
            password = None
            if decision == 'confirm' and plan['needs_password']:
                password = p.get('sudo_password')
                if password in (None, ''):
                    raise CodedError(PASSWORD_MISSING, 'autostart_password_missing')
                if not auto.password_ok(password):
                    raise CodedError(PASSWORD_INVALID, 'autostart_password_invalid')
            self._answer = Answer(decision, password)
            self.state = {**self.state, 'state': 'running', 'plan': None}
            self._answered.set()                     # under the lock: never after the next question cleared it
        return self.job.view()

    def _host(self):
        try:
            connection = self.job.platform.store.get('device_connections', self.job.connection_id)
            return '%s@%s' % (connection.get('username'), connection.get('host'))
        except (ValueError, AttributeError):
            return ''

    # ------------------------------------------------------------------ on the robot

    def _check(self, stage, deadline=None):
        job = self.job
        deadline = deadline or job._deadline(stage)
        word, keys, log, _, _ = auto.run_script(job.session, 'autostart_check', job.package.id, job.port, job.adapter,
                                                seconds=60, deadline=deadline)
        if word != 'checked':
            raise self._failure(stage, 'autostart_check_failed', '设备没有完成开机自启的检查（%s）' % (job._shown(word, 40) or '没有答复'))
        job.service._heard(job.connection_id)
        facts = facts_of(keys, log)
        if facts['adapter']:                       # the body the install recorded (issue #21): the service and a
            job.adapter = facts['adapter']         # later start use that one, never another body for this install
        return facts

    def _mark(self, want):
        job = self.job
        word, _, _, _, _ = auto.run_script(job.session, 'autostart_mark', job.package.id, job.port, job.adapter, want,
                                           seconds=30, deadline=job._deadline(job.stage if job.stage in BUDGETS else ON))
        return word in ('marked', 'unmarked')

    def _root(self, kind, facts, answer, *words):
        """Run a root script. What comes back is cleaned while the typed password is still known, then it is dropped."""
        job = self.job
        password, answer.password = answer.password or '', None
        self._secret = password or None
        try:
            word, keys, log, stderr, code = auto.run_root(job.session, kind, facts['sudo'], password, *words, seconds=150,
                                                          deadline=job._deadline(job.stage))
            return word, keys, [job._shown(line) for line in log], job._shown(stderr or '', 2000), code
        finally:
            self._secret, password = None, None

    def leftovers(self):
        """After a failure or a stop: the system files a root script of this job may have left, from one more look."""
        job = self.job
        if not self.root_ran:
            return
        try:
            look = dev.DeviceSession(job._login, threading.Event(), waits=(2,))
            try:
                word, keys, _, _, _ = auto.run_script(look, 'autostart_check', job.package.id, job.port, job.adapter,
                                                      seconds=30, deadline=time.monotonic() + job.service.budgets['look'])
            finally:
                look.close()
        except Exception:                          # a look that fails changes nothing of what the report says
            LOG.exception('looking at what autostart left on %s failed', job.robot_id)
            return
        if word != 'checked' or keys.get('unit') != 'ours':
            return
        job.left.append({'path': auto.UNIT_PATH, 'what': '开机自启的服务文件（这次写的）'})
        if keys.get('mark'):
            job.left.append({'path': '~/neuxmind/' + auto.MARK_FILE, 'what': '开机自启的标记文件'})
        job.cleanup = (job.cleanup + ' ' if job.cleanup else '') + '开机自启的服务文件要点「取消开机自启」删掉（需要管理员权限）。'

    # ------------------------------------------------------------------ the outcome

    def _failure(self, stage, code, message, log=()):
        from .runtime_install import Failure
        return Failure(stage, code, message, [self.job._shown(line) for line in log])

    def _record(self, state, reason, message):
        job = self.job
        self.record = {'state': state, 'reason': reason, 'message': message, 'unit': auto.UNIT_PATH,
                       'package_id': job.package.id, 'port': job.port, 'adapter': job.adapter, 'job_id': job.id, 'at': now()}

    def _on(self, facts, how):
        self._record('on', None, '已开机自启：系统服务 %s 以 %s 的身份运行管理层' % (auto.UNIT_NAME, facts['user']))
        self._set('on', message=self.record['message'], how=how)

    def _off(self, reason, details='', state='off'):
        minutes = max(1, self.job.service.budgets['confirm'] // 60)
        message = details if reason == 'no_sudo' and details else REASONS[reason].format(what=details, minutes=minutes)
        self._record('off', reason, message)
        self._set(state, reason=reason, message=message)
        return False

    def refuse(self, reason):
        """The install was asked for autostart but its plan has no such step (an install this console did not make)."""
        self._off(reason)


# ------------------------------------------------------------------ the jobs of their own (the card)

def run_mode(job):
    """``MODES``: turn autostart on (then link again) or off (then start the runtime detached and link)."""
    step = job.autostart
    if job.mode == 'autostart':
        if step.turn_on():
            job.done.add(ON)
            job._link()
            job.done.add('link')
        else:
            job.steps = [ON]
            job.done.add(ON)
        return 'succeeded'
    if step.turn_off():
        job.done.add(OFF)
        if step.facts['install'] == 'installed' and step.facts['script']:
            job._start()                           # the service stopped it: it runs detached again, as after an install
            job.done.add('start')
            job._link()
            job.done.add('link')
        else:                                      # the install itself is gone (removed by hand): nothing to start
            job.steps = [OFF]
    else:
        job.steps = [OFF]
        job.done.add(OFF)
    return 'succeeded'


def start_mode(service, p, mode):
    """``robots/runtime-install/autostart`` and ``…/autostart-off``: a job of its own for a robot whose
    management layer this console installed."""
    from .runtime_install import ACTIVE, BUSY, Job, _Remembered
    with service.platform.engineering.lock:
        robot, connection = service._robot(p)
        outcome, saved = robot.get('runtime_install') or {}, robot.get(RECORD) or {}
        if mode == 'autostart':
            if not outcome.get('package_id'):
                raise CodedError(NOT_INSTALLED, 'autostart_not_installed')
            if not outcome.get('console_install', outcome.get('created_install')):
                raise CodedError(NOT_CONSOLE, 'autostart_not_console')
            source = outcome
        else:
            source = saved if saved.get('package_id') else outcome
            if not source.get('package_id'):
                raise CodedError(NOTHING_TO_REMOVE, 'autostart_nothing_to_remove')
        package = _Remembered({**outcome, 'package_id': source['package_id']})
        port, adapter = source.get('port') or 8790, source.get('adapter') or 'sim_quadruped'
        with service._guard:
            if service._jobs.get(robot['id']) is not None and service._jobs[robot['id']].state in ACTIVE:
                raise CodedError(BUSY, 'install_in_progress')
            job = Job(service, robot, connection, package, port, adapter, mode=mode, previous=outcome)
            service._jobs[robot['id']] = job
    service.platform.audit('robot.runtime_autostart_started', {'robot_id': robot['id'], 'mode': mode, 'package': package.id},
                           robot['project_id'])
    job.thread.start()
    return job.view()


def answer(service, p):
    """``robots/runtime-install/answer``: the person's answer to what an autostart step waits on."""
    robot, _ = service._robot(p)
    with service._guard:
        job = service._jobs.get(robot['id'])
    if job is None or job.id != p.get('job_id') or job.state != 'running':
        if p.get('decision') not in ('confirm', 'decline'):
            raise CodedError(ANSWER_INVALID, 'autostart_answer_invalid')
        raise CodedError(NOT_WAITING, 'autostart_not_waiting')
    return job.autostart.answer(p)


def ended(service, job, state):
    """Save what the autostart step concluded on the robot (the card shows it after a restart too)."""
    record = job.autostart.record
    if record is None:
        return
    try:
        with service.platform.runtime_link._robot_lock(job.robot_id), service.store.atomic():
            robot = service.store.get('robots', job.robot_id)
            if robot.get('connection_id') != job.connection_id:
                return
            robot[RECORD] = record
            service.store.put('robots', robot)
    except ValueError:                             # the robot was deleted meanwhile
        return
    service.platform.audit('robot.runtime_autostart_' + record['state'], {'robot_id': job.robot_id, 'mode': job.mode,
                                                                          'reason': record['reason'], 'job': state}, job.project_id)
