"""MJCF, MuJoCo's own model format, read by rules (issue #77, docs/MODEL_LIBRARY.md).

A robot model may be an MJCF file (root ``<mujoco>``) instead of a URDF: one ``.xml``, or an archive with
its ``.xml`` files and meshes (the Microduck's model is one). What a person checks before the model is
used is the same as for a URDF: the bodies (links), the joints and their limits. They are read the way
MuJoCo reads them:

* ``<include file>`` is replaced by the children of the named file, when that file came in the same
  archive (MuJoCo takes the name relative to the main file's directory);
* an element takes each attribute it does not set from its default class (its ``class``, else the
  ``childclass`` of the nearest body above it, else ``main``), and a class from the classes around it;
* a hinge's range is in degrees unless ``<compiler angle="radian">``; a joint is limited when it says
  so, or (``autolimits``, MuJoCo's default) when it has a range;
* a joint's effort is its ``actuatorfrcrange``, else the largest ``forcerange`` of the actuators that
  drive it, else for a ``motor`` its control range times its gear. MJCF has no velocity limit.

Loaded by ``material_extract.py`` by its full path, inside the sandboxed process; standard library only.
The XML is read by that file's safe reader (``parse``: no DOCTYPE, bounded depth and size).
"""
import posixpath
import re

ACTUATORS = ("general", "motor", "position", "velocity", "intvelocity", "damper", "cylinder", "muscle", "adhesion")
MAX_INCLUDES = 200
MAX_INCLUDE_DEPTH = 8
MAX_NAME = 256
_ROOT = re.compile(rb"<mujoco[\s>/]")


class Refusal(Exception):
    """The model cannot be read; ``material_extract`` turns it into its own refusal."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.children = tag, dict(attrs), []


def tree(data, parse):
    """The element tree of one XML file. ``parse(data, on_start, on_end)`` is the caller's safe reader."""
    stack, roots = [], []

    def start(tag, attrs):
        node = Node(tag, attrs)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    def end(_tag):
        stack.pop()

    parse(data, start, end)
    return roots[0] if roots else None


def looks_like(data):
    """Whether a file may be MJCF (``<mujoco`` near its start); only such files are read in full."""
    return bool(_ROOT.search(data[:64 * 1024]))


def joined(base_dir, reference):
    """The archive path a reference names, relative to ``base_dir``; ``None`` for an absolute one or one
    that leads out of the archive."""
    text = str(reference or "").replace("\\", "/").strip()
    if not text or "\x00" in text or text.startswith("/") or (len(text) > 1 and text[1] == ":") or "://" in text:
        return None
    path = posixpath.normpath(posixpath.join(base_dir, text))
    return None if path == ".." or path.startswith("../") else path


def _find(files, path):
    if path is None:
        return None
    if path in files:
        return path
    lowered = path.lower()
    return next((name for name in files if name.lower() == lowered), None)


def _includes(node):
    for child in node.children:
        if child.tag == "include":
            yield child.attrs.get("file", "")
        else:
            yield from _includes(child)


def roots(files, parse):
    """``(MJCF files, files that one of them includes)`` among ``files`` ({archive path: bytes}). An include is
    followed as :func:`expand` follows it (from the main file's folder, as MuJoCo does, else from the including
    file's), so a file that only a nested include reaches is not taken for a model of its own."""
    found, included = {}, set()
    for name, data in files.items():
        if not looks_like(data):
            continue
        root = tree(data, parse)
        if root is not None and root.tag == "mujoco":
            found[name] = [reference for reference in _includes(root)]
    for start in found:
        seen, queue = {start}, [start]
        while queue and len(seen) <= MAX_INCLUDES:
            current = queue.pop()
            for reference in found[current]:
                target = _find(found, joined(posixpath.dirname(start), reference)) or \
                    _find(found, joined(posixpath.dirname(current), reference))
                if target and target not in seen:
                    seen.add(target)
                    queue.append(target)
        included |= seen - {start}
    return found, included


def _size(node):
    count, stack = 0, [node]
    while stack:
        current = stack.pop()
        count += 1
        stack.extend(current.children)
    return count


def expand(root, main, files, parse, notes, max_elements=200_000):
    """``root`` (of the file ``main``) with every ``<include>`` replaced by the children of the file it names.
    ``files`` is ``None`` for a single file: its includes cannot be read and are only counted. The elements of
    every included copy count against ``max_elements`` (one file can be included many times)."""
    missing, count, total = [], [0], [_size(root)]

    def walk(node, depth, chain):
        out = []
        for child in node.children:
            if child.tag != "include":
                walk(child, depth, chain)
                out.append(child)
                continue
            reference = child.attrs.get("file", "")
            target = None
            if files is not None:
                target = _find(files, joined(posixpath.dirname(main), reference)) or \
                    _find(files, joined(posixpath.dirname(chain[-1]), reference))
            if target is None or target in chain or depth >= MAX_INCLUDE_DEPTH or count[0] >= MAX_INCLUDES:
                missing.append(str(reference)[:120])
                continue
            count[0] += 1
            included = tree(files[target], parse)
            if included is None or included.tag != "mujoco":
                missing.append(str(reference)[:120])
                continue
            total[0] += _size(included)
            if total[0] > max_elements:
                raise Refusal("xml_too_large", f"加上 <include> 的文件后 XML 元素超过 {max_elements} 个，没有解析")
            out.extend(walk(included, depth + 1, chain + [target]))
        node.children = out
        return out

    walk(root, 0, [main])
    if missing:
        names = "、".join(dict.fromkeys(missing))
        if files is None:
            notes.append(f"这份 MJCF 用 <include> 引用了 {len(missing)} 个别的文件（{names[:300]}），单个 .xml 不带它们，没有读；"
                         "那些文件里的关节和刚体不在下面。要带上，请把它们和这个 .xml 打成一个压缩包上传")
        else:
            notes.append(f"<include> 引用的 {len(missing)} 个文件在压缩包里没有或读不了（{names[:300]}），没有读")
    return root


# ------------------------------------------------------------------ defaults
def _defaults(root):
    """{class: (parent class, {element: attributes})}; every actuator element shares one entry, as in MuJoCo."""
    classes = {}

    def read(node, parent):
        name = node.attrs.get("class") or ("main" if parent is None else None)
        if name is None:
            return                                  # MuJoCo refuses a nested default without a class
        if parent is None and name != "main":
            parent = "main"
        own = classes.setdefault(name, (parent, {}))[1]
        for child in node.children:
            if child.tag == "default":
                read(child, name)
            else:
                key = "actuator" if child.tag in ACTUATORS else child.tag
                own.setdefault(key, {}).update(child.attrs)

    for node in root.children:
        if node.tag == "default":
            read(node, None)
    return classes


def _attr(classes, node, key, name, cls):
    """The attribute ``name`` of ``node``: its own, else of its default class ``cls`` and the classes around it."""
    if name in node.attrs:
        return node.attrs[name]
    seen, cls = set(), cls or "main"
    while cls is not None and cls not in seen:
        seen.add(cls)
        parent, own = classes.get(cls, (None, {}))
        if name in own.get(key, {}):
            return own[key][name]
        cls = parent
    return None


def unnamed(kind, index):
    """The name an unnamed joint is listed under; the console gives it the same name in the simulation."""
    return f"unnamed_{kind}_{index}"


def _numbers(text, count):
    try:
        values = [float(v) for v in str(text).split()]
    except (TypeError, ValueError):
        return None
    if len(values) < count or any(v != v or abs(v) == float("inf") for v in values[:count]):
        return None
    return values[:count]


def _text(value):
    return f"{value:.6g}"


# ------------------------------------------------------------------ what a person checks
def facts(root, notes):
    """Bodies, joints and their limits of an expanded MJCF tree, in the shape of a URDF's facts."""
    compiler = {}
    for node in root.children:
        if node.tag == "compiler":
            compiler.update(node.attrs)
    degrees = compiler.get("angle", "degree") != "radian"
    autolimits = compiler.get("autolimits", "true") != "false"
    meshdir = compiler.get("meshdir") or compiler.get("assetdir") or ""
    classes = _defaults(root)
    links, joints, counts = [], [], {"unnamed": 0, "ball": 0, "scene": 0, "joints": 0, "unlimited": 0}
    found, taken = [], set()                               # joints in document order; the names in use

    def joint(node, body, parent, cls):
        kind = node.attrs.get("type") if node.tag == "joint" else "free"
        if node.tag == "joint" and kind is None:
            kind = _attr(classes, node, "joint", "type", node.attrs.get("class") or cls) or "hinge"
        if kind == "ball":
            counts["ball"] += 1
            return
        name = node.attrs.get("name")
        if not name:                                     # never a name in use; the console names it the same way
            counts["joints"] += 1
            while unnamed("free" if kind == "free" else "joint", counts["joints"]) in taken:
                counts["joints"] += 1
            name = unnamed("free" if kind == "free" else "joint", counts["joints"])
            taken.add(name)
        item = {"name": str(name)[:MAX_NAME], "type": "floating", "parent": parent, "child": body, "raw": {}}
        if kind in ("hinge", "slide"):
            item["unstated"] = ["velocity"]          # MJCF has no velocity limit: not asked for
            own = node.attrs.get("class") or cls
            span = _numbers(_attr(classes, node, "joint", "range", own), 2)
            span = span if span is not None and span[0] < span[1] else None   # MuJoCo limits a joint only on a real range
            limited = _attr(classes, node, "joint", "limited", own)
            if span is not None and limited is None and not autolimits:
                counts["unlimited"] += 1
            if span is not None and (limited == "true" or (limited in (None, "auto") and autolimits)):
                scale = 3.141592653589793 / 180.0 if kind == "hinge" and degrees else 1.0
                item["lower"], item["upper"] = _text(span[0] * scale), _text(span[1] * scale)
                item["type"] = "revolute" if kind == "hinge" else "prismatic"
            else:
                item["type"] = "continuous" if kind == "hinge" else "prismatic"
            force = _numbers(_attr(classes, node, "joint", "actuatorfrcrange", own), 2)
            if force is not None and force[0] < force[1] and \
                    (_attr(classes, node, "joint", "actuatorfrclimited", own) or "auto") != "false":
                item["effort"] = _text(max(abs(force[0]), abs(force[1])))
        if not item["raw"]:
            item.pop("raw")
        joints.append(item)

    def walk(node, body, parent, childclass):
        for child in node.children:
            if child.tag == "body":
                name = child.attrs.get("name")
                if name:
                    links.append(str(name)[:MAX_NAME])
                else:
                    counts["unnamed"] += 1
                walk(child, str(name or "")[:MAX_NAME], body, child.attrs.get("childclass", childclass))
            elif child.tag == "frame":
                walk(child, body, parent, child.attrs.get("childclass", childclass))
            elif child.tag in ("joint", "freejoint"):
                found.append((child, body, parent, childclass))
                if child.attrs.get("name"):
                    taken.add(child.attrs["name"])
            elif body == "world" and child.tag in ("geom", "light", "site", "camera"):
                counts["scene"] += 1

    for node in root.children:
        if node.tag == "worldbody":
            walk(node, "world", "", None)
    for found_joint in found:
        joint(*found_joint)
    _efforts(root, classes, joints)
    meshes = [joined(meshdir, child.attrs["file"]) or child.attrs["file"]
              for node in root.children if node.tag == "asset" for child in node.children
              if child.tag == "mesh" and child.attrs.get("file")]
    if counts["unnamed"]:
        notes.append(f"有 {counts['unnamed']} 个刚体（body）没有名字，没有列在连杆里")
    if counts["ball"]:
        notes.append(f"有 {counts['ball']} 个球关节（ball），下面没有列出；仿真不给它们加驱动")
    if counts["scene"]:
        notes.append("文件里有地面、灯光等场景内容：仿真只取机器人本身，放进自己的环境里")
    if counts["unlimited"]:
        notes.append(f"<compiler autolimits=\"false\"> 时，有 {counts['unlimited']} 个关节写了 range 却没写 limited：MuJoCo 不接受这样的文件，"
                     "仿真会建不起来；请给它们写上 limited=\"true\" 后重新上传")
    return {"robot_name": root.attrs.get("model"), "links": links, "joints": joints, "warnings": notes,
            "mesh_files": meshes[:2000], "model_format": "mjcf"}


def _efforts(root, classes, joints):
    """Joints without an ``actuatorfrcrange`` take the effort of the actuators that drive them."""
    best = {}
    for node in root.children:
        if node.tag != "actuator":
            continue
        for child in node.children:
            target = child.attrs.get("joint") or child.attrs.get("jointinparent")
            if child.tag not in ACTUATORS or not target:
                continue
            cls = child.attrs.get("class") or "main"
            force = _numbers(_attr(classes, child, "actuator", "forcerange", cls), 2)
            limited = _attr(classes, child, "actuator", "forcelimited", cls) or "auto"
            value = None
            if force is not None and force[0] < force[1] and limited != "false":      # as MuJoCo: only a real range
                value = max(abs(force[0]), abs(force[1]))
            elif child.tag == "motor":
                control = _numbers(_attr(classes, child, "actuator", "ctrlrange", cls), 2)
                gear = _numbers(_attr(classes, child, "actuator", "gear", cls) or "1", 1)
                if control is not None and control[0] < control[1] and gear is not None and \
                        (_attr(classes, child, "actuator", "ctrllimited", cls) or "auto") != "false":
                    value = max(abs(control[0]), abs(control[1])) * abs(gear[0])
            if value is not None and value > 0:
                best[target] = max(best.get(target, 0.0), value)
    for item in joints:
        if "effort" not in item and item["name"] in best:
            item["effort"] = _text(best[item["name"]])
