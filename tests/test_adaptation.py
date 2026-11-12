"""Round 04 §6.2: the "本体适配" workflow template.

Unit tests run everywhere. The end-to-end group drives all four stages through
the real Codex 0.146.1 app-server (``NEUXMIND_TEST_AGENT_BINARY``) against a
local Responses double (stages 1, 2) and the **real** runtime
``python -m neuxmind_runtime scaffold|conformance`` (stages 2, 3; runtime
09c1297, ROS 2 parts on the runtime's rclpy test double). ``runtime_double.py``
is only used when the runtime package cannot be run here
(``NEUXMIND_TEST_RUNTIME_DOUBLE=1`` forces it); its report shape is checked
against the same strict parser.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_binary  # noqa: F401
from brainos import adaptation, agent_runtime
from brainos.service import Platform
from adaptation_support import DOUBLE, AdaptationModel, mapping_for, seed_inventory
from test_product import Server

ROOT=Path(__file__).resolve().parents[1]


def real_runtime_available():
    if os.environ.get('NEUXMIND_TEST_RUNTIME_DOUBLE')=='1':return False
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'runtime'),str(ROOT/'spec')])}
    try:return subprocess.run([sys.executable,'-c','import neuxmind_runtime.scaffold,neuxmind_runtime.conformance'],env=env,capture_output=True,timeout=60).returncode==0
    except (OSError,subprocess.TimeoutExpired):return False


REAL_TOOLS=real_runtime_available()


class use_double:
    """Runtime tools for the tests: the real ``python -m neuxmind_runtime``
    (default), or the CLI double when the runtime cannot run here."""
    def __init__(self):
        self.patches=[patch.object(adaptation,'RUNTIME_TOOL',None if REAL_TOOLS else str(DOUBLE))]
    def start(self):
        for p in self.patches:p.start()
    def stop(self):
        for p in reversed(self.patches):p.stop()
    def __enter__(self):self.start();return self
    def __exit__(self,*exc):self.stop()


class MappingValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name)
        self.pid=self.platform.add_project({'name':'Adapt','objective':'unit'})['id']
        self.rid,self.report,self.ids=seed_inventory(self.platform,self.pid)
        self.work=Path(self.tmp.name)/'work';self.work.mkdir()
        self.run={'definition':{'inventory_id':self.report['id'],'robot_id':self.rid}}
    def tearDown(self):self.platform.close();self.tmp.cleanup()
    def check(self,mapping):
        (self.work/'mapping.json').write_text(json.dumps(mapping,ensure_ascii=False))
        return adaptation.validate_mapping(self.platform.workflows,self.run,0,self.work,{},[])['mapping']

    def test_every_capability_mapped_and_unevidenced_entries_need_confirmation(self):
        mapping=self.check(mapping_for(self.report,self.ids,adaptation.registry()))
        rows={r['id']:r for r in mapping['capabilities']}
        self.assertEqual(len(rows),len(adaptation.registry()))
        self.assertEqual(rows['base.velocity']['implementation'],'ros2_topic')
        self.assertEqual(rows['sensor.battery']['implementation'],'needs_confirmation')      # claimed vendor_sdk, no evidence
        self.assertTrue(rows['sensor.battery']['downgraded']);self.assertEqual(rows['sensor.battery']['declared_implementation'],'vendor_sdk')
        self.assertEqual(rows['arm.joint_position']['implementation'],'unsupported')        # has evidence: kept
        self.assertEqual(rows['base.odometry']['implementation'],'needs_confirmation');self.assertEqual(rows['base.odometry']['missing_params'],['frame'])
        self.assertEqual(mapping['downgraded'],2)
        caps,params=adaptation.scaffold_selection(mapping)
        self.assertEqual(set(caps),{'safety.stop','system.health','base.velocity','sensor.camera.depth'})
        self.assertEqual(params['base.velocity']['max_vx'],0.5)

    def test_unsupported_without_evidence_is_also_needs_confirmation(self):
        m=mapping_for(self.report,self.ids,adaptation.registry())
        next(r for r in m['capabilities'] if r['id']=='arm.joint_position')['evidence_ids']=[]
        rows={r['id']:r for r in self.check(m)['capabilities']}
        self.assertEqual(rows['arm.joint_position']['implementation'],'needs_confirmation')

    def test_rejects_foreign_evidence_missing_unknown_and_duplicate_capabilities(self):
        registry=adaptation.registry()
        with self.assertRaisesRegex(ValueError,'档案中不存在的证据'):self.check(mapping_for(self.report,self.ids,registry,foreign=True))
        m=mapping_for(self.report,self.ids,registry);m['capabilities']=m['capabilities'][1:]
        with self.assertRaisesRegex(ValueError,'缺少标准能力'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);m['capabilities'].append({'id':'vendor.magic','implementation':'serial','evidence_ids':[]})
        with self.assertRaisesRegex(ValueError,'非标准能力'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);m['capabilities'].append(dict(m['capabilities'][0]))
        with self.assertRaisesRegex(ValueError,'重复'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);m['capabilities'][0]['implementation']='telepathy'
        with self.assertRaisesRegex(ValueError,'实现方式无效'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);next(r for r in m['capabilities'] if r['id']=='base.velocity')['params']['max_vx']=-1
        with self.assertRaisesRegex(ValueError,'参数无效'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);next(r for r in m['capabilities'] if r['id']=='base.velocity')['params']['turbo']=True
        with self.assertRaisesRegex(ValueError,'不在能力标准中'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);m['schema']='other'
        with self.assertRaisesRegex(ValueError,'schema'):self.check(m)
        m=mapping_for(self.report,self.ids,registry);m['inventory_id']='inventory-other'
        with self.assertRaisesRegex(ValueError,'固定的本体档案'):self.check(m)

    def test_start_requires_inventory_and_latest_version(self):
        other=self.platform.add_robot({'project_id':self.pid,'name':'无档案','family':'mobile'})
        with self.assertRaisesRegex(ValueError,'还没有本体档案'):self.platform.workflows.start_adaptation({'robot_id':other['id'],'model_id':'x','data_authorized':True})
        with self.assertRaisesRegex(ValueError,'最新的本体档案'):self.platform.workflows.start_adaptation({'robot_id':self.rid,'inventory_id':'inventory-old','model_id':'x','data_authorized':True})

    def test_users_cannot_define_product_stages(self):
        model=self.platform.engineering.model_add({'project_id':self.pid,'name':'m','model':'m','base_url':'http://127.0.0.1:9/v1','api_key':'k'})
        stage={'name':'x','role':'x','objective':'x','kind':'product','template_step':'conformance','required_outputs':['a.json'],'model_id':model['id']}
        with self.assertRaisesRegex(ValueError,'阶段类型无效'):self.platform.workflows.create({'project_id':self.pid,'name':'n','objective':'o','stages':[stage]})


def sim_report(level='dry'):
    """A real conformance.report.v1 from the runtime CLI on sim_quadruped."""
    with tempfile.TemporaryDirectory() as tmp:
        out=Path(tmp)/'r.json'
        with patch.object(adaptation,'RUNTIME_TOOL',None):
            r=adaptation.run_tool(['conformance','--adapter','sim_quadruped','--level',level,'--time-scale','20','--out',str(out)],Path(tmp),120)
        if r['exit_code']!=0:raise AssertionError(r['stderr'])
        return json.loads(out.read_text())


class ReportParsingTests(unittest.TestCase):
    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_parses_the_real_runtime_report(self):
        report=sim_report('dry')
        parsed=adaptation.parse_report(report,'dry')
        self.assertTrue(parsed['ok']);self.assertEqual(parsed['fields']['field_acceptance'],False)
        self.assertEqual(parsed['fields']['evidence_kind'],'simulation');self.assertTrue(parsed['fields']['simulated'])
        self.assertEqual(set(parsed['levels']),{'static','dry','motion'});self.assertTrue(parsed['levels']['dry']['ran'])
        self.assertEqual(set(parsed['levels']['dry']),set(adaptation.LEVEL_FIELDS))
        stop=parsed['capabilities']['safety.stop'];self.assertTrue(stop['declared']);self.assertEqual(stop['status'],'passed')
        self.assertTrue(any(c['id']=='dry.safety.stop.idempotent_latched' for c in stop['cases']))
        undeclared=[r for r in parsed['capabilities'].values() if not r['declared']]
        self.assertTrue(undeclared);self.assertTrue(all(r['status']=='skipped' and r['skip_kind']=='not_applicable' for r in undeclared))
        if 'manifest_digest' in report:self.assertEqual(parsed['fields']['manifest_digest'],report['manifest_digest'])

    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_refuses_acceptance_claims_and_foreign_shapes(self):
        report=sim_report('static')
        for change,message in (({'field_acceptance':True},'field_acceptance'),({'field_acceptance':None},'field_acceptance'),
                               ({'evidence_kind':'device_run'},'evidence_kind'),({'schema':'x'},'conformance.report.v1'),
                               ({'level':'dry'},'级别'),({'static_ok':None},'static_ok'),({'levels':{}},'levels')):
            with self.assertRaisesRegex(ValueError,message):adaptation.parse_report({**report,**change},'static')
        broken=json.loads(json.dumps(report));broken['capabilities'][0]['levels'].pop('static')
        with self.assertRaisesRegex(ValueError,'static 级结果'):adaptation.parse_report(broken,'static')

    def test_motion_is_never_a_conformance_level_here(self):
        self.assertEqual(adaptation.LEVELS,('static','dry'))

    def test_families_match_the_spec(self):
        sys.path.insert(0,str(ROOT/'spec'))
        try:
            from neuxmind_spec import manifest
            self.assertEqual(tuple(manifest.FAMILIES),adaptation.FAMILIES)
        finally:sys.path.remove(str(ROOT/'spec'))


class RuntimeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def script(self,body):
        path=self.dir/'tool.py';path.write_text(body);return patch.object(adaptation,'RUNTIME_TOOL',str(path))

    def test_minimal_environment_without_secrets(self):
        with self.script('import json,os,sys;print(json.dumps(dict(os.environ)))'),patch.dict(os.environ,{'NEUXMIND_MODEL_KEY':'sk-secret','BRAIN_EDGE_TOKEN':'tok'}):
            result=adaptation.run_tool([],self.dir,10)
        env=json.loads(result['stdout'])
        self.assertNotIn('NEUXMIND_MODEL_KEY',env);self.assertNotIn('BRAIN_EDGE_TOKEN',env)
        self.assertNotIn('sk-secret',result['stdout'])

    def test_timeout_and_stop_kill_the_process_group(self):
        body='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);time.sleep(60)'
        with self.script(body):
            t=time.monotonic();result=adaptation.run_tool([],self.dir,1);self.assertEqual(result['killed'],'timeout');self.assertLess(time.monotonic()-t,8)
            stop=threading.Event();threading.Timer(.5,stop.set).start()
            t=time.monotonic();result=adaptation.run_tool([],self.dir,60,stop);self.assertEqual(result['killed'],'stopped');self.assertLess(time.monotonic()-t,8)

    MAPPING={'capabilities':[{'id':'safety.stop','implementation':'ros2_topic','interface':'/cmd_vel 零速度','evidence_ids':['e1'],'params':{'hardware_estop':False}},
                             {'id':'system.health','implementation':'ros2_topic','interface':'/diagnostics','evidence_ids':['e1']},
                             {'id':'base.velocity','implementation':'ros2_topic','interface':'/cmd_vel geometry_msgs/Twist','evidence_ids':['e1'],'params':{'max_vx':0.5,'max_vy':0.0,'max_wz':1.0,'holonomic':False}},
                             {'id':'sensor.imu','implementation':'vendor_sdk','interface':'imu.read()','evidence_ids':['e2'],'params':{'rate_hz':100.0,'frame':'imu'}}]}
    ROBOT={'id':'robot-unit','family':'mobile'}
    def selection(self,mapping=None):
        mapping=mapping or self.MAPPING
        caps=[x['id'] for x in mapping['capabilities']];params={x['id']:x['params'] for x in mapping['capabilities'] if x.get('params')}
        return mapping,caps,params

    def test_scaffold_spec_uses_runtime_hint_kinds(self):
        mapping,caps,params=self.selection()
        spec=adaptation.scaffold_spec({'definition':{'robot_id':'robot-unit'}},self.ROBOT,mapping,caps,params)
        self.assertEqual(spec['schema'],'neuxmind.adapter_scaffold.v1');self.assertEqual(spec['name'],'nx_robot_unit')
        self.assertEqual([c['implementation'] for c in spec['capabilities']],['ros2:/cmd_vel','ros2:/diagnostics','ros2:/cmd_vel','sdk'])
        self.assertEqual(spec['capabilities'][0]['evidence_id'],'e1')
        hint=adaptation.scaffold_hint
        self.assertEqual(hint({'implementation':'serial','interface':'/dev/ttyUSB0 115200'}),'serial:/dev/ttyUSB0')
        self.assertEqual(hint({'implementation':'other','interface':'http://10.0.0.2/api'}),'http:http://10.0.0.2/api')
        self.assertEqual(hint({'implementation':'other','interface':'厂商私有协议'}),'todo')
        self.assertEqual(hint({'implementation':'ros2_topic','interface':'/a"b'}),'ros2')
        self.assertEqual(adaptation.scaffold_spec({'definition':{}},{'id':'r','family':'wheeled'},mapping,caps,params)['family'],'custom')

    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_real_scaffold_then_conformance_on_the_rclpy_double(self):
        mapping,caps,params=self.selection()
        with use_double():
            result=adaptation.scaffold({'definition':{'robot_id':'robot-unit'}},self.dir/'adapter',caps,params,None,self.ROBOT,mapping)
            self.assertEqual(result['adapter'],'nx_robot_unit.adapter:NxRobotUnitAdapter')
            self.assertEqual(result['implemented'],['safety.stop','system.health','base.velocity']);self.assertEqual(result['todo'],['sensor.imu'])
            self.assertEqual(result['mandatory_todo'],[]);self.assertIn('nx_robot_unit/adapter.py',result['files']);self.assertIn('tests/test_adapter.py',result['files'])
            env,double=adaptation.conformance_env(self.dir);self.assertTrue(double)
            for level in adaptation.LEVELS:
                r=adaptation.run_tool(['conformance','--adapter',result['adapter'],'--level',level,'--out',str(self.dir/(level+'.json'))],self.dir,120,None,env)
                self.assertEqual(r['exit_code'],0,r['stderr'])
                parsed=adaptation.parse_report(json.loads((self.dir/(level+'.json')).read_text()),level)
                self.assertTrue(parsed['ok']);self.assertTrue(parsed['fields']['simulated'])           # use_sim_time on the double
                self.assertEqual(parsed['capabilities']['system.health']['status'],'passed')
                self.assertFalse(parsed['capabilities']['sensor.imu']['declared'])                     # TODO: not declared
            self.assertEqual(parsed['capabilities']['base.velocity']['status'],'passed')
            self.assertTrue(any(c['id']=='dry.base.velocity.zero_setpoint' for c in parsed['capabilities']['base.velocity']['cases']))

    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_scaffold_refuses_missing_limits_with_exit_2(self):
        mapping,caps,params=self.selection();params=dict(params);params['base.velocity']={'max_vx':0.5}
        with use_double(),self.assertRaisesRegex(ValueError,'(?s)exit 2.*base.velocity 缺少必填参数'):
            adaptation.scaffold({'definition':{'robot_id':'robot-unit'}},self.dir/'adapter',caps,params,None,self.ROBOT,mapping)
        self.assertFalse((self.dir/'adapter').exists())

    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_unimplemented_mandatory_capability_is_refused_with_exit_2(self):
        mapping=json.loads(json.dumps(self.MAPPING));mapping['capabilities'][0]['implementation']='vendor_sdk'
        mapping,caps,params=self.selection(mapping)
        with use_double():
            result=adaptation.scaffold({'definition':{'robot_id':'robot-unit'}},self.dir/'adapter',caps,params,None,self.ROBOT,mapping)
            self.assertEqual(result['mandatory_todo'],['safety.stop'])
            env,_=adaptation.conformance_env(self.dir)
            r=adaptation.run_tool(['conformance','--adapter',result['adapter'],'--level','static','--out',str(self.dir/'s.json')],self.dir,120,None,env)
        self.assertEqual(r['exit_code'],2);self.assertIn('必备能力',r['stderr']);self.assertFalse((self.dir/'s.json').exists())

    def test_cli_double_matches_the_report_contract(self):
        """runtime_double.py is the fallback when the runtime cannot run: its
        output must satisfy the same strict parser as the real reports."""
        mapping,caps,params=self.selection()
        with patch.object(adaptation,'RUNTIME_TOOL',str(DOUBLE)):
            result=adaptation.scaffold({'definition':{'robot_id':'robot-unit'}},self.dir/'adapter',caps,params,None,self.ROBOT,mapping)
            self.assertEqual(result['adapter'],'nx_robot_unit.adapter:NxRobotUnitAdapter')
            env,_=adaptation.conformance_env(self.dir)
            for level in adaptation.LEVELS:
                r=adaptation.run_tool(['conformance','--adapter',result['adapter'],'--level',level,'--out',str(self.dir/(level+'.json'))],self.dir,60,None,env)
                self.assertEqual(r['exit_code'],0,r['stderr'])
                parsed=adaptation.parse_report(json.loads((self.dir/(level+'.json')).read_text()),level)
                self.assertTrue(parsed['ok']);self.assertEqual(parsed['capabilities']['system.health']['status'],'passed')


def minimal_report(level='dry',ok=True,aborted=None,failed=0,blocked=0,prelatched=False):
    """A conformance.report.v1 with the runtime's field set (no runtime needed)."""
    def lv(name,ran):return {'requested':ran,'ran':ran,'ok':(ok if name==level else True) if ran else None,'passed':3 if ran else 0,
                             'failed':failed if name==level else 0,'skipped':0,'blocked':blocked if name==level else 0,'blocked_reasons':['测试前已锁存'] if blocked else []}
    ran={'static':True,'dry':level=='dry','motion':False}
    return {'schema':'conformance.report.v1','conformance_id':'cf-unit','level':level,'simulated':True,'evidence_kind':'simulation','field_acceptance':False,
            'disclaimer':'不等于现场验收证据（ADR-011）','static_ok':True if level=='dry' else ok,'dry_ok':ok if level=='dry' else None,'motion_ok':None,
            'levels':{k:lv(k,v) for k,v in ran.items()},'prelatched':prelatched,'aborted':aborted,'final_latched':bool(aborted),
            'capabilities':[{'id':'safety.stop','declared':True,'levels':{'static':{'status':'passed','reason':''},'dry':{'status':'failed' if failed else 'passed','reason':aborted or ''}}}],
            'cases':[]}


class ExitCodeAgreementTests(unittest.TestCase):
    """Review D4: the report's verdict and the CLI exit code must agree."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name);(self.dir/'adapter').mkdir()
    def tearDown(self):self.tmp.cleanup()
    def tool(self,report,code):
        (self.dir/'report.json').write_text(json.dumps(report,ensure_ascii=False))
        script=self.dir/'tool.py'
        script.write_text('import shutil,sys\nargs=sys.argv[1:]\nshutil.copy(%r,args[args.index("--out")+1])\nsys.exit(%d)\n'%(str(self.dir/'report.json'),code))
        return patch.object(adaptation,'RUNTIME_TOOL',str(script))
    def level(self,level='dry'):
        return adaptation.conformance_level('nx_unit.adapter:NxUnitAdapter',level,self.dir,{'PYTHONPATH':str(self.dir/'adapter')})

    def test_agreeing_exit_codes_are_accepted(self):
        with self.tool(minimal_report(ok=True),0):report,record=self.level()
        self.assertEqual(record['exit_code'],0);self.assertTrue(report['dry_ok'])
        with self.tool(minimal_report(ok=False,failed=1),1):report,record=self.level()
        self.assertEqual(record['exit_code'],1)

    def test_a_report_contradicting_its_exit_code_is_refused(self):
        with self.tool(minimal_report(ok=True),1),self.assertRaisesRegex(ValueError,'报告结论与退出码不一致.*dry_ok=True.*exit 1'):self.level()
        with self.tool(minimal_report(ok=False,failed=1),0),self.assertRaisesRegex(ValueError,'报告结论与退出码不一致.*dry_ok=False.*exit 0'):self.level()

    def test_exit_2_and_missing_reports(self):
        with self.tool(minimal_report(),2),self.assertRaisesRegex(ValueError,'exit 2'):self.level()
        script=self.dir/'none.py';script.write_text('import sys;sys.exit(1)\n')
        with patch.object(adaptation,'RUNTIME_TOOL',str(script)),self.assertRaisesRegex(ValueError,'没有写出报告'):self.level()


class AbortedReportTests(unittest.TestCase):
    """After the runtime's safety rewrite a run can be aborted (e.g. operator
    emergency stop) or blocked (latched before the test): never 'passed'."""
    MAPPING={'capabilities':[{'id':c['id'],'implementation':'ros2_topic' if c['id']=='safety.stop' else 'unsupported'} for c in adaptation.registry()]}
    def summary(self,static,dry):
        return adaptation.summarize({},self.MAPPING,{'static':adaptation.parse_report(static,'static'),'dry':adaptation.parse_report(dry,'dry')},
                                    [{'level':'static','conformance_id':'a','exit_code':0,'network_isolated':True},{'level':'dry','conformance_id':'b','exit_code':1,'network_isolated':True}])

    def test_aborted_run_is_shown_as_aborted(self):
        reason='一致性测试已中止：急停被其他来源锁存（operator）；一致性测试不会替操作员解除急停'
        s=self.summary(minimal_report('static'),minimal_report('dry',ok=False,failed=1,blocked=4,aborted=reason))
        self.assertEqual(s['verdict'],{'static':'passed','dry':'aborted','motion':'not_run'})
        self.assertEqual(s['aborted'],reason);self.assertTrue(s['final_latched']);self.assertTrue(s['network_isolated'])
        self.assertIn('适配器所在进程',s['self_reported_note'])

    def test_blocked_and_prelatched(self):
        s=self.summary(minimal_report('static'),minimal_report('dry',ok=False,blocked=3,prelatched=True))
        self.assertEqual(s['verdict']['dry'],'blocked');self.assertTrue(s['prelatched'])

    def test_aborted_report_claiming_ok_is_refused(self):
        with self.assertRaisesRegex(ValueError,'aborted'):adaptation.parse_report(minimal_report('dry',ok=True,aborted='x'),'dry')
        with self.assertRaisesRegex(ValueError,'aborted 无效'):adaptation.parse_report({**minimal_report('dry',ok=False,failed=1),'aborted':True},'dry')


class TamperScreenTests(unittest.TestCase):
    """Review Q4-13: adapter code must not reach the suite running in its process."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'bot').mkdir()
    def tearDown(self):self.tmp.cleanup()
    def screen(self,code,name='bot/adapter.py'):
        (self.root/name).write_text(code);return adaptation.tamper_findings(self.root)

    def test_suite_patching_is_found(self):
        for code in ('from neuxmind_runtime import conformance as c\nc.Suite._record=lambda *a:None\n',
                     'import neuxmind_runtime.safety\n','import neuxmind_runtime\n','import importlib\nimportlib.import_module("neuxmind_runtime."+"core")\n',
                     'import sys\nsys.modules["x"]=1\n','exec("import os")\n','from neuxmind_runtime.core import EdgeRuntime\n','from .. import x\n'):
            self.assertTrue(self.screen(code),code)
        (self.root/'bot'/'adapter.py').write_text('x=1\n')
        self.assertTrue(self.screen('print(1)\n','sitecustomize.py'))
        (self.root/'sitecustomize.py').unlink();(self.root/'bot'/'x.pth').write_text('import os')
        self.assertTrue(adaptation.tamper_findings(self.root))

    def test_ordinary_adapter_code_and_tests_pass(self):
        self.assertEqual(self.screen('from neuxmind_runtime.hal import RobotAdapter\nfrom neuxmind_runtime.adapters.ros2 import Ros2Bridge\nimport json, os\n'),[])
        (self.root/'tests').mkdir();(self.root/'tests'/'test_adapter.py').write_text('from neuxmind_runtime import conformance\n')
        (self.root/'bot'/'__pycache__').mkdir();(self.root/'bot'/'__pycache__'/'adapter.cpython-313.pyc').write_bytes(b'x')
        self.assertEqual(adaptation.tamper_findings(self.root),[])

    @unittest.skipUnless(REAL_TOOLS,'runtime package required')
    def test_real_scaffold_output_passes_the_screen(self):
        mapping=RuntimeToolTests.MAPPING;caps=[x['id'] for x in mapping['capabilities']];params={x['id']:x['params'] for x in mapping['capabilities'] if x.get('params')}
        with use_double():adaptation.scaffold({'definition':{'robot_id':'robot-unit'}},self.root/'adapter',caps,params,None,RuntimeToolTests.ROBOT,mapping)
        self.assertEqual(adaptation.tamper_findings(self.root/'adapter'),[])


class NetworkIsolationTests(unittest.TestCase):
    """Review Q4-14: stage 3 runs without network where the platform allows it."""
    def test_isolated_tool_cannot_reach_a_local_listener(self):
        if not adaptation.network_isolation():self.skipTest('no network namespace / sandbox on this host')
        import socket
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);port=server.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script=Path(tmp)/'probe.py'
                script.write_text('import socket,sys\ns=socket.socket();s.settimeout(3)\nprint(s.connect_ex(("127.0.0.1",%d)))\n'%port)
                with patch.object(adaptation,'RUNTIME_TOOL',str(script)):
                    open_=adaptation.run_tool([],Path(tmp),30);closed=adaptation.run_tool([],Path(tmp),30,isolate=True)
        finally:server.close()
        self.assertEqual(open_['stdout'].strip(),'0');self.assertFalse(open_['network_isolated'])
        self.assertNotEqual(closed['stdout'].strip(),'0',closed['stderr']);self.assertTrue(closed['network_isolated'])


@unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
class AdaptationWorkflowTests(unittest.TestCase):
    """Real Codex + local Responses double + real runtime scaffold/conformance, four stages."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name);self.flows=self.platform.workflows;self.store=self.platform.store
        AdaptationModel.requests=[];AdaptationModel.delay=0.0
        self.server=Server(AdaptationModel)
        self.pid=self.platform.add_project({'name':'本体适配','objective':'§6.2'})['id']
        self.rid,self.report,self.ids=seed_inventory(self.platform,self.pid)
        AdaptationModel.mapping=mapping_for(self.report,self.ids,adaptation.registry())
        self.model=self.platform.engineering.model_add({'project_id':self.pid,'name':'fixture','model':'fixture','base_url':self.server.url+'/v1','api_key':'fixture-key'})
        self.tool=use_double();self.tool.start()
    def tearDown(self):self.tool.stop();self.platform.close();self.server.close();self.tmp.cleanup()
    def start(self):
        return self.flows.start_adaptation({'robot_id':self.rid,'project_id':self.pid,'inventory_id':self.report['id'],'model_id':self.model['id'],'data_authorized':True,'max_tokens':5000,'max_minutes':2})
    def wait(self,rid,statuses=('awaiting_review','blocked','interrupted','completed'),seconds=60):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            run=self.store.get('workflow_runs',rid)
            if run['status'] in statuses and rid not in self.flows.live:return run
            time.sleep(.05)
        self.fail('timeout: '+json.dumps({'status':run['status'],'error':run.get('error'),'live':rid in self.flows.live,'stages':[(s['status'],s.get('session_id')) for s in run['stages']],
                                          'events':run['events'][-5:]},ensure_ascii=False))
    def approve(self,rid,note='已核对'):
        return self.flows.review({'run_id':rid,'decision':'approve','note':note})

    def test_four_stages_end_to_end(self):
        run=self.start();rid=run['id']
        self.assertEqual(run['definition']['template'],'robot_adaptation');self.assertEqual(run['definition']['inventory_id'],self.report['id'])
        run=self.wait(rid);self.assertEqual(run['status'],'awaiting_review',run.get('error'))
        mapping=run['stages'][0]['adaptation']['mapping']
        self.assertEqual(mapping['inventory_id'],self.report['id']);self.assertEqual(mapping['downgraded'],2)
        session=self.store.get('agent_sessions',run['stages'][0]['session_id'])
        context=json.loads((Path(self.tmp.name)/'engineering'/session['id']/'workspace'/'ADAPTATION.json').read_text())
        self.assertEqual({c['id'] for c in context['registry']},{c['id'] for c in adaptation.registry()})
        self.assertIn(self.ids['ros'],context['evidence_excerpts'])
        self.assertNotIn('ADAPTATION.json',[a['path'] for a in run['stages'][0]['artifacts']])
        self.approve(rid);run=self.wait(rid);self.assertEqual(run['status'],'awaiting_review',run.get('error'))
        stage=run['stages'][1];paths=[a['path'] for a in stage['artifacts']]
        module=stage['adaptation']['adapter_entry'].split('.')[0]
        for name in ('adapter/'+module+'/adapter.py','adapter/'+module+'/adapter.json','adapter/README.md','adapter/IMPLEMENTATION.md'):self.assertIn(name,paths)
        self.assertEqual(set(stage['adaptation']['adapter_capabilities']),{'safety.stop','system.health','base.velocity','sensor.camera.depth'})
        self.assertTrue(stage['verified_commands']);self.assertEqual(stage['adaptation']['static_screen'],'passed')
        self.assertIn('本机',stage['adaptation']['next_step_note'])
        self.approve(rid);run=self.wait(rid);self.assertEqual(run['status'],'awaiting_review',run.get('error'))
        stage=run['stages'][2];conf=stage['adaptation']['conformance']
        self.assertEqual(conf['verdict'],{'static':'passed','dry':'passed','motion':'not_run'})
        # Summary fields are the runtime report's (conformance.report.v1).
        self.assertEqual(set(conf['levels']['dry']),set(adaptation.LEVEL_FIELDS));self.assertTrue(conf['levels']['dry']['ok'])
        self.assertIs(conf['levels']['motion']['requested'],False);self.assertIs(conf['field_acceptance'],False)
        self.assertIn(conf['evidence_kind'],('simulation','conformance'));self.assertIn('ADR-011',conf['disclaimer'])
        self.assertEqual(set(conf['exit_codes'].values()),{0});self.assertIsNone(conf['aborted'])
        self.assertEqual(conf['network_isolated'],bool(adaptation.network_isolation()))
        rows={r['id']:r for r in conf['capabilities']}
        self.assertEqual(rows['system.health']['dry'],'passed');self.assertEqual(rows['base.velocity']['dry'],'passed')
        # The real scaffold has no ROS 2 template for the depth camera yet (TODO, undeclared).
        self.assertEqual(rows['sensor.camera.depth']['dry'],'not_declared')
        self.assertEqual(rows['sensor.imu']['levels']['static']['skip_kind'],'not_applicable')
        self.assertEqual(rows['base.velocity']['motion'],'not_run');self.assertEqual(rows['sensor.imu']['static'],'not_declared')
        self.assertIn('不等于现场验收证据',conf['note']);self.assertIs(conf['simulated'],REAL_TOOLS)          # rclpy double: use_sim_time
        self.assertEqual(stage['attempts'][-1]['usage_tokens'],0);self.assertIsNone(stage['session_id'] if 'session_id' in stage else None)
        record=self.store.get('conformance_reports',conf['report_id']);self.assertEqual(record['robot_id'],self.rid);self.assertNotIn('review',record)
        raw=self.flows.artifact({'run_id':rid,'stage_index':2,'path':'conformance/dry.report.json'})
        self.assertEqual(json.loads(raw['content'])['schema'],'conformance.report.v1')
        self.approve(rid,'已核对 static/dry 报告');self.assertEqual(self.store.get('conformance_reports',conf['report_id'])['review']['decision'],'approved')
        run=self.wait(rid);self.assertEqual(run['status'],'awaiting_review',run.get('error'))
        report=json.loads(self.flows.artifact({'run_id':rid,'stage_index':3,'path':'adaptation.report.json'})['content'])
        self.assertEqual({x['id'] for x in report['available']},{'safety.stop','system.health','base.velocity'})
        onsite={x['id'] for x in report['needs_onsite_verification']}
        self.assertIn('base.velocity',onsite);self.assertIn('sensor.battery',onsite)
        self.assertIn('sensor.camera.depth',{x['id'] for x in report['not_implemented']})      # scaffold TODO, not declared
        self.assertIs(report['field_acceptance'],False);self.assertEqual(report['levels'],{'static':'passed','dry':'passed','motion':'not_run'})
        self.assertIn('arm.joint_position',{x['id'] for x in report['not_implemented']})
        self.assertTrue(any('motion' in r for r in report['risks']))
        text=self.flows.artifact({'run_id':rid,'stage_index':3,'path':'ADAPTATION_REPORT.md'})['content']
        for heading in ('可用能力','未实现','需现场验证','风险','不等于现场验收证据'):self.assertIn(heading,text)
        run=self.approve(rid);self.assertEqual(run['status'],'completed')
        # The robot detail (SSH-discovered body, no runtime endpoint) still opens,
        # and the snapshot carries the latest conformance summary for the UI.
        detail=self.platform.robot_detail({'robot_id':self.rid,'project_id':self.pid})
        self.assertEqual(detail['robot']['id'],self.rid)
        reports=[r for r in self.platform.snapshot()['conformance_reports'] if r['robot_id']==self.rid]
        self.assertEqual([r['id'] for r in reports],[conf['report_id']]);self.assertEqual(reports[0]['verdict']['motion'],'not_run')
        self.assertEqual(run['stages'][2]['adaptation']['conformance']['created_at'],reports[0]['created_at'])
        # Only the two agent stages called the model.
        self.assertEqual(sum(1 for s in run['stages'] for a in s['attempts'] if a.get('session_id')),2)

    def test_adapter_code_reaching_the_suite_blocks_stage_2(self):
        """Review Q4-13: real Codex writes an import of the conformance suite
        into the adapter; the stage-2 gate refuses it before any test runs."""
        AdaptationModel.tamper=True
        try:
            rid=self.start()['id'];self.wait(rid);self.approve(rid);run=self.wait(rid)
        finally:AdaptationModel.tamper=False
        self.assertEqual(run['status'],'blocked');self.assertIn('静态检查',run['error']);self.assertIn('引用运行时内部模块',run['error'])
        self.assertFalse(self.store.list('conformance_reports'))

    def test_foreign_evidence_blocks_the_mapping_stage(self):
        AdaptationModel.mapping=mapping_for(self.report,self.ids,adaptation.registry(),foreign=True)
        run=self.wait(self.start()['id'])
        self.assertEqual(run['status'],'blocked');self.assertIn('档案中不存在的证据',run['error'])

    def test_failed_dry_check_reaches_the_report_as_a_risk(self):
        """Stage 2 (real Codex, native command) breaks the double payload of
        system.health: dry must fail and the report must say so."""
        AdaptationModel.break_health=True
        try:
            rid=self.start()['id'];self.wait(rid);self.approve(rid);run=self.wait(rid)
            # Stage 2 must reach review: a blocked stage here means the native command itself failed.
            self.assertEqual(run['status'],'awaiting_review',run.get('error'))
            self.approve(rid);run=self.wait(rid)
        finally:AdaptationModel.break_health=False
        conf=run['stages'][2]['adaptation']['conformance']
        self.assertEqual(conf['verdict']['dry'],'failed');self.assertEqual(conf['verdict']['static'],'passed');self.assertEqual(conf['exit_codes'],{'static':0,'dry':1})
        rows={r['id']:r for r in conf['capabilities']};self.assertEqual(rows['system.health']['dry'],'failed')
        self.assertEqual([c['status'] for c in run['stages'][2]['handoff']['checks']],['passed','failed','not_run'])
        self.approve(rid,'记录 dry 失败');run=self.wait(rid)
        report=json.loads(self.flows.artifact({'run_id':rid,'stage_index':3,'path':'adaptation.report.json'})['content'])
        self.assertNotIn('system.health',{x['id'] for x in report['available']})
        self.assertIn('system.health',{x['id'] for x in report['not_implemented']})
        self.assertTrue(any('system.health' in r and '失败' in r for r in report['risks']))

    def test_stop_during_conformance_is_bounded_and_saves_no_report(self):
        rid=self.start()['id'];self.wait(rid);self.approve(rid);self.wait(rid)
        real=adaptation.run_tool
        def tool(args,cwd,timeout,cancel=None,extra_env=None,**kw):
            if args and args[0]=='conformance':
                # The adapter copy under test hangs on import (real runtime CLI) / the double's hook.
                module=Path(cwd)/'adapter'/args[2].split('.')[0]
                (module/'HANG_CONFORMANCE').write_text('test hook')
                with open(module/'adapter.py','a') as f:f.write('\nimport time as _t\n_t.sleep(600)\n')
            return real(args,cwd,timeout,cancel,extra_env,**kw)
        with patch.object(adaptation,'run_tool',tool):
            self.approve(rid)
            deadline=time.monotonic()+20
            while self.store.get('workflow_runs',rid)['stages'][2]['status']!='running' and time.monotonic()<deadline:time.sleep(.05)
            time.sleep(.5);started=time.monotonic();self.flows.stop({'run_id':rid})
            run=self.wait(rid,('interrupted',),20)
        self.assertLess(time.monotonic()-started,5)
        self.assertEqual(run['stages'][2]['status'],'interrupted');self.assertFalse(self.store.list('conformance_reports'))
        # Retry (product stage, no model call) produces the report.
        requests=len(AdaptationModel.requests)
        self.flows.retry({'run_id':rid,'feedback':'重新运行一致性测试','data_authorized':True});run=self.wait(rid)
        self.assertEqual(run['stages'][2]['adaptation']['conformance']['verdict']['dry'],'passed');self.assertEqual(len(AdaptationModel.requests),requests)
        self.assertEqual(len(run['stages'][2]['attempts']),2)

if __name__=='__main__':unittest.main()
