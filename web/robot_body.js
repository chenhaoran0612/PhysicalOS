'use strict';
// Issue #38 — a robot's body for the management layer (brainos/robot_body.py).
// "安装管理层" used to install a simulated body only: nothing moved. A robot reached over SSH now has
// "本体设置" on its card: simulated, or 真实本体 — the robot's own ROS 2, with the ROS settings the install
// is made with and the device configuration (topic names, and for a robot that brings its own
// navigation programs the places it knows). "从机器人读取" fills the form from the robot (read only).
// Saving reaches no robot. A real body gets there through "安装管理层"; a changed configuration through
// "应用到机器人" (the management layer is started again). The kind of body and the ROS settings of an
// install only change with a new install, and the page says so.
// Loaded after runtime_autostart.js. It wraps engineRobotCard, engineAction, engineSubmit, riForm,
// riFound, riSummary, riResult, riActions and riShow.

const RB_DEFAULT_SETUP = '/opt/ros/humble/setup.bash';
const RB_SETTINGS = ['ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION', 'FASTRTPS_DEFAULT_PROFILES_FILE',
  'CYCLONEDDS_URI', 'ZENOH_SESSION_CONFIG_URI'];
// The configuration file each RMW reads, by the setting that names it.
const RB_RMW_FILE = {rmw_fastrtps_cpp: 'FASTRTPS_DEFAULT_PROFILES_FILE', rmw_cyclonedds_cpp: 'CYCLONEDDS_URI',
  rmw_zenoh_cpp: 'ZENOH_SESSION_CONFIG_URI'};
const RB_FILE_KEYS = Object.values(RB_RMW_FILE);
const RB_CONFIG_MAX = 256 * 1024;
// [what the card says, tone, what to do next]
const RB_STATE = {
  sim: ['仿真本体', '', ''],
  unset: ['还没有装到机器人上', 'warn', 'install'],
  unknown: ['机器人上已有真实本体，它用的配置不是从这里放上去的', 'warn', 'apply'],
  current: ['机器人上用的就是这份', 'good', ''],
  config_changed: ['配置改过了，还没有放到机器人上', 'warn', 'apply'],
  reinstall: ['改动要重新安装管理层才生效', 'warn', 'install'],
};
const RB_EXAMPLE = `{
  "name": "我的机器人",
  "family": "quadruped",
  "topics": {"cmd_vel": "/cmd_vel", "odom": "/odom", "battery": "/battery_state"},
  "nav2": false,
  "programs": {
    "workdir": "/home/robot/nav",
    "places": {
      "会议室": {"steps": [
        {"title": "走到会议室", "argv": ["python3", "goto.py", "--mark", "会议室", "--speed", "{max_speed}"],
         "speed_range": [0.2, 0.6], "ok": "arrived", "timeout_s": 300}
      ]}
    }
  }
}
`;

const rbOwn = (table, key, otherwise) => (Object.hasOwn(table, key) ? table[key] : otherwise);
const rbCan = r => typeof riCanInstall === 'function' && riCanInstall(r);

// The body of a robot as the page shows it: what is saved, with the defaults for what never was.
function rbBody(r) {
  const b = r && typeof r.body === 'object' && r.body ? r.body : {};
  const settings = b.settings && typeof b.settings === 'object' ? b.settings : {};
  return {kind: b.kind === 'ros2' ? 'ros2' : 'sim', setup: b.setup || RB_DEFAULT_SETUP,
    settings: Object.fromEntries(RB_SETTINGS.filter(k => settings[k] !== undefined && settings[k] !== '').map(k => [k, String(settings[k])])),
    config_text: typeof b.config_text === 'string' ? b.config_text : '',
    state: Object.hasOwn(RB_STATE, b.state) ? b.state : (b.kind === 'ros2' ? 'unset' : 'sim')};
}

// The fields of the form as the settings a save sends. Throws what a person can fix.
function rbRequest(p) {
  const kind = p.kind === 'ros2' ? 'ros2' : 'sim';
  const settings = {}, rmw = String(p.RMW_IMPLEMENTATION ?? '').trim(), file = String(p.rmw_file ?? '').trim();
  const domain = String(p.ROS_DOMAIN_ID ?? '').trim();
  if (domain !== '') {
    if (!/^[0-9]{1,3}$/.test(domain) || Number(domain) > 232) throw Error('ROS_DOMAIN_ID 须为 0–232 的整数；不确定就留空，或点「从机器人读取」');
    settings.ROS_DOMAIN_ID = String(Number(domain));
  }
  if (p.ROS_LOCALHOST_ONLY === '0' || p.ROS_LOCALHOST_ONLY === '1') settings.ROS_LOCALHOST_ONLY = p.ROS_LOCALHOST_ONLY;
  if (rmw !== '') {
    if (!/^rmw_[a-z0-9_]{1,60}$/.test(rmw)) throw Error('通信中间件须形如 rmw_fastrtps_cpp；不确定就留空，或点「从机器人读取」');
    settings.RMW_IMPLEMENTATION = rmw;
  }
  if (file !== '') {
    const key = rbOwn(RB_RMW_FILE, rmw, null);
    if (!key) throw Error('中间件配置文件要和通信中间件一起填：rmw_fastrtps_cpp、rmw_cyclonedds_cpp 或 rmw_zenoh_cpp');
    settings[key] = file;
  }
  const config = String(p.config_text ?? '');
  if (new Blob([config]).size > RB_CONFIG_MAX) throw Error(`本体配置太大（${RB_CONFIG_MAX / 1024} KB 以内）`);
  return {kind, setup: String(p.setup ?? '').trim() || RB_DEFAULT_SETUP, settings, config_text: config};
}

// What "从机器人读取" found, as values for the form and one sentence about where they came from.
function rbFromDetect(found) {
  const installed = found?.installed, running = found?.running || {count: 0, settings: {}};
  const setups = Array.isArray(found?.setups) ? found.setups : [];
  if (installed && installed.adapter === 'ros2') {
    const config = typeof installed.config_text === 'string' ? installed.config_text : null;
    const about = config !== null ? '它的 ROS 2 环境和本体配置已经填进来' : installed.config_bytes
      ? '它的 ROS 2 环境已经填进来；它的本体配置没能读回来（太大，或者不是这个版本认识的格式），请用「导入文件」' : '它的 ROS 2 环境已经填进来；它没有本体配置';
    return {kind: 'ros2', setup: installed.setup || setups[0] || '', settings: installed.settings || {}, config_text: config,
      installed: true, no_config: config === null && !installed.config_bytes,
      message: `机器人上已经有真实本体的管理层：${about}。保存后点「应用到机器人」，这里和机器人上的就一致了。`, tone: 'good'};
  }
  const names = Object.keys(running.settings || {});
  const sim = installed ? '机器人上现在的管理层是仿真本体。' : '';
  if (running.count > 0 && names.length) {
    return {kind: 'ros2', setup: setups[0] || '', settings: running.settings, config_text: null, tone: 'good',
      message: `${sim}从机器人上 ${running.count} 个正在运行的 ROS 2 程序读到了它们用的设置（${names.join('、')}），已经填进来。本体配置读不出来，要你来填。`};
  }
  if (setups.length) {
    return {kind: null, setup: setups[0], settings: {}, config_text: null, tone: 'warn',
      message: `${sim}机器人上有 ROS 2 环境文件 ${setups.join('、')}，但没有读到正在运行的 ROS 2 程序用的设置（它们可能没在运行，或者是用别的账号运行的）。其余设置要你来填。`};
  }
  return {kind: null, setup: '', settings: {}, config_text: null, tone: 'warn',
    message: `${sim}没有在机器人上读到 ROS 2：/opt/ros 下没有环境文件，也没有正在运行的 ROS 2 程序。真实本体需要机器人上装有 ROS 2 Humble。`};
}

// The places of a device configuration and how the robot gets to each, for the preview. [] when there are none
// or the text is not a configuration; the backend's check says what is wrong with it.
function rbPlaces(text) {
  let config;
  try { config = JSON.parse(text); } catch { return []; }
  const places = config?.programs?.places;
  if (!places || typeof places !== 'object' || Array.isArray(places)) return [];
  const stepsOf = list => (Array.isArray(list) ? list : []).filter(s => s && typeof s === 'object').map(s => ({
    title: typeof s.title === 'string' ? s.title : '', command: Array.isArray(s.argv) ? s.argv.map(String).join(' ') : '',
    limit: typeof s.timeout_s === 'number' ? s.timeout_s : null}));
  return Object.keys(places).map(name => {
    const place = places[name] && typeof places[name] === 'object' ? places[name] : {};
    const routes = place.routes && typeof place.routes === 'object' && !Array.isArray(place.routes)
      ? Object.keys(place.routes).map(from => ({from, steps: stepsOf(place.routes[from])}))
      : [{from: '', steps: stepsOf(place.steps)}];
    return {name, region: typeof place.region === 'string' ? place.region : '', routes};
  });
}

// ------------------------------------------------------------------ the card

function rbLine(r) {
  const body = rbBody(r);
  if (body.kind !== 'ros2') return '';
  const [text, tone] = RB_STATE[body.state];
  return `<div class="rb-line"><span class="ri-label">本体</span>${badge('真实本体（ROS 2）· ' + text, tone)}</div>`;
}

function rbCard(html, r) {
  if (!rbCan(r)) return html;
  const anchor = button('安装管理层', 'engine-runtime-install', 'secondary small', r.id);
  const mine = button('本体设置', 'engine-robot-body', 'secondary small', r.id);
  let out = html.includes(anchor) ? html.replace(anchor, () => anchor + mine) : html;
  const line = rbLine(r);
  if (line) out = out.replace('<div class="tags">', () => line + '<div class="tags">');
  return out;
}

// ------------------------------------------------------------------ the dialog

function rbPreview(text, declares) {
  if (!String(text).trim()) return '<p class="rb-none">没有本体配置：管理层用默认的话题名（/cmd_vel、/odom、/battery_state），机器人不认识任何地点。</p>';
  const places = rbPlaces(text);
  const known = declares ? `<p class="rb-declares">装好后这台机器人会声明 ${declares.capabilities.length} 项能力` +
    `${declares.places.length ? `，认识 ${declares.places.length} 个地点：${declares.places.map(n => `<span class="rb-chip">${h(n)}</span>`).join('')}` : '，不认识任何地点'}` +
    `${declares.explore ? '；可以无图自由走' : ''}。</p>` : '';
  const list = places.map(place => `<li><b>${h(place.name)}</b>${place.region ? `<small>在${h(place.region)}</small>` : ''}` +
    place.routes.map(route => `<div class="rb-route">${route.from ? `<em>从${h(route.from)}出发</em>` : ''}` +
      (route.steps.length ? `<ol>${route.steps.map(step => `<li><span>${h(step.title || '（没有名称的步骤）')}</span><code>${h(step.command)}</code>` +
        `${step.limit === null ? '' : `<small>最多 ${h(step.limit)} 秒</small>`}</li>`).join('')}</ol>` : '<p>已经在那里，不用动</p>') +
      '</div>').join('') + '</li>').join('');
  return known + (list ? `<ul class="rb-places">${list}</ul>` : '') +
    (places.length ? '<p class="rb-note">地点的坐标和地图在机器人自己的程序里，这里只有名称和去那里要运行的程序。</p>' : '');
}

function rbForm(robotId, draft = null, message = null) {
  const r = find('robots', robotId);
  if (!rbCan(r)) throw Error('这台机器人没有 SSH 连接；请先通过 SSH 接入');
  const body = draft || rbBody(r), s = body.settings || {};
  const rmw = s.RMW_IMPLEMENTATION || '', fileKey = rbOwn(RB_RMW_FILE, rmw, null);
  const file = fileKey ? s[fileKey] || '' : RB_FILE_KEYS.map(k => s[k]).find(Boolean) || '';
  const pick = (value, title, text) => `<label class="extension-choice rb-kind"><input type="radio" name="kind" value="${value}"${body.kind === value ? ' checked' : ''}>` +
    `<span><b>${title}</b><small>${text}</small></span></label>`;
  const local = v => `<option value="${v}"${(s.ROS_LOCALHOST_ONLY ?? '') === v ? ' selected' : ''}>${v === '' ? '不设置（按机器人的默认）' : v === '1' ? '是（1）' : '否（0）'}</option>`;
  form('本体设置', h(r.name),
    '<p class="inline-note rb-how">管理层装在机器人上，技能通过它指挥机器人。这里决定它接什么本体。保存不会动机器人；' +
    '真实本体要再点「安装管理层」才装上去，改过的本体配置要点「应用到机器人」。</p>' +
    `<div class="extension-picks rb-kinds">${pick('sim', '仿真本体', '不接真实硬件，不会让机器人动。用来先看流程。')}` +
    `${pick('ros2', '真实本体（这台机器人自己的 ROS 2）', '管理层接到机器人自己的 ROS 2 上：技能会让机器人真的动起来。需要机器人上有 ROS 2 Humble。')}</div>` +
    `<div class="rb-real"><div class="rb-detect"><div><b>不知道下面怎么填？</b><span>让控制台去机器人上读一下（只读，不改任何东西）。</span></div>` +
    `${button('从机器人读取', 'engine-robot-body-detect', 'secondary small', robotId)}</div>` +
    `<div class="rb-found${message ? ` is-${h(message.tone)}` : ''}" role="status">${message ? h(message.text) : ''}</div>` +
    '<h4 class="rb-h">ROS 2 环境</h4>' +
    field('ROS 2 环境文件', `<input name="setup" value="${h(body.setup || '')}" placeholder="${RB_DEFAULT_SETUP}" autocomplete="off" spellcheck="false">`,
      '机器人上的 setup.bash：ROS 2 Humble 的，或者叠在它上面的工作区的。') +
    '<div class="rb-grid">' +
    field('ROS_DOMAIN_ID', `<input name="ROS_DOMAIN_ID" inputmode="numeric" value="${h(s.ROS_DOMAIN_ID ?? '')}" placeholder="留空 = 0" autocomplete="off">`,
      '机器人自己的程序所在的域。') +
    field('只在本机通信', `<select name="ROS_LOCALHOST_ONLY">${['', '0', '1'].map(local).join('')}</select>`, 'ROS_LOCALHOST_ONLY') +
    '</div><div class="rb-grid">' +
    field('通信中间件', `<input name="RMW_IMPLEMENTATION" list="rb-rmw" value="${h(rmw)}" placeholder="留空 = ROS 2 默认" autocomplete="off" spellcheck="false">` +
      `<datalist id="rb-rmw">${Object.keys(RB_RMW_FILE).map(name => `<option value="${name}">`).join('')}</datalist>`, 'RMW_IMPLEMENTATION') +
    field('中间件配置文件', `<input name="rmw_file" value="${h(file)}" placeholder="没有就留空" autocomplete="off" spellcheck="false">`,
      '机器人上的路径；按中间件对应 FastDDS 的 profiles、CycloneDDS 的配置或 Zenoh 的 session 配置。') +
    '</div><h4 class="rb-h">本体配置</h4>' +
    '<p class="rb-note">话题名、限值，以及机器人自带导航程序时它认识的地点和去那里要运行的程序（JSON，格式见 docs/PLACE_SKILLS.md）。' +
    '技能只按地点的名称去；坐标留在机器人自己的程序里。</p>' +
    `<div class="rb-tools"><label class="button secondary small rb-import">导入文件<input type="file" accept=".json,application/json" data-rb-file hidden></label>` +
    `${button('填入示例', 'engine-robot-body-example', 'secondary small', robotId)}${button('检查并预览', 'engine-robot-body-check', 'secondary small', robotId)}</div>` +
    `<textarea name="config_text" class="rb-config" rows="12" spellcheck="false" autocomplete="off" aria-label="本体配置（JSON）" placeholder="留空 = 没有本体配置">${h(body.config_text || '')}</textarea>` +
    `<div class="rb-preview" data-rb-preview>${rbPreview(body.config_text || '', null)}</div></div>`,
    'engine-robot-body', '保存', `data-id="${h(robotId)}"`);
  rbSync();
}

// The real body's fields are only for a real body.
function rbSync() {
  if (typeof document === 'undefined') return;
  const f = document.querySelector('#dialog[open] form[data-form="engine-robot-body"]');
  if (!f) return;
  const real = f.querySelector('[name="kind"]:checked')?.value === 'ros2';
  f.querySelector('.rb-real')?.toggleAttribute('hidden', !real);
}

function rbFields() {
  const f = document.querySelector('#dialog[open] form[data-form="engine-robot-body"]');
  return f ? {form: f, values: Object.fromEntries(new FormData(f))} : null;
}

function rbSay(text, tone) {
  const el = document.querySelector('#dialog[open] .rb-found');
  if (!el) return;
  el.className = `rb-found${tone ? ` is-${tone}` : ''}`;
  el.textContent = text;
}

async function rbDetect(robotId) {
  const open = rbFields();
  if (!open) return;
  rbSay('正在从机器人读取…', 'busy');
  let found;
  try {
    found = rbFromDetect(await api('robots/body/detect', {project_id: projectId, robot_id: robotId}));
  } catch (error) {
    return rbSay(error.message || '没能从机器人读取', 'bad');
  }
  const f = rbFields()?.form;
  if (!f || f.dataset.id !== robotId) return;
  const set = (name, value) => { const el = f.elements[name]; if (el && value !== undefined && value !== null) el.value = value; };
  if (found.kind) for (const el of f.querySelectorAll('[name="kind"]')) el.checked = el.value === found.kind;
  if (found.setup) set('setup', found.setup);
  // What an install on the robot recorded replaces every field, also the ones it left empty.
  if (found.installed || Object.keys(found.settings).length) {
    const rmw = found.settings.RMW_IMPLEMENTATION || '', key = rbOwn(RB_RMW_FILE, rmw, null);
    set('ROS_DOMAIN_ID', found.settings.ROS_DOMAIN_ID ?? '');
    set('ROS_LOCALHOST_ONLY', found.settings.ROS_LOCALHOST_ONLY ?? '');
    set('RMW_IMPLEMENTATION', rmw);
    set('rmw_file', key ? found.settings[key] ?? '' : '');
  }
  const config = found.config_text !== null ? found.config_text : found.installed && found.no_config ? '' : null;
  if (config !== null) {
    set('config_text', config);
    rbShowPreview(config, null);
  }
  rbSync();
  rbSay(found.message, found.tone);
}

function rbShowPreview(text, declares, error = '') {
  const el = document.querySelector('#dialog[open] [data-rb-preview]');
  if (el) el.innerHTML = error ? `<p class="rb-bad" role="alert">${h(error)}</p>` : rbPreview(text, declares);
}

async function rbCheck(robotId) {
  const open = rbFields();
  if (!open) return;
  const text = String(open.values.config_text ?? '');
  try {
    const result = await api('robots/body/check', {project_id: projectId, robot_id: robotId, config_text: text});
    rbShowPreview(text, result.empty ? null : result.declares);
  } catch (error) {
    rbShowPreview(text, null, error.message || '本体配置有问题');
  }
}

// What a save led to, and the one thing to do next.
function rbSaved(robotId, result) {
  const body = rbBody({body: result.body}), next = RB_STATE[body.state][2];
  const what = body.kind === 'sim' ? '这台机器人的本体设为仿真本体。'
    : `这台机器人的本体设为真实本体（ROS 2）${result.declares?.places?.length ? `，认识 ${result.declares.places.length} 个地点：${result.declares.places.join('、')}` : ''}。`;
  const todo = body.state === 'reinstall'
    ? '本体的种类或 ROS 2 环境只有重新安装管理层才会生效：点「安装管理层」，在窗口里选“删除后重新安装”（会删掉机器人上原有的 ~/neuxmind，连同装在里面的技能）。'
    : body.state === 'unset' ? '还没有装到机器人上：点「安装管理层」。'
    : next === 'apply' ? '点「应用到机器人」把这份本体配置放到机器人上；管理层会重新启动（正在运行的技能会被停掉）。'
    : body.kind === 'sim' ? '' : '机器人上用的已经是这份，不用再做什么。';
  const action = next === 'install' ? button('安装管理层', 'engine-runtime-install', '', robotId)
    : next === 'apply' ? button('应用到机器人', 'engine-robot-body-apply', '', robotId) : '';
  dialog('本体设置', h(find('robots', robotId)?.name || ''),
    `<div class="dialog-body">${notice(h(what))}${todo ? `<p class="rb-todo">${h(todo)}</p>` : ''}</div>` +
    `<div class="dialog-footer">${button(action ? '以后再说' : '完成', 'close', action ? 'secondary' : '')}${action}</div>`);
}

async function rbSubmit(p, f) {
  const robotId = f.dataset.id;
  const result = await api('robots/body/save', {project_id: projectId, robot_id: robotId, ...rbRequest(p)});
  await refresh(true);
  rbSaved(robotId, result);
}

async function rbApply(robotId) {
  const view = await api('robots/runtime-install/apply-body', {project_id: projectId, robot_id: robotId});
  riInstalls = {...riInstalls, [robotId]: view};
  riShow(robotId, true);
  riPoll(true);
}

// ------------------------------------------------------------------ the install dialog, with a body

function rbInstallBody(robotId) {
  const el = typeof document === 'undefined' ? null : document.querySelector('#dialog[open] form[data-form="engine-runtime-install"] .ri-body-type');
  const r = find('robots', robotId);
  if (!el || !r) return;
  const body = rbBody(r), change = `<button type="button" class="text-link" data-action="engine-robot-body" data-id="${h(robotId)}">更改</button>`;
  if (body.kind !== 'ros2') {
    el.querySelector('span')?.insertAdjacentHTML('beforeend', `。要让机器人真的动，先把本体改成真实本体 ${change}`);
    return;
  }
  el.classList.add('is-real');
  el.innerHTML = `<b>本体</b><span>真实本体（这台机器人自己的 ROS 2，环境文件 <code>${h(body.setup)}</code>）：装好后技能会让机器人真的动起来。` +
    `${body.config_text.trim() ? '' : '还没有本体配置，机器人不会认识任何地点。'} ${change}</span>`;
}

const rbIsApply = view => !!view && view.mode === 'body';

{
  const baseCard = engineRobotCard, baseAction = engineAction, baseSubmit = engineSubmit;
  const baseForm = riForm, baseFound = riFound, baseSummary = riSummary, baseResult = riResult, baseActions = riActions, baseShow = riShow;
  engineRobotCard = r => rbCard(baseCard(r), r);
  riForm = (robotId, port = null) => { baseForm(robotId, port); rbInstallBody(robotId); };
  riFound = found => baseFound(found) + (found.other_body
    ? `<p class="ri-found rb-other">它是为${found.adapter === 'ros2' ? '真实本体' : '仿真本体'}装的，而这台机器人现在的本体设置是${found.adapter === 'ros2' ? '仿真本体' : '真实本体'}。` +
      '换本体只能删除后重新安装：装在里面的技能和运行记录会一起删除。</p>' : '');
  riSummary = view => {
    if (!rbIsApply(view)) return baseSummary(view);
    if (view.state === 'running') return view.stopping ? `正在停止：${view.stage_label || ''}` : `正在把本体配置放到机器人上：${view.stage_label || ''}`;
    if (view.state === 'failed') return `本体配置没有放上去：停在「${view.failure?.stage_label || view.stage_label || ''}」`;
    if (view.state === 'stopped') return '已停止放本体配置';
    return '';
  };
  riResult = view => (rbIsApply(view) && view.state === 'succeeded'
    ? notice('本体配置已经放到机器人上，管理层重新启动并接入了。打开这台机器人的「能力与技能」可以看到它现在认识的地点。') : baseResult(view));
  riActions = (view, robotId) => {
    if (!rbIsApply(view) || view.state === 'running') return baseActions(view, robotId);
    const again = view.state === 'succeeded' ? '' : button('再试一次', 'engine-robot-body-apply', 'secondary', robotId);
    return again + button(view.state === 'succeeded' ? '完成' : '关闭', 'close', view.state === 'succeeded' ? '' : 'secondary');
  };
  riShow = (robotId, fresh = false) => {
    baseShow(robotId, fresh);
    const view = typeof riLive === 'function' ? riLive(robotId) : null;
    const title = typeof document === 'undefined' ? null : document.querySelector('#dialog[open] [data-ri-view]')?.closest('#dialog')?.querySelector('.dialog-head h2');
    if (title && rbIsApply(view)) title.textContent = '应用本体配置';
  };
  engineAction = async (type, id) => {
    if (!type.startsWith('engine-robot-body') || !state.user || !project()) return baseAction(type, id);
    if (type === 'engine-robot-body') return rbForm(id);
    if (type === 'engine-robot-body-detect') return rbDetect(id);
    if (type === 'engine-robot-body-check') return rbCheck(id);
    if (type === 'engine-robot-body-apply') return rbApply(id);
    if (type === 'engine-robot-body-example') {
      const f = rbFields()?.form;
      if (f && !String(f.elements.config_text.value).trim()) { f.elements.config_text.value = RB_EXAMPLE; rbShowPreview(RB_EXAMPLE, null); }
      else rbSay('本体配置里已经有内容，没有覆盖。要用示例，先把它清空。', 'warn');
      return undefined;
    }
    return baseAction(type, id);
  };
  engineSubmit = async (kind, p, f) => (kind === 'engine-robot-body' ? rbSubmit(p, f) : baseSubmit(kind, p, f));
}

if (typeof document !== 'undefined') {
  document.addEventListener('change', event => {
    const target = event.target;
    if (target?.name === 'kind' && target.closest?.('form[data-form="engine-robot-body"]')) return rbSync();
    if (!target?.matches?.('[data-rb-file]')) return undefined;
    const file = target.files?.[0], f = target.closest('form');
    target.value = '';
    if (!file || !f) return undefined;
    if (file.size > RB_CONFIG_MAX) return rbSay(`这个文件太大（${RB_CONFIG_MAX / 1024} KB 以内）`, 'bad');
    file.text().then(text => {
      f.elements.config_text.value = text;
      rbShowPreview(text, null);
      rbSay(`已导入 ${file.name}；点「检查并预览」看它是否可用。`, 'good');
    }, () => rbSay('没能读这个文件', 'bad'));
    return undefined;
  });
}
