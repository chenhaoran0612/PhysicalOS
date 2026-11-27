// Pure-logic checks of web/model_hub.js (issue #71): the three pages are a board over a wall of cards, a
// model leads with the main measure of its category, a round of improvement shows the step it is at, and
// what a person typed is never markup.
//   node tests/model_hub_logic.cjs
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
const hub = vm.runInContext(readFileSync('web/page_titles.js', 'utf8') + '\n' + readFileSync('web/model_hub.js', 'utf8') +
  '\n;({hubValue, hubTarget, hubTargets, hubHeadline, hubLegacyCategory, hubLibraryFacts, hubDucks, hubRunnable, hubRunning, hubCard, hubLibraryPage,' +
  ' hubModelPage, hubRuns, hubLive, hubEvalDetail, hubTrainCard, hubTrainingPage, hubImprovePage, hubDataPage, hubSetDetail, HUB_STEPS})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;

const measure = (id, name, unit, better, target, primary = false) => ({id, name, unit, better, target, primary, min: 0, max: 100});
const MOTION = {id: 'motion_rl', name: '动作类强化模型', en: 'MOTION POLICY', summary: '整段动作。', embodied: true,
                measures: [measure('success_rate', '成功率', '%', 'higher', 100, true), measure('mean_duration_s', '平均用时', '秒', 'lower', null)]};
const NAV = {id: 'navigation', name: '导航模型', en: 'NAVIGATION', summary: '定位和规划。', embodied: true,
             measures: [measure('reach_rate', '到达成功率', '%', 'higher', 95, true), measure('collision_rate', '碰撞率', '%', 'lower', 0),
                        measure('position_error_m', '到点误差', '米', 'lower', 0.2)]};
const LLM = {id: 'llm', name: '大语言模型（LLM）', en: 'LLM', summary: '理解任务。', embodied: false, measures: [measure('pass_rate', '任务通过率', '%', 'higher', 90, true)]};
const VLM = {id: 'vlm', name: '视觉语言模型（VLM）', en: 'VLM', summary: '看图回答。', embodied: false, measures: [measure('vqa_accuracy', '问答准确率', '%', 'higher', 85, true)]};
const OTHER = {id: 'other', name: '其他模型', en: 'OTHER', summary: '其他。', embodied: false, measures: [measure('score', '综合得分', '分', 'higher', null, true)]};
const DUCK = {vendor: 'Microduck', model: 'AD-P102'};
const judged = (metrics, met, rows) => ({rows: rows || Object.entries(metrics).map(([id, value]) => ({id, value, target: null, met: null})), met});
const evaluation = (id, version, set, metrics, met, more = {}) => ({id, model_id: 'm', version_id: version, set_id: set, mode: 'imported', status: 'done', metrics,
  passed: null, total: null, source: '评测报告', round_id: '', error: '', created_at: '2026-11-27T02', finished_at: '2026-11-27T02', done: 0, judgement: judged(metrics, met), ...more});
const version = (id, label, more = {}) => ({id, model_id: 'm', label, files: [{id: 'file-' + id, filename: label + '.onnx', size: 2048, sha256: 'ab'.repeat(32)}],
  origin: {}, note: '', round_id: '', builtin: false, runnable: false, created_at: '2026-11-27T01', ...more});
const model = (id, more = {}) => ({id, name: '模型 ' + id, category: 'navigation', summary: '', created_at: '2026-11-27T01', product: null, builtin: false, show: '', note: '',
  status: 'unrated', status_name: '待评测', current_version_id: '', versions: [], evaluations: [], rounds: [], skill: null, ...more});
const round = (number, status, more = {}) => ({id: 'r' + number, model_id: 'm', number, status, version_id: 'v1', set_id: 'nav50', goal: '到达成功率到 95%', weaknesses: '', plan: '',
  training_set_ids: [], base_evaluation_id: '', candidate_version_id: '', candidate_evaluation_id: '', note: '', created_at: '2026-11-27T03', closed_at: '',
  comparison: null, base_met: null, candidate_met: null, ...more});
const set = (id, more = {}) => ({id, purpose: 'evaluation', name: '集 ' + id, category: 'navigation', product: null, description: '说明', count: 50, source: '', file: null,
  created_at: '2026-11-27T01', seeds: null, targets: {}, builtin: false, runnable: false, used: 0, ...more});

const duck = model('hubm-duck-bow', {name: '鞠躬鞠过头', category: 'motion_rl', product: DUCK, builtin: true, show: 'bow', status: 'delivered', status_name: '已交付',
  current_version_id: 'dv', versions: [version('dv', '第 40 轮 · 第 4 次更新', {builtin: true, runnable: true, files: [{id: '', filename: 'bow.zip', size: null, sha256: 'cd'.repeat(32)}],
                                               origin: {round: 40, update: 4, steps: 2048, device: 'Mac CPU'}})],
  evaluations: [evaluation('e-run', 'dv', 'fixed', {success_rate: 80, mean_duration_s: 4.5}, false, {mode: 'run', passed: 8, total: 10, source: '在这台电脑的仿真里运行'}),
                evaluation('e-fixed', 'dv', 'fixed', {success_rate: 100}, true, {passed: 10, total: 10})],
  skill: {id: 's1', name: '鞠躬', version: '0.40.0', auto: true}});
const nav = model('nav', {name: '走廊<导航>', product: {vendor: 'NeuxMind', model: 'CS1'}, current_version_id: 'v1', versions: [version('v2', 'v2', {round_id: 'r1'}), version('v1', 'v1')]});
const bare = model('bare', {name: '空模型', category: 'llm', status: 'empty', status_name: '还没有模型文件'});
const DATA = {categories: [MOTION, NAV, LLM, VLM, OTHER], status_names: {}, models: [nav, bare, duck], duck: {available: true, problem: '', profile_id: 'microduck-adp102'},
  limits: {file_bytes: 4 * 1024 ** 3},
  sets: [set('nav50', {name: '走廊 50 点', targets: {reach_rate: 90}, used: 2}), set('tracks', {purpose: 'training', name: '走廊轨迹', count: 1200, file: {id: 'f1', filename: 'tracks.jsonl', size: 9}}),
         set('fixed', {name: '固定起姿组', category: 'motion_rl', product: DUCK, seeds: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], count: 10, builtin: true, runnable: true, used: 3})]};
const STATE = {projects: [{id: 'p1', name: '仓库巡检'}], robots: [], skills: [], datasets: [{id: 'd1', project_id: 'p1', name: '仓库样本', version: '1', format: 'custom', source: '现场采集'}],
  models: [{id: 'm1', project_id: 'p1', name: 'Qwen-VL', task_type: '视觉语言理解', model_ref: 'qwen-vl-max', catalog_model_id: 'c1'},
           {id: 'm2', project_id: 'gone', name: '自有模型', task_type: '<b>排序</b>', model_ref: 'x'}]};
const actions = record => `<button data-old="${h(record.id)}"></button>`;

// ---- values, targets, the number a card leads with
same([hub.hubValue(NAV.measures[0], 96), hub.hubValue(NAV.measures[2], 0.153), hub.hubValue(OTHER.measures[0], 7), hub.hubValue(NAV.measures[0], null)],
     ['96%', '0.15 米', '7 分', '—'], 'a value is written with its unit');
same([hub.hubTarget(NAV.measures[0], 95), hub.hubTarget(NAV.measures[1], 0), hub.hubTarget(MOTION.measures[1], null)], ['≥ 95%', '≤ 0%', ''], 'a target is a rule');
same(hub.hubTargets(NAV, DATA.sets[0]).map(t => [t.measure.id, t.target]), [['reach_rate', 90], ['collision_rate', 0], ['position_error_m', 0.2]], "a set's own target replaces the category's");
const head = hub.hubHeadline(DATA, duck);
same([head.measure.id, head.value, head.met, head.set.name], ['success_rate', 80, false, '固定起姿组'], 'the newest finished evaluation of the current version leads');
same(hub.hubHeadline(DATA, nav), null, 'no evaluation, no number');
same(['视觉语言理解', '导航', '意图识别', '大模型对话', '目标检测', '语音唤醒', '步态强化学习', '排序'].map(t => hub.hubLegacyCategory({task_type: t})),
     ['vlm', 'navigation', 'intent', 'llm', 'perception', 'speech', 'motion_rl', 'other'], "a task's model is shown in the category its purpose names");
same(hub.hubDucks({robots: [{id: 'a', sim: {adapter: 'brainos.duck_body:body', state: 'running'}}, {id: 'b', sim: {adapter: 'brainos.duck_body:body', state: 'stopped'}},
                            {id: 'c', sim: {adapter: 'other', state: 'running'}}, {id: 'd'}]}).map(r => r.id), ['a'], 'only a running simulated duck can evaluate');
ok(hub.hubRunnable(duck.versions[0], DATA.sets[2]) && !hub.hubRunnable(nav.versions[0], DATA.sets[0]) && !hub.hubRunnable(duck.versions[0], DATA.sets[0]), 'a packaged version on start poses is run here');
same(hub.hubRunning({models: [nav, {...duck, evaluations: [{id: 'x', status: 'running'}]}]}).id, 'x', 'the running evaluation is found');
same(hub.hubRunning(DATA), null, 'none is running');

// ---- 模型库
const facts = hub.hubLibraryFacts(STATE, DATA);
same([facts.total, facts.good, facts.skills, facts.improving, facts.categories.map(x => x.count)], [5, 1, 1, 0, [1, 1, 1, 1, 1]], 'the board counts models and the tasks\' own');
const library = hub.hubLibraryPage(STATE, DATA, '', actions);
ok(library.startsWith('<div class="page-head mh-head"><div><div class="eyebrow">MODEL LIBRARY</div><h1>模型库</h1></div><div class="button-row">'), 'the head is the two title lines and the buttons');
ok(!/<div class="page-head[^>]*>[\s\S]*?<p/.test(library.slice(0, library.indexOf('mh-board'))), 'no sentence under the title');
same([count(library, 'class="mh-card"'), count(library, 'class="mh-card legacy"'), count(library, '<section class="mh-group"')], [3, 2, 5], 'every model is a card of its category');
ok(library.includes('走廊&lt;导航&gt;') && !library.includes('走廊<导航>'), 'a name is text');
ok(library.includes('&lt;b&gt;排序&lt;/b&gt;') && !library.includes('<b>排序</b>'), "a task's model's purpose is text");
ok(library.includes('任务「仓库巡检」') && library.includes('任务已经不在了') && count(library, 'data-old=') === 2, "a task's model keeps its task and its buttons");
ok(library.includes('<strong>80%</strong>') && library.includes('未达标'), 'a card leads with the main measure and says whether it meets the target');
const only = hub.hubLibraryPage(STATE, DATA, 'llm', actions);
same([count(only, '<section class="mh-group"'), count(only, 'class="mh-card"'), only.includes('aria-pressed="true"'), only.includes('查看全部分类')], [1, 1, true, true], 'a category filters the wall');
ok(hub.hubLibraryPage(STATE, {...DATA, models: []}, 'navigation', actions).includes('这个分类还没有模型'), 'an empty category says so and offers to add');

// ---- the page of one model
const page = hub.hubModelPage(STATE, DATA, duck);
ok(page.includes('<div class="eyebrow">MOTION POLICY</div><h1>鞠躬鞠过头</h1>'), 'a model is titled with its category and name');
ok(page.includes('随应用带来') && !page.includes('/api/model-hub/file?id=&') && !page.includes('data-action="hub-model-remove"'), 'a package of the app is not downloaded or deleted');
ok(page.includes('data-action="hub-eval-run"') && page.includes('第 40 轮 · 第 4 次更新 · 2048 步 · Mac CPU'), 'a packaged version can be evaluated here; its origin is told');
ok(page.includes('这个技能就是用这个模型做的') && page.includes('鞠躬'), 'the skill made from the model is named');
ok(page.includes('仿真里运行') && page.includes('8 / 10') && page.includes('data-hub-count="e-run"'), 'an evaluation run here says so, with its count');
const own = hub.hubModelPage(STATE, DATA, nav);
ok(own.includes('href="/api/model-hub/file?id=file-v1" download') && own.includes('data-action="hub-model-remove"') && !own.includes('data-action="hub-eval-run"'), "a model of one's own: download, delete, no run");
ok(own.includes('第 1 轮的候选') === false && own.includes('当前版本'), 'the current version is marked');
ok(hub.hubModelPage(STATE, DATA, bare).includes('还没有模型文件。训练完把模型文件传到这里。'), 'a model without a file asks for one');

// ---- an evaluation as its runs
const runs = hub.hubRuns({status: 'running', total: 4, cases: [{seed: 5, success: true}, {seed: 6, success: false, reason: 'fall'}]}, [5, 6, 7, 8]);
same([count(runs, 'mh-run pass'), count(runs, 'mh-run fail'), count(runs, 'mh-run now'), count(runs, 'mh-run wait')], [1, 1, 1, 1], 'one mark per start pose');
ok(runs.includes('起姿 6：没有通过（fall）') && runs.includes('起姿 7：正在运行'), 'a mark says which pose and how it went');
const live = hub.hubLive({id: 'e1', status: 'running', total: 4, cases: [{seed: 5, success: true}]}, [5, 6, 7, 8]);
ok(live.includes('正在评测：1 / 4 个起姿，1 个通过') && live.includes('data-action="hub-eval-stop"') && live.includes('data-hub-live="e1"'), 'a running evaluation can be stopped');
const ended = hub.hubLive({id: 'e1', status: 'interrupted', error: '有人按了<停止>', total: 4, cases: []}, [5, 6, 7, 8]);
ok(ended.includes('被打断：有人按了&lt;停止&gt;') && !ended.includes('hub-eval-stop'), 'an evaluation that ended says why, as text');
const detail = hub.hubEvalDetail(DATA, duck, {...duck.evaluations[0], cases: [{seed: 1, success: false, reason: '<x>', steps: 200, duration_s: 4.5}],
  judgement: judged({success_rate: 80}, false, [{id: 'success_rate', value: 80, target: 100, met: false}, {id: 'mean_duration_s', value: null, target: null, met: null}])});
ok(detail.includes('≥ 100%') && detail.includes('&lt;x&gt;') && detail.includes('仿真里的结果不是现场证据'), 'the detail has the target, the reason as text, and what a simulation is not');

// ---- 训练与改进
const base = evaluation('eb', 'v1', 'nav50', {reach_rate: 80, collision_rate: 4}, false, {round_id: 'r1',
  judgement: judged({}, false, [{id: 'reach_rate', value: 80, target: 90, met: false}, {id: 'collision_rate', value: 4, target: 0, met: false}, {id: 'position_error_m', value: null, target: 0.2, met: null}])});
const candidate = evaluation('ec', 'v2', 'nav50', {reach_rate: 96, collision_rate: 0}, true, {round_id: 'r1'});
const comparison = {primary: 'better', rows: [{id: 'reach_rate', before: 80, after: 96, change: 16, direction: 'better'}, {id: 'collision_rate', before: 4, after: 0, change: -4, direction: 'better'},
                                             {id: 'position_error_m', before: null, after: null, change: null, direction: 'unknown'}]};
const at = (status, more = {}, evaluations = []) => ({...nav, evaluations, rounds: [round(1, status, more)]});
const improve = (m, liveDetail) => hub.hubImprovePage(STATE, DATA, m, liveDetail);
const baseline = improve(at('baseline'));
ok(baseline.includes('<li class="current" aria-current="step"><span aria-hidden="true">1</span>基线评测</li>') && baseline.includes('data-action="hub-eval-import"') && !baseline.includes('hub-eval-run'), 'a round begins with the baseline; a model of one\'s own imports it');
ok(baseline.includes('data-id="nav|r1|v1"') && baseline.includes('data-action="hub-round-drop"') && !baseline.includes('hub-round-add'), 'the buttons name the round and its version; no second round is offered');
const duckRound = hub.hubImprovePage(STATE, DATA, {...duck, rounds: [round(1, 'baseline', {version_id: 'dv', set_id: 'fixed'})]}, null);
ok(duckRound.includes('data-action="hub-eval-run"') && duckRound.includes('data-id="hubm-duck-bow|r1|dv"'), 'a packaged skill model is evaluated in the simulation');
const going = hub.hubImprovePage(STATE, DATA, {...duck, evaluations: [{id: 'er', version_id: 'dv', set_id: 'fixed', round_id: 'r1', status: 'running', total: 10, done: 0}],
  rounds: [round(1, 'baseline', {version_id: 'dv', set_id: 'fixed'})]}, {id: 'er', status: 'running', total: 10, cases: [{seed: 1, success: true}]});
ok(going.includes('正在评测：1 / 10 个起姿') && !going.includes('data-action="hub-eval-run"'), 'while it runs the page shows the runs, not the buttons');
const diagnose = improve(at('diagnose', {base_evaluation_id: 'eb'}, [base]));
ok(diagnose.includes('data-action="hub-diagnose"') && diagnose.includes('≥ 90%') && count(diagnose, '未达标') === 2, 'the baseline is judged by the targets of the set');
const training = improve(at('training', {base_evaluation_id: 'eb', weaknesses: '转角<漂移>', plan: '加数据', training_set_ids: ['tracks']}, [base]));
ok(training.includes('转角&lt;漂移&gt;') && training.includes('走廊轨迹') && training.includes('data-action="hub-version-add"') && training.includes('data-id="nav|r1"'), 'training: what was written, and where the new file goes');
const review = improve(at('review', {base_evaluation_id: 'eb', candidate_version_id: 'v2', candidate_evaluation_id: 'ec', comparison, candidate_met: true}, [candidate, base]));
ok(review.includes('主指标变好，并且达标。') && review.includes('+16% · 变好') && review.includes('-4% · 变好'), 'the review compares the candidate with the start');
ok(review.includes('data-action="hub-round-accept"') && review.includes('data-action="hub-round-reject"') && count(review, '<tr class="mh-') === 2, 'the decision is offered; a measure neither has is left out');
const closed = improve(at('accepted', {base_evaluation_id: 'eb', candidate_version_id: 'v2', candidate_evaluation_id: 'ec', comparison, closed_at: '2026-11-27T05', note: '达标'}, [candidate, base]));
ok(closed.includes('开始第 2 轮') && closed.includes('到达成功率 80% → 96%') && closed.includes('已采纳') && !closed.includes('mh-round"'), 'a closed round is history and the next one can begin');
ok(improve(bare).includes('先把训练好的模型文件传上来') && !improve(bare).includes('hub-round-add'), 'no round without a model file');
const card = hub.hubTrainCard(DATA, at('candidate', {base_evaluation_id: 'eb', candidate_version_id: 'v2'}, [base]));
same([count(card, '<i class="done">'), count(card, '<i class="next">'), card.includes('第 1 轮 · 等候选评测'), card.includes('继续这一轮'), card.includes('<strong>80%</strong>')], [3, 1, true, true, true], 'a card shows the step the round is at');
const trainingPage = hub.hubTrainingPage(STATE, {...DATA, models: [bare, at('review', {comparison}), at('rejected')]}, '<table id="jobs"></table>', true);
ok(trainingPage.startsWith('<div class="page-head mh-head"><div><div class="eyebrow">TRAINING &amp; IMPROVEMENT</div><h1>训练与改进</h1></div>'), 'the head of 训练与改进');
ok(trainingPage.indexOf('继续这一轮') < trainingPage.indexOf('空模型') && trainingPage.includes('data-duck-slot') && trainingPage.includes('<table id="jobs">'), 'models in a round first; the stage and the old jobs are kept');
same(count(trainingPage, '<li><span aria-hidden="true">'), hub.HUB_STEPS.length, 'the board names the five steps of a round');
ok(!hub.hubTrainingPage(STATE, DATA, '', false).includes('data-duck-slot'), 'no stage without the kit');

// ---- 训练与测试数据
const data = hub.hubDataPage(STATE, DATA);
ok(data.startsWith('<div class="page-head mh-head"><div><div class="eyebrow">TRAINING &amp; TEST DATA</div><h1>训练与测试数据</h1></div>'), 'the head of 训练与测试数据');
ok(data.indexOf('id="hub-evaluation"') < data.indexOf('id="hub-training"') && count(data, 'mh-card mh-set"') === 3 && count(data, 'mh-card mh-set legacy"') === 1, 'two libraries; a task\'s data version is in the training library');
ok(data.includes('到达成功率 ≥ 90%') && data.includes('<strong>10</strong><span>个起姿') && data.includes('tracks.jsonl') && data.includes('任务「仓库巡检」'), 'a set shows its targets, its size and its file');
const posed = hub.hubSetDetail(DATA, DATA.sets[2]);
ok(count(posed, '<span>') >= 10 && !posed.includes('hub-set-remove'), 'the start poses are listed; a set of the app is not removed');
ok(hub.hubSetDetail(DATA, {...DATA.sets[1], used: 0}).includes('hub-set-remove') && !hub.hubSetDetail(DATA, DATA.sets[0]).includes('hub-set-remove'), 'only an unused set of one\'s own can be removed');

console.log(`model hub logic: ${checks} checks passed`);
