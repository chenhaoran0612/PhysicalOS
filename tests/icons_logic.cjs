'use strict';
// Issue #72 — 整体 Icon 优化: one table of icons (web/icons.js). Pure-logic checks.
//   node tests/icons_logic.cjs
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.join(__dirname, '..', 'web');
const source = name => fs.readFileSync(path.join(WEB, name), 'utf8');
let checks = 0;
function ck(ok, message) {
  if (!ok) throw new Error(message);
  checks += 1;
}

// app.js up to its first view is the table of the sidebar and icon(); the pages that add themselves to
// the sidebar later are named in their own files.
const head = source('app.js').split('\n').slice(0, 6).join('\n');
const sandbox = vm.createContext({console});
const api = vm.runInContext(
  `${head}\nconst before = {...icons};\n${source('icons.js')}\n` +
  ';({icons, before, icon, navigation, NX_ICON_PATHS, NX_GLYPHS, nxIcon, nxGlyphParts, nxGlyphIcon, nxRect, nxCircle})', sandbox);

// Every page of the sidebar has a picture of its own.
const later = [];
for (const [file, pattern] of [['material_library.js', /const MATERIAL_ROUTE = '([^']+)'/],
  ['cloud_skills.js', /const CLOUD_SKILLS_ROUTE = '([^']+)'/], ['cloud_skills.js', /const CLOUD_SKILLS_LOAD_ROUTE = '([^']+)'/]]) {
  const found = pattern.exec(source(file));
  ck(found, `${file} still names its page the way this test reads it`);
  later.push(found[1]);
}
const routes = [...api.navigation.map(([route]) => route), ...later, 'workflows'];
ck(routes.length >= 18, 'the sidebar pages were found: ' + routes.length);
const seen = new Map();
for (const route of routes) {
  ck(Object.hasOwn(api.NX_ICON_PATHS, route), `the page ${route} has an icon of its own`);
  const d = api.NX_ICON_PATHS[route];
  ck(!seen.has(d), `the icon of ${route} is not the one of ${seen.get(d)}`);
  seen.set(d, route);
  ck(api.icon(route).includes(`d="${d}"`), `icon('${route}') of app.js draws the new picture`);
}

// Nothing a page already asks for was lost, and nothing is drawn the old way.
for (const key of Object.keys(api.before)) {
  ck(Object.hasOwn(api.NX_ICON_PATHS, key), `the icon ${key} of app.js is in the table`);
  ck(api.icons[key] === api.NX_ICON_PATHS[key], `icon('${key}') is drawn from the table`);
}

// One way to draw: path data only, on the 24 grid with a margin, nothing that needs a fill.
const number = /-?\d*\.?\d+/g;
for (const [key, d] of Object.entries(api.NX_ICON_PATHS)) {
  ck(/^M[\d.]/.test(d), `${key} starts with an absolute move`);
  ck(/^[MmLlHhVvCcSsAaZz\d\s.,-]+$/.test(d), `${key} is plain path data`);
  ck(!/NaN|undefined|e-/.test(d), `${key} has no broken number`);
  for (const move of d.matchAll(/M\s*(-?[\d.]+)[\s,]*(-?[\d.]+)/g)) {
    const [x, y] = [Number(move[1]), Number(move[2])];
    ck(x >= 2 && x <= 22 && y >= 2 && y <= 22, `${key} starts its strokes inside the margin: ${move[0]}`);
  }
  ck((d.match(number) || []).every(n => Math.abs(Number(n)) <= 24), `${key} stays on the 24 grid`);
}
ck(api.nxRect(3.5, 3.5, 7, 9, 2) === 'M5.5 3.5h3a2 2 0 0 1 2 2v5a2 2 0 0 1 -2 2h-3a2 2 0 0 1 -2 -2v-5a2 2 0 0 1 2 -2z', 'a rounded rectangle as a path');
ck(api.nxCircle(12, 4, 1.25) === 'M10.75 4a1.25 1.25 0 1 0 2.5 0a1.25 1.25 0 1 0 -2.5 0z', 'a circle as a path');
ck(api.nxIcon('check') === `<svg class="nx-i" viewBox="0 0 24 24" aria-hidden="true"><path d="${api.NX_ICON_PATHS.check}"/></svg>`, 'an icon by name');
ck(api.nxIcon('no-such-icon') === '', 'an unknown name draws nothing');

// Symbols typed as text.
for (const [glyph, name] of Object.entries(api.NX_GLYPHS)) {
  ck(Object.hasOwn(api.NX_ICON_PATHS, name), `the symbol ${glyph} has its icon ${name}`);
  const parts = api.nxGlyphParts(glyph);
  ck(parts && parts.length === 1 && parts[0].glyph === glyph && parts[0].icon === name, `${glyph} alone is drawn as ${name}`);
}
const shape = parts => parts && parts.map(part => part.glyph ? `<${part.icon}>` : part.text).join('|');
const text = parts => parts.map(part => part.glyph || part.text).join('');
ck(shape(api.nxGlyphParts('← 全部任务')) === '<back>| 全部任务', 'a line that opens with an arrow');
ck(shape(api.nxGlyphParts('开发指南 ↗')) === '开发指南 |<open>', 'a line that ends with an arrow');
ck(shape(api.nxGlyphParts(' × ')) === ' |<close>| ', 'a symbol alone keeps the space around it');
ck(shape(api.nxGlyphParts('✓ · 急停可用')) === '<check>| · 急停可用', 'a passed line');
ck(shape(api.nxGlyphParts('← 返回 ↗')) === '<back>| 返回 |<open>', 'both ends');
for (const sentence of ['我的机器人 → 能力表', '2 × 3 点', '仿真四足机器人 ✓ 可用', '正在读取…', '↗开发指南', '开发指南↗', '', '   ', '工作台']) {
  ck(api.nxGlyphParts(sentence) === null, `words stay words: "${sentence}"`);
}
for (const line of ['← 全部任务', '开发指南 ↗', ' × ', '✓ · 急停可用', '← 返回 ↗', '  ☰\n']) {
  ck(text(api.nxGlyphParts(line)) === line, `no character is lost from "${line}"`);
}
// A character of two code units before the arrow is not cut in half.
for (const line of ['巡检 😀 ↗', 'a𠮷 ↗', '← 😀']) ck(text(api.nxGlyphParts(line)) === line, `no half character in "${line}"`);
ck(shape(api.nxGlyphParts('✓ ✓ 通过')) === '<check>| ✓ 通过', 'only the first symbol of a line is drawn');
ck(api.nxGlyphParts(null) === null && api.nxGlyphParts(undefined) === null, 'nothing in, nothing out');

// The same symbol in two places that mean something else.
const arrow = {glyph: '↗', icon: 'open'};
ck(api.nxGlyphIcon(arrow, {id: 'user-avatar'}) === 'user', 'nobody signed in is a person, not an arrow');
ck(api.nxGlyphIcon(arrow, {id: '', matches: selector => selector === '.notice > span'}) === 'info', 'the mark of a notice');
ck(api.nxGlyphIcon(arrow, {id: '', matches: () => false}) === 'open', 'elsewhere it opens something');
ck(api.nxGlyphIcon({glyph: '✓', icon: 'check'}, {id: 'user-avatar'}) === 'check', 'other symbols are not changed by the place');

// The mark is one shape in the page and in the installer.
const css = source('icons.css');
const mark = /--nx-mark: url\("data:image\/svg\+xml,([^"]+)"\)/.exec(css);
ck(mark, 'the mark is in icons.css');
const drawn = decodeURIComponent(mark[1]);
ck(drawn.includes("d='M18 45V19L36 45V29L46 19M37 19h9v9'"), 'the mark is the N with the arrow');
const swift = fs.readFileSync(path.join(WEB, '..', 'desktop', 'icon.swift'), 'utf8');
const points = /let mark: \[\[\(Double, Double\)\]\] = (\[\[.*\]\])/.exec(swift);
ck(points, 'desktop/icon.swift lists the strokes of the mark');
ck(points[1].replace(/\s/g, '') === '[[(18,45),(18,19),(36,45),(36,29),(46,19)],[(37,19),(46,19),(46,28)]]', 'the installer icon draws the same strokes');
ck(/\.brand-mark span \{ display: none; \}/.test(css) && /\.brand-mark \{[^}]*mask: var\(--nx-mark\)/.test(css), 'the two characters of the old mark are not shown');

console.log(`icons_logic: ${checks} checks passed`);
