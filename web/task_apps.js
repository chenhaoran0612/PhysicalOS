'use strict';
// Issue #50 — 任务工作台 binds robot applications; it does not make them.
//
// The owner's rule: a task binds an application, or sends the person to 「机器人应用」 to make one;
// nothing is developed inside a task. Decided with the owner (2026-11-26):
//   * an application belongs to the account (as a robot does): made and changed on 「机器人应用」;
//   * a task binds applications, many to many; unbinding removes the relation and nothing else;
//   * a task is for one brand and model: the first application bound to it says which.
// The rules are kept by brainos/task_apps.py; the page only explains beforehand what the server will say.
//
// What this file draws:
//   * #task, the page of one task: its brand and model, the applications it has bound and how far each
//     is (checked, tested on site, released), 「绑定应用」 and 「去创建应用」, then the skills section of
//     web/task_skills.js. The step-by-step view of web/journey.js is not shown any more.
//   * on 「机器人应用」: which tasks an application is bound to, and the way to bind it. A person who
//     came from a task to make an application is told so, and binds with one click.
//
// The functions down to taTasksDialog are pure (state in, facts or markup out): tests/task_apps_logic.cjs.
// Loaded after web/task_workbench.js and web/robot_model.js.

const TA_FOR = 'nx-task-app-for';                 // browser storage: the task a person left to make an application for
const TA_NAMES_SHOWN = 3;
const TA_STEPS = ['配置检查', '现场测试', '发布'];
const TA_ACCOUNT_PAGES = ['skills', 'harness', 'deployment'];
// What web/app.js lets a person do with no task open: put skills and applications of the account together and check them.
const TA_ACCOUNT_ACTIONS = ['skill-add', 'step-add', 'ux-adopt', 'ux-adopt-one', 'harness-run', 'detail-skills'];
const taNoTaskNeeded = type => TA_ACCOUNT_ACTIONS.includes(type);

// ---------------------------------------------------------------- which applications a task has

const taRoot = a => a.root_application_id || a.id;

// The newest version of the application whose first version is `root`.
function taNewest(st, root) {
  return (st.applications || []).filter(a => taRoot(a) === root)
    .reduce((best, a) => (!best || String(a.created_at || '') >= String(best.created_at || '') ? a : best), null);
}

// Every application of the account once, as its newest version, newest first.
function taAccountApps(st) {
  const roots = [...new Set((st.applications || []).map(taRoot))];
  return roots.map(root => taNewest(st, root)).sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
}

// The applications a task has bound, oldest binding first, each as its newest version.
function taBound(st, task) {
  return (st.task_applications || []).filter(b => b.project_id === task.id)
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')))
    .map(b => taNewest(st, b.application_id)).filter(Boolean);
}

const taProduct = x => (x && x.product && x.product.vendor && x.product.model ? {vendor: x.product.vendor, model: x.product.model} : null);
const taProductLabel = p => `${p.vendor} ${p.model}`;

// What two applications are compared by when a task takes one (kind_of in brainos/task_apps.py).
function taKind(app) {
  const p = taProduct(app);
  return p ? 'product:' + p.vendor.toLowerCase() + '\n' + p.model.toLowerCase() : 'robot:' + (app.robot_id || '');
}

function taLabel(st, app) {
  const p = taProduct(app);
  if (p) return taProductLabel(p);
  const robot = (st.robots || []).find(r => r.id === app.robot_id);
  return robot ? `${robot.name}（未填写品牌型号）` : '未填写品牌型号';
}

// The robots of the account an application can be used with (issue #48).
function taRobots(st, app) {
  const p = taProduct(app);
  return (st.robots || []).filter(r => (p ? taKind({product: r.product}) === taKind(app) : r.id === app.robot_id));
}

// How far an application is, by the records kept for the version in use.
function taProgress(st, app) {
  const mine = e => e.application_id === app.id && e.application_digest === app.digest;
  const passed = scope => (st.evaluations || []).some(e => mine(e) && e.scope === scope && e.passed);
  const released = (st.deployments || []).filter(d => mine(d) && d.status === 'deployed');
  const done = [passed('static'), passed('onsite'), released.length > 0];
  const robots = [...new Set(released.map(d => d.robot_id))].map(id => (st.robots || []).find(r => r.id === id)?.name).filter(Boolean);
  return {done, next: done.indexOf(false), releasedOn: robots};
}

// A task as the pages need it: what it has bound, and the brand and model it is for.
function taFacts(st, task) {
  const apps = taBound(st, task), first = apps[0] || null;
  const kind = first ? taKind(first) : null;
  const items = apps.map(app => ({app, fits: taKind(app) === kind, robots: taRobots(st, app), progress: taProgress(st, app),
                                  versions: (st.applications || []).filter(a => taRoot(a) === taRoot(app)).length}));
  const robots = first ? taRobots(st, first) : [];
  return {apps, items, kind, label: first ? taLabel(st, first) : '', product: first ? taProduct(first) : null, robots,
          online: robots.filter(r => r.status === 'connected').length,
          released: items.filter(i => i.progress.done[2]).length};
}

// Whether `app` can be bound to `task` now, and why not. `facts` = taFacts of the task.
function taCanBind(st, task, facts, app) {
  if (facts.apps.some(a => taRoot(a) === taRoot(app))) return {ok: false, bound: true, why: '已绑定'};
  if (facts.kind && facts.kind !== taKind(app)) {
    return {ok: false, why: `这个任务用的是「${facts.label}」的应用，不能绑定「${taLabel(st, app)}」的应用`};
  }
  return {ok: true, why: ''};
}

// The tasks an application is bound to.
function taTasksOf(st, app) {
  const ids = (st.task_applications || []).filter(b => b.application_id === taRoot(app)).map(b => b.project_id);
  return (st.projects || []).filter(p => ids.includes(p.id));
}

const taNames = items => items.slice(0, TA_NAMES_SHOWN).map(x => x.name).join('、') + (items.length > TA_NAMES_SHOWN ? ' 等' : '');

// ---------------------------------------------------------------- the page of a task

function taSteps(progress) {
  const text = progress.next < 0 ? '已发布' : `下一步：${TA_STEPS[progress.next]}`;
  return `<div class="ta-steps" role="img" aria-label="${h(TA_STEPS.map((s, i) => `${s}${progress.done[i] ? '已通过' : '未完成'}`).join('，'))}">` +
    TA_STEPS.map((s, i) => `<span class="${progress.done[i] ? 'done' : i === progress.next ? 'next' : ''}"><i aria-hidden="true"></i>${s}</span>`).join('') +
    `</div><p class="ta-next">${h(text)}${progress.releasedOn.length ? ` · 已发布到 ${h(progress.releasedOn.join('、'))}` : ''}</p>`;
}

function taItem(st, task, item) {
  const {app, robots, progress} = item;
  const online = robots.filter(r => r.status === 'connected').length;
  const where = robots.length ? `${robots.length} 台可用 · ${online} 台已连接 · ${taNames(robots)}` : '账号里没有这个品牌型号的机器人';
  const skills = (app.steps || []).map(s => (st.skills || []).find(x => x.id === s.skill_id)?.name || s.entrypoint || s.skill_id);
  return `<article class="ta-app${item.fits ? '' : ' other'}" role="listitem" data-app="${h(taRoot(app))}">` +
    `<header><h3 title="${h(app.name)}">${h(app.name)}</h3><span class="ta-version">v${h(app.version)}${item.versions > 1 ? ` · 共 ${item.versions} 个版本` : ''}</span>` +
    `<span class="ta-tag">${h(taLabel(st, app))}</span></header>` +
    (item.fits ? '' : `<p class="ta-warn" role="note">它的品牌型号和这个任务的不一样（升级前绑定的）。可以继续使用，也可以解除绑定。</p>`) +
    `<dl class="ta-facts"><div><dt>工作步骤</dt><dd title="${h(skills.join(' → '))}">${(app.steps || []).length} 步 · ${h(skills.join(' → '))}</dd></div>` +
    `<div class="${robots.length ? '' : 'none'}"><dt>机器人</dt><dd title="${h(where)}">${h(where)}</dd></div></dl>` +
    `${taSteps(progress)}` +
    `<footer>${button('交付清单 ' + icon('arrow'), 'deliver-open', 'small', app.id)}` +
    `<button type="button" class="ta-unbind" data-action="taskapp-unbind" data-id="${h(taRoot(app))}" aria-label="解除绑定 ${h(app.name)}">解除绑定</button></footer></article>`;
}

// The whole page of one task. `skills` is the section of web/task_skills.js.
function taTaskMarkup(st, task, skills) {
  const facts = taFacts(st, task), any = taAccountApps(st).length > 0;
  const head = '<div class="tw-back"><a href="#overview">← 全部任务</a></div>' +
    `<div class="page-head ta-head"><div><div class="eyebrow">TASK</div><h1>${h(task.name)}</h1><p class="subtitle">${h(task.objective)}</p></div>` +
    `<div class="button-row">${button(icon('plus') + ' 绑定应用', 'taskapp-pick')}${button('去创建应用', 'taskapp-create', 'secondary')}</div></div>`;
  const model = facts.apps.length
    ? `<section class="ta-model" aria-label="任务的品牌型号"><div><span class="ta-model-label">品牌型号</span><strong>${h(facts.label)}</strong></div>` +
      `<p>这个任务只绑定这个品牌型号的应用。账号里有 ${facts.robots.length} 台这样的机器人，${facts.online} 台已连接。` +
      '解除全部应用后，可以改绑另一个品牌型号。</p></section>'
    : '<section class="ta-model free" aria-label="任务的品牌型号"><div><span class="ta-model-label">品牌型号</span><strong>还没有确定</strong></div>' +
      '<p>应用属于一个品牌型号的机器人，不同型号之间不通用。绑定第一个应用后，这个任务就固定为它的品牌型号。</p></section>';
  const list = facts.apps.length
    ? `<div class="section-head ta-list-head"><h2>已绑定的应用 <span class="badge-count">${facts.apps.length}</span></h2><span>${facts.released} 个已发布</span></div>` +
      `<div class="ta-list" role="list">${facts.items.map(item => taItem(st, task, item)).join('')}</div>`
    : '<section class="ta-empty"><h2>这个任务还没有绑定应用</h2>' +
      `<p>任务工作台不开发应用。${any ? '从账号已有的应用里选一个绑定；没有合适的，就到「机器人应用」里创建。' : '账号里还没有应用：先到「机器人应用」里创建一个，再回来绑定。'}</p>` +
      `<div class="button-row">${any ? button('绑定应用', 'taskapp-pick') : ''}${button('去创建应用 ' + icon('arrow'), 'taskapp-create', any ? 'secondary' : '')}</div></section>`;
  return head + model + list + (skills || '');
}

// ---------------------------------------------------------------- binding: the two dialogs

// From a task: the applications of the account, by brand and model, each with the way to bind it or why not.
function taPickDialog(st, task) {
  const facts = taFacts(st, task), apps = taAccountApps(st);
  if (!apps.length) {
    return ['绑定应用', h(task.name), '<div class="dialog-body ta-pick"><p class="ta-pick-none">账号里还没有应用。应用在「机器人应用」里创建。</p>' +
      `<div class="button-row">${button('去创建应用', 'taskapp-create', 'small')}${button('关闭', 'close', 'secondary small')}</div></div>`];
  }
  const groups = new Map();
  apps.forEach(app => { const k = taKind(app); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(app); });
  const order = [...groups.keys()].sort((a, b) => (b === facts.kind) - (a === facts.kind));     // the task's own brand and model first
  const body = order.map(k => {
    const own = groups.get(k);
    return `<section class="ta-pick-group${facts.kind && k !== facts.kind ? ' other' : ''}"><h3>${h(taLabel(st, own[0]))}` +
      `<small>${taRobots(st, own[0]).length} 台机器人</small></h3>` + own.map(app => {
        const can = taCanBind(st, task, facts, app);
        return `<div class="ta-pick-row"><div><b>${h(app.name)}</b><small>v${h(app.version)} · ${(app.steps || []).length} 个步骤</small>` +
          (can.ok || can.bound ? '' : `<small class="ta-pick-why">${h(can.why)}</small>`) + '</div>' +
          (can.bound ? badge('已绑定', 'good') : button('绑定', 'taskapp-bind', 'small', taRoot(app), !can.ok)) + '</div>';
      }).join('') + '</section>';
  }).join('');
  const lead = facts.kind ? `这个任务用的是「${h(facts.label)}」的应用，只能再绑定同一品牌型号的。` : '绑定第一个应用后，这个任务就固定为它的品牌型号。';
  return ['绑定应用', h(task.name), `<div class="dialog-body ta-pick"><div class="form-error" role="alert"></div><p class="ta-pick-lead">${lead}</p>${body}` +
    `<div class="button-row ta-pick-foot">${button('去创建应用', 'taskapp-create', 'secondary small')}${button('关闭', 'close', 'secondary small')}</div></div>`];
}

// From 「机器人应用」: the tasks of the account, each with the way to bind this application to it or why not.
function taTasksDialog(st, app) {
  const tasks = st.projects || [];
  const body = tasks.length ? tasks.map(task => {
    const can = taCanBind(st, task, taFacts(st, task), app);
    return `<div class="ta-pick-row"><div><b>${h(task.name)}</b>` + (can.ok || can.bound ? '' : `<small class="ta-pick-why">${h(can.why)}</small>`) + '</div>' +
      (can.bound ? button('解除绑定', 'taskapp-unbind-from', 'secondary small', task.id + '|' + taRoot(app))
        : button('绑定', 'taskapp-bind-to', 'small', task.id + '|' + taRoot(app), !can.ok)) + '</div>';
  }).join('') : '<p class="ta-pick-none">还没有任务。到「任务工作台」里新建一个，再回来绑定。</p>';
  return ['绑定到任务', `${h(app.name)} · ${h(taLabel(st, app))}`,
          `<div class="dialog-body ta-pick"><div class="form-error" role="alert"></div>${body}` +
          `<div class="button-row ta-pick-foot">${button('关闭', 'close', 'secondary small')}</div></div>`];
}

// ---------------------------------------------------------------- on 「机器人应用」

// Under an application's card text: the tasks it is bound to. `forTask` is the task a person came from, if any.
function taCardLine(st, app, forTask) {
  const tasks = taTasksOf(st, app);
  const text = tasks.length ? `已绑定任务：${taNames(tasks)}` : '还没有绑定到任务';
  let control = button('绑定到任务', 'taskapp-tasks', 'secondary small', app.id);
  if (forTask) {
    const can = taCanBind(st, forTask, taFacts(st, forTask), app);
    control = can.bound ? badge('已绑定到这个任务', 'good')
      : `<span class="ta-card-bind" title="${h(can.why)}">${button('绑定到这个任务', 'taskapp-bind-to', 'small', forTask.id + '|' + taRoot(app), !can.ok)}</span>`;
  }
  return `<div class="ta-card-line${tasks.length ? '' : ' none'}"><span title="${h(tasks.map(t => t.name).join('、'))}">${h(text)}</span>${control}</div>`;
}

// What a person who came from a task to make an application is told at the top of 「机器人应用」.
function taBanner(st, forTask) {
  if (!forTask) return '';
  const facts = taFacts(st, forTask);
  const which = facts.kind ? `这个任务用的是「${h(facts.label)}」的应用，请为这个品牌型号的机器人创建。` : '创建时选的目标机器人决定应用的品牌型号。';
  return `<section class="ta-banner" role="status"><div><b>正在为任务「${h(forTask.name)}」准备应用</b>` +
    `<p>${which}创建后在应用卡片上点「绑定到这个任务」。</p></div>` +
    `<div class="button-row">${button('返回任务', 'taskapp-back', 'small')}${button('不绑定了', 'taskapp-forget', 'secondary small')}</div></section>`;
}

// ---------------------------------------------------------------- on the page

function taPending() {
  let id = '';
  try { id = sessionStorage.getItem(TA_FOR) || ''; } catch { /* no storage: nothing is remembered */ }
  return id ? (state.projects || []).find(p => p.id === id) || null : null;
}

function taRemember(id) {
  try { if (id) sessionStorage.setItem(TA_FOR, id); else sessionStorage.removeItem(TA_FOR); } catch { /* not remembered */ }
}

function taTaskView() {
  const task = project();
  if (!task) return views.overview();
  return taTaskMarkup(state, task, typeof taskSkillsSection === 'function' ? taskSkillsSection() : '');
}

// A refusal is shown inside the open dialog (a toast would sit under its backdrop).
function taRefused(error) {
  const slot = $('#dialog[open] .form-error');
  if (!slot) return toast(error.message);
  return typeof formError === 'function' ? formError(slot, error) : (slot.textContent = error.message);
}

async function taBind(taskId, appId) {
  try {
    await api('task-apps/bind', {project_id: taskId, application_id: appId});
  } catch (error) {
    if (error.status === 401) throw error;
    await refresh(true);
    return taRefused(error);
  }
  await refresh(true);
  return true;
}

async function taUnbind(taskId, appId) {
  await api('task-apps/unbind', {project_id: taskId, application_id: appId});
  await refresh(true);
  return true;
}

function taUnbindDialog(task, app) {
  return dialog('解除绑定', `${h(app.name)} · ${h(task.name)}`,
    '<div class="dialog-body ta-pick"><p class="ta-pick-lead">只解除这个任务和应用的关系。应用、它的测试报告和发布记录都保留在「机器人应用」里，已经发布到机器人上的版本也不受影响。</p></div>' +
    `<div class="dialog-footer">${button('取消', 'close', 'secondary')}${button('解除绑定', 'taskapp-unbind-confirm', 'danger', task.id + '|' + taRoot(app))}</div>`);
}

async function taAction(type, id) {
  const task = project();
  const pair = String(id || '').split('|');
  if (type === 'taskapp-pick') return task ? dialog(...taPickDialog(state, task)) : undefined;
  if (type === 'taskapp-create') {
    if (task) taRemember(task.id);
    $('#dialog').close();
    location.hash = 'skills';
    return undefined;
  }
  if (type === 'taskapp-bind') {
    if (!task || await taBind(task.id, id) !== true) return undefined;
    $('#dialog').close();
    return toast('应用已绑定到这个任务');
  }
  if (type === 'taskapp-unbind') {
    const app = taNewest(state, id);
    return task && app ? taUnbindDialog(task, app) : undefined;
  }
  if (type === 'taskapp-unbind-confirm') {
    await taUnbind(pair[0], pair[1]);
    $('#dialog').close();
    return toast('已解除绑定，应用仍在「机器人应用」里');
  }
  if (type === 'taskapp-tasks') {
    const app = find('applications', id);
    return app ? dialog(...taTasksDialog(state, app)) : toast('这个应用已经不在了');
  }
  if (type === 'taskapp-bind-to' || type === 'taskapp-unbind-from') {
    const done = type === 'taskapp-bind-to' ? await taBind(pair[0], pair[1]) : await taUnbind(pair[0], pair[1]);
    if (done !== true) return undefined;
    const pending = taPending(), app = taNewest(state, pair[1]);
    if (type === 'taskapp-bind-to' && pending && pending.id === pair[0]) return taAction('taskapp-back');
    if ($('#dialog').open && app) dialog(...taTasksDialog(state, app));          // drawn again with what is true now
    return toast(type === 'taskapp-bind-to' ? '应用已绑定到任务' : '已解除绑定');
  }
  if (type === 'taskapp-back') {
    const pending = taPending();
    taRemember('');
    if (!pending) return render();
    projectId = pending.id;
    sessionStorage.setItem('brain-project', pending.id);
    location.hash = 'task';
    return undefined;
  }
  if (type === 'taskapp-forget') { taRemember(''); return render(); }
  return undefined;
}

function taInstall() {
  views.task = taTaskView;
  const baseAction = action;
  action = async (type, id, el) => (String(type).startsWith('taskapp-') ? taAction(type, id) : baseAction(type, id, el));
  // 「机器人应用」 (web/robot_model.js draws it): every card says which tasks have it, and the page says whom an application is being made for.
  if (typeof rmApplicationCard === 'function') {
    const baseCard = rmApplicationCard;
    // A function, not a string: a task name with "$" in it must not be read as a replacement pattern.
    rmApplicationCard = a => baseCard(a).replace('<div class="card-bottom">', () => taCardLine(state, a, taPending()) + '<div class="card-bottom">');
  }
  const baseSkills = views.skills;
  if (typeof baseSkills === 'function') {
    views.skills = () => {
      const html = baseSkills(), banner = taBanner(state, taPending()), at = html.indexOf('</div></div>');
      // After the head of the page (the first block the heading helper closes), before everything else.
      return banner ? (at >= 0 ? html.slice(0, at + 12) + banner + html.slice(at + 12) : banner + html) : html;
    };
  }
  // Applications, their tests and releases are the account's: these pages need no task to be open.
  const baseNoProject = noProject;
  noProject = () => (TA_ACCOUNT_PAGES.includes(current) ? '' : baseNoProject());
  if (typeof route === 'function') route();
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', taInstall);
