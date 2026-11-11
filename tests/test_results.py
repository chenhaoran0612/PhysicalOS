import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from brainos.service import Platform


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Platform(self.tmp.name)
        self.pid = self.app.add_project({'name': 'Training', 'objective': 'Improve perception'})['id']
        self.job = {'id': 'job-result', 'project_id': self.pid, 'kind': 'training', 'name': 'Trial',
                    'service_id': 'training-service', 'status': 'succeeded', 'manifest_digest': 'job-proof',
                    'outputs': {'checkpoint': 'fixture://weights/v1', 'metrics': {'accuracy': 0.7}},
                    'recipe': {'id': 'train', 'version': '1', 'digest': 'recipe-proof', 'fields': [
                        {'key': 'data', 'type': 'asset', 'asset_kind': 'datasets'}]},
                    'config': {'data': {'id': 'data-v1', 'uri': 'fixture://data', 'version': '1', 'token': 'SECRET'}},
                    'recipe_inputs': {'data': 'data-v1'}, 'input_summary': [{'label': 'Data', 'value': 'Data v1'}]}
        self.app.store.put('jobs', self.job)
        self.p = {'project_id': self.pid, 'job_id': self.job['id'], 'name': 'Candidate', 'task_type': 'Perception'}

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def confirmed(self):
        return {**self.p, 'preview_digest': self.app.preview_training_model(self.p)['preview_digest']}

    def test_save_is_idempotent_and_retains_snapshot_without_granting_execution(self):
        p = self.confirmed()
        self.assertFalse(self.app.store.list('models'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(lambda _: self.app.save_training_model(p), range(8)))
        self.assertEqual(len({x['id'] for x in saved}), 1)
        model = saved[0]
        self.assertEqual(model['provenance']['assets']['data']['version'], '1')
        self.assertNotIn('token', model['provenance']['assets']['data'])
        self.assertEqual(len(self.app.store.list('models')), 1)
        self.assertNotIn('service_id', model)
        self.assertNotIn('package_ref', model)
        self.job['outputs']['metrics']['accuracy'] = 0.9
        self.app.store.put('jobs', self.job)
        self.assertEqual(self.app.store.get('models', model['id'])['provenance']['metrics']['accuracy'], 0.7)
        with patch('brainos.connector.request') as remote:
            with self.assertRaisesRegex(ValueError, '训练产物'):
                self.app.infer({'model_id': model['id'], 'inputs': {}})
            remote.assert_not_called()

    def test_changed_result_requires_confirmation_and_new_checkpoint_gets_new_asset(self):
        p = self.confirmed()
        self.job['outputs']['checkpoint'] = 'fixture://weights/v2'
        self.app.store.put('jobs', self.job)
        with self.assertRaisesRegex(ValueError, '重新查看'):
            self.app.save_training_model(p)
        first = self.app.save_training_model(self.confirmed())
        self.job['outputs']['checkpoint'] = 'fixture://weights/v3'
        self.app.store.put('jobs', self.job)
        second = self.app.save_training_model(self.confirmed())
        self.assertNotEqual(first['id'], second['id'])

    def test_unfinished_simulation_or_invalid_checkpoint_rejected(self):
        for field, value in [('status', 'running'), ('status', 'failed'), ('kind', 'simulation'),
                             ('outputs', {}), ('outputs', {'checkpoint': {'uri': 'fixture://x'}})]:
            candidate = {**self.job, field: value}
            self.app.store.put('jobs', candidate)
            with self.assertRaises(ValueError):
                self.app.save_training_model(self.p)
        self.assertFalse(self.app.store.list('models'))

    def test_cross_project_and_missing_confirmation_rejected(self):
        other = self.app.add_project({'name': 'Other', 'objective': 'Other'})['id']
        with self.assertRaises(ValueError):
            self.app.preview_training_model({**self.p, 'project_id': other})
        with self.assertRaisesRegex(ValueError, '重新查看'):
            self.app.save_training_model(self.p)

    def test_configuring_call_creates_separate_model_with_origin_and_requires_loaded_service(self):
        source = self.app.save_training_model(self.confirmed())
        service = self.app.add_service({'project_id': self.pid, 'name': 'Inference', 'kind': 'inference', 'endpoint': 'http://127.0.0.1:1'})
        p = {'project_id': self.pid, 'name': 'Callable', 'service_id': service['id'], 'model_ref': 'served-version',
             'task_type': 'Perception', 'input_contract': 'image', 'output_contract': 'classes', 'source_model_id': source['id']}
        with self.assertRaisesRegex(ValueError, '确认'):
            self.app.add_model(p)
        p['loaded_confirm'] = True
        with self.assertRaisesRegex(ValueError, '连接'):
            self.app.add_model(p)
        service['status'] = 'connected'
        self.app.store.put('services', service)
        with patch('brainos.connector.request') as remote:
            model = self.app.add_model(p)
            remote.assert_not_called()
        self.assertEqual(model['source_model_digest'], source['digest'])
        self.assertEqual(model['provenance'], source['provenance'])
        self.assertEqual(model['model_ref'], 'served-version')
        self.assertEqual(self.app.store.get('models', source['id']), source)
        other = self.app.add_project({'name': 'Other', 'objective': 'Other'})['id']
        source['project_id'] = other
        self.app.store.put('models', source)
        with self.assertRaises(ValueError):
            self.app.add_model(p)
