// Pure-logic checks of web/material_parse.js (issue #34): how the form's text becomes the values the cloud
// expects, how a result is drawn (states, missing items, joint limits with units), that what the model or
// the file said is shown as text, and that the 资料库 table keeps its columns. The rules themselves live
// in the cloud (cloud/neuxmind_cloud/material_parse.py, cloud/tests/test_material_parse.py).
//   node tests/material_parse_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const dialogElement = {addEventListener: () => {}, dataset: {}, open: false, close: () => {}};
const sandbox = {
  h, navigation: [['overview', '任务工作台']], icons: {}, views: {}, current: 'library',
  state: {cloud_account: {logged_in: true}, families: {quadruped: '四足机器人'}, robots: []},
  familyNames: {quadruped: '四足机器人'},
  document: {addEventListener: () => {}, querySelector: () => null},
  location: {hash: ''}, route: () => {}, render: () => {}, refresh: async () => {}, toast: () => {},
  api: async () => ({}), fetch: async () => ({ok: true, json: async () => ({})}), dialog: () => {},
  $: () => dialogElement, CSS: {escape: v => v}, setTimeout: () => 0, clearTimeout: () => {},
  heading: (en, title) => `<h1>${title}</h1>`, notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}">${text}</div>`,
  panel: (title, body) => `<section>${title}${body}</section>`, empty: (title, sub) => `<div class="empty">${title}${sub}</div>`,
  table: (headers, data) => `<table><thead>${headers.map(x => `<th>${x}</th>`).join('')}</thead><tbody>${data.join('')}</tbody></table>`,
  badge: (text, cls = '') => `<span class="status ${cls}">${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  field: (label, control, note = '') => `<label class="field">${label}${control}${note}</label>`,
  date: value => `date:${value}`, find: () => null,
  // device_profile.js / live_status.js, as far as this file uses them
  PROFILE_TABS: {ros2: 'ROS 2', hardware: '硬件', resources: '资源占用'}, profileTabs: () => '<div class="ls-tabs"></div>',
  profileBody: () => '<base-body>', profileHead: () => '<head>', profileSystem: () => '<system>', profileRender: () => {},
  liveCount: n => `<i>${n}</i>`, liveLoading: text => `<p class="loading">${text}</p>`,
};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/material_library.js', 'utf8'), sandbox);
const page = vm.runInContext(readFileSync('web/material_parse.js', 'utf8') + `
;({parseLines, parseNamedLines, parseNamedText, parseNumber, parseFormValues, parsePanel, parseStageText,
   materialRow, MATERIAL_STATES, PROFILE_TABS})`, sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${String(text).slice(0, 500)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${String(text).slice(0, 500)}`); checks += 1; };

// --- 1. The four states of R1-13, and the profile's tab.
check(page.MATERIAL_STATES.parsed, ['待确认', 'warn'], 'parsed reads 待确认');
check(page.MATERIAL_STATES.usable, ['可用', 'good'], 'usable reads 可用');
check(page.MATERIAL_STATES.failed, ['没解析出来', 'bad'], 'failed reads 没解析出来');
check(page.PROFILE_TABS.materials, '资料', 'the device profile has a 资料 tab');

// --- 2. Text fields become lists; names and types are split at the first space.
check(page.parseLines(' a \n\n b\r\n'), ['a', 'b'], 'one item per line, blanks dropped');
check(page.parseNamedLines('/cmd_vel geometry_msgs/msg/Twist\n/odom'), [{name: '/cmd_vel', type: 'geometry_msgs/msg/Twist'},
  {name: '/odom', type: ''}], 'name and type');
check(page.parseNamedText([{name: '/a', type: 't'}, {name: '/b', type: ''}]), '/a t\n/b', 'and back');
check([page.parseNumber(''), page.parseNumber(' 1.5 '), page.parseNumber('-0')], [null, 1.5, 0], 'numbers');
assert.ok(Number.isNaN(page.parseNumber('abc')) && Number.isNaN(page.parseNumber('Infinity'))); checks += 1;

// --- 3. The form, read back as the cloud expects it.
const urdf = {material: {id: 'mat_1'}, kind: 'urdf', status: 'failed', status_detail: '缺：关节 b 的力矩上限', stage: 'done',
  editable: true, revision: 3, note: '关节上限只是参考，不会自动生效。', warnings: ['<b>xacro</b> 表达式'], error: '',
  missing: ['关节 b 的力矩上限'], not_applicable: [], values: {robot_name: 'demo', links: ['l1'], joints: [
    {name: 'a', type: 'revolute', lower: -1, upper: 1, velocity: 2, effort: 3},
    {name: 'b', type: 'prismatic', lower: 0, upper: 0.4, velocity: 0.2, effort: null, raw: {effort: '${e}'}}]},
  fields: [{key: 'robot_name', label: '机器人名称', type: 'text', required: false},
    {key: 'links', label: '连杆', type: 'text_list', required: true},
    {key: 'joints', label: '关节和上限', type: 'joints', required: true}]};
const inputs = {'pf-robot_name': ' arm ', 'pf-links': 'l1\nl2', 'pj-0-lower': '-1', 'pj-0-upper': '1', 'pj-0-velocity': '2',
  'pj-0-effort': '3', 'pj-1-lower': '0', 'pj-1-upper': '0.4', 'pj-1-velocity': '0.2', 'pj-1-effort': '12', 'na-links': false};
const form = {querySelector: selector => {
  const name = selector.match(/name="([^"]+)"/)[1];
  if (!(name in inputs)) return null;
  return name.startsWith('na-') ? {checked: inputs[name]} : {value: inputs[name]};
}};
const read = page.parseFormValues(form, urdf);
check(read.errors, [], 'no errors');
check(read.values.robot_name, 'arm', 'text trimmed');
check(read.values.links, ['l1', 'l2'], 'list');
check(read.values.joints[1].effort, 12, 'a limit filled in by a person');
check(read.values.joints[1].raw, {effort: '${e}'}, 'what the file said is kept');
inputs['pj-1-effort'] = 'x';
includes(page.parseFormValues(form, urdf).errors.join(), '关节 b 的力矩上限不是数字', 'a limit that is not a number is named');
inputs['na-links'] = true;
check(page.parseFormValues(form, urdf).not_applicable, ['links'], 'not applicable');

// --- 4. A result as drawn: states, units, missing items, and only text from the file or the model.
const html = page.parsePanel(urdf);
includes(html, '没解析出来', 'state');
includes(html, '不会自动生效', 'limits are a reference');
includes(html, 'name="pj-1-effort"', 'a field for the missing limit');
includes(html, '还缺：关节 b 的力矩上限', 'the missing item is named');
includes(html, '&lt;b&gt;xacro&lt;/b&gt;', 'a warning is text');
includes(html, 'title="资料里写的是 ${e}"', 'the xacro expression is shown where it was');
includes(html, '>m</span>', 'prismatic positions are in metres');
includes(html, '>N</span>', 'prismatic force is in newtons');
const doc = {...urdf, kind: 'document', status: 'parsed', missing: [], values: {driver_name: '<img src=x onerror=alert(1)>',
  topics: [{name: '/odom', type: 'nav_msgs/msg/Odometry'}]}, fields: [
  {key: 'driver_name', label: '驱动名称', type: 'text', required: true},
  {key: 'topics', label: '话题', type: 'named_list', required: true}]};
const docHtml = page.parsePanel(doc);
excludes(docHtml, '<img', 'an answer is never markup');
includes(docHtml, '/odom nav_msgs/msg/Odometry', 'topics one per line');
excludes(docHtml, '还缺', 'nothing missing');
const waiting = page.parsePanel({...urdf, editable: false, stage: 'indexing', retrying: false, status: 'parsing'});
includes(waiting, '解析服务正在读取这份文档', 'the stage while parsing');
excludes(waiting, 'data-parse-action="confirm"', 'nothing to confirm while parsing');
includes(page.parseStageText({stage: 'asking', retrying: true, error: '连不上解析服务'}), '稍后会自动再试', 'retrying');

// --- 5. The 资料库 table keeps one cell per column and gets the result button.
vm.runInContext('materialLibrary.formats = {formats: []};', sandbox);   // a let of the page script
const row = page.materialRow({id: 'mat_9', filename: 'a.urdf', kind: 'document', format: 'urdf', size: 10, body_type: 'quadruped',
  vendor: 'v', model: 'm', version: '1', status: 'failed', status_detail: '缺：连杆', sha256: 'f'.repeat(64), created_at: 1});
check((row.match(/<td/g) || []).length, 8, 'eight cells as before');
includes(row, 'data-parse-action="open"', 'the result opens from the row');
includes(row, '缺：连杆', 'what is missing shows under the state');

// A `$` in a name inside the state's detail stays as it is (a string replacement would expand `$'`, `$&`).
const dollar = page.materialRow({id: 'mat_8', filename: 'b.urdf', kind: 'document', format: 'urdf', size: 1, body_type: 'quadruped',
  vendor: 'v', model: 'm', version: '1', status: 'failed', status_detail: "缺：关节 $'x$& 的力矩上限", sha256: 'e'.repeat(64), created_at: 1});
check((dollar.match(/<td/g) || []).length, 8, 'still eight cells');
includes(dollar, '$&#39;x$&amp; 的力矩上限', 'the name is shown as written');

console.log(`material_parse_logic: ${checks} checks passed`);
