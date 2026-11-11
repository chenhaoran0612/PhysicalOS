"""Project-owned, versioned engineering skills and explicitly configured HTTP MCP."""
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import urlparse
from . import agent_runtime
from .contracts import required_text
from .service import now,uid,digest

PROBE_MODEL={'model':'connection-inspection','base_url':'http://127.0.0.1:1/v1','api_key':'not-used'}


def skill_files(value):
    if not isinstance(value,dict) or not 1<=len(value)<=32 or 'SKILL.md' not in value:raise ValueError('Skill 包须包含 SKILL.md，最多 32 个文本文件')
    size=0
    for name,content in value.items():
        if not isinstance(name,str) or not isinstance(content,str):raise ValueError('Skill 文件名与内容须为文本')
        path=PurePosixPath(name)
        if not name or len(name)>200 or path.is_absolute() or any(x in ('..','.') or x.startswith('.') for x in name.split('/')) or '\\' in name or '\x00' in name or str(path)!=name:raise ValueError('Skill 包路径必须位于包内，不能包含隐藏目录或相对跳转')
        size+=len(content.encode())
        if '\x00' in content or size>512000:raise ValueError('Skill 包只支持总计不超过 500 KB 的文本文件')
    return dict(value)


class Extensions:
    def __init__(self,platform):self.platform=platform;self.store=platform.store

    def snapshot(self):
        return {k:self.store.list(k) for k in ('agent_skills','agent_mcp_servers')}

    def save_skill(self,p):
        self.platform.project(p.get('project_id'))
        old=self.platform.same_project('agent_skills',p['skill_id'],p['project_id']) if p.get('skill_id') else None
        version=required_text(p.get('version'),'Skill 版本',60)
        files=p.get('files')
        if files is None:
            slug=required_text(p.get('name'),'Skill 名称',64)
            description=required_text(p.get('description'),'使用场景',1024)
            instructions=required_text(p.get('instructions'),'工程指令',50000)
            files={'SKILL.md':'---\nname: '+json.dumps(slug,ensure_ascii=False)+'\ndescription: '+json.dumps(description,ensure_ascii=False)+'\n---\n\n'+instructions+'\n'}
        files=skill_files(files)
        with tempfile.TemporaryDirectory(prefix='skill-check-',dir=self.store.root) as tmp:
            root=Path(tmp)
            process=agent_runtime.AgentProcess(root/'runtime',root/'workspace',PROBE_MODEL,engineering=True,
                extensions={'skills':[{'slug':'package','files':files}],'mcp':[]})
            try:metadata=process.loaded_skills[0]
            finally:process.close()
        slug=metadata['name']
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',slug) or len(slug)>64:raise ValueError('Skill 名称须为小写英文、数字或连字符，最长 64 个字符')
        description=required_text(metadata['description'],'使用场景',1024)
        if old and old['name']!=slug:raise ValueError('新版本须保留原 Skill 名称')
        with self.store.atomic():
            if old:old=self.store.get('agent_skills',old['id'])
            if any(x['project_id']==p['project_id'] and x['name']==slug and x['id']!=(old or {}).get('id') for x in self.store.list('agent_skills')):raise ValueError('项目已存在同名 Skill，请在原 Skill 发布新版本')
            sid=old['id'] if old else uid('skill')
            if any(v['skill_id']==sid and v['version']==version for v in self.store.list('agent_skill_versions')):raise ValueError('该版本已存在，请使用新版本号')
            revision={'id':uid('skill-revision'),'skill_id':sid,'project_id':p['project_id'],'version':version,'files':files,'digest':digest(files),'created_at':now()}
            self.store.put('agent_skill_versions',revision)
            obj={'id':sid,'project_id':p['project_id'],'name':slug,'description':description,'version':version,'revision_id':revision['id'],'digest':revision['digest'],
                 'file_count':len(files),'enabled':old['enabled'] if old else True,'created_at':old['created_at'] if old else now(),'updated_at':now()}
            self.store.put('agent_skills',obj)
        return obj

    def skill_detail(self,p):
        skill=self.store.get('agent_skills',p.get('skill_id'))
        versions=[v for v in self.store.list('agent_skill_versions') if v['skill_id']==skill['id']]
        return {'skill':skill,'versions':versions}

    def toggle_skill(self,p):
        obj=self.store.get('agent_skills',p.get('skill_id'))
        if not isinstance(p.get('enabled'),bool):raise ValueError('启用状态无效')
        obj['enabled']=p['enabled'];self.store.put('agent_skills',obj)
        if not obj['enabled']:self._revoke('skills',obj['id'])
        return obj

    def add_mcp(self,p):
        from .engineering import model_url
        self.platform.project(p.get('project_id'))
        if p.get('connection_authorized') is not True:raise ValueError('请确认连接此 MCP 服务')
        url=model_url(p.get('url'));parsed=urlparse(url)
        if parsed.query or parsed.fragment:raise ValueError('MCP 地址不能包含查询令牌或片段，请使用独立认证字段')
        token=p.get('token','')
        if not isinstance(token,str) or len(token)>4096 or '\n' in token or '\r' in token:raise ValueError('Bearer Token 格式无效')
        sid=uid('mcp')
        obj={'id':sid,'project_id':p['project_id'],'name':required_text(p.get('name'),'服务名称',120),'url':url,'runtime_name':sid.replace('-','_'),
             'enabled':False,'enabled_tools':[],'tools':{},'resources':[],'status':'unconnected','has_token':bool(token),'created_at':now()}
        self.store.put('agent_secrets',{'id':sid,'token':token});self.store.put('agent_mcp_servers',obj)
        return obj

    def inspect_mcp(self,p):
        obj=self.store.get('agent_mcp_servers',p.get('server_id'));secret=self.store.get('agent_secrets',obj['id'])
        try:
            with tempfile.TemporaryDirectory(prefix='mcp-check-',dir=self.store.root) as tmp:
                root=Path(tmp)
                process=agent_runtime.AgentProcess(root/'runtime',root/'workspace',PROBE_MODEL,engineering=True,extensions={'skills':[],'mcp':[{**obj,**secret,'enabled_tools':None}]})
                try:
                    data=process.request('mcpServerStatus/list',{},timeout=20)['data']
                    report=next((x for x in data if x['name']==obj['runtime_name']),None)
                    if not report or not report.get('serverInfo'):raise ValueError('MCP 服务未完成握手，请检查地址、协议与认证')
                    if len(report['tools'])>200:raise ValueError('MCP 工具过多，请在服务端拆分为不超过 200 个工具')
                    if len(json.dumps(report))>500000:raise ValueError('MCP 工具清单超过允许大小')
                finally:process.close()
            # Keep enablement changes made while the network check was in flight.
            with self.store.atomic():
                obj=self.store.get('agent_mcp_servers',obj['id'])
                obj.update(status='connected',tools=report['tools'],resources=report['resources'],resource_templates=report['resourceTemplates'],server_info=report.get('serverInfo'),
                           auth_status=report['authStatus'],inspected_at=now(),last_error=None)
                obj['enabled_tools']=[x for x in obj['enabled_tools'] if x in obj['tools']]
                if not obj['enabled_tools']:obj['enabled']=False
                self.store.put('agent_mcp_servers',obj)
        except Exception:
            with self.store.atomic():
                obj=self.store.get('agent_mcp_servers',obj['id'])
                obj.update(status='error',last_error='MCP 连接检查失败，请核对服务可用性、地址与 Bearer Token。')
                self.store.put('agent_mcp_servers',obj)
            raise ValueError(obj['last_error']) from None
        self._revoke('mcp',obj['id'],set(obj['enabled_tools']) if obj['enabled'] else set())
        return obj

    def configure_mcp(self,p):
        obj=self.store.get('agent_mcp_servers',p.get('server_id'));names=p.get('enabled_tools',[]);enabled=p.get('enabled')
        if not isinstance(enabled,bool) or not isinstance(names,list) or len(names)>200 or any(not isinstance(x,str) or x not in obj['tools'] for x in names):raise ValueError('请选择已发现的 MCP 工具')
        if enabled and (obj['status']!='connected' or not names):raise ValueError('启用前须成功检查连接并选择至少一个工具')
        obj.update(enabled=enabled,enabled_tools=sorted(set(names)));self.store.put('agent_mcp_servers',obj)
        self._revoke('mcp',obj['id'],set(names) if enabled else set())
        return obj

    def _revoke(self,kind,key,allowed=None):
        engine=self.platform.engineering
        for s in self.store.list('agent_sessions'):
            entries=s.get('extensions',{}).get(kind,[])
            if any(x['id']==key and (allowed is None or not set(x.get('enabled_tools',[]))<=allowed) for x in entries):
                with engine.lock:
                    if s['id'] in engine.live:
                        engine.event(s['id'],'extensions','扩展权限已收回，正在停止会话；已经执行的外部操作不会自动撤销')
                        engine.stop({'session_id':s['id']})

    def selection(self,p):
        result={'skills':[],'mcp':[]}
        for field,kind,maximum in [('skill_ids','agent_skills',16),('mcp_ids','agent_mcp_servers',8)]:
            ids=p.get(field,[])
            if not isinstance(ids,list) or len(ids)>maximum or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise ValueError('工程扩展选择无效或过多')
            for key in ids:
                item=self.platform.same_project(kind,key,p['project_id'])
                if not item['enabled']:raise ValueError('所选扩展已停用')
                if kind=='agent_skills':
                    result['skills'].append({k:item[k] for k in ('id','name','description','version','revision_id','digest')})
                else:
                    if item['status']!='connected' or not item['enabled_tools']:raise ValueError('MCP 服务需先检查连接并选择工具')
                    selected={name:item['tools'][name] for name in item['enabled_tools']}
                    result['mcp'].append({**{k:item[k] for k in ('id','name','url','runtime_name','enabled_tools')},'tool_digest':digest(selected)})
        return result

    def runtime_selection(self,s):
        chosen=s.get('extensions',{'skills':[],'mcp':[]});result={'skills':[],'mcp':[]}
        for item in chosen['skills']:
            current=self.platform.same_project('agent_skills',item['id'],s['project_id'])
            if not current['enabled']:raise ValueError('会话所用 Skill 已停用；请重新启用或新建任务')
            version=self.platform.same_project('agent_skill_versions',item['revision_id'],s['project_id'])
            if version['digest']!=item['digest']:raise ValueError('Skill 版本摘要不匹配')
            result['skills'].append({'slug':item['name'],'files':version['files']})
        for item in chosen['mcp']:
            current=self.platform.same_project('agent_mcp_servers',item['id'],s['project_id'])
            if not current['enabled'] or not set(item['enabled_tools'])<=set(current['enabled_tools']):raise ValueError('会话 MCP 权限已收回；请调整工具选择后新建任务')
            result['mcp'].append({**item,**self.store.get('agent_secrets',item['id'])})
        return result

    @staticmethod
    def verify_loaded(process,s):
        expected=s.get('extensions',{}).get('mcp',[])
        if not expected:return
        data=process.request('mcpServerStatus/list',{},timeout=20)['data']
        for item in expected:
            report=next((x for x in data if x['name']==item['runtime_name']),None)
            if not report or not report.get('serverInfo') or any(name not in report['tools'] for name in item['enabled_tools']):raise ValueError('MCP 工具不可用，请重新检查连接')
            if digest({n:report['tools'][n] for n in item['enabled_tools']})!=item['tool_digest']:raise ValueError('MCP 工具定义已改变，请重新检查并新建任务')
