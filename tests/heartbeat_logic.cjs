// Pure-logic checks of web/heartbeat.js (issue #24): what a robot's card and the status page say for
// 未接入 / 接入中 / 在线 / 离线, the time of the last heartbeat, and how long a robot has been offline.
// The rules themselves are in brainos/heartbeat.py, checked in tests/test_heartbeat.py.
//   node tests/heartbeat_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps functions of earlier scripts when it loads; stand-ins are enough here. The helpers of
// web/app.js it uses are written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
let renders = 0;
const sandbox = {
  h, badge, projectId: 'project-1', current: 'robots', setInterval: () => 0, render: () => { renders += 1; },
  state: {robots: [{id: 'robot-1', name: 'dog'}], heartbeat: {interval_seconds: 60, offline_after_seconds: 180}},
  engineRobotCard: r => `<article class="card"><h3>${h(r.name)}</h3><p>四足机器人</p></article>`,
  engineAction: async (type, id) => ['base', type, id],
  engineRefresh: () => {},
  // As web/live_status.js draws the head: its own badge says whether this one read reached the machine.
  liveHead: view => '<div class="ls-bar"><div class="tags">' +
    (view.reachable === null ? badge('正在连接…') : badge(view.reachable ? '在线' : '连不上', view.reachable ? 'good' : 'bad')) +
    '<span class="tag">真机 · 只读</span></div></div>',
};
vm.createContext(sandbox);
const api = vm.runInContext(readFileSync('web/heartbeat.js', 'utf8') +
  '\n;({hbDuration, hbWhen, hbLine, hbChecking, setLinks: hbSetLinks, card: r => engineRobotCard(r), head: v => liveHead(v),' +
  ' action: (t, i) => engineAction(t, i)})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const count = (text, part) => String(text).split(part).length - 1;

// A fixed "now": 2026-11-19 10:30:00 local time. Heartbeat times are seconds, as the backend sends them.
const now = new Date(2026, 10, 19, 10, 30, 0).getTime();
const ago = seconds => now / 1000 - seconds;
const link = (state, label, fields = {}) => ({state, label, last_heartbeat_at: null, error: null, retrying: true, step: null, ...fields});

// How long: whole minutes up to an hour, then hours and minutes, then days and hours.
check(api.hbDuration(0), '不到 1 分钟', 'under a minute');
check(api.hbDuration(59), '不到 1 分钟', 'still under a minute');
check(api.hbDuration(180), '3 分钟', 'three minutes');
check(api.hbDuration(3599), '59 分钟', 'under an hour');
check(api.hbDuration(3600 + 5 * 60), '1 小时 5 分', 'hours and minutes');
check(api.hbDuration(2 * 3600), '2 小时', 'whole hours');
check(api.hbDuration(86400 + 3 * 3600 + 60), '1 天 3 小时', 'days and hours');
check(api.hbDuration(NaN), '—', 'no time');

// When: the time today, the date as well on another day.
check(api.hbWhen(ago(53), now), '10:29:07', 'today: the time, with seconds');
includes(api.hbWhen(ago(86400 + 60), now), '11-18', 'another day: the date');
includes(api.hbWhen(ago(86400 + 60), now), '10:29', 'another day: the time');
check(api.hbWhen(null, now), '—', 'never');
check(api.hbWhen(NaN, now), '—', 'not a time');

// 在线: the state and the time of the last heartbeat.
let html = api.hbLine('robot-1', link('online', '在线', {last_heartbeat_at: ago(53)}), now);
includes(html, '<span class="status good">', 'online is green');
includes(html, '在线', 'the state');
includes(html, '最近心跳 10:29:07', 'the time of the last heartbeat');
excludes(html, 'engine-heartbeat-check', 'nothing to retry while online');
includes(html, 'data-hb-robot="robot-1"', 'the line can be found and redrawn');

// A heartbeat was missed, but three minutes have not passed: still online, and no alarm.
html = api.hbLine('robot-1', link('online', '在线', {last_heartbeat_at: ago(130),
  error: {hop: 'device', reason: 'unreachable', message: 'SSH 连接超时或设备不可达'}}), now);
includes(html, '在线', 'still online');
excludes(html, 'SSH 连接超时', 'one missed heartbeat is not shown as a fault');

// 离线: for how long, when it was last heard, why, and a way to try now.
html = api.hbLine('robot-1', link('offline', '离线', {last_heartbeat_at: ago(12 * 60),
  error: {hop: 'jump', reason: 'unreachable', message: '连不上跳板机；请核对跳板机的地址、端口和网络'}}), now);
includes(html, '<span class="status bad">', 'offline is red');
includes(html, '离线', 'the state');
includes(html, '已离线 12 分钟', 'how long');
includes(html, '最近心跳 10:18:00', 'when it was last heard');
includes(html, '跳板机：连不上跳板机', 'which hop and why');
includes(html, 'data-action="engine-heartbeat-check"', 'a retry');
includes(html, 'data-id="robot-1"', 'for this robot');
includes(html, 'aria-label="重试：检查 dog 的连接"', 'the retry says which robot it is for');
excludes(html, '停止自动重试', 'it is still asked every minute');
excludes(html, 'aria-disabled', 'the retry can be pressed');
// While the check runs the retry says so. It stays focusable (not `disabled`), so the keyboard focus is not dropped.
api.hbChecking.add('robot-1');
html = api.hbLine('robot-1', link('offline', '离线', {last_heartbeat_at: ago(12 * 60)}), now);
includes(html, '正在检查…', 'a check that is running');
includes(html, 'aria-disabled="true"', 'it cannot be started twice');
excludes(html, ' disabled', 'but the button keeps the focus');
api.hbChecking.delete('robot-1');
// A hop this page has no name for (a later backend) is called the connection, whatever its key is.
html = api.hbLine('robot-1', link('offline', '离线', {last_heartbeat_at: ago(900),
  error: {hop: 'constructor', reason: 'unreachable', message: 'SSH 会话中断'}}), now);
includes(html, '连接：SSH 会话中断', 'an unknown hop');

// A login the machine refused is not repeated: the line says so.
html = api.hbLine('robot-1', link('offline', '离线', {last_heartbeat_at: ago(900), retrying: false,
  error: {hop: 'device', reason: 'auth', message: '设备拒绝登录：SSH 认证失败'}}), now);
includes(html, '已停止自动重试', 'no more automatic logins');
includes(html, '目标机：设备拒绝登录', 'why');
includes(html, 'engine-heartbeat-check', 'the user can still ask');

// What a device or an old record says is shown as text, never as markup.
html = api.hbLine('robot-1', link('offline', '离线', {last_heartbeat_at: ago(900),
  error: {hop: 'device', reason: 'timeout', message: '<img src=x onerror=alert(1)>'}}), now);
excludes(html, '<img', 'markup in a reason is escaped');
includes(html, '&lt;img', 'and shown as text');
html = api.hbLine('"><script>', link('offline', '离线', {last_heartbeat_at: ago(900)}), now);
excludes(html, '<script>', 'a robot id cannot break out of its attribute');
html = api.hbLine('robot-1', link('<b>x</b>', '<i>y</i>'), now);
excludes(html, '<i>y</i>', 'a label is text');
excludes(html, '<b>x</b>', 'a state is text');

// 接入中: what is being done.
html = api.hbLine('robot-1', link('onboarding', '接入中', {last_heartbeat_at: ago(86400), step: '正在检查连接'}), now);
includes(html, '<span class="status blue hb-checking">', 'onboarding is marked as in progress');
includes(html, '接入中', 'the state');
includes(html, '正在检查连接', 'the step');
excludes(html, '已离线', 'not offline');

// 未接入: grey, with the reason of the last failure when there was one.
html = api.hbLine('robot-1', link('not_onboarded', '未接入', {retrying: false}), now);
includes(html, '<span class="status ">', 'not onboarded is grey');
includes(html, '未接入', 'the state');
includes(html, '还没有连接过', 'never connected');
excludes(html, 'engine-heartbeat-check', 'there is no SSH connection to ask');
html = api.hbLine('robot-1', link('not_onboarded', '未接入', {retrying: false,
  error: {hop: null, reason: 'connect_failed', message: '设备身份不一致'}}), now);
includes(html, '上一次失败：设备身份不一致', 'the reason of the last failure');

// A state this page does not know (a newer backend) is shown by its name, grey.
html = api.hbLine('robot-1', link('rebooting', '重启中'), now);
includes(html, '重启中', 'an unknown state keeps its name');

// The states arrive apart from the page's own state. The cards are drawn again only when a robot gains or
// loses its line, not for every heartbeat.
vm.runInContext(`Date.now = () => ${now}`, sandbox);        // the page's own clock, for what it draws by itself
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(53)})});
check(renders, 1, 'the first states: the cards get their lines');
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(1)})});
api.setLinks({'robot-1': link('offline', '离线', {last_heartbeat_at: ago(1)})});
check(renders, 1, 'a heartbeat or a change of state does not draw the page again');
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(53)}), 'robot-9': link('not_onboarded', '未接入')});
check(renders, 2, 'a new robot does');
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(53)})});
check(renders, 3, 'and so does one that is gone');

// On the card: under the name, only for robots that have a connection state.
html = api.card({id: 'robot-1', name: 'dog'});
includes(html, '<h3>dog</h3><div class="hb-line"', 'right under the name');
includes(html, '在线', 'the state on the card');
check(api.card({id: 'robot-2', name: 'sim'}), '<article class="card"><h3>sim</h3><p>四足机器人</p></article>',
      'a robot without a connection state keeps its card as it was');
check(api.card({id: 'constructor', name: 'odd id'}), '<article class="card"><h3>odd id</h3><p>四足机器人</p></article>',
      'an id that is also a property of every object has no state of its own');
// A reason with "$" in it (a path, a shell prompt) must not be read as a pattern when the line is put in.
api.setLinks({'robot-1': link('offline', '离线', {last_heartbeat_at: ago(600),
  error: {hop: 'device', reason: 'timeout', message: "no answer from $HOME/$'x' $& $`"}})});
html = api.card({id: 'robot-1', name: 'dog'});
check(count(html, '<p>四足机器人</p></article>'), 1, 'the rest of the card is there once');
includes(html, 'no answer from $HOME/$&#39;x&#39; $&amp; $`', 'the reason is shown as it is');

// On the status page: the state comes from the heartbeat, not from the one read that just failed.
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(53)})});
html = api.head({robot_id: 'robot-1', reachable: false, checked_at: ago(1)});
includes(html, '在线', 'a failed read does not turn an online robot into "连不上"');
excludes(html, '连不上', 'the old wording is gone');
includes(html, '最近心跳 10:29:07', 'the time of the last heartbeat on the status page');
includes(html, '真机 · 只读', 'the rest of the head stays');
api.setLinks({'robot-1': link('offline', '离线', {last_heartbeat_at: ago(600)})});
html = api.head({robot_id: 'robot-1', reachable: false, checked_at: ago(1)});
includes(html, '离线', 'offline once the heartbeat says so');
includes(html, '已离线 10 分钟', 'for how long');
// A read that has just reached the machine is an answer from it: online as of that read, until the states catch up.
html = api.head({robot_id: 'robot-1', reachable: true, checked_at: ago(2)});
includes(html, '<span class="status good">', 'a read that worked a moment ago shows online');
includes(html, '最近心跳 10:29:58', 'as of that read');
excludes(html, '离线', 'not offline next to fresh figures');
// But a read from long ago proves nothing about now: the dialog stayed open while later reads failed.
html = api.head({robot_id: 'robot-1', reachable: true, checked_at: ago(15 * 60)});
includes(html, '离线', 'an old read does not keep the robot online');
includes(html, '已离线 10 分钟', 'the heartbeat is what counts');
// A heartbeat that is newer than the read wins as well.
api.setLinks({'robot-1': link('online', '在线', {last_heartbeat_at: ago(5)})});
html = api.head({robot_id: 'robot-1', reachable: true, checked_at: ago(50)});
includes(html, '最近心跳 10:29:55', 'the newer of the two');
// While the first connection is being made the page says so itself.
html = api.head({robot_id: 'robot-1', reachable: null, checked_at: ago(1)});
includes(html, '正在连接…', 'connecting');
// Without a connection state (an older backend, or before the first answer) the head stays as it was.
api.setLinks({});
includes(api.head({robot_id: 'robot-1', reachable: false, checked_at: ago(1)}), '连不上', 'no state: unchanged');

// Other actions still reach the scripts loaded before.
api.action('engine-status', 'robot-1').then(result => {
  check(Array.from(result), ['base', 'engine-status', 'robot-1'], 'other actions are passed on');
  console.log(JSON.stringify({passed: true, checks}));
});
