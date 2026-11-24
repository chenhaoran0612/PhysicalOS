'use strict';
// Issue #43: the 「程序和场景数据」 section of a robot's page. Some robots walk by programs on their own
// computer; those programs and the scene data they use (place marks, reference maps) are one directory.
//  * 收进云端: the directory is packed on the robot, read back over its SSH connection and kept in the
//    cloud library as this robot's own. The person types the directory and confirms.
//  * 装到机器人: one of them is sent back and unpacked under ~/neuxmind-bundles/<name>/, next to the
//    original, which is never overwritten. The person confirms.
// Both run in the background of the desktop service; the page asks how far they are on its own
// endpoint (robots/bundles/status) and redraws its progress row in place.
// Loaded after web/cloud_skills.js (subDialog, dialogError). It wraps robotDetailRender instead of
// editing it. File names and directories come from a robot or the cloud: all of it goes through h().

const BUNDLE_POLL_MS = 1500;
const BUNDLE_PHASES = {starting: '正在准备', packing: '正在机器人上打包', fetching: '正在取回', uploading: '正在送到云平台',
  sending: '正在送到机器人', unpacking: '正在机器人上解开', done: '完成'};
const BUNDLE_FETCHING = {collect: '正在从机器人取回打好的包', install: '正在从云平台取这一份'};
const BUNDLE_NO_SSH = '这台机器人没有保存 SSH 连接。程序和场景数据要经 SSH 读写，请先通过 SSH 接入。';
const BUNDLE_INTRO = '有的机器人靠自己电脑上的程序行走。把这些程序和它们用的场景数据（地点标记、参考地图）收进云端，换了电脑或重装之后可以再装回来。';
const BUNDLE_SIGN_IN = '程序和场景数据保存在云平台的资料库里。请先登录云平台账户。';
const BUNDLE_DIRECTORY_MAX = 300;
const BUNDLE_POLL_TRIES = 4;                    // status questions that may fail in a row before the page stops asking
const BUNDLE_NAMES_SHOWN = 8;
// Control characters, line separators and the marks that change the direction of text (code point ranges).
const BUNDLE_UNSEEN_RANGES = [[0x00, 0x1f], [0x7f, 0x7f], [0x2028, 0x2029], [0x202a, 0x202e], [0x2066, 0x2069]];
const bundleUnseen = text => [...text].some(ch => BUNDLE_UNSEEN_RANGES.some(([low, high]) => ch.codePointAt(0) >= low && ch.codePointAt(0) <= high));

let robotBundle = {robotId: '', view: null, job: null, timer: null, pending: false, lastDirectory: '', pollFailures: 0};

// ---------------------------------------------------------------- small rules (no DOM)
function bundleSize(bytes) {
  const value = Number(bytes) || 0;
  return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}

// Why the directory is not sent ('' when it can be). The same rules are applied again by the desktop service.
function bundleDirectoryProblem(value) {
  const text = String(value ?? '').trim().replace(/\/+$/, '');
  if (!text) return '请填写机器人上的目录，例如 ~/walk_programs。';
  if (text.length > BUNDLE_DIRECTORY_MAX) return `目录太长：最多 ${BUNDLE_DIRECTORY_MAX} 个字符。`;
  if (bundleUnseen(text)) return '目录里有看不见的字符或换行，请重新输入。';
  if (!text.startsWith('/') && !text.startsWith('~/')) return '目录要写成完整路径（/ 开头）或主目录下的路径（~/ 开头），例如 ~/walk_programs。';
  const names = text.replace(/^~?\//, '').split('/');
  if (text === '~' || names.some(name => name === '' || name === '.' || name === '..')) {
    return '不能是主目录或根目录本身，也不能含有 . 或 .. 这样的段。';
  }
  return '';
}

// {percent, text, tone} of a job, running or ended.
function bundleProgress(job) {
  if (job.status === 'running') {
    const share = job.total ? Math.min(100, Math.floor(job.done * 100 / job.total)) : 0;
    const phase = job.phase === 'fetching' ? BUNDLE_FETCHING[job.kind] : BUNDLE_PHASES[job.phase];
    const counted = ['fetching', 'uploading', 'sending'].includes(job.phase) && job.total;
    return {percent: counted ? share : 0, text: `${phase || '进行中'}${counted ? ` ${share}%（${bundleSize(job.done)} / ${bundleSize(job.total)}）` : '…'}`, tone: ''};
  }
  if (job.status === 'done') return {percent: 100, text: bundleResultText(job), tone: 'good'};
  if (job.status === 'stopped') return {percent: 0, text: job.note || '已停止。', tone: 'warn'};
  if (job.status === 'unknown') return {percent: 0, text: `问不到进度（${job.error || '连接中断'}）。它可能还在进行，点“刷新”再看。`, tone: 'warn'};
  return {percent: 0, text: `没有完成：${job.error || '原因不明'}`, tone: 'bad'};
}

function bundleResultText(job) {
  const r = job.result || {};
  if (job.kind === 'collect') {
    const kept = r.already_in_library ? '云端已经有内容完全相同的一份，没有重复保存' : `已收进云端：${r.filename}`;
    return `${kept}（${r.files} 个文件，${bundleSize(r.bytes)}）。`;
  }
  return `已装到机器人的 ${r.device_dir}，${r.current} 已指向它${r.already_unpacked ? '（这一份之前解开过，没有重复解）' : ''}。`;
}

// "a、b 等 N 个" for the names of what stayed behind.
function bundleNames(left) {
  const names = (left.names || []).slice(0, BUNDLE_NAMES_SHOWN).join('、');
  return left.count > Math.min(BUNDLE_NAMES_SHOWN, (left.names || []).length) ? `${names} 等 ${left.count} 个` : names;
}

// A status question failed: the job is asked again; only after several in a row does the page stop, and
// then it says that it does not know, not that the job failed.
function bundlePollFailed(job, failures, message) {
  return failures >= BUNDLE_POLL_TRIES ? {...job, status: 'unknown', error: message} : {...job, error: null};
}

// Where a bundle in the list came from, in one line.
function bundleOrigin(material) {
  if (material.collected_here) return `从这台机器人的 ${material.directory} 收进来`;
  return `不是这台电脑上的控制台收的${material.created_by ? `（云端记录的上传人：${material.created_by}）` : ''}`;
}

// What the install confirmation adds for a bundle this console did not collect ('' for our own).
function bundleForeignWarning(material) {
  if (material.collected_here) return '';
  return `这一份${bundleOrigin(material)}。组织里的成员都可以往这台机器人名下上传；请确认它的来源，再装到机器人上。`;
}

// What a person should know besides "done": lines of text, possibly none.
function bundleWarnings(job) {
  const r = job.result || {}, out = [];
  if (job.status !== 'done') return out;
  if (job.kind === 'collect') {
    if (r.guarded?.count > 0) out.push(`有 ${r.guarded.count} 个像登录凭据的文件或目录没有收：${bundleNames(r.guarded)}。云端的这一份里没有它们；装回机器人后需要的话要另外放回去。`);
    if (r.skipped?.count > 0) out.push(`按约定跳过了 ${r.skipped.count} 项（运行记录、缓存、备份）：${bundleNames(r.skipped)}。`);
    if (r.cancelled_other_upload) out.push('云端有一次把同一个文件当作驱动安装包、没传完的上传，已经放弃了它。');
    if (r.outside_links > 0) out.push(`这个目录里有 ${r.outside_links} 个链接指向目录之外。链接原样收了进来，它们指向的内容没有；这样的一份装回机器人时会被拒绝。`);
    if (r.changed_while_packing) out.push('打包时有文件正在被改写，这一份里它们可能不完整；最好在程序停下时再收一次。');
  } else {
    if (r.set_aside) out.push(`机器人上原来解开的这一份被改动过，没有覆盖它：已另存为 ${r.set_aside}，并重新解开了云端的这一份。`);
    if (r.note) out.push(r.note);
  }
  return out;
}

function bundleHistoryText(entry) {
  if (entry.kind === 'collect') return `从 ${entry.directory} 收进云端：${entry.filename}`;
  return `把 ${entry.filename} 装到 ${entry.device_dir}`;
}

// ---------------------------------------------------------------- drawing
const bundleButton = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-bundle-action="${action}" ${id !== '' ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;

function bundleJobHtml(job) {
  if (!job) return '';
  const p = bundleProgress(job), running = job.status === 'running';
  const title = `${job.kind === 'collect' ? '收进云端' : '装到机器人'}：${job.subject}`;
  return `<div class="bundle-job ${p.tone}" data-bundle-job="${h(job.id)}" data-bundle-status="${h(job.status)}"><b>${h(title)}</b>` +
    (running ? `<div class="bundle-progress" role="progressbar" aria-label="进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.percent}">` +
      `<i style="transform:scaleX(${p.percent / 100})"></i></div>` : '') +
    `<div class="bundle-job-foot"><span class="bundle-job-state" aria-live="polite">${h(p.text)}</span>` +
    `${running ? bundleButton('停止', 'stop', 'secondary small', job.id) : ''}</div>` +
    bundleWarnings(job).map(line => `<p class="inline-note bundle-warning">${h(line)}</p>`).join('') + '</div>';
}

function bundleRow(material, busy) {
  return `<tr data-bundle-material="${h(material.id)}"><td><span class="cell-main">${h(material.filename)}</span>` +
    `<span class="cell-sub">${bundleSize(material.size)} · 校验值 ${h(String(material.sha256 || '').slice(0, 12))}…</span>` +
    `<span class="cell-sub bundle-origin${material.collected_here ? '' : ' foreign'}">${h(bundleOrigin(material))}</span></td>` +
    `<td>${date(typeof material.created_at === 'number' ? material.created_at * 1000 : material.created_at)}</td>` +
    `<td>${bundleButton('装到机器人', 'install', 'secondary small', material.id, busy)}</td></tr>`;
}

function bundleListHtml(view, busy) {
  if (view.bundles_error) return `<p class="inline-note">${h(view.bundles_error)}</p>`;
  if (!view.bundles.length) return '<p class="inline-note bundle-empty">云端还没有这台机器人的程序和场景数据。</p>';
  return table(['云端里这台机器人的', '收进来的时间', ''], view.bundles.map(m => bundleRow(m, busy)));
}

function bundleHistoryHtml(view) {
  if (!view.history.length) return '';
  return `<details class="tech-detail bundle-history"><summary>做过的操作（${view.history.length}）</summary>` +
    view.history.map(entry => `<p>${date(entry.at)} · ${h(bundleHistoryText(entry))}</p>`).join('') + '</details>';
}

function bundleBody(view, job) {
  if (!view) return '<p class="inline-note">正在读取…</p>';
  if (!view.ssh) return `<p class="inline-note" data-bundle-no-ssh>${h(BUNDLE_NO_SSH)}</p>`;
  if (!view.logged_in) return typeof cloudSignInNotice === 'function' ? cloudSignInNotice(BUNDLE_SIGN_IN) : notice(BUNDLE_SIGN_IN);
  const busy = job?.status === 'running';
  return `<div class="button-row bundle-actions">${bundleButton('收进云端', 'collect', 'small', '', busy)}` +
    `${bundleButton('刷新', 'refresh', 'secondary small')}</div><div id="bundle-job">${bundleJobHtml(job)}</div>` +
    `<div id="bundle-list">${bundleListHtml(view, busy)}</div>${bundleHistoryHtml(view)}`;
}

function bundleSection(robot) {
  return `<section class="bundle-section" id="robot-bundles" data-bundle-robot="${h(robot.id)}"><div class="section-head"><h3>程序和场景数据</h3>` +
    `<span>机器人自己电脑上的一个目录</span></div><p class="inline-note">${h(BUNDLE_INTRO)}</p><div id="bundle-body">${bundleBody(null)}</div></section>`;
}

const bundleShowing = () => $('#dialog').open && document.querySelector(`#robot-bundles[data-bundle-robot="${CSS.escape(robotBundle.robotId)}"]`);

function bundleDraw() {
  const body = document.querySelector('#robot-bundles #bundle-body');
  if (body) body.innerHTML = bundleBody(robotBundle.view, robotBundle.job);
}

// A running job changed: only its row is redrawn, so nothing else on the page moves.
function bundleDrawJob() {
  const holder = document.querySelector('#robot-bundles #bundle-job');
  if (holder) holder.innerHTML = bundleJobHtml(robotBundle.job);
  else bundleDraw();
}

// ---------------------------------------------------------------- reading
async function bundleLoad() {
  const robotId = robotBundle.robotId;
  if (!robotId || robotBundle.pending || !bundleShowing()) return;
  robotBundle.pending = true;
  try {
    const view = await api('robots/bundles', {robot_id: robotId, project_id: projectId});
    if (robotId !== robotBundle.robotId || !bundleShowing()) return;
    robotBundle.view = view;
    const kept = robotBundle.job?.status === 'unknown' ? view.jobs.find(job => job.id === robotBundle.job.id) : robotBundle.job;
    robotBundle.job = view.jobs.find(job => job.status === 'running') || kept || null;
    robotBundle.pollFailures = 0;
    bundleDraw();
    if (robotBundle.job?.status === 'running') bundleWatch();
  } catch (err) {
    const body = document.querySelector('#robot-bundles #bundle-body');
    if (body && robotId === robotBundle.robotId) body.innerHTML = `<p class="inline-note">${h(err.message)}</p>`;
  } finally {
    robotBundle.pending = false;
  }
}

function bundleWatch() {
  clearInterval(robotBundle.timer);
  robotBundle.timer = setInterval(bundlePoll, BUNDLE_POLL_MS);
}

async function bundlePoll() {
  const job = robotBundle.job;
  if (!job || job.status !== 'running' || !bundleShowing()) return clearInterval(robotBundle.timer);
  try {
    const next = await api('robots/bundles/status', {job_id: job.id, project_id: projectId});
    if (robotBundle.job?.id !== next.id) return;
    robotBundle.pollFailures = 0;
    robotBundle.job = next;
    if (next.status === 'running') return bundleDrawJob();
    clearInterval(robotBundle.timer);
    await bundleLoad();                       // the list and the history changed with it
  } catch (err) {
    if (robotBundle.job?.id !== job.id) return;
    robotBundle.pollFailures += 1;
    robotBundle.job = bundlePollFailed(job, robotBundle.pollFailures, err.message);
    if (robotBundle.job.status === 'running') return;      // asked again at the next tick
    clearInterval(robotBundle.timer);
    bundleDrawJob();
  }
}

function bundleStop() {
  clearInterval(robotBundle.timer);
  robotBundle = {robotId: '', view: null, job: null, timer: null, pending: false, lastDirectory: robotBundle.lastDirectory, pollFailures: 0};
}

// Puts the section into a robot detail that was just drawn, after the remote calls. A simulated robot has no computer of its own.
function bundleAttach(robot) {
  const body = $('#dialog-content .dialog-body');
  bundleStop();
  if (!robot || !body || robot.sim) return;
  const anchor = body.querySelector('[data-action="cloud-models-for"]')?.closest('.contract-line');
  if (anchor) anchor.insertAdjacentHTML('beforebegin', bundleSection(robot));
  else body.insertAdjacentHTML('beforeend', bundleSection(robot));
  robotBundle.robotId = robot.id;
  bundleLoad();
}

// ---------------------------------------------------------------- the two confirmations
function bundleCollectOpen() {
  const robot = find('robots', robotBundle.robotId), view = robotBundle.view;
  subDialog('把程序和场景数据收进云端', h(robot?.name || ''),
    `<form data-bundle-form="collect" novalidate><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    field('机器人上的目录', `<input name="directory" placeholder="~/walk_programs" value="${h(robotBundle.lastDirectory)}" autocomplete="off" ` +
      `spellcheck="false" maxlength="${BUNDLE_DIRECTORY_MAX}">`, '写成 ~/ 开头（登录账号的主目录下）或完整路径。整个目录会打成一个包。') +
    notice(`不会收进来的：运行记录（runs/）、__pycache__/、*.bak*、*.pyc 文件，以及常见的登录凭据文件和目录（.ssh、.git、.config、.env、.netrc、id_rsa、*.pem、*.key 等，只按完整的名字认）。收完会列出没有收的是哪些。` +
      `符号链接原样保留，不会顺着它去读别处的内容。打好的包最大 ${bundleSize(view?.limit_bytes || 0)}。`) +
    `<label class="hardware-confirm"><input type="checkbox" name="authorized"><span>我确认让控制台读取这个目录，并把它存到云平台（只有我的组织能看到）。` +
    `机器人上的这个目录只读取，不会改动。</span></label>` +
    `</div><div class="dialog-footer"><button type="button" class="button secondary" data-sub-dialog-close>取消</button>` +
    `<button class="button" type="submit">开始收进云端</button></div></form>`);
}

function bundleInstallOpen(materialId) {
  const robot = find('robots', robotBundle.robotId), material = robotBundle.view?.bundles.find(m => m.id === materialId);
  if (!material) return;
  const store = robotBundle.view.store;
  subDialog('把这一份装到机器人', h(robot?.name || ''),
    `<form data-bundle-form="install" data-material="${h(material.id)}" novalidate><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    `<div class="detail-meta"><div><b>要装的</b>${h(material.filename)}</div><div><b>大小</b>${bundleSize(material.size)}</div></div>` +
    notice(`它会被送到机器人的 ${h(store)}/ 下并解开，${h(store)}/…/current 指向解开的这一份。` +
      `<b>不会覆盖机器人上原来的目录，也不改适配器的配置。</b>要让管理层用这一份，装完后由你把 ros2-config.json 里的路径改到 current。`) +
    (bundleForeignWarning(material) ? notice(h(bundleForeignWarning(material)), true) : '') +
    `<p class="inline-note">解开之前会在机器人上检查：里面有写到目录之外的路径、指向目录之外的链接或设备文件时，整份都不解。</p>` +
    `<label class="hardware-confirm"><input type="checkbox" name="authorized"><span>我确认把这一份写到这台机器人上。</span></label>` +
    `</div><div class="dialog-footer"><button type="button" class="button secondary" data-sub-dialog-close>取消</button>` +
    `<button class="button" type="submit">开始装到机器人</button></div></form>`);
}

function bundleNeedsConsent(form) {
  if (form.elements.authorized.checked) return;
  form.querySelector('.hardware-confirm').classList.add('needs-confirm');
  throw Error('请先勾选下面的确认，再开始。');
}

async function bundleSubmit(kind, form) {
  const ask = {robot_id: robotBundle.robotId, project_id: projectId, authorized: true};
  if (kind === 'collect') {
    const directory = form.elements.directory.value.trim(), problem = bundleDirectoryProblem(directory);
    if (problem) throw Error(problem);
    bundleNeedsConsent(form);
    robotBundle.lastDirectory = directory;
    robotBundle.job = await api('robots/bundles/collect', {...ask, directory});
  } else {
    bundleNeedsConsent(form);
    robotBundle.job = await api('robots/bundles/install', {...ask, material_id: form.dataset.material});
  }
  subDialogClose();
  bundleDraw();
  bundleWatch();
}

async function bundleAction(type, id) {
  if (type === 'collect') return bundleCollectOpen();
  if (type === 'install') return bundleInstallOpen(id);
  if (type === 'refresh') return bundleLoad();
  if (type === 'stop') robotBundle.job = await api('robots/bundles/stop', {job_id: id, project_id: projectId});
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-bundle-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  bundleAction(el.dataset.bundleAction, el.dataset.id).catch(err => dialogError($('#dialog'), err));
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-bundle-form]');
  if (!form) return;
  e.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  form.querySelector('.form-error').textContent = '';
  bundleSubmit(form.dataset.bundleForm, form).catch(err => dialogError(form, err)).finally(() => { submit.disabled = false; });
});
$('#dialog').addEventListener('close', bundleStop);

{
  const baseRender = robotDetailRender, baseDialog = dialog;
  robotDetailRender = d => { baseRender(d); bundleAttach(d.robot); };
  dialog = (...args) => { bundleStop(); return baseDialog(...args); };
}
