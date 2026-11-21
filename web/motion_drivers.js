'use strict';
// Issue #28 — the motion driver check (R1-10): which motion-control interfaces a device's body type
// should have, and which of them its ROS 2 listing lacks.
//
// The comparison is made by the backend (brainos/motion_drivers.py) and arrives with the state as
// `motion_driver_checks`, one entry per real robot with an SSH connection. Nothing here contacts a
// device, except through what already exists: "重新核对" reads the device profile again (issue #27),
// "交给修复助手" goes through the hardware page's own scan and repair form (issue #2).
//
// A robot's card gets an entry only while something is missing: a device with its drivers shows nothing.
//
// Loaded after hardware.js, repair_policy.js, body_types.js, live_status.js and device_profile.js. It wraps
// engineRobotCard / engineAction / engineRefresh / dialog and the profile dialog's body and read.

const MD_PREFIX = 'motion-driver:';
const MD_STATE = {present: ['在', 'good'], missing: ['缺少', 'bad'], wrong_type: ['类型不对', 'bad'],
                  unverified: ['核对不了', ''], unchecked: ['没有核对', '']};
const MD_KIND = {topic: '话题', action: '动作'};
const MD_ACTIONS = ['engine-drivers', 'engine-drivers-recheck', 'engine-drivers-repair'];
const MD_FOUND_SHOWN = 3;
const MD_SCOPE = '只核对接口在不在 ROS 2 的清单里：按消息类型认，名称可以不同；不核对是谁在发布或订阅。全程只读，不发布运动指令。';
// What handing it over leads to today. Installing and starting motion drivers is not allowed to the assistant yet.
const MD_ASSISTANT = '修复助手目前不会自己安装或启动运控驱动（驱动会带起底盘或电机）：它会查清设备上缺什么、为什么没有运行，并写明需要你提供或在现场处理的事。';
// The open dialog: {robotId, busy, failure}. null when none is open.
let mdView = null;
let mdBaseAction = null;

const mdCheck = robotId => (state.motion_driver_checks || []).find(c => c.robot_id === robotId) || null;
const mdLacking = check => (check?.items || []).filter(i => i.state === 'missing' || i.state === 'wrong_type');
const mdIssueIds = check => mdLacking(check).map(i => i.issue_id);
const mdSameIds = (a, b) => a.length === b.length && [...a].sort().join('\n') === [...b].sort().join('\n');
const mdTitles = items => items.map(i => h(i.title)).join('、');
// The problems of this kind that wait for a person (issue #13): the assistant is not given them again.
const mdWaiting = (robotId, check) => mdLacking(check).filter(i => typeof rpLocked === 'function' && rpLocked(robotId, i.issue_id));

// One sentence on where the device stands. Escaped: names of types are written by people.
function mdSummary(check) {
  const lacking = mdLacking(check), type = check.body_type ? h(check.body_type.name) : '';
  if (check.status === 'missing') {
    return `按本体类型“${type}”，这台设备缺 ${lacking.length} 项运控接口：${mdTitles(lacking)}。运控驱动没有安装，或者没有在运行。`;
  }
  if (check.status === 'ok') {
    const checked = check.items.filter(i => i.state === 'present'), unverified = check.items.filter(i => i.state === 'unverified');
    return `按本体类型“${type}”应有的 ${checked.length} 项运控接口都在 ROS 2 清单里。` +
      (unverified.length ? `另有 ${unverified.length} 项平台核对不了（${mdTitles(unverified)}）。` : '');
  }
  return h(check.reason);
}

// ---------------------------------------------------------------- the robot card

// Only a device that lacks something gets the tag and the button.
function mdCard(html, robot) {
  if (robot.sim || !robot.connection_id) return html;
  const check = mdCheck(robot.id), lacking = mdLacking(check);
  if (check?.status !== 'missing' || !lacking.length) return html;
  const bottom = '<div class="card-bottom"><div class="button-row">';
  return html.replace('<div class="tags">', () => '<div class="tags">' + badge(`运控驱动缺 ${lacking.length} 项`, 'bad'))
    .replace(bottom, () => bottom + button('运控驱动', 'engine-drivers', 'secondary small', robot.id));
}

// ---------------------------------------------------------------- the dialog

function mdExpected(item) {
  if (!item.kind) return `<span class="md-none">${h(item.note)}</span>`;
  return `<span class="cell-main">${MD_KIND[item.kind]} <span class="dp-name">${h(item.name)}</span></span>` +
    item.types.map(type => `<span class="dp-type">${h(type)}</span>`).join('');
}

function mdFound(item) {
  if (item.state === 'present') {
    const names = item.found.slice(0, MD_FOUND_SHOWN).map(row => `<span class="dp-name">${h(row.name)}</span>`).join('、');
    return `<span class="cell-sub md-found">${names}${item.found_total > MD_FOUND_SHOWN ? ` 等 ${item.found_total} 个` : ''}</span>`;
  }
  if (item.state === 'wrong_type' && item.found[0]) {
    return `<span class="cell-sub md-found">${h(item.found[0].name)} 的类型是 ${item.found[0].types.map(h).join('、')}</span>`;
  }
  return '';
}

function mdRow(item, waiting) {
  const [text, cls] = MD_STATE[item.state] || MD_STATE.unchecked;
  return `<tr class="md-row md-${h(item.state)}"><td><span class="cell-main">${h(item.title)}</span><span class="cell-sub">${h(item.capability)}</span></td>` +
    `<td class="md-wrap">${mdExpected(item)}</td><td class="md-wrap">${badge(text, cls)}` +
    `${waiting ? '<span class="tag md-waiting">等待现场处理</span>' : ''}${mdFound(item)}</td></tr>`;
}

function mdButtons(robot, check, ui) {
  const profile = button('设备档案', 'engine-profile', 'secondary small', robot.id);
  if (check.status === 'unlisted_type') return button('修改本体类型', 'engine-body-type', 'small', robot.id) + profile;
  if (check.status === 'not_read') return button('读取设备档案', 'engine-profile', 'small', robot.id);
  const recheck = button(ui.busy ? '正在重新读取…' : '重新核对', 'engine-drivers-recheck', 'secondary small', robot.id, ui.busy);
  if (check.status !== 'missing') return recheck + profile;
  const waiting = mdWaiting(robot.id, check).length === mdLacking(check).length;
  const running = typeof hwRunning === 'function' && hwRunning(robot.id);
  return button('交给修复助手', 'engine-drivers-repair', 'small', robot.id, ui.busy || waiting || !!running) + recheck +
    (waiting ? button('硬件初始化', 'hardware-open', 'secondary small', robot.id) : '') + profile;
}

function mdBody(robot, check, ui) {
  const when = check.checked_at ? `<span class="ls-updated" role="status">ROS 2 清单读取于 ${liveWhen(check.checked_at)}</span>` : '';
  const head = '<div class="ls-bar"><div class="tags"><span class="tag">真机 · 只读</span><span class="tag">不发布运动指令</span></div>' +
    `<div class="ls-refresh">${when}</div></div>`;
  const failure = ui.failure ? notice('这一次没有重新读到设备档案：' + h(ui.failure), true) : '';
  const stale = check.problem && check.checked_at
    ? `<p class="inline-note md-stale">最近一次读取 ROS 2 没有成功（${h(check.problem)}），下面按之前读到的清单核对。</p>` : '';
  const waiting = check.status === 'missing' ? mdWaiting(robot.id, check) : [];
  const waitingNote = waiting.length
    ? notice(`${mdTitles(waiting)}：助手处理过，结论是需要人处理。说明在“硬件初始化”里；现场装好并启动驱动后重新核对，接口出现就不再列为问题。`, true) : '';
  const running = typeof hwRunning === 'function' && hwRunning(robot.id);
  const runningNote = running ? notice('助手正在处理这台机器人的硬件问题。 ' + button('查看进度', 'engine-session', 'small', running.id), true) : '';
  const rows = check.items.map(item => mdRow(item, waiting.includes(item)));
  return head + failure + notice(mdSummary(check), check.status !== 'ok') + stale + waitingNote + runningNote +
    (rows.length ? table(['接口', '应有的话题或动作', '这台设备'], rows) + `<p class="inline-note md-scope">${MD_SCOPE}</p>` : '') +
    (check.status === 'missing' ? `<p class="inline-note md-assistant">${MD_ASSISTANT}</p>` : '') +
    `<div class="button-row md-actions">${mdButtons(robot, check, ui)}</div>`;
}

const mdShown = ui => Boolean(ui) && mdView === ui && $('#dialog').open &&
  $('#dialog').dataset.motionDrivers === ui.robotId && $('#motion-drivers');
const mdSignature = robotId => JSON.stringify([mdCheck(robotId), typeof rpSignature === 'function' ? rpSignature(robotId) : '',
                                               typeof hwRunning === 'function' ? hwRunning(robotId)?.id : '']);

function mdRender() {
  const ui = mdView;
  if (!mdShown(ui)) return;
  const robot = find('robots', ui.robotId), check = mdCheck(ui.robotId);
  if (!robot || !check) return $('#dialog').close();
  const scroll = $('#dialog').scrollTop;
  $('#motion-drivers').innerHTML = mdBody(robot, check, ui);
  $('#dialog').dataset.motionDriversSig = mdSignature(ui.robotId);
  $('#dialog').scrollTop = scroll;
}

function mdOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  if (!mdCheck(robotId)) return toast('这台机器人没有通过 SSH 接入，不核对运控驱动');
  dialog(h(robot.name), '运控驱动检查 · 按本体类型核对应有的话题和动作',
         '<div class="dialog-body live-status motion-drivers" id="motion-drivers"></div>');
  $('#dialog').dataset.motionDrivers = robotId;
  mdView = {robotId, busy: false, failure: ''};
  mdRender();
}

// "重新核对" reads the device profile again: that is where the ROS 2 listing comes from.
async function mdRecheck() {
  const ui = mdView;
  if (!mdShown(ui) || ui.busy) return;
  ui.busy = true;
  ui.failure = '';
  mdRender();
  try {
    await api('engineering/profile/read', {project_id: projectId, robot_id: ui.robotId});
  } catch (err) {
    ui.failure = err.message;
  }
  try { await refresh(true); } catch (err) { ui.failure = ui.failure || err.message; }
  ui.busy = false;
  mdRender();
}

// "交给修复助手": the problems are handed over the way every hardware problem is. The repair form works on a
// scan record, so one that lists these problems is made first when the latest does not.
async function mdRepair() {
  const ui = mdView;
  if (!mdShown(ui) || ui.busy) return;
  const robotId = ui.robotId, wanted = mdIssueIds(mdCheck(robotId));
  if (!wanted.length) return mdRender();
  const listed = (hwLatest(robotId)?.issues || []).filter(i => i.id.startsWith(MD_PREFIX)).map(i => i.id);
  if (mdSameIds(wanted, listed)) hardwareOpen(robotId);
  else await hardwareAction('hardware-scan', robotId);
  if (!$('#dialog .hw-issue-list')) return;       // the scan did not finish: its own dialog says why
  let chosen = 0;
  document.querySelectorAll('#dialog [name=hw-issue]').forEach(box => {
    if (box.disabled) return;
    box.checked = box.value.startsWith(MD_PREFIX);
    chosen += box.checked ? 1 : 0;
  });
  if (chosen) hardwareRepairForm(robotId);
}

function mdAction(type, id) {
  if (type === 'engine-drivers') return mdOpen(id);
  if (type === 'engine-drivers-recheck') return mdRecheck();
  return mdRepair();
}

// ---------------------------------------------------------------- the device profile dialog

// A line above the profile's tabs: what the check made of the listing that is shown below.
const mdBannerSig = check => [check.status, check.checked_at, check.family, mdIssueIds(check).join(',')].join('|');

function mdBanner(robotId) {
  const check = mdCheck(robotId);
  if (!check || check.status === 'not_read') return '';
  const open = button(check.status === 'missing' ? '查看并处理' : '查看', 'engine-drivers', 'secondary small', robotId);
  const control = check.status === 'unlisted_type' ? button('修改本体类型', 'engine-body-type', 'secondary small', robotId) : open;
  return `<div class="md-banner md-${h(check.status)}" id="md-banner" data-sig="${h(mdBannerSig(check))}">` +
    `<span class="md-banner-text"><b>运控驱动</b>${mdSummary(check)}</span>${control}</div>`;
}

function mdProfileBody(html, ui) {
  if (!ui.profile) return html;
  const tabs = '<div class="ls-tabs"';
  return html.replace(tabs, () => mdBanner(ui.robotId) + tabs);
}

// ---------------------------------------------------------------- the hardware page (issue #2)

// In the list of problems these are not ticked in advance and not called "可远程处理": the assistant only
// diagnoses them this round. They can still be ticked and handed over.
const MD_ROW_TAG = '助手只排查，安装要人处理';
function mdIssueRow(html, issue) {
  if (!String(issue.id).startsWith(MD_PREFIX)) return html;
  const value = `value="${h(issue.id)}"`;
  return html.replace(`${value} checked`, () => value)
    .replace('<span class="tag">可远程处理</span>', () => `<span class="tag md-row-tag">${MD_ROW_TAG}</span>`);
}

{
  const baseCard = engineRobotCard, baseDialog = dialog, baseRefresh = engineRefresh;
  if (typeof hwIssueRow === 'function') {
    const baseRow = hwIssueRow;
    hwIssueRow = (issue, robotId, locked) => mdIssueRow(baseRow(issue, robotId, locked), issue);
  }
  mdBaseAction = engineAction;
  engineRobotCard = r => mdCard(baseCard(r), r);
  engineAction = async (type, id) => (MD_ACTIONS.includes(type) && state.user && project()
    ? mdAction(type, id) : mdBaseAction(type, id));
  // Any other dialog replaces this one: a read that finishes later must not draw over it.
  dialog = (...args) => { delete $('#dialog').dataset.motionDrivers; delete $('#dialog').dataset.motionDriversSig; return baseDialog(...args); };
  // The state refresh: redraw the open dialog and the profile's line only when what they show changed.
  engineRefresh = () => {
    baseRefresh();
    const ui = mdView;
    if (mdShown(ui) && !ui.busy && $('#dialog').dataset.motionDriversSig !== mdSignature(ui.robotId)) mdRender();
    const banner = $('#md-banner'), robotId = $('#dialog').dataset.deviceProfile, check = robotId && mdCheck(robotId);
    if (banner && check && $('#dialog').open && banner.dataset.sig !== mdBannerSig(check)) banner.outerHTML = mdBanner(robotId);
  };
  if (typeof profileBody === 'function') {
    const baseBody = profileBody, baseRead = profileRead;
    profileBody = ui => mdProfileBody(baseBody(ui), ui);
    // The check comes with the state: fetch it as soon as a profile was read, then draw the line.
    profileRead = async () => { await baseRead(); try { await refresh(true); } catch (err) { /* the next poll brings it */ } profileRender(); };
  }
}
$('#dialog').addEventListener('close', () => { mdView = null; });
render();
