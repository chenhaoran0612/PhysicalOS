"""Issue #13: hardware problems that software cannot fix, and repairs that run without asking.

1. "默认允许执行": a repair started with it runs the device commands the assistant proposes
   as soon as they pass the product's checks, without a question per command. Without it,
   every command waits for the user as before; it can be switched during a session.
2. A problem software cannot fix is only annotated: one the scan already classes as needing
   on-site work, or one a repair report left as needs_user / not_fixed / pending_reboot.
   It cannot be handed to the assistant until a person marks it as handled.
Uses the SSH fixture, the local Responses fixture and the Codex stand-in, never a paid API.
"""
import queue
import secrets
import time
import unittest
from unittest import mock

from brainos import agent_runtime, hardware_init, repair_policy
from brainos.engineering import ACTIVE
from hardware_fixture import jetson
from test_hardware_init import SCAN, SERIAL, USERMOD, Base, RepairModel
from test_investigation_limits import HeavyUsageRuntime
from test_product import Server

MODEL_KEY = secrets.token_hex(12)       # throwaway key for the local fixture
CAN = 'net-down:can0'                   # the scan says it needs on-site work (bus bitrate)
UNIT = 'unit:nvargus-daemon.service'    # a note the user judges; never blocked by the scan
ISSUES = SERIAL + ['usb-driver:1-4']    # what the scripted repair model reports on


class PolicyBase(Base):
    def setUp(self):
        super().setUp()
        self.policy = self.hw.policy
        self.baseline = self.scan()
        self.stand_ins = []

    def tearDown(self):
        for sid in self.stand_ins:
            self.e.live.pop(sid, None)
        super().tearDown()

    def stand_in(self, sid):
        """A running session, as far as approvals go: the queue its answers are put on."""
        answers = queue.Queue()
        self.e.live[sid] = {'answers': answers}
        self.stand_ins.append(sid)
        return answers

    def blocks(self):
        return {b['issue_id']: b for b in self.p.snapshot()['hardware_blocks'] if b['robot_id'] == self.robot}

    def mark(self, issue_id=CAN, handled=True, **extra):
        return self.policy.mark({'project_id': self.pid, 'robot_id': self.robot, 'issue_id': issue_id, 'handled': handled, **extra})

    def report(self, outcomes, issue_ids=SERIAL):
        """A saved repair report: {issue_id: outcome}."""
        s = self.session(self.baseline, issue_ids=issue_ids)
        results = [{'issue_id': issue_id, 'outcome': outcome, 'actions': '查了驱动和接线：' + issue_id, 'verification': '重新扫描后问题仍在',
                    'evidence_ids': []} for issue_id, outcome in outcomes.items()]
        self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '处理结果', 'results': results, 'followups': []}, None)
        self.store.delete('agent_sessions', s['id'])
        return s


class ScanBlockTests(PolicyBase):
    def test_an_issue_that_needs_on_site_work_is_blocked_from_the_scan(self):
        blocks = self.blocks()
        self.assertEqual(set(blocks), {CAN}, 'software-fixable problems and notes stay selectable')
        self.assertEqual((blocks[CAN]['state'], blocks[CAN]['reason']), ('blocked', 'onsite'))
        self.assertEqual(blocks[CAN]['scan_id'], self.baseline['id'])
        self.assertIsNone(blocks[CAN]['handled_at'])

    def test_only_the_latest_scan_of_a_robot_is_described(self):
        latest = self.scan()
        self.assertEqual({b['scan_id'] for b in self.p.snapshot()['hardware_blocks']}, {latest['id']})

    def test_onsite_rule(self):
        for issue, expected in [({'fixable': 'needs_user', 'severity': 'warning'}, True), ({'fixable': 'needs_user', 'severity': 'error'}, True),
                                ({'fixable': 'needs_user', 'severity': 'info'}, False), ({'fixable': 'auto', 'severity': 'warning'}, False)]:
            with self.subTest(issue=issue):
                self.assertEqual(repair_policy.needs_onsite(issue), expected)


class MarkTests(PolicyBase):
    def test_marking_it_handled_lifts_the_block_and_undoing_restores_it(self):
        result = self.mark()
        self.assertEqual(result['state'], 'handled')
        block = self.blocks()[CAN]
        self.assertEqual((block['state'], block['reason']), ('handled', 'onsite'))
        self.assertTrue(block['handled_at'])
        self.policy.check_selectable(self.robot, self.baseline, [CAN])       # no error: it can be chosen again
        audit = [e for e in self.store.list('events') if e['action'] == 'device.hardware_issue_marked']
        self.assertEqual(audit[0]['details'], {'robot_id': self.robot, 'issue_id': CAN, 'handled': True, 'reason': 'onsite'})
        self.assertEqual(self.mark(handled=False)['state'], 'blocked')
        self.assertEqual(self.blocks()[CAN]['state'], 'blocked')
        with self.assertRaisesRegex(ValueError, '现场处理'):
            self.policy.check_selectable(self.robot, self.baseline, [CAN])

    def test_mark_checks(self):
        for extra in ({'issue_id': 'net-down:wlan9'}, {'issue_id': SERIAL[0]}, {'issue_id': UNIT}, {'issue_id': None}, {'handled': 'yes'},
                      {'handled': None}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.mark(**extra)
        with self.assertRaisesRegex(ValueError, '没有标注'):
            self.mark(handled=False)
        elsewhere = self.p.add_project({'name': 'Other', 'objective': 'Other'})['id']
        with self.assertRaises(ValueError):
            self.mark(project_id=elsewhere)
        self.assertEqual(self.store.list('hardware_issue_marks'), [])

    def test_a_robot_without_a_scan_has_nothing_to_mark(self):
        for scan in self.store.list('hardware_scans'):
            self.store.delete('hardware_scans', scan['id'])
        with self.assertRaisesRegex(ValueError, '扫描'):
            self.mark()


class ReportBlockTests(PolicyBase):
    def test_what_the_assistant_could_not_fix_is_blocked_with_its_explanation(self):
        self.report({SERIAL[0]: 'needs_user', SERIAL[1]: 'pending_reboot'})
        blocks = self.blocks()
        self.assertEqual({k: (v['state'], v['reason']) for k, v in blocks.items()},
                         {CAN: ('blocked', 'onsite'), SERIAL[0]: ('blocked', 'needs_user'), SERIAL[1]: ('blocked', 'pending_reboot')})
        self.assertIn('查了驱动和接线：' + SERIAL[0], blocks[SERIAL[0]]['note'])
        self.assertTrue(blocks[SERIAL[0]]['repair_id'])
        with self.assertRaisesRegex(ValueError, '现场处理'):
            self.policy.check_selectable(self.robot, self.baseline, SERIAL[:1])

    def test_not_fixed_blocks_and_skipped_does_not(self):
        self.report({SERIAL[0]: 'not_fixed', SERIAL[1]: 'skipped'})
        blocks = self.blocks()
        self.assertEqual(blocks[SERIAL[0]]['reason'], 'not_fixed')
        self.assertNotIn(SERIAL[1], blocks)

    def test_the_users_own_request_in_a_chat_is_never_blocked(self):
        s = self.session(self.baseline, issue_ids=[], chat=True, extra_issue_ids=[hardware_init.USER_REQUEST])
        self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '处理结果', 'followups': [], 'results': [
            {'issue_id': hardware_init.USER_REQUEST, 'outcome': 'needs_user', 'actions': '查了', 'verification': '无', 'evidence_ids': []}]}, None)
        self.assertEqual(self.store.list('hardware_issue_marks'), [], 'no mark is kept for something that is not a scan problem')
        self.assertEqual(set(self.blocks()), {CAN})

    def test_a_problem_the_final_rescan_no_longer_shows_is_not_annotated(self):
        s = self.session(self.baseline)
        self.ssh.command_outputs[SCAN] = jetson(fixed=True).encode()        # the serial problems are gone when the repair rescans
        self.hw.tool(s['id'], s, 'hardware_rescan', {}, None)
        results = [{'issue_id': issue_id, 'outcome': 'not_fixed', 'actions': '试过', 'verification': '无', 'evidence_ids': []} for issue_id in SERIAL]
        self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '处理结果', 'results': results, 'followups': []}, None)
        self.assertEqual(self.store.list('hardware_issue_marks'), [], 'gone is gone, whatever the report calls it')
        self.ssh.command_outputs[SCAN] = jetson().encode()
        self.scan()
        self.assertEqual(set(self.blocks()), {CAN}, 'when they come back they start clean')

    def test_an_incomplete_rescan_does_not_talk_the_report_out_of_its_annotation(self):
        s = self.session(self.baseline)
        self.ssh.command_outputs[SCAN] = jetson(fixed=True).split('@@NX-HW:failed_units@@')[0].encode()     # cut before the end marker
        self.hw.tool(s['id'], s, 'hardware_rescan', {}, None)
        results = [{'issue_id': issue_id, 'outcome': 'needs_user', 'actions': '试过', 'verification': '无', 'evidence_ids': []} for issue_id in SERIAL]
        self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '处理结果', 'results': results, 'followups': []}, None)
        self.assertEqual({m['issue_id'] for m in self.store.list('hardware_issue_marks')}, set(SERIAL))

    def test_marked_handled_then_stuck_again_is_blocked_again(self):
        self.report({SERIAL[0]: 'needs_user', SERIAL[1]: 'skipped'})
        self.mark(SERIAL[0])
        self.assertEqual(self.blocks()[SERIAL[0]]['state'], 'handled')
        self.report({SERIAL[0]: 'not_fixed', SERIAL[1]: 'skipped'})
        block = self.blocks()[SERIAL[0]]
        self.assertEqual((block['state'], block['reason'], block['handled_at']), ('blocked', 'not_fixed', None))

    def test_a_problem_that_is_gone_in_a_complete_scan_starts_clean_when_it_comes_back(self):
        self.report({SERIAL[0]: 'needs_user', SERIAL[1]: 'skipped'})
        self.ssh.command_outputs[SCAN] = jetson(fixed=True).split('@@NX-HW:failed_units@@')[0].encode()     # cut before the end marker
        self.assertFalse(self.scan()['complete'])
        self.assertEqual(len(self.store.list('hardware_issue_marks')), 1, 'an incomplete scan proves nothing')
        self.ssh.command_outputs[SCAN] = jetson(fixed=True).encode()
        self.scan()
        self.assertEqual(self.store.list('hardware_issue_marks'), [], 'the serial problem is gone')
        self.ssh.command_outputs[SCAN] = jetson().encode()
        self.scan()
        self.assertEqual(set(self.blocks()), {CAN}, 'back again: selectable, like any software problem')


class HardLimitTests(PolicyBase):
    """With nobody approving, the product's own limits are what stands between the model and the robot."""

    def fix(self, s, command, issue_ids=SERIAL):
        return self.hw.fix_request(s, {'command': command, 'purpose': '修复', 'issue_ids': issue_ids, 'risk': 'low', 'rollback': '撤销',
                                       'needs_root': True, 'timeout_seconds': 30}, self.connection)

    def test_a_line_continuation_does_not_hide_a_forbidden_command(self):
        s = self.session(self.baseline)
        for command in ('re\\\nboot', 'shut\\\ndown -h now', 'ip link set eth0 \\\ndown', 'su\\\ndo modprobe x'):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, '产品拒绝'):
                self.fix(s, command)
        self.assertEqual(self.fix(s, 'apt-get install -y \\\n linux-firmware')['command'], 'apt-get install -y \\\n linux-firmware')

    def test_the_assistant_cannot_change_a_blocked_problem_during_a_repair(self):
        s = self.session(self.baseline)
        with self.assertRaisesRegex(ValueError, '现场处理'):
            self.fix(s, 'ip link set can0 up type can bitrate 500000', issue_ids=[CAN])
        with self.assertRaisesRegex(ValueError, '现场处理'):
            self.fix(s, 'ip link set can0 up type can bitrate 500000', issue_ids=SERIAL[:1] + [CAN])
        self.mark()
        self.assertEqual(self.fix(s, 'ip link set can0 up type can bitrate 500000', issue_ids=[CAN])['issue_ids'], [CAN])


class StartTests(PolicyBase):
    """What starting a repair or a fix chat checks and stores, without running the engine."""

    def setUp(self):
        super().setUp()
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': 'http://127.0.0.1:9/v1', 'api_key': MODEL_KEY})
        self.launched = []
        for patcher in (mock.patch.object(self.e, '_launch', lambda s, message, resume: self.launched.append(s['id'])),
                        mock.patch.object(agent_runtime, 'available', lambda: {'available': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def repair(self, issue_ids=SERIAL, **extra):
        return self.hw.start_repair({'project_id': self.pid, 'robot_id': self.robot, 'scan_id': self.baseline['id'], 'issue_ids': issue_ids,
                                     'model_id': self.model['id'], 'data_authorized': True, 'modify_authorized': True, **extra})

    def chat(self, issue_ids=SERIAL, **extra):
        return self.p.investigation.start_fix({'project_id': self.pid, 'robot_id': self.robot, 'issue_ids': issue_ids, 'message': '',
                                               'model_id': self.model['id'], 'data_authorized': True, 'modify_authorized': True, **extra})

    def free(self, s):
        self.e.update(s['id'], status='completed')

    def test_a_blocked_problem_cannot_be_handed_to_the_assistant(self):
        for start in (self.repair, self.chat):
            with self.subTest(start=start.__name__):
                with self.assertRaisesRegex(ValueError, '现场处理'):
                    start(issue_ids=SERIAL + [CAN])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.store.list('agent_sessions'), [])

    def test_a_problem_marked_as_handled_can_be_handed_over_again(self):
        self.mark()
        s = self.repair(issue_ids=[CAN])
        self.assertEqual(s['hardware']['issue_ids'], [CAN])
        self.free(s)
        self.assertEqual(self.chat(issue_ids=[CAN])['hardware']['issue_ids'], [CAN])

    def test_the_choice_to_run_without_asking_is_stored_as_given(self):
        for start in (self.repair, self.chat):
            for given, stored in (({'auto_approve': True}, True), ({'auto_approve': False}, False), ({}, None)):
                with self.subTest(start=start.__name__, given=given):
                    s = start(**given)
                    self.assertIs(s['hardware']['auto_approve'], stored)
                    self.free(s)

    def test_the_choice_must_be_yes_or_no(self):
        for start in (self.repair, self.chat):
            for bad in ('yes', 1, 0, 'true'):
                with self.subTest(start=start.__name__, bad=bad), self.assertRaises(ValueError):
                    start(auto_approve=bad)
        self.assertEqual(self.launched, [])


class InstructionTests(PolicyBase):
    def test_repairs_are_told_to_fix_in_software_and_only_annotate_the_rest(self):
        text = hardware_init.instructions(self.session(self.baseline), 'DISCOVERY')
        self.assertIn(hardware_init.REPAIR_INSTRUCTIONS, text)
        self.assertIn(repair_policy.SOFTWARE_ONLY, text)
        self.assertNotIn(repair_policy.AUTO_ALLOW, text, 'the user still approves each command')

    def test_a_repair_that_runs_without_asking_is_told_so(self):
        text = hardware_init.instructions(self.session(self.baseline, auto_approve=True), 'DISCOVERY')
        self.assertIn(repair_policy.SOFTWARE_ONLY, text)
        self.assertIn(repair_policy.AUTO_ALLOW, text)

    def test_a_fix_chat_keeps_its_conversation_rules(self):
        text = hardware_init.instructions(self.session(self.baseline, chat=True, auto_approve=True), 'DISCOVERY')
        self.assertIn(hardware_init.CHAT_INSTRUCTIONS, text)
        self.assertIn(repair_policy.AUTO_ALLOW, text)

    def test_other_sessions_are_unchanged(self):
        self.assertEqual(hardware_init.instructions({'mode': 'discovery'}, 'DISCOVERY'), 'DISCOVERY')

    def test_the_built_in_skill_carries_the_same_rule(self):
        skill = hardware_init.load_skill()['SKILL.md']
        self.assertIn('## 软件修不了的问题', skill)
        for outcome in repair_policy.STUCK:
            self.assertIn('`' + outcome + '`', skill.split('## 软件修不了的问题')[1])
        self.assertEqual(hardware_init.skill_info()['version'], '1.2.0', 'the Skill text changed: so does its version')


class QueueTests(PolicyBase):
    """The moment a request for approval is queued, with a stand-in for the running session."""

    def waiting(self, method, request=None, **hardware):
        s = self.session(self.baseline, **hardware)
        payload = {'id': 7, 'method': method, 'request': request or {'command': 'modprobe uvcvideo', 'purpose': '加载相机驱动'}}
        answers = self.stand_in(s['id'])
        self.e._queue_request(s['id'], 'approval', payload)
        self.policy.answer_waiting(s['id'])
        return self.store.get('agent_sessions', s['id']), answers

    def test_device_commands_are_answered_at_once_when_the_user_chose_so(self):
        for method, request, shown in (('device_fix/requestApproval', None, 'modprobe uvcvideo'), ('device_exec/requestApproval', None, 'modprobe uvcvideo')):
            with self.subTest(method=method):
                s, answers = self.waiting(method, request, auto_approve=True)
                self.assertEqual(answers.get_nowait(), (7, {'decision': 'accept'}))
                self.assertIsNone(s['pending_approval'])
                self.assertEqual(s['status'], 'running')
                event = s['events'][-1]
                self.assertEqual(event['kind'], 'approval')
                self.assertIn('默认允许执行', event['message'])
                self.assertIn(shown, event['message'], 'the record says what ran')
                self.assertNotIn('用户批准', event['message'], 'nobody was asked')

    def test_a_very_long_command_is_cut_in_the_record_and_says_so(self):
        s, _ = self.waiting('device_fix/requestApproval', {'command': 'echo ' + 'x' * 3000, 'purpose': '长命令'}, auto_approve=True)
        message = s['events'][-1]['message']
        self.assertLess(len(message), 2000)
        self.assertIn('完整命令见这一步的依据', message)

    def test_without_the_choice_the_command_waits(self):
        for hardware in ({}, {'auto_approve': False}, {'auto_approve': None}):
            with self.subTest(hardware=hardware):
                s, answers = self.waiting('device_fix/requestApproval', **hardware)
                self.assertTrue(answers.empty())
                self.assertEqual((s['status'], s['pending_approval']['id']), ('waiting_input', 7))

    def test_commands_on_the_users_own_computer_always_wait(self):
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/permissions/requestApproval'):
            with self.subTest(method=method):
                s, answers = self.waiting(method, auto_approve=True)
                self.assertTrue(answers.empty())
                self.assertEqual(s['pending_approval']['id'], 7)

    def test_reading_a_new_directory_always_waits(self):
        s, answers = self.waiting('device_request_scope/requestApproval', {'path': '/opt/robot/config', 'purpose': '读取底盘配置'}, auto_approve=True)
        self.assertTrue(answers.empty(), 'a wider read permission is not a repair command')
        self.assertEqual(s['pending_approval']['id'], 7)

    def test_ordinary_repair_commands_run_unasked(self):
        for command in ('modprobe uvcvideo', 'DEBIAN_FRONTEND=noninteractive apt-get install -y linux-firmware', 'usermod -aG dialout robot',
                        'nmcli connection up "Wired connection 1"', 'systemctl restart nvargus-daemon', 'udevadm control --reload-rules',
                        "echo 'KERNEL==\"ttyUSB*\", MODE=\"0660\", GROUP=\"dialout\"' | tee /etc/udev/rules.d/99-serial.rules",
                        'ip link set can0 up type can bitrate 500000', 'journalctl -u NetworkManager -n 80 --no-pager', 'lsmod', 'cat /etc/os-release',
                        'ls /lib/firmware/iwlwifi-cc-a0-72.ucode', 'dmesg | tail -n 50', 'python3 --version', 'python3 -m pip install pyserial',
                        "find /lib/firmware -name 'iwlwifi*'", 'rm -f /etc/udev/rules.d/99-old.rules', 'chmod 660 /dev/ttyUSB0', 'usermod -aG dialout $USER',
                        'ssh-keygen -l -f /etc/ssh/ssh_host_ed25519_key.pub'):
            with self.subTest(command=command):
                self.assertEqual(repair_policy.ask_first({'command': command}), '')

    def test_commands_the_product_cannot_judge_from_their_text_wait_for_a_person(self):
        for command in ('apt-get install -y linux-modules-extra-$(uname -r)', 'echo `uname -r`', 'echo cmVib290 | base64 -d', 'modprobe \\\nuvcvideo',
                        'x=re; y=boot; echo done',
                        'curl -o /tmp/driver.deb https://example.com/driver.deb', 'wget https://example.com/x', 'scp x.ko robot@10.0.0.9:/tmp/', 'nc 10.0.0.9 80',
                        'git clone https://example.com/vendor/driver.git',
                        'bash /tmp/install.sh', 'sh install.sh', './install.sh', '/tmp/fix', 'cd /tmp && ./fix', 'DEBUG=1 ./install.sh', 'python3 setup.py install',
                        'echo modprobe uvcvideo | sh', 'cat steps.txt | bash',
                        'rm -rf /var/lib/robot/cache', 'rm -fr /opt/vendor', 'chmod -R 777 /opt/robot', 'chown -R robot /opt/robot',
                        'find /var/log -name "*.gz" -delete', "find / -name '*.ko' -exec rm {} +",
                        'crontab -l', 'echo "@reboot x" >> /etc/cron.d/fix', 'echo "robot ALL=(ALL) NOPASSWD:ALL" | tee /etc/sudoers.d/robot',
                        'cat ~/.ssh/id_ed25519', 'cat /etc/shadow', 'cat ~/.netrc', 'cat /home/robot/.aws/credentials', 'cp server.pem /tmp/'):
            with self.subTest(command=command):
                self.assertTrue(repair_policy.ask_first({'command': command}), 'ran unasked')
        for quoted in ("'cu'rl -o /tmp/x https://example.com/x", 'r"m" -rf /var/lib/robot'):
            with self.subTest(quoted=quoted):
                self.assertTrue(repair_policy.ask_first({'command': quoted}), 'quotes hid it')

    def test_a_command_held_for_a_person_says_why_once_and_runs_when_approved(self):
        s, answers = self.waiting('device_fix/requestApproval', {'command': 'curl -o /tmp/driver.deb https://example.com/driver.deb', 'purpose': '下载驱动'},
                                  auto_approve=True)
        self.assertTrue(answers.empty())
        self.assertEqual((s['status'], s['pending_approval']['id']), ('waiting_input', 7))
        self.assertEqual((s['hardware']['auto_hold']['id'], s['hardware']['auto_hold']['command']), (7, 'curl -o /tmp/driver.deb https://example.com/driver.deb'))
        self.assertIn('网络', s['hardware']['auto_hold']['reason'])
        holds = [e for e in s['events'] if '等你批准' in e['message']]
        self.assertEqual(len(holds), 1)
        self.policy.answer_waiting(s['id'])         # looked at again: still held, said only once
        self.policy.set_auto({'project_id': self.pid, 'session_id': s['id'], 'enabled': True})      # nor does switching on wave it through
        current = self.store.get('agent_sessions', s['id'])
        self.assertEqual(len([e for e in current['events'] if '等你批准' in e['message']]), 1)
        self.assertTrue(answers.empty())
        self.e.approve({'session_id': s['id'], 'approval_id': 7, 'decision': 'accept'})
        self.assertEqual(answers.get_nowait(), (7, {'decision': 'accept'}))
        # A later run numbers its requests from the start again: another command with the same number says its own reason.
        self.e._queue_request(s['id'], 'approval', {'id': 7, 'method': 'device_exec/requestApproval', 'request': {'command': 'cat /etc/shadow', 'purpose': '看账号'}})
        self.policy.answer_waiting(s['id'])
        again = self.store.get('agent_sessions', s['id'])
        self.assertIn('凭据', again['hardware']['auto_hold']['reason'])
        self.assertEqual(len([e for e in again['events'] if '等你批准' in e['message']]), 2)

    def test_after_many_commands_in_a_row_it_asks_again_until_the_user_says_go_on(self):
        s = self.session(self.baseline, auto_approve=True)
        answers = self.stand_in(s['id'])
        def propose(request_id):
            self.e._queue_request(s['id'], 'approval', {'id': request_id, 'method': 'device_exec/requestApproval', 'request': {'command': 'lsmod', 'purpose': '看模块'}})
            self.policy.answer_waiting(s['id'])
        with mock.patch.object(repair_policy, 'AUTO_RUN_LIMIT', 3):
            for request_id in (1, 2, 3):
                propose(request_id)
            self.assertEqual([answers.get_nowait()[0] for _ in range(3)], [1, 2, 3])
            propose(4)
            self.assertTrue(answers.empty(), 'the fourth in a row waits')
            held = self.store.get('agent_sessions', s['id'])
            self.assertEqual(held['pending_approval']['id'], 4)
            self.assertIn('3 条', held['hardware']['auto_hold']['reason'])
            self.policy.set_auto({'project_id': self.pid, 'session_id': s['id'], 'enabled': True})      # the user says go on
            self.assertEqual(answers.get_nowait(), (4, {'decision': 'accept'}))
            propose(5)
            self.assertEqual(answers.get_nowait(), (5, {'decision': 'accept'}), 'and the count starts again')

    def test_only_repair_sessions(self):
        s = self.session(self.baseline, auto_approve=True)
        self.store.put('agent_sessions', {**s, 'mode': 'discovery'})
        answers = self.stand_in(s['id'])
        self.e._queue_request(s['id'], 'approval', {'id': 7, 'method': 'device_exec/requestApproval', 'request': {'command': 'lsmod', 'purpose': '看模块'}})
        self.policy.answer_waiting(s['id'])
        self.assertTrue(answers.empty(), 'an investigation keeps asking')

    def test_a_device_command_behind_the_users_own_decision_runs_once_that_is_made(self):
        s = self.session(self.baseline, auto_approve=True)
        answers = self.stand_in(s['id'])
        self.e._queue_request(s['id'], 'approval', {'id': 1, 'method': 'item/commandExecution/requestApproval', 'request': {'command': 'ls'}})
        self.e._queue_request(s['id'], 'approval', {'id': 2, 'method': 'device_fix/requestApproval', 'request': {'command': 'modprobe uvcvideo', 'purpose': '加载相机驱动'}})
        self.policy.answer_waiting(s['id'])
        self.assertTrue(answers.empty(), 'the command on the user\'s computer is theirs to decide')
        self.e.approve({'session_id': s['id'], 'approval_id': 1, 'decision': 'decline'})
        self.assertEqual([answers.get_nowait(), answers.get_nowait()], [(1, {'decision': 'decline'}), (2, {'decision': 'accept'})])
        current = self.store.get('agent_sessions', s['id'])
        self.assertEqual((current['status'], current['pending_approval']), ('running', None), 'nothing is left waiting')

    def test_a_device_command_behind_a_question_runs_once_it_is_answered(self):
        s = self.session(self.baseline, auto_approve=True)
        answers = self.stand_in(s['id'])
        self.e._queue_request(s['id'], 'question', {'id': 1, 'questions': [{'id': 'q', 'question': '波特率是多少？'}]})
        self.e._queue_request(s['id'], 'approval', {'id': 2, 'method': 'device_exec/requestApproval', 'request': {'command': 'ip -details link show can0', 'purpose': '看 CAN 参数'}})
        self.policy.answer_waiting(s['id'])
        self.assertTrue(answers.empty())
        self.e.answer({'session_id': s['id'], 'question_id': 1, 'answers': {'q': '500000'}})
        self.assertEqual(answers.get_nowait()[0], 1)
        self.assertEqual(answers.get_nowait(), (2, {'decision': 'accept'}))


class SwitchTests(PolicyBase):
    def switch(self, s, enabled, **extra):
        return self.policy.set_auto({'project_id': self.pid, 'session_id': s['id'], 'enabled': enabled, **extra})

    def test_switching_is_recorded_on_the_session(self):
        s = self.session(self.baseline)
        on = self.switch(s, True)
        self.assertIs(on['hardware']['auto_approve'], True)
        self.assertIn('默认允许执行', on['events'][-1]['message'])
        off = self.switch(s, False)
        self.assertIs(off['hardware']['auto_approve'], False)
        self.assertIn('逐条批准', off['events'][-1]['message'])
        audit = [e['details'] for e in self.store.list('events') if e['action'] == 'device.hardware_auto_approve_changed']
        self.assertEqual(sorted(a['enabled'] for a in audit), [False, True])

    def test_switching_on_answers_what_is_already_waiting_but_not_local_commands(self):
        s = self.session(self.baseline, auto_approve=False)
        answers = self.stand_in(s['id'])
        for request_id, method in ((1, 'device_fix/requestApproval'), (2, 'device_exec/requestApproval'), (3, 'item/commandExecution/requestApproval'),
                                   (4, 'device_fix/requestApproval')):
            self.e._queue_request(s['id'], 'approval', {'id': request_id, 'method': method, 'request': {'command': 'lsmod', 'purpose': '试'}})
        current = self.switch(s, True)
        self.assertEqual([answers.get_nowait() for _ in range(2)], [(1, {'decision': 'accept'}), (2, {'decision': 'accept'})])
        self.assertTrue(answers.empty(), 'the local command is still the user\'s to decide, and what is behind it waits its turn')
        self.assertEqual((current['status'], current['pending_approval']['id']), ('waiting_input', 3))

    def test_switch_checks(self):
        s = self.session(self.baseline)
        for bad in ('yes', 1, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.switch(s, bad)
        elsewhere = self.p.add_project({'name': 'Other', 'objective': 'Other'})['id']
        with self.assertRaises(ValueError):
            self.switch(s, True, project_id=elsewhere)
        self.store.put('agent_sessions', {**self.store.get('agent_sessions', s['id']), 'mode': 'discovery'})
        with self.assertRaisesRegex(ValueError, '修复'):
            self.switch(s, True)


class RunTests(PolicyBase):
    """Whole repairs through the engine: scan file, fix, refused reboot, rescan, report."""

    def setUp(self):
        super().setUp()
        RepairModel.requests, RepairModel.ssh = [], self.ssh
        self.addCleanup(setattr, RepairModel, 'ssh', None)          # shared class state: never leave a closed fixture behind
        self.addCleanup(setattr, RepairModel, 'requests', [])
        self.model_server = Server(RepairModel)
        self.addCleanup(self.model_server.close)
        self.model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                       'base_url': self.model_server.url + '/v1', 'api_key': MODEL_KEY})
        available = agent_runtime.available
        for patcher in (mock.patch.object(agent_runtime, 'AgentProcess', HeavyUsageRuntime),
                        mock.patch.object(agent_runtime, 'available', lambda: {**available(), 'available': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, **extra):
        return self.hw.start_repair({'project_id': self.pid, 'robot_id': self.robot, 'scan_id': self.baseline['id'], 'issue_ids': ISSUES,
                                     'model_id': self.model['id'], 'data_authorized': True, 'modify_authorized': True, **extra})

    def poll(self, sid, predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = self.store.get('agent_sessions', sid)
            if predicate(s):
                return s
            time.sleep(.05)
        self.fail('Timed out in status ' + s['status'] + ': ' + str(s.get('error')))

    def test_a_repair_allowed_by_default_finishes_without_anyone_approving(self):
        sid = self.start(auto_approve=True)['id']
        s = self.poll(sid, lambda s: s['status'] not in ACTIVE or bool(s.get('pending_approval')))
        self.assertEqual(s['status'], 'completed', [event['message'] for event in s['events']][-4:])
        self.assertIn(USERMOD, self.ssh.commands, 'the fix ran')
        messages = [event['message'] for event in s['events']]
        self.assertTrue(any('默认允许执行' in m and 'usermod -aG dialout robot' in m for m in messages), messages)
        self.assertFalse(any('用户批准' in m for m in messages), 'nobody approved anything')
        self.assertNotIn('reboot', ' '.join(self.ssh.commands), 'the product still refuses what it always refused')
        self.assertTrue(any('不允许重启' in event['message'] for event in s['events'] if event['kind'] == 'tool_error'))
        report = self.store.get('hardware_repairs', s['repair_report_id'])
        self.assertEqual({r['issue_id']: r['outcome'] for r in report['results']},
                         {SERIAL[0]: 'fixed', SERIAL[1]: 'fixed', 'usb-driver:1-4': 'not_reproduced'})
        self.assertEqual(set(self.blocks()), {CAN}, 'what was fixed leaves no annotation')

    def test_a_repair_that_asks_goes_on_alone_once_the_user_switches(self):
        sid = self.start(auto_approve=False)['id']
        waiting = self.poll(sid, lambda s: bool(s.get('pending_approval')) or s['status'] not in ACTIVE)
        self.assertEqual(waiting['pending_approval']['method'], 'device_fix/requestApproval')
        self.assertNotIn(USERMOD, self.ssh.commands, 'nothing runs before the user decides')
        self.policy.set_auto({'project_id': self.pid, 'session_id': sid, 'enabled': True})
        s = self.poll(sid, lambda s: s['status'] not in ACTIVE)
        self.assertEqual(s['status'], 'completed', s.get('error'))
        self.assertIn(USERMOD, self.ssh.commands)
        self.assertTrue(s['repair_report_id'])


if __name__ == '__main__':
    unittest.main()
