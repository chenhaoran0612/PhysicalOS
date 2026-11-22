// Pure-logic checks of web/repair_driver.js (issue #29): what an approval of a repair that installs and
// starts a motion driver shows (the product's ROS 2 lines, the start of the driver with the on-site tick,
// a library package being sent), and that approving the start sends the on-site confirmation first and is
// refused without the tick. The rules themselves are the backend's (tests/test_repair_driver_rules.py).
//   node tests/repair_driver_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const element = {addEventListener: () => {}, dataset: {}, open: false, close: () => {}};
const calls = [];
const sandbox = {
  h, $: () => element, document: {addEventListener: () => {}, querySelectorAll: () => []}, setInterval: () => 0,
  engineRobotCard: () => '', engineAction: async () => {}, engineRefresh: () => {}, dialog: () => {}, render: () => {},
  state: {}, projectId: 'project-1', toast: () => {}, refresh: async () => {}, calls,
  api: async (path, body) => { calls.push(['api', path, body]); return {}; },
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button type="button" class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`,
};
vm.createContext(sandbox);
// web/hardware.js is loaded for real (hardwareFixApproval, hwFind, the engineApproval wrapper); repair_policy.js is stood in for.
vm.runInContext('const rows = k => state[k] || []; const find = (k, id) => rows(k).find(x => x.id === id);' +
                'const engineActive = s => ["starting", "running"].includes(s);' +
                'const RP_AUTO_METHODS = ["device_fix/requestApproval", "device_exec/requestApproval"];' +
                'const rpApprovalChoice = s => RP_AUTO_METHODS.includes(s.pending_approval?.method) ? "<label class=\\"rp-choice\\">之后默认允许执行</label>" : "";' +
                'const rpSessionLine = () => "";' +
                'let engineApproval = () => "<base-approval>", engineSessionBody = () => "";' +
                'let engineSubmit = async (kind, p, f) => { calls.push(["submit", kind, p.decision]); return {}; };', sandbox);
vm.runInContext(readFileSync('web/hardware.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/repair_driver.js', 'utf8') +
  '\n;({engineApproval: s => engineApproval(s), engineSubmit: (...a) => engineSubmit(...a), RP_AUTO_METHODS, rdIsStart, RD_NOT_TICKED})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 700))}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 700))}`); checks += 1; };

const VELOCITY = 'motion-driver:base.velocity';
sandbox.state = {hardware_scans: [{id: 'scan-1', issues: [{id: VELOCITY, title: '缺少底盘速度接口'}]}]};
const fix = (changes = {}) => ({command: 'systemctl start vendor-motion-driver', purpose: '启动底盘的运控驱动', issue_ids: [VELOCITY], risk: 'medium',
  rollback: 'systemctl stop vendor-motion-driver', needs_root: true, sudo: 'sudo_password', timeout_seconds: 120, warnings: [],
  environment: [], starts_motion_driver: false, username: 'robot', host: '192.0.2.10', port: 22, ...changes});
const session = (method, request, id = 7) => ({id: 'session-1', mode: 'repair', hardware: {scan_id: 'scan-1'},
  pending_approval: {id, method, request}});

// ---- the start of the motion driver -------------------------------------------------------------------
const start = session('device_fix/requestApproval', fix({starts_motion_driver: true, nonce: 'driver-start-1'}));
const startHtml = page.engineApproval(start);
includes(startHtml, 'name="onsite"', 'the start asks for the on-site tick');
includes(startHtml, '我在机器人旁边', 'in the words the backend records');
includes(startHtml, '上电', 'what the body may do is said');
includes(startHtml, '开了“默认允许执行”也一样', 'and that it waits whatever the mode');
includes(startHtml, 'systemctl start vendor-motion-driver', 'the command is shown');
includes(startHtml, '之后默认允许执行', 'the choice of the session is still offered');
assert.ok(startHtml.indexOf('rd-start') > startHtml.indexOf('hw-command'), 'the tick comes after the command'); checks += 1;
check(page.rdIsStart(start), true, 'recognised as the start');
const plain = session('device_fix/requestApproval', fix({command: 'systemctl restart nvargus-daemon'}));
excludes(page.engineApproval(plain), 'name="onsite"', 'any other command has no on-site tick');
check(page.rdIsStart(plain), false, 'not the start');
check(page.rdIsStart(session('device_exec/requestApproval', {command: 'x', starts_motion_driver: true})), false, 'only a repair command can be the start');

// ---- the product's ROS 2 lines --------------------------------------------------------------------------
const env = session('device_fix/requestApproval', fix({command: 'ros2 pkg list', environment: ['. /opt/ros/humble/setup.sh', ". '/home/robot/ws/install/setup.sh'"]}));
const envHtml = page.engineApproval(env);
includes(envHtml, '先加载 ROS 2 环境', 'the lines the product runs first are shown');
includes(envHtml, '. /opt/ros/humble/setup.sh\n. &#39;/home/robot/ws/install/setup.sh&#39;', 'in order, escaped');
assert.ok(envHtml.indexOf('rd-env') > envHtml.indexOf('hw-command-size') && envHtml.indexOf('rd-env') < envHtml.indexOf('runtime-list'),
          'right under the command'); checks += 1;
excludes(page.engineApproval(plain), 'rd-env', 'nothing is added when the environment was not asked for');

// ---- a package from the library ------------------------------------------------------------------------
check(page.RP_AUTO_METHODS.includes('driver_package_deliver/requestApproval'), true, 'sending a package follows the session mode');
const deliver = session('driver_package_deliver/requestApproval', {material_id: 'mat_0123456789abcdef0123', purpose: '把厂家驱动包送到设备',
  issue_ids: [VELOCITY], filename: 'vendor<img src=x>.deb', size: 2 * 1024 * 1024, sha256: 'a'.repeat(64), labels: {vendor: '示例厂家', model: 'X1'},
  device_target: '~/neuxmind-pkg/library/aaaa/vendor_img_src_x_.deb', username: 'robot', host: '192.0.2.10', port: 22});
const deliverHtml = page.engineApproval(deliver);
includes(deliverHtml, '把资料库里的驱动安装包送到机器人', 'the step is named');
includes(deliverHtml, 'vendor&lt;img src=x&gt;.deb', 'a name from the library is shown as text');
excludes(deliverHtml, '<img', 'and never as markup');
includes(deliverHtml, 'a'.repeat(64), 'the checksum is shown in full');
includes(deliverHtml, '~/neuxmind-pkg/library/aaaa/vendor_img_src_x_.deb', 'and where it goes');
includes(deliverHtml, '2.0 MB', 'its size');
includes(deliverHtml, '示例厂家 · X1', 'its labels');
includes(deliverHtml, '不解压、不安装、不执行', 'what this step does not do');
includes(deliverHtml, '缺少底盘速度接口', 'the problem it is for');
includes(deliverHtml, '之后默认允许执行', 'the session choice is offered');
check(page.engineApproval(session('item/commandExecution/requestApproval', {})), '<base-approval>', 'other approvals are left as they were');

// ---- approving --------------------------------------------------------------------------------------------
const form = (sessionId, ticked) => {
  const marked = [];
  return {dataset: {id: sessionId}, elements: {onsite: ticked === undefined ? undefined : {checked: ticked}},
          querySelector: () => ({classList: {add: c => marked.push(c)}}), marked};
};
(async () => {
  sandbox.state.agent_sessions = [start];
  calls.length = 0;
  const unticked = form('session-1', false);
  await assert.rejects(page.engineSubmit('engine-approval', {decision: 'accept'}, unticked), {message: page.RD_NOT_TICKED});
  check(calls, [], 'without the tick nothing is sent: no confirmation, no approval');
  check(unticked.marked, ['needs-confirm'], 'the tick is pointed at');
  await page.engineSubmit('engine-approval', {decision: 'accept'}, form('session-1', true));
  check(JSON.parse(JSON.stringify(calls)), [['api', 'engineering/hardware/onsite', {project_id: 'project-1', session_id: 'session-1', approval_id: 7, onsite: true}],
                                            ['submit', 'engine-approval', 'accept']], 'the confirmation first, the approval after it, the id as a number');
  calls.length = 0;
  await page.engineSubmit('engine-approval', {decision: 'decline'}, form('session-1', false));
  check(JSON.parse(JSON.stringify(calls)), [['submit', 'engine-approval', 'decline']], 'declining needs no tick');
  sandbox.state.agent_sessions = [plain];
  calls.length = 0;
  await page.engineSubmit('engine-approval', {decision: 'accept'}, form('session-1'));
  check(JSON.parse(JSON.stringify(calls)), [['submit', 'engine-approval', 'accept']], 'any other command is approved as before');
  calls.length = 0;
  await page.engineSubmit('engine-answer', {}, form('session-1'));
  check(JSON.parse(JSON.stringify(calls)), [['submit', 'engine-answer', null]], 'other forms are passed on');
  console.log(`repair_driver_logic: ${checks} checks passed`);
})().catch(err => { console.error(err); process.exit(1); });
