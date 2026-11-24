// Pure-logic checks of web/runtime_install.js (issue #19): which robots get "安装管理层", what the card line
// and the dialog say in each state of an install, what the forms send, and that "清理" takes two clicks.
// The rules themselves are in brainos/runtime_install*.py, checked in tests/test_runtime_install*.py.
//   node tests/runtime_install_logic.cjs
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
const calls = [], forms = [], dialogs = [];
const ssh = {id: 'robot-1', name: 'dog <1>', family: 'quadruped', connection_id: 'connection-1', endpoint: ''};
const arm = {id: 'robot-2', name: 'arm', family: 'manipulator', connection_id: 'connection-2', endpoint: 'ssh-forward:robot-2',
             runtime_install: {state: 'failed', stage: 'start', failure: {stage: 'start', stage_label: '启动', message: 'x'}, can_clean: true}};
const typed = {id: 'robot-3', name: 'typed', family: 'quadruped', endpoint: 'http://192.0.2.1:8790'};
const sim = {id: 'robot-4', name: 'sim', family: 'quadruped', connection_id: 'connection-4', sim: {state: 'running'}};
let answer = {};
const sandbox = {
  h, badge, button, field, notice, projectId: 'project-1', setInterval: () => 0,
  state: {user: {id: 'u'}, robots: [ssh, arm, typed, sim],
          device_connections: [{id: 'connection-1', username: 'robot', host: '192.0.2.7', port: 22},
                               {id: 'connection-2', username: 'arm', host: 'arm.example', port: 2222}]},
  project: () => ({id: 'project-1'}),
  find: (kind, id) => sandbox.state[kind].find(x => x.id === id),
  form: (...args) => { forms.push(args); },
  dialog: (...args) => { dialogs.push(args); },
  refresh: async () => {},
  api: async (path, payload) => { calls.push([path, payload]); return answer[path] || {}; },
  engineRobotCard: r => `<article class="card"><div class="card-body"><h3>${h(r.name)}</h3><div class="tags"><span class="tag">t</span></div></div>` +
    `<div class="card-bottom"><div class="button-row">${button('接入管理层', 'engine-runtime-link', 'secondary small', r.id)}` +
    `${button('管理层设置', 'engine-runtime-link', 'secondary small', r.id)}</div></div></article>`,
  engineAction: async (type, id) => ['base', type, id],
  engineSubmit: async (kind) => ['base', kind],
  engineRefresh: () => {},
  rlCanLink: r => !!r && !r.sim && !!r.connection_id && !r.managed_resource_id,
  rlLinked: r => !!r && r.endpoint === 'ssh-forward:' + r.id,
};
vm.createContext(sandbox);
const api = vm.runInContext(readFileSync('web/runtime_install.js', 'utf8') +
  '\n;({riPort, riSummary, riLine, riCard, riBody, riActions, riOf, riConfirming, card: r => engineRobotCard(r),' +
  ' action: (t, i) => engineAction(t, i), submit: (k, p, f) => engineSubmit(k, p, f), set: v => { riInstalls = v; }})', sandbox);
let checks = 0;
const byValue = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const check = (actual, expected, what) => { assert.deepEqual(byValue(actual), byValue(expected), what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text)}`); checks += 1; };
const lastStart = () => calls.filter(([path]) => path.endsWith('/start')).at(-1);
const stages = states => ['check', 'upload', 'install', 'start', 'link'].map((id, i) => ({id, label: ['检查设备', '上传安装包', '安装', '启动', '接入并检查'][i], state: states[i]}));

(async () => {
  // Ports: the standard one when left empty, nothing that cannot be the layer's.
  check(api.riPort(''), 8790, 'empty port');
  check(api.riPort(' 8795 '), 8795, 'typed port');
  for (const bad of ['22', '1023', '65536', '8790a', '-1', '1e4']) check(api.riPort(bad), null, 'port ' + bad);

  // Which cards: robots reached over SSH, after 接入管理层 / 管理层设置; not a typed address, not a simulated robot.
  const card = api.card(ssh);
  includes(card, 'data-action="engine-runtime-install"', 'SSH robot gets the button');
  assert.ok(card.indexOf('>接入管理层<') < card.indexOf('>安装管理层<'), 'next to 接入管理层'); checks += 1;
  includes(api.card(arm), '管理层设置</button><button type="button" class="button secondary small" data-action="engine-runtime-install"', 'after 管理层设置 when linked');
  for (const r of [typed, sim]) excludes(api.card(r), 'engine-runtime-install', 'no install for ' + r.name);
  // The line: empty and without a button while nothing happened; the saved outcome after a failure.
  check(/<div class="ri-line"[^>]*hidden><\/div>/.test(card), true, 'an empty, hidden place for the line');
  includes(api.card(arm), '安装没有完成：停在「启动」', 'the saved failure on the card');

  // The line while it runs, with the share of the upload; nothing after success.
  const running = {state: 'running', stage: 'upload', stage_label: '上传安装包', progress: {sent: 512, total: 2048}, stages: stages(['done', 'running', 'pending', 'pending', 'pending'])};
  check(api.riSummary(running), '正在安装：上传安装包 25%', 'running upload');
  check(api.riSummary({state: 'needs_choice'}), '设备上已经有管理层，等你选择怎么处理', 'choice');
  check(api.riSummary({state: 'succeeded'}), '', 'nothing after success');
  includes(api.riLine('robot-1', running), 'aria-label="查看 dog &lt;1&gt; 的管理层安装"', 'the name is escaped');

  // The dialog: steps, a failure with its log and what is left, and the package.
  const failed = {state: 'failed', stage: 'install', stages: stages(['done', 'done', 'failed', 'pending', 'pending']), can_clean: true,
                  failure: {stage: 'install', stage_label: '安装', message: 'exit <2>', log: ['pip: <b>no</b>']},
                  left: [{path: '~/neuxmind-pkg/console/runtime-0.4.0-abc', what: '安装包'}], cleanup: '点「清理」会删除…',
                  package: {runtime_version: '0.4.0', bytes: 4096}, package_dir: '~/neuxmind-pkg/console/runtime-0.4.0-abc'};
  const body = api.riBody(failed);
  includes(body, 'class="ri-stage is-failed"', 'the failed step');
  includes(body, '停在「安装」', 'where it stopped');
  includes(body, 'pip: &lt;b&gt;no&lt;/b&gt;', 'the log is escaped');
  includes(body, '~/neuxmind-pkg/console/runtime-0.4.0-abc', 'what is left');
  includes(body, '管理层 0.4.0，4 KB', 'the package');
  // "清理" takes two clicks: the first only asks.
  let actions = api.riActions(failed, 'robot-1');
  includes(actions, 'engine-runtime-install-clean"', 'clean offered');
  excludes(actions, 'clean-confirm', 'not confirmed at once');
  api.riConfirming.add('robot-1');
  actions = api.riActions(failed, 'robot-1');
  includes(actions, 'engine-runtime-install-clean-confirm', 'confirm after the first click');
  api.riConfirming.delete('robot-1');
  excludes(api.riActions({...failed, can_clean: false}, 'robot-1'), 'clean', 'no clean when nothing is ours');
  includes(api.riActions(running, 'robot-1'), 'engine-runtime-install-stop', 'stop while running');
  const stopping = {...running, stopping: true};
  excludes(api.riActions(stopping, 'robot-1'), 'engine-runtime-install-stop', 'no second stop while stopping');
  includes(api.riActions(stopping, 'robot-1'), '正在停止', 'stopping is said');
  check(api.riSummary(stopping), '正在停止：上传安装包', 'the card line while stopping');

  // The start form sends the robot and the port, nothing else; a choice sends what was chosen.
  answer = {'robots/runtime-install/start': {...running, robot_id: 'robot-1'}};
  await api.submit('engine-runtime-install', {port: '8790'}, {dataset: {id: 'robot-1'}});
  check(lastStart(), ['robots/runtime-install/start', {project_id: 'project-1', robot_id: 'robot-1', port: 8790}], 'start request');
  await assert.rejects(api.submit('engine-runtime-install', {port: '80'}, {dataset: {id: 'robot-1'}}), /1024–65535/); checks += 1;
  await assert.rejects(api.submit('engine-runtime-install', {existing: 'replace'}, {dataset: {id: 'robot-1', choice: '1', port: '8790'}}), /勾选确认/); checks += 1;
  await api.submit('engine-runtime-install', {existing: 'replace', confirm_replace: 'on'}, {dataset: {id: 'robot-1', choice: '1', port: '8790'}});
  check(lastStart()[1], {project_id: 'project-1', robot_id: 'robot-1', port: 8790, existing: 'replace', confirm_replace: true}, 'replace request');
  await api.submit('engine-runtime-install', {existing: 'use'}, {dataset: {id: 'robot-1', choice: '1', port: '8791'}});
  check(lastStart()[1], {project_id: 'project-1', robot_id: 'robot-1', port: 8791, existing: 'use'}, 'use request');
  check(await api.submit('engine-other', {}, {}), ['base', 'engine-other'], 'other forms pass through');
  check(await api.action('engine-other', 'x'), ['base', 'engine-other', 'x'], 'other actions pass through');

  console.log(JSON.stringify({ok: true, checks}));
})().catch(error => { console.error(error); process.exit(1); });
