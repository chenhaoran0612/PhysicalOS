'use strict';
// Issue #46 — the page of one task shows the skills it works with and how they are configured.
//
// For every robot of the task:
//   * the places the robot says it knows (its capability base.navigate_to_place);
//   * the skills installed on it, read from the device when it is connected (the same read as the
//     robot's 「能力与技能」), otherwise what the console recorded when it installed them;
//   * where the task uses each skill and with which values: the steps of its applications (their order,
//     parameters and time limit) and the latest direct run. A skill the task does not use yet shows the
//     values it would run with.
// Values that are an order are shown as one: places the robot knows by name, and lists of points with
// their coordinates.
//
// Nothing here is new data and nothing reaches a robot except that one read-only request. What a robot
// does not report (the coordinates of its places, its map) is said to be missing, not made up.
//
// web/task_workbench.js puts the section on the page (twTaskView) and routes its one action.
// The functions down to tsSectionMarkup are pure (state in, facts or markup out): tests/task_skills_logic.cjs.

const TS_PLACE_CAPABILITY = 'base.navigate_to_place';
const TS_RETRY_MS = 30000;          // a read that failed is tried again when the page is drawn after this long
const TS_TEXT_MAX = 300;             // a value of another shape is shown as it was saved, up to this length
const TS_RUN_STATUS = {dispatching: '正在发给机器人', accepted: '机器人已开始运行', rejected: '机器人拒绝了这次运行',
                       failed: '请求没有发出', unknown: '结果不明'};
const TS_INSTALLED = {device: ['已安装', 'good'], record: ['按记录已安装', ''], missing: ['设备上没有这个技能', 'bad'], unknown: ['不知道是否已安装', 'warn']};
const TS_ORIGIN = {default: '默认值', unset: '未填写'};

// A device gives the time of an install in seconds since 1970, the console's own records as text.
const tsWhen = value => {
  const seconds = typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value ?? '')) ? Number(value) : NaN;
  return Number.isFinite(seconds) && seconds < 1e11 ? seconds * 1000 : value;
};

// Issue #42: robots belong to the account (st.robot_home), and so does what was installed or run on them directly.
// The robot a task was created with (task.robot_id) is its robot; without a choice, all of them are.
const tsOf = (st, kind, task) => (st[kind] || []).filter(x => (x.project_id === task.id ||
  (['robots', 'operations'].includes(kind) && Boolean(st.robot_home) && x.project_id === st.robot_home)) &&
  (kind !== 'robots' || !task.robot_id || x.id === task.robot_id));

// ---------------------------------------------------------------- what a robot knows

// The names of the places the robot can be sent to, as its manifest declares them.
function tsPlaces(robot) {
  const capability = (robot?.manifest_v2?.capabilities || []).find(c => c?.id === TS_PLACE_CAPABILITY);
  const names = capability?.params?.places;
  return Array.isArray(names) ? names.filter(name => typeof name === 'string' && name.trim()).map(name => name.trim()) : [];
}

// ---------------------------------------------------------------- one value, as a person reads it

const tsNames = text => String(text).split(/[、,，]/).map(part => part.trim()).filter(Boolean);

const tsIsPoints = value => Array.isArray(value) && value.length > 0 &&
  value.every(p => Array.isArray(p) && p.length >= 2 && p.length <= 7 && p.length === value[0].length && p.every(x => typeof x === 'number'));

function tsOptionLabel(decl, value) {
  const options = Array.isArray(decl?.options) ? decl.options : [];
  const found = options.find(o => o && typeof o === 'object' && JSON.stringify(o.value) === JSON.stringify(value));
  return found ? String(found.label ?? found.value) : null;
}

// {kind: 'places', items} | {kind: 'points', items, unit} | {kind: 'text', text}
function tsValue(decl, value, places) {
  const unit = typeof decl?.unit === 'string' ? decl.unit : '';
  if (tsIsPoints(value)) return {kind: 'points', items: value, unit};
  const label = tsOptionLabel(decl, value);
  if (label !== null) return {kind: 'text', text: label};
  if (typeof value === 'string') {
    if (!value.trim()) return {kind: 'text', text: '（留空）'};
    const names = tsNames(value);
    if (places.length && names.length && names.every(name => places.includes(name))) return {kind: 'places', items: names};
    return {kind: 'text', text: value};
  }
  if (typeof value === 'boolean') return {kind: 'text', text: value ? '是' : '否'};
  if (typeof value === 'number') return {kind: 'text', text: unit ? `${value} ${unit}` : String(value)};
  const text = JSON.stringify(value) ?? '';
  return {kind: 'text', text: text.length > TS_TEXT_MAX ? text.slice(0, TS_TEXT_MAX - 1) + '…' : text};
}

// One configuration: every declared parameter with its saved value (or its default, or nothing), then
// whatever else was saved. `values` null means "not used yet": the defaults.
function tsRows(decls, values, places) {
  const declared = Array.isArray(decls) ? decls.filter(d => d && typeof d.name === 'string') : [];
  const saved = values && typeof values === 'object' && !Array.isArray(values) ? values : {};
  const rows = declared.map(decl => {
    const has = Object.hasOwn(saved, decl.name), origin = has ? 'saved' : Object.hasOwn(decl, 'default') ? 'default' : 'unset';
    const value = origin === 'unset' ? {kind: 'text', text: ''} : tsValue(decl, has ? saved[decl.name] : decl.default, places);
    return {name: decl.name, title: decl.title || decl.name, description: decl.description || '', origin, value};
  });
  const known = new Set(declared.map(d => d.name));
  const extra = Object.keys(saved).filter(name => !known.has(name)).map(name =>
    ({name, title: name, description: '', origin: 'saved', value: tsValue({name}, saved[name], places)}));
  return [...rows, ...extra];
}

// ---------------------------------------------------------------- the skills of one robot

// `live` is what the device answered ({available, items, error}), {loading: true} while it is asked,
// or nothing when it was not asked (not connected).
function tsRobotView(st, task, robot, live) {
  const places = tsPlaces(robot), fromDevice = Boolean(live?.available);
  const operations = tsOf(st, 'operations', task).filter(o => o.robot_id === robot.id);
  const registered = tsOf(st, 'skills', task);
  const entries = new Map();
  const entry = (id, more = {}) => {
    if (!entries.has(id)) {
      entries.set(id, {id, title: id, version: '', summary: '', installed: fromDevice ? 'missing' : 'unknown', installedAt: null,
                       decls: null, compatible: true, uses: [], defaults: []});
    }
    return Object.assign(entries.get(id), more);
  };
  if (fromDevice) {
    for (const s of live.items || []) {
      entry(String(s.id), {title: s.title || s.id, version: s.version || '', summary: s.summary || '', installed: 'device',
                           installedAt: tsWhen(s.installed_at) || null, decls: Array.isArray(s.parameters) ? s.parameters : null, compatible: s.compatible !== false});
    }
  } else {
    for (const o of operations.filter(o => o.kind === 'skill_install' && o.status === 'succeeded')) {
      entry(String(o.skill_id), {version: o.version || '', installed: 'record', installedAt: o.created_at || null});
    }
  }
  for (const app of tsOf(st, 'applications', task).filter(a => a.robot_id === robot.id)) {
    const steps = Array.isArray(app.steps) ? app.steps : [];
    steps.forEach((step, index) => {
      const skill = registered.find(s => s.id === step.skill_id);
      const e = entry(String(step.entrypoint || skill?.entrypoint || step.skill_id));
      if (e.title === e.id && skill?.name) e.title = skill.name;
      if (!e.version && skill?.version) e.version = skill.version;
      if (!e.decls && Array.isArray(skill?.parameters)) e.decls = skill.parameters;
      e.uses.push({from: 'app', label: `应用「${app.name}」第 ${index + 1} 步（共 ${steps.length} 步）`, timeout: step.timeout_s,
                   rows: tsRows(e.decls, step.parameters || {}, places)});
    });
  }
  const runs = operations.filter(o => o.kind === 'skill_run');
  for (const id of [...new Set(runs.map(o => String(o.skill_id)))]) {
    const own = runs.filter(o => String(o.skill_id) === id);
    const latest = own.reduce((a, b) => (String(b.created_at || '') >= String(a.created_at || '') ? b : a));
    const e = entry(id);
    e.uses.push({from: 'run', label: `最近一次直接运行（共 ${own.length} 次）`, when: latest.created_at || null, status: latest.status,
                 rows: tsRows(e.decls, latest.parameters || {}, places)});
  }
  const all = [...entries.values()];
  for (const e of all) if (!e.uses.length) e.defaults = tsRows(e.decls, null, places);
  const skills = [...all.filter(e => e.uses.length), ...all.filter(e => !e.uses.length)];
  return {robot, places, skills, source: fromDevice ? 'device' : 'record', loading: live?.loading === true,
          error: !fromDevice && live && !live.loading ? live.error || '暂不能读取已安装技能' : null,
          used: skills.filter(e => e.uses.length).length, installed: skills.filter(e => e.installed === 'device' || e.installed === 'record').length};
}

// The robots whose skills are read from the device, and the key under which an answer is kept: another
// successful install on the robot (or a change of its connection) is a reason to ask again.
const tsWanted = (st, task) => tsOf(st, 'robots', task).filter(r => r.status === 'connected' && r.endpoint);
// Whether the device is asked (again): never while an answer is kept or awaited, except a failure that is old enough.
const tsShouldRead = (kept, now) => !kept || (kept.failed === true && now - kept.at >= TS_RETRY_MS);
function tsLiveKey(st, robot) {
  const installs = (st.operations || []).filter(o => o.robot_id === robot.id && o.kind === 'skill_install' && o.status === 'succeeded').length;
  return `${robot.id}|${robot.status}|${installs}`;
}

// ---------------------------------------------------------------- the markup

function tsValueMarkup(value) {
  if (value.kind === 'places') return `<ol class="ts-route" aria-label="顺序">${value.items.map(name => `<li>${h(name)}</li>`).join('')}</ol>`;
  if (value.kind === 'points') {
    const axes = value.items[0].length <= 3 ? ['x', 'y', 'z'] : null;
    return `<ol class="ts-points" aria-label="顺序和坐标">${value.items.map(point =>
      `<li>${point.map((n, i) => `<span>${axes ? axes[i] : i + 1} ${h(n)}</span>`).join('')}${value.unit ? `<em>${h(value.unit)}</em>` : ''}</li>`).join('')}</ol>`;
  }
  return `<span class="ts-text">${h(value.text)}</span>`;
}

function tsRowsMarkup(rows) {
  if (!rows.length) return '<p class="ts-none">这个技能没有需要配置的参数。</p>';
  return `<dl class="ts-rows">${rows.map(row =>
    `<div class="ts-row" data-origin="${row.origin}"><dt title="${h(row.description)}">${h(row.title)}` +
    `${row.title === row.name ? '' : `<small>${h(row.name)}</small>`}</dt>` +
    `<dd>${row.origin === 'unset' ? '' : tsValueMarkup(row.value)}${TS_ORIGIN[row.origin] ? `<em class="ts-origin">${TS_ORIGIN[row.origin]}</em>` : ''}</dd></div>`).join('')}</dl>`;
}

function tsUseMarkup(use) {
  const facts = use.from === 'app'
    ? (typeof use.timeout === 'number' ? `最多 ${use.timeout} 秒` : '')
    : [use.when ? date(use.when) : '', TS_RUN_STATUS[use.status] || ''].filter(Boolean).join(' · ');
  return `<div class="ts-use" data-from="${use.from}"><p class="ts-from"><strong>${h(use.label)}</strong>${facts ? `<span>${h(facts)}</span>` : ''}</p>` +
    `${tsRowsMarkup(use.rows)}</div>`;
}

function tsSkillMarkup(skill) {
  const [word, tone] = TS_INSTALLED[skill.installed];
  const installed = skill.installedAt && (skill.installed === 'device' || skill.installed === 'record') ? `${word} · ${date(skill.installedAt)}` : word;
  const body = skill.uses.length
    ? skill.uses.map(tsUseMarkup).join('')
    : `<div class="ts-use" data-from="default"><p class="ts-from"><strong>这个任务还没有用到</strong>` +
      `${skill.defaults.length ? '<span>运行时默认使用下面的值</span>' : ''}</p>${tsRowsMarkup(skill.defaults)}</div>`;
  return `<article class="ts-skill${skill.uses.length ? '' : ' unused'}" data-ts-skill="${h(skill.id)}">` +
    `<header class="ts-skill-head"><h4>${h(skill.title)}</h4><span class="mono ts-key">${h(skill.id)}${skill.version ? '@' + h(skill.version) : ''}</span>` +
    `<span class="ts-marks">${badge(installed, tone)}${skill.compatible ? '' : badge('与这台机器人不兼容', 'bad')}</span></header>` +
    `${skill.summary ? `<p class="ts-summary">${h(skill.summary)}</p>` : ''}${body}</article>`;
}

function tsRobotMarkup(view) {
  const robot = view.robot, connected = robot.status === 'connected';
  const source = view.loading ? '正在读取这台机器人上的技能…'
    : view.source === 'device' ? '技能列表是刚从这台机器人上读到的。'
    : `按控制台的安装记录显示：${view.error || (connected ? '这台机器人没有设备地址，读不到它上面的技能。' : '机器人现在没有连接，读不到它上面的技能。')}`;
  const places = view.places.length
    ? `<div class="ts-places"><h4>这台机器人认识的地点</h4><p>${view.places.map(name => `<span class="ts-chip">${h(name)}</span>`).join('')}</p>` +
      '<small>机器人只报告了地点的名称，没有报告它们的坐标和所用的地图：这些保存在机器人自己的导航程序里。</small></div>'
    : '';
  const skills = view.skills.length
    ? view.skills.map(tsSkillMarkup).join('')
    : `<p class="ts-none">${view.loading ? '' : '这台机器人上还没有技能。在“我的机器人”里打开它的“能力与技能”，可以安装技能包。'}</p>`;
  return `<article class="ts-robot" data-ts-robot="${h(robot.id)}"${view.loading ? ' aria-busy="true"' : ''}>` +
    `<header class="ts-robot-head"><div><h3>${h(robot.name)}</h3>` +
    `<p>${badge(connected ? '已连接' : '未连接', connected ? 'good' : '')}<span>已安装 ${view.installed} 个技能 · 这个任务用到 ${view.used} 个</span></p></div>` +
    `${connected && robot.endpoint ? button('重新读取', 'task-skills-reload', 'secondary small', robot.id, view.loading) : ''}</header>` +
    `<p class="ts-source">${h(source)}</p>${places}${skills}</article>`;
}

// The whole section. `lives` holds what the devices answered, by tsLiveKey.
function tsSectionMarkup(st, task, lives) {
  const robots = tsOf(st, 'robots', task);
  const head = '<div class="section-head"><h2>技能与配置</h2><span>这个任务的机器人装了哪些技能、用到哪些、各自怎样配置</span></div>';
  const body = robots.length
    ? robots.map(robot => tsRobotMarkup(tsRobotView(st, task, robot, lives[tsLiveKey(st, robot)]))).join('')
    : `<div class="ts-empty"><p>这个任务还没有机器人。连接机器人并安装技能后，这里会列出每个技能和它的配置。</p>${button('去连接机器人', 'go-robots', 'small')}</div>`;
  return `<section class="ts" id="task-skills" aria-label="技能与配置">${head}${body}</section>`;
}

// ---------------------------------------------------------------- on the page

const tsLives = {};                  // tsLiveKey -> what the device answered

// Draws the section again where it stands. The rest of the page is left alone: drawing all of it would
// close what a person has open.
function tsPaint() {
  const slot = document.getElementById('task-skills'), task = project();
  if (slot && task && current === 'task') slot.outerHTML = tsSectionMarkup(state, task, tsLives);
}

// One read-only request per robot and state of its installs; the answer is kept, since the page is drawn
// again whenever its data changes. A failure is kept too (it is shown), and tried again later.
function tsRead(robot, again = false) {
  const key = tsLiveKey(state, robot);
  if (!again && !tsShouldRead(tsLives[key], Date.now())) return;
  tsForget(robot.id, key);
  tsLives[key] = {loading: true};
  const failed = error => ({available: false, items: [], error: error?.message || '读取已安装技能失败', failed: true, at: Date.now()});
  api('robots/detail', {robot_id: robot.id, project_id: robot.project_id})
    .then(detail => (detail?.skills && typeof detail.skills === 'object' ? detail.skills : failed(Error('设备没有返回技能列表'))), failed)
    .then(answer => { tsLives[key] = answer.available || answer.failed ? answer : {...answer, failed: true, at: Date.now()}; tsPaint(); })
    .catch(error => { tsLives[key] = failed(error); });
}

// Drops what is kept about a robot, except under `keep`.
function tsForget(robotId, keep = '') {
  for (const key of Object.keys(tsLives)) if (key !== keep && key.startsWith(robotId + '|')) delete tsLives[key];
}

// Called by web/task_workbench.js while it draws the page of a task.
function taskSkillsSection() {
  const task = project();
  if (!task) return '';
  const wanted = tsWanted(state, task);
  // A robot that is not connected now is asked afresh once it is back: its skills may have changed meanwhile.
  for (const robot of tsOf(state, 'robots', task)) if (!wanted.includes(robot)) tsForget(robot.id);
  for (const robot of wanted) tsRead(robot);
  return tsSectionMarkup(state, task, tsLives);
}

function taskSkillsReload(id) {
  const robot = (state.robots || []).find(r => r.id === id);
  if (!robot) return;
  tsRead(robot, true);
  tsPaint();
}
