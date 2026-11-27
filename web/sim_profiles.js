'use strict';
// Issue #54: 「添加仿真机器人」 chooses a type, a brand and a model from the built-in profiles.
//
// The profiles and which of them this installation can add come with the state (`state.sim_catalog`,
// brainos/sim_profiles.py). This file draws the dialog, replacing the one of web/sim.js (two made-up
// robots to choose from), and sends the chosen profile. Nothing else of web/sim.js changes: the robot
// that comes back is an ordinary simulated robot.

const SP_TYPE_PICTURES = {manipulator: 'type-manipulator.svg', wheel_legged: 'type-wheel-legged.svg',
  quadruped: 'type-quadruped.svg', biped: 'type-humanoid.svg', other: 'type-other.svg'};
const SP_IMAGES = '/robot_images/';
const SP_FIRST_TYPE = 'quadruped';              // what the dialog opens on when it has something to add
const SP_NO_MODEL = '这个类型暂时没有可以仿真的型号';

// ---------------------------------------------------------------- pure
const spEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

function spCatalog(source) {
  const raw = source || (typeof state === 'object' && state && state.sim_catalog) || {};
  return {types: Array.isArray(raw.types) ? raw.types : [], profiles: Array.isArray(raw.profiles) ? raw.profiles : []};
}
const spOfType = (catalog, type) => catalog.profiles.filter(p => p.type === type);
// The brands that have a model of this type, in the order of the list.
const spVendors = (catalog, type) => [...new Set(spOfType(catalog, type).map(p => p.vendor))];
const spModels = (catalog, type, vendor) => spOfType(catalog, type).filter(p => p.vendor === vendor);

// What the dialog opens on: {type, vendor, profile}. The first model that can be added, looking at
// SP_FIRST_TYPE first; with nothing to add, the first type.
function spStart(catalog) {
  const order = [SP_FIRST_TYPE, ...catalog.types.map(t => t.id).filter(id => id !== SP_FIRST_TYPE)];
  for (const type of order) {
    const found = spOfType(catalog, type).find(p => p.available);
    if (found) return {type, vendor: found.vendor, profile: found.id};
  }
  return {type: (catalog.types[0] || {}).id || '', vendor: '', profile: ''};
}

// The choice that follows from a type (and a brand): the first brand and model that can be added.
function spFollow(catalog, type, vendor) {
  const brands = spVendors(catalog, type);
  const usable = v => spModels(catalog, type, v).some(p => p.available);
  const brand = brands.includes(vendor) ? vendor : (brands.find(usable) || brands[0] || '');
  const model = spModels(catalog, type, brand).find(p => p.available);
  return {type, vendor: brand, profile: model ? model.id : ''};
}

function spPicture(profile) {
  if (!profile) return '';
  const file = profile.image ? profile.image + '.webp' : (SP_TYPE_PICTURES[profile.type] || SP_TYPE_PICTURES.other);
  return SP_IMAGES + file;
}

function spTypeTiles(catalog, chosen) {
  return catalog.types.map(t => {
    const count = spOfType(catalog, t.id).filter(p => p.available).length;
    return `<label class="sp-type"><input type="radio" name="sim_type" value="${spEsc(t.id)}"${t.id === chosen ? ' checked' : ''}>` +
      `<img src="${SP_IMAGES}${SP_TYPE_PICTURES[t.id] || SP_TYPE_PICTURES.other}" alt="" width="40" height="40">` +
      `<b>${spEsc(t.name)}</b><small>${count ? count + ' 款' : '暂无'}</small></label>`;
  }).join('');
}

function spVendorOptions(catalog, type, chosen) {
  const brands = spVendors(catalog, type);
  if (!brands.length) return '<option value="" selected>—</option>';
  return brands.map(v => `<option value="${spEsc(v)}"${v === chosen ? ' selected' : ''}>${spEsc(v)}</option>`).join('');
}

function spModelOptions(catalog, type, vendor, chosen) {
  const models = spModels(catalog, type, vendor);
  if (!models.length) return '<option value="" selected>—</option>';
  return (chosen ? '' : '<option value="" selected>这里的型号现在都不能添加</option>') + models.map(p =>
    `<option value="${spEsc(p.id)}"${p.id === chosen ? ' selected' : ''}${p.available ? '' : ' disabled'}>` +
    `${spEsc(p.model)}${p.available ? '' : '（' + spEsc(p.problem) + '）'}</option>`).join('');
}

// What is shown about the chosen model: its picture, what kind of simulation it is.
function spPreview(catalog, choice) {
  const profile = catalog.profiles.find(p => p.id === choice.profile);
  if (!profile) {
    const none = !spOfType(catalog, choice.type).length;
    const why = (spModels(catalog, choice.type, choice.vendor).find(p => p.problem) || {}).problem || '';
    return `<p class="sp-none" role="status">${none ? SP_NO_MODEL : '这个品牌的型号现在不能添加' + (why ? '：' + spEsc(why) : '') + '。'}</p>`;
  }
  const what = profile.builtin ? '示例机器人：没有模型文件，形状是示意的。'
    : '按这个型号的 URDF 模型在 MuJoCo 里仿真；行走直接移动机身，不模拟步态。';
  return `<img src="${spEsc(spPicture(profile))}" alt="${spEsc(profile.vendor + ' ' + profile.model)}" width="120" height="120">` +
    `<div><b>${spEsc(profile.vendor)} · ${spEsc(profile.model)}</b><p>${what}</p></div>`;
}

function spBody(catalog, choice) {
  return `<fieldset class="sp-types"><legend>类型</legend>${spTypeTiles(catalog, choice.type)}</fieldset>` +
    '<div class="sp-pick">' +
    `<label class="field">品牌<select name="sim_vendor" aria-label="品牌">${spVendorOptions(catalog, choice.type, choice.vendor)}</select></label>` +
    `<label class="field">型号<select name="profile_id" aria-label="型号" required>${spModelOptions(catalog, choice.type, choice.vendor, choice.profile)}</select></label>` +
    '</div>' + `<div class="sp-preview">${spPreview(catalog, choice)}</div>`;
}

// A brand and model in the spelling of the profiles (brainos/sim_profiles.py `canonical`): what a device or an
// earlier version wrote as "zs" / "L2U" is 智身科技 / 钢镚 L2-U. Unknown brands and models come back unchanged.
const spFold = text => String(text ?? '').toLowerCase().replace(/[ \t\-_]/g, '');
function spCanon(product, table) {
  const t = table || (typeof state === 'object' && state && state.product_aliases) || {};
  if (!product || typeof product.vendor !== 'string' || typeof product.model !== 'string') return product;
  const vendors = t.vendors || {}, key = spFold(product.vendor);
  if (!Object.prototype.hasOwnProperty.call(vendors, key)) return product;
  const vendor = vendors[key], models = (t.models || {})[vendor] || {}, folded = spFold(product.model);
  return {...product, vendor, model: Object.prototype.hasOwnProperty.call(models, folded) ? models[folded] : product.model};
}

// ---------------------------------------------------------------- the dialog
function spAddForm() {
  const catalog = spCatalog();
  form('添加仿真机器人', '选择类型、品牌和型号，在这台电脑上启动一台虚拟机器人', spBody(catalog, spStart(catalog)) +
    notice('<b>仿真</b> 仿真机器人运行在本机，不连接任何真实硬件；它的测试结果不能作为现场证据。'), 'sim-add', '添加并启动');
  spSync($('#dialog form[data-form="sim-add"]'));
}

function spChoice(formEl) {
  return {type: formEl.querySelector('[name="sim_type"]:checked')?.value || '', vendor: formEl.querySelector('[name="sim_vendor"]').value,
    profile: formEl.querySelector('[name="profile_id"]').value};
}

function spSync(formEl) {
  const submit = formEl?.querySelector('button[type="submit"]');
  if (submit) submit.disabled = !formEl.querySelector('[name="profile_id"]').value;
}

function spChanged(target) {
  const formEl = target.closest('form[data-form="sim-add"]');
  if (!formEl) return;
  const catalog = spCatalog(), now = spChoice(formEl);
  let choice = now;
  if (target.name === 'sim_type') choice = spFollow(catalog, now.type, '');
  else if (target.name === 'sim_vendor') choice = spFollow(catalog, now.type, now.vendor);
  if (target.name !== 'profile_id') {
    formEl.querySelector('[name="sim_vendor"]').innerHTML = spVendorOptions(catalog, choice.type, choice.vendor);
    formEl.querySelector('[name="profile_id"]').innerHTML = spModelOptions(catalog, choice.type, choice.vendor, choice.profile);
  }
  formEl.querySelector('.sp-preview').innerHTML = spPreview(catalog, choice);
  spSync(formEl);
}

if (typeof document !== 'undefined') {
  document.addEventListener('change', e => { if (e.target.matches?.('form[data-form="sim-add"] [name]')) spChanged(e.target); });
  const install = () => {
    if (typeof simAddForm !== 'function' || typeof simSubmit !== 'function') return;
    const baseSubmit = simSubmit;
    simAddForm = spAddForm;
    simSubmit = async (kind, p, f) => {
      if (kind !== 'sim-add') return baseSubmit(kind, p, f);
      if (!p.profile_id) throw Error('请选择要添加的型号');
      toast('正在启动仿真机器人…第一次添加一个型号要先解开它的模型，可能需要十几秒');
      const robot = await api('sims', {profile_id: p.profile_id, project_id: projectId || undefined});
      projectId = robotNoTaskNext(robot.project_id, projectId, state.projects);   // issue #56: no task is made for a robot
      try { sessionStorage.setItem('brain-project', projectId); } catch { /* optional */ }
      location.hash = 'robots';
      await refresh(true);
      toast('仿真机器人已启动并连接');
      return robotDetailOpen(robot.id);
    };
  };
  // Deferred scripts run before the later ones exist: wait until all of them are there.
  if (document.readyState === 'complete') install();
  else document.addEventListener('DOMContentLoaded', install);
}
