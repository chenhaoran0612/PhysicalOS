"""Issue #78: the data of the public library — the sources table (``scripts/library_sources.json``) and the tool
that collects it (``scripts/library_collect.py``). Nothing is fetched here: the sources are made up in a
temporary folder, the way ``test_robot_models_pack`` does it."""
import importlib.util
import io
import json
import re
import sys
import tarfile
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco_support as ms
from test_robot_models_pack import dae

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import library_collect as lc  # noqa: E402

SHA = re.compile(r'[0-9a-f]{40}')
BODY_TYPES = {'quadruped', 'wheel_legged', 'manipulator', 'biped', 'other'}      # brainos/body_types.py


def urdf(meshes):
    """A two-link robot whose visuals name ``meshes`` (one per link)."""
    links = ''.join(f'<link name="l{i}"><visual><geometry><mesh filename="{m}"/></geometry></visual>'
                    f'<visual><geometry><box size="1 1 1"/></geometry><material name="a"/><material name="b"/></visual></link>'
                    for i, m in enumerate(meshes))
    joint = '<joint name="j" type="revolute"><parent link="l0"/><child link="l1"/><limit lower="-1" upper="1" velocity="1" effort="1"/></joint>'
    return f'<?xml version="1.0"?><robot name="demo">{links}{joint}<gazebo/><transmission name="t"/></robot>'.encode()


class SourcesTable(unittest.TestCase):
    """The table itself: every brand and model of the catalogue is either collected or said why not."""

    @classmethod
    def setUpClass(cls):
        cls.data = lc.load_sources()
        cls.catalog = lc.catalog()

    def test_every_entry_is_well_formed(self):
        for entry in self.data['models']:
            with self.subTest(entry['id']):
                self.assertIn((entry['vendor'], entry['model']), self.catalog)
                self.assertIn(lc.body_type(entry['type']), BODY_TYPES)
                self.assertEqual(sum(bool(entry.get(k)) for k in ('urdf', 'xacro', 'mjcf')), 1)
                if entry.get('private'):
                    self.assertNotIn('repo', entry)
                else:
                    self.assertRegex(entry['repo'], r'^[\w.-]+/[\w.-]+$')
                    self.assertTrue(SHA.fullmatch(entry['commit']), 'a full pinned commit')
                    self.assertTrue(entry['paths'], 'what to fetch')
                    self.assertFalse(any(p.startswith(('/', '../')) or '/../' in p for p in entry['paths']))
                self.assertIsInstance(entry.get('licence_name', ''), str)
                self.assertLessEqual(len(lc._version(entry.get('version') or 'x')), lc.MAX_VERSION)
        for section in ('local_models', 'packages', 'local'):
            for entry in self.data[section]:
                with self.subTest(entry['id']):
                    self.assertIn((entry['vendor'], entry['model']), self.catalog)
                    self.assertIn(lc.body_type(entry['type']), BODY_TYPES)
                    if section == 'packages':
                        self.assertTrue(SHA.fullmatch(entry['commit']))
                        self.assertTrue(entry['what'])

    def test_the_catalogue_is_covered(self):
        self.assertEqual(lc.coverage(self.data) and [k for k in lc.coverage(self.data)
                                                       if k not in {(s['vendor'], s['model']) for s in self.data['skipped']}], [])
        for s in self.data['skipped']:
            self.assertTrue(s['why'], f'{s["vendor"]} {s["model"]} says why it has no model')

    def test_the_two_devices_of_the_issue_are_in(self):
        brands = {(e['vendor'], e['model']) for section in ('models', 'local_models', 'packages', 'local') for e in self.data[section]}
        self.assertIn(('宇树科技', 'R1-7A'), brands)
        self.assertIn(('幻尔科技', 'WonderDuck'), brands)
        self.assertIn(('宇树科技', 'R1-7A'), self.catalog)
        self.assertIn(('幻尔科技', 'WonderDuck'), self.catalog)


class PackCase(unittest.TestCase):
    """A made-up checkout and a way to pack one entry from it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / 'checkout'
        (self.base / 'robot' / 'meshes').mkdir(parents=True)
        (self.base / 'other' / 'meshes').mkdir(parents=True)
        (self.base / 'LICENSE').write_text('BSD-3-Clause text')

    def pack(self, entry, base=None):
        entry = {'id': 'demo-robot', 'vendor': '示例', 'model': 'X1', 'type': 'quadruped', 'licence': 'LICENSE',
                 'licence_name': 'BSD-3-Clause', 'mesh_paths': ['robot/meshes'], **entry}
        done = lc.pack_model(entry, base or self.base, self.tmp / 'out', {'repo': 'https://github.com/a/b', 'commit': 'c' * 40,
                                                                          'checkout': base or self.base})
        archive = zipfile.ZipFile(self.tmp / 'out' / done['file'])
        return done, archive


class PackModel(PackCase):
    def test_meshes_are_found_converted_and_a_missing_one_is_kept_as_a_reference(self):
        (self.base / 'robot' / 'meshes' / 'body.stl').write_bytes(ms.stl())
        (self.base / 'robot' / 'meshes' / 'leg.dae').write_bytes(dae())
        (self.base / 'other' / 'meshes' / 'gone.stl').write_bytes(ms.stl())        # another robot's: not looked at
        (self.base / 'robot' / 'demo.urdf').write_bytes(urdf(['package://demo/meshes/body.stl', 'package://demo/meshes/leg.dae',
                                                                'package://demo/meshes/gone.stl']))
        done, archive = self.pack({'urdf': 'robot/demo.urdf'})
        names = set(archive.namelist())
        self.assertEqual(names, {'model.urdf', 'LICENSE.txt', 'SOURCE.json', 'meshes/robot/meshes/body.stl',
                                 'meshes/robot/meshes/leg.dae.stl'})
        root = ET.fromstring(archive.read('model.urdf'))
        refs = [m.get('filename') for m in root.iter('mesh')]
        self.assertEqual(refs, ['meshes/robot/meshes/body.stl', 'meshes/robot/meshes/leg.dae.stl', 'package://demo/meshes/gone.stl'])
        self.assertEqual((root.find('gazebo'), root.find('transmission')), (None, None))
        self.assertTrue(all(len(v.findall('material')) <= 1 for v in root.iter('visual')))
        source = json.loads(archive.read('SOURCE.json'))
        self.assertEqual((source['origin'], source['licence']), ({'repo': 'https://github.com/a/b', 'commit': 'c' * 40}, 'BSD-3-Clause'))
        self.assertTrue(any('COLLADA' in c for c in done['changes']))
        self.assertTrue(any('找不到' in c and 'gone.stl' in c for c in done['changes']))

    def test_a_source_without_a_licence_says_so(self):
        (self.base / 'robot' / 'demo.urdf').write_bytes(urdf([]))
        _, archive = self.pack({'urdf': 'robot/demo.urdf', 'licence': None, 'licence_name': ''})
        self.assertNotIn('LICENSE.txt', archive.namelist())
        self.assertEqual(json.loads(archive.read('SOURCE.json'))['licence'], lc.NO_LICENCE)

    def test_what_is_not_a_plain_urdf_is_refused(self):
        (self.base / 'robot' / 'entity.urdf').write_bytes(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e "x">]><robot name="r"/>')
        (self.base / 'robot' / 'mjcf.urdf').write_bytes(b'<mujoco model="m"/>')
        for name, words in (('entity.urdf', 'DOCTYPE'), ('mjcf.urdf', '不是 URDF'), ('none.urdf', '找不到')):
            with self.subTest(name), self.assertRaisesRegex(lc.CollectError, words):
                self.pack({'urdf': 'robot/' + name})

    def test_a_urdf_inside_an_archive_of_the_repository(self):
        payload = {'arm/urdf/arm.urdf': urdf(['package://arm/meshes/a.STL']), 'arm/meshes/a.STL': ms.stl(),
                   '__MACOSX/arm/._a.STL': b'junk', '../escape.urdf': b'<robot/>'}
        for name, data in (('arm.zip', ms.zip_of(payload)), ('arm.tar.gz', ms.tar_gz_of(payload))):
            with self.subTest(name):
                (self.base / name).write_bytes(data)
                _, archive = self.pack({'id': 'demo-' + name.split('.')[1], 'archive': name, 'urdf': 'arm/urdf/arm.urdf', 'mesh_paths': ['arm/meshes']})
                self.assertIn('meshes/arm/meshes/a.STL', archive.namelist())
                self.assertFalse(any('MACOSX' in n or 'escape' in n for n in archive.namelist()))

    @unittest.skipUnless(importlib.util.find_spec('xacro'), 'xacro is not installed here (scripts/library_collect.py needs it for .xacro sources)')
    def test_a_xacro_is_expanded_with_its_arguments(self):
        (self.base / 'robot' / 'meshes' / 'body.stl').write_bytes(ms.stl())
        (self.base / 'robot' / 'parts.xacro').write_text('<robot xmlns:xacro="http://www.ros.org/wiki/xacro"><xacro:macro name="part">'
                                                         '<link name="${n}"><visual><geometry><mesh filename="package://demo/meshes/body.stl"/></geometry></visual></link>'
                                                         '</xacro:macro></robot>')
        (self.base / 'robot' / 'demo.urdf.xacro').write_text(
            '<robot name="demo" xmlns:xacro="http://www.ros.org/wiki/xacro"><xacro:arg name="n" default="a"/>'
            '<xacro:property name="n" value="$(arg n)"/><xacro:include filename="$(find demo)/parts.xacro"/><xacro:part/></robot>')
        _, archive = self.pack({'xacro': 'robot/demo.urdf.xacro', 'xacro_args': {'n': 'trunk'}, 'ros_packages': {'demo': 'robot'}})
        root = ET.fromstring(archive.read('model.urdf'))
        self.assertEqual([l.get('name') for l in root.iter('link')], ['trunk'])
        self.assertIn('meshes/robot/meshes/body.stl', archive.namelist())


class ReviewFindings(PackCase):
    """What the review of 2026-11-28 found: the environment of xacro, UTF-16 entities, broken archives."""

    @unittest.skipUnless(importlib.util.find_spec('xacro'), 'xacro is not installed here')
    def test_xacro_sees_no_environment_of_this_computer(self):
        (self.base / 'robot' / 'demo.urdf.xacro').write_text(
            '<robot name="demo" xmlns:xacro="http://www.ros.org/wiki/xacro"><link name="$(env HOME)"/>'
            '<link name="$(optenv NEUXMIND_SECRET_PROBE nothing)"/></robot>')
        import os
        os.environ['NEUXMIND_SECRET_PROBE'] = 'leaked'
        try:
            _, archive = self.pack({'xacro': 'robot/demo.urdf.xacro'})
        finally:
            del os.environ['NEUXMIND_SECRET_PROBE']
        names = [l.get('name') for l in ET.fromstring(archive.read('model.urdf')).iter('link')]
        self.assertNotIn(str(Path.home()), names)
        self.assertEqual(names[1], 'nothing')

    def test_a_urdf_that_is_not_utf8_is_refused(self):
        (self.base / 'robot' / 'wide.urdf').write_bytes('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
                                                        '<robot name="&a;"/>'.encode('utf-16'))
        with self.assertRaisesRegex(lc.CollectError, 'UTF-8'):
            self.pack({'urdf': 'robot/wide.urdf'})

    def test_an_archive_that_cannot_be_unpacked_leaves_nothing_half_done(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as tar:
            for name in ('arm', 'arm/a.urdf'):
                info = tarfile.TarInfo(name)
                info.size = 3
                tar.addfile(info, io.BytesIO(b'abc'))
        (self.base / 'bad.tar').write_bytes(out.getvalue())
        with self.assertRaisesRegex(lc.CollectError, '解不开'):
            self.pack({'id': 'demo-bad', 'archive': 'bad.tar', 'urdf': 'arm/a.urdf'})
        self.assertEqual([p for p in (lc.CACHE / 'unpacked').glob('demo-bad-*')], [])


class Collect(unittest.TestCase):
    """The whole run, offline, on made-up sources: what the manifest says of each kind of entry."""

    def test_models_files_and_the_manifest(self):
        tmp = Path(tempfile.mkdtemp())
        private, materials, extra = tmp / 'private', tmp / 'materials', tmp / 'extra'
        (private / 'dog' / 'meshes').mkdir(parents=True)
        (private / 'dog' / 'dog.urdf').write_bytes(urdf(['package://dog/meshes/body.stl']))
        (private / 'dog' / 'meshes' / 'body.stl').write_bytes(ms.stl())
        (materials / 'Duck').mkdir(parents=True)
        (materials / 'Duck' / 'manual.pdf').write_bytes(b'%PDF-1.4 manual')
        (materials / 'Duck' / 'rl.zip').write_bytes(ms.zip_of({
            'rl/LICENSE': b'Apache', 'rl/robot/scene.xml': b'<mujoco><include file="robot.xml"/></mujoco>',
            'rl/robot/robot.xml': b'<mujoco><compiler meshdir="assets"/></mujoco>', 'rl/robot/other.xml': b'<mujoco/>',
            'rl/robot/assets/a.stl': ms.stl()}))
        (extra / 'pages').mkdir(parents=True)
        (extra / 'pages' / 'guide.md').write_text('# guide')
        (tmp / 'secret.pdf').write_bytes(b'%PDF secret')
        base = {'vendor': '示例', 'model': 'X1', 'type': 'humanoid'}
        sources = {
            'models': [{'id': 'demo-dog', **base, 'private': 'dog', 'urdf': 'dog.urdf', 'mesh_paths': ['meshes'],
                        'licence': None, 'licence_name': '厂家文件，不公开', 'version': '厂家文件'},
                       {'id': 'demo-gone', **base, 'private': 'nowhere', 'urdf': 'x.urdf', 'licence': None, 'licence_name': ''}],
            'local_models': [{'id': 'demo-mjcf', **base, 'zip': 'Duck/rl.zip', 'dir': 'rl/robot', 'main': 'scene.xml', 'assets': ['assets'],
                              'licences': ['rl/LICENSE'], 'licence_name': 'Apache-2.0', 'label': 'MJCF', 'version': 'rl'}],
            'packages': [],
            'local': [{'id': 'demo-manual', **base, 'device': 'demo', 'path': 'Duck/manual.pdf', 'version': 'v1', 'what': '手册。'},
                      {'id': 'demo-guide', **base, 'device': 'demo', 'root': 'extra', 'path': 'pages/guide.md', 'version': 'v1',
                       'filename': '指南.md', 'what': '指南。', 'kind': 'model'},
                      {'id': 'demo-escape', **base, 'device': 'demo', 'path': '../secret.pdf', 'version': 'v1', 'what': 'x'}],
            'skipped': [{'vendor': '示例', 'model': 'X2', 'why': '没有公开的模型'}],
            'not_collected': [{'vendor': '示例', 'model': 'X1', 'what': 'image.zip', 'why': '只有网盘'}],
        }
        manifest, report = lc.collect(tmp / 'out', materials, extra, offline=True, sources=sources, private=private)
        by_file = {m['file']: m for m in manifest}
        self.assertEqual(set(by_file), {'models/demo-dog.zip', 'models/demo-mjcf.zip', 'materials/demo/manual.pdf', 'materials/demo/指南.md'})
        dog = by_file['models/demo-dog.zip']
        self.assertEqual((dog['kind'], dog['body_type'], dog['vendor'], dog['model'], dog['version']), ('model', 'biped', '示例', 'X1', '厂家文件'))
        self.assertIn('厂家文件 dog.urdf', dog['description'])
        mjcf = zipfile.ZipFile(tmp / 'out' / 'models' / 'demo-mjcf.zip')
        self.assertEqual(set(mjcf.namelist()), {'scene.xml', 'robot.xml', 'assets/a.stl', 'LICENSE', 'SOURCE.json'})
        self.assertIn('机器人模型（MJCF）', by_file['models/demo-mjcf.zip']['description'])
        self.assertNotIn('kind', by_file['materials/demo/manual.pdf'])
        self.assertEqual(by_file['materials/demo/指南.md']['kind'], 'model')
        self.assertEqual({f['id'] for f in report['failed']}, {'demo-gone', 'demo-escape'})
        written = json.loads((tmp / 'out' / 'manifest.json').read_text())
        self.assertEqual(written, {'version': 1, 'materials': manifest})
        text = (tmp / 'out' / 'REPORT.md').read_text()
        for words in ('demo-gone', '没有公开的模型', '只有网盘', '厂家文件（不公开）'):
            self.assertIn(words, text)


if __name__ == '__main__':
    unittest.main()
