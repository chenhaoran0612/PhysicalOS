'use strict';
// Issue #71 — 模型库. Three pages of the sidebar are drawn here, each as a board (the numbers) over a wall
// of cards of one size, the way 工作台 is (web/task_workbench.js):
//
//   * 「模型库」 (#models): every model of the account by category. A category binds the measures a model
//     of it is judged by. A model opens as a page of its own (#model): its versions (the model files),
//     its evaluations, the skill it has become.
//   * 「训练与改进」 (#training): a model is chosen and improved round by round (#model-improve): baseline,
//     what is weak, training (outside the app), the candidate's evaluation, the decision.
//   * 「训练与测试数据」 (#datasets): the evaluation library and the training library.
//
// What these pages had before is kept in second place: the models a task chose from the platform or
// connected itself are cards of their category, the training jobs are a table under the models, the
// data versions a task registered are cards of the training library.
//
// This file is pure (data in, facts or markup out): tests/model_hub_logic.cjs. `data` is the answer of
// model-hub/overview (brainos/model_hub.py), `st` the state the page already has. web/model_hub_pages.js
// fetches, remembers what is open and acts.

const HUB_OPEN = ['baseline', 'diagnose', 'training', 'candidate', 'review'];
const HUB_STEPS = [['baseline', '基线评测'], ['diagnose', '找短板'], ['training', '训练'], ['candidate', '评测候选'], ['review', '决定']];
const HUB_ROUND_NAMES = {baseline: '等基线评测', diagnose: '等写下短板和做法', training: '训练中', candidate: '等候选评测', review: '等决定',
                         accepted: '已采纳', rejected: '未采纳'};
const HUB_EVAL_NAMES = {running: '正在评测', done: '已完成', stopped: '已停止', interrupted: '被打断', error: '出错'};
const HUB_STATUS_CLASS = {delivered: 'good', qualified: 'good', candidate: 'warn', below: 'warn', training: 'blue'};
const HUB_GOOD = ['delivered', 'qualified'];
const HUB_PURPOSES = {evaluation: '评测库', training: '训练库'};
const HUB_DUCK_ADAPTER = 'brainos.duck_body:body';       // ADAPTER of brainos/duck_kit.py: how the state marks a simulated duck
// What a task's own model record says it is for → the category its card is shown in (first match).
const HUB_LEGACY = [[/vlm|视觉语言|多模态|图文/i, 'vlm'], [/vla|端到端|操作策略|manipulat/i, 'vla'], [/llm|语言|对话|文本|规划|chat/i, 'llm'],
                    [/导航|navigation|slam|定位|建图/i, 'navigation'], [/意图|intent/i, 'intent'],
                    [/语音|asr|tts|speech|唤醒/i, 'speech'], [/强化|步态|运动控制|motion|locomotion/i, 'motion_rl'],
                    [/检测|分割|感知|识别|detect|segment|vision|视觉/i, 'perception']];

// ---------------------------------------------------------------- facts

const hubCat = (data, id) => data.categories.find(c => c.id === id) || data.categories[data.categories.length - 1];
const hubPrimary = cat => cat.measures.find(m => m.primary);
const hubProduct = p => (p ? `${p.vendor} ${p.model}` : '');
const hubModel = (data, id) => data.models.find(m => m.id === id) || null;
const hubVersion = (model, id) => model.versions.find(v => v.id === id) || null;
const hubSet = (data, id) => data.sets.find(s => s.id === id) || null;
const hubOpenRound = model => model.rounds.find(r => HUB_OPEN.includes(r.status)) || null;
const hubEval = (model, id) => model.evaluations.find(e => e.id === id) || null;

function hubNumber(value) {
  return String(Math.round(value * 100) / 100);
}

// A measured value with its unit: 96%, 4.2 秒, 18.3.
function hubValue(measure, value) {
  if (value === null || value === undefined) return '—';
  const n = hubNumber(value);
  return measure.unit === '%' ? n + '%' : measure.unit ? `${n} ${measure.unit}` : n;
}

// The target of a measure as a rule: ≥ 95%, ≤ 0.2 米.
function hubTarget(measure, target) {
  return target === null || target === undefined ? '' : `${measure.better === 'higher' ? '≥' : '≤'} ${hubValue(measure, target)}`;
}

// The targets an evaluation set judges by: the category's, replaced by the set's own.
function hubTargets(cat, set) {
  const own = (set && set.targets) || {};
  return cat.measures.map(m => ({measure: m, target: Object.hasOwn(own, m.id) ? own[m.id] : m.target})).filter(x => x.target !== null && x.target !== undefined);
}

// The number a model's card leads with: the main measure of the newest finished evaluation of its current version.
function hubHeadline(data, model) {
  const measure = hubPrimary(hubCat(data, model.category));
  const found = model.evaluations.find(e => e.status === 'done' && e.version_id === model.current_version_id);
  if (!found) return null;
  return {measure, value: found.metrics[measure.id], met: found.judgement ? found.judgement.met : null, set: hubSet(data, found.set_id), evaluation: found};
}

function hubLegacyCategory(record) {
  const hit = HUB_LEGACY.find(([pattern]) => pattern.test(`${record.task_type || ''} ${record.name || ''}`));
  return hit ? hit[1] : 'other';
}

// The model records of the tasks (chosen from the platform, connected, or made by a training job), each with its category.
function hubLegacy(st) {
  return (st.models || []).map(record => ({record, category: hubLegacyCategory(record),
                                           task: ((st.projects || []).find(p => p.id === record.project_id) || {}).name || ''}));
}

function hubLibraryFacts(st, data) {
  const legacy = hubLegacy(st);
  const per = id => data.models.filter(m => m.category === id).length + legacy.filter(x => x.category === id).length;
  return {legacy, total: data.models.length + legacy.length, good: data.models.filter(m => HUB_GOOD.includes(m.status)).length,
          skills: data.models.filter(m => m.skill).length, improving: data.models.filter(hubOpenRound).length,
          categories: data.categories.map(c => ({category: c, count: per(c.id)}))};
}

// The simulated ducks that are running: where a packaged skill model can be evaluated.
function hubDucks(st) {
  return (st.robots || []).filter(r => r.sim && r.sim.adapter === HUB_DUCK_ADAPTER && r.sim.state === 'running');
}

// The evaluation that is running now, if one is (a simulated duck runs one at a time; the page follows the first).
function hubRunningAll(data) {
  return data.models.flatMap(model => model.evaluations.filter(e => e.status === 'running'));
}

function hubRunning(data) {
  return hubRunningAll(data)[0] || null;
}

// Whether a version of a model can be run on a set here (a packaged skill model of the duck on a set of start poses).
const hubRunnable = (version, set) => Boolean(version && version.runnable && set && set.runnable);

// ---------------------------------------------------------------- small pieces of markup

const hubChip = cat => `<span class="mh-chip" data-cat="${h(cat.id)}"><i aria-hidden="true"></i>${h(cat.name)}</span>`;
const hubHead = (route, buttons) => `<div class="page-head mh-head"><div>${pageTitleMarkup(route)}</div><div class="button-row">${buttons}</div></div>`;
const hubBack = (action, text) => `<button type="button" class="text-link mh-back" data-action="${action}">← ${text}</button>`;
const hubHold = (name, value, none = false) => `<div class="mh-hold${none ? ' none' : ''}"><dt>${name}</dt><dd>${value}</dd></div>`;
const hubSize = bytes => (bytes === null || bytes === undefined ? '' : bytes >= 1024 ** 3 ? hubNumber(bytes / 1024 ** 3) + ' GB'
  : bytes >= 1024 ** 2 ? hubNumber(bytes / 1024 ** 2) + ' MB' : bytes >= 1024 ? hubNumber(bytes / 1024) + ' KB' : bytes + ' 字节');
const hubDay = value => (value ? date(value) : '');

function hubBoard(label, count, unit, index, figures) {
  return `<section class="mh-board" aria-label="${h(label)}概况"><div class="mh-count"><span class="mh-count-label">${h(label)}</span>` +
    `<strong>${h(count)}</strong><small>${h(unit)}</small></div><div class="mh-index">${index}</div>` +
    `<dl class="mh-figures">${figures.map(([name, value]) => `<div><dt>${h(name)}</dt><dd>${h(value)}</dd></div>`).join('')}</dl></section>`;
}

function hubMetric(head, measure) {
  if (!head) return `<div class="mh-metric none"><strong>—</strong><span>${h(measure.name)} · 还没有评测结果</span></div>`;
  const met = head.met === true ? '<em class="mh-met good">达标</em>' : head.met === false ? '<em class="mh-met bad">未达标</em>' : '';
  return `<div class="mh-metric${head.met === false ? ' below' : ''}"><strong>${h(hubValue(head.measure, head.value))}</strong>` +
    `<span>${h(head.measure.name)}${head.set ? ' · ' + h(head.set.name) : ''}</span>${met}</div>`;
}

function hubRoundLine(model) {
  const open = hubOpenRound(model);
  if (open) return `第 ${open.number} 轮 · ${HUB_ROUND_NAMES[open.status]}`;
  return model.rounds.length ? `做过 ${model.rounds.length} 轮 · 上一轮${HUB_ROUND_NAMES[model.rounds[0].status]}` : '';
}

// ---------------------------------------------------------------- 模型库: the board and the wall

function hubCard(data, model) {
  const cat = hubCat(data, model.category), current = hubVersion(model, model.current_version_id), round = hubRoundLine(model);
  return `<article class="mh-card" role="listitem" data-model="${h(model.id)}" data-cat="${h(cat.id)}">` +
    `<header class="mh-card-top">${hubChip(cat)}${badge(model.status_name, HUB_STATUS_CLASS[model.status] || '')}</header>` +
    `<h3>${h(model.name)}</h3><p class="mh-product">${model.product ? h(hubProduct(model.product)) : '不限品牌型号'}</p>` +
    hubMetric(hubHeadline(data, model), hubPrimary(cat)) + '<dl class="mh-holds">' +
    hubHold('当前版本', current ? h(current.label) : '还没有模型文件', !current) +
    hubHold('技能', model.skill ? h(model.skill.name) : '还不是技能', !model.skill) +
    hubHold('改进', round ? h(round) : '还没有改进过', !round) + '</dl>' +
    `<footer class="mh-card-foot">${button('查看', 'hub-open', 'small', model.id)}${button('训练与改进', 'hub-improve', 'secondary small', model.id)}</footer></article>`;
}

// A model record of a task. `actions` are the buttons the old page had for it.
function hubLegacyCard(data, item, actions) {
  const cat = hubCat(data, item.category), m = item.record;
  return `<article class="mh-card legacy" role="listitem" data-legacy="${h(m.id)}" data-cat="${h(cat.id)}">` +
    `<header class="mh-card-top">${hubChip(cat)}${badge('任务选用')}</header><h3>${h(m.name)}</h3>` +
    `<p class="mh-product">${item.task ? '任务「' + h(item.task) + '」' : '任务已经不在了'}</p>` +
    `<div class="mh-metric none"><strong>—</strong><span>${h(m.task_type || '没有写用途')}</span></div><dl class="mh-holds">` +
    hubHold('模型引用', h(m.model_ref || ''), !m.model_ref) + hubHold('来自', m.status === 'artifact' ? '训练任务的产物' : m.catalog_model_id ? '平台模型目录' : '自己接入') +
    hubHold('改进', '由任务管理', true) + `</dl><footer class="mh-card-foot">${actions}</footer></article>`;
}

// `filter` is a category id or ''. `legacyActions(record)` gives the buttons of a task's model record.
function hubLibraryPage(st, data, filter, legacyActions) {
  const facts = hubLibraryFacts(st, data);
  const index = '<h2>分类</h2><ul class="mh-cats">' + facts.categories.map(({category, count}) =>
    `<li><button type="button" class="mh-cat${filter === category.id ? ' active' : ''}" data-action="hub-filter" data-id="${h(category.id)}" aria-pressed="${filter === category.id}">` +
    `<i class="mh-swatch" data-cat="${h(category.id)}" aria-hidden="true"></i><span>${h(category.name)}</span><em>${count}</em></button></li>`).join('') + '</ul>';
  const board = hubBoard('模型', facts.total, `${facts.categories.filter(x => x.count).length} 个分类里有模型`, index,
    [['已交付或已达标', facts.good], ['已成为技能', facts.skills], ['正在改进', facts.improving]]);
  const shown = facts.categories.filter(x => (filter ? x.category.id === filter : x.count));
  const walls = shown.map(({category, count}) => {
    const cards = data.models.filter(m => m.category === category.id).map(m => hubCard(data, m)).join('') +
      facts.legacy.filter(x => x.category === category.id).map(x => hubLegacyCard(data, x, legacyActions(x.record))).join('');
    const measures = category.measures.map(m => `<span class="tag${m.primary ? ' mh-main' : ''}">${h(m.name)}${m.target === null ? '' : ' ' + h(hubTarget(m, m.target))}</span>`).join('');
    return `<section class="mh-group" data-cat="${h(category.id)}" aria-label="${h(category.name)}"><div class="section-head mh-group-head"><h2>${h(category.name)} ` +
      `<span class="badge-count">${count}</span></h2><span>${h(category.summary)}</span></div><p class="mh-measures" aria-label="这一类模型的指标">${measures}</p>` +
      (cards ? `<div class="mh-wall" role="list">${cards}</div>` : `<div class="mh-empty"><p>这个分类还没有模型。</p>${button(icon('plus') + ' 添加模型', 'hub-model-add', 'secondary small', category.id)}</div>`) + '</section>';
  }).join('');
  const rest = filter ? `<p class="mh-rest">${button('查看全部分类', 'hub-filter', 'secondary small', '')}</p>`
    : facts.categories.some(x => !x.count) ? `<p class="mh-rest">还没有模型的分类：${facts.categories.filter(x => !x.count).map(x => h(x.category.name)).join('、')}</p>` : '';
  return hubHead('models', button(icon('plus') + ' 添加模型', 'hub-model-add', '', filter)) + board + walls + rest;
}

// ---------------------------------------------------------------- the page of one model

function hubOrigin(origin) {
  const o = origin || {};
  return [o.round !== undefined ? `第 ${o.round} 轮` : '', o.update !== undefined ? `第 ${o.update} 次更新` : '',
          o.steps !== undefined ? `${o.steps} 步` : '', o.device || ''].filter(Boolean).join(' · ');
}

function hubVersionRow(model, version) {
  const round = model.rounds.find(r => r.candidate_version_id === version.id);
  const mark = version.id === model.current_version_id ? '<span class="mh-mark current">当前版本</span>'
    : round ? `<span class="mh-mark">第 ${round.number} 轮的候选${round.status === 'rejected' ? ' · 未采纳' : ''}</span>` : '';
  const files = version.files.map(f => `<li><span class="mh-file-name">${h(f.filename)}</span><span class="mh-file-meta">${[hubSize(f.size), 'SHA-256 ' + h(f.sha256.slice(0, 12))].filter(Boolean).join(' · ')}</span>` +
    (f.id ? `<a class="text-link" href="/api/model-hub/file?id=${encodeURIComponent(f.id)}" download>下载</a>` : '<span class="mh-file-meta">随应用带来</span>') + '</li>').join('');
  return `<li class="mh-version" data-version="${h(version.id)}"><div class="mh-version-head"><b>${h(version.label)}</b>${mark}<time>${h(hubDay(version.created_at))}</time></div>` +
    `${hubOrigin(version.origin) ? `<p class="mh-origin">${h(hubOrigin(version.origin))}</p>` : ''}${version.note ? `<p class="mh-note">${h(version.note)}</p>` : ''}<ul class="mh-files">${files}</ul></li>`;
}

function hubMeasureTags(cat, metrics) {
  return cat.measures.filter(m => metrics[m.id] !== undefined).map(m => `<span class="tag${m.primary ? ' mh-main' : ''}">${h(m.name)} ${h(hubValue(m, metrics[m.id]))}</span>`).join(' ');
}

function hubEvalRow(data, model, e) {
  const cat = hubCat(data, model.category), set = hubSet(data, e.set_id), version = hubVersion(model, e.version_id);
  const state = e.status === 'done' ? (e.judgement.met === true ? badge('达标', 'good') : e.judgement.met === false ? badge('未达标', 'warn') : badge('已完成'))
    : badge(HUB_EVAL_NAMES[e.status] || e.status, e.status === 'running' ? 'blue' : 'bad');
  const count = e.total ? `${e.status === 'done' ? e.passed : e.done} / ${e.total}` : '';
  return `<tr data-evaluation="${h(e.id)}"><td><span class="cell-main">${h(set ? set.name : '已经不在的评测集')}</span><span class="cell-sub">${h(version ? version.label : '')}</span></td>` +
    `<td>${e.status === 'done' ? hubMeasureTags(cat, e.metrics) : `<span class="cell-sub">${h(e.error || '')}</span>`}</td><td data-hub-count="${h(e.id)}">${h(count)}</td>` +
    `<td><span class="cell-main">${e.mode === 'run' ? '仿真里运行' : '导入'}</span><span class="cell-sub">${h(e.source)}</span></td><td>${state}</td>` +
    `<td>${button('详情', 'hub-eval-open', 'secondary small', e.id)}</td></tr>`;
}

function hubModelPage(st, data, model) {
  const cat = hubCat(data, model.category), versions = model.versions, open = hubOpenRound(model);
  const fact = (title, body) => `<section class="mh-fact"><h2>${title}</h2>${body}</section>`;
  const skill = model.skill
    ? `<p><b>${h(model.skill.name)}</b> v${h(model.skill.version)}</p><p class="mh-fact-note">${model.skill.auto ? '这个技能就是用这个模型做的。' : '绑定的技能。'}</p>`
    : '<p class="mh-fact-none">还不是技能</p><p class="mh-fact-note">模型训练好，绑定到同一品牌型号的一个技能，就能编排进机器人应用。</p>';
  const evaluations = model.evaluations.length
    ? table(['评测集 / 版本', '指标', '通过', '方式', '结果', '操作'], model.evaluations.map(e => hubEvalRow(data, model, e)))
    : empty('还没有评测', '评测是一个版本在一个评测集上的指标。', '');
  return hubBack('hub-back-models', '模型库') +
    `<div class="page-head mh-head mh-model-head"><div><div class="eyebrow">${h(cat.en)}</div><h1>${h(model.name)}</h1></div><div class="button-row">` +
    `${button('训练与改进', 'hub-improve', '', model.id)}${button('上传模型文件', 'hub-version-add', 'secondary', model.id)}` +
    `${model.builtin ? '' : button('删除', 'hub-model-remove', 'secondary', model.id)}</div></div>` +
    `<div class="mh-detail"><div class="mh-detail-main"><section class="mh-block" aria-label="版本"><div class="section-head"><h2>版本 <span class="badge-count">${versions.length}</span></h2><span>一个版本是一次训练交出的模型文件</span></div>` +
    (versions.length ? `<ol class="mh-versions">${versions.map(v => hubVersionRow(model, v)).join('')}</ol>`
      : `<div class="mh-empty"><p>还没有模型文件。训练完把模型文件传到这里。</p>${button('上传模型文件', 'hub-version-add', 'small', model.id)}</div>`) + '</section>' +
    `<section class="mh-block" aria-label="评测"><div class="section-head"><h2>评测 <span class="badge-count">${model.evaluations.length}</span></h2>` +
    `<div class="button-row">${versions.length ? button('导入评测结果', 'hub-eval-import', 'secondary small', model.id) : ''}` +
    `${versions.some(v => v.runnable) ? button('在仿真里评测', 'hub-eval-run', 'small', model.id) : ''}</div></div><div class="panel mh-evals">${evaluations}</div></section></div>` +
    '<aside class="mh-side" aria-label="模型的情况">' +
    fact('状态', `<p>${badge(model.status_name, HUB_STATUS_CLASS[model.status] || '')}</p>${open ? `<p class="mh-fact-note">第 ${open.number} 轮改进：${h(HUB_ROUND_NAMES[open.status])}</p>` : ''}`) +
    fact('分类', `<p>${hubChip(cat)}</p><p class="mh-fact-note">${h(cat.summary)}</p>`) +
    fact('品牌型号', model.product ? `<p><b>${h(hubProduct(model.product))}</b></p>` : '<p class="mh-fact-none">不限品牌型号</p>') +
    fact('技能', skill + `<p>${button(model.skill && !model.skill.auto ? '更换技能' : '绑定技能', 'hub-skill', 'secondary small', model.id)}</p>`) +
    fact('指标', `<ul class="mh-measure-list">${cat.measures.map(m => `<li><span>${h(m.name)}${m.primary ? '<em>主指标</em>' : ''}</span><b>${h(hubTarget(m, m.target) || '不设达标线')}</b></li>`).join('')}</ul>`) +
    (model.summary || model.note ? fact('说明', `<p>${h(model.summary || '')}</p>${model.note ? `<p class="mh-fact-note">${h(model.note)}</p>` : ''}`) : '') +
    '</aside></div>';
}

// ---------------------------------------------------------------- an evaluation, as its runs

// The runs of an evaluation of start poses: one mark per pose. `detail` has `cases`; `seeds` are the set's.
function hubRuns(detail, seeds) {
  const cases = detail.cases || [], total = detail.total || seeds.length;
  const marks = Array.from({length: total}, (_, i) => {
    const one = cases[i], seed = one ? one.seed : seeds[i];
    const kind = one ? (one.success ? 'pass' : 'fail') : detail.status === 'running' && i === cases.length ? 'now' : 'wait';
    const said = `起姿 ${seed === undefined ? i + 1 : seed}：${one ? (one.success ? '通过' : '没有通过' + (one.reason ? '（' + one.reason + '）' : '')) : kind === 'now' ? '正在运行' : '还没有运行'}`;
    return `<li class="mh-run ${kind}" title="${h(said)}"><i aria-hidden="true">${kind === 'pass' ? '✓' : kind === 'fail' ? '×' : ''}</i><span class="mh-sr">${h(said)}</span></li>`;
  }).join('');
  return `<ol class="mh-runs" aria-label="每个起姿的结果">${marks}</ol>`;
}

// The block of a running (or just ended) evaluation, updated in place while it runs.
function hubLiveText(detail) {
  const passed = (detail.cases || []).filter(c => c.success).length, done = (detail.cases || []).length;
  return detail.status === 'running' ? `正在评测：${done} / ${detail.total} 个起姿，${passed} 个通过`
    : detail.status === 'done' ? `评测完成：${detail.total} 个起姿，${passed} 个通过` : `${HUB_EVAL_NAMES[detail.status] || detail.status}：${detail.error || `跑了 ${done} / ${detail.total} 个起姿`}`;
}

function hubLive(detail, seeds) {
  return `<div class="mh-live ${h(detail.status)}" data-hub-live="${h(detail.id)}" data-hub-status="${h(detail.status)}"><p role="status">${h(hubLiveText(detail))}</p>${hubRuns(detail, seeds)}` +
    (detail.status === 'running' ? button('停止评测', 'hub-eval-stop', 'secondary small', detail.id) : '') + '</div>';
}

// The body of the dialog of one evaluation.
function hubEvalDetail(data, model, detail) {
  const cat = hubCat(data, model.category), set = hubSet(data, detail.set_id), version = hubVersion(model, detail.version_id);
  const rows = detail.status === 'done' ? detail.judgement.rows.filter(r => r.value !== null && r.value !== undefined).map(r => {
    const m = cat.measures.find(x => x.id === r.id);
    return `<tr><td>${h(m.name)}</td><td><b>${h(hubValue(m, r.value))}</b></td><td>${h(hubTarget(m, r.target) || '—')}</td>` +
      `<td>${r.met === true ? badge('达标', 'good') : r.met === false ? badge('未达标', 'warn') : ''}</td></tr>`;
  }) : [];
  const cases = (detail.cases || []).length ? table(['起姿', '结果', '原因', '步数', '用时'], detail.cases.map(c =>
    `<tr><td>${h(c.seed)}</td><td>${c.success ? badge('通过', 'good') : badge('没有通过', 'bad')}</td><td>${h(c.reason || '')}</td>` +
    `<td>${c.steps === null || c.steps === undefined ? '' : h(c.steps)}</td><td>${c.duration_s === null || c.duration_s === undefined ? '' : h(hubNumber(c.duration_s)) + ' 秒'}</td></tr>`)) : '';
  return `<div class="dialog-body mh-eval-detail"><dl class="mh-holds wide">${hubHold('版本', h(version ? version.label : ''))}${hubHold('评测集', h(set ? set.name : ''))}` +
    `${hubHold('方式', detail.mode === 'run' ? '在这台电脑的仿真里运行' : '导入')}${hubHold('出处', h(detail.source || ''))}` +
    `${detail.total ? hubHold('通过', `${h(detail.passed)} / ${h(detail.total)}`) : ''}</dl>` +
    (detail.mode === 'run' ? hubLive(detail, (set && set.seeds) || []) : '') +
    (rows.length ? table(['指标', '结果', '达标线', '是否达标'], rows) : '') + cases +
    (detail.mode === 'run' ? '<p class="mh-fact-note">仿真里的结果不是现场证据。</p>' : '') + '</div>';
}

// ---------------------------------------------------------------- 训练与改进: the board and the wall

function hubTrack(round) {
  const at = round ? HUB_STEPS.findIndex(([key]) => key === round.status) : -1, closed = round && !HUB_OPEN.includes(round.status);
  return '<div class="mh-track" aria-hidden="true">' + HUB_STEPS.map((_, i) =>
    `<i class="${closed ? (round.status === 'accepted' ? 'done' : 'off') : i < at ? 'done' : i === at ? 'next' : ''}"></i>`).join('') + '</div>';
}

// The main measure of a round: where it started, where the candidate got to.
function hubRoundMeasure(data, model, round) {
  const measure = hubPrimary(hubCat(data, model.category));
  const value = id => { const e = hubEval(model, id); return e && e.status === 'done' ? e.metrics[measure.id] : undefined; };
  return {measure, before: value(round.base_evaluation_id), after: value(round.candidate_evaluation_id)};
}

function hubRoundFigures(data, model, round) {
  const {measure, before, after} = hubRoundMeasure(data, model, round);
  if (before === undefined) return `<div class="mh-metric none"><strong>—</strong><span>${h(measure.name)} · 还没有基线</span></div>`;
  const direction = round.comparison ? round.comparison.primary : '';
  return `<div class="mh-metric mh-delta ${h(direction)}"><strong>${h(hubValue(measure, before))}${after === undefined ? '' : `<i aria-hidden="true">→</i>${h(hubValue(measure, after))}`}</strong>` +
    `<span>${h(measure.name)}${after === undefined ? ' · 基线' : ' · 起点 → 候选'}</span></div>`;
}

function hubTrainCard(data, model) {
  const cat = hubCat(data, model.category), round = hubOpenRound(model) || model.rounds[0] || null;
  const line = round ? `第 ${round.number} 轮 · ${HUB_ROUND_NAMES[round.status]}` : '还没有改进过';
  return `<article class="mh-card mh-train" role="listitem" data-model="${h(model.id)}" data-cat="${h(cat.id)}">` +
    `<header class="mh-card-top">${hubChip(cat)}${badge(model.status_name, HUB_STATUS_CLASS[model.status] || '')}</header>` +
    `<h3>${h(model.name)}</h3><p class="mh-product">${model.product ? h(hubProduct(model.product)) : '不限品牌型号'}</p>` +
    (round ? hubRoundFigures(data, model, round) : hubMetric(hubHeadline(data, model), hubPrimary(cat))) +
    `${hubTrack(round)}<p class="mh-track-text">${h(line)}</p><p class="mh-goal${round ? '' : ' none'}">${round ? h(round.goal) : '选一个版本和一个评测集，开始第一轮。'}</p>` +
    `<footer class="mh-card-foot">${button(round && HUB_OPEN.includes(round.status) ? '继续这一轮' : '进入', 'hub-improve', 'small', model.id)}</footer></article>`;
}

// `jobs` is the old page's table of training jobs (markup), `duck` whether the duck's stage is shown.
function hubTrainingPage(st, data, jobs, duck) {
  const rounds = data.models.flatMap(m => m.rounds), open = rounds.filter(r => HUB_OPEN.includes(r.status)).length;
  const rank = m => (hubOpenRound(m) ? 0 : m.rounds.length ? 1 : 2);
  const models = [...data.models].sort((a, b) => rank(a) - rank(b));
  const loop = '<h2>一轮改进</h2><ol class="mh-loop">' + HUB_STEPS.map(([, name], i) => `<li><span aria-hidden="true">${i + 1}</span>${name}</li>`).join('') + '</ol>';
  const board = hubBoard('改进轮次', rounds.length, `${data.models.filter(m => m.rounds.length).length} 个模型改进过`, loop,
    [['进行中', open], ['已采纳', rounds.filter(r => r.status === 'accepted').length], ['未采纳', rounds.filter(r => r.status === 'rejected').length]]);
  return hubHead('training', button('选择模型开始改进', 'hub-pick', '')) + board +
    `<div class="section-head mh-group-head"><h2>模型 <span class="badge-count">${models.length}</span></h2><span>选一个模型，一轮一轮地改进</span></div>` +
    (models.length ? `<div class="mh-wall" role="list">${models.map(m => hubTrainCard(data, m)).join('')}</div>`
      : `<div class="mh-empty"><p>模型库里还没有模型。</p>${button('去模型库添加', 'hub-back-models', 'secondary small')}</div>`) +
    (duck ? '<div class="section-head mh-group-head"><h2>仿真舞台</h2><span>Microduck AD-P102 的技能模型在仿真小鸭身上运行</span></div><div data-duck-slot></div>' : '') +
    `<div class="section-head mh-group-head"><h2>训练任务</h2><div class="button-row">${button('新建训练任务', 'plan-choose', 'secondary small', 'training')}</div></div><section class="panel mh-jobs">${jobs}</section>`;
}

// ---------------------------------------------------------------- the page where one model is improved

function hubResultTable(cat, evaluation, set) {
  const targets = hubTargets(cat, set);
  return table(['指标', '结果', '达标线', '是否达标'], cat.measures.filter(m => evaluation.metrics[m.id] !== undefined).map(m => {
    const row = evaluation.judgement.rows.find(r => r.id === m.id), target = (targets.find(t => t.measure.id === m.id) || {}).target;
    return `<tr><td>${h(m.name)}${m.primary ? ' <em class="mh-main-mark">主指标</em>' : ''}</td><td><b>${h(hubValue(m, evaluation.metrics[m.id]))}</b></td>` +
      `<td>${h(hubTarget(m, target) || '—')}</td><td>${row.met === true ? badge('达标', 'good') : row.met === false ? badge('未达标', 'warn') : ''}</td></tr>`;
  }));
}

function hubCompareTable(cat, round, set) {
  const targets = hubTargets(cat, set), words = {better: '变好', worse: '变差', same: '没有变', unknown: ''};
  return table(['指标', '起点', '候选', '变化', '达标线'], round.comparison.rows.filter(r => r.before !== null || r.after !== null).map(r => {
    const m = cat.measures.find(x => x.id === r.id), target = (targets.find(t => t.measure.id === m.id) || {}).target;
    const change = r.change === null ? '' : `${r.change > 0 ? '+' : ''}${hubValue(m, r.change)} · ${words[r.direction]}`;
    return `<tr class="mh-${h(r.direction)}"><td>${h(m.name)}${m.primary ? ' <em class="mh-main-mark">主指标</em>' : ''}</td><td>${h(hubValue(m, r.before))}</td>` +
      `<td><b>${h(hubValue(m, r.after))}</b></td><td class="mh-change">${h(change)}</td><td>${h(hubTarget(m, target) || '—')}</td></tr>`;
  }));
}

// The buttons that get an evaluation of `version` for a round, and the running one if there is one.
function hubEvalAsk(st, data, model, round, version, live) {
  const set = hubSet(data, round.set_id), running = model.evaluations.find(e => e.round_id === round.id && e.status === 'running');
  if (running) return hubLive(live && live.id === running.id ? live : {...running, cases: []}, (set && set.seeds) || []);
  const failed = model.evaluations.find(e => e.round_id === round.id && e.version_id === version.id && !['done', 'running'].includes(e.status));
  const key = `${model.id}|${round.id}|${version.id}`;
  const run = hubRunnable(version, set) ? button('在仿真里评测', 'hub-eval-run', '', key) : '';
  const training = model.show && version.origin && version.origin.round !== undefined && !version.builtin ? button('取训练项目的评估', 'hub-eval-training', run ? 'secondary' : '', key) : '';
  return (failed ? `<p class="mh-warn">上一次评测${h(HUB_EVAL_NAMES[failed.status] || '')}${failed.error ? '：' + h(failed.error) : ''}。</p>` : '') +
    `<div class="button-row">${run}${training}${button('导入评测结果', 'hub-eval-import', run || training ? 'secondary' : '', key)}</div>` +
    (run || !model.show || !version.files.some(f => f.id) ? '' : '<p class="mh-fact-note">上传的模型文件不在这台电脑上运行，它的评测结果从训练项目取，或者手动导入。</p>');
}

function hubRoundBody(st, data, model, round, live) {
  const cat = hubCat(data, model.category), set = hubSet(data, round.set_id), base = hubVersion(model, round.version_id);
  const candidate = hubVersion(model, round.candidate_version_id);
  const baseEval = hubEval(model, round.base_evaluation_id), candidateEval = hubEval(model, round.candidate_evaluation_id);
  const noted = round.weaknesses ? `<dl class="mh-holds wide">${hubHold('短板', h(round.weaknesses))}${hubHold('做法', h(round.plan))}` +
    `${round.training_set_ids.length ? hubHold('训练集', round.training_set_ids.map(id => h((hubSet(data, id) || {}).name || '')).join('、')) : ''}</dl>` : '';
  const gone = '<p class="mh-warn">这一轮用到的版本或评测记录已经不在了（随应用带来的模型包换过）。请放弃这一轮，重新开始。</p>';
  if (!base || !set || (round.status === 'review' && !round.comparison)) return gone;
  if (round.status === 'baseline') {
    return `<p class="mh-say">先知道起点：「${h(base ? base.label : '')}」在「${h(set ? set.name : '')}」上的指标。</p>${base ? hubEvalAsk(st, data, model, round, base, live) : ''}`;
  }
  if (round.status === 'diagnose') {
    return `<p class="mh-say">基线出来了。哪里弱，这一轮训练要改什么？</p>${baseEval ? hubResultTable(cat, baseEval, set) : ''}` +
      `<div class="button-row">${button('写下短板和做法', 'hub-diagnose', '', round.id)}${baseEval ? button('看这次评测', 'hub-eval-open', 'secondary', baseEval.id) : ''}</div>`;
  }
  if (round.status === 'training') {
    // Issue #76: the candidate does not have to be uploaded; 「从历史中选择」 is web/model_hub_history.js.
    return `<p class="mh-say">训练在应用外进行。训练完，上传新的模型文件，或者从历史中选一个，成为这一轮的候选版本。</p>${noted}` +
      `<div class="button-row">${button('上传新模型文件', 'hub-version-add', '', `${model.id}|${round.id}`)}${button('从历史中选择', 'hub-history', 'secondary', `${model.id}|${round.id}`)}</div>`;
  }
  if (round.status === 'candidate') {
    return `<p class="mh-say">候选版本是「${h(candidate ? candidate.label : '')}」。在同一个评测集上评测它。</p>${noted}${candidate ? hubEvalAsk(st, data, model, round, candidate, live) : ''}` +
      `<p class="mh-swap">${button('换一个候选版本', 'hub-history', 'secondary small', `${model.id}|${round.id}`)}</p>`;
  }
  const verdict = round.comparison.primary === 'better' ? (round.candidate_met ? '主指标变好，并且达标。' : round.candidate_met === false ? '主指标变好，但还没有全部达标。' : '主指标变好。')
    : round.comparison.primary === 'worse' ? '主指标比起点差。' : round.comparison.primary === 'same' ? '主指标没有变化。' : '两次评测没有共同的主指标，无法比较。';
  return `<p class="mh-say mh-verdict ${h(round.comparison.primary)}">${h(verdict)}</p>${hubCompareTable(cat, round, set)}${noted}` +
    (candidateEval && candidate ? `<details class="mh-again"><summary>重新评测，或换一个候选版本</summary>${hubEvalAsk(st, data, model, round, candidate, live)}` +
      `<p class="mh-swap">${button('换一个候选版本', 'hub-history', 'secondary small', `${model.id}|${round.id}`)}</p></details>` : '') +
    `<div class="button-row">${button('采纳为当前版本', 'hub-round-accept', '', round.id)}${button('不采纳', 'hub-round-reject', 'secondary', round.id)}</div>`;
}

function hubPastRound(data, model, round) {
  const {measure, before, after} = hubRoundMeasure(data, model, round);
  const moved = before === undefined ? '' : `${hubValue(measure, before)}${after === undefined ? '' : ' → ' + hubValue(measure, after)}`;
  return `<li class="mh-past ${h(round.status)}" data-round="${h(round.id)}"><div class="mh-past-head"><b>第 ${round.number} 轮</b>${badge(HUB_ROUND_NAMES[round.status], round.status === 'accepted' ? 'good' : '')}` +
    `<span class="mh-past-move">${moved ? h(measure.name) + ' ' + h(moved) : ''}</span><time>${h(hubDay(round.closed_at))}</time></div><p>${h(round.goal)}</p>` +
    `${round.weaknesses ? `<p class="mh-fact-note">短板：${h(round.weaknesses)}</p>` : ''}${round.note ? `<p class="mh-fact-note">结论：${h(round.note)}</p>` : ''}</li>`;
}

// `live` is the detail of the evaluation that is running on this page, when one was fetched.
function hubImprovePage(st, data, model, live) {
  const cat = hubCat(data, model.category), open = hubOpenRound(model), past = model.rounds.filter(r => !HUB_OPEN.includes(r.status));
  const next = Math.max(0, ...model.rounds.map(r => r.number)) + 1;
  const start = model.versions.length ? button(`开始第 ${next} 轮`, 'hub-round-add', '', model.id) : button('上传模型文件', 'hub-version-add', '', model.id);
  const at = open ? HUB_STEPS.findIndex(([key]) => key === open.status) : -1;
  const current = open ? `<section class="mh-round" data-round="${h(open.id)}" aria-label="第 ${open.number} 轮"><header class="mh-round-head"><div><span class="mh-round-n">第 ${open.number} 轮</span>` +
    `<h2>${h(open.goal)}</h2><p>从「${h((hubVersion(model, open.version_id) || {}).label || '')}」出发 · 评测集「${h((hubSet(data, open.set_id) || {}).name || '')}」</p></div>` +
    `<button type="button" class="mh-drop" data-action="hub-round-drop" data-id="${h(open.id)}">放弃这一轮</button></header>` +
    `<ol class="mh-steps">${HUB_STEPS.map(([, name], i) => `<li class="${i < at ? 'done' : i === at ? 'current' : ''}"${i === at ? ' aria-current="step"' : ''}><span aria-hidden="true">${i + 1}</span>${name}</li>`).join('')}</ol>` +
    `<div class="mh-round-body">${hubRoundBody(st, data, model, open, live)}</div></section>`
    : `<div class="mh-empty mh-start"><p>${model.versions.length ? (past.length ? '上一轮已经结束。' : '这个模型还没有改进过。') + '一轮改进：评测得到基线，找出短板，在应用外训练，评测候选版本，决定是否采纳。'
      : '先把训练好的模型文件传上来，才能开始改进。'}</p>${start}</div>`;
  return hubBack('hub-back-training', '训练与改进') +
    `<div class="page-head mh-head mh-model-head"><div><div class="eyebrow">${h(cat.en)}</div><h1>${h(model.name)}</h1></div><div class="button-row">` +
    `${open ? '' : start}${button('查看模型', 'hub-open', 'secondary', model.id)}</div></div>${current}` +
    (past.length ? `<div class="section-head mh-group-head"><h2>历次改进 <span class="badge-count">${past.length}</span></h2></div><ol class="mh-pasts">${past.map(r => hubPastRound(data, model, r)).join('')}</ol>` : '');
}

// ---------------------------------------------------------------- 训练与测试数据: the two libraries

function hubSetCard(data, set) {
  const cat = hubCat(data, set.category), poses = Boolean(set.seeds);
  const targets = set.purpose === 'evaluation' ? hubTargets(cat, set).map(t => `<span class="tag">${h(t.measure.name)} ${h(hubTarget(t.measure, t.target))}</span>`).join('') : '';
  return `<article class="mh-card mh-set" role="listitem" data-set="${h(set.id)}" data-cat="${h(cat.id)}">` +
    `<header class="mh-card-top">${hubChip(cat)}${set.builtin ? badge('随应用带来') : ''}</header><h3>${h(set.name)}</h3>` +
    `<p class="mh-product">${set.product ? h(hubProduct(set.product)) : '不限品牌型号'}</p>` +
    `<div class="mh-metric"><strong>${h(set.count)}</strong><span>${poses ? '个起姿' : '条'}${set.file ? ' · ' + h(set.file.filename) : ''}</span></div>` +
    `<p class="mh-goal">${h(set.description)}</p><p class="mh-set-targets">${targets}</p>` +
    `<footer class="mh-card-foot"><small>${set.purpose === 'evaluation' ? `用于 ${h(set.used)} 次评测` : `用于 ${h(set.used)} 轮改进`}</small>` +
    `<div class="button-row">${button('查看', 'hub-set-open', 'secondary small', set.id)}</div></footer></article>`;
}

// A data version a task registered (the old page's record).
function hubDatasetCard(st, record) {
  const task = ((st.projects || []).find(p => p.id === record.project_id) || {}).name || '';
  return `<article class="mh-card mh-set legacy" role="listitem" data-legacy="${h(record.id)}"><header class="mh-card-top"><span class="mh-chip" data-cat="other"><i aria-hidden="true"></i>数据版本</span>` +
    `${badge('任务登记')}</header><h3>${h(record.name)}</h3><p class="mh-product">${task ? '任务「' + h(task) + '」' : ''}</p>` +
    `<div class="mh-metric none"><strong>${h(record.version)}</strong><span>${h(record.format)}</span></div><p class="mh-goal">${h(record.source)}</p><p class="mh-set-targets"></p>` +
    `<footer class="mh-card-foot"><small>只登记地址，数据不上传</small><div class="button-row">${button('详情', 'detail-datasets', 'secondary small', record.id)}</div></footer></article>`;
}

function hubDataPage(st, data) {
  const of = purpose => data.sets.filter(s => s.purpose === purpose), tests = of('evaluation'), trains = of('training'), versions = st.datasets || [];
  const index = '<h2>两个库</h2><ul class="mh-cats mh-two">' +
    `<li><button type="button" class="mh-cat" data-action="hub-jump" data-id="hub-evaluation"><i class="mh-swatch" data-cat="motion_rl" aria-hidden="true"></i><span>评测库：一个模型好不好，用它量</span><em>${tests.length}</em></button></li>` +
    `<li><button type="button" class="mh-cat" data-action="hub-jump" data-id="hub-training"><i class="mh-swatch" data-cat="llm" aria-hidden="true"></i><span>训练库：一轮改进用了哪些数据</span><em>${trains.length + versions.length}</em></button></li></ul>`;
  const board = hubBoard('数据集', data.sets.length + versions.length, `${new Set(data.sets.map(s => s.category)).size} 个分类`, index,
    [['评测集', tests.length], ['训练集', trains.length + versions.length], ['评测用过', tests.reduce((n, s) => n + s.used, 0) + ' 次']]);
  const wall = (id, title, note, cards, add) => `<section class="mh-group" id="${id}" aria-label="${title}"><div class="section-head mh-group-head"><h2>${title} <span class="badge-count">${cards.length}</span></h2><span>${note}</span></div>` +
    (cards.length ? `<div class="mh-wall" role="list">${cards.join('')}</div>` : `<div class="mh-empty"><p>还没有${title === '评测库' ? '评测集' : '训练集'}。</p>${add}</div>`) + '</section>';
  return hubHead('datasets', button(icon('plus') + ' 新建评测集', 'hub-set-add', '', 'evaluation') + button(icon('plus') + ' 新建训练集', 'hub-set-add', 'secondary', 'training') +
      button('登记数据版本', 'dataset-add', 'secondary')) + board +
    wall('hub-evaluation', '评测库', '按模型分类；每个集可以有自己的达标线', tests.map(s => hubSetCard(data, s)), button('新建评测集', 'hub-set-add', 'secondary small', 'evaluation')) +
    wall('hub-training', '训练库', '训练集，和任务登记的数据版本', trains.map(s => hubSetCard(data, s)).concat(versions.map(d => hubDatasetCard(st, d))), button('新建训练集', 'hub-set-add', 'secondary small', 'training'));
}

// The body of the dialog of one set.
function hubSetDetail(data, set) {
  const cat = hubCat(data, set.category);
  const targets = set.purpose === 'evaluation' ? hubTargets(cat, set).map(t => `${t.measure.name} ${hubTarget(t.measure, t.target)}`).join('，') : '';
  return `<div class="dialog-body mh-set-detail"><p>${hubChip(cat)} ${badge(HUB_PURPOSES[set.purpose])}</p><p>${h(set.description)}</p><dl class="mh-holds wide">` +
    hubHold('品牌型号', set.product ? h(hubProduct(set.product)) : '不限') + hubHold(set.seeds ? '起姿' : '条数', h(set.count)) +
    (set.source ? hubHold('来源', h(set.source)) : '') + (targets ? hubHold('达标线', h(targets)) : '') +
    (set.file ? hubHold('文件', `${h(set.file.filename)} · ${hubSize(set.file.size)} · <a class="text-link" href="/api/model-hub/file?id=${encodeURIComponent(set.file.id)}" download>下载</a>`) : '') +
    hubHold('使用', set.purpose === 'evaluation' ? `${h(set.used)} 次评测` : `${h(set.used)} 轮改进`) + '</dl>' +
    (set.seeds ? `<p class="mh-seeds" data-keep-code>${set.seeds.map(s => `<span>${h(s)}</span>`).join('')}</p>` : '') +
    (set.builtin || set.used ? '' : `<div class="button-row">${button('删除这个集', 'hub-set-remove', 'secondary small', set.id)}</div>`) + '</div>';
}
