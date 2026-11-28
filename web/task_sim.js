'use strict';
// Issues #66 and #75 — 仿真试运行 is by task, and every skill of a task has its own environment.
//
//   * The page (#simulation) lists the account's tasks. Choosing one lists that task's skills: the ones its
//     applications use (with the values of their steps), the duck's skill models, the other skills of its brand and
//     model, and what is installed on its simulated robot.
//   * Choosing a skill opens that skill's simulation. Its environment is built once — written by the engineering agent
//     for that one skill, or an empty floor — checked and saved with the skill (issue #75: 「仿真环境构建基于单个技能」).
//     The example robots need nothing built, and each skill model of the duck is performed on the stage it was trained on.
//   * 「进入仿真」 starts the task's own simulated robot in that skill's environment (or moves it there). Inside: the live
//     picture, what the robot is doing, 「停止」, and the skill with 「运行」.
//
// Rules and data: brainos/task_sim.py (routes task-sim/…). Running a skill, the live state, the picture and stopping
// are the routes every simulated robot has (web/sim.js, web/mujoco_sim.js, web/duck.js). What changes while one
// watches is asked for by this file and drawn in place: the block is one element that is kept and put back after every
// redraw of the page (as web/duck.js does).
// The functions down to tsimSkillPanel are pure (data in, facts or markup out): tests/task_sim_logic.cjs.

const TSIM_ROUTE = 'simulation';
const TSIM_KEY = 'brain-sim-task';
const TSIM_SKILL_KEY = 'brain-sim-skill';
const TSIM_LIST_MS = 5000, TSIM_VIEW_MS = 2500, TSIM_BUSY_MS = 1200, TSIM_LIVE_MS = 1000, TSIM_FRAME_MS = 500;
const TSIM_BUILD = {preparing: '正在准备机器人模型…', generating: '工程助手正在为这个技能写仿真环境，一般一两分钟…', checking: '正在检查环境并生成预览…'};
const TSIM_ROBOT = {starting: '正在启动', running: '运行中', stopped: '已关闭', failed: '启动失败', exited: '进程已退出'};
const TSIM_ACTIVE = ['queued', 'running'];
const TSIM_NONE = {skills: 0, ready: 0, building: 0, failed: 0};

// ---------------------------------------------------------------- pure
const tsimEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

// Where a task's simulation stands, for its card: {key, text, tone}. `row.counts` counts its skills' environments.
function tsimStatus(row) {
  const c = row.counts || TSIM_NONE;
  if (c.building) return {key: 'building', text: '正在构建', tone: 'blue'};
  if (row.model && row.model.staged && !row.problem && c.skills) return {key: 'ready', text: '可以直接进入', tone: 'good'};
  if (c.skills && c.ready >= c.skills) return {key: 'saved', text: '每个技能都有环境', tone: 'good'};
  if (c.ready) return {key: 'partial', text: `${c.ready} / ${c.skills} 个技能有环境`, tone: 'good'};
  if (c.failed) return {key: 'failed', text: '上次构建没有成功', tone: 'bad'};
  if (row.problem) return {key: 'blocked', text: '还不能仿真', tone: 'warn'};
  if (!c.skills) return {key: 'empty', text: '还没有技能', tone: 'warn'};
  return {key: 'none', text: '还没有构建', tone: ''};
}

// What the button of a task's card does.
function tsimCardAction(row) {
  return tsimStatus(row).key === 'blocked' ? {label: '查看原因', primary: false} : {label: '选择技能', primary: true};
}

// Where one skill's environment stands: {key, text, tone}.
function tsimSkillStatus(skill) {
  const build = skill.build, env = skill.environment;
  if (build && build.busy) return {key: 'building', text: '正在构建', tone: 'blue'};
  if (env && env.stale) return {key: 'stale', text: '品牌型号变了，需要重新构建', tone: 'warn'};
  if (env && env.staged) return {key: 'ready', text: '自带场地', tone: 'good'};
  if (env) return {key: 'saved', text: '已保存 · ' + (env.source_name || ''), tone: 'good'};
  if (build && build.status === 'failed') return {key: 'failed', text: '上次构建没有成功', tone: 'bad'};
  return {key: 'none', text: '还没有环境', tone: ''};
}

// What the button of a skill's card does.
function tsimSkillAction(skill) {
  const s = tsimSkillStatus(skill).key;
  if (s === 'saved' || s === 'ready') return {label: '进入仿真', primary: true};
  if (s === 'building') return {label: '查看进度', primary: false};
  return {label: s === 'stale' ? '重新构建环境' : '构建环境', primary: true};
}

// Issue #74, per skill since #75: whether there is anything to run in yet. Until there is, the page shows no list of
// simulated runs (web/run_boards.js puts it at the foot, web/task_sim.css hides it). On a task's list of skills: one of
// them has an environment; on a skill's page: that skill has one.
function tsimBuilt(view, skill = null) {
  if (!view) return false;
  if (skill) return Boolean(skill.environment && !skill.environment.stale);
  return Boolean((view.model && view.model.staged && (view.counts || TSIM_NONE).skills) || (view.counts || TSIM_NONE).ready);
}

// A value of a step, short enough for one line.
function tsimValue(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 60 ? text.slice(0, 57) + '…' : text;
}
function tsimUseLine(use) {
  const values = Object.entries(use.parameters || {}).map(([k, v]) => `${k} = ${tsimValue(v)}`).join('，');
  return `应用「${use.application || ''}」第 ${use.step} 步${values ? '：' + values : ''}`;
}

// Why the skill cannot be run now ('' when it can). `facts.here`: the task's robot runs in this skill's environment.
function tsimRunBlock(skill, facts) {
  if (!facts.robot) return '进入仿真后可以运行';
  if (!facts.robot.connected) return '仿真机器人没有在运行';
  if (!facts.here) return '仿真机器人在另一个技能的环境里；先进入这个技能的仿真';
  if (facts.staging) return '正在换到这个技能的场地';
  if (!skill.installed) return skill.has_package ? '还没有安装到仿真机器人上' : '这台电脑上没有它的技能包';
  if (!skill.compatible) return '这台仿真机器人做不了这个技能';
  if (facts.latched) return '急停已锁存，先解除急停';
  if (facts.active) return '有技能正在运行';
  return '';
}

// The panel of the skill on its own page: what it is, the steps that use it, 「运行」 per step, 「安装」.
function tsimSkillPanel(skill, facts) {
  const why = tsimRunBlock(skill, facts);
  const chip = skill.installed ? (skill.compatible ? ['已安装', 'good'] : ['不兼容', 'bad']) : ['未安装', 'warn'];
  const uses = skill.uses || [];
  const runs = (uses.length ? uses : [null]).map((use, i) =>
    `<button type="button" class="button small${i ? ' secondary' : ''}" data-tsim-run="${tsimEsc(skill.id)}" data-use="${use ? i : ''}" ${why ? 'disabled' : ''}>` +
    `${uses.length > 1 ? '按第 ' + use.step + ' 步运行' : '运行'}</button>`).join('');
  const install = facts.here && facts.robot && facts.robot.connected && !skill.installed
    ? (skill.has_package ? `<button type="button" class="button small secondary" data-tsim-install="${tsimEsc(skill.id)}">安装</button>`
      : `<button type="button" class="button small secondary" data-action="skillpkg-open" data-id="${tsimEsc(facts.robot.id)}">从文件夹安装</button>`) : '';
  return `<section class="tsim-panel tsim-skill" data-tsim-skill="${tsimEsc(skill.id)}"><div class="tsim-skill-main"><h2>运行这个技能 ` +
    `<span class="status ${chip[1]}"><i class="dot"></i>${chip[0]}</span></h2>` +
    (skill.summary ? `<p>${tsimEsc(skill.summary)}</p>` : '') +
    (uses.length ? `<ul class="tsim-uses">${uses.map(u => `<li>${tsimEsc(tsimUseLine(u))}</li>`).join('')}</ul>` : `<p class="tsim-quiet">${tsimEsc(skill.origin_name || '')}；任务的应用没有用到它。</p>`) +
    (why ? `<p class="tsim-quiet" data-tsim-why>${tsimEsc(why)}</p>` : '') +
    `<code class="tsim-id">${tsimEsc(skill.id)}</code></div><div class="tsim-skill-actions">${runs}${install}</div></section>`;
}

// ---------------------------------------------------------------- state of the page
const tsim = {task: '', skill: '', list: null, listError: '', view: null, viewError: '', live: null, duck: null, root: null, drawn: {},
  busy: {}, timers: {}, chains: {}, frame: 0, frameBusy: false, formOpen: false, entering: false, removing: false};

function tsimRemember(key, value) {
  try { value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key); } catch { /* optional */ }
}
function tsimTaskId() {
  if (!tsim.task) { try { tsim.task = sessionStorage.getItem(TSIM_KEY) || ''; tsim.skill = tsim.task ? sessionStorage.getItem(TSIM_SKILL_KEY) || '' : ''; } catch { tsim.task = ''; } }
  if (tsim.task && typeof state === 'object' && state.projects && state.user && last && !state.projects.some(p => p.id === tsim.task)) tsimChoose('');
  return tsim.task;
}
function tsimLeaveSkill() {
  tsim.live = null; tsim.duck = null; tsim.formOpen = false; tsim.removing = false; tsim.entering = false; tsim.drawn = {};
}
function tsimChoose(id) {
  tsim.task = id || '';
  tsim.view = null; tsim.viewError = '';
  tsimChooseSkill('');
  tsimRemember(TSIM_KEY, tsim.task);
}
function tsimChooseSkill(id) {
  tsim.skill = id || '';
  tsimLeaveSkill();
  tsimRemember(TSIM_SKILL_KEY, tsim.skill);
}
const tsimSkillOf = view => (view && tsim.skill ? (view.skills || []).find(s => s.id === tsim.skill) || null : null);

// ---------------------------------------------------------------- markup: the list of tasks
function tsimPicture(row, scene = row.preview) {
  if (scene) return `<img class="tsim-thumb" alt="" loading="lazy" width="640" height="480" src="/api/sim/mujoco/preview?id=${encodeURIComponent(scene)}">`;
  const profile = (state.sim_catalog?.profiles || []).find(p => row.product && spCanonSame(p, row.product));
  return profile && profile.image ? `<img class="tsim-thumb tsim-thumb-robot" alt="" loading="lazy" width="320" height="320" src="/robot_images/${encodeURIComponent(profile.image)}.webp">`
    : '<div class="tsim-thumb tsim-thumb-none" aria-hidden="true"></div>';
}
function spCanonSame(profile, product) {
  const canon = typeof spCanon === 'function' ? spCanon(product) : product;
  return profile.vendor.toLowerCase() === String(canon.vendor).toLowerCase() && profile.model.toLowerCase() === String(canon.model).toLowerCase();
}

function tsimCard(row) {
  const s = tsimStatus(row), act = tsimCardAction(row), c = row.counts || TSIM_NONE;
  return `<li class="tsim-card" data-tsim-task="${tsimEsc(row.task.id)}" data-tsim-status="${s.key}"><div class="tsim-card-shot">${tsimPicture(row)}` +
    `<span class="status ${s.tone}"><i class="dot"></i>${tsimEsc(s.text)}</span></div>` +
    `<div class="tsim-card-body"><h3>${tsimEsc(row.task.name)}</h3><p class="tsim-objective">${tsimEsc(row.task.objective || '')}</p>` +
    `<dl><div><dt>品牌型号</dt><dd>${row.label ? tsimEsc(row.label) : '还没有确定'}</dd></div>` +
    `<div><dt>绑定的应用</dt><dd>${row.applications.length ? tsimEsc(row.applications.map(a => a.name).join('、')) : '还没有'}</dd></div>` +
    `<div><dt>技能</dt><dd>${c.skills ? `${c.skills} 个，${c.ready} 个有仿真环境` : '还没有'}</dd></div></dl>` +
    (row.problem ? `<p class="tsim-problem">${tsimEsc(row.problem.message)}</p>` : '') +
    `<button type="button" class="button small${act.primary ? '' : ' secondary'}" data-tsim-open="${tsimEsc(row.task.id)}">${act.label}</button></div></li>`;
}

function tsimListMarkup() {
  if (tsim.listError && !tsim.list) return `<div class="notice bad">${tsimEsc(tsim.listError)}</div>`;
  if (!tsim.list) return '<p class="tsim-quiet">正在读取任务…</p>';
  if (!tsim.list.tasks.length) {
    return '<div class="tsim-none"><b>还没有任务</b><p>仿真是按任务做的：先在工作台创建一个任务并绑定应用，再回到这里。</p>' +
      '<button type="button" class="button small" data-action="go-overview">去工作台</button></div>';
  }
  return `<ul class="tsim-cards">${tsim.list.tasks.map(tsimCard).join('')}</ul>` +
    '<p class="tsim-quiet tsim-foot">任务里的每个技能有自己的仿真环境，构建一次后保存，以后直接进入。仿真结果只说明逻辑是否正确，不能作为现场证据。</p>';
}

// ---------------------------------------------------------------- markup: one task, its skills
function tsimTaskHead(view) {
  const s = tsimStatus(view);
  return '<div class="tw-back"><a href="#simulation" data-tsim-back>← 全部任务</a></div>' +
    `<div class="tsim-head"><div><div class="eyebrow">SIMULATION TRIALS</div><h1>${tsimEsc(view.task.name)}</h1>` +
    `<p>${view.label ? tsimEsc(view.label) : '还没有确定品牌型号'}${view.model ? ' · ' + tsimEsc(view.model.name) : ''}` +
    `${view.applications.length ? ' · 应用：' + tsimEsc(view.applications.map(a => a.name).join('、')) : ''}</p></div>` +
    `<span class="status ${s.tone}"><i class="dot"></i>${tsimEsc(s.text)}</span></div>`;
}

function tsimProblem(view) {
  if (!view.problem) return '';
  const where = view.problem.code === 'task_sim_no_product' ? '<button type="button" class="button small" data-tsim-task-page>去任务页面绑定应用</button>'
    : view.problem.code === 'sim_no_robot_model' ? '<button type="button" class="button small secondary" data-action="go-library">去资料库</button>' : '';
  return `<section class="tsim-panel tsim-blocked"><h2>还不能仿真</h2><p>${tsimEsc(view.problem.message)}</p><div class="button-row">${where}</div></section>`;
}

const tsimSkillTitle = (view, id) => ((view.skills || []).find(s => s.id === id) || {}).title || id;

function tsimRobotStrip(view) {
  const robot = view.robot;
  if (!robot) return '';
  const where = robot.connected && robot.skill ? `，在「${tsimEsc(tsimSkillTitle(view, robot.skill))}」的环境里` : '';
  return `<div class="tsim-strip"><p><b>${tsimEsc(robot.name)}</b> · ${tsimEsc(robot.connected ? '运行中' : TSIM_ROBOT[robot.state] || '没有在运行')}${where}</p>` +
    (robot.connected ? '<button type="button" class="button small secondary" data-tsim-leave>关闭仿真</button>' : '') + '</div>';
}

function tsimSkillCard(view, skill) {
  const s = tsimSkillStatus(skill), act = tsimSkillAction(skill), here = view.robot && view.robot.connected && view.robot.skill === skill.id;
  const uses = skill.uses || [];
  return `<li class="tsim-skill-card${here ? ' is-here' : ''}" data-tsim-skill-card="${tsimEsc(skill.id)}" data-tsim-status="${s.key}">` +
    `<div class="tsim-card-shot">${tsimPicture({product: view.product}, skill.environment && skill.environment.preview ? skill.environment.scene_id : null)}` +
    `<span class="status ${s.tone}"><i class="dot"></i>${tsimEsc(s.text)}</span>${here ? '<span class="tsim-here">仿真机器人在这里</span>' : ''}</div>` +
    `<div class="tsim-card-body"><h3>${tsimEsc(skill.title)}</h3><p class="tsim-origin">${tsimEsc(skill.origin_name || '')}` +
    `${uses.length ? ` · ${uses.length} 处步骤` : ''}</p>` +
    (skill.summary ? `<p class="tsim-objective">${tsimEsc(skill.summary)}</p>` : '') +
    `<button type="button" class="button small${act.primary ? '' : ' secondary'}" data-tsim-skill-open="${tsimEsc(skill.id)}">${act.label}</button></div></li>`;
}

function tsimSkillList(view) {
  const skills = view.skills || [];
  const none = view.problem ? '' : '<div class="tsim-none"><b>这个任务还没有技能</b><p>给它绑定一个应用，或者给这个品牌型号装上技能，技能就会列在这里，每个技能各自构建仿真环境。</p>' +
    '<button type="button" class="button small" data-tsim-task-page>去任务页面</button></div>';
  return tsimProblem(view) + tsimRobotStrip(view) +
    (skills.length ? `<div class="section-head tsim-skills-head"><h2>任务的技能 <span class="badge-count">${skills.length}</span></h2>` +
      '<span>每个技能有自己的仿真环境：选一个技能，构建或进入它的仿真</span></div>' +
      `<ul class="tsim-cards tsim-skill-cards">${skills.map(s => tsimSkillCard(view, s)).join('')}</ul>` : none);
}

// ---------------------------------------------------------------- markup: one skill
function tsimFacts(view, skill) {
  const live = tsim.live, robot = view?.robot || null;
  const here = Boolean(robot && robot.connected && robot.skill === skill.id);
  const staging = Boolean(here && robot.duck && tsim.duck && tsim.duck.staging);
  return {robot, here, staging, latched: Boolean(live?.estop?.latched), active: Boolean(live?.run && TSIM_ACTIVE.includes(live.run.status))};
}

function tsimSkillHead(view, skill) {
  const s = tsimSkillStatus(skill);
  return `<div class="tw-back"><a href="#simulation" data-tsim-up>← ${tsimEsc(view.task.name)}</a></div>` +
    `<div class="tsim-head"><div><div class="eyebrow">SIMULATION TRIALS · SKILL</div><h1>${tsimEsc(skill.title)}</h1>` +
    `<p>任务「${tsimEsc(view.task.name)}」 · ${view.label ? tsimEsc(view.label) : '还没有确定品牌型号'} · ${tsimEsc(skill.origin_name || '')}</p></div>` +
    `<span class="status ${s.tone}"><i class="dot"></i>${tsimEsc(s.text)}</span></div>`;
}

function tsimBuildForm(skill) {
  return '<form class="tsim-form" data-tsim-form><div class="form-error" role="alert"></div>' +
    '<label class="tsim-field"><span>补充说明（可选）</span><textarea name="note" maxlength="2000" rows="3" ' +
    'placeholder="例如：巡逻的三个地点在走廊两侧，中间有一根柱子要绕开"></textarea></label>' +
    '<label class="tsim-consent"><input type="checkbox" name="data_authorized"> 同意把这个任务的说明、用到这个技能的应用步骤和这个品牌型号已确认的资料交给工程助手和所选模型，用来生成仿真环境</label>' +
    '<div class="button-row"><button type="submit" class="button small">让工程助手构建</button>' +
    '<button type="button" class="button small secondary" data-tsim-empty>先用空场地</button>' +
    (skill.environment ? '<button type="button" class="button small secondary" data-tsim-form-close>取消</button>' : '') + '</div></form>';
}

function tsimEnvironment(view, skill) {
  const build = skill.build, saved = skill.environment;
  if (view.problem && !saved) return tsimProblem(view);
  if (saved && saved.staged) {
    return `<section class="tsim-panel tsim-saved"><div class="tsim-saved-head"><h2>仿真环境</h2><span class="tsim-quiet">${tsimEsc(saved.source_name)}，不用构建</span></div>` +
      `<p>${tsimEsc(saved.summary)}</p></section>`;
  }
  let html = '';
  if (build && build.busy) {
    html += `<section class="tsim-panel tsim-building" aria-live="polite"><h2>正在构建这个技能的仿真环境</h2><p class="tsim-busy">${tsimEsc(TSIM_BUILD[build.status] || '')}</p>` +
      `<div class="button-row">${build.session_id ? `<button type="button" class="button small secondary" data-action="engine-session" data-id="${tsimEsc(build.session_id)}">查看过程</button>` : ''}` +
      '<button type="button" class="button small secondary" data-tsim-cancel>取消这次构建</button></div></section>';
  } else if (build && build.status === 'failed') {
    html += `<section class="tsim-panel tsim-failed"><h2>上次构建没有成功</h2><p>${tsimEsc(build.error || '')}</p>` +
      (saved ? '<p class="tsim-quiet">已保存的环境没有变，仍然可以进入。</p>' : '') + '</section>';
  }
  if (!saved) {
    if (!(build && build.busy)) {
      html += '<section class="tsim-panel"><h2>构建这个技能的仿真环境</h2><p>工程助手只为这一个技能布置场地：它要去的地点、要绕开的障碍、要推或要抓的物体；' +
        '任务里别的技能有各自的环境。构建一次后保存，以后直接进入。</p>' + tsimBuildForm(skill) + '</section>';
    }
    return html;
  }
  const lines = (saved.lines || []).length ? `<details class="tsim-lines"><summary>环境里有什么（${saved.lines.length} 项）</summary><ul>${saved.lines.map(l => `<li>${tsimEsc(l)}</li>`).join('')}</ul></details>` : '';
  html += `<section class="tsim-panel tsim-saved"><div class="tsim-saved-head"><h2>仿真环境</h2><span class="tsim-quiet">${tsimEsc(saved.source_name)} · ${date(saved.built_at)} 保存</span></div>` +
    (saved.stale ? `<p class="tsim-problem">这个环境是为「${tsimEsc(saved.product.vendor + ' ' + saved.product.model)}」构建的，任务现在的品牌型号不同；请重新构建。</p>` : '') +
    (saved.carried ? '<p class="tsim-quiet">这是以前整个任务共用的环境，改成每个技能一个环境时沿用了下来；需要只为这个技能布置时，点「重新构建」。</p>' : '') +
    (saved.summary ? `<p>${tsimEsc(saved.summary)}</p>` : '') + (saved.notes || []).map(n => `<p class="tsim-problem">${tsimEsc(n)}</p>`).join('') + lines +
    (tsim.formOpen || saved.stale ? tsimBuildForm(skill)
      : `<div class="button-row"><button type="button" class="button small secondary" data-tsim-form-open ${build && build.busy ? 'disabled' : ''}>重新构建</button>` +
        (tsim.removing ? '<button type="button" class="button small danger" data-tsim-remove-yes>确认删除这个环境</button><button type="button" class="button small secondary" data-tsim-remove-no>不删了</button>'
          : '<button type="button" class="button small secondary" data-tsim-remove>删除环境</button>') + '</div>' +
        (tsim.removing ? '<p class="tsim-quiet">只删除这个技能的环境；任务的仿真机器人、装在它上面的技能和其他技能的环境都不受影响。</p>' : '')) + '</section>';
  return html;
}

function tsimStage(view, skill) {
  const saved = skill.environment, robot = view.robot, facts = tsimFacts(view, skill);
  if (view.problem && !saved) return '';
  if (!saved) {                                       // nothing to enter yet: the stage says so, with the robot it will show
    const busy = skill.build && skill.build.busy;
    return `<div class="tsim-stage" data-running=""><div class="tsim-screen">${tsimPicture({product: view.product}, null)}<div class="tsim-cover"><b>${busy ? '正在构建仿真环境…' : '这个技能还没有仿真环境'}</b>` +
      `<span>${busy ? '构建好就保存在这个技能上。' : '在旁边构建一次，保存后从这里进入。'}</span></div></div></div>`;
  }
  const preview = saved.preview ? `<img class="tsim-preview" alt="环境预览" src="/api/sim/mujoco/preview?id=${encodeURIComponent(saved.scene_id)}">` : '';
  const elsewhere = robot && robot.connected && !facts.here;
  const title = tsim.entering ? '正在启动仿真机器人…' : elsewhere ? `仿真机器人在「${tsimSkillTitle(view, robot.skill)}」的环境里`
    : robot ? '仿真机器人' + (TSIM_ROBOT[robot.state] || '没有在运行') : saved.staged ? '自带场地' : '环境已经保存';
  const note = tsim.entering ? '第一次启动要建立模型，可能要一两分钟。' : elsewhere ? '进入后它会换到这个技能的环境里。' : (robot && robot.error ? robot.error : '');
  const cover = facts.here ? '' : `<div class="tsim-cover"><b>${tsimEsc(title)}</b>${note ? `<span>${tsimEsc(note)}</span>` : ''}` +
    `<button type="button" class="button" data-tsim-enter ${tsim.entering || saved.stale ? 'disabled' : ''}>进入仿真</button></div>`;
  return `<div class="tsim-stage" data-running="${facts.here ? '1' : ''}"><div class="tsim-screen">${preview}<img class="tsim-frame" alt="仿真的实时画面" hidden>` +
    `<div class="tsim-map" hidden></div>${cover}</div><div class="tsim-bar"><p class="tsim-line" role="status"></p><div class="tsim-bar-actions">` +
    (facts.here ? `<button type="button" class="button small danger" data-action="simlive-stop" data-id="${tsimEsc(robot.id)}">停止</button>` +
      `<button type="button" class="button small secondary" data-action="robot-reset" data-id="${tsimEsc(robot.id)}" data-tsim-reset hidden>解除急停</button>` +
      `<button type="button" class="button small secondary" data-action="sim-live" data-id="${tsimEsc(robot.id)}">详细状态</button>` +
      '<button type="button" class="button small secondary" data-tsim-leave>关闭仿真</button>' : '') + '</div></div></div>';
}

// ---------------------------------------------------------------- drawing
function tsimPart(name, html) {
  const el = tsim.root.querySelector(`[data-tsim-part="${name}"]`);
  if (el && tsim.drawn[name] !== html) { el.innerHTML = html; tsim.drawn[name] = html; return true; }
  return false;
}

function tsimMode() {
  return !tsimTaskId() ? 'list' : tsim.skill ? 'skill' : 'task';
}

function tsimDraw() {
  if (!tsim.root) return;
  const mode = tsimMode(), view = tsim.view, skill = tsimSkillOf(view);
  tsim.root.dataset.mode = mode;
  tsim.root.dataset.built = tsimBuilt(view, mode === 'skill' ? skill : null) ? '1' : '';
  if (mode === 'list') {
    for (const part of ['head', 'skills', 'stage', 'env', 'skill', 'run']) tsimPart(part, '');
    return void tsimPart('list', tsimListMarkup());
  }
  tsimPart('list', '');
  if (!view) {
    tsimPart('head', '<div class="tw-back"><a href="#simulation" data-tsim-back>← 全部任务</a></div>' +
      (tsim.viewError ? `<div class="notice bad">${tsimEsc(tsim.viewError)}</div>` : '<p class="tsim-quiet">正在读取…</p>'));
    return;
  }
  if (mode === 'task') {
    for (const part of ['stage', 'env', 'skill', 'run']) tsimPart(part, '');
    tsimPart('head', tsimTaskHead(view));
    return void tsimPart('skills', tsimSkillList(view));
  }
  if (!skill) {                                       // the skill is gone (an application was unbound): back to the task
    tsimChooseSkill('');
    return void render();
  }
  tsimPart('skills', '');
  tsimPart('head', tsimSkillHead(view, skill));
  if (tsimPart('stage', tsimStage(view, skill))) tsim.frame = 0;
  const note = tsim.root.querySelector('[data-tsim-form] textarea')?.value, agreed = tsim.root.querySelector('[data-tsim-form] [name=data_authorized]')?.checked;
  if (tsimPart('env', tsimEnvironment(view, skill)) && note !== undefined) {   // a form drawn again keeps what was typed
    const area = tsim.root.querySelector('[data-tsim-form] textarea'), box = tsim.root.querySelector('[data-tsim-form] [name=data_authorized]');
    if (area) area.value = note;
    if (box && agreed) box.checked = true;
  }
  const facts = tsimFacts(view, skill);
  tsimPart('skill', skill.environment ? tsimSkillPanel(skill, facts) : '');
  tsimPart('run', facts.here && tsim.live?.reachable && typeof simRunSection === 'function'
    ? `<section class="tsim-panel tsim-run">${simRunSection(tsim.live, view.robot.id)}</section>` : '');
  tsimLive();
}

// What does not redraw a part: the status line, the picture, the map.
function tsimLive() {
  const root = tsim.root, view = tsim.view, skill = tsimSkillOf(view);
  const line = root.querySelector('.tsim-line');
  if (!line || !view || !skill) return;
  const robot = view.robot, live = tsim.live, facts = tsimFacts(view, skill);
  let text = '', tone = '';
  if (!robot || !robot.connected) text = robot ? '仿真机器人' + (TSIM_ROBOT[robot.state] || '没有在运行') : '还没有进入仿真';
  else if (!facts.here) text = `仿真机器人在「${tsimSkillTitle(view, robot.skill)}」的环境里`;
  else if (facts.staging) { text = `正在换到「${skill.title}」的场地…`; tone = 'busy'; }
  else if (!live || !live.reachable) text = live?.error || '正在读取机器人状态…';
  else if (live.estop?.latched) { text = '急停已锁存：' + (live.estop.reason || '') + '。解除后才能再运行技能'; tone = 'bad'; }
  else if (live.run && TSIM_ACTIVE.includes(live.run.status)) { text = '正在运行 ' + (live.run.target || '技能') + (typeof live.run.progress === 'number' ? ` · ${Math.round(live.run.progress * 100)}%` : ''); tone = 'busy'; }
  else text = '待命 · ' + (typeof simModeText === 'function' ? simModeText(live.state?.mode || live.mode) : '');
  if (line.textContent !== text) line.textContent = text;
  line.dataset.tone = tone;
  const reset = root.querySelector('[data-tsim-reset]');
  if (reset) reset.hidden = !(live?.estop?.latched);
  const frame = root.querySelector('.tsim-frame'), map = root.querySelector('.tsim-map');
  const picture = Boolean(facts.here && (robot.picture || robot.duck));
  if (frame) frame.hidden = !picture || !frame.getAttribute('src');
  if (map) {
    const drawn = facts.here && !picture && live?.reachable && typeof simMap === 'function'
      ? (live.state?.pose ? simMap(live) : live.state?.joints ? simJoints(live) : '') : '';
    map.hidden = !drawn;
    if (drawn && tsim.drawn.map !== drawn) { map.innerHTML = drawn; tsim.drawn.map = drawn; }
  }
}

function tsimFrameTick() {
  const view = tsim.view, skill = tsimSkillOf(view), robot = view?.robot, frame = tsim.root?.isConnected ? tsim.root.querySelector('.tsim-frame') : null;
  if (!frame || !skill || document.hidden || !tsimFacts(view, skill).here || !(robot.picture || robot.duck) || tsim.frameBusy) return;
  tsim.frameBusy = true;
  const next = new Image(), done = () => { tsim.frameBusy = false; };
  next.onload = () => { if (frame.isConnected) { frame.src = next.src; frame.hidden = false; } done(); };
  next.onerror = done;
  next.src = `/api/sims/frame?id=${encodeURIComponent(robot.id)}&t=${++tsim.frame}`;
}

// ---------------------------------------------------------------- asking
const tsimShown = () => Boolean(tsim.root && tsim.root.isConnected && !document.hidden);

async function tsimOnce(name, work) {
  if (tsim.busy[name]) return;
  tsim.busy[name] = true;
  try { await work(); } finally { tsim.busy[name] = false; }
}

async function tsimAskList() {
  await tsimOnce('list', async () => {
    try { tsim.list = await api('task-sim/list', {}); tsim.listError = ''; }
    catch (error) { tsim.listError = error.message || '读取任务失败'; }
  });
  tsimDraw();
}

async function tsimAskView() {
  const id = tsim.task;
  if (!id) return;
  await tsimOnce('view:' + id, async () => {
    try {
      const view = await api('task-sim/view', {project_id: id});
      if (id === tsim.task) { tsim.view = view; tsim.viewError = ''; }
    } catch (error) {
      if (id === tsim.task) tsim.viewError = error.message || '读取失败';
    }
  });
  tsimDraw();
}

// Only on a skill's page and only while the task's robot is in that skill's environment.
async function tsimAskLive() {
  const view = tsim.view, skill = tsimSkillOf(view), robot = view?.robot;
  if (!skill || !tsimFacts(view, skill).here) { tsim.live = null; tsim.duck = null; return; }
  const still = () => tsim.view?.robot?.id === robot.id && tsim.skill === skill.id;    // not after another task or skill was opened meanwhile
  await tsimOnce('live:' + robot.id, async () => {
    try {
      const [live, duck] = await Promise.all([api('robots/live', {robot_id: robot.id, project_id: tsim.task}),
        robot.duck ? api('sims/duck/status', {robot_id: robot.id}) : Promise.resolve(null)]);
      if (!still()) return;
      tsim.live = live;
      tsim.duck = duck;
    } catch { /* the line keeps what it said; the next tick asks again */ }
  });
  tsimDraw();
}

// One chain per loop: a chain that was waiting for an answer when the loop was started again ends there.
function tsimLoop(name, every, work) {
  clearTimeout(tsim.timers[name]);
  const mine = tsim.chains[name] = (tsim.chains[name] || 0) + 1;
  const tick = async () => {
    if (tsimShown()) await work();
    if (tsim.chains[name] !== mine) return;
    tsim.timers[name] = setTimeout(tick, typeof every === 'function' ? every() : every);
  };
  tick();
}
function tsimStopLoops() {
  for (const name of ['list', 'view', 'live']) { clearTimeout(tsim.timers[name]); tsim.chains[name] = (tsim.chains[name] || 0) + 1; }
}

const tsimBusyNow = () => Boolean(tsim.entering || (tsim.view?.counts?.building));

function tsimStart() {
  tsimLoop('list', TSIM_LIST_MS, () => (tsim.task ? undefined : tsimAskList()));
  tsimLoop('view', () => (tsimBusyNow() ? TSIM_BUSY_MS : TSIM_VIEW_MS), () => (tsim.task ? tsimAskView() : undefined));
  tsimLoop('live', TSIM_LIVE_MS, () => (tsim.task && tsim.skill ? tsimAskLive() : undefined));
  clearInterval(tsim.timers.frame);
  tsim.timers.frame = setInterval(tsimFrameTick, TSIM_FRAME_MS);
}

// ---------------------------------------------------------------- doing
// An answer is shown only while its task is still the one that is open.
function tsimTake(id, view) {
  if (id !== tsim.task) return false;
  tsim.view = view;
  return true;
}

async function tsimBuild(mode, form) {
  const data = form ? new FormData(form) : null, id = tsim.task;
  const view = await api('task-sim/build', {project_id: id, skill_id: tsim.skill, mode, note: data ? String(data.get('note') || '').trim() : '',
    data_authorized: data ? data.get('data_authorized') === 'on' : false});
  if (!tsimTake(id, view)) return;
  tsim.formOpen = false;
  tsimDraw();
}

async function tsimEnter() {
  const id = tsim.task, skill = tsim.skill;
  tsim.entering = id;
  tsimDraw();
  try {
    const view = await api('task-sim/enter', {project_id: id, skill_id: skill});
    tsimTake(id, view);
    const failed = (view.installed_now || []).filter(x => !x.installed);
    await refresh(true);                               // the new robot is among the robots of the state: its dialogs can open
    toast(failed.length ? `已进入仿真；这个技能没有装上：${failed[0].error || ''}` : '已进入仿真');
  } finally {
    if (tsim.entering === id) tsim.entering = false;
    tsimDraw();
  }
  tsimAskLive();
}

function tsimRunForm(skillId, useIndex) {
  const view = tsim.view, skill = (view.skills || []).find(s => s.id === skillId), robot = find('robots', view.robot.id);
  if (!skill || !robot) throw Error('找不到这个技能或仿真机器人，请刷新页面');
  const use = useIndex === '' ? null : skill.uses[Number(useIndex)];
  return form('运行技能', h(robot.name) + ' · ' + h(skill.title),
    (use ? `<p class="subtitle">${h(tsimUseLine(use))}。运行前可以改。</p>` : skill.summary ? `<p class="subtitle">${h(skill.summary)}</p>` : '') +
    skillParamsForm(skill.parameters || [], use ? use.parameters : null) + skillRunConfirmHtml(robot),
    'sim-run', '授权并运行',
    `data-robot="${h(robot.id)}" data-skill="${h(skill.id)}" data-request="${h(newRequestId())}" data-project="${h(tsim.task)}" data-tsim="1"`);
}

// The form of web/sim.js sends the run; from this page the person stays on the page and watches it there.
async function tsimRunSubmit(f) {
  const result = await api('robots/run-skill', {robot_id: f.dataset.robot, skill_id: f.dataset.skill, parameters: skillParamsCollect(f),
    ...skillRunConfirmations(f), request_id: f.dataset.request, project_id: f.dataset.project})
    .catch(err => { f.dataset.request = newRequestId(); throw err; });
  if (result.status !== 'accepted') { f.dataset.request = newRequestId(); throw uxOpError(result, '设备没有接受运行请求'); }
  $('#dialog').close();
  toast('技能开始运行');
  return tsimAskLive();
}

// Moving between the list, a task and a skill draws the page again: its head (and the run list at its foot) change.
function tsimGo(task, skill) {
  if (task !== tsim.task) tsimChoose(task);
  if (skill !== tsim.skill) tsimChooseSkill(skill);
  render();
  $('#content-scroll')?.scrollTo(0, 0);
  if (!task) return void tsimAskList();
  tsimAskView();
}

async function tsimClick(event) {
  const el = event.target.closest('[data-tsim-open],[data-tsim-back],[data-tsim-skill-open],[data-tsim-up],[data-tsim-enter],[data-tsim-empty],[data-tsim-cancel],' +
    '[data-tsim-remove],[data-tsim-remove-yes],[data-tsim-remove-no],[data-tsim-form-open],[data-tsim-form-close],[data-tsim-run],[data-tsim-install],[data-tsim-leave],[data-tsim-task-page]');
  if (!el || el.disabled || !tsim.root.contains(el)) return;
  event.preventDefault();
  const d = el.dataset;
  try {
    if ('tsimOpen' in d) return void tsimGo(d.tsimOpen, '');
    if ('tsimBack' in d) return void tsimGo('', '');
    if ('tsimSkillOpen' in d) return void tsimGo(tsim.task, d.tsimSkillOpen);
    if ('tsimUp' in d) return void tsimGo(tsim.task, '');
    if ('tsimTaskPage' in d) { if (twSelect(tsim.task)) twGo('task'); return; }
    if ('tsimFormOpen' in d || 'tsimFormClose' in d) { tsim.formOpen = 'tsimFormOpen' in d; return void tsimDraw(); }
    if ('tsimRemove' in d || 'tsimRemoveNo' in d) { tsim.removing = 'tsimRemove' in d; return void tsimDraw(); }
    el.disabled = true;
    if ('tsimEnter' in d) return void await tsimEnter();
    if ('tsimEmpty' in d) return void await tsimBuild('empty');
    const id = tsim.task;
    if ('tsimCancel' in d) { tsimTake(id, await api('task-sim/cancel', {project_id: id, skill_id: tsim.skill})); return void tsimDraw(); }
    if ('tsimRemoveYes' in d) {
      tsim.removing = false;
      await api('task-sim/remove', {project_id: id, skill_id: tsim.skill});
      await refresh(true);
      return void tsimAskView();
    }
    if ('tsimInstall' in d) { tsimTake(id, await api('task-sim/install', {project_id: id, skill_id: d.tsimInstall})); toast('技能已安装'); tsim.drawn.skill = null; return void tsimDraw(); }
    if ('tsimRun' in d) { el.disabled = false; return void tsimRunForm(d.tsimRun, d.use); }
    if ('tsimLeave' in d) { await api('sims/stop', {robot_id: tsim.view.robot.id}); await refresh(true); toast('仿真已关闭；环境和技能都留着，下次直接进入'); return void tsimAskView(); }
  } catch (error) {
    el.disabled = false;
    const slot = el.closest('form')?.querySelector('.form-error');
    if (slot) slot.textContent = error.message || '没有完成'; else toast(error.message || '没有完成');
    tsimDraw();
  }
}

function tsimRoot() {
  if (tsim.root) return tsim.root;
  const root = document.createElement('section');
  root.className = 'tsim';
  root.innerHTML = '<div data-tsim-part="list"></div><div data-tsim-part="head"></div><div data-tsim-part="skills"></div>' +
    '<div class="tsim-body"><div class="tsim-left"><div data-tsim-part="stage"></div><div data-tsim-part="run"></div></div>' +
    '<div class="tsim-right"><div data-tsim-part="env"></div><div data-tsim-part="skill"></div></div></div>';
  root.addEventListener('click', tsimClick);
  root.addEventListener('submit', event => {
    const f = event.target.closest('[data-tsim-form]');
    if (!f) return;
    event.preventDefault();
    const slot = f.querySelector('.form-error'), submit = f.querySelector('[type=submit]');
    slot.textContent = '';
    submit.disabled = true;
    tsimBuild('agent', f).catch(error => { slot.textContent = error.message || '没有完成'; submit.disabled = false; });
  });
  tsim.root = root;
  return root;
}

function tsimMount() {
  const slot = document.querySelector('[data-tsim-slot]');
  if (!slot) return;
  const root = tsimRoot();
  if (root.parentNode !== slot) {
    const fresh = !root.isConnected;
    slot.replaceChildren(root);
    tsim.drawn.map = '';
    tsimDraw();
    if (fresh || !tsim.chains.view) tsimStart();
  } else {
    tsimDraw();
  }
  const crumb = document.getElementById('breadcrumb'), task = tsim.task && state.projects.find(p => p.id === tsim.task);
  const skill = tsimSkillOf(tsim.view);
  if (crumb && task) crumb.textContent = '仿真试运行 / ' + task.name + (skill ? ' / ' + skill.title : '');
}

function tsimPage() {
  const id = state.user ? tsimTaskId() : '';
  const head = id ? '' : `<div class="page-head"><div>${pageTitleMarkup(TSIM_ROUTE)}</div></div>`;
  return head + (state.user ? '<div data-tsim-slot></div>' : (typeof noProject === 'function' ? noProject() : ''));
}

function tsimInstall() {
  views[TSIM_ROUTE] = tsimPage;
  const baseSubmit = simSubmit;
  simSubmit = async (kind, p, f) => (kind === 'sim-run' && f.dataset.tsim ? tsimRunSubmit(f) : baseSubmit(kind, p, f));
  new MutationObserver(tsimMount).observe(document.getElementById('main'), {childList: true});
  document.addEventListener('visibilitychange', () => { if (tsimShown()) tsimStart(); });
  window.addEventListener('hashchange', () => { if (location.hash.slice(1) !== TSIM_ROUTE) tsimStopLoops(); });
  if (location.hash.slice(1) === TSIM_ROUTE && typeof route === 'function') route();
}

if (typeof document !== 'undefined' && typeof views === 'object') document.addEventListener('DOMContentLoaded', tsimInstall);
