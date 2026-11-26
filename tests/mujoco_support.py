"""Made-up robot models and scenes for the simulation tests (issue #51). Nothing here is a real robot."""
import gzip
import io
import struct
import tarfile
import zipfile

SCENE = 'neuxmind.mujoco-scene.v1'


def _inertial(mass, xyz='0 0 0'):
    i = mass * 0.01
    return (f'<inertial><origin xyz="{xyz}"/><mass value="{mass}"/>'
            f'<inertia ixx="{i}" iyy="{i}" izz="{i}" ixy="0" ixz="0" iyz="0"/></inertial>')


def dog_urdf(mesh='package://demo_dog/meshes/visual/trunk.stl'):
    """A trunk on four two-joint legs. Standing, its feet are 0.4 m below the trunk's centre."""
    visual = f'<visual><geometry><mesh filename="{mesh}"/></geometry></visual>' if mesh else ''
    parts = [f'<link name="trunk">{_inertial(8)}{visual}'
             '<collision><geometry><box size="0.5 0.25 0.12"/></geometry></collision></link>']
    for leg, (x, y) in {'fl': (0.2, 0.12), 'fr': (0.2, -0.12), 'rl': (-0.2, 0.12), 'rr': (-0.2, -0.12)}.items():
        for part, parent, origin in (('thigh', 'trunk', f'{x} {y} 0'), ('calf', f'{leg}_thigh', '0 0 -0.2')):
            parts.append(f'<link name="{leg}_{part}">{_inertial(0.6, "0 0 -0.1")}'
                         '<collision><origin xyz="0 0 -0.1"/><geometry><cylinder radius="0.02" length="0.2"/></geometry>'
                         '</collision></link>')
            parts.append(f'<joint name="{leg}_{part}_joint" type="revolute"><parent link="{parent}"/>'
                         f'<child link="{leg}_{part}"/><origin xyz="{origin}"/><axis xyz="0 1 0"/>'
                         '<limit lower="-1.2" upper="1.2" velocity="8" effort="30"/></joint>')
    return ('<?xml version="1.0"?>\n<robot name="demo_dog">' + ''.join(parts) + '</robot>').encode()


def arm_urdf(fingers=True):
    """Three joints in a column (0.9 m reach upwards), and a gripper of two fingers, one following the other."""
    parts = [f'<link name="base">{_inertial(4)}<collision><geometry><cylinder radius="0.08" length="0.1"/></geometry>'
             '</collision></link>']
    parent = 'base'
    for index, axis in enumerate(('0 0 1', '0 1 0', '0 1 0'), start=1):
        parts.append(f'<link name="link{index}">{_inertial(1.5, "0 0 0.15")}<collision><origin xyz="0 0 0.15"/>'
                     '<geometry><cylinder radius="0.04" length="0.3"/></geometry></collision></link>')
        parts.append(f'<joint name="joint{index}" type="revolute"><parent link="{parent}"/><child link="link{index}"/>'
                     f'<origin xyz="0 0 {0.05 if index == 1 else 0.3}"/><axis xyz="{axis}"/>'
                     '<limit lower="-2.0" upper="2.0" velocity="2" effort="60"/></joint>')
        parent = f'link{index}'
    if fingers:
        for name, sign in (('left', 1), ('right', -1)):
            parts.append(f'<link name="{name}_finger">{_inertial(0.1)}<collision><geometry><box size="0.02 0.01 0.06"/>'
                         '</geometry></collision></link>')
            mimic = '<mimic joint="left_finger_joint" multiplier="1" offset="0"/>' if name == 'right' else ''
            parts.append(f'<joint name="{name}_finger_joint" type="prismatic"><parent link="link3"/>'
                         f'<child link="{name}_finger"/><origin xyz="0 0 0.33"/><axis xyz="0 {sign} 0"/>'
                         f'<limit lower="0" upper="0.04" velocity="0.1" effort="20"/>{mimic}</joint>')
    return ('<?xml version="1.0"?>\n<robot name="demo_arm">' + ''.join(parts) + '</robot>').encode()


def stl(binary=True):
    """One small tetrahedron."""
    points = [(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0), (0, 0, 0.1)]
    faces = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
    if not binary:
        lines = ['solid demo']
        for face in faces:
            lines += ['facet normal 0 0 0', 'outer loop'] + [f'vertex {" ".join(str(v) for v in points[i])}' for i in face]
            lines += ['endloop', 'endfacet']
        return ('\n'.join(lines) + '\nendsolid demo\n').encode()
    out = io.BytesIO()
    out.write(b'demo'.ljust(80, b' '))
    out.write(struct.pack('<I', len(faces)))
    for face in faces:
        out.write(struct.pack('<12fH', 0, 0, 0, *points[face[0]], *points[face[1]], *points[face[2]], 0))
    return out.getvalue()


def zip_of(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


def tar_gz_of(members, links=None):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            archive.addfile(info)
    return gzip.compress(out.getvalue())


def dog_archive():
    return zip_of({'demo_dog/urdf/dog.urdf': dog_urdf(), 'demo_dog/meshes/visual/trunk.stl': stl(),
                   'demo_dog/README.md': b'not a mesh'})


def scene(**changes):
    base = {'schema': SCENE, 'summary': '一面墙和一个箱子', 'floor': {'size': [6, 6]}, 'robot': {'x': 0, 'y': 0, 'yaw': 0},
            'objects': [{'name': '墙', 'shape': 'box', 'size': [0.1, 3, 0.8], 'pos': [1.5, 0, 0.4], 'color': '#888888'},
                        {'name': '箱子', 'shape': 'box', 'size': [0.2, 0.2, 0.2], 'pos': [0, 1.2, 0.1], 'movable': True,
                         'mass': 0.5}],
            'places': [{'name': '门口', 'x': -2, 'y': 0}, {'name': '墙后', 'x': 3, 'y': 0}]}
    base.update(changes)
    return base
