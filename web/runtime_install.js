'use strict';
// Issue #19 — install the management layer on a robot from the console (brainos/runtime_install.py).
// A robot reached over SSH gets "安装管理层" on its card: the console checks the robot, sends its own
// install package, installs and starts it in ~/neuxmind and links it (issue #17). No sudo, no
// system files; autostart is a step of its own, listed and confirmed (runtime_autostart.js, issue #20).
// A management layer that is already there is not touched until the
// person chooses here what to do with it.
// The progress changes all the time, so it is asked for on its own (robots/runtime-install/status)
// and drawn in place, never through the workspace state the page redraws itself from.
// Loaded after runtime_link.js. It wraps engineRobotCard, engineAction, engineSubmit and engineRefresh.

const RI_POLL_MS = 2000, RI_IDLE_POLL_MS = 15000;
const RI_DEFAULT_PORT = 8790, RI_PORT_MIN = 1024, RI_PORT_MAX = 65535;
const RI_MARK = {done: '✓', running: '', failed: '✕', stopped: '■', pending: ''};
const RI_STATE = {done: '完成', running: '进行中', failed: '失败', stopped: '已停止', pending: '未开始'};
const RI_BODY = {quadruped: '仿真四足（sim_quadruped）', manipulator: '仿真机械臂（sim_arm）'};
const RI_ROOT = {none: '没有', empty: '一个空目录', unfinished: '一份没装完的安装', installed: '一个装好的管理层',
  foreign: '不是安装脚本建的目录', link: '一个符号链接'};
// {robot id: view of its install} as the backend last sent them; robots whose "清理" waits for a second click.
let riInstalls = {}, riPolling = false, riLastPoll = 0, riBaseAction = null, riBaseSubmit = null;
const riConfirming = new Set();

const riOwn = (table, key, otherwise = '') => (Object.hasOwn(table, key) ? table[key] : otherwise);
const riLive = robotId => riOwn(riInstalls, robotId, null);
// What the page knows of a robot's install: the live view, else the outcome saved with the robot.
const riOf = r => riLive(r.id) || (r.runtime_install ? {...r.runtime_install, robot_id: r.id, saved: true} : null);
const riRunning = view => !!view && view.state === 'running';
const riCanInstall = r => typeof rlCanLink === 'function' ? rlCanLink(r) : (!!r && !r.sim && !!r.connection_id && !r.managed_resource_id);
const riName = robotId => state.robots?.find(r => r.id === robotId)?.name || '';

function riSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

// The port a person typed, or null when the management layer cannot have it.
function riPort(text) {
  const value = String(text ?? '').trim();
  if (value === '') return RI_DEFAULT_PORT;
  if (!/^[0-9]{1,5}$/.test(value)) return null;
  const port = Number(value);
  return port >= RI_PORT_MIN && port <= RI_PORT_MAX ? port : null;
}

function riPercent(view) {
  const p = view?.progress;
  return p && p.total > 0 ? Math.min(100, Math.floor(p.sent * 100 / p.total)) : null;
}

// One line on the card while an install runs or after one did not finish; nothing after success.
function riSummary(view) {
  if (!view) return '';
  if (view.state === 'running') {
    const pct = view.stage === 'upload' ? riPercent(view) : null;
    if (view.stopping) return `正在停止：${view.stage_label || ''}`;
    return `${view.mode === 'clean' ? '正在清理' : '正在安装'}：${view.stage_label || ''}${pct === null ? '' : ` ${pct}%`}`;
  }
  if (view.state === 'needs_choice') return '设备上已经有管理层，等你选择怎么处理';
  if (view.state === 'failed') return `${view.mode === 'clean' ? '清理' : '安装'}没有完成：停在「${view.failure?.stage_label || view.stage_label || ''}」`;
  if (view.state === 'stopped') return '安装已停止';
  return '';
}

const riLineSig = view => JSON.stringify([view?.state, view?.stopping, view?.stage, riPercent(view), view?.failure?.stage]);
function riLine(robotId, view) {
  const text = riSummary(view);
  const tone = view?.state === 'failed' ? 'bad' : view?.state === 'running' ? 'blue' : 'warn';
  const name = riName(robotId), open = `<div class="ri-line" data-ri-robot="${h(robotId)}" data-ri-sig="${h(riLineSig(view))}"`;
  if (!text) return open + ' hidden></div>';           // a place to draw into once an install begins
  return open + `><span class="ri-label">管理层安装</span>${badge(text, tone)}` +
    `<button type="button" class="text-link" data-action="engine-runtime-install" data-id="${h(robotId)}"` +
    ` aria-label="${h(name ? `查看 ${name} 的管理层安装` : '查看管理层安装')}">查看</button></div>`;
}

function riCard(html, r) {
  if (!riCanInstall(r)) return html;
  const mine = button('安装管理层', 'engine-runtime-install', 'secondary small', r.id);
  const linked = typeof rlLinked === 'function' && rlLinked(r);
  const anchor = button(linked ? '管理层设置' : '接入管理层', 'engine-runtime-link', 'secondary small', r.id);
  const end = '</div></div></article>';
  // Functions, not strings: a "$" in a name must not be read as a pattern of String.replace.
  let out = html.includes(anchor) ? html.replace(anchor, () => anchor + mine)
    : html.endsWith(end) ? html.slice(0, -end.length) + mine + end : html;
  out = out.replace('<div class="tags">', () => riLine(r.id, riOf(r)) + '<div class="tags">');
  return out;
}

// ------------------------------------------------------------------ the dialog

function riStages(view) {
  const pct = riPercent(view);
  return '<ol class="ri-stages">' + (view.stages || []).map(stage => {
    const extra = stage.state === 'running' && stage.id === 'upload' && pct !== null
      ? ` · ${pct}%（${riSize(view.progress.sent)} / ${riSize(view.progress.total)}）` : '';
    return `<li class="ri-stage is-${h(stage.state)}"><span class="ri-mark" aria-hidden="true">${riOwn(RI_MARK, stage.state)}</span>` +
      `<b>${h(stage.label)}</b><small>${h(riOwn(RI_STATE, stage.state, stage.state))}${h(extra)}</small></li>`;
  }).join('') + '</ol>';
}

function riLeft(view) {
  const items = (view.left || []).map(item => `<li>${item.path ? `<code>${h(item.path)}</code> ` : ''}${h(item.what)}</li>`).join('');
  return `<div class="ri-left"><b>设备上留下的</b>${items ? `<ul>${items}</ul>` : ''}<p>${h(view.cleanup || '')}</p></div>`;
}

function riFound(found) {
  const where = riOwn(RI_ROOT, found.root, found.root);
  const facts = [found.version ? `版本 ${found.version}` : '', found.running ? `正在运行${found.running_port ? `（端口 ${found.running_port}）` : ''}` : '没有运行',
    found.console_package ? '由控制台安装' : '不是这个控制台装的'].filter(Boolean).join('，');
  return `<p class="ri-found">设备上的 <code>~/neuxmind</code> 已经有${h(where)}：${h(facts)}。控制台没有动它，也没有上传任何东西。</p>`;
}

function riResult(view) {
  if (view.state === 'succeeded') {
    return view.mode === 'clean'
      ? notice('已清理：这次安装放到设备上的东西已经删除。' + (view.notes || []).map(h).join(''))
      : notice('管理层已安装并接入，连接状态「在线」。卡片上「管理层」一行显示通道的状态。');
  }
  if (view.state === 'failed') {
    const f = view.failure || {};
    const log = (f.log || []).length ? `<details class="ri-log"><summary>设备上的输出（最后 ${f.log.length} 行）</summary><pre>${h(f.log.join('\n'))}</pre></details>` : '';
    return `<div class="ri-failure" role="alert"><b>停在「${h(f.stage_label || '')}」</b><p>${h(f.message || '')}</p>${log}</div>` + riLeft(view);
  }
  if (view.state === 'stopped') return `<div class="ri-failure is-stopped" role="status"><b>已停止等待</b><p>设备上已经开始的那一步会在设备上继续做完；再点「重新安装」会接着看它的结果。</p></div>` + riLeft(view);
  return '';
}

function riBody(view) {
  const notes = (view.notes || []).length && view.state !== 'succeeded'
    ? `<ul class="ri-notes">${view.notes.map(note => `<li>${h(note)}</li>`).join('')}</ul>` : '';
  const pkg = view.package?.runtime_version ? `<p class="inline-note ri-package">控制台自带的安装包：管理层 ${h(view.package.runtime_version)}` +
    `${view.package.bytes ? `，${h(riSize(view.package.bytes))}` : ''}；设备上的位置 <code>${h(view.package_dir || '')}</code></p>` : '';
  return riStages(view) + notes + riResult(view) + pkg;
}

function riActions(view, robotId) {
  const close = button(view.state === 'succeeded' ? '完成' : '关闭', 'close', view.state === 'succeeded' ? '' : 'secondary');
  if (view.state === 'running' && view.stopping) return `<span class="ri-stopping">正在停止：等设备那边的这一步放弃（网络很差时最多约两分钟）</span>` + close;
  if (view.state === 'running') return button('停止', 'engine-runtime-install-stop', 'secondary', robotId) + close;
  if (view.state === 'failed' || view.state === 'stopped') {
    const clean = view.can_clean ? (riConfirming.has(robotId)
      ? button('确认清理', 'engine-runtime-install-clean-confirm', 'danger', robotId) + button('不清理', 'engine-runtime-install-clean-cancel', 'secondary', robotId)
      : button('清理', 'engine-runtime-install-clean', 'secondary', robotId)) : '';
    return clean + button('重新安装', 'engine-runtime-install-again', 'secondary', robotId) + close;
  }
  return close;
}

const riViewSig = view => JSON.stringify([view.state, view.stage, (view.stages || []).map(s => s.state), riPercent(view), view.failure, view.left]);
const riActionSig = (view, robotId) => JSON.stringify([view.state, view.stopping, view.can_clean, riConfirming.has(robotId)]);

// "安装管理层" and "查看": what is under way or did not finish, else a new install.
const RI_SHOWN = ['running', 'needs_choice', 'failed', 'stopped'];
function riShow(robotId, fresh = false) {
  const robot = find('robots', robotId);
  const view = riLive(robotId) || (robot && riOf(robot));
  if (!view || !(fresh || RI_SHOWN.includes(view.state))) return riForm(robotId);
  if (view.state === 'needs_choice') return riChoice(robotId, view);
  dialog('安装管理层', h(riName(robotId)),
    `<div class="dialog-body"><div class="ri-view" data-ri-view="${h(robotId)}" data-ri-sig="${h(riViewSig(view))}">${riBody(view)}</div></div>` +
    `<div class="dialog-footer ri-actions" data-ri-actions="${h(robotId)}" data-ri-sig="${h(riActionSig(view, robotId))}">${riActions(view, robotId)}</div>`);
}

function riConnection(r) {
  const c = find('device_connections', r.connection_id);
  if (!c) return '';
  return `${c.username}@${c.host}:${c.port}` + (typeof sshRouteLabel === 'function' ? sshRouteLabel(c) : '');
}

function riForm(robotId, port = null) {
  const r = find('robots', robotId);
  if (!riCanInstall(r)) throw Error('这台机器人没有 SSH 连接；请先通过 SSH 接入');
  const body = riOwn(RI_BODY, r.family, '');
  form('安装管理层', h(r.name),
    `<p class="inline-note ri-how">经这台机器人的 SSH 连接（${h(riConnection(r))}）：先只读检查设备，再把控制台自带的安装包传到 ` +
    '<code>~/neuxmind-pkg/console/</code>，在 <code>~/neuxmind</code> 安装、生成访问令牌并启动，最后接入管理层。' +
    '安装本身不用 sudo、不改系统文件；开机自启要管理员权限，在下面单独勾选。管理层只监听机器人本机。设备上已经有管理层时，先问你怎么处理，不会直接覆盖。</p>' +
    field('管理层端口', `<input name="port" type="number" inputmode="numeric" min="${RI_PORT_MIN}" max="${RI_PORT_MAX}" step="1" value="${h(port ?? RI_DEFAULT_PORT)}" required>`,
      `管理层在机器人本机监听的端口，默认 ${RI_DEFAULT_PORT}。`) +
    `<div class="ri-body-type"><b>本体</b><span>${body ? h(body) + '：不接真实硬件，不会让机器人动' : '这个本体类型没有对应的仿真本体，装不上'}</span></div>`,
    'engine-runtime-install', '开始安装', `data-id="${h(robotId)}"`);
}

function riChoice(robotId, view) {
  const options = view.choice?.options || [];
  const radios = options.map((option, index) =>
    `<label class="extension-choice ri-option"><input type="radio" name="existing" value="${h(option.id)}"${index === 0 ? ' checked' : ''}>` +
    `<span><b>${h(option.label)}</b></span></label>`).join('');
  const replace = options.some(option => option.id === 'replace')
    ? '<label class="hardware-confirm ri-replace"><input type="checkbox" name="confirm_replace"><span>选“删除后重新安装”时：我确认删除设备上原有的 ~/neuxmind（它的状态目录、日志和令牌），这不能撤销。</span></label>' : '';
  form('安装管理层', h(riName(robotId)), riFound(view.choice?.found || {}) + `<div class="extension-picks">${radios}</div>` + replace,
    'engine-runtime-install', '继续', `data-id="${h(robotId)}" data-port="${h(view.port || RI_DEFAULT_PORT)}" data-choice="1"`);
}

async function riSubmit(p, f) {
  const robotId = f.dataset.id;
  const port = f.dataset.choice ? Number(f.dataset.port) : riPort(p.port);
  if (port === null) throw Error(`管理层端口须为 ${RI_PORT_MIN}–${RI_PORT_MAX} 之间的整数（默认 ${RI_DEFAULT_PORT}）`);
  const request = {project_id: projectId, robot_id: robotId, port};
  if (f.dataset.choice) {
    if (!p.existing) throw Error('请选择怎么处理设备上已有的管理层');
    request.existing = p.existing;
    if (p.existing === 'replace') {
      if (!p.confirm_replace) throw Error('删除后重新安装会删掉设备上原有的 ~/neuxmind，请先勾选确认');
      request.confirm_replace = true;
    }
  }
  if (typeof raInstallRequest === 'function') Object.assign(request, raInstallRequest(p));   // issue #20
  const view = await api('robots/runtime-install/start', request);
  riInstalls = {...riInstalls, [robotId]: view};
  riConfirming.delete(robotId);
  riShow(robotId, true);
  riPoll(true);
}

async function riStop(robotId) {
  const result = await api('robots/runtime-install/stop', {project_id: projectId, robot_id: robotId});
  if (result.install) riInstalls = {...riInstalls, [robotId]: result.install};
  await refresh(true);
  riPaint();
}

async function riClean(robotId) {
  riConfirming.delete(robotId);
  const view = await api('robots/runtime-install/clean', {project_id: projectId, robot_id: robotId});
  riInstalls = {...riInstalls, [robotId]: view};
  riShow(robotId, true);
  riPoll(true);
}

// ------------------------------------------------------------------ drawing in place, asking for it

function riPaint() {
  if (typeof document === 'undefined') return;
  for (const el of document.querySelectorAll('[data-ri-robot]')) {
    const r = find('robots', el.dataset.riRobot), view = r && riOf(r);
    if (!r || el.dataset.riSig === riLineSig(view)) continue;
    const focused = el.contains(document.activeElement), parent = el.parentNode, next = el.nextSibling;
    el.outerHTML = riLine(r.id, view);
    const fresh = next ? next.previousSibling : parent?.lastChild;
    if (focused) fresh?.querySelector?.('[data-action]')?.focus({preventScroll: true});
  }
  const viewEl = document.querySelector('#dialog[open] [data-ri-view]');
  if (!viewEl) return;
  const robotId = viewEl.dataset.riView, robot = find('robots', robotId), view = riLive(robotId) || (robot && riOf(robot));
  if (!view) return;
  if (view.state === 'needs_choice') return riChoice(robotId, view);
  const sig = riViewSig(view);
  if (viewEl.dataset.riSig !== sig) {
    viewEl.innerHTML = riBody(view);
    viewEl.dataset.riSig = sig;
  }
  const actions = document.querySelector('#dialog[open] [data-ri-actions]');
  if (actions && actions.dataset.riSig !== riActionSig(view, robotId)) {
    const focused = actions.contains(document.activeElement);
    actions.innerHTML = riActions(view, robotId);
    actions.dataset.riSig = riActionSig(view, robotId);
    if (focused) actions.querySelector('.button')?.focus({preventScroll: true});
  }
}

async function riPoll(force = false) {
  if (riPolling || !state.user) return;
  const anyRunning = Object.values(riInstalls).some(riRunning);
  if (!force && !anyRunning && Date.now() - riLastPoll < RI_IDLE_POLL_MS) return;
  if (!(state.robots || []).some(riCanInstall)) { riInstalls = {}; return; }
  riPolling = true;
  riLastPoll = Date.now();
  try {
    const before = riInstalls;
    riInstalls = (await api('robots/runtime-install/status', {})).installs || {};
    // An install that ended changed the robot (its link, its saved outcome): the page reads it again.
    const ended = Object.keys(before).some(id => riRunning(before[id]) && !riRunning(riInstalls[id]));
    if (ended) {
      await refresh(true);
      if (typeof rlPoll === 'function') rlPoll();
    }
    riPaint();
  } catch { /* the lines keep what they showed; the next poll answers */ }
  finally { riPolling = false; }
}

{
  const baseCard = engineRobotCard, baseRefresh = engineRefresh;
  riBaseAction = engineAction;
  riBaseSubmit = engineSubmit;
  engineRobotCard = r => riCard(baseCard(r), r);
  engineAction = async (type, id) => {
    if (!type.startsWith('engine-runtime-install') || !state.user || !project()) return riBaseAction(type, id);
    if (type === 'engine-runtime-install') return riShow(id);
    if (type === 'engine-runtime-install-again') return riForm(id, riLive(id)?.port);
    if (type === 'engine-runtime-install-stop') return riStop(id);
    if (type === 'engine-runtime-install-clean') { riConfirming.add(id); return riPaint(); }
    if (type === 'engine-runtime-install-clean-cancel') { riConfirming.delete(id); return riPaint(); }
    if (type === 'engine-runtime-install-clean-confirm') return riClean(id);
    return riBaseAction(type, id);
  };
  engineSubmit = async (kind, p, f) => (kind === 'engine-runtime-install' ? riSubmit(p, f) : riBaseSubmit(kind, p, f));
  engineRefresh = () => {
    baseRefresh();
    riPaint();
    riPoll();
  };
}
setInterval(() => {
  if (typeof document !== 'undefined' && document.hidden) return;
  const showing = typeof document !== 'undefined' && document.querySelector('#dialog[open] [data-ri-view]');
  if (showing || Object.values(riInstalls).some(riRunning)) riPoll(true);
}, RI_POLL_MS);
riPoll(true);
