// Pure-logic checks of web/robot_catalog.js (issue #53): the list of brands and models, the two
// pick-lists that replace the two inputs of web/robot_model.js, and what a robot looks like.
//   node tests/robot_catalog_logic.cjs
const {readFileSync, existsSync, readdirSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {rmInputs: () => 'the two inputs of issue #48'};
vm.createContext(sandbox);
const rc = vm.runInContext(readFileSync('web/robot_catalog.js', 'utf8') +
  '\n;({ROBOT_CATALOG, RC_OTHER, rcBrand, rcFind, rcBrands, rcPicture, rcInputs, rcModelOptions, rcStart, rcOwnBrands, rmInputs})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };

// ---- the list
{
  const brands = rc.rcBrands().map(b => b.vendor);
  for (const brand of ['智身科技', '星玑', '宇树科技', 'Universal Robots', 'Franka Robotics', 'KUKA', 'ABB', 'FANUC', 'Kinova', 'UFACTORY', '越疆', '节卡', '遨博', '睿尔曼', '松灵机器人']) {
    ok(brands.includes(brand), `${brand} can be chosen`);
  }
  same(brands.indexOf('智身科技'), 0, 'our own robots come first');
  ok(!brands.includes('neuxmind-sim'), 'the brand of the simulated robots is not offered to a person');
  const zs = rc.rcBrand('智身科技').models.map(m => m.model);
  for (const model of ['钢镚 L1', '钢镚 L2', '钢镚 L2-W', '钢镚 L2U', '铜锤 M1']) ok(zs.includes(model), `智身科技 ${model} can be chosen`);
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

// ---- the two pick-lists
{
  same(rc.rmInputs, rc.rcInputs, 'the forms of issue #48 draw the pick-lists');
  const count = (html, piece) => html.split(piece).length - 1;
  const empty = rc.rcInputs(null, true);
  ok(empty.includes('data-rc-picker data-rc-required') && count(empty, '<select') === 2, 'two lists');
  ok(count(empty, 'name="vendor"') === 1 && count(empty, 'name="model"') === 1, 'what is sent is still vendor and model, once each');
  ok(/<select data-rc="vendor" aria-label="品牌" required>/.test(empty), 'a required brand is required of the list');
  ok(empty.includes('<option value="智身科技">智身科技</option>') && empty.includes(`<option value="${rc.RC_OTHER}">其他品牌</option>`), 'the brands, and a way to type another');
  ok(!empty.includes('neuxmind-sim'), 'not the simulated brand');
  ok(/<label class="field rc-typed" hidden>品牌名称<input name="vendor"[^>]*value=""[^>]*>/.test(empty) && !/name="vendor"[^>]*required/.test(empty),
     'the input of a brand is out of sight, and not required while it is');
  ok(/<select data-rc="model" aria-label="型号" disabled>/.test(empty), 'no model before a brand');
  const optional = rc.rcInputs(null, false);
  ok(!optional.includes('required') && optional.includes('不选（由设备上报）'), 'where the device may report, nothing is required');
  // A saved brand and model that are listed.
  const listed = rc.rcInputs({vendor: '宇树科技', model: 'Go2'}, true);
  ok(listed.includes('<option value="宇树科技" selected>') && listed.includes('<option value="Go2" selected>'), 'both lists show what is saved');
  ok(/name="vendor"[^>]*value="宇树科技"/.test(listed) && /name="model"[^>]*value="Go2"/.test(listed), 'and the inputs hold it');
  // A reported spelling is kept as it is.
  const reported = rc.rcInputs({vendor: 'unitree', model: 'go2'}, true);
  ok(reported.includes('<option value="宇树科技" selected>') && /name="vendor"[^>]*value="unitree"/.test(reported) && /name="model"[^>]*value="go2"/.test(reported),
     'a brand in another spelling is shown as the brand it is, and sent as it was');
  // A listed brand with a model that is not.
  const other = rc.rcInputs({vendor: '宇树科技', model: 'X99'}, true);
  ok(other.includes(`<option value="${rc.RC_OTHER}" selected>其他型号</option>`) && /<label class="field rc-typed">型号名称<input name="model"[^>]*value="X99"[^>]*required>/.test(other),
     'a model that is not listed is typed, in sight');
  // A brand that is not listed.
  const typed = rc.rcInputs({vendor: 'Beta <b>', model: 'B"2'}, true);
  ok(typed.includes(`<option value="${rc.RC_OTHER}" selected>其他品牌</option>`) && typed.includes('value="Beta &lt;b&gt;"') && typed.includes('value="B&quot;2"'),
     'a brand that is not listed is typed, written as text');
  ok(!/<label class="field rc-typed" hidden>/.test(typed), 'both inputs are in sight then');
  // A brand without models yet: its model is typed.
  ok(rc.rcModelOptions('星玑').includes('<option value="CS1">CS1</option>'), '星玑 CS1 can be chosen');
  ok(rc.rcModelOptions('某个没有型号的品牌', '', [{product: {vendor: '某个没有型号的品牌', model: 'M'}}]).includes('<option value="M">M</option>'), 'a brand of the account with its one model');
  ok(rc.rcModelOptions('智身科技').startsWith('<option value="" selected>请选择</option>'), 'a brand with models starts unchosen');
  same(rc.rcStart({vendor: 'neuxmind-sim', model: 'sim-quadruped'}).vendorChoice, rc.RC_OTHER, 'the simulated brand is shown as typed while the account has no such robot');
  // The brands of the account's own robots that the list does not offer can be chosen like a listed one.
  const robots = [{product: {vendor: 'neuxmind-sim', model: 'sim-quadruped'}}, {product: {vendor: 'Beta <b>', model: 'B2'}}, {product: {vendor: 'beta <B>', model: 'B3'}},
                  {product: {vendor: '宇树科技', model: 'Go2'}}, {product: null}, {}];
  const own = rc.rcOwnBrands(robots);
  same(own.map(b => b.vendor + ':' + b.models.map(m => m.model).join('/')).join(), 'neuxmind-sim:sim-quadruped,Beta <b>:B2/B3', 'each once, with the models the robots have; listed brands are not repeated');
  const withOwn = rc.rcInputs({vendor: 'neuxmind-sim', model: 'sim-quadruped'}, true, robots);
  ok(withOwn.includes('<option value="neuxmind-sim" selected>neuxmind-sim</option>') && withOwn.includes('<option value="sim-quadruped" selected>sim-quadruped</option>'),
     'a robot one already has is chosen, not typed');
  ok(withOwn.indexOf('value="neuxmind-sim"') < withOwn.indexOf('value="智身科技"'), 'the account\'s own brands come first');
  ok(rc.rcInputs(null, true, robots).includes('<option value="Beta &lt;b&gt;">Beta &lt;b&gt;</option>'), 'written as text');
  ok(rc.rcModelOptions('Beta <b>', '', robots).includes('<option value="B3">B3</option>') && rc.rcModelOptions('Beta <b>', '', robots).includes('其他型号'), 'its models, and a way to type another');
}

console.log(`robot_catalog_logic: ${checks} checks passed`);
