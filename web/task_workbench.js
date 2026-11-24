'use strict';
// Issue #44 — 任务工作台 as a dashboard.
//
// The entry page used to be the step-by-step view of one task, with the list of tasks at its foot and
// no way to delete one. Now:
//
//   * #overview is the dashboard: how many tasks the account has and what they are called, then every
//     task as a card in a masonry wall. A task is created from the head of the page and deleted from
//     its card. 「使用指南」 is at the top right.
//   * #task is one task: the step-by-step view that used to be the entry page (web/journey.js draws
//     it), reached with 「进入任务」.
//
// A task is what the console has called a 任务项目 (state.projects). Tasks are kept in the workspace
// of the account that is signed in, so the list is that account's own.
// Deleting is brainos/task_remove.py; the server decides, the dialog only explains beforehand.
//
// The other scripts are not edited. The page is taken over once every script has loaded
// (DOMContentLoaded), so the place of this file in index.html does not matter.
// The functions down to twGuideBody are pure (state in, markup or facts out): tests/task_workbench_logic.cjs.

const TW_STAGES = ['明确任务', '连接机器人', '准备能力', '验证效果', '部署使用'];
const TW_SESSION_ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];    // ACTIVE in brainos/engineering.py
const TW_FLOW_RUNNING = ['running', 'stopping'];                                     // RUNNING in brainos/workflows.py
const TW_JOB_ACTIVE = ['dispatching', 'submitted', 'queued', 'running', 'cancelling', 'unknown'];   // JOB_ACTIVE in brainos/task_remove.py
const TW_NAMES_SHOWN = 3;            // robots named in a sentence, as brainos/task_remove.py does
const TW_INDEX_SHOWN = 10;           // task names on the dashboard; the wall below has all of them
const TW_OLD_LIST = '<div class="section-head"><h2>我的任务项目</h2>';               // the foot of web/journey.js's view

// ---------------------------------------------------------------- what a task holds

const twOf = (st, kind, task) => (st[kind] || []).filter(x => x.project_id === task.id);

// From the state the page already has. `stages` says which of TW_STAGES have a record.
function twFacts(st, task) {
  const of = kind => twOf(st, kind, task);
  const robots = of('robots'), applications = of('applications'), sessions = of('agent_sessions'), jobs = of('jobs');
  const flows = of('workflow_runs');
  const running = {
    session: sessions.find(s => TW_SESSION_ACTIVE.includes(s.status)) || null,
    flow: flows.find(run => TW_FLOW_RUNNING.includes(run.status)) || null,
    job: jobs.find(j => TW_JOB_ACTIVE.includes(j.status)) || null,
  };
  const stages = [
    !task.auto_created,
    robots.some(r => r.status === 'connected' || r.inventory_id),
    applications.length > 0,
    of('evaluations').some(e => e.scope === 'onsite' && e.passed),
    of('deployments').some(d => d.status === 'deployed'),
  ];
  return {
    robots, online: robots.filter(r => r.status === 'connected').length, applications, sessions, jobs, flows,
    models: of('models').length, datasets: of('datasets').length, skills: of('skills').length, services: of('services').length,
    reports: of('evaluations').length, releases: of('deployments').length,
    deployed: stages[4], running,
    working: sessions.filter(s => TW_SESSION_ACTIVE.includes(s.status)).length + flows.filter(run => TW_FLOW_RUNNING.includes(run.status)).length +
      jobs.filter(j => TW_JOB_ACTIVE.includes(j.status)).length,
    stages, done: stages.filter(Boolean).length, next: stages.indexOf(false),
  };
}

// The numbers of the dashboard, over all tasks.
function twTotals(list) {
  const sum = pick => list.reduce((n, [, f]) => n + pick(f), 0);
  return {tasks: list.length, robots: sum(f => f.robots.length), online: sum(f => f.online), applications: sum(f => f.applications.length),
          deployed: list.filter(([, f]) => f.deployed).length, working: list.filter(([, f]) => f.working).length};
}

const twNames = items => items.slice(0, TW_NAMES_SHOWN).map(x => x.name).join('、') + (items.length > TW_NAMES_SHOWN ? ' 等' : '');

// ---------------------------------------------------------------- deleting: what the dialog says

// Why the task cannot be deleted right now, with the way to what holds it; null when it can.
function twBlocked(task, facts) {
  if (facts.robots.length) {
    return {text: `这个任务里还有 ${facts.robots.length} 台机器人（${twNames(facts.robots)}）。请先到“我的机器人”里删除它们，再删除任务。`,
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
  if (facts.applications.length) {
    const kept = joined([count(facts.reports, '份', '测试报告'), count(facts.releases, '条', '发布记录')]);
    lines.push(`${facts.applications.length} 个机器人应用（${twNames(facts.applications)}）${kept ? '，以及 ' + kept : ''}`);
  } else if (facts.reports || facts.releases) {
    lines.push(joined([count(facts.reports, '份', '测试报告'), count(facts.releases, '条', '发布记录')]));
  }
  const assets = joined([count(facts.models, '个', '模型'), count(facts.datasets, '份', '数据'), count(facts.skills, '个', '技能'),
                         count(facts.services, '个', '自有服务的连接')]);
  if (assets) lines.push(assets);
  if (facts.sessions.length) lines.push(`${facts.sessions.length} 次助手会话的对话记录和它们在这台电脑上生成的文件`);
  if (facts.flows.length) lines.push(`${facts.flows.length} 个协作流程和它们的交付记录`);
  if (facts.jobs.length) lines.push(`${facts.jobs.length} 条训练、仿真或测试任务的记录`);
  const notes = ['只删除这台电脑上保存的内容，不会联系任何机器人或服务。'];
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

// The five steps of a task as a row of marks, and in words which have a record.
function twTrack(facts) {
  const text = facts.next < 0 ? '五步都已有记录' : `${facts.done} / ${TW_STAGES.length} 步已有记录 · 下一步：${TW_STAGES[facts.next]}`;
  return `<div class="tw-track" role="img" aria-label="${h(text)}">${facts.stages.map((done, i) =>
    `<i class="${done ? 'done' : i === facts.next ? 'next' : ''}" title="${h(TW_STAGES[i])}${done ? '：已有记录' : ''}"></i>`).join('')}</div>` +
    `<p class="tw-track-text" aria-hidden="true">${h(text)}</p>`;
}

// One task on the wall.
function twCard(task, facts, isCurrent) {
  const tags = [isCurrent ? badge('上次打开', 'good') : '', task.auto_created ? badge('试用任务') : '',
                facts.working ? badge(`${facts.working} 项工作进行中`, 'blue') : ''].join('');
  const holds = [];
  if (facts.robots.length) holds.push(['机器人', `${facts.robots.length} 台 · ${facts.online} 台已连接`, twNames(facts.robots)]);
  if (facts.applications.length) holds.push(['机器人应用', `${facts.applications.length} 个`, twNames(facts.applications)]);
  const assets = [[facts.models, '模型'], [facts.skills, '技能'], [facts.datasets, '数据']].filter(([n]) => n).map(([n, t]) => `${t} ${n}`).join(' · ');
  if (assets) holds.push(['已准备', assets, '']);
  return `<article class="tw-card${isCurrent ? ' current' : ''}" role="listitem" data-task="${h(task.id)}">` +
    `<header class="tw-card-top"><time>${h(date(task.created_at))} 创建</time><span class="tw-tags">${tags}</span></header>` +
    `<h3>${h(task.name)}</h3><p class="tw-objective">${h(task.objective)}</p>${twTrack(facts)}` +
    (holds.length ? `<dl class="tw-holds">${holds.map(([t, n, names]) =>
      `<div><dt>${t}</dt><dd>${h(n)}${names ? `<small>${h(names)}</small>` : ''}</dd></div>`).join('')}</dl>`
      : '<p class="tw-holds-none">还没有机器人和应用。进入任务，从连接机器人开始。</p>') +
    `<footer class="tw-card-foot">${button('进入任务 ' + icon('arrow'), 'task-open', 'small', task.id)}` +
    `<button type="button" class="tw-remove" data-action="task-remove" data-id="${h(task.id)}" aria-label="删除任务 ${h(task.name)}">删除</button></footer></article>`;
}

// The top of the page: how many tasks, what they are called, and the numbers over all of them.
function twBoard(list, user) {
  const totals = twTotals(list), shown = list.slice(0, TW_INDEX_SHOWN), more = list.length - shown.length;
  const names = list.length
    ? `<ol class="tw-names" aria-label="任务名称">${shown.map(([task, facts]) =>
      `<li><button type="button" data-action="task-open" data-id="${h(task.id)}" title="进入任务：${h(task.name)}">` +
      `<i class="tw-dot${facts.working ? ' working' : facts.deployed ? ' deployed' : ''}" aria-hidden="true"></i><span>${h(task.name)}</span>` +
      `<em>${facts.done}/${TW_STAGES.length}</em></button></li>`).join('')}</ol>` +
      (more > 0 ? `<p class="tw-names-more">另有 ${more} 个，见下方的任务列表。</p>` : '')
    : '<p class="tw-names-none">还没有任务。一个任务是一项要机器人完成的工作，例如“每天巡检仓库，发现异常后生成报告”。</p>';
  const figures = [['机器人', totals.robots, `${totals.online} 台已连接`], ['机器人应用', totals.applications, '技能组成的工作流程'],
                   ['已部署的任务', totals.deployed, '应用已发布到机器人']];
  return '<section class="tw-board" aria-label="任务概览">' +
    `<div class="tw-count"><span class="tw-count-label">我的任务</span><strong data-tw-count>${totals.tasks}</strong>` +
    `<small>${totals.working ? `${totals.working} 个任务有工作正在进行` : totals.tasks ? '现在没有正在进行的工作' : '从新建一个任务开始'}</small></div>` +
    `<div class="tw-index"><h2>任务名称</h2>${names}</div>` +
    `<dl class="tw-figures">${figures.map(([t, n, note]) => `<div><dt>${t}</dt><dd>${n}</dd><small>${note}</small></div>`).join('')}</dl>` +
    `<p class="tw-owner">任务属于当前登录的账号${user?.email ? ` ${h(user.email)}` : ''}，其他账号看不到。</p></section>`;
}

// The whole dashboard. `guide` is the first-run guide of web/ux05.js, when it has something to show.
function twDashboardMarkup(st, currentId, guide) {
  const list = (st.projects || []).map(task => [task, twFacts(st, task)]);
  const head = '<div class="page-head tw-head"><div><div class="eyebrow">TASK WORKBENCH</div><h1>任务工作台</h1>' +
    '<p class="subtitle">每个任务保存一项工作的机器人、应用、测试和部署记录。新建任务，或进入一个任务继续。</p></div>' +
    `<div class="button-row">${button(icon('plus') + ' 新建任务', 'journey-start')}${button('使用指南', 'task-guide', 'secondary')}</div></div>`;
  const wall = list.length
    ? `<div class="section-head tw-wall-head"><h2>全部任务 <span class="badge-count">${list.length}</span></h2><span>最近创建的在前</span></div>` +
      `<div class="tw-wall" role="list">${list.map(([task, facts]) => twCard(task, facts, task.id === currentId)).join('')}</div>`
    : '<section class="tw-empty"><h2>创建你的第一个任务</h2><p>写清机器人在哪里、做什么、怎样算完成。之后连接的机器人、准备的应用、测试和部署记录都保存在这个任务里。</p>' +
      `${button('创建第一个任务 ' + icon('arrow'), 'journey-start')}</section>`;
  // With tasks the wall comes right under the board and the guide after it; without, the guide is the way in.
  return head + twBoard(list, st.user) + (list.length ? wall + (guide || '') : (guide || '') + wall);
}

// One task: the step-by-step view, with the way back and without the old list of tasks at its foot.
function twTaskMarkup(html, count) {
  const at = html.indexOf(TW_OLD_LIST);
  // Not a <nav>: style.css lays every nav out as the sidebar.
  return '<div class="tw-back"><a href="#overview">← 全部任务</a>' +
    `<span>任务工作台里的 ${count} 个任务之一</span></div>` + (at >= 0 ? html.slice(0, at) : html);
}

function twGuideBody(hasTasks) {
  const lines = [
    ['任务是什么', '一个任务是一项要机器人完成的工作，例如“每天巡检仓库”。它保存这项工作用到的机器人、应用、测试和部署记录。'],
    ['新建任务', '点右上角的「新建任务」，写清任务名称和目标。建好后直接进入这个任务。'],
    ['查看全部任务', '任务工作台最上面是任务的数量和名称，下面是每个任务的卡片：目标、已经做到哪一步、有几台机器人和几个应用。'],
    ['进入任务', '点卡片上的「进入任务」或上面的任务名称。里面按五步进行：明确任务 → 连接机器人 → 准备能力 → 验证效果 → 部署使用，页面会告诉你现在该做哪一步。'],
    ['删除任务', '点卡片上的「删除」。删除前会列出要一起删除的内容；任务里还有机器人，或有工作正在进行时不能删除，需要先处理它们。删除后不能恢复。'],
    ['任务属于谁', '任务保存在当前登录账号的工作空间里。换一个账号登录，看到的是那个账号自己的任务。'],
  ];
  return `<div class="dialog-body tw-guide">${lines.map(([t, d]) => `<div class="contract-line"><b>${t}</b>${d}</div>`).join('')}` +
    `<div class="button-row">${button(hasTasks ? '新建任务' : '创建第一个任务', 'journey-start')}${button('知道了', 'close', 'secondary')}</div></div>`;
}

// ---------------------------------------------------------------- on the page

let twBaseTaskView = null;           // the step-by-step view of the current task, as the other scripts draw it

function twDashboard() {
  const guide = typeof uxFirstRunGuide === 'function' ? uxFirstRunGuide() : '';
  return twDashboardMarkup(state, projectId, guide);
}

function twTaskView() {
  if (!project()) return twDashboard();
  return twTaskMarkup(twBaseTaskView(), state.projects.length);
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
  toast('任务已创建，下一步连接机器人');
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
  if (type === 'task-guide') return dialog('任务工作台怎么用', '新建、查看、进入和删除任务', twGuideBody(state.projects.length > 0));
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
  if (crumb) crumb.textContent = '任务工作台 / ' + (project()?.name || '任务');
}

function twInstall() {
  twBaseTaskView = typeof journeyOverview === 'function' ? journeyOverview : views.overview;
  views.overview = twDashboard;
  views.task = twTaskView;
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
