'use strict';
// Issue #78: the public library (公共资料库) on the 资料库 page.
//
// The cloud lists the materials of the public library next to the account's own (`public: true`); every
// account reads them, only platform operators change them (`read_only` says the viewer may only read).
// This file marks such a row 「公共」, hides the buttons that change a material the viewer may only read
// (the cloud refuses them anyway), and shows a parse result of such a material read-only. It wraps the
// functions of material_library.js, material_parse.js and model_library.js (loaded before it).

const LP_BADGE = '<span class="lp-badge" title="公共资料库：所有账号都能看到">公共</span>';
const LP_NOTE = '这是公共资料库里的资料，所有账号都能查看和下载；只有平台运营人员能修改、确认或删除。';

// ---------------------------------------------------------------- pure
// A row of 「已有资料」 for a public material: marked, and read-only when the viewer may only read it.
function lpRow(html, m) {
  if (!m || !m.public) return html;
  const marks = ' data-lp-public' + (m.read_only ? ' data-lp-read-only' : '');
  return html.replace(/^<tr\b/, () => '<tr' + marks)
    .replace('<span class="cell-main">', () => '<span class="cell-main">' + LP_BADGE);
}

// A parse result the viewer may only read: the same fields, disabled, without the buttons that change it.
function lpPanel(html, result) {
  if (!result || !result.read_only || !result.editable) return html;
  const opened = html.replace(/^<form class="mp-panel"([^>]*)>/,
    (_, attrs) => `<div class="mp-panel lp-read-only"${attrs.replace(/\sdata-parse-form="[^"]*"/, '')} data-lp-read-only>` +
      `<p class="inline-note lp-note">${LP_NOTE}</p><fieldset disabled class="lp-fields">`);
  const end = opened.lastIndexOf('</form>');
  if (opened === html || end < 0) return html;
  return opened.slice(0, end) + '</fieldset></div>' + opened.slice(end + '</form>'.length);
}

// ---------------------------------------------------------------- wrapping what is there
function lpInstall() {
  if (typeof materialRow === 'function') {
    const baseRow = materialRow;
    materialRow = m => lpRow(baseRow(m), m);
  }
  if (typeof parsePanel === 'function') {
    const basePanel = parsePanel;
    parsePanel = result => lpPanel(basePanel(result), result);
  }
}
if (typeof document !== 'undefined') lpInstall();
