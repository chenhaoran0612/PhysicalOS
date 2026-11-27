'use strict';
// Issue #68 — 机器人应用页面. The owner's words: 「机器人应用页面展示已经创建的应用，如果没有就是空。点击创建，
// 需要绑定给对应任务和机器人的品牌型号，然后编排技能。技能默认不用安装，已经是安装好的状态（对应品牌型号）。」
// Decided with the owner (2026-11-27):
//   * 「机器人应用」 (#skills) shows the applications and 「创建应用」, nothing else: no notice about a task
//     being prepared for, no filter by brand and model, no 「技能」 button, no "add skills first".
//   * An application is created for a task (required) and a brand and model. A task that already has a
//     brand and model (its robot, or an application it has bound) decides it. Saving binds the
//     application to the task (brainos/model_skills.py: one request).
//   * Every skill of the brand and model can be chosen: when the form has a brand and model, the server
//     makes its skills ready (those on its robots, and those that come with the app and fit it).
//     Nothing is installed or added by hand; what a robot lacks is installed before an on-site test.
// The functions down to acStatus are pure (state in, facts or markup out): tests/app_create_logic.cjs.
// Loaded right after web/app_gallery.js: it draws with it and web/task_apps.js, and replaces what they put on #skills.

const AC_PICK_TASK = '请选择任务';
const AC_FORM = 'form[data-form="application"]';
const AC_SUB = '选任务和品牌型号，再按顺序添加技能。保存应用不会让机器人动作。';

// ---------------------------------------------------------------- 机器人应用: the applications, or nothing

function acGallery(st) {
  const items = agItems(st, '');
  if (!items.length) return '<section class="panel ac-none"><p>还没有应用</p></section>';
  return `<div class="cards ag-waterfall" role="list" aria-label="全部应用">${items.map(i => agCard(st, i, null)).join('')}</div>`;
}

const acHead = () => button(icon('plus') + ' 创建应用', 'application-add');

// ---------------------------------------------------------------- creating: a task, then its brand and model

// What stands in the way of creating an application, as a dialog [title, sub, body]; null when nothing does.
function acBlocked(st) {
  if (!(st.projects || []).length) {
    return ['先建一个任务', '应用是为任务准备的：创建时要选它用在哪个任务上。',
            `<div class="dialog-body"><div class="button-row">${button('新建任务', 'project-add')}${button('关闭', 'close', 'secondary')}</div></div>`];
  }
  if (!(st.robots || []).length) {
    return ['先添加机器人', '应用属于一个品牌型号的机器人：账号里还没有机器人。',
            `<div class="dialog-body"><div class="button-row">${button('去我的机器人', 'go-robots')}${button('关闭', 'close', 'secondary')}</div></div>`];
  }
  return null;
}

// The task a new form starts with: the one a person came from, or the only one there is. '' = to be chosen.
function acStartTask(st, from = '') {
  const tasks = st.projects || [];
  if (tasks.some(t => t.id === from)) return from;
  return tasks.length === 1 ? tasks[0].id : '';
}

function acTaskField(st, chosen = '') {
  const options = (st.projects || []).map(t => `<option value="${h(t.id)}"${t.id === chosen ? ' selected' : ''}>${h(t.name)}</option>`).join('');
  return field('任务', `<select name="task_id" required aria-describedby="ac-task-note"><option value=""${chosen ? '' : ' selected'}>${AC_PICK_TASK}</option>${options}</select>`,
    '应用保存后绑定到这个任务，以后还可以绑定到别的任务。');
}

// What the chosen task says about the brand and model: {value, label, text}. `value` is the robot that
// stands for the brand and model the task fixes ('' when it fixes none, null when no robot has it).
function acLock(st, taskId) {
  const task = (st.projects || []).find(t => t.id === taskId);
  if (!task) return {value: '', label: '', text: ''};
  const facts = taFacts(st, task);
  if (!facts.kind) return {value: '', label: '', text: '这个任务还没有确定品牌型号：这里选的就是它的品牌型号。'};
  const target = agTargets(st.robots || []).find(t => t.key === facts.kind);
  if (!target) return {value: null, label: facts.label, text: `这个任务用的是「${facts.label}」，账号里现在没有这个品牌型号的机器人：先在“我的机器人”里添加一台。`};
  return {value: target.value, label: facts.label, text: `这个任务用的是「${facts.label}」：品牌型号跟着任务定。`};
}

// The fields of the form. `from` is the task a person came from, if any.
function acFields(st, from = '') {
  const task = acStartTask(st, from), lock = acLock(st, task);
  const name = (st.projects || []).find(t => t.id === task)?.name || '';
  return field('应用名称', `<input name="name" type="text" placeholder="例如：每日巡检流程" value="${h(name)}" required>`) +
    field('版本', '<input name="version" type="text" value="1.0" required>') +
    acTaskField(st, task) + '<p class="inline-note ac-task-note" id="ac-task-note" role="note" aria-live="polite"></p>' +
    agTargetField(st.robots || [], lock.value || '') + '<p class="inline-note ag-target-note" id="ag-target-note" role="note" aria-live="polite"></p>' +
    '<p class="inline-note ac-status" role="status" aria-live="polite"></p>' +
    `<div id="steps-container"></div>${button('添加工作步骤', 'step-add', 'secondary small')}`;
}

// The skills a step of an application for `robot` can call: those of its brand and model, and those of none yet.
function acSkills(st, robot, ready = []) {
  const kind = robot ? taRobotKind(robot) : '', known = new Set((st.skills || []).map(s => s.id));
  // `ready`: what the server just made ready. A state read before that may not have it yet.
  return [...(st.skills || []), ...ready.filter(s => !known.has(s.id))].filter(s => !taProduct(s) || taKind(s) === kind);
}

// What a skill is called in a step: its name, with the version when another skill offered has the same name.
function acSkillLabel(skill, offered) {
  return offered.some(s => s !== skill && s.name === skill.name) ? `${skill.name} · v${skill.version}` : skill.name;
}

// What is said above the steps once the skills of the brand and model were made ready (`result` of the server).
function acStatus(result, count) {
  if (result.product === null) return '这台机器人还没有填写品牌型号：在“我的机器人”里填写后，它的技能才能直接选用。';
  const unread = ((result.unread || []).length ? ` 有 ${result.unread.length} 台没有读到：${result.unread.join('、')}。` : '') +
    ((result.skipped || []).length ? ` 有 ${result.skipped.length} 个技能的说明不完整，没有列出：${result.skipped.join('、')}。` : '');
  if (count) return `这个品牌型号有 ${count} 个技能可以直接选用，不用先安装。` + unread;
  if (!result.connected) return '这个品牌型号的机器人现在都没有连接，读不到它的技能：先连接一台，再回来创建。';
  return '这个品牌型号还没有可用的技能。' + unread;
}

// ---------------------------------------------------------------- on the page

let acFrom = '';                 // the task a person left to create an application for
let acAsked = 0;                 // the newest request for a brand and model's skills; older answers are dropped
let acMade = [];                 // the skills the server made ready for the open form

const acForm = () => $('#dialog[open] ' + AC_FORM + ', #dialog[open] form[data-form="revision-preview"]');

function acView() {
  return heading('ROBOT APPLICATIONS', '机器人应用', '', acHead()) + acGallery(state);
}

// The task decides the brand and model when it has one: the other choices cannot be picked.
function acApplyTask() {
  const formEl = $('#dialog ' + AC_FORM), picker = formEl?.elements.robot_id, taskEl = formEl?.elements.task_id;
  if (!picker || !taskEl) return;
  const lock = acLock(state, taskEl.value), submit = formEl.querySelector('[type="submit"]');
  [...picker.options].forEach(option => {
    const off = lock.value !== '' && option.value !== lock.value;
    option.disabled = off;
    option.hidden = off;
  });
  if (lock.value) picker.value = lock.value;
  const note = formEl.querySelector('.ac-task-note');
  if (note) note.textContent = lock.text;
  if (submit) submit.disabled = lock.value === null;
}

// The steps of the open form offer the skills of the chosen brand and model.
function acOfferSkills() {
  const formEl = acForm(), robot = formEl ? find('robots', formEl.elements.robot_id?.value) : null;
  if (!formEl || !robot) return 0;
  const skills = acSkills(state, robot, acMade);
  formEl.querySelectorAll('.step-editor select[name="skill_id"]').forEach(selectEl => {
    const had = selectEl.value;
    selectEl.innerHTML = skills.length ? skills.map(s => `<option value="${h(s.id)}">${h(acSkillLabel(s, skills))}</option>`).join('') : '<option value="">这个品牌型号还没有技能</option>';
    if (skills.some(s => s.id === had)) selectEl.value = had;
    else selectEl.dispatchEvent(new Event('change', {bubbles: true}));          // the parameters of the step follow the skill
  });
  if (typeof rmLimitSteps === 'function') rmLimitSteps();
  return skills.length;
}

// Ask the server to make the skills of the chosen brand and model ready, then offer them.
async function acReady() {
  const formEl = acForm(), robotId = formEl?.elements.robot_id?.value, status = formEl?.querySelector('.ac-status');
  if (!formEl || !robotId) return;
  const asked = ++acAsked;
  if (status) status.textContent = '正在准备这个品牌型号的技能…';
  let result;
  try {
    result = await api('model-skills/ready', {project_id: projectId, robot_id: robotId});
  } catch (error) {
    if (error.status === 401) throw error;
    if (asked === acAsked && status && acForm() === formEl) status.textContent = '没有读到这个品牌型号的技能：' + error.message;
    return;
  }
  if (asked !== acAsked || acForm() !== formEl || formEl.elements.robot_id.value !== robotId) return;
  const ids = new Set(result.skills.map(s => s.id)), fresh = () => asked === acAsked && acForm() === formEl && formEl.elements.robot_id.value === robotId;
  acMade = result.skills;
  state = {...state, skills: [...state.skills.filter(s => !ids.has(s.id)), ...result.skills]};
  if (formEl.matches(AC_FORM) && !formEl.querySelector('.step-editor') && acSkills(state, find('robots', robotId), acMade).length) await action('step-add');
  if (!fresh()) return;
  const count = acOfferSkills();
  if (status) status.textContent = acStatus(result, count);
}

async function acOpen(from = '') {
  if (!state.user) return auth();
  const blocked = acBlocked(state);
  if (blocked) return dialog(...blocked);
  acMade = [];
  form('创建机器人应用', AC_SUB, acFields(state, from), 'application', '保存并查看交付清单');
  const sub = $('#dialog-content .dialog-head p');
  if (sub) sub.textContent = AC_SUB;
  acApplyTask();
  return acReady();
}

async function acAction(base, type, id, el) {
  if (type === 'application-add') return acOpen(acFrom);
  if (type === 'taskapp-create') {
    // From a task (web/task_apps.js): the form opens on 机器人应用 with that task chosen.
    acFrom = project()?.id || '';
    $('#dialog').close();
    if (location.hash !== '#skills') location.hash = 'skills';
    return acOpen(acFrom);
  }
  const out = await base(type, id, el);
  if (type === 'step-add' || type === 'revision-add') acOfferSkills();          // a new step offers the same skills, by the same names
  if (type === 'revision-edit' || type === 'revision-back') await acReady();   // a new version can call every skill of the brand and model too
  return out;
}

function acInstall() {
  views.skills = acView;
  if (typeof taRemember === 'function') taRemember('');          // the notice of web/task_apps.js is not shown any more: nothing is left waiting
  const baseAction = action, baseApi = api;
  action = (type, id, el) => acAction(baseAction, type, id, el);
  // Saving the form creates the application for its task: one request, all or nothing.
  api = (path, payload) => {
    // Only the open form: a closed dialog keeps its last content.
    const taskEl = path === 'applications' && payload ? $('#dialog[open] ' + AC_FORM)?.elements.task_id : null;
    return taskEl ? baseApi('model-skills/create', {...payload, task_id: taskEl.value}) : baseApi(path, payload);
  };
  document.addEventListener('change', e => {
    // What is known of the brand and model is offered at once; what the server makes ready follows.
    if (e.target.matches?.(AC_FORM + ' [name="task_id"]')) { acApplyTask(); if (typeof agTargetNote === 'function') agTargetNote(); acOfferSkills(); acReady(); }
    else if (e.target.matches?.(AC_FORM + ' [name="robot_id"], form[data-form="revision-preview"] [name="robot_id"]')) { acOfferSkills(); acReady(); }
  });
  document.addEventListener('close', e => { if (e.target.id === 'dialog') acFrom = ''; }, true);
  if (current === 'skills' && state.user) render();
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', acInstall);
