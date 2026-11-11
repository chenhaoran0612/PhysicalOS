"""Install a local skill folder (``skill.package.v1``) on a robot (INTERFACES §4.5).

Flow, as the user sees it:

1. choose a local folder → :func:`preview` reads it (size and path limits,
   symbolic links are never followed), validates it with the spec and runs
   ``check_compatibility`` against the robot's live ``/v2/manifest``;
2. the result is explained in plain words (missing capabilities, parameters
   that do not meet the skill's constraints);
3. only a compatible package can be confirmed → :func:`install` reads the
   folder again, insists on the same content digest the user saw, and sends
   ``POST /v2/skills/install`` with an idempotent ``request_id``.

The folder layout and exclusions follow ``sdk/neuxmind/package.py`` (the SDK
and the runtime use the same limits); the runtime stays the final authority.
"""
import hashlib
import json
import os
import stat
from pathlib import PurePosixPath

from . import connector, robot_caps
from .contracts import required_text
from .spec_bridge import capabilities as spec_caps, manifest as spec_manifest

MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1536 * 1024
MAX_SKILL_JSON_BYTES = 256 * 1024
MAX_ENTRIES = 2000          # directory entries looked at, shipped or not
MAX_DEPTH = 8
EXCLUDED_DIRS = {'tests', '__pycache__', 'node_modules'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo'}


# --------------------------------------------------------------- reading
def folder_path(value, bundled=False):
    """Validate the folder path typed or chosen by the user.

    ``bundled=True`` is for the app's own example skills: their install location
    may sit under a hidden directory (e.g. a developer checkout), so only the
    hidden-directory rule is relaxed; links and file checks stay the same."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请选择技能文件夹')
    path = value.strip()
    if len(path) > 1024 or '\x00' in path:
        raise ValueError('文件夹路径无效')
    if not os.path.isabs(path):
        raise ValueError('请使用完整路径，例如 /Users/you/skills/patrol')
    path = os.path.normpath(path)
    parts = [x for x in path.split(os.sep) if x]
    if not bundled and any(x.startswith('.') for x in parts):
        raise ValueError('不读取隐藏目录中的文件夹（路径中有以“.”开头的部分）')
    return path


def _open_dir(name, dir_fd=None):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=dir_fd)


def _open_folder(path):
    """Open the chosen folder one path component at a time with ``O_NOFOLLOW``.

    A symbolic link anywhere on the way — an ancestor such as macOS ``/tmp``,
    or the folder itself — is refused, and so is anything that is not a
    directory. Opening relative to the parent fd leaves no gap in which a
    component could be swapped for a link after it was checked.
    """
    fd = os.open('/', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    walked = ''
    try:
        for part in [x for x in path.split(os.sep) if x]:
            walked += os.sep + part
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise ValueError('找不到该文件夹，请检查路径') from None
            except OSError:
                raise ValueError(f'无法读取 {walked}') from None
            if stat.S_ISLNK(info.st_mode):
                if walked == path:
                    raise ValueError('所选路径是符号链接；出于安全原因不跟随，请选择实际的文件夹')
                raise ValueError(f'路径中的 {walked} 是符号链接；出于安全原因不跟随，请填写不经过链接的实际路径')
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('所选路径不是文件夹' if walked == path else f'{walked} 不是文件夹')
            try:
                child = _open_dir(part, dir_fd=fd)
            except OSError:
                raise ValueError(f'无法打开 {walked}（可能是符号链接或没有读取权限）') from None
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(name, dir_fd, limit, label):
    """Read one regular file: no symbolic link, no special file, no hard link."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise ValueError(f'找不到 {label}') from None
    except OSError:
        raise ValueError(f'无法读取 {label}') from None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f'{label} 是符号链接；出于安全原因不跟随，请换成实际文件')
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'{label} 不是普通文件（设备文件、管道等不能放进技能包）')
    try:
        # O_NONBLOCK: a file swapped for a FIFO after the check cannot block the read.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=dir_fd)
    except OSError:
        raise ValueError(f'无法读取 {label}（可能是符号链接或没有读取权限）') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'{label} 不是普通文件（设备文件、管道等不能放进技能包）')
        if info.st_nlink > 1:
            # A hard link can pull a file from outside the folder into the package.
            raise ValueError(f'{label} 是硬链接（还有 {info.st_nlink - 1} 个其他路径指向同一文件）；'
                             '为避免把包外文件带进技能包，请复制成独立文件')
        if info.st_size > limit:
            raise ValueError(f'{label} 有 {info.st_size} 字节，超过上限 {limit}')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError(f'{label} 超过上限 {limit} 字节')
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b''.join(chunks).decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError(f'{label} 不是 UTF-8 文本；技能包只能包含文本文件') from None


def read_folder(value, bundled=False):
    """Read a skill folder into an installable package (``skill.json`` + ``files``).

    The folder is reached component by component and every directory and file
    inside it is opened relative to its parent with ``O_NOFOLLOW``: a symbolic
    link anywhere on the path or in the shipped tree, a special file or a hard
    link is refused.
    """
    path = folder_path(value, bundled)
    root_fd = _open_folder(path)

    files, sizes = {}, {}
    state = {'entries': 0, 'total': 0}

    def walk(dir_fd, prefix, depth):
        with os.scandir(dir_fd) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            state['entries'] += 1
            if state['entries'] > MAX_ENTRIES:
                raise ValueError(f'文件夹内条目超过 {MAX_ENTRIES} 个；请只选择技能本身的文件夹')
            rel = f'{prefix}{entry.name}'
            if entry.name.startswith('.'):
                continue                                 # hidden files and folders are never shipped
            if entry.is_symlink():
                raise ValueError(f'技能文件夹包含符号链接 {rel}；出于安全原因不跟随，请换成实际文件')
            if entry.is_dir(follow_symlinks=False):
                if entry.name in EXCLUDED_DIRS:
                    continue
                if depth >= MAX_DEPTH:
                    raise ValueError(f'目录层级超过 {MAX_DEPTH} 层：{rel}')
                try:
                    child = _open_dir(entry.name, dir_fd=dir_fd)
                except OSError:
                    raise ValueError(f'无法打开目录 {rel}') from None
                try:
                    walk(child, rel + '/', depth + 1)
                finally:
                    os.close(child)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(f'{rel} 不是普通文件（设备文件、管道等不能放进技能包）')
            if rel == 'skill.json' or PurePosixPath(rel).suffix in EXCLUDED_SUFFIXES:
                continue
            if len(files) >= MAX_FILES:
                raise ValueError(f'技能文件最多 {MAX_FILES} 个')
            text = _read_file(entry.name, dir_fd, MAX_FILE_BYTES, rel)
            size = len(text.encode('utf-8'))
            state['total'] += size
            if state['total'] > MAX_TOTAL_BYTES:
                raise ValueError(f'技能包超过总大小上限 {MAX_TOTAL_BYTES} 字节')
            files[rel] = text
            sizes[rel] = size

    try:
        try:
            os.stat('skill.json', dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise ValueError('该文件夹中没有 skill.json，不是技能包') from None
        except OSError:
            pass                                   # reported by _read_file below
        text = _read_file('skill.json', root_fd, MAX_SKILL_JSON_BYTES, 'skill.json')
        walk(root_fd, '', 0)
    finally:
        os.close(root_fd)
    try:
        meta = json.loads(text)
    except ValueError as exc:
        raise ValueError(f'skill.json 不是合法 JSON：{exc}') from None
    if not isinstance(meta, dict):
        raise ValueError('skill.json 须为 JSON 对象')
    meta.pop('files', None)
    return {**meta, 'files': files}, {'path': path, 'sizes': sizes, 'total_bytes': state['total']}


# ------------------------------------------------------------ validation
def validate(package):
    """Spec validation plus the checks the runtime will apply (entry file present,
    parameter defaults match their type). Returns the normalised metadata."""
    meta = {k: v for k, v in package.items() if k != 'files'}
    try:
        skill = spec_manifest.validate_skill_package(meta)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'skill.json 无效：{exc}') from None
    module, sep, attr = skill['entry'].partition(':')
    if not sep or not module or not attr.isidentifier():
        raise ValueError('entry 须为 “<文件>.py:<函数或类名>”，例如 skill.py:run')
    entry_file = module if module.endswith('.py') else module.replace('.', '/') + '.py'
    if not package.get('files'):
        raise ValueError('技能文件夹里除 skill.json 外没有可安装的文件')
    if entry_file not in package['files']:
        raise ValueError(f'入口文件 {entry_file} 不在技能文件夹中（entry={skill["entry"]}）')
    for decl in skill['parameters']:
        if 'default' in decl:
            field = spec_caps.Field(name=str(decl.get('name')), type=decl.get('type'), length=decl.get('length'),
                                    choices=tuple(decl.get('choices', ())))
            try:
                spec_caps.check_field(field, decl['default'], where='parameters')
            except ValueError as exc:
                raise ValueError(f'参数 {decl["name"]} 的默认值与类型不符：{exc}') from None
    return skill


def package_digest(package):
    return hashlib.sha256(json.dumps(package, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


# --------------------------------------------------------- compatibility
def _cap_label(cap_id):
    cap = spec_caps.CAPABILITIES.get(cap_id)
    return f'「{cap.title}」（{cap_id}）' if cap else cap_id


def _param_unit(cap_id, name):
    cap = spec_caps.CAPABILITIES.get(cap_id)
    for f in (cap.params if cap else ()):
        if f.name == name:
            return f.unit
    return ''


def _with_unit(value, unit):
    return f'{value} {unit}'.strip() if isinstance(value, (int, float)) and not isinstance(value, bool) else json.dumps(value, ensure_ascii=False)


def _explain_rule(cap_id, name, rule, value):
    """Plain-words reason one constraint fails (mirrors spec.check_compatibility), or None."""
    label = f'{_cap_label(cap_id)}的参数 {name}'
    unit = _param_unit(cap_id, name)
    if value is None:
        return f'{label}：技能对它有要求，但这台机器人没有声明该参数'
    if not isinstance(rule, dict):
        return None
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    needs = []
    if 'min' in rule and not (numeric and value >= rule['min']):
        needs.append(f'至少 {_with_unit(rule["min"], unit)}')
    if 'max' in rule and not (numeric and value <= rule['max']):
        needs.append(f'至多 {_with_unit(rule["max"], unit)}')
    if 'equals' in rule and value != rule['equals']:
        needs.append(f'等于 {_with_unit(rule["equals"], unit)}')
    if not needs:
        return None
    return f'{label}：技能需要{"、".join(needs)}，这台机器人声明为 {_with_unit(value, unit)}'


def explain(skill, manifest):
    """``check_compatibility`` (the single rule) plus readable reasons."""
    result = dict(spec_manifest.check_compatibility(skill, manifest))
    have = {c['id']: c for c in manifest.get('capabilities', []) if isinstance(c, dict)}
    missing = [{'id': c, 'title': robot_caps.capability_row(c)['title'],
                'text': f'缺少能力{_cap_label(c)}：这台机器人没有声明它'} for c in result['missing']]
    violations = []
    for cap_id, rules in (skill.get('constraints') or {}).items():
        cap = have.get(cap_id)
        if not cap or not isinstance(rules, dict):
            continue
        for name, rule in rules.items():
            text = _explain_rule(cap_id, name, rule, (cap.get('params') or {}).get(name))
            if text:
                violations.append(text)
    if len(violations) < len(result['violations']):
        violations.extend(v for v in result['violations'] if v not in violations)   # never hide a spec verdict
    notes = [f'可选能力{_cap_label(c)}这台机器人没有，技能会跳过相关功能' for c in result['optional_missing']]
    if manifest.get('simulated') is True:
        notes.append('这是仿真本体：技能运行结果只能说明协议与逻辑，不能作为现场证据')
    result.update(missing_detail=missing, violation_texts=violations, notes=notes,
                  summary='兼容，可以安装' if result['compatible'] else '不兼容，不能安装')
    return result


# ------------------------------------------------------------ operations
def _robot_for(platform, p):
    robot = platform.store.get('robots', p.get('robot_id'))
    if p.get('project_id') and robot.get('project_id') != p['project_id']:
        raise ValueError('该机器人不属于当前项目')
    if robot.get('status') != 'connected':
        raise ValueError('请先对这台机器人做连接检查')
    return robot


def _skill_view(skill):
    def caps(items):
        return [{'id': r['id'], 'title': robot_caps.capability_row(r['id'])['title']} for r in items]
    return {'id': skill['id'], 'version': skill['version'], 'title': skill['title'],
            'description': skill['description'], 'summary': robot_caps.plain_text(skill['description']), 'entry': skill['entry'], 'timeout_s': skill['timeout_s'],
            'requires': caps(skill['requires']), 'optional': caps(skill['optional']),
            'parameters': robot_caps.parameter_views(skill['parameters'])}


def read_source(p):
    """Read a bundled example (``example``: its id) or a folder the user chose (``path``)."""
    if p.get('example'):
        from .sim_robots import example_path
        return read_folder(example_path(p['example']), bundled=True)
    return read_folder(p.get('path'))


def preview(platform, p):
    """Read, validate and check compatibility. Sends nothing that changes the robot."""
    robot = _robot_for(platform, p)
    package, info = read_source(p)
    skill = validate(package)
    manifest = robot_caps.fetch_manifest_v2(robot)
    compat = explain(skill, manifest)
    return {'robot': {'id': robot['id'], 'name': robot['name'], 'simulated': manifest.get('simulated') is True},
            'path': info['path'], 'example': p.get('example') or None,
            'package_digest': package_digest(package), 'skill': _skill_view(skill),
            'files': [{'path': k, 'bytes': v} for k, v in sorted(info['sizes'].items())],
            'total_bytes': info['total_bytes'], 'compatibility': compat, 'can_install': compat['compatible']}


def _installed_summary(response):
    keys = ('id', 'version', 'digest', 'already_installed', 'installed')
    return {k: response.get(k) for k in keys if k in response}


def install(platform, p):
    """Install the previewed package. ``request_id`` is idempotent: the same id
    returns the recorded outcome and is never sent twice; an unknown outcome is
    never retried automatically."""
    request_id = required_text(p.get('request_id'), '请求标识', 100)
    op_id = 'skill-install-' + request_id
    expected = required_text(p.get('package_digest'), '技能包摘要', 100)
    existing = next((o for o in platform.store.list('operations') if o['id'] == op_id), None)
    if existing:
        if existing.get('robot_id') != p.get('robot_id') or existing.get('package_digest') != expected:
            raise ValueError('请求标识已用于另一次安装')
        return existing
    robot = _robot_for(platform, p)
    package, info = read_source(p)
    skill = validate(package)
    if package_digest(package) != expected:
        raise ValueError('技能文件夹在预检之后发生了变化；请重新预检，确认后再安装')
    manifest = robot_caps.fetch_manifest_v2(robot)
    compat = explain(skill, manifest)
    if not compat['compatible']:
        raise ValueError('技能包与这台机器人不兼容，不能安装：' + '；'.join(
            [m['text'] for m in compat['missing_detail']] + compat['violation_texts']))
    from .service import now
    op = {'id': op_id, 'project_id': robot['project_id'], 'robot_id': robot['id'], 'kind': 'skill_install',
          'skill_id': skill['id'], 'version': skill['version'], 'package_digest': expected,
          'path': info['path'], 'example': p.get('example') or None, 'status': 'dispatching', 'created_at': now()}
    platform.store.put('operations', op)
    try:
        response = connector.call(robot['endpoint'], '/v2/skills/install', robot.get('token', ''),
                                  {'request_id': op_id, 'package': package}, timeout=15)
        if response.get('installed') is not True:
            raise connector.DeviceError('设备未确认安装', status=200, sent=True)
        op.update(status='succeeded', response=_installed_summary(response))
    except connector.DeviceError as exc:
        if exc.code == 'incompatible':
            device_compat = exc.body.get('compatibility') if isinstance(exc.body.get('compatibility'), dict) else None
            op.update(status='rejected', error='设备判定技能包与本体不兼容，未安装', compatibility=device_compat)
        elif exc.status is not None and 400 <= exc.status < 500 and exc.code != 'outcome_unknown':
            op.update(status='rejected', error=f'设备拒绝安装：{exc}')
        elif exc.sent is False:
            op.update(status='failed', error='请求未发出：' + str(exc))
        else:
            op.update(status='unknown', error=f'{exc}；安装结果未知，不会自动重试，请查看设备上的已安装技能')
    platform.store.put('operations', op)
    platform.audit('skill.install', {'operation_id': op['id'], 'robot_id': robot['id'],
                                     'skill': f'{skill["id"]}@{skill["version"]}', 'status': op['status']},
                   robot['project_id'])
    return {**op, 'skills': robot_caps.installed_skills(robot)}
