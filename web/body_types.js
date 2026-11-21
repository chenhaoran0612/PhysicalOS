'use strict';
// Issue #32: the platform keeps the list of body types (brainos/body_types.py). This file
//   * offers that list where a device is added: the two forms keep their markup, only the type field is swapped;
//   * names the type of a registered device, also a value from before the list existed ("旧类型");
//   * points out when the device itself reports another listed type, and lets a person change the type;
//   * shows the list at the foot of "我的机器人", where types are added and removed.
// The type of a device is the one a person chose. What the device reports never replaces it.
// It wraps form / engineRobotCard / engineAction / engineSubmit / views.robots instead of editing them,
// as ssh_edit.js does. It adds nothing to the end of a card, so its place among those scripts does not matter.

const BODY_TYPE_ADD_FORMS = ['robot', 'engine-ssh'];                 // the forms that register a new device
const BODY_TYPE_OLD_FIELD = /<label class="field">本体形态<select name="family" required>[\s\S]*?<\/select><\/label>/;
const BODY_TYPE_ACTIONS = ['engine-body-type', 'engine-body-type-reported', 'engine-body-types-add',
                           'engine-body-types-remove', 'engine-body-types-remove-confirm'];
const BODY_TYPE_FORMS = ['engine-body-type', 'engine-body-types-add'];
const BODY_TYPE_UNKNOWN = '未知类型';
const BODY_TYPE_OLD = '旧类型，不在清单里';
const BODY_TYPE_TEMPLATE_PATTERN = '[a-z0-9][a-z0-9_.\\-]{0,99}';     // TEMPLATE_ID in brainos/body_types.py
const BODY_TYPE_NAME_MAX = 40;                                        // NAME_MAX there
const BODY_TYPE_STOP = 'safety.stop';                                 // listed with the motion interfaces
const BODY_TYPE_CARD_TAGS = '<div class="tags">';
let bodyTypeBaseAction = null, bodyTypeBaseSubmit = null;
let bodyTypesOpen = false;                // the list stays open across refreshes once a person opened it

const bodyTypeList = () => (Array.isArray(state.body_types) ? state.body_types : []);
// False until the page has the list (signed out, or the first answer of the backend is still on its way):
// then nothing here changes what the page shows.
const bodyTypesReady = () => bodyTypeList().length > 0;
const bodyTypeFind = id => bodyTypeList().find(t => t.id === id) || null;
// A listed type by its name; a value from before the list by the name it always had.
const bodyTypeName = id => bodyTypeFind(id)?.name || familyNames[id] || BODY_TYPE_UNKNOWN;
const bodyTypeIsOld = id => !bodyTypeFind(id);

// What the device says it is (a value of the standard), once it has been asked; null before that.
function bodyTypeReported(robot) {
  const id = robot.manifest?.family;
  return id ? {id, name: bodyTypeName(id), listed: Boolean(bodyTypeFind(id))} : null;
}

// Worth pointing out: the device names another type of the list, one the person could change to.
function bodyTypeDiffers(robot) {
  const reported = bodyTypeReported(robot);
  return Boolean(reported && reported.listed && reported.id !== robot.family);
}

// The devices of the workspace that use a type, in every project, oldest first.
function bodyTypeUsers(typeId) {
  const projects = Object.fromEntries((state.projects || []).map(p => [p.id, p.name]));
  return (state.robots || []).filter(r => r.family === typeId)
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')))
    .map(r => ({id: r.id, name: r.name, project_name: projects[r.project_id] || ''}));
}

// ---------------------------------------------------------------- choosing a type
function bodyTypeSelect(chosen = '') {
  const current = bodyTypeFind(chosen) ? chosen : '';
  const options = [['', '请选择本体类型'], ...bodyTypeList().map(t => [t.id, t.name])];
  return `<select name="family" required>${options.map(([value, text]) =>
    `<option value="${h(value)}" ${value === current ? 'selected' : ''}>${h(text)}</option>`).join('')}</select>`;
}

function bodyTypeField(chosen = '') {
  return field('本体类型', bodyTypeSelect(chosen),
               '类型决定这台设备对应的状态机模板和应有的运控接口。清单在“我的机器人”页面底部维护。');
}

// The fields of a form that adds a device, with the platform's list in place of the seven old values.
function bodyTypeFormFields(fields, kind) {
  if (!bodyTypesReady() || !BODY_TYPE_ADD_FORMS.includes(kind) || typeof fields !== 'string') return fields;
  return fields.replace(BODY_TYPE_OLD_FIELD, () => bodyTypeField());
}

// ---------------------------------------------------------------- a registered device
function bodyTypeFlags(robot) {
  const flags = bodyTypeIsOld(robot.family) ? [BODY_TYPE_OLD] : [];
  return bodyTypeDiffers(robot) ? [...flags, `设备自报：${bodyTypeReported(robot).name}`] : flags;
}

// The card of a real robot: its type by the list's name, a way to change it, and what is worth knowing.
function bodyTypeCard(html, robot) {
  const line = `<p>${h(familyNames[robot.family])}<br>`;
  if (!bodyTypesReady() || robot.sim || !html.includes(line)) return html;
  const change = '<button type="button" class="text-link body-type-change" data-action="engine-body-type" ' +
    `data-id="${h(robot.id)}" aria-label="修改 ${h(robot.name)} 的本体类型">修改类型</button>`;
  const flags = bodyTypeFlags(robot).map(text => `<span class="tag body-type-flag">${h(text)}</span>`).join('');
  return html.replace(line, () => `<p><span class="body-type-name">${h(bodyTypeName(robot.family))}</span> ${change}<br>`)
    .replace(BODY_TYPE_CARD_TAGS, () => BODY_TYPE_CARD_TAGS + flags);
}

// The cell of the robot detail (robot_detail.js): the chosen type, and what the device reports when it differs.
function bodyTypeDetail(robot) {
  if (!bodyTypesReady()) return `<div><b>本体类型</b>${h(familyNames[robot.family] || robot.family)}</div>`;
  const old = bodyTypeIsOld(robot.family) ? ` <span class="tag body-type-flag">${BODY_TYPE_OLD}</span>` : '';
  const reported = bodyTypeReported(robot);
  const said = reported && reported.id !== robot.family
    ? `<small class="body-type-reported">设备自报：${h(reported.name)}${reported.listed ? '' : '（不在清单里）'}</small>` : '';
  return `<div><b>本体类型</b>${h(bodyTypeName(robot.family))}${old}${said}</div>`;
}

// The form that changes the type of a registered device, as the arguments of form().
function bodyTypeAssignForm(robot) {
  const reported = bodyTypeReported(robot);
  const notes = [];
  if (bodyTypeIsOld(robot.family)) {
    notes.push(notice(`这台设备登记的是“${h(bodyTypeName(robot.family))}”，它不在现在的清单里。` +
                      '改成清单里的类型以后，不能再改回这个旧类型。', true));
  }
  if (reported && reported.id !== robot.family) {
    notes.push(notice(reported.listed
      ? `设备自报的类型是“${h(reported.name)}”，和登记的不一样。类型以这里选的为准，不会被设备自报的覆盖。 ` +
        button(`改成“${h(reported.name)}”`, 'engine-body-type-reported', 'secondary small', robot.id)
      : `设备自报的类型是“${h(reported.name)}”，它不在清单里，不能选用。`));
  }
  return ['修改本体类型', h(robot.name), notes.join('') + bodyTypeField(robot.family), 'engine-body-type', '保存类型',
          `data-id="${h(robot.id)}"`];
}

// ---------------------------------------------------------------- the list
function bodyTypeInterface(id) {
  const known = (state.body_type_interfaces || []).find(c => c.id === id);
  return `<span class="tag body-type-interface">${known ? h(known.title) + ' ' : ''}<span class="mono">${h(id)}</span></span>`;
}

function bodyTypeRow(type) {
  const users = bodyTypeUsers(type.id).length;
  const control = type.builtin ? '<span class="body-type-builtin">内置</span>'
    : button('删除', 'engine-body-types-remove', 'danger-outline small', type.id);
  return `<tr><td><span class="cell-main">${h(type.name)}</span></td>` +
    `<td><span class="mono">${h(type.state_machine_template)}</span></td>` +
    `<td><div class="tags">${type.required_capabilities.map(bodyTypeInterface).join('')}</div></td>` +
    `<td>${users ? `${users} 台` : '—'}</td><td>${control}</td></tr>`;
}

// The list at the foot of "我的机器人". Folded until a person opens it.
function bodyTypesPanel() {
  if (!state.user) bodyTypesOpen = false;                  // the next person to sign in finds it folded
  if (!state.user || !bodyTypesReady()) return '';
  const types = bodyTypeList();
  return `<details class="page-purpose body-types"${bodyTypesOpen ? ' open' : ''}><summary>本体类型清单（${types.length} 类）</summary>` +
    '<p class="body-types-lead">添加设备时从这里选类型。每个类型对应一个状态机模板和一组应有的运控接口；还有设备在用的类型不能删除。</p>' +
    table(['类型', '状态机模板', '应有的运控接口', '在用设备', ''], types.map(bodyTypeRow)) +
    `<div class="button-row body-types-actions">${button('添加本体类型', 'engine-body-types-add', 'secondary small')}</div></details>`;
}

// The form that adds a type, as the arguments of form().
function bodyTypeAddForm() {
  const all = state.body_type_interfaces || [];
  const motion = all.filter(c => c.safety === 'motion' || c.id === BODY_TYPE_STOP);
  const other = all.filter(c => !motion.includes(c));
  const group = (title, items) => (items.length ? `<fieldset class="body-type-interfaces"><legend>${title}</legend>` +
    items.map(c => '<label class="body-type-check"><input type="checkbox" name="required_capabilities" ' +
      `value="${h(c.id)}"><span>${h(c.title)}</span><span class="mono">${h(c.id)}</span></label>`).join('') + '</fieldset>' : '');
  return ['添加本体类型', '添加以后，添加设备时就可以选它。',
          field('类型名称', `<input name="name" maxlength="${BODY_TYPE_NAME_MAX}" placeholder="例如：轮式底盘" required autocomplete="off">`) +
          field('状态机模板标识', `<input name="state_machine_template" pattern="${BODY_TYPE_TEMPLATE_PATTERN}" maxlength="100" ` +
                'placeholder="例如：wheeled.default" required autocomplete="off" spellcheck="false">',
                '状态机模板还没有上线，这里先登记它的标识：小写字母、数字、点、下划线或连字符，以字母或数字开头。') +
          `<fieldset class="field body-type-required"><legend>应有的运控接口（至少选一个）</legend>${group('运动与停止', motion)}` +
          `${group('状态与感知', other)}<small>用标准能力的编号表示；之后检查运控驱动时按它核对。</small></fieldset>`,
          'engine-body-types-add', '添加类型'];
}

// The dialog of a removal as [title, subtitle, body]: the confirmation, or why there is nothing to confirm.
function bodyTypeRemoveDialog(type, users) {
  if (users.length) {
    return ['这个类型现在不能删除', h(type.name),
            `<div class="dialog-body body-type-remove">${notice(`还有 ${users.length} 台设备在用“${h(type.name)}”。` +
            '先把这些设备改成别的类型，或删除这些设备，再删除这个类型。', true)}` +
            `<ul class="body-type-users">${users.map(u => `<li><b>${h(u.name)}</b><span>${h(u.project_name)}</span></li>`).join('')}</ul>` +
            `<div class="button-row">${button('关闭', 'close', 'secondary small')}</div></div>`];
  }
  return ['删除本体类型', h(type.name),
          '<div class="dialog-body body-type-remove"><div class="form-error" role="alert"></div>' +
          `<p class="body-type-lead">没有设备在用“${h(type.name)}”。删除以后，添加设备时不能再选它；需要时可以重新添加。</p></div>` +
          `<div class="dialog-footer">${button('取消', 'close', 'secondary')}` +
          `${button('删除', 'engine-body-types-remove-confirm', 'danger', type.id)}</div>`];
}

// ---------------------------------------------------------------- acting
// Changes a device's type from the open form. A refusal is shown in that form, drawn again from what the
// page knows after reading the state anew: a type that left the list meanwhile is no longer offered.
// (A toast would sit under the dialog.)
async function bodyTypeAssign(robotId, family, done) {
  try {
    await api('robots/body-type', {project_id: projectId, robot_id: robotId, family});
  } catch (error) {
    if (error.status === 401) throw error;               // signed out meanwhile: the page's own handling says so
    await refresh(true);
    return bodyTypeAssignRefused(robotId, error);
  }
  await refresh(true);
  $('#dialog').close();
  return toast(done);
}

function bodyTypeAssignRefused(robotId, error) {
  const robot = find('robots', robotId);
  if (!robot) {
    $('#dialog').close();
    return toast('这台机器人已经不在项目里了');
  }
  form(...bodyTypeAssignForm(robot));
  return formError($('#dialog .form-error'), error);
}

// One click on what the device reports.
async function bodyTypeTakeReported(robotId) {
  const control = $('#dialog [data-action="engine-body-type-reported"]');
  if (control) control.disabled = true;                   // one request per click
  const robot = find('robots', robotId), reported = robot ? bodyTypeReported(robot) : null;
  if (!reported?.listed) return bodyTypeAssignRefused(robotId, Error('设备自报的类型已经不在清单里了'));
  return bodyTypeAssign(robotId, reported.id, `本体类型已改成“${reported.name}”`);
}

async function bodyTypeRemoveConfirm(typeId) {
  const control = $('#dialog [data-action="engine-body-types-remove-confirm"]'), type = bodyTypeFind(typeId);
  if (control) control.disabled = true;                   // one request per confirmation
  try {
    await api('body-types/remove', {body_type_id: typeId});
  } catch (error) {
    if (control) control.disabled = false;
    if (error.status === 401) throw error;                // signed out meanwhile: the page's own handling says so
    await refresh(true);
    if (!bodyTypeFind(typeId)) {                          // somebody else removed it meanwhile: nothing left to confirm
      $('#dialog').close();
      return toast('这个类型已经不在清单里了');
    }
    // The server knows what the page did not: a device registered meanwhile is listed from its answer.
    const devices = Array.isArray(error.body?.devices) ? error.body.devices : [];
    if (type && devices.length) return dialog(...bodyTypeRemoveDialog(type, devices));
    const slot = $('#dialog .form-error');
    if (!slot) throw error;
    return formError(slot, error);
  }
  $('#dialog').close();
  await refresh(true);
  return toast('本体类型已删除');
}

async function bodyTypeAction(type, id) {
  if (type === 'engine-body-types-add') return form(...bodyTypeAddForm());
  if (type === 'engine-body-types-remove-confirm') return bodyTypeRemoveConfirm(id);
  if (type === 'engine-body-types-remove') {
    const found = bodyTypeFind(id);
    return found ? dialog(...bodyTypeRemoveDialog(found, bodyTypeUsers(id))) : toast('这个类型已经不在清单里了');
  }
  if (type === 'engine-body-type-reported') return bodyTypeTakeReported(id);
  const robot = find('robots', id);
  return robot ? form(...bodyTypeAssignForm(robot)) : toast('这台机器人已经不在项目里了');
}

async function bodyTypeSubmit(kind, p, f) {
  if (kind === 'engine-body-type') return bodyTypeAssign(f.dataset.id, p.family, '本体类型已修改');
  const required = [...f.querySelectorAll('[name=required_capabilities]:checked')].map(box => box.value);
  if (!required.length) {
    f.querySelector('[name=required_capabilities]')?.focus();
    throw Error('请至少选择一个应有的运控接口');
  }
  await api('body-types', {name: p.name, state_machine_template: p.state_machine_template, required_capabilities: required});
  bodyTypesOpen = true;                                   // the new type is shown where it was added
  await refresh(true);
  $('#dialog').close();
  return toast('本体类型已添加');
}

{
  const baseCard = engineRobotCard, baseForm = form, baseRobots = views.robots;
  bodyTypeBaseAction = engineAction;
  bodyTypeBaseSubmit = engineSubmit;
  engineRobotCard = r => bodyTypeCard(baseCard(r), r);
  form = (title, sub, fields, kind, ...rest) => baseForm(title, sub, bodyTypeFormFields(fields, kind), kind, ...rest);
  views.robots = () => baseRobots() + bodyTypesPanel();
  engineAction = async (type, id) => (BODY_TYPE_ACTIONS.includes(type) && state.user
    ? bodyTypeAction(type, id) : bodyTypeBaseAction(type, id));
  engineSubmit = async (kind, p, f) => (BODY_TYPE_FORMS.includes(kind)
    ? bodyTypeSubmit(kind, p, f) : bodyTypeBaseSubmit(kind, p, f));
}
// `toggle` does not bubble: listen while it is captured.
document.addEventListener('toggle', e => { if (e.target.matches?.('details.body-types')) bodyTypesOpen = e.target.open; }, true);
render();
