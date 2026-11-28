// Pure-logic checks of web/task_sim.js (issues #66 and #75): where a task's simulation stands, where each of its
// skills' environments stands, and what a skill's page offers.
//   node tests/task_sim_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const t = vm.runInContext(readFileSync('web/task_sim.js', 'utf8') +
  '\n;({tsimStatus, tsimCardAction, tsimSkillStatus, tsimSkillAction, tsimBuilt, tsimValue, tsimUseLine, tsimRunBlock, tsimSkillPanel, tsimEsc})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(JSON.stringify(actual), JSON.stringify(expected), what); checks += 1; };

// ---- where a task stands: counted over its skills' environments
const counts = (skills, ready = 0, building = 0, failed = 0) => ({counts: {skills, ready, building, failed}});
same(t.tsimStatus({}).key, 'empty', 'not read: no skills');
same(t.tsimStatus(counts(3)).key, 'none', 'skills, nothing built');
same(t.tsimStatus({...counts(0), problem: {code: 'task_sim_no_product'}}).key, 'blocked', 'cannot be simulated');
same(t.tsimStatus({...counts(4, 4), model: {staged: true}}).key, 'ready', 'a robot with its own stage needs no build');
same(t.tsimStatus({...counts(0), model: {staged: true}}).key, 'empty', 'its own stage but no skill to run');
same(t.tsimStatus(counts(3, 1, 1)).key, 'building', 'a build under way');
same(t.tsimStatus(counts(3, 3)), {key: 'saved', text: '每个技能都有环境', tone: 'good'}, 'every skill has one');
same(t.tsimStatus(counts(3, 1)), {key: 'partial', text: '1 / 3 个技能有环境', tone: 'good'}, 'some have one');
same(t.tsimStatus(counts(3, 0, 0, 1)).key, 'failed', 'a failed first build');
same(t.tsimStatus({...counts(3, 1), problem: {code: 'sim_engine_missing'}}).key, 'partial', 'saved environments are shown although something is missing now');

// ---- the button of a task's card
same(t.tsimCardAction(counts(3, 1)), {label: '选择技能', primary: true}, 'a task opens on its skills');
same(t.tsimCardAction({...counts(0), problem: {}}), {label: '查看原因', primary: false}, 'blocked: the reason');

// ---- where one skill's environment stands
const saved = {source_name: '空场地', stale: false};
same(t.tsimSkillStatus({}).key, 'none', 'nothing built');
same(t.tsimSkillStatus({environment: {staged: true, source_name: '自带场地', stale: false}}).key, 'ready', 'a stage of its own');
same(t.tsimSkillStatus({build: {busy: true, status: 'generating'}}).key, 'building', 'a build under way');
same(t.tsimSkillStatus({build: {busy: true}, environment: saved}).key, 'building', 'a rebuild shows as building');
same(t.tsimSkillStatus({environment: saved}), {key: 'saved', text: '已保存 · 空场地', tone: 'good'}, 'saved');
same(t.tsimSkillStatus({environment: saved, build: {busy: false, status: 'failed'}}).key, 'saved', 'a failed rebuild leaves the saved one in use');
same(t.tsimSkillStatus({build: {busy: false, status: 'failed'}}).key, 'failed', 'a failed first build');
same(t.tsimSkillStatus({environment: {...saved, stale: true}}).key, 'stale', 'built for another brand and model');
same(t.tsimSkillAction({environment: saved}), {label: '进入仿真', primary: true}, 'saved: enter');
same(t.tsimSkillAction({environment: {staged: true, stale: false}}).label, '进入仿真', 'own stage: enter');
same(t.tsimSkillAction({}).label, '构建环境', 'nothing built: build');
same(t.tsimSkillAction({environment: {...saved, stale: true}}).label, '重新构建环境', 'stale: build again');
same(t.tsimSkillAction({build: {busy: true}}), {label: '查看进度', primary: false}, 'building: the progress');

// ---- issue #74, per skill since #75: the list of runs waits for something to run in
same(t.tsimBuilt(null), false, 'not read yet');
same(t.tsimBuilt(counts(2)), false, 'a task whose skills have no environment');
same(t.tsimBuilt(counts(2, 1)), true, 'a task with one environment');
same(t.tsimBuilt({...counts(2), model: {staged: true}}), true, 'a robot with its own stage needs no build');
same(t.tsimBuilt(counts(2, 1), {environment: null, build: {busy: false, status: 'failed'}}), false, 'a skill whose first build failed');
same(t.tsimBuilt(counts(2, 1), {environment: null, build: {busy: true}}), false, 'a skill whose first build is under way');
same(t.tsimBuilt(counts(2, 1), {environment: saved, build: {busy: true}}), true, 'a rebuild keeps the saved one');
same(t.tsimBuilt(counts(2, 1), {environment: {...saved, stale: true}}), false, 'one for another brand and model cannot be entered');

// ---- a step's values in one line
same(t.tsimValue('货架'), '货架', 'text as it is');
same(t.tsimValue([[1, 0], [0, 0]]), '[[1,0],[0,0]]', 'a list as JSON');
ok(t.tsimValue('x'.repeat(200)).length === 58 && t.tsimValue('x'.repeat(200)).endsWith('…'), 'a long value is cut');
same(t.tsimUseLine({application: '巡检', step: 2, parameters: {place: '门口', loops: 1}}), '应用「巡检」第 2 步：place = 门口，loops = 1', 'a use with values');
same(t.tsimUseLine({application: '巡检', step: 1, parameters: {}}), '应用「巡检」第 1 步', 'a use without values');

// ---- why the skill cannot be run
const skill = {id: 'patrol', title: '巡逻', installed: true, compatible: true, has_package: true, origin_name: '任务的应用用到',
               uses: [{application: 'A', step: 1, parameters: {loops: 2}}]};
const on = {robot: {id: 'r1', connected: true}, here: true, staging: false, latched: false, active: false};
same(t.tsimRunBlock(skill, on), '', 'can run');
same(t.tsimRunBlock(skill, {robot: null}), '进入仿真后可以运行', 'not entered');
same(t.tsimRunBlock(skill, {robot: {connected: false}}), '仿真机器人没有在运行', 'robot closed');
same(t.tsimRunBlock(skill, {...on, here: false}), '仿真机器人在另一个技能的环境里；先进入这个技能的仿真', 'in another skill\'s environment');
same(t.tsimRunBlock(skill, {...on, staging: true}), '正在换到这个技能的场地', 'the duck is moving to this stage');
same(t.tsimRunBlock({...skill, installed: false}, on), '还没有安装到仿真机器人上', 'not installed, package here');
same(t.tsimRunBlock({...skill, installed: false, has_package: false}, on), '这台电脑上没有它的技能包', 'not installed, no package');
same(t.tsimRunBlock({...skill, compatible: false}, on), '这台仿真机器人做不了这个技能', 'not compatible');
same(t.tsimRunBlock(skill, {...on, latched: true}), '急停已锁存，先解除急停', 'latched');
same(t.tsimRunBlock(skill, {...on, active: true}), '有技能正在运行', 'busy');

// ---- the skill's panel on its page
const panel = t.tsimSkillPanel(skill, on);
ok(panel.includes('data-tsim-run="patrol"') && !panel.includes('disabled') && panel.includes('已安装'), 'an installed skill has a run button');
ok(panel.includes('应用「A」第 1 步：loops = 2'), 'the panel says where the task uses the skill');
const two = t.tsimSkillPanel({...skill, uses: [...skill.uses, {application: 'A', step: 3, parameters: {loops: 5}}]}, on);
ok(two.includes('按第 1 步运行') && two.includes('按第 3 步运行') && two.includes('data-use="1"'), 'a skill used twice runs with either step');
const missing = t.tsimSkillPanel({...skill, installed: false}, on);
ok(missing.includes('data-tsim-install="patrol"') && missing.includes('disabled') && missing.includes('未安装'), 'a skill not installed offers the install');
const nowhere = t.tsimSkillPanel({...skill, installed: false, has_package: false}, on);
ok(nowhere.includes('data-action="skillpkg-open"') && !nowhere.includes('data-tsim-install'), 'without a package here the folder install is offered');
const outside = t.tsimSkillPanel(skill, {robot: null});
ok(outside.includes('disabled') && !outside.includes('data-tsim-install') && outside.includes('进入仿真后可以运行'), 'before entering nothing can be run or installed');
const elsewhere = t.tsimSkillPanel({...skill, installed: false}, {...on, here: false});
ok(elsewhere.includes('disabled') && !elsewhere.includes('data-tsim-install'), 'nothing is installed while the robot is in another skill\'s environment');
const extra = t.tsimSkillPanel({...skill, uses: [], origin_name: '这个品牌型号的技能'}, on);
ok(extra.includes('任务的应用没有用到它') && extra.includes('这个品牌型号的技能') && extra.includes('>运行<'), 'a skill the task does not use can still be run');
const hostile = t.tsimSkillPanel({...skill, id: '"><img src=x>', title: '<b>x</b>', summary: '<script>', uses: [{application: '<i>', step: 1, parameters: {a: '<u>'}}]}, on);
ok(!/<img|<b>x|<script>|<i>|<u>/.test(hostile), 'names and values are never markup');

console.log(`task_sim_logic: ${checks} checks passed`);
