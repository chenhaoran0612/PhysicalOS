"""Local protocol fixture only; no training, cloud, robot or paid resource."""
import copy
import json
from http.server import BaseHTTPRequestHandler


def manifest(kind='training'):
    fields=[{'key':'objective','label':'希望达到什么效果','type':'text','required':True,'prefill':'project_objective'}]
    if kind=='training':
        fields += [{'key':'dataset','label':'训练数据','type':'asset','asset_kind':'datasets','required':True},
                   {'key':'epochs','label':'训练轮数','type':'integer','min':1,'max':100,'default':3,'required':True,'advanced':True}]
    else:
        fields += [{'key':'scene','label':'测试场景','type':'asset','asset_kind':'scenes','required':True},
                   {'key':'episodes','label':'运行次数','type':'integer','min':1,'max':100,'default':5,'required':True},
                   {'key':'seed','label':'随机种子','type':'integer','min':0,'max':10000,'default':0,'advanced':True}]
    return {'schema':'platform.service.v1','kind':kind,'version':'1','job_recipes':[{
        'schema':'platform.job-recipe.v1','id':'test-'+kind,'version':'1.0','name':'协议验收方案（非真实'+('训练' if kind=='training' else '仿真')+'）',
        'description':'仅用于验证用户表单、参数快照和服务提交，不执行训练或物理仿真。',
        'config':{'recipe':'fixture-v1'},'outputs':['模型检查点','训练指标'] if kind=='training' else ['仿真报告','运行指标'],'fields':fields}]}


class RecipeFixture(BaseHTTPRequestHandler):
    kind='training';jobs=[];changed=False
    def log_message(self,*args):pass
    def respond(self,data):
        raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/v1/manifest':
            m=manifest(self.kind)
            if self.changed:m['job_recipes'][0]['version']='2.0'
            return self.respond(m)
        outputs={'checkpoint':'fixture://checkpoint','metrics':{'fixture_only':True}} if self.kind=='training' else {'report_ref':'fixture://simulation-report','metrics':{'fixture_only':True}}
        return self.respond({'status':'succeeded','outputs':outputs,'logs':[{'message':'本地协议验收，不执行真实训练或仿真。'}],'usage_units':1,'usage_evidence_ref':'fixture://meter'})
    def do_POST(self):
        p=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path=='/v1/jobs':type(self).jobs.append(copy.deepcopy(p));return self.respond({'job_id':'fixture-job-'+str(len(self.jobs))})
        return self.respond({'accepted':True})


class SimulationFixture(RecipeFixture):kind='simulation';jobs=[]


class InferenceFixture(RecipeFixture):
    def do_GET(self):
        self.respond({'schema':'platform.service.v1','kind':'inference','version':'1'})
    def do_POST(self):
        p=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.respond({'fixture_only':True,'model_ref':p['model_ref'],'result':'fixture-call-ok'})
