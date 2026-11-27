'use strict';
// Issue #56 — a robot is added, looked at and looked after with no task.
//
// Robots belong to the account (brainos/robot_scope.py), and a task chooses its robot when it is
// created (web/task_robot.js). So nothing a person does to a robot asks for a task first: the form
// 「你希望机器人完成什么工作？」 opens only where a task is what is being made or worked on.
//
//   * robotNoTaskNeeded(type)   whether the action `type` can go on with no task open. The handlers
//                               that used to open the task form ask it: engineAction, hardwareAction,
//                               journeyAction and the page's own action() (web/app.js).
//   * With no task open, the lists of the page (rows, hwList) are those of the robots' home, the
//     assistant's models included: robotShared() in web/app.js.
//
// Checked in tests/robot_no_task_logic.cjs and tests/robot_no_task_browser.cjs.

// What is done to a robot, by its action or the start of it.
const RNT_ROBOT_ACTIONS = ['detail-robots', 'robot-add', 'robot-config', 'robot-connect', 'journey-connect'];
const RNT_ROBOT_PREFIXES = ['engine-', 'hardware-', 'skillpkg-', 'simlive-'];
// What starts like a robot's action but is a task's work: an engineering job, its flows and the assistant's tools.
const RNT_TASK_ACTIONS = ['engine-work'];
const RNT_TASK_PREFIXES = ['engine-flow-', 'engine-ext-'];

function robotNoTaskNeeded(type) {
  if (typeof type !== 'string') return false;
  if (RNT_TASK_ACTIONS.includes(type) || RNT_TASK_PREFIXES.some(p => type.startsWith(p))) return false;
  return RNT_ROBOT_ACTIONS.includes(type) || RNT_ROBOT_PREFIXES.some(p => type.startsWith(p));
}

// The task the page goes on in after a robot was added: the one the server names when it is a task
// of the account, otherwise the one that was open (none, when the account has no task).
function robotNoTaskNext(answered, open, tasks) {
  return (tasks || []).some(t => t.id === answered) ? answered : open || '';
}
