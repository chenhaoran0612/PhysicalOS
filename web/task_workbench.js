'use strict';
// Issue #44 — 任务工作台 as a dashboard.
//
// The entry page used to be the step-by-step view of one task, with the list of tasks at its foot and
// no way to delete one. Now:
//
//   * #overview is the dashboard: how many tasks the account has and what they are called, then every
//     task as a card. A task is created from the head of the page and deleted from its card.
//   * #task is one task, reached with 「进入任务」 (web/task_apps.js draws it since issue #50).
//
// Issue #46 changed both pages:
//   * Every card is the same size: the same parts in the same order, each with a fixed number of lines
//     (a long name or goal is cut, a part without content says so), laid out in a grid of equal cells.
//   * The first-run guide of web/ux05.js is for the first visit only (twGuideOnce).
//   * Gone: 「使用指南」 in the head, the sentence about whose tasks these are, "one of N tasks" on the
//     page of a task, and that page's 「新建任务项目」 and 「使用指南」.
//   * The page of a task shows the skills its robots have and how they are configured (web/task_skills.js).
//
// Issue #52: the page is called 工作台, and the sentences that only described it are gone (under its name,
// under the count and the three numbers, beside 全部任务); a card does not show a robot's address as its name.
//
// Issue #50: a task binds robot applications and does not make them. The page of one task is drawn by
// web/task_apps.js (the step-by-step view of web/journey.js is not shown any more), and a card here says
// which brand and model the task is for, which applications it has bound and how many are released.
//
// A task is what the console has called a 任务项目 (state.projects). Tasks are kept in the workspace
// of the account that is signed in, so the list is that account's own.
// Deleting is brainos/task_remove.py; the server decides, the dialog only explains beforehand.
//
// The other scripts are not edited. The page is taken over once every script has loaded
// (DOMContentLoaded), so the place of this file in index.html does not matter.
// The functions down to twGuideOnce are pure (state in, markup or facts out): tests/task_workbench_logic.cjs.

const TW_MARKS = 5;                  // the bound applications a card marks one by one; the text has the number
const TW_SESSION_ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];    // ACTIVE in brainos/engineering.py
const TW_FLOW_RUNNING = ['running', 'stopping'];                                     // RUNNING in brainos/workflows.py
const TW_JOB_ACTIVE = ['dispatching', 'submitted', 'queued', 'running', 'cancelling', 'unknown'];   // JOB_ACTIVE in brainos/task_remove.py
const TW_NAMES_SHOWN = 3;            // robots named in a sentence, as brainos/task_remove.py does
const TW_INDEX_SHOWN = 10;           // task names on the dashboard; the wall below has all of them
const TW_CARD_ROWS = ['品牌型号', '机器人应用', '机器人'];                             // on every card, with or without content
const TW_IPV4 = /^(\d{1,3}\.){3}\d{1,3}(:\d+)?$/;                                    // a robot's name that is only an address
const TW_NAME = '工作台';                                                             // issue #52: what the page is called (was 任务工作台)
const TW_GUIDE_SEEN = 'nx-first-run-guide-seen';                                     // browser storage, one key per account

// ---------------------------------------------------------------- what a task holds

// Issue #42: robots belong to the account (st.robot_home), so every task has all of them.
// The robot a task was created with (task.robot_id) is its robot; without a choice, all of them are.
const twOf = (st, kind, task) => (st[kind] || []).filter(x => (x.project_id === task.id ||
  (kind === 'robots' && Boolean(st.robot_home) && x.project_id === st.robot_home)) &&
  (kind !== 'robots' || !task.robot_id || x.id === task.robot_id));

// From the state the page already has. Issue #50: `applications` are the ones the task has bound
// (web/task_apps.js), `bound` says what they are for and how far each is.
function twFacts(st, task) {
  const of = kind => twOf(st, kind, task);
  const bound = taFacts(st, task);
  const robots = of('robots'), applications = bound.apps, sessions = of('agent_sessions'), jobs = of('jobs');
  const flows = of('workflow_runs');
  const running = {
    session: sessions.find(s => TW_SESSION_ACTIVE.includes(s.status)) || null,
    flow: flows.find(run => TW_FLOW_RUNNING.includes(run.status)) || null,
    job: jobs.find(j => TW_JOB_ACTIVE.includes(j.status)) || null,
  };
  return {
    robots, online: robots.filter(r => r.status === 'connected').length, applications, bound, sessions, jobs, flows,
    models: of('models').length, datasets: of('datasets').length, skills: of('skills').length, services: of('services').length,
    released: bound.released, deployed: bound.released > 0, running,
    working: sessions.filter(s => TW_SESSION_ACTIVE.includes(s.status)).length + flows.filter(run => TW_FLOW_RUNNING.includes(run.status)).length +
      jobs.filter(j => TW_JOB_ACTIVE.includes(j.status)).length,
  };
}

// The numbers of the dashboard, over all tasks.
function twTotals(list) {
  const sum = pick => list.reduce((n, [, f]) => n + pick(f), 0);
  const robots = [...new Map(list.flatMap(([, f]) => f.robots).map(r => [r.id, r])).values()];     // issue #42: a robot that several tasks use counts once
  const bound = new Set(list.flatMap(([, f]) => f.applications).map(a => a.root_application_id || a.id));     // issue #50: an application several tasks bound counts once
  return {tasks: list.length, robots: robots.length, online: robots.filter(r => r.status === 'connected').length, applications: bound.size,
          deployed: list.filter(([, f]) => f.deployed).length, working: list.filter(([, f]) => f.working).length};
}

// Issue #52: a robot nobody named is called by its address, and a card does not show addresses.
function twIsAddress(robot) {
  const name = String(robot.name ?? '').trim();
  let host = '';
  try { host = robot.endpoint ? new URL(robot.endpoint).hostname : ''; } catch { /* not an address the page can read */ }
  return TW_IPV4.test(name) || (Boolean(host) && (name === host || name.startsWith(host + ':')));
}

const twNames = items => items.slice(0, TW_NAMES_SHOWN).map(x => x.name).join('、') + (items.length > TW_NAMES_SHOWN ? ' 等' : '');

// ---------------------------------------------------------------- deleting: what the dialog says

// Why the task cannot be deleted right now, with the way to what holds it; null when it can.
function twBlocked(task, facts) {
  const own = facts.robots.filter(r => r.project_id === task.id);      // issue #42: robots of the account stay when a task goes
  if (own.length) {
    return {text: `这个任务里还有 ${own.length} 台机器人（${twNames(own)}）。请先到“我的机器人”里删除它们，再删除任务。`,
            label: '去我的机器人', action: 'task-robots'};
  }
  if (facts.running.session) return {text: '这个任务有助手会话正在运行。请等它结束，或先停止它，再删除。', label: '进入任务', action: 'task-open'};
  if (facts.running.flow) return {text: '这个任务的协作流程正在运行。请等这一阶段结束，或先停止流程，再删除。', label: '进入任务', action: 'task-open'};
  if (facts.running.job) {
    return {text: `这个任务提交给执行服务的任务还在执行，或提交结果还没有确认（${facts.running.job.name || '未命名'}）。请等它结束、同步状态或先取消它，再删除。`,
            label: '进入任务', action: 'task-open'};
  }
  return null;
}

// What goes with the task (lines) and what is worth knowing (notes).
function twPlan(task, facts) {
  const lines = [], count = (n, unit, what) => (n ? `${n} ${unit}${what}` : '');
  const joined = parts => parts.filter(Boolean).join('、');
  // Issue #50: applications are the account's. Only the binding goes with the task.
  if (facts.applications.length) lines.push(`和 ${facts.applications.length} 个机器人应用的绑定（${twNames(facts.applications)}）`);
  const assets = joined([count(facts.models, '个', '模型'), count(facts.datasets, '份', '数据'), count(facts.skills, '个', '技能'),
                         count(facts.services, '个', '自有服务的连接')]);
  if (assets) lines.push(assets);
  if (facts.sessions.length) lines.push(`${facts.sessions.length} 次助手会话的对话记录和它们在这台电脑上生成的文件`);
  if (facts.flows.length) lines.push(`${facts.flows.length} 个协作流程和它们的交付记录`);
  if (facts.jobs.length) lines.push(`${facts.jobs.length} 条训练、仿真或测试任务的记录`);
  const notes = ['只删除这台电脑上保存的内容，不会联系任何机器人或服务。'];
  if (facts.applications.length) notes.unshift('绑定的应用本身不会删除：应用、它们的测试报告和发布记录都保留在「机器人应用」里。');
  return {blocked: twBlocked(task, facts), lines, notes};
}

// The dialog of a task as [title, subtitle, body]: the confirmation, or why there is nothing to confirm now.
function twRemoveDialog(task, plan) {
  const blocked = plan.blocked;
  if (blocked) {
    return ['删除任务', h(task.name),
            `<div class="dialog-body tw-remove-dialog" data-tw-blocked>${notice(h(blocked.text), true)}<div class="button-row">` +
            `${button(blocked.label, blocked.action, 'small', task.id)}${button('关闭', 'close', 'secondary small')}</div></div>`];
  }
  const what = plan.lines.length
    ? '<p class="tw-remove-lead">会从这台电脑上删除这个任务，以及它保存的：</p>' +
      `<ul class="tw-remove-list">${plan.lines.map(line => `<li>${h(line)}</li>`).join('')}</ul>`
    : '<p class="tw-remove-lead">这个任务里还没有保存其他内容，只删除任务本身。</p>';
  return ['删除任务', `${h(task.name)} · ${h(date(task.created_at))} 创建`,
          `<div class="dialog-body tw-remove-dialog"><div class="form-error" role="alert"></div>${what}` +
          `${plan.notes.map(note => `<p class="tw-remove-note">${h(note)}</p>`).join('')}` +
          '<p class="tw-remove-note tw-remove-final">删除后不能恢复。</p></div>' +
          `<div class="dialog-footer">${button('取消', 'close', 'secondary')}${button('删除任务', 'task-remove-confirm', 'danger', task.id)}</div>`];
}

// ---------------------------------------------------------------- the dashboard

// The applications a task has bound as a row of marks (released ones filled), and the numbers in words.
function twTrack(facts) {
  const items = facts.bound.items, n = items.length;
  const text = n ? `绑定了 ${n} 个应用 · ${facts.released} 个已发布` : '还没有绑定应用';
  const marks = Array.from({length: TW_MARKS}, (_, i) => (i < n ? (items[i].progress.done[2] ? 'done' : 'next') : ''));
  return `<div class="tw-track" role="img" aria-label="${h(text)}">${marks.map(cls => `<i class="${cls}"></i>`).join('')}</div>` +
    `<p class="tw-track-text" aria-hidden="true">${h(text)}</p>`;
}

// One task on the wall. Every card has the same parts, one line each except the goal (two lines),
// so that all cards are the same size whatever a task is called and holds (issue #46).
function twCard(task, facts, isCurrent) {
  const tags = [isCurrent ? badge('上次打开', 'good') : '', task.auto_created ? badge('试用任务') : '',
                facts.working ? badge(`${facts.working} 项工作进行中`, 'blue') : ''].join('');
  // Issue #50: the brand and model the task is for, and the robots of the account of that brand and model.
  const fitting = facts.bound.robots;
  const rows = [
    facts.bound.label,
    facts.applications.length ? `${facts.applications.length} 个 · ${twNames(facts.applications)}` : '',
    fitting.length ? [`${fitting.length} 台 · ${facts.bound.online} 台已连接`, twNames(fitting.filter(r => !twIsAddress(r)))].filter(Boolean).join(' · ') : '',
  ];
  const none = ['绑定应用后确定', '还没有绑定', facts.applications.length ? '账号里没有这个品牌型号的机器人' : '绑定应用后确定'];
  return `<article class="tw-card${isCurrent ? ' current' : ''}" role="listitem" data-task="${h(task.id)}">` +
    `<header class="tw-card-top"><time>${h(date(task.created_at))} 创建</time><span class="tw-tags">${tags}</span></header>` +
    `<h3 title="${h(task.name)}">${h(task.name)}</h3><p class="tw-objective">${h(task.objective)}</p>${twTrack(facts)}` +
    `<dl class="tw-holds">${TW_CARD_ROWS.map((label, i) => `<div class="tw-hold${rows[i] ? '' : ' none'}"><dt>${label}</dt>` +
      `<dd title="${h(rows[i] || none[i])}">${h(rows[i] || none[i])}</dd></div>`).join('')}</dl>` +
    `<footer class="tw-card-foot">${button('进入任务 ' + icon('arrow'), 'task-open', 'small', task.id)}` +
    `<button type="button" class="tw-remove" data-action="task-remove" data-id="${h(task.id)}" aria-label="删除任务 ${h(task.name)}">删除</button></footer></article>`;
}

// The top of the page: how many tasks, what they are called, and the numbers over all of them.
function twBoard(list) {
  const totals = twTotals(list), shown = list.slice(0, TW_INDEX_SHOWN), more = list.length - shown.length;
  const names = list.length
    ? `<ol class="tw-names" aria-label="任务名称">${shown.map(([task, facts]) =>
      `<li><button type="button" data-action="task-open" data-id="${h(task.id)}" title="进入任务：${h(task.name)}">` +
      `<i class="tw-dot${facts.working ? ' working' : facts.deployed ? ' deployed' : ''}" aria-hidden="true"></i><span>${h(task.name)}</span>` +
      `<em>${facts.applications.length} 个应用</em></button></li>`).join('')}</ol>` +
      (more > 0 ? `<p class="tw-names-more">另有 ${more} 个，见下方的任务列表。</p>` : '')
    : '<p class="tw-names-none">还没有任务。一个任务是一项要机器人完成的工作，例如“每天巡检仓库，发现异常后生成报告”。</p>';
  const figures = [['机器人', totals.robots], ['绑定的应用', totals.applications], ['已部署的任务', totals.deployed]];      // issue #52: the numbers, no note under them
  return '<section class="tw-board" aria-label="任务概览">' +
    `<div class="tw-count"><span class="tw-count-label">我的任务</span><strong data-tw-count>${totals.tasks}</strong>` +
    `${totals.working ? `<small>${totals.working} 个任务有工作正在进行</small>` : ''}</div>` +      // issue #52: said only while there is some
    `<div class="tw-index"><h2>任务名称</h2>${names}</div>` +
    `<dl class="tw-figures">${figures.map(([t, n]) => `<div><dt>${t}</dt><dd>${n}</dd></div>`).join('')}</dl></section>`;
}

// The whole dashboard. `guide` is the first-run guide of web/ux05.js, when it has something to show.
function twDashboardMarkup(st, currentId, guide) {
  const list = (st.projects || []).map(task => [task, twFacts(st, task)]);
  const head = `<div class="page-head tw-head"><div><div class="eyebrow">WORKBENCH</div><h1>${TW_NAME}</h1></div>` +      // issue #52: the name, no sentence under it
    `<div class="button-row">${button(icon('plus') + ' 新建任务', 'journey-start')}</div></div>`;
  const wall = list.length
    ? `<div class="section-head tw-wall-head"><h2>全部任务 <span class="badge-count">${list.length}</span></h2></div>` +
      `<div class="tw-wall" role="list">${list.map(([task, facts]) => twCard(task, facts, task.id === currentId)).join('')}</div>`
    : '<section class="tw-empty"><h2>创建你的第一个任务</h2><p>写清机器人在哪里、做什么、怎样算完成。之后给它绑定「机器人应用」里的应用。</p>' +
      `${button('创建第一个任务 ' + icon('arrow'), 'journey-start')}</section>`;
  // With tasks the wall comes right under the board and the guide after it; without, the guide is the way in.
  return head + twBoard(list) + (list.length ? wall + (guide || '') : (guide || '') + wall);
}

// The first-run guide is shown on the first visit of an account and not again (issue #46). A visit is
// one load of the page: the page is drawn again whenever its data changes, and the guide must not leave
// while somebody follows it. `shownFor` is the account it is being shown to in this visit.
// Without browser storage (a private window) it cannot be remembered and is simply shown.
function twGuideOnce(guide, who, shownFor, storage) {
  if (!guide) return {html: '', shownFor};
  if (!who || who === shownFor) return {html: guide, shownFor};
  const key = TW_GUIDE_SEEN + ':' + who;
  try {
    if (storage.getItem(key) === '1') return {html: '', shownFor};
    storage.setItem(key, '1');
  } catch { /* shown */ }
  return {html: guide, shownFor: who};
}

// ---------------------------------------------------------------- on the page

let twGuideShownFor = null;          // the account the first-run guide is shown to during this visit
const twStorage = {getItem: key => localStorage.getItem(key), setItem: (key, value) => localStorage.setItem(key, value)};

function twDashboard() {
  const once = twGuideOnce(typeof uxFirstRunGuide === 'function' ? uxFirstRunGuide() : '', state.user?.id || state.user?.email || '',
                           twGuideShownFor, twStorage);
  twGuideShownFor = once.shownFor;
  return twDashboardMarkup(state, projectId, once.html);
}

function twGo(route) {
  $('#dialog').close();
  if (location.hash === '#' + route) render();
  else location.hash = route;
}

function twSelect(id) {
  if (!state.projects.some(p => p.id === id)) { toast('这个任务已经不在了'); return false; }
  projectId = id;
  sessionStorage.setItem('brain-project', id);
  return true;
}

// Called by web/app.js once a new task is saved: the person goes on inside it.
function taskCreated(task) {
  twGo('task');
  toast('任务已创建，下一步绑定应用');
  return task;
}

function twRemoveOpen(id) {
  const task = state.projects.find(p => p.id === id);
  if (!task) return toast('这个任务已经不在了');
  return dialog(...twRemoveDialog(task, twPlan(task, twFacts(state, task))));
}

async function twRemoveConfirm(id) {
  const control = $('#dialog [data-action="task-remove-confirm"]');
  if (control) control.disabled = true;                   // one request per confirmation
  try {
    await api('projects/remove', {project_id: id});
  } catch (error) {
    if (error.status === 401) {                           // signed out meanwhile: the page's own handling says so
      if (control) control.disabled = false;
      throw error;
    }
    await refresh(true);
    if (!state.projects.some(p => p.id === id)) { $('#dialog').close(); return toast('这个任务已经不在了'); }
    // The reason is shown in the dialog (a toast would sit under its backdrop), drawn again from what the page knows now.
    twRemoveOpen(id);
    const slot = $('#dialog .form-error');
    if (slot) (typeof formError === 'function' ? formError(slot, error) : slot.textContent = error.message);
    else if (!$('#dialog [data-tw-blocked]')) toast(error.message);      // the dialog drawn again already says why
    return undefined;
  }
  $('#dialog').close();
  await refresh(true);
  if (current === 'task' && !project()) twGo('overview');
  return toast('任务已删除');
}

async function twAction(type, id) {
  if (type === 'task-open') return twSelect(id) ? twGo('task') : undefined;
  if (type === 'task-robots') return twSelect(id) ? twGo('robots') : undefined;
  if (type === 'task-remove') return twRemoveOpen(id);
  if (type === 'task-remove-confirm') return twRemoveConfirm(id);
  return undefined;
}

// The sidebar and the breadcrumb know the page of one task as part of 任务工作台.
function twChrome() {
  if (current !== 'task') return;
  const link = document.querySelector('#navigation a[href="#overview"]'), crumb = document.getElementById('breadcrumb');
  if (link) { link.classList.add('active'); link.setAttribute('aria-current', 'page'); }
  if (crumb) crumb.textContent = TW_NAME + ' / ' + (project()?.name || '任务');
}

function twInstall() {
  views.overview = twDashboard;
  views.task = twDashboard;            // until web/task_apps.js, which draws the page of one task, has loaded
  const baseAction = action, baseRender = render;
  action = async (type, id, el) => (String(type).startsWith('task-') ? twAction(type, id) : baseAction(type, id, el));
  render = function () {
    // #task without a task (none yet, or the last one was just deleted) is the dashboard. Not before the
    // state has arrived (`last` of web/app.js is the state as it was last drawn; the account is known
    // earlier than the tasks): a page reloaded on #task has no task for a moment and stays where it is.
    if (current === 'task' && state.user && last && !project()) {
      current = 'overview';
      history.replaceState(null, '', location.pathname + location.search + '#overview');
    }
    baseRender();
    twChrome();
  };
  if (typeof route === 'function') route();               // #task was not a page while the earlier scripts loaded
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', twInstall);
