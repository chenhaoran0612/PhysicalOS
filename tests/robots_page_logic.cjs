// Pure-logic checks of web/robots_page.js (issues #41 and #53): what a robot's card keeps in sight, what goes
// into its 「更多功能」 list, what is shown in 「实时状态」 instead, and that nothing another feature put on the card is lost on the way.
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
// web/robot_catalog.js answers what a robot looks like; here a stand-in that says which robot it was asked about.
const rcPicture = r => ({src: '/robot_images/' + (r.product ? 'model.webp' : 'type-other.svg'), alt: r.product ? r.product.vendor + ' ' + r.product.model : '机器人', exact: Boolean(r.product)});
const engineRobotOptions = () => '<div class="journey-options robot-entry"><article class="journey-option"><small class="entry-eyebrow">没有真机</small><h3>添加仿真机器人</h3><p>说明文字。</p>' +
  button('添加仿真机器人', 'sim-add', 'small') + '</article></div>';
const sandbox = {h, button, badge, rcPicture, engineRobotOptions, state: {robots: []}};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/robots_page.js', 'utf8') +
  '\n;({rpCard, rpParts, rpOverview, rpButtons, rpPick, rpSummary, rpWays, RP_MENU_LABEL})', sandbox);

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
const fullTags = badge('硬件故障 2 项', 'bad') + '<span class="tag">硬件与软件待调查</span><span class="tag">控制通道：未连接</span>';

const robot = {id: ID, name: '巡检机器人', status: 'unconnected', connection_id: 'c1', product: {vendor: '示例机器人公司', model: 'X1'}};
const menu = html => (html.includes('<details class="robot-menu"') ? html.slice(html.indexOf('<details class="robot-menu"')) : '');
const kept = html => (html.match(/<div class="rp-details" hidden>[\s\S]*?<\/div><\/div><div class="card-bottom">/) || [''])[0];
// What is in sight without opening anything: the card without its list and without what it keeps for 「实时状态」.
const shown = html => html.replace(menu(html), '').replace(kept(html), '');
const actions = html => [...html.matchAll(/data-action="([^"]+)"/g)].map(m => m[1]);
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

// ---- a real robot that was just connected
{
  const before = real({tags: fullTags, row: fullRow()});
  const after = page.rpCard(before, robot, false);
  ok(after.startsWith('<article class="card rp-card" data-rp-robot="robot-1">'), 'the card is marked as laid out and says whose it is');
  ok(after.endsWith('</article>'), 'the card is still one article');
  // Every control another feature put on the card is still on it, once.
  same(actions(after).sort().join(), actions(before).sort().join(), 'no action is lost or doubled');
  // In sight: the picture, the name, online or not, brand and model, the next step, the live state, the list. Nothing else.
  same(actions(shown(after)).join(), 'engine-discover,engine-status', 'two buttons stay in sight');
  // (The heartbeat line is kept whole, for the script that paints it in place; the style sheet shows only its badge, which
  // tests/robots_page_browser.cjs checks.)
  same(text(shown(after)), '巡检机器人 在线 最近心跳 05:09:20 示例机器人公司 · X1 开始调研 实时状态', 'the only texts outside the list and the kept part');
  ok(shown(after).includes('<img src="/robot_images/model.webp" alt="示例机器人公司 X1"') && !shown(after).includes('rp-picture-type'), 'the picture of its model');
  ok(/class="button small"[^>]*data-action="engine-discover"/.test(shown(after)), 'the next step is the filled button');
  ok(/class="button secondary small rp-status-button"[^>]*data-action="engine-status"/.test(shown(after)), 'the live state is quiet');
  ok(shown(after).includes('<i class="rp-attention"'), 'a proven problem among the states marks the live state button');
  ok(menu(after).includes(`>${page.RP_MENU_LABEL}</summary>`), 'the list is called 更多功能');
  ok(!menu(after).startsWith('<details class="robot-menu" data-rp-menu="robot-1" open'), 'the list is closed until asked for');
  // In the list, grouped; deleting comes last and apart. Only entries: no states, no notes.
  const m = menu(after);
  ok(m.indexOf('engine-ssh-verify') < m.indexOf('hardware-open') && m.indexOf('hardware-open') < m.indexOf('engine-runtime-install'),
     'connection, then device, then management layer');
  ok(m.indexOf('engine-robot-remove') > m.indexOf('engine-runtime-install'), 'deleting is the last entry');
  ok(/class="robot-menu-item robot-remove robot-menu-remove"[^>]*data-action="engine-robot-remove"/.test(m), 'deleting keeps its own class and is set apart');
  ok(/class="robot-menu-item cv-open"[^>]*data-cv-card/.test(m), 'the verify button keeps what its own script paints by');
  ok(m.includes('aria-label="删除机器人 巡检机器人"'), 'labels for screen readers are kept');
  ok(m.includes('aria-label="修改本体类型：巡检机器人"') && m.includes('>修改本体类型</button>'), 'the name read out starts with what the entry shows');
  ok(m.includes('role="group" aria-label="连接"'), 'the groups are named for screen readers');
  ok(!/class="button[ "]/.test(m.slice(m.indexOf('</summary>'))), 'entries of the list are not drawn as buttons');
  same(m.replace(/<button\b[^>]*>[\s\S]*?<\/button>/g, '').replace(/<[^>]+>/g, '').replace(/\s/g, ''), '更多功能连接设备管理层', 'the list holds entries and the names of its groups, nothing else');
  // The state lines other scripts draw in place are all still on the card, once, with what they are found by.
  for (const mark of ['data-hb-robot="robot-1"', 'data-ri-robot="robot-1"', 'data-ra-robot="robot-1"']) {
    same(after.split(mark).length, 2, `${mark} is on the card once`);
  }
  ok(shown(after).includes('data-hb-robot'), 'online / offline stays in sight');
  // Everything else is kept, out of sight, for 「实时状态」.
  const k = kept(after);
  for (const what of ['最近心跳 05:09:20', '四足机器人', 'robot@192.0.2.10:22', '硬件故障 2 项', '控制通道：未连接', '硬件与软件待调查', '开机自启', '可发起调查', 'data-ra-robot', 'data-ri-robot']) {
    ok((k + shown(after)).includes(what), `"${what}" is still on the card`);
    if (what !== '最近心跳 05:09:20') ok(k.includes(what) && !shown(after).includes(what), `"${what}" is out of sight`);
  }
  ok(!k.includes('修改类型') && !k.includes('<h3>') && !k.includes('agent-avatar'), 'the name, the old picture and the type button are not kept twice');
  // 「实时状态」 shows the heartbeat line and everything that is kept, in the order the card has them.
  const overview = page.rpOverview(before, robot);
  ok(overview.startsWith('<section class="rp-overview" data-rp-overview="robot-1"'), 'the overview says whose it is');
  const inner = k.slice('<div class="rp-details" hidden>'.length, -'</div></div><div class="card-bottom">'.length);
  ok(overview.includes('最近心跳 05:09:20') && overview.endsWith(inner + '</section>'), 'the overview is the heartbeat line and what the card keeps');
  // Asked to be open (the page was drawn again while the list was open).
  ok(page.rpCard(before, robot, true).includes('<details class="robot-menu" data-rp-menu="robot-1" open>'), 'an open list stays open');
  // Nothing of a problem among the states: no mark. A warning alone is not one.
  ok(!page.rpCard(real({tags: badge('锁存守护已放宽', 'warn') + '<span class="tag">控制通道：未连接</span>', row: fullRow()}), robot, false).includes('rp-attention'),
     'a warning does not mark the button');
  ok(page.rpCard(real({tags: '<span class="tag body-type-flag">设备自报：轮式底盘</span>', row: fullRow()}), robot, false).includes('rp-attention'),
     'a body type the device disagrees with does');
}

// ---- brand and model
{
  const none = page.rpCard(real({tags: '', row: fullRow()}), {...robot, product: null}, false);
  ok(shown(none).includes('<p class="rp-model rp-model-missing">未填写品牌型号</p>'), 'a robot without a brand and model says so');
  ok(shown(none).includes('rp-picture-type') && shown(none).includes('type-other.svg'), 'and shows a drawing instead of a picture');
  const odd = page.rpCard(real({tags: '', row: fullRow()}), {...robot, name: 'A<b>', product: {vendor: '<i>V', model: 'M"1'}}, false);
  ok(shown(odd).includes('&lt;i&gt;V') && shown(odd).includes('M&quot;1') && shown(odd).includes('<h3>A&lt;b&gt;</h3>'), 'names are written as text');
}

// ---- what leads
{
  const lead = (row, r = robot) => actions(shown(page.rpCard(real({tags: '', row}), r, false)));
  same(lead(fullRow(button('调研进展', 'engine-session', 'small', 's1'))).join(), 'engine-session,engine-status', 'a running investigation leads');
  const connected = {...robot, status: 'connected'};
  same(lead(fullRow().replace(remove, button('能力与技能', 'detail-robots', 'secondary small', ID) + remove), connected).join(),
       'detail-robots,engine-status', 'a connected robot leads with its skills');
  same(lead(button('调研进展', 'engine-session', 'small', 's1') + button('能力与技能', 'detail-robots', 'secondary small', ID) +
            button('实时状态', 'engine-status', 'secondary small', ID) + remove, connected).join(),
       'engine-session,engine-status', 'what is running comes before the skills');
  same(lead(button('解除急停', 'robot-reset', 'small', ID) + fullRow()).join(), 'robot-reset,engine-discover,engine-status',
       'releasing an emergency stop is never put away');
  // A robot that only has a service address: no live page of its own, so 「实时状态」 is the overview.
  const bare = page.rpCard(real({tags: '<span class="tag">控制通道：未连接</span>', row: button('配置 SSH', 'engine-ssh', 'small', ID) + button('连接检查', 'robot-connect', 'secondary small', ID) +
    button('适配器配置', 'robot-config', 'secondary small', ID) + remove}).replace(/<div class="hb-line"[\s\S]*?<\/div>/, ''), {...robot, connection_id: null}, false);
  same(actions(shown(bare)).join(), 'engine-ssh,engine-robot-overview', 'every card has 实时状态');
  ok(/data-action="engine-robot-overview" data-id="robot-1">实时状态</.test(shown(bare)), 'it is called 实时状态 and names the robot');
  ok(shown(bare).includes(badge('离线')), 'without a heartbeat line the card says online or not from the control channel');
  ok(shown(page.rpCard(real({tags: '', row: remove}).replace(/<div class="hb-line"[\s\S]*?<\/div>/, ''), {...robot, status: 'connected'}, false)).includes(badge('在线', 'good')), 'connected is online');
  // Both live pages: the one over SSH is 实时状态, the other one is in the list.
  const both = page.rpCard(real({tags: '', row: fullRow() + button('技能运行状态', 'sim-live', 'secondary small', ID)}), robot, false);
  same(actions(shown(both)).join(), 'engine-discover,engine-status', 'the live page over SSH is the status button');
  ok(menu(both).includes('data-action="sim-live"'), 'the other one is in the list');
}

// ---- simulated robots
{
  const top = badges => `<div class="card-top"><span class="agent-avatar"></span><span class="button-row">${badges}</span></div>`;
  const sim = (row, corner = top(badge('仿真', 'blue sim') + badge('运行中', 'good'))) => `<article class="card sim-card"><div class="card-body">${corner}<h3>仿真四足机器人</h3>` +
    '<p>仿真四足机器人 · 运行在这台电脑上<br>地址和令牌由应用自动管理</p><div class="tags"><span class="tag">9 项能力</span></div>' +
    '<p class="inline-note">仿真结果只说明逻辑是否正确，不能作为现场证据。</p></div>' +
    `<div class="card-bottom"><div class="button-row">${row}</div></div></article>`;
  const simRobot = {id: 'sim-1', name: '仿真四足机器人', status: 'connected', sim: {state: 'running'}, product: {vendor: 'neuxmind-sim', model: 'sim-quadruped'}};
  const running = page.rpCard(sim(button('实时状态', 'sim-live', 'small', 'sim-1') + button('能力与技能', 'detail-robots', 'secondary small', 'sim-1') +
    button('关闭仿真', 'sim-stop', 'secondary small', 'sim-1') + button('删除', 'sim-remove', 'secondary small', 'sim-1')), simRobot, false);
  ok(running.startsWith('<article class="card rp-card sim-card"'), 'a simulated robot keeps its own class');
  same(actions(shown(running)).join(), 'detail-robots,sim-live', 'a running simulation: use it, watch it');
  same(text(shown(running)), '仿真四足机器人 运行中 neuxmind-sim · sim-quadruped 能力与技能 实时状态', 'the only texts in sight');
  same(actions(menu(running)).join(), 'sim-stop,sim-remove', 'stopping and deleting are in the list');
  // Issue #53: what only explains is gone, from the card and from the list.
  ok(!running.includes('不能作为现场证据') && !running.includes('地址和令牌'), 'the notes that only explain are gone');
  ok(kept(running).includes('9 项能力') && kept(running).includes(badge('仿真', 'blue sim')) && !kept(running).includes('运行中'), 'what the card says about the robot is kept for 实时状态');
  const stopped = page.rpCard(sim(button('启动仿真', 'sim-start', 'small', 'sim-1') + button('删除', 'sim-remove', 'secondary small', 'sim-1'), top(badge('仿真', 'blue sim') + badge('已停止'))), simRobot, false);
  same(actions(shown(stopped)).join(), 'sim-start,engine-robot-overview', 'a stopped simulation: start it; 实时状态 is the overview');
  ok(shown(stopped).includes(badge('已停止')), 'and it says that it is stopped');
  const starting = page.rpCard(sim(''), simRobot, false);
  ok(!starting.includes('robot-menu'), 'a card without anything to put away has no list');
  same(actions(starting).join(), 'engine-robot-overview', 'and only 实时状态');
  const odd = page.rpCard(sim('', top(badge('仿真', 'blue sim') + '<button type="button">x</button>')), simRobot, false);
  ok(kept(odd).includes('<button type="button">x</button>'), 'a corner with something else in it is kept whole');
}

// ---- things this file does not know
{
  const odd = page.rpCard(real({tags: fullTags, row: fullRow() + button('新功能', 'engine-something-new', 'secondary small', ID)}), robot, false);
  ok(menu(odd).includes('engine-something-new'), 'a button of a later feature lands in the list');
  ok(menu(odd).indexOf('engine-something-new') < menu(odd).indexOf('engine-robot-remove'), 'before deleting');
  const blocked = '<div class="safety-limits-card safety-limits-card-blocked">' + badge('安全限值未填，不能运动', 'bad') +
    `<button type="button" class="button small" data-action="engine-safety-limits-edit" data-id="${ID}" aria-label="填写安全限值：巡检机器人">填写安全限值</button></div>`;
  const withBlock = page.rpCard(real({tags: '', row: fullRow(), extra: blocked}), robot, false);
  ok(kept(withBlock).includes('engine-safety-limits-edit') && page.rpOverview(real({tags: '', row: fullRow(), extra: blocked}), robot).includes('engine-safety-limits-edit'),
     'a block another feature adds to the card is shown in 实时状态');
  ok(shown(withBlock).includes('rp-attention'), 'and when it is a problem the button is marked');
  const foreign = '<article class="card"><div class="card-body"><h3>别的卡片</h3></div></article>';
  same(page.rpCard(foreign, robot, false), foreign, 'a card of another shape is left as it is');
  same(page.rpOverview(foreign, robot), '', 'and has no overview');
  const dollar = page.rpCard(real({tags: '<span class="tag">价格 $1 $& $\'</span>', row: fullRow()}), robot, false);
  ok(dollar.includes("价格 $1 $& $'"), 'a "$" in a text is not read as a pattern');
}

// ---- the three ways to add a robot: a title and its button, nothing that describes
{
  const ways = page.rpWays();
  ok(!ways.includes('entry-eyebrow') && !ways.includes('<p>') && !ways.includes('说明文字'), 'what described a way is gone');
  ok(ways.includes('<h3>添加仿真机器人</h3>') && ways.includes('data-action="sim-add"'), 'its title and its button are kept');
}

// ---- the line under the page title
{
  same(page.rpSummary([{}, {sim: {}}, {sim: {}}]), '1 台真机 · 2 台仿真', 'real and simulated robots are counted apart');
  same(page.rpSummary([{}]), '1 台真机', 'only what there is');
  same(page.rpSummary([{sim: {}}]), '1 台仿真', 'only what there is');
}

console.log(`robots_page_logic: ${checks} checks passed`);
