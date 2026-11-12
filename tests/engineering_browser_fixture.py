"""Local-only UI fixtures, using real SSH protocol and the packaged agent runtime."""
import json
import os
import sys
import tempfile
import time
import tokenfans_fixture as tkf
from test_product import Server
from test_engineering import ResponsesFixture
from ssh_fixture import SSHFixture
from mcp_fixture import MCPFixture
from test_extensions import ExtensionModel
from test_workflows import WorkflowModel
from brainos.server import Application,make_handler

class WorkResponses(ResponsesFixture):
    requests=[]
    engineering=True

# ux03: no pinned Codex binary on this machine -> the deterministic app-server stand-in (tests/codex_standin.py).
import agent_binary  # noqa: E402,F401
from brainos import agent_runtime  # noqa: E402
CodexStandInRef=[None]
if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn
    CodexStandInRef[0]=CodexStandIn
    _real_available=agent_runtime.available
    agent_runtime.AgentProcess=CodexStandIn
    agent_runtime.available=lambda:{**_real_available(),'available':True}

if __name__=='__main__':
    model=Server(ResponsesFixture);work=Server(WorkResponses);ssh=SSHFixture();mcp=Server(MCPFixture);ext_model=Server(ExtensionModel);flow_model=Server(WorkflowModel)
    # Issue #4: models run only through TokenFans; one stand-in model id per scripted flow.
    tokenfans=tkf.TokenFansFixture(routes={'fixture-discovery':model.url+'/v1','fixture-work':work.url+'/v1',
                                           'fixture-extension':ext_model.url+'/v1','fixture-workflow':flow_model.url+'/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL']=tokenfans.url
    tmp=tempfile.TemporaryDirectory();app=Application(tmp.name);api=Server(make_handler(app))
    config={'url':api.url,'model':model.url+'/v1','work':work.url+'/v1','ssh_port':ssh.port,'mcp':mcp.url+'/mcp','extension_model':ext_model.url+'/v1','workflow_model':flow_model.url+'/v1',
            'tokenfans':tokenfans.url,'tokenfans_key':tkf.API_KEY,'runtime':'standin' if agent_runtime.AgentProcess is CodexStandInRef[0] else 'codex'}
    with open(sys.argv[1] if len(sys.argv)>1 else '/tmp/neuxmind-engineering-qa.json','w') as f:json.dump(config,f)
    print(json.dumps(config),flush=True)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:api.close();app.close();model.close();work.close();ssh.close();mcp.close();ext_model.close();flow_model.close();tokenfans.close();tmp.cleanup()
