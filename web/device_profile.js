'use strict';
// Issue #27 — the device profile of a real machine: its system, its hardware, its ROS 2 (distribution,
// nodes, topics with message types, actions, services) and what its resources are used for, in one
// record. Read over SSH with fixed read-only commands (brainos/device_profile.py, brainos/ros2_profile.py);
// nothing here moves a robot. The last profile is kept, so the dialog opens with it and reads again on request.
// Loaded after live_status.js, whose formatting helpers it uses. It wraps engineRobotCard / engineAction
// instead of editing them, as hardware.js does.

const PROFILE_TABS = {ros2: 'ROS 2', hardware: '硬件', resources: '资源占用'};
const PROFILE_LISTS = {topics: ['话题', '消息类型'], nodes: ['节点', ''], actions: ['动作', '动作类型'], services: ['服务', '服务类型']};
const PROFILE_POLL_SECONDS = 3;           // while a read started elsewhere is still running
const PROFILE_ROWS_SHOWN = 400;           // rows of one list drawn at once; the search narrows the rest down
const PROFILE_GENERATION = {1: 'ROS 1', 2: 'ROS 2'};
// The open profile dialog: {robotId, tab, list, query, profile, busy, failure}. null when none is open.
let profileView = null;
let profileBaseAction = null;

// ---------------------------------------------------------------- ROS 2

const profileDistro = name => name ? name.charAt(0).toUpperCase() + name.slice(1) : '';

// The one line that says where this machine stands: [title, badges, sentence].
function profileVerdict(ros) {
  if (ros.status === 'not_read') return ['还没有读到 ROS 2', '', '这一次没有读到，原因见上面的提示。重新读取会再试一次。'];
  if (ros.status === 'absent') {
    return ['没有读到 ROS 2', badge('未安装或未运行'),
            '这台设备的 /opt/ros 下没有 ROS 发行版，也没有带 ROS 环境的进程在运行。这不算故障：档案里的其他部分照常可用。'];
  }
  const kind = PROFILE_GENERATION[ros.generation] || 'ROS';
  if (!ros.supported) {
    return [`${kind} ${profileDistro(ros.distro)}`, badge('不支持', 'bad'),
            ros.generation === 1 ? '目前只支持 ROS 2 Humble。ROS 1 的节点和话题没有读取。'
                                 : '目前只支持 Humble。下面的清单按同样的只读方式读出，仅供参考，不能据此接入。'];
  }
  const partial = ros.status === 'incomplete' ? badge('没有读全', 'warn') : '';
  return [`${kind} ${profileDistro(ros.distro)}`, badge('支持', 'good') + partial,
          ros.status === 'incomplete' ? '发行版在支持范围内，但这一次有清单没有读到或没有读全，见下面的说明。'
                                      : '发行版在支持范围内。下面是这一刻网络上的节点、话题、动作和服务。'];
}

function profileEnvironment(ros) {
  const env = ros.environment;
  if (!env) return '';
  const installed = (ros.installed || []).map(row => h(profileDistro(row.name)) +
    (row.generation ? `<small>${PROFILE_GENERATION[row.generation]}</small>` : '')).join('、');
  const facts = [
    ['域 ID（ROS_DOMAIN_ID）', env.domain_id === null ? '默认（0）' : h(env.domain_id)],
    ['RMW', env.rmw ? h(env.rmw) : '默认'],
    ['环境变量来自', {process: '正在运行的 ROS 进程', session: 'SSH 会话自己的环境'}[env.source] || '默认设置'],
    ['/opt/ros 下已安装', installed || '没有'],
  ];
  if (env.localhost_only) facts.splice(2, 0, ['只在本机通信', '是（ROS_LOCALHOST_ONLY=1）']);
  return `<dl class="ls-facts dp-env">${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
}

const profileTypes = types => types.map(type => `<code class="dp-type">${h(type)}</code>`).join('');

// The rows of one list that match the search (in the name or a type), and how many there are in all.
function profileMatches(rows, query) {
  const wanted = query.trim().toLowerCase();
  if (!wanted) return rows;
  return rows.filter(row => row.name.toLowerCase().includes(wanted) ||
    (row.types || []).some(type => type.toLowerCase().includes(wanted)));
}

function profileListTable(ros, list, query) {
  const [label, typeHead] = PROFILE_LISTS[list], rows = ros[list];
  if (rows === null) {
    return `<p class="inline-note dp-unread">这一次没有读到${label}。${ros.status === 'unsupported' && ros.generation === 1 ? '' : '原因见上面的说明；'}没有读到不等于没有。</p>`;
  }
  if (!rows.length) {
    return `<p class="inline-note">${list === 'nodes' ? '没有发现正在运行的节点。' : `网络上没有${label}。`}</p>`;
  }
  const matched = profileMatches(rows, query), shown = matched.slice(0, PROFILE_ROWS_SHOWN);
  if (!matched.length) return `<p class="inline-note">没有名称或类型包含“${h(query.trim())}”的${label}。</p>`;
  const body = shown.map(row => `<tr><td><span class="cell-main dp-name">${h(row.name)}</span></td>` +
    (typeHead ? `<td>${profileTypes(row.types)}${row.types.length > 1 ? '<span class="cell-sub">同一个名称上有多种类型，发布方和订阅方对不上</span>' : ''}</td>` : '') + '</tr>');
  const more = matched.length > shown.length
    ? `<p class="inline-note">共 ${matched.length} 项，这里显示前 ${shown.length} 项；用上面的搜索缩小范围。</p>` : '';
  return table(typeHead ? [label, typeHead] : [label], body) + more;
}

function profileLists(ros, list, query) {
  if (!['ok', 'incomplete', 'unsupported'].includes(ros.status) || ros.generation === 1) return '';
  const chips = Object.entries(PROFILE_LISTS).map(([key, [label]]) =>
    `<button type="button" class="ls-chip" data-action="engine-profile-list" data-id="${key}" aria-pressed="${key === list}">` +
    `${label}<span>${ros[key] === null ? '没读到' : ros[key].length}</span></button>`).join('');
  // Nothing to search in a list that was not read or is empty.
  const search = ros[list]?.length
    ? `<input type="search" id="dp-search" class="dp-search" value="${h(query)}" placeholder="按名称或类型筛选" aria-label="按名称或类型筛选" autocomplete="off" spellcheck="false">` : '';
  return `<div class="dp-list-bar"><div class="ls-filters" role="toolbar" aria-label="选择清单">${chips}</div>${search}</div>` +
    `<div id="dp-list">${profileListTable(ros, list, query)}</div>`;
}

function profileRos2(ros, list, query) {
  const [title, badges, sentence] = profileVerdict(ros);
  const listed = ['nodes', 'topics', 'services'].some(key => ros[key] !== null);
  const read = ros.checked_at
    ? `<p class="inline-note">读取于 ${liveWhen(ros.checked_at)}` +
      (typeof ros.duration_ms === 'number' ? `，用时 ${(ros.duration_ms / 1000).toFixed(1)} 秒` : '') + '。' +
      (listed ? '清单是读取那一刻的：节点启动或退出后要重新读取。' : '') +
      '读取只列出，不发布、不调用服务，也不会在机器人上留下进程。</p>' : '';
  return liveStale(ros) +
    `<section class="dp-verdict dp-${h(ros.status)}"><div class="dp-verdict-head"><h3>${h(title)}</h3>${badges}</div><p>${sentence}</p></section>` +
    (ros.notes.length ? notice(ros.notes.map(h).join('<br>'), true) : '') +
    (ros.found ? profileEnvironment(ros) : '') + profileLists(ros, list, query) + read;
}

// ---------------------------------------------------------------- hardware and resources

function profileHardware(profile) {
  const hardware = profile.hardware;
  if (!hardware.devices) return notice(h(hardware.problem || '这一次没有读到硬件。') + ' 重新读取会再读一次。', true);
  const s = hardware.summary, labels = Object.fromEntries(hardware.categories.map(c => [c.key, c.label]));
  const tile = (n, label, cls = '') => `<div class="${cls}"><b>${n}</b><span>${label}</span></div>`;
  const rows = hardware.devices.map(d =>
    `<tr class="ls-row-${h(d.status)}"><td><span class="cell-main">${h(d.name)}</span><span class="cell-sub">${h(d.detail)}</span></td>` +
    `<td>${h(labels[d.category] || d.category)}</td><td>${h(d.bus || '—')}</td><td>${liveDriver(d)}</td>` +
    `<td>${liveBadge(LIVE_DEVICE, d.status)}${d.issue ? `<span class="cell-sub">${h(d.issue)}</span>` : ''}</td></tr>`);
  return liveStale(hardware) +
    (hardware.complete ? '' : notice('这次硬件扫描不完整（输出被截断或缺少结尾），表格可能缺少部分设备。', true)) +
    '<div class="ls-tiles">' + tile(s.devices, '已识别硬件') + tile(s.ok, '工作正常', 'good') +
    tile(s.attention, '需要处理', s.attention ? (hardware.devices.some(d => d.status === 'error') ? 'bad' : 'warn') : '') +     // issue #47: red is for a fault
    tile(s.driver_missing, '未加载驱动') + tile(s.unknown, '未确认') + '</div>' +
    (hardware.notes.length ? notice(hardware.notes.map(h).join('<br>')) : '') +
    table(['设备', '类别', '接口', '驱动', '工作状态'], rows) +
    `<p class="inline-note">和“实时状态”“硬件初始化”用的是同一条只读扫描。读取于 ${liveWhen(hardware.checked_at)}。</p>` +
    (s.attention && typeof hardwareOpen === 'function'
      ? `<div class="button-row">${button('去硬件初始化处理这些问题', 'hardware-open', 'secondary small', profile.robot_id)}</div>` : '');
}

function profileResources(profile) {
  const r = profile.resources, memory = r.memory, disk = r.disk_root;
  const facts = [['已开机', liveDuration(r.uptime_seconds)],
                 ['负载（1 / 5 / 15 分钟）', r.load ? r.load.map(x => x.toFixed(2)).join(' / ') : '—'],
                 ['处理器核心', r.cores ? String(r.cores) : '—'],
                 ['最高温度', typeof r.temperature_c === 'number' ? r.temperature_c.toFixed(1) + ' °C' : '—'],
                 ['内存', liveUsage(memory, memory ? memory.total_kb - memory.available_kb : 0)],
                 ['根分区', liveUsage(disk, disk ? disk.used_kb : 0)]];
  const top = r.top
    ? `<h4>占用资源最高的进程${typeof r.process_count === 'number' ? liveCount(`共 ${r.process_count} 个进程`) : ''}</h4>` +
      table(['程序', ...LIVE_FIGURE_HEADS], r.top.map(row => `<tr><td><span class="cell-main">${h(row.name)}</span></td>${liveFigures(row)}</tr>`)) +
      '<p class="inline-note">CPU 是间隔 1 秒两次采样之间的占用，按单核计：100% 表示占满一个核心。只显示程序名，不显示命令行。</p>'
    : `<p class="inline-note">${h(r.problem || '这一次没有读到进程列表。')}</p>`;
  return (r.problem && r.top ? notice(h(r.problem), true) : '') +
    `<dl class="ls-facts">${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>` + top +
    `<p class="inline-note">读取于 ${liveWhen(r.checked_at)}，在读取 ROS 2 之前：读取 ROS 2 用到的工具不在这些数字里。</p>`;
}

// ---------------------------------------------------------------- the dialog

function profileSystem(profile) {
  const s = profile.system;
  const processor = [s.cpu, s.cores && s.cores + ' 核'].filter(Boolean).join(' · ');
  const system = [s.os, s.kernel && '内核 ' + s.kernel, s.jetson].filter(Boolean).join(' · ');
  const facts = [['主机名', s.hostname], ['型号', s.model], ['处理器', processor], ['系统', system], ['架构', s.arch]].filter(([, value]) => value);
  return `<dl class="ls-facts">${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${h(value)}</dd></div>`).join('')}</dl>`;
}

function profileTabs(profile, tab) {
  const ros = profile.ros2, attention = profile.hardware.summary?.attention || 0;
  const flags = {ros2: ros.status === 'ok' ? '' : {absent: '没有读到', unsupported: '不支持', incomplete: '没有读全', not_read: '没有读到'}[ros.status] || '',
                 hardware: attention ? `${attention} 项需处理` : '', resources: ''};
  const counts = {ros2: ros.topics ? `${ros.topics.length} 个话题` : null,
                  hardware: profile.hardware.summary ? profile.hardware.summary.devices : null, resources: null};
  return '<div class="ls-tabs" role="tablist" aria-label="档案内容">' + Object.entries(PROFILE_TABS).map(([key, label]) =>
    `<button type="button" role="tab" class="ls-tab" id="dp-tab-${key}" aria-selected="${tab === key}" aria-controls="dp-panel" ` +
    `data-action="engine-profile-tab" data-id="${key}">${label}${counts[key] === null ? '' : liveCount(counts[key])}` +
    `${flags[key] ? `<span class="ls-flag${key === 'ros2' && ros.status !== 'unsupported' ? ' dp-soft' : ''}">${flags[key]}</span>` : ''}</button>`).join('') + '</div>';
}

function profileHead(ui) {
  const profile = ui.profile;
  const when = profile ? `<span class="ls-updated" role="status">档案读取于 ${liveWhen(profile.resources.checked_at)}</span>` : '';
  return '<div class="ls-bar"><div class="tags"><span class="tag">真机 · 只读</span><span class="tag">通过 SSH 读取，不会让机器人运动</span></div>' +
    `<div class="ls-refresh">${when}<span class="ls-next" role="status">${ui.busy ? '正在读取…' : ''}</span>` +
    `${button(profile ? '重新读取' : '读取', 'engine-profile-read', 'secondary small', ui.robotId, ui.busy)}</div></div>`;
}

const PROFILE_READING = '正在读取设备档案：先读硬件和资源占用，再读 ROS 2 的节点、话题和服务。只读，通常需要二三十秒。';

function profileBody(ui) {
  const failure = ui.failure ? notice('这一次没有读到设备档案：' + h(ui.failure), true) : '';
  const profile = ui.profile;
  if (!profile) return profileHead(ui) + failure + (ui.busy ? liveLoading(PROFILE_READING) : '');
  const panel = ui.tab === 'hardware' ? profileHardware(profile) : ui.tab === 'resources' ? profileResources(profile)
    : profileRos2(profile.ros2, ui.list, ui.query);
  return profileHead(ui) + failure + (ui.busy ? `<p class="inline-note dp-reading" role="status">${PROFILE_READING}下面是上一次读到的档案。</p>` : '') +
    profileSystem(profile) + profileTabs(profile, ui.tab) +
    `<div class="ls-panel" id="dp-panel" role="tabpanel" aria-labelledby="dp-tab-${ui.tab}">${panel}</div>`;
}

// Whether the dialog still shows the profile that ``ui`` belongs to (another dialog may have replaced it).
const profileShown = ui => Boolean(ui) && profileView === ui && $('#dialog').open &&
  $('#dialog').dataset.deviceProfile === ui.robotId && $('#device-profile');

// Replaces the dialog's content, keeping the scroll position and the keyboard focus on a tab, chip or button.
function profileRender() {
  const ui = profileView;
  if (!profileShown(ui)) return;
  const dialogEl = $('#dialog'), scroll = dialogEl.scrollTop;
  const refocus = document.activeElement?.closest?.('#device-profile [data-action]');
  const selector = refocus && `[data-action="${refocus.dataset.action}"]` +
    (refocus.dataset.id === undefined ? '' : `[data-id="${CSS.escape(refocus.dataset.id)}"]`);
  $('#device-profile').innerHTML = profileBody(ui);
  dialogEl.scrollTop = scroll;
  if (selector) $('#device-profile').querySelector(selector)?.focus({preventScroll: true});
}

async function profileRead() {
  const ui = profileView;
  if (!profileShown(ui) || ui.busy) return;
  ui.busy = true;
  ui.failure = '';
  profileRender();
  try {
    ui.profile = await api('engineering/profile/read', {project_id: projectId, robot_id: ui.robotId});
  } catch (err) {
    // A slow robot can outlast the request (the desktop app's web view gives up on one after a minute)
    // while the read goes on: then its result is waited for instead of calling it a failure.
    const kept = await api('engineering/profile', {project_id: projectId, robot_id: ui.robotId}).catch(() => null);
    if (kept?.reading && profileShown(ui)) {
      ui.profile = kept.profile || ui.profile;
      return profileWait(ui);
    }
    ui.failure = err.message;
  }
  ui.busy = false;
  profileRender();
}

// A read that was started before this dialog opened is still running: show the kept profile until it is done.
async function profileWait(ui) {
  while (profileShown(ui) && ui.busy) {
    await new Promise(resolve => setTimeout(resolve, PROFILE_POLL_SECONDS * 1000));
    if (!profileShown(ui)) return;
    try {
      const kept = await api('engineering/profile', {project_id: projectId, robot_id: ui.robotId});
      ui.profile = kept.profile || ui.profile;
      ui.busy = kept.reading;
    } catch (err) {
      ui.failure = err.message;
      ui.busy = false;
    }
  }
  profileRender();
}

async function profileOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  dialog(h(robot.name), '设备档案 · 硬件、系统、ROS 2 和资源占用',
         `<div class="dialog-body live-status device-profile" id="device-profile">${liveLoading('正在打开设备档案…')}</div>`);
  $('#dialog').dataset.deviceProfile = robotId;
  const ui = profileView = {robotId, tab: 'ros2', list: 'topics', query: '', profile: null, busy: false, failure: ''};
  let kept;
  try {
    kept = await api('engineering/profile', {project_id: projectId, robot_id: robotId});
  } catch (err) {
    ui.failure = err.message;
    return profileRender();
  }
  if (!profileShown(ui)) return;
  ui.profile = kept.profile;
  if (kept.reading) {
    ui.busy = true;
    profileRender();
    return profileWait(ui);
  }
  profileRender();
  if (!kept.profile) return profileRead();        // nothing kept yet: the first read starts by itself
}

function profileAction(type, id) {
  if (type === 'engine-profile') return profileOpen(id);
  const ui = profileView;
  if (!profileShown(ui)) return;
  if (type === 'engine-profile-read') return profileRead();
  if (type === 'engine-profile-tab' && Object.hasOwn(PROFILE_TABS, id)) ui.tab = id;
  if (type === 'engine-profile-list' && Object.hasOwn(PROFILE_LISTS, id)) ui.list = id;
  profileRender();
}

// The card of a real robot with an SSH connection gets "设备档案" next to "实时状态".
function profileCardButton(html, robot) {
  if (robot.sim || !robot.connection_id) return html;
  const status = button('实时状态', 'engine-status', 'secondary small', robot.id);
  return html.replace(status, status + button('设备档案', 'engine-profile', 'secondary small', robot.id));
}

{
  const baseCard = engineRobotCard, baseDialog = dialog;
  profileBaseAction = engineAction;
  engineRobotCard = r => profileCardButton(baseCard(r), r);
  engineAction = async (type, id) => (type.startsWith('engine-profile') && state.user && project()
    ? profileAction(type, id) : profileBaseAction(type, id));
  // Any other dialog replaces the profile: a read that finishes later must not draw over it.
  dialog = (...args) => { delete $('#dialog').dataset.deviceProfile; return baseDialog(...args); };
}
$('#dialog').addEventListener('close', () => { profileView = null; });
// Typing in the search narrows the list in place: the field keeps its focus and its caret.
document.addEventListener('input', e => {
  const ui = profileView;
  if (e.target.id !== 'dp-search' || !profileShown(ui)) return;
  ui.query = e.target.value;
  $('#dp-list').innerHTML = profileListTable(ui.profile.ros2, ui.list, ui.query);
});
render();
