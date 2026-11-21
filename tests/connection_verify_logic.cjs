// Pure-logic checks of web/connection_verify.js (issue #16): where the button goes on a robot's card,
// when it is not offered, what the dialog says for a verification that passed (system, architecture,
// time) and for one that got stuck (at which hop), and that one press is one request.
// The verification itself is in brainos/connection_verify.py, checked in tests/test_connection_verify*.py.
//   node tests/connection_verify_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps functions of earlier scripts when it loads; stand-ins are enough here. The helpers of
// web/app.js and web/ssh_jump.js it uses are written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;

const gate = level => ({host: `gate${level}.example.test`, port: 22, username: `gate${level}`, auth: 'password', fingerprint: 'SHA256:x'});
const robots = [
  {id: 'robot-ssh', name: 'dog', project_id: 'project-1', connection_id: 'connection-1'},
  {id: 'robot-both', name: 'arm', project_id: 'project-1', connection_id: 'connection-2', endpoint: 'http://robot.example.test:8790'},
  {id: 'robot-service', name: 'service only', project_id: 'project-1', endpoint: 'http://robot.example.test:8790'},
  {id: 'robot-sim', name: 'sim', project_id: 'project-1', connection_id: 'connection-3', sim: {kind: 'quadruped'}},
];
const connections = [
  {id: 'connection-1', host: 'robot.example.test', port: 22, username: 'robot', jumps: [gate(1), gate(2), gate(3)]},
  {id: 'connection-2', host: 'arm.example.test', port: 2222, username: 'arm'},
];
const links = {}, toasts = [], dialogs = [], calls = [];
let reply = async () => ({}), polls = 0, paints = 0, refreshes = 0;
const sandbox = {
  h, badge, button, projectId: 'project-other', setInterval: () => 1, clearInterval: () => {},
  sshJumpLabel: (level, total) => (total > 1 ? `第 ${level} 级跳板机` : '跳板机'),
  state: {user: {id: 'user-1'}, robots, device_connections: connections, connection_verify: {holds: {}}},
  find: (kind, id) => ({robots, device_connections: connections})[kind].find(x => x.id === id),
  toast: text => { toasts.push(text); },
  dialog: (...args) => { dialogs.push(args); },
  api: (path, payload) => { calls.push([path, payload]); return reply(path, payload); },
  hbLink: id => (Object.hasOwn(links, id) ? links[id] : null),
  hbPaint: () => { paints += 1; },
  hbPoll: () => { polls += 1; },
  refresh: async () => { refreshes += 1; },
  // As web/engineering.js and web/ssh_edit.js draw the buttons of a card.
  engineRobotCard: r => `<article class="card"><h3>${h(r.name)}</h3><div class="card-bottom"><div class="button-row">` +
    (r.connection_id ? button('实时状态', 'engine-status', 'secondary small', r.id) : '') +
    (r.endpoint ? button('连接检查', 'robot-connect', 'secondary small', r.id) : '') +
    (r.connection_id && !r.sim ? button('配置连接', 'engine-ssh-edit', 'secondary small', r.id)
      : button('适配器配置', 'robot-config', 'secondary small', r.id)) + '</div></div></article>',
  engineAction: async (type, id) => ['base', type, id],
};
vm.createContext(sandbox);
const baseCard = sandbox.engineRobotCard;
const page = vm.runInContext(readFileSync('web/connection_verify.js', 'utf8') +
  '\n;({cvSeconds, cvHold, cvHeadline, cvBody, cvSummary, cvRuns, card: r => engineRobotCard(r),' +
  ' action: (t, i) => engineAction(t, i), paint: () => hbPaint()})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const count = (text, part) => String(text).split(part).length - 1;
const [ssh, both, service, sim] = robots;
const VERIFY = 'data-action="engine-ssh-verify"';

// How long it took.
check(page.cvSeconds(0.4), '1 毫秒', 'never zero');
check(page.cvSeconds(320), '320 毫秒', 'on a local network');
check(page.cvSeconds(1234), '1.2 秒', 'one decimal under ten seconds');
check(page.cvSeconds(9999), '10 秒', 'rounded once: not "10.0 秒" next to "10 秒"');
check(page.cvSeconds(9940), '9.9 秒', 'still one decimal just under ten');
check(page.cvSeconds(12345), '12 秒', 'whole seconds');
check(page.cvSeconds(59940), '59 秒', 'whole seconds below a minute');
check(page.cvSeconds(59999), '1 分 0 秒', 'rounded the same way at a minute');
check(page.cvSeconds(65000), '1 分 5 秒', 'minutes and seconds');
check(page.cvSeconds(undefined), '—', 'no time');

// The card of a robot with an SSH connection: one button, in front of 配置连接.
let html = page.card(ssh);
check(count(html, VERIFY), 1, 'one button');
includes(html, '>验证 SSH 连接</button>', 'its name says which check it is');
check(html.indexOf(VERIFY) < html.indexOf('data-action="engine-ssh-edit"'), true, 'in front of 配置连接');
check(html.indexOf(VERIFY) > html.indexOf('data-action="engine-status"'), true, 'after 实时状态');
includes(html, 'SSH', 'the hint says it is the SSH connection');
excludes(html, 'aria-disabled', 'offered');
excludes(html, 'cv-held', 'not held');

// No SSH connection, or a simulated robot: nothing to verify.
excludes(page.card(service), VERIFY, 'a robot reached through its service only');
excludes(page.card(sim), VERIFY, 'a simulated robot');
check(page.card(service), baseCard(service), 'its card is unchanged');
check(page.card(sim), baseCard(sim), 'and so is a simulated robot\'s');
excludes(page.card(service), 'title=', 'the other check is left as it is where it stands alone');

// Both checks on one card: each says what it checks.
html = page.card(both);
check(count(html, VERIFY), 1, 'the SSH verification');
check(count(html, 'data-action="robot-connect"'), 1, 'the check of the robot\'s own service is still there');
includes(html, '>连接检查</button>', 'under its name');
includes(html, 'title="检查机器人上接入服务的地址和令牌（HTTP），不是 SSH 登录"', 'and says that it is not the SSH login');

// While the robot is being onboarded the button is there, but not offered: the reason is on it.
sandbox.state.connection_verify.holds = {'robot-ssh': '这台机器人正在扫描硬件；扫描结束后再验证连接'};
html = page.card(ssh);
includes(html, 'aria-disabled="true"', 'held by a scan');
includes(html, 'cv-held', 'looks held');
includes(html, 'title="这台机器人正在扫描硬件；扫描结束后再验证连接"', 'and says why');
excludes(page.card(both), 'aria-disabled', 'another robot is not held');
sandbox.state.connection_verify.holds = {};

// 接入中 (the heartbeat's first check after the start) holds it too.
links['robot-ssh'] = {state: 'onboarding', label: '接入中', step: '正在检查连接'};
html = page.card(ssh);
includes(html, 'aria-disabled="true"', 'held while 接入中');
includes(page.cvHold(ssh), '接入中', 'the reason names the state');
includes(page.cvHold(ssh), '第一次心跳检查', 'and what is running');
for (const state of ['online', 'offline', 'not_onboarded']) {
  links['robot-ssh'] = {state, label: state};
  check(page.cvHold(ssh), '', `${state} does not hold it`);
}
delete links['robot-ssh'];
// An inherited name is not a hold.
sandbox.state.connection_verify.holds = Object.create({'robot-ssh': 'inherited'});
check(page.cvHold(ssh), '', 'own keys only');
sandbox.state.connection_verify.holds = {};
// An older server that does not send holds.
delete sandbox.state.connection_verify;
check(page.cvHold(ssh), '', 'no holds known');
sandbox.state.connection_verify = {holds: {}};

// A verification that passed: the system, the architecture and how long it took.
const route = states => [1, 2, 3].map((level, index) => ({kind: 'jump', level, label: `第 ${level} 级跳板机`,
  endpoint: `gate${level}@gate${level}.example.test:22`, state: states[index]}))
  .concat({kind: 'device', level: null, label: '目标机', endpoint: 'robot@robot.example.test:22', state: states[3]});
const passed = {ok: true, duration_ms: 1234, checked_at: 1, error: null, route: route(['ok', 'ok', 'ok', 'ok']),
                system: {os: 'Ubuntu 22.04.4 LTS', kernel: 'Linux 5.15.148-tegra', arch: 'aarch64'}};
const run = (result, failure = null) => ({robotId: 'robot-ssh', startedAt: 1000, result, failure});
html = page.cvBody(run(passed), ssh, connections[0]);
includes(html, 'SSH 连接验证通过', 'the verdict');
includes(html, '<dt>系统</dt><dd>Ubuntu 22.04.4 LTS</dd>', 'the system');
includes(html, '<dt>架构</dt><dd>aarch64</dd>', 'the architecture');
includes(html, '<dt>耗时</dt><dd>1.2 秒</dd>', 'how long it took');
includes(html, '内核 Linux 5.15.148-tegra', 'the kernel, small');
check(count(html, 'class="cv-hop cv-ok"'), 4, 'every hop logged in to');
check(count(html, '已登录'), 4, 'and says so');
includes(html, '第 3 级跳板机', 'the hops are named');
includes(html, 'gate2@gate2.example.test:22', 'with their addresses');
includes(html, '再验证一次', 'it can be done again');
excludes(html, 'engine-ssh-edit', 'nothing to fix');
excludes(html, '连接检查', 'the other check is not mentioned where the card has none');
includes(page.cvBody(run(passed), both, connections[1]), '“连接检查”', 'and is told apart where the card has both');
includes(page.cvSummary(run(passed), ssh), 'dog：SSH 连接验证通过 · Ubuntu 22.04.4 LTS · aarch64 · 1.2 秒', 'as a toast');

// A device without /etc/os-release: its kernel is the system, and is not said twice.
html = page.cvBody(run({...passed, system: {os: 'Linux 4.9.201', kernel: 'Linux 4.9.201', arch: 'armv7l'}}), ssh, connections[0]);
excludes(html, '内核', 'the kernel is not repeated');

// Stuck at the second of three jump hosts: which hop, why, and what was not tried.
const stuck = {ok: false, duration_ms: 3400, checked_at: 1, system: null, route: route(['ok', 'failed', 'not_reached', 'not_reached']),
               error: {hop: 'jump', level: 2, stage: 'login', reason: 'auth', where: '第 2 级跳板机 gate2@gate2.example.test:22',
                       message: '第 2 级跳板机拒绝登录：SSH 认证失败；请核对第 2 级跳板机的用户名和密码或密钥'}};
check(page.cvHeadline(run(stuck)), '卡在第 2 级跳板机', 'the hop it got stuck at');
html = page.cvBody(run(stuck), ssh, connections[0]);
includes(html, '<h3 id="cv-headline" tabindex="-1" aria-describedby="cv-detail">卡在第 2 级跳板机</h3>', 'the verdict names the hop');
includes(html, '<p class="cv-reason" id="cv-detail">', 'and the reason is read out with it');
includes(html, '<p class="cv-where"><code>gate2@gate2.example.test:22</code></p>', 'with its address');
includes(html, '第 2 级跳板机拒绝登录：SSH 认证失败', 'and the reason');
includes(html, '用了 3.4 秒', 'how long until it got stuck');
check(count(html, 'class="cv-hop cv-ok"'), 1, 'the first hop was logged in to');
check(count(html, 'class="cv-hop cv-failed"'), 1, 'the second is where it got stuck');
check(count(html, '卡在这里'), 1, 'said once');
check(count(html, '没有走到'), 2, 'the third hop and the device were not tried');
excludes(html, 'role="alert"', 'not announced twice: the headline takes the focus');
includes(html, 'data-action="engine-ssh-edit"', 'the way to fix the saved login');
includes(html, '反复被拒绝的登录可能让账号被锁', 'a refused login is not to be hammered');
check(html.indexOf('data-action="engine-ssh-edit"') < html.indexOf('>再验证一次<'), true, 'fixing the login comes first');
includes(html, 'class="button small" data-action="engine-ssh-edit"', 'and is the main button');
includes(html, 'class="button secondary small" data-action="engine-ssh-verify"', 'trying again unchanged is not');
check(count(html, 'id="cv-headline"'), 1, 'one headline');
check(count(html, 'id="cv-detail"'), 1, 'one detail');
excludes(html, '<dt>系统</dt>', 'no system is shown');
includes(page.cvSummary(run(stuck), ssh), '卡在第 2 级跳板机', 'as a toast');

// Stuck at the device; one jump host is named without a number by the server.
const atDevice = {...stuck, route: route(['ok', 'ok', 'ok', 'failed']),
                  error: {hop: 'device', level: null, stage: 'login', reason: 'auth', message: '设备拒绝登录：SSH 认证失败'}};
check(page.cvHeadline(run(atDevice)), '卡在目标机', 'the device');
const unreachable = {...stuck, error: {...stuck.error, reason: 'unreachable', message: '连不上第 2 级跳板机'}};
html = page.cvBody(run(unreachable), ssh, connections[0]);
excludes(html, '反复被拒绝', 'only a refused login carries that caution');
check(html.indexOf('>再验证一次<') < html.indexOf('data-action="engine-ssh-edit"'), true, 'otherwise trying again comes first');

// Logged in everywhere, but nothing was read.
const unread = {...stuck, route: route(['ok', 'ok', 'ok', 'ok']),
                error: {hop: 'device', level: null, stage: 'read', reason: 'timeout', message: '已登录目标机，但它在 10 秒内没有返回系统信息'}};
html = page.cvBody(run(unread), ssh, connections[0]);
includes(html, '已逐级登录，但没有读到系统信息', 'the verdict');
check(count(html, '已登录</span>'), 4, 'every hop is shown as logged in to');
excludes(html, 'engine-ssh-edit', 'the saved logins are fine: nothing to fix there');

// The session broke without saying where.
const lost = {...stuck, route: route(['unknown', 'unknown', 'unknown', 'unknown']),
              error: {hop: 'connection', level: null, stage: 'login', reason: 'session_lost', message: 'SSH 连接没有建立起来'}};
check(page.cvHeadline(run(lost)), '连接没有建立起来，没能确定卡在哪一跳', 'not a made-up hop');
check(count(page.cvBody(run(lost), ssh, connections[0]), '没有确定'), 4, 'no hop is shown as passed or failed');

// What the device printed is text, never markup; a state the page does not know is shown as unknown.
const hostile = {...passed, system: {os: '<img src=x onerror=alert(1)>', kernel: 'k', arch: '"><b>'},
                 route: [{kind: 'device', level: null, label: '<i>目标机</i>', endpoint: '<script>', state: 'constructor'}]};
html = page.cvBody(run(hostile), ssh, connections[0]);
excludes(html, '<img', 'the system is escaped');
excludes(html, '<script>', 'an address is escaped');
excludes(html, '<i>', 'a label is escaped');
// In attributes too: a robot id and a hold's reason end up in data-id and title.
const odd = {id: 'r"><img src=x onerror=1>', name: 'x', project_id: 'project-1', connection_id: 'connection-1'};
sandbox.state.connection_verify.holds = {[odd.id]: '"><script>alert(1)</script>$&$1'};
const oddCard = page.card(odd);
excludes(oddCard, '<img', 'a robot id is escaped in data-id');
excludes(oddCard, '<script>', 'a hold is escaped in title');
includes(oddCard, '$&amp;$1', 'and a "$" pattern is not read as a replacement pattern');
sandbox.state.connection_verify.holds = {};
// A route that is not a list does not stop the dialog from being drawn.
includes(page.cvBody(run({...stuck, route: 'not a list'}), ssh, connections[0]), '连接没有建立起来', 'a route that is not a list');
includes(html, 'class="cv-hop cv-unknown"', 'an unknown state is not looked up on the prototype');

// While it runs: the hops that will be logged in to, from the saved connection.
html = page.cvBody(run(null), ssh, connections[0], 4000);
includes(html, '正在逐级登录', 'it says what is going on');
includes(html, '<span id="cv-elapsed">3</span>', 'and for how long');
includes(html, '<h3 role="status">正在逐级登录…</h3>', 'only the line announces itself');
check(/role="status"[^]*cv-elapsed/.test(html.replace(/<h3 role="status">[^<]*<\/h3>/, '')), false, 'the moving seconds are not inside a live region');
check(count(html, 'class="cv-hop cv-pending"'), 4, 'three jump hosts and the device');
includes(html, '第 1 级跳板机', 'named as the form names them');
includes(html, 'gate3@gate3.example.test:22', 'with their addresses');
includes(html, 'robot@robot.example.test:22', 'the device last');
excludes(html, '再验证一次', 'not again while it runs');
html = page.cvBody(run(null), both, connections[1], 1000);
check(count(html, 'class="cv-hop cv-pending"'), 1, 'a direct connection is one hop');
includes(html, 'arm@arm.example.test:2222', 'the device');

// The server refused, or the request failed.
html = page.cvBody(run(null, {message: '这台机器人正在扫描硬件；扫描结束后再验证连接', refused: true}), ssh, connections[0]);
includes(html, '现在不能验证连接', 'a refusal');
includes(html, '正在扫描硬件', 'with its reason');
includes(html, '再验证一次', 'and can be tried again');
includes(page.cvBody(run(null, {message: 'Failed to fetch', refused: false}), ssh, connections[0]), '这一次没有验证成', 'a failed request');

(async () => {
  // One press is one request, for the robot's own project; other actions are left to the page.
  check(await page.action('engine-status', 'robot-ssh'), ['base', 'engine-status', 'robot-ssh'], 'other actions pass through');
  let finish;
  reply = () => new Promise(resolve => { finish = resolve; });
  const pending = page.action('engine-ssh-verify', 'robot-ssh');
  await Promise.resolve();
  check(calls.length, 1, 'one request');
  check(calls[0][0], 'engineering/ssh/verify', 'the route');
  check({...calls[0][1]}, {project_id: 'project-1', robot_id: 'robot-ssh'}, 'the robot and its project');
  check(dialogs.length, 1, 'the dialog opens at once');
  check(dialogs[0][1], '验证 SSH 连接', 'and says which check this is');
  includes(page.card(ssh), '>验证中…</button>', 'the card says it is running');
  excludes(page.card(ssh), 'aria-disabled', 'and can be pressed to look at it');
  page.action('engine-ssh-verify', 'robot-ssh');
  await Promise.resolve();
  check(calls.length, 1, 'a second press sends nothing');
  check(dialogs.length, 2, 'it shows the one that is running');
  const before = polls;
  finish(passed);
  await pending;
  check(page.cvRuns.size, 0, 'done');
  includes(toasts.at(-1), 'SSH 连接验证通过', 'the outcome is said');
  check(polls, before + 1, 'the state on the card is asked for at once');
  // Its dialog was not on screen (there is no page here): the result is kept, and the card opens it.
  includes(page.card(ssh), '>查看验证结果</button>', 'the card offers the result');
  let shown = dialogs.length;
  await page.action('engine-ssh-verify', 'robot-ssh');
  check(calls.length, 1, 'looking at it logs in to nothing');
  check(dialogs.length, shown + 1, 'it is shown');
  includes(page.card(ssh), '>验证 SSH 连接</button>', 'and then the button is back');

  // A verification that failed does not ask the heartbeat; the toast carries the reason.
  reply = async () => stuck;
  await page.action('engine-ssh-verify', 'robot-ssh');
  check(calls.length, 2, 'a new verification');
  check(polls, before + 1, 'no answer from the device, no new state');
  includes(toasts.at(-1), '卡在第 2 级跳板机', 'the hop is said');
  includes(toasts.at(-1), '拒绝登录', 'with the reason');
  includes(page.card(ssh), '查看验证结果', 'and it is kept to be looked at');
  await page.action('engine-ssh-verify', 'robot-ssh');
  check(calls.length, 2, 'without logging in again');

  // Held: a press sends nothing and says why.
  sandbox.state.connection_verify.holds = {'robot-ssh': '这台机器人有调查会话正在运行；等它结束或先停止它，再验证连接'};
  const sent = calls.length;
  await page.action('engine-ssh-verify', 'robot-ssh');
  check(calls.length, sent, 'nothing is sent while the robot is being onboarded');
  includes(toasts.at(-1), '现在不能验证连接：这台机器人有调查会话正在运行', 'and the reason is said');
  sandbox.state.connection_verify.holds = {};

  // The server's refusal (the page did not know yet) is shown, not thrown.
  reply = async () => { throw Object.assign(new Error('这台机器人正在扫描硬件；扫描结束后再验证连接'), {status: 400, reason: 'verify_while_onboarding'}); };
  await page.action('engine-ssh-verify', 'robot-ssh');
  includes(toasts.at(-1), '现在不能验证连接：这台机器人正在扫描硬件', 'the refusal');
  check(page.cvRuns.size, 0, 'and nothing stays running');
  includes(page.card(ssh), '>验证 SSH 连接</button>', 'a refusal is not kept as a result to look at');

  // Any other error is not "not now": the login may have gone out.
  reply = async () => { throw Object.assign(new Error('内部错误，操作未确认；请查看本地服务'), {status: 500, reason: 'desktop_internal_error'}); };
  await page.action('engine-ssh-verify', 'robot-ssh');
  includes(toasts.at(-1), '这一次没有验证成：内部错误', 'an internal error');
  excludes(toasts.at(-1), '现在不能验证连接', 'is not said to be a refusal');

  // No answer at all (the page stopped waiting): the verification may still be running on the server.
  reply = async () => { throw new TypeError('Load failed'); };
  await page.action('engine-ssh-verify', 'robot-ssh');
  includes(toasts.at(-1), '验证可能还在后台逐级登录', 'it may still be running');
  excludes(toasts.at(-1), 'Load failed', 'and the engine\'s own words are not shown');

  // An answer that is not a result.
  for (const answer of [null, 'ok', {}]) {
    reply = async () => answer;
    await page.action('engine-ssh-verify', 'robot-ssh');
    includes(toasts.at(-1), '后台的回答不是一次验证的结果', `the answer ${JSON.stringify(answer)}`);
  }
  check(page.cvRuns.size, 0, 'and nothing stays running');

  // Signed out while it ran: no verdict is made up; the page is asked to look at its state again.
  reply = async () => { throw Object.assign(new Error('请先登录工作空间'), {status: 401, reason: 'unauthorized'}); };
  const said = toasts.length, looked = refreshes;
  await page.action('engine-ssh-verify', 'robot-ssh');
  check(toasts.length, said, 'nothing is said about a verification');
  check(refreshes, looked + 1, 'the page looks at its state again');
  check(page.cvRuns.size, 0, 'and nothing stays running');

  // A robot that is gone.
  await page.action('engine-ssh-verify', 'robot-gone');
  includes(toasts.at(-1), '已经不在项目里', 'said');

  // Signed out: the page's own handling takes the action.
  sandbox.state.user = null;
  check(await page.action('engine-ssh-verify', 'robot-ssh'), ['base', 'engine-ssh-verify', 'robot-ssh'], 'not without an account');
  sandbox.state.user = {id: 'user-1'};

  // 接入中 comes and goes with the heartbeat's own repaint.
  const painted = paints;
  page.paint();
  check(paints, painted + 1, 'the heartbeat still paints its lines');

  console.log(`connection_verify logic: ${checks} checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
