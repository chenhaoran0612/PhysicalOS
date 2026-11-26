"""Issue #49 (功能简化): 「工程助手」「助手工具」「自有服务」 are hidden for now (web/hidden_pages.js).

The console serves the script and loads it; which pages are hidden and what happens to the links
that led to them are pure functions, checked in tests/hidden_pages_logic.cjs. Hiding is all it does:
the routes of the hidden pages still answer.
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
LINE = '<script src="/hidden_pages.js" defer></script>'
HIDDEN_PAGE_SCRIPTS = ('engineering.js', 'extensions.js', 'workflows.js')


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
        status, content_type, body = self.get('/hidden_pages.js')

        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith('text/javascript'), content_type)
        self.assertEqual(body, (ROOT / 'web' / 'hidden_pages.js').read_bytes())

    def test_the_console_loads_it_once_and_it_waits_for_every_other_script(self):
        _, _, body = self.get('/')
        index = body.decode('utf-8')

        self.assertEqual(index.count(LINE), 1)
        # It acts once every script has run and clears whatever is drawn later, so the place of the
        # line does not matter; the script has to say so itself.
        script = (ROOT / 'web' / 'hidden_pages.js').read_text(encoding='utf-8')
        self.assertIn("document.addEventListener('DOMContentLoaded', hpInstall)", script)

    def test_the_hidden_pages_are_still_part_of_the_console(self):
        _, _, body = self.get('/')
        index = body.decode('utf-8')

        for name in HIDDEN_PAGE_SCRIPTS:
            self.assertIn(f'<script src="/{name}" defer></script>', index)
            self.assertEqual(self.get('/' + name)[0], 200, name)


class LogicTests(unittest.TestCase):
    def test_the_rules_of_what_is_hidden(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is required for the logic checks')

        result = subprocess.run([node, 'tests/hidden_pages_logic.cjs'], capture_output=True, text=True,
                                cwd=ROOT, timeout=60)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'hidden_pages_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
