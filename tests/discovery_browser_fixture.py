import json
import os
import sys
import tempfile
import time
import tokenfans_fixture as tkf
from brainos.server import Application,make_handler
from test_product import Server
from test_discovery import DiscoveryModel,COMMAND,ROOT,FILE
from ssh_fixture import SSHFixture

# ux03: no pinned Codex binary on this machine -> the deterministic app-server stand-in (tests/codex_standin.py).
import agent_binary  # noqa: E402,F401
from brainos import agent_runtime  # noqa: E402
if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn
    _real_available=agent_runtime.available
    agent_runtime.AgentProcess=CodexStandIn
    agent_runtime.available=lambda:{**_real_available(),'available':True}

if __name__=='__main__':
    model=Server(DiscoveryModel)
    # Issue #4: models run only through TokenFans.
    tokenfans=tkf.TokenFansFixture(routes={'fixture-discovery':model.url+'/v1'});os.environ['NEUXMIND_TOKENFANS_URL']=tokenfans.url
    tmp=tempfile.TemporaryDirectory();app=Application(tmp.name);api=Server(make_handler(app))
    ssh=SSHFixture(files={FILE:b'# vendor fixture\n'+b'x'*48000+b'\nprotocol: fixture-bus\n'},command_outputs={COMMAND:b'ExecStart=/home/robot/vendor_ws/driver.py\n'})
    with open(sys.argv[1] if len(sys.argv)>1 else '/tmp/neuxmind-discovery-ui.json','w') as f:json.dump({'url':api.url,'model':model.url+'/v1','ssh_port':ssh.port,'tokenfans':tokenfans.url,'tokenfans_key':tkf.API_KEY},f)
    print(api.url,flush=True)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:api.close();app.close();model.close();ssh.close();tokenfans.close();tmp.cleanup()
