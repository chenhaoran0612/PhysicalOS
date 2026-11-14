'use strict';
// Issue #9: "配置连接" on a robot connected over SSH opens the SSH form that connected it, filled
// with the saved connection, and saves the change to that robot's one connection.
// Loaded last. It wraps engineRobotCard / engineAction / engineSubmit instead of editing them and
// reuses the form, the fingerprint steps and the request builders of engineering.js and ssh_jump.js.
// Passwords and keys are never sent to the page: an empty field keeps the saved login.

const SSH_EDIT_SECRETS = {password: ['password', '密码'], key: ['private_key', '私钥']};
const SSH_EDIT_SECRET_FIELDS = ['password', 'private_key', 'passphrase', 'jump_password', 'jump_private_key', 'jump_passphrase'];
const SSH_EDIT_FORM = '#dialog form[data-form="engine-ssh"]';
const SSH_EDIT_SESSION_NAMES = {discovery: '调查', repair: '修复'};      // as brainos/ssh_edit.py names them
let sshEditBaseAction = null, sshEditBaseSubmit = null;

const sshEditSaved = robotId => find('device_connections', find('robots', robotId)?.connection_id);
const sshEditBusy = robotId => rows('agent_sessions').find(s => s.robot_id === robotId && engineActive(s.status));

// The hops of a request as the form names their fields, each with what is saved for it.
function sshEditHops(p, saved) {
  const hops = [{prefix: '', label: '设备', title: '', saved}];
  if (p.route === 'jump') hops.push({prefix: 'jump_', label: '跳板机', title: '跳板机 ', saved: saved.jump || null});
  return hops;
}

// True when a hop's address changed, or a name has to be resolved again: the fingerprints are then
// read and confirmed again, as for a new connection. The backend applies the same rule.
function sshEditAddressChanged(p, saved) {
  const viaJump = p.route === 'jump';
  if (viaJump !== Boolean(saved.jump) || p.host.trim() !== saved.host || Number(p.port) !== saved.port) return true;
  return viaJump && (Boolean(p.resolve_on_jump) || p.jump_host.trim() !== saved.jump.host || Number(p.jump_port) !== saved.jump.port);
}

// A login method other than the saved one has nothing to fall back on: say so before any step is taken.
function sshEditMissingSecret(p, saved) {
  for (const hop of sshEditHops(p, saved)) {
    const auth = p[hop.prefix + 'auth'], [field, name] = SSH_EDIT_SECRETS[auth] || [];
    if (field && !p[hop.prefix + field] && auth !== hop.saved?.auth) return `请输入${hop.title}SSH ${name}`;
  }
  return '';
}

// ---------------------------------------------------------------- the form

function sshEditBusyDialog(robot, session) {
  const kind = SSH_EDIT_SESSION_NAMES[session.mode] || '工程';
  dialog('配置连接', h(robot.name),
         `<div class="dialog-body">${notice(`这台机器人有${kind}会话正在运行，现在改连接会让它中途断开。请等它结束，或先停止它，再修改连接。`, true)}` +
         `<div class="button-row">${button('查看这次会话', 'engine-session', 'small', session.id)}${button('关闭', 'close', 'secondary small')}</div></div>`);
}

function sshEditKeepHint(prefix, auth) {
  const hints = {password: [['password', '留空：沿用已保存的密码']],
                 key: [['private_key', '留空：沿用已保存的私钥'], ['passphrase', '留空：沿用已保存的口令']]};
  for (const [name, text] of hints[auth] || []) {
    const control = $(`#dialog [name="${prefix}${name}"]`);
    if (control) control.placeholder = text;
  }
}

function sshEditNotes(robot, saved) {
  const alias = saved.alias
    ? notice(`接入时，目标机用的是跳板机上的名称“${h(saved.alias)}”；这里显示的是它当时解析出的地址。` +
             '要按名称重新解析，把主机改回这个名称，并勾选“目标机名称按跳板机上的 SSH 配置解析”。')
    : '';
  return alias +
    '<div id="ssh-edit-route" hidden>' +
    notice('主机或跳板机的地址变了：保存前要重新读取并确认主机指纹（经跳板机时两跳都要），和新接入时一样。', true) + '</div>' +
    '<label class="hardware-confirm"><input type="checkbox" name="rescan" value="1">' +
    '<span>重新读取并确认主机指纹。地址没变、但设备重装过系统，或提示“指纹与已确认的不一致”时勾选。</span></label>' +
    '<p class="inline-note">机器人上已经运行 NeuxMind 运行时或兼容的接入服务？' +
    `<button type="button" class="text-link" data-action="robot-config" data-id="${h(robot.id)}">配置接入服务的地址和令牌 ↗</button></p>`;
}

// Turns the open "connect over SSH" form into the edit form of this robot.
function sshEditFill(robot, saved) {
  const form = $(SSH_EDIT_FORM);
  if (!form) return;
  $('#dialog .dialog-head h2').textContent = '配置连接';
  $('#dialog .dialog-head p').textContent = `${robot.name} · 修改这台机器人的 SSH 连接。保存前会用新的设置登录一次，登录不上就不保存。`;
  const values = {host: saved.host, port: saved.port, username: saved.username, auth: saved.auth,
                  route: saved.jump ? 'jump' : 'direct', read_roots: saved.read_roots.join('\n'),
                  watch_services: (saved.watch_services || []).join('\n')};
  if (saved.jump) {
    Object.assign(values, {jump_host: saved.jump.host, jump_port: saved.jump.port,
                           jump_username: saved.jump.username, jump_auth: saved.jump.auth});
  }
  // route comes before the jump fields in this order, so they are enabled by the time they are filled
  for (const [name, value] of Object.entries(values)) sshSetControl(name, value);
  sshEditKeepHint('', saved.auth);
  if (saved.jump) sshEditKeepHint('jump_', saved.jump.auth);
  const body = form.querySelector('.dialog-body');
  body.querySelector('.form-error').insertAdjacentHTML('afterend',
    notice('密码和私钥不会显示在这里。留空就沿用已保存的；已保存的登录信息只会发给指纹和原来一致的那台机器。'));
  body.insertAdjacentHTML('beforeend', sshEditNotes(robot, saved) +
                          `<input type="hidden" name="edit_robot_id" value="${h(robot.id)}">`);
  sshEditSync();
}

// Keeps the submit button and the route note in step with what is typed.
function sshEditSync() {
  const form = $(SSH_EDIT_FORM), saved = sshEditSaved(form?.elements.edit_robot_id?.value);
  if (!saved) return;
  const p = Object.fromEntries(new FormData(form)), moved = sshEditAddressChanged(p, saved);
  const note = $('#ssh-edit-route'), button = form.querySelector('[type=submit]');
  const label = moved || p.rescan ? '读取主机指纹' : '保存';
  if (note.hidden === moved) note.hidden = !moved;
  // Changed in place and only when it differs: leaving a field fires `change` between the mousedown and
  // the mouseup of a click on this button, and WebKit drops a click whose mousedown node was replaced.
  if (button.textContent !== label) button.firstChild.nodeValue = label;
}

function sshEditOpen(robotId) {
  const robot = find('robots', robotId), saved = sshEditSaved(robotId);
  if (!robot || !saved) return action('robot-config', robotId);        // not connected over SSH: service address and token
  const busy = sshEditBusy(robotId);
  if (busy) return sshEditBusyDialog(robot, busy);
  return Promise.resolve(sshEditBaseAction('engine-ssh', robotId)).then(() => sshEditFill(robot, saved));
}

// ---------------------------------------------------------------- fingerprint steps

function sshEditSecretFields(hop, auth) {
  const why = `已保存的${SSH_EDIT_SECRETS[auth][1]}不会发给另一台机器，请重新输入。`;
  if (auth === 'password') {
    return field(`${hop.title}SSH 密码`, `<input name="${hop.prefix}password" type="password" autocomplete="new-password" required>`, why);
  }
  return field(`${hop.title}SSH 私钥`, `<textarea name="${hop.prefix}private_key" spellcheck="false" placeholder="粘贴私钥内容" required></textarea>`, why) +
    field('私钥解密口令（可选）', `<input name="${hop.prefix}passphrase" type="password" autocomplete="new-password">`);
}

// A fingerprint step of an edit is on screen: say whether this is the machine the robot was connected
// to before, and ask for the login again when the saved one may not be sent to it.
function sshEditDecorateStep() {
  const draft = sshDraft, form = $('#dialog form'), saved = sshEditSaved(draft?.edit_robot_id);
  const confirm = form?.querySelector('.hardware-confirm');
  if (!saved || !confirm) return;
  const device = Boolean(draft.host_check_id);          // the device's key has been read: this is its step
  const hop = sshEditHops({route: 'jump'}, saved)[device ? 0 : 1];
  const fingerprint = device ? draft.trust_fingerprint : draft.trust_jump_fingerprint;
  const auth = draft[hop.prefix + 'auth'], secret = (SSH_EDIT_SECRETS[auth] || [])[0];
  let html;
  if (!hop.saved) {
    html = notice('这台机器人以前没有经过跳板机连接，这是第一次保存这台跳板机的指纹。');
  } else if (fingerprint === hop.saved.fingerprint) {
    html = notice(`和这台机器人原来保存的${hop.label}指纹一致：还是同一台机器。`);
  } else {
    html = notice(`和这台机器人原来保存的${hop.label}指纹不同：这是另一台机器，或者它重装过系统。请和设备管理员核对后再确认。`, true);
    if (secret && !draft[hop.prefix + secret] && auth === hop.saved.auth) html += sshEditSecretFields(hop, auth);
  }
  confirm.insertAdjacentHTML('beforebegin', html);
  if (device) form.querySelector('[type=submit]').textContent = '确认并保存';
}

const sshEditTyped = p => Object.fromEntries(SSH_EDIT_SECRET_FIELDS.filter(name => p[name]).map(name => [name, p[name]]));

// ---------------------------------------------------------------- saving

function sshEditDone(before, after) {
  const robot = find('robots', after.robot_id);
  if (!robot || before?.fingerprint === after.fingerprint) {
    $('#dialog').close();
    toast('SSH 连接已更新');
    return;
  }
  dialog('SSH 连接已更新', h(robot.name),
         `<div class="dialog-body">${notice('目标机的主机指纹和原来不同：现在连接的是另一台机器，或者它重装过系统。' +
           '这台机器人已有的调研档案、调查记录和硬件扫描都保留着，但它们描述的是原来那台机器。建议重新扫描硬件，再重新调研。', true)}` +
         `<div class="button-row">${button('重新扫描硬件', 'hardware-scan', 'small', robot.id)}` +
         `${button('打开调研', 'engine-discover', 'secondary small', robot.id)}` +
         `${button('实时状态', 'engine-status', 'secondary small', robot.id)}${button('关闭', 'close', 'secondary small')}</div></div>`);
}

async function sshEditSave(request) {
  const before = sshEditSaved(request.edit_robot_id);
  const after = await api('engineering/ssh/update', request);
  sshDraft = null;
  await refresh(true);
  return sshEditDone(before, after);
}

async function sshEditSubmit(p, f) {
  const saved = sshEditSaved(p.edit_robot_id);
  if (!saved) throw Error('找不到这台机器人的 SSH 连接，请关闭后重试');
  const missing = sshEditMissingSecret(p, saved);
  if (missing) throw Error(missing);
  if (p.rescan || sshEditAddressChanged(p, saved)) {
    const result = await sshEditBaseSubmit('engine-ssh', p, f);         // the fingerprint steps of a new connection
    sshEditDecorateStep();
    return result;
  }
  // No address changed: the hops stay pinned to the fingerprints confirmed before.
  return sshEditSave({...p, ...sshScanRequest(p), read_roots: sshServiceList(p.read_roots),
                      watch_services: sshServiceList(p.watch_services), ssh_command: undefined});
}

{
  const baseCard = engineRobotCard;
  sshEditBaseAction = engineAction;
  sshEditBaseSubmit = engineSubmit;
  engineRobotCard = r => {
    const html = baseCard(r);
    if (r.sim || !r.connection_id) return html;
    return html.replace(button('适配器配置', 'robot-config', 'secondary small', r.id),
                        button('配置连接', 'engine-ssh-edit', 'secondary small', r.id));
  };
  engineAction = async (type, id) => (type === 'engine-ssh-edit' && state.user && project()
    ? sshEditOpen(id) : sshEditBaseAction(type, id));
  engineSubmit = async (kind, p, f) => {
    if (kind === 'engine-ssh' && p.edit_robot_id) return sshEditSubmit(p, f);
    if (sshDraft?.edit_robot_id && (kind === 'engine-ssh-jump' || kind === 'engine-ssh-confirm')) {
      sshDraft = {...sshDraft, ...sshEditTyped(p)};                     // a login typed in this step
      if (kind === 'engine-ssh-confirm') return sshEditSave(sshDraft);
      const result = await sshEditBaseSubmit(kind, p, f);
      sshEditDecorateStep();
      return result;
    }
    return sshEditBaseSubmit(kind, p, f);
  };
}
for (const type of ['input', 'change']) {
  document.addEventListener(type, e => { if (e.target.closest?.(SSH_EDIT_FORM)) sshEditSync(); });
}
render();
