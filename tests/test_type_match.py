"""Issue #58: the body types 「添加机器人」 offers are the types of the simulation profiles.

The list itself is checked in tests/test_body_types.py. Here: what reads a robot's type and needs the
value of the standard (or a simulated body) still gets one for the three new types, and a simulated
robot of a profile is of the profile's type.
"""
import unittest

import test_mujoco_sim
import test_sim_profiles
from brainos import adaptation, body_types, robot_scope, runtime_install, runtime_install_plan, sim_profiles


class Consumers(unittest.TestCase):
    def test_a_wheel_legged_robot_gets_the_simulated_quadruped_when_the_management_layer_is_installed(self):
        self.assertEqual(runtime_install_plan.ADAPTERS['wheel_legged'], runtime_install_plan.ADAPTERS['quadruped'])
        self.assertIs(runtime_install.ADAPTERS, runtime_install_plan.ADAPTERS)

    def test_types_without_a_simulated_body_are_named_in_the_refusal_by_what_can_be_chosen(self):
        self.assertEqual(set(runtime_install_plan.ADAPTERS), {'quadruped', 'wheel_legged', 'manipulator'})
        for name in ('四足机械狗', '轮足机器狗', '机械臂'):
            self.assertIn(name, runtime_install.BODY)

    def test_an_adapter_draft_names_the_standard_value_of_the_type(self):
        def family(type_id):
            mapping = {'capabilities': []}
            return adaptation.scaffold_spec({'definition': {}}, {'id': 'r', 'family': type_id}, mapping, [], {})['family']
        self.assertEqual([family(t) for t in ('wheel_legged', 'biped', 'other', 'quadruped', 'manipulator', 'mobile', 'bodytype-1')],
                         ['quadruped', 'humanoid', 'custom', 'quadruped', 'manipulator', 'mobile', 'custom'])

    def test_every_built_in_type_is_a_profile_type(self):
        self.assertEqual({t['id'] for t in body_types.built_in()}, set(sim_profiles.TYPE_BY_ID))


@unittest.skipUnless(test_mujoco_sim.HAVE_MUJOCO, 'needs the mujoco package')
class WheelLeggedApplication(test_mujoco_sim.Case):
    """An application whose robot is wheel-legged is simulated as the quadruped body it moves like."""
    family = 'wheel_legged'

    def test_the_simulation_takes_the_standard_value_of_the_robots_type(self):
        self.model()
        view = self.sim.overview(self.ask)
        self.assertEqual((view['problem'], view['family']), (None, 'quadruped'))


@unittest.skipUnless(test_sim_profiles.HAVE_MUJOCO, 'needs the mujoco package')
class SimulatedRobotType(test_sim_profiles.Case):
    def add(self, profile_id):
        return self.app.add_sim_robot({'profile_id': profile_id})

    def test_a_simulated_robot_is_of_its_profiles_type(self):
        robot = self.add('acme-dog')
        self.assertEqual(self.app.store.get('robots', robot['id'])['family'], 'wheel_legged')
        config = (self.app.sims.root / robot['id'] / 'mujoco.json').read_text()
        self.assertIn('"family": "quadruped"', config)          # the simulation itself keeps the standard value

    def test_simulated_robots_added_before_get_their_profiles_type_and_others_are_left(self):
        robot = self.add('acme-dog')
        store = self.app.store
        store.put('robots', {**store.get('robots', robot['id']), 'family': 'quadruped'}, owner=True)     # as 0.21.14 saved it
        store.put('robots', {'id': 'robot-real', 'project_id': robot_scope.HOME, 'name': 'Real', 'family': 'quadruped',
                             'endpoint': '', 'token': 't', 'status': 'unconnected', 'capabilities': []})
        self.assertEqual(self.app.sim_profiles.align_types(), [robot['id']])
        self.assertEqual(store.get('robots', robot['id'])['family'], 'wheel_legged')
        self.assertEqual(store.get('robots', 'robot-real')['family'], 'quadruped')
        self.assertEqual(self.app.sim_profiles.align_types(), [])

