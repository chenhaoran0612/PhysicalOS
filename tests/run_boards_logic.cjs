// Pure-logic checks of web/run_boards.js and web/run_record_view.js (issue #70): the records of
// 仿真试运行 / 验证与测试 / 部署与运行 are one list built from what was recorded, a board counts how they
// ended, and the page of a record draws the route and the series from the samples.
//   node tests/run_boards_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const table = (headers, data) => `<div class="table-wrap"><table><thead><tr>${headers.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${data.join('')}</tbody></table></div>`;
const sandbox = {h, button, badge, table, date: v => (v ? String(v).slice(5, 16) : '—'), simBadge: () => badge('仿真', 'blue sim'),
                 reportScopeName: r => (r.scope === 'simulation' ? '仿真测试（不能作为现场证据）' : r.scope === 'onsite' ? '设备端现场测试' : '静态工程检查')};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/run_boards.js', 'utf8') + '\n' + readFileSync('web/run_record_view.js', 'utf8') +
  '\n;({rbDuration, rbTasks, rbOperationStatus, rbEntries, rbOnPage, rbOfTask, rbTotals, rbFlow, rbBoard, rbFigures, rbRow,' +
  ' rrPathPoints, rrPathSvg, rrLinePoints, rrLineSvg, rrEventTime, RB_PAGES, RB_STAGES})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };

// ---- durations
same(page.rbDuration(4.25), '4.3 秒', 'a short run keeps a decimal');
same(page.rbDuration(42), '42 秒');
same(page.rbDuration(72), '1 分 12 秒');
same(page.rbDuration(120), '2 分');
same([page.rbDuration(119.6), page.rbDuration(59.7), page.rbDuration(3599.7)], ['2 分', '1 分', '1 小时'], 'seconds are rounded before they are split');
same(page.rbDuration(3 * 3600 + 5 * 60), '3 小时 5 分');
same([page.rbDuration(null), page.rbDuration(-1), page.rbDuration(NaN)], ['', '', ''], 'nothing is shown for a time that is not known');

// ---- the account: a simulated dog and a real one of the same model, one application bound by the task 巡检
const HOME = 'project-robot-home';
const st = {
  robot_home: HOME,
  projects: [{id: 'task-1', name: '仓库巡检', robot_id: 'real-1'}, {id: 'task-2', name: '搬运'}],
  robots: [{id: 'sim-1', name: '仿真狗', sim: {kind: 'quadruped'}, status: 'connected', created_at: '2026-11-20T08:00:00Z', product: {vendor: 'NX', model: 'L2'}},
           {id: 'real-1', name: '巡检狗', status: 'connected', created_at: '2026-11-21T08:00:00Z', product: {vendor: 'NX', model: 'L2'}}],
  applications: [{id: 'app-1', name: '每日巡检', project_id: HOME}, {id: 'app-2', name: '每日巡检', root_application_id: 'app-1', project_id: HOME}],
  task_applications: [{id: 'taskapp-1', project_id: 'task-1', application_id: 'app-1'}],
  deployments: [{id: 'release-1', application_id: 'app-2', application_name: '每日巡检', robot_id: 'real-1', status: 'deployed', report_id: 'onsite-report-2', created_at: '2026-11-25T09:00:00Z'}],
  evaluations: [
    {id: 'check-1', scope: 'static', application_id: 'app-2', robot_id: 'real-1', passed: true, created_at: '2026-11-22T09:00:00Z', cases: [{name: '能力', passed: true}, {name: '版本', passed: true}]},
    {id: 'simulation-report-1', scope: 'simulation', simulated: true, application_id: 'app-2', robot_id: 'sim-1', passed: true, created_at: '2026-11-23T09:05:00Z', details: {cases: [{name: '步骤 1', passed: true}]}},
    {id: 'onsite-report-2', scope: 'onsite', application_id: 'app-2', robot_id: 'real-1', passed: false, created_at: '2026-11-24T09:05:00Z', details: {cases: [{name: '步骤 1', passed: false}]}},
  ],
  operations: [
    {id: 'skill-install-1', kind: 'skill_install', robot_id: 'real-1', skill_id: 'patrol_places', version: '1.0.0', status: 'succeeded', created_at: '2026-11-21T10:00:00Z'},
    {id: 'skill-run-1', kind: 'skill_run', robot_id: 'sim-1', skill_id: 'patrol_places', status: 'accepted', simulated: true, created_at: '2026-11-23T08:00:00Z'},
    {id: 'onsite-1', kind: 'onsite', robot_id: 'sim-1', application_id: 'app-2', status: 'succeeded', report_id: 'simulation-report-1', created_at: '2026-11-23T09:00:00Z', finished_at: '2026-11-23T09:01:30Z'},
    {id: 'onsite-2', kind: 'onsite', robot_id: 'real-1', application_id: 'app-2', status: 'succeeded', report_id: 'onsite-report-2', created_at: '2026-11-24T09:00:00Z'},
    {id: 'skill-run-2', kind: 'skill_run', robot_id: 'real-1', skill_id: 'patrol_places', status: 'rejected', error: '设备拒绝运行：参数不对', created_at: '2026-11-24T10:00:00Z'},
    {id: 'operation-1', kind: 'start', robot_id: 'real-1', release_id: 'release-1', status: 'accepted', created_at: '2026-11-25T10:00:00Z'},
    {id: 'reset-1', kind: 'reset', robot_id: 'real-1', status: 'accepted', created_at: '2026-11-25T11:00:00Z'},
  ],
};
const summaries = [
  {operation_id: 'skill-run-1', state: 'final', run_status: 'succeeded', duration_s: 31.5, distance_m: 12.4, samples: 30, steps: 0, steps_passed: 0},
  {operation_id: 'operation-1', state: 'following', run_status: 'running', duration_s: null, distance_m: 3, samples: 8, steps: 2, steps_passed: 1},
];
const entries = page.rbEntries(st, summaries);
const of = key => entries.find(e => e.key === key);

same(entries.map(e => e.key), ['op:reset-1', 'op:operation-1', 'op:skill-run-2', 'op:onsite-2', 'op:onsite-1', 'op:skill-run-1', 'ev:check-1', 'op:skill-install-1'],
  'every operation is a record, a report with its operation is not listed twice, newest first');
same(of('op:skill-run-1').status, {key: 'pass', text: '已完成'}, 'a followed run shows how it ended, not that it was accepted');
same([of('op:skill-run-1').duration_s, of('op:skill-run-1').distance_m, of('op:skill-run-1').collected], [31.5, 12.4, true]);
same(of('op:operation-1').status, {key: 'run', text: '运行中'});
same(of('op:onsite-1').status, {key: 'pass', text: '通过'}, 'an on-site test stands as its report says');
same(of('op:onsite-2').status, {key: 'fail', text: '存在缺项'});
same(of('op:onsite-1').duration_s, 90, 'without a run record the time is from the request to its end');
same(of('op:skill-run-2').status, {key: 'fail', text: '设备拒绝'});
same([of('op:skill-run-2').collected, of('op:skill-run-2').samples], [false, 0]);
same(of('op:reset-1').status, {key: 'wait', text: '已受理'});
same(of('ev:check-1').name, '静态工程检查');
same([of('ev:check-1').steps, of('ev:check-1').steps_passed], [2, 2]);
same([of('op:skill-run-1').sim, of('op:onsite-1').sim, of('op:onsite-2').sim], [true, true, false]);
same(of('op:operation-1').subject, '每日巡检', 'started work is named by the released application');
same(of('op:skill-install-1').subject, 'patrol_places 1.0.0');

// ---- a run whose following was cut off, and a status the page does not know
same(page.rbOperationStatus({kind: 'skill_run', status: 'accepted'}, {state: 'lost', run_status: 'running'}, null), {key: 'wait', text: '结果没有收集到'},
  'a run that was not followed to its end does not stay 运行中');
same(page.rbOperationStatus({kind: 'skill_run', status: 'accepted'}, {state: 'final', run_status: 'constructor'}, null), {key: 'wait', text: '已受理'},
  'a status that is the name of something every object has is not looked up');
same(page.rbOperationStatus({kind: 'start', status: 'toString'}, null, null), {key: 'none', text: 'toString'});

// ---- tasks
same(of('op:onsite-2').tasks, [{id: 'task-1', name: '仓库巡检'}], 'a record of an application belongs to the tasks that bound any version of it');
same(of('op:operation-1').tasks, [{id: 'task-1', name: '仓库巡检'}], 'started work is found through its release');
same(of('op:skill-run-2').tasks, [{id: 'task-1', name: '仓库巡检'}], 'a skill run belongs to the task whose robot it ran on');
same(of('op:skill-run-1').tasks, [], 'a run on a robot no task chose belongs to none');
same(page.rbOfTask(entries, 'task-2'), []);
same(page.rbOfTask(entries, '').length, entries.length);

// ---- which page lists what
const on = name => entries.filter(e => page.rbOnPage(name, e)).map(e => e.id);
same(on('simulation'), ['onsite-1', 'skill-run-1'], '仿真试运行: what ran on a simulated robot');
same(on('harness'), ['skill-run-2', 'onsite-2', 'onsite-1', 'check-1'], '验证与测试: checks, tests (simulated ones marked) and runs on a real robot');
same(on('deployment'), ['reset-1', 'operation-1', 'skill-run-2', 'onsite-2', 'onsite-1', 'skill-run-1', 'skill-install-1'], '部署与运行: every request sent to a robot');
same(page.rbOnPage('robots', entries[0]), false);

// ---- the board
const totals = page.rbTotals(entries.filter(e => page.rbOnPage('harness', e)));
same([totals.total, totals.pass, totals.fail, totals.run, totals.rate, totals.robots], [4, 2, 2, 0, 50, 2]);
same(page.rbTotals(entries.filter(e => page.rbOnPage('simulation', e))).seconds, 121.5, 'the time of every record that has one');
same(page.rbTotals([]).rate, null, 'no rate without a record that ended one way or the other');
const board = page.rbBoard('测试记录', '次检查与测试', totals, page.rbFigures(totals));
ok(board.includes('<strong>4</strong>') && board.includes('50%'), 'the board has the count and the rate');
ok(board.includes('class="pass" style="flex:2"') && board.includes('class="fail" style="flex:2"') && !board.includes('class="run"'), 'the bar has a part for every way records ended, and no empty part');
ok(page.rbBoard('x', 'y', page.rbTotals([]), page.rbFigures(page.rbTotals([]))).includes('还没有记录'), 'an empty board says so');

// ---- a row
const row = page.rbRow(of('op:onsite-1'));
ok(row.includes('data-action="rb-open" data-id="op:onsite-1"') && row.includes('仿真测试（不能作为现场证据）') && row.includes('status blue sim'), 'a simulated test is marked in its row');
ok(row.includes('1 分 30 秒') && row.includes('机器人：仿真狗'), 'the row has the time and the robot');
ok(page.rbRow(of('op:skill-run-2')).includes('没有运行数据'), 'a request without a run says there is no run data');
ok(!page.rbRow({...of('op:skill-run-1'), subject: '<img>'}).includes('<img>'), 'what a device or a person named is escaped');

// ---- the five stages
const flow = page.rbFlow(st, entries, 'real-1', 'app-2');
same(flow.map(s => s.name), ['接入机器人', '安装技能', '仿真试运行', '真机测试', '部署运行']);
same(flow.map(s => s.state), ['done', 'done', 'done', 'done', 'done']);
same(flow.map(s => s.text), ['已连接', '装了 1 个技能包', '2 次，2 次完成', '2 次，0 次通过', '已发布，启动过 1 次'],
  'the simulated runs of the same application and of the same brand and model count for the real robot');
same(page.rbFlow(st, entries, 'sim-1', '').map(s => s.state), ['done', 'none', 'done', 'none', 'none'], 'a simulated robot has not reached the stages of a real one');
same(page.rbFlow(st, entries, 'gone', '')[0].text, '机器人已删除');

// ---- the route and the series
const samples = [{t: 0, x: 0, y: 0, speed: 0, battery: 90, progress: 0}, {t: 1, x: 2, y: 0, speed: 0.5, battery: 90}, {t: 2}, {t: 3, x: 2, y: 1, speed: 0.2, battery: 89, progress: 1}];
const drawn = page.rrPathPoints(samples);
same(drawn.points.length, 3, 'a sample without a position is not a point of the route');
ok(drawn.points[1][0] > drawn.points[0][0] && drawn.points[2][1] < drawn.points[1][1], 'x grows to the right and y grows upwards, as on a map');
ok(drawn.points.every(([x, y]) => x >= 0 && x <= 520 && y >= 0 && y <= 300), 'the route stays inside the drawing');
same(page.rrPathPoints([{t: 0, x: 1, y: 1}]), null);
ok(page.rrPathSvg(samples).includes('<polyline') && page.rrPathSvg(samples).includes('米'), 'the route has a scale bar');
same(page.rrPathSvg([]), '');
same(page.rrPathPoints([{x: 1, y: 1}, {x: 1, y: 1}]).points.length, 2, 'a robot that stood still is still drawn');
const line = page.rrLinePoints(samples, 'speed');
same([line.min, line.max, line.seconds, line.points.length], [0, 0.5, 3, 3]);
same(page.rrLinePoints(samples, 'yaw'), null, 'a value that was never sampled has no chart');
ok(Number.isFinite(page.rrLinePoints([{t: 0, battery: 80}, {t: 5, battery: 80}], 'battery').points[0][1]), 'a value that never changed is a flat line');
ok(page.rrLineSvg(samples, 'progress', '进度', '').includes('0% – 100%'), 'progress is shown in percent');
same(page.rrEventTime({ts: 1795773605.0}, '2026-11-27T10:00:01+00:00'), '+4 秒');
same([page.rrEventTime({}, '2026-11-27T10:00:01+00:00'), page.rrEventTime({ts: 5}, null)], ['', '']);

console.log(`run boards logic: ${checks} checks passed`);
