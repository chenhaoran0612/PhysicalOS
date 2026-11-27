// Pure-logic checks of web/model_library.js and web/mujoco_sim.js (issue #51): which brands and models an
// upload can be for, which robot model is the one in use, what the upload form sends, what cannot be a robot
// model, which buttons an environment has in each state, and that names from the cloud are shown as text.
// The rules themselves are in brainos/model_library.py, brainos/mujoco_sim.py and the cloud.
//   node tests/model_library_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const calls = [];
const sandbox = {
  h, navigation: [], icons: {}, views: {}, current: 'library', projectId: 'project-1',
  state: {cloud_account: {logged_in: true, configured: true}, families: {quadruped: '四足机器人', manipulator: '机械臂'}, robots: []},
  familyNames: {quadruped: '四足机器人', manipulator: '机械臂'},
  document: {addEventListener: () => {}, querySelector: () => null},
  location: {hash: ''}, route: () => {}, render: () => {}, refresh: async () => {}, toast: () => {},
  api: async (path, payload) => { calls.push([path, payload]); return sandbox.answers[path] || {}; }, answers: {},
  fetch: async () => ({ok: true, json: async () => ({})}), dialog: () => {}, $: () => null,
  setTimeout: fn => setTimeout(fn, 0), clearTimeout, setInterval: () => 0,
  heading: (en, title, sub, buttons = '') => `<h1>${title}</h1><p>${sub}</p>${buttons}`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}">${text}</div>`,
  panel: (title, body, right = '') => `<section class="panel"><h2>${title}</h2>${right}${body}</section>`,
  empty: (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b}</div>`,
  table: (headers, data) => `<table><thead>${headers.join('|')}</thead><tbody>${data.join('')}</tbody></table>`,
  badge: (text, cls = '') => `<span class="status ${cls}">${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  field: (label, control, note = '') => `<label class="field">${label}${control}${note}</label>`,
  date: value => `date:${value}`, find: () => null,
  simStateNames: {running: ['运行中', 'good'], stopped: ['已关闭', '']}, simLiveOpen: () => {}, simKindLabel: r => 'kind:' + r.sim.kind,
  engineAction: async () => {},
};
vm.createContext(sandbox);
const lib = vm.runInContext(['combo.js', 'robot_catalog.js', 'cloud_auto_login.js', 'material_library.js', 'model_library.js', 'mujoco_sim.js']
  .map(name => readFileSync('web/' + name, 'utf8')).join('\n') + `
;({mlKey, mlLabel, mlProducts, mlModelsOf, mlModelPrecheck, mlLabels, mlKnown, mlProductFields, mlVendorChoices, mlOwned, rcModelChoices, mlTypedSync, mlModelsPanel, mlState, materialRow, materialFilters,
   materialLibrary, MATERIAL_KINDS, api, mjSceneActions, mjTouchText, mjSceneCard, mjBody, mjBusyScene, simKindLabel})`, sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${String(text).slice(0, 400)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${String(text).slice(0, 400)}`); checks += 1; };

const ACME = {vendor: 'Acme Robotics', model: 'Go-7'};

(async () => {
  // ---- brands and models
  check(lib.mlKey({vendor: ' Acme  Robotics', model: 'GO-7 '}), 'acme robotics\ngo-7', 'compared without case or extra spaces');
  check(lib.mlKey(null), '', 'none');
  check(lib.mlLabel(null), '未填写品牌型号', 'no label');
  const robots = [{name: '一号', family: 'quadruped', product: ACME}, {name: '二号', family: 'quadruped', product: {vendor: 'acme robotics', model: 'go-7'}},
    {name: '无名', family: 'quadruped'}, {name: '仿真', family: 'quadruped', product: {vendor: 'neuxmind-sim', model: 'sim-quadruped'}, sim: {kind: 'quadruped'}}];
  check(lib.mlProducts([], robots), [{...ACME, family: 'quadruped', robots: ['一号', '二号']}], 'from the robots in the page: each once, simulated ones aside');
  check(lib.mlProducts([{vendor: 'Other', model: 'Arm-1', family: 'manipulator', robots: []}, {vendor: '', model: 'x'}], robots),
    [{vendor: 'Other', model: 'Arm-1', family: 'manipulator', robots: []}], 'the server\'s list wins, incomplete entries are dropped');

  // ---- the robot model in use
  const models = [
    {id: 'a', kind: 'model', ...ACME, status: 'usable', created_at: 10}, {id: 'b', kind: 'model', vendor: 'ACME ROBOTICS', model: 'go-7', status: 'usable', created_at: 30},
    {id: 'c', kind: 'model', ...ACME, status: 'parsed', created_at: 40}, {id: 'd', kind: 'document', ...ACME, status: 'usable', created_at: 50},
    {id: 'e', kind: 'model', vendor: 'Other', model: 'Arm-1', status: 'usable', created_at: 60}];
  const found = lib.mlModelsOf(ACME, models);
  check([found.current.id, found.others.map(m => m.id), found.older.map(m => m.id)], ['b', ['c'], ['a']], 'the newest confirmed one is in use');
  check(lib.mlModelsOf({vendor: 'None', model: 'x'}, models).current, null, 'none for another brand');

  // ---- what cannot be a robot model
  const formats = {extensions: ['.urdf', '.zip', '.tar.gz', '.tgz'], max_bytes: 1000};
  check(lib.mlModelPrecheck({name: 'dog.URDF', size: 10}, formats), '', 'a urdf');
  check(lib.mlModelPrecheck({name: 'dog.tar.gz', size: 10}, formats), '', 'an archive');
  includes(lib.mlModelPrecheck({name: 'dog.xacro', size: 10}, formats), '.xacro 要先展开', 'xacro is turned away');
  includes(lib.mlModelPrecheck({name: 'dog.zip', size: 2000}, formats), '太大', 'over the limit');
  includes(lib.mlModelPrecheck({name: 'dog.urdf', size: 0}, formats), '空文件', 'empty');

  // ---- what an upload sends
  const products = [{...ACME, family: 'quadruped', robots: ['一号']}];
  // issue #61: the brand and model are the same two controls as everywhere, chosen or typed
  check(lib.mlLabels({version: ' 1.0 ', vendor: ' acme  robotics ', model: 'go-7', body_type: 'manipulator'}, products).labels,
    {body_type: 'quadruped', vendor: 'Acme Robotics', model: 'Go-7', version: '1.0'}, 'a brand and model the account has gives its own spelling and body type');
  check(lib.mlLabels({version: '1', vendor: ' New ', model: ' N1 ', body_type: 'manipulator'}, products).labels,
    {body_type: 'manipulator', vendor: 'New', model: 'N1', version: '1'}, 'a new one is taken as typed');
  includes(lib.mlLabels({version: '1', vendor: 'New', model: ''}, products).error, '一起填', 'half is refused');
  includes(lib.mlLabels({version: '', vendor: 'Acme Robotics', model: 'Go-7'}, products).error, '版本', 'the version is needed');
  includes(lib.mlLabels({version: '1', vendor: 'New', model: 'N1'}, products).error, '本体类型', 'a new brand and model needs its body type');
  check(lib.mlKnown({vendor: 'ACME ROBOTICS', model: 'Go-7'}, products)?.family, 'quadruped', 'known in any case');
  check(lib.mlKnown({vendor: 'Acme Robotics', model: ''}, products), null, 'half a brand and model is none');
  {
    const before = lib.mlState.products;
    lib.mlState.products = products;
    const count = (html, piece) => html.split(piece).length - 1;
    const known = lib.mlProductFields({vendor: ACME.vendor, model: ACME.model}, {});
    check([count(known, 'name="vendor"'), count(known, 'name="model"'), count(known, 'name="product"'), count(known, '<select')], [1, 1, 0, 1],
      'one brand, one model, no list of pairs next to them; the only list is the body type');
    includes(known, 'data-combo-source="mlVendorChoices"', 'the brand offers what the library knows');
    includes(known, 'value="Acme Robotics"', 'filled in');
    includes(known, 'data-ml-typed hidden', 'a brand and model the account has brings its body type: not asked');
    excludes(known, '其他品牌型号', 'no "other" to choose first');
    excludes(lib.mlProductFields(null, {}), 'data-ml-typed hidden', 'nothing chosen yet: the body type is asked');
    excludes(lib.mlProductFields({vendor: 'New', model: 'N1'}, {}), 'data-ml-typed hidden', 'a new brand and model: asked');
    excludes(lib.mlProductFields({vendor: ACME.vendor, model: ACME.model}, {}, false), 'body_type', 'moving a material asks no body type');
    includes(known, 'data-rc-owned="mlOwned"', 'and what the library has comes first');
    check(lib.mlVendorChoices()[0], {value: 'Acme Robotics', keys: [], note: '已有'}, 'the brands the library has come first in the list');
    check(lib.mlOwned().map(o => o.product.model), ['Go-7'], 'what the library has');
    const box = vendor => ({closest: () => ({dataset: {rcOwned: 'mlOwned'}, querySelector: () => ({value: vendor})})});
    check(lib.rcModelChoices(box('acme robotics')).map(o => o.value), ['Go-7'], 'and their models');
    const typed = {hidden: true}, form = (vendor, model) => ({querySelector: () => typed, elements: {vendor: {value: vendor}, model: {value: model}}});
    lib.mlTypedSync(form('New', 'N1'));
    check(typed.hidden, false, 'typing a new brand and model brings the body type into sight');
    lib.mlTypedSync(form('acme robotics', 'GO-7'));
    check(typed.hidden, true, 'and one the account has takes it away again');
    lib.mlTypedSync(null);
    lib.mlState.products = before;
  }
  // ---- the pages
  check(lib.MATERIAL_KINDS.model, '机器人模型', 'the kind has a name');
  lib.mlState.products = products;
  lib.mlState.models = [{id: 'mat_1', kind: 'model', ...ACME, status: 'parsed', filename: '<img src=x onerror=alert(1)>.urdf', version: '1', size: 10,
    created_at: 1, status_detail: '缺：关节 <b>hip</b> 的位置上限'}];
  lib.mlState.modelsLoaded = true;
  const panel = lib.mlModelsPanel();
  includes(panel, '还没有可用的机器人模型', 'a brand and model without a confirmed model says so');
  includes(panel, '&lt;img src=x onerror=alert(1)&gt;.urdf', 'a file name is text');
  includes(panel, '缺：关节 &lt;b&gt;hip&lt;/b&gt;', 'what is missing is text');
  excludes(panel, '<img src=x', 'no markup from a file name');
  includes(panel, 'data-ml-action="upload-model"', 'the model has its own upload');
  const row = lib.materialRow({id: 'mat_2', filename: 'a.md', kind: 'document', format: 'markdown', size: 1, body_type: 'quadruped', ...ACME,
    version: '1', status: 'parsing', sha256: 'f'.repeat(64), created_at: 1});
  includes(row, 'data-ml-action="move"', 'a material can be moved');
  includes(lib.materialFilters(), 'data-ml-filter', 'the list can be narrowed to a brand and model');

  // ---- the library overview comes with the brands and models; a device's materials are its brand and model's
  sandbox.answers = {'library/products': {products, formats: {model: formats, formats: []}, materials: [], total: 0},
    'library/robot': {materials: [{id: 'mat_3'}], total: 1, product: ACME, shared_with: ['二号']}};
  const overview = await lib.api('library/overview', {limit: 5});
  check([calls.at(-1)[0], overview.products.length, lib.mlState.modelFormats.max_bytes], ['library/products', 1, 1000], 'the overview');
  const mine = await lib.api('library/list', {device_id: 'robot-1', limit: 200});
  check([calls.at(-1), mine.materials.length, lib.mlState.robots['robot-1'].shared], [['library/robot', {robot_id: 'robot-1', project_id: 'project-1'}], 1, ['二号']],
    'a device\'s materials are asked for by robot');
  await lib.api('library/list', {kind: 'bundle', device_id: 'robot-1'});
  check(calls.at(-1)[0], 'library/list', 'a robot\'s programs and scene data are still listed by device');

  // ---- simulation environments
  check(lib.mjSceneActions({busy: true, status: 'generating', session_id: 's1'}, []), ['session'], 'while the assistant writes');
  check(lib.mjSceneActions({busy: true, status: 'preparing'}, []), [], 'while the model is fetched');
  check(lib.mjSceneActions({status: 'awaiting_review'}, []), ['confirm', 'reject', 'remove'], 'to be confirmed');
  check(lib.mjSceneActions({status: 'confirmed'}, []), ['start', 'reject', 'remove'], 'confirmed');
  check(lib.mjSceneActions({status: 'confirmed'}, [{id: 'r'}]), ['start'], 'in use: it cannot be refused or removed');
  check(lib.mjSceneActions({status: 'rejected'}, []), ['confirm', 'remove'], 'refused');
  check(lib.mjSceneActions({status: 'failed', session_id: 's1'}, []), ['session', 'remove'], 'failed after the assistant ran');
  check(lib.mjTouchText({touching: ['墙'], sim_time_s: 12.4}), '正碰到：墙 · 仿真时间 12 秒', 'what it touches');
  check(lib.mjTouchText({touching: [], fault: '仿真发散了'}), '仿真出错，已停止推进：仿真发散了', 'a fault comes first');
  check(lib.mjBusyScene({scenes: [{busy: false}, {busy: true}]}), true, 'busy');
  const view = {application: {id: 'app-1', name: '送货', product: ACME}, family: 'quadruped', problem: null, robots: [],
    robot_model: {filename: 'dog.zip', version: '1.0'},
    scenes: [{id: 'mujoco-scene-1', status: 'awaiting_review', source: 'agent', created_at: 1, updated_at: 2, preview: true, busy: false,
      material: {filename: 'dog.zip', version: '1.0'}, scene: {summary: '<script>alert(1)</script>'}, lines: ['墙：方块 <b>'], error: ''}]};
  const body = lib.mjBody(view);
  includes(body, '&lt;script&gt;alert(1)&lt;/script&gt;', 'what the assistant wrote is text');
  includes(body, '墙：方块 &lt;b&gt;', 'the lines are text');
  excludes(body, '<script>', 'no markup from the scene');
  includes(body, '/api/sim/mujoco/preview?id=mujoco-scene-1', 'the preview is a picture from this backend');
  includes(body, 'data-mj-action="confirm"', 'it can be confirmed');
  includes(lib.mjBody({...view, problem: {code: 'sim_no_robot_model', message: '还没有可用的机器人模型'}, scenes: []}), 'data-mj-action="library"', 'a missing model leads to the library');
  excludes(lib.mjBody({...view, problem: {code: 'sim_no_robot_model', message: 'x'}, scenes: []}), 'data-mj-action="empty"', 'nothing can be made without a model');
  check(lib.simKindLabel({sim: {kind: 'mujoco'}, product: ACME}), 'MuJoCo 仿真 · Acme Robotics Go-7', 'the card of a MuJoCo robot');
  check(lib.simKindLabel({sim: {kind: 'arm'}}), 'kind:arm', 'other simulated robots keep their label');

  console.log(`model_library_logic: PASS (${checks} checks)`);
})().catch(err => { console.error('model_library_logic: FAIL'); console.error(err); process.exitCode = 1; });
