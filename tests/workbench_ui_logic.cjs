// Issue #52 — 工作台 with fewer words. Pure-logic checks of what the dashboard, the page of one task and
// its skills section say after the clean-up (web/task_workbench.js, web/task_apps.js, web/task_skills.js).
//   node tests/workbench_ui_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const notice = (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`;
const sandbox = {URL, h, button, badge, notice, icon: k => `<svg data-icon="${k}"></svg>`, date: v => (v ? 'D' + v : '—')};
vm.createContext(sandbox);
const source = name => readFileSync('web/' + name, 'utf8');
const page = vm.runInContext(['task_apps.js', 'task_workbench.js', 'task_skills.js'].map(source).join('\n') +
  '\n;({twFacts, twIsAddress, twCard, twBoard, twDashboardMarkup, taTaskMarkup, tsRobotView, tsSectionMarkup, tsLiveKey, TS_RUN_STATUS})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

const product = {vendor: 'Acme', model: 'X1'};
const task = {id: 't', name: '夜间巡检任务', objective: '每晚巡检一遍仓库', created_at: '2026-11-24'};
const idle = {id: 'u', name: '空任务', objective: '还没开始', created_at: '2026-11-25'};
const dog = {id: 'r1', project_id: 'home', name: '192.0.2.10', status: 'connected', endpoint: 'http://192.0.2.10:8787', product};
const spare = {id: 'r2', project_id: 'home', name: '备用狗', status: 'unconnected', product};
const state = {
  robot_home: 'home', projects: [task, idle], robots: [dog, spare],
  skills: [{id: 's1', project_id: 'home', name: '巡检', entrypoint: 'patrol_places', version: '1.0.0', parameters: [{name: 'loops', title: '圈数', default: 1}]}],
  applications: [{id: 'a1', project_id: 'home', name: '夜间巡检', version: '1', digest: 'd1', robot_id: 'r1', product, created_at: '2026-11-24',
                  steps: [{skill_id: 's1', entrypoint: 'patrol_places', parameters: {loops: 2}, timeout_s: 600}]}],
  task_applications: [{id: 'b1', project_id: 't', application_id: 'a1', created_at: '2026-11-24'}],
  evaluations: [], deployments: [],
  operations: [
    {id: 'o1', project_id: 'home', robot_id: 'r1', kind: 'skill_run', skill_id: 'patrol', status: 'accepted', parameters: {}, created_at: '2026-11-25T01:00:00'},
    {id: 'o2', project_id: 'home', robot_id: 'r1', kind: 'skill_run', skill_id: 'patrol', status: 'rejected', error: '设备拒绝运行：急停已锁存',
     parameters: {}, created_at: '2026-11-25T02:00:00'},
  ],
};
const list = state.projects.map(t => [t, page.twFacts(state, t)]);

// ---- the dashboard (items 1–9)
{
  const html = page.twDashboardMarkup(state, 't', '');
  ok(html.includes('<h1>工作台</h1>') && !html.includes('任务工作台'), '1: the page is called 工作台');
  ok(!html.includes('最近创建的在前'), '2: the wall does not say how it is ordered');
  ok(!html.includes('class="subtitle"'), '8: no sentence under the name of the page');
  ok(html.includes('全部任务') && html.includes('新建任务'), 'the list and the way to make a task stay');

  const board = page.twBoard(list);
  ok(!board.includes('现在没有正在进行的工作') && !board.includes('从新建一个任务开始'), '3: nothing is said while no work is in progress');
  ok(!/<dl class="tw-figures">.*<small>/.test(board), '5–7: the three numbers have no explanation under them');
  ok(!board.includes('台已连接') && !board.includes('应用已发布到机器人') && !board.includes('任务用到的机器人应用'), '5–7: and none of the old ones is left');
  same(text(board.slice(board.indexOf('<dl class="tw-figures">'))).trim(), '机器人 2 绑定的应用 1 已部署的任务 0', 'the numbers themselves stay');
  const busy = page.twBoard([[task, {...page.twFacts(state, task), working: 2}]]);
  ok(busy.includes('1 个任务有工作正在进行'), 'work in progress is still said');

  const card = page.twCard(task, page.twFacts(state, task), false);
  ok(!card.includes('192.0.2.10'), '4: a card does not show the address of a robot that has no other name');
  ok(card.includes('2 台 · 1 台已连接 · 备用狗</dd>'), 'it counts the robots and names the ones that have a name');
  const hosts = {...state, robots: [{...dog, name: 'dog.example.test:8787', endpoint: 'http://dog.example.test:8787'}, {...spare, name: '198.51.100.7:22', status: 'connected'}]};
  ok(page.twCard(task, page.twFacts(hosts, task), false).includes('>2 台 · 2 台已连接</dd>'), 'a host name with a port and an address with a port are addresses too');
  ok(card.includes('1 个 · 夜间巡检') && card.includes('绑定了 1 个应用 · 0 个已发布') && card.includes('Acme X1'), '14: the bound application is on the card');
}

// ---- the page of one task (items 10–12, 14)
{
  const html = page.taTaskMarkup(state, task, '');
  ok(html.includes(`<h1>${task.name}</h1>`), 'the task is named');
  ok(!html.includes('class="eyebrow"') && !html.includes('class="subtitle"') && !html.includes(task.objective), '10: no line above the name and no sentence under it');
  for (const gone of ['从任务到上岗', '补齐完成任务所需的能力', '现在做这一步', 'journey-step', '需要时，再使用这些能力', 'journey-option']) {
    ok(!html.includes(gone), `11–12: 「${gone}」 is not on the page`);
  }
  ok(html.includes('已绑定的应用') && html.includes('data-app="a1"') && html.includes('夜间巡检') && html.includes('配置检查'), '14: the applications the task has bound, and how far each is');
  ok(html.includes('data-action="taskapp-pick"'), 'and the way to bind another');
}

// ---- 技能与配置 (item 13)
{
  const live = {available: true, items: [
    {id: 'patrol_places', title: '按地点巡检', version: '1.0.0', installed_at: 1764000000, parameters: [{name: 'loops', title: '圈数', default: 1}]},
    {id: 'patrol', title: '巡逻', version: '1.0.0', installed_at: 1764000000, parameters: []},
    {id: 'free_walk', title: '自由行走', version: '1.0.0', installed_at: 1764000000, parameters: []}]};
  const html = page.tsSectionMarkup(state, task, {[page.tsLiveKey(state, dog)]: live});
  ok(html.includes('<h2>技能与配置</h2>'), 'the section stays');
  ok(!html.includes('装了哪些技能'), 'the sentence beside its name is gone');
  ok(!html.includes('技能列表是刚从这台机器人上读到的'), 'so is the sentence about where the list came from');
  ok(!html.includes('重新读取') && !html.includes('task-skills-reload'), 'and the button to read again');
  const head = html.slice(html.indexOf('<header class="ts-robot-head">'), html.indexOf('</header>'));
  ok(/<span class="ts-state">.*已连接.*<\/span>$/.test(head), 'whether the robot is connected stands where the button was: last in the head');
  ok(!head.slice(0, head.indexOf('<span class="ts-state">')).includes('已连接'), 'and only there');

  const view = page.tsRobotView(state, task, dog, live);
  const of = id => view.skills.find(s => s.id === id);
  same(JSON.stringify(of('patrol').runs), JSON.stringify({count: 2, when: '2026-11-25T02:00:00', status: 'rejected', error: '设备拒绝运行：急停已锁存'}),
       'a skill knows how often it ran and how the latest run went');
  same(of('free_walk').runs.count, 0, 'and that it never ran');
  const block = id => html.slice(html.indexOf(`data-ts-skill="${id}"`), html.indexOf('</article>', html.indexOf(`data-ts-skill="${id}"`)));
  const ran = text(block('patrol').slice(block('patrol').indexOf('<p class="ts-run"'), block('patrol').indexOf('</p>', block('patrol').indexOf('<p class="ts-run"'))));
  ok(ran.includes('运行过 2 次') && ran.includes('D2026-11-25T02:00:00') && ran.includes(page.TS_RUN_STATUS.rejected) && ran.includes('急停已锁存'),
     'every skill shows its run information: ' + ran);
  ok(block('free_walk').includes('<p class="ts-run none"') && block('free_walk').includes('还没有运行过'), 'also one that never ran');
  ok(block('patrol_places').includes('应用「夜间巡检」第 1 步') && block('patrol_places').includes('ts-run'), 'next to how the task configures it');

  const evil = {...state, operations: [{id: 'o9', project_id: 'home', robot_id: 'r1', kind: 'skill_run', skill_id: 'patrol', status: 'failed', error: '<img src=x>', parameters: {}}]};
  ok(!page.tsSectionMarkup(evil, task, {[page.tsLiveKey(evil, dog)]: live}).includes('<img'), 'what a device answered to a run is text, not markup');

  const record = page.tsSectionMarkup(state, {...task, robot_id: 'r2'}, {});
  ok(record.includes('按控制台的安装记录显示') && record.includes('未连接'), 'a robot that cannot be read still says why the list is from the records');
  const loading = page.tsSectionMarkup(state, task, {[page.tsLiveKey(state, dog)]: {loading: true}});
  ok(loading.includes('正在读取这台机器人上的技能'), 'and reading is said while it lasts');
}

console.log(`workbench_ui_logic: ${checks} checks passed`);
