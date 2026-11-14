// Issue #9 browser acceptance: "配置连接" on a robot connected over SSH opens the SSH form filled
// with its saved connection (no credentials), saves changes to that robot's connection, asks for the
// fingerprints again when an address changes, and refuses while the robot is being investigated.
// Robots that are not connected over SSH, and "自有服务", keep the service-address form.
// Fixture: tests/ssh_edit_browser_fixture.py (local SSH protocol fixtures, no real machine).
// Usage: node tests/ssh_edit_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const SSH_ROBOT = '巡检狗', PLAIN_ROBOT = '已有接入程序的机器人';
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-ssh-edit-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `ssh-edit-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const control = name => dialog.locator(`[name="${name}"]`);
  const value = name => control(name).inputValue();
  const fill = (name, text) => control(name).fill(String(text));
  const click = async action => {
    const inDialog = page.locator(`#dialog[open] [data-action="${action}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${action}"]`)).first().click();
  };
  const formErrors = async () => (await page.locator('#dialog .form-error').allTextContents()).filter(Boolean).join(';');
  // The page disables the submit button for the length of the request: enabled again, or replaced, means it is over.
  const settled = () => page.waitForFunction(() => {
    const button = document.querySelector('#dialog button[type=submit]');
    return !button || !button.disabled;
  });
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    await settled();
    const error = await formErrors();
    if (error) throw Error(error);
  };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 500)}`);
  };
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const card = name => page.locator('article.card', {hasText: name});
  const robotsPage = async () => { await page.goto(cfg.url + '/#robots'); await card(SSH_ROBOT).waitFor(); };
  const openEdit = async () => {
    await robotsPage();
    await card(SSH_ROBOT).locator('[data-action="engine-ssh-edit"]').click();
    await dialog.getByText('配置连接', {exact: true}).waitFor();
  };
  const confirmFingerprint = async (title, secrets = {}) => {
    await dialog.getByText(title, {exact: true}).waitFor();
    for (const [name, text] of Object.entries(secrets)) await fill(name, text);
    await dialog.locator('input[type=checkbox]').check();
    await submit();
  };
  const savedToast = () => page.locator('#toast.show', {hasText: 'SSH 连接已更新'}).waitFor();
  const status = page.locator('#ssh-status');
  const softwareTab = () => status.locator('[data-action="engine-status-tab"][data-id="software"]').click();
  // The software list of the status page (issue #8) names the machine it was read from: "<hostname>-telemetry".
  const softwareOf = async hostname => {
    await softwareTab();
    await status.getByText(`${hostname}-telemetry`).first().waitFor({timeout: 30000});
  };
  const liveHostname = async hostname => {
    await robotsPage();
    await card(SSH_ROBOT).locator('[data-action="engine-status"]').click();
    await status.getByText('在线', {exact: true}).waitFor({timeout: 20000});
    await expectText(status, [hostname]);
    await softwareOf(hostname);                    // the full read ended: its hardware and software are kept for quick reads
  };
  // While set, the full status reads (hardware and software) wait: what the page shows after the quick read alone can be checked.
  let fullReads = null;
  await page.route('**/api/engineering/ssh/status', async route => {
    if (fullReads && route.request().postDataJSON()?.quick !== true) await fullReads;
    await route.continue();
  });
  const noSecretsVisible = async where => {
    const seen = await page.content() + await page.evaluate(() => fetch('/api/state').then(r => r.text()));
    for (const secret of [cfg.robot_login, cfg.jump_login]) must(!seen.includes(secret), `A saved login is visible ${where}`);
  };
  const busy = async on => {                                        // the fixture starts or ends a run; the page reads it at once
    await fetch(`${cfg.control}/busy?robot=${encodeURIComponent(SSH_ROBOT)}&on=${on ? 1 : 0}`);
    await page.evaluate(() => refresh(true));
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '改连接验收', email: `edit-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'edit1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await fill('name', '巡检机器人接入'); await fill('objective', '接入真机并在换网络后改连接');
  await submit();

  // Two robots: one connected over SSH (directly, to the first machine), one with a service address only.
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  await fill('name', SSH_ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.first_port);
  await fill('username', cfg.robot_user); await fill('password', cfg.robot_login);
  await dialog.locator('details.page-purpose summary').click();
  await fill('watch_services', 'bridge');
  await submit();
  await confirmFingerprint('确认 SSH 主机指纹');
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await click('robot-add');
  await fill('name', PLAIN_ROBOT); await fill('endpoint', 'http://127.0.0.1:9');
  await submit();

  // 1 · The SSH robot's card says "配置连接"; the form is the SSH form, filled in, without credentials.
  await robotsPage();
  must(await card(SSH_ROBOT).locator('[data-action="robot-config"]').count() === 0, 'SSH robot still offers the service-address form on its card');
  must(await card(PLAIN_ROBOT).locator('[data-action="engine-ssh-edit"]').count() === 0, 'A robot without SSH offers the SSH edit');
  await expectText(card(SSH_ROBOT).locator('[data-action="engine-ssh-edit"]'), ['配置连接']);
  await openEdit();
  const shown = {};
  for (const name of ['host', 'port', 'username', 'auth', 'route', 'password', 'watch_services']) shown[name] = await value(name);
  const expected = {host: '127.0.0.1', port: String(cfg.first_port), username: cfg.robot_user, auth: 'password',
                    route: 'direct', password: '', watch_services: 'bridge'};
  must(JSON.stringify(shown) === JSON.stringify(expected), 'Edit form shows: ' + JSON.stringify(shown));
  must((await value('read_roots')).includes('/opt/ros'), 'Read roots are not filled in');
  must(await control('password').getAttribute('placeholder') === '留空：沿用已保存的密码', 'No hint that an empty password keeps the saved one');
  must(await control('ssh_command').count() === 1, 'No field to paste an ssh command');
  must(await control('name').count() === 0, 'The edit form asks for a robot name');
  must(await page.locator('#ssh-jump').isHidden() && await page.locator('#ssh-edit-route').isHidden(), 'Jump fields or the route note shown for an unchanged direct connection');
  await expectText(dialog.locator('button[type=submit]'), ['保存']);
  await noSecretsVisible('in the edit form');
  await shot('1-form');
  result.prefilledWithoutCredentials = true;

  // 2 · Nothing about the addresses changed: saved at once with the kept password, no fingerprint step.
  await dialog.locator('details.page-purpose summary').click();
  await fill('watch_services', 'bridge\nnav-stack');
  await submit();
  await savedToast();
  must(!(await dialog.isVisible()), 'Dialog still open after saving');
  await liveHostname('dog-a');
  await expectText(page.locator('#ssh-status'), ['nav-stack']);
  await click('close');
  result.savedWithKeptLogin = true;

  // 3 · Another host: its fingerprint is confirmed again, and the saved password is not sent to it.
  await openEdit();
  await fill('port', cfg.second_port);
  await page.locator('#ssh-edit-route').waitFor({state: 'visible'});
  await expectText(dialog.locator('button[type=submit]'), ['读取主机指纹']);
  await submit();
  await dialog.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['指纹不同', '不会发给另一台机器', `${cfg.robot_user}@127.0.0.1:${cfg.second_port}`]);
  await expectText(dialog.locator('button[type=submit]'), ['确认并保存']);
  await shot('2-new-host-fingerprint');
  await confirmFingerprint('确认 SSH 主机指纹', {password: cfg.robot_login});
  await dialog.getByText('SSH 连接已更新', {exact: true}).waitFor();
  await expectText(dialog, ['另一台机器', '都保留着', '重新扫描硬件']);
  await shot('3-other-machine');
  let release;
  fullReads = new Promise(resolve => { release = resolve; });
  await click('engine-status');
  await status.getByText('在线', {exact: true}).waitFor({timeout: 20000});
  await expectText(status, ['dog-b']);                               // not a cached session of the old host
  await softwareTab();                                               // nor what the status page kept of the old host:
  await expectText(status, ['正在读取软件']);                         // before the full read, no software list at all
  must(!(await status.innerText()).includes('dog-a-telemetry'), 'The software of the old machine is shown for the new one');
  await shot('3b-status-before-full-read');
  release();
  fullReads = null;
  await softwareOf('dog-b');
  must(!(await status.innerText()).includes('dog-a-telemetry'), 'The software of the old machine is still listed');
  result.statusShowsOnlyTheNewMachine = true;
  await click('close');
  await robotsPage();
  await expectText(card(SSH_ROBOT), [`${cfg.robot_user}@127.0.0.1:${cfg.second_port}`]);
  result.newHostNeedsFingerprint = true;

  // 4 · Through a jump host: both fingerprints are confirmed; the robot itself is still the same machine.
  await openEdit();
  must(await value('password') === '', 'A typed password came back');
  await control('route').selectOption('jump');
  await page.locator('#ssh-jump').waitFor({state: 'visible'});
  await fill('jump_host', '127.0.0.1'); await fill('jump_port', cfg.jump_port);
  await fill('jump_username', cfg.jump_user); await fill('jump_password', cfg.jump_login);
  await submit();
  await dialog.getByText('第 1 步：确认跳板机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['第一次保存这台跳板机的指纹']);
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await dialog.getByText('第 2 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['指纹一致', '经跳板机']);
  must(await control('password').count() === 0, 'Password asked again for the same machine');
  await shot('4-jump-second-step');
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await savedToast();
  await robotsPage();
  await expectText(card(SSH_ROBOT), ['经跳板机', `${cfg.jump_user}@127.0.0.1:${cfg.jump_port}`]);
  await liveHostname('dog-b');
  await expectText(page.locator('#ssh-status'), ['经跳板机']);
  await click('close');
  result.jumpConfirmsBothFingerprints = true;

  // 5 · The saved jump host is filled in too, and a pasted ssh command rewrites the form as it does for a new robot.
  await openEdit();
  const jumpShown = {route: await value('route'), jump_host: await value('jump_host'), jump_port: await value('jump_port'),
                     jump_username: await value('jump_username'), jump_auth: await value('jump_auth'), jump_password: await value('jump_password')};
  const jumpExpected = {route: 'jump', jump_host: '127.0.0.1', jump_port: String(cfg.jump_port), jump_username: cfg.jump_user,
                        jump_auth: 'password', jump_password: ''};
  must(JSON.stringify(jumpShown) === JSON.stringify(jumpExpected), 'Jump host shown: ' + JSON.stringify(jumpShown));
  must(await control('jump_password').getAttribute('placeholder') === '留空：沿用已保存的密码', 'No keep hint for the jump password');
  await noSecretsVisible('after saving a jump host');
  await shot('5-form-with-jump');
  await control('ssh_command').fill(`ssh -p ${cfg.first_port} ${cfg.robot_user}@127.0.0.1`);
  await control('ssh_command').blur();                             // leaving the field applies the command, as for a new robot
  await expectText(page.locator('#ssh-command-result'), ['已填好', '直连']);
  must(await value('route') === 'direct' && await value('port') === String(cfg.first_port) && await value('auth') === 'agent', 'Pasted command not applied');
  await control('auth').selectOption('password');                 // the fixtures only know passwords
  await page.locator('#ssh-edit-route').waitFor({state: 'visible'});
  await submit();
  await confirmFingerprint('确认 SSH 主机指纹', {password: cfg.robot_login});
  await dialog.getByText('SSH 连接已更新', {exact: true}).waitFor();
  await click('hardware-scan');                                    // the suggested next step works on the new route
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
  await click('close');
  await robotsPage();
  await expectText(card(SSH_ROBOT), [`${cfg.robot_user}@127.0.0.1:${cfg.first_port}`]);
  must(!(await card(SSH_ROBOT).innerText()).includes('跳板机'), 'Direct robot still shows a jump host');
  result.pastedCommand = true;

  // 6 · While the robot is being investigated the connection cannot be changed, and the reason is given.
  await busy(true);
  await robotsPage();
  await card(SSH_ROBOT).locator('[data-action="engine-session"]').first().waitFor();     // the page knows about the run
  await card(SSH_ROBOT).locator('[data-action="engine-ssh-edit"]').click();
  await expectText(dialog, ['调查会话正在运行', '停止', '查看这次会话']);
  must(await control('host').count() === 0, 'The form opened although an investigation is running');
  await shot('6-busy');
  const refused = await page.evaluate(async name => {
    const robot = state.robots.find(r => r.name === name);
    const response = await fetch('/api/engineering/ssh/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                 body: JSON.stringify({project_id: projectId, edit_robot_id: robot.id})});
    return {status: response.status, body: await response.json()};
  }, SSH_ROBOT);
  must(refused.status === 400 && refused.body.error.includes('调查会话正在运行'), 'API did not refuse: ' + JSON.stringify(refused));
  await click('close');
  await busy(false);
  await openEdit();                                                // a form that was open before the run started
  await busy(true);
  await dialog.locator('details.page-purpose summary').click();
  await fill('watch_services', 'bridge');
  await dialog.locator('button[type=submit]').click();
  await page.locator('#dialog .form-error', {hasText: '调查会话正在运行'}).waitFor();
  await busy(false);
  await submit();
  await savedToast();
  result.refusedWhileRunning = true;

  // 7 · A robot without SSH, and a service, keep the service-address form. The SSH form links to it too.
  await robotsPage();
  await expectText(card(PLAIN_ROBOT).locator('[data-action="robot-config"]'), ['适配器配置']);
  await card(PLAIN_ROBOT).locator('[data-action="robot-config"]').click();
  await dialog.getByText('配置连接', {exact: true}).waitFor();
  must(await control('endpoint').count() === 1 && await control('token').count() === 1 && await control('host').count() === 0,
       'A robot without SSH did not get the service-address form');
  must(await value('endpoint') === 'http://127.0.0.1:9', 'Service address not filled in');
  await shot('7-plain-robot');
  await click('close');
  await page.goto(cfg.url + '/#services');
  await click('service-add');
  await fill('name', '团队推理服务'); await fill('endpoint', 'http://127.0.0.1:9');
  await submit();
  await page.locator('article.card', {hasText: '团队推理服务'}).locator('[data-action="service-config"]').click();
  await dialog.getByText('配置连接', {exact: true}).waitFor();
  must(await control('endpoint').count() === 1 && await control('host').count() === 0, 'A service did not get the service-address form');
  await click('close');
  await openEdit();
  await dialog.locator('[data-action="robot-config"]').click();
  await control('endpoint').waitFor();
  must(await control('host').count() === 0, 'The link from the SSH form did not open the service-address form');
  await click('close');
  result.othersUnchanged = true;

  // 8 · Still the same two robots, and the form fits a phone-wide window.
  await robotsPage();
  must(await page.locator('#main .cards').first().locator('article.card').count() === 2, 'The number of robots changed');
  await page.setViewportSize({width: 375, height: 800});
  await openEdit();
  const overflow = await page.evaluate(() => {
    const d = document.querySelector('#dialog');
    return d.scrollWidth - d.clientWidth;
  });
  must(overflow <= 1, `Edit form overflows a 375 px window by ${overflow} px`);
  await shot('8-narrow');
  await click('close');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), ...result, jsErrors: errors.length}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
