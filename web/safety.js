'use strict';
// ux05-fix (Q7-03 / Q7-05 / Q7-11): a robot that keeps moving after repeated stops is announced everywhere in
// the app -- a persistent banner under the top bar (and at the top of any open dialog), naming the robot, with
// a way to it -- for real robots as well as simulated ones. It clears when the robot is seen unlatched again
// (e-stop released on site). Also: the latch guard on the robot's detail page, and a warning when the
// runtime says the latch guard's calibration was loosened.
// Data: POST /api/robots/safety (brainos/safety_watch.py) -- read-only, bounded per robot on the server.
// ux05-fix2 (Q7b-10): robots of every project are watched (a robot of project B alerts while the user works in A),
// and polling never pauses while any robot is latched -- not even in a hidden window. With nothing latched, a
// hidden window still looks every SAFETY_HIDDEN_MS to notice a latch made elsewhere.

let safetyData = {robots: [], alerts: [], latched: [], relaxed: []};
let safetyBusy = false, safetyPending = '', safetyTimer = 0, safetyRelaxedKey = '', safetyLastAt = 0;
const SAFETY_HIDDEN_MS = 30000;

const safetyEntry = id => safetyData.robots.find(r => r.robot_id === id) || null;

// What to do now, by kind of robot (Q7-11: a simulated body has no hardware e-stop).
function safetyAdvice(e) {
  if (e.desktop_sim) return '仿真机器人：点“重启仿真机器人”或关闭仿真（重启后急停仍保持锁存）。';
  if (e.simulated) return '这是仿真本体：请关闭它的运行时进程。';
  if (e.hardware_estop === false) return '这台机器人声明没有硬件急停：请马上切断它的动力，并让所有人远离。';
  return '软件停止对它无效——请马上按下机器人上的硬件急停（或切断动力），并让所有人远离。';
}

function safetyHeadline(e) {
  return e.desktop_sim || e.simulated ? '仿真机器人在“停止”后仍在移动' : '请立即使用硬件急停';
}

function safetyWhen(ev) {
  return ev && typeof ev.ts === 'number' ? new Date(ev.ts * 1000).toLocaleTimeString('zh-CN', {hour12: false}) : '';
}

function safetyButtons(e, where) {
  if (e.desktop_sim) {
    return button('重启仿真机器人', 'sim-restart', where === 'banner' ? 'small' : '', e.robot_id) +
      button('关闭仿真', 'sim-stop', 'secondary small', e.robot_id) +
      (where === 'banner' ? button('实时状态', 'sim-live', 'secondary small', e.robot_id) : '');
  }
  if (where !== 'banner') return '';
  return safetyOtherProject(e) ? button('切换到该项目查看', 'safety-goto', 'small', e.robot_id)
    : button('查看这台机器人', 'detail-robots', 'small', e.robot_id);
}

// A robot of another project than the one in view (Q7b-10): say which project it belongs to.
const safetyOtherProject = e => Boolean(e && e.project_id && e.project_id !== projectId);
const safetyProjectNote = e => safetyOtherProject(e) ? `（项目：${e.project_name || find('projects', e.project_id)?.name || e.project_id}）` : '';

// One alerting robot, as a banner row.
function safetyAlertRow(e, where) {
  const ev = e.stop_not_obeyed || {}, when = safetyWhen(ev);
  const restops = typeof ev.restops === 'number' ? `已再停 ${ev.restops} 次` : '';
  const moved = typeof ev.displacement_m === 'number' ? `离停下时的位置约 ${Math.round(ev.displacement_m * 100)} 厘米` : '';
  const facts = [restops, moved, when ? '机器人报告时间 ' + when : '', e.reachable === false ? '现在读不到它的状态（提示保留到确认解除急停）' : '']
    .filter(Boolean).join(' · ');
  return `<div class="safety-alert${e.desktop_sim || e.simulated ? ' sim' : ''}" data-safety-alert="${h(e.robot_id)}">` +
    `<div class="safety-alert-text"><b>${h(safetyHeadline(e))}：${h(e.name)}${h(safetyProjectNote(e))}</b>` +
    `<p>机器人在“停止”后没有停下来：运行时已经多次发出停止，它仍在运动。${h(safetyAdvice(e))}</p>` +
    (facts ? `<small>${h(facts)}</small>` : '') + `</div>` +
    `<div class="button-row">${safetyButtons(e, where)}</div></div>`;
}

function safetyBannerHtml(where, except = '') {
  const alerts = safetyData.robots.filter(e => e.stop_not_obeyed && e.robot_id !== except);
  if (!alerts.length) return '';
  return `<section class="safety-banner" role="alert" data-safety-banner="${where}">${alerts.map(e => safetyAlertRow(e, where)).join('')}</section>`;
}

function safetyPaint() {
  const top = document.getElementById('safety-banner');
  const html = state.user ? safetyBannerHtml('banner') : '';
  if (top && top.dataset.html !== html) { top.dataset.html = html; top.innerHTML = html; }
  // A modal dialog makes the page underneath inert: repeat the banner at the top of the dialog.
  const dlg = $('#dialog');
  if (dlg) {
    let slot = dlg.querySelector(':scope > .safety-dialog-slot');
    if (!slot) { slot = document.createElement('div'); slot.className = 'safety-dialog-slot'; dlg.prepend(slot); }
    // The simulated robot's live view and a robot's detail page already lead with its own alert.
    const html2 = dlg.open ? safetyBannerHtml('dialog', dlg.dataset.simLive || dlg.dataset.robotDetail || '') : '';
    if (slot.dataset.html !== html2) { slot.dataset.html = html2; slot.innerHTML = html2; }
  }
  const box = document.getElementById('robot-safety');
  if (box && $('#dialog').open && box.dataset.robot && box.dataset.robot === $('#dialog').dataset.robotDetail) {
    const next = safetyDetailInner(box.dataset.robot);
    if (box.dataset.html !== next) { box.dataset.html = next; box.innerHTML = next; }
  }
  const key = safetyData.relaxed.join(',') + '|' + safetyData.latched.join(',') + '|' + safetyData.alerts.join(',');
  if (key !== safetyRelaxedKey) { safetyRelaxedKey = key; if (typeof render === 'function' && state.user) render(); }
}

async function safetyPoll(focus = '') {
  if (safetyBusy) { if (focus) safetyPending = focus; return; }      // one request at a time
  clearTimeout(safetyTimer);
  safetyBusy = true;
  let wait = 3000;
  try {
    if (!state.user) return;
    // Q7b-10: a hidden window keeps polling at the normal rate while anything is latched or alerting; when all
    // is quiet it still looks every SAFETY_HIDDEN_MS (the server bounds each robot's rate either way).
    const quiet = !safetyData.alerts.length && !safetyData.latched.length;
    if (document.hidden && quiet && Date.now() - safetyLastAt < SAFETY_HIDDEN_MS) return;
    safetyLastAt = Date.now();
    const next = await api('robots/safety', {project_id: projectId, all_projects: true, ...(focus ? {robot_id: focus} : {})});
    safetyData = {robots: next.robots || [], alerts: next.alerts || [], latched: next.latched || [], relaxed: next.relaxed || []};
    wait = Math.max(1500, Math.min(10000, (next.poll_s || 3) * 1000));
    safetyPaint();
  } catch { /* the banner keeps what it showed; a later poll answers */ }
  finally {
    safetyBusy = false;
    clearTimeout(safetyTimer);
    const again = safetyPending;
    safetyPending = '';
    safetyTimer = setTimeout(() => safetyPoll(again), again ? 0 : wait);
  }
}

// ---------------------------------------------------------------- robot detail: latch guard and calibration
function safetyRelaxedHtml(e) {
  const r = e?.relaxed;
  if (!r) return '';
  const names = safetyFieldNames;
  // Q7b-07: only the keys the runtime calls loosened (relaxed_keys), with the default they exceed.
  const fields = (r.fields || []).map(k => `${names[k] || k}（${k}${r.values && k in r.values ? ' = ' + r.values[k] : ''}` +
    `${r.defaults && k in r.defaults ? '，默认 ' + r.defaults[k] : ''}）`);
  const src = safetySourceNames;
  const sources = (r.sources || []).map(s => src[s] || s);
  return `<div class="latch-relaxed" data-latch-relaxed role="status"><b>锁存守护的标定被放宽了</b>` +
    `<p>急停期间，机器人要移动得比默认更多才会被发现并再次停止${fields.length ? '：' + h(fields.join('、')) : ''}。` +
    `${sources.length ? '来源：' + h(sources.join('、')) + '。' : ''}请确认这是现场标定的结果，而不是误配置；不确定时请联系适配器作者。</p>` +
    ((r.notes || []).length || r.message ? `<details class="tech-detail"><summary>技术细节</summary><code>${h([r.message, ...(r.notes || [])].filter(Boolean).join('\n'))}</code></details>` : '') +
    `</div>`;
}

const safetyFieldNames = {pos_m: '位置', yaw_rad: '偏航', yaw_total_rad: '偏航总量上限', joint_rad: '关节', noise_m: '位置噪声', noise_rad: '角度噪声',
  yaw_drift_rad_s: '偏航漂移', stop_settle_s: '停稳时间', settle_s: '停稳时间', escalate_after: '升级次数'};
const safetySourceNames = {manifest: '机器人清单', runtime: '运行时配置', adapter: '适配器'};

// Field calibration that is not the runtime's default (shown even when the runtime does not call it loosened).
// Q7b-07: listed apart from the "loosened" warning -- a stricter value is calibration, not a loosening.
function safetyCalibrationHtml(e) {
  const c = e?.calibration;
  if (!c) return '';
  const loosened = new Set(e.relaxed?.fields || []);
  const entries = Object.entries(c.fields || {}).filter(([k]) => !loosened.has(k));
  if (!entries.length) return '';
  const items = entries.map(([k, f]) =>
    `${safetyFieldNames[k] || k}（${k}${f.value !== null && f.value !== undefined ? ' = ' + f.value : ''}，来源：${safetySourceNames[f.source] || f.source}）`);
  return `<p class="inline-note latch-calibrated" data-latch-calibrated>${e.relaxed ? '其余现场标定（不比默认宽松）' : '锁存守护按现场标定运行'}：${h(items.join('、'))}。</p>`;
}

function safetyDetailInner(robotId) {
  const e = safetyEntry(robotId);
  if (!e) return `<p class="inline-note">正在读取急停与锁存守护状态…</p>`;
  const lm = e.latched_motion;
  const guard = !lm ? '<p class="inline-note">这台机器人的运行时没有报告锁存守护（急停期间是否仍在移动无法由软件发现）。</p>'
    : lm.enabled === false ? '<p class="inline-note">锁存守护未启用：机器人没有提供里程计或关节状态，急停期间软件看不到它是否在动。</p>'
    : e.latched ? simLatchGuardHtml(lm, e)
    : '<p class="latch-guard ok" data-latch-guard="armed">锁存守护已就绪：一旦急停锁存，它会盯着机器人是否仍在移动，并在需要时再次停止。</p>';
  const head = e.latched ? badge('急停已锁存', 'bad') : e.reachable === false ? badge('状态读不到', 'warn') : badge('急停未锁存', 'good');
  const alert = e.stop_not_obeyed ? safetyAlertRow(e, 'detail') : '';
  const reason = e.latched && e.reason ? `<p class="inline-note">锁存原因：${h(e.reason)}</p>` : '';
  // ux05-fix2 (Q7b-05/08): a reset still running on the robot, worded for what this runtime supports.
  const stuck = e.latched && e.reset_in_progress ? `<p class="reset-stuck" data-reset-stuck data-reset-expire="${e.reset?.reset_expire === true ? 'supported' : 'unsupported'}">` +
    `“解除急停”（复位）还在机器人上执行。${h(uxLateResetText(e.reset, false))}</p>` +
    `<div class="button-row">${button('停止（取消那次复位）', 'simlive-stop', 'danger small', e.robot_id)}</div>` : '';
  const reset = e.latched ? `<div class="button-row">${button('解除急停', 'robot-reset', 'secondary small', e.robot_id + (e.desktop_sim ? '::live' : ''))}</div>` : '';
  const when = e.checked_at ? new Date(e.checked_at * 1000).toLocaleTimeString('zh-CN', {hour12: false}) : '';
  return `<div class="section-head"><h3>急停与锁存守护</h3>${head}</div>` + alert + reason + stuck + guard + safetyRelaxedHtml(e) + safetyCalibrationHtml(e) + reset +
    (e.reachable === false ? `<p class="inline-note">读不到机器人状态：${h(e.error || '')}</p>` : '') +
    (when ? `<p class="inline-note">最近读取 ${h(when)}，每几秒更新</p>` : '');
}

function safetyDetailSection(robotId) {
  const inner = safetyDetailInner(robotId);
  return `<div class="contract-line robot-safety" id="robot-safety" data-robot="${h(robotId)}" data-html="${h(inner)}">${inner}</div>`;
}

// Badges for robot lists and cards.
function safetyBadges(robotId) {
  const e = safetyEntry(robotId);
  if (!e) return '';
  return (e.stop_not_obeyed ? ' ' + badge(e.desktop_sim || e.simulated ? '停止后仍在移动' : '停止后仍在移动 · 用硬件急停', 'bad') : '') +
    (e.relaxed ? ' ' + badge('锁存守护已放宽', 'warn') : '');
}

{
  // The banner lives between the top bar and the page; it survives every re-render of <main>.
  const shell = document.querySelector('.shell'), top = document.querySelector('.topbar');
  if (shell && top && !document.getElementById('safety-banner')) {
    const div = document.createElement('div');
    div.id = 'safety-banner';
    top.after(div);
  }
  safetyTimer = setTimeout(() => safetyPoll(), 800);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) safetyPoll(); });
  // Q7b-10: a robot of another project -- switch to its project, then open it.
  document.addEventListener('click', e => {
    const el = e.target.closest?.('[data-action="safety-goto"]');
    if (!el) return;
    e.preventDefault(); e.stopImmediatePropagation();
    const entry = safetyEntry(el.dataset.id);
    if (entry?.project_id && state.projects.some(p => p.id === entry.project_id)) {
      projectId = entry.project_id;
      try { sessionStorage.setItem('brain-project', projectId); } catch { /* per-tab convenience only */ }
      render();
    }
    action('detail-robots', el.dataset.id);
  }, true);
  // Re-check soon after anything that can change a latch (stop, reset, restart, start/close a sim).
  document.addEventListener('click', e => {
    const a = e.target.closest?.('[data-action]')?.dataset.action || '';
    if (/^(simlive-stop|sim-restart|sim-stop|sim-start|robot-connect)$/.test(a)) setTimeout(() => safetyPoll(), 1200);
  }, true);
  document.addEventListener('submit', () => setTimeout(() => safetyPoll(), 1500), true);
}

// Any dialog that opens shows the banner at its top right away (not only at the next poll).
if (typeof dialog === 'function') {
  const safetyDialogOrig = dialog;
  dialog = function (...args) { const out = safetyDialogOrig.apply(this, args); try { safetyPaint(); } catch { /* cosmetic */ } return out; };
}
