// SSH through a jump host (one hop, like `ssh -J`): the connection form and its two fingerprint
// steps. Loaded after engineering.js and built from its helpers. The status of a connected
// machine is in live_status.js (issue #8).

const SSH_AUTH_CHOICES = [['password', '密码'], ['key', '私钥'], ['agent', '本机 SSH Agent']];

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
               '状态页里把这些 systemd 服务排在最前面，没有运行也会列出，最多 12 个。');
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
