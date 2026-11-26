'use strict';
// Issue #51: simulation environments for a robot application, run with MuJoCo.
//   · 应用交付清单 gets a 「仿真环境」 entry. It opens a dialog for that application: the robot model of its
//     brand and model (from the library), the environments made for it (by the engineering agent, or an
//     empty floor), each with a preview and what is in it, to be confirmed by a person; a confirmed
//     environment starts a simulated robot of that brand and model.
//   · 实时状态 of a MuJoCo robot shows the picture of the simulation and what the robot touches.
// Loaded after delivery.js, sim.js and robot_model.js; it wraps deliveryOpen and simLiveOpen and listens
// for its own data-mj-* attributes. Rules: brainos/mujoco_sim.py, docs/MUJOCO_SIM.md.

const MJ_STATES = {preparing: ['正在取机器人模型', 'blue'], generating: ['工程助手正在写环境', 'blue'], checking: ['正在检查并生成预览', 'blue'],
  awaiting_review: ['等你确认', 'warn'], confirmed: ['已确认', 'good'], rejected: ['不用', ''], failed: ['没有做成', 'bad']};
const MJ_BUSY_TEXT = {preparing: '正在从资料库取机器人模型并解开；模型大时要等一会儿。',
  generating: '工程助手正在按应用的步骤写环境描述，一般一两分钟。它可能会请你批准一条本地命令：点“查看过程”。',
  checking: '正在用 MuJoCo 试着建立这个环境，并生成预览。'};
const MJ_SOURCES = {agent: '工程助手生成', empty: '空场地'};
const MJ_POLL_MS = 2000, MJ_FRAME_MS = 500, MJ_VIEW_EVERY = 3;
let mjView = null, mjShown = '', mjBusy = false, mjFormOpen = false, mjTicks = 0, mjFrameBusy = false;

// ---------------------------------------------------------------- pure
const mjBusyScene = view => Boolean(view && (view.scenes || []).some(s => s.busy));
const mjRobotsOf = (view, sceneId) => (view.robots || []).filter(r => r.scene_id === sceneId);

// What the buttons of one environment are, by its state.
function mjSceneActions(scene, robots) {
  if (scene.busy) return scene.session_id ? ['session'] : [];
  if (scene.status === 'awaiting_review') return ['confirm', 'reject', 'remove'];
  if (scene.status === 'confirmed') return robots.length ? ['start'] : ['start', 'reject', 'remove'];
  if (scene.status === 'rejected') return ['confirm', 'remove'];
  return scene.session_id ? ['session', 'remove'] : ['remove'];
}

function mjTouchText(view) {
  if (view.fault) return '仿真出错，已停止推进：' + view.fault;
  const touching = view.touching || [];
  return (touching.length ? '正碰到：' + touching.join('、') : '没有碰到环境里的东西') +
    (typeof view.sim_time_s === 'number' ? ` · 仿真时间 ${Math.round(view.sim_time_s)} 秒` : '') +
    (view.render_error ? ' · ' + view.render_error : '');
}

// ---------------------------------------------------------------- markup
const mjButton = (label, type, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-mj-action="${type}" data-id="${h(id)}" ${disabled ? 'disabled' : ''}>${label}</button>`;

function mjSceneCard(scene, view) {
  const [name, tone] = MJ_STATES[scene.status] || [scene.status, ''];
  const robots = mjRobotsOf(view, scene.id);
  const labels = {confirm: ['确认可用', ''], reject: ['不用这个', 'secondary'], remove: ['删除', 'secondary'],
    start: ['启动仿真机器人', ''], session: ['查看过程', 'secondary']};
  const buttons = mjSceneActions(scene, robots).map(type => mjButton(labels[type][0], type, labels[type][1] + ' small',
    type === 'session' ? scene.session_id : scene.id)).join('');
  const picture = scene.preview ? `<img class="mj-preview" alt="环境预览" width="640" height="480" loading="lazy" ` +
    `src="/api/sim/mujoco/preview?id=${encodeURIComponent(scene.id)}&t=${encodeURIComponent(scene.updated_at || '')}">` : '';
  const lines = (scene.lines || []).length ? `<ul class="mj-lines">${scene.lines.map(line => `<li>${h(line)}</li>`).join('')}</ul>` : '';
  const running = robots.map(r => `<div class="mj-robot"><b>${h(r.name)}</b>${badge((simStateNames[r.state] || [r.state])[0], (simStateNames[r.state] || [])[1] || '')}` +
    `${r.state === 'running' ? button('实时画面', 'sim-live', 'secondary small', r.id) : button('去我的机器人', 'go-robots', 'secondary small')}</div>`).join('');
  return `<article class="mj-scene" data-mj-scene="${h(scene.id)}" data-mj-status="${h(scene.status)}"><div class="mj-scene-head">` +
    `${badge(name, tone)}<span class="cell-sub">${h(MJ_SOURCES[scene.source] || scene.source)} · ${date(scene.created_at)} · ` +
    `机器人模型 ${h(scene.material?.filename || '')}（版本 ${h(scene.material?.version || '')}）</span></div>` +
    (scene.busy ? `<p class="mj-busy" aria-live="polite">${h(MJ_BUSY_TEXT[scene.status] || '')}</p>` : '') +
    (scene.error ? notice(h(scene.error), true) : '') + (scene.notes || []).map(note => notice(h(note), true)).join('') +
    (scene.scene?.summary ? `<p class="mj-summary">${h(scene.scene.summary)}</p>` : '') +
    (picture || lines ? `<div class="mj-scene-body">${picture}${lines}</div>` : '') +
    (scene.render_error && !scene.preview ? `<p class="inline-note">这台电脑上没能画出预览（${h(scene.render_error)}）；环境本身检查通过了。</p>` : '') +
    (scene.status === 'awaiting_review' ? '<p class="inline-note">请对照应用的步骤看一遍：地点、障碍和要操作的物体对不对。确认后才能用它启动仿真机器人。</p>' : '') +
    running + `<div class="button-row">${buttons}</div></article>`;
}

function mjGenerateForm(appId) {
  return `<form class="mj-form" data-mj-form="generate" data-id="${h(appId)}"><div class="form-error" role="alert"></div>` +
    field('补充说明（可选）', '<textarea name="note" maxlength="2000" placeholder="例如：货架靠北墙，门口在西侧，地上有一个要推到墙边的纸箱"></textarea>',
      '工程助手会读这个应用的步骤、任务说明和这个品牌型号已确认的资料。') +
    '<label class="mj-consent"><input type="checkbox" name="data_authorized" required> 同意把这个应用的步骤、任务说明和这个品牌型号已确认的资料交给工程助手和所选模型，用来生成仿真环境</label>' +
    `<div class="button-row">${mjButton('取消', 'form-close', 'secondary small', appId)}<button type="submit" class="button small">开始生成</button></div></form>`;
}

function mjBody(view) {
  const app = view.application, product = app.product;
  const model = view.robot_model;
  const head = `<div class="detail-meta mj-meta"><div><b>品牌型号</b>${product ? h(product.vendor + ' ' + product.model) : '未填写'}</div>` +
    `<div><b>本体类型</b>${view.family ? h((state.families || familyNames)[view.family] || view.family) : '—'}</div>` +
    `<div><b>机器人模型</b>${model ? h(model.filename) + '（版本 ' + h(model.version) + '）' : '还没有'}</div></div>`;
  const problem = view.problem ? notice(h(view.problem.message) + (['sim_no_robot_model', 'cloud_login_required'].includes(view.problem.code)
    ? ' ' + mjButton('去资料库', 'library', 'secondary small') : ''), true) : '';
  const busy = mjBusyScene(view);
  const make = view.problem ? '' : `<div class="button-row mj-make">${mjButton('让工程助手生成环境', 'form-open', 'small', app.id, busy || mjFormOpen)}` +
    `${mjButton('用空场地', 'empty', 'secondary small', app.id, busy)}</div>` + (mjFormOpen ? mjGenerateForm(app.id) : '');
  const scenes = (view.scenes || []).length ? view.scenes.map(scene => mjSceneCard(scene, view)).join('')
    : (view.problem ? '' : empty('还没有仿真环境', '让工程助手按这个应用的步骤生成一个，或者先用只有地面的空场地。'));
  return head + problem + make + `<div class="mj-scenes">${scenes}</div>` +
    '<p class="inline-note">仿真里机器人按速度指令在地面上平移和转向，不模拟腿的步态；关节和夹爪按物理运动，会被固定的物体挡住、会推动可移动的物体。' +
    '仿真结果只说明逻辑是否正确，不能作为现场证据。</p>';
}

// ---------------------------------------------------------------- the dialog
function mjDraw(force = false) {
  const el = $('#mj-body');
  if (!el || !mjView) return;
  const html = mjBody(mjView);
  if (html === mjShown && !force) return;
  const note = el.querySelector('textarea[name=note]')?.value, agreed = el.querySelector('input[name=data_authorized]')?.checked;
  mjShown = html;
  el.innerHTML = html;
  if (note !== undefined && el.querySelector('textarea[name=note]')) el.querySelector('textarea[name=note]').value = note;
  if (agreed && el.querySelector('input[name=data_authorized]')) el.querySelector('input[name=data_authorized]').checked = true;
}

async function mjLoad(appId) {
  if (mjBusy) return;
  mjBusy = true;
  try {
    const view = await api('sim/mujoco/overview', {project_id: projectId, application_id: appId});
    if (!$('#dialog').open || $('#dialog').dataset.mjApp !== appId) return;
    mjView = view;
    mjDraw();
  } catch (err) {
    const el = $('#mj-body');
    if (el && !mjView) el.innerHTML = notice(h(err.message), true);
  } finally {
    mjBusy = false;
  }
}

function mjOpen(appId) {
  const app = find('applications', appId);
  dialog('仿真环境', h(app?.name || ''), '<div class="dialog-body" id="mj-body"><p class="subtitle">正在读取…</p></div>' +
    `<div class="dialog-footer">${button('返回交付清单', 'deliver-open', 'secondary', appId)}${button('关闭', 'close', 'secondary')}</div>`);
  $('#dialog').classList.add('wide');
  $('#dialog').dataset.mjApp = appId;
  mjView = null; mjShown = ''; mjFormOpen = false;
  return mjLoad(appId);
}

async function mjAction(type, id) {
  const appId = $('#dialog').dataset.mjApp;
  if (type === 'open') return mjOpen(id);
  if (type === 'library') { $('#dialog').close(); location.hash = '#library'; return; }
  if (type === 'form-open' || type === 'form-close') { mjFormOpen = type === 'form-open'; return mjDraw(true); }
  if (type === 'session') return engineAction('engine-session', id);
  if (type === 'empty') await api('sim/mujoco/empty', {project_id: projectId, application_id: id});
  if (type === 'confirm' || type === 'reject') await api('sim/mujoco/review', {project_id: projectId, scene_id: id, decision: type});
  if (type === 'remove') await api('sim/mujoco/remove', {project_id: projectId, scene_id: id});
  if (type === 'start') {
    const el = document.querySelector(`[data-mj-scene="${CSS.escape(id)}"] [data-mj-action="start"]`);
    if (el) { el.disabled = true; el.textContent = '正在启动…'; }
    const robot = await api('sims/mujoco', {project_id: projectId, scene_id: id});
    await refresh(true);
    toast('仿真机器人已启动');
    return simLiveOpen(robot.id);
  }
  if (appId) return mjLoad(appId);
}

async function mjSubmit(form) {
  const error = form.querySelector('.form-error'), data = new FormData(form);
  try {
    await api('sim/mujoco/generate', {project_id: projectId, application_id: form.dataset.id, note: String(data.get('note') || '').trim(),
      data_authorized: data.get('data_authorized') === 'on'});
  } catch (err) {
    error.textContent = err.message;
    return;
  }
  mjFormOpen = false;
  mjShown = '';
  return mjLoad(form.dataset.id);
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-mj-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  mjAction(el.dataset.mjAction, el.dataset.id).catch(err => {
    toast(err.message);
    mjShown = '';                         // a button this click changed (正在启动…) is drawn anew
    if ($('#dialog').dataset.mjApp) mjLoad($('#dialog').dataset.mjApp);
  });
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-mj-form]');
  if (!form) return;
  e.preventDefault();
  mjSubmit(form);
});

// While an environment is being made its card follows the work; while a MuJoCo robot is watched its picture does.
setInterval(() => {
  const open = $('#dialog').open, appId = open ? $('#dialog').dataset.mjApp : '';
  if (appId && mjBusyScene(mjView)) mjLoad(appId);
}, MJ_POLL_MS);

function mjFrameTick() {
  const img = $('#mj-frame'), robotId = $('#dialog').open ? $('#dialog').dataset.simLive : '';
  if (!img || !robotId || img.dataset.robot !== robotId) return;
  mjTicks += 1;
  if (!mjFrameBusy) {
    mjFrameBusy = true;
    const next = new Image();
    next.onload = () => { mjFrameBusy = false; if ($('#mj-frame') === img) { img.src = next.src; img.hidden = false; } };
    next.onerror = () => { mjFrameBusy = false; };
    next.src = `/api/sims/frame?id=${encodeURIComponent(robotId)}&t=${Date.now()}`;
  }
  if (mjTicks % MJ_VIEW_EVERY === 0) {
    api('sims/view', {project_id: projectId, robot_id: robotId}).then(view => {
      const el = $('#mj-touch');
      if (el && $('#mj-frame')?.dataset.robot === robotId) el.textContent = mjTouchText(view);
    }).catch(() => {});
  }
}
setInterval(mjFrameTick, MJ_FRAME_MS);

// ---------------------------------------------------------------- wrapping what is there
{
  const baseLive = simLiveOpen, baseKind = simKindLabel, baseDialog = dialog;
  // Any other dialog ends this one: without this its timer would keep asking about the application.
  dialog = (...args) => { delete $('#dialog').dataset.mjApp; return baseDialog(...args); };
  simKindLabel = r => (r.sim?.kind === 'mujoco' ? 'MuJoCo 仿真' + (r.product ? ' · ' + r.product.vendor + ' ' + r.product.model : '') : baseKind(r));
  simLiveOpen = robotId => {
    const out = baseLive(robotId), robot = find('robots', robotId);
    if (robot?.sim?.kind === 'mujoco' && $('#sim-live') && !$('#mj-frame')) {
      const box = document.createElement('div');
      box.className = 'mj-view';
      box.innerHTML = `<img id="mj-frame" data-robot="${h(robotId)}" alt="仿真画面" width="480" height="360" hidden>` +
        '<p id="mj-touch" class="inline-note" aria-live="polite">正在等待画面…</p>';
      $('#sim-live').before(box);
    }
    return out;
  };
  if (typeof deliveryOpen === 'function') {
    const baseOpen = deliveryOpen;
    deliveryOpen = async id => {
      const out = await baseOpen(id);
      const meta = $('#dialog .delivery-detail .detail-meta');
      if (meta && !$('#dialog .mj-entry')) {
        const line = document.createElement('div');
        line.className = 'mj-entry';
        line.innerHTML = '<span><b>MuJoCo 仿真</b> 用这个品牌型号的机器人模型和应用的步骤生成仿真环境，在里面运行这个应用。</span>' +
          mjButton('仿真环境', 'open', 'secondary small', id);
        (($('#dialog .robot-model-delivery')) || meta).after(line);
      }
      return out;
    };
  }
}
