// Pure-logic checks of web/material_library.js (issue #33): the streamed SHA-256 the page computes
// before an upload, which files it turns away before sending anything, which chunks an interrupted
// upload still owes, which errors can be continued, and that names from the cloud are shown as text.
// The rules themselves live in the cloud (cloud/neuxmind_cloud/library.py, cloud/tests/test_library_*.py).
//   node tests/material_library_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

// Helpers of web/app.js the file uses, written as they are there (or as plain stand-ins).
const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const sandbox = {
  h, navigation: [['overview', '任务工作台'], ['cloud', '云平台账户']], icons: {}, views: {}, current: 'library',
  state: {cloud_account: {logged_in: true, configured: true}, families: {quadruped: '四足机器人', manipulator: '机械臂'}},
  familyNames: {quadruped: '四足机器人', manipulator: '机械臂'},
  document: {addEventListener: () => {}, querySelector: () => null},
  location: {hash: ''}, route: () => {}, render: () => {}, refresh: async () => {}, toast: () => {},
  api: async () => ({}), fetch: async () => ({ok: true, json: async () => ({})}), dialog: () => {}, $: () => null,
  setTimeout: fn => setTimeout(fn, 0), clearTimeout,          // waits between retries pass at once
  heading: (en, title, sub, buttons = '') => `<h1>${title}</h1><p>${sub}</p>${buttons}`,
  notice: (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}">${text}</div>`,
  panel: (title, body, right = '') => `<section class="panel"><h2>${title}</h2>${right}${body}</section>`,
  empty: (title, sub, b = '') => `<div class="empty"><b>${title}</b>${sub}${b}</div>`,
  table: (headers, data) => `<table><thead>${headers.join('|')}</thead><tbody>${data.join('')}</tbody></table>`,
  badge: (text, cls = '') => `<span class="status ${cls}">${h(text)}</span>`,
  button: (text, action, cls = '', id = '') => `<button class="button ${cls}" data-action="${action}" data-id="${h(id)}">${text}</button>`,
  field: (label, control, note = '') => `<label class="field">${label}${control}${note}</label>`,
  date: value => `date:${value}`,
};
vm.createContext(sandbox);
const lib = vm.runInContext(readFileSync('web/material_library.js', 'utf8') + `
;({MaterialSha256, materialFormatOf, materialPrecheck, materialMissing, materialChunkRange, materialSizeText,
   materialResumable, materialProgress, materialLibraryPage, materialLibrary, materialRow, materialQueueRow,
   materialPump, materialLibraryAction, materialLibraryLoad})`, sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${String(text).slice(0, 400)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${String(text).slice(0, 400)}`); checks += 1; };
const plain = value => JSON.parse(JSON.stringify(value));      // values from the other realm, for deepEqual

// --- 1. The page registers itself without editing the shared navigation table.
check(sandbox.navigation.at(-1)[0], 'library', 'the page is the last navigation entry');
includes(sandbox.navigation.at(-1)[1], '资料', 'it is called 资料库');
check(typeof sandbox.views.library, 'function', 'the view is registered');
check(typeof sandbox.icons.library, 'string', 'it has an icon');

// --- 2. SHA-256, fed in pieces of any size, equals the digest of the whole.
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const streamed = (bytes, sizes) => {
  const hash = new lib.MaterialSha256();
  let offset = 0, turn = 0;
  while (offset < bytes.length) {
    const size = sizes[turn++ % sizes.length];
    hash.update(new Uint8Array(bytes.buffer, bytes.byteOffset + offset, Math.min(size, bytes.length - offset)));
    offset += size;
  }
  return hash.hex();
};
check(new lib.MaterialSha256().hex(), digest(Buffer.alloc(0)), 'the empty input');
check(streamed(Buffer.from('abc'), [3]), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 'the standard vector');
for (const length of [1, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000, 65537]) {
  const bytes = crypto.randomBytes(length);
  for (const sizes of [[length], [1], [63, 1, 64, 65], [7, 4096]]) {
    check(streamed(bytes, sizes), digest(bytes), `length ${length}, pieces ${sizes}`);
  }
}
const big = crypto.randomBytes(3 * 1024 * 1024 + 17);
check(streamed(big, [1024 * 1024]), digest(big), 'three megabytes in one-megabyte slices');
check(streamed(big, [999_983]), digest(big), 'slices that never line up with a block');

// --- 3. Which files are turned away before anything is sent.
const formats = {
  formats: [{id: 'pdf', label: 'PDF', kind: 'document', extensions: ['.pdf']},
            {id: 'tar', label: 'tar 包', kind: 'package', extensions: ['.tar']},
            {id: 'tar.gz', label: 'tar.gz 压缩包', kind: 'package', extensions: ['.tar.gz', '.tgz']},
            {id: 'urdf', label: 'URDF', kind: 'document', extensions: ['.urdf', '.xacro']}],
  supported: '支持的类型：PDF（.pdf）、URDF（.urdf、.xacro）、驱动安装包（.tar、.tar.gz、.tgz）。',
  limits: {document_bytes: 1000, package_bytes: 5000, org_quota_bytes: 9000, chunk_bytes: 256},
};
check(lib.materialFormatOf('driver.TAR.GZ', formats).id, 'tar.gz', 'the longest suffix wins, whatever the case');
check(lib.materialFormatOf('notes.tar', formats).id, 'tar', 'a plain tar');
check(lib.materialFormatOf('leg.xacro', formats).id, 'urdf', 'a second suffix of the same type');
check(lib.materialFormatOf('setup.exe', formats), null, 'not listed');
check(lib.materialFormatOf('driver.tar.gz.exe', formats), null, 'a listed suffix in the middle does not count');
check(lib.materialFormatOf('pdf', formats), null, 'no suffix at all');
check(lib.materialPrecheck({name: 'spec.pdf', size: 1000}, formats), '', 'a document at its limit');
includes(lib.materialPrecheck({name: 'setup.exe', size: 10}, formats), '.tar.gz', 'the refusal names what is supported');
includes(lib.materialPrecheck({name: 'setup.exe', size: 10}, formats), 'setup.exe', 'and the file it refuses');
includes(lib.materialPrecheck({name: 'spec.pdf', size: 1001}, formats), '文档', 'a document over its limit');
check(lib.materialPrecheck({name: 'driver.tgz', size: 1001}, formats), '', 'a package may be larger than a document');
includes(lib.materialPrecheck({name: 'driver.tgz', size: 5001}, formats), '安装包', 'a package over its limit');
includes(lib.materialPrecheck({name: 'empty.pdf', size: 0}, formats), '空', 'an empty file');

// --- 4. What an interrupted upload still owes, and where each chunk sits in the file.
const upload = {id: 'upl_1', size: 1000, chunk_bytes: 256, chunk_count: 4, received: [0, 2]};
check(plain(lib.materialMissing(upload)), [1, 3], 'only the chunks the cloud does not have');
check(plain(lib.materialMissing({...upload, received: []})), [0, 1, 2, 3], 'a new upload owes everything');
check(plain(lib.materialMissing({...upload, received: [3, 2, 1, 0]})), [], 'nothing left');
check(plain(lib.materialChunkRange(upload, 0)), [0, 256], 'the first chunk');
check(plain(lib.materialChunkRange(upload, 3)), [768, 1000], 'the last chunk is the remainder');

// --- 5. Which failures can be continued and which are final.
const failure = (reason, status = 400) => Object.assign(Error('x'), {reason, status});
for (const reason of ['cloud_unreachable', 'unauthorized', 'cloud_login_required', 'rate_limited', 'too_many_uploads',
                      'storage_full', 'upload_lost', 'not_found', 'desktop_internal_error']) {
  check(lib.materialResumable(failure(reason)), true, `${reason} can be continued`);
}
for (const reason of ['unsupported_type', 'file_too_large', 'digest_mismatch', 'version_exists', 'material_exists',
                      'library_quota_exceeded', 'forbidden', 'email_unverified', 'chunk_attempts_exceeded', 'invalid_request']) {
  check(lib.materialResumable(failure(reason)), false, `${reason} is final`);
}
check(lib.materialResumable(new TypeError('Failed to fetch')), true, 'the page lost the desktop backend');
check(lib.materialResumable(failure('', 500)), true, 'an error without a reason from the backend');
check(lib.materialResumable(failure('', 400)), false, 'a refusal without a reason');
// The desktop answers 400 for whatever the cloud said and passes the cloud's status on as device_status.
const fromCloud = (reason, cloudStatus) => Object.assign(Error('x'), {reason, status: 400, body: {device_status: cloudStatus}});
check(lib.materialResumable(fromCloud('', 502)), true, 'the cloud is down behind its proxy');
check(lib.materialResumable(fromCloud('internal_error', 500)), true, 'the cloud failed');
check(lib.materialResumable(fromCloud('library_busy', 503)), true, 'the cloud is busy');
check(lib.materialResumable(fromCloud('', 408)), true, 'the cloud timed out');
check(lib.materialResumable(fromCloud('unsupported_type', 415)), false, 'the cloud said no');
check(lib.materialResumable(fromCloud('chunk_attempts_exceeded', 429)), false, 'no more chunks for this upload');
check(lib.materialResumable(fromCloud('', 404)), false, 'an unexplained 4xx from the cloud');

// --- 6. Sizes and progress in words.
check(lib.materialSizeText(999), '999 B', 'bytes');
check(lib.materialSizeText(1536), '1.5 KB', 'kilobytes');
check(lib.materialSizeText(200 * 1024 * 1024), '200 MB', 'megabytes');
check(lib.materialSizeText(2 * 1024 ** 3), '2 GB', 'gigabytes');
const item = {id: 1, file: {name: 'a.pdf', size: 1000}, labels: {}, state: 'hashing', hashed: 250};
check(lib.materialProgress(item).percent, 25, 'hashing: the share of the file read');
includes(lib.materialProgress(item).text, '校验', 'hashing is named');
Object.assign(item, {state: 'uploading', upload, sent: 2});
check(lib.materialProgress(item).percent, 50, 'uploading: chunks the cloud has');
includes(lib.materialProgress(item).text, '2 / 4', 'chunks in words');
Object.assign(item, {state: 'done'});
check(lib.materialProgress(item).percent, 100, 'done');
includes(lib.materialProgress(item).text, '解析中', 'a finished upload waits for parsing');
Object.assign(item, {state: 'interrupted', message: '无法连接云平台'});
includes(lib.materialProgress(item).text, '无法连接云平台', 'the reason of an interruption is shown');

// --- 7. The page: sign-in notice, list, and names shown as text.
sandbox.state.cloud_account = {logged_in: false, configured: true};
Object.assign(lib.materialLibrary, {loaded: true, loading: false, error: '', formats: null, materials: [], total: 0, queue: []});
let html = lib.materialLibraryPage();
includes(html, '登录', 'without a cloud login the page says to sign in');
includes(html, 'go-cloud', 'and links to the account page');
excludes(html, 'data-library-action="upload"', 'no upload button without a login');

sandbox.state.cloud_account = {logged_in: true, configured: true};
const evil = '<img src=x onerror=alert(1)>.pdf';
Object.assign(lib.materialLibrary, {formats, total: 2, materials: [
  {id: 'mat_1', filename: evil, kind: 'document', format: 'pdf', body_type: 'quadruped', vendor: '<b>厂家</b>', model: 'X1',
   version: '1.0', size: 2048, sha256: 'ab'.repeat(32), status: 'parsing', created_at: 1700000000},
  {id: 'mat_2', filename: 'driver.tgz', kind: 'package', format: 'tar.gz', body_type: 'unknown-type', vendor: 'V', model: 'M',
   version: '2', size: 4096, sha256: 'cd'.repeat(32), status: 'parsed', created_at: 1700000100}]});
html = lib.materialLibraryPage();
includes(html, 'data-library-action="upload"', 'signed in: the upload button');
includes(html, '&lt;img src=x onerror=alert(1)&gt;.pdf', 'a file name is shown as text');
excludes(html, '<img src=x', 'and never as markup');
excludes(html, '<b>厂家</b>', 'a vendor name is shown as text');
includes(html, '解析中', 'the parsing state');
includes(html, '四足机器人', 'a known body type by its name');
includes(html, 'unknown-type', 'an unknown body type by its identifier');
includes(html, 'abababababab', 'the start of the SHA-256');
includes(html, '.tar.gz', 'the supported types are listed on the page');
includes(html, 'date:1700000000000', 'cloud times are seconds');
includes(lib.materialRow(lib.materialLibrary.materials[1]), '驱动安装包', 'a package is named as one');

const queued = {id: 7, file: {name: evil, size: 1000}, labels: {body_type: 'quadruped', vendor: 'V', model: 'M', version: '1'},
                state: 'interrupted', message: '<script>x</script>', upload, sent: 2};
html = lib.materialQueueRow(queued);
excludes(html, '<img src=x', 'a queued file name is text');
excludes(html, '<script>x</script>', 'an error message is text');
includes(html, 'data-library-action="resume"', 'an interrupted upload can be continued');
includes(html, 'data-library-action="cancel"', 'or cancelled');
includes(html, 'role="progressbar"', 'the bar is announced as progress');
html = lib.materialQueueRow({...queued, state: 'failed', message: '不支持这种文件类型'});
excludes(html, 'data-library-action="resume"', 'a final refusal is not continued');
includes(html, 'data-library-action="dismiss"', 'it can be removed from the list');
html = lib.materialQueueRow({...queued, state: 'finishing'});
excludes(html, 'data-library-action', 'while the cloud stores the file there is nothing to cancel');

// --- 8. The upload itself, against a stand-in for the desktop backend.
const CHUNK = 256;
const fileOf = (name, bytes) => {
  const file = {name, size: bytes.length, reads: 0};
  file.slice = (a, b) => ({arrayBuffer: async () => { file.reads += 1; return bytes.buffer.slice(bytes.byteOffset + a, bytes.byteOffset + Math.min(b, bytes.length)); }});
  return file;
};
const refusal = (reason, cloudStatus, message = reason) => Object.assign(Error(message), {reason, status: 400, body: {device_status: cloudStatus}});
function backend({received = [], startError = null, startGate = null, completeErrors = [], chunkReply = null} = {}) {
  const b = {calls: [], held: [...received], dropChunks: 0};
  sandbox.api = async (path, payload) => {
    b.calls.push(path);
    if (path === 'library/upload/start') {
      if (startGate) await startGate;
      if (startError) throw startError;
      return {id: 'upl_x', size: payload.size, chunk_bytes: CHUNK, chunk_count: Math.ceil(payload.size / CHUNK), received: [...b.held]};
    }
    if (path === 'library/upload/complete') {
      if (completeErrors.length) throw completeErrors.shift();
      return {id: 'mat_x'};
    }
    if (path === 'library/overview' || path === 'library/list') return {formats, materials: [], total: 0};
    return {cancelled: true};
  };
  sandbox.fetch = async url => {
    const index = Number(new URL(url, 'http://desktop.test').searchParams.get('index'));
    b.calls.push('chunk:' + index);
    if (b.dropChunks > 0) { b.dropChunks -= 1; throw new TypeError('Failed to fetch'); }
    if (chunkReply) return chunkReply;
    b.held.push(index);
    return {ok: true, status: 200, json: async () => ({index})};
  };
  return b;
}
const labels = {body_type: 'quadruped', vendor: 'V', model: 'M', version: '1'};
const enqueue = (name, bytes) => {
  const item = {id: lib.materialLibrary.nextId++, file: fileOf(name, bytes), labels, state: 'waiting', hashed: 0, sent: 0, message: '', cancelled: false};
  lib.materialLibrary.queue.push(item);
  return item;
};
const settled = async () => {
  for (let turn = 0; turn < 5000 && (lib.materialLibrary.running || lib.materialLibrary.pending); turn++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(lib.materialLibrary.running, null, 'the upload did not come to rest');
};
const chunksOf = b => b.calls.filter(c => c.startsWith('chunk:')).map(c => Number(c.slice(6)));
const count = (b, path) => b.calls.filter(c => c === path).length;
const reset = () => Object.assign(lib.materialLibrary, {queue: [], running: null, pending: null, reloadWanted: false});

(async () => {
  const bytes = crypto.randomBytes(2 * CHUNK + 88);                  // three chunks

  // A whole upload: hash, start, every chunk in order, complete, then the list is read again.
  reset();
  let b = backend(), item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check(item.state, 'done', 'a whole upload ends as done');
  check(item.sha256, digest(bytes), 'the SHA-256 sent is the file\'s');
  check(chunksOf(b), [0, 1, 2], 'every chunk, once, in order');
  check([count(b, 'library/upload/start'), count(b, 'library/upload/complete')], [1, 1], 'one start, one complete');
  check(b.calls.indexOf('library/overview') > b.calls.indexOf('library/upload/complete'), true, 'the list is read again afterwards');

  // The cloud already has two of three chunks: only the missing one is sent.
  reset();
  b = backend({received: [0, 2]}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check([item.state, chunksOf(b)], ['done', [1]], 'continuing sends only what is missing');

  // The connection drops for good on the second chunk: three tries, then "interrupted"; "继续" goes on from there.
  reset();
  b = backend(); item = enqueue('a.pdf', bytes);
  b.dropChunks = 1e9;
  lib.materialPump();
  await settled();
  check([item.state, chunksOf(b)], ['interrupted', [0, 0, 0]], 'three tries, then it is shown as interrupted');
  const readsBefore = item.file.reads;
  b.dropChunks = 0; b.held.push(0);                                  // one chunk did arrive before the drop
  await lib.materialLibraryAction('resume', item.id);
  await settled();
  check([item.state, chunksOf(b).slice(3)], ['done', [1, 2]], 'continuing asks the cloud what it has and sends the rest');
  check(item.file.reads, readsBefore, 'the file is not hashed a second time');
  check(count(b, 'library/upload/start'), 2, 'continuing starts from "start" again');

  // Cancelled while the cloud is still answering "start": what it reserved is released, no chunk is sent.
  reset();
  let open;
  b = backend({startGate: new Promise(resolve => { open = resolve; })}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  for (let turn = 0; turn < 200 && item.state !== 'starting'; turn++) await new Promise(resolve => setImmediate(resolve));
  check(item.state, 'starting', 'the upload is waiting for the cloud');
  const cancelling = lib.materialLibraryAction('cancel', item.id);
  open();
  await cancelling;
  await settled();
  check([count(b, 'library/upload/cancel'), chunksOf(b), lib.materialLibrary.queue.length], [1, [], 0], 'cancelled during start: released, nothing sent');

  // A refusal that is final, with an upload still open in the cloud: removing the row releases it.
  reset();
  b = backend({chunkReply: {ok: false, status: 400, json: async () => ({error: '这次上传发送分片的次数过多', code: 'chunk_attempts_exceeded', device_status: 429})}});
  item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check([item.state, chunksOf(b)], ['failed', [0]], 'a final refusal is not retried');
  await lib.materialLibraryAction('dismiss', item.id);
  check(count(b, 'library/upload/cancel'), 1, 'removing a refused upload frees it in the cloud');

  // Refused before anything was reserved: nothing to release.
  reset();
  b = backend({startError: refusal('unsupported_type', 415, '不支持这种文件类型')}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check([item.state, item.message], ['failed', '不支持这种文件类型'], 'the cloud\'s reason is kept for the row');
  await lib.materialLibraryAction('dismiss', item.id);
  check(count(b, 'library/upload/cancel'), 0, 'nothing was reserved, nothing is cancelled');

  // Already in the library: neither a failure nor an interruption.
  reset();
  b = backend({startError: refusal('material_exists', 409)}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check(item.state, 'duplicate', 'a duplicate is its own state');

  // The cloud is busy verifying other files: the page waits and asks again by itself.
  reset();
  b = backend({completeErrors: [refusal('library_busy', 503), refusal('library_busy', 503)]}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check([item.state, count(b, 'library/upload/complete')], ['done', 3], 'busy twice, stored on the third try');

  // The cloud went down during the last step: that can be continued.
  reset();
  b = backend({completeErrors: [refusal('', 502, '云平台返回 HTTP 502')]}); item = enqueue('a.pdf', bytes);
  lib.materialPump();
  await settled();
  check(item.state, 'interrupted', 'a cloud that is down is an interruption, not a refusal');

  // Two files: one at a time, in the order they were chosen.
  reset();
  b = backend();
  const first = enqueue('1.pdf', bytes), second = enqueue('2.pdf', crypto.randomBytes(CHUNK));
  lib.materialPump();
  lib.materialPump();                                                // a second call does not start a second upload
  await settled();
  check([first.state, second.state], ['done', 'done'], 'both files are uploaded');
  check(count(b, 'library/upload/complete'), 2, 'each is completed once');
  check(b.calls.indexOf('library/upload/complete') < b.calls.lastIndexOf('library/upload/start'), true, 'the second starts after the first is done');

  // A list request made while another is running is not lost: the list is read once more afterwards.
  reset();
  b = backend();
  const running = lib.materialLibraryLoad();
  lib.materialLibrary.filter = {body_type: 'manipulator', q: ''};
  lib.materialLibraryLoad();
  await running;
  await settled();
  check(count(b, 'library/overview'), 2, 'the second request is carried out after the first');

  console.log(`material_library_logic: ${checks} checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
