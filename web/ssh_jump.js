// SSH through jump hosts: the connection form and its fingerprint steps. Issue #3 added one jump
// host (like `ssh -J`); issue #15 several in a row (like `ssh -J a,b,c`), each with its own
// address, login and fingerprint, confirmed one step per hop. Loaded after engineering.js and built
// from its helpers. The status of a connected machine is in live_status.js (issue #8).

const SSH_AUTH_CHOICES = [['password', '密码'], ['key', '私钥'], ['agent', '本机 SSH Agent']];
const SSH_LOGIN_FIELDS = ['auth', 'password', 'private_key', 'passphrase'];
const SSH_HOP_FIELDS = ['host', 'port', 'username', ...SSH_LOGIN_FIELDS, 'resolve_level', 'resolve_plain'];
const SSH_MAX_HOPS = typeof SSH_MAX_JUMPS === 'number' ? SSH_MAX_JUMPS : 5;       // ssh_command.js; the backend has the same limit

// The first hop keeps the field names it had when there was only one jump host.
const sshJumpPrefix = level => (level === 1 ? 'jump_' : `jump${level}_`);
const sshJumpLabel = (level, total) => (total > 1 ? `第 ${level} 级跳板机` : '跳板机');

// ---------------------------------------------------------------- connection form

function sshCommandField() {
  return field('粘贴 SSH 命令（可选）',
               '<input name="ssh_command" placeholder="ssh -J ops@203.0.113.10:2222 robot@192.168.1.100" autocomplete="off" spellcheck="false">',
               '平时在终端里怎么连，就把那条命令贴进来：地址、端口、用户名和跳板机（可以有多级，如 -J a,b,c）会自动填好。命令不会被执行。') +
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

const sshPlaceText = place => (place.resolveOn ? place.host : sshEndpoint(place));

function sshCommandSummary(parsed) {
  const {target, jumps} = parsed;
  const route = jumps.length ? `，经跳板机 ${h(jumps.map(sshPlaceText).join(' → '))}` : '，直连';
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
  const {target, jumps} = parsed;
  sshSetControl('host', target.host);
  sshSetControl('port', target.port);
  sshSetControl('username', target.username);
  sshSetControl('route', jumps.length ? 'jump' : 'direct');      // enables the jump controls before they are filled
  sshSetControl('auth', 'agent');
  if (jumps.length) {
    // A password or key already typed into a jump host stays in the row of the same level, as it does for the device.
    const typed = sshReadHops();
    sshSetHops(jumps.map((hop, index) => ({host: hop.host, port: hop.port ?? '', username: hop.username, auth: 'agent',
                                           password: typed[index]?.password, private_key: typed[index]?.private_key,
                                           passphrase: typed[index]?.passphrase, resolve: Boolean(hop.resolveOn),
                                           resolve_level: hop.resolveOn ?? '', resolve_plain: hop.plain ? '1' : ''})));
  }
  // Whether the device is given by name belongs to this command, not to one pasted before it.
  $('#dialog [name=resolve_on_level]').value = parsed.resolveOn ?? '';
  $('#dialog [name=resolve_plain]').value = parsed.plain ? '1' : '';
  sshSetControl('resolve_on_jump', parsed.resolveOnJump);
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

// ---------------------------------------------------------------- the jump hosts of the form

// One jump host of the form. Values are set afterwards (sshRenderHops): no typed login is written into markup.
// `off`: the controls start disabled, as long as the route is a direct one.
function sshHopFields(level, total, off = false) {
  const pre = sshJumpPrefix(level), box = level === 1 ? 'ssh-jump' : `ssh-jump${level}`, label = sshJumpLabel(level, total);
  const state = off ? ' disabled' : '';
  const options = SSH_AUTH_CHOICES.map(([value, text]) => `<option value="${value}">${h(text)}</option>`).join('');
  const remove = total > 1 ? `<button type="button" class="text-link" data-ssh-hop="remove" data-level="${level}">删除这一级</button>` : '';
  const named = level === 1 ? '' :
    `<label class="inline-note"><input type="checkbox" name="${pre}resolve" value="1"> <span data-hop-resolve></span></label>` +
    `<input type="hidden" name="${pre}resolve_level"><input type="hidden" name="${pre}resolve_plain">`;
  return `<section class="step-editor ssh-hop" data-hop="${level}" aria-label="${label}">` +
    `<div class="section-head"><h3>${label}</h3>${remove}</div><div class="field-row">` +
    field('跳板机主机 / IP', `<input name="${pre}host" placeholder="203.0.113.10" required${state}>`) +
    field('端口', `<input name="${pre}port" type="number" min="1" max="65535" value="22" required${state}>`) + '</div>' +
    field('跳板机用户名', `<input name="${pre}username" placeholder="ops" required${state}>`) +
    field('跳板机认证方式', `<select name="${pre}auth" data-hop-auth required${state}>${options}</select>`) +
    `<div id="${box}-password" data-hop-password>${field('跳板机密码', `<input name="${pre}password" type="password" autocomplete="new-password"${state}>`)}</div>` +
    `<div id="${box}-key" data-hop-key hidden>${field('跳板机私钥', `<textarea name="${pre}private_key" spellcheck="false" placeholder="粘贴私钥内容"${state}></textarea>`)}` +
    `${field('私钥解密口令（可选）', `<input name="${pre}passphrase" type="password" autocomplete="new-password"${state}>`)}</div>` +
    named + '</section>';
}

const sshHopCount = () => document.querySelectorAll('#ssh-hops .ssh-hop').length;

// What is typed into the jump hosts of the form right now, first hop first.
function sshReadHops() {
  return [...document.querySelectorAll('#ssh-hops .ssh-hop')].map((section, index) => {
    const pre = sshJumpPrefix(index + 1), control = name => section.querySelector(`[name="${pre}${name}"]`);
    const hop = Object.fromEntries(SSH_HOP_FIELDS.map(name => [name, control(name)?.value ?? '']));
    return {...hop, resolve: Boolean(control('resolve')?.checked)};
  });
}

// Shows the given jump hosts in the form (at least one), enabled only while the route goes through them.
function sshRenderHops(hops) {
  const holder = $('#ssh-hops'), enabled = $('#dialog [name=route]')?.value === 'jump';
  const list = hops.length ? hops : [{}];
  holder.innerHTML = list.map((_, index) => sshHopFields(index + 1, list.length)).join('');
  list.forEach((hop, index) => {
    const pre = sshJumpPrefix(index + 1), section = holder.children[index];
    for (const name of SSH_HOP_FIELDS) {
      const control = section.querySelector(`[name="${pre}${name}"]`);
      if (control && hop[name] !== undefined && hop[name] !== null && (hop[name] !== '' || name !== 'auth')) control.value = hop[name];
    }
    const resolve = section.querySelector(`[name="${pre}resolve"]`);
    if (resolve) resolve.checked = Boolean(hop.resolve);
    sshSyncHop(section);
  });
  $('#dialog [name=jump_count]').value = list.length;
  $('#ssh-jump').querySelectorAll('input,select,textarea,button').forEach(control => { control.disabled = !enabled; });
  if (enabled) $('#ssh-hop-add').disabled = list.length >= SSH_MAX_HOPS;
  sshSyncNamedTarget();
}

// Replaces the jump hosts of the form, e.g. by those of a pasted command or of a saved connection.
function sshSetHops(hops) {
  sshRenderHops(hops);
  document.querySelectorAll('#ssh-hops [data-hop-auth]').forEach(control => control.dispatchEvent(new Event('change', {bubbles: true})));
}

// The level whose ssh config resolves a name at `position` (the jump hosts count from 1, the device comes
// after the last): the one a pasted command gave, else the jump host right before it.
const sshResolveLevel = (stored, position) => {
  const level = Number(stored);
  return Number.isInteger(level) && level >= 1 && level < position ? level : position - 1;
};

// One jump host's controls follow its login method and whether it is given by name.
function sshSyncHop(section) {
  const level = Number(section.dataset.hop), pre = sshJumpPrefix(level), total = sshHopCount();
  const control = name => section.querySelector(`[name="${pre}${name}"]`);
  const auth = control('auth').value, named = Boolean(control('resolve')?.checked);
  section.querySelector('[data-hop-password]').hidden = auth !== 'password';
  section.querySelector('[data-hop-key]').hidden = auth !== 'key';
  for (const name of ['username', 'port']) control(name).required = !named;
  control('username').placeholder = named ? '留空：用那台跳板机配置里的用户' : 'ops';
  const text = section.querySelector('[data-hop-resolve]');
  if (text) {
    const by = sshJumpLabel(sshResolveLevel(control('resolve_level').value, level), total);
    text.textContent = `这一级填的是名称，按${by}上的 SSH 配置解析（它是那台跳板机 ~/.ssh/config 里的名称）；用户名和端口留空时也取那里的配置。`;
  }
}

// The jump hosts without the one of level `removed`. A name that was resolved by a jump host behind it
// follows that host to its new level; one that the removed host resolved falls back to the hop before it.
function sshWithoutHop(hops, removed) {
  return hops.filter((_, index) => index + 1 !== removed).map((hop, index) => {
    if (!hop.resolve) return hop;
    if (index === 0) return {...hop, resolve: false, resolve_level: '', resolve_plain: ''};      // the first hop has nobody to ask
    const level = Number(hop.resolve_level);
    return {...hop, resolve_level: level > removed ? level - 1 : level === removed ? '' : hop.resolve_level};
  });
}

document.addEventListener('click', e => {
  const button = e.target.closest('[data-ssh-hop]');
  if (!button || button.disabled) return;
  const form = button.closest('form');                 // looked up now: a "remove" button goes away with its row
  const hops = sshReadHops();
  if (button.dataset.sshHop === 'add' && hops.length < SSH_MAX_HOPS) sshRenderHops([...hops, {auth: hops[hops.length - 1]?.auth}]);
  if (button.dataset.sshHop === 'remove') {
    const removed = Number(button.dataset.level), stored = $('#dialog [name=resolve_on_level]');
    const level = Number(stored.value);
    stored.value = level > removed ? level - 1 : level === removed ? '' : stored.value;
    sshRenderHops(sshWithoutHop(hops, removed));
  }
  form?.dispatchEvent(new Event('change', {bubbles: true}));       // an edit form re-reads what changed
});

// Controls of the jump hosts start disabled: a direct connection must neither validate nor send them.
function sshRouteFields() {
  return field('连接方式', select('route', [['direct', '直连'], ['jump', '经跳板机']]),
               '机器人在内网、要先登录一台或几台中转机器才能到达时，选“经跳板机”。') +
    '<div id="ssh-jump" hidden><p class="inline-note">按连接的先后填写：第 1 级从这台电脑直接登录，最后一级连到机器人。' +
    `每一级用自己的登录方式，指纹逐级确认。最多 ${SSH_MAX_HOPS} 级。</p>` +
    `<div id="ssh-hops">${sshHopFields(1, 1, true)}</div>` +
    '<input type="hidden" name="jump_count" value="1" disabled>' +
    '<p><button type="button" class="button secondary small" id="ssh-hop-add" data-ssh-hop="add" disabled>再加一级跳板机</button></p>' +
    '<label class="hardware-confirm"><input type="checkbox" name="resolve_on_jump" value="1" disabled>' +
    '<span id="ssh-target-resolve">目标机名称按跳板机上的 SSH 配置解析（它是跳板机 ~/.ssh/config 里的名称）。用户名和端口留空时也取那里的配置。</span></label>' +
    '<input type="hidden" name="resolve_on_level" disabled><input type="hidden" name="resolve_plain" disabled></div>';
}

// A name a jump host resolves brings its own user and port, so those two fields may stay empty.
function sshSyncNamedTarget() {
  const viaJump = $('#dialog [name=route]')?.value === 'jump', total = sshHopCount();
  const named = viaJump && $('#dialog [name=resolve_on_jump]')?.checked;
  for (const name of ['username', 'port']) {
    const control = $(`#dialog [name="${name}"]`);
    if (!control) continue;
    control.required = !named;
    if (name === 'username') control.placeholder = named ? '留空：用跳板机配置里的用户' : 'robot';
  }
  const text = $('#ssh-target-resolve');
  if (!text) return;
  const by = sshJumpLabel(sshResolveLevel($('#dialog [name=resolve_on_level]').value, total + 1), total);
  text.textContent = `目标机名称按${by}上的 SSH 配置解析（它是${total > 1 ? '那台' : ''}跳板机 ~/.ssh/config 里的名称）。用户名和端口留空时也取那里的配置。`;
}

function sshWatchField(value = '') {
  return field('关注的系统服务（可选，每行一个）',
               `<textarea name="watch_services" spellcheck="false" placeholder="nav-stack.service">${h(value)}</textarea>`,
               '状态页里把这些 systemd 服务排在最前面，没有运行也会列出，最多 12 个。');
}

const sshServiceList = text => String(text || '').split('\n').map(x => x.trim()).filter(Boolean);
const sshEndpoint = e => `${e.username}@${e.host}:${e.port}`;
const sshHopText = hop => sshEndpoint(hop) + (hop.alias ? `（名称 ${hop.alias}）` : '');
const sshRouteText = jumps => jumps.map(sshHopText).join(' → ');
const sshRouteLabel = c => (c?.jumps?.length ? `（经跳板机 ${sshRouteText(c.jumps)}${c.alias ? `，跳板机上的名称 ${c.alias}` : ''}）` : '');

// ---------------------------------------------------------------- requests

// The jump hosts of a submitted form, first hop first, each with its login fields.
function sshFormJumps(p) {
  if (p.route !== 'jump') return [];
  return Array.from({length: Number(p.jump_count) || 1}, (_, index) => {
    const level = index + 1, pre = sshJumpPrefix(level), named = level > 1 && Boolean(p[pre + 'resolve']);
    const hop = {host: p[pre + 'host'], port: Number(p[pre + 'port']), username: p[pre + 'username']};
    if (named) {                                        // user and port may be left to the resolving host's config
      Object.assign(hop, {port: p[pre + 'port'] ? Number(p[pre + 'port']) : null, username: p[pre + 'username'] || '',
                          resolve_on: sshResolveLevel(p[pre + 'resolve_level'], level)});
      if (p[pre + 'resolve_plain']) hop.plain = true;
    }
    for (const name of SSH_LOGIN_FIELDS) hop[name] = p[pre + name] ?? '';
    return hop;
  });
}

// A draft keeps the jump hosts as the backend last described them; before the first answer, as the form has them.
const sshDraftJumps = draft => draft.jumps ?? sshFormJumps(draft);

// One jump host as a request carries it. Its login only goes along once its fingerprint is confirmed.
function sshHopRequest(hop, withLogin) {
  const request = {host: hop.host, port: hop.port, username: hop.username};
  if (hop.resolve_on) request.resolve_on = hop.resolve_on;
  if (hop.plain) request.plain = true;
  if (withLogin) for (const name of SSH_LOGIN_FIELDS) request[name] = hop[name] ?? '';
  return request;
}

// The request that reads the next fingerprint. `logins`: how many jump hosts send their login along
// (those confirmed so far; all of them when a connection is saved without reading fingerprints again).
function sshScanRequest(draft, logins = draft.jump_level || 0) {
  const request = {project_id: projectId, host: draft.host, port: Number(draft.port), username: draft.username};
  if (draft.edit_robot_id) request.edit_robot_id = draft.edit_robot_id;      // issue #9: an edit of that robot's connection
  const jumps = sshDraftJumps(draft);
  if (!jumps.length) return request;
  request.jumps = jumps.map((hop, index) => sshHopRequest(hop, index < logins));
  if (draft.resolve_on_jump) {                          // user and port may be left to the resolving host's config
    request.resolve_on_jump = true;
    request.resolve_on = sshResolveLevel(draft.resolve_on_level, jumps.length + 1);
    if (draft.resolve_plain) request.resolve_plain = true;
    request.port = draft.port ? Number(draft.port) : null;
    request.username = draft.username || '';
  }
  return request;
}

// The route as the backend now describes it: names resolved, and the jump hosts it found in a jump
// host's ssh config (ProxyJump) added. A hop that was found has no login yet: its step asks for one.
function sshAdoptRoute(draft, scan) {
  const known = [...sshDraftJumps(draft)], {jumps, target} = scan.route;
  if (scan.expanded) {
    known.splice(scan.expanded.at - 1, 0, ...Array.from({length: scan.expanded.count}, () => ({found: true, found_by: scan.expanded.by})));
  }
  const login = hop => Object.fromEntries(SSH_LOGIN_FIELDS.filter(name => hop?.[name] !== undefined).map(name => [name, hop[name]]));
  return {...draft,
          jumps: jumps.map((hop, index) => ({...login(known[index]), host: hop.host, port: hop.port, username: hop.username,
                                             alias: hop.alias, resolve_on: hop.resolve_on, plain: hop.plain,
                                             found: Boolean(known[index]?.found), found_by: known[index]?.found_by})),
          host: target.host, port: target.port, username: target.username, resolve_on_jump: Boolean(target.resolve_on),
          resolve_on_level: target.resolve_on, resolve_plain: Boolean(target.plain)};
}

// ---------------------------------------------------------------- fingerprint steps

// The login of a jump host that was not in the form: asked for in its own fingerprint step. Nothing is
// chosen in advance: its address came from another machine's ssh config, so no key of the local SSH
// agent and no password is offered to it unless the user picks that here.
function sshStepLoginFields(label) {
  const options = SSH_AUTH_CHOICES.map(([value, text]) => `<option value="${value}">${h(text)}</option>`).join('');
  return field(`${label}认证方式`, `<select name="step_auth" required><option value="" selected disabled>请选择登录方式</option>${options}</select>`,
               '选好之前不会向这台机器发送任何登录信息。') +
    `<div id="ssh-step-password" hidden>${field(`${label}密码`, '<input name="step_password" type="password" autocomplete="new-password">')}</div>` +
    `<div id="ssh-step-key" hidden>${field(`${label}私钥`, '<textarea name="step_private_key" spellcheck="false" placeholder="粘贴私钥内容"></textarea>')}` +
    `${field('私钥解密口令（可选）', '<input name="step_passphrase" type="password" autocomplete="new-password">')}</div>`;
}

// What a fingerprint step typed for the jump host it shows (field names start with step_).
const sshStepLogin = p => Object.fromEntries(SSH_LOGIN_FIELDS.filter(name => p?.['step_' + name]).map(name => [name, p['step_' + name]]));

function sshJumpFingerprintForm(scan) {
  const level = scan.level ?? 1, total = scan.total ?? 1, label = sshJumpLabel(level, total);
  const next = level < total ? sshJumpLabel(level + 1, total) : '目标机';
  const hop = sshDraft?.jumps?.[level - 1] || {};
  const resolved = scan.alias ? `${scan.alias} → ` : '';         // what an earlier jump host's ssh config made of the name
  const found = hop.found
    ? notice(`这台跳板机不是你填写的：它的地址是${sshJumpLabel(hop.found_by, total)}上的 SSH 配置（ProxyJump）给出的，要先经过它才能继续。` +
             '请先确认你认识这台机器，再核对指纹、选择它的登录方式；不认识就取消。', true)
    : '';
  const intro = total > 1
    ? `先核对${label}（共 ${total} 级）。确认后才向它发送认证信息，并通过它读取${next}的指纹。`
    : '先核对跳板机。确认后才向跳板机发送认证信息，并通过它读取目标机的指纹。';
  return form(`第 ${level} 步：确认${label}指纹`, h(resolved + sshEndpoint(scan)),
              notice(intro) + found + json({fingerprint: scan.fingerprint, key_type: scan.key_type}) +
              (hop.auth ? '' : sshStepLoginFields(label)) +
              `<label class="hardware-confirm"><input type="checkbox" required><span>已核对这是我要用的${total > 1 ? label : '跳板机'}。</span></label>`,
              'engine-ssh-jump', `确认并读取${next}指纹`);
}

function sshFingerprintForm(scan) {
  const jumps = scan.jumps || [];
  const through = jumps.length ? `（经跳板机 ${sshRouteText(jumps)}）` : '';
  const resolved = scan.alias ? `${scan.alias} → ` : '';         // what a jump host's ssh config made of the name
  return form(jumps.length ? `第 ${jumps.length + 1} 步：确认目标机指纹` : '确认 SSH 主机指纹', h(resolved + sshEndpoint(scan) + through),
              notice('请与设备管理员或已知主机记录核对指纹。确认后才发送认证信息。') +
              json({fingerprint: scan.fingerprint, key_type: scan.key_type}) +
              '<label class="hardware-confirm"><input type="checkbox" required><span>已核对这是我要连接的设备。</span></label>',
              'engine-ssh-confirm', '确认并连接');
}

// The answer to a scan: the fingerprint step of the next jump host, or the device's.
function sshAfterScan(draft, scan) {
  if (scan.stage === 'jump') {
    sshDraft = {...sshAdoptRoute(draft, scan), jump_check_id: scan.id, trust_jump_fingerprint: scan.fingerprint, jump_level: scan.level};
    return sshJumpFingerprintForm(scan);
  }
  sshDraft = {...draft, host_check_id: scan.id, trust_fingerprint: scan.fingerprint};
  return sshFingerprintForm(scan);
}

// A jump host's fingerprint was confirmed: its login goes out, and the next key is read through it.
async function sshJumpSubmit(p) {
  if (!sshDraft) throw Error('连接草稿已失效，请重新填写');
  const level = sshDraft.jump_level, typed = sshStepLogin(p);
  const jumps = sshDraft.jumps.map((hop, index) => (index === level - 1 ? {...hop, ...typed} : hop));
  const draft = {...sshDraft, jumps};
  const scan = await api('engineering/ssh/scan', {...sshScanRequest(draft), jump_check_id: draft.jump_check_id,
                                                  trust_jump_fingerprint: draft.trust_jump_fingerprint});
  return sshAfterScan(draft, scan);
}

document.addEventListener('change', e => {
  if (e.target.matches('[name=ssh_command]')) sshApplyCommand(e.target.value);
  if (e.target.matches('[name=route]')) {
    const viaJump = e.target.value === 'jump', box = $('#ssh-jump');
    box.hidden = !viaJump;
    box.querySelectorAll('input,select,textarea,button').forEach(control => { control.disabled = !viaJump; });
    if (viaJump) $('#ssh-hop-add').disabled = sshHopCount() >= SSH_MAX_HOPS;
  }
  if (e.target.matches('[name=resolve_on_jump]') && e.isTrusted) {       // chosen by hand: the jump host right before the device
    $('#dialog [name=resolve_on_level]').value = '';
    $('#dialog [name=resolve_plain]').value = '';
  }
  if (e.target.matches('[name=route],[name=resolve_on_jump]')) sshSyncNamedTarget();
  const hop = e.target.closest?.('.ssh-hop');
  if (hop) {
    if (e.target.matches('[name$=resolve]') && e.isTrusted) {            // chosen by hand: the jump host right before this one
      hop.querySelector('[name$=resolve_level]').value = '';
      hop.querySelector('[name$=resolve_plain]').value = '';
    }
    sshSyncHop(hop);
  }
  if (e.target.matches('[name=step_auth]')) {
    $('#ssh-step-password').hidden = e.target.value !== 'password';
    $('#ssh-step-key').hidden = e.target.value !== 'key';
  }
});
