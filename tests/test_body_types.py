"""Issue #32 (R1-11): the platform keeps the list of body types.

The list is saved with the desktop workspace. Two types are built in (quadruped, manipulator); more
can be added and removed. A type carries a name, the identifier of its state machine template and the
motion-control interfaces a body of that type should have (standard capability ids). A type that a
device still uses cannot be removed. What a person chose is not replaced by what the device reports.

Devices here are stand-ins: a patched connector or a local SSH protocol fixture. Nothing reaches a robot.
"""
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import tempfile
import unittest
from unittest.mock import patch

import test_ssh_jump as ssh_base
from brainos import body_types
from brainos.contracts import FAMILIES
from brainos.errors import error_payload
from brainos.server import Application, make_handler
from brainos.service import Platform
from brainos.store import Store
from neuxmind_runtime.adapters import sim_arm, sim_quadruped
from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest
from test_product import Server

BUILT_IN = ['quadruped', 'manipulator']
OLD_ONLY = sorted(set(FAMILIES) - set(BUILT_IN))     # the five values that are no longer offered
WHEELED = {'name': '轮式底盘', 'state_machine_template': 'wheeled.default',
           'required_capabilities': ['safety.stop', 'base.velocity', 'base.odometry']}


def device(family, instance_id='stand-in', **extra):
    """What a device answers on /v1/manifest and /v1/status (through a patched connector)."""
    manifest = {'schema': 'robot.adapter.v1', 'instance_id': instance_id, 'name': 'Stand-in', 'family': family,
                'adapter_version': '1', 'capabilities': ['safety.stop', 'system.health'], **extra}

    def request(endpoint, path, *args):
        return manifest if path == '/v1/manifest' else {'instance_id': instance_id}
    return patch('brainos.connector.request', side_effect=request)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/workspace'
        self.platform = self.open()
        self.pid = self.platform.add_project({'name': 'Site A', 'objective': 'Inspect the site'})['id']

    def open(self):
        platform = Platform(self.root)
        self.addCleanup(platform.close)
        return platform

    @property
    def types(self):
        return self.platform.body_types

    def ids(self):
        return [t['id'] for t in self.types.list()]

    def add_type(self, **changes):
        return self.types.add({**WHEELED, **changes})

    def add_robot(self, family, name='Robot', project_id=None, **extra):
        return self.platform.add_robot({'project_id': project_id or self.pid, 'name': name, 'family': family, **extra})

    def family(self, robot_id):
        return self.platform.store.get('robots', robot_id)['family']


class BuiltInTests(Case):
    def test_list_starts_with_quadruped_and_manipulator(self):
        self.assertEqual(self.ids(), BUILT_IN)
        self.assertTrue(all(t['builtin'] for t in self.types.list()))

    def test_every_type_has_a_name_a_template_and_standard_interfaces(self):
        for item in self.types.list():
            with self.subTest(item['id']):
                self.assertTrue(item['name'].strip())
                self.assertTrue(body_types.TEMPLATE_ID.fullmatch(item['state_machine_template']))
                self.assertTrue(item['required_capabilities'])
                for cap_id in item['required_capabilities']:
                    self.assertIn(cap_id, spec_caps.CAPABILITIES)

    def test_built_in_types_use_values_a_device_can_report(self):
        # The standard is unchanged: a device reports one of its seven values, two of which are these.
        self.assertLessEqual(set(BUILT_IN), set(spec_manifest.FAMILIES))

    def test_simulated_bodies_have_what_their_type_expects(self):
        for adapter, type_id in ((sim_quadruped.SimQuadruped, 'quadruped'), (sim_arm.SimArm, 'manipulator')):
            with self.subTest(type_id):
                declared = spec_manifest.capability_ids(adapter().manifest())
                expected = set(self.types.get(type_id)['required_capabilities'])
                self.assertEqual(expected - declared, set())

    def test_workspace_state_carries_the_list_and_the_interfaces_to_choose_from(self):
        snapshot = self.platform.snapshot()
        self.assertEqual([t['id'] for t in snapshot['body_types']], BUILT_IN)
        offered = {c['id']: c for c in snapshot['body_type_interfaces']}
        self.assertEqual(set(offered), set(spec_caps.CAPABILITIES))
        self.assertEqual(offered['base.velocity']['title'], spec_caps.CAPABILITIES['base.velocity'].title)


class AddTests(Case):
    def test_added_type_can_be_chosen_when_adding_a_device(self):
        added = self.add_type()

        robot = self.add_robot(added['id'])

        self.assertIn(added['id'], [t['id'] for t in self.platform.snapshot()['body_types']])
        self.assertEqual(self.family(robot['id']), added['id'])

    def test_added_type_keeps_its_three_parts(self):
        added = self.add_type()

        saved = self.types.get(added['id'])

        self.assertEqual({k: saved[k] for k in WHEELED}, WHEELED)
        self.assertFalse(saved['builtin'])
        self.assertNotIn(saved['id'], FAMILIES)       # never mistaken for a value of the standard

    def test_added_types_survive_a_restart(self):
        added = self.add_type()
        self.platform.close()

        self.platform = self.open()

        self.assertEqual(self.ids(), BUILT_IN + [added['id']])

    def test_parts_of_a_type_are_required(self):
        bad = {'name': ('body_type_name_invalid', ['', '   ', None, 7, 'x' * 41, 'right\u202eto left', 'zero\u200bwidth']),
               'state_machine_template': ('body_type_template_invalid', ['', None, 'Wheeled Default', '../escape', 'x' * 101]),
               'required_capabilities': ('body_type_interfaces_required', [None, [], 'base.velocity'])}
        for field, (code, values) in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError) as refused:
                        self.add_type(**{field: value})
                    self.assertEqual((refused.exception.code, refused.exception.extra['field']), (code, field))
        self.assertEqual(self.ids(), BUILT_IN)

    def test_interfaces_must_be_standard_capability_ids(self):
        for cap_id in ('base.fly', 'cmd_vel', 'x.vendor.special', 'BASE.VELOCITY', 7):
            with self.subTest(cap_id):
                with self.assertRaises(ValueError) as refused:
                    self.add_type(required_capabilities=['safety.stop', cap_id])
                self.assertEqual(refused.exception.code, 'body_type_interface_unknown')
                self.assertIn(str(cap_id), str(refused.exception))

    def test_interfaces_are_kept_once_each_in_the_order_given(self):
        added = self.add_type(required_capabilities=['base.velocity', 'safety.stop', 'base.velocity'])

        self.assertEqual(added['required_capabilities'], ['base.velocity', 'safety.stop'])

    def test_two_types_cannot_share_a_name(self):
        self.add_type()
        self.add_type(name='AGV Cart')
        same = (WHEELED['name'], ' ' + WHEELED['name'] + ' ', self.types.get('quadruped')['name'],
                'agv cart', 'AGV  Cart', '\uff21\uff27\uff36\u3000Cart')      # case, spacing, full-width letters
        for name in same:
            with self.subTest(name):
                with self.assertRaises(ValueError) as refused:
                    self.add_type(name=name)
                self.assertEqual(refused.exception.code, 'body_type_name_taken')
        self.assertEqual(len(self.ids()), len(BUILT_IN) + 2)

    def test_name_is_saved_the_way_people_compare_it(self):
        added = self.add_type(name='  \uff21\uff27\uff36   Cart ')
        pasted = self.add_type(name='two\nlines\tpasted')

        self.assertEqual((added['name'], pasted['name']), ('AGV Cart', 'two lines pasted'))

    def test_list_has_room_for_a_limited_number_of_added_types(self):
        with patch.object(body_types, 'ADDED_MAX', 2):
            self.add_type(name='One')
            self.add_type(name='Two')
            with self.assertRaises(ValueError) as refused:
                self.add_type(name='Three')
        self.assertEqual(refused.exception.code, 'body_type_limit')
        self.assertEqual(len(self.ids()), len(BUILT_IN) + 2)

    def test_adding_is_recorded(self):
        added = self.add_type()

        event = self.platform.store.list('events')[0]

        self.assertEqual((event['action'], event['details']['body_type_id']), ('body_type.added', added['id']))


class RemoveTests(Case):
    def test_unused_type_is_removed(self):
        added = self.add_type()

        self.types.remove({'body_type_id': added['id']})

        self.assertEqual(self.ids(), BUILT_IN)

    def test_type_in_use_is_refused_and_the_devices_are_listed(self):
        added = self.add_type()
        other = self.platform.add_project({'name': 'Site B', 'objective': 'Carry parts'})['id']
        first = self.add_robot(added['id'], name='Patrol 1')
        second = self.add_robot(added['id'], name='Carrier 2', project_id=other)
        self.add_robot('quadruped', name='Unrelated')

        with self.assertRaises(ValueError) as refused:
            self.types.remove({'body_type_id': added['id']})

        error = refused.exception
        self.assertEqual(error.code, 'body_type_in_use')
        self.assertEqual({(d['id'], d['name'], d['project_name']) for d in error.devices},
                         {(first['id'], 'Patrol 1', 'Site A'), (second['id'], 'Carrier 2', 'Site B')})
        self.assertIn('Patrol 1', str(error))
        self.assertIn('Carrier 2', str(error))
        self.assertIn(added['id'], self.ids())

    def test_sentence_names_a_few_devices_and_says_how_many_there_are(self):
        added = self.add_type()
        for number in range(body_types.NAMES_IN_MESSAGE + 2):
            self.add_robot(added['id'], name=f'Robot {number}')

        with self.assertRaises(ValueError) as refused:
            self.types.remove({'body_type_id': added['id']})

        self.assertEqual(len(refused.exception.devices), body_types.NAMES_IN_MESSAGE + 2)
        self.assertIn(f'等 {body_types.NAMES_IN_MESSAGE + 2} 台', str(refused.exception))
        self.assertNotIn(f'Robot {body_types.NAMES_IN_MESSAGE}', str(refused.exception))

    def test_refusal_reaches_the_browser_with_the_devices(self):
        added = self.add_type()
        robot = self.add_robot(added['id'], name='Patrol 1')
        with self.assertRaises(ValueError) as refused:
            self.types.remove({'body_type_id': added['id']})

        body = error_payload(refused.exception)

        self.assertEqual(body['code'], 'body_type_in_use')
        self.assertEqual(body['devices'], [{'id': robot['id'], 'name': 'Patrol 1', 'project_id': self.pid,
                                            'project_name': 'Site A'}])
        self.assertEqual(body['devices_total'], 1)

    def test_type_can_be_removed_once_no_device_uses_it(self):
        added = self.add_type()
        moved = self.add_robot(added['id'], name='Moved')
        gone = self.add_robot(added['id'], name='Gone')
        self.types.assign({'project_id': self.pid, 'robot_id': moved['id'], 'family': 'quadruped'})
        with self.assertRaises(ValueError):
            self.types.remove({'body_type_id': added['id']})
        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': gone['id']})

        self.types.remove({'body_type_id': added['id']})

        self.assertEqual(self.ids(), BUILT_IN)

    def test_built_in_type_is_not_removed(self):
        for type_id in BUILT_IN:
            with self.subTest(type_id):
                with self.assertRaises(ValueError) as refused:
                    self.types.remove({'body_type_id': type_id})
                self.assertEqual(refused.exception.code, 'body_type_builtin')
        self.assertEqual(self.ids(), BUILT_IN)

    def test_unknown_type_is_refused(self):
        for type_id in (None, '', 'mobile', 'bodytype-missing', ['quadruped']):
            with self.subTest(type_id):
                with self.assertRaises(ValueError) as refused:
                    self.types.remove({'body_type_id': type_id})
                self.assertEqual(refused.exception.code, 'body_type_unknown')

    def test_removing_is_recorded(self):
        added = self.add_type()

        self.types.remove({'body_type_id': added['id']})

        event = self.platform.store.list('events')[0]
        self.assertEqual((event['action'], event['details']['name']), ('body_type.removed', WHEELED['name']))


class NewDeviceTests(Case):
    def test_new_device_needs_a_type_from_the_list(self):
        for family in [None, '', 'bodytype-missing', ['quadruped'], *OLD_ONLY]:
            with self.subTest(family):
                with self.assertRaises(ValueError) as refused:
                    self.add_robot(family)
                self.assertEqual(refused.exception.code, 'body_type_required')
        self.assertEqual(self.platform.store.list('robots'), [])

    def test_type_removed_between_the_check_and_the_save_is_refused(self):
        added = self.add_type()
        check, removed = self.types.require, []

        def removed_right_after_the_first_check(type_id):
            answer = check(type_id)
            if not removed:
                removed.append(self.types.remove({'body_type_id': added['id']}))
            return answer

        with patch.object(self.types, 'require', side_effect=removed_right_after_the_first_check):
            with self.assertRaises(ValueError) as refused:
                self.add_robot(added['id'])

        self.assertEqual(refused.exception.code, 'body_type_required')
        self.assertEqual(self.platform.store.list('robots'), [])

    def test_removed_type_cannot_be_chosen(self):
        added = self.add_type()
        self.types.remove({'body_type_id': added['id']})

        with self.assertRaises(ValueError) as refused:
            self.add_robot(added['id'])
        self.assertEqual(refused.exception.code, 'body_type_required')


class SshDeviceTests(ssh_base.SshCase):
    def test_added_type_can_be_chosen_when_adding_a_device_over_ssh(self):
        added = self.platform.body_types.add(dict(WHEELED))

        connection = self.connect_directly(family=added['id'])

        self.assertEqual(self.store.get('robots', connection['robot_id'])['family'], added['id'])

    def test_registered_device_keeps_its_type_when_it_gets_an_ssh_connection(self):
        self.store.put('robots', {'id': 'robot-old', 'project_id': self.pid, 'name': 'Registered earlier', 'family': 'custom',
                                  'endpoint': '', 'token': '', 'status': 'unconnected', 'capabilities': []})

        connection = self.connect_directly(robot_id='robot-old', family='manipulator')

        saved = self.store.get('robots', 'robot-old')
        self.assertEqual((saved['family'], saved['connection_id']), ('custom', connection['id']))

    def test_ssh_device_without_a_listed_type_is_refused_and_nothing_is_saved(self):
        for family in (None, 'custom'):
            with self.subTest(family):
                with self.assertRaises(ValueError) as refused:
                    self.connect_directly(family=family)
                self.assertEqual(refused.exception.code, 'body_type_required')
        self.assertEqual(self.store.list('robots'), [])
        self.assertEqual(self.store.list('device_connections'), [])
        self.assertEqual(self.store.list('agent_secrets'), [])


class ReportedTypeTests(Case):
    def test_connecting_keeps_the_type_the_person_chose(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')

        with device('manipulator'):
            connected = self.platform.connect_robot({'robot_id': robot['id']})

        self.assertEqual(connected['status'], 'connected')
        self.assertEqual(self.family(robot['id']), 'quadruped')
        self.assertEqual(connected['manifest']['family'], 'manipulator')       # what the device said stays readable

    def test_connecting_keeps_a_type_the_platform_added(self):
        added = self.add_type()
        robot = self.add_robot(added['id'], endpoint='http://127.0.0.1:9')

        with device('mobile'):
            self.platform.connect_robot({'robot_id': robot['id']})

        self.assertEqual(self.family(robot['id']), added['id'])

    def changing_meanwhile(self, robot, family, fail=False):
        """A device that answers slowly: while the desktop waits for it, a person changes the robot's type."""
        def answer(endpoint, path, *args):
            self.types.assign({'project_id': self.pid, 'robot_id': robot['id'], 'family': family})
            if fail:
                raise ValueError('设备没有回应')
            return ({'schema': 'robot.adapter.v1', 'instance_id': 'stand-in', 'name': 'Stand-in', 'family': 'mobile',
                     'adapter_version': '1', 'capabilities': ['safety.stop']} if path == '/v1/manifest'
                    else {'instance_id': 'stand-in'})
        return answer

    def test_type_changed_while_the_device_was_asked_is_kept(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')

        with patch('brainos.connector.request', side_effect=self.changing_meanwhile(robot, 'manipulator')):
            self.platform.connect_robot({'robot_id': robot['id']})

        saved = self.platform.store.get('robots', robot['id'])
        self.assertEqual((saved['family'], saved['status']), ('manipulator', 'connected'))

    def test_type_changed_while_a_failing_check_ran_is_kept(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')

        with patch('brainos.connector.request', side_effect=self.changing_meanwhile(robot, 'manipulator', fail=True)):
            with self.assertRaises(ValueError):
                self.platform.connect_robot({'robot_id': robot['id']})

        saved = self.platform.store.get('robots', robot['id'])
        self.assertEqual((saved['family'], saved['status']), ('manipulator', 'error'))

    def test_type_changed_while_the_detail_was_read_is_kept(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')
        with device('quadruped'):
            self.platform.connect_robot({'robot_id': robot['id']})

        def slow_manifest(record):
            self.types.assign({'project_id': self.pid, 'robot_id': robot['id'], 'family': 'manipulator'})
            raise ValueError('设备没有提供 v2 清单')

        with patch('brainos.robot_caps.fetch_manifest_v2', side_effect=slow_manifest):
            detail = self.platform.robot_detail({'robot_id': robot['id'], 'project_id': self.pid})

        self.assertEqual(detail['robot']['family'], 'manipulator')
        self.assertEqual(self.family(robot['id']), 'manipulator')

    def test_type_changed_while_an_on_site_test_waited_for_the_device_is_kept(self):
        # The on-site test reads the robot, asks the device to validate, and saves the robot again when the
        # device reports a latched e-stop.
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')
        manifest = {'schema': 'robot.adapter.v1', 'instance_id': 'stand-in', 'name': 'Stand-in', 'family': 'mobile',
                    'adapter_version': '1', 'capabilities': ['navigation.goal']}
        skill = self.platform.add_skill({'project_id': self.pid, 'name': 'Navigate', 'version': '1.0', 'requires': ['navigation.goal'],
                                         'entrypoint': 'navigate', 'success_condition': 'at goal',
                                         'stop_condition': 'timeout', 'model_ids': []})
        with device('mobile', capabilities=['navigation.goal']):
            self.platform.connect_robot({'robot_id': robot['id']})
        app = self.platform.add_application({'project_id': self.pid, 'name': 'Inspection', 'version': '1.0', 'robot_id': robot['id'],
                                             'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})

        def answer(endpoint, path, *args):
            if path == '/v1/validate':
                self.types.assign({'project_id': self.pid, 'robot_id': robot['id'], 'family': 'manipulator'})
                return {'accepted': False, 'estop_latched': True}
            return manifest if path == '/v1/manifest' else {'instance_id': 'stand-in'}

        with patch('brainos.connector.request', side_effect=answer), self.assertRaises(ValueError):
            self.platform.onsite_test({'application_id': app['id'], 'hardware_authorized': True})

        saved = self.platform.store.get('robots', robot['id'])
        self.assertEqual((saved['family'], saved['estop_latched']), ('manipulator', True))

    def test_models_are_still_matched_against_what_the_device_declares(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')
        digest = secrets.token_hex(32)
        package = {'id': 'model-fixture', 'project_id': self.pid, 'name': 'Gait', 'package_ref': 'test://package',
                   'package_digest': digest, 'runtime': 'test-runtime'}
        extra = {'capabilities': ['safety.stop', 'model.install'], 'model_runtimes': ['test-runtime']}

        def install(model, reported):
            self.platform.store.put('models', model)
            manifest = {'schema': 'robot.adapter.v1', 'instance_id': 'stand-in', 'name': 'Stand-in',
                        'family': reported, 'adapter_version': '1', **extra}
            answers = {'/v1/manifest': manifest, '/v1/status': {'instance_id': 'stand-in'},
                       '/v1/models/install': {'accepted': True, 'package_digest': digest}}
            with patch('brainos.connector.request', side_effect=lambda endpoint, path, *args: answers[path]):
                return self.platform.deploy_model({'model_id': model['id'], 'robot_id': robot['id']})

        self.assertEqual(install({**package, 'families': ['mobile']}, 'mobile')['status'], 'deployed')
        with self.assertRaises(ValueError):
            install({**package, 'families': ['quadruped']}, 'mobile')
        self.assertEqual(self.family(robot['id']), 'quadruped')


class AssignTests(Case):
    def assign(self, robot_id, family, project_id=None):
        return self.types.assign({'project_id': project_id or self.pid, 'robot_id': robot_id, 'family': family})

    def test_type_of_a_registered_device_can_be_changed(self):
        robot = self.add_robot('quadruped')

        changed = self.assign(robot['id'], 'manipulator')

        self.assertEqual(changed['family'], 'manipulator')
        self.assertEqual(self.family(robot['id']), 'manipulator')
        self.assertNotIn('token', changed)
        event = self.platform.store.list('events')[0]
        self.assertEqual((event['action'], event['details']['from'], event['details']['to']),
                         ('robot.body_type_changed', 'quadruped', 'manipulator'))

    def test_changing_the_type_changes_nothing_else(self):
        robot = self.add_robot('quadruped', endpoint='http://127.0.0.1:9')
        with device('quadruped'):
            self.platform.connect_robot({'robot_id': robot['id']})
        before = self.platform.store.get('robots', robot['id'])

        self.assign(robot['id'], 'manipulator')

        after = self.platform.store.get('robots', robot['id'])
        self.assertEqual({k: v for k, v in after.items() if k != 'family'},
                         {k: v for k, v in before.items() if k != 'family'})

    def test_type_must_be_in_the_list(self):
        robot = self.add_robot('quadruped')
        for family in [None, '', 'bodytype-missing', *OLD_ONLY]:
            with self.subTest(family):
                with self.assertRaises(ValueError) as refused:
                    self.assign(robot['id'], family)
                self.assertEqual(refused.exception.code, 'body_type_required')
        self.assertEqual(self.family(robot['id']), 'quadruped')

    def test_robot_and_project_have_to_be_named(self):
        robot = self.add_robot('quadruped')
        for request in ({'robot_id': robot['id']}, {'project_id': self.pid}, {'project_id': self.pid, 'robot_id': [robot['id']]},
                        {'project_id': {'id': self.pid}, 'robot_id': robot['id']}, {'project_id': self.pid, 'robot_id': 'robot-missing'}):
            with self.subTest(request), self.assertRaises(ValueError):
                self.types.assign({**request, 'family': 'manipulator'})
        self.assertEqual(self.family(robot['id']), 'quadruped')

    def test_device_of_another_project_is_refused(self):
        robot = self.add_robot('quadruped')
        other = self.platform.add_project({'name': 'Site B', 'objective': 'Carry parts'})['id']

        with self.assertRaises(ValueError):
            self.assign(robot['id'], 'manipulator', project_id=other)

        self.assertEqual(self.family(robot['id']), 'quadruped')

    def test_simulated_body_keeps_the_type_of_its_kind(self):
        self.platform.store.put('robots', {'id': 'robot-sim', 'project_id': self.pid, 'name': 'Sim', 'family': 'quadruped',
                                           'endpoint': '', 'token': '', 'status': 'unconnected', 'capabilities': [],
                                           'sim': {'kind': 'quadruped', 'adapter': 'sim_quadruped', 'enabled': False}})

        with self.assertRaises(ValueError) as refused:
            self.assign('robot-sim', 'manipulator')

        self.assertEqual(refused.exception.code, 'body_type_sim_fixed')
        self.assertEqual(self.family('robot-sim'), 'quadruped')


class TypeOwnerTests(Case):
    """Only a change of type changes the type of a saved robot, whoever else saves the record."""

    def test_copy_read_before_a_change_cannot_undo_it(self):
        robot = self.add_robot('quadruped')
        store = self.platform.store
        earlier = store.get('robots', robot['id'])                  # read, then a slow answer of the device ...
        self.types.assign({'project_id': self.pid, 'robot_id': robot['id'], 'family': 'manipulator'})

        written = store.put('robots', {**earlier, 'status': 'error'})   # ... and the copy is saved afterwards

        saved = store.get('robots', robot['id'])
        self.assertEqual((saved['family'], saved['status']), ('manipulator', 'error'))
        self.assertEqual(written['family'], 'manipulator')          # the writer's copy says what was saved

    def test_record_cannot_be_given_another_type_by_saving_it(self):
        robot = self.add_robot('quadruped')
        store = self.platform.store

        store.put('robots', {**store.get('robots', robot['id']), 'family': 'mobile'})

        self.assertEqual(self.family(robot['id']), 'quadruped')

    def test_new_record_is_saved_with_the_type_it_carries(self):
        self.platform.store.put('robots', {'id': 'robot-new', 'project_id': self.pid, 'name': 'New', 'family': 'manipulator'})

        self.assertEqual(self.family('robot-new'), 'manipulator')

    def test_record_saved_without_a_type_gets_one(self):
        store = self.platform.store
        store.put('robots', {'id': 'robot-untyped', 'project_id': self.pid, 'name': 'No type', 'endpoint': '', 'token': '',
                             'status': 'unconnected', 'capabilities': []})
        self.assertNotIn('family', [key for r in self.platform.snapshot()['robots'] if r['id'] == 'robot-untyped' for key in r])

        self.types.assign({'project_id': self.pid, 'robot_id': 'robot-untyped', 'family': 'quadruped'})

        self.assertEqual(self.family('robot-untyped'), 'quadruped')

    def test_other_records_are_saved_as_given(self):
        store = self.platform.store
        store.put('skills', {'id': 'skill-x', 'family': 'quadruped'})

        store.put('skills', {'id': 'skill-x', 'family': 'mobile'})

        self.assertEqual(store.get('skills', 'skill-x')['family'], 'mobile')


class UpgradeTests(unittest.TestCase):
    """A workspace written before this change: its devices keep the type they have."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/workspace'
        old = Store(self.root)
        old.put('projects', {'id': 'project-old', 'name': 'Old site', 'objective': 'Registered before the list existed'})
        for family in FAMILIES:
            old.put('robots', {'id': 'robot-' + family, 'project_id': 'project-old', 'name': 'Old ' + family,
                               'family': family, 'endpoint': 'http://127.0.0.1:9', 'token': '', 'status': 'unconnected',
                               'capabilities': [], 'created_at': '2026-11-01T00:00:00+00:00'})
        old.close()
        self.platform = self.open()

    def open(self):
        platform = Platform(self.root)
        self.addCleanup(platform.close)
        return platform

    def families(self):
        return {r['id']: r['family'] for r in self.platform.store.list('robots')}

    def test_registered_devices_keep_their_type(self):
        expected = {'robot-' + family: family for family in FAMILIES}

        shown = {r['id']: r['family'] for r in self.platform.snapshot()['robots']}
        self.platform.close()
        self.platform = self.open()

        self.assertEqual(shown, expected)
        self.assertEqual(self.families(), expected)

    def test_old_types_are_not_put_into_the_list(self):
        snapshot = self.platform.snapshot()

        self.assertEqual([t['id'] for t in snapshot['body_types']], BUILT_IN)
        for family in OLD_ONLY:
            self.assertEqual(snapshot['families'][family], FAMILIES[family])    # still has a name to show

    def test_connecting_does_not_change_the_type_of_a_registered_device(self):
        for family, reported in (('custom', 'quadruped'), ('mobile', 'manipulator'), ('quadruped', 'mobile')):
            with self.subTest(family=family, reported=reported), device(reported, 'device-' + family):
                self.platform.connect_robot({'robot_id': 'robot-' + family})
        self.assertEqual(self.families(), {'robot-' + family: family for family in FAMILIES})

    def test_device_with_an_old_type_still_works(self):
        with device('mobile'):
            self.platform.connect_robot({'robot_id': 'robot-mobile'})
            detail = self.platform.robot_detail({'robot_id': 'robot-mobile', 'project_id': 'project-old'})
        self.platform.configure_connection({'kind': 'robots', 'id': 'robot-mobile', 'endpoint': 'http://127.0.0.1:19'})

        self.assertEqual(detail['robot']['family'], 'mobile')
        self.assertEqual(self.families()['robot-mobile'], 'mobile')

    def test_device_with_an_old_type_moves_to_a_listed_type_only_by_hand(self):
        types = self.platform.body_types

        types.assign({'project_id': 'project-old', 'robot_id': 'robot-custom', 'family': 'quadruped'})

        self.assertEqual(self.families()['robot-custom'], 'quadruped')
        with self.assertRaises(ValueError):       # and the old value cannot be chosen again
            types.assign({'project_id': 'project-old', 'robot_id': 'robot-custom', 'family': 'custom'})
        self.assertEqual({k: v for k, v in self.families().items() if k != 'robot-custom'},
                         {'robot-' + family: family for family in FAMILIES if family != 'custom'})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name)
        self.server = Server(make_handler(self.app))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.close)
        self.addCleanup(self.server.close)

    def request(self, path, payload=None, cookie=''):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.http.server_port)
        headers = {'Cookie': cookie, **({'Content-Type': 'application/json'} if payload is not None else {})}
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None, headers)
        response = conn.getresponse()
        raw, mime = response.read(), response.getheader('Content-Type')
        result = (response.status, json.loads(raw) if mime.startswith('application/json') else raw.decode(),
                  response.getheader('Set-Cookie'))
        conn.close()
        return result

    def account(self, email):
        form = {'name': 'Workspace', 'email': email, 'password': 'pw' + secrets.token_hex(8) + '7'}
        code = self.request('/api/register/start', form)[1]['dev_code']
        status, _, cookie = self.request('/api/register', {**form, 'code': code})
        self.assertEqual(status, 200)
        cookie = cookie.split(';')[0]
        self.request('/api/projects', {'name': 'Site A', 'objective': 'Inspect the site'}, cookie)
        return cookie, self.request('/api/state', cookie=cookie)[1]['projects'][0]['id']

    def test_add_choose_refuse_change_and_remove_over_the_api(self):
        cookie, pid = self.account('a@example.test')

        status, added, _ = self.request('/api/body-types', WHEELED, cookie)
        self.assertEqual(status, 200)
        listed = [t['id'] for t in self.request('/api/state', cookie=cookie)[1]['body_types']]
        self.assertEqual(listed, BUILT_IN + [added['id']])

        status, robot, _ = self.request('/api/robots', {'vendor': 'Acme', 'model': 'X1', 'project_id': pid, 'name': 'Patrol 1', 'family': added['id']}, cookie)
        self.assertEqual(status, 200)

        status, refusal, _ = self.request('/api/body-types/remove', {'body_type_id': added['id']}, cookie)
        self.assertEqual((status, refusal['code']), (400, 'body_type_in_use'))
        self.assertEqual(([d['name'] for d in refusal['devices']], refusal['devices_total']), (['Patrol 1'], 1))

        status, changed, _ = self.request('/api/robots/body-type',
                                          {'project_id': pid, 'robot_id': robot['id'], 'family': 'quadruped'}, cookie)
        self.assertEqual((status, changed['family']), (200, 'quadruped'))
        self.assertEqual(self.request('/api/body-types/remove', {'body_type_id': added['id']}, cookie)[0], 200)
        self.assertEqual([t['id'] for t in self.request('/api/state', cookie=cookie)[1]['body_types']], BUILT_IN)

    def test_each_account_has_its_own_list(self):
        mine, _ = self.account('a@example.test')
        theirs, their_project = self.account('b@example.test')

        added = self.request('/api/body-types', WHEELED, mine)[1]

        self.assertEqual([t['id'] for t in self.request('/api/state', cookie=theirs)[1]['body_types']], BUILT_IN)
        status = self.request('/api/robots', {'vendor': 'Acme', 'model': 'X1', 'project_id': their_project, 'name': 'Theirs', 'family': added['id']}, theirs)[0]
        self.assertEqual(status, 400)
        self.assertEqual(self.request('/api/body-types/remove', {'body_type_id': added['id']}, theirs)[0], 400)

    def test_page_loads_the_body_type_script_and_style(self):
        page = self.request('/')[1]
        for name in ('body_types.js', 'body_types.css'):
            with self.subTest(name):
                self.assertIn('/' + name, page)
                self.assertEqual(self.request('/' + name)[0], 200)


if __name__ == '__main__':
    unittest.main()
