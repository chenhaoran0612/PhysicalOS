"""From a robot model in the library to files a simulation can load (issue #51, docs/MUJOCO_SIM.md).

A robot model (library kind ``model``) is one ``.urdf``, or a ``.zip`` / ``.tar.gz`` holding one
``.urdf`` and its meshes. :func:`prepare` turns the downloaded file into a directory::

    <dest>/model.urdf        the URDF, rewritten: mesh names point into meshes/, what cannot be loaded is taken out
    <dest>/meshes/m<N>.<ext> the mesh files the URDF uses (.stl, .obj, .msh)
    <dest>/model.json        robot name, joints with their limits, warnings

The file is untrusted. Nothing is extracted by name: only members that end in ``.urdf`` or a mesh suffix
are read, each into a file name chosen here, within size and count limits; links, devices and names that
lead out of the archive are never followed. The URDF is parsed with DOCTYPE and entities refused. Only the
standard library is used; MuJoCo reads the result later, in the simulation's own process.
"""
import gzip
import io
import json
import os
import shutil
import struct
import tarfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

MESH_SUFFIXES = ('.stl', '.obj', '.msh')
MAX_URDF_BYTES = 32 * 1024 * 1024
MAX_MESH_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_MESHES = 2000
MAX_ELEMENTS = 200_000
MAX_STL_FACES = 2_000_000
MAX_ASCII_STL_BYTES = 64 * 1024 * 1024     # a text STL is converted here, line by line: a bound on that work
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024  # everything a tar.gz inflates to, the members that are skipped included
MAX_DEPTH = 64
MOVABLE = ('revolute', 'continuous', 'prismatic')
MAX_NAME = 200
COMPILER = {'meshdir': 'meshes', 'strippath': 'false', 'discardvisual': 'false', 'fusestatic': 'false',
            'balanceinertia': 'true', 'boundmass': '0.001', 'boundinertia': '0.000001'}
FORMAT = 2                      # bump when what prepare() writes changes: older directories are rebuilt
MODEL_SUFFIXES = ('.urdf', '.xml')     # issue #77: an MJCF model is an .xml (mujoco_mjcf.py)


class ModelError(ValueError):
    """The robot model cannot be used for a simulation; the message says why."""


def _unsafe(name):
    clean = name.replace('\\', '/')
    return '\x00' in clean or clean.startswith('/') or (len(clean) > 1 and clean[1] == ':') or '..' in clean.split('/')


def _wanted(name):
    lower = name.lower()
    return lower.endswith(MODEL_SUFFIXES) or lower.endswith(MESH_SUFFIXES)


def _limit(name):
    """The largest a member may be: a model file (URDF, issue #77: or MJCF) or a mesh."""
    return MAX_URDF_BYTES if name.lower().endswith(MODEL_SUFFIXES) else MAX_MESH_BYTES


class _Budget:
    def __init__(self):
        self.total = 0
        self.members = 0

    def member(self):
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise ModelError(f'压缩包里的成员超过 {MAX_MEMBERS} 个')

    def room(self):
        """How much one more member may be: what is left of the total, so it is never read past it."""
        return max(0, MAX_TOTAL_BYTES - self.total)

    def take(self, name, size):
        limit = _limit(name)
        if size > limit:
            raise ModelError(f'压缩包里的 {name[:80]} 太大（{size} 字节，上限 {limit}）')
        self.total += size
        if self.total > MAX_TOTAL_BYTES:
            raise ModelError(f'压缩包解开后超过 {MAX_TOTAL_BYTES // (1024 * 1024)} MB')


def _read_capped(stream, name, budget):
    limit = _limit(name)
    data = stream.read(min(limit, budget.room()) + 1)
    budget.take(name, len(data))
    return data


def _zip_members(path, budget):
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                budget.member()
                name = info.filename
                mode = (info.external_attr >> 16) & 0o170000
                if info.is_dir() or _unsafe(name) or not _wanted(name) or mode == 0o120000 or info.flag_bits & 1:
                    continue
                if info.file_size > _limit(name):
                    raise ModelError(f'压缩包里的 {name[:80]} 太大')
                with archive.open(info) as member:
                    yield name.replace('\\', '/'), _read_capped(member, name, budget)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, ValueError) as exc:
        if isinstance(exc, ModelError):
            raise
        raise ModelError('压缩包读不出来（已损坏，或不是 zip）') from None


class _Inflated:
    """The inflated bytes of a ``.gz`` file, refused beyond a total: members that are skipped cost time too."""

    def __init__(self, path):
        self.stream, self.total = gzip.open(path, 'rb'), 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.total += len(data)
        if self.total > MAX_UNPACKED_BYTES:
            raise ModelError(f'压缩包解开后超过 {MAX_UNPACKED_BYTES // (1024 ** 3)} GB，没有读完')
        return data

    def close(self):
        self.stream.close()


def _tar_members(path, budget):
    inflated = None
    try:
        inflated = _Inflated(path)
        with tarfile.open(fileobj=inflated, mode='r|') as archive:
            for info in archive:
                budget.member()
                if not info.isreg() or _unsafe(info.name) or not _wanted(info.name):
                    continue
                member = archive.extractfile(info)
                if member is None:
                    continue
                yield info.name.replace('\\', '/'), _read_capped(member, info.name, budget)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        if isinstance(exc, ModelError):
            raise
        raise ModelError('压缩包读不出来（已损坏，或不是 tar.gz）') from None
    finally:
        if inflated is not None:
            inflated.close()


# ------------------------------------------------------------------ meshes
def _binary_stl(data):
    """``data`` as a binary STL MuJoCo can read, or ``None``. An ASCII STL is converted."""
    if len(data) >= 84:
        faces = struct.unpack_from('<I', data, 80)[0]
        if len(data) == 84 + 50 * faces:
            return data if 1 <= faces <= MAX_STL_FACES else None
    head = data[:512].lstrip().lower()
    if not head.startswith(b'solid') or len(data) > MAX_ASCII_STL_BYTES:
        return None
    vertices = []
    for line in io.BytesIO(data):               # one line at a time: no list of every line of a hostile file
        if len(line) > 4096:
            return None
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == b'vertex':
            try:
                vertices.append(tuple(float(v) for v in parts[1:]))
            except ValueError:
                return None
            if len(vertices) > 3 * MAX_STL_FACES:
                return None
    if not vertices or len(vertices) % 3:
        return None
    out = io.BytesIO()
    out.write(b'converted from ASCII STL'.ljust(80, b' '))
    out.write(struct.pack('<I', len(vertices) // 3))
    for index in range(0, len(vertices), 3):
        out.write(struct.pack('<12fH', 0.0, 0.0, 0.0, *vertices[index], *vertices[index + 1], *vertices[index + 2], 0))
    return out.getvalue()


def _reference_parts(reference):
    """The path components of a URDF mesh reference, without ``package://name/`` or ``file://``."""
    text = reference.strip().replace('\\', '/')
    if text.startswith('package://'):
        text = text[len('package://'):].split('/', 1)[-1] if '/' in text[len('package://'):] else ''
    elif '://' in text:
        text = text.split('://', 1)[1]
    return [part for part in text.split('/') if part and part not in ('.', '..')]


def _match(reference, members):
    """The archive member a mesh reference means: the longest matching path ending; else a unique file name."""
    parts = [p.lower() for p in _reference_parts(reference)]
    if not parts:
        return None
    best, best_len = [], 0
    for name in members:
        own = [p.lower() for p in name.split('/') if p]
        length = 0
        while length < min(len(own), len(parts)) and own[-1 - length] == parts[-1 - length]:
            length += 1
        if length > best_len:
            best, best_len = [name], length
        elif length == best_len and length:
            best.append(name)
    return best[0] if best_len and (len(best) == 1 or best_len > 1) else None


# ------------------------------------------------------------------ URDF
def _parse_xml(data, root_tag='robot', what='URDF'):
    """The element tree of a model file; DOCTYPE and entities refused, size and depth bounded.
    ``root_tag``/``what``: issue #77 reads MJCF (``mujoco``) with it as well."""
    builder, parser, count, depth = ET.TreeBuilder(), expat.ParserCreate(), [0], [0]

    def refuse(*_args):
        raise ModelError(f'{what} 里有 DOCTYPE 或实体声明，出于安全没有读取')

    def start(name, attrs):
        count[0] += 1
        depth[0] += 1
        if count[0] > MAX_ELEMENTS:
            raise ModelError(f'{what} 的元素超过 {MAX_ELEMENTS} 个')
        if depth[0] > MAX_DEPTH:
            raise ModelError(f'{what} 的 XML 嵌套超过 {MAX_DEPTH} 层')
        builder.start(name, attrs)

    def end(name):
        depth[0] -= 1
        builder.end(name)
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = refuse
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, builder.data
    try:
        parser.Parse(data, True)
        root = builder.close()
    except (expat.ExpatError, AssertionError, RecursionError) as exc:
        raise ModelError(f'{what} 的 XML 格式不对：{exc}') from None
    if root.tag != root_tag:
        raise ModelError(f'这个文件的根元素不是 {what} 的 <{root_tag}>')
    return root


def _float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float('inf') else None


def _joints(root):
    joints = []
    for joint in root.findall('joint'):
        kind = joint.get('type', '')
        if kind not in MOVABLE or not joint.get('name'):
            continue
        limit = joint.find('limit')
        limit = limit.attrib if limit is not None else {}
        mimic = joint.find('mimic')
        item = {'name': joint.get('name')[:MAX_NAME], 'type': kind,
                'parent': (joint.find('parent').get('link', '') if joint.find('parent') is not None else ''),
                'child': (joint.find('child').get('link', '') if joint.find('child') is not None else ''),
                **{key: _float(limit.get(key)) for key in ('lower', 'upper', 'velocity', 'effort')}}
        if mimic is not None and mimic.get('joint'):
            multiplier = _float(mimic.get('multiplier', '1'))
            item['mimic'] = {'joint': mimic.get('joint'), 'multiplier': 1.0 if multiplier is None else multiplier,
                             'offset': _float(mimic.get('offset', '0')) or 0.0}
        joints.append(item)
    return joints


def write_mesh(member, members, meshes_dir, written, unusable):
    """The file under ``meshes/`` that archive member ``member`` became (written once), or ``None`` when it is
    missing or cannot be loaded. ``written``/``unusable`` carry what was decided already."""
    target = written.get(member) if member else None
    if member and target is None and member not in unusable and len(written) < MAX_MESHES:
        suffix = '.' + member.lower().rsplit('.', 1)[-1]
        data = members[member]
        if suffix == '.stl':
            data = _binary_stl(data)
        if data and suffix in MESH_SUFFIXES:
            target = f'm{len(written)}{suffix}'
            (meshes_dir / target).write_bytes(data)
            written[member] = target
        else:
            unusable.add(member)
    return target


def _rewrite(root, members, meshes_dir, warnings):
    """Point every mesh at a file under ``meshes/``; take out the shapes whose mesh cannot be loaded."""
    written, dropped, unusable = {}, [], set()       # a member that cannot be used is looked at once, however often it is named
    for link in root.findall('link'):
        for shape in list(link):
            if shape.tag not in ('visual', 'collision'):
                continue
            mesh = shape.find('geometry/mesh')
            if mesh is None:
                continue
            reference = mesh.get('filename', '')
            member = _match(reference, members) if members else None
            target = write_mesh(member, members, meshes_dir, written, unusable)
            if target is None:
                link.remove(shape)
                dropped.append(reference.replace('\\', '/').rsplit('/', 1)[-1][:80])
            else:
                mesh.set('filename', target)
    if dropped:
        names = list(dict.fromkeys(dropped))
        warnings.append(f'有 {len(names)} 个网格文件没有或读不了（例如 {"、".join(names[:5])}），仿真里对应的部分不显示、不参与碰撞')
    return len(written)


def _prepare_urdf(data, members, dest, warnings):
    root = _parse_xml(data)
    meshes_dir = dest / 'meshes'
    meshes_dir.mkdir()
    for extra in [child for child in root if child.tag in ('mujoco', 'gazebo', 'transmission')]:
        root.remove(extra)
    meshes = _rewrite(root, members, meshes_dir, warnings)
    links = [link.get('name') for link in root.findall('link') if link.get('name')]
    if not links:
        raise ModelError('URDF 里没有连杆（link）')
    extension = ET.SubElement(root, 'mujoco')
    ET.SubElement(extension, 'compiler', COMPILER)
    (dest / 'model.urdf').write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    return {'format': FORMAT, 'robot_name': (root.get('name') or '')[:100], 'links': len(links), 'meshes': meshes,
            'joints': _joints(root), 'warnings': warnings}


def _model_of(fmt, source):
    """``(format, main file name, its bytes, other model-file bytes, mesh members)`` of a downloaded model.
    Issue #77: an MJCF ``.xml`` (``fmt`` ``mjcf``, or the one in an archive that no other includes) as well."""
    if fmt in ('urdf', 'mjcf'):
        if source.stat().st_size > MAX_URDF_BYTES:
            raise ModelError('模型文件太大')
        return fmt, None, source.read_bytes(), None, {}
    if fmt not in ('zip', 'tar.gz'):
        raise ModelError(f'机器人模型不能是 {fmt} 文件（仿真只能读 URDF 和 MJCF）')
    budget, urdfs, xmls, members = _Budget(), {}, {}, {}
    for name, data in (_zip_members if fmt == 'zip' else _tar_members)(source, budget):
        lower = name.lower()
        (urdfs if lower.endswith('.urdf') else xmls if lower.endswith('.xml') else members)[name] = data
    found = list(urdfs) + mujoco_mjcf.mains(xmls)
    if len(found) != 1:
        raise ModelError('压缩包里没有 .urdf 或 MJCF（.xml）文件（仿真只能读这两种）' if not found else
                         f'压缩包里有 {len(found)} 个模型文件（{"、".join(n[:60] for n in found[:5])}），只能有一个')
    main = found[0]
    return ('urdf' if main in urdfs else 'mjcf'), main, urdfs.get(main) or xmls[main], xmls, members


def prepare(source, fmt, dest):
    """Build the model directory ``dest`` from the downloaded file ``source`` (library format ``fmt``:
    ``urdf``, ``mjcf`` (issue #77), ``zip`` or ``tar.gz``). Returns what ``model.json`` holds. ``dest`` must not exist."""
    source, dest = Path(source), Path(dest)
    work = dest.with_name(dest.name + '.part')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    try:
        warnings = []
        kind, main, data, xmls, members = _model_of(fmt, source)
        if kind == 'mjcf':
            meta = mujoco_mjcf.prepare(main, xmls, data, members, work, warnings)
        else:
            meta = _prepare_urdf(data, members, work, warnings)
        (work / 'model.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(work, dest)
        return meta
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def load(dest):
    """``model.json`` of a prepared directory, or ``None`` when it is missing, incomplete or of an older format."""
    dest = Path(dest)
    try:
        meta = json.loads((dest / 'model.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get('format') != FORMAT or not (dest / model_file(meta)).is_file():
        return None
    return meta


def model_file(meta):
    """The file of a prepared directory MuJoCo reads: ``model.urdf``, or (issue #77) ``model.xml`` for MJCF."""
    return 'model.xml' if isinstance(meta, dict) and meta.get('model_format') == 'mjcf' else 'model.urdf'


from . import mujoco_mjcf  # noqa: E402  (issue #77; it uses the helpers above, so it is imported after them)
