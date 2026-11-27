'use strict';
// Issue #63: the Microduck AD-P102's skill models and their training, on 「训练与改进」.
//
// What there is comes with the state (`state.duck_kit`, brainos/duck_kit.py): the skill models, which of
// them this installation has a package of, and whether a simulated duck can be added. What changes while
// one watches (a show's phase, the picture, the training figures) is asked for by this file and drawn in
// place: the block is one element that is kept and put back after every redraw of the page, so a playing
// video or a running show is not thrown away when the rest of the page is drawn again.

const DUCK_ROUTE = 'training';
const DUCK_STATUS_MS = 300;
const DUCK_IDLE_MS = 2000;
const DUCK_TRAINING_MS = 15000;
const DUCK_ENDED = ['succeeded', 'failed', 'stopped', 'error'];
const DUCK_REASONS = {
  complete: '完整演完', completed: '完整演完', timeout: '超时，没有按要求收尾', time_limit: '超时，没有按要求收尾',
  stopped: '已停止', replaced: '被新的一次运行替换', simulation_error: '仿真出错，已复位',
  nonfoot_ground_contact: '脚以外的部位触地', beak_ground_contact: '喙触地',
  nonright_contact_before_scored_kick: '射门前球被别的部位碰到',
};

// ---------------------------------------------------------------- pure
const duckEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

function duckKit(source) {
  const raw = source || (typeof state === 'object' && state && state.duck_kit) || {};
  return {...raw, shows: Array.isArray(raw.shows) ? raw.shows : [], available: raw.available === true,
    problem: String(raw.problem || ''), engine: raw.engine || {}};
}
const duckScore = pair => Array.isArray(pair) && pair.length === 2 ? `${pair[0]}/${pair[1]}` : '—';
const duckSteps = n => typeof n === 'number' ? n.toLocaleString('en-US') : '—';
function duckOrigin(origin) {
  return origin ? `第 ${origin.round} 轮第 ${origin.update} 次更新 · ${duckSteps(origin.steps)} 步 · ${origin.device}` : '';
}
// The simulated ducks among the robots of the state (the state names a simulated robot's body, not its profile).
const DUCK_ADAPTER = 'brainos.duck_body:body';
const duckRobots = robots => (Array.isArray(robots) ? robots : []).filter(r => r && r.sim && r.sim.adapter === DUCK_ADAPTER);
const duckReason = reason => DUCK_REASONS[reason] || String(reason || '');

// What the stage says about a run. `asked`: the run this page asked for and has not seen answered yet.
function duckRunLine(status, asked, shows) {
  const title = id => (shows.find(s => s.id === id) || {}).title || id;
  if (!status) return {text: '还没有连接到这个仿真小鸭', tone: ''};
  if (status.error && (status.state === 'error' || !status.run_id)) return {text: status.error, tone: 'bad'};
  if (asked && status.run_id !== asked.run_id) return {text: `正在准备「${title(asked.show)}」…`, tone: 'busy'};
  if (!status.run_id || status.state === 'idle') return {text: status.running ? '站在起点，可以开始' : '这个仿真小鸭没有在运行', tone: ''};
  const name = `「${title(status.show)}」`;
  if (status.state === 'starting') return {text: `正在准备${name}…`, tone: 'busy'};
  if (status.state === 'running') return {text: `${name}${status.phase ? ' · ' + status.phase : ''}`, tone: 'busy'};
  const result = status.result || {};
  const took = typeof result.duration_s === 'number' ? `，用时 ${result.duration_s.toFixed(2)} 秒` : '';
  if (status.state === 'succeeded') return {text: `${name}完整演完${took}`, tone: 'good'};
  if (status.state === 'stopped') return {text: `${name}已停止`, tone: ''};
  return {text: `${name}没有成功：${duckReason(result.reason) || status.error || '原因没有记录'}${took}`, tone: 'bad'};
}

// The evaluations of one show as the points of a line: x by order, y by passes out of the episodes.
function duckSpark(series, width, height) {
  const rows = (Array.isArray(series) ? series : []).filter(r => Array.isArray(r) && typeof r[2] === 'number' && r[3] > 0);
  if (!rows.length) return '';
  const step = rows.length > 1 ? (width - 4) / (rows.length - 1) : 0;
  return rows.map((r, i) => `${(2 + i * step).toFixed(1)},${(height - 2 - (height - 4) * Math.min(1, r[2] / r[3])).toFixed(1)}`).join(' ');
}
function duckFigure(figure) {
  if (!figure || typeof figure.successes !== 'number') return {score: '—', where: ''};
  return {score: `${Number(figure.successes)}/${Number(figure.episodes)}`, where: `第 ${figure.round} 轮第 ${figure.update} 次${figure.device ? ' · ' + figure.device : ''}`};
}
function duckAgo(seconds, now) {
  if (typeof seconds !== 'number') return '';
  const d = Math.max(0, now - seconds);
  return d < 90 ? '刚刚' : d < 3600 ? `${Math.round(d / 60)} 分钟前` : d < 86400 ? `${Math.round(d / 3600)} 小时前` : `${Math.round(d / 86400)} 天前`;
}
function duckTrainingSource(training, now) {
  if (!training || training.source === 'none') return training && training.checking ? '正在询问本机的训练看板…' : '这个安装里没有训练记录，本机的训练看板也没有应答';
  const when = duckAgo(training.last_update || training.generated_at, now);
  if (training.source === 'live') return `来自本机的训练看板${when ? '，训练文件最后变更于' + when : ''}`;
  return `安装包里的记录${when ? '，训练文件最后变更于' + when : ''}；本机的训练看板没有应答`;
}

// ---------------------------------------------------------------- drawing
const duckState = {root: null, robot: '', status: null, asked: null, replay: '', training: null, timer: 0, trainingTimer: 0,
  drawn: {}, busy: false, frame: 0};

function duckShowRow(show, canRun, performing) {
  const a = show.acceptance || {};
  const chip = `<span class="duck-chip duck-${duckEsc(show.status)}">${duckEsc(show.status_name)}</span>`;
  const scores = show.acceptance ? `<span class="duck-score"><b>${duckScore(a.fixed)}</b> 固定组</span><span class="duck-score"><b>${duckScore(a.independent)}</b> 独立组</span>` : '';
  const buttons = show.packaged
    ? `<button type="button" class="button small" data-duck-run="${duckEsc(show.id)}" ${canRun && !performing ? '' : 'disabled'}>运行</button>`
      + `<button type="button" class="button small secondary" data-duck-replay="${duckEsc(show.id)}">验收录像</button>`
    : '';
  return `<li class="duck-show" data-duck-show="${duckEsc(show.id)}"><div class="duck-show-main"><h3>${duckEsc(show.title)} ${chip}</h3>`
    + `<p>${duckEsc(show.summary)}</p><div class="duck-show-facts">${scores}<span>${duckEsc(duckOrigin(show.origin))}</span></div>`
    + (show.note ? `<p class="duck-note">${duckEsc(show.note)}</p>` : '')
    + (show.status !== 'training' && !show.packaged ? '<p class="duck-note">这个安装里没有它的模型包。</p>' : '')
    + `</div><div class="duck-show-actions">${buttons}</div></li>`;
}

function duckHead(kit) {
  const count = kit.shows.filter(s => s.packaged).length;
  return `<header class="duck-head"><img src="/robot_images/microduck-adp102.webp" alt="" width="84" height="84">`
    + `<div><span class="duck-eyebrow">技能模型</span><h2>${duckEsc(kit.vendor || 'Microduck')} · ${duckEsc(kit.model || 'AD-P102')}</h2>`
    + `<p>${count} 个模型包 · 在 MuJoCo ${duckEsc(kit.engine.mujoco || '')} 上验收和运行 · 仿真结果，不是现场证据</p></div></header>`;
}

function duckStage(kit, ducks) {
  const options = ducks.map(r => `<option value="${duckEsc(r.id)}" ${r.id === duckState.robot ? 'selected' : ''}>${duckEsc(r.name)}</option>`).join('');
  const pick = ducks.length > 1 ? `<select class="duck-pick" aria-label="仿真小鸭">${options}</select>` : '';
  const empty = !ducks.length
    ? `<div class="duck-empty"><b>还没有仿真小鸭</b>${kit.available ? '添加一个，技能模型就在它身上运行。' : duckEsc(kit.problem)}`
      + (kit.available ? '<div><button type="button" class="button" data-duck-add>添加仿真小鸭</button></div>' : '') + '</div>'
    : '';
  return `<div class="duck-stage"><div class="duck-screen">${empty}<img class="duck-frame" alt="仿真小鸭的实时画面" hidden>`
    + `<video class="duck-video" controls playsinline hidden></video></div>`
    + `<div class="duck-bar"><div class="duck-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i></i></div>`
    + `<p class="duck-line" role="status"></p><div class="duck-bar-actions">${pick}`
    + `<button type="button" class="button small secondary" data-duck-live hidden>回到实时画面</button>`
    + `<button type="button" class="button small danger" data-duck-stop hidden>停止</button>`
    + `<button type="button" class="button small secondary" data-duck-home hidden>回到起点</button></div></div></div>`;
}

function duckTrainingBlock(training, now) {
  const tasks = training && Array.isArray(training.tasks) ? training.tasks : [];
  const rows = tasks.map(t => {
    const best = duckFigure(t.best), latest = duckFigure(t.latest), points = duckSpark(t.series, 160, 36);
    return `<tr><td><span class="cell-main">${duckEsc(t.title)}</span><span class="cell-sub">${duckEsc(t.id)}${t.active ? '' : ' · 历史版本'}</span></td>`
      + `<td><b class="duck-big">${duckEsc(best.score)}</b><span class="cell-sub">${duckEsc(best.where)}</span></td>`
      + `<td><b class="duck-big">${duckEsc(latest.score)}</b><span class="cell-sub">${duckEsc(latest.where)}</span></td>`
      + `<td>${Number(t.evaluations) || 0} 次</td><td>${points ? `<svg class="duck-spark" viewBox="0 0 160 36" role="img" aria-label="${duckEsc(t.title)}历次评估的通过数"><polyline points="${points}"/></svg>` : '—'}</td></tr>`;
  }).join('');
  const link = training && training.dashboard
    ? `<a class="button small secondary" href="${duckEsc(training.dashboard)}/" target="_blank" rel="noopener noreferrer">打开完整训练看板</a>` : '';
  return `<div class="duck-training-head"><div><h3>训练进展</h3><p>${duckEsc(duckTrainingSource(training, now))}`
    + `${training && training.rounds ? ` · 共 ${training.rounds} 轮` : ''} · 每次评估是固定十个起姿的整段通过数</p></div>${link}</div>`
    + (rows ? `<div class="table-wrap"><table class="duck-table"><thead><tr><th>节目</th><th>历史最佳</th><th>最新评估</th><th>评估</th><th>走势</th></tr></thead><tbody>${rows}</tbody></table></div>` : '');
}

function duckPart(name, html) {
  // A part is replaced only when what it shows changed: the picture, a video and a pressed button stay.
  const el = duckState.root.querySelector(`[data-duck-part="${name}"]`);
  if (el && duckState.drawn[name] !== html) { el.innerHTML = html; duckState.drawn[name] = html; return true; }
  return false;
}

function duckDraw() {
  if (!duckState.root) return;
  const kit = duckKit(), ducks = duckRobots(typeof state === 'object' ? state.robots : []);
  if (!ducks.some(r => r.id === duckState.robot)) { duckState.robot = ducks.length ? ducks[0].id : ''; duckState.status = null; }
  duckPart('head', duckHead(kit));
  if (duckPart('stage', duckStage(kit, ducks))) {
    duckState.frame = 0;
    // The stage was drawn again (a duck was added or renamed): a replay that was showing is put back.
    if (duckState.replay) duckState.root.querySelector('.duck-video').src = `/api/duck/replay?show=${encodeURIComponent(duckState.replay)}`;
  }
  const status = duckState.status, asked = duckState.asked;
  const performing = Boolean(asked) || Boolean(status && ['starting', 'running'].includes(status.state));
  const canRun = Boolean(duckState.robot) && Boolean(status && status.running);
  duckPart('shows', `<ul class="duck-shows">${kit.shows.map(s => duckShowRow(s, canRun, performing)).join('')}</ul>`);
  duckPart('training', duckTrainingBlock(duckState.training, Date.now() / 1000));
  duckLive();
}

function duckLive() {
  const root = duckState.root, kit = duckKit(), status = duckState.status;
  const line = root.querySelector('.duck-line');
  if (!line) return;
  const said = duckState.robot ? duckRunLine(status, duckState.asked, kit.shows) : {text: '', tone: ''};
  if (line.textContent !== said.text) line.textContent = said.text;
  line.dataset.tone = said.tone;
  const performing = Boolean(duckState.asked) || Boolean(status && ['starting', 'running'].includes(status.state));
  const progress = performing && status && !duckState.asked ? Math.round((status.progress || 0) * 100) : status && status.state === 'succeeded' ? 100 : 0;
  const bar = root.querySelector('.duck-progress');
  bar.setAttribute('aria-valuenow', String(progress));
  bar.firstElementChild.style.transform = `scaleX(${progress / 100})`;
  root.querySelector('[data-duck-stop]').hidden = !performing;
  root.querySelector('[data-duck-home]').hidden = performing || !(status && DUCK_ENDED.includes(status.state) && status.state !== 'succeeded');
  root.querySelector('[data-duck-live]').hidden = !duckState.replay;
  const video = root.querySelector('.duck-video'), frame = root.querySelector('.duck-frame');
  video.hidden = !duckState.replay;
  frame.hidden = Boolean(duckState.replay) || !duckState.robot || !(status && status.has_frame);
  if (!frame.hidden && !duckState.replay) {
    // One request at a time: the next picture is asked for when this one has arrived.
    if (!duckState.frameBusy) {
      duckState.frameBusy = true;
      const next = new Image();
      const done = () => { duckState.frameBusy = false; };
      next.onload = () => { frame.src = next.src; done(); };
      next.onerror = done;
      next.src = `/api/sims/frame?id=${encodeURIComponent(duckState.robot)}&t=${++duckState.frame}`;
    }
  }
}

// ---------------------------------------------------------------- asking
async function duckPoll() {
  clearTimeout(duckState.timer);
  const visible = duckState.root && duckState.root.isConnected && !document.hidden;
  if (visible && duckState.robot && !duckState.busy) {
    duckState.busy = true;
    const asked = duckState.robot;
    try {
      const status = await api('sims/duck/status', {robot_id: asked});
      if (asked === duckState.robot) {                // not when another duck was chosen meanwhile
        duckState.status = status;
        if (duckState.asked && status.run_id === duckState.asked.run_id) duckState.asked = null;
      }
    } catch (error) {
      if (asked === duckState.robot) {
        duckState.status = {running: false, state: 'error', run_id: '', error: error.message || '读取状态失败'};
        duckState.asked = null;
      }
    }
    duckState.busy = false;
    duckDraw();
  }
  const status = duckState.status;
  const lively = duckState.asked || (status && ['starting', 'running'].includes(status.state));
  duckState.timer = setTimeout(duckPoll, visible && lively ? DUCK_STATUS_MS : DUCK_IDLE_MS);
}

async function duckTrainingPoll() {
  clearTimeout(duckState.trainingTimer);
  if (duckState.root && duckState.root.isConnected && !document.hidden) {
    try {
      duckState.training = await api('duck/training', {});
      duckDraw();
    } catch { /* the block keeps what it had */ }
  }
  const soon = duckState.training && duckState.training.checking;
  duckState.trainingTimer = setTimeout(duckTrainingPoll, soon ? 2500 : DUCK_TRAINING_MS);
}

async function duckClick(event) {
  const target = event.target.closest('button');
  if (!target || !duckState.root.contains(target)) return;
  try {
    if (target.dataset.duckRun) {
      duckState.replay = '';
      duckState.asked = {run_id: '', show: target.dataset.duckRun};
      duckDraw();
      duckState.asked = await api('sims/duck/perform', {robot_id: duckState.robot, show: target.dataset.duckRun});
      duckPoll();
    } else if (target.dataset.duckReplay) {
      duckState.replay = target.dataset.duckReplay;
      const video = duckState.root.querySelector('.duck-video');
      video.src = `/api/duck/replay?show=${encodeURIComponent(duckState.replay)}`;
      duckDraw();
      video.play().catch(() => { /* the person presses play */ });
    } else if ('duckLive' in target.dataset) {
      duckState.replay = '';
      const video = duckState.root.querySelector('.duck-video');
      video.pause();
      video.removeAttribute('src');
      duckDraw();
    } else if ('duckStop' in target.dataset || 'duckHome' in target.dataset) {
      duckState.asked = null;
      duckState.status = await api('sims/duck/stop', {robot_id: duckState.robot, home: 'duckHome' in target.dataset});
      duckDraw();
    } else if ('duckAdd' in target.dataset) {
      target.disabled = true;
      toast('正在启动仿真小鸭…');
      const robot = await api('sims', {profile_id: duckKit().profile_id, project_id: projectId || undefined});
      duckState.robot = robot.id;
      await refresh(true);
      toast('仿真小鸭已启动并连接');
      duckPoll();
    }
  } catch (error) {
    duckState.asked = null;
    target.disabled = false;
    toast(error.message || '没有完成');
    duckDraw();
  }
}

function duckRoot() {
  if (duckState.root) return duckState.root;
  const root = document.createElement('section');
  root.className = 'duck';
  root.setAttribute('aria-label', 'Microduck AD-P102 的技能模型');
  root.innerHTML = '<div data-duck-part="head"></div><div class="duck-body"><div data-duck-part="stage"></div><div data-duck-part="shows"></div></div>'
    + '<div class="duck-training" data-duck-part="training"></div>';
  root.addEventListener('click', duckClick);
  // A video that is taken out of the page and put back (a redraw) stops; it goes on if it was playing.
  root.addEventListener('playing', () => { duckState.playing = true; }, true);
  root.addEventListener('pause', event => { if (event.target.isConnected) duckState.playing = false; }, true);
  root.addEventListener('ended', () => { duckState.playing = false; }, true);
  root.addEventListener('change', event => {
    if (event.target.matches('.duck-pick')) { duckState.robot = event.target.value; duckState.status = null; duckState.asked = null; duckPoll(); }
  });
  duckState.root = root;
  return root;
}

// Put the kept block back into its place after the page was drawn again.
function duckMount() {
  const slot = document.querySelector('[data-duck-slot]');
  if (!slot) return;
  const root = duckRoot();
  if (root.parentNode !== slot) {
    slot.replaceChildren(root);
    duckDraw();
    if (duckState.replay && duckState.playing) root.querySelector('.duck-video').play().catch(() => {});
    duckPoll();
    duckTrainingPoll();
  } else {
    duckDraw();
  }
}

if (typeof views === 'object' && typeof document === 'object' && document.querySelector) {
  const base = views[DUCK_ROUTE];
  views[DUCK_ROUTE] = () => {
    const html = base();
    const kit = duckKit();
    if (!state.user || !kit.shows.length) return html;
    return uxInsertAfterHead(html, '<div data-duck-slot></div>');
  };
  new MutationObserver(duckMount).observe(document.getElementById('main'), {childList: true});
  document.addEventListener('visibilitychange', () => { if (!document.hidden && duckState.root && duckState.root.isConnected) { duckPoll(); duckTrainingPoll(); } });
  if (location.hash.slice(1) === DUCK_ROUTE && typeof route === 'function') route();
}
