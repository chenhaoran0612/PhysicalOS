"""§4.4: runtime/pyproject.toml (neuxmind-runtime) and the interpreter used by skill processes."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import _support

import neuxmind_runtime
from neuxmind_runtime import cli
from neuxmind_runtime import sandbox as sandbox_module
from neuxmind_runtime.sandbox import choose_python, python_usable, user_can_execute


class PyprojectTests(unittest.TestCase):
    def setUp(self):
        self.meta = _support.load_toml((_support.RUNTIME_DIR / "pyproject.toml").read_text())

    def test_metadata(self):
        project = self.meta["project"]
        self.assertEqual(project["name"], "neuxmind-runtime")
        self.assertEqual(project["version"], neuxmind_runtime.__version__)
        self.assertIn("neuxmind-spec", project["dependencies"])
        self.assertEqual(project["scripts"]["neuxmind-runtime"], "neuxmind_runtime.cli:main")
        self.assertTrue(callable(cli.main))
        self.assertEqual(project["requires-python"], ">=3.10")      # §5.0: Jetson / Ubuntu 22.04

    def test_every_package_is_listed(self):
        root = _support.RUNTIME_DIR
        found = sorted(str(p.parent.relative_to(root)).replace(os.sep, ".")
                       for p in (root / "neuxmind_runtime").rglob("__init__.py"))
        self.assertEqual(sorted(self.meta["tool"]["setuptools"]["packages"]), found)
        # The sandbox child script ships inside the package.
        self.assertTrue((root / "neuxmind_runtime" / "sandbox_child.py").is_file())


class InterpreterChoiceTests(unittest.TestCase):
    NOBODY = (65534, 65534, "nobody")

    def test_same_user_uses_runtime_interpreter(self):
        self.assertEqual(choose_python(None, None), (sys.executable, None))
        self.assertEqual(choose_python("/opt/py/bin/python3", self.NOBODY), ("/opt/py/bin/python3", None))

    def test_untraversable_parent_is_detected(self):
        """The check the two tests above rely on: a 0755 file under a 0700 parent is not executable by nobody."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            inner = Path(tmp) / "pub"
            inner.mkdir(mode=0o755)
            tool = inner / "tool"
            tool.write_text("#!/bin/sh\n")
            os.chmod(tool, 0o755)
            if os.stat(tmp).st_uid == 65534:
                self.skipTest("测试以 nobody 运行")
            self.assertFalse(user_can_execute(str(tool), 65534, 65534))
            with unittest.mock.patch.object(tempfile, "tempdir", str(inner)):
                root = _support._traversable_tmp_root()
            self.assertNotEqual(root, os.path.realpath(str(inner)))

    def test_private_venv_falls_back_to_real_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir(mode=0o700)
            link = private / "python3"
            link.symlink_to(os.path.realpath(sys.executable))
            self.assertFalse(user_can_execute(str(link), *self.NOBODY[:2]))
            real = os.path.realpath(sys.executable)
            if not user_can_execute(real, *self.NOBODY[:2]):
                self.skipTest("系统解释器对 nobody 也不可执行")
            with unittest.mock.patch.object(sys, "executable", str(link)):
                python, note = choose_python(None, self.NOBODY)
            self.assertEqual(python, real)
            self.assertIn("不可访问", note)

    def test_same_version_system_interpreter_is_the_next_fallback(self):
        """§5.0: a uv/pyenv interpreter under /root is not executable by nobody; a system
        python3.X of the same version is used instead, and if there is none the runtime
        says so (python_usable=false) rather than failing every run mysteriously.

        The "public" fake interpreter must be reachable by nobody along its whole path,
        so it lives in a traversable temp root (iter03 leftover: TMPDIR under a 0700 parent)."""
        tmp = _support.public_tempdir("nx-interp-")
        self.addCleanup(shutil.rmtree, tmp, True)
        if True:
            private = Path(tmp) / "private"
            private.mkdir(mode=0o700)
            hidden = private / "python3"              # e.g. ~/.local/share/uv/python/.../python3
            hidden.write_text("#!/bin/sh\n")
            os.chmod(hidden, 0o755)
            public = Path(tmp) / "public"
            public.mkdir(mode=0o755)
            os.chmod(tmp, 0o755)
            system = public / f"python{sys.version_info.major}.{sys.version_info.minor}"
            system.write_text("#!/bin/sh\n")
            os.chmod(system, 0o755)
            with unittest.mock.patch.object(sys, "executable", str(hidden)), \
                    unittest.mock.patch.object(sandbox_module, "_same_version_candidates", lambda: [str(system)]):
                python, note = choose_python(None, self.NOBODY)
                self.assertEqual(python, str(system))
                self.assertIn("同版本系统解释器", note)
            with unittest.mock.patch.object(sys, "executable", str(hidden)), \
                    unittest.mock.patch.object(sandbox_module, "_same_version_candidates", lambda: []):
                python, note = choose_python(None, self.NOBODY)
                self.assertEqual(python, str(hidden))
                self.assertIn("技能将无法启动", note)
                self.assertFalse(python_usable(python, self.NOBODY))
        self.assertTrue(python_usable("/does/not/matter", None))        # same user: nothing to check

    def test_permission_bits(self):
        tmp = _support.public_tempdir("nx-perm-")
        self.addCleanup(shutil.rmtree, tmp, True)
        if True:
            script = Path(tmp) / "tool"
            script.write_text("#!/bin/sh\n")
            os.chmod(script, 0o754)                 # others may not execute
            owner = os.stat(script)
            self.assertTrue(user_can_execute(str(script), owner.st_uid, owner.st_gid))
            self.assertFalse(user_can_execute(str(script), 65534, 65534))
            os.chmod(script, 0o755)
            self.assertTrue(user_can_execute(str(script), 65534, 65534))
            self.assertFalse(user_can_execute(str(Path(tmp) / "missing"), 65534, 65534))


if __name__ == "__main__":
    unittest.main()
