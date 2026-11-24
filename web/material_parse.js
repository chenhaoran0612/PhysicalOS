'use strict';
// Issue #34: what the cloud parsed out of a material (a manual, a URDF, a driver package), checked and
// confirmed by a person. Shown in two places:
//   - the device profile of a robot (device_profile.js) gets a 「资料」 tab: the materials of that one device,
//     uploaded there with the device's labels, each with its parsing result;
//   - the 「资料库」 page (material_library.js) gets 「解析结果」 on every row.
// Loaded after both; it wraps their functions (profileTabs, profileBody, materialRow, materialDrawItem,
// materialRender) instead of editing them. A result is never run: install steps are text.

const PARSE_POLL_MS = 4000;
const PARSE_TAB = 'materials';
const PARSE_UNITS = {revolute: ['rad', 'rad/s', 'N·m'], prismatic: ['m', 'm/s', 'N'], continuous: ['—', 'rad/s', 'N·m']};
const PARSE_LIMITS = [['lower', '位置下限'], ['upper', '位置上限'], ['velocity', '速度上限'], ['effort', '力矩上限']];
const PARSE_MOVABLE = ['revolute', 'prismatic', 'continuous'];
// The four states of R1-13 (material_parse_models.py explains them).
Object.assign(MATERIAL_STATES, {parsing: ['解析中', 'blue'], parsed: ['待确认', 'warn'], usable: ['可用', 'good'],
  failed: ['没解析出来', 'bad']});
PROFILE_TABS[PARSE_TAB] = '资料';

// The device whose 「资料」 tab is open: {robotId, loaded, materials, error, open, results, uploading, askRetry}.
let parseDevice = null;
// Results shown on screen, by material id (a dialog of the 资料库 page or rows of the device tab).
const parseShown = new Map();
let parsePollTimer = null;

// ---------------------------------------------------------------- pure rules (tests/material_parse_logic.cjs)
const parseLines = text => String(text || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);

// "/cmd_vel geometry_msgs/msg/Twist" -> {name, type}; the type is optional.
function parseNamedLines(text) {
  return parseLines(text).map(line => {
    const [name, ...rest] = line.split(/\s+/);
    return {name, type: rest.join(' ')};
  });
}

const parseNamedText = list => (list || []).map(item => item.type ? `${item.name} ${item.type}` : item.name).join('\n');

// A number typed into a limit field: null when empty, NaN when it is not a number.
function parseNumber(text) {
  const value = String(text ?? '').trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : NaN;
}

// What the form says, as the cloud expects it: {values, not_applicable, errors}.
function parseFormValues(form, result) {
  const values = {}, errors = [], na = [];
  for (const f of result.fields) {
    if (f.read_only) continue;
    const box = form.querySelector(`[name="na-${f.key}"]`);
    if (box?.checked) na.push(f.key);
    if (f.type === 'joints') {
      values.joints = (result.values.joints || []).map((joint, index) => {
        const out = {...joint};
        for (const [key, label] of PARSE_LIMITS) {
          const input = form.querySelector(`[name="pj-${index}-${key}"]`);
          if (!input) continue;
          const number = parseNumber(input.value);
          if (Number.isNaN(number)) errors.push(`关节 ${joint.name} 的${label}不是数字`);
          out[key] = Number.isNaN(number) ? null : number;
        }
        return out;
      });
      continue;
    }
    const input = form.querySelector(`[name="pf-${f.key}"]`);
    if (!input) continue;
    values[f.key] = f.type === 'text' ? input.value.trim() || null
      : f.type === 'named_list' ? parseNamedLines(input.value) : parseLines(input.value);
  }
  return {values, not_applicable: na, errors};
}

// The stage a result is in, in words, and whether the page should ask again soon.
function parseStageText(result) {
  if (result.stage === 'done') return '';
  if (result.retrying) return `暂时没有成功，稍后会自动再试：${result.error}`;
  return {queued: '排队等待解析', indexing: '解析服务正在读取这份文档', asking: '正在请大模型填写各项'}[result.stage] || '解析中';
}

// ---------------------------------------------------------------- one result
function parseFieldControl(f, result) {
  const value = result.values[f.key], missing = new Set(result.missing || []);
  const na = (result.not_applicable || []).includes(f.key);
  const label = `${h(f.label)}${f.required ? '<i class="mp-required" aria-label="必填">*</i>' : ''}`;
  const lacking = missing.has(f.label) ? '<span class="mp-lacking">缺</span>' : '';
  const naBox = f.required && !f.read_only && result.editable
    ? `<label class="mp-na"><input type="checkbox" name="na-${f.key}" ${na ? 'checked' : ''}> 不适用</label>` : '';
  const disabled = result.editable ? '' : 'disabled';
  let control;
  if (f.read_only) control = `<code class="mp-readonly">${h(value || '')}</code>`;
  else if (f.type === 'text') control = `<input name="pf-${f.key}" value="${h(value || '')}" ${disabled}>`;
  else if (f.type === 'joints') control = parseJointsTable(value || [], result);
  else {
    const text = f.type === 'named_list' ? parseNamedText(value) : (value || []).join('\n');
    const rows = Math.min(10, Math.max(2, (value || []).length + 1));
    control = `<textarea name="pf-${f.key}" rows="${rows}" ${disabled} placeholder="${f.type === 'named_list'
      ? '每行一个：名称 类型' : '每行一项'}">${h(text)}</textarea>`;
  }
  return `<div class="mp-field${lacking ? ' lacking' : ''}${na ? ' na' : ''}"><div class="mp-label">${label}${lacking}${naBox}</div>` +
    `${control}${f.hint ? `<small>${h(f.hint)}</small>` : ''}</div>`;
}

function parseJointsTable(joints, result) {
  if (!joints.length) return '<p class="inline-note">没有读到关节。</p>';
  const head = ['关节', '类型', ...PARSE_LIMITS.map(([, label]) => label)];
  const rows = joints.map((joint, index) => {
    const units = PARSE_UNITS[joint.type] || ['', '', ''];
    const cells = PARSE_LIMITS.map(([key], k) => {
      if (!PARSE_MOVABLE.includes(joint.type)) return '<td class="muted">—</td>';
      const raw = joint.raw?.[key] ? ` title="资料里写的是 ${h(joint.raw[key])}"` : '';
      const unit = units[Math.max(0, k - 1)] || '';
      const empty = joint[key] === null || joint[key] === undefined;
      return `<td class="${empty && !(joint.type === 'continuous' && k < 2) ? 'lacking' : ''}"><span class="mp-limit">` +
        `<input name="pj-${index}-${key}" inputmode="decimal" value="${empty ? '' : h(joint[key])}"${raw} ` +
        `${result.editable ? '' : 'disabled'} aria-label="${h(joint.name)} ${PARSE_LIMITS[k][1]}">` +
        `<span>${k === 0 || k === 1 ? units[0] : unit}</span></span></td>`;
    }).join('');
    return `<tr><td><b>${h(joint.name)}</b><small>${h(joint.parent || '')}${joint.child ? ' → ' + h(joint.child) : ''}</small></td>` +
      `<td>${h(joint.type || '未写')}</td>${cells}</tr>`;
  });
  return `<div class="mp-joints table-wrap"><table><thead><tr>${head.map(x => `<th>${x}</th>`).join('')}</tr></thead>` +
    `<tbody>${rows.join('')}</tbody></table></div>`;
}

function parseWho(result) {
  const parts = [];
  if (result.confirmed_by) parts.push(`${h(result.confirmed_by)} 于 ${date(result.confirmed_at * 1000)} 确认可用`);
  if (result.edited_by) parts.push(`${h(result.edited_by)} 于 ${date(result.edited_at * 1000)} 修改过`);
  return parts.length ? `<p class="mp-who">${parts.join('；')}</p>` : '';
}

function parsePanel(result) {
  const id = result.material.id, [stateName, tone] = MATERIAL_STATES[result.status] || [result.status, ''];
  const stage = parseStageText(result);
  const warnings = (result.warnings || []).length
    ? `<ul class="mp-warnings">${result.warnings.map(w => `<li>${h(w)}</li>`).join('')}</ul>` : '';
  const head = `<div class="mp-state">${badge(stateName, tone)}<span>${h(stage || result.status_detail || '')}</span></div>`;
  if (!result.editable) {
    return `<div class="mp-panel" data-parse-panel="${h(id)}">${head}${warnings}` +
      `${liveLoading('解析完成后这里会显示结果，可以修改和确认。')}</div>`;
  }
  const failed = result.error ? notice('解析时：' + h(result.error), true) : '';
  const ask = parseDevice?.askRetry === id || parseShown.get(id)?.askRetry;
  const retry = ask
    ? `<span class="mp-ask">重新解析会替换现在的结果和手工填写的内容。</span>${parseButton('确定重新解析', 'retry-confirm', id, 'danger small')}` +
      parseButton('不了', 'retry-cancel', id, 'secondary small')
    : parseButton('重新解析', 'retry', id, 'secondary small');
  const confirmable = !(result.missing || []).length;
  return `<form class="mp-panel" data-parse-panel="${h(id)}" data-parse-form="result">${head}${failed}${warnings}` +
    `<p class="inline-note mp-note">${h(result.note)}</p>` +
    `<div class="mp-fields">${result.fields.map(f => parseFieldControl(f, result)).join('')}</div>` +
    `<div class="form-error" role="alert"></div>${parseWho(result)}<div class="mp-actions"><div class="button-row">${retry}</div>` +
    `<div class="button-row">${parseButton('保存', 'save', id, 'secondary')}` +
    `${parseButton(result.status === 'usable' ? '已确认可用' : '确认可用', 'confirm', id, '', result.status === 'usable' && !confirmable)}` +
    `</div></div>${confirmable ? '' : `<p class="inline-note mp-missing">还缺：${h(result.missing.join('、'))}。填写后，或把不适用的项目勾上“不适用”，才能确认。</p>`}</form>`;
}

const parseButton = (text, action, id, cls = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-parse-action="${action}" data-id="${h(id)}" ${disabled ? 'disabled' : ''}>${text}</button>`;

// ---------------------------------------------------------------- loading and polling
async function parseLoad(materialId) {
  const result = await api('library/parse', {material_id: materialId});
  parseShown.set(materialId, {...(parseShown.get(materialId) || {}), result});
  return result;
}

// While a shown result is still being parsed, ask again every few seconds (only what is on screen).
function parseSchedulePoll() {
  clearTimeout(parsePollTimer);
  const waiting = [...parseShown.entries()].filter(([id, entry]) => entry.result && !entry.result.editable &&
    document.querySelector(`[data-parse-panel="${CSS.escape(id)}"]`));
  if (!waiting.length) return;
  parsePollTimer = setTimeout(async () => {
    for (const [id] of waiting) {
      try { parseDraw(await parseLoad(id)); } catch { /* the next round tries again */ }
    }
    parseSchedulePoll();
  }, PARSE_POLL_MS);
}

// Redraw one result where it is shown, keeping the rest of the page (and any typing elsewhere) as it is.
function parseDraw(result) {
  const holder = document.querySelector(`[data-parse-panel="${CSS.escape(result.material.id)}"]`);
  if (holder) {
    const box = document.createElement('div');
    box.innerHTML = parsePanel(result);
    holder.replaceWith(box.firstElementChild);
  }
  if (parseDevice) {
    const row = parseDevice.materials.find(m => m.id === result.material.id);
    if (row) Object.assign(row, {status: result.status, status_detail: result.status_detail});
    const line = document.querySelector(`[data-parse-row="${CSS.escape(result.material.id)}"] .mp-row-state`);
    if (line) line.innerHTML = parseRowState(row || result.material);
  }
  parseSchedulePoll();
}

// ---------------------------------------------------------------- the 「资料」 tab of a device profile
const parseRobot = () => parseDevice && find('robots', parseDevice.robotId);

function parseRowState(m) {
  const [name, tone] = MATERIAL_STATES[m.status] || [m.status, ''];
  return badge(name, tone) + (m.status_detail ? `<small>${h(m.status_detail)}</small>` : '');
}

function parseDeviceRow(m) {
  const open = parseDevice.open === m.id, entry = parseShown.get(m.id);
  const format = (materialLibrary.formats?.formats || []).find(f => f.id === m.format);
  const body = open ? (entry?.result ? parsePanel(entry.result) : liveLoading('正在读取解析结果…')) : '';
  return `<li class="mp-row${open ? ' open' : ''}" data-parse-row="${h(m.id)}"><div class="mp-row-head">` +
    `<div><b>${h(m.filename)}</b><span class="cell-sub">${h(format?.label || m.format)} · ${materialSizeText(m.size)} · ` +
    `${h(m.vendor)} / ${h(m.model)} · 版本 ${h(m.version)}</span></div><div class="mp-row-state">${parseRowState(m)}</div>` +
    `${parseButton(open ? '收起' : '查看解析结果', 'toggle', m.id, 'secondary small')}</div>${body}</li>`;
}

function parseUploadForm(robot) {
  const last = materialLibrary.lastLabels || {}, accept = (materialLibrary.formats?.extensions || []).join(',');
  const text = (name, label, max, placeholder) => field(label,
    `<input name="${name}" maxlength="${max}" placeholder="${h(placeholder)}" value="${h(last[name] || '')}" required>`);
  return `<form class="mp-upload" data-parse-form="device-upload"><div class="form-error" role="alert"></div>` +
    field('文件（可多选）', `<input name="files" type="file" multiple required accept="${h(accept)}">`,
      h(materialLibrary.formats?.supported || '')) +
    `<div class="material-label-row">${text('vendor', '厂家', 100, '例如：示例机器人公司')}${text('model', '型号', 100, '例如：X1')}` +
    `${text('version', '版本', 50, '例如：1.0')}</div>` +
    `<p class="inline-note">本体类型按这台设备填写（${h(materialBodyType(robot.family))}），资料只属于这台设备。</p>` +
    `<div class="button-row">${parseButton('取消', 'upload-cancel', robot.id, 'secondary small')}` +
    `<button type="submit" class="button small">开始上传</button></div></form>`;
}

function parseDevicePanel() {
  const ui = parseDevice, robot = parseRobot();
  if (!robot) return notice('找不到这台设备', true);
  if (!(state.cloud_account || {}).logged_in) {
    return cloudSignInNotice('资料保存在云平台。请先登录云平台账户，再上传或查看这台设备的资料。');   // issue #36: with the reason
  }
  const queue = materialLibrary.queue.filter(item => item.labels.device_id === robot.id);
  const list = ui.error ? notice(h(ui.error) + ' ' + parseButton('重试', 'reload', robot.id, 'secondary small'), true)
    : !ui.loaded ? liveLoading('正在读取这台设备的资料…')
      : ui.materials.length ? `<ul class="mp-list">${ui.materials.map(parseDeviceRow).join('')}</ul>`
        : empty('这台设备还没有资料', '上传它的驱动手册、参数表、URDF 或驱动安装包。上传后自动解析，由人核对确认后才算“可用”。');
  return `<div class="mp-device"><div class="mp-device-head"><div><h3>这台设备的资料 <span class="badge-count">${ui.materials.length}</span></h3>` +
    `<p class="inline-note">只列出上传给这台设备的资料。解析中 → 待确认 → 可用；没解析出来的可以手工填写后确认。</p></div>` +
    `<div class="button-row">${parseButton('上传资料', 'upload-open', robot.id, 'small', ui.uploading)}` +
    `${parseButton('刷新', 'reload', robot.id, 'secondary small')}</div></div>` +
    (ui.uploading ? parseUploadForm(robot) : '') +
    (queue.length ? `<div class="mp-queue">${queue.map(materialQueueRow).join('')}</div>` : '') + list + '</div>';
}

async function parseDeviceLoad() {
  const ui = parseDevice;
  if (!ui) return;
  if (ui.loading) { ui.again = true; return; }       // read once more when the running read ends
  ui.loading = true;
  try {
    if (!materialLibrary.formats) await materialLibraryLoad();     // the accepted types, for the upload form
    const listing = await api('library/list', {device_id: ui.robotId, limit: 200});
    if (parseDevice !== ui) return;
    ui.materials = listing.materials || [];
    ui.error = '';
  } catch (err) {
    if (parseDevice !== ui) return;
    ui.error = err.message;
  } finally {
    ui.loading = false;
    if (ui.again) { ui.again = false; setTimeout(parseDeviceLoad); }
  }
  ui.loaded = true;
  profileRender();
  if (ui.open && !parseShown.get(ui.open)?.result) parseToggle(ui.open, true);
}

async function parseToggle(materialId, keepOpen = false) {
  const ui = parseDevice;
  ui.open = ui.open === materialId && !keepOpen ? null : materialId;
  profileRender();
  if (!ui.open) return;
  try {
    await parseLoad(materialId);
  } catch (err) {
    parseShown.set(materialId, {result: null});
    return toast(err.message);
  }
  profileRender();
  parseSchedulePoll();
}

function parseDeviceSubmit(form) {
  const robot = parseRobot(), error = form.querySelector('.form-error');
  const files = [...form.elements.files.files];
  const refused = files.map(file => materialPrecheck(file, materialLibrary.formats)).filter(Boolean);
  if (refused.length) { error.textContent = refused.join('\n'); return; }
  const data = new FormData(form);
  const labels = {body_type: robot.family, vendor: String(data.get('vendor')).trim(), model: String(data.get('model')).trim(),
    version: String(data.get('version')).trim(), device_id: robot.id, device_name: robot.name};
  materialLibrary.lastLabels = {...materialLibrary.lastLabels, vendor: labels.vendor, model: labels.model, version: labels.version};
  for (const file of files) {
    materialLibrary.queue.push({id: materialLibrary.nextId++, file, labels, state: 'waiting', hashed: 0, sent: 0, message: '',
      cancelled: false});
  }
  parseDevice.uploading = false;
  profileRender();
  materialPump();
}

// ---------------------------------------------------------------- actions
async function parseAction(type, id, el) {
  if (type === 'toggle') return parseToggle(id);
  if (type === 'reload') { parseDevice.loaded = false; profileRender(); return parseDeviceLoad(); }
  if (type === 'upload-open') { if (!materialLibrary.formats) await materialLibraryLoad(); parseDevice.uploading = true; return profileRender(); }
  if (type === 'upload-cancel') { parseDevice.uploading = false; return profileRender(); }
  if (type === 'open') {
    const m = materialLibrary.materials.find(x => x.id === id);
    dialog(h(m?.filename || '解析结果'), '解析结果 · 核对后确认才算“可用”',
      `<div class="dialog-body mp-dialog">${liveLoading('正在读取解析结果…')}</div>`);
    let html;
    try {
      html = parsePanel(await parseLoad(id));
    } catch (err) {
      html = notice('读不到解析结果：' + h(err.message), true);
    }
    const body = document.querySelector('#dialog .mp-dialog');
    if (body) body.innerHTML = html;
    return parseSchedulePoll();
  }
  const entry = parseShown.get(id), result = entry?.result;
  if (!result) return;
  const form = el.closest('form'), error = form?.querySelector('.form-error');
  if (type === 'retry' || type === 'retry-cancel') {
    const ask = type === 'retry';
    if (parseDevice) parseDevice.askRetry = ask ? id : null;
    parseShown.set(id, {...entry, askRetry: ask});
    return parseDraw(result);
  }
  try {
    let next;
    if (type === 'retry-confirm') {
      next = await api('library/parse/retry', {material_id: id});
      if (parseDevice) parseDevice.askRetry = null;
      parseShown.set(id, {result: next});
    } else {
      const collected = parseFormValues(form, result);
      if (collected.errors.length) { error.textContent = collected.errors.join('；'); return; }
      next = await api(type === 'confirm' ? 'library/parse/confirm' : 'library/parse/save',
        {material_id: id, revision: result.revision, values: collected.values, not_applicable: collected.not_applicable});
      parseShown.set(id, {result: next});
      toast(type === 'confirm' ? '已确认：这份资料现在是“可用”' : '已保存');
    }
    parseDraw(next);
    if (materialLibrary.loaded) materialLibraryLoad();
  } catch (err) {
    if (error) (typeof formError === 'function' ? formError(error, err) : error.textContent = err.message);
    else toast(err.message);
  }
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-parse-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  parseAction(el.dataset.parseAction, el.dataset.id, el).catch(err => toast(err.message));
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-parse-form]');
  if (!form) return;
  e.preventDefault();
  if (form.dataset.parseForm === 'device-upload') parseDeviceSubmit(form);
});
$('#dialog').addEventListener('close', () => { parseDevice = null; clearTimeout(parsePollTimer); });

// ---------------------------------------------------------------- wrapping the two pages
{
  const baseTabs = profileTabs, baseBody = profileBody;
  // The tab strip of the profile, with 「资料」 at the end (the base strip knows nothing of it).
  profileTabs = (profile, tab) => {
    const label = PROFILE_TABS[PARSE_TAB];
    delete PROFILE_TABS[PARSE_TAB];
    let html;
    try { html = baseTabs(profile, tab); } finally { PROFILE_TABS[PARSE_TAB] = label; }
    const count = parseDevice?.loaded ? liveCount(parseDevice.materials.length) : '';
    return html.replace(/<\/div>$/, `<button type="button" role="tab" class="ls-tab" id="dp-tab-${PARSE_TAB}" ` +
      `aria-selected="${tab === PARSE_TAB}" aria-controls="dp-panel" data-action="engine-profile-tab" data-id="${PARSE_TAB}">` +
      `${label}${count}</button></div>`);
  };
  profileBody = ui => {
    if (!parseDevice || parseDevice.robotId !== ui.robotId) {
      parseDevice = {robotId: ui.robotId, loaded: false, loading: false, materials: [], error: '', open: null,
        uploading: false, askRetry: null};
    }
    // The device's materials are read when its tab is first shown (signed in to the cloud), not on every opening.
    if (ui.tab === PARSE_TAB && !parseDevice.loaded && !parseDevice.loading && (state.cloud_account || {}).logged_in) {
      setTimeout(parseDeviceLoad);
    }
    if (ui.tab !== PARSE_TAB) {
      // Before a profile was read there is no tab strip: the materials are one click away all the same.
      return baseBody(ui) + (ui.profile ? '' : `<div class="mp-entry">${button('查看这台设备的资料', 'engine-profile-tab', 'secondary small', PARSE_TAB)}</div>`);
    }
    const tabs = ui.profile ? profileSystem(ui.profile) + profileTabs(ui.profile, PARSE_TAB)
      : `<div class="mp-entry">${button('返回设备档案', 'engine-profile-tab', 'secondary small', 'ros2')}</div>`;
    return profileHead(ui) + tabs + `<div class="ls-panel" id="dp-panel" role="tabpanel" aria-labelledby="dp-tab-${PARSE_TAB}">` +
      `${parseDevicePanel()}</div>`;
  };

  const baseRow = materialRow, baseDraw = materialDrawItem, baseRender = materialRender;
  // 资料库 page: the state says what is missing, and every row opens its result.
  materialRow = m => {
    const [stateName, tone] = MATERIAL_STATES[m.status] || [m.status, ''];
    const plainState = `<td>${badge(stateName, tone)}</td>`;
    // Replacement functions, not strings: a `$` in a name (shown in the state's detail) must stay a `$`.
    return baseRow(m).replace(plainState, () => `<td>${parseRowState(m)}</td>`)
      .replace(/<td>(<button[^>]*data-library-action="delete")/,
        (_, deleteButton) => `<td class="mp-row-actions">${parseButton('解析结果', 'open', m.id, 'secondary small')}${deleteButton}`);
  };
  // An upload of this device finished: list its materials again (only while its tab is showing).
  const tabShown = () => parseDevice && profileView?.tab === PARSE_TAB;
  materialDrawItem = item => {
    baseDraw(item);
    if (!parseDevice || item.labels.device_id !== parseDevice.robotId) return;
    if (MATERIAL_FINISHED.includes(item.state)) {
      if (tabShown()) parseDeviceLoad();
      else parseDevice.loaded = false;                   // read again when the tab is shown
    } else if (tabShown() && !document.querySelector(`[data-library-item="${item.id}"]`)) profileRender();
  };
  materialRender = () => {
    baseRender();
    if (tabShown()) profileRender();
  };
}
