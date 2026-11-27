'use strict';
// Issue #17 — a robot's management layer, reached over the robot's own SSH connection. Nobody opens a
// tunnel or types an address: the backend sends every request through an SSH session to the robot
// (brainos/runtime_link.py), to one port on the robot's own loopback address, and logs in again by
// itself when that session drops.
// What changes over time (connected, dropped, how long ago) is asked for on its own
// (robots/runtime-link/state) and drawn in place. It is not part of the state the page redraws itself
// from: a link that drops must not close an open explanation or take the keyboard focus away.
// Loaded after heartbeat.js and ssh_edit.js. It wraps engineRobotCard, engineAction, engineSubmit and
// engineRefresh instead of editing them, as those files do.

const RL_PREFIX = 'ssh-forward:';
const RL_DEFAULT_PORT = 8790, RL_PORT_MIN = 1024, RL_PORT_MAX = 65535;
const RL_POLL_SECONDS = 5;                // how often the states are asked for; "已断开 12 秒" moves on with it
const RL_BADGE = {up: 'good', down: 'bad', refused: 'bad', moved: 'bad', connecting: 'blue hb-checking', idle: ''};
const RL_LABEL = {up: '已连通', down: '已断开', refused: '登录被拒绝', moved: '需要重新接入', connecting: '连接中', idle: '待连接'};
const RL_HOPS = {jump: '跳板机', device: '目标机'};
const RL_TOKEN_FILE = '~/neuxmind/edge-token';
// Robots whose link is being tried right now because the user clicked "立即重试".
const rlRetrying = new Set();
// {robot id: state of its link} as the backend last sent them.
let rlLinks = {}, rlPolling = false, rlBaseAction = null, rlBaseSubmit = null;

// The robot is reached over SSH and its management layer through that connection.
const rlLinked = r => !!r && !r.sim && !!r.connection_id && r.endpoint === RL_PREFIX + r.id;
// The robot is reached over SSH: its management layer can be reached the same way.
const rlCanLink = r => !!r && !r.sim && !!r.connection_id && !r.managed_resource_id;
const rlLink = robotId => (Object.hasOwn(rlLinks, robotId) ? rlLinks[robotId] : null);
const rlRobotName = robotId => state.robots?.find(r => r.id === robotId)?.name || '';
const rlPortOf = r => (Number.isInteger(r.runtime_link?.port) ? r.runtime_link.port : RL_DEFAULT_PORT);
// Until the backend has said, a linked robot's line says what the link is, not how it is.
const rlUnknown = r => ({state: 'idle', port: rlPortOf(r), since: null, down_since: null, error: null, retry_in: null, unknown: true});

function rlDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} 秒`;
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return hours ? `${hours} 小时` + (minutes ? ` ${minutes} 分` : '') : `${minutes} 分钟`;
}

// The port a person typed, or null when it is not a port the management layer can have.
function rlPort(text) {
  const value = String(text ?? '').trim();
  if (value === '') return RL_DEFAULT_PORT;
  if (!/^[0-9]{1,5}$/.test(value)) return null;
  const port = Number(value);
  return port >= RL_PORT_MIN && port <= RL_PORT_MAX ? port : null;
}

const rlOwn = (table, key, otherwise) => (Object.hasOwn(table, key) ? table[key] : otherwise);
const rlBadge = link => badge(link.unknown ? '经 SSH 通道' : rlOwn(RL_LABEL, link.state, link.state), link.unknown ? '' : rlOwn(RL_BADGE, link.state, ''));
// The facts next to the badge, as plain text.
function rlFacts(link, now = Date.now()) {
  const where = `到机器人本机 ${link.port} 端口`;
  if (link.unknown) return where;
  if (link.state === 'up') return '经 SSH 通道' + where;
  if (link.state === 'idle') return '经 SSH 通道' + where + ' · 用到时自动连接';
  if (link.state === 'connecting') return '正在建立 SSH 通道…';
  // The SSH connection was changed to another machine: the token saved for the old one is not sent to it.
  if (link.state === 'moved') return 'SSH 连接换了一台机器，保存的令牌不会发给它；请在“管理层设置”里重新接入';
  const gone = Number.isFinite(link.down_since) ? rlDuration(now / 1000 - link.down_since) : '';
  // The badge already says 已断开: next to it, for how long.
  if (link.state === 'refused') return (gone ? `已断开 ${gone}` : '刚断开') + ' · 已停止自动重连';
  return (gone ? `已持续 ${gone}` : '刚刚断开') + ' · 正在自动重连';
}

// Why the link is down, what a request gets meanwhile, and what can be done.
function rlTrouble(robotId, link) {
  if (link.unknown || (link.state !== 'down' && link.state !== 'refused')) return '';
  const busy = rlRetrying.has(robotId), name = rlRobotName(robotId);
  // Not `disabled` while it runs: a disabled button drops the keyboard focus. rlRetry ignores a second press.
  const retry = `<button type="button" class="text-link hb-retry rl-retry" data-action="engine-runtime-link-retry" data-id="${h(robotId)}" ` +
    `aria-label="${h(name ? `立即重试：连接 ${name} 的管理层` : '立即重试：连接管理层')}"${busy ? ' aria-disabled="true"' : ''}>${busy ? '正在连接…' : '立即重试'}</button>`;
  const hop = link.error && (Object.hasOwn(RL_HOPS, link.error.hop) ? RL_HOPS[link.error.hop] : 'SSH 连接');
  const note = link.state === 'refused'
    ? '登录被拒绝，已停止自动重连，以免账号被锁。请修改连接，或点“立即重试”。'
    : '这期间发给管理层的请求不会发出，会直接提示失败；连上后自动恢复，不需要手工操作。';
  return retry + (link.error ? `<span class="hb-reason">${hop}：${h(link.error.message)}</span>` : '') +
    `<span class="hb-reason hb-paused">${note}</span>`;
}

// A line is three parts, each redrawn only when it changes: the retry button is not replaced under the
// pointer every time the seconds move on.
const rlStateSig = link => `${link.unknown ? 'unknown' : link.state}`;
const rlTroubleSig = (robotId, link) => (link.unknown || (link.state !== 'down' && link.state !== 'refused') ? '' : JSON.stringify(
  [link.state, rlRetrying.has(robotId), link.error?.hop ?? null, link.error?.message ?? null, rlRobotName(robotId)]));
const rlStatePart = link => `<span class="rl-state" data-rl-sig="${h(rlStateSig(link))}">${rlBadge(link)}</span>`;
const rlTroublePart = (robotId, link) =>
  `<span class="rl-trouble" data-rl-sig="${h(rlTroubleSig(robotId, link))}">${rlTrouble(robotId, link)}</span>`;

// On a robot's card, above its tags. It can take the focus (when its retry button goes away), not by Tab.
function rlLine(robotId, link, now = Date.now()) {
  return `<div class="rl-line" tabindex="-1" data-rl-robot="${h(robotId)}"><span class="rl-label">管理层</span>` +
    rlStatePart(link) + `<span class="rl-facts">${h(rlFacts(link, now))}</span>` + rlTroublePart(robotId, link) + '</div>';
}

// The card of a robot reached over SSH: the way to its management layer, and the state of that way.
function rlCard(html, r) {
  if (!rlCanLink(r)) return html;
  const linked = rlLinked(r);
  const mine = button(linked ? '管理层设置' : '接入管理层', 'engine-runtime-link', 'secondary small', r.id);
  const anchor = button('配置连接', 'engine-ssh-edit', 'secondary small', r.id), end = '</div></div></article>';
  // Functions, not strings: a "$" in a name must not be read as a pattern of String.replace.
  let out = html.includes(anchor) ? html.replace(anchor, () => anchor + mine)
    : html.endsWith(end) ? html.slice(0, -end.length) + mine + end : html;
  if (linked) out = out.replace('<div class="tags">', () => rlLine(r.id, rlLink(r.id) || rlUnknown(r)) + '<div class="tags">');
  return out;
}

// Bring every line on screen up to date, touching only what changed.
function rlPaint(now = Date.now()) {
  if (typeof document === 'undefined') return;
  for (const el of document.querySelectorAll('[data-rl-robot]')) {
    const robotId = el.dataset.rlRobot, link = rlLink(robotId);
    if (!link) continue;
    const stateEl = el.querySelector('.rl-state'), factsEl = el.querySelector('.rl-facts'), troubleEl = el.querySelector('.rl-trouble');
    if (stateEl && stateEl.dataset.rlSig !== rlStateSig(link)) stateEl.outerHTML = rlStatePart(link);
    const facts = rlFacts(link, now);
    if (factsEl && factsEl.textContent !== facts) factsEl.textContent = facts;
    if (troubleEl && troubleEl.dataset.rlSig !== rlTroubleSig(robotId, link)) {
      const focused = troubleEl.contains(document.activeElement);
      troubleEl.outerHTML = rlTroublePart(robotId, link);
      // The focus stays with this robot: on its retry button, or on the line once the link is back.
      if (focused) (el.querySelector('.rl-trouble [data-action]') || el).focus({preventScroll: true});
    }
  }
}

async function rlPoll() {
  if (rlPolling || !state.user) return;
  if (!(state.robots || []).some(rlLinked)) { rlLinks = {}; return; }
  rlPolling = true;
  try {
    rlLinks = (await api('robots/runtime-link/state', {})).links || {};
    rlPaint();
  } catch { /* the lines keep what they showed; the next poll answers */ }
  finally { rlPolling = false; }
}

// "立即重试": one login now, also after a refusal. The answer is drawn in place.
async function rlRetry(robotId) {
  if (rlRetrying.has(robotId)) return;
  rlRetrying.add(robotId);
  rlPaint();
  try {
    const result = await api('robots/runtime-link/retry', {project_id: projectId, robot_id: robotId});
    if (result.link) rlLinks = {...rlLinks, [robotId]: result.link};
    toast(result.link?.state === 'up' ? '通道已连通' : '还没有连上' + (result.link?.error ? '：' + result.link.error.message : ''));
  } finally {
    rlRetrying.delete(robotId);
    rlPaint();
  }
}

function rlOpen(robotId) {
  const r = find('robots', robotId), c = r && find('device_connections', r.connection_id);
  if (!rlCanLink(r) || !c) throw Error('这台机器人没有 SSH 连接；请先通过 SSH 接入');
  const linked = rlLinked(r);
  const route = `${c.username}@${c.host}:${c.port}` + (typeof sshRouteLabel === 'function' ? sshRouteLabel(c) : '');
  form(linked ? '管理层设置' : '接入管理层', h(r.name),
    `<p class="inline-note rl-how">平台经这台机器人已有的 SSH 连接（${h(route)}）访问它本机的管理层服务：不用开隧道，不用填地址，连接断了会自动重连。` +
    '管理层只监听机器人本机，这条通道也只通到下面这个端口。</p>' +
    field('管理层端口', `<input name="port" type="number" inputmode="numeric" min="${RL_PORT_MIN}" max="${RL_PORT_MAX}" step="1" value="${h(rlPortOf(r))}" required>`,
      `管理层在机器人本机监听的端口，标准安装是 ${RL_DEFAULT_PORT}。`) +
    field('令牌（可留空）', '<input name="token" type="password" autocomplete="new-password">',
      `留空：从机器人上标准安装的令牌文件（${RL_TOKEN_FILE}）读取。管理层装在别处时，在这里填写。`) +
    (linked ? `<div class="rl-remove">${button('移除接入', 'engine-runtime-link-remove', 'secondary small', r.id)}<small>保留 SSH 连接，删除保存的令牌；之后可以再接入。</small></div>` : ''),
    'engine-runtime-link', linked ? '保存并检查' : '接入并检查', `data-id="${h(r.id)}"`);
}

async function rlSubmit(p, f) {
  const port = rlPort(p.port);
  if (port === null) throw Error(`管理层端口须为 ${RL_PORT_MIN}–${RL_PORT_MAX} 之间的整数（标准安装是 ${RL_DEFAULT_PORT}）`);
  // No address goes with it: the backend only ever connects to the robot's own loopback.
  const request = {project_id: projectId, robot_id: f.dataset.id, port};
  if (p.token) request.token = p.token;
  try {
    await api('robots/runtime-link', request);
  } finally {
    await refresh(true);          // a check that failed still saved the link: the card shows it, with the reason
    rlPoll();
  }
  $('#dialog').close();
  toast('已经 SSH 通道接入管理层，连接检查通过');
}

async function rlRemove(robotId) {
  await api('robots/runtime-link/remove', {project_id: projectId, robot_id: robotId});
  const {[robotId]: gone, ...rest} = rlLinks;
  rlLinks = rest;
  await refresh(true);
  $('#dialog').close();
  toast('已移除管理层接入，SSH 连接保留');
}

{
  const baseCard = engineRobotCard, baseRefresh = engineRefresh;
  rlBaseAction = engineAction;
  rlBaseSubmit = engineSubmit;
  engineRobotCard = r => rlCard(baseCard(r), r);
  engineAction = async (type, id) => {
    if (type === 'engine-runtime-link-retry') return rlRetry(id);
    if (type === 'engine-runtime-link-remove') return rlRemove(id);
    if (type === 'engine-runtime-link' && state.user) return rlOpen(id);
    return rlBaseAction(type, id);
  };
  engineSubmit = async (kind, p, f) => (kind === 'engine-runtime-link' ? rlSubmit(p, f) : rlBaseSubmit(kind, p, f));
  // The page was drawn again: a robot that is newly linked has no state yet, so ask at once.
  engineRefresh = () => {
    baseRefresh();
    if ((state.robots || []).some(r => rlLinked(r) && !rlLink(r.id))) rlPoll();
    else rlPaint();
  };
}
setInterval(() => {
  if (typeof document !== 'undefined' && document.hidden) return;
  rlPaint();
  rlPoll();
}, RL_POLL_SECONDS * 1000);
if (typeof document !== 'undefined') document.addEventListener('visibilitychange', () => { if (!document.hidden) rlPoll(); });
rlPoll();
