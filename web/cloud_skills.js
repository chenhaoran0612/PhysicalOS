'use strict';
// Issue #43: the 「技能发布」 and 「技能加载」 pages. Skill packages are kept in the cloud platform, private
// to the organisation. Publishing and loading are two pages (the issue asks for that):
//  * 技能发布 lists what the organisation has published, publishes a local skill folder (or the app's own
//    examples), shows the versions of a skill and deletes one. Nothing on it touches a robot.
//  * 技能加载 lists the same library and installs one version on a robot: the backend downloads it, checks
//    its digest, explains whether it fits the robot, and only then sends it through the same install as a
//    local folder. Nothing on it changes the library. After a successful install the flow goes on in
//    skillInstallResult (web/robot_detail.js), as every install does.
// Loaded after web/app.js and web/robot_detail.js. It adds its pages to the navigation, listens for its
// own data-cloudskill-* attributes and wraps robotDetailRender, so no other page script is edited.
// Titles, descriptions and notes come from other people: every one of them goes through h().

const CLOUD_SKILLS_ROUTE = 'cloud-skills';            // 技能发布
const CLOUD_SKILLS_LOAD_ROUTE = 'cloud-skill-load';   // 技能加载
const CLOUD_SKILLS_PAGE_NAMES = {[CLOUD_SKILLS_ROUTE]: '技能发布', [CLOUD_SKILLS_LOAD_ROUTE]: '技能加载'};
const CLOUD_SKILLS_DIGEST_SHOWN = 12;
const CLOUD_SKILLS_SIGN_IN = '技能库保存在云平台。请先登录云平台账户，再发布技能。';
const CLOUD_SKILLS_LOAD_SIGN_IN = '技能库保存在云平台。请先登录云平台账户，再把技能安装到机器人上。';
const CLOUD_SKILLS_PUBLISH_STATES = {
  new: ['可以发布', '云端还没有这个技能的这个版本。发布后，只有你的组织能看到它。'],
  same: ['云端已经有完全相同的这个版本', '内容一模一样，不需要再发布。'],
  conflict: ['不能发布：这个版本在云端已经有了，内容不同', '版本发布后不能改。请在 skill.json 里提高版本号（例如 0.1.0 → 0.1.1），再重新发布。'],
};
const CLOUD_SKILLS_EXAMPLE_STATES = {published: ['已发布', 'good'], existing: ['已在云端', 'good'], conflict: ['云端的同一版本内容不同', 'warn'],
  refused: ['没有发布', 'bad'], invalid: ['示例本身有问题', 'bad']};

let cloudSkills = {loaded: false, pending: null, loadedFor: '', error: '', skills: [], examples: [], lastPath: ''};

// ---------------------------------------------------------------- small rules (no DOM)
function cloudSkillSize(bytes) {
  const value = Number(bytes) || 0;
  return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}

// The cloud writes a time as seconds or as text; '—' for anything else.
function cloudSkillWhen(value) {
  const at = typeof value === 'number' && Number.isFinite(value) ? new Date(value * 1000) : typeof value === 'string' && value ? new Date(value) : null;
  if (!at || Number.isNaN(at.getTime())) return '—';
  return at.toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
}

// What an example row says: [text, tone]. `published` and `same_content` are the backend's words.
function cloudSkillExampleState(example) {
  if (!example.valid) return ['示例本身有问题', 'bad'];
  if (!example.published) return ['还没有发布', ''];
  return example.same_content === false ? ['云端的同一版本内容不同', 'warn'] : ['已在云端', 'good'];
}

// Why the folder form is not sent ('' when it can be). The backend applies its own rules again.
function cloudSkillPathProblem(path) {
  const text = String(path ?? '').trim();
  if (!text) return '请填写技能文件夹的完整路径。';
  if (!text.startsWith('/')) return '请使用完整路径（以 / 开头），例如 /Users/you/skills/patrol。';
  return '';
}

// Robots of the current project a release can be installed on: connected ones.
function cloudSkillRobots(robots, project) {
  return (robots || []).filter(r => r.project_id === project && r.status === 'connected');
}

// Who published a release, for showing. The cloud records an account or key number, which says nothing to a reader.
function cloudSkillPublisher(createdBy) {
  const text = String(createdBy ?? '');
  if (text.startsWith('key:')) return '经 API Key 发布';
  return /^user:/.test(text) ? '' : text;
}

function cloudSkillMotionBadge(release) {
  return release.motion ? badge('会让机器人运动', 'warn') : badge('不会让机器人运动', 'good');
}

// ---------------------------------------------------------------- a second dialog on top of the first
// The robot's page is itself a dialog; a confirmation asked from it must not replace it. This one opens
// above (the browser's top layer keeps the order) and is shared with remote_calls.js and robot_bundle.js.
function subDialog(title, sub, body) {
  let box = document.getElementById('sub-dialog');
  if (!box) {
    box = document.createElement('dialog');
    box.id = 'sub-dialog';
    document.body.append(box);
  }
  box.innerHTML = `<div class="dialog-head"><div><h2>${title}</h2><p>${sub}</p></div>` +
    `<button type="button" class="close-dialog" aria-label="关闭" data-sub-dialog-close>×</button></div>${body}`;
  if (!box.open) box.showModal();
  return box;
}

function subDialogClose() {
  const box = document.getElementById('sub-dialog');
  if (box?.open) box.close();
}

// An error of a step started from a dialog is shown inside that dialog: a toast would be hidden behind it.
function dialogError(root, err) {
  let box = root?.querySelector('.form-error');
  const body = root?.querySelector('.dialog-body');
  if (!box && body) {                       // a dialog drawn by another script: give it a place for the message
    body.insertAdjacentHTML('afterbegin', '<div class="form-error" role="alert"></div>');
    box = body.firstElementChild;
  }
  if (!box) return toast(err.message);
  if (typeof formError === 'function') formError(box, err);
  else box.textContent = err.message;
  box.scrollIntoView?.({block: 'nearest'});
}

const cloudSkillButton = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-cloudskill-action="${action}" ${id !== '' ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;

// ---------------------------------------------------------------- the two pages
// Which of the two pages is showing: the same library, different things to do with it.
const cloudSkillOnLoadPage = () => current === CLOUD_SKILLS_LOAD_ROUTE;
const cloudSkillOnAPage = () => current === CLOUD_SKILLS_ROUTE || current === CLOUD_SKILLS_LOAD_ROUTE;

function cloudSkillCard(row, loading = cloudSkillOnLoadPage()) {
  const latest = row.latest, more = row.versions.length - 1;
  return `<article class="card cloud-skill-card" data-cloudskill="${h(row.id)}"><div class="card-body">` +
    `<div class="card-top"><span class="agent-avatar">${icon('skills')}</span>${cloudSkillMotionBadge(latest)}</div>` +
    `<h3>${h(row.title)}</h3><p class="cloud-skill-key"><span class="mono">${h(row.id)}</span> · 最新版本 ${h(latest.version)}` +
    `${more > 0 ? ` · 另有 ${more} 个版本` : ''}</p><p>${h(row.summary)}</p>` +
    `<div class="tags">${latest.requires.map(r => `<span class="tag" title="${h(r.id)}">${h(r.title)}</span>`).join('')}</div></div>` +
    `<div class="card-bottom"><small>${h(cloudSkillWhen(latest.created_at))}</small><div class="button-row">` +
    (loading ? cloudSkillButton('版本', 'versions-load', 'secondary small', row.id) +
      cloudSkillButton('安装到机器人', 'install', 'small', row.id + '|' + latest.version)
      : cloudSkillButton('版本', 'versions', 'secondary small', row.id)) + `</div></div></article>`;
}

function cloudSkillList(loading = cloudSkillOnLoadPage()) {
  const lib = cloudSkills;
  if (lib.error) return `<section class="panel">${notice(h(lib.error) + ' ' + cloudSkillButton('重试', 'refresh', 'secondary small'), true)}</section>`;
  if (!lib.loaded) return `<section class="panel">${notice('正在读取云端技能库…')}</section>`;
  if (!lib.skills.length && loading) {
    return `<section class="panel">${empty('云端技能库里还没有技能', '先在“技能发布”页把技能发布到云端，再回到这里安装到机器人上。',
      button('去技能发布页', 'go-' + CLOUD_SKILLS_ROUTE, 'secondary small'))}</section>`;
  }
  if (!lib.skills.length) {
    return `<section class="panel">${empty('云端技能库还是空的', '把自己写的技能文件夹发布上来，或者先把应用自带的示例技能发布到自己的技能库。',
      cloudSkillButton('发布应用自带的示例技能', 'examples', 'secondary small'))}</section>`;
  }
  return `<div class="cards">${lib.skills.map(row => cloudSkillCard(row, loading)).join('')}</div>`;
}

function cloudSkillExamples() {
  const examples = cloudSkills.examples;
  if (!cloudSkills.loaded || !examples.length) return '';
  const rows = examples.map(e => {
    const [text, tone] = cloudSkillExampleState(e);
    return `<div class="cloud-skill-example" data-cloudskill-example="${h(e.example)}"><div><b>${h(e.title || e.example)}</b>` +
      `<span class="cell-sub">${e.valid ? `${h(e.skill_id)} · 版本 ${h(e.version)}` : h(e.error)}</span>` +
      `${e.valid && e.summary ? `<p>${h(e.summary)}</p>` : ''}</div>${badge(text, tone)}</div>`;
  }).join('');
  const missing = examples.filter(e => e.valid && !e.published).length;
  return '<div class="spacer"></div>' + panel('应用自带的示例技能', `<div class="cloud-skill-examples">${rows}</div>`,
    cloudSkillButton(missing ? `一键发布到我的技能库（${missing} 个还没有发布）` : '再发布一次', 'examples', missing ? 'small' : 'secondary small'));
}

// The part both pages share: the count and the list, redrawn in place by cloudSkillsDraw.
function cloudSkillLibrary(title, hint, loading) {
  return `<div class="section-head"><h2>${title} <span class="badge-count" id="cloud-skill-count">${cloudSkills.skills.length}</span></h2>` +
    `<span>${hint}</span></div><div id="cloud-skill-list">${cloudSkillList(loading)}</div>`;
}

function cloudSkillsWanted() {
  const lib = cloudSkills;
  if ((!lib.loaded || lib.loadedFor !== cloudSignInKey()) && !lib.pending) setTimeout(cloudSkillsLoad);
}

// 技能发布: what is in the cloud and how it gets there. No robot is involved.
function cloudSkillsPage() {
  const account = state.cloud_account || {logged_in: false};
  const title = ['PUBLISH SKILLS', CLOUD_SKILLS_PAGE_NAMES[CLOUD_SKILLS_ROUTE], '把技能包发布到云平台，只有你的组织能看到。发布不会改动任何机器人；要把技能装到机器人上，去“技能加载”页。'];
  if (!account.logged_in) return heading(...title) + cloudSignInNotice(CLOUD_SKILLS_SIGN_IN);
  cloudSkillsWanted();
  return heading(...title, cloudSkillButton('发布技能', 'publish') + cloudSkillButton('刷新', 'refresh', 'secondary')) +
    cloudSkillLibrary('已发布的技能', `版本发布后不能改；改了内容请提高版本号再发布 ${button('去技能加载页', 'go-' + CLOUD_SKILLS_LOAD_ROUTE, 'secondary small')}`, false) +
    `<div id="cloud-skill-examples">${cloudSkillExamples()}</div>`;
}

// 技能加载: from the cloud onto a robot. Nothing here publishes or deletes.
function cloudSkillLoadPage() {
  const account = state.cloud_account || {logged_in: false};
  const title = ['LOAD SKILLS', CLOUD_SKILLS_PAGE_NAMES[CLOUD_SKILLS_LOAD_ROUTE], '把云端技能库里的一个版本安装到机器人上。发布和删除版本在“技能发布”页。'];
  if (!account.logged_in) return heading(...title) + cloudSignInNotice(CLOUD_SKILLS_LOAD_SIGN_IN);
  cloudSkillsWanted();
  return heading(...title, cloudSkillButton('刷新', 'refresh', 'secondary')) +
    cloudSkillLibrary('可以加载的技能', '安装前会在这台电脑上核对内容，并检查机器人是否具备技能需要的能力', true);
}

// The library changed: redraw the list, the count and (技能发布 only) the examples in place. False when
// neither page is showing them.
function cloudSkillsDraw() {
  const list = document.querySelector('#cloud-skill-list'), count = document.querySelector('#cloud-skill-count');
  if (!cloudSkillOnAPage() || !list || !count) return false;
  list.innerHTML = cloudSkillList();
  count.textContent = String(cloudSkills.skills.length);
  const examples = document.querySelector('#cloud-skill-examples');
  if (examples) examples.innerHTML = cloudSkillExamples();
  return true;
}

async function cloudSkillsFetch() {
  const lib = cloudSkills;
  try {
    const result = await api('cloud-skills/overview', {});
    lib.error = result.error || '';
    lib.skills = result.skills || [];
    lib.examples = result.examples || [];
  } catch (err) {
    lib.error = err.message;
  }
  lib.loaded = true;
  lib.loadedFor = cloudSignInKey();
  if (!cloudSkillsDraw() && cloudSkillOnAPage()) render();
}

function cloudSkillsLoad() {
  const lib = cloudSkills;
  if (!lib.pending) lib.pending = cloudSkillsFetch().finally(() => { lib.pending = null; });
  return lib.pending;
}

// ---------------------------------------------------------------- publishing
function cloudSkillPublishForm(value = '') {
  dialog('发布技能到云端', '第 1 步：选择技能文件夹',
    `<form data-cloudskill-form="publish-preview" novalidate><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    field('技能文件夹的完整路径', `<input name="path" placeholder="/Users/you/skills/patrol" value="${h(value || cloudSkills.lastPath)}" autocomplete="off">`,
      '文件夹里需要有 skill.json。在“访达”中选中文件夹后按 ⌥⌘C 可复制完整路径。') +
    notice('下一步只读取和检查这个文件夹，不会发布。平台只读取文本文件，不跟随符号链接，不包含 tests/ 和隐藏文件。') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">读取并检查</button></div></form>`);
}

function cloudSkillPublishBody(r) {
  const s = r.skill, [head, text] = CLOUD_SKILLS_PUBLISH_STATES[r.state] || CLOUD_SKILLS_PUBLISH_STATES.new;
  const files = `<details class="page-purpose cloud-skill-files"><summary>将要发布的文件（${r.files.length} 个，共 ${cloudSkillSize(r.total_bytes)}）</summary>` +
    r.files.map(f => `<p class="mono">${h(f.path)} · ${f.bytes} 字节</p>`).join('') +
    `<p class="inline-note">来源：${r.example ? '应用自带的示例技能' : h(r.path)}</p></details>`;
  return `<div class="detail-meta"><div><b>技能</b>${h(s.title)} <span class="mono">${h(s.id)}@${h(s.version)}</span></div>` +
    `<div><b>运动</b>${cloudSkillMotionBadge(r)}</div></div><p class="subtitle">${h(s.summary || s.description)}</p>` +
    `<section class="plan-review ${r.can_publish ? 'ok' : 'blocked'}" data-cloudskill-state="${h(r.state)}"><h3>${h(head)}</h3><p>${h(text)}</p></section>` +
    `<div class="contract-line"><b>需要机器人能做到</b><div class="tags">${s.requires.map(x =>
      `<span class="tag" title="${h(x.id)}">${h(x.title)}</span>`).join('')}</div></div>${files}`;
}

function cloudSkillPublishShow(r) {
  const sub = '第 2 步：确认要发布的内容';
  if (r.state !== 'new') {
    return dialog('发布技能到云端', sub, `<div class="dialog-body">${cloudSkillPublishBody(r)}</div>` +
      `<div class="dialog-footer">${cloudSkillButton('选择其他文件夹', 'publish', 'secondary', r.path || '')}${button('关闭', 'close')}</div>`);
  }
  dialog('发布技能到云端', sub,
    `<form data-cloudskill-form="publish" data-path="${h(r.path)}" data-digest="${h(r.digest)}"><div class="dialog-body">` +
    `<div class="form-error" role="alert"></div>${cloudSkillPublishBody(r)}` +
    field('这个版本的说明（可选）', '<textarea name="notes" maxlength="2000" placeholder="例如：第一次发布；修正了巡逻路线的终点"></textarea>') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">发布到云端</button></div></form>`);
}

async function cloudSkillPublishExamples() {
  const result = await api('cloud-skills/publish-examples', {});
  const rows = result.results.map(r => {
    const [text, tone] = CLOUD_SKILLS_EXAMPLE_STATES[r.status] || [r.status, ''];
    return `<div class="cloud-skill-example"><div><b>${h(r.title)}</b>${r.error ? `<span class="cell-sub">${h(r.error)}</span>` : ''}</div>${badge(text, tone)}</div>`;
  }).join('');
  dialog('发布应用自带的示例技能', `${result.published} / ${result.results.length} 个已经在你的技能库里`,
    `<div class="dialog-body"><div class="cloud-skill-examples">${rows}</div></div><div class="dialog-footer">${button('完成', 'close')}</div>`);
  return cloudSkillsLoad();
}

// ---------------------------------------------------------------- versions of one skill
// One version in the versions dialog. Opened from 技能发布 it can be deleted, from 技能加载 installed.
function cloudSkillReleaseRow(release, loading = false) {
  const key = release.skill_id + '|' + release.version;
  return `<div class="contract-line cloud-skill-release" data-cloudskill-release="${h(release.version)}"><div class="section-head">` +
    `<b>版本 ${h(release.version)} ${cloudSkillMotionBadge(release)}</b><div class="button-row">` +
    `${loading ? cloudSkillButton('安装到机器人', 'install', 'small', key) : cloudSkillButton('删除', 'delete', 'secondary small', key)}</div></div>` +
    `<p>${h(cloudSkillWhen(release.created_at))}${cloudSkillPublisher(release.created_by) ? ' · ' + h(cloudSkillPublisher(release.created_by)) : ''} · ${release.files.length} 个文件，` +
    `${cloudSkillSize(release.size)} · 摘要 <span class="mono" title="${h(release.digest)}">${h(release.digest.slice(0, CLOUD_SKILLS_DIGEST_SHOWN))}…</span></p>` +
    `${release.notes ? `<p class="cloud-skill-notes">${h(release.notes)}</p>` : ''}</div>`;
}

async function cloudSkillVersions(skillId, loading = false) {
  const result = await api('cloud-skills/detail', {skill_id: skillId});
  const title = result.releases[0]?.title || skillId;
  dialog(h(title), `${h(skillId)} · 云端的 ${result.releases.length} 个版本`,
    `<div class="dialog-body"><div class="form-error" role="alert"></div>${result.releases.map(r => cloudSkillReleaseRow(r, loading)).join('') ||
      empty('这个技能在云端已经没有版本了', '可能刚被删除。')}</div><div class="dialog-footer">${button('关闭', 'close')}</div>`);
}

function cloudSkillDeleteAsk(key) {
  const [skillId, version] = String(key).split('|');
  dialog('删除这个版本？', `${h(skillId)}@${h(version)}`, `<div class="dialog-body">` +
    notice('云端的这个版本会被删除，不能恢复。已经装到机器人上的技能不受影响。', true) +
    `<div class="form-error" role="alert"></div></div><div class="dialog-footer">${cloudSkillButton('返回', 'versions', 'secondary', skillId)}` +
    `${cloudSkillButton('删除', 'delete-confirm', 'danger', key)}</div>`);
}

// ---------------------------------------------------------------- installing on a robot
// Choose the robot (and the skill and version when they are not given). `fixed.robotId` comes from a robot's page.
async function cloudSkillInstallForm(fixed) {
  if (!project()) return toast('请先创建或选择一个项目');
  if (!state.cloud_account?.logged_in) {
    return dialog('从云端安装技能', '云端技能库', `<div class="dialog-body">${cloudSignInNotice(CLOUD_SKILLS_LOAD_SIGN_IN)}</div>`);
  }
  if (!cloudSkills.loaded || cloudSkills.loadedFor !== cloudSignInKey()) await cloudSkillsLoad();
  const robots = cloudSkillRobots(state.robots, projectId), skills = cloudSkills.skills;
  if (!skills.length) {
    return dialog('从云端安装技能', '云端技能库', `<div class="dialog-body">${empty('云端技能库里还没有技能',
      h(cloudSkills.error) || '先在“技能发布”页发布一个技能，或把应用自带的示例技能发布上去。', button('去技能发布页', 'go-' + CLOUD_SKILLS_ROUTE, 'secondary small'))}</div>`);
  }
  if (!robots.length) {
    return dialog('从云端安装技能', '选择机器人', `<div class="dialog-body">${empty('当前项目里没有已连接的机器人',
      '先在“我的机器人”里接入一台机器人（或添加一台仿真机器人），连接检查通过后再安装。', button('去我的机器人', 'go-robots', 'secondary small'))}</div>`);
  }
  const chosen = skills.find(s => s.id === fixed.skillId) || skills[0];
  const options = skills.flatMap(s => s.versions.map(v => [s.id + '|' + v, `${s.title}（${s.id}）· 版本 ${v}`]));
  dialog('从云端安装技能', '第 1 步：选择技能和机器人',
    `<form data-cloudskill-form="install-preview"><div class="dialog-body"><div class="form-error" role="alert"></div>` +
    field('技能和版本', select('release', options, chosen.id + '|' + (fixed.version || chosen.latest.version))) +
    field('安装到哪台机器人', select('robot_id', robots.map(r => [r.id, r.name]), fixed.robotId || '')) +
    notice('下一步会从云端下载这个版本，在这台电脑上核对内容，并检查它和这台机器人是否兼容。不会安装，也不会让机器人动作。') +
    `</div><div class="dialog-footer">${button('取消', 'close', 'secondary')}<button class="button" type="submit">下载并检查</button></div></form>`);
}

function cloudSkillInstallShow(r) {
  const title = '从云端安装技能', sub = h(r.robot.name) + ' · 第 2 步：确认兼容性';
  const source = `云端技能库 · 摘要 ${r.package_digest.slice(0, CLOUD_SKILLS_DIGEST_SHOWN)}…（已在这台电脑上核对，与云端记录一致）`;
  const body = skillPreviewBody({...r, example: null, path: source});
  const back = cloudSkillButton('选择其他技能或机器人', 'install', 'secondary', r.skill.id + '|' + r.skill.version + '|' + r.robot.id);
  if (!r.can_install) {
    return dialog(title, sub, `<div class="dialog-body">${body}<p class="inline-note">可以换一台具备这些能力的机器人。</p></div>` +
      `<div class="dialog-footer">${back}${button('关闭', 'close')}</div>`);
  }
  dialog(title, sub,
    `<form data-cloudskill-form="install" data-robot="${h(r.robot.id)}" data-skill="${h(r.skill.id)}" data-version="${h(r.skill.version)}" ` +
    `data-digest="${h(r.package_digest)}" data-request="${h(newRequestId())}" data-project="${h(projectId)}"><div class="dialog-body">` +
    `<div class="form-error" role="alert"></div>${body}` +
    notice('安装只把技能文件写入机器人的运行时，不会启动技能，也不会让机器人动作。') +
    `</div><div class="dialog-footer">${back}<button class="button" type="submit">确认安装到机器人</button></div></form>`);
}

// ---------------------------------------------------------------- actions
async function cloudSkillAction(type, id) {
  if (type === 'refresh') { cloudSkills.loaded = false; cloudSkills.error = ''; cloudSkillsDraw(); return cloudSkillsLoad(); }
  if (type === 'publish') return cloudSkillPublishForm(id || '');
  if (type === 'examples') return cloudSkillPublishExamples();
  if (type === 'versions') return cloudSkillVersions(id);
  if (type === 'versions-load') return cloudSkillVersions(id, true);
  if (type === 'delete') return cloudSkillDeleteAsk(id);
  if (type === 'install') {
    const [skillId, version, robotId] = String(id || '').split('|');
    return cloudSkillInstallForm({skillId, version, robotId});
  }
  if (type === 'install-robot') return cloudSkillInstallForm({robotId: id});
  if (type === 'delete-confirm') {
    const [skillId, version] = String(id).split('|');
    await api('cloud-skills/delete', {skill_id: skillId, version});
    toast('这个版本已从云端删除');
    await cloudSkillsLoad();
    return cloudSkills.skills.some(s => s.id === skillId) ? cloudSkillVersions(skillId) : $('#dialog').close();
  }
}

async function cloudSkillSubmit(kind, form) {
  const data = Object.fromEntries(new FormData(form));
  if (kind === 'publish-preview') {
    const problem = cloudSkillPathProblem(data.path);
    if (problem) throw Error(problem);
    cloudSkills.lastPath = data.path.trim();
    return cloudSkillPublishShow(await api('cloud-skills/publish-preview', {path: data.path.trim()}));
  }
  if (kind === 'publish') {
    const result = await api('cloud-skills/publish', {path: form.dataset.path, digest: form.dataset.digest, notes: data.notes || ''});
    $('#dialog').close();
    toast(result.digest_matches ? `已发布到云端：${result.release.skill_id}@${result.release.version}`
      : '已发布，但云端算出的摘要和这台电脑不一致：安装这个版本时会被拒绝，请联系平台运营人员');
    return cloudSkillsLoad();
  }
  if (kind === 'install-preview') {
    const [skillId, version] = String(data.release).split('|');
    return cloudSkillInstallShow(await api('cloud-skills/install-preview',
      {robot_id: data.robot_id, skill_id: skillId, version, project_id: projectId}));
  }
  if (kind === 'install') {
    if (form.dataset.project !== projectId) throw Error('项目已切换，请重新打开');
    const result = await api('cloud-skills/install', {robot_id: form.dataset.robot, skill_id: form.dataset.skill, version: form.dataset.version,
      package_digest: form.dataset.digest, request_id: form.dataset.request, project_id: projectId,
      authorized: true});      // pressing 「确认安装到机器人」 on the preview is the person's consent
    await refresh(true);
    return skillInstallResult(result, form.dataset.robot);      // the same ending as an install from a local folder
  }
}

document.addEventListener('click', e => {
  if (e.target.closest('[data-sub-dialog-close]')) return subDialogClose();
  const el = e.target.closest('[data-cloudskill-action]');
  if (!el || el.disabled) return;
  e.preventDefault();
  el.disabled = true;
  cloudSkillAction(el.dataset.cloudskillAction, el.dataset.id)
    .catch(err => dialogError($('#dialog').open ? $('#dialog') : null, err))
    .finally(() => { el.disabled = false; });
});
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-cloudskill-form]');
  if (!form) return;
  e.preventDefault();
  const submit = form.querySelector('[type="submit"]'), error = form.querySelector('.form-error');
  submit.disabled = true;
  error.textContent = '';
  cloudSkillSubmit(form.dataset.cloudskillForm, form).catch(err => dialogError(form, err)).finally(() => { submit.disabled = false; });
});

// A robot's page: one more way to install, next to "安装技能包".
function cloudSkillAttach(robot) {
  const row = document.querySelector('#dialog-content #robot-skills .button-row');
  if (!row || !robot || robot.status !== 'connected' || row.querySelector('[data-cloudskill-action]')) return;
  row.insertAdjacentHTML('beforeend', cloudSkillButton('从云端安装', 'install-robot', 'secondary small', robot.id));
}

{
  const baseRender = robotDetailRender;
  robotDetailRender = d => { baseRender(d); cloudSkillAttach(d.robot); };
}

// Register the two pages: two more entries at the end of the navigation, their icons and their views.
icons[CLOUD_SKILLS_ROUTE] = 'M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1.3A4.5 4.5 0 0 1 17 18z M12 11v5 M9.5 13.5 12 11l2.5 2.5';
icons[CLOUD_SKILLS_LOAD_ROUTE] = 'M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1.3A4.5 4.5 0 0 1 17 18z M12 11v5 M9.5 13.5 12 16l2.5-2.5';
navigation.push([CLOUD_SKILLS_ROUTE, CLOUD_SKILLS_PAGE_NAMES[CLOUD_SKILLS_ROUTE]]);
navigation.push([CLOUD_SKILLS_LOAD_ROUTE, CLOUD_SKILLS_PAGE_NAMES[CLOUD_SKILLS_LOAD_ROUTE]]);
views[CLOUD_SKILLS_ROUTE] = cloudSkillsPage;
views[CLOUD_SKILLS_LOAD_ROUTE] = cloudSkillLoadPage;
if (Object.hasOwn(CLOUD_SKILLS_PAGE_NAMES, location.hash.slice(1))) route();      // app.js routed before this file was loaded
