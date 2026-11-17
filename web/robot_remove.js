'use strict';
// Issue #11: the card of a real robot gets "删除". The robot leaves the project together with its SSH
// connection, the saved logins, its investigations and repairs and what they found.
// Loaded last. It wraps engineRobotCard / engineAction instead of editing them, as ssh_edit.js does.
// Simulated robots keep their own delete (sim.js), which also stops their process.

const ROBOT_REMOVE_SESSION_NAMES = {discovery: '调查', repair: '修复'};      // as brainos/robot_remove.py names them
const ROBOT_REMOVE_FLOW_RUNNING = ['running', 'stopping'];                  // RUNNING in brainos/workflows.py
const ROBOT_REMOVE_FLOW_TEMPLATE = 'robot_adaptation';                      // TEMPLATE in brainos/adaptation.py
const ROBOT_REMOVE_NAMES_SHOWN = 3;
const ROBOT_REMOVE_CARD_END = '</div></div></article>';
let robotRemoveBaseAction = null;

const robotRemoveOf = (kind, robotId) =>
  (state[kind] || []).filter(x => x.project_id === projectId && x.robot_id === robotId);

// What the project holds about a robot, from the state the page already has.
function robotRemoveFacts(robot) {
  const sessions = robotRemoveOf('agent_sessions', robot.id);
  const flows = (state.workflow_runs || []).filter(run => run.project_id === projectId &&
    run.definition?.template === ROBOT_REMOVE_FLOW_TEMPLATE && run.definition?.robot_id === robot.id);
  return {
    connection: find('device_connections', robot.connection_id),
    sessions,
    running: sessions.find(s => engineActive(s.status)) || null,
    reports: robotRemoveOf('robot_inventories', robot.id).length,
    scans: robotRemoveOf('hardware_scans', robot.id).length,
    repairs: robotRemoveOf('hardware_repairs', robot.id).length,
    applications: robotRemoveOf('applications', robot.id),
    flowRunning: flows.find(run => ROBOT_REMOVE_FLOW_RUNNING.includes(run.status)) || null,
    flowsUnfinished: flows.filter(run => run.status !== 'completed').length,
  };
}

// Why the robot cannot be deleted right now, with the way to what is using it; null when it can.
function robotRemoveBlocked(facts) {
  if (facts.running) {
    const kind = ROBOT_REMOVE_SESSION_NAMES[facts.running.mode] || '工程';
    return {text: `这台机器人有${kind}会话正在运行。请等它结束，或先停止它，再删除。`,
            label: '查看这次会话', action: 'engine-session', id: facts.running.id};
  }
  if (facts.flowRunning) {
    return {text: '这台机器人的本体适配流程正在运行。请等这一阶段结束，或先停止流程，再删除。',
            label: '查看流程', action: 'engine-flow-run', id: facts.flowRunning.id};
  }
  return null;
}

// What the confirmation says: what goes with the robot (lines) and what is worth knowing (notes).
function robotRemovePlan(robot, facts) {
  const lines = [], c = facts.connection;
  if (c) lines.push(`SSH 连接 ${c.username}@${c.host}:${c.port}${sshRouteLabel(c)}，和为它保存的登录信息（密码或私钥）`);
  else if (robot.endpoint) lines.push(`服务地址 ${robot.endpoint}${robot.has_token ? '，和保存的接入令牌' : ''}`);
  const investigations = facts.sessions.filter(s => s.mode === 'discovery').length;
  const repairs = facts.sessions.filter(s => s.mode === 'repair').length;
  const counted = [investigations ? `${investigations} 次调研` : '', repairs ? `${repairs} 次修复` : ''].filter(Boolean);
  if (facts.sessions.length) {
    lines.push(`${counted.join('、') || `${facts.sessions.length} 次会话`}的对话记录、读取到的设备证据和生成的文件`);
  }
  if (facts.reports) lines.push(`${facts.reports} 份本体档案`);
  const scans = facts.scans ? '硬件扫描结果' : '', repairReports = facts.repairs ? `${facts.repairs} 份硬件修复报告` : '';
  if (scans || repairReports) lines.push(scans && repairReports ? `${scans}和 ${repairReports}` : scans || repairReports);
  const notes = ['不会登录机器人，也不会改动或停止机器人上的任何程序。'];
  if (facts.applications.length) {
    const names = facts.applications.slice(0, ROBOT_REMOVE_NAMES_SHOWN).map(a => a.name).join('、');
    const more = facts.applications.length > ROBOT_REMOVE_NAMES_SHOWN ? ' 等' : '';
    notes.push(`有 ${facts.applications.length} 个机器人应用以它为目标（${names}${more}）。` +
               '应用和它们的测试、发布记录会保留，但要改到别的机器人才能继续使用。');
  }
  if (facts.flowsUnfinished) {
    notes.push(`它有 ${facts.flowsUnfinished} 个没走完的本体适配流程：删除后不能再继续，已生成的文件保留。`);
  }
  return {blocked: robotRemoveBlocked(facts), lines, notes};
}

// The card of a real robot with "删除" as the last button of its row.
function robotRemoveCardButton(html, robot) {
  if (robot.sim || !html.endsWith(ROBOT_REMOVE_CARD_END)) return html;
  const control = '<button type="button" class="button danger-outline small robot-remove" data-action="engine-robot-remove" ' +
    `data-id="${h(robot.id)}" aria-label="删除机器人 ${h(robot.name)}">删除</button>`;
  return html.slice(0, -ROBOT_REMOVE_CARD_END.length) + control + ROBOT_REMOVE_CARD_END;
}

// The dialog of a robot as [title, subtitle, body]: the confirmation, or why there is nothing to confirm now.
function robotRemoveDialog(robot, plan) {
  const blocked = plan.blocked;
  if (blocked) {
    return ['删除机器人', h(robot.name),
            `<div class="dialog-body">${notice(h(blocked.text), true)}<div class="button-row">` +
            `${button(blocked.label, blocked.action, 'small', blocked.id)}${button('关闭', 'close', 'secondary small')}</div></div>`];
  }
  const what = plan.lines.length
    ? `<p class="robot-remove-lead">会从这台电脑上删除这台机器人，以及：</p>` +
      `<ul class="robot-remove-list">${plan.lines.map(line => `<li>${h(line)}</li>`).join('')}</ul>`
    : `<p class="robot-remove-lead">这台机器人还没有连接和调研记录，只删除它在项目里的登记。</p>`;
  return ['删除机器人', `${h(robot.name)} · ${h(date(robot.created_at))} 添加`,
          `<div class="dialog-body robot-remove"><div class="form-error" role="alert"></div>${what}` +
          `${plan.notes.map(note => `<p class="robot-remove-note">${h(note)}</p>`).join('')}` +
          `<p class="robot-remove-note robot-remove-final">删除后不能恢复；以后需要时可以重新添加并调研。</p></div>` +
          `<div class="dialog-footer">${button('取消', 'close', 'secondary')}` +
          `${button('删除', 'engine-robot-remove-confirm', 'danger', robot.id)}</div>`];
}

function robotRemoveOpen(robotId) {
  const robot = find('robots', robotId);
  if (!robot) return toast('这台机器人已经不在项目里了');
  return dialog(...robotRemoveDialog(robot, robotRemovePlan(robot, robotRemoveFacts(robot))));
}

async function robotRemoveConfirm(robotId) {
  const control = $('#dialog [data-action="engine-robot-remove-confirm"]');
  if (control) control.disabled = true;                   // one request per confirmation
  try {
    await api('robots/remove', {project_id: projectId, robot_id: robotId});
  } catch (error) {
    if (error.status === 401) {                           // signed out meanwhile: the page's own handling says so
      if (control) control.disabled = false;
      throw error;
    }
    await refresh(true);
    return robotRemoveRefused(robotId, error);
  }
  $('#dialog').close();
  await refresh(true);
  return toast('机器人已删除');
}

// The server said no. The reason is shown in the dialog (a toast would sit under the dialog's backdrop),
// drawn again from what the page knows now: a session that started meanwhile gets the way to it.
function robotRemoveRefused(robotId, error) {
  if (!find('robots', robotId)) {
    $('#dialog').close();
    return toast('这台机器人已经不在项目里了');
  }
  robotRemoveOpen(robotId);
  const slot = $('#dialog .form-error');
  if (slot) formError(slot, error);
}

{
  const baseCard = engineRobotCard;
  robotRemoveBaseAction = engineAction;
  engineRobotCard = r => robotRemoveCardButton(baseCard(r), r);
  engineAction = async (type, id) => {
    if (!state.user || !project()) return robotRemoveBaseAction(type, id);
    if (type === 'engine-robot-remove') return robotRemoveOpen(id);
    if (type === 'engine-robot-remove-confirm') return robotRemoveConfirm(id);
    return robotRemoveBaseAction(type, id);
  };
}
render();
