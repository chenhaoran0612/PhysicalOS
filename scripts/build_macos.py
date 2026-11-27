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
APP=DIST/'NeuxMind Physical OS.app'
VERSION='0.21.16'

# (source, destination inside the bundle). Destinations mirror the source tree so that
# brainos finds them at the same place relative to itself (brainos/runtime_bridge.py).
DATA=[(ROOT/'web','web'),(ROOT/'runtime'/'examples'/'skills','runtime/examples/skills'),
      (ROOT/'brainos'/'skills','brainos/skills')]     # issue #2: built-in hardware-init Skill (text files)
# Issue #19: the management layer's install package (scripts/build_device_package.py), sent to robots from the console.
DATA+=[(BUILD/'device-package','device-package')]
# Issue #54: the robot model packages of the built-in profiles (scripts/robot_models.py). None of them is in the repository.
DATA+=[(BUILD/'robot-models','robot_models')]
# Issue #63: the Microduck kit (scripts/duck_kit.py): its skill model packages and the physics engine they were accepted on.
# None of it is in the repository. The engine is a folder of its own, loaded only by the duck's physics child.
DATA+=[(BUILD/'duck-kit','duck_kit')]
PATHS=[ROOT,ROOT/'spec',ROOT/'runtime']
PACKAGES=['brainos','neuxmind_spec','neuxmind_runtime']
# Imported lazily (inside functions or by name) and therefore named explicitly.
HIDDEN=['brainos.sim_child','brainos.sim_robots','brainos.runtime_bridge','neuxmind_runtime.cli',
        'neuxmind_runtime.adapters.sim_quadruped','neuxmind_runtime.adapters.sim_arm',
        # Issue #38: the console checks a real body's device configuration with the adapter's own parser.
        'neuxmind_runtime.adapters.ros2','neuxmind_runtime.adapters.programs']
# Issue #51: MuJoCo simulation. The engine runs only in the simulation's own child processes (the body named by
# --adapter brainos.mujoco_body:body, and the checking tool behind --nx-mujoco-tool); its off-screen renderer and
# the platform's OpenGL context are found by name at run time. Its plugin libraries are loaded from its folder.
HIDDEN+=['brainos.mujoco_body','brainos.mujoco_tool','mujoco','mujoco.renderer','mujoco.gl_context',
         'mujoco.rendering.classic.gl_context']+(['mujoco.cgl'] if sys.platform=='darwin' else [])
BINARIES=['mujoco']
# Issue #63: the simulated duck's body (--adapter brainos.duck_body:body) and its physics child (--nx-duck-engine).
HIDDEN+=['brainos.duck_body','brainos.duck_engine','brainos.duck_policy','brainos.duck_paths']


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
    for package in BINARIES:
        args+=['--collect-binaries',package]
    for source,target in DATA:
        args+=['--add-data',f'{source}{os.pathsep}{target}']
    return args+['--distpath',distpath,'--workpath',workpath,'--specpath',specpath,ROOT/'desktop/launcher.py']


def build_device_package():
    """Issue #19: (re)build the install package the console sends to robots, into .build/device-package."""
    # Its summary goes to stderr: --check prints only its own JSON report on stdout.
    subprocess.run([sys.executable,str(ROOT/'scripts/build_device_package.py'),'--out',str(BUILD/'device-package')],cwd=ROOT,check=True,stdout=sys.stderr)


def build_robot_models():
    """Issue #54: (re)build the robot model packages into .build/robot-models. A package that cannot be built here
    (no network and no earlier download, or the folder of the models that are not public is missing) is left out:
    the app then lists that brand and model as not available. The summary goes to stderr."""
    subprocess.run([sys.executable,str(ROOT/'scripts/robot_models.py'),'--out',str(BUILD/'robot-models')],cwd=ROOT,check=False,stdout=sys.stderr)
    (BUILD/'robot-models').mkdir(parents=True,exist_ok=True)


def build_duck_kit():
    """Issue #63: (re)build the Microduck kit into .build/duck-kit. Without the folder of its files on this
    computer the kit is empty and the app lists the duck as not available. The summary goes to stderr."""
    subprocess.run([sys.executable,str(ROOT/'scripts/duck_kit.py'),'build','--out',str(BUILD/'duck-kit')],cwd=ROOT,check=False,stdout=sys.stderr)
    (BUILD/'duck-kit').mkdir(parents=True,exist_ok=True)


def check():
    """Linux-runnable: everything the bundle is built from exists and imports cleanly."""
    problems=[]
    if not (BUILD/'robot-models'/'NOTICE.txt').is_file():build_robot_models()   # issue #54
    if not (BUILD/'duck-kit').is_dir():build_duck_kit()   # issue #63
    if not (BUILD/'device-package'/'package.json').is_file():build_device_package()   # issue #19
    try:
        sys.path.insert(0,str(ROOT));from brainos import runtime_package;runtime_package.load(BUILD/'device-package')
    except Exception as exc:problems.append(f'管理层安装包不可用：{exc}')   # issue #19
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
    if '--nx-mujoco-tool' not in launcher:
        problems.append('desktop/launcher.py 不处理 --nx-mujoco-tool（打包后无法检查仿真环境）')   # issue #51
    if '--nx-duck-engine' not in launcher:
        problems.append('desktop/launcher.py 不处理 --nx-duck-engine（打包后仿真小鸭无法运行技能模型）')   # issue #63
    code=('import importlib,sys\n'
          f'sys.path[:0]={[str(p) for p in PATHS]!r}\n'
          f'for m in {PACKAGES+HIDDEN!r}: importlib.import_module(m)\n'
          'from brainos import runtime_bridge\n'
          'print(runtime_bridge.examples_dir())\n')
    result=subprocess.run([sys.executable,'-I','-c',code],cwd=str(ROOT),capture_output=True,text=True,timeout=120)
    if result.returncode:
        problems.append('导入失败：'+result.stderr.strip()[-800:])
    models=sorted(p.stem for p in (BUILD/'robot-models').glob('*.zip'))   # issue #54: which profiles this build can simulate
    report={'ok':not problems,'examples':examples,'robot_models':models,'data':[str(t) for _,t in DATA],'packages':PACKAGES,
            'hidden_imports':HIDDEN,'problems':problems}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def _http(method,url,body=None,opener=None,timeout=30):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json'} if data else {})
    with (opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))).open(req,timeout=timeout) as r:
        return json.loads(r.read())


def profile_smoke(api):
    """Issue #54: the packaged backend lists the built-in profiles and starts a robot of the first one whose model
    package is in the bundle (the whole MuJoCo robot, as a child of the packaged backend)."""
    catalog=api('state')['sim_catalog']
    assert [t['id'] for t in catalog['types']]==['manipulator','wheel_legged','quadruped','biped','other'],catalog['types']
    usable=[p for p in catalog['profiles'] if p['available'] and not p['builtin']]
    if not usable:
        return {'profiles':[],'profile_robot':'no model package in this bundle'}
    robot=api('sims',{'profile_id':usable[0]['id']})
    assert robot['status']=='connected' and robot['sim']['kind']=='mujoco' and robot['sim']['state']=='running',robot
    live=api('robots/live',{'robot_id':robot['id']})
    assert live.get('reachable') and live.get('simulated'),live
    api('sims/remove',{'robot_id':robot['id']})
    return {'profiles':[p['id'] for p in usable],'profile_robot':usable[0]['id']+' started'}


def smoke(backend,state_dir):
    """Drive a frozen backend the way the app does: a simulated robot, an example skill, a run."""
    import http.cookiejar
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    env['NEUXMIND_MAIL_DEV_ECHO']='1'  # freeze check registers without a mail server
    env['NEUXMIND_CLOUD_AUTO_LOGIN']='0'  # issue #36: ... and must not register its test account in a cloud on this computer
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
        assert names==['free_walk','goto_place','patrol','patrol_places','status_report','wave'],gallery
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
        profiles=profile_smoke(api)   # issue #54
        return {'backend':base,'robot':robot['id'],'sandbox':'inprocess (frozen)','run':live['run']['status'],**profiles}
    finally:
        process.terminate()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()


MUJOCO_SMOKE_URDF=b"""<?xml version="1.0"?>
<robot name="freeze"><link name="trunk"><inertial><mass value="5"/><inertia ixx="0.05" iyy="0.05" izz="0.05" ixy="0" ixz="0" iyz="0"/></inertial>
<collision><geometry><box size="0.4 0.2 0.1"/></geometry></collision></link>
<link name="leg"><inertial><origin xyz="0 0 -0.1"/><mass value="0.5"/><inertia ixx="0.005" iyy="0.005" izz="0.005" ixy="0" ixz="0" iyz="0"/></inertial>
<collision><origin xyz="0 0 -0.1"/><geometry><cylinder radius="0.02" length="0.2"/></geometry></collision></link>
<joint name="hip" type="revolute"><parent link="trunk"/><child link="leg"/><axis xyz="0 1 0"/><limit lower="-1" upper="1" velocity="5" effort="20"/></joint>
</robot>"""


def mujoco_smoke(backend,work):
    """Issue #51: the frozen backend builds a MuJoCo scene from a URDF in its own child process and draws it."""
    sys.path.insert(0,str(ROOT))
    from brainos import mujoco_model,mujoco_scene
    work=pathlib.Path(work);(work/'robot.urdf').write_bytes(MUJOCO_SMOKE_URDF)
    mujoco_model.prepare(work/'robot.urdf','urdf',work/'model')
    (work/'check.json').write_text(json.dumps({'model_dir':str(work/'model'),'scene':mujoco_scene.EMPTY,'family':'quadruped'}))
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    done=subprocess.run([str(backend),'--nx-mujoco-tool','check',str(work/'check.json'),str(work/'out')],capture_output=True,text=True,timeout=180,env=env,cwd=str(work))
    lines=done.stdout.strip().splitlines()
    assert done.returncode==0 and lines,(done.returncode,done.stderr[-800:])
    result=json.loads(lines[-1])
    assert result.get('ok') is True and result['summary']['joints']==['hip'],result
    # The picture needs OpenGL, which a build machine without a display may lack: reported, not required.
    return {'mujoco':'ok','mujoco_preview':result.get('preview') is True,'mujoco_render_error':result.get('render_error','')}


def duck_smoke(backend,work):
    """Issue #63: the frozen backend, started as the duck's physics child, performs one skill model of the kit on the
    engine that is packaged next to it (not the MuJoCo the rest of the bundle uses). Without a kit: says so."""
    kit=backend.parent/'_internal'/'duck_kit'
    packages=sorted((kit/'shows').glob('*.zip')) if (kit/'kit.json').is_file() else []
    if not packages or not (kit/'engine'/'mujoco').is_dir():
        return {'duck':'no kit in this build'}
    sys.path.insert(0,str(ROOT));from brainos import duck_kit
    work=pathlib.Path(work);package=duck_kit.unpack(packages[0],work/'package')
    child=subprocess.Popen([str(backend),'--nx-duck-engine','--package',str(package),'--view',str(work/'view'),'--engine',str(kit/'engine')],
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=str(work))
    try:
        ready=json.loads(child.stdout.readline() or '{}')
        assert ready.get('event')=='ready' and ready.get('mujoco')==duck_kit.ENGINE['mujoco'],(ready,child.stderr.read()[-800:] if child.poll() is not None else '')
        child.stdin.write(json.dumps({'cmd':'show','id':'smoke'})+'\n');child.stdin.flush()
        done=next((e for e in map(json.loads,child.stdout) if e.get('event')=='done'),None)
    finally:
        child.stdin.close();child.wait(15)
    assert done and done.get('success') is True,done
    return {'duck':'ok','duck_show':ready['task'],'duck_engine':ready['mujoco'],'duck_steps':done['steps'],'duck_frame':(work/'view'/'frame.png').is_file()}


def freeze_check(out=None):
    """Freeze the backend with the same arguments as the Mac build (current OS) and smoke-test it."""
    out=pathlib.Path(out or tempfile.mkdtemp(prefix='neuxmind-freeze-'))
    out.mkdir(parents=True,exist_ok=True)
    build_device_package()   # issue #19: the bundle carries the install package
    build_robot_models()     # issue #54: and the robot model packages
    build_duck_kit()         # issue #63: and the Microduck kit
    run([sys.executable,'-m','PyInstaller','--log-level','WARN',*pyinstaller_args(out/'dist',out/'work',out)])
    backend=out/'dist'/'brainos-server'/'brainos-server'
    with tempfile.TemporaryDirectory(prefix='neuxmind-freeze-state-') as state:
        result=smoke(backend,pathlib.Path(state))
    with tempfile.TemporaryDirectory(prefix='neuxmind-freeze-mujoco-') as work:
        result.update(mujoco_smoke(backend,work))   # issue #51
    with tempfile.TemporaryDirectory(prefix='neuxmind-freeze-duck-') as work:
        result.update(duck_smoke(backend,work))   # issue #63
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
    build_device_package()   # issue #19: always from the tree being built, never an older .build copy
    build_robot_models()     # issue #54: likewise the robot model packages
    build_duck_kit()         # issue #63: and the Microduck kit
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
         ROOT/'desktop/main.swift',ROOT/'desktop/DataFolder.swift','-o',macos/'NeuxMind'])
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
             '--identifier','com.neuxmind.robotos','--version',VERSION,DIST/f'NeuxMind-Physical-OS-{VERSION}-arm64.pkg'])
        (staging/'Applications').symlink_to('/Applications')
        shutil.copy2(ROOT/'desktop/INSTALL.txt',staging/'安装说明.txt')
        run(['xattr','-cr',staging])
        dmg=DIST/f'NeuxMind-Physical-OS-{VERSION}-arm64.dmg'
        if dmg.exists():dmg.unlink()
        run(['hdiutil','create','-volname','NeuxMind Physical OS','-srcfolder',staging,'-ov','-format','UDZO',dmg])
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
