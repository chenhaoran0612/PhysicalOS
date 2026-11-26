"""Issue #38: 去指定地点 / 巡检 / 无图自由走 from the console, on a simulated quadruped.

Starts a real ``neuxmind_runtime serve`` child with the simulated body (made-up places, a stand-in
wander) and goes through what a user does: find the skill in the gallery, install it, run it with
the values the form starts with, watch it end. Nothing touches real hardware; a real robot runs the
same skills after an on-site confirmation (issue #42, tests/test_skill_run.py) or through an application.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import robot_caps, sim_robots  # noqa: E402
from brainos.service import Platform  # noqa: E402

SKILLS = ('goto_place', 'patrol_places', 'free_walk')


def wait_for(predicate, seconds=60.0, step=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class PlaceSkillsOnASimulatedRobot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-issue38-test-')
        self.app = Platform(self.tmp.name + '/ws')
        self.quad = self.app.add_sim_robot({'kind': 'quadruped'})

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def install(self, example):
        preview = self.app.preview_skill_package({'robot_id': self.quad['id'], 'example': example})
        self.assertTrue(preview['can_install'], preview['compatibility'])
        return self.app.install_skill_package({'robot_id': self.quad['id'], 'example': example,
                                               'request_id': 'install-' + example,
                                               'package_digest': preview['package_digest']})

    def run_to_end(self, skill, parameters, seconds=90.0):
        op = self.app.run_robot_skill({'robot_id': self.quad['id'], 'skill_id': skill, 'request_id': 'run-' + skill,
                                       'parameters': parameters, 'hardware_authorized': True})
        self.assertEqual(op['status'], 'accepted', op)

        def ended():
            run = self.app.robot_live({'robot_id': self.quad['id']}).get('run') or {}
            return run if run.get('id') == op['run_id'] and run.get('status') not in ('queued', 'running') else None
        run = wait_for(ended, seconds)
        self.assertTrue(run, f'{skill} 没有在 {seconds:g} 秒内结束')
        return run

    def test_the_robot_page_explains_the_two_new_capabilities(self):
        detail = self.app.robot_detail({'robot_id': self.quad['id']})
        rows = {row['id']: row for row in detail['capabilities']['rows']}
        for cap_id in ('base.navigate_to_place', 'base.explore'):
            with self.subTest(cap_id=cap_id):
                self.assertEqual(rows[cap_id]['safety'], 'motion')
                self.assertEqual(rows[cap_id]['does'], robot_caps.USER_DOES[cap_id])
        places = next(p for p in rows['base.navigate_to_place']['params'] if p['name'] == 'places')
        self.assertEqual(places['value'], ['出发点', '会议室', '二楼'])

    def test_gallery_offers_them_with_values_to_start_from(self):
        gallery = self.app.skill_examples({'project_id': self.quad['project_id'], 'robot_id': self.quad['id']})
        items = {x['example']: x for x in gallery['items']}
        for name in SKILLS:
            with self.subTest(name=name):
                item = items[name]
                self.assertTrue(item['valid'], item)
                self.assertTrue(item['moves'])
                self.assertTrue(item['note'])
                self.assertEqual(item['preset'], sim_robots.EXAMPLE_PRESETS[name])
                self.assertEqual([c['compatible'] for c in item['compatibility']], [True])
        place = next(p for p in items['goto_place']['skill']['parameters'] if p['name'] == 'place')
        self.assertEqual([o['value'] if isinstance(o, dict) else o for o in place['options']], ['会议室', '二楼'])

    def test_go_to_the_meeting_room(self):
        self.install('goto_place')
        run = self.run_to_end('goto_place', sim_robots.EXAMPLE_PRESETS['goto_place'])
        self.assertEqual(run['status'], 'succeeded', run)
        self.assertEqual(run['result']['place'], '会议室')
        pose = self.app.robot_live({'robot_id': self.quad['id']})['state']['pose']
        self.assertAlmostEqual(pose['x'], 3.0, delta=0.2)
        self.assertAlmostEqual(pose['y'], 1.0, delta=0.2)

    def test_a_place_outside_the_choice_is_refused_by_the_device(self):
        self.install('goto_place')
        op = self.app.run_robot_skill({'robot_id': self.quad['id'], 'skill_id': 'goto_place', 'request_id': 'bad',
                                       'parameters': {'place': '天台'}, 'hardware_authorized': True})
        self.assertEqual((op['status'], op['code'], op['param']), ('rejected', 'param_choice', 'place'))
        self.assertEqual(op['choices'], ['会议室', '二楼'])
        pose = self.app.robot_live({'robot_id': self.quad['id']})['state']['pose']
        self.assertEqual((pose['x'], pose['y']), (0.0, 0.0))

    def test_inspection_round(self):
        self.install('patrol_places')
        run = self.run_to_end('patrol_places', sim_robots.EXAMPLE_PRESETS['patrol_places'], 120.0)
        self.assertEqual(run['status'], 'succeeded', run)
        self.assertEqual(run['result']['visited'], ['会议室', '二楼', '出发点'])

    def test_free_walk_then_stop(self):
        self.install('free_walk')
        op = self.app.run_robot_skill({'robot_id': self.quad['id'], 'skill_id': 'free_walk', 'request_id': 'walk-1',
                                       'parameters': sim_robots.EXAMPLE_PRESETS['free_walk'], 'hardware_authorized': True})
        self.assertEqual(op['status'], 'accepted', op)
        moving = wait_for(lambda: (lambda v: v if abs(v['state']['pose']['x']) > 0.2 else None)(
            self.app.robot_live({'robot_id': self.quad['id']})), 30)
        self.assertTrue(moving, '自由走开始后位置没有变化')
        self.assertEqual(self.app.stop_robot({'robot_id': self.quad['id']})['status'], 'accepted')
        stopped = wait_for(lambda: (lambda v: v if v['run']['status'] != 'running' else None)(
            self.app.robot_live({'robot_id': self.quad['id']})), 15)
        self.assertEqual(stopped['run']['status'], 'stopped')
        self.assertTrue(stopped['estop']['latched'])
        x = stopped['state']['pose']['x']
        time.sleep(1.0)
        self.assertAlmostEqual(self.app.robot_live({'robot_id': self.quad['id']})['state']['pose']['x'], x, places=3)


if __name__ == '__main__':
    unittest.main()
