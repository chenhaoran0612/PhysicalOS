// Pure-logic checks of web/robot_body.js (issue #38): what a robot's body is when nothing was saved, what the
// form sends, what "从机器人读取" makes of the robot's answer, the preview of the places of a device
// configuration, the card, and how the install dialog shows a real body and the job that applies one.
// The rules are in brainos/robot_body.py and brainos/runtime_install.py.
//   node tests/robot_body_logic.cjs
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
const EXAMPLE = readFileSync('runtime/examples/ros2/onbody-programs.example.json', 'utf8');
const real = (state, more = {}) => ({kind: 'ros2', setup: '/opt/ros/humble/setup.bash', state,
  settings: {ROS_DOMAIN_ID: '24', RMW_IMPLEMENTATION: 'rmw_zenoh_cpp', ZENOH_SESSION_CONFIG_URI: '/opt/z/session.json5'}, config_text: EXAMPLE, ...more});
const robots = {
  plain: {id: 'r-plain', name: 'plain <dog>', family: 'quadruped', connection_id: 'c-1'},
  real: {id: 'r-real', name: 'real', family: 'quadruped', connection_id: 'c-1', body: real('current')},
  changed: {id: 'r-changed', name: 'changed', family: 'quadruped', connection_id: 'c-1', body: real('config_changed')},
  typed: {id: 'r-typed', name: 'typed', family: 'quadruped', endpoint: 'http://192.0.2.1:8790'},
  sim: {id: 'r-sim', name: 'sim', family: 'quadruped', sim: {kind: 'quadruped'}},
};
const calls = [], forms = [], dialogs = [];
let answer = {};
const sandbox = {
  h, badge, button, field, notice, projectId: 'project-1', setInterval: () => 0, Blob, FormData: class {},
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
const page = vm.runInContext(['runtime_install', 'runtime_autostart', 'robot_body'].map(name => readFileSync(`web/${name}.js`, 'utf8')).join('\n') +
  '\n;({rbBody, rbRequest, rbFromDetect, rbPlaces, rbPreview, rbLine, RB_STATE, RB_EXAMPLE, riSummary, riResult, riActions, riFound,' +
  ' card: r => engineRobotCard(r), action: (t, i) => engineAction(t, i), submit: (k, p, f) => engineSubmit(k, p, f), set: v => { riInstalls = v; }})', sandbox);
let checks = 0;
const byValue = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const same = (actual, expected, what) => { assert.deepEqual(byValue(actual), byValue(expected), what); checks += 1; };
const includes = (text, part, what) => { assert.ok(String(text).includes(part), `${what}: ${JSON.stringify(text).slice(0, 400)}`); checks += 1; };
const excludes = (text, part, what) => { assert.ok(!String(text).includes(part), `${what}: ${JSON.stringify(text).slice(0, 400)}`); checks += 1; };
const throws = (run, part, what) => { assert.throws(run, error => String(error.message).includes(part), what); checks += 1; };

(async () => {
  // --- the body of a robot
  same(page.rbBody(robots.plain), {kind: 'sim', setup: '/opt/ros/humble/setup.bash', settings: {}, config_text: '', state: 'sim'}, 'nothing saved: simulated');
  same(page.rbBody(robots.real).state, 'current', 'the saved state is kept');
  same(page.rbBody({body: {kind: 'ros2', state: 'nonsense', settings: {ROS_DOMAIN_ID: 7, LD_PRELOAD: '/x'}}}),
    {kind: 'ros2', setup: '/opt/ros/humble/setup.bash', settings: {ROS_DOMAIN_ID: '7'}, config_text: '', state: 'unset'}, 'unknown fields and states are not shown');
  same(page.rbBody({body: {kind: 'other'}}).kind, 'sim', 'an unknown kind is simulated');

  // --- what the form sends
  same(page.rbRequest({kind: 'ros2', setup: ' /opt/robot/install/setup.bash ', ROS_DOMAIN_ID: ' 024', ROS_LOCALHOST_ONLY: '0',
    RMW_IMPLEMENTATION: 'rmw_zenoh_cpp', rmw_file: '/opt/z/session.json5', config_text: '{}'}),
    {kind: 'ros2', setup: '/opt/robot/install/setup.bash', config_text: '{}',
     settings: {ROS_DOMAIN_ID: '24', ROS_LOCALHOST_ONLY: '0', RMW_IMPLEMENTATION: 'rmw_zenoh_cpp', ZENOH_SESSION_CONFIG_URI: '/opt/z/session.json5'}}, 'a full real body');
  same(page.rbRequest({kind: 'sim'}), {kind: 'sim', setup: '/opt/ros/humble/setup.bash', settings: {}, config_text: ''}, 'empty fields are left out');
  same(page.rbRequest({kind: 'ros2', RMW_IMPLEMENTATION: 'rmw_fastrtps_cpp', rmw_file: '/etc/fast.xml'}).settings,
    {RMW_IMPLEMENTATION: 'rmw_fastrtps_cpp', FASTRTPS_DEFAULT_PROFILES_FILE: '/etc/fast.xml'}, 'the file goes to the setting of its RMW');
  same(page.rbRequest({kind: 'ros2', RMW_IMPLEMENTATION: 'rmw_cyclonedds_cpp', rmw_file: '/etc/c.xml'}).settings.CYCLONEDDS_URI, '/etc/c.xml', 'CycloneDDS too');
  throws(() => page.rbRequest({kind: 'ros2', ROS_DOMAIN_ID: '233'}), '0–232', 'a domain out of range');
  throws(() => page.rbRequest({kind: 'ros2', ROS_DOMAIN_ID: 'x'}), '0–232', 'a domain that is not a number');
  throws(() => page.rbRequest({kind: 'ros2', RMW_IMPLEMENTATION: 'zenoh'}), 'rmw_fastrtps_cpp', 'an RMW that is not one');
  throws(() => page.rbRequest({kind: 'ros2', rmw_file: '/etc/x.xml'}), '一起填', 'a file without its RMW');
  throws(() => page.rbRequest({kind: 'ros2', RMW_IMPLEMENTATION: 'rmw_other_cpp', rmw_file: '/etc/x.xml'}), '一起填', 'a file of an RMW without one');
  throws(() => page.rbRequest({kind: 'ros2', config_text: 'x'.repeat(256 * 1024 + 1)}), '太大', 'a configuration too large');

  // --- what the robot answered
  const installed = {adapter: 'ros2', setup: '/opt/ros/humble/setup.bash', settings: {ROS_DOMAIN_ID: '24'}, config_bytes: 9000, config_text: EXAMPLE};
  let found = page.rbFromDetect({setups: ['/opt/ros/humble/setup.bash'], running: {count: 40, settings: {ROS_DOMAIN_ID: '3'}}, installed});
  same([found.kind, found.setup, found.settings, found.config_text === EXAMPLE, found.tone], ['ros2', '/opt/ros/humble/setup.bash', {ROS_DOMAIN_ID: '24'}, true, 'good'],
    'a real install on the robot comes first');
  includes(found.message, '应用到机器人', 'and says what makes the two agree');
  found = page.rbFromDetect({setups: [], running: {count: 0, settings: {}}, installed: {...installed, config_text: null}});
  same(found.config_text, null, 'a configuration that could not be read is not put in');
  includes(found.message, '导入文件', 'and says how to bring it');
  found = page.rbFromDetect({setups: [], running: {count: 0, settings: {}}, installed: {...installed, config_text: null, config_bytes: null}});
  includes(found.message, '没有本体配置', 'a real install without one');
  found = page.rbFromDetect({setups: ['/opt/ros/humble/setup.bash'], running: {count: 12, settings: {ROS_DOMAIN_ID: '24', RMW_IMPLEMENTATION: 'rmw_zenoh_cpp'}},
    installed: {adapter: 'sim_quadruped', setup: '', settings: {}, config_bytes: null, config_text: null}});
  same([found.kind, found.settings.ROS_DOMAIN_ID, found.config_text, found.tone], ['ros2', '24', null, 'good'], 'else the running programs');
  includes(found.message, '仿真本体', 'with what is installed now');
  includes(found.message, '12 个', 'and how many programs');
  found = page.rbFromDetect({setups: ['/opt/ros/humble/setup.bash'], running: {count: 0, settings: {}}, installed: null});
  same([found.kind, found.setup, found.tone], [null, '/opt/ros/humble/setup.bash', 'warn'], 'only a setup file: the kind is not changed');
  found = page.rbFromDetect({setups: [], running: {count: 3, settings: {}}, installed: null});
  same([found.kind, found.tone], [null, 'warn'], 'programs without a setting and no setup file');
  includes(page.rbFromDetect({}).message, '没有在机器人上读到 ROS 2', 'nothing at all');

  // --- the places of a configuration
  const places = page.rbPlaces(EXAMPLE);
  same(places.map(p => p.name), ['出发点', '会议室', '二楼'], 'in the order they are written');
  same(places.every(p => p.routes.length >= 1 && p.routes.every(r => Array.isArray(r.steps))), true, 'each with its routes');
  same(page.rbPlaces('{"programs": {"places": {"门口": {"steps": [{"title": "走", "argv": ["a", "b c"], "timeout_s": 30}]}}}}'),
    [{name: '门口', region: '', routes: [{from: '', steps: [{title: '走', command: 'a b c', limit: 30}]}]}], 'a place with plain steps');
  for (const bad of ['', '{', '[]', '{"programs": 3}', '{"programs": {"places": []}}', 'null']) same(page.rbPlaces(bad), [], `no places in ${bad}`);
  includes(page.rbPreview('', null), '没有本体配置', 'the preview of nothing');
  const preview = page.rbPreview(EXAMPLE, {capabilities: ['a', 'b'], places: ['出发点', '会议室', '二楼'], explore: true});
  includes(preview, '认识 3 个地点', 'the preview says what the robot will know');
  includes(preview, '无图自由走', 'and that it can wander');
  includes(preview, '坐标和地图在机器人自己的程序里', 'and where the coordinates are');
  excludes(page.rbPreview('{"programs": {"places": {"<script>": {"steps": [{"title": "<script>", "argv": ["<script>"]}]}}}}', null), '<script>', 'names are escaped');
  includes(page.rbPreview('{"name": "x"}', {capabilities: ['a'], places: [], explore: false}), '不认识任何地点', 'a configuration without places');

  // --- the card
  let card = page.card(robots.plain);
  includes(card, 'data-action="engine-robot-body" data-id="r-plain"', 'an SSH robot gets 本体设置');
  excludes(card, 'rb-line', 'a simulated body has no line');
  card = page.card(robots.real);
  includes(card, '真实本体（ROS 2）· 机器人上用的就是这份', 'a real body says how it relates to the robot');
  includes(page.card(robots.changed), '还没有放到机器人上', 'a changed configuration is said');
  for (const robot of [robots.typed, robots.sim]) excludes(page.card(robot), 'engine-robot-body', `${robot.name}: no SSH connection, no button`);
  for (const state of Object.keys(page.RB_STATE)) same(page.rbBody({body: real(state)}).state, state, `state ${state} is known`);

  // --- actions and the form
  same(await page.action('engine-other', 'x'), ['base', 'engine-other', 'x'], 'other actions go on');
  await page.action('engine-robot-body', 'r-real');
  const [title, , fields, kind, , attrs] = forms.at(-1);
  same([title, kind, attrs], ['本体设置', 'engine-robot-body', 'data-id="r-real"'], 'the form');
  includes(fields, 'value="ros2" checked', 'opens on the saved kind');
  includes(fields, 'name="rmw_file" value="/opt/z/session.json5"', 'with the file of its RMW');
  includes(fields, 'engine-robot-body-detect', 'and the read from the robot');
  includes(fields, '不会动机器人', 'and says that saving reaches no robot');
  await assert.rejects(page.action('engine-robot-body', 'r-typed'), /SSH/); checks += 1;

  answer = {'robots/body/save': {body: real('unset'), declares: {places: ['会议室', '二楼'], capabilities: [], explore: false}}};
  await page.submit('engine-robot-body', {kind: 'ros2', ROS_DOMAIN_ID: '24', config_text: EXAMPLE}, {dataset: {id: 'r-plain'}});
  same(calls.at(-1), ['robots/body/save', {project_id: 'project-1', robot_id: 'r-plain', kind: 'ros2', setup: '/opt/ros/humble/setup.bash',
    settings: {ROS_DOMAIN_ID: '24'}, config_text: EXAMPLE}], 'a save sends the body');
  includes(dialogs.at(-1)[2], 'engine-runtime-install', 'a body never installed: install next');
  includes(dialogs.at(-1)[2], '会议室、二楼', 'with the places it will know');
  answer = {'robots/body/save': {body: real('config_changed'), declares: null}};
  await page.submit('engine-robot-body', {kind: 'ros2'}, {dataset: {id: 'r-real'}});
  includes(dialogs.at(-1)[2], 'engine-robot-body-apply', 'a changed configuration: apply next');
  includes(dialogs.at(-1)[2], '正在运行的技能会被停掉', 'and what the restart does');
  answer = {'robots/body/save': {body: real('reinstall'), declares: null}};
  await page.submit('engine-robot-body', {kind: 'ros2'}, {dataset: {id: 'r-real'}});
  includes(dialogs.at(-1)[2], '删除后重新安装', 'changed ROS settings: a new install');
  excludes(dialogs.at(-1)[2], 'engine-robot-body-apply', 'and no apply');
  answer = {'robots/body/save': {body: real('current'), declares: null}};
  await page.submit('engine-robot-body', {kind: 'ros2'}, {dataset: {id: 'r-real'}});
  excludes(dialogs.at(-1)[2], 'data-action="engine-r', 'nothing to do when the robot has it');
  same(await page.submit('other', {}, {}), ['base', 'other'], 'other forms go on');
  const before = calls.length;
  await assert.rejects(page.submit('engine-robot-body', {kind: 'ros2', ROS_DOMAIN_ID: '999'}, {dataset: {id: 'r-plain'}}), /0–232/); checks += 1;
  same(calls.length, before, 'a body that is filled in wrongly is not sent');

  // --- the install dialog
  const applying = {robot_id: 'r-real', mode: 'body', state: 'running', stage: 'restart', stage_label: '重新启动', stages: []};
  same(page.riSummary(applying), '正在把本体配置放到机器人上：重新启动', 'the card line of an apply');
  same(page.riSummary({...applying, state: 'failed', failure: {stage_label: '重新启动'}}), '本体配置没有放上去：停在「重新启动」', 'a failed one');
  same(page.riSummary({mode: 'install', state: 'running', stage: 'check', stage_label: '检查设备'}), '正在安装：检查设备', 'an install is said as before');
  includes(page.riResult({...applying, state: 'succeeded'}), '本体配置已经放到机器人上', 'the result of an apply');
  includes(page.riActions({...applying, state: 'failed'}, 'r-real'), 'engine-robot-body-apply', 'a failed apply can be tried again');
  excludes(page.riActions({...applying, state: 'failed'}, 'r-real'), '重新安装', 'and does not offer an install');
  includes(page.riActions({mode: 'install', state: 'failed'}, 'r-real'), '重新安装', 'a failed install still does');
  const other = page.riFound({root: 'installed', version: '0.4.0', running: true, running_port: 8790, console_package: 'p', adapter: 'sim_quadruped', other_body: true});
  includes(other, '它是为仿真本体装的', 'an install for the other body is said');
  includes(other, '删除后重新安装', 'with the only way to change it');
  excludes(page.riFound({root: 'installed', version: '0.4.0', running: true, console_package: 'p', adapter: 'ros2', other_body: false}), 'rb-other', 'the same body: nothing more');
  same(JSON.parse(page.RB_EXAMPLE).programs.places['会议室'].steps.length, 1, 'the example is JSON');

  console.log(`robot_body_logic: ${checks} checks passed`);
})().catch(error => { console.error(error); process.exit(1); });
