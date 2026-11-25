// Browser acceptance for issue #41, second part: the dialogs behind a robot's buttons in one frame.
// Every dialog of a robot says what it does as its title and which robot under it; a row leads to the
// robot's other dialogs without closing; the head and the actions stay in sight while the content scrolls.
//   node tests/robot_dialogs_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/heartbeat_browser_fixture.py (a local SSH protocol fixture, no real machine; the server
// needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {robotAction, addRobotWay} = require('./robots_page_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const ROBOT = '巡检机器人';
const ROW = ['实时状态', '设备档案', '硬件', '连接', '管理层'];

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-heartbeat-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const main = page.locator('#main'), dialog = page.locator('#dialog[open]');
  const card = () => main.locator('article.card', {hasText: ROBOT});
  const head = dialog.locator('.dialog-head'), row = head.locator('nav.rd-nav');
  const title = () => head.locator('h2').innerText();
  const titled = name => head.locator('h2', {hasText: new RegExp(`^${name}$`)}).waitFor();
  const closeDialog = async () => { await head.locator('[data-action="close"]').click(); await page.locator('#dialog:not([open])').waitFor({state: 'attached'}); };
  const shot = name => page.screenshot({path: `${shots}/robot-dialogs-${name}.png`});
  // Where the head and an element are inside the dialog's box, for "it is in sight".
  const boxes = selector => page.evaluate(sel => {
    const d = document.querySelector('#dialog'), box = el => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; };
    const el = d.querySelector(sel);
    return {dialog: box(d), head: box(d.querySelector('.dialog-head')), el: el ? box(el) : null, scrollTop: d.scrollTop, room: d.scrollHeight - d.clientHeight};
  }, selector);
  const inSight = (b, what) => {
    check(b.el, `${what}: not in the dialog`);
    check(b.el.top >= b.dialog.top - 1 && b.el.bottom <= b.dialog.bottom + 1, `${what}: out of sight (${JSON.stringify(b)})`);
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '对话框验收', email: `dialogs-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'frame1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '对话框整齐一致');
  await submit();
  await page.goto(cfg.url + '/#robots');
  await addRobotWay(page, 'engine-ssh');
  await fill('name', ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.robot_port);
  await page.locator('#dialog [name=family]').selectOption('quadruped');
  await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
  await submit();
  await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await page.locator('#dialog input[type=checkbox]').check();
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});

  // 1. The dialog that opens by itself after connecting is a robot's dialog too: the same head. Nobody
  //    asked for it from a card, so it has no row yet.
  check(await title() === '硬件初始化', `The title after connecting is ${await title()}`);
  check((await head.locator('.rd-robot').innerText()).trim() === ROBOT, 'The head does not say which robot');
  await closeDialog();
  await page.evaluate(() => refresh(true));
  await card().locator('.hb-line').waitFor({timeout: 15000});

  // 2. 实时状态 from the card: what it does is the title, the robot is under it, the rest of the line is kept.
  await robotAction(card(), 'engine-status').click();
  await titled('实时状态');
  check((await head.locator('.rd-robot').innerText()).trim() === ROBOT, 'The head of 实时状态 does not say which robot');
  const rest = await head.locator('.rd-rest').innerText();
  check(rest.includes('每分钟') && rest.includes('只读') && !rest.includes('真机状态'), `The line under the title is ${rest}`);
  check(await head.locator('h2').count() === 1 && await head.locator('[data-action="close"]').count() === 1, 'The head lost its title or its close button');

  // 3. The row: the robot's other dialogs, the open one marked.
  check(await row.getAttribute('aria-label') === '这台机器人的其他功能', 'The row has no name for a screen reader');
  const labels = (await row.locator('button').allInnerTexts()).map(text => text.trim());
  check(JSON.stringify(labels) === JSON.stringify(ROW), `The row is ${JSON.stringify(labels)}`);
  check((await row.locator('[aria-current="true"]').innerText()).trim() === '实时状态', 'The open dialog is not marked in the row');
  await shot('1-live-status');

  // 4. Moving along the row: the dialog stays open, the row moves its mark, the keyboard stays in the dialog.
  await page.evaluate(() => { window.rdClosed = 0; document.querySelector('#dialog').addEventListener('close', () => { window.rdClosed += 1; }); });
  await row.locator('button', {hasText: '设备档案'}).click();
  await titled('设备档案');
  check((await row.locator('[aria-current="true"]').innerText()).trim() === '设备档案', 'The mark did not move to 设备档案');
  check(await row.locator('[aria-current]').count() === 1, 'More than one entry is marked');
  check(await page.evaluate(() => document.querySelector('#dialog').contains(document.activeElement)), 'The keyboard left the dialog after moving along the row');
  await row.locator('button', {hasText: '硬件'}).click();
  await titled('硬件初始化');
  check((await row.locator('[aria-current="true"]').innerText()).trim() === '硬件', 'The mark did not move to 硬件');
  check(await page.evaluate(() => window.rdClosed) === 0, 'The dialog closed on the way');
  // With the keyboard: Tab reaches the row, Enter opens the entry.
  await row.locator('button', {hasText: '实时状态'}).focus();
  await page.keyboard.press('Enter');
  await titled('实时状态');

  // 5. The row survives what a dialog repaints by itself (实时状态 reads again and redraws its content).
  await dialog.locator('[data-action="engine-status-refresh"]').first().click();
  await page.waitForTimeout(1200);
  check(await row.locator('button').count() === ROW.length, 'The row is gone after the dialog redrew itself');
  check(await title() === '实时状态', 'The title changed after the dialog redrew itself');
  check(!(await page.evaluate(() => document.activeElement?.classList.contains('rd-tab'))), 'A dialog that redrew itself moved the keyboard to the row');

  // 6. A long dialog: the head stays in sight when the content is scrolled, and the actions are in
  //    sight before anything is scrolled.
  await row.locator('button', {hasText: '硬件'}).click();
  await titled('硬件初始化');
  let b = await boxes('.hw-next');
  check(b.room > 200, 'The hardware dialog of the fixture is not long enough to scroll');
  inSight(b, 'The actions of 硬件初始化 before scrolling');
  await page.evaluate(() => { const d = document.querySelector('#dialog'); d.scrollTop = d.scrollHeight; });
  b = await boxes('.hw-next');
  check(Math.abs(b.head.top - b.dialog.top) <= 2, `The head scrolled away (${JSON.stringify(b)})`);
  inSight(b, 'The actions of 硬件初始化 at the end');
  inSight(await boxes('.rd-nav'), 'The row at the end of 硬件初始化');
  await shot('2-hardware-end');

  // 7. A long form: its buttons are in sight before anything is scrolled, and a field reached with the
  //    keyboard is not hidden under them or under the head.
  await row.locator('button', {hasText: '连接'}).click();
  await titled('配置连接');
  check((await head.locator('.rd-robot').innerText()).trim() === ROBOT, 'The head of 配置连接, which the form writes itself, does not say which robot');
  check((await row.locator('[aria-current="true"]').innerText()).trim() === '连接', 'The mark did not move to 连接');
  await page.setViewportSize({width: 1440, height: 700});
  b = await boxes('.dialog-footer');
  check(b.room > 100, 'The SSH form is not long enough to scroll at this height');
  inSight(b, 'The buttons of the SSH form before scrolling');
  for (const name of ['host', 'username']) {
    await dialog.locator(`[name="${name}"]`).focus();
    const f = await boxes(`[name="${name}"]`), foot = await boxes('.dialog-footer');
    check(f.el.top >= f.head.bottom - 1 && f.el.bottom <= foot.el.top + 1, `The field ${name} is under the head or the buttons when focused (${JSON.stringify(f)})`);
  }
  await shot('3-ssh-form');
  await page.setViewportSize({width: 1440, height: 900});
  await closeDialog();

  // 8. A dialog outside the row (验证 SSH 连接): the same head, its actions in the same bar, no row.
  await robotAction(card(), 'engine-ssh-verify').click();
  await titled('验证 SSH 连接');
  check((await head.locator('.rd-robot').innerText()).trim() === ROBOT, 'The head of 验证 SSH 连接 does not say which robot');
  await dialog.locator('.cv-actions [data-action="engine-ssh-verify"]').waitFor({timeout: 30000});
  check(await row.count() === 0, 'A dialog that is not in the row has the row');
  const bar = await dialog.locator('.cv-actions').evaluate(el => {
    const d = document.querySelector('#dialog').getBoundingClientRect(), r = el.getBoundingClientRect();
    const buttons = [...el.querySelectorAll('button')].map(x => ({text: x.innerText.trim(), left: x.getBoundingClientRect().left}));
    return {flush: Math.abs(r.bottom - d.bottom) <= 2 && Math.abs(r.left - d.left) <= 2 && Math.abs(r.right - d.right) <= 2, buttons};
  });
  check(bar.flush, 'The actions of 验证 SSH 连接 are not the bar at the foot of the dialog');
  const close = bar.buttons.find(x => x.text === '关闭'), again = bar.buttons.find(x => x.text === '再验证一次');
  check(close && again && close.left < again.left, `关闭 is not before the action in the bar (${JSON.stringify(bar.buttons)})`);
  await shot('4-verify');
  await closeDialog();

  // 9. Deleting: the same head; no row leads away from a confirmation.
  await robotAction(card(), 'engine-robot-remove').click();
  await titled('删除机器人');
  check((await head.locator('.rd-robot').innerText()).trim() === ROBOT, 'The head of 删除机器人 does not say which robot');
  check(await row.count() === 0, 'The confirmation of deleting has the row');
  await closeDialog();

  // 9b. What was asked for is forgotten with the dialog: a robot's dialog that opens without a click on
  //     the robot has no row left over from before.
  await robotAction(card(), 'engine-status').click();
  await titled('实时状态');
  await closeDialog();
  await page.evaluate(name => dialog('设备档案 · ' + h(name), '自己打开的', '<div class="dialog-body"></div>'), ROBOT);
  await titled('设备档案');
  check(await row.count() === 0, 'A dialog nobody asked for from a card has the row of the last one');
  await closeDialog();

  // 9c. A low window: nothing is pinned.
  await page.setViewportSize({width: 900, height: 420});
  await robotAction(card(), 'hardware-open').click();
  await titled('硬件初始化');
  await page.evaluate(() => { const d = document.querySelector('#dialog'); d.scrollTop = d.scrollHeight; });
  b = await boxes('.hw-next');
  check(b.head.bottom <= b.dialog.top + 1, `The head is pinned in a low window (${JSON.stringify(b)})`);
  await closeDialog();
  await page.setViewportSize({width: 1440, height: 900});

  // 10. A dialog that is not about a robot is as it was.
  await page.locator('#main [data-action="engine-robots-add"]').click();
  await titled('添加机器人');
  check(await head.locator('.rd-robot').count() === 0 && await row.count() === 0, 'A dialog that is not about a robot got a robot head');
  await closeDialog();

  // 11. Narrow windows: the row takes a second line, every entry stays in sight, nothing scrolls sideways.
  for (const width of [320, 375, 768]) {
    await page.setViewportSize({width, height: 800});
    await robotAction(card(), 'engine-status').click();
    await titled('实时状态');
    const fit = await page.evaluate(() => { const d = document.querySelector('#dialog'); return {dialog: d.scrollWidth <= d.clientWidth + 1, page: document.documentElement.scrollWidth <= document.documentElement.clientWidth}; });
    check(fit.dialog && fit.page, `The dialog scrolls sideways at ${width}px (${JSON.stringify(fit)})`);
    check(await row.locator('button').count() === ROW.length, `The row is incomplete at ${width}px`);
    const cut = await row.locator('button').evaluateAll(els => { const d = document.querySelector('#dialog').getBoundingClientRect();
      return els.filter(el => { const r = el.getBoundingClientRect(); return r.left < d.left || r.right > d.right; }).map(el => el.innerText.trim()); });
    check(!cut.length, `Entries of the row are cut off at ${width}px: ${cut.join(' ')}`);
    if (width === 320) await shot('5-320');
    await closeDialog();
  }

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('robot_dialogs_browser: passed on ' + engine.name());
})().catch(e => { console.error(e); process.exit(1); });
