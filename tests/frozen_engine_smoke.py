"""Smoke test the actual packaged backend, SSH library and embedded native runtime."""
import http.cookiejar
import os
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
from test_product import Server, DeviceFixture
from brainos.edge import Gateway, make_handler as edge_handler
from test_engineering import ResponsesFixture
from ssh_fixture import SSHFixture
from test_extensions import ExtensionModel
from mcp_fixture import MCPFixture
from test_workflows import WorkflowModel
from recipe_fixture import RecipeFixture
from test_discovery import DiscoveryModel,COMMAND,ROOT,FILE

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    process=subprocess.Popen([str(root/'dist/NeuxMind Robot OS.app/Contents/Resources/backend/brainos-server'),'--port','0','--state-dir',tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={**os.environ,'NEUXMIND_MAIL_DEV_ECHO':'1'})
    ssh=SSHFixture();model=Server(ResponsesFixture);mcp=Server(MCPFixture);ext_model=Server(ExtensionModel);flow_model=Server(WorkflowModel);recipe_service=Server(RecipeFixture)
    deep_model=Server(DiscoveryModel);deep_ssh=SSHFixture(files={FILE:b'# fixture\n'+b'x'*48000+b'\nprotocol: fixture-bus\n'},command_outputs={COMMAND:b'ExecStart=/home/robot/vendor_ws/driver.py\n'})
    gateway=Gateway(DeviceFixture(),'frozen-edge-token-123456789',tmp+'/edge');edge=Server(edge_handler(gateway))
    try:
        base=process.stdout.readline().strip().split()[-1]
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def api(path,p=None):
            req=urllib.request.Request(base+'/api/'+path,data=json.dumps(p).encode() if p is not None else None,headers={'Content-Type':'application/json'})
            return json.load(opener.open(req,timeout=30))
        assert api('health')['version']=='0.14.0'
        with opener.open(base+'/inventory.js',timeout=10) as response:
            assert b'function inventoryOpen(' in response.read()
        form={'name':'Frozen QA','email':'frozen@example.test','password':'test-password-123'}
        api('register',{**form,'code':api('register/start',form)['dev_code']})
        project=api('projects',{'name':'Frozen QA','objective':'Verify packaged discovery'})
        m=api('engineering/models',{'project_id':project['id'],'name':'Local fixture','model':'fixture-agent','base_url':model.url+'/v1','api_key':'fixture-key'})
        check=api('engineering/ssh/scan',{'project_id':project['id'],'host':'127.0.0.1','port':ssh.port,'username':'robot'})
        c=api('engineering/ssh/connect',{'project_id':project['id'],'host_check_id':check['id'],'trust_fingerprint':check['fingerprint'],'auth':'password','password':'fixture-password','name':'Frozen SSH','family':'custom','read_roots':['/opt/ros']})
        run=api('engineering/start',{'robot_id':c['robot_id'],'model_id':m['id'],'data_authorized':True,'max_tokens':1000,'max_minutes':1})
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=api('state');s=next(x for x in state['agent_sessions'] if x['id']==run['id'])
            if s['status'] not in ('starting','running','waiting_input','cancelling'):break
            time.sleep(.2)
        assert s['status']=='completed',s
        assert s['inventory_id'] and ssh.commands
        dm=api('engineering/models',{'project_id':project['id'],'name':'Deep discovery fixture','model':'fixture-agent','base_url':deep_model.url+'/v1','api_key':'fixture-key'})
        check=api('engineering/ssh/scan',{'project_id':project['id'],'host':'127.0.0.1','port':deep_ssh.port,'username':'robot'})
        dc=api('engineering/ssh/connect',{'project_id':project['id'],'host_check_id':check['id'],'trust_fingerprint':check['fingerprint'],'auth':'password','password':'fixture-password','name':'Frozen deep SSH','family':'custom','read_roots':['/opt/ros']})
        task=api('engineering/start',{'robot_id':dc['robot_id'],'model_id':dm['id'],'data_authorized':True,'max_tokens':10000,'max_minutes':1})
        deadline=time.monotonic()+35;approved=[]
        while time.monotonic()<deadline:
            s=next(x for x in api('state')['agent_sessions'] if x['id']==task['id'])
            if s.get('pending_approval'):
                a=s['pending_approval'];assert a['method'] in ('device_exec/requestApproval','device_request_scope/requestApproval')
                approved.append(a['method']);api('engineering/approve',{'session_id':s['id'],'approval_id':a['id'],'decision':'accept'})
            if s['status'] not in ('starting','running','waiting_input','cancelling'):break
            time.sleep(.1)
        assert s['status']=='completed' and s['inventory_id'] and len(approved)==2,s
        assert s['coverage']['checked']==1 and deep_ssh.reads==[FILE,FILE]
        assert 'ripgrep' in api('engineering/artifact',{'session_id':s['id'],'path':'analysis.txt'})['content']
        skill=api('engineering/skills',{'project_id':project['id'],'name':'frozen-review','version':'1.0','description':'Review fixture interfaces','instructions':'Use the selected MCP to inspect the fixture.'})
        service=api('engineering/mcp',{'project_id':project['id'],'name':'Frozen MCP','url':mcp.url+'/mcp','token':'fixture-mcp-key','connection_authorized':True})
        api('engineering/mcp/inspect',{'server_id':service['id']})
        api('engineering/mcp/configure',{'server_id':service['id'],'enabled':True,'enabled_tools':['lookup_robot']})
        em=api('engineering/models',{'project_id':project['id'],'name':'Extension fixture','model':'fixture','base_url':ext_model.url+'/v1','api_key':'fixture-key'})
        task=api('engineering/work',{'project_id':project['id'],'model_id':em['id'],'name':'Frozen extensions','objective':'Inspect fixture interface with selected skill and tool.','data_authorized':True,'max_tokens':1000,'max_minutes':1,'skill_ids':[skill['id']],'mcp_ids':[service['id']]})
        deadline=time.monotonic()+30;approved=False
        while time.monotonic()<deadline:
            s=next(x for x in api('state')['agent_sessions'] if x['id']==task['id'])
            if s.get('pending_approval') and not approved:
                assert not MCPFixture.calls
                api('engineering/approve',{'session_id':s['id'],'approval_id':s['pending_approval']['id'],'decision':'accept'});approved=True
            if s['status'] not in ('starting','running','waiting_input','cancelling'):break
            time.sleep(.2)
        assert s['status']=='completed' and approved and MCPFixture.calls,s
        fm=api('engineering/models',{'project_id':project['id'],'name':'Workflow fixture','model':'fixture','base_url':flow_model.url+'/v1','api_key':'fixture-key'})
        definition=api('engineering/workflows',{'project_id':project['id'],'name':'Frozen workflow','objective':'Verify two role handoff','max_total_tokens':4000,
            'stages':[{'name':'Stage '+str(i+1),'role':role,'objective':'Create result.txt and HANDOFF.json','model_id':fm['id'],
                       'required_outputs':['result.txt'],'require_command':True,'max_tokens':1000,'max_minutes':1} for i,role in enumerate(['PIPELINE_ONE','PIPELINE_TWO'])]})
        run=api('engineering/workflows/start',{'workflow_id':definition['id'],'data_authorized':True})
        for index in range(2):
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                run=next(x for x in api('state')['workflow_runs'] if x['id']==run['id'])
                if run['status']!='running':break
                time.sleep(.2)
            assert run['status']=='awaiting_review' and run['current_stage']==index,run
            artifact=api('engineering/workflows/artifact',{'run_id':run['id'],'stage_index':index,'path':'result.txt'})
            assert artifact['content']=='upstream-payload',artifact
            run=api('engineering/workflows/review',{'run_id':run['id'],'decision':'approve','note':'Frozen snapshot and native command reviewed'})
        assert run['status']=='completed' and run['allocated_tokens']==2000,run
        dataset=api('datasets',{'project_id':project['id'],'name':'Frozen data','uri':'fixture://data','version':'1','format':'custom','source':'Fixture only'})
        service=api('services',{'project_id':project['id'],'name':'Frozen recipes','kind':'training','endpoint':recipe_service.url})
        api('services/connect',{'service_id':service['id']})
        parameters={'project_id':project['id'],'service_id':service['id'],'recipe_id':'test-training','name':'Frozen planned job','values':{'objective':'Verify planning','dataset':dataset['id'],'epochs':2},'max_minutes':1}
        plan=api('jobs/preview',parameters)
        assert not api('state')['jobs'] and not RecipeFixture.jobs
        job=api('jobs',{**parameters,'preview_digest':plan['preview_digest']})
        api('jobs/submit',{'job_id':job['id']})
        result=api('jobs/sync',{'job_id':job['id']})
        assert result['status']=='succeeded' and result['config']['dataset']['uri']=='fixture://data',result
        proof=api('jobs/model-preview',{'project_id':project['id'],'job_id':job['id']})
        saved=api('jobs/save-model',{'project_id':project['id'],'job_id':job['id'],'preview_digest':proof['preview_digest'],'name':'Frozen trained asset','task_type':'Fixture'})
        assert saved['status']=='artifact' and saved['provenance']['assets']['dataset']['version']=='1'
        assert b'resultModelActions' in opener.open(base+'/results.js').read()
        robot=api('robots',{'project_id':project['id'],'name':'Frozen delivery device','family':'mobile','endpoint':edge.url,'token':gateway.token})
        api('robots/connect',{'robot_id':robot['id']})
        skill=api('skills',{'project_id':project['id'],'name':'Fixture navigate','version':'1','entrypoint':'navigate','requires':['navigation.goal'],'success_condition':'at goal','stop_condition':'stop on error','model_ids':[]})
        application=api('applications',{'project_id':project['id'],'name':'Frozen delivery','version':'1','robot_id':robot['id'],'steps':[{'skill_id':skill['id']}]})
        ready=api('applications/readiness',{'project_id':project['id'],'application_id':application['id']})
        assert ready['next_step']=='check' and not gateway.adapter.calls
        tested=api('onsite',{'application_id':application['id'],'hardware_authorized':True})
        release=api('deployments',{'application_id':application['id'],'report_id':tested['report_id']})
        ready=api('applications/readiness',{'project_id':project['id'],'application_id':application['id']})
        assert ready['can_start'] and ready['release']['id']==release['id']
        assert b'deliveryOpen' in opener.open(base+'/delivery.js').read()
        revision={'project_id':project['id'],'source_application_id':application['id'],'name':application['name'],'version':'2','robot_id':robot['id'],'steps':[{'skill_id':skill['id'],'parameters':{'goal':[2,3]},'timeout_s':30}]}
        review=api('applications/revision-preview',revision)
        revised=api('applications/revise',{**revision,'preview_digest':review['preview_digest']})
        ready=api('applications/readiness',{'project_id':project['id'],'application_id':revised['id']})
        assert ready['next_step']=='check' and not ready['onsite_report'] and not ready['release']
        assert revised['parent_application_digest']==application['digest']
        assert b'revisionSubmit' in opener.open(base+'/revisions.js').read()


        assert b'planSubmit' in opener.open(base+'/planner.js').read()
        print(json.dumps({'adaptive_discovery':True,'bundled_search_tool':True,'ssh_scope_approval':True,'immutable_application_revision':True,'application_delivery_checklist':True,'training_result_saved':True,'job_recipe_preview_and_execution':True,'role_workflow':True,'review_gates':True,'upstream_snapshot':True,'skills_and_mcp':True,'call_approval':True,'frozen_backend' :True,'packaged_ssh':True,'embedded_agent':True,'inventory_saved':True,'version':'0.14.0'}))
    finally:
        process.terminate();process.wait(timeout=10);ssh.close();model.close();mcp.close();ext_model.close();flow_model.close();recipe_service.close();edge.close();gateway.store.close();deep_ssh.close();deep_model.close()
