"""Issue #48: a robot has a brand and model; applications and skills belong to one brand and model.

Rules under test (brainos/robot_model.py): the brand and model come from the device's manifest, or
are typed when the robot is added; an application can be used with every robot of its brand and
model and with no other; a skill is kept to one brand and model.

The devices are the HTTP contract fixture of tests/test_product.py; no robot is contacted.
"""
import tempfile
import unittest

from brainos import robot_model
from brainos.edge import Gateway, make_handler as edge_handler
from brainos.errors import CodedError
from brainos.service import Platform
from test_product import DeviceFixture, Server

ACCESS = 'fixture-access-' + 'x' * 16


class Reporting(DeviceFixture):
    """A device whose manifest names a brand and model (or the adapters' "unknown")."""

    def __init__(self, vendor=None, model=None, instance='test-device'):
        super().__init__()
        self.vendor, self.model, self.instance = vendor, model, instance

    def manifest(self):
        manifest = {**super().manifest(), 'instance_id': self.instance}
        if self.vendor is not None:
            manifest.update(vendor=self.vendor, model=self.model)
        return manifest

    def status(self):
        return {'instance_id': self.instance, 'mode': 'idle'}

    def evaluate(self, p):
        from brainos.service import digest
        return {**super().evaluate(p), 'robot_manifest_digest': digest(self.manifest())}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/platform'
        self.app = Platform(self.root)
        self.addCleanup(lambda: self.app.close())
        self.pid = self.app.add_project({'name': 'Site', 'objective': 'Inspect the site'})['id']
        self.devices = 0

    def device(self, vendor=None, model=None):
        """A running fixture device; returns its address."""
        self.devices += 1
        gateway = Gateway(Reporting(vendor, model, f'device-{self.devices}'), ACCESS, f'{self.tmp.name}/edge-{self.devices}')
        server = Server(edge_handler(gateway))
        self.addCleanup(gateway.store.close)
        self.addCleanup(server.close)
        return server.url

    def robot(self, name, vendor=None, model=None, reports=None, connect=True):
        """A robot record; ``reports`` = (vendor, model) the device names, () for a device that names none."""
        p = {'project_id': self.pid, 'name': name, 'family': 'quadruped', 'vendor': vendor, 'model': model}
        if reports is not None:
            p.update(endpoint=self.device(*reports), token=ACCESS)
        robot = self.app.add_robot(p)
        if reports is not None and connect:
            robot = self.app.connect_robot({'robot_id': robot['id']})
        return robot

    def skill(self, name='Navigate', **more):
        return self.app.add_skill({'project_id': self.pid, 'name': name, 'version': '1.0', 'requires': ['navigation.goal'],
                                   'entrypoint': 'navigate', 'success_condition': 'at goal', 'stop_condition': 'timeout',
                                   'model_ids': [], **more})

    def application(self, robot, skill, name='Round', version='1.0'):
        return self.app.add_application({'project_id': self.pid, 'name': name, 'version': version, 'robot_id': robot['id'],
                                         'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class RobotProduct(Case):
    def test_typed_brand_and_model_are_kept_without_surrounding_space(self):
        robot = self.app.robot_models.register({'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped',
                                                'vendor': '  Acme  Robotics ', 'model': 'X1\n'})
        self.assertEqual(robot_model.of(robot), {'vendor': 'Acme Robotics', 'model': 'X1'})
        self.assertEqual(robot['product']['source'], 'manual')

    def test_adding_without_them_is_refused_when_no_device_reports(self):
        base = {'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped'}
        self.assertEqual(self.refused('robot_model_required', self.app.robot_models.register, base).extra['field'], 'vendor')
        # A device that reports none, one that says "unknown", and an address nothing answers at.
        for endpoint in (self.device(), self.device('unknown', 'unknown'), 'http://127.0.0.1:9'):
            self.refused('robot_model_required', self.app.robot_models.register, {**base, 'endpoint': endpoint, 'token': ACCESS})
        self.assertEqual(self.app.store.list('robots'), [])

    def test_half_of_it_and_placeholders_are_refused(self):
        base = {'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped'}
        self.assertEqual(self.refused('robot_model_required', self.app.robot_models.register, {**base, 'vendor': 'Acme'}).extra['field'], 'model')
        self.assertEqual(self.refused('robot_model_required', self.app.robot_models.register, {**base, 'vendor': 'unknown', 'model': 'X1'}).extra['field'], 'vendor')
        self.refused('robot_model_invalid', self.app.robot_models.register, {**base, 'vendor': 'A' * 101, 'model': 'X1'})

    def test_nothing_typed_takes_what_the_device_reports(self):
        robot = self.app.robot_models.register({'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped',
                                                'endpoint': self.device('Acme', 'X1'), 'token': ACCESS})
        self.assertEqual(robot_model.of(robot), {'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual(robot['product']['source'], 'device')

    def test_a_connection_check_replaces_typed_values_with_reported_ones(self):
        robot = self.robot('Dog', 'Typed', 'T1', reports=('Acme', 'X1'), connect=False)
        self.assertEqual(robot['product']['source'], 'manual')
        connected = self.app.connect_robot({'robot_id': robot['id']})
        self.assertEqual(robot_model.of(connected), {'vendor': 'Acme', 'model': 'X1'})
        saved = self.app.store.get('robots', robot['id'])
        self.assertEqual(saved['product']['source'], 'device')
        events = [e for e in self.app.store.list('events') if e['action'] == 'robot.model_changed']
        self.assertEqual(events[0]['details']['from'], {'vendor': 'Typed', 'model': 'T1'})

    def test_a_device_that_reports_none_leaves_typed_values(self):
        for reports in ((), ('unknown', 'unknown')):
            robot = self.robot('Dog', 'Typed', 'T1', reports=reports)
            self.assertEqual(robot_model.of(self.app.store.get('robots', robot['id'])), {'vendor': 'Typed', 'model': 'T1'})

    def test_a_person_fills_them_in_later_but_not_over_the_device(self):
        plain = self.robot('Old')
        self.assertIsNone(robot_model.of(plain))
        changed = self.app.robot_models.assign({'project_id': self.pid, 'robot_id': plain['id'], 'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual(robot_model.of(changed), {'vendor': 'Acme', 'model': 'X1'})
        self.assertNotIn('token', changed)
        reporting = self.robot('New', reports=('Acme', 'X2'))
        self.refused('robot_model_from_device', self.app.robot_models.assign,
                     {'project_id': self.pid, 'robot_id': reporting['id'], 'vendor': 'Other', 'model': 'O1'})
        self.refused('robot_model_required', self.app.robot_models.assign, {'project_id': self.pid, 'robot_id': plain['id']})

    def test_a_stale_copy_saved_by_other_code_does_not_undo_it(self):
        robot = self.robot('Old')
        stale = self.app.store.get('robots', robot['id'])
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': robot['id'], 'vendor': 'Acme', 'model': 'X1'})
        self.app.store.put('robots', {**stale, 'status': 'error'})
        saved = self.app.store.get('robots', robot['id'])
        self.assertEqual((saved['status'], robot_model.of(saved)), ('error', {'vendor': 'Acme', 'model': 'X1'}))
        self.assertEqual(saved['family'], 'quadruped')


class ApplicationBinding(Case):
    def test_an_application_carries_the_product_of_its_robot(self):
        application = self.application(self.robot('Dog', 'Acme', 'X1'), self.skill())
        self.assertEqual(application['product'], {'vendor': 'Acme', 'model': 'X1'})

    def test_every_robot_of_the_same_brand_and_model_can_be_checked_with_it(self):
        first, second = self.robot('Dog 1', 'Acme', 'X1', reports=()), self.robot('Dog 2', 'ACME', 'x1', reports=())
        other = self.robot('Arm', 'Acme', 'X2', reports=())
        application = self.application(first, self.skill())
        self.assertEqual(self.app.harness({'application_id': application['id']})['robot_id'], first['id'])
        report = self.app.harness({'application_id': application['id'], 'robot_id': second['id']})
        self.assertEqual((report['robot_id'], report['passed']), (second['id'], True))
        self.refused('app_other_model', self.app.harness, {'application_id': application['id'], 'robot_id': other['id']})
        ready = self.app.delivery_readiness({'project_id': self.pid, 'application_id': application['id'], 'robot_id': second['id']})
        self.assertEqual(ready['robot']['id'], second['id'])
        self.assertEqual([r['id'] for r in ready['robots']], [first['id'], second['id']])
        self.refused('app_other_model', self.app.delivery_readiness,
                     {'project_id': self.pid, 'application_id': application['id'], 'robot_id': other['id']})

    def test_test_and_release_go_to_the_robot_named_and_evidence_stays_per_robot(self):
        first, second = self.robot('Dog 1', 'Acme', 'X1', reports=()), self.robot('Dog 2', 'Acme', 'X1', reports=())
        application = self.application(first, self.skill())
        operation = self.app.onsite_test({'application_id': application['id'], 'robot_id': second['id'], 'hardware_authorized': True})
        self.assertEqual(operation['robot_id'], second['id'])
        report = next(e for e in self.app.store.list('evaluations') if e['scope'] == 'onsite')
        self.assertEqual((report['robot_id'], report['passed']), (second['id'], True))
        release = self.app.deploy({'application_id': application['id'], 'report_id': report['id']})
        self.assertEqual((release['robot_id'], release['status']), (second['id'], 'deployed'))
        # The first robot has no evidence of its own.
        with self.assertRaises(ValueError):
            self.app.deploy({'application_id': application['id'], 'report_id': report['id'], 'robot_id': first['id']})
        ready = self.app.delivery_readiness({'project_id': self.pid, 'application_id': application['id']})
        self.assertEqual((ready['robot']['id'], ready['next_step']), (first['id'], 'check'))

    def test_without_its_first_robot_it_goes_to_another_of_the_model_or_says_there_is_none(self):
        first, second = self.robot('Dog 1', 'Acme', 'X1', reports=()), self.robot('Dog 2', 'Acme', 'X1', reports=())
        application = self.application(first, self.skill())
        self.app.store.delete('robots', first['id'])
        self.assertEqual(self.app.harness({'application_id': application['id']})['robot_id'], second['id'])
        self.app.store.delete('robots', second['id'])
        self.refused('app_no_robot', self.app.harness, {'application_id': application['id']})

    def test_a_robot_without_a_product_keeps_its_applications_to_itself_until_it_has_one(self):
        old, twin = self.robot('Old', reports=()), self.robot('Twin', 'Acme', 'X1', reports=())
        application = self.application(old, self.skill())
        self.assertNotIn('product', application)
        self.assertTrue(self.app.harness({'application_id': application['id']})['passed'])
        self.refused('app_other_model', self.app.harness, {'application_id': application['id'], 'robot_id': twin['id']})
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': old['id'], 'vendor': 'acme', 'model': 'x1'})
        saved = self.app.store.get('applications', application['id'])
        self.assertEqual((saved['product'], saved['digest']), ({'vendor': 'acme', 'model': 'x1'}, application['digest']))
        self.assertEqual(self.app.harness({'application_id': application['id'], 'robot_id': twin['id']})['robot_id'], twin['id'])

    def test_what_a_device_is_sent_always_has_the_digest_the_device_computes(self):
        from brainos import robot_caps
        from neuxmind_runtime.core import json_digest
        old, new = self.robot('Old', reports=()), self.robot('New', 'Acme', 'X1', reports=())
        adopted, made = self.application(old, self.skill()), self.application(new, self.skill('Walk'), 'Second')
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': old['id'], 'vendor': 'Acme', 'model': 'X1'})
        for application in (self.app.store.get('applications', adopted['id']), self.app.store.get('applications', made['id'])):
            self.assertEqual(application['product'], {'vendor': 'Acme', 'model': 'X1'})
            sent = robot_caps.typed_application(self.app, application)
            self.assertEqual(json_digest({k: v for k, v in sent.items() if k != 'digest'}), sent['digest'])
        self.assertNotIn('product', robot_caps.typed_application(self.app, self.app.store.get('applications', adopted['id'])))
        self.assertIn('product', robot_caps.typed_application(self.app, made))

    def test_an_application_with_a_skill_of_another_brand_is_not_adopted(self):
        first, second = self.robot('One', reports=()), self.robot('Two', reports=())
        shared = self.skill()
        one, two = self.application(first, shared), self.application(second, shared, 'Other')
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': first['id'], 'vendor': 'Acme', 'model': 'X1'})
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': second['id'], 'vendor': 'Acme', 'model': 'X2'})
        self.assertEqual(self.app.store.get('applications', one['id'])['product'], {'vendor': 'Acme', 'model': 'X1'})
        self.assertNotIn('product', self.app.store.get('applications', two['id']))      # still usable, with its one robot
        self.assertEqual(self.app.harness({'application_id': two['id']})['robot_id'], second['id'])

    def test_the_local_agent_reports_an_application_without_a_robot_and_goes_on(self):
        robot = self.robot('Dog', 'Acme', 'X1', reports=())
        self.application(robot, self.skill())
        self.app.store.delete('robots', robot['id'])
        findings = self.app.run_agent({'project_id': self.pid, 'agent_id': 'testing'})['outputs']['findings']
        self.assertTrue(any('没有可以使用这个应用的机器人' in line for line in findings), findings)

    def test_a_new_version_stays_with_the_brand_and_model(self):
        first, second, other = self.robot('Dog 1', 'Acme', 'X1'), self.robot('Dog 2', 'Acme', 'X1'), self.robot('Arm', 'Acme', 'X2')
        skill = self.skill()
        application = self.application(first, skill)
        step = [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]
        revision = {'project_id': self.pid, 'source_application_id': application['id'], 'name': 'Round', 'version': '1.1', 'steps': step}
        self.assertEqual(self.app.preview_revision({**revision, 'robot_id': second['id']})['manifest']['product'], application['product'])
        self.refused('app_other_model', self.app.preview_revision, {**revision, 'robot_id': other['id']})


class SkillBinding(Case):
    def test_a_skill_takes_the_product_typed_or_that_of_the_robot_it_was_read_from(self):
        robot = self.robot('Dog', 'Acme', 'X1')
        self.assertEqual(self.skill(robot_id=robot['id'])['product'], {'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual(self.skill(vendor='Other', model='O1')['product'], {'vendor': 'Other', 'model': 'O1'})
        self.assertNotIn('product', self.skill())
        with self.assertRaises(CodedError):
            self.skill(vendor='Other')

    def test_an_application_takes_only_skills_of_its_brand_and_model(self):
        robot, other = self.robot('Dog', 'Acme', 'X1'), self.robot('Arm', 'Acme', 'X2')
        foreign = self.skill('Grasp', robot_id=other['id'])
        self.refused('skill_other_model', self.application, robot, foreign)
        own = self.skill('Walk', vendor='ACME', model='x1')
        self.assertEqual(self.application(robot, own)['product'], {'vendor': 'Acme', 'model': 'X1'})

    def test_a_skill_without_one_takes_that_of_the_first_application_that_uses_it(self):
        robot, other = self.robot('Dog', 'Acme', 'X1'), self.robot('Arm', 'Acme', 'X2')
        skill = self.skill()
        self.application(robot, skill)
        saved = self.app.store.get('skills', skill['id'])
        self.assertEqual((saved['product'], saved['digest']), ({'vendor': 'Acme', 'model': 'X1'}, skill['digest']))
        self.refused('skill_other_model', self.application, other, saved, 'Other round')


class RecordsFromBefore(Case):
    def test_opening_a_workspace_fills_in_what_the_saved_manifests_say(self):
        robot = self.robot('Dog', reports=())
        skill = self.skill()
        application = self.application(robot, skill)
        store = self.app.store
        order = [a['id'] for a in store.list('applications')]
        saved = store.get('robots', robot['id'])
        # As a version from before left it: the device named a brand and model, nobody read them.
        store.put('robots', {**saved, 'manifest': {**saved['manifest'], 'vendor': 'Acme', 'model': 'X1'}})
        self.app.close()
        self.app = Platform(self.root)
        store = self.app.store
        self.assertEqual(store.get('robots', robot['id'])['product']['source'], 'device')
        self.assertEqual(store.get('applications', application['id'])['product'], {'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual(store.get('applications', application['id'])['digest'], application['digest'])
        self.assertEqual(store.get('skills', skill['id'])['product'], {'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual([a['id'] for a in store.list('applications')], order)
        self.assertEqual([e for e in store.list('events') if e['action'] == 'robot_model.migration_failed'], [])


if __name__ == '__main__':
    unittest.main()
