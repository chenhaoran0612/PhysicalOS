'use strict';
// Issue #22 (R1-06): safety limits are filled in per device by a person. Maximum linear speed, maximum
// angular speed and the motion-command timeout are not guessed by the platform; until a person has
// filled them in, no capability can make the device move.
//  * A robot's card says "安全限值未填，不能运动" with the way to the form, or the filled values in a quiet line.
//  * The robot's detail has a "安全限值" section: the state, the values, who filled them in and when.
//  * The form shows the range the device accepts for each value and where it comes from; it is empty until
//    a person has filled it in. A refusal of the device is shown in the form, on the field it is about.
// The device keeps, checks and enforces the values (brainos/safety_limits.py is the console's way to it);
// who filled them in is the signed-in account, decided on the server.
// Loaded last. It wraps engineRobotCard / engineAction / engineSubmit / robotDetailRender instead of
// editing them, as robot_remove.js and ssh_edit.js do.

const SAFETY_LIMITS_GROUP = 'base';       // the only group so far: the chassis
const SAFETY_LIMITS_FIELDS = [
  {name: 'max_linear_speed', label: '最大线速度', unit: 'm/s'},
  {name: 'max_angular_speed', label: '最大角速度', unit: 'rad/s'},
  {name: 'command_timeout_s', label: '运动指令超时', unit: 's'},
];
const SAFETY_LIMITS_BLOCKED = '安全限值未填，不能运动';
const SAFETY_LIMITS_EDIT = 'engine-safety-limits-edit', SAFETY_LIMITS_RELOAD = 'engine-safety-limits-reload';
const SAFETY_LIMITS_FORM = 'engine-safety-limits-save';
const SAFETY_LIMITS_CARD_AT = ['<div class="tags">', '<div class="card-bottom">'];      // before the first one a card has
const SAFETY_LIMITS_DIGITS = 6;
const SAFETY_LIMITS_WIDE_OFFSET = 0xFEE0;      // from a full-width digit to its ASCII digit
const SAFETY_LIMITS_NO_ENDPOINT = '这台机器人还没有配置设备端服务地址。设备上的运行时接入并通过连接检查后，才能在这里填写安全限值。';
const SAFETY_LIMITS_UNFILLED = '这台设备还没有填写安全限值。填写之前，任何能力都不能让它运动。';
const SAFETY_LIMITS_SIMULATED = '这是仿真本体，自带运动上限，可以不填。填写后，设备按你填的值和本体自带上限中较低的执行。';

// Plain words for this feature's refusals are in ux04_errors.js (UX_CODES), with the others.

const safetyLimitsIsNumber = value => typeof value === 'number' && Number.isFinite(value);
const safetyLimitsNumber = value => String(Number(Number(value).toPrecision(SAFETY_LIMITS_DIGITS)));
const safetyLimitsWho = user => String(user?.name ?? '').trim() || String(user?.email ?? '').trim();
const safetyLimitsWhen = seconds => (safetyLimitsIsNumber(seconds)
  ? new Date(seconds * 1000).toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false})
  : '—');

// "限速 0.5 m/s · 0.8 rad/s · 指令超时 0.5 s", with only the values that are there.
function safetyLimitsSummary(limits) {
  const base = limits?.[SAFETY_LIMITS_GROUP] || {};
  const shown = name => safetyLimitsIsNumber(base[name]);
  const speeds = SAFETY_LIMITS_FIELDS.slice(0, 2).filter(f => shown(f.name)).map(f => `${safetyLimitsNumber(base[f.name])} ${f.unit}`);
  const parts = speeds.length ? ['限速 ' + speeds.join(' · ')] : [];
  if (shown('command_timeout_s')) parts.push(`指令超时 ${safetyLimitsNumber(base.command_timeout_s)} s`);
  return parts.join(' · ');
}

// ---------------------------------------------------------------- the robot's card

// What a card says, from the summary the robot record keeps (the last connection check, read or save).
function safetyLimitsCardState(robot) {
  const summary = robot?.device_status?.safety_limits;
  if (!summary || typeof summary !== 'object') return {kind: 'unknown'};        // never checked, or an older device
  if (summary.motion_blocked === true) return {kind: 'blocked'};
  const text = summary.filled === true ? safetyLimitsSummary(summary.limits) : '';
  return text ? {kind: 'filled', text} : {kind: 'none'};
}

function safetyLimitsCard(html, robot) {
  const state = safetyLimitsCardState(robot);
  const at = SAFETY_LIMITS_CARD_AT.map(mark => html.indexOf(mark)).find(index => index >= 0) ?? -1;
  let block = '';
  if (state.kind === 'blocked') {
    block = `<div class="safety-limits-card safety-limits-card-blocked">${badge(SAFETY_LIMITS_BLOCKED, 'bad')}` +
      `<button type="button" class="button small" data-action="${SAFETY_LIMITS_EDIT}" data-id="${h(robot.id)}" ` +
      `aria-label="填写安全限值：${h(robot.name)}">填写安全限值</button></div>`;
  } else if (state.kind === 'filled') {
    block = `<div class="safety-limits-card safety-limits-card-line">${h(state.text)}</div>`;
  }
  return block && at >= 0 ? html.slice(0, at) + block + html.slice(at) : html;
}

// ---------------------------------------------------------------- what was typed, against the device's ranges

// What a person typed, as a number; null when it is not one. Full-width digits and the full stop of a
// Chinese input method count as what they mean.
function safetyLimitsParse(text) {
  const plain = String(text ?? '').trim()
    .replace(/[０-９]/g, c => String.fromCharCode(c.charCodeAt(0) - SAFETY_LIMITS_WIDE_OFFSET)).replace(/[．。]/g, '.');
  const value = /^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(plain) ? Number(plain) : NaN;
  return Number.isFinite(value) ? value : null;
}

const safetyLimitsUnit = bound => (bound?.unit ? ' ' + bound.unit : '');
const safetyLimitsFloor = bound => (bound.min_exclusive ? '大于 ' : '不小于 ') + safetyLimitsNumber(bound.min);
// Where a field's maximum comes from: what the body itself declares when that is less than the platform allows.
const safetyLimitsMaxSource = bound => (safetyLimitsIsNumber(bound.body_max) &&
  (!safetyLimitsIsNumber(bound.platform_max) || bound.body_max < bound.platform_max) ? '本体声明的上限' : '平台上限');

// Why a value is outside the range the device gave for its field; '' when it is inside, or no range is known.
function safetyLimitsRangeProblem(value, bound) {
  if (!bound) return '';
  const unit = safetyLimitsUnit(bound);
  if (safetyLimitsIsNumber(bound.min) && (bound.min_exclusive ? value <= bound.min : value < bound.min)) {
    return (bound.min_exclusive ? '需要大于 ' : '不能小于 ') + safetyLimitsNumber(bound.min) + unit;
  }
  if (safetyLimitsIsNumber(bound.max) && value > bound.max) {
    return `不能超过 ${safetyLimitsNumber(bound.max)}${unit}（${safetyLimitsMaxSource(bound)}）`;
  }
  return '';
}

// Checked before anything is sent: {values: the usable numbers, problems: [{field, text}]}.
function safetyLimitsCheck(typed, bounds) {
  const values = {}, problems = [];
  for (const field of SAFETY_LIMITS_FIELDS) {
    const text = String(typed?.[field.name] ?? '').trim(), value = safetyLimitsParse(text);
    const problem = !text ? `请填写${field.label}` : value === null ? '请填写一个数字'
      : safetyLimitsRangeProblem(value, bounds?.[SAFETY_LIMITS_GROUP]?.[field.name]);
    if (problem) problems.push({field: field.name, text: problem});
    else values[field.name] = value;
  }
  return {values, problems};
}

// The line under an input: the range the device accepts and where its maximum comes from.
function safetyLimitsHint(bound) {
  if (!bound || !safetyLimitsIsNumber(bound.max)) return '';
  const unit = safetyLimitsUnit(bound), fromBody = safetyLimitsMaxSource(bound) === '本体声明的上限';
  const other = fromBody ? bound.platform_max : bound.body_max;
  const range = [safetyLimitsIsNumber(bound.min) ? safetyLimitsFloor(bound) : '', `不超过 ${safetyLimitsNumber(bound.max)}${unit}`];
  return `允许范围：${range.filter(Boolean).join('，')}。上限来自${fromBody ? '本体声明的上限' : '平台上限'}` +
    (safetyLimitsIsNumber(other) ? `（${fromBody ? '平台上限' : '本体声明的上限'} ${safetyLimitsNumber(other)}${unit}）` : '') + '。';
}

// The input a refusal is about and what to say next to it; null when it names none of the three inputs.
// `body` is the error body of the console: the device's own code, field, min, max and bound.
function safetyLimitsRefusal(body, bounds) {
  const name = String(body?.field ?? '').split('.').pop();
  if (!SAFETY_LIMITS_FIELDS.some(f => f.name === name)) return null;
  if (body.code !== 'limits_out_of_range') return {field: name, text: String(body.error || '设备没有接受这个值')};
  // The range in the refusal is the device's and newer than the one the form was opened with.
  const known = bounds?.[SAFETY_LIMITS_GROUP]?.[name] || {};
  const bound = {...known, min: safetyLimitsIsNumber(body.min) ? body.min : known.min, max: safetyLimitsIsNumber(body.max) ? body.max : known.max};
  const source = {body: '本体声明的上限', platform: '平台上限'}[body.bound] || safetyLimitsMaxSource(bound);
  const range = [safetyLimitsIsNumber(bound.min) ? safetyLimitsFloor(bound) : '',
                 safetyLimitsIsNumber(bound.max) ? `不超过 ${safetyLimitsNumber(bound.max)}${safetyLimitsUnit(bound)}（${source}）` : ''];
  return {field: name, text: '设备没有接受这个值：允许' + range.filter(Boolean).join('、')};
}

// ---------------------------------------------------------------- the "安全限值" section of a robot's detail

function safetyLimitsBadge(summary) {
  if (!summary || typeof summary !== 'object') return '';
  if (summary.motion_blocked === true) return badge('未填，不能运动', 'bad');
  if (summary.filled === true) return badge('已填', 'good');
  return summary.required === false ? badge('仿真本体可不填') : '';
}

// A filled device: the values, who filled them in and when, and what is enforced where that is less.
function safetyLimitsFilledHtml(view) {
  const base = view.limits?.[SAFETY_LIMITS_GROUP] || {}, enforced = view.effective?.[SAFETY_LIMITS_GROUP] || {};
  const fields = SAFETY_LIMITS_FIELDS.filter(f => safetyLimitsIsNumber(base[f.name]));
  const cells = fields.map(f => `<div><b>${h(f.label)}</b>${h(safetyLimitsNumber(base[f.name]))} ${h(f.unit)}</div>`).join('');
  const lower = fields.filter(f => safetyLimitsIsNumber(enforced[f.name]) && enforced[f.name] !== base[f.name])
    .map(f => `${f.label} ${safetyLimitsNumber(enforced[f.name])} ${f.unit}`);
  return `<div class="detail-meta safety-limits-values">${cells}</div>` +
    `<p class="safety-limits-record">${h(`填写人：${view.set_by || '—'} · 填写时间：${safetyLimitsWhen(view.set_at)}`)}</p>` +
    (lower.length ? notice(h(`实际执行：${lower.join('、')}。本体自己声明的上限更低，设备按较低的值执行。`)) : '');
}

function safetyLimitsHistoryHtml(history, revision) {
  if (!Array.isArray(history) || history.length < 2) return '';
  const items = history.map(entry => `<li>${h([`第 ${entry.revision} 次`, safetyLimitsWhen(entry.set_at), entry.set_by || '—',
                                               safetyLimitsSummary(entry.limits)].filter(Boolean).join(' · '))}</li>`);
  const count = revision > history.length ? `最近 ${history.length} 次` : `${history.length} 次`;
  return `<details class="safety-limits-history"><summary>修改记录（${h(count)}）</summary><ul>${items.join('')}</ul></details>`;
}

// What the device holds, once it has answered.
function safetyLimitsBody(robot, view) {
  const filled = view.filled === true, enforced = safetyLimitsSummary(view.effective);
  const others = (Array.isArray(view.groups) ? view.groups : []).filter(group => group !== SAFETY_LIMITS_GROUP);
  let html = view.motion_blocked === true ? `<p class="safety-limits-reason">${h(view.reason || SAFETY_LIMITS_UNFILLED)}</p>` : '';
  if (filled) html += safetyLimitsFilledHtml(view);
  else if (view.required === false) html += `<p class="inline-note">${h(SAFETY_LIMITS_SIMULATED + (enforced ? `现在执行的是：${enforced}。` : ''))}</p>`;
  if (others.length) html += `<p class="inline-note">${h(`这台设备还声明了其他限值分组（${others.join('、')}）；这一版应用只能填写底盘的限值。`)}</p>`;
  return html + safetyLimitsHistoryHtml(view.history, view.revision) +
    `<div class="button-row">${button(filled ? '修改安全限值' : '填写安全限值', SAFETY_LIMITS_EDIT, filled ? 'secondary small' : 'small', robot.id)}</div>`;
}

const safetyLimitsUnreadable = error =>
  `<div class="form-error safety-limits-unreadable">读不到这台设备的安全限值：${typeof errorBlock === 'function' ? errorBlock(error) : h(error?.message)}</div>`;

// The section. `loaded`: null while the device is asked, {view} with its answer, {error} when it gave none.
function safetyLimitsSection(robot, loaded) {
  const view = loaded?.view, known = view?.supported ? view : robot?.device_status?.safety_limits;
  const head = `<div class="section-head"><h3>安全限值</h3>${safetyLimitsBadge(known)}</div>`;
  if (!robot?.endpoint) return head + `<p class="inline-note">${h(SAFETY_LIMITS_NO_ENDPOINT)}</p>`;
  if (!loaded) return head + '<p class="inline-note" role="status">正在读取这台设备的安全限值…</p>';
  if (loaded.error) {
    return head + safetyLimitsUnreadable(loaded.error) +
      `<div class="button-row">${button('重新读取', SAFETY_LIMITS_RELOAD, 'secondary small', robot.id)}</div>`;
  }
  return head + (view.supported ? safetyLimitsBody(robot, view) : notice(h(view.reason), true));
}

// The newest question this page asked a device. The answer to an older one is dropped: it must not draw over
// what a newer one showed, or over a form somebody is typing in.
let safetyLimitsAsked = 0;

function safetyLimitsPaint(robot, loaded, asked) {
  const box = $('#safety-limits-section');
  if (asked !== safetyLimitsAsked || !box || box.dataset.robot !== robot.id || !$('#dialog').open) return;
  box.innerHTML = safetyLimitsSection(robot, loaded);
}

// Asks the device and draws its answer into the open detail. Errors end in the section, never outside it.
async function safetyLimitsLoad(robot) {
  const asked = ++safetyLimitsAsked;
  safetyLimitsPaint(robot, null, asked);
  try {
    safetyLimitsPaint(robot, {view: await api('robots/limits', {robot_id: robot.id, project_id: projectId})}, asked);
  } catch (error) {
    safetyLimitsPaint(robot, {error}, asked);
  }
}

// Puts the section into a robot detail that was just drawn, before the capability table.
function safetyLimitsAttach(robot) {
  const body = $('#dialog-content .dialog-body');
  if (!robot || !body) return;
  const html = `<div class="contract-line safety-limits-section" id="safety-limits-section" data-robot="${h(robot.id)}">` +
    `${safetyLimitsSection(robot, null)}</div>`;
  const anchor = body.querySelector('.detail-split');
  if (anchor) anchor.insertAdjacentHTML('beforebegin', html);
  else body.insertAdjacentHTML('beforeend', html);
  if (robot.endpoint) safetyLimitsLoad(robot);
}

// ---------------------------------------------------------------- the form

function safetyLimitsField(field, value, bound) {
  const id = `safety-limits-${field.name}`;
  return `<div class="field safety-limits-field"><label for="${id}">${h(field.label)}（${h(bound?.unit || field.unit)}）</label>` +
    `<input id="${id}" name="${field.name}" type="text" inputmode="decimal" autocomplete="off" spellcheck="false" ` +
    `aria-required="true" aria-describedby="${id}-hint ${id}-problem" value="${h(value)}">` +
    `<small id="${id}-hint">${h(safetyLimitsHint(bound))}</small>` +
    `<small id="${id}-problem" class="safety-limits-problem" role="alert"></small></div>`;
}

// The three inputs: the current values of a filled device, empty otherwise (the platform does not guess).
function safetyLimitsFormFields(view, user) {
  const blocked = view.motion_blocked === true, ranges = view.bounds?.[SAFETY_LIMITS_GROUP] || {};
  const base = view.filled === true ? view.limits?.[SAFETY_LIMITS_GROUP] || {} : {};
  const lead = '这三个值由人按现场情况填写，平台不会替你取值。' + (blocked ? '没有填写之前，任何能力都不能让这台设备运动。' : '');
  return notice(h(lead), blocked) + (view.invalid && view.reason ? notice(h(view.reason), true) : '') +
    // exactly what the device holds, not a rounded form of it: saving an untouched value must not change it
    SAFETY_LIMITS_FIELDS.map(f => safetyLimitsField(f, safetyLimitsIsNumber(base[f.name]) ? String(base[f.name]) : '', ranges[f.name])).join('') +
    `<p class="safety-limits-who">${h(`保存后会记录填写人：${safetyLimitsWho(user)}，以及保存的时间。`)}</p>` +
    '<p class="inline-note">设备按你填的值和本体自己声明的上限中较低的执行。有技能在运行、或机器人正在运动时，设备不接受修改：请先停止。</p>';
}

// Marks the inputs that have a problem (and clears the others); the first one gets the focus.
function safetyLimitsMark(form, problems) {
  for (const field of SAFETY_LIMITS_FIELDS) {
    const input = form.elements[field.name], slot = form.querySelector(`#safety-limits-${field.name}-problem`);
    const problem = problems.find(p => p.field === field.name);
    if (problem) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
    slot.textContent = problem ? problem.text : '';
  }
  if (problems.length) form.elements[problems[0].field].focus();
}

const safetyLimitsIsOpen = robotId => $('#dialog').open && $('#dialog').dataset.safetyLimits === robotId;

// Opens the form of a robot: asks the device first, so the form shows what it holds and the ranges it accepts.
async function safetyLimitsOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) return toast('这台机器人已经不在项目里了');
  const asked = ++safetyLimitsAsked;
  const show = body => {              // the focus goes to what can be done next, not back to the page
    dialog('安全限值', h(robot.name), `<div class="dialog-body">${body}</div>`);
    $('#dialog').dataset.safetyLimits = robotId;
    ($('#dialog .dialog-body .button') || $('#dialog .close-dialog')).focus();
  };
  const current = () => asked === safetyLimitsAsked && safetyLimitsIsOpen(robotId);
  const closing = button('关闭', 'close', 'secondary small');
  show('<p class="subtitle" role="status">正在读取这台设备的安全限值…</p>');
  let view;
  try {
    view = await api('robots/limits', {robot_id: robotId, project_id: projectId});
  } catch (error) {               // shown in the dialog: a toast would sit under its backdrop
    if (current()) {
      show(`${safetyLimitsUnreadable(error)}<div class="button-row">${button('重新读取', SAFETY_LIMITS_EDIT, 'small', robotId)}${closing}</div>`);
    }
    return;
  }
  if (!current()) return;             // the user moved on, or asked again, while the device was answering
  if (!view.supported) return show(`${notice(h(view.reason), true)}<div class="button-row">${closing}</div>`);
  form(view.filled === true ? '修改安全限值' : '填写安全限值', `${h(robot.name)} · 由人填写，设备执行`,
       safetyLimitsFormFields(view, state.user), SAFETY_LIMITS_FORM, '保存安全限值',
       `novalidate data-robot="${h(robotId)}" data-project="${h(projectId)}" data-bounds="${h(JSON.stringify(view.bounds || {}))}"`);
  $('#dialog').dataset.safetyLimits = robotId;
  const inputs = SAFETY_LIMITS_FIELDS.map(f => $(`#safety-limits-${f.name}`));
  (inputs.find(input => !input.value) || inputs[0]).focus();
}

// One request per click (the page disables the button meanwhile), each under its own request id: the device
// ties an id to what was sent with it. A refusal stays in the form; nothing is ever sent again by itself.
async function safetyLimitsSubmit(p, f) {
  if (f.dataset.project !== projectId) throw Error('项目已切换，请重新打开');
  const bounds = JSON.parse(f.dataset.bounds || '{}'), checked = safetyLimitsCheck(p, bounds);
  safetyLimitsMark(f, checked.problems);
  if (checked.problems.length) {
    const error = Error(`有 ${checked.problems.length} 项需要修改，见输入框下方的说明。没有发给设备。`);
    error.fields = checked.problems;                       // our own check: shown as it is, nothing to classify
    throw error;
  }
  let saved;
  try {
    saved = await api('robots/limits/save', {robot_id: f.dataset.robot, project_id: projectId,
                                             limits: {[SAFETY_LIMITS_GROUP]: checked.values}, request_id: newRequestId()});
  } catch (error) {
    const refusal = safetyLimitsRefusal(error.body, bounds);
    if (refusal) safetyLimitsMark(f, [refusal]);
    throw error;                                           // the page's own handler shows it in the form
  }
  $('#dialog').close();
  await refresh(true);
  return toast(`安全限值已保存，填写人：${saved.set_by || safetyLimitsWho(state.user)}`);
}

{
  const baseCard = engineRobotCard, baseAction = engineAction, baseSubmit = engineSubmit;
  const baseRender = robotDetailRender, baseDialog = dialog;
  engineRobotCard = r => safetyLimitsCard(baseCard(r), r);
  engineAction = async (type, id) => {
    if (!state.user) return baseAction(type, id);
    if (type === SAFETY_LIMITS_EDIT) return safetyLimitsOpen(id);
    if (type !== SAFETY_LIMITS_RELOAD) return baseAction(type, id);
    const robot = find('robots', id);
    return robot ? safetyLimitsLoad(robot) : undefined;
  };
  engineSubmit = async (kind, p, f) => (kind === SAFETY_LIMITS_FORM ? safetyLimitsSubmit(p, f) : baseSubmit(kind, p, f));
  robotDetailRender = d => { baseRender(d); safetyLimitsAttach(d.robot); };
  // Any other dialog replaces this one: an answer of the device that arrives later must not draw over it.
  dialog = (...args) => { delete $('#dialog').dataset.safetyLimits; return baseDialog(...args); };
}
$('#dialog').addEventListener('close', () => { delete $('#dialog').dataset.safetyLimits; });
// Editing a marked input clears its mark.
document.addEventListener('input', e => {
  const input = e.target.closest?.('.safety-limits-field input[aria-invalid]');
  if (!input) return;
  input.removeAttribute('aria-invalid');
  const slot = document.getElementById(input.id + '-problem');
  if (slot) slot.textContent = '';
});
render();
