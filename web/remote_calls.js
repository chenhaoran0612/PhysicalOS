'use strict';
// Issue #43: the 「远程调用」 section of a robot's page. Third parties can call a skill of this robot
// through the cloud's open platform; the robot runs such a call only if its owner switched
// "允许远程调用" on for that skill. The section shows whether the robot is linked to the cloud, one
// switch per installed skill and the recent remote calls.
//  * Switching on asks first, in a dialog of its own; for a skill that moves the robot the dialog says
//    plainly that third parties will be able to make it move without anyone confirming each time.
//    Switching off is immediate.
//  * The link state and the calls change by themselves. They come from their own endpoint
//    (robots/remote), never from the workspace snapshot, and are redrawn in place: a switch that has
//    the focus, an open confirmation and the scroll position stay as they are.
// Loaded after web/cloud_skills.js (subDialog, dialogError). It wraps robotDetailRender instead of
// editing it. Skill titles and call records come from other parties: all of it goes through h().

const REMOTE_POLL_MS = 5000;
const REMOTE_UNSUPPORTED = '这台机器人的管理层还不支持远程调用';
const REMOTE_NO_RUNTIME = '连上这台机器人的管理层并通过连接检查后，才能在这里设置远程调用。';
const REMOTE_INTRO = '第三方可以经云平台的开放接口调用这台机器人上的技能。只有打开了“允许远程调用”的技能才会被执行，随时可以关掉；停止始终可用。';
const REMOTE_CALL_STATES = {running: ['运行中', 'blue'], succeeded: ['已完成', 'good'], failed: ['失败', 'bad'], cancelled: ['已取消', ''],
  stopped: ['已停止', 'warn'], rejected: ['机器人拒绝了', 'warn']};
const REMOTE_MOTION_WARNING = '这个技能会让机器人运动。打开后，第三方可以经云平台让这台机器人运动，不需要有人每次确认。';
const REMOTE_PLAIN_NOTE = '打开后，第三方可以经云平台运行这个技能，不需要有人每次确认。这个技能不会让机器人运动。';

let remoteCalls = {robotId: '', view: null, timer: null, pending: false, epoch: 0};

// ---------------------------------------------------------------- small rules (no DOM)
function remoteWhen(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  return new Date(seconds * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
}

// "3 秒前" / "2 分钟前" / a date, for the last time the robot reached the cloud.
function remoteAgo(seconds, now = Date.now() / 1000) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '';
  const passed = Math.max(0, Math.round(now - seconds));
  if (passed < 60) return `${passed} 秒前`;
  if (passed < 3600) return `${Math.floor(passed / 60)} 分钟前`;
  return remoteWhen(seconds);
}

// What the link line says: {text, tone, detail}.
function remoteLinkState(link, now) {
  if (!link || !link.configured) {
    return {text: '没有配置云端连接', tone: '', detail: '这台机器人还没有配置云端连接，第三方现在调用不到它。开关可以先设好，配置好连接后生效。'};
  }
  if (!link.enabled || link.state === 'off') return {text: '云端连接已关闭', tone: '', detail: '这台机器人的云端连接被关掉了，第三方现在调用不到它。'};
  const ago = remoteAgo(link.last_sync_at, now), host = link.host ? `云平台 ${link.host}` : '云平台';
  if (link.state === 'online') return {text: '已连上云端', tone: 'good', detail: `${host}${ago ? '，上次同步 ' + ago : ''}`};
  if (link.state === 'connecting') return {text: '正在连接云端', tone: 'blue', detail: host};
  return {text: '没有连上云端', tone: 'bad', detail: `${host}${link.last_error ? '：' + link.last_error : ''}${ago ? '（上次同步 ' + ago + '）' : ''}`};
}

// The line under a switch: who switched it on, or why it is off again.
function remoteSwitchNote(skill) {
  if (skill.stale) return '这个技能重新安装过，原来的开关已经失效；需要的话请重新打开。';
  if (!skill.allowed) return '';
  const who = skill.set_by ? `由 ${skill.set_by} ` : '';
  return `${who}于 ${remoteWhen(skill.set_at)} 打开`;
}

// The words of the confirmation for switching one skill on.
function remoteConfirmText(skill, robotName) {
  return {title: `允许远程调用「${skill.title}」？`, sub: robotName, warning: skill.motion ? REMOTE_MOTION_WARNING : REMOTE_PLAIN_NOTE,
    motion: Boolean(skill.motion),
    check: skill.motion ? '我了解第三方可以让这台机器人运动，并确认它所在的场地适合这样做' : '我确认允许第三方远程运行这个技能'};
}

// A read that was on its way while a switch was sent shows the state before it: such an answer is dropped.
const remoteAnswerOutdated = (startedAt, epoch) => startedAt !== epoch;

// What switching one skill sends. On: the digest of the installed content the confirmation was about.
function remoteSwitchRequest(robotId, project, skill, allowed, requestId) {
  const ask = {robot_id: robotId, project_id: project, skill_id: skill.id, allowed, confirmed: allowed, request_id: requestId};
  return allowed ? {...ask, digest: skill.digest} : ask;
}

// Why a call did not succeed: the plain words for its code (web/ux04_errors.js), else what the robot said.
function remoteCallNote(call) {
  if (call.error) {
    const known = typeof UX_CODES === 'object' ? UX_CODES[call.error.code] : null;
    return known?.[1] || call.error.message || call.error.code || '';
  }
  return call.finished_at && call.started_at ? `用时 ${Math.max(0, Math.round(call.finished_at - call.started_at))} 秒` : '';
}

// ---------------------------------------------------------------- drawing
function remoteLinkHtml(link) {
  const s = remoteLinkState(link);
  return `${badge(s.text, s.tone)}<span class="remote-link-detail">${h(s.detail)}</span>`;
}

function remoteSwitchHtml(skill) {
  const note = remoteSwitchNote(skill);
  return `<div class="remote-skill" data-remote-skill="${h(skill.id)}"><div class="remote-skill-text"><b>${h(skill.title)}</b> ` +
    `<span class="mono skill-key">${h(skill.id)}@${h(skill.version)}</span> ${skill.motion ? badge('会让机器人运动', 'warn') : ''}` +
    `<p class="remote-skill-note">${h(note)}</p></div>` +
    `<button type="button" class="remote-switch" role="switch" aria-checked="${skill.allowed ? 'true' : 'false'}" ` +
    `data-remote-action="switch" data-id="${h(skill.id)}" aria-label="允许远程调用：${h(skill.title)}">` +
    `<span class="remote-switch-track" aria-hidden="true"><i></i></span><span class="remote-switch-text">允许远程调用</span></button></div>`;
}

function remoteCallsHtml(calls) {
  if (!calls.length) return '<p class="inline-note remote-empty">还没有远程调用。</p>';
  return table(['时间', '技能', '状态', '说明'], calls.map(c => {
    const [text, tone] = REMOTE_CALL_STATES[c.status] || [c.status, ''];
    return `<tr data-remote-call="${h(c.call_id)}"><td>${h(remoteWhen(c.started_at ?? c.finished_at))}</td><td>${h(c.skill_id)}</td>` +
      `<td>${badge(text, tone)}</td><td>${h(remoteCallNote(c))}</td></tr>`;
  }));
}

function remoteBody(view) {
  if (!view) return '<p class="inline-note">正在读取远程调用的设置…</p>';
  if (view.supported === false) return `<p class="inline-note" data-remote-unsupported>${h(view.message || REMOTE_UNSUPPORTED)}</p>`;
  if (view.supported !== true) return `<p class="inline-note" data-remote-unreachable>${h(view.message || '读不到远程调用的状态')}</p>`;
  const skills = view.skills.length ? view.skills.map(remoteSwitchHtml).join('')
    : `<p class="inline-note remote-empty">${h(view.skills_error || '这台机器人还没有安装技能；安装之后可以在这里逐个打开远程调用。')}</p>`;
  return `<div class="remote-link" id="remote-link" aria-live="polite">${remoteLinkHtml(view.link)}</div>` +
    `<div class="remote-skills" id="remote-skills">${skills}</div>` +
    `<h4 class="remote-calls-title">最近的远程调用</h4><div id="remote-call-list">${remoteCallsHtml(view.calls)}</div>`;
}

function remoteSection(robot) {
  const ready = robot.endpoint && robot.status === 'connected';
  return `<section class="remote-section" id="remote-calls" data-remote-robot="${h(robot.id)}"><div class="section-head"><h3>远程调用</h3>` +
    `<span>由机器人的主人逐个技能打开</span></div><p class="inline-note">${h(REMOTE_INTRO)}</p>` +
    `<div id="remote-body">${ready ? remoteBody(null) : `<p class="inline-note" data-remote-unreachable>${h(REMOTE_NO_RUNTIME)}</p>`}</div></section>`;
}

// What changes by itself is redrawn in place; the switches are only touched where their state differs.
function remoteRedraw(view) {
  const body = document.querySelector('#remote-calls #remote-body');
  if (!body) return;
  const drawn = body.querySelector('#remote-link') && view.supported === true &&
    view.skills.length === body.querySelectorAll('[data-remote-skill]').length &&
    view.skills.every(s => body.querySelector(`[data-remote-skill="${CSS.escape(s.id)}"]`));
  if (!drawn) { body.innerHTML = remoteBody(view); return; }
  body.querySelector('#remote-link').innerHTML = remoteLinkHtml(view.link);
  body.querySelector('#remote-call-list').innerHTML = remoteCallsHtml(view.calls);
  for (const skill of view.skills) {
    const row = body.querySelector(`[data-remote-skill="${CSS.escape(skill.id)}"]`), toggle = row.querySelector('.remote-switch');
    const checked = skill.allowed ? 'true' : 'false';
    if (toggle.getAttribute('aria-checked') !== checked) toggle.setAttribute('aria-checked', checked);
    const note = row.querySelector('.remote-skill-note'), text = remoteSwitchNote(skill);
    if (note.textContent !== text) note.textContent = text;
  }
}

// ---------------------------------------------------------------- reading
const remoteShowing = () => $('#dialog').open && document.querySelector(`#remote-calls[data-remote-robot="${CSS.escape(remoteCalls.robotId)}"]`);

async function remoteLoad() {
  const robotId = remoteCalls.robotId;
  if (!robotId || remoteCalls.pending || !remoteShowing()) return;
  remoteCalls.pending = true;
  const epoch = remoteCalls.epoch;
  try {
    const view = await api('robots/remote', {robot_id: robotId, project_id: projectId});
    if (robotId !== remoteCalls.robotId || !remoteShowing() || remoteAnswerOutdated(epoch, remoteCalls.epoch)) return;
    // A read that failed for a moment keeps what was shown; only a first read shows the reason.
    if (view.supported === null && remoteCalls.view?.supported === true) return;
    remoteCalls.view = view;
    remoteRedraw(view);
  } catch (err) {
    if (robotId === remoteCalls.robotId && remoteShowing() && !remoteCalls.view) {
      remoteRedraw({supported: null, message: err.message});
    }
  } finally {
    remoteCalls.pending = false;
  }
}

function remoteStop() {
  clearInterval(remoteCalls.timer);
  remoteCalls = {robotId: '', view: null, timer: null, pending: false, epoch: remoteCalls.epoch + 1};
}

// Puts the section into a robot detail that was just drawn, after the installed skills.
function remoteAttach(robot) {
  const body = $('#dialog-content .dialog-body');
  remoteStop();
  if (!robot || !body) return;
  const anchor = body.querySelector('[data-action="cloud-models-for"]')?.closest('.contract-line');
  if (anchor) anchor.insertAdjacentHTML('beforebegin', remoteSection(robot));
  else body.insertAdjacentHTML('beforeend', remoteSection(robot));
  if (!robot.endpoint || robot.status !== 'connected') return;
  remoteCalls.robotId = robot.id;
  remoteCalls.timer = setInterval(() => { if (remoteShowing()) remoteLoad(); else remoteStop(); }, REMOTE_POLL_MS);
  remoteLoad();
}

// ---------------------------------------------------------------- switching
async function remoteSend(skill, allowed) {
  remoteCalls.epoch += 1;                    // a read on its way now is older than what this returns
  let view;
  try {
    view = await api('robots/remote/skill', remoteSwitchRequest(remoteCalls.robotId, projectId, skill, allowed, newRequestId()));
  } finally {
    remoteCalls.epoch += 1;
  }
  remoteCalls.view = view;
  remoteRedraw(view);
  return view;
}

function remoteConfirmOpen(skill) {
  const robot = find('robots', remoteCalls.robotId), words = remoteConfirmText(skill, robot?.name || '');
  subDialog(h(words.title), h(words.sub),
    `<form data-remote-form="allow" data-skill="${h(skill.id)}" data-digest="${h(skill.digest)}" novalidate><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    notice(h(words.warning), words.motion) +
    `<p class="inline-note">机器人仍然自己检查每一次调用：参数不对、急停锁存、没有填安全限值时照样拒绝。` +
    `会记下是你（${h(state.user?.name || state.user?.email || '')}）打开的；随时可以在这里关掉。</p>` +
    `<label class="hardware-confirm"><input type="checkbox" name="confirmed"><span>${h(words.check)}</span></label>` +
    `</div><div class="dialog-footer"><button type="button" class="button secondary" data-sub-dialog-close>取消</button>` +
    `<button class="button" type="submit">确认打开</button></div></form>`);
}

async function remoteAction(type, id, el) {
  if (type !== 'switch' || !remoteCalls.view) return;
  const skill = remoteCalls.view.skills.find(s => s.id === id);
  if (!skill) return;
  if (el.getAttribute('aria-checked') !== 'true') return remoteConfirmOpen(skill);     // switching on asks first
  el.disabled = true;
  try {
    await remoteSend(skill, false);
    toast(`已关闭「${skill.title}」的远程调用`);
  } finally {
    el.disabled = false;
  }
}

async function remoteSubmit(form) {
  if (!form.elements.confirmed.checked) {
    form.querySelector('.hardware-confirm').classList.add('needs-confirm');
    throw Error('请先勾选下面的确认，再打开。');
  }
  const skillId = form.dataset.skill, skill = {id: skillId, digest: form.dataset.digest};
  try {
    await remoteSend(skill, true);
  } catch (err) {
    // The skill was installed again since the dialog opened: what is shown is no longer what would be allowed.
    if (err.reason === 'skill_changed') { remoteLoad(); form.querySelector('[type="submit"]').remove(); }
    throw err;
  }
  subDialogClose();
  toast('已允许远程调用这个技能');
  document.querySelector(`[data-remote-skill="${CSS.escape(skillId)}"] .remote-switch`)?.focus();
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-remote-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  remoteAction(el.dataset.remoteAction, el.dataset.id, el).catch(err => dialogError($('#dialog'), err));
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-remote-form]');
  if (!form) return;
  e.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  form.querySelector('.form-error').textContent = '';
  remoteSubmit(form).catch(err => dialogError(form, err)).finally(() => { submit.disabled = false; });
});
$('#dialog').addEventListener('close', () => { remoteStop(); subDialogClose(); });

{
  const baseRender = robotDetailRender, baseDialog = dialog;
  robotDetailRender = d => { baseRender(d); remoteAttach(d.robot); };
  // Any other dialog replaces the robot's page: nothing is read for a section that is gone.
  dialog = (...args) => { remoteStop(); return baseDialog(...args); };
}
