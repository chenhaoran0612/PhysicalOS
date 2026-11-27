"""Issue #61: a brand, and a model, is one control: a list that can also be typed in (web/combo.js).

The console serves the control's two files and loads them; no form of the page scripts draws the old
pair (a pick-list with 「其他」 next to a second input) any more. How the control behaves is checked in
tests/combo_logic.cjs, the brand and model lists in tests/robot_catalog_logic.cjs and
tests/model_library_logic.cjs, and in a browser by tests/brand_combo_browser.cjs.
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
LINE = '<link rel="stylesheet" href="/combo.css"><script src="/combo.js" defer></script>'
# What only the old pair had: the value of 「其他」, the list of brand-and-model pairs, its two labels.
OLD_PAIR = ('__other__', 'RC_OTHER', 'ML_OTHER', 'data-ml-product]', 'mlProductSelect', '其他品牌', '其他型号', '品牌名称', '型号名称')
LOGIC = {'tests/combo_logic.cjs': r'combo_logic: \d+ checks passed',
         'tests/robot_catalog_logic.cjs': r'robot_catalog_logic: \d+ checks passed',
         'tests/model_library_logic.cjs': r'model_library_logic: PASS \(\d+ checks\)'}


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

    def test_the_control_is_served_as_it_is_on_disk(self):
        for path, mime in (('/combo.js', 'text/javascript'), ('/combo.css', 'text/css')):
            status, content_type, body = self.get(path)

            self.assertEqual(status, 200, path)
            self.assertTrue(content_type.startswith(mime), content_type)
            self.assertEqual(body, (ROOT / 'web' / path[1:]).read_bytes())

    def test_the_console_loads_it_once_after_the_base_styles(self):
        _, _, body = self.get('/')
        index = body.decode('utf-8')

        self.assertEqual(index.count(LINE), 1)
        self.assertGreater(index.index(LINE), index.index('/style.css'))


class OnePairOfControlsTests(unittest.TestCase):
    def test_no_page_script_draws_the_old_pair(self):
        found = [f'{script.name}: {piece}' for script in sorted((ROOT / 'web').glob('*.js'))
                 for piece in OLD_PAIR if piece in script.read_text(encoding='utf-8')]

        self.assertEqual(found, [])

    def test_the_brand_and_model_forms_use_the_one_control(self):
        catalog = (ROOT / 'web' / 'robot_catalog.js').read_text(encoding='utf-8')
        library = (ROOT / 'web' / 'model_library.js').read_text(encoding='utf-8')

        self.assertIn('comboInput({name, source: sources[name]', catalog)
        self.assertNotIn('<select', catalog)
        self.assertIn('rcInputs(product, true, ML_SOURCES)', library)
        self.assertNotIn('name="product"', library)

    def test_the_logic_checks_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        for script, passed in LOGIC.items():
            with self.subTest(script=script):
                result = subprocess.run([node, script], capture_output=True, text=True, timeout=120, cwd=str(ROOT))

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex(result.stdout, passed)


if __name__ == '__main__':
    unittest.main()
