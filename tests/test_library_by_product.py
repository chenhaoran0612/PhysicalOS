"""Issue #77 (console side): the 「资料库」 page starts from a brand and model, and a robot model may be MJCF.

* ``library/products`` offers the brands and models of the account's robots and those the cloud keeps
  materials for (``GET /library/products``), with how many materials and usable robot models each has.
* An MJCF model (MuJoCo's own ``.xml``: one file, or an archive with what it includes and its meshes) is
  prepared without MuJoCo into ``model.xml`` that names only files written here, and built like a URDF: the
  robot alone, its free joint, own actuators and scene removed, its joints as MuJoCo reads them.

Every model here is made up by the test."""
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import mujoco_support as ms
from brainos import mujoco_model, mujoco_scene
from brainos.cloud_account import CloudError
from brainos.mujoco_model import ModelError
from test_model_library import ACME, Case

HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None

# A walker in MJCF: degrees (MuJoCo's default), a default class, a floor, a light and a ball in the world, a free
# joint, an actuator that gives a joint its effort, a coupled joint, a sensor, a keyframe and a textured material.
WALKER = b"""<mujoco model="walker">
  <compiler autolimits="true" meshdir="parts"/>
  <option timestep="0.002"/>
  <default><default class="leg"><joint range="-60 60" actuatorfrcrange="-12 12"/></default></default>
  <asset>
    <mesh name="trunk" file="trunk.stl"/>
    <mesh file="missing.stl"/>
    <texture name="skin" type="2d" file="skin.png"/>
    <material name="painted" texture="skin" rgba="0.8 0.2 0.2 1"/>
    <hfield name="ground" file="ground.png" size="1 1 1 1"/>
  </asset>
  <worldbody>
    <light pos="0 0 3"/>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <geom type="hfield" hfield="ground"/>
    <body name="ball" pos="1 0 0.1"><freejoint/><geom type="sphere" size="0.1"/></body>
    <body name="trunk" pos="0 0 0.5">
      <freejoint name="root"/>
      <geom type="mesh" mesh="trunk" material="painted"/>
      <geom type="mesh" mesh="missing"/>
      <geom type="box" size="0.2 0.1 0.05" mass="4"/>
      <body name="thigh" pos="0 0 -0.05" childclass="leg">
        <joint name="hip" axis="0 1 0"/>
        <geom type="capsule" fromto="0 0 0 0 0 -0.2" size="0.03" mass="0.5"/>
        <body name="shin" pos="0 0 -0.2">
          <joint name="knee" axis="0 1 0" range="0 90"/>
          <joint name="roll" axis="1 0 0"/>
          <geom type="capsule" fromto="0 0 0 0 0 -0.2" size="0.03" mass="0.4"/>
        </body>
      </body>
      <body name="tail" pos="-0.25 0 0"><joint name="wag" axis="0 0 1" range="-30 30"/><geom type="sphere" size="0.03" mass="0.1"/>
        <body name="tip" pos="-0.05 0 0"><joint axis="0 1 0" range="-10 10"/><geom type="sphere" size="0.02" mass="0.05"/></body>
      </body>
    </body>
  </worldbody>
  <equality><joint joint1="roll" joint2="knee"/></equality>
  <actuator><position name="tail_motor" joint="wag" forcerange="-2 2"/></actuator>
  <sensor><jointpos joint="hip"/></sensor>
  <keyframe><key name="home" qpos="1 0 0.1 1 0 0 0 0 0 0.5 1 0 0 0 0 0 0 0"/></keyframe>
</mujoco>
"""
SCENE = b"""<mujoco model="scene"><include file="robot/walker.xml"/><include file="../outside.xml"/>
<worldbody><geom name="floor" type="plane" size="3 3 .1"/></worldbody></mujoco>"""


def prepared(source_name, data, fmt):
    tmp = Path(tempfile.mkdtemp(prefix='nx-mjcf-'))
    source = tmp / source_name
    source.write_bytes(data)
    return tmp, mujoco_model.prepare(source, fmt, tmp / 'model')


class TheBrandsAndModelsToChooseFrom(Case):
    def setUp(self):
        super().setUp()
        self.cloud.products = []
        base = self.cloud._call

        def call(method, path, payload=None, timeout=None):
            if path == '/library/products':
                self.cloud.calls.append((method, path, payload))
                if isinstance(self.cloud.products, Exception):
                    raise self.cloud.products
                return {'products': self.cloud.products}
            return base(method, path, payload, timeout)
        self.cloud._call = call

    def test_the_robots_come_first_then_what_the_cloud_keeps_materials_for(self):
        self.robot('一号', **ACME)
        self.cloud.products = [{'vendor': 'acme robotics', 'model': 'GO-7', 'body_type': 'quadruped', 'materials': 3, 'usable_models': 1},
                               {'vendor': 'Microduck', 'model': 'AD-P102', 'body_type': 'other', 'materials': 2, 'usable_models': 0},
                               {'vendor': '', 'model': 'x'}, 'not a row']
        products = self.lib.overview({})['products']
        self.assertEqual(products, [{**ACME, 'family': 'quadruped', 'robots': ['一号'], 'materials': 3, 'usable_models': 1},
                                    {'vendor': 'Microduck', 'model': 'AD-P102', 'family': 'other', 'robots': [],
                                     'materials': 2, 'usable_models': 0}])

    def test_a_cloud_from_before_has_no_list_and_a_real_failure_is_not_hidden(self):
        self.robot('一号', **ACME)
        self.cloud.products = CloudError('Not Found', 404)
        self.assertEqual([p['robots'] for p in self.lib.overview({})['products']], [['一号']])
        self.cloud.products = CloudError('云平台出错', 500, 'internal')
        with self.assertRaises(CloudError):
            self.lib.overview({})

    def test_signed_out_the_cloud_is_not_asked(self):
        self.cloud.overview = lambda p=None: {'account': {'logged_in': False}, 'formats': None, 'materials': [], 'total': 0}
        self.lib.overview({})
        self.assertNotIn('/library/products', [c[1] for c in self.cloud.calls if len(c) == 3])


class AnMjcfModelIsPrepared(unittest.TestCase):
    def test_a_single_file_names_only_files_written_here(self):
        tmp, meta = prepared('walker.xml', WALKER, 'mjcf')
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual((meta['model_format'], meta['robot_name'], meta['meshes']), ('mjcf', 'walker', 0))
        self.assertEqual([j['name'] for j in meta['joints']], ['hip', 'knee', 'roll', 'wag', 'unnamed_joint_2'])
        hip = meta['joints'][0]
        self.assertEqual((hip['type'], round(hip['upper'], 4)), ('revolute', 1.0472))
        text = (tmp / 'model' / 'model.xml').read_text(encoding='utf-8')
        for gone in ('skin.png', 'ground.png', 'parts', 'trunk.stl', 'missing.stl', 'hfield="ground"', 'texture="skin"'):
            self.assertNotIn(gone, text)
        self.assertIn('meshdir="meshes"', text)
        self.assertIn('rgba="0.8 0.2 0.2 1"', text)                   # the material stays, without its picture
        self.assertTrue(any('网格' in w for w in meta['warnings']), meta['warnings'])
        self.assertTrue(any('texture' in w and 'hfield' in w for w in meta['warnings']), meta['warnings'])
        self.assertEqual(mujoco_model.load(tmp / 'model')['model_format'], 'mjcf')

    def test_an_archive_brings_what_the_main_file_includes_and_its_meshes(self):
        members = {'pkg/scene.xml': SCENE, 'pkg/robot/walker.xml': WALKER, 'pkg/robot/parts/trunk.stl': ms.stl(),
                   'pkg/package.xml': b'<package/>'}
        for name, data, fmt in (('m.zip', ms.zip_of(members), 'zip'), ('m.tar.gz', ms.tar_gz_of(members), 'tar.gz')):
            with self.subTest(fmt=fmt):
                tmp, meta = prepared(name, data, fmt)
                self.addCleanup(shutil.rmtree, tmp, True)
                self.assertEqual((meta['robot_name'], meta['meshes']), ('scene', 1))      # found by its name, as for a URDF
                self.assertIn('hip', [j['name'] for j in meta['joints']])
                self.assertTrue(any('../outside.xml' in w for w in meta['warnings']), meta['warnings'])
                files = sorted(p.name for p in (tmp / 'model').rglob('*') if p.is_file())
                self.assertEqual(files, ['m0.stl', 'model.json', 'model.xml'])

    def test_meshes_are_found_through_the_compiler_folder(self):
        members = {'walker.xml': WALKER, 'parts/trunk.stl': ms.stl()}
        tmp, meta = prepared('m.zip', ms.zip_of(members), 'zip')
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(meta['meshes'], 1)
        self.assertIn('file="m0.stl"', (tmp / 'model' / 'model.xml').read_text(encoding='utf-8'))

    def test_what_cannot_be_simulated_is_refused_with_the_reason(self):
        cases = ((ms.zip_of({'a.urdf': ms.dog_urdf(None), 'b.xml': WALKER}), '2 个模型文件'),
                 (ms.zip_of({'m/model.sdf': b'<sdf/>', 'notes.xml': b'<notes/>'}), 'MJCF'),
                 (ms.zip_of({'x.xml': b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY e "x">]><mujoco/>'}), 'DOCTYPE'))
        for data, words in cases:
            with self.subTest(words=words):
                tmp = Path(tempfile.mkdtemp(prefix='nx-mjcf-'))
                self.addCleanup(shutil.rmtree, tmp, True)
                (tmp / 'm.zip').write_bytes(data)
                with self.assertRaises(ModelError) as caught:
                    mujoco_model.prepare(tmp / 'm.zip', 'zip', tmp / 'model')
                self.assertIn(words, str(caught.exception))
                self.assertFalse((tmp / 'model').exists())


# Review of issue #77: what a real model does that the first version got wrong, and what a hostile one may try.
NAMELESS_MESH = b"""<mujoco model="arm"><compiler angle="radian"/><asset><mesh file="parts/base.stl"/></asset>
<worldbody><body name="base"><joint name="spin" range="-1 1"/><geom type="mesh" mesh="base"/></body></worldbody></mujoco>"""
HEAVY = b"""<mujoco model="heavy"><size memory="2G"/><visual><quality shadowsize="65536"/></visual>
<asset><hfield name="h" nrow="200000" ncol="200000" size="1 1 1 1"/><texture name="t" builtin="checker" width="12000" height="12000"/>
<material name="m" texture="t"><layer texture="t" role="rgb"/></material></asset>
<extension><plugin plugin="mujoco.pid"/></extension>
<worldbody><geom type="hfield" hfield="h"/><replicate count="20000"><body><geom size=".1"/></body></replicate>
<body name="base"><joint name="spin"/><geom size=".1" material="m"/></body></worldbody></mujoco>"""
LIMITS = b"""<mujoco model="odd"><compiler angle="radian"/><worldbody>
<body name="a"><joint name="flat" range="0 0"/><geom size=".1" mass="1"/>
<body name="b" pos="0 0 .3"><joint name="backwards" axis="1 0 0" range="1 0"/><geom size=".1" mass="1"/>
<body name="c" pos="0 0 .3"><joint name="unnamed_joint_1" axis="0 1 0" range="-1 1"/><geom size=".1" mass="1"/>
<body name="d" pos="0 0 .3"><joint axis="1 0 0" range="-2 2"/><geom size=".1" mass="1"/></body></body></body></body>
</worldbody></mujoco>"""


class WhatTheReviewFound(unittest.TestCase):
    def prepare_zip(self, members):
        tmp, meta = prepared('m.zip', ms.zip_of(members), 'zip')
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp / 'model', meta

    def test_a_mesh_without_a_name_keeps_the_name_mujoco_gave_it(self):
        model, meta = self.prepare_zip({'arm.xml': NAMELESS_MESH, 'parts/base.stl': ms.stl()})
        self.assertEqual(meta['meshes'], 1)
        self.assertIn('<mesh file="m0.stl" name="base"', (model / 'model.xml').read_text(encoding='utf-8'))

    def test_what_can_make_mujoco_allocate_gigabytes_is_taken_out(self):
        model, meta = self.prepare_zip({'heavy.xml': HEAVY})
        text = (model / 'model.xml').read_text(encoding='utf-8')
        for gone in ('hfield', 'texture', 'replicate', 'plugin', 'extension', '<size', '<visual', 'shadowsize'):
            self.assertNotIn(gone, text)
        self.assertIn('<material name="m"', text)
        self.assertTrue(any('hfield' in w and 'replicate' in w for w in meta['warnings']), meta['warnings'])
        self.assertEqual([j['name'] for j in meta['joints']], ['spin'])

    def test_a_nested_include_is_found_from_the_main_files_folder(self):
        main = b'<mujoco model="main"><include file="sub/b.xml"/></mujoco>'
        sub = b'<mujoco><include file="c.xml"/></mujoco>'          # MuJoCo reads c.xml next to main.xml
        leaf = b'<mujoco><worldbody><body name="base"><joint name="spin"/><geom size=".1"/></body></worldbody></mujoco>'
        _model, meta = self.prepare_zip({'main.xml': main, 'sub/b.xml': sub, 'c.xml': leaf})
        self.assertEqual((meta['robot_name'], [j['name'] for j in meta['joints']]), ('main', ['spin']))

    def test_one_file_included_many_times_counts_every_time(self):
        many = b'<mujoco>' + b'<custom><numeric name="n" data="1"/></custom>' * 1000 + b'</mujoco>'
        main = b'<mujoco model="main">' + b'<include file="many.xml"/>' * 100 + b'</mujoco>'
        tmp = Path(tempfile.mkdtemp(prefix='nx-mjcf-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / 'm.zip').write_bytes(ms.zip_of({'main.xml': main, 'many.xml': many}))
        with self.assertRaises(ModelError) as caught:
            mujoco_model.prepare(tmp / 'm.zip', 'zip', tmp / 'model')
        self.assertIn('元素超过', str(caught.exception))

    def test_limits_are_those_mujoco_applies_and_made_up_names_are_never_taken(self):
        _model, meta = self.prepare_zip({'odd.xml': LIMITS})
        joints = {j['name']: j for j in meta['joints']}
        self.assertEqual(list(joints), ['flat', 'backwards', 'unnamed_joint_1', 'unnamed_joint_2'])
        self.assertEqual([joints[n]['type'] for n in ('flat', 'backwards', 'unnamed_joint_1')], ['continuous', 'continuous', 'revolute'])


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class AnMjcfModelIsBuilt(unittest.TestCase):
    def test_the_robot_alone_with_its_joints_as_mujoco_reads_them(self):
        from brainos import mujoco_build
        tmp, _meta = prepared('walker.xml', WALKER, 'mjcf')
        self.addCleanup(shutil.rmtree, tmp, True)
        world = mujoco_build.build(tmp / 'model', mujoco_scene.validate(ms.scene()), 'quadruped')
        joints = {j['name']: j for j in world.joints}
        self.assertEqual(list(joints), ['hip', 'knee', 'roll', 'wag', 'unnamed_joint_2'])
        self.assertAlmostEqual(joints['knee']['upper'], 1.5707963, places=5)
        self.assertEqual((joints['hip']['effort'], joints['wag']['effort'], joints['hip']['velocity']), (12.0, 2.0, None))
        self.assertEqual((joints['roll'].get('mimic'), joints['unnamed_joint_2']['effort']), ('knee', None))
        model = world.model
        names = {mujoco_name(model, 'actuator', i) for i in range(model.nu)}
        self.assertEqual(names & {'tail_motor', 'robot/tail_motor'}, set())       # the file's own actuator is gone
        self.assertIn('p_hip', names)
        self.assertNotIn('p_roll', names)                                         # it follows the knee
        bodies = {mujoco_name(model, 'body', i) for i in range(model.nbody)}
        self.assertNotIn('robot/ball', bodies)
        self.assertEqual((model.nsensor, model.nkey, model.nlight > 0), (0, 0, True))
        geoms = [mujoco_name(model, 'geom', i) for i in range(model.ngeom)]
        self.assertEqual(geoms.count('floor'), 1)                                 # the scene's floor only
        self.assertNotIn('robot/floor', geoms)
        summary = mujoco_build.settle(world, 0.5)
        self.assertEqual(summary['joints'], ['hip', 'knee', 'roll', 'wag', 'unnamed_joint_2'])
        self.assertTrue(world.robot_geoms)


    def test_a_mesh_without_a_name_and_the_heavy_parts_taken_out_still_build(self):
        from brainos import mujoco_build
        for members in ({'arm.xml': NAMELESS_MESH, 'parts/base.stl': ms.stl()}, {'heavy.xml': HEAVY}):
            with self.subTest(model=next(iter(members))):
                tmp, _meta = prepared('m.zip', ms.zip_of(members), 'zip')
                self.addCleanup(shutil.rmtree, tmp, True)
                world = mujoco_build.build(tmp / 'model', mujoco_scene.validate(ms.scene()), 'manipulator')
                self.assertEqual(world.summary()['joints'], ['spin'])

    def test_the_odd_limits_build_as_listed(self):
        from brainos import mujoco_build
        tmp, _meta = prepared('odd.xml', LIMITS, 'mjcf')
        self.addCleanup(shutil.rmtree, tmp, True)
        world = mujoco_build.build(tmp / 'model', mujoco_scene.validate(ms.scene()), 'manipulator')
        self.assertEqual([(j['name'], j['type']) for j in world.joints],
                         [('flat', 'continuous'), ('backwards', 'continuous'), ('unnamed_joint_1', 'revolute'), ('unnamed_joint_2', 'revolute')])


def mujoco_name(model, kind, index):
    import mujoco
    return mujoco.mj_id2name(model, getattr(mujoco.mjtObj, 'mjOBJ_' + kind.upper()), index) or ''


if __name__ == '__main__':
    unittest.main()
