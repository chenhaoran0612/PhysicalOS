"""Issue #51 (console side): a material belongs to a brand and model of robot.

Whose a material is, how the materials kept for single devices are moved once per robot, what the pages
are told. The cloud is a stand-in that records what it is asked; the cloud's own rules are in
cloud/tests/test_library_product.py and test_library_model.py."""
import tempfile
import unittest
from unittest import mock

from brainos import model_library
from brainos.cloud_account import CloudError
from brainos.errors import CodedError
from brainos.service import Platform

ACME = {'vendor': 'Acme Robotics', 'model': 'Go-7'}


class Cloud:
    """``library._call`` and ``library.list`` of a signed-in workspace."""

    def __init__(self):
        self.calls, self.materials, self.fail, self.kept = [], [], None, []

    def _call(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if self.fail:
            raise self.fail
        if path.endswith('/rebind'):
            return {'moved': 2, 'merged': 1, 'kept': self.kept, 'device_id': payload['device_id']}
        if path == '/library/products':          # issue #77: the brands and models with materials (none here)
            return {'products': []}
        return {'id': path.split('/')[3], 'filename': 'arm.urdf', **payload}

    def list(self, p=None):
        p = dict(p or {})
        self.calls.append(('LIST', p))
        rows = [m for m in self.materials if all(str(m.get(k, '')).casefold() == str(v).casefold()
                                                 for k, v in p.items() if k in ('vendor', 'model', 'device_id', 'status', 'kind'))]
        return {'materials': rows, 'total': len(rows)}

    def overview(self, p=None):
        return {'account': {'logged_in': True}, 'formats': {'model': {'extensions': ['.urdf']}}, 'materials': [], 'total': 0}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Platform(tmp.name + '/platform')
        self.addCleanup(self.app.close)
        self.pid = self.app.add_project({'name': 'Site', 'objective': 'Inspect'})['id']
        self.cloud = self.app.library = Cloud()
        self.signed_in = True
        patcher = mock.patch.object(self.app.cloud, 'summary', lambda: {'logged_in': self.signed_in})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lib = self.app.model_library

    def robot(self, name, **product):
        return self.app.add_robot({'project_id': self.pid, 'name': name, 'family': 'quadruped', **product})

    def rebinds(self):
        return [payload for method, path, payload in (c for c in self.cloud.calls if len(c) == 3) if path.endswith('/rebind')]


class WhoseAMaterialIs(Case):
    def test_a_robot_uses_the_materials_of_its_brand_and_model_whatever_the_spelling(self):
        robot = self.app.store.get('robots', self.robot('一号', **ACME)['id'])
        for labels, mine in (({'vendor': 'acme  robotics', 'model': 'GO-7'}, True), ({'vendor': 'Acme Robotics', 'model': 'Go-8'}, False),
                             ({'vendor': 'Acme Robotics'}, False), ({'vendor': 'Acme Robotics', 'model': 7}, False),
                             ({**ACME, 'kind': 'bundle'}, False),
                             ({**ACME, 'device_id': 'robot-other'}, False),                    # still bound to another device
                             ({'vendor': 'typed', 'model': 'x', 'device_id': robot['id']}, True)):     # still bound to this one
            with self.subTest(labels=labels):
                self.assertEqual(model_library.owned(labels, robot), mine)
        nameless = self.app.store.get('robots', self.robot('无名')['id'])
        self.assertFalse(model_library.owned({**ACME}, nameless))
        self.assertTrue(model_library.owned({'device_id': nameless['id'], **ACME}, nameless))
        self.assertFalse(model_library.owned(None, robot))

    def test_the_listing_of_a_robot_asks_for_both_and_trusts_neither_answer(self):
        robot = self.app.store.get('robots', self.robot('一号', **ACME)['id'])
        self.cloud.materials = [{'id': 'mat_a', **ACME}, {'id': 'mat_b', 'vendor': 'typed', 'model': 'x', 'device_id': robot['id']},
                                {'id': 'mat_c', 'vendor': 'Other', 'model': 'Go-7'}]
        self.cloud.list = lambda p=None: {'materials': list(self.cloud.materials), 'total': 3}      # a cloud that ignores filters
        rows = model_library.listing(self.cloud, robot)
        self.assertEqual([m['id'] for m in rows['materials']], ['mat_a', 'mat_b'])


    def test_what_the_cloud_lists_for_others_is_not_counted_as_missing(self):
        """Review, 2026-11-26: the totals of both questions were added up, so a repair was told it had not seen everything."""
        robot = self.app.store.get('robots', self.robot('一号', **ACME)['id'])
        self.cloud.materials = [{'id': 'mat_a', **ACME, 'device_id': robot['id']}, {'id': 'mat_x', **ACME, 'device_id': 'robot-other'}]
        self.cloud.list = lambda p=None: {'materials': list(self.cloud.materials), 'total': 2}
        rows = model_library.listing(self.cloud, robot, limit=20)
        self.assertEqual(([m['id'] for m in rows['materials']], rows['total']), (['mat_a'], 1))
        self.assertEqual(model_library.listing(self.cloud, robot, limit=2)['total'], 2)         # a full page: there may be more


class OldMaterialsMove(Case):
    def test_each_robot_with_a_brand_and_model_is_told_to_the_cloud_once(self):
        first, second = self.robot('一号', **ACME), self.robot('二号', vendor='acme robotics', model='go-7')
        self.robot('无名')
        moved = self.lib.sync()
        by_id = lambda rows, key: sorted(rows, key=lambda row: row[key])       # noqa: E731
        self.assertEqual(by_id(self.rebinds(), 'device_id'),
                         by_id([{'device_id': first['id'], **ACME},
                                {'device_id': second['id'], 'vendor': 'acme robotics', 'model': 'go-7'}], 'device_id'))
        self.assertEqual(by_id(moved, 'robot_id'), by_id([{'robot_id': first['id'], 'moved': 2, 'merged': 1, 'kept': 0},
                                                          {'robot_id': second['id'], 'moved': 2, 'merged': 1, 'kept': 0}], 'robot_id'))
        self.assertEqual([e['details']['moved'] for e in self.app.store.list('events') if e['action'] == 'library.rebound'], [2, 2])
        self.assertEqual(self.lib.sync(), [])
        self.assertEqual(len(self.rebinds()), 2)                               # nothing is asked again

    def test_a_new_brand_and_model_is_told_again_and_a_failure_is_tried_the_next_time(self):
        robot = self.robot('一号', **ACME)
        self.cloud.fail = CloudError('无法连接云平台', code='cloud_unreachable')
        self.assertEqual(self.lib.sync(), [])
        self.cloud.fail = None
        self.assertEqual(len(self.lib.sync()), 1)
        self.app.robot_models.assign({'project_id': self.pid, 'robot_id': robot['id'], 'vendor': 'Acme Robotics', 'model': 'Go-8'})
        self.lib.sync()
        self.assertEqual([p['model'] for p in self.rebinds()], ['Go-7', 'Go-7', 'Go-8'])

    def test_a_cloud_that_did_not_move_everything_is_asked_again(self):
        self.robot('一号', **ACME)
        real = self.cloud._call
        self.cloud._call = lambda *a, **k: {**real(*a, **k), 'more': True}
        self.lib.sync()
        self.lib.sync()
        self.assertEqual(len(self.rebinds()), 2)
        self.cloud._call = real
        self.lib.sync()
        self.lib.sync()
        self.assertEqual(len(self.rebinds()), 3)

    def test_without_a_cloud_sign_in_and_for_simulated_robots_nothing_is_asked(self):
        self.robot('一号', **ACME)
        self.signed_in = False
        self.assertEqual(self.lib.sync(), [])
        self.assertEqual(self.cloud.calls, [])
        self.signed_in = True
        robot = self.app.store.get('robots', self.robot('仿真', vendor='neuxmind-sim', model='sim-quadruped')['id'])
        self.app.store.put('robots', {**robot, 'sim': {'kind': 'quadruped'}})
        self.lib.sync()
        self.assertEqual([p['vendor'] for p in self.rebinds()], ['Acme Robotics'])


class WhatThePagesAreTold(Case):
    def test_the_overview_names_the_brands_and_models_to_choose_from(self):
        self.robot('一号', **ACME)
        self.robot('二号', vendor='ACME ROBOTICS', model='go-7')
        self.robot('三号', vendor='Other', model='Arm-1')
        self.robot('无名')
        view = self.lib.overview({})
        self.assertEqual(view['products'], [{**ACME, 'family': 'quadruped', 'robots': ['一号', '二号']},
                                            {'vendor': 'Other', 'model': 'Arm-1', 'family': 'quadruped', 'robots': ['三号']}])
        self.assertEqual((len(view['rebound']), view['formats']['model']['extensions']), (3, ['.urdf']))

    def test_a_robots_materials_are_its_brand_and_models_and_say_who_shares_them(self):
        first, second = self.robot('一号', **ACME), self.robot('二号', **ACME)
        self.robot('三号', vendor='Other', model='Arm-1')
        self.cloud.materials = [{'id': 'mat_a', **ACME, 'status': 'usable'}, {'id': 'mat_c', 'vendor': 'Other', 'model': 'Arm-1'}]
        view = self.lib.robot({'project_id': self.pid, 'robot_id': first['id']})
        self.assertEqual((view['product'], [m['id'] for m in view['materials']], view['shared_with']), (ACME, ['mat_a'], ['二号']))
        nameless = self.robot('无名')
        view = self.lib.robot({'project_id': self.pid, 'robot_id': nameless['id']})
        self.assertEqual((view['product'], view['materials'], view['shared_with']), (None, [], []))
        with self.assertRaises(ValueError):
            self.lib.robot({'project_id': self.app.add_project({'name': '别的', 'objective': 'x'})['id'], 'robot_id': second['id']})

    def test_one_material_is_moved_to_the_brand_and_model_a_person_names(self):
        moved = self.lib.move({'material_id': 'mat_' + 'a' * 20, 'vendor': ' Acme  Robotics ', 'model': 'Go-7'})
        self.assertEqual(self.cloud.calls[-1], ('PUT', '/library/materials/mat_' + 'a' * 20 + '/product', ACME))
        self.assertEqual((moved['vendor'], moved['model']), ('Acme Robotics', 'Go-7'))
        for bad in ({'material_id': 'mat_' + 'a' * 20}, {'material_id': 'mat_' + 'a' * 20, 'vendor': 'Acme'}):
            with self.assertRaises(CodedError):
                self.lib.move(bad)
        with self.assertRaises(ValueError):
            self.lib.move({'material_id': '../admin', **ACME})


if __name__ == '__main__':
    unittest.main()
