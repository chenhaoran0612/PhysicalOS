"""Issue #24 × issue #15: a jump host that refuses a login counts for every robot behind it, also when
the route has several jump hosts. The record of a connection names its jump hosts in ``jumps`` (#15);
the heartbeat (#24) was written against the single ``jump`` of 0.19.0 and is told apart here by level.

The SSH case with one jump host is tests/test_heartbeat_limits.py
(``test_a_jump_host_that_refuses_is_not_asked_once_for_every_robot_behind_it``); these are the records
alone, so no machine is needed.
"""
import tempfile
import unittest

from brainos import heartbeat
from brainos.service import Platform

FIRST = {'host': 'jump-1.example', 'port': 22, 'username': 'ops'}
SECOND = {'host': 'jump-2.example', 'port': 2222, 'username': 'relay'}
OTHER = {'host': 'jump-3.example', 'port': 22, 'username': 'relay'}
SAVED = '2026-11-19T00:00:00+00:00'


def connection(key, *jumps):
    record = {'id': key, 'robot_id': 'robot-' + key, 'host': 'robot.example', 'port': 22,
              'username': 'robot', 'created_at': SAVED}
    return {**record, 'jumps': [dict(jump) for jump in jumps]} if jumps else record


class MultiHopRefusalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.platform = Platform(tmp.name)
        self.addCleanup(self.platform.close)
        self.hb = self.platform.heartbeat
        self.hb.stop_timer()

    def refuse(self, at, level):
        error = {'hop': 'jump', 'level': level, 'reason': 'auth', 'message': 'refused'}
        with self.hb._guard:
            self.hb._refusal(at, error, {}, now=1000.0)

    def in_the_way(self, of):
        return [who for who, _ in self.hb._standing(of)]

    def test_each_jump_host_is_named_with_the_hosts_before_it(self):
        keys = heartbeat._jump_addresses(connection('a', FIRST, SECOND))
        self.assertEqual(keys, [('jump', 'jump-1.example', 22, 'ops'), ('jump', 'jump-1.example', 22, 'ops', 'jump-2.example', 2222, 'relay')])
        self.assertEqual(heartbeat._jump_addresses(connection('b')), [])

    def test_a_refusal_at_the_second_level_counts_for_every_robot_behind_that_host(self):
        refused, behind_it, only_first, direct = (connection('a', FIRST, SECOND), connection('b', FIRST, SECOND),
                                                 connection('c', FIRST, OTHER), connection('d'))
        self.refuse(refused, level=2)
        second = heartbeat._jump_addresses(refused)[1]
        self.assertEqual(self.in_the_way(behind_it), [second])
        self.assertEqual(self.in_the_way(only_first), [], 'a robot behind another second jump host was held back')
        self.assertEqual(self.in_the_way(direct), [])

    def test_a_refusal_at_the_first_level_counts_for_every_route_through_it(self):
        refused, longer = connection('a', FIRST), connection('b', FIRST, SECOND)
        self.refuse(refused, level=1)
        self.assertEqual(self.in_the_way(longer), [('jump', 'jump-1.example', 22, 'ops')])

    def test_a_record_of_0_19_0_with_a_single_jump_is_still_read(self):
        old = {**connection('a'), 'jump': dict(FIRST)}
        self.refuse(old, level=None)                     # 0.19.0 errors name no level
        self.assertEqual(self.in_the_way({**connection('b'), 'jump': dict(FIRST)}), [('jump', 'jump-1.example', 22, 'ops')])

    def test_checking_by_hand_clears_the_refusals_of_every_level_of_its_route(self):
        route = connection('a', FIRST, SECOND)
        self.refuse(route, level=1)
        self.refuse(route, level=2)
        self.assertIsNotNone(self.hb._may_login(route, by_hand=True))
        self.assertEqual(self.in_the_way(route), [])


if __name__ == '__main__':
    unittest.main()
