// Pure-logic checks of web/task_skills.js (issue #46): which skills the robots of a task have, where the
// task uses them, and how each use is configured (the values, the order of places and of points).
//   node tests/task_skills_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const sandbox = {h, button, badge, date: v => (v ? 'D' + v : '—')};
vm.createContext(sandbox);
// Issue #50: the applications and robots of a task are the ones it has bound (web/task_apps.js).
const page = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') + '\n' + readFileSync('web/task_skills.js', 'utf8') +
  '\n;({tsWhen, tsPlaces, tsNames, tsValue, tsRows, tsRobotView, tsLiveKey, tsWanted, tsShouldRead, tsSectionMarkup, TS_RUN_STATUS, TS_RETRY_MS})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const PLACES = ['出发点', '会议室', '二楼'];
const manifest = places => ({capabilities: [{id: 'safety.stop'}, {id: 'base.navigate_to_place', params: {places}}]});
const dog = {id: 'r1', project_id: 't', name: '巡检狗', status: 'connected', endpoint: 'http://127.0.0.1:1', manifest_v2: manifest(PLACES)};
const arm = {id: 'r2', project_id: 't', name: '机械臂', status: 'unconnected'};
const task = {id: 't', name: '办公室日常巡检', objective: '每天巡检'};

const patrolDecls = [
  {name: 'places', type: 'str', title: '巡检地点', default: '二楼、会议室'},
  {name: 'return_to', type: 'str', title: '最后回到', default: '出发点'},
  {name: 'loops', type: 'int', title: '圈数', unit: '圈', default: 1},
  {name: 'max_speed', type: 'float', title: '最快速度', unit: 'm/s'},
];
const live = {available: true, error: null, items: [
  {id: 'patrol_places', version: '0.1.0', title: '巡检', summary: '依次走到一组地点', installed_at: '2026-11-24T03:00', parameters: patrolDecls,
   requires: [{id: 'base.navigate_to_place', title: '去指定地点'}], compatible: true},
  {id: 'wave', version: '0.2.0', title: '挥手', summary: '', installed_at: '2026-11-24T03:01', compatible: true, requires: [],
   parameters: [{name: 'cycles', type: 'int', title: '次数', default: 2}]},
  {id: 'patrol', version: '0.1.0', title: '巡逻', summary: '', installed_at: '2026-11-24T03:02', compatible: true, requires: [],
   parameters: [{name: 'waypoints', type: 'object', title: '航点', unit: 'm'}, {name: 'loops', type: 'int', title: '圈数', unit: '圈', default: 1}]},
]};
const state = {
  projects: [task], robots: [dog, arm, {id: 'r9', project_id: 'other', name: '别人的', status: 'connected'}],
  skills: [{id: 's1', project_id: 't', name: '巡检（登记）', entrypoint: 'patrol_places', version: '0.1.0', parameters: patrolDecls},
           {id: 's2', project_id: 't', name: '抓取', entrypoint: 'grasp', version: '1.0.0'}],
  applications: [
    {id: 'app1', project_id: 't', name: '夜间巡检', robot_id: 'r1', version: '1', steps: [
      {skill_id: 's1', entrypoint: 'patrol_places', parameters: {places: '会议室、二楼', return_to: '', loops: 2}, timeout_s: 600},
      {skill_id: 's2', entrypoint: 'grasp', parameters: {target: 'cup'}, timeout_s: 30}]},
    {id: 'app2', project_id: 't', name: '机械臂应用', robot_id: 'r2', version: '1', steps: [{skill_id: 's2', entrypoint: 'grasp', parameters: {}, timeout_s: 30}]},
    {id: 'app9', project_id: 't', name: '没有绑定的应用', robot_id: 'r9', version: '1', steps: [{skill_id: 's2', entrypoint: 'grasp', parameters: {}, timeout_s: 30}]},
  ],
  // The task has bound two applications, each made for one robot without brand and model (data from before issue #48).
  task_applications: [{id: 'b1', project_id: 't', application_id: 'app1', created_at: '1'}, {id: 'b2', project_id: 't', application_id: 'app2', created_at: '2'}],
  operations: [
    {id: 'o1', project_id: 't', robot_id: 'r1', kind: 'skill_install', skill_id: 'patrol_places', version: '0.1.0', status: 'succeeded', created_at: '2026-11-24T03:00'},
    {id: 'o2', project_id: 't', robot_id: 'r1', kind: 'skill_run', skill_id: 'patrol', status: 'accepted', created_at: '2026-11-24T04:00',
     parameters: {waypoints: [[1, 0], [1, 1.5]], loops: 1}},
    {id: 'o3', project_id: 't', robot_id: 'r1', kind: 'skill_run', skill_id: 'patrol', status: 'rejected', created_at: '2026-11-24T05:00',
     parameters: {waypoints: [[2, 0], [2, 2], [0, 0]], loops: 3}},
    {id: 'o4', project_id: 't', robot_id: 'r2', kind: 'skill_install', skill_id: 'grasp', version: '1.0.0', status: 'succeeded', created_at: '2026-11-23T01:00'},
    {id: 'o5', project_id: 't', robot_id: 'r2', kind: 'skill_install', skill_id: 'broken', version: '1.0.0', status: 'failed', created_at: '2026-11-23T02:00'},
    {id: 'o6', project_id: 'other', robot_id: 'r9', kind: 'skill_run', skill_id: 'wave', status: 'accepted', created_at: '2026-11-24T06:00', parameters: {}},
  ],
};

// ---- the places a robot says it knows
{
  same(page.tsPlaces(dog), PLACES, 'the places come from the capability the robot declares');
  same(page.tsPlaces(arm), [], 'a robot without a manifest knows none');
  same(page.tsPlaces({manifest_v2: {capabilities: [{id: 'base.navigate_to_place', params: {places: 'x'}}]}}), [], 'a list of another shape is not a list of places');
  same(page.tsPlaces({manifest_v2: {capabilities: [{id: 'base.navigate_to_place', params: {places: ['甲 ', 3, '']}}]}}), ['甲'], 'only names count, without the blanks around them');
}

// ---- when a skill was installed
{
  same(page.tsWhen(1795500000), 1795500000000, 'a device counts seconds, the page milliseconds');
  same(page.tsWhen('2026-11-24T03:00'), '2026-11-24T03:00', 'a time written out is kept');
  same(page.tsWhen('1795500000'), 1795500000000, 'seconds sent as text are seconds too');
  same(page.tsWhen(null), null, 'no time is no time');
  same(page.tsRobotView(state, task, dog, {available: true, items: [{id: 'x', installed_at: 1795500000}]}).skills.find(s => s.id === 'x').installedAt,
       1795500000000, 'so an install read from the device has the right day');
}

// ---- one value, as a person reads it
{
  same(page.tsNames('二楼、 会议室,出发点，二楼'), ['二楼', '会议室', '出发点', '二楼'], 'names are separated by 、 or commas');
  same(page.tsValue({name: 'places', type: 'str'}, '会议室、二楼', PLACES), {kind: 'places', items: ['会议室', '二楼']}, 'names of places the robot knows are an order of places');
  same(page.tsValue({name: 'return_to', type: 'str'}, '出发点', PLACES), {kind: 'places', items: ['出发点']}, 'one place too');
  same(page.tsValue({name: 'places', type: 'str'}, '会议室、三楼', PLACES), {kind: 'text', text: '会议室、三楼'}, 'a place the robot does not know leaves the text as it is');
  same(page.tsValue({name: 'note', type: 'str'}, '会议室', []), {kind: 'text', text: '会议室'}, 'a robot that knows no places has no order of places');
  same(page.tsValue({name: 'waypoints', type: 'object', unit: 'm'}, [[1, 0], [1, 1.5]], []), {kind: 'points', items: [[1, 0], [1, 1.5]], unit: 'm'}, 'a list of points is an order of points');
  same(page.tsValue({name: 'loops', type: 'int', unit: '圈'}, 2, []), {kind: 'text', text: '2 圈'}, 'a number carries its unit');
  same(page.tsValue({name: 'on', type: 'bool'}, false, []), {kind: 'text', text: '否'}, 'yes or no in words');
  same(page.tsValue({name: 'mode', type: 'enum', options: [{value: 1, label: '慢'}, {value: 2, label: '快'}]}, 2, []), {kind: 'text', text: '快'}, 'an option by its label');
  same(page.tsValue({name: 'x', type: 'object'}, {a: 1}, []), {kind: 'text', text: '{"a":1}'}, 'anything else as it was saved');
  same(page.tsValue({name: 'return_to', type: 'str'}, '', PLACES), {kind: 'text', text: '（留空）'}, 'an empty text is said to be empty');
}

// ---- the rows of one configuration
{
  const rows = page.tsRows(patrolDecls, {places: '会议室、二楼', return_to: '', loops: 2, extra: 'x'}, PLACES);
  same(rows.map(r => r.name), ['places', 'return_to', 'loops', 'max_speed', 'extra'], 'every declared parameter, then what else was saved');
  same(rows[0].title, '巡检地点', 'by its title');
  same(rows[0].value.items, ['会议室', '二楼'], 'in the saved order');
  same(rows[2].value.text, '2 圈', 'the saved value, not the default');
  same(rows[3].origin, 'unset', 'a parameter without a value and without a default is not filled in');
  same(rows[4].title, 'extra', 'an undeclared value under its own key');
  const defaults = page.tsRows(patrolDecls, null, PLACES);
  same(defaults[0].origin, 'default', 'without saved values the defaults are shown');
  same(defaults[0].value.items, ['二楼', '会议室'], 'as the order they describe');
  same(page.tsRows(null, {a: 1}, []).map(r => [r.name, r.value.text]), [['a', '1']], 'a skill without declarations shows what was saved');
  same(page.tsRows([], {}, []), [], 'no parameters, no rows');
}

// ---- a robot whose skills were read from the device
{
  const view = page.tsRobotView(state, task, dog, live);
  same(view.places, PLACES, 'its places');
  same(view.source, 'device', 'read from the device');
  same(view.skills.map(s => s.id), ['patrol_places', 'patrol', 'grasp', 'wave'], 'skills in use first, in the order of first use; the unused after them');
  const patrolPlaces = view.skills[0];
  same([patrolPlaces.title, patrolPlaces.version, patrolPlaces.installed], ['巡检', '0.1.0', 'device'], 'what the device says about it');
  same(patrolPlaces.uses.length, 1, 'used by one step');
  same(patrolPlaces.uses[0].label, '应用「夜间巡检」第 1 步（共 2 步）', 'which says where');
  same(patrolPlaces.uses[0].timeout, 600, 'with its time limit');
  same(patrolPlaces.uses[0].rows[0].value.items, ['会议室', '二楼'], 'and the order of places saved in the step');
  const patrol = view.skills[1];
  same(patrol.uses.length, 1, 'direct runs are one use');
  same(patrol.uses[0].label, '最近一次直接运行（共 2 次）', 'the latest of them');
  same(patrol.uses[0].status, 'rejected', 'with how it went');
  same(patrol.uses[0].rows[0].value.items, [[2, 0], [2, 2], [0, 0]], 'and its points in order');
  const grasp = view.skills[2];
  same([grasp.title, grasp.installed], ['抓取', 'missing'], 'a skill a step names but the device does not have is said to be missing');
  same(view.skills[3].uses.length, 0, 'an installed skill the task does not use');
  same(view.skills[3].defaults.map(r => [r.name, r.value.text, r.origin]), [['cycles', '2', 'default']], 'shows the values it would run with');
  same(view.used, 3, 'three skills in use');
  same(view.installed, 3, 'three installed');
}

// ---- a robot that could not be read: what the console itself recorded
{
  const view = page.tsRobotView(state, task, arm, undefined);
  same(view.source, 'record', 'from the records of the console');
  same(view.skills.map(s => [s.id, s.installed, s.version]), [['grasp', 'record', '1.0.0']], 'what it installed successfully; a failed install is not a skill');
  same(view.skills[0].uses[0].label, '应用「机械臂应用」第 1 步（共 1 步）', 'and where the task uses it');
  const failed = page.tsRobotView(state, task, dog, {available: false, items: [], error: '读取已安装技能失败：超时'});
  same(failed.source, 'record', 'a device that did not answer is read from the records too');
  same(failed.error, '读取已安装技能失败：超时', 'and the reason is kept');
  same(failed.skills.find(s => s.id === 'grasp').installed, 'unknown', 'without the device nobody knows whether a skill named by a step is on it');
  same(page.tsRobotView(state, task, dog, {loading: true}).loading, true, 'while the device is being read the view says so');
}

// ---- which robots are read from the device, and when again
{
  same(page.tsWanted(state, task).map(r => r.id), ['r1'], 'only connected robots of this task with an address');
  const key = page.tsLiveKey(state, dog);
  ok(key.startsWith('r1|'), 'the key names the robot');
  const more = {...state, operations: [...state.operations, {id: 'o7', robot_id: 'r1', kind: 'skill_install', skill_id: 'wave', status: 'succeeded'}]};
  ok(page.tsLiveKey(more, dog) !== key, 'another install is a reason to read again');
  ok(page.tsLiveKey({...state, operations: [...state.operations, {id: 'o8', robot_id: 'r1', kind: 'skill_run', skill_id: 'wave', status: 'accepted'}]}, dog) === key,
     'a run is not');
  same(page.tsShouldRead(undefined, 0), true, 'a robot not asked yet is asked');
  same(page.tsShouldRead({loading: true}, 1e9), false, 'not twice at once');
  same(page.tsShouldRead(live, 1e9), false, 'an answer is kept');
  same(page.tsShouldRead({available: false, failed: true, at: 1000}, 1000 + page.TS_RETRY_MS - 1), false, 'a failure is not asked again at once');
  same(page.tsShouldRead({available: false, failed: true, at: 1000}, 1000 + page.TS_RETRY_MS), true, 'but again later');
  const noAddress = page.tsSectionMarkup({projects: [task], robots: [{id: 'n', project_id: 't', name: 'N', status: 'connected'}],
    applications: [{id: 'a', project_id: 't', name: 'A', robot_id: 'n', steps: []}], task_applications: [{id: 'b', project_id: 't', application_id: 'a'}]}, task, {});
  ok(noAddress.includes('没有设备地址') && !noAddress.includes('现在没有连接'), 'a connected robot without an address is not called disconnected');
}

// ---- the section
{
  const html = page.tsSectionMarkup(state, task, {[page.tsLiveKey(state, dog)]: live});
  ok(html.startsWith('<section class="ts" id="task-skills"'), 'one section with a fixed id');
  ok(html.includes('技能与配置'), 'named');
  same(count(html, 'data-ts-robot='), 2, 'one block per robot the bound applications can be used with');
  ok(!html.includes('别人的') && !html.includes('没有绑定的应用'), 'a robot only an application the task did not bind is for is not here');
  ok(html.includes('出发点') && html.includes('这台机器人认识的地点'), 'the places the robot knows');
  ok(html.includes('没有报告') && html.includes('坐标'), 'and that it reported names only, no coordinates or map');
  ok(html.includes('<ol class="ts-route"') && html.indexOf('会议室</li>') < html.indexOf('二楼</li>'), 'the order of places as an ordered list, in the saved order');
  ok(html.includes('<ol class="ts-points"') && html.includes('x 2') && html.includes('y 2'), 'points with their coordinates');
  ok(html.includes('应用「夜间巡检」第 1 步（共 2 步）') && html.includes('最多 600 秒'), 'where a configuration comes from and its time limit');
  ok(html.includes(page.TS_RUN_STATUS.rejected), 'how the latest run went');
  ok(html.includes('这个任务还没有用到'), 'installed skills the task does not use are told apart');
  ok(html.includes('设备上没有这个技能'), 'and so are skills that are missing on the device');
  ok(html.includes('data-action="task-skills-reload"'), 'the device can be read again');
  ok(html.includes('按控制台的安装记录'), 'a robot that is not connected is shown from the records');

  const loading = page.tsSectionMarkup(state, task, {[page.tsLiveKey(state, dog)]: {loading: true}});
  ok(loading.includes('正在读取这台机器人上的技能'), 'reading is said while it lasts');
  same(page.tsSectionMarkup({projects: [task], robots: [dog]}, task, {}), '', 'a task that bound no application has no such section');
  const gone = {projects: [task], robots: [], applications: [{id: 'a', project_id: 't', name: 'A', robot_id: 'r1', steps: [], product: {vendor: 'V', model: 'M'}}],
                task_applications: [{id: 'b', project_id: 't', application_id: 'a'}]};
  const none = page.tsSectionMarkup(gone, task, {});
  ok(none.includes('账号里还没有这个品牌型号的机器人'), 'a task whose brand and model has no robot says what is missing');
  ok(none.includes('data-action="go-robots"'), 'and where to go');
  // One brand and model: every robot of it, and no other.
  const twin = {...dog, id: 'r3', name: '二号', product: {vendor: 'V', model: 'M'}}, same1 = {...dog, product: {vendor: 'v', model: 'm'}};
  const model = page.tsSectionMarkup({...gone, robots: [same1, twin, arm]}, task, {});
  same(count(model, 'data-ts-robot='), 2, 'the robots of the task\'s brand and model, compared without regard to case');
  ok(!model.includes('机械臂'), 'a robot of another brand and model is not the task\'s');

  const evil = {id: 'e', project_id: 't', name: '<img src=x>', status: 'unconnected', manifest_v2: manifest(['<b>p</b>'])};
  const bad = page.tsSectionMarkup({projects: [task], robots: [evil], skills: [{id: 's', project_id: 't', name: '<u>s</u>', entrypoint: '<i>e</i>'}],
    applications: [{id: 'a', project_id: 't', name: '<script>1</script>', robot_id: 'e', steps: [{skill_id: 's', entrypoint: '<i>e</i>', parameters: {'<k>': '<v>'}, timeout_s: 1}]}],
    task_applications: [{id: 'b', project_id: 't', application_id: 'a'}]}, task, {});
  ok(!/<(img|b|u|i|script|k|v)>/.test(bad.replace(/<i class="dot"><\/i>/g, '')) && !bad.includes('<img'), 'nothing a person or a device wrote is markup');
}

console.log(`task_skills_logic: ${checks} checks passed`);
