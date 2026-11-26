// Browser acceptance for issue #28 (R1-10): after a device's profile is read, the platform says which
// motion-control interfaces of its body type are missing (a quadruped without its chassis driver, an arm
// without its trajectory controller), offers to hand them to the repair assistant, and shows no entry
// for a device that has its drivers. After the repair the interfaces are checked by reading ROS 2 again.
//   node tests/motion_drivers_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/motion_drivers_browser_fixture.py (local SSH protocol fixtures and a scripted model
// behind a TokenFans stand-in, no real machine; the server needs NEUXMIND_MAIL_DEV_ECHO=1; one repair per
// fixture run, so restart it before each run). BRAIN_BROWSER=webkit runs it on WebKit, the engine of
// the desktop app (WKWebView); default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {connectTokenFans, useTokenFansModel} = require('./tokenfans_browser.cjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const MODEL = 'fixture-drivers';
const DOG = '缺底盘驱动的四足', ARM = '缺控制器的机械臂', COMPLETE = '驱动齐全的四足';
const VELOCITY = 'motion-driver:base.velocity', ODOMETRY = 'motion-driver:base.odometry';
const ACTIVE = ['starting', 'running', 'waiting_input', 'cancelling'];
const WIDTHS = [1024, 768, 375, 320];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-motion-drivers-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `motion-drivers-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const scans = [];                                  // every hardware scan the page asks for
  page.on('request', r => { if (r.url().endsWith('/api/engineering/hardware/scan')) scans.push(r.postDataJSON()); });

  const dialog = page.locator('#dialog'), drivers = page.locator('#motion-drivers'), banner = page.locator('#md-banner');
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) check(text.includes(t), `Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const lacksText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) check(!text.includes(t), `Unexpected "${t}" in: ${text.slice(0, 900)}`);
  };
  const control = async route => {
    const response = await fetch(`${cfg.control}/${route}`, {method: 'POST'});
    check(response.status === 204, `the fixture refused ${route}`);
  };
  const close = async () => { await page.locator('#dialog [data-action="close"]').first().click(); await dialog.waitFor({state: 'hidden'}); };
  const card = name => page.locator('article.card', {hasText: name});
  const entry = name => card(name).locator('[data-action="engine-drivers"]');
  const row = capability => drivers.locator('tr.md-row', {hasText: capability});
  const fits = async what => {
    const overflow = await page.evaluate(() => {
      const d = document.querySelector('#dialog');
      return {page: document.documentElement.scrollWidth - window.innerWidth, dialog: d.scrollWidth - d.clientWidth};
    });
    check(overflow.page <= 0 && overflow.dialog <= 0, `${what}: horizontal overflow ${JSON.stringify(overflow)}`);
  };
  const connect = async (name, family, port) => {
    await page.locator('[data-action="engine-ssh"]').first().click();
    await fill('name', name); await fill('host', '127.0.0.1'); await fill('port', port);
    await page.locator('#dialog [name=family]').selectOption(family); await require('./robot_model_helper.cjs').fillBrandAndModel(page);    // issue #32: a new device needs a type from the list
    await fill('username', cfg.ssh_user); await fill('password', cfg.ssh_login);
    await submit();
    await page.locator('#dialog input[type=checkbox]').check();          // the host fingerprint
    await submit();
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 40000});   // issue #2: the scan after connecting
    await close();
  };
  // Opens the device profile from the card; the first opening reads it. Ends with the profile shown.
  const readProfile = async name => {
    await card(name).locator('[data-action="engine-profile"]').click();
    await page.locator('#device-profile .dp-verdict').waitFor({timeout: 60000});
    await page.locator('#device-profile [data-action="engine-profile-read"]:not([disabled])').waitFor({timeout: 60000});
  };

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '运控驱动验收', email: `drivers-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'drv1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '接入项目'); await fill('objective', '接入一台缺运控驱动的设备');
  await submit();
  await connectTokenFans(page, cfg);
  await useTokenFansModel(page, cfg, MODEL);

  await page.goto(cfg.url + '/#robots');
  await connect(DOG, 'quadruped', cfg.ports.dog);
  await connect(ARM, 'manipulator', cfg.ports.arm);
  await connect(COMPLETE, 'quadruped', cfg.ports.complete);

  // 1 · Nothing is claimed before a device's profile was read.
  check(await page.locator('[data-action="engine-drivers"]').count() === 0, 'an entry before any profile was read');
  await lacksText(page.locator('#main'), ['运控驱动缺']);

  // 2 · A quadruped without its chassis driver: the profile says what is missing, and so does its card.
  await readProfile(DOG);
  await banner.waitFor({timeout: 15000});
  await expectText(banner, ['运控驱动', '四足机器人', '缺 2 项运控接口', '底盘速度', '里程计', '查看并处理']);
  await shot('1-profile-banner');
  await close();
  await card(DOG).getByText('运控驱动缺 2 项').waitFor({timeout: 15000});
  check(await entry(DOG).count() === 1, 'the card of the device without its driver has no entry');

  // 3 · A device with its drivers: the profile says they are there, the card shows no entry.
  await readProfile(COMPLETE);
  await banner.waitFor({timeout: 15000});
  await expectText(banner, ['应有的 2 项运控接口都在']);
  check(await banner.locator('[data-action="engine-drivers-repair"]').count() === 0, 'a repair is offered for a complete device');
  await close();
  check(await entry(COMPLETE).count() === 0, 'a device with its drivers got an entry');
  await lacksText(card(COMPLETE), ['运控驱动']);

  // 4 · An arm without its trajectory controller: the missing interface is an action, with its type.
  await readProfile(ARM);
  await banner.waitFor({timeout: 15000});
  await expectText(banner, ['机械臂', '缺 1 项运控接口', '机械臂关节位置']);
  await banner.locator('[data-action="engine-drivers"]').click();
  await drivers.waitFor();
  await expectText(row('arm.joint_position'), ['动作', 'control_msgs/action/FollowJointTrajectory', '缺少']);
  await expectText(row('joint.state'), ['话题', 'sensor_msgs/msg/JointState', '在', '/joint_states']);
  await expectText(row('safety.stop'), ['核对不了']);
  await shot('2-arm');
  await close();
  await card(ARM).getByText('运控驱动缺 1 项').waitFor({timeout: 15000});
  // On the hardware page, after scanning again: the problem is listed, not ticked in advance, and says the
  // assistant only diagnoses it; the hardware problems are ticked as before.
  await card(ARM).locator('[data-action="hardware-open"]').click();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 20000});
  await dialog.locator('[data-action="hardware-scan"]').first().click();
  await dialog.locator('.hw-issue-list').waitFor({timeout: 60000});
  const armProblem = dialog.locator('label.hw-issue', {hasText: '缺少运控接口：机械臂关节位置（arm.joint_position）'});
  await expectText(armProblem, ['助手可安装，启动时要人在现场', 'control_msgs/action/FollowJointTrajectory']);
  check(!(await armProblem.locator('[name=hw-issue]').isChecked()), 'the motion driver problem is ticked in advance');
  check(!(await armProblem.locator('[name=hw-issue]').isDisabled()), 'the motion driver problem cannot be ticked');
  check(await dialog.locator('[name=hw-issue][value="serial-perm:/dev/ttyUSB0"]').isChecked(), 'the hardware problems are no longer ticked');
  await shot('2-arm-hardware');
  await close();

  // 5 · The check of the quadruped: every interface of its type, what is expected, what the device has.
  await entry(DOG).click();
  await drivers.waitFor();
  await expectText(drivers, ['不发布运动指令', 'ROS 2 清单读取于', '按消息类型认']);
  await expectText(row('base.velocity'), ['底盘速度', '话题', '/cmd_vel', 'geometry_msgs/msg/Twist', '缺少']);
  await expectText(row('base.odometry'), ['里程计', '/odom', 'nav_msgs/msg/Odometry', '缺少']);
  await expectText(row('legged.posture'), ['足式姿态', '核对不了']);
  await expectText(row('safety.stop'), ['停止', '核对不了']);
  check(!(await row('legged.posture').innerText()).includes('缺少'), 'what cannot be checked is shown as missing');
  await shot('3-dog-1440');
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await fits(`the check at ${width}px`);
    if (width === 375) await shot('3-dog-375');
  }
  await page.setViewportSize({width: 1440, height: 1000});
  // Keyboard: the hand-over can be reached and has a visible name.
  const handOver = drivers.locator('[data-action="engine-drivers-repair"]');
  await handOver.focus();
  check(await handOver.evaluate(b => document.activeElement === b && !b.disabled), 'the hand-over cannot take the keyboard focus');

  // 6 · Handing it over: a scan that lists the problems, then the repair form with exactly these two.
  const before = scans.length;
  await page.keyboard.press('Enter');
  await dialog.getByText('让助手修复硬件问题', {exact: true}).waitFor({timeout: 60000});
  check(scans.length === before + 1, `one scan to list the problems, saw ${scans.length - before}`);
  const form = dialog.locator('form');
  const listed = await form.locator('[name=issue_ids]').evaluateAll(boxes => boxes.map(b => b.value).sort());
  check(JSON.stringify(listed) === JSON.stringify([VELOCITY, ODOMETRY].sort()), `the form lists: ${listed.join(', ')}`);
  await expectText(form, ['缺少运控接口：底盘速度（base.velocity）', '缺少运控接口：里程计（base.odometry）']);
  check((await form.locator('[name=model_id] option:checked').innerText()).includes(MODEL), 'the TokenFans default model is not chosen');
  await form.locator('[name=data_authorized]').check();
  await form.locator('[name=modify_authorized]').check();
  await shot('4-repair-form');
  await submit();
  await page.locator('#engine-live').waitFor({timeout: 20000});

  // 7 · The assistant, under the rules of this round: its motion command is refused, it rescans (ROS 2 is
  // read again) and reports that a person has to deal with it.
  await page.waitForFunction(active => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    return s && !active.includes(s.status);
  }, ACTIVE, {timeout: 120000});
  const done = await page.evaluate(() => {
    const s = rows('agent_sessions').find(x => x.mode === 'repair');
    const report = (state.hardware_repairs || []).find(r => r.id === s.repair_report_id) || null;
    const final = report && (state.hardware_scans || []).find(x => x.id === report.final_scan_id);
    return {status: s.status, error: s.error, issues: s.hardware.issue_ids, fixes: s.hardware.fixes,
            outcomes: report && report.results.map(r => r.outcome), final: final && final.motion_drivers,
            refused: s.events.filter(e => e.kind === 'tool_error').map(e => e.message)};
  });
  check(done.status === 'completed', `the repair ended ${done.status}: ${done.error || ''}`);
  check(JSON.stringify(done.issues.sort()) === JSON.stringify([VELOCITY, ODOMETRY].sort()), 'the session was not given the two problems');
  check(done.outcomes && done.outcomes.every(o => o === 'needs_user') && done.outcomes.length === 2, `outcomes: ${done.outcomes}`);
  check(done.fixes === 0, 'the assistant changed the device');
  check(done.refused.some(m => m.includes('不能发布运动或控制指令')), `the motion command was not refused: ${done.refused}`);
  check(done.final && done.final.reverified === true && done.final.status === 'missing', `the rescan did not read ROS 2 again: ${JSON.stringify(done.final)}`);
  await page.locator('#engine-live').getByText('需现场处理').first().waitFor({timeout: 15000});
  await shot('5-session');
  await close();

  // 8 · The problems now wait for a person: the check says so and does not offer the assistant again.
  await page.evaluate(() => refresh(true));
  await entry(DOG).click();
  await drivers.waitFor();
  await expectText(drivers, ['等待现场处理', '助手处理过，结论是需要人处理']);
  check(await drivers.locator('[data-action="engine-drivers-repair"]').isDisabled(), 'the assistant is offered again for problems that wait for a person');
  check(await drivers.locator('[data-action="hardware-open"]').count() === 1, 'no way to the hardware page with the notes');
  await shot('6-waiting');

  // 9 · Somebody installs and starts the driver on site: "重新核对" reads again, and the entry is gone.
  await control('drivers/dog/installed');
  await drivers.locator('[data-action="engine-drivers-recheck"]').click();
  await drivers.getByText('应有的 2 项运控接口都在').waitFor({timeout: 60000});
  check(await drivers.locator('[data-action="engine-drivers-repair"]').count() === 0, 'a repair is offered although nothing is missing');
  await expectText(row('base.velocity'), ['在', '/cmd_vel']);
  await shot('7-complete');
  await close();
  await entry(DOG).waitFor({state: 'detached', timeout: 15000});
  await lacksText(card(DOG), ['运控驱动']);
  // The hardware page still shows the scan of before; scanning again there brings it in line.
  await card(DOG).locator('[data-action="hardware-open"]').click();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 20000});
  await dialog.locator('[data-action="hardware-scan"]').first().click();
  await dialog.locator('.hw-issue-list').waitFor({timeout: 60000});
  await page.waitForFunction(() => !document.querySelector('#dialog .hw-scanning'), null, {timeout: 60000});
  await lacksText(dialog, ['缺少运控接口']);
  await close();

  // 10 · A device whose type is not in the list: nothing is guessed, the profile asks to change the type.
  await control(`family/${encodeURIComponent(COMPLETE)}/mobile`);
  await control('drivers/complete/missing');
  await page.evaluate(() => refresh(true));
  await card(COMPLETE).locator('[data-action="engine-profile"]').click();
  await page.locator('#device-profile .dp-verdict').waitFor({timeout: 60000});
  await page.locator('#device-profile [data-action="engine-profile-read"]').click();
  await page.locator('#device-profile [data-action="engine-profile-read"]:not([disabled])').waitFor({timeout: 60000});
  await banner.waitFor({timeout: 15000});
  await expectText(banner, ['不在本体类型清单里', '改成清单里的类型', '修改本体类型']);
  await shot('8-unlisted');
  await close();
  check(await entry(COMPLETE).count() === 0, 'a device of a type outside the list got an entry');
  // Changing the type to one of the list: the kept profile is judged at once.
  await card(COMPLETE).locator('[data-action="engine-body-type"]').first().click();
  await page.locator('#dialog [name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #48: a new robot needs a brand and model
  await submit();
  await card(COMPLETE).getByText('运控驱动缺 2 项').waitFor({timeout: 15000});

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), quadrupedMissing: 2, armMissing: 1, completeHasNoEntry: true,
                              handedToAssistant: true, rescanReadRos2Again: true, unlistedTypeNotGuessed: true}));
  await browser.close();
})().catch(async err => { await failed(); console.error(err); process.exit(1); });
