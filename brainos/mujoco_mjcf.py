"""An MJCF robot model made ready for a simulation (issue #77, docs/MUJOCO_SIM.md): the console's side.

A robot model may be MJCF, MuJoCo's own format (one ``.xml``, or the main ``.xml`` of an archive with the
files it includes and its meshes), as well as a URDF. ``mujoco_model.prepare`` calls :func:`prepare`, which
writes ``<dest>/model.xml`` so that MuJoCo never opens a file this module did not write:

* ``<include>`` is replaced by the children of the file it names (taken from the same archive; anything else
  is dropped with a warning);
* ``<mesh file>`` points at ``meshes/m<N>.<ext>`` (.stl, .obj, .msh, as for a URDF); a mesh that is missing
  or cannot be loaded is taken out with the geoms that use it;
* every other element that names a file (textures, height fields, skins, models to attach, flex meshes) is
  taken out, with what refers to it by name; so is everything a robot does not need whose size is set by the file
  itself (built-in textures and height fields, flexes, ``<replicate>``, ``<composite>``, plugins, ``<size>``,
  ``<visual>``, ``<statistic>``): a few bytes of it can make MuJoCo allocate gigabytes;
* ``<compiler>`` keeps its meaning (angle, autolimits, ...); its directories are replaced by ``meshes``;
* an unnamed joint gets the name the cloud lists it under (``unnamed_joint_<n>``, cloud ``material_mjcf``).

What else the file holds besides the robot (floor, lights, other bodies), its free joint and its own
actuators are taken away when the simulation is built (``mujoco_mjcf_build``), on MuJoCo's own reading of the
file. This module imports no MuJoCo: the console's backend runs it, on an untrusted file.
"""
import posixpath
import re
from xml.etree import ElementTree as ET

from .mujoco_model import FORMAT, MAX_ELEMENTS, MAX_NAME, ModelError, _match, _parse_xml, write_mesh

MAX_INCLUDES = 200
MAX_INCLUDE_DEPTH = 8
ACTUATORS = ('general', 'motor', 'position', 'velocity', 'intvelocity', 'damper', 'cylinder', 'muscle', 'adhesion')
# What refers to a removed asset by name, per kind of asset: (element, attribute).
REFERENCES = {'mesh': (('geom', 'mesh'),), 'texture': (('material', 'texture'), ('layer', 'texture')), 'hfield': (('geom', 'hfield'),),
              'model': (('attach', 'model'),), 'skin': ()}
# Taken out wherever they are (review of issue #77: a built-in <hfield nrow="200000" ncol="200000"> took 11.5 GB).
DROP = ('hfield', 'texture', 'skin', 'deformable', 'flex', 'flexcomp', 'replicate', 'composite', 'extension', 'plugin',
        'model', 'attach', 'size', 'visual', 'statistic')
_ROOT = re.compile(rb'<mujoco[\s>/]')


def _joined(base_dir, reference):
    """The archive path a reference names, relative to ``base_dir``; ``None`` when absolute or leading out."""
    text = str(reference or '').replace('\\', '/').strip()
    if not text or '\x00' in text or text.startswith('/') or (len(text) > 1 and text[1] == ':') or '://' in text:
        return None
    path = posixpath.normpath(posixpath.join(base_dir, text))
    return None if path == '..' or path.startswith('../') else path


def _find(files, path):
    if path is None or not files:
        return None
    if path in files:
        return path
    lowered = path.lower()
    return next((name for name in files if name.lower() == lowered), None)


def _norm(name):
    return posixpath.normpath(name.replace('\\', '/'))


def mains(xmls):
    """The MJCF files among ``xmls`` ({archive path: bytes}) that no other one includes. An include is followed as
    :func:`_expand` follows it (from the main file's folder, as MuJoCo does, else from the including file's), so a
    file that a nested include reaches is not taken for a model of its own."""
    files, roots, reached = {_norm(name): name for name in xmls}, {}, set()
    for name, data in xmls.items():
        if _ROOT.search(data[:64 * 1024]):
            roots[_norm(name)] = [node.get('file') for node in _parse_xml(data, 'mujoco', 'MJCF').iter('include')]
    for start in roots:
        seen, queue = {start}, [start]
        while queue and len(seen) <= MAX_INCLUDES:
            current = queue.pop()
            for reference in roots[current]:
                target = _find(roots, _joined(posixpath.dirname(start), reference)) or \
                    _find(roots, _joined(posixpath.dirname(current), reference))
                if target and target not in seen:
                    seen.add(target)
                    queue.append(target)
        reached |= seen - {start}
    return [files[name] for name in roots if name not in reached]


def _expand(root, main, xmls, warnings):
    """``root`` with every ``<include>`` replaced by the children of the file it names."""
    files = {_norm(name): name for name in (xmls or {})}
    missing, count, total = [], [0], [sum(1 for _ in root.iter())]

    def walk(node, depth, chain):
        children = []
        for child in list(node):
            if child.tag != 'include':
                walk(child, depth, chain)
                children.append(child)
                continue
            reference = child.get('file', '')
            target = _find(files, _joined(posixpath.dirname(_norm(main or '')), reference)) or \
                _find(files, _joined(posixpath.dirname(chain[-1]), reference))
            if target is None or target in chain or depth >= MAX_INCLUDE_DEPTH or count[0] >= MAX_INCLUDES:
                missing.append(str(reference)[:120])
                continue
            count[0] += 1
            included = _parse_xml(xmls[files[target]], 'mujoco', 'MJCF')
            total[0] += sum(1 for _ in included.iter())
            if total[0] > MAX_ELEMENTS:                     # every include counts: one file included many times
                raise ModelError(f'MJCF 加上 <include> 的文件后元素超过 {MAX_ELEMENTS} 个')
            walk(included, depth + 1, chain + [target])
            children.extend(list(included))
        node[:] = children                                  # one assignment: removing one by one is quadratic

    walk(root, 0, [_norm(main or 'model.xml')])
    if missing:
        warnings.append(f'<include> 引用的 {len(missing)} 个文件没有一起上传或读不了（{"、".join(dict.fromkeys(missing))[:300]}），没有读')
    return root


# ------------------------------------------------------------------ defaults (the same reading as the cloud's)
def _defaults(root):
    classes = {}

    def read(node, parent):
        name = node.get('class') or ('main' if parent is None else None)
        if name is None:
            return
        if parent is None and name != 'main':
            parent = 'main'
        own = classes.setdefault(name, (parent, {}))[1]
        for child in node:
            if child.tag == 'default':
                read(child, name)
            else:
                own.setdefault('actuator' if child.tag in ACTUATORS else child.tag, {}).update(child.attrib)

    for node in root.findall('default'):
        read(node, None)
    return classes


def _attr(classes, node, key, name, cls):
    if name in node.attrib:
        return node.get(name)
    seen, cls = set(), cls or 'main'
    while cls is not None and cls not in seen:
        seen.add(cls)
        parent, own = classes.get(cls, (None, {}))
        if name in own.get(key, {}):
            return own[key][name]
        cls = parent
    return None


def _numbers(text, count):
    try:
        values = [float(v) for v in str(text).split()]
    except (TypeError, ValueError):
        return None
    return values[:count] if len(values) >= count and all(v == v and abs(v) != float('inf') for v in values[:count]) else None


# ------------------------------------------------------------------ files
def _asset_name(node):
    """The name MuJoCo gives an asset: its own, else its file name without directory and suffix."""
    if node.get('name'):
        return node.get('name')
    base = str(node.get('file') or '').replace('\\', '/').rsplit('/', 1)[-1]
    return base.rsplit('.', 1)[0] if '.' in base else base


def _drop_references(root, removed):
    """Take out what refers by name to an asset that was taken out (``removed``: {kind: names})."""
    pairs = [(tag, attribute, names) for kind, names in removed.items() for tag, attribute in REFERENCES.get(kind, ())]

    def visit(parent):
        keep = []
        for child in parent:
            hit = next((attribute for tag, attribute, names in pairs if child.tag == tag and child.get(attribute) in names), None)
            if hit and (child.tag == 'material' or parent.tag == 'default'):
                del child.attrib[hit]                           # a material keeps its colour; a default its other values
            elif hit:
                continue
            visit(child)
            keep.append(child)
        parent[:] = keep

    if pairs:
        visit(root)


def _files(root, main, members, dest, warnings):
    """Meshes into ``meshes/``; every other element that names a file, and every element of :data:`DROP`, is taken
    out. Returns how many meshes were written."""
    compilers = root.findall('compiler')
    folder = ''
    for compiler in compilers:
        folder = compiler.get('meshdir') or compiler.get('assetdir') or folder
        for key in ('meshdir', 'texturedir', 'assetdir'):
            compiler.attrib.pop(key, None)
    if not compilers:
        compilers = [ET.Element('compiler')]
        root.insert(0, compilers[0])
    compilers[0].set('meshdir', 'meshes')
    meshes_dir = dest / 'meshes'
    meshes_dir.mkdir()
    base = posixpath.dirname(_norm(main or ''))
    by_path = {_norm(name): name for name in members}
    written, unusable, dropped, removed = {}, set(), [], {}

    def mesh(node):
        """The mesh's file under meshes/, or None. An unnamed mesh keeps the name MuJoCo gave it (its file's)."""
        reference = node.get('file')
        path = _joined(base, posixpath.join(folder, str(reference).replace('\\', '/'))) if folder else _joined(base, reference)
        member = (by_path.get(path) if path else None) or _find({k: k for k in members}, path) or \
            (_match(reference, members) if members else None)
        target = write_mesh(member, members, meshes_dir, written, unusable)
        if target is None:
            dropped.append(str(reference).replace('\\', '/').rsplit('/', 1)[-1][:80])
            return None
        node.set('name', _asset_name(node))
        node.set('file', target)
        return target

    def visit(parent):
        keep = []
        for node in parent:
            named = [key for key in node.attrib if key.startswith('file')]
            if node.tag in DROP or (named and parent.tag != 'default' and not (node.tag == 'mesh' and named == ['file'] and mesh(node))):
                if parent.tag != 'default':
                    removed.setdefault(node.tag, set()).add(_asset_name(node))
                continue
            for key in named:                                   # left only in a default, which names no file of its own
                if key != 'file' or node.tag != 'mesh' or parent.tag == 'default':
                    del node.attrib[key]
            visit(node)
            keep.append(node)
        parent[:] = keep

    visit(root)
    _drop_references(root, removed)
    if dropped:
        names = list(dict.fromkeys(dropped))
        warnings.append(f'有 {len(names)} 个网格文件没有或读不了（例如 {"、".join(names[:5])}），仿真里对应的部分不显示、不参与碰撞')
    others = sorted(kind for kind in removed if kind != 'mesh')
    if others:
        warnings.append(f'MJCF 里的 {"、".join(others)} 仿真不用（只用刚体、关节和网格），已去掉')
    return len(written)


# ------------------------------------------------------------------ joints
def _joints(root, warnings):
    """Name every unnamed joint as the cloud lists it; the movable joints with their limits, in document order."""
    classes = _defaults(root)
    compiler = {}
    for node in root.findall('compiler'):
        compiler.update(node.attrib)
    degrees, autolimits = compiler.get('angle', 'degree') != 'radian', compiler.get('autolimits', 'true') != 'false'
    joints, counter, unnamed = [], [0], [0]
    found = []                                              # (joint element, childclass) in document order

    def walk(node, childclass):
        for child in node:
            if child.tag in ('body', 'frame'):
                walk(child, child.get('childclass', childclass))
            elif child.tag in ('joint', 'freejoint'):
                found.append((child, childclass))

    for node in root.findall('worldbody'):
        walk(node, None)
    taken = {node.get('name') for node, _ in found if node.get('name')}
    for node, childclass in found:
        cls = node.get('class') or childclass
        kind = 'free' if node.tag == 'freejoint' else (_attr(classes, node, 'joint', 'type', cls) or 'hinge')
        if kind == 'ball':
            continue
        if not node.get('name'):                            # the same name the cloud lists it under: never one in use
            unnamed[0] += 1
            counter[0] += 1
            while f'unnamed_{"free" if kind == "free" else "joint"}_{counter[0]}' in taken:
                counter[0] += 1
            node.set('name', f'unnamed_{"free" if kind == "free" else "joint"}_{counter[0]}')
            taken.add(node.get('name'))
        if kind not in ('hinge', 'slide'):
            continue
        span = _numbers(_attr(classes, node, 'joint', 'range', cls), 2)
        span = span if span is not None and span[0] < span[1] else None      # MuJoCo limits a joint only on a real range
        limited = _attr(classes, node, 'joint', 'limited', cls) or 'auto'
        item = {'name': node.get('name')[:MAX_NAME], 'type': 'prismatic' if kind == 'slide' else 'continuous',
                'lower': None, 'upper': None, 'velocity': None, 'effort': None}
        if span is not None and (limited == 'true' or (limited == 'auto' and autolimits)):
            scale = 3.141592653589793 / 180.0 if kind == 'hinge' and degrees else 1.0
            item['lower'], item['upper'] = span[0] * scale, span[1] * scale
            if kind == 'hinge':
                item['type'] = 'revolute'
        joints.append(item)
    if unnamed[0]:
        warnings.append(f'有 {unnamed[0]} 个关节没有名字，仿真里叫 unnamed_joint_<序号>')
    return joints


def prepare(main, xmls, data, members, dest, warnings):
    """Write ``dest/model.xml`` and ``dest/meshes/`` from the MJCF ``data`` (archive member ``main`` with the
    other model files ``xmls``, or a single file: ``main`` is ``None``). Returns what ``model.json`` holds."""
    root = _expand(_parse_xml(data, 'mujoco', 'MJCF'), main, xmls, warnings)
    meshes = _files(root, main, members or {}, dest, warnings)
    joints = _joints(root, warnings)
    bodies = sum(1 for _ in root.iter('body'))
    if not bodies:
        raise ModelError('MJCF 里没有刚体（body），没有机器人')
    (dest / 'model.xml').write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    return {'format': FORMAT, 'model_format': 'mjcf', 'robot_name': (root.get('model') or '')[:100], 'links': bodies,
            'meshes': meshes, 'joints': joints, 'warnings': warnings}
