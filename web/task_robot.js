'use strict';
// Issue #42, third part — the robot a task is done with.
//
// Robots belong to the account (brainos/robot_scope.py). When a task is created, a person chooses
// which robot (具身设备) it is done with; the task keeps the robot's id (brainos/task_robot.py).
// Several tasks can name the same robot. The choice can be left open and changed later on the page
// of the task. A task without a choice works with every robot of the account, as before.
//
//   * taskRobotField()        the field of the form 「你希望机器人完成什么工作？」 (web/app.js)
//   * taskRobotChoice()       what a task chose: {robot, gone, open}
//   * taskRobotLine()         the line on the page of a task, with 「选择机器人」 / 「更换」
//   * web/task_workbench.js (twOf) and web/task_skills.js (tsOf) show the chosen robot as the task's.
//
// Pure functions of their arguments are checked in tests/task_robot_logic.cjs, the pages in
// tests/task_robot_browser.cjs.

const TASK_ROBOT_FORM = 'engine-task-robot';        // a form of the engine* family, so the page's own handler shows its errors
const TASK_ROBOT_CHANGE = 'trobot-change';          // not "task-…": web/task_workbench.js takes those
const TASK_ROBOT_LATER = '之后再选';
const TASK_ROBOT_STATE = {connected: '已连接', unconnected: '未连接', error: '连接失败'};

// The robots a person can choose: every robot of the account, the connected ones first.
function taskRobotOptions(robots) {
  return [...(robots || [])].sort((a, b) => (b.status === 'connected') - (a.status === 'connected'));
}

const taskRobotLabel = robot =>
  `${robot.name}（${robot.sim ? '仿真 · ' : ''}${TASK_ROBOT_STATE[robot.status] || robot.status || '未连接'}）`;

// The field. `chosen` is the robot that is selected when the form opens ('' for 之后再选).
function taskRobotField(robots, chosen = '') {
  const options = taskRobotOptions(robots);
  const picked = options.some(r => r.id === chosen) ? chosen : '';
  const note = options.length
    ? '这个任务的页面会以这台机器人为准；几个任务可以选同一台，之后可以更换。'
    : '还没有机器人。可以先创建任务，再到“我的机器人”里添加，然后回到任务里选择。';
  return `<label class="field" data-task-robot-field>用哪台机器人来做<select name="robot_id">` +
    `<option value=""${picked ? '' : ' selected'}>${TASK_ROBOT_LATER}</option>` +
    options.map(r => `<option value="${h(r.id)}"${r.id === picked ? ' selected' : ''}>${h(taskRobotLabel(r))}</option>`).join('') +
    `</select><small>${note}</small></label>`;
}

// What a task chose. `open`: nothing was chosen; `gone`: the chosen robot is not there any more.
function taskRobotChoice(robots, task) {
  const id = task?.robot_id || '';
  const robot = id ? (robots || []).find(r => r.id === id) || null : null;
  return {robot, gone: Boolean(id) && !robot, open: !id};
}

// The robots of a task, from the robots of the account: the chosen one; all of them while nothing is
// chosen; none when the chosen one was deleted.
function taskRobotsOf(robots, task) {
  const choice = taskRobotChoice(robots, task);
  return choice.open ? [...(robots || [])] : choice.robot ? [choice.robot] : [];
}

// The line on the page of a task.
function taskRobotLine(robots, task) {
  const choice = taskRobotChoice(robots, task);
  const change = label => button(label, TASK_ROBOT_CHANGE, 'secondary small', task.id);
  const text = choice.robot
    ? `<b>${h(choice.robot.name)}</b> · ${h(choice.robot.sim ? '仿真 · ' : '')}${h(TASK_ROBOT_STATE[choice.robot.status] || '未连接')}`
    : choice.gone ? '<span class="task-robot-gone">原来选的机器人已经删除，请重新选择。</span>'
      : `还没有选择。现在这个任务可以使用账号里的每一台机器人${(robots || []).length ? '' : '（还没有添加机器人）'}。`;
  // One row: what it is, the robot, the way to change it.
  return `<section class="section-head task-robot" data-task-robot="${h(choice.robot?.id || '')}">` +
    `<span>这个任务用的机器人：${text}</span>${change(choice.robot ? '更换' : '选择机器人')}</section>`;
}

// ---------------------------------------------------------------- on the page

const taskRobotAccount = () => (typeof rows === 'function' ? rows('robots') : state.robots || []);

function taskRobotChangeForm(id) {
  const task = state.projects.find(p => p.id === id);
  if (!task) return toast('这个任务已经不在了');
  return form('这个任务用哪台机器人来做', h(task.name), taskRobotField(taskRobotAccount(), task.robot_id || ''),
              TASK_ROBOT_FORM, '保存', `data-id="${h(task.id)}"`);
}

async function taskRobotSubmit(p, f) {
  await api('projects/robot', {project_id: f.dataset.id, robot_id: p.robot_id || ''});
  await refresh(true);
  $('#dialog').close();
  toast(p.robot_id ? '已选择机器人' : '已改为之后再选');
}

function taskRobotInstall() {
  const baseAction = action, baseSubmit = engineSubmit, baseTask = views.task;
  action = async (type, id, el) => (type === TASK_ROBOT_CHANGE ? taskRobotChangeForm(id) : baseAction(type, id, el));
  engineSubmit = async (kind, p, f) => (kind === TASK_ROBOT_FORM ? taskRobotSubmit(p, f) : baseSubmit(kind, p, f));
  if (!baseTask) return;
  // The page of a task: the line comes under the head of the task.
  views.task = function () {
    const html = baseTask(), task = project(), back = '<div class="tw-back">';
    if (!task || !html.startsWith(back)) return html;
    const step = html.indexOf('<section class="journey-next">');      // above "现在做这一步", under the head of the task
    const at = step >= 0 ? step : html.indexOf('</div>') + '</div>'.length;
    return html.slice(0, at) + taskRobotLine(taskRobotAccount(), task) + html.slice(at);
  };
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', taskRobotInstall);
