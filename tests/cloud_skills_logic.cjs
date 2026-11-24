// Pure-logic checks of web/cloud_skills.js (issue #43): what the 云端技能 page says about a skill, an
// example and a preview, what the folder form accepts before anything is sent, which robots a release
// can go to, and that text from the cloud never reaches the page as markup. The backend rules are in
// brainos/cloud_skills.py, checked in tests/test_cloud_skills.py.
//   node tests/cloud_skills_logic.cjs
const {load, checker, HOSTILE, escaped} = require('./cloud_skills_logic_helper.cjs');

const {page, box} = load(['web/cloud_skills.js'], ['cloudSkillSize', 'cloudSkillWhen', 'cloudSkillExampleState', 'cloudSkillPathProblem',
  'cloudSkillRobots', 'cloudSkillCard', 'cloudSkillList', 'cloudSkillExamples', 'cloudSkillPublishBody', 'cloudSkillReleaseRow',
  'cloudSkillsPage', 'cloudSkills', 'CLOUD_SKILLS_ROUTE', 'CLOUD_SKILLS_PUBLISH_STATES']);
const counted = checker(), {check, includes, excludes} = counted;

const release = (changes = {}) => ({skill_id: 'patrol', version: '0.1.0', title: '巡逻', description: '走一圈', summary: '走一圈',
  digest: 'a'.repeat(64), notes: '', created_by: 'dev@example.test', created_at: 1795000000, size: 2048, timeout_s: 600, motion: true,
  requires: [{id: 'base.navigate_to', title: '导航到目标点'}], optional: [], parameters: [], files: [{path: 'skill.py', size: 2048}], ...changes});
const row = (changes = {}) => ({id: 'patrol', title: '巡逻', summary: '走一圈', latest: release(), versions: ['0.1.0'], ...changes});

// ---------------------------------------------------------------- the page registers itself
check(page.CLOUD_SKILLS_ROUTE, 'cloud-skills', 'the route');
check(box.navigation.at(-1), ['cloud-skills', '云端技能'], 'one more entry at the end of the navigation');
check(typeof box.views['cloud-skills'], 'function', 'its view');
check(typeof box.icons['cloud-skills'], 'string', 'its icon');

// ---------------------------------------------------------------- sizes, times
check(page.cloudSkillSize(0), '0 B', 'nothing');
check(page.cloudSkillSize(2048), '2.0 KB', 'kilobytes');
check(page.cloudSkillSize(3 * 1024 * 1024), '3.0 MB', 'megabytes');
check(page.cloudSkillSize('x'), '0 B', 'not a number');
check(page.cloudSkillWhen(null), '—', 'no time');
check(page.cloudSkillWhen('not a date'), '—', 'text that is not a time');
includes(page.cloudSkillWhen(1795000000), '2026', 'seconds since 1970');
includes(page.cloudSkillWhen('2026-11-24T02:00:00+00:00'), '2026', 'ISO text');

// ---------------------------------------------------------------- the folder form
check(page.cloudSkillPathProblem(''), '请填写技能文件夹的完整路径。', 'empty');
check(page.cloudSkillPathProblem('   '), '请填写技能文件夹的完整路径。', 'blank');
includes(page.cloudSkillPathProblem('skills/patrol'), '完整路径', 'a relative path');
check(page.cloudSkillPathProblem(' /Users/you/skills/patrol '), '', 'a full path');

// ---------------------------------------------------------------- which robots
const robots = [{id: 'a', project_id: 'project-1', status: 'connected'}, {id: 'b', project_id: 'project-1', status: 'error'},
  {id: 'c', project_id: 'project-2', status: 'connected'}];
check(page.cloudSkillRobots(robots, 'project-1').map(r => r.id), ['a'], 'connected robots of this project only');
check(page.cloudSkillRobots(undefined, 'project-1'), [], 'no robots');

// ---------------------------------------------------------------- examples
check(page.cloudSkillExampleState({valid: false, error: 'x'}), ['示例本身有问题', 'bad'], 'an example that does not validate');
check(page.cloudSkillExampleState({valid: true, published: false}), ['还没有发布', ''], 'not in the library');
check(page.cloudSkillExampleState({valid: true, published: true, same_content: true}), ['已在云端', 'good'], 'there with the same content');
check(page.cloudSkillExampleState({valid: true, published: true, same_content: null}), ['已在云端', 'good'], 'there, an older version');
check(page.cloudSkillExampleState({valid: true, published: true, same_content: false}), ['云端的同一版本内容不同', 'warn'], 'other content');

// ---------------------------------------------------------------- a card
let html = page.cloudSkillCard(row({versions: ['0.2.0', '0.1.0'], latest: release({version: '0.2.0'})}));
includes(html, '最新版本 0.2.0', 'the newest version');
includes(html, '另有 1 个版本', 'how many more');
includes(html, '会让机器人运动', 'a skill that moves the robot says so');
includes(html, 'data-cloudskill-action="install"', 'install from the card');
includes(html, 'data-id="patrol|0.2.0"', 'the release the button installs');
html = page.cloudSkillCard(row({latest: release({motion: false})}));
includes(html, '不会让机器人运动', 'a skill that does not move');
excludes(html, '另有', 'one version: nothing more to say');
html = page.cloudSkillCard(row({id: 'x', title: HOSTILE, summary: HOSTILE,
  latest: release({requires: [{id: HOSTILE, title: HOSTILE}], version: HOSTILE})}));
check(escaped(html), true, 'text from the cloud is escaped in a card');

// ---------------------------------------------------------------- the list
page.cloudSkills.loaded = false;
includes(page.cloudSkillList(), '正在读取云端技能库', 'before the first answer');
Object.assign(page.cloudSkills, {loaded: true, skills: [], error: ''});
includes(page.cloudSkillList(), '云端技能库还是空的', 'an empty library');
includes(page.cloudSkillList(), 'data-cloudskill-action="examples"', 'the way to fill it');
Object.assign(page.cloudSkills, {error: HOSTILE});
check(escaped(page.cloudSkillList()), true, 'an error is shown as text');
includes(page.cloudSkillList(), 'data-cloudskill-action="refresh"', 'with a way to try again');
Object.assign(page.cloudSkills, {error: '', skills: [row()]});
includes(page.cloudSkillList(), 'data-cloudskill="patrol"', 'one card per skill');

// ---------------------------------------------------------------- the examples panel
Object.assign(page.cloudSkills, {examples: [{example: 'patrol', valid: true, skill_id: 'patrol', version: '0.1.0', title: '巡逻', summary: '走一圈', published: true, same_content: true},
  {example: 'wave', valid: true, skill_id: 'wave', version: '0.1.0', title: '挥手', summary: '', published: false, same_content: null},
  {example: 'broken', valid: false, error: HOSTILE}]});
html = page.cloudSkillExamples();
includes(html, '1 个还没有发布', 'how many are missing');
check(escaped(html), true, 'an example\'s error is shown as text');
page.cloudSkills.examples = [];
check(page.cloudSkillExamples(), '', 'no examples: no panel');

// ---------------------------------------------------------------- the publish preview
const preview = (changes = {}) => ({path: '/Users/you/skills/patrol', example: null, digest: 'b'.repeat(64), state: 'new', can_publish: true,
  motion: true, total_bytes: 2048, files: [{path: 'skill.py', bytes: 2048}],
  skill: {id: 'patrol', version: '0.1.0', title: '巡逻', description: '走一圈', summary: '走一圈', requires: [{id: 'base.navigate_to', title: '导航到目标点'}], optional: []}, ...changes});
html = page.cloudSkillPublishBody(preview());
includes(html, '可以发布', 'a new version');
includes(html, 'data-cloudskill-state="new"', 'the state for the test to read');
includes(html, '/Users/you/skills/patrol', 'where it came from');
html = page.cloudSkillPublishBody(preview({state: 'conflict', can_publish: false}));
includes(html, '提高版本号', 'a conflict says what to do');
includes(html, 'plan-review blocked', 'and is drawn as blocked');
html = page.cloudSkillPublishBody(preview({state: 'same'}));
includes(html, '不需要再发布', 'the same content');
html = page.cloudSkillPublishBody(preview({path: HOSTILE, files: [{path: HOSTILE, bytes: 1}],
  skill: {id: HOSTILE, version: HOSTILE, title: HOSTILE, description: HOSTILE, summary: HOSTILE, requires: [{id: HOSTILE, title: HOSTILE}], optional: []}}));
check(escaped(html), true, 'a local skill.json is escaped too');
check(Object.keys(page.CLOUD_SKILLS_PUBLISH_STATES).sort(), ['conflict', 'new', 'same'], 'the three states the backend names');

// ---------------------------------------------------------------- one release in the versions dialog
html = page.cloudSkillReleaseRow(release({notes: '第一次发布'}));
includes(html, '版本 0.1.0', 'the version');
includes(html, '第一次发布', 'its notes');
includes(html, 'aaaaaaaaaaaa…', 'the digest, shortened');
includes(html, 'data-cloudskill-action="delete"', 'delete');
html = page.cloudSkillReleaseRow(release({notes: HOSTILE, created_by: HOSTILE, version: HOSTILE, digest: HOSTILE}));
check(escaped(html), true, 'notes and names from other people are escaped');

// ---------------------------------------------------------------- who published
const who = load(['web/cloud_skills.js'], ['cloudSkillPublisher']).page.cloudSkillPublisher;
check(who('user:42'), '', 'an account number says nothing: not shown');
check(who('key:7'), '经 API Key 发布', 'published with a key');
check(who('dev@example.test'), 'dev@example.test', 'a name is shown');
check(who(null), '', 'nothing');

// ---------------------------------------------------------------- the page itself
box.state.cloud_account = {logged_in: false};
html = page.cloudSkillsPage();
includes(html, '技能库保存在云平台', 'signed out: the sign-in notice');
excludes(html, 'data-cloudskill-action="publish"', 'and nothing to publish with');
box.state.cloud_account = {logged_in: true};
Object.assign(page.cloudSkills, {loaded: true, loadedFor: 'owner|1', skills: [row()], error: ''});
html = page.cloudSkillsPage();
includes(html, 'data-cloudskill-action="publish"', 'signed in: publish');
includes(html, 'id="cloud-skill-count">1<', 'the count');

console.log(`cloud skills logic checks passed: ${counted.checks}`);
