import copy
import tempfile
import unittest
from unittest.mock import patch
from brainos.service import Platform
from brainos.commerce import Commerce
from brainos.recipes import validate_recipes
from test_product import Server
from recipe_fixture import RecipeFixture,manifest


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.commerce=Commerce(self.tmp.name+'/commerce');self.app=Platform(self.tmp.name+'/workspace',self.commerce,'customer')
        self.pid=self.app.add_project({'name':'Recipe test','objective':'Verify job planning'})['id'];self.server=Server(RecipeFixture);RecipeFixture.jobs=[];RecipeFixture.changed=False
        self.data=self.app.add_dataset({'project_id':self.pid,'name':'Task data','uri':'fixture://data','format':'custom','version':'1','source':'test only'})
        self.service=self.app.add_service({'project_id':self.pid,'name':'Local recipe fixture','kind':'training','endpoint':self.server.url})
        self.app.connect_service({'service_id':self.service['id']})
        self.p={'project_id':self.pid,'service_id':self.service['id'],'name':'Prepared task','recipe_id':'test-training','values':{'objective':'Improve task','dataset':self.data['id'],'epochs':3},'max_minutes':10}
    def tearDown(self):self.app.close();self.commerce.close();self.server.close();self.tmp.cleanup()
    def confirmed(self,p=None):
        p=copy.deepcopy(p or self.p);p['preview_digest']=self.app.preview_job(p)['preview_digest'];return p
    def resource(self):
        return self.commerce.publish({'kind':'training','name':'Platform fixture','description':'Local test','endpoint':self.server.url,'unit':'minute','credits_per_unit':3})
    def managed(self):
        r=self.resource();self.commerce.grant({'user_id':'customer','credits':100,'reason':'Fixture points'},'test')
        p={k:v for k,v in self.p.items() if k!='service_id'};p.update(resource_id=r['id'],max_units=4);return p,r
    def test_preview_has_no_execution_or_reservation_and_snapshots_assets(self):
        p,r=self.managed();plan=self.app.preview_job(p)
        self.assertEqual(plan['cost']['reserved_credits'],12);self.assertFalse(self.app.store.list('jobs'));self.assertFalse(self.commerce.store.list('reservations'));self.assertFalse(RecipeFixture.jobs)
        job=self.app.add_managed_job(self.confirmed(p),'customer',self.commerce)
        self.assertEqual(job['config']['dataset']['uri'],'fixture://data');self.assertEqual(job['recipe']['version'],'1.0')
        self.app.submit_job({'job_id':job['id']});self.assertEqual(RecipeFixture.jobs[0]['config'],job['config'])
        self.app.sync_job({'job_id':job['id']});self.assertEqual(self.commerce.balance('customer')['available'],97)
    def test_changed_assets_price_or_parameters_require_fresh_preview(self):
        p,r=self.managed();confirmed=self.confirmed(p)
        for change in ('asset','price','input'):
            candidate=self.confirmed(p)
            if change=='asset':self.data['version']='2';self.app.store.put('datasets',self.data)
            if change=='price':
                obj=self.commerce.store.get('resources',r['id']);obj['credits_per_unit']=4;self.commerce.store.put('resources',obj)
            if change=='input':candidate['values']['epochs']=8
            with self.assertRaisesRegex(ValueError,'重新查看'):self.app.add_managed_job(candidate,'customer',self.commerce)
        self.assertFalse(self.commerce.store.list('reservations'));self.assertFalse(self.app.store.list('jobs'))
    def test_types_limits_and_project_scope_checked_server_side(self):
        for value in (0,101,True,1.5,float('nan')):
            p=copy.deepcopy(self.p);p['values']['epochs']=value
            with self.assertRaises(ValueError):self.app.preview_job(p)
        other=self.app.add_project({'name':'Other','objective':'Other'})['id'];self.data['project_id']=other;self.app.store.put('datasets',self.data)
        with self.assertRaises(ValueError):self.app.preview_job(self.p)
        p=copy.deepcopy(self.p);p['values']['unexpected']='hidden override'
        with self.assertRaises(ValueError):self.app.preview_job(p)
    def test_optional_fields_boolean_false_and_choice_validated(self):
        service=self.app.store.get('services',self.service['id']);recipe=service['manifest']['job_recipes'][0]
        recipe['fields'] += [{'key':'enabled','label':'Enable','type':'boolean','required':True},{'key':'mode','label':'Mode','type':'choice','options':[{'value':'fast','label':'Fast'}]}]
        self.app.store.put('services',service);p=copy.deepcopy(self.p);p['values']['enabled']=False
        self.assertIs(self.app.preview_job(p)['config']['enabled'],False)
        p['values']['mode']='unknown'
        with self.assertRaises(ValueError):self.app.preview_job(p)
    def test_recipe_change_before_dispatch_leaves_draft_and_does_not_execute(self):
        p,r=self.managed();job=self.app.add_managed_job(self.confirmed(p),'customer',self.commerce);RecipeFixture.changed=True
        with self.assertRaisesRegex(ValueError,'配方已变化'):self.app.submit_job({'job_id':job['id']})
        self.assertFalse(RecipeFixture.jobs);self.assertEqual(self.app.store.get('jobs',job['id'])['status'],'draft')
        self.app.cancel_job({'job_id':job['id']});self.assertEqual(self.commerce.balance('customer')['available'],100)
    def test_own_recipe_pins_parameters_and_legacy_remains_compatible(self):
        job=self.app.add_job(self.confirmed());self.assertEqual(job['kind'],'training');self.app.submit_job({'job_id':job['id']});self.assertEqual(len(RecipeFixture.jobs),1)
        legacy=self.app.add_job({'project_id':self.pid,'service_id':self.service['id'],'kind':'training','name':'Legacy','config':{'custom':True},'max_minutes':1});self.assertNotIn('recipe',legacy)
        self.app.submit_job({'job_id':legacy['id']});self.assertEqual(len(RecipeFixture.jobs),2)
    def test_service_address_change_requires_new_confirmation(self):
        confirmed=self.confirmed();job=self.app.add_job(confirmed)
        service=self.app.store.get('services',self.service['id']);service['endpoint']='http://127.0.0.1:1';self.app.store.put('services',service)
        with self.assertRaisesRegex(ValueError,'重新查看'):self.app.add_job(confirmed)
        with self.assertRaisesRegex(ValueError,'地址已变化'):self.app.submit_job({'job_id':job['id']})
        self.assertFalse(RecipeFixture.jobs)
    def test_invalid_provider_forms_rejected_and_preview_required(self):
        for mutation in ('duplicate','collision','advanced','asset'):
            m=manifest();r=m['job_recipes'][0]
            if mutation=='duplicate':r['fields'].append(copy.deepcopy(r['fields'][0]))
            if mutation=='collision':r['config']['objective']='hidden'
            if mutation=='advanced':r['fields'][0]['advanced']=True
            if mutation=='asset':r['fields'][1]['asset_kind']='agent_models'
            with self.assertRaises(ValueError):validate_recipes(m)
        with self.assertRaisesRegex(ValueError,'重新查看'):self.app.add_job(self.p)
