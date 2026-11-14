// Issue #10 browser acceptance: the "让助手修复硬件问题" form asks for no Token limit and no run
// time, sends neither, and the repair it starts still goes through the per-command approval,
// the rescan and the report without being stopped by usage or by the clock.
// Fixture: tests/hardware_browser_fixture.py (SSH protocol stand-in, scripted model behind a
// TokenFans stand-in; one repair per fixture run, so restart it before each run of this script).
// Usage: node tests/hardware_repair_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const {connectTokenFans, useTokenFansModel} = require('./tokenfans_browser.cjs');

const MODEL = 'fixture-repair';
// What the scripted repair model reports on (tests/test_hardware_init.py RepairModel).
const ISSUES = ['serial-perm:/dev/ttyUSB0', 'serial-perm:/dev/ttyTHS0', 'usb-driver:1-4'];
const REMOVED_LABELS = ['Token 观察限额', '最长运行时间', '硬限额'];
const LIMIT_FIELDS = ['max_tokens', 'max_minutes'];
const ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];
const WIDE = {width: 1440, height: 1000}, SMALL = {width: 900, height: 640};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-hardware-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: WIDE}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  failed = async () => {
    await page.screenshot({path: path.join(shots, 'hardware-repair-failure.png')}).catch(() => {});
    console.error('page errors:', errors);
  };
  const dlg = page.locator('#dialog');
  const shot = name => page.screenshot({path: path.join(shots, 'hardware-repair-' + name + '.png')});
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
  const repairSession = () => page.evaluate(() => rows('agent_sessions').find(s => s.mode === 'repair') || null);

  // Sign in, a project, the TokenFans stand-in with the scripted repair model as default.
  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '硬件修复验收', email: 'repair@example.test', password: 'fix1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await fill('name', '巡检机器人接入'); await fill('objective', '修好驱动与设备权限');
  await submit();
  await connectTokenFans(page, cfg);
  await useTokenFansModel(page, cfg, MODEL);

  // An SSH robot: the read-only hardware scan opens right after the fingerprint is confirmed.
  await page.goto(cfg.url + '/#robots');
  await click('engine-ssh');
  await fill('name', '巡检机器人'); await fill('host', '127.0.0.1'); await fill('port', cfg.ssh_port);
  await fill('username', cfg.ssh_user); await fill('password', cfg.ssh_login);
  await submit();
  await dlg.getByText('确认 SSH 主机指纹', {exact: true}).waitFor();
  await dlg.locator('input[type=checkbox]').check();
  await submit();
  await dlg.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});
  for (const box of await dlg.locator('[name=hw-issue]').all()) {
    if (ISSUES.includes(await box.getAttribute('value'))) await box.check(); else await box.uncheck();
  }

  // 1 · The repair form: the issues, the model, the two consents, and nothing about limits.
  await click('hardware-repair');
  await dlg.getByText('让助手修复硬件问题', {exact: true}).waitFor();
  const form = dlg.locator('form');
  const listed = await form.locator('[name=issue_ids]').evaluateAll(boxes => boxes.map(b => b.value).sort());
  if (JSON.stringify(listed) !== JSON.stringify([...ISSUES].sort())) throw Error('Repair form lists: ' + listed.join(', '));
  if (await form.locator(LIMIT_FIELDS.map(n => `[name=${n}]`).join(', ')).count()) throw Error('Repair form still asks for limits');
  if (await form.locator('input[type=number]').count()) throw Error('Repair form still has a number field');
  for (const t of REMOVED_LABELS) if (await form.getByText(t).count()) throw Error('Repair form still shows: ' + t);
  if (!(await form.locator('[name=model_id] option:checked').innerText()).includes(MODEL)) throw Error('TokenFans default model not pre-selected');
  // Both consents are still required before the form can be sent.
  if (await form.evaluate(f => f.checkValidity())) throw Error('Repair form can be sent without the consents');
  await form.locator('[name=data_authorized]').check();
  if (await form.evaluate(f => f.checkValidity())) throw Error('Repair form can be sent without allowing device changes');
  await form.locator('[name=modify_authorized]').check();
  if (!(await form.evaluate(f => f.checkValidity()))) throw Error('Repair form cannot be sent with every field filled');
  await shot('1-form');
  await page.setViewportSize(SMALL);
  const overflow = await dlg.evaluate(d => d.scrollWidth - d.clientWidth);
  if (overflow > 1) throw Error(`Repair form overflows sideways by ${overflow}px at ${SMALL.width}px`);
  await shot('1-form-small');
  await page.setViewportSize(WIDE);

  // 2 · The request carries no limit, and the session it starts has none.
  const sent = page.waitForRequest(r => r.url().endsWith('/api/engineering/hardware/repair'));
  await submit();
  const body = (await sent).postDataJSON();
  for (const k of LIMIT_FIELDS) if (k in body) throw Error(`Repair request still sends ${k}: ${JSON.stringify(body[k])}`);
  if (body.data_authorized !== true || body.modify_authorized !== true) throw Error('Consents missing from the repair request');
  await page.locator('#engine-live').waitFor({timeout: 15000});
  const started = await repairSession();
  if (!started) throw Error('No repair session after sending the form');
  for (const k of LIMIT_FIELDS) if (started[k] !== null) throw Error(`Repair session has ${k}=${JSON.stringify(started[k])}, expected none`);

  // 3 · Every command still waits for the user: approve the one fix the model proposes.
  await page.locator('#engine-live [data-action=engine-approval]').waitFor({timeout: 30000});
  await click('engine-approval');
  await dlg.getByText('在机器人上执行修复命令', {exact: true}).waitFor();
  if (!(await dlg.innerText()).includes('usermod -aG dialout robot')) throw Error('Approval lacks the exact command');
  await shot('2-approval');
  await dlg.locator('[name=decision]').selectOption('accept');
  await submit();

  // 4 · The repair finishes with its report; nothing stopped it for usage or time.
  await page.waitForFunction(active => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    return s && !active.includes(s.status);
  }, ACTIVE, {timeout: 90000});
  const done = await repairSession();
  if (done.status !== 'completed') throw Error(`Repair ended ${done.status}: ${done.error || ''}`);
  if (!done.repair_report_id) throw Error('Repair finished without a report');
  if ((done.events || []).some(e => e.kind === 'budget')) throw Error('Repair was stopped by a Token limit');
  for (const k of LIMIT_FIELDS) if (done[k] !== null) throw Error(`Finished repair has ${k}=${JSON.stringify(done[k])}`);
  await page.locator('#engine-live').getByText('已修复').first().waitFor({timeout: 15000});
  await shot('3-report');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), formHasNoLimitFields: true, requestSendsNoLimits: true,
                              sessionHasNoLimits: true, approvalStillRequired: true, repairCompletedWithReport: true,
                              reportedTokens: done.usage?.totalTokens ?? 0, jsErrors: errors.length}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
