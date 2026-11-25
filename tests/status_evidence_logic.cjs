// Pure-logic checks of issue #47 in web/hardware.js: what a robot's card and the hardware page say about a
// scan. Only a fault (seen not working, and saying what it blocks) is loud; what is to be confirmed is a
// plain tag; notes are not counted. The grading itself is the backend's (tests/test_status_evidence.py).
//   node tests/status_evidence_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const element = {addEventListener: () => {}, dataset: {}, open: false, close: () => {}};
const CARD = '<article class="card"><div class="tags"></div><div class="card-bottom"><div class="button-row"></div></div></article>';
const sandbox = {
  h, $: () => element, document: {addEventListener: () => {}, querySelectorAll: () => []},
  engineRobotCard: () => CARD, engineRefresh: () => {}, dialog: () => {}, render: () => {},
  state: {}, projectId: 'project-1', project: () => ({id: 'project-1'}), find: () => null,
  api: async () => ({}), refresh: async () => {}, toast: () => {},
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button type="button" class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}">${text}</div>`,
};
vm.createContext(sandbox);
vm.runInContext('const rpLocked = () => false; const rpSignature = () => ""; const rpSelectable = () => true; const rpStrip = () => "";' +
                'const rows = k => state[k] || []; const engineActive = () => false;' +
                'let engineApproval = () => "", engineSessionBody = () => "";', sandbox);
const page = vm.runInContext(readFileSync('web/hardware.js', 'utf8') +
  '\n;({hardwareCardTag, hwMetrics, hwIssues, hwIssueRow, HW_RULE})', sandbox);

let checks = 0;
const ok = (value, what) => { assert.ok(value, what); checks += 1; };
const includes = (text, part, what) => ok(String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 500))}`);
const lacks = (text, part, what) => ok(!String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 500))}`);

const issue = (id, severity, changes = {}) => ({id, severity, category: 'network', title: '标题 ' + id, detail: '依据 ' + id, hint: '',
                                               device_id: null, fixable: 'auto', source: 'scan', blocks: '', ...changes});
const robot = {id: 'robot-1', name: 'Rover', connection_id: 'c1'};
const scan = issues => ({id: 'scan-1', project_id: 'project-1', robot_id: 'robot-1', created_at: '2026-11-25T00:00:00+00:00',
                         duration_ms: 900, notes: [], devices: [], issues,
                         summary: {devices: 24, ok: 17, attention: 0, errors: issues.filter(i => i.severity === 'error').length,
                                   warnings: issues.filter(i => i.severity === 'warning').length, categories: {}}});
const show = issues => { const record = scan(issues); sandbox.state = {hardware_scans: [record]}; return record; };
const NOTES = ['net-noip:eth1', 'net-down:wlan1', 'video-perm:/dev/video0', 'serial-perm:/dev/ttyUSB0', 'log-probe:x', 'log-firmware:y']
  .map(id => issue(id, 'info'));
const FAULT = issue('rootfs-full', 'error', {category: 'storage', blocks: '往这台设备上安装管理层、技能和驱动'});
const LOUD = /class="status (bad|warn)/;

// ---- the card
{
  sandbox.state = {hardware_scans: []};
  includes(page.hardwareCardTag(robot), '硬件待扫描', 'no scan yet');

  show(NOTES);                                           // what issue #47's robot had: notes only
  let tag = page.hardwareCardTag(robot);
  includes(tag, '硬件 24 项 · 未发现故障', 'notes are not counted');
  includes(tag, 'class="status good"', 'and the card is calm');
  lacks(tag, '个问题', 'nothing is called a problem');

  show([...NOTES, issue('pci-driver:0008:01:00.0', 'warning'), issue('net-route', 'warning')]);
  tag = page.hardwareCardTag(robot);
  includes(tag, '<span class="tag">硬件 24 项 · 2 项待确认</span>', 'what is to be confirmed is a plain tag with its count');
  ok(!LOUD.test(tag), 'which is neither red nor amber');

  show([...NOTES, issue('net-route', 'warning'), FAULT]);
  tag = page.hardwareCardTag(robot);
  includes(tag, '硬件故障 1 项', 'a fault is counted on its own');
  includes(tag, 'class="status bad"', 'and is the only thing that is red');
}

// ---- the hardware page
{
  let record = show(NOTES);
  includes(page.hwMetrics(record), '<div class="good"><b>0</b><span>故障</span></div>', 'no fault: the count says so');
  let list = page.hwIssues(record, 'robot-1', false);
  includes(list, '>提示 <span>6</span>', 'notes are listed as notes');
  lacks(list, '需要处理的问题', 'not as problems to handle');
  includes(list, h(page.HW_RULE), 'the rule for calling something a fault is on the page');
  lacks(list, ' checked', 'a note is never ticked in advance');

  record = show([issue('net-route', 'warning'), ...NOTES]);
  includes(page.hwMetrics(record), '<b>0</b><span>故障 · 另有 1 项待确认</span>', 'to be confirmed is said next to the faults');
  includes(page.hwIssues(record, 'robot-1', false), '>待确认 <span>1</span>', 'the list is headed by what it holds');

  record = show([FAULT, issue('net-route', 'warning'), ...NOTES]);
  includes(page.hwMetrics(record), '<div class="bad"><b>1</b><span>故障 · 另有 1 项待确认</span></div>', 'a fault is counted');
  list = page.hwIssues(record, 'robot-1', false);
  includes(list, '>故障 <span>1</span><small>另有 1 项待确认</small>', 'faults head the list');
  const row = page.hwIssueRow(FAULT, 'robot-1', false);
  includes(row, '故障', 'a fault is called one');
  includes(row, '依据 rootfs-full', 'with what was seen');
  includes(row, '<span class="hw-issue-blocks">阻塞：往这台设备上安装管理层、技能和驱动</span>', 'and what it blocks');
  lacks(page.hwIssueRow(issue('net-route', 'warning'), 'robot-1', false), 'hw-issue-blocks', 'nothing else claims to block anything');
  includes(page.hwIssueRow(issue('net-route', 'warning'), 'robot-1', false), '待确认', 'a warning is "to be confirmed"');
  lacks(page.hwIssueRow(issue('x', 'warning', {blocks: '<img src=x>'}), 'robot-1', false), '<img', 'texts from a device are escaped');
}

console.log(`status_evidence_logic: ${checks} checks passed`);
