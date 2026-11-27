'use strict';
// Issue #71 — 模型库 on the page: reading model-hub/overview, what is open, the dialogs, uploading a file
// in chunks, and following an evaluation while it runs. The markup is web/model_hub.js.
//
// The other scripts are not edited. The pages are taken over once every script has loaded
// (DOMContentLoaded), so the place of this file in index.html does not matter. The forms here are
// `data-hub-form`, not `data-form`: the submit handler of web/app.js leaves them alone.

const HUB_SUBPAGES = {model: 'models', 'model-improve': 'training'};      // a page of one model → the sidebar page it belongs to
const HUB_PAGES = ['models', 'training', 'datasets', 'model', 'model-improve'];
const HUB_MODEL_KEY = 'nx-hub-model';             // browser storage: the model whose page is open
const HUB_POLL_MS = 1000;

let hubData = null;              // the last answer of model-hub/overview
let hubLoading = null;           // the read that is under way (a promise)
let hubAgain = false;            // one more read was asked for meanwhile
let hubFailed = '';
let hubFilter = '';              // the category 模型库 shows; '' for all
let hubModelId = '';
let hubLiveDetail = null;        // the evaluation that is running on the page, with its runs
let hubLiveTimer = 0;
let hubMoreOpen = false;         // 「从平台选择更多模型」 stays open over a redraw
let hubUser = '';                // the account `hubData` was read for

// ---------------------------------------------------------------- data

// Reads the overview. A call made while one is under way waits for it and then reads once more, so what
// the caller sees is never older than its own change.
async function hubLoad() {
  if (hubLoading) {
    hubAgain = true;
    return hubLoading;
  }
  hubLoading = (async () => {
    do {
      hubAgain = false;
      const asked = hubUser;
      try {
        const data = await api('model-hub/overview', {});
        if (asked === hubUser) { hubData = data; hubFailed = ''; }      // not the answer for an account that has left
      } catch (err) {
        hubFailed = err.message || '读取失败';
      }
    } while (hubAgain);
  })();
  try { await hubLoading; } finally { hubLoading = null; }
  if (HUB_PAGES.includes(current)) render();
  return undefined;
}

function hubRecall() {
  if (!hubModelId) {
    try { hubModelId = sessionStorage.getItem(HUB_MODEL_KEY) || ''; } catch { /* no storage: nothing is remembered */ }
  }
  return hubModelId;
}

function hubRemember(id) {
  hubModelId = id || '';
  try { if (hubModelId) sessionStorage.setItem(HUB_MODEL_KEY, hubModelId); else sessionStorage.removeItem(HUB_MODEL_KEY); } catch { /* not remembered */ }
}

function hubGo(route) {
  if ($('#dialog').open) $('#dialog').close();
  if (location.hash === '#' + route) render();
  else location.hash = route;
}

const hubOpenModel = () => (hubData ? hubModel(hubData, hubRecall()) : null);

// ---------------------------------------------------------------- the views

// What a page shows before the first answer has arrived, or when it did not arrive.
function hubWaiting(route) {
  const head = HUB_SUBPAGES[route] ? '' : hubHead(route, '');
  return head + (hubFailed ? `<div class="mh-empty" role="alert"><p>没有读到模型库：${h(hubFailed)}</p>${button('再读一次', 'hub-reload', 'secondary small')}</div>`
    : '<div class="mh-empty mh-loading" role="status"><p>正在读取…</p></div>');
}

function hubMore() {
  const engine = typeof engineModelsSummary === 'function' ? `<details class="mh-more"><summary>工程助手使用的大模型</summary>${engineModelsSummary()}</details>` : '';
  return `<details class="mh-more" data-hub-more${hubMoreOpen ? ' open' : ''}><summary>从平台选择更多模型</summary>${noProject()}` +
    `<div class="button-row mh-more-row">${button('接入自有模型服务', 'model-add', 'secondary small')}</div>${catalogModels()}</details>${engine}`;
}

const hubViews = {
  models: () => hubLibraryPage(state, hubData, hubFilter, record => resultModelActions(record)) + hubMore(),
  training: () => hubTrainingPage(state, hubData, jobTable(rows('jobs').filter(j => j.kind === 'training'), ''), Boolean(state.duck_kit && state.duck_kit.shows.length)),
  datasets: () => hubDataPage(state, hubData),
  model: () => (hubOpenModel() ? hubModelPage(state, hubData, hubOpenModel()) : hubViews.models()),
  'model-improve': () => (hubOpenModel() ? hubImprovePage(state, hubData, hubOpenModel(), hubLiveDetail) : hubViews.training()),
};

// The sidebar and the breadcrumb know the page of one model as part of its sidebar page.
function hubChrome() {
  const parent = HUB_SUBPAGES[current];
  if (!parent) return;
  const link = document.querySelector(`#navigation a[href="#${parent}"]`), crumb = document.getElementById('breadcrumb'), model = hubOpenModel();
  if (link) { link.classList.add('active'); link.setAttribute('aria-current', 'page'); }
  if (crumb) crumb.textContent = (parent === 'models' ? '模型库' : '训练与改进') + ' / ' + (model ? model.name : '模型');
}

// ---------------------------------------------------------------- an evaluation that is running

function hubSeedsOf(detail) {
  const set = hubData ? hubSet(hubData, detail.set_id) : null;
  return (set && set.seeds) || [];
}

// While an evaluation runs, its record is asked for every second and what the page shows of it is
// changed in place (the block with one mark per start pose, the count in the table): the page is not redrawn.
async function hubLiveTick() {
  hubLiveTimer = 0;
  const running = hubData && HUB_PAGES.includes(current) ? hubRunningAll(hubData) : [];
  if (!running.length) return undefined;
  let ended = false;
  for (const one of running) {
    let detail;
    try {
      detail = await api('model-hub/evaluations/detail', {evaluation_id: one.id});
    } catch {
      continue;                                         // asked again a second later
    }
    if (hubOpenModel() && detail.model_id === hubOpenModel().id) hubLiveDetail = detail;
    hubLiveShow(detail);
    ended = ended || detail.status !== 'running';
  }
  return ended ? hubLoad() : hubLiveSync();             // one ended: its round moved on, its model has a result
}

// What the page shows of a running evaluation, changed in place: the sentence, the marks, the count in the
// table. The block itself (and its 停止评测 button, which may have the focus) is replaced only when the
// evaluation is no longer running.
function hubLiveShow(detail) {
  const id = CSS.escape(detail.id);
  document.querySelectorAll(`[data-hub-live="${id}"]`).forEach(el => {
    if (el.dataset.hubStatus !== detail.status) { el.outerHTML = hubLive(detail, hubSeedsOf(detail)); return; }
    const text = hubLiveText(detail), line = el.querySelector('p'), marks = el.querySelector('.mh-runs');
    if (line.textContent !== text) line.textContent = text;
    const next = hubRuns(detail, hubSeedsOf(detail));
    if (marks.outerHTML !== next) marks.outerHTML = next;
  });
  document.querySelectorAll(`[data-hub-count="${id}"]`).forEach(el => { el.textContent = `${detail.cases.length} / ${detail.total}`; });
}

function hubLiveSync() {
  if (!hubLiveTimer && hubData && HUB_PAGES.includes(current) && hubRunning(hubData)) hubLiveTimer = setTimeout(hubLiveTick, HUB_POLL_MS);
}

// ---------------------------------------------------------------- dialogs

function hubForm(title, sub, fields, kind, submit, id = '') {
  dialog(title, sub, `<form data-hub-form="${kind}" data-hub-id="${h(id)}"><div class="dialog-body"><div class="form-error" role="alert"></div>${fields}</div>` +
    `<div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">${submit}</button></div></form>`);
}

const hubOptional = (name, placeholder = '', type = 'text', more = '') => `<input name="${name}" type="${type}" placeholder="${h(placeholder)}" ${more}>`;
const hubSelect = (name, options, chosen = '') => `<select name="${name}" required>${options.map(([value, text]) =>
  `<option value="${h(value)}"${value === chosen ? ' selected' : ''}>${h(text)}</option>`).join('')}</select>`;
const hubCategoryOptions = () => hubData.categories.map(c => [c.id, c.name]);

function hubProductFields(product) {
  return `<div class="mh-pair">${field('品牌（可选）', hubOptional('vendor', '例如：Microduck', 'text', `value="${h(product ? product.vendor : '')}"`))}` +
    `${field('型号（可选）', hubOptional('model', '例如：AD-P102', 'text', `value="${h(product ? product.model : '')}"`))}</div>`;
}

// The inputs of a category's measures: the values of an evaluation, or (targets) a set's own targets.
function hubMeasureInputs(categoryId, targets = false) {
  const cat = hubCat(hubData, categoryId);
  return `<fieldset class="mh-measure-inputs"><legend>${targets ? '达标线（不填就用这个分类的）' : '指标'}</legend>` + cat.measures.map(m =>
    `<label><span>${h(m.name)}${m.unit ? `（${h(m.unit)}）` : ''}${!targets && m.primary ? '<em>必填</em>' : ''}</span>` +
    `<input name="measure:${h(m.id)}" type="number" step="any" inputmode="decimal" placeholder="${targets && m.target !== null ? h(hubTarget(m, m.target)) : ''}"${!targets && m.primary ? ' required' : ''}></label>`).join('') + '</fieldset>';
}

function hubMeasureValues(form) {
  const values = {};
  new FormData(form).forEach((value, name) => { if (name.startsWith('measure:') && value !== '') values[name.slice(8)] = Number(value); });
  return values;
}

// The part of the set form that depends on its category.
function hubSetDynamic(purpose, categoryId) {
  const poses = purpose === 'evaluation' && categoryId === 'motion_rl';
  return (poses ? field('起姿编号', '<textarea name="seeds" placeholder="例如：1700003000, 1700003001, 1700003002" required></textarea>', '一个编号是一个起姿：同一个编号每次都从同一个姿态开始。用逗号或空格分开。')
    : field('有多少条', '<input name="count" type="number" min="1" step="1" placeholder="例如：500" required>')) +
    (purpose === 'evaluation' ? hubMeasureInputs(categoryId, true) : '');
}

function hubKey(id) {
  const [model, round = '', version = ''] = String(id || '').split('|');
  return {model: hubModel(hubData, model), round, version};
}

const hubFileField = (label, multiple, required) => field(label, `<input name="files" type="file"${multiple ? ' multiple' : ''}${required ? ' required' : ''}>`,
  `文件保存在这台电脑上，一个文件最多 ${hubSize(hubData.limits.file_bytes)}。`) + '<p class="mh-progress" role="status" aria-live="polite"></p>';

function hubDialog(type, id) {
  if (type === 'hub-pick') {
    return dialog('选择模型', '选一个模型，一轮一轮地改进。', `<div class="dialog-body"><ul class="mh-pick">${hubData.models.map(m =>
      `<li><button type="button" data-action="hub-improve" data-id="${h(m.id)}">${hubChip(hubCat(hubData, m.category))}<b>${h(m.name)}</b><small>${h(hubRoundLine(m) || m.status_name)}</small></button></li>`).join('')}</ul></div>`);
  }
  if (type === 'hub-model-add') {
    return hubForm('添加模型', '先登记模型，再把训练好的模型文件传上来。', field('模型名称', input('name', '例如：走廊导航')) +
      field('分类', hubSelect('category', hubCategoryOptions(), id || 'motion_rl'), '分类决定这个模型用哪些指标来评测。') + hubProductFields(null) +
      '<p class="inline-note">和机器人本体有关的模型（动作、导航等）请填品牌型号：训练好以后，它是这个品牌型号的技能。</p>' +
      field('说明（可选）', '<textarea name="summary" placeholder="这个模型做什么"></textarea>'), 'model', '添加');
  }
  if (type === 'hub-model-remove') {
    const model = hubModel(hubData, id);
    return hubForm(`删除「${h(model.name)}」`, '', `<p>它的 ${model.versions.length} 个版本的模型文件、${model.evaluations.length} 条评测记录和 ${model.rounds.length} 轮改进记录会一起删除，不能恢复。</p>`, 'model-remove', '删除', id);
  }
  if (type === 'hub-version-add') {
    const {model, round} = hubKey(id);
    const origin = model.show ? `<div class="mh-pair">${field('训练轮次（可选）', hubOptional('origin_round', '例如：41', 'number', 'min="0" step="1"'))}` +
      `${field('第几次更新（可选）', hubOptional('origin_update', '例如：8', 'number', 'min="0" step="1"'))}</div>` +
      '<p class="inline-note">填了这两项，就能从训练项目的评估记录里取这一版的成绩。</p>' : '';
    return hubForm('上传模型文件', round ? '成为这一轮的候选版本。' : `成为「${h(model.name)}」的一个新版本。`,
      field('版本名', input('label', '例如：v2，或 第 41 轮')) + hubFileField('模型文件', true, true) + origin +
      field('说明（可选）', '<textarea name="note" placeholder="这一版改了什么"></textarea>'), 'version', '上传', id);
  }
  if (type === 'hub-skill') {
    const model = hubModel(hubData, id), fold = p => `${p.vendor}\n${p.model}`.toLowerCase();
    const same = s => !model.product || !s.product || fold(s.product) === fold(model.product);      // the server decides (it knows other spellings); this only shortens the list
    const skills = (state.skills || []).filter(same);
    return hubForm('绑定技能', '模型训练完就是一个技能：选出用这个模型的技能。', field('技能', hubSelect('skill_id', [['', '不绑定'], ...skills.map(s => [s.id, `${s.name} v${s.version}`])],
      model.skill && !model.skill.auto ? model.skill.id : '').replace(' required', ''), skills.length ? '只列出同一品牌型号的技能。' : '这个品牌型号还没有技能；先在「机器人应用 → 技能」里登记。'), 'skill', '保存', id);
  }
  if (type === 'hub-eval-import' || type === 'hub-eval-run') {
    const {model, round, version} = hubKey(id), found = round ? model.rounds.find(r => r.id === round) : null, running = type === 'hub-eval-run';
    const versions = model.versions.filter(v => (version ? v.id === version : !running || v.runnable));
    const sets = hubData.sets.filter(s => s.purpose === 'evaluation' && s.category === model.category && (found ? s.id === found.set_id : !running || s.runnable));
    const base = field('版本', hubSelect('version_id', versions.map(v => [v.id, v.label]), model.current_version_id)) +
      (sets.length ? field('评测集', hubSelect('set_id', sets.map(s => [s.id, `${s.name}（${s.count} ${s.seeds ? '个起姿' : '条'}）`])))
        : notice('这个分类还没有评测集。 ' + button('新建评测集', 'hub-set-add', 'secondary small', 'evaluation'), true));
    if (!running) {
      return hubForm('导入评测结果', '在别处评测出来的指标，记到这一版上。', base + hubMeasureInputs(model.category) +
        `<div class="mh-pair">${field('通过条数（可选）', hubOptional('passed', '', 'number', 'min="0" step="1"'))}${field('评测条数（可选）', hubOptional('total', '', 'number', 'min="0" step="1"'))}</div>` +
        field('结果出处', input('source', '例如：训练机上的评测报告，2026-11-27')), 'eval-import', '导入', id);
    }
    const ducks = hubDucks(state);
    const robot = ducks.length ? field('仿真小鸭', hubSelect('robot_id', ducks.map(r => [r.id, r.name])), '评测期间这个仿真小鸭不能做别的。')
      : notice('要有一个正在运行的仿真小鸭。 ' + (hubData.duck.available ? button('添加仿真小鸭', 'hub-duck-add', 'secondary small') : h(hubData.duck.problem)), true);
    return hubForm('在仿真里评测', '每个起姿运行一遍，由模型包自带的裁判判定。仿真里的结果不是现场证据。', base + robot, 'eval-run', '开始评测', id);
  }
  if (type === 'hub-round-add') {
    const model = hubModel(hubData, id), sets = hubData.sets.filter(s => s.purpose === 'evaluation' && s.category === model.category);
    const number = Math.max(0, ...model.rounds.map(r => r.number)) + 1;
    return hubForm(`开始第 ${number} 轮改进`, '一轮从一个版本出发，在一个评测集上比较训练前后。', field('从哪个版本出发', hubSelect('version_id', model.versions.map(v => [v.id, v.label]), model.current_version_id)) +
      (sets.length ? field('评测集', hubSelect('set_id', sets.map(s => [s.id, `${s.name}（${s.count} ${s.seeds ? '个起姿' : '条'}）`])), '起点和候选都在这个评测集上评测，才能比较。')
        : notice('这个分类还没有评测集。 ' + button('新建评测集', 'hub-set-add', 'secondary small', 'evaluation'), true)) +
      field('这一轮的目标', textarea('goal', '例如：独立起姿组的成功率从 80% 到 100%')), 'round', '开始', id);
  }
  if (type === 'hub-diagnose') {
    const model = hubOpenModel(), sets = hubData.sets.filter(s => s.purpose === 'training' && s.category === model.category);
    return hubForm('短板和做法', '基线说明了什么，这一轮训练要改什么。', field('短板', textarea('weaknesses', '例如：独立组第 3、7 个起姿在前滚后站不稳')) +
      field('改进做法', textarea('plan', '例如：加大落地阶段的奖励，从上一轮的权重继续训练 100 次更新')) +
      (sets.length ? `<fieldset class="mh-checks"><legend>用到的训练集（可选）</legend>${sets.map(s => `<label><input type="checkbox" name="training_set" value="${h(s.id)}"> ${h(s.name)}</label>`).join('')}</fieldset>` : ''),
      'diagnose', '开始训练', id);
  }
  if (['hub-round-accept', 'hub-round-reject', 'hub-round-drop'].includes(type)) {
    const accept = type === 'hub-round-accept', drop = type === 'hub-round-drop';
    return hubForm(accept ? '采纳候选版本' : drop ? '放弃这一轮' : '不采纳候选版本',
      accept ? '候选版本成为这个模型的当前版本。' : drop ? '这一轮到此为止。已经上传的版本和评测记录都留着。' : '当前版本不变。候选版本留在版本列表里。',
      field('说明（可选）', '<textarea name="note" placeholder="为什么"></textarea>'), accept ? 'round-accept' : 'round-reject', accept ? '采纳' : drop ? '放弃' : '不采纳', id);
  }
  if (type === 'hub-set-add') {
    const purpose = id === 'training' ? 'training' : 'evaluation', first = hubFilter || 'motion_rl';
    return hubForm(purpose === 'evaluation' ? '新建评测集' : '新建训练集', purpose === 'evaluation' ? '评测集用来量一个模型好不好。' : '训练集记下一轮改进用了哪些数据。',
      field('名称', input('name', purpose === 'evaluation' ? '例如：雨天起姿' : '例如：走廊轨迹 11 月')) + field('分类', hubSelect('category', hubCategoryOptions(), first)) +
      hubProductFields(null) + field('说明', textarea('description', '里面是什么，怎么来的')) + `<div data-hub-dynamic data-purpose="${purpose}">${hubSetDynamic(purpose, first)}</div>` +
      field('来源（可选）', hubOptional('source', '例如：2026 年 11 月现场采集')) + hubFileField('文件（可选）', false, false), 'set', '新建', purpose);
  }
  if (type === 'hub-set-open') {
    const set = hubSet(hubData, id);
    return dialog(h(set.name), HUB_PURPOSES[set.purpose], hubSetDetail(hubData, set));
  }
  return undefined;
}

// ---------------------------------------------------------------- uploading

async function hubPutChunk(uploadId, index, blob) {
  const response = await fetch(`/api/model-hub/chunk?upload=${encodeURIComponent(uploadId)}&index=${index}`,
    {method: 'PUT', headers: {'Content-Type': 'application/octet-stream'}, body: blob});
  let result = {};
  try { result = await response.json(); } catch { /* an answer without a body: the status below says it */ }
  if (!response.ok) throw Object.assign(Error(result.error || '这一片没有送到'), {status: response.status, reason: typeof result.code === 'string' ? result.code : '', body: result});
  return result;
}

// Send one file; returns its record. `say(text)` reports progress.
async function hubUpload(file, say) {
  if (file.size > hubData.limits.file_bytes) throw Error(`「${file.name}」太大：一个文件最多 ${hubSize(hubData.limits.file_bytes)}`);
  const started = await api('model-hub/upload/start', {filename: file.name, size: file.size});
  try {
    for (let index = 0; index < started.chunk_count; index += 1) {
      say(`正在上传「${file.name}」：${Math.round(100 * index / started.chunk_count)}%`);
      await hubPutChunk(started.upload_id, index, file.slice(index * started.chunk_bytes, (index + 1) * started.chunk_bytes));
    }
    return await api('model-hub/upload/complete', {upload_id: started.upload_id});
  } catch (err) {
    api('model-hub/upload/cancel', {upload_id: started.upload_id}).catch(() => {});
    throw err;
  }
}

async function hubUploadAll(form) {
  const say = text => { form.querySelector('.mh-progress').textContent = text; };
  const kept = [];
  for (const file of form.elements.files.files) kept.push((await hubUpload(file, say)).id);
  say(kept.length ? '文件已经传完，正在保存…' : '');
  return kept;
}

// ---------------------------------------------------------------- acting

const hubWhole = value => (value === '' || value === undefined ? undefined : Number(value));

async function hubSubmit(kind, form) {
  const data = Object.fromEntries(new FormData(form)), id = form.dataset.hubId;
  if (kind === 'model') {
    const made = await api('model-hub/models', {name: data.name, category: data.category, vendor: data.vendor, model: data.model, summary: data.summary});
    hubRemember(made.id);
    return {go: 'model', said: '模型已添加'};
  }
  if (kind === 'model-remove') {
    await api('model-hub/models/remove', {model_id: id, confirm: true});
    return {go: 'models', said: '模型已删除'};
  }
  if (kind === 'version') {
    const {model, round} = hubKey(id);
    // Asked before the file travels: a refusal after a long upload would cost the upload.
    if (model.versions.some(v => v.label === data.label.trim())) throw Error('这个模型已经有一个同名的版本');
    await api('model-hub/versions', {model_id: model.id, round_id: round || undefined, label: data.label, note: data.note, file_ids: await hubUploadAll(form),
                                     origin_round: hubWhole(data.origin_round), origin_update: hubWhole(data.origin_update)});
    return {said: '模型文件已保存为新版本'};
  }
  if (kind === 'skill') {
    await api('model-hub/skill', {model_id: id, skill_id: data.skill_id});
    return {said: data.skill_id ? '技能已绑定' : '已取消绑定'};
  }
  if (kind === 'eval-import' || kind === 'eval-run') {
    const {model, round} = hubKey(id), base = {model_id: model.id, round_id: round || undefined, version_id: data.version_id, set_id: data.set_id};
    if (kind === 'eval-run') {
      hubLiveDetail = {...await api('model-hub/evaluations/run', {...base, robot_id: data.robot_id}), cases: []};
      return {said: '评测开始了'};
    }
    await api('model-hub/evaluations/import', {...base, metrics: hubMeasureValues(form), passed: hubWhole(data.passed), total: hubWhole(data.total), source: data.source});
    return {said: '评测结果已导入'};
  }
  if (kind === 'round') {
    await api('model-hub/rounds', {model_id: id, version_id: data.version_id, set_id: data.set_id, goal: data.goal});
    hubRemember(id);
    return {go: 'model-improve', said: '这一轮开始了'};
  }
  if (kind === 'diagnose') {
    await api('model-hub/rounds/diagnose', {round_id: id, weaknesses: data.weaknesses, plan: data.plan, training_set_ids: new FormData(form).getAll('training_set')});
    return {said: '已记下。训练完把新模型文件传上来'};
  }
  if (kind === 'round-accept' || kind === 'round-reject') {
    await api('model-hub/rounds/close', {round_id: id, decision: kind === 'round-accept' ? 'accept' : 'reject', note: data.note});
    return {said: kind === 'round-accept' ? '候选版本已成为当前版本' : '这一轮已结束'};
  }
  if (kind === 'set') {
    const files = form.elements.files.files.length ? await hubUploadAll(form) : [];
    await api('model-hub/sets', {purpose: id, name: data.name, category: data.category, vendor: data.vendor, model: data.model, description: data.description,
                                 seeds: data.seeds, count: hubWhole(data.count), source: data.source, targets: hubMeasureValues(form), file_id: files[0]});
    return {said: id === 'evaluation' ? '评测集已新建' : '训练集已新建'};
  }
  return {};
}

async function hubAction(type, id) {
  if (type === 'hub-reload') return hubLoad();
  if (type === 'hub-filter') { hubFilter = id === hubFilter ? '' : id || ''; return render(); }
  if (type === 'hub-open') { hubRemember(id); return hubGo('model'); }
  if (type === 'hub-improve') { hubRemember(id); return hubGo('model-improve'); }
  if (type === 'hub-back-models') return hubGo('models');
  if (type === 'hub-back-training') return hubGo('training');
  if (type === 'hub-jump') return document.getElementById(id)?.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  if (type === 'hub-eval-open') {
    const detail = await api('model-hub/evaluations/detail', {evaluation_id: id}), model = hubModel(hubData, detail.model_id);
    if (!model) return hubLoad();
    dialog('评测', h(model.name), hubEvalDetail(hubData, model, detail));
    $('#dialog').classList.add('wide');
    return hubLiveSync();
  }
  if (type === 'hub-eval-stop') { await api('model-hub/evaluations/stop', {evaluation_id: id}); return toast('这一回合结束后停止'); }
  if (type === 'hub-eval-training') {
    const {model, round, version} = hubKey(id);
    await api('model-hub/evaluations/import-training', {model_id: model.id, round_id: round || undefined, version_id: version});
    toast('已取到训练项目的评估');
    return hubLoad();
  }
  if (type === 'hub-set-remove') {
    await api('model-hub/sets/remove', {set_id: id});
    $('#dialog').close();
    toast('已删除');
    return hubLoad();
  }
  if (type === 'hub-duck-add') {
    await api('sims', {profile_id: hubData.duck.profile_id, project_id: projectId || undefined});
    $('#dialog').close();
    toast('仿真小鸭正在启动，启动好以后再开始评测');
    return refresh(true);
  }
  if (!hubData || (id && /^hub-(model-remove|version-add|skill|eval-import|eval-run|round-add)$/.test(type) && !hubKey(id).model) ||
      (type === 'hub-diagnose' && !hubOpenModel()) || (type === 'hub-set-open' && !hubSet(hubData, id))) {
    toast('它已经不在了，页面已更新');                   // what the page showed was older than a deletion
    return hubLoad();
  }
  return hubDialog(type, id);
}

async function hubSubmitted(event) {
  const form = event.target.closest('[data-hub-form]');
  if (!form) return;
  event.preventDefault();
  const send = form.querySelector('[type="submit"]'), error = form.querySelector('.form-error');
  send.disabled = true;
  error.textContent = '';
  try {
    const done = await hubSubmit(form.dataset.hubForm, form);
    if (form.isConnected && $('#dialog').open) $('#dialog').close();      // not a dialog that was opened meanwhile
    if (done.said) toast(done.said);
    await Promise.all([hubLoad(), refresh(true)]);
    if (done.go) hubGo(done.go);
  } catch (err) {
    if (form.querySelector('.mh-progress')) form.querySelector('.mh-progress').textContent = '';
    if (typeof formError === 'function') formError(error, err); else error.textContent = err.message;
    if (form.isConnected) send.disabled = false;
  }
}

function hubInstall() {
  HUB_PAGES.forEach(route => { views[route] = () => (hubData ? hubViews[route]() : hubWaiting(route)); });
  const baseAction = action, baseRender = render;
  action = async (type, id, el) => (String(type).startsWith('hub-') ? hubAction(type, id) : baseAction(type, id, el));
  render = function () {
    const mine = HUB_PAGES.includes(current), who = state.user ? state.user.id : '';
    if (who !== hubUser) {                                  // another account signed in: what was read is not theirs
      hubUser = who;
      hubData = null;
      hubFailed = '';
      hubFilter = '';
      hubLiveDetail = null;
      if (hubModelId) hubRemember('');
    }
    if (mine && state.user && !hubData && !hubLoading && !hubFailed) hubLoad();
    // The page of a model that is gone (deleted, or nothing was opened) is its sidebar page.
    if (HUB_SUBPAGES[current] && hubData && !hubOpenModel()) {
      current = HUB_SUBPAGES[current];
      history.replaceState(null, '', location.pathname + location.search + '#' + current);
    }
    baseRender();
    if (mine) { hubChrome(); hubLiveSync(); }
  };
  document.addEventListener('submit', hubSubmitted);
  document.addEventListener('change', event => {
    const box = event.target.matches?.('[data-hub-form="set"] [name="category"]') ? event.target.form.querySelector('[data-hub-dynamic]') : null;
    if (box) box.innerHTML = hubSetDynamic(box.dataset.purpose, event.target.value);
  });
  document.addEventListener('toggle', event => { if (event.target.matches?.('[data-hub-more]')) hubMoreOpen = event.target.open; }, true);
  // Coming to one of the pages: what other pages changed meanwhile (a skill, a simulated duck) is read again.
  window.addEventListener('hashchange', () => { if (HUB_PAGES.includes(location.hash.slice(1)) && state.user) hubLoad(); });
  if (typeof route === 'function') route();               // #model and #model-improve were not pages while the earlier scripts loaded
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', hubInstall);
