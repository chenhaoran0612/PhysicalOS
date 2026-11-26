'use strict';
// Issue #51: the library belongs to brands and models of robot.
//   · 「资料库」 page: an upload chooses the brand and model it is for (those of this account's robots, or one
//     typed in); the list can be narrowed to one; a material with a wrong label can be moved.
//   · 机器人模型: the URDF of a brand and model is kept apart from the other materials — one section, one
//     line per brand and model, with its own upload (a single .urdf, or a .zip / .tar.gz with the meshes).
//   · 设备档案 →「资料」: shows the materials of the robot's brand and model (shared by every robot of it);
//     the brand and model are the robot's own, not typed.
// Loaded after material_library.js, material_parse.js and robot_model.js; it wraps their functions and
// listens for its own data-ml-* attributes, so those files are not edited. Rules: brainos/model_library.py.

const ML_OTHER = '__other__';          // never a brand-and-model key: those contain a line break
const ML_KIND = 'model';
const ML_MODEL_STATES = {usable: ['当前使用', 'good'], parsed: ['待确认', 'warn'], parsing: ['解析中', 'blue'],
  failed: ['没解析出来', 'bad'], stored: ['已入库', '']};
let mlState = {products: [], modelFormats: null, models: [], modelsLoaded: false, modelsError: '', robots: {}};

// ---------------------------------------------------------------- pure
const mlKey = product => (product ? (String(product.vendor).trim().replace(/\s+/g, ' ') + '\n' +
  String(product.model).trim().replace(/\s+/g, ' ')).toLowerCase() : '');
const mlLabel = product => (product ? product.vendor + ' ' + product.model : '未填写品牌型号');

// The brands and models an upload can be for: the server's list, or (an older backend) those of the robots in the page.
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

// Why a chosen file cannot be a robot model ('' when it can). The cloud applies the same rules again.
function mlModelPrecheck(file, formats) {
  if (!file.size) return `「${file.name}」是空文件，不能上传。`;
  const extensions = formats?.extensions || ['.urdf', '.zip', '.tar.gz', '.tgz'];
  if (!extensions.some(ext => file.name.toLowerCase().endsWith(ext))) {
    return `「${file.name}」不能作为机器人模型：只接受单个 .urdf，或装着一个 .urdf 和它的网格文件的 .zip / .tar.gz（.xacro 要先展开成 .urdf）。`;
  }
  if (formats?.max_bytes && file.size > formats.max_bytes) {
    return `「${file.name}」太大：机器人模型最大 ${materialSizeText(formats.max_bytes)}，这个文件 ${materialSizeText(file.size)}。`;
  }
  return '';
}

// The labels of an upload from the form's values; a message instead when something is missing.
function mlLabels(values, products) {
  const version = String(values.version || '').trim();
  if (!version) return {error: '请填写版本'};
  if (values.product && values.product !== ML_OTHER) {
    const chosen = products.find(p => mlKey(p) === values.product);
    if (!chosen) return {error: '请选择品牌型号'};
    return {labels: {body_type: chosen.family || values.body_type, vendor: chosen.vendor, model: chosen.model, version}};
  }
  const vendor = String(values.vendor || '').trim(), model = String(values.model || '').trim();
  if (!vendor || !model) return {error: '请填写品牌和型号（要一起填）'};
  if (!values.body_type) return {error: '请选择本体类型'};
  return {labels: {body_type: values.body_type, vendor, model, version}};
}

// ---------------------------------------------------------------- markup
const mlButton = (label, type, cls = '', id = '') =>
  `<button type="button" class="button ${cls}" data-ml-action="${type}" data-id="${h(id)}">${label}</button>`;

function mlProductSelect(products, chosenKey, allowOther) {
  const options = products.map(p => `<option value="${h(mlKey(p))}" ${mlKey(p) === chosenKey ? 'selected' : ''}>` +
    `${h(mlLabel(p))}${p.robots?.length ? `（${h(p.robots.slice(0, 3).join('、'))}${p.robots.length > 3 ? ' 等' : ''}）` : ''}</option>`).join('');
  const other = allowOther ? `<option value="${ML_OTHER}" ${chosenKey === ML_OTHER || !products.length ? 'selected' : ''}>其他品牌型号（手动填写）</option>` : '';
  return `<select name="product" data-ml-product required>${options}${other}</select>`;
}

function mlProductFields(chosenKey, last) {
  const products = mlProducts(mlState.products, state.robots);
  const typed = chosenKey === ML_OTHER || !products.length;
  const families = Object.entries(materialFamilies());
  return field('品牌型号（这份资料属于哪种机器人）', mlProductSelect(products, typed ? ML_OTHER : chosenKey, true),
    '同品牌同型号的机器人共用这里的资料。') +
    `<div class="ml-typed" data-ml-typed ${typed ? '' : 'hidden'}><div class="material-label-row">` +
    field('品牌', `<input name="vendor" maxlength="100" placeholder="例如：示例机器人公司" value="${h(typed ? last.vendor || '' : '')}">`) +
    field('型号', `<input name="model" maxlength="100" placeholder="例如：X1" value="${h(typed ? last.model || '' : '')}">`) + '</div>' +
    field('本体类型', `<select name="body_type">${families.map(([id, name]) =>
      `<option value="${h(id)}" ${id === last.body_type ? 'selected' : ''}>${h(name)}</option>`).join('')}</select>`) + '</div>';
}

function mlUploadDialog(kind, productKey) {
  const lib = materialLibrary, last = lib.lastLabels || {}, model = kind === ML_KIND;
  const products = mlProducts(mlState.products, state.robots);
  const chosen = productKey || (products.find(p => mlKey(p) === mlKey(last)) ? mlKey(last) : products[0] ? mlKey(products[0]) : ML_OTHER);
  const accept = model ? (mlState.modelFormats?.extensions || ['.urdf', '.zip', '.tar.gz', '.tgz']).join(',') : (lib.formats?.extensions || []).join(',');
  dialog(model ? '上传机器人模型' : '上传资料', model
    ? '一个 .urdf，或把 .urdf 和它用到的网格文件打成一个 .zip / .tar.gz。解析后由人核对确认，确认的那一份用于仿真。'
    : '文件会分片送到云平台；中途断开后，重新选同一个文件就能接着传。',
    `<form ${model ? 'data-ml-form="upload-model"' : 'data-library-form="upload"'}><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    field(model ? '文件' : '文件（可多选，下面的标注对所选文件都适用）',
      `<input name="files" type="file" ${model ? '' : 'multiple'} required accept="${h(accept)}">`,
      model ? '网格文件只读 .stl、.obj、.msh；别的格式（如 .dae）在仿真里不显示。' : h(lib.formats?.supported || '')) +
    mlProductFields(chosen, last) +
    field('版本', `<input name="version" maxlength="50" placeholder="例如：1.0 或 2026-11" value="${h(last.version || '')}" required>`) +
    field('说明（可选）', '<textarea name="description" maxlength="2000" placeholder="这份资料是什么、适用范围"></textarea>') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">开始上传</button></div></form>`);
}

function mlModelLine(m, extra = '') {
  const [name, tone] = ML_MODEL_STATES[m.status] || [m.status, ''];
  return `<div class="ml-model-file" data-ml-model="${h(m.id)}"><span class="cell-main">${h(m.filename)}</span>` +
    `<span class="cell-sub">版本 ${h(m.version)} · ${materialSizeText(m.size)} · ${date(m.created_at * 1000)}</span>` +
    `${badge(name, tone)}${m.status_detail && m.status !== 'usable' ? `<span class="cell-sub">${h(m.status_detail)}</span>` : ''}` +
    `<span class="button-row">${typeof parseButton === 'function' ? parseButton(m.status === 'usable' ? '查看' : '核对并确认', 'open', m.id, 'secondary small') : ''}${extra}</span></div>`;
}

function mlModelsPanel() {
  const products = mlProducts(mlState.products, state.robots);
  if (!products.length) {
    return panel('机器人模型（URDF）', empty('还没有带品牌型号的机器人', '机器人模型按品牌型号保存。先在「我的机器人」里添加机器人并填写品牌和型号。'));
  }
  if (mlState.modelsError) return panel('机器人模型（URDF）', notice(h(mlState.modelsError), true));
  const rows = products.map(product => {
    const found = mlModelsOf(product, mlState.models), key = mlKey(product);
    const body = !mlState.modelsLoaded ? '<span class="cell-sub">正在读取…</span>'
      : (found.current ? mlModelLine(found.current) : '<span class="ml-model-none">还没有可用的机器人模型：仿真需要它。</span>') +
        found.others.map(m => mlModelLine(m)).join('') +
        (found.older.length ? `<details class="ml-model-older"><summary>更早确认的 ${found.older.length} 份</summary>${found.older.map(m => mlModelLine(m)).join('')}</details>` : '');
    return `<div class="ml-model-row" data-ml-product-row="${h(key)}"><div class="ml-model-head"><b>${h(mlLabel(product))}</b>` +
      `<span class="cell-sub">${h(materialBodyType(product.family))}${product.robots?.length ? ' · ' + h(product.robots.join('、')) : ''}</span>` +
      `${mlButton(found.current ? '上传新版本' : '上传机器人模型', 'upload-model', 'secondary small', key)}</div>` +
      `<div class="ml-model-files">${body}</div></div>`;
  }).join('');
  return panel('机器人模型（URDF）', `<div class="ml-models">${rows}</div>` +
    '<p class="inline-note ml-note">每个品牌型号用最近确认为“可用”的那一份做仿真。上传后先核对解析出的关节和上限，再确认。</p>');
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
  const holder = document.querySelector('#ml-models');
  if (holder && current === MATERIAL_ROUTE) holder.innerHTML = mlModelsPanel();
}

// ---------------------------------------------------------------- actions
function mlSubmitUpload(form, kind) {
  const lib = materialLibrary, data = new FormData(form), error = form.querySelector('.form-error');
  const files = [...form.elements.files.files], model = kind === ML_KIND;
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
  materialRender();
  materialPump();
}

async function mlAction(type, id) {
  if (type === 'upload-model' || type === 'upload') {
    if (!materialLibrary.formats) await materialLibraryLoad();
    if (!materialLibrary.formats) return toast(materialLibrary.error || '暂时读不到云平台支持的文件类型，请稍后再试');
    return mlUploadDialog(type === 'upload-model' ? ML_KIND : 'document', id);
  }
  if (type === 'move') {
    const m = materialLibrary.materials.concat(mlState.models).find(x => x.id === id);
    if (!m) return;
    const products = mlProducts(mlState.products, state.robots);
    const own = products.find(p => mlKey(p) === mlKey(m));
    return dialog('改品牌型号', h(m.filename), `<form data-ml-form="move" data-id="${h(m.id)}"><div class="dialog-body">` +
      `<div class="form-error" role="alert"></div><p class="inline-note">现在标的是「${h(mlLabel(m))}」。改到另一个品牌型号后，那种机器人就能使用它；` +
      '那里已经有同名同版本的资料时不会覆盖。</p>' + mlProductFields(own ? mlKey(own) : products[0] ? mlKey(products[0]) : ML_OTHER, m)
        .replace(/<label[^>]*>本体类型[\s\S]*?<\/label>/, '') +
      `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">保存</button></div></form>`);
  }
}

async function mlSubmitMove(form) {
  const data = Object.fromEntries(new FormData(form).entries()), error = form.querySelector('.form-error');
  const products = mlProducts(mlState.products, state.robots);
  const chosen = data.product && data.product !== ML_OTHER ? products.find(p => mlKey(p) === data.product) : null;
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
  const form = e.target.closest('form[data-ml-form]');
  if (!form) return;
  e.preventDefault();
  if (form.dataset.mlForm === 'move') mlSubmitMove(form);
  else mlSubmitUpload(form, ML_KIND);
});
document.addEventListener('change', e => {
  const select = e.target.closest('[data-ml-product]');
  if (select) {
    const typed = select.closest('form').querySelector('[data-ml-typed]');
    if (typed) typed.hidden = select.value !== ML_OTHER;
    return;
  }
  const filter = e.target.closest('[data-ml-filter]');
  if (!filter) return;
  const product = mlProducts(mlState.products, state.robots).find(p => mlKey(p) === filter.value);
  const {vendor, model, ...rest} = materialLibrary.filter;          // eslint-disable-line no-unused-vars
  materialLibrary.filter = product ? {...rest, vendor: product.vendor, model: product.model} : rest;
  materialLibrary.loaded = false;
  materialLibraryLoad();
});

// ---------------------------------------------------------------- wrapping what is there
const mlBaseApi = api;
{
  MATERIAL_KINDS[ML_KIND] = '机器人模型';
  MATERIAL_KINDS.bundle = '程序和场景数据';
  // The library overview comes with the brands and models (and moves old device-bound materials first);
  // the materials of a device are those of its brand and model.
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
      mlState.modelsLoaded = false;
      setTimeout(mlLoadModels);
      return result;
    }
    if (path === 'library/list' && payload && payload.device_id && !payload.kind) {
      const result = await mlBaseApi('library/robot', {robot_id: payload.device_id, project_id: projectId});
      mlState.robots[payload.device_id] = {product: result.product, shared: result.shared_with || []};
      return result;
    }
    return mlBaseApi(path, payload);
  };

  const baseFilters = materialFilters, basePage = views[MATERIAL_ROUTE], baseRow = materialRow, baseLibraryAction = materialLibraryAction;
  materialFilters = () => {
    const f = materialLibrary.filter, products = mlProducts(mlState.products, state.robots);
    const chosen = f.vendor && f.model ? mlKey(f) : '';
    const select = `<select class="material-type-filter" data-ml-filter aria-label="按品牌型号筛选"><option value="">全部品牌型号</option>` +
      products.map(p => `<option value="${h(mlKey(p))}" ${mlKey(p) === chosen ? 'selected' : ''}>${h(mlLabel(p))}</option>`).join('') + '</select>';
    return baseFilters().replace('<div class="material-filters">', () => '<div class="material-filters">' + select);
  };
  views[MATERIAL_ROUTE] = () => {
    const html = basePage();
    if (!(state.cloud_account || {}).logged_in) return html;
    return html.replace('<div class="section-head"><h2>已有资料', () => `<div id="ml-models">${mlModelsPanel()}</div><div class="spacer"></div>` +
      '<div class="section-head"><h2>已有资料');
  };
  materialRow = m => baseRow(m).replace(/(<button[^>]*data-library-action="delete")/, shown => mlButton('改品牌型号', 'move', 'secondary small', m.id) + shown);
  // "上传资料" asks which brand and model the files are for. The form keeps its name (data-library-form="upload"),
  // so the listener of material_library.js hands it to materialSubmit, which is this one now.
  materialLibraryAction = async (type, id) => (type === 'upload' ? mlAction('upload', '') : baseLibraryAction(type, id));
  materialSubmit = form => mlSubmitUpload(form, 'document');

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
        `<p class="inline-note">存到「${h(mlLabel(product))}」的资料库（${h(materialBodyType(robot.family))}），同品牌同型号的机器人都能使用。机器人模型（URDF）请到「资料库」页上传。</p>` +
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
