"""Real bundled runtime + SSH/SFTP transport; local model protocol, no paid API."""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from brainos import agent_runtime, device_access
from brainos.service import Platform
from brainos.engineering import model_failure_message
from test_engineering import ResponsesFixture
from test_product import Server
from ssh_fixture import SSHFixture

COMMAND='systemctl show robot-stack --property=ExecStart --no-pager'
ROOT='/home/robot/vendor_ws'
FILE=ROOT+'/driver.py'


class DiscoveryModel(ResponsesFixture):
    requests=[];engineering=False;delay=False
    def select_item(self,data,fallback):
        step=len(type(self).requests)-1
        evidence=list(dict.fromkeys(re.findall(r'evidence-[a-f0-9]{12}',json.dumps(data))))
        calls=[('device_probe',{'probe':'system'}),
               ('device_exec',{'command':COMMAND,'purpose':'查明本体服务的实际启动入口','timeout_seconds':10}),
               ('device_request_scope',{'path':ROOT,'purpose':'读取启动入口所在的厂商工程目录'}),
               ('device_config',{'path':FILE,'listing':False,'offset':0}),
               ('device_config',{'path':FILE,'listing':False,'offset':48000}),
               ('exec_command',{'cmd':"rg --version > analysis.txt && rg 'name' DEVICE_CONTEXT.json >> analysis.txt"}),
               ('save_inventory',{'summary':'从服务启动入口追查厂商源码，保存调查证据',
                 'components':[{'category':'system','name':'Linux fixture','details':'协议验收主机','evidence_ids':evidence[:1]},
                               {'category':'software','name':'robot-stack','details':'已追查厂商驱动源码与启动入口','evidence_ids':evidence[1:]}],
                 'unknowns':['未现场验证动作；其他基础探针尚未完成'],'recommendations':['继续补齐硬件与接口调查']})]
        if step>=len(calls):return {'id':'msg_done','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'已保存有来源的本体档案。','annotations':[]}]}
        name,args=calls[step]
        return {'id':f'fc_{step}','type':'function_call','call_id':f'call_{step}','name':name,'arguments':json.dumps(args,ensure_ascii=False)}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Platform(self.tmp.name);self.e=self.p.engineering
        self.pid=self.p.add_project({'name':'Discovery','objective':'Inspect vendor configuration'})['id']
        self.ssh=SSHFixture(files={FILE:b'# vendor driver\n'+b'x'*48000+b'\nprotocol: fixture-bus\n','/opt/ros/readme.txt':b'fixture'},
                            command_outputs={COMMAND:b'ExecStart=/home/robot/vendor_ws/driver.py\n'})
        scan=self.e.scan({'project_id':self.pid,'host':'127.0.0.1','port':self.ssh.port,'username':'robot'})
        self.connection=self.e.connect({'project_id':self.pid,'host_check_id':scan['id'],'trust_fingerprint':scan['fingerprint'],
            'auth':'password','password':'fixture-password','name':'Vendor fixture','read_roots':['/opt/ros']})
        self.robot=self.connection['robot_id']
        robot=self.p.store.get('robots',self.robot);robot['token']='ADAPTER_SECRET_NOT_CONTEXT';self.p.store.put('robots',robot)
    def tearDown(self):self.p.close();self.ssh.close();self.tmp.cleanup()
    def poll(self,sid,predicate,seconds=30):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            s=self.p.store.get('agent_sessions',sid)
            if predicate(s):return s
            time.sleep(.05)
        self.fail('Timed out: '+json.dumps(s,ensure_ascii=False))
    def start(self,url):
        model=self.e.model_add({'project_id':self.pid,'name':'Fixture','model':'fixture-agent','base_url':url+'/v1','api_key':'model-fixture-secret'})
        return self.e.start({'robot_id':self.robot,'model_id':model['id'],'data_authorized':True,'max_tokens':10000,'max_minutes':1})
    @unittest.skipUnless(agent_runtime.binary_path(),'Bundled runtime required')
    def test_adaptive_ssh_scope_pagination_analysis_inventory_and_resume(self):
        DiscoveryModel.requests=[];server=Server(DiscoveryModel)
        try:
            s=self.start(server.url);sid=s['id']
            a=self.poll(sid,lambda s:bool(s.get('pending_approval')))
            self.assertEqual(a['pending_approval']['method'],'device_exec/requestApproval')
            self.assertNotIn(COMMAND,self.ssh.commands)
            aid=a['pending_approval']['id'];self.e.approve({'session_id':sid,'approval_id':aid,'decision':'accept'})
            with self.assertRaises(ValueError):self.e.approve({'session_id':sid,'approval_id':aid,'decision':'accept'})
            a=self.poll(sid,lambda s:s.get('pending_approval',{} ) is not None and s.get('pending_approval',{}).get('method')=='device_request_scope/requestApproval')
            self.assertIn(COMMAND,self.ssh.commands);self.assertEqual(self.ssh.reads,[])
            self.e.approve({'session_id':sid,'approval_id':a['pending_approval']['id'],'decision':'accept'})
            s=self.poll(sid,lambda s:s['status'] not in ('starting','running','waiting_input','cancelling'))
            self.assertEqual(s['status'],'completed',s)
            report=self.p.store.get('robot_inventories',s['inventory_id'])
            self.assertEqual(report['coverage']['checked'],1);self.assertEqual(report['coverage']['total'],9)
            self.assertIn('sensors',report['unconfirmed_categories'])
            self.assertEqual(self.ssh.reads,[FILE,FILE]);self.assertEqual(s['approved_read_roots'],[ROOT])
            self.assertIn('analysis.txt',[a['path'] for a in s['artifacts']])
            analysis=self.e.artifact({'session_id':sid,'path':'analysis.txt'})['content']
            self.assertIn('ripgrep',analysis);self.assertIn('Vendor fixture',analysis)
            context=(self.p.store.root/'engineering'/sid/'workspace/DEVICE_CONTEXT.json').read_text()
            self.assertNotIn('ADAPTER_SECRET_NOT_CONTEXT',context)
            evidence=self.p.store.list('device_evidence');pages=[e['result'] for e in evidence if e['tool']=='device_config']
            self.assertTrue(any(p['next_offset']==48000 for p in pages))
            self.assertTrue(any('fixture-bus' in p['text'] for p in pages))
            transcript=json.dumps(DiscoveryModel.requests)
            for secret in ['fixture-password','model-fixture-secret']:self.assertNotIn(secret,transcript)
            thread=s['thread_id'];inventory=s['inventory_id']
            self.e.resume({'session_id':sid,'message':'解释已有档案','data_authorized':True})
            s=self.poll(sid,lambda s:s['status'] not in ('starting','running','waiting_input','cancelling'))
            self.assertEqual(s['thread_id'],thread);self.assertEqual(s['inventory_id'],inventory)
        finally:server.close()

    @unittest.skipUnless(agent_runtime.binary_path(),'Bundled runtime required')
    def test_declined_ssh_command_is_not_executed_and_pending_scope_can_stop(self):
        DiscoveryModel.requests=[];server=Server(DiscoveryModel)
        try:
            s=self.start(server.url);sid=s['id']
            a=self.poll(sid,lambda s:bool(s.get('pending_approval')))
            self.e.approve({'session_id':sid,'approval_id':a['pending_approval']['id'],'decision':'decline'})
            self.poll(sid,lambda s:bool(s.get('pending_approval')))
            self.assertNotIn(COMMAND,self.ssh.commands)
            self.e.stop({'session_id':sid})
            s=self.poll(sid,lambda s:s['status']=='interrupted')
            self.assertIsNone(s['pending_approval']);self.assertEqual(self.ssh.reads,[])
        finally:server.close()

    def test_sftp_scope_symlinks_pagination_and_credentials(self):
        d=device_access.SSHDevice(self.connection,{'password':'fixture-password'})
        try:
            with self.assertRaises(ValueError):d.config(FILE)
            d.grant_root(ROOT)
            self.ssh.links[ROOT+'/escape.py']='/etc/shadow'
            with self.assertRaises(ValueError):d.config(ROOT+'/escape.py')
            with self.assertRaises(ValueError):d.config(FILE,offset=-1)
            self.assertEqual(d.config(FILE)['next_offset'],48000)
            self.assertIn('fixture-bus',d.config(FILE,offset=48000)['text'])
            self.assertEqual(d.config(ROOT,listing=True)['entries'][0]['name'],'driver.py')
            self.assertNotIn('fixture-password',d.clean('echo fixture-password'))
        finally:d.close()

    def test_direct_tool_cannot_bypass_approval(self):
        sid='session-direct';self.p.store.put('agent_sessions',{'id':sid,'project_id':self.pid,'robot_id':self.robot,'connection_id':self.connection['id']})
        for name,args in [('device_exec',{'command':COMMAND,'purpose':'inspect','timeout_seconds':10}),('device_request_scope',{'path':ROOT,'purpose':'inspect'})]:
            with self.assertRaises(ValueError):self.e._tool(sid,name,args,object())
        self.assertEqual(self.ssh.commands,[])

    def test_overview_preserves_full_evidence_and_failed_probes(self):
        sid='session-overview';self.p.store.put('agent_sessions',{'id':sid,'project_id':self.pid,'robot_id':self.robot,'connection_id':self.connection['id'],'events':[]})
        class Device:
            def probe(self,name):
                if name=='containers':raise ValueError('No container access')
                return {'probe':name,'label':name,'stdout':'x'*15000,'stderr':'','exit_code':0}
        device=Device();result=self.e._tool(sid,'device_overview',{},device)
        self.assertEqual(result['coverage']['checked'],9)
        self.assertTrue(result['results'][0]['summary_only'])
        self.assertEqual(result['coverage']['probes'][-1]['state'],'limited')
        eid=result['results'][0]['evidence_id'];a=self.e._tool(sid,'device_evidence',{'evidence_id':eid},device)
        b=self.e._tool(sid,'device_evidence',{'evidence_id':eid,'offset':a['next_offset']},device)
        self.assertEqual(json.loads(a['text']+b['text'])['stdout'],'x'*15000)
        other='session-other';self.p.store.put('agent_sessions',{'id':other,'project_id':self.pid,'robot_id':self.robot,'connection_id':self.connection['id']})
        with self.assertRaises(ValueError):self.e._tool(other,'device_evidence',{'evidence_id':eid},device)

    def test_command_shape_is_validated_without_claiming_shell_safety(self):
        for args in [{'command':'x','purpose':'inspect','timeout_seconds':True},{'command':'x\0','purpose':'inspect'},{'command':'x','purpose':''}]:
            with self.assertRaises(ValueError):device_access.command_request(args)
        self.assertEqual(device_access.command_request({'command':'cat a | head','purpose':'inspect'})['command'],'cat a | head')

    def test_gateway_failure_is_actionable_and_keys_are_redacted(self):
        message=model_failure_message('502 Bad Gateway: first valid SSE chunk before 1m0s precommit timeout')
        self.assertIn('已保留',message);self.assertIn('不会自动重复',message)
        self.assertNotIn('sk-example-123456789',device_access.redact('invalid key sk-example-123456789'))

if __name__=='__main__':unittest.main()
