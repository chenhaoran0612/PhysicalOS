// Browser acceptance: connect a robot through a jump host (two fingerprint steps), watch its
// read-only live status, change the watched services, connect a second robot directly, and
// connect a third one by pasting an ssh command.
// Fixture: tests/ssh_jump_browser_fixture.py (local SSH protocol fixtures, no real machine).
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ssh-jump-ui.json'));
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const dialog = page.locator('#dialog');
  const click = async a => {
    const inDialog = page.locator(`#dialog[open] [data-action="${a}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${a}"]`)).first().click();
  };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 400)}`);
  };
  const card = name => page.locator('article.card', {hasText: name});
  const confirmFingerprint = async title => {
    await dialog.getByText(title, {exact: true}).waitFor();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
  };
  const robotLogin = async name => {
    await page.goto(cfg.url + '/#robots');
    await click('engine-ssh');
    await fill('name', name); await fill('host', '127.0.0.1'); await fill('port', cfg.target_port);
    await fill('username', cfg.target_user); await fill('password', cfg.target_password);
  };

  await page.goto(cfg.url);
  await click('account');
  await fill('name', '跳板机验收'); await fill('email', 'jump@example.test'); await fill('password', 'fixture-password-123');
  await submit();
  await dialog.waitFor({state: 'hidden'});
  await click('journey-start');
  await fill('name', '内网机器人'); await fill('objective', '经跳板机接入真机并查看实时状态');
  await submit();

  // 1. Through the jump host: its fields appear only for that route; two fingerprints are confirmed in order.
  await robotLogin('跳板机后的机器人');
  if (await page.locator('#ssh-jump').isVisible()) throw Error('Jump host fields shown for a direct connection');
  await page.locator('#dialog [name=route]').selectOption('jump');
  await page.locator('#ssh-jump').waitFor({state: 'visible'});
  await fill('jump_host', '127.0.0.1'); await fill('jump_port', cfg.jump_port);
  await fill('jump_username', cfg.jump_user); await fill('jump_password', cfg.jump_password);
  await page.locator('#dialog details summary').click();
  await fill('watch_services', cfg.services.join('\n'));
  await submit();
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await dialog.getByText('第 2 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['经跳板机', `${cfg.jump_user}@127.0.0.1:${cfg.jump_port}`]);
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await dialog.getByText('选择工程模型', {exact: true}).waitFor();      // connected: the next step of onboarding
  await click('close');

  // 2. Live status: read-only, names the route, shows the device's own numbers and the watched services.
  await page.goto(cfg.url + '/#robots');
  await expectText(card('跳板机后的机器人'), ['经跳板机']);
  await card('跳板机后的机器人').locator('[data-action="engine-status"]').click();
  const status = page.locator('#ssh-status');
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['真机 · 只读', '经跳板机', 'dog-fixture', '6 分钟', '0.52 / 0.61 / 0.40', '61.3 °C', '75%',
                            'idle-start.service', 'nav-stack', '失败', 'recorder', '未运行']);
  fs.mkdirSync('artifacts', {recursive: true});
  await page.screenshot({path: 'artifacts/ssh-jump-live-status.png'});
  const firstStamp = await status.locator('.tag', {hasText: '更新于'}).innerText();
  await page.waitForTimeout(4200);                                      // at least one more poll
  await status.getByText('在线', {exact: true}).waitFor();
  if (await status.locator('.tag', {hasText: '更新于'}).innerText() === firstStamp) throw Error('Status did not refresh');

  // 3. Watched services can be changed from the status dialog.
  await click('engine-status-services');
  await fill('watch_services', 'bridge');
  await submit();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['bridge', '运行中']);
  if ((await status.innerText()).includes('recorder')) throw Error('Old watched service still shown');
  await click('close');

  // 4. A direct connection is unchanged: one fingerprint step, no jump host anywhere.
  await robotLogin('局域网里的机器人');
  await submit();
  await confirmFingerprint('确认 SSH 主机指纹');
  await dialog.getByText('选择工程模型', {exact: true}).waitFor();
  await click('close');
  await page.goto(cfg.url + '/#robots');
  if ((await card('局域网里的机器人').innerText()).includes('跳板机')) throw Error('Direct robot shows a jump host');
  await card('局域网里的机器人').locator('[data-action="engine-status"]').click();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['直连', 'dog-fixture']);
  await page.screenshot({path: 'artifacts/ssh-direct-live-status.png'});

  // 5. Pasting an ssh command fills the form; a command the app cannot follow says why.
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  const value = async name => page.locator(`#dialog [name="${name}"]`).inputValue();
  const command = page.locator('#dialog [name="ssh_command"]');
  await command.fill('ssh -J a@h1.example.com,b@h2.example.com robot@192.168.1.100');
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['一跳', '没有改动']);
  if (await value('host')) throw Error('A refused command must not fill the form');
  await command.fill('ssh robot@192.168.9.9');                          // typing: nothing happens until the field is left
  if (await value('host')) throw Error('Half-typed command rewrote the form');
  await command.dispatchEvent('change');
  if (await value('name') !== '192.168.9.9' || await value('route') !== 'direct') throw Error('Direct command not applied');
  await command.fill(`ssh -J ${cfg.jump_user}@127.0.0.1:${cfg.jump_port} -p ${cfg.target_port} ${cfg.target_user}@127.0.0.1`);
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['已填好', '经跳板机', '本机 SSH Agent']);
  const filled = {};
  for (const name of ['name', 'host', 'port', 'username', 'route', 'auth', 'jump_host', 'jump_port', 'jump_username', 'jump_auth']) {
    filled[name] = await value(name);
  }
  const expected = {name: '127.0.0.1', host: '127.0.0.1', port: String(cfg.target_port), username: cfg.target_user,
                    route: 'jump', auth: 'agent', jump_host: '127.0.0.1', jump_port: String(cfg.jump_port),
                    jump_username: cfg.jump_user, jump_auth: 'agent'};
  if (JSON.stringify(filled) !== JSON.stringify(expected)) throw Error('Pasted command filled: ' + JSON.stringify(filled));
  if (await page.locator('#ssh-password').isVisible()) throw Error('Password field shown for agent login');
  await page.screenshot({path: 'artifacts/ssh-command-filled.png'});
  // The fixtures only know passwords, so the two logins are switched back before connecting.
  await fill('name', '粘贴命令接入的机器人');
  await page.locator('#dialog [name=auth]').selectOption('password');
  await page.locator('#dialog [name=jump_auth]').selectOption('password');
  await fill('password', cfg.target_password); await fill('jump_password', cfg.jump_password);
  await submit();
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await dialog.getByText('选择工程模型', {exact: true}).waitFor();
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await expectText(card('粘贴命令接入的机器人'), ['经跳板机', `${cfg.target_user}@127.0.0.1:${cfg.target_port}`]);

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, jumpTwoStepFingerprints: true, liveStatus: true, statusRefreshes: true,
                              watchedServicesEdited: true, directUnchanged: true, pastedCommand: true,
                              jsErrors: errors.length}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
