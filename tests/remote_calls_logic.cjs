// Pure-logic checks of web/remote_calls.js (issue #43): what the 远程调用 section says about the link to
// the cloud, a switch, a call and the confirmation for switching on, and that names and records from
// other parties never reach the page as markup. The backend rules are in brainos/remote_calls.py,
// checked in tests/test_remote_calls.py.
//   node tests/remote_calls_logic.cjs
const {load, checker, HOSTILE, escaped} = require('./cloud_skills_logic_helper.cjs');

const {page} = load(['web/cloud_skills.js', 'web/remote_calls.js'], ['remoteWhen', 'remoteAgo', 'remoteLinkState', 'remoteSwitchNote',
  'remoteConfirmText', 'remoteCallNote', 'remoteSwitchHtml', 'remoteCallsHtml', 'remoteBody', 'remoteSection', 'REMOTE_UNSUPPORTED',
  'REMOTE_MOTION_WARNING', 'remoteAnswerOutdated', 'remoteSwitchRequest']);
const counted = checker(), {check, includes, excludes} = counted;
const NOW = 1795000000;

const skill = (changes = {}) => ({id: 'goto_place', version: '0.1.0', title: '去指定地点', summary: '', motion: true, allowed: false,
  stale: false, set_by: '', set_at: null, ...changes});
const link = (changes = {}) => ({configured: true, enabled: true, state: 'online', host: 'cloud.example.test', last_sync_at: NOW - 3,
  last_error: '', device_id: 'dlk_1', ...changes});
const view = (changes = {}) => ({supported: true, robot_id: 'robot-1', link: link(), skills: [skill()], skills_error: null, calls: [], ...changes});

// ---------------------------------------------------------------- times
check(page.remoteWhen(null), '—', 'no time');
check(page.remoteAgo(NOW - 3, NOW), '3 秒前', 'seconds');
check(page.remoteAgo(NOW - 125, NOW), '2 分钟前', 'minutes');
check(page.remoteAgo(NOW + 5, NOW), '0 秒前', 'a clock that is ahead is not "in the future"');
check(page.remoteAgo(null, NOW), '', 'never');
check(page.remoteAgo(0, NOW), '', 'zero is never');
includes(page.remoteAgo(NOW - 7200, NOW), ':', 'older than an hour: the time itself');

// ---------------------------------------------------------------- the link line
check(page.remoteLinkState(null, NOW).text, '没有配置云端连接', 'nothing known');
includes(page.remoteLinkState(link({configured: false}), NOW).detail, '第三方现在调用不到它', 'not configured: said plainly');
check(page.remoteLinkState(link({enabled: false}), NOW).text, '云端连接已关闭', 'switched off in the configuration');
check(page.remoteLinkState(link({state: 'off'}), NOW).text, '云端连接已关闭', 'the runtime says off');
let s = page.remoteLinkState(link(), NOW);
check([s.text, s.tone], ['已连上云端', 'good'], 'online');
check(s.detail, '云平台 cloud.example.test，上次同步 3 秒前', 'with the host and the last sync');
check(page.remoteLinkState(link({state: 'connecting'}), NOW).tone, 'blue', 'connecting');
s = page.remoteLinkState(link({state: 'error', last_error: '证书校验失败', last_sync_at: NOW - 90}), NOW);
check([s.text, s.tone], ['没有连上云端', 'bad'], 'an error');
check(s.detail, '云平台 cloud.example.test：证书校验失败（上次同步 1 分钟前）', 'with the reason and the last sync');
check(page.remoteLinkState(link({state: 'error', host: '', last_sync_at: null}), NOW).detail, '云平台', 'no host, never synced');

// ---------------------------------------------------------------- a switch
check(page.remoteSwitchNote(skill()), '', 'off: nothing to add');
includes(page.remoteSwitchNote(skill({allowed: true, set_by: '张工', set_at: NOW})), '由 张工 于', 'on: who and when');
excludes(page.remoteSwitchNote(skill({allowed: true, set_by: '', set_at: NOW})), '由', 'on without a name');
includes(page.remoteSwitchNote(skill({stale: true})), '重新安装过', 'a switch that lapsed says why');
let html = page.remoteSwitchHtml(skill());
includes(html, 'role="switch"', 'a switch for assistive technology');
includes(html, 'aria-checked="false"', 'off');
includes(html, '会让机器人运动', 'a skill that moves the robot says so');
includes(html, 'aria-label="允许远程调用：去指定地点"', 'named by its skill');
html = page.remoteSwitchHtml(skill({allowed: true, motion: false}));
includes(html, 'aria-checked="true"', 'on');
excludes(html, '会让机器人运动', 'a skill that does not move');
html = page.remoteSwitchHtml(skill({id: HOSTILE, title: HOSTILE, version: HOSTILE, allowed: true, set_by: HOSTILE, set_at: NOW}));
check(escaped(html), true, 'a skill\'s name and who switched it are escaped');

// ---------------------------------------------------------------- the confirmation
let words = page.remoteConfirmText(skill(), '巡检机器人 A');
check(words.title, '允许远程调用「去指定地点」？', 'what is asked');
check(words.warning, page.REMOTE_MOTION_WARNING, 'a skill that moves: the plain warning');
includes(words.warning, '第三方可以经云平台让这台机器人运动', 'third parties can make it move');
includes(words.warning, '不需要有人每次确认', 'without anyone confirming each time');
check(words.motion, true, 'drawn as a warning');
includes(words.check, '运动', 'the checkbox names it too');
words = page.remoteConfirmText(skill({motion: false, title: '状态报告'}), '巡检机器人 A');
check(words.motion, false, 'a skill that does not move: no warning tone');
includes(words.warning, '不会让机器人运动', 'and it says so');
excludes(words.check, '运动', 'nothing about moving in its checkbox');

// ---------------------------------------------------------------- an answer older than the last switch is dropped
check(page.remoteAnswerOutdated(3, 3), false, 'nothing was switched while it was on its way');
check(page.remoteAnswerOutdated(3, 4), true, 'a switch was sent since: the older answer does not overwrite it');

// ---------------------------------------------------------------- what a switch sends
let sent = page.remoteSwitchRequest('robot-1', 'project-1', skill({digest: 'a'.repeat(64)}), true, 'req-1');
check([sent.allowed, sent.confirmed, sent.digest], [true, true, 'a'.repeat(64)], 'on: bound to the content the dialog was about');
sent = page.remoteSwitchRequest('robot-1', 'project-1', skill({digest: 'a'.repeat(64)}), false, 'req-2');
check([sent.allowed, sent.confirmed, 'digest' in sent], [false, false, false], 'off: nothing to bind');

// ---------------------------------------------------------------- calls
check(page.remoteCallNote({error: {code: 'remote_not_allowed', message: '主人没有打开'}}), '主人没有打开', 'a refusal: its message');
check(page.remoteCallNote({error: {code: 'some_new_code', message: ''}}), 'some_new_code', 'without a message: its code');
check(page.remoteCallNote({started_at: 10, finished_at: 12.4}), '用时 2 秒', 'a finished call: how long');
check(page.remoteCallNote({started_at: 10, finished_at: null}), '', 'a running call');
includes(page.remoteCallsHtml([]), '还没有远程调用', 'no calls yet');
html = page.remoteCallsHtml([{call_id: 'cal_1', skill_id: 'goto_place', status: 'succeeded', started_at: NOW, finished_at: NOW + 4, error: null},
  {call_id: 'cal_2', skill_id: 'goto_place', status: 'rejected', started_at: null, finished_at: NOW, error: {code: 'estop_latched', message: '急停锁存中'}},
  {call_id: 'cal_3', skill_id: 'goto_place', status: 'something_new', started_at: NOW, finished_at: null, error: null}]);
includes(html, '已完成', 'a known state in words');
includes(html, '机器人拒绝了', 'a rejection in words');
includes(html, '急停锁存中', 'with its reason');
includes(html, 'something_new', 'an unknown state is shown as it is');
html = page.remoteCallsHtml([{call_id: HOSTILE, skill_id: HOSTILE, status: HOSTILE, started_at: NOW, finished_at: null, error: {code: HOSTILE, message: HOSTILE}}]);
check(escaped(html), true, 'a call record from a third party is escaped');

// ---------------------------------------------------------------- the section
includes(page.remoteBody(null), '正在读取', 'before the first answer');
html = page.remoteBody({supported: false, message: page.REMOTE_UNSUPPORTED});
check(page.REMOTE_UNSUPPORTED, '这台机器人的管理层还不支持远程调用', 'the plain sentence for an older management layer');
includes(html, page.REMOTE_UNSUPPORTED, 'an older management layer: that sentence');
excludes(html, 'role="switch"', 'and no switch');
excludes(html, 'form-error', 'not drawn as an error');
includes(page.remoteBody({supported: null, message: '读不到远程调用的状态：连接中断'}), '读不到', 'unreachable: said, not called unsupported');
html = page.remoteBody(view());
includes(html, 'id="remote-link"', 'the link line');
includes(html, 'id="remote-call-list"', 'the calls');
includes(html, 'data-remote-skill="goto_place"', 'one row per skill');
html = page.remoteBody(view({skills: [], skills_error: null}));
includes(html, '还没有安装技能', 'no skills installed');
html = page.remoteBody(view({link: link({host: HOSTILE, state: 'error', last_error: HOSTILE}), skills: [], skills_error: HOSTILE}));
check(escaped(html), true, 'what the robot reports about its link is escaped');
html = page.remoteSection({id: 'robot-1', endpoint: '', status: 'unconnected'});
includes(html, '连上这台机器人的管理层', 'no management layer: what to do first');
html = page.remoteSection({id: HOSTILE, endpoint: 'http://127.0.0.1:1', status: 'connected'});
check(escaped(html), true, 'the robot id is escaped in the section');

console.log(`remote calls logic checks passed: ${counted.checks}`);
