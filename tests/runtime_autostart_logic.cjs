// Pure-logic checks of web/runtime_autostart.js (issue #20): what the card line says in every state, the
// confirmation (what it lists, when it asks for a password, what it sends), the box in the install form,
// and the restart hint of a real robot's stuck reset. The rules are in brainos/runtime_autostart*.py.
//   node tests/runtime_autostart_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const badge = (text, cls = '') => `<span class="status ${cls}"><i class="dot"></i>${h(text)}</span>`;
const button = (text, action, cls = '', id = '', disabled = false) =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${id ? `data-id="${h(id)}"` : ''} ${disabled ? 'disabled' : ''}>${text}</button>`;
const field = (label, control, note = '') => `<label class="field">${label}${control}${note ? `<small>${note}</small>` : ''}</label>`;
const notice = (text, warn = false) => `<div class="notice ${warn ? 'warn' : ''}"><span>↗</span><div>${text}</div></div>`;
const PKG = 'runtime-0.4.0-abcdef012345';
const installed = {state: 'succeeded', package_id: PKG, created_install: true, console_install: true, port: 8795, adapter: 'sim_arm'};
const robots = {
  fresh: {id: 'r-fresh', name: 'fresh', family: 'quadruped', connection_id: 'c-1'},
  asked: {id: 'r-asked', name: 'asked', family: 'quadruped', connection_id: 'c-1', runtime_install: installed},
  on: {id: 'r-on', name: 'on <dog>', family: 'quadruped', connection_id: 'c-1', runtime_install: installed,
       runtime_autostart: {state: 'on', unit: '/etc/systemd/system/neuxmind-runtime.service', message: '已开机自启：…'}},
  declined: {id: 'r-declined', name: 'declined', family: 'quadruped', connection_id: 'c-1', runtime_install: installed,
             runtime_autostart: {state: 'off', reason: 'declined', message: '你没有确认'}},
  nosudo: {id: 'r-nosudo', name: 'nosudo', family: 'quadruped', connection_id: 'c-1', runtime_install: installed,
           runtime_autostart: {state: 'off', reason: 'no_sudo', message: '这个账号不在 sudoers 里'}},
  hand: {id: 'r-hand', name: 'hand', family: 'quadruped', connection_id: 'c-1',
         runtime_install: {state: 'succeeded', package_id: PKG, created_install: false, console_install: false}},
  failed: {id: 'r-failed', name: 'failed', family: 'quadruped', connection_id: 'c-1', runtime_install: installed,
           runtime_autostart: {state: 'failed', reason: 'failed', message: 'systemd 没能启动'}},
  typed: {id: 'r-typed', name: 'typed', family: 'quadruped', endpoint: 'http://192.0.2.1:8790'},
};
const calls = [], forms = [], dialogs = [];
let answer = {};
const sandbox = {
  h, badge, button, field, notice, projectId: 'project-1', setInterval: () => 0,
  state: {user: {id: 'u'}, robots: Object.values(robots), device_connections: [{id: 'c-1', username: 'robot', host: '192.0.2.7', port: 22}]},
  project: () => ({id: 'project-1'}),
  find: (kind, id) => sandbox.state[kind].find(x => x.id === id),
  form: (...args) => { forms.push(args); },
  dialog: (...args) => { dialogs.push(args); },
  refresh: async () => {},
  api: async (path, payload) => { calls.push([path, JSON.parse(JSON.stringify(payload))]); return answer[path] || {}; },
  engineRobotCard: r => `<article class="card"><div class="card-body"><h3>${h(r.name)}</h3><div class="tags"><span class="tag">t</span></div></div>` +
    `<div class="card-bottom"><div class="button-row">${button('接入管理层', 'engine-runtime-link', 'secondary small', r.id)}</div></div></article>`,
  engineAction: async (type, id) => ['base', type, id],
  engineSubmit: async (kind) => ['base', kind],
  engineRefresh: () => {},
  rlCanLink: r => !!r && !r.sim && !!r.connection_id && !r.managed_resource_id,
  rlLinked: () => false,
};
vm.createContext(sandbox);
const page = vm.runInContext(readFileSync('web/runtime_install.js', 'utf8') + '\n' + readFileSync('web/runtime_autostart.js', 'utf8') +
  '\n;({raStatus, raLine, raRestartHint, raInstallRequest, riSummary, riResult, riActions, card: r => engineRobotCard(r),' +
  ' action: (t, i) => engineAction(t, i), submit: (k, p, f) => engineSubmit(k, p, f), riSubmit, set: v => { riInstalls = v; }})', sandbox);
let checks = 0;
const byValue = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const check = (actual, expected, what) => { assert.deepEqual(byValue(actual), byValue(expected), what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const sent = () => calls.filter(([path]) => !path.endsWith('/status'));      // the page also asks for the progress

const plan = (fields = {}) => ({id: 'plan-1', kind: 'on', user: 'robot', host: 'robot@192.0.2.7', how: 'password', how_label: '用 sudo 执行（需要这个账号的 sudo 密码）',
  needs_password: true, unit_path: '/etc/systemd/system/neuxmind-runtime.service', unit: '[Unit]\nDescription=<x>\n', script: 'echo <script>',
  error: null, attempt: 0, does_not: '不改别的系统文件',
  account_steps: [{command: 'printf ... > ~/neuxmind/.autostart', what: '写标记文件'}],
  commands: [{command: 'sudo tee /etc/systemd/system/neuxmind-runtime.service', what: '写入服务文件'},
             {command: 'sudo systemctl enable neuxmind-runtime.service', what: '登记开机自启'}], ...fields});
const waitingView = (fields = {}) => ({robot_id: 'r-asked', job_id: 'job-1', mode: 'install', state: 'running', stage: 'autostart', stage_label: '开机自启',
  stages: [], autostart: {state: 'waiting', plan: plan(fields)}});

(async () => {
  // The card line: on, off and why, failed, and what an install alone says.
  const status = id => page.raStatus(robots[id]);
  check(status('on'), {tone: 'good', text: '已开机自启', detail: '系统服务 neuxmind-runtime.service'}, 'on');
  check(status('fresh'), {tone: 'warn', text: '未开机自启', detail: '还没有安装管理层'}, 'never installed');
  check(status('asked'), {tone: 'warn', text: '未开机自启', detail: '安装时没有选择'}, 'installed without it');
  check(status('declined').detail, '你没有确认', 'declined');
  check(status('nosudo').detail, '这个账号不能用 sudo', 'no sudo');
  check(status('hand').detail, '不是这个控制台装的管理层', 'a hand install');
  check(status('failed').tone, 'bad', 'failed');
  const card = page.card(robots.on);
  includes(card, 'data-ra-robot="r-on"', 'the card has the line');
  includes(card, '已开机自启', 'and says so');
  includes(card, 'aria-label="on &lt;dog&gt; 的开机自启"', 'the name is escaped');
  check((card.match(/class="button /g) || []).length, 2,
    'the stub\'s button and #19\'s "安装管理层": the line itself has a text link, no card button of its own');
  excludes(page.card(robots.typed), 'data-ra-robot', 'a robot not reached over SSH has no autostart line');

  // While the job waits for the person: the line and the summary say so.
  page.set({'r-asked': waitingView()});
  check(status('asked'), {tone: 'warn', text: '等你确认要执行的内容', detail: ''}, 'waiting');
  includes(page.raLine(robots.asked), '>去确认<', 'the line leads to the confirmation');
  check(page.riSummary(waitingView()), '等你确认开机自启要执行的内容', 'summary while waiting');

  // The confirmation lists everything, asks for the password only when sudo needs one, sends it once.
  forms.length = 0;
  await page.action('engine-runtime-autostart', 'r-asked');
  const [title, , body, kind, submitLabel, attrs] = forms.at(-1);
  check([title, kind, submitLabel], ['开机自启：确认要执行的内容', 'engine-runtime-autostart-answer', '确认执行'], 'the form');
  for (const part of ['sudo tee /etc/systemd/system/neuxmind-runtime.service', 'sudo systemctl enable neuxmind-runtime.service', '写标记文件',
                      'Description=&lt;x&gt;', 'echo &lt;script&gt;', '不改别的系统文件', 'robot@192.0.2.7', 'name="sudo_password" type="password"',
                      'name="confirm_plan" type="checkbox"'.replace(' type="checkbox"', ''), 'engine-runtime-autostart-decline']) {
    includes(body, part, 'the confirmation lists ' + part);
  }
  excludes(body, 'value=', 'no value is put into any field');
  includes(attrs, 'data-needs-sudo="1"', 'a password is needed');
  calls.length = 0;
  answer = {'robots/runtime-install/answer': {robot_id: 'r-asked', job_id: 'job-1', state: 'running', mode: 'install', stages: [], autostart: {state: 'running'}}};
  const f = {dataset: {id: 'r-asked', job: 'job-1', plan: 'plan-1', needsSudo: '1'}};
  await assert.rejects(page.submit('engine-runtime-autostart-answer', {sudo_password: 'p'}, f), /勾选/);
  check(sent().length, 0, 'nothing is sent before the box is ticked');
  await page.submit('engine-runtime-autostart-answer', {sudo_password: 'secret-typed', confirm_plan: 'on'}, f);
  check(sent(), [['robots/runtime-install/answer', {project_id: 'project-1', robot_id: 'r-asked', job_id: 'job-1', plan: 'plan-1',
    decision: 'confirm', sudo_password: 'secret-typed'}]], 'confirm with the password');
  forms.length = 0;
  page.set({'r-asked': waitingView({needs_password: false, how: 'nopasswd'})});
  await page.action('engine-runtime-autostart', 'r-asked');
  excludes(forms.at(-1)[2], 'sudo_password', 'no password field when sudo needs none');
  calls.length = 0;
  await page.submit('engine-runtime-autostart-answer', {confirm_plan: 'on', sudo_password: 'ignored'}, {dataset: {id: 'r-asked', job: 'job-1', plan: 'plan-1', needsSudo: ''}});
  check(sent()[0][1].sudo_password, undefined, 'and none is sent');
  calls.length = 0;
  page.set({'r-asked': waitingView()});
  await page.action('engine-runtime-autostart-decline', 'r-asked');
  check(sent(), [['robots/runtime-install/answer', {project_id: 'project-1', robot_id: 'r-asked', job_id: 'job-1', plan: 'plan-1', decision: 'decline'}]],
    'declining sends no password');
  forms.length = 0;
  page.set({'r-asked': waitingView({error: 'sudo 密码不对'})});
  await page.action('engine-runtime-autostart', 'r-asked');
  includes(forms.at(-1)[2], 'role="alert">sudo 密码不对', 'a wrong password is said above the list');

  // The card's own dialog: turn it on (an install of this console), off (when on), or say why it cannot.
  page.set({});
  dialogs.length = 0;
  await page.action('engine-runtime-autostart', 'r-on');
  includes(dialogs.at(-1)[2], 'engine-runtime-autostart-off', 'on: it can be turned off');
  await page.action('engine-runtime-autostart', 'r-declined');
  includes(dialogs.at(-1)[2], 'engine-runtime-autostart-on', 'off: it can be turned on');
  await page.action('engine-runtime-autostart', 'r-hand');
  excludes(dialogs.at(-1)[2], 'engine-runtime-autostart-on', 'a hand install: refused');
  includes(dialogs.at(-1)[2], '删除后重新安装', 'and it says how to get there');
  calls.length = 0;
  answer = {'robots/runtime-install/autostart': {robot_id: 'r-declined', job_id: 'job-2', mode: 'autostart', state: 'running', stages: []}};
  await page.action('engine-runtime-autostart-on', 'r-declined');
  check(sent(), [['robots/runtime-install/autostart', {project_id: 'project-1', robot_id: 'r-declined'}]], 'turning it on');
  check(page.riSummary(answer['robots/runtime-install/autostart']), '正在设置开机自启：', 'its own job has its own words');

  // The install form: the box sends autostart only when ticked.
  check(page.raInstallRequest({autostart: 'on'}), {autostart: true}, 'ticked');
  check(page.raInstallRequest({}), {}, 'not ticked');
  calls.length = 0;
  answer = {};
  await page.riSubmit({port: '8790', autostart: 'on'}, {dataset: {id: 'r-fresh'}});
  check(sent().at(-1), ['robots/runtime-install/start', {project_id: 'project-1', robot_id: 'r-fresh', port: 8790, autostart: true}], 'the install asks for it');
  await page.riSubmit({port: '8790'}, {dataset: {id: 'r-fresh'}});
  check(sent().at(-1)[1].autostart, undefined, 'and not without the box');

  // The result of an install that asked for it, and of the card's own jobs.
  const done = {mode: 'install', state: 'succeeded', autostart: {state: 'off', message: '你没有确认'}, stages: [], notes: []};
  includes(page.riResult(done), '未开机自启：你没有确认', 'install result says autostart is off and why');
  includes(page.riResult({...done, mode: 'autostart_off', autostart: {state: 'off', message: '已取消开机自启'}}), '已取消开机自启', 'off');
  includes(page.riActions({mode: 'autostart', state: 'failed'}, 'r-x'), 'engine-runtime-autostart-off', 'a failed turn-on offers to take it away');
  excludes(page.riActions({mode: 'autostart', state: 'failed'}, 'r-x'), 'engine-runtime-install-again', 'not "重新安装"');

  // ux04's restart hint for a real robot: the service only when it exists.
  includes(page.raRestartHint(robots.on), 'sudo systemctl restart neuxmind-runtime.service', 'autostart on');
  const own = page.raRestartHint(robots.asked);
  includes(own, `bash ~/neuxmind-pkg/console/${PKG}/device_install.sh stop`, 'the console install: its script');
  includes(own, `NX_PORT=8795 NX_ADAPTER=sim_arm bash ~/neuxmind-pkg/console/${PKG}/device_install.sh start`, 'with its port and body');
  excludes(own, 'systemctl', 'no service that is not there');
  includes(page.raRestartHint(robots.hand), '不是这个控制台装的', 'a hand install: no command');
  includes(page.raRestartHint(robots.typed), '不是这个控制台装的', 'nor for a robot with a typed address');
  excludes(page.raRestartHint({runtime_install: {package_id: 'runtime-x; rm -rf ~', console_install: true}}), 'rm -rf', 'only a plain package id');
  console.log(`runtime_autostart_logic: ${checks} checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
