// Browser acceptance for issue #34 (R1-13): a robot's materials live in its device profile. Uploaded there,
// they are parsed by themselves; the URDF's joint limits and the manual's driver facts are shown, a missing
// limit is named and filled in by a person, and only a confirmation makes a material 「可用」. The 资料库
// page shows the same states and opens the same result. What the model answered is shown as text.
//   node tests/material_parse_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/material_parse_browser_fixture.py (local SSH fixture, real cloud app, a stand-in for the
// knowledge service; the server needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-material-parse-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `material-parse-${engine.name()}-${name}.png`)});
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const dialog = page.locator('#dialog'), profile = page.locator('#device-profile');
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('#dialog .form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const row = name => profile.locator('.mp-row', {hasText: name});
  const fits = async what => {
    const overflow = await page.evaluate(() => {
      const d = document.querySelector('#dialog');
      return {page: document.documentElement.scrollWidth - window.innerWidth, dialog: d.scrollWidth - d.clientWidth};
    });
    check(overflow.page <= 0 && overflow.dialog <= 0, `${what}: horizontal overflow ${JSON.stringify(overflow)}`);
  };
  const stateOf = async name => (await row(name).locator('.mp-row-state .status').innerText()).trim();
  const waitState = async (name, wanted, timeout = 45000) => {
    const until = Date.now() + timeout;
    while (Date.now() < until) {
      if ((await stateOf(name).catch(() => '')) === wanted) return;
      await profile.locator('[data-parse-action="reload"]').first().click();
      await page.waitForTimeout(1500);
    }
    throw Error(`${name} did not reach ${wanted}; it shows ${await stateOf(name).catch(() => '?')}`);
  };

  try {
    await page.goto(cfg.url);
    // a throwaway account on the local fixture; the password is made up on every run
    await gateSignIn(page, {name: '资料解析验收', email: `parse-${crypto.randomBytes(4).toString('hex')}@example.test`,
                            password: 'pars1-' + crypto.randomBytes(8).toString('hex')});
    await page.locator('[data-action="journey-start"]').first().click();
    await fill('name', '机械臂项目'); await fill('objective', '整理设备资料');
    await submit();
    await page.evaluate(async c => {
      await api('cloud/configure', {base_url: c.cloud});
      await api('cloud/login', {email: c.email, password: c.password});
      await refresh(true);
    }, cfg);

    await page.goto(cfg.url + '/#robots');
    await page.locator('[data-action="engine-ssh"]').first().click();
    await fill('name', '一号机械臂'); await fill('host', '127.0.0.1'); await fill('port', cfg.target_port);
    await page.locator('#dialog [name=family]').selectOption('quadruped');
    await fill('username', cfg.target_user); await fill('password', cfg.target_password);
    await submit();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
    await page.locator('#dialog [data-action="close"]').first().click();

    // 1 · The device profile has a 「资料」 tab; the device has no materials yet.
    const card = page.locator('article.card', {hasText: '一号机械臂'});
    await card.locator('[data-action="engine-profile"]').click();
    await profile.locator('.dp-verdict').waitFor({timeout: 30000});
    await profile.locator('#dp-tab-materials').click();
    await profile.locator('.empty', {hasText: '这台设备还没有资料'}).waitFor({timeout: 15000});
    check((await profile.locator('#dp-tab-materials').getAttribute('aria-selected')) === 'true', 'the 资料 tab is selected');
    await shot('1-empty');

    // 2 · Upload a URDF and a manual from the device profile: the device's type and name go with them.
    await profile.locator('[data-parse-action="upload-open"]').click();
    const form = profile.locator('form[data-parse-form="device-upload"]');
    await form.locator('input[type=file]').setInputFiles([cfg.samples['demo_arm.urdf'], cfg.samples['驱动手册.docx']]);
    await form.locator('[name=vendor]').fill('示例厂家'); await form.locator('[name=model]').fill('A1');
    await form.locator('[name=version]').fill('2.1');
    await form.locator('button[type=submit]').click();
    await row('demo_arm.urdf').waitFor({timeout: 30000});
    await row('驱动手册.docx').waitFor({timeout: 30000});
    const listed = await page.evaluate(async () => (await api('library/list', {})).materials);
    check(listed.length === 2 && listed.every(m => m.device_name === '一号机械臂' && m.body_type === 'quadruped'),
          `materials carry the device: ${JSON.stringify(listed.map(m => [m.device_name, m.body_type]))}`);

    // 3 · The URDF: the first joint's limits are read, the second joint's missing limits are named.
    await waitState('demo_arm.urdf', '没解析出来');
    await row('demo_arm.urdf').locator('[data-parse-action="toggle"]').click();
    const urdf = row('demo_arm.urdf').locator('.mp-panel');
    await urdf.locator('.mp-joints').waitFor({timeout: 15000});
    check(await urdf.locator('[name="pj-0-effort"]').inputValue() === '40', 'the shoulder effort limit is read');
    check(await urdf.locator('[name="pj-0-lower"]').inputValue() === '-1.57', 'the shoulder lower limit is read');
    check((await urdf.innerText()).includes('不会自动生效'), 'the limits are said to be a reference only');
    check((await urdf.innerText()).includes('关节 elbow 的位置下限'), 'the missing limits are named');
    check(await urdf.locator('[data-parse-action="confirm"]').isDisabled() === false, 'confirm is offered (it is refused while items are missing)');
    await urdf.locator('[name="pj-1-lower"]').fill('abc');
    await urdf.locator('[data-parse-action="save"]').click();
    await urdf.locator('.form-error', {hasText: '不是数字'}).waitFor();
    for (const [key, value] of [['lower', '-2.0'], ['upper', '0.5'], ['velocity', '1.2'], ['effort', '25']]) {
      await urdf.locator(`[name="pj-1-${key}"]`).fill(value);
    }
    await shot('2-urdf');
    await urdf.locator('[data-parse-action="confirm"]').click();
    await row('demo_arm.urdf').locator('.mp-row-state', {hasText: '可用'}).waitFor({timeout: 15000});
    check((await row('demo_arm.urdf').locator('.mp-who').innerText()).includes('确认可用'), 'who confirmed is shown');

    // 4 · The manual: the knowledge service's model filled the items; its markup is only text.
    await waitState('驱动手册.docx', '待确认');
    await row('驱动手册.docx').locator('[data-parse-action="toggle"]').click();
    const doc = row('驱动手册.docx').locator('.mp-panel');
    await doc.locator('[name="pf-driver_name"]').waitFor({timeout: 15000});
    check((await doc.locator('[name="pf-driver_name"]').inputValue()).startsWith('demo_arm_driver'), 'the driver name is filled in');
    check(await doc.locator('[name="pf-driver_version"]').inputValue() === '2.1.0', 'the driver version is filled in');
    check((await doc.locator('[name="pf-topics"]').inputValue()).includes('/joint_states sensor_msgs/msg/JointState'), 'the topics');
    check(await page.evaluate(() => !window.__markupRan && !document.querySelector('#dialog img')), 'markup in an answer stays text');
    await doc.locator('[name="pf-source"]').fill('厂家提供的 U 盘');
    await doc.locator('[data-parse-action="confirm"]').click();
    await row('驱动手册.docx').locator('.mp-row-state', {hasText: '可用'}).waitFor({timeout: 15000});
    await fits('device profile at 1440');
    await shot('3-confirmed');

    // 5 · A narrow window: the tab still fits.
    await page.setViewportSize({width: 390, height: 900});
    await page.waitForTimeout(300);
    await fits('device profile at 390');
    await shot('4-narrow');
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('#dialog [data-action="close"]').first().click();

    // 6 · The 资料库 page shows the same states and opens the same result.
    await page.locator('#navigation a[href="#library"]').click();
    const tableRow = page.locator('#material-list tbody tr', {hasText: 'demo_arm.urdf'});
    await tableRow.waitFor({timeout: 15000});
    check((await tableRow.innerText()).includes('可用'), 'the library page shows 可用');
    await tableRow.locator('[data-parse-action="open"]').click();
    await dialog.locator('.mp-joints').waitFor({timeout: 15000});
    check(await dialog.locator('[name="pj-1-effort"]').inputValue() === '25', 'the confirmed values are kept');
    await shot('5-library-dialog');
    check(errors.length === 0, 'page errors: ' + errors.join(' | '));
    console.log(JSON.stringify({browser: engine.name(), ok: true}));
  } catch (err) {
    await shot('failure').catch(() => {});
    console.error('page errors:', errors);
    throw err;
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exit(1); });
