'use strict';
// Issue #60 — 机器人应用管理. An application is a workflow configured for a robot: it calls one or more
// skills, in order. So
//   * 「机器人应用」 (#skills) is a waterfall of every application of the account, each card drawn as its
//     workflow and marked with the brand and model it is for. Nothing else is on that page.
//   * an application opens as a page of its own (#app): the workflow step by step, the robots that can
//     use it, how far it is, the tasks that have it, its versions. Changing, checking and the delivery
//     checklist start there.
//   * the skills have a page of their own (#app-skills), behind the 「技能」 button of 「机器人应用」.
//   * an application is made for a brand and model, not for one robot: the form's robot field becomes a
//     choice of brand and model. The field keeps its name (robot_id) and sends one robot of the choice,
//     so brainos, web/app.js and web/robot_model.js work as before.
// The functions down to agSkillsPage are pure (state in, facts or markup out): tests/app_gallery_logic.cjs.
// Loaded after web/task_apps.js, whose facts about applications (taAccountApps, taRobots, taProgress,
// taTasksOf, taCardLine, taBanner) this file draws with.

const AG_OPEN = 'nx-app-open';                    // browser storage: the application whose page is open
const AG_STEPS_SHOWN = 6;                         // steps a card shows before "还有 N 步"
const AG_ROUTES = ['app', 'app-skills'];          // the pages under 机器人应用
const AG_NO_MODEL = '未填写品牌型号';
const AG_UNSET = '-';                             // RM_UNSET of web/robot_model.js
const AG_FORMS = ['application', 'revision-preview'];
const AG_TARGET_FIELD = /<label class="field">目标机器人<select name="robot_id" required>[\s\S]*?<\/select><\/label>/;
const AG_SIM_NOTICE = /<div class="notice "><span>↗<\/span><div><b>仿真<\/b> 目标选仿真机器人时[^<]*<\/div><\/div>/;
const AG_SIM_ONLY = '这个品牌型号现在只有仿真机器人：应用只能做仿真测试，接入真机后才能发布到设备。';

// ---------------------------------------------------------------- which applications, and what they are

const agIsSim = r => Boolean(r && (r.sim || (r.manifest || {}).simulated === true));
const agOldestFirst = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''));

// Whether an application is shown under a filter of web/robot_model.js: '' all, '-' none filled in, or a brand and model.
function agShown(app, filter) {
  if (!filter) return true;
  if (filter === AG_UNSET) return !taProduct(app);
  return taKind(app) === 'product:' + filter;
}

// The workflow of an application: its steps, each with the skill it calls.
function agSteps(st, app) {
  return (app.steps || []).map((step, i) => {
    const skill = (st.skills || []).find(x => x.id === step.skill_id) || null;
    return {n: i + 1, name: skill ? skill.name : step.entrypoint || step.skill_id, version: skill ? skill.version : '', skill, step};
  });
}

// Every application of the account once (its newest version), newest first, as the pages need it.
function agItems(st, filter = '') {
  return taAccountApps(st).filter(app => agShown(app, filter)).map(app => {
    const robots = taRobots(st, app), root = taRoot(app);
    return {app, root, versions: (st.applications || []).filter(a => taRoot(a) === root).length, steps: agSteps(st, app), robots,
            online: robots.filter(r => r.status === 'connected').length, progress: taProgress(st, app), tasks: taTasksOf(st, app)};
  });
}

// The version of an application a page shows: the one asked for, or the newest. null when it is gone.
function agFindApp(st, root, versionId) {
  return (st.applications || []).find(a => taRoot(a) === root && a.id === versionId) || taNewest(st, root);
}

// ---------------------------------------------------------------- small pieces of markup

const agModel = app => `<span class="ag-model${taProduct(app) ? '' : ' missing'}">${h(taProduct(app) ? taProductLabel(taProduct(app)) : AG_NO_MODEL)}</span>`;

function agVersionText(app, versions) {
  return `v${h(app.version)}${versions > 1 ? ` · 共 ${versions} 个版本` : ''}`;
}

function agProgress(progress) {
  const said = TA_STEPS.map((s, i) => `${s}${progress.done[i] ? '已通过' : '未完成'}`).join('，');
  return `<div class="ag-progress" role="img" aria-label="${h(said)}">` +
    TA_STEPS.map((s, i) => `<span class="${progress.done[i] ? 'done' : i === progress.next ? 'next' : ''}"><i aria-hidden="true"></i>${s}</span>`).join('') + '</div>';
}

function agWhere(st, app, robots, online) {
  if (!taProduct(app)) return `只用于：${h((st.robots || []).find(r => r.id === app.robot_id)?.name || '已经不在的机器人')}`;
  return `${robots.length} 台同品牌型号的机器人可用${robots.length ? ` · ${online} 台已连接` : ''}`;
}

// ---------------------------------------------------------------- 机器人应用: the waterfall

// One application of the waterfall. `forTask` is the task a person came from to make an application, if any.
function agCard(st, item, forTask) {
  const {app, steps} = item, shown = steps.slice(0, AG_STEPS_SHOWN), more = steps.length - shown.length;
  const flow = steps.length
    ? shown.map(s => `<li class="ag-step"><span class="ag-n" aria-hidden="true">${s.n}</span><span class="ag-step-name">${h(s.name)}</span></li>`).join('') +
      (more > 0 ? `<li class="ag-more">还有 ${more} 步</li>` : '')
    : '<li class="ag-more">还没有步骤</li>';
  return `<article class="card ag-card" role="listitem" data-app="${h(item.root)}">` +
    `<header class="ag-card-head">${agModel(app)}<span class="ag-version">${agVersionText(app, item.versions)}</span></header>` +
    `<h3><button type="button" class="ag-open" data-action="appg-open" data-id="${h(item.root)}">${h(app.name)}</button></h3>` +
    `<ol class="ag-flow" aria-label="工作流，共 ${steps.length} 步">${flow}</ol>` +
    `<p class="ag-where">${agWhere(st, app, item.robots, item.online)}</p>${agProgress(item.progress)}` +
    taCardLine(st, app, forTask) + '</article>';
}

// The filter of web/robot_model.js (same chips, same action), counting applications the way the waterfall
// shows them: each once, however many versions it has. `filter` is the chosen key.
function agFilterBar(st, filter) {
  const apps = taAccountApps(st), groups = new Map();
  const add = (key, label) => { if (!groups.has(key)) groups.set(key, {key, label, robots: 0}); return groups.get(key); };
  [...(st.robots || [])].sort(agOldestFirst).filter(r => taProduct(r)).forEach(r => { add(taRobotKind(r), taProductLabel(taProduct(r))).robots += 1; });
  // A brand and model that only applications or skills still name (its robots are gone) is listed too.
  [...apps, ...(st.skills || [])].filter(x => taProduct(x)).forEach(x => add(taKind(x), taProductLabel(taProduct(x))));
  const unset = [...apps, ...(st.skills || [])].some(x => !taProduct(x));
  if (!groups.size && !unset) return '';
  const count = key => apps.filter(a => agShown(a, key)).length;
  const chip = (key, text, sub) => `<button type="button" class="robot-model-chip ${filter === key ? 'active' : ''}" data-action="rm-filter" ` +
    `data-id="${h(key)}" aria-pressed="${filter === key}"><b>${h(text)}</b><small>${h(sub)}</small></button>`;
  return '<div class="robot-model-filter" role="group" aria-label="按品牌型号查看">' + chip('', '全部品牌型号', `${apps.length} 个应用`) +
    [...groups.values()].map(g => chip(g.key.slice('product:'.length), g.label, `${g.robots} 台机器人 · ${count(g.key.slice('product:'.length))} 个应用`)).join('') +
    (unset ? chip(AG_UNSET, AG_NO_MODEL, `${count(AG_UNSET)} 个应用`) : '') + '</div>';
}

// The buttons of the head of 机器人应用.
function agHead(st) {
  return button(`技能 <span class="ag-count">${(st.skills || []).length}</span>`, 'appg-skills', 'secondary') +
    button(icon('plus') + ' 创建应用', 'application-add');
}

// Everything under the head and the filter: the waterfall, or what to do first.
function agGallery(st, filter, forTask) {
  const items = agItems(st, filter);
  if (items.length) return `<div class="cards ag-waterfall" role="list" aria-label="全部应用">${items.map(i => agCard(st, i, forTask)).join('')}</div>`;
  const what = '应用是给机器人配置的一个工作流：按顺序调用一个或多个技能。';
  if ((st.applications || []).length) {
    return `<section class="panel ag-empty">${empty('这个品牌型号还没有应用', '应用属于一个品牌型号：为这种机器人创建的应用，只在这里出现。',
      button('创建应用', 'application-add', 'secondary small'))}</section>`;
  }
  if ((st.skills || []).length) {
    return `<section class="panel ag-empty">${empty('还没有应用', what + '选择品牌型号，把技能排成步骤。', button(icon('plus') + ' 创建应用', 'application-add', 'small'))}</section>`;
  }
  return `<section class="panel ag-empty">${empty('先把技能加进来，再编排成应用', what + '机器人上已经安装的技能（例如示例技能“巡逻”）可以直接加入。',
    button('使用机器人上已安装的技能', 'ux-adopt', 'small') + ' ' + button('查看技能', 'appg-skills', 'secondary small'))}</section>`;
}

// What a person who came from a task to make an application is told (taBanner of web/task_apps.js), in
// the words of this page: the application is made for a brand and model, and opens as a page of its own.
function agBanner(st, forTask) {
  return taBanner(st, forTask).replace('创建时选的目标机器人决定应用的品牌型号。', '创建时选的品牌型号就是应用的品牌型号。')
    .replace('创建后在应用卡片上点「绑定到这个任务」。', '创建后在应用的卡片或它的页面上点「绑定到这个任务」。');
}

// ---------------------------------------------------------------- an application is for a brand and model

// The robot that stands for its brand and model when an application is made: a connected real one first.
function agRepresentative(robots) {
  const rank = r => (agIsSim(r) ? 2 : 0) + (r.status === 'connected' ? 0 : 1);
  return [...robots].sort((a, b) => rank(a) - rank(b) || agOldestFirst(a, b))[0] || null;
}

// What an application can be made for: every brand and model of the account's robots once, then each
// robot that has none. `value` is the robot sent for the choice; `keepId` stays the one of its group.
function agTargets(robots, keepId = '') {
  const sorted = [...robots].sort(agOldestFirst), groups = new Map();
  sorted.filter(r => taProduct(r)).forEach(r => {
    const key = taRobotKind(r);
    if (!groups.has(key)) groups.set(key, {key, product: taProduct(r), robots: []});
    groups.get(key).robots.push(r);
  });
  const fact = (key, product, own, label) => ({key, product, robots: own, label,
    value: own.some(r => r.id === keepId) ? keepId : agRepresentative(own).id,
    simulatedOnly: own.every(agIsSim), online: own.filter(r => r.status === 'connected').length});
  return [...groups.values()].map(g => fact(g.key, g.product, g.robots, `${taProductLabel(g.product)} · ${g.robots.length} 台机器人`))
    .concat(sorted.filter(r => !taProduct(r)).map(r => fact(taRobotKind(r), null, [r], `${r.name}（${AG_NO_MODEL}）`)));
}

// The field of a form that says what an application is for. `chosen` is a robot id.
function agTargetField(robots, chosen = '') {
  const targets = agTargets(robots, chosen);
  const options = targets.length
    // data-ag-target: a choice of brand and model, not a robot (web/robot_model.js adds the brand and model to robot options).
    ? targets.map(t => `<option value="${h(t.value)}"${t.value === chosen ? ' selected' : ''} data-ag-target>${h(t.label)}</option>`).join('')
    : '<option value="">请先添加机器人</option>';
  return field('品牌型号', `<select name="robot_id" required aria-describedby="ag-target-note">${options}</select>`,
    '应用属于一个品牌型号：这个品牌型号的每一台机器人都能使用它。');
}

// The fields of a form that puts an application together, with the robot field turned into the brand and
// model. `prefer` is the robot to start from when the form itself has chosen none.
function agReplaceTarget(fields, robots, prefer = '') {
  if (typeof fields !== 'string' || !AG_TARGET_FIELD.test(fields)) return fields;
  return fields.replace(AG_SIM_NOTICE, '').replace(AG_TARGET_FIELD, found => {
    const had = (found.match(/<option value="([^"]*)" selected>/) || [])[1] || '';
    return agTargetField(robots, had || prefer) + '<p class="inline-note ag-target-note" id="ag-target-note" role="note" aria-live="polite"></p>';
  });
}

// ---------------------------------------------------------------- the page of one application: its workflow

function agParams(parameters) {
  const pairs = Object.entries(parameters || {});
  if (!pairs.length) return '<p class="ag-params none">没有参数</p>';
  return `<ul class="ag-params" aria-label="参数" data-keep-code>${pairs.map(([k, v]) =>
    `<li><b>${h(k)}</b><code>${h(typeof v === 'string' ? v : JSON.stringify(v))}</code></li>`).join('')}</ul>`;
}

function agNode(s) {
  const step = s.step, fact = (name, value) => (value ? `<div><dt>${name}</dt><dd>${value}</dd></div>` : '');
  const needs = (step.requires || []).map(c => `<span class="tag">${h(c)}</span>`).join(' ');
  return `<li class="ag-node ag-node-step"${s.skill ? '' : ' data-missing'}><span class="ag-n" aria-hidden="true">${s.n}</span><div class="ag-node-body">` +
    `<h3>${h(s.name)}${s.version ? ` <small>v${h(s.version)}</small>` : ''}</h3>` +
    (s.skill ? `<p class="ag-entry mono">${h(s.skill.entrypoint || '')}</p>` : '<p class="ag-entry gone">这个技能已经不在了：这一步检查时不会通过。</p>') +
    `<dl class="ag-node-facts">${fact('完成条件', h(step.success_condition || ''))}${fact('停止条件', h(step.stop_condition || ''))}` +
    `${fact('时间', `最长 ${h(step.timeout_s)} 秒`)}${fact('设备能力', needs)}</dl>${agParams(step.parameters)}</div></li>`;
}

// The page of one application, as the version `app`. `forTask` as in agCard.
function agAppPage(st, app, forTask) {
  const root = taRoot(app), newest = taNewest(st, root) || app, steps = agSteps(st, app);
  const versions = (st.applications || []).filter(a => taRoot(a) === root).sort((a, b) => agOldestFirst(b, a));
  const robots = taRobots(st, app), progress = taProgress(st, app);
  const next = progress.next < 0 ? '已发布' : `下一步：${TA_STEPS[progress.next]}`;
  // Connected robots first, then in the order they were added.
  const names = [...robots].sort((a, b) => (a.status === 'connected' ? 0 : 1) - (b.status === 'connected' ? 0 : 1) || agOldestFirst(a, b))
    .map(r => r.name).join('、');
  const stale = newest.id === app.id ? '' : `<div class="notice warn ag-stale" role="note"><span>↗</span><div>这不是最新版本。最新的是 v${h(newest.version)}。 ` +
    `${button('查看最新版本', 'appg-version', 'small', newest.id)}</div></div>`;
  return '<button type="button" class="text-link ag-back" data-action="appg-back">← 全部应用</button>' +
    `<div class="page-head ag-app-head"><div><div class="ag-app-meta">${agModel(app)}<span class="ag-version">${agVersionText(app, versions.length)}</span></div>` +
    `<h1>${h(app.name)}</h1></div><div class="button-row">${button('修改工作流', 'revision-edit', '', app.id)}` +
    `${button('检查', 'harness-run', 'secondary', app.id)}${button('交付清单', 'deliver-open', 'secondary', app.id)}</div></div>${stale}` +
    '<div class="ag-app-grid"><section class="ag-workflow" aria-label="工作流">' +
    `<div class="section-head"><h2>工作流 <span class="badge-count">${steps.length} 步</span></h2><span>按顺序执行 · 任何一步失败就停止并报告</span></div>` +
    '<ol class="ag-nodes"><li class="ag-node ag-node-start"><b>开始</b><small>机器人收到启动请求</small></li>' +
    steps.map(agNode).join('') +
    `<li class="ag-node ag-node-end"><b>结束</b><small>${steps.length ? '全部步骤完成' : '还没有步骤'}</small></li></ol></section>` +
    '<aside class="ag-side" aria-label="应用的情况">' +
    `<section class="ag-fact"><h2>机器人</h2><p>${agWhere(st, app, robots, robots.filter(r => r.status === 'connected').length)}</p>` +
    (taProduct(app) && names ? `<p class="ag-names" title="${h(names)}">${h(names)}</p>` : '') + '</section>' +
    `<section class="ag-fact"><h2>进度</h2>${agProgress(progress)}<p>${h(next)}${progress.releasedOn.length ? ` · 已发布到 ${h(progress.releasedOn.join('、'))}` : ''}</p></section>` +
    `<section class="ag-fact ag-fact-tasks"><h2>任务</h2>${taCardLine(st, app, forTask)}</section>` +
    `<section class="ag-fact"><h2>版本记录 <span class="badge-count">${versions.length}</span></h2><ul class="ag-versions">${versions.map(v =>
      `<li><span>v${h(v.version)}</span>${v.id === app.id ? '<small>当前查看</small>' : button('查看', 'appg-version', 'secondary small', v.id)}</li>`).join('')}</ul></section>` +
    '</aside></div>';
}

// ---------------------------------------------------------------- the page of the skills

function agSkillsPage(st) {
  const skills = st.skills || [], apps = taAccountApps(st);
  const used = id => apps.filter(a => (a.steps || []).some(s => s.skill_id === id)).length;
  const add = button('使用机器人上已安装的技能', 'ux-adopt', 'secondary') + button(icon('plus') + ' 登记技能', 'skill-add');
  const body = skills.length
    ? table(['技能名称', '品牌型号', '执行入口', '能力要求', '模型', '使用情况', ''], skills.map(s =>
      `<tr><td><span class="cell-main">${h(s.name)}</span><span class="cell-sub">v${h(s.version)}</span></td>` +
      `<td>${taProduct(s) ? agModel(s) : '<span class="cell-sub">第一次编排进应用时确定</span>'}</td><td class="mono">${h(s.entrypoint)}</td>` +
      `<td>${(s.requires || []).map(c => `<span class="tag">${h(c)}</span>`).join(' ')}</td><td>${(s.model_ids || []).length}</td>` +
      `<td>${used(s.id) ? `${used(s.id)} 个应用在用` : '<span class="cell-sub">还没有应用使用</span>'}</td>` +
      `<td>${button('详情', 'detail-skills', 'secondary small', s.id)}</td></tr>`))
    : empty('还没有技能', '机器人上已经安装的技能（例如示例技能“巡逻”）可以直接加入；也可以手动登记设备端已有的技能。',
      button('使用机器人上已安装的技能', 'ux-adopt', 'small') + ' ' + button('手动登记技能', 'skill-add', 'secondary small'));
  return '<button type="button" class="text-link ag-back" data-action="appg-back">← 全部应用</button>' +
    `<div class="page-head ag-skills-head"><div><div class="eyebrow">SKILLS</div><h1>技能</h1></div><div class="button-row">${add}</div></div>` +
    `<section class="panel ag-skills">${body}</section>`;
}

// ---------------------------------------------------------------- on the page

let agRoot = '';                 // the application whose page is open (its first version's id)
let agVersion = '';              // the version of it being looked at; '' for the newest

function agRecall() {
  if (!agRoot) {
    try { agRoot = sessionStorage.getItem(AG_OPEN) || ''; } catch { /* no storage: nothing is remembered */ }
  }
  return agRoot;
}

function agRemember(root) {
  agRoot = root || '';
  agVersion = '';
  try { if (agRoot) sessionStorage.setItem(AG_OPEN, agRoot); else sessionStorage.removeItem(AG_OPEN); } catch { /* not remembered */ }
}

function agGo(route) {
  $('#dialog').close();
  if (location.hash === '#' + route) render();
  else location.hash = route;
}

const agPending = () => (typeof taPending === 'function' ? taPending() : null);
const agFilter = () => (typeof rmFilter === 'string' ? rmFilter : '');

function agGalleryView() {
  if (typeof rmFilterBar === 'function') rmFilterBar();                        // settles the filter (a brand and model that is gone)
  const bar = agFilterBar(state, agFilter());
  return heading('ROBOT APPLICATIONS', '机器人应用', '应用是给机器人配置的工作流：按顺序调用一个或多个技能，属于一个品牌型号的机器人。', agHead(state)) +
    noProject() + agBanner(state, agPending()) + bar + agGallery(state, agFilter(), agPending());
}

function agAppView() {
  const app = agFindApp(state, agRecall(), agVersion);
  return app ? agBanner(state, agPending()) + agAppPage(state, app, agPending()) : agGalleryView();
}

// The robot a new application starts from: the brand and model being looked at, or the one of the task it is made for.
function agPreferred() {
  const pending = agPending(), filter = agFilter();
  const key = filter && filter !== AG_UNSET ? 'product:' + filter : pending ? taFacts(state, pending).kind : '';
  return agTargets(state.robots || []).find(t => t.key === key)?.value || '';
}

// Changing an application whose robot is gone: the form starts from the application's own brand and model.
function agRevisionTarget() {
  const source = typeof revisionDraft === 'object' && revisionDraft ? find('applications', revisionDraft.source_application_id) : null;
  return source ? agTargets(state.robots || []).find(t => t.key === taKind(source))?.value || '' : '';
}

// Under the brand and model of an open form: what choosing it means, when that needs saying.
function agTargetNote() {
  const formEl = $('#dialog form[data-form="application"], #dialog form[data-form="revision-preview"]');
  const note = formEl?.querySelector('.ag-target-note'), chosen = formEl?.elements.robot_id?.value;
  if (!note) return;
  const target = agTargets(state.robots || [], chosen).find(t => t.value === chosen);
  note.textContent = target && target.simulatedOnly ? AG_SIM_ONLY : '';
}

// The sidebar and the breadcrumb know the two pages as part of 机器人应用.
function agChrome() {
  if (!AG_ROUTES.includes(current)) return;
  const link = document.querySelector('#navigation a[href="#skills"]'), crumb = document.getElementById('breadcrumb');
  if (link) { link.classList.add('active'); link.setAttribute('aria-current', 'page'); }
  const app = current === 'app' ? agFindApp(state, agRecall(), agVersion) : null;
  if (crumb) crumb.textContent = '机器人应用 / ' + (current === 'app' ? app?.name || '应用' : '技能');
}

async function agAction(type, id) {
  if (type === 'appg-open') { agRemember(id); return agGo('app'); }
  if (type === 'appg-back') return agGo('skills');
  if (type === 'appg-skills') return agGo('app-skills');
  if (type === 'appg-version') { agVersion = id || ''; return render(); }
  return undefined;
}

function agInstall() {
  views.skills = agGalleryView;
  views.app = agAppView;
  views['app-skills'] = () => agSkillsPage(state);
  const baseAction = action, baseForm = form, baseRender = render;
  action = async (type, id, el) => (String(type).startsWith('appg-') ? agAction(type, id) : baseAction(type, id, el));
  form = (title, sub, fields, kind, ...rest) => {
    if (!AG_FORMS.includes(kind)) return baseForm(title, sub, fields, kind, ...rest);
    const making = kind === 'application';
    const out = baseForm(title, making ? '选择品牌型号，按顺序添加要调用的技能。保存应用不会执行机器人动作。' : sub,
      agReplaceTarget(fields, state.robots || [], making ? agPreferred() : agRevisionTarget()), kind, ...rest);
    agTargetNote();
    return out;
  };
  render = function () {
    // #app without its application (deleted, or nothing was opened) is the waterfall. Not before the state
    // has arrived (`last` of web/app.js): a page reloaded on #app stays where it is.
    if (current === 'app' && state.user && last && !agFindApp(state, agRecall(), agVersion)) {
      current = 'skills';
      history.replaceState(null, '', location.pathname + location.search + '#skills');
    }
    baseRender();
    agChrome();
  };
  document.addEventListener('change', e => {
    if (e.target.matches?.('form[data-form="application"] [name="robot_id"], form[data-form="revision-preview"] [name="robot_id"]')) agTargetNote();
  });
  if (typeof route === 'function') route();               // #app and #app-skills were not pages while the earlier scripts loaded
}

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', agInstall);
