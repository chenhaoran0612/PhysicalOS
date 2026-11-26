// Pure-logic checks of web/robot_model.js (issue #48): which robots are of one brand and model, what an
// application and a skill fit, what the forms and cards get, and what is added to a request.
//   node tests/robot_model_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const field = (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`;
const notice = (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}">${text}</div>`;

const product = (vendor, model, source = 'manual') => ({vendor, model, source});
const robots = [
  {id: 'r1', name: '狗一', created_at: '2026-11-01', product: product('Beta', 'B2')},
  {id: 'r2', name: '臂', created_at: '2026-11-02', product: product('Acme', 'X1', 'device')},
  {id: 'r3', name: '狗二', created_at: '2026-11-03', product: product('BETA', 'b2')},
  {id: 'r4', name: '旧', created_at: '2026-11-04'},
  {id: 'r5', name: '仿真', created_at: '2026-11-05', sim: {kind: 'quadruped'}, product: product('neuxmind-sim', 'sim-quadruped', 'device')},
];
const applications = [
  {id: 'a1', robot_id: 'r1', product: {vendor: 'Beta', model: 'B2'}},
  {id: 'a2', robot_id: 'r4'},
];
const state = {robots, applications, skills: []};
// What the script wraps when it loads; none of it is called here.
const sandbox = {h, button, field, notice, state, document: {addEventListener() {}, querySelector: () => null}, views: {}, render() {},
                 find: (kind, id) => state[kind].find(x => x.id === id), engineRobotCard: () => '', form: () => {},
                 engineAction: async () => {}, engineSubmit: async () => {}, action: async () => {}, api: async () => {}};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/robot_model.js', 'utf8') +
  '\n;({rmProduct, rmKey, rmLabel, rmProducts, rmFits, rmSkillFits, rmShown, rmFormFields, rmSkillFormFields, rmCard, rmPayload,' +
  ' rmDeliveryRobot, rmCloudReleaseRow, rmCloudCard, RM_NONE, RM_ALL, RM_UNSET})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, what); checks += 1; };

// ---- products
same(page.rmProduct(robots[0]), {vendor: 'Beta', model: 'B2'}, 'the product of a robot, without how it was learned');
same([page.rmProduct(robots[3]), page.rmProduct({product: {vendor: 'A'}}), page.rmProduct(null)], [null, null, null], 'no product');
same(page.rmLabel(null), page.RM_NONE, 'the label of no product');
const groups = page.rmProducts(robots);
same(groups.map(g => [g.label, g.robots.map(r => r.id)]), [['Beta B2', ['r1', 'r3']], ['Acme X1', ['r2']], ['neuxmind-sim sim-quadruped', ['r5']]],
     'robots by brand and model, case apart, in the order they were added');

// ---- what fits
same(robots.map(r => page.rmFits(applications[0], r)), [true, false, true, false, false], 'an application and the robots of its brand and model');
same(robots.map(r => page.rmFits(applications[1], r)), [false, false, false, true, false], 'an application without a product and its one robot');
ok(page.rmSkillFits({}, robots[1]) && page.rmSkillFits({product: {vendor: 'acme', model: 'x1'}}, robots[1]), 'a skill of the model, or of none yet');
ok(!page.rmSkillFits({product: {vendor: 'Beta', model: 'B2'}}, robots[1]) && !page.rmSkillFits({product: {vendor: 'Beta', model: 'B2'}}, robots[3]),
   'a skill of another model, and any bound skill for a robot without a product');
same([page.rmShown(applications[0], page.RM_ALL), page.rmShown(applications[0], page.rmKey({vendor: 'BETA', model: 'b2'})),
      page.rmShown(applications[0], page.rmKey({vendor: 'Acme', model: 'X1'})), page.rmShown(applications[1], page.RM_UNSET)],
     [true, true, false, true], 'the filter of the page');

// ---- the forms that add a robot
const family = '<label class="field">本体形态<select name="family" required><option value="quadruped">四足</option></select></label>';
const added = page.rmFormFields('<label class="field">机器人名称<input name="name"></label>' + family + '<label class="field">设备端服务地址<input name="endpoint"></label>', 'robot');
ok(added.indexOf('name="vendor"') > added.indexOf('name="family"') && added.indexOf('name="model"') < added.indexOf('name="endpoint"'),
   'the brand and model come after the body type');
ok(!/name="vendor"[^>]*required/.test(added) && added.includes('设备没有上报时必须填写'), 'by service address they may be left to the device');
ok(/name="vendor"[^>]*required/.test(page.rmFormFields(family, 'engine-ssh')) && /name="model"[^>]*required/.test(page.rmFormFields(family, 'engine-ssh')),
   'over SSH they are required');
same(page.rmFormFields('<label class="field">主机<input name="host"></label>', 'engine-ssh'), '<label class="field">主机<input name="host"></label>',
     'the SSH form of an existing robot (no body type) asks for nothing');
same(page.rmFormFields(family, 'project'), family, 'other forms are left alone');
const picker = page.rmSkillFormFields('<select name="robot_id" required><option value="r1" selected>狗一</option><option value="r4" >旧</option></select>', 'application');
ok(picker.includes('狗一（Beta B2）') && picker.includes(`旧（${page.RM_NONE}）`) && /value="r1" selected/.test(picker), 'the robots of an application form name their brand and model');
ok(/name="vendor"[^>]*required/.test(page.rmSkillFormFields('<input name="name">', 'skill')), 'a registered skill names its brand and model');

// ---- the card
const cardOf = robot => page.rmCard('<article class="card"><div class="card-body"><h3>x</h3><p>四足机器人<br>地址</p><div class="tags"></div></div>' +
  '<div class="card-bottom"><div class="button-row"></div></div></article>', robot);
ok(cardOf(robots[0]).includes('四足机器人 · <span class="robot-model-name">Beta B2</span><br>'), 'the brand and model next to the body type');
ok(cardOf(robots[0]).includes('data-action="engine-robot-model"') && cardOf(robots[0]).includes('修改品牌型号'), 'a typed one can be changed');
ok(!cardOf(robots[1]).includes('engine-robot-model') && !cardOf(robots[4]).includes('engine-robot-model'), 'a reported one, and a simulated robot, cannot');
ok(cardOf(robots[3]).includes(`status warn robot-model-flag`) && cardOf(robots[3]).includes('填写品牌型号'), 'a robot without one is flagged and can be filled in');
ok(cardOf({id: 'x', name: 'x', product: product('<b>', '"m"')}).includes('&lt;b&gt; &quot;m&quot;'), 'text from a device is shown as text');

// ---- what is sent
page.rmDeliveryRobot.a1 = 'r3';
same(page.rmPayload('harness', {application_id: 'a1'}), {application_id: 'a1'}, 'with no checklist open the choice is not used');
vm.runInContext("rmOpenDelivery = 'a1'", sandbox);
same(page.rmPayload('harness', {application_id: 'a1'}), {application_id: 'a1', robot_id: 'r3'}, 'the robot chosen in the checklist goes with the request');
same(page.rmPayload('harness', {application_id: 'a1', robot_id: 'r1'}), {application_id: 'a1', robot_id: 'r1'}, 'a robot the caller names is kept');
page.rmDeliveryRobot.a1 = 'r2';
same(page.rmPayload('onsite', {application_id: 'a1'}), {application_id: 'a1'}, 'a choice that no longer fits is not sent');
same(page.rmPayload('robots', {name: 'n'}), {name: 'n'}, 'other requests are left alone');

// ---- cloud releases
const row = '<div class="contract-line cloud-skill-release"><p>x</p></div>';
ok(page.rmCloudReleaseRow(row, {skill_id: 's', version: '0.1.0', product: {vendor: 'Acme', model: 'X1'}}, false).includes('适用品牌型号'), 'a release names what it is for');
const unbound = page.rmCloudReleaseRow(row, {skill_id: 's', version: '0.1.0', product: null}, false);
ok(unbound.includes('data-action="rm-release-bind"') && unbound.includes('data-id="s|0.1.0"') && unbound.endsWith('</div>'), 'a release that names none can be told, on 技能发布');
ok(!page.rmCloudReleaseRow(row, {skill_id: 's', version: '0.1.0', product: null}, true).includes('rm-release-bind'), 'not on 技能加载');
ok(page.rmCloudCard('<article><div class="tags"></div></article>', {latest: {product: {vendor: 'Acme', model: 'X1'}}}).includes('Acme X1'), 'a card names it too');

console.log(`robot_model_logic: ${checks} checks passed`);
