// Pure-logic checks of web/runtime_link.js (issue #17): which robots get "接入管理层", what the line on a
// robot's card says while its link is up, down or refused, and what the form sends. The rules themselves
// are in brainos/runtime_link.py, checked in tests/test_runtime_link.py and test_runtime_link_reconnect.py.
//   node tests/runtime_link_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps functions of earlier scripts when it loads; stand-ins are enough here. The helpers of
// web/app.js it uses are written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const field = (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`;
const calls = [], forms = [], toasts = [];
let failNext = null, refreshes = 0, closed = 0;
const ssh = {id: 'robot-1', name: 'dog', connection_id: 'connection-1', endpoint: ''};
const linked = {id: 'robot-2', name: 'arm', connection_id: 'connection-2', endpoint: 'ssh-forward:robot-2', runtime_link: {port: 8790}};
const sandbox = {
  h, badge, button, field, projectId: 'project-1', setInterval: () => 0,
  state: {user: {id: 'u'}, robots: [ssh, linked],
          device_connections: [{id: 'connection-1', username: 'robot', host: '10.0.0.7', port: 22},
                               {id: 'connection-2', username: 'arm', host: 'arm.example', port: 2222}]},
  project: () => ({id: 'project-1'}),
  find: (kind, id) => sandbox.state[kind].find(x => x.id === id),
  form: (...args) => { forms.push(args); },
  toast: text => { toasts.push(text); },
  refresh: async () => { refreshes += 1; },
  $: () => ({close: () => { closed += 1; }}),
  api: async (path, payload) => {
    calls.push([path, payload]);
    if (failNext) { const error = failNext; failNext = null; throw error; }
    if (path === 'robots/runtime-link/state') return {links: {}};
    if (path === 'robots/runtime-link/retry') return {robot_id: payload.robot_id, link: {state: 'up', port: 8790, error: null}};
    return {};
  },
  // As web/engineering.js draws a card after ssh_edit.js replaced the last button.
  engineRobotCard: r => `<article class="card"><div class="card-body"><h3>${h(r.name)}</h3><p>四足机器人</p><div class="tags"><span class="tag">控制通道</span></div></div>` +
    `<div class="card-bottom"><div class="button-row">${r.connection_id ? button('配置连接', 'engine-ssh-edit', 'secondary small', r.id) : button('适配器配置', 'robot-config', 'secondary small', r.id)}</div></div></article>`,
  engineAction: async (type, id) => ['base', type, id],
  engineSubmit: async (kind, p, f) => ['base', kind],
  engineRefresh: () => {},
};
vm.createContext(sandbox);
const api = vm.runInContext(readFileSync('web/runtime_link.js', 'utf8') +
  '\n;({rlLinked, rlCanLink, rlPort, rlDuration, rlFacts, rlLine, rlCard, rlRetrying, card: r => engineRobotCard(r),' +
  ' action: (t, i) => engineAction(t, i), submit: (k, p, f) => engineSubmit(k, p, f), setLinks: v => { rlLinks = v; }})', sandbox);
let checks = 0;
// What the page script built lives in another realm (the VM): compared by value, not by prototype.
const byValue = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const check = (actual, expected, what) => { assert.deepEqual(byValue(actual), byValue(expected), what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const count = (text, part) => String(text).split(part).length - 1;

const now = new Date(2026, 10, 21, 10, 30, 0).getTime();
const ago = seconds => now / 1000 - seconds;
const link = (state, fields = {}) => ({state, port: 8790, since: ago(5), down_since: null, error: null, retry_in: null, ...fields});

(async () => {
  // Which robots: reached over SSH, real, and not run by the platform itself.
  check(api.rlCanLink(ssh), true, 'a robot reached over SSH can be linked');
  check(api.rlCanLink({id: 'r', endpoint: 'http://10.0.0.5:8790'}), false, 'a robot without an SSH connection cannot');
  check(api.rlCanLink({id: 'r', connection_id: 'c', sim: {kind: 'quadruped'}}), false, 'a simulated robot cannot');
  check(api.rlCanLink({id: 'r', connection_id: 'c', managed_resource_id: 'res'}), false, 'a platform resource cannot');
  check(api.rlCanLink(null), false, 'no robot');
  check(api.rlLinked(linked), true, 'its endpoint names the robot itself');
  check(api.rlLinked(ssh), false, 'not linked yet');
  check(api.rlLinked({...linked, endpoint: 'ssh-forward:robot-9'}), false, 'an endpoint that names another robot is not a link');
  check(api.rlLinked({...linked, endpoint: 'http://127.0.0.1:18790'}), false, 'an address typed by hand is not a link');
  check(api.rlLinked({...linked, connection_id: undefined}), false, 'no SSH connection, no link');

  // The port a person types: an unprivileged one, 8790 when left empty.
  check(api.rlPort(''), 8790, 'empty means the standard port');
  check(api.rlPort(' 18790 '), 18790, 'another port');
  check(api.rlPort('1024'), 1024, 'the lowest');
  check(api.rlPort('65535'), 65535, 'the highest');
  for (const bad of ['22', '80', '1023', '65536', '0', '-1', '8790.5', '87 90', 'abc', '8790;22', '1e4', '999999'])
    check(api.rlPort(bad), null, `not a port of the management layer: ${bad}`);

  // How long.
  check(api.rlDuration(0), '1 秒', 'just now');
  check(api.rlDuration(12), '12 秒', 'seconds');
  check(api.rlDuration(60), '1 分钟', 'a minute');
  check(api.rlDuration(3600 + 5 * 60), '1 小时 5 分', 'hours and minutes');
  check(api.rlDuration(NaN), '', 'no time');

  // The card of a robot reached over SSH that is not linked: one more button, no line.
  let html = api.card(ssh);
  includes(html, 'data-action="engine-runtime-link"', 'the way to link it');
  includes(html, '接入管理层', 'named for what it does');
  excludes(html, 'rl-line', 'nothing to say about a link that does not exist');
  includes(html, 'engine-ssh-edit', 'the SSH connection keeps its own button');
  check(count(html, '</article>'), 1, 'the card is still one card');
  // A robot that is not reached over SSH keeps its card as it was.
  const plain = {id: 'robot-3', name: 'cart', endpoint: 'http://10.0.0.9:8790'};
  check(api.card(plain), sandbox.rlCard(api.card(plain), plain), 'its card is not touched');
  excludes(api.card(plain), 'engine-runtime-link', 'no SSH connection: no link offered');
  includes(api.card(plain), 'robot-config', 'it keeps the form where an address is typed');
  // A card without the button of ssh_edit.js still gets the way in.
  const bare = sandbox.rlCard('<article class="card"><div class="card-bottom"><div class="button-row"></div></div></article>', ssh);
  includes(bare, 'engine-runtime-link', 'the button is added at the end of the row');
  check(bare.endsWith('</div></div></article>'), true, 'and the card stays well formed');

  // A linked robot before the backend has said how the link is: what it is, and no verdict.
  html = api.card(linked);
  includes(html, 'data-rl-robot="robot-2"', 'the line of this robot');
  includes(html, '管理层设置', 'the button now opens the settings');
  includes(html, '到机器人本机 8790 端口', 'where it goes');
  excludes(html, '已连通', 'no state is claimed before it is known');
  excludes(html, '已断开', 'nor a failure');
  check(html.indexOf('rl-line') < html.indexOf('class="tags"'), true, 'the line sits above the tags');

  // 已连通.
  html = api.rlLine('robot-2', link('up'), now);
  includes(html, '<span class="status good">', 'up is green');
  includes(html, '已连通', 'the state');
  includes(html, '经 SSH 通道到机器人本机 8790 端口', 'the way it takes');
  excludes(html, 'engine-runtime-link-retry', 'nothing to retry');

  // 待连接: closed after a while without use; the next request opens it.
  html = api.rlLine('robot-2', link('idle'), now);
  includes(html, '待连接', 'idle');
  includes(html, '用到时自动连接', 'it opens by itself');
  excludes(html, 'engine-runtime-link-retry', 'idle is not a fault');

  // 连接中.
  html = api.rlLine('robot-2', link('connecting'), now);
  includes(html, 'hb-checking', 'in progress');
  includes(html, '正在建立 SSH 通道', 'what is being done');

  // 需要重新接入: the SSH connection now leads to another machine.
  html = api.rlLine('robot-2', link('moved'), now);
  includes(html, '<span class="status bad">', 'moved is red');
  includes(html, '需要重新接入', 'the state');
  includes(html, '保存的令牌不会发给它', 'why');
  includes(html, '管理层设置', 'where to go');
  excludes(html, 'engine-runtime-link-retry', 'trying again would not help');

  // 已断开: since when, why, what a request gets meanwhile, and that nobody has to do anything.
  const down = link('down', {down_since: ago(12), retry_in: 4,
                             error: {hop: 'device', reason: 'unreachable', message: 'SSH 连接超时或设备不可达'}});
  html = api.rlLine('robot-2', down, now);
  includes(html, '<span class="status bad">', 'down is red');
  includes(html, '已持续 12 秒', 'for how long');
  includes(html, '正在自动重连', 'it is being reconnected');
  includes(html, '目标机：SSH 连接超时或设备不可达', 'which hop and why');
  includes(html, '请求不会发出', 'what a request gets meanwhile');
  includes(html, '不需要手工操作', 'nobody has to do anything');
  includes(html, 'data-action="engine-runtime-link-retry"', 'trying now is offered');
  includes(html, 'aria-label="立即重试：连接 arm 的管理层"', 'the button says which robot');
  html = api.rlLine('robot-2', link('down', {down_since: null}), now);
  includes(html, '刚刚断开', 'a drop without a time');

  // 登录被拒绝: not tried again by itself.
  html = api.rlLine('robot-2', link('refused', {down_since: ago(600),
    error: {hop: 'jump', level: 2, reason: 'auth', message: '第 2 级跳板机拒绝登录'}}), now);
  includes(html, '登录被拒绝', 'the state');
  includes(html, '已断开 10 分钟', 'for how long');
  includes(html, '已停止自动重连', 'no more automatic logins');
  includes(html, '跳板机：第 2 级跳板机拒绝登录', 'which hop');
  includes(html, '以免账号被锁', 'why it stopped');
  includes(html, 'engine-runtime-link-retry', 'a person can still ask');

  // While a retry runs the button says so and stays focusable.
  api.rlRetrying.add('robot-2');
  html = api.rlLine('robot-2', down, now);
  includes(html, '正在连接…', 'the retry is running');
  includes(html, 'aria-disabled="true"', 'marked, not disabled');
  excludes(html, ' disabled', 'so the focus stays');
  api.rlRetrying.delete('robot-2');

  // What a device, a jump host or an old record says is shown as text, never as markup.
  html = api.rlLine('robot-2', link('down', {error: {hop: 'device', reason: 'x', message: '<img src=x onerror=alert(1)>'}}), now);
  excludes(html, '<img', 'markup in a reason is escaped');
  includes(html, '&lt;img', 'and shown as text');
  html = api.rlLine('"><script>', link('down'), now);
  excludes(html, '<script>', 'a robot id cannot break out of its attribute');
  html = api.rlLine('robot-2', link('<b>x</b>'), now);
  excludes(html, '<b>x</b>', 'a state this page does not know is text');
  html = api.rlLine('robot-2', link('constructor'), now);
  excludes(html, 'function', 'a state named like a built-in is not looked up');
  html = api.rlLine('robot-2', link('down', {error: {hop: 'toString', reason: 'x', message: 'm'}}), now);
  includes(html, 'SSH 连接：m', 'a hop this page has no name for');
  html = api.rlLine('robot-2', link('up', {port: '<i>'}), now);
  excludes(html, '<i>', 'a port is text');

  // With the states from the backend the card shows them.
  api.setLinks({'robot-2': link('up')});
  includes(api.card(linked), '已连通', 'the state from the backend');

  // The form: the route it will take, the port, an optional token, and no field for an address.
  check(await api.action('engine-runtime-link', 'robot-1'), undefined, 'the action opens the form');
  let [title, sub, fields, kind, submit, attrs] = forms.pop();
  check([title, kind, submit, attrs], ['接入管理层', 'engine-runtime-link', '接入并检查', 'data-id="robot-1"'], 'the form of a robot that is not linked');
  check(sub, 'dog', 'named by the robot');
  includes(fields, 'robot@10.0.0.7:22', 'the SSH connection it will use');
  includes(fields, 'name="port"', 'the port');
  includes(fields, 'value="8790"', 'the standard port is filled in');
  includes(fields, 'name="token"', 'a token may be typed');
  includes(fields, 'type="password"', 'and is not shown');
  includes(fields, '~/neuxmind/edge-token', 'or it is read from the robot');
  excludes(fields, 'name="endpoint"', 'no address is asked for');
  excludes(fields, 'name="host"', 'no host either');
  excludes(fields, 'engine-runtime-link-remove', 'nothing to take away yet');
  await api.action('engine-runtime-link', 'robot-2');
  [title, sub, fields, kind, submit] = forms.pop();
  check([title, submit], ['管理层设置', '保存并检查'], 'the form of a linked robot');
  includes(fields, 'engine-runtime-link-remove', 'the link can be taken away');
  excludes(fields, 'value="ssh-forward', 'the endpoint is not a field');
  await assert.rejects(api.action('engine-runtime-link', 'robot-9'), /没有 SSH 连接/);
  checks += 1;
  check(await api.action('engine-other', 'x'), ['base', 'engine-other', 'x'], 'other actions pass through');

  // Submitting: the robot, the port and, only when typed, the token. Never an address.
  calls.length = 0;
  await api.submit('engine-runtime-link', {port: '8790', token: ''}, {dataset: {id: 'robot-1'}});
  check(calls[0], ['robots/runtime-link', {project_id: 'project-1', robot_id: 'robot-1', port: 8790}], 'no token typed: none sent');
  check(closed, 1, 'the form closes');
  includes(toasts.pop(), '连接检查通过', 'and says it worked');
  calls.length = 0;
  await api.submit('engine-runtime-link', {port: '18790', token: 'typed-token', host: '10.0.0.5', endpoint: 'http://x'},
                   {dataset: {id: 'robot-1'}});
  check(calls[0], ['robots/runtime-link', {project_id: 'project-1', robot_id: 'robot-1', port: 18790, token: 'typed-token'}],
        'a typed token goes along; an address does not');
  calls.length = 0;
  await assert.rejects(api.submit('engine-runtime-link', {port: '22', token: ''}, {dataset: {id: 'robot-1'}}), /1024–65535/);
  check(calls, [], 'a port that cannot be the management layer is not sent');
  // A check that fails keeps the form open with the reason, and the card is drawn again (the link was saved).
  const before = refreshes, closedBefore = closed;
  failNext = Object.assign(new Error('设备上的管理层服务没有在监听'), {reason: 'device_service_down'});
  await assert.rejects(api.submit('engine-runtime-link', {port: '8790', token: ''}, {dataset: {id: 'robot-1'}}), /没有在监听/);
  check([refreshes - before, closed - closedBefore], [1, 0], 'drawn again, not closed');
  check(await api.submit('engine-ssh', {}, {}), ['base', 'engine-ssh'], 'other forms pass through');

  // "立即重试" asks the backend once, and a second press while it runs does nothing.
  calls.length = 0;
  const first = api.action('engine-runtime-link-retry', 'robot-2'), second = api.action('engine-runtime-link-retry', 'robot-2');
  await Promise.all([first, second]);
  check(calls.filter(([path]) => path === 'robots/runtime-link/retry').length, 1, 'one retry');
  includes(toasts.pop(), '通道已连通', 'the answer');

  // Taking the link away.
  calls.length = 0;
  await api.action('engine-runtime-link-remove', 'robot-2');
  check(calls[0], ['robots/runtime-link/remove', {project_id: 'project-1', robot_id: 'robot-2'}], 'which robot');
  includes(toasts.pop(), 'SSH 连接保留', 'what stays');

  console.log(`${checks} runtime link logic checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
