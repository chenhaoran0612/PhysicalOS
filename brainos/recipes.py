"""Provider-declared job forms. Preview is read-only; legacy configs remain supported."""
import copy
import json
import math
import re
from .contracts import required_text

ASSETS={
    'datasets':('id','name','uri','version','format','source'),
    'models':('id','name','model_ref','version','digest','package_ref','package_digest','runtime'),
    'scenes':('id','name','scene_ref','version','engine','digest'),
    'applications':('id','name','version','digest','robot_id','steps'),
    'robots':('id','name','family','manifest_digest','capabilities'),
}
TYPES=('text','integer','number','choice','boolean','asset')


def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def scalar(field,value):
    kind=field['type'];label=field['label']
    if kind=='text':return required_text(value,label,field.get('max_length',2000))
    if kind in ('integer','number'):
        if not finite(value) or (kind=='integer' and not isinstance(value,int)):raise ValueError(label+'须为有效数值')
        if value<field.get('min',-1e15) or value>field.get('max',1e15):raise ValueError(label+'超出允许范围')
    if kind=='boolean' and not isinstance(value,bool):raise ValueError(label+'须为是或否')
    if kind=='choice' and value not in [o['value'] for o in field['options']]:raise ValueError(label+'选项无效')
    return value


def validate_recipes(manifest):
    recipes=manifest.get('job_recipes',[])
    if not isinstance(recipes,list) or len(recipes)>20:raise ValueError('任务配方清单无效')
    ids=set()
    for recipe in recipes:
        if not isinstance(recipe,dict) or recipe.get('schema')!='platform.job-recipe.v1':raise ValueError('任务配方协议无效')
        rid=required_text(recipe.get('id'),'配方 ID',100)
        if rid in ids:raise ValueError('任务配方 ID 重复')
        ids.add(rid)
        for key,label,limit in [('name','配方名称',100),('version','配方版本',100),('description','配方说明',2000)]:required_text(recipe.get(key),label,limit)
        defaults=recipe.get('config',{})
        if not isinstance(defaults,dict) or len(json.dumps(defaults,allow_nan=False))>64000:raise ValueError('配方基础配置无效')
        outputs=recipe.get('outputs',[])
        if not isinstance(outputs,list) or len(outputs)>10:raise ValueError('配方预期产物无效')
        for output in outputs:required_text(output,'预期产物',300)
        fields=recipe.get('fields',[])
        if not isinstance(fields,list) or len(fields)>20:raise ValueError('配方字段无效')
        keys=set()
        for f in fields:
            if not isinstance(f,dict) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',f.get('key','')):raise ValueError('配方字段名称无效')
            key=f['key']
            if key in ('constructor','prototype'):raise ValueError('配方字段名称为保留字')
            if key in keys or key in defaults:raise ValueError('配方字段重复或覆盖基础配置')
            keys.add(key);required_text(f.get('label'),'字段标题',100)
            if f.get('type') not in TYPES:raise ValueError('配方字段类型不支持')
            for flag in ('required','advanced'):
                if flag in f and not isinstance(f[flag],bool):raise ValueError('配方字段标志无效')
            if f.get('advanced') and f.get('required') and 'default' not in f:raise ValueError('必填高级参数需要默认值')
            if f.get('prefill') not in (None,'project_objective') or (f.get('prefill') and f['type']!='text'):raise ValueError('配方预填规则无效')
            if 'max_length' in f and (type(f['max_length']) is not int or not 1<=f['max_length']<=8000):raise ValueError('文本长度限制无效')
            for bound in ('min','max'):
                if bound in f and not finite(f[bound]):raise ValueError('配方数值范围无效')
            if f.get('min',-1e15)>f.get('max',1e15):raise ValueError('配方数值范围相反')
            if f['type']=='asset':
                if f.get('asset_kind') not in ASSETS or 'default' in f:raise ValueError('配方资产类型或默认值无效')
            elif f['type']=='choice':
                opts=f.get('options');values=[]
                if not isinstance(opts,list) or not 1<=len(opts)<=50:raise ValueError('配方选项无效')
                for option in opts:
                    if not isinstance(option,dict):raise ValueError('配方选项无效')
                    values.append(required_text(option.get('value'),'选项值',100));required_text(option.get('label'),'选项标题',100)
                if len(set(values))!=len(values):raise ValueError('配方选项重复')
            if 'default' in f:scalar(f,f['default'])
    return recipes


def prepare(platform,p,*,verify=False,resource=None):
    from .service import digest
    platform.project(p.get('project_id'))
    if bool(p.get('resource_id'))==bool(p.get('service_id')):raise ValueError('请选择一个平台资源或自有服务')
    if p.get('resource_id'):
        if not platform.commerce:raise ValueError('平台资源尚未开通')
        resource=resource or platform.commerce.store.get('resources',p['resource_id'])
        if resource['status']!='available':raise ValueError('平台资源暂不可用')
        worker=resource
    else:
        worker=platform.same_project('services',p.get('service_id'),p['project_id'])
        if worker.get('managed_resource_id'):raise ValueError('请从平台资源入口使用此服务')
        if worker['status']!='connected':raise ValueError('请先检查服务连接')
    if worker['kind'] not in ('training','simulation','agent','evaluation'):raise ValueError('该服务不支持作业')
    recipe=next((r for r in validate_recipes(worker.get('manifest',{})) if r['id']==p.get('recipe_id')),None)
    if not recipe:raise ValueError('服务未提供此任务配方，请重新选择')
    name=required_text(p.get('name'),'任务名称',100);minutes=p.get('max_minutes')
    if type(minutes) is not int or not 1<=minutes<=43200:raise ValueError('最长运行时间须为 1–43200 分钟')
    values=p.get('values',{})
    if not isinstance(values,dict) or set(values)-{f['key'] for f in recipe.get('fields',[])}:raise ValueError('配方参数不在允许清单中')
    config=copy.deepcopy(recipe.get('config',{}));summary=[];inputs={}
    for f in recipe.get('fields',[]):
        value=values.get(f['key'],f.get('default'))
        if value is None or value=='':
            if f.get('required'):raise ValueError('请填写'+f['label'])
            continue
        inputs[f['key']]=value
        if f['type']=='asset':
            obj=platform.same_project(f['asset_kind'],value,p['project_id'])
            config[f['key']]={key:copy.deepcopy(obj[key]) for key in ASSETS[f['asset_kind']] if key in obj}
            display=obj['name']+(' · '+str(obj['version']) if obj.get('version') else '')
        else:
            config[f['key']]=scalar(f,value)
            display=next(o['label'] for o in f['options'] if o['value']==value) if f['type']=='choice' else ('是' if value else '否') if f['type']=='boolean' else str(value)
        summary.append({'label':f['label'],'value':display})
    if not config or len(json.dumps(config,allow_nan=False))>128000:raise ValueError('生成的任务配置为空或过大')
    cost=None
    if resource:
        units=p.get('max_units')
        if type(units) is not int or not 1<=units<=1000000:raise ValueError('用量上限须为 1–1000000 的整数')
        cost={'max_units':units,'unit':resource['unit'],'price':resource['credits_per_unit'],'reserved_credits':units*resource['credits_per_unit']}
    plan={'name':name,'project_id':p['project_id'],'kind':worker['kind'],'provider_id':worker['id'],'provider_name':worker['name'],
          'provider_fingerprint':digest({'endpoint':worker['endpoint'],'kind':worker['kind']}),
          'recipe':{**copy.deepcopy(recipe),'digest':digest(recipe)},'config':config,'inputs':inputs,'input_summary':summary,'max_minutes':minutes,'cost':cost}
    plan['preview_digest']=digest(plan)
    if verify and p.get('preview_digest')!=plan['preview_digest']:raise ValueError('任务内容、资产或价格已变化，请重新查看确认页')
    return plan


def check_dispatch(worker,job):
    if not job.get('recipe'):return
    from . import connector
    from .service import digest
    if digest({'endpoint':worker['endpoint'],'kind':worker['kind']})!=job.get('provider_fingerprint'):
        raise ValueError('执行服务地址已变化，任务未提交；请重新配置任务')
    manifest=connector.request(worker['endpoint'],'/v1/manifest',worker['token'])
    if manifest.get('schema')!='platform.service.v1' or manifest.get('kind')!=job['kind']:raise ValueError('执行服务协议发生变化，请重新检查连接')
    recipe=next((r for r in validate_recipes(manifest) if r['id']==job['recipe']['id']),None)
    if not recipe or digest(recipe)!=job['recipe']['digest']:raise ValueError('服务配方已变化，任务未提交；请取消旧草稿并重新配置')
