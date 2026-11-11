"""Test-only device/provider endpoints. Never imported or started by product code."""
import json
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from test_product import DeviceFixture, Server
from brainos.edge import Gateway, make_handler


class Provider(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,value):
        body=json.dumps(value).encode();self.send_response(200)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        kind=self.path.strip('/').split('/')[0]
        if self.path.endswith('/manifest'):
            self.reply({'schema':'platform.service.v1','kind':kind,'version':'test-only'})
        else:self.reply({'status':'succeeded','outputs':{'checkpoint':'test-fixture://checkpoint'},'logs':[{'tool':'fixture','message':'Protocol test only'}]})
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        if self.path.endswith('/infer'):self.reply({'objects':[{'label':'test-fixture-object','confidence':1.0}]})
        elif self.path.endswith('/cancel'):self.reply({'accepted':True})
        else:self.reply({'job_id':'fixture-job-001'})


if __name__=='__main__':
    tmp=tempfile.TemporaryDirectory()
    gateway=Gateway(DeviceFixture(),'browser-fixture-token-12345678',tmp.name)
    device,provider=Server(make_handler(gateway)),Server(Provider)
    print(json.dumps({'device':device.url,'provider':provider.url,'token':'browser-fixture-token-12345678'}),flush=True)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:device.close();provider.close();gateway.store.close();tmp.cleanup()
