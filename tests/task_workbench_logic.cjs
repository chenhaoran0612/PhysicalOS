// Pure-logic checks of web/task_workbench.js (issue #44): what the dashboard counts and names, what a
// task's card shows, and what the delete dialog says before the server is asked.
//   node tests/task_workbench_logic.cjs
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
const sandbox = {h, button, badge, notice, icon: k => `<svg data-icon="${k}"></svg>`, date: v => (v ? 'D' + v : '—')};
vm.createContext(sandbox);
// Issue #50: what a task has bound comes from web/task_apps.js (its own checks: tests/task_apps_logic.cjs).
const page = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') + '\n' + readFileSync('web/task_workbench.js', 'utf8') +
  '\n;({twFacts, twTotals, twBlocked, twPlan, twRemoveDialog, twCard, twBoard, twDashboardMarkup, twGuideOnce,' +
  ' TW_MARKS, TW_INDEX_SHOWN, TW_GUIDE_SEEN, TW_CARD_ROWS, TW_SESSION_ACTIVE, TW_FLOW_RUNNING, TW_JOB_ACTIVE})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const task = (id, more = {}) => ({id, name: '任务 ' + id, objective: '目标 ' + id, created_at: '2026-11-24', ...more});
const A = task('a'), B = task('b', {auto_created: true}), C = task('c');
const state = {
  user: {email: 'me@example.test', name: 'Me'},
  projects: [A, B, C],
  robots: [{id: 'r1', project_id: 'a', name: '巡检狗', status: 'connected', product: {vendor: 'Acme', model: 'X1'}},
           {id: 'r2', project_id: 'a', name: '机械臂', status: 'unconnected'},
           {id: 'r3', project_id: 'b', name: '仿真四足', status: 'unconnected', inventory_id: 'inv'}],
  // Issue #50: applications are the account's ('home'); task a has bound one, which is released on r1.
  applications: [{id: 'app1', project_id: 'home', name: '夜间巡检', version: '1', digest: 'd1', robot_id: 'r1', steps: [],
                  product: {vendor: 'Acme', model: 'X1'}, created_at: '2026-11-24'},
                 {id: 'app2', project_id: 'home', name: '没人用的应用', version: '1', digest: 'd2', robot_id: 'r2', steps: [], created_at: '2026-11-25'}],
  task_applications: [{id: 'b1', project_id: 'a', application_id: 'app1', created_at: '2026-11-24'}],
  evaluations: [{id: 'e1', project_id: 'home', application_id: 'app1', application_digest: 'd1', scope: 'static', passed: true},
                {id: 'e2', project_id: 'home', application_id: 'app1', application_digest: 'd1', scope: 'onsite', passed: true}],
  deployments: [{id: 'd1', project_id: 'home', application_id: 'app1', application_digest: 'd1', robot_id: 'r1', status: 'deployed'}],
  models: [{id: 'm1', project_id: 'a'}, {id: 'm2', project_id: 'c'}],
  datasets: [], skills: [{id: 's1', project_id: 'a'}], services: [],
  agent_sessions: [{id: 'ses1', project_id: 'c', status: 'completed'}, {id: 'ses2', project_id: 'b', status: 'running'}],
  workflow_runs: [], jobs: [{id: 'j1', project_id: 'c', status: 'succeeded', name: '训练'}],
};
const facts = t => page.twFacts(state, t);

// ---- what a task holds
{
  const a = facts(A), b = facts(B), c = facts(C);
  same(a.robots.length, 2, 'a task counts its own robots');
  same(a.online, 1, 'and how many are connected');
  same(a.applications.length, 1, 'a task has the applications it has bound, not every application of the account');
  same(a.applications[0].id, 'app1', 'the one it bound');
  same(a.bound.label, 'Acme X1', 'and is for the brand and model of that application');
  same(a.bound.robots.length, 1, 'whose robots are the ones of that brand and model');
  same(a.released, 1, 'the application is released');
  ok(a.deployed, 'so the task is deployed');
  same(a.working, 0, 'nothing of it is at work');
  same(b.applications.length, 0, 'a task that bound nothing has no application');
  same(b.deployed, false, 'and is not deployed');
  same(b.bound.label, '', 'nor for a brand and model yet');
  same(b.working, 1, 'a running session is work');
  same(c.models, 1, 'records of other tasks are not counted');
  same(c.robots.length, 0, 'nor their robots');
  for (const status of page.TW_SESSION_ACTIVE) {
    same(page.twFacts({...state, agent_sessions: [{id: 'x', project_id: 'c', status}]}, C).working, 1, `a session that is ${status} is work`);
  }
  for (const status of page.TW_FLOW_RUNNING) {
    same(page.twFacts({...state, workflow_runs: [{id: 'x', project_id: 'c', status}]}, C).working, 1, `a flow that is ${status} is work`);
  }
  for (const status of page.TW_JOB_ACTIVE) {
    same(page.twFacts({...state, jobs: [{id: 'x', project_id: 'c', status}]}, C).working, 1, `a job that is ${status} is work`);
  }
  same(page.twFacts({projects: [C]}, C).applications.length, 0, 'a state without the other lists is read as empty');
}

// ---- the numbers over all tasks
{
  const totals = page.twTotals(state.projects.map(t => [t, facts(t)]));
  same(totals.tasks, 3, 'three tasks');
  same(totals.robots, 3, 'three robots over all of them');
  same(totals.online, 1, 'one connected');
  same(totals.applications, 1, 'one application is bound');
  const shared = {...state, task_applications: [...state.task_applications, {id: 'b2', project_id: 'c', application_id: 'app1', created_at: '2026-11-25'}]};
  same(page.twTotals(shared.projects.map(t => [t, page.twFacts(shared, t)])).applications, 1, 'an application two tasks bound counts once');
  same(totals.deployed, 1, 'one task deployed');
  same(totals.working, 1, 'one task with work going on');
  same(page.twTotals([]).tasks, 0, 'no task, no numbers');
}

// ---- the dashboard: the count, the names, every task once on the wall
{
  const html = page.twDashboardMarkup(state, 'c', '');
  ok(html.includes('<strong data-tw-count>3</strong>'), 'the number of tasks is the figure of the page');
  for (const t of state.projects) {
    same(count(html, `data-task="${t.id}"`), 1, `task ${t.id} is on the wall once`);
    ok(count(html, h(t.name)) >= 2, `the name of ${t.id} is in the index and on its card`);
    same(count(html, `data-action="task-remove" data-id="${t.id}"`), 1, `task ${t.id} can be deleted from its card`);
    ok(html.includes(`aria-label="删除任务 ${t.name}"`), 'and the button says which task');
  }
  same(count(html, 'class="tw-card current"'), 1, 'one card is the task opened last');
  ok(html.includes('data-action="journey-start"'), 'a task is created from the head');
  // Issue #46: no 使用指南 in the head, and no sentence about whose tasks these are.
  ok(!html.includes('task-guide') && !html.includes('使用指南'), 'the head has no guide');
  ok(!html.includes('me@example.test') && !html.includes('其他账号看不到') && !html.includes('tw-owner'), 'the page does not say whose tasks these are');
  ok(html.indexOf('tw-board') < html.indexOf('tw-wall'), 'the board is above the wall');
  same(count(html, 'tw-empty'), 0, 'no empty state with tasks');
  const guided = page.twDashboardMarkup(state, 'c', '<section class="first-run">G</section>');
  ok(guided.indexOf('tw-board') < guided.indexOf('tw-wall') && guided.indexOf('tw-wall') < guided.indexOf('first-run'),
     'with tasks the wall is right under the board and the first-run guide after it');
  const fresh = page.twDashboardMarkup({user: state.user, projects: []}, '', '<section class="first-run">G</section>');
  ok(fresh.indexOf('tw-board') < fresh.indexOf('first-run') && fresh.indexOf('first-run') < fresh.indexOf('tw-empty'),
     'without a task the guide comes first');

  const empty = page.twDashboardMarkup({user: state.user, projects: []}, '', '');
  ok(empty.includes('<strong data-tw-count>0</strong>'), 'no task: the count is 0');
  ok(empty.includes('tw-empty'), 'and the page offers the first task');
  same(count(empty, 'tw-wall'), 0, 'without a wall');
  ok(count(empty, 'data-action="journey-start"') >= 2, 'creating is offered in the head and in the empty state');

  const many = {user: state.user, projects: Array.from({length: page.TW_INDEX_SHOWN + 4}, (_, i) => task('n' + i))};
  const long = page.twDashboardMarkup(many, '', '');
  same(count(long, '<li><button type="button" data-action="task-open"'), page.TW_INDEX_SHOWN, 'the index names the first tasks');
  ok(long.includes('另有 4 个'), 'and says how many more the wall has');
  same(count(long, 'data-task="'), page.TW_INDEX_SHOWN + 4, 'the wall has all of them');
}

// ---- nothing a person typed is markup
{
  const evil = task('x', {name: '<img src=x onerror=1>', objective: '"><script>1</script>'});
  const html = page.twDashboardMarkup({user: {email: '<b>@x'}, projects: [evil], robots: [{id: 'r', project_id: 'x', name: '<i>bot</i>'}]}, 'x', '');
  ok(!html.includes('<img') && !html.includes('<script>') && !html.includes('<i>bot') && !html.includes('<b>@x'), 'names, goals and addresses are escaped');
  const bound = {projects: [evil], applications: [{id: 'a', project_id: 'home', name: '<u>app</u>', product: {vendor: '<s>v', model: 'm'}}],
                 task_applications: [{id: 'b', project_id: 'x', application_id: 'a'}]};
  ok(!page.twCard(evil, page.twFacts(bound, evil), false).includes('<s>v') && !page.twCard(evil, page.twFacts(bound, evil), false).includes('<u>app'),
     'a brand and the name of an application are escaped on the card');
  const [title, sub, body] = page.twRemoveDialog(evil, page.twPlan(evil, page.twFacts(bound, evil)));
  ok(!(title + sub + body).includes('<img') && !body.includes('<u>app'), 'in the dialog too');
}

// ---- a card
{
  const a = page.twCard(A, facts(A), false), b = page.twCard(B, facts(B), true), c = page.twCard(C, facts(C), false);
  same(count(a, '<i class="done"'), 1, 'one mark per bound application, filled when it is released');
  same(count(a, '<div class="tw-track"') && count(a, '<i class="'), page.TW_MARKS, 'the row of marks has the same length on every card');
  ok(a.includes('绑定了 1 个应用 · 1 个已发布'), 'said in words too');
  ok(a.includes('Acme X1'), 'the brand and model the task is for');
  ok(a.includes('1 台 · 1 台已连接 · 巡检狗') && !a.includes('机械臂'), 'the robots of that brand and model are counted and named, no others');
  ok(a.includes('1 个 · 夜间巡检') && !a.includes('没人用的应用'), 'the bound application is named, not the others of the account');
  ok(b.includes('试用任务') && b.includes('上次打开') && b.includes('1 项工作进行中'), 'the tags say what is special about a task');
  ok(c.includes('还没有绑定应用') && c.includes('绑定应用后确定'), 'a task that bound nothing says so');
  same(count(c, '<i class="next"') + count(c, '<i class="done"'), 0, 'and has no mark filled');
  const waiting = {...state, deployments: []};
  same(count(page.twCard(A, page.twFacts(waiting, A), false), '<i class="next"'), 1, 'a bound application that is not released is marked as on its way');
  // Issue #46: every card has the same parts, whatever the task holds, so every card is the same size.
  const bare = task('bare'), bareCard = page.twCard(bare, page.twFacts({projects: [bare]}, bare), false);
  const long = task('long', {name: '名'.repeat(100), objective: '很长的目标。'.repeat(300)});
  const longCard = page.twCard(long, page.twFacts({projects: [long]}, long), true);
  const shape = card => card.replace(/>[^<]*</g, '><').replace(/ (title|aria-label|data-id|data-task|datetime)="[^"]*"/g, '')
    .replace(/ class="(done|next)?"/g, '').replace(/ none"/g, '"').replace(/<span class="status[^>]*><i class="dot"><\/i><\/span>/g, '').replace(' current', '');
  for (const [card, what] of [[a, 'a full task'], [c, 'a task that bound nothing'], [bareCard, 'an empty task'], [longCard, 'a task with very long texts']]) {
    same(count(card, '<div class="tw-hold'), page.TW_CARD_ROWS.length, `${what} has every row`);
    same(shape(card), shape(bareCard), `${what} has the same parts as any other card`);
  }
  same(count(bareCard, 'class="tw-hold none"'), page.TW_CARD_ROWS.length, 'a row without content says so instead of leaving');
  ok(longCard.includes(`title="${'名'.repeat(100)}"`), 'a name cut short on the card can be read in full');
  ok(c.includes('data-action="task-open"') && c.includes('data-id="c"'), 'the card leads into the task');
}

// ---- deleting
{
  const withRobots = page.twPlan(A, facts(A));
  ok(withRobots.blocked && withRobots.blocked.text.includes('还有 2 台机器人（巡检狗、机械臂）'), 'a task with robots is not offered for deletion');
  same(withRobots.blocked.action, 'task-robots', 'the way leads to its robots');
  const blockedDialog = page.twRemoveDialog(A, withRobots)[2];
  ok(!blockedDialog.includes('task-remove-confirm'), 'a blocked dialog has nothing to confirm');
  ok(blockedDialog.includes('data-action="task-robots" data-id="a"'), 'but the way to the robots of that task');

  const five = {projects: [C], robots: Array.from({length: 5}, (_, i) => ({id: 'r' + i, project_id: 'c', name: 'R' + i}))};
  ok(page.twPlan(C, page.twFacts(five, C)).blocked.text.includes('还有 5 台机器人（R0、R1、R2 等）'), 'three robots are named, the rest counted');

  const running = (kind, record, word) => {
    const blocked = page.twPlan(C, page.twFacts({projects: [C], [kind]: [{id: 'x', project_id: 'c', ...record}]}, C)).blocked;
    ok(blocked && blocked.text.includes(word), `${kind} at work keeps the task: ${word}`);
    same(blocked.action, 'task-open', 'and the way leads into the task');
  };
  running('agent_sessions', {status: 'running'}, '助手会话正在运行');
  running('workflow_runs', {status: 'running'}, '协作流程正在运行');
  running('jobs', {status: 'submitted', name: '夜间训练'}, '夜间训练');

  const plan = page.twPlan(C, facts(C));
  same(plan.blocked, null, 'a task without robots and running work can be deleted');
  same(plan.lines.length, 3, 'its model, its session and its job record are listed');
  const [title, sub, body] = page.twRemoveDialog(C, plan);
  same(title, '删除任务', 'the dialog is named');
  ok(sub.includes(C.name), 'and names the task');
  ok(body.includes('data-action="task-remove-confirm" data-id="c"'), 'the confirmation carries the task');
  ok(body.includes('删除后不能恢复'), 'and says it cannot be undone');
  ok(body.includes('class="form-error"'), 'a refusal of the server has a place in the dialog');
  ok(body.includes('不会联系任何机器人或服务'), 'and says nothing is contacted');

  const bare = task('z'), none = page.twPlan(bare, page.twFacts({projects: [bare]}, bare));
  same(none.lines.length, 0, 'an empty task lists nothing');
  ok(page.twRemoveDialog(bare, none)[2].includes('只删除任务本身'), 'and says only the task goes');

  // Issue #50: the applications are the account's; only the binding goes with the task.
  const apps = {projects: [C], applications: [{id: 'a1', project_id: 'home', name: '甲'}], task_applications: [{id: 'b', project_id: 'c', application_id: 'a1'}],
                evaluations: [{id: 'e', project_id: 'home', application_id: 'a1'}], deployments: []};
  const appsPlan = page.twPlan(C, page.twFacts(apps, C));
  same(appsPlan.lines.length, 1, 'a task with a bound application lists the binding');
  ok(appsPlan.lines[0].includes('和 1 个机器人应用的绑定（甲）') && !appsPlan.lines[0].includes('测试报告'), 'the binding goes, not the reports');
  ok(page.twRemoveDialog(C, appsPlan)[2].includes('绑定的应用本身不会删除'), 'and the dialog says the applications stay');
}

// ---- the first-run guide is for the first visit only (issue #46)
{
  const memory = () => { const kept = {}; return {kept, getItem: k => (k in kept ? kept[k] : null), setItem: (k, v) => { kept[k] = String(v); }}; };
  const G = '<section class="first-run">G</section>';
  const store = memory();
  const first = page.twGuideOnce(G, 'u1', null, store);
  same(first.html, G, 'the first visit shows the guide');
  same(first.shownFor, 'u1', 'and remembers for whom during this visit');
  same(store.kept[page.TW_GUIDE_SEEN + ':u1'], '1', 'and that this account has seen it');
  same(page.twGuideOnce(G, 'u1', first.shownFor, store).html, G, 'it stays while the page is open (the page is drawn again and again)');
  same(page.twGuideOnce(G, 'u1', null, store).html, '', 'the second visit has no guide');
  const other = page.twGuideOnce(G, 'u2', first.shownFor, store);
  same(other.html, G, 'another account on this computer has its own first visit');
  same(other.shownFor, 'u2', 'which becomes the account of this visit');
  same(page.twGuideOnce('', 'u3', null, store).html, '', 'nothing to show is nothing');
  ok(!((page.TW_GUIDE_SEEN + ':u3') in store.kept), 'and does not use up the first visit');
  const broken = {getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); }};
  same(page.twGuideOnce(G, 'u1', null, broken).html, G, 'without browser storage the guide is simply shown');
  same(page.twGuideOnce(G, '', null, store).html, G, 'without an account nothing is remembered');
}

console.log(`task_workbench_logic: ${checks} checks passed`);
