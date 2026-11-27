'use strict';
// Issue #20 — the management layer starts when the robot boots (brainos/runtime_autostart.py).
// Installing needs no administrator rights; autostart does, so it is a step of its own: the install form has
// a box for it, and the card has a line "开机自启" that says whether it is on and, when not, why. Before
// anything runs as root the job waits and this page lists exactly what will run (the service file, the
// commands, the fixed script); only "确认执行" lets it go on. A sudo password typed here is sent once, with
// that answer, and is not kept by the page or the console.
// Loaded after runtime_install.js. It wraps riForm, riShow, riSummary, riResult, riActions, riPaint,
// engineRobotCard, engineAction and engineSubmit.

const RA_MODES = ['autostart', 'autostart_off'];
const RA_SHORT = {
  not_asked: '安装时没有选择', declined: '你没有确认', not_confirmed: '没有确认（等待超时）',
  no_sudo: '这个账号不能用 sudo', no_systemd: '设备没有 systemd', not_console: '不是这个控制台装的管理层',
  not_installed: '还没有安装管理层', unit_taken: '设备上已有别人建的同名服务', unsupported: '设备缺少需要的东西',
  wrong_password: 'sudo 密码不对', removed: '已取消', failed: '设置没有成功',
};

const raLive = robotId => (typeof riLive === 'function' ? riLive(robotId) : null);
const raWaiting = view => !!view && view.state === 'running' && view.autostart?.state === 'waiting' && !!view.autostart.plan;

// What the card says: {tone, text, detail}. The saved outcome first; without one, what the install says.
function raStatus(r) {
  const live = raLive(r.id);
  if (raWaiting(live)) return {tone: 'warn', text: '等你确认要执行的内容', detail: ''};
  const saved = r.runtime_autostart;
  if (saved?.state === 'on') return {tone: 'good', text: '已开机自启', detail: `系统服务 ${saved.unit ? saved.unit.split('/').pop() : 'neuxmind-runtime.service'}`};
  if (saved?.state === 'failed') return {tone: 'bad', text: '开机自启没有成功', detail: saved.message || ''};
  if (saved?.state === 'off') return {tone: 'warn', text: '未开机自启', detail: Object.hasOwn(RA_SHORT, saved.reason) ? RA_SHORT[saved.reason] : (saved.message || '')};
  const install = r.runtime_install;
  if (!install || !install.package_id) return {tone: 'warn', text: '未开机自启', detail: RA_SHORT.not_installed};
  if (install.console_install === false || (install.console_install === undefined && !install.created_install)) {
    return {tone: 'warn', text: '未开机自启', detail: RA_SHORT.not_console};
  }
  return {tone: 'warn', text: '未开机自启', detail: install.state === 'succeeded' ? RA_SHORT.not_asked : RA_SHORT.not_installed};
}

const raLineSig = r => JSON.stringify(raStatus(r));
function raLine(r) {
  const s = raStatus(r), name = r.name || '';
  return `<div class="ra-line" data-ra-robot="${h(r.id)}" data-ra-sig="${h(raLineSig(r))}"><span class="ri-label">开机自启</span>` +
    `${badge(s.text, s.tone)}${s.detail ? `<span class="ra-detail">${h(s.detail)}</span>` : ''}` +
    `<button type="button" class="text-link" data-action="engine-runtime-autostart" data-id="${h(r.id)}"` +
    ` aria-label="${h(name ? `${name} 的开机自启` : '开机自启')}">${raWaiting(raLive(r.id)) ? '去确认' : '设置'}</button></div>`;
}

function raCard(html, r) {
  if (typeof riCanInstall !== 'function' || !riCanInstall(r)) return html;
  return html.replace('<div class="tags">', () => raLine(r) + '<div class="tags">');
}

// ------------------------------------------------------------------ the install form: one more box

function raAddInstallField() {
  const body = typeof document === 'undefined' ? null : document.querySelector('#dialog[open] form[data-form="engine-runtime-install"] .dialog-body');
  if (!body || body.querySelector('[name="autostart"]')) return;
  body.insertAdjacentHTML('beforeend', '<label class="hardware-confirm ra-install"><input type="checkbox" name="autostart">' +
    '<span><b>装好后设置开机自启</b>（要管理员权限）：断电重启后管理层自己起来。执行前会列出要以管理员身份做的每一件事，等你确认；' +
    '需要时再输入 sudo 密码，只用这一次、不保存。不勾选也照常安装和运行，只是重启机器人后要再点「安装管理层」启动它。</span></label>');
}

function raInstallRequest(p) {
  return p.autostart ? {autostart: true} : {};
}

// ------------------------------------------------------------------ the confirmation

function raCommands(title, steps) {
  if (!steps?.length) return '';
  return `<h4 class="ra-h">${h(title)}</h4><ol class="ra-commands">` +
    steps.map(step => `<li><code>${h(step.command)}</code><span>${h(step.what)}</span></li>`).join('') + '</ol>';
}

function raConfirm(robotId, view) {
  const plan = view.autostart.plan, on = plan.kind === 'on';
  const unit = on && plan.unit ? `<details class="ra-unit" open><summary>服务文件 <code>${h(plan.unit_path)}</code>（${plan.unit.split('\n').length - 1} 行）</summary><pre>${h(plan.unit)}</pre></details>` : '';
  const password = plan.needs_password
    ? field(`sudo 密码（${h(plan.user)}）`, '<input name="sudo_password" type="password" autocomplete="off" maxlength="256">',
      '只用于这一次：经标准输入交给设备上的 sudo，不保存、不显示、不写进日志，也不出现在命令行里。') : '';
  form(on ? '开机自启：确认要执行的内容' : '取消开机自启：确认要执行的内容', h(riName(robotId)),
    // data-keep-code: the service file and the script are shown exactly as they run, not made "readable" (readable_dom.js).
    `<div class="ra-confirm" data-keep-code data-ra-confirm="${h(robotId)}" data-ra-plan="${h(plan.id)}">` +
    `<p class="ra-intro">在 <b>${h(plan.host || plan.user)}</b> 上，${h(plan.how_label || '')}。确认之前什么都不会执行。</p>` +
    (plan.error ? `<div class="ra-error" role="alert">${h(plan.error)}</div>` : '') +
    raCommands(`以 ${plan.user} 的身份`, plan.account_steps) + raCommands('以管理员身份（root）', plan.commands) + unit +
    `<details class="ra-script"><summary>查看会执行的完整脚本</summary><pre>${h(plan.script || '')}</pre></details>` +
    `<p class="ra-not">${h(plan.does_not || '')}</p>${password}` +
    '<label class="hardware-confirm"><input type="checkbox" name="confirm_plan"><span>我看过上面的内容，确认以管理员身份执行。</span></label>' +
    `<div class="button-row ra-decline">${button(on ? '不设开机自启' : '不取消', 'engine-runtime-autostart-decline', 'secondary small', robotId)}` +
    '<small>关掉这个窗口不算回答：卡片上会一直提示，过一段时间没有回答就按“不执行”处理。</small></div></div>',
    'engine-runtime-autostart-answer', '确认执行',
    `data-id="${h(robotId)}" data-job="${h(view.job_id)}" data-plan="${h(plan.id)}" data-needs-sudo="${plan.needs_password ? '1' : ''}"`);
}

async function raAnswer(robotId, request) {
  const view = await api('robots/runtime-install/answer', {project_id: projectId, robot_id: robotId, ...request});
  riInstalls = {...riInstalls, [robotId]: view};
  riShow(robotId, true);
  riPoll(true);
}

async function raSubmit(p, f) {
  if (!p.confirm_plan) throw Error('请先勾选“我看过上面的内容，确认以管理员身份执行”');
  const request = {job_id: f.dataset.job, plan: f.dataset.plan, decision: 'confirm'};
  if (f.dataset.needsSudo === '1') {
    if (!p.sudo_password) throw Error('请输入这个账号的 sudo 密码（只用这一次，不保存）');
    request.sudo_password = String(p.sudo_password);
  }
  try { await raAnswer(f.dataset.id, request); } finally { delete request.sudo_password; }
}

async function raDecline(robotId) {
  const view = raLive(robotId);
  if (!raWaiting(view)) return riShow(robotId, true);
  return raAnswer(robotId, {job_id: view.job_id, plan: view.autostart.plan.id, decision: 'decline'});
}

// ------------------------------------------------------------------ the card's dialog

function raDialog(robotId) {
  const r = find('robots', robotId), live = raLive(robotId);
  if (!r) return;
  if (raWaiting(live)) return raConfirm(robotId, live);
  if (live && RA_MODES.includes(live.mode) && live.state === 'running') return riShow(robotId, true);
  const s = raStatus(r), saved = r.runtime_autostart, on = saved?.state === 'on' || saved?.state === 'failed';
  const canTurnOn = !!r.runtime_install?.package_id && r.runtime_install.console_install !== false;
  const why = saved?.message || (s.detail ? s.detail + '。' : '');
  const what = '开机自启是一个系统服务（neuxmind-runtime.service），以 SSH 登录的这个账号运行管理层，断电重启后由 systemd 在网络就绪后启动，起不来时每 15 秒重试。' +
    '登记和取消都要管理员权限：控制台先只读检查设备，再列出要以管理员身份执行的每一件事，等你确认。';
  const actions = (on ? button('取消开机自启', 'engine-runtime-autostart-off', 'secondary', robotId) : '') +
    (!on && canTurnOn ? button('设置开机自启', 'engine-runtime-autostart-on', '', robotId) : '') + button('关闭', 'close', 'secondary');
  dialog('开机自启', h(riName(robotId)),
    `<div class="dialog-body"><div class="ra-state">${badge(s.text, s.tone)}<p>${h(why)}</p></div><p class="inline-note">${h(what)}</p>` +
    (!on && !canTurnOn ? `<p class="inline-note">${h(r.runtime_install?.package_id ? RA_SHORT.not_console + '：要开机自启，先点「安装管理层」选「删除后重新安装」。' : '先点「安装管理层」把管理层装上。')}</p>` : '') +
    `</div><div class="dialog-footer">${actions}</div>`);
}

async function raStart(robotId, off) {
  const view = await api(off ? 'robots/runtime-install/autostart-off' : 'robots/runtime-install/autostart', {project_id: projectId, robot_id: robotId});
  riInstalls = {...riInstalls, [robotId]: view};
  riShow(robotId, true);
  riPoll(true);
}

// ------------------------------------------------------------------ the restart hint of ux04 (real robots)

// What to run on the robot to restart its management layer: the service when it autostarts, the install
// script of this console otherwise. Nothing for a management layer this console did not install.
function raRestartHint(robot) {
  const code = text => `<code class="mono howto-cmd">${h(text)}</code>`;
  if (robot?.runtime_autostart?.state === 'on') return code('sudo systemctl restart neuxmind-runtime.service');
  const install = robot?.runtime_install;
  if (install?.package_id && install.console_install !== false && /^runtime-[A-Za-z0-9._+-]+$/.test(install.package_id)) {
    const script = `~/neuxmind-pkg/console/${install.package_id}/device_install.sh`;
    const port = Number.isInteger(install.port) ? install.port : 8790, adapter = /^[a-z0-9_]+$/.test(install.adapter || '') ? install.adapter : 'sim_quadruped';
    return code(`bash ${script} stop`) + code(`NX_PORT=${port} NX_ADAPTER=${adapter} bash ${script} start`);
  }
  return '<span class="howto-cmd-note">这台机器人的管理层不是这个控制台装的：用当初启动它的方式重启（例如安装脚本的 stop 再 start）。</span>';
}

// ------------------------------------------------------------------ drawing in place

function raPaint() {
  if (typeof document === 'undefined') return;
  for (const el of document.querySelectorAll('[data-ra-robot]')) {
    const r = find('robots', el.dataset.raRobot);
    if (!r || el.dataset.raSig === raLineSig(r)) continue;
    const focused = el.contains(document.activeElement), parent = el.parentNode, next = el.nextSibling;
    el.outerHTML = raLine(r);
    const fresh = next ? next.previousSibling : parent?.lastChild;
    if (focused) fresh?.querySelector?.('[data-action]')?.focus({preventScroll: true});
  }
  const confirmEl = document.querySelector('#dialog[open] [data-ra-confirm]');
  if (confirmEl) {
    const robotId = confirmEl.dataset.raConfirm, view = raLive(robotId);
    if (!raWaiting(view)) return riShow(robotId, true);       // answered, timed out or stopped meanwhile
    if (view.autostart.plan.id !== confirmEl.dataset.raPlan) return raConfirm(robotId, view);   // asked again (a wrong password)
    return;                                                  // never redrawn while the person reads or types
  }
  const progress = document.querySelector('#dialog[open] [data-ri-view]');
  if (progress && raWaiting(raLive(progress.dataset.riView))) raConfirm(progress.dataset.riView, raLive(progress.dataset.riView));
}

{
  const baseCard = engineRobotCard, baseAction = engineAction, baseSubmit = engineSubmit;
  const baseForm = riForm, baseShow = riShow, baseSummary = riSummary, baseResult = riResult, baseActions = riActions, basePaint = riPaint;
  engineRobotCard = r => raCard(baseCard(r), r);
  riForm = (robotId, port = null) => { baseForm(robotId, port); raAddInstallField(); };
  riShow = (robotId, fresh = false) => {
    const view = raLive(robotId);
    if (raWaiting(view)) return raConfirm(robotId, view);
    if (view && RA_MODES.includes(view.mode) && !fresh && view.state === 'succeeded') return raDialog(robotId);
    return baseShow(robotId, fresh);
  };
  riSummary = view => {
    if (raWaiting(view)) return '等你确认开机自启要执行的内容';
    if (!view || !RA_MODES.includes(view.mode)) return baseSummary(view);
    const what = view.mode === 'autostart' ? '设置开机自启' : '取消开机自启';
    if (view.state === 'running') return view.stopping ? `正在停止：${view.stage_label || ''}` : `正在${what}：${view.stage_label || ''}`;
    if (view.state === 'failed') return `${what}没有完成：停在「${view.failure?.stage_label || view.stage_label || ''}」`;
    if (view.state === 'stopped') return `已停止${what}`;
    return '';
  };
  riResult = view => {
    const a = view.autostart;
    if (RA_MODES.includes(view.mode) && view.state === 'succeeded') {
      return notice(h(a?.message || ''), !(a && (a.state === 'on' || (view.mode === 'autostart_off' && a.state === 'off'))));
    }
    const base = baseResult(view);
    if (view.state !== 'succeeded' || !a || view.mode !== 'install') return base;
    return base + notice(a.state === 'on' ? h(a.message) : `未开机自启：${h(a.message || '')}`, a.state !== 'on');
  };
  riActions = (view, robotId) => {
    if (!RA_MODES.includes(view.mode) || view.state === 'running') return baseActions(view, robotId);
    const off = view.state === 'failed' && view.mode === 'autostart' ? button('取消开机自启', 'engine-runtime-autostart-off', 'secondary', robotId) : '';
    return off + button(view.state === 'succeeded' ? '完成' : '关闭', 'close', view.state === 'succeeded' ? '' : 'secondary');
  };
  riPaint = () => { basePaint(); raPaint(); };
  engineAction = async (type, id) => {
    if (!type.startsWith('engine-runtime-autostart') || !state.user) return baseAction(type, id);
    if (type === 'engine-runtime-autostart') return raDialog(id);
    if (type === 'engine-runtime-autostart-on') return raStart(id, false);
    if (type === 'engine-runtime-autostart-off') return raStart(id, true);
    if (type === 'engine-runtime-autostart-decline') return raDecline(id);
    return baseAction(type, id);
  };
  engineSubmit = async (kind, p, f) => (kind === 'engine-runtime-autostart-answer' ? raSubmit(p, f) : baseSubmit(kind, p, f));
}
