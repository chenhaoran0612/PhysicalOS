import json
import sys
import tempfile
import time
from test_product import Server
from recipe_fixture import RecipeFixture,SimulationFixture,InferenceFixture
from brainos.server import Application,make_handler

if __name__=='__main__':
    tmp=tempfile.TemporaryDirectory();app=Application(tmp.name,operator_email='planner@example.test')
    api=Server(make_handler(app));training=Server(RecipeFixture);simulation=Server(SimulationFixture)
    inference=Server(InferenceFixture)
    config={'url':api.url,'training':training.url,'simulation':simulation.url,'inference':inference.url}
    with open(sys.argv[1] if len(sys.argv)>1 else '/tmp/neuxmind-planner-qa.json','w') as f:json.dump(config,f)
    print(json.dumps(config),flush=True)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:api.close();app.close();training.close();simulation.close();inference.close();tmp.cleanup()
