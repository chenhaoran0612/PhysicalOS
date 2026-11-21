"""Issues #11 and #13 together: deleting a robot also deletes the marks kept about its hardware problems.

Issue #13 keeps a mark per problem that software cannot fix (``hardware_issue_marks``): that a person
handled it on site, or that a repair report left it unresolved, with the assistant's note. Issue #11
deletes what this computer keeps about a robot and was written before these marks existed.

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
from brainos.repair_policy import MARKS
from hardware_fixture import jetson
from ssh_fixture import SSHFixture
from test_hardware_init import SCAN, SERIAL
from test_repair_policy import CAN, PolicyBase


class MarksOfADeletedRobotTests(PolicyBase):
    def marks_of(self, robot_id):
        return [m for m in self.store.list(MARKS) if m['robot_id'] == robot_id]

    def remove(self, robot_id):
        return self.p.robot_remove.remove({'project_id': self.pid, 'robot_id': robot_id})

    def second_robot(self):
        """Another machine with the same problems, scanned, its bus problem marked as handled on site."""
        ssh = SSHFixture(command_outputs={SCAN: jetson().encode()})
        self.addCleanup(ssh.close)
        check = self.e.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': ssh.port, 'username': 'robot'})
        connection = self.e.connect({'project_id': self.pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
                                     'auth': 'password', 'password': 'fixture-password', 'name': 'Second robot', 'family': 'quadruped'})
        self.hw.scan({'project_id': self.pid, 'robot_id': connection['robot_id']})
        self.policy.mark({'project_id': self.pid, 'robot_id': connection['robot_id'], 'issue_id': CAN, 'handled': True})
        return connection['robot_id']

    def test_a_mark_made_by_a_person_goes_with_the_robot(self):
        self.mark(CAN)
        self.assertEqual(len(self.marks_of(self.robot)), 1)

        result = self.remove(self.robot)

        self.assertEqual(self.marks_of(self.robot), [])
        self.assertEqual(result['deleted'].get(MARKS), 1, 'the deletion says how many marks went')

    def test_what_a_repair_report_left_unresolved_goes_with_the_robot(self):
        self.report({SERIAL[0]: 'needs_user'}, issue_ids=SERIAL[:1])
        notes = [m['note'] for m in self.marks_of(self.robot)]
        self.assertTrue(notes and all(notes), 'the report left a mark that carries the assistant\'s note')

        self.remove(self.robot)

        self.assertEqual(self.marks_of(self.robot), [], 'the assistant\'s notes about the machine do not stay behind')

    def test_the_marks_of_another_robot_stay(self):
        other = self.second_robot()
        self.mark(CAN)

        self.remove(self.robot)

        self.assertEqual([m['issue_id'] for m in self.marks_of(other)], [CAN])
        blocks = {b['issue_id']: b['state'] for b in self.p.snapshot()['hardware_blocks'] if b['robot_id'] == other}
        self.assertEqual(blocks.get(CAN), 'handled', 'the other robot\'s problem is still marked as handled')
