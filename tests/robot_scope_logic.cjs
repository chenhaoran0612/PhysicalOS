// Pure-logic checks of the pages for issue #42, second part: robots belong to the account, not to one
// task. The lists every page filters by the open task also hold what is kept at the robots' home
// (state.robot_home); 任务工作台 counts a shared robot once and does not ask to delete robots before a
// task; a task's skills are read from the account's robots. The backend rule is brainos/robot_scope.py,
// checked in tests/test_robot_scope.py.
//   node tests/robot_scope_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const notice = (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`;
let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const ids = list => list.map(x => x.id);

const HOME = 'project-robot-home';
const A = {id: 'a', name: '巡检', objective: '每天巡检'}, B = {id: 'b', name: '搬运', objective: '搬零件'};
const dog = {id: 'r1', project_id: HOME, name: '巡检狗', status: 'connected', endpoint: 'http://127.0.0.1:1'};
const arm = {id: 'r2', project_id: HOME, name: '机械臂', status: 'unconnected'};
const state = {
  robot_home: HOME, projects: [A, B], robots: [dog, arm],
  agent_sessions: [{id: 's-home', project_id: HOME, robot_id: 'r1', mode: 'discovery', status: 'completed'},
                   {id: 's-a', project_id: 'a', robot_id: null, mode: 'engineering', status: 'completed'}],
  robot_inventories: [{id: 'i1', project_id: HOME, robot_id: 'r1'}],
  hardware_scans: [{id: 'h1', project_id: HOME, robot_id: 'r1'}],
  applications: [{id: 'app-a', project_id: 'a', robot_id: 'r1', name: '巡检一圈'}, {id: 'app-b', project_id: 'b', robot_id: 'r1', name: '送件'}],
  operations: [{id: 'o-home', project_id: HOME, robot_id: 'r1', kind: 'skill_install', skill_id: 'patrol', status: 'succeeded'},
               {id: 'o-b', project_id: 'b', robot_id: 'r1', kind: 'skill_install', skill_id: 'carry', status: 'succeeded'}],
  agent_models: [{id: 'm-home', project_id: HOME}, {id: 'm-a', project_id: 'a'}, {id: 'm-b', project_id: 'b'}],
  agent_skills: [{id: 'k-home', project_id: HOME}, {id: 'k-a', project_id: 'a'}],
  agent_mcp_servers: [{id: 'c-home', project_id: HOME}, {id: 'c-b', project_id: 'b'}],
  workflow_runs: [], jobs: [], evaluations: [], deployments: [], models: [], datasets: [], skills: [], services: [],
};

// ---- the lists of web/app.js and web/hardware.js: the lines as they are in the files
const lines = (file, starts) => readFileSync(file, 'utf8').split('\n').filter(line => starts.some(s => line.startsWith(s))).join('\n');
const appLines = lines('web/app.js', ['const rows=', 'const ROBOT_HOME_PRIVATE=', 'function robotShared(']);
const hwLines = lines('web/hardware.js', ['const hwList=']);
same([appLines.split('\n').length, hwLines.split('\n').length], [3, 1], 'the lines are where they are expected');
const lists = {state, projectId: 'a'};
vm.createContext(lists);
vm.runInContext(appLines.replace('const rows=', 'var rows=') + '\n' + hwLines.replace('const hwList=', 'var hwList='), lists);
for (const open of ['a', 'b']) {
  lists.projectId = open;
  same(ids(lists.rows('robots')), ['r1', 'r2'], `task ${open} lists every robot of the account`);
  same(ids(lists.rows('robot_inventories')), ['i1'], `task ${open} sees what is kept about a robot`);
  same(ids(lists.hwList('hardware_scans')), ['h1'], `task ${open} sees a robot's hardware scans`);
  same(ids(lists.rows('applications')), ['app-' + open], `task ${open} lists its own applications only`);
  same(ids(lists.rows('agent_models')), ['m-' + open], `task ${open} offers its own models, not the home's`);
}
lists.projectId = 'a';
same(ids(lists.rows('agent_sessions')), ['s-home', 's-a'], 'a robot\'s investigation is listed next to the task\'s own sessions');
same([ids(lists.rows('agent_skills')), ids(lists.rows('agent_mcp_servers'))], [['k-a'], []], 'a task\'s Skills and MCP servers are its own');
lists.projectId = '';
same(ids(lists.rows('robots')), ['r1', 'r2'], 'the robots are listed with no task open');
lists.state = {...state, robot_home: undefined, robots: [{id: 'old', name: '旧'}, {id: 'mine', project_id: 'a'}]};
lists.projectId = 'a';
same(ids(lists.rows('robots')), ['mine'], 'a state without the home (an older backend) is filtered as before');

// ---- 任务工作台
const tw = {h, button, badge, notice, icon: k => `<svg data-icon="${k}"></svg>`, date: v => (v ? 'D' + v : '—')};
vm.createContext(tw);
const board = vm.runInContext(readFileSync('web/task_workbench.js', 'utf8') + '\n;({twFacts, twTotals, twBlocked, twPlan})', tw);
const factsA = board.twFacts(state, A), factsB = board.twFacts(state, B);
same([ids(factsA.robots), ids(factsB.robots)], [['r1', 'r2'], ['r1', 'r2']], 'every task has the robots of the account');
same([factsA.stages[1], factsB.stages[1]], [true, true], 'a connected robot of the account counts for every task');
same([ids(factsA.applications), ids(factsB.applications)], [['app-a'], ['app-b']], 'the applications stay each task\'s own');
same(ids(factsA.sessions), ['s-a'], 'a robot\'s investigation is not one task\'s session');
const totals = board.twTotals([[A, factsA], [B, factsB]]);
same([totals.robots, totals.online, totals.applications], [2, 1, 2], 'a robot that two tasks use is counted once');
same(board.twBlocked(A, factsA), null, 'the robots of the account do not stand in the way of deleting a task');
const plan = board.twPlan(A, factsA);
same(plan.blocked, null, 'the task is offered for deletion');
ok(!plan.lines.join('').includes('机器人（'), 'and nothing says the robots go with it');
const legacy = {...state, robots: [{...dog, project_id: 'a'}]};
ok(board.twBlocked(A, board.twFacts(legacy, A)).text.includes('还有 1 台机器人'), 'a robot still kept in the task (data from before) is said');

// ---- the skills of a task
const ts = {h, button, badge, date: v => (v ? 'D' + v : '—')};
vm.createContext(ts);
const skills = vm.runInContext(readFileSync('web/task_skills.js', 'utf8') + '\n;({tsWanted, tsRobotView})', ts);
same([ids(skills.tsWanted(state, A)), ids(skills.tsWanted(state, B))], [['r1'], ['r1']], 'both tasks read the skills of the same connected robot');
const view = skills.tsRobotView(state, A, dog, null);
ok(JSON.stringify(view).includes('patrol'), 'what was installed on the robot directly is known in every task');
ok(!JSON.stringify(view).includes('carry'), 'what another task recorded is not');

// ---- skills from the cloud: which robots are offered
const cs = {h, button, badge, notice, state, document: {addEventListener() {}}, window: {}};
vm.createContext(cs);
const offered = vm.runInContext(lines('web/cloud_skills.js', ['function cloudSkillRobots(', '  return (robots || []).filter(r => r.status']) + '\n}\ncloudSkillRobots', cs);
same(ids(offered(state.robots, 'b')), ['r1'], 'a release can be installed on a connected robot from any task');

// ---- the safety banner does not call a robot of the account "another project's"
const sf = {state, projectId: 'a', find: () => undefined};
vm.createContext(sf);
const other = vm.runInContext(lines('web/safety.js', ['const safetyOtherProject = ']).replace('const ', 'var ') + '\nsafetyOtherProject', sf);
same([other({project_id: HOME}), other({project_id: 'b'}), other({project_id: 'a'}), other(null)], [false, true, false, false],
     'only a robot that is still kept in another task is named with its task');

console.log(`robot_scope_logic: ${checks} checks passed`);
