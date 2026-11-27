// Pure-logic checks of web/sim_profiles.js (issue #54): what 「添加仿真机器人」 offers and opens on.
//   node tests/sim_profiles_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const sp = vm.runInContext(readFileSync('web/sim_profiles.js', 'utf8') +
  '\n;({spCatalog, spOfType, spVendors, spModels, spStart, spFollow, spPicture, spTypeTiles, spVendorOptions, spModelOptions, spPreview, spBody, spCanon})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(JSON.stringify(actual), JSON.stringify(expected), what); checks += 1; };   // values come from another realm

const TYPES = [{id: 'manipulator', name: '机械臂'}, {id: 'wheel_legged', name: '轮足机器狗'}, {id: 'quadruped', name: '四足机械狗'},
  {id: 'biped', name: '双足机器人'}, {id: 'other', name: '其他'}];
const row = (id, vendor, model, type, more = {}) => ({id, vendor, model, type, image: '', builtin: false, available: true, problem: '', ...more});
const catalog = sp.spCatalog({types: TYPES, profiles: [
  row('unitree-go2', '宇树科技', 'Go2', 'quadruped', {image: 'unitree-go2'}),
  row('unitree-g1', '宇树科技', 'G1', 'biped'),
  row('unitree-z1', '宇树科技', 'Z1', 'manipulator'),
  row('deeprobotics-lite3', '云深处科技', '绝影 Lite3', 'quadruped'),
  row('zsibot-l2u', '智身科技', '钢镚 L2-U', 'wheel_legged', {available: false, problem: '这个安装包里没有它的机器人模型'}),
  row('xingji-cs1', '星玑科技', 'CS1', 'quadruped', {available: false, problem: '这个安装包里没有它的机器人模型'}),
  row('neuxmind-quadruped', 'NeuxMind 示例', '通用四足', 'quadruped', {builtin: true}),
]});

// ---- what a type and a brand offer
same(sp.spVendors(catalog, 'quadruped'), ['宇树科技', '云深处科技', '星玑科技', 'NeuxMind 示例'], 'the brands of a type, in the order of the list');
same(sp.spVendors(catalog, 'other'), [], 'a type without models has no brand');
same(sp.spModels(catalog, 'quadruped', '宇树科技').map(p => p.id), ['unitree-go2'], 'only the models of that type');

// ---- what the dialog opens on
same(sp.spStart(catalog), {type: 'quadruped', vendor: '宇树科技', profile: 'unitree-go2'}, 'it opens on the first dog that can be added');
const onlyArm = sp.spCatalog({types: TYPES, profiles: catalog.profiles.filter(p => p.type === 'manipulator' || !p.available)});
same(sp.spStart(onlyArm), {type: 'manipulator', vendor: '宇树科技', profile: 'unitree-z1'}, 'with no dog to add, on the first model that can be');
same(sp.spStart(sp.spCatalog({types: TYPES, profiles: []})), {type: 'manipulator', vendor: '', profile: ''}, 'with nothing at all, on the first type');
same(sp.spStart(sp.spCatalog(undefined)), {type: '', vendor: '', profile: ''}, 'a state without the list does not break it');

// ---- following a change
same(sp.spFollow(catalog, 'biped', ''), {type: 'biped', vendor: '宇树科技', profile: 'unitree-g1'}, 'a type brings its first brand and model');
same(sp.spFollow(catalog, 'quadruped', '云深处科技'), {type: 'quadruped', vendor: '云深处科技', profile: 'deeprobotics-lite3'}, 'a brand brings its first model');
same(sp.spFollow(catalog, 'quadruped', '星玑科技'), {type: 'quadruped', vendor: '星玑科技', profile: ''}, 'a brand whose model cannot be added brings none');
same(sp.spFollow(catalog, 'wheel_legged', ''), {type: 'wheel_legged', vendor: '智身科技', profile: ''}, 'a type with only such a brand shows the brand and no model');
same(sp.spFollow(catalog, 'other', '宇树科技'), {type: 'other', vendor: '', profile: ''}, 'a type without models brings nothing');

// ---- what is drawn
const tiles = sp.spTypeTiles(catalog, 'quadruped');
same((tiles.match(/name="sim_type"/g) || []).length, 5, 'five types');
ok(tiles.indexOf('机械臂') < tiles.indexOf('轮足机器狗') && tiles.indexOf('轮足机器狗') < tiles.indexOf('四足机械狗') &&
  tiles.indexOf('四足机械狗') < tiles.indexOf('双足机器人') && tiles.indexOf('双足机器人') < tiles.indexOf('其他'), 'in the order of the issue');
ok(tiles.includes('value="quadruped" checked') && !tiles.includes('value="biped" checked'), 'the chosen one is checked');
ok(/四足机械狗<\/b><small>3 款/.test(tiles) && /轮足机器狗<\/b><small>暂无/.test(tiles) && /其他<\/b><small>暂无/.test(tiles), 'each says how many models can be added');
const models = sp.spModelOptions(catalog, 'quadruped', '星玑科技', '');
ok(models.includes('<option value="xingji-cs1" disabled>CS1（这个安装包里没有它的机器人模型）</option>'), 'a model that cannot be added is shown, with the reason, and cannot be chosen');
ok(models.includes('<option value="" selected>'), 'and nothing is chosen for it');
ok(sp.spModelOptions(catalog, 'quadruped', '宇树科技', 'unitree-go2').includes('<option value="unitree-go2" selected>Go2</option>'), 'the chosen model is selected');
ok(sp.spVendorOptions(catalog, 'quadruped', '云深处科技').includes('<option value="云深处科技" selected>'), 'the chosen brand is selected');
same(sp.spPicture(catalog.profiles[0]), '/robot_images/unitree-go2.webp', 'a model with a picture shows it');
same(sp.spPicture(catalog.profiles[1]), '/robot_images/type-humanoid.svg', 'one without shows the drawing of its type');
ok(sp.spPreview(catalog, {type: 'quadruped', vendor: '宇树科技', profile: 'unitree-go2'}).includes('MuJoCo'), 'a model from a URDF says how it is simulated');
ok(sp.spPreview(catalog, {type: 'quadruped', vendor: 'NeuxMind 示例', profile: 'neuxmind-quadruped'}).includes('示例机器人'), 'the example robot says it is one');
ok(sp.spPreview(catalog, {type: 'other', vendor: '', profile: ''}).includes('这个类型暂时没有可以仿真的型号'), 'a type without models says so');
ok(sp.spPreview(catalog, {type: 'wheel_legged', vendor: '智身科技', profile: ''}).includes('不能添加'), 'a brand whose models cannot be added says so');
{ // issue #65: the Microduck is listed under 「其他」; it is not built from a URDF and its preview does not say so
  const withDuck = sp.spCatalog({types: TYPES, profiles: [...catalog.profiles, row('microduck-adp102', 'Microduck', 'AD-P102', 'other', {image: 'microduck-adp102', kit: true})]});
  same(sp.spVendors(withDuck, 'other'), ['Microduck'], 'the type 其他 has the duck\'s brand');
  same(sp.spFollow(withDuck, 'other', ''), {type: 'other', vendor: 'Microduck', profile: 'microduck-adp102'}, 'choosing 其他 brings the duck');
  ok(/其他<\/b><small>1 款/.test(sp.spTypeTiles(withDuck, 'other')), 'the tile counts it');
  const preview = sp.spPreview(withDuck, {type: 'other', vendor: 'Microduck', profile: 'microduck-adp102'});
  ok(preview.includes('/robot_images/microduck-adp102.webp') && preview.includes('Microduck · AD-P102'), 'its picture and name are shown');
  ok(!preview.includes('URDF') && preview.includes('技能模型'), 'and what it says is about the duck: ' + preview);
  // the preview is one box of one size whatever it shows (web/sim_profiles.css): the dialog does not change size with the type
  const css = readFileSync('web/sim_profiles.css', 'utf8');
  const box = /\.sp-preview\{([^}]*)\}/.exec(css)[1], picture = /\.sp-preview img\{([^}]*)\}/.exec(css)[1];
  const px = (rule, name) => Number((new RegExp('(?:^|;)' + name + ':(\\d+)px').exec(rule) || [])[1]);
  const pad = Number((/(?:^|;)padding:(\d+)px/.exec(box) || [])[1]);
  ok(px(picture, 'height') > 0 && px(box, 'min-height') === px(picture, 'height') + 2 * pad + 2, `without a picture the box is as high as with one (${px(box, 'min-height')} / ${px(picture, 'height')} + 2×${pad} + border)`);
}
const hostile = sp.spCatalog({types: [{id: 'quadruped', name: '<b>x</b>'}], profiles: [row('p"1', '<img src=x>', '</option><script>', 'quadruped')]});
const drawn = sp.spBody(hostile, sp.spStart(hostile));
ok(!drawn.includes('<img src=x>') && !drawn.includes('<script>') && !drawn.includes('<b>x</b>') && drawn.includes('p&quot;1'), 'names are text, not markup');
ok(sp.spBody(catalog, sp.spStart(catalog)).includes('name="profile_id" aria-label="型号" required'), 'the model is what is sent, and it is required');

// ---- another spelling of a known brand and model
const table = {vendors: {zs: '智身科技', '智身科技': '智身科技', '星玑': '星玑科技'}, models: {'智身科技': {l2u: '钢镚 L2-U', '钢镚l2u': '钢镚 L2-U'}}};
same(sp.spCanon({vendor: 'ZS', model: 'L2U'}, table), {vendor: '智身科技', model: '钢镚 L2-U'}, 'what the device reports is the brand and model of the profile');
same(sp.spCanon({vendor: '智身科技', model: '钢镚 L2-U', source: 'device'}, table), {vendor: '智身科技', model: '钢镚 L2-U', source: 'device'}, 'the profile spelling stays, with what else the record holds');
same(sp.spCanon({vendor: 'zs', model: 'L2-W'}, table), {vendor: '智身科技', model: 'L2-W'}, 'a model that is not listed keeps its name');
same(sp.spCanon({vendor: 'Acme', model: 'toString'}, table), {vendor: 'Acme', model: 'toString'}, 'an unknown brand is left alone');
same(sp.spCanon({vendor: 'constructor', model: 'x'}, table), {vendor: 'constructor', model: 'x'}, 'a name of the table object itself is not a brand');
same(sp.spCanon(null, table), null, 'no product stays none');
same(sp.spCanon({vendor: 'zs', model: 'L2U'}, undefined), {vendor: 'zs', model: 'L2U'}, 'without the table nothing changes');
{
  // the two pages that compare brands and models use it
  const pages = {};
  vm.createContext(pages);
  // Only the lines that compare: the rest of those two files draws pages and needs the whole app around it.
  const lines = (file, names) => readFileSync(file, 'utf8').split('\n').filter(line => names.some(n => line.startsWith('const ' + n + ' ='))).join('\n');
  const fn = (file, name) => { const text = readFileSync(file, 'utf8'), at = text.indexOf('function ' + name + '('); return text.slice(at, text.indexOf('\n}\n', at) + 3); };
  vm.runInContext(readFileSync('web/sim_profiles.js', 'utf8') + '\nvar state = ' + JSON.stringify({product_aliases: table}) + ';\nconst RM_UNSET = "";\n' +
    lines('web/robot_model.js', ['rmCanon', 'rmKey', 'rmSame']) + '\n' + lines('web/task_apps.js', ['taCanon']) + '\n' +
    fn('web/robot_model.js', 'rmProduct').replace('rmProduct', 'taProduct') + fn('web/task_apps.js', 'taKind') + fn('web/task_apps.js', 'taRobotKind') +
    '\n;this.out = {rmSame, taKind, taRobotKind};', pages);
  const real = {vendor: 'zs', model: 'L2U'}, sim = {vendor: '智身科技', model: '钢镚 L2-U'};
  ok(pages.out.rmSame(real, sim), 'the real robot and the simulated one are the same brand and model on the page');
  ok(!pages.out.rmSame(real, {vendor: '星玑科技', model: 'CS1'}), 'and another brand is another');
  same(pages.out.taKind({product: real}), pages.out.taRobotKind({id: 'r1', product: sim}), 'an application of the real robot is of the simulated robot\'s kind');
}

console.log(`sim_profiles_logic: ${checks} checks passed`);
