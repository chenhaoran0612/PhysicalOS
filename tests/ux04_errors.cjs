// Table-driven checks of the plain-language error classifier (web/ux04_errors.js), ux04 / Q5-04.
// Runs the browser file in a bare VM (no DOM). Usage:
//   node tests/ux04_errors.cjs [codes.json]
// codes.json (written by tests/test_ux04.py) lists every error code found in runtime/, cloud/, sdk/ and
// brainos/ sources; each must have its own explanation (or be a param_* code).
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(readFileSync(path.join(root, 'web/ux04_errors.js'), 'utf8') +
  ';this.uxClassify=uxClassify;this.UX_CODES=UX_CODES;this.uxParamRefusalText=uxParamRefusalText;this.uxLateResetText=uxLateResetText;', sandbox);
const {uxClassify, UX_CODES, uxLateResetText} = sandbox;

// What must never be said about a refusal: a fault to retry or a network problem.
const FAULT = /内部出错|稍后重试/;
const NETWORK = /网络|超时|连不上|没有在时限内回应/;
const apiError = (message, body, status = 400) => Object.assign(new Error(message), {status, reason: body.code || '', body});
let checks = 0;
const check = (name, got, expect) => {
  for (const [k, v] of Object.entries(expect)) {
    if (v instanceof RegExp) assert.match(String(got[k]), v, `${name}: ${k}=${JSON.stringify(got[k])}`);
    else assert.deepEqual(got[k] === undefined ? undefined : JSON.parse(JSON.stringify(got[k])), v, `${name}: ${k}=${JSON.stringify(got[k])}`);
  }
  checks++;
};

// ---------------------------------------------------------------- 1. every known code has its own entry
const codesFile = process.argv[2];
if (codesFile) {
  const codes = JSON.parse(readFileSync(codesFile, 'utf8'));
  const missing = codes.filter(c => !UX_CODES[c] && !/^param(eter)?_/.test(c));
  assert.deepEqual(missing, [], '这些错误码没有白话说明：' + missing.join(', '));
  for (const code of codes) {
    const r = uxClassify(apiError('Some device text 500', {error: 'Some device text 500', code, device_status: 409}));
    assert.equal(r.source, 'code', code);
    assert.ok(r.plain && r.kind, code);
    assert.equal(r.code, code);
    checks++;
  }
}

// Safety refusals and parameter/state refusals never read as a fault or a network problem.
const SAFETY_CODES = ['estop_latched', 'safety_rejected', 'hardware_not_authorized', 'authorization_required',
  'reset_in_progress', 'conformance_running', 'simulated_only', 'param_out_of_range', 'joint_names_required',
  'joint_state_required', 'stop_count_mismatch', 'stale_after_stop', 'simulated_evidence', 'runs_active',
  'outcome_unknown', 'manifest_changed', 'param_not_in_choices', 'parameter_not_allowed', 'invalid_parameter'];
for (const code of SAFETY_CODES) {
  for (const status of [400, 403, 409]) {
    // Messages full of numbers and the word timeout must not change the verdict.
    const msg = `目标 x=500.0 超出 [-200.0, 200.0]；动作块最多 512 步；timeout_s=503；HTTP ${status}`;
    const r = uxClassify(apiError(msg, {error: msg, code, device_status: status}));
    check(code + '/' + status, r, {source: 'code', code});
    assert.doesNotMatch(r.plain, FAULT, code);
    assert.doesNotMatch(r.plain, NETWORK, code);
    assert.ok(r.next.length > 0, code + ' 需要给出下一步');
  }
}
check('reset_in_progress', uxClassify({error: 'x', code: 'reset_in_progress', device_status: 409}), {kind: 'safety'});
assert.match(uxClassify({error: 'x', code: 'reset_in_progress'}).next.join(' '), /重启.*运行时.*锁存会保留/);
assert.match(uxClassify({error: 'x', code: 'reset_in_progress'}).next.join(' '), /重启仿真机器人/);

// ---------------------------------------------------------------- 2. the review's misclassified texts (Q5-04)
const review = [
  ['base.navigate_to 被安全监督器拒绝：目标 x=500.0 超出允许工作区 [-200.0, 200.0]', 'safety'],
  ['policy.action_chunk 被安全监督器拒绝：动作块最多 512 步，收到 600 步', 'safety'],
  ['timeout_s 须为正的有限数值', 'param'],
  ['运行 r-123 超时', 'state'],
  ['急停已锁存（operator）；需经现场授权 reset 后才能运动', 'safety'],
  ['适配器上一次复位（adapter.reset()）尚未返回（已 1.0 s）；在它返回之前拒绝新的复位，急停保持锁存', 'safety'],
  ['仿真本体的测试结果不能作为现场证据；请在真实本体上进行现场测试（本体能力清单声明 simulated=true）', 'sim'],
  ['…会使机器人运动，需要 hardware_authorized=true', 'auth'],
];
for (const [text, kind] of review) {
  // as a bare string (no code, no status) and as a desktop 400 without a code
  for (const input of [text, apiError(text, {error: text})]) {
    const r = uxClassify(input);
    check(text, r, {kind});
    assert.doesNotMatch(r.plain, FAULT, text);
    assert.doesNotMatch(r.plain, /网络|连不上/, text);
  }
}

// ---------------------------------------------------------------- 3. status without code
check('409 no code', uxClassify(apiError('x', {error: 'x', device_status: 409})), {kind: 'state', source: 'status'});
check('409 no code, safety words win', uxClassify(apiError('目标超出允许工作区', {error: '目标超出允许工作区', device_status: 409})), {kind: 'safety'});
check('403 no code', uxClassify(apiError('nope', {error: 'nope', device_status: 403})), {kind: 'auth'});
check('502 no code', uxClassify(apiError('bad', {error: 'bad', device_status: 502})), {kind: 'service'});
// ux05 (Q6-08): the desktop's own 500 -- outcome unknown: check the robot first, never "retry later".
for (const body of [{}, {code: 'desktop_internal_error'}]) {
  const r = uxClassify(Object.assign(new Error('内部错误，操作未确认；请查看本地服务'), {status: 500, reason: body.code || '', body}));
  check('desktop 500 ' + JSON.stringify(body), r, {kind: 'service'});
  assert.doesNotMatch(r.plain + r.next.join(''), /稍后重试|可以重试/, 'desktop 500 不应建议重试');
  assert.match(r.next.join(''), /核对机器人状态/);
}
check('desktop 401', uxClassify(Object.assign(new Error('请先登录工作空间'), {status: 401, reason: 'unauthorized', body: {code: 'unauthorized'}})), {kind: 'account'});
// The desktop's own 400 says nothing by itself: plain Chinese stays as it is.
check('desktop plain 400', uxClassify(apiError('请输入有效服务地址', {error: '请输入有效服务地址'})), {kind: '', plain: '请输入有效服务地址', detail: ''});

// ---------------------------------------------------------------- 4. heuristics are narrow
check('explicit HTTP 503', uxClassify('接入服务返回 HTTP 503'), {kind: 'service'});
check('bare 500 number', uxClassify('目标 500 号工位'), {kind: '', plain: '目标 500 号工位'});
check('timeout param name', uxClassify('参数 timeout 须为数字'), {kind: 'param'});
check('english timeout word only', uxClassify('field timeout invalid'), {kind: ''});
check('real network timeout', uxClassify('<urlopen error timed out>'), {kind: 'network'});
check('desktop timeout code', uxClassify({error: '接入服务没有在 8 秒内响应；请求结果未知', code: 'device_timeout'}), {kind: 'network'});
check('refused', uxClassify('<urlopen error [Errno 111] Connection refused>'), {kind: 'network'});
check('fetch failed', uxClassify(new TypeError('Failed to fetch')), {kind: 'network'});
check('no CJK', uxClassify('ValueError: boom'), {plain: /技术细节/});

// ---------------------------------------------------------------- 5. parameter refusals carry the field
const stub = {error: '设备拒绝运行：技能参数 loops=99 超出声明范围 [1, 3]', code: 'param_out_of_range', device_status: 400,
  param: 'loops', min: 1, max: 3, status: 'rejected'};
check('param field', uxClassify(stub), {kind: 'param', param: 'loops', range: {min: 1, max: 3}});
check('param from message', uxClassify({error: '技能参数 speed 超出允许范围', code: 'param_out_of_range'}), {param: 'speed'});
check('unknown param_* code', uxClassify({error: 'x', code: 'param_not_in_choices', parameter: 'mode', choices: ['a', 'b']}),
  {kind: 'param', param: 'mode', choices: ['a', 'b']});
assert.match(sandbox.uxParamRefusalText(uxClassify(stub), '圈数（loops）'), /^圈数（loops）：允许范围 1 – 3/);
checks++;
// generic codes keep the device's own sentence
check('bad_request keeps text', uxClassify({error: '技能参数 loops 需要 int（有限数值）', code: 'bad_request', device_status: 400}),
  {plain: /技能参数 loops 需要 int/});

// ---------------------------------------------------------------- 6. ux05 (Q6-08): each parameter refusal its own words
const P = (code, extra = {}) => uxClassify({error: `技能参数 loops 不行`, code, device_status: 400, param: 'loops', ...extra});
check('param_type', P('param_type', {expected: 'int'}), {kind: 'param', plain: /类型不对/, expected: 'int'});
check('param_choice', P('param_choice', {choices: ['slow', 'fast']}), {kind: 'param', plain: /选项/, choices: ['slow', 'fast']});
check('param_length', P('param_length', {constraint: {min_length: 2, max_length: 5}}), {kind: 'param', plain: /长度/, constraint: {min_length: 2, max_length: 5}});
check('param_required', P('param_required'), {kind: 'param', plain: /必填/});
check('param_declaration_invalid', P('param_declaration_invalid'), {kind: 'skill', plain: /技能包本身/});
assert.match(P('param_declaration_invalid').next.join(''), /联系技能作者/);
const texts = new Set(['param_out_of_range', 'param_type', 'param_choice', 'param_length', 'param_required', 'param_declaration_invalid'].map(c => P(c).plain));
assert.equal(texts.size, 6, '六种参数拒绝应各有自己的说法');
for (const c of ['param_type', 'param_choice', 'param_length', 'param_required', 'param_declaration_invalid']) {
  assert.doesNotMatch(P(c).plain, /超出了技能声明的允许范围|内部出错|稍后重试/, c);
  checks++;
}
check('param_unknown via problem', uxClassify({error: '技能不接受参数：[\'x\']', code: 'bad_request', device_status: 400, problem: 'param_unknown', param: 'x'}),
  {kind: 'param', plain: /不认识的参数/});
// field-level lines
const F = (code, extra, label = '圈数（loops）', decl) => sandbox.uxParamRefusalText(P(code, extra), label, decl);
assert.match(F('param_type', {expected: 'int'}), /^圈数（loops）：类型不对，需要整数/);
assert.match(F('param_length', {constraint: {length: 3}}), /长度不对，需要 3 个/);
assert.match(F('param_length', {constraint: {min_length: 2, max_length: 5}}), /长度不对，允许 2 – 5/);
assert.match(F('param_required', {}), /：必填/);
assert.match(F('param_declaration_invalid', {min: 1, max: 3}), /声明有误/);
assert.doesNotMatch(F('param_declaration_invalid', {min: 1, max: 3}), /允许范围/);
// Q6-07: labels, not raw values, next to the field
assert.match(F('param_choice', {choices: ['slow', 'fast']}, '速度（speed）', {options: [{value: 'slow', label: '慢'}, {value: 'fast', label: '快'}]}), /可选 慢 \/ 快/);
assert.match(F('param_choice', {choices: [1, 2]}), /可选 1 \/ 2/);
checks += 9;

// ---------------------------------------------------------------- 7. ux05 (Q6-08): reset cause, seconds and hint
const guard = uxClassify({error: '复位动作超过允许时间仍在进行且本体仍在运动，锁存守护已再次停止；保持锁存', code: 'estop_latched',
  device_status: 409, cause: 'latch_guard_reset_grace', status: 'rejected'});
check('cause latch guard', guard, {kind: 'safety', cause: 'latch_guard_reset_grace', source: 'code+cause', plain: /锁存守护/});
const during = uxClassify({error: '复位期间又收到停止；保持锁存', code: 'estop_latched', device_status: 409, cause: 'stop_during_reset'});
check('cause stop during reset', during, {kind: 'safety', cause: 'stop_during_reset', plain: /又收到了一次“停止”/});
assert.notEqual(guard.plain, during.plain);
check('plain estop_latched unchanged', uxClassify({error: 'x', code: 'estop_latched', device_status: 409}), {plain: UX_CODES.estop_latched[1]});
const stuck = uxClassify({error: 'x', code: 'reset_in_progress', device_status: 409, reset_in_progress_s: 42.4, hint: '确认现场安全后重启运行时'});
check('reset seconds + hint', stuck, {reset_in_progress_s: 42.4, hint: '确认现场安全后重启运行时', plain: /已经执行了 42 秒/});
const pending = uxClassify({error: 'x', code: 'reset_pending', status: 'unknown'});
check('reset pending', pending, {kind: 'pending', plain: /结果要等它完成后才知道/});
assert.doesNotMatch(pending.plain, /被拒绝/, '待定的复位不应说“被拒绝”');
for (const r of [guard, during, stuck, pending]) { assert.doesNotMatch(r.plain, FAULT); assert.doesNotMatch(r.plain, NETWORK); }

// ---------------------------------------------------------------- 8. ux05-fix2 (Q7b-05/08): late resets, per runtime
// No promise without the runtime's `reset_expire`: a late reset may still release the latch.
for (const r of [stuck, pending, uxClassify({error: 'x', code: 'reset_pending', status: 'unknown', reset_expire: false, expire_s: 8})]) {
  assert.doesNotMatch(r.plain, /会被(机器人)?作废|已作废/, '旧运行时不应承诺作废：' + r.plain);
  assert.match(r.plain, /可能自动解除/);
}
const supported = uxClassify({error: 'x', code: 'reset_in_progress', device_status: 409, reset_expire: true, expire_s: 13, reset_in_progress_s: 4});
check('supported, previous reset', supported, {plain: /如果超过 13 秒的时限才完成，会被机器人作废/});
const lateOld = uxClassify({error: 'x', code: 'reset_pending', status: 'unknown', reset_expire: false, expire_s: 8, expired: true, own_reset: true});
check('own late reset, old runtime', lateOld, {expired: true, plain: /这次复位.*超过 8 秒.*不会作废超时的复位.*按“停止”/});
assert.doesNotMatch(lateOld.plain, /上一次|如果它超过时限才完成/);
// ux05-fix3: `reset_expire` missing (a runtime that does not report it) words exactly like false -- never "void".
const lateMissing = uxClassify({error: 'x', code: 'reset_pending', status: 'unknown', expire_s: 8, expired: true, own_reset: true});
check('own late reset, reset_expire missing', lateMissing, {expired: true, plain: /这次复位.*超过 8 秒.*不会作废超时的复位.*按“停止”/});
for (const t of [lateMissing.plain, uxLateResetText({expire_s: 8, in_progress_s: 20}, true), uxLateResetText({expire_s: 8, in_progress_s: 20}, false),
  uxLateResetText({reset_expire: 'true', expire_s: 8}, true), uxLateResetText(undefined, true)]) {
  assert.doesNotMatch(t, /会被(机器人)?作废|已作废|把它作废/, 'reset_expire 缺失或非 true 时不应说作废：' + t);
  assert.match(t, /不会作废超时的复位/);
}
const lateNew = uxClassify({error: 'x', code: 'reset_pending', status: 'unknown', reset_expire: true, expire_s: 8, expired: true, own_reset: true});
check('own late reset, void', lateNew, {expired: true, plain: /超过 8 秒的时限，现在在机器人上已作废.*继续保持锁存/});
assert.doesNotMatch(lateNew.plain, /上一次|如果它超过时限才完成|可能自动解除/);
check('reset_expired cause', uxClassify({error: 'x', code: 'estop_latched', device_status: 409, cause: 'reset_expired'}),
  {kind: 'safety', source: 'code+cause', plain: /已把它作废，急停继续保持锁存/});

console.log(`${checks} error classification checks passed`);
