// Pure-logic checks of web/model_hub_history.js (issue #76): the training step of a round offers 「从历史中选择」
// next to the upload, the dialog lists the model's other versions (a result on the round's set comes with
// one) and, for a duck's skill model, the training project's evaluated model files, newest first, twenty at
// a time; the start and the current candidate cannot be chosen, and what anyone typed is never markup.
//   node tests/model_hub_history_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const empty = (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b ? `<div>${b}</div>` : ''}</div>`;
const table = (headers, data) => `<div class="table-wrap"><table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table></div>`;
const sandbox = {h, button, badge, empty, table, icon: k => `<svg data-icon="${k}"></svg>`, date: v => 'D:' + v};
vm.createContext(sandbox);
const hub = vm.runInContext(['web/page_titles.js', 'web/model_hub.js', 'web/model_hub_history.js'].map(f => readFileSync(f, 'utf8')).join('\n') +
  '\n;({hubImprovePage, hubHistoryBody, hubHistoryTraining, hubHistoryWhen, HUB_HISTORY_PAGE})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const measure = (id, name, unit, better, target, primary = false) => ({id, name, unit, better, target, primary, min: 0, max: 100});
const MOTION = {id: 'motion_rl', name: '动作类强化模型', en: 'MOTION POLICY', summary: '整段动作。', measures: [measure('success_rate', '成功率', '%', 'higher', 100, true)]};
const NAV = {id: 'navigation', name: '导航模型', en: 'NAVIGATION', summary: '定位和规划。', measures: [measure('reach_rate', '到达成功率', '%', 'higher', 95, true)]};
const version = (id, label, more = {}) => ({id, model_id: 'm', label, files: [{id: 'file-' + id, filename: label + '.onnx', size: 2048, sha256: 'ab'.repeat(32)}],
  origin: {}, note: '', round_id: '', builtin: false, runnable: false, created_at: '2026-11-27T01', ...more});
const round = (status, more = {}) => ({id: 'r1', model_id: 'nav', number: 2, status, version_id: 'v1', set_id: 'nav50', goal: '到 95%', weaknesses: '弱', plan: '练',
  training_set_ids: [], base_evaluation_id: '', candidate_version_id: '', candidate_evaluation_id: '', note: '', created_at: '', closed_at: '', comparison: null, ...more});
const nav = {id: 'nav', name: '走廊导航', category: 'navigation', product: null, builtin: false, show: '', note: '', status: 'unrated', status_name: '待评测',
  current_version_id: 'v1', versions: [version('v3', 'v3<b>'), version('v2', 'v2'), version('v1', 'v1')], evaluations: [], rounds: [], skill: null, summary: '', created_at: ''};
const DATA = {categories: [MOTION, NAV], models: [nav], sets: [{id: 'nav50', purpose: 'evaluation', name: '走廊 50 点', category: 'navigation', seeds: null, targets: {}, count: 50}],
  duck: {available: true}, limits: {file_bytes: 1}};

// ---- the steps of a round
const training = hub.hubImprovePage({}, DATA, {...nav, rounds: [round('training')]}, null);
ok(training.includes('data-action="hub-version-add" data-id="nav|r1"') && training.includes('data-action="hub-history" data-id="nav|r1"'), 'the training step offers the upload and the history');
ok(training.indexOf('上传新模型文件') < training.indexOf('从历史中选择') && training.includes('或者从历史中选一个'), 'the upload first, the history next to it, both in the sentence');
const candidate = hub.hubImprovePage({}, DATA, {...nav, rounds: [round('candidate', {candidate_version_id: 'v2'})]}, null);
ok(candidate.includes('候选版本是「v2」') && candidate.includes('data-action="hub-history" data-id="nav|r1"') && !candidate.includes('已经传上来'),
   'the candidate step does not say it was uploaded, and swaps through the history');

// ---- the model's versions
const choices = {versions: [
  {...version('v3', 'v3<b>'), candidate: false, round_number: null, evaluation: null},
  {...version('v2', 'v2', {origin: {round: 6, update: 8}}), candidate: true, round_number: 2, evaluation: null},
  {...version('dv', '第 3 轮', {builtin: true, created_at: '', files: [{filename: 'nod.zip', size: null, sha256: 'cd'.repeat(32)}]}), candidate: false, round_number: 1,
   evaluation: {id: 'e1', metrics: {reach_rate: 91}, passed: null, total: null, judgement: {met: false}}}]};
const body = hub.hubHistoryBody(DATA, nav, round('candidate', {candidate_version_id: 'v2'}), choices, null, {onlyFull: false, shown: 20});
same(count(body, 'data-action="hub-history-pick"'), 3, 'one row per version');
ok(body.includes('data-id="r1|v|v3"') && body.includes('data-id="r1|v|dv"'), 'a row names the round and the version');
ok(body.includes('v3&lt;b&gt;') && !body.includes('v3<b>'), 'a label is text, not markup');
ok(/data-id="r1\|v\|v2" disabled>/.test(body) && body.includes('现在的候选'), 'the candidate it has cannot be chosen again');
ok(body.includes('第 2 轮的候选') && body.includes('随应用带来') && body.includes('第 6 轮 · 第 8 次更新'), 'where a version came from');
ok(body.includes('到达成功率 91%') && body.includes('选它，用已有的评测') && body.includes('title="在「走廊 50 点」上"'), 'a result on the round\'s set comes with the version');
ok(!body.includes('训练项目里的模型文件'), 'a model of one\'s own has no training project part');
ok(body.includes('data-action="hub-version-add" data-id="nav|r1"') && body.includes('data-action="close"'), 'the dialog can still upload, or be closed');
ok(hub.hubHistoryBody(DATA, nav, round('training'), null, null, {onlyFull: false, shown: 20}).includes('正在读取…'), 'while the versions are read');
ok(hub.hubHistoryBody(DATA, nav, round('training'), {versions: []}, null, {onlyFull: false, shown: 20}).includes('这个模型还没有别的版本。'), 'no other version');

// ---- the training project's model files
const file = (n, more = {}) => ({key: `${n}-1`, round: n, update: 1, passed: n % 3 ? 10 : 7, total: 10, steps: 2048, device: 'GPU', at: 1795760000 + n, sha256: 'ef'.repeat(32),
  version_id: '', start: false, candidate: false, ...more});
const files = Array.from({length: 45}, (_, i) => file(100 - i));
const answer = {available: true, problem: '', dashboard: 'http://127.0.0.1:8767', files};
const view = (onlyFull = false, shown = hub.HUB_HISTORY_PAGE) => ({onlyFull, shown});
const part = (training, v = view()) => hub.hubHistoryTraining(round('training'), training, v);
ok(part({loading: true}).includes('正在读取训练看板'), 'while the dashboard is read');
ok(part({available: false, problem: '训练看板（http://127.0.0.1:8767）没有<回答>', files: []}).includes('没有&lt;回答&gt;'), 'why the files are not there, as text');
const first = part(answer);
same([count(first, 'data-action="hub-history-pick"'), first.includes('再显示 20 个'), first.includes('45 个评估过的模型文件，从新到旧')], [20, true, true], 'twenty at first, more on request');
ok(first.indexOf('第 100 轮') < first.indexOf('第 99 轮') && first.includes('data-id="r1|t|100-1"'), 'newest first; a row names the file by round and update');
ok(first.includes('2048 步 · GPU') && first.includes('10 / 10 通过') && first.includes('mhh-score warn">7 / 10 通过'), 'how it was trained and how it did');
same(count(part(answer, view(false, 40)), 'data-action="hub-history-pick"'), 40, 'twenty more');
ok(part(answer, view(false, 40)).includes('再显示 5 个') && !part(answer, view(false, 60)).includes('hub-history-more'), 'the last few, then nothing more to show');
const full = part(answer, view(true));
ok(!full.includes('7 / 10') && full.includes('checked') && full.includes('30 个评估过的模型文件'), 'only the ones that passed every run');
same(part({...answer, files: [file(6)]}, view(true)).includes('没有全部通过的模型文件。'), true, 'none passed every run');
const marked = part({...answer, files: [file(3, {start: true}), file(2, {version_id: 'hubv-x'}), file(1, {version_id: 'hubv-y', candidate: true})]});
ok(/data-id="r1\|t\|3-1" disabled>/.test(marked) && marked.includes('这一轮的起点'), 'the start cannot be its own candidate');
ok(marked.includes('已是一个版本，选为候选') && /data-id="r1\|t\|1-1" disabled>/.test(marked), 'a file that is a version already; the current candidate');
same([hub.hubHistoryWhen(undefined), /^\d\d-\d\d \d\d:\d\d$/.test(hub.hubHistoryWhen(1795760000))], ['', true], 'when it was evaluated');
ok(hub.hubHistoryBody(DATA, {...nav, show: 'bow'}, round('training'), {versions: []}, {loading: true}, view()).includes('训练项目里的模型文件'), 'a duck\'s model shows both parts');

console.log(`model_hub_history_logic: ${checks} checks passed`);
