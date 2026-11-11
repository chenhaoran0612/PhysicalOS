'use strict';
// 第 02 轮（INTERFACES §4.5）：机器人详情里的标准能力表，以及从本地文件夹安装技能包。

const safetyBadgeClass = {none: 'good', motion: 'warn', system: 'blue'};

function robotDetailIsCurrent(id) {
  return $('#dialog').open && $('#dialog').dataset.robotDetail === id;
}

async function robotDetailOpen(id, focus = '') {
  const robot = find('robots', id);
  if (!robot) throw Error('找不到该机器人');
  dialog(h(robot.name), '机器人能力与技能', '<div class="dialog-body"><p class="subtitle">正在读取设备能力清单…</p></div>');
  $('#dialog').classList.add('wide');
  $('#dialog').dataset.robotDetail = id;
  if (typeof safetyPaint === 'function') safetyPaint();     // its own alert is on the page: none on top
  const detail = await api('robots/detail', {robot_id: id, project_id: projectId});
  if (!robotDetailIsCurrent(id)) return;          // the user moved on
  robotDetailRender(detail);
  if (typeof safetyPoll === 'function') safetyPoll(id);
  if (focus === 'skills') $('#robot-skills')?.scrollIntoView({block: 'start'});
}

function capParams(row) {
  if (!row.params.length) return '';
  const items = row.params.map(p =>
    `<span class="${p.declared ? '' : 'muted'}" title="${h(p.description)}"><b>${h(p.name)}</b> ${h(p.text)}</span>`);
  return `<div class="cap-params">${items.join('')}</div>`;
}

function capRow(row) {
  // ux01: lead with the Chinese name and what it lets the robot do; the raw ID is secondary.
  const declaredAs = row.declared_as ? `<small>设备声明为 ${h(row.declared_as)}</small>` : '';
  const mandatory = row.mandatory ? '<span class="tag">必备</span>' : '';
  const safety = row.safety ? badge(row.safety_name, safetyBadgeClass[row.safety] || '') : badge('安全级未知', 'bad');
  return `<div class="cap-row${row.standard ? '' : ' cap-nonstandard'}">` +
    `<div class="cap-name"><b>${h(row.title)}</b>${mandatory}</div>` +
    `<p class="cap-does">${h(row.does || row.description)}</p>` +
    `<div class="cap-tags">${badge(row.kind_name)}${safety}</div>` +
    `<details class="tech-detail cap-tech"><summary>技术细节</summary><p class="cap-desc"><span class="mono">${h(row.id)}</span>${declaredAs}` +
    `${row.does && row.does !== row.description ? ' · ' + h(row.description) : ''}</p>${capParams(row)}</details></div>`;
}

function capabilitySection(caps) {
  const motion = caps.rows.filter(r => r.safety === 'motion').length;
  const head = `<div class="section-head"><h3>它能做什么 <span class="badge-count">${caps.rows.length}</span></h3>` +
    `<span title="${h(caps.source_name)}">由机器人自己报告</span></div>`;
  if (!caps.rows.length) return head + notice(h(caps.note || '尚无能力信息'));
  const summary = `<p class="inline-note">其中 ${motion} 项会让机器人运动，调用前需要现场授权，并受设备端安全监督（限速、限位、急停）。</p>` +
    `<details class="tech-detail"><summary>技术细节</summary><p class="inline-note">来源：${h(caps.source_name)}；名称、类型与安全级来自 NeuxMind 能力标准 v${h(caps.local_spec_version)}。</p></details>`;
  const versionNote = caps.spec_version && caps.spec_version !== caps.local_spec_version
    ? notice(`设备按能力标准 v${h(caps.spec_version)} 声明，本机为 v${h(caps.local_spec_version)}；名称可能有差异。`, true) : '';
  return head + (caps.note ? notice(h(caps.note)) : '') + versionNote + summary +
    `<div class="cap-list">${caps.rows.map(capRow).join('')}</div>`;
}

function installedSkillsSection(robot, skills) {
  const head = `<div class="section-head" id="robot-skills"><h3>已安装技能包 <span class="badge-count">${skills.items.length}</span></h3>` +
    `<div class="button-row">${robot.sim ? button('实时状态', 'sim-live', 'secondary small', robot.id) : ''}` +
    button('安装技能包', 'skillpkg-open', 'small', robot.id, !skills.available) + '</div></div>';
  if (!skills.available) return head + notice(h(skills.error || '暂不能读取已安装技能'));
  if (!skills.items.length) {
    return head + empty('这台机器人还没有安装技能包', '可以从示例技能里直接选一个安装；也可以从本地技能文件夹安装。平台会先检查兼容性。');
  }
  return head + skills.items.map(s =>
    `<div class="contract-line"><div class="section-head"><b>${h(s.title)} <span class="mono skill-key">${h(s.id)}@${h(s.version)}</span>` +
    `${s.compatible ? '' : ' ' + badge('与当前本体不兼容', 'bad')}</b>` +
    `${robot.sim ? button('运行', 'sim-run', 'small', robot.id + '|' + s.id, !s.compatible) : ''}</div>` +
    `<p>${h(s.summary || s.description || '')}</p><div class="tags">${s.requires.map(r => `<span class="tag">${h(r.title)}</span>`).join('')}</div></div>`
  ).join('') + (robot.sim ? '' : `<p class="inline-note">真机上的技能通过“机器人应用”完成现场测试后部署运行。</p>`);
}

function robotDetailRender(d) {
  const robot = d.robot, caps = d.capabilities;
  const simulated = caps.simulated || robot.manifest?.simulated;
  const meta = `<div class="detail-meta"><div><b>机器人</b>${h(robot.name)}${simulated ? ' ' + simBadge() : ''}</div>` +
    `<div><b>形态</b>${h(familyNames[robot.family] || robot.family)}</div>` +
    `<div><b>连接</b>${status(robot.status)}${robotEstop(robot).latched ? ' ' + badge('急停已锁存', 'bad') : ''}</div>` +
    `<div><b>厂商 / 型号</b>${h(caps.vendor || '—')} / ${h(caps.model || '—')}</div></div>`;
  const simNote = simulated ? notice('<b>仿真本体</b> ' + h(SIMULATION_NOTE) + '。', true) : '';
  const cloudLink = `<div class="contract-line"><b>需要模型？</b><p>在云平台账户中查看适合这台机器人能力的模型。</p>` +
    `${button('查看云平台可用模型', 'cloud-models-for', 'secondary small', robot.id)}</div>`;
  const advanced = caps.source === 'v2'
    ? `<details class="page-purpose"><summary>设备安全设置与端侧推理运行时</summary>` +
      `<p class="inline-note">端侧推理运行时：${h((caps.model_runtimes || []).join('、') || '未声明')}</p>${json(caps.safety)}</details>`
    : '';
  $('#dialog-content').querySelector('.dialog-body').innerHTML =
    meta + simNote +
    // ux05-fix (Q7-03/Q7-05): e-stop, latch guard, "stop not obeyed" and calibration, for real robots too.
    (typeof safetyDetailSection === 'function' && robot.status === 'connected' ? safetyDetailSection(robot.id) : '') +
    // §6.2: the latest conformance summary sits next to the capability table.
    `<div class="detail-split"><div>${capabilitySection(caps)}</div><div>${adaptConformanceSummary(adaptLatestReport(robot.id), {run: adaptRunFor(robot.id)})}</div></div>` +
    installedSkillsSection(robot, d.skills) + cloudLink + advanced +
    `<div class="button-row">${button('重新读取', 'detail-robots', 'secondary small', robot.id)}` +
    `${button('连接检查', 'robot-connect', 'secondary small', robot.id)}</div>`;
}

// ---------------------------------------------------------------- skill packages
function rememberedSkillPath() {
  try { return sessionStorage.getItem('brain-skill-path') || ''; } catch { return ''; }
}

function skillFolderForm(robotId, value = '') {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  form('安装技能包', h(robot.name) + ' · 第 1 步：选择技能文件夹',
    field('技能文件夹的完整路径',
      `<input name="path" placeholder="/Users/you/skills/patrol" value="${h(value || rememberedSkillPath())}" required autocomplete="off">`,
      '文件夹里需要有 skill.json。在“访达”中选中文件夹后按 ⌥⌘C 可复制完整路径。') +
    notice('下一步只检查技能包与这台机器人是否兼容，不会安装，也不会让机器人动作。平台只读取文本文件，' +
      '不跟随符号链接，不包含 tests/ 和隐藏文件。'),
    'skillpkg-preview', '检查兼容性', `data-robot="${h(robotId)}" data-project="${h(projectId)}"`);
}

async function skillPackageAction(type, id) {
  if (type === 'skillpkg-open') return simGalleryOpen(id);
  if (type === 'skillpkg-folder') return skillFolderForm(id);
  if (type === 'skillpkg-example') {
    const [robotId, example] = String(id).split('|');
    const r = await api('skills/package-preview', {robot_id: robotId, example, project_id: projectId});
    return skillPreviewShow(r, robotId);
  }
  if (type === 'skillpkg-pick') {
    // Desktop shell only: native folder picker, answered through window.nxFolderPicked(path).
    window.webkit?.messageHandlers?.nxPickFolder?.postMessage('pick');
    return;
  }
  if (type === 'skillpkg-back') {
    const [robotId, path] = String(id).split('|');
    if (!path) return simGalleryOpen(robotId);
    return skillFolderForm(robotId, path);
  }
}

function compatibilityReasons(c) {
  const reasons = c.missing_detail.map(m => m.text).concat(c.violation_texts);
  if (!reasons.length) return '';
  return `<ul class="reason-list">${reasons.map(r => `<li>${h(r)}</li>`).join('')}</ul>`;
}

function skillPreviewBody(r) {
  const s = r.skill, c = r.compatibility;
  const verdict = `<section class="plan-review ${c.compatible ? 'ok' : 'blocked'}"><h3>${c.compatible ? '兼容：可以安装' : '不兼容：不能安装'}</h3>` +
    `<p>${c.compatible ? '这台机器人具备技能需要的全部能力，参数也满足技能的要求。'
      : '原因如下。不兼容的技能包不会发送到机器人。'}</p>${compatibilityReasons(c)}</section>`;
  const notes = c.notes.map(n => `<p class="inline-note">${h(n)}</p>`).join('');
  const requires = `<div class="contract-line"><b>需要机器人能做到</b><div class="tags">${s.requires.map(x =>
    `<span class="tag" title="${h(x.id)}">${h(x.title)}</span>`).join('')}</div>` +
    (s.optional.length ? `<p>有更好（不是必需）：${h(s.optional.map(x => x.title).join('、'))}</p>` : '') +
    `<details class="tech-detail"><summary>技术细节</summary><code>${h(s.requires.map(x => x.id).concat(s.optional.map(x => x.id + '（可选）')).join('、'))}</code></details></div>`;
  const params = s.parameters.length
    ? `<div class="contract-line preview-params"><b>运行时要填的参数</b>${s.parameters.map(x => {
        const info = uxParamInfo(x);
        const bits = [uxTypeName(x)];
        if (info.unit) bits.push('单位 ' + info.unit);
        if (x.min !== undefined || x.max !== undefined) bits.push('范围 ' + (x.min ?? '不限') + ' – ' + (x.max ?? '不限'));
        if (uxOptions(x).length) bits.push('可选 ' + uxOptionLabels(x).join(' / '));
        bits.push(x.required ? '必填' : '可不填');
        return `<p data-preview-param="${h(x.name)}">${uxParamLabel(x)} · ${h(bits.join(' · '))}${info.description ? ' — ' + h(info.description) : ''}</p>`;
      }).join('')}</div>`
    : '';
  const files = `<details class="page-purpose"><summary>将要安装的文件（${r.files.length} 个，共 ${r.total_bytes} 字节）</summary>` +
    r.files.map(f => `<p class="mono">${h(f.path)} · ${f.bytes} 字节</p>`).join('') +
    `<p class="inline-note">来源：${r.example ? '应用自带的示例技能' : h(r.path)}</p></details>`;
  return `<div class="detail-meta"><div><b>技能</b>${h(s.title)} <span class="mono">${h(s.id)}@${h(s.version)}</span></div>` +
    `<div><b>目标机器人</b>${h(r.robot.name)}${r.robot.simulated ? ' ' + simBadge() : ''}</div></div>` +
    `<p class="subtitle">${h(s.summary || s.description)}</p>` + verdict + notes + requires + params + files;
}

function skillPreviewShow(r, robotId) {
  const title = '安装技能包', sub = h(r.robot.name) + ' · 第 2 步：确认兼容性';
  if (!r.can_install) {
    return dialog(title, sub, `<div class="dialog-body">${skillPreviewBody(r)}` +
      `<p class="inline-note">可以换一台具备这些能力的机器人，或修改技能的 requires / constraints 后重新检查。</p></div>` +
      `<div class="dialog-footer">${button('返回机器人详情', 'detail-robots', 'secondary', robotId)}` +
      `${button(r.example ? '返回技能列表' : '选择其他文件夹', 'skillpkg-back', '', robotId + '|' + (r.example ? '' : r.path))}</div>`);
  }
  form(title, sub, skillPreviewBody(r) +
    notice('安装只把技能文件写入机器人的运行时，不会启动技能，也不会让机器人动作。'),
    'skillpkg-install', '确认安装到机器人',
    `data-robot="${h(robotId)}" data-path="${h(r.path)}" data-example="${h(r.example || '')}" data-digest="${h(r.package_digest)}" ` +
    `data-request="${h(newRequestId())}" data-project="${h(projectId)}"`);
}

function skillInstallResult(result, robotId) {
  const messages = {
    succeeded: ['技能包已安装', result.response?.already_installed ? '机器人上已有完全相同的版本，未重复写入。' : '机器人已确认安装。'],
    rejected: ['设备拒绝安装', result.error],
    failed: ['请求未发出', result.error],
    unknown: ['安装结果待核对', result.error],
  };
  const [head, text] = messages[result.status] || ['安装结果', result.error || ''];
  if (result.status === 'succeeded') {
    toast(`${head}：${result.skill_id}@${result.version}`);
    if (find('robots', robotId)?.sim) return simRunForm(robotId, result.skill_id, true);
    return robotDetailOpen(robotId, 'skills');
  }
  dialog('安装技能包', h(result.skill_id) + '@' + h(result.version),
    `<div class="dialog-body"><section class="plan-review blocked"><h3>${h(head)}</h3><p>${h(text)}</p></section>` +
    `${button('返回机器人详情', 'detail-robots', 'secondary', robotId)}</div>`);
}

async function skillPackageSubmit(kind, p, f) {
  if (f.dataset.project !== projectId) throw Error('项目已切换，请重新打开');
  const robotId = f.dataset.robot;
  if (kind === 'skillpkg-preview') {
    try { sessionStorage.setItem('brain-skill-path', p.path); } catch { /* optional convenience */ }
    const r = await api('skills/package-preview', {robot_id: robotId, path: p.path, project_id: projectId});
    return skillPreviewShow(r, robotId);
  }
  if (kind === 'skillpkg-install') {
    const result = await api('skills/package-install', {
      robot_id: robotId, path: f.dataset.example ? undefined : f.dataset.path, example: f.dataset.example || undefined,
      package_digest: f.dataset.digest,
      request_id: f.dataset.request, project_id: projectId,
    });
    await refresh(true);
    return skillInstallResult(result, robotId);
  }
}
