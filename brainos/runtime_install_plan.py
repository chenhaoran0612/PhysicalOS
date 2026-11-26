"""What an install does on one robot, decided from the read-only probe (issue #19, R1-03).

``plan`` is a pure function of what the probe found, so every case is a plain test:

* **Nothing at ``~/neuxmind``** (or an empty directory): upload, install, start, link.
* **An unfinished install** (the install script's marker still says ``installing``): if this
  package's install job is still running there, wait for it; if that job began there and never ended
  (killed, power lost), install again (the script deletes the unfinished one first). An unfinished install this package never started
  (someone may be installing by hand right now) is only replaced after a person chose and confirmed it.
* **A finished install that this console made from this package** (``.console-package``): this is
  our own earlier attempt. Link it if it runs, start it first if it does not. No question asked.
* **Any other finished install** (made by hand, or from another package): nothing happens until a
  person chooses. ``use``: link the running one, nothing on the robot changes. ``start``: start a
  stopped one of the same runtime version, then link. ``replace``: delete it (its state, log and
  token) and install this package; only with ``confirm`` set. Upgrading in place, keeping the
  state, is not offered: the install script cannot do it.
* **Something at ``~/neuxmind`` that the install script did not make** (or a symbolic link):
  refused. The console never touches it.

Also refused before anything is written: a robot that is not Linux, has no Python 3.10 with
``venv``, misses a tool the install script needs, has too little free space, already has
something else listening on the port, or whose SSH service does not forward TCP (issue #17 could
not reach the management layer then).
"""
import re

from . import runtime_install_device as dev

_NUMBER = re.compile(r'[0-9]{1,12}', re.ASCII)       # a robot's digits: '²'.isdigit() is True, int('²') fails
MIN_PYTHON = (3, 10)
MIN_FREE_KB = 300 * 1024                  # a venv with the two wheels takes about 25 MB; pip needs room to work
ADAPTERS = {'quadruped': 'sim_quadruped', 'manipulator': 'sim_arm'}    # see the module text of runtime_install
FRESH = ('upload', 'install', 'start', 'link')
SCRIPTED = ('install', 'start', 'remove', 'wait_install')
AUTOSTART = 'autostart'     # issue #20: only when the person asked for it, and only for this console's own install


def with_autostart(steps):
    """The steps with autostart (issue #20) right before the link: after the start when there is one.
    ``FRESH`` with it is ('upload', 'install', 'start', 'autostart', 'link')."""
    steps = list(steps)
    if AUTOSTART not in steps and 'link' in steps:
        steps.insert(steps.index('link'), AUTOSTART)
    return tuple(steps)
CHOICES = {'use': '接入这个已有的管理层（设备上什么都不改）', 'start': '启动这个已有的管理层并接入',
           'replace': '删除它（连同它的状态目录、日志和令牌）后重新安装'}
TAKEN = ('设备上的 ~/neuxmind 已经存在，但不是安装脚本建的（或者是一个符号链接）。控制台不动它；'
         '请在设备上把它移走或改名后再安装')
FORWARDING_OFF = ('设备的 SSH 服务不允许 TCP 转发（AllowTcpForwarding），装好了平台也接不上管理层。'
                  '什么都没有安装；请让设备的管理员打开转发后再试')
PORT_BUSY = '设备本机的 {port} 端口已经有别的程序在监听。什么都没有安装；请换一个端口'
NOT_LINUX = '这台设备不是 Linux（报告的是 {os}），管理层只能装在 Linux 上。什么都没有安装'
NO_PYTHON = '设备上没有 Python 3.10 或更新的版本（找到：{found}）。什么都没有安装'
NO_VENV = ('设备上的 Python 不能建虚拟环境（缺少 venv / ensurepip；Ubuntu 上是 python3-venv 这个包）。'
           '什么都没有安装；装上之后再试（要管理员权限，控制台不会替你做）')
NO_TOOLS = '设备上缺少安装脚本要用的命令：{tools}。什么都没有安装'
NO_SPACE = '设备的主目录只剩 {free} MB，安装至少需要 {need} MB。什么都没有安装'
CHOICE_INVALID = '这台设备上的情况不允许这样处理（可选：{options}）'
REPLACE_UNCONFIRMED = '删除后重装会删掉设备上原有的 ~/neuxmind（状态目录、日志和令牌），需要先确认'
REAL_BODY = 'ros2'                        # issue #38: device_install.sh's adapter for the robot's own ROS 2


def body_differs(found, adapter):
    """Issue #38: the install on the robot was made for the other kind of body (simulated against real) than the
    one wanted now. Only a new install changes that, so such an install is never linked or started as it is."""
    if adapter is None:
        return False
    return ((found.get('adapter') or 'sim_quadruped') == REAL_BODY) != (adapter == REAL_BODY)


class Decision:
    def __init__(self, kind, steps=(), port=None, options=(), code=None, message='', notes=(), found=None):
        self.kind, self.steps, self.port, self.options = kind, tuple(steps), port, tuple(options)
        self.code, self.message, self.notes, self.found = code, message, list(notes), found or {}

    @property
    def creates_install(self):
        return 'install' in self.steps


def _refuse(code, message, found=None):
    return Decision('refuse', code=code, message=message, found=found)


def _python(text):
    try:
        major, minor = (int(part) for part in (text or '').split('.')[:2])
        return major, minor
    except ValueError:
        return None


def free_kb(df_line):
    """The available kilobytes of a ``df -Pk`` line, or None."""
    fields = (df_line or '').split()
    return int(fields[3]) if len(fields) >= 4 and _NUMBER.fullmatch(fields[3]) else None


def found_of(probe):
    """What the probe found at ~/neuxmind, as the page shows it."""
    port = probe.get('running_port', '')
    port = int(port) if _NUMBER.fullmatch(port) and int(port) <= 65535 else None
    return {'root': probe.get('root', ''), 'version': probe.get('version', '')[:40],
            'console_package': probe.get('console', '')[:80], 'running': probe.get('running') == 'yes',
            'running_port': port, 'adapter': probe.get('adapter', '')[:40]}


def _support(probe, steps):
    """Why the robot cannot run these steps, or None."""
    if not any(step in SCRIPTED for step in steps):
        return None
    if probe.get('os') != 'Linux':
        return _refuse('install_device_unsupported', NOT_LINUX.format(os=probe.get('os') or '未知'))
    missing = probe.get('missing', '').split()
    if missing:
        return _refuse('install_device_unsupported', NO_TOOLS.format(tools='、'.join(missing)))
    if 'install' not in steps:
        return None
    version = _python(probe.get('python'))
    if version is None or version < MIN_PYTHON:
        return _refuse('install_device_unsupported', NO_PYTHON.format(found=probe.get('python') or '无'))
    if probe.get('venv') != 'yes':
        return _refuse('install_device_unsupported', NO_VENV)
    free = free_kb(probe.get('df'))
    if free is not None and free < MIN_FREE_KB:
        return _refuse('install_no_space', NO_SPACE.format(free=free // 1024, need=MIN_FREE_KB // 1024))
    return None


def _existing(probe, package, port, existing, confirm, found, other_body=False):
    """The steps for a finished install that this console did not make from this package, or (issue #38,
    ``other_body``) that was made for the other kind of body: that one can only be replaced."""
    running = found['running']
    options = ['use'] if running and not other_body else []
    if not running and not other_body and found['version'] == package.runtime_version:
        options.append('start')
    options.append('replace')
    if existing is None:
        return Decision('choice', options=options, found=found, port=port)
    if existing not in options:
        return _refuse('install_choice_invalid', CHOICE_INVALID.format(options='、'.join(CHOICES[o] for o in options)), found)
    if existing == 'use':
        return Decision('steps', ['link'], found['running_port'] or port, found=found,
                        notes=['设备上原有的管理层保持原样，只是接入'])
    if existing == 'start':
        return Decision('steps', ['upload', 'start', 'link'], port, found=found,
                        notes=['用这个控制台带的安装脚本启动设备上原有的管理层'])
    if not confirm:
        return _refuse('install_replace_unconfirmed', REPLACE_UNCONFIRMED, found)
    return Decision('steps', ['upload', 'remove', 'install', 'start', 'link'], port, found=found,
                    notes=['删除设备上原有的 ~/neuxmind，然后重新安装'])


def _unfinished_elsewhere(port, existing, confirm, found):
    """An unfinished install that this package did not start: replacing it is the only way, and only when chosen."""
    if existing is None:
        return Decision('choice', options=['replace'], found=found, port=port)
    if existing != 'replace':
        return _refuse('install_choice_invalid', CHOICE_INVALID.format(options=CHOICES['replace']), found)
    if not confirm:
        return _refuse('install_replace_unconfirmed', REPLACE_UNCONFIRMED, found)
    return Decision('steps', FRESH, port, found=found, notes=['设备上那份没装完的 ~/neuxmind 由安装脚本先清掉，然后重新安装'])


def plan(probe, port_state, package, port, existing=None, confirm=False, adapter=None):
    """The ``Decision`` for this robot: steps to run, a choice to ask for, or a refusal. ``adapter`` (issue #38):
    the body the install is wanted for; None does not compare it with what is there."""
    found = found_of(probe)
    other_body = body_differs(found, adapter)
    found['other_body'] = other_body
    if port_state == 'forbidden':
        return _refuse('install_forwarding_off', FORWARDING_OFF, found)
    root = found['root']
    install_job = dev.job_state(probe.get('job_install'))
    if root in ('link', 'foreign'):
        return _refuse('install_target_taken', TAKEN, found)
    if root in ('none', 'empty'):
        decision = Decision('steps', FRESH, port, found=found)
    elif root == 'unfinished' and install_job[2]:
        decision = Decision('steps', ['wait_install', 'start', 'link'], port, found=found,
                            notes=['设备上这个安装包的安装还在进行，接着等它的结果'])
    elif root == 'unfinished' and install_job[0] is not None and install_job[1] is None:
        # This package's install job began here and never ended (the robot lost power, the job was killed): its
        # unfinished install. A job that did end leaves a finished install or none, so then someone else made it.
        decision = Decision('steps', FRESH, port, found=found, notes=['设备上有一份没装完的 ~/neuxmind，安装脚本会先把它清掉'])
    elif root == 'unfinished':
        decision = _unfinished_elsewhere(port, existing, confirm, found)
        if decision.kind != 'steps':
            return decision
    elif root == 'installed' and found['console_package'] == package.id and not other_body:
        if found['running']:
            decision = Decision('steps', ['link'], found['running_port'] or port, found=found,
                                notes=['设备上已经有这个控制台装好的同一个安装包，正在运行：直接接入'])
        else:
            decision = Decision('steps', ['upload', 'start', 'link'], port, found=found,
                                notes=['设备上已经有这个控制台装好的同一个安装包：启动并接入'])
    elif root == 'installed':
        decision = _existing(probe, package, port, existing, confirm, found, other_body)
        if decision.kind != 'steps':
            return decision
    else:
        return _refuse('install_probe_failed', '没有读懂设备上 ~/neuxmind 的情况（%s）' % (root or '空')[:40], found)
    refused = _support(probe, decision.steps)
    if refused is not None:
        return refused
    starts = 'start' in decision.steps
    freed = 'remove' in decision.steps and found['running'] and found['running_port'] == decision.port
    if starts and port_state == 'busy' and not freed:
        return _refuse('install_port_busy', PORT_BUSY.format(port=decision.port), found)
    return decision
