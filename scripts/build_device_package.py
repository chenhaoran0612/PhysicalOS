"""Build the management layer's install package that ships with the console (issue #19, R1-03).

    python scripts/build_device_package.py               # into .build/device-package
    python scripts/build_device_package.py --out DIR

The package is runtime/deploy/device_install.sh, the two wheels it installs (neuxmind-spec,
neuxmind-runtime; built by pip from a copy of spec/ and runtime/, offline, without build isolation,
so the Python running this needs setuptools) and package.json with each file's size and SHA-256
(brainos/runtime_package.py). scripts/build_macos.py runs this before it freezes the backend and
puts the result into the app as device-package/. In the source tree the console finds it in
.build/device-package. Prints the package's summary as JSON.
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import runtime_package  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=str(ROOT / '.build' / runtime_package.BUNDLE_NAME), help='输出目录（会被替换）')
    options = parser.parse_args(argv)
    package = runtime_package.build(pathlib.Path(options.out), ROOT, python=sys.executable)
    print(json.dumps({**package.summary(), 'directory': str(package.directory), 'source': package.source},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
