"""The management layer's install package that ships with the console (issue #19, R1-03).

A package is one directory: ``device_install.sh`` (``runtime/deploy``), the two wheels it installs
(``neuxmind_spec-*.whl``, ``neuxmind_runtime-*.whl``) and ``package.json``, which names each file
with its role, size and SHA-256. Its ``id`` is made of those checksums, so two builds of different
code never share one: it names the directory the files go to on a robot, and an install made from
it carries it (``.console-package`` in the install directory).

Where the console finds it: ``NEUXMIND_DEVICE_PACKAGE`` (a directory, for tests and development),
else ``device-package`` next to ``brainos`` (the desktop app's bundle, ``scripts/build_macos.py``),
else ``.build/device-package`` of the source tree (``scripts/build_device_package.py``). A package
whose files are not exactly what its manifest says is refused, so a damaged or swapped file is
never sent to a robot.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import CodedError

ROOT = Path(__file__).resolve().parents[1]
ENV = 'NEUXMIND_DEVICE_PACKAGE'
BUNDLE_NAME = 'device-package'
MANIFEST = 'package.json'
FORMAT = 1
ROLES = {'script': 'device_install.sh', 'spec': 'neuxmind_spec', 'runtime': 'neuxmind_runtime'}
MAX_FILE_BYTES = 64 * 1024 * 1024
_NAME = re.compile(r'[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,199}')       # what the upload channel (issue #18) accepts
_VERSION = re.compile(r'[0-9][0-9A-Za-z.+-]{0,40}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_ID = re.compile(r'runtime-[0-9][0-9A-Za-z.+-]{0,40}-[0-9a-f]{12}')
MISSING = ('这个控制台没有带管理层的安装包（{where}）。安装包随桌面应用一起发布；'
           '在源码里运行时，先运行 scripts/build_device_package.py')
DAMAGED = '控制台自带的管理层安装包不完整或被改动过（{why}），没有发给设备。请重新安装桌面应用'


class PackageError(CodedError):
    pass


class Package:
    def __init__(self, directory, manifest):
        self.directory = Path(directory)
        self.id = manifest['id']
        self.runtime_version = manifest['runtime_version']
        self.spec_version = manifest['spec_version']
        self.source = manifest.get('source', '')
        self.files = [dict(entry) for entry in manifest['files']]

    def entry(self, role):
        return next(entry for entry in self.files if entry['role'] == role)

    def path(self, role):
        return self.directory / self.entry(role)['name']

    @property
    def total_bytes(self):
        return sum(entry['bytes'] for entry in self.files)

    def summary(self):
        return {'id': self.id, 'runtime_version': self.runtime_version, 'spec_version': self.spec_version,
                'bytes': self.total_bytes, 'files': [entry['name'] for entry in self.files]}


def _damaged(why):
    return PackageError(DAMAGED.format(why=why), 'install_package_damaged')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def package_id(runtime_version, files):
    joined = ''.join(sorted(entry['sha256'] for entry in files))
    return 'runtime-%s-%s' % (runtime_version, hashlib.sha256(joined.encode()).hexdigest()[:12])


def _check_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
        raise _damaged('清单格式不对')
    for key in ('runtime_version', 'spec_version'):
        if not isinstance(manifest.get(key), str) or not _VERSION.fullmatch(manifest[key]):
            raise _damaged('清单里的版本号无效')
    files = manifest.get('files')
    if not isinstance(files, list) or len(files) != len(ROLES):
        raise _damaged('清单里的文件数不对')
    roles = set()
    for entry in files:
        if not isinstance(entry, dict) or entry.get('role') not in ROLES or entry['role'] in roles:
            raise _damaged('清单里的文件角色不对')
        roles.add(entry['role'])
        name = entry.get('name')
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name == MANIFEST:
            raise _damaged('清单里的文件名无效')
        prefix = ROLES[entry['role']]
        if not (name == prefix if entry['role'] == 'script' else name.startswith(prefix + '-') and name.endswith('.whl')):
            raise _damaged('清单里的文件名和角色不符')
        size = entry.get('bytes')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
            raise _damaged('清单里的文件大小无效')
        if not isinstance(entry.get('sha256'), str) or not _SHA256.fullmatch(entry['sha256']):
            raise _damaged('清单里的校验值无效')
    if manifest.get('id') != package_id(manifest['runtime_version'], files) or not _ID.fullmatch(manifest['id']):
        raise _damaged('安装包编号和文件对不上')


def load(directory):
    """The package in ``directory``, after every file was checked against its manifest."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise PackageError(MISSING.format(where=directory), 'install_package_missing') from None
    except (OSError, ValueError):
        raise _damaged('清单读不出来') from None
    _check_manifest(manifest)
    for entry in manifest['files']:
        path = directory / entry['name']
        if path.is_symlink() or not path.is_file():
            raise _damaged('缺少文件 ' + entry['name'])
        if path.stat().st_size != entry['bytes'] or _sha256(path) != entry['sha256']:
            raise _damaged('文件 %s 和清单不一致' % entry['name'])
    return Package(directory, manifest)


def directory():
    """Where the console's package is: see the module text."""
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    bundled = ROOT / BUNDLE_NAME
    return bundled if bundled.is_dir() else ROOT / '.build' / BUNDLE_NAME


def bundled():
    return load(directory())


# --- building one (scripts/build_device_package.py, scripts/build_macos.py) ---------------------------

def write_manifest(out_dir, runtime_version, spec_version, source=''):
    """Describe the files already in ``out_dir`` and write ``package.json``. Returns the package."""
    out_dir = Path(out_dir)
    files = []
    for role, prefix in ROLES.items():
        found = [out_dir / prefix] if role == 'script' else sorted(out_dir.glob(prefix + '-*.whl'))
        if len(found) != 1 or not found[0].is_file():
            raise PackageError('打包时需要恰好一个 %s，找到 %d 个' % (prefix, len(found)), 'install_package_missing')
        files.append({'role': role, 'name': found[0].name, 'bytes': found[0].stat().st_size, 'sha256': _sha256(found[0])})
    manifest = {'format': FORMAT, 'id': package_id(runtime_version, files), 'runtime_version': runtime_version,
                'spec_version': spec_version, 'source': source, 'files': files}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return load(out_dir)


def _project_version(pyproject):
    found = re.search(r'^version\s*=\s*"([^"]+)"', Path(pyproject).read_text(encoding='utf-8'), re.M)
    if not found:
        raise PackageError('%s 里没有版本号' % pyproject, 'install_package_missing')
    return found.group(1)


def _source(tree):
    done = subprocess.run(['git', '-C', str(tree), 'rev-parse', '--short=12', 'HEAD'], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ''


def _replaceable(out_dir):
    """``build`` replaces only a directory that is empty or holds an earlier package and nothing else."""
    if not out_dir.exists():
        return
    names = {entry.name for entry in out_dir.iterdir()} if out_dir.is_dir() and not out_dir.is_symlink() else None
    known = names is not None and all(name in (MANIFEST, ROLES['script']) or name.endswith('.whl') for name in names)
    if names is None or (names and (MANIFEST not in names or not known)):
        raise PackageError('%s 不是空目录，也不是之前构建的安装包；没有替换它' % out_dir, 'install_package_missing')


def build(out_dir, tree=ROOT, python=None):
    """Build the package from the source ``tree`` into ``out_dir`` (replaced). The wheels are made by
    ``pip wheel`` from a copy of ``spec/`` and ``runtime/``, offline and without build isolation (the
    build Python needs setuptools), so the tree itself gets no build directories."""
    tree, out_dir = Path(tree), Path(out_dir)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'build', '*.egg-info', 'tests', 'examples')
    with tempfile.TemporaryDirectory(prefix='nx-device-package-') as temporary:
        copy, wheels = Path(temporary) / 'src', Path(temporary) / 'wheels'
        for part in ('spec', 'runtime'):
            shutil.copytree(tree / part, copy / part, ignore=ignore)
        subprocess.run([python or sys.executable, '-m', 'pip', 'wheel', '--quiet', '--no-deps', '--no-index',
                        '--no-build-isolation', '--disable-pip-version-check', '-w', str(wheels),
                        str(copy / 'spec'), str(copy / 'runtime')], check=True, cwd=temporary)
        _replaceable(out_dir)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        for wheel in sorted(wheels.glob('*.whl')):
            shutil.copy2(wheel, out_dir / wheel.name)
        shutil.copy2(tree / 'runtime' / 'deploy' / 'device_install.sh', out_dir / 'device_install.sh')
    return write_manifest(out_dir, _project_version(tree / 'runtime' / 'pyproject.toml'),
                          _project_version(tree / 'spec' / 'pyproject.toml'), _source(tree))
