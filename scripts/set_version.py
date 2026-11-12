"""Set the product version everywhere it is written, or show where it is written.

    python3 scripts/set_version.py              # list every location and the version it holds
    python3 scripts/set_version.py 0.15.0       # write 0.15.0 everywhere; Info.plist build number + 1
    python3 scripts/set_version.py 0.15.0 --build 30

tests/test_version.py fails when the locations disagree, so a release cannot ship
an app that names two versions (the installer notes once said 0.7.1 inside a 0.14.0 DMG).
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEMVER = re.compile(r'\d+\.\d+\.\d+')
V = r'(\d+\.\d+\.\d+)'

# (file, patterns). Each pattern has exactly one group: the version.
LOCATIONS = [
    ('scripts/build_macos.py', [rf"^VERSION='{V}'"]),
    ('brainos/server.py', [rf"'status':'ok','version':'{V}'"]),
    ('brainos/service.py', [rf"system=\{{'version': '{V}'"]),
    ('brainos/agent_runtime.py', [rf"'title':'NeuxMind Robot OS','version':'{V}'"]),
    ('desktop/main.swift', [rf'messageText = "NeuxMind Robot OS {V}"']),
    ('desktop/Info.plist', [rf'<key>CFBundleShortVersionString</key><string>{V}</string>']),
    ('desktop/INSTALL.txt', [rf'^NeuxMind Robot OS {V} —']),
    ('web/index.html', [rf'ROBOT OS <b>{V} / PLATFORM</b>']),
    ('tests/frozen_engine_smoke.py', [rf"\['version'\]=='{V}'", rf"'version':'{V}'\}}"]),
]
EXPECTED_COUNTS = {path: len(patterns) for path, patterns in LOCATIONS}
BUILD_NUMBER = re.compile(r'(<key>CFBundleVersion</key><string>)(\d+)(</string>)')


def _compiled(pattern):
    return re.compile(pattern, re.M)


def current_version(root=ROOT):
    text = (pathlib.Path(root) / 'scripts' / 'build_macos.py').read_text(encoding='utf-8')
    match = _compiled(LOCATIONS[0][1][0]).search(text)
    if not match:
        raise ValueError('scripts/build_macos.py 中找不到 VERSION')
    return match.group(1)


def versions_in_tree(root=ROOT):
    """{'<file>#<n>': version} for every location found; a missing one is simply absent."""
    found = {}
    for path, patterns in LOCATIONS:
        text = (pathlib.Path(root) / path).read_text(encoding='utf-8')
        index = 0
        for pattern in patterns:
            for match in _compiled(pattern).finditer(text):
                found[f'{path}#{index}'] = match.group(1)
                index += 1
    return found


def set_version(version, *, build=None, root=ROOT):
    if not SEMVER.fullmatch(version):
        raise ValueError(f'版本号须为 X.Y.Z：{version!r}')
    root = pathlib.Path(root)
    for path, patterns in LOCATIONS:
        file = root / path
        text = file.read_text(encoding='utf-8')
        for pattern in patterns:
            text, count = _compiled(pattern).subn(
                lambda m: m.group(0)[:m.start(1) - m.start(0)] + version + m.group(0)[m.end(1) - m.start(0):], text)
            if count != 1:
                raise ValueError(f'{path}：版本位置应出现 1 次，实际 {count} 次（{pattern}）')
        if path == 'desktop/Info.plist':
            def bump(m):
                number = build if build is not None else int(m.group(2)) + 1
                if number < 1:
                    raise ValueError('build 号须为正整数')
                return f'{m.group(1)}{number}{m.group(3)}'
            text, count = BUILD_NUMBER.subn(bump, text)
            if count != 1:
                raise ValueError('desktop/Info.plist：找不到唯一的 CFBundleVersion')
        file.write_text(text, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('version', nargs='?', help='新版本号 X.Y.Z；省略则只列出当前各处版本')
    parser.add_argument('--build', type=int, help='Info.plist 的 CFBundleVersion；默认在原值上加 1')
    args = parser.parse_args(argv)
    if args.version:
        try:
            set_version(args.version, build=args.build)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
    for where, version in versions_in_tree().items():
        print(f'{version}\t{where}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
