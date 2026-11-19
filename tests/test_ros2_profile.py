"""Reading ROS 2 from a real machine (issue #27): the fixed command and its parser.

The command is checked as text (what it may and may not contain); the parser on outputs shaped
like the command's. Nothing here reaches a real machine.
"""
import json
import re
import shutil
import subprocess
import unittest

from brainos import device_software, hardware_scan, ros2_profile
from ros2_fixture import (ACTION_LIST, DEFAULT_ENV, NODE_ENV, NODES, TOPIC_LIST, TOPICS, TRACEBACK, absent, listing,
                          ros1_only, ros2)

COMMAND = ros2_profile.ROS2_COMMAND
# The calls of the ros2 command line tool, one per line of the script.
CALLS = [line for line in COMMAND.splitlines() if re.search(r'\$T ros2 ', line)]


def names(rows):
    return [row['name'] for row in rows]


class Ros2CommandTests(unittest.TestCase):
    def test_command_only_reads(self):
        for word in ('sudo', 'modprobe', 'apt', 'pip', 'tee', 'rm ', 'mv ', 'cp ', 'chmod', 'chown', 'kill', 'systemctl', 'docker',
                     'reboot', 'shutdown', 'source ', 'eval', 'python', 'curl', 'wget', 'rostopic', 'rosservice', 'rosnode',
                     'cmd_vel', ' pub', ' call', 'send_goal', 'ros2 param', 'ros2 run', 'ros2 launch', 'ros2 bag',
                     'ros2 lifecycle', 'ros2 daemon', 'ros2 action', 'ros2 component', 'ros2 control', 'mkdir', 'touch ', 'ln ',
                     'dd ', 'truncate', 'sed -i', 'mkfifo', 'mktemp', 'bash', 'exec ', 'nohup', 'setsid', 'xargs'):
            self.assertNotIn(word, COMMAND, word)
        # The only redirections are "2>&1" and "to /dev/null": the command writes no file and reads none through "<".
        rest = re.sub(r'2>&1|2?>/dev/null', '', COMMAND)
        self.assertNotIn('>', rest)
        self.assertNotIn('<', COMMAND)
        # Nothing is put in the background: no "&" besides "&&".
        self.assertNotIn('&', rest.replace('&&', ''))

    @unittest.skipUnless(shutil.which('sh'), 'needs a POSIX shell')
    def test_the_script_is_valid_sh(self):
        # Parsed only (-n): nothing of it runs here.
        checked = subprocess.run(['sh', '-n', '-c', COMMAND], capture_output=True, text=True, timeout=20)

        self.assertEqual((checked.returncode, checked.stderr), (0, ''))

    def test_ros2_is_only_asked_to_list_nodes_topics_and_services(self):
        self.assertEqual(re.findall(r'\bros2 ([a-z_]+) ([a-z_]+)', COMMAND), [('node', 'list'), ('topic', 'list'), ('service', 'list')])
        self.assertEqual(len(CALLS), 3)
        self.assertEqual(len(re.findall(r'\bros2 [a-z]', COMMAND)), 3)        # no other call of the tool anywhere

    def test_listing_never_starts_or_uses_the_ros2_daemon(self):
        # Without this flag the tool starts a background daemon that stays on the robot after the read.
        for line in CALLS:
            self.assertIn(' --no-daemon ', line, line)

    def test_the_only_file_loaded_is_the_setup_file_of_an_installed_distribution(self):
        loaded = re.findall(r'(?:^|[\s;&|(])\.\s+(\S+)', COMMAND, re.M)

        self.assertEqual(loaded, ['"/opt/ros/$D/setup.sh"'])
        # The name comes from the device: before it is used in a path it may only hold a-z, 0-9 and _.
        guard = COMMAND.index('case "$D" in *[!a-z0-9_]*) D=;; esac')
        self.assertLess(guard, COMMAND.index('. "/opt/ros/$D/setup.sh"'))
        for match in re.finditer(r'D=\$n', COMMAND):                         # a name taken from the directory listing
            before = COMMAND[:match.start()].rsplit('\n', 1)[-1]
            self.assertIn('case "$n" in *[!a-z0-9_]*) continue;; esac', before)

    def test_another_distribution_is_never_loaded_in_place_of_the_one_that_runs(self):
        # A robot whose nodes run a distribution that is not under /opt/ros must not be read, and reported,
        # as the Humble that happens to be installed next to it.
        fallbacks = [line for line in COMMAND.splitlines() if re.search(r'D=humble|D=\$n', line)]

        self.assertEqual(len(fallbacks), 2)
        for line in fallbacks:
            self.assertTrue(line.startswith('[ -z "$R" ] && '), line)
        self.assertIn('R=$(val ROS_DISTRO)\nD=$R\n', COMMAND)

    def test_without_a_time_limit_on_the_device_the_tool_is_not_run(self):
        guard = COMMAND.index('''if [ -z "$T" ]; then echo 'cli=notimeout'\nelif command -v ros2''')

        self.assertLess(guard, COMMAND.index('$T ros2 '))

    def test_every_ros2_call_runs_under_a_time_limit_and_the_whole_read_fits(self):
        grace, limit = map(int, re.search(r"T='timeout -k (\d+) (\d+)'", COMMAND).groups())
        for line in CALLS:
            self.assertRegex(line, r'\$\(\$T ros2 ')
            spin = float(re.search(r'--spin-time (\d+(?:\.\d+)?) ', line).group(1))
            self.assertLess(spin + 4, limit)                                 # room for the tool to start and to end
        self.assertLessEqual(len(CALLS) * (limit + grace) + 8, ros2_profile.TIMEOUT_SECONDS)

    def test_every_listing_is_capped_in_lines_and_in_width(self):
        for line in CALLS:
            self.assertRegex(line, r'\| head -n \d+ \| cut -c1-\d+$')
        # The parser knows these limits: a listing that reached one is reported as cut.
        sent = {re.search(r'ros2 ([a-z]+) list', line).group(1) + 's': int(re.search(r'head -n (\d+)', line).group(1)) for line in CALLS}
        self.assertEqual(sent, ros2_profile.HEAD_LINES)
        wide = {re.search(r'ros2 ([a-z]+) list', line).group(1) + 's': int(re.search(r'cut -c1-(\d+)$', line).group(1)) for line in CALLS}
        self.assertEqual(wide, ros2_profile.LINE_WIDTH)
        for name, limit in (('nodes', ros2_profile.MAX_NODES), ('topics', ros2_profile.MAX_TOPICS), ('services', ros2_profile.MAX_SERVICES)):
            self.assertGreater(sent[name], limit)
        self.assertGreaterEqual(ros2_profile.OUTPUT_LIMIT, 262144)

    def test_only_named_variables_are_taken_from_a_running_node(self):
        # The environment of a process holds credentials often enough: only these names are read from it,
        # and only five of them are printed (the parser checks their shape).
        wanted = re.search(r"K='([A-Z_|]+)'", COMMAND).group(1).split('|')

        self.assertEqual(sorted(wanted), sorted(ros2_profile.ENVIRONMENT_NAMES))
        for name in wanted:
            self.assertRegex(name, r'^(ROS_|RMW_|CYCLONEDDS_|FASTRTPS_|FASTDDS_)')
        # printf, not echo: a value from the device is printed as it is, backslashes included.
        self.assertEqual(sorted(re.findall(r'''printf '%s\\n' "([a-z]+)=\$\(val ''', COMMAND)),
                         ['distro', 'domain', 'localhost', 'rmw', 'version'])
        self.assertNotRegex(COMMAND, r'echo "[^"\n]*\$\(val ')
        self.assertEqual(COMMAND.count('grep -E "^($K)="'), 2)              # a process's environment, and this session's

    def test_the_environment_is_taken_from_a_node_before_anything_else(self):
        # In this order: a process with the ROS 2 client library loaded that is not the tool's own daemon (it keeps
        # the environment of whoever started it), any process with a ROS environment, this SSH session's own.
        order = re.search(r'''pick "\$\(grep -l 'librcl' /proc/\[0-9\]\*/maps [^)]*\)" nodes \|\| '''
                          r'''pick "\$\(grep -l 'ROS_DISTRO=' /proc/\[0-9\]\*/environ [^)]*\)" \|\| \{ e=\$\(env \| ''', COMMAND)

        self.assertIsNotNone(order)
        self.assertIn('''[ -n "$2" ] && grep -q -e 'ros2cli' -e '_ros2_daemon' "/proc/$p/cmdline" 2>/dev/null && continue''', COMMAND)
        self.assertIn('[ "$p" = "$$" ] || [ "$p" = "$PPID" ] && continue', COMMAND)     # never this command or its parent

    def test_command_is_a_constant_run_under_sh(self):
        self.assertTrue(ros2_profile.ROS2_EXEC.startswith('sh -c '))
        sections = re.findall(r"echo '@@NX-ROS:([a-z_]+)@@'", COMMAND)

        self.assertEqual(sections, ['install', 'env', 'setup', 'nodes', 'topics', 'services', 'end'])

    def test_the_hardware_scan_and_the_software_read_stay_free_of_ros2(self):
        # Issue #27 adds its own command; the two older ones are not widened.
        for command in (hardware_scan.SCAN_COMMAND, device_software.SOFTWARE_COMMAND):
            self.assertNotIn('ros2', command)
            self.assertNotRegex(command, r'(^|\s)\.\s+/')


class HumbleRobotTests(unittest.TestCase):
    def setUp(self):
        self.ros = ros2_profile.parse(ros2())

    def test_the_topic_list_is_what_ros2_topic_list_prints_on_the_device(self):
        # The acceptance of issue #27.
        self.assertEqual(names(self.ros['topics']), TOPIC_LIST.split())

    def test_every_topic_has_its_message_types(self):
        topics = {row['name']: row['types'] for row in self.ros['topics']}

        self.assertEqual(topics['/cmd_vel'], ['geometry_msgs/msg/Twist'])
        self.assertEqual(topics['/camera/color/image_raw'], ['sensor_msgs/msg/Image'])
        self.assertEqual(topics['/status/text'], ['std_msgs/msg/String', 'std_msgs/msg/Header'])   # two publishers disagree
        self.assertEqual(set(self.ros['topics'][0]), {'name', 'types'})

    def test_actions_are_what_ros2_action_list_prints(self):
        printed = [f"{row['name']} [{', '.join(row['types'])}]" for row in self.ros['actions']]

        self.assertEqual(printed, ACTION_LIST.splitlines())

    def test_nodes_and_services(self):
        self.assertEqual(names(self.ros['nodes']), NODES.split())
        self.assertEqual(set(self.ros['nodes'][0]), {'name'})
        services = {row['name']: row['types'] for row in self.ros['services']}
        self.assertEqual(len(services), 7)
        self.assertEqual(services['/chassis_bridge/set_mode'], ['robot_interfaces/srv/SetMode'])

    def test_humble_is_the_supported_distribution(self):
        self.assertEqual({key: self.ros[key] for key in ('status', 'found', 'distro', 'generation', 'supported', 'complete')},
                         {'status': 'ok', 'found': True, 'distro': 'humble', 'generation': 2, 'supported': True, 'complete': True})
        self.assertEqual(self.ros['installed'], [{'name': 'humble', 'generation': 2}])
        self.assertEqual(self.ros['notes'], [])

    def test_the_environment_of_the_running_nodes_is_reported(self):
        self.assertEqual(self.ros['environment'],
                         {'source': 'process', 'domain_id': 42, 'rmw': 'rmw_cyclonedds_cpp', 'localhost_only': False,
                          'adopted': ['ROS_DOMAIN_ID', 'RMW_IMPLEMENTATION', 'ROS_LOCALHOST_ONLY', 'CYCLONEDDS_URI']})

    def test_the_result_can_be_sent_as_json(self):
        json.dumps(self.ros, allow_nan=False)


class DistributionTests(unittest.TestCase):
    def test_a_distribution_other_than_humble_is_marked_unsupported_and_still_listed(self):
        ros = ros2_profile.parse(ros2(installed=(('foxy', 1, 0),), env={**NODE_ENV, 'distro': 'foxy'}, distro='foxy'))

        self.assertEqual((ros['status'], ros['distro'], ros['generation'], ros['supported']), ('unsupported', 'foxy', 2, False))
        self.assertEqual(names(ros['topics']), TOPIC_LIST.split())     # read the same way, for reference
        self.assertEqual(ros['notes'], [])                               # the fields say it; there is nothing to add

    def test_every_other_ros2_distribution_is_unsupported(self):
        for name in ('galactic', 'iron', 'jazzy', 'rolling'):
            ros = ros2_profile.parse(ros2(installed=((name, 1, 0),), env={**NODE_ENV, 'distro': name}, distro=name))
            self.assertEqual((ros['status'], ros['supported']), ('unsupported', False), name)

    def test_ros1_is_unsupported_and_nothing_is_listed(self):
        ros = ros2_profile.parse(ros1_only())

        self.assertEqual((ros['status'], ros['found'], ros['distro'], ros['generation'], ros['supported']),
                         ('unsupported', True, 'noetic', 1, False))
        for part in ('nodes', 'topics', 'services', 'actions'):
            self.assertIsNone(ros[part], part)                               # not read, which is not "none there"

    def test_with_two_distributions_the_one_that_was_loaded_is_reported(self):
        ros = ros2_profile.parse(ros2(installed=(('foxy', 1, 0), ('humble', 1, 0))))

        self.assertEqual((ros['distro'], ros['supported']), ('humble', True))
        self.assertEqual(ros['installed'], [{'name': 'foxy', 'generation': 2}, {'name': 'humble', 'generation': 2}])

    def test_nodes_of_another_distribution_next_to_an_installed_humble_are_not_reported_as_humble(self):
        # Built from source elsewhere and running; /opt/ros/humble is installed but is not what runs.
        ros = ros2_profile.parse(ros2(env={**NODE_ENV, 'distro': 'jazzy'}, distro='', sourced=None, cli=None,
                                      nodes=None, topics=None, services=None))

        self.assertEqual((ros['distro'], ros['generation'], ros['supported'], ros['status']), ('jazzy', 2, False, 'unsupported'))
        self.assertEqual(ros['installed'], [{'name': 'humble', 'generation': 2}])
        self.assertIsNone(ros['topics'])
        self.assertTrue(any('jazzy' in note and '/opt/ros' in note for note in ros['notes']), ros['notes'])

    def test_what_runs_decides_even_if_another_distribution_s_tool_listed_it(self):
        ros = ros2_profile.parse(ros2(env={**NODE_ENV, 'distro': 'jazzy'}, distro='humble'))

        self.assertEqual((ros['distro'], ros['supported'], ros['status']), ('jazzy', False, 'unsupported'))
        self.assertTrue(any('jazzy' in note and 'humble' in note for note in ros['notes']), ros['notes'])

    def test_a_ros2_that_is_not_under_opt_ros_is_named_but_not_listed(self):
        # Built from source somewhere else: a running node says which distribution, the command cannot load it.
        ros = ros2_profile.parse(ros2(installed=(), distro='', sourced=None, cli=None, nodes=None, topics=None, services=None))

        self.assertEqual((ros['found'], ros['distro'], ros['generation'], ros['supported'], ros['status']),
                         (True, 'humble', 2, True, 'incomplete'))
        self.assertIsNone(ros['topics'])
        self.assertTrue(any('/opt/ros' in note for note in ros['notes']), ros['notes'])


class AbsentTests(unittest.TestCase):
    def test_a_device_without_ros_is_reported_as_such_not_as_a_failure(self):
        ros = ros2_profile.parse(absent())

        self.assertEqual({key: ros[key] for key in ('status', 'found', 'distro', 'generation', 'supported', 'complete')},
                         {'status': 'absent', 'found': False, 'distro': None, 'generation': None, 'supported': None,
                          'complete': True})
        self.assertEqual(ros['installed'], [])
        for part in ('nodes', 'topics', 'services', 'actions'):
            self.assertIsNone(ros[part], part)
        self.assertEqual(ros['notes'], [])

    def test_installed_but_nothing_running_lists_an_empty_graph(self):
        only_the_tool = '/parameter_events [rcl_interfaces/msg/ParameterEvent]\n/rosout [rcl_interfaces/msg/Log]\n'
        ros = ros2_profile.parse(ros2(env=DEFAULT_ENV, nodes=listing(''), topics=listing(only_the_tool), services=listing('')))

        self.assertEqual((ros['status'], ros['nodes'], ros['services'], ros['actions']), ('ok', [], [], []))
        self.assertEqual(names(ros['topics']), ['/parameter_events', '/rosout'])
        self.assertEqual(ros['environment'], {'source': 'default', 'domain_id': None, 'rmw': None, 'localhost_only': None,
                                              'adopted': []})
        # Read with the defaults: a robot on another domain would look empty, and the profile says so.
        self.assertTrue(any('ROS_DOMAIN_ID' in note for note in ros['notes']), ros['notes'])


class PartlyReadTests(unittest.TestCase):
    def test_a_listing_that_ran_out_of_time_is_unknown_not_empty(self):
        for code in (124, 137):
            ros = ros2_profile.parse(ros2(services=listing('', code)))

            self.assertIsNone(ros['services'])
            self.assertEqual(ros['status'], 'incomplete')
            self.assertEqual(names(ros['topics']), TOPIC_LIST.split())      # the rest is still there
            self.assertTrue(any('服务' in note and '时间' in note for note in ros['notes']), ros['notes'])

    def test_a_listing_that_failed_says_what_the_tool_said(self):
        ros = ros2_profile.parse(ros2(nodes=listing(TRACEBACK, 1), topics=listing(TRACEBACK, 1), services=listing(TRACEBACK, 1)))

        self.assertEqual((ros['status'], ros['nodes'], ros['topics'], ros['actions']), ('incomplete', None, None, None))
        self.assertTrue(any('rmw_cyclonedds_cpp' in note and '退出码 1' in note for note in ros['notes']), ros['notes'])
        self.assertEqual(ros['distro'], 'humble')                           # which distribution it is was still read

    def test_what_the_tool_said_passes_through_the_credential_filter(self):
        ros = ros2_profile.parse(ros2(nodes=listing('fatal: login hunter2 was rejected\n', 1)),
                                 clean=lambda text: text.replace('hunter2', '[hidden]'))

        text = json.dumps(ros, ensure_ascii=False)
        self.assertNotIn('hunter2', text)
        self.assertIn('[hidden]', text)

    def test_warnings_printed_among_the_names_are_not_taken_for_names(self):
        noisy = ('WARNING: Be aware that there are nodes in the graph that share an exact name, this can have unintended side effects.\n'
                 '/chassis_bridge\n/chassis_bridge\n/sensors/lidar\n')
        ros = ros2_profile.parse(ros2(nodes=listing(noisy)))

        self.assertEqual(names(ros['nodes']), ['/chassis_bridge', '/sensors/lidar'])
        self.assertTrue(any('重名' in note and '/chassis_bridge' in note for note in ros['notes']), ros['notes'])

    def test_the_environment_of_the_ssh_session_is_named_as_such(self):
        ros = ros2_profile.parse(ros2(env={**NODE_ENV, 'source': 'session'}))

        self.assertEqual((ros['environment']['source'], ros['environment']['domain_id']), ('session', 42))
        self.assertEqual(ros['notes'], [])                                   # not the defaults: no warning about them

    def test_a_device_without_timeout_is_not_asked_and_says_why(self):
        ros = ros2_profile.parse(ros2(cli='notimeout', nodes=None, topics=None, services=None))

        self.assertEqual((ros['status'], ros['distro'], ros['nodes'], ros['topics']), ('incomplete', 'humble', None, None))
        self.assertTrue(any('timeout' in note for note in ros['notes']), ros['notes'])

    def test_a_line_cut_at_the_command_s_width_is_reported_not_dropped_silently(self):
        long_line = '/many_types [' + ', '.join(f'pkg_{index:03d}/msg/Kind' for index in range(40))
        cut = long_line[:ros2_profile.LINE_WIDTH['topics']]                  # what `cut -c` leaves of it: no closing bracket
        ros = ros2_profile.parse(ros2(topics=listing(TOPICS + cut + '\n')))

        self.assertNotIn('/many_types', names(ros['topics']))
        self.assertFalse(ros['complete'])
        self.assertTrue(any('话题' in note and '太长' in note for note in ros['notes']), ros['notes'])

    def test_topics_without_any_node_get_a_hint(self):
        ros = ros2_profile.parse(ros2(nodes=listing('')))

        self.assertEqual((ros['status'], ros['nodes']), ('ok', []))
        self.assertTrue(any('没有读到节点' in note and '账号' in note for note in ros['notes']), ros['notes'])

    def test_a_setup_file_without_the_command_line_tool(self):
        ros = ros2_profile.parse(ros2(cli='missing', nodes=None, topics=None, services=None))

        self.assertEqual((ros['status'], ros['distro'], ros['topics']), ('incomplete', 'humble', None))
        self.assertTrue(any('ros2' in note for note in ros['notes']), ros['notes'])

    def test_cut_off_output_is_marked_incomplete(self):
        self.assertFalse(ros2_profile.parse(ros2(end=False))['complete'])
        self.assertEqual(ros2_profile.parse(ros2(end=False))['status'], 'incomplete')
        empty = ros2_profile.parse('')
        self.assertFalse(empty['complete'])
        self.assertEqual((empty['status'], empty['found']), ('absent', False))

    def test_a_listing_that_reached_the_command_s_line_limit_is_reported_as_cut(self):
        full = ''.join(f'/n{index:04d}\n' for index in range(ros2_profile.HEAD_LINES['nodes']))
        ros = ros2_profile.parse(ros2(nodes=listing(full)))

        self.assertFalse(ros['complete'])
        self.assertEqual(ros['status'], 'incomplete')
        self.assertTrue(any('节点' in note and '太长' in note for note in ros['notes']), ros['notes'])

    def test_long_lists_are_capped_and_say_so(self):
        many = ''.join(f'/bus/channel_{index:04d} [std_msgs/msg/String]\n' for index in range(ros2_profile.MAX_TOPICS + 40))
        ros = ros2_profile.parse(ros2(topics=listing(many)))

        self.assertEqual(len(ros['topics']), ros2_profile.MAX_TOPICS)
        self.assertEqual(ros['status'], 'incomplete')
        self.assertTrue(any('话题' in note and '只' in note for note in ros['notes']), ros['notes'])


class UntrustedOutputTests(unittest.TestCase):
    def test_hostile_output_never_breaks_the_profile(self):
        hostile = ('@@NX-ROS:install@@\nhumble|1|0\n../../etc|1|0\n' + 'x' * 400 + '|1|0\nRM -RF|1|1\nhumble\n'
                   '@@NX-ROS:env@@\nsource=<script>\ndistro=humble; rm -rf /\nversion=nan\ndomain=1e999\nrmw=$(reboot)\n'
                   'localhost=yes\nadopted= ROS_DOMAIN_ID LD_PRELOAD ' + 'A' * 300 + '\n'
                   '@@NX-ROS:setup@@\ndistro=humble\nsourced=0\ncli=ok\n'
                   '@@NX-ROS:nodes@@\nexit=0\n/ok_node\n<img src=x onerror=alert(1)>\n/' + 'n' * 400 + '\n/bad node\nno_slash\n'
                   '@@NX-ROS:topics@@\nexit=0\n/good [std_msgs/msg/String]\n/no_type\n/bad [not a type]\n'
                   '/worse [' + 'a/' * 300 + 'b]\n/inject [std_msgs/msg/String, <b>]\n; rm -rf / [std_msgs/msg/String]\n'
                   '@@NX-ROS:services@@\nexit=nan\n/s [std_srvs/srv/Empty]\n@@NX-ROS:end@@\n')

        ros = ros2_profile.parse(hostile)

        json.dumps(ros, allow_nan=False)
        self.assertEqual(ros['installed'], [{'name': 'humble', 'generation': 2}])
        self.assertEqual(ros['environment'], {'source': 'default', 'domain_id': None, 'rmw': None, 'localhost_only': None,
                                              'adopted': ['ROS_DOMAIN_ID']})
        self.assertEqual(names(ros['nodes']), ['/ok_node'])
        self.assertEqual(ros['topics'], [{'name': '/good', 'types': ['std_msgs/msg/String']}])
        self.assertIsNone(ros['services'])                                   # an exit status that is not a number: not read
        for note in ros['notes']:
            self.assertLessEqual(len(note), 600)
            self.assertNotIn('<', note)

    def test_a_domain_id_outside_its_range_is_not_reported(self):
        for value in ('233', '-1', '4294967296', '07x'):
            ros = ros2_profile.parse(ros2(env={**NODE_ENV, 'domain': value}))
            self.assertIsNone(ros['environment']['domain_id'], value)
        self.assertEqual(ros2_profile.parse(ros2(env={**NODE_ENV, 'domain': '0'}))['environment']['domain_id'], 0)

    def test_the_distribution_name_is_checked_before_it_is_shown(self):
        ros = ros2_profile.parse(ros2(installed=(), env={**NODE_ENV, 'distro': 'Humble<b>'}, distro='', sourced=None, cli=None,
                                      nodes=None, topics=None, services=None))

        self.assertIsNone(ros['distro'])
        self.assertEqual(ros['status'], 'absent')


class NotReadTests(unittest.TestCase):
    def test_a_read_that_did_not_happen_has_every_field_and_knows_nothing(self):
        blank = ros2_profile.parse(ros2())

        never = ros2_profile.NOT_READ

        self.assertEqual(set(never), set(blank))
        self.assertEqual((never['status'], never['found'], never['topics'], never['complete']), ('not_read', None, None, False))


if __name__ == '__main__':
    unittest.main()
