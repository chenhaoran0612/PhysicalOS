"""Agent integration tests use a local Responses protocol fixture, never a paid API."""
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch
import agent_binary  # noqa: F401  (optional NEUXMIND_TEST_AGENT_BINARY)
from brainos.service import Platform
from brainos import device_access, agent_runtime
from test_product import Server
from brainos.commerce import Commerce
from ssh_fixture import SSHFixture


class Device:
    calls=[]
    def __init__(self,*args):pass
    def probe(self,name):
        self.calls.append(name)
        return {'probe':name,'stdout':'Linux fixture; CPU: Test ARM processor; ROS test-only','stderr':'','exit_code':0}
    def config(self,path,listing=False):return {'path':path,'text':'fixture: true'}
    def close(self):pass


class ResponsesFixture(BaseHTTPRequestHandler):
    requests=[]
    delay=False
    engineering=False
    def log_message(self,*args):pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers['Content-Length']))
        data=json.loads(raw);type(self).requests.append(data)
        if self.delay:time.sleep(2)
        text=json.dumps(data,ensure_ascii=False)
        evidence=re.findall(r'evidence-[a-f0-9]{12}',text)
        saved='inventory-' in text
        turn=len(type(self).requests)
        if not evidence:item={'id':'fc_1','type':'function_call','call_id':'call_1','name':'device_probe','arguments':json.dumps({'probe':'system'})}
        elif not saved:item={'id':'fc_2','type':'function_call','call_id':'call_2','name':'save_inventory','arguments':json.dumps({'summary':'测试设备档案','components':[{'category':'system','name':'Linux fixture','details':'由本机协议测试返回','evidence_ids':[evidence[-1]]}],'unknowns':['驱动与动作尚未验证'],'recommendations':['继续核对厂商驱动']},ensure_ascii=False)}
        else:item={'id':'msg_3','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'已保存本体调查档案。','annotations':[]}]}
        if type(self).engineering:
            if turn==1:item={'id':'fc_work','type':'function_call','call_id':'call_work','name':'exec_command','arguments':json.dumps({'cmd':"printf '# fixture adapter\\n' > adapter.py"})}
            else:item={'id':'msg_work','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'已生成适配器工程文件。','annotations':[]}]}
        if hasattr(self,'select_item'):item=self.select_item(data,item)
        response={'id':'resp_'+str(turn),'object':'response','created_at':int(time.time()),'status':'completed','model':'fixture-agent','output':[item],
                  'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                {'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':''} if item['type']=='function_call' else item}]
        if item['type']=='function_call':events.append({'type':'response.function_call_arguments.delta','item_id':item['id'],'output_index':0,'delta':item['arguments']})
        else:events.append({'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':'已保存本体调查档案。'})
        events += [{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e,ensure_ascii=False)+'\n\n' for e in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name)
        self.pid=self.platform.add_project({'name':'Test','objective':'Discover robot'})['id']
        self.robot=self.platform.add_robot({'project_id':self.pid,'name':'Test robot','family':'custom'})
        self.engine=self.platform.engineering
        self.store=self.platform.store
        self.store.put('device_connections',{'id':'connection-test','project_id':self.pid,'robot_id':self.robot['id'],'host':'127.0.0.1','port':22,'username':'test','auth':'password','fingerprint':'SHA256:test','read_roots':['/opt/ros']})
        self.store.put('agent_secrets',{'id':'connection-test','password':'SSH_SECRET_MUST_STAY_PRIVATE'})
        robot=self.store.get('robots',self.robot['id']);robot['connection_id']='connection-test';self.store.put('robots',robot)
    def tearDown(self):self.platform.close();self.tmp.cleanup()
    def profile(self,url='http://127.0.0.1:9999/v1'):
        return self.engine.model_add({'project_id':self.pid,'name':'Test agent','model':'fixture-agent','base_url':url,'api_key':'API_SECRET_MUST_STAY_PRIVATE'})
    def test_secrets_are_not_in_snapshot_or_model_config(self):
        self.profile();serialized=json.dumps(self.platform.snapshot())
        self.assertNotIn('SSH_SECRET_MUST_STAY_PRIVATE',serialized);self.assertNotIn('API_SECRET_MUST_STAY_PRIVATE',serialized)
    def test_discovery_requires_authorization_and_project_model_match(self):
        model=self.profile()
        with self.assertRaises(ValueError):self.engine.start({'robot_id':self.robot['id'],'model_id':model['id']})
        other=self.platform.add_project({'name':'Other','objective':'Other'})['id']
        m=self.engine.model_add({'project_id':other,'name':'Other','model':'test','base_url':'http://localhost:9999/v1','api_key':'key'})
        with self.assertRaises(ValueError):self.engine.start({'robot_id':self.robot['id'],'model_id':m['id'],'data_authorized':True})
    def test_no_arbitrary_probe_or_secret_directory(self):
        for roots in [['/'],['/etc'],['/home/me/.ssh'],['/opt/ros/../../etc']]:
            with self.assertRaises(ValueError):device_access.validate_roots(roots)
        with self.assertRaises(ValueError):device_access.target({'host':'host;shutdown','port':22,'username':'test'})
        redacted=device_access.redact('api_key: abc\npassword=secret\nAuthorization: Bearer token')
        self.assertNotIn('abc',redacted);self.assertNotIn('=secret',redacted)
    def test_inventory_requires_same_session_evidence(self):
        self.store.put('agent_sessions',{'id':'s','project_id':self.pid,'robot_id':self.robot['id'],'connection_id':'connection-test','events':[]})
        args={'summary':'Test','components':[{'category':'system','name':'made up','details':'unsupported','evidence_ids':['missing']}],'unknowns':[],'recommendations':[]}
        with self.assertRaises(ValueError):self.engine._tool('s','save_inventory',args,Device())
        evidence=self.engine._tool('s','device_probe',{'probe':'system'},Device())['evidence_id']
        args['components'][0]['evidence_ids']=[evidence]
        report=self.engine._tool('s','save_inventory',args,Device())
        self.assertTrue(report['saved']);self.assertEqual(self.store.get('robots',self.robot['id'])['status'],'unconnected')
    @unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
    def test_real_runtime_drives_tools_and_saves_inventory(self):
        ResponsesFixture.requests=[];ResponsesFixture.delay=False;ResponsesFixture.engineering=False
        server=Server(ResponsesFixture)
        try:
            model=self.profile(server.url+'/v1')
            with patch('brainos.device_access.SSHDevice',Device):
                s=self.engine.start({'robot_id':self.robot['id'],'model_id':model['id'],'data_authorized':True,'max_tokens':1000,'max_minutes':1})
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    s=self.store.get('agent_sessions',s['id'])
                    if s['status'] not in ('starting','running','waiting_input','cancelling'):break
                    time.sleep(.1)
                self.assertEqual(s['status'],'completed',json.dumps(s,ensure_ascii=False))
                self.assertTrue(s['inventory_id']);self.assertGreater(s['usage'].get('totalTokens',0),0)
                self.assertEqual(len(ResponsesFixture.requests),3)
                data=json.dumps(ResponsesFixture.requests)
                self.assertNotIn('SSH_SECRET_MUST_STAY_PRIVATE',data)
                self.assertNotIn('API_SECRET_MUST_STAY_PRIVATE',data)
                names=[t.get('name',t.get('type')) for t in ResponsesFixture.requests[0]['tools']]
                self.assertIn('exec_command',names)
                self.assertIn('device_probe',names)
                self.assertIn('device_exec',names)
                self.assertIn('device_request_scope',names)
                print('Real runtime model tools:',names)
        finally:server.close()

    @unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
    def test_real_runtime_edits_workspace_and_collects_artifact(self):
        ResponsesFixture.requests=[];ResponsesFixture.engineering=True
        server=Server(ResponsesFixture)
        try:
            model=self.profile(server.url+'/v1')
            s=self.engine.start_work({'project_id':self.pid,'model_id':model['id'],'name':'Adapter fixture','objective':'Create adapter.py in the project workspace','data_authorized':True,'max_tokens':1000,'max_minutes':1})
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                s=self.store.get('agent_sessions',s['id'])
                if s['status'] not in ('starting','running','waiting_input','cancelling'):break
                time.sleep(.1)
            self.assertEqual(s['status'],'completed',json.dumps(s,ensure_ascii=False))
            self.assertTrue(any(a['path']=='adapter.py' for a in s.get('artifacts',[])),json.dumps(s,ensure_ascii=False))
            artifact=self.engine.artifact({'session_id':s['id'],'path':'adapter.py'})
            self.assertIn('fixture adapter',artifact['content'])
            with self.assertRaises(ValueError):self.engine.artifact({'session_id':s['id'],'path':'../../registry.sqlite3'})
        finally:server.close();ResponsesFixture.engineering=False

    @unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
    def test_ssh_to_real_runtime_to_inventory_end_to_end(self):
        ssh=SSHFixture();server=Server(ResponsesFixture)
        ResponsesFixture.requests=[];ResponsesFixture.engineering=False
        try:
            check=self.engine.scan({'project_id':self.pid,'host':'127.0.0.1','port':ssh.port,'username':'robot'})
            p={'project_id':self.pid,'host_check_id':check['id'],'trust_fingerprint':check['fingerprint'],'auth':'password','password':'fixture-password','name':'SSH robot','family':'mobile','read_roots':['/opt/ros']}
            with self.assertRaises(ValueError):self.engine.connect({**p,'trust_fingerprint':'SHA256:wrong'})
            c=self.engine.connect(p);self.assertEqual(ssh.commands,[])
            model=self.profile(server.url+'/v1')
            session=self.engine.start({'robot_id':c['robot_id'],'model_id':model['id'],'data_authorized':True,'max_tokens':1000,'max_minutes':1})
            session=self.wait_finished(session['id'])
            self.assertEqual(session['status'],'completed',session)
            self.assertTrue(ssh.commands)
            self.assertTrue(all(cmd in [x[1] for x in device_access.PROBES.values()] for cmd in ssh.commands))
            self.assertTrue(self.store.get('robots',c['robot_id'])['inventory_id'])
            self.assertNotIn('fixture-password',json.dumps(ResponsesFixture.requests))
            wrong={**c,'fingerprint':'SHA256:changed'}
            with self.assertRaises(ValueError):device_access.SSHDevice(wrong,{'password':'fixture-password'})
        finally:ssh.close();server.close()

    def test_concurrent_questions_and_approvals_are_not_overwritten(self):
        import queue
        self.store.put('agent_sessions',{'id':'queued','status':'running','events':[]})
        self.engine.live['queued']={'answers':queue.Queue()}
        try:
            self.engine._queue_request('queued','question',{'id':11,'questions':[{'id':'q','question':'Target?'}]})
            self.engine._queue_request('queued','approval',{'id':12,'method':'item/commandExecution/requestApproval','request':{'command':'test'}})
            s=self.store.get('agent_sessions','queued')
            self.assertEqual(s['pending_question']['id'],11);self.assertIsNone(s['pending_approval'])
            self.engine.answer({'session_id':'queued','question_id':11,'answers':{'q':'fixture'}})
            s=self.store.get('agent_sessions','queued');self.assertEqual(s['pending_approval']['id'],12)
            self.engine.approve({'session_id':'queued','approval_id':12,'decision':'decline'})
            self.assertEqual(self.store.get('agent_sessions','queued')['status'],'running')
            self.assertEqual(self.engine.live['queued']['answers'].qsize(),2)
        finally:self.engine.live.pop('queued')

    @unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
    def test_resume_keeps_native_thread_and_stop_interrupts(self):
        ResponsesFixture.requests=[];ResponsesFixture.engineering=False;ResponsesFixture.delay=False
        server=Server(ResponsesFixture)
        try:
            model=self.profile(server.url+'/v1')
            with patch('brainos.device_access.SSHDevice',Device):
                s=self.engine.start({'robot_id':self.robot['id'],'model_id':model['id'],'data_authorized':True,'max_tokens':1000,'max_minutes':1})
                s=self.wait_finished(s['id']);thread_id=s['thread_id'];inventory=s['inventory_id']
                self.engine.resume({'session_id':s['id'],'message':'Explain the existing inventory.','data_authorized':True})
                s=self.wait_finished(s['id']);self.assertEqual(s['status'],'completed',s);self.assertEqual(s['thread_id'],thread_id);self.assertEqual(s['inventory_id'],inventory)
                ResponsesFixture.delay=True
                self.engine.resume({'session_id':s['id'],'message':'Continue investigation.','data_authorized':True})
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if self.store.get('agent_sessions',s['id']).get('turn_id'):break
                    time.sleep(.05)
                self.engine.stop({'session_id':s['id']});s=self.wait_finished(s['id']);self.assertEqual(s['status'],'interrupted',s)
        finally:ResponsesFixture.delay=False;server.close()

    def wait_finished(self,sid):
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            s=self.store.get('agent_sessions',sid)
            if s['status'] not in ('starting','running','waiting_input','cancelling'):return s
            time.sleep(.1)
        self.fail('Session did not finish: '+json.dumps(s,ensure_ascii=False))

    @unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
    def test_platform_gateway_reserves_and_settles_metered_model(self):
        class Metered(ResponsesFixture):
            requests=[];grants=[]
            def reply(self,obj):
                raw=json.dumps(obj).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):self.reply({'schema':'platform.service.v1','kind':'reasoning','protocol':'responses','models':['fixture-agent'],'metered_sessions':True})
            def do_POST(self):
                if self.path=='/v1/agent-sessions':
                    self.grants.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    return self.reply({'session_id':'test-meter','base_url':f'http://127.0.0.1:{self.server.server_port}/v1','access_token':'scoped-test-token'})
                if self.path.endswith('/close'):
                    self.rfile.read(int(self.headers['Content-Length']));return self.reply({'status':'closed','usage_units':1,'usage_evidence_ref':'fixture://usage/1'})
                return super().do_POST()
        server=Server(Metered);commerce=Commerce(self.tmp.name+'/commerce')
        self.platform.commerce=commerce;self.platform.user_id='customer'
        try:
            offer=commerce.publish({'name':'Metered test model','kind':'reasoning','description':'test','unit':'1k_tokens','credits_per_unit':3,'endpoint':server.url,'token':'provider-private-key'})
            commerce.grant({'user_id':'customer','credits':100,'reason':'test'},'operator')
            model=self.engine.use_offer({'project_id':self.pid,'offer_id':offer['id'],'model':'fixture-agent'})
            with patch('brainos.device_access.SSHDevice',Device):
                session=self.engine.start({'robot_id':self.robot['id'],'model_id':model['id'],'data_authorized':True,'max_tokens':3000,'max_minutes':1})
                session=self.wait_finished(session['id'])
            self.assertEqual(session['status'],'completed',session)
            self.assertEqual(session['metering_status'],'settled')
            self.assertEqual(commerce.balance('customer')['available'],97)
            self.assertEqual(Metered.grants[0]['max_tokens'],3000)
            self.assertEqual(commerce.balance('customer')['reserved'],0)
            self.assertNotIn('provider-private-key',json.dumps(self.platform.snapshot()))
        finally:server.close();commerce.close()

if __name__=='__main__':unittest.main()
