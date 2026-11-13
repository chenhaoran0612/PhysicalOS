"""Reproducible Apple Silicon app + installer. Run using the packaging venv.

    python scripts/build_macos.py                 # full build on a Mac (PyInstaller + Swift + codesign + pkg/dmg)
    python scripts/build_macos.py --check         # any OS: the bundle contents the app needs are present and importable
    python scripts/build_macos.py --freeze-check  # any OS with PyInstaller: freeze the backend exactly as the
                                                  # Mac build does and smoke-test it (simulated robot, example
                                                  # skill, run) with no Python source tree on the path

The backend bundle carries everything a user needs with no Python installed:
the desktop backend (brainos), the console (web/), the shared spec
(neuxmind_spec), the edge runtime (neuxmind_runtime, used for one-click
simulated robots through ``--nx-sim-child``) and the example skills
(runtime/examples/skills, the gallery in 安装技能包).
"""
import argparse
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=pathlib.Path(__file__).resolve().parents[1]
BUILD=ROOT/'.build'
DIST=ROOT/'dist'
APP=DIST/'NeuxMind Robot OS.app'
VERSION='0.16.0'

# (source, destination inside the bundle). Destinations mirror the source tree so that
# brainos finds them at the same place relative to itself (brainos/runtime_bridge.py).
DATA=[(ROOT/'web','web'),(ROOT/'runtime'/'examples'/'skills','runtime/examples/skills'),
      (ROOT/'brainos'/'skills','brainos/skills')]     # issue #2: built-in hardware-init Skill (text files)
PATHS=[ROOT,ROOT/'spec',ROOT/'runtime']
PACKAGES=['brainos','neuxmind_spec','neuxmind_runtime']
# Imported lazily (inside functions or by name) and therefore named explicitly.
HIDDEN=['brainos.sim_child','brainos.sim_robots','brainos.runtime_bridge','neuxmind_runtime.cli',
        'neuxmind_runtime.adapters.sim_quadruped','neuxmind_runtime.adapters.sim_arm']


def run(args):
    subprocess.run([str(x) for x in args],cwd=ROOT,check=True)


def pyinstaller_args(distpath,workpath,specpath):
    args=['--noconfirm','--clean','--onedir','--name','brainos-server']
    for path in PATHS:
        args+=['--paths',path]
    for package in PACKAGES:
        args+=['--collect-submodules',package]
    for module in HIDDEN:
        args+=['--hidden-import',module]
    for source,target in DATA:
        args+=['--add-data',f'{source}{os.pathsep}{target}']
    return args+['--distpath',distpath,'--workpath',workpath,'--specpath',specpath,ROOT/'desktop/launcher.py']


def check():
    """Linux-runnable: everything the bundle is built from exists and imports cleanly."""
    problems=[]
    for source,_ in DATA:
        if not source.is_dir():
            problems.append(f'缺少数据目录 {source}')
    skills=ROOT/'runtime'/'examples'/'skills'
    examples=sorted(p.name for p in skills.iterdir() if (p/'skill.json').is_file()) if skills.is_dir() else []
    if not examples:
        problems.append('没有示例技能（runtime/examples/skills/*/skill.json）')
    launcher=(ROOT/'desktop/launcher.py').read_text(encoding='utf-8')
    if '--nx-sim-child' not in launcher:
        problems.append('desktop/launcher.py 不处理 --nx-sim-child（打包后无法启动仿真机器人）')
    code=('import importlib,sys\n'
          f'sys.path[:0]={[str(p) for p in PATHS]!r}\n'
          f'for m in {PACKAGES+HIDDEN!r}: importlib.import_module(m)\n'
          'from brainos import runtime_bridge\n'
          'print(runtime_bridge.examples_dir())\n')
    result=subprocess.run([sys.executable,'-I','-c',code],cwd=str(ROOT),capture_output=True,text=True,timeout=120)
    if result.returncode:
        problems.append('导入失败：'+result.stderr.strip()[-800:])
    report={'ok':not problems,'examples':examples,'data':[str(t) for _,t in DATA],'packages':PACKAGES,
            'hidden_imports':HIDDEN,'problems':problems}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def _http(method,url,body=None,opener=None,timeout=30):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json'} if data else {})
    with (opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))).open(req,timeout=timeout) as r:
        return json.loads(r.read())


def smoke(backend,state_dir):
    """Drive a frozen backend the way the app does: a simulated robot, an example skill, a run."""
    import http.cookiejar
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    env['NEUXMIND_MAIL_DEV_ECHO']='1'  # freeze check registers without a mail server
    process=subprocess.Popen([str(backend),'--port','0','--state-dir',str(state_dir)],stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,text=True,env=env,cwd=str(state_dir))
    try:
        base=process.stdout.readline().strip().split()[-1]
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        api=lambda path,p=None:_http('POST' if p is not None else 'GET',base+'/api/'+path,p,opener)
        form={'name':'freeze','email':'freeze@example.test','password':'freeze-check-1234'}
        api('register',{**form,'code':api('register/start',form)['dev_code']})
        robot=api('sims',{'kind':'quadruped'})
        assert robot['status']=='connected' and robot['sim']['state']=='running',robot
        gallery=api('skills/examples',{'project_id':robot['project_id'],'robot_id':robot['id']})
        names=sorted(x['example'] for x in gallery['items'] if x.get('valid'))
        assert names==['patrol','status_report','wave'],gallery
        preview=api('skills/package-preview',{'robot_id':robot['id'],'example':'status_report'})
        installed=api('skills/package-install',{'robot_id':robot['id'],'example':'status_report',
                                                'package_digest':preview['package_digest'],'request_id':'freeze-1'})
        assert installed['status']=='succeeded',installed
        op=api('robots/run-skill',{'robot_id':robot['id'],'skill_id':'status_report','parameters':{},
                                    'hardware_authorized':True,'request_id':'freeze-run-1'})
        assert op['status']=='accepted',op
        deadline=time.monotonic()+30
        while True:
            live=api('robots/live',{'robot_id':robot['id']})
            if live.get('run',{}).get('status')=='succeeded':
                break
            assert time.monotonic()<deadline,live
            time.sleep(0.5)
        return {'backend':base,'robot':robot['id'],'sandbox':'inprocess (frozen)','run':live['run']['status']}
    finally:
        process.terminate()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()


def freeze_check(out=None):
    """Freeze the backend with the same arguments as the Mac build (current OS) and smoke-test it."""
    out=pathlib.Path(out or tempfile.mkdtemp(prefix='neuxmind-freeze-'))
    out.mkdir(parents=True,exist_ok=True)
    run([sys.executable,'-m','PyInstaller','--log-level','WARN',*pyinstaller_args(out/'dist',out/'work',out)])
    backend=out/'dist'/'brainos-server'/'brainos-server'
    with tempfile.TemporaryDirectory(prefix='neuxmind-freeze-state-') as state:
        result=smoke(backend,pathlib.Path(state))
    print(json.dumps({'ok':True,'bundle':str(out/'dist'/'brainos-server'),**result},ensure_ascii=False,indent=2))
    return result


def main():
    global APP
    destination=APP
    # Keep the bundle itself outside File Provider storage until signing and
    # installer verification finish; clearing Desktop attributes races sync.
    with tempfile.TemporaryDirectory(prefix='neuxmind-build-') as temporary:
        APP=pathlib.Path(temporary)/destination.name
        build_app()
        if destination.exists():shutil.rmtree(destination)
        shutil.copytree(APP,destination,symlinks=True)
        run(['xattr','-cr',destination])
    APP=destination


def build_app():
    DIST.mkdir(exist_ok=True)
    if not check()['ok']:
        raise SystemExit('打包内容检查未通过')
    run([sys.executable,ROOT/'scripts/prepare_agent_runtime.py','--verify-only'])
    run([sys.executable,'-m','PyInstaller',*pyinstaller_args(BUILD/'backend-dist',BUILD/'pyinstaller',BUILD)])
    if APP.exists():shutil.rmtree(APP)
    macos=APP/'Contents/MacOS';resources=APP/'Contents/Resources'
    macos.mkdir(parents=True);resources.mkdir(parents=True)
    shutil.copy2(ROOT/'desktop/Info.plist',APP/'Contents/Info.plist')
    shutil.copytree(BUILD/'backend-dist/brainos-server',resources/'backend',symlinks=True)
    shutil.copytree(BUILD/'agent-runtime',resources/'agent-runtime')
    shutil.copytree(ROOT/'licenses',resources/'licenses')
    shutil.copy2(ROOT/'runtime-lock.json',resources/'runtime-lock.json')
    run(['swiftc','-O','-target','arm64-apple-macos13.0','-framework','Cocoa','-framework','WebKit',
         ROOT/'desktop/main.swift','-o',macos/'NeuxMind'])
    run(['swift',ROOT/'desktop/icon.swift',BUILD])
    run(['iconutil','-c','icns',BUILD/'AppIcon.iconset','-o',resources/'AppIcon.icns'])
    shutil.copy2(ROOT/'README.md',resources/'README.md')
    # Desktop/File Provider metadata is not valid inside signed bundles.
    run(['xattr','-cr',APP])
    run(['codesign','--force','--deep','--sign','-',APP])
    run(['codesign','--verify','--deep','--strict','--verbose=2',APP])
    package_installers()


def package_installers():
    # Stage outside Desktop/iCloud. File Provider can reattach FinderInfo to
    # copied bundles, invalidating signatures inside the final disk image.
    with tempfile.TemporaryDirectory(prefix='neuxmind-package-') as temporary:
        staging=pathlib.Path(temporary)/'payload'
        staging.mkdir()
        clean_app=staging/APP.name
        shutil.copytree(APP,clean_app,symlinks=True)
        run(['xattr','-cr',clean_app])
        run(['codesign','--verify','--deep','--strict',clean_app])
        components=pathlib.Path(temporary)/'components.plist'
        components.write_bytes(plistlib.dumps([{'RootRelativeBundlePath':APP.name,
            'BundleIsRelocatable':False,'BundleIsVersionChecked':True,'BundleOverwriteAction':'upgrade'}]))
        run(['pkgbuild','--root',staging,'--component-plist',components,'--install-location','/Applications',
             '--identifier','com.neuxmind.robotos','--version',VERSION,DIST/f'NeuxMind-Robot-OS-{VERSION}-arm64.pkg'])
        (staging/'Applications').symlink_to('/Applications')
        shutil.copy2(ROOT/'desktop/INSTALL.txt',staging/'安装说明.txt')
        run(['xattr','-cr',staging])
        dmg=DIST/f'NeuxMind-Robot-OS-{VERSION}-arm64.dmg'
        if dmg.exists():dmg.unlink()
        run(['hdiutil','create','-volname','NeuxMind Robot OS','-srcfolder',staging,'-ov','-format','UDZO',dmg])
        run(['hdiutil','verify',dmg])
        mount=pathlib.Path(temporary)/'mounted'
        mount.mkdir()
        run(['hdiutil','attach','-readonly','-nobrowse','-mountpoint',mount,dmg])
        try:
            run(['codesign','--verify','--deep','--strict',mount/APP.name])
        finally:
            run(['hdiutil','detach',mount])
    print('Built:',dmg,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check',action='store_true',help='只检查打包内容（任何系统）')
    parser.add_argument('--freeze-check',action='store_true',help='在当前系统冻结后端并做冒烟测试（需要 PyInstaller）')
    parser.add_argument('--out',default=None,help='--freeze-check 的输出目录')
    options=parser.parse_args()
    if options.check:
        sys.exit(0 if check()['ok'] else 1)
    if options.freeze_check:
        freeze_check(options.out)
        sys.exit(0)
    main()
