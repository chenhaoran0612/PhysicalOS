// Pure-logic checks of web/skill_run.js (issue #42): which robots get the "运行" button of an installed
// skill, what the run form asks a person to confirm on a real robot and on a simulated one, what it says
// when a real robot has no safety limits yet, and what is sent. The backend rule is in
// brainos/skill_run.py, checked in tests/test_skill_run.py.
//   node tests/skill_run_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const sandbox = {
  h,
  button: (text, action, cls = '', id = '', disabled = false) =>
    `<button type="button" class="button ${cls}" data-action="${action}" data-id="${h(id)}"${disabled ? ' disabled' : ''}>${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><div>${text}</div></div>`,
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/ux04_errors.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/skill_run.js', 'utf8') +
  '\n;({skillRunIsSim, skillRunnable, skillRunButton, skillRunConfirmHtml, skillRunLimitsNote, skillRunConfirmations,' +
  ' skillRunStatusButton, skillRunInstalledNote, uxClassify, UX_CODES})', sandbox);
let checks = 0;
const ok = (value, what) => { assert.ok(value, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => String(text).split(part).length - 1;

const sim = {id: 'robot-s', name: '仿真四足', status: 'connected', sim: {kind: 'quadruped', state: 'running'}};
const real = {id: 'robot-r', name: '巡检狗', status: 'connected', manifest: {simulated: false}};
const claimsSim = {id: 'robot-c', name: '别处的仿真', status: 'connected', manifest: {simulated: true}};
const offline = {...real, status: 'unconnected'};
const blocked = {...real, device_status: {safety_limits: {required: true, filled: false, motion_blocked: true}}};
const filled = {...real, device_status: {safety_limits: {required: true, filled: true, motion_blocked: false}}};
const skill = {id: 'goto_place', title: '去指定地点', compatible: true};

// ---- who counts as simulated
same([sim, real, claimsSim, offline, null].map(page.skillRunIsSim), [true, false, false, false, false], 'simulated: only what the console started itself');

// ---- the "运行" button of an installed skill: every connected robot, real ones too
same([sim, real, claimsSim, offline, null].map(page.skillRunnable), [true, true, true, false, false], 'a connected robot can run its installed skills');
const realButton = page.skillRunButton(real, skill);
ok(realButton.includes('data-action="sim-run"') && realButton.includes('data-id="robot-r|goto_place"'), 'a real robot gets the run button');
ok(!realButton.includes('disabled'), 'and it can be pressed');
ok(page.skillRunButton(real, {...skill, compatible: false}).includes('disabled'), 'a skill that does not fit the body cannot be run');
same(page.skillRunButton(offline, skill), '', 'no button while the robot is not connected');

// ---- what the form asks for
const simForm = page.skillRunConfirmHtml(sim), realForm = page.skillRunConfirmHtml(real);
same([count(simForm, 'type="checkbox"'), count(realForm, 'type="checkbox"')], [1, 2], 'one confirmation in simulation, two on a real robot');
ok(simForm.includes('name="hardware_authorized"') && !simForm.includes('onsite_confirmed'), 'simulation: the authorization only');
ok(simForm.includes('仿真'), 'simulation is called simulation');
ok(realForm.includes('name="hardware_authorized"') && realForm.includes('name="onsite_confirmed"'), 'real: the authorization and the on-site confirmation');
ok(count(realForm, ' required') === 2, 'both have to be ticked');
ok(realForm.includes('真实的机器人') && realForm.includes('急停'), 'real: says the robot will really move and names the emergency stop');
ok(!realForm.includes('仿真'), 'real: nothing about simulation');
same(count(page.skillRunConfirmHtml(claimsSim), 'type="checkbox"'), 2, 'a device that calls itself simulated cannot waive the on-site confirmation');

// ---- safety limits not filled in yet
const note = page.skillRunLimitsNote(blocked);
ok(note.includes('安全限值') && note.includes('data-action="engine-safety-limits-edit"') && note.includes('data-id="robot-r"'),
   'missing limits are said before the run, with the way to fill them in');
same([page.skillRunLimitsNote(filled), page.skillRunLimitsNote(real), page.skillRunLimitsNote(sim)], ['', '', ''], 'nothing to say otherwise');
ok(page.skillRunConfirmHtml(blocked).includes(note), 'the note is part of the form');

// ---- what is sent
const form = ticks => ({elements: Object.fromEntries(Object.entries(ticks).map(([name, checked]) => [name, {checked}]))});
same(page.skillRunConfirmations(form({hardware_authorized: true})), {hardware_authorized: true}, 'simulation sends the authorization');
same(page.skillRunConfirmations(form({hardware_authorized: true, onsite_confirmed: true})),
     {hardware_authorized: true, onsite_confirmed: true}, 'a real robot sends both');
same(page.skillRunConfirmations(form({hardware_authorized: false, onsite_confirmed: false})),
     {hardware_authorized: false, onsite_confirmed: false}, 'unticked boxes are sent as they are');

// ---- after installing, and the way to the run's state
ok(page.skillRunInstalledNote(real).includes('技能已安装') && page.skillRunInstalledNote(real).includes('现在可以'), 'installing leads straight to running');
ok(page.skillRunStatusButton(sim).includes('data-action="sim-live"') && page.skillRunStatusButton(sim).includes('实时状态'), 'simulation: 实时状态');
ok(page.skillRunStatusButton(real).includes('data-action="sim-live"') && page.skillRunStatusButton(real).includes('技能运行状态'), 'real: its own name');
same(page.skillRunStatusButton(offline), '', 'not while the robot is not connected');

// ---- the refusal of the backend reads as a safety step, not as a fault
const refusal = page.uxClassify(Object.assign(new Error('这是一台真实的机器人：运行技能前，需要确认现场有人看着它'),
  {status: 400, reason: 'onsite_confirmation_required', body: {code: 'onsite_confirmation_required'}}));
same([refusal.source, refusal.code, refusal.kind], ['code', 'onsite_confirmation_required', 'auth'], 'the refusal has its own explanation');
ok(!/内部出错|稍后重试|网络/.test(refusal.plain) && refusal.next.length > 0, 'and a next step');

console.log(`skill_run_logic: ${checks} checks passed`);
