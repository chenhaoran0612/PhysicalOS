"""Issue #40: file inputs of the console open a file panel in the desktop window.

WKWebView on macOS shows nothing for <input type="file"> unless its UI delegate
implements the open-panel callback, so every upload form was dead in the app
while it worked in a browser. The checks read desktop/main.swift and, on a Mac
with the Swift compiler, type-check it against the real WebKit headers.
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'desktop/main.swift'
OPEN_PANEL = re.compile(
    r'func webView\(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, '
    r'initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping \(\[URL\]\?\) -> Void\) \{'
    r'(?P<body>.*?)\n    \}', re.S)


def open_panel_body():
    found = OPEN_PANEL.search(HOST.read_text(encoding='utf-8'))
    return found.group('body') if found else None


class DesktopFilePickerTest(unittest.TestCase):
    def test_console_has_file_inputs_that_need_the_panel(self):
        pages = [p.name for p in sorted((ROOT / 'web').glob('*.js')) if 'type="file"' in p.read_text(encoding='utf-8')]

        self.assertIn('material_parse.js', pages)
        self.assertIn('material_library.js', pages)
        self.assertIn('extensions.js', pages)

    def test_host_is_the_ui_delegate_and_answers_the_open_panel_request(self):
        source = HOST.read_text(encoding='utf-8')

        self.assertIn('webView.uiDelegate = self', source)
        self.assertIsNotNone(open_panel_body(), 'desktop/main.swift does not implement runOpenPanelWith')

    def test_panel_follows_what_the_input_allows(self):
        body = open_panel_body() or ''

        self.assertIn('panel.allowsMultipleSelection = parameters.allowsMultipleSelection', body)
        self.assertIn('panel.canChooseDirectories = parameters.allowsDirectories', body)
        self.assertIn('panel.canChooseFiles = true', body)

    def test_every_way_out_of_the_panel_answers_the_page(self):
        # WebKit raises an exception when the handler is dropped without being called.
        body = open_panel_body() or ''

        self.assertIn('completionHandler(result == .OK ? panel.urls : nil)', body)

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'needs the Swift compiler on macOS')
    def test_host_type_checks_against_webkit(self):
        done = subprocess.run(
            ['swiftc', '-typecheck', '-target', 'arm64-apple-macos13.0', '-framework', 'Cocoa', '-framework', 'WebKit', str(HOST)],
            capture_output=True, text=True, timeout=300)

        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
