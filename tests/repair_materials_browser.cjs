// Issue #30 browser acceptance: a repair session says what it found in the library. With no usable driver
// material for the device it tells the user to upload one first (session view and fix chat); once there is
// one, the next repair gives it to the assistant and says so. Nothing in the page shows who confirmed it.
// Fixture: tests/repair_materials_browser_fixture.py (SSH stand-in, scripted model behind a TokenFans
// stand-in, a library stand-in switched by the fixture's control address). Restart it before each run.
// Usage: node tests/repair_materials_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const {connectTokenFans, useTokenFansModel} = require('./tokenfans_browser.cjs');

const ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-repair-materials-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  failed = async () => {
    await page.screenshot({path: path.join(shots, 'repair-materials-failure.png')}).catch(() => {});
    console.error('page errors:', errors);
  };
  const dlg = page.locator('#dialog');
  const shot = name => page.screenshot({path: path.join(shots, 'repair-materials-' + name + '.png')});
  const click = async a => {
    const inDialog = page.locator(`#dialog[open] [data-action="${a}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${a}"]`)).first().click();
  };
  const fill = async (n, v) => dlg.locator(`[name="${n}"]`).first().fill(String(v));
  const submit = async () => {
    await dlg.locator('button[type=submit]').first().click();
    await page.waitForTimeout(200);
    const e = await dlg.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const repairs = () => page.evaluate(() => rows('agent_sessions').filter(s => s.mode === 'repair'));
  // Tick only the driver problem in the open hardware dialog and start a repair that runs to its report.
  const repair = async (issue, known = []) => {
    await dlg.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
    for (const box of await dlg.locator('[name=hw-issue]').all()) {
      if (await box.getAttribute('value') === issue) await box.check(); else await box.uncheck();
    }
    await click('hardware-repair');
    await dlg.getByText('让助手修复硬件问题', {exact: true}).waitFor();
    await dlg.locator('form [name=data_authorized]').check();
    await dlg.locator('form [name=modify_authorized]').check();
    await submit();
    await page.locator('#engine-live').waitFor({timeout: 15000});
    await page.waitForFunction(([active, ids]) => {
      const list = rows('agent_sessions').filter(s => s.mode === 'repair');
      return list.some(s => !ids.includes(s.id)) && list.every(s => !active.includes(s.status));
    }, [ACTIVE, known], {timeout: 90000});
    const s = (await repairs()).find(x => !known.includes(x.id));
    if (s.status !== 'completed') throw Error(`Repair ended ${s.status}: ${s.error || ''}`);
    return s;
  };
  const chat = s => page.evaluate(id => invChatLog(rows('agent_sessions').find(x => x.id === id)), s.id);
  const live = () => page.locator('#engine-live').innerText();

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '资料库验收', email: `materials-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'Mat' + crypto.randomBytes(6).toString('hex') + '7'});
  await click('journey-start');
  await fill('name', '巡检机器人接入'); await fill('objective', '装上运控驱动');
  await submit();
  await connectTokenFans(page, cfg);
  await useTokenFansModel(page, cfg, cfg.model);
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  await fill('name', '巡检狗'); await fill('host', '127.0.0.1'); await fill('port', cfg.ssh_port);
  await page.locator('#dialog [name=family]').selectOption('quadruped');
  await fill('username', cfg.ssh_user); await fill('password', cfg.ssh_login);
  await submit();
  await dlg.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await dlg.locator('input[type=checkbox]').check();
  await submit();

  // 1 · No usable driver material: the session says to upload one first, in both views.
  const first = await repair(cfg.issues[0]);
  const [missing] = (first.events || []).filter(e => e.kind === 'library');
  if (!missing || missing.missing !== true) throw Error('No "upload first" event: ' + JSON.stringify(first.events));
  await page.locator('#engine-live .tag', {hasText: '资料库'}).first().waitFor({timeout: 15000});
  const shown = await live();
  for (const words of ['没有这台设备可用的驱动资料', '设备档案', '上传', '不会自己猜']) {
    if (!shown.includes(words)) throw Error(`Session view lacks "${words}"`);
  }
  if (shown.includes('>library<') || /\blibrary\b/.test(shown)) throw Error('Session view shows the raw event kind');
  const chatMissing = await chat(first);
  if (!chatMissing.includes('inv-act warn') || !chatMissing.includes('没有这台设备可用的驱动资料')) throw Error('Fix chat does not warn: ' + chatMissing);
  await shot('1-upload-first');

  // 2 · A usable driver manual appears for the device: the next repair gives it to the assistant and says so.
  await fetch(cfg.control + '/found', {method: 'POST'});
  await page.goto(cfg.url + '/#robots');
  await click('hardware-open');
  const second = await repair(cfg.issues[1], [first.id]);
  if (second.id === first.id) throw Error('No second repair session');
  const [found] = (second.events || []).filter(e => e.kind === 'library');
  if (!found || found.missing !== false) throw Error('No "materials found" event: ' + JSON.stringify(second.events));
  await page.locator('#engine-live').getByText('1 份可用资料', {exact: false}).first().waitFor({timeout: 15000});
  const shownFound = await live();
  if (!shownFound.includes('demo-driver-manual.md')) throw Error('Session view does not name the material');
  const chatFound = await chat(second);
  if (!chatFound.includes('读取：资料库里这台设备有 1 份可用资料')) throw Error('Fix chat does not show the read: ' + chatFound);
  const everything = await page.evaluate(() => JSON.stringify(state));
  if (everything.includes('checker@example.test')) throw Error('Who confirmed the material reached the page');
  await shot('2-materials-found');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), uploadFirstShown: true, chatWarns: true, foundShown: true,
                              confirmerNotShown: true, jsErrors: errors.length}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
