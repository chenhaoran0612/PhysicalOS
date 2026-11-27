// Pure-logic checks of web/duck.js (issue #63): what the skill model block says and draws.
//   node tests/duck_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const d = vm.runInContext(readFileSync('web/duck.js', 'utf8') +
  '\n;({duckKit, duckScore, duckOrigin, duckRobots, duckReason, duckRunLine, duckSpark, duckFigure, duckAgo, duckTrainingSource, duckShowRow, duckTrainingBlock, duckStage, duckHead, duckSkillLine})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(JSON.stringify(actual), JSON.stringify(expected), what); checks += 1; };

const SHOWS = [
  {id: 'bow_roll', title: '鞠躬鞠过头', summary: '鞠躬后前滚。', status: 'delivered', status_name: '已交付', packaged: true,
    origin: {round: 40, update: 4, steps: 2048, device: 'Mac CPU'}, acceptance: {fixed: [10, 10], independent: [10, 10]}, note: ''},
  {id: 'drunk_circle', title: '醉鸭不倒', summary: '绕场两圈。', status: 'candidate', status_name: '候选', packaged: true,
    origin: {round: 19, update: 108, steps: 69120, device: 'Mac CPU'}, acceptance: {fixed: [10, 10], independent: [8, 10]}, note: '独立组 8/10。'},
  {id: 'recharge', title: '没电了', summary: '趴下再起来。', status: 'training', status_name: '训练中', packaged: false, note: '还没有打包的模型。'},
];

// ---- the kit as the page reads it
same(d.duckKit(undefined).shows, [], 'no state: no shows');
same(d.duckKit({shows: 'x', available: 1}).available, false, 'only true is available');
same(d.duckScore([8, 10]), '8/10', 'a score');
same(d.duckScore(null), '—', 'no score');
same(d.duckOrigin(SHOWS[1].origin), '第 19 轮第 108 次更新 · 69,120 步 · Mac CPU', 'where a model comes from');
same(d.duckRobots([{id: 'a', sim: {kind: 'mujoco', adapter: 'brainos.duck_body:body'}}, {id: 'b', sim: {kind: 'mujoco', adapter: 'brainos.mujoco_body:body'}}, {id: 'c'}, null]).map(r => r.id), ['a'], 'only simulated ducks');

// ---- what the stage says
const line = (status, asked) => d.duckRunLine(status, asked, SHOWS);
same(line(null), {text: '还没有连接到这个仿真小鸭', tone: ''}, 'nothing read yet');
same(line({running: true, state: 'idle', run_id: ''}), {text: '站在起点，可以开始', tone: ''}, 'standing');
same(line({running: false, state: 'idle', run_id: ''}).text, '这个仿真小鸭没有在运行', 'not running');
same(line({running: true, state: 'succeeded', run_id: 'old', show: 'bow_roll'}, {run_id: 'new', show: 'drunk_circle'}),
  {text: '正在准备「醉鸭不倒」…', tone: 'busy'}, 'asked, and the body still shows the run before');
same(line({running: true, state: 'running', run_id: 'r', show: 'bow_roll', phase: '贴地前滚一圈'}, {run_id: 'r', show: 'bow_roll'}),
  {text: '「鞠躬鞠过头」 · 贴地前滚一圈', tone: 'busy'}, 'performing names the phase');
same(line({running: true, state: 'succeeded', run_id: 'r', show: 'bow_roll', result: {success: true, duration_s: 4.54}}),
  {text: '「鞠躬鞠过头」完整演完，用时 4.54 秒', tone: 'good'}, 'done');
same(line({running: true, state: 'failed', run_id: 'r', show: 'bow_roll', result: {success: false, reason: 'timeout', duration_s: 8.7}}),
  {text: '「鞠躬鞠过头」没有成功：超时，没有按要求收尾，用时 8.70 秒', tone: 'bad'}, 'a failure says why, in words');
same(line({running: true, state: 'failed', run_id: 'r', show: 'bow_roll', result: {reason: 'some_new_reason'}}).text,
  '「鞠躬鞠过头」没有成功：some_new_reason', 'a reason without words is shown as it is');
same(line({running: true, state: 'stopped', run_id: 'r', show: 'bow_roll', result: {reason: 'stopped'}}), {text: '「鞠躬鞠过头」已停止', tone: ''}, 'stopped is not a failure');
same(line({running: true, state: 'error', run_id: 'r', show: 'bow_roll', error: '物理引擎进程退出了'}), {text: '物理引擎进程退出了', tone: 'bad'}, 'an engine problem');

// ---- rows of the list
const row = d.duckShowRow(SHOWS[0], true, false);
ok(row.includes('data-duck-run="bow_roll"') && !/data-duck-run="bow_roll" disabled/.test(row), 'a packaged model can be run');
ok(row.includes('<b>10/10</b> 固定组') && row.includes('<b>10/10</b> 独立组') && row.includes('已交付'), 'its two scores and status');
ok(/data-duck-run="bow_roll" disabled/.test(d.duckShowRow(SHOWS[0], true, true)), 'not while a show is performed');
ok(/data-duck-run="bow_roll" disabled/.test(d.duckShowRow(SHOWS[0], false, false)), 'not without a running duck');
ok(d.duckShowRow(SHOWS[0], false, false).includes('data-duck-replay="bow_roll"'), 'the replay needs no duck');
// ---- issue #67: a model is a skill of the robot
const SKILLED = {...SHOWS[0], skill: {id: 'duck.bow_roll', version: '40.4.0', title: '鞠躬鞠过头'}};
same(d.duckSkillLine(SHOWS[2]), '', 'a model without a package is no skill yet');
ok(d.duckSkillLine(SKILLED).includes('<code>duck.bow_roll</code><span>v40.4.0</span><span>添加仿真小鸭时自动装上</span>'), 'which skill it is, before a duck is there');
ok(d.duckSkillLine(SKILLED, {installed: true, error: ''}).includes('data-tone="good"') && d.duckSkillLine(SKILLED, {installed: true}).includes('已装在这个仿真小鸭上'), 'installed on the chosen duck');
const missing = d.duckSkillLine(SKILLED, {installed: false, error: '没有装上：<b>'});
ok(missing.includes('data-tone="bad"') && missing.includes('没有装到这个仿真小鸭上：没有装上：&lt;b&gt;'), 'not installed says why, escaped');
ok(d.duckShowRow(SKILLED, true, false, {installed: true}).includes('已装在这个仿真小鸭上'), 'the row carries the skill line');
const half = d.duckSkillLine(SKILLED, {installed: true, error: '已经装上，但没有登记到技能列表：x'});
ok(half.includes('data-tone="bad"') && half.includes('已经装上，但没有登记到技能列表：x') && !half.includes('没有装到'), 'installed but not registered says exactly that');
ok(!d.duckShowRow(SHOWS[0], true, false).includes('duck-skill'), 'a state from before has no skill line');
ok(d.duckHead({shows: SHOWS, engine: {mujoco: '3.10.0'}}).includes('都是这个品牌型号的技能'), 'the head says what the models are');
const training = d.duckShowRow(SHOWS[2], true, false);
ok(!training.includes('data-duck-run') && !training.includes('data-duck-replay') && training.includes('还没有打包的模型'), 'a show in training has no buttons');
ok(d.duckShowRow(SHOWS[1], true, false).includes('<b>8/10</b> 独立组') && d.duckShowRow(SHOWS[1], true, false).includes('duck-candidate'), 'a candidate says so');
ok(d.duckShowRow({...SHOWS[0], title: '<img onerror=1>'}, true, false).includes('&lt;img onerror=1&gt;'), 'text is escaped');
ok(d.duckShowRow({...SHOWS[0], packaged: false}, true, false).includes('这个安装里没有它的模型包'), 'a delivered model without its package here says so');

// ---- the stage with and without a duck
ok(d.duckStage({available: true, problem: ''}, []).includes('data-duck-add'), 'no duck: offer to add one');
const cannot = d.duckStage({available: false, problem: '这个安装包里没有它的机器人模型和技能模型'}, []);
ok(!cannot.includes('data-duck-add') && cannot.includes('没有它的机器人模型'), 'no kit: say why, no button');
ok(!d.duckStage({available: true}, [{id: 'a', name: '小鸭'}]).includes('duck-pick'), 'one duck: nothing to choose');
ok(d.duckStage({available: true}, [{id: 'a', name: '小鸭'}, {id: 'b', name: '小鸭 2'}]).includes('<option value="b"'), 'two ducks: a choice');
ok(d.duckHead({vendor: 'Microduck', model: 'AD-P102', engine: {mujoco: '3.10.0'}, shows: SHOWS}).includes('2 个模型包 · 在 MuJoCo 3.10.0 上验收和运行'), 'the head counts the packages');

// ---- training
same(d.duckSpark([[1, 4, 0, 10, 1], [1, 8, 5, 10, 2], [2, 4, 10, 10, 3]], 160, 36), '2.0,34.0 80.0,18.0 158.0,2.0', 'passes over the evaluations');
same(d.duckSpark([[1, 4, 7, 10, 1]], 160, 36), '2.0,11.6', 'one evaluation is one point');
same(d.duckSpark([[1, 4, null, 10], 'x', [1, 4, 3, 0]], 160, 36), '', 'nothing to draw');
same(d.duckFigure({round: 6, update: 4, successes: 10, episodes: 10, device: 'GPU'}), {score: '10/10', where: '第 6 轮第 4 次 · GPU'}, 'a figure');
same(d.duckFigure(null), {score: '—', where: ''}, 'no figure');
same([d.duckAgo(1000, 1030), d.duckAgo(1000, 1000 + 600), d.duckAgo(1000, 1000 + 7200), d.duckAgo(1000, 1000 + 3 * 86400), d.duckAgo(null, 5)],
  ['刚刚', '10 分钟前', '2 小时前', '3 天前', ''], 'how long ago');
ok(d.duckTrainingSource({source: 'live', last_update: 1000}, 1600).startsWith('来自本机的训练看板，训练文件最后变更于10 分钟前'), 'live');
ok(d.duckTrainingSource({source: 'snapshot', generated_at: 1000}, 1000 + 86400).includes('安装包里的记录') &&
  d.duckTrainingSource({source: 'snapshot'}, 5).includes('训练看板没有应答'), 'the copy in the kit says it is one');
same(d.duckTrainingSource({source: 'none', checking: true}, 5), '正在询问本机的训练看板…', 'asking');
ok(d.duckTrainingSource({source: 'none'}, 5).includes('没有训练记录'), 'nothing');
const block = d.duckTrainingBlock({source: 'live', dashboard: 'http://127.0.0.1:8767', rounds: 73, last_update: 10, tasks: [
  {id: 'water', title: '单脚探水', active: true, best: {round: 6, update: 4, successes: 10, episodes: 10, device: 'GPU'},
    latest: {round: 9, update: 8, successes: 7, episodes: 10, device: 'CPU'}, evaluations: 83, series: [[6, 4, 10, 10, 1], [9, 8, 7, 10, 2]]},
  {id: 'drunk', title: '旧版', active: false, best: null, latest: null, evaluations: 0, series: []}]}, 20);
ok(block.includes('href="http://127.0.0.1:8767/"') && block.includes('rel="noopener noreferrer"'), 'the full dashboard opens in its own window');
ok(block.includes('共 73 轮') && block.includes('>10/10<') && block.includes('>7/10<') && block.includes('83 次') && block.includes('<polyline'), 'best, latest, count and trend');
ok(block.includes('drunk · 历史版本') && block.includes('<td>—</td>'), 'an old version is marked and has no trend');
ok(!d.duckTrainingBlock({source: 'none', tasks: []}, 5).includes('<table'), 'no table without tasks');
const hostile = d.duckTrainingBlock({source: 'live', tasks: [{id: 't', title: 't', active: true, evaluations: '<b>9</b>',
  best: {round: 1, update: 1, successes: 1, episodes: '<img src=x>'}, latest: null, series: []}]}, 5);
ok(!hostile.includes('<img') && !hostile.includes('<b>9'), 'figures from the dashboard are never markup');

console.log(`duck_logic: ${checks} checks passed`);
