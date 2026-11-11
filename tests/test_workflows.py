import json
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from brainos import agent_runtime
from brainos.service import Platform
from brainos.workflows import Workflows,relative_path
from test_product import Server
from test_engineering import ResponsesFixture


class WorkflowModel(ResponsesFixture):
    requests=[];bad_evidence=False;missing=False;delay=False
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));type(self).requests.append(data)
        if self.delay:time.sleep(1.5)
        text=json.dumps(data,ensure_ascii=False)
        has_result=any(x.get('type')=='function_call_output' for x in data.get('input',[]))
        if not has_result:
            report={'schema':'neuxmind.stage-result.v1','outcome':'ready_for_review','summary':'本地协议测试阶段完成；非真实机器人验收。','artifacts':['result.txt'],
                    'checks':[{'name':'Generated output check','status':'passed','command_id':'made-up' if self.bad_evidence else 'call_stage_work','detail':'Native command must exit successfully'}]}
            command="cat UPSTREAM/stage-1/result.txt > result.txt" if 'PIPELINE_TWO' in text else "printf 'upstream-payload' > result.txt"
            if self.missing:command='true'
            command+='; mkdir -p .pytest_cache; printf cache > .pytest_cache/README.md; printf %s '+shlex.quote(json.dumps(report))+' > HANDOFF.json'
            item={'id':'fc_stage_work','type':'function_call','name':'exec_command','call_id':'call_stage_work','arguments':json.dumps({'cmd':command})}
        else:item={'id':'msg_stage','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'已生成阶段交接文件，请验收。','annotations':[]}]}
        response={'id':'resp_stage','status':'completed','output':[item],'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},{'type':'response.output_item.added','output_index':0,'item':item},{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode();self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers()
        try:self.wfile.write(raw)
        except BrokenPipeError:pass


@unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name);self.store=self.platform.store;self.flow=self.platform.workflows
        WorkflowModel.requests=[];WorkflowModel.bad_evidence=False;WorkflowModel.missing=False;WorkflowModel.delay=False
        self.server=Server(WorkflowModel)
        self.pid=self.platform.add_project({'name':'Workflow','objective':'Coordinate engineering stages'})['id']
        self.model=self.platform.engineering.model_add({'project_id':self.pid,'name':'Workflow fixture','model':'fixture','base_url':self.server.url+'/v1','api_key':'fixture-key'})
    def tearDown(self):self.platform.close();self.server.close();self.tmp.cleanup()
    def definition(self,count=2,budget=None):
        stages=[{'name':'Stage '+str(i+1),'role':'PIPELINE_ONE' if i==0 else 'PIPELINE_TWO','objective':'Create the required result and handoff','model_id':self.model['id'],
                 'required_outputs':['result.txt'],'require_command':True,'max_tokens':1000,'max_minutes':1} for i in range(count)]
        return self.flow.create({'project_id':self.pid,'name':'Fixture workflow','objective':'Verify stage transfer','stages':stages,'max_total_tokens':budget or count*2000})
    def run_flow(self,count=2,budget=None):
        d=self.definition(count,budget);return self.flow.start({'workflow_id':d['id'],'data_authorized':True})
    def wait(self,rid,statuses=('awaiting_review','blocked','interrupted')):
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            run=self.store.get('workflow_runs',rid)
            if run['status'] in statuses:return run
            time.sleep(.05)
        self.fail('Workflow timeout: '+json.dumps(run,ensure_ascii=False))
    def test_real_two_role_handoff_requires_review_and_keeps_snapshot(self):
        run=self.wait(self.run_flow()['id']);self.assertEqual(run['status'],'awaiting_review',run)
        self.assertEqual(len(run['stages'][1]['attempts']),0)
        first=run['stages'][0];session=self.store.get('agent_sessions',first['session_id'])
        self.assertTrue(first['verified_commands'],session)
        self.assertFalse(any('.pytest_cache' in a['path'] for a in first['artifacts']))
        # Completed session edits cannot change the approved immutable handoff.
        original=Path(self.tmp.name)/'engineering'/session['id']/'workspace/result.txt';original.write_text('modified-after-session')
        self.flow.review({'run_id':run['id'],'decision':'approve','note':'Fixture output reviewed'})
        run=self.wait(run['id']);self.assertEqual(run['current_stage'],1);self.assertEqual(run['status'],'awaiting_review',run)
        output=self.flow.artifact({'run_id':run['id'],'stage_index':1,'path':'result.txt'})
        self.assertEqual(output['content'],'upstream-payload')
        run=self.flow.review({'run_id':run['id'],'decision':'approve','note':'Second stage reviewed'})
        self.assertEqual(run['status'],'completed');self.assertEqual(run['allocated_tokens'],2000);self.assertGreater(run['usage_tokens'],0)
        with self.assertRaises(ValueError):self.platform.engineering.resume({'session_id':session['id'],'message':'Bypass flow','data_authorized':True})
    def test_fabricated_command_evidence_blocks_progress(self):
        WorkflowModel.bad_evidence=True;run=self.wait(self.run_flow()['id'])
        self.assertEqual(run['status'],'blocked');self.assertIn('命令证据',run['error']);self.assertFalse(run['stages'][1]['attempts'])
        with self.assertRaises(ValueError):self.flow.review({'run_id':run['id'],'decision':'approve','note':'Cannot bypass evidence'})
    def test_missing_output_blocks_and_retry_consumes_budget(self):
        WorkflowModel.missing=True;run=self.wait(self.run_flow(count=1,budget=2000)['id']);self.assertEqual(run['status'],'blocked',run)
        WorkflowModel.missing=False
        self.flow.retry({'run_id':run['id'],'feedback':'Generate the missing output','data_authorized':True});run=self.wait(run['id'])
        self.assertEqual(run['status'],'awaiting_review',run);self.assertEqual(run['allocated_tokens'],2000);self.assertEqual(len(run['stages'][0]['attempts']),2)
        self.flow.review({'run_id':run['id'],'decision':'reject','note':'Further changes requested'})
        with self.assertRaises(ValueError):self.flow.retry({'run_id':run['id'],'feedback':'No budget','data_authorized':True})
    def test_unsettled_model_costs_block_the_stage(self):
        # Review Q3-16 / mutation B4: every state in which the model cost is not
        # yet known blocks the stage instead of sending it to review.
        from unittest.mock import patch
        engine=self.platform.engineering;original=engine.update
        for metering,needle in (('reconciliation_required','费用等待核对'),('cloud_reconciliation_required','费用等待核对'),
                                ('cloud_close_failed','费用待确认'),('cloud_close_pending','费用待确认')):
            with self.subTest(metering=metering):
                def update(sid,**values):
                    if values.get('status')=='completed':values['metering_status']=metering
                    return original(sid,**values)
                with patch.object(engine,'update',update):run=self.wait(self.run_flow(count=1,budget=2000)['id'])
                self.assertEqual(run['status'],'blocked',run);self.assertIn(needle,run['error'])
    def test_stop_interrupts_stage_without_launching_next(self):
        WorkflowModel.delay=True;run=self.run_flow();deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            run=self.store.get('workflow_runs',run['id'])
            if run['stages'][0].get('session_id'):break
            time.sleep(.05)
        self.flow.stop({'run_id':run['id']});run=self.wait(run['id'],('interrupted',))
        self.assertFalse(run['stages'][1]['attempts'])
    def test_scope_paths_and_total_budget_validated(self):
        for path in ('../escape','/tmp/escape','UPSTREAM/a','PROJECT.json','a/./b'):
            with self.assertRaises(ValueError):relative_path(path)
        with self.assertRaises(ValueError):self.definition(2,1000)
        d=self.definition();d['project_id']=self.platform.add_project({'name':'Other','objective':'Other'})['id']
        with self.assertRaises(ValueError):self.flow.create(d)
    def test_recovery_does_not_replay_model_calls(self):
        run=self.wait(self.run_flow(count=1)['id']);calls=len(WorkflowModel.requests)
        run['status']='running';run['stages'][0]['status']='running';self.store.put('workflow_runs',run)
        recovered=Workflows(self.platform)
        self.assertEqual(self.store.get('workflow_runs',run['id'])['status'],'interrupted');self.assertFalse(recovered.live);self.assertEqual(len(WorkflowModel.requests),calls)
        recovered.close()

    def test_snapshot_tampering_blocks_review_and_artifact_preview(self):
        run=self.wait(self.run_flow(count=1)['id'])
        snapshot=self.flow._snapshot_root(run['id'],0,1)/'result.txt'
        snapshot.write_text('changed outside workflow')
        with self.assertRaises(ValueError):self.flow.review({'run_id':run['id'],'decision':'approve','note':'Must not approve changed bytes'})
        with self.assertRaises(ValueError):self.flow.artifact({'run_id':run['id'],'stage_index':0,'path':'result.txt'})
