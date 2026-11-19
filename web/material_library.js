'use strict';
// Issue #33: the 「资料库」 page. Manuals, parameter sheets, URDF files and driver packages are kept in the
// cloud platform, each labelled with body type, vendor and model, and version. This page uploads them
// (in chunks, continuing an interrupted upload), shows the progress and lists what is stored.
// Loaded after web/app.js. It adds its page to the navigation and listens for its own data-library-* attributes,
// so web/app.js is not edited. Which files are accepted is decided by the cloud; the page only turns
// away what the cloud would refuse anyway, before sending it.

const MATERIAL_ROUTE = 'library';
const MATERIAL_HASH_SLICE = 1024 * 1024;         // bytes read at a time while computing the SHA-256 (the page stays responsive in between)
const MATERIAL_CHUNK_TRIES = 3;                  // per chunk, before the upload is shown as interrupted
const MATERIAL_RETRY_DELAY_MS = 1500;
const MATERIAL_BUSY_TRIES = 6;                   // the cloud verifies few files at a time: wait and ask again
const MATERIAL_BUSY_DELAY_MS = 5000;
const MATERIAL_PAGE_SIZE = 100;
const MATERIAL_SHA_SHOWN = 12;
const MATERIAL_KINDS = {document: '文档', package: '驱动安装包'};
const MATERIAL_STATES = {parsing: ['解析中', 'blue'], parsed: ['已解析', 'good'], failed: ['解析失败', 'bad']};
// Failures after which "继续" makes sense; every other refusal of the cloud is final for this file.
const MATERIAL_RESUMABLE = new Set(['cloud_unreachable', 'unauthorized', 'cloud_login_required', 'rate_limited',
  'too_many_uploads', 'storage_full', 'library_busy', 'upload_lost', 'not_found', 'desktop_internal_error']);
const MATERIAL_ACTIVE = ['waiting', 'hashing', 'starting', 'uploading', 'finishing'];
const MATERIAL_CANCELLABLE = ['waiting', 'hashing', 'starting', 'uploading'];   // while "finishing" the cloud is storing it
const MATERIAL_FINISHED = ['done', 'duplicate'];

let materialLibrary = {loaded: false, pending: null, reloadWanted: false, loadedFor: '', error: '', formats: null,
  materials: [], total: 0, filter: {body_type: '', q: ''}, queue: [], running: null, nextId: 1, lastLabels: {}};

// ---------------------------------------------------------------- SHA-256, fed piece by piece
// WebCrypto digests only a whole buffer; a 2 GB package cannot be held in memory, so the page hashes
// the file slice by slice with this (FIPS 180-4).
const MATERIAL_SHA_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

class MaterialSha256 {
  constructor() {
    this.state = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.block = new Uint8Array(64);
    this.words = new Uint32Array(64);
    this.filled = 0;
    this.length = 0;
  }

  update(bytes) {
    const total = bytes.length;
    let offset = 0;
    this.length += total;
    if (this.filled) {
      offset = Math.min(64 - this.filled, total);
      this.block.set(bytes.subarray(0, offset), this.filled);
      this.filled += offset;
      if (this.filled < 64) return this;
      this.compress(this.block, 0);
      this.filled = 0;
    }
    for (; offset + 64 <= total; offset += 64) this.compress(bytes, offset);
    if (offset < total) {
      this.block.set(bytes.subarray(offset), 0);
      this.filled = total - offset;
    }
    return this;
  }

  compress(data, at) {
    const w = this.words, s = this.state;
    for (let i = 0; i < 16; i++) {
      const j = at + i * 4;
      w[i] = (data[j] << 24) | (data[j + 1] << 16) | (data[j + 2] << 8) | data[j + 3];
    }
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let a = s[0], b = s[1], c = s[2], d = s[3], e = s[4], f = s[5], g = s[6], k = s[7];
    for (let i = 0; i < 64; i++) {
      const e1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (k + e1 + ((e & f) ^ (~e & g)) + MATERIAL_SHA_K[i] + w[i]) | 0;
      const a0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (a0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      k = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    s[0] += a; s[1] += b; s[2] += c; s[3] += d; s[4] += e; s[5] += f; s[6] += g; s[7] += k;
  }

  // The digest of everything fed so far. The object can be fed further afterwards.
  hex() {
    const tail = new Uint8Array(this.filled < 56 ? 64 : 128), bits = this.length * 8, kept = this.state.slice();
    tail.set(this.block.subarray(0, this.filled));
    tail[this.filled] = 0x80;
    const view = new DataView(tail.buffer);
    view.setUint32(tail.length - 8, Math.floor(bits / 4294967296));
    view.setUint32(tail.length - 4, bits >>> 0);
    for (let at = 0; at < tail.length; at += 64) this.compress(tail, at);
    const out = Array.from(this.state, v => v.toString(16).padStart(8, '0')).join('');
    this.state = kept;
    return out;
  }
}

// ---------------------------------------------------------------- small rules
function materialSizeText(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes) || 0, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit ? value.toFixed(1).replace(/\.0$/, '') : value} ${units[unit]}`;
}

// The accepted type a file name claims (longest suffix first, so a.tar.gz is not "a .gz"), or null.
function materialFormatOf(name, formats) {
  const lowered = String(name || '').toLowerCase();
  let best = null, bestLength = 0;
  for (const format of formats?.formats || []) {
    for (const extension of format.extensions || []) {
      if (lowered.endsWith(extension) && lowered.length > extension.length && extension.length > bestLength) {
        best = format;
        bestLength = extension.length;
      }
    }
  }
  return best;
}

// Why a chosen file is not sent at all ('' when it can be). The cloud applies the same rules again.
function materialPrecheck(file, formats) {
  if (!file.size) return `「${file.name}」是空文件，不能上传。`;
  const format = materialFormatOf(file.name, formats);
  if (!format) return `不支持「${file.name}」这种文件类型。${formats?.supported || ''}`;
  const pack = format.kind === 'package', limit = formats.limits?.[pack ? 'package_bytes' : 'document_bytes'];
  if (limit && file.size > limit) {
    return `「${file.name}」太大：${pack ? '驱动安装包' : '文档'}最大 ${materialSizeText(limit)}，这个文件 ${materialSizeText(file.size)}。`;
  }
  return '';
}

function materialMissing(upload) {
  const have = new Set(upload.received || []), missing = [];
  for (let index = 0; index < upload.chunk_count; index++) if (!have.has(index)) missing.push(index);
  return missing;
}

function materialChunkRange(upload, index) {
  const start = index * upload.chunk_bytes;
  return [start, Math.min(upload.size, start + upload.chunk_bytes)];
}

// Can "继续" help? The desktop passes a cloud refusal on as its own 400 with the cloud's status in
// `device_status`, so a cloud that is down (5xx) is told apart from a cloud that said no (4xx).
function materialResumable(err) {
  if (!err || typeof err.status !== 'number') return true;       // the request never got an answer
  if (MATERIAL_RESUMABLE.has(err.reason)) return true;
  const cloud = err.body?.device_status;
  if (Number.isInteger(cloud)) return cloud >= 500 || cloud === 408 || cloud === 425;
  return !err.reason && err.status >= 500;
}

function materialProgress(item) {
  const share = (done, all) => (all ? Math.min(100, Math.floor(done * 100 / all)) : 0);
  const sent = share(item.sent || 0, item.upload?.chunk_count);
  switch (item.state) {
    case 'waiting': return {percent: 0, text: '排队中'};
    case 'hashing': { const p = share(item.hashed || 0, item.file.size); return {percent: p, text: `正在计算校验值 ${p}%`}; }
    case 'starting': return {percent: 0, text: '正在向云平台登记这份资料'};
    case 'uploading': return {percent: sent, text: `正在上传 ${sent}%（${item.sent || 0} / ${item.upload.chunk_count} 片）`};
    case 'finishing': return {percent: 100, text: '正在核对文件内容'};
    case 'done': return {percent: 100, text: '已上传，解析中', tone: 'good'};
    case 'duplicate': return {percent: 100, text: '资料库里已经有这份资料的这个版本，没有重复上传', tone: 'good'};
    case 'interrupted': return {percent: sent, text: `上传中断：${item.message || '连接断开'}。已送到的部分保留着，可以继续。`, tone: 'warn'};
    default: return {percent: sent, text: `没有上传：${item.message || '被拒绝'}`, tone: 'bad'};
  }
}

// ---------------------------------------------------------------- page
const materialButton = (text, action, cls = '', id = '') =>
  `<button type="button" class="button ${cls}" data-library-action="${action}" ${id !== '' ? `data-id="${h(id)}"` : ''}>${text}</button>`;
const materialFamilies = () => state.families || familyNames;
const materialBodyType = id => materialFamilies()[id] || id;

function materialQueueRow(item) {
  const p = materialProgress(item), l = item.labels;
  const buttons = item.state === 'interrupted'
    ? materialButton('继续', 'resume', 'small', item.id) + materialButton('取消', 'cancel', 'secondary small', item.id)
    : MATERIAL_CANCELLABLE.includes(item.state) ? materialButton('取消', 'cancel', 'secondary small', item.id)
      : item.state === 'finishing' ? '' : materialButton('从列表移除', 'dismiss', 'secondary small', item.id);
  return `<div class="material-upload ${p.tone || ''}" data-library-item="${item.id}">` +
    `<div class="material-upload-head"><b>${h(item.file.name)}</b><span class="cell-sub">${h(materialBodyType(l.body_type))} · ` +
    `${h(l.vendor)} / ${h(l.model)} · 版本 ${h(l.version)} · ${materialSizeText(item.file.size)}</span></div>` +
    `<div class="material-progress" role="progressbar" aria-label="上传进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.percent}">` +
    `<i style="transform:scaleX(${p.percent / 100})"></i></div>` +
    `<div class="material-upload-foot"><span class="material-upload-state" aria-live="polite">${h(p.text)}</span>` +
    `<div class="button-row">${buttons}</div></div></div>`;
}

function materialRow(m) {
  const [stateName, tone] = MATERIAL_STATES[m.status] || [m.status, ''];
  const format = (materialLibrary.formats?.formats || []).find(f => f.id === m.format);
  return `<tr data-library-material="${h(m.id)}"><td><span class="cell-main">${h(m.filename)}</span>` +
    `<span class="cell-sub">${h(MATERIAL_KINDS[m.kind] || m.kind)} · ${h(format?.label || m.format)} · ${materialSizeText(m.size)}</span></td>` +
    `<td>${h(materialBodyType(m.body_type))}</td><td>${h(m.vendor)} / ${h(m.model)}</td><td>${h(m.version)}</td>` +
    `<td>${badge(stateName, tone)}</td><td><span class="mono" title="SHA-256 ${h(m.sha256)}">${h(String(m.sha256).slice(0, MATERIAL_SHA_SHOWN))}…</span></td>` +
    `<td>${date(m.created_at * 1000)}</td><td>${materialButton('删除', 'delete', 'secondary small', m.id)}</td></tr>`;
}

function materialList() {
  const lib = materialLibrary;
  if (lib.error) return notice(h(lib.error) + ' ' + materialButton('重试', 'refresh', 'secondary small'), true);
  if (!lib.loaded) return notice('正在读取资料库…');
  if (!lib.materials.length) {
    return lib.filter.body_type || lib.filter.q
      ? empty('没有符合条件的资料', '换一个本体类型或搜索词试试。')
      : empty('资料库还是空的', '上传机器人的手册、参数表、URDF 或驱动安装包，标明它属于哪种本体、哪个厂家和型号、哪个版本。', materialButton('上传资料', 'upload', 'secondary small'));
  }
  const more = lib.total > lib.materials.length
    ? `<div class="material-more">${materialButton(`显示更多（已显示 ${lib.materials.length} / ${lib.total}）`, 'more', 'secondary small')}</div>` : '';
  return table(['资料', '本体类型', '厂家 / 型号', '版本', '状态', '校验值', '上传时间', ''], lib.materials.map(materialRow)) + more;
}

function materialFilters() {
  const f = materialLibrary.filter;
  return `<div class="material-filters"><select class="material-type-filter" data-library-filter="body_type" aria-label="按本体类型筛选">` +
    `<option value="">全部本体类型</option>${Object.entries(materialFamilies()).map(([id, name]) =>
      `<option value="${h(id)}" ${id === f.body_type ? 'selected' : ''}>${h(name)}</option>`).join('')}</select>` +
    `<input type="search" data-library-filter="q" value="${h(f.q)}" placeholder="搜索文件名、厂家或型号，按回车" aria-label="搜索资料" maxlength="100"></div>`;
}

function materialSupportedNote() {
  const f = materialLibrary.formats;
  if (!f) return '';
  const l = f.limits || {};
  return `<p class="inline-note material-supported">${h(f.supported)}文档最大 ${materialSizeText(l.document_bytes)}，` +
    `驱动安装包最大 ${materialSizeText(l.package_bytes)}，本组织合计 ${materialSizeText(l.org_quota_bytes)}。` +
    `其他类型不接收。上传完成后进入“解析中”。</p>`;
}

function materialLibraryPage() {
  const lib = materialLibrary, account = state.cloud_account || {logged_in: false};
  const title = ['MATERIAL LIBRARY', '资料库', '把机器人的手册、参数表、URDF 和驱动安装包存到云平台，按本体类型、厂家型号和版本归档。'];
  if (!account.logged_in) {
    return heading(...title) + notice('资料库保存在云平台。请先登录云平台账户，再上传或查看资料。' +
      `<div class="button-row">${button('去登录云平台', 'go-cloud', 'small')}</div>`);
  }
  // First visit, or another cloud account than the one the list was read for.
  if ((!lib.loaded || lib.loadedFor !== (account.email || '')) && !lib.pending) setTimeout(materialLibraryLoad);
  const queue = lib.queue.length
    ? panel('本次上传', `<div class="material-queue">${lib.queue.map(materialQueueRow).join('')}</div>`,
      `<div class="material-queue-head">${materialQueueHead()}</div>`) + '<div class="spacer"></div>' : '';
  return heading(...title, materialButton('上传资料', 'upload') + materialButton('刷新', 'refresh', 'secondary')) + queue +
    `<div class="section-head"><h2>已有资料 <span class="badge-count" id="material-count">${Number(lib.total) || 0}</span></h2>${materialFilters()}</div>` +
    `<section class="panel" id="material-list">${materialList()}</section>${materialSupportedNote()}`;
}

function materialQueueHead() {
  const queue = materialLibrary.queue;
  return `<span>${queue.filter(i => MATERIAL_ACTIVE.includes(i.state)).length} 个进行中</span>` +
    (queue.some(i => MATERIAL_FINISHED.includes(i.state)) ? materialButton('清除已完成', 'clear-done', 'secondary small') : '');
}

function materialRender() {
  if (current === MATERIAL_ROUTE) render();
}

// One upload changed: redraw its row and the head of the queue only, so a filter being typed, the
// focus and an open form are left alone. (A full render happens when rows are added or removed.)
function materialDrawItem(item) {
  const row = document.querySelector(`[data-library-item="${item.id}"]`);
  if (!row) return;
  const holder = document.createElement('div');
  holder.innerHTML = materialQueueRow(item);
  row.replaceWith(holder.firstElementChild);
  const head = document.querySelector('.material-queue-head');
  if (head) head.innerHTML = materialQueueHead();
}

// The list changed: redraw the list and its count in place. False when the page is not showing them.
function materialDrawList() {
  const holder = document.querySelector('#material-list'), count = document.querySelector('#material-count');
  if (current !== MATERIAL_ROUTE || !holder || !count) return false;
  holder.innerHTML = materialList();
  count.textContent = String(Number(materialLibrary.total) || 0);
  return true;
}

async function materialFetch(append) {
  const lib = materialLibrary, first = !lib.formats;
  try {
    const query = {limit: MATERIAL_PAGE_SIZE, offset: append ? lib.materials.length : 0};
    for (const [key, value] of Object.entries(lib.filter)) if (value) query[key] = value;
    const result = await api(append ? 'library/list' : 'library/overview', query);
    lib.error = result.error || '';
    if (!append) lib.formats = result.formats || lib.formats;
    lib.materials = append ? lib.materials.concat(result.materials || []) : result.materials || [];
    lib.total = Number(result.total) || 0;
  } catch (err) {
    lib.error = err.message;
  }
  lib.loaded = true;
  lib.loadedFor = (state.cloud_account || {}).email || '';
  // The first answer brings the supported types (button, note): draw the whole page once.
  if (first || !materialDrawList()) materialRender();
}

// Read the list. A request made while one is running is not dropped: the list is read once more
// when the running one ends (a filter changed meanwhile, or an upload finished).
function materialLibraryLoad(append = false) {
  const lib = materialLibrary;
  if (lib.pending) {
    if (!append) lib.reloadWanted = true;
    return lib.pending;
  }
  lib.pending = materialFetch(append).finally(() => {
    lib.pending = null;
    if (lib.reloadWanted) {
      lib.reloadWanted = false;
      materialLibraryLoad();
    }
  });
  return lib.pending;
}

// ---------------------------------------------------------------- uploading
const materialSleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function materialHashFile(item) {
  const hash = new MaterialSha256(), size = item.file.size;
  for (let offset = 0; offset < size; offset += MATERIAL_HASH_SLICE) {
    if (item.cancelled) return '';
    hash.update(new Uint8Array(await item.file.slice(offset, offset + MATERIAL_HASH_SLICE).arrayBuffer()));
    item.hashed = Math.min(size, offset + MATERIAL_HASH_SLICE);
    materialDrawItem(item);
  }
  return hash.hex();
}

async function materialPutChunk(uploadId, index, blob) {
  const response = await fetch(`/api/library/chunk?upload=${encodeURIComponent(uploadId)}&index=${index}`,
    {method: 'PUT', headers: {'Content-Type': 'application/octet-stream'}, body: blob});
  let result = {};
  try { result = await response.json(); } catch { /* an answer without a body: handled by the status below */ }
  if (!response.ok) {
    throw Object.assign(Error(result.error || '这一片没有送到'), {status: response.status, reason: typeof result.code === 'string' ? result.code : '', body: result});
  }
  return result;
}

async function materialSendChunk(item, index) {
  const [start, end] = materialChunkRange(item.upload, index);
  for (let attempt = 1; ; attempt++) {
    if (item.cancelled) return null;
    try {
      return await materialPutChunk(item.upload.id, index, item.file.slice(start, end));
    } catch (err) {
      if (item.cancelled || attempt >= MATERIAL_CHUNK_TRIES || !materialResumable(err)) throw err;
      await materialSleep(MATERIAL_RETRY_DELAY_MS * attempt);
    }
  }
}

// The last step. The cloud verifies only a few files at a time; when it is busy, wait and ask again.
async function materialFinish(item) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await api('library/upload/complete', {upload_id: item.upload.id});
    } catch (err) {
      if (err.reason !== 'library_busy' || attempt >= MATERIAL_BUSY_TRIES) throw err;
      await materialSleep(MATERIAL_BUSY_DELAY_MS);
    }
  }
}

async function materialRun(item) {
  materialLibrary.running = item;
  try {
    if (!item.sha256) {
      item.state = 'hashing';
      materialDrawItem(item);
      item.sha256 = await materialHashFile(item);
    }
    if (item.cancelled) return;
    item.state = 'starting';
    materialDrawItem(item);
    // The cloud answers with the upload it already has for this file, if one was interrupted.
    item.upload = await api('library/upload/start', {filename: item.file.name, size: item.file.size, sha256: item.sha256, ...item.labels});
    if (item.cancelled) {                    // cancelled while the cloud was answering: release what it just reserved
      await api('library/upload/cancel', {upload_id: item.upload.id}).catch(() => {});
      return;
    }
    item.sent = item.upload.received.length;
    item.state = 'uploading';
    materialDrawItem(item);
    for (const index of materialMissing(item.upload)) {
      if (item.cancelled) return;
      await materialSendChunk(item, index);
      if (item.cancelled) return;
      item.sent += 1;
      materialDrawItem(item);
    }
    item.state = 'finishing';
    materialDrawItem(item);
    item.material = await materialFinish(item);
    item.state = 'done';
  } catch (err) {
    if (item.cancelled) return;
    item.message = err.message;
    item.state = err.reason === 'material_exists' ? 'duplicate' : materialResumable(err) ? 'interrupted' : 'failed';
  } finally {
    materialLibrary.running = null;
    materialDrawItem(item);
    if (MATERIAL_FINISHED.includes(item.state)) materialLibraryLoad();
    materialPump();
  }
}

// One file at a time: the next waiting upload starts when nothing is running — also not a cancelled
// one whose last request is still on its way.
function materialPump() {
  const lib = materialLibrary;
  if (lib.running) return;
  const next = lib.queue.find(item => item.state === 'waiting');
  if (next) materialRun(next);
}

// ---------------------------------------------------------------- dialogs and actions
function materialUploadDialog() {
  const lib = materialLibrary, last = lib.lastLabels, accept = (lib.formats?.extensions || []).join(',');
  const families = Object.entries(materialFamilies());
  const text = (name, label, max, placeholder) => field(label,
    `<input name="${name}" maxlength="${max}" placeholder="${h(placeholder)}" value="${h(last[name] || '')}" required>`);
  dialog('上传资料', '文件会分片送到云平台；中途断开后，重新选同一个文件就能接着传。',
    `<form data-library-form="upload"><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    field('文件（可多选，下面的标注对所选文件都适用）', `<input name="files" type="file" multiple required accept="${h(accept)}">`,
      `${h(lib.formats?.supported || '')}`) +
    field('本体类型', `<select name="body_type" required>${families.map(([id, name]) =>
      `<option value="${h(id)}" ${id === last.body_type ? 'selected' : ''}>${h(name)}</option>`).join('')}</select>`) +
    `<div class="material-label-row">${text('vendor', '厂家', 100, '例如：示例机器人公司')}${text('model', '型号', 100, '例如：X1')}` +
    `${text('version', '版本', 50, '例如：1.0 或 2026-11')}</div>` +
    field('说明（可选）', '<textarea name="description" maxlength="2000" placeholder="这份资料是什么、适用范围"></textarea>') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">开始上传</button></div></form>`);
}

function materialSubmit(form) {
  const lib = materialLibrary, data = new FormData(form), error = form.querySelector('.form-error');
  const files = [...form.elements.files.files];
  const refused = files.map(file => materialPrecheck(file, lib.formats)).filter(Boolean);
  if (refused.length) {                     // nothing is sent while any chosen file would be refused
    error.textContent = refused.join('\n');
    return;
  }
  const labels = {body_type: data.get('body_type'), vendor: String(data.get('vendor')).trim(), model: String(data.get('model')).trim(),
    version: String(data.get('version')).trim()};
  const description = String(data.get('description') || '').trim();
  lib.lastLabels = labels;
  for (const file of files) {
    lib.queue.push({id: lib.nextId++, file, labels: description ? {...labels, description} : labels, state: 'waiting',
      hashed: 0, sent: 0, message: '', cancelled: false});
  }
  $('#dialog').close();
  materialRender();
  materialPump();
}

async function materialLibraryAction(type, id) {
  const lib = materialLibrary, item = lib.queue.find(i => String(i.id) === String(id));
  if (type === 'upload') {
    if (!lib.formats) await materialLibraryLoad();      // waits for a read that is already on its way
    if (!lib.formats) return toast(lib.error || '暂时读不到云平台支持的文件类型，请稍后再试');
    return materialUploadDialog();
  }
  if (type === 'refresh') { lib.loaded = false; lib.error = ''; return materialLibraryLoad(); }
  if (type === 'more') return materialLibraryLoad(true);
  if (type === 'clear-done') { lib.queue = lib.queue.filter(i => !MATERIAL_FINISHED.includes(i.state)); return materialRender(); }
  if (type === 'resume' && item) { Object.assign(item, {state: 'waiting', message: '', cancelled: false}); materialRender(); return materialPump(); }
  if ((type === 'cancel' || type === 'dismiss') && item) {
    const unfinished = item.upload && !MATERIAL_FINISHED.includes(item.state);
    item.cancelled = true;
    lib.queue = lib.queue.filter(i => i !== item);
    materialRender();
    // What the cloud still holds of an unfinished upload is released — also of one it refused to go on with.
    if (unfinished) await api('library/upload/cancel', {upload_id: item.upload.id}).catch(() => {});
    return materialPump();
  }
  if (type === 'delete') {
    const m = lib.materials.find(x => x.id === id);
    if (!m) return;
    return dialog('删除这份资料？', h(m.filename), `<div class="dialog-body">` +
      notice('云平台上的这份文件和它的记录会被删除，不能恢复。已经下载到设备上的副本不受影响。', true) +
      `<div class="form-error" role="alert"></div></div><div class="dialog-footer">${button('取消', 'close', 'secondary')}` +
      `${materialButton('删除', 'delete-confirm', 'danger', m.id)}</div>`);
  }
  if (type === 'delete-confirm') {
    try {
      await api('library/delete', {material_id: id});
    } catch (err) {                         // a toast would be hidden behind the dialog
      const box = document.querySelector('#dialog .form-error');
      if (box) box.textContent = err.message;
      return;
    }
    $('#dialog').close();
    toast('资料已删除');
    return materialLibraryLoad();
  }
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-library-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  materialLibraryAction(el.dataset.libraryAction, el.dataset.id).catch(err => toast(err.message));
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-library-form]');
  if (!form) return;
  e.preventDefault();
  materialSubmit(form);
});
document.addEventListener('change', e => {
  const el = e.target.closest('[data-library-filter]');
  if (!el) return;
  materialLibrary.filter = {...materialLibrary.filter, [el.dataset.libraryFilter]: el.value.trim()};
  materialLibrary.loaded = false;
  materialLibraryLoad();
});

// Register the page: one more entry at the end of the navigation, its icon and its view.
icons[MATERIAL_ROUTE] = 'M4 5h6l2 2h8v12H4z M8 12h8 M8 15h5';
navigation.push([MATERIAL_ROUTE, '资料库']);
views[MATERIAL_ROUTE] = materialLibraryPage;
if (location.hash.slice(1) === MATERIAL_ROUTE) route();      // app.js routed before this file was loaded
