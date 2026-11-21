// Pure-logic checks of web/safety_limits.js (issue #22, R1-06): what a robot's card says about its safety
// limits, what the form accepts before anything is sent, how a refusal of the device lands on its field,
// and what the detail section shows. The backend rules are in brainos/safety_limits.py, checked in
// tests/test_safety_limits.py.
//   node tests/safety_limits_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps functions of earlier scripts when it loads; stand-ins are enough here. The helpers of
// web/app.js it uses are written as they are there. web/ux04_errors.js is the real file: the script adds
// its own refusals to that file's table.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const element = () => ({addEventListener() {}, dataset: {}});
const sandbox = {
  engineRobotCard: () => '', engineAction: async () => {}, engineSubmit: async () => {}, robotDetailRender: () => {},
  dialog: () => {}, render: () => {}, h, state: {}, projectId: 'project-1',
  document: {addEventListener() {}}, $: element,
  badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button type="button" class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><div>${text}</div></div>`,
  errorBlock: error => `<span class="plain-error">${h(error.message)}</span>`,
};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/ux04_errors.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/safety_limits.js', 'utf8') +
  '\n;({safetyLimitsNumber, safetyLimitsSummary, safetyLimitsCardState, safetyLimitsCard, safetyLimitsParse,' +
  ' safetyLimitsRangeProblem, safetyLimitsCheck, safetyLimitsHint, safetyLimitsRefusal, safetyLimitsWho,' +
  ' safetyLimitsWhen, safetyLimitsSection, safetyLimitsFormFields, uxClassify, SAFETY_LIMITS_BLOCKED})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const tagsOf = html => [...html.matchAll(/<(\w+)/g)].map(m => m[1]);

const filled = {base: {max_linear_speed: 0.5, max_angular_speed: 0.8, command_timeout_s: 0.5}};
const bounds = {base: {
  max_linear_speed: {unit: 'm/s', min: 0, min_exclusive: true, max: 1, platform_max: 3, body_max: 1},
  max_angular_speed: {unit: 'rad/s', min: 0, min_exclusive: true, max: 1.5, platform_max: 3, body_max: 1.5},
  command_timeout_s: {unit: 's', min: 0.05, min_exclusive: false, max: 2, platform_max: 2, body_max: null}}};
const summaryOf = changes => ({required: true, filled: false, motion_blocked: true, reason: '这台设备还没有填写安全限值',
                               limits: null, set_by: null, set_at: null, ...changes});
const robotWith = (summary, changes = {}) =>
  ({id: 'robot-1', name: 'dog', endpoint: 'http://127.0.0.1:9', device_status: summary === undefined ? undefined : {safety_limits: summary}, ...changes});
const view = (changes = {}) => ({robot_id: 'robot-1', supported: true, ...summaryOf({}), revision: 0, effective: null, bounds,
                                 groups: ['base'], history: [], invalid: null, ...changes});
const filledView = (changes = {}) => view({filled: true, motion_blocked: false, reason: null, limits: filled, set_by: '张工',
                                           set_at: 1781000000, revision: 1, effective: filled, ...changes});

// ---------------------------------------------------------------- numbers and the one-line summary
check(page.safetyLimitsNumber(0.5), '0.5', 'a plain number');
check(page.safetyLimitsNumber(1), '1', 'no trailing zero');
check(page.safetyLimitsNumber(0.1 + 0.2), '0.3', 'no binary noise');
check(page.safetyLimitsSummary(filled), '限速 0.5 m/s · 0.8 rad/s · 指令超时 0.5 s', 'the summary line');
check(page.safetyLimitsSummary({base: {max_linear_speed: 1}}), '限速 1 m/s', 'only what is there');
check(page.safetyLimitsSummary(null), '', 'nothing filled: nothing to say');
check(page.safetyLimitsSummary({base: {max_linear_speed: '<b>'}}), '', 'what is not a number is not shown');

// ---------------------------------------------------------------- the card: badge or quiet line
check(page.SAFETY_LIMITS_BLOCKED, '安全限值未填，不能运动', 'the exact words of the requirement');
check(page.safetyLimitsCardState(robotWith(summaryOf({}))), {kind: 'blocked'}, 'not filled and required: cannot move');
check(page.safetyLimitsCardState(robotWith(summaryOf({filled: true, motion_blocked: false, limits: filled}))),
      {kind: 'filled', text: '限速 0.5 m/s · 0.8 rad/s · 指令超时 0.5 s'}, 'filled: the values');
check(page.safetyLimitsCardState(robotWith(summaryOf({required: false, motion_blocked: false}))), {kind: 'none'},
      'a simulated body that brings its own limits: nothing on the card');
check(page.safetyLimitsCardState(robotWith(undefined)), {kind: 'unknown'}, 'never checked: nothing is claimed');
check(page.safetyLimitsCardState({id: 'robot-1', device_status: {mode: 'idle'}}), {kind: 'unknown'}, 'an older device says nothing');
check(page.safetyLimitsCardState(robotWith('blocked')), {kind: 'unknown'}, 'a summary that is not one');
check(page.safetyLimitsCardState(robotWith(summaryOf({motion_blocked: 'yes'}))), {kind: 'none'}, 'only a literal true blocks');

const card = '<article class="card"><div class="card-body"><h3>dog</h3><div class="tags"><span class="tag">x</span></div></div>' +
  '<div class="card-bottom"><div class="button-row"><button>配置连接</button></div></div></article>';
let html = page.safetyLimitsCard(card, robotWith(summaryOf({}), {name: 'dog <b>'}));
includes(html, '<span class="status bad"><i class="dot"></i>安全限值未填，不能运动</span>', 'the badge, in the red the app has');
includes(html, 'data-action="engine-safety-limits-edit" data-id="robot-1"', 'the way to the form');
includes(html, 'aria-label="填写安全限值：dog &lt;b&gt;"', 'the button names its robot, escaped');
includes(html, '>填写安全限值</button></div><div class="tags">', 'above the tags');
check(tagsOf(html).filter(t => t === 'b').length, 0, 'no markup from a name');
html = page.safetyLimitsCard(card, robotWith(summaryOf({filled: true, motion_blocked: false, limits: filled})));
includes(html, '限速 0.5 m/s · 0.8 rad/s · 指令超时 0.5 s</div><div class="tags">', 'filled: a quiet line');
excludes(html, '不能运动', 'filled: no badge');
excludes(html, 'engine-safety-limits-edit', 'filled: the card stays quiet');
check(page.safetyLimitsCard(card, robotWith(undefined)), card, 'unknown: the card is left alone');
check(page.safetyLimitsCard(card, robotWith(summaryOf({required: false, motion_blocked: false}))), card, 'not required: left alone');
check(page.safetyLimitsCard('<article>other</article>', robotWith(summaryOf({}))), '<article>other</article>', 'an unknown card is left alone');
includes(page.safetyLimitsCard('<article class="card"><div class="card-body"></div><div class="card-bottom"></div></article>',
                              robotWith(summaryOf({}))),
         '填写安全限值</button></div><div class="card-bottom">', 'a card without tags still says it: above its buttons');
const simCard = '<article class="card sim-card"><div class="card-body"><div class="tags"></div></div></article>';
includes(page.safetyLimitsCard(simCard, robotWith(summaryOf({}), {sim: {kind: 'quadruped'}})), '安全限值未填，不能运动',
         'a simulated robot that needs limits says so too');

// ---------------------------------------------------------------- what was typed
check(page.safetyLimitsParse('0.5'), 0.5, 'a decimal');
check(page.safetyLimitsParse(' 1 '), 1, 'spaces around it');
check(page.safetyLimitsParse('.5'), 0.5, 'no leading zero');
check(page.safetyLimitsParse('２．５'), 2.5, 'full-width digits of a Chinese input method');
check(page.safetyLimitsParse('0。5'), 0.5, 'and its full stop');
check(page.safetyLimitsParse('-1'), -1, 'a sign is a number (the range says no)');
for (const text of ['', '  ', 'abc', '1,5', '1e3', '0.5 m/s', '1.2.3', 'Infinity', 'NaN', '0x10']) {
  check(page.safetyLimitsParse(text), null, `not a number: ${JSON.stringify(text)}`);
}

const linear = bounds.base.max_linear_speed, timeout = bounds.base.command_timeout_s;
check(page.safetyLimitsRangeProblem(0.5, linear), '', 'inside');
check(page.safetyLimitsRangeProblem(1, linear), '', 'the maximum itself is allowed');
check(page.safetyLimitsRangeProblem(1.01, linear), '不能超过 1 m/s（本体声明的上限）', 'above what the body declares');
check(page.safetyLimitsRangeProblem(0, linear), '需要大于 0 m/s', 'an exclusive minimum');
check(page.safetyLimitsRangeProblem(-1, linear), '需要大于 0 m/s', 'below it');
check(page.safetyLimitsRangeProblem(0.05, timeout), '', 'an inclusive minimum is allowed');
check(page.safetyLimitsRangeProblem(0.04, timeout), '不能小于 0.05 s', 'below an inclusive minimum');
check(page.safetyLimitsRangeProblem(2.5, timeout), '不能超过 2 s（平台上限）', 'above the platform maximum');
check(page.safetyLimitsRangeProblem(5, {...linear, body_max: 5, max: 3}), '不能超过 3 m/s（平台上限）', 'a body that declares more than the platform allows');
check(page.safetyLimitsRangeProblem(99, undefined), '', 'no range known: the device decides');

let result = page.safetyLimitsCheck({max_linear_speed: '0.5', max_angular_speed: '0.8', command_timeout_s: '0.5'}, bounds);
check(result, {values: filled.base, problems: []}, 'three good values');
result = page.safetyLimitsCheck({max_linear_speed: '', max_angular_speed: 'fast', command_timeout_s: '9'}, bounds);
check(result.problems, [{field: 'max_linear_speed', text: '请填写最大线速度'}, {field: 'max_angular_speed', text: '请填写一个数字'},
                        {field: 'command_timeout_s', text: '不能超过 2 s（平台上限）'}], 'each problem on its own field');
check(result.values, {}, 'nothing usable');
check(page.safetyLimitsCheck({}, bounds).problems.length, 3, 'nothing typed: three fields to fill');
check(page.safetyLimitsCheck({max_linear_speed: '9', max_angular_speed: '9', command_timeout_s: '9'}, null).problems, [],
      'without ranges only the device can refuse');

// ---------------------------------------------------------------- the hint under an input
check(page.safetyLimitsHint(linear), '允许范围：大于 0，不超过 1 m/s。上限来自本体声明的上限（平台上限 3 m/s）。', 'the body is lower');
check(page.safetyLimitsHint(timeout), '允许范围：不小于 0.05，不超过 2 s。上限来自平台上限。', 'the body declares none');
check(page.safetyLimitsHint({...linear, body_max: 5, max: 3}), '允许范围：大于 0，不超过 3 m/s。上限来自平台上限（本体声明的上限 5 m/s）。',
      'the platform is lower');
check(page.safetyLimitsHint(undefined), '', 'no range known: no hint');

// ---------------------------------------------------------------- a refusal lands on its field
let refusal = page.safetyLimitsRefusal({code: 'limits_out_of_range', field: 'max_linear_speed', value: 1.2, min: 0, max: 1, bound: 'body',
                                        error: '最大线速度填的是 1.2 m/s'}, bounds);
check(refusal, {field: 'max_linear_speed', text: '设备没有接受这个值：允许大于 0、不超过 1 m/s（本体声明的上限）'}, 'the device named the body');
refusal = page.safetyLimitsRefusal({code: 'limits_out_of_range', field: 'command_timeout_s', value: 5, min: 0.05, max: 2, bound: 'platform'}, bounds);
check(refusal.text, '设备没有接受这个值：允许不小于 0.05、不超过 2 s（平台上限）', 'the device named the platform');
// The range in a refusal is the device's and newer than the one the form was opened with.
refusal = page.safetyLimitsRefusal({code: 'limits_out_of_range', field: 'max_linear_speed', value: 0.9, min: 0, max: 0.6}, bounds);
check(refusal.text, '设备没有接受这个值：允许大于 0、不超过 0.6 m/s（本体声明的上限）', 'no source named: told from the ranges');
check(page.safetyLimitsRefusal({code: 'limits_out_of_range', field: 'base.max_angular_speed', max: 1.5, min: 0}, bounds).field,
      'max_angular_speed', 'a field named with its group');
refusal = page.safetyLimitsRefusal({code: 'limits_invalid', field: 'max_angular_speed', error: '最大角速度须为有限数值 <b>'}, bounds);
check(refusal, {field: 'max_angular_speed', text: '最大角速度须为有限数值 <b>'}, 'another refusal of a field: the words that came with it (as text)');
check(page.safetyLimitsRefusal({code: 'limits_invalid', field: 'set_by', error: 'x'}, bounds), null, 'not one of the three inputs');
check(page.safetyLimitsRefusal({code: 'runs_active', error: 'x'}, bounds), null, 'no field named');
check(page.safetyLimitsRefusal(undefined, bounds), null, 'no body at all');

// ---------------------------------------------------------------- who is recorded
check(page.safetyLimitsWho({name: ' 张工 ', email: 'zhang@example.test'}), '张工', 'the display name');
check(page.safetyLimitsWho({name: '  ', email: 'zhang@example.test'}), 'zhang@example.test', 'else the e-mail');
check(page.safetyLimitsWho(null), '', 'signed out');

// ---------------------------------------------------------------- the detail section
const robot = robotWith(summaryOf({}));
let section = page.safetyLimitsSection(robot, {view: view()});
includes(section, '<h3>安全限值</h3>', 'the heading');
includes(section, '<span class="status bad"><i class="dot"></i>未填，不能运动</span>', 'not filled: marked as the requirement asks');
includes(section, '这台设备还没有填写安全限值', 'the device\'s own reason');
includes(section, 'data-action="engine-safety-limits-edit" data-id="robot-1">填写安全限值</button>', 'the way to fill them in');
excludes(section, '修改安全限值', 'nothing to change yet');

section = page.safetyLimitsSection(robot, {view: filledView()});
includes(section, '<span class="status good"><i class="dot"></i>已填</span>', 'filled');
for (const part of ['<b>最大线速度</b>0.5 m/s', '<b>最大角速度</b>0.8 rad/s', '<b>运动指令超时</b>0.5 s']) includes(section, part, 'a value');
includes(section, '填写人：张工', 'who');
includes(section, '填写时间：' + page.safetyLimitsWhen(1781000000), 'when');
includes(page.safetyLimitsWhen(1781000000), '2026', 'a full date');
check(page.safetyLimitsWhen(null), '—', 'no time known');
includes(section, '>修改安全限值</button>', 'the way to change them');
excludes(section, '实际执行', 'enforced as filled: nothing more to say');
excludes(section, '修改记录', 'one entry: no history to open');

section = page.safetyLimitsSection(robot, {view: filledView({limits: {base: {...filled.base, max_linear_speed: 2}},
                                                             effective: {base: {...filled.base, max_linear_speed: 1}}})});
includes(section, '<b>最大线速度</b>2 m/s', 'what was filled in');
includes(section, '实际执行：最大线速度 1 m/s', 'what is enforced when the body is lower');
excludes(section, '实际执行：最大线速度 1 m/s、', 'only the values that differ');

section = page.safetyLimitsSection(robot, {view: view({required: false, motion_blocked: false, reason: null,
                                                       effective: {base: {max_linear_speed: 1, max_angular_speed: 1.5}}})});
includes(section, '<span class="status "><i class="dot"></i>仿真本体可不填</span>', 'a simulated body');
includes(section, '现在执行的是：限速 1 m/s · 1.5 rad/s', 'what it enforces by itself');
includes(section, '>填写安全限值</button>', 'it can still be filled in');

section = page.safetyLimitsSection(robot, {view: view({invalid: '填写时间无效', reason: '设备上保存的安全限值无效（填写时间无效），已按没有填写处理。'})});
includes(section, '设备上保存的安全限值无效（填写时间无效），已按没有填写处理。', 'a record the device did not use: its reason as it is');
includes(section, '未填，不能运动', 'and it counts as not filled');

const history = [{revision: 2, limits: filled, set_by: '张工', set_at: 1781000000},
                 {revision: 1, limits: {base: {...filled.base, max_linear_speed: 0.3}}, set_by: '李工 <i>', set_at: 1780000000}];
section = page.safetyLimitsSection(robot, {view: filledView({revision: 2, history})});
includes(section, '修改记录（2 次）', 'the history can be opened');
includes(section, '第 1 次 · ' + page.safetyLimitsWhen(1780000000) + ' · 李工 &lt;i&gt; · 限速 0.3 m/s', 'an earlier entry, escaped');
includes(page.safetyLimitsSection(robot, {view: filledView({groups: ['base', 'arm <b>']})}), 'arm &lt;b&gt;', 'a group this version cannot fill is named');

section = page.safetyLimitsSection(robot, {view: {robot_id: 'robot-1', supported: false, reason: '这台设备上的管理层版本较旧 <b>'}});
includes(section, '这台设备上的管理层版本较旧 &lt;b&gt;', 'an older device: said plainly');
excludes(section, 'engine-safety-limits-edit', 'an older device: no form');
section = page.safetyLimitsSection(robot, {error: Error('无法连接接入服务 <b>')});
includes(section, '读不到这台设备的安全限值', 'unreachable: said plainly');
includes(section, '无法连接接入服务 &lt;b&gt;', 'with the reason');
includes(section, 'data-action="engine-safety-limits-reload"', 'and a way to try again');
excludes(section, 'engine-safety-limits-edit', 'unreachable: no form');
includes(section, '未填，不能运动', 'what the last connection check saw stays visible');
includes(page.safetyLimitsSection(robot, null), '正在读取', 'while the device is asked');
section = page.safetyLimitsSection(robotWith(undefined, {endpoint: ''}), null);
includes(section, '还没有配置设备端服务地址', 'no service address: nothing to ask');
excludes(section, 'engine-safety-limits', 'and nothing to press');
const hostile = '<img src=x onerror=alert(1)>';
section = page.safetyLimitsSection({...robot, id: hostile, name: hostile},
                                   {view: filledView({set_by: hostile, reason: hostile, groups: [hostile],
                                                      history: [{revision: 2, limits: filled, set_by: hostile, set_at: 1}, {revision: 1, limits: null, set_by: hostile, set_at: null}]})});
check(tagsOf(section).includes('img'), false, 'whatever a device or an account is called, it is shown as text');
includes(section, '&lt;img src=x onerror=alert(1)&gt;', 'shown, escaped');

// ---------------------------------------------------------------- the form
let fields = page.safetyLimitsFormFields(view(), {name: '张工', email: 'zhang@example.test'});
for (const name of ['max_linear_speed', 'max_angular_speed', 'command_timeout_s']) {
  includes(fields, `<label for="safety-limits-${name}">`, `${name}: a label bound to its input`);
  includes(fields, `id="safety-limits-${name}" name="${name}" type="text" inputmode="decimal"`, `${name}: its input`);
  includes(fields, `aria-describedby="safety-limits-${name}-hint safety-limits-${name}-problem"`, `${name}: hint and problem are tied to it`);
  includes(fields, `id="safety-limits-${name}-problem" class="safety-limits-problem" role="alert"`, `${name}: a place for its problem`);
}
check([...fields.matchAll(/ value="([^"]*)"/g)].map(m => m[1]), ['', '', ''], 'not filled: empty, the platform does not guess');
excludes(fields, 'placeholder', 'no example value either');
includes(fields, '最大线速度（m/s）', 'the unit is in the label');
includes(fields, '允许范围：大于 0，不超过 1 m/s。上限来自本体声明的上限（平台上限 3 m/s）。', 'the range and where it comes from');
includes(fields, '保存后会记录填写人：张工', 'who will be recorded');
includes(fields, '任何能力都不能让这台设备运动', 'what not filling means');
fields = page.safetyLimitsFormFields(filledView(), {name: '', email: 'zhang@example.test'});
check([...fields.matchAll(/ value="([^"]*)"/g)].map(m => m[1]), ['0.5', '0.8', '0.5'], 'filled: the current values');
includes(fields, '保存后会记录填写人：zhang@example.test', 'no name: the e-mail');
fields = page.safetyLimitsFormFields(filledView({limits: {base: {...filled.base, max_linear_speed: 0.1234567}}}), {name: '张工'});
check([...fields.matchAll(/ value="([^"]*)"/g)].map(m => m[1])[0], '0.1234567', 'exactly what the device holds: saving it again changes nothing');
fields = page.safetyLimitsFormFields(filledView({bounds: {base: {}}}), {name: hostile});
check(tagsOf(fields).includes('img'), false, 'the account name is text');
includes(fields, '最大线速度（m/s）', 'a device that names no range: the unit the field has');

// ---------------------------------------------------------------- the refusals have their own plain words
for (const code of ['safety_limits_missing', 'limits_out_of_range', 'limits_invalid', 'set_by_required', 'request_id_reused',
                    'limits_not_saved']) {
  const explained = page.uxClassify(Object.assign(Error('device text 500'), {status: 400, reason: code,
                                                                             body: {error: 'device text 500', code, device_status: 409}}));
  check([explained.source, explained.code], ['code', code], `${code}: explained by its code`);
  assert.ok(explained.plain && explained.kind && explained.next.length, `${code}: says what happened and what to do`);
  assert.doesNotMatch(explained.plain, /内部出错|稍后重试|网络|连不上/, `${code}: not a fault to retry, not a network problem`);
  checks += 1;
}
check(page.uxClassify({error: 'x', code: 'safety_limits_missing', device_status: 409}).kind, 'safety', 'missing limits are a safety refusal');
check(page.uxClassify({error: 'x', code: 'runs_active', device_status: 409}).kind, 'state', 'a running skill keeps the words it has');

console.log(`safety_limits logic: ${checks} checks passed`);
