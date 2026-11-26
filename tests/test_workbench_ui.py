"""Issue #52: 工作台 with fewer words.

The page 任务工作台 is called 工作台; the sentences that only described the dashboard and the page of a
task are gone; the skills section of a task has no 「重新读取」 and shows how each skill's runs went.
The pages are web/task_workbench.js, web/task_apps.js and web/task_skills.js; what they say is checked in
tests/workbench_ui_logic.cjs, in a browser by tests/task_workbench_browser.cjs and tests/task_skills_browser.cjs.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_NAME = '任务工作台'


class PageTests(unittest.TestCase):
    def test_the_pages_say_what_the_logic_checks_say(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')

        result = subprocess.run([node, 'tests/workbench_ui_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'workbench_ui_logic: \d+ checks passed')

    def test_no_page_script_shows_the_old_name(self):
        # Comments may keep it (they tell the history); text a person reads may not.
        shown = []
        for path in sorted((ROOT / 'web').glob('*.js')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                code = line if line.lstrip().startswith(('//', '*', '/*')) is False else ''
                code = re.sub(r'\s//\s.*$', '', code)
                if OLD_NAME in code:
                    shown.append(f'{path.name}:{number}')

        self.assertEqual(shown, [])

    def test_the_sidebar_calls_the_page_by_its_new_name(self):
        script = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')

        self.assertIn("['overview','工作台']", script)


if __name__ == '__main__':
    unittest.main()
