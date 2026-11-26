"""Issue #51: what the agent writes as a simulation environment is checked field by field, and a robot
model from the library is unpacked into known file names only. Neither needs MuJoCo."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import mujoco_support as ms
from brainos import mujoco_model, mujoco_scene
from brainos.mujoco_model import ModelError
from brainos.mujoco_scene import SceneError


class SceneRules(unittest.TestCase):
    def test_a_scene_is_returned_with_defaults_filled_in(self):
        scene = mujoco_scene.validate(ms.scene())
        self.assertEqual([o['name'] for o in scene['objects']], ['墙', '箱子'])
        wall, crate = scene['objects']
        self.assertEqual((wall['movable'], wall['yaw'], 'mass' in wall), (False, 0.0, False))
        self.assertEqual((crate['movable'], crate['mass'], crate['color']), (True, 0.5, mujoco_scene.DEFAULT_COLOR))
        self.assertEqual(scene['places'][0], {'name': '门口', 'x': -2.0, 'y': 0.0})
        self.assertEqual(mujoco_scene.validate(mujoco_scene.EMPTY)['objects'], [])
        self.assertEqual(mujoco_scene.parse(json.dumps(ms.scene())), scene)

    def test_anything_else_is_refused_and_the_message_names_the_field(self):
        def changed(path, value):
            scene = copy.deepcopy(ms.scene())
            target = scene
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return scene
        cases = [
            (changed(['schema'], 'mjcf'), 'schema'),
            (changed(['include'], 'other.xml'), 'include'),
            (changed(['objects', 0, 'file'], '/etc/passwd'), 'file'),
            (changed(['objects', 0, 'shape'], 'mesh'), '形状'),
            (changed(['objects', 0, 'size'], [0.1, 3]), '尺寸'),
            (changed(['objects', 0, 'size'], [0.1, 3, 1e9]), '尺寸'),
            (changed(['objects', 0, 'pos'], [0, 0, float('nan')]), 'z'),
            (changed(['objects', 0, 'pos'], [0, 0, -1]), 'z'),
            (changed(['objects', 0, 'color'], 'red'), '颜色'),
            (changed(['objects', 0, 'name'], '<b>墙</b>'), '名称'),
            (changed(['objects', 1, 'name'], '墙'), '重复'),
            (changed(['objects', 0, 'mass'], 3), '固定'),
            (changed(['objects', 1, 'mass'], True), '质量'),
            (changed(['objects'], [ms.scene()['objects'][0]] * 201), '200'),
            (changed(['places', 1, 'name'], '门口'), '重复'),
            (changed(['floor', 'size'], [500, 5]), '地面'),
            (changed(['robot', 'yaw'], 'north'), '朝向'),
            (changed(['summary'], 'x' * 501), 'summary'),
            ([], '对象'),
        ]
        for scene, word in cases:
            with self.subTest(word=word):
                with self.assertRaises(SceneError) as caught:
                    mujoco_scene.validate(scene)
                self.assertIn(word, str(caught.exception))

    def test_text_that_is_too_long_or_not_json_is_refused(self):
        for text in ('{', 'null', b'\xff\xfe', '[' * 100000, ' ' * (mujoco_scene.MAX_BYTES + 1)):
            with self.subTest(text=text[:10]):
                with self.assertRaises(SceneError):
                    mujoco_scene.parse(text)

    def test_the_description_for_a_person_names_every_thing(self):
        lines = mujoco_scene.describe(mujoco_scene.validate(ms.scene()))
        self.assertEqual(len(lines), 5)
        self.assertIn('固定', lines[1])
        self.assertIn('可移动，0.5 kg', lines[2])
        self.assertIn('门口', lines[3])


class ModelFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='nx-model-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def prepare(self, name, data, fmt):
        (self.tmp / name).write_bytes(data)
        return mujoco_model.prepare(self.tmp / name, fmt, self.tmp / 'out')

    def files(self):
        return sorted(str(p.relative_to(self.tmp / 'out')) for p in (self.tmp / 'out').rglob('*') if p.is_file())

    def test_a_single_urdf_keeps_its_joints_and_loses_the_shapes_whose_mesh_did_not_come(self):
        meta = self.prepare('dog.urdf', ms.dog_urdf(), 'urdf')
        self.assertEqual((meta['robot_name'], meta['links'], meta['meshes'], len(meta['joints'])), ('demo_dog', 9, 0, 8))
        self.assertEqual(meta['joints'][0], {'name': 'fl_thigh_joint', 'type': 'revolute', 'parent': 'trunk',
                                             'child': 'fl_thigh', 'lower': -1.2, 'upper': 1.2, 'velocity': 8.0, 'effort': 30.0})
        self.assertTrue(any('trunk.stl' in w for w in meta['warnings']), meta['warnings'])
        text = (self.tmp / 'out/model.urdf').read_text()
        self.assertNotIn('<mesh', text)
        self.assertIn('<compiler', text)
        self.assertEqual(self.files(), ['model.json', 'model.urdf'])
        self.assertEqual(mujoco_model.load(self.tmp / 'out'), meta)

    def test_an_archive_gives_the_urdf_and_only_the_meshes_it_names_under_names_chosen_here(self):
        for fmt, data in (('zip', ms.dog_archive()),
                          ('tar.gz', ms.tar_gz_of({'d/dog.urdf': ms.dog_urdf(), 'd/meshes/visual/trunk.stl': ms.stl(False),
                                                   'd/meshes/unused.stl': ms.stl()}))):
            with self.subTest(fmt=fmt):
                meta = self.prepare('dog.' + fmt, data, fmt)
                self.assertEqual((meta['meshes'], meta['warnings']), (1, []))
                self.assertEqual(self.files(), ['meshes/m0.stl', 'model.json', 'model.urdf'])
                self.assertIn('filename="m0.stl"', (self.tmp / 'out/model.urdf').read_text())
                self.assertEqual((self.tmp / 'out/meshes/m0.stl').read_bytes()[80:84], b'\x04\x00\x00\x00')   # binary, 4 faces

    def test_two_meshes_of_the_same_file_name_are_told_apart_by_their_folders(self):
        urdf = ms.dog_urdf().replace(b'<collision><geometry><box size="0.5 0.25 0.12"/></geometry></collision>',
                                     b'<collision><geometry><mesh filename="package://demo_dog/meshes/collision/trunk.stl"/>'
                                     b'</geometry></collision>')
        small, large = ms.stl(), ms.stl() + b''
        meta = self.prepare('dog.zip', ms.zip_of({'demo_dog/dog.urdf': urdf, 'demo_dog/meshes/visual/trunk.stl': small,
                                                  'demo_dog/meshes/collision/trunk.stl': large}), 'zip')
        self.assertEqual((meta['meshes'], meta['warnings']), (2, []))

    def test_names_that_lead_out_links_and_other_files_are_never_written(self):
        outside = self.tmp / 'outside.stl'
        data = ms.tar_gz_of({'dog.urdf': ms.dog_urdf('../../outside.stl'), '../../outside.stl': ms.stl(),
                             '/abs/trunk.stl': ms.stl(), 'run.sh': b'#!/bin/sh\n'},
                            links={'meshes/trunk.stl': '/etc/passwd'})
        meta = self.prepare('dog.tar.gz', data, 'tar.gz')
        self.assertEqual(meta['meshes'], 0)
        self.assertEqual(self.files(), ['model.json', 'model.urdf'])
        self.assertFalse(outside.exists())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name not in ('dog.tar.gz', 'out')], [])

    def test_a_mesh_the_simulation_cannot_read_is_left_out_with_a_warning(self):
        meta = self.prepare('dog.zip', ms.zip_of({'dog.urdf': ms.dog_urdf('meshes/trunk.dae'), 'meshes/trunk.dae': b'<COLLADA/>',
                                                  'meshes/bad.stl': b'not an stl'}), 'zip')
        self.assertEqual(meta['meshes'], 0)
        self.assertTrue(any('trunk.dae' in w for w in meta['warnings']))
        broken = self.prepare('dog2.zip', ms.zip_of({'dog.urdf': ms.dog_urdf('meshes/bad.stl'), 'meshes/bad.stl': b'solid x\n'}), 'zip')
        self.assertEqual(broken['meshes'], 0)

    def test_what_is_not_a_model_is_refused_and_nothing_is_left_behind(self):
        cases = [('zip', ms.zip_of({'a.stl': ms.stl()}), '没有 .urdf'),
                 ('zip', ms.zip_of({'a.urdf': ms.dog_urdf(), 'b/c.urdf': ms.dog_urdf()}), '2 个'),
                 ('zip', b'PK\x03\x04 not a zip', '读不出来'),
                 ('tar.gz', b'\x1f\x8b not a tar', '读不出来'),
                 ('urdf', b'<!DOCTYPE robot [<!ENTITY x SYSTEM "file:///etc/passwd">]><robot name="&x;"><link name="a"/></robot>', 'DOCTYPE'),
                 ('urdf', b'<html/>', '<robot>'),
                 ('urdf', b'<robot name="empty"/>', '连杆'),
                 ('urdf', b'<robot><link name="a">', 'XML'),
                 ('deb', b'x', 'deb')]
        for fmt, data, word in cases:
            with self.subTest(word=word):
                with self.assertRaises(ModelError) as caught:
                    self.prepare('m.' + fmt, data, fmt)
                self.assertIn(word, str(caught.exception))
                self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith('out')], [])

    def test_a_hostile_file_costs_bounded_work(self):
        """What a review found (2026-11-26): a text STL was split into a list of all its lines, once per shape that named it."""
        from unittest import mock
        shapes = b''.join(b'<visual><geometry><mesh filename="meshes/bad.stl"/></geometry></visual>' for _ in range(50))
        urdf = ms.dog_urdf(None).replace(b'<link name="trunk">', b'<link name="trunk">' + shapes)
        with mock.patch.object(mujoco_model, '_binary_stl', wraps=mujoco_model._binary_stl) as convert:
            meta = self.prepare('dog.zip', ms.zip_of({'dog.urdf': urdf, 'meshes/bad.stl': b'solid x\n' + b'\n' * 100000}), 'zip')
        self.assertEqual((meta['meshes'], convert.call_count), (0, 1))
        self.assertIsNone(mujoco_model._binary_stl(b'solid x\n' + b'vertex 0 0 0 ' * 2000 + b'\n'))       # one endless line
        with mock.patch.object(mujoco_model, 'MAX_ASCII_STL_BYTES', 100):
            self.assertIsNone(mujoco_model._binary_stl(ms.stl(False)))
        deep = b'<robot name="d"><link name="a">' + b'<x>' * 100 + b'</x>' * 100 + b'</link></robot>'
        with self.assertRaises(ModelError) as caught:
            self.prepare('deep.urdf', deep, 'urdf')
        self.assertIn('嵌套', str(caught.exception))
        with mock.patch.object(mujoco_model, 'MAX_UNPACKED_BYTES', 50_000), self.assertRaises(ModelError) as caught:
            self.prepare('big.tar.gz', ms.tar_gz_of({'padding.bin': bytes(200_000), 'dog.urdf': ms.dog_urdf(None)}), 'tar.gz')
        self.assertIn('解开后超过', str(caught.exception))
        with mock.patch.object(mujoco_model, 'MAX_TOTAL_BYTES', 300), self.assertRaises(ModelError):
            self.prepare('many.zip', ms.zip_of({'dog.urdf': ms.dog_urdf(), 'a/trunk.stl': ms.stl(), 'b/x.stl': ms.stl() * 2}), 'zip')

    def test_a_directory_of_an_older_format_is_not_reused(self):
        self.prepare('dog.urdf', ms.dog_urdf(None), 'urdf')
        meta = json.loads((self.tmp / 'out/model.json').read_text())
        (self.tmp / 'out/model.json').write_text(json.dumps({**meta, 'format': 1}))
        self.assertIsNone(mujoco_model.load(self.tmp / 'out'))
        self.assertIsNone(mujoco_model.load(self.tmp / 'missing'))


if __name__ == '__main__':
    unittest.main()
