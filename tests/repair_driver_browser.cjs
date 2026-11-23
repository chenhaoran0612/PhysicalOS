// Browser acceptance for issue #29 (R1-10): a device lacks its motion driver; handed to the repair assistant
// with 默认允许执行, the package comes from the library, is installed unasked, and the driver's start stops for
// a person on site. Approving without the tick is refused; with it the driver starts, ROS 2 is read again,
// the motion interfaces are there, and nothing that moves or unlocks the robot reached it.
//   node tests/repair_driver_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/repair_driver_browser_fixture.py (a local SSH stand-in that plays the install and the start,
// a library stand-in and a scripted model behind a TokenFans stand-in; no real machine; the server needs
// NEUXMIND_MAIL_DEV_ECHO=1; one repair per fixture run, so restart it before each run).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {connectTokenFans, useTokenFansModel} = require('./tokenfans_browser.cjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const MODEL = 'fixture-driver-install';
const ROBOT = '缺运控驱动的四足';
const VELOCITY = 'motion-driver:base.velocity', ODOMETRY = 'motion-driver:base.odometry';
const ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];
const MOVES = /topic\s+pub|service\s+call|send_goal|param\s+(?:set|load)|lifecycle\s+set|switch_controller|bag\s+play|teleop|\/v[12]\/(?:reset|start|runs|capabilities|conformance|safety)|neuxmind[-_]runtime|device_install\.sh/;
const WIDTHS = [1024, 768, 375, 320];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-repair-driver-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `repair-driver-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const onsiteCalls = [];
  page.on('request', r => { if (r.url().endsWith('/api/engineering/hardware/onsite')) onsiteCalls.push(r.postDataJSON()); });

  const dialog = page.locator('#dialog'), drivers = page.locator('#motion-drivers'), banner = page.locator('#md-banner');
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const formErrors = async () => (await page.locator('#dialog .form-error').allTextContents()).filter(Boolean);
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await formErrors();
    if (e.length) throw Error(e.join(';'));
  };
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) check(text.includes(t), `Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const device = async () => (await fetch(`${cfg.control}/device`)).json();
  const close = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await dialog.waitFor({state: 'hidden'}); };
  const card = name => page.locator('article.card', {hasText: name});
  const session = () => page.evaluate(() => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    return s ? JSON.parse(JSON.stringify(s)) : null;
  });
  const fits = async what => {
    const overflow = await page.evaluate(() => {
      const d = document.querySelector('#dialog');
      return {page: document.documentElement.scrollWidth - window.innerWidth, dialog: d.scrollWidth - d.clientWidth};
    });
    check(overflow.page <= 0 && overflow.dialog <= 0, `${what}: horizontal overflow ${JSON.stringify(overflow)}`);
  };

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '运控驱动安装验收', email: `install-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'ins1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '接入项目'); await fill('objective', '给一台缺运控驱动的设备装上驱动');
  await submit();
  await connectTokenFans(page, cfg);
  await useTokenFansModel(page, cfg, MODEL);
  check((await fetch(`${cfg.control}/cloud`, {method: 'POST'})).status === 204, 'the library stand-in could not sign the account in');

  // 1 · The device: connected, its profile read, two motion interfaces missing.
  // A hash change, not a second goto: WebKit reloads on goto and reports the status poll it cut off as a page error.
  await page.evaluate(() => { location.hash = '#robots'; });
  await page.locator('[data-action="engine-ssh"]').first().waitFor({timeout: 15000});
  await page.locator('[data-action="engine-ssh"]').first().click();
  await fill('name', ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.port);
  await page.locator('#dialog [name=family]').selectOption('quadruped');
  await fill('username', cfg.ssh_user); await fill('password', cfg.ssh_login);
  await submit();
  await page.locator('#dialog input[type=checkbox]').check();
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 40000});
  await close();
  await card(ROBOT).locator('[data-action="engine-profile"]').click();
  await page.locator('#device-profile .dp-verdict').waitFor({timeout: 60000});
  await banner.waitFor({timeout: 15000});
  await expectText(banner, ['缺 2 项运控接口']);
  await banner.locator('[data-action="engine-drivers"]').click();
  await drivers.waitFor();
  await expectText(drivers, ['启动驱动那一步一定要有人在现场确认']);     // what handing it over leads to, since issue #29

  // 2 · Handed over with 默认允许执行 (the form's default): the library is asked, the package sent and installed unasked.
  await drivers.locator('[data-action="engine-drivers-repair"]').click();
  await dialog.getByText('让助手修复硬件问题', {exact: true}).waitFor({timeout: 60000});
  const form = dialog.locator('form');
  await form.locator('[name=data_authorized]').check();
  await form.locator('[name=modify_authorized]').check();
  check(await form.locator('[name=auto_approve]').isChecked(), '默认允许执行 is not the default of the repair form');
  await submit();
  await page.locator('#engine-live').waitFor({timeout: 20000});
  await page.waitForFunction(() => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    return s && s.pending_approval && s.pending_approval.request && s.pending_approval.request.starts_motion_driver === true;
  }, null, {timeout: 120000});
  let robot = await device();
  check(robot.installed && !robot.running, `before the start: ${JSON.stringify({installed: robot.installed, running: robot.running})}`);
  let s = await session();
  const unasked = s.events.filter(e => e.message.startsWith('默认允许执行，未询问')).map(e => e.message);
  check(unasked.length === 2 && unasked[0].includes(cfg.package) && unasked[1].includes('apt-get install'), `ran unasked: ${unasked}`);

  // 3 · The start waits for a person: what the body may do, the command, the tick.
  await page.locator('#engine-live [data-action="engine-approval"]').first().click();
  await dialog.getByText('启动运控驱动前要有人在现场', {exact: true}).waitFor({timeout: 15000});
  await expectText(dialog, ['systemctl start vendor-motion-driver', '上电', '我在现场', '能随时按下硬件急停',
                            '这条命令没有直接执行', '要有人在现场确认', '开了“默认允许执行”也一样']);
  await shot('1-start-1440');
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await fits(`the approval at ${width}px`);
    if (width === 375) await shot('1-start-375');
  }
  await page.setViewportSize({width: 1440, height: 1000});
  const box = dialog.locator('[name=onsite]');
  await box.focus();
  check(await box.evaluate(b => document.activeElement === b), 'the on-site tick cannot take the keyboard focus');

  // 4 · Approving without the tick: refused in the form, nothing sent, the driver not started.
  await dialog.locator('[name=decision]').selectOption('accept');
  await page.locator('#dialog button[type=submit]').click();
  await page.waitForTimeout(300);
  const refused = await formErrors();
  check(refused.some(t => t.includes('我在现场')), `no refusal without the tick: ${refused}`);
  check(await dialog.locator('.rd-onsite.needs-confirm').count() === 1, 'the tick is not pointed at');
  check(onsiteCalls.length === 0, 'a confirmation was sent without the tick');
  robot = await device();
  check(!robot.running, 'the driver started without anybody on site');
  await shot('2-refused');

  // 5 · With the tick (keyboard: Space): the confirmation, then the approval; the driver starts, the rescan sees the interfaces.
  await box.focus();
  await page.keyboard.press('Space');
  check(await box.isChecked(), 'Space does not tick the on-site box');
  await submit();
  check(onsiteCalls.length === 1 && onsiteCalls[0].onsite === true && typeof onsiteCalls[0].approval_id === 'number',
        `the confirmation: ${JSON.stringify(onsiteCalls)}`);
  await page.waitForFunction(active => {
    const x = rows('agent_sessions').find(y => y.mode === 'repair');
    return x && !active.includes(x.status);
  }, ACTIVE, {timeout: 120000});
  s = await session();
  const done = await page.evaluate(id => {
    const x = rows('agent_sessions').find(y => y.id === id);
    const report = (state.hardware_repairs || []).find(r => r.id === x.repair_report_id) || null;
    const final = report && (state.hardware_scans || []).find(y => y.id === report.final_scan_id);
    return {outcomes: report && Object.fromEntries(report.results.map(r => [r.issue_id, r.outcome])), final: final && final.motion_drivers};
  }, s.id);
  check(s.status === 'completed', `the repair ended ${s.status}: ${s.error || ''}`);
  check(done.outcomes && done.outcomes[VELOCITY] === 'fixed' && done.outcomes[ODOMETRY] === 'fixed', `outcomes: ${JSON.stringify(done.outcomes)}`);
  check(done.final && done.final.reverified === true && done.final.status === 'ok', `the rescan: ${JSON.stringify(done.final)}`);
  check(s.hardware.onsite_confirmations.length === 1, 'the confirmation was not recorded');
  check(s.events.some(e => e.message.startsWith('已确认有人在现场')), 'the session record does not say someone was on site');
  check(s.events.filter(e => e.kind === 'tool_error').length === 0, `refused steps: ${s.events.filter(e => e.kind === 'tool_error').map(e => e.message)}`);
  robot = await device();
  check(robot.running && robot.latched, `after: ${JSON.stringify({running: robot.running, latched: robot.latched})}`);
  const moved = robot.commands.filter(c => MOVES.test(c));
  check(moved.length === 0, `commands that move or unlock the robot reached it: ${moved}`);
  await page.locator('#engine-live').getByText('已修复').first().waitFor({timeout: 15000});
  await shot('3-session');
  await close();

  // 6 · The card no longer says a motion driver is missing.
  await page.evaluate(() => refresh(true));
  await card(ROBOT).locator('[data-action="engine-drivers"]').waitFor({state: 'detached', timeout: 15000});
  check(!(await card(ROBOT).innerText()).includes('运控驱动缺'), 'the card still says the driver is missing');
  check(errors.length === 0, `page errors: ${errors}`);
  await browser.close();
  console.log(`repair_driver_browser (${engine.name()}): ok`);
})().catch(async err => {
  console.error(err);
  await failed();
  process.exit(1);
});
