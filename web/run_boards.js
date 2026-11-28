'use strict';
// Issue #70 — 仿真试运行 / 验证与测试 / 部署与运行 show what happened.
//
//   * 验证与测试 (#harness) and 部署与运行 (#deployment) are a board (how many, how they ended, how long)
//     over a list of records; a row opens the page of that record (#record, web/run_record_view.js).
//     What a person starts from these pages (检查, 现场测试, 发布, 启动, 停止) stays in the head and in
//     the rows; the body is what was recorded.
//   * 仿真试运行 (#simulation) keeps its page (issue #66 makes it "by task"); the simulated runs and
//     simulated tests are listed under it the same way.
//   * A record is an operation (运行技能, 现场测试, 启动工作 …), a report without an operation (静态检查),
//     or a release. What a run did — how it ended, how long, how far, its steps, events and samples — is
//     kept by brainos/run_records.py and asked for here (runs/records), not part of /api/state.
//   * Nothing is shown that was not recorded: a run from before the data was collected says so.
//
// The other scripts are not edited; the pages are taken over once every script has loaded.
// The functions down to rbFlow are pure (state in, facts out): tests/run_boards_logic.cjs.

const RB_PAGES = ['simulation', 'harness', 'deployment'];
const RB_OPEN = 'brain-run-record';              // session storage: {key, from}
const RB_TASK = 'brain-run-task';                // session storage: the task the lists are narrowed to
const RB_POLL_MS = 3000;
const RB_KIND = {onsite: '现场测试', start: '启动工作', stop: '请求停止', reset: '解除急停', skill_install: '安装技能包', skill_run: '运行技能',
                 static: '静态工程检查', report: '测试报告', release: '发布应用'};
const RB_RUN = {succeeded: ['pass', '已完成'], failed: ['fail', '失败'], stopped: ['stop', '已停止'], cancelled: ['stop', '已取消'],
                timeout: ['fail', '超时'], unknown: ['wait', '待人工核对']};
const RB_OP = {dispatching: ['run', '发送中'], running: ['run', '运行中'], accepted: ['wait', '已受理'], succeeded: ['pass', '已完成'],
               rejected: ['fail', '设备拒绝'], failed: ['fail', '失败'], unknown: ['wait', '待人工核对'], deployed: ['pass', '已部署']};
const RB_TONE = {pass: 'good', fail: 'bad', run: 'blue', stop: 'warn', wait: 'warn', none: ''};
const RB_STAGES = ['接入机器人', '安装技能', '仿真试运行', '真机测试', '部署运行'];

// ---------------------------------------------------------------- pure

const rbRoot = app => (app ? app.root_application_id || app.id : '');
const rbSeconds = v => { const t = typeof v === 'number' ? v * 1000 : Date.parse(v); return Number.isFinite(t) ? t / 1000 : null; };

// "1 分 12 秒"; '' when not known.
function rbDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 0.05) return '不到 0.1 秒';                // issue #75: a run of a few milliseconds is not "0 秒"
  if (seconds < 59.5) return (seconds < 10 ? seconds.toFixed(1).replace(/\.0$/, '') : Math.round(seconds)) + ' 秒';
  const whole = Math.round(seconds), m = Math.floor(whole / 60), s = whole % 60;
  return m < 60 ? `${m} 分${s ? ` ${s} 秒` : ''}` : `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分` : ''}`;
}

const rbIsSim = robot => Boolean(robot && (robot.sim || (robot.manifest || {}).simulated === true));

// The tasks a record belongs to: the ones that bound its application, or whose robot it ran on.
function rbTasks(st, applicationId, robotId) {
  const app = (st.applications || []).find(a => a.id === applicationId);
  const root = rbRoot(app);
  const bound = new Set((st.task_applications || []).filter(b => root && b.application_id === root).map(b => b.project_id));
  return (st.projects || []).filter(p => bound.has(p.id) || (!root && robotId && p.robot_id === robotId)).map(p => ({id: p.id, name: p.name}));
}

// How an operation stands: {key, text}. `record` is the run's record (or its summary), `report` the on-site report.
function rbOperationStatus(op, record, report) {
  if (record && record.state === 'following') return {key: 'run', text: '运行中'};
  if (op.kind === 'onsite' && report) return report.passed ? {key: 'pass', text: '通过'} : {key: 'fail', text: '存在缺项'};
  if (record && Object.hasOwn(RB_RUN, String(record.run_status))) { const [key, text] = RB_RUN[record.run_status]; return {key, text}; }
  // The following was cut off before the run ended: how it ended is not known here.
  if (record && record.state === 'lost' && ['accepted', 'running'].includes(op.status)) return {key: 'wait', text: '结果没有收集到'};
  const [key, text] = Object.hasOwn(RB_OP, String(op.status)) ? RB_OP[op.status] : ['none', op.status || '—'];
  return {key, text};
}

// Every record of the account as one list, newest first.
function rbEntries(st, summaries) {
  const records = new Map((summaries || []).map(r => [r.operation_id, r]));
  const robots = new Map((st.robots || []).map(r => [r.id, r]));
  const reports = new Map((st.evaluations || []).map(e => [e.id, e]));
  const releases = new Map((st.deployments || []).map(d => [d.id, d]));
  const named = (list, id) => ((list || []).find(x => x.id === id) || {}).name || '';
  const taken = new Set();
  const out = [];
  for (const op of st.operations || []) {
    const robot = robots.get(op.robot_id), record = records.get(op.id) || null;
    const report = op.report_id ? reports.get(op.report_id) || null : null;
    if (report) taken.add(report.id);
    const release = op.release_id ? releases.get(op.release_id) || null : null;
    const applicationId = op.application_id || (release ? release.application_id : '');
    const sim = op.simulated === true || rbIsSim(robot) || Boolean(report && (report.scope === 'simulation' || report.simulated));
    const started = rbSeconds(op.created_at), finished = rbSeconds(op.finished_at);
    out.push({
      key: 'op:' + op.id, id: op.id, source: 'operation', kind: op.kind, name: RB_KIND[op.kind] || op.kind || '操作',
      subject: op.skill_id ? op.skill_id + (op.version ? ' ' + op.version : '') : release ? release.application_name : named(st.applications, applicationId),
      skill_id: op.skill_id || '',                       // issue #75: a skill's page of 仿真试运行 lists that skill's runs
      robot_id: op.robot_id || '', robot: robot ? robot.name : '', sim, at: op.created_at, status: rbOperationStatus(op, record, report),
      duration_s: record && typeof record.duration_s === 'number' ? record.duration_s
        : started !== null && finished !== null && finished >= started ? Math.round((finished - started) * 10) / 10 : null,
      distance_m: record ? record.distance_m ?? null : null, samples: record ? record.samples || 0 : 0,
      steps: record ? record.steps || 0 : 0, steps_passed: record ? record.steps_passed || 0 : 0,
      application_id: applicationId, release_id: op.release_id || '', report_id: report ? report.id : '', error: op.error || (record && record.error) || '',
      tasks: rbTasks(st, applicationId, op.robot_id), collected: Boolean(record),
    });
  }
  for (const report of st.evaluations || []) {
    if (taken.has(report.id)) continue;
    const robot = robots.get(report.robot_id);
    const sim = report.scope === 'simulation' || report.simulated === true;
    const cases = Array.isArray(report.cases) ? report.cases : Array.isArray((report.details || {}).cases) ? report.details.cases : [];
    out.push({
      key: 'ev:' + report.id, id: report.id, source: 'report', kind: report.scope === 'static' ? 'static' : 'report',
      name: report.scope === 'static' ? RB_KIND.static : sim ? '仿真测试' : '现场测试', subject: named(st.applications, report.application_id),
      robot_id: report.robot_id || '', robot: robot ? robot.name : '', sim, at: report.created_at,
      status: report.passed ? {key: 'pass', text: '通过'} : {key: 'fail', text: '存在缺项'}, duration_s: null, distance_m: null, samples: 0,
      steps: cases.length, steps_passed: cases.filter(c => c && c.passed === true).length,
      application_id: report.application_id || '', release_id: '', report_id: report.id, error: '',
      tasks: rbTasks(st, report.application_id, report.robot_id), collected: true,
    });
  }
  return out.sort((a, b) => (rbSeconds(b.at) || 0) - (rbSeconds(a.at) || 0));
}

// Which records a page lists.
function rbOnPage(page, entry) {
  if (page === 'simulation') return entry.sim && ['skill_run', 'onsite', 'report'].includes(entry.kind);
  if (page === 'harness') return ['onsite', 'static', 'report'].includes(entry.kind) || (entry.kind === 'skill_run' && !entry.sim);
  if (page === 'deployment') return entry.source === 'operation';
  return false;
}

const rbOfTask = (entries, taskId) => (taskId ? entries.filter(e => e.tasks.some(t => t.id === taskId)) : entries);

// The numbers of a board.
function rbTotals(entries) {
  const count = key => entries.filter(e => e.status.key === key).length;
  const pass = count('pass'), fail = count('fail'), run = count('run'), stop = count('stop'), wait = count('wait');
  const sum = pick => Math.round(entries.reduce((n, e) => n + (typeof pick(e) === 'number' ? pick(e) : 0), 0) * 10) / 10;
  return {total: entries.length, pass, fail, run, stop, wait, other: entries.length - pass - fail - run - stop - wait,
          rate: pass + fail ? Math.round(pass * 100 / (pass + fail)) : null, seconds: sum(e => e.duration_s), metres: sum(e => e.distance_m),
          collected: entries.filter(e => e.samples > 0).length, last: entries.length ? entries[0].at : null,
          robots: new Set(entries.map(e => e.robot_id).filter(Boolean)).size};
}

// Issue #75: "2 次：1 次完成，1 次待核对" — every run is in one of the parts, so a run whose end was not read is not
// silently counted as not done (the user read 「2 次，0 次完成」 for two runs that had both succeeded).
function rbCounted(list, done) {
  const t = rbTotals(list);
  const parts = [[t.pass, '次' + done], [t.fail, '次失败'], [t.stop, '次被停止'], [t.run, '次进行中'], [t.wait + t.other, '次待核对']]
    .filter(([n], i) => n || i === 0).map(([n, text]) => `${n} ${text}`);
  return `${t.total} 次：${parts.join('，')}`;
}

// How far a robot and an application have come along the five stages: [{name, state: done|none, text}].
function rbFlow(st, entries, robotId, applicationId) {
  const robot = (st.robots || []).find(r => r.id === robotId);
  const root = rbRoot((st.applications || []).find(a => a.id === applicationId));
  const sameApp = e => root && rbRoot((st.applications || []).find(a => a.id === e.application_id)) === root;
  const product = r => (r && r.product ? `${r.product.vendor}\u0000${r.product.model}` : '');
  const sameModel = e => { const other = (st.robots || []).find(r => r.id === e.robot_id); return Boolean(product(robot)) && product(other) === product(robot); };
  const mine = entries.filter(e => e.robot_id === robotId || sameApp(e));
  const installs = entries.filter(e => e.robot_id === robotId && e.kind === 'skill_install' && e.status.key === 'pass');
  const sims = entries.filter(e => e.sim && ['skill_run', 'onsite', 'report'].includes(e.kind) && (e.robot_id === robotId || sameApp(e) || sameModel(e)));
  const real = mine.filter(e => !e.sim && ['skill_run', 'onsite', 'report'].includes(e.kind));
  const releases = (st.deployments || []).filter(d => d.status === 'deployed' && (d.robot_id === robotId || (root && sameApp(d))));
  const starts = mine.filter(e => e.kind === 'start' && (!root || sameApp(e)));
  const step = (index, done, text, at) => ({name: RB_STAGES[index], state: done ? 'done' : 'none', text, at: at || null});
  return [
    step(0, Boolean(robot && (robot.status === 'connected' || robot.checked_at)), robot ? (robot.status === 'connected' ? '已连接' : robot.checked_at ? '接入过，现在没有连接' : '还没有连接') : '机器人已删除', robot && robot.created_at),
    step(1, installs.length > 0, installs.length ? `装了 ${installs.length} 个技能包` : '没有安装记录', installs.length ? installs[0].at : null),
    step(2, sims.length > 0, sims.length ? rbCounted(sims, '完成') : '还没有仿真记录', sims.length ? sims[0].at : null),
    step(3, real.length > 0, real.length ? rbCounted(real, '通过') : '还没有真机记录', real.length ? real[0].at : null),
    step(4, releases.length > 0, releases.length ? `已发布${starts.length ? `，启动过 ${starts.length} 次` : ''}` : '还没有发布', releases.length ? releases[0].created_at : null),
  ];
}

// ---------------------------------------------------------------- what the pages keep

let rbRecords = [];              // summaries from runs/records
let rbRecordsText = '';
let rbCollecting = true;         // false: this server does not follow runs (said once on the board)
let rbTask = '';

function rbRecallTask() {
  if (!rbTask) { try { rbTask = sessionStorage.getItem(RB_TASK) || ''; } catch { /* nothing is remembered */ } }
  if (rbTask && !state.projects.some(p => p.id === rbTask)) rbTask = '';
  return rbTask;
}

const rbAll = () => rbEntries({...state, operations: rows('operations'), evaluations: rows('evaluations'), deployments: rows('deployments')}, rbRecords);
// The task a page's list is narrowed to: the chosen one, when it has a record on this page (otherwise the picker could not show it).
const rbChosen = entries => { const chosen = rbRecallTask(); return entries.some(e => e.tasks.some(t => t.id === chosen)) ? chosen : ''; };

// ---------------------------------------------------------------- markup

const rbBadge = st => badge(st.text, RB_TONE[st.key] || '');

function rbTaskPick(entries) {
  const used = new Map(entries.flatMap(e => e.tasks).map(t => [t.id, t.name]));
  if (!used.size) return '';
  const chosen = rbChosen(entries);
  return `<label class="rb-pick">任务<select data-rb-task>${[['', '全部任务'], ...used].map(([id, name]) =>
    `<option value="${h(id)}" ${id === chosen ? 'selected' : ''}>${h(name)}</option>`).join('')}</select></label>`;
}

// The board: the count, how the records ended, and three figures.
function rbBoard(label, unit, totals, figures) {
  const parts = [['pass', '完成 / 通过'], ['fail', '失败 / 缺项'], ['stop', '被停止'], ['run', '进行中'], ['wait', '待核对']].filter(([key]) => totals[key]);
  const bar = totals.total
    ? `<div class="rb-bar" role="img" aria-label="${h(parts.map(([key, text]) => `${text} ${totals[key]}`).join('，'))}">${parts.map(([key]) =>
        `<i class="${key}" style="flex:${totals[key]}"></i>`).join('')}${totals.other ? `<i style="flex:${totals.other}"></i>` : ''}</div>` +
      `<ul class="rb-legend">${parts.map(([key, text]) => `<li><i class="${key}"></i>${text}<b>${totals[key]}</b></li>`).join('')}</ul>`
    : '<p class="rb-none">还没有记录。</p>';
  return `<section class="rb-board" aria-label="${h(label)}"><div class="rb-count"><span>${h(label)}</span><strong>${totals.total}</strong><small>${h(unit)}</small></div>` +
    `<div class="rb-split"><h2>结果</h2>${bar}</div>` +
    `<dl class="rb-figures">${figures.map(([name, value, note]) => `<div><dt>${h(name)}</dt><dd>${h(value)}</dd>${note ? `<small>${h(note)}</small>` : ''}</div>`).join('')}</dl></section>`;
}

const rbFigures = totals => [
  ['通过率', totals.rate === null ? '—' : totals.rate + '%', totals.rate === null ? '还没有分出结果的记录' : `${totals.pass} 次完成，${totals.fail} 次失败`],
  ['累计运行', (totals.seconds && rbDuration(totals.seconds)) || '—', totals.metres ? `走过 ${totals.metres} 米` : ''],
  ['最近一次', totals.last ? date(totals.last) : '—', totals.robots ? `${totals.robots} 台机器人` : ''],
];

function rbRow(entry) {
  const data = [entry.duration_s !== null ? rbDuration(entry.duration_s) : '', entry.distance_m ? entry.distance_m + ' 米' : '',
                entry.steps ? `${entry.steps_passed}/${entry.steps} 步` : ''].filter(Boolean).join(' · ');
  const scope = entry.source === 'report' && typeof reportScopeName === 'function'
    ? reportScopeName({scope: entry.kind === 'static' ? 'static' : entry.sim ? 'simulation' : 'onsite'})
    : entry.kind === 'onsite' && entry.sim ? '仿真测试（不能作为现场证据）' : entry.name;
  return `<tr class="rb-row" data-action="rb-open" data-id="${h(entry.key)}"><td><span class="cell-main">${h(scope)}${entry.sim ? ' ' + simBadge() : ''}</span>` +
    `<span class="cell-sub">${h(entry.subject || '—')}</span></td>` +
    `<td>${h(entry.robot ? '机器人：' + entry.robot : '—')}<span class="cell-sub">${h(entry.tasks.map(t => t.name).join('、'))}</span></td>` +
    `<td>${rbBadge(entry.status)}</td><td class="rb-data">${h(data || (entry.collected ? '—' : '没有运行数据'))}</td><td>${date(entry.at)}</td>` +
    `<td>${button('查看', 'rb-open', 'secondary small', entry.key)}</td></tr>`;
}

const rbTable = entries => table(['记录', '机器人 / 任务', '结果', '运行数据', '时间', ''], entries.map(rbRow));

function rbListSection(title, entries, none, pick = '', note = '') {
  return `<div class="section-head rb-list-head"><h2>${h(title)} <span class="badge-count">${entries.length}</span></h2>${note ? `<span>${h(note)}</span>` : ''}${pick}</div>` +
    `<section class="panel rb-list">${entries.length ? rbTable(entries) : none}</section>`;
}

const rbNotCollecting = () => (rbCollecting ? '' : notice('这个服务没有打开「收集运行数据」：列表里只有请求和它的结果，没有运行过程的数据。'));

// ---------------------------------------------------------------- the pages

function rbHarness() {
  const all = rbAll().filter(e => rbOnPage('harness', e)), entries = rbOfTask(all, rbChosen(all)), totals = rbTotals(entries);
  const jobs = rows('jobs').filter(j => j.kind === 'evaluation');
  return heading('VERIFICATION & TESTING', '验证与测试', '', button('外部测试任务', 'job-evaluation', 'secondary') + button('发起现场测试', 'onsite-pick', 'secondary') + button('运行应用检查', 'harness-pick')) +
    noProject() + rbNotCollecting() + rbBoard('测试记录', '次检查与测试', totals, rbFigures(totals)) + (state.user ? simDeployNotice('harness') : '') +
    rbListSection('全部记录', entries, empty('还没有测试记录', '检查一个应用，或在机器人上做一次现场测试，结果和运行数据会列在这里。', button('选择应用检查', 'harness-pick', 'secondary small')), rbTaskPick(all),
      '现场测试需要明确确认；网络超时的操作标记为待核对，不自动重复') +
    (jobs.length ? `<div class="section-head"><h2>外部测试任务</h2></div><section class="panel">${jobTable(jobs)}</section>` : '');
}

function rbReleaseRow(d, entries) {
  const runs = entries.filter(e => e.release_id === d.id && e.kind === 'start'), last = runs[0];
  const latched = deploymentLatched(d);
  return `<tr class="rb-row" data-action="rb-open" data-id="dep:${h(d.id)}"><td><span class="cell-main">${h(d.application_name)}</span>` +
    `<span class="cell-sub">${h('机器人：' + named('robots', d.robot_id))}</span>${latched ? '<p class="inline-note card-note">急停已锁存：解除急停后才能启动工作。</p>' : ''}</td>` +
    `<td>${status(d.status)}</td><td>${date(d.created_at)}</td>` +
    `<td class="rb-data">${runs.length ? `启动过 ${runs.length} 次` : '还没有启动过'}${last ? `<span class="cell-sub">最近一次 ${h(last.status.text)} · ${date(last.at)}</span>` : ''}</td>` +
    `<td><div class="button-row">${button('启动工作', 'operate-start', 'small', d.id, d.status !== 'deployed' || latched)}${button('请求停止', 'operate-stop', 'secondary small', d.id, d.status !== 'deployed')}` +
    `${button('查看', 'rb-open', 'secondary small', 'dep:' + d.id)}</div></td></tr>`;
}

function rbDeployment() {
  const all = rbAll().filter(e => rbOnPage('deployment', e)), entries = rbOfTask(all, rbChosen(all));
  const releases = rows('deployments'), runs = entries.filter(e => e.kind === 'start'), totals = rbTotals(runs);
  const onlySim = rows('robots').some(r => r.sim) && !rows('robots').some(r => !isSimRobot(r));
  return heading('DEPLOYMENT & OPERATIONS', '部署与运行', '', button(icon('deployment') + ' 发布应用', 'deploy-pick')) +
    noProject() + rbNotCollecting() +
    rbBoard('已发布的应用', '个发布版本', {...totals, total: releases.length}, [
      ['启动工作', runs.length + ' 次', totals.rate === null ? '' : `${totals.pass} 次完成，${totals.fail} 次失败`],
      ['累计运行', (totals.seconds && rbDuration(totals.seconds)) || '—', totals.metres ? `走过 ${totals.metres} 米` : ''],
      ['最近一次启动', totals.last ? date(totals.last) : '—', '']]) + (state.user ? simDeployNotice('deployment') : '') +
    `<div class="section-head"><h2>发布版本 <span class="badge-count">${releases.length}</span></h2></div>` +
    (releases.length ? `<div class="panel rb-list">${table(['应用', '状态', '发布时间', '运行', ''], releases.map(d => rbReleaseRow(d, entries)))}</div>`
      : `<div class="panel">${empty('通过现场测试后，发布机器人应用', '发布会调用设备端适配器。未连接设备或缺少匹配证据时，平台会阻止发布。', button('发布应用', 'deploy-pick', 'secondary small'))}</div>`) +
    rbListSection('运行与操作记录', entries, empty('暂无设备操作', '发布、启动和停止请求会记录在这里；平台只记录实际发出的请求。',
      onlySim ? button('在仿真机器人上运行技能', 'sim-run-pick', 'secondary small', rows('robots').find(r => r.sim).id) : button('查看应用交付清单', 'deploy-pick', 'secondary small')), rbTaskPick(all), '停止请求不能替代设备硬件急停');
}

// On the page of 仿真试运行: the board of the simulated runs and tests goes right under the page's head,
// their list to the foot of the page. {board, list}
// Issue #66 made the page "by task": inside one task (`scope` = {task, robot}, its own simulated robot) there is
// no board — the page's own head is the task's — and the list holds that task's records only.
function rbSimulationParts(scope = null) {
  const all = rbAll().filter(e => rbOnPage('simulation', e));
  if (scope) {
    const mine = all.filter(e => ((scope.robot && e.robot_id === scope.robot) || e.tasks.some(t => t.id === scope.task))
      && (!scope.skill || e.skill_id === scope.skill));          // issue #75: on a skill's page, that skill's runs
    return {board: '', list: `<div class="rb-sim" data-rb-sim>` + (scope.skill
      ? rbListSection('这个技能的仿真运行记录', mine, '<p class="rb-none rb-none-list">这个技能还没有仿真记录。在上面运行一次，它的结果、用时和走过的路线会列在这里。</p>')
      : rbListSection('这个任务的仿真运行记录', mine,
      '<p class="rb-none rb-none-list">这个任务还没有仿真记录。在上面运行一次技能，它的结果、用时和走过的路线会列在这里。</p>')) + '</div>'};
  }
  const entries = rbOfTask(all, rbChosen(all)), totals = rbTotals(entries);
  const sim = rows('robots').find(r => r.sim);
  return {board: `<div data-rb-sim-board>${rbBoard('仿真记录', '次仿真运行与测试', totals, rbFigures(totals))}</div>`, list: `<div class="rb-sim" data-rb-sim>` +
    rbListSection('仿真运行记录', entries, empty('还没有仿真记录', '在仿真机器人上运行一次技能或做一次仿真测试，它的结果、用时和走过的路线会列在这里。',
      sim ? button('在仿真机器人上运行技能', 'sim-run-pick', 'secondary small', sim.id) : button('添加仿真机器人', 'sim-add', 'secondary small')), rbTaskPick(all)) + '</div>'};
}

// ---------------------------------------------------------------- on the page

async function rbPoll() {
  if (!state.user || !(RB_PAGES.includes(current) || current === 'record') || document.hidden) return;
  let answer;
  try { answer = await api('runs/records', {project_id: projectId}); } catch { return; }      // the next round asks again
  const text = JSON.stringify(answer.records.map(r => [r.operation_id, r.state, r.run_status, r.duration_s, r.distance_m, r.steps, r.steps_passed, r.error, r.samples > 0]));
  rbCollecting = answer.collecting !== false;
  rbRecords = answer.records;
  if (text === rbRecordsText) return;
  rbRecordsText = text;
  if (current === 'record' && typeof rrRefresh === 'function') rrRefresh();
  // Issue #75: the page of one record draws its badge and its 「走到了哪一步」 from these summaries too; it was left
  // as it was ("已受理", "0 次完成") when only a summary changed and the full record it had read did not.
  if (current === 'record' && !$('#dialog').open && !rbBusy() && typeof rrRedraw === 'function') return rrRedraw();
  if (current === 'record') { rbRecordsText = ''; return; }     // drawn at the next round, as the other pages are
  if (!RB_PAGES.includes(current) || $('#dialog').open) return;
  if (rbBusy()) { rbRecordsText = ''; return; }      // drawn at the next round
  render();
}

// Inside one task of 仿真试运行 (issue #66, web/task_sim.js: its block without the page's head): {task, robot}; null on the list of tasks.
function rbSimTask(head) {
  if (head || !$('#main [data-tsim-slot]') || typeof tsim !== 'object' || !tsim || !tsim.task) return null;
  return {task: tsim.task, robot: (tsim.view && tsim.view.robot && tsim.view.robot.id) || '', skill: tsim.skill || ''};
}

// A person is typing or choosing in the page: a redraw would take the focus away.
const rbBusy = () => Boolean(document.activeElement?.matches?.('#main input, #main textarea, #main select'));

function rbGo(target) {
  if ($('#dialog').open) $('#dialog').close();
  if (location.hash === '#' + target) render(); else location.hash = target;
}

function rbAction(type, id) {
  if (type === 'rb-open') {
    try { sessionStorage.setItem(RB_OPEN, JSON.stringify({key: id, from: RB_PAGES.includes(current) ? current : (rbOpened().from || 'harness')})); } catch { /* not remembered */ }
    return rbGo('record');
  }
  if (type === 'rb-back') return rbGo(rbOpened().from || 'harness');
  if (type === 'rb-retry' && typeof rrRetry === 'function') return rrRetry();
  return undefined;
}

function rbOpened() {
  try { return JSON.parse(sessionStorage.getItem(RB_OPEN) || '{}') || {}; } catch { return {}; }
}

function rbInstall() {
  views.harness = rbHarness;
  views.deployment = rbDeployment;
  if (typeof rrView === 'function') views.record = rrView;
  const baseAction = action, baseRender = render;
  action = async (type, id, el) => (String(type).startsWith('rb-') ? rbAction(type, id) : baseAction(type, id, el));
  render = function () {
    baseRender();
    if (current === 'simulation' && state.user && !$('#main [data-rb-sim]')) {
      const head = $('#main .page-head'), parts = rbSimulationParts(rbSimTask(head));
      if (head) head.insertAdjacentHTML('afterend', parts.board);
      $('#main').insertAdjacentHTML('beforeend', parts.list);
    }
    if (current === 'record' && typeof rrChrome === 'function') rrChrome();
  };
  document.addEventListener('change', e => {
    if (!e.target.matches?.('[data-rb-task]')) return;
    rbTask = e.target.value;
    try { if (rbTask) sessionStorage.setItem(RB_TASK, rbTask); else sessionStorage.removeItem(RB_TASK); } catch { /* not remembered */ }
    render();
  });
  setInterval(rbPoll, RB_POLL_MS);
  window.addEventListener('hashchange', () => setTimeout(rbPoll, 0));
  if (typeof route === 'function') route();          // #record was not a page while the earlier scripts loaded
  rbPoll();
}

// After every other script's own take-over (they run on DOMContentLoaded too), so this render is the outermost.
if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => setTimeout(rbInstall, 0));
