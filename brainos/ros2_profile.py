"""ROS 2 of a real machine, for its device profile (issue #27).

One product-owned, read-only command: no model or user text is ever added to it. It says which
ROS distributions are installed under /opt/ros, loads the environment of one (its ``setup.sh``:
the tools are not on the path otherwise) and asks the ``ros2`` command line tool for the nodes
that run, the topics and the services with their types. Actions are read from the topic list,
the way the ROS client library finds them: an action is the owner of a ``…/_action/feedback`` topic.

Why it is a command of its own and not part of the hardware scan (``hardware_scan``): it loads a
file from the device and runs a tool that joins the robot's ROS network, which the scan never does.
What it still never does: publish, call a service, send a goal, change a parameter, or start the
ros2 daemon (every call has ``--no-daemon``, so nothing is left running after the read). The
command writes no file itself. The ``ros2`` tool does what it does whenever it is run by hand: each
call leaves an empty log file under the account's ``~/.ros/log`` (ROS 2 Humble has no switch for
that in these calls), and its DDS layer may leave a port file in /dev/shm.

Nodes are only visible on the right domain. The robot's own nodes were started with settings the
SSH session does not have (ROS_DOMAIN_ID, the RMW in use, the DDS configuration), so the command
takes them from the environment of a running node (failing that, of any process with a ROS
environment, then of the SSH session itself): the variables named in ``ENVIRONMENT_NAMES`` and no
others, of which five are printed. With none of these the defaults are used, and the result says so.

``parse`` turns the output into the ``ros2`` part of the device profile. Every name and type is
checked against the grammar of ROS names before it is returned; what the tool printed besides
(warnings, an error) is only quoted, filtered, when a call failed.
"""
import re
import shlex

from .device_software import ROS1_DISTROS

TIMEOUT_SECONDS = 45
OUTPUT_LIMIT = 524288
SUPPORTED_DISTROS = ('humble',)
MAX_INSTALLED = 20
MAX_NODES = 500
MAX_TOPICS = 2000
MAX_SERVICES = 3000
MAX_ACTIONS = 500
# Lines of each listing the command sends (its ``head -n``): a listing this long was cut.
HEAD_LINES = {'nodes': 600, 'topics': 2400, 'services': 3200}
# Characters of a line the command sends (its ``cut -c``): a line this long lost its end.
LINE_WIDTH = {'nodes': 300, 'topics': 400, 'services': 400}
SOURCES = ('process', 'session')        # where the environment came from; anything else is 'default'
# What is read from the environment of a running ROS process. The first two say what runs; the
# rest decide which nodes a tool can see, and are set for the three ``ros2`` calls.
ENVIRONMENT_NAMES = ('ROS_DISTRO', 'ROS_VERSION', 'ROS_DOMAIN_ID', 'RMW_IMPLEMENTATION', 'ROS_LOCALHOST_ONLY',
                     'ROS_AUTOMATIC_DISCOVERY_RANGE', 'ROS_STATIC_PEERS', 'ROS_DISCOVERY_SERVER', 'CYCLONEDDS_URI',
                     'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE')
MARK = re.compile(r'^@@NX-ROS:([a-z_]+)@@$')

# POSIX sh; every probe tolerates a missing tool or directory. A name read from the device is used
# in a path only after it was checked to hold nothing but a-z, 0-9 and _. ``pick`` takes the ROS
# variables of the first process in a list whose environment can be read and has them: first a
# process that has the ROS 2 client library loaded (a node, whatever its command line looks like;
# not the ros2 tool's own daemon, which keeps the environment of whoever started it),
# otherwise any process with a ROS environment (a login shell has one too, often without the domain
# the robot's nodes use, which is why it comes second), and last the environment of this SSH session
# itself. ``val`` gives one variable of what was taken; it is printed with printf, because echo would
# interpret backslashes in it. When a running process names its distribution, no other one is loaded
# in its place: a robot that runs another distribution is not reported as the one that happens to be
# installed. Without ``timeout`` on the device the tool is not run at all. Each ``ros2`` call runs under ``timeout``: a tool that waits for
# a network that never answers is ended on the robot, and three of them stuck still end within
# TIMEOUT_SECONDS. ``--spin-time`` is how long each call listens for the other nodes.
ROS2_COMMAND = r'''export LC_ALL=C
T=; command -v timeout >/dev/null 2>&1 && T='timeout -k 1 10'
K='ROS_DISTRO|ROS_VERSION|ROS_DOMAIN_ID|RMW_IMPLEMENTATION|ROS_LOCALHOST_ONLY|ROS_AUTOMATIC_DISCOVERY_RANGE|ROS_STATIC_PEERS|ROS_DISCOVERY_SERVER|CYCLONEDDS_URI|FASTRTPS_DEFAULT_PROFILES_FILE|FASTDDS_DEFAULT_PROFILES_FILE'
val(){ printf '%s\n' "$E" | sed -n "s/^$1=//p" | head -n 1; }
echo '@@NX-ROS:install@@'
for d in /opt/ros/*/; do [ -f "${d}setup.sh" ] || continue; n=${d%/}; n=${n##*/}; case "$n" in *[!a-z0-9_]*) continue;; esac; a=0; [ -x "${d}bin/ros2" ] && a=1; b=0; [ -x "${d}bin/roscore" ] && b=1; echo "$n|$a|$b"; done
echo '@@NX-ROS:env@@'
E=; S=default
pick(){ for f in $1; do p=${f#/proc/}; p=${p%%/*}; [ "$p" = "$$" ] || [ "$p" = "$PPID" ] && continue; [ -n "$2" ] && grep -q -e 'ros2cli' -e '_ros2_daemon' "/proc/$p/cmdline" 2>/dev/null && continue; e=$(cat "/proc/$p/environ" 2>/dev/null | tr '\0' '\n' | grep -E "^($K)=" | cut -c1-2000); case "$e" in *ROS_DISTRO=*) E=$e; S=process; return 0;; esac; done; return 1; }
pick "$(grep -l 'librcl' /proc/[0-9]*/maps 2>/dev/null | head -n 60)" nodes || pick "$(grep -l 'ROS_DISTRO=' /proc/[0-9]*/environ 2>/dev/null | head -n 60)" || { e=$(env | grep -E "^($K)=" | cut -c1-2000); case "$e" in *ROS_DISTRO=*) E=$e; S=session;; esac; }
echo "source=$S"
printf '%s\n' "distro=$(val ROS_DISTRO)"
printf '%s\n' "version=$(val ROS_VERSION)"
printf '%s\n' "domain=$(val ROS_DOMAIN_ID)"
printf '%s\n' "rmw=$(val RMW_IMPLEMENTATION)"
printf '%s\n' "localhost=$(val ROS_LOCALHOST_ONLY)"
A=
for k in ROS_DOMAIN_ID RMW_IMPLEMENTATION ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE ROS_STATIC_PEERS ROS_DISCOVERY_SERVER CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE; do v=$(val "$k"); [ -n "$v" ] || continue; export "$k=$v"; A="$A $k"; done
echo "adopted=$A"
echo '@@NX-ROS:setup@@'
R=$(val ROS_DISTRO)
D=$R
case "$D" in *[!a-z0-9_]*) D=;; esac
[ -n "$D" ] && [ -f "/opt/ros/$D/setup.sh" ] || D=
[ -z "$R" ] && [ -x /opt/ros/humble/bin/ros2 ] && D=humble
[ -z "$R" ] && [ -z "$D" ] && for d in /opt/ros/*/; do [ -x "${d}bin/ros2" ] || continue; n=${d%/}; n=${n##*/}; case "$n" in *[!a-z0-9_]*) continue;; esac; D=$n; break; done
echo "distro=$D"
if [ -n "$D" ] && [ -x "/opt/ros/$D/bin/ros2" ] && [ -r "/opt/ros/$D/setup.sh" ]; then
. "/opt/ros/$D/setup.sh" >/dev/null 2>&1
echo "sourced=$?"
if [ -z "$T" ]; then echo 'cli=notimeout'
elif command -v ros2 >/dev/null 2>&1; then
echo 'cli=ok'
echo '@@NX-ROS:nodes@@'
out=$($T ros2 node list --no-daemon --spin-time 3 2>&1); echo "exit=$?"; printf '%s\n' "$out" | head -n 600 | cut -c1-300
echo '@@NX-ROS:topics@@'
out=$($T ros2 topic list -t --include-hidden-topics --no-daemon --spin-time 3 2>&1); echo "exit=$?"; printf '%s\n' "$out" | head -n 2400 | cut -c1-400
echo '@@NX-ROS:services@@'
out=$($T ros2 service list -t --no-daemon --spin-time 3 2>&1); echo "exit=$?"; printf '%s\n' "$out" | head -n 3200 | cut -c1-400
else echo 'cli=missing'; fi
fi
echo '@@NX-ROS:end@@'
'''

# What is sent over SSH: the script runs under sh whatever the account's login shell is.
ROS2_EXEC = 'sh -c ' + shlex.quote(ROS2_COMMAND)

DISTRO_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')
ROS_NAME = re.compile(r'(?:/[A-Za-z0-9_]+)+')                    # a fully qualified node, topic or service name
ROS_TYPE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z0-9_]+){1,3}')
RMW_NAME = re.compile(r'rmw_[a-z0-9_]{1,60}')
TYPED_LINE = re.compile(r'(\S+) \[([^\[\]]*)\]')                 # "/name [type, type]"
EXIT_CODE = re.compile(r'[0-9]{1,3}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
MAX_NAME = 255
MAX_TYPE = 200
MAX_DOMAIN_ID = 232                                              # the highest domain a DDS participant can use
TIMED_OUT = ('124', '137')                                       # ended by ``timeout``, or killed by it
TOOL_TOPICS = ('/parameter_events', '/rosout')                   # the listing tool's own: there on an empty network too
ACTION_TOPIC = '/_action/feedback'
ACTION_TYPE = '_FeedbackMessage'
LABELS = {'nodes': '节点', 'topics': '话题', 'services': '服务'}

# The ``ros2`` part of a profile that was never read: every field unknown.
NOT_READ = {'status': 'not_read', 'found': None, 'distro': None, 'generation': None, 'supported': None, 'installed': None,
            'environment': None, 'nodes': None, 'topics': None, 'services': None, 'actions': None, 'notes': [],
            'complete': False}


def sections(text):
    result, name = {}, None
    for line in str(text).splitlines():
        match = MARK.match(line.strip())
        if match:
            name = match.group(1)
            result.setdefault(name, [])
        elif name:
            result[name].append(line.rstrip('\r'))
    return result


def _pairs(lines):
    return dict(line.split('=', 1) for line in lines if re.match(r'^[a-z_]+=', line))


def _distro(value):
    value = (value or '').strip()
    return value if DISTRO_NAME.fullmatch(value) else None


def _name(value):
    return len(value) <= MAX_NAME and ROS_NAME.fullmatch(value) is not None


def _type(value):
    return len(value) <= MAX_TYPE and ROS_TYPE.fullmatch(value) is not None


def _hidden(name):
    """What the ros2 tool leaves out of its lists unless asked: a name with a part that starts with ``_``."""
    return any(part.startswith('_') for part in name.split('/'))


def _installed(s):
    rows = {}
    for line in s.get('install', []):
        parts = line.strip().split('|')
        name = _distro(parts[0])
        if len(parts) != 3 or not name or name in rows:
            continue
        rows[name] = 2 if parts[1] == '1' else 1 if parts[2] == '1' or name in ROS1_DISTROS else None
    return [{'name': name, 'generation': generation} for name, generation in sorted(rows.items())][:MAX_INSTALLED]


def _environment(env):
    domain = env.get('domain', '').strip()
    rmw = env.get('rmw', '').strip()
    localhost = env.get('localhost', '').strip()
    source = env.get('source', '').strip()
    return {'source': source if source in SOURCES else 'default',
            'domain_id': int(domain) if re.fullmatch(r'[0-9]{1,3}', domain) and int(domain) <= MAX_DOMAIN_ID else None,
            'rmw': rmw if RMW_NAME.fullmatch(rmw) else None,
            'localhost_only': {'0': False, '1': True}.get(localhost),
            'adopted': [name for name in env.get('adopted', '').split() if name in ENVIRONMENT_NAMES]}


def _generation(distro, installed, env, running):
    known = next((row['generation'] for row in installed if row['name'] == distro), None)
    if known:
        return known
    if running == distro and env.get('version', '').strip() in ('1', '2'):
        return int(env['version'].strip())
    return 1 if distro in ROS1_DISTROS else None


class _Listings:
    """The three ``ros2 ... list`` sections: their lines when the call succeeded, and why not otherwise."""

    def __init__(self, s, shown):
        self.s, self.shown = s, shown
        self.failures = {}              # reason -> the lists it cost
        self.cut = []                   # lists whose output reached the command's line limit

    def lines(self, name):
        """The lines of a listing, or None: a call that did not succeed says nothing about what is there."""
        if name not in self.s:
            return None
        code = _pairs(self.s[name][:1]).get('exit', '').strip()
        output = [line.strip() for line in self.s[name][1:] if line.strip()]
        if code == '0':
            if len(output) >= HEAD_LINES[name] or any(len(line) >= LINE_WIDTH[name] for line in self.s[name][1:]):
                self.cut.append(name)
            return output
        if code in TIMED_OUT:
            reason = '的命令在限定时间内没有返回，没有读到'
        elif EXIT_CODE.fullmatch(code):
            said = self.shown(output[-1] if output else '', 300)    # its last line: a traceback ends with the error
            reason = f'的命令失败（退出码 {code}），没有读到' + ('：' + said if said else '')
        else:
            reason = '的命令没有给出结果，没有读到'
        self.failures.setdefault(reason, []).append(LABELS[name])
        return None

    def notes(self):
        return [f"读取{'、'.join(labels)}{reason}。" for reason, labels in self.failures.items()]


def _typed(lines):
    """{name: types} of a ``-t`` listing. A line that is not a name with types is not kept (the tool
    prints warnings among them)."""
    rows = {}
    for line in lines:
        match = TYPED_LINE.fullmatch(line)
        if not match or not _name(match.group(1)):
            continue
        types = [part.strip() for part in match.group(2).split(',')]
        if all(_type(part) for part in types):
            rows.setdefault(match.group(1), types)
    return rows


def _rows(named):
    return [{'name': name, 'types': types} for name, types in sorted(named.items())]


def _actions(topics):
    """Actions as ``ros2 action list -t`` gives them: the owners of the feedback topics, with the action's type."""
    actions = {}
    for name, types in topics.items():
        if not name.endswith(ACTION_TOPIC) or name == ACTION_TOPIC:
            continue
        owned = [part[:-len(ACTION_TYPE)] for part in types if part.endswith(ACTION_TYPE)]
        if owned:
            actions[name[:-len(ACTION_TOPIC)]] = owned
    return actions


def _capped(rows, limit, label, notes):
    if rows is None or len(rows) <= limit:
        return rows, False
    notes.append(f'{label}超过 {limit} 个，只保留前 {limit} 个。')
    return rows[:limit], True


def _nodes(lines, notes):
    names = [line for line in lines if _name(line)]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        notes.append('有重名的节点：' + '、'.join(twice[:5]) + (' 等' if len(twice) > 5 else '') +
                     '。同名的节点会互相干扰（参数、服务和日志分不清是谁的）。')
    return [{'name': name} for name in sorted(set(names))]


def parse(text, clean=str, truncated=False):
    """Parse ``ROS2_COMMAND`` output into the ``ros2`` part of a device profile.

    A list that was not read is None, never empty: ``[]`` means the tool answered and nothing is
    there. ``clean`` hides connection credentials in the text that is quoted from the device;
    ``truncated`` says the output was cut at the transport's limit."""
    s = sections(text)

    def shown(value, limit):
        return CONTROL.sub(' ', clean(str(value))).strip()[:limit]

    env, setup = _pairs(s.get('env', [])), _pairs(s.get('setup', []))
    installed = _installed(s)
    loaded, running = _distro(setup.get('distro')), _distro(env.get('distro'))
    # What runs decides: the tool of an installed distribution may have listed the nodes of another one.
    distro = running or loaded or (installed[0]['name'] if installed else None)
    found = distro is not None
    generation = _generation(distro, installed, env, running) if found else None
    supported = distro in SUPPORTED_DISTROS if found else None
    environment = _environment(env)

    listings = _Listings(s, shown)
    node_lines, topic_lines, service_lines = (listings.lines(name) for name in ('nodes', 'topics', 'services'))
    notes = []          # what the fields do not say; that a distribution is unsupported is in ``supported``
    if found and generation != 1 and 'nodes' not in s:
        if setup.get('cli', '').strip() == 'notimeout':
            notes.append('设备上没有 timeout 命令，不能给 ros2 工具设时限，所以没有读取节点和话题。')
        elif running and not loaded:
            notes.append(f'正在运行的进程用的是 {running}，但 /opt/ros 下没有它的安装（可能是从源码构建的），没有读取节点和话题。')
        elif loaded:
            notes.append(f'/opt/ros/{loaded} 里没有可用的 ros2 命令行工具（没有安装 ros2cli），没有读取节点和话题。')
        else:
            notes.append('没有能加载的 ROS 2 环境，没有读取节点和话题。')
    elif setup.get('sourced', '0').strip() != '0':
        notes.append(f'加载 /opt/ros/{loaded}/setup.sh 时报错，下面的清单可能不全。')
    if running and loaded and running != loaded:
        notes.append(f'正在运行的进程用的是 {running}，下面的清单是用 /opt/ros/{loaded} 的工具读的，可能不全。')
    notes += listings.notes()

    nodes = _nodes(node_lines, notes) if node_lines is not None else None
    every_topic = _typed(topic_lines) if topic_lines is not None else None
    topics = _rows({name: types for name, types in every_topic.items() if not _hidden(name)}) if every_topic is not None else None
    actions = _rows(_actions(every_topic)) if every_topic is not None else None
    services = _rows(_typed(service_lines)) if service_lines is not None else None
    limited = bool(listings.cut)
    for name in listings.cut:
        notes.append(f'{LABELS[name]}的清单太长或有一行太长，设备送回的不是全部（最多 {HEAD_LINES[name]} 行，每行 {LINE_WIDTH[name]} 个字符）。')
    (nodes, a), (topics, b), (services, c), (actions, d) = (
        _capped(nodes, MAX_NODES, '节点', notes), _capped(topics, MAX_TOPICS, '话题', notes),
        _capped(services, MAX_SERVICES, '服务', notes), _capped(actions, MAX_ACTIONS, '动作', notes))
    limited = limited or a or b or c or d
    if nodes == [] and any(row['name'] not in TOOL_TOPICS for row in topics or []):
        notes.append('读到了话题，但没有读到节点。常见原因是 SSH 账号和运行节点的账号不同：DDS 的共享内存不能跨账号，节点名传不过来。')
    if environment['source'] == 'default' and nodes is not None:
        notes.append('没有从正在运行的 ROS 进程读到环境变量（没有 ROS 进程在运行，或它们由别的账号运行），这次按默认设置读取。'
                     '机器人如果设置了 ROS_DOMAIN_ID 或自己的 DDS 配置，下面的清单会不全。')
    complete = 'end' in s and not truncated and not limited
    if 'end' not in s or truncated:
        notes.append('ROS 2 的输出不完整（命令超时或输出被截断），清单可能缺少一部分。')
    if not found:
        status = 'absent'
    elif not supported:
        status = 'unsupported'
    else:
        status = 'ok' if complete and None not in (nodes, topics, services) else 'incomplete'
    return {'status': status, 'found': found, 'distro': distro, 'generation': generation, 'supported': supported,
            'installed': installed, 'environment': environment, 'nodes': nodes, 'topics': topics, 'services': services,
            'actions': actions, 'notes': notes, 'complete': complete}
