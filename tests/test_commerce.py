"""Budget and model-release contract tests; no billable providers are contacted."""
import json
import tempfile
import unittest
from unittest.mock import patch
from brainos.commerce import Commerce
from brainos.service import Platform

class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.commerce=Commerce(self.tmp.name+'/commerce')
        self.platform=Platform(self.tmp.name+'/workspace',self.commerce,'customer')
        self.project=self.platform.add_project({'name':'Test project','objective':'Contract verification'})['id']
    def tearDown(self):
        self.platform.close();self.commerce.close();self.tmp.cleanup()
    def resource(self,kind='training'):
        with patch('brainos.connector.request',return_value={'schema':'platform.service.v1','kind':kind}):
            return self.commerce.publish({'name':'Test provider','description':'Contract fixture','kind':kind,
                'endpoint':'http://127.0.0.1:9999','token':'private-provider-secret','unit':'minute','credits_per_unit':3})
    def grant(self):
        self.commerce.grant({'user_id':'customer','credits':100,'reason':'Test allocation'},'operator')
    def test_reservations_prevent_overspend_and_settle_once(self):
        resource=self.resource();self.grant()
        first=self.commerce.reserve('customer',resource['id'],20,'job1')
        with self.assertRaises(ValueError):self.commerce.reserve('customer',resource['id'],20,'job2')
        self.commerce.settle({'reservation_id':first['id'],'actual_units':7,'evidence_ref':'test://meter/1'},'provider')
        self.assertEqual(self.commerce.balance('customer'),{'credits':79,'reserved':0,'available':79})
        with self.assertRaises(ValueError):self.commerce.settle({'reservation_id':first['id'],'actual_units':7,'evidence_ref':'test://meter/1'},'provider')
        self.assertEqual(len(self.commerce.entries('customer')),2)
        self.assertNotIn('private-provider-secret',json.dumps(self.commerce.catalog()))
    def test_purchase_never_creates_unpaid_credits(self):
        order=self.commerce.purchase_request('customer',{'credits':100})
        self.assertEqual(order['status'],'awaiting_payment_channel')
        self.assertEqual(self.commerce.balance('customer')['available'],0)
        self.assertEqual(self.commerce.account('another')['orders'],[])
    def test_managed_job_reserves_and_reconciles_provider_usage(self):
        resource=self.resource();self.grant()
        p={'project_id':self.project,'resource_id':resource['id'],'name':'Training contract','config':{'recipe':'fixture'},'max_minutes':10,'max_units':10}
        job=self.platform.add_managed_job(p,'customer',self.commerce)
        self.assertEqual(self.commerce.balance('customer')['reserved'],30)
        with patch('brainos.connector.request',return_value={'job_id':'provider-job'}) as call:
            self.platform.submit_job({'job_id':job['id']})
            self.assertEqual(call.call_args.args[3]['max_units'],10)
        with patch('brainos.connector.request',return_value={'status':'succeeded','usage_units':4,'usage_evidence_ref':'test://meter/2'}):
            self.platform.sync_job({'job_id':job['id']});self.platform.sync_job({'job_id':job['id']})
        self.assertEqual(self.commerce.balance('customer')['available'],88)
        self.assertNotIn('private-provider-secret',json.dumps(self.platform.snapshot()))
    def test_draft_cancellation_releases_budget(self):
        resource=self.resource();self.grant()
        job=self.platform.add_managed_job({'project_id':self.project,'resource_id':resource['id'],'name':'Simulation','config':{'recipe':'fixture'},'max_minutes':10,'max_units':10},'customer',self.commerce)
        self.platform.cancel_job({'job_id':job['id']})
        self.assertEqual(self.commerce.balance('customer')['available'],100)
    def test_edge_model_install_checks_runtime_and_exact_digest(self):
        catalog=self.commerce.publish_model({'name':'Test model','source':'native','version':'1','model_ref':'test@1','task_type':'perception','input_contract':'image','output_contract':'objects','license':'test only','package_ref':'test://package','package_digest':'a'*64,'runtime':'test-runtime','families':['mobile']})
        model=self.platform.use_catalog_model({'project_id':self.project,'catalog_model_id':catalog['id']})
        robot=self.platform.add_robot({'project_id':self.project,'name':'Test robot','family':'mobile','endpoint':'http://127.0.0.1:9999'})
        manifest={'schema':'robot.adapter.v1','instance_id':'fixture','name':'Test robot','family':'mobile','adapter_version':'1','capabilities':['model.install'],'model_runtimes':['wrong-runtime']}
        with patch('brainos.connector.request',return_value=manifest):
            with self.assertRaises(ValueError):self.platform.deploy_model({'model_id':model['id'],'robot_id':robot['id']})
        manifest['model_runtimes']=['test-runtime']
        def request(endpoint,path,*args):
            if path=='/v1/manifest':return manifest
            if path=='/v1/status':return {'instance_id':'fixture'}
            return {'accepted':True,'package_digest':'b'*64}
        with patch('brainos.connector.request',side_effect=request):
            release=self.platform.deploy_model({'model_id':model['id'],'robot_id':robot['id']})
        self.assertEqual(release['status'],'unknown')
        request_good=lambda endpoint,path,*args: {'accepted':True,'package_digest':'a'*64} if path=='/v1/models/install' else request(endpoint,path,*args)
        with patch('brainos.connector.request',side_effect=request_good):
            self.assertEqual(self.platform.deploy_model({'model_id':model['id'],'robot_id':robot['id']})['status'],'deployed')

if __name__=='__main__':unittest.main()
