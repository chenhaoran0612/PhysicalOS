"""Issue #55: the product is called 「Physical OS」 (was 「Robot OS」), and the sign-in page has a new left side.

Three things are checked here:

* no source, page, mail, installer text or current document still says "Robot OS" in any spelling, except
  the places listed in KEPT, where the old spelling is an identifier or a location that data depends on;
* the sign-in page no longer lists the four steps and loads its own stylesheet (web/login_hero.css);
* what the user reads first carries the new name: page title, sign-in page, verification mail, app bundle.

How the left side looks and moves is checked in a browser by tests/login_page_browser.cjs.
"""
import re
import tempfile
import unittest
import urllib.request
from pathlib import Path

from brainos import mail_templates, mailer
from brainos.server import Application, make_handler
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
OLD_NAME = re.compile(r'robot(?:[\s_-]|&nbsp;|\u00a0)*os(?![a-z])', re.IGNORECASE)
NEW_NAME = 'NeuxMind Physical OS'
SCANNED = ('brainos', 'web', 'desktop', 'scripts', 'sdk', 'runtime', 'spec', 'cloud', 'tests', 'docs', 'README.md')
TEXT_SUFFIXES = {'.py', '.js', '.cjs', '.css', '.html', '.md', '.txt', '.swift', '.plist', '.toml', '.json', '.sh',
                 '.yml', '.yaml', '.svg', '.cfg', '.ini'}
SKIPPED_DIRECTORIES = {'__pycache__', 'node_modules', '.build', 'dist', 'build', '.git', '.venv'}

# Records of earlier rounds: they say what the product was called when they were written.
HISTORY = (
    'docs/evolution/',
    'docs/HANDOFF.md',
    'docs/system-architecture-20261102/',
    'docs/product-architecture-20261103/',
    'cloud/tests/fixtures/',
)
# The files that check or describe the rename have to spell the old name.
ABOUT_THE_RENAME = {'tests/test_physical_os_name.py', 'tests/test_data_folder.py', 'tests/login_page_browser.cjs',
                    'docs/PRODUCT_NAME.md'}
# (file, pattern of the line) -> why the old spelling stays.
KEPT = (
    # The name the data folder had: the app moves that folder to the new name on its first start.
    ('desktop/DataFolder.swift', r'static let formerName = "NeuxMind Robot OS"'),
    # The install notes tell people who upgrade which old app to delete.
    ('desktop/INSTALL.txt', r'原来的 NeuxMind Robot OS\.app'),
    # The bundle identifier: macOS keys the app's permissions and saved window state on it.
    ('desktop/Info.plist', r'<key>CFBundleIdentifier</key><string>com\.neuxmind\.robotos</string>'),
    ('scripts/build_macos.py', r"'--identifier','com\.neuxmind\.robotos'"),
    # The client name the bundled agent runtime reports to model services (recorded in the cloud fixtures).
    ('brainos/agent_runtime.py', r"'name':'neuxmind_robot_os'"),
    # Where the code lived before it had its own repository.
    ('docs/AGENT_COLLABORATION.md', r'apps/robot-os'),
    ('scripts/demo.py', r'apps/robot-os'),
)


def text_files():
    for name in SCANNED:
        top = ROOT / name
        paths = [top] if top.is_file() else sorted(top.rglob('*'))
        for path in paths:
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if SKIPPED_DIRECTORIES.intersection(path.relative_to(ROOT).parts):
                continue
            yield path


def old_name_lines():
    """Every line that still carries the old name: (relative path, line number, line without the kept part)."""
    found = []
    for path in text_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in ABOUT_THE_RENAME or relative.startswith(HISTORY):
            continue
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            rest = line
            for kept_file, pattern in KEPT:
                if kept_file == relative:
                    rest = re.sub(pattern, '', rest)
            if OLD_NAME.search(rest):
                found.append((relative, number, rest.strip()[:160]))
    return found


class NameTests(unittest.TestCase):
    def test_nothing_outside_the_kept_places_says_robot_os(self):
        found = old_name_lines()

        self.assertEqual(found, [], '\n' + '\n'.join(f'{path}:{number}: {line}' for path, number, line in found))

    def test_every_kept_place_still_exists(self):
        # A kept entry that matches nothing is a leftover: the list must not outlive what it excuses.
        for relative, pattern in KEPT:
            with self.subTest(file=relative, pattern=pattern):
                text = (ROOT / relative).read_text(encoding='utf-8')

                self.assertRegex(text, pattern)

    def test_the_page_title_and_the_waiting_lines_use_the_new_name(self):
        page = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        style = (ROOT / 'web/style.css').read_text(encoding='utf-8')

        self.assertIn('<title>NeuxMind · Physical OS</title>', page)
        self.assertIn('正在连接 Physical OS…', page)
        self.assertIn('content:"正在连接 Physical OS…"', style)

    def test_the_verification_mail_is_from_the_new_name(self):
        _, text, html = mail_templates.verification_mail('register', '123456', 10)

        self.assertEqual(mail_templates.PRODUCT, NEW_NAME)
        self.assertEqual(mailer.DEFAULT_FROM_NAME, NEW_NAME)
        for part in (text, html):
            self.assertIn(NEW_NAME, part)
            self.assertNotRegex(part, OLD_NAME)
        self.assertIn('>PHYSICAL OS</span>', html)

    def test_the_app_bundle_and_installers_carry_the_new_name(self):
        plist = (ROOT / 'desktop/Info.plist').read_text(encoding='utf-8')
        build = (ROOT / 'scripts/build_macos.py').read_text(encoding='utf-8')
        host = (ROOT / 'desktop/main.swift').read_text(encoding='utf-8')

        self.assertIn(f'<key>CFBundleName</key><string>{NEW_NAME}</string>', plist)
        self.assertIn(f'<key>CFBundleDisplayName</key><string>{NEW_NAME}</string>', plist)
        self.assertIn(f"APP=DIST/'{NEW_NAME}.app'", build)
        self.assertIn("NeuxMind-Physical-OS-{VERSION}-arm64.dmg", build)
        self.assertIn("NeuxMind-Physical-OS-{VERSION}-arm64.pkg", build)
        self.assertIn(f'window.title = "{NEW_NAME}"', host)


class SignInPageTests(unittest.TestCase):
    STEPS = ('接入本体', '开发应用', '测试验证', '部署上岗')

    def gate_markup(self):
        script = (ROOT / 'web/account.js').read_text(encoding='utf-8')
        start = script.index('function acctGateHtml()')
        return script[start:script.index('\n}\n', start)]

    def test_the_four_steps_are_gone_from_the_sign_in_page(self):
        markup = self.gate_markup()

        self.assertNotIn('auth-steps', markup)
        for step in self.STEPS:
            self.assertNotIn(step, markup)

    def test_no_style_is_left_for_the_removed_steps(self):
        for sheet in sorted((ROOT / 'web').glob('*.css')):
            with self.subTest(sheet=sheet.name):
                self.assertNotIn('auth-steps', sheet.read_text(encoding='utf-8'))

    def test_the_sign_in_page_names_the_product_and_keeps_its_slogan(self):
        markup = self.gate_markup()

        self.assertIn('PHYSICAL OS', markup)
        self.assertIn('auth-slogan', markup)
        self.assertIn('让你的机器人', markup)
        self.assertIn('准备上岗', markup)


class ServedTests(unittest.TestCase):
    LINE = '<link rel="stylesheet" href="/login_hero.css">'

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

    def test_the_stylesheet_of_the_left_side_is_served_as_it_is_on_disk(self):
        status, content_type, body = self.get('/login_hero.css')

        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith('text/css'))
        self.assertEqual(body, (ROOT / 'web/login_hero.css').read_bytes())

    def test_the_page_loads_the_stylesheet_once_after_the_base_one(self):
        _, _, body = self.get('/')
        page = body.decode('utf-8')

        self.assertEqual(page.count(self.LINE), 1)
        self.assertLess(page.index('href="/style.css"'), page.index(self.LINE))

    def test_the_left_side_stands_still_for_people_who_ask_for_less_motion(self):
        sheet = (ROOT / 'web/login_hero.css').read_text(encoding='utf-8')

        self.assertIn('@media (prefers-reduced-motion:reduce)', sheet.replace(': ', ':'))
        self.assertNotRegex(sheet, r'@keyframes[^}]*\{[^@]*\b(width|height|top|left|margin|padding|font-size)\s*:')


if __name__ == '__main__':
    unittest.main()
