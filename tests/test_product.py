"""Contract fixtures exist only in tests; no emulated robots/models are shipped in the product."""
import copy
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from brainos.auth import Identity
from brainos.edge import Gateway, make_handler as edge_handler
from brainos.server import Application, make_handler
from brainos.service import Platform, digest


class DeviceFixture:
    def __init__(self):
        self.calls=[]
        self.version='1.0'

    def manifest(self):
        return {'schema':'robot.adapter.v1','instance_id':'test-device','name':'contract fixture','family':'mobile',
                'adapter_version':self.version,'capabilities':['navigation.goal','localization.pose']}

    def status(self):
        return {'instance_id':'test-device','mode':'idle'}

    def validate(self,application):
        self.calls.append('validate')
        return {'accepted':True}

    def evaluate(self,p):
        self.calls.append('evaluate')
        return {'passed':True,'application_digest':p['application']['digest'],'robot_manifest_digest':digest(self.manifest()),
                'evidence_ref':'test-fixture://report/1','evidence_kind':'device_run','simulated':False}

    def deploy(self,p):
        self.calls.append('deploy')
        return {'accepted':True,'release_id':'edge-release-1'}

    def start(self,p):
        self.calls.append('start')
        return {'accepted':True,'state':'running'}

    def stop(self,p):
        self.calls.append('stop')
        return {'accepted':True,'state':'stopping'}


class Server:
    def __init__(self,handler):
        self.http=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.url=f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Platform(self.tmp.name+'/platform')
        self.project=self.app.add_project({'name':'Delivery','objective':'Navigate and inspect a site'})
        self.pid=self.project['id']
        self.adapter=DeviceFixture()
        self.token='test-only-token-123456789000'
        self.gateway=Gateway(self.adapter,self.token,self.tmp.name+'/edge')
        self.server=Server(edge_handler(self.gateway))

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def workflow(self,connect=True):
        robot=self.app.add_robot({'project_id':self.pid,'name':'Test robot','family':'mobile','endpoint':self.server.url,'token':self.token})
        if connect:
            self.app.connect_robot({'robot_id':robot['id']})
        skill=self.app.add_skill({'project_id':self.pid,'name':'Navigate','version':'1.0','requires':['navigation.goal'],
                                  'entrypoint':'navigate','success_condition':'at goal','stop_condition':'timeout or lost localization','model_ids':[]})
        app=self.app.add_application({'project_id':self.pid,'name':'Inspection','version':'1.0','robot_id':robot['id'],
                                       'steps':[{'skill_id':skill['id'],'parameters':{'goal':[1,2]},'timeout_s':30}]})
        return robot,skill,app

    def test_empty_workspace_has_no_personal_hardware_or_demo_assets(self):
        snapshot=self.app.snapshot()
        self.assertFalse(snapshot['models'])
        self.assertFalse(snapshot['datasets'])
        self.assertFalse(snapshot['services'])
        self.assertFalse(snapshot['robots'])

    def test_complete_delivery_over_real_http_contract(self):
        robot,skill,app=self.workflow()
        static=self.app.harness({'application_id':app['id']})
        self.assertTrue(static['passed'])
        self.assertEqual(static['scope'],'static')
        # A static report is never enough to authorize release.
        with self.assertRaises(ValueError):
            self.app.deploy({'application_id':app['id'],'report_id':static['id']})
        op=self.app.onsite_test({'application_id':app['id'],'hardware_authorized':True})
        self.assertEqual(op['status'],'succeeded')
        release=self.app.deploy({'application_id':app['id'],'report_id':op['report_id']})
        self.assertEqual(release['status'],'deployed')
        started=self.app.operate({'release_id':release['id'],'action':'start','hardware_authorized':True})
        stopped=self.app.operate({'release_id':release['id'],'action':'stop'})
        self.assertEqual(started['status'],'accepted')
        self.assertEqual(stopped['status'],'accepted')
        self.assertIn('evaluate',self.adapter.calls)
        self.assertIn('deploy',self.adapter.calls)
        self.assertIn('start',self.adapter.calls)
        self.assertIn('stop',self.adapter.calls)
        self.assertNotIn('token',self.app.snapshot()['robots'][0])

    def test_unconnected_device_and_missing_capability_block_execution(self):
        robot,skill,app=self.workflow(connect=False)
        self.assertFalse(self.app.harness({'application_id':app['id']})['passed'])
        with self.assertRaises(ValueError):
            self.app.onsite_test({'application_id':app['id'],'hardware_authorized':True})
        self.assertEqual(self.adapter.calls,[])
        self.app.connect_robot({'robot_id':robot['id']})
        extra=self.app.add_skill({'project_id':self.pid,'name':'Fly','version':'1','requires':['flight.takeoff'],'entrypoint':'fly',
                                   'success_condition':'airborne','stop_condition':'abort','model_ids':[]})
        flight=self.app.add_application({'project_id':self.pid,'name':'Flight','version':'1','robot_id':robot['id'],'steps':[{'skill_id':extra['id']}]})
        check=self.app.harness({'application_id':flight['id']})
        self.assertFalse(check['passed'])
        self.assertIn('flight.takeoff',check['cases'][1]['detail'])

    def test_requires_explicit_hardware_authorization(self):
        _,_,app=self.workflow()
        with self.assertRaises(ValueError):
            self.app.onsite_test({'application_id':app['id']})
        self.assertNotIn('evaluate',self.adapter.calls)
        with self.assertRaises(ValueError):
            self.gateway.execute('start',{'request_id':'no-auth','release_id':'x'})

    def test_changed_robot_invalidates_evidence(self):
        _,_,app=self.workflow()
        tested=self.app.onsite_test({'application_id':app['id'],'hardware_authorized':True})
        self.adapter.version='2.0'
        with self.assertRaisesRegex(ValueError,'证据'):
            self.app.deploy({'application_id':app['id'],'report_id':tested['report_id']})
        self.assertNotIn('deploy',self.adapter.calls)

    def test_unknown_operation_is_not_retried(self):
        _,_,app=self.workflow()
        with patch.object(self.adapter,'evaluate',side_effect=RuntimeError('device failure')):
            op=self.app.onsite_test({'application_id':app['id'],'hardware_authorized':True})
        self.assertEqual(op['status'],'unknown')
        self.assertFalse([e for e in self.app.store.list('evaluations') if e['scope']=='onsite'])

    def test_gateway_idempotency_and_payload_binding(self):
        p={'request_id':'request-1','release_id':'r','hardware_authorized':True}
        self.assertEqual(self.gateway.execute('start',p),self.gateway.execute('start',p))
        self.assertEqual(self.adapter.calls.count('start'),1)
        with self.assertRaises(ValueError):
            self.gateway.execute('start',{**p,'release_id':'other'})

    def test_stop_is_not_blocked_by_long_running_adapter_call(self):
        entered,release=threading.Event(),threading.Event()
        def blocked(p):
            entered.set()
            release.wait(3)
            return {'accepted':True}
        with patch.object(self.adapter,'start',side_effect=blocked):
            thread=threading.Thread(target=lambda:self.gateway.execute('start',{'request_id':'long','hardware_authorized':True}))
            thread.start()
            self.assertTrue(entered.wait(1))
            stop=self.gateway.execute('stop',{'request_id':'stop-now'})
            self.assertTrue(stop['accepted'])
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_assets_cannot_cross_projects(self):
        robot,skill,_=self.workflow()
        another=self.app.add_project({'name':'Other','objective':'Another workcell'})
        with self.assertRaises(ValueError):
            self.app.add_application({'project_id':another['id'],'name':'bad','version':'1','robot_id':robot['id'],'steps':[{'skill_id':skill['id']}]})

    def test_agent_produces_actionable_findings_without_hardware_calls(self):
        robot,skill,app=self.workflow(connect=False)
        job=self.app.run_agent({'project_id':self.pid,'agent_id':'testing'})
        self.assertEqual(job['status'],'succeeded')
        self.assertTrue(job['outputs']['findings'])
        self.assertEqual(self.adapter.calls,[])
        self.assertTrue(job['outputs']['report_ids'])

    def test_job_submission_and_status_use_external_executor(self):
        service=self.app.add_service({'project_id':self.pid,'name':'User executor','kind':'training','endpoint':'http://127.0.0.1:9999'})
        with patch('brainos.connector.request',return_value={'schema':'platform.service.v1','kind':'training','version':'1'}):
            self.app.connect_service({'service_id':service['id']})
        job=self.app.add_job({'project_id':self.pid,'name':'User training','kind':'training','service_id':service['id'],
                             'max_minutes':30,'config':{'recipe':'user-defined','dataset_uri':'dataset://v1'}})
        with patch('brainos.connector.request',return_value={'job_id':'remote-123'}) as request:
            self.assertEqual(self.app.submit_job({'job_id':job['id']})['status'],'submitted')
            self.assertEqual(request.call_args.args[1],'/v1/jobs')
        with self.assertRaises(ValueError):
            self.app.submit_job({'job_id':job['id']})
        with patch('brainos.connector.request',return_value={'status':'succeeded','outputs':{'checkpoint':'artifact://weights'},'logs':[]}):
            synced=self.app.sync_job({'job_id':job['id']})
        self.assertEqual(synced['outputs']['checkpoint'],'artifact://weights')

    def test_job_unknown_when_acknowledgement_is_lost(self):
        service=self.app.add_service({'project_id':self.pid,'name':'User executor','kind':'agent','endpoint':'http://127.0.0.1:9999'})
        raw=self.app.store.get('services',service['id']);raw['status']='connected';self.app.store.put('services',raw)
        job=self.app.add_job({'project_id':self.pid,'name':'Engineering','kind':'agent','service_id':service['id'],
                             'max_minutes':30,'config':{'objective':'Implement adapter'}})
        with patch('brainos.connector.request',side_effect=ValueError('timeout')):
            self.assertEqual(self.app.submit_job({'job_id':job['id']})['status'],'unknown')
        with self.assertRaises(ValueError):
            self.app.submit_job({'job_id':job['id']})

    def test_model_invocation_uses_registered_provider(self):
        service=self.app.add_service({'project_id':self.pid,'name':'Inference','kind':'inference','endpoint':'http://127.0.0.1:9999','token':'never-return-this'})
        model=self.app.add_model({'project_id':self.pid,'name':'Perception','service_id':service['id'],'model_ref':'vision@digest',
                                 'task_type':'vision','input_contract':'image','output_contract':'objects'})
        with self.assertRaises(ValueError):
            self.app.infer({'model_id':model['id'],'inputs':{}})
        raw=self.app.store.get('services',service['id']);raw['status']='connected';self.app.store.put('services',raw)
        with patch('brainos.connector.request',return_value={'objects':['detected-object']}) as request:
            response=self.app.infer({'model_id':model['id'],'inputs':{'image_ref':'user://frame'}})
            self.assertEqual(request.call_args.args[3]['model_ref'],'vision@digest')
        self.assertEqual(response['response']['objects'],['detected-object'])
        self.assertNotIn('never-return-this',json.dumps(self.app.snapshot()))


class IdentityHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Application(self.tmp.name)
        self.server=Server(make_handler(self.app))

    def tearDown(self):
        self.server.close();self.app.close();self.tmp.cleanup()

    def request(self,path,payload=None,cookie='',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.http.server_port)
        hs={'Cookie':cookie,**(headers or {})}
        if payload is not None:hs['Content-Type']='application/json'
        conn.request('POST' if payload is not None else 'GET',path,json.dumps(payload) if payload is not None else None,hs)
        r=conn.getresponse();raw=r.read();result=(r.status,json.loads(raw),r.getheader('Set-Cookie'))
        conn.close();return result

    def register(self,email):
        form={'name':'Workspace','email':email,'password':'test-password-123'}
        code=self.request('/api/register/start',form)[1]['dev_code']
        status,user,cookie=self.request('/api/register',{**form,'code':code})
        self.assertEqual(status,200)
        self.assertIn('HttpOnly',cookie)
        return cookie.split(';')[0]

    def test_accounts_and_sessions_isolate_all_assets(self):
        self.assertEqual(self.request('/api/state')[0],401)
        a,b=self.register('a@example.test'),self.register('b@example.test')
        self.assertEqual(self.request('/api/projects',{'name':'Private A','objective':'Private work'},a)[0],200)
        self.assertEqual(len(self.request('/api/state',cookie=a)[1]['projects']),1)
        self.assertEqual(len(self.request('/api/state',cookie=b)[1]['projects']),0)
        aid=self.request('/api/state',cookie=a)[1]['projects'][0]['id']
        self.assertEqual(self.request('/api/robots',{'project_id':aid,'name':'intrusion','family':'mobile'},b)[0],400)
        self.request('/api/logout',{},a)
        self.assertEqual(self.request('/api/state',cookie=a)[0],401)

    def test_login_failure_and_origin_boundaries(self):
        cookie=self.register('a@example.test')
        self.assertEqual(self.request('/api/login',{'email':'a@example.test','password':'wrong-password'})[0],400)
        self.assertEqual(self.request('/api/login',{'email':'a@example.test','password':'test-password-123'})[0],200)
        self.assertEqual(self.request('/api/projects',{'name':'bad','objective':'bad'},cookie,{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/state',cookie=cookie,headers={'Host':'evil.example'})[0],403)


if __name__=='__main__':unittest.main()
