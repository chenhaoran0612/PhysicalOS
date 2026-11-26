'use strict';
// Issue #49（功能简化）: 「工程助手」「助手工具」「自有服务」 are hidden for now. Nothing is deleted: the
// pages' code, the server's routes and the stored records stay as they are, and taking this script out
// of web/index.html brings the pages back. See docs/HIDDEN_PAGES.md.
//
// What "hidden" means here:
//   * the pages are not in the sidebar or the account menu, and their addresses open 任务工作台;
//   * a link on another page that only leads to one of them is taken out, and so is a button whose
//     result could only be found again on one of them;
//   * what other pages do with the same engine (调研, 硬件修复, 本体适配, 让助手准备) is untouched.
//
// It acts once every script has run (DOMContentLoaded), wherever its line is. The acceptance scripts of the hidden pages keep them in sight by setting
// window.__showHiddenPages before the page loads (tests/hidden_pages_helper.cjs).

// 协作流程 (#workflows) is a page inside 工程助手 and is hidden with it.
const HIDDEN_PAGES = Object.freeze(['agents', 'workflows', 'extensions', 'services']);
// 「提出工程需求」 outside 工程助手: the work it starts is listed only on that page.
const HIDDEN_ACTIONS = Object.freeze(['engine-work']);
// A step of a task's page that opened a hidden page opens this one instead (准备能力 → 机器人应用).
const HIDDEN_STEP_TARGETS = Object.freeze({agents: 'skills'});
const HIDDEN_FALLBACK = 'overview';
const HP_LINKS = '[data-action], a[href^="#"]';
const HP_ROOTS = Object.freeze(['#navigation', '#main', '#dialog-content', '#user-menu']);

const hpIsHidden = route => HIDDEN_PAGES.includes(String(route || '').replace(/^#/, ''));

// The page a control leads to: a "go-<page>" action or an in-page address. '' when it leads to none.
function hpRouteOf(link) {
  const action = String(link.action || ''), href = String(link.href || '');
  if (action.startsWith('go-')) return action.slice(3);
  return href.startsWith('#') ? href.slice(1) : '';
}

// What happens to one control. `link` says what it is: {action, href, inStep, aloneInCard}.
//   keep | remove (the control) | remove-card (the card it is the only control of) | retarget (to `action`)
function hpDecide(link) {
  if (HIDDEN_ACTIONS.includes(String(link.action || ''))) return {kind: 'remove'};
  const route = hpRouteOf(link);
  if (!hpIsHidden(route)) return {kind: 'keep'};
  const target = HIDDEN_STEP_TARGETS[route];
  if (link.inStep && target) return {kind: 'retarget', action: 'go-' + target};
  return {kind: link.aloneInCard ? 'remove-card' : 'remove'};
}

function hpDescribe(el) {
  const card = el.closest('.journey-option');
  return {
    action: el.dataset.action || '', href: el.getAttribute('href') || '',
    inStep: el.classList.contains('journey-step'),
    aloneInCard: Boolean(card) && card.querySelectorAll(HP_LINKS).length === 1,
  };
}

function hpApply(el) {
  const decision = hpDecide(hpDescribe(el));
  if (decision.kind === 'retarget') el.dataset.action = decision.action;
  else if (decision.kind === 'remove-card') el.closest('.journey-option').remove();
  else if (decision.kind === 'remove') el.remove();
}

function hpSweep(root) {
  if (root) root.querySelectorAll(HP_LINKS).forEach(hpApply);
}

// An address of a hidden page shows 任务工作台 (route() of web/app.js falls back to it once the view is
// gone); the address bar is made to say so too.
function hpFixAddress() {
  if (!hpIsHidden(location.hash)) return false;
  history.replaceState(null, '', location.pathname + location.search + '#' + HIDDEN_FALLBACK);
  return true;
}

function hpInstall() {
  if (window.__showHiddenPages === true) return;
  HIDDEN_PAGES.forEach(page => { delete views[page]; });
  window.addEventListener('hashchange', hpFixAddress);
  HP_ROOTS.map(selector => document.querySelector(selector)).filter(Boolean).forEach(root => {
    hpSweep(root);
    // Pages and dialogs are drawn again, or changed in place, at any time: clear each time.
    new MutationObserver(() => {
      try { hpSweep(root); } catch (error) { console.warn('hidden pages: sweep', error); }
    }).observe(root, {childList: true, subtree: true});
  });
  if (hpFixAddress() && typeof route === 'function') route();
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', hpInstall);
