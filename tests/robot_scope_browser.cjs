// Issue #42 (second part) browser acceptance: "我的机器人" and the tasks are independent.
//   1  a robot added while task A is open is listed while task B is open, and with every task's card on 任务工作台;
//      the dashboard counts it once;
//   2  from task B its installed skills are read and a skill is installed and run on it;
//   3  task A is deleted without deleting the robot first, and the robot is still there;
//   4  the robot can be deleted from the task that is left.
// Fixture: tests/robot_scope_browser_fixture.py (the real management layer with a stand-in body; no real machine).
// Usage: node tests/robot_scope_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ROBOT = '共用巡检狗', TASK_A = '园区巡检', TASK_B = '仓库搬运';
const RUN_FORM = 'form[data-form="sim-run"]';
const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-scope-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `robot-scope-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const post = (route, body) => page.evaluate(([route, body]) => fetch('/api/' + route, {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(async r => ({status: r.status, body: await r.json()})), [route, body]);
  const card = robotId => page.locator(`article.card:has([data-action="robot-connect"][data-id="${robotId}"])`);
  const taskCard = name => main.locator('.tw-card', {hasText: name});
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    await page.waitForFunction(() => {
      const button = document.querySelector('#dialog button[type=submit]');
      return !button || !button.disabled;
    });
  };
  const shots = async name => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: DESKTOP.height});
      const overflow = await main.evaluate(el => el.scrollWidth - el.clientWidth);
      must(overflow <= 1, `${name} is ${overflow}px wider than its box at ${width}px`);
      await page.screenshot({path: file(`${name}-${width}`), animations: 'disabled'});
    }
    await page.setViewportSize(DESKTOP);
  };
  const openTask = async name => {
    await page.goto(cfg.url + '/#overview');
    await taskCard(name).locator('[data-action="task-open"]').click();
    await page.waitForFunction(() => location.hash === '#task');
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '机器人归属验收', email: `scope-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'scope42-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await dialog.locator('[name="name"]').fill(TASK_A);
  await dialog.locator('[name="objective"]').fill('每天沿园区走一圈');
  await submit();

  // The robot is added while task A is open.
  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="robot-add"]').first().click();
  await dialog.locator('[name="name"]').fill(ROBOT);
  await dialog.locator('[name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #48: a new robot needs a brand and model
  await dialog.locator('[name="endpoint"]').fill(cfg.device.url);
  await dialog.locator('[name="token"]').fill(cfg.device.access);
  await submit();
  await page.locator('article.card h3', {hasText: ROBOT}).first().waitFor();
  let state = await appState();
  const robot = state.robots.find(r => r.name === ROBOT), taskA = state.projects.find(p => p.name === TASK_A);
  must(robot.project_id === state.robot_home && robot.project_id !== taskA.id, 'The robot was kept in the task that was open');
  must(!state.projects.some(p => p.id === state.robot_home), 'The robots\' home is offered as a task');
  await card(robot.id).locator('[data-action="robot-connect"]').click();
  await card(robot.id).locator('.tag', {hasText: '控制通道：已连接'}).waitFor();

  // 1 · A second task: the robot is its robot too.
  const made = await post('projects', {name: TASK_B, objective: '把零件从库房送到工位'});
  same(made.status, 200, 'creating the second task');
  const taskB = made.body;
  await openTask(TASK_B);
  must(await page.evaluate(() => projectId) === taskB.id, 'The second task is not the one that is open');
  await page.goto(cfg.url + '/#robots');
  await card(robot.id).waitFor();
  must((await main.innerText()).includes(ROBOT), 'The robot is not listed while the other task is open');
  must(!(await main.innerText()).includes('先告诉平台机器人要完成什么工作'), 'The page asks for a task before it shows the robots');
  await shots('robots-in-task-b');
  await page.goto(cfg.url + '/#overview');
  await taskCard(TASK_B).waitFor();
  for (const name of [TASK_A, TASK_B]) {
    must((await taskCard(name).innerText()).includes(ROBOT), `The card of ${name} does not name the robot`);
  }
  const figure = await main.locator('.tw-board').innerText();
  must(/机器人\s*1\b/.test(figure.replace(/\n/g, ' ')), `The dashboard does not count the shared robot once: ${figure.slice(0, 200)}`);
  await shots('dashboard-two-tasks');
  result.shared = true;

  // 2 · From task B: read its skills, install one, run it.
  await page.goto(cfg.url + '/#robots');
  await card(robot.id).locator('[data-action="detail-robots"]').click();
  await dialog.locator('#robot-skills').waitFor();
  must((await dialog.innerText()).includes('这台机器人还没有安装技能包'), 'The robot\'s skills are not read from the other task');
  same((await post('robots/limits', {robot_id: robot.id, project_id: taskB.id})).status, 200, 'reading its safety limits from the other task');
  await dialog.locator('[data-action="skillpkg-open"]').click();
  const item = dialog.locator('.gallery-item[data-example="goto_place"]');
  await item.waitFor();
  await item.locator('[data-action="skillpkg-example"]').click();
  await dialog.locator('form[data-form="skillpkg-install"]').waitFor();
  await submit();
  await dialog.locator(RUN_FORM).waitFor();
  must((await dialog.innerText()).includes('技能已安装'), 'Installing from the other task did not lead to the run form');
  for (const tick of await dialog.locator(RUN_FORM + ' input[type=checkbox]').all()) await tick.check();
  await submit();
  await page.waitForFunction(() => fetch('/api/state').then(r => r.json()).then(s => s.operations.some(o => o.kind === 'skill_run')));
  state = await appState();
  const run = state.operations.find(o => o.kind === 'skill_run');
  same([run.robot_id, run.onsite_confirmed], [robot.id, true], 'the run that was started from the other task');
  result.run = run.status;
  await page.keyboard.press('Escape');

  // 3 · Task A goes; the robot stays.
  await page.goto(cfg.url + '/#overview');
  await taskCard(TASK_A).locator('[data-action="task-remove"]').click();
  await dialog.locator('[data-action="task-remove-confirm"]').waitFor();
  must(!(await dialog.innerText()).includes('请先到“我的机器人”里删除它们'), 'Deleting the task asks to delete the robot first');
  await dialog.locator('[data-action="task-remove-confirm"]').click();
  await page.waitForFunction(name => ![...document.querySelectorAll('.tw-card h3')].some(el => el.textContent.includes(name)), TASK_A);
  state = await appState();
  same(state.projects.map(p => p.name), [TASK_B], 'the tasks that are left');
  same(state.robots.map(r => r.name), [ROBOT], 'the robots that are left');
  await page.goto(cfg.url + '/#robots');
  await card(robot.id).locator('.tag', {hasText: '控制通道：已连接'}).waitFor();
  result.taskDeleted = true;

  // 4 · The robot is deleted from the task that is left.
  const gone = await post('robots/remove', {project_id: taskB.id, robot_id: robot.id});
  same(gone.status, 200, 'deleting the robot from the other task');
  same((await appState()).robots, [], 'no robot is left');

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({passed: true, engine: engine.name(), ...result}));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
