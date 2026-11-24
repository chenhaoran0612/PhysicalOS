// Issue #11 browser acceptance: a real robot's card on "我的机器人" has "删除". The confirmation says
// what goes (the SSH connection with its saved login, investigations, hardware scans) and what stays;
// deleting one of two cards of the same machine leaves the other one working; a robot that is being
// investigated is not deleted; a robot with a service address only can be deleted too.
// Fixture: tests/robot_remove_browser_fixture.py (a local SSH protocol fixture, no real machine).
// Usage: node tests/robot_remove_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// The second name must be shown as text wherever it appears: it carries markup.
const SSH_ROBOT = '巡检狗', PLAIN_ROBOT = '已有接入程序的机器人 <b id="markup-in-a-name">';
const REMOVE = '[data-action="engine-robot-remove"]', CONFIRM = '[data-action="engine-robot-remove-confirm"]';
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-remove-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `robot-remove-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const fill = (name, text) => dialog.locator(`[name="${name}"]`).fill(String(text));
  const click = async action => {
    const inDialog = page.locator(`#dialog[open] [data-action="${action}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${action}"]`)).first().click();
  };
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    // The page disables the submit button for the length of the request: enabled again, or replaced, means it is over.
    await page.waitForFunction(() => {
      const button = document.querySelector('#dialog button[type=submit]');
      return !button || !button.disabled;
    });
    const error = (await page.locator('#dialog .form-error').allTextContents()).filter(Boolean).join(';');
    if (error) throw Error(error);
  };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 600)}`);
  };
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const control = async (what, robotId, extra = '') =>
    (await fetch(`${cfg.control}/${what}?robot=${encodeURIComponent(robotId)}${extra}`)).json();
  const reread = () => page.evaluate(() => refresh(true));         // the page reads a change made behind its back at once
  const card = robotId => page.locator(`article.card:has(${REMOVE}[data-id="${robotId}"])`);
  const robotsPage = async () => { await page.goto(cfg.url + '/#robots'); await page.locator('.page-head h1').waitFor(); await reread(); };
  const openRemove = async robotId => {
    await card(robotId).locator(REMOVE).click();
    await dialog.getByText('删除机器人', {exact: true}).waitFor();
  };
  // Confirms the open dialog and waits for the deletion to show: the card goes, the dialog closes, the page says so.
  // (The card is the signal: a toast of an earlier deletion can still be on screen.)
  const confirmRemoved = async robotId => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));   // not the toast of an earlier step
    await dialog.locator(CONFIRM).click();
    await card(robotId).waitFor({state: 'detached'});
    await page.locator('#toast.show', {hasText: '机器人已删除'}).waitFor();
    must(!(await dialog.evaluate(d => d.open)), 'The confirmation stays open after the deletion');
  };
  const addOverSsh = async () => {
    await page.goto(cfg.url + '/#robots');
    await click('engine-ssh');
    await fill('name', SSH_ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.robot_port);
    await page.locator('#dialog [name=family]').selectOption('quadruped');   // issue #32: a new device needs a type from the list
    await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
    await submit();
    await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
    await dialog.locator('input[type=checkbox]').check();
    await submit();
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});      // the scan that follows a connection
    await click('close');
  };
  const noSecretsVisible = async where => {
    const seen = await page.content() + JSON.stringify(await appState());
    must(!seen.includes(cfg.robot_login), `A saved login is visible ${where}`);
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '删除机器人验收', email: `remove-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'remove1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await dialog.locator('[name="name"]').fill('巡检机器人接入');
  await dialog.locator('[name="objective"]').fill('接入真机，去掉重复添加的机器人');
  await submit();

  // The case of the issue: the same machine was added twice. Plus a robot with a service address only.
  await addOverSsh();
  await addOverSsh();
  await page.goto(cfg.url + '/#robots');
  await click('robot-add');
  await fill('name', PLAIN_ROBOT); await fill('endpoint', 'http://127.0.0.1:9');
  await page.locator('#dialog [name=family]').selectOption('quadruped');   // issue #32: a new device needs a type from the list
  await submit();
  const robots = (await appState()).robots.sort((a, b) => a.created_at.localeCompare(b.created_at));
  const [first, second] = robots.filter(r => r.name === SSH_ROBOT), plain = robots.find(r => r.name === PLAIN_ROBOT);
  must(first && second && plain && first.connection_id !== second.connection_id, 'The fixture robots were not all added');

  // 1 · Every real robot's card ends with "删除", named for screen readers.
  await robotsPage();
  for (const robot of [first, second, plain]) {
    const buttons = card(robot.id).locator('.card-bottom [data-action]');
    must(await buttons.last().getAttribute('data-action') === 'engine-robot-remove', `"删除" is not the last button of ${robot.name}`);
    must(await buttons.last().getAttribute('aria-label') === '删除机器人 ' + robot.name, 'The button does not name its robot');
    must((await buttons.last().innerText()).trim() === '删除', 'The button is not labelled 删除');
  }
  must(await page.locator('article.card').count() === 3, 'Expected three cards');
  await shot('1-cards');
  result.everyRealRobotCanBeDeleted = true;

  // 2 · The confirmation: what goes, what stays, and nothing happens on "取消".
  await control('investigated', first.id);
  await reread();
  await openRemove(first.id);
  await expectText(dialog, [SSH_ROBOT, `SSH 连接 ${cfg.robot_user}@127.0.0.1:${cfg.robot_port}`, '登录信息', '1 次调研',
                            '设备证据', '硬件扫描结果', '不会登录机器人', '删除后不能恢复']);
  must(await dialog.locator('.robot-remove-list li').count() === 3, 'Expected the connection, the investigation and the hardware scan');
  must(!(await dialog.innerText()).includes('修复'), 'A repair is named although the robot has none');
  await noSecretsVisible('in the confirmation');
  await shot('2-confirm');
  await click('close');
  must(!(await dialog.evaluate(d => d.open)), 'The confirmation stays open after 取消');
  must((await control('left', first.id)).robots === 1, '取消 deleted the robot');
  result.confirmationSaysWhatGoes = true;

  // 3 · While the robot is being investigated it is not deleted; the dialog leads to that run.
  await control('busy', first.id, '&on=1');
  await reread();
  await openRemove(first.id);
  await expectText(dialog, ['调查会话正在运行', '先停止它']);
  must(await dialog.locator(CONFIRM).count() === 0, 'A robot under investigation can be deleted');
  must(await dialog.locator('[data-action="engine-session"]').count() === 1, 'No way to the running session');
  await shot('3-busy');
  await click('close');
  await control('busy', first.id, '&on=0');
  await reread();
  result.refusedWhileInvestigated = true;

  // 4 · An investigation that started after the confirmation opened: the server refuses, the page says why.
  await openRemove(first.id);
  await control('busy', first.id, '&on=1');
  await dialog.locator(CONFIRM).click();
  await dialog.locator('.notice', {hasText: '调查会话正在运行'}).waitFor();
  must(await dialog.locator(CONFIRM).count() === 0, 'The refused deletion can be confirmed again while the run goes on');
  must(await dialog.locator('[data-action="engine-session"]').count() === 1, 'No way to the session that started meanwhile');
  must((await control('left', first.id)).robots === 1, 'The robot was deleted although an investigation was running');
  await shot('4-refused');
  await click('close');
  await control('busy', first.id, '&on=0');
  await reread();
  // A refusal the page cannot foresee (the robot's hardware is being scanned) is shown in the confirmation itself.
  await openRemove(first.id);
  await control('scanning', first.id, '&on=1');
  await dialog.locator(CONFIRM).click();
  await dialog.locator('.form-error', {hasText: '正在扫描硬件'}).waitFor();
  must(await dialog.locator(CONFIRM).isEnabled(), 'After a refusal the deletion cannot be tried again');
  must((await control('left', first.id)).robots === 1, 'The robot was deleted although its hardware was being scanned');
  await shot('4-refused-scan');
  await click('close');
  await control('scanning', first.id, '&on=0');
  result.serverRefusalShown = true;

  // 5 · Deleting one of the two cards: it is gone with everything kept about it; the other one still works.
  const before = await control('left', first.id);
  must(before.device_connections === 1 && before.agent_sessions === 1 && before.device_evidence === 1 &&
       before.hardware_scans === 1 && before.files === 1, 'Fixture records are missing: ' + JSON.stringify(before));
  await openRemove(first.id);
  await confirmRemoved(first.id);
  must(await page.locator('article.card').count() === 2, 'Expected two cards after the deletion');
  const left = await control('left', first.id);
  must(Object.values(left).every(n => n === 0), 'Something of the robot is left: ' + JSON.stringify(left));
  const state = JSON.stringify({...(await appState()), events: []});
  must(!state.includes(first.id) && !state.includes(first.connection_id), 'The state still names the deleted robot');
  const kept = await control('left', second.id);
  must(kept.robots === 1 && kept.device_connections === 1 && kept.hardware_scans === 1, 'The other card lost records: ' + JSON.stringify(kept));
  await card(second.id).locator('[data-action="engine-status"]').click();
  await page.locator('#ssh-status').getByText('在线', {exact: true}).waitFor({timeout: 20000});
  await click('close');
  await noSecretsVisible('after the deletion');
  await shot('5-one-left');
  result.oneOfTwoCardsDeleted = true;

  // 6 · A robot with a service address only.
  await robotsPage();
  await openRemove(plain.id);
  await expectText(dialog, [PLAIN_ROBOT, '服务地址 http://127.0.0.1:9', '不会登录机器人']);
  must(!(await dialog.innerText()).includes('SSH 连接'), 'A robot without SSH is said to have an SSH connection');
  must(await page.locator('#markup-in-a-name').count() === 0, 'Markup in a robot name became part of the page');
  await shot('6-plain');
  await confirmRemoved(plain.id);
  must((await control('left', plain.id)).robots === 0, 'The plain robot is still there');
  result.plainRobotDeleted = true;

  // 7 · The last one: the page goes back to "add a robot".
  await openRemove(second.id);
  await confirmRemoved(second.id);
  must(await page.locator('article.card').count() === 0, 'A card is left after deleting every robot');
  await expectText(page.locator('#main'), ['选择一种方式添加机器人']);
  must((await appState()).robots.length === 0, 'A robot is left in the state');
  await shot('7-empty');
  result.lastRobotDeleted = true;

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
