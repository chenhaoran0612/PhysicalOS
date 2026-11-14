'use strict';
// Issue #8 — the status of a real machine: system figures, every piece of hardware with its driver
// and working state, and the software that is running. Read over SSH once a minute with fixed
// read-only commands (brainos/device_status.py); nothing here moves a robot.
// Loaded after ssh_jump.js (the connection form) and built from the shared helpers of app.js.

const LIVE_REFRESH_SECONDS = 60;          // used until the server says how often to read (refresh_seconds)
const LIVE_RETRY_SECONDS = 5;             // another read was still running: look again soon
const LIVE_HOPS = {jump: '跳板机', device: '目标机'};
const LIVE_DEVICE = {ok: ['正常', 'good'], warning: ['需处理', 'warn'], error: ['异常', 'bad'], unknown: ['未确认', '']};
const LIVE_SEVERITY = {error: ['错误', 'bad'], warning: ['警告', 'warn'], info: ['提示', '']};
const LIVE_SERVICE = {running: ['运行中', 'good'], active: ['已启动', ''], failed: ['失败', 'bad'], restarting: ['反复重启', 'bad'],
                      activating: ['启动中', 'warn'], deactivating: ['停止中', 'warn'], reloading: ['重新加载中', ''],
                      inactive: ['未运行', ''], unknown: ['未知', '']};
const LIVE_SERVICE_ORDER = ['failed', 'restarting', 'activating', 'deactivating', 'reloading', 'unknown', 'inactive', 'active', 'running'];
const LIVE_CONTAINER = {running: ['运行中', 'good'], unhealthy: ['运行中 · 不健康', 'warn'], restarting: ['反复重启', 'bad'],
                        exited: ['已退出', ''], exited_error: ['异常退出', 'warn'], paused: ['已暂停', ''],
                        created: ['已创建，未启动', ''], dead: ['已损坏', 'bad'], removing: ['正在删除', ''], unknown: ['未知', '']};
const LIVE_PROCESS = {running: ['运行中', 'good'], stopped: ['已暂停', 'warn'], zombie: ['已退出，未回收', 'bad'], unknown: ['未知', '']};
const LIVE_KIND = {ros2_node: 'ROS 2 节点', ros2_launch: 'ROS 2 launch', ros2_daemon: 'ROS 2 守护进程', ros2_tool: 'ROS 2 命令',
                   ros1_master: 'ROS 1 Master', ros1_launch: 'ROS 1 launch', ros1_node: 'ROS 1 节点', agent: '中间件代理'};
const LIVE_DOCKER = {
  missing: ['这台设备没有安装 Docker。', false],
  denied: ['SSH 账号无权访问 Docker（通常是不在 docker 用户组里），读不到容器列表。这不代表没有容器在运行。', true],
  stopped: ['Docker 已安装，但它的服务没有运行。', true],
  timeout: ['Docker 没有及时应答，这一次没有读到容器列表。', true],
  failed: ['没能读到容器列表。', true],
};
const LIVE_TROUBLED_SERVICE = ['failed', 'restarting'];
const LIVE_TROUBLED_CONTAINER = ['unhealthy', 'restarting', 'dead'];
const LIVE_TROUBLED_PROCESS = ['stopped', 'zombie'];
// The open status dialog: {robotId, tab, filter, view, busy, nextAt, failure}. null when none is open.
let liveStatus = null;

// ---------------------------------------------------------------- formatting

// A state comes from the device: it is looked up as an own key only, and shown as text otherwise.
const liveBadge = (map, key) => { const [text, cls] = Object.hasOwn(map, key) ? map[key] : [key, '']; return badge(text, cls); };
const liveClock = seconds => new Date(seconds * 1000).toLocaleTimeString('zh-CN', {hour12: false});
// When a part was read: the time, and the date too once it is not today's (a result kept from an earlier day).
function liveWhen(seconds) {
  const then = new Date(seconds * 1000);
  const day = then.toDateString() === new Date().toDateString() ? '' : then.toLocaleDateString('zh-CN', {month: 'numeric', day: 'numeric'}) + ' ';
  return day + liveClock(seconds);
}
const liveGb = kb => (kb / 1048576).toFixed(1) + ' GB';
const liveCpu = percent => typeof percent === 'number' ? percent.toFixed(1) + '%' : '—';
const liveCount = n => `<span class="ls-count">${n}</span>`;

function liveDuration(seconds) {
  if (typeof seconds !== 'number') return '—';
  if (seconds < 60) return `${seconds} 秒`;
  const d = Math.floor(seconds / 86400), hrs = Math.floor(seconds % 86400 / 3600), min = Math.floor(seconds % 3600 / 60);
  return d ? `${d} 天 ${hrs} 小时` : hrs ? `${hrs} 小时 ${min} 分` : `${min} 分钟`;
}

function liveMemory(kb) {
  if (typeof kb !== 'number') return '—';
  return kb >= 1048576 ? liveGb(kb) : kb >= 1024 ? Math.round(kb / 1024) + ' MB' : kb + ' KB';
}

const liveUsage = (part, usedKb) => part ? `${part.used_percent}%（${liveGb(usedKb)} / ${liveGb(part.total_kb)}）` : '—';
const liveLoading = text => `<div class="ls-loading" role="status"><span class="ls-spinner" aria-hidden="true"></span><p>${text}</p></div>`;
// "This read did not get this part": what is shown is older, so its own time is said.
const liveStale = part => part.problem
  ? notice(h(part.problem) + (part.checked_at ? `。下面是 ${liveWhen(part.checked_at)} 读到的结果。` : '。'), true) : '';

// ---------------------------------------------------------------- head and system

function liveHead(view) {
  const route = h(view.target) + (view.jump ? ` · 经跳板机 ${h(view.jump)}` : ' · 直连');
  // reachable is null while the first connection is still being made.
  const state = view.reachable === null ? badge('正在连接…') : badge(view.reachable ? '在线' : '连不上', view.reachable ? 'good' : 'bad');
  return `<div class="ls-bar"><div class="tags">${state}<span class="tag">真机 · 只读</span><span class="tag">${route}</span>` +
    (view.reachable ? `<span class="tag">往返 ${view.latency_ms} ms</span>` : '') + '</div>' +
    `<div class="ls-refresh"><span class="ls-updated" role="status">上次更新 ${liveClock(view.checked_at)}</span>` +
    `<span class="ls-next" id="ls-next" aria-hidden="true"></span>${button('立即刷新', 'engine-status-refresh', 'secondary small')}</div></div>`;
}

function liveSystem(view) {
  const status = view.status, memory = status.memory, disk = status.disk_root, machine = view.hardware?.system;
  const facts = [['主机名', h(status.hostname || '—')], ['已开机', liveDuration(status.uptime_seconds)],
                 ['负载（1 / 5 / 15 分钟）', status.load ? status.load.map(x => x.toFixed(2)).join(' / ') : '—'],
                 ['最高温度', typeof status.temperature_c === 'number' ? status.temperature_c.toFixed(1) + ' °C' : '—'],
                 ['内存', liveUsage(memory, memory ? memory.total_kb - memory.available_kb : 0)],
                 ['根分区', liveUsage(disk, disk ? disk.used_kb : 0)]];
  if (machine) {                                    // known once the hardware has been read
    const processor = [machine.cpu, machine.cores && machine.cores + ' 核'].filter(Boolean).join(' · ');
    const system = [machine.os, machine.kernel && '内核 ' + machine.kernel, machine.jetson].filter(Boolean).join(' · ');
    for (const [label, value] of [['型号', machine.model], ['处理器', processor], ['系统', system]]) {
      if (value) facts.push([label, h(value)]);
    }
  }
  return `<dl class="ls-facts">${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
}

// ---------------------------------------------------------------- hardware

const liveBroken = device => device.status === 'warning' || device.status === 'error';

function liveDriver(device) {
  if (device.driver_state === 'loaded') return `<span class="ls-driver"><small>已加载</small>${h(device.driver)}</span>`;
  // No kernel driver is a fault for a network card; for a vendor-specific USB interface ("未确认") it is often how
  // the device is meant to be used, through a user-space SDK. Only the first is shown as a fault.
  if (device.driver_state === 'missing') return `<span class="ls-driver missing${liveBroken(device) ? '' : ' soft'}">未加载</span>`;
  if (device.driver_state === 'unknown') return '<span class="ls-driver unknown">读不到</span>';
  return '<span class="ls-driver none" title="这一项不需要内核驱动">—</span>';
}

const LIVE_FILTERS = {'!attention': liveBroken, '!unknown': d => d.status === 'unknown'};
const liveMatches = filter => LIVE_FILTERS[filter] || (d => !filter || d.category === filter);

function liveDeviceTable(hardware, filter) {
  const summary = hardware.summary;
  const chips = [['', '全部', summary.devices], ['!attention', '需要处理', summary.attention], ['!unknown', '未确认', summary.unknown],
                 ...hardware.categories.map(c => [c.key, c.label, c.count])].filter(([key, , count]) => !key || count);
  const labels = Object.fromEntries(hardware.categories.map(c => [c.key, c.label]));
  // A filter whose devices have gone since it was chosen (a USB device was unplugged) falls back to all.
  if (!chips.some(([key]) => key === filter)) filter = '';
  const rows = hardware.devices.filter(liveMatches(filter)).map(d =>
    `<tr class="ls-row-${h(d.status)}"><td><span class="cell-main">${h(d.name)}</span><span class="cell-sub">${h(d.detail)}</span></td>` +
    `<td>${h(labels[d.category] || d.category)}</td><td>${h(d.bus || '—')}</td><td>${liveDriver(d)}</td>` +
    `<td>${liveBadge(LIVE_DEVICE, d.status)}${d.issue ? `<span class="cell-sub">${h(d.issue)}</span>` : ''}</td></tr>`);
  return '<div class="ls-filters" role="toolbar" aria-label="筛选硬件">' + chips.map(([key, label, count]) =>
    `<button type="button" class="ls-chip" data-action="engine-status-filter" data-id="${h(key)}" aria-pressed="${key === filter}">` +
    `${h(label)}<span>${count}</span></button>`).join('') + '</div>' +
    table(['设备', '类别', '接口', '驱动', '工作状态'], rows);
}

// Problems of the machine as a whole (no default route, a full disk, kernel log entries). Failed services are software.
function liveSystemIssues(hardware) {
  const issues = hardware.issues.filter(i => !i.device_id && i.category !== 'service');
  if (!issues.length) return '';
  return `<h4>不属于单个设备的问题${liveCount(issues.length)}</h4><ul class="ls-issues">` + issues.map(i =>
    `<li>${liveBadge(LIVE_SEVERITY, i.severity)}<div><b>${h(i.title)}</b><p>${h(i.detail)}</p></div></li>`).join('') + '</ul>';
}

function liveHardware(view, filter) {
  const hardware = view.hardware;
  if (!hardware) return liveLoading('正在读取硬件：只读扫描处理器、GPU、相机、串口、网卡、USB 与 PCI 设备及其驱动，通常需要几秒。');
  if (!hardware.devices) return notice(h(hardware.problem || '还没有读到硬件。') + ' 下一次刷新会再读一次。', true);
  const s = hardware.summary;
  const tile = (n, label, cls = '') => `<div class="${cls}"><b>${n}</b><span>${label}</span></div>`;
  const incomplete = hardware.complete ? '' : notice('这次硬件扫描不完整（输出被截断或缺少结尾），表格可能缺少部分设备。', true);
  const driverFault = hardware.devices.some(d => d.driver_state === 'missing' && liveBroken(d));
  return liveStale(hardware) + incomplete +
    '<div class="ls-tiles">' + tile(s.devices, '已识别硬件') + tile(s.ok, '工作正常', 'good') +
    tile(s.attention, '需要处理', s.attention ? 'bad' : '') + tile(s.driver_missing, '未加载驱动', driverFault ? 'bad' : '') +
    tile(s.unknown, '未确认') + '</div>' +
    (hardware.notes.length ? notice(hardware.notes.map(h).join('<br>')) : '') +
    liveDeviceTable(hardware, filter) + liveSystemIssues(hardware) +
    '<p class="inline-note">“正常”表示系统识别到了设备、驱动已加载，链路和访问权限没有发现问题；不代表已经通过功能测试（相机画面、底盘控制等要各自验证）。' +
    `“未确认”多为厂商自定义接口，可能由用户态程序直接驱动。读取于 ${liveWhen(hardware.checked_at)}` +
    (typeof hardware.duration_ms === 'number' ? `，用时 ${(hardware.duration_ms / 1000).toFixed(1)} 秒` : '') + '。</p>' +
    (s.attention && typeof hardwareOpen === 'function'
      ? `<div class="button-row">${button('去硬件初始化处理这些问题', 'hardware-open', 'secondary small', view.robot_id)}</div>` : '');
}

// ---------------------------------------------------------------- software

const liveFigures = row => `<td>${liveBadge(LIVE_PROCESS, row.state)}</td><td class="ls-number">${liveCpu(row.cpu_percent)}</td>` +
  `<td class="ls-number">${liveMemory(row.memory_kb)}</td><td>${liveDuration(row.elapsed_seconds)}</td><td>${h(row.user)}</td>`;
const LIVE_FIGURE_HEADS = ['状态', 'CPU', '内存', '已运行', '用户'];

function liveMiddleware(software) {
  const rows = software.middleware;
  const head = `<h4>机器人中间件${rows ? liveCount(rows.length) : ''}</h4>`;
  if (!rows) return head + '<p class="inline-note">读不到这台设备的进程列表（它的 ps 不支持所需的输出格式）。</p>';
  if (!rows.length) {
    return head + '<p class="inline-note">没有发现正在运行的 ROS 节点、launch 或中间件代理。这里按进程识别，机器人程序用别的方式运行时不会出现在这里。</p>';
  }
  // Under the name: the package and program of a node, the program of an agent. A launch or a command is named in full already.
  const origin = row => row.package ? `${row.package} · ${row.program}` : row.kind === 'agent' ? row.program : '';
  return head + table(['名称', '类型', ...LIVE_FIGURE_HEADS], rows.map(row =>
    `<tr><td><span class="cell-main">${h(row.name)}</span><span class="cell-sub">${h(origin(row))}</span></td>` +
    `<td>${h(Object.hasOwn(LIVE_KIND, row.kind) ? LIVE_KIND[row.kind] : row.kind)}</td>${liveFigures(row)}</tr>`));
}

function liveServices(software, robotId) {
  const rows = software.services;
  const edit = button('修改关注的服务', 'engine-status-services', 'secondary small', robotId);
  if (!rows) {
    return `<div class="section-head"><h4>系统服务</h4>${edit}</div><p class="inline-note">读不到这台设备的系统服务：它没有可用的 systemd，或 systemd 没有及时应答。</p>`;
  }
  const rank = row => (row.watched ? 0 : 100) + (LIVE_SERVICE_ORDER.indexOf(row.state) + 1 || LIVE_SERVICE_ORDER.length);
  const sorted = [...rows].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  const running = rows.filter(r => r.state === 'running').length, troubled = rows.filter(r => LIVE_TROUBLED_SERVICE.includes(r.state)).length;
  return `<div class="section-head"><h4>系统服务${liveCount(`${running} 个在运行` + (troubled ? ` · ${troubled} 个异常` : ''))}</h4>${edit}</div>` +
    table(['服务', '说明', '状态'], sorted.map(row =>
      `<tr class="${LIVE_TROUBLED_SERVICE.includes(row.state) ? 'ls-row-error' : ''}"><td><span class="cell-main">${h(row.name)}` +
      `${row.watched ? '<span class="ls-watched">关注</span>' : ''}</span></td><td class="ls-wrap">${h(row.description || '—')}</td>` +
      `<td>${liveBadge(LIVE_SERVICE, row.state)}</td></tr>`)) +
    '<p class="inline-note">列出正在运行、启动中、失败和反复重启的服务；你关注的服务排在最前面，没有运行也会列出。</p>';
}

function liveContainers(software) {
  const containers = software.containers;
  if (!containers) return '';
  if (containers.state !== 'ok') {
    const [text, warn] = Object.hasOwn(LIVE_DOCKER, containers.state) ? LIVE_DOCKER[containers.state] : LIVE_DOCKER.failed;
    return '<h4>Docker 容器</h4>' + (warn ? notice(text, true) : `<p class="inline-note">${text}</p>`);
  }
  const head = `<h4>Docker 容器${liveCount(containers.items.length)}</h4>`;
  if (!containers.items.length) return head + '<p class="inline-note">没有容器。</p>';
  return head + table(['容器', '镜像', '状态'], containers.items.map(row =>
    `<tr class="${LIVE_TROUBLED_CONTAINER.includes(row.state) ? 'ls-row-error' : ''}"><td><span class="cell-main">${h(row.name)}</span></td>` +
    `<td class="ls-wrap">${h(row.image)}</td><td>${liveBadge(LIVE_CONTAINER, row.state)}<span class="cell-sub">${h(row.status)}</span></td></tr>`));
}

function liveTop(software) {
  if (!software.top) return '';
  const total = typeof software.process_count === 'number' ? liveCount(`共 ${software.process_count} 个进程`) : '';
  const cores = software.cores ? `，这台机器有 ${software.cores} 个核心` : '';
  return `<h4>占用资源最高的进程${total}</h4>` + table(['程序', ...LIVE_FIGURE_HEADS], software.top.map(row =>
    `<tr><td><span class="cell-main">${h(row.name)}</span></td>${liveFigures(row)}</tr>`)) +
    `<p class="inline-note">CPU 是间隔 1 秒两次采样之间的占用，按单核计：100% 表示占满一个核心${cores}。按 CPU 排序，另外列出内存占用最高的几个。只显示程序名，不显示命令行。</p>`;
}

function liveSoftware(view) {
  const software = view.software;
  if (!software) return liveLoading('正在读取软件：系统服务、容器和进程，需要一两秒。');
  const read = software.checked_at ? `<p class="inline-note">读取于 ${liveWhen(software.checked_at)}。</p>` : '';
  const block = (name, html) => html && `<section class="ls-block" data-block="${name}">${html}</section>`;
  // Never read (the first read timed out): only what the basic status gave, the watched services, can be shown.
  const known = Boolean(software.checked_at);
  return liveStale(software) + (software.notes.length ? notice(software.notes.map(h).join('<br>'), true) : '') +
    block('middleware', known ? liveMiddleware(software) : '') +
    block('services', known || software.services ? liveServices(software, view.robot_id) : '') +
    block('containers', liveContainers(software)) + block('top', liveTop(software)) + read;
}

function liveSoftwareTroubles(software) {
  if (!software) return 0;
  const count = (rows, states) => (rows || []).filter(row => states.includes(row.state)).length;
  return count(software.services, LIVE_TROUBLED_SERVICE) + count(software.containers?.items, LIVE_TROUBLED_CONTAINER) +
    count(software.middleware, LIVE_TROUBLED_PROCESS);
}

// ---------------------------------------------------------------- the dialog

function liveTabs(view, tab) {
  const attention = view.hardware?.summary?.attention || 0, troubles = liveSoftwareTroubles(view.software);
  const running = view.software?.services ? view.software.services.filter(r => r.state === 'running').length : null;
  const one = (key, label, count, flag) =>
    `<button type="button" role="tab" class="ls-tab" id="ls-tab-${key}" aria-selected="${tab === key}" aria-controls="ls-panel" ` +
    `data-action="engine-status-tab" data-id="${key}">${label}${count === null ? '' : liveCount(count)}` +
    `${flag ? `<span class="ls-flag">${flag}</span>` : ''}</button>`;
  return '<div class="ls-tabs" role="tablist" aria-label="状态内容">' +
    one('hardware', '硬件', view.hardware?.summary ? view.hardware.summary.devices : null, attention ? `${attention} 项需处理` : '') +
    one('software', '软件', running === null ? null : `${running} 个服务在运行`, troubles ? `${troubles} 项异常` : '') + '</div>';
}

function liveBody(ui) {
  const view = ui.view;
  const failure = ui.failure ? notice('这一次没有读到状态：' + h(ui.failure), true) : '';
  if (!view) return failure || liveLoading('正在连接…');
  const problem = view.error ? notice(`<b>${LIVE_HOPS[view.error.hop] || '连接'}：</b>${h(view.error.message)}`, true) : '';
  if (!view.status) return liveHead(view) + failure + problem + (view.reachable === null ? liveLoading('正在登录设备…') : '');
  const panel = ui.tab === 'software' ? liveSoftware(view) : liveHardware(view, ui.filter);
  return liveHead(view) + failure + problem + liveSystem(view) + liveTabs(view, ui.tab) +
    `<div class="ls-panel" id="ls-panel" role="tabpanel" aria-labelledby="ls-tab-${ui.tab}">${panel}</div>`;
}

// Whether this dialog still shows the status that ``ui`` belongs to (another dialog may have replaced it).
const liveShown = ui => Boolean(ui) && liveStatus === ui && $('#dialog').open && $('#dialog').dataset.sshStatus === ui.robotId && $('#ssh-status');

function liveCountdown() {
  const ui = liveStatus, el = $('#ls-next');
  if (!ui || !el) return;
  const seconds = Math.max(0, Math.ceil((ui.nextAt - Date.now()) / 1000));
  el.textContent = ui.busy ? '正在读取…' : `${seconds} 秒后自动刷新`;
  const refresh = $('#ssh-status [data-action="engine-status-refresh"]');
  if (refresh) refresh.disabled = ui.busy;
}

// Replaces the dialog's content, keeping the scroll position and the keyboard focus on a tab or chip.
function liveRender() {
  const ui = liveStatus;
  if (!liveShown(ui)) return;
  const dialogEl = $('#dialog'), scroll = dialogEl.scrollTop, focused = document.activeElement;
  const refocus = focused?.closest?.('#ssh-status [data-action]');
  const selector = refocus && `[data-action="${refocus.dataset.action}"]` +
    (refocus.dataset.id === undefined ? '' : `[data-id="${CSS.escape(refocus.dataset.id)}"]`);
  $('#ssh-status').innerHTML = liveBody(ui);
  dialogEl.scrollTop = scroll;
  if (selector) $('#ssh-status').querySelector(selector)?.focus({preventScroll: true});
  liveCountdown();
}

async function liveRead(quick = false) {
  const ui = liveStatus;
  if (!liveShown(ui) || ui.busy) return;
  ui.busy = true;
  liveCountdown();
  let wait = ui.view?.refresh_seconds || LIVE_REFRESH_SECONDS;
  try {
    const view = await api('engineering/ssh/status', {project_id: projectId, robot_id: ui.robotId, quick});
    ui.view = view;
    ui.failure = '';
    // Another read of this robot was still running: what came back is the previous result.
    wait = view.refreshing ? LIVE_RETRY_SECONDS : view.refresh_seconds || LIVE_REFRESH_SECONDS;
  } catch (err) {
    ui.failure = err.message;
  } finally {
    ui.busy = false;
    ui.nextAt = Date.now() + wait * 1000;
  }
  liveRender();
}

async function sshStatusOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  const tab = liveStatus?.robotId === robotId ? liveStatus.tab : 'hardware';     // back from the watched-services form: same tab
  dialog(h(robot.name), '真机状态 · 每分钟通过 SSH 读取一次，只读，不会让机器人运动',
         `<div class="dialog-body live-status" id="ssh-status">${liveLoading('正在连接…')}</div>`);
  $('#dialog').dataset.sshStatus = robotId;
  liveStatus = {robotId, tab, filter: '', view: null, busy: false, nextAt: 0, failure: ''};
  await liveRead(true);                           // the basic figures first: about a second
  liveRead(false);                                // then hardware and software, which take several
}

function sshStatusAction(type, id) {
  if (type === 'engine-status') return sshStatusOpen(id);
  if (type === 'engine-status-services') {
    const connection = find('device_connections', find('robots', id)?.connection_id);
    return form('关注的系统服务', '状态页里把这些 systemd 服务排在最前面，没有运行也会列出',
                sshWatchField((connection?.watch_services || []).join('\n')),
                'engine-status-services', '保存', `data-id="${h(id)}"`);
  }
  if (!liveStatus) return;
  if (type === 'engine-status-refresh') return liveRead(false);
  if (type === 'engine-status-tab' && ['hardware', 'software'].includes(id)) liveStatus.tab = id;
  if (type === 'engine-status-filter') liveStatus.filter = id || '';
  liveRender();
}

async function sshServicesSubmit(p, f) {
  const robotId = f.dataset.id;
  await api('engineering/ssh/services', {project_id: projectId, robot_id: robotId, services: sshServiceList(p.watch_services)});
  await refresh(true);
  return sshStatusOpen(robotId);
}

$('#dialog').addEventListener('close', () => { liveStatus = null; });
// One ticker: it counts down to the next read and starts it. Nothing is read while no status dialog is open.
setInterval(() => {
  const ui = liveStatus;
  if (!ui) return;
  if (!liveShown(ui)) {                             // another dialog took over
    if ($('#dialog').open && !$('#dialog form[data-form="engine-status-services"]')) liveStatus = null;
    return;
  }
  liveCountdown();
  if (!ui.busy && ui.nextAt && Date.now() >= ui.nextAt) liveRead(false);
}, 1000);
