'use strict';
// Issue #24 — whether a robot can be reached right now: 未接入 / 接入中 / 在线 / 离线, and the time of
// its last heartbeat. The backend asks every robot with an SSH connection once a minute, whatever page
// is open (brainos/heartbeat.py). A robot is offline when no heartbeat came for three minutes, not when
// one read failed.
// The states are asked for on their own (engineering/ssh/links) and drawn in place. They are not part of
// the state the page redraws itself from: a heartbeat a minute must not close an open explanation or
// take the keyboard focus away.
// Loaded after live_status.js. It wraps engineRobotCard, liveHead, engineAction and engineRefresh
// instead of editing them, as hardware.js and ssh_edit.js do.

const HB_BADGE = {online: 'good', offline: 'bad', onboarding: 'blue hb-checking', not_onboarded: ''};
const HB_HOPS = {jump: '跳板机', device: '目标机'};
const HB_POLL_SECONDS = 10;               // how often the states are asked for; "已离线 12 分钟" moves on with it
const HB_OFFLINE_AFTER_SECONDS = 180;     // used until the server says (state.heartbeat.offline_after_seconds)
// The badge web/live_status.js puts first in the head of the status page: the outcome of that one read.
const HB_READ_BADGE = /<span class="status [a-z]*"><i class="dot"><\/i>(?:在线|连不上)<\/span>/;
// Robots that are being asked right now because the user clicked "重试".
const hbChecking = new Set();
// {robot id: connection state} as the backend last sent them.
let hbLinks = {}, hbPolling = false;

const hbLink = robotId => (Object.hasOwn(hbLinks, robotId) ? hbLinks[robotId] : null);
const hbRobotName = robotId => state.robots?.find(r => r.id === robotId)?.name || '';
const hbTwo = n => String(n).padStart(2, '0');

// The page is only served to this computer (brainos/server.py answers 127.0.0.1 alone), so its clock is
// the clock the heartbeat times were taken with.
function hbDuration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds < 60) return '不到 1 分钟';
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  if (days) return `${days} 天` + (hours ? ` ${hours} 小时` : '');
  if (hours) return `${hours} 小时` + (minutes ? ` ${minutes} 分` : '');
  return `${minutes} 分钟`;
}

// The time of a heartbeat: with seconds today, with the date on another day.
function hbWhen(seconds, now = Date.now()) {
  if (!Number.isFinite(seconds)) return '—';
  const then = new Date(seconds * 1000), time = `${hbTwo(then.getHours())}:${hbTwo(then.getMinutes())}`;
  if (then.toDateString() === new Date(now).toDateString()) return `${time}:${hbTwo(then.getSeconds())}`;
  return `${hbTwo(then.getMonth() + 1)}-${hbTwo(then.getDate())} ${time}`;
}

const hbBadge = link => badge(link.label || link.state, Object.hasOwn(HB_BADGE, link.state) ? HB_BADGE[link.state] : '');
const hbHeard = (link, now) => Number.isFinite(link.last_heartbeat_at) ? `最近心跳 ${hbWhen(link.last_heartbeat_at, now)}` : '还没有收到过心跳';
// The facts next to the badge, as plain text.
function hbFacts(link, now) {
  if (link.state === 'online') return hbHeard(link, now);
  if (link.state === 'offline') {
    const heard = Number.isFinite(link.last_heartbeat_at);
    return (heard ? `已离线 ${hbDuration(now / 1000 - link.last_heartbeat_at)} · ` : '') + hbHeard(link, now);
  }
  if (link.state === 'onboarding') return (link.step || '正在检查连接') + '…';
  if (link.state === 'not_onboarded') return link.error ? '上一次失败：' + link.error.message : '还没有连接过';
  return '';
}

// Why no heartbeat comes, and what can be done. Only once the robot counts as offline: one missed heartbeat is no fault.
function hbTrouble(robotId, link) {
  if (link.state !== 'offline') return '';
  const busy = hbChecking.has(robotId), name = hbRobotName(robotId);
  // Not `disabled` while it runs: a disabled button drops the keyboard focus. hbCheck ignores a second press.
  const retry = `<button type="button" class="text-link hb-retry" data-action="engine-heartbeat-check" data-id="${h(robotId)}" ` +
    `aria-label="${h(name ? `重试：检查 ${name} 的连接` : '重试：检查连接')}"${busy ? ' aria-disabled="true"' : ''}>${busy ? '正在检查…' : '重试'}</button>`;
  const hop = link.error && (Object.hasOwn(HB_HOPS, link.error.hop) ? HB_HOPS[link.error.hop] : '连接');
  return retry + (link.error ? `<span class="hb-reason">${hop}：${h(link.error.message)}</span>` : '') +
    (link.retrying === false ? '<span class="hb-reason hb-paused">登录被拒绝，已停止自动重试，以免账号被锁。请修改连接，或点“重试”。</span>' : '');
}

// A line is three parts, each redrawn only when it changes: the retry button is not replaced under the
// pointer every time the minutes move on.
const hbStateSig = link => `${link.state}|${link.label || ''}`;
// What the trouble part depends on. Nothing is shown unless the robot is offline, so nothing else counts then.
const hbTroubleSig = (robotId, link) => (link.state !== 'offline' ? '' : JSON.stringify(
  [hbChecking.has(robotId), link.error?.hop ?? null, link.error?.message ?? null, link.retrying !== false, hbRobotName(robotId)]));
const hbStatePart = link => `<span class="hb-state" data-hb-sig="${h(hbStateSig(link))}">${hbBadge(link)}</span>`;
const hbFactsPart = (link, now, cls) => `<span class="${cls}">${h(hbFacts(link, now))}</span>`;
const hbTroublePart = (robotId, link) =>
  `<span class="hb-trouble" data-hb-sig="${h(hbTroubleSig(robotId, link))}">${hbTrouble(robotId, link)}</span>`;

// On a robot's card, under its name. It can take the focus (when its retry button goes away), not by Tab.
function hbLine(robotId, link, now = Date.now()) {
  return `<div class="hb-line" tabindex="-1" data-hb-robot="${h(robotId)}" data-hb-view="card">` +
    hbStatePart(link) + hbFactsPart(link, now, 'hb-facts') + hbTroublePart(robotId, link) + '</div>';
}

// On the status page a read that has just reached the machine is an answer from it: online as of that read,
// until the states catch up a few seconds later. A read from long ago says nothing about now.
function hbAfterRead(link, reachable, readAt, now) {
  const offlineAfter = state.heartbeat?.offline_after_seconds || HB_OFFLINE_AFTER_SECONDS;
  const newer = Number.isFinite(readAt) && readAt > (link.last_heartbeat_at ?? 0);
  if (!reachable || !newer || now / 1000 - readAt >= offlineAfter) return link;
  return {...link, state: 'online', label: '在线', error: null, last_heartbeat_at: readAt};
}

// In the head of the status page, in place of the badge of the read.
function hbHead(robotId, link, view, now = Date.now()) {
  const readAt = Number(view.checked_at) || 0, shown = hbAfterRead(link, view.reachable === true, readAt, now);
  return `<span class="hb-head" data-hb-robot="${h(robotId)}" data-hb-view="head" data-hb-reachable="${view.reachable ? 1 : 0}" ` +
    `data-hb-read="${readAt}">${hbStatePart(shown)}${hbFactsPart(shown, now, 'tag hb-facts')}</span>`;
}

// Bring every state on screen up to date, touching only what changed.
function hbPaint(now = Date.now()) {
  if (typeof document === 'undefined') return;
  for (const el of document.querySelectorAll('[data-hb-robot]')) {
    const robotId = el.dataset.hbRobot, stored = hbLink(robotId);
    if (!stored) continue;
    const link = el.dataset.hbView === 'head'
      ? hbAfterRead(stored, el.dataset.hbReachable === '1', Number(el.dataset.hbRead), now) : stored;
    const stateEl = el.querySelector('.hb-state'), factsEl = el.querySelector('.hb-facts'), troubleEl = el.querySelector('.hb-trouble');
    if (stateEl && stateEl.dataset.hbSig !== hbStateSig(link)) stateEl.outerHTML = hbStatePart(link);
    const facts = hbFacts(link, now);
    if (factsEl && factsEl.textContent !== facts) factsEl.textContent = facts;
    if (troubleEl && troubleEl.dataset.hbSig !== hbTroubleSig(robotId, link)) {
      const focused = troubleEl.contains(document.activeElement);
      troubleEl.outerHTML = hbTroublePart(robotId, link);
      // The focus stays with this robot: on its retry button, or on the line once the robot is back and the button is gone.
      if (focused) (el.querySelector('.hb-trouble [data-action]') || el).focus({preventScroll: true});
    }
  }
}

// New states from the backend. A robot that gained or lost its line needs its card drawn again; anything
// else (a heartbeat, online to offline and back) is drawn in place.
function hbSetLinks(links) {
  const ids = of => Object.keys(of).sort().join('\n'), changed = ids(links) !== ids(hbLinks);
  hbLinks = links;
  if (changed && current === 'robots') render();
  else hbPaint();
}

async function hbPoll() {
  if (hbPolling || !state.user) return;
  hbPolling = true;
  try {
    hbSetLinks((await api('engineering/ssh/links', {})).links || {});
  } catch { /* the lines keep what they showed, with the time it was true at; the next poll answers */ }
  finally { hbPolling = false; }
}

// Robots whose card should carry a line (as brainos/heartbeat.py decides): reached over SSH, or not connected at all.
const hbExpected = () => (state.robots || []).filter(r => !r.sim && (r.connection_id || r.status !== 'connected'));

// "重试": ask the robot now. The answer is drawn in place; the keyboard focus stays where it was.
async function hbCheck(robotId) {
  if (hbChecking.has(robotId)) return;
  hbChecking.add(robotId);
  hbPaint();
  try {
    const result = await api('engineering/ssh/heartbeat', {project_id: projectId, robot_id: robotId});
    if (result.link) hbLinks = {...hbLinks, [robotId]: result.link};
    const online = result.link?.state === 'online';
    toast(result.checking ? '还在检查，结果稍后会显示在卡片上' : online ? '收到心跳，机器人在线' : '还是没有收到心跳' +
      (result.link?.error ? '：' + result.link.error.message : ''));
  } finally {
    hbChecking.delete(robotId);
    hbPaint();
  }
}

{
  const baseCard = engineRobotCard, baseHead = liveHead, baseAction = engineAction, baseRefresh = engineRefresh;
  // The line is put in with a function: a "$" in a reason must not be read as a pattern of String.replace.
  engineRobotCard = r => {
    const html = baseCard(r), link = hbLink(r.id);
    return link ? html.replace('</h3>', () => '</h3>' + hbLine(r.id, link)) : html;
  };
  liveHead = view => {
    const html = baseHead(view), link = hbLink(view.robot_id);
    if (!link || view.reachable === null) return html;          // while the first connection is made the page says so itself
    return html.replace(HB_READ_BADGE, () => hbHead(view.robot_id, link, view));
  };
  engineAction = async (type, id) => (type === 'engine-heartbeat-check' ? hbCheck(id) : baseAction(type, id));
  // The page was drawn again: a robot that is new on it has no state yet, so ask at once instead of at the next poll.
  engineRefresh = () => {
    baseRefresh();
    if (hbExpected().some(r => !hbLink(r.id))) hbPoll();
    else hbPaint();
  };
}
setInterval(() => {
  if (typeof document !== 'undefined' && document.hidden) return;
  hbPaint();
  hbPoll();
}, HB_POLL_SECONDS * 1000);
if (typeof document !== 'undefined') document.addEventListener('visibilitychange', () => { if (!document.hidden) hbPoll(); });
hbPoll();
