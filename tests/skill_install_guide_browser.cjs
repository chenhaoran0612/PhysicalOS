// Issue #62 browser acceptance: from 机器人应用, a robot that is not connected no longer ends in buttons
// that cannot be pressed. An account with no task and robots reached over SSH only (the state of the report):
//   1  「使用机器人上已安装的技能」 says that no robot is connected, not that no skill is installed, and
//      gives each robot its step: 「安装管理层」 and 「接入已有的管理层」;
//   2  the install dialog of such a robot says above the skills why none can be installed, with the same
//      two buttons; the skills below stay disabled; nothing overflows from 320 to 1440; keyboard reaches it;
//   3  「安装管理层」 there opens the install; when it is done the robot is connected, the notice is gone
//      and 「使用机器人上已安装的技能」 reads the robot again;
//   4  「接入已有的管理层」 opens the link form of a robot that already runs one;
//   5  a robot connected by address that does not answer: 「重新连接」 says what went wrong inside the dialog;
//   6  「打开「我的机器人」」 (a robot with no way in) closes the dialog and goes there.
// Fixture: tests/runtime_install_browser_fixture.py (local SSH stand-ins; no real machine).
// Usage: node tests/skill_install_guide_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [320, 375, 768, 1024, 1440], DESKTOP = {width: 1440, height: 1000};
const NAMES = {fresh: '新机器人', existing: '已有管理层的机器人'};
const DEAD_ENDPOINT = 'http://127.0.0.1:9';                    // the discard port: nothing answers there
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-runtime-install-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `skill-install-guide-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => page.locator('#dialog button[type=submit]').click();
  const closeDialog = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await page.waitForTimeout(100); };
  const labels = locator => locator.evaluateAll(list => list.map(b => b.textContent.trim()));
  const control = async (p, robot = 'fresh') => (await fetch(`${cfg.control}${p}?robot=${robot}`)).json();
  const robotId = name => page.evaluate(n => state.robots.find(r => r.name === n)?.id || '', name);
  const noSidewaysScroll = () => page.evaluate(() => {
    const box = document.querySelector('#dialog');
    return document.documentElement.scrollWidth <= document.documentElement.clientWidth && box.scrollWidth <= box.clientWidth + 1;
  });
  const openAdopt = async () => {
    await page.goto(cfg.url + '/#skills');
    await page.evaluate(() => action('ux-adopt'));      // issue #68: 机器人应用 has no button for this any more
    await dialog.getByText('使用机器人上已安装的技能', {exact: true}).first().waitFor();
  };
  const openGallery = async name => {
    const id = await robotId(NAMES[name] || name);
    if (!id) throw Error('no robot named ' + name + ': ' + JSON.stringify(await page.evaluate(() => state.robots.map(r => r.name))));
    await page.evaluate(i => action('skillpkg-open', i), id);
    await dialog.locator('.gallery-item').first().waitFor({timeout: 30000});
    return id;
  };
  const addOverSsh = async name => {
    const robot = cfg.robots[name];
    await page.goto(cfg.url + '/#robots');
    await page.locator('[data-action="engine-ssh"]').first().click();
    await fill('name', NAMES[name]); await fill('host', '127.0.0.1'); await fill('port', robot.port);
    await page.locator('#dialog [name=family]').selectOption('quadruped');
    await require('./robot_model_helper.cjs').fillBrandAndModel(page);
    await fill('username', cfg.robot_user); await fill('password', robot.login);
    await submit();
    await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
    await closeDialog();
    await page.evaluate(() => refresh(true));
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '技能安装验收', email: `skill-guide-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: crypto.randomBytes(9).toString('hex') + 'A1'});
  await control('/tune');
  same((await page.evaluate(() => state.projects)).length, 0, 'the account has no task, as in the report');
  await addOverSsh('fresh');
  await addOverSsh('existing');
  same(await page.evaluate(() => state.robots.map(r => [r.status, Boolean(r.endpoint), Boolean(r.connection_id)])),
       [['unconnected', false, true], ['unconnected', false, true]], 'two robots reached over SSH only');

  // 1. No robot is connected: the dialog says so and gives each robot its step.
  await openAdopt();
  await dialog.locator('.sg-none').waitFor();
  const adoptText = await dialog.innerText();
  must(adoptText.includes('没有已连接的机器人'), 'the dialog does not say that no robot is connected');
  must(!adoptText.includes('还没有安装技能'), 'the dialog still says that no skill is installed');
  same(await labels(dialog.locator('.sg-robot .section-head b')), [NAMES.fresh, NAMES.existing], 'each robot once, oldest first');
  same(await labels(dialog.locator('.sg-robot').first().locator('button')), ['安装管理层', '接入已有的管理层'], 'the step of a robot reached over SSH');
  must(await dialog.locator('[data-action="skillpkg-open"]').count() === 0, 'the dead end 「安装技能」 is still offered');
  await page.screenshot({path: file('1-no-robot-connected')});
  await closeDialog();

  // 2. The install dialog of such a robot: why, the two buttons, the skills disabled; narrow screens; keyboard.
  const freshId = await openGallery('fresh');
  const notice = dialog.locator('.sg-notice');
  await notice.waitFor();
  must((await notice.innerText()).includes('还没有接入管理层'), 'the notice does not say what is missing');
  same(await labels(notice.locator('button')), ['安装管理层', '接入已有的管理层'], 'the buttons of the notice');
  same(await notice.locator('button').evaluateAll(list => list.map(b => b.dataset.id)), [freshId, freshId], 'they act on this robot');
  const installs = dialog.locator('.gallery [data-action="skillpkg-example"]');
  must(await installs.count() > 0 && await installs.evaluateAll(list => list.every(b => b.disabled)), 'a skill can be installed on a robot that is not connected');
  must(await notice.evaluate(el => el.compareDocumentPosition(document.querySelector('#dialog .gallery')) & Node.DOCUMENT_POSITION_FOLLOWING),
       'the notice is not above the skills');
  await page.screenshot({path: file('2-install-dialog')});
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    must(await noSidewaysScroll(), `the install dialog scrolls sideways at ${width}`);
    const box = await notice.locator('button').first().boundingBox();
    must(box && box.x >= 0 && box.x + box.width <= width, `the first button of the notice is cut off at ${width}`);
    if (width === 320 || width === 768) await page.screenshot({path: file(`2-install-dialog-${width}`)});
  }
  await page.setViewportSize(DESKTOP);
  await notice.locator('button').first().focus();
  must(await page.evaluate(() => document.activeElement?.dataset.action) === 'engine-runtime-install', 'the first button of the notice cannot take the focus');

  // 3. 「安装管理层」 from there: the install; then the robot is connected and nothing stands in the way.
  await page.keyboard.press('Enter');
  await dialog.getByText('安装管理层', {exact: true}).first().waitFor();
  await submit();
  await dialog.getByText('管理层已安装并接入').first().waitFor({timeout: 90000});
  await closeDialog();
  await page.waitForFunction(id => state.robots.find(r => r.id === id)?.status === 'connected', freshId, {timeout: 30000});
  await openGallery('fresh');
  must(await dialog.locator('.sg-notice').count() === 0, 'the notice stays for a connected robot');
  await page.screenshot({path: file('3-connected')});
  await closeDialog();
  await openAdopt();
  await page.waitForTimeout(1500);
  must(await dialog.locator('.sg-none').count() === 0, 'with a robot connected the dialog still says none is');
  const offered = dialog.locator('[data-action="skillpkg-open"]');
  if (await offered.count()) same(await offered.first().getAttribute('data-id'), freshId, '「安装技能」 opens the robot that can take a skill');
  await closeDialog();

  // 4. 「接入已有的管理层」 of a robot that already runs one opens its link form.
  await openGallery('existing');
  await dialog.locator('.sg-notice button', {hasText: '接入已有的管理层'}).click();
  await dialog.getByText('接入管理层').first().waitFor();
  must((await dialog.innerText()).includes(NAMES.existing), 'the link form is not the one of this robot');
  await closeDialog();

  // 5. A robot connected by address that does not answer: the refusal is said inside the dialog.
  const deadId = await page.evaluate(async endpoint => {
    const made = await api('robots', {project_id: projectId, name: '直连的机器人', family: 'quadruped', endpoint, token: 'none', vendor: 'Acme', model: 'D1'});
    await refresh(true);
    return made.id;
  }, DEAD_ENDPOINT);
  await openGallery('直连的机器人');
  same(await labels(dialog.locator('.sg-notice button')), ['重新连接'], 'a robot connected by address');
  await dialog.locator('.sg-notice [data-action="skillpkg-guide-connect"]').click();
  await page.waitForFunction(() => document.querySelector('#dialog .sg-error')?.textContent.trim().length > 0, null, {timeout: 30000});
  must(await dialog.evaluate(d => d.open), 'the dialog closed on a failed reconnect');
  must(await dialog.locator('.sg-notice [data-action="skillpkg-guide-connect"]').isEnabled(), 'the button stays disabled after a failure');
  same(await page.evaluate(id => state.robots.find(r => r.id === id)?.status, deadId), 'unconnected', 'the robot that does not answer');
  await page.screenshot({path: file('5-reconnect-refused')});

  // 6. A robot with no way in: 「我的机器人」.
  await page.evaluate(() => { document.querySelector('#dialog .sg-notice button').dataset.action = 'skillpkg-guide-robots'; });
  await dialog.locator('.sg-notice [data-action="skillpkg-guide-robots"]').click();
  await page.waitForFunction(() => location.hash === '#robots' && !document.querySelector('#dialog').open);

  same(errors, [], 'page errors');
  await browser.close();
  console.log('skill install guide browser: passed on ' + engine.name());
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
