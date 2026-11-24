// Pure-logic checks of web/robots_page.js (issue #41): what a robot's card keeps in sight, what goes
// into its 「更多功能」 list, and that nothing another feature put on the card is lost on the way.
//   node tests/robots_page_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Helpers of web/app.js, written as they are there.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const sandbox = {h, button, badge, state: {robots: []}};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/robots_page.js', 'utf8') +
  '\n;({rpCard, rpButtons, rpPick, rpSplitTags, rpSummary, RP_MENU_LABEL})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };

// A card as the feature scripts leave it (the markup of a real robot reached over SSH on 0.21.4).
const ID = 'robot-1';
const real = ({tags, row, top = badge('可发起调查'), extra = ''}) =>
  `<article class="card"><div class="card-body"><div class="card-top"><span class="agent-avatar"></span><span class="button-row">${top}</span></div>` +
  `<h3>巡检机器人</h3><div class="hb-line" tabindex="-1" data-hb-robot="${ID}" data-hb-view="card"><span class="hb-state" data-hb-sig="online|在线">${badge('在线', 'good')}</span>` +
  '<span class="hb-facts">最近心跳 05:09:20</span><span class="hb-trouble" data-hb-sig=""></span></div>' +
  `<p><span class="body-type-name">四足机器人</span> <button type="button" class="text-link body-type-change" data-action="engine-body-type" data-id="${ID}" aria-label="修改 巡检机器人 的本体类型">修改类型</button><br>robot@192.0.2.10:22</p>` +
  `<div class="ri-line" data-ri-robot="${ID}" data-ri-sig="[]" hidden></div>` +
  `<div class="ra-line" data-ra-robot="${ID}" data-ra-sig="{}"><span class="ri-label">开机自启</span>${badge('未开机自启', 'warn')}<span class="ra-detail">还没有安装管理层</span>` +
  `<button type="button" class="text-link" data-action="engine-runtime-autostart" data-id="${ID}" aria-label="巡检机器人 的开机自启">设置</button></div>${extra}` +
  `<div class="tags">${tags}</div></div><div class="card-bottom"><div class="button-row">${row}</div></div></article>`;
const remove = `<button type="button" class="button danger-outline small robot-remove" data-action="engine-robot-remove" data-id="${ID}" aria-label="删除机器人 巡检机器人">删除</button>`;
const verify = `<button type="button" class="button secondary small cv-open" data-action="engine-ssh-verify" data-id="${ID}" data-cv-card title="按保存的 SSH 连接逐级登录一次">验证 SSH 连接</button>`;
const fullRow = (lead = button('开始调研', 'engine-discover', 'small', ID)) =>
  button('硬件初始化', 'hardware-open', 'secondary small', ID) + lead + button('实时状态', 'engine-status', 'secondary small', ID) +
  button('设备档案', 'engine-profile', 'secondary small', ID) + verify + button('配置连接', 'engine-ssh-edit', 'secondary small', ID) +
  button('接入管理层', 'engine-runtime-link', 'secondary small', ID) + button('安装管理层', 'engine-runtime-install', 'secondary small', ID) + remove;
const fullTags = badge('硬件 17 项 · 5 个问题', 'bad') + '<span class="tag">硬件与软件待调查</span><span class="tag">控制通道：未连接</span>';
const robot = {id: ID, name: '巡检机器人', status: 'unconnected', connection_id: 'c1'};

const shown = html => html.slice(0, html.indexOf('<details class="robot-menu"'));       // what is in sight without opening anything
const menu = html => html.slice(html.indexOf('<details class="robot-menu"'));
const actions = html => [...html.matchAll(/data-action="([^"]+)"/g)].map(m => m[1]);

// ---- a real robot that was just connected
{
  const before = real({tags: fullTags, row: fullRow()});
  const after = page.rpCard(before, robot, false);
  ok(after.startsWith('<article class="card rp-card"'), 'the card is marked as laid out');
  ok(after.endsWith('</article>'), 'the card is still one article');
  // Every control another feature put on the card is still on it, once.
  same(actions(after).sort().join(), actions(before).sort().join(), 'no action is lost or doubled');
  // In sight: the next step, the live state, and the list.
  same(actions(shown(after)).join(), 'engine-discover,engine-status', 'two buttons stay in sight');
  ok(/class="button small"[^>]*data-action="engine-discover"/.test(shown(after)), 'the next step is the filled button');
  ok(/class="button secondary small"[^>]*data-action="engine-status"/.test(shown(after)), 'the other one is quiet');
  ok(menu(after).includes(`>${page.RP_MENU_LABEL}</summary>`), 'the list is called 更多功能');
  ok(!menu(after).startsWith('<details class="robot-menu" data-rp-menu="robot-1" open'), 'the list is closed until asked for');
  // In the list, grouped; deleting comes last and apart.
  const m = menu(after);
  ok(m.indexOf('engine-ssh-verify') < m.indexOf('hardware-open') && m.indexOf('hardware-open') < m.indexOf('engine-runtime-install'),
     'connection, then device, then management layer');
  ok(m.indexOf('engine-robot-remove') > m.indexOf('engine-runtime-autostart'), 'deleting is the last entry');
  ok(/class="robot-menu-item robot-remove robot-menu-remove"[^>]*data-action="engine-robot-remove"/.test(m), 'deleting keeps its own class and is set apart');
  ok(/class="robot-menu-item cv-open"[^>]*data-cv-card/.test(m), 'the verify button keeps what its own script paints by');
  ok(m.includes('aria-label="删除机器人 巡检机器人"'), 'labels for screen readers are kept');
  ok(m.includes('aria-label="修改本体类型：巡检机器人"') && m.includes('>修改本体类型</button>'), 'the name read out starts with what the entry shows');
  ok(m.includes('role="group" aria-label="连接"'), 'the groups are named for screen readers');
  ok(!/class="button[ "]/.test(m.slice(m.indexOf('</summary>'))), 'entries of the list are not drawn as buttons');
  // The state lines other scripts draw in place are all still there, with what they are found by.
  for (const mark of ['data-hb-robot="robot-1"', 'data-ri-robot="robot-1"', 'data-ra-robot="robot-1"']) {
    same(after.split(mark).length, 2, `${mark} is on the card once`);
  }
  ok(shown(after).includes('data-hb-robot'), 'online / offline stays in sight');
  ok(!shown(after).includes('data-ra-robot') && m.includes('data-ra-robot'), 'start-at-boot moves into the list');
  // Of the tags, only what asks for attention stays in sight.
  ok(shown(after).includes('硬件 17 项 · 5 个问题'), 'a problem stays in sight');
  ok(!shown(after).includes('控制通道：未连接') && m.includes('控制通道：未连接'), 'a plain fact moves into the list');
  ok(!shown(after).includes('修改类型') && m.includes('engine-body-type'), 'changing the type moves into the list');
  ok(shown(after).includes('四足机器人') && shown(after).includes('robot@192.0.2.10:22'), 'type and address stay in sight');
  // Asked to be open (the page was drawn again while the list was open).
  ok(page.rpCard(before, robot, true).includes('<details class="robot-menu" data-rp-menu="robot-1" open>'), 'an open list stays open');
}

// ---- what leads
{
  const lead = (row, r = robot) => actions(shown(page.rpCard(real({tags: '', row}), r, false)));
  same(lead(fullRow(button('调研进展', 'engine-session', 'small', 's1'))).join(), 'engine-session,engine-status', 'a running investigation leads');
  const connected = {...robot, status: 'connected'};
  const withSkills = fullRow() + '';
  same(lead(withSkills.replace(remove, button('能力与技能', 'detail-robots', 'secondary small', ID) + remove), connected).join(),
       'detail-robots,engine-status', 'a connected robot leads with its skills');
  same(lead(button('调研进展', 'engine-session', 'small', 's1') + button('能力与技能', 'detail-robots', 'secondary small', ID) +
            button('实时状态', 'engine-status', 'secondary small', ID) + remove, connected).join(),
       'engine-session,engine-status', 'what is running comes before the skills');
  same(lead(button('解除急停', 'robot-reset', 'small', ID) + fullRow()).join(), 'robot-reset,engine-discover,engine-status',
       'releasing an emergency stop is never put away');
  // A robot that only has a service address.
  same(lead(button('配置 SSH', 'engine-ssh', 'small', ID) + button('连接检查', 'robot-connect', 'secondary small', ID) +
            button('适配器配置', 'robot-config', 'secondary small', ID) + remove, {...robot, connection_id: null}).join(),
       'engine-ssh', 'without anything else to show, one button');
}

// ---- simulated robots
{
  const sim = row => '<article class="card sim-card"><div class="card-body"><div class="card-top"></div><h3>仿真四足机器人</h3><p>运行在这台电脑上</p>' +
    '<div class="tags"><span class="tag">9 项能力</span></div><p class="inline-note">仿真结果只说明逻辑是否正确，不能作为现场证据。</p></div>' +
    `<div class="card-bottom"><div class="button-row">${row}</div></div></article>`;
  const simRobot = {id: 'sim-1', name: '仿真四足机器人', status: 'connected', sim: {state: 'running'}};
  const running = page.rpCard(sim(button('实时状态', 'sim-live', 'small', 'sim-1') + button('能力与技能', 'detail-robots', 'secondary small', 'sim-1') +
    button('关闭仿真', 'sim-stop', 'secondary small', 'sim-1') + button('删除', 'sim-remove', 'secondary small', 'sim-1')), simRobot, false);
  ok(running.startsWith('<article class="card rp-card sim-card"'), 'a simulated robot keeps its own class');
  same(actions(shown(running)).join(), 'sim-live,detail-robots', 'a running simulation: watch it, use it');
  same(actions(menu(running)).join(), 'sim-stop,sim-remove', 'stopping and deleting are in the list');
  ok(shown(running).includes('不能作为现场证据'), 'the note about simulated results stays in sight');
  const stopped = page.rpCard(sim(button('启动仿真', 'sim-start', 'small', 'sim-1') + button('删除', 'sim-remove', 'secondary small', 'sim-1')), simRobot, false);
  same(actions(shown(stopped)).join(), 'sim-start', 'a stopped simulation: start it');
  const starting = page.rpCard(sim(''), simRobot, false);
  ok(!starting.includes('robot-menu') || starting.includes('9 项能力'), 'a card without buttons loses nothing');
  ok(!actions(starting).length, 'and gains none');
}

// ---- things this file does not know
{
  const odd = page.rpCard(real({tags: fullTags, row: fullRow() + button('新功能', 'engine-something-new', 'secondary small', ID)}), robot, false);
  ok(menu(odd).includes('engine-something-new'), 'a button of a later feature lands in the list');
  ok(menu(odd).indexOf('engine-something-new') < menu(odd).indexOf('engine-robot-remove'), 'before deleting');
  const blocked = '<div class="safety-limits-card safety-limits-card-blocked">' + badge('安全限值未填，不能运动', 'bad') +
    `<button type="button" class="button small" data-action="engine-safety-limits-edit" data-id="${ID}" aria-label="填写安全限值：巡检机器人">填写安全限值</button></div>`;
  const withBlock = page.rpCard(real({tags: fullTags, row: fullRow(), extra: blocked}), robot, false);
  ok(shown(withBlock).includes('engine-safety-limits-edit'), 'a block that asks for attention stays where it is');
  const foreign = '<article class="card"><div class="card-body"><h3>别的卡片</h3></div></article>';
  same(page.rpCard(foreign, robot, false), foreign, 'a card of another shape is left as it is');
  const dollar = page.rpCard(real({tags: '<span class="tag">价格 $1 $& $\'</span>', row: fullRow()}), robot, false);
  ok(dollar.includes('价格 $1 $&amp; $&#39;') || dollar.includes("价格 $1 $& $'"), 'a "$" in a text is not read as a pattern');
}

// ---- tags
{
  const [seen, quiet] = page.rpSplitTags(badge('调研中 · 读取硬件', 'blue') + '<span class="tag">3 项设备能力</span>' + badge('硬件 17 项正常', 'good') +
    '<span class="tag body-type-flag">设备自报：轮式底盘</span>' + badge('运控驱动缺 2 项', 'bad') + ' ' + badge('锁存守护已放宽', 'warn'));
  ok(seen.includes('调研中') && seen.includes('运控驱动缺 2 项') && seen.includes('锁存守护已放宽') && seen.includes('设备自报'), 'work in progress, problems and warnings stay');
  ok(quiet.includes('3 项设备能力') && quiet.includes('硬件 17 项正常'), 'plain facts and "all fine" go into the list');
  ok(!seen.includes('3 项设备能力') && !quiet.includes('运控驱动缺'), 'each tag is in one place');
  const [, kept] = page.rpSplitTags('<span class="tag">一</span><a href="#x">链接</a> 文字 <span class="tag">二</span><button data-action="x">按钮</button>');
  ok(kept.includes('<a href="#x">链接</a>') && kept.includes('文字') && kept.includes('<button data-action="x">按钮</button>'), 'what is not a tag is not dropped');
}

// ---- the line under the page title
{
  same(page.rpSummary([{}, {sim: {}}, {sim: {}}]), '1 台真机 · 2 台仿真', 'real and simulated robots are counted apart');
  same(page.rpSummary([{}]), '1 台真机', 'only what there is');
  same(page.rpSummary([{sim: {}}]), '1 台仿真', 'only what there is');
}

console.log(`robots_page_logic: ${checks} checks passed`);
