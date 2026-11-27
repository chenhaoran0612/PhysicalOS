'use strict';
// Issue #41 — "我的机器人", tidied.
//
// Over time every feature put its own buttons, lines and tags on a robot's card (a real robot had eleven
// buttons and six tags) and its own section on the page. Issue #41 laid the page out again; issue #53
// took what was left of the texts and figures off the card:
//
//   * A card is what the robot looks like, its name, whether it is online, its brand and model, the
//     next step, 「实时状态」 and the 「更多功能」 list. Nothing else is in sight, and nothing describes.
//   * Everything else the features put on the card (when the last heartbeat was and why none comes,
//     the body type and address, the state tags, start at boot, the progress of an installation) is
//     kept in the card, out of sight (`.rp-details`), and shown in the robot's 「实时状态」 above its
//     live figures (`.rp-overview`). A robot without a live page of its own gets a dialog with that.
//   * The page follows its state. Without a robot it shows the three ways to add one. With a robot it
//     shows the robots and one 「添加机器人」 button; the three ways are in that button's dialog.
//   * The two settings that are not a robot's (the assistant's models, the list of body types) are in
//     the 「设置」 list of the page head, each in its own dialog.
//
// The other scripts are not edited: each still builds its part of a card by wrapping engineRobotCard.
// rpParts() takes the finished card apart, keeping every element (and the attributes the in-place
// painters of heartbeat.js, runtime_install.js, runtime_autostart.js and connection_verify.js find
// them by). It is a pure function of the card's markup: tests/robots_page_logic.cjs.
// The page is taken over once every script has loaded (DOMContentLoaded), so the place of this file in
// index.html does not matter. A later feature that adds a button to the card needs nothing here: an
// action this file does not know lands in the list under 「其他」; anything else it adds to the card's
// body is shown in 「实时状态」.

const RP_MENU_LABEL = '更多功能';
const RP_BOTTOM = '<div class="card-bottom"><div class="button-row">', RP_END = '</div></div></article>';
const RP_TAGS = '<div class="tags">';
// The list, in this order. An action that is in none of the groups goes to 「其他」.
const RP_GROUPS = [
  ['连接', ['engine-ssh', 'engine-ssh-verify', 'engine-ssh-edit', 'robot-connect', 'robot-config']],
  ['设备', ['engine-status', 'sim-live', 'detail-robots', 'engine-profile', 'engine-inventory', 'hardware-open', 'engine-drivers',
            'engine-discover', 'engine-session', 'engine-body-type', 'engine-robot-model']],
  ['管理层', ['engine-runtime-link', 'engine-runtime-install']],
  ['仿真', ['sim-start', 'sim-stop']],
];
const RP_OTHER = '其他';
const RP_START = '<article class="card', RP_BODY_OPEN = '<div class="card-body">';
const RP_STATUS = ['engine-status', 'sim-live'], RP_STATUS_LABEL = '实时状态';       // a robot's live page: over SSH, or its own service
const RP_OVERVIEW = 'engine-robot-overview', RP_OVERVIEW_LABEL = '概览';         // the same for a robot that has neither
const RP_NO_MODEL = '未填写品牌型号', RP_ATTENTION = '有需要处理的事';
const RP_HOSTS = {'ssh-status': 'sshStatus', 'sim-live': 'simLive', 'rp-status': 'rpStatus'};   // a live page in the dialog -> where the dialog says whose it is
const RP_SETTINGS_LABEL = '设置', RP_PAGE = 'page';         // the list of the page head; RP_PAGE is its key where robots have their id
const RP_TYPES_OPEN = 'engine-body-types-open';
const RP_REMOVE = ['engine-robot-remove', 'sim-remove'];
const RP_ALWAYS = ['robot-reset'];                        // releasing an emergency stop is never put away
const RP_BUTTON_CLASSES = ['button', 'secondary', 'small', 'danger-outline', 'text-link', 'body-type-change'];
const RP_BODY_TYPE_LABEL = '修改本体类型';
const RP_MENU_GAP = 8, RP_MENU_EDGE = 12;                 // issue #64: between a list and its button, and between a list and the edge of the window

// ---------------------------------------------------------------- reading a card's markup

const rpAttr = (tag, name) => (tag.match(new RegExp(`\\b${name}="([^"]*)"`)) || [])[1] ?? '';

// The buttons in a piece of markup, and what is left of it without them.
function rpButtons(html) {
  const buttons = [];
  const rest = html.replace(/<button\b[^>]*>[\s\S]*?<\/button>/g, whole => {
    const open = whole.slice(0, whole.indexOf('>') + 1);
    buttons.push({html: whole, action: rpAttr(open, 'data-action'), classes: rpAttr(open, 'class').split(/\s+/).filter(Boolean)});
    return '';
  });
  return {buttons, rest};
}

// The same button with other classes.
function rpWithClasses(item, classes) {
  const open = item.html.slice(0, item.html.indexOf('>') + 1), value = `class="${classes.join(' ')}"`;
  const fresh = /\bclass="[^"]*"/.test(open) ? open.replace(/\bclass="[^"]*"/, () => value) : open.replace('<button', () => '<button ' + value);
  return {...item, classes, html: fresh + item.html.slice(open.length)};
}

const rpAsEntry = item => rpWithClasses(item, ['robot-menu-item', ...item.classes.filter(c => !RP_BUTTON_CLASSES.includes(c)),
                                              ...(RP_REMOVE.includes(item.action) ? ['robot-menu-remove'] : [])]);
const rpAsLead = item => rpWithClasses(item, item.classes.filter(c => c !== 'secondary'));
const rpAsQuiet = item => rpWithClasses(item, item.classes.includes('secondary') ? item.classes
  : [...item.classes.filter(c => c !== 'small'), 'secondary', ...(item.classes.includes('small') ? ['small'] : [])]);

// ---------------------------------------------------------------- what stays in sight

// Of the buttons of a card's row: {always, lead, status}. The rest goes into the list.
// `status` is the robot's 「实时状态」: its own live page when it has one, otherwise the overview alone.
function rpPick(buttons, robot) {
  const always = buttons.filter(b => RP_ALWAYS.includes(b.action));
  const status = RP_STATUS.map(action => buttons.find(b => b.action === action)).find(Boolean) || rpOverviewButton(robot);
  const rest = buttons.filter(b => !RP_ALWAYS.includes(b.action) && b !== status);
  // The button its own feature drew filled is that feature's next step ("开始调研", "调研进展", "配置 SSH", "启动仿真").
  const filled = b => !b.classes.includes('secondary') && !b.classes.some(c => c.startsWith('danger')) && !RP_REMOVE.includes(b.action);
  const running = rest.find(b => b.action === 'engine-session' && filled(b));
  const skills = (robot?.sim || robot?.status === 'connected') ? rest.find(b => b.action === 'detail-robots') : null;
  const lead = running || (!robot?.sim && skills) || rest.find(filled) || skills || null;
  return {always, lead, status};
}

// 「实时状态」 of a robot that has no live page of its own: what its card used to show, in a dialog.
function rpOverviewButton(robot) {
  const html = `<button type="button" class="button secondary small" data-action="${RP_OVERVIEW}" data-id="${h(robot?.id || '')}">${RP_STATUS_LABEL}</button>`;
  return {html, action: RP_OVERVIEW, classes: ['button', 'secondary', 'small']};
}

// ---------------------------------------------------------------- a card

function rpMenu(robot, entries, open) {
  const group = (title, body, cls = '') => (body
    ? `<div class="robot-menu-group${cls}" role="group" aria-label="${title}"><span class="robot-menu-title" aria-hidden="true">${title}</span>${body}</div>` : '');
  const of = actions => entries.filter(e => actions.includes(e.action)).sort((a, b) => actions.indexOf(a.action) - actions.indexOf(b.action))
    .map(e => e.html).join('');
  const known = [...RP_GROUPS.flatMap(([, actions]) => actions), ...RP_REMOVE];
  const groups = RP_GROUPS.map(([title, actions]) => group(title, of(actions))).join('');
  const other = group(RP_OTHER, entries.filter(e => !known.includes(e.action)).map(e => e.html).join(''));
  const remove = of(RP_REMOVE) ? `<div class="robot-menu-group robot-menu-danger">${of(RP_REMOVE)}</div>` : '';
  return `<details class="robot-menu" data-rp-menu="${h(robot.id)}"${open ? ' open' : ''}>` +
    `<summary class="button secondary small" aria-label="${h(robot.name || '')} 的${RP_MENU_LABEL}">${RP_MENU_LABEL}</summary>` +
    `<div class="robot-menu-panel">${groups}${other}${remove}</div></details>`;
}

// What a robot looks like (web/robot_catalog.js).
function rpPicture(robot) {
  const picture = typeof rcPicture === 'function' ? rcPicture(robot) : null;
  return picture ? `<div class="rp-picture${picture.exact ? '' : ' rp-picture-type'}"><img src="${h(picture.src)}" alt="${h(picture.alt)}" width="360" height="360" loading="lazy" decoding="async"></div>` : '';
}

function rpModelLine(robot) {
  const p = robot?.product, known = p && p.vendor && p.model;
  return known ? `<p class="rp-model"><span class="rp-brand">${h(p.vendor)}</span><span class="rp-sep" aria-hidden="true"> · </span><span class="rp-model-name">${h(p.model)}</span></p>`
    : `<p class="rp-model rp-model-missing">${RP_NO_MODEL}</p>`;
}

// Whether a robot without a heartbeat line is there: a simulated one says so itself, another one is as its control channel is.
function rpStateBadge(robot, simState) {
  if (simState) return simState;
  const on = robot?.status === 'connected';
  return `<span class="status ${on ? 'good' : ''}"><i class="dot"></i>${on ? '在线' : '离线'}</span>`;
}

// The parts of a finished card, or null for a card of a shape this file does not know:
//   always, lead, status   the buttons that stay in sight
//   entries                the entries of 「更多功能」
//   line                   the heartbeat line of a robot reached over SSH ('' when it has none)
//   state                  online or not, for a robot without that line
//   details                everything else the features put on the card: shown in 「实时状态」
//   attention              whether something in the details is a proven problem
function rpParts(html, robot) {
  const at = html.lastIndexOf(RP_BOTTOM);
  if (!html.startsWith(RP_START) || at < 0 || !html.endsWith(RP_END)) return null;
  const classes = html.slice(RP_START.length, html.indexOf('"', RP_START.length));        // what else the card is (after "card")
  let body = html.slice(RP_START.length, at);
  if (!body.endsWith('</div>')) return null;
  body = body.slice(body.indexOf('>') + 1, -'</div>'.length);          // inside the card's body
  if (!body.startsWith(RP_BODY_OPEN)) return null;
  body = body.slice(RP_BODY_OPEN.length);
  const row = rpButtons(html.slice(at + RP_BOTTOM.length, -RP_END.length));
  const pick = rpPick(row.buttons, robot), kept = [...pick.always, pick.lead, pick.status].filter(Boolean);
  const entries = row.buttons.filter(b => !kept.includes(b)).map(rpAsEntry);

  // Changing the body type: from the line of text into the list.
  body = body.replace(/\s?<button\b[^>]*data-action="engine-body-type"[^>]*>[\s\S]*?<\/button>/, whole => {
    const item = rpButtons(whole).buttons[0];
    entries.push(rpAsEntry({...item, html: item.html.replace(/>[^<]*<\/button>$/, () => `>${RP_BODY_TYPE_LABEL}</button>`)
      .replace(/\baria-label="[^"]*"/, () => `aria-label="${RP_BODY_TYPE_LABEL}：${h(robot?.name || '')}"`)}));
    return '';
  });
  // The corner of the card: the picture of the body type goes (the card has a picture), its badges are states.
  let corner = '', simState = '';
  body = body.replace(/<div class="card-top">[\s\S]*?<span class="button-row">([\s\S]*?)<\/span><\/div>/, (whole, badges) => { corner = badges; return ''; });
  body = body.replace(/<div class="card-top"><\/div>/, '');
  if (robot?.sim) { const [kind, state] = rpSplitKind(corner); if (state) { corner = kind; simState = state; } }
  body = body.replace(/<h3>[\s\S]*?<\/h3>/, '');
  let line = '';
  body = body.replace(/<div class="hb-line"[\s\S]*?<\/div>/, whole => { line = whole; return ''; });
  // What only explains (issue #53: no descriptions): the second line and the notes of a simulated robot.
  if (robot?.sim) {
    body = body.replace(/<p>([\s\S]*?)<\/p>/, (whole, inner) => `<p>${inner.split('<br>')[0]}</p>`).replace(/<p class="inline-note">[\s\S]*?<\/p>/g, '');
  }
  // What the robot is, and where: one line.
  body = body.replace(/<p>([\s\S]*?)<\/p>/, (whole, inner) => {
    const cut = inner.indexOf('<br>');
    if (cut < 0) return `<p class="rp-what">${inner}</p>`;
    return `<p class="rp-what">${inner.slice(0, cut)}<span class="rp-sep" aria-hidden="true"> · </span><span class="rp-where">${inner.slice(cut + 4)}</span></p>`;
  });
  // The badges of the corner join the other states.
  if (corner) body = body.includes(RP_TAGS) ? body.replace(RP_TAGS, () => RP_TAGS + corner) : body + RP_TAGS + corner + '</div>';
  const attention = /class="status[^"]*\bbad\b/.test(body) || /class="[^"]*\bbody-type-flag\b/.test(body);      // a proven problem (issue #47), not a hint
  return {...pick, row, entries, classes, line, state: line ? '' : rpStateBadge(robot, simState), details: body, attention};
}

// The status button: quiet, with a dot when something asks for attention.
function rpStatusButton(item, attention) {
  const quiet = rpAsQuiet(item);
  const mark = attention ? `<i class="rp-attention" role="img" aria-label="${RP_ATTENTION}"></i>` : '';
  return rpWithClasses(quiet, [...quiet.classes, 'rp-status-button']).html.replace(/>([^<]*)<\/button>$/, (whole, text) => `>${text}${mark}</button>`);
}

// The finished card of a robot, laid out again: its picture, its name, whether it is there, its brand and
// model, and the everyday buttons. Everything else the features put on it is kept, out of sight, in
// `.rp-details` (the scripts that paint their parts in place still find them) and is shown in 「实时状态」.
// A card of a shape this file does not know is returned as it is.
function rpCard(html, robot, open = false) {
  const parts = rpParts(html, robot);
  if (!parts) return html;
  const visible = [...parts.always.map(b => b.html), parts.lead ? rpAsLead(parts.lead).html : '', rpStatusButton(parts.status, parts.attention)].join('');
  return `<article class="card rp-card${parts.classes}" data-rp-robot="${h(robot?.id || '')}">${rpPicture(robot)}` +
    `<div class="card-body"><div class="rp-title"><h3>${h(robot?.name || '')}</h3>${parts.state}${parts.line}</div>${rpModelLine(robot)}` +
    `<div class="rp-details" hidden>${parts.details}</div></div>` +
    RP_BOTTOM + visible + parts.row.rest + (parts.entries.length ? rpMenu(robot, parts.entries, open) : '') + RP_END;
}

// What 「实时状态」 shows of a robot besides its live figures: whether it is there, and everything its card keeps out of sight.
function rpOverview(html, robot) {
  const parts = rpParts(html, robot);
  return parts ? `<section class="rp-overview" data-rp-overview="${h(robot?.id || '')}" aria-label="${RP_OVERVIEW_LABEL}">${parts.line}${parts.details}</section>` : '';
}

// The badges in the corner of a simulated robot's card: [what it is (仿真), how it is (运行中)].
function rpSplitKind(badges) {
  const parts = badges.match(/<span class="status[^"]*">[\s\S]*?<\/span>/g) || [];
  if (parts.join('') !== badges) return [badges, ''];                         // something else is in the corner: leave it
  const kind = parts.filter(part => /^<span class="status[^"]*\bsim\b/.test(part));
  return kind.length ? [kind.join(''), parts.filter(part => !kind.includes(part)).join('')] : [badges, ''];
}

// ---------------------------------------------------------------- the page

// Under the title: how many robots, real and simulated apart.
function rpSummary(list) {
  const sims = list.filter(r => r.sim).length, real = list.length - sims;
  return [real ? `${real} 台真机` : '', sims ? `${sims} 台仿真` : ''].filter(Boolean).join(' · ');
}

let rpOpen = null;                      // whose list is open (a robot's id, or RP_PAGE); kept when the page is drawn again
let rpChosenFrom = null;                // whose list an entry was last chosen from: where the focus goes back to
let rpTypesBack = false;                // a form was opened from the list of body types: closing it leads back to the list

// 「设置」 in the page head: what is set for all robots, each in its own dialog.
function rpSettings() {
  const models = rows('agent_models').length;
  const types = typeof bodyTypesPanel === 'function' && bodyTypesPanel() ? bodyTypeList().length : 0;
  const entry = (label, count, action) => `<button type="button" class="robot-menu-item" data-action="${action}">${label}` +
    `<span class="robot-menu-count">${count}</span></button>`;
  const entries = entry('助手模型', models ? `${models} 个` : '未配置', 'engine-models') + (types ? entry('本体类型清单', `${types} 类`, RP_TYPES_OPEN) : '');
  return `<details class="robot-menu rp-settings" data-rp-menu="${RP_PAGE}"${rpOpen === RP_PAGE ? ' open' : ''}>` +
    `<summary class="button secondary" aria-label="我的机器人页面的${RP_SETTINGS_LABEL}">${RP_SETTINGS_LABEL}</summary>` +
    `<div class="robot-menu-panel"><div class="robot-menu-group" role="group" aria-label="${RP_SETTINGS_LABEL}">${entries}</div></div></details>`;
}

// The head of the page: the title as every page has it (issue #57, web/page_titles.js), under it one line of
// state, and on the right what can be done to the page.
const rpHead = (line, buttons) => `<div class="page-head rp-head"><div>${pageTitleMarkup('robots')}${line ? `<p class="subtitle rp-count">${h(line)}</p>` : ''}</div>` +
  `<div class="button-row">${buttons}${rpSettings()}</div></div>`;

function rpPage() {
  const list = rows('robots');
  // The list of body types is open in its dialog: it follows what the page now knows.
  if (typeof document !== 'undefined' && document.querySelector('#dialog[open] .rp-types')) setTimeout(rpTypesRepaint, 0);
  if (!state.user) rpOpen = null;
  if (rpOpen !== RP_PAGE && !list.some(r => r.id === rpOpen)) rpOpen = null;
  if (rpOpen && rpOpen !== RP_PAGE && typeof document !== 'undefined') { queueMicrotask(rpPlace); setTimeout(rpPlace, 0); }      // drawn again: placed before it is painted
  // No robot yet: the three ways to add one are the page.
  if (!list.length) return rpHead('', '') + rpWays();
  return rpHead(rpSummary(list), button(icon('plus') + ' 添加机器人', 'engine-robots-add')) +
    `<div class="cards rp-cards">${list.map(r => rpCard(engineRobotCard(r), r, rpOpen === r.id)).join('')}</div>`;
}

// The list of body types, which was a fold of the page: a dialog of its own.
const rpTypesInner = () => (typeof bodyTypesPanel === 'function' ? bodyTypesPanel() : '')
  .replace(/^<details\b[^>]*><summary>[^<]*<\/summary>/, '').replace(/<\/details>$/, '');
const rpTypesCount = () => `${bodyTypeList().length} 类`;

function rpTypesOpen() {
  const inner = rpTypesInner();
  if (!inner) return;
  dialog('本体类型清单', rpTypesCount(), `<div class="dialog-body body-types rp-types">${inner}</div>`);
  document.getElementById('dialog').classList.add('wide');
}

// The open list, written again from the state (a type was added or removed, or a device changed its type).
function rpTypesRepaint() {
  const body = document.querySelector('#dialog[open] .rp-types'), inner = rpTypesInner();
  if (!body || !inner || body.innerHTML === inner) return;
  body.innerHTML = inner;
  const line = document.querySelector('#dialog .dialog-head p');
  if (line) line.textContent = rpTypesCount();
}

// The three ways to add a robot, each a title and its button: what explained them is gone (issue #53).
function rpWays() {
  return engineRobotOptions().replace(/<small class="entry-eyebrow">[\s\S]*?<\/small>/g, '').replace(/(<\/h3>)<p>[\s\S]*?<\/p>/g, '$1');
}

// ---------------------------------------------------------------- 「实时状态」: what the card keeps out of sight

// The dialog shows a robot's live page: {host, robotId}, or null.
function rpStatusShown() {
  const dialogEl = document.getElementById('dialog');
  if (!dialogEl?.open) return null;
  for (const [id, key] of Object.entries(RP_HOSTS)) {
    const host = dialogEl.querySelector('#' + id);
    if (host && dialogEl.dataset[key]) return {host, robotId: dialogEl.dataset[key]};
  }
  return null;
}

let rpOverviewLast = '';                // the overview as it was last written from the state

// Puts the overview of the robot above its live page, and keeps it up to date. What other scripts paint
// in place inside it stays as painted until the state itself says something else.
function rpOverviewSync() {
  const shown = rpStatusShown(), dialogEl = document.getElementById('dialog');
  const old = dialogEl?.querySelector('.rp-overview');
  const robot = shown && rows('robots').find(r => r.id === shown.robotId);
  let html = robot ? rpOverview(engineRobotCard(robot), robot) : '';
  // A robot with a live page over SSH has its overview there, not on the page of its skills.
  if (html && shown.host.id === 'sim-live' && rpParts(engineRobotCard(robot), robot)?.status.action !== 'sim-live') html = '';
  if (!html) { old?.remove(); rpOverviewLast = ''; return; }
  if (old && old.dataset.rpOverview === robot.id && html === rpOverviewLast) return;
  rpOverviewLast = html;
  if (old) old.outerHTML = html;
  else shown.host.insertAdjacentHTML('beforebegin', html);
  if (typeof hbPaint === 'function') hbPaint();
}

// 「实时状态」 of a robot without a live page of its own.
function rpStatusOpen(robotId) {
  const robot = rows('robots').find(r => r.id === robotId);
  if (!robot) return toast('这台机器人已经不在了');
  dialog(RP_STATUS_LABEL, h(robot.name), '<div class="dialog-body rp-status" id="rp-status"></div>');
  document.getElementById('dialog').dataset.rpStatus = robotId;
  rpOverviewSync();
}

// 「添加机器人」 on a page that already has robots: the three ways, in a dialog.
function rpAddOpen() {
  dialog('添加机器人', '', `<div class="dialog-body rp-add">${rpWays()}</div>`);
}

// ---------------------------------------------------------------- the list, on the page

// Issue #64 — where the list of a card is put. It used to hang under its card whatever its length: a real
// robot's list (15 entries) left the window, and the page scrolled to show what it could of it.
//   button   the rectangle of 「更多功能」, in the window
//   size     {width, height} of the whole list
//   area     the part of the window the page has (not the sidebar, not the top bar)
// -> {side, left, top, maxHeight}. Under the button while the list fits there. Otherwise beside the button,
// as near to its height as the window allows, which is about the middle of the window; on a narrow window
// above it, or over the page. Always inside `area`: a list higher than the window scrolls inside itself.
function rpPlaceAt(button, size, area) {
  const edge = RP_MENU_EDGE, gap = RP_MENU_GAP, clamp = (value, low, high) => Math.max(low, Math.min(value, high));
  const maxHeight = Math.max(0, Math.floor(area.bottom - area.top - 2 * edge)), height = Math.min(size.height, maxHeight);
  const minLeft = area.left + edge, maxLeft = area.right - edge - size.width, minTop = area.top + edge, maxTop = area.bottom - edge - height;
  const under = clamp(button.right - size.width, minLeft, maxLeft);                       // its right edge on the button's
  const level = clamp((button.top + button.bottom) / 2 - height / 2, minTop, maxTop);     // its middle at the button's
  const at = (side, left, top) => ({side, left: Math.round(left), top: Math.round(top), maxHeight});
  if (button.bottom + gap + height <= area.bottom - edge) return at('below', under, button.bottom + gap);
  if (button.right + gap <= maxLeft) return at('right', button.right + gap, level);
  if (button.left - gap - size.width >= minLeft) return at('left', button.left - gap - size.width, level);
  if (button.top - gap - height >= minTop) return at('above', under, button.top - gap - height);
  return at('over', under, level);
}

// Puts the open list of a card there. The place is written on the root of the document, not on the list:
// the page is drawn again every few seconds, and the new list is where the old one was from its first frame.
function rpPlace(scrolled = false) {
  if (typeof document === 'undefined') return;
  const menu = document.querySelector('.rp-card details.robot-menu[open]'), panel = menu?.querySelector('.robot-menu-panel');
  const scroller = document.getElementById('content-scroll');
  if (!panel || !scroller) return;
  const button = menu.querySelector('summary').getBoundingClientRect(), box = scroller.getBoundingClientRect();
  const area = {left: box.left, top: box.top, right: box.left + scroller.clientWidth, bottom: Math.min(box.bottom, window.innerHeight)};
  // The person scrolled its button out of sight: the list closes. Out of sight for another reason (the page above it
  // grew, a script opened it): the list stays, inside the page area.
  if (scrolled && (button.bottom <= area.top || button.top >= area.bottom)) return rpCloseMenus();
  const size = {width: panel.offsetWidth, height: panel.scrollHeight + panel.offsetHeight - panel.clientHeight};
  const at = rpPlaceAt(button, size, area), root = document.documentElement.style;
  root.setProperty('--rp-menu-left', at.left + 'px');
  root.setProperty('--rp-menu-top', at.top + 'px');
  root.setProperty('--rp-menu-max', at.maxHeight + 'px');
  if (panel.scrollTop !== rpMenuScroll) panel.scrollTop = rpMenuScroll;          // a list drawn again is where the person had scrolled it to
}

let rpMenuScroll = 0;                   // how far the open list of a card is scrolled inside itself
let rpPlaceQueued = false;
// The page was scrolled or the window resized while a card's list is open: the list follows its button.
function rpPlaceSoon(e) {
  const own = e?.target?.closest?.('.rp-card .robot-menu-panel');
  if (own) rpMenuScroll = own.scrollTop;
  if (!rpOpen || rpOpen === RP_PAGE || rpPlaceQueued || own) return;      // not for the list's own scrolling
  rpPlaceQueued = true;
  requestAnimationFrame(() => { rpPlaceQueued = false; rpPlace(e?.type === 'scroll'); });
}

function rpCloseMenus(focus = false) {
  if (typeof document === 'undefined') return;
  rpOpen = null;
  for (const el of document.querySelectorAll('details.robot-menu[open]')) {
    el.open = false;
    if (focus) el.querySelector('summary')?.focus({preventScroll: true});
  }
}

function rpInstall() {
  const baseAction = engineAction;
  engineAction = async (type, id) => (type === 'engine-robots-add' ? rpAddOpen() : type === RP_TYPES_OPEN ? rpTypesOpen()
    : type === RP_OVERVIEW ? rpStatusOpen(id) : baseAction(type, id));
  views.robots = rpPage;
  // A live page was opened (whoever opened it says whose it is right after): the overview goes above it.
  const baseDialog = dialog, baseRefresh = engineRefresh;
  dialog = (...args) => { const out = baseDialog(...args); queueMicrotask(rpOverviewSync); return out; };
  engineRefresh = () => { baseRefresh(); rpOverviewSync(); };
  document.addEventListener('click', e => {
    const summary = e.target.closest?.('details.robot-menu > summary');
    if (summary) {
      // The browser opens or closes it after this; what is remembered is set here, at once, because
      // the `toggle` event comes later and can arrive after the page was drawn again.
      const menu = summary.parentNode, opening = !menu.open;
      for (const other of document.querySelectorAll('details.robot-menu[open]')) if (other !== menu) other.open = false;
      rpOpen = opening ? menu.dataset.rpMenu : null;
      // A card's list is opened here and put in its place before it is first painted (issue #64); the page does not
      // scroll for it, unless its button is out of sight (opened by a script, not by a person): the button is shown first.
      if (opening && menu.closest('.rp-card')) { e.preventDefault(); menu.open = true; rpMenuScroll = 0; summary.scrollIntoView({block: 'nearest', behavior: 'instant'}); rpPlace(); }
      else if (opening) setTimeout(() => menu.querySelector('.robot-menu-panel')?.scrollIntoView({block: 'nearest', behavior: 'smooth'}), 0);
      return;
    }
    // A form reached from the list of body types (add, remove): when it closes, the list is back.
    // (The form may have replaced the list by now: the control is found by its list, not by the dialog.)
    if (e.target.closest?.('.rp-types [data-action]:not([data-action="close"])')) rpTypesBack = true;
    const inPanel = e.target.closest?.('.robot-menu-panel');
    if (inPanel && e.target.closest('[data-action]')) rpChosenFrom = inPanel.parentNode.dataset.rpMenu;
    if (!inPanel || e.target.closest('[data-action]')) rpCloseMenus();       // a click elsewhere, or an entry was chosen
  });
  // The keyboard left the list (Tab past its last entry): it closes instead of covering what is focused next.
  document.addEventListener('focusout', e => {
    const menu = e.target.closest?.('details.robot-menu[open]'), to = e.relatedTarget;
    // Not when the focus goes to something the list is inside of: WebKit (the desktop app) does not focus a
    // button on a mouse press but the page's scroll area around it, and closing then would lose the click.
    if (menu && to && !menu.contains(to) && !to.contains(menu)) rpCloseMenus();
  });
  // The dialog of an entry closed: the entry it would give the focus back to is put away, so its list's button gets it.
  document.getElementById('dialog')?.addEventListener('close', () => {
    const from = rpChosenFrom, back = rpTypesBack;
    rpTypesBack = false;
    if (back) return setTimeout(() => { if (!document.querySelector('dialog[open]') && current === 'robots') rpTypesOpen(); }, 0);
    rpChosenFrom = null;
    setTimeout(() => {
      const menu = from && [...document.querySelectorAll('details.robot-menu')].find(el => el.dataset.rpMenu === from);
      const active = document.activeElement;
      // Only when the focus is nowhere in particular: on the page itself, or (WebKit) on the area around the card.
      if (!menu || document.querySelector('dialog[open]') || (active && active !== document.body && !active.contains(menu))) return;
      menu.querySelector('summary')?.focus({preventScroll: true});
    }, 0);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && rpOpen && !document.querySelector('dialog[open]')) rpCloseMenus(true);
    // Tab past the last entry, or back past the list's button: the keyboard leaves the list. Closed here,
    // not on focusout: when nothing on the page follows the list the focus leaves the page, and no
    // focusout names where it went.
    if (e.key === 'Tab' && rpOpen) {
      const menu = document.querySelector('details.robot-menu[open]'), at = document.activeElement;
      const stops = menu ? [...menu.querySelectorAll('summary, button:not([disabled]), a[href]')] : [];
      if (stops.length && at === (e.shiftKey ? stops[0] : stops[stops.length - 1])) rpCloseMenus();
    }
  });
  document.addEventListener('scroll', rpPlaceSoon, {capture: true, passive: true});
  window.addEventListener('resize', rpPlaceSoon);
  if (current === 'robots') render();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', rpInstall);
  else rpInstall();
}
