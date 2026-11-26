'use strict';
// Issue #41 — "我的机器人", tidied.
//
// Over time every feature put its own buttons, lines and tags on a robot's card (a real robot had eleven
// buttons and six tags) and its own section on the page. This file lays the page out again:
//
//   * A card keeps in sight what the robot is, whether it is online, what asks for attention, the next
//     step and the live state. Everything else is one click away in the card's 「更多功能」 list, grouped.
//   * The page follows its state. Without a robot it shows the three ways to add one. With a robot it
//     shows the robots and one 「添加机器人」 button; the three ways are in that button's dialog.
//   * Nothing explains the page on the page. The two settings that are not a robot's (the assistant's
//     models, the list of body types) are in the 「设置」 list of the page head, each in its own dialog.
//   * Cards line up: every card is a name, a line of state, one line of what and where, then what asks
//     for attention. Notes that explain rather than report are in the card's list.
//
// The other scripts are not edited: each still builds its part of a card by wrapping engineRobotCard.
// rpCard() takes the finished card and moves the parts, keeping every element (and the attributes the
// in-place painters of heartbeat.js, runtime_install.js, runtime_autostart.js and connection_verify.js
// find them by). It is a pure function of the card's markup: tests/robots_page_logic.cjs.
// The page is taken over once every script has loaded (DOMContentLoaded), so the place of this file in
// index.html does not matter. A later feature that adds a button to the card needs nothing here: an
// action this file does not know lands in the list under 「其他」.

const RP_MENU_LABEL = '更多功能';
const RP_BOTTOM = '<div class="card-bottom"><div class="button-row">', RP_END = '</div></div></article>';
const RP_TAGS = '<div class="tags">';
// The list, in this order. An action that is in none of the groups goes to 「其他」.
const RP_GROUPS = [
  ['连接', ['engine-ssh', 'engine-ssh-verify', 'engine-ssh-edit', 'robot-connect', 'robot-config']],
  ['设备', ['engine-status', 'sim-live', 'detail-robots', 'engine-profile', 'engine-inventory', 'hardware-open', 'engine-drivers',
            'engine-discover', 'engine-session', 'engine-body-type']],
  ['管理层', ['engine-runtime-link', 'engine-runtime-install']],
  ['仿真', ['sim-start', 'sim-stop']],
];
const RP_OTHER = '其他', RP_FACTS = '其他状态', RP_NOTES = '说明';
const RP_SETTINGS_LABEL = '设置', RP_PAGE = 'page';         // the list of the page head; RP_PAGE is its key where robots have their id
const RP_EMPTY_LINE = '选择一种方式添加机器人';
const RP_TYPES_OPEN = 'engine-body-types-open';
const RP_REMOVE = ['engine-robot-remove', 'sim-remove'];
const RP_ALWAYS = ['robot-reset'];                        // releasing an emergency stop is never put away
const RP_SECOND = ['engine-status', 'sim-live', 'detail-robots', 'engine-profile'];
const RP_BUTTON_CLASSES = ['button', 'secondary', 'small', 'danger-outline', 'text-link', 'body-type-change'];
const RP_LOUD_TONES = ['bad', 'warn', 'blue'];            // a problem, a warning, work in progress
const RP_BODY_TYPE_LABEL = '修改本体类型';

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

// The tags of a card: [what stays in sight, what goes into the list]. A tag is a top-level <span>.
function rpSplitTags(inner) {
  const seen = [], quiet = [], token = /<span\b[^>]*>|<\/span>/g;
  let depth = 0, start = 0, end = 0, match;
  // Anything that is not a tag (none of today's scripts puts such a thing there) is kept, in the list.
  const between = upTo => { const other = inner.slice(end, upTo).trim(); if (other) quiet.push(other); };
  while ((match = token.exec(inner))) {
    if (match[0] !== '</span>') { if (!depth) { start = match.index; between(start); } depth += 1; continue; }
    depth -= 1;
    if (depth) continue;
    end = match.index + match[0].length;
    const tag = inner.slice(start, match.index + match[0].length), classes = rpAttr(tag.slice(0, tag.indexOf('>') + 1), 'class').split(/\s+/);
    const loud = classes.includes('body-type-flag') || (classes.includes('status') && RP_LOUD_TONES.some(tone => classes.includes(tone)));
    (loud ? seen : quiet).push(tag);
  }
  between(inner.length);
  return [seen.join(''), quiet.join('')];
}

// ---------------------------------------------------------------- what stays in sight

// Of the buttons of a card's row: {always, lead, second}. The rest goes into the list.
function rpPick(buttons, robot) {
  const always = buttons.filter(b => RP_ALWAYS.includes(b.action)), rest = buttons.filter(b => !RP_ALWAYS.includes(b.action));
  // The button its own feature drew filled is that feature's next step ("开始调研", "调研进展", "配置 SSH", "启动仿真").
  const filled = b => !b.classes.includes('secondary') && !b.classes.some(c => c.startsWith('danger')) && !RP_REMOVE.includes(b.action);
  const running = rest.find(b => b.action === 'engine-session' && filled(b));
  const skills = !robot?.sim && robot?.status === 'connected' ? rest.find(b => b.action === 'detail-robots') : null;
  const everyday = except => RP_SECOND.map(action => rest.find(b => b.action === action && b !== except)).find(Boolean) || null;
  const lead = running || skills || rest.find(filled) || everyday(null);
  const second = everyday(lead);
  return {always, lead, second};
}

// ---------------------------------------------------------------- a card

function rpMenu(robot, entries, managementLine, facts, open, notes = '') {
  const group = (title, body, cls = '') => (body
    ? `<div class="robot-menu-group${cls}" role="group" aria-label="${title}"><span class="robot-menu-title" aria-hidden="true">${title}</span>${body}</div>` : '');
  const of = actions => entries.filter(e => actions.includes(e.action)).sort((a, b) => actions.indexOf(a.action) - actions.indexOf(b.action))
    .map(e => e.html).join('');
  const known = [...RP_GROUPS.flatMap(([, actions]) => actions), ...RP_REMOVE];
  const groups = RP_GROUPS.map(([title, actions]) => group(title, of(actions) + (title === '管理层' ? managementLine : ''))).join('');
  const other = group(RP_OTHER, entries.filter(e => !known.includes(e.action)).map(e => e.html).join(''));
  const state = group(RP_FACTS, facts ? RP_TAGS + facts + '</div>' : '', ' robot-menu-facts');
  const remove = of(RP_REMOVE) ? `<div class="robot-menu-group robot-menu-danger">${of(RP_REMOVE)}</div>` : '';
  const about = group(RP_NOTES, notes, ' robot-menu-notes');
  return `<details class="robot-menu" data-rp-menu="${h(robot.id)}"${open ? ' open' : ''}>` +
    `<summary class="button secondary small" aria-label="${h(robot.name || '')} 的${RP_MENU_LABEL}">${RP_MENU_LABEL}</summary>` +
    `<div class="robot-menu-panel">${groups}${other}${state}${about}${remove}</div></details>`;
}

// The finished card of a robot, laid out again. A card of a shape this file does not know is returned as it is.
function rpCard(html, robot, open = false) {
  const at = html.lastIndexOf(RP_BOTTOM);
  if (!html.startsWith('<article class="card') || at < 0 || !html.endsWith(RP_END)) return html;
  let body = html.slice(0, at).replace('<article class="card', () => '<article class="card rp-card');
  const row = rpButtons(html.slice(at + RP_BOTTOM.length, -RP_END.length));
  const pick = rpPick(row.buttons, robot), kept = [...pick.always, pick.lead, pick.second].filter(Boolean);
  const entries = row.buttons.filter(b => !kept.includes(b)).map(rpAsEntry);
  const visible = [...pick.always.map(b => b.html), pick.lead ? rpAsLead(pick.lead).html : '', pick.second ? rpAsQuiet(pick.second).html : ''].join('');
  if (!entries.length) return body + RP_BOTTOM + visible + row.rest + RP_END;          // nothing to put away: the body stays as it is

  // Changing the body type: from the line of text into the list.
  body = body.replace(/\s?<button\b[^>]*data-action="engine-body-type"[^>]*>[\s\S]*?<\/button>/, whole => {
    const item = rpButtons(whole).buttons[0];
    entries.push(rpAsEntry({...item, html: item.html.replace(/>[^<]*<\/button>$/, () => `>${RP_BODY_TYPE_LABEL}</button>`)
      .replace(/\baria-label="[^"]*"/, () => `aria-label="${RP_BODY_TYPE_LABEL}：${h(robot?.name || '')}"`)}));
    return '';
  });
  // Start at boot: a state of the management layer, next to its entries.
  let managementLine = '';
  body = body.replace(/<div class="ra-line"[\s\S]*?<\/div>/, whole => { managementLine = whole; return ''; });
  // Tags: what asks for attention stays, plain facts go into the list.
  let facts = '';
  const tagsAt = body.indexOf(RP_TAGS), tagsEnd = tagsAt < 0 ? -1 : body.indexOf('</div>', tagsAt);
  if (tagsEnd >= 0) {
    const [seen, quiet] = rpSplitTags(body.slice(tagsAt + RP_TAGS.length, tagsEnd));
    facts = quiet;
    body = body.slice(0, tagsAt) + (seen ? RP_TAGS + seen + '</div>' : '') + body.slice(tagsEnd + '</div>'.length);
  }
  const lined = rpLines(body, robot);
  return lined.body + RP_BOTTOM + visible + row.rest + rpMenu(robot, entries, managementLine, facts, open, lined.notes) + RP_END;
}

// The lines of a card, so that cards line up: under the name one line of state, then one line of what
// the robot is and where. What explains rather than reports (a second line of a simulated robot, its
// note about evidence) goes to the list as notes. Returns the body and the notes; nothing is dropped.
function rpLines(body, robot) {
  let notes = '';
  const note = text => { if (text.trim()) notes += `<p class="robot-menu-note">${text.trim()}</p>`; };
  // What and where: one line. The first plain paragraph of the card is that line.
  let next = body.replace(/<p>([\s\S]*?)<\/p>/, (whole, inner) => {
    const at = inner.indexOf('<br>');
    if (at < 0) return `<p class="rp-what">${inner}</p>`;
    const first = inner.slice(0, at), rest = inner.slice(at + '<br>'.length);
    if (robot?.sim) { note(rest); return `<p class="rp-what">${first}</p>`; }
    return `<p class="rp-what">${first}<span class="rp-sep" aria-hidden="true"> · </span><span class="rp-where">${rest}</span></p>`;
  });
  if (!robot?.sim) return {body: next, notes};
  next = next.replace(/<p class="inline-note">([\s\S]*?)<\/p>/g, (whole, inner) => { note(inner); return ''; });
  // Whether it runs: from the corner, where a real robot has no state, to the line under the name, where it has.
  const top = /(<div class="card-top">[\s\S]*?<span class="button-row">)([\s\S]*?)(<\/span><\/div>)/.exec(next);
  if (top && next.includes('</h3>')) {
    const [kind, state] = rpSplitKind(top[2]);
    if (state) next = next.replace(top[0], () => top[1] + kind + top[3]).replace('</h3>', () => `</h3><div class="rp-state">${state}</div>`);
  }
  return {body: next, notes};
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

// The head of the page: one line of state, the title, and on the right what can be done to the page.
const rpHead = (line, buttons) => `<div class="page-head rp-head"><div><div class="eyebrow">${h(line)}</div><h1>我的机器人</h1></div>` +
  `<div class="button-row">${buttons}${rpSettings()}</div></div>`;

function rpPage() {
  const list = rows('robots');
  // The list of body types is open in its dialog: it follows what the page now knows.
  if (typeof document !== 'undefined' && document.querySelector('#dialog[open] .rp-types')) setTimeout(rpTypesRepaint, 0);
  if (!state.user) rpOpen = null;
  if (rpOpen !== RP_PAGE && !list.some(r => r.id === rpOpen)) rpOpen = null;
  // No robot yet: the three ways to add one are the page.
  if (!list.length) return rpHead(RP_EMPTY_LINE, '') + engineRobotOptions();
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

// 「添加机器人」 on a page that already has robots: the three ways, in a dialog.
function rpAddOpen() {
  dialog('添加机器人', '选择一种方式', `<div class="dialog-body rp-add">${engineRobotOptions()}</div>`);
}

// ---------------------------------------------------------------- the list, on the page

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
  engineAction = async (type, id) => (type === 'engine-robots-add' ? rpAddOpen() : type === RP_TYPES_OPEN ? rpTypesOpen() : baseAction(type, id));
  views.robots = rpPage;
  document.addEventListener('click', e => {
    const summary = e.target.closest?.('details.robot-menu > summary');
    if (summary) {
      // The browser opens or closes it after this; what is remembered is set here, at once, because
      // the `toggle` event comes later and can arrive after the page was drawn again.
      const menu = summary.parentNode, opening = !menu.open;
      for (const other of document.querySelectorAll('details.robot-menu[open]')) if (other !== menu) other.open = false;
      rpOpen = opening ? menu.dataset.rpMenu : null;
      if (opening) setTimeout(() => menu.querySelector('.robot-menu-panel')?.scrollIntoView({block: 'nearest', behavior: 'smooth'}), 0);
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
  if (current === 'robots') render();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', rpInstall);
  else rpInstall();
}
