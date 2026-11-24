"""Issue #41: the layout of "我的机器人" (web/robots_page.js, web/robots_page.css).

The console serves the two files and loads them; the rules that lay a robot's card out again are
pure functions of the card's markup and are checked in tests/robots_page_logic.cjs.
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
LINE = '<link rel="stylesheet" href="/robots_page.css"><script src="/robots_page.js" defer></script>'


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

    def test_the_page_files_are_served_as_they_are_on_disk(self):
        for path, mime in (('/robots_page.js', 'text/javascript'), ('/robots_page.css', 'text/css')):
            status, content_type, body = self.get(path)

            self.assertEqual(status, 200, path)
            self.assertTrue(content_type.startswith(mime), content_type)
            self.assertEqual(body, (ROOT / 'web' / path[1:]).read_bytes())

    def test_the_console_loads_them_once_and_after_the_base_styles(self):
        _, _, body = self.get('/')
        index = body.decode('utf-8')

        self.assertEqual(index.count(LINE), 1)
        self.assertGreater(index.index(LINE), index.index('/style.css'))
        # The page is taken over once every script has run, so the place of the line does not matter;
        # the script has to say so itself, or a later script's card would be laid out without its part.
        script = (ROOT / 'web' / 'robots_page.js').read_text(encoding='utf-8')
        self.assertIn("document.addEventListener('DOMContentLoaded', rpInstall)", script)


class PageTests(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/robots_page_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'robots_page_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
