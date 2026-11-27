"""Issue #55: the data folder is renamed with the product, and nobody's data is left behind.

The desktop app keeps everything under ~/Library/Application Support/<name>/. The name was
"NeuxMind Robot OS" and is "NeuxMind Physical OS" now. desktop/DataFolder.swift decides at start-up
which folder the app uses: it moves the former folder to the new name once and leaves a link at the
former place, so that paths saved inside the data, an older copy of the app and scripts that still
use the former path all reach the same data. If the move cannot be made, the app goes on with the
former folder: it never starts on an empty one while the data sits next to it.

The Swift file is compiled here with a few lines that print what it decided, and run on throwaway
folders. Without a Swift compiler (anything but a Mac with the developer tools) these tests skip.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'desktop/DataFolder.swift'
NEW = 'NeuxMind Physical OS'
FORMER = 'NeuxMind ' + 'Robot OS'
HARNESS = '''import Foundation
let support = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let (folder, outcome) = DataFolder.resolve(in: support)
switch outcome {
case .fresh: print("fresh")
case .current: print("current")
case .moved: print("moved")
case .keptFormer(let why): print("keptFormer"); FileHandle.standardError.write((why + "\\n").data(using: .utf8)!)
}
print(folder.lastPathComponent)
'''
COMPILE_TIMEOUT_S = 300
RUN_TIMEOUT_S = 30


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'Swift compiler required (macOS)')
class DataFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        main = Path(cls.build.name) / 'main.swift'
        main.write_text(HARNESS, encoding='utf-8')
        cls.binary = Path(cls.build.name) / 'resolve'
        done = subprocess.run(['swiftc', str(SOURCE), str(main), '-o', str(cls.binary)],
                              capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S)
        if done.returncode:
            raise AssertionError('desktop/DataFolder.swift does not compile:\n' + done.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.support = Path(self.tmp.name) / 'Application Support'
        self.support.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.chmod(self.support, 0o755))

    def resolve(self):
        done = subprocess.run([str(self.binary), str(self.support)], capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        self.assertEqual(done.returncode, 0, done.stderr)
        outcome, folder = done.stdout.split('\n')[:2]
        return outcome, folder

    def former_with_data(self):
        state = self.support / FORMER / 'state'
        (state / 'workspaces' / 'user-1').mkdir(parents=True)
        (state / 'workspaces' / 'user-1' / 'registry.sqlite3').write_bytes(b'records')
        (state / 'mail.json').write_text('{"host": "smtp.example.test"}', encoding='utf-8')
        os.chmod(state / 'mail.json', 0o600)
        (self.support / FORMER / 'runtime.log').write_text('log\n', encoding='utf-8')
        return state

    def test_a_first_install_uses_the_new_name_and_creates_nothing_itself(self):
        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('fresh', NEW))
        self.assertEqual(list(self.support.iterdir()), [])

    def test_the_former_folder_is_moved_with_everything_in_it(self):
        self.former_with_data()

        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('moved', NEW))
        moved = self.support / NEW
        self.assertFalse(moved.is_symlink())
        self.assertEqual((moved / 'state/workspaces/user-1/registry.sqlite3').read_bytes(), b'records')
        self.assertEqual((moved / 'runtime.log').read_text(encoding='utf-8'), 'log\n')
        self.assertEqual(stat.S_IMODE((moved / 'state/mail.json').stat().st_mode), 0o600)

    def test_the_former_path_still_reaches_the_data_after_the_move(self):
        state = self.former_with_data()

        self.resolve()

        former = self.support / FORMER
        self.assertTrue(former.is_symlink())
        self.assertEqual(os.readlink(former), NEW)  # relative: it survives a restored or copied home folder
        self.assertEqual((state / 'workspaces/user-1/registry.sqlite3').read_bytes(), b'records')

    def test_starting_again_after_the_move_changes_nothing(self):
        self.former_with_data()
        self.resolve()
        before = sorted(p.relative_to(self.support).as_posix() for p in self.support.rglob('*'))

        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('current', NEW))
        self.assertEqual(sorted(p.relative_to(self.support).as_posix() for p in self.support.rglob('*')), before)

    def test_when_both_folders_hold_data_the_new_one_is_used_and_the_former_is_left_alone(self):
        self.former_with_data()
        (self.support / NEW / 'state').mkdir(parents=True)
        (self.support / NEW / 'state' / 'marker').write_text('new', encoding='utf-8')

        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('current', NEW))
        self.assertFalse((self.support / FORMER).is_symlink())
        self.assertEqual((self.support / FORMER / 'state/mail.json').read_text(encoding='utf-8'), '{"host": "smtp.example.test"}')
        self.assertEqual((self.support / NEW / 'state/marker').read_text(encoding='utf-8'), 'new')

    def test_when_the_move_cannot_be_made_the_app_stays_on_the_former_folder(self):
        self.former_with_data()
        os.chmod(self.support, 0o555)  # nothing can be renamed or created next to the former folder

        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('keptFormer', FORMER))
        os.chmod(self.support, 0o755)
        self.assertFalse((self.support / NEW).exists())
        self.assertEqual((self.support / FORMER / 'state/workspaces/user-1/registry.sqlite3').read_bytes(), b'records')

    def test_a_leftover_link_without_a_target_does_not_hide_the_former_data(self):
        self.former_with_data()
        os.symlink('gone', self.support / NEW)

        outcome, folder = self.resolve()

        self.assertEqual((outcome, folder), ('keptFormer', FORMER))
        self.assertTrue((self.support / FORMER / 'state/mail.json').is_file())


class WiringTests(unittest.TestCase):
    def test_the_host_asks_the_data_folder_code_and_names_no_folder_itself(self):
        host = (ROOT / 'desktop/main.swift').read_text(encoding='utf-8')

        self.assertIn('DataFolder.resolve(in:', host)
        self.assertNotIn('appendingPathComponent("NeuxMind', host)

    def test_the_build_compiles_the_data_folder_code_into_the_app(self):
        build = (ROOT / 'scripts/build_macos.py').read_text(encoding='utf-8')

        self.assertIn("ROOT/'desktop/DataFolder.swift'", build)

    def test_the_two_names_are_the_ones_written_in_the_documents(self):
        source = SOURCE.read_text(encoding='utf-8')

        self.assertIn(f'static let name = "{NEW}"', source)
        self.assertIn(f'static let formerName = "{FORMER}"', source)
        for document in ('README.md', 'desktop/INSTALL.txt', 'docs/MAIL_SERVICE.md'):
            with self.subTest(document=document):
                self.assertIn(f'Application Support/{NEW}/', (ROOT / document).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
