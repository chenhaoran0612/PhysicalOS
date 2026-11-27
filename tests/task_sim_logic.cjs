// Pure-logic checks of web/task_sim.js (issue #66): where a task's simulation stands and what its skills offer.
//   node tests/task_sim_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const t = vm.runInContext(readFileSync('web/task_sim.js', 'utf8') +
  '\n;({tsimStatus, tsimCardAction, tsimValue, tsimUseLine, tsimRunBlock, tsimSkillRows, tsimEsc})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(JSON.stringify(actual), JSON.stringify(expected), what); checks += 1; };

// ---- where a task's simulation stands
const saved = {source_name: '空场地', stale: false};
same(t.tsimStatus({}).key, 'none', 'nothing built');
same(t.tsimStatus({problem: {code: 'task_sim_no_product'}}).key, 'blocked', 'cannot be simulated');
same(t.tsimStatus({model: {staged: true}}).key, 'ready', 'a robot with its own stage needs no build');
same(t.tsimStatus({build: {busy: true, status: 'generating'}}).key, 'building', 'a build under way');
same(t.tsimStatus({build: {busy: true}, environment: saved}).key, 'building', 'a rebuild shows as building');
same(t.tsimStatus({environment: saved}), {key: 'saved', text: '已保存 · 空场地', tone: 'good'}, 'saved');
same(t.tsimStatus({environment: saved, build: {busy: false, status: 'failed'}}).key, 'saved', 'a failed rebuild leaves the saved one in use');
same(t.tsimStatus({build: {busy: false, status: 'failed'}}).key, 'failed', 'a failed first build');
same(t.tsimStatus({environment: {...saved, stale: true}}).key, 'stale', 'built for another brand and model');
same(t.tsimStatus({environment: saved, problem: {code: 'sim_engine_missing'}}).key, 'saved', 'a saved environment is shown although something is missing now');

// ---- the button of a card
same(t.tsimCardAction({environment: saved}), {label: '进入仿真', primary: true}, 'saved: enter');
same(t.tsimCardAction({model: {staged: true}}).label, '进入仿真', 'own stage: enter');
same(t.tsimCardAction({}).label, '构建仿真环境', 'nothing built: build');
same(t.tsimCardAction({build: {busy: false, status: 'failed'}}).label, '构建仿真环境', 'failed: build again');
same(t.tsimCardAction({problem: {}}), {label: '查看原因', primary: false}, 'blocked: the reason');
same(t.tsimCardAction({build: {busy: true}}).label, '查看进度', 'building: the progress');

// ---- a step's values in one line
same(t.tsimValue('货架'), '货架', 'text as it is');
same(t.tsimValue([[1, 0], [0, 0]]), '[[1,0],[0,0]]', 'a list as JSON');
ok(t.tsimValue('x'.repeat(200)).length === 58 && t.tsimValue('x'.repeat(200)).endsWith('…'), 'a long value is cut');
same(t.tsimUseLine({application: '巡检', step: 2, parameters: {place: '门口', loops: 1}}), '应用「巡检」第 2 步：place = 门口，loops = 1', 'a use with values');
same(t.tsimUseLine({application: '巡检', step: 1, parameters: {}}), '应用「巡检」第 1 步', 'a use without values');

// ---- why a skill cannot be run
const skill = {id: 'patrol', title: '巡逻', installed: true, compatible: true, has_package: true, uses: [{application: 'A', step: 1, parameters: {loops: 2}}]};
const on = {robot: {id: 'r1', connected: true}, latched: false, active: false};
same(t.tsimRunBlock(skill, on), '', 'can run');
same(t.tsimRunBlock(skill, {robot: null}), '进入仿真后可以运行', 'not entered');
same(t.tsimRunBlock(skill, {robot: {connected: false}}), '仿真机器人没有在运行', 'robot closed');
same(t.tsimRunBlock({...skill, installed: false}, on), '还没有安装到仿真机器人上', 'not installed, package here');
same(t.tsimRunBlock({...skill, installed: false, has_package: false}, on), '这台电脑上没有它的技能包', 'not installed, no package');
same(t.tsimRunBlock({...skill, compatible: false}, on), '这台仿真机器人做不了这个技能', 'not compatible');
same(t.tsimRunBlock(skill, {...on, latched: true}), '急停已锁存，先解除急停', 'latched');
same(t.tsimRunBlock(skill, {...on, active: true}), '有技能正在运行', 'busy');

// ---- the rows
const rows = t.tsimSkillRows({skills: [skill]}, on);
ok(rows.includes('data-tsim-run="patrol"') && !rows.includes('disabled') && rows.includes('已安装'), 'an installed skill has a run button');
ok(rows.includes('应用「A」第 1 步：loops = 2'), 'the row says where the task uses the skill');
const two = t.tsimSkillRows({skills: [{...skill, uses: [...skill.uses, {application: 'A', step: 3, parameters: {loops: 5}}]}]}, on);
ok(two.includes('按第 1 步运行') && two.includes('按第 3 步运行') && two.includes('data-use="1"'), 'a skill used twice runs with either step');
const missing = t.tsimSkillRows({skills: [{...skill, installed: false}]}, on);
ok(missing.includes('data-tsim-install="patrol"') && missing.includes('disabled') && missing.includes('未安装'), 'a skill not installed offers the install');
const nowhere = t.tsimSkillRows({skills: [{...skill, installed: false, has_package: false}]}, on);
ok(nowhere.includes('data-action="skillpkg-open"') && !nowhere.includes('data-tsim-install'), 'without a package here the folder install is offered');
const outside = t.tsimSkillRows({skills: [skill]}, {robot: null});
ok(outside.includes('disabled') && !outside.includes('data-tsim-install') && outside.includes('进入仿真后可以运行'), 'before entering nothing can be run or installed');
const extra = t.tsimSkillRows({skills: [{...skill, uses: []}]}, on);
ok(extra.includes('任务的应用没有用到它') && extra.includes('>运行<'), 'a skill the task does not use can still be run');
const hostile = t.tsimSkillRows({skills: [{...skill, id: '"><img src=x>', title: '<b>x</b>', summary: '<script>', uses: [{application: '<i>', step: 1, parameters: {a: '<u>'}}]}]}, on);
ok(!/<img|<b>x|<script>|<i>|<u>/.test(hostile), 'names and values are never markup');
same(t.tsimSkillRows({}, on), '', 'no skills, no rows');

console.log(`task_sim_logic: ${checks} checks passed`);
