"""What a hardware repair runs without asking, and which problems it is not given (issue #13).

* ``hardware.auto_approve`` ("默认允许执行"): chosen when a repair or a fix chat starts, or
  switched during the session. While it is on, a device command the assistant proposes
  (``device_fix``, ``device_exec``) is answered the moment it is queued for approval, so
  nobody has to be there. It has already passed the product's hard limits
  (``hardware_init.check_command``) by then; those stay as they are. With nobody reading
  the command, those limits are all that is left, so three things still wait for a person:
  a command the product cannot judge from its text (``ASK_FIRST``: command substitution,
  downloads, running a script file, recursive deletes, credential locations ...), the
  command after ``AUTO_RUN_LIMIT`` unattended ones in a row, and every request that is not
  a repair command: reading a new directory, anything on the user's own computer.
* Blocks: a problem that software cannot fix is only annotated. Either the scan already
  classes it as needing on-site work, or a repair report left it as ``needs_user``,
  ``not_fixed`` or ``pending_reboot``. A blocked problem cannot be handed to the
  assistant until a person marks it as handled (``hardware_issue_marks``). A problem
  that is gone in a complete scan loses its mark, so it starts clean if it comes back.
"""
import hashlib
import re

from . import device_access
from .service import now

AUTO_METHODS = ('device_fix/requestApproval', 'device_exec/requestApproval')
# Unattended commands in a row before the next one waits; the user's "go on" starts the count again.
AUTO_RUN_LIMIT = 60
_REST = r'[^\n;|&]*'       # the rest of one simple command
_START = r'(?:^|[;&|(]\s*)(?:\w+=\S*\s+)*'     # where a command starts, after any VAR=value prefixes
_SHELL = r'(?:ba|z|da|k)?sh'
_INTERPRETER = r'(?:python[\d.]*|perl|ruby|node|php|lua)'
# Allowed once a person has read them, never run unasked: the product cannot tell from the text what
# will run, or the command reaches for more than a repair of drivers, network and permissions needs.
# Like the hard limits this list cannot be complete; it narrows what runs with nobody watching.
ASK_FIRST = [
    (re.compile(r'\$\(|`|\bbase64\b|\bxxd\b|\\\n|' + _START + r'\w+=[^\s;&|]*\s*(?:;|&&|\|\|)'),
     '含有命令替换、编码内容、续行或自定义变量，从文字上看不出实际执行什么'),
    (re.compile(r'\b(?:curl|wget|nc|ncat|netcat|socat|telnet|scp|sftp|rsync|ftp|tftp)\b|\bgit\s+clone\b'), '会从网络取回内容或向外传输数据'),
    (re.compile(_START + r'(?:\./|~/|/tmp/|/var/tmp/|/dev/shm/|/home/)\S*|\b' + _SHELL + r'\s+(?!-)[^\s;|&]|\b' + _INTERPRETER + r'\s+(?!-)[^\s;|&]|'
                r'\|\s*(?:' + _SHELL + r'|' + _INTERPRETER + r')\b'),
     '会运行一个脚本或程序文件，或把内容交给解释器执行，产品看不到它的内容'),
    (re.compile(r'\brm\b' + _REST + r'\s-(?:-recursive\b|[a-zA-Z]*[rR])|\b(?:chmod|chown|chgrp)\b' + _REST + r'\s-(?:-recursive\b|[a-zA-Z]*R)|'
                r'\bfind\b' + _REST + r'\s-(?:delete|exec|execdir|ok|okdir)\b'),
     '会递归删除或批量修改文件'),
    (re.compile(r'\bcrontab\b|/etc/cron|/etc/rc\.local|\bat\s+(?:now|midnight|noon|\d)|/etc/sudoers|\bvisudo\b'), '会设置定时或开机自动执行，或修改提权配置'),
    (re.compile(r'(?:^|[\s/=])\.(?:ssh|aws|gnupg|kube|docker|netrc|pgpass|git-credentials|bash_history|env)(?![\w-])|/etc/g?shadow\b|'
                r'\bid_(?:rsa|dsa|ecdsa|ed25519)(?!\S*\.pub\b)|\.(?:pem|key|p12|pfx)\b', re.I),
     '涉及存放密钥或凭据的位置'),
]
# Report outcomes that mean the assistant cannot get further without a person.
STUCK = ('needs_user', 'not_fixed', 'pending_reboot')
RESOLVED = ('fixed', 'not_reproduced')
ONSITE = 'onsite'
MARKS = 'hardware_issue_marks'
NOTE_CHARS = 600
COMMAND_CHARS = 1500

SOFTWARE_ONLY = '''
只做软件层面的修复：驱动与内核模块、固件包、网络配置、设备权限、相关系统服务。软件能修的问题直接着手修复。
判断为软件修不了的问题（硬件损坏、线缆或接口松动、供电不足、设备没有接上、需要现场操作或需要用户提供的东西）不要反复尝试：在修复报告里标为 needs_user，需要重启或重新插拔才能生效的标为 pending_reboot，试过仍未解决的标为 not_fixed；actions 写明判断依据，followups 写明现场要做什么。
产品会把这三种结果的问题标注为“等待现场处理”，不再交给你；用户现场处理并标注后，才会重新交给你。
'''
AUTO_ALLOW = '''
用户开启了“默认允许执行”：device_fix 和 device_exec 通过产品检查后直接执行，不再逐条询问，用户可能不在场。
所以每条命令要更保守：一次一条、最小改动、写清回退方法；没有把握的改动不要执行，把问题标为 needs_user 并说明原因。不要调用需要用户回答的提问工具来等待用户。
下面这些命令不会直接执行，会停下来等用户批准，能不用就不用：命令替换（$(…)、反引号）、编码内容、续行和自定义变量；curl、wget、scp 等下载或网络传输；运行脚本或程序文件、把内容交给 sh / python 执行；递归删除或批量改权限；定时任务与 sudoers；读取密钥或凭据所在的位置。需要某个值（例如内核版本）时，先用只读命令取得，再把值直接写进命令。
申请读取新目录（device_request_scope）也始终要等用户批准；修复通常用不到它。
'''
SWITCHED_ON = '用户开启了默认允许执行：之后的设备命令通过产品检查后直接执行，不再逐条询问'
SWITCHED_OFF = '用户改为逐条批准：之后的设备命令执行前都会先询问'


def needs_onsite(issue):
    """The scan itself says this problem needs on-site work. Notes (info) are the user's to judge."""
    return issue.get('fixable') != 'auto' and issue.get('severity') != 'info'


def auto_allowed(s):
    return s.get('mode') == 'repair' and (s.get('hardware') or {}).get('auto_approve') is True


def ask_first(request):
    """Why this device command waits for a person even in a session that runs without asking; '' when it may run."""
    command = str(request.get('command', ''))
    plain = re.sub(r'["\'\\]', '', command)      # 'cu'rl, r"m" -rf, \curl
    for pattern, reason in ASK_FIRST:
        if pattern.search(command) or pattern.search(plain):
            return reason
    return ''


def instructions(s):
    """What a repair session's model is told on top of the repair rules."""
    return SOFTWARE_ONLY + (AUTO_ALLOW if auto_allowed(s) else '')


def mark_id(robot_id, issue_id):
    return 'hardware-mark-' + hashlib.sha256(f'{robot_id}\n{issue_id}'.encode()).hexdigest()[:20]


def describe(scan, marks):
    """One entry for each problem of this scan that is blocked, or was and has been marked as handled."""
    entries = []
    for issue in scan['issues']:
        mark = marks.get(issue['id'])
        if not mark and not needs_onsite(issue):
            continue
        mark = mark or {'reason': ONSITE, 'note': '', 'repair_id': None, 'blocked_at': scan['created_at'], 'handled_at': None}
        entries.append({'robot_id': scan['robot_id'], 'project_id': scan['project_id'], 'scan_id': scan['id'], 'issue_id': issue['id'],
                        'state': 'handled' if mark.get('handled_at') else 'blocked', 'reason': mark['reason'], 'note': mark['note'],
                        'repair_id': mark.get('repair_id'), 'blocked_at': mark['blocked_at'], 'handled_at': mark.get('handled_at')})
    return entries


def auto_message(pending):
    """The session record of a command that ran without a question: what it was, in full view."""
    request = pending.get('request') or {}
    purpose = request.get('purpose', '')
    command = str(request.get('command', ''))
    shown = command if len(command) <= COMMAND_CHARS else command[:COMMAND_CHARS] + '…（完整命令见这一步的依据）'
    kind = '修复命令' if pending['method'] == 'device_fix/requestApproval' else 'SSH 命令'
    return f'默认允许执行，未询问：{kind}“{purpose}”\n{shown}'


class RepairPolicy:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    # ---- which problems can be handed to the assistant ----------------------
    def _marks(self, robot_id):
        return {m['issue_id']: m for m in self.store.list(MARKS) if m['robot_id'] == robot_id}

    def _latest_scan(self, robot_id):
        latest = None
        for scan in self.store.list('hardware_scans'):       # newest first: the first of equal times wins
            if scan['robot_id'] == robot_id and (latest is None or scan['created_at'] > latest['created_at']):
                latest = scan
        return latest

    def snapshot(self, scans):
        """``hardware_blocks``: the blocked and handled problems of each robot's latest scan."""
        marks, latest = {}, {}
        for mark in self.store.list(MARKS):
            marks.setdefault(mark['robot_id'], {})[mark['issue_id']] = mark
        for scan in scans:
            if scan['robot_id'] not in latest or scan['created_at'] > latest[scan['robot_id']]['created_at']:
                latest[scan['robot_id']] = scan
        return {'hardware_blocks': [entry for scan in latest.values() for entry in describe(scan, marks.get(scan['robot_id'], {}))]}

    def check_selectable(self, robot_id, scan, chosen):
        blocked = {entry['issue_id'] for entry in describe(scan, self._marks(robot_id)) if entry['state'] == 'blocked'}
        titles = [issue['title'] for issue in scan['issues'] if issue['id'] in blocked and issue['id'] in set(chosen)]
        if titles:
            raise ValueError('这些问题软件修不了，要先现场处理并标注为“已处理”，才能交给助手：' + '、'.join(titles))

    def start_fields(self, robot_id, scan, chosen, p):
        """Checked when a repair or a fix chat starts; what it adds to the session's ``hardware``."""
        self.check_selectable(robot_id, scan, chosen)
        choice = p.get('auto_approve')
        if choice is not None and not isinstance(choice, bool):
            raise ValueError('“默认允许执行”须为是或否')
        # None: nobody chose (an API caller, a session from before this option). It asks, like False.
        return {'auto_approve': choice}

    def mark(self, p):
        """A person says a blocked problem has been dealt with on site (or takes that back)."""
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        handled, issue_id = p.get('handled'), p.get('issue_id')
        if not isinstance(handled, bool):
            raise ValueError('标注须为是或否')
        with self.store.atomic():       # with scan_saved: a mark is never written against a scan that was just replaced
            scan = self._latest_scan(robot['id'])
            if not scan:
                raise ValueError('这台机器人还没有硬件扫描结果，没有可以标注的问题')
            entry = next((e for e in describe(scan, self._marks(robot['id'])) if e['issue_id'] == issue_id), None)
            if not entry:
                raise ValueError('这个问题不需要标注：它不在最近一次扫描里，或者可以直接交给助手处理')
            if handled == (entry['state'] == 'handled'):
                raise ValueError('这个问题已经标注为处理完成' if handled else '这个问题没有标注过处理完成')
            record = {'id': mark_id(robot['id'], issue_id), 'project_id': robot['project_id'], 'robot_id': robot['id'], 'issue_id': issue_id,
                      'reason': entry['reason'], 'note': entry['note'], 'repair_id': entry['repair_id'], 'blocked_at': entry['blocked_at'],
                      'handled_at': now() if handled else None}
            self.store.put(MARKS, record)
        self.platform.audit('device.hardware_issue_marked', {'robot_id': robot['id'], 'issue_id': issue_id, 'handled': handled,
                                                             'reason': entry['reason']}, robot['project_id'])
        return next(e for e in describe(scan, {issue_id: record}) if e['issue_id'] == issue_id)

    def report_saved(self, report):
        """A repair report was saved: what it could not fix is blocked, what it fixed is clean again."""
        try:
            known = {issue['id'] for issue in self.store.get('hardware_scans', report['scan_id'])['issues']}
        except ValueError:      # the baseline scan was removed: nothing to annotate against
            return
        with self.store.atomic():
            self._record_results(report, known)

    def _record_results(self, report, known):
        latest = self._latest_scan(report['robot_id'])
        # What the newest complete scan no longer shows is gone: it gets no annotation, whatever the report says.
        present = known & {issue['id'] for issue in latest['issues']} if latest and latest.get('complete', True) else known
        for result in report['results']:
            issue_id = result['issue_id']
            if issue_id not in known:       # the user's own request in a fix chat is not a scan problem
                continue
            if result['outcome'] in STUCK and issue_id in present:
                self.store.put(MARKS, {'id': mark_id(report['robot_id'], issue_id), 'project_id': report['project_id'], 'robot_id': report['robot_id'],
                                       'issue_id': issue_id, 'reason': result['outcome'], 'note': device_access.redact(result['actions'])[:NOTE_CHARS],
                                       'repair_id': report['id'], 'blocked_at': report['created_at'], 'handled_at': None})
            elif result['outcome'] in RESOLVED or issue_id not in present:
                self.store.delete(MARKS, mark_id(report['robot_id'], issue_id))

    def scan_saved(self, record):
        """A problem that is gone in a complete scan loses its mark. Called under the store lock that saved the scan."""
        if not record.get('complete', True):
            return
        present = {issue['id'] for issue in record['issues']}
        with self.store.atomic():
            for mark in self._marks(record['robot_id']).values():
                if mark['issue_id'] not in present:
                    self.store.delete(MARKS, mark['id'])

    # ---- 默认允许执行 ---------------------------------------------------------
    def answer_waiting(self, sid):
        """Called whenever the head of a session's request queue may have changed: the engine queued a
        device command, or the user answered the request in front of one."""
        with self.platform.engineering.lock:
            self._accept_head(sid)

    def _accept_head(self, sid):
        """Accept the device commands at the head of the session's queue while it runs without asking.
        Stops at the first request of another kind: that one is the user's to decide."""
        engine = self.platform.engineering
        while True:
            s, handle = self.store.get('agent_sessions', sid), engine.live.get(sid)
            pending = s.get('pending_approval')
            if not handle or not pending or not auto_allowed(s) or pending.get('method') not in AUTO_METHODS:
                return
            state, request = s['hardware'], pending.get('request') or {}
            runs = state.get('auto_runs', 0)
            hold = ask_first(request) or (f'已经连续直接执行了 {runs} 条设备命令，之后的命令先由你确认' if runs >= AUTO_RUN_LIMIT else '')
            if hold:
                # Said once per request. Request ids start again with every run, so the command is part of what identifies it.
                held = {'id': pending['id'], 'command': str(request.get('command', '')), 'reason': hold}
                known = state.get('auto_hold') or {}
                if (known.get('id'), known.get('command')) != (held['id'], held['command']):
                    engine.update(sid, hardware={**state, 'auto_hold': held})
                    engine.event(sid, 'approval', '这条命令没有直接执行，等你批准：' + hold)
                return
            handle['answers'].put((pending['id'], {'decision': 'accept'}))
            engine.update(sid, hardware={**state, 'auto_runs': runs + 1})
            engine.event(sid, 'approval', auto_message(pending))
            engine._advance_request(sid)

    def set_auto(self, p):
        """Switch a repair session between running device commands unasked and asking for each."""
        s = self.platform.same_project('agent_sessions', p.get('session_id'), p.get('project_id'))
        if s.get('mode') != 'repair':
            raise ValueError('只有硬件修复会话可以设置“默认允许执行”')
        enabled = p.get('enabled')
        if not isinstance(enabled, bool):
            raise ValueError('“默认允许执行”须为是或否')
        engine = self.platform.engineering
        with engine.lock:
            current = self.store.get('agent_sessions', s['id'])
            # Switching on is also the user's "go on" after AUTO_RUN_LIMIT commands in a row.
            engine.update(s['id'], hardware={**current['hardware'], 'auto_approve': enabled, 'auto_runs': 0})
            engine.event(s['id'], 'approval', SWITCHED_ON if enabled else SWITCHED_OFF)
            if enabled:
                self._accept_head(s['id'])
        self.platform.audit('device.hardware_auto_approve_changed', {'robot_id': s.get('robot_id'), 'session_id': s['id'], 'enabled': enabled},
                            s['project_id'])
        return self.store.get('agent_sessions', s['id'])
