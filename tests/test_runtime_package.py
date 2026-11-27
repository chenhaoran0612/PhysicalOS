"""Issue #19: the management layer's install package that ships with the console (R1-03).

The package is a directory with the install script, the two wheels and ``package.json`` (names, sizes,
SHA-256). The console only sends a package whose files are exactly what its manifest says.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from brainos import runtime_package
from install_fixture import fake_package

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name) / 'device-package'

    def test_a_package_names_its_versions_files_and_an_id_made_of_their_checksums(self):
        fake_package(self.dir)
        package = runtime_package.load(self.dir)
        self.assertEqual(package.runtime_version, '0.4.0')
        self.assertEqual(sorted(f['role'] for f in package.files), ['runtime', 'script', 'spec'])
        self.assertRegex(package.id, r'^runtime-0\.4\.0-[0-9a-f]{12}$')
        for entry in package.files:
            data = (self.dir / entry['name']).read_bytes()
            self.assertEqual(entry['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry['bytes'], len(data))
        self.assertEqual(package.path('script'), self.dir / 'device_install.sh')
        self.assertGreater(package.total_bytes, 0)

    def test_a_changed_missing_or_linked_file_is_refused(self):
        for spoil in ('change', 'missing', 'link'):
            with self.subTest(spoil=spoil):
                fake_package(self.dir)
                wheel = next(self.dir.glob('neuxmind_runtime-*.whl'))
                if spoil == 'change':
                    wheel.write_bytes(wheel.read_bytes() + b'x')
                elif spoil == 'missing':
                    wheel.unlink()
                else:
                    elsewhere = pathlib.Path(self.tmp.name) / 'elsewhere.whl'
                    elsewhere.write_bytes(wheel.read_bytes())
                    wheel.unlink()
                    wheel.symlink_to(elsewhere)
                with self.assertRaises(runtime_package.PackageError) as caught:
                    runtime_package.load(self.dir)
                self.assertEqual(caught.exception.code, 'install_package_damaged')
                for entry in self.dir.iterdir():
                    entry.unlink()
                self.dir.rmdir()

    def test_a_manifest_that_does_not_describe_a_package_is_refused(self):
        fake_package(self.dir)
        good = json.loads((self.dir / 'package.json').read_text())
        bad = [
            {**good, 'format': 2},
            {**good, 'files': good['files'][:2]},                                    # a role is missing
            {**good, 'files': [{**good['files'][0], 'name': '../escape.sh'}] + good['files'][1:]},
            {**good, 'files': [{**good['files'][0], 'name': '.hidden'}] + good['files'][1:]},
            {**good, 'runtime_version': '0.4.0; rm'},
            [],
        ]
        for manifest in bad:
            with self.subTest(manifest=str(manifest)[:80]):
                (self.dir / 'package.json').write_text(json.dumps(manifest))
                with self.assertRaises(runtime_package.PackageError):
                    runtime_package.load(self.dir)
        (self.dir / 'package.json').write_text('{not json')
        with self.assertRaises(runtime_package.PackageError):
            runtime_package.load(self.dir)

    def test_the_package_is_found_by_the_environment_then_in_the_app_bundle_then_in_the_build_directory(self):
        fake_package(self.dir)
        with mock.patch.dict(os.environ, {runtime_package.ENV: str(self.dir)}):
            self.assertEqual(runtime_package.bundled().id, runtime_package.load(self.dir).id)
        bundle = pathlib.Path(self.tmp.name) / 'bundle'
        fake_package(bundle / runtime_package.BUNDLE_NAME)
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(runtime_package, 'ROOT', bundle):
            os.environ.pop(runtime_package.ENV, None)
            self.assertEqual(runtime_package.directory(), bundle / runtime_package.BUNDLE_NAME)
        empty = pathlib.Path(self.tmp.name) / 'empty'
        empty.mkdir()
        with mock.patch.object(runtime_package, 'ROOT', empty):
            os.environ.pop(runtime_package.ENV, None)
            self.assertEqual(runtime_package.directory(), empty / '.build' / runtime_package.BUNDLE_NAME)
            with self.assertRaises(runtime_package.PackageError) as caught:
                runtime_package.bundled()
            self.assertEqual(caught.exception.code, 'install_package_missing')


class BuildTests(unittest.TestCase):
    """The package is built from the source tree with pip, offline, the way runtime/README.md §0.4 did by hand."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = pathlib.Path(cls.tmp.name) / 'device-package'
        try:
            import setuptools  # noqa: F401
        except ImportError:
            raise unittest.SkipTest('building wheels needs setuptools in this Python')
        cls.package = runtime_package.build(cls.out, ROOT, python=sys.executable)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_build_holds_the_install_script_and_two_wheels_of_the_tree(self):
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(names, sorted(['device_install.sh', 'package.json', 'neuxmind_spec-1.3.0-py3-none-any.whl',
                                        'neuxmind_runtime-0.4.0-py3-none-any.whl']))
        self.assertEqual((self.out / 'device_install.sh').read_bytes(), (ROOT / 'runtime/deploy/device_install.sh').read_bytes())
        self.assertEqual(runtime_package.load(self.out).id, self.package.id)
        with zipfile.ZipFile(self.out / 'neuxmind_runtime-0.4.0-py3-none-any.whl') as wheel:
            listed = wheel.namelist()
        self.assertIn('neuxmind_runtime/cli.py', listed)
        self.assertIn('neuxmind_runtime/adapters/sim_quadruped.py', listed)
        self.assertFalse([name for name in listed if '/tests/' in name or name.startswith('tests/')])

    def test_building_leaves_nothing_in_the_source_tree(self):
        status = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--ignored', 'runtime', 'spec'],
                                capture_output=True, text=True)
        if status.returncode:
            self.skipTest('not a git checkout')
        left = [line for line in status.stdout.splitlines() if 'build/' in line or '.egg-info' in line]
        self.assertEqual(left, [])

    def test_a_directory_that_is_not_a_package_is_not_replaced(self):
        other = pathlib.Path(self.tmp.name) / 'notes'
        other.mkdir()
        (other / 'keep.txt').write_text('mine')
        with self.assertRaises(runtime_package.PackageError):
            runtime_package.build(other, ROOT, python=sys.executable)
        self.assertEqual((other / 'keep.txt').read_text(), 'mine')
        mixed = pathlib.Path(self.tmp.name) / 'mixed'
        shutil.copytree(self.out, mixed)
        (mixed / 'keep.txt').write_text('mine')
        with self.assertRaises(runtime_package.PackageError):
            runtime_package.build(mixed, ROOT, python=sys.executable)
        again = pathlib.Path(self.tmp.name) / 'again'
        shutil.copytree(self.out, again)
        runtime_package.build(again, ROOT, python=sys.executable)          # an earlier package is replaced

    def test_the_command_line_builds_into_the_named_directory(self):
        out = pathlib.Path(self.tmp.name) / 'cli'
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/build_device_package.py'), '--out', str(out)],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(json.loads(done.stdout)['id'], runtime_package.load(out).id)


class AppBundleTests(unittest.TestCase):
    """The desktop app carries the package (scripts/build_macos.py)."""

    def test_the_app_bundle_carries_the_package_under_its_bundle_name(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        self.addCleanup(sys.path.remove, str(ROOT / 'scripts'))
        import build_macos
        targets = [target for _, target in build_macos.DATA]
        self.assertIn(runtime_package.BUNDLE_NAME, targets)
        source = next(source for source, target in build_macos.DATA if target == runtime_package.BUNDLE_NAME)
        self.assertEqual(source, build_macos.BUILD / runtime_package.BUNDLE_NAME)


if __name__ == '__main__':
    unittest.main()
