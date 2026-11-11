import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_product as product


class RevisionTests(unittest.TestCase):
    setUp = product.ProductTests.setUp
    tearDown = product.ProductTests.tearDown
    workflow = product.ProductTests.workflow

    def parameters(self, source):
        return {'project_id': self.pid, 'source_application_id': source['id'], 'name': source['name'],
                'version': '1.1', 'robot_id': source['robot_id'],
                'steps': [{'skill_id': source['steps'][0]['skill_id'], 'parameters': {'goal': [3, 4]}, 'timeout_s': 45}]}

    def confirm(self, p):
        return {**p, 'preview_digest': self.app.preview_revision(p)['preview_digest']}

    def test_preview_and_save_preserve_source_and_do_not_inherit_evidence(self):
        _, _, source = self.workflow()
        tested = self.app.onsite_test({'application_id': source['id'], 'hardware_authorized': True})
        release = self.app.deploy({'application_id': source['id'], 'report_id': tested['report_id']})
        before, calls = self.app.snapshot(), list(self.adapter.calls)
        p = self.parameters(source)
        plan = self.app.preview_revision(p)
        self.assertEqual(before, self.app.snapshot())
        self.assertEqual(plan['changes'][-1]['before']['parameters'], {'goal': [1, 2]})
        self.assertEqual(plan['changes'][-1]['after']['timeout_s'], 45)
        revised = self.app.save_revision(self.confirm(p))
        self.assertEqual(self.app.store.get('applications', source['id']), source)
        self.assertEqual(revised['parent_application_digest'], source['digest'])
        self.assertEqual(revised['root_application_id'], source['id'])
        readiness = self.app.delivery_readiness({'project_id': self.pid, 'application_id': revised['id']})
        self.assertIsNone(readiness['onsite_report'])
        self.assertIsNone(readiness['release'])
        self.assertFalse(readiness['can_start'])
        self.assertEqual(self.adapter.calls, calls)
        with self.assertRaises(ValueError):
            self.app.deploy({'application_id': revised['id'], 'report_id': tested['report_id']})
        self.assertEqual(self.app.store.get('deployments', release['id'])['status'], 'deployed')

    def test_concurrent_retries_are_idempotent_and_version_collisions_rejected(self):
        _, _, source = self.workflow()
        p = self.confirm(self.parameters(source))
        with ThreadPoolExecutor(max_workers=4) as pool:
            models = list(pool.map(lambda _: self.app.save_revision(p), range(8)))
        self.assertEqual(len({a['id'] for a in models}), 1)
        self.assertEqual(len(self.app.store.list('applications')), 2)
        collision = {**p, 'name': 'Different configuration'}
        with self.assertRaisesRegex(ValueError, '同名版本'):
            self.app.preview_revision(collision)
        child = self.parameters(models[0]);child['version'] = '2.0'
        next_version = self.app.save_revision(self.confirm(child))
        self.assertEqual(next_version['root_application_id'], source['id'])
        child['version'] = source['version']
        with self.assertRaisesRegex(ValueError, '同名版本'):
            self.app.preview_revision(child)

    def test_changed_confirmation_and_pinned_skill_rejected(self):
        _, skill, source = self.workflow()
        p = self.confirm(self.parameters(source))
        changed = copy.deepcopy(p);changed['steps'][0]['timeout_s'] = 10
        with self.assertRaisesRegex(ValueError, '重新查看'):
            self.app.save_revision(changed)
        skill['digest'] = 'changed-skill'
        self.app.store.put('skills', skill)
        with self.assertRaisesRegex(ValueError, '原技能记录已变化'):
            self.app.preview_revision(p)
        self.assertEqual(len(self.app.store.list('applications')), 1)

    def test_project_scope_version_and_step_validation(self):
        _, _, source = self.workflow()
        p = self.parameters(source)
        for mutation in [{'project_id': 'other'}, {'version': source['version']}, {'steps': []}, {'steps': [{'skill_id': 'missing'}]}]:
            with self.assertRaises(ValueError):
                self.app.preview_revision({**p, **mutation})
        with self.assertRaisesRegex(ValueError, '重新查看'):
            self.app.save_revision(p)

    def test_step_order_addition_and_removal_are_compared(self):
        _, _, source = self.workflow()
        p = self.parameters(source)
        p['steps'].append({'skill_id': source['steps'][0]['skill_id'], 'parameters': {'goal': [9, 9]}, 'timeout_s': 5})
        first = self.app.save_revision(self.confirm(p))
        p = self.parameters(first);p['version'] = '1.2'
        p['steps'] = [{'skill_id': s['skill_id'], 'parameters': s['parameters'], 'timeout_s': s['timeout_s']} for s in reversed(first['steps'])]
        plan = self.app.preview_revision(p)
        self.assertEqual(len([c for c in plan['changes'] if c['label'].startswith('工作步骤')]), 2)
        p['steps'].pop()
        plan = self.app.preview_revision(p)
        self.assertIsNone(plan['changes'][-1]['after'])
