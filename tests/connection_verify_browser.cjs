// Browser acceptance for issue #16 (R1-02): 「验证 SSH 连接」 on the card of a robot that is already added.
// It logs in to every hop of the saved SSH connection once and shows the system, the architecture and
// how long it took; when it does not get through it says at which hop it got stuck; while the robot is
// being onboarded the button is not offered. Direct, through one jump host and through three.
//   node tests/connection_verify_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/connection_verify_browser_fixture.py (local SSH protocol fixtures, no real machine; the
// server needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the
// desktop app; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const VERIFY_API = '/api/engineering/ssh/verify';
const DIRECT = '直连的机器人', ONE = '一级跳板机后的机器人', THREE = '三级跳板机后的机器人';
const WIDTHS = [320, 768, 1024, 1440];

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-connection-verify-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const sent = [];                                    // every verification the page asks for
  page.on('request', r => { if (r.url().endsWith(VERIFY_API)) sent.push(r.postDataJSON()); });
  let step = 'start';

  const dialog = page.locator('#dialog'), body = page.locator('#cv-body');
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const control = async path => (await fetch(cfg.control + path)).json();
  const card = name => page.locator('article.card', {hasText: name});
  const verifyButton = name => card(name).locator('[data-action="engine-ssh-verify"]');
  const text = async locator => (await locator.innerText()).replace(/\s+/g, ' ');
  const expectText = async (locator, parts, what) => {
    const shown = await text(locator);
    for (const part of parts) {
      check(part instanceof RegExp ? part.test(shown) : shown.includes(part), `${what}: missing ${part} in "${shown.slice(0, 500)}"`);
    }
    return shown;
  };
  const hops = () => body.locator('.cv-hop');
  const hopStates = async () => hops().evaluateAll(rows => rows.map(row => row.className.replace('cv-hop cv-', '')));
  const fact = async label => (await body.locator('.cv-facts div', {hasText: label}).locator('dd').innerText()).trim();
  const closeDialog = () => page.locator('#dialog [data-action="close"]').first().click();
  // Nothing inside the dialog reaches past its sides (the page itself clips: its own width says nothing).
  const noSidewaysScroll = () => page.evaluate(() => {
    const dialog = document.querySelector('#dialog'), box = dialog.getBoundingClientRect();
    const sticking = [...dialog.querySelectorAll('#cv-body *')].filter(el => {
      const r = el.getBoundingClientRect();
      return r.width && (r.left < box.left - 1 || r.right > box.right + 1);
    });
    return dialog.scrollWidth <= dialog.clientWidth && !sticking.length;
  });
  // The dialog at the widths the product is checked at: nothing is cut off sideways.
  const atEveryWidth = async name => {
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      check(await noSidewaysScroll(), `${name}: the dialog scrolls sideways at ${width}px`);
      await page.screenshot({path: `${shots}/connection-verify-${name}-${width}.png`});
    }
    await page.setViewportSize({width: 1440, height: 1000});
  };
  const confirmFingerprint = async title => {
    await dialog.getByText(title, {exact: true}).waitFor();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
  };
  // Connect a robot over SSH through the given jump hosts; the read-only hardware scan that follows is closed.
  const connect = async (name, gates) => {
    await page.locator('[data-action="engine-ssh"]').first().click();
    await fill('name', name); await fill('host', '127.0.0.1'); await fill('port', cfg.robot.port);
    await page.locator('#dialog [name=family]').selectOption('quadruped');   // issue #32: a new device needs a type from the list
    await fill('username', cfg.robot.user); await fill('password', cfg.robot.login);
    if (gates.length) {
      await page.locator('#dialog [name=route]').selectOption('jump');
      for (let level = 1; level <= gates.length; level += 1) {
        const prefix = level === 1 ? 'jump_' : `jump${level}_`, gate = gates[level - 1];
        if (level > 1) await page.locator('#ssh-hop-add').click();
        await fill(prefix + 'host', '127.0.0.1'); await fill(prefix + 'port', gate.port);
        await fill(prefix + 'username', gate.user); await fill(prefix + 'password', gate.login);
      }
    }
    await submit();
    for (let level = 1; level <= gates.length; level += 1) {
      await confirmFingerprint(gates.length > 1 ? `第 ${level} 步：确认第 ${level} 级跳板机指纹` : '第 1 步：确认跳板机指纹');
    }
    await confirmFingerprint(gates.length ? `第 ${gates.length + 1} 步：确认目标机指纹` : '确认 SSH 主机指纹');
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 40000});      // issue #2: the scan after connecting
    await closeDialog();
    await page.evaluate(() => refresh(true));
    await verifyButton(name).waitFor({timeout: 15000});
  };
  // Press 「验证连接」 on a card and wait for the outcome: 'passed', 'failed' or 'refused'.
  const outcome = async () => {
    await page.locator('#cv-body:not([data-cv-state="running"])[data-cv-state]').waitFor({timeout: 90000});
    return body.getAttribute('data-cv-state');
  };
  // The page has asked for its nth verification, and that one has ended.
  const sentCount = async n => {
    for (let waited = 0; sent.length < n && waited < 15000; waited += 50) await page.waitForTimeout(50);
    check(sent.length === n, `Expected ${n} verifications to be asked for, saw ${sent.length}`);
    await page.waitForFunction(() => cvRuns.size === 0, null, {timeout: 90000});
  };
  const verify = async name => {
    await verifyButton(name).click();
    await body.waitFor();
    return outcome();
  };

  try {
    await page.goto(cfg.url);
    // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
    await gateSignIn(page, {name: '验证连接验收', email: `verify-${crypto.randomBytes(4).toString('hex')}@example.test`,
                            password: 'verify1-' + crypto.randomBytes(8).toString('hex')});
    await page.locator('[data-action="journey-start"]').first().click();
    await fill('name', '园区巡检'); await fill('objective', '随时确认每台机器人的 SSH 连接还通不通');
    await submit();
    await page.goto(cfg.url + '/#robots');
    await connect(DIRECT, []);
    await connect(ONE, [cfg.jump]);
    await connect(THREE, cfg.chain);
    const connected = await control('/seen');
    check(connected.verifications === 0, 'Connecting a robot already ran a verification');

    // 1. The button: on the card of every robot with an SSH connection, in front of 「配置连接」, offered.
    step = '1';
    for (const name of [DIRECT, ONE, THREE]) {
      check(await verifyButton(name).count() === 1, `${name}: expected one 「验证连接」 on the card`);
      check((await verifyButton(name).innerText()).trim() === '验证 SSH 连接', `${name}: the button is not called 验证 SSH 连接`);
      check(await verifyButton(name).getAttribute('aria-disabled') === null, `${name}: the button is held although nothing runs`);
      check(await card(name).locator('[data-action="engine-ssh-verify"] + [data-action="engine-ssh-edit"]').count() === 1,
            `${name}: 验证连接 is not right in front of 配置连接`);
      check((await verifyButton(name).getAttribute('title')).includes('SSH'), `${name}: the hint does not say SSH`);
    }

    // 2. Direct: passes, and shows the system, the architecture and how long it took. One login, one command.
    step = '2';
    check(await verify(DIRECT) === 'passed', 'The direct connection did not pass: ' + await text(body));
    await expectText(dialog.locator('.dialog-head'), [DIRECT, '验证 SSH 连接'], 'The head of the dialog');
    check(await page.evaluate(() => document.activeElement?.id) === 'cv-headline', 'The focus is not on the outcome');
    await expectText(body, ['SSH 连接验证通过', '只读'], 'A verification that passed');
    check(await fact('系统') === 'Ubuntu 22.04.4 LTS', 'The system is not shown: ' + await fact('系统'));
    check(await fact('架构') === 'aarch64', 'The architecture is not shown: ' + await fact('架构'));
    check(/^\d+(\.\d)? (毫秒|秒)$/.test(await fact('耗时')), 'How long it took is not shown: ' + await fact('耗时'));
    check(JSON.stringify(await hopStates()) === '["ok"]', 'A direct connection is one hop: ' + await hopStates());
    await expectText(hops().first(), ['目标机', `${cfg.robot.user}@127.0.0.1:${cfg.robot.port}`, '已登录'], 'The device');
    let seen = await control('/seen');
    check(seen.verifications === 1, `Expected one verification on the robot, saw ${seen.verifications}`);
    check(seen.robot_logins === connected.robot_logins + 1, 'One verification is not one login: ' + JSON.stringify(seen));
    check(seen.hardware_scans === connected.hardware_scans, 'The verification scanned the hardware');
    check(sent.length === 1, `One press sent ${sent.length} requests`);
    await page.screenshot({path: `${shots}/connection-verify-passed.png`});
    await atEveryWidth('passed');

    // 3. Again and again, from the dialog: every time a login of its own; the keyboard gets there too.
    step = '3';
    check(await page.evaluate(() => document.activeElement?.id) === 'cv-headline', 'The focus is not on the outcome');
    // On macOS (WebKit, and the desktop app's web view) Tab reaches buttons only with Option held.
    await page.keyboard.press(engine.name() === 'webkit' ? 'Alt+Tab' : 'Tab');
    check(await page.evaluate(() => document.activeElement?.textContent) === '再验证一次', 'Tab from the outcome does not reach 再验证一次');
    await page.keyboard.press('Enter');
    await sentCount(2);
    check(await outcome() === 'passed', 'The second verification did not pass');
    seen = await control('/seen');
    check(seen.verifications === 2 && seen.robot_logins === connected.robot_logins + 2,
          'Verifying again did not log in again: ' + JSON.stringify(seen));
    await page.keyboard.press('Escape');
    await dialog.waitFor({state: 'hidden'});

    // 4. Through one jump host: both hops are logged in to, the jump host without a number.
    step = '4';
    check(await verify(ONE) === 'passed', 'The connection through one jump host did not pass: ' + await text(body));
    check(JSON.stringify(await hopStates()) === '["ok","ok"]', 'Expected two hops: ' + await hopStates());
    await expectText(hops().nth(0), ['跳板机', `${cfg.jump.user}@127.0.0.1:${cfg.jump.port}`, '已登录'], 'The jump host');
    check(!(await text(hops().nth(0))).includes('第 1 级'), 'One jump host must not be numbered');
    await expectText(hops().nth(1), ['目标机', '已登录'], 'The device behind it');
    check((await control('/seen')).jump_logins === connected.jump_logins + 1, 'The jump host was not logged in to exactly once');
    await closeDialog();

    // 5. Through three jump hosts: four hops, each logged in to once, in order.
    step = '5';
    check(await verify(THREE) === 'passed', 'The connection through three jump hosts did not pass: ' + await text(body));
    check(JSON.stringify(await hopStates()) === '["ok","ok","ok","ok"]', 'Expected four hops: ' + await hopStates());
    for (const level of [1, 2, 3]) {
      const gate = cfg.chain[level - 1];
      await expectText(hops().nth(level - 1), [`第 ${level} 级跳板机`, `${gate.user}@127.0.0.1:${gate.port}`, '已登录'], `Level ${level}`);
    }
    check(await fact('系统') === 'Ubuntu 22.04.4 LTS' && await fact('架构') === 'aarch64', 'System and architecture through three jump hosts');
    seen = await control('/seen');
    check(JSON.stringify(seen.chain_logins) === JSON.stringify(connected.chain_logins.map(count => count + 1)),
          'Every level is not logged in to exactly once: ' + JSON.stringify(seen.chain_logins));
    await page.screenshot({path: `${shots}/connection-verify-three-hops.png`});
    await closeDialog();

    // 6. Stuck at the second of three jump hosts: the hop, its address, the reason, and what was not tried.
    step = '6';
    await control('/refuse?level=2');
    const before = await control('/seen');
    check(await verify(THREE) === 'failed', 'A refused login at level 2 was not reported as a failure: ' + await text(body));
    await expectText(body.locator('.cv-verdict'), ['卡在第 2 级跳板机', `${cfg.chain[1].user}@127.0.0.1:${cfg.chain[1].port}`,
                                                 '第 2 级跳板机拒绝登录', /用了 \d/], 'Stuck at level 2');
    check(JSON.stringify(await hopStates()) === '["ok","failed","not_reached","not_reached"]', 'The hops after it: ' + await hopStates());
    await expectText(hops().nth(1), ['卡在这里'], 'The hop it got stuck at');
    await expectText(hops().nth(2), ['没有走到'], 'The hop after it');
    check(await page.evaluate(() => document.activeElement?.id) === 'cv-headline', 'The focus is not on the outcome');
    check((await body.locator('.cv-actions .button').first().innerText()).trim() === '配置连接',
          'After a refused login, fixing it is not the first action');
    check(await body.locator('[data-action="engine-ssh-edit"]').count() === 1, 'No way to the saved login from a failed verification');
    check(await body.locator('.cv-facts').count() === 0, 'A system is shown although nothing was read');
    seen = await control('/seen');
    check(seen.chain_logins[2] === before.chain_logins[2] && seen.robot_logins === before.robot_logins,
          'Machines behind the hop that refused were contacted: ' + JSON.stringify(seen));
    await page.screenshot({path: `${shots}/connection-verify-stuck.png`});
    await atEveryWidth('stuck');
    await control('/restore');
    const again = sent.length + 1;
    await body.locator('[data-action="engine-ssh-verify"]').click();           // 再验证一次
    await sentCount(again);
    check(await outcome() === 'passed', 'It did not pass again once the jump host accepted its login: ' + await text(body));
    await closeDialog();

    // 7. Stuck at the device itself.
    step = '7';
    await control('/cut');
    check(await verify(DIRECT) === 'failed', 'A robot that is off was not reported as a failure');
    await expectText(body.locator('.cv-verdict'), ['卡在目标机', `${cfg.robot.user}@127.0.0.1:${cfg.robot.port}`], 'Stuck at the device');
    check(JSON.stringify(await hopStates()) === '["failed"]', 'The device is where it got stuck: ' + await hopStates());
    await closeDialog();
    check(await verify(ONE) === 'failed', 'A robot the jump host cannot reach was not reported as a failure');
    await expectText(body.locator('.cv-verdict'), ['卡在目标机', '跳板机连不上目标机'], 'Stuck at the device behind the jump host');
    check(JSON.stringify(await hopStates()) === '["ok","failed"]', 'The jump host passed, the device did not: ' + await hopStates());
    await closeDialog();
    await control('/restore');

    // 8. While it runs: the hops that will be logged in to, the seconds, and the card says so.
    step = '8';
    await control('/slow?seconds=2');
    const requests = sent.length;
    await verifyButton(THREE).click();
    await page.locator('#cv-body[data-cv-state="running"]').waitFor();
    await expectText(body, ['正在逐级登录', '已用'], 'While it runs');
    check(JSON.stringify(await hopStates()) === '["pending","pending","pending","pending"]', 'The route while it runs: ' + await hopStates());
    check((await verifyButton(THREE).innerText()).trim() === '验证中…', 'The card does not say that it is running');
    await page.screenshot({path: `${shots}/connection-verify-running.png`});
    check(await outcome() === 'passed', 'The slow verification did not pass: ' + await text(body));
    check(sent.length === requests + 1, 'More than one request for one verification');
    check((await verifyButton(THREE).innerText()).trim() === '验证 SSH 连接', 'The card still says that it is running');
    await closeDialog();

    // 8b. Closed while it runs: the outcome is said, kept on the card, and shown from there without logging in again.
    step = '8b';
    await page.locator('#toast').evaluate(el => el.classList.remove('show'));
    await verifyButton(THREE).click();
    await page.locator('#cv-body[data-cv-state="running"]').waitFor();
    await closeDialog();
    await page.locator('#toast.show', {hasText: 'SSH 连接验证通过'}).waitFor({timeout: 60000});
    check((await verifyButton(THREE).innerText()).trim() === '查看验证结果', 'The card does not offer the result');
    const asked = sent.length, verified = (await control('/seen')).verifications;
    await verifyButton(THREE).click();
    check(await outcome() === 'passed', 'The kept result is not shown');
    check(sent.length === asked && (await control('/seen')).verifications === verified, 'Looking at the result logged in again');
    check((await verifyButton(THREE).innerText()).trim() === '验证 SSH 连接', 'The card still offers a result that was looked at');
    await closeDialog();
    await control('/restore');

    // 9. While the robot is being onboarded (its hardware is being scanned) the button is not offered.
    step = '9';
    await control('/scan?name=' + encodeURIComponent(ONE));
    await page.evaluate(() => refresh(true));
    await page.locator('article.card', {hasText: ONE}).locator('[data-action="engine-ssh-verify"][aria-disabled="true"]').waitFor({timeout: 15000});
    check((await verifyButton(ONE).getAttribute('title')).includes('正在扫描硬件'), 'The held button does not say why');
    check(await verifyButton(DIRECT).getAttribute('aria-disabled') === null, 'Another robot is held too');
    const untouched = sent.length, logins = (await control('/seen')).robot_logins;
    await page.locator('#toast').evaluate(el => el.classList.remove('show'));
    await verifyButton(ONE).focus();                      // it can be reached from the keyboard, and pressed
    await page.keyboard.press('Enter');
    await page.locator('#toast.show', {hasText: '现在不能验证连接'}).waitFor({timeout: 10000});
    await expectText(page.locator('#toast'), ['正在扫描硬件'], 'The reason');
    check(!(await dialog.evaluate(el => el.open)), 'A dialog opened for a held robot');
    check(sent.length === untouched, 'A press on the held button sent a request');
    check((await control('/seen')).robot_logins === logins, 'A press on the held button logged in to the robot');
    await page.screenshot({path: `${shots}/connection-verify-held.png`});
    await control('/restore');
    await page.evaluate(() => refresh(true));
    await page.locator('article.card', {hasText: ONE}).locator('[data-action="engine-ssh-verify"]:not([aria-disabled])').waitFor({timeout: 15000});
    check(await verify(ONE) === 'passed', 'It is not offered again once the scan has ended');
    await closeDialog();

    // 10. The robot's own service has a 「连接检查」 of its own: on a card with both, each says what it checks.
    step = '10';
    await card(DIRECT).locator('[data-action="engine-ssh-edit"]').click();
    await dialog.locator('[data-action="robot-config"]').click();
    await fill('endpoint', 'http://127.0.0.1:9');
    await submit();
    await page.evaluate(() => refresh(true));
    const service = card(DIRECT).locator('[data-action="robot-connect"]');
    await service.waitFor({timeout: 15000});
    check((await service.innerText()).trim() === '连接检查', 'The check of the robot\'s own service lost its name');
    check((await service.getAttribute('title')).includes('不是 SSH 登录'), 'The other check does not say what it checks');
    check(await verify(DIRECT) === 'passed', 'The SSH verification of a robot that also has a service address');
    await expectText(body.locator('.cv-what'), ['SSH 登录', '“连接检查”'], 'The dialog tells the two checks apart');
    await closeDialog();

    check(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), 'The page scrolls sideways');
    check(errors.length === 0, 'Page errors: ' + errors.join(' | '));
    console.log(`connection_verify browser (${engine.name()}): 10 steps passed, ${sent.length} verifications sent`);
  } catch (error) {
    await page.screenshot({path: `${shots}/connection-verify-failure.png`}).catch(() => {});
    console.error(`step ${step}: ${error.stack || error}`);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
