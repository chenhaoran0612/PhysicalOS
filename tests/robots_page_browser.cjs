// Browser acceptance for issue #41: "我的机器人" laid out again. Without a robot the page is the three
// ways to add one; with a robot it is the robots and one 「添加机器人」 button. A card keeps two buttons
// in sight and everything else in its 「更多功能」 list.
//   node tests/robots_page_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/heartbeat_browser_fixture.py (a local SSH protocol fixture, no real machine; the server
// needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {openRobotMenu, robotEntry, addRobotWay} = require('./robots_page_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const ROBOT = '巡检机器人';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-heartbeat-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const card = name => main.locator('article.card', {hasText: name});
  const menu = name => card(name).locator('details.robot-menu');
  const inSight = name => card(name).locator('button:visible, summary:visible').evaluateAll(els => els.map(el => el.innerText.trim()));
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth &&
    document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth);
  const shot = name => page.screenshot({path: `${shots}/robots-page-${name}.png`, fullPage: true});

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '页面验收', email: `robots-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'page1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '页面只显示现在用得上的东西');
  await submit();

  // 1. No robot yet: the three ways to add one are the page. Nothing else asks for attention.
  await page.goto(cfg.url + '/#robots');
  await main.locator('.robot-entry').waitFor();
  check(await main.locator('.robot-entry .journey-option').count() === 3, 'An empty page does not show the three ways to add a robot');
  check(await main.locator('.page-head button:visible').count() === 0, 'The empty page repeats the three ways as buttons in its head');
  check(await main.locator('article.card').count() === 0, 'A card is shown without a robot');
  check(await main.locator('details[open]').count() === 0, 'Something is open before anyone opened it');
  check(await main.locator('.page-purpose, .rp-folds').count() === 0, 'The page still has folds of explanations');
  check(await main.locator('h1').innerText() === '我的机器人' && await main.locator('.page-head .subtitle, .section-head').count() === 0,
        'The empty page explains itself above the three ways');
  check(await main.locator('.page-head details.rp-settings').count() === 1, 'The empty page has no 设置');
  check(!(await main.locator('.notice:visible').count()), 'A notice is in sight on the empty page');
  check(await noSidewaysScroll(), 'The empty page scrolls sideways');
  await shot('1-empty');

  // 2. A robot over SSH. The page is now the robot: the three ways are gone from it.
  await addRobotWay(page, 'engine-ssh');
  await fill('name', ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.robot_port);
  await page.locator('#dialog [name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #48: a new robot needs a brand and model
  await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
  await submit();
  await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await page.locator('#dialog input[type=checkbox]').check();
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
  await page.locator('#dialog [data-action="close"]').first().click();
  await page.evaluate(() => refresh(true));
  await card(ROBOT).locator('.hb-line').waitFor({timeout: 15000});
  check(await main.locator('.robot-entry').count() === 0, 'The three ways to add a robot still fill the page once there is a robot');
  check(await main.locator('h1').innerText() === '我的机器人', 'The page with a robot is not titled 我的机器人');
  check((await main.locator('.page-head .eyebrow').innerText()).includes('1 台真机'), 'The head does not say how many robots there are');
  check(await main.locator('.page-head button:visible').count() === 1, 'The head has more than the one button');
  check(await main.locator('.page-head details.rp-settings > summary').isVisible(), 'The head has no 设置');
  check(await main.locator('.page-purpose, .rp-folds').count() === 0, 'The page with a robot still has folds of explanations');
  const seen = await inSight(ROBOT);
  check(seen.length === 3 && seen[2] === '更多功能', `The card shows ${JSON.stringify(seen)} instead of two buttons and the list`);
  check(seen[1] === '实时状态', 'The live state is not the second button');
  check(await card(ROBOT).locator('.card-bottom .button:not(.secondary):visible').count() === 1, 'There is not exactly one filled button');
  check(await card(ROBOT).locator('.hb-line .status').isVisible(), 'Online / offline is not in sight');
  check(await card(ROBOT).getByText(`${cfg.robot_user}@127.0.0.1:${cfg.robot_port}`).isVisible(), 'The address is not in sight');
  // Issue #47: this robot has devices to be confirmed and no fault, so nothing about its hardware is loud.
  check(await card(ROBOT).locator('.card-body .tags .status.bad').count() === 0, 'Hardware without a fault is shown in red');
  check((await card(ROBOT).locator('.robot-menu .tag', {hasText: '项待确认'}).count()) === 1, 'What is to be confirmed is not in the list');
  check(!(await card(ROBOT).locator('.card-body > .tags .tag').count()), 'A plain tag is still in the card body');
  check(!(await card(ROBOT).locator('.ra-line').isVisible()), 'Start at boot is in sight before the list is opened');
  await shot('2-one-robot');

  // 3. The list: everything else, grouped; deleting last and apart. It hangs out of the card and is not cut.
  await openRobotMenu(card(ROBOT));
  const panel = menu(ROBOT).locator('.robot-menu-panel');
  const titles = await panel.locator('.robot-menu-title').allInnerTexts();
  check(['连接', '设备', '管理层'].every(t => titles.includes(t)), `The list is not grouped: ${titles.join(',')}`);
  const entries = await panel.locator('.robot-menu-item').evaluateAll(els => els.map(el => el.dataset.action));
  for (const action of ['engine-ssh-verify', 'engine-ssh-edit', 'engine-profile', 'hardware-open', 'engine-body-type',
                        'engine-runtime-link', 'engine-runtime-install', 'engine-robot-remove']) {
    check(entries.includes(action), `${action} is not in the list`);
  }
  check(entries[entries.length - 1] === 'engine-robot-remove', 'Deleting is not the last entry');
  check(await panel.locator('.ra-line').isVisible(), 'Start at boot is not in the list');
  const reachable = await panel.locator('.robot-menu-item').evaluateAll(els => els.every(el => {
    el.scrollIntoView({block: 'nearest'});
    const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit === el || el.contains(hit);
  }));
  check(reachable, 'An entry of the list is cut off or covered');
  check(await noSidewaysScroll(), 'The open list makes the page scroll sideways');
  await shot('3-list-open');
  // The page is drawn again while the list is open (a poll): it stays open.
  await page.evaluate(() => refresh(true));
  check(await menu(ROBOT).evaluate(el => el.open), 'The list closed when the page was drawn again');
  // Esc closes it and the focus is back on its button.
  await page.keyboard.press('Escape');
  check(!(await menu(ROBOT).evaluate(el => el.open)), 'Esc does not close the list');
  check(await menu(ROBOT).locator('summary').evaluate(el => el === document.activeElement), 'After Esc the focus is not on 更多功能');
  // The keyboard opens it too.
  await page.keyboard.press('Enter');
  check(await menu(ROBOT).evaluate(el => el.open), 'Enter on 更多功能 does not open the list');
  // A click elsewhere closes it.
  await main.locator('h1').click();
  check(!(await menu(ROBOT).evaluate(el => el.open)), 'A click elsewhere does not close the list');
  // An entry does what its button did, and the list is closed behind it.
  await robotEntry(card(ROBOT), 'engine-ssh-edit').click();
  await dialog.locator('form[data-form="engine-ssh"]').waitFor();
  check(!(await menu(ROBOT).evaluate(el => el.open)), 'The list stays open behind the dialog of its entry');
  await page.locator('#dialog [data-action="close"]').first().click();
  // The entry the dialog would give the focus back to is put away: the focus is on the list's button, not lost.
  await page.waitForFunction(() => document.activeElement?.matches?.('details.robot-menu > summary'), null, {timeout: 5000})
    .catch(() => { throw Error('After the dialog of an entry closed the focus is not on 更多功能'); });
  // Tab past the last entry: the list closes instead of covering what is focused next.
  await openRobotMenu(card(ROBOT));
  await menu(ROBOT).locator('.robot-menu-item').last().focus();
  await page.keyboard.press(engine.name() === 'webkit' ? 'Alt+Tab' : 'Tab');
  check(!(await menu(ROBOT).evaluate(el => el.open)), 'The list stays open after the keyboard left it');
  await robotEntry(card(ROBOT), 'engine-robot-remove').click();
  await dialog.getByText('删除机器人', {exact: true}).first().waitFor();
  await page.locator('#dialog [data-action="close"]').first().click();
  // The buttons in sight still work.
  await card(ROBOT).locator('[data-action="engine-status"]').click();
  await page.locator('#ssh-status').waitFor();
  await page.locator('#dialog [data-action="close"]').first().click();

  // 4. 「添加机器人」: the three ways, in a dialog. A simulated robot gets a card of the same shape.
  await main.locator('[data-action="engine-robots-add"]').click();
  check(await dialog.locator('.journey-option').count() === 3, 'The dialog of 添加机器人 does not offer the three ways');
  await shot('4-add-dialog');
  await dialog.locator('[data-action="sim-add"]').click();
  await dialog.locator('button[type=submit]').click();
  await main.locator('article.sim-card [data-action="sim-live"]').waitFor({timeout: 60000});
  if (await page.locator('#dialog[open] [data-action="close"]').count()) await page.locator('#dialog [data-action="close"]').first().click();
  check((await main.locator('.page-head .eyebrow').innerText()).includes('1 台真机 · 1 台仿真'), 'The head does not count real and simulated robots apart');
  const sim = main.locator('article.sim-card');
  const simSeen = await sim.locator('button:visible, summary:visible').evaluateAll(els => els.map(el => el.innerText.trim()));
  check(simSeen.join() === '实时状态,能力与技能,更多功能', `A running simulation shows ${JSON.stringify(simSeen)}`);
  await openRobotMenu(sim);
  check(await sim.locator('.robot-menu-item[data-action="sim-stop"]').isVisible() && await sim.locator('.robot-menu-item[data-action="sim-remove"]').isVisible(),
        'Stopping and deleting a simulation are not in its list');
  // Only one list is open at a time.
  await openRobotMenu(card(ROBOT));
  check(await main.locator('details.robot-menu[open]').count() === 1, 'Two lists are open at once');
  await page.keyboard.press('Escape');
  await shot('5-two-robots');

  // 5. Cards line up: each is a name, a line of state, one line of what and where. What only explains
  //    is in the list.
  const lines = name => card(name).evaluate(el => { const top = s => el.querySelector(s)?.getBoundingClientRect().top - el.getBoundingClientRect().top;
    return {name: top('h3'), state: top('.hb-line, .rp-state'), what: top('.rp-what'), whatLines: el.querySelector('.rp-what')?.getClientRects().length,
            bottom: top('.card-bottom'), height: el.getBoundingClientRect().height}; });
  const a = await lines(ROBOT), b = await lines('仿真四足机器人');
  for (const key of ['name', 'state', 'what', 'bottom', 'height']) {
    check(Number.isFinite(a[key]) && Math.abs(a[key] - b[key]) <= 1, `The two cards do not line up at "${key}": ${JSON.stringify([a, b])}`);
  }
  check(!(await sim.locator('.card-body .inline-note').count()) && !(await sim.locator('.card-body').innerText()).includes('地址和令牌'),
        'A note that only explains is still on the simulated robot\'s card');
  check(await sim.locator('.card-top .status').count() === 1 && (await sim.locator('.rp-state').innerText()).trim() === '运行中',
        'Whether the simulation runs is not the line under its name');
  await openRobotMenu(sim);
  check((await sim.locator('.robot-menu-notes').innerText()).includes('不能作为现场证据'), 'The note about simulated results is not in the list');
  await page.keyboard.press('Escape');

  // 5b. 「设置」 in the head: the two settings that are not a robot's, each in a dialog. It stays open
  //     when the page is drawn again, and closes like a card's list.
  const settings = main.locator('.page-head details.rp-settings');
  await settings.locator('summary').click();
  const offered = await settings.locator('.robot-menu-item').evaluateAll(els => els.map(el => el.dataset.action));
  check(offered.join() === 'engine-models,engine-body-types-open', `设置 offers ${offered.join()}`);
  const box = await settings.locator('.robot-menu-panel').boundingBox();
  check(box && box.x >= 0 && box.x + box.width <= 1440, 'The list of 设置 leaves the window');
  await page.evaluate(() => refresh(true));
  check(await settings.evaluate(el => el.open), '设置 closed when the page was drawn again');
  await openRobotMenu(card(ROBOT));
  check(!(await settings.evaluate(el => el.open)), '设置 stayed open next to a card\'s list');
  await page.keyboard.press('Escape');
  await settings.locator('summary').click();
  await settings.locator('[data-action="engine-body-types-open"]').click();
  await dialog.locator('.rp-types tbody tr').first().waitFor();
  check((await dialog.locator('.dialog-head h2').innerText()) === '本体类型清单', 'The list of body types is not a dialog of its own');
  check(!(await settings.evaluate(el => el.open)), '设置 stayed open behind its dialog');
  await page.locator('#dialog [data-action="close"]').first().click();
  await settings.locator('summary').click();
  await settings.locator('[data-action="engine-models"]').click();
  await page.locator('#dialog[open] .dialog-head').waitFor();
  await page.locator('#dialog [data-action="close"]').first().click();
  await shot('5b-settings');

  // 6. Narrow windows: nothing scrolls sideways, and the list stays inside the window.
  for (const width of [320, 375, 768, 1024, 1920]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(150);
    check(await noSidewaysScroll(), `The page scrolls sideways at ${width}px`);
    await openRobotMenu(card(ROBOT));
    const box = await menu(ROBOT).locator('.robot-menu-panel').boundingBox();
    check(box && box.x >= 0 && box.x + box.width <= width, `The list leaves the window at ${width}px (${JSON.stringify(box)})`);
    check(await noSidewaysScroll(), `The open list makes the page scroll sideways at ${width}px`);
    if ([320, 768].includes(width)) await shot(`6-${width}`);
    await page.keyboard.press('Escape');
  }

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('robots_page_browser: passed on ' + engine.name());
})().catch(e => { console.error(e); process.exit(1); });
