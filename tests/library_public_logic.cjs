// Pure-logic checks of web/library_public.js (issue #78): materials of the public library on the 资料库 page.
//   node tests/library_public_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// What material_library.js and material_parse.js draw (reduced to the parts this file changes).
const BASE_ROW = m => `<tr data-library-material="${m.id}"><td><span class="cell-main">${m.filename}</span></td>` +
  `<td><button type="button" class="button secondary small" data-library-action="delete" data-id="${m.id}">删除</button></td></tr>`;
const BASE_PANEL = r => (r.editable
  ? `<form class="mp-panel" data-parse-panel="${r.material.id}" data-parse-form="result"><input name="x"><div class="mp-actions">` +
    `<button data-parse-action="confirm">确认可用</button></div></form>`
  : `<div class="mp-panel" data-parse-panel="${r.material.id}">解析中</div>`);
const sandbox = {document: {}, materialRow: BASE_ROW, parsePanel: BASE_PANEL};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/library_public.js', 'utf8'), sandbox);
const lp = vm.runInContext('({lpRow, lpPanel, materialRow, parsePanel})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };

// ---- a row of 「已有资料」
{
  const own = {id: 'mat_1', filename: 'own.pdf', public: false, read_only: false};
  same(lp.materialRow(own), BASE_ROW(own), "the account's own material is drawn as before");
  same(lp.materialRow({id: 'mat_0', filename: 'old.pdf'}), BASE_ROW({id: 'mat_0', filename: 'old.pdf'}), 'a row of an older cloud (no marks) is drawn as before');
  const seen = lp.materialRow({id: 'mat_2', filename: 'go2.zip', public: true, read_only: true});
  ok(seen.startsWith('<tr data-lp-public data-lp-read-only data-library-material="mat_2"'), 'a public row the viewer may only read is marked so');
  ok(seen.includes('<span class="cell-main"><span class="lp-badge"'), 'and says 公共 in front of its name');
  ok(seen.includes('>公共</span>go2.zip'), 'the badge reads 公共');
  ok(seen.includes('data-library-action="delete"'), 'the delete button stays in the markup (hidden by the style sheet)');
  const operator = lp.materialRow({id: 'mat_3', filename: 'go2.zip', public: true, read_only: false});
  ok(operator.startsWith('<tr data-lp-public data-library-material'), 'an operator sees it public but not read-only');
  ok(!operator.includes('data-lp-read-only'), 'nothing hides an operator\'s buttons');
  same(lp.lpRow('<tr><td><span class="cell-main">a$&b</span></td></tr>', {public: true, read_only: true}),
       '<tr data-lp-public data-lp-read-only><td><span class="cell-main"><span class="lp-badge" title="公共资料库：所有账号都能看到">公共</span>a$&b</span></td></tr>',
       'a $ in a name stays a $');
}

// ---- a parse result
{
  const own = {material: {id: 'mat_1'}, editable: true, read_only: false, public: false};
  same(lp.parsePanel(own), BASE_PANEL(own), 'an own result is drawn as before (editable)');
  const parsing = {material: {id: 'mat_2'}, editable: false, read_only: true, public: true};
  same(lp.parsePanel(parsing), BASE_PANEL(parsing), 'a public result still being parsed is drawn as before');
  const seen = lp.parsePanel({material: {id: 'mat_3'}, editable: true, read_only: true, public: true});
  ok(seen.startsWith('<div class="mp-panel lp-read-only" data-parse-panel="mat_3" data-lp-read-only>'), 'a public result the viewer may only read is not a form');
  ok(!seen.includes('data-parse-form'), 'so the page does not submit it');
  ok(seen.includes('<fieldset disabled class="lp-fields">'), 'its fields are disabled');
  ok(seen.includes('只有平台运营人员能修改、确认或删除'), 'and it says who may change it');
  ok(seen.endsWith('</fieldset></div>'), 'the closing tags match');
  ok(!seen.includes('<form') && !seen.includes('</form>'), 'no form tag is left');
  const wrapped = lp.lpPanel('<form class="mp-panel" data-parse-panel="m"><i></i></form><p>after</p>', {editable: true, read_only: true});
  same(wrapped, '<div class="mp-panel lp-read-only" data-parse-panel="m" data-lp-read-only><p class="inline-note lp-note">' +
       '这是公共资料库里的资料，所有账号都能查看和下载；只有平台运营人员能修改、确认或删除。</p><fieldset disabled class="lp-fields"><i></i></fieldset></div><p>after</p>',
       'something another wrapper put after the form stays after it');
  same(lp.lpPanel('<div>other</div>', {editable: true, read_only: true}), '<div>other</div>', 'markup that is not the form is left alone');
}

console.log(`library_public_logic: ${checks} checks passed`);
