// Pure-logic checks of web/robot_bundle.js (issue #43): what the 程序和场景数据 section accepts as a
// directory before anything is sent, what it says while a job runs and when it ends, and that file
// names and directories from a robot or the cloud never reach the page as markup. The backend rules
// are in brainos/robot_bundle.py and robot_bundle_device.py, checked in tests/test_robot_bundle*.py.
//   node tests/robot_bundle_logic.cjs
const {load, checker, HOSTILE, escaped} = require('./cloud_skills_logic_helper.cjs');

const {page} = load(['web/cloud_skills.js', 'web/robot_bundle.js'], ['bundleSize', 'bundleDirectoryProblem', 'bundleProgress', 'bundleResultText',
  'bundleWarnings', 'bundleHistoryText', 'bundleJobHtml', 'bundleListHtml', 'bundleBody', 'bundleSection', 'BUNDLE_NO_SSH',
  'bundlePollFailed', 'bundleOrigin', 'bundleForeignWarning', 'BUNDLE_POLL_TRIES']);
const counted = checker(), {check, includes, excludes} = counted;
const MB = 1024 * 1024;

const job = (changes = {}) => ({id: 'bundle-job-1', kind: 'collect', robot_id: 'robot-1', subject: '~/walk_programs', status: 'running',
  phase: 'packing', done: 0, total: 0, error: null, code: null, note: null, result: null, ...changes});
const collected = (changes = {}) => ({filename: 'walk_programs-20261124-010203.tar.gz', files: 12, bytes: 3 * MB, sha256: 'a'.repeat(64),
  outside_links: 0, changed_while_packing: false, already_in_library: false, directory: '~/walk_programs', ...changes});
const installed = (changes = {}) => ({filename: 'walk_programs-20261124-010203.tar.gz', device_dir: '~/neuxmind-bundles/walk_programs/0123456789abcdef',
  current: '~/neuxmind-bundles/walk_programs/current', already_unpacked: false, note: '没有改动机器人上原来的目录', ...changes});
const view = (changes = {}) => ({robot_id: 'robot-1', ssh: true, logged_in: true, store: '~/neuxmind-bundles', limit_bytes: 64 * MB,
  bundles: [], bundles_error: '', jobs: [], history: [], skipped: [], ...changes});

// ---------------------------------------------------------------- the directory a person types
for (const good of ['~/walk_programs', ' ~/walk_programs/ ', '/opt/robot/nav', '~/程序 和 场景/v2', "~/it's $(x)"]) {
  check(page.bundleDirectoryProblem(good), '', `accepted: ${good}`);
}
includes(page.bundleDirectoryProblem(''), '请填写', 'empty');
includes(page.bundleDirectoryProblem('walk_programs'), '~/ 开头', 'a relative path');
includes(page.bundleDirectoryProblem('~'), '主目录', 'the home directory itself');
includes(page.bundleDirectoryProblem('/'), '请填写', 'the root: nothing is left of it');
includes(page.bundleDirectoryProblem('~/a/../b'), '..', 'climbing');
includes(page.bundleDirectoryProblem('~/a//b'), '..', 'an empty part');
includes(page.bundleDirectoryProblem('~/a\nb'), '看不见的字符', 'a line break');
includes(page.bundleDirectoryProblem('~/a' + String.fromCharCode(0x202e) + 'b'), '看不见的字符', 'a mark that reverses the text');
includes(page.bundleDirectoryProblem('~/' + 'x'.repeat(400)), '太长', 'too long');

// ---------------------------------------------------------------- sizes
check(page.bundleSize(0), '0 B', 'nothing');
check(page.bundleSize(1536), '1.5 KB', 'kilobytes');
check(page.bundleSize(64 * MB), '64.0 MB', 'megabytes');

// ---------------------------------------------------------------- while it runs
let p = page.bundleProgress(job());
check([p.percent, p.text], [0, '正在机器人上打包…'], 'packing has no bytes to count');
p = page.bundleProgress(job({phase: 'fetching', done: MB, total: 4 * MB}));
check([p.percent, p.text], [25, '正在从机器人取回打好的包 25%（1.0 MB / 4.0 MB）'], 'reading back from the robot');
p = page.bundleProgress(job({kind: 'install', phase: 'fetching', done: MB, total: 2 * MB}));
includes(p.text, '正在从云平台取这一份 50%', 'the same phase of an install comes from the cloud');
includes(page.bundleProgress(job({phase: 'uploading', done: 3 * MB, total: 4 * MB})).text, '正在送到云平台 75%', 'uploading');
includes(page.bundleProgress(job({kind: 'install', phase: 'sending', done: 5, total: 10})).text, '正在送到机器人 50%', 'sending');
check(page.bundleProgress(job({kind: 'install', phase: 'unpacking', done: 10, total: 10})).text, '正在机器人上解开…', 'unpacking is not counted');
check(page.bundleProgress(job({phase: 'something_new'})).text, '进行中…', 'a phase this page does not know');
check(page.bundleProgress(job({phase: 'uploading', done: 9, total: 4})).percent, 100, 'never over 100');

// ---------------------------------------------------------------- when it ended
p = page.bundleProgress(job({status: 'done', result: collected()}));
check([p.tone, p.text], ['good', '已收进云端：walk_programs-20261124-010203.tar.gz（12 个文件，3.0 MB）。'], 'collected');
includes(page.bundleResultText(job({status: 'done', result: collected({already_in_library: true})})), '没有重复保存', 'the same content again');
p = page.bundleProgress(job({kind: 'install', status: 'done', result: installed()}));
includes(p.text, '已装到机器人的 ~/neuxmind-bundles/walk_programs/0123456789abcdef', 'installed: where');
includes(p.text, 'current 已指向它', 'and that current points at it');
includes(page.bundleResultText(job({kind: 'install', status: 'done', result: installed({already_unpacked: true})})), '没有重复解', 'unpacked before');
p = page.bundleProgress(job({status: 'stopped', note: '已按你的要求停止；机器人上原来的目录没有动'}));
check([p.tone, p.text], ['warn', '已按你的要求停止；机器人上原来的目录没有动'], 'stopped');
p = page.bundleProgress(job({status: 'failed', error: '机器人上没有这个目录，或者它不是目录'}));
check([p.tone, p.text], ['bad', '没有完成：机器人上没有这个目录，或者它不是目录'], 'failed, with the reason');

check(page.bundleWarnings(job()), [], 'nothing to warn about while it runs');
check(page.bundleWarnings(job({status: 'done', result: collected()})), [], 'a clean collection');
let warnings = page.bundleWarnings(job({status: 'done', result: collected({outside_links: 2, changed_while_packing: true})}));
check(warnings.length, 2, 'two things to know');
includes(warnings[0], '2 个链接指向目录之外', 'links that lead out');
includes(warnings[0], '装回机器人时会被拒绝', 'and what that means');
includes(warnings[1], '正在被改写', 'files that changed while packing');
warnings = page.bundleWarnings(job({kind: 'install', status: 'done', result: installed()}));
check(warnings, ['没有改动机器人上原来的目录'], 'after an install: what was not changed');

// ---------------------------------------------------------------- what stayed behind, what was set aside
warnings = page.bundleWarnings(job({status: 'done', result: collected({
  guarded: {count: 23, names: ['./id_ecdsa', './.netrc', HOSTILE]}, skipped: {count: 2, names: ['./runs', './old.bak']}})}));
check(warnings.length, 2, 'both kinds of left-out content are told');
includes(warnings[0], '23 个像登录凭据的文件或目录没有收', 'credentials: how many');
includes(warnings[0], './id_ecdsa、./.netrc', 'and the first names');
includes(warnings[0], '等 23 个', 'more than are named: said');
includes(warnings[1], '按约定跳过了 2 项', 'the routine ones');
includes(warnings[1], './runs、./old.bak', 'with their names');
check(page.bundleWarnings(job({status: 'done', result: collected({guarded: {count: 0, names: []}, skipped: {count: 0, names: []}})})), [], 'nothing left out: nothing said');
warnings = page.bundleWarnings(job({status: 'done', result: collected({cancelled_other_upload: true})}));
includes(warnings[0], '没传完的上传', 'a stale upload of the other kind that was given up is told');
warnings = page.bundleWarnings(job({kind: 'install', status: 'done', result: installed({set_aside: '~/neuxmind-bundles/walk_programs/0123456789abcdef.changed-20261124-010203'})}));
includes(warnings[0], '被改动过', 'a directory that was changed on the robot');
includes(warnings[0], '0123456789abcdef.changed-20261124-010203', 'and where the changed one is now');
check(escaped(page.bundleJobHtml(job({status: 'done', result: collected({guarded: {count: 1, names: [HOSTILE]}})}))), true, 'names from the robot are escaped');

// ---------------------------------------------------------------- a status question that fails is asked again
check(page.BUNDLE_POLL_TRIES >= 3, true, 'several tries');
let failedOnce = page.bundlePollFailed(job(), 1, 'fetch failed');
check([failedOnce.status, failedOnce.error], ['running', null], 'one failure: still running, asked again');
let failedOften = page.bundlePollFailed(job(), page.BUNDLE_POLL_TRIES, 'fetch failed');
check(failedOften.status, 'unknown', 'after several: not "failed", the job may well be running');
includes(page.bundleProgress(failedOften).text, '问不到进度', 'and it says what is not known');
includes(page.bundleProgress(failedOften).text, '刷新', 'and how to look again');

// ---------------------------------------------------------------- where a bundle came from
check(page.bundleOrigin({collected_here: true, directory: '~/walk_programs'}), '从这台机器人的 ~/walk_programs 收进来', 'collected by this console');
includes(page.bundleOrigin({collected_here: false, created_by: 'usr_42', description: '来自别处'}), '不是这台电脑上的控制台收的', 'someone else put it there');
includes(page.bundleOrigin({collected_here: false, created_by: 'usr_42'}), 'usr_42', 'with who, as the library records it');
check(page.bundleForeignWarning({collected_here: true}), '', 'our own: no warning');
includes(page.bundleForeignWarning({collected_here: false, created_by: 'usr_42'}), '请确认它的来源', 'a foreign one: the install confirmation says so');
check(escaped(page.bundleOrigin({collected_here: false, created_by: HOSTILE}) === undefined ? '' : `<p>${require('./cloud_skills_logic_helper.cjs').h(page.bundleOrigin({collected_here: false, created_by: HOSTILE}))}</p>`), true, 'who is shown as text');

// ---------------------------------------------------------------- drawing
check(page.bundleJobHtml(null), '', 'no job: nothing');
let html = page.bundleJobHtml(job({phase: 'fetching', done: MB, total: 4 * MB}));
includes(html, 'role="progressbar"', 'a running job has a bar');
includes(html, 'aria-valuenow="25"', 'with its value');
includes(html, 'data-bundle-action="stop"', 'and a stop button');
html = page.bundleJobHtml(job({status: 'done', result: collected()}));
excludes(html, 'role="progressbar"', 'an ended job has no bar');
excludes(html, 'data-bundle-action="stop"', 'and nothing to stop');
html = page.bundleJobHtml(job({subject: HOSTILE, status: 'failed', error: HOSTILE}));
check(escaped(html), true, 'a directory and an error are escaped');
html = page.bundleJobHtml(job({kind: 'install', subject: HOSTILE, status: 'done', result: installed({device_dir: HOSTILE, current: HOSTILE, note: HOSTILE})}));
check(escaped(html), true, 'what a robot reports is escaped');

includes(page.bundleListHtml(view(), false), '云端还没有这台机器人的程序和场景数据', 'an empty list');
const material = {id: 'mat_1', filename: 'walk_programs-20261124-010203.tar.gz', size: 3 * MB, sha256: 'b'.repeat(64), created_at: 1795000000,
  collected_here: true, directory: '~/walk_programs', created_by: 'usr_1'};
html = page.bundleListHtml(view({bundles: [material]}), false);
includes(html, 'data-bundle-action="install"', 'each can be installed');
excludes(html, 'disabled', 'when nothing runs');
includes(page.bundleListHtml(view({bundles: [material]}), true), 'disabled', 'not while a job runs');
html = page.bundleListHtml(view({bundles: [{...material, id: HOSTILE, filename: HOSTILE, sha256: HOSTILE}]}), false);
check(escaped(html), true, 'a file name from the cloud is escaped');
includes(page.bundleListHtml(view({bundles: [material]}), false), '从这台机器人的 ~/walk_programs 收进来', 'the list says which directory it was');
html = page.bundleListHtml(view({bundles: [{...material, collected_here: false, directory: '', created_by: HOSTILE}]}), false);
includes(html, '不是这台电脑上的控制台收的', 'and which ones this console did not collect');
check(escaped(html), true, 'the uploader is escaped');
check(escaped(page.bundleListHtml(view({bundles_error: HOSTILE}), false)), true, 'an error of the cloud is shown as text');

check(page.bundleHistoryText({kind: 'collect', directory: '~/walk_programs', filename: 'a.tar.gz'}), '从 ~/walk_programs 收进云端：a.tar.gz', 'history: collected');
check(page.bundleHistoryText({kind: 'install', device_dir: '~/neuxmind-bundles/a/0123', filename: 'a.tar.gz'}), '把 a.tar.gz 装到 ~/neuxmind-bundles/a/0123', 'history: installed');

includes(page.bundleBody(null, null), '正在读取', 'before the first answer');
html = page.bundleBody(view({ssh: false}), null);
includes(html, page.BUNDLE_NO_SSH, 'no SSH connection: said plainly');
excludes(html, 'data-bundle-action="collect"', 'and nothing to start');
html = page.bundleBody(view({logged_in: false}), null);
includes(html, '请先登录云平台账户', 'signed out of the cloud');
excludes(html, 'data-bundle-action="collect"', 'and nothing to start either');
html = page.bundleBody(view({history: [{kind: 'collect', at: '2026-11-24T01:02:03+00:00', directory: HOSTILE, filename: HOSTILE}]}), null);
includes(html, 'data-bundle-action="collect"', 'ready: the way in');
includes(html, '做过的操作（1）', 'what was done before');
check(escaped(html), true, 'history is escaped');
check(/data-bundle-action="collect"\s+disabled/.test(page.bundleBody(view(), job())), true, 'one at a time: the button waits');
check(escaped(page.bundleSection({id: HOSTILE})), true, 'the robot id is escaped in the section');

console.log(`robot bundle logic checks passed: ${counted.checks}`);
