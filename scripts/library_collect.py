#!/usr/bin/env python3
"""Collect the data of the public library (issue #78, docs/PUBLIC_LIBRARY.md).

    python scripts/library_collect.py [--out DIR] [--materials DIR] [--extra DIR] [--only ID ...] [--offline]

Writes ``DIR`` (default ``$NEUXMIND_LIBRARY_DATA`` or ``~/code/PhysicalOS-private/library-data``, outside every
git repository) with:

* ``models/<id>.zip``: the robot model (URDF + meshes + licence + ``SOURCE.json``) of a brand and model of the
  catalogue (``web/robot_catalog.js``), fetched from its GitHub repository at the commit pinned in
  ``scripts/library_sources.json`` (the same fetch and the same packing rules as ``scripts/robot_models.py``:
  sparse checkout, COLLADA → STL, one ``model.urdf``). A ``.xacro`` is expanded first, a URDF kept inside
  an archive of the repository is taken out of it.
* ``packages/<id>.zip``: a GitHub repository at a pinned commit, as GitHub's source archive (an SDK, a
  control program).
* ``materials/<device>/<file>``: the vendor files the user put in ``--materials`` (default
  ``~/code/PhysicalOS/materials``) and page captures kept in ``--extra`` (default
  ``~/code/PhysicalOS-private/library-sources``), as the sources table names them.
* ``manifest.json``: what ``python -m neuxmind_cloud library-import --dir DIR`` stores in the public library
  (file, kind, body type, brand, model, version, description).
* ``REPORT.md``: what was collected, under which licence, and what was not (and why).

Nothing of it goes into this repository. Running it again reuses what is already fetched.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import robot_models as rm  # noqa: E402  (same folder: the fetch, COLLADA conversion and mesh lookup)

SOURCES = ROOT / 'scripts' / 'library_sources.json'
CATALOG = ROOT / 'web' / 'robot_catalog.js'
DEFAULT_OUT = Path(os.environ.get('NEUXMIND_LIBRARY_DATA') or Path.home() / 'code' / 'PhysicalOS-private' / 'library-data')
DEFAULT_MATERIALS = Path(os.environ.get('NEUXMIND_LIBRARY_MATERIALS') or Path.home() / 'code' / 'PhysicalOS' / 'materials')
DEFAULT_EXTRA = Path(os.environ.get('NEUXMIND_LIBRARY_EXTRA') or Path.home() / 'code' / 'PhysicalOS-private' / 'library-sources')
DEFAULT_PRIVATE = Path(os.environ.get(rm.PRIVATE_ENV) or rm.DEFAULT_PRIVATE)
DEFAULT_DUCK_RL = Path(os.environ.get('NEUXMIND_DUCK_RL') or Path.home() / 'code' / 'duck-rl')   # issue #63's training project
CACHE = ROOT / '.build' / 'library-sources'
BODY_TYPES = {'humanoid': 'biped', 'wheeled': 'other'}      # catalogue type → body type id (web/robot_catalog.js rcBodyType)
MESH_SUFFIXES = rm.MESHES + ('.dae',)
KEPT_MESHES = ('.glb', '.gltf', '.fbx', '.ply')        # kept as they are: the simulation cannot read them
MAX_VERSION, MAX_DESCRIPTION = 50, 2000
NO_LICENCE = '来源仓库没有许可证文件：仅供内部参考，不得再分发'
DOWNLOAD_TIMEOUT_S = 600
MAX_DOWNLOAD_BYTES = 2 * 1024 ** 3                   # the library's limit for a package
MAX_UNPACKED_BYTES, MAX_UNPACKED_MEMBERS = 2 * 1024 ** 3, 20000      # an archive inside a repository
ID = re.compile(r'[a-z0-9][a-z0-9.\-]{0,63}')


class CollectError(ValueError):
    """One entry could not be collected; the message says why."""


# ---------------------------------------------------------------- the catalogue and the sources table
def catalog():
    """``{(vendor, model): type}`` of the brands and models a robot can be chosen as (hidden brands left out)."""
    text = CATALOG.read_text(encoding='utf-8')
    found = {}
    for brand in re.finditer(r"\{vendor: '([^']+)', aliases: \[[^\]]*\],( hidden: true,)? models: \[(.*?)\]\}", text, re.S):
        if brand.group(2):
            continue
        for model, kind in re.findall(r"rcM\('([^']+)', '(\w+)'", brand.group(3)):
            found[(brand.group(1), model)] = kind
    return found


def load_sources(path=SOURCES):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    seen = set()
    for section in ('models', 'local_models', 'packages', 'local'):
        for entry in data.get(section, []):
            if not ID.fullmatch(entry.get('id', '')) or entry['id'] in seen:
                raise CollectError(f'{section}: 编号无效或重复：{entry.get("id")!r}')
            seen.add(entry['id'])
    return data


def body_type(kind):
    return BODY_TYPES.get(kind, kind)


def _version(text):
    text = ' '.join(str(text).split())
    return text[:MAX_VERSION].rstrip()


def _description(parts):
    return '\n'.join(p for p in parts if p)[:MAX_DESCRIPTION]


def _licence_line(entry):
    return f"许可：{entry['licence_name']}" if entry.get('licence_name') else NO_LICENCE


# ---------------------------------------------------------------- one robot model
# `$(find pkg)` in a .xacro is answered by ROS's ament_index_python. Not installed here: this stand-in answers it from
# the folders the sources table names for the entry (``ros_packages``), passed in LIBRARY_ROS_PACKAGES.
_AMENT_STAND_IN = """import json, os
class PackageNotFoundError(KeyError):
    pass
_FOLDERS = json.loads(os.environ.get('LIBRARY_ROS_PACKAGES') or '{}')
def get_package_share_directory(name, print_warning=True):
    if name not in _FOLDERS:
        raise PackageNotFoundError(name)
    return _FOLDERS[name]
"""


def _xacro(entry, base):
    """The URDF text of a ``.xacro`` source, expanded with ``xacro``; ``$(find pkg)`` → the folders the entry names."""
    source = base / entry['xacro']
    if not source.is_file() or source.is_symlink() or not rm._inside(source, base):
        raise CollectError(f'找不到 xacro：{entry["xacro"]}')
    folders = {}
    for package, folder in (entry.get('ros_packages') or {}).items():
        where = (base / folder).resolve()
        if not where.is_dir() or not rm._inside(where, base):
            raise CollectError(f'ROS 包 {package} 的文件夹不在来源里：{folder}')
        folders[package] = str(where)
    args = [f'{k}:={v}' for k, v in (entry.get('xacro_args') or {}).items()]
    # The repository's files are run by xacro: an isolated interpreter (no import from the checkout, no PYTHON*
    # variables) and an environment with nothing of this computer in it, so `$(env …)` / `$(optenv …)` cannot
    # copy anything into a model that every account will see. xacro itself refuses `__…__` names in `${…}`.
    with tempfile.TemporaryDirectory() as home:
        stand_in = Path(home) / 'stand-in' / 'ament_index_python'
        stand_in.mkdir(parents=True)
        (stand_in / '__init__.py').write_text('')
        (stand_in / 'packages.py').write_text(_AMENT_STAND_IN)
        bootstrap = f'import sys; sys.path.insert(0, {str(stand_in.parent)!r}); import xacro; xacro.main()'
        result = subprocess.run([sys.executable, '-I', '-c', bootstrap, str(source), *args],
                                capture_output=True, text=True, timeout=300, cwd=str(source.parent),
                                env={'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8',
                                     'LIBRARY_ROS_PACKAGES': json.dumps(folders)})
    if result.returncode:
        raise CollectError(f'xacro 展开失败：{result.stderr.strip()[:300]}')
    return result.stdout.encode('utf-8')


def _from_archive(entry, base):
    """The folder an archive kept in the repository is unpacked into (members with links or ``..`` are left out)."""
    archive = base / entry['archive']
    target = CACHE / 'unpacked' / (entry['id'] + '-' + hashlib.sha256(archive.read_bytes()).hexdigest()[:12])
    if not (target / '.done').exists():
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        try:
            _unpack(archive, target)
        except (OSError, zipfile.BadZipFile, tarfile.TarError, CollectError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise CollectError(f'解不开 {entry["archive"]}：{exc}') from None
        (target / '.done').write_text('ok')
    return target


def _unpack(archive, target):
    """Regular files only; names with ``..``, absolute names and ``__MACOSX`` are left out; size and count are bounded."""
    total, count = 0, 0

    def keep(name):
        parts = Path(name).parts
        return not (name.startswith('/') or '..' in parts or (parts and parts[0] == '__MACOSX'))

    def write(name, size, source):
        nonlocal total, count
        total, count = total + size, count + 1
        if total > MAX_UNPACKED_BYTES or count > MAX_UNPACKED_MEMBERS:
            raise CollectError('压缩包解开后太大或文件太多')
        out = target.joinpath(*Path(name).parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        with source() as src, out.open('wb') as dst:
            shutil.copyfileobj(src, dst)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                if not info.is_dir() and keep(info.filename) and (info.external_attr >> 16) & 0o170000 != 0o120000:
                    write(info.filename, info.file_size, lambda info=info: zipped.open(info))
    else:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.isreg() and keep(member.name):
                    write(member.name, member.size, lambda member=member: tar.extractfile(member))


def _mesh_files(base, roots):
    files = {}
    for path in sorted(base.rglob('*')):
        name = str(path.relative_to(base)).replace(os.sep, '/')
        if path.suffix.lower() not in MESH_SUFFIXES + KEPT_MESHES or '.git' in path.parts or path.is_symlink() or not path.is_file():
            continue
        if roots and not any(name == r or name.startswith(r.rstrip('/') + '/') for r in roots):
            continue
        files[name] = path
    return files


def pack_model(entry, base, out_dir, origin):
    """``models/<id>.zip`` of one entry: like ``robot_models.pack``, but a mesh that is not found keeps its
    reference (the library reports it) instead of failing the package."""
    if entry.get('archive'):
        base = _from_archive(entry, base)
    if entry.get('xacro'):
        data = _xacro(entry, base)
        named = entry['xacro']
    else:
        urdf_path = base / entry['urdf']
        if not urdf_path.is_file() or urdf_path.is_symlink() or not rm._inside(urdf_path, base):
            raise CollectError(f'找不到 URDF：{entry["urdf"]}')
        data, named = urdf_path.read_bytes(), entry['urdf']
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise CollectError(f'{named} 不是 UTF-8 文本') from None
    if '<!DOCTYPE' in text or '<!ENTITY' in text:
        raise CollectError('URDF 带有 DOCTYPE 或实体')
    root = ET.fromstring(data)
    if root.tag != 'robot':
        raise CollectError(f'{named} 不是 URDF（根元素是 {root.tag}）')
    files = _mesh_files(base, entry.get('mesh_paths') or [])
    members, converted, missing, kept_dae = {}, [], set(), []
    for link in root.findall('link'):
        for holder in list(link):
            if holder.tag not in ('visual', 'collision'):
                continue
            for extra in holder.findall('material')[1:]:        # MuJoCo's URDF reader refuses a second one
                holder.remove(extra)
            mesh = holder.find('geometry/mesh')
            if mesh is None:
                continue
            found = rm._find(files, mesh.get('filename') or '')
            if found is None:
                missing.add(mesh.get('filename') or '')
                continue
            member = 'meshes/' + found
            if found.lower().endswith('.dae'):
                try:
                    body = rm.dae_to_stl(files[found].read_bytes())
                    member += '.stl'
                    if member not in members:
                        converted.append(found)
                except (rm.PackError, ET.ParseError, ValueError, IndexError, KeyError) as exc:
                    body = files[found].read_bytes()
                    kept_dae.append(f'{found}（{exc}）')
                members.setdefault(member, body)
            else:
                if found.lower().endswith(KEPT_MESHES) and member not in members:
                    kept_dae.append(found)
                members.setdefault(member, files[found].read_bytes())
            mesh.set('filename', member)
    notes = list(entry.get('changes') or [])
    if converted:
        notes.append(f'{len(converted)} 个 COLLADA 网格转成了 STL（颜色和贴图没有保留），仿真才能读')
    if kept_dae:
        notes.append(f'{len(kept_dae)} 个网格是仿真读不了的格式（GLB、FBX 等，或转换不了的 COLLADA），原样保留')
    if missing:
        notes.append(f'{len(missing)} 个网格文件在来源里找不到，引用原样保留：' + '、'.join(sorted(missing)[:10]))
    for tag in ('gazebo', 'transmission'):
        removed = root.findall(tag)
        for element in removed:
            root.remove(element)
        if removed:
            notes.append(f'去掉了 {len(removed)} 个 <{tag}>（仿真用不到）')
    members['model.urdf'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if entry.get('licence'):
        licence = base / entry['licence'] if not entry.get('archive') else origin['checkout'] / entry['licence']
        if licence.is_symlink() or not licence.is_file():
            raise CollectError(f'找不到许可证文件：{entry["licence"]}')
        members['LICENSE.txt'] = licence.read_bytes()
    record = {'id': entry['id'], 'vendor': entry['vendor'], 'model': entry['model'],
              'origin': {k: v for k, v in origin.items() if k != 'checkout'}, 'urdf': named,
              'licence': entry.get('licence_name') or NO_LICENCE, 'exact': entry.get('exact', True),
              'notes': entry.get('notes', ''), 'changes': notes,
              'files': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(members.items())}}
    members['SOURCE.json'] = json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
    target = Path(out_dir) / 'models' / (entry['id'] + '.zip')
    _write_zip(target, members)
    return {'file': f'models/{entry["id"]}.zip', 'bytes': target.stat().st_size, 'meshes': len(members) - 2 - ('LICENSE.txt' in members),
            'changes': notes}


def _write_zip(target, members):
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix('.part')
    with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, rm.ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name])
    os.replace(part, target)


def _model_entry(entry, done):
    """The manifest entry of a packed model."""
    if entry.get('repo'):
        where = f"https://github.com/{entry['repo']} 提交 {entry['commit'][:12]}，文件 " \
                f"{entry.get('archive', '') + ' → ' if entry.get('archive') else ''}{entry.get('urdf') or entry.get('xacro') or entry.get('mjcf')}"
    elif entry.get('zip'):
        where = f"厂家资料包 {entry['zip']} → {entry['dir']}/{entry['main']}"
    elif entry.get('main'):
        where = f"{entry.get('where') or entry['dir']} → {entry['main']}"
    else:
        where = f"厂家文件 {entry.get('urdf')}"
    version = entry.get('version') or f"{entry['repo'].split('/')[1]}@{entry['commit'][:7]}"
    return {'file': done['file'], 'kind': 'model', 'body_type': body_type(entry['type']), 'vendor': entry['vendor'],
            'model': entry['model'], 'version': _version(version),
            'description': _description([f"机器人模型（{entry.get('label') or 'URDF'}）。来源：{where}。", _licence_line(entry),
                                         '' if entry.get('exact', True) else '注意：不是这个型号本身的模型，是最接近的一款。',
                                         entry.get('notes', ''), '；'.join(done['changes']),
                                         '由 scripts/library_collect.py 收集（issue #78）。'])}


# ---------------------------------------------------------------- a model taken out of an archive of the materials
_INCLUDE = re.compile(rb'<include\s+file="([^"]+)"')


def _mjcf_members(entry, names, read):
    """The members of an MJCF model package: the main file, what it includes (recursively), the asset folders,
    named relative to the main file's folder (``entry['dir']``), and the licence files by their own names.
    ``names``: every file there is (full names); ``read(full name)``: its bytes."""
    prefix = entry['dir'].rstrip('/') + '/' if entry.get('dir') else ''
    members, todo, seen = {}, [entry['main']], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        if '..' in Path(name).parts or prefix + name not in names:
            raise CollectError(f'找不到 {prefix + name}')
        seen.add(name)
        members[name] = read(prefix + name)
        todo.extend(m.decode('utf-8') for m in _INCLUDE.findall(members[name]))
    for folder in entry.get('assets', []):
        for full in sorted(n for n in names if n.startswith(prefix + folder.rstrip('/') + '/')):
            members[full[len(prefix):]] = read(full)
    for licence in entry.get('licences', []):
        members[Path(licence).name] = read(licence)
    return members


_FILE_ATTR = re.compile(r'(\sfile=")([^"]+)(")')


def _flattened(entry, folder, root):
    """One MJCF file whose meshes lie anywhere under ``root``: each ``file="…"`` is copied to ``assets/<name>``
    and the reference rewritten (``entry['flatten']``)."""
    main = (folder / entry['main']).resolve()
    text = main.read_text(encoding='utf-8')
    if '<include' in text:
        raise CollectError('flatten 只用于不 include 别的文件的单个 MJCF')
    members, taken = {}, {}

    def move(match):
        target = (main.parent / match.group(2)).resolve()
        if root.resolve() not in target.parents or not target.is_file() or target.is_symlink():
            raise CollectError(f'MJCF 引用的文件不在来源文件夹里：{match.group(2)}')
        name = taken.get(target)
        if name is None:
            name = 'assets/' + target.name
            while name in members:
                name = 'assets/' + str(len(members)) + '-' + target.name
            taken[target] = name
            members[name] = target.read_bytes()
        return match.group(1) + name + match.group(3)
    members[entry['main']] = _FILE_ATTR.sub(move, text).encode('utf-8')
    for extra in entry.get('extras', []):
        members[Path(extra).name] = (folder / extra).read_bytes()
    return members


def pack_local_model(entry, roots, out_dir):
    """``models/<id>.zip`` from a folder of the materials (``dir``), or from a folder inside a zip of them (``zip``):
    an MJCF model."""
    root = roots[entry.get('root', 'materials')]
    if not entry.get('zip'):
        folder = (root / entry['dir']).resolve()
        if not folder.is_dir() or (folder != root.resolve() and root.resolve() not in folder.parents):
            raise CollectError(f'找不到文件夹：{root / entry["dir"]}')
        if entry.get('flatten'):
            members = _flattened(entry, folder, root)
        else:
            names = {str(p.relative_to(root)).replace(os.sep, '/') for p in folder.rglob('*') if p.is_file() and not p.is_symlink()}
            members = _mjcf_members(entry, names, lambda name: (root / name).read_bytes())
        return _write_mjcf(entry, members, out_dir, {'folder': entry['dir']})
    archive_path = (root / entry['zip']).resolve()
    if not archive_path.is_file() or root.resolve() not in archive_path.parents:
        raise CollectError(f'找不到文件：{root / entry["zip"]}')
    with zipfile.ZipFile(archive_path) as archive:
        names = {n for n in archive.namelist() if not n.endswith('/')}
        members = _mjcf_members(entry, names, archive.read)
    return _write_mjcf(entry, members, out_dir, {'archive': entry['zip'], 'dir': entry['dir']})


def pack_repo_mjcf(entry, base, out_dir, origin):
    """``models/<id>.zip`` of an MJCF model in a checkout (``mjcf`` = the main file; ``assets`` relative to its folder)."""
    main = Path(entry['mjcf'])
    folder = str(main.parent).replace(os.sep, '/')
    names = {str(p.relative_to(base)).replace(os.sep, '/') for p in base.rglob('*')
             if p.is_file() and not p.is_symlink() and '.git' not in p.parts}
    members = _mjcf_members({**entry, 'dir': '' if folder == '.' else folder, 'main': main.name,
                             'licences': [entry['licence']] if entry.get('licence') else []},
                            names, lambda name: (base / name).read_bytes())
    return _write_mjcf(entry, members, out_dir, {k: v for k, v in origin.items() if k != 'checkout'})


def _write_mjcf(entry, members, out_dir, origin):
    record = {'id': entry['id'], 'vendor': entry['vendor'], 'model': entry['model'], 'origin': origin,
              'main': entry.get('main') or Path(entry['mjcf']).name,
              'licence': entry.get('licence_name') or NO_LICENCE, 'notes': entry.get('notes', ''),
              'files': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(members.items())}}
    members['SOURCE.json'] = json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
    target = Path(out_dir) / 'models' / (entry['id'] + '.zip')
    _write_zip(target, members)
    return {'file': f'models/{entry["id"]}.zip', 'bytes': target.stat().st_size, 'meshes': len(members), 'changes': []}


# ---------------------------------------------------------------- a repository as a package
def fetch_archive(entry, out_dir, offline=False):
    """``packages/<id>.zip``: GitHub's source archive of the repository at the pinned commit."""
    target = Path(out_dir) / 'packages' / (entry['id'] + '.zip')
    stamp = target.with_suffix('.commit')
    if target.is_file() and stamp.is_file() and stamp.read_text().strip() == entry['commit']:
        return {'file': f'packages/{entry["id"]}.zip', 'bytes': target.stat().st_size}
    if offline:
        raise CollectError('还没有取到本机（--offline）')
    url = f"https://codeload.github.com/{entry['repo']}/zip/{entry['commit']}"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix('.part')
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response, part.open('wb') as out:
            while block := response.read(1024 * 1024):
                out.write(block)
                if out.tell() > MAX_DOWNLOAD_BYTES:
                    raise CollectError('下载的压缩包超过 2 GB（资料库的上限）')
        with zipfile.ZipFile(part) as archive:
            if archive.testzip() is not None:
                raise CollectError('下载的压缩包损坏')
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    stamp.write_text(entry['commit'])
    return {'file': f'packages/{entry["id"]}.zip', 'bytes': target.stat().st_size}


def _package_entry(entry, done):
    return {'file': done['file'], 'filename': entry.get('filename') or f"{entry['repo'].split('/')[1]}.zip",
            'body_type': body_type(entry['type']), 'vendor': entry['vendor'], 'model': entry['model'],
            'version': _version(entry.get('version') or f"{entry['repo'].split('/')[1]}@{entry['commit'][:7]}"),
            'description': _description([f"{entry['what']}。来源：https://github.com/{entry['repo']} 提交 {entry['commit'][:12]}（源码压缩包）。",
                                         _licence_line(entry), entry.get('notes', ''), '由 scripts/library_collect.py 收集（issue #78）。'])}


# ---------------------------------------------------------------- the user's files
def copy_local(entry, roots, out_dir):
    """``materials/<device>/<name>``: one file of the materials folder (or of the page captures)."""
    root = roots[entry.get('root', 'materials')]
    source = (root / entry['path']).resolve()
    if not source.is_file() or source.is_symlink() or root.resolve() not in source.parents:
        raise CollectError(f'找不到文件：{root / entry["path"]}')
    name = entry.get('filename') or source.name
    target = Path(out_dir) / 'materials' / entry['device'] / name
    if not (target.is_file() and target.stat().st_size == source.stat().st_size
            and target.stat().st_mtime >= source.stat().st_mtime):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return {'file': f'materials/{entry["device"]}/{name}', 'bytes': target.stat().st_size}


def _local_entry(entry, done):
    item = {'file': done['file'], 'body_type': body_type(entry['type']), 'vendor': entry['vendor'], 'model': entry['model'],
            'version': _version(entry['version']),
            'description': _description([entry.get('what', ''), entry.get('source', ''), entry.get('notes', ''),
                                         '由 scripts/library_collect.py 收集（issue #78）。'])}
    if entry.get('kind'):
        item['kind'] = entry['kind']
    return item


# ---------------------------------------------------------------- all of it
def collect(out_dir=DEFAULT_OUT, materials=DEFAULT_MATERIALS, extra=DEFAULT_EXTRA, only=None, offline=False, sources=None,
            private=DEFAULT_PRIVATE, duck_rl=DEFAULT_DUCK_RL):
    data = sources or load_sources()
    out_dir = Path(out_dir)
    roots = {'materials': Path(materials), 'extra': Path(extra), 'duck_rl': Path(duck_rl)}
    manifest, report = [], {'models': [], 'packages': [], 'local': [], 'failed': [], 'skipped': data.get('skipped', [])}
    report['models_local'] = []
    needed = {}
    for entry in data.get('models', []):
        if entry.get('repo'):
            needed.setdefault((entry['repo'], entry['commit']), set()).update(entry['paths'] + ([entry['licence']] if entry.get('licence') else []))
    wanted = (lambda e: e['id'] in only) if only else (lambda e: True)
    for entry in filter(wanted, data.get('models', [])):
        try:
            if entry.get('private'):
                checkout = Path(private) / entry['private']
                if not checkout.is_dir():
                    raise CollectError(f'没有这份不公开的模型（{checkout}）')
                origin = {'private': entry['private'], 'checkout': checkout}
            else:
                paths = sorted(needed[(entry['repo'], entry['commit'])])
                checkout = rm.fetch({'repo': entry['repo'], 'commit': entry['commit'], 'paths': paths,
                                     'licence': entry.get('licence') or paths[0]}, offline, paths)
                origin = {'repo': 'https://github.com/' + entry['repo'], 'commit': entry['commit'], 'checkout': checkout}
            done = (pack_repo_mjcf if entry.get('mjcf') else pack_model)(entry, checkout, out_dir, origin)
            manifest.append(_model_entry(entry, done))
            report['models'].append({**entry, **done})
        except (CollectError, rm.PackError, ET.ParseError, OSError, subprocess.SubprocessError, ValueError, KeyError,
                tarfile.TarError) as exc:
            (out_dir / 'models' / (entry['id'] + '.zip')).unlink(missing_ok=True)
            report['failed'].append({'id': entry['id'], 'vendor': entry['vendor'], 'model': entry['model'], 'reason': str(exc) or type(exc).__name__})
    for section, run, view in (('local_models', lambda e: pack_local_model(e, roots, out_dir), _model_entry),
                               ('packages', lambda e: fetch_archive(e, out_dir, offline), _package_entry),
                               ('local', lambda e: copy_local(e, roots, out_dir), _local_entry)):
        for entry in filter(wanted, data.get(section, [])):
            try:
                done = run(entry)
                manifest.append(view(entry, done))
                report['models' if section == 'local_models' else section].append({**entry, **done})
            except (CollectError, OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
                report['failed'].append({'id': entry['id'], 'vendor': entry['vendor'], 'model': entry['model'], 'reason': str(exc) or type(exc).__name__})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'manifest.json').write_text(json.dumps({'version': 1, 'materials': manifest}, ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'REPORT.md').write_text(render_report(report, data), encoding='utf-8')
    return manifest, report


def coverage(data):
    """Brands and models of the catalogue with no model in the sources table: ``[(vendor, model)]``."""
    have = {(e['vendor'], e['model']) for e in data.get('models', []) + data.get('local_models', [])}
    have |= {(e['vendor'], e['model']) for e in data.get('local', []) if e.get('kind') == 'model'}
    return [key for key in catalog() if key not in have]


def render_report(report, data):
    lines = ['# 公共资料库的数据（issue #78）', '', '由 `scripts/library_collect.py` 生成。导入：`python -m neuxmind_cloud library-import --dir <这个文件夹>`。', '',
             f"机器人模型 {len(report['models'])} 份，源码包 {len(report['packages'])} 份，厂家资料 {len(report['local'])} 份，"
             f"没有取到 {len(report['failed'])} 份。", '', '## 机器人模型', '', '| 品牌 | 型号 | 来源 | 许可 | 说明 |', '|---|---|---|---|---|']
    for m in report['models']:
        where = f"[{m['repo']}](https://github.com/{m['repo']}) `{m['commit'][:7]}`" if m.get('repo') else (m.get('zip') or '厂家文件（不公开）')
        lines.append(f"| {m['vendor']} | {m['model']} | {where} | "
                     f"{m.get('licence_name') or '无（仅内部参考）'} | {'近似型号；' if not m.get('exact', True) else ''}{'；'.join(m['changes'])} |")
    lines += ['', '## 源码包', '', '| 品牌 | 型号 | 内容 | 来源 | 许可 |', '|---|---|---|---|---|']
    for p in report['packages']:
        lines.append(f"| {p['vendor']} | {p['model']} | {p['what']} | [{p['repo']}](https://github.com/{p['repo']}) `{p['commit'][:7]}` | "
                     f"{p.get('licence_name') or '无（仅内部参考）'} |")
    lines += ['', '## 厂家资料', '', '| 品牌 | 型号 | 文件 | 说明 |', '|---|---|---|---|']
    for item in report['local']:
        lines.append(f"| {item['vendor']} | {item['model']} | {item['file']} | {item.get('what', '')} |")
    lines += ['', '## 没有取到', '', '| 编号 | 品牌 | 型号 | 原因 |', '|---|---|---|---|']
    for f in report['failed']:
        lines.append(f"| {f['id']} | {f['vendor']} | {f['model']} | {f['reason']} |")
    lines += ['', '## 网上没有公开模型的型号', '', '| 品牌 | 型号 | 原因 |', '|---|---|---|']
    for s in report['skipped']:
        lines.append(f"| {s['vendor']} | {s['model']} | {s['why']} |")
    lines += ['', '## 没有收进来的厂家文件', '', '| 品牌 | 型号 | 文件 | 原因 |', '|---|---|---|---|']
    for n in data.get('not_collected', []):
        lines.append(f"| {n['vendor']} | {n['model']} | {n['what']} | {n['why']} |")
    gaps = [k for k in coverage(data) if k not in {(s['vendor'], s['model']) for s in report['skipped']}]
    if gaps:
        lines += ['', '## 清单里有、来源表里没有写的型号', ''] + [f'- {v} {m}' for v, m in gaps]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--materials', default=str(DEFAULT_MATERIALS))
    parser.add_argument('--extra', default=str(DEFAULT_EXTRA))
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--private', default=str(DEFAULT_PRIVATE))
    parser.add_argument('--duck-rl', default=str(DEFAULT_DUCK_RL))
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    manifest, report = collect(args.out, args.materials, args.extra, set(args.only) if args.only else None, args.offline,
                               private=args.private, duck_rl=args.duck_rl)
    for m in report['models']:
        print(f"model    {m['id']:34} {m['bytes'] / 1e6:7.1f} MB  {m['meshes']} 个网格")
    for p in report['packages'] + report['local']:
        print(f"file     {p['id']:34} {p['bytes'] / 1e6:7.1f} MB")
    for f in report['failed']:
        print(f"failed   {f['id']:34} {f['reason']}")
    print(f"{len(manifest)} 份写进了 {Path(args.out) / 'manifest.json'}；没有取到 {len(report['failed'])} 份")
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
