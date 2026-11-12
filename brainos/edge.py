"""Device gateway. Requires an operator-installed RobotAdapter; ships with no robot driver."""
import argparse
import hashlib
import importlib
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .contracts import validate_manifest
from .httpd import FastBindHTTPServer
from .store import Store


class Gateway:
    def __init__(self, adapter, token, root):
        if not isinstance(token,str) or len(token)<24:
            raise ValueError('BRAIN_EDGE_TOKEN 至少需要 24 个字符')
        self.adapter,self.token=adapter,token
        validate_manifest(adapter.manifest())
        self.store=Store(root)
        self.lock=threading.Lock()
        for item in self.store.list('requests'):
            if item['status']=='running':
                item['status']='unknown'
                self.store.put('requests',item)

    def execute(self, operation, payload):
        request_id=payload.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:
            raise ValueError('执行请求需要有效 request_id')
        if operation in ('evaluate','start','reset') and payload.get('hardware_authorized') is not True:
            raise ValueError('本次硬件操作尚未明确授权')
        fingerprint=hashlib.sha256(json.dumps({'operation':operation,'payload':payload},sort_keys=True,allow_nan=False).encode()).hexdigest()
        with self.lock:
            existing=next((x for x in self.store.list('requests') if x['id']==request_id),None)
            if existing:
                if existing['fingerprint']!=fingerprint:
                    raise ValueError('request_id 已用于不同请求')
                if existing['status']!='completed':
                    raise ValueError('上次请求状态未知，不会重复执行；请现场核对')
                return existing['response']
            record={'id':request_id,'fingerprint':fingerprint,'operation':operation,'status':'running'}
            self.store.put('requests',record)
        # Do not hold the gateway lock during adapter execution: a stop request
        # must remain dispatchable while an evaluation or start is in progress.
        # The vendor adapter owns physical resource arbitration and stop semantics.
        try:
            response=getattr(self.adapter,operation)(payload)
            if not isinstance(response,dict):
                raise ValueError('适配器必须返回 JSON 对象')
            json.dumps(response,allow_nan=False)
            record.update(status='completed',response=response)
            self.store.put('requests',record)
            return response
        except Exception:
            record['status']='unknown'
            self.store.put('requests',record)
            raise ValueError('设备端执行未确认；不会自动重试，请检查设备状态') from None


def make_handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            pass

        def respond(self,obj,status=200):
            data=json.dumps(obj,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+gateway.token):
                self.respond({'error':'unauthorized'},401)
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            try:
                if self.path=='/v1/manifest':
                    return self.respond(validate_manifest(gateway.adapter.manifest()))
                if self.path=='/v1/status':
                    return self.respond(gateway.adapter.status())
                return self.respond({'error':'not_found'},404)
            except Exception:
                return self.respond({'error':'adapter_unavailable'},503)

        def do_POST(self):
            if not self.authorized():
                return
            try:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ValueError('需要 application/json')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1_000_000:
                    raise ValueError('请求大小无效')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError('请求须为对象')
                operation=self.path.removeprefix('/v1/')
                if operation=='models/install':operation='install_model'
                if operation=='validate':
                    return self.respond(gateway.adapter.validate(payload.get('application',{})))
                if operation not in ('evaluate','deploy','start','stop','install_model','reset'):
                    return self.respond({'error':'not_found'},404)
                if operation=='reset' and not callable(getattr(gateway.adapter,'reset',None)):
                    # Optional adapter method: older adapters have no latch to clear.
                    return self.respond({'error':'reset_not_supported'},404)
                return self.respond(gateway.execute(operation,payload))
            except (ValueError,TypeError,KeyError) as exc:
                return self.respond({'error':str(exc)},400)
    return Handler


def main():
    parser=argparse.ArgumentParser(description='Robot OS device gateway; install your robot adapter first.')
    parser.add_argument('--adapter',required=True,help='Trusted installed Python module:AdapterClass')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8790)
    parser.add_argument('--state-dir',type=Path,default=Path('.edge-state'))
    args=parser.parse_args()
    module,cls=args.adapter.split(':',1)
    adapter=getattr(importlib.import_module(module),cls)()
    gateway=Gateway(adapter,os.environ.get('BRAIN_EDGE_TOKEN',''),args.state_dir)
    server=FastBindHTTPServer((args.host,args.port),make_handler(gateway))
    server.daemon_threads=True
    print(f'Robot OS Edge gateway http://{args.host}:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.store.close()


if __name__=='__main__':
    main()
