"""Local HTTP MCP fixture. Never executes robot or filesystem operations."""
import json
from http.server import BaseHTTPRequestHandler

class MCPFixture(BaseHTTPRequestHandler):
    calls=[]
    headers_seen=[]
    def log_message(self,*args):pass
    def do_GET(self):self.send_response(405);self.end_headers()
    def do_DELETE(self):self.send_response(204);self.end_headers()
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).headers_seen.append(self.headers.get('Authorization'))
        method=data.get('method')
        if 'id' not in data:self.send_response(202);self.end_headers();return
        result={}
        if method=='initialize':result={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'Local test tools','version':'1.0'}}
        elif method=='tools/list':result={'tools':[{'name':'lookup_robot','description':'Read a fixture robot specification','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']},'annotations':{'readOnlyHint':True}}, {'name':'write_robot','description':'Fixture mutation, must not be enabled','inputSchema':{'type':'object','properties':{}},'annotations':{'destructiveHint':True}}]}
        elif method=='tools/call':
            type(self).calls.append(data['params']);result={'content':[{'type':'text','text':'FIXTURE: robot navigation interface v1'}],'isError':False}
        elif method in ('resources/list','resources/templates/list'):result={'resources':[]} if method=='resources/list' else {'resourceTemplates':[]}
        raw=json.dumps({'jsonrpc':'2.0','id':data['id'],'result':result}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
