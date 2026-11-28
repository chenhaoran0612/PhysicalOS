"""Issue #72 (整体 Icon 优化): one table of icons, drawn one way, and a drawn mark.

The table, the symbols it replaces and the mark are checked in tests/icons_logic.cjs, in a browser by
tests/icons_browser.cjs; here: the console serves the two files and loads them after the files they build on, and the pictures of
the body types are drawn from the same table.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from brainos.server import Application, make_handler
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
LINE = '<link rel="stylesheet" href="/icons.css"><script src="/icons.js" defer></script>'
TYPE_PICTURES = ('manipulator', 'wheel-legged', 'quadruped', 'humanoid', 'other')


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name)
        self.server = Server(make_handler(self.app))

    def tearDown(self):
        self.server.close()
        self.app.close()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
            return response.status, response.headers.get('Content-Type', ''), response.read()

    def test_the_files_are_served_as_they_are_on_disk(self):
        for name, kind in (('icons.js', 'text/javascript'), ('icons.css', 'text/css')):
            with self.subTest(name=name):
                status, content_type, body = self.get('/' + name)

                self.assertEqual(status, 200)
                self.assertTrue(content_type.startswith(kind), content_type)
                self.assertEqual(body, (ROOT / 'web' / name).read_bytes())

    def test_the_console_loads_them_once_after_what_they_build_on(self):
        _, _, body = self.get('/')
        page = body.decode('utf-8')

        self.assertEqual(page.count(LINE), 1)
        before = page.split(LINE)[0]
        for earlier in ('href="/style.css"', 'src="/app.js"', 'href="/hover.css"', 'href="/task_workbench.css"'):
            self.assertIn(earlier, before)


class PictureTests(unittest.TestCase):
    def test_the_body_type_pictures_are_drawn_like_the_icons(self):
        for name in TYPE_PICTURES:
            with self.subTest(name=name):
                root = ET.parse(ROOT / 'web' / 'robot_images' / f'type-{name}.svg').getroot()

                self.assertEqual(root.get('viewBox'), '0 0 360 360')
                self.assertTrue(root.get('aria-label'))
                self.assertEqual((root.get('stroke-linecap'), root.get('stroke-linejoin')), ('round', 'round'))
                self.assertEqual(root.get('stroke'), '#28674f')
                paths = [child for child in root if child.tag.endswith('path')]
                self.assertEqual(len(paths), 1)
                self.assertRegex(paths[0].get('d'), r'^M[\d.]')

    def test_the_installer_icon_is_drawn_with_round_strokes(self):
        swift = (ROOT / 'desktop' / 'icon.swift').read_text(encoding='utf-8')

        self.assertIn('path.lineCapStyle = .round', swift)
        self.assertIn('path.lineJoinStyle = .round', swift)
        self.assertEqual(len(re.findall(r'let mark: ', swift)), 1)


class TableTests(unittest.TestCase):
    def test_the_table_is_as_the_logic_checks_say(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')

        result = subprocess.run([node, 'tests/icons_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'icons_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
