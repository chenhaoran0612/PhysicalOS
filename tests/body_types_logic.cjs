// Pure-logic checks of web/body_types.js (issue #32): which types a new device can be given, how the type
// of a registered device is named (also a value from before the list existed), when what the device
// reports is pointed out, and what the page says when a type that devices use is to be removed.
// The rules themselves are in brainos/body_types.py, checked in tests/test_body_types.py.
//   node tests/body_types_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps functions of earlier scripts when it loads; stand-ins are enough here. The helpers of
// web/app.js it uses are written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const familyNames = {manipulator: '机械臂', mobile: '移动机器人', quadruped: '四足机器人', humanoid: '人形机器人',
                     aerial: '无人机', composite: '复合机器人', custom: '其他 / 自定义'};
const field = (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`;
const select = (name, items, chosen = '') =>
  `<select name="${name}" required>${items.map(([v, t]) => `<option value="${h(v)}" ${v === chosen ? 'selected' : ''}>${h(t)}</option>`).join('')}</select>`;
const input = (name, placeholder = '', value = '') => `<input name="${name}" type="text" placeholder="${h(placeholder)}" value="${h(value)}" required>`;
const sandbox = {
  h, familyNames, field, input, render: () => {}, views: {robots: () => '<div class="cards"></div>'},
  form: () => {}, dialog: () => {}, engineRobotCard: () => '', engineAction: async () => {}, engineSubmit: async () => {},
  state: {}, projectId: 'project-a', document: {addEventListener: () => {}},
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><div>${text}</div></div>`,
  button: (text, action, cls = '', id = '') => `<button class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  table: (headers, data) => `<table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table>`,
};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/body_types.js', 'utf8') +
  '\n;({bodyTypeName, bodyTypeReported, bodyTypeDiffers, bodyTypeUsers, bodyTypeField, bodyTypeFormFields, bodyTypeCard,' +
  ' bodyTypesPanel, bodyTypeRemoveDialog, bodyTypeAssignForm, bodyTypeAddForm, bodyTypeDetail})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const count = (text, part) => String(text).split(part).length - 1;

const quadruped = {id: 'quadruped', name: '四足机器人', state_machine_template: 'quadruped.default',
                   required_capabilities: ['safety.stop', 'base.velocity'], builtin: true};
const manipulator = {id: 'manipulator', name: '机械臂', state_machine_template: 'manipulator.default',
                     required_capabilities: ['safety.stop', 'arm.joint_position'], builtin: true};
const wheeled = {id: 'bodytype-1a2b3c', name: '轮式底盘 <A&B>', state_machine_template: 'wheeled.default',
                 required_capabilities: ['safety.stop', 'base.velocity', 'base.odometry'], builtin: false};
const interfaces = [{id: 'safety.stop', title: '停止', kind: 'command', safety: 'none'},
                    {id: 'base.velocity', title: '底盘速度', kind: 'command', safety: 'motion'},
                    {id: 'base.odometry', title: '里程计', kind: 'state', safety: 'none'},
                    {id: 'arm.joint_position', title: '机械臂关节位置', kind: 'action', safety: 'motion'}];
const robot = (changes = {}) => ({id: 'robot-1', name: 'Patrol 1', project_id: 'project-a', family: 'quadruped', ...changes});
const reporting = (family, changes = {}) => robot({manifest: {family}, ...changes});
sandbox.state = {user: {email: 'a@example.test'}, body_types: [quadruped, manipulator, wheeled], body_type_interfaces: interfaces,
                 projects: [{id: 'project-a', name: 'Site A'}, {id: 'project-b', name: 'Site <B>'}], robots: []};

// ---- the markup of earlier scripts this file looks for: if they change it, this says so before the page does
const appSource = readFileSync('web/app.js', 'utf8'), engineSource = readFileSync('web/engineering.js', 'utf8');
const OLD_FIELD_SOURCE = "field('本体形态',select('family',Object.entries(familyNames),'custom'))";
includes(appSource, OLD_FIELD_SOURCE, 'the form of a robot with a service address has the type field as it is swapped');
includes(engineSource, OLD_FIELD_SOURCE, 'the SSH form has the type field as it is swapped');
includes(engineSource, '<p>${h(familyNames[r.family])}<br>', 'the card has its type line as it is rewritten');
includes(engineSource, '<div class="tags">', 'the card has the row the flags go into');

// ---- the name of a device's type
check(page.bodyTypeName('quadruped'), '四足机器人', 'a built-in type');
check(page.bodyTypeName(wheeled.id), wheeled.name, 'a type the platform added');
check(page.bodyTypeName('mobile'), '移动机器人', 'a value from before the list keeps its name');
check(page.bodyTypeName('bodytype-gone'), '未知类型', 'neither in the list nor an old value');
check(page.bodyTypeName(undefined), '未知类型', 'no type at all');

// ---- what the device reports, and when it is pointed out
check(page.bodyTypeReported(robot()), null, 'not asked yet: nothing reported');
check({...page.bodyTypeReported(reporting('manipulator'))}, {id: 'manipulator', name: '机械臂', listed: true}, 'a listed type');
check({...page.bodyTypeReported(reporting('mobile'))}, {id: 'mobile', name: '移动机器人', listed: false}, 'a value outside the list');
check(page.bodyTypeDiffers(reporting('quadruped')), false, 'the device agrees');
check(page.bodyTypeDiffers(reporting('manipulator')), true, 'the device names another listed type');
check(page.bodyTypeDiffers(reporting('mobile')), false, 'a value outside the list is not something to change to');
check(page.bodyTypeDiffers(reporting('quadruped', {family: wheeled.id})), true, 'chosen an added type, device names a built-in one');
check(page.bodyTypeDiffers(robot()), false, 'nothing reported, nothing to point out');

// ---- choosing the type of a new device
let html = page.bodyTypeField();
includes(html, '本体类型', 'the field is named after the list');
includes(html, '<select name="family" required>', 'the form field keeps its name');
includes(html, '<option value="" selected>请选择本体类型</option>', 'nothing is chosen for the person');
includes(html, `<option value="${wheeled.id}" >${h(wheeled.name)}</option>`, 'an added type can be chosen, its name escaped');
check(count(html, '<option'), 4, 'the placeholder and the three listed types');
for (const old of ['mobile', 'humanoid', 'aerial', 'composite', 'custom']) excludes(html, `value="${old}"`, `${old} is not offered`);
includes(page.bodyTypeField('manipulator'), '<option value="manipulator" selected>机械臂</option>', 'a current type is preselected');
excludes(page.bodyTypeField('manipulator'), 'value="" selected', 'and the placeholder is not');

// The two forms that add a device are written with the seven old values (web/app.js, web/engineering.js).
const oldField = field('本体形态', select('family', Object.entries(familyNames), 'custom'));
const addForm = field('机器人名称', input('name', '例如：巡检机器人 A')) + oldField + field('设备端服务地址', '<input name="endpoint">');
for (const kind of ['robot', 'engine-ssh']) {
  html = page.bodyTypeFormFields(addForm, kind);
  excludes(html, '本体形态', `${kind}: the old field is gone`);
  excludes(html, 'value="custom"', `${kind}: "其他 / 自定义" is not offered`);
  includes(html, `value="${wheeled.id}"`, `${kind}: the added type is offered`);
  check(count(html, '<select name="family"'), 1, `${kind}: one type field`);
  includes(html, 'name="name"', `${kind}: the other fields stay`);
  includes(html, 'name="endpoint"', `${kind}: the other fields stay`);
}
check(page.bodyTypeFormFields(addForm, 'service'), addForm, 'other forms are left alone');
const noTypeField = field('机器人名称', input('name'));
check(page.bodyTypeFormFields(noTypeField, 'engine-ssh'), noTypeField, 'a form without the field (a robot that exists) is left alone');

// ---- the card of a registered device
const card = (r, type = familyNames[r.family]) =>
  `<article class="card"><div class="card-body"><h3>${h(r.name)}</h3><p>${h(type)}<br>robot@192.0.2.10:22</p>` +
  `<div class="tags"><span class="tag">硬件与软件待调查</span></div></div>` +
  `<div class="card-bottom"><div class="button-row">${sandbox.button('配置 SSH', 'engine-ssh', 'small', r.id)}</div></div></article>`;
html = page.bodyTypeCard(card(robot()), robot());
includes(html, '四足机器人', 'the type is named');
includes(html, 'data-action="engine-body-type" data-id="robot-1"', 'the type can be changed from the card');
excludes(html, 'body-type-flag', 'a listed type the device agrees with: nothing is pointed out');
check(count(html, 'robot@192.0.2.10:22'), 1, 'the rest of the card stays');

const added = robot({family: wheeled.id});
html = page.bodyTypeCard(card(added), added);
includes(html, h(wheeled.name), 'a type the platform added is named on the card');
excludes(html, '<p><br>', 'and its line is not left empty');

const old = robot({family: 'mobile'});
html = page.bodyTypeCard(card(old), old);
includes(html, '移动机器人', 'an old value keeps its name');
includes(html, '旧类型', 'and is marked as not in the list');

const differs = reporting('manipulator');
html = page.bodyTypeCard(card(differs), differs);
includes(html, '设备自报：机械臂', 'another listed type the device reports is pointed out');
includes(html, '四足机器人', 'the chosen type stays the one shown');
excludes(page.bodyTypeCard(card(reporting('mobile')), reporting('mobile')), '设备自报', 'a reported value outside the list is not flagged');

const sim = robot({sim: {kind: 'quadruped'}});
check(page.bodyTypeCard(card(sim), sim), card(sim), 'a simulated body: its card is left alone');
const unusual = '<article class="card">another layout</article>';
check(page.bodyTypeCard(unusual, robot()), unusual, 'a card of another shape is left as it is');
const both = reporting('manipulator', {family: 'mobile'});
html = page.bodyTypeCard(card(both), both);
check(count(html, 'body-type-flag'), 2, 'an old type whose device reports a listed one: both are said');

// ---- the detail of a device
html = page.bodyTypeDetail(differs);
includes(html, '<b>本体类型</b>四足机器人', 'the chosen type');
includes(html, '设备自报', 'what the device reports is shown beside it');
includes(html, '机械臂', 'by name');
excludes(page.bodyTypeDetail(robot()), '设备自报', 'nothing reported: one cell only');
includes(page.bodyTypeDetail(reporting('mobile')), '不在清单里', 'a reported value outside the list is said to be');
includes(page.bodyTypeDetail(old), '旧类型', 'an old value is marked');

// ---- text of a robot that looks like a replacement pattern stays as it is
const odd = robot({name: "Dog $& $1 $' <b>", family: wheeled.id});
html = page.bodyTypeCard(card(odd), odd);
includes(html, h(odd.name), 'the name of the robot is untouched');
includes(html, `aria-label="修改 ${h(odd.name)} 的本体类型"`, 'and escaped where it names the control');
excludes(html, '<b>"', 'markup in a name is not markup on the page');
const oddType = {...wheeled, id: 'bodytype-odd', name: "$& <i>wheels</i> $'"};
sandbox.state = {...sandbox.state, body_types: [quadruped, manipulator, oddType]};
html = page.bodyTypeFormFields(addForm, 'robot');
includes(html, h(oddType.name), 'a type name with a pattern in it is offered as it is');
check(count(html, '<select name="family"'), 1, 'and the field is swapped once');
sandbox.state = {...sandbox.state, body_types: [quadruped, manipulator, wheeled]};

// ---- which devices use a type (all projects of the workspace)
sandbox.state.robots = [robot({family: wheeled.id}),
                        robot({id: 'robot-2', name: 'Carrier <2>', project_id: 'project-b', family: wheeled.id}),
                        robot({id: 'robot-3', name: 'Dog', family: 'quadruped'})];
let users = page.bodyTypeUsers(wheeled.id);
check(Array.from(users, u => [u.id, u.name, u.project_name]), [['robot-1', 'Patrol 1', 'Site A'], ['robot-2', 'Carrier <2>', 'Site <B>']],
      'devices of every project, with the project they are in');
check(page.bodyTypeUsers('manipulator').length, 0, 'a type nobody uses');

// ---- the list on the page
html = page.bodyTypesPanel();
includes(html, '本体类型清单', 'the list has a name');
includes(html, '3 类', 'and says how many types it has');
for (const type of [quadruped, manipulator, wheeled]) includes(html, h(type.name), `${type.id} is listed`);
includes(html, 'wheeled.default', 'with its state machine template');
includes(html, '底盘速度', 'its interfaces by title');
includes(html, 'base.odometry', 'and by standard id');
includes(html, '2 台', 'and how many devices use it');
check(count(html, 'data-action="engine-body-types-remove"'), 1, 'only the added type can be removed');
includes(html, `data-action="engine-body-types-remove" data-id="${wheeled.id}"`, 'the added one');
includes(html, 'data-action="engine-body-types-add"', 'a type can be added');
excludes(html, '<A&B>', 'names are escaped');
sandbox.state = {...sandbox.state, user: null};
check(page.bodyTypesPanel(), '', 'signed out: no list');
sandbox.state = {...sandbox.state, user: {email: 'a@example.test'}};

// A capability whose title or id carries markup (the registry is the standard's, but nothing here relies on that).
sandbox.state = {...sandbox.state, body_types: [{...wheeled, required_capabilities: ['x<y>"z']}],
                 body_type_interfaces: [{id: 'x<y>"z', title: '<img src=x>', kind: 'state', safety: 'none'}]};
html = page.bodyTypesPanel();
includes(html, '&lt;img src=x&gt;', 'a title is shown as text in the list');
includes(html, 'x&lt;y&gt;&quot;z', 'and so is an id');
excludes(html, '<img', 'no markup from a title');
html = page.bodyTypeAddForm()[2];
includes(html, 'value="x&lt;y&gt;&quot;z"', 'an id is escaped as a value in the form');
excludes(html, '<img', 'no markup from a title in the form');
sandbox.state = {...sandbox.state, body_types: [quadruped, manipulator, wheeled], body_type_interfaces: interfaces};

// ---- before the page has the list (signed out, first answer on its way): nothing is changed
const ready = sandbox.state;
for (const missing of [undefined, [], 'not a list']) {
  sandbox.state = {...ready, body_types: missing};
  check(page.bodyTypeFormFields(addForm, 'robot'), addForm, 'the form keeps its field');
  check(page.bodyTypeCard(card(old), old), card(old), 'the card is left alone');
  check(page.bodyTypesPanel(), '', 'no list is shown');
  excludes(page.bodyTypeDetail(old), '旧类型', 'nothing is called an old type');
  includes(page.bodyTypeDetail(old), '移动机器人', 'the detail still names the type');
}
sandbox.state = ready;

// ---- removing a type
let [title, , body] = page.bodyTypeRemoveDialog(wheeled, users);
includes(title, '不能删除', 'in use: the dialog says no');
includes(body, '2 台设备', 'how many devices');
includes(body, 'Patrol 1', 'the devices by name');
includes(body, 'Carrier &lt;2&gt;', 'escaped');
includes(body, 'Site &lt;B&gt;', 'with their project');
excludes(body, 'engine-body-types-remove-confirm', 'and offers no way to remove it');
[title, , body] = page.bodyTypeRemoveDialog(wheeled, []);
includes(title, '删除本体类型', 'unused: a confirmation');
includes(body, `data-action="engine-body-types-remove-confirm" data-id="${wheeled.id}"`, 'that removes this type');
includes(body, h(wheeled.name), 'named');

// ---- changing the type of a registered device
let [, sub, fields, kind, , attrs] = page.bodyTypeAssignForm(differs);
check(kind, 'engine-body-type', 'the form of a type change');
includes(sub, 'Patrol 1', 'for this device');
includes(attrs, 'data-id="robot-1"', 'carried by the form');
includes(fields, '<option value="quadruped" selected>', 'the current type is preselected');
includes(fields, '设备自报', 'what the device reports is said');
includes(fields, 'data-action="engine-body-type-reported" data-id="robot-1"', 'and can be taken with one click');
[, , fields] = page.bodyTypeAssignForm(old);
includes(fields, '<option value="" selected>', 'an old value: the person has to choose');
includes(fields, '不能再改回', 'and is told the old value cannot be chosen again');
excludes(fields, 'value="mobile"', 'the old value is not offered');
[, , fields] = page.bodyTypeAssignForm(reporting('mobile'));
excludes(fields, 'engine-body-type-reported', 'a reported value outside the list cannot be taken');
excludes(page.bodyTypeAssignForm(robot())[2], '设备自报', 'nothing reported: nothing said about it');

// ---- adding a type
[, , fields, kind] = page.bodyTypeAddForm();
check(kind, 'engine-body-types-add', 'the form that adds a type');
includes(fields, 'name="name"', 'a name');
includes(fields, 'name="state_machine_template"', 'the identifier of the state machine template');
check(count(fields, 'name="required_capabilities"'), interfaces.length, 'every standard capability can be named');
includes(fields, 'value="base.velocity"', 'by its standard id');
includes(fields, '底盘速度', 'and with its title');

console.log(`body types logic: ${checks} checks passed`);
