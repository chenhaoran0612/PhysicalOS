// Pure-logic checks of web/motion_drivers.js (issue #28): which robot cards get the entry (only those
// that lack a motion interface), what the dialog says in each state of the check, when the problems can
// be handed to the assistant, and the line it adds to the device profile. The comparison itself is the
// backend's (tests/test_motion_drivers.py); the page as a whole is in tests/motion_drivers_browser.cjs.
//   node tests/motion_drivers_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written here as they are there; web/live_status.js and web/device_profile.js are
// loaded for real, with stand-ins for the page they attach to.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const element = {addEventListener: () => {}, dataset: {}, open: false, close: () => {}};
const CARD = '<article class="card"><div class="tags"><span class="tag">x</span></div>' +
  '<div class="card-bottom"><div class="button-row"><button>实时状态</button></div></div></article>';
const sandbox = {
  h, $: () => element, document: {addEventListener: () => {}, querySelectorAll: () => []}, setInterval: () => 0,
  engineRobotCard: () => CARD, engineAction: async () => {}, engineRefresh: () => {}, dialog: () => {}, render: () => {},
  state: {}, projectId: 'project-1', project: () => ({id: 'project-1'}),
  find: () => null, api: async () => ({}), refresh: async () => {}, toast: () => {},
  locked: new Set(),
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
  button: (text, action, cls = '', id = '', disabled = false) =>
    `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`,
  table: (headers, data) => `<table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table>`,
};
vm.createContext(sandbox);
// web/hardware.js is loaded for real: its problem rows are wrapped, and hwRunning / hwLatest read the state
// set below. repair_policy.js is stood in for (`locked` holds the problems that wait for on-site work).
vm.runInContext('const rpLocked = (robotId, issueId) => locked.has(issueId); const rpSignature = () => [...locked].join(",");' +
                'const rpSelectable = () => true; const rpStrip = () => "";' +
                'const rows = k => state[k] || []; const engineActive = s => ["starting", "running"].includes(s);' +
                'let engineApproval = () => "", engineSessionBody = () => "";', sandbox);
vm.runInContext(readFileSync('web/hardware.js', 'utf8'), sandbox);
vm.runInContext(readFileSync('web/live_status.js', 'utf8'), sandbox);
vm.runInContext(readFileSync('web/device_profile.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/motion_drivers.js', 'utf8') +
  '\n;({mdCard, mdSummary, mdBody, mdBanner, mdProfileBody, mdSameIds, mdIssueIds, mdLacking, profileBody, engineRobotCard,' +
  ' issueRow: (...a) => hwIssueRow(...a)})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 600))}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 600))}`); checks += 1; };

const item = (capability, title, changes = {}) => ({
  capability, title, kind: 'topic', role: 'subscribes', name: '/cmd_vel', types: ['geometry_msgs/msg/Twist'], note: '',
  state: 'present', found: [{name: '/cmd_vel', types: ['geometry_msgs/msg/Twist']}], found_total: 1, ...changes});
const stop = item('safety.stop', '停止', {kind: null, role: null, name: null, types: [], state: 'unverified', found: [], found_total: 0,
                                         note: '停止没有自己的 ROS 2 接口'});
const velocity = item('base.velocity', '底盘速度');
const odometry = item('base.odometry', '里程计', {role: 'publishes', name: '/odom', types: ['nav_msgs/msg/Odometry'],
                                                 found: [{name: '/odom', types: ['nav_msgs/msg/Odometry']}]});
const lacking = (base, state = 'missing', found = []) =>
  ({...base, state, found, found_total: found.length, issue_id: 'motion-driver:' + base.capability});
const result = (status, items, changes = {}) => ({
  robot_id: 'robot-1', project_id: 'project-1', family: 'quadruped', status, reason: '', cause: '',
  body_type: {id: 'quadruped', name: '四足机器人'}, items, checked_at: 1763700000, problem: null, ...changes});
const robot = {id: 'robot-1', name: 'Rover', connection_id: 'connection-1', family: 'quadruped'};
const show = (checks_, sessions = []) => { sandbox.state = {motion_driver_checks: checks_, agent_sessions: sessions}; };
const ui = {robotId: 'robot-1', busy: false, failure: ''};

const missing = result('missing', [stop, lacking(velocity), lacking(odometry)]);
const complete = result('ok', [stop, velocity, odometry]);
const unlisted = result('unlisted_type', [], {family: 'mobile', body_type: null,
                                               reason: '这台设备的类型“移动机器人”不在本体类型清单里，平台不知道它应该有哪些运控接口，所以没有核对。先把它改成清单里的类型，再读取设备档案。'});
const unread = result('not_read', [{...velocity, state: 'unchecked', found: [], found_total: 0}],
                      {checked_at: null, reason: '还没有读取这台设备的档案。'});
const unknown = result('unknown', [{...velocity, state: 'unchecked', found: [], found_total: 0}],
                       {reason: 'ROS 2 的清单不完整。这一次没有核对运控驱动。重新读取设备档案后再核对。'});

// ---- the card: an entry only while something is missing
show([missing]);
let card = page.mdCard(CARD, robot);
includes(card, '运控驱动缺 2 项', 'the card of a device that lacks interfaces says how many');
includes(card, 'data-action="engine-drivers" data-id="robot-1"', 'and has the button that opens the check');
includes(card, '<button>实时状态</button>', 'the card keeps what it had');
for (const [name, value] of [['with its drivers', complete], ['of a type outside the list', unlisted], ['without a profile', unread],
                             ['whose listing could not be judged', unknown]]) {
  show([value]);
  check(page.mdCard(CARD, robot), CARD, `a device ${name} gets no entry`);
}
show([missing]);
check(page.mdCard(CARD, {...robot, sim: {kind: 'quadruped'}}), CARD, 'a simulated robot gets no entry');
check(page.mdCard(CARD, {...robot, connection_id: null}), CARD, 'a robot without SSH gets no entry');
show([]);
check(page.mdCard(CARD, robot), CARD, 'no check, no entry');
show([missing]);
includes(page.engineRobotCard(robot), 'engine-drivers', 'the shared card is wrapped');

// ---- one sentence per state
includes(page.mdSummary(missing), '缺 2 项运控接口：底盘速度、里程计', 'missing: how many and which');
includes(page.mdSummary(missing), '四足机器人', 'missing: by which type');
includes(page.mdSummary(complete), '应有的 2 项运控接口都在', 'complete: what was checked');
includes(page.mdSummary(complete), '另有 1 项平台核对不了（停止）', 'complete: what could not be checked is said');
check(page.mdSummary(unlisted), h(unlisted.reason), 'a type outside the list: the reason as given');
includes(page.mdSummary(result('missing', [lacking(velocity)], {body_type: {id: 't', name: '<img src=x>'}})), '&lt;img src=x&gt;',
         'the name of a type is written by a person: it is escaped');

// ---- the dialog
show([missing]);
let body = page.mdBody(robot, missing, ui);
includes(body, '话题 <span class="dp-name">/cmd_vel</span>', 'the expected topic is named');
includes(body, '<span class="dp-type">geometry_msgs/msg/Twist</span>', 'with its message type');
includes(body, '<span class="dp-type">nav_msgs/msg/Odometry</span>', 'for every missing interface');
includes(body, '缺少', 'missing rows say so');
includes(body, '停止没有自己的 ROS 2 接口', 'what cannot be checked says why');
includes(body, '核对不了', 'and is marked as not checked, not as missing');
includes(body, 'data-action="engine-drivers-repair" data-id="robot-1" >交给修复助手', 'the problems can be handed to the assistant');
includes(body, '启动驱动那一步一定要有人在现场确认', 'what handing it over leads to is said before the click (issue #29)');
includes(body, '重新核对', 'and checked again');
includes(body, '不发布运动指令', 'the dialog says the check is read-only');
includes(body, 'ROS 2 清单读取于', 'and when the listing was read');

body = page.mdBody(robot, complete, ui);
excludes(body, 'engine-drivers-repair', 'nothing to hand over when nothing is missing');
excludes(body, '修复助手', 'and nothing about the assistant');
includes(body, '/cmd_vel', 'the interface that was found is named');

const stamped = result('missing', [lacking(velocity, 'wrong_type', [{name: '/cmd_vel', types: ['geometry_msgs/msg/TwistStamped']}])]);
body = page.mdBody(robot, stamped, ui);
includes(body, '类型不对', 'a wrong message type is its own state');
includes(body, '/cmd_vel 的类型是 geometry_msgs/msg/TwistStamped', 'with the type the device has');

const arm = result('missing', [lacking(item('arm.joint_position', '机械臂关节位置', {
  kind: 'action', role: 'serves', name: '/joint_trajectory_controller/follow_joint_trajectory', types: ['control_msgs/action/FollowJointTrajectory']}))],
                   {family: 'manipulator', body_type: {id: 'manipulator', name: '机械臂'}});
body = page.mdBody(robot, arm, ui);
includes(body, '动作 <span class="dp-name">/joint_trajectory_controller/follow_joint_trajectory</span>', 'an action is called an action');
includes(body, 'control_msgs/action/FollowJointTrajectory', 'with its type');

body = page.mdBody(robot, unlisted, ui);
includes(body, 'data-action="engine-body-type" data-id="robot-1"', 'a type outside the list: the way to change it');
excludes(body, '<table>', 'and no table of guesses');
excludes(body, 'engine-drivers-repair', 'nor anything to hand over');
body = page.mdBody(robot, unread, ui);
includes(body, 'data-action="engine-profile" data-id="robot-1" >读取设备档案', 'no profile yet: the way to read one');
body = page.mdBody(robot, unknown, ui);
includes(body, 'ROS 2 的清单不完整', 'a listing that cannot be judged: the reason');
excludes(body, 'engine-drivers-repair', 'and nothing to hand over');

body = page.mdBody(robot, {...missing, problem: 'ROS 2 的读取没有完成'}, ui);
includes(body, '最近一次读取 ROS 2 没有成功（ROS 2 的读取没有完成）', 'a failed read: what is shown is the earlier listing');
body = page.mdBody(robot, missing, {...ui, failure: 'SSH 连不上这台机器人'});
includes(body, '这一次没有重新读到设备档案：SSH 连不上这台机器人', 'a recheck that failed says so');
body = page.mdBody(robot, missing, {...ui, busy: true});
includes(body, 'data-action="engine-drivers-repair" data-id="robot-1" disabled', 'nothing is started while the profile is read');
includes(body, '正在重新读取…', 'and the button says what is going on');

// Problems the assistant could not solve wait for a person (issue #13): they are not handed over again.
sandbox.locked = new Set(['motion-driver:base.velocity', 'motion-driver:base.odometry']);
body = page.mdBody(robot, missing, ui);
includes(body, 'data-action="engine-drivers-repair" data-id="robot-1" disabled', 'all of them wait: no hand-over');
includes(body, '等待现场处理', 'the rows say they wait');
includes(body, 'data-action="hardware-open" data-id="robot-1"', 'and the hardware page with the notes is one click away');
sandbox.locked = new Set(['motion-driver:base.odometry']);
body = page.mdBody(robot, missing, ui);
includes(body, 'data-action="engine-drivers-repair" data-id="robot-1" >', 'one of two waits: the other can still be handed over');
sandbox.locked = new Set();
show([missing], [{id: 'agent-session-1', robot_id: 'robot-1', mode: 'repair', status: 'running'}]);
body = page.mdBody(robot, missing, ui);
includes(body, 'data-action="engine-drivers-repair" data-id="robot-1" disabled', 'a repair is running: no second one');
includes(body, 'data-action="engine-session" data-id="agent-session-1"', 'its progress is one click away');
show([missing], [{id: 'agent-session-2', robot_id: 'robot-1', mode: 'repair', status: 'completed'}]);
includes(page.mdBody(robot, missing, ui), 'data-action="engine-drivers-repair" data-id="robot-1" >', 'a finished repair does not hold it back');
show([missing]);

// ---- the hardware page: these problems are not ticked in advance and not called "可远程处理"
const scanIssue = (id, changes = {}) => ({id, severity: 'error', category: 'serial', title: id, detail: 'd', hint: '',
                                         device_id: null, fixable: 'auto', source: 'scan', ...changes});
let hwRow = page.issueRow(scanIssue('motion-driver:base.velocity', {category: 'motion', source: 'ros2_profile'}), 'robot-1', false);
excludes(hwRow, ' checked', 'a motion driver problem is not ticked in advance');
includes(hwRow, '助手可安装，启动时要人在现场', 'and says what the assistant does with it (issue #29)');
excludes(hwRow, '可远程处理', 'instead of "可远程处理"');
excludes(hwRow, 'disabled', 'it can still be ticked');
hwRow = page.issueRow(scanIssue('serial-perm:/dev/ttyUSB0'), 'robot-1', false);
includes(hwRow, 'value="serial-perm:/dev/ttyUSB0" checked', 'other problems are ticked as before');
includes(hwRow, '可远程处理', 'and keep their tag');
sandbox.locked = new Set(['motion-driver:base.velocity']);
hwRow = page.issueRow(scanIssue('motion-driver:base.velocity'), 'robot-1', false);
includes(hwRow, 'disabled', 'one that waits for on-site work stays locked');
sandbox.locked = new Set();

// What a device reports is shown as text.
const hostile = result('missing', [lacking(velocity, 'wrong_type', [{name: '/cmd_vel"><script>', types: ['<b>x</b>']}])]);
body = page.mdBody(robot, hostile, ui);
excludes(body, '<script>', 'a name from the device cannot become markup');
excludes(body, '<b>x</b>', 'nor can a type');

// ---- the line in the device profile
show([missing]);
includes(page.mdBanner('robot-1'), '查看并处理', 'missing: the profile offers to deal with it');
includes(page.mdBanner('robot-1'), '缺 2 项运控接口', 'and says what is missing');
show([complete]);
includes(page.mdBanner('robot-1'), '都在 ROS 2 清单里', 'complete: the profile says the interfaces are there');
excludes(page.mdBanner('robot-1'), 'engine-drivers-repair', 'and offers no repair');
show([unlisted]);
includes(page.mdBanner('robot-1'), 'data-action="engine-body-type"', 'a type outside the list: change it first');
show([unread]);
check(page.mdBanner('robot-1'), '', 'no profile, no line');
show([]);
check(page.mdBanner('robot-1'), '', 'no check, no line');

show([missing]);
const withTabs = '<div class="ls-bar"></div><div class="ls-tabs" role="tablist"></div>';
const placed = page.mdProfileBody(withTabs, {robotId: 'robot-1', profile: {}});
check(placed.indexOf('id="md-banner"') < placed.indexOf('<div class="ls-tabs"'), true, 'the line stands above the tabs');
check(placed.indexOf('id="md-banner"') > 0, true, 'and below the bar');
check(page.mdProfileBody(withTabs, {robotId: 'robot-1', profile: null}), withTabs, 'nothing is added while no profile is shown');

// ---- which scan can be reused for the hand-over
check(page.mdSameIds(['a', 'b'], ['b', 'a']), true, 'the same problems in another order');
check(page.mdSameIds(['a'], ['a', 'b']), false, 'one more');
check(page.mdSameIds([], []), true, 'none and none');
check(Array.from(page.mdIssueIds(missing)), ['motion-driver:base.velocity', 'motion-driver:base.odometry'], 'the ids the hardware page lists');

console.log(`motion_drivers_logic: ${checks} checks passed`);
