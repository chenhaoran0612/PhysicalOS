// Pure-logic checks of web/robot_remove.js (issue #11): what the confirmation says will be deleted with a
// robot, what it says will stay, and when the deletion is not offered. The backend rules are in
// brainos/robot_remove.py, checked in tests/test_robot_remove.py.
//   node tests/robot_remove_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps two functions of earlier scripts when it loads; stand-ins are enough here.
// It also uses a few helpers of web/app.js and web/engineering.js; these are written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const sandbox = {
  engineRobotCard: () => '', engineAction: async () => {}, render: () => {}, h,
  state: {}, projectId: 'project-1',
  find: (kind, id) => (sandbox.state[kind] || []).find(x => x.id === id),
  engineActive: status => ['starting', 'running', 'waiting_input', 'cancelling'].includes(status),
  date: value => String(value),
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><div>${text}</div></div>`,
  button: (text, action, cls = '', id = '') => `<button class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  sshRouteLabel: c => (c?.jump ? `（经跳板机 ${c.jump.username}@${c.jump.host}:${c.jump.port}）` : ''),
};
vm.createContext(sandbox);
const {robotRemovePlan: plan, robotRemoveCardButton: cardButton, robotRemoveFacts: factsOf, robotRemoveDialog: dialogOf} =
  vm.runInContext(readFileSync('web/robot_remove.js', 'utf8') +
                  '\n;({robotRemovePlan, robotRemoveCardButton, robotRemoveFacts, robotRemoveDialog})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };

const robot = {id: 'robot-1', name: 'dog', endpoint: ''};
const direct = {host: '192.0.2.10', port: 22, username: 'robot', auth: 'password'};
const viaJump = {...direct, jump: {host: '203.0.113.10', port: 2222, username: 'ops', auth: 'password'}};
const facts = (changes = {}) => ({connection: direct, sessions: [], running: null, reports: 0, scans: 0, repairs: 0,
                                  applications: [], flowRunning: null, flowsUnfinished: 0, ...changes});
const session = (mode, status = 'completed') => ({id: `agent-session-${mode}-${status}`, mode, status});

// A robot that was only connected: its connection and the saved login.
let p = plan(robot, facts());
check(p.blocked, null, 'nothing runs: the deletion is offered');
check(p.lines.length, 1, 'only the connection');
includes(p.lines[0], 'robot@192.0.2.10:22', 'the address');
includes(p.lines[0], '登录信息', 'the saved login');
includes(p.notes[0], '不会登录机器人', 'the machine is not touched');
check(p.notes.length, 1, 'nothing else to say');

// Through a jump host: the route is named, so two cards of one machine can be told apart.
includes(plan(robot, facts({connection: viaJump})).lines[0], '经跳板机 ops@203.0.113.10:2222', 'the jump host');

// Investigations, repairs, inventories and hardware records are listed with their numbers.
p = plan(robot, facts({sessions: [session('discovery'), session('discovery', 'interrupted'), session('repair')],
                       reports: 2, scans: 3, repairs: 1}));
check(p.lines.length, 4, 'connection, sessions, inventories, hardware');
includes(p.lines[1], '2 次调研', 'investigations are counted');
includes(p.lines[1], '1 次修复', 'repairs are counted');
includes(p.lines[1], '设备证据', 'what the sessions read goes too');
includes(p.lines[2], '2 份本体档案', 'inventories are counted');
check(p.lines[3], '硬件扫描结果和 1 份硬件修复报告', 'hardware scans and repair reports');
check(plan(robot, facts({scans: 2})).lines[1], '硬件扫描结果', 'scans alone: no repair report is named');
check(plan(robot, facts({repairs: 2})).lines[1], '2 份硬件修复报告', 'repair reports alone');
check(plan(robot, facts({sessions: [session('repair')]})).lines[1].includes('调研'), false, 'no investigation: none is named');
check(plan(robot, facts({sessions: [session('discovery')]})).lines[1].includes('修复'), false, 'no repair: none is named');

// A robot connected by its service address, and one that was only registered.
p = plan({...robot, endpoint: 'http://192.0.2.20:8080', has_token: true}, facts({connection: undefined}));
includes(p.lines[0], 'http://192.0.2.20:8080', 'the service address');
includes(p.lines[0], '接入令牌', 'the saved token');
p = plan({...robot, endpoint: 'http://192.0.2.20:8080', has_token: false}, facts({connection: undefined}));
check(p.lines[0].includes('令牌'), false, 'no token saved: none is named');
check(plan(robot, facts({connection: undefined})).lines.length, 0, 'only registered: nothing but the robot itself');

// Applications that target the robot stay, and the user is told so by name.
p = plan(robot, facts({applications: [{name: '巡检'}, {name: '充电'}]}));
includes(p.notes[1], '2 个机器人应用', 'applications are counted');
includes(p.notes[1], '巡检、充电', 'and named');
includes(p.notes[1], '保留', 'they stay');
p = plan(robot, facts({applications: [1, 2, 3, 4, 5].map(n => ({name: 'app' + n}))}));
includes(p.notes[1], 'app1、app2、app3 等', 'a long list is cut after three names');

// Adaptation flows that were not finished cannot go on afterwards.
includes(plan(robot, facts({flowsUnfinished: 1})).notes[1], '1 个没走完的本体适配流程', 'unfinished flows');

// While something uses the robot the deletion is not offered; the way to that thing is.
p = plan(robot, facts({sessions: [session('discovery', 'running')], running: session('discovery', 'running')}));
includes(p.blocked.text, '调查会话正在运行', 'a running investigation');
check([p.blocked.action, p.blocked.id], ['engine-session', 'agent-session-discovery-running'], 'opens that session');
includes(plan(robot, facts({running: session('repair', 'waiting_input')})).blocked.text, '修复会话', 'a running repair');
includes(plan(robot, facts({running: session('other', 'starting')})).blocked.text, '工程会话', 'any other session');
p = plan(robot, facts({flowRunning: {id: 'workflow-run-1', status: 'running'}}));
includes(p.blocked.text, '本体适配流程正在运行', 'a running adaptation flow');
check([p.blocked.action, p.blocked.id], ['engine-flow-run', 'workflow-run-1'], 'opens that flow');
check(plan(robot, facts({running: session('discovery', 'running'), flowRunning: {id: 'x'}})).blocked.action, 'engine-session',
      'the session is named first');

// The card's button: last in the row, on real robots only, and it names the robot for screen readers.
const card = '<article class="card"><div class="card-bottom"><div class="button-row"><button>配置连接</button></div></div></article>';
let html = cardButton(card, {id: 'robot-1', name: 'dog <b>'});
includes(html, '<button>配置连接</button><button type="button" class="button danger-outline small robot-remove"', 'after the last button');
includes(html, 'data-action="engine-robot-remove" data-id="robot-1"', 'the action and the robot');
includes(html, 'aria-label="删除机器人 dog &lt;b&gt;"', 'the name, escaped');
check(html.endsWith('>删除</button></div></div></article>'), true, 'inside the button row');
check(cardButton(card, {id: 'robot-2', name: 'sim', sim: {kind: 'quadruped'}}), card, 'a simulated robot keeps its own delete');
check(cardButton('<article>other</article>', {id: 'robot-1', name: 'dog'}), '<article>other</article>', 'an unknown card is left alone');

// What the page's state holds about a robot: its own records in this project, nobody else's.
const mine = {project_id: 'project-1', robot_id: 'robot-1'};
const flow = (id, status, robotId = 'robot-1', template = 'robot_adaptation', projectId = 'project-1') =>
  ({id, status, project_id: projectId, definition: {template, robot_id: robotId}});
sandbox.state = {
  device_connections: [{id: 'connection-1', ...viaJump}, {id: 'connection-2', ...direct}],
  agent_sessions: [{id: 's1', mode: 'discovery', status: 'completed', ...mine}, {id: 's2', mode: 'repair', status: 'waiting_input', ...mine},
                   {id: 's3', mode: 'discovery', status: 'running', project_id: 'project-1', robot_id: 'robot-2'},
                   {id: 's4', mode: 'engineering', status: 'running', project_id: 'project-1', robot_id: null}],
  robot_inventories: [{id: 'i1', ...mine}, {id: 'i2', project_id: 'project-1', robot_id: 'robot-2'}],
  hardware_scans: [{id: 'h1', ...mine}, {id: 'h2', ...mine}],
  hardware_repairs: [{id: 'r1', ...mine}, {id: 'r2', project_id: 'project-2', robot_id: 'robot-1'}],
  applications: [{id: 'a1', name: '巡检', ...mine}, {id: 'a2', name: '别的', project_id: 'project-1', robot_id: 'robot-2'}],
  workflow_runs: [flow('w1', 'completed'), flow('w2', 'paused'), flow('w3', 'stopping'), flow('w4', 'running', 'robot-2'),
                  flow('w5', 'running', 'robot-1', null), flow('w6', 'running', 'robot-1', 'robot_adaptation', 'project-2')],
};
let f = factsOf({id: 'robot-1', connection_id: 'connection-1'});
check(f.connection.id, 'connection-1', 'the connection the robot names');
check(f.sessions.map(s => s.id), ['s1', 's2'], 'its own sessions only');
check(f.running.id, 's2', 'the one that is still going');
check([f.reports, f.scans, f.repairs], [1, 2, 1], 'its own inventories, scans and repair reports, in this project');
check(f.applications.map(a => a.name), ['巡检'], 'applications that target it');
check(f.flowRunning.id, 'w3', 'its adaptation flow that is running or stopping');
check(f.flowsUnfinished, 2, 'its adaptation flows that were not finished: the paused and the stopping one');
f = factsOf({id: 'robot-3'});
check([f.connection, f.running, f.flowRunning, f.sessions.length, f.flowsUnfinished], [undefined, null, null, 0, 0],
      'a robot with nothing');
sandbox.state = {};
check(factsOf({id: 'robot-1'}).sessions.length, 0, 'a state without these lists yet');

// Whatever a robot, an application or an address is called, the dialog shows it as text.
const hostile = '<img src=x onerror=alert(1)>';
const tagsOf = html => [...html.matchAll(/<(\w+)/g)].map(m => m[1]);
let [title, sub, body] = dialogOf({id: hostile, name: hostile, created_at: hostile, endpoint: hostile},
                                  plan({id: hostile, name: hostile, endpoint: hostile},
                                       facts({connection: {...direct, host: hostile, username: hostile}, applications: [{name: hostile}]})));
check(title, '删除机器人', 'the title');
check(tagsOf(sub), [], 'the name and the date in the subtitle are text');
check(tagsOf(body).includes('img'), false, 'no markup from a name, an address or an application');
includes(body, '&lt;img src=x onerror=alert(1)&gt;', 'they are shown, escaped');
includes(body, 'data-action="engine-robot-remove-confirm" data-id="&lt;img', 'the id on the button is escaped');
includes(body, 'class="form-error" role="alert"', 'a place for a refusal of the server');
[title, sub, body] = dialogOf({id: 'robot-1', name: hostile}, plan(robot, facts({running: session('discovery', 'running')})));
check(tagsOf(sub + body).includes('img'), false, 'the same while the robot is in use');
check(body.includes('engine-robot-remove-confirm'), false, 'in use: nothing to confirm');
includes(body, 'data-action="engine-session" data-id="agent-session-discovery-running"', 'in use: the way to the session');
check(dialogOf(robot, plan(robot, facts({connection: undefined})))[2].includes('robot-remove-list'), false, 'only registered: no list');

console.log(`robot_remove logic: ${checks} checks passed`);
