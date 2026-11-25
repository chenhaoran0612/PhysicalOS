// Pure-logic checks of web/robot_dialogs.js (issue #41, second part): the one head every dialog of a
// robot gets (what it does, then which robot), and the row that leads to the robot's other dialogs.
//   node tests/robot_dialogs_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const sandbox = {h, state: {robots: []}};
vm.createContext(sandbox);
const rd = vm.runInContext(readFileSync('web/robot_dialogs.js', 'utf8') +
  '\n;({rdHead, rdSub, rdNavItems, rdNav, rdNames, RD_NAV})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const plain = value => JSON.parse(JSON.stringify(value));

const NAME = '巡检机器人';

// ---- the head: the four ways the feature scripts name a robot's dialog become one
{
  same(plain(rd.rdHead(NAME, '真机状态 · 每分钟通过 SSH 读取一次，只读，不会让机器人运动', [NAME])),
    {fn: '实时状态', name: NAME, rest: '每分钟通过 SSH 读取一次，只读，不会让机器人运动'},
    'the robot as the title: what the dialog does comes out of the line under it, named as the button that opens it');
  same(plain(rd.rdHead(NAME, '设备档案 · 硬件、系统、ROS 2 和资源占用', [NAME])),
    {fn: '设备档案', name: NAME, rest: '硬件、系统、ROS 2 和资源占用'}, 'the same for the profile');
  same(plain(rd.rdHead(NAME, '验证 SSH 连接', [NAME])), {fn: '验证 SSH 连接', name: NAME, rest: ''},
    'nothing after the name of the function: nothing is left for the line');
  same(plain(rd.rdHead('硬件初始化 · ' + NAME, 'robot@127.0.0.1:22 · 只读', [NAME])),
    {fn: '硬件初始化', name: NAME, rest: 'robot@127.0.0.1:22 · 只读'}, 'the robot after the function in the title');
  same(plain(rd.rdHead('安装管理层', NAME, [NAME])), {fn: '安装管理层', name: NAME, rest: ''}, 'the robot as the line under the title');
  same(plain(rd.rdHead('配置连接', NAME + ' · 修改这台机器人的 SSH 连接。', [NAME])),
    {fn: '配置连接', name: NAME, rest: '修改这台机器人的 SSH 连接。'}, 'the robot at the start of the line under the title');
}

// ---- a dialog that is not about a robot stays as it is
{
  same(rd.rdHead('工程模型', '工程助手的大模型只对接 TokenFans', [NAME]), null, 'no robot is named');
  same(rd.rdHead('添加机器人', '三种方式', []), null, 'no robots at all');
  same(rd.rdHead(NAME, '', [NAME]), null, 'a title that is only the name says nothing about what the dialog does');
  same(rd.rdHead('硬件初始化 · ', '连接成功', ['']), null, 'an empty name matches nothing');
  same(rd.rdHead('巡检机器人二号', '真机状态 · 只读', [NAME]), null, 'another robot whose name starts the same way');
  same(rd.rdHead('备注', NAME + '的说明', [NAME]), null, 'a line that merely starts with the name');
}

// ---- names
{
  same(plain(rd.rdHead('巡检机器人 二号', '设备档案 · 硬件', [NAME, '巡检机器人 二号'])), {fn: '设备档案', name: '巡检机器人 二号', rest: '硬件'},
    'the robot whose whole name it is');
  same(plain(rd.rdHead('硬件初始化 · A · B', '只读', ['B', 'A · B'])), {fn: '硬件初始化', name: 'A · B', rest: '只读'},
    'a name with the separator in it: the longest name that fits');
  same(plain(rd.rdNames([{name: 'a<b'}, {name: 'A · B · C'}, {name: ''}, {}, {name: 'a<b'}])), ['A · B · C', 'a&lt;b'],
    'names as the dialogs write them (escaped), the longest first, each once, none empty');
  same(plain(rd.rdHead('a&lt;b', '设备档案 · 硬件', rd.rdNames([{name: 'a<b'}]))), {fn: '设备档案', name: 'a&lt;b', rest: '硬件'},
    'an escaped name is found and stays escaped');
}

// ---- the line under the title
{
  same(rd.rdSub(NAME, '只读'), `<b class="rd-robot">${NAME}</b><span class="rd-rest">只读</span>`, 'the robot, then the rest');
  same(rd.rdSub(NAME, ''), `<b class="rd-robot">${NAME}</b>`, 'the robot alone');
  ok(!rd.rdSub('a&lt;b', '').includes('<b class="rd-robot">a<b'), 'the name is not unescaped on the way');
}

// ---- the row to the robot's other dialogs
{
  const card = ['hardware-open', 'engine-discover', 'engine-status', 'engine-profile', 'engine-ssh-verify', 'engine-ssh-edit',
                'engine-runtime-link', 'engine-runtime-install', 'engine-runtime-autostart', 'engine-robot-remove', 'engine-body-type'];
  const items = plain(rd.rdNavItems(card));
  same(items.map(item => item[0]), ['engine-status', 'engine-profile', 'hardware-open', 'engine-ssh-edit', 'engine-runtime-link'],
    'the dialogs one moves between, in one order whatever the order on the card; one entry for the management layer');
  same(items.map(item => item[1]), ['实时状态', '设备档案', '硬件', '连接', '管理层'], 'short names');
  same(plain(rd.rdNavItems(['engine-status', 'engine-runtime-install', 'engine-ssh-edit'])).map(item => item[0]),
    ['engine-status', 'engine-ssh-edit', 'engine-runtime-install'], 'not linked yet: the management layer entry installs it');
  same(plain(rd.rdNavItems(['engine-robot-remove', 'sim-start'])), [], 'a card without these dialogs has no row');
  ok(!card.filter(a => !rd.RD_NAV.some(([action]) => action === a)).some(a => items.some(item => item[0] === a)),
    'nothing that changes or deletes the robot at one click is in the row');

  const nav = rd.rdNav(items, 'engine-profile', 'robot-1');
  ok(nav.startsWith('<nav class="rd-nav" aria-label="这台机器人的其他功能">'), 'a labelled navigation');
  same((nav.match(/<button /g) || []).length, 5, 'one button for each');
  ok(nav.includes('data-action="engine-profile" data-id="robot-1" aria-current="true"'), 'the open one is marked');
  same((nav.match(/aria-current/g) || []).length, 1, 'and only that one');
  ok(nav.includes('data-action="hardware-open" data-id="robot-1">硬件</button>'), 'the others open the dialog of the same robot');
  ok(/type="button"/.test(nav) && !/type="submit"/.test(nav), 'no button of the row submits a form it is shown above');
  same(rd.rdNav(items, 'engine-robot-remove', 'robot-1'), '', 'a dialog that is not one of the row gets no row');
  same(rd.rdNav(items.slice(0, 1), 'engine-status', 'robot-1'), '', 'one entry leads nowhere: no row');
  same(rd.rdNav(items, 'engine-status', ''), '', 'no robot: no row');
  ok(rd.rdNav(items, 'engine-status', 'a"b').includes('data-id="a&quot;b"'), 'the id is escaped');
  ok(rd.rdNav(items, 'engine-runtime-install', 'robot-1').includes('data-action="engine-runtime-link" data-id="robot-1" aria-current="true"'),
    'installing is the management layer entry while a link entry is what the card has');
}

console.log(`robot_dialogs_logic: ${checks} checks passed`);
