// Issue #42 browser acceptance: a skill that was just installed on a robot can be run at once, on a robot
// that is not a simulation too; the run form has an input for every parameter the skill declares.
//   1  installing a skill ends in the run form: the parameters as inputs, the two confirmations of a real
//      robot, and (the device has no safety limits yet) what that means and the way to fill them in;
//   2  after the limits are filled in, 「运行」 next to the installed skill opens the same form without the note;
//   3  nothing is sent without both confirmations; the backend's own refusal is explained in the form;
//   4  the run starts, its state is shown and it ends; the record says the on-site confirmation was given;
//   5  a second skill with numbers as parameters: 「停止」 ends it and latches the emergency stop.
// Fixture: tests/skill_run_browser_fixture.py (the real management layer with a stand-in body that does
// not call itself simulated; no real machine).
// Usage: node tests/skill_run_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ROBOT = '巡检狗 <i id="markup-in-a-robot">';
const RUN_FORM = 'form[data-form="sim-run"]', LIMITS_FORM = 'form[data-form="engine-safety-limits-save"]';
const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-skill-run-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `skill-run-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const runs = async () => (await appState()).operations.filter(o => o.kind === 'skill_run');
  const card = robotId => page.locator(`article.card:has([data-action="robot-connect"][data-id="${robotId}"])`);
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    await page.waitForFunction(() => {
      const button = document.querySelector('#dialog button[type=submit]');
      return !button || !button.disabled;
    });
  };
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
  const openDetail = async robotId => {
    await page.goto(cfg.url + '/#robots');
    await card(robotId).locator('[data-action="detail-robots"]').click();
    await dialog.locator('#robot-skills').waitFor();
  };
  // From the robot's detail: 安装技能包 → the example → 确认安装. Ends where the page leads to.
  const install = async example => {
    await dialog.locator('[data-action="skillpkg-open"]').click();
    const item = dialog.locator(`.gallery-item[data-example="${example}"]`);
    await item.waitFor();
    await item.locator('[data-action="skillpkg-example"]').click();
    await dialog.locator('form[data-form="skillpkg-install"]').waitFor();
    await submit();
  };
  const ticks = () => dialog.locator(RUN_FORM + ' input[type=checkbox]');
  const field = name => dialog.locator(`${RUN_FORM} [data-param-field="${name}"]`);
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '技能运行验收', email: `run-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'run42a-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await dialog.locator('[name="name"]').fill('园区巡检');
  await dialog.locator('[name="objective"]').fill('在真机上安装技能后直接运行');
  await submit();

  // A robot added by its service address: not one of the console's own simulated robots.
  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="robot-add"]').first().click();
  await dialog.locator('[name="name"]').fill(ROBOT);
  await dialog.locator('[name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #48: a new robot needs a brand and model
  await dialog.locator('[name="endpoint"]').fill(cfg.device.url);
  await dialog.locator('[name="token"]').fill(cfg.device.access);
  await submit();
  await page.locator('article.card h3', {hasText: '巡检狗'}).first().waitFor();
  const robot = (await appState()).robots.find(r => r.name === ROBOT);
  must(!robot.sim, 'The robot of this test is one of the console\'s simulated robots');
  await card(robot.id).locator('[data-action="robot-connect"]').click();
  await card(robot.id).locator('.tag', {hasText: '控制通道：已连接'}).waitFor();

  // 1 · Installing ends in the run form.
  await openDetail(robot.id);
  await expectText(dialog.locator('.dialog-body'), ['这台机器人还没有安装技能包']);
  await install('goto_place');
  await dialog.locator(RUN_FORM).waitFor();
  await expectText(dialog, ['运行技能', '技能已安装', '现在可以运行', '去哪里', '最快速度', '真实的机器人',
                            '这台设备还没有填写安全限值', '急停按钮够得着']);
  must(await page.locator('#markup-in-a-robot').count() === 0, 'Markup in a robot name became part of the page');
  must(!(await dialog.locator(RUN_FORM).innerText()).includes('仿真'), 'The form of a real robot talks about simulation');
  same(await field('place').locator('select option').allInnerTexts().then(all => all.filter(t => ['会议室', '二楼'].includes(t.trim())).length), 2,
       'The places a person can choose from');
  must(await field('max_speed').locator('input').count() === 1, 'A number parameter has no input');
  same(await ticks().count(), 2, 'Confirmations on a real robot');
  same(await ticks().evaluateAll(all => all.map(x => [x.name, x.required, x.checked])),
       [['onsite_confirmed', true, false], ['hardware_authorized', true, false]], 'Both are required and start unticked');
  await shots('1-form-after-install', dialog);
  result.installLeadsToTheRunForm = true;

  // The way to the limits form is in the note; a person fills them in.
  await dialog.locator('[data-skill-run-limits] [data-action="engine-safety-limits-edit"]').click();
  await dialog.locator(LIMITS_FORM).waitFor();
  await dialog.getByLabel('最大线速度（m/s）').fill('0.5');
  await dialog.getByLabel('最大角速度（rad/s）').fill('0.8');
  await dialog.getByLabel('运动指令超时（s）').fill('0.5');
  await submit();
  await card(robot.id).locator('.safety-limits-card-line').waitFor();

  // 2 · 「运行」 next to the installed skill: the same form, nothing about limits any more.
  await openDetail(robot.id);
  const row = dialog.locator('.contract-line', {hasText: 'goto_place@'});
  await expectText(row, ['去指定地点']);
  await expectText(dialog.locator('.dialog-body'), ['在真机上运行技能前要确认现场安全']);
  must(await dialog.locator('[data-action="sim-live"]', {hasText: '技能运行状态'}).count() === 1, 'No way to the run\'s state from the detail');
  await row.locator('[data-action="sim-run"]').click();
  await dialog.locator(RUN_FORM).waitFor();
  must(await dialog.locator('[data-skill-run-limits]').count() === 0, 'The note about limits stays after they were filled in');
  result.runButtonNextToTheInstalledSkill = true;

  // 3 · Nothing is sent without both confirmations.
  await field('place').locator('select').selectOption({label: '会议室'});
  await field('max_speed').locator('input').fill('0.4');
  await dialog.locator('button[type=submit]').click();
  must(await dialog.locator(RUN_FORM).count() === 1, 'The form was sent without any confirmation');
  await ticks().nth(1).check();
  await dialog.locator('button[type=submit]').click();
  must(await dialog.locator(RUN_FORM).count() === 1, 'The form was sent without the on-site confirmation');
  same((await runs()).length, 0, 'Run requests so far');
  // A page that does not hold the box back (an older page, a script): the backend says no, and the form explains it.
  await ticks().nth(0).evaluate(box => { box.required = false; });
  await submit();
  await dialog.locator('.form-error .plain-error[data-error-code="onsite_confirmation_required"]').waitFor();
  await expectText(dialog.locator('.form-error'), ['真实的机器人']);
  same((await runs()).length, 0, 'Run requests after the refusal');
  await shots('2-form-refused', dialog);
  result.refusedWithoutOnSiteConfirmation = true;

  // 4 · With both: the run starts, its state is shown, it ends.
  await ticks().nth(0).check();
  await submit();
  const live = dialog.locator('#sim-live');
  await live.locator('.sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  await expectText(live, ['当前技能', 'goto_place', '软件停止不能替代真机的硬件急停']);
  must(await live.locator('[data-action="simlive-stop"]').isEnabled(), '「停止」 cannot be pressed while the skill runs');
  must(await live.locator('[data-action="sim-run-pick"]').isDisabled(), 'A second skill can be started while one runs');
  await shots('3-running', dialog);
  await live.locator('.sim-run[data-run-status="succeeded"]').waitFor({timeout: 60000});
  await expectText(live, ['上一次技能', '已完成']);
  const first = await runs();
  same(first.map(o => [o.skill_id, o.status, o.simulated, o.onsite_confirmed, o.parameters]),
       [['goto_place', 'accepted', false, true, {place: '会议室', max_speed: 0.4}]], 'What was recorded about the run');
  result.runsAndEnds = true;

  // 5 · A second skill, with numbers: installed from the gallery, run at once, stopped.
  await live.locator('[data-action="detail-robots"]').click();          // from the run's state back to the robot's skills
  await dialog.locator('#robot-skills').waitFor();
  await install('free_walk');
  await dialog.locator(RUN_FORM).waitFor();
  same(await field('duration_s').locator('input').inputValue(), '60', 'The value the form starts with');
  await field('duration_s').locator('input').fill('40');
  await ticks().nth(0).check();
  await ticks().nth(1).check();
  await submit();
  await live.locator('.sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  await live.locator('[data-action="simlive-stop"]').click();
  await live.locator('.estop-latched').waitFor();
  await live.locator('.sim-run[data-run-status="stopped"]').waitFor({timeout: 20000});
  await expectText(live, ['急停已锁存', '急停已锁存，先在现场确认安全并解除急停']);
  must(await live.locator('[data-action="sim-run-pick"]').isDisabled(), 'A skill can be started while the emergency stop is latched');
  const second = (await runs()).find(o => o.skill_id === 'free_walk');
  same([second.status, second.onsite_confirmed, second.parameters.duration_s], ['accepted', true, 40], 'The second run');
  await shots('4-stopped', dialog);
  result.stopEndsTheRunAndLatches = true;

  same(errors, [], 'Page errors');
  await browser.close();
  console.log(JSON.stringify({engine: engine.name(), ...result}, null, 2));
})().catch(async error => { await failed(); console.error(error); process.exit(1); });
