// Browser acceptance: connect a robot through a jump host (two fingerprint steps, then the issue #2
// hardware scan through the same route), watch its
// read-only live status, change the watched services, connect a second robot directly, and
// connect a third one by pasting an ssh command.
// Issue #15: connect a robot through three jump hosts in a row (one fingerprint step per hop, typed by
// hand and pasted as `ssh -J a,b,c`), see which level a wrong login belongs to, and follow a jump host
// that only the first jump host's ssh config knows (ProxyJump).
// Fixture: tests/ssh_jump_browser_fixture.py (local SSH protocol fixtures, no real machine).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ssh-jump-ui.json'));
  const browser = await engine.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  let step = 'start';
  page.on('pageerror', e => errors.push(`${e.message} (during: ${step})`));
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
  // Issue #2: a new connection first scans the hardware (read-only), then continues to the investigation.
  const throughHardwareScan = async (expected = [], screenshot = '') => {
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
    await expectText(dialog, ['硬件初始化', ...expected]);
    if (screenshot) { fs.mkdirSync('artifacts', {recursive: true}); await page.screenshot({path: screenshot}); }
    await page.locator('#dialog [data-action="engine-discover"]').first().click();
    // No model is configured yet, so the investigation first shows the model dialog (issue #4:
    // the engineering model comes from TokenFans only).
    await dialog.getByText('工程助手的大模型只对接 TokenFans', {exact: true}).waitFor();
  };
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
  // the app opens on the sign-in screen; registration takes the e-mailed code (see gate_helper.cjs)
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '跳板机验收', email: 'jump@example.test', password: 'jump1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await fill('name', '内网机器人'); await fill('objective', '经跳板机接入真机并查看实时状态');
  await submit();

  // 1. Through the jump host: its fields appear only for that route; two fingerprints are confirmed in order.
  step = '1';
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
  // connected: the hardware scan runs through the jump host
  await throughHardwareScan(['经跳板机', '/dev/ttyUSB0', '需要处理的问题'], 'artifacts/ssh-jump-hardware-scan.png');
  await click('close');

  // 2. Live status: read-only, names the route, shows the device's own numbers and the watched services.
  step = '2';
  await page.goto(cfg.url + '/#robots');
  await expectText(card('跳板机后的机器人'), ['经跳板机']);
  await card('跳板机后的机器人').locator('[data-action="engine-status"]').click();
  const status = page.locator('#ssh-status');
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['真机 · 只读', '经跳板机', 'dog-fixture', '6 分钟', '0.52 / 0.61 / 0.40', '61.3 °C', '75%']);
  // Issue #8: the services are on the software tab, with the rest of the running software
  // (tests/live_status_browser.cjs covers that page in full).
  await status.locator('#ls-tab-software').click();
  const services = status.locator('[data-block="services"]');
  await services.waitFor({timeout: 30000});
  await expectText(services, ['idle-start.service', 'nav-stack', '失败', 'recorder', '未运行']);
  fs.mkdirSync('artifacts', {recursive: true});
  await page.screenshot({path: 'artifacts/ssh-jump-live-status.png'});
  // The page reads once a minute by itself; here it is asked to read now.
  const stamp = () => status.locator('.ls-updated').innerText();
  const firstStamp = await stamp();
  await page.waitForTimeout(1100);                                      // the stamp shows seconds
  await status.locator('[data-action="engine-status-refresh"]').click();
  for (let waited = 0; await stamp() === firstStamp; waited += 200) {
    if (waited > 15000) throw Error('Status did not refresh');
    await page.waitForTimeout(200);
  }

  // 3. Watched services can be changed from the status dialog.
  step = '3';
  await click('engine-status-services');
  await fill('watch_services', 'bridge');
  await submit();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await services.locator('tr', {hasText: 'bridge'}).first().waitFor({timeout: 15000});
  await expectText(services.locator('tr', {hasText: 'bridge'}).first(), ['关注', '运行中']);
  if ((await services.innerText()).includes('recorder')) throw Error('Old watched service still shown');
  await click('close');

  // 4. A direct connection is unchanged: one fingerprint step, no jump host anywhere.
  step = '4';
  await robotLogin('局域网里的机器人');
  await submit();
  await confirmFingerprint('确认 SSH 主机指纹');
  await throughHardwareScan(['/dev/ttyUSB0']);
  await click('close');
  await page.goto(cfg.url + '/#robots');
  if ((await card('局域网里的机器人').innerText()).includes('跳板机')) throw Error('Direct robot shows a jump host');
  await card('局域网里的机器人').locator('[data-action="engine-status"]').click();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['直连', 'dog-fixture']);
  await page.screenshot({path: 'artifacts/ssh-direct-live-status.png'});

  // 5. Pasting an ssh command fills the form; a command the app cannot follow says why.
  step = '5';
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  const value = async name => page.locator(`#dialog [name="${name}"]`).inputValue();
  const command = page.locator('#dialog [name="ssh_command"]');
  await command.fill('ssh -o ProxyCommand="nc %h %p" robot@192.168.1.100');
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['ProxyCommand', '没有改动']);
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
  await throughHardwareScan(['经跳板机']);
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await expectText(card('粘贴命令接入的机器人'), ['经跳板机', `${cfg.target_user}@127.0.0.1:${cfg.target_port}`]);

  // 6. The nested form `ssh gate 'ssh dog'`: the first host is the jump host and "dog" is a name
  //    only the jump host knows. Its ssh config resolves it; user and port stay empty in the form.
  step = '6';
  await click('engine-ssh');
  await command.fill(`ssh -tt -p ${cfg.jump_port} ${cfg.jump_user}@127.0.0.1 'ssh -tt dog'`);
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['目标名称 dog', '经跳板机', '跳板机上的 SSH 配置']);
  const nested = {host: await value('host'), port: await value('port'), username: await value('username'),
                  route: await value('route'), jump_port: await value('jump_port'),
                  resolve: await page.locator('#dialog [name=resolve_on_jump]').isChecked()};
  const wanted = {host: 'dog', port: '', username: '', route: 'jump', jump_port: String(cfg.jump_port), resolve: true};
  if (JSON.stringify(nested) !== JSON.stringify(wanted)) throw Error('Nested command filled: ' + JSON.stringify(nested));
  await fill('name', '跳板机上叫 dog 的机器人');
  await page.locator('#dialog [name=auth]').selectOption('password');
  await page.locator('#dialog [name=jump_auth]').selectOption('password');
  await fill('password', cfg.target_password); await fill('jump_password', cfg.jump_password);
  await submit();                                                       // user and port are empty and that is fine
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await dialog.getByText('第 2 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, [`dog → ${cfg.target_user}@127.0.0.1:${cfg.target_port}`]);     // resolved, shown before trusting
  await page.screenshot({path: 'artifacts/ssh-nested-resolved.png'});
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await throughHardwareScan(['经跳板机']);
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await expectText(card('跳板机上叫 dog 的机器人'), ['经跳板机', '跳板机上的名称 dog']);
  await card('跳板机上叫 dog 的机器人').locator('[data-action="engine-status"]').click();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 15000});
  await expectText(status, ['dog-fixture', '经跳板机']);

  // ---------------------------------------------------------------- issue #15: several jump hosts in a row
  await click('close');
  const gates = cfg.chain, hopPrefix = level => (level === 1 ? 'jump_' : `jump${level}_`);
  const route = gates.map(gate => `${gate.user}@127.0.0.1:${gate.port}`).join(' → ');
  const fillHop = async (level, gate, password = gate.password) => {
    await fill(hopPrefix(level) + 'host', '127.0.0.1'); await fill(hopPrefix(level) + 'port', gate.port);
    await fill(hopPrefix(level) + 'username', gate.user); await fill(hopPrefix(level) + 'password', password);
  };
  const addHop = async () => page.locator('#ssh-hop-add').click();
  const hops = page.locator('#ssh-hops .ssh-hop');
  const jumpStep = level => `第 ${level} 步：确认第 ${level} 级跳板机指纹`;

  // 7. Three jump hosts typed by hand: one more can be added and removed again; every hop has its own
  //    fingerprint step, in order, and the robot's comes last.
  step = '7';
  await robotLogin('三级跳板机后的机器人');
  await page.locator('#dialog [name=route]').selectOption('jump');
  await expectText(hops.first(), ['跳板机']);
  if ((await hops.first().innerText()).includes('第 1 级')) throw Error('One jump host must not be numbered');
  await fillHop(1, gates[0]);
  await addHop(); await addHop(); await addHop();
  if (await hops.count() !== 4) throw Error('Three more jump hosts were not added');
  await expectText(hops.nth(3), ['第 4 级跳板机']);
  await hops.nth(3).locator('[data-ssh-hop="remove"]').click();
  if (await hops.count() !== 3) throw Error('The fourth jump host was not removed');
  if (await value('jump_host') !== '127.0.0.1' || await value('jump_password') !== gates[0].password) {
    throw Error('Adding and removing jump hosts lost what was typed into the first');
  }
  await fillHop(2, gates[1]); await fillHop(3, gates[2]);
  if (await value('jump_count') !== '3') throw Error('jump_count is ' + await value('jump_count'));
  await page.screenshot({path: 'artifacts/ssh-three-hops-form.png', fullPage: true});
  await submit();
  for (const level of [1, 2, 3]) {
    await dialog.getByText(jumpStep(level), {exact: true}).waitFor();
    await expectText(dialog, [`${gates[level - 1].user}@127.0.0.1:${gates[level - 1].port}`, '共 3 级']);
    if (level === 2) await page.screenshot({path: 'artifacts/ssh-three-hops-step.png'});
    await confirmFingerprint(jumpStep(level));
  }
  await dialog.getByText('第 4 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['经跳板机', route]);
  await confirmFingerprint('第 4 步：确认目标机指纹');
  await throughHardwareScan(['经跳板机', '/dev/ttyUSB0'], 'artifacts/ssh-three-hops-hardware-scan.png');
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await expectText(card('三级跳板机后的机器人'), ['经跳板机', route]);
  await card('三级跳板机后的机器人').locator('[data-action="engine-status"]').click();
  await status.getByText('在线', {exact: true}).waitFor({timeout: 20000});
  await expectText(status, ['dog-fixture', '经跳板机', route]);
  await page.screenshot({path: 'artifacts/ssh-three-hops-live-status.png'});
  await click('close');

  // 7b. "配置连接" (issue #9) opens the form with all three saved jump hosts. No password is shown; saving
  //     without a change keeps every login, and reading the fingerprints again takes one step per hop.
  step = '7b';
  const editThreeHops = async () => {
    await page.goto(cfg.url + '/#robots');
    await card('三级跳板机后的机器人').locator('[data-action="engine-ssh-edit"]').click();
    await dialog.getByText('配置连接', {exact: true}).waitFor();
  };
  await editThreeHops();
  if (await hops.count() !== 3) throw Error('The edit form does not show three jump hosts');
  for (const [index, gate] of gates.entries()) {
    const pre = hopPrefix(index + 1);
    const shown = [await value(pre + 'host'), await value(pre + 'port'), await value(pre + 'username'), await value(pre + 'password')];
    if (JSON.stringify(shown) !== JSON.stringify(['127.0.0.1', String(gate.port), gate.user, ''])) {
      throw Error(`Saved jump host ${index + 1} shown as ${JSON.stringify(shown)}`);
    }
    if (await page.locator(`#dialog [name="${pre}password"]`).getAttribute('placeholder') !== '留空：沿用已保存的密码') {
      throw Error(`No keep hint for the password of jump host ${index + 1}`);
    }
  }
  if ((await page.locator('#dialog button[type=submit]').innerText()).trim() !== '保存') throw Error('An unchanged route asks for fingerprints');
  // Adding a jump host and taking it away again redraws the rows: the saved ones keep their "leave empty" hints,
  // and the new one, which has no saved login, gets none.
  const keepHint = level => page.locator(`#dialog [name="${hopPrefix(level)}password"]`).getAttribute('placeholder');
  await addHop();
  if (await keepHint(2) !== '留空：沿用已保存的密码' || await keepHint(4)) throw Error('Hints after adding a jump host: ' + await keepHint(2) + ' / ' + await keepHint(4));
  if ((await page.locator('#dialog button[type=submit]').innerText()).trim() !== '读取主机指纹') throw Error('A fourth jump host does not ask for fingerprints');
  await hops.nth(3).locator('[data-ssh-hop="remove"]').click();
  if (await keepHint(3) !== '留空：沿用已保存的密码') throw Error('The hint was lost when a jump host was removed');
  if ((await page.locator('#dialog button[type=submit]').innerText()).trim() !== '保存') throw Error('The route is as saved again, but fingerprints are asked for');
  await page.locator('#dialog button[type=submit]').click();
  await dialog.waitFor({state: 'hidden', timeout: 30000});                // saved: every kept login was tried first
  await editThreeHops();
  await page.locator('#dialog [name=rescan]').check();
  await submit();
  for (const level of [1, 2, 3]) {
    await dialog.getByText(jumpStep(level), {exact: true}).waitFor();
    await expectText(dialog, [`和这台机器人原来保存的第 ${level} 级跳板机指纹一致`]);
    await confirmFingerprint(jumpStep(level));
  }
  await dialog.getByText('第 4 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, ['和这台机器人原来保存的设备指纹一致']);
  await page.locator('#dialog input[type=checkbox]').check();
  await page.locator('#dialog button[type=submit]', {hasText: '确认并保存'}).click();
  await dialog.waitFor({state: 'hidden', timeout: 30000});
  await expectText(card('三级跳板机后的机器人'), ['经跳板机', route]);

  // 8. A wrong login in the middle is reported with its level, after that hop's fingerprint and not before.
  step = '8';
  await robotLogin('第二级密码错误');
  await page.locator('#dialog [name=route]').selectOption('jump');
  await addHop(); await addHop();
  await fillHop(1, gates[0]); await fillHop(2, gates[1], 'not-' + gates[1].password); await fillHop(3, gates[2]);
  await submit();
  await confirmFingerprint(jumpStep(1));
  await dialog.getByText(jumpStep(2), {exact: true}).waitFor();
  await page.locator('#dialog input[type=checkbox]').check();
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('.form-error', {hasText: '第 2 级跳板机拒绝登录'}).waitFor({timeout: 15000});
  await page.screenshot({path: 'artifacts/ssh-three-hops-wrong-login.png'});
  await click('close');

  // 9. `ssh -J a,b,c target` pasted: three jump hosts are filled in, in order. Until issue #15 this was refused.
  step = '9';
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  await page.locator('#dialog [name=route]').selectOption('jump');
  await fill('jump_password', gates[0].password);                        // typed before the command is pasted: it stays
  await command.fill(`ssh -J ${gates.map(gate => `${gate.user}@127.0.0.1:${gate.port}`).join(',')} -p ${cfg.target_port} ${cfg.target_user}@127.0.0.1`);
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['已填好', '经跳板机', route]);
  if (await value('jump_password') !== gates[0].password) throw Error('Pasting a command wiped the password typed for the first jump host');
  for (const [index, gate] of gates.entries()) {
    const pre = hopPrefix(index + 1);
    const shown = [await value(pre + 'host'), await value(pre + 'port'), await value(pre + 'username'), await value(pre + 'auth')];
    if (JSON.stringify(shown) !== JSON.stringify(['127.0.0.1', String(gate.port), gate.user, 'agent'])) {
      throw Error(`Pasted jump host ${index + 1} filled: ${JSON.stringify(shown)}`);
    }
  }
  if (await value('jump_count') !== '3' || await value('route') !== 'jump') throw Error('The pasted route is not three jump hosts');
  await page.screenshot({path: 'artifacts/ssh-three-hops-pasted.png', fullPage: true});
  // A direct command pasted after a nested one leaves nothing of the earlier one behind.
  await command.fill(`ssh -p ${cfg.jump_port} ${cfg.jump_user}@127.0.0.1 'ssh dog'`);
  await command.dispatchEvent('change');
  if (!await page.locator('#dialog [name=resolve_on_jump]').isChecked()) throw Error('The nested command did not mark the target as a name');
  await command.fill('ssh robot@192.168.9.9');
  await command.dispatchEvent('change');
  if (await value('route') !== 'direct' || await page.locator('#dialog [name=resolve_on_jump]').isChecked()) {
    throw Error('A direct command kept the "name on the jump host" choice of the command before it');
  }
  await click('close');

  // 10. A jump host nobody typed: the first jump host's ssh config sends "pup" through "inner" (ProxyJump).
  //     It is shown with its fingerprint, asks for its own login, and only then the robot's key is read.
  step = '10';
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  await command.fill(`ssh -p ${cfg.jump_port} ${cfg.jump_user}@127.0.0.1 'ssh pup'`);
  await command.dispatchEvent('change');
  await expectText(page.locator('#ssh-command-result'), ['目标名称 pup', 'ProxyJump']);
  await fill('name', '配置里还有一级的机器人');
  await page.locator('#dialog [name=auth]').selectOption('password');
  await page.locator('#dialog [name=jump_auth]').selectOption('password');
  await fill('password', cfg.target_password); await fill('jump_password', cfg.jump_password);
  await submit();
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await dialog.getByText(jumpStep(2), {exact: true}).waitFor();
  await expectText(dialog, [`inner → ${cfg.inner.user}@127.0.0.1:${cfg.inner.port}`, '不是你填写的', 'ProxyJump',
                            '第 1 级跳板机上的 SSH 配置']);
  // Nothing is chosen for a machine the user did not type: no local key or password goes to it unasked.
  if (await value('step_auth') !== '') throw Error('A login method was chosen in advance for a jump host that was found');
  await page.locator('#dialog input[type=checkbox]').check();
  await page.locator('#dialog button[type=submit]').click();
  await page.waitForTimeout(300);
  if (!await dialog.getByText(jumpStep(2), {exact: true}).isVisible()) throw Error('The step went on without a login method');
  await page.locator('#dialog input[type=checkbox]').uncheck();
  await page.locator('#dialog [name=step_auth]').selectOption('password');
  await fill('step_password', cfg.inner.password);
  await page.screenshot({path: 'artifacts/ssh-found-hop-step.png'});
  await confirmFingerprint(jumpStep(2));
  await dialog.getByText('第 3 步：确认目标机指纹', {exact: true}).waitFor();
  await expectText(dialog, [`pup → ${cfg.target_user}@127.0.0.1:${cfg.target_port}`, '名称 inner']);
  await confirmFingerprint('第 3 步：确认目标机指纹');
  await throughHardwareScan(['经跳板机']);
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await expectText(card('配置里还有一级的机器人'), ['经跳板机', '名称 inner', '跳板机上的名称 pup']);

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, jumpTwoStepFingerprints: true, liveStatus: true, statusRefreshes: true,
                              watchedServicesEdited: true, directUnchanged: true, pastedCommand: true,
                              nestedCommand: true, hardwareScanAfterConnect: true, threeJumpHosts: true,
                              threeJumpHostsEdited: true, wrongLoginNamesItsLevel: true, pastedThreeJumpHosts: true,
                              proxyJumpFollowed: true,
                              jsErrors: errors.length}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
