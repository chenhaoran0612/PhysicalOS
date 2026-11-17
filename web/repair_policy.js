'use strict';
// Issue #13 — hardware problems that software cannot fix, and repairs that run without asking.
//
// 1. "默认允许执行": the repair form and the fix-chat form carry the choice, ticked by default.
//    With it, the device commands the assistant proposes run as soon as they pass the product's
//    checks; the few the product cannot judge from their text still wait, and say why. Without it
//    every command waits for approval, and the approval itself offers the choice; the session
//    view can switch it either way while the repair runs.
// 2. A problem software cannot fix is only annotated: its checkbox is disabled until a person
//    marks it as handled on site (state.hardware_blocks, engineering/hardware/mark).
//
// Loaded right after hardware.js. The pieces here are called from web/hardware.js and
// web/investigation.js, where the issue lists, forms and approvals are drawn.
const RP_AUTO_METHODS = ['device_fix/requestApproval', 'device_exec/requestApproval'];
const RP_REASONS = {
  onsite: {tag: '等待现场处理', text: '这类问题软件修不了（接线、供电、总线参数等），助手不会处理。', done: '标注为已现场处理'},
  needs_user: {tag: '等待现场处理', text: '助手判断软件修不了，需要现场处理。', done: '标注为已现场处理'},
  not_fixed: {tag: '助手未能修复', text: '助手试过，软件没有修好。', done: '标注为已现场处理'},
  pending_reboot: {tag: '等待重启生效', text: '改动已完成，要重启或重新插拔后才生效。', done: '标注为已重启'},
};
const RP_ID_SEPARATOR = '|';

const rpBlocks = robotId => (state.hardware_blocks || []).filter(b => b.robot_id === robotId);
const rpBlock = (robotId, issueId) => rpBlocks(robotId).find(b => b.issue_id === issueId);
// Blocked: annotated only, not selectable until a person marks it as handled.
const rpLocked = (robotId, issueId) => rpBlock(robotId, issueId)?.state === 'blocked';
const rpSelectable = (robotId, issues) => issues.some(i => !rpLocked(robotId, i.id));
const rpSignature = robotId => rpBlocks(robotId).map(b => b.issue_id + ':' + b.state + ':' + b.reason).sort().join(',');

// The annotation under a problem: why it waits, and the button a person uses once it is dealt with.
function rpStrip(robotId, issue) {
  const block = rpBlock(robotId, issue.id);
  if (!block) return '';
  const reason = RP_REASONS[block.reason] || RP_REASONS.onsite;
  const id = robotId + RP_ID_SEPARATOR + issue.id;
  const tone = `sev-${h(issue.severity)}`;
  if (block.state === 'handled') {
    return `<div class="rp-strip is-handled ${tone}" data-rp-issue="${h(issue.id)}"><p class="rp-strip-text"><b>已标注处理完成</b><span>${date(block.handled_at)} 标注。现在可以勾选它交给助手；建议先重新扫描，确认问题是否还在。</span></p>${button('撤销标注', 'hardware-unmark', 'secondary small', id)}</div>`;
  }
  const note = block.note ? `<span class="rp-note">助手的说明：${h(block.note)}</span>` : '';
  return `<div class="rp-strip ${tone}" data-rp-issue="${h(issue.id)}"><p class="rp-strip-text"><b>${reason.tag}</b><span>${reason.text}处理好以后在这里标注，就可以重新勾选。</span>${note}</p>${button(reason.done, 'hardware-mark', 'small', id)}</div>`;
}

// The choice in the two start forms, ticked by default.
const rpStartChoice = () => `<label class="hardware-confirm rp-choice"><input type="checkbox" name="auto_approve" checked><span><b>默认允许执行</b>：助手提出的设备命令通过产品检查后直接执行，不再逐条问我，执行记录留在会话里。产品从文字上看不清的少数命令（命令替换、下载、运行脚本文件、递归删除、读取密钥位置等）仍会先问我。取消勾选后，每条命令执行前都会先给我看，由我批准。</span></label>`;

const rpAutoOn = s => s?.hardware?.auto_approve === true;
// Why a session that runs without asking stopped at this command (hardware.auto_hold, set by the product).
function rpHoldReason(s) {
  const hold = s.hardware?.auto_hold, pending = s.pending_approval;
  // Request ids start again with every run of a session: the command is part of what identifies the request.
  return hold && pending && String(hold.id) === String(pending.id) && hold.command === pending.request?.command ? hold.reason : '';
}

// The same choice on an approval. Ticked only where the session already runs without asking:
// there, unticking it while approving goes back to asking for each command.
function rpApprovalChoice(s) {
  if (s.mode !== 'repair' || !RP_AUTO_METHODS.includes(s.pending_approval?.method)) return '';
  const reason = rpHoldReason(s);
  return `${reason ? `<p class="rp-hold"><b>这条命令没有直接执行</b>${h(reason)}。</p>` : ''}<label class="hardware-confirm rp-choice rp-approval-choice"><input type="checkbox" name="auto_approve" ${rpAutoOn(s) ? 'checked' : ''}><span><b>之后默认允许执行</b>：批准后，这次会话后面的设备命令通过产品检查就直接执行，不再逐条问我。</span></label>`;
}

// One line in a repair session's view: how it runs now, and the switch.
function rpSessionLine(s) {
  if (s.mode !== 'repair') return '';
  const on = rpAutoOn(s);
  const text = on ? '默认允许执行：设备命令通过产品检查后直接执行，不逐条询问' : '逐条批准：每条设备命令执行前都会先问你';
  return `<p class="rp-auto ${on ? 'is-on' : ''}"><span class="rp-auto-state"><i aria-hidden="true"></i>${text}</span>${button(on ? '改为逐条批准' : '改为默认允许执行', 'hardware-auto', 'secondary small', s.id + RP_ID_SEPARATOR + (on ? 'off' : 'on'))}</p>`;
}

const rpSetAuto = (sessionId, enabled) => api('engineering/hardware/auto_approve', {project_id: projectId, session_id: sessionId, enabled});

// After the user approved one command: apply what the box on the approval said. Ticked switches
// the session to running without asking (or lets it go on after a stop); unticked on a session
// that was running without asking switches it back.
async function rpAfterApproval(sessionId, chosen, wasOn) {
  if (!chosen && !wasOn) return false;
  await rpSetAuto(sessionId, chosen);
  await refresh(true);
  return true;
}

function rpSplit(id) {
  const at = id.indexOf(RP_ID_SEPARATOR);
  return [id.slice(0, at), id.slice(at + 1)];
}

// Draw the open view again after a mark changed, keeping what the user had ticked or typed.
function rpRedraw(robotId) {
  const d = $('#dialog');
  if (!d.open) return;
  if (d.dataset.hardwareRobot === robotId && $('#dialog .hw-metrics')) {
    const ticked = new Set([...d.querySelectorAll('[name=hw-issue]:checked')].map(x => x.value)), scroll = d.scrollTop;
    hardwareOpen(robotId, true);
    d.querySelectorAll('[name=hw-issue]').forEach(x => { if (!x.disabled) x.checked = ticked.has(x.value); });
    d.scrollTop = scroll;
    return;
  }
  if (typeof inventoryView === 'object' && inventoryView?.robotId === robotId && $('#dialog .inv-fix')) {
    const ticked = new Set([...d.querySelectorAll('.inv-fix [name=issue_ids]:checked')].map(x => x.value));
    const message = $('#dialog .inv-fix [name=message]')?.value || '', scroll = d.scrollTop;
    inventoryOpen(robotId, inventoryView.reportId, true);
    d.querySelectorAll('.inv-fix [name=issue_ids]').forEach(x => { if (!x.disabled) x.checked = ticked.has(x.value); });
    const field = $('#dialog .inv-fix [name=message]');
    if (field) field.value = message;
    d.scrollTop = scroll;
  }
}

async function rpMark(type, id) {
  const [robotId, issueId] = rpSplit(id);
  const strip = () => [...document.querySelectorAll('#dialog .rp-strip')].find(x => x.dataset.rpIssue === issueId);
  let refused = null;
  try {
    await api('engineering/hardware/mark', {project_id: projectId, robot_id: robotId, issue_id: issueId, handled: type === 'hardware-mark'});
  } catch (err) {
    refused = err;
  }
  // Draw the current state first: a refusal usually means it changed elsewhere.
  await refresh(true);
  rpRedraw(robotId);
  if (!refused) return;
  // A toast would sit under the open dialog: say it where the button is, or let the page report it.
  const where = strip();
  if (!where) throw refused;
  where.insertAdjacentHTML('beforeend', `<p class="rp-error" role="alert">${h(refused.message)}</p>`);
}

async function rpSwitch(id) {
  const [sessionId, to] = rpSplit(id);
  await rpSetAuto(sessionId, to === 'on');
  await refresh(true);
  if ($('#dialog').dataset.invChat && typeof invChatSync === 'function') invChatSync(true);
  else engineRefresh();
}

async function rpAction(type, id) {
  if (type === 'hardware-auto') return rpSwitch(id);
  return rpMark(type, id);
}

// The approval form of a session view ("查看操作并决定"): read the choice before the form is
// replaced, apply it only after the approval itself went through.
{
  const baseSubmit = engineSubmit;
  engineSubmit = async (kind, p, f) => {
    if (kind !== 'engine-approval') return baseSubmit(kind, p, f);
    const box = f.elements.auto_approve, sessionId = f.dataset.id, wasOn = rpAutoOn(find('agent_sessions', sessionId));
    const decided = p.decision === 'accept' && !!box, chosen = decided && box.checked;
    const result = await baseSubmit(kind, p, f);
    if (decided && await rpAfterApproval(sessionId, chosen, wasOn)) engineRefresh();
    return result;
  };
}
