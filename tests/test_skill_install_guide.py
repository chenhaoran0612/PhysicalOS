"""Issue #62 — 机器人应用页面无法安装技能: the file of the fix is served and loaded, and its logic holds.

A robot that is not connected cannot take a skill. The two dialogs that lead there from 机器人应用 used to
end in buttons that cannot be pressed; they now say why and offer the step that gets the robot connected.
What the page shows is checked in ``tests/skill_install_guide_logic.cjs`` (pure functions) and
``tests/skill_install_guide_browser.cjs`` (a browser on the real page); no rule of the server changed.
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
LINE = '<link rel="stylesheet" href="/skill_install_guide.css"><script src="/skill_install_guide.js" defer></script>'


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.application = Application(self.tmp.name)
        self.addCleanup(self.application.close)
        self.server = Server(make_handler(self.application))
        self.addCleanup(self.server.close)

    def test_the_files_are_served_and_loaded_once(self):
        for path, mime in (('/skill_install_guide.js', 'text/javascript'), ('/skill_install_guide.css', 'text/css')):
            with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
                self.assertTrue(response.headers.get('Content-Type', '').startswith(mime), path)
                self.assertEqual(response.read(), (ROOT / 'web' / path[1:]).read_bytes())
        with urllib.request.urlopen(self.server.url + '/', timeout=10) as response:
            index = response.read().decode('utf-8')
        self.assertEqual(index.count(LINE), 1)
        # It wraps the install dialog, the adopt dialog and the actions of the management layer, so it comes after them.
        for earlier in ('/sim.js', '/ux02.js', '/runtime_link.js', '/runtime_install.js', '/app_gallery.js'):
            self.assertGreater(index.index(LINE), index.index(earlier), earlier)


class PageTests(unittest.TestCase):
    def test_the_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/skill_install_guide_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'skill install guide logic: \d+ checks passed')

    def test_what_it_wraps_is_still_there(self):
        # web/skill_install_guide.js draws into the two dialogs after they are drawn; a change of their names must show up here.
        sim = (ROOT / 'web' / 'sim.js').read_text(encoding='utf-8')
        self.assertIn('async function simGalleryOpen(robotId)', sim)
        self.assertIn("$('#dialog').dataset.simGallery = robotId;", sim)
        ux02 = (ROOT / 'web' / 'ux02.js').read_text(encoding='utf-8')
        self.assertIn("if (type === 'ux-adopt') {", ux02)
        self.assertIn("button('安装技能', 'skillpkg-open', 'small', rows('robots')[0].id)", ux02)
        self.assertIn("'engine-runtime-install'", (ROOT / 'web' / 'runtime_install.js').read_text(encoding='utf-8'))
        self.assertIn("'engine-runtime-link'", (ROOT / 'web' / 'runtime_install.js').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
