"""Running software of a real machine, for its live status (issue #8).

One product-owned, read-only command: no model or user text is ever added to it. It lists the
systemd services that run, fail or keep restarting, the Docker containers, and the processes,
which it samples twice a second apart to tell how much CPU each one uses now.

``parse`` turns the output into rows. A process's command line is only used here, to recognise
robot middleware (ROS nodes, launches, agents) and to name a script; it is never returned,
because command lines carry credentials often enough.
"""
import math
import re
import shlex

TIMEOUT_SECONDS = 15
OUTPUT_LIMIT = 262144
MAX_SERVICES = 200
MAX_CONTAINERS = 60
MAX_MIDDLEWARE = 120
MAX_PROCESSES = 3000            # lines of each process listing the command sends (its ``head -n``)
TOP_BY_CPU = 10
TOP_BY_MEMORY = 5
MARK = re.compile(r'^@@NX-SW:([a-z_]+)@@$')

# POSIX sh; every probe tolerates a missing tool. ``sample`` prints the uptime and, for every
# process, its pid, state, user and system CPU ticks and start time (the name, which may hold
# spaces and brackets, is cut out of /proc/<pid>/stat first). systemctl and docker ask a daemon,
# which can be stuck: they run under ``timeout``, so that a read which gave up leaves nothing
# running on the robot. Three of them stuck still end within TIMEOUT_SECONDS.
SOFTWARE_COMMAND = r'''export LC_ALL=C
T=; command -v timeout >/dev/null 2>&1 && T='timeout 4'
sample(){ cat /proc/uptime 2>/dev/null; cat /proc/[0-9]*/stat 2>/dev/null | sed 's/^\([0-9]*\) (.*) \([A-Za-z] \)/\1 \2/' | cut -d' ' -f1,2,13,14,21 | head -n 3000; }
echo '@@NX-SW:clock@@'
echo "tick=$(getconf CLK_TCK 2>/dev/null)"
echo "cores=$(nproc 2>/dev/null)"
echo '@@NX-SW:sample_a@@'
sample
echo '@@NX-SW:services@@'
$T systemctl list-units --type=service --state=running,failed,activating,deactivating,reloading --no-legend --plain --no-pager 2>/dev/null | head -n 300 | cut -c1-300
echo '@@NX-SW:systemd@@'
$T systemctl list-units --type=service --no-legend --plain --no-pager >/dev/null 2>&1 && echo ok
echo '@@NX-SW:containers@@'
if command -v docker >/dev/null 2>&1; then out=$($T docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>&1); echo "exit=$?"; printf '%s\n' "$out" | head -n 80 | cut -c1-400; else echo 'exit=missing'; fi
echo '@@NX-SW:processes@@'
ps -eo pid=,ppid=,user=,rss=,comm= 2>/dev/null | head -n 3000
echo '@@NX-SW:arguments@@'
ps -ww -eo pid=,args= 2>/dev/null | cut -c1-600 | head -n 3000
sleep 1
echo '@@NX-SW:sample_b@@'
sample
echo '@@NX-SW:end@@'
'''

# What is sent over SSH: the script runs under sh whatever the account's login shell is.
SOFTWARE_EXEC = 'sh -c ' + shlex.quote(SOFTWARE_COMMAND)

UNIT_NAME = re.compile(r'[A-Za-z0-9_.@:\\-]+\.service')
UNIT_STATE = re.compile(r'[a-z-]{1,32}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
PROCESS_STATES = {'R': 'running', 'S': 'running', 'D': 'running', 'I': 'running', 'T': 'stopped', 't': 'stopped',
                  'Z': 'zombie', 'X': 'zombie'}
INTERPRETER = re.compile(r'(?:python[\d.]*|bash|sh|dash|zsh|node|nodejs|perl|ruby)')
ROS1_DISTROS = {'boxturtle', 'cturtle', 'diamondback', 'electric', 'fuerte', 'groovy', 'hydro', 'indigo', 'jade',
                'kinetic', 'lunar', 'melodic', 'noetic'}
# <prefix>/lib/<package>/<executable>: where ROS installs the programs of a package.
ROS_LIB = re.compile(r'/opt/ros/([a-z0-9_]+)/lib/([^/]+)/([^/]+)$')
WORKSPACE_LIB = re.compile(r'/(install|devel)/(?:[^/]+/)?lib/([^/]+)/([^/]+)$')
ROS2_NODE = re.compile(r'(?:^|\s)__node:=(\S+)')
ROS2_NAMESPACE = re.compile(r'(?:^|\s)__ns:=(\S+)')
ROS1_NAME = re.compile(r'(?:^|\s)__name:=(\S+)')
# Middleware that is not a ROS node itself, by program name.
AGENTS = {'MicroXRCEAgent': 'Micro XRCE-DDS Agent', 'micro_ros_agent': 'micro-ROS Agent', 'zenohd': 'Zenoh 路由',
          'zenoh-bridge-ros2dds': 'Zenoh ROS 2 桥', 'zenoh-bridge-dds': 'Zenoh DDS 桥', 'fastdds': 'Fast DDS 发现服务',
          'fast-discovery-server': 'Fast DDS 发现服务', 'mosquitto': 'MQTT 代理（Mosquitto）', 'mavlink-routerd': 'MAVLink 路由'}
NODE_KINDS = ('ros1_node', 'ros2_node')


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


def _int(value, low=0, high=2 ** 53):
    """A whole number within bounds, or None: the device's output is not trusted to be numeric."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pairs(lines):
    return dict(line.split('=', 1) for line in lines if re.match(r'^[a-z_]+=', line))


def _base(path):
    return path.rstrip('/').rsplit('/', 1)[-1]


def _services(s, text):
    if 'ok' not in [line.strip() for line in s.get('systemd', [])]:
        return None                 # no usable systemd: unknown, which is not the same as "nothing runs"
    rows = []
    for line in s.get('services', []):
        parts = line.split(None, 4)
        if len(parts) < 4 or not UNIT_NAME.fullmatch(parts[0]) or not all(UNIT_STATE.fullmatch(x) for x in parts[1:4]):
            continue
        rows.append({'name': text(parts[0], 128), 'description': text(parts[4] if len(parts) > 4 else '', 200),
                     'state': _service_state(parts[2], parts[3]), 'watched': False})
    return rows


def _service_state(active, sub):
    if 'failed' in (active, sub):
        return 'failed'
    if sub == 'auto-restart':
        return 'restarting'         # systemd keeps starting it again: it does not stay up
    if active in ('activating', 'deactivating', 'reloading'):
        return active
    if active == 'active':
        return 'running' if sub == 'running' else 'active'
    return 'unknown'


def _container_state(status):
    low = status.lower()
    if low.startswith('up'):
        return 'paused' if '(paused)' in low else 'unhealthy' if '(unhealthy)' in low else 'running'
    exited = re.match(r'exited \((-?\d+)\)', low)
    if exited:
        return 'exited' if exited.group(1) == '0' else 'exited_error'
    for prefix, state in (('restarting', 'restarting'), ('created', 'created'), ('dead', 'dead'), ('removal', 'removing')):
        if low.startswith(prefix):
            return state
    return 'unknown'


def _containers(s, text):
    lines = s.get('containers', [])
    code = _pairs(lines[:1]).get('exit', '')
    output = [line for line in lines[1:] if line.strip()]
    if code == '0':
        items = []
        for line in output:
            parts = line.split('|', 2)
            if len(parts) == 3 and parts[0].strip():
                items.append({'name': text(parts[0], 128), 'image': text(parts[1], 200),
                              'state': _container_state(parts[2].strip()), 'status': text(parts[2], 80)})
        return {'state': 'ok', 'items': items}
    said = ' '.join(output).lower()
    # Not being allowed to ask, or the daemon being down, says nothing about which containers exist.
    state = ('missing' if code == 'missing' else 'timeout' if code in ('124', '137') else
             'denied' if 'permission denied' in said else 'stopped' if 'cannot connect' in said else 'failed')
    return {'state': state, 'items': []}


def _sample(lines):
    """(uptime in seconds, {pid: (state letter, CPU ticks used so far, start time in ticks)})."""
    uptime, rows = None, {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and uptime is None:
            uptime = _float(parts[0])
        elif len(parts) == 5:
            pid, user, system, start = (_int(parts[0], 1), _int(parts[2]), _int(parts[3]), _int(parts[4]))
            if None not in (pid, user, system, start) and re.fullmatch(r'[A-Za-z]', parts[1]):
                rows[pid] = (parts[1], user + system, start)
    return uptime, rows


def _listing(s):
    listed = {}
    for line in s.get('processes', []):
        parts = line.split(None, 4)
        pid, parent = (_int(parts[0], 1), _int(parts[1])) if len(parts) == 5 else (None, None)
        if pid is not None and parent is not None:
            listed[pid] = {'pid': pid, 'parent': parent, 'user': parts[2], 'memory_kb': _int(parts[3]),
                           'comm': parts[4].strip().removesuffix(' <defunct>'), 'args': None}
    for line in s.get('arguments', []):
        parts = line.split(None, 1)
        pid = _int(parts[0], 1) if len(parts) == 2 else None
        if pid in listed:
            listed[pid]['args'] = parts[1].strip()
    return listed


def _is_kernel_thread(process):
    # Children of kthreadd, shown by ps as "[name]". Inside a container pid 2 can be an ordinary program.
    return 2 in (process['pid'], process['parent']) and (process['args'] is None or process['args'].startswith('['))


def _is_this_command(process):
    # The shell that runs SOFTWARE_COMMAND carries the script, and so its section markers, on its command line.
    return '@@NX-SW:' in (process['args'] or '')


def _measured(s, listing):
    """Every user-space process with its state, run time and the CPU it used between the two samples."""
    # Linux reports process times to user space in 1/100 s whatever the kernel's own tick is: that is
    # what a device without getconf (busybox) gets.
    tick = _int(_pairs(s.get('clock', [])).get('tick'), 1, 100000) or 100
    began, first = _sample(s.get('sample_a', []))
    ended, second = _sample(s.get('sample_b', []))
    interval = ended - began if None not in (began, ended) and ended - began >= 0.2 else None
    processes = []
    for process in listing.values():
        if _is_kernel_thread(process) or _is_this_command(process) or (second and process['pid'] not in second):
            continue                # the last: gone since it was listed, like this command's own ps
        letter, ticks, start = second.get(process['pid'], ('?', None, None))
        before = first.get(process['pid'])
        same = before is not None and before[2] == start          # a pid can be reused within the second
        processes.append({**process, 'state': PROCESS_STATES.get(letter, 'unknown'),
                          'elapsed_seconds': max(0, int(ended - start / tick)) if tick and start is not None and ended is not None else None,
                          'cpu_percent': round(max(0, ticks - before[1]) / tick / interval * 100, 1) if tick and interval and same else None})
    return processes


def _program_tokens(args):
    """The command line as words, without the interpreter that runs a script (``python3 /opt/ros/.../ros2 launch``)."""
    tokens = args.split() if args and not args.startswith('[') else []
    if len(tokens) > 1 and INTERPRETER.fullmatch(_base(tokens[0])) and not tokens[1].startswith('-'):
        return tokens[1:]
    return tokens


def _label(process):
    """A name for the process list: the whole program name (comm stops at 15 characters) and the script it runs."""
    tokens = process['args'].split() if process['args'] and not process['args'].startswith('[') else []
    if not tokens:
        return process['comm']
    # "-bash" is a login shell, "@dbus-daemon" and "nginx:" are how some daemons rewrite their own name.
    program = _base(tokens[0]).lstrip('-@').rstrip(':') or process['comm']
    if INTERPRETER.fullmatch(program) and len(tokens) > 1:
        script = tokens[2] if tokens[1] == '-m' and len(tokens) > 2 else '' if tokens[1].startswith('-') else _base(tokens[1])
        return f'{program} · {script}' if script else program
    return program


def _words(tokens, limit=2):
    """The first plain words of a command line: package and file names, never options or assignments."""
    words = []
    for token in tokens:
        if token.startswith('-') or ':=' in token or len(words) == limit:
            break
        words.append(_base(token))
    return ' '.join(words)


def _installed(token):
    """(ROS generation, package, executable) if the path is a ROS package's program, else None."""
    ros = ROS_LIB.search(token)
    if ros:
        return ('ros1' if ros.group(1) in ROS1_DISTROS else 'ros2'), ros.group(2), ros.group(3)
    workspace = WORKSPACE_LIB.search(token)
    if workspace:                   # catkin's devel space is ROS 1; install/ is what colcon (ROS 2) builds
        return ('ros1' if workspace.group(1) == 'devel' else 'ros2'), workspace.group(2), workspace.group(3)
    return None


def _middleware(process):
    """What robot middleware a process is: {'kind', 'name', 'package', 'program'}, or None."""
    args = process['args'] or ''
    tokens = _program_tokens(args)
    if not tokens:
        return None
    program = _base(tokens[0])

    def row(kind, name, package=''):
        return {'kind': kind, 'name': name, 'package': package, 'program': program}

    if 'ros2cli.daemon' in args or program == '_ros2_daemon':
        return row('ros2_daemon', 'ros2 daemon')
    if program == 'ros2':
        verb, rest = (tokens[1] if len(tokens) > 1 else ''), tokens[2:]
        if verb == 'launch':
            return row('ros2_launch', _words(rest) or 'ros2 launch')
        return row('ros2_daemon', 'ros2 daemon') if verb == 'daemon' else row('ros2_tool', ' '.join(x for x in ('ros2', verb, _words(rest)) if x))
    if program in ('roscore', 'rosmaster'):
        return row('ros1_master', program)
    if program == 'roslaunch':
        return row('ros1_launch', _words(tokens[1:]) or 'roslaunch')
    if program in AGENTS:
        return row('agent', AGENTS[program])
    installed = _installed(tokens[0])
    package = installed[1] if installed else ''
    ros1_name = ROS1_NAME.search(args)
    if ros1_name:
        return row('ros1_node', ros1_name.group(1), package)
    ros2_name, namespace = ROS2_NODE.search(args), ROS2_NAMESPACE.search(args)
    if ros2_name or '--ros-args' in tokens:
        name = ros2_name.group(1) if ros2_name else program
        return row('ros2_node', namespace.group(1).rstrip('/') + '/' + name if namespace and ros2_name else name, package)
    if installed:
        return row(installed[0] + '_node', program, package)
    return None


def _rows(processes, text):
    """(middleware rows, the busiest processes)."""
    figures = ('pid', 'user', 'state', 'elapsed_seconds', 'cpu_percent', 'memory_kb')
    found = [(process, _middleware(process)) for process in processes]
    nodes_of = {process['parent'] for process, kind in found if kind and kind['kind'] in NODE_KINDS}
    middleware = []
    for process, kind in found:
        # `ros2 run` stays as the parent of the node it started: one row for the node is enough.
        if kind and not (kind['name'].startswith('ros2 run') and process['pid'] in nodes_of):
            middleware.append({**{key: process[key] for key in figures}, 'user': text(process['user'], 32), 'kind': kind['kind'],
                               'name': text(kind['name'], 160), 'package': text(kind['package'], 80), 'program': text(kind['program'], 80)})
    by_cpu = sorted(processes, key=lambda p: (-1 if p['cpu_percent'] is None else p['cpu_percent'], p['memory_kb'] or 0), reverse=True)
    by_memory = sorted(processes, key=lambda p: p['memory_kb'] or 0, reverse=True)
    busiest = by_cpu[:TOP_BY_CPU]
    busiest += [p for p in by_memory[:TOP_BY_MEMORY] if p not in busiest]
    top = [{**{key: p[key] for key in figures}, 'user': text(p['user'], 32), 'name': text(_label(p), 160)} for p in busiest]
    return middleware, top


def parse(text, clean=str):
    """Parse ``SOFTWARE_COMMAND`` output. Missing sections are reported as unknown (None), never as empty.

    ``clean`` hides connection credentials in every piece of text that is returned; numbers are
    read from the unfiltered output, so that a short password cannot corrupt them."""
    s = sections(text)

    def shown(value, limit):
        return CONTROL.sub(' ', clean(str(value))).strip()[:limit]

    notes = []
    services = _services(s, shown)
    if services is not None and len(services) > MAX_SERVICES:
        notes.append(f'系统服务超过 {MAX_SERVICES} 个，只显示前 {MAX_SERVICES} 个。')
        services = services[:MAX_SERVICES]
    containers = _containers(s, shown)
    if len(containers['items']) > MAX_CONTAINERS:
        notes.append(f'容器超过 {MAX_CONTAINERS} 个，只显示前 {MAX_CONTAINERS} 个。')
        containers = {**containers, 'items': containers['items'][:MAX_CONTAINERS]}
    listing = _listing(s)
    if len(listing) >= MAX_PROCESSES:
        notes.append(f'进程超过 {MAX_PROCESSES} 个，进程列表不完整。')
    processes = _measured(s, listing)
    middleware, top = _rows(processes, shown) if processes else (None, None)
    if middleware is not None and len(middleware) > MAX_MIDDLEWARE:
        notes.append(f'机器人中间件进程超过 {MAX_MIDDLEWARE} 个，只显示前 {MAX_MIDDLEWARE} 个。')
        middleware = middleware[:MAX_MIDDLEWARE]
    complete = 'end' in s
    if not complete:
        notes.append('软件状态的输出不完整（命令超时或输出被截断），列表可能缺少一部分。')
    return {'services': services, 'containers': containers, 'middleware': middleware, 'top': top,
            'process_count': len(processes) if processes else None, 'cores': _int(_pairs(s.get('clock', [])).get('cores'), 1, 4096),
            'notes': notes, 'complete': complete}


def mark_watched(software, watched):
    """The software with the user's watched services marked. One that is not in the list (it is not
    running) is added with the state ``systemctl is-active`` gave for it. Returns a new dict."""
    rows = [dict(row) for row in software['services'] or []]
    for service in watched:
        row = next((row for row in rows if row['name'] in (service['name'], service['name'] + '.service')), None)
        if row:
            row['watched'] = True
        else:
            rows.append({'name': service['name'], 'description': '', 'state': service['state'], 'watched': True})
    return {**software, 'services': rows if rows or software['services'] is not None else None}
