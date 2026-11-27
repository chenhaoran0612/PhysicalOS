"""Issue #60 — 机器人应用管理: the files of the page are served and loaded, and its logic holds.

What the page shows is checked in ``tests/app_gallery_logic.cjs`` (pure functions) and
``tests/app_gallery_browser.cjs`` (a browser on the real page); nothing on the server changed its rules.
"""
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from brainos.server import Application, make_handler
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
LINE = '<link rel="stylesheet" href="/app_gallery.css"><script src="/app_gallery.js" defer></script>'


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.application = Application(self.tmp.name)
        self.addCleanup(self.application.close)
        self.server = Server(make_handler(self.application))
        self.addCleanup(self.server.close)

    def test_the_page_files_are_served_and_loaded_once(self):
        for path, mime in (('/app_gallery.js', 'text/javascript'), ('/app_gallery.css', 'text/css')):
            with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
                self.assertTrue(response.headers.get('Content-Type', '').startswith(mime), path)
                self.assertEqual(response.read(), (ROOT / 'web' / path[1:]).read_bytes())
        with urllib.request.urlopen(self.server.url + '/', timeout=10) as response:
            index = response.read().decode('utf-8')
        self.assertEqual(index.count(LINE), 1)
        # It draws with what these know about applications and takes the page over from them, so it comes after.
        for earlier in ('/robot_model.js', '/task_apps.js', '/page_titles.js'):
            self.assertGreater(index.index(LINE), index.index(earlier), earlier)


class PageTests(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/app_gallery_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'app gallery logic: \d+ checks passed')

    def test_the_form_field_it_replaces_is_still_written_the_way_it_expects(self):
        # web/app_gallery.js finds the robot field of the two forms by its markup; a change there must show up here.
        self.assertIn("picker('robots','robot_id','目标机器人')", (ROOT / 'web' / 'delivery.js').read_text(encoding='utf-8'))
        self.assertIn("field('目标机器人',select('robot_id',", (ROOT / 'web' / 'revisions.js').read_text(encoding='utf-8'))
        self.assertIn('目标选仿真机器人时', (ROOT / 'web' / 'delivery.js').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
