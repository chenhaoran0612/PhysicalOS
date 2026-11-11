import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from brainos.service import Platform
from brainos.extensions import skill_files
from brainos import agent_runtime
from test_product import Server
from test_engineering import ResponsesFixture
from mcp_fixture import MCPFixture


class ExtensionModel(ResponsesFixture):
    requests=[]
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));type(self).requests.append(data)
        has_result=any(x.get('type')=='function_call_output' for x in data.get('input',[]))
        if not has_result:
            server=next(t for t in data['tools'] if t.get('type')=='namespace' and t['name'].startswith('mcp__'))
            item={'id':'fc_mcp','type':'function_call','name':'lookup_robot','namespace':server['name'],'call_id':'call_mcp','arguments':'{"query":"fixture"}'}
        else:item={'id':'msg','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'已读取所选工具的测试结果。','annotations':[]}]}
        response={'id':'resp_fixture','status':'completed','output':[item],'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},{'type':'response.output_item.added','output_index':0,'item':item},{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode();self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


@unittest.skipUnless(agent_runtime.binary_path(),'Packaged runtime required')
class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name);self.store=self.platform.store
        self.pid=self.platform.add_project({'name':'Extensions','objective':'Verify extensions'})['id'];self.ext=self.platform.engineering.extensions
        self.mcp=Server(MCPFixture);self.model=Server(ExtensionModel);MCPFixture.calls=[];MCPFixture.headers_seen=[];ExtensionModel.requests=[]
    def tearDown(self):self.platform.close();self.mcp.close();self.model.close();self.tmp.cleanup()
    def skill(self,**extra):return self.ext.save_skill({'project_id':self.pid,'version':'1.0.0','name':'robot-review','description':'Review robot interfaces','instructions':'SKILL_REVISION_ONE: inspect robot interfaces using the selected MCP tool.',**extra})
    def server(self):
        x=self.ext.add_mcp({'project_id':self.pid,'name':'Fixture MCP','url':self.mcp.url+'/mcp','token':'MCP_SECRET','connection_authorized':True})
        x=self.ext.inspect_mcp({'server_id':x['id']});self.assertEqual(len(x['tools']),2);self.assertFalse(MCPFixture.calls)
        return self.ext.configure_mcp({'server_id':x['id'],'enabled':True,'enabled_tools':['lookup_robot']})
    def start(self,skill,mcp):
        model=self.platform.engineering.model_add({'project_id':self.pid,'name':'Local','model':'fixture','base_url':self.model.url+'/v1','api_key':'MODEL_SECRET'})
        return self.platform.engineering.start_work({'project_id':self.pid,'model_id':model['id'],'name':'Extension job','objective':'Use robot-review and lookup_robot to investigate.','data_authorized':True,'max_tokens':1000,'max_minutes':1,'skill_ids':[skill['id']],'mcp_ids':[mcp['id']]})
    def wait(self,sid,predicate):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            s=self.store.get('agent_sessions',sid)
            if predicate(s):return s
            time.sleep(.05)
        self.fail('Session did not reach expected state: '+json.dumps(s,ensure_ascii=False))
    def test_skill_version_pinning_and_path_validation(self):
        first=self.skill();selection=self.ext.selection({'project_id':self.pid,'skill_ids':[first['id']]})
        second=self.skill(skill_id=first['id'],version='2.0.0',instructions='SKILL_REVISION_TWO')
        frozen=self.ext.runtime_selection({'project_id':self.pid,'extensions':selection})
        self.assertIn('SKILL_REVISION_ONE',frozen['skills'][0]['files']['SKILL.md']);self.assertNotEqual(first['digest'],second['digest'])
        for path in ('../escape','/absolute','.agents/config','scripts/../../escape','a\\b'):
            with self.assertRaises(ValueError):skill_files({'SKILL.md':'a',path:'b'})
        with self.assertRaises(ValueError):self.skill(skill_id=first['id'],version='2.0.0')
        with self.assertRaises(ValueError):self.ext.save_skill({'project_id':self.pid,'version':'1','files':{'SKILL.md':'no frontmatter'}})
    def test_selection_is_project_scoped_and_requires_enabled_tools(self):
        s=self.skill();other=self.platform.add_project({'name':'Other','objective':'Other'})['id']
        with self.assertRaises(ValueError):self.ext.selection({'project_id':other,'skill_ids':[s['id']]})
        self.ext.toggle_skill({'skill_id':s['id'],'enabled':False})
        with self.assertRaises(ValueError):self.ext.selection({'project_id':self.pid,'skill_ids':[s['id']]})
        m=self.server()
        with self.assertRaises(ValueError):self.ext.configure_mcp({'server_id':m['id'],'enabled':True,'enabled_tools':['not-discovered']})
        snapshot=json.dumps(self.platform.snapshot());self.assertNotIn('MCP_SECRET',snapshot)
        self.assertTrue(all(x=='Bearer MCP_SECRET' for x in MCPFixture.headers_seen))
    def test_native_skill_and_mcp_call_wait_for_approval(self):
        skill=self.skill();mcp=self.server();s=self.start(skill,mcp)
        s=self.wait(s['id'],lambda s:bool(s.get('pending_approval')))
        self.assertFalse(MCPFixture.calls);self.assertEqual(s['pending_approval']['method'],'mcpServer/elicitation/request')
        data=json.dumps(ExtensionModel.requests);self.assertIn('SKILL_REVISION_ONE',data);self.assertNotIn('MCP_SECRET',data);self.assertNotIn('MODEL_SECRET',data)
        tools=ExtensionModel.requests[0]['tools'];namespace=next(t for t in tools if t.get('name','').startswith('mcp__'))
        self.assertEqual([t['name'] for t in namespace['tools']],['lookup_robot'])
        self.platform.engineering.approve({'session_id':s['id'],'approval_id':s['pending_approval']['id'],'decision':'accept'})
        s=self.wait(s['id'],lambda s:s['status'] in ('completed','failed'))
        self.assertEqual(s['status'],'completed',s);self.assertEqual(len(MCPFixture.calls),1)
        self.assertTrue(any(e['kind']=='mcpToolCall' for e in s['events']))
        config=(Path(self.tmp.name)/'engineering'/s['id']/'runtime-home/config.toml').read_text();self.assertNotIn('MCP_SECRET',config)
    def test_declining_mcp_call_does_not_execute_it(self):
        s=self.start(self.skill(),self.server());s=self.wait(s['id'],lambda s:bool(s.get('pending_approval')))
        self.platform.engineering.approve({'session_id':s['id'],'approval_id':s['pending_approval']['id'],'decision':'decline'})
        self.wait(s['id'],lambda s:s['status'] in ('completed','failed'));self.assertFalse(MCPFixture.calls)
    def test_disabling_mcp_stops_active_session_and_blocks_resume(self):
        m=self.server();s=self.start(self.skill(),m);s=self.wait(s['id'],lambda s:bool(s.get('pending_approval')))
        self.ext.configure_mcp({'server_id':m['id'],'enabled':False,'enabled_tools':m['enabled_tools']})
        s=self.wait(s['id'],lambda s:s['status']=='interrupted');self.assertFalse(MCPFixture.calls)
        with self.assertRaises(ValueError):self.platform.engineering.resume({'session_id':s['id'],'message':'Continue','data_authorized':True})

    def test_inspection_cannot_undo_concurrent_permission_revocation(self):
        m=self.server();factory=agent_runtime.AgentProcess
        def inspect(*args,**kwargs):
            self.ext.configure_mcp({'server_id':m['id'],'enabled':False,'enabled_tools':[]})
            return factory(*args,**kwargs)
        with patch('brainos.agent_runtime.AgentProcess',inspect):result=self.ext.inspect_mcp({'server_id':m['id']})
        self.assertFalse(result['enabled']);self.assertEqual(result['enabled_tools'],[])
