"""Browser acceptance fixture for round 04 (§6.2 本体适配).

Serves the desktop app with an isolated inventory (test data, no robot is
contacted), a local Responses double acting as the engineering assistant
(driven through the real Codex 0.146.1 app-server when
``NEUXMIND_TEST_AGENT_BINARY`` is set, otherwise through the deterministic
stand-in tests/codex_standin.py) and the real runtime
``python -m neuxmind_runtime scaffold|conformance`` (ROS 2 parts on the
runtime's rclpy test double). ``NEUXMIND_TEST_RUNTIME_DOUBLE=1`` switches to
``tests/runtime_double.py`` where the runtime cannot run.

Usage: python3 tests/adaptation_browser_fixture.py CONFIG.json
Writes {url,email,password,robot_id} to CONFIG.json; runs until SIGTERM.
"""
import json
import os
import signal
import sys
import tempfile
import time

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import agent_binary  # noqa: F401,E402
from brainos import adaptation, agent_runtime  # noqa: E402
from codex_standin import CodexStandIn  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from adaptation_support import DOUBLE, AdaptationModel, mapping_for, seed_inventory  # noqa: E402
from test_product import Server  # noqa: E402

if __name__=='__main__':
    config=sys.argv[1]
    if os.environ.get('NEUXMIND_TEST_RUNTIME_DOUBLE')=='1':adaptation.RUNTIME_TOOL=str(DOUBLE)
    # ux03: without the pinned Codex binary the engine refused to start ("工程引擎未安装") and the
    # test waited for a mapping table that could never appear. Use the deterministic stand-in instead.
    if not agent_runtime.binary_path():
        real_available=agent_runtime.available
        agent_runtime.AgentProcess=CodexStandIn
        agent_runtime.available=lambda:{**real_available(),'available':True}
    tmp=tempfile.TemporaryDirectory(prefix='neuxmind-adapt-qa-')
    app=Application(tmp.name);model=Server(AdaptationModel)
    email,password='adapt@example.test','adaptation-test-only'
    _,user=app.identity.authenticate({'email':email,'password':password,'name':'本体适配验收'},register=True)
    p=app.workspace(user)
    pid=p.add_project({'name':'本体适配验收','objective':'从档案生成适配器初稿并做一致性测试。全部为测试资料。'})['id']
    rid,report,ids=seed_inventory(p,pid)
    AdaptationModel.mapping=mapping_for(report,ids,adaptation.registry())
    p.engineering.model_add({'project_id':pid,'name':'适配模型（本地协议测试）','model':'fixture','base_url':model.url+'/v1','api_key':'fixture-adapt-key'})
    server=Server(make_handler(app))
    with open(config,'w') as f:json.dump({'url':server.url,'email':email,'password':password,'robot_id':rid,'project_id':pid,'pid':os.getpid()},f)
    stop=[]
    signal.signal(signal.SIGTERM,lambda *_:stop.append(1))
    try:
        while not stop:time.sleep(.2)
    finally:
        server.close();app.close();model.close();tmp.cleanup()
