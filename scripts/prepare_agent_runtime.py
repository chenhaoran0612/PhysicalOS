"""Fetch the pinned upstream archive, verify integrity and copy only locked binaries."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--verify-only',action='store_true');args=parser.parse_args()
    lock=json.loads((ROOT/'runtime-lock.json').read_text());target=ROOT/'.build/agent-runtime/bin'
    def valid():
        return all((target/name).is_file() and hashlib.sha256((target/name).read_bytes()).hexdigest()==digest for name,digest in lock['files'].items())
    if not valid():
        if args.verify_only:raise SystemExit('Pinned runtime missing or modified; run scripts/prepare_agent_runtime.py')
        with tempfile.TemporaryDirectory(prefix='neuxmind-runtime-') as tmp:
            archive=Path(tmp)/'runtime.tgz';urllib.request.urlretrieve(lock['tarball'],archive)
            algorithm,expected=lock['integrity'].split('-',1)
            if base64.b64encode(hashlib.new(algorithm,archive.read_bytes()).digest()).decode()!=expected:raise SystemExit('Runtime archive integrity mismatch')
            target.mkdir(parents=True,exist_ok=True)
            with tarfile.open(archive,'r:gz') as tar:
                for name,digest in lock['files'].items():
                    archive_path=lock.get('archive_paths',{}).get(name)
                    matches=[m for m in tar.getmembers() if m.isfile() and (m.name==archive_path if archive_path else m.name.endswith('/bin/'+name))]
                    if len(matches)!=1:raise SystemExit('Unexpected upstream archive layout')
                    payload=tar.extractfile(matches[0]).read()
                    if hashlib.sha256(payload).hexdigest()!=digest:raise SystemExit('Binary digest mismatch')
                    (target/name).write_bytes(payload);(target/name).chmod(0o755)
    print('Verified pinned engineering runtime',lock['version'])

if __name__=='__main__':main()
