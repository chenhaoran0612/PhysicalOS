import unittest
from unittest.mock import patch
import test_product as product


class DeliveryTests(unittest.TestCase):
    setUp = product.ProductTests.setUp
    tearDown = product.ProductTests.tearDown
    workflow = product.ProductTests.workflow

    def read(self, app):
        return self.app.delivery_readiness({'application_id': app['id'], 'project_id': self.pid})

    def test_readiness_is_read_only_scoped_and_follows_actual_evidence(self):
        robot, _, app = self.workflow()
        before = self.app.snapshot()
        for _ in range(2):
            d = self.read(app)
            self.assertEqual(d['next_step'], 'check')
            self.assertFalse(d['can_publish'])
            self.assertNotIn('token', d['robot'])
        self.assertEqual(before, self.app.snapshot())
        self.assertEqual(self.adapter.calls, [])
        with self.assertRaises(ValueError):
            self.app.delivery_readiness({'application_id': app['id'], 'project_id': 'another-project'})
        self.app.harness({'application_id': app['id']})
        self.assertEqual(self.read(app)['next_step'], 'onsite')
        op = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertEqual(self.read(app)['next_step'], 'publish')
        self.app.deploy({'application_id': app['id'], 'report_id': op['report_id']})
        self.assertEqual(self.read(app)['next_step'], 'start')
        robot = self.app.store.get('robots', robot['id'])
        robot['manifest_digest'] = 'changed'
        self.app.store.put('robots', robot)
        self.assertFalse(self.read(app)['can_start'])
        self.assertIsNone(self.read(app)['onsite_report'])

    def test_new_failure_invalidates_old_pass_for_publish_and_start_but_stop_remains(self):
        _, _, app = self.workflow()
        passed = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        release = self.app.deploy({'application_id': app['id'], 'report_id': passed['report_id']})
        evaluate = self.adapter.evaluate
        with patch.object(self.adapter, 'evaluate', side_effect=lambda p: {**evaluate(p), 'passed': False}):
            failed = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        d = self.read(app)
        self.assertFalse(d['can_start'])
        self.assertFalse(d['can_publish'])
        self.assertTrue(d['can_stop'])
        self.assertEqual(d['onsite_report']['id'], failed['report_id'])
        for callback in [lambda: self.app.deploy({'application_id': app['id'], 'report_id': passed['report_id']}),
                         lambda: self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})]:
            with self.assertRaisesRegex(ValueError, '旧报告'):
                callback()
        self.assertNotIn('start', self.adapter.calls)
        self.assertEqual(self.adapter.calls.count('deploy'), 1)
        self.assertEqual(self.app.operate({'release_id': release['id'], 'action': 'stop'})['status'], 'accepted')

    def test_unknown_new_test_blocks_old_pass_and_new_pass_requires_new_release(self):
        _, _, app = self.workflow()
        passed = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        release = self.app.deploy({'application_id': app['id'], 'report_id': passed['report_id']})
        with patch.object(self.adapter, 'evaluate', side_effect=RuntimeError('fixture timeout')):
            self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertEqual(self.read(app)['next_step'], 'reconcile')
        with self.assertRaisesRegex(ValueError, '尚待核对'):
            self.app.deploy({'application_id': app['id'], 'report_id': passed['report_id']})
        with self.assertRaisesRegex(ValueError, '尚待核对'):
            self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        new = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertTrue(self.read(app)['can_publish'])
        self.assertFalse(self.read(app)['can_start'])
        with self.assertRaises(ValueError):
            self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.app.deploy({'application_id': app['id'], 'report_id': new['report_id']})
        self.assertTrue(self.read(app)['can_start'])

    def test_current_missing_capability_prevents_start_even_with_matching_saved_evidence(self):
        robot, _, app = self.workflow()
        passed = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        release = self.app.deploy({'application_id': app['id'], 'report_id': passed['report_id']})
        robot = self.app.store.get('robots', robot['id'])
        robot['capabilities'] = []
        self.app.store.put('robots', robot)
        self.assertEqual(self.read(app)['next_step'], 'prepare')
        with patch.object(self.app, 'connect_robot'):
            with self.assertRaisesRegex(ValueError, '依赖检查'):
                self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.assertNotIn('start', self.adapter.calls)
