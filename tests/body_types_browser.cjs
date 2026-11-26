// Issue #32 browser acceptance: the platform keeps the list of body types.
//   * the list is at the foot of "我的机器人": two built-in types, add a type, remove an unused one;
//   * an added type can be chosen in both forms that add a device, and the seven old values cannot;
//   * a type that devices use is not removed, and the page lists those devices;
//   * a device registered earlier with a type outside the list keeps it, marked "旧类型", until a person
//     changes it; a device that reports another listed type keeps the registered one and says so.
// Fixture: tests/body_types_browser_fixture.py (a local SSH protocol fixture, no real machine).
// Usage: node tests/body_types_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// The name carries markup: it must be shown as text wherever it appears.
const TYPE = '轮式底盘 <b id="markup-in-a-type">', TEMPLATE = 'wheeled.default';
const PLAIN_ROBOT = '已有接入程序的小车', SSH_ROBOT = '巡检小车';
const OLD_VALUES = ['mobile', 'humanoid', 'aerial', 'composite', 'custom'];
const LIST = '#dialog[open] .rp-types', SETTINGS = 'details.rp-settings', CHANGE = '[data-action="engine-body-type"]';
const WIDTHS = [320, 768, 1024, 1440];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-body-types-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `body-types-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const fill = (name, text) => dialog.locator(`[name="${name}"]`).fill(String(text));
  const click = async action => {
    const inDialog = page.locator(`#dialog[open] [data-action="${action}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${action}"]`)).first().click();
  };
  // Submits the open form and waits for the request to end; answers what the form says went wrong, if anything.
  const send = async () => {
    await dialog.locator('button[type=submit]').click();
    await page.waitForFunction(() => {
      const button = document.querySelector('#dialog button[type=submit]');
      return !button || !button.disabled;
    });
    return (await page.locator('#dialog .form-error').allTextContents()).filter(Boolean).join(';');
  };
  const submit = async () => { const error = await send(); if (error) throw Error(error); };
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) must(text.includes(t), `Missing "${t}" in: ${text.slice(0, 600)}`);
  };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const reread = () => page.evaluate(() => refresh(true));         // the page reads a change made behind its back at once
  const robotsPage = async () => { await page.goto(cfg.url + '/#robots'); await page.locator('.page-head h1').waitFor(); await reread(); };
  const card = robotId => page.locator(`article.card:has(${CHANGE}[data-id="${robotId}"])`);
  const list = page.locator(LIST);
  // Since issue #41 the list is a dialog, behind 「设置」 in the head of the page.
  const settings = page.locator(SETTINGS), listHead = page.locator('#dialog[open] .dialog-head');
  const openList = async () => {
    if (await list.count()) return;
    if (!(await settings.evaluate(d => d.open))) await settings.locator('summary').click();
    await settings.locator('[data-action="engine-body-types-open"]').click();
    await list.waitFor();
  };
  const closeList = async () => {
    await list.waitFor();
    await listHead.locator('[data-action="close"]').click();
    await list.waitFor({state: 'detached'});
  };
  const row = name => list.locator('tbody tr', {hasText: name});
  const typeOptions = () => dialog.locator('select[name=family] option').evaluateAll(options => options.map(o => [o.value, o.textContent.trim()]));
  const toastGone = () => page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
  const toast = text => page.locator('#toast.show', {hasText: text}).waitFor();
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '本体类型验收', email: `types-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'types1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await dialog.locator('[name="name"]').fill('园区巡检');
  await dialog.locator('[name="objective"]').fill('接入不同类型的机器人');
  await submit();
  const projectId = (await appState()).projects[0].id;

  // 1 · The list is behind 「设置」 in the head of the page: two built-in types that cannot be removed.
  await robotsPage();
  must(await page.locator('#main details.body-types').count() === 0, 'The list is still a fold of the page');
  await settings.locator('summary').click();
  await expectText(settings.locator('[data-action="engine-body-types-open"]'), ['本体类型清单', '2 类']);
  must(!(await list.count()), 'The list is open before anybody opened it');
  await openList();
  await expectText(row('四足机器人'), ['quadruped.default', 'base.velocity', '底盘速度', '内置']);
  await expectText(row('机械臂'), ['manipulator.default', 'arm.joint_position', '内置']);
  must(await list.locator('[data-action="engine-body-types-remove"]').count() === 0, 'A built-in type offers "删除"');
  await shot('1-built-in');
  result.twoBuiltInTypes = true;

  // 2 · Add a type: a name, the identifier of its state machine template, the interfaces it should have.
  await click('engine-body-types-add');
  await fill('name', TYPE); await fill('state_machine_template', TEMPLATE);
  must((await send()).includes('至少选择一个'), 'A type without an interface is not refused in the form');
  for (const id of ['safety.stop', 'base.velocity', 'base.odometry']) await dialog.locator(`[name=required_capabilities][value="${id}"]`).check();
  await shot('2-add');
  await toastGone();
  await submit();
  await toast('本体类型已添加');
  await list.waitFor();                                          // adding a type leads back to the list
  await expectText(listHead, ['本体类型清单', '3 类']);
  await expectText(row('轮式底盘'), [TYPE, TEMPLATE, 'safety.stop', 'base.velocity', 'base.odometry', '里程计']);
  must(await page.locator('#markup-in-a-type').count() === 0, 'The name of a type is rendered as markup');
  const added = (await appState()).body_types.find(t => t.name === TYPE);
  must(added && !added.builtin && added.state_machine_template === TEMPLATE, 'The added type was not saved as entered');
  // The same name again is refused, in the form.
  await click('engine-body-types-add');
  await fill('name', TYPE); await fill('state_machine_template', TEMPLATE);
  await dialog.locator('[name=required_capabilities][value="safety.stop"]').check();
  await send();
  await dialog.locator('.form-error [data-error-code="body_type_name_taken"]', {hasText: '已经有同名的本体类型'}).waitFor();
  await click('close');
  await closeList();                                             // the form closed: the list is back, and is closed too
  result.typeAdded = true;

  // 3 · Both forms that add a device offer the list, the added type included, and none of the old values.
  const expected = ['', 'quadruped', 'manipulator', added.id];
  for (const [action, name] of [['robot-add', PLAIN_ROBOT], ['engine-ssh', SSH_ROBOT]]) {
    await robotsPage();
    await click(action);
    const options = await typeOptions();
    must(JSON.stringify(options.map(o => o[0])) === JSON.stringify(expected), `${action} offers ${JSON.stringify(options)}`);
    must(options.some(o => o[1] === TYPE), `${action} does not name the added type`);
    must(!options.some(o => OLD_VALUES.includes(o[0])), `${action} still offers an old value`);
    must(await dialog.locator('select[name=family]').inputValue() === '', `${action} chose a type for the person`);
    must(!(await dialog.innerText()).includes('本体形态'), `${action} still shows the old field`);
    await fill('name', name);
    if (action === 'engine-ssh') {
      await fill('host', '127.0.0.1'); await fill('port', cfg.robot_port);
      await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
    }
    // Without a type the form does not go out: the field is required.
    await dialog.locator('button[type=submit]').click();
    must(await dialog.locator('form').evaluate(f => !f.checkValidity()), `${action} can be sent without a type`);
    await dialog.locator('select[name=family]').selectOption(added.id);
    if (action === 'robot-add') await shot('3-add-device');
    await submit();
    if (action === 'engine-ssh') {
      await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
      await dialog.locator('input[type=checkbox]').check();
      await submit();
      await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});      // the scan that follows a connection
      await click('close');
    }
  }
  await robotsPage();
  const robots = (await appState()).robots;
  const plain = robots.find(r => r.name === PLAIN_ROBOT), overSsh = robots.find(r => r.name === SSH_ROBOT);
  must(plain?.family === added.id && overSsh?.family === added.id, 'The devices were not registered with the added type');
  for (const robot of [plain, overSsh]) {
    await require('./robots_page_helper.cjs').openRobotMenu(card(robot.id));                 // issue #41: changing the type is in the card's 更多功能 list
    await expectText(card(robot.id), [robot.name, TYPE, '修改本体类型']);
    await page.keyboard.press('Escape');
    must(await card(robot.id).locator('.body-type-flag').count() === 0, `${robot.name} is flagged although its type is listed`);
  }
  await shot('3-cards');
  result.addedTypeCanBeChosen = true;

  // 4 · A type that devices use is not removed: the page lists them, and so does the server.
  await openList();
  await expectText(row('轮式底盘'), ['2 台']);
  await row('轮式底盘').locator('[data-action="engine-body-types-remove"]').click();
  await expectText(dialog, ['这个类型现在不能删除', '2 台设备', PLAIN_ROBOT, SSH_ROBOT, '园区巡检']);
  must(await dialog.locator('[data-action="engine-body-types-remove-confirm"]').count() === 0, 'A type in use offers a way to remove it');
  await shot('4-in-use');
  await click('close');
  await closeList();
  const refusal = await page.evaluate(id => api('body-types/remove', {body_type_id: id}).then(() => null, e => e.body), added.id);
  must(refusal?.code === 'body_type_in_use', `The server removed a type in use, or refused otherwise: ${JSON.stringify(refusal)}`);
  must(JSON.stringify(refusal.devices.map(d => d.name).sort()) === JSON.stringify([PLAIN_ROBOT, SSH_ROBOT].sort()),
       `The server lists ${JSON.stringify(refusal.devices)}`);
  must((await appState()).body_types.some(t => t.id === added.id), 'The type is gone after the refusal');
  result.typeInUseIsNotRemoved = true;

  // 5 · The type of a registered device is changed by hand; once no device uses a type it can be removed.
  await card(plain.id).locator(CHANGE).click();
  await dialog.getByText('修改本体类型', {exact: true}).waitFor();
  must(await dialog.locator('select[name=family]').inputValue() === added.id, 'The current type is not preselected');
  await dialog.locator('select[name=family]').selectOption('quadruped');
  await toastGone();
  await submit();
  await toast('本体类型已修改');
  await expectText(card(plain.id), ['四足机器人']);
  await page.evaluate(([project_id, robot_id]) => api('robots/body-type', {project_id, robot_id, family: 'manipulator'}), [projectId, overSsh.id]);
  await reread();
  await openList();
  await expectText(row('轮式底盘'), ['—']);
  await row('轮式底盘').locator('[data-action="engine-body-types-remove"]').click();
  await expectText(dialog, ['删除本体类型', '没有设备在用', TYPE]);
  await shot('5-confirm');
  await toastGone();
  await click('engine-body-types-remove-confirm');
  await toast('本体类型已删除');
  await list.waitFor();
  await expectText(listHead, ['2 类']);
  must(await row('轮式底盘').count() === 0, 'The removed type is still listed');
  await closeList();
  must(!(await appState()).body_types.some(t => t.id === added.id), 'The type was not removed');
  result.unusedTypeRemoved = true;

  // 6 · Devices an earlier version left: a type outside the list stays, marked, until a person changes it.
  const earlier = await (await fetch(`${cfg.control}/earlier?project=${encodeURIComponent(projectId)}`)).json();
  await reread();
  await expectText(card(earlier.old), [cfg.old_name, '移动机器人', '旧类型']);
  must((await appState()).robots.find(r => r.id === earlier.old).family === 'mobile', 'The old type was changed by showing it');
  await shot('6-old-type');
  await card(earlier.old).locator(CHANGE).click();
  await expectText(dialog, ['移动机器人', '不在现在的清单里', '不能再改回']);
  must(await dialog.locator('select[name=family]').inputValue() === '', 'A type was chosen for a device with an old type');
  must(!(await typeOptions()).some(o => OLD_VALUES.includes(o[0])), 'The old value is offered again');
  await click('close');
  must((await appState()).robots.find(r => r.id === earlier.old).family === 'mobile', 'Closing the form changed the type');
  await card(earlier.old).locator(CHANGE).click();
  await dialog.locator('select[name=family]').selectOption('manipulator');
  await submit();
  await expectText(card(earlier.old), ['机械臂']);
  must(await card(earlier.old).locator('.body-type-flag').count() === 0, 'A listed type is still marked as old');
  result.oldTypeStaysUntilChanged = true;

  // 7 · A device that reports another listed type: the registered one stays, the card says what was reported,
  //     and one click takes it.
  await expectText(card(earlier.differs), [cfg.differs_name, '四足机器人', '设备自报：机械臂']);
  await card(earlier.differs).locator(CHANGE).click();
  await expectText(dialog, ['设备自报的类型是“机械臂”', '以这里选的为准']);
  await shot('7-reported');
  await toastGone();
  await click('engine-body-type-reported');
  await toast('本体类型已改成“机械臂”');
  await expectText(card(earlier.differs), ['机械臂']);
  must(await card(earlier.differs).locator('.body-type-flag').count() === 0, 'The card still points out a difference');
  must((await appState()).robots.find(r => r.id === earlier.differs).family === 'manipulator', 'The reported type was not taken');
  result.reportedTypeIsOfferedNotForced = true;

  // 8 · The list and the cards fit the window at every width; nothing scrolls sideways.
  await openList();
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    // On a narrow window the navigation slides away: wait until it has.
    await page.waitForFunction(() => innerWidth > 600 || document.querySelector('#sidebar').getBoundingClientRect().right <= 0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    must(overflow <= 1, `The page scrolls sideways by ${overflow}px at ${width}px`);
    await list.scrollIntoViewIfNeeded();
    await shot(`8-width-${width}`);
  }
  await page.setViewportSize({width: 1440, height: 1000});
  result.fitsEveryWidth = true;

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})().catch(async error => { await failed(); console.error(error); process.exit(1); });
