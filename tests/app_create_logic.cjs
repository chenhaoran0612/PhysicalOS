// Pure-logic checks of web/app_create.js (issue #68): 「机器人应用」 shows the applications and nothing
// else; an application is created for a task (required) and a brand and model, which the task decides
// when it has one; the skills of the brand and model are offered without installing anything.
//   node tests/app_create_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const field = (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`;
const empty = (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b ? `<div>${b}</div>` : ''}</div>`;
const sandbox = {h, button, badge, field, empty, icon: k => `<svg data-icon="${k}"></svg>`};
vm.createContext(sandbox);
const page = vm.runInContext(['task_apps', 'app_gallery', 'app_create'].map(f => readFileSync(`web/${f}.js`, 'utf8')).join('\n') +
  '\n;({acGallery, acHead, acBlocked, acStartTask, acTaskField, acLock, acFields, acSkills, acSkillLabel, acStatus, AC_PICK_TASK})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const HOME = 'home', ACME = {vendor: 'Acme', model: 'X1'}, DUCK = {vendor: 'Microduck', model: 'AD-P102'};
const step = id => ({skill_id: id, entrypoint: 'navigate', parameters: {}, timeout_s: 60, requires: [], model_digests: {}});
const state = {
  robot_home: HOME,
  projects: [{id: 'free', name: '空任务'}, {id: 'dog', name: '仓库<巡检>', robot_id: 'r1'}, {id: 'duck', name: '小黄鸭能力展示'}, {id: 'gone', name: '旧任务'}],
  robots: [{id: 'r1', project_id: HOME, name: '巡检狗一号', status: 'connected', created_at: '2026-11-20T01', product: ACME},
           {id: 'r2', project_id: HOME, name: '小黄鸭', status: 'connected', sim: true, created_at: '2026-11-20T02', product: DUCK}],
  skills: [{id: 's1', project_id: HOME, name: '前往巡检点', version: '1', entrypoint: 'navigate', product: ACME},
           {id: 's2', project_id: HOME, name: '鞠躬', version: '1', entrypoint: 'bow', product: DUCK},
           {id: 's3', project_id: HOME, name: '没有归属的技能', version: '1', entrypoint: 'x'}],
  applications: [
    {id: 'show', project_id: HOME, name: '鸭子表演', version: '1', digest: 'd1', robot_id: 'r2', created_at: '2026-11-26T01', steps: [step('s2')], product: DUCK},
    {id: 'lost', project_id: HOME, name: '旧应用', version: '1', digest: 'd2', robot_id: 'r9', created_at: '2026-11-26T02', steps: [step('s9')],
     product: {vendor: 'Gone', model: 'G1'}}],
  task_applications: [{id: 'b1', project_id: 'duck', application_id: 'show', created_at: '2026-11-26T08'},
                      {id: 'b2', project_id: 'gone', application_id: 'lost', created_at: '2026-11-26T09'}],
  evaluations: [], deployments: [],
};
const blank = {...state, applications: [], task_applications: []};

// ------------------------------------------------------------------ 1. the page: applications, or nothing
const none = page.acGallery(blank);
ok(none.includes('还没有应用') && !none.includes('class="empty"'), 'with no application the page says so, in a plain line');
same(count(none, '<button'), 0, 'and offers nothing under it: no way to add skills, no other button');
ok(!/技能|品牌型号/.test(none), 'it does not talk about skills or brands');
const some = page.acGallery(state);
same(count(some, '<article class="card ag-card"'), 2, 'every application of the account is a card, whatever its brand and model');
ok(!some.includes('robot-model-filter') && !some.includes('ta-banner'), 'no filter by brand and model, no notice about a task');
ok(some.includes('data-action="taskapp-tasks"'), 'a card can still be bound to more tasks');
same(count(page.acHead(), '<button'), 1, 'the head has one button');
ok(page.acHead().includes('data-action="application-add"') && !page.acHead().includes('appg-skills'), 'which creates an application; the skills button is gone');

// ------------------------------------------------------------------ 2. what has to be there first
same(page.acBlocked(state), null, 'with a task and a robot the form can open');
ok(page.acBlocked({...state, projects: []})[2].includes('data-action="project-add"'), 'without a task: create one first');
ok(page.acBlocked({...state, robots: []})[2].includes('data-action="go-robots"'), 'without a robot: add one first');
same(page.acBlocked({...state, skills: []}), null, 'no skill has to be added beforehand');

// ------------------------------------------------------------------ 3. the task is required
same(page.acStartTask(state, 'dog'), 'dog', 'coming from a task, that task is chosen');
same(page.acStartTask(state, 'nowhere'), '', 'a task that is gone is not');
same(page.acStartTask(state), '', 'with several tasks none is chosen for the person');
same(page.acStartTask({...state, projects: [state.projects[0]]}), 'free', 'the only task is');
const taskField = page.acTaskField(state);
ok(/<select name="task_id" required/.test(taskField), 'the task is a required choice');
ok(taskField.includes(`<option value="" selected>${page.AC_PICK_TASK}</option>`), 'that starts empty, so the form cannot be saved without choosing');
ok(taskField.includes('仓库&lt;巡检&gt;') && !taskField.includes('仓库<巡检>'), 'task names are escaped');
ok(page.acTaskField(state, 'dog').includes('<option value="dog" selected>'), 'the chosen task is selected');

// ------------------------------------------------------------------ 4. the task decides the brand and model when it has one
same(page.acLock(state, ''), {value: '', label: '', text: ''}, 'no task chosen: nothing is decided');
same(page.acLock(state, 'free').value, '', 'a task with no brand and model yet leaves the choice open');
ok(page.acLock(state, 'free').text.includes('还没有确定品牌型号'), 'and says the choice becomes its brand and model');
same([page.acLock(state, 'dog').value, page.acLock(state, 'dog').label], ['r1', 'Acme X1'], 'a task with a robot is for that robot\'s brand and model');
same(page.acLock(state, 'duck').value, 'r2', 'a task with a bound application is for that application\'s brand and model');
const stuck = page.acLock(state, 'gone');
same(stuck.value, null, 'a task whose brand and model has no robot any more cannot get an application now');
ok(stuck.text.includes('Gone G1') && stuck.text.includes('我的机器人'), 'and the form says what to do');

// ------------------------------------------------------------------ 5. the form
const fields = page.acFields(state, 'dog');
ok(fields.indexOf('name="task_id"') < fields.indexOf('name="robot_id"'), 'the task comes before the brand and model');
ok(fields.indexOf('name="robot_id"') < fields.indexOf('id="steps-container"'), 'and the steps after both');
ok(/<option value="r1" selected data-ag-target>Acme X1/.test(fields), 'the brand and model of the task is chosen');
ok(fields.includes('value="仓库&lt;巡检&gt;"'), 'the application starts with the task\'s name');
ok(fields.includes('data-action="step-add"') && fields.includes('class="inline-note ac-status"'), 'steps can be added; where the skills are reported');
ok(!/安装|加入项目/.test(fields), 'nothing in the form asks to install or add a skill');
ok(!page.acFields(state).includes('value="仓库'), 'with no task chosen the name is empty');

// ------------------------------------------------------------------ 6. the skills of a brand and model
same(page.acSkills(state, state.robots[0]).map(s => s.id), ['s1', 's3'], 'a step offers the skills of the brand and model, and those of none yet');
same(page.acSkills(state, state.robots[1]).map(s => s.id), ['s2', 's3'], 'another brand and model, other skills');
same(page.acSkills(state, null).map(s => s.id), ['s3'], 'no robot chosen: nothing of a brand and model');
const late = {id: 's9', name: '刚准备好的', version: '1', product: ACME};
same(page.acSkills(state, state.robots[0], [late, state.skills[0]]).map(s => s.id), ['s1', 's3', 's9'], 'what the server just made ready is offered even when the state was read before it');
const twins = [{id: 'a', name: '巡逻', version: '1.0.0'}, {id: 'b', name: '巡逻', version: '1.1.0'}, {id: 'c', name: '挥手', version: '1'}];
same(twins.map(s => page.acSkillLabel(s, twins)), ['巡逻 · v1.0.0', '巡逻 · v1.1.0', '挥手'], 'two skills of one name are told apart by their versions');
ok(page.acStatus({connected: 1, unread: [], product: null}, 0).includes('还没有填写品牌型号'), 'a robot with no brand and model: say what to fill in');
ok(page.acStatus({connected: 1, unread: [], skipped: ['坏技能']}, 2).includes('1 个技能的说明不完整，没有列出：坏技能'), 'a skill that could not be listed is named');
ok(page.acStatus({connected: 1, unread: []}, 3).includes('3 个技能可以直接选用，不用先安装'), 'the form says the skills need no installing');
ok(page.acStatus({connected: 2, unread: ['二号']}, 1).includes('1 台没有读到：二号'), 'a robot that could not be read is named');
ok(page.acStatus({connected: 0, unread: []}, 0).includes('没有连接'), 'no connected robot: say so, and what to do');
ok(page.acStatus({connected: 1, unread: []}, 0).includes('还没有可用的技能'), 'a brand and model without skills');

console.log(`app_create logic: ${checks} checks passed`);
