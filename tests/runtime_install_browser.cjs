// Browser acceptance for issue #19 (R1-03): from adding a robot to its connection state 在线, all in the console.
// A robot added over SSH gets "安装管理层"; the install shows its steps as they go and ends linked and online.
// A robot whose layer fails to start says where it stopped, what is left and cleans it up in two clicks.
// A robot that already runs a layer someone installed by hand is not touched until a choice is made.
//   node tests/runtime_install_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/runtime_install_browser_fixture.py (local SSH stand-ins, no real machine; the server needs
// NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const NAMES = {fresh: '新机器人', failing: '起不来的机器人', existing: '已有管理层的机器人'};

let failurePage = null, failureShots = 'artifacts';
(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-runtime-install-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [], answers = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  failurePage = page; failureShots = shots;
  page.on('pageerror', e => errors.push(e.message));
  const sent = [];
  page.on('request', r => { if (r.url().includes('/api/robots/runtime-install')) sent.push([r.url(), r.postDataJSON()]); });
  page.on('response', async r => { if (r.url().includes('/api/')) { try { answers.push(await r.text()); } catch { /* a body that is gone */ } } });

  const dialog = page.locator('#dialog');
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const control = async (path, robot = 'fresh') => (await fetch(`${cfg.control}${path}?robot=${robot}`)).json();
  const card = name => page.locator('article.card', {hasText: NAMES[name]});
  const dialogSays = async (text, seconds = 30) => dialog.getByText(text).first().waitFor({timeout: seconds * 1000});
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  // The step of the dialog with exactly this label, in this state ('安装' is part of '上传安装包' too).
  const stage = (state, label) => dialog.locator(`.ri-stage.is-${state}`).filter({has: page.locator('b', {hasText: new RegExp(`^${label}$`)})});
  const closeDialog = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await page.waitForTimeout(100); };

  const addRobot = async name => {
    const robot = cfg.robots[name];
    await page.goto(cfg.url + '/#robots');
    await page.locator('[data-action="engine-ssh"]').first().click();
    await fill('name', NAMES[name]); await fill('host', '127.0.0.1'); await fill('port', robot.port);
    await page.locator('#dialog [name=family]').selectOption('quadruped');
    await fill('username', cfg.robot_user); await fill('password', robot.login);
    await submit();
    await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
    await closeDialog();
    await page.evaluate(() => refresh(true));
    await card(name).locator('[data-action="engine-runtime-install"]').first().waitFor({timeout: 15000});
  };
  const openInstall = async name => {
    await card(name).locator('button[data-action="engine-runtime-install"]', {hasText: '安装管理层'}).click();
    await dialog.getByText('安装管理层', {exact: true}).first().waitFor();
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '安装验收', email: `install-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'inst1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '在控制台里给机器人装上管理层');
  await submit();
  await control('/tune');

  // 1. A robot added over SSH: its card offers the install next to linking, and says nothing about an install yet.
  await addRobot('fresh');
  await require('./robots_page_helper.cjs').openRobotMenu(card('fresh'));                    // issue #41: both are in the card's 更多功能 list
  const buttons = await card('fresh').locator('.robot-menu-item').allInnerTexts();
  await page.keyboard.press('Escape');
  check(buttons.indexOf('安装管理层') === buttons.indexOf('接入管理层') + 1, 'The install is not next to 接入管理层: ' + buttons);
  check(await card('fresh').locator('.ri-line:not([hidden])').count() === 0, 'A line about an install is shown before there is one');

  // 2. The form: what will happen and where, the standard port, the simulated body; nothing about sudo or autostart is done.
  await openInstall('fresh');
  const formText = await dialog.innerText();
  // Issue #20: autostart is now a box of its own in this form ("开机自启要管理员权限"), not something the install never does.
  for (const part of [`${cfg.robot_user}@127.0.0.1:${cfg.robots.fresh.port}`, '~/neuxmind-pkg/console/', '~/neuxmind', '不用 sudo', '开机自启要管理员权限',
    '只监听机器人本机', '不会直接覆盖', '仿真四足', '不会让机器人动']) check(formText.includes(part), `The form does not say "${part}"`);
  check(await page.locator('#dialog [name=port]').inputValue() === '8790', 'The standard port is not filled in');
  await page.screenshot({path: `${shots}/runtime-install-form.png`});

  // 3. A port that cannot be the management layer is refused in the form; nothing is sent.
  await fill('port', 22);
  await page.locator('#dialog [name=port]').evaluate(el => { el.removeAttribute('min'); });
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '1024–65535'}).waitFor();
  const starts = () => sent.filter(([url]) => url.endsWith('/runtime-install/start'));
  check(starts().length === 0, 'A request went out for port 22');
  await fill('port', 8790);

  // 4. Install: the steps are shown as they go, here held while the robot installs; the card says so too.
  await control('/hold');
  await submit();
  await stage('running', '安装').waitFor({timeout: 30000});
  check(await dialog.locator('.ri-stage.is-done').count() === 2, 'The check and the upload are not shown as done');
  check((await dialog.innerText()).includes(`管理层 ${cfg.runtime_version}`), 'The dialog does not name the package version');
  check(JSON.stringify(Object.keys(starts()[0][1]).sort()) === JSON.stringify(['port', 'project_id', 'robot_id']), 'The install request carries more than it needs');
  await page.screenshot({path: `${shots}/runtime-install-progress.png`});
  await page.setViewportSize({width: 390, height: 844});
  check(await noSidewaysScroll(), 'The progress dialog scrolls sideways on a phone');
  await page.screenshot({path: `${shots}/runtime-install-progress-phone.png`});
  await page.setViewportSize({width: 1440, height: 1000});
  await closeDialog();
  await card('fresh').locator('.ri-line:not([hidden])', {hasText: '正在安装'}).waitFor({timeout: 10000});
  await card('fresh').locator('.ri-line [data-action="engine-runtime-install"]').click();     // "查看" opens it again
  await stage('running', '安装').waitFor({timeout: 10000});

  // 5. The robot finishes: linked over SSH, the connection state 在线, the install's line gone from the card.
  await control('/release');
  await dialogSays('管理层已安装并接入', 60);
  check(await dialog.locator('.ri-stage.is-done').count() === 5, 'Not every step is shown as done');
  await page.screenshot({path: `${shots}/runtime-install-done.png`});
  await closeDialog();
  await card('fresh').locator('.rl-line', {hasText: '已连通'}).waitFor({timeout: 30000});
  await card('fresh').locator('.hb-line', {hasText: '在线'}).waitFor({timeout: 30000});
  await require('./robots_page_helper.cjs').openRobotMenu(card('fresh'));                    // issue #41: plain facts are in the card's 更多功能 list
  check((await card('fresh').innerText()).includes('控制通道：已连接'), 'The card does not say the control channel is connected');
  await page.keyboard.press('Escape');
  check(await card('fresh').locator('.ri-line:not([hidden])').count() === 0, 'The install line stays after a success');
  const fresh = await control('/seen');
  check(JSON.stringify(fresh.runs) === JSON.stringify([['install', '8790', 'sim_quadruped'], ['start', '8790', 'sim_quadruped']]),
        'The robot did not install once and start once: ' + JSON.stringify(fresh.runs));
  await page.screenshot({path: `${shots}/runtime-install-online.png`});

  // 6. A robot whose management layer does not start: where it stopped, its output, what is left, and cleaning up.
  await addRobot('failing');
  await openInstall('failing');
  await submit();
  await dialogSays('停在「启动」', 60);
  const failed = await dialog.innerText();
  check(failed.includes('~/neuxmind-pkg/console/' + cfg.package_id) && failed.includes('这次装的'), 'The dialog does not say what is left: ' + failed);
  check(failed.includes('停掉并删除这次装的 ~/neuxmind'), 'The dialog does not say what cleaning does');
  await dialog.locator('.ri-log summary').click();
  await dialogSays('the adapter did not start');
  await page.screenshot({path: `${shots}/runtime-install-failed.png`});
  await page.locator('#dialog [data-action="engine-runtime-install-clean"]').click();
  await page.locator('#dialog [data-action="engine-runtime-install-clean-confirm"]').waitFor();
  check(!sent.some(([url]) => url.endsWith('/clean')), 'One click on 清理 already cleaned');
  await page.locator('#dialog [data-action="engine-runtime-install-clean-confirm"]').click();
  await dialogSays('已清理', 60);
  const failing = await control('/seen', 'failing');
  check(failing.files.length === 0 && !failing.installed, 'Cleaning left something on the robot: ' + JSON.stringify(failing));
  await closeDialog();

  // 7. A robot that already runs a layer someone installed by hand: a question first, nothing sent; then linking it.
  const before = await control('/seen', 'existing');
  await addRobot('existing');
  await openInstall('existing');
  await submit();
  await dialogSays('已经有一个装好的管理层', 60);
  check((await dialog.innerText()).includes('没有上传任何东西'), 'The question does not say nothing was sent');
  check(await page.locator('#dialog input[name=existing]').count() === 2, 'Not two ways to go on');
  await page.screenshot({path: `${shots}/runtime-install-existing.png`});
  await page.locator('#dialog input[name=existing][value=replace]').check();
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '请先勾选确认'}).waitFor();
  await page.locator('#dialog input[name=existing][value=use]').check();
  await submit();
  await dialogSays('管理层已安装并接入', 60);
  const after = await control('/seen', 'existing');
  check(after.token === before.token && JSON.stringify(after.runs) === JSON.stringify(before.runs) && after.files.length === 0,
        'Linking the existing layer changed something on the robot');
  await closeDialog();
  await card('existing').locator('.rl-line', {hasText: '已连通'}).waitFor({timeout: 30000});

  // 8. No token in the page or in anything the page was sent.
  const html = await page.content();
  for (const token of [fresh.token, after.token]) {
    check(token && !html.includes(token), 'A token is in the page');
    check(!answers.some(text => text.includes(token)), 'A token was sent to the page');
  }
  check(errors.length === 0, 'Page errors: ' + errors.join('; '));
  await browser.close();
  console.log(JSON.stringify({ok: true, engine: engine.name(), shots}));
})().catch(async error => {
  console.error(error);
  if (failurePage) {           // what the page showed when it failed
    await failurePage.screenshot({path: `${failureShots}/runtime-install-failure.png`}).catch(() => {});
    console.error('dialog:', await failurePage.locator('#dialog').innerText().catch(() => ''));
  }
  process.exit(1);
});
