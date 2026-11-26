"""Issue #47: a robot's card reported faults that were none.

The rule under test: a finding is a **fault** (severity ``error``) only when the scan saw directly that
something does not work now *and* can say what that blocks (``blocks``). What was seen but is not known
to block anything is ``warning`` (to be confirmed); what is only inferred (a listing that did not show
something, the kernel log of this boot, the groups of the SSH account, an interface that is switched
off) is ``info``. Only faults make a card red.
"""
import tempfile
import unittest
from unittest import mock

from brainos import hardware_scan, motion_drivers, repair_policy, ros2_profile
from brainos.service import Platform
from brainos.body_types import built_in
from hardware_fixture import JETPACK6, X86_NVIDIA, jetson
from motion_drivers_fixture import chassis, idle
from ros2_fixture import absent

QUADRUPED = built_in()[0]

# What issue #47's robot showed: a board whose spare ports are unused, whose 4G modem's serial ports
# belong to dialout, and whose boot log has two harmless lines. The robot walked.
SPARE_PORTS = '''@@NX-HW:system@@
hostname=robot
kernel=6.1.0
arch=aarch64
os=Ubuntu 22.04.5 LTS
model=Some RK3588 Board
user=operator
uid=1000
groups=operator sudo audio
ssh=192.0.2.9 50000 192.0.2.32 22
@@NX-HW:cpu@@
Model name:                         Cortex-A76
CPU(s):                             8
@@NX-HW:memory@@
MemTotal:        8000000 kB
MemAvailable:    5700000 kB
@@NX-HW:rootfs@@
/dev/mmcblk0p3 60000000 20000000 40000000  34% /
@@NX-HW:gpu@@
exit=missing
@@NX-HW:net@@
eth1|up|1|0x1003|1|02:00:00:00:00:01|rk_gmac-dwmac|0|1000|/sys/devices/platform/fe1c0000.ethernet/net/eth1
wlan0|up|1|0x1003|1|02:00:00:00:00:02|rtl8852be|1||/sys/devices/platform/fe170000.pcie/pci0003:30/0003:30:00.0/0003:31:00.0/net/wlan0
wlan1|down||0x1002|1|02:00:00:00:00:03|rtl8852be|1||/sys/devices/platform/fe170000.pcie/pci0003:30/0003:30:00.0/0003:31:00.0/net/wlan1
@@NX-HW:addr@@
3: wlan0    inet 192.0.2.32/24 brd 192.0.2.255 scope global dynamic wlan0
@@NX-HW:route@@
default via 192.0.2.1 dev wlan0
@@NX-HW:dns@@
nameserver 192.0.2.1
@@NX-HW:video@@
video0|stream_cif_mipi_id0|rkcif|video|660|/sys/devices/platform/rkcif-mipi-lvds/video4linux/video0
@@NX-HW:serial@@
/dev/ttyUSB0|dialout|660|option1|/sys/devices/platform/usb/usb1/1-1/1-1.3/1-1.3:1.0/ttyUSB0
@@NX-HW:kernel@@
source=dmesg
[    4.045219] rk-pcie: probe of fe150000.pcie failed with error -22
[    6.586374] platform regulatory.0: Direct firmware load for regulatory.db failed with error -2
@@NX-HW:failed_units@@
@@NX-HW:end@@
'''


def by_id(rows):
    return {row['id']: row for row in rows}


def part(text):
    return {**ros2_profile.parse(text), 'checked_at': 1763700000.0, 'duration_ms': 12, 'problem': None}


class HardwareFaultTests(unittest.TestCase):
    def test_a_robot_with_unused_ports_and_boot_log_lines_has_no_fault_and_nothing_to_confirm(self):
        result = hardware_scan.parse(SPARE_PORTS)
        issues, devices = by_id(result['issues']), by_id(result['devices'])

        self.assertEqual(set(issues), {'net-noip:eth1', 'net-down:wlan1', 'video-perm:/dev/video0', 'serial-perm:/dev/ttyUSB0',
                                       'log-probe:fe150000.pcie', 'log-firmware:regulatory.db'})
        self.assertEqual({issue['severity'] for issue in issues.values()}, {'info'})
        self.assertEqual((result['summary']['errors'], result['summary']['warnings'], result['summary']['attention']), (0, 0, 0))
        # The devices themselves were seen working: an account's groups say nothing about the camera.
        self.assertEqual(devices['camera:/dev/video0']['status'], 'ok')
        self.assertEqual(devices['serial:/dev/ttyUSB0']['status'], 'ok')
        self.assertEqual(devices['net:wlan1']['status'], 'unknown')
        self.assertEqual(devices['net:eth1']['status'], 'unknown')

    def test_what_an_account_cannot_open_is_said_about_the_account_not_about_the_robot(self):
        issues = by_id(hardware_scan.parse(SPARE_PORTS)['issues'])

        for issue_id in ('video-perm:/dev/video0', 'serial-perm:/dev/ttyUSB0'):
            self.assertIn('这个账号', issues[issue_id]['detail'], issue_id)
            self.assertIn('不影响', issues[issue_id]['detail'], issue_id)
            self.assertNotIn('将无法', issues[issue_id]['detail'], issue_id)

    def test_every_fault_says_what_it_blocks(self):
        full = jetson().replace(' 52% /', ' 99% /')
        for name, text in (('jetson', jetson()), ('jetpack6', JETPACK6), ('x86', X86_NVIDIA), ('full', full), ('spare', SPARE_PORTS)):
            for issue in hardware_scan.parse(text)['issues']:
                self.assertIn('blocks', issue, (name, issue['id']))
                self.assertEqual(bool(issue['blocks']), issue['severity'] == 'error', (name, issue['id']))

    def test_a_full_root_partition_is_a_fault_and_an_almost_full_one_is_not(self):
        full = by_id(hardware_scan.parse(jetson().replace(' 52% /', ' 99% /'))['issues'])['rootfs-full']
        almost = by_id(hardware_scan.parse(X86_NVIDIA)['issues'])['rootfs-full']          # 96 %

        self.assertEqual(full['severity'], 'error')
        self.assertIn('安装', full['blocks'])
        self.assertEqual((almost['severity'], almost['blocks']), ('warning', ''))

    def test_a_device_without_a_driver_is_to_be_confirmed_not_a_fault(self):
        result = hardware_scan.parse(jetson())
        issue, row = by_id(result['issues'])['pci-driver:0008:01:00.0'], by_id(result['devices'])['pci:0008:01:00.0']

        self.assertEqual((issue['severity'], row['status']), ('warning', 'warning'))
        self.assertNotIn('设备当前不可用', issue['detail'])
        self.assertIn('没有绑定内核驱动', issue['detail'])
        self.assertEqual(result['summary']['errors'], 0)

    def test_a_gpu_whose_driver_does_not_answer_is_to_be_confirmed(self):
        result = hardware_scan.parse(X86_NVIDIA)

        self.assertEqual(by_id(result['issues'])['nvidia-driver']['severity'], 'warning')
        self.assertEqual(result['summary']['errors'], 0)

    def test_a_port_that_is_switched_off_is_a_note_and_a_can_bus_that_is_down_is_to_be_confirmed(self):
        port = by_id(hardware_scan.parse(SPARE_PORTS)['issues'])['net-down:wlan1']
        bus = by_id(hardware_scan.parse(jetson())['issues'])['net-down:can0']

        self.assertEqual(port['severity'], 'info')
        self.assertIn('不是故障', port['detail'])
        # Bringing a CAN interface up takes the bus bitrate: it stays with what needs someone on site (issue #13).
        self.assertEqual((bus['severity'], bus['fixable']), ('warning', 'needs_user'))
        old = {'id': 'net-down:can0', 'severity': 'warning', 'category': 'can', 'device_id': None, 'fixable': 'needs_user'}
        scan = {'issues': [old], 'devices': [], 'summary': {'errors': 0, 'warnings': 1}}
        self.assertEqual(hardware_scan.regraded(scan)['issues'][0]['severity'], 'warning')

    def test_the_summary_counts_what_the_issues_say(self):
        for text in (jetson(), JETPACK6, X86_NVIDIA, SPARE_PORTS):
            result = hardware_scan.parse(text)
            severities = [issue['severity'] for issue in result['issues']]
            self.assertEqual((result['summary']['errors'], result['summary']['warnings']),
                             (severities.count('error'), severities.count('warning')))
            self.assertEqual(result['rules'], hardware_scan.RULES)


class SavedScanTests(unittest.TestCase):
    """A scan saved under the earlier rules is shown by the rules of today."""

    def _old(self):
        issues = [
            {'id': 'motion-driver:base.velocity', 'severity': 'error', 'category': 'motion', 'title': '缺少运控接口：底盘速度（base.velocity）',
             'detail': 'd', 'hint': 'h', 'device_id': None, 'fixable': 'auto', 'source': 'ros2_profile'},
            {'id': 'pci-driver:0008:01:00.0', 'severity': 'error', 'category': 'network', 'title': 't', 'detail': 'd', 'hint': '',
             'device_id': 'pci:0008:01:00.0', 'fixable': 'auto', 'source': 'scan'},
            {'id': 'serial-perm:/dev/ttyUSB0', 'severity': 'warning', 'category': 'serial', 'title': 't', 'detail': 'd', 'hint': '',
             'device_id': 'serial:/dev/ttyUSB0', 'fixable': 'auto', 'source': 'scan'},
            {'id': 'net-route', 'severity': 'warning', 'category': 'network', 'title': 't', 'detail': 'd', 'hint': '',
             'device_id': None, 'fixable': 'auto', 'source': 'scan'},
            {'id': 'unit:x.service', 'severity': 'info', 'category': 'service', 'title': 't', 'detail': 'd', 'hint': '',
             'device_id': None, 'fixable': 'needs_user', 'source': 'scan'},
        ]
        devices = [{'id': 'pci:0008:01:00.0', 'category': 'network', 'name': 'wifi', 'bus': '', 'driver': '未加载', 'detail': '', 'status': 'error'},
                   {'id': 'serial:/dev/ttyUSB0', 'category': 'serial', 'name': 'tty', 'bus': '', 'driver': 'x', 'detail': '', 'status': 'warning'},
                   {'id': 'power:BAT0', 'category': 'power', 'name': 'BAT0', 'bus': '', 'driver': '', 'detail': '', 'status': 'warning'},
                   {'id': 'compute:cpu', 'category': 'compute', 'name': 'cpu', 'bus': '', 'driver': '', 'detail': '', 'status': 'ok'}]
        return {'id': 'hardware-scan-1', 'robot_id': 'robot-1', 'issues': issues, 'devices': devices, 'notes': [],
                'summary': {'devices': 4, 'ok': 1, 'attention': 3, 'errors': 2, 'warnings': 2, 'categories': {}}}

    def test_a_scan_of_the_earlier_rules_loses_the_faults_it_could_not_prove(self):
        old = self._old()
        shown = hardware_scan.regraded(old)
        issues, devices = by_id(shown['issues']), by_id(shown['devices'])

        self.assertEqual({issue_id: issue['severity'] for issue_id, issue in issues.items()},
                         {'motion-driver:base.velocity': 'info', 'pci-driver:0008:01:00.0': 'warning', 'serial-perm:/dev/ttyUSB0': 'info',
                          'net-route': 'warning', 'unit:x.service': 'info'})
        self.assertEqual((shown['summary']['errors'], shown['summary']['warnings'], shown['summary']['attention']), (0, 2, 2))
        self.assertEqual(devices['pci:0008:01:00.0']['status'], 'warning')
        self.assertEqual(devices['serial:/dev/ttyUSB0']['status'], 'unknown', 'no longer "needs handling"; what it is was not read again')
        self.assertEqual(devices['power:BAT0']['status'], 'warning', 'a row no problem is about keeps what was read')
        orphan = {**old, 'devices': [{**old['devices'][0], 'id': 'pci:0000:02:00.0'}]}
        self.assertEqual(hardware_scan.regraded(orphan)['devices'][0]['status'], 'warning', 'no row stays a fault')
        self.assertTrue(shown['regraded'])
        self.assertTrue(all(issue['blocks'] == '' for issue in shown['issues']))
        self.assertEqual([issue['severity'] for issue in shown['issues']],
                         sorted((issue['severity'] for issue in shown['issues']), key=lambda s: -hardware_scan.SEVERITY_ORDER[s]))
        self.assertEqual(old['summary']['errors'], 2, 'the saved record is not changed')
        self.assertEqual(old['issues'][0]['severity'], 'error')

    def test_a_scan_of_todays_rules_is_shown_as_saved(self):
        parsed = {**hardware_scan.parse(jetson().replace(' 52% /', ' 99% /')), 'id': 'hardware-scan-2'}

        self.assertIs(hardware_scan.regraded(parsed), parsed)
        self.assertEqual(parsed['summary']['errors'], 1)


class OnSiteTests(unittest.TestCase):
    """Whether someone has to go to the robot is about the remedy, not about how much the problem weighs:
    what issue #13 keeps away from the assistant stays away from it."""

    def test_a_cable_that_is_not_plugged_in_is_a_note_that_still_needs_someone_on_site(self):
        unplugged = jetson().replace('eth0|up|1|0x1003', 'eth0|down|0|0x1003')
        issue = by_id(hardware_scan.parse(unplugged)['issues'])['net-carrier:eth0']

        self.assertEqual((issue['severity'], issue['onsite']), ('info', True))
        self.assertTrue(repair_policy.needs_onsite(issue))

    def test_a_spare_port_and_a_failed_service_are_the_users_to_judge(self):
        issues = by_id(hardware_scan.parse(JETPACK6)['issues'])

        for issue_id in ('net-carrier:eth1', 'unit:robot-idle-start.service', 'usb-driver:1-1.4'):
            self.assertFalse(issues[issue_id]['onsite'], issue_id)
            self.assertFalse(repair_policy.needs_onsite(issues[issue_id]), issue_id)

    def test_a_device_that_never_enumerated_needs_someone_on_site(self):
        missing = jetson().replace('[   12.000001] usb 1-4:', '[   12.000001] usb 1-7:')
        issue = by_id(hardware_scan.parse(missing)['issues'])['log-usb-enum:1-7']

        self.assertEqual((issue['severity'], issue['onsite']), ('info', True))

    def test_what_software_can_do_is_never_on_site(self):
        for text in (jetson(), JETPACK6, X86_NVIDIA, SPARE_PORTS):
            for issue in hardware_scan.parse(text)['issues']:
                if issue['fixable'] == 'auto':
                    self.assertFalse(issue['onsite'], issue['id'])

    def test_a_scan_of_the_earlier_rules_keeps_who_has_to_handle_what(self):
        old = [{'id': 'net-carrier:eth0', 'severity': 'warning', 'category': 'network', 'device_id': None, 'fixable': 'needs_user'},
               {'id': 'unit:x.service', 'severity': 'info', 'category': 'service', 'device_id': None, 'fixable': 'needs_user'},
               {'id': 'serial-perm:/dev/ttyUSB0', 'severity': 'warning', 'category': 'serial', 'device_id': None, 'fixable': 'auto'}]
        shown = by_id(hardware_scan.regraded({'issues': old, 'devices': [], 'summary': {}})['issues'])

        self.assertEqual({issue_id: (issue['severity'], repair_policy.needs_onsite(issue)) for issue_id, issue in shown.items()},
                         {'net-carrier:eth0': ('info', True), 'unit:x.service': ('info', False), 'serial-perm:/dev/ttyUSB0': ('info', False)})


class WorkspaceTests(unittest.TestCase):
    def test_opening_a_workspace_replaces_scans_of_the_earlier_rules_once(self):
        """The page, the repair flow and the assistant read the same record: one grading, not two."""
        with tempfile.TemporaryDirectory() as folder:
            old = {**SavedScanTests()._old(), 'project_id': 'project-1', 'created_at': '2026-11-24T10:00:00+00:00'}
            Platform(folder).store.put('hardware_scans', old)

            platform = Platform(folder)
            saved = platform.store.get('hardware_scans', old['id'])

            self.assertEqual((saved['rules'], saved['regraded'], saved['summary']['errors']), (hardware_scan.RULES, True, 0))
            self.assertEqual(saved, hardware_scan.regraded(old))
            self.assertIs(hardware_scan.regraded(saved), saved, 'and is left alone from then on')
            shown = [scan for scan in platform.hardware.snapshot()['hardware_scans'] if scan['id'] == old['id']]
            self.assertEqual(shown, [saved])


    def test_a_record_that_lacks_parts_is_regraded_as_having_none_of_them(self):
        bare = {'id': 'hardware-scan-3', 'robot_id': 'robot-1',
                'issues': [{'id': 'pci-driver:0000:01:00.0', 'severity': 'error'}, {'severity': 'fatal'}, {'id': 'net-route'}]}

        shown = hardware_scan.regraded(bare)

        self.assertEqual([issue['severity'] for issue in shown['issues']], ['warning', 'info', 'info'])
        self.assertEqual((shown['devices'], shown['summary']['errors'], shown['summary']['warnings'], shown['rules']),
                         ([], 0, 1, hardware_scan.RULES))
        self.assertEqual(hardware_scan.regraded({'id': 'hardware-scan-4'})['issues'], [])

    def test_a_scan_that_cannot_be_regraded_does_not_keep_the_workspace_from_opening(self):
        with tempfile.TemporaryDirectory() as folder:
            good = {**SavedScanTests()._old(), 'project_id': 'project-1', 'created_at': '2026-11-24T10:00:00+00:00'}
            bad = {**good, 'id': 'hardware-scan-bad', 'created_at': '2026-11-24T11:00:00+00:00'}
            store = Platform(folder).store
            store.put('hardware_scans', good)
            store.put('hardware_scans', bad)
            real = hardware_scan.regraded

            def regraded(scan):
                if scan.get('id') == bad['id'] and scan.get('rules') != hardware_scan.RULES:
                    raise KeyError('summary')
                return real(scan)

            with mock.patch.object(hardware_scan, 'regraded', regraded):
                platform = Platform(folder)
                shown = {scan['id']: scan for scan in platform.hardware.snapshot()['hardware_scans']}

            self.assertEqual(platform.store.get('hardware_scans', good['id'])['rules'], hardware_scan.RULES, 'the others are still done')
            self.assertEqual(platform.store.get('hardware_scans', bad['id']), bad, 'the one that failed is left as saved')
            self.assertEqual(shown[bad['id']]['summary']['errors'], 2, 'and shown as saved')
            (event,) = [e for e in platform.store.list('events') if e['action'] == 'hardware_scan.regrade_failed']
            self.assertEqual((event['details']['count'], event['details']['scans'][0]['scan_id']), (1, bad['id']))
            self.assertIn('KeyError', event['details']['scans'][0]['error'])


class MotionInterfaceTests(unittest.TestCase):
    def test_a_listing_read_while_no_node_was_seen_proves_nothing(self):
        """Issue #47's robot: ROS 2 Humble is there, the read ran with default settings and saw no node."""
        result = motion_drivers.assess(QUADRUPED, part(idle()))

        self.assertEqual(result['status'], 'missing')
        self.assertFalse(result['credible'])
        found = motion_drivers.issues(result)
        self.assertEqual({issue['severity'] for issue in found}, {'info'})
        self.assertTrue(all(issue['blocks'] == '' for issue in found))
        for issue in found:
            self.assertIn('没有读到正在运行的节点', issue['detail'])
            self.assertIn('不说明', issue['detail'])
            self.assertNotIn('缺少', issue['title'])

    def test_a_listing_of_running_nodes_without_the_interface_is_to_be_confirmed_never_a_fault(self):
        result = motion_drivers.assess(QUADRUPED, part(chassis('/cmd_vel')))

        self.assertTrue(result['credible'])
        (issue,) = motion_drivers.issues(result)
        self.assertEqual((issue['id'], issue['severity'], issue['blocks']), ('motion-driver:base.velocity', 'warning', ''))
        self.assertIn('不说明本体不能运动', issue['detail'])

    def test_a_device_without_ros2_is_to_be_confirmed(self):
        result = motion_drivers.assess(QUADRUPED, part(absent()))

        self.assertTrue(result['credible'])
        self.assertEqual({issue['severity'] for issue in motion_drivers.issues(result)}, {'warning'})

    def test_no_reading_of_a_listing_makes_a_fault(self):
        for text in (idle(), chassis('/cmd_vel', '/odom'), absent()):
            for issue in motion_drivers.issues(motion_drivers.assess(QUADRUPED, part(text))):
                self.assertNotEqual(issue['severity'], 'error', issue['id'])


if __name__ == '__main__':
    unittest.main()
