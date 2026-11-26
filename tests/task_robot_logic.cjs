// Pure-logic checks of web/task_robot.js (issue #42, third part): the field of the form of a new task,
// what a task chose, which robots are a task's on 任务工作台 and in its skills, and the line on the page
// of a task. The backend rule is brainos/task_robot.py, checked in tests/test_task_robot.py.
//   node tests/task_robot_logic.cjs
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
const count = (text, part) => String(text).split(part).length - 1;

const sandbox = {h, button};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/task_robot.js', 'utf8') +
  '\n;({taskRobotOptions, taskRobotLabel, taskRobotField, taskRobotChoice, taskRobotsOf, taskRobotLine, TASK_ROBOT_CHANGE, TASK_ROBOT_LATER})', sandbox);

const HOME = 'project-robot-home';
const arm = {id: 'r1', project_id: HOME, name: '机械臂', status: 'unconnected'};
const dog = {id: 'r2', project_id: HOME, name: '巡检狗', status: 'connected', endpoint: 'http://127.0.0.1:1'};
const sim = {id: 'r3', project_id: HOME, name: '仿真四足', status: 'connected', endpoint: 'http://127.0.0.1:2', sim: {kind: 'quadruped'}};
const robots = [arm, dog, sim];
const open = {id: 'a', name: '巡检'}, withDog = {id: 'b', name: '搬运', robot_id: 'r2'}, lost = {id: 'c', name: '清洁', robot_id: 'r-deleted'};

// ---- the field of the form of a new task
let field = page.taskRobotField(robots);
ok(field.includes('用哪台机器人来做') && field.includes('name="robot_id"'), 'the form asks which robot the task is done with');
same(count(field, '<option'), 4, 'every robot of the account, and 之后再选');
ok(/<option value="" selected>之后再选<\/option>/.test(field), 'nothing is chosen for the person');
ok(field.indexOf('巡检狗') < field.indexOf('机械臂') && field.indexOf('仿真四足') < field.indexOf('机械臂'), 'connected robots come first');
ok(field.includes('巡检狗（已连接）') && field.includes('仿真四足（仿真 · 已连接）') && field.includes('机械臂（未连接）'), 'each says what it is and whether it is connected');
ok(!field.includes(' required'), 'the choice can be left open');
field = page.taskRobotField(robots, 'r2');
ok(/<option value="r2" selected>/.test(field) && !/<option value="" selected>/.test(field), 'the robot of the task is selected when it is changed');
ok(/<option value="" selected>/.test(page.taskRobotField(robots, 'r-deleted')), 'a robot that is gone is not selected');
field = page.taskRobotField([]);
same(count(field, '<option'), 1, 'without robots only 之后再选');
ok(field.includes('还没有机器人') && field.includes('我的机器人'), 'and the way to add one is said');
const hostile = '<img src=x onerror=alert(1)>';
ok(!page.taskRobotField([{id: hostile, name: hostile, status: hostile}]).includes('<img'), 'a robot\'s name and id are text');

// ---- what a task chose
same(page.taskRobotChoice(robots, withDog), {robot: dog, gone: false, open: false}, 'a task with its robot');
same(page.taskRobotChoice(robots, open), {robot: null, gone: false, open: true}, 'a task without a choice');
same(page.taskRobotChoice(robots, lost), {robot: null, gone: true, open: false}, 'a task whose robot was deleted');
same([ids(page.taskRobotsOf(robots, withDog)), ids(page.taskRobotsOf(robots, open)), ids(page.taskRobotsOf(robots, lost))],
     [['r2'], ['r1', 'r2', 'r3'], []], 'the robots of a task: the chosen one, all without a choice, none when it is gone');

// ---- the line on the page of a task
let line = page.taskRobotLine(robots, withDog);
ok(line.includes('这个任务用的机器人：<b>巡检狗</b> · 已连接'), 'the robot of the task and its state');
ok(line.includes(`data-action="${page.TASK_ROBOT_CHANGE}"`) && line.includes('data-id="b"') && line.includes('>更换<'), 'it can be changed');
ok(line.includes('data-task-robot="r2"'), 'the line names the robot for the page');
line = page.taskRobotLine(robots, open);
ok(line.includes('还没有选择') && line.includes('>选择机器人<') && line.includes('每一台机器人'), 'no choice: said, with the way to choose');
ok(page.taskRobotLine([], open).includes('还没有添加机器人'), 'no robot at all is said');
line = page.taskRobotLine(robots, lost);
ok(line.includes('已经删除') && line.includes('>选择机器人<'), 'a deleted robot: said, with the way to choose another');
ok(!page.taskRobotLine([{id: 'x', name: hostile, status: 'connected'}], {id: 't', robot_id: 'x'}).includes('<img'), 'the name is text');
ok(!page.TASK_ROBOT_CHANGE.startsWith('task-'), 'the action is not one web/task_workbench.js takes');

// ---- 任务工作台 and the skills of a task show the chosen robot as the task's
const state = {robot_home: HOME, projects: [open, withDog, lost], robots,
               applications: [], agent_sessions: [], jobs: [], workflow_runs: [], evaluations: [], deployments: [],
               models: [], datasets: [], skills: [], services: [], operations: []};
const tw = {h, button, badge, notice, icon: k => `<svg data-icon="${k}"></svg>`, date: v => (v ? 'D' + v : '—')};
vm.createContext(tw);
const board = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') + '\n' + readFileSync('web/task_workbench.js', 'utf8') + '\n;({twFacts, twTotals, twCard, twBlocked})', tw);
const facts = task => board.twFacts(state, task);
same([ids(facts(withDog).robots), ids(facts(open).robots), ids(facts(lost).robots)], [['r2'], ['r1', 'r2', 'r3'], []],
     'the card of a task counts the robot it was created with');
ok(board.twCard(withDog, facts(withDog), false).includes('1 台 · 1 台已连接 · 巡检狗'), 'and names it');
// Issue #50: a card has no five steps any more; the robot of the task says which brand and model the task is for.
same([ids(facts(withDog).bound.robots), facts(lost).bound.robots.length, facts(open).bound.robots.length], [['r2'], 0, 0],
     'the robots a task works with: the one it was created with; none while nothing is chosen or bound');
const totals = board.twTotals([open, withDog, lost].map(t => [t, facts(t)]));
same([totals.robots, totals.online], [3, 2], 'the dashboard still counts every robot once');
same(board.twBlocked(withDog, facts(withDog)), null, 'the robot of a task does not keep the task from being deleted');
const ts = {h, button, badge, date: v => (v ? 'D' + v : '—')};
vm.createContext(ts);
const skills = vm.runInContext(readFileSync('web/task_apps.js', 'utf8') + '\n' + readFileSync('web/task_skills.js', 'utf8') + '\n;({tsWanted})', ts);
// Issue #50: without a choice the task's robots follow from the applications it has bound; it has bound none here.
same([ids(skills.tsWanted(state, withDog)), ids(skills.tsWanted(state, open))], [['r2'], []],
     'the skills of a task are read from its robot; without a choice and without an application from none');

// ---- the form of a new task has the field
const formLine = readFileSync('web/app.js', 'utf8').split('\n').find(l => l.includes("type==='project-add'"));
ok(formLine.includes("taskRobotField(rows('robots'))"), 'the form 「你希望机器人完成什么工作？」 asks for the robot');

console.log(`task_robot_logic: ${checks} checks passed`);
