// Pure-logic checks of web/skill_install_guide.js (issue #62): a robot that is not connected cannot take
// a skill, and the two dialogs of 机器人应用 that lead to installing one must say so and offer the step
// that gets the robot connected, instead of a row of buttons that cannot be pressed.
//   node tests/skill_install_guide_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const sandbox = {h, button};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/skill_install_guide.js', 'utf8') +
  '\n;({sgStep, sgNotice, sgAdoptBody, sgInstallTarget, SG_ACTIONS})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const count = (text, part) => text.split(part).length - 1;
const actions = step => step.actions.map(a => [a.action, a.label]);

const connected = {id: 'r-on', name: '巡检狗', status: 'connected', endpoint: 'http://10.0.0.5:8790', created_at: '2026-11-20T01'};
const sshOnly = {id: 'r-ssh', name: 'L2U', status: 'unconnected', connection_id: 'conn-1', created_at: '2026-11-20T02'};
const linked = {id: 'r-link', name: '已接入的狗', status: 'unconnected', connection_id: 'conn-2', endpoint: 'ssh-forward:r-link', created_at: '2026-11-20T03'};
const direct = {id: 'r-direct', name: '直连的臂', status: 'unconnected', endpoint: 'http://10.0.0.9:8790', created_at: '2026-11-20T04'};
const sim = {id: 'r-sim', name: '仿真狗', status: 'unconnected', sim: true, endpoint: 'http://127.0.0.1:1', created_at: '2026-11-20T05'};
const bare = {id: 'r-bare', name: '只有名字', status: 'unconnected', created_at: '2026-11-20T06'};

// ---------------------------------------------------------------- the step that gets a robot connected
same(page.sgStep(connected), null, 'a connected robot needs no step');
same(page.sgStep(null), null, 'no robot, nothing to say');

const ssh = page.sgStep(sshOnly);
same(ssh.kind, 'layer', 'a robot reached over SSH only has no management layer linked');
same(actions(ssh), [['engine-runtime-install', '安装管理层'], ['engine-runtime-link', '接入已有的管理层']], 'both ways to get the management layer, install first');
ok(ssh.title.includes('管理层') && ssh.text.includes('SSH'), 'it says what is missing and what is there');
ok(ssh.actions.every(a => a.id === 'r-ssh'), 'the buttons act on this robot');

const again = page.sgStep(linked);
same(again.kind, 'connect', 'a robot with a management layer that is not answering is connected again');
same(actions(again), [['skillpkg-guide-connect', '重新连接'], ['engine-runtime-link', '管理层设置']], 'reconnect first; the settings of its link second');

same(actions(page.sgStep(direct)), [['skillpkg-guide-connect', '重新连接']], 'a robot connected by address has only the reconnect');
same(page.sgStep(sim).kind, 'robots', 'a simulated robot that is not running is started from 我的机器人');
same(actions(page.sgStep(sim)), [['skillpkg-guide-robots', '打开「我的机器人」']], 'simulated');
same(page.sgStep(bare).kind, 'robots', 'a robot with no way in at all');
same(actions(page.sgStep({...sshOnly, managed_resource_id: 'm-1'})), [['skillpkg-guide-robots', '打开「我的机器人」']],
     'a managed robot is not installed or linked by hand');

// ---------------------------------------------------------------- the notice above the skills of the install dialog
same(page.sgNotice(connected), '', 'nothing above the skills of a connected robot');
const note = page.sgNotice(sshOnly);
ok(note.includes('role="status"') && note.includes('sg-notice'), 'a notice that is read out when it appears');
same(count(note, '<button'), 2, 'two buttons');
ok(note.includes('data-action="engine-runtime-install"') && note.includes('data-id="r-ssh"'), 'the install button of this robot');
ok(note.includes('class="sg-error"') && note.includes('role="alert"'), 'a place for what goes wrong inside the dialog');
ok(!page.sgNotice({...sshOnly, id: 'r"<x>'}).includes('r"<x>'), 'ids are escaped');

// ---------------------------------------------------------------- 使用机器人上已安装的技能 with no robot connected
same(page.sgAdoptBody([]), '', 'no robots: the dialog keeps its own words (add a simulated robot)');
same(page.sgAdoptBody([sshOnly, connected]), '', 'a connected robot: the dialog reads its skills as before');
const body = page.sgAdoptBody([direct, sshOnly, {...bare, name: '<b>狗</b>'}]);
ok(body.includes('没有已连接的机器人'), 'it says that no robot is connected, not that no skill is installed');
ok(!body.includes('还没有安装技能'), 'nothing about missing skills');
same(count(body, 'class="contract-line sg-robot"'), 3, 'each robot once');
ok(body.indexOf('L2U') < body.indexOf('直连的臂'), 'in the order the robots were added');
ok(body.includes('&lt;b&gt;狗&lt;/b&gt;') && !body.includes('<b>狗</b>'), 'names are shown as text');
ok(body.includes('data-action="engine-runtime-install"') && body.includes('data-action="skillpkg-guide-connect"'), 'each robot with its own step');

// ---------------------------------------------------------------- which robot 「安装技能」 of that dialog opens
same(page.sgInstallTarget([sshOnly, connected, direct]), 'r-on', 'a connected robot when there is one');
same(page.sgInstallTarget([direct, sshOnly]), 'r-ssh', 'otherwise the robot added first');
same(page.sgInstallTarget([]), '', 'none');

same([...page.SG_ACTIONS], ['skillpkg-guide-connect', 'skillpkg-guide-robots'], 'the actions the page adds');

console.log(`skill install guide logic: ${checks} checks passed`);
