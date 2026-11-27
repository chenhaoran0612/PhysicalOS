'use strict';
// Issue #53: the brands and models a robot can be chosen as, and what each one looks like.
//
// Issue #48 gave a robot a brand and model, typed as two free texts. This file
//   * holds the list (ROBOT_CATALOG): our own robots' brands, common quadrupeds and humanoids, and the
//     common robot-arm brands. Adding a brand or a model is one line here;
//   * draws the brand and the model of a form (rmInputs of web/robot_model.js is replaced, the file is
//     not edited). Since issue #61 each is one control: a list to choose from that can also be typed in
//     (web/combo.js), so a brand or model that is not listed is simply typed. The fields that are sent
//     stay `vendor` and `model`;
//   * answers what a robot looks like (rcPicture): the picture of its model when the list has one, a
//     drawing of its body type otherwise. Pictures are files under web/robot_images/ (SOURCES.md there
//     says where each one is from).
// A device may report its brand in another spelling ("Unitree", "unitree robotics"): `aliases` lists
// those, and names are compared without case, spaces, hyphens and underscores.

const RC_IMAGES = '/robot_images/';
// A robot with wheels at the ends of its legs (轮足) is not a quadruped: it moves, and is drawn, differently.
const RC_TYPE_NAMES = {quadruped: '四足机器人', wheel_legged: '轮足机器人', manipulator: '机械臂', humanoid: '人形机器人', wheeled: '轮式机器人'};
const RC_TYPE_PICTURES = {quadruped: 'type-quadruped.svg', wheel_legged: 'type-wheel-legged.svg', manipulator: 'type-manipulator.svg', humanoid: 'type-humanoid.svg'};
const RC_TYPE_DEFAULT = 'type-other.svg';
const RC_MAX = 100;

// m(model, type, picture file without extension, other spellings)
const rcM = (model, type, image = '', aliases = []) => ({model, type, image: image ? image + '.webp' : '', aliases});
const ROBOT_CATALOG = [
  {vendor: '智身科技', aliases: ['智身', 'zs', 'zsibot', 'genisom', 'genisom ai'], models: [
    rcM('钢镚 L1', 'quadruped', '', ['L1', 'ZSL-1']), rcM('钢镚 L1-W', 'wheel_legged', '', ['L1-W', 'zsl1_w']),
    rcM('钢镚 L1 Maker', 'quadruped', '', ['L1 Maker']), rcM('钢镚 L2', 'quadruped', '', ['L2']),
    rcM('钢镚 L2-W', 'wheel_legged', '', ['L2-W']), rcM('钢镚 L2-U', 'wheel_legged', 'zsibot-l2u', ['L2U', 'L2 Ultra', 'L2W Ultra']),
    rcM('铜锤 M1', 'quadruped', '', ['M1']), rcM('铅球 SP1', 'wheel_legged', '', ['SP1']),
    rcM('银毅 NE01', 'humanoid', '', ['NE01']), rcM('破风者 RX', 'humanoid', '', ['RX'])]},
  {vendor: '星玑科技', aliases: ['星玑', 'xingji', 'xinji'], models: [rcM('CS1', 'quadruped', 'xingji-cs1', ['CS-DOG1'])]},
  {vendor: '宇树科技', aliases: ['宇树', 'unitree', 'unitree robotics'], models: [
    rcM('Go2', 'quadruped', 'unitree-go2'), rcM('Go1', 'quadruped', 'unitree-go1'), rcM('A1', 'quadruped', 'unitree-a1'),
    rcM('B2', 'quadruped'), rcM('Go2-W', 'wheel_legged'), rcM('B2-W', 'wheel_legged'),
    rcM('G1', 'humanoid', 'unitree-g1'), rcM('H1', 'humanoid', 'unitree-h1'), rcM('H1-2', 'humanoid'), rcM('R1', 'humanoid'), rcM('Z1', 'manipulator', 'unitree-z1')]},
  {vendor: '云深处科技', aliases: ['云深处', 'deep robotics'], models: [
    rcM('绝影 Lite3', 'quadruped', 'deeprobotics-lite3', ['Lite3']), rcM('绝影 X30', 'quadruped', '', ['X30']), rcM('山猫 M20', 'wheel_legged', 'deeprobotics-m20', ['M20'])]},
  {vendor: 'Boston Dynamics', aliases: ['波士顿动力'], models: [rcM('Spot', 'quadruped', 'boston-dynamics-spot')]},
  {vendor: 'ANYbotics', aliases: [], models: [rcM('ANYmal C', 'quadruped', 'anybotics-anymal-c'), rcM('ANYmal D', 'quadruped')]},
  {vendor: 'Universal Robots', aliases: ['优傲', 'ur'], models: [
    rcM('UR3e', 'manipulator'), rcM('UR5e', 'manipulator', 'universal-robots-ur5e'), rcM('UR10e', 'manipulator', 'universal-robots-ur10e'),
    rcM('UR16e', 'manipulator'), rcM('UR20', 'manipulator')]},
  {vendor: 'Franka Robotics', aliases: ['franka', 'franka emika'], models: [
    rcM('Panda', 'manipulator', 'franka-panda', ['Franka Emika Panda']), rcM('FR3', 'manipulator', 'franka-panda', ['Franka Research 3'])]},
  {vendor: 'KUKA', aliases: ['库卡'], models: [
    rcM('LBR iiwa 7 R800', 'manipulator', 'kuka-iiwa-14', ['iiwa 7']), rcM('LBR iiwa 14 R820', 'manipulator', 'kuka-iiwa-14', ['iiwa 14']),
    rcM('LBR iisy', 'manipulator'), rcM('KR AGILUS', 'manipulator')]},
  {vendor: 'ABB', aliases: [], models: [
    rcM('IRB 120', 'manipulator'), rcM('IRB 1200', 'manipulator'), rcM('GoFa CRB 15000', 'manipulator', '', ['GoFa']), rcM('YuMi IRB 14000', 'manipulator', '', ['YuMi'])]},
  {vendor: 'FANUC', aliases: ['发那科'], models: [
    rcM('CRX-5iA', 'manipulator', 'fanuc-crx-5ia'), rcM('CRX-10iA', 'manipulator', 'fanuc-crx-5ia'), rcM('LR Mate 200iD', 'manipulator')]},
  {vendor: 'Yaskawa', aliases: ['安川', 'motoman'], models: [rcM('GP7', 'manipulator'), rcM('HC10', 'manipulator')]},
  {vendor: 'Kinova', aliases: [], models: [rcM('Gen3', 'manipulator', 'kinova-gen3'), rcM('Gen3 lite', 'manipulator')]},
  {vendor: 'UFACTORY', aliases: ['ufactory xarm', '众为创造'], models: [
    rcM('xArm 6', 'manipulator', 'ufactory-xarm-7'), rcM('xArm 7', 'manipulator', 'ufactory-xarm-7'), rcM('Lite 6', 'manipulator', 'ufactory-lite-6'),
    rcM('850', 'manipulator')]},
  {vendor: 'Flexiv', aliases: ['非夕', '非夕科技'], models: [rcM('Rizon 4', 'manipulator', 'flexiv-rizon-4'), rcM('Rizon 4s', 'manipulator', 'flexiv-rizon-4'), rcM('Rizon 10', 'manipulator')]},
  {vendor: '松灵机器人', aliases: ['松灵', 'agilex', 'agilex robotics'], models: [rcM('PiPER', 'manipulator', 'agilex-piper')]},
  {vendor: '方舟无限', aliases: ['arx', 'arx robotics'], models: [rcM('L5', 'manipulator', 'arx-l5'), rcM('R5', 'manipulator'), rcM('X5', 'manipulator')]},
  {vendor: '睿尔曼', aliases: ['realman', 'realman robotics'], models: [rcM('RM65-B', 'manipulator', '', ['RM65']), rcM('RM75-B', 'manipulator', '', ['RM75']), rcM('ECO65', 'manipulator')]},
  {vendor: '越疆', aliases: ['dobot', '越疆科技'], models: [rcM('CR5', 'manipulator'), rcM('Nova 5', 'manipulator'), rcM('MG400', 'manipulator'), rcM('Magician E6', 'manipulator')]},
  {vendor: '节卡', aliases: ['jaka', '节卡机器人'], models: [rcM('Zu 5', 'manipulator'), rcM('Zu 7', 'manipulator'), rcM('Zu 12', 'manipulator'), rcM('MiniCobo', 'manipulator')]},
  {vendor: '遨博', aliases: ['aubo', '遨博智能'], models: [rcM('i5', 'manipulator'), rcM('i10', 'manipulator'), rcM('i16', 'manipulator')]},
  {vendor: '艾利特', aliases: ['elite', 'elite robots'], models: [rcM('EC66', 'manipulator'), rcM('CS66', 'manipulator'), rcM('CS612', 'manipulator')]},
  {vendor: '珞石', aliases: ['rokae', '珞石机器人'], models: [rcM('xMate ER3', 'manipulator'), rcM('xMate ER7', 'manipulator'), rcM('xMate CR7', 'manipulator')]},
  {vendor: '大族机器人', aliases: ['大族', "han's robot", 'hans robot'], models: [rcM('Elfin 5', 'manipulator', '', ['E05']), rcM('Elfin 10', 'manipulator', '', ['E10'])]},
  {vendor: '大象机器人', aliases: ['elephant robotics'], models: [rcM('myCobot 280', 'manipulator'), rcM('myCobot 320', 'manipulator'), rcM('mechArm 270', 'manipulator')]},
  {vendor: 'Trossen Robotics', aliases: ['trossen', 'interbotix'], models: [
    rcM('ViperX 300 S', 'manipulator', 'trossen-vx300s', ['vx300s']), rcM('WidowX 250 S', 'manipulator', 'trossen-wx250s', ['wx250s'])]},
  {vendor: 'LeRobot', aliases: ['the robot studio', 'robotstudio', 'hugging face'], models: [
    rcM('SO-ARM100', 'manipulator', 'so-arm100', ['SO-100']), rcM('SO-101', 'manipulator', 'so-101')]},
  {vendor: 'Rethink Robotics', aliases: ['rethink'], models: [rcM('Sawyer', 'manipulator', 'rethink-sawyer')]},
  {vendor: '加速进化', aliases: ['booster', 'booster robotics'], models: [rcM('T1', 'humanoid', 'booster-t1'), rcM('K1', 'humanoid')]},
  {vendor: '傅利叶', aliases: ['fourier', 'fourier intelligence'], models: [rcM('N1', 'humanoid', 'fourier-n1'), rcM('GR-1', 'humanoid'), rcM('GR-2', 'humanoid')]},
  {vendor: '智元机器人', aliases: ['智元', 'agibot'], models: [rcM('A3', 'humanoid', 'agibot-a3'), rcM('远征 A2', 'humanoid', '', ['A2']), rcM('灵犀 X2', 'humanoid', '', ['X2']), rcM('精灵 G1', 'humanoid')]},
  {vendor: '优必选', aliases: ['ubtech', 'ubtech robotics'], models: [rcM('Walker S1', 'humanoid'), rcM('Walker S2', 'humanoid'), rcM('天工行者', 'humanoid', '', ['Tiangong Walker'])]},
  {vendor: '众擎机器人', aliases: ['众擎', 'engineai'], models: [rcM('SE01', 'humanoid'), rcM('PM01', 'humanoid')]},
  {vendor: '银河通用', aliases: ['galbot'], models: [rcM('Galbot G1', 'humanoid')]},
  {vendor: '乐聚机器人', aliases: ['乐聚', 'leju', 'leju robotics'], models: [rcM('夸父 KUAVO', 'humanoid', '', ['KUAVO', '夸父'])]},
  {vendor: '星动纪元', aliases: ['robotera'], models: [rcM('星动 L7', 'humanoid', '', ['L7']), rcM('星动 STAR1', 'humanoid', '', ['STAR1'])]},
  {vendor: 'Tesla', aliases: ['特斯拉'], models: [rcM('Optimus', 'humanoid')]},
  {vendor: 'Figure', aliases: ['figure ai'], models: [rcM('Figure 02', 'humanoid'), rcM('Figure 03', 'humanoid')]},
  {vendor: 'Apptronik', aliases: [], models: [rcM('Apollo', 'humanoid', 'apptronik-apollo')]},
  {vendor: 'Agility Robotics', aliases: ['agility'], models: [rcM('Digit', 'humanoid')]},
  {vendor: '1X', aliases: ['1x technologies'], models: [rcM('NEO', 'humanoid')]},
  {vendor: 'PNDbotics', aliases: ['pnd'], models: [rcM('Adam Lite', 'humanoid', 'pndbotics-adam-lite')]},
  {vendor: 'PAL Robotics', aliases: ['pal'], models: [rcM('TALOS', 'humanoid', 'pal-talos')]},
  {vendor: 'ROBOTIS', aliases: [], models: [rcM('OP3', 'humanoid', 'robotis-op3')]},
  // What the simulated robots of this application report.
  {vendor: 'neuxmind-sim', aliases: [], hidden: true, models: [rcM('sim-quadruped', 'quadruped'), rcM('sim-arm-6dof', 'manipulator')]},
];

// ---------------------------------------------------------------- pure
const rcNorm = text => String(text ?? '').toLowerCase().replace(/[\s\-_.·]+/g, '');
const rcNamed = (entry, name, text) => [entry[name], ...(entry.aliases || [])].some(n => rcNorm(n) === rcNorm(text));

// The brand of the list a typed or reported brand is, or null.
function rcBrand(vendor) {
  return rcNorm(vendor) ? ROBOT_CATALOG.find(b => rcNamed(b, 'vendor', vendor)) || null : null;
}

// The entry of the list a brand and model is: {brand, entry}; entry is null for a model that is not listed.
function rcFind(product) {
  const brand = product ? rcBrand(product.vendor) : null;
  if (!brand) return null;
  return {brand, entry: (rcNorm(product.model) && brand.models.find(m => rcNamed(m, 'model', product.model))) || null};
}

// The brands a person can choose, in the order of the list.
const rcBrands = () => ROBOT_CATALOG.filter(b => !b.hidden);

// What a robot looks like: {src, alt, exact}. `exact` says the picture is of this very model.
function rcPicture(robot) {
  const p = robot && robot.product, product = p && p.vendor && p.model ? p : null;
  const found = rcFind(product), entry = found && found.entry;
  const type = (entry && entry.type) || (robot && robot.family) || '';
  const label = product ? product.vendor + ' ' + product.model : (RC_TYPE_NAMES[type] || '机器人');
  if (entry && entry.image) return {src: RC_IMAGES + entry.image, alt: label, exact: true};
  return {src: RC_IMAGES + (RC_TYPE_PICTURES[type] || RC_TYPE_DEFAULT), alt: label, exact: false};
}

// ---------------------------------------------------------------- the picker: a brand and a model, each chosen or typed
const RC_OWN_NOTE = '已有';
const RC_SOURCES = {vendor: 'rcVendorChoices', model: 'rcModelChoices', owned: ''};
const rcRobots = () => (typeof state === 'object' && state && state.robots) || [];
const rcEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

// The brands and models of the account's own robots, as written on them: [{vendor, model}].
function rcOwned(robots) {
  return (Array.isArray(robots) ? robots : []).map(robot => robot && robot.product)
    .filter(p => p && typeof p.vendor === 'string' && typeof p.model === 'string' && p.vendor && p.model);
}
// The brand of the list a text is, leaving out the one of the simulated robots; or null.
function rcListed(vendor) {
  const brand = rcBrand(vendor);
  return brand && !brand.hidden ? brand : null;
}
// Whether two texts are one brand: the same brand of the list (in any spelling), or the same text.
function rcSameBrand(a, b) {
  const listed = rcListed(a);
  return listed ? listed === rcListed(b) : rcNorm(a) === rcNorm(b);
}
const rcHas = (options, value) => options.some(o => rcNorm(o.value) === rcNorm(value));

// The brands to choose from: those of the account's own robots first (a skill for a robot one already
// has needs no searching), then the rest of the list. [{value, keys: other spellings, note}]
function rcVendorOptions(robots = rcRobots()) {
  const own = [];
  for (const p of rcOwned(robots)) {
    const listed = rcListed(p.vendor), value = listed ? listed.vendor : p.vendor;
    if (!rcHas(own, value)) own.push({value, keys: listed ? listed.aliases : [], note: RC_OWN_NOTE});
  }
  return [...own, ...rcBrands().filter(b => !rcHas(own, b.vendor)).map(b => ({value: b.vendor, keys: b.aliases}))];
}

// The models to choose from for a brand as it stands in the box (any spelling of it): those of the
// account's own robots of that brand first, then the list's. A brand nobody knows has none.
function rcModelOptions(vendor, robots = rcRobots()) {
  if (!rcNorm(vendor)) return [];
  const listed = rcListed(vendor), models = listed ? listed.models : [];
  const own = [];
  for (const p of rcOwned(robots)) {
    if (!rcSameBrand(vendor, p.vendor)) continue;
    const entry = models.find(m => rcNamed(m, 'model', p.model)), value = entry ? entry.model : p.model;
    if (!rcHas(own, value)) own.push({value, keys: entry ? entry.aliases : [], note: RC_OWN_NOTE});
  }
  return [...own, ...models.filter(m => !rcHas(own, m.model)).map(m => ({value: m.model, keys: m.aliases}))];
}

// Whether the model in the box stays when the brand changes from `was` to `now`: a model that is one of
// the old brand's and not one of the new brand's goes; anything else was typed and is the person's.
function rcKeepsModel(was, now, model, robots = rcRobots()) {
  if (!rcNorm(model) || !rcNorm(now) || rcSameBrand(was, now)) return true;      // an emptied brand box decides nothing
  const of = vendor => rcModelOptions(vendor, robots).some(o => [o.value, ...o.keys].some(k => rcNorm(k) === rcNorm(model)));
  return !of(was) || of(now);
}

// The two controls. `sources` names the functions web/combo.js asks for the options, and `owned` the function
// that says which brands and models come first ([{product}], '' for the account's robots): another page can
// offer its own (the library: what it has materials of).
function rcInputs(product, required, sources = RC_SOURCES) {
  const p = product || {vendor: '', model: ''};
  const hint = required ? '选择或输入' : '选择或输入，设备上报时可留空';
  const one = (text, name, value) => `<label class="field">${text}` +
    comboInput({name, source: sources[name], value, label: text, placeholder: hint, required, maxlength: RC_MAX, attrs: `data-rc="${name}"`}) + '</label>';
  return `<div class="robot-model-inputs rc-picker" data-rc-picker${required ? ' data-rc-required' : ''} data-rc-was="${rcEsc(p.vendor)}"` +
    `${sources.owned ? ` data-rc-owned="${rcEsc(sources.owned)}"` : ''}>` +
    one('品牌', 'vendor', p.vendor) + one('型号', 'model', p.model) + '</div>';
}

// What web/combo.js asks: the options of a brand box, and of the model box next to it.
function rcVendorChoices() { return rcVendorOptions(); }
function rcModelChoices(input) {
  const picker = input.closest('[data-rc-picker]');
  return picker ? rcModelOptions(picker.querySelector('[data-rc="vendor"]').value, rcPickerOwned(picker)) : [];
}
// Whose brands and models a picker puts first: what its `owned` function says, else the account's robots.
function rcPickerOwned(picker) {
  const name = picker.dataset.rcOwned;
  return name && Object.prototype.hasOwnProperty.call(globalThis, name) && typeof globalThis[name] === 'function' ? globalThis[name]() : rcRobots();
}

// A box of the picker was changed (chosen from its list, or typed and left).
function rcChanged(input) {
  const picker = input.closest('[data-rc-picker]');
  if (!picker) return;
  const vendorInput = picker.querySelector('[data-rc="vendor"]'), modelInput = picker.querySelector('[data-rc="model"]');
  if (input === vendorInput) {
    if (!rcKeepsModel(picker.dataset.rcWas, vendorInput.value, modelInput.value, rcPickerOwned(picker))) {
      modelInput.value = '';
      modelInput.dispatchEvent(new Event('input', {bubbles: true}));
    }
    picker.dataset.rcWas = vendorInput.value;
  }
  // Issue #58 (the body type of the form follows the chosen model) hooks in here when it is there.
  if (typeof rcFollowType === 'function') rcFollowType(picker);
}

// web/robot_model.js draws its forms with rmInputs: replaced once every script has loaded, so the place of
// this file in index.html does not matter.
function rcInstall() { if (typeof rmInputs === 'function') rmInputs = rcInputs; }
if (typeof document === 'undefined') rcInstall();
else {
  // Deferred scripts run while the document is "interactive", before the later ones exist: wait for all of them.
  if (document.readyState === 'complete') rcInstall();
  else document.addEventListener('DOMContentLoaded', rcInstall);
  document.addEventListener('change', e => { if (e.target.matches?.('[data-rc-picker] input[data-rc]')) rcChanged(e.target); });
}
