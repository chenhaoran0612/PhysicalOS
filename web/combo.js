'use strict';
// Issue #61: one control that is a list to choose from and a box to type in.
//
// Before, a brand (and a model) was a pick-list with 「其他」 plus a second text input for what the list
// did not have. This file is the one control that replaces the pair:
//   * comboInput({...}) is the markup: a text input with a button that opens a list;
//   * the options come from a function of the page named on the input (data-combo-source="rcVendorChoices"):
//     it gets the input and returns [{value, keys?: [other spellings], note?: ''}];
//   * a click, the button or the arrow keys open the whole list; typing narrows it; a value that is not
//     in the list is simply kept. Choosing an option fires `input` and `change` on the input, so a form
//     that listens for those needs nothing else.
// One list element is shared by every such input of the page (it lives in the form of the input, so it is
// drawn above the dialog and goes when the form does), and it knows nothing about robots: web/robot_catalog.js supplies the brands and models.

const COMBO_LIST_ID = 'combo-list';
const COMBO_MAX_OPTIONS = 300;            // more than this is not a list a person reads
const COMBO_MAX_HEIGHT = 264;             // px: about eight options
const COMBO_MIN_ROOM = 150;               // px under the input below which the list opens upwards
const COMBO_GAP = 4, COMBO_EDGE = 8;      // px between input and list, and between list and window edge
const COMBO_IME_KEY = 229;                // keyCode while an input method is composing

// ---------------------------------------------------------------- pure
const comboEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const comboNorm = text => String(text ?? '').toLowerCase().replace(/[\s\-_.·]+/g, '');

// The options a typed text leaves: those whose name or another spelling contains it.
function comboFilter(options, text) {
  const wanted = comboNorm(text);
  return wanted ? options.filter(o => [o.value, ...(o.keys || [])].some(k => comboNorm(k).includes(wanted))) : options;
}

// The option a text is (by its name, or another spelling of it), or -1.
function comboIndexOf(options, text) {
  const wanted = comboNorm(text);
  if (!wanted) return -1;
  const named = options.findIndex(o => comboNorm(o.value) === wanted);
  return named >= 0 ? named : options.findIndex(o => (o.keys || []).some(k => comboNorm(k) === wanted));
}

// The option an arrow key reaches from `index` (-1: none yet), going round at the ends.
function comboStep(index, delta, length) {
  if (!length) return -1;
  if (index < 0) return delta > 0 ? 0 : length - 1;
  return (index + delta + length) % length;
}

function comboSource(input) {
  const name = input.dataset.comboSource;
  const made = Object.prototype.hasOwnProperty.call(globalThis, name) && typeof globalThis[name] === 'function' ? globalThis[name](input) : [];
  return Array.isArray(made) ? made : [];
}

const COMBO_CHEVRON = '<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function comboInput({name, source, value = '', label = '', placeholder = '', required = false, maxlength = 100, attrs = ''}) {
  return '<span class="combo" data-combo>' +
    `<input name="${comboEsc(name)}" value="${comboEsc(value)}" maxlength="${Number(maxlength)}" placeholder="${comboEsc(placeholder)}" ` +
    'autocomplete="off" spellcheck="false" role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false" ' +
    `aria-label="${comboEsc(label)}" data-combo-source="${comboEsc(source)}"${attrs ? ' ' + attrs : ''}${required ? ' required' : ''}>` +
    `<button type="button" class="combo-toggle" tabindex="-1" aria-label="展开${comboEsc(label)}列表" data-combo-toggle>${COMBO_CHEVRON}</button></span>`;
}

// The lines of the list. `active`: the option the keys are on; `chosen`: the option the box holds.
function comboOptionsHtml(options, active, chosen) {
  if (!options.length) return '<li class="combo-empty" role="presentation">没有可选的，直接输入</li>';
  return options.map((o, i) => `<li role="option" id="combo-option-${i}" data-combo-index="${i}" aria-selected="${i === active}" ` +
    `class="combo-option${i === active ? ' combo-active' : ''}${i === chosen ? ' combo-chosen' : ''}"><span>${comboEsc(o.value)}</span>` +
    `${o.note ? `<small>${comboEsc(o.note)}</small>` : ''}</li>`).join('');
}

// ---------------------------------------------------------------- the open list
let comboOpen = null;                     // {input, options, active} while a list is shown
let comboChoosing = false;                // our own `input` event is not typing

const comboInputOf = el => (el && el.matches && el.matches('[data-combo] > input') ? el : null);

function comboListEl(input) {
  // In the form of the input: above the dialog it is in, and gone with the form when the dialog is drawn again.
  const host = input.closest('form') || input.closest('dialog') || document.body;
  let list = document.getElementById(COMBO_LIST_ID);
  if (!list) {
    list = document.createElement('ul');
    list.id = COMBO_LIST_ID;
    list.className = 'combo-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
  }
  if (list.parentNode !== host) host.append(list);
  return list;
}

// Under the input, or above it when there is little room below. The list is `position: fixed`; where its
// own 0,0 is depends on the page (a transformed ancestor moves it), so that is measured, not assumed.
function comboPlace(input, list) {
  const box = input.getBoundingClientRect();
  const below = window.innerHeight - box.bottom - COMBO_GAP - COMBO_EDGE, above = box.top - COMBO_GAP - COMBO_EDGE;
  const up = below < COMBO_MIN_ROOM && above > below;
  list.style.maxHeight = Math.max(COMBO_MIN_ROOM / 2, Math.min(COMBO_MAX_HEIGHT, up ? above : below)) + 'px';
  list.style.width = box.width + 'px';
  list.style.left = '0px';
  list.style.top = '0px';
  const origin = list.getBoundingClientRect();
  list.style.left = box.left - origin.left + 'px';
  list.style.top = (up ? box.top - COMBO_GAP - origin.height : box.bottom + COMBO_GAP) - origin.top + 'px';
  list.classList.toggle('combo-up', up);
}

function comboDraw(list, chosen) {
  const {input, options, active} = comboOpen;
  list.innerHTML = comboOptionsHtml(options, active, chosen);
  if (active >= 0) input.setAttribute('aria-activedescendant', 'combo-option-' + active);
  else input.removeAttribute('aria-activedescendant');
  const shown = list.querySelector(active >= 0 ? '.combo-active' : '.combo-chosen');
  if (shown && shown.scrollIntoView) shown.scrollIntoView({block: 'nearest'});
}

// Shows the list of an input: all of it, or (while typing) what the typed text leaves.
function comboShow(input, narrowed) {
  if (input.disabled || input.readOnly) return;
  const all = comboSource(input), options = (narrowed ? comboFilter(all, input.value) : all).slice(0, COMBO_MAX_OPTIONS);
  if (narrowed && !options.length) { comboClose(); return; }      // a name of one's own: nothing to offer
  if (comboOpen && comboOpen.input !== input) comboClose();
  const chosen = comboIndexOf(options, input.value);
  comboOpen = {input, options, active: narrowed ? -1 : chosen};
  const list = comboListEl(input), wasHidden = list.hidden;
  list.hidden = false;
  list.setAttribute('aria-label', input.getAttribute('aria-label') || '');
  comboDraw(list, chosen);
  if (wasHidden) list.classList.remove('combo-in');               // measured where it will rest, then it comes in;
  comboPlace(input, list);                                        // a list that is narrowed while open does not come in again
  list.classList.add('combo-in');
  input.setAttribute('aria-expanded', 'true');
  input.setAttribute('aria-controls', COMBO_LIST_ID);
  input.closest('[data-combo]').classList.add('combo-open');
}

// The list of an input that is no longer in the page (its dialog was drawn again) is closed. WebKit does not
// tell an input that is removed that it lost the focus.
function comboStillThere() {
  if (comboOpen && !comboOpen.input.isConnected) comboClose();
  return Boolean(comboOpen);
}

function comboClose() {
  if (!comboOpen) return;
  const {input} = comboOpen;
  comboOpen = null;
  input.setAttribute('aria-expanded', 'false');
  input.removeAttribute('aria-activedescendant');
  input.closest('[data-combo]')?.classList.remove('combo-open');
  const list = document.getElementById(COMBO_LIST_ID);
  if (list) { list.hidden = true; list.innerHTML = ''; }
}

function comboMove(delta) {
  comboOpen = {...comboOpen, active: comboStep(comboOpen.active, delta, comboOpen.options.length)};
  comboDraw(document.getElementById(COMBO_LIST_ID), comboIndexOf(comboOpen.options, comboOpen.input.value));
}

function comboChoose(index) {
  const {input, options} = comboOpen, option = options[index];
  if (!option) return;
  comboClose();
  input.value = option.value;
  comboChoosing = true;
  try {
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  } finally {
    comboChoosing = false;
  }
  input.focus();
}

function comboKey(e) {
  const input = comboInputOf(e.target);
  if (!input || e.isComposing || e.keyCode === COMBO_IME_KEY) return;      // the key belongs to the input method
  const open = comboStillThere() && comboOpen.input === input;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!open) comboShow(input, false);
    if (comboOpen && (open || comboOpen.active < 0)) comboMove(e.key === 'ArrowDown' ? 1 : -1);
  } else if (e.key === 'Enter' && open && comboOpen.active >= 0) {
    e.preventDefault();                                                     // chooses; does not send the form
    comboChoose(comboOpen.active);
  } else if (e.key === 'Escape' && open) {
    e.preventDefault();                                                     // closes the list, not the dialog
    e.stopPropagation();
    comboClose();
  } else if (e.key === 'Tab') {
    comboClose();
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('input', e => {
    const input = comboInputOf(e.target);
    if (!input || comboChoosing || e.isComposing) return;         // letters an input method is still turning into a word are not the text yet
    comboStillThere();
    if (document.activeElement === input) comboShow(input, true);
  });
  document.addEventListener('keydown', comboKey);
  // A press on the list or the button must not take the focus out of the input.
  document.addEventListener('mousedown', e => {
    const target = e.target.closest ? e.target : e.target.parentElement;
    if (!target) return;
    comboStillThere();
    if (target.closest('#' + COMBO_LIST_ID) || target.closest('[data-combo-toggle]')) { e.preventDefault(); return; }
    const input = comboInputOf(target);
    if (!input) { comboClose(); return; }
    if (e.button === 0 && !(comboOpen && comboOpen.input === input)) comboShow(input, false);      // the main button only
  });
  document.addEventListener('click', e => {
    const target = e.target.closest ? e.target : e.target.parentElement;
    if (!target) return;
    const option = target.closest('#' + COMBO_LIST_ID + ' [data-combo-index]');
    if (option && comboOpen) { comboChoose(Number(option.dataset.comboIndex)); return; }
    const toggle = target.closest('[data-combo-toggle]');
    if (!toggle) return;
    const input = toggle.parentElement.querySelector('input');
    if (comboOpen && comboOpen.input === input) { comboClose(); return; }
    input.focus();
    comboShow(input, false);
  });
  document.addEventListener('focusout', e => {
    if (comboOpen && e.target === comboOpen.input && !(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('#' + COMBO_LIST_ID))) comboClose();
  });
  // The list does not follow a page that moves under it.
  document.addEventListener('scroll', e => { if (comboOpen && e.target.id !== COMBO_LIST_ID) comboClose(); }, true);
  // A window that changes size moves the input: the list goes with it.
  if (typeof window !== 'undefined') window.addEventListener('resize', () => { if (comboStillThere()) comboPlace(comboOpen.input, document.getElementById(COMBO_LIST_ID)); });
  document.addEventListener('close', comboClose, true);                     // its dialog was closed
}
