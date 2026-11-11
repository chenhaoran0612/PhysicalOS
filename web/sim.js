'use strict';
// ux01：一键添加仿真机器人、示例技能库、在仿真机器人上运行技能并观察实时状态。
// 仿真机器人是本机启动的端侧运行时（neuxmind_runtime serve + 仿真本体），地址和令牌由桌面端自动管理。
// 运动始终需要页面上的现场授权；“停止”立即发送并锁存急停，解除急停沿用原有的现场确认流程。

const simStateNames = {starting: ['正在启动', 'blue'], running: ['运行中', 'good'], stopped: ['已关闭', ''],
  failed: ['启动失败', 'bad'], exited: ['进程已退出', 'bad']};
const simRunNames = {queued: ['排队中', 'blue'], running: ['运行中', 'blue'], succeeded: ['已完成', 'good'],
  failed: ['失败', 'bad'], stopped: ['已停止', 'warn'], cancelled: ['已取消', 'warn'], timeout: ['超时', 'bad'],
  unknown: ['待核对', 'warn']};
let simGalleryCache = {};
const simTrails = {};
let simLiveBusy = false, simLiveLast = '';

function simStateBadge(r) {
  const [text, cls] = simStateNames[r.sim?.state] || [r.sim?.state || '未知', ''];
  return badge(text, cls);
}

function simKindLabel(r) {
  return r.sim?.kind === 'arm' ? '仿真六轴机械臂' : '仿真四足机器人';
}

function simRobotCard(r, latched) {
  const s = r.sim || {}, on = s.state === 'running', down = ['stopped', 'failed', 'exited'].includes(s.state);
  return `<article class="card sim-card"><div class="card-body"><div class="card-top"><span class="agent-avatar">` +
    `${icon(r.family === 'manipulator' ? 'robots' : r.family)}</span><span class="button-row">${simBadge()}${simStateBadge(r)}</span></div>` +
    `<h3>${h(r.name)}</h3><p>${h(simKindLabel(r))} · 运行在这台电脑上<br>地址和令牌由应用自动管理</p>` +
    `<div class="tags">${r.capabilities.length ? `<span class="tag">${r.capabilities.length} 项能力</span>` : ''}` +
    `${latched ? badge('急停已锁存', 'bad') : ''}${typeof safetyBadges === 'function' ? safetyBadges(r.id) : ''}</div>` +
    (s.error && down ? notice(h(s.error), true) : '') +
    `<p class="inline-note">仿真结果只说明逻辑是否正确，不能作为现场证据。</p></div>` +
    `<div class="card-bottom"><div class="button-row">` +
    (on ? button('实时状态', 'sim-live', 'small', r.id) + button('能力与技能', 'detail-robots', 'secondary small', r.id) : '') +
    (latched && on ? button('解除急停', 'robot-reset', 'secondary small', r.id + '::live') : '') +
    (down ? button('启动仿真', 'sim-start', 'small', r.id) : '') +
    (on ? button('关闭仿真', 'sim-stop', 'secondary small', r.id) : '') +
    (s.state !== 'starting' ? button('删除', 'sim-remove', 'secondary small', r.id) : '') +
    `</div></div></article>`;
}

// ---------------------------------------------------------------- add
function simAddForm() {
  const kinds = state.sim_kinds?.length ? state.sim_kinds : [
    {kind: 'quadruped', name: '仿真四足机器人', summary: ''}, {kind: 'arm', name: '仿真机械臂', summary: ''}];
  const choices = kinds.map((k, i) =>
    `<label class="sim-kind"><input type="radio" name="kind" value="${h(k.kind)}" ${i === 0 ? 'checked' : ''} required>` +
    `<span class="agent-avatar">${icon(k.kind === 'arm' ? 'robots' : 'quadruped')}</span>` +
    `<span><b>${h(k.kind === 'arm' ? '机械臂' : '四足')}</b><small>${h(k.summary)}</small></span></label>`).join('');
  return form('添加仿真机器人', '在这台电脑上启动一台虚拟机器人，用来试用技能和观察运行效果',
    `<div class="sim-kinds">${choices}</div>` +
    notice('<b>仿真</b> 仿真机器人运行在本机，不连接任何真实硬件；它的测试结果不能作为现场证据。' +
      '关闭应用时它会停止，下次打开应用会自动恢复，已安装的技能也会保留。'),
    'sim-add', '添加并启动');
}

// ---------------------------------------------------------------- gallery
async function simGalleryOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  dialog('安装技能包', h(robot.name) + ' · 选择要安装的技能', '<div class="dialog-body"><p class="subtitle">正在读取示例技能…</p></div>');
  $('#dialog').classList.add('wide');
  $('#dialog').dataset.simGallery = robotId;
  const [gallery, detail] = await Promise.all([
    api('skills/examples', {project_id: projectId, robot_id: robotId}),
    api('robots/detail', {robot_id: robotId, project_id: projectId}).catch(() => null)]);
  if (!$('#dialog').open || $('#dialog').dataset.simGallery !== robotId) return;
  gallery.items.filter(x => x.valid).forEach(x => { simGalleryCache[x.skill.id] = x; });
  const installed = new Set((detail?.skills?.items || []).map(s => s.id + '@' + s.version));
  const cards = gallery.items.filter(x => x.valid).map(x => simGalleryCard(x, robot, installed)).join('');
  const body = `<div class="section-head"><h3>示例技能</h3><span>随应用提供，不需要找文件夹</span></div>` +
    (gallery.items.length ? `<div class="gallery">${cards}</div>` : notice('这个安装包里没有示例技能。', true)) +
    simFolderSection(robotId);
  $('#dialog-content').querySelector('.dialog-body').innerHTML = body;
}

function simGalleryCard(x, robot, installed) {
  const s = x.skill;
  const mine = x.compatibility.find(c => c.robot_id === robot.id);
  const others = x.compatibility.filter(c => c.robot_id !== robot.id);
  const isInstalled = installed.has(s.id + '@' + s.version);
  const verdict = !mine ? badge('无法判断（机器人未连接）', 'warn')
    : mine.compatible ? badge('适用于这台机器人', 'good') : badge('这台机器人不能用', 'bad');
  const reasons = mine && !mine.compatible && mine.reasons.length
    ? `<ul class="reason-list">${mine.reasons.map(r => `<li>${h(r)}</li>`).join('')}</ul>` : '';
  const other = others.length ? `<p class="inline-note">其他机器人：${others.map(c =>
    h(c.robot_name) + (c.compatible ? ' ✓ 可用' : ' ✗ 不可用')).join('；')}</p>` : '';
  const action = isInstalled
    ? (robot.sim ? button('运行', 'sim-run', 'small', robot.id + '|' + s.id) : '') + badge('已安装', 'good')
    : button('安装到这台机器人', 'skillpkg-example', 'small', robot.id + '|' + x.example, !mine || !mine.compatible);
  return `<article class="gallery-item" data-example="${h(x.example)}"><div class="section-head"><h3>${h(s.title)}</h3>${verdict}</div>` +
    `<p>${h(x.note || s.summary || s.description)}</p><div class="tags">` +
    `<span class="tag">${x.moves ? '会让机器人运动 · 运行前需授权' : '只读取状态，不会运动'}</span>` +
    `${s.requires.map(r => `<span class="tag">需要：${h(r.title)}</span>`).join('')}</div>${reasons}${other}` +
    `<div class="button-row">${action}</div><small class="mono gallery-id">${h(s.id)}@${h(s.version)}</small></article>`;
}

function simFolderSection(robotId) {
  const picker = window.webkit?.messageHandlers?.nxPickFolder
    ? button('选择文件夹…', 'skillpkg-pick', 'secondary small') : '';
  return `<details class="page-purpose folder-install"><summary>从文件夹安装（高级）</summary>` +
    `<form data-form="skillpkg-preview" data-robot="${h(robotId)}" data-project="${h(projectId)}">` +
    `<div class="form-error" role="alert"></div>` +
    field('技能文件夹的完整路径', `<div class="path-row"><input name="path" placeholder="/Users/you/skills/patrol" ` +
      `value="${h(rememberedSkillPath())}" required autocomplete="off">${picker}</div>`,
      '文件夹里需要有 skill.json。' + (picker ? '' : '在“访达”中选中文件夹后按 ⌥⌘C 可复制完整路径。')) +
    `<p class="inline-note">下一步只检查兼容性，不会安装，也不会让机器人动作。平台只读取文本文件，不跟随符号链接，不包含 tests/ 和隐藏文件。</p>` +
    `<div class="button-row"><button class="button secondary small" type="submit">检查兼容性</button></div></form></details>`;
}

// Native folder picker (desktop shell): the Swift side answers with window.nxFolderPicked(path).
window.nxFolderPicked = path => {
  const input = document.querySelector('#dialog [name="path"]');
  if (input && typeof path === 'string' && path) input.value = path;
};

// ---------------------------------------------------------------- run
async function simRunForm(robotId, skillId, installedNote = false) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  const g = simGalleryCache[skillId];
  // ux02: the form is generated from the installed skill's skill.json parameters;
  // the example gallery only supplies suggested values.
  let installed = null;
  try {
    const detail = await api('robots/detail', {robot_id: robotId, project_id: projectId});
    installed = (detail.skills.items || []).find(s => s.id === skillId) || null;
  } catch { /* fall back to the gallery declaration */ }
  const decls = installed?.parameters || g?.skill.parameters || [];
  const title = installed?.title || g?.skill.title || skillId;
  const about = g?.note || installed?.summary || installed?.description || '';
  return form('运行技能', h(robot.name) + ' · ' + h(title),
    (installedNote ? `<section class="plan-review ok"><h3>技能已安装</h3><p>现在可以让机器人运行它。也可以先关闭，之后在“实时状态”或“能力与技能”里运行。</p></section>` : '') +
    (about ? `<p class="subtitle">${h(about)}</p>` : '') +
    skillParamsForm(decls, g ? g.preset : null) +
    notice('<b>仿真</b> 这是仿真机器人：运动发生在虚拟环境中。授权规则与真机相同，运行中随时可以按“停止”。') +
    `<label class="hardware-confirm"><input type="checkbox" name="hardware_authorized" required>` +
    `<span>我确认机器人周围安全，授权它执行这个技能（技能可能让机器人运动）。</span></label>`,
    'sim-run', '授权并运行',
    `data-robot="${h(robotId)}" data-skill="${h(skillId)}" data-request="${h(newRequestId())}" data-project="${h(projectId)}"`);
}

async function simRunPick(robotId) {
  const robot = find('robots', robotId);
  const detail = await api('robots/detail', {robot_id: robotId, project_id: projectId});
  const items = detail.skills.items || [];
  if (!items.length) return simGalleryOpen(robotId);
  if (!Object.keys(simGalleryCache).length) {
    try { (await api('skills/examples', {project_id: projectId})).items.filter(x => x.valid).forEach(x => { simGalleryCache[x.skill.id] = x; }); }
    catch { /* presets are a convenience */ }
  }
  return dialog('运行技能', h(robot.name) + ' · 选择一个已安装的技能', `<div class="dialog-body">` + items.map(s =>
    `<div class="contract-line"><div class="section-head"><b>${h(s.title)}</b>${button('运行', 'sim-run', 'small', robotId + '|' + s.id, !s.compatible)}</div>` +
    `<p>${h(simGalleryCache[s.id]?.note || s.summary || s.description)}</p></div>`).join('') +
    `<div class="button-row">${button('安装更多技能', 'skillpkg-open', 'secondary small', robotId)}</div></div>`);
}

// ---------------------------------------------------------------- live status
function simLiveOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  dialog(h(robot.name), '实时状态 · 每秒更新', '<div class="dialog-body" id="sim-live"><p class="subtitle">正在读取机器人状态…</p></div>');
  $('#dialog').classList.add('wide');
  $('#dialog').dataset.simLive = robotId;
  if (typeof safetyPaint === 'function') safetyPaint();
  simLiveLast = '';
  return simLiveTick(true);
}

async function simLiveTick(force = false) {
  const robotId = $('#dialog').open ? $('#dialog').dataset.simLive : '';
  if (!robotId || (simLiveBusy && !force)) return;
  simLiveBusy = true;
  try {
    const v = await api('robots/live', {robot_id: robotId, project_id: projectId});
    if (!$('#dialog').open || $('#dialog').dataset.simLive !== robotId) return;
    simTrack(v);
    const text = JSON.stringify(v, (k, x) => k === 'checked_at' ? undefined : x);
    if (text === simLiveLast && !force) return;
    simLiveLast = text;
    const el = $('#sim-live');
    if (!el) return;
    const scroll = $('#dialog').scrollTop;
    el.innerHTML = simLiveBody(v);
    $('#dialog').scrollTop = scroll;
    if (typeof safetyPaint === 'function') safetyPaint();      // its own banner is in the body: no duplicate on top
  } catch (err) {
    const el = $('#sim-live');
    if (el) el.querySelector('[data-live-error]')?.replaceChildren(document.createTextNode('暂时读不到状态：' + err.message));
  } finally {
    simLiveBusy = false;
  }
}

function simTrack(v) {
  const pose = v.state?.pose;
  if (!pose) return;
  const trail = simTrails[v.robot_id] || (simTrails[v.robot_id] = []);
  const lastPoint = trail[trail.length - 1];
  if (!lastPoint || Math.hypot(lastPoint[0] - pose.x, lastPoint[1] - pose.y) > 0.01) trail.push([pose.x, pose.y]);
  if (trail.length > 600) trail.splice(0, trail.length - 600);
}

const num = (x, d = 2) => typeof x === 'number' && isFinite(x) ? x.toFixed(d) : '—';
const deg = r => typeof r === 'number' ? Math.round(r * 180 / Math.PI) + '°' : '—';

function simMap(v) {
  const pose = v.state.pose, trail = simTrails[v.robot_id] || [];
  const pts = trail.concat([[pose.x, pose.y], [0, 0]]);
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const span = Math.max(3, Math.max(...xs) - Math.min(...xs) + 1.2, (Math.max(...ys) - Math.min(...ys) + 1.2) * 1.4);
  const W = 420, H = 300, s = W / span;
  const X = x => (W / 2 + (x - cx) * s).toFixed(1), Y = y => (H / 2 - (y - cy) * s).toFixed(1);
  const grid = [];
  for (let g = Math.floor(cx - span / 2); g <= cx + span / 2; g++) grid.push(`<line x1="${X(g)}" y1="0" x2="${X(g)}" y2="${H}"/>`);
  for (let g = Math.floor(cy - span / 2); g <= cy + span / 2; g++) grid.push(`<line x1="0" y1="${Y(g)}" x2="${W}" y2="${Y(g)}"/>`);
  const path = trail.length > 1 ? `<polyline class="sim-trail" points="${trail.map(p => X(p[0]) + ',' + Y(p[1])).join(' ')}"/>` : '';
  const yaw = pose.yaw || 0, rx = +X(pose.x), ry = +Y(pose.y), L = 14;
  const tri = [[L, 0], [-L * 0.7, L * 0.6], [-L * 0.7, -L * 0.6]].map(([a, b]) =>
    (rx + a * Math.cos(yaw) - b * Math.sin(yaw)).toFixed(1) + ',' + (ry - (a * Math.sin(yaw) + b * Math.cos(yaw))).toFixed(1)).join(' ');
  return `<svg class="sim-map" viewBox="0 0 ${W} ${H}" role="img" aria-label="俯视图：机器人位置与走过的路线">` +
    `<g class="sim-grid">${grid.join('')}</g><circle class="sim-origin" cx="${X(0)}" cy="${Y(0)}" r="4"/>` +
    `<text class="sim-label" x="${+X(0) + 7}" y="${+Y(0) - 7}">原点</text>${path}<polygon class="sim-robot" points="${tri}"/></svg>` +
    `<p class="inline-note">俯视图，每格 1 米；绿色线是机器人走过的路线。</p>`;
}

function simJoints(v) {
  const joints = v.state.joints || [];
  const bars = joints.map(j => {
    const p = typeof j.position === 'number' ? j.position : 0;
    const pct = Math.max(0, Math.min(100, (p + Math.PI) / (2 * Math.PI) * 100));
    return `<div class="joint-row"><span>${h(j.name)}</span><div class="joint-track"><i style="left:${pct.toFixed(1)}%"></i></div>` +
      `<b class="mono">${num(p)} rad · ${deg(p)}</b></div>`;
  }).join('');
  const g = v.state.gripper;
  const grip = g ? `<div class="joint-row"><span>夹爪开度</span><div class="progress-track"><i style="width:${Math.round((g.opening || 0) * 100)}%"></i></div>` +
    `<b class="mono">${Math.round((g.opening || 0) * 100)}%${g.grasped ? ' · 已夹持' : ''}</b></div>` : '';
  return `<div class="joint-list">${bars}${grip}</div><p class="inline-note">关节角范围按 ±180° 显示。</p>`;
}

// ux05 (Q6-11 #3): the body's mode in plain Chinese; the raw string stays in a tooltip.
const simModeWords = {stand: '站立', lie: '趴下', sit: '坐下', recover: '恢复站立', navigating: '正在前往目标点',
  moving: '正在运动', idle: '静止', ros2: 'ROS 2 驱动', unavailable: '读不到状态'};
function simModeText(mode) {
  if (typeof mode !== 'string' || !mode) return '—';
  const parts = mode.split(':').filter(p => p && p !== 'sim');
  const words = parts.map(p => simModeWords[p]);
  if (parts.length && words.every(Boolean)) return words.join(' · ');
  return '其他（机器人报告：' + mode + '）';
}

// A run ended by "停止" -- after the e-stop is released that is history, not news (Q6-11 #4).
const simStoppedByUser = r => r && r.status === 'stopped' && (!r.error || /停止指令|收到停止|stopped/i.test(r.error));

function simRunSection(v, robotId) {
  const r = v.run;
  if (!r) return `<div class="contract-line"><b>当前技能</b><p>还没有运行过技能。</p></div>`;
  const [label, cls] = simRunNames[r.status] || [r.status, ''];
  const active = ['queued', 'running'].includes(r.status);
  const pct = typeof r.progress === 'number' ? Math.round(r.progress * 100) : null;
  const stopped = simStoppedByUser(r), released = stopped && !v.estop?.latched;
  const msgs = (r.messages || []).filter(m => !(released && /停止指令|收到停止/.test(m.text))).slice(-5).reverse().map(m => `<li>${h(m.text)}</li>`).join('');
  return `<div class="contract-line sim-run" data-run-status="${h(r.status)}"><div class="section-head"><b>${active ? '当前技能' : '上一次技能'} ·${h(r.target || '')}</b>${badge(label, cls)}</div>` +
    (pct !== null ? `<div class="progress-track"><i style="width:${pct}%"></i></div><p>进度 ${pct}%</p>` : '') +
    (msgs ? `<ul class="run-log">${msgs}</ul>` : '') +
    (stopped ? `<p class="inline-note" data-run-stopped>${released ? '这次运行之前被“停止”中断了；急停已经解除，可以再运行技能。' : '这次运行被“停止”中断了。'}</p>`
      : r.error ? notice(h(r.error), true) : '') +
    (r.stop_reason && !active && !stopped && !r.error && r.stop_reason !== 'stopped' ? `<p>停止原因：${h(r.stop_reason)}</p>` : '') +
    (!active && r.result ? `<details><summary>技能返回的结果</summary>${json(r.result)}</details>` : '') +
    `<details class="tech-detail"><summary>技术细节</summary><code>运行编号 ${h(r.id)}</code></details></div>`;
}

function simLiveBody(v) {
  const robotId = v.robot_id;
  const proc = v.process ? `${badge((simStateNames[v.process.state] || [v.process.state])[0], (simStateNames[v.process.state] || [])[1] || '')}` : '';
  if (!v.reachable) {
    const down = v.process && ['stopped', 'failed', 'exited'].includes(v.process.state);
    return `<div class="detail-meta"><div><b>机器人</b>${h(v.name)} ${v.simulated ? simBadge() : ''}</div><div><b>仿真进程</b>${proc || '—'}</div></div>` +
      notice(h(v.error || '暂时读不到状态'), true) +
      `<div class="button-row">${down ? button('启动仿真', 'sim-start', '', robotId) : ''}${button('请求停止', 'simlive-stop', 'danger', robotId)}</div>`;
  }
  const st = v.state || {}, latched = v.estop?.latched, active = v.run && ['queued', 'running'].includes(v.run.status);
  const meta = `<div class="detail-meta sim-meta"><div><b>机器人</b>${h(v.name)} ${v.simulated ? simBadge() : ''}</div>` +
    `<div><b>急停</b>${latched ? badge('已锁存', 'bad') : badge('未锁存', 'good')}</div>` +
    `<div><b>状态</b><span data-mode title="${h(st.mode || v.mode || '')}">${h(simModeText(st.mode || v.mode))}</span></div>` +
    `<div><b>电量</b>${typeof st.battery === 'number' ? Math.round(st.battery) + '%' : '—'}</div></div>`;
  const stuck = v.reset_in_progress === true;
  const estop = latched ? `<section class="plan-review estop-latched"><h3>急停已锁存</h3><p>${h(v.estop.reason || '')}。` +
    `机器人不会再接受运动指令，直到你在现场确认安全并解除急停。</p>` + simLatchGuardHtml(v.latched_motion, {desktop_sim: Boolean(v.process), simulated: v.simulated}) +
    // ux05-fix2 (Q7b-05/08): "void after the limit" only when this runtime advertises it.
    (stuck ? `<p class="reset-stuck" data-reset-stuck data-reset-expire="${v.reset?.reset_expire === true ? 'supported' : 'unsupported'}">上一次“解除急停”（复位）还在执行，机器人暂时不接受新的复位。` +
      `${h(uxLateResetText(v.reset, false))}一直这样时，重启这台仿真机器人（急停锁存会保留），再解除急停。</p>` : '') +
    `<div class="button-row">${stuck && v.process ? button('重启仿真机器人', 'sim-restart', '', robotId) : ''}${button('解除急停', 'robot-reset', stuck ? 'secondary' : '', robotId + '::live', active)}</div></section>` : '';
  const pose = st.pose ? `<div class="pose-line"><span>X <b class="mono">${num(st.pose.x)} m</b></span><span>Y <b class="mono">${num(st.pose.y)} m</b></span>` +
    `<span>朝向 <b class="mono">${deg(st.pose.yaw)}</b></span>${st.velocity ? `<span>速度 <b class="mono">${num(Math.hypot(st.velocity[0], st.velocity[1]))} m/s</b></span>` : ''}</div>` : '';
  const view = st.pose ? simMap(v) + pose : st.joints ? simJoints(v) : notice('这台机器人没有提供位置或关节状态。');
  const controls = `<div class="sim-controls">${button('停止', 'simlive-stop', 'danger', robotId)}` +
    `${button('运行技能', 'sim-run-pick', 'secondary', robotId, latched || active)}` +
    `${button('能力与技能', 'detail-robots', 'secondary', robotId)}</div>` +
    (latched || active ? `<p class="button-reason" data-run-disabled>${latched ? '“运行技能”暂不可用：急停已锁存，先在现场确认安全并解除急停。' : '“运行技能”暂不可用：当前技能还在运行，结束或停止后再运行新的。'}</p>` : '') +
    `<p class="inline-note">“停止”立即让机器人停下并锁存急停；之后需要确认现场安全并解除急停，才能再次运动。${v.process || v.simulated ? '仿真机器人停不下来时：点“重启仿真机器人”或关闭仿真。' : '软件停止不能替代真机的硬件急停。'}</p>`;
  return simStopNotObeyedHtml(v) + meta + estop + `<div class="detail-split"><div>${view}</div><div>${simRunSection(v, robotId)}${controls}</div></div>` +
    `<p class="inline-note" data-live-error>最近更新 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}</p>`;
}

// ---------------------------------------------------------------- ux05: latch guard in plain words
// /v1/status.latched_motion (INTERFACES §7-§8), counted for this e-stop by the desktop (brainos/sim_robots.py).
function simLatchGuardLines(lm) {
  if (!lm || !lm.enabled) return [];
  const lines = [];
  const since = lm.since === 'runtime_start' ? '（自机器人运行时启动以来）' : '';
  const motion = Math.max(0, (lm.restops || 0) - (lm.invalid_restops || 0));
  if (motion > 0) lines.push(['bad', `急停期间检测到机器人仍在移动，已再次停止 ${motion} 次${since}`]);
  if ((lm.invalid_restops || 0) > 0) lines.push(['bad', `反馈数据异常，按可能在动处理，已再次停止 ${lm.invalid_restops} 次${since}`]);
  else if (lm.feedback_ok === false) lines.push(['warn', '反馈数据异常，按可能在动处理']);
  if (typeof lm.read_in_progress_s === 'number' && lm.read_in_progress_s >= 1) {
    lines.push(['warn', `读取机器人反馈已卡住 ${Math.round(lm.read_in_progress_s)} 秒，按可能在动处理`]);
  }
  if (lm.drift_notice) lines.push(['warn', lm.drift_notice.message || '急停期间机器人离停下位置已超过首次发现界，请到现场确认']);
  if (lm.displaced && typeof lm.max_drift_m === 'number') {
    lines.push(['warn', `急停后机器人离开了停下时的位置，最远约 ${Math.round(lm.max_drift_m * 100)} 厘米`]);
  }
  return lines;
}

// who: {desktop_sim, simulated, hardware_estop} -- a simulated body has no hardware e-stop (Q7-11).
function simLatchGuardHtml(lm, who = {}) {
  if (!lm || !lm.enabled) return '';
  const lines = simLatchGuardLines(lm);
  if (!lines.length) {
    return `<p class="latch-guard ok" data-latch-guard="quiet">锁存守护正在看着：急停期间没有发现机器人移动。</p>`;
  }
  return `<div class="latch-guard" data-latch-guard="alert" role="status"><b>锁存守护</b><ul>${lines.map(([cls, t]) =>
    `<li class="${cls}">${h(t)}</li>`).join('')}</ul>` +
    (who.desktop_sim || who.simulated
      ? `<p>软件只能再发停止。仿真机器人如果仍在动：点“重启仿真机器人”或关闭仿真。</p></div>`
      : `<p>请在现场确认机器人确实停住了。软件只能再发停止，如果它仍在动，请使用硬件急停${who.hardware_estop === false ? '（这台机器人声明没有硬件急停：切断动力）' : ''}。</p></div>`);
}

// safety.stop_not_obeyed (round 07): the robot keeps moving after repeated stops. Say it first and loudly.
function simStopNotObeyedHtml(v) {
  if (!v.stop_not_obeyed) return '';
  const e = v.stop_not_obeyed;
  const when = typeof e.ts === 'number' ? new Date(e.ts * 1000).toLocaleTimeString('zh-CN', {hour12: false}) : '';
  // Q7-11: a simulated robot has no hardware e-stop -- restart or close the simulation instead.
  const sim = Boolean(v.process) || v.simulated;
  const who = {desktop_sim: Boolean(v.process), simulated: v.simulated, robot_id: v.robot_id};
  return `<section class="stop-not-obeyed${sim ? ' sim' : ''}" role="alert" data-stop-not-obeyed><h3>${sim ? '仿真机器人在“停止”后仍在移动' : '请立即使用硬件急停'}</h3>` +
    `<p>机器人在“停止”后没有停下来：运行时已经多次发出停止，它仍在运动。${h(safetyAdvice(who))}</p>` +
    (who.desktop_sim ? `<div class="button-row">${button('重启仿真机器人', 'sim-restart', 'small', v.robot_id)}${button('关闭仿真', 'sim-stop', 'secondary small', v.robot_id)}</div>` : '') +
    (when ? `<small>机器人报告时间 ${h(when)}</small>` : '') +
    (e.note || e.message ? `<details class="tech-detail"><summary>技术细节</summary><code>${h(e.note || e.message)}</code></details>` : '') +
    `</section>`;
}

setInterval(() => { if ($('#dialog').open && $('#dialog').dataset.simLive) simLiveTick(); }, 1000);

// ---------------------------------------------------------------- dispatch
async function simAction(type, id) {
  if (type === 'sim-add') return simAddForm();
  if (type === 'sim-live') return simLiveOpen(id);
  if (type === 'sim-run-pick') return simRunPick(id);
  if (type === 'sim-run') { const [robotId, skillId] = String(id).split('|'); return simRunForm(robotId, skillId); }
  if (type === 'sim-start') {
    toast('正在启动仿真机器人…');
    await api('sims/start', {robot_id: id});
    await refresh(true);
    toast('仿真机器人已启动');
    if ($('#dialog').open && $('#dialog').dataset.simLive === id) return simLiveTick(true);
    return;
  }
  if (type === 'sim-restart') return uxSimRestart(id);
  if (type === 'sim-stop') {
    await api('sims/stop', {robot_id: id});
    await refresh(true);
    return toast('仿真机器人已关闭；需要时可以再启动');
  }
  if (type === 'sim-remove') {
    const r = find('robots', id);
    return dialog('删除仿真机器人', h(r?.name || ''), `<div class="dialog-body"><p>仿真机器人会被关闭并从项目中删除，它上面安装的技能和运行记录也会一起删除。</p>` +
      `<div class="dialog-footer">${button('取消', 'close', 'secondary')}${button('删除', 'sim-remove-confirm', 'danger', id)}</div></div>`);
  }
  if (type === 'sim-remove-confirm') {
    await api('sims/remove', {robot_id: id});
    $('#dialog').close();
    await refresh(true);
    return toast('仿真机器人已删除');
  }
  if (type === 'simlive-stop') {
    // Sent at once: no confirmation stands between the user and a stop.
    const result = await api('robots/stop', {robot_id: id, project_id: projectId});
    toast(result.status === 'accepted' ? '机器人已停止，急停已锁存' : '停止结果待核对：' + (result.error || ''));
    await refresh(true);
    return simLiveTick(true);
  }
}

async function simSubmit(kind, p, f) {
  if (kind === 'sim-add') {
    toast('正在启动仿真机器人…');
    const robot = await api('sims', {kind: p.kind, project_id: projectId || undefined});
    projectId = robot.project_id;
    try { sessionStorage.setItem('brain-project', projectId); } catch { /* optional */ }
    location.hash = 'robots';
    await refresh(true);
    toast('仿真机器人已启动并连接');
    return robotDetailOpen(robot.id);
  }
  if (kind === 'sim-run') {
    const parameters = skillParamsCollect(f);
    const result = await api('robots/run-skill', {robot_id: f.dataset.robot, skill_id: f.dataset.skill, parameters,
      hardware_authorized: f.elements.hardware_authorized.checked, request_id: f.dataset.request, project_id: projectId})
      .catch(err => { f.dataset.request = newRequestId(); throw err; });
    // ux04: keep the device's code and field (e.g. param_out_of_range on loops) for the explanation.
    if (result.status !== 'accepted') { f.dataset.request = newRequestId(); throw uxOpError(result, '设备没有接受运行请求'); }
    toast('技能开始运行');
    return simLiveOpen(f.dataset.robot);
  }
}
