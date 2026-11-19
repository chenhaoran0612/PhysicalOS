// Pure-logic checks of web/device_profile.js (issue #27): what the profile says about ROS 2 in each state
// (supported, another distribution, ROS 1, none, not read), how the lists are drawn and searched, and
// which robot cards get the button. The backend is checked in tests/test_ros2_profile.py and
// tests/test_device_profile.py; the page as a whole in tests/device_profile_browser.cjs.
//   node tests/device_profile_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file uses helpers of web/app.js (written here as they are there) and of web/live_status.js (loaded
// for real, with stand-ins for the page it attaches to).
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const element = {addEventListener: () => {}, dataset: {}, open: false};
const sandbox = {
  h, $: () => element, document: {addEventListener: () => {}}, setInterval: () => 0,
  engineRobotCard: () => '', engineAction: async () => {}, dialog: () => {}, render: () => {},
  state: {}, projectId: 'project-1', project: () => ({id: 'project-1'}),
  find: () => null, api: async () => ({}),
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
  button: (text, action, cls = '', id = '', disabled = false) =>
    `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`,
  table: (headers, data) => `<table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table>`,
};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/live_status.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/device_profile.js', 'utf8') +
  '\n;({profileVerdict, profileListTable, profileLists, profileRos2, profileCardButton, profileTabs, profileMatches, PROFILE_ROWS_SHOWN})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 400))}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 400))}`); checks += 1; };

const topics = [{name: '/cmd_vel', types: ['geometry_msgs/msg/Twist']}, {name: '/scan', types: ['sensor_msgs/msg/LaserScan']},
                {name: '/status/text', types: ['std_msgs/msg/String', 'std_msgs/msg/Header']}];
const ros = (changes = {}) => ({
  status: 'ok', found: true, distro: 'humble', generation: 2, supported: true, installed: [{name: 'humble', generation: 2}],
  environment: {source: 'process', domain_id: 42, rmw: 'rmw_cyclonedds_cpp', localhost_only: false, adopted: ['ROS_DOMAIN_ID']},
  nodes: [{name: '/chassis_bridge'}], topics, services: [{name: '/chassis_bridge/set_mode', types: ['robot_interfaces/srv/SetMode']}],
  actions: [{name: '/dock', types: ['robot_interfaces/action/Dock']}], notes: [], complete: true,
  checked_at: 1763500000, duration_ms: 9400, problem: null, ...changes});
const unread = {nodes: null, topics: null, services: null, actions: null};

// Humble is the supported distribution.
let [title, badges, sentence] = page.profileVerdict(ros());
check(title, 'ROS 2 Humble', 'the distribution is named');
includes(badges, '支持', 'marked supported');
excludes(badges, '不支持', 'and not unsupported');
includes(sentence, '支持范围内', 'said in words too');

// Supported, but a list is missing: said next to the verdict, not instead of it.
[title, badges, sentence] = page.profileVerdict(ros({status: 'incomplete', services: null}));
check(title, 'ROS 2 Humble', 'still Humble');
includes(badges, '没有读全', 'the gap is flagged');

// Another ROS 2 distribution: unsupported, its lists for reference only.
[title, badges, sentence] = page.profileVerdict(ros({status: 'unsupported', distro: 'foxy', supported: false}));
check(title, 'ROS 2 Foxy', 'the other distribution is named');
includes(badges, '不支持', 'marked unsupported');
includes(sentence, '只支持 Humble', 'what is supported');
includes(page.profileLists(ros({status: 'unsupported', distro: 'foxy', supported: false}), 'topics', ''), '/cmd_vel', 'its topics are still listed');

// ROS 1: unsupported, and nothing was listed.
const ros1 = ros({status: 'unsupported', distro: 'noetic', generation: 1, supported: false, ...unread});
[title, badges, sentence] = page.profileVerdict(ros1);
check(title, 'ROS 1 Noetic', 'ROS 1 is named as such');
includes(sentence, '没有读取', 'nothing was read');
check(page.profileLists(ros1, 'topics', ''), '', 'no lists are offered for ROS 1');

// No ROS on the device: stated, not a failure.
const absent = ros({status: 'absent', found: false, distro: null, generation: null, supported: null, installed: [], ...unread});
[title, badges, sentence] = page.profileVerdict(absent);
check(title, '没有读到 ROS 2', 'the absence is the headline');
includes(sentence, '不算故障', 'not a failure');
check(page.profileLists(absent, 'topics', ''), '', 'no lists without ROS 2');
excludes(page.profileRos2(absent, 'topics', ''), 'notice warn', 'no warning for a device without ROS 2');

// Never read: the reason is shown, nothing is made up.
const never = ros({status: 'not_read', found: null, distro: null, generation: null, supported: null, installed: null, environment: null,
                   ...unread, complete: false, checked_at: null, duration_ms: null, problem: 'ROS 2 的读取没有完成'});
let html = page.profileRos2(never, 'topics', '');
includes(html, 'ROS 2 的读取没有完成', 'why it was not read');
includes(html, '还没有读到 ROS 2', 'said plainly');
excludes(html, 'dp-list-bar', 'no lists');
excludes(html, '读取于', 'no time for a read that did not happen');

// A kept part after a read that failed: the earlier lists with their own time, and the reason.
html = page.profileRos2(ros({problem: 'ROS 2 的读取没有完成'}), 'topics', '');
includes(html, 'ROS 2 的读取没有完成', 'the reason');
includes(html, '读到的结果', 'what follows is older');
includes(html, '/cmd_vel', 'the earlier list is shown');

// The lists.
html = page.profileListTable(ros(), 'topics', '');
includes(html, '/cmd_vel', 'topic names');
includes(html, 'geometry_msgs/msg/Twist', 'with their message types');
includes(html, '多种类型', 'a topic with two types is pointed out');
check((html.match(/<tr>/g) || []).length, 4, 'a header row and one row per topic');
includes(page.profileListTable(ros(), 'nodes', ''), '/chassis_bridge', 'nodes have no type column');
excludes(page.profileListTable(ros(), 'nodes', ''), '<th></th>', 'no empty column for nodes');
includes(page.profileListTable(ros(), 'actions', ''), 'robot_interfaces/action/Dock', 'actions with their types');
includes(page.profileListTable(ros({topics: null}), 'topics', ''), '没有读到不等于没有', 'not read is not empty');
includes(page.profileListTable(ros({nodes: []}), 'nodes', ''), '没有发现正在运行的节点', 'an empty list is said as such');
includes(page.profileLists(ros({services: null}), 'topics', ''), '没读到', 'the chip of a list that was not read');
excludes(page.profileLists(ros({topics: null}), 'topics', ''), 'dp-search', 'nothing to search in a list that was not read');
includes(page.profileLists(ros(), 'topics', ''), 'dp-search', 'a list with rows can be searched');

// The search looks at names and types, whatever the case.
check(page.profileMatches(topics, 'SCAN').map(r => r.name), ['/scan'], 'by name, any case');
check(page.profileMatches(topics, 'twist').map(r => r.name), ['/cmd_vel'], 'by message type');
check(page.profileMatches(topics, '  ').length, 3, 'blank matches all');
includes(page.profileListTable(ros(), 'topics', '<img>'), '&lt;img&gt;', 'what was typed is shown as text');

// A very long list is drawn in part and says so.
const many = Array.from({length: page.PROFILE_ROWS_SHOWN + 25}, (_, i) => ({name: `/bus/channel_${i}`, types: ['std_msgs/msg/String']}));
html = page.profileListTable(ros({topics: many}), 'topics', '');
includes(html, `共 ${many.length} 项`, 'the total');
check((html.match(/<tr>/g) || []).length, page.PROFILE_ROWS_SHOWN + 1, 'only the first rows are drawn');

// Text that came from the device is never markup.
html = page.profileRos2(ros({notes: ['读取节点的命令失败（退出码 1）：<script>alert(1)</script>']}), 'topics', '');
excludes(html, '<script>', 'a note is shown as text');
includes(html, '&lt;script&gt;', 'escaped');

// The tabs flag what needs a look.
const profile = (part, summary = {devices: 17, attention: 0}) => ({ros2: part, hardware: {summary}, resources: {}});
includes(page.profileTabs(profile(ros({status: 'unsupported', distro: 'foxy', supported: false})), 'ros2'), '不支持', 'unsupported is flagged on the tab');
excludes(page.profileTabs(profile(ros()), 'ros2'), 'ls-flag', 'nothing to flag when all is well');
includes(page.profileTabs(profile(ros(), {devices: 17, attention: 5}), 'ros2'), '5 项需处理', 'hardware problems are flagged');
includes(page.profileTabs(profile(absent, null), 'ros2'), '没有读到', 'no ROS 2 is flagged softly');

// Only the card of a real robot with an SSH connection gets the button.
const status = sandbox.button('实时状态', 'engine-status', 'secondary small', 'robot-1');
const card = `<article class="card"><div class="button-row">${status}</div></article>`;
includes(page.profileCardButton(card, {id: 'robot-1', connection_id: 'connection-1'}), 'data-action="engine-profile"', 'a connected real robot');
check(page.profileCardButton(card, {id: 'robot-1', connection_id: 'connection-1', sim: {}}), card, 'not a simulated robot');
check(page.profileCardButton(card, {id: 'robot-1'}), card, 'not a robot without an SSH connection');

console.log(`device profile logic: ${checks} checks passed`);
