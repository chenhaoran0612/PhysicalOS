'use strict';
// Issue #42 — an installed skill can be run at once, on a real robot too.
//
// Running an installed skill from the console ("运行" → parameters → 授权并运行 → the run's state with
// "停止") was for simulated robots only. It is now offered on every connected robot. The parameter form
// is the one simulation already had (web/ux02.js builds it from the skill's own declaration); this file
// holds what differs between a real robot and a simulated one:
//
//   * A real robot asks for two confirmations: the authorization, and that somebody is on site with the
//     emergency stop within reach. The backend refuses a real robot without the second one
//     (brainos/skill_run.py), so an older page cannot start one by accident.
//   * A real robot without safety limits refuses every motion. The form says so before the run and
//     leads to the limits form (web/safety_limits.js).
//
// The functions are called from web/sim.js and web/robot_detail.js. Pure functions of their arguments:
// tests/skill_run_logic.cjs.

const SKILL_RUN_LIMITS_ACTION = 'engine-safety-limits-edit';       // web/safety_limits.js

// Only a robot the console started itself as a simulation. What a device says about itself does not
// count: a real body with a mislabelled adapter must not be able to waive the on-site confirmation.
function skillRunIsSim(robot) {
  return Boolean(robot?.sim);
}

// Installed skills can be run on a robot that passed its connection check.
function skillRunnable(robot) {
  return Boolean(robot) && robot.status === 'connected';
}

function skillRunButton(robot, skill) {
  if (!skillRunnable(robot)) return '';
  return button('运行', 'sim-run', 'small', robot.id + '|' + skill.id, skill.compatible === false);
}

// The run's state (progress, 停止). A real robot's card already has a 「实时状态」 of its own (read over SSH).
function skillRunStatusButton(robot) {
  if (!skillRunnable(robot)) return '';
  return button(skillRunIsSim(robot) ? '实时状态' : '技能运行状态', 'sim-live', 'secondary small', robot.id);
}

function skillRunInstalledNote(robot) {
  return `<section class="plan-review ok"><h3>技能已安装</h3><p>现在可以让机器人运行它。也可以先关闭，之后在` +
    `${skillRunIsSim(robot) ? '“实时状态”或' : ''}“能力与技能”里运行。</p></section>`;
}

// A real robot whose device says it will refuse motion until a person fills in its safety limits.
function skillRunLimitsNote(robot) {
  if (skillRunIsSim(robot) || robot?.device_status?.safety_limits?.motion_blocked !== true) return '';
  return `<section class="plan-review blocked" data-skill-run-limits><h3>这台设备还没有填写安全限值</h3>` +
    `<p>填写之前，设备会拒绝一切让机器人运动的指令：会运动的技能会在第一步失败，只读取状态的技能可以运行。</p>` +
    `<div class="button-row">${button('填写安全限值', SKILL_RUN_LIMITS_ACTION, 'small', robot.id)}</div></section>`;
}

const skillRunTick = (name, text) =>
  `<label class="hardware-confirm"><input type="checkbox" name="${name}" required><span>${text}</span></label>`;

// The foot of the run form: what kind of robot this is, and what a person confirms.
function skillRunConfirmHtml(robot) {
  if (skillRunIsSim(robot)) {
    return notice('<b>仿真</b> 这是仿真机器人：运动发生在虚拟环境中。授权规则与真机相同，运行中随时可以按“停止”。') +
      skillRunTick('hardware_authorized', '我确认机器人周围安全，授权它执行这个技能（技能可能让机器人运动）。');
  }
  return skillRunLimitsNote(robot) +
    notice('<b>真机</b> 这台机器人按真实的机器人处理：技能开始后它会真的运动。运行中随时可以按“停止”，停止后急停会锁存；' +
      '软件停止不能替代硬件急停。', true) +
    skillRunTick('onsite_confirmed', '现场有人看着这台机器人，周围没有人和障碍物，急停按钮够得着。') +
    skillRunTick('hardware_authorized', '我授权它执行这个技能（技能可能让机器人运动）。');
}

// What the form sends with the run request.
function skillRunConfirmations(form) {
  const ticked = name => form.elements[name]?.checked;
  const sent = {hardware_authorized: ticked('hardware_authorized') === true};
  if (form.elements.onsite_confirmed) sent.onsite_confirmed = ticked('onsite_confirmed') === true;
  return sent;
}
