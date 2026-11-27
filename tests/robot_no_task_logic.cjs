// Pure-logic checks of issue #56: a robot is added and looked after with no task (web/robot_no_task.js),
// and with no task open the lists of the page are those of the robots' home (web/app.js, web/hardware.js).
// The server's side is tests/test_robot_no_task.py, the pages tests/robot_no_task_browser.cjs.
//   node tests/robot_no_task_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual ?? null)), expected, what); checks += 1; };
const ids = list => list.map(x => x.id);
const read = file => readFileSync(file, 'utf8');

const sandbox = {};
vm.createContext(sandbox);
const page = vm.runInContext(read('web/robot_no_task.js') + '\n;({robotNoTaskNeeded, robotNoTaskNext})', sandbox);

// ---- what goes on with no task
for (const type of ['engine-ssh', 'engine-robots-add', 'engine-discover', 'engine-models', 'engine-own-model', 'engine-session',
                    'engine-ssh-edit', 'engine-robot-remove', 'engine-runtime-install', 'engine-runtime-autostart', 'engine-robot-body',
                    'engine-body-type', 'hardware-open', 'hardware-scan', 'hardware-repair', 'robot-add', 'robot-config', 'robot-connect',
                    'detail-robots', 'skillpkg-open', 'simlive-open', 'journey-connect']) {
  ok(page.robotNoTaskNeeded(type), `${type} needs no task`);
}
for (const type of ['engine-work', 'engine-flow-start', 'engine-ext-skill', 'journey-prepare', 'journey-start', 'project-add',
                    'application-add', 'model-add', 'dataset-add', 'scene-add', 'operate-start', 'plan-choose', 'robot', '', undefined, null, 7]) {
  ok(!page.robotNoTaskNeeded(type), `${String(type)} still asks for a task`);
}

// ---- the task the page goes on in after a robot was added
const tasks = [{id: 'a'}, {id: 'b'}];
same(page.robotNoTaskNext('b', 'a', tasks), 'b', 'the task the server names');
same(page.robotNoTaskNext('', 'a', tasks), 'a', 'the open task when the server names none');
same(page.robotNoTaskNext('', '', []), '', 'none when the account has no task');
same(page.robotNoTaskNext('project-robot-home', '', []), '', 'the robots\' home is not a task to open');
same(page.robotNoTaskNext(undefined, undefined, undefined), '', 'nothing known: no task');

// ---- the lists of the page with no task open: the lines as they are in the files
const lines = (file, starts) => read(file).split('\n').filter(line => starts.some(s => line.startsWith(s))).join('\n');
const appLines = lines('web/app.js', ['const rows=', 'const ROBOT_HOME_PRIVATE=', 'function robotShared(']);
const hwLines = lines('web/hardware.js', ['const hwList=']);
same([appLines.split('\n').length, hwLines.split('\n').length], [3, 1], 'the lines are where they are expected');
const HOME = 'project-robot-home';
const state = {
  robot_home: HOME,
  robots: [{id: 'r1', project_id: HOME}],
  agent_models: [{id: 'm-home', project_id: HOME}, {id: 'm-a', project_id: 'a'}],
  agent_skills: [{id: 'k-home', project_id: HOME}, {id: 'k-a', project_id: 'a'}],
  agent_sessions: [{id: 's-home', project_id: HOME}, {id: 's-a', project_id: 'a'}],
  hardware_scans: [{id: 'h1', project_id: HOME}],
  jobs: [{id: 'j-a', project_id: 'a'}],
};
const lists = {state, projectId: ''};
vm.createContext(lists);
vm.runInContext(appLines.replace('const rows=', 'var rows=') + '\n' + hwLines.replace('const hwList=', 'var hwList='), lists);
same(ids(lists.rows('robots')), ['r1'], 'no task: the robots of the account');
same(ids(lists.rows('agent_models')), ['m-home'], 'no task: the models kept with the robots, so a robot can be investigated');
same(ids(lists.rows('agent_skills')), ['k-home'], 'no task: the assistant\'s tools kept with the robots');
same(ids(lists.rows('agent_sessions')), ['s-home'], 'no task: the robots\' own sessions');
same(ids(lists.hwList('hardware_scans')), ['h1'], 'no task: a robot\'s hardware scans');
same(ids(lists.rows('jobs')), [], 'no task: nothing of a task');
lists.projectId = 'a';
same(ids(lists.rows('agent_models')), ['m-a'], 'a task open: its own models, not the home\'s (as before)');
same(ids(lists.rows('agent_sessions')), ['s-home', 's-a'], 'a task open: the robots\' sessions next to its own (as before)');
lists.projectId = '';
lists.state = {...state, robot_home: undefined};
same(ids(lists.rows('agent_models')), [], 'a state without the home (an older backend) lists nothing with no task');

// ---- the handlers ask before they open the form of a new task
const asks = (file, start) => read(file).split('\n').filter(line => line.includes(start));
for (const [file, fn] of [['web/engineering.js', 'engineAction'], ['web/hardware.js', 'hardwareAction'], ['web/journey.js', 'journeyAction']]) {
  const gate = asks(file, "if(!project()&&!robotNoTaskNeeded(type))return action('project-add');");
  same(gate.length, 1, `${fn} opens the form of a new task only for what needs one`);
}
ok(!/if\(!project\(\)\)return action\('project-add'\)/.test(read('web/engineering.js') + read('web/hardware.js') + read('web/journey.js')),
   'no handler of a robot opens the form of a new task unasked');
same(asks('web/app.js', "robotNoTaskNeeded(type)))return toast('请先创建或选择一个项目')").length, 1, 'the page\'s own handler lets a robot\'s actions through');
// The modules that add to a robot's card: none of them stands aside because no task is open.
for (const file of ['safety_limits', 'robot_remove', 'runtime_install', 'runtime_autostart', 'robot_body', 'ssh_edit', 'cloud_skills',
                    'robots_page', 'robot_dialogs', 'robot_model', 'live_status', 'connection_verify', 'heartbeat', 'body_types', 'skill_run',
                    'device_profile', 'motion_drivers', 'runtime_link', 'investigation']) {
  ok(!/[!]project\(\)|&& ?project\(\)/.test(read(`web/${file}.js`)), `web/${file}.js does not wait for a task`);
}
// Every other script of the page: only those whose pages are a task's ask whether a task is open.
const TASK_PAGES = ['app', 'journey', 'planner', 'workflows', 'ux02', 'ux03', 'task_workbench', 'engineering', 'hardware', 'extensions',
                    'task_robot', 'task_apps', 'task_skills', 'delivery',
                    'app_create'];        // issue #68: the form for a new application starts on the open task (a task is required)
const asking = require('node:fs').readdirSync('web').filter(f => f.endsWith('.js') && /[^a-zA-Z.]project\(\)/.test(read('web/' + f)))
  .map(f => f.slice(0, -3)).filter(f => !TASK_PAGES.includes(f));
same(asking, [], 'no script of a robot asks whether a task is open');
// A simulated robot: the page does not take the server's answer for a task that is not there.
for (const file of ['web/sim.js', 'web/sim_profiles.js']) {
  ok(read(file).includes('projectId = robotNoTaskNext(robot.project_id, projectId, state.projects);') &&
     !/projectId = robot\.project_id;/.test(read(file)), `${file} keeps the open task when none is named`);
}
ok(read('web/index.html').includes('<script src="/robot_no_task.js" defer></script>'), 'the page loads the script');

console.log(`robot_no_task_logic: ${checks} checks passed`);
