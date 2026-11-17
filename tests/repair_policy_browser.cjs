// Issue #13 browser acceptance.
//  1. A problem software cannot fix is only annotated: its checkbox is disabled, the server refuses
//     it too, and a person marks it as handled (or takes that back) to make it selectable again.
//  2. The repair form ticks "默认允许执行" by default; the repair it starts runs its device command
//     without anyone approving, refuses what the product always refused, and saves its report.
//     The session view shows how it runs and can switch back to asking.
// Fixture: tests/hardware_browser_fixture.py (SSH protocol stand-in, scripted model behind a
// TokenFans stand-in; one repair per fixture run, so restart it before each run of this script).
// Usage: node tests/repair_policy_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const {connectTokenFans, useTokenFansModel} = require('./tokenfans_browser.cjs');

const MODEL = 'fixture-repair';
// What the scripted repair model reports on (tests/test_hardware_init.py RepairModel).
const ISSUES = ['serial-perm:/dev/ttyUSB0', 'serial-perm:/dev/ttyTHS0', 'usb-driver:1-4'];
const ONSITE = 'net-down:can0';            // the scan says it needs on-site work
const NOTE = 'unit:nvargus-daemon.service'; // a note the user judges: never blocked
const ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];
const WIDE = {width: 1440, height: 1000}, SMALL = {width: 900, height: 640}, NARROW = {width: 700, height: 900};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-hardware-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: WIDE}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  failed = async () => {
    await page.screenshot({path: path.join(shots, 'repair-policy-failure.png')}).catch(() => {});
    console.error('page errors:', errors);
  };
  const dlg = page.locator('#dialog');
  const shot = name => page.screenshot({path: path.join(shots, 'repair-policy-' + name + '.png')});
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
  const box = id => dlg.locator(`[name=hw-issue][value="${id}"]`);
  const strip = id => dlg.locator(`.rp-strip[data-rp-issue="${id}"]`);
  const noSidewaysScroll = async (size, what) => {
    await page.setViewportSize(size);
    const overflow = await dlg.evaluate(d => d.scrollWidth - d.clientWidth);
    if (overflow > 1) throw Error(`${what} overflows sideways by ${overflow}px at ${size.width}px`);
  };

  // Sign in, a project, the TokenFans stand-in with the scripted repair model as default.
  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '硬件标注验收', email: 'policy@example.test', password: 'fix1-' + crypto.randomBytes(8).toString('hex')});
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
  const robotId = await page.evaluate(() => rows('robots').find(r => r.connection_id).id);

  // 1 · The on-site problem is annotated and cannot be ticked; software problems and notes can.
  if (!(await box(ONSITE).isDisabled()) || await box(ONSITE).isChecked()) throw Error('The on-site problem can still be ticked');
  if (!(await strip(ONSITE).innerText()).includes('等待现场处理')) throw Error('The on-site problem has no annotation');
  for (const id of [...ISSUES, NOTE]) {
    if (await box(id).isDisabled()) throw Error('Wrongly blocked: ' + id);
    if (await strip(id).count()) throw Error('Wrongly annotated: ' + id);
  }
  if (!(await box(ISSUES[0]).isChecked())) throw Error('Software problems are no longer ticked by default');
  if (await box(NOTE).isChecked()) throw Error('A note is ticked by default');
  await strip(ONSITE).scrollIntoViewIfNeeded();
  await shot('1-blocked');
  await noSidewaysScroll(SMALL, 'Hardware page'); await shot('1-blocked-small');
  await noSidewaysScroll(NARROW, 'Hardware page'); await shot('1-blocked-narrow');
  await page.setViewportSize(WIDE);
  // The server refuses it as well, whatever the page sends.
  const refusal = await page.evaluate(async ([robot, issue]) => {
    const scan = hwLatest(robot);
    try {
      await api('engineering/hardware/repair', {project_id: projectId, robot_id: robot, scan_id: scan.id, issue_ids: [issue], data_authorized: true, modify_authorized: true});
      return '';
    } catch (err) { return err.message; }
  }, [robotId, ONSITE]);
  if (!refusal.includes('现场处理')) throw Error('The server accepted a blocked problem: ' + JSON.stringify(refusal));

  // 2 · A person marks it as handled: it can be ticked again, and what was ticked before is kept.
  await box(ISSUES[2]).uncheck();
  await strip(ONSITE).locator('[data-action=hardware-mark]').click();
  await strip(ONSITE).locator('[data-action=hardware-unmark]').waitFor();
  if (!(await strip(ONSITE).innerText()).includes('已标注处理完成')) throw Error('Marked problem does not say so');
  if (await box(ONSITE).isDisabled()) throw Error('Marked problem is still disabled');
  if (await box(ONSITE).isChecked()) throw Error('Marked problem was ticked for the user');
  if (await box(ISSUES[2]).isChecked() || !(await box(ISSUES[0]).isChecked())) throw Error('Marking changed what the user had ticked');
  await strip(ONSITE).scrollIntoViewIfNeeded();
  await shot('2-handled');
  // ... and takes it back: blocked again.
  await strip(ONSITE).locator('[data-action=hardware-unmark]').click();
  await strip(ONSITE).locator('[data-action=hardware-mark]').waitFor();
  if (!(await box(ONSITE).isDisabled())) throw Error('Taking the mark back did not block the problem again');

  // What the assistant could not fix carries its explanation (drawn from state.hardware_blocks).
  await page.evaluate(([robot, issue]) => {
    state.hardware_blocks.push({robot_id: robot, project_id: projectId, issue_id: issue, state: 'blocked', reason: 'not_fixed',
                                note: '试过加载 uvcvideo，设备仍然没有枚举出来', repair_id: null, blocked_at: new Date().toISOString(), handled_at: null});
    hardwareOpen(robot, true);
  }, [robotId, ISSUES[2]]);
  const explained = await strip(ISSUES[2]).innerText();
  if (!explained.includes('助手未能修复') || !explained.includes('试过加载 uvcvideo')) throw Error('Annotation lacks the assistant\'s explanation: ' + explained);
  if (!(await box(ISSUES[2]).isDisabled())) throw Error('A problem the assistant could not fix can still be ticked');
  await strip(ISSUES[2]).scrollIntoViewIfNeeded();
  await shot('2-not-fixed');
  await page.evaluate(robot => refresh(true).then(() => hardwareOpen(robot, true)), robotId);

  // 3 · The repair form: "默认允许执行" is there and ticked.
  for (const b of await dlg.locator('[name=hw-issue]').all()) {
    if (await b.isDisabled()) continue;
    if (ISSUES.includes(await b.getAttribute('value'))) await b.check(); else await b.uncheck();
  }
  await click('hardware-repair');
  await dlg.getByText('让助手修复硬件问题', {exact: true}).waitFor();
  const form = dlg.locator('form');
  const listed = await form.locator('[name=issue_ids]').evaluateAll(boxes => boxes.map(b => b.value).sort());
  if (JSON.stringify(listed) !== JSON.stringify([...ISSUES].sort())) throw Error('Repair form lists: ' + listed.join(', '));
  const choice = form.locator('[name=auto_approve]');
  if (await choice.count() !== 1 || !(await choice.isChecked())) throw Error('"默认允许执行" is missing or not ticked by default');
  if (!(await form.locator('.rp-choice').innerText()).includes('默认允许执行')) throw Error('The choice is not labelled');
  await form.locator('[name=data_authorized]').check();
  await form.locator('[name=modify_authorized]').check();
  await choice.scrollIntoViewIfNeeded();
  await shot('3-form');
  await noSidewaysScroll(SMALL, 'Repair form'); await shot('3-form-small');
  await page.setViewportSize(WIDE);

  // 4 · The request says so, and the repair finishes without anyone approving a command.
  const sent = page.waitForRequest(r => r.url().endsWith('/api/engineering/hardware/repair'));
  await submit();
  const body = (await sent).postDataJSON();
  if (body.auto_approve !== true) throw Error('Repair request does not carry the choice: ' + JSON.stringify(body.auto_approve));
  await page.locator('#engine-live').waitFor({timeout: 15000});
  await page.locator('#engine-live .rp-auto.is-on').waitFor({timeout: 15000});
  await page.waitForFunction(active => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    return s && (!active.includes(s.status) || s.pending_approval);
  }, ACTIVE, {timeout: 90000});
  const done = await repairSession();
  if (done.pending_approval) throw Error('The repair asked for approval: ' + done.pending_approval.method);
  if (done.status !== 'completed') throw Error(`Repair ended ${done.status}: ${done.error || ''}`);
  if (!done.repair_report_id) throw Error('Repair finished without a report');
  const messages = (done.events || []).map(e => e.message);
  if (!messages.some(m => m.includes('默认允许执行') && m.includes('usermod -aG dialout robot'))) throw Error('The session does not record the command that ran unasked');
  if (messages.some(m => m.includes('用户批准'))) throw Error('The session claims the user approved something');
  if (!(done.events || []).some(e => e.kind === 'tool_error' && e.message.includes('不允许重启'))) throw Error('The product no longer refuses a reboot');
  await page.locator('#engine-live').getByText('已修复').first().waitFor({timeout: 15000});
  await shot('4-ran-unasked');

  // What an approval looks like in each kind of session (drawn from made-up sessions, nothing is sent).
  const cards = await page.evaluate(() => {
    const request = {command: 'curl -o /tmp/driver.deb https://example.com/driver.deb', purpose: '下载驱动', issue_ids: [], risk: 'low', rollback: '删除文件',
                     sudo: 'none', timeout_seconds: 30, username: 'robot', host: '127.0.0.1', port: 22};
    const draw = (hardware, pending) => {
      const box = document.createElement('div');
      box.innerHTML = engineApproval({mode: 'repair', hardware: {scan_id: null, issue_ids: [], ...hardware}, pending_approval: pending});
      const choice = box.querySelector('[name=auto_approve]');
      return {offered: !!choice, ticked: !!choice?.checked, why: box.querySelector('.rp-hold')?.textContent || ''};
    };
    const fix = {id: 9, method: 'device_fix/requestApproval', request};
    return {held: draw({auto_approve: true, auto_hold: {id: 9, command: request.command, reason: '会从网络取回内容或向外传输数据'}}, fix),
            stale: draw({auto_approve: true, auto_hold: {id: 9, command: 'wget https://example.com/earlier', reason: '上一轮另一条命令的原因'}}, fix),
            asks: draw({auto_approve: false}, fix), neverChose: draw({}, fix),
            scope: draw({auto_approve: true}, {id: 9, method: 'device_request_scope/requestApproval', request: {path: '/opt/robot', purpose: '读取配置', username: 'robot', host: '127.0.0.1', port: 22}})};
  });
  if (!cards.held.why.includes('会从网络取回内容') || !cards.held.ticked) throw Error('A held command does not say why, or loses the choice: ' + JSON.stringify(cards.held));
  if (cards.stale.why) throw Error('An approval shows the reason of an earlier command');
  if (!cards.asks.offered || cards.asks.ticked) throw Error('A session that asks has the choice pre-ticked');
  if (cards.neverChose.ticked) throw Error('A session where nobody chose has the choice pre-ticked');
  if (cards.scope.offered) throw Error('Reading a new directory offers the choice: it always asks');

  // 5 · The session view switches back to asking, and says so.
  await click('hardware-auto');
  await page.locator('#engine-live .rp-auto:not(.is-on)').waitFor({timeout: 15000});
  if (!(await page.locator('#engine-live .rp-auto').innerText()).includes('逐条批准')) throw Error('Session view does not say it asks now');
  if ((await repairSession()).hardware.auto_approve !== false) throw Error('The switch did not reach the session');
  await shot('5-switched');

  // 6 · Back on the hardware page: what was fixed is gone, the on-site problem still waits.
  await click('hardware-open');
  await dlg.getByText('硬件清单', {exact: true}).waitFor();
  if (await box(ISSUES[0]).count()) throw Error('A fixed problem is still listed');
  if (!(await box(ONSITE).isDisabled())) throw Error('The on-site problem is no longer blocked after the repair');
  await shot('6-after');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), onsiteProblemBlocked: true, serverRefusesBlocked: true, markAndUndo: true,
                              explanationShown: true, choiceTickedByDefault: true, ranWithoutApproval: true, rebootStillRefused: true,
                              approvalCards: true, switchBackToAsking: true, jsErrors: errors.length}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
