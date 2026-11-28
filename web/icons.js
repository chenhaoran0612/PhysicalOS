'use strict';
// Issue #72 — 整体 Icon 优化. One table of icons for the whole console, drawn one way:
// a 24 × 24 grid, the drawing inside the middle 20, stroke 1.75, round ends and corners, radius 2, no fill.
//
//   * NX_ICON_PATHS is the table. Its entries replace and extend `icons` of web/app.js, so icon(k) there
//     draws them and every page of the sidebar has a picture of its own (seven pages used to borrow the
//     one of 机器人应用).
//   * Symbols typed as text (← ↗ ✓ × ☰ …) are drawn from the same table: nxDrawGlyphs() runs after every
//     change of the page, the way web/dropdown.js takes over <select>. The character stays in the page,
//     hidden from the eye only, so a reader, a copy and innerText get what they got before.
//   * The mark (N whose last stroke leaves as an arrow) is in web/icons.css; desktop/icon.swift draws
//     the same shape for the installer.
//
// Loaded after web/app.js. The rules are in docs/ICONS.md; tests/icons_logic.cjs checks the table.

const NX_ROUND = n => Math.round(n * 100) / 100;

// A rounded rectangle and a circle as path data: icon() of web/app.js draws one <path> per icon.
function nxRect(x, y, w, h, r) {
  const a = (dx, dy) => `a${r} ${r} 0 0 1 ${dx} ${dy}`;
  return `M${NX_ROUND(x + r)} ${y}h${NX_ROUND(w - 2 * r)}${a(r, r)}v${NX_ROUND(h - 2 * r)}${a(-r, r)}` +
    `h${NX_ROUND(2 * r - w)}${a(-r, -r)}v${NX_ROUND(2 * r - h)}${a(r, -r)}z`;
}

function nxCircle(cx, cy, r) {
  return `M${NX_ROUND(cx - r)} ${cy}a${r} ${r} 0 1 0 ${NX_ROUND(2 * r)} 0a${r} ${r} 0 1 0 ${NX_ROUND(-2 * r)} 0z`;
}

const NX_CLOUD_OPEN = 'M7.5 17.5H7a4.5 4.5 0 0 1-.5-8.97A6 6 0 0 1 18 10a3.8 3.8 0 0 1-.6 7.5h-.9';

const NX_ICON_PATHS = Object.freeze({
  // Pages of the sidebar.
  overview: nxRect(3.5, 3.5, 7, 9, 2) + nxRect(13.5, 3.5, 7, 5, 2) + nxRect(13.5, 11.5, 7, 9, 2) + nxRect(3.5, 15.5, 7, 5, 2),
  robots: nxRect(4.5, 8, 15, 11.5, 3) + 'M12 8V5' + nxCircle(12, 3.75, 1.25) + 'M9.25 12.75v1.5M14.75 12.75v1.5M2.25 12.5v3M21.75 12.5v3',
  skills: nxRect(3.5, 3.5, 7, 7, 2) + nxRect(3.5, 13.5, 7, 7, 2) + nxRect(13.5, 13.5, 7, 7, 2) + 'M17 3.5v7M13.5 7h7',
  simulation: 'M12 2.75l8 4.5v9.5l-8 4.5-8-4.5v-9.5zM4.2 7.4L12 12l7.8-4.6M12 12v9',
  harness: 'M12 2.75l7.5 2.75v5.75c0 4.6-3 8.1-7.5 10-4.5-1.9-7.5-5.4-7.5-10V5.5zM8.75 11.75L11 14l4.25-4.5',
  deployment: 'M12 15V3.5M7.5 8L12 3.5 16.5 8M4 14v4.5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V14',
  models: 'M12 3.25l8.5 4.5-8.5 4.5-8.5-4.5zM3.5 12l8.5 4.5 8.5-4.5M3.5 16.25l8.5 4.5 8.5-4.5',
  training: 'M3.5 3.5v15a2 2 0 0 0 2 2h15M7.5 15.5l3.75-4.25 3 2.5 5.25-6.25M15.75 7.5h3.75v3.75',
  datasets: 'M4 6c0-1.66 3.58-3 8-3s8 1.34 8 3-3.58 3-8 3-8-1.34-8-3zM4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3',
  resources: nxRect(6, 6, 12, 12, 2) + nxRect(9.75, 9.75, 4.5, 4.5, 1) + 'M9.5 3v3M14.5 3v3M9.5 18v3M14.5 18v3M3 9.5h3M3 14.5h3M18 9.5h3M18 14.5h3',
  billing: 'M6 3.5h12v17l-3-1.75-3 1.75-3-1.75-3 1.75zM9.5 8.5h5M9.5 12.5h5',
  cloud: 'M7 19a4.5 4.5 0 0 1-.5-8.97A6 6 0 0 1 18 11.5a3.8 3.8 0 0 1-.75 7.5z',
  'cloud-skills': NX_CLOUD_OPEN + 'M12 21v-8M9 15.75l3-3 3 3',
  'cloud-skill-load': NX_CLOUD_OPEN + 'M12 13v8M9 18.25l3 3 3-3',
  library: 'M12 6.5c-2-1.5-4.5-2-8-2v13c3.5 0 6 .5 8 2 2-1.5 4.5-2 8-2v-13c-3.5 0-6 .5-8 2zM12 6.5v13',
  agents: 'M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4 20l1.4-6A7.5 7.5 0 1 1 20 11.5zM12.5 8.25l.9 2.35 2.35.9-2.35.9-.9 2.35-.9-2.35-2.35-.9 2.35-.9z',
  workflows: nxCircle(6, 6, 2.25) + nxCircle(18, 18, 2.25) + nxCircle(6, 18, 2.25) + 'M6 8.25v7.5M8.25 6H14a4 4 0 0 1 4 4v5.75',
  extensions: 'M9 2.75v5M15 2.75v5M6.5 7.75h11v3.75a5.5 5.5 0 0 1-11 0zM12 17v4.25',
  services: nxRect(3.5, 3.75, 17, 6.75, 2) + nxRect(3.5, 13.5, 17, 6.75, 2) + 'M7.25 7.1h.01M7.25 16.9h.01M11 7.1h5.5M11 16.9h5.5',
  // Actions.
  plus: 'M12 5v14M5 12h14',
  arrow: 'M4.5 12h15M13.5 6l6 6-6 6',
  back: 'M19.5 12h-15M10.5 6l-6 6 6 6',
  open: 'M7 17L17 7M8.5 7H17v8.5',
  up: 'M12 19.5v-15M6 10.5l6-6 6 6',
  down: 'M12 4.5v15M6 13.5l6 6 6-6',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  close: 'M6 6l12 12M18 6L6 18',
  menu: 'M4 7h16M4 12h16M4 17h16',
  search: nxCircle(11, 11, 6.5) + 'M16 16l4.5 4.5',
  edit: 'M4 20l1-4.5L16.5 4a2.1 2.1 0 0 1 3 3L8 18.5zM14.5 6l3.5 3.5',
  repair: 'M16.7 4.2a4.5 4.5 0 0 0-5.9 5.6L4.6 16a2.1 2.1 0 0 0 3 3l6.2-6.2a4.5 4.5 0 0 0 6-5.5l-2.6 2.6-3.1-3.1z',
  refresh: 'M19.5 12a7.5 7.5 0 1 1-2.2-5.3M18 3v4.25h-4.25',
  stop: nxRect(6, 6, 12, 12, 2.5),
  chevron: 'M6 9.5l6 6 6-6',
  eye: 'M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12z' + nxCircle(12, 12, 3),
  more: nxCircle(5.5, 12, 0.4) + nxCircle(12, 12, 0.4) + nxCircle(18.5, 12, 0.4),
  info: nxCircle(12, 12, 8.5) + 'M12 11v5.5M12 7.75h.01',
  user: nxCircle(12, 8, 3.5) + 'M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6',
  // Body types. The first five are the built-in types; the others are the families older pages ask for.
  manipulator: 'M4.5 20.5h11M10 20.5v-3.5' + nxCircle(10, 15, 2) + 'M11.2 13.4l2.9-4.1' + nxCircle(15.25, 7.7, 2) + 'M17.1 6.9l2.4-1M21.5 3.5l-2 2.4 1.6 2.6',
  wheel_legged: nxRect(5, 5.5, 14, 5.5, 2.5) + 'M8.5 11l-2.25 3 1.6 2.4M15.5 11l-2.25 3 1.6 2.4' + nxCircle(8.5, 18.5, 2.25) + nxCircle(15.5, 18.5, 2.25),
  quadruped: nxRect(4, 8, 13.5, 5.5, 2.5) + 'M17.5 9.5l2.75-1.5a.9.9 0 0 1 1.25.8v2.2a1 1 0 0 1-1 1h-3M6.5 13.5L5 17l1 3.5M9.5 13.5l1 3.5-1 3.5M13 13.5L11.5 17l1 3.5M16 13.5l1 3.5-1 3.5',
  biped: nxCircle(12, 4.75, 2.25) + nxRect(8.5, 8.5, 7, 7.5, 2) + 'M8.5 10.5L5.5 15M15.5 10.5l3 4.5M10.5 16v5.25M13.5 16v5.25',
  other: nxRect(3.5, 3.5, 7.5, 7.5, 2) + nxCircle(17, 7.25, 3.75) + 'M7.25 13.5l3.9 6.75h-7.8zM17 13.75v6.5M13.75 17h6.5',
  humanoid: nxCircle(12, 4.75, 2.25) + nxRect(8.5, 8.5, 7, 7.5, 2) + 'M8.5 10.5L5.5 15M15.5 10.5l3 4.5M10.5 16v5.25M13.5 16v5.25',
  mobile: nxRect(3.5, 7.5, 17, 8, 2.5) + nxCircle(8, 18.25, 2.25) + nxCircle(16, 18.25, 2.25) + 'M9 7.5V5h6v2.5',
  aerial: nxRect(9, 9, 6, 6, 1.5) + 'M9.4 9.4L6 6M14.6 9.4L18 6M9.4 14.6L6 18M14.6 14.6L18 18M3 6h6M15 6h6M3 18h6M15 18h6',
});

// A symbol typed as text, and the icon that says the same.
const NX_GLYPHS = Object.freeze({
  '←': 'back', '↗': 'open', '↑': 'up', '↓': 'down', '✓': 'check', '×': 'close', '✕': 'close', '✗': 'close',
  '☰': 'menu', '⌕': 'search', '✎': 'edit', '⚒': 'repair', '⟳': 'refresh', '■': 'stop',
});
// Only these open or close a line of words ("← 返回", "开发指南 ↗"); the others count when they stand alone.
const NX_GLYPHS_LEADING = '←✓✗✕';
const NX_GLYPHS_TRAILING = '↗';

function nxIcon(name) {
  const d = NX_ICON_PATHS[name];
  return d ? `<svg class="nx-i" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>` : '';
}

// A piece of text cut into words and symbols: [{text}|{glyph, icon}], or null when it has no symbol to draw.
// A symbol in the middle of a sentence ("我的机器人 → 能力表", "2 × 3") is a word and stays one.
function nxGlyphParts(text) {
  const value = String(text == null ? '' : text);
  const alone = value.trim();
  if (Object.hasOwn(NX_GLYPHS, alone)) {
    const at = value.indexOf(alone);
    return [{text: value.slice(0, at)}, {glyph: alone, icon: NX_GLYPHS[alone]}, {text: value.slice(at + alone.length)}]
      .filter(part => part.glyph || part.text);
  }
  const parts = [];
  let rest = value;
  const head = /^(\s*)(\S)(\s+)(?=\S)/u.exec(rest);
  if (head && NX_GLYPHS_LEADING.includes(head[2])) {
    if (head[1]) parts.push({text: head[1]});
    parts.push({glyph: head[2], icon: NX_GLYPHS[head[2]]});
    rest = head[3] + rest.slice(head[0].length);
  }
  const tail = /(\S)(\s+)(\S)(\s*)$/u.exec(rest);
  if (tail && NX_GLYPHS_TRAILING.includes(tail[3])) {
    parts.push({text: rest.slice(0, tail.index + tail[1].length) + tail[2]}, {glyph: tail[3], icon: NX_GLYPHS[tail[3]]});
    if (tail[4]) parts.push({text: tail[4]});
    rest = '';
  }
  if (!parts.length) return null;
  if (rest) parts.push({text: rest});
  return parts;
}

// Where a symbol means something else than its usual icon.
function nxGlyphIcon(part, element) {
  if (part.glyph === '↗' && element) {
    if (element.id === 'user-avatar') return 'user';
    if (element.matches && element.matches('.notice > span')) return 'info';
  }
  return part.icon;
}

// Not drawn: code and fields, what is already drawn (.nx-line: its words are not looked at twice), and lines of a
// log, which are what a device or an assistant wrote (web/readable_dom.js folds them).
const NX_SKIP = 'script,style,textarea,select,option,input,pre,code,svg,.nx-glyph,.nx-line,.log-item,[contenteditable],[data-nx-raw]';

function nxGlyphNode(part, element) {
  const holder = document.createElement('span');
  holder.className = 'nx-glyph';
  holder.innerHTML = nxIcon(nxGlyphIcon(part, element));
  const kept = document.createElement('span');
  kept.className = 'nx-glyph-text';
  kept.textContent = part.glyph;
  holder.appendChild(kept);
  return holder;
}

// Draws the symbols of one text node. True when something was drawn.
function nxDrawText(node) {
  const element = node.parentElement;
  if (!element || element.closest(NX_SKIP) || !node.nodeValue.trim()) return false;
  const parts = nxGlyphParts(node.nodeValue);
  if (!parts) return false;
  if (parts.length === 1) {
    element.replaceChild(nxGlyphNode(parts[0], element), node);
    return true;
  }
  // Words and their symbol stay one piece of the line: in a flex row (most buttons and links) two
  // pieces would lose the space between them and read as two lines.
  const line = document.createElement('span');
  line.className = 'nx-line';
  for (const part of parts) line.appendChild(part.glyph ? nxGlyphNode(part, element) : document.createTextNode(part.text));
  element.replaceChild(line, node);
  return true;
}

// Draws every symbol in root (an element or one text node). Safe to run again: what is drawn is skipped.
function nxDrawGlyphs(root) {
  if (!root) return 0;
  if (root.nodeType === 3) return nxDrawText(root) ? 1 : 0;
  if (root.nodeType !== 1 || root.closest(NX_SKIP)) return 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), found = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) found.push(node);
  return found.filter(nxDrawText).length;
}

// The sidebar drawn before this file ran still shows the old pictures.
function nxRedrawNavigation() {
  if (typeof icon !== 'function') return;
  for (const link of document.querySelectorAll('#navigation a[href^="#"]')) {
    const holder = link.querySelector('.nav-icon');
    if (holder) holder.innerHTML = icon(link.getAttribute('href').slice(1));
  }
}

if (typeof icons === 'object' && icons) Object.assign(icons, NX_ICON_PATHS);

if (typeof document !== 'undefined' && typeof MutationObserver === 'function') {
  const watch = () => {
    nxRedrawNavigation();
    nxDrawGlyphs(document.body);
    new MutationObserver(changes => {
      // Only what was added or rewritten is looked at, not the page around it.
      const roots = new Set();
      for (const change of changes) {
        if (change.type === 'characterData') roots.add(change.target);
        else for (const node of change.addedNodes) roots.add(node);
      }
      for (const root of roots) if (root.isConnected) nxDrawGlyphs(root);
    }).observe(document.body, {childList: true, subtree: true, characterData: true});
  };
  if (document.body) watch(); else document.addEventListener('DOMContentLoaded', watch);
}
