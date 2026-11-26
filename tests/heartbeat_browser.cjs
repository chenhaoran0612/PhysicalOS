// Browser acceptance for issue #24: a robot's card says 未接入 / 接入中 / 在线 / 离线 and when its last
// heartbeat came, without the status page being open. A robot whose network is cut is offline within
// three minutes and online again by itself; the status page no longer says "连不上" after one failed read.
//   node tests/heartbeat_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/heartbeat_browser_fixture.py (a local SSH protocol fixture, no real machine; the server
// needs NEUXMIND_MAIL_DEV_ECHO=1). Minutes pass through the fixture's control port and the page's clock:
// nobody waits for them. BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const STATUS_API = '/api/engineering/ssh/status';
const ROBOT = '巡检机器人', OLD_ROBOT = '只填了服务地址的机器人';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-heartbeat-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  await page.clock.install();                         // the page's clock moves with the fixture's
  const reads = [];                                   // every status read the page asks for
  page.on('request', r => { if (r.url().endsWith(STATUS_API)) reads.push(r.postDataJSON()); });

  const dialog = page.locator('#dialog'), status = page.locator('#ssh-status');
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const control = async path => (await fetch(cfg.control + path)).json();
  // The page asks for the states now instead of at its next poll (it does not draw itself again for that).
  const sync = () => page.evaluate(async () => {
    while (hbPolling) await new Promise(resolve => setTimeout(resolve, 20));
    await hbPoll();
  });
  // That much later, for the heartbeat (the fixture runs the round that is due) and for the page alike.
  const later = async (seconds = 60) => {
    const seen = await control(`/advance?seconds=${seconds}`);
    await page.clock.fastForward(seconds * 1000);
    await sync();
    return seen;
  };
  const card = name => page.locator('article.card', {hasText: name});
  const line = name => card(name).locator('.hb-line');
  const lineText = async name => (await line(name).innerText()).replace(/\s+/g, ' ');
  const expectLine = async (name, parts, what) => {
    const text = await lineText(name);
    for (const part of parts) {
      check(part instanceof RegExp ? part.test(text) : text.includes(part), `${what}: missing ${part} in "${text}"`);
    }
    return text;
  };
  const toastSays = text => page.locator('#toast.show', {hasText: text}).waitFor({timeout: 20000});
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '心跳验收', email: `heartbeat-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'beat1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '知道每台机器人现在连不连得上');
  await submit();

  // A robot reached over SSH, directly.
  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="engine-ssh"]').first().click();
  await fill('name', ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.robot_port);
  await page.locator('#dialog [name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #32: a new device needs a type from the list
  await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
  await submit();
  await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await page.locator('#dialog input[type=checkbox]').check();
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});      // issue #2: the scan after connecting
  await page.locator('#dialog [data-action="close"]').first().click();

  // 1. Right after connecting the card says online and when the robot was last heard: saving tested the login.
  // (No second page.goto to the same address: WebKit reloads on it and reports the poll it cut off as a page error.)
  await page.evaluate(() => refresh(true));
  await line(ROBOT).waitFor({timeout: 15000});
  const first = await expectLine(ROBOT, ['在线', /最近心跳 \d\d:\d\d:\d\d/], 'A robot that was just connected');
  check(await line(ROBOT).locator('.status.good').count() === 1, 'Online is not shown in green');
  check(await card(ROBOT).locator('h3 + .hb-line').count() === 1, 'The state is not right under the robot\'s name');
  check(await line(ROBOT).locator('[data-action="engine-heartbeat-check"]').count() === 0, 'A retry is offered while online');
  check((await control('/seen')).heartbeats === 0, 'The heartbeat logged in again right after the connection was saved');

  // 2. A minute later, with no status page open: one tiny heartbeat, a newer time, and no status read at all.
  let seen = await later();
  check(seen.heartbeats === 1, `Expected one heartbeat a minute later, saw ${seen.heartbeats}`);
  const second = await expectLine(ROBOT, ['在线', /最近心跳 \d\d:\d\d:\d\d/], 'A minute later');
  check(second !== first, 'The time of the last heartbeat did not move: ' + second);
  const logins = seen.logins;
  seen = await later();
  check(seen.heartbeats === 2, `Expected a second heartbeat a minute later, saw ${seen.heartbeats}`);
  check(seen.logins === logins, 'The heartbeat logged in again instead of keeping its session');
  check(seen.status_reads === 0 && seen.software_reads === 0, 'The heartbeat read the status: ' + JSON.stringify(seen));
  check(seen.hardware_scans === 1, 'The heartbeat scanned the hardware: ' + JSON.stringify(seen));   // only the scan after connecting
  check(reads.length === 0, 'The page read the status although no status page was open');
  await page.screenshot({path: `${shots}/heartbeat-online.png`});

  // 3. A robot without an SSH connection that was never connected: 未接入, with the reason once a check failed.
  await page.locator('[data-action="robot-add"]').first().click();
  await fill('name', OLD_ROBOT); await fill('endpoint', 'http://127.0.0.1:9');
  await page.locator('#dialog [name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #32: a new device needs a type from the list
  await submit();
  await page.evaluate(() => refresh(true));
  await line(OLD_ROBOT).waitFor({timeout: 15000});
  await expectLine(OLD_ROBOT, ['未接入', '还没有连接过'], 'A robot that was never connected');
  check(await line(OLD_ROBOT).locator('.status.good, .status.bad').count() === 0, '未接入 is not grey');
  await page.locator('#toast').evaluate(el => el.classList.remove('show'));       // the toast of adding it is still up
  await card(OLD_ROBOT).locator('[data-action="robot-connect"]').click();
  await page.locator('#toast.show').waitFor({timeout: 20000});
  await page.evaluate(() => refresh(true));
  await sync();
  await expectLine(OLD_ROBOT, ['未接入', '上一次失败：'], 'A robot whose connection check failed');

  // 4. The robot's network is cut. One and two minutes without a heartbeat: still online. Three: offline.
  //    None of this draws the page again: the card stays the same element, an opened explanation stays open.
  await later();                                      // a heartbeat right before the cut
  await page.locator('details.page-purpose summary').last().click();
  await card(ROBOT).evaluate(el => { el.dataset.sameCard = 'yes'; });
  await control('/cut');
  await later();
  await expectLine(ROBOT, ['在线'], 'One minute without a heartbeat');
  await later();
  await expectLine(ROBOT, ['在线'], 'Two minutes without a heartbeat');
  check(await line(ROBOT).locator('.hb-reason').count() === 0, 'A missed heartbeat is shown as a fault before the robot is offline');
  await later();
  await expectLine(ROBOT, ['离线', '已离线 3 分钟', /最近心跳 \d\d:\d\d:\d\d/, '目标机：', '重试'], 'Three minutes without a heartbeat');
  check(await line(ROBOT).locator('.status.bad').count() === 1, 'Offline is not shown in red');
  check(reads.length === 0, 'The page read the status to find out that the robot is offline');
  check(await card(ROBOT).getAttribute('data-same-card') === 'yes', 'The cards were drawn again for a heartbeat or a change of state');
  check(await page.locator('details.page-purpose').last().evaluate(el => el.open), 'An opened explanation was closed by a heartbeat');
  for (const action of ['engine-status', 'engine-ssh-edit', 'hardware-open', 'engine-robot-remove']) {
    check(await card(ROBOT).locator(`[data-action="${action}"]`).count() === 1, `The offline card lost its "${action}" button`);
  }
  await page.screenshot({path: `${shots}/heartbeat-offline.png`});
  // Two more minutes pass on the page alone (no new answer from the backend): the duration moves on in place.
  await page.clock.fastForward(120000);
  await page.waitForTimeout(300);
  await expectLine(ROBOT, ['离线', '已离线 5 分钟'], 'Two more minutes offline');
  await control('/advance?seconds=120');

  // 5. "重试" while it is still cut off says so and changes nothing. It works from the keyboard.
  const retry = line(ROBOT).locator('[data-action="engine-heartbeat-check"]');
  check((await retry.getAttribute('aria-label')).includes(ROBOT), 'The retry does not say which robot it is for');
  await retry.focus();
  await page.keyboard.press('Enter');
  await toastSays('还是没有收到心跳');
  await expectLine(ROBOT, ['离线', '重试'], 'A retry while the robot is still cut off');
  check(await retry.evaluate(el => el === document.activeElement), 'The keyboard focus left the retry button');

  // 6. The network is back. Nobody touches the app: the next heartbeat brings it online.
  await control('/restore');
  await later();
  await expectLine(ROBOT, ['在线', /最近心跳 \d\d:\d\d:\d\d/], 'After the network came back');
  check(await line(ROBOT).locator('.hb-reason, [data-action="engine-heartbeat-check"]').count() === 0, 'The fault is still shown after the robot came back');

  // 7. Offline again; this time the user does not wait for the next round.
  await control('/cut');
  await later(); await later(); await later();
  await expectLine(ROBOT, ['离线'], 'Cut off a second time');
  await control('/restore');
  await page.locator('#toast').evaluate(el => el.classList.remove('show'));
  await retry.focus();
  await page.keyboard.press('Enter');
  await toastSays('收到心跳，机器人在线');
  await expectLine(ROBOT, ['在线'], 'After a retry that reached the robot');
  check(await retry.count() === 0, 'A retry is still offered although the robot is online');
  check(await line(ROBOT).evaluate(el => el === document.activeElement), 'The keyboard focus was dropped when the retry button went away');
  check(await card(ROBOT).getAttribute('data-same-card') === 'yes', 'The cards were drawn again for a retry');

  // 8. The status page takes its state from the heartbeat. One failed read no longer says "连不上".
  await card(ROBOT).locator('[data-action="engine-status"]').click();
  await status.locator('.ls-tabs').waitFor({timeout: 30000});
  const head = status.locator('.hb-head');
  check((await head.innerText()).includes('在线') && (await head.innerText()).includes('最近心跳'), 'The status page does not show the heartbeat: ' + await head.innerText());
  const opened = reads.length;
  check(opened >= 2, 'The status page did not read when it was opened');
  await later(20);                                    // the page's own first reads are over
  await control('/cut');
  await status.locator('[data-action="engine-status-refresh"]').click();
  await status.locator('.notice.warn').first().waitFor({timeout: 30000});
  await sync();
  check((await head.innerText()).includes('在线'), 'One failed read turned the robot offline: ' + await head.innerText());
  check(!(await status.innerText()).includes('连不上'), 'The status page still says "连不上" after one failed read');
  await later(); await later(); await later();
  check(/离线/.test(await head.innerText()) && /已离线 \d+ 分钟/.test(await head.innerText()), 'Three minutes later the status page does not say offline: ' + await head.innerText());
  await page.screenshot({path: `${shots}/heartbeat-status-page-offline.png`});
  await control('/restore');
  await later();
  check((await head.innerText()).includes('在线'), 'The status page did not follow the robot back online: ' + await head.innerText());

  // 9. A closed status page reads nothing, as before. The heartbeat goes on.
  await status.locator('#ls-next', {hasText: '秒后自动刷新'}).waitFor({timeout: 60000});     // the page's own read of this minute is over
  await page.locator('#dialog [data-action="close"]').first().click();
  const atClose = reads.length, before = await control('/seen');
  await later(); await later();
  await page.waitForTimeout(800);
  seen = await control('/seen');
  check(reads.length === atClose, 'The status is still read after its page was closed');
  check(seen.status_reads === before.status_reads && seen.software_reads === before.software_reads &&
        seen.hardware_scans === before.hardware_scans, 'A full read reached the robot with no status page open: ' + JSON.stringify(seen));
  check(seen.heartbeats === before.heartbeats + 2, 'The heartbeat stopped with the status page: ' + JSON.stringify([before, seen]));
  await expectLine(ROBOT, ['在线'], 'With the status page closed');

  // 10. The cards at every width, with one robot offline: nothing runs off the side.
  await control('/cut');
  await later(); await later(); await later();
  await expectLine(ROBOT, ['离线'], 'For the screenshots');
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(150);
    check(await noSidewaysScroll(), `The page scrolls sideways at ${width}px`);
    check(await line(ROBOT).evaluate(el => el.scrollWidth <= el.clientWidth + 1), `The state line is cut off at ${width}px`);
    await page.screenshot({path: `${shots}/heartbeat-cards-${width}.png`});
  }
  await page.setViewportSize({width: 1440, height: 1000});
  // Colour is not the only signal, and the "接入中" dot does not move when reduced motion is asked for.
  check((await line(ROBOT).locator('.status').innerText()).trim() === '离线', 'The state is not written out');
  await page.emulateMedia({reducedMotion: 'reduce'});
  const animation = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.className = 'status blue hb-checking';
    probe.innerHTML = '<i class="dot"></i>';
    document.body.append(probe);
    const name = getComputedStyle(probe.firstChild).animationName;
    probe.remove();
    return name;
  });
  check(animation === 'none', 'The checking dot still animates when reduced motion is asked for');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), onlineWithoutStatusPage: true, oneHeartbeatAMinute: true,
                              offlineAfterThreeMinutes: true, backOnlineByItself: true, retryByHand: true, notOnboarded: true,
                              statusPageFollowsHeartbeat: true, closedStatusPageReadsNothing: true, heartbeatGoesOn: true,
                              noSidewaysOverflow: true, statusReadsByPage: reads.length, heartbeats: seen.heartbeats,
                              jsErrors: errors.length}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
