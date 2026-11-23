// Pure-logic checks of web/repair_materials.js (issue #30): the `library` event of a repair session in the
// session view (named 资料库) and in the fix chat (a warning when there is no usable driver material).
// What the event says is decided in brainos/repair_materials.py, checked in tests/test_repair_materials.py.
//   node tests/repair_materials_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// Stand-ins for the views the file wraps, drawn the way engineering.js and investigation.js draw them.
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const sandbox = {
  h,
  engineSessionBody: s => (s.events || []).map(e => `<div class="log-item"><span class="tag">${h(e.kind)}</span><p>${h(e.message)}</p></div>`).join(''),
  invChatLog: s => (s.events || []).map(e => e.kind === 'tool' ? `<div class="inv-act">读取：${h(e.message)}</div>`
    : e.kind === 'tool_error' ? `<div class="inv-act warn">${h(e.message)}</div>` : e.kind === 'message' ? `<p>${h(e.message)}</p>` : '').join(''),
};
vm.createContext(sandbox);
const api = vm.runInContext(readFileSync('web/repair_materials.js', 'utf8') + '\n;({body: s => engineSessionBody(s), chat: s => invChatLog(s)})', sandbox);
let checks = 0;
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };

const missing = {kind: 'library', missing: true, message: '资料库里没有这台设备可用的驱动资料。要装运控驱动，请先……上传'};
const found = {kind: 'library', missing: false, message: '资料库里这台设备有 2 份可用资料（驱动资料 1 份）：<b>manual</b>.md。'};
const repair = {mode: 'repair', events: [{kind: 'message', message: '开始'}, missing, found]};

// The session view: the tag is named, the message is the backend's, escaped by the view.
const body = api.body(repair);
includes(body, '<span class="tag">资料库</span>', 'tag named');
excludes(body, '<span class="tag">library</span>', 'raw kind gone');
includes(body, '没有这台设备可用的驱动资料', 'missing message shown');
includes(body, '&lt;b&gt;manual&lt;/b&gt;.md', 'file names stay text');
// Other sessions are left as they were.
includes(api.body({mode: 'discovery', events: [missing]}), '<span class="tag">library</span>', 'not a repair: untouched');

// The fix chat: no usable driver material is a warning line, materials found are a read.
const chat = api.chat(repair);
includes(chat, '<div class="inv-act warn">资料库里没有这台设备可用的驱动资料', 'missing is a warning');
includes(chat, '<div class="inv-act">读取：资料库里这台设备有 2 份可用资料', 'found is a read');
includes(chat, '&lt;b&gt;manual&lt;/b&gt;', 'escaped in the chat too');
includes(chat, '<p>开始</p>', 'other events unchanged');
// The session record itself is not changed by drawing it.
assert.equal(repair.events[1].kind, 'library'); checks += 1;
assert.equal(api.chat({events: undefined}), ''); checks += 1;
assert.equal(api.body({mode: 'repair', events: []}), ''); checks += 1;

console.log(JSON.stringify({passed: true, checks}));
