// Pure-logic checks of web/task_apps.js (issue #50): which applications a task has bound, the brand and
// model it is for, what can be bound and why not, and what the page of a task, the two dialogs and
// 「机器人应用」 say.
//   node tests/task_apps_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const sandbox = {h, button, badge, icon: k => `<svg data-icon="${k}"></svg>`};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') +
  '\n;({taRoot, taNewest, taAccountApps, taBound, taKind, taLabel, taRobots, taProgress, taFacts, taCanBind, taTasksOf,' +
  ' taTaskMarkup, taPickDialog, taTasksDialog, taCardLine, taBanner, TA_STEPS})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;
const ids = list => list.map(x => x.id);

const HOME = 'home', ACME = {vendor: 'Acme', model: 'X1'}, OTHER = {vendor: 'Acme', model: 'X2'};
const A = {id: 'a', name: '仓库巡检', objective: '每天巡检'}, B = {id: 'b', name: '夜间巡检', objective: '夜里再巡'}, C = {id: 'c', name: '空任务', objective: '还没开始'};
const app = (id, more = {}) => ({id, project_id: HOME, name: '应用 ' + id, version: '1', digest: 'digest-' + id, robot_id: 'r1', created_at: '2026-11-26T01',
                                 steps: [{skill_id: 's1', entrypoint: 'navigate', parameters: {}, timeout_s: 60}], product: ACME, ...more});
const state = {
  robot_home: HOME, projects: [A, B, C],
  robots: [{id: 'r1', project_id: HOME, name: '巡检狗一号', status: 'connected', product: {...ACME, source: 'manual'}},
           {id: 'r2', project_id: HOME, name: '巡检狗二号', status: 'unconnected', product: {vendor: 'ACME', model: 'x1', source: 'device'}},
           {id: 'r3', project_id: HOME, name: '协作臂', status: 'connected', product: {...OTHER, source: 'device'}},
           {id: 'r4', project_id: HOME, name: '旧机器人', status: 'unconnected'}],
  skills: [{id: 's1', project_id: HOME, name: '前往巡检点', entrypoint: 'navigate'}],
  applications: [
    app('round'),
    app('round-2', {root_application_id: 'round', parent_application_id: 'round', version: '2', created_at: '2026-11-26T05'}),
    app('pick', {robot_id: 'r3', product: OTHER, created_at: '2026-11-26T02'}),
    app('old', {robot_id: 'r4', product: undefined, created_at: '2026-11-26T03'}),
    app('patrol', {created_at: '2026-11-26T04'}),
  ],
  task_applications: [
    {id: 'b2', project_id: 'a', application_id: 'patrol', created_at: '2026-11-26T09'},
    {id: 'b1', project_id: 'a', application_id: 'round', created_at: '2026-11-26T08'},
    {id: 'b3', project_id: 'b', application_id: 'round', created_at: '2026-11-26T10'},
  ],
  evaluations: [
    {id: 'e1', application_id: 'round-2', application_digest: 'digest-round-2', robot_id: 'r1', scope: 'static', passed: true},
    {id: 'e2', application_id: 'round', application_digest: 'digest-round', robot_id: 'r1', scope: 'onsite', passed: true},     // an older version's
    {id: 'e3', application_id: 'patrol', application_digest: 'digest-patrol', robot_id: 'r1', scope: 'static', passed: true},
    {id: 'e4', application_id: 'patrol', application_digest: 'digest-patrol', robot_id: 'r1', scope: 'onsite', passed: true},
    {id: 'e5', application_id: 'pick', application_digest: 'digest-pick', robot_id: 'r3', scope: 'static', passed: false},
  ],
  deployments: [{id: 'd1', application_id: 'patrol', application_digest: 'digest-patrol', robot_id: 'r1', status: 'deployed'},
                {id: 'd2', application_id: 'pick', application_digest: 'digest-pick', robot_id: 'r3', status: 'failed'}],
};
const find = id => state.applications.find(a => a.id === id);

// ---- which applications a task has
{
  same(page.taRoot(find('round-2')), 'round', 'every version of an application has the id of its first version');
  same(page.taNewest(state, 'round').id, 'round-2', 'the newest version is the one in use');
  same(ids(page.taAccountApps(state)), ['round-2', 'patrol', 'old', 'pick'], 'the account has every application once, newest first');
  same(ids(page.taBound(state, A)), ['round-2', 'patrol'], 'a task has what it bound, oldest binding first, each as its newest version');
  same(ids(page.taBound(state, B)), ['round-2'], 'two tasks can have the same application');
  same(ids(page.taBound(state, C)), [], 'a task that bound nothing has none');
  same(ids(page.taBound({projects: [A]}, A)), [], 'a state without the lists is read as empty');
  same(ids(page.taBound({...state, task_applications: [{id: 'x', project_id: 'c', application_id: 'gone'}]}, C)), [], 'a binding whose application is gone shows nothing');
  same(ids(page.taTasksOf(state, find('round'))), ['a', 'b'], 'the tasks an application is bound to, by any version of it');
  same(ids(page.taTasksOf(state, find('pick'))), [], 'none for one nobody bound');
}

// ---- the brand and model
{
  ok(page.taKind(find('round')) === page.taKind(app('x', {product: {vendor: 'ACME', model: 'x1'}})), 'compared without regard to case');
  ok(page.taKind(find('round')) !== page.taKind(find('pick')), 'another model is another kind');
  ok(page.taKind(find('old')) !== page.taKind(app('y', {product: undefined, robot_id: 'r1'})), 'without a brand and model, the robot it was made for says the kind');
  same(page.taLabel(state, find('pick')), 'Acme X2', 'named by brand and model');
  same(page.taLabel(state, find('old')), '旧机器人（未填写品牌型号）', 'or by its robot when it has none');
  same(ids(page.taRobots(state, find('round'))), ['r1', 'r2'], 'every robot of the brand and model can use the application');
  same(ids(page.taRobots(state, find('old'))), ['r4'], 'one without brand and model is for its robot only');
  const a = page.taFacts(state, A), c = page.taFacts(state, C);
  same([a.label, a.product, ids(a.robots), a.online, a.released], ['Acme X1', ACME, ['r1', 'r2'], 1, 1], 'a task is for the brand and model of its first application');
  same([c.label, c.kind, c.product, c.robots.length, c.released], ['', null, null, 0, 0], 'a task that bound nothing is for none yet');
  same(a.items.map(i => i.versions), [2, 1], 'how many versions each application has');
}

// ---- how far an application is
{
  same(page.taProgress(state, find('round-2')), {done: [true, false, false], next: 1, releasedOn: []}, 'evidence of an older version does not count for the version in use');
  same(page.taProgress(state, find('patrol')), {done: [true, true, true], next: -1, releasedOn: ['巡检狗一号']}, 'checked, tested and released, and where');
  same(page.taProgress(state, find('pick')), {done: [false, false, false], next: 0, releasedOn: []}, 'a failed check and a failed release are nothing');
}

// ---- what can be bound
{
  const a = page.taFacts(state, A), c = page.taFacts(state, C);
  same(page.taCanBind(state, A, a, find('round')), {ok: false, bound: true, why: '已绑定'}, 'an older version of a bound application is that application');
  const other = page.taCanBind(state, A, a, find('pick'));
  ok(!other.ok && !other.bound && other.why.includes('「Acme X1」') && other.why.includes('「Acme X2」'), 'another brand and model is refused, naming both');
  ok(!page.taCanBind(state, A, a, find('old')).ok, 'and so is an application without brand and model');
  for (const id of ['round', 'pick', 'old']) same(page.taCanBind(state, C, c, find(id)).ok, true, `a task that bound nothing takes ${id}`);
  const legacy = {...state, task_applications: [{id: 'x', project_id: 'c', application_id: 'old', created_at: '1'}]};
  ok(!page.taCanBind(legacy, C, page.taFacts(legacy, C), find('pick')).ok, 'a task of a robot without brand and model takes no application of a model');
  ok(page.taCanBind(legacy, C, page.taFacts(legacy, C), app('again', {product: undefined, robot_id: 'r4'})).ok, 'but another one made for the same robot');
}

// ---- the robot a task was created with decides (issue #42, third part)
{
  const withRobot = (robotId, more = {}) => ({id: 'w', name: '选了机器人的任务', objective: 'x', robot_id: robotId, ...more});
  const W = withRobot('r2'), st = {...state, projects: [...state.projects, W]};
  const w = page.taFacts(st, W);
  same([w.label, w.product, ids(w.robots), w.online, w.apps.length], ['ACME x1', {vendor: 'ACME', model: 'x1'}, ['r2'], 0, 0],
       'a task with a robot is for its brand and model before it binds anything, and works with that robot');
  ok(page.taCanBind(st, W, w, find('round')).ok && !page.taCanBind(st, W, w, find('pick')).ok, 'it takes applications of that brand and model only');
  const arm = withRobot('r3'), bound = {...st, projects: [arm], task_applications: [{id: 'x', project_id: 'w', application_id: 'round', created_at: '1'}]};
  const mismatch = page.taFacts(bound, arm);
  same([mismatch.label, mismatch.items[0].fits, mismatch.items[0].robots.length], ['Acme X2', false, 0], 'the robot decides against what is bound: that application is set apart and has no robot here');
  ok(page.taTaskMarkup(bound, arm, '').includes('这个任务的机器人用不了这个应用'), 'and the page says so');
  const fitting = page.taFacts({...bound, projects: [W]}, W);
  same([fitting.items[0].fits, ids(fitting.items[0].robots)], [true, ['r2']], 'a bound application of the robot\'s brand and model is used with that robot, not with every robot of the model');
  const old = withRobot('r4'), o = page.taFacts(st, old);
  same(o.label, '旧机器人（未填写品牌型号）', 'a robot without brand and model is named');
  ok(page.taCanBind(st, old, o, find('old')).ok && !page.taCanBind(st, old, o, find('round')).ok, 'and takes only what was made for it');
  const gone = page.taFacts(st, withRobot('deleted'));
  same([gone.kind, gone.chosen], [null, null], 'a robot that is gone decides nothing');
  const html = page.taTaskMarkup(st, W, '');
  ok(html.includes('<strong>ACME x1</strong>') && html.includes('由这个任务用的机器人「巡检狗二号」决定'), 'the page says where the brand and model come from');
  ok(page.taPickDialog(st, W)[2].includes('这个任务用「巡检狗二号」来做，只能绑定「ACME x1」的应用'), 'and so does the dialog');
  sandbox.taskRobotLine = (robots, task) => `<section data-task-robot="${task.robot_id}">LINE ${robots.length}</section>`;
  const lined = page.taTaskMarkup(st, W, '');
  ok(lined.indexOf('ta-head') < lined.indexOf('data-task-robot="r2"') && lined.indexOf('data-task-robot="r2"') < lined.indexOf('ta-model'),
     'the line of web/task_robot.js, with the way to change the robot, is under the head and above the brand and model');
  ok(lined.includes('LINE 4'), 'and is given every robot of the account to choose from');
  delete sandbox.taskRobotLine;
}

// ---- the page of a task
{
  const html = page.taTaskMarkup(state, A, '<section id="task-skills">SKILLS</section>');
  ok(html.indexOf('tw-back') < html.indexOf('ta-head') && html.includes('href="#overview"'), 'the way back to all tasks is above the head');
  ok(html.includes('data-action="taskapp-pick"') && html.includes('data-action="taskapp-create"'), 'the head binds an application or goes to make one');
  ok(!html.includes('application-add') && !html.includes('journey-') && !html.includes('go-agents') && !html.includes('go-training'), 'nothing on the page develops');
  ok(html.includes('<strong>Acme X1</strong>') && html.includes('2 台这样的机器人，1 台已连接'), 'the brand and model the task is for, and its robots');
  same(count(html, 'class="ta-app"'), 2, 'one row per bound application');
  ok(html.indexOf('data-app="round"') < html.indexOf('data-app="patrol"'), 'in the order they were bound');
  ok(html.includes('v2 · 共 2 个版本'), 'the version in use and how many there are');
  ok(html.includes('1 步 · 前往巡检点'), 'the steps by the names of their skills');
  ok(html.includes('2 台可用 · 1 台已连接 · 巡检狗一号、巡检狗二号'), 'the robots it can be used with');
  ok(html.includes('下一步：现场测试') && html.includes('已发布 · 已发布到 巡检狗一号'), 'how far each is');
  same(count(html, 'data-action="deliver-open"'), 2, 'each leads to its checklist');
  ok(html.includes('data-action="deliver-open" data-id="round-2"'), 'the checklist of the version in use');
  ok(html.includes('data-action="taskapp-unbind" data-id="round"') && html.includes('aria-label="解除绑定 应用 round-2"'), 'each can be unbound, by the id all versions share');
  ok(html.includes('2</span></h2><span>1 个已发布</span>'), 'the list says how many are bound and released');
  ok(html.endsWith('<section id="task-skills">SKILLS</section>'), 'the skills section comes last');

  const empty = page.taTaskMarkup(state, C, '');
  ok(empty.includes('还没有确定') && empty.includes('不同型号之间不通用'), 'a task that bound nothing says no brand and model is fixed, and why it matters');
  ok(empty.includes('这个任务还没有绑定应用') && empty.includes('工作台不开发应用') && empty.includes('从账号已有的应用里选一个绑定'), 'and what to do');
  same(count(empty, 'class="ta-app'), 0, 'without rows');
  const bare = page.taTaskMarkup({projects: [C]}, C, '');
  ok(bare.includes('账号里还没有应用') && count(bare, 'data-action="taskapp-pick"') === 1, 'with no application in the account, the empty state only sends to 机器人应用');

  const mixed = {...state, task_applications: [...state.task_applications, {id: 'b4', project_id: 'a', application_id: 'pick', created_at: '2026-11-26T11'}]};
  const mixedHtml = page.taTaskMarkup(mixed, A, '');
  same(count(mixedHtml, 'class="ta-app other"'), 1, 'an application of another brand and model bound before the rule is marked');
  ok(mixedHtml.includes('它的品牌型号和这个任务的不一样'), 'and said');
  ok(mixedHtml.includes('<strong>Acme X1</strong>'), 'the task stays with the brand and model of its first application');
}

// ---- binding from a task
{
  const [title, sub, body] = page.taPickDialog(state, A);
  same([title, sub], ['绑定应用', '仓库巡检'], 'the dialog is named after the task');
  ok(body.includes('这个任务用的是「Acme X1」的应用，只能再绑定同一品牌型号的'), 'it says which brand and model the task takes');
  same(count(body, 'class="ta-pick-group'), 3, 'the applications of the account by brand and model');
  ok(body.indexOf('Acme X1<small>') < body.indexOf('Acme X2<small>'), 'the task\'s own brand and model first');
  same(count(body, 'class="ta-pick-group other"'), 2, 'the others are set apart');
  same(count(body, '已绑定'), 2, 'what is bound already is said');
  same(count(body, 'data-action="taskapp-bind"'), 2, 'the rest have a button each');
  same(count(body, 'data-action="taskapp-bind" data-id="pick" disabled') + count(body, 'data-action="taskapp-bind" data-id="old" disabled'), 2, 'which is off for another brand and model');
  ok(body.includes('不能绑定「Acme X2」的应用'), 'with the reason');
  ok(body.includes('class="form-error"'), 'a refusal of the server has a place in the dialog');
  const free = page.taPickDialog(state, C)[2];
  ok(free.includes('绑定第一个应用后，这个任务就固定为它的品牌型号'), 'a task that bound nothing is told what the first one decides');
  same(count(free, 'disabled'), 0, 'and may take any');
  same(count(free, 'data-action="taskapp-bind" data-id="round"'), 1, 'an application is offered once, by the id all its versions share');
  ok(free.includes('<b>应用 round-2</b><small>v2'), 'as its newest version');
  ok(page.taPickDialog({projects: [C]}, C)[2].includes('账号里还没有应用') && page.taPickDialog({projects: [C]}, C)[2].includes('taskapp-create'), 'no application: the way to make one');
}

// ---- binding from 机器人应用
{
  const [title, sub, body] = page.taTasksDialog(state, find('pick'));
  same([title, sub], ['绑定到任务', '应用 pick · Acme X2'], 'the dialog names the application and its brand and model');
  same(count(body, 'class="ta-pick-row"'), 3, 'every task of the account');
  same(count(body, 'data-action="taskapp-bind-to" data-id="c|pick" '), 1, 'a free task can take it');
  ok(body.includes('data-action="taskapp-bind-to" data-id="a|pick" disabled') && body.includes('data-action="taskapp-bind-to" data-id="b|pick" disabled'), 'tasks of another brand and model cannot');
  const bound = page.taTasksDialog(state, find('round-2'))[2];
  ok(bound.includes('data-action="taskapp-unbind-from" data-id="a|round"') && bound.includes('data-id="b|round"'), 'a task that has it can let go of it here');
  ok(page.taTasksDialog({projects: []}, find('pick'))[2].includes('还没有任务'), 'no task: said');

  ok(page.taCardLine(state, find('round-2'), null).includes('已绑定任务：仓库巡检、夜间巡检'), 'a card names the tasks that have the application');
  ok(page.taCardLine(state, find('pick'), null).includes('还没有绑定到任务') && page.taCardLine(state, find('pick'), null).includes('data-action="taskapp-tasks" data-id="pick"'), 'or says none has, with the way to bind');
  ok(page.taCardLine(state, find('pick'), C).includes('data-action="taskapp-bind-to" data-id="c|pick" '), 'coming from a task: one click binds to that task');
  ok(page.taCardLine(state, find('pick'), A).includes('disabled') && page.taCardLine(state, find('pick'), A).includes('title="这个任务用的是「Acme X1」的应用'), 'or says why it cannot');
  ok(page.taCardLine(state, find('round-2'), A).includes('已绑定到这个任务'), 'or that it is bound already');
  same(page.taBanner(state, null), '', 'nobody came from a task: no banner');
  ok(page.taBanner(state, A).includes('正在为任务「仓库巡检」准备应用') && page.taBanner(state, A).includes('「Acme X1」'), 'the banner names the task and its brand and model');
  ok(page.taBanner(state, C).includes('目标机器人决定应用的品牌型号') && page.taBanner(state, C).includes('taskapp-back'), 'a free task: what decides it, and the way back');
}

// ---- nothing a person typed is markup
{
  const evilTask = {id: 'e', name: '<img src=x onerror=1>', objective: '"><script>1</script>'};
  const evilApp = app('evil', {name: '<u>app</u>', version: '<i>v</i>', product: {vendor: '<b>Beta', model: 'B2'}, robot_id: 'bot'});
  const st = {projects: [evilTask], robots: [{id: 'bot', name: '<s>bot</s>', status: 'connected', product: {vendor: '<b>beta', model: 'b2'}}],
              skills: [{id: 's1', name: '<em>skill</em>'}], applications: [evilApp], task_applications: [{id: 'b', project_id: 'e', application_id: 'evil'}]};
  const all = page.taTaskMarkup(st, evilTask, '') + page.taPickDialog(st, evilTask).join('') + page.taTasksDialog(st, evilApp).join('') +
    page.taCardLine(st, evilApp, evilTask) + page.taBanner(st, evilTask);
  for (const typed of ['<img', '<script>', '<u>app', '<i>v', '<b>Beta', '<b>beta', '<s>bot', '<em>skill']) {
    ok(!all.includes(typed), `what a person typed is escaped everywhere: ${typed}`);
  }
  ok(all.includes('&lt;u&gt;app') && all.includes('&lt;b&gt;Beta B2'), 'and shown as the text it is');
}

console.log(`task_apps_logic: ${checks} checks passed`);
