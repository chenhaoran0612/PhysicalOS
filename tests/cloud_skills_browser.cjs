// Issue #43 browser acceptance: the 云端技能 page and the two new sections of a robot's page.
//  * 云端技能: signed out of the cloud it says so; the app's own examples are published in one action; a
//    local folder is previewed, then published; the same content is not published twice and other
//    content under the same version is refused with what to do; a version is deleted; a release is
//    installed on a simulated robot (downloaded, verified, compatibility explained) and the flow goes
//    on in the run form, as after any install; a download that was tampered with never reaches the robot.
//  * 远程调用: one switch per installed skill; switching on asks first, and for a skill that moves the
//    robot the dialog says third parties can make it move without anyone confirming each time; an older
//    management layer gets a plain sentence; recent calls arrive without the section being redrawn.
//  * 程序和场景数据: a directory of the robot is collected into the cloud and installed back, each after
//    an explicit confirmation; the original directory is not touched.
// Fixture: tests/cloud_skills_browser_fixture.py (stand-ins for the cloud, an SSH robot and a management
// layer; the simulated robot is a real runtime). The robot's page is opened by its own function.
// Usage: node tests/cloud_skills_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const NAME = '云端技能验收';
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2]));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `cloud-skills-${engine.name()}-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog'), sub = page.locator('#sub-dialog');
  const fixture = async what => (await page.request.get(`${cfg.url}/fixture/${what}`)).json();
  const reload = () => page.evaluate(() => refresh(true));
  const formError = root => root.locator('.form-error').innerText();
  const result = {browser: engine.name()};

  await page.goto(cfg.url);
  await gateSignIn(page, {name: NAME, email: 'cloud-skills-ui@example.test', password: 'desk-' + crypto.randomBytes(8).toString('hex') + '-7'});

  // 1 · The page is in the navigation; without a cloud login it says where to sign in.
  await page.locator('#navigation a[href="#cloud-skills"]').click();
  await main.locator('h1', {hasText: '云端技能'}).waitFor();
  must((await main.innerText()).includes('请先登录云平台账户'), 'Signed out of the cloud, the page does not say to sign in');
  must(await main.locator('[data-cloudskill-action]').count() === 0, 'Something to publish with is offered without a cloud login');
  await shot('01-signed-out');

  // 2 · Signed in: an empty library with the way to fill it.
  await fixture('cloud-login');
  await reload();
  await main.locator('.empty', {hasText: '云端技能库还是空的'}).waitFor();
  await shot('02-empty');

  // 3 · One action publishes the app's own examples; the library then shows them.
  await main.locator('.empty [data-cloudskill-action="examples"]').click();
  await dialog.locator('.cloud-skill-example').first().waitFor();
  const published = await dialog.locator('.cloud-skill-example').count();
  must(published >= 6, `Expected the six bundled examples, saw ${published}`);
  must((await dialog.locator('.cloud-skill-example .status').allInnerTexts()).every(t => t.includes('已发布')), 'An example was not published');
  await shot('03-examples-published');
  await dialog.locator('.dialog-footer [data-action="close"]').click();
  await main.locator('[data-cloudskill="patrol"]').waitFor();
  must(await main.locator('.cloud-skill-card').count() === published, 'The library does not show one card per published skill');
  must((await main.locator('[data-cloudskill="patrol"]').innerText()).includes('会让机器人运动'), 'A skill that moves the robot does not say so');
  must((await main.locator('[data-cloudskill="status_report"]').innerText()).includes('不会让机器人运动'), 'A skill that does not move is not told apart');
  must((await fixture('state')).releases.length === published, 'The cloud does not hold the published examples');
  must((await main.locator('#cloud-skill-examples').innerText()).includes('已在云端'), 'The examples are not shown as published');
  await shot('04-library');
  result.examples = published;

  // 4 · A local folder: what is typed is checked in the dialog, then previewed, then published.
  const folder = (await fixture('folder')).path;
  await main.locator('.page-head [data-cloudskill-action="publish"]').click();
  const pathForm = dialog.locator('form[data-cloudskill-form="publish-preview"]');
  await pathForm.waitFor();
  await pathForm.locator('[type=submit]').click();
  must((await formError(dialog)).includes('请填写技能文件夹的完整路径'), 'An empty path is not refused in the dialog');
  await pathForm.locator('[name=path]').fill('skills/status_report');
  await pathForm.locator('[type=submit]').click();
  must((await formError(dialog)).includes('完整路径'), 'A relative path is not refused in the dialog');
  await pathForm.locator('[name=path]').fill(folder + '-missing');
  await pathForm.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('#dialog .form-error')?.textContent.includes('找不到'));
  await pathForm.locator('[name=path]').fill(folder);
  await pathForm.locator('[type=submit]').click();
  const publishForm = dialog.locator('form[data-cloudskill-form="publish"]');
  await publishForm.waitFor();
  must(await dialog.locator('[data-cloudskill-state="new"]').count() === 1, 'A new version is not shown as publishable');
  must((await dialog.innerText()).includes(cfg.markup), 'The title of the skill is not shown as text');
  must(await page.evaluate(() => window.__markupRan) === undefined, 'Markup in a skill title was run');
  await shot('05-publish-preview');
  await publishForm.locator('[name=notes]').fill('验收脚本发布的版本');
  await publishForm.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('[data-cloudskill="status_report"]')?.innerText.includes('最新版本 0.2.0'));
  must((await main.locator('[data-cloudskill="status_report"]').innerText()).includes('另有 1 个版本'), 'The earlier version is not counted');
  must(await page.evaluate(() => window.__markupRan) === undefined, 'Markup in a skill title was run on the card');

  // The same folder again: nothing to publish. Changed content under the same version: refused, with what to do.
  const preview = async () => {
    await main.locator('.page-head [data-cloudskill-action="publish"]').click();
    await pathForm.waitFor();
    await pathForm.locator('[name=path]').fill(folder);
    await pathForm.locator('[type=submit]').click();
    await dialog.locator('[data-cloudskill-state]').waitFor();
    return dialog.locator('[data-cloudskill-state]').getAttribute('data-cloudskill-state');
  };
  must(await preview() === 'same', 'The same content is not recognised');
  must(await dialog.locator('form[data-cloudskill-form="publish"]').count() === 0, 'The same content can be published again');
  await dialog.locator('.dialog-footer [data-action="close"]').click();
  await fixture('folder-change');
  must(await preview() === 'conflict', 'Other content under the same version is not refused');
  must((await dialog.innerText()).includes('提高版本号'), 'A conflict does not say what to do');
  await shot('06-publish-conflict');
  await dialog.locator('.dialog-footer [data-action="close"]').click();

  // 5 · Versions of one skill; one of them is deleted after a confirmation.
  await main.locator('[data-cloudskill="status_report"] [data-cloudskill-action="versions"]').click();
  await dialog.locator('[data-cloudskill-release="0.2.0"]').waitFor();
  must(await dialog.locator('.cloud-skill-release').count() === 2, 'The versions dialog does not list both versions');
  must((await dialog.locator('[data-cloudskill-release="0.2.0"]').innerText()).includes('验收脚本发布的版本'), 'The notes of a version are missing');
  await shot('07-versions');
  await dialog.locator('[data-cloudskill-release="0.2.0"] [data-cloudskill-action="delete"]').click();
  await dialog.locator('[data-cloudskill-action="delete-confirm"]').waitFor();
  must((await dialog.innerText()).includes('已经装到机器人上的技能不受影响'), 'The deletion does not say what it leaves alone');
  await dialog.locator('[data-cloudskill-action="delete-confirm"]').click();
  await dialog.locator('[data-cloudskill-release="0.1.0"]').waitFor();
  must(await dialog.locator('.cloud-skill-release').count() === 1, 'The deleted version is still listed');
  must(!(await fixture('state')).releases.includes('status_report@0.2.0'), 'The cloud still holds the deleted version');
  await dialog.locator('.dialog-footer [data-action="close"]').click();

  // 6 · Install on a simulated robot (a real runtime): download, verify, explain, install, then the run form.
  const sim = await page.evaluate(async () => { const robot = await api('sims', {kind: 'quadruped'}); await refresh(true); return robot; });
  await page.waitForFunction(id => find('robots', id)?.status === 'connected', sim.id);
  await main.locator('[data-cloudskill="wave"] [data-cloudskill-action="install"]').click();
  const chooseForm = dialog.locator('form[data-cloudskill-form="install-preview"]');
  await chooseForm.waitFor();
  must(await chooseForm.locator('select[name=release]').inputValue() === 'wave|0.1.0', 'The release of the card is not preselected');
  await chooseForm.locator('[type=submit]').click();
  await dialog.locator('.plan-review.blocked').waitFor();
  must((await dialog.innerText()).includes('不兼容：不能安装'), 'An incompatible release is not explained');
  must(await dialog.locator('form[data-cloudskill-form="install"]').count() === 0, 'An incompatible release can be installed');
  await shot('08-install-incompatible');
  await dialog.locator('.dialog-footer [data-cloudskill-action="install"]').click();
  await chooseForm.waitFor();

  await fixture('tamper?on=1');
  await chooseForm.locator('select[name=release]').selectOption('patrol|0.1.0');
  await chooseForm.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('#dialog .form-error')?.textContent.trim());
  must((await formError(dialog)).includes('摘要不一致'), 'A download that was tampered with is not refused in the dialog: ' + await formError(dialog));
  await shot('09-install-tampered');
  await fixture('tamper?on=0');
  await chooseForm.locator('[type=submit]').click();
  const installForm = dialog.locator('form[data-cloudskill-form="install"]');
  await installForm.waitFor();
  const previewText = await dialog.innerText();
  must(previewText.includes('兼容：可以安装'), 'A compatible release is not shown as installable');
  must(previewText.includes('云端技能库') || await dialog.locator('details.page-purpose').count() > 0, 'The source of the package is not shown');
  await shot('10-install-preview');
  await installForm.locator('[type=submit]').click();
  await dialog.locator('form[data-form="sim-run"][data-skill="patrol"]').waitFor({timeout: 20000});   // skillInstallResult went on to the run form
  must((await dialog.innerText()).includes('技能已安装'), 'After the install the flow did not go on as after any install');
  await shot('11-installed-run-form');
  result.installed = await page.evaluate(async id => (await api('robots/detail', {robot_id: id, project_id: projectId})).skills.items.map(s => s.id), sim.id);
  must(result.installed.includes('patrol'), 'The robot does not have the skill: ' + result.installed);
  await dialog.locator('.dialog-footer [data-action="close"]').click();

  // 7 · The simulated robot's page: install from the cloud is offered there, and the remote section is the real endpoint's.
  await page.evaluate(id => robotDetailOpen(id), sim.id);
  await dialog.locator('#remote-calls').waitFor();
  must(await dialog.locator('#robot-skills [data-cloudskill-action="install-robot"]').count() === 1, 'The robot page does not offer to install from the cloud');
  must(await dialog.locator('#robot-bundles').count() === 0, 'A simulated robot has no computer of its own, yet the section is shown');
  await page.waitForFunction(() => document.querySelector('#remote-body [data-remote-skill], #remote-body [data-remote-unsupported]'));
  result.real_runtime_remote = await dialog.locator('#remote-body [data-remote-unsupported]').count() === 0;
  const switchOf = id => dialog.locator(`[data-remote-skill="${id}"] .remote-switch`);
  if (result.real_runtime_remote) {
    await switchOf('patrol').click();
    await sub.locator('form[data-remote-form="allow"]').waitFor();
    must((await sub.innerText()).includes('第三方可以经云平台让这台机器人运动'), 'The real robot\'s confirmation does not warn about motion');
    await sub.locator('[name=confirmed]').check();
    await sub.locator('[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('[data-remote-skill="patrol"] .remote-switch')?.getAttribute('aria-checked') === 'true');
    must((await dialog.locator('[data-remote-skill="patrol"] .remote-skill-note').innerText()).includes(NAME), 'Who switched it on is not shown');
    await switchOf('patrol').click();
    await page.waitForFunction(() => document.querySelector('[data-remote-skill="patrol"] .remote-switch')?.getAttribute('aria-checked') === 'false');
  }
  await shot('12-sim-robot-page');

  // 8 · A robot with a management layer stand-in: the link, the switches, the confirmation, an older version, the calls.
  await fixture('attach?project=' + await page.evaluate(() => projectId));
  await reload();
  await page.evaluate(id => robotDetailOpen(id), cfg.robot_id);
  await switchOf('goto_place').waitFor();
  must((await dialog.locator('#remote-link').innerText()).includes('已连上云端'), 'The link to the cloud is not shown as online');
  must(await dialog.locator('#remote-skills .remote-switch[aria-checked="false"]').count() === 2, 'Not every installed skill has a switch that is off');

  await switchOf('goto_place').click();
  const allow = sub.locator('form[data-remote-form="allow"]');
  await allow.waitFor();
  const asked = await sub.innerText();
  for (const words of ['第三方可以经云平台让这台机器人运动', '不需要有人每次确认', NAME]) must(asked.includes(words), `The confirmation does not say: ${words}`);
  must(await dialog.evaluate(el => el.open), 'The robot\'s page was replaced by the confirmation');
  await shot('13-remote-confirm-motion');
  await allow.locator('[type=submit]').click();
  must((await formError(sub)).includes('请先勾选'), 'Switching on without the confirmation is not refused in the dialog');
  must((await fixture('state')).posted.length === 0, 'Something was sent to the robot before the confirmation');
  await allow.locator('[name=confirmed]').check();
  await allow.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('[data-remote-skill="goto_place"] .remote-switch')?.getAttribute('aria-checked') === 'true');
  must(!(await sub.evaluate(el => el.open)), 'The confirmation stayed open');
  let posted = (await fixture('state')).posted;
  must(posted.length === 1 && posted[0].allowed === true && posted[0].hardware_authorized === true && posted[0].set_by === NAME && posted[0].digest === 'a'.repeat(64),
    'The robot was not told who switched it on: ' + JSON.stringify(posted));

  await switchOf('status_report').click();
  await allow.waitFor();
  must((await sub.innerText()).includes('这个技能不会让机器人运动'), 'A skill that does not move gets the motion warning');
  must(!(await sub.innerText()).includes('让这台机器人运动，不需要'), 'A skill that does not move is said to move the robot');
  await shot('14-remote-confirm-plain');
  // The skill is installed again while the dialog is open: what was confirmed is no longer what would be allowed.
  await fixture('skill-change');
  await allow.locator('[name=confirmed]').check();
  await allow.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('#sub-dialog .form-error')?.textContent.includes('重新安装过'));
  must(await allow.locator('[type=submit]').count() === 0, 'After the skill changed the stale confirmation can still be sent');
  await shot('14b-remote-skill-changed');
  await sub.locator('[data-sub-dialog-close]').last().click();
  must(await switchOf('status_report').getAttribute('aria-checked') === 'false', 'A refused confirmation switched it on');

  // Recent calls arrive by themselves, and nothing else is redrawn: the switch is the very same element.
  // (The switch above left an audit event in the workspace state: let the page take that in first.)
  await reload();
  await page.evaluate(() => {
    window.__switchBefore = document.querySelector('[data-remote-skill="goto_place"] .remote-switch');
    window.__switchBefore.focus();
    window.__mainBefore = document.querySelector('#main').firstElementChild;
  });
  await fixture('remote-calls');
  await dialog.locator('[data-remote-call="cal_fixture_1"]').waitFor({timeout: 15000});
  const kept = await page.evaluate(() => ({
    same: window.__switchBefore === document.querySelector('[data-remote-skill="goto_place"] .remote-switch'),
    focused: document.activeElement === window.__switchBefore,
    main: window.__mainBefore === document.querySelector('#main').firstElementChild, ran: window.__markupRan}));
  must(kept.same && kept.focused, 'The switches were redrawn when the calls arrived: ' + JSON.stringify(kept));
  must(kept.main, 'The page behind was redrawn when the calls arrived');
  must(kept.ran === undefined, 'Markup in a call record was run');
  must(await dialog.locator('#remote-call-list tbody tr').count() === 2, 'The recent calls are not listed');
  must((await dialog.locator('#remote-call-list').innerText()).includes(cfg.markup), 'A call record is not shown as text');
  await shot('15-remote-calls');

  await switchOf('goto_place').click();           // switching off asks nothing
  await page.waitForFunction(() => document.querySelector('[data-remote-skill="goto_place"] .remote-switch')?.getAttribute('aria-checked') === 'false');
  posted = (await fixture('state')).posted;
  must(posted.at(-1).allowed === false && posted.at(-1).skill_id === 'goto_place' && !('digest' in posted.at(-1)), 'Switching off was not sent: ' + JSON.stringify(posted));

  // An older management layer: a plain sentence, not an error.
  await fixture('remote?supports=0');
  await page.evaluate(id => robotDetailOpen(id), cfg.robot_id);
  await dialog.locator('#remote-body [data-remote-unsupported]').waitFor();
  must((await dialog.locator('#remote-body').innerText()).trim() === '这台机器人的管理层还不支持远程调用', 'An older management layer is not told plainly');
  must(await dialog.locator('.form-error:not(:empty)').count() === 0 && await dialog.locator('.remote-switch').count() === 0,
    'An older management layer is shown as an error or with switches');
  await shot('16-remote-unsupported');
  await fixture('remote?supports=1');

  // 9 · Programs and scene data: collected into the cloud and installed back, each after an explicit confirmation.
  const bundles = dialog.locator('#robot-bundles');
  await bundles.locator('[data-bundle-action="collect"]').waitFor();
  must((await bundles.innerText()).includes('云端还没有这台机器人的程序和场景数据'), 'The empty list does not say so');
  const programBefore = (await fixture('state')).program;
  await bundles.locator('[data-bundle-action="collect"]').click();
  const collect = sub.locator('form[data-bundle-form="collect"]');
  await collect.waitFor();
  for (const words of ['runs/', '__pycache__/', '只读取，不会改动']) must((await sub.innerText()).includes(words), `The collect dialog does not say: ${words}`);
  await collect.locator('[type=submit]').click();
  must((await formError(sub)).includes('请填写机器人上的目录'), 'An empty directory is not refused in the dialog');
  await collect.locator('[name=directory]').fill('walk_programs');
  await collect.locator('[type=submit]').click();
  must((await formError(sub)).includes('~/ 开头'), 'A relative directory is not refused in the dialog');
  await collect.locator('[name=directory]').fill('~/walk_programs');
  await collect.locator('[type=submit]').click();
  must((await formError(sub)).includes('请先勾选'), 'Collecting without the confirmation is not refused');
  must((await fixture('state')).bundle_scripts.length === 0, 'The robot was asked before the confirmation');
  await shot('17-bundle-collect-dialog');
  await collect.locator('[name=authorized]').check();
  await collect.locator('[type=submit]').click();
  await bundles.locator('.bundle-job[data-bundle-status="done"]').waitFor({timeout: 30000});
  must((await bundles.locator('.bundle-job').innerText()).includes('已收进云端'), 'The collection does not say it is done');
  must(await bundles.locator('#bundle-list tbody tr').count() === 1, 'The collected bundle is not listed');
  must((await bundles.locator('#bundle-list').innerText()).includes('从这台机器人的 ~/walk_programs 收进来'), 'The list does not say which directory it was');
  must((await bundles.locator('.bundle-job').innerText()).includes('按约定跳过了'), 'What stayed behind is not told');
  let held = await fixture('state');
  must(held.materials.length === 1 && held.materials[0].kind === 'bundle' && held.materials[0].device_id === cfg.robot_id,
    'The cloud does not hold a bundle of this robot: ' + JSON.stringify(held.materials));
  must(JSON.stringify(held.program) === JSON.stringify(programBefore), 'The directory on the robot was changed');
  await shot('18-bundle-collected');

  await bundles.locator('#bundle-list [data-bundle-action="install"]').click();
  const install = sub.locator('form[data-bundle-form="install"]');
  await install.waitFor();
  for (const words of ['不会覆盖机器人上原来的目录', 'ros2-config.json', 'current']) must((await sub.innerText()).includes(words), `The install dialog does not say: ${words}`);
  await install.locator('[type=submit]').click();
  must((await formError(sub)).includes('请先勾选'), 'Installing without the confirmation is not refused');
  await shot('19-bundle-install-dialog');
  await install.locator('[name=authorized]').check();
  await install.locator('[type=submit]').click();
  await page.waitForFunction(() => document.querySelector('#robot-bundles .bundle-job[data-bundle-status="done"]')?.innerText.includes('已装到机器人'), null, {timeout: 30000});
  held = await fixture('state');
  const digits = held.materials[0].sha256.slice(0, 16);
  must(held.store[`walk_programs/current`] === 'link:' + digits && held.store[`walk_programs/${digits}/walk.py`] > 0,
    'The bundle was not unpacked next to the original: ' + JSON.stringify(held.store));
  must(JSON.stringify(held.program) === JSON.stringify(programBefore), 'Installing changed the original directory');
  must(held.refused.length === 0, 'The robot was asked to run something that is not one of the product\'s scripts: ' + held.refused);
  must((await bundles.innerText()).includes('ros2-config.json'), 'After the install nothing says how to use it');
  await bundles.scrollIntoViewIfNeeded();
  await shot('20-bundle-installed');
  result.bundle = {scripts: held.bundle_scripts.map(s => s[0]), dir: `~/neuxmind-bundles/walk_programs/${digits}`};

  // 10 · A narrow window: the robot's page and the library still fit.
  await page.setViewportSize({width: 600, height: 900});
  await bundles.scrollIntoViewIfNeeded();
  await shot('21-narrow-robot-page');
  await dialog.locator('#remote-calls').scrollIntoViewIfNeeded();
  await shot('22-narrow-remote');
  const overflow = await dialog.evaluate(el => el.scrollWidth - el.clientWidth);
  must(overflow <= 1, `The robot's page scrolls sideways in a narrow window (${overflow}px)`);
  await dialog.locator('.close-dialog').click();
  await main.locator('.cloud-skill-card').first().waitFor();
  await shot('23-narrow-library');
  must(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) <= 1, 'The library scrolls sideways in a narrow window');

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify(result));
  console.log('issue #43 browser acceptance passed');
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
