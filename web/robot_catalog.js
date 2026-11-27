'use strict';
// Issue #53: the brands and models a robot can be chosen as, and what each one looks like.
//
// Issue #48 gave a robot a brand and model, typed as two free texts. This file
//   * holds the list (ROBOT_CATALOG): our own robots' brands, common quadrupeds and humanoids, and the
//     common robot-arm brands. Adding a brand or a model is one line here;
//   * turns the two inputs of web/robot_model.js into two pick-lists (rmInputs is replaced, the file is
//     not edited). 「其他」 in either list shows the text input, so a brand or model that is not listed
//     can still be typed. The fields that are sent stay `vendor` and `model`;
//   * answers what a robot looks like (rcPicture): the picture of its model when the list has one, a
//     drawing of its body type otherwise. Pictures are files under web/robot_images/ (SOURCES.md there
//     says where each one is from).
// A device may report its brand in another spelling ("Unitree", "unitree robotics"): `aliases` lists
// those, and names are compared without case, spaces, hyphens and underscores.

const RC_OTHER = '__other__';
const RC_IMAGES = '/robot_images/';
// A robot with wheels at the ends of its legs (轮足) is not a quadruped: it moves, and is drawn, differently.
// Issue #58: named as the body type list names them (brainos/body_types.py, the types of the profiles).
const RC_TYPE_NAMES = {quadruped: '四足机械狗', wheel_legged: '轮足机器狗', manipulator: '机械臂', humanoid: '双足机器人', wheeled: '轮式机器人', biped: '双足机器人', other: '其他'};
const RC_TYPE_PICTURES = {quadruped: 'type-quadruped.svg', wheel_legged: 'type-wheel-legged.svg', manipulator: 'type-manipulator.svg', humanoid: 'type-humanoid.svg', biped: 'type-humanoid.svg'};
// A type of this list → the id of the body type a robot of it is registered as.
const RC_BODY_TYPE = {humanoid: 'biped', wheeled: 'other'};
const rcBodyType = type => (type ? RC_BODY_TYPE[type] || type : '');
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

// ---------------------------------------------------------------- the two pick-lists
// The brands of the account's own robots that the list does not offer (typed by hand before, or the
// simulated robots' brand), each with the models those robots have: [{vendor, models: [{model}]}].
// They can be chosen like a listed brand, so a skill for a robot one already has needs no typing.
function rcOwnBrands(robots) {
  const own = [];
  for (const robot of Array.isArray(robots) ? robots : []) {
    const p = robot && robot.product;
    if (!p || typeof p.vendor !== 'string' || typeof p.model !== 'string' || !p.vendor || !p.model) continue;
    const listed = rcBrand(p.vendor);
    if (listed && !listed.hidden) continue;
    let brand = own.find(b => rcNorm(b.vendor) === rcNorm(p.vendor));
    if (!brand) own.push(brand = {vendor: p.vendor, models: [], aliases: []});
    if (!brand.models.some(m => rcNorm(m.model) === rcNorm(p.model))) brand.models.push({model: p.model, aliases: []});
  }
  return own;
}
const rcRobots = () => (typeof state === 'object' && state && state.robots) || [];
// The brand a choice of the brand list stands for: of the list, or one of the account's own.
function rcChosenBrand(vendorChoice, robots = rcRobots()) {
  if (!vendorChoice || vendorChoice === RC_OTHER) return null;
  const listed = rcBrand(vendorChoice);
  return listed && !listed.hidden ? listed : rcOwnBrands(robots).find(b => rcNorm(b.vendor) === rcNorm(vendorChoice)) || null;
}

const rcEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const rcOption = (value, text, selected) => `<option value="${rcEsc(value)}"${selected ? ' selected' : ''}>${rcEsc(text)}</option>`;

// The options of the model list for a chosen brand (a brand of the list, RC_OTHER, or '').
function rcModelOptions(vendorChoice, chosen = '', robots = rcRobots()) {
  const brand = rcChosenBrand(vendorChoice, robots);
  if (!brand) return rcOption('', vendorChoice === RC_OTHER ? '在下面填写' : '先选品牌', true);
  return (brand.models.length ? rcOption('', '请选择', !chosen) : '') +
    brand.models.map(m => rcOption(m.model, m.model, m.model === chosen)).join('') +
    rcOption(RC_OTHER, '其他型号', chosen === RC_OTHER || !brand.models.length);
}

// What the picker starts as for a saved brand and model: what each list shows, and what each input holds.
function rcStart(product, robots = rcRobots()) {
  if (!product) return {vendorChoice: '', modelChoice: '', vendor: '', model: ''};
  const brand = rcChosenBrand(product.vendor, robots);
  if (!brand) return {vendorChoice: RC_OTHER, modelChoice: '', vendor: product.vendor, model: product.model};
  const entry = brand.models.find(m => rcNamed(m, 'model', product.model));
  // A reported spelling ("unitree") is kept as it is: the list only shows which brand it is.
  return {vendorChoice: brand.vendor, modelChoice: entry ? entry.model : RC_OTHER, vendor: product.vendor, model: product.model};
}

function rcInputs(product, required, robots = rcRobots()) {
  const start = rcStart(product, robots), req = required ? ' required' : '';
  const own = rcOwnBrands(robots);
  const typedVendor = start.vendorChoice === RC_OTHER, typedModel = typedVendor || start.modelChoice === RC_OTHER;
  const label = (text, body, cls = '', hidden = false) => `<label class="field ${cls}"${hidden ? ' hidden' : ''}>${text}${body}</label>`;
  const input = (name, value, shown, hint) => `<input name="${name}" maxlength="${RC_MAX}" value="${rcEsc(value)}" placeholder="${hint}" autocomplete="off"${shown ? req : ''}>`;
  return `<div class="robot-model-inputs rc-picker" data-rc-picker${required ? ' data-rc-required' : ''}>` +
    label('品牌', `<select data-rc="vendor" aria-label="品牌"${req}>${rcOption('', required ? '请选择' : '不选（由设备上报）', !start.vendorChoice)}` +
      [...own, ...rcBrands()].map(b => rcOption(b.vendor, b.vendor, b.vendor === start.vendorChoice)).join('') + rcOption(RC_OTHER, '其他品牌', typedVendor) + '</select>') +
    label('型号', `<select data-rc="model" aria-label="型号"${start.vendorChoice && !typedVendor ? req : ' disabled'}>` +
      rcModelOptions(start.vendorChoice, start.modelChoice, robots) + '</select>') +
    label('品牌名称', input('vendor', start.vendor, typedVendor, '填写品牌'), 'rc-typed', !typedVendor) +
    label('型号名称', input('model', start.model, typedModel, '填写型号'), 'rc-typed', !typedModel) + '</div>';
}

// A list of the picker changed: the inputs that are sent follow it.
function rcChanged(selectEl) {
  const picker = selectEl.closest('[data-rc-picker]');
  if (!picker) return;
  const required = picker.hasAttribute('data-rc-required');
  const vendorList = picker.querySelector('[data-rc="vendor"]'), modelList = picker.querySelector('[data-rc="model"]');
  const vendorInput = picker.querySelector('[name="vendor"]'), modelInput = picker.querySelector('[name="model"]');
  const show = (inputEl, shown) => { inputEl.closest('label').hidden = !shown; inputEl.required = shown && required; };
  if (selectEl === vendorList) {
    const choice = vendorList.value, typed = choice === RC_OTHER;
    modelList.innerHTML = rcModelOptions(choice);
    modelList.disabled = !choice || typed;
    modelList.required = required && !modelList.disabled;
    vendorInput.value = typed ? '' : choice;
    show(vendorInput, typed);
  }
  const modelTyped = vendorList.value === RC_OTHER || modelList.value === RC_OTHER;
  if (!modelTyped || selectEl === vendorList || selectEl === modelList) modelInput.value = modelTyped ? '' : modelList.value;
  show(modelInput, modelTyped);
  if (modelTyped && selectEl === modelList) modelInput.focus();
  if (vendorList.value === RC_OTHER && selectEl === vendorList) vendorInput.focus();
  rcFollowType(picker);
}

// ---------------------------------------------------------------- issue #58: the body type follows the model
// The body type (an id of the body type list) of a model chosen in the two lists; '' when the model is typed
// by hand, is one of the account's own, or nothing is chosen.
function rcTypeFor(vendorChoice, modelChoice) {
  const brand = vendorChoice && vendorChoice !== RC_OTHER ? rcBrand(vendorChoice) : null;
  const entry = brand && modelChoice && modelChoice !== RC_OTHER ? brand.models.find(m => m.model === modelChoice) : null;
  return entry ? rcBodyType(entry.type) : '';
}

// In a form that adds a robot (it has the body type list next to the picker): a listed model is of one
// type, so choosing the model chooses that type, as long as the person has not chosen a type themselves
// (the list is still empty, or holds what an earlier model put there). The type is the person's to decide.
function rcFollowType(picker) {
  const formEl = picker.closest('form'), list = formEl && formEl.querySelector('select[name="family"]');
  if (!list) return;
  const type = rcTypeFor(picker.querySelector('[data-rc="vendor"]').value, picker.querySelector('[data-rc="model"]').value);
  if (!type || list.value === type || ![...list.options].some(o => o.value === type)) return;
  if (list.value && list.value !== list.dataset.rcFollowed) return;
  list.value = list.dataset.rcFollowed = type;
  list.dispatchEvent(new Event('change', {bubbles: true}));
}

// web/robot_model.js draws its forms with rmInputs: replaced once every script has loaded, so the place of
// this file in index.html does not matter.
function rcInstall() { if (typeof rmInputs === 'function') rmInputs = rcInputs; }
if (typeof document === 'undefined') rcInstall();
else {
  // Deferred scripts run while the document is "interactive", before the later ones exist: wait for all of them.
  if (document.readyState === 'complete') rcInstall();
  else document.addEventListener('DOMContentLoaded', rcInstall);
  document.addEventListener('change', e => { if (e.target.matches?.('[data-rc-picker] select[data-rc]')) rcChanged(e.target); });
}
