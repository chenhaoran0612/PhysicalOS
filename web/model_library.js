'use strict';
// Issue #51: the library belongs to brands and models of robot.
//   · 「资料库」 page: an upload says the brand and model it is for, in the same two controls as every other
//     form (issue #61: chosen from the list or typed, web/robot_catalog.js); a material with a wrong label can be moved.
//   · 设备档案 →「资料」: shows the materials of the robot's brand and model (shared by every robot of it);
//     the brand and model are the robot's own, not typed.
// Issue #77: the page starts from a brand and model — a row of them on top (the account's robots' and those the
// library has materials for); the materials are listed for the one chosen. The robot model is no longer a
// section of its own: it is listed with the other materials (the one a simulation uses says 「仿真使用中」), and
// it is uploaded through the same 「上传资料」 with 用途「机器人模型」. A robot model may be URDF or MJCF
// (simulated), SDF, USD or STP (kept only).
// Loaded after material_library.js, material_parse.js and robot_model.js; it wraps their functions and
// listens for its own data-ml-* attributes, so those files are not edited. Rules: brainos/model_library.py.

const ML_KIND = 'model';
const ML_REMEMBERED = 'nx.library.product';            // the brand and model chosen last (this browser only)
// How a robot model's own format is named (single files; an archive is named by its container).
const ML_FORMATS = {urdf: 'URDF', mjcf: 'MJCF', sdf: 'SDF', usd: 'USD', usdz: 'USDZ', stp: 'STP'};
const ML_MODEL_EXTENSIONS = ['.urdf', '.xml', '.sdf', '.usd', '.usda', '.usdc', '.usdz', '.stp', '.step', '.zip', '.tar.gz', '.tgz'];
let mlState = {products: [], modelFormats: null, models: [], modelsLoaded: false, modelsError: '', robots: {}, selected: '', extra: null,
  account: undefined};

// ---------------------------------------------------------------- pure
const mlKey = product => (product ? (String(product.vendor).trim().replace(/\s+/g, ' ') + '\n' +
  String(product.model).trim().replace(/\s+/g, ' ')).toLowerCase() : '');
const mlLabel = product => (product ? product.vendor + ' ' + product.model : '未填写品牌型号');

// The brands and models to choose from: the server's list (the robots' first, then those with materials), or
// (an older backend) those of the robots in the page.
function mlProducts(serverList, robots) {
  if (Array.isArray(serverList) && serverList.length) return serverList.filter(p => p && p.vendor && p.model);
  const seen = new Map();
  (robots || []).forEach(robot => {
    const p = robot && robot.product;
    if (!p || !p.vendor || !p.model || robot.sim) return;
    const key = mlKey(p);
    if (!seen.has(key)) seen.set(key, {vendor: p.vendor, model: p.model, family: robot.family, robots: []});
    seen.get(key).robots.push(robot.name);
  });
  return [...seen.values()];
}

// The robot model in use for a brand and model, and the ones that are not (yet): newest first within each.
function mlModelsOf(product, models) {
  const mine = (models || []).filter(m => m.kind === ML_KIND && mlKey(m) === mlKey(product))
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  return {current: mine.find(m => m.status === 'usable') || null, others: mine.filter(m => m.status !== 'usable'),
    older: mine.filter(m => m.status === 'usable').slice(1)};
}

// Which brand and model the page shows: the one chosen, else the one chosen last in this browser, else the only one.
function mlPick(products, selected, remembered) {
  const keys = products.map(mlKey);
  if (selected && keys.includes(selected)) return selected;
  if (remembered && keys.includes(remembered)) return remembered;
  return products.length === 1 ? keys[0] : '';
}

// Why a chosen file cannot be a robot model ('' when it can). The cloud applies the same rules again.
function mlModelPrecheck(file, formats) {
  if (!file.size) return `「${file.name}」是空文件，不能上传。`;
  const extensions = formats?.extensions || ML_MODEL_EXTENSIONS;
  if (!extensions.some(ext => file.name.toLowerCase().endsWith(ext))) {
    return `「${file.name}」不能作为机器人模型：只接受 URDF（.urdf）、MJCF（.xml）、SDF（.sdf）、USD（.usd、.usda、.usdc、.usdz）、STP（.stp、.step），` +
      '或把一个模型文件和它的网格文件打成的 .zip / .tar.gz（.xacro 要先展开成 .urdf）。';
  }
  if (formats?.max_bytes && file.size > formats.max_bytes) {
    return `「${file.name}」太大：机器人模型最大 ${materialSizeText(formats.max_bytes)}，这个文件 ${materialSizeText(file.size)}。`;
  }
  return '';
}

// The brand and model of the account a typed brand and model is (in any case and spacing), or null.
function mlKnown(values, products) {
  const typed = {vendor: String(values.vendor || '').trim(), model: String(values.model || '').trim()};
  return typed.vendor && typed.model ? products.find(p => mlKey(p) === mlKey(typed)) || null : null;
}

// The labels of an upload from the form's values; a message instead when something is missing. A brand and
// model the account already has is written as it is there, with its own body type.
function mlLabels(values, products) {
  const version = String(values.version || '').trim();
  if (!version) return {error: '请填写版本'};
  const vendor = String(values.vendor || '').trim(), model = String(values.model || '').trim();
  if (!vendor || !model) return {error: '请填写品牌和型号（要一起填）'};
  const known = mlKnown(values, products);
  if (known) return {labels: {body_type: known.family || values.body_type, vendor: known.vendor, model: known.model, version}};
  if (!values.body_type) return {error: '请选择本体类型'};
  return {labels: {body_type: values.body_type, vendor, model, version}};
}

// ---------------------------------------------------------------- the chosen brand and model
function mlRemembered() {
  try { return localStorage.getItem(ML_REMEMBERED) || ''; } catch { return ''; }
}

function mlRemember(key) {
  try { localStorage.setItem(ML_REMEMBERED, key); } catch { /* a private window: the choice lasts for this visit only */ }
}

// The brands and models offered: the account's, and one just uploaded for that the cloud does not list yet.
function mlChoices() {
  const products = mlProducts(mlState.products, state.robots);
  const extra = mlState.extra;
  return extra && !products.some(p => mlKey(p) === mlKey(extra)) ? [...products, extra] : products;
}

function mlChosen() {
  return mlChoices().find(p => mlKey(p) === mlState.selected) || null;
}

// Show the materials of ``product`` (null: none chosen). The list is read again unless ``reload`` is false.
function mlSelect(product, reload = true) {
  mlState.selected = mlKey(product);
  const {vendor, model, ...rest} = materialLibrary.filter;          // eslint-disable-line no-unused-vars
  materialLibrary.filter = product ? {...rest, vendor: product.vendor, model: product.model} : rest;
  if (!reload) return;
  materialLibrary.loaded = false;
  materialRender();
  materialLibraryLoad();
}

// ---------------------------------------------------------------- markup
const mlButton = (label, type, cls = '', id = '') =>
  `<button type="button" class="button ${cls}" data-ml-action="${type}" data-id="${h(id)}">${label}</button>`;

function mlProductsBar() {
  const products = mlChoices();
  if (!products.length) {
    return empty('还没有品牌型号', '资料按品牌型号保存。先在「我的机器人」里添加机器人并填写品牌和型号，或者直接上传资料并写明它属于哪个品牌型号。',
      materialButton('上传资料', 'upload', 'secondary small'));
  }
  const chips = products.map(product => {
    const key = mlKey(product), on = key === mlState.selected, count = Number(product.materials) || 0;
    const facts = [materialBodyType(product.family), count ? `${count} 份资料` : '还没有资料'];
    if (product.usable_models) facts.push('有仿真模型');
    return `<button type="button" class="ml-chip${on ? ' on' : ''}" data-ml-action="product" data-id="${h(key)}" aria-pressed="${on}">` +
      `<b>${h(mlLabel(product))}</b><span>${h(facts.filter(Boolean).join(' · '))}</span></button>`;
  }).join('');
  return `<div class="ml-products" role="group" aria-label="品牌型号"><span class="ml-products-label">品牌型号</span>${chips}</div>`;
}

// What the chosen brand and model has for a simulation, above its list.
function mlModelNotice(product) {
  if (!product || !mlState.modelsLoaded) return '';
  if (mlState.modelsError) return notice(h(mlState.modelsError), true);
  const found = mlModelsOf(product, mlState.models), key = mlKey(product);
  if (found.current) {
    const m = found.current;
    return `<p class="inline-note ml-model-in-use">仿真使用的机器人模型：<b>${h(m.filename)}</b>（版本 ${h(m.version)}）。` +
      '要换，上传新版本并核对确认。</p>';
  }
  const waiting = found.others.filter(m => m.status === 'parsed' || m.status === 'parsing').length;
  const text = waiting
    ? `这个品牌型号有 ${waiting} 份机器人模型还没有确认为“可用”：在下面的列表里核对解析结果并确认后，仿真才能用它。`
    : '这个品牌型号还没有可用的机器人模型：仿真需要它（URDF 或 MJCF；SDF、USD、STP 只保存，仿真暂时用不了）。';
  return notice(h(text) + ' ' + mlButton('上传机器人模型', 'upload-model', 'secondary small', key), true);
}

// The upload form: what the file is for decides which files it takes.
function mlUploadDialog(kind, productKey) {
  const lib = materialLibrary, last = lib.lastLabels || {}, model = kind === ML_KIND;
  const products = mlChoices();
  // What the boxes start with: the brand and model the upload was opened for or the one shown, else the last one used, else the first.
  const chosen = products.find(p => mlKey(p) === (productKey || mlState.selected)) || (last.vendor && last.model ? last : products[0] || null);
  const purpose = (value, label, hint) => `<label class="ml-purpose"><input type="radio" name="purpose" value="${value}" ` +
    `${(value === ML_KIND) === model ? 'checked' : ''}><span><b>${label}</b><small>${hint}</small></span></label>`;
  dialog('上传资料', '文件会分片送到云平台；中途断开后，重新选同一个文件就能接着传。',
    `<form data-library-form="upload" data-ml-upload><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    `<fieldset class="ml-purposes"><legend>用途</legend>${purpose('document', '资料', '手册、参数表、驱动安装包等')}` +
    `${purpose(ML_KIND, '机器人模型', '仿真用：URDF、MJCF；SDF、USD、STP 只保存')}</fieldset>` +
    field('文件', `<input name="files" type="file" required>`, '<span data-ml-files-hint></span>') +
    mlProductFields(chosen ? {vendor: chosen.vendor, model: chosen.model} : null, last) +
    field('版本', `<input name="version" maxlength="50" placeholder="例如：1.0 或 2026-11" value="${h(last.version || '')}" required>`) +
    field('说明（可选）', '<textarea name="description" maxlength="2000" placeholder="这份资料是什么、适用范围"></textarea>') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">开始上传</button></div></form>`);
  mlPurposeSync(document.querySelector('#dialog form[data-ml-upload]'));
}

// The file box follows the chosen use: several documents, or one robot model.
function mlPurposeSync(form) {
  if (!form) return;
  const model = form.elements.purpose?.value === ML_KIND, input = form.elements.files, hint = form.querySelector('[data-ml-files-hint]');
  const extensions = model ? mlState.modelFormats?.extensions || ML_MODEL_EXTENSIONS : materialLibrary.formats?.extensions || [];
  input.multiple = !model;
  input.accept = extensions.join(',');
  if (hint) {
    hint.textContent = model
      ? '一个 .urdf 或 MJCF 的 .xml；或把它和用到的网格文件（MJCF 还有它引用的 .xml）打成一个 .zip / .tar.gz。网格只读 .stl、.obj、.msh。' +
        'SDF（.sdf）、USD（.usd 等）、STP（.stp、.step）也能保存，但仿真暂时用不了。解析后由人核对确认，确认的那一份用于仿真。'
      : `可以多选，下面的标注对所选文件都适用。${materialLibrary.formats?.supported || ''}`;
  }
}

// The brand and model of a form, and (for an upload) the body type, asked only for a brand and model the
// account does not have yet: one it has brings its own.
const ML_SOURCES = {vendor: 'mlVendorChoices', model: 'rcModelChoices', owned: 'mlOwned'};
function mlOwned() { return mlProducts(mlState.products, state.robots).map(product => ({product})); }
function mlVendorChoices() { return rcVendorOptions(mlOwned()); }

function mlProductFields(product, last, withType = true) {
  const known = Boolean(product && mlKnown(product, mlProducts(mlState.products, state.robots)));
  const families = Object.entries(materialFamilies());
  return rcInputs(product, true, ML_SOURCES) + '<p class="inline-note robot-model-note">同品牌同型号的机器人共用这里的资料。</p>' +
    (withType ? `<div class="ml-typed" data-ml-typed ${known ? 'hidden' : ''}>` +
      field('本体类型', `<select name="body_type">${families.map(([id, name]) =>
        `<option value="${h(id)}" ${id === last.body_type ? 'selected' : ''}>${h(name)}</option>`).join('')}</select>`) + '</div>' : '');
}

// The body type is asked while the brand and model in the boxes are not ones the account has.
function mlTypedSync(formEl) {
  const typed = formEl?.querySelector('[data-ml-typed]');
  if (!typed) return;
  typed.hidden = Boolean(mlKnown({vendor: formEl.elements.vendor?.value, model: formEl.elements.model?.value}, mlProducts(mlState.products, state.robots)));
}

// ---------------------------------------------------------------- loading
async function mlLoadModels() {
  try {
    const listing = await mlBaseApi('library/list', {kind: ML_KIND, limit: 200});
    mlState.models = listing.materials || [];
    mlState.modelsError = '';
  } catch (err) {
    mlState.modelsError = err.message;
  }
  mlState.modelsLoaded = true;
  if (current !== MATERIAL_ROUTE) return;
  const holder = document.querySelector('#ml-model-note'), bar = document.querySelector('#ml-products');
  if (holder) holder.innerHTML = mlModelNotice(mlChosen());
  if (bar) {                                             // the counts change when an upload is stored (the list alone is redrawn then)
    const focused = bar.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    bar.innerHTML = mlProductsBar();
    if (focused !== null) bar.querySelector(`[data-ml-action="product"][data-id="${CSS.escape(focused)}"]`)?.focus();
  }
  materialDrawList();                                    // the model in use says so in its row
}

// ---------------------------------------------------------------- actions
function mlSubmitUpload(form, kind) {
  const lib = materialLibrary, data = new FormData(form), error = form.querySelector('.form-error');
  const files = [...form.elements.files.files], model = kind === ML_KIND;
  if (model && files.length > 1) { error.textContent = '机器人模型一次只能上传一个文件（带网格时请打成一个压缩包）。'; return; }
  const refused = files.map(file => (model ? mlModelPrecheck(file, mlState.modelFormats) : materialPrecheck(file, lib.formats))).filter(Boolean);
  if (refused.length) { error.textContent = refused.join('\n'); return; }
  const made = mlLabels(Object.fromEntries(data.entries()), mlProducts(mlState.products, state.robots));
  if (made.error) { error.textContent = made.error; return; }
  const description = String(data.get('description') || '').trim();
  const labels = {...made.labels, ...(description ? {description} : {}), ...(model ? {kind: ML_KIND} : {})};
  lib.lastLabels = made.labels;
  for (const file of files) {
    lib.queue.push({id: lib.nextId++, file, labels, state: 'waiting', hashed: 0, sent: 0, message: '', cancelled: false});
  }
  $('#dialog').close();
  // The list shows the brand and model the files are for, so that they appear in it when they are stored.
  const product = {vendor: made.labels.vendor, model: made.labels.model, family: made.labels.body_type, robots: []};
  mlState.extra = mlChoices().some(p => mlKey(p) === mlKey(product)) ? mlState.extra : product;
  mlRemember(mlKey(product));
  if (mlKey(product) !== mlState.selected) mlSelect(product);
  else materialRender();
  materialPump();
}

async function mlAction(type, id) {
  if (type === 'product') {
    const product = mlChoices().find(p => mlKey(p) === id);
    if (!product || mlKey(product) === mlState.selected) return;
    mlRemember(id);
    return mlSelect(product);
  }
  if (type === 'upload-model' || type === 'upload') {
    if (!materialLibrary.formats) await materialLibraryLoad();
    if (!materialLibrary.formats) return toast(materialLibrary.error || '暂时读不到云平台支持的文件类型，请稍后再试');
    return mlUploadDialog(type === 'upload-model' ? ML_KIND : 'document', id);
  }
  if (type === 'move') {
    const m = materialLibrary.materials.concat(mlState.models).find(x => x.id === id);
    if (!m) return;
    return dialog('改品牌型号', h(m.filename), `<form data-ml-form="move" data-id="${h(m.id)}"><div class="dialog-body">` +
      `<div class="form-error" role="alert"></div><p class="inline-note">现在标的是「${h(mlLabel(m))}」。改到另一个品牌型号后，那种机器人就能使用它；` +
      '那里已经有同名同版本的资料时不会覆盖。</p>' + mlProductFields(m.vendor && m.model ? {vendor: m.vendor, model: m.model} : null, m, false) +
      `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">保存</button></div></form>`);
  }
}

async function mlSubmitMove(form) {
  const data = Object.fromEntries(new FormData(form).entries()), error = form.querySelector('.form-error');
  const products = mlProducts(mlState.products, state.robots);
  const chosen = mlKnown(data, products);
  const product = chosen ? {vendor: chosen.vendor, model: chosen.model} : {vendor: String(data.vendor || '').trim(), model: String(data.model || '').trim()};
  if (!product.vendor || !product.model) { error.textContent = '请填写品牌和型号（要一起填）'; return; }
  try {
    await mlBaseApi('library/move', {material_id: form.dataset.id, ...product});
  } catch (err) {
    error.textContent = err.message;
    return;
  }
  $('#dialog').close();
  toast('已改到「' + mlLabel(product) + '」');
  mlState.modelsLoaded = false;
  materialLibraryLoad();
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-ml-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  mlAction(el.dataset.mlAction, el.dataset.id).catch(err => toast(err.message));
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-ml-form="move"]');
  if (!form) return;
  e.preventDefault();
  mlSubmitMove(form);
});
for (const type of ['input', 'change']) {
  document.addEventListener(type, e => { if (e.target.matches?.('[data-rc-picker] input[data-rc]')) mlTypedSync(e.target.closest('form')); });
}
document.addEventListener('change', e => { if (e.target.matches?.('form[data-ml-upload] input[name="purpose"]')) mlPurposeSync(e.target.form); });

// ---------------------------------------------------------------- wrapping what is there
const mlBaseApi = api;
{
  MATERIAL_KINDS[ML_KIND] = '机器人模型';
  MATERIAL_KINDS.bundle = '程序和场景数据';
  MATERIAL_STATES.in_use = ['仿真使用中', 'good'];
  // The library overview comes with the brands and models (and moves old device-bound materials first); its
  // first page is the chosen brand and model's. The materials of a device are those of its brand and model.
  api = async (path, payload) => {
    if (path === 'library/overview') {
      let result;
      try {
        result = await mlBaseApi('library/products', payload);
      } catch (err) {
        if (err.status !== 404) throw err;
        return mlBaseApi(path, payload);                      // an older backend
      }
      mlState.products = result.products || [];
      mlState.modelFormats = result.formats?.model || null;
      // The brand and model of an upload is offered until the server lists it, and only for the account it was made in.
      const account = typeof cloudSignInKey === 'function' ? cloudSignInKey() : '';
      if (mlState.account !== account) Object.assign(mlState, {account, extra: null, selected: ''});
      if (mlState.extra && mlProducts(mlState.products, state.robots).some(p => mlKey(p) === mlKey(mlState.extra))) mlState.extra = null;
      mlState.modelsLoaded = false;
      setTimeout(mlLoadModels);
      const picked = mlChoices().find(p => mlKey(p) === mlPick(mlChoices(), mlState.selected, mlRemembered())) || null;
      if (mlKey(picked) !== mlKey(payload?.vendor && payload?.model ? payload : null)) {
        mlSelect(picked, false);
        if (picked && result.formats) {                       // the first page is the chosen one's, not everyone's
          const listed = await mlBaseApi('library/list', {...materialLibrary.filter, limit: payload?.limit || 100, offset: 0});
          result = {...result, materials: listed.materials || [], total: listed.total || 0};
        }
      } else if (!picked) mlState.selected = '';
      return result;
    }
    if (path === 'library/list' && payload && payload.device_id && !payload.kind) {
      const result = await mlBaseApi('library/robot', {robot_id: payload.device_id, project_id: projectId});
      mlState.robots[payload.device_id] = {product: result.product, shared: result.shared_with || []};
      return result;
    }
    return mlBaseApi(path, payload);
  };

  const basePage = views[MATERIAL_ROUTE], baseRow = materialRow, baseList = materialList, baseLibraryAction = materialLibraryAction;
  // Issue #77: the brands and models come first; the list (and its count) is the chosen one's.
  views[MATERIAL_ROUTE] = () => {
    const html = basePage();
    if (!(state.cloud_account || {}).logged_in) return html;
    const listAt = html.indexOf('<div class="section-head">'), queueAt = html.indexOf('<section class="panel"><div class="panel-head"><h2>本次上传');
    if (listAt < 0) return html;
    const top = queueAt >= 0 && queueAt < listAt ? queueAt : listAt, product = mlChosen();
    const bar = `<div id="ml-products">${materialLibrary.loaded || mlChoices().length ? mlProductsBar() : ''}</div>`;
    const list = !product
      ? (mlChoices().length ? `<div class="ml-choose">${notice('先在上面选一个品牌型号，再看它的资料。')}</div>` : '') + materialSupportedNote()
      : `<div id="ml-model-note">${mlModelNotice(product)}</div>` +
        html.slice(listAt).replace('<h2>已有资料 ', () => `<h2>${h(mlLabel(product))} 的资料 `);
    return html.slice(0, top) + bar + html.slice(top, listAt) + list;
  };
  materialList = () => {
    const lib = materialLibrary, product = mlChosen();
    if (!product || lib.error || !lib.loaded || lib.materials.length || lib.filter.body_type || lib.filter.q) return baseList();
    return empty('这个品牌型号还没有资料', '上传它的手册、参数表、驱动安装包，或者机器人模型（URDF、MJCF、SDF、USD、STP）。',
      materialButton('上传资料', 'upload', 'secondary small'));
  };
  // A robot model's row names its format and says whether a simulation uses it; every row can be moved.
  materialRow = m => {
    const shown = {...m, format: ML_FORMATS[m.format] || m.format};
    if (m.kind === ML_KIND && m.status === 'usable' && mlState.modelsLoaded && mlModelsOf(m, mlState.models).current?.id === m.id) shown.status = 'in_use';
    return baseRow(shown).replace(/(<button[^>]*data-library-action="delete")/, found => mlButton('改品牌型号', 'move', 'secondary small', m.id) + found);
  };
  // "上传资料" asks what the files are for and which brand and model they belong to. The form keeps its name
  // (data-library-form="upload"), so the listener of material_library.js hands it to materialSubmit, which is this one now.
  materialLibraryAction = async (type, id) => (type === 'upload' ? mlAction('upload', '') : baseLibraryAction(type, id));
  materialSubmit = form => mlSubmitUpload(form, form.elements.purpose?.value === ML_KIND ? ML_KIND : 'document');

  // 设备档案 →「资料」: the brand and model are the robot's own.
  if (typeof parseUploadForm === 'function') {
    const basePanel = parseDevicePanel;
    parseUploadForm = robot => {
      const product = rmProduct(robot), last = materialLibrary.lastLabels || {};
      if (!product) {
        return `<div class="mp-upload">${notice('这台机器人还没有品牌型号。资料按品牌型号保存，同型号的机器人共用；请先在机器人卡片的「更多功能」里填写品牌型号。', true)}` +
          `<div class="button-row">${parseButton('取消', 'upload-cancel', robot.id, 'secondary small')}</div></div>`;
      }
      const accept = (materialLibrary.formats?.extensions || []).join(',');
      return `<form class="mp-upload" data-parse-form="device-upload"><div class="form-error" role="alert"></div>` +
        field('文件（可多选）', `<input name="files" type="file" multiple required accept="${h(accept)}">`, h(materialLibrary.formats?.supported || '')) +
        `<input type="hidden" name="vendor" value="${h(product.vendor)}"><input type="hidden" name="model" value="${h(product.model)}">` +
        `<div class="material-label-row">${field('版本', `<input name="version" maxlength="50" placeholder="例如：1.0" value="${h(last.version || '')}" required>`)}</div>` +
        `<p class="inline-note">存到「${h(mlLabel(product))}」的资料库（${h(materialBodyType(robot.family))}），同品牌同型号的机器人都能使用。机器人模型请到「资料库」页上传。</p>` +
        `<div class="button-row">${parseButton('取消', 'upload-cancel', robot.id, 'secondary small')}` +
        `<button type="submit" class="button small">开始上传</button></div></form>`;
    };
    parseDevicePanel = () => {
      const robot = parseRobot(), product = robot && rmProduct(robot), known = robot && mlState.robots[robot.id];
      const shared = known?.shared?.length ? `，和 ${known.shared.slice(0, 3).join('、')}${known.shared.length > 3 ? ' 等' : ''} 共用` : '';
      return basePanel()
        .replace('这台设备的资料 <span', () => `${product ? h(mlLabel(product)) + ' 的资料' : '这台设备的资料'} <span`)
        .replace('只列出上传给这台设备的资料。', () => (product
          ? `资料按品牌型号保存：这里是「${h(mlLabel(product))}」的资料${h(shared)}。`
          : '这台机器人还没有品牌型号，这里只有以前传给它的资料；填写品牌型号后，它们会归到那个品牌型号下。'))
        .replace('这台设备还没有资料', () => (product ? '这个品牌型号还没有资料' : '这台设备还没有资料'));
    };
  }
}
