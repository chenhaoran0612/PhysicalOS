"""The description of a simulation environment (issue #51, docs/MUJOCO_SIM.md).

A scene is a small JSON document, ``neuxmind.mujoco-scene.v1``: the floor, where the robot starts,
simple objects (boxes, spheres, cylinders, capsules; fixed or movable) and named places. The engineering
agent writes it from an application's description; a person looks at the result and confirms it; the
simulation builds the MuJoCo model from it together with the robot's URDF (``mujoco_build.py``).

The agent never writes MuJoCo XML: XML could name files, plugins and includes. This module is the only
reader of what the agent wrote, and it accepts nothing but the fields below, each bounded. It imports
neither MuJoCo nor numpy, so the console itself can check a scene.
"""
import json
import math
import re

SCHEMA = 'neuxmind.mujoco-scene.v1'
MAX_BYTES = 256 * 1024
MAX_OBJECTS = 200
MAX_PLACES = 50
MAX_FLOOR = 50.0            # half extent, metres
MAX_COORD = 50.0
MAX_HEIGHT = 10.0
MAX_SIZE = 20.0
MIN_SIZE = 0.005
MAX_MASS = 500.0
SHAPES = {'box': 3, 'sphere': 1, 'cylinder': 2, 'capsule': 2}     # how many numbers ``size`` holds
SHAPE_SIZES = {'box': '长、宽、高', 'sphere': '半径', 'cylinder': '半径、高', 'capsule': '半径、圆柱段高'}
SHAPE_NAMES = {'box': '方块', 'sphere': '球', 'cylinder': '圆柱', 'capsule': '胶囊'}
NAME = re.compile(r'[0-9A-Za-z_\-一-鿿][0-9A-Za-z_\- 一-鿿]{0,39}')
COLOR = re.compile(r'#[0-9a-fA-F]{6}')
DEFAULT_COLOR = '#8a8f98'
EMPTY = {'schema': SCHEMA, 'summary': '空场地：只有地面和机器人。', 'floor': {'size': [5.0, 5.0]},
         'robot': {'x': 0.0, 'y': 0.0, 'yaw': 0.0}, 'objects': [], 'places': []}


class SceneError(ValueError):
    """The scene cannot be used; the message says which field and why, in words for the page."""


def _number(value, what, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SceneError(f'{what}须为数字')
    if not low <= value <= high:
        raise SceneError(f'{what}须在 {low:g} 到 {high:g} 之间（现在是 {value:g}）')
    return float(value)


def _numbers(value, what, count, low, high):
    if not isinstance(value, list) or len(value) != count:
        raise SceneError(f'{what}须为 {count} 个数字')
    return [_number(v, what, low, high) for v in value]


def _name(value, what):
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()):
        raise SceneError(f'{what}须为 1–40 个字的名称（字母、数字、汉字、空格、下划线、连字符）')
    return value.strip()


def _only(source, allowed, what):
    if not isinstance(source, dict):
        raise SceneError(f'{what}须为对象')
    extra = sorted(set(source) - set(allowed))
    if extra:
        raise SceneError(f'{what}里有不认识的字段：{"、".join(str(k)[:40] for k in extra[:5])}')
    return source


def _object(raw, index, taken):
    what = f'第 {index + 1} 个物体'
    raw = _only(raw, ('name', 'shape', 'size', 'pos', 'yaw', 'color', 'movable', 'mass'), what)
    name = _name(raw.get('name'), what + '的名称')
    if name in taken:
        raise SceneError(f'物体名称重复：{name}')
    shape = raw.get('shape')
    if shape not in SHAPES:
        raise SceneError(f'{what}（{name}）的形状须为 {" / ".join(SHAPES)} 之一')
    size = _numbers(raw.get('size'), f'{name} 的尺寸（{SHAPE_SIZES[shape]}，米）', SHAPES[shape], MIN_SIZE, MAX_SIZE)
    pos = raw.get('pos')
    if not isinstance(pos, list) or len(pos) != 3:
        raise SceneError(f'{name} 的位置须为 3 个数字（x、y、z，米；z 是物体中心离地面的高度）')
    pos = [_number(pos[0], f'{name} 的 x', -MAX_COORD, MAX_COORD), _number(pos[1], f'{name} 的 y', -MAX_COORD, MAX_COORD),
           _number(pos[2], f'{name} 的 z', 0.0, MAX_HEIGHT)]
    color = raw.get('color', DEFAULT_COLOR)
    if not isinstance(color, str) or not COLOR.fullmatch(color):
        raise SceneError(f'{name} 的颜色须为 #rrggbb')
    movable = raw.get('movable', False)
    if not isinstance(movable, bool):
        raise SceneError(f'{name} 的 movable 须为 true 或 false')
    item = {'name': name, 'shape': shape, 'size': size, 'pos': pos,
            'yaw': _number(raw.get('yaw', 0.0), f'{name} 的朝向（弧度）', -math.tau, math.tau),
            'color': color.lower(), 'movable': movable}
    if movable:
        item['mass'] = _number(raw.get('mass', 1.0), f'{name} 的质量（千克）', 0.001, MAX_MASS)
    elif 'mass' in raw:
        raise SceneError(f'{name} 是固定的物体，不需要质量')
    return item


def validate(raw):
    """The scene with every field checked and defaults filled in. Raises :class:`SceneError`."""
    raw = _only(raw, ('schema', 'summary', 'floor', 'robot', 'objects', 'places'), '环境描述')
    if raw.get('schema') != SCHEMA:
        raise SceneError(f'schema 须为 {SCHEMA}')
    summary = raw.get('summary', '')
    if not isinstance(summary, str) or len(summary) > 500 or any(ord(ch) < 32 and ch not in '\n\t' for ch in summary):
        raise SceneError('summary 须为不超过 500 个字的说明')
    floor = _only(raw.get('floor', EMPTY['floor']), ('size',), '地面')
    robot = _only(raw.get('robot', EMPTY['robot']), ('x', 'y', 'yaw'), '机器人起点')
    objects = raw.get('objects', [])
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise SceneError(f'objects 须为不超过 {MAX_OBJECTS} 个物体的列表')
    cleaned, taken = [], set()
    for index, item in enumerate(objects):
        cleaned.append(_object(item, index, taken))
        taken.add(cleaned[-1]['name'])
    places = raw.get('places', [])
    if not isinstance(places, list) or len(places) > MAX_PLACES:
        raise SceneError(f'places 须为不超过 {MAX_PLACES} 个地点的列表')
    named, seen = [], set()
    for index, place in enumerate(places):
        place = _only(place, ('name', 'x', 'y'), f'第 {index + 1} 个地点')
        name = _name(place.get('name'), f'第 {index + 1} 个地点的名称')
        if name in seen:
            raise SceneError(f'地点名称重复：{name}')
        seen.add(name)
        named.append({'name': name, 'x': _number(place.get('x'), f'{name} 的 x', -MAX_COORD, MAX_COORD),
                      'y': _number(place.get('y'), f'{name} 的 y', -MAX_COORD, MAX_COORD)})
    return {'schema': SCHEMA, 'summary': summary.strip(),
            'floor': {'size': _numbers(floor.get('size', EMPTY['floor']['size']), '地面的半长和半宽（米）', 2, 0.5, MAX_FLOOR)},
            'robot': {'x': _number(robot.get('x', 0.0), '机器人起点的 x', -MAX_COORD, MAX_COORD),
                      'y': _number(robot.get('y', 0.0), '机器人起点的 y', -MAX_COORD, MAX_COORD),
                      'yaw': _number(robot.get('yaw', 0.0), '机器人起点的朝向（弧度）', -math.tau, math.tau)},
            'objects': cleaned, 'places': named}


def parse(text):
    """Validate the JSON text of a scene (what the agent wrote, or what is read back from disk)."""
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            raise SceneError(f'环境描述超过 {MAX_BYTES // 1024} KB')
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            raise SceneError('环境描述不是 UTF-8 文本') from None
    if not isinstance(text, str) or len(text) > MAX_BYTES:
        raise SceneError(f'环境描述超过 {MAX_BYTES // 1024} KB')
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        raise SceneError('环境描述不是合法的 JSON') from None
    return validate(raw)


def describe(scene):
    """One line per thing, for a person who is about to confirm the scene."""
    lines = [f'地面 {scene["floor"]["size"][0] * 2:g} × {scene["floor"]["size"][1] * 2:g} 米；'
             f'机器人从 ({scene["robot"]["x"]:g}, {scene["robot"]["y"]:g}) 出发']
    for item in scene['objects']:
        size = ' × '.join(f'{v:g}' for v in item['size'])
        kind = '可移动' + (f'，{item["mass"]:g} kg' if item.get('mass') else '') if item['movable'] else '固定'
        lines.append(f'{item["name"]}：{SHAPE_NAMES[item["shape"]]} {size} 米，位于 ({item["pos"][0]:g}, {item["pos"][1]:g}, {item["pos"][2]:g})，{kind}')
    for place in scene['places']:
        lines.append(f'地点 {place["name"]}：({place["x"]:g}, {place["y"]:g})')
    return lines
