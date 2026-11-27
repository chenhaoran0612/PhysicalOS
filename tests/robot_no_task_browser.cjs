// Issue #56 browser acceptance: a robot is added with no task, and no task is made for it.
// A new account, which has no task:
//   1  「我的机器人」: each of the three ways to add a robot opens its own form, never the form
//      「你希望机器人完成什么工作？」;
//   2  a robot is added by its service address and connected: it is on the page, and the account still has no task;
//   3  a simulated robot is added and started: no trial task appears;
//   4  what the page offers for a robot works with no task: 助手模型 in 设置, the robot's own page, its 「更多功能」
//      (配置连接, 删除); one robot is deleted;
//   5  the dashboard has no task; a task is created there and bound to the robot that is left: its card shows it.
// Fixture: tests/task_robot_browser_fixture.py (no real machine).
// Usage: node tests/robot_no_task_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const {fillBrandAndModel} = require('./robot_model_helper.cjs');
const {addRobotWay, robotAction, openRobotMenu} = require('./robots_page_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DOG = '巡检狗 56';
const TASK_FORM = 'form[data-form="project"]', TASK_TITLE = '你希望机器人完成什么工作';
const WIDTHS = [375, 1440], DESKTOP = {width: 1440, height: 1000};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-no-task-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.setDefaultTimeout(20000);
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `robot-no-task-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const noTask = async when => {
    const state = await appState();
    must(state.projects.length === 0, `A task was made ${when}: ${JSON.stringify(state.projects.map(p => p.name))}`);
    return state;
  };
  // The dialog that is open is `what` (a selector inside it) and not the form of a new task.
  const opened = async (what, name) => {
    await dialog.locator(what).first().waitFor();
    must(await dialog.locator(TASK_FORM).count() === 0 && !(await dialog.innerText()).includes(TASK_TITLE),
         `${name} opened the form of a new task`);
  };
  const close = async () => {
    await dialog.locator('[data-action="close"]').first().click();
    await dialog.waitFor({state: 'hidden'});
  };
  const card = name => main.locator('article.card', {hasText: name});
  const shots = async name => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: DESKTOP.height});
      await page.screenshot({path: file(`${name}-${width}`), animations: 'disabled'});
    }
    await page.setViewportSize(DESKTOP);
  };
  const result = {};

  await page.goto(cfg.url + '/#robots');
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '机器人不需要任务', email: `no-task-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'robot56-' + crypto.randomBytes(8).toString('hex')});
  await main.locator('.robot-entry').waitFor();
  await noTask('by signing up');

  // 1 · The three ways to add a robot each open their own form.
  await addRobotWay(page, 'engine-ssh');
  await opened('form[data-form="engine-ssh"]', '通过 SSH 调查真机');
  await close();
  await addRobotWay(page, 'sim-add');
  await opened('form[data-form="sim-add"]', '添加仿真机器人');
  await close();
  await addRobotWay(page, 'robot-add');
  await opened('form[data-form="robot"]', '连接已在运行的机器人');
  await shots('1-add-form');
  result.everyWayOpensItsOwnForm = true;

  // 2 · A robot by its service address.
  await dialog.locator('[name="name"]').fill(DOG);
  await dialog.locator('[name="family"]').selectOption('quadruped');
  await fillBrandAndModel(page);
  await dialog.locator('[name="endpoint"]').fill(cfg.device.url);
  await dialog.locator('[name="token"]').fill(cfg.device.access);
  await dialog.locator('button[type=submit]').click();
  await card(DOG).waitFor();
  if (await dialog.evaluate(d => d.open)) await close();
  let state = await noTask('by adding a robot by its address');
  const dog = state.robots.find(r => r.name === DOG);
  must(dog && dog.project_id === state.robot_home, 'The robot is not kept with the account');
  const connected = await page.evaluate(id => fetch('/api/robots/connect', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({robot_id: id})}).then(r => r.status), dog.id);
  must(connected === 200, 'The robot could not be connected with no task: ' + connected);
  await page.evaluate(() => refresh(true));
  result.addedByAddress = true;

  // 3 · A simulated robot: added and started, and no trial task.
  await addRobotWay(page, 'sim-add');
  const sim = dialog.locator('form[data-form="sim-add"]');
  await sim.waitFor();
  must(!(await sim.locator('button[type=submit]').isDisabled()), 'The dialog does not open on a model that can be added');
  await sim.locator('button[type=submit]').click();
  await page.waitForFunction(() => state.robots.some(r => r.sim && r.status === 'connected'), null, {timeout: 120000});
  if (await dialog.evaluate(d => d.open)) await close();
  state = await noTask('by adding a simulated robot');
  must(state.robots.length === 2 && state.robots.every(r => r.project_id === state.robot_home), 'The two robots are not both the account\'s');
  must(await page.evaluate(() => projectId === '' && location.hash === '#robots'), 'The page left for a task that is not there');
  must(await main.locator('article.card').count() === 2, 'The page does not show the two robots');
  await shots('2-two-robots');
  result.simulatedRobotMakesNoTask = true;

  // 4 · What the page offers for a robot, with no task.
  const settings = main.locator('.page-head details.rp-settings');
  await settings.locator('summary').click();
  await settings.locator('[data-action="engine-models"]').click();
  await opened('.dialog-body', '助手模型');
  must(!(await dialog.innerText()).includes('请先创建'), '助手模型 asks for a task');
  await close();
  await robotAction(card(DOG), 'detail-robots').locator.count() ? await robotAction(card(DOG), 'detail-robots').click()
    : await card(DOG).locator('.card-bottom .button').first().click();
  await opened('.dialog-body', 'The robot\'s own page');
  await close();
  await openRobotMenu(card(DOG));
  const entries = await card(DOG).locator('.robot-menu-panel [data-action]').evaluateAll(els => els.map(el => el.dataset.action));
  must(entries.includes('engine-robot-remove'), 'The card has no 删除: ' + entries.join());
  await card(DOG).locator('.robot-menu-panel [data-action="engine-robot-remove"]').click();
  await opened('[data-action="engine-robot-remove-confirm"]', '删除');
  await dialog.locator('[data-action="engine-robot-remove-confirm"]').click();
  await card(DOG).waitFor({state: 'detached'});
  if (await dialog.evaluate(d => d.open)) await close();
  state = await noTask('by deleting a robot');
  must(state.robots.length === 1 && state.robots[0].sim, 'Deleting did not leave the simulated robot alone');
  const left = state.robots[0];
  result.robotLookedAfterWithNoTask = true;

  // 5 · A task is made by a person, and bound to the robot when it is created.
  await page.goto(cfg.url + '/#overview');
  await main.locator('[data-action="journey-start"]').first().waitFor();
  must(await main.locator('.tw-card').count() === 0, 'The dashboard shows a task nobody made');
  await main.locator('[data-action="journey-start"]').first().click();
  await dialog.locator(TASK_FORM).waitFor();
  const options = await dialog.locator('select[name="robot_id"] option').allInnerTexts();
  must(options.length === 2 && options[0] === '之后再选' && options[1].startsWith(left.name), 'The form does not offer the robot: ' + options.join(' | '));
  await dialog.locator('[name="name"]').fill('园区巡检');
  await dialog.locator('[name="objective"]').fill('每天巡检园区一圈并报告异常');
  await dialog.locator('select[name="robot_id"]').selectOption(left.id);
  await shots('3-task-form');
  await dialog.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  await main.locator('.task-robot').waitFor();
  must((await main.locator('.task-robot').innerText()).includes(left.name), 'The page of the task does not name its robot');
  state = await appState();
  must(state.projects.length === 1 && state.projects[0].robot_id === left.id && !state.projects[0].auto_created,
       'The task is not the one that was made, bound to the robot: ' + JSON.stringify(state.projects));
  must(state.robots[0].project_id === state.robot_home, 'Binding moved the robot into the task');
  await shots('4-task');
  result.taskBoundWhenCreated = true;

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, engine: engine.name(), ...result}));
  await browser.close();
})().catch(async error => { await failed(); console.error(error); process.exit(1); });
