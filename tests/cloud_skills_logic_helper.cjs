// Shared by the pure-logic checks of issue #43's page scripts (web/cloud_skills.js, web/remote_calls.js,
// web/robot_bundle.js): a bare VM with stand-ins for what web/app.js and earlier scripts provide. The
// helpers that build HTML are written as they are in web/app.js, so escaping is checked for real.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);

function sandbox(extra = {}) {
  const element = () => ({addEventListener() {}, dataset: {}, open: false, close() {}});
  const box = {
    h, state: {robots: [], cloud_account: {logged_in: true}, user: {name: '张工', email: 'owner@example.test'}},
    projectId: 'project-1', current: 'cloud-skills', location: {hash: ''}, icons: {}, navigation: [], views: {},
    document: {addEventListener() {}, querySelector: () => null, getElementById: () => null, body: {append() {}}},
    $: element, CSS: {escape: v => String(v)}, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
    robotDetailRender: () => {}, dialog: () => {}, render: () => {}, route: () => {}, toast: () => {},
    api: async () => ({}), find: () => null, project: () => ({id: 'project-1'}), newRequestId: () => 'request-1',
    cloudSignInKey: () => 'owner|1', cloudSignInNotice: text => `<div class="notice">${text}</div>`,
    icon: () => '<svg></svg>', date: v => (v ? 'DATE' : '—'),
    badge: (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`,
    button: (text, action, cls = '', id = '', disabled = false) =>
      `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`,
    notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><div>${text}</div></div>`,
    panel: (title, body, right = '') => `<section class="panel"><h2>${title}</h2>${right}${body}</section>`,
    empty: (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b}</div>`,
    heading: (en, title, sub, buttons = '') => `<div class="page-head"><h1>${title}</h1><p>${sub}</p>${buttons}</div>`,
    table: (headers, data) => `<table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table>`,
    field: (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`,
    select: (name, items) => `<select name="${name}">${items.map(([v, t]) => `<option value="${h(v)}">${h(t)}</option>`).join('')}</select>`,
    skillPreviewBody: r => `<div class="preview">${h(r.path)}</div>`,
    ...extra,
  };
  vm.createContext(box);
  return box;
}

// Run page scripts in one VM and hand back the names asked for.
function load(files, names, extra) {
  const box = sandbox(extra);
  for (const file of files.slice(0, -1)) vm.runInContext(readFileSync(file, 'utf8'), box);
  const page = vm.runInContext(readFileSync(files.at(-1), 'utf8') + `\n;({${names.join(', ')}})`, box);
  return {page, box};
}

function checker() {
  const counted = {checks: 0};
  // Values made inside the VM have other prototypes than ours: compare what they hold, as JSON.
  counted.check = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); counted.checks += 1; };
  counted.includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); counted.checks += 1; };
  counted.excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); counted.checks += 1; };
  return counted;
}

const HOSTILE = '<img src=x onerror=alert(1)>"\'&';
// True when nothing of HOSTILE reached the HTML as markup.
const escaped = html => !/<img|onerror=alert\(1\)>"/.test(html) && html.includes('&lt;img');

module.exports = {load, checker, HOSTILE, escaped, h};
