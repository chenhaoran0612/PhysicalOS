// SSH through a jump host (one hop, like `ssh -J`) and the read-only live status of a
// real machine. Loaded after engineering.js and built from its helpers.

const SSH_AUTH_CHOICES = [['password', '密码'], ['key', '私钥'], ['agent', '本机 SSH Agent']];
const SSH_STATUS_POLL_MS = 3000;
const SSH_MAX_FAILED_UNITS = 20;          // the status command lists at most this many
const SSH_SERVICE_STATES = {active: ['运行中', 'good'], failed: ['失败', 'bad'], inactive: ['未运行', ''],
                            activating: ['启动中', ''], deactivating: ['停止中', ''], unknown: ['未知', '']};
const SSH_HOPS = {jump: '跳板机', device: '目标机'};
let sshStatusBusy = false;

// ---------------------------------------------------------------- connection form

function sshCommandField() {
  return field('粘贴 SSH 命令（可选）',
               '<input name="ssh_command" placeholder="ssh -J ops@203.0.113.10:2222 robot@192.168.1.100" autocomplete="off" spellcheck="false">',
               '平时在终端里怎么连，就把那条命令贴进来：地址、端口、用户名和跳板机会自动填好。命令不会被执行。') +
    '<div id="ssh-command-result" hidden></div>';
}

// Setting a value fires `change`, so that the route and the auth selects show the right controls.
function sshSetControl(name, value) {
  const control = $(`#dialog [name="${name}"]`);
  if (!control) return;
  if (control.type === 'checkbox') control.checked = Boolean(value);
  else control.value = value ?? '';
  control.dispatchEvent(new Event('change', {bubbles: true}));
}

function sshCommandSummary(parsed) {
  const {target, jump} = parsed;
  const route = jump ? `，经跳板机 ${h(sshEndpoint(jump))}` : '，直连';
  if (parsed.resolveOnJump) {
    const notes = `<ul>${parsed.notes.map(note => `<li>${h(note)}</li>`).join('')}</ul>`;
    return notice(`已填好：目标名称 ${h(target.host)}${route}。认证方式已设为“本机 SSH Agent”；用密码或私钥的话在下面改。${notes}`);
  }
  const notes = parsed.notes.length ? `<ul>${parsed.notes.map(note => `<li>${h(note)}</li>`).join('')}</ul>` : '';
  return notice(`已填好：目标 ${h(sshEndpoint(target))}${route}。认证方式已设为“本机 SSH Agent”` +
                `（终端里能免密码连上，通常就是它）；用密码或私钥的话在下面改。${notes}`);
}

function sshApplyCommand(text) {
  const box = $('#ssh-command-result');
  if (!text.trim()) { box.hidden = true; return; }
  const parsed = sshParseCommand(text);
  box.hidden = false;
  if (!parsed.ok) {
    box.innerHTML = notice(`${h(parsed.error)}。下面各项没有改动。`, true);
    return;
  }
  const {target, jump} = parsed;
  sshSetControl('host', target.host);
  sshSetControl('port', target.port);
  sshSetControl('username', target.username);
  sshSetControl('route', jump ? 'jump' : 'direct');      // enables the jump controls before they are filled
  sshSetControl('auth', 'agent');
  if (jump) {
    sshSetControl('jump_host', jump.host);
    sshSetControl('jump_port', jump.port);
    sshSetControl('jump_username', jump.username);
    sshSetControl('jump_auth', 'agent');
    sshSetControl('resolve_on_jump', parsed.resolveOnJump);
  }
  // The name follows the command only while it is still the one a command filled in.
  const name = $('#dialog [name="name"]');
  if (name && (!name.value.trim() || name.value === name.dataset.auto)) {
    name.value = target.host;
    name.dataset.auto = target.host;
  }
  box.innerHTML = sshCommandSummary(parsed);
}

// A paste or drop is applied at once. Typing is applied when the field is left or Enter is
// pressed (the change listener below), so that half a command never rewrites the form.
document.addEventListener('input', e => {
  if (e.target.matches('[name=ssh_command]') && ['insertFromPaste', 'insertFromDrop'].includes(e.inputType)) {
    sshApplyCommand(e.target.value);
  }
});

// Controls of the jump host start disabled: a direct connection must neither validate nor send them.
function sshRouteFields() {
  const options = SSH_AUTH_CHOICES.map(([value, text]) => `<option value="${value}">${h(text)}</option>`).join('');
  return field('连接方式', select('route', [['direct', '直连'], ['jump', '经跳板机']]),
               '机器人在内网、要先登录一台中转机器才能到达时，选“经跳板机”。') +
    `<div id="ssh-jump" hidden><div class="field-row">` +
    field('跳板机主机 / IP', '<input name="jump_host" placeholder="203.0.113.10" required disabled>') +
    field('端口', '<input name="jump_port" type="number" min="1" max="65535" value="22" required disabled>') + '</div>' +
    field('跳板机用户名', '<input name="jump_username" placeholder="ops" required disabled>') +
    field('跳板机认证方式', `<select name="jump_auth" required disabled>${options}</select>`) +
    `<div id="ssh-jump-password">${field('跳板机密码', '<input name="jump_password" type="password" autocomplete="new-password" disabled>')}</div>` +
    `<div id="ssh-jump-key" hidden>${field('跳板机私钥', '<textarea name="jump_private_key" spellcheck="false" placeholder="粘贴私钥内容" disabled></textarea>')}` +
    `${field('私钥解密口令（可选）', '<input name="jump_passphrase" type="password" autocomplete="new-password" disabled>')}</div>` +
    '<label class="hardware-confirm"><input type="checkbox" name="resolve_on_jump" value="1" disabled>' +
    '<span>目标机名称按跳板机上的 SSH 配置解析（它是跳板机 ~/.ssh/config 里的名称）。用户名和端口留空时也取那里的配置。</span></label></div>';
}

// A name the jump host resolves brings its own user and port, so those two fields may stay empty.
function sshSyncNamedTarget() {
  const named = $('#dialog [name=route]')?.value === 'jump' && $('#dialog [name=resolve_on_jump]')?.checked;
  for (const name of ['username', 'port']) {
    const control = $(`#dialog [name="${name}"]`);
    if (!control) continue;
    control.required = !named;
    if (name === 'username') control.placeholder = named ? '留空：用跳板机配置里的用户' : 'robot';
  }
}

function sshWatchField(value = '') {
  return field('关注的系统服务（可选，每行一个）',
               `<textarea name="watch_services" spellcheck="false" placeholder="nav-stack.service">${h(value)}</textarea>`,
               '实时状态里会显示这些 systemd 服务是否在运行，最多 12 个。');
}

const sshServiceList = text => String(text || '').split('\n').map(x => x.trim()).filter(Boolean);
const sshEndpoint = e => `${e.username}@${e.host}:${e.port}`;
const sshRouteLabel = c => c?.jump ? `（经跳板机 ${sshEndpoint(c.jump)}${c.alias ? `，跳板机上的名称 ${c.alias}` : ''}）` : '';

function sshScanRequest(draft) {
  const request = {project_id: projectId, host: draft.host, port: Number(draft.port), username: draft.username};
  if (draft.route === 'jump') {
    request.jump = {host: draft.jump_host, port: Number(draft.jump_port), username: draft.jump_username};
    if (draft.resolve_on_jump) {                        // user and port may be left to the jump host's config
      request.resolve_on_jump = true;
      request.port = draft.port ? Number(draft.port) : null;
      request.username = draft.username || '';
    }
  }
  return request;
}

function sshJumpFingerprintForm(scan) {
  return form('第 1 步：确认跳板机指纹', h(sshEndpoint(scan)),
              notice('先核对跳板机。确认后才向跳板机发送认证信息，并通过它读取目标机的指纹。') +
              json({fingerprint: scan.fingerprint, key_type: scan.key_type}) +
              '<label class="hardware-confirm"><input type="checkbox" required><span>已核对这是我要用的跳板机。</span></label>',
              'engine-ssh-jump', '确认并读取目标机指纹');
}

function sshFingerprintForm(scan) {
  const through = scan.jump ? `（经跳板机 ${sshEndpoint(scan.jump)}）` : '';
  const resolved = scan.alias ? `${scan.alias} → ` : '';         // what the jump host's ssh config made of the name
  return form(scan.jump ? '第 2 步：确认目标机指纹' : '确认 SSH 主机指纹', h(resolved + sshEndpoint(scan) + through),
              notice('请与设备管理员或已知主机记录核对指纹。确认后才发送认证信息。') +
              json({fingerprint: scan.fingerprint, key_type: scan.key_type}) +
              '<label class="hardware-confirm"><input type="checkbox" required><span>已核对这是我要连接的设备。</span></label>',
              'engine-ssh-confirm', '确认并连接');
}

async function sshJumpSubmit() {
  if (!sshDraft) throw Error('连接草稿已失效，请重新填写');
  const login = {};
  for (const key of ['jump_check_id', 'trust_jump_fingerprint', 'jump_auth', 'jump_password', 'jump_private_key', 'jump_passphrase']) {
    login[key] = sshDraft[key];
  }
  const scan = await api('engineering/ssh/scan', {...sshScanRequest(sshDraft), ...login});
  sshDraft = {...sshDraft, host_check_id: scan.id, trust_fingerprint: scan.fingerprint};
  return sshFingerprintForm(scan);
}

document.addEventListener('change', e => {
  if (e.target.matches('[name=ssh_command]')) sshApplyCommand(e.target.value);
  if (e.target.matches('[name=route]')) {
    const viaJump = e.target.value === 'jump', box = $('#ssh-jump');
    box.hidden = !viaJump;
    box.querySelectorAll('input,select,textarea').forEach(control => { control.disabled = !viaJump; });
  }
  if (e.target.matches('[name=route],[name=resolve_on_jump]')) sshSyncNamedTarget();
  if (e.target.matches('[name=jump_auth]')) {
    $('#ssh-jump-password').hidden = e.target.value !== 'password';
    $('#ssh-jump-key').hidden = e.target.value !== 'key';
  }
});

// ---------------------------------------------------------------- live status

function sshDuration(seconds) {
  if (typeof seconds !== 'number') return '—';
  const d = Math.floor(seconds / 86400), hrs = Math.floor(seconds % 86400 / 3600), min = Math.floor(seconds % 3600 / 60);
  return d ? `${d} 天 ${hrs} 小时` : hrs ? `${hrs} 小时 ${min} 分` : `${min} 分钟`;
}

const sshGb = kb => (kb / 1048576).toFixed(1) + ' GB';
const sshCell = (label, value) => `<div><b>${label}</b>${value}</div>`;

function sshUsage(part, usedKb) {
  return part ? `${part.used_percent}%（${sshGb(usedKb)} / ${sshGb(part.total_kb)}）` : '—';
}

function sshMetrics(status) {
  const memory = status.memory, disk = status.disk_root;
  return '<div class="detail-meta sim-meta">' +
    sshCell('主机名', h(status.hostname || '—')) +
    sshCell('已开机', sshDuration(status.uptime_seconds)) +
    sshCell('负载（1 / 5 / 15 分钟）', status.load ? status.load.map(x => x.toFixed(2)).join(' / ') : '—') +
    sshCell('最高温度', typeof status.temperature_c === 'number' ? status.temperature_c.toFixed(1) + ' °C' : '—') +
    sshCell('内存', sshUsage(memory, memory ? memory.total_kb - memory.available_kb : 0)) +
    sshCell('根分区', sshUsage(disk, disk ? disk.used_kb : 0)) + '</div>';
}

function sshFailedUnits(units) {
  // null: the device has no usable systemd, which is not the same as "nothing failed".
  if (units === null) return '<p class="inline-note">这台设备没有可用的 systemd，读不到系统服务的状态。</p>';
  if (!units.length) return '<p class="inline-note">没有失败的系统服务。</p>';
  const more = units.length >= SSH_MAX_FAILED_UNITS ? `<p class="inline-note">只显示前 ${SSH_MAX_FAILED_UNITS} 个。</p>` : '';
  return `<div class="tags">${units.map(name => badge(name, 'bad')).join('')}</div>${more}`;
}

function sshServices(status) {
  const watched = status.services.length
    ? status.services.map(s => {
        // The state comes from the device: look it up as an own key only.
        const [text, cls] = Object.hasOwn(SSH_SERVICE_STATES, s.state) ? SSH_SERVICE_STATES[s.state] : [s.state, ''];
        return `<div class="contract-line"><b>${h(s.name)}</b>${badge(text, cls)}</div>`;
      }).join('')
    : '<p class="inline-note">还没有指定要关注的服务。</p>';
  return `<h3>失败的系统服务</h3>${sshFailedUnits(status.failed_units)}<h3>关注的服务</h3>${watched}`;
}

function sshStatusBody(v) {
  const route = h(v.target) + (v.jump ? ` · 经跳板机 ${h(v.jump)}` : ' · 直连');
  // reachable is null while the first connection is still being made.
  const state = v.reachable === null ? badge('正在连接…') : badge(v.reachable ? '在线' : '连不上', v.reachable ? 'good' : 'bad');
  const head = `<div class="tags">${state}` +
    `<span class="tag">真机 · 只读</span><span class="tag">${route}</span>` +
    (v.reachable ? `<span class="tag">往返 ${v.latency_ms} ms</span>` : '') +
    `<span class="tag">更新于 ${new Date(v.checked_at * 1000).toLocaleTimeString()}</span></div>`;
  const problem = v.error ? notice(`<b>${SSH_HOPS[v.error.hop] || '连接'}：</b>${h(v.error.message)}`, true) : '';
  const body = v.status ? sshMetrics(v.status) + sshServices(v.status) : '';
  return head + problem + body +
    `<div class="button-row">${button('修改关注的服务', 'engine-status-services', 'secondary small', v.robot_id)}</div>`;
}

async function sshStatusTick(force = false) {
  const dialogEl = $('#dialog'), robotId = dialogEl.open ? dialogEl.dataset.sshStatus : '';
  if (!robotId || (sshStatusBusy && !force)) return;
  const el = $('#ssh-status');
  if (!el) { delete dialogEl.dataset.sshStatus; return; }      // another dialog took over
  sshStatusBusy = true;
  try {
    const view = await api('engineering/ssh/status', {project_id: projectId, robot_id: robotId});
    if ($('#ssh-status') && dialogEl.dataset.sshStatus === robotId) $('#ssh-status').innerHTML = sshStatusBody(view);
  } catch (err) {
    if ($('#ssh-status') && dialogEl.dataset.sshStatus === robotId) {
      $('#ssh-status').innerHTML = notice('暂时读不到状态：' + h(err.message), true);
    }
  } finally {
    sshStatusBusy = false;
  }
}

function sshStatusOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) throw Error('找不到该机器人');
  dialog(h(robot.name), '真机实时状态 · 每 3 秒通过 SSH 读取一次，只读，不会让机器人运动',
         '<div class="dialog-body" id="ssh-status"><p class="subtitle">正在连接…</p></div>');
  $('#dialog').classList.add('wide');
  $('#dialog').dataset.sshStatus = robotId;
  return sshStatusTick(true);
}

function sshStatusAction(type, id) {
  if (type === 'engine-status') return sshStatusOpen(id);
  if (type === 'engine-status-services') {
    const connection = find('device_connections', find('robots', id)?.connection_id);
    return form('关注的系统服务', '实时状态里显示这些 systemd 服务是否在运行',
                sshWatchField((connection?.watch_services || []).join('\n')),
                'engine-status-services', '保存', `data-id="${h(id)}"`);
  }
}

async function sshServicesSubmit(p, f) {
  const robotId = f.dataset.id;
  await api('engineering/ssh/services', {project_id: projectId, robot_id: robotId, services: sshServiceList(p.watch_services)});
  await refresh(true);
  return sshStatusOpen(robotId);
}

setInterval(() => { if ($('#dialog').open && $('#dialog').dataset.sshStatus) sshStatusTick(); }, SSH_STATUS_POLL_MS);
