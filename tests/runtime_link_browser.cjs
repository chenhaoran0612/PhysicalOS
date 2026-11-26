// Browser acceptance for issue #17: a robot connected over SSH gets its management layer linked with one
// form that asks for no address and no token; the card says how the link is; when the robot's network is
// cut the card says so and requests fail in plain words, and the link comes back by itself.
//   node tests/runtime_link_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/runtime_link_browser_fixture.py (a local SSH protocol fixture and a stand-in of the
// management layer, no real machine; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app; default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const ROBOT = '巡检机器人';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-runtime-link-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const sent = [];                                    // everything the page sends about the link
  page.on('request', r => { if (r.url().includes('/api/robots/runtime-link')) sent.push([r.url(), r.postDataJSON()]); });

  const dialog = page.locator('#dialog');
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const control = async path => (await fetch(cfg.control + path)).json();
  const card = () => page.locator('article.card', {hasText: ROBOT});
  const line = () => card().locator('.rl-line');
  const lineText = async () => (await line().innerText()).replace(/\s+/g, ' ');
  // The page asks for the states now instead of at its next poll (it does not draw itself again for that).
  const sync = () => page.evaluate(async () => {
    while (rlPolling) await new Promise(resolve => setTimeout(resolve, 20));
    await rlPoll();
  });
  // Until the line says all of this. Nobody clicks anything meanwhile.
  const lineSays = async (parts, what, seconds = 30) => {
    const deadline = Date.now() + seconds * 1000;
    let text = '';
    while (Date.now() < deadline) {
      await sync();
      text = await lineText();
      if (parts.every(part => (part instanceof RegExp ? part.test(text) : text.includes(part)))) return text;
      await page.waitForTimeout(300);
    }
    throw Error(`${what}: the line says "${text}"`);
  };
  const hideToast = () => page.locator('#toast').evaluate(el => el.classList.remove('show'));
  const toastSays = text => page.locator('#toast.show', {hasText: text}).waitFor({timeout: 30000});
  const errorToast = code => page.locator(`#toast.show [data-error-code="${code}"]`).waitFor({timeout: 30000});
  const connectCheck = async () => { await hideToast(); await card().locator('[data-action="robot-connect"]').click(); };
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '管理层验收', email: `link-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'link1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '园区巡检'); await fill('objective', '不开隧道就能管理机器人上的管理层');
  await submit();
  await control('/tune');

  // A robot reached over SSH: the only thing a person types about it.
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
  await page.evaluate(() => refresh(true));

  // 1. Its card offers the link, says nothing about one yet, and still has the SSH connection's own button.
  await card().locator('[data-action="engine-runtime-link"]').waitFor({timeout: 15000});
  check((await card().locator('[data-action="engine-runtime-link"]').innerText()).trim() === '接入管理层', 'The button is not named 接入管理层');
  check(await line().count() === 0, 'A line about the link is shown before there is one');
  check(await card().locator('[data-action="engine-ssh-edit"]').count() === 1, 'The SSH connection lost its own button');
  check((await card().innerText()).includes('控制通道：未连接'), 'The card does not say the control channel is not connected');

  // 2. The form: the SSH connection it will use, the standard port, an optional token, and no address.
  await card().locator('[data-action="engine-runtime-link"]').click();
  await dialog.getByText('接入管理层', {exact: true}).first().waitFor();
  const formText = await dialog.innerText();
  check(formText.includes(`${cfg.robot_user}@127.0.0.1:${cfg.robot_port}`), 'The form does not name the SSH connection it uses');
  check(formText.includes('不用开隧道') && formText.includes('不用填地址'), 'The form does not say what is no longer needed');
  check(await page.locator('#dialog [name=port]').inputValue() === String(cfg.layer_port), 'The standard port is not filled in');
  check(await page.locator('#dialog [name=token]').inputValue() === '', 'A token is filled in');
  check(await page.locator('#dialog [name=token]').getAttribute('type') === 'password', 'A typed token would be readable');
  check(await page.locator('#dialog [name=endpoint], #dialog [name=host], #dialog [name=address]').count() === 0, 'The form asks for an address');
  await page.screenshot({path: `${shots}/runtime-link-form.png`});

  // 3. A port that cannot be the management layer is refused in the form, and nothing is sent.
  await fill('port', 22);
  await page.locator('#dialog [name=port]').evaluate(el => { el.removeAttribute('min'); });     // past the browser's own check
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '1024–65535'}).waitFor();
  check(sent.filter(([url]) => url.endsWith('/api/robots/runtime-link')).length === 0, 'A request went out for port 22');
  await fill('port', cfg.layer_port);

  // 4. One submit: the token is read from the robot, the layer answers through SSH, the card says so.
  await hideToast();
  await submit();
  await toastSays('连接检查通过');
  const request = sent.find(([url]) => url.endsWith('/api/robots/runtime-link'))[1];
  check(JSON.stringify(Object.keys(request).sort()) === JSON.stringify(['port', 'project_id', 'robot_id']),
        'The form sent more than the robot and the port: ' + Object.keys(request));
  let text = await lineSays(['管理层', '已连通', `到机器人本机 ${cfg.layer_port} 端口`], 'After linking');
  check(await line().locator('.status.good').count() === 1, 'A link that is up is not shown in green');
  check(await line().locator('[data-action="engine-runtime-link-retry"]').count() === 0, 'A retry is offered while the link is up');
  await require('./robots_page_helper.cjs').openRobotMenu(card());                           // issue #41: plain facts are in the card's 更多功能 list
  const cardText = await card().innerText();
  await page.keyboard.press('Escape');
  check(cardText.includes('控制通道：已连接'), 'The card does not say the control channel is connected: ' + cardText);
  check(/\d+ 项设备能力/.test(cardText), 'The capabilities the layer reported are not on the card');
  check(!cardText.includes('ssh-forward'), 'The card shows the internal name of the link');
  check((await card().locator('[data-action="engine-runtime-link"]').innerText()).trim() === '管理层设置', 'The button did not become 管理层设置');
  let seen = await control('/seen');
  check(seen.token_reads === 1, `The token was read ${seen.token_reads} times from the robot`);
  check(seen.layer_calls.includes('GET /v1/manifest') && seen.layer_calls.includes('GET /v1/status'), 'The layer was not asked: ' + seen.layer_calls);
  check(seen.loopback_channels >= 2, 'The requests did not go through the robot\'s SSH service');
  await page.screenshot({path: `${shots}/runtime-link-up.png`});

  // 5. "连接检查" and the robot's detail use the same way, without another login.
  const logins = seen.logins;
  await connectCheck();
  await toastSays('协议连接检查通过');
  seen = await control('/seen');
  check(seen.logins === logins, 'A connection check logged in again instead of using the kept session');

  // 6. The robot's network is cut. The card says so by itself, in place: the same card, an opened
  //    explanation stays open. What a request gets meanwhile is said on the card.
  await page.locator('details.rp-settings > summary').click();          // issue #41: the page has no folds; 「设置」 is what can be open
  await card().evaluate(el => { el.dataset.sameCard = 'yes'; });
  await control('/cut');
  text = await lineSays(['已断开', '正在自动重连', '请求不会发出', '不需要手工操作'], 'After the network was cut');
  check(/已持续 \d+ 秒|刚刚断开/.test(text), 'The line does not say for how long: ' + text);
  check(await line().locator('.status.bad').count() === 1, 'A link that is down is not shown in red');
  check(await line().locator('.hb-reason').count() >= 1, 'The reason is not shown');
  check(await line().locator('[data-action="engine-runtime-link-retry"]').count() === 1, 'Trying now is not offered');
  check(await card().getAttribute('data-same-card') === 'yes', 'The cards were drawn again because the link dropped');
  check(await page.locator('details.rp-settings').evaluate(el => el.open), 'An opened explanation was closed by the drop');
  await page.screenshot({path: `${shots}/runtime-link-down.png`});
  // On a phone the line and its reason wrap; nothing scrolls sideways.
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(400);                     // the sidebar slides away
  check(await noSidewaysScroll(), 'The page scrolls sideways at 390 px while the link is down');
  check(await line().evaluate(el => el.getBoundingClientRect().right <= document.documentElement.clientWidth), 'The line is wider than a phone');
  await line().evaluate(el => el.scrollIntoView({block: 'center'}));
  await page.screenshot({path: `${shots}/runtime-link-down-390.png`});
  await page.setViewportSize({width: 1440, height: 1000});

  // 7. A request while it is down fails in plain words: it was not sent, and it is being reconnected.
  const answered = (await control('/seen')).layer_calls.length;
  await connectCheck();
  await errorToast('device_link_down');
  const toast = await page.locator('#toast').innerText();
  check(toast.includes('没有发出') && toast.includes('自动重连'), 'The failure does not say what happened: ' + toast);
  check((await control('/seen')).layer_calls.length === answered, 'A request reached the layer while the robot was cut off');

  // 8. The robot is back. Nobody clicks anything: the line returns to 已连通, and the check passes again.
  await control('/restore');
  await lineSays(['已连通'], 'After the network came back', 40);
  check(await line().locator('[data-action="engine-runtime-link-retry"]').count() === 0, 'A retry is still offered after the link came back');
  await connectCheck();
  await toastSays('协议连接检查通过');

  // 9. A login the robot refuses is tried once, then left alone; a person can ask again.
  await control('/refuse');
  await lineSays(['登录被拒绝', '已停止自动重连', '以免账号被锁'], 'After the robot refused the login');
  const refusedLogins = (await control('/seen')).logins;
  await page.waitForTimeout(3500);                    // several of the fixture's one-second pauses
  check((await control('/seen')).logins === refusedLogins, 'A refused login was repeated by itself');
  await page.screenshot({path: `${shots}/runtime-link-refused.png`});
  await control('/accept');
  await hideToast();
  await line().locator('[data-action="engine-runtime-link-retry"]').click();
  await toastSays('通道已连通');
  await lineSays(['已连通'], 'After asking again by hand');

  // 10. SSH is fine but the management layer is not running: the check says exactly that.
  await control('/stop-layer');
  await connectCheck();
  await errorToast('device_service_down');
  check((await lineSays(['已连通'], 'While only the layer is down')).includes('已连通'), 'The SSH link is shown as down although only the layer is');
  await control('/start-layer');
  await connectCheck();
  await toastSays('协议连接检查通过');

  // 11. Keyboard: the button on the card opens the form, Escape closes it, the focus is not lost.
  await require('./robots_page_helper.cjs').openRobotMenu(card());                           // issue #41: the entry is in the card's 更多功能 list
  await card().locator('[data-action="engine-runtime-link"]').focus();
  await page.keyboard.press('Enter');
  await dialog.getByText('管理层设置', {exact: true}).first().waitFor();
  check(await page.locator('#dialog [name=port]').inputValue() === String(cfg.layer_port), 'The saved port is not shown');
  check(await page.locator('#dialog [name=token]').inputValue() === '', 'The saved token is shown in the form');
  check(!(await dialog.innerText()).includes('ssh-forward'), 'The form shows the internal name of the link');

  // 12. Taking the link away: the SSH connection stays, the card offers the link again.
  await hideToast();
  await page.locator('#dialog [data-action="engine-runtime-link-remove"]').click();
  await toastSays('SSH 连接保留');
  await card().locator('[data-action="engine-runtime-link"]', {hasText: '接入管理层'}).waitFor({timeout: 15000});
  check(await line().count() === 0, 'The line is still shown after the link was taken away');
  check((await card().innerText()).includes('控制通道：未连接'), 'The card still says connected after the link was taken away');
  check(await card().locator('[data-action="engine-ssh-edit"]').count() === 1, 'The SSH connection went with the link');

  check(sent.every(([, body]) => !body || !['host', 'endpoint', 'address', 'url'].some(key => key in body)),
        'The page sent an address for the link');
  check(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('runtime link browser acceptance passed');
})().catch(error => { console.error(error); process.exit(1); });
