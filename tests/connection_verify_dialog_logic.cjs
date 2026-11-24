// The dialog of web/connection_verify.js (issue #16) on a small stand-in page: what happens to a result
// that arrives while its dialog is on screen, under another dialog, or after the dialog was closed; the
// seconds that count while it runs; the buttons on the cards following a run and 接入中 in place; a
// sign-out while it runs. tests/connection_verify_logic.cjs checks the HTML it builds; this one runs the
// paths that need a page.
//   node tests/connection_verify_dialog_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;

// ---------------------------------------------------------------- a stand-in page
const toasts = [], focused = [], intervals = new Map(), refreshes = [];
let intervalIds = 0;
const dialogEl = {
  open: false, dataset: {}, listeners: [], content: null,
  addEventListener(type, fn) { if (type === 'close') this.listeners.push(fn); },
  showModal() { this.open = true; },
  close() { if (!this.open) return; this.open = false; for (const fn of this.listeners) fn(); },
};
let body = null;                                   // #cv-body of the dialog on screen, when it is ours
const newBody = () => ({
  dataset: {}, html: '', elapsed: {textContent: ''},
  set innerHTML(value) { this.html = String(value); },
  get innerHTML() { return this.html; },
  querySelector(selector) {
    if (selector !== '#cv-headline') throw Error('unexpected selector ' + selector);
    return this.html.includes('id="cv-headline"') ? {focus: options => focused.push(options)} : null;
  },
});
// The buttons on the cards, as the page drew them.
const cards = [];
const cardButton = id => {
  const classes = new Set(), attributes = {};
  const el = {dataset: {id}, textContent: '验证 SSH 连接', title: '', attributes,
              classList: {contains: name => classes.has(name), toggle: (name, on) => (on ? classes.add(name) : classes.delete(name))},
              setAttribute: (name, value) => { attributes[name] = value; }, removeAttribute: name => { delete attributes[name]; }};
  cards.push(el);
  return el;
};
const document = {
  querySelector(selector) {
    if (selector === '#dialog') return dialogEl;
    if (selector === '#cv-body') return body;
    if (selector === '#cv-elapsed') return body && body.html.includes('id="cv-elapsed"') ? body.elapsed : null;
    throw Error('unexpected selector ' + selector);
  },
  querySelectorAll(selector) {
    if (selector !== '[data-cv-card]') throw Error('unexpected selector ' + selector);
    return cards;
  },
};
const robots = [{id: 'robot-1', name: 'dog', project_id: 'project-1', connection_id: 'connection-1'},
                {id: 'robot-2', name: 'arm', project_id: 'project-1', connection_id: 'connection-2'}];
const connections = [{id: 'connection-1', host: 'dog.example.test', port: 22, username: 'robot'},
                     {id: 'connection-2', host: 'arm.example.test', port: 22, username: 'arm'}];
const links = {}, pending = [];
let hbPaints = 0, hbPolls = 0;
const sandbox = {
  h, badge, button, document, Date, setInterval: fn => { intervalIds += 1; intervals.set(intervalIds, fn); return intervalIds; },
  clearInterval: id => { intervals.delete(id); },
  state: {user: {id: 'user-1'}, robots, device_connections: connections, connection_verify: {holds: {}}},
  find: (kind, id) => sandbox.state[kind].find(x => x.id === id),
  sshJumpLabel: (level, total) => (total > 1 ? `第 ${level} 级跳板机` : '跳板机'),
  toast: text => { toasts.push({text, underDialog: dialogEl.open}); },
  // As web/app.js: puts the content in, opens the dialog when it is closed.
  dialog: (title, sub, html) => { dialogEl.content = {title, sub}; body = html.includes('id="cv-body"') ? newBody() : null;
                                  if (!dialogEl.open) dialogEl.showModal(); },
  api: (path, payload) => new Promise((resolve, reject) => pending.push({path, payload, resolve, reject})),
  refresh: async force => { refreshes.push(force); },
  render: () => {},
  hbLink: id => (Object.hasOwn(links, id) ? links[id] : null),
  hbPaint: () => { hbPaints += 1; },
  hbPoll: () => { hbPolls += 1; },
  engineRobotCard: r => `<article class="card"><h3>${h(r.name)}</h3><div class="button-row">` +
    button('实时状态', 'engine-status', 'secondary small', r.id) + button('配置连接', 'engine-ssh-edit', 'secondary small', r.id) + '</div></article>',
  engineAction: async (type, id) => ['base', type, id],
};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/connection_verify.js', 'utf8') +
  '\n;({press: id => engineAction("engine-ssh-verify", id), other: (...a) => dialog(...a), paint: () => hbPaint(), cvRuns, cvUnseen})', sandbox);

let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const has = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(String(text).slice(0, 600))}`); checks += 1; };
const settle = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
const passed = {ok: true, duration_ms: 812, checked_at: 1, error: null, robot_id: 'robot-1',
                route: [{kind: 'device', level: null, label: '目标机', endpoint: 'robot@dog.example.test:22', state: 'ok'}],
                system: {os: 'Ubuntu 22.04.4 LTS', kernel: 'Linux 5.15', arch: 'aarch64'}};
const refused = {ok: false, duration_ms: 400, checked_at: 1, system: null,
                 route: [{kind: 'device', level: null, label: '目标机', endpoint: 'robot@dog.example.test:22', state: 'failed'}],
                 error: {hop: 'device', level: null, stage: 'login', reason: 'auth', message: '设备拒绝登录：SSH 认证失败'}};
cardButton('robot-1'); cardButton('robot-2');

(async () => {
  // 1. The dialog is on screen when the result arrives: drawn in place, the headline takes the focus.
  const first = page.press('robot-1');
  check(pending.length, 1, 'one request');
  check(dialogEl.open, true, 'the dialog opens at once');
  check(dialogEl.content.sub, '验证 SSH 连接', 'and says which check it is');
  check(body.dataset.cvState, 'running', 'it runs');
  check(cards[0].textContent, '验证中…', 'the card says so in place');
  check(intervals.size, 1, 'the seconds count');
  for (const fn of intervals.values()) fn();
  check(/^\d+$/.test(body.elapsed.textContent), true, 'and move on in place');
  pending.shift().resolve(passed);
  await first;
  check(body.dataset.cvState, 'passed', 'the result is drawn in the dialog');
  has(body.html, 'Ubuntu 22.04.4 LTS', 'with the system');
  has(body.html, 'aarch64', 'the architecture');
  has(body.html, '812 毫秒', 'and the time');
  check(focused.length, 1, 'the headline takes the focus');
  check(intervals.size, 0, 'the seconds stop');
  check(toasts.length, 0, 'nothing else is said');
  check(page.cvUnseen.size, 0, 'it has been seen');
  check(cards[0].textContent, '验证 SSH 连接', 'the card is back');
  check(hbPolls, 1, 'the state on the card is asked for at once');
  dialogEl.close();

  // 2. Another dialog took its place while it ran: the result waits, and so does the toast.
  const second = page.press('robot-1');
  page.other('实时状态', 'dog', '<div class="dialog-body"></div>');
  check(intervals.size, 0, 'the seconds stop with the other dialog');
  pending.shift().resolve(refused);
  await second;
  check(toasts.length, 0, 'no toast under the other dialog\'s backdrop');
  check(page.cvUnseen.has('robot-1'), true, 'the result is kept');
  check(cards[0].textContent, '查看验证结果', 'the card offers it');
  has(cards[0].title, '卡在目标机', 'and its hint says what it was');
  dialogEl.close();
  check(toasts.length, 1, 'said once the other dialog is closed');
  check(toasts[0].underDialog, false, 'not under a backdrop');
  has(toasts[0].text, '卡在目标机', 'where it got stuck');
  has(toasts[0].text, '设备拒绝登录', 'and why');
  // The card's button shows it, without logging in again.
  await page.press('robot-1');
  check(pending.length, 0, 'no new login');
  check(body.dataset.cvState, 'failed', 'the kept result is shown');
  has(body.html, '反复被拒绝的登录可能让账号被锁', 'with the caution');
  check(page.cvUnseen.size, 0, 'and it has been seen now');
  check(cards[0].textContent, '验证 SSH 连接', 'the card is back');
  // From here, 再验证一次 logs in again.
  const third = page.press('robot-1');
  check(pending.length, 1, 'a new verification from the dialog');
  dialogEl.close();

  // 3. The dialog was closed while it ran: said at once, kept for the card.
  pending.shift().resolve(passed);
  await third;
  check(toasts.length, 2, 'said at once');
  has(toasts[1].text, 'SSH 连接验证通过', 'what it was');
  check(page.cvUnseen.has('robot-1'), true, 'and kept');

  // 4. 接入中 comes with the heartbeat's repaint; the card's button follows without being drawn again.
  links['robot-2'] = {state: 'onboarding', label: '接入中'};
  page.paint();
  check(hbPaints, 1, 'the heartbeat still paints its own lines');
  check(cards[1].attributes['aria-disabled'], 'true', 'held while 接入中');
  check(cards[1].classList.contains('cv-held'), true, 'and looks it');
  has(cards[1].title, '接入中', 'with the reason');
  await page.press('robot-2');
  check(pending.length, 0, 'a press sends nothing');
  has(toasts.at(-1).text, '现在不能验证连接', 'and says why');
  delete links['robot-2'];
  page.paint();
  check(cards[1].attributes['aria-disabled'], undefined, 'offered again');

  // 5. Held while its dialog is open: 再验证一次 shows why in the dialog, and sends nothing.
  await page.press('robot-1');                          // the kept result
  sandbox.state.connection_verify.holds = {'robot-1': '这台机器人正在扫描硬件；扫描结束后再验证连接'};
  await page.press('robot-1');
  check(pending.length, 0, 'nothing is sent');
  check(body.dataset.cvState, 'refused', 'the dialog says it is not possible now');
  has(body.html, '正在扫描硬件', 'and why');
  sandbox.state.connection_verify.holds = {};
  dialogEl.close();

  // 6. Signed out while it ran: the dialog closes and the page's own handling takes over; no verdict is made up.
  const said = toasts.length;
  const fourth = page.press('robot-1');
  pending.shift().reject(Object.assign(new Error('请先登录工作空间'), {status: 401, reason: 'unauthorized'}));
  await fourth;
  await settle();
  check(dialogEl.open, false, 'the dialog is closed');
  check(refreshes, [true], 'the page looks at its state again');
  check(toasts.length, said, 'nothing is said about a verification');
  check(page.cvRuns.size, 0, 'nothing stays running');
  check(intervals.size, 0, 'no timer is left behind');

  console.log(`connection_verify dialog logic: ${checks} checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
