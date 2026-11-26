"""Issue #53: the pictures of robot models, and that the server only serves files of their directory."""
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import robot_pictures  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

from brainos.server import Application, make_handler  # noqa: E402


class StaticTests(unittest.TestCase):
    def test_a_picture_that_exists_is_named_with_its_type(self):
        self.assertEqual(robot_pictures.static('/robot_images/unitree-go2.webp'), ('robot_images/unitree-go2.webp', 'image/webp'))
        self.assertEqual(robot_pictures.static('/robot_images/type-quadruped.svg'), ('robot_images/type-quadruped.svg', 'image/svg+xml'))

    def test_anything_else_is_not_a_picture(self):
        for path in ['/robot_images/', '/robot_images/missing.webp', '/robot_images/SOURCES.md', '/robot_images/../app.js',
                     '/robot_images/%2e%2e/app.js', '/robot_images/a/b.webp', '/robot_images/Unitree-Go2.webp', '/robot_images/.hidden.webp',
                     '/robot_images/unitree-go2.webp/', '/robot_images/unitree-go2.png', '/robot_imagesX/unitree-go2.webp', '/app.js', '', None, 7]:
            with self.subTest(path=path):
                self.assertIsNone(robot_pictures.static(path))

    def test_only_files_of_the_directory_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'robot_images').mkdir()
            (root / 'robot_images' / 'a.webp').write_bytes(b'x')
            (root / 'robot_images' / 'folder.webp').mkdir()
            (root / 'secret.webp').write_bytes(b'y')
            self.assertEqual(robot_pictures.static('/robot_images/a.webp', root), ('robot_images/a.webp', 'image/webp'))
            self.assertIsNone(robot_pictures.static('/robot_images/folder.webp', root))
            self.assertIsNone(robot_pictures.static('/robot_images/secret.webp', root))


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        application = Application(self.tmp.name)
        self.addCleanup(application.close)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(application))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.headers.get('Content-Type'), response.read()

    def test_the_console_gets_the_list_and_the_pictures(self):
        mime, body = self.get('/robot_catalog.js')
        self.assertTrue(mime.startswith('text/javascript'))
        self.assertIn('ROBOT_CATALOG', body.decode())
        mime, body = self.get('/robot_images/unitree-go2.webp')
        self.assertEqual(mime, 'image/webp')
        self.assertEqual(body[:4], b'RIFF')
        mime, body = self.get('/robot_images/type-manipulator.svg')
        self.assertEqual(mime, 'image/svg+xml')
        self.assertTrue(body.startswith(b'<svg'))
        self.assertIn('<script src="/robot_catalog.js" defer></script>', self.get('/')[1].decode())

    def test_a_path_that_is_not_a_picture_is_not_served(self):
        for path in ['/robot_images/SOURCES.md', '/robot_images/..%2Fapp.js', '/robot_images/missing.webp']:
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get(path)
                self.assertIn(caught.exception.code, (401, 404))     # refused like any unknown path (401 before sign-in)


if __name__ == '__main__':
    unittest.main()
