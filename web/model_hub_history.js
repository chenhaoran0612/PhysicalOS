'use strict';
// Issue #76 — the candidate of a round does not have to be uploaded: 「从历史中选择」 offers the model's own
// versions (earlier uploads, candidates an earlier round did not take, the package that came with the app)
// and, for the Microduck's skill models, the model files the training project kept for every evaluated
// update (read from its dashboard on this computer). A click makes one the round's candidate.
// Server side: brainos/model_hub_history.py. Doc: docs/MODEL_HUB_HISTORY.md.
//
// The markup functions are pure (tests/model_hub_history_logic.cjs); the rest opens the dialog, reads the
// two lists (the versions at once, the training project's files when the dashboard has answered) and
// chooses. web/model_hub.js and web/model_hub_pages.js are not otherwise changed: `hubAction` is wrapped.

const HUB_HISTORY_PAGE = 20;                       // training project files shown at first, and added by 「再显示」
const HUB_HISTORY_ACTIONS = ['hub-history', 'hub-history-pick', 'hub-history-more'];

// ---------------------------------------------------------------- markup

// When the dashboard evaluated a file (epoch seconds): 11-27 18:03.
function hubHistoryWhen(at) {
  if (typeof at !== 'number') return '';
  const d = new Date(at * 1000), two = n => String(n).padStart(2, '0');
  return `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

function hubHistoryItem(id, disabled, name, meta, result, said) {
  return `<li class="mhh-item${disabled ? ' off' : ''}"><button type="button" class="mhh-pick" data-action="hub-history-pick" data-id="${h(id)}"${disabled ? ' disabled' : ''}>` +
    `<span class="mhh-main"><span class="mhh-name">${name}</span><span class="mhh-meta">${meta}</span></span>` +
    `<span class="mhh-side">${result}<span class="mhh-go">${h(said)}</span></span></button></li>`;
}

// One of the model's versions as a choice. A result it has on the round's set comes with it.
function hubHistoryVersion(data, model, round, set, v) {
  const marks = [v.id === model.current_version_id ? '当前版本' : '', v.builtin ? '随应用带来' : '', v.round_number ? `第 ${v.round_number} 轮的候选` : '']
    .filter(Boolean).map(m => `<span class="mh-mark">${h(m)}</span>`).join('');
  const files = v.files.map(f => [f.filename, hubSize(f.size)].filter(Boolean).join(' ')).join('、');
  const meta = [hubOrigin(v.origin), files, hubDay(v.created_at)].filter(Boolean).map(h).join(' · ');
  const result = v.evaluation ? `<span class="mhh-result" title="在「${h(set ? set.name : '')}」上">${hubMeasureTags(hubCat(data, model.category), v.evaluation.metrics)}</span>` : '';
  const said = v.candidate ? '现在的候选' : v.evaluation ? '选它，用已有的评测' : '选为候选';
  return hubHistoryItem(`${round.id}|v|${v.id}`, v.candidate, `<b>${h(v.label)}</b>${marks}`, meta, result, said);
}

// One model file of the training project as a choice.
function hubHistoryFile(round, f) {
  const said = f.start ? '这一轮的起点' : f.candidate ? '现在的候选' : f.version_id ? '已是一个版本，选为候选' : '取来当候选';
  const meta = [f.steps ? `${f.steps} 步` : '', f.device, hubHistoryWhen(f.at)].filter(Boolean).map(h).join(' · ');
  const result = `<span class="mhh-score ${f.passed === f.total ? 'good' : f.passed ? 'warn' : 'bad'}">${f.passed} / ${f.total} 通过</span>`;
  return hubHistoryItem(`${round.id}|t|${f.key}`, f.start || f.candidate, `<b>第 ${f.round} 轮 · 第 ${f.update} 次更新</b>`, meta, result, said);
}

// The training project's part: `training` is null (not a duck's model), {loading}, or the route's answer.
function hubHistoryTraining(round, training, view) {
  if (!training) return '';
  const head = extra => `<div class="mhh-part-head"><h3>训练项目里的模型文件</h3>${extra || ''}</div>`;
  if (training.loading) return `<section class="mhh-part" aria-label="训练项目里的模型文件">${head()}<p class="mhh-wait" role="status">正在读取训练看板…</p></section>`;
  if (!training.available) return `<section class="mhh-part" aria-label="训练项目里的模型文件">${head()}<p class="mh-warn">${h(training.problem)}</p></section>`;
  const shown = training.files.filter(f => !view.onlyFull || f.passed === f.total);
  const filter = `<label class="mhh-filter"><input type="checkbox" data-hub-history-full${view.onlyFull ? ' checked' : ''}> 只看全部通过的</label>`;
  const rest = shown.length - view.shown;
  return `<section class="mhh-part" aria-label="训练项目里的模型文件">${head(filter)}` +
    `<p class="mhh-sub">${h(training.dashboard)} · ${shown.length} 个评估过的模型文件，从新到旧</p>` +
    (shown.length ? `<ol class="mhh-list">${shown.slice(0, view.shown).map(f => hubHistoryFile(round, f)).join('')}</ol>` : '<p class="mhh-none">没有全部通过的模型文件。</p>') +
    (rest > 0 ? `<p class="mhh-more">${button(`再显示 ${Math.min(rest, HUB_HISTORY_PAGE)} 个`, 'hub-history-more', 'secondary small', round.id)}</p>` : '') + '</section>';
}

// The whole dialog body. `choices` is null while the versions are read.
function hubHistoryBody(data, model, round, choices, training, view) {
  const set = hubSet(data, round.set_id);
  const versions = !choices ? '<p class="mhh-wait" role="status">正在读取…</p>'
    : choices.versions.length ? `<ol class="mhh-list">${choices.versions.map(v => hubHistoryVersion(data, model, round, set, v)).join('')}</ol>`
      : '<p class="mhh-none">这个模型还没有别的版本。</p>';
  return `<div class="dialog-body mhh" data-hub-history="${h(round.id)}">` +
    // Stays in sight while the list scrolls: a refusal after a click far down the list is read where it was made.
    '<div class="mhh-notes"><div class="form-error" role="alert"></div><p class="mhh-status" role="status" aria-live="polite"></p></div>' +
    `<section class="mhh-part" aria-label="这个模型的版本"><div class="mhh-part-head"><h3>这个模型的版本</h3></div>${versions}</section>` +
    `${hubHistoryTraining(round, training, view)}</div>` +
    `<div class="dialog-footer">${button('取消', 'close', 'secondary')}${button('上传新模型文件', 'hub-version-add', '', `${model.id}|${round.id}`)}</div>`;
}

// ---------------------------------------------------------------- the dialog

// The dialog that is open: {model, round, choices, training, view, busy, error}. `busy` is the sentence of a
// choice under way (every row stays disabled until it is answered), `error` the last refusal. Both live
// here, not in the DOM: the dialog is drawn again when the training files arrive, which can be while a
// choice is under way.
let hubHistoryOpen = null;

const hubHistoryBox = o => ($('#dialog').open && o ? document.querySelector(`#dialog [data-hub-history="${CSS.escape(o.round.id)}"]`) : null);

// What is not in the markup: the choice under way and the last refusal.
function hubHistoryState(o) {
  const box = hubHistoryBox(o);
  if (!box) return;
  box.querySelector('.mhh-status').textContent = o.busy || '';
  if (o.busy) box.querySelectorAll('.mhh-pick').forEach(b => { b.disabled = true; });
  const error = box.querySelector('.form-error');
  error.textContent = '';
  if (o.error) { if (typeof formError === 'function') formError(error, o.error); else error.textContent = o.error.message; }
}

function hubHistoryDraw(first) {
  const o = hubHistoryOpen;
  if (!o) return;
  const body = hubHistoryBody(hubData, o.model, o.round, o.choices, o.training, o.view);
  if (first) {
    dialog('选择候选版本', `第 ${o.round.number} 轮 · 点一个，它就成为这一轮的候选版本`, body);
    $('#dialog').classList.add('wide');
    return;
  }
  const box = hubHistoryBox(o);
  if (!box) return;                                     // closed, or another dialog took its place
  const focused = box.contains(document.activeElement) ? document.activeElement.dataset.id || '' : '';
  const scrolled = [box.scrollTop, $('#dialog').scrollTop];
  const head = $('#dialog-content .dialog-head');
  while (head.nextSibling) head.nextSibling.remove();
  head.insertAdjacentHTML('afterend', body);
  [hubHistoryBox(o).scrollTop, $('#dialog').scrollTop] = scrolled;
  hubHistoryState(o);
  if (focused) document.querySelector(`#dialog .mhh-pick[data-id="${CSS.escape(focused)}"]`)?.focus();
}

async function hubHistoryShow(id) {
  const {model, round: roundId} = hubKey(id);
  const round = model ? model.rounds.find(r => r.id === roundId) : null;
  if (!round) {
    toast('它已经不在了，页面已更新');
    return hubLoad();
  }
  const open = {model, round, choices: null, training: model.show ? {loading: true} : null, view: {onlyFull: false, shown: HUB_HISTORY_PAGE}, busy: '', error: null};
  hubHistoryOpen = open;
  hubHistoryDraw(true);
  const versions = api('model-hub/rounds/history', {round_id: round.id}).then(found => { open.choices = found; }, err => { open.choices = {versions: []}; open.error = err; });
  const files = open.training ? api('model-hub/rounds/history/training', {round_id: round.id})
    .then(found => { open.training = found; }, err => { open.training = {available: false, problem: err.message, files: []}; }) : Promise.resolve();
  await versions;
  if (hubHistoryOpen === open) hubHistoryDraw();
  await files;
  if (hubHistoryOpen === open) hubHistoryDraw();
  return undefined;
}

async function hubHistoryPick(id) {
  const o = hubHistoryOpen, [roundId, kind, key] = String(id || '').split('|');
  if (!o || o.busy || !hubHistoryBox(o) || roundId !== o.round.id) return undefined;     // one choice at a time
  o.busy = kind === 't' ? `正在从训练看板取第 ${key.replace('-', ' 轮第 ')} 次更新的模型文件…` : '正在保存…';
  o.error = null;
  hubHistoryState(o);
  try {
    const round = await api('model-hub/rounds/candidate', {round_id: roundId, ...(kind === 't' ? {training_key: key} : {version_id: key})});
    if (hubHistoryOpen === o && hubHistoryBox(o)) $('#dialog').close();     // not a dialog that was opened meanwhile, even for this round
    if (hubHistoryOpen === o) hubHistoryOpen = null;
    toast(round.status === 'review' ? '候选版本已选好，它在这个评测集上已有评测' : '候选版本已选好');
    return hubLoad();
  } catch (err) {
    o.busy = '';
    o.error = err;
    if (hubHistoryOpen === o) hubHistoryDraw();         // the rows as they were, the refusal above them
    document.querySelector(`#dialog .mhh-pick[data-id="${CSS.escape(String(id))}"]`)?.focus();
    return undefined;
  }
}

function hubHistoryAction(type, id) {
  if (type === 'hub-history') return hubHistoryShow(id);
  if (type === 'hub-history-pick') return hubHistoryPick(id);
  if (type === 'hub-history-more' && hubHistoryOpen) {
    const before = hubHistoryOpen.view.shown;
    hubHistoryOpen.view = {...hubHistoryOpen.view, shown: before + HUB_HISTORY_PAGE};
    hubHistoryDraw();
    document.querySelectorAll('#dialog [aria-label="训练项目里的模型文件"] .mhh-pick')[before]?.focus();      // the first one just shown
  }
  return undefined;
}

function hubHistoryInstall() {
  if (typeof hubAction !== 'function') return;          // the model library is not on this page
  const base = hubAction;
  hubAction = async (type, id) => (HUB_HISTORY_ACTIONS.includes(type) ? hubHistoryAction(type, id) : base(type, id));
  document.addEventListener('change', event => {
    if (!event.target.matches?.('#dialog [data-hub-history-full]') || !hubHistoryOpen) return;
    hubHistoryOpen.view = {onlyFull: event.target.checked, shown: HUB_HISTORY_PAGE};
    hubHistoryDraw();
    document.querySelector('#dialog [data-hub-history-full]')?.focus();
  });
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', hubHistoryInstall);
