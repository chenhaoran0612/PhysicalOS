// Issue #41, second part: the dialogs behind a robot's buttons, in one frame.
//
// Every feature script names its dialog its own way (the robot as the title and the function under
// it, the function and the robot in the title, the robot under the title). This layer does not edit
// those scripts: it stands in front of dialog() and, when the dialog is about a robot, writes one
// head — what the dialog does as the title, the robot under it — and adds a row that leads to the
// robot's other dialogs, so that one moves between them without closing. The frame itself (the head
// and the actions stay in sight while the content scrolls) is web/robot_dialogs.css.
//
// The pure functions (rdHead, rdSub, rdNames, rdNavItems, rdNav) are checked in
// tests/robot_dialogs_logic.cjs, the dialogs themselves in tests/robot_dialogs_browser.cjs.

const RD_SEPARATOR = ' · ';
// A dialog is named as the button that opens it.
const RD_RENAME = {'真机状态': '实时状态'};
// The dialogs one moves between, in the order of the row. Looking comes first, then the connection,
// then the management layer. Nothing that changes or deletes the robot at one click is here.
const RD_NAV = [
  ['engine-status', '实时状态'],
  ['engine-profile', '设备档案'],
  ['hardware-open', '硬件'],
  ['engine-ssh-edit', '连接'],
  ['engine-runtime-link', '管理层'],
  ['engine-runtime-install', '管理层'],
];
const RD_LAYER = ['engine-runtime-link', 'engine-runtime-install'];
const RD_NAV_LABEL = '这台机器人的其他功能';

// The names of the robots as the dialogs write them (escaped): the longest first, each once.
function rdNames(robots) {
  const names = new Set((robots || []).map(robot => h(robot?.name || '')).filter(Boolean));
  return [...names].sort((a, b) => b.length - a.length || (a < b ? -1 : 1));
}

function rdSplit(text) {
  const at = text.indexOf(RD_SEPARATOR);
  return at < 0 ? [text, ''] : [text.slice(0, at), text.slice(at + RD_SEPARATOR.length)];
}

function rdHeadFor(title, sub, name) {
  if (title === name) {
    const [fn, rest] = rdSplit(sub);
    return fn ? {fn, name, rest} : null;
  }
  if (title.endsWith(RD_SEPARATOR + name)) {
    const fn = title.slice(0, -(RD_SEPARATOR + name).length);
    return fn ? {fn, name, rest: sub} : null;
  }
  if (sub === name) return {fn: title, name, rest: ''};
  if (sub.startsWith(name + RD_SEPARATOR)) return {fn: title, name, rest: sub.slice((name + RD_SEPARATOR).length)};
  return null;
}

// What a dialog does, which robot it is about and what else its head said; null when the dialog is
// not about one of the robots. `title` and `sub` are the arguments of dialog(), `names` is rdNames().
function rdHead(title, sub, names) {
  const fits = (names || []).filter(Boolean).map(name => rdHeadFor(String(title ?? ''), String(sub ?? ''), name)).filter(Boolean);
  if (!fits.length) return null;
  const head = fits.reduce((longest, fit) => (fit.name.length > longest.name.length ? fit : longest));
  return {...head, fn: RD_RENAME[head.fn] || head.fn};
}

// The line under the title: the robot, then the rest. Both are markup already (escaped by the caller of dialog()).
const rdSub = (name, rest) => `<b class="rd-robot">${name}</b>` + (rest ? `<span class="rd-rest">${rest}</span>` : '');

// The entries of the row for a robot whose card has these actions.
function rdNavItems(actions) {
  const has = new Set(actions || []);
  const layer = RD_LAYER.find(action => has.has(action));
  return RD_NAV.filter(([action]) => has.has(action) && (!RD_LAYER.includes(action) || action === layer));
}

// The row, with the open dialog marked. Empty when the open dialog is not one of the row.
function rdNav(items, current, id) {
  if (!id || !items || items.length < 2) return '';
  const here = RD_LAYER.includes(current) ? (items.find(([action]) => RD_LAYER.includes(action)) || [])[0] : current;
  if (!items.some(([action]) => action === here)) return '';
  const entry = ([action, label]) => `<button type="button" class="rd-tab" data-action="${action}" data-id="${h(id)}"` +
    `${action === here ? ' aria-current="true"' : ''}>${label}</button>`;
  return `<nav class="rd-nav" aria-label="${RD_NAV_LABEL}">${items.map(entry).join('')}</nav>`;
}

// ---- in the page

// The robot and the dialog the person asked for last: {id, action}. Forgotten when the dialog closes,
// so a dialog that opens by itself later is not taken for the one that was asked for.
let rdAsked = null;
// The row was just clicked: the dialog that replaces the open one puts the keyboard back on the row.
let rdFromRow = false;

function rdRemember(event) {
  const control = event.target.closest?.('[data-action][data-id]');
  if (!control) return;
  const robot = (state.robots || []).find(item => item.id === control.dataset.id);
  if (!robot) return;
  const action = control.dataset.action, inRow = RD_NAV.some(([known]) => known === action);
  // A button inside the dialog (scan again, refresh) keeps the dialog it belongs to.
  if (control.closest('#dialog') && !inRow && rdAsked?.id === robot.id) return;
  rdAsked = {id: robot.id, action};
  rdFromRow = Boolean(control.closest('.rd-nav'));
}

function rdCardActions(id) {
  const controls = document.querySelectorAll(`#main article.card [data-action][data-id="${CSS.escape(id)}"]`);
  return [...new Set([...controls].map(control => control.dataset.action))];
}

function rdPaintNav(head) {
  const robot = rdAsked && (state.robots || []).find(item => item.id === rdAsked.id);
  if (!robot || h(robot.name || '') !== head.name) return;
  const nav = rdNav(rdNavItems(rdCardActions(robot.id)), rdAsked.action, robot.id);
  const place = document.querySelector('#dialog-content .dialog-head > div');
  if (!nav || !place || place.querySelector('.rd-nav')) return;
  place.insertAdjacentHTML('beforeend', nav);
  // The row was clicked and is drawn again: the keyboard stays on it. A dialog that redraws itself
  // (a timer, a rescan) does not move the keyboard.
  if (rdFromRow && !document.querySelector('#dialog').contains(document.activeElement)) place.querySelector('[aria-current]')?.focus();
  rdFromRow = false;
}

// A script that writes the head of an open dialog itself (the SSH form does, after form() drew it):
// the same head for it. Only a head of plain text is rewritten, so no markup of a script is lost.
function rdHeadInPlace() {
  const title = document.querySelector('#dialog-content .dialog-head h2'), line = document.querySelector('#dialog-content .dialog-head p');
  if (!title || !line || title.children.length || line.children.length) return;
  const head = rdHead(h(title.textContent), h(line.textContent), rdNames(state.robots));
  if (!head) return;
  title.innerHTML = head.fn;
  line.innerHTML = rdSub(head.name, head.rest);
  rdPaintNav(head);
}

function rdInstall() {
  const plain = dialog;
  dialog = function rdDialog(title, sub, body) {
    let head = null;
    try { head = rdHead(title, sub, rdNames(state.robots)); } catch (error) { console.warn('robot dialogs: head', error); }
    if (!head) return plain.call(this, title, sub, body);
    const out = plain.call(this, head.fn, rdSub(head.name, head.rest), body);
    try { rdPaintNav(head); } catch (error) { console.warn('robot dialogs: row', error); }
    return out;
  };
  document.addEventListener('click', rdRemember, true);
  const frame = document.querySelector('#dialog');
  const forget = () => { if (!frame.open) { rdAsked = null; rdFromRow = false; } };
  frame?.addEventListener('close', forget);
  // The close event comes a moment after the dialog closed; its own close buttons find it closed already.
  document.addEventListener('click', event => { if (event.target.closest?.('#dialog [data-action="close"]')) forget(); });
  const content = document.querySelector('#dialog-content');
  if (content) new MutationObserver(() => {
    try { rdHeadInPlace(); } catch (error) { console.warn('robot dialogs: head in place', error); }
  }).observe(content, {childList: true, characterData: true, subtree: true});
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', rdInstall);
