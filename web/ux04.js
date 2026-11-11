'use strict';
// ux04 (round 06): the operator's way out, and parameter problems on the field they belong to.
//  * Q5-03: a reset that is refused (409 reset_in_progress, conformance_running, …) or hangs shows the
//    real reason and what to do -- wait, or restart the robot's runtime (simulated robots: one button;
//    the e-stop stays latched after the restart, then 解除急停 as usual). Never "无法连接接入服务".
//  * Q5-05: parameter refusals (ours before sending, the device's after) are shown on the form field.
//  * Q5-12: the authorization checkbox explains itself instead of the browser's "请勾选此框".
// Nothing here grants an execution right: authorization, e-stop and sim-only rules stay on the server.

// An operation result that did not succeed, as an Error the classifier understands.
function uxOpError(result, fallback) {
  const err = Error(result?.error || fallback || '操作没有完成');
  err.reason = typeof result?.code === 'string' ? result.code : '';
  err.body = result || {};
  return err;
}

// ---------------------------------------------------------------- Q5-05: field-level parameter errors
function uxMarkParamFields(form, err, classified) {
  form.querySelectorAll('.param-error').forEach(x => x.remove());
  form.querySelectorAll('.param-field.invalid').forEach(x => x.classList.remove('invalid'));
  const set = form.querySelector('.param-form');
  if (!set) return;
  const decls = JSON.parse(set.dataset.decls || '[]');
  let marks = Array.isArray(err?.fields) ? err.fields.filter(f => f.name) : [];
  if (!marks.length && classified?.kind === 'param' && classified.param) {
    const decl = decls.find(d => d.name === classified.param);
    marks = [{name: classified.param, text: uxParamRefusalText(classified, decl ? uxParamName(decl) : classified.param, decl ? {options: uxOptions(decl)} : null), server: true}];
  }
  let first = null;
  for (const m of marks) {
    const wrap = set.querySelector(`[data-param-field="${CSS.escape(m.name)}"]`);
    if (!wrap) continue;
    wrap.classList.add('invalid');
    wrap.insertAdjacentHTML('beforeend', `<small class="param-error" role="alert" data-param-error="${h(m.name)}"${m.server ? ' data-from="device"' : ''}>${h(m.text)}</small>`);
    first = first || wrap;
  }
  // With the advanced JSON open the form is hidden: say which key, next to the JSON box.
  const raw = form.querySelector('.raw-params');
  if (marks.length && raw?.open) {
    raw.insertAdjacentHTML('beforeend', `<small class="param-error" role="alert" data-param-error="json">${h(marks.map(m => m.text).join('；'))}</small>`);
  }
  first?.scrollIntoView?.({block: 'nearest'});
}

// Editing a marked field clears its mark.
document.addEventListener('input', e => {
  const wrap = e.target.closest?.('.param-field.invalid');
  if (!wrap) return;
  wrap.classList.remove('invalid');
  wrap.querySelector('.param-error')?.remove();
});

// ---------------------------------------------------------------- Q5-12: authorization checkbox
// The browser's own bubble ("请勾选此框") says nothing about why. Say it in the form instead.
document.addEventListener('invalid', e => {
  const box = e.target;
  if (!(box instanceof HTMLInputElement) || box.name !== 'hardware_authorized') return;
  e.preventDefault();
  const form = box.closest('form'), el = form?.querySelector('.form-error');
  const reset = form?.dataset.form === 'robot-reset';
  const text = reset
    ? '还没有勾选现场确认。解除急停后机器人可以再次运动，所以必须由在现场的人确认急停原因已排除、周围安全，再勾选下面的确认框。'
    : '还没有勾选现场安全确认。这个操作会让机器人运动，所以必须由你确认机器人周围没有人和障碍物，再勾选下面的确认框。';
  if (el) el.innerHTML = `<span class="plain-error" data-error-code="authorization_required"><span class="error-kind" data-kind="auth">${h(UX_KIND_NAMES.auth)}</span>${h(text)}</span>`;
  const label = box.closest('.hardware-confirm');
  label?.classList.add('needs-confirm');
  label?.scrollIntoView?.({block: 'nearest'});
}, true);
document.addEventListener('change', e => {
  if (e.target?.name === 'hardware_authorized' && e.target.checked) e.target.closest('.hardware-confirm')?.classList.remove('needs-confirm');
});

// ---------------------------------------------------------------- Q5-03: reset outcomes
const uxResetHeads = {rejected: '机器人拒绝了这次解除急停', unknown: '解除急停的结果还不确定', failed: '解除急停的请求没有发出'};
// ux05 (Q6-11 #1): a reset sent while the previous one hangs is not "rejected" and not merely "uncertain":
// it waits on the one still running. Heading, explanation and the note below all say the same thing.
const uxResetStuckHeads = {rejected: '机器人拒绝了这次解除急停', unknown: '解除急停还在等上一次复位完成'};

function uxHostOf(endpoint) {
  try { return new URL(endpoint).hostname; } catch { return ''; }
}

// ux05 (Q6-11 #2): a real robot whose reset hangs -- numbered steps, not a paragraph with a command in it.
function uxRealResetHowTo(robot) {
  const host = uxHostOf(robot?.endpoint || '');
  const steps = [
    ['先确认现场安全', '让所有人远离机器人。需要时直接按机器人上的硬件急停（红色按钮）或切断动力——它比软件更可靠。'],
    ['找到运行时所在的电脑', '运行时就是这台机器人“设备端地址”指向的那台电脑（通常是机器人本体里的工控机）' + (host ? `，地址 ${host}` : '') + '。'],
    ['重启那台电脑上的 NeuxMind 运行时', '用 SSH 或接上显示器和键盘登录，执行下面的命令；如果运行时是在终端里手动启动的，就在那个终端按 Ctrl+C，再用原来的命令启动。'],
    ['回到这里核对', '点“连接检查”，确认机器人重新连上、急停仍显示“已锁存”（锁存保存在运行时的状态目录里，重启不会解除）。'],
    ['再解除急停', '确认现场安全后，再点“解除急停”。'],
  ];
  return `<section class="reset-howto" data-reset-howto="real"><h4>真机：复位一直不返回时怎么办</h4><ol>` +
    steps.map(([t, d], i) => `<li><b>${h(t)}</b><span>${h(d)}</span>` +
      (i === 2 ? `<code class="mono howto-cmd">sudo systemctl restart neuxmind-runtime</code>` : '') + `</li>`).join('') +
    `</ol>${robot ? `<div class="button-row">${button('连接检查', 'robot-connect', 'secondary small', robot.id)}</div>` : ''}</section>`;
}

function uxResetOutcome(result, robotId, back) {
  const robot = find('robots', robotId);
  const stuck = result.code === 'reset_in_progress';
  // ux05-fix2 (Q7b-08): the desktop's own request outlived the limit it sent -- "this" reset, not "the previous".
  const late = stuck && result.expired === true && result.own_reset === true;
  const err = uxOpError(result, '设备没有确认解除急停');
  if (stuck && result.status !== 'rejected') err.reason = 'reset_pending';
  const sim = Boolean(robot?.sim);
  const retry = button('再试一次解除急停', 'robot-reset', stuck ? 'secondary' : '', robotId + (back === 'live' ? '::live' : ''));
  const restart = stuck && sim ? button('重启仿真机器人', 'sim-restart', '', robotId) : '';
  const live = sim ? button('查看实时状态', 'sim-live', 'secondary', robotId) : '';
  // Q6-03: a hung reset is still running on the robot and may unlatch by itself when it returns.
  // ux05: real robots too -- the stop path works for any connected robot and makes the hung reset fail.
  const cancel = stuck ? button('停止（取消那次复位）', 'simlive-stop', 'danger', robotId) : '';
  const real = stuck && !sim ? uxRealResetHowTo(robot) : '';
  return dialog('解除急停', h(robot?.name || ''),
    `<div class="dialog-body"><section class="plan-review blocked reset-outcome" data-reset-status="${h(result.status)}" data-reset-code="${h(result.code || '')}">` +
    `<h3>${h((late ? (result.reset_expire === true ? '这次解除急停已超时作废，急停保持锁存' : '这次解除急停超时了，复位还在执行') : null) ||
      (stuck ? uxResetStuckHeads[result.status] : null) || uxResetHeads[result.status] || '解除急停没有完成')}</h3>${errorBlock(err)}` +
    (stuck
      ? `<p class="inline-note" data-reset-pending data-reset-expire="${result.reset_expire === true ? 'supported' : 'unsupported'}"${late ? ' data-reset-late' : ''}>` +
        // The explanation above already says what happens to the late reset when the desktop knows the runtime's
        // support (`reset_expire`); then this line only sums up. Older records: the full sentence here.
        h(typeof result.reset_expire !== 'boolean'
          ? uxLateResetText({reset_expire: result.reset_expire, expire_s: result.expire_s, in_progress_s: result.reset_in_progress_s}, late)
          : late && result.reset_expire ? '急停保持锁存：这次复位即使之后完成，也不会解除急停。'
          : '在它返回之前请保持远离；“停止”始终可用，按下会取消正在执行的复位（急停保持锁存）。') + `</p></section>`
      : `<p class="inline-note">急停仍按锁存处理：在机器人报告“已解除”之前，它不会接受任何运动指令。“停止”始终可用。</p></section>`) +
    real + `<div class="button-row">${cancel}${restart}${retry}${live}</div>` +
    (stuck && sim ? `<p class="inline-note">“重启仿真机器人”会重新启动这台仿真机器人的运行时进程；急停锁存会保留，重启后请确认安全再解除急停。</p>` : '') +
    `</div>`);
}

async function uxSimRestart(robotId) {
  toast('正在重启仿真机器人…');
  await api('sims/restart', {robot_id: robotId, project_id: projectId});
  await refresh(true);
  toast('仿真机器人已重启；急停仍然锁存，确认安全后再解除急停');
  return simLiveOpen(robotId);
}

// ---------------------------------------------------------------- Q5-12: operation records in words
// "解除急停" used to show the robot's internal id in the 应用 / 版本 column, which looked like an error.
function uxOperationTarget(o) {
  const robot = o.robot_id ? named('robots', o.robot_id) : '';
  if (o.release_id) {
    const d = find('deployments', o.release_id);
    return h(d ? d.application_name + ' · 已发布版本' : '已发布版本') + (robot ? `<span class="cell-sub">${h(robot)}</span>` : '');
  }
  if (o.application_id) return h(named('applications', o.application_id)) + (robot ? `<span class="cell-sub">${h(robot)}</span>` : '');
  if (o.skill_id) return h('技能：' + o.skill_id + (o.version ? ' ' + o.version : '')) + (robot ? `<span class="cell-sub">${h(robot)}</span>` : '');
  return h(robot ? '机器人：' + robot : '—');
}
