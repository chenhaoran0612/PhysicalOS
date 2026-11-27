// Browser acceptance for issue #41: "我的机器人" laid out again. Without a robot the page is the three
// ways to add one; with a robot it is the robots and one 「添加机器人」 button. A card keeps two buttons
// in sight and everything else in its 「更多功能」 list.
//   node tests/robots_page_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/heartbeat_browser_fixture.py (a local SSH protocol fixture, no real machine; the server
// needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {openRobotMenu, robotEntry, addRobotWay, robotStatus} = require('./robots_page_helper.cjs');
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
  // Issue #53: nothing describes anything. A way to add a robot is its title and its button.
  check(await main.locator('.page-head .eyebrow').innerText() === 'MY ROBOTS' && await main.locator('.page-head .rp-count').count() === 0,
        'The empty page is not titled as every page is (issue #57), or counts robots it does not have');
  check(await main.locator('.robot-entry .journey-option p, .robot-entry .entry-eyebrow').count() === 0, 'A way to add a robot is still described');
  check(await main.locator('.robot-entry .journey-option .button:visible').count() === 3, 'A way to add a robot lost its button');
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
  check((await main.locator('.page-head .rp-count').innerText()).includes('1 台真机'), 'The head does not say how many robots there are');
  check(await main.locator('.page-head button:visible').count() === 1, 'The head has more than the one button');
  check(await main.locator('.page-head details.rp-settings > summary').isVisible(), 'The head has no 设置');
  check(await main.locator('.page-purpose, .rp-folds').count() === 0, 'The page with a robot still has folds of explanations');
  const seen = await inSight(ROBOT);
  check(seen.length === 3 && seen[2] === '更多功能', `The card shows ${JSON.stringify(seen)} instead of two buttons and the list`);
  check(seen[1] === '实时状态', 'The live state is not the second button');
  check(await card(ROBOT).locator('.card-bottom .button:not(.secondary):visible').count() === 1, 'There is not exactly one filled button');
  check(await card(ROBOT).locator('.hb-line .status').isVisible(), 'Online / offline is not in sight');
  // Issue #53: the card is a picture, the name, online or not, the brand and model, and the buttons. Nothing else is in sight.
  const address = `${cfg.robot_user}@127.0.0.1:${cfg.robot_port}`;
  const picture = card(ROBOT).locator('.rp-picture img');
  check(await picture.isVisible() && await picture.evaluate(el => el.complete && el.naturalWidth > 0), 'The card has no picture, or the picture did not load');
  check((await card(ROBOT).locator('.rp-model').innerText()).replace(/\s+/g, ' ').trim() === '示例机器人公司 · X1', 'The card does not show the brand and model');
  const text = await card(ROBOT).evaluate(el => el.innerText);
  for (const hidden of [address, '最近心跳', '四足机器人', '待确认', '控制通道', '开机自启', '可发起调查']) {
    check(!text.includes(hidden), `"${hidden}" is still in sight on the card`);
  }
  check(!/\d/.test(text.replace(ROBOT, '').replace('X1', '')), `A number is in sight on the card: ${JSON.stringify(text)}`);
  // Issue #47: this robot has devices to be confirmed and no fault, so nothing about its hardware is loud.
  check(await card(ROBOT).locator('.status.bad').count() === 0, 'Hardware without a fault is marked as a fault');
  check(await card(ROBOT).locator('.rp-details').count() === 1 && !(await card(ROBOT).locator('.rp-details').isVisible()), 'What the card keeps is in sight');
  await shot('2-one-robot');

  // 2b. All of it is in 「实时状态」, above the live figures: when the last heartbeat was, what and where, the states, start at boot.
  const overview = await robotStatus(page, card(ROBOT));
  await page.locator('#ssh-status').waitFor();
  const told = await overview.innerText();
  for (const shown of [address, '最近心跳', '四足机器人', '示例机器人公司 X1', '项待确认', '控制通道', '开机自启', '可发起调查']) {
    check(told.includes(shown), `"${shown}" is not in 实时状态: ${JSON.stringify(told)}`);
  }
  check(await overview.evaluate(el => el.compareDocumentPosition(document.querySelector('#ssh-status')) & Node.DOCUMENT_POSITION_FOLLOWING), 'The overview is not above the live figures');
  await page.evaluate(() => refresh(true));
  check(await page.locator('#dialog .rp-overview').count() === 1, 'A refresh doubled or dropped the overview');
  // The buttons inside 实时状态 are one height.
  await page.locator('#ssh-status .ls-tab').first().waitFor({timeout: 30000});
  const heights = await page.locator('#dialog .rp-overview button:visible, #ssh-status button:visible').evaluateAll(els => [...new Set(els.map(el => Math.round(el.getBoundingClientRect().height)))]);
  check(heights.length === 1, `The buttons inside 实时状态 have different heights: ${heights.join(', ')}`);
  await shot('2b-live-status');
  await page.locator('#dialog [data-action="close"]').first().click();

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
  check(await panel.locator('.ra-line, .tags, .robot-menu-note').count() === 0, 'The list holds something that is not an entry');
  const entryHeights = await panel.locator('.robot-menu-item').evaluateAll(els => [...new Set(els.map(el => Math.round(el.getBoundingClientRect().height)))]);
  check(entryHeights.length === 1, `The entries of the list have different heights: ${entryHeights.join(', ')}`);
  const cardHeights = await card(ROBOT).locator('.card-bottom .button:visible, .card-bottom summary:visible').evaluateAll(els => [...new Set(els.map(el => Math.round(el.getBoundingClientRect().height)))]);
  check(cardHeights.length === 1, `The buttons of the card have different heights: ${cardHeights.join(', ')}`);
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
  check(await dialog.locator('.journey-option p, .entry-eyebrow').count() === 0 && !(await dialog.locator('.dialog-head p').innerText()).trim(), 'The dialog of 添加机器人 still describes the ways');
  await shot('4-add-dialog');
  await dialog.locator('[data-action="sim-add"]').click();
  await require('./sim_profiles_helper.cjs').pickSimProfile(page, 'quadruped');   // issue #54: type, brand and model
  await dialog.locator('button[type=submit]').click();
  await main.locator('article.sim-card [data-action="sim-live"]').waitFor({timeout: 60000});
  if (await page.locator('#dialog[open] [data-action="close"]').count()) await page.locator('#dialog [data-action="close"]').first().click();
  check((await main.locator('.page-head .rp-count').innerText()).includes('1 台真机 · 1 台仿真'), 'The head does not count real and simulated robots apart');
  const sim = main.locator('article.sim-card');
  const simSeen = await sim.locator('button:visible, summary:visible').evaluateAll(els => els.map(el => el.innerText.trim()));
  check(simSeen.join() === '能力与技能,实时状态,更多功能', `A running simulation shows ${JSON.stringify(simSeen)}`);
  await openRobotMenu(sim);
  check(await sim.locator('.robot-menu-item[data-action="sim-stop"]').isVisible() && await sim.locator('.robot-menu-item[data-action="sim-remove"]').isVisible(),
        'Stopping and deleting a simulation are not in its list');
  // Only one list is open at a time.
  await openRobotMenu(card(ROBOT));
  check(await main.locator('details.robot-menu[open]').count() === 1, 'Two lists are open at once');
  await page.keyboard.press('Escape');
  await shot('5-two-robots');

  // 5. Cards line up: each is a picture, a name with its state, the brand and model, the buttons. Nothing explains.
  const lines = name => card(name).evaluate(el => { const top = s => el.querySelector(s)?.getBoundingClientRect().top - el.getBoundingClientRect().top;
    return {picture: top('.rp-picture'), name: top('h3'), model: top('.rp-model'), bottom: top('.card-bottom'), height: el.getBoundingClientRect().height}; });
  const a = await lines(ROBOT), b = await lines('仿真四足机器人');
  for (const key of ['picture', 'name', 'bottom', 'height']) {
    check(Number.isFinite(a[key]) && Math.abs(a[key] - b[key]) <= 1, `The two cards do not line up at "${key}": ${JSON.stringify([a, b])}`);
  }
  const simText = await sim.evaluate(el => el.innerText);
  check(!simText.includes('地址和令牌') && !simText.includes('现场证据') && !simText.includes('运行在这台电脑上'), 'A note that only explains is still on the simulated robot\'s card');
  check((await sim.locator('.rp-title .status').innerText()).trim() === '运行中', 'Whether the simulation runs is not next to its name');
  check((await sim.locator('.rp-model').innerText()).replace(/\s+/g, ' ').trim() === 'neuxmind-sim · sim-quadruped', 'The simulated robot does not show its brand and model');
  check(await sim.locator('.rp-picture img').evaluate(el => el.complete && el.naturalWidth > 0), 'The simulated robot has no picture');
  await openRobotMenu(sim);
  check(await sim.locator('.robot-menu-notes, .robot-menu-note').count() === 0, 'The list of the simulated robot still explains');
  await page.keyboard.press('Escape');
  // Its 实时状态 has what its card kept, too.
  const simOverview = await robotStatus(page, sim);
  check((await simOverview.innerText()).includes('仿真'), 'The overview of the simulated robot does not say it is simulated');
  await page.locator('#dialog [data-action="close"]').first().click();

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
