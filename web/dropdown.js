'use strict';
// Issue #7: every <select> in the console gets a dropdown drawn in the console's own style.
// The native <select> stays in the form, only visually hidden: FormData, `required`,
// existing change/input listeners and Playwright's selectOption keep working, and the
// trigger mirrors whatever the select holds. Add `data-native` to a select to opt out.
(() => {
  const LIST_GAP = 6;
  const LIST_MAX_HEIGHT = 300;
  const LIST_MIN_HEIGHT = 120;
  const VIEWPORT_EDGE = 8;
  const PAGE_STEP = 8;
  const TYPEAHEAD_RESET_MS = 600;
  const MIRRORED = ['disabled', 'hidden', 'multiple', 'required', 'class'];
  const CHEVRON = '<svg class="nx-select-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>';
  const CHECK = '<svg class="nx-option-check" viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"/></svg>';
  const controllers = new WeakMap();
  let openController = null;
  let sequence = 0;

  const optionText = option => (option.label || option.textContent || '').trim();
  const isPlaceholder = option => !option || option.value === '';
  const nextId = prefix => `nx-${prefix}-${++sequence}`;

  function labelFor(select) {
    if (select.getAttribute('aria-label')) return select.getAttribute('aria-label');
    const label = select.labels && select.labels[0];
    if (!label) return '';
    return [...label.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' ').trim();
  }

  function summary(select) {
    const chosen = [...select.selectedOptions];
    if (select.multiple) {
      if (!chosen.length) return { text: '请选择（可多选）', placeholder: true };
      return { text: chosen.map(optionText).join('、'), placeholder: false };
    }
    const option = chosen[0];
    if (!option) return { text: select.options.length ? '请选择' : '暂无可选项', placeholder: true };
    return { text: optionText(option) || '请选择', placeholder: isPlaceholder(option) };
  }

  class Dropdown {
    constructor(select) {
      this.select = select;
      this.list = null;
      this.active = -1;
      this.offset = { dx: 0, dy: 0 };
      this.typed = '';
      this.typedAt = 0;
      this.trigger = this.buildTrigger();
      select.classList.add('nx-native');
      select.setAttribute('tabindex', '-1');
      select.setAttribute('aria-hidden', 'true');
      select.after(this.trigger);
      this.interceptValueWrites();
      this.bindNative();
      this.sync();
    }

    buildTrigger() {
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'nx-select';
      trigger.setAttribute('role', 'combobox');
      trigger.setAttribute('aria-haspopup', 'listbox');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.innerHTML = `<span class="nx-select-value"></span>${CHEVRON}`;
      trigger.addEventListener('click', () => (this.list ? this.close(true) : this.open()));
      trigger.addEventListener('keydown', e => this.onKey(e));
      trigger.addEventListener('focus', () => this.sync());
      trigger.addEventListener('blur', () => setTimeout(() => {
        if (this.list && document.activeElement !== this.trigger) this.close(false);
      }, 0));
      return trigger;
    }

    // Code that sets select.value / selectedIndex fires no event; mirror those writes too.
    interceptValueWrites() {
      const proto = HTMLSelectElement.prototype;
      for (const key of ['value', 'selectedIndex']) {
        const native = Object.getOwnPropertyDescriptor(proto, key);
        const self = this;
        Object.defineProperty(this.select, key, {
          configurable: true,
          get() { return native.get.call(this); },
          set(v) { native.set.call(this, v); self.sync(); },
        });
      }
    }

    bindNative() {
      const { select } = this;
      select.addEventListener('change', () => { this.sync(); this.clearInvalid(); });
      select.addEventListener('input', () => this.sync());
      // A click on the surrounding <label> focuses the hidden select; hand focus to the trigger.
      select.addEventListener('focus', () => this.trigger.focus());
      select.addEventListener('invalid', e => this.onInvalid(e));
      select.form?.addEventListener('reset', () => setTimeout(() => this.sync(), 0));
      new MutationObserver(() => { this.sync(); if (this.list) this.renderOptions(); })
        .observe(select, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: [...MIRRORED, 'selected', 'label'] });
    }

    sync() {
      const { select, trigger } = this;
      const { text, placeholder } = summary(select);
      trigger.querySelector('.nx-select-value').textContent = text;
      trigger.title = placeholder ? '' : text;
      trigger.className = ['nx-select', ...[...select.classList].filter(c => c !== 'nx-native')].join(' ');
      trigger.classList.toggle('is-placeholder', placeholder);
      trigger.classList.toggle('is-multiple', select.multiple);
      trigger.classList.toggle('is-invalid', this.invalid === true);
      trigger.classList.toggle('is-open', Boolean(this.list));
      trigger.disabled = select.disabled;
      trigger.hidden = select.hidden;
      const name = labelFor(select);
      if (name) trigger.setAttribute('aria-label', name); else trigger.removeAttribute('aria-label');
      if (select.disabled && this.list) this.close(false);
    }

    onInvalid(event) {
      event.preventDefault();
      this.invalid = true;
      this.sync();
      let note = this.trigger.nextElementSibling;
      if (!note || !note.classList.contains('nx-select-error')) {
        note = document.createElement('small');
        note.className = 'nx-select-error';
        note.setAttribute('role', 'alert');
        this.trigger.after(note);
      }
      note.textContent = this.select.multiple ? '请至少选择一项' : '请选择一项';
      // Browsers focus the first invalid control after all `invalid` events; follow that order.
      setTimeout(() => {
        const first = this.select.form?.querySelector(':invalid');
        if (first === this.select) this.trigger.focus();
      }, 0);
    }

    clearInvalid() {
      if (!this.invalid || !this.select.checkValidity()) return;
      this.invalid = false;
      const note = this.trigger.nextElementSibling;
      if (note && note.classList.contains('nx-select-error')) note.remove();
      this.sync();
    }

    enabledIndexes() {
      return [...this.select.options].map((o, i) => (o.disabled || o.parentElement?.disabled ? -1 : i)).filter(i => i >= 0);
    }

    open(edge) {
      if (this.select.disabled || this.list) return;
      if (openController) openController.close(false);
      openController = this;
      // Safari/WKWebView does not focus a <button> on click: keys (and Escape) must reach the trigger.
      if (document.activeElement !== this.trigger) this.trigger.focus({ preventScroll: true });
      this.sync(); // option.selected set by code fires nothing; pick it up before showing the list
      const host = this.trigger.closest('dialog[open]') || document.body;
      const list = document.createElement('div');
      list.className = 'nx-select-list';
      list.id = nextId('list');
      list.setAttribute('role', 'listbox');
      if (this.select.multiple) list.setAttribute('aria-multiselectable', 'true');
      if (this.trigger.getAttribute('aria-label')) list.setAttribute('aria-label', this.trigger.getAttribute('aria-label'));
      list.addEventListener('pointerdown', e => e.preventDefault());
      list.addEventListener('mousedown', e => e.preventDefault());
      list.addEventListener('click', e => this.onListClick(e));
      list.addEventListener('mousemove', e => this.onListHover(e));
      this.list = list;
      this.renderOptions();
      host.append(list);
      const indexes = this.enabledIndexes();
      const selected = this.select.selectedIndex;
      if (edge === 'first') this.active = indexes[0] ?? -1;
      else if (edge === 'last') this.active = indexes[indexes.length - 1] ?? -1;
      else this.active = indexes.includes(selected) ? selected : (indexes[0] ?? -1);
      this.trigger.setAttribute('aria-expanded', 'true');
      this.trigger.setAttribute('aria-controls', list.id);
      this.place(true);
      this.highlight(true);
      this.sync();
      requestAnimationFrame(() => list.classList.add('is-shown'));
    }

    close(refocus) {
      if (!this.list) return;
      this.list.remove();
      this.list = null;
      if (openController === this) openController = null;
      this.trigger.setAttribute('aria-expanded', 'false');
      this.trigger.removeAttribute('aria-controls');
      this.trigger.removeAttribute('aria-activedescendant');
      this.sync();
      if (refocus && this.trigger.isConnected) this.trigger.focus();
    }

    renderOptions() {
      const { list, select } = this;
      list.textContent = '';
      if (!select.options.length) {
        list.innerHTML = '<div class="nx-select-empty">暂无可选项</div>';
        return;
      }
      let group = null;
      [...select.options].forEach((option, index) => {
        const parent = option.parentElement;
        if (parent && parent.tagName === 'OPTGROUP' && parent !== group) {
          group = parent;
          const title = document.createElement('div');
          title.className = 'nx-group';
          title.setAttribute('role', 'presentation');
          title.textContent = parent.label;
          list.append(title);
        }
        list.append(this.renderOption(option, index));
      });
      const indexes = this.enabledIndexes();
      if (!indexes.includes(this.active)) this.active = indexes.includes(select.selectedIndex) ? select.selectedIndex : (indexes[0] ?? -1);
      this.highlight(false);
    }

    renderOption(option, index) {
      const row = document.createElement('div');
      row.className = 'nx-option';
      row.id = `${this.list.id}-${index}`;
      row.dataset.index = String(index);
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.selected));
      if (option.disabled || option.parentElement?.disabled) row.setAttribute('aria-disabled', 'true');
      if (isPlaceholder(option)) row.classList.add('is-placeholder');
      row.innerHTML = `<span class="nx-option-label"></span>${CHECK}`;
      row.firstElementChild.textContent = optionText(option) || '—';
      return row;
    }

    place(measure) {
      const { list, trigger } = this;
      if (!list) return;
      const r = trigger.getBoundingClientRect();
      if (r.bottom < 0 || r.top > innerHeight) { this.close(false); return; }
      list.style.minWidth = `${r.width}px`;
      list.style.maxWidth = `${Math.max(r.width, Math.min(520, innerWidth - 2 * VIEWPORT_EDGE))}px`;
      const below = innerHeight - r.bottom - VIEWPORT_EDGE - LIST_GAP;
      const above = r.top - VIEWPORT_EDGE - LIST_GAP;
      const natural = Math.min(list.scrollHeight, LIST_MAX_HEIGHT);
      const upward = natural > below && above > below;
      list.style.maxHeight = `${Math.max(LIST_MIN_HEIGHT, Math.min(LIST_MAX_HEIGHT, upward ? above : below))}px`;
      list.classList.toggle('is-up', upward);
      const left = Math.max(VIEWPORT_EDGE, Math.min(r.left, innerWidth - VIEWPORT_EDGE - list.offsetWidth));
      const top = upward ? r.top - LIST_GAP - list.offsetHeight : r.bottom + LIST_GAP;
      if (measure) {
        // position:fixed is relative to a transformed ancestor, if any; measure that offset once.
        list.style.left = `${left}px`;
        list.style.top = `${top}px`;
        const got = list.getBoundingClientRect();
        this.offset = { dx: got.left - left, dy: got.top - top };
      }
      list.style.left = `${left - this.offset.dx}px`;
      list.style.top = `${top - this.offset.dy}px`;
    }

    highlight(scroll) {
      const { list } = this;
      if (!list) return;
      list.querySelectorAll('.nx-option.is-active').forEach(row => row.classList.remove('is-active'));
      const row = this.active >= 0 ? list.querySelector(`[data-index="${this.active}"]`) : null;
      if (!row) { this.trigger.removeAttribute('aria-activedescendant'); return; }
      row.classList.add('is-active');
      this.trigger.setAttribute('aria-activedescendant', row.id);
      if (scroll) row.scrollIntoView({ block: 'nearest' });
    }

    move(step) {
      const indexes = this.enabledIndexes();
      if (!indexes.length) return;
      const at = indexes.indexOf(this.active);
      const next = at < 0 ? 0 : Math.max(0, Math.min(indexes.length - 1, at + step));
      this.active = indexes[next];
      this.highlight(true);
    }

    choose(index) {
      const { select } = this;
      const option = select.options[index];
      if (!option || option.disabled || option.parentElement?.disabled) return;
      if (select.multiple) {
        option.selected = !option.selected;
        this.list?.querySelector(`[data-index="${index}"]`)?.setAttribute('aria-selected', String(option.selected));
        this.emit();
        return;
      }
      const changed = select.selectedIndex !== index;
      select.selectedIndex = index;
      this.close(true);
      if (changed) this.emit();
    }

    emit() {
      this.select.dispatchEvent(new Event('input', { bubbles: true }));
      this.select.dispatchEvent(new Event('change', { bubbles: true }));
      this.sync();
    }

    onListClick(event) {
      const row = event.target.closest('.nx-option');
      if (row) this.choose(Number(row.dataset.index));
    }

    onListHover(event) {
      const row = event.target.closest('.nx-option:not([aria-disabled="true"])');
      if (!row || Number(row.dataset.index) === this.active) return;
      this.active = Number(row.dataset.index);
      this.highlight(false);
    }

    typeahead(key) {
      const now = Date.now();
      this.typed = now - this.typedAt > TYPEAHEAD_RESET_MS ? key : this.typed + key;
      this.typedAt = now;
      const options = [...this.select.options];
      const indexes = this.enabledIndexes();
      const start = indexes.indexOf(this.active);
      const ordered = [...indexes.slice(start + 1), ...indexes.slice(0, start + 1)];
      const needle = this.typed.toLowerCase();
      const hit = ordered.find(i => optionText(options[i]).toLowerCase().startsWith(needle));
      if (hit === undefined) return;
      this.active = hit;
      this.highlight(true);
    }

    onKey(event) {
      const { key } = event;
      if (event.metaKey || event.ctrlKey) return;
      if (!this.list) {
        const opens = { ArrowDown: null, ArrowUp: null, Enter: null, ' ': null, Home: 'first', End: 'last' };
        if (key in opens) { event.preventDefault(); this.open(opens[key]); return; }
        if (key.length === 1 && !event.altKey) { this.open(); this.typeahead(key); }
        return;
      }
      const steps = { ArrowDown: 1, ArrowUp: -1, PageDown: PAGE_STEP, PageUp: -PAGE_STEP, Home: -Infinity, End: Infinity };
      if (key in steps) { event.preventDefault(); this.move(steps[key]); return; }
      const typing = Date.now() - this.typedAt < TYPEAHEAD_RESET_MS;
      if (key === 'Enter' || (key === ' ' && !typing)) { event.preventDefault(); this.choose(this.active); return; }
      if (key === 'Escape') { event.preventDefault(); event.stopPropagation(); swallowEscape(); this.close(true); return; }
      if (key === 'Tab') { this.close(false); return; }
      if (key.length === 1 && !event.altKey) { event.preventDefault(); this.typeahead(key); }
    }
  }

  // Escape that closes a list must not also close the surrounding <dialog>.
  let escapeAt = 0;
  function swallowEscape() { escapeAt = Date.now(); }
  document.addEventListener('cancel', e => {
    if (e.target instanceof HTMLDialogElement && Date.now() - escapeAt < 300) e.preventDefault();
  }, true);

  function enhance(select) {
    if (controllers.has(select) || select.hasAttribute('data-native') || select.size > 1) return;
    controllers.set(select, new Dropdown(select));
  }

  function enhanceWithin(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === 'SELECT') enhance(node);
    else node.querySelectorAll?.('select').forEach(enhance);
  }

  function dropOrphans() {
    if (openController && !openController.select.isConnected) {
      openController.list?.remove();
      openController.list = null;
      openController = null;
    }
  }

  new MutationObserver(records => {
    for (const record of records) record.addedNodes.forEach(enhanceWithin);
    dropOrphans();
  }).observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener('pointerdown', e => {
    if (!openController) return;
    const { list, trigger } = openController;
    if (!list.contains(e.target) && !trigger.contains(e.target)) openController.close(false);
  }, true);

  let framed = false;
  const reposition = () => {
    if (!openController || framed) return;
    framed = true;
    requestAnimationFrame(() => { framed = false; openController?.place(false); });
  };
  document.addEventListener('scroll', e => {
    if (openController && !openController.list.contains(e.target)) reposition();
  }, true);
  addEventListener('resize', reposition);

  const start = () => enhanceWithin(document.body);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.nxDropdown = { enhance, controllers };
})();
