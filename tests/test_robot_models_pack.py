"""Issue #54: the tool that builds the robot model packages of the built-in profiles
(``scripts/robot_models.py``) — COLLADA to STL, stand-in shapes for links without a mesh file, and the
package itself. Nothing is fetched here: the sources are made up in a temporary folder."""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco_support as ms

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import robot_models  # noqa: E402

DAE = '''<?xml version="1.0"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
 <asset><unit name="{unit_name}" meter="{unit}"/><up_axis>{up}</up_axis></asset>
 <library_geometries><geometry id="g"><mesh>
  <source id="g-pos"><float_array id="g-pos-array" count="12">0 0 0 1 0 0 0 1 0 0 0 1</float_array></source>
  <source id="g-nor"><float_array id="g-nor-array" count="3">0 0 1</float_array></source>
  <vertices id="g-vert"><input semantic="POSITION" source="#g-pos"/></vertices>
  {primitive}
 </mesh></geometry></library_geometries>
 <library_visual_scenes><visual_scene id="s"><node id="n"><matrix>{matrix}</matrix>
  <instance_geometry url="#g"/></node></visual_scene></library_visual_scenes>
</COLLADA>'''
TRIANGLES = ('<triangles count="2"><input semantic="VERTEX" source="#g-vert" offset="0"/>'
             '<input semantic="NORMAL" source="#g-nor" offset="1"/><p>0 0 1 0 2 0 0 0 1 0 3 0</p></triangles>')
QUAD = ('<polylist count="1"><input semantic="VERTEX" source="#g-vert" offset="0"/><vcount>4</vcount><p>0 1 2 3</p></polylist>')
IDENTITY = '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1'


def dae(primitive=TRIANGLES, matrix=IDENTITY, up='Z_UP', unit='1', unit_name='meter'):
    return DAE.format(primitive=primitive, matrix=matrix, up=up, unit=unit, unit_name=unit_name).encode()


def triangles(stl):
    count = struct.unpack_from('<I', stl, 80)[0]
    assert len(stl) == 84 + 50 * count
    out = []
    for index in range(count):
        values = struct.unpack_from('<12f', stl, 84 + 50 * index)
        out.append([tuple(round(v, 4) for v in values[3 + 3 * k:6 + 3 * k]) for k in range(3)])
    return out


class Collada(unittest.TestCase):
    def test_triangles_come_out_as_they_are_when_the_file_is_in_metres_and_z_up(self):
        self.assertEqual(triangles(robot_models.dae_to_stl(dae())),
                         [[(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 0, 0), (1, 0, 0), (0, 0, 1)]])

    def test_the_node_transform_the_unit_and_the_up_axis_are_applied(self):
        moved = triangles(robot_models.dae_to_stl(dae(matrix='1 0 0 10 0 1 0 20 0 0 1 30 0 0 0 1', unit='0.01', unit_name='centimeter')))
        self.assertEqual(moved[0], [(0.1, 0.2, 0.3), (0.11, 0.2, 0.3), (0.1, 0.21, 0.3)])
        y_up = triangles(robot_models.dae_to_stl(dae(up='Y_UP')))
        self.assertEqual(y_up[0], [(0, 0, 0), (1, 0, 0), (0, 0, 1)])           # the file's +y is up: it becomes +z
        x_up = triangles(robot_models.dae_to_stl(dae(up='X_UP')))
        self.assertEqual(x_up[0], [(0, 0, 0), (0, 0, 1), (-1, 0, 0)])          # the file's +x is up: it becomes +z

    def test_a_polygon_is_cut_into_triangles(self):
        self.assertEqual(triangles(robot_models.dae_to_stl(dae(primitive=QUAD))),
                         [[(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 0, 0), (0, 1, 0), (0, 0, 1)]])

    def test_a_file_without_faces_or_with_a_doctype_is_refused(self):
        with self.assertRaises(robot_models.PackError):
            robot_models.dae_to_stl(dae(primitive=''))
        with self.assertRaises(robot_models.PackError):
            robot_models.dae_to_stl(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>' + dae()[21:])
        with self.assertRaises(robot_models.PackError):          # also when it comes late in the file
            robot_models.dae_to_stl(b'<?xml version="1.0"?><!--' + b'x' * 6000 + b'--><!DOCTYPE x [<!ENTITY a "b">]>' + dae()[21:])


class StandIn(unittest.TestCase):
    def test_a_link_gets_a_box_a_cylinder_towards_the_next_joint_or_a_ball(self):
        root = ET.fromstring(ms.dog_urdf(mesh=''))
        for link in root.findall('link'):
            for shape in link.findall('collision'):
                link.remove(shape)
        robot_models.stand_in(root, {'trunk', 'fl_thigh', 'fl_calf'})
        shapes = {link.get('name'): [(holder.tag, holder.find('geometry')[0].tag) for holder in link if holder.tag in ('visual', 'collision')]
                  for link in root.findall('link')}
        self.assertEqual(shapes['trunk'], [('visual', 'box'), ('collision', 'box')])
        self.assertEqual(shapes['fl_thigh'], [('visual', 'cylinder'), ('collision', 'cylinder')])
        self.assertEqual(shapes['fl_calf'], [('visual', 'sphere'), ('collision', 'sphere')])
        self.assertEqual(shapes['rr_thigh'], [])
        trunk = [l for l in root.findall('link') if l.get('name') == 'trunk'][0]
        size = [float(v) for v in trunk.find('visual/geometry/box').get('size').split()]
        self.assertEqual([round(v, 2) for v in size], [0.49, 0.33, 0.09])       # the four hips, and a margin around them
        thigh = [l for l in root.findall('link') if l.get('name') == 'fl_thigh'][0]
        cylinder, origin = thigh.find('visual/geometry/cylinder'), thigh.find('visual/origin')
        self.assertEqual(float(cylinder.get('length')), 0.2)
        self.assertEqual([round(float(v), 3) for v in origin.get('xyz').split()], [0, 0, -0.1])
        self.assertEqual(round(float(origin.get('rpy').split()[1]), 4), 3.1416)   # it points down, to the knee


class Package(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base, self.out = Path(self.tmp.name) / 'src', Path(self.tmp.name) / 'out'
        (self.base / 'robot' / 'urdf').mkdir(parents=True)
        (self.base / 'robot' / 'meshes').mkdir()
        (self.base / 'LICENSE').write_text('BSD 3-Clause License\n\nCopyright (c) 2026, Somebody\n')
        self.profile = {'id': 'acme-dog', 'vendor': 'Acme', 'model': 'Dog', 'type': 'quadruped',
                        'source': {'urdf': 'robot/urdf/dog.urdf', 'licence': 'LICENSE', 'licence_name': 'BSD-3-Clause'}}

    def write(self, urdf, **meshes):
        (self.base / 'robot' / 'urdf' / 'dog.urdf').write_bytes(urdf)
        for name, data in meshes.items():
            (self.base / 'robot' / 'meshes' / name.replace('_', '.')).write_bytes(data)

    def pack(self):
        robot_models.pack(self.profile, self.base, self.out, {'repo': 'https://github.com/acme/dog', 'commit': 'c' * 40})
        with zipfile.ZipFile(self.out / 'acme-dog.zip') as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_it_holds_the_urdf_the_meshes_it_uses_the_licence_and_where_they_are_from(self):
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.stl'), trunk_stl=ms.stl(), unused_stl=ms.stl())
        members = self.pack()
        self.assertEqual(sorted(members), ['LICENSE.txt', 'SOURCE.json', 'meshes/robot/meshes/trunk.stl', 'model.urdf'])
        self.assertIn(b'filename="meshes/robot/meshes/trunk.stl"', members['model.urdf'])
        self.assertIn(b'Copyright (c) 2026, Somebody', members['LICENSE.txt'])
        record = json.loads(members['SOURCE.json'])
        self.assertEqual((record['profile'], record['licence'], record['origin']['commit'], record['changes']),
                         ('acme-dog', 'BSD-3-Clause', 'c' * 40, []))
        self.assertEqual(record['files']['model.urdf'], hashlib.sha256(members['model.urdf']).hexdigest())

    def test_the_same_input_gives_the_same_bytes(self):
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.stl'), trunk_stl=ms.stl())
        self.pack()
        first = (self.out / 'acme-dog.zip').read_bytes()
        self.pack()
        self.assertEqual((self.out / 'acme-dog.zip').read_bytes(), first)

    def test_a_collada_mesh_is_converted_and_the_urdf_names_the_stl(self):
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.dae'), trunk_dae=dae())
        members = self.pack()
        self.assertEqual(len(triangles(members['meshes/robot/meshes/trunk.dae.stl'])), 2)
        self.assertIn(b'filename="meshes/robot/meshes/trunk.dae.stl"', members['model.urdf'])
        self.assertIn('COLLADA', json.loads(members['SOURCE.json'])['changes'][0])

    def test_a_missing_mesh_is_refused_unless_the_profile_asks_for_stand_in_shapes(self):
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.stl'))
        with self.assertRaises(robot_models.PackError) as caught:
            self.pack()
        self.assertIn('trunk', str(caught.exception))
        self.profile['source']['stand_in_shapes'] = True
        members = self.pack()
        self.assertIn(b'<box', members['model.urdf'])
        self.assertIn('不是机器人的真实外形', json.loads(members['SOURCE.json'])['changes'][0])

    def test_what_the_simulation_cannot_read_is_taken_out_of_the_urdf(self):
        urdf = ms.dog_urdf(mesh='').replace(
            b'<collision><geometry><box size="0.5 0.25 0.12"/></geometry></collision>',
            b'<visual><geometry><box size="0.5 0.25 0.12"/></geometry><material name="a"/><material name="b"/></visual>', 1).replace(
            b'</robot>', b'<gazebo><plugin filename="libx.so"/></gazebo><transmission name="t"/></robot>')
        self.write(urdf)
        document = self.pack()['model.urdf']
        self.assertEqual((document.count(b'<material'), document.count(b'gazebo'), document.count(b'transmission')), (1, 0, 0))

    def test_a_mesh_that_is_a_link_to_a_file_elsewhere_is_not_packed(self):
        outside = Path(self.tmp.name) / 'private.stl'
        outside.write_bytes(ms.stl())
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.stl'))
        (self.base / 'robot' / 'meshes' / 'trunk.stl').symlink_to(outside)
        with self.assertRaises(robot_models.PackError):
            self.pack()

    def test_only_the_profiles_own_folders_are_looked_in(self):
        (self.base / 'other' / 'meshes').mkdir(parents=True)
        (self.base / 'other' / 'meshes' / 'trunk.stl').write_bytes(ms.stl())
        self.write(ms.dog_urdf(mesh='package://acme_dog/meshes/trunk.stl'))
        self.profile['source']['paths'] = ['robot/urdf/dog.urdf', 'robot/meshes/*']
        with self.assertRaises(robot_models.PackError):          # another robot's file of the same name is not this one's
            self.pack()

    def test_a_profile_that_cannot_be_built_now_does_not_keep_its_package_from_an_earlier_build(self):
        from unittest import mock
        from brainos import sim_profiles
        private = Path(self.tmp.name) / 'private'
        (private / 'dog').mkdir(parents=True)
        profile = {'id': 'acme-dog', 'vendor': 'Acme', 'model': 'Dog', 'type': 'quadruped', 'source': {'private': 'dog', 'urdf': 'dog.urdf'}}
        (private / 'dog' / 'dog.urdf').write_bytes(ms.dog_urdf(mesh=''))
        with mock.patch.object(sim_profiles, 'PROFILES', (profile,)), mock.patch.object(sim_profiles, 'BY_ID', {'acme-dog': profile}):
            done, skipped = robot_models.build(self.out, private)
            self.assertEqual(([d['id'] for d in done], skipped, (self.out / 'acme-dog.zip').is_file()), (['acme-dog'], [], True))
            (private / 'dog' / 'dog.urdf').write_bytes(b'<robot><link name="a"><visual><geometry><mesh filename="gone.stl"/></geometry></visual></link></robot>')
            done, skipped = robot_models.build(self.out, private)
            self.assertEqual((done, [s['id'] for s in skipped], (self.out / 'acme-dog.zip').exists()), ([], ['acme-dog'], False))
            self.assertNotIn('acme-dog.zip', (self.out / 'NOTICE.txt').read_text(encoding='utf-8'))
            (private / 'dog' / 'dog.urdf').write_bytes(b'not xml at all')
            self.assertEqual([s['id'] for s in robot_models.build(self.out, private)[1]], ['acme-dog'])

    def test_a_urdf_with_a_doctype_is_refused(self):
        self.write(b'<?xml version="1.0"?><!DOCTYPE robot [<!ENTITY x "y">]>' + ms.dog_urdf(mesh='')[21:])
        with self.assertRaises(robot_models.PackError):
            self.pack()

    def test_the_notice_lists_what_is_there_and_a_package_nobody_lists_goes(self):
        from brainos import sim_profiles
        listed = sim_profiles.PROFILES[0]
        self.profile.update(id=listed['id'], vendor=listed['vendor'], model=listed['model'])
        self.write(ms.dog_urdf(mesh=''))
        robot_models.pack(self.profile, self.base, self.out, {'repo': 'https://github.com/acme/dog', 'commit': 'c' * 40})
        (self.out / 'gone.zip').write_bytes(b'old')
        robot_models.write_notice(self.out)
        notice = (self.out / 'NOTICE.txt').read_text(encoding='utf-8')
        self.assertIn(f'{listed["id"]}.zip: {listed["vendor"]} {listed["model"]} | https://github.com/acme/dog @ {"c" * 40} | BSD-3-Clause', notice)
        self.assertFalse((self.out / 'gone.zip').exists())


if __name__ == '__main__':
    unittest.main()
