"""Issue #57 (标题统一): every page of the sidebar is titled the way 工作台 is.

The English name in small capitals above, the name the sidebar uses below (web/page_titles.js). What each
page is called and how its head is drawn are checked in tests/page_titles_logic.cjs, in a browser by
tests/page_titles_browser.cjs; here: the console serves the script and loads it.
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
LINE = '<script src="/page_titles.js" defer></script>'


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

    def test_the_script_is_served_as_it_is_on_disk(self):
        status, content_type, body = self.get('/page_titles.js')

        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith('text/javascript'), content_type)
        self.assertEqual(body, (ROOT / 'web' / 'page_titles.js').read_bytes())

    def test_the_console_loads_it_once(self):
        _, _, body = self.get('/')

        self.assertEqual(body.decode('utf-8').count(LINE), 1)


class PageTests(unittest.TestCase):
    def test_the_pages_are_titled_as_the_logic_checks_say(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')

        result = subprocess.run([node, 'tests/page_titles_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'page_titles_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
