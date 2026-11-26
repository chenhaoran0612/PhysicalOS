// Browser acceptance for issue #38: a robot's body for the management layer, all in the console.
// A robot added over SSH gets "本体设置": a real body (the robot's own ROS 2) with its ROS settings and its
// device configuration (the places it knows) is saved, installed with "安装管理层", read back from the robot,
// and a changed configuration is put there with "应用到机器人". The dialog fits a narrow window.
//   node tests/robot_body_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/runtime_install_browser_fixture.py (local SSH stand-ins, no real machine; the server needs
// NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {robotAction, openRobotMenu} = require('./robots_page_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const NAME = '带导航程序的四足';
const EXAMPLE_FILE = path.join(__dirname, '..', 'runtime', 'examples', 'ros2', 'onbody-programs.example.json');
const WIDTHS = [320, 768, 1024, 1440];

let failurePage = null, failureShots = 'artifacts';
(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-runtime-install-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);
  failurePage = page; failureShots = shots;
  page.on('pageerror', e => errors.push(e.message));
  const sent = [];
  page.on('request', r => { if (/\/api\/robots\/(body|runtime-install\/(start|apply-body))/.test(r.url())) sent.push([r.url().split('/api/')[1], r.postDataJSON()]); });

  const dialog = page.locator('#dialog');
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const control = async route => (await fetch(`${cfg.control}${route}?robot=fresh`)).json();
  const card = page.locator('article.card', {hasText: NAME});
  const says = async (text, seconds = 30) => dialog.getByText(text).first().waitFor({timeout: seconds * 1000});
  const closeDialog = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await page.waitForTimeout(100); };
  const openBody = async () => { await robotAction(card, 'engine-robot-body').click(); await dialog.getByText('本体设置', {exact: true}).first().waitFor(); };
  const found = () => page.locator('#dialog .rb-found');
  const fits = () => dialog.evaluate(el => {
    const wide = [...el.querySelectorAll('*')].filter(node => node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().right > window.innerWidth + 1);
    return {page: document.documentElement.scrollWidth <= document.documentElement.clientWidth, wide: wide.slice(0, 3).map(node => node.className || node.tagName)};
  });

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '本体验收', email: `body-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'body1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '办公室巡检'); await fill('objective', '让机器人按地点的名称去巡检');
  await submit();
  await control('/tune');

  // 1. A robot added over SSH has 本体设置 in its list; nothing says "real body" before one is set.
  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="engine-ssh"]').first().click();
  await fill('name', NAME); await fill('host', '127.0.0.1'); await fill('port', cfg.robots.fresh.port);
  await page.locator('#dialog [name=family]').selectOption('quadruped');
  await fill('username', cfg.robot_user); await fill('password', cfg.robots.fresh.login);
  await submit();
  await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await page.locator('#dialog input[type=checkbox]').check();
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
  await closeDialog();
  await page.evaluate(() => refresh(true));
  await card.locator('[data-action="engine-robot-body"]').first().waitFor({state: 'attached', timeout: 15000});
  await openRobotMenu(card);
  const entries = await card.locator('.robot-menu-item').allInnerTexts();
  await page.keyboard.press('Escape');
  check(entries.indexOf('本体设置') === entries.indexOf('安装管理层') + 1, '本体设置 is not next to 安装管理层: ' + entries);
  check(await card.locator('.rb-line').count() === 0, 'A line about a real body is shown before one is set');

  // 2. The dialog opens on the simulated body; the real body's fields appear when it is chosen.
  await openBody();
  check(await page.locator('#dialog [name=kind][value=sim]').isChecked(), 'A robot without a saved body does not open on the simulated one');
  check(await page.locator('#dialog .rb-real').isHidden(), 'The real body\'s fields are shown for a simulated body');
  await page.locator('#dialog [name=kind][value=ros2]').check();
  await page.locator('#dialog .rb-real').waitFor();
  const text = await dialog.innerText();
  for (const part of ['保存不会动机器人', '会让机器人真的动起来', '从机器人读取', '只读', 'ROS 2 环境文件', 'ROS_DOMAIN_ID', '通信中间件', '本体配置', '坐标留在机器人自己的程序里'])
    check(text.includes(part), `The dialog does not say "${part}"`);
  check(await page.locator('#dialog [name=setup]').inputValue() === '/opt/ros/humble/setup.bash', 'The standard setup file is not filled in');

  // 3. Reading from a robot that has no ROS 2 says so and changes nothing there.
  await page.locator('#dialog [data-action="engine-robot-body-detect"]').click();
  await found().filter({hasText: '没有在机器人上读到 ROS 2'}).waitFor({timeout: 30000});
  check((await control('/seen')).runs.length === 0, 'Reading from the robot ran its install script');
  check(await page.locator('#dialog [name=kind][value=ros2]').isChecked(), 'Reading nothing changed the chosen body');

  // 4. A field filled in wrongly is refused in the form; nothing is sent.
  await fill('ROS_DOMAIN_ID', 999);
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '0–232'}).waitFor();
  check(!sent.some(([url]) => url === 'robots/body/save'), 'A body with a bad domain was sent');
  await fill('ROS_DOMAIN_ID', 24);
  await fill('RMW_IMPLEMENTATION', 'rmw_zenoh_cpp');
  await fill('rmw_file', '/etc/robot/zenoh_session.json5');

  // 5. A configuration that is not usable is explained; an imported one is previewed with the places the robot will know.
  await page.locator('#dialog [name=config_text]').fill('{"programs": {"places": {}}}');
  await page.locator('#dialog [data-action="engine-robot-body-check"]').click();
  await page.locator('#dialog .rb-bad', {hasText: '本体配置有问题'}).waitFor();
  await page.locator('#dialog [data-rb-file]').setInputFiles(EXAMPLE_FILE);
  await found().filter({hasText: '已导入 onbody-programs.example.json'}).waitFor();
  await page.locator('#dialog [data-action="engine-robot-body-check"]').click();
  await page.locator('#dialog .rb-declares', {hasText: '认识 3 个地点'}).waitFor();
  const places = await page.locator('#dialog .rb-places > li > b').allInnerTexts();
  check(JSON.stringify(places) === JSON.stringify(['出发点', '会议室', '二楼']), 'The preview does not list the places in order: ' + places);
  check((await page.locator('#dialog .rb-preview').innerText()).includes('坐标和地图在机器人自己的程序里'), 'The preview does not say where the coordinates are');
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(120);
    const fit = await fits();
    check(fit.page && !fit.wide.length, `The body dialog does not fit ${width}px: ${JSON.stringify(fit)}`);
    await page.screenshot({path: `${shots}/robot-body-form-${width}.png`});
  }
  await page.setViewportSize({width: 1440, height: 1000});

  // 6. Saving reaches no robot and says what comes next: the install.
  await submit();
  await says('还没有装到机器人上');
  await says('认识 3 个地点');
  check((await control('/seen')).runs.length === 0, 'Saving the body ran something on the robot');
  const saved = sent.filter(([url]) => url === 'robots/body/save').at(-1)[1];
  check(saved.kind === 'ros2' && saved.settings.ROS_DOMAIN_ID === '24' && saved.settings.ZENOH_SESSION_CONFIG_URI === '/etc/robot/zenoh_session.json5',
    'The saved body is not what was filled in: ' + JSON.stringify(saved.settings));
  await page.screenshot({path: `${shots}/robot-body-saved.png`});

  // 7. The install says it is for a real body and ends linked; the robot's install was made for ros2.
  await page.locator('#dialog [data-action="engine-runtime-install"]').click();
  await says('真实本体（这台机器人自己的 ROS 2');
  check((await dialog.innerText()).includes('技能会让机器人真的动起来'), 'The install form does not say that a real body moves the robot');
  await page.screenshot({path: `${shots}/robot-body-install-form.png`});
  await submit();
  await says('管理层已安装并接入', 90);
  const seen = await control('/seen');
  check(JSON.stringify(seen.runs) === JSON.stringify([['install', '8790', 'ros2'], ['start', '8790', 'ros2']]), 'The robot was not installed with the real body: ' + JSON.stringify(seen.runs));
  check(seen.files.includes('body.env') && seen.files.includes('ros2-config.json'), 'The body files did not reach the robot: ' + seen.files);
  await closeDialog();
  await page.evaluate(() => refresh(true));
  await card.locator('.rb-line', {hasText: '机器人上用的就是这份'}).waitFor({timeout: 15000});
  await page.screenshot({path: `${shots}/robot-body-card.png`});

  // 8. Reading from the robot now finds its real install; a changed configuration is applied with a restart.
  await openBody();
  check(await page.locator('#dialog [name=kind][value=ros2]').isChecked(), 'The dialog does not open on the saved real body');
  await page.locator('#dialog [data-action="engine-robot-body-detect"]').click();
  await found().filter({hasText: '机器人上已经有真实本体的管理层'}).waitFor({timeout: 30000});
  check(await page.locator('#dialog [name=ROS_DOMAIN_ID]').inputValue() === '24', 'The domain read from the robot is not filled in');
  const config = JSON.parse(await page.locator('#dialog [name=config_text]').inputValue());
  config.programs.places['前台'] = config.programs.places['会议室'];
  await page.locator('#dialog [name=config_text]').fill(JSON.stringify(config, null, 2));
  await submit();
  await says('点「应用到机器人」把这份本体配置放到机器人上');
  await says('正在运行的技能会被停掉');
  await page.locator('#dialog [data-action="engine-robot-body-apply"]').click();
  await dialog.locator('.dialog-head h2', {hasText: '应用本体配置'}).waitFor();
  await says('本体配置已经放到机器人上', 90);
  await page.screenshot({path: `${shots}/robot-body-applied.png`});
  const after = await control('/seen');
  check(JSON.stringify(after.runs.slice(2)) === JSON.stringify([['stop', '8790', 'ros2'], ['start', '8790', 'ros2']]), 'Applying did not restart the layer: ' + JSON.stringify(after.runs));
  await closeDialog();
  await page.evaluate(() => refresh(true));
  await card.locator('.rb-line', {hasText: '机器人上用的就是这份'}).waitFor({timeout: 15000});

  // 9. Going back to a simulated body is said to need a new install, and offers it.
  await openBody();
  await page.locator('#dialog [name=kind][value=sim]').check();
  await submit();
  await says('重新安装');
  check(await page.locator('#dialog [data-action="engine-runtime-install"]').count() === 1, 'A changed kind of body does not offer the install');
  check(await page.locator('#dialog [data-action="engine-robot-body-apply"]').count() === 0, 'A changed kind of body offers an apply');
  await page.locator('#dialog [data-action="engine-runtime-install"]').click();
  await says('仿真四足');
  await submit();
  await says('它是为真实本体装的');
  const options = await page.locator('#dialog [name=existing]').evaluateAll(list => list.map(el => el.value));
  check(JSON.stringify(options) === JSON.stringify(['replace']), 'An install for the other body can be linked as it is: ' + options);
  await page.screenshot({path: `${shots}/robot-body-other.png`});
  await closeDialog();

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, engine: engine.name(), shots}));
  await browser.close();
})().catch(async error => {
  try { if (failurePage) await failurePage.screenshot({path: `${failureShots}/robot-body-failure.png`}); } catch { /* no page */ }
  console.error(error);
  process.exit(1);
});
