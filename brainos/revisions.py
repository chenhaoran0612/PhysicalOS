"""Immutable application revisions, explicit comparison and retry-safe saves."""
from .service import digest, now


def preview(platform, p):
    source = platform.same_project('applications', p.get('source_application_id'), p.get('project_id'))
    manifest = platform.application_manifest(p)
    if manifest['version'] == source['version']:
        raise ValueError('请为修改后的应用填写新的版本号')
    # Reusing a skill ID must not silently replace a source's pinned definition.
    pinned = {s['skill_id']: s['skill_digest'] for s in source['steps']}
    for step in manifest['steps']:
        if step['skill_id'] in pinned and pinned[step['skill_id']] != step['skill_digest']:
            raise ValueError('原技能记录已变化，请登记并明确选择新的技能版本')
    root = source.get('root_application_id', source['id'])
    proof = digest({'source_id': source['id'], 'source_digest': source['digest'], 'manifest': manifest})
    existing = next((a for a in platform.store.list('applications')
                     if a.get('root_application_id', a['id']) == root and a['version'] == manifest['version']), None)
    if existing and existing.get('revision_digest') != proof:
        raise ValueError('此应用已有同名版本，请换一个版本号')
    changes = []
    for key, label in [('name', '应用名称'), ('version', '版本'), ('robot_id', '目标机器人')]:
        if source[key] != manifest[key]:
            before, after = source[key], manifest[key]
            if key == 'robot_id':
                before = platform.store.get('robots', before)['name']
                after = platform.store.get('robots', after)['name']
            changes.append({'label': label, 'before': before, 'after': after})
    def step_summary(step):
        if step is None:
            return None
        skill = platform.store.get('skills', step['skill_id'])
        return {'skill': skill['name'], 'version': skill['version'], 'timeout_s': step['timeout_s'],
                'parameters': step['parameters'], 'skill_digest': step['skill_digest']}
    for i in range(max(len(source['steps']), len(manifest['steps']))):
        before = source['steps'][i] if i < len(source['steps']) else None
        after = manifest['steps'][i] if i < len(manifest['steps']) else None
        if before != after:
            description = '新增步骤' if before is None else '移除步骤' if after is None else '、'.join(
                text for key, text in [('skill_digest', '更换技能'), ('parameters', '参数已调整'), ('timeout_s', '执行时限已调整')]
                if before[key] != after[key])
            changes.append({'label': '工作步骤 ' + str(i + 1), 'description': description,
                            'before': step_summary(before), 'after': step_summary(after)})
    return {'source_id': source['id'], 'source_digest': source['digest'], 'root_application_id': root,
            'manifest': manifest, 'changes': changes, 'preview_digest': proof,
            'existing_id': existing['id'] if existing else None}


def save(platform, p):
    with platform.store.atomic():
        plan = preview(platform, p)
        if p.get('preview_digest') != plan['preview_digest']:
            raise ValueError('配置或依赖已变化，请重新查看变更后保存')
        if plan['existing_id']:
            return platform.store.get('applications', plan['existing_id'])
        obj = {**plan['manifest'], 'id': 'app-revision-' + digest([plan['root_application_id'], plan['manifest']['version']])[:24],
               'parent_application_id': plan['source_id'], 'parent_application_digest': plan['source_digest'],
               'root_application_id': plan['root_application_id'], 'revision_digest': plan['preview_digest'], 'created_at': now()}
        obj['digest'] = digest(obj)
        platform.store.put('applications', obj)
        platform.audit('application.revised', {'application_id': obj['id'], 'parent_application_id': plan['source_id']}, obj['project_id'])
        return obj
