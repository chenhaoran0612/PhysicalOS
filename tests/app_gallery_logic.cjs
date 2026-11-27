// Pure-logic checks of web/app_gallery.js (issue #60): 「机器人应用」 is a waterfall of every application
// of the account, an application is opened as its workflow (the skills it calls, in order), the skills
// have a page of their own, and an application is made for a brand and model instead of one robot.
//   node tests/app_gallery_logic.cjs
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
const select = (name, items, chosen = '') => `<select name="${name}" required>${items.length ? items.map(([v, t]) =>
  `<option value="${h(v)}" ${v === chosen ? 'selected' : ''}>${h(t)}</option>`).join('') : '<option value="">请先添加相应资产</option>'}</select>`;
const notice = (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`;
const empty = (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b ? `<div>${b}</div>` : ''}</div>`;
const table = (headers, data) => `<div class="table-wrap"><table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table></div>`;
const sandbox = {h, button, badge, field, select, notice, empty, table, icon: k => `<svg data-icon="${k}"></svg>`};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') + '\n' + readFileSync('web/app_gallery.js', 'utf8') +
  '\n;({agShown, agItems, agCard, agGallery, agRepresentative, agTargets, agTargetField, agReplaceTarget, agFindApp,' +
  ' agAppPage, agSkillsPage, agHead, agBanner, agFilterBar, AG_STEPS_SHOWN, AG_ROUTES})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const HOME = 'home', ACME = {vendor: 'Acme', model: 'X1'}, OTHER = {vendor: 'Acme', model: 'X2'};
const A = {id: 'a', name: '仓库巡检'}, B = {id: 'b', name: '夜间巡检'}, C = {id: 'c', name: '空任务'};
const step = (skillId, more = {}) => ({skill_id: skillId, entrypoint: 'navigate', parameters: {goal: [1, 2]}, timeout_s: 60,
                                       success_condition: '到达目标点', stop_condition: '急停或超时', requires: ['navigation.goal'], model_digests: {}, ...more});
const app = (id, more = {}) => ({id, project_id: HOME, name: '应用 ' + id, version: '1', digest: 'digest-' + id, robot_id: 'r1', created_at: '2026-11-26T01',
                                 steps: [step('s1')], product: ACME, ...more});
const state = {
  robot_home: HOME, projects: [A, B, C],
  robots: [{id: 'r1', project_id: HOME, name: '巡检狗一号', status: 'connected', created_at: '2026-11-20T01', product: {...ACME, source: 'manual'}},
           {id: 'r2', project_id: HOME, name: '巡检狗二号', status: 'unconnected', created_at: '2026-11-20T02', product: {vendor: 'ACME', model: 'x1', source: 'device'}},
           {id: 'r3', project_id: HOME, name: '协作臂', status: 'connected', created_at: '2026-11-20T03', product: {...OTHER, source: 'device'}},
           {id: 'r4', project_id: HOME, name: '旧机器人', status: 'unconnected', created_at: '2026-11-20T04'}],
  skills: [{id: 's1', project_id: HOME, name: '前往巡检点', version: '1.2', entrypoint: 'navigate', requires: ['navigation.goal'], model_ids: [], product: ACME},
           {id: 's2', project_id: HOME, name: '拍照<记录>', version: '0.3', entrypoint: 'camera.shot', requires: ['camera'], model_ids: ['m1'], product: ACME},
           {id: 's3', project_id: HOME, name: '伸到货架', version: '1', entrypoint: 'arm.reach', requires: [], model_ids: []},
           {id: 's4', project_id: HOME, name: '回到充电桩', version: '1', entrypoint: 'dock.return', requires: [], model_ids: [], product: ACME}],
  applications: [
    app('round'),
    app('round-2', {root_application_id: 'round', parent_application_id: 'round', version: '2', created_at: '2026-11-26T05',
                    steps: [step('s1'), step('s2', {timeout_s: 15, parameters: {}}), step('gone', {entrypoint: 'dock.back'})]}),
    app('pick', {robot_id: 'r3', product: OTHER, created_at: '2026-11-26T02', steps: [step('s3')]}),
    app('old', {robot_id: 'r4', product: undefined, created_at: '2026-11-26T03'}),
    app('patrol', {name: '<b>巡逻</b>', created_at: '2026-11-26T04', steps: Array.from({length: 9}, () => step('s1'))}),
  ],
  task_applications: [
    {id: 'b1', project_id: 'a', application_id: 'round', created_at: '2026-11-26T08'},
    {id: 'b3', project_id: 'b', application_id: 'round', created_at: '2026-11-26T10'},
  ],
  evaluations: [{id: 'e1', application_id: 'round-2', application_digest: 'digest-round-2', robot_id: 'r1', scope: 'static', passed: true}],
  deployments: [],
};

// ------------------------------------------------------------------ 1. every application once, newest first
const all = page.agItems(state, '');
same(all.map(i => i.app.id), ['round-2', 'patrol', 'old', 'pick'], 'one item per application, as its newest version, newest first');
same(all.map(i => i.root), ['round', 'patrol', 'old', 'pick'], 'an item is known by its first version');
same(all[0].versions, 2, 'how many versions the application has');
same(all[0].steps.map(s => [s.n, s.name]), [[1, '前往巡检点'], [2, '拍照<记录>'], [3, 'dock.back']],
     'the workflow is the skills by name, in order; a skill that is gone is called by its entry point');
same(all[0].robots.map(r => r.id), ['r1', 'r2'], 'the robots of the brand and model (spelling does not matter)');
same(all[0].online, 1, 'how many of them are connected');
same(all[0].tasks.map(t => t.id), ['a', 'b'], 'the tasks it is bound to');
same(all[0].progress.done, [true, false, false], 'how far the version in use is');
same(page.agItems(state, 'acme\nx1').map(i => i.root), ['round', 'patrol'], 'one brand and model');
same(page.agItems(state, '-').map(i => i.root), ['old'], 'applications with no brand and model');
ok(page.agShown(state.applications[0], '') && !page.agShown(state.applications[0], 'acme\nx2'), 'agShown');

// ------------------------------------------------------------------ 2. a card of the waterfall
const card = page.agCard(state, all[0], null);
ok(card.startsWith('<article class="card ag-card"'), 'a card is still an article.card');
ok(card.includes('data-action="appg-open" data-id="round"'), 'the card opens the application');
ok(card.includes('Acme X1'), 'the brand and model it is for');
ok(card.includes('v2') && card.includes('共 2 个版本'), 'the version in use and how many there are');
same(count(card, '<li class="ag-step"'), 3, 'every step of the workflow');
ok(card.indexOf('前往巡检点') < card.indexOf('拍照&lt;记录&gt;') && card.indexOf('拍照&lt;记录&gt;') < card.indexOf('dock.back'), 'in order, escaped');
ok(card.includes('2 台同品牌型号的机器人可用'), 'which robots can use it');
ok(card.includes('已绑定任务：仓库巡检、夜间巡检'), 'the tasks that have it');
ok(card.includes('data-action="taskapp-tasks"'), 'it can be bound to a task from the card');
const long = page.agCard(state, all[1], null);
same(count(long, '<li class="ag-step"'), page.AG_STEPS_SHOWN, 'a long workflow shows its first steps');
ok(long.includes(`还有 ${9 - page.AG_STEPS_SHOWN} 步`), 'and says how many more there are');
ok(long.includes('&lt;b&gt;巡逻&lt;/b&gt;') && !long.includes('<b>巡逻</b>'), 'a name is text');
ok(page.agCard(state, all[2], null).includes('只用于：旧机器人'), 'an application with no brand and model names its robot');
ok(page.agCard(state, all[2], null).includes('未填写品牌型号'), 'and says the brand and model is missing');
const forTask = page.agCard(state, all[1], C);
ok(forTask.includes('data-action="taskapp-bind-to"') && forTask.includes('data-id="c|patrol"'), 'made for a task: bound from the card');
ok(page.agCard(state, all[0], A).includes('已绑定到这个任务'), 'made for a task that has it already');

// ------------------------------------------------------------------ 3. the page: only the waterfall
const gallery = page.agGallery(state, '', null);
ok(gallery.includes('class="cards ag-waterfall" role="list"'), 'a waterfall');
same(count(gallery, '<article class="card ag-card"'), 4, 'of every application');
ok(!gallery.includes('技能资产') && !gallery.includes('<table'), 'the skills are not on this page any more');
same(count(page.agGallery(state, 'acme\nx2', null), '<article class="card ag-card"'), 1, 'filtered');
const noneHere = page.agGallery({...state, applications: state.applications.filter(a => a.id !== 'pick')}, 'acme\nx2', null);
ok(noneHere.includes('这个品牌型号还没有应用') && noneHere.includes('data-action="application-add"'), 'a brand and model with no application');
const fresh = page.agGallery({...state, applications: [], task_applications: []}, '', null);
ok(fresh.includes('data-action="application-add"') && !fresh.includes('data-action="ux-adopt"'), 'no application yet, skills are there: make one');
const bare = page.agGallery({...state, applications: [], skills: [], task_applications: []}, '', null);
ok(bare.includes('data-action="ux-adopt"') && bare.includes('data-action="appg-skills"'), 'no skill yet: skills first');
const bar = page.agFilterBar(state, 'acme\nx1');
ok(bar.includes('全部品牌型号</b><small>4 个应用'), 'the filter counts applications, not versions');
ok(bar.includes('data-id="acme\nx1" aria-pressed="true"><b>Acme X1</b><small>2 台机器人 · 2 个应用'), 'a brand and model: its robots and applications');
ok(bar.includes('<b>Acme X2</b><small>1 台机器人 · 1 个应用') && bar.includes('<b>未填写品牌型号</b><small>1 个应用'), 'the others');
same(count(bar, 'data-action="rm-filter"'), 4, 'the chips');
same(page.agFilterBar({robots: [], applications: [], skills: []}, ''), '', 'nothing to choose from');
const head = page.agHead(state);
ok(head.includes('data-action="appg-skills"') && head.includes('data-action="application-add"'), 'the head: 技能 and 创建应用');
ok(head.includes('技能') && head.includes('>4<'), 'the 技能 button says how many there are');
ok(!head.includes('data-action="skill-add"'), '注册技能 moved to the page of the skills');

// ------------------------------------------------------------------ 4. an application is for a brand and model
const sim = {id: 'v1', name: '仿真狗', status: 'connected', sim: true, created_at: '2026-11-19T01', product: ACME};
const targets = page.agTargets(state.robots);
same(targets.map(t => [t.value, t.label]), [['r1', 'Acme X1 · 2 台机器人'], ['r3', 'Acme X2 · 1 台机器人'], ['r4', '旧机器人（未填写品牌型号）']],
     'one choice per brand and model, then the robots that have none');
same(page.agTargets(state.robots, 'r2')[0].value, 'r2', 'the robot an application already has stays its robot');
same(page.agRepresentative([sim, state.robots[1], state.robots[0]]).id, 'r1', 'a connected real robot stands for its brand and model');
same(page.agRepresentative([sim, state.robots[1]]).id, 'r2', 'then a real one');
same(page.agRepresentative([sim]).id, 'v1', 'then a simulated one');
same(page.agTargets([sim])[0].simulatedOnly, true, 'only simulated robots of a brand and model');
same(page.agTargets([sim, state.robots[0]])[0].simulatedOnly, false, 'a real one among them');
same(page.agTargets([]), [], 'no robot, no choice');
const targetField = page.agTargetField(state.robots, 'r3');
ok(targetField.includes('<select name="robot_id" required') && targetField.includes('品牌型号'), 'the field keeps its name; it asks for the brand and model');
ok(!targetField.includes('目标机器人'), 'no robot is asked for');
ok(/<option value="r3" selected data-ag-target>Acme X2/.test(targetField), 'the chosen one');
const was = field('应用名称', '<input name="name">') + field('目标机器人', select('robot_id', state.robots.map(r => [r.id, r.name]))) +
  notice('<b>仿真</b> 目标选仿真机器人时，这个应用只能做仿真测试，不能发布到设备；要部署，请选择真机。') + '<div id="steps-container"></div>';
const now = page.agReplaceTarget(was, state.robots, '');
ok(now.includes('应用名称') && now.includes('<div id="steps-container"></div>'), 'the rest of the form is kept');
ok(!now.includes('目标机器人') && !now.includes('目标选仿真机器人'), 'the robot field and its note are gone');
same(count(now, '<select name="robot_id"'), 1, 'one field in its place');
ok(now.includes('class="inline-note ag-target-note"'), 'with a place for what the choice means');
same(page.agReplaceTarget('<p>别的表单</p>', state.robots, ''), '<p>别的表单</p>', 'a form with no robot field is left alone');
const kept = page.agReplaceTarget(field('目标机器人', select('robot_id', state.robots.map(r => [r.id, r.name]), 'r2')), state.robots, 'r2');
ok(/<option value="r2" selected data-ag-target>Acme X1/.test(kept), 'changing an application keeps its robot');
ok(!/<option value="[^"]*"\s*(selected)?\s*>/.test(targetField), 'web/robot_model.js does not take the choices for robots');

const banner = page.agBanner(state, C);
ok(banner.includes('正在为任务「空任务」准备应用') && banner.includes('创建时选的品牌型号就是应用的品牌型号'), 'made for a task: the brand and model is what is chosen');
ok(!banner.includes('目标机器人'), 'no robot is spoken of');
ok(page.agBanner(state, A).includes('这个任务用的是「Acme X1」的应用'), 'a task that has a brand and model says which');
same(page.agBanner(state, null), '', 'nobody came from a task');

// ------------------------------------------------------------------ 5. the page of one application: its workflow
same(page.agFindApp(state, 'round', '').id, 'round-2', 'an application is opened as its newest version');
same(page.agFindApp(state, 'round', 'round').id, 'round', 'or as the version asked for');
same(page.agFindApp(state, 'round', 'pick').id, 'round-2', 'a version of another application is not one of its versions');
same(page.agFindApp(state, 'nope', ''), null, 'an application that is gone');
const open = page.agAppPage(state, page.agFindApp(state, 'round', ''), null);
ok(open.includes('data-action="appg-back"'), 'back to every application');
ok(open.includes('<h1>应用 round-2</h1>') && open.includes('Acme X1'), 'its name, its brand and model');
same(count(open, '<li class="ag-node ag-node-step"'), 3, 'a node per step');
ok(open.includes('ag-node-start') && open.includes('ag-node-end'), 'between a start and an end');
ok(open.includes('最长 15 秒') && open.includes('到达目标点') && open.includes('navigation.goal'), 'what a step waits for and needs');
ok(open.includes('<li><b>goal</b><code>[1,2]</code></li>'), 'the parameters a step is called with');
ok(open.includes('这个技能已经不在了'), 'a step whose skill is gone says so');
for (const [act, id] of [['revision-edit', 'round-2'], ['harness-run', 'round-2'], ['deliver-open', 'round-2'], ['taskapp-tasks', 'round-2']]) {
  ok(open.includes(`data-action="${act}" data-id="${id}"`), `${act} from the page`);
}
ok(open.includes('data-action="appg-version" data-id="round"') && open.includes('当前查看'), 'its versions');
ok(open.includes('已绑定任务：仓库巡检、夜间巡检'), 'the tasks that have it');
const older = page.agAppPage(state, page.agFindApp(state, 'round', 'round'), null);
ok(older.includes('这不是最新版本') && older.includes('data-action="appg-version" data-id="round-2"'), 'an older version says so and leads to the newest');
same(count(older, '<li class="ag-node ag-node-step"'), 1, 'and shows its own workflow');
ok(page.agAppPage(state, page.agFindApp(state, 'patrol', ''), C).includes('data-id="c|patrol"'), 'made for a task: bound from here too');

// ------------------------------------------------------------------ 6. the page of the skills
const skillsPage = page.agSkillsPage(state);
ok(skillsPage.includes('data-action="appg-back"'), 'back to the applications');
same(count(skillsPage, 'data-action="detail-skills"'), 4, 'every skill, with its details');
ok(skillsPage.includes('data-action="skill-add"') && skillsPage.includes('data-action="ux-adopt"'), 'a skill is added here');
ok(skillsPage.includes('拍照&lt;记录&gt;') && skillsPage.includes('camera.shot'), 'name and entry point');
ok(skillsPage.includes('第一次编排进应用时确定'), 'a skill with no brand and model yet');
ok(skillsPage.includes('3 个应用在用') && skillsPage.includes('还没有应用使用'), 'which skills the applications call (newest versions)');
const noSkills = page.agSkillsPage({...state, skills: []});
ok(noSkills.includes('data-action="ux-adopt"') && !noSkills.includes('<table'), 'no skill yet');
same(page.AG_ROUTES, ['app', 'app-skills'], 'the two pages under 机器人应用');

console.log(`app gallery logic: ${checks} checks passed`);
