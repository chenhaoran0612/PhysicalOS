"""Explicitly save training output references without granting execution capability."""
from .contracts import required_text
from .service import digest, now
from .recipes import ASSETS


def preview(platform, p):
    job = platform.same_project('jobs', p.get('job_id'), p.get('project_id'))
    if job.get('kind') != 'training' or job.get('status') != 'succeeded':
        raise ValueError('只有执行完成的训练任务可以保存模型产物')
    checkpoint = required_text(job.get('outputs', {}).get('checkpoint'), '训练服务返回的检查点地址', 500)
    assets = {}
    for field in job.get('recipe', {}).get('fields', []):
        kind, key = field.get('asset_kind'), field.get('key')
        value = job.get('config', {}).get(key)
        if field.get('type') == 'asset' and kind in ASSETS and isinstance(value, dict):
            assets[key] = {k: v for k, v in value.items() if k in ASSETS[kind]}
    source = {
        'job_id': job['id'], 'job_name': job['name'], 'job_manifest_digest': job.get('manifest_digest'),
        'service_id': job['service_id'], 'checkpoint': checkpoint,
        'recipe': {k: job.get('recipe', {}).get(k) for k in ('id', 'version', 'digest')},
        'inputs': job.get('recipe_inputs', {}), 'input_summary': job.get('input_summary', []), 'assets': assets,
        'metrics': job.get('outputs', {}).get('metrics', {}),
    }
    proof = digest(source)
    model_id = 'model-training-' + digest([job['project_id'], job['id'], checkpoint])[:24]
    existing = next((m for m in platform.store.list('models') if m['id'] == model_id), None)
    return {'project_id': job['project_id'], 'name': job['name'] + ' · 训练产物',
            'checkpoint': checkpoint, 'source': source, 'preview_digest': proof,
            'model_id': model_id, 'saved_model_id': existing['id'] if existing else None}


def save(platform, p):
    with platform.store.atomic():
        plan = preview(platform, p)
        if p.get('preview_digest') != plan['preview_digest']:
            raise ValueError('训练结果已变化，请重新查看后保存')
        if plan['saved_model_id']:
            return platform.store.get('models', plan['saved_model_id'])
        obj = {'id': plan['model_id'], 'project_id': plan['project_id'],
               'name': required_text(p.get('name'), '模型名称', 100),
               'task_type': required_text(p.get('task_type'), '模型用途'),
               'model_ref': plan['checkpoint'], 'status': 'artifact', 'source': 'training',
               'provenance': plan['source'], 'provenance_digest': plan['preview_digest'],
               'created_at': now()}
        obj['digest'] = digest(obj)
        platform.store.put('models', obj)
        platform.audit('training.model_saved', {'job_id': p['job_id'], 'model_id': obj['id']}, obj['project_id'])
        return obj
