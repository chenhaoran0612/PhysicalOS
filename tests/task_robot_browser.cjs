// Issue #42 (third part) browser acceptance: when a task is created, a person chooses the robot it is done with.
//   1  the first task of an account: the form asks for the robot, there is none yet, the task is made without one
//      and its page says so;
//   2  with two robots in the account, a new task is created with one of them: its card and its page show that
//      robot only, and its skills are read from it;
//   3  a second task is created with the same robot; the dashboard still counts the robots once;
//   4  the robot of a task is changed on the page of the task, and set back to 之后再选;
//   5  the chosen robot is deleted: the page of the task says so and offers to choose another.
// Fixture: tests/task_robot_browser_fixture.py (no real machine).
// Usage: node tests/task_robot_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DOG = '巡检狗 <i id="markup-in-a-robot">', ARM = '上料臂';
const FORM = 'form[data-form="project"]', CHANGE = 'form[data-form="engine-task-robot"]';
const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-task-robot-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `task-robot-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const post = (route, body) => page.evaluate(([route, body]) => fetch('/api/' + route, {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(async r => ({status: r.status, body: await r.json()})), [route, body]);
  const taskCard = name => main.locator('.tw-card', {hasText: name});
  const line = main.locator('.task-robot');
  const options = () => dialog.locator('select[name="robot_id"] option').allInnerTexts();
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    await dialog.waitFor({state: 'hidden'});
  };
  const shots = async (name, target = main) => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: DESKTOP.height});
      const overflow = await target.evaluate(el => el.scrollWidth - el.clientWidth);
      must(overflow <= 1, `${name} is ${overflow}px wider than its box at ${width}px`);
      await page.screenshot({path: file(`${name}-${width}`), animations: 'disabled'});
    }
    await page.setViewportSize(DESKTOP);
  };
  // 新建任务 from the dashboard: fills the form; `pick` chooses the robot by the start of its label.
  const newTask = async (name, objective, pick) => {
    await page.goto(cfg.url + '/#overview');
    await main.locator('[data-action="journey-start"]').first().click();
    await dialog.locator(FORM).waitFor();
    await dialog.locator('[name="name"]').fill(name);
    await dialog.locator('[name="objective"]').fill(objective);
    return pick;
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '任务选机器人验收', email: `task-robot-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'robot42-' + crypto.randomBytes(8).toString('hex')});

  // 1 · The first task: no robot yet.
  await page.locator('[data-action="journey-start"]').first().click();
  await dialog.locator(FORM).waitFor();
  must((await dialog.innerText()).includes('用哪台机器人来做'), 'The form of a new task does not ask for the robot');
  same(await options(), ['之后再选'], 'the choices without a robot');
  must((await dialog.innerText()).includes('还没有机器人'), 'The form does not say that there is no robot yet');
  await dialog.locator('[name="name"]').fill('先建的任务');
  await dialog.locator('[name="objective"]').fill('先描述目标，之后再选机器人');
  await submit();
  await line.waitFor();
  must((await line.innerText()).includes('还没有选择'), 'The page of a task without a robot does not say so');
  result.without = true;

  // Two robots of the account: one by its service address (connected), one that is only registered.
  const added = await post('robots', {name: DOG, family: 'quadruped', endpoint: cfg.device.url, token: cfg.device.access});
  same(added.status, 200, 'adding the first robot');
  same((await post('robots/connect', {robot_id: added.body.id})).status, 200, 'connecting it');
  const arm = await post('robots', {name: ARM, family: 'manipulator'});
  same(arm.status, 200, 'adding the second robot');
  const dog = added.body.id;
  await page.evaluate(() => refresh(true));      // the robots were added behind the page

  // 2 · A task created with a robot.
  await newTask('园区巡检', '每天沿园区走一圈');
  const offered = await options();
  same([offered.length, offered[0]], [3, '之后再选'], 'the choices with two robots');
  must(offered[1].startsWith('巡检狗') && offered[1].includes('已连接') && offered[2].startsWith(ARM), `The connected robot is not offered first: ${offered}`);
  must(await dialog.locator('#markup-in-a-robot').count() === 0, 'A robot\'s name is drawn as markup in the form');
  await page.setViewportSize({width: 375, height: DESKTOP.height});
  must(await dialog.evaluate(el => el.scrollWidth - el.clientWidth) <= 1, 'The form of a new task scrolls sideways at 375px');
  await page.screenshot({path: file('1-new-task-form-375'), animations: 'disabled'});
  await page.setViewportSize(DESKTOP);
  await page.screenshot({path: file('1-new-task-form-1440'), animations: 'disabled'});
  await dialog.locator('select[name="robot_id"]').selectOption(dog);
  await submit();
  await line.waitFor();
  let state = await appState();
  const patrol = state.projects.find(p => p.name === '园区巡检');
  same(patrol.robot_id, dog, 'the robot kept with the new task');
  same(await line.getAttribute('data-task-robot'), dog, 'the robot on the page of the task');
  must((await line.innerText()).includes('巡检狗') && (await line.innerText()).includes('已连接'), 'The page of the task does not name its robot');
  must(await main.locator('#markup-in-a-robot').count() === 0, 'A robot\'s name is drawn as markup on the page of the task');
  const skills = main.locator('#task-skills');
  await skills.getByText('巡检狗').first().waitFor();
  must(!(await skills.innerText()).includes(ARM), 'The skills of the task list a robot the task was not created with');
  await shots('2-task-page');
  await page.goto(cfg.url + '/#overview');
  const holds = await taskCard('园区巡检').locator('.tw-holds').innerText();
  must(holds.includes('1 台') && holds.includes('巡检狗') && !holds.includes(ARM), `The card does not show the task's robot only: ${holds}`);
  const first = await taskCard('先建的任务').locator('.tw-holds').innerText();
  must(first.includes('2 台'), `A task without a choice does not show the robots of the account: ${first}`);
  result.created = true;

  // 3 · Another task with the same robot.
  await newTask('夜间巡检', '夜里再走一圈');
  await dialog.locator('select[name="robot_id"]').selectOption(dog);
  await submit();
  await line.waitFor();
  state = await appState();
  same(state.projects.filter(p => p.robot_id === dog).map(p => p.name).sort(), ['园区巡检', '夜间巡检'].sort(), 'the tasks that are done with the robot');
  await page.goto(cfg.url + '/#overview');
  await taskCard('夜间巡检').waitFor();
  const figure = (await main.locator('.tw-board').innerText()).replace(/\n/g, ' ');
  must(/机器人\s*2\b/.test(figure), `The dashboard does not count the two robots once: ${figure.slice(0, 200)}`);
  await shots('3-dashboard');
  result.shared = true;

  // 4 · Change the robot on the page of the task, then leave it open again.
  await taskCard('夜间巡检').locator('[data-action="task-open"]').click();
  await line.waitFor();
  await line.locator('[data-action="trobot-change"]').click();
  await dialog.locator(CHANGE).waitFor();
  same(await dialog.locator('select[name="robot_id"]').inputValue(), dog, 'the robot selected when the choice is changed');
  await dialog.locator('select[name="robot_id"]').selectOption(arm.body.id);
  await submit();
  await page.waitForFunction(id => document.querySelector('.task-robot')?.dataset.taskRobot === id, arm.body.id);
  must((await line.innerText()).includes(ARM) && (await line.innerText()).includes('未连接'), 'The page does not show the robot that was chosen instead');
  same((await appState()).projects.find(p => p.name === '夜间巡检').robot_id, arm.body.id, 'the robot saved after the change');
  await line.locator('[data-action="trobot-change"]').click();
  await dialog.locator(CHANGE).waitFor();
  await dialog.locator('select[name="robot_id"]').selectOption('');
  await submit();
  await page.waitForFunction(() => document.querySelector('.task-robot')?.dataset.taskRobot === '');
  must((await line.innerText()).includes('还没有选择'), 'Leaving the choice open is not shown');
  same((await appState()).projects.find(p => p.name === '夜间巡检').robot_id ?? null, null, 'the choice after it was cleared');
  result.changed = true;

  // 5 · The chosen robot is deleted.
  same((await post('robots/remove', {robot_id: dog})).status, 200, 'deleting the robot');
  await page.goto(cfg.url + '/#overview');
  await taskCard('园区巡检').locator('[data-action="task-open"]').click();
  await line.waitFor();
  await page.waitForFunction(() => document.querySelector('.task-robot')?.innerText.includes('已经删除'));
  must(await line.locator('[data-action="trobot-change"]').innerText() === '选择机器人', 'A task whose robot is gone does not offer to choose another');
  await line.locator('[data-action="trobot-change"]').click();
  await dialog.locator(CHANGE).waitFor();
  same(await dialog.locator('select[name="robot_id"]').inputValue(), '', 'nothing is selected when the robot is gone');
  await dialog.locator('select[name="robot_id"]').selectOption(arm.body.id);
  await submit();
  await page.waitForFunction(id => document.querySelector('.task-robot')?.dataset.taskRobot === id, arm.body.id);
  result.replaced = true;

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({passed: true, engine: engine.name(), ...result}));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
