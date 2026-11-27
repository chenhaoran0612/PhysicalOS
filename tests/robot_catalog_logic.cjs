// Pure-logic checks of web/robot_catalog.js (issue #53): the list of brands and models, the brand and
// model controls that replace the two inputs of web/robot_model.js (since issue #61 each is one control,
// a list that can also be typed in), and what a robot looks like.
//   node tests/robot_catalog_logic.cjs
const {readFileSync, existsSync, readdirSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {rmInputs: () => 'the two inputs of issue #48'};
vm.createContext(sandbox);
const rc = vm.runInContext(readFileSync('web/combo.js', 'utf8') + '\n' + readFileSync('web/robot_catalog.js', 'utf8') +
  '\n;({ROBOT_CATALOG, rcBrand, rcFind, rcBrands, rcPicture, rcInputs, rcVendorOptions, rcModelOptions, rcKeepsModel, rcVendorChoices, rcModelChoices, rcPickerOwned, rcChanged, rmInputs})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };

// ---- the list
{
  const brands = rc.rcBrands().map(b => b.vendor);
  for (const brand of ['智身科技', '星玑科技', '宇树科技', 'Universal Robots', 'Franka Robotics', 'KUKA', 'ABB', 'FANUC', 'Kinova', 'UFACTORY', '越疆', '节卡', '遨博', '睿尔曼', '松灵机器人']) {
    ok(brands.includes(brand), `${brand} can be chosen`);
  }
  same(brands.indexOf('智身科技'), 0, 'our own robots come first');
  ok(!brands.includes('neuxmind-sim'), 'the brand of the simulated robots is not offered to a person');
  const zs = rc.rcBrand('智身科技').models.map(m => m.model);
  for (const model of ['钢镚 L1', '钢镚 L2', '钢镚 L2-W', '钢镚 L2-U', '铜锤 M1']) ok(zs.includes(model), `智身科技 ${model} can be chosen`);
  const names = new Set();
  for (const brand of rc.ROBOT_CATALOG) {
    ok(brand.vendor && brand.vendor.length <= 100, `a brand name fits the field: ${brand.vendor}`);
    for (const name of [brand.vendor, ...brand.aliases]) {
      const key = name.toLowerCase().replace(/[\s\-_.·]+/g, '');
      ok(!names.has(key), `the brand name "${name}" means one brand`);
      names.add(key);
    }
    const models = new Set();
    for (const m of brand.models) {
      ok(m.model && m.model.length <= 100 && ['quadruped', 'wheel_legged', 'manipulator', 'humanoid', 'wheeled'].includes(m.type), `${brand.vendor} ${m.model} has a name and a body type`);
      for (const name of [m.model, ...m.aliases]) {
        const key = name.toLowerCase().replace(/[\s\-_.·]+/g, '');
        ok(!models.has(key), `${brand.vendor}: the model name "${name}" means one model`);
        models.add(key);
      }
      if (m.image) ok(existsSync('web/robot_images/' + m.image), `the picture of ${brand.vendor} ${m.model} exists: ${m.image}`);
    }
  }
  // Every picture in the directory is used and has its source written down.
  const sources = readFileSync('web/robot_images/SOURCES.md', 'utf8');
  const used = new Set(rc.ROBOT_CATALOG.flatMap(b => b.models.map(m => m.image)).filter(Boolean));
  for (const file of readdirSync('web/robot_images')) {
    if (file.endsWith('.webp')) {
      ok(used.has(file), `${file} is the picture of a model`);
      ok(sources.includes('| ' + file.replace('.webp', '') + ' |'), `${file} has its source in SOURCES.md`);
      ok(/^[a-z0-9][a-z0-9-]*\.webp$/.test(file), `${file} has a name the server serves`);
    }
  }
  for (const file of ['type-quadruped.svg', 'type-wheel-legged.svg', 'type-manipulator.svg', 'type-humanoid.svg', 'type-other.svg']) ok(existsSync('web/robot_images/' + file), `${file} exists`);
}

// ---- which brand and model a typed or reported text is
{
  same(rc.rcBrand('Unitree').vendor, '宇树科技', 'a brand is known by its other names');
  same(rc.rcBrand(' unitree-robotics ').vendor, '宇树科技', 'without case, spaces and hyphens');
  same(rc.rcBrand('示例机器人公司'), null, 'a brand that is not listed');
  same(rc.rcFind({vendor: '星玑', model: 'cs1'}).entry.type, 'quadruped', '星玑 CS1 is a quadruped');
  same(rc.rcPicture({product: {vendor: '星玑', model: 'CS-DOG1'}}).src, '/robot_images/xingji-cs1.webp', 'with its own picture, also by the name its manual uses');
  same(rc.rcBrand(''), null, 'no brand');
  same(rc.rcFind({vendor: 'unitree', model: 'go2'}).entry.model, 'Go2', 'a model likewise');
  same(rc.rcFind({vendor: 'zsibot', model: 'ZSL-1'}).entry.model, '钢镚 L1', 'a model by another name');
  same(rc.rcFind({vendor: '宇树科技', model: 'X99'}).entry, null, 'a model that is not listed, of a brand that is');
  same(rc.rcFind({vendor: 'Acme', model: 'X1'}), null, 'a brand that is not listed');
  same(rc.rcFind(null), null, 'no brand and model');
  // A robot with wheels on its legs is of another type than a quadruped.
  const type = (vendor, model) => rc.rcFind({vendor, model}).entry.type;
  same([type('智身科技', '钢镚 L2'), type('智身科技', '钢镚 L2-W'), type('智身科技', '钢镚 L2U'), type('智身科技', '钢镚 L1-W')].join(), 'quadruped,wheel_legged,wheel_legged,wheel_legged', 'quadruped and wheel-legged are told apart');
  same(rc.rcPicture({family: 'quadruped', product: {vendor: '智身科技', model: '钢镚 L2-W'}}).src, '/robot_images/type-wheel-legged.svg', 'and drawn apart');
  same(rc.rcPicture({family: 'quadruped', product: {vendor: 'zsibot', model: 'l2u'}}).src, '/robot_images/zsibot-l2u.webp', 'the L2U has its own picture, made from its URDF');
  const humanoids = rc.ROBOT_CATALOG.flatMap(b => b.models.filter(m => m.type === 'humanoid').map(m => b.vendor + ' ' + m.model));
  ok(humanoids.length >= 20, `humanoids can be chosen (${humanoids.length})`);
  for (const one of ['宇树科技 G1', '智元机器人 远征 A2', '优必选 Walker S2', '傅利叶 GR-1', '智身科技 银毅 NE01', 'Tesla Optimus']) ok(humanoids.includes(one), `${one} can be chosen`);
}

// ---- what a robot looks like
{
  const go2 = rc.rcPicture({family: 'quadruped', product: {vendor: 'Unitree', model: 'Go2'}});
  same(go2.src, '/robot_images/unitree-go2.webp', 'the picture of its model');
  ok(go2.exact && go2.alt === 'Unitree Go2', 'named as the robot is');
  const l2 = rc.rcPicture({family: 'quadruped', product: {vendor: '智身科技', model: '钢镚 L2'}});
  ok(l2.src === '/robot_images/type-quadruped.svg' && !l2.exact, 'a listed model without a picture: the drawing of its body type');
  same(rc.rcPicture({family: 'wheeled', product: {vendor: '智身科技', model: '银毅 NE01'}}).src, '/robot_images/type-humanoid.svg', 'the body type of the list counts before the chosen one');
  same(rc.rcPicture({family: 'manipulator', product: {vendor: 'Acme', model: 'A1'}}).src, '/robot_images/type-manipulator.svg', 'a robot that is not listed: by its body type');
  same(rc.rcPicture({family: 'manipulator'}).alt, '机械臂', 'without a brand and model it is named by its body type');
  same(rc.rcPicture({family: 'some-added-type'}).src, '/robot_images/type-other.svg', 'a body type without a drawing');
  same(rc.rcPicture({family: 'quadruped', product: {vendor: 'neuxmind-sim', model: 'sim-quadruped'}}).src, '/robot_images/type-quadruped.svg', 'a simulated robot');
  same(rc.rcPicture(null).src, '/robot_images/type-other.svg', 'no robot');
}

// ---- the brand and the model: one control each, chosen from its list or typed (issue #61)
{
  same(rc.rmInputs, rc.rcInputs, 'the forms of issue #48 draw these controls');
  const count = (html, piece) => html.split(piece).length - 1;
  const empty = rc.rcInputs(null, true);
  ok(empty.includes('data-rc-picker data-rc-required'), 'the picker, required');
  same(count(empty, '<input'), 2, 'two controls: a brand and a model');
  same(count(empty, '<select'), 0, 'no pick-list next to an input');
  same(count(empty, '<label'), 2, 'and no second field for a name that is not listed');
  ok(!empty.includes('其他') && !empty.includes('品牌名称') && !empty.includes('型号名称'), 'no 「其他」, no second name');
  ok(count(empty, 'name="vendor"') === 1 && count(empty, 'name="model"') === 1, 'what is sent is still vendor and model, once each');
  ok(/<label class="field">品牌<span class="combo" data-combo><input name="vendor"[^>]*role="combobox"[^>]*data-combo-source="rcVendorChoices"[^>]*data-rc="vendor"[^>]* required>/.test(empty),
     'the brand: a list that can be typed in, required');
  ok(/<label class="field">型号<span class="combo" data-combo><input name="model"[^>]*data-combo-source="rcModelChoices"[^>]*data-rc="model"[^>]* required>/.test(empty), 'the model likewise');
  ok(!/<input[^>]*disabled/.test(empty), 'the model can be typed before a brand is chosen');
  const optional = rc.rcInputs(null, false);
  ok(!optional.includes('required') && optional.includes('设备上报时可留空'), 'where the device may report, nothing is required');
  const saved = rc.rcInputs({vendor: 'unitree', model: 'go2'}, true);
  ok(/name="vendor"[^>]*value="unitree"/.test(saved) && /name="model"[^>]*value="go2"/.test(saved), 'a saved brand and model are shown, and sent, as they are written');
  const typed = rc.rcInputs({vendor: 'Beta <b>', model: 'B"2'}, true);
  ok(typed.includes('value="Beta &lt;b&gt;"') && typed.includes('value="B&quot;2"') && typed.includes('data-rc-was="Beta &lt;b&gt;"'), 'written as text');
  const library = rc.rcInputs(null, true, {vendor: 'mlVendorChoices', model: 'rcModelChoices', owned: 'mlOwned'});
  ok(/data-combo-source="mlVendorChoices"/.test(library) && library.includes('data-rc-owned="mlOwned"'), 'another page can offer its own brands, and say whose come first');
  ok(!empty.includes('data-rc-owned'), 'the forms of the robots say nothing: the account\'s robots come first');

  // the brands to choose from
  const values = options => options.map(o => o.value);
  const brands = rc.rcVendorOptions([]);
  ok(values(brands).includes('智身科技') && values(brands).includes('宇树科技'), 'the brands of the list');
  ok(!values(brands).includes('neuxmind-sim'), 'not the simulated brand');
  ok(brands.find(o => o.value === '宇树科技').keys.includes('unitree'), 'with their other spellings, so typing "unitree" finds 宇树科技');
  ok(brands.every(o => !o.note), 'nothing is marked while the account has no robot');
  const robots = [{product: {vendor: 'neuxmind-sim', model: 'sim-quadruped'}}, {product: {vendor: 'Beta <b>', model: 'B2'}}, {product: {vendor: 'beta <B>', model: 'B3'}},
                  {product: {vendor: 'unitree', model: 'go2'}}, {product: {vendor: '宇树科技', model: 'X99'}}, {product: null}, {}];
  const mine = rc.rcVendorOptions(robots);
  same(values(mine).slice(0, 4).join(), 'neuxmind-sim,Beta <b>,宇树科技,智身科技', 'the brands of the account\'s own robots come first, each once, a listed one by its name of the list');
  same(mine.slice(0, 3).every(o => o.note === '已有') && !mine[3].note, true, 'and are marked');
  same(values(mine).filter(v => v === '宇树科技').length, 1, 'a listed brand is not repeated further down');
  same(mine.length, brands.length + 2, 'the rest of the list follows');

  // the models to choose from
  ok(values(rc.rcModelOptions('星玑科技', [])).includes('CS1'), '星玑科技 CS1 can be chosen');
  same(values(rc.rcModelOptions('unitree', [])).join(), values(rc.rcModelOptions('宇树科技', [])).join(), 'a brand typed in another spelling has the models of the brand it is');
  same(rc.rcModelOptions('', []).length, 0, 'no brand: nothing to choose, the model is typed');
  same(rc.rcModelOptions('某个没人知道的品牌', []).length, 0, 'a brand nobody knows: likewise');
  same(rc.rcModelOptions('neuxmind-sim', []).length, 0, 'the simulated brand is not offered to an account without such a robot');
  same(values(rc.rcModelOptions('neuxmind-sim', robots)).join(), 'sim-quadruped', 'a brand of the account with the model its robot has');
  same(values(rc.rcModelOptions('BETA<b>', robots)).join(), 'B2,B3', 'the models of the account\'s robots of a brand that is not listed');
  const unitree = rc.rcModelOptions('宇树科技', robots);
  same(values(unitree).slice(0, 3).join(), 'Go2,X99,Go1', 'the account\'s own models first (by their name of the list), also one the list does not have');
  same(values(unitree).filter(v => v === 'Go2').length, 1, 'and not repeated');
  ok(rc.rcModelOptions('智身科技', []).find(o => o.value === '钢镚 L2-U').keys.includes('L2U'), 'models carry their other spellings too');

  // the model when the brand changes
  same(rc.rcKeepsModel('宇树科技', '智身科技', 'Go2', []), false, 'a model of the old brand does not stay under another brand');
  same(rc.rcKeepsModel('宇树科技', 'unitree', 'Go2', []), true, 'the same brand in another spelling: it stays');
  same(rc.rcKeepsModel('宇树科技', '智身科技', 'X99', []), true, 'a model that was typed stays: it is the person\'s');
  same(rc.rcKeepsModel('智身科技', '宇树科技', 'g1', []), true, 'a model the new brand has too stays');
  same(rc.rcKeepsModel('', '宇树科技', 'Whatever', []), true, 'a model typed before any brand stays');
  same(rc.rcKeepsModel('宇树科技', '', '', []), true, 'no model: nothing to decide');
  same(rc.rcKeepsModel('宇树科技', '', 'Go2', []), true, 'an emptied brand box decides nothing: the model stays');
  same(rc.rcKeepsModel('Beta <b>', '宇树科技', 'B3', robots), false, 'a model of an own brand goes with it');
  same(rc.rcKeepsModel('Beta <b>', '宇树科技', 'B2', robots), true, 'unless the new brand has a model of that name (宇树科技 B2)');

  // a change in the page
  const fired = [];
  const box = (value, extra = {}) => ({value, dispatchEvent: e => fired.push(e.type), ...extra});
  const picker = (vendor, model, was, more = {}) => {
    const boxes = {'[data-rc="vendor"]': box(vendor), '[data-rc="model"]': box(model)}, el = {dataset: {rcWas: was, ...more}, querySelector: q => boxes[q], closest: () => null};
    for (const b of Object.values(boxes)) b.closest = q => (q === '[data-rc-picker]' ? el : null);
    return {el, vendor: boxes['[data-rc="vendor"]'], model: boxes['[data-rc="model"]']};
  };
  sandbox.Event = function Event(type) { this.type = type; };
  let p = picker('智身科技', 'Go2', '宇树科技');
  rc.rcChanged(p.vendor);
  same(p.model.value + '|' + p.el.dataset.rcWas + '|' + fired.join(), '|智身科技|input', 'another brand: its model is emptied, the form is told, the brand is remembered');
  p = picker('智身科技', 'My Own', '宇树科技');
  rc.rcChanged(p.vendor);
  same(p.model.value, 'My Own', 'a typed model is left');
  p = picker('宇树科技', 'Go2', '智身科技');
  rc.rcChanged(p.model);
  same(p.model.value + '|' + p.el.dataset.rcWas, 'Go2|智身科技', 'a change of the model changes nothing else');
  rc.rcChanged({closest: () => null});
  checks += 1;
  // a picker of another page: whose brands and models count is what that page says (the library's, not the robots')
  sandbox.state = {robots: []};
  sandbox.libraryOwned = () => [{product: {vendor: 'Acme Robotics', model: 'Go-7'}}];
  p = picker('宇树科技', 'Go-7', 'Acme Robotics', {rcOwned: 'libraryOwned'});
  rc.rcChanged(p.vendor);
  same(p.model.value, '', 'a model the library has under the old brand does not stay under another brand');
  p = picker('宇树科技', 'Go-7', 'Acme Robotics');
  rc.rcChanged(p.vendor);
  same(p.model.value, 'Go-7', 'in a form of the robots that model is nobody\'s: typed, and kept');
  same(rc.rcModelChoices(picker('acme robotics', '', '', {rcOwned: 'libraryOwned'}).model).map(o => o.value).join(), 'Go-7', 'and its model box offers what the library has');
  same(rc.rcPickerOwned({dataset: {rcOwned: 'constructor'}}).length, 0, 'only a function the page defined is asked');
  // what web/combo.js asks
  sandbox.state = {robots};
  same(values(rc.rcVendorChoices())[0], 'neuxmind-sim', 'the brands, with the account\'s own');
  same(values(rc.rcModelChoices(picker('beta <b>', '', '').model)).join(), 'B2,B3', 'the models of the brand in the box next to it');
  same(rc.rcModelChoices({closest: () => null}).length, 0, 'a model box on its own has no brand');
  delete sandbox.state;
}

console.log(`robot_catalog_logic: ${checks} checks passed`);
