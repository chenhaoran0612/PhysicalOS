// Issue #22 browser acceptance (R1-06): a device without safety limits is marked "安全限值未填，不能运动" on its
// card; the form is empty until a person fills it in, refuses a value above the maximum next to its input
// (before sending, and when the device says no), and a valid save turns the card into the filled values and
// shows who filled them in and when in the robot's detail. An older device and a device that cannot be
// reached are said to be so, without a form.
// Fixture: tests/safety_limits_browser_fixture.py (device doubles on loopback, no real machine).
// Usage: node tests/safety_limits_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// Names carry markup: wherever they appear they must be shown as text.
const PERSON = '安全限值验收 <b id="markup-in-a-person">';
const REAL = '巡检狗 <i id="markup-in-a-robot">', OLD = '旧版运行时的机器人', DOWN = '关了机的机器人';
const BLOCKED = '安全限值未填，不能运动';
const EDIT = '[data-action="engine-safety-limits-edit"]', FORM = 'form[data-form="engine-safety-limits-save"]';
const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
const FIELDS = ['max_linear_speed', 'max_angular_speed', 'command_timeout_s'];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-safety-limits-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `safety-limits-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 700)}`);
  };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const control = async (what, name, extra = '') => (await fetch(`${cfg.control}/${what}?name=${name}${extra}`)).json();
  const reread = () => page.evaluate(() => refresh(true));         // the page reads a change made behind its back at once
  const card = robotId => page.locator(`article.card:has([data-action="robot-connect"][data-id="${robotId}"])`);
  const input = name => dialog.locator(`#safety-limits-${name}`);
  const problem = name => dialog.locator(`#safety-limits-${name}-problem`);
  const section = page.locator('#safety-limits-section');
  const focused = () => page.evaluate(() => document.activeElement?.id || '');
  // Submits the open form and waits for the request to be over: the page disables the button meanwhile.
  const save = async () => {
    await dialog.locator('button[type=submit]').click();
    await page.waitForFunction(() => {
      const button = document.querySelector('#dialog button[type=submit]');
      return !button || !button.disabled;
    });
  };
  // One screenshot per width, with `target` in view and nothing wider than its box. Transitions (the side bar
  // sliding away on a narrow window) are finished first; the toast of an earlier step is not in the picture.
  const shots = async (name, target) => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: DESKTOP.height});
      await target.scrollIntoViewIfNeeded();
      const overflow = await target.evaluate(el => el.scrollWidth - el.clientWidth);
      must(overflow <= 1, `${name} is ${overflow}px wider than its box at ${width}px`);
      await page.screenshot({path: file(`${name}-${width}`), animations: 'disabled'});
    }
    await page.setViewportSize(DESKTOP);
  };
  const addRobot = async (name, device) => {
    await page.goto(cfg.url + '/#robots');
    await page.locator('[data-action="robot-add"]').first().click();
    await dialog.locator('[name="name"]').fill(name);
    await dialog.locator('[name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #32: a new device needs a type from the list
    await dialog.locator('[name="endpoint"]').fill(device.url);
    await dialog.locator('[name="token"]').fill(device.access);
    await save();
    await page.locator('article.card h3', {hasText: name.split(' ')[0]}).first().waitFor();
    return (await appState()).robots.find(r => r.name === name).id;
  };
  const connect = async robotId => {
    await card(robotId).locator('[data-action="robot-connect"]').click();
    await card(robotId).locator('.tag', {hasText: '控制通道：已连接'}).waitFor();
  };
  const openForm = async (from, title) => {
    await from.locator(EDIT).click();
    await dialog.locator(FORM).waitFor();
    same(await dialog.locator('.dialog-head h2').innerText(), title, 'The title of the form');
  };
  const openDetail = async robotId => {
    await card(robotId).locator('[data-action="detail-robots"]').click();
    await section.waitFor();
    await section.locator('.inline-note', {hasText: '正在读取'}).waitFor({state: 'detached'});
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: PERSON, email: `limits-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'limits1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await dialog.locator('[name="name"]').fill('园区巡检');
  await dialog.locator('[name="objective"]').fill('接入真机，填写安全限值后再让它运动');
  await save();

  // 1 · Nothing is claimed about a robot that was never checked; after the connection check its card says it cannot move.
  const real = await addRobot(REAL, cfg.devices.real);
  must(await card(real).locator('.safety-limits-card').count() === 0, 'A robot that was never checked already says something about its limits');
  await connect(real);
  const strip = card(real).locator('.safety-limits-card-blocked');
  await strip.waitFor();
  same((await strip.locator('.status.bad').innerText()).trim(), BLOCKED, 'The badge of a device without limits');
  same((await strip.locator(EDIT).innerText()).trim(), '填写安全限值', 'The way to the form');
  same(await strip.locator(EDIT).getAttribute('aria-label'), '填写安全限值：' + REAL, 'The button names its robot');
  must(await page.locator('#markup-in-a-robot').count() === 0, 'Markup in a robot name became part of the page');
  await shots('1-card-unfilled', card(real));
  result.unfilledDeviceIsMarked = true;

  // 2 · The form: empty (the platform does not guess), labelled, with the range of each value and who will be recorded.
  await openForm(card(real), '填写安全限值');
  for (const name of FIELDS) same(await input(name).inputValue(), '', `${name} before anybody filled it in`);
  same(await focused(), 'safety-limits-max_linear_speed', 'The focus when the form opens');
  await expectText(dialog, ['允许范围：大于 0，不超过 1 m/s。上限来自本体声明的上限（平台上限 3 m/s）。',
                            '允许范围：不小于 0.05，不超过 2 s。上限来自平台上限。', '保存后会记录填写人：' + PERSON,
                            '任何能力都不能让这台设备运动']);
  must(await page.locator('#markup-in-a-person').count() === 0, 'Markup in an account name became part of the page');
  await page.keyboard.press('Tab');
  same(await focused(), 'safety-limits-max_angular_speed', 'Tab goes to the next value');
  result.formIsEmptyAndExplained = true;

  // 3 · A value above the maximum is refused next to its input, before anything is sent.
  await dialog.getByLabel('最大线速度（m/s）').fill('5');
  await dialog.getByLabel('最大角速度（rad/s）').fill('0.8');
  await dialog.getByLabel('运动指令超时（s）').fill('0.5');
  await save();
  await problem('max_linear_speed').getByText('不能超过 1 m/s（本体声明的上限）').waitFor();
  same(await input('max_linear_speed').getAttribute('aria-invalid'), 'true', 'The refused input');
  same(await input('max_angular_speed').getAttribute('aria-invalid'), null, 'An input that is fine');
  same(await focused(), 'safety-limits-max_linear_speed', 'The focus after a refusal');
  await expectText(dialog.locator('.form-error'), ['有 1 项需要修改', '没有发给设备']);
  same((await control('device', 'real')).writes, 0, 'Requests that reached the device');
  await shots('2-form-refused', dialog);
  await input('max_linear_speed').fill('0.9');
  same(await input('max_linear_speed').getAttribute('aria-invalid'), null, 'Typing clears the mark');
  same(await problem('max_linear_speed').innerText(), '', 'Typing clears the words under the input');
  result.overMaximumRefusedInline = true;

  // 4 · The device says no (its body now declares less than when the form was opened): shown in the form, on the input.
  await control('body-max', 'real', '&linear=0.6');
  await save();
  await problem('max_linear_speed').getByText('设备没有接受这个值：允许大于 0、不超过 0.6 m/s（本体声明的上限）').waitFor();
  same(await input('max_linear_speed').getAttribute('aria-invalid'), 'true', 'The input the device refused');
  await dialog.locator('.form-error .plain-error[data-error-code="limits_out_of_range"]').waitFor();
  must(await dialog.locator('button[type=submit]').isEnabled(), 'After a refusal the form cannot be sent again');
  let device = await control('device', 'real');
  same([device.writes, device.limits], [1, null], 'One request, nothing filled in');
  await shots('3-form-refused-by-device', dialog);
  await control('body-max', 'real', '&linear=1');
  // A skill is running: the refusal names no input.
  await control('busy', 'real', '&on=1');
  await input('max_linear_speed').fill('0.5');
  await save();
  await dialog.locator('.form-error .plain-error[data-error-code="runs_active"]').waitFor();
  must(await dialog.locator('[aria-invalid="true"]').count() === 0, 'An input is marked for a refusal that names none');
  await control('busy', 'real', '&on=0');
  result.deviceRefusalShownInForm = true;

  // 5 · A valid save: the card shows the values, the device recorded the signed-in person, every click was its own request.
  await save();
  const line = card(real).locator('.safety-limits-card-line');
  await line.waitFor();
  same((await line.innerText()).trim(), '限速 0.5 m/s · 0.8 rad/s · 指令超时 0.5 s', 'The card of a filled device');
  must(await card(real).locator('.safety-limits-card-blocked').count() === 0, 'The badge stays after the limits were filled in');
  must(!(await dialog.evaluate(d => d.open)), 'The form stays open after the save');
  device = await control('device', 'real');
  same(device.limits, {base: {max_linear_speed: 0.5, max_angular_speed: 0.8, command_timeout_s: 0.5}}, 'What the device holds');
  same([device.set_by, device.writes], [PERSON, 3], 'Who the device recorded, and how many requests it got');
  must(device.set_by_sent.every(who => who === PERSON), 'A request named somebody else');
  must(new Set(device.request_ids).size === 3 && device.request_ids.every(Boolean), 'Clicks shared a request id: ' + device.request_ids);
  await shots('4-card-filled', card(real));
  result.savedAndCardFlipped = true;

  // 6 · The detail: filled, the values, who and when.
  await openDetail(real);
  await expectText(section, ['安全限值', '已填', '最大线速度', '0.5 m/s', '0.8 rad/s', '运动指令超时', '填写人：' + PERSON,
                            '填写时间：20', '修改安全限值']);
  must(await section.locator('.status.good').count() === 1, 'A filled device is not marked as filled');
  must(await page.locator('#markup-in-a-person').count() === 0, 'Markup in the recorded name became part of the page');
  await shots('5-detail-filled', section);
  result.detailShowsWhoAndWhen = true;

  // 7 · Changing them, by keyboard: the form opens with the current values; Enter saves.
  await openForm(section, '修改安全限值');
  same(await Promise.all(FIELDS.map(name => input(name).inputValue())), ['0.5', '0.8', '0.5'], 'The current values in the form');
  same(await focused(), 'safety-limits-max_linear_speed', 'The focus when the form opens again');
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.type('0.4');
  await page.keyboard.press('Enter');
  await card(real).locator('.safety-limits-card-line', {hasText: '限速 0.4 m/s'}).waitFor();
  await openDetail(real);
  const history = section.locator('.safety-limits-history');
  same((await history.locator('summary').innerText()).trim(), '修改记录（2 次）', 'The history of a device changed once');
  await history.locator('summary').click();
  same(await history.locator('li').count(), 2, 'Entries of the history');
  await expectText(history.locator('li').first(), ['第 2 次', PERSON, '限速 0.4 m/s']);
  await dialog.locator('[data-action="close"]').first().click();
  result.changedByKeyboard = true;

  // 8 · A device with an older management layer: nothing on the card, said plainly in the detail, no form.
  const old = await addRobot(OLD, cfg.devices.old);
  await connect(old);
  must(await card(old).locator('.safety-limits-card').count() === 0, 'An older device says something about limits on its card');
  await openDetail(old);
  await expectText(section, ['安全限值', '版本较旧', '升级']);
  must(await section.locator(EDIT).count() === 0, 'An older device is offered the form');
  await shots('6-detail-older-device', section);
  await dialog.locator('[data-action="close"]').first().click();
  result.olderDeviceSaidPlainly = true;

  // 9 · A device that cannot be reached: said plainly where the form would be and in the detail, no form.
  const down = await addRobot(DOWN, cfg.devices.down);
  await connect(down);
  await card(down).locator('.safety-limits-card-blocked').waitFor();
  await control('off', 'down');
  await card(down).locator(EDIT).click();
  await dialog.locator('.safety-limits-unreadable').waitFor();
  await expectText(dialog, ['读不到这台设备的安全限值', '重新读取']);
  must(await dialog.locator(FORM).count() === 0, 'A device that cannot be reached is offered the form');
  await dialog.locator('[data-action="close"]').first().click();
  await openDetail(down);
  await expectText(section, ['读不到这台设备的安全限值', '未填，不能运动', '重新读取']);
  must(await section.locator(EDIT).count() === 0, 'The detail of a device that cannot be reached offers the form');
  await shots('7-detail-unreachable', section);
  result.unreachableDeviceSaidPlainly = true;

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
