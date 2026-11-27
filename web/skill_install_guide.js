'use strict';
// Issue #62 — 机器人应用页面无法安装技能. A skill is installed into the management layer on a robot, so a
// robot that is not connected cannot take one. From 机器人应用 a person got there in two clicks
// (「使用机器人上已安装的技能」 → 「安装技能」) and found every 「安装到这台机器人」 greyed out, with a small
// "无法判断（机器人未连接）" as the only word about it; the dialog before it said "机器人上还没有安装技能"
// although no robot had been asked. Both dialogs now say what is missing and carry the button for it:
//   * a robot reached over SSH only         → 「安装管理层」 / 「接入已有的管理层」 (issues #19, #17)
//   * a robot whose management layer is not answering → 「重新连接」, done in place, then the dialog goes on
//   * anything else (a stopped simulated robot, a robot with no way in) → 「我的机器人」
// sgStep … sgInstallTarget are pure (a robot in, facts or markup out): tests/skill_install_guide_logic.cjs.
// Loaded last: it wraps simGalleryOpen (web/sim.js) and `action`, and draws into the dialogs they made.

const SG_CONNECT = 'skillpkg-guide-connect';      // "skillpkg-": allowed with no task open (web/robot_no_task.js)
const SG_ROBOTS = 'skillpkg-guide-robots';
const SG_ACTIONS = [SG_CONNECT, SG_ROBOTS];
const SG_ADOPT_TITLE = '使用机器人上已安装的技能';

const sgIsSim = r => Boolean(r.sim || (r.manifest || {}).simulated === true);
const sgOldestFirst = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''));

// What stands between a robot and installing a skill on it, and the buttons that deal with it. null: nothing does.
function sgStep(robot) {
  if (!robot || robot.status === 'connected') return null;
  const act = (label, action) => ({label, action, id: robot.id});
  const toRobots = act('打开「我的机器人」', SG_ROBOTS);
  if (sgIsSim(robot)) {
    return {kind: 'robots', title: '这台仿真机器人现在没有在运行', text: '在「我的机器人」里把它启动起来，再回来安装技能。', actions: [toRobots]};
  }
  const canLink = Boolean(robot.connection_id) && !robot.managed_resource_id;          // rlCanLink of web/runtime_link.js
  if (robot.endpoint) {
    return {kind: 'connect', title: '这台机器人现在没有连上',
            text: '技能要安装到机器人上的管理层里，现在连不上它。重新连接成功后就可以安装。',
            actions: [act('重新连接', SG_CONNECT)].concat(canLink ? [act('管理层设置', 'engine-runtime-link')] : [])};
  }
  if (canLink) {
    return {kind: 'layer', title: '这台机器人还没有接入管理层',
            text: '现在只通过 SSH 连着它：可以看状态、做调查，但技能要安装到机器人上的管理层里。安装并接入管理层之后，就可以在这里安装技能。',
            actions: [act('安装管理层', 'engine-runtime-install'), act('接入已有的管理层', 'engine-runtime-link')]};
  }
  return {kind: 'robots', title: '这台机器人还没有接入', text: '在「我的机器人」里把它连接好，再回来安装技能。', actions: [toRobots]};
}

const sgButtons = step => `<div class="button-row">${step.actions.map((a, i) => button(a.label, a.action, i ? 'secondary small' : 'small', a.id)).join('')}</div>` +
  '<div class="sg-error" role="alert"></div>';

// Above the skills of the install dialog: why none of them can be installed, and what to do. '' for a connected robot.
function sgNotice(robot) {
  const step = sgStep(robot);
  if (!step) return '';
  return `<section class="sg-notice" role="status" data-sg-kind="${step.kind}"><b class="sg-title">${h(step.title)}</b><p>${h(step.text)}</p>${sgButtons(step)}</section>`;
}

// 「使用机器人上已安装的技能」 when there are robots and none is connected: each robot with its own step.
// '' when the dialog's own words are right (no robot at all, or one that can be asked for its skills).
function sgAdoptBody(robots) {
  const all = [...(robots || [])].sort(sgOldestFirst);
  if (!all.length || all.some(r => r.status === 'connected')) return '';
  return '<div class="sg-none"><b>没有已连接的机器人</b><p>已安装的技能要从机器人上读取，现在一台都连不上。先把机器人连接好：</p></div>' +
    all.map(r => { const step = sgStep(r); return `<div class="contract-line sg-robot" data-sg-kind="${step.kind}"><div class="section-head"><b>${h(r.name)}</b></div>` +
      `<p>${h(step.title)}。${h(step.text)}</p>${sgButtons(step)}</div>`; }).join('');
}

// The robot 「安装技能」 of that dialog opens: one that can take a skill, else the one added first.
function sgInstallTarget(robots) {
  const all = [...(robots || [])].sort(sgOldestFirst);
  return (all.find(r => r.status === 'connected') || all[0] || {}).id || '';
}

// ---------------------------------------------------------------- on the page

function sgDecorateGallery(robotId) {
  const box = $('#dialog');
  if (!box?.open || box.dataset.simGallery !== robotId) return;
  const body = box.querySelector('.dialog-body');
  if (!body) return;
  body.querySelector('.sg-notice')?.remove();
  const html = sgNotice(find('robots', robotId));
  if (html) body.insertAdjacentHTML('afterbegin', html);
}

function sgAdoptOpen() {
  const body = sgAdoptBody(rows('robots'));
  if (!body) return false;
  dialog(SG_ADOPT_TITLE, '加入后，就能把它编排进机器人应用', `<div class="dialog-body">${body}</div>`);
  return true;
}

// 「重新连接」: in place. What goes wrong is said inside the dialog (a toast sits under an open dialog).
async function sgConnect(robotId, el) {
  const box = el?.closest('.sg-notice, .sg-robot'), error = box?.querySelector('.sg-error');
  const inGallery = Boolean(box?.classList.contains('sg-notice'));
  if (error) error.textContent = '';
  if (el) el.disabled = true;
  try {
    await api('robots/connect', {robot_id: robotId});
    await refresh(true);
  } catch (err) {
    if (el?.isConnected) el.disabled = false;
    if (!error?.isConnected) throw err;
    formError(error, err);
    return undefined;
  }
  if (find('robots', robotId)?.status !== 'connected') {            // the answer came, the robot still is not there
    if (el?.isConnected) el.disabled = false;
    if (error?.isConnected) error.textContent = '还是没有连上。请到「我的机器人」里看这台机器人的连接情况。';
    return undefined;
  }
  toast('已连接，可以安装技能了');
  // Go on only where the person still is: a dialog opened in the meantime is left alone.
  if (!box?.isConnected || !$('#dialog').open) return undefined;
  return inGallery ? simGalleryOpen(robotId) : action('ux-adopt');
}

if (typeof document !== 'undefined') {
  const baseGallery = simGalleryOpen, baseAction = action;
  simGalleryOpen = async robotId => {
    const reading = baseGallery(robotId);          // it opens the dialog before it waits for the robot
    sgDecorateGallery(robotId);                    // so the reason is there while the skills are being read,
    try { return await reading; } finally { sgDecorateGallery(robotId); }      // and stays when reading them fails
  };
  action = async (type, id, el) => {
    if (type === SG_CONNECT) return sgConnect(id, el);
    if (type === SG_ROBOTS) { $('#dialog').close(); location.hash = 'robots'; return undefined; }
    if (type !== 'ux-adopt') return baseAction(type, id, el);
    if (sgAdoptOpen()) return undefined;
    const out = await baseAction(type, id, el);
    // With no skill on any robot the dialog offers 「安装技能」 for the first robot of the list: make it one that can take a skill.
    const install = $('#dialog [data-action="skillpkg-open"]'), target = sgInstallTarget(rows('robots'));
    if (install && target) install.dataset.id = target;
    return out;
  };
}
