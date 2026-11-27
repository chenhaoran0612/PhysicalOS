#!/usr/bin/env python3
"""Build the Microduck kit the app is packaged with (issue #63, docs/DUCK_SKILLS.md).

    python scripts/duck_kit.py collect --from ~/code/duck-rl     # copy the delivered packages out of the training project
    python scripts/duck_kit.py build [--out .build/duck-kit] [--offline]

None of the kit is in this repository: the robot's meshes are the vendor's files and third-party models
under a non-commercial licence, and the weights are the training project's. They are kept in a folder on
the build computer (``--private``, else ``$NEUXMIND_PRIVATE_DUCK``, else ``~/code/PhysicalOS-private/duck``)::

    shows/<package file name>      the packages named in brainos/duck_kit.py SHOWS
    training.json                  the training progress when the packages were collected

``collect`` fills that folder from a checkout of the training project and, when the project's dashboard is
running on this computer, takes a copy of the training progress. A package whose checksum is not the one
in ``SHOWS`` is not copied; the script prints the checksum it found, so that the list can be updated on
purpose.

``build`` makes ``<out>/``: the packages under ``shows/<id>.zip`` (checksum checked again), the physics
engine the packages were accepted on (MuJoCo 3.10.0, and onnxruntime for the two third-party gait
policies) unpacked from their wheels into ``engine/``, ``training.json``, ``NOTICE.txt`` and ``kit.json``.
The wheels are downloaded once into ``.build/duck-kit-wheels`` (``--offline`` uses only what is there).
Without the private folder the kit is left empty and the app lists the duck as not available.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brainos import duck_kit  # noqa: E402

PRIVATE_ENV = 'NEUXMIND_PRIVATE_DUCK'
DEFAULT_PRIVATE = Path.home() / 'code' / 'PhysicalOS-private' / 'duck'
WHEELS = {'mujoco': duck_kit.ENGINE['mujoco'], 'onnxruntime': duck_kit.ENGINE['onnxruntime'], 'typing_extensions': '4.12.2'}
# Where a package is in the training project (relative to its root).
SOURCES = {
    'bow_roll': 'reports/training/models/bow_roll', 'water': 'reports/training/models/water',
    'penalty': 'reports/training/models/penalty', 'drunk': 'reports/training/models/drunk',
    'drunk_circle': 'reports/training/evidence-circle-control',
}
# Parts of the wheels the engine never loads (viewers, tests, headers, model converters).
PRUNE = ('mujoco/MuJoCo_(mjpython).app', 'mujoco/testdata', 'mujoco/include', 'mujoco/introspect', 'mujoco/usd', 'mujoco/sysid',
         'mujoco/viewer.py', 'mujoco/mjpython', 'mujoco/experimental', 'onnxruntime/transformers', 'onnxruntime/tools', 'onnxruntime/quantization',
         'onnxruntime/datasets')
NOTICE = """Microduck AD-P102 kit (NeuxMind Physical OS, issue #63)

This folder is not part of the app's own source. It holds, unchanged:

shows/*.zip
  The skill model packages of the duck-rl training project. Each package carries its own licence files
  and source records (vendor/SOURCES.md, vendor/*/LICENSE, vendor/*/SOURCE.md). In short:
  - Robot meshes "AD-P102 second version": the supplier's 3D print files. Not public; not to be passed on.
  - Nine placeholder meshes (battery, servos, electronics, bearings) and the robot description they come
    with: Pollen Robotics microduck_rl. Software Apache-2.0; the 3D models are Creative Commons
    Attribution-NonCommercial-ShareAlike (CC BY-NC-SA). They may not be used commercially.
  - HD1910 servo adaptation: Microduck-kissqy (parameters and training code Apache-2.0; its own
    documents and tools under a personal non-commercial licence) and fanhao375/microduck-replica (Apache-2.0).
  - BAM actuator model: see vendor/bam/LICENSE in each package.
  - velstand.onnx, roulade.onnx: Pollen Robotics microduck-policies, Apache-2.0.
  - The show policies (*.pt), choreography and judges: the duck-rl training project.

engine/
  MuJoCo {mujoco} (Apache-2.0, Google DeepMind), onnxruntime {onnxruntime} (MIT, Microsoft),
  typing_extensions (PSF-2.0), from their wheels on pypi.org, with viewers, tests and headers removed.

Because of the non-commercial models, an installer that contains this kit is for internal use and
demonstration, not for sale.
"""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def say(*parts):
    print(*parts, file=sys.stderr, flush=True)


def private_dir(named):
    return Path(named or os.environ.get(PRIVATE_ENV) or DEFAULT_PRIVATE)


def collect(args):
    source, private = Path(args.source).expanduser(), private_dir(args.private)
    (private / 'shows').mkdir(parents=True, exist_ok=True)
    problems = 0
    for show in duck_kit.PACKAGED:
        path = source / SOURCES[show['id']] / show['package']
        if not path.is_file():
            say(f'  没有找到 {show["id"]}：{path}')
            problems += 1
            continue
        found = sha256(path)
        if found != show['sha256']:
            say(f'  {show["id"]}：训练项目里的包已经换了（{found}），登记的是 {show["sha256"]}；没有复制')
            problems += 1
            continue
        shutil.copyfile(path, private / 'shows' / show['package'])
        say(f'  已复制 {show["id"]}  {path.stat().st_size} 字节')
    url = duck_kit.dashboard_url()
    training = duck_kit.fetch_training(url) if url and not args.no_training else None
    if training:
        training['collected_at'] = time.time()
        (private / 'training.json').write_text(json.dumps(training, ensure_ascii=False), encoding='utf-8')
        say(f'  已保存训练进展（{len(training["tasks"])} 个节目）')
    else:
        say('  训练看板没有应答，训练进展没有更新')
    return 1 if problems else 0


def wheels(cache, offline):
    """The wheel files of the engine for this Python, downloaded once."""
    cache.mkdir(parents=True, exist_ok=True)
    found = {}
    for name, version in WHEELS.items():
        have = sorted(cache.glob(f'{name}-{version}-*.whl'))
        if not have and not offline:
            done = subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-deps', '--only-binary=:all:', '--dest', str(cache),
                                   f'{name}=={version}'], capture_output=True, text=True)
            if done.returncode:
                say(f'  下载 {name} {version} 失败：{done.stderr.strip()[-300:]}')
            have = sorted(cache.glob(f'{name}-{version}-*.whl'))
        if not have:
            return None
        found[name] = have[-1]
    return found


def build_engine(out, cache, offline):
    found = wheels(cache, offline)
    if found is None:
        say('  物理引擎的安装文件不全（没有网络，也没有以前下载的）：这次的套件里没有引擎')
        return False
    engine = out / 'engine'
    for name, wheel in found.items():
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                target = (engine / member.filename).resolve()
                if not target.is_relative_to(engine.resolve()):
                    raise SystemExit(f'{wheel.name} 里有不该有的路径')
                if '.dist-info/' in member.filename or '.data/' in member.filename or member.filename.endswith('_test.py'):
                    continue
                archive.extract(member, engine)
                mode = (member.external_attr >> 16) & 0o777
                if mode and not member.is_dir():
                    os.chmod(target, mode | 0o600)
    for name in PRUNE:
        path = engine / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    # onnxruntime's Python module has the whole library inside; the separate C library is for other programs.
    for path in (engine / 'onnxruntime' / 'capi').glob('libonnxruntime*.dylib'):
        path.unlink()
    thin(engine)
    return True


def thin(engine):
    """On a Mac, keep only this computer's architecture of a library built for two (half the size)."""
    if sys.platform != 'darwin' or not shutil.which('lipo'):
        return
    arch = os.uname().machine
    for path in [*engine.rglob('*.so'), *engine.rglob('*.dylib')]:
        listed = subprocess.run(['lipo', '-archs', str(path)], capture_output=True, text=True)
        if listed.returncode or arch not in listed.stdout.split() or len(listed.stdout.split()) < 2:
            continue
        part = path.with_name(path.name + '.thin')
        if subprocess.run(['lipo', '-thin', arch, str(path), '-output', str(part)], capture_output=True).returncode == 0:
            os.replace(part, path)
        else:
            part.unlink(missing_ok=True)


def build(args):
    out, private = Path(args.out), private_dir(args.private)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if not (private / 'shows').is_dir():
        say(f'没有找到小鸭的资料目录 {private}：套件是空的，应用里小鸭显示为不能添加')
        return 0
    (out / 'shows').mkdir()
    shows = {}
    for show in duck_kit.PACKAGED:
        path = private / 'shows' / show['package']
        if not path.is_file():
            say(f'  没有 {show["id"]} 的技能模型包')
            continue
        if sha256(path) != show['sha256']:
            say(f'  {show["id"]} 的技能模型包不是登记的那一份，没有收进套件')
            continue
        shutil.copyfile(path, out / 'shows' / (show['id'] + '.zip'))
        shows[show['id']] = {'package': show['package'], 'sha256': show['sha256'], 'size': path.stat().st_size}
    has_engine = build_engine(out, Path(args.wheels), args.offline)
    training = private / 'training.json'
    if training.is_file():
        shutil.copyfile(training, out / 'training.json')
    (out / 'NOTICE.txt').write_text(NOTICE.format(**duck_kit.ENGINE), encoding='utf-8')
    kit = {'schema': 'duck.kit.v1', 'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'shows': shows,
           'engine': {**duck_kit.ENGINE, 'python': f'{sys.version_info[0]}.{sys.version_info[1]}', 'present': has_engine},
           'training': training.is_file()}
    (out / 'kit.json').write_text(json.dumps(kit, ensure_ascii=False, indent=1), encoding='utf-8')
    size = sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
    say(f'小鸭套件：{len(shows)} 个技能模型包，引擎{"已" if has_engine else "未"}收入，共 {size / 1e6:.1f} MB → {out}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    one = commands.add_parser('collect')
    one.add_argument('--from', dest='source', required=True)
    one.add_argument('--private', default='')
    one.add_argument('--no-training', action='store_true')
    two = commands.add_parser('build')
    two.add_argument('--out', default=str(ROOT / '.build' / 'duck-kit'))
    two.add_argument('--private', default='')
    two.add_argument('--wheels', default=str(ROOT / '.build' / 'duck-kit-wheels'))
    two.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    return collect(args) if args.command == 'collect' else build(args)


if __name__ == '__main__':
    sys.exit(main())
