"""Every place that shows or reports the product version agrees with the build script.

The version is written by hand in several files (backend health, engine client info,
native About box, Info.plist, sidebar, installer notes, frozen smoke test). A release
that bumps some of them and not others ships an app that names two versions;
``scripts/set_version.py`` rewrites them all and this test keeps them in step.
"""
import pathlib
import plistlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import set_version  # noqa: E402


class VersionConsistencyTests(unittest.TestCase):
    def test_every_version_string_matches_the_build_script(self):
        expected = set_version.current_version()
        self.assertRegex(expected, r'^\d+\.\d+\.\d+$')
        found = set_version.versions_in_tree()
        self.assertEqual(sorted(found), sorted(f'{p}#{i}' for p, n in set_version.EXPECTED_COUNTS.items() for i in range(n)),
                         'a version location disappeared or a new one appeared; update set_version.LOCATIONS')
        wrong = {where: version for where, version in found.items() if version != expected}
        self.assertEqual(wrong, {}, f'these places do not say {expected}')

    def test_info_plist_build_number_is_a_positive_integer(self):
        info = plistlib.loads((ROOT / 'desktop' / 'Info.plist').read_bytes())
        self.assertEqual(info['CFBundleShortVersionString'], set_version.current_version())
        self.assertRegex(info['CFBundleVersion'], r'^[1-9]\d*$')

    def test_set_version_rewrites_a_copy_of_every_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp)
            for rel in set_version.EXPECTED_COUNTS:
                (copy / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / rel, copy / rel)
            build_before = int(plistlib.loads((copy / 'desktop' / 'Info.plist').read_bytes())['CFBundleVersion'])
            set_version.set_version('9.8.7', root=copy)
            self.assertEqual(set(set_version.versions_in_tree(root=copy).values()), {'9.8.7'})
            build_after = int(plistlib.loads((copy / 'desktop' / 'Info.plist').read_bytes())['CFBundleVersion'])
            self.assertEqual(build_after, build_before + 1)
            with self.assertRaises(ValueError):
                set_version.set_version('1.2', root=copy)


if __name__ == '__main__':
    unittest.main()
