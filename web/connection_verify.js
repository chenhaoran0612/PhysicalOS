'use strict';
// Issue #16 — 「验证 SSH 连接」 of a robot that is already added: log in to every hop of its saved SSH
// connection once and read the system version and the processor architecture
// (brainos/connection_verify.py). It shows the system, the architecture and how long it took, or the
// hop it got stuck at. It can be repeated at will; it only reads.
// This is the SSH route to the machine. The 「连接检查」 on the same card asks the robot's own service
// (HTTP): the button's name says SSH, and the hints and the dialog say which is which.
// While the robot is being onboarded the button is not offered: the server names work that starts and
// ends with an action (state.connection_verify.holds), the heartbeat's state says 接入中.
// A result that comes back while its dialog is not on screen is kept until it has been looked at: the
// card's button opens it without logging in again.
// Loaded after heartbeat.js and ssh_edit.js. It wraps engineRobotCard / engineAction / dialog / hbPaint
// instead of editing them.

const CV_ACTION = 'engine-ssh-verify';
const CV_LABEL = '验证 SSH 连接';
const CV_HINT = '按保存的 SSH 连接逐级登录一次，读出系统版本和处理器架构';
const CV_SERVICE_HINT = '检查机器人上接入服务的地址和令牌（HTTP），不是 SSH 登录';
// A login that a machine refused will be refused again; machines lock accounts and ban addresses for that.
const CV_REFUSED_CAUTION = '反复被拒绝的登录可能让账号被锁，或让这台电脑被暂时封禁。先核对这一级的登录信息，再重新验证。';
// The page stopped waiting (the desktop's web view gives up on a request after about a minute): the
// verification may still be logging in, and is noted when it ends.
const CV_NOT_WAITED = '页面没有等到这一次的结果：验证可能还在后台逐级登录。过一会儿再点“验证 SSH 连接”。';
// The two refusals that mean "not now": nothing was sent to any machine.
const CV_NOT_NOW = ['verify_while_onboarding', 'verify_in_progress'];
// What became of a hop: [what the row says, the badge's colour].
const CV_HOPS = {ok: ['已登录', 'good'], failed: ['卡在这里', 'bad'], not_reached: ['没有走到', ''],
                 unknown: ['没有确定', ''], pending: ['等待登录', '']};
// Robots whose connection is being verified from this page: {robot id: the run}.
const cvRuns = new Map();
// Runs that ended while their dialog was not on screen, not looked at yet: {robot id: the run}.
const cvUnseen = new Map();
// The run the dialog shows, the timer that counts its seconds, and outcomes to say once the dialog in the way is closed.
let cvShown = null, cvTicker = 0, cvLater = [];

// How long it took: milliseconds on a local network, seconds through jump hosts, minutes when a hop timed out.
function cvSeconds(ms) {
  if (!Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.max(Math.round(ms), 1)} 毫秒`;
  const tenths = Math.round(ms / 100) / 10;         // rounded once, before it is compared
  if (tenths < 10) return `${tenths.toFixed(1)} 秒`;
  const seconds = Math.floor(tenths);              // from the rounded value: 9999 ms is "10 秒", not "9 秒"
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

// ---------------------------------------------------------------- when it is not offered

// Why this robot's connection cannot be verified right now; '' when it can.
function cvHold(robot) {
  const held = state.connection_verify?.holds;
  if (held && Object.hasOwn(held, robot.id)) return String(held[robot.id]);
  const link = typeof hbLink === 'function' ? hbLink(robot.id) : null;
  if (link?.state === 'onboarding') return '这台机器人还在接入中：应用启动后的第一次心跳检查还没有结束。结束后再验证连接';
  return '';
}

// What the button on the card says. A run that was started here, or one whose result was not looked at
// yet, opens again from it.
function cvButtonFacts(robot) {
  if (cvRuns.has(robot.id)) return {text: '验证中…', title: '正在验证 SSH 连接，点开查看', held: false};
  if (cvUnseen.has(robot.id)) return {text: '查看验证结果', title: '上一次验证的结果还没有看过：' + cvHeadline(cvUnseen.get(robot.id)), held: false};
  const hold = cvHold(robot);
  return {text: CV_LABEL, title: hold || CV_HINT, held: Boolean(hold)};
}

// Not `disabled` while it is held: a disabled button cannot be focused, so its reason could not be read
// from the keyboard. A press on a held button starts nothing and says why (cvStart).
function cvButton(robot) {
  const facts = cvButtonFacts(robot);
  return `<button type="button" class="button secondary small cv-open${facts.held ? ' cv-held' : ''}" data-action="${CV_ACTION}" ` +
    `data-id="${h(robot.id)}" data-cv-card${facts.held ? ' aria-disabled="true"' : ''} title="${h(facts.title)}">${h(facts.text)}</button>`;
}

// The card of a real robot with an SSH connection gets 「验证 SSH 连接」 in front of 「配置连接」.
function cvCard(html, robot) {
  if (robot.sim || !robot.connection_id) return html;
  const mine = cvButton(robot);
  const edit = button('配置连接', 'engine-ssh-edit', 'secondary small', robot.id);
  const status = button('实时状态', 'engine-status', 'secondary small', robot.id);
  if (html.includes(edit)) html = html.replace(edit, () => mine + edit);
  else if (html.includes(status)) html = html.replace(status, () => status + mine);
  else return html;
  // The other check on the same card says what it checks.
  const service = button('连接检查', 'robot-connect', 'secondary small', robot.id);
  return html.replace(service, () => service.replace('<button ', `<button title="${h(CV_SERVICE_HINT)}" `));
}

// The buttons on the cards follow what holds a robot without the cards being drawn again.
function cvPaint() {
  if (typeof document === 'undefined') return;
  for (const el of document.querySelectorAll('[data-cv-card]')) {
    const robot = state.robots?.find(r => r.id === el.dataset.id);
    if (!robot) continue;
    const facts = cvButtonFacts(robot);
    if (el.textContent !== facts.text) el.textContent = facts.text;
    if (el.title !== facts.title) el.title = facts.title;
    if (el.classList.contains('cv-held') !== facts.held) {
      el.classList.toggle('cv-held', facts.held);
      if (facts.held) el.setAttribute('aria-disabled', 'true');
      else el.removeAttribute('aria-disabled');
    }
  }
}

// ---------------------------------------------------------------- what the dialog shows

const cvEndpoint = hop => `${hop.username}@${hop.host}:${hop.port}`;

// The route as the saved connection has it, before any result: what will be logged in to, in order.
function cvSavedRoute(connection) {
  const jumps = connection?.jumps || [];
  const hops = jumps.map((hop, index) => ({kind: 'jump', level: index + 1, label: sshJumpLabel(index + 1, jumps.length),
                                           endpoint: cvEndpoint(hop), state: 'pending'}));
  return connection ? [...hops, {kind: 'device', level: null, label: '目标机', endpoint: cvEndpoint(connection), state: 'pending'}] : [];
}

// A hop's state comes from the server: it is looked up as an own key only.
const cvHopState = hop => (Object.hasOwn(CV_HOPS, hop?.state) ? hop.state : 'unknown');

function cvHopRow(hop) {
  const state = cvHopState(hop), [text, cls] = CV_HOPS[state];
  return `<li class="cv-hop cv-${state}"><span class="cv-node" aria-hidden="true"></span>` +
    `<div class="cv-hop-name"><b>${h(hop?.label)}</b><code>${h(hop?.endpoint)}</code></div>${badge(text, cls)}</li>`;
}

// role="list": WebKit drops the list role of a list without bullets, and with it the list's name.
function cvRouteList(route) {
  if (!route.length) return '';
  return '<section class="cv-route-block"><h4>登录路线</h4>' +
    `<ol class="cv-route" role="list" aria-label="从这台电脑到目标机，依次登录">${route.map(cvHopRow).join('')}</ol></section>`;
}

const cvStuck = result => (Array.isArray(result.route) ? result.route.find(hop => hop?.state === 'failed') : null);

// One line that says where it stands: passed, the hop it got stuck at, or why nothing was verified.
function cvHeadline(run) {
  if (run.failure) return run.failure.refused ? '现在不能验证连接' : '这一次没有验证成';
  const result = run.result;
  if (result.ok) return 'SSH 连接验证通过';
  if (result.error?.stage === 'read') return '已逐级登录，但没有读到系统信息';
  const stuck = cvStuck(result);
  return stuck ? `卡在${stuck.label}` : '连接没有建立起来，没能确定卡在哪一跳';
}

// The headline takes the focus once the outcome is there; with it, its details are read out.
const cvHead = (run, mark) => `<div class="cv-verdict-head"><span class="cv-mark" aria-hidden="true">${mark}</span>` +
  `<h3 id="cv-headline" tabindex="-1" aria-describedby="cv-detail">${h(cvHeadline(run))}</h3></div>`;

function cvPassed(run) {
  const result = run.result, system = result.system || {};
  const facts = [['系统', system.os], ['架构', system.arch], ['耗时', cvSeconds(result.duration_ms)]];
  const kernel = system.kernel && system.kernel !== system.os ? `<p class="cv-more">内核 ${h(system.kernel)}</p>` : '';
  return `<section class="cv-verdict cv-passed">${cvHead(run, '✓')}` +
    `<dl class="cv-facts" id="cv-detail">${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${h(value || '—')}</dd></div>`).join('')}</dl>${kernel}</section>`;
}

function cvFailed(run) {
  const result = run.result, error = result.error || {}, stuck = cvStuck(result);
  return `<section class="cv-verdict cv-failed">${cvHead(run, '!')}` +
    (stuck ? `<p class="cv-where"><code>${h(stuck.endpoint)}</code></p>` : '') +
    `<p class="cv-reason" id="cv-detail">${h(error.message || '没有得到失败原因')}</p>` +
    (cvRefusedLogin(result) ? `<p class="cv-caution">${h(CV_REFUSED_CAUTION)}</p>` : '') +
    `<p class="cv-more">用了 ${h(cvSeconds(result.duration_ms))}</p></section>`;
}

const cvRefusedLogin = result => result?.error?.stage === 'login' && result.error.reason === 'auth';

// Only the line announces itself while it runs: the seconds that move on beside it are not read out each time.
function cvRunning(run, now) {
  const seconds = Math.max(Math.floor((now - run.startedAt) / 1000), 0);
  return '<section class="cv-verdict cv-running"><div class="cv-verdict-head"><span class="cv-spinner" aria-hidden="true"></span>' +
    '<h3 role="status">正在逐级登录…</h3></div>' +
    `<p class="cv-more">已用 <span id="cv-elapsed">${seconds}</span> 秒。每一级最多等几十秒；登录不上时一般会说明卡在哪一跳。</p></section>`;
}

// The server refused, or the request itself failed: nothing was verified.
function cvNotDone(run) {
  return `<section class="cv-verdict cv-refused">${cvHead(run, '–')}<p class="cv-reason" id="cv-detail">${h(run.failure.message)}</p></section>`;
}

// What 「验证 SSH 连接」 does, and which check it is not: said only where the robot's card carries the other one too.
function cvIntro(robot) {
  const both = Boolean(robot) && engineRobotCard(robot).includes('data-action="robot-connect"');
  return `<p class="cv-what">${h(CV_HINT)}。只读：不改动设备上的文件和设置，也不扫描硬件；每次登录会照常记入设备的登录日志。` +
    (both ? '这里验证的是到这台机器的 SSH 登录；机器人上接入服务（HTTP）的检查是卡片上的“连接检查”。' : '') + '</p>';
}

// The whole body of the dialog for a run: while it runs, its result, or why there is none.
function cvBody(run, robot, connection, now = Date.now()) {
  const done = run.result || run.failure;
  const verdict = run.result ? (run.result.ok ? cvPassed(run) : cvFailed(run)) : run.failure ? cvNotDone(run) : cvRunning(run, now);
  const route = run.result ? (Array.isArray(run.result.route) ? run.result.route : []) : run.failure ? [] : cvSavedRoute(connection);
  const fixable = run.result && !run.result.ok && run.result.error?.stage === 'login';
  // After a refused login, fixing the saved login comes first: trying again unchanged is what locks accounts.
  const fixFirst = cvRefusedLogin(run.result);
  const again = done ? button('再验证一次', CV_ACTION, fixFirst ? 'secondary small' : 'small', run.robotId) : '';
  const fix = fixable ? button('配置连接', 'engine-ssh-edit', fixFirst ? 'small' : 'secondary small', run.robotId) : '';
  return cvIntro(robot) + verdict + cvRouteList(route) +
    `<div class="button-row cv-actions">${fixFirst ? fix + again : again + fix}${button('关闭', 'close', 'secondary small')}</div>`;
}

// What a toast says of a run whose dialog was not on screen when it ended.
function cvSummary(run, robot) {
  const name = robot?.name ? robot.name + '：' : '';
  if (run.failure) return `${name}${cvHeadline(run)}：${run.failure.message}`;
  const result = run.result;
  if (result.ok) return `${name}SSH 连接验证通过 · ${result.system?.os || ''} · ${result.system?.arch || ''} · ${cvSeconds(result.duration_ms)}`;
  return `${name}验证 SSH 连接：${cvHeadline(run)}。${result.error?.message || ''} 点卡片上的“查看验证结果”看详情。`;
}

// ---------------------------------------------------------------- running one

const cvDialog = () => (typeof document === 'undefined' ? null : document.querySelector('#dialog'));
const cvIsShown = run => { const el = cvDialog(); return Boolean(el?.open && cvShown === run && el.dataset.sshVerify === run.robotId); };

function cvStopTicker() {
  if (cvTicker) clearInterval(cvTicker);
  cvTicker = 0;
}

const cvState = run => (run.result ? (run.result.ok ? 'passed' : 'failed') : run.failure ? 'refused' : 'running');

// Draw the run into the dialog that shows it, in place: the dialog is not opened again.
function cvDraw(run) {
  const body = cvIsShown(run) ? document.querySelector('#cv-body') : null;
  if (!body) return false;
  body.innerHTML = cvBody(run, find('robots', run.robotId), find('device_connections', find('robots', run.robotId)?.connection_id));
  body.dataset.cvState = cvState(run);             // after the content: a test that waits for it finds the content there
  if (run.result || run.failure) body.querySelector('#cv-headline')?.focus({preventScroll: true});
  return true;
}

function cvOpen(run) {
  const robot = find('robots', run.robotId);
  cvUnseen.delete(run.robotId);                     // looked at now
  dialog(h(robot?.name || ''), CV_LABEL, '<div class="dialog-body cv" id="cv-body"></div>');
  cvShown = run;
  const el = cvDialog();
  if (!el) return;
  el.dataset.sshVerify = run.robotId;
  cvDraw(run);
  cvPaint();
  cvStopTicker();
  if (run.result || run.failure) return;
  cvTicker = setInterval(() => {                    // the seconds move on in place: nothing else is redrawn
    const elapsed = cvIsShown(run) && !run.result && !run.failure ? document.querySelector('#cv-elapsed') : null;
    if (!elapsed) return cvStopTicker();
    elapsed.textContent = String(Math.max(Math.floor((Date.now() - run.startedAt) / 1000), 0));
  }, 1000);
}

// Why a request did not bring a result. Only the two "not now" refusals sent nothing to any machine.
function cvFailure(error) {
  const code = typeof error?.reason === 'string' ? error.reason : '';
  if (CV_NOT_NOW.includes(code)) return {message: String(error.message), refused: true, status: error.status};
  if (!Number.isInteger(error?.status)) return {message: CV_NOT_WAITED, refused: false, status: null};      // no answer at all
  return {message: String(error.message || '请求失败'), refused: false, status: error.status};
}

// A run ended while its dialog was not on screen: said in a toast, and a result (not a refusal or a
// request that failed: there is nothing more to look at) is kept for the card's button. The toast waits
// while another dialog is open: it would sit under that dialog's backdrop.
function cvTell(run) {
  if (run.result) cvUnseen.set(run.robotId, run);
  if (cvDialog()?.open) cvLater.push(run);
  else toast(cvSummary(run, find('robots', run.robotId)));
}

async function cvRequest(run, projectId) {
  let answer = null;
  try {
    answer = await api('engineering/ssh/verify', {project_id: projectId, robot_id: run.robotId});
  } catch (error) {
    run.failure = cvFailure(error);
  }
  if (!run.failure) {
    if (answer && typeof answer === 'object' && typeof answer.ok === 'boolean') run.result = answer;
    else run.failure = {message: '后台的回答不是一次验证的结果；请重试', refused: false, status: 200};
  }
  cvRuns.delete(run.robotId);
  if (run.failure?.status === 401) {                // signed out meanwhile: the page's own handling takes over
    if (cvIsShown(run)) cvDialog().close();
    cvPaint();
    return refresh(true).then(() => run);
  }
  if (cvDraw(run)) cvStopTicker();
  else cvTell(run);
  cvPaint();
  // A verification that passed is an answer from the device: its state on the card follows at once.
  if (run.result?.ok && typeof hbPoll === 'function') hbPoll();
  return run;
}

// The button on a card, and 「再验证一次」 in the dialog.
function cvStart(robotId) {
  const robot = find('robots', robotId);
  if (!robot) return toast('这台机器人已经不在项目里了');
  const shownHere = cvDialog()?.open && cvDialog().dataset.sshVerify === robotId;
  let run = cvRuns.get(robotId);
  if (!run && cvUnseen.has(robotId) && !shownHere) return cvOpen(cvUnseen.get(robotId));    // look first, log in again from there
  if (!run) {
    const hold = cvHold(robot);
    run = {robotId, startedAt: Date.now(), result: null, failure: null, promise: null};
    if (hold) {                                     // not while it is being onboarded: nothing is sent
      if (!shownHere) return toast('现在不能验证连接：' + hold);
      run.failure = {message: hold, refused: true, status: null};
      return cvOpen(run);
    }
    cvRuns.set(robotId, run);
    run.promise = cvRequest(run, robot.project_id);
  }
  cvOpen(run);
  return run.promise;
}

{
  const baseCard = engineRobotCard, baseAction = engineAction, baseDialog = dialog;
  engineRobotCard = r => cvCard(baseCard(r), r);
  engineAction = async (type, id) => (type === CV_ACTION && state.user ? cvStart(id) : baseAction(type, id));
  // Any other dialog takes the place of this one: a result that arrives later is kept for the card's button.
  dialog = (...args) => {
    const el = cvDialog();
    if (el) delete el.dataset.sshVerify;
    cvShown = null;
    cvStopTicker();
    return baseDialog(...args);
  };
  if (typeof hbPaint === 'function') {
    const basePaint = hbPaint;
    hbPaint = (...args) => { basePaint(...args); cvPaint(); };     // 接入中 came or went
  }
}
if (typeof document !== 'undefined') {
  document.querySelector('#dialog').addEventListener('close', () => {
    cvShown = null;
    cvStopTicker();
    // Outcomes that ended under another dialog are said now, unless they were looked at meanwhile.
    const later = cvLater.filter(run => !run.result || cvUnseen.get(run.robotId) === run);
    cvLater = [];
    if (later.length) toast(later.map(run => cvSummary(run, find('robots', run.robotId))).join('\n'));
  });
  render();
}
