// Browser acceptance for issue #20 (R1-03, R1-08): the management layer starts when the robot boots.
// The install form has a box for it; before anything runs as root the console lists exactly what will run and
// waits; a wrong sudo password asks again; the card says "已开机自启"; after a power cut the robot is reached
// again; turning it off needs the same confirmation. A robot without a sudo password is never asked for one; a
// management layer the console did not install is refused, and the card says why.
//   node tests/runtime_autostart_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/runtime_autostart_browser_fixture.py (local SSH stand-ins, no real machine, nothing runs as root;
// the server needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const NAMES = {sudo: '要密码的机器人', nopasswd: '免密的机器人', hand: '手工装的机器人'};

let failurePage = null, failureShots = 'artifacts';
(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-runtime-autostart-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [], answers = [], sent = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  failurePage = page; failureShots = shots;
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (r.url().includes('/api/')) sent.push([r.url(), r.postData() || '']); });
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
  const control = async (path, robot = 'sudo') => (await fetch(`${cfg.control}${path}?robot=${robot}`)).json();
  const card = name => page.locator('article.card', {hasText: NAMES[name]});
  const line = name => card(name).locator('.ra-line');
  const dialogSays = async (text, seconds = 30) => dialog.getByText(text).first().waitFor({timeout: seconds * 1000});
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  const closeDialog = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await page.waitForTimeout(100); };
  const confirmShown = async (title = '开机自启：确认要执行的内容') => dialog.getByText(title, {exact: true}).first().waitFor({timeout: 60000});

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
    await line(name).waitFor({timeout: 15000});
  };
  const openInstall = async name => {
    await card(name).locator('button[data-action="engine-runtime-install"]', {hasText: '安装管理层'}).click();
    await dialog.getByText('安装管理层', {exact: true}).first().waitFor();
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '自启验收', email: `autostart-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'auto1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '机器人断电重启后管理层自己起来');
  await submit();
  await control('/tune');

  // 1. Before any install the card says autostart is off, and why.
  await addRobot('sudo');
  check((await line('sudo').innerText()).includes('未开机自启') && (await line('sudo').innerText()).includes('还没有安装管理层'),
        'The card does not say why: ' + await line('sudo').innerText());

  // 2. The install form has the box; ticked, the install waits for the person before anything runs as root.
  await openInstall('sudo');
  const formText = await dialog.innerText();
  check(formText.includes('安装本身不用 sudo') && formText.includes('装好后设置开机自启'), 'The form does not explain the box: ' + formText);
  check(!(await page.locator('#dialog [name=autostart]').isChecked()), 'The box is ticked before the person ticks it');
  await page.screenshot({path: `${shots}/runtime-autostart-form.png`});
  await page.locator('#dialog [name=autostart]').check();
  await submit();
  await confirmShown();
  const robot = await control('/seen');
  check(robot.root_runs.length === 0 && robot.unit === null && robot.runtime_port === 8790, 'Something ran as root before the confirmation: ' + JSON.stringify(robot));
  const confirmText = await dialog.innerText();
  for (const part of ['sudo tee /etc/systemd/system/neuxmind-runtime.service', 'sudo systemctl daemon-reload',
                      'sudo systemctl enable neuxmind-runtime.service', 'sudo systemctl --no-block restart neuxmind-runtime.service',
                      '.autostart', `User=${cfg.robot_user}`, 'Restart=on-failure', '需要这个账号的 sudo 密码', '不改别的系统文件', '确认之前什么都不会执行']) {
    check(confirmText.includes(part), `The confirmation does not list "${part}"`);
  }
  check(await page.locator('#dialog [name=sudo_password]').getAttribute('type') === 'password', 'The password is shown as typed');
  const unitText = await page.locator('#dialog .ra-unit pre').innerText();     // shown exactly as it is written, not reformatted
  check(unitText.startsWith('# Made by the NeuxMind console') && unitText.includes('\n[Service]\nType=forking\n'), 'The service file is not shown as written: ' + unitText);
  await page.screenshot({path: `${shots}/runtime-autostart-confirm.png`, fullPage: true});
  await page.setViewportSize({width: 375, height: 812});
  check(await noSidewaysScroll(), 'The confirmation scrolls sideways on a phone');
  await page.screenshot({path: `${shots}/runtime-autostart-confirm-phone.png`});
  await page.setViewportSize({width: 1440, height: 1000});

  // 3. Not without ticking the box; a wrong password asks again and says so; the right one goes on.
  await fill('sudo_password', 'not-the-password');
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '请先勾选'}).waitFor();
  check(!sent.some(([url]) => url.endsWith('/runtime-install/answer')), 'An answer went out without the box ticked');
  await page.locator('#dialog [name=confirm_plan]').check();
  await submit();
  await dialog.locator('.ra-error', {hasText: '密码不对'}).waitFor({timeout: 60000});
  check(await page.locator('#dialog [name=sudo_password]').inputValue() === '', 'The wrong password is still in the field');
  await page.screenshot({path: `${shots}/runtime-autostart-wrong-password.png`});
  await fill('sudo_password', cfg.robots.sudo.sudo_password);
  await page.locator('#dialog [name=confirm_plan]').check();
  await submit();
  await dialogSays('管理层已安装并接入', 60);
  await dialogSays('已开机自启');
  await page.screenshot({path: `${shots}/runtime-autostart-done.png`});
  await closeDialog();
  const on = await control('/seen');
  check(on.enabled && on.service === 'active' && on.runtime_port === 8790, 'The service is not enabled and running: ' + JSON.stringify(on));
  check(JSON.stringify(on.right_password) === JSON.stringify([false, true]), 'sudo did not get the two typed passwords: ' + JSON.stringify(on.right_password));
  await line('sudo').getByText('已开机自启').waitFor({timeout: 15000});
  await card('sudo').locator('.hb-line', {hasText: '在线'}).waitFor({timeout: 30000});

  // 4. The typed passwords went out once each, in the answer, and came back nowhere.
  const typed = [cfg.robots.sudo.sudo_password, 'not-the-password'];
  for (const secret of typed) {
    const carrying = sent.filter(([, body]) => body.includes(secret));
    check(carrying.length === 1 && carrying[0][0].endsWith('/runtime-install/answer'), 'A password went out other than once in its answer: ' + secret);
    check(!answers.some(text => text.includes(secret)), 'A password came back to the page');
    check(!(await page.content()).includes(secret), 'A password is in the page');
  }
  const stored = await page.evaluate(() => JSON.stringify([{...localStorage}, {...sessionStorage}]));
  check(!typed.some(secret => stored.includes(secret)), 'A password is in the browser storage');

  // 5. A power cut: the robot comes back by itself; the connection check reaches it and it is online again.
  await control('/reboot');
  const back = await control('/seen');
  check(back.service === 'active' && back.runtime_port === 8790, 'The service did not start the runtime at boot: ' + JSON.stringify(back));
  await card('sudo').locator('[data-action="robot-connect"]').first().click();
  await card('sudo').locator('.hb-line', {hasText: '在线'}).waitFor({timeout: 60000});

  // 6. Turning it off: the same confirmation, then the card says it is off.
  await line('sudo').locator('[data-action="engine-runtime-autostart"]').click();
  await dialogSays('取消开机自启');
  await page.locator('#dialog [data-action="engine-runtime-autostart-off"]').click();
  await confirmShown('取消开机自启：确认要执行的内容');
  check((await dialog.innerText()).includes('sudo systemctl disable --now neuxmind-runtime.service'), 'The off plan does not list disable');
  await fill('sudo_password', cfg.robots.sudo.sudo_password);
  await page.locator('#dialog [name=confirm_plan]').check();
  await submit();
  await dialogSays('已取消开机自启', 60);
  await closeDialog();
  const off = await control('/seen');
  check(off.unit === null && !off.enabled && off.runtime_port === 8790, 'Turning it off left the service or stopped the runtime: ' + JSON.stringify(off));
  await line('sudo').getByText('已取消').waitFor({timeout: 15000});

  // 7. A robot whose sudo needs no password: no password field at all.
  await addRobot('nopasswd');
  await openInstall('nopasswd');
  await page.locator('#dialog [name=autostart]').check();
  await submit();
  await confirmShown();
  check(await page.locator('#dialog [name=sudo_password]').count() === 0, 'A password is asked for although sudo needs none');
  check((await dialog.innerText()).includes('这个账号不需要密码'), 'The confirmation does not say no password is needed');
  await page.locator('#dialog [name=confirm_plan]').check();
  await submit();
  await dialogSays('已开机自启', 60);
  await closeDialog();
  check((await control('/seen', 'nopasswd')).enabled, 'The robot without a sudo password did not get autostart');

  // 8. A management layer someone installed by hand: linked as it is, autostart refused, and the card says why.
  await addRobot('hand');
  await openInstall('hand');
  await page.locator('#dialog [name=autostart]').check();
  await submit();
  await dialogSays('已经有一个装好的管理层', 60);
  await page.locator('#dialog input[name=existing][value=use]').check();
  await submit();
  await dialogSays('管理层已安装并接入', 60);
  await closeDialog();
  await line('hand').getByText('不是这个控制台装的管理层').waitFor({timeout: 15000});
  await line('hand').locator('[data-action="engine-runtime-autostart"]').click();
  await dialogSays('删除后重新安装');
  check(await page.locator('#dialog [data-action="engine-runtime-autostart-on"]').count() === 0, 'A hand install can be given autostart');
  const hand = await control('/seen', 'hand');
  check(hand.root_runs.length === 0 && hand.unit === null, 'Something ran as root on the hand install: ' + JSON.stringify(hand));
  await closeDialog();

  check(errors.length === 0, 'Page errors: ' + errors.join('; '));
  await browser.close();
  console.log(JSON.stringify({ok: true, engine: engine.name(), shots}));
})().catch(async error => {
  console.error(error);
  if (failurePage) {           // what the page showed when it failed
    await failurePage.screenshot({path: `${failureShots}/runtime-autostart-failure.png`}).catch(() => {});
    console.error('dialog:', await failurePage.locator('#dialog').innerText().catch(() => ''));
  }
  process.exit(1);
});
