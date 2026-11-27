#!/usr/bin/env python3
"""Build the robot model packages of the built-in profiles (issue #54, docs/SIM_PROFILES.md).

    python scripts/robot_models.py [--out .build/robot-models] [--private DIR] [--only ID ...] [--offline]

One ``<profile id>.zip`` per profile of ``brainos/sim_profiles.py`` that has a ``source``: the URDF, the
meshes it uses and the licence of where they come from. The app reads these packages when a simulated
robot of that brand and model is added. **No robot model is kept in this repository.**

* A public model is fetched from its GitHub repository at a pinned commit (``git`` with a sparse
  checkout, cached under ``.build/robot-model-sources/``).
* A model that is not public (``private`` in its source) is read from ``--private DIR`` (default
  ``$NEUXMIND_PRIVATE_ROBOT_MODELS`` or ``~/code/PhysicalOS-private/robot-models``): ``DIR/<name>/``.
  When that folder is missing the profile is skipped and the app does not offer it.

What is changed on the way (all of it is written into the package's ``SOURCE.json``):

* COLLADA meshes (``.dae``) are converted to binary STL, because the simulation reads only STL/OBJ/MSH.
  Colours and textures are lost.
* With ``stand_in_shapes``, a link whose mesh file is not there gets simple shapes drawn from the
  joint positions (a box for a link with several joints, a cylinder towards the next joint, a ball at
  the end), so the robot is visible and can touch things. They are not the robot's real outline.

Only the standard library is used. The output is the same bytes for the same input.
"""
import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / '.build' / 'robot-models'
CACHE = ROOT / '.build' / 'robot-model-sources'
PRIVATE_ENV = 'NEUXMIND_PRIVATE_ROBOT_MODELS'
DEFAULT_PRIVATE = Path.home() / 'code' / 'PhysicalOS-private' / 'robot-models'
MESHES = ('.stl', '.obj', '.msh')
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_XML_BYTES = 256 * 1024 * 1024
STAND_IN_RADIUS = 0.022
STAND_IN_PAD = 0.045


class PackError(ValueError):
    """This profile's package could not be built; the message says why."""


# ---------------------------------------------------------------- sources
def fetch(source, offline=False, paths=None):
    """The checked-out directory of a public source (fetched once per repository and commit; ``paths`` is
    everything any profile needs from it)."""
    repo, commit = source['repo'], source['commit']
    target = CACHE / (repo.replace('/', '--') + '-' + commit[:12])
    done = target / '.fetched'
    wanted = sorted(set(paths or source['paths']) | {source['licence']})
    try:
        have = set(json.loads(done.read_text())['paths'])
    except (OSError, ValueError, KeyError, TypeError):
        have = None
    if have is not None and have >= set(wanted):
        return target
    if offline:
        raise PackError(f'{repo} 还没有取到本机（--offline）')
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)

    def git(*args):
        result = subprocess.run(['git', '-C', str(target), *args], capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise PackError(f'git {args[0]} 失败（{repo}）：{result.stderr.strip()[-300:]}')
    git('init', '-q')
    git('remote', 'add', 'origin', f'https://github.com/{repo}.git')
    git('sparse-checkout', 'set', '--no-cone', *wanted)
    git('fetch', '-q', '--depth', '1', '--filter=blob:none', 'origin', commit)
    git('checkout', '-q', 'FETCH_HEAD')
    head = subprocess.run(['git', '-C', str(target), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head != commit:
        raise PackError(f'{repo}：取到的不是指定的提交（{head[:12]} ≠ {commit[:12]}）')
    done.write_text(json.dumps({'paths': wanted}))
    return target


# ---------------------------------------------------------------- COLLADA → STL
def _matrix(text):
    values = [float(v) for v in text.split()]
    if len(values) != 16:
        raise PackError('COLLADA 里的变换矩阵不是 16 个数')
    return [values[i:i + 4] for i in (0, 4, 8, 12)]


def _multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


IDENTITY = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _node_transform(node):
    matrix = IDENTITY
    for child in node:
        kind = _local(child.tag)
        if kind == 'matrix':
            matrix = _multiply(matrix, _matrix(child.text or ''))
        elif kind == 'translate':
            x, y, z = (float(v) for v in child.text.split())
            matrix = _multiply(matrix, [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]])
        elif kind == 'scale':
            x, y, z = (float(v) for v in child.text.split())
            matrix = _multiply(matrix, [[x, 0, 0, 0], [0, y, 0, 0], [0, 0, z, 0], [0, 0, 0, 1]])
        elif kind == 'rotate':
            x, y, z, degrees = (float(v) for v in child.text.split())
            length = math.sqrt(x * x + y * y + z * z) or 1.0
            x, y, z, c, s = x / length, y / length, z / length, math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
            t = 1 - c
            matrix = _multiply(matrix, [[t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0],
                                        [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0],
                                        [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0], [0, 0, 0, 1]])
    return matrix


def _geometry_triangles(geometry, by_id):
    """Triangles (three points each) of one ``<geometry>``: ``<triangles>`` and ``<polylist>`` of triangles/quads/fans."""
    out = []
    mesh = next((c for c in geometry if _local(c.tag) == 'mesh'), None)
    if mesh is None:
        return out
    for prim in mesh:
        kind = _local(prim.tag)
        if kind not in ('triangles', 'polylist', 'polygons'):
            continue
        inputs = [c for c in prim if _local(c.tag) == 'input']
        stride = max(int(i.get('offset', '0')) for i in inputs) + 1
        vertex = next((i for i in inputs if i.get('semantic') == 'VERTEX'), None)
        if vertex is None:
            continue
        vertices = by_id.get(vertex.get('source', '')[1:])
        position = next((c for c in (vertices if vertices is not None else []) if c.get('semantic') == 'POSITION'), None)
        source = by_id.get(position.get('source', '')[1:]) if position is not None else None
        array = next((c for c in (source if source is not None else []) if _local(c.tag) == 'float_array'), None)
        if array is None:
            raise PackError('COLLADA 网格没有顶点坐标')
        floats = [float(v) for v in (array.text or '').split()]
        points = [tuple(floats[i:i + 3]) for i in range(0, len(floats) - 2, 3)]
        offset = int(vertex.get('offset', '0'))
        groups = [[int(v) for v in (p.text or '').split()] for p in prim if _local(p.tag) == 'p']
        counts = next(([int(v) for v in (c.text or '').split()] for c in prim if _local(c.tag) == 'vcount'), None)
        for index_list in groups:
            corners = [index_list[i + offset] for i in range(0, len(index_list), stride)]
            if kind == 'triangles':
                counts_here = [3] * (len(corners) // 3)
            elif kind == 'polylist':
                counts_here = counts or []
            else:
                counts_here = [len(corners)]
            at = 0
            for count in counts_here:
                polygon = corners[at:at + count]
                at += count
                for k in range(1, len(polygon) - 1):
                    out.append((points[polygon[0]], points[polygon[k]], points[polygon[k + 1]]))
    return out


def _apply(matrix, point):
    x, y, z = point
    return tuple(matrix[i][0] * x + matrix[i][1] * y + matrix[i][2] * z + matrix[i][3] for i in range(3))


def stl_bytes(triangles, note):
    out = io.BytesIO()
    out.write(note.encode('ascii', 'replace')[:80].ljust(80, b' '))
    out.write(struct.pack('<I', len(triangles)))
    for a, b, c in triangles:
        out.write(struct.pack('<12fH', 0.0, 0.0, 0.0, *a, *b, *c, 0))
    return out.getvalue()


def dae_to_stl(data):
    """A COLLADA document as one binary STL: every geometry the scene shows, in the file's own frame, in metres."""
    if len(data) > MAX_XML_BYTES or b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        raise PackError('COLLADA 文件太大或带有 DOCTYPE')
    root = ET.fromstring(data)
    by_id = {e.get('id'): e for e in root.iter() if e.get('id')}
    unit = next((float(e.get('meter', '1')) for e in root.iter() if _local(e.tag) == 'unit'), 1.0)
    up = next(((e.text or '').strip() for e in root.iter() if _local(e.tag) == 'up_axis'), 'Y_UP')
    axis = {'Z_UP': IDENTITY, 'Y_UP': [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
            'X_UP': [[0, -1, 0, 0], [0, 0, -1, 0], [1, 0, 0, 0], [0, 0, 0, 1]]}.get(up, IDENTITY)
    base = _multiply([[unit, 0, 0, 0], [0, unit, 0, 0], [0, 0, unit, 0], [0, 0, 0, 1]], axis)
    triangles = []

    def walk(node, matrix, depth=0):
        if depth > 64:
            raise PackError('COLLADA 的节点嵌套太深')
        matrix = _multiply(matrix, _node_transform(node))
        for child in node:
            kind = _local(child.tag)
            if kind == 'instance_geometry':
                geometry = by_id.get(child.get('url', '')[1:])
                if geometry is not None:
                    triangles.extend(tuple(_apply(matrix, p) for p in t) for t in _geometry_triangles(geometry, by_id))
            elif kind == 'node':
                walk(child, matrix, depth + 1)
            elif kind == 'instance_node':
                target = by_id.get(child.get('url', '')[1:])
                if target is not None:
                    walk(target, matrix, depth + 1)

    scenes = [e for e in root.iter() if _local(e.tag) == 'visual_scene']
    for scene in scenes:
        for node in scene:
            if _local(node.tag) == 'node':
                walk(node, base)
    if not triangles:
        raise PackError('COLLADA 文件里没有三角面')
    return stl_bytes(triangles, 'converted from COLLADA')


# ---------------------------------------------------------------- stand-in shapes
def _xyz(element, name='xyz'):
    try:
        values = [float(v) for v in (element.get(name) or '0 0 0').split()]
    except (AttributeError, ValueError):
        values = [0.0, 0.0, 0.0]
    return values if len(values) == 3 else [0.0, 0.0, 0.0]


def _shape(parent, tag, geometry, attrs, xyz=(0, 0, 0), rpy=(0, 0, 0)):
    holder = ET.SubElement(parent, tag)
    ET.SubElement(holder, 'origin', {'xyz': ' '.join(f'{v:.5f}' for v in xyz), 'rpy': ' '.join(f'{v:.5f}' for v in rpy)})
    ET.SubElement(ET.SubElement(holder, 'geometry'), geometry, attrs)


def stand_in(root, link_names):
    """Give each of ``link_names`` simple shapes drawn from where its joints are (visual and collision)."""
    offsets = {}
    for joint in root.findall('joint'):
        parent, origin = joint.find('parent'), joint.find('origin')
        if parent is not None:
            offsets.setdefault(parent.get('link'), []).append(_xyz(origin) if origin is not None else [0.0, 0.0, 0.0])
    for link in root.findall('link'):
        if link.get('name') not in link_names:
            continue
        ends = [o for o in offsets.get(link.get('name'), []) if math.sqrt(sum(v * v for v in o)) > 0.02]
        for tag in ('visual', 'collision'):
            if len(ends) >= 3:
                low = [min(o[i] for o in ends) - STAND_IN_PAD for i in range(3)]
                high = [max(o[i] for o in ends) + STAND_IN_PAD for i in range(3)]
                size = [max(high[i] - low[i], 2 * STAND_IN_PAD) for i in range(3)]
                _shape(link, tag, 'box', {'size': ' '.join(f'{v:.5f}' for v in size)}, [(low[i] + high[i]) / 2 for i in range(3)])
            elif ends:
                for end in ends:
                    length = math.sqrt(sum(v * v for v in end))
                    pitch, yaw = math.acos(max(-1.0, min(1.0, end[2] / length))), math.atan2(end[1], end[0])
                    _shape(link, tag, 'cylinder', {'radius': f'{STAND_IN_RADIUS:.5f}', 'length': f'{length:.5f}'},
                           [v / 2 for v in end], (0.0, pitch, yaw))
            else:
                _shape(link, tag, 'sphere', {'radius': f'{STAND_IN_RADIUS * 1.2:.5f}'})


# ---------------------------------------------------------------- one package
def _reference_tail(reference):
    text = reference.strip().replace('\\', '/')
    if text.startswith('package://'):
        text = text[len('package://'):].split('/', 1)[-1]
    elif '://' in text:
        text = text.split('://', 1)[1]
    return [part for part in text.split('/') if part and part not in ('.', '..')]


def _find(files, reference):
    """The file a mesh reference means: the longest matching path ending among ``files`` ({relative path: Path})."""
    parts = [p.lower() for p in _reference_tail(reference)]
    best, best_len = None, 0
    for name in sorted(files):
        own = [p.lower() for p in name.split('/')]
        length = 0
        while length < min(len(own), len(parts)) and own[-1 - length] == parts[-1 - length]:
            length += 1
        if length > best_len:
            best, best_len = name, length
    return best


def _inside(path, base):
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def pack(profile, base, out_dir, origin):
    """Write ``<out_dir>/<profile id>.zip`` from the files under ``base``; returns what was written."""
    source = profile['source']
    urdf_path = base / source['urdf']
    if not urdf_path.is_file():
        raise PackError(f'找不到 URDF：{source["urdf"]}')
    data = urdf_path.read_bytes()
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        raise PackError('URDF 带有 DOCTYPE 或实体')
    root = ET.fromstring(data)
    if urdf_path.is_symlink() or not _inside(urdf_path, base):
        raise PackError('URDF 是一个链接，或不在来源目录里')
    # Only this profile's own folders (a checkout is shared by several profiles), and never through a link:
    # a repository must not be able to make the package carry a file from elsewhere on this computer.
    roots = sorted({pattern.rsplit('/', 1)[0] for pattern in source.get('paths', []) if '/' in pattern})
    files = {}
    for path in sorted(base.rglob('*')):
        name = str(path.relative_to(base)).replace(os.sep, '/')
        if path.suffix.lower() not in MESHES + ('.dae',) or '.git' in path.parts or path.is_symlink() or not path.is_file():
            continue
        if roots and not any(name.startswith(root + '/') for root in roots):
            continue
        if _inside(path, base):
            files[name] = path
    members, converted, missing_links = {}, [], set()
    for link in root.findall('link'):
        for holder in list(link):
            if holder.tag not in ('visual', 'collision'):
                continue
            for extra in holder.findall('material')[1:]:        # MuJoCo's URDF reader refuses a second one
                holder.remove(extra)
            mesh = holder.find('geometry/mesh')
            if mesh is None:
                continue
            found = _find(files, mesh.get('filename') or '')
            if found is None:
                link.remove(holder)
                missing_links.add(link.get('name'))
                continue
            member = 'meshes/' + found
            if found.lower().endswith('.dae'):
                member = member + '.stl'            # its own name: a foo.stl next to foo.dae stays another file
                if member not in members:
                    members[member] = dae_to_stl(files[found].read_bytes())
                    converted.append(found)
            elif member not in members:
                members[member] = files[found].read_bytes()
            mesh.set('filename', member)
    notes = []
    if converted:
        notes.append(f'{len(converted)} 个 COLLADA 网格转成了 STL（颜色和贴图没有保留）')
    if missing_links:
        if not source.get('stand_in_shapes'):
            raise PackError(f'这些连杆的网格文件没有找到：{"、".join(sorted(missing_links))}')
        stand_in(root, missing_links)
        notes.append(f'{len(missing_links)} 个连杆没有网格文件，用按关节位置画出的简单形状代替，不是机器人的真实外形')
    for tag in ('gazebo', 'transmission'):
        for element in root.findall(tag):
            root.remove(element)
    members['model.urdf'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    licence = base / source['licence'] if source.get('licence') else None
    if licence is not None:
        if licence.is_symlink() or not licence.is_file():
            raise PackError(f'找不到许可证文件：{source["licence"]}')
        members['LICENSE.txt'] = licence.read_bytes()
    record = {'profile': profile['id'], 'vendor': profile['vendor'], 'model': profile['model'], 'origin': origin,
              'urdf': source['urdf'], 'licence': source.get('licence_name', ''), 'changes': notes,
              'files': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(members.items())}}
    members['SOURCE.json'] = json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (profile['id'] + '.zip')
    part = target.with_suffix('.part')
    with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name])
    os.replace(part, target)
    return {'id': profile['id'], 'path': str(target), 'bytes': target.stat().st_size, 'meshes': len(members) - 2 - bool(licence),
            'notes': notes}


def write_notice(out_dir):
    """``NOTICE.txt``: where each package in ``out_dir`` comes from and under which licence (each package also
    carries the licence text itself). Packages of profiles that are no longer listed are removed."""
    from brainos import sim_profiles
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ['Robot models packaged with this application (built by scripts/robot_models.py).',
             'Each <id>.zip holds the URDF, its meshes, LICENSE.txt where the source has one, and SOURCE.json', '']
    for path in sorted(out_dir.glob('*.zip')):
        if path.stem not in sim_profiles.BY_ID:
            path.unlink()
            continue
        with zipfile.ZipFile(path) as archive:
            record = json.loads(archive.read('SOURCE.json'))
        origin = record['origin']
        where = f"{origin['repo']} @ {origin['commit']}" if origin.get('repo') else 'provided by the manufacturer, not public'
        lines.append(f"{path.name}: {record['vendor']} {record['model']} | {where} | {record.get('licence') or '-'}")
        lines.extend('    ' + note for note in record.get('changes', []))
    (out_dir / 'NOTICE.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def build(out_dir=DEFAULT_OUT, private=None, only=None, offline=False):
    """Build every package that can be built; ``(done, skipped)`` with the reason of each skipped one."""
    from brainos import sim_profiles
    private = Path(private or os.environ.get(PRIVATE_ENV) or DEFAULT_PRIVATE)
    done, skipped, needed = [], [], {}
    for profile in sim_profiles.PROFILES:           # one checkout per repository, with what every profile needs from it
        source = profile.get('source') or {}
        if source.get('repo'):
            needed.setdefault((source['repo'], source['commit']), set()).update(source['paths'])
    for profile in sim_profiles.PROFILES:
        source = profile.get('source')
        if profile.get('duck'):                     # issue #63: its kit is built by scripts/duck_kit.py
            continue
        if not source or (only and profile['id'] not in only):
            continue
        try:
            if source.get('private'):
                base = private / source['private']
                if not base.is_dir():
                    raise PackError(f'没有这份不公开的模型（{base}）')
                origin = {'private': source['private']}
            else:
                base = fetch(source, offline, needed[(source['repo'], source['commit'])])
                origin = {'repo': 'https://github.com/' + source['repo'], 'commit': source['commit']}
            done.append(pack(profile, base, Path(out_dir), origin))
        except (PackError, ET.ParseError, OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError, struct.error) as exc:
            # A package from an earlier build is not what this build was asked for: it goes, and the app lists
            # this brand and model as not available.
            (Path(out_dir) / (profile['id'] + '.zip')).unlink(missing_ok=True)
            skipped.append({'id': profile['id'], 'reason': str(exc) or type(exc).__name__})
    if not only:
        write_notice(Path(out_dir))
    return done, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--private', default=None)
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    done, skipped = build(args.out, args.private, args.only, args.offline)
    for item in done:
        print(f'{item["id"]:22} {item["bytes"] / 1e6:6.1f} MB  {item["meshes"]} 个网格  {"；".join(item["notes"])}')
    for item in skipped:
        print(f'{item["id"]:22} 跳过：{item["reason"]}')
    return 0 if done or not skipped else 1


if __name__ == '__main__':
    sys.exit(main())
