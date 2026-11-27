// Issue #68 browser acceptance: 机器人应用 shows the applications and nothing else; an application is
// created for a task and a brand and model, with that model's skills ready to choose.
//   0  with no application the page is empty: one button, no notice, no filter, no skills button;
//   1  without a task, 「创建应用」 asks for a task first;
//   2  the form: the task is a required choice; choosing the brand and model offers its skills
//      without anything having been installed or added, and another brand and model offers others;
//   3  saving creates the application, binds it to the task and opens its checklist; the card is on
//      the page and the task has it;
//   4  a task that has a brand and model decides it: the other choices cannot be picked;
//   5  「去创建应用」 on a task opens the form with that task chosen;
//   6  nothing overflows from 320 to 1920.
// Fixture: tests/app_create_browser_fixture.py (the real management layer with two simulated bodies; no real machine).
// Usage: node tests/app_create_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [320, 375, 768, 1024, 1440, 1920], DESKTOP = {width: 1440, height: 1000};
const FORM = 'form[data-form="application"]';
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-app-create-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `app-create-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main'), form = dialog.locator(FORM);
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const has = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const lacks = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (text.includes(t)) throw Error(`"${t}" should not be in: ${text.slice(0, 900)}`);
  };
  const post = (route, body) => page.evaluate(async ([r, b]) => {
    const response = await fetch('/api/' + r, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
    const result = await response.json();
    if (!response.ok) throw Error(r + ': ' + result.error);
    return result;
  }, [route, body]);
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  // No reload: on WebKit a reload cuts off the page's own poll, which is then reported as a page error.
  const open = async hash => { await page.goto(cfg.url + '/#' + hash); await page.evaluate(() => refresh(true)); await page.locator('#main .page-head').first().waitFor(); };
  // The skills a step offers, once the form has made the brand and model's skills ready.
  const offered = async () => {
    await form.locator('.ac-status').filter({hasText: /个技能可以直接选用|还没有可用的技能|没有连接/}).waitFor();
    return form.locator('.step-editor select[name="skill_id"]').first().locator('option:not([disabled])').allInnerTexts();
  };
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '应用创建验收', email: `app-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'app68a-' + crypto.randomBytes(8).toString('hex')});

  // ---------------------------------------------------------------- 0. the empty page
  await open('skills');
  await has(main, ['机器人应用', '创建应用', '还没有应用']);
  await lacks(main, ['先把技能加进来', '使用机器人上已安装的技能', '查看技能', '正在为任务', '全部品牌型号']);
  same(await main.locator('button').count(), 1, 'the empty page has one button');
  await page.screenshot({path: file('0-empty')});

  // ---------------------------------------------------------------- 1. a task first
  await main.locator('[data-action="application-add"]').click();
  await has(dialog, ['先建一个任务']);
  must(!(await form.count()), 'the form does not open without a task');
  await dialog.locator('[data-action="close"]').first().click();

  const home = (await appState()).robot_home;
  const free = await post('projects', {name: '空任务', objective: '还没有定品牌型号'});
  const other = await post('projects', {name: '另一个任务', objective: '第二个任务'});
  const robots = {};
  for (const [key, name, family] of [['dog', '巡检狗', 'quadruped'], ['arm', '协作臂', 'manipulator']]) {
    const robot = await post('robots', {project_id: home, name, family, vendor: 'Fixture', model: key === 'dog' ? 'Dog-1' : 'Arm-1',
                                         endpoint: cfg.devices[key], token: cfg.access});
    robots[key] = await post('robots/connect', {robot_id: robot.id, project_id: home});
    must(robots[key].status === 'connected', `${name} is connected`);
  }
  same((await appState()).skills.length, 0, 'no skill was installed or added beforehand');

  // ---------------------------------------------------------------- 2. the form
  await open('skills');
  await main.locator('[data-action="application-add"]').click();
  await form.waitFor();
  await has(dialog, ['创建机器人应用', '任务', '品牌型号']);
  same(await form.locator('[name="task_id"]').inputValue(), '', 'with several tasks none is chosen');
  must(await form.locator('[name="task_id"]').evaluate(el => el.required && !el.checkValidity()), 'the task is required: the form cannot be sent without one');
  const targets = await form.locator('[name="robot_id"] option').allInnerTexts();
  same(targets.length, 2, 'one choice per brand and model');

  await form.locator('[name="robot_id"]').selectOption(robots.dog.id);
  const dogSkills = await offered();
  must(dogSkills.length > 0, 'the brand and model of the quadruped has skills without installing: ' + JSON.stringify(dogSkills));
  await has(form.locator('.ac-status'), ['个技能可以直接选用，不用先安装']);
  await form.locator('[name="robot_id"]').selectOption(robots.arm.id);
  await form.locator('.ac-status').filter({hasText: '正在准备'}).waitFor({state: 'detached'}).catch(() => {});
  await page.waitForFunction(id => !document.querySelector('.ac-status')?.textContent.includes('正在准备') &&
    document.querySelector('form[data-form="application"] [name="robot_id"]').value === id, robots.arm.id);
  const armSkills = await offered();
  must(armSkills.length > 0, 'the arm has skills too: ' + JSON.stringify(armSkills));
  must(JSON.stringify(armSkills) !== JSON.stringify(dogSkills), 'another brand and model offers other skills');
  result.skills = {dog: dogSkills, arm: armSkills};
  await page.screenshot({path: file('2-form')});

  // ---------------------------------------------------------------- 3. saving binds it to the task
  await form.locator('[name="robot_id"]').selectOption(robots.dog.id);
  await page.waitForFunction(() => !document.querySelector('.ac-status')?.textContent.includes('正在准备'));
  same(await offered(), dogSkills, 'back to the first brand and model, its skills again');
  await form.locator('[name="name"]').fill('园区巡逻');
  await form.locator('[type="submit"]').click();
  must(await form.locator('[name="task_id"]').evaluate(el => !el.checkValidity()) && await form.isVisible(), 'without a task the form stays open');
  await form.locator('[name="task_id"]').selectOption(free.id);
  await has(form.locator('.ac-task-note'), ['还没有确定品牌型号']);
  await form.locator('[type="submit"]').click();
  await dialog.locator('.delivery-detail').waitFor();
  let state = await appState();
  const made = state.applications.find(a => a.name === '园区巡逻');
  must(made, 'the application was created');
  same(state.task_applications.map(b => [b.project_id, b.application_id]), [[free.id, made.id]], 'and bound to the chosen task');
  same([made.product.vendor, made.steps.length], [robots.dog.product.vendor, 1], 'it is the brand and model\'s, with the step that was added');
  await dialog.locator('[data-action="close"]').first().click();
  await open('skills');
  same(await main.locator('article.ag-card').count(), 1, 'the application is a card on the page');
  await has(main.locator('article.ag-card'), ['园区巡逻', '已绑定任务：空任务']);
  await lacks(main, ['全部品牌型号', '正在为任务']);
  must(!(await main.locator('[data-action="appg-skills"]').count()), 'no skills button');
  await page.screenshot({path: file('3-card')});

  // ---------------------------------------------------------------- 4. the task decides the brand and model
  await main.locator('[data-action="application-add"]').click();
  await form.waitFor();
  await form.locator('[name="task_id"]').selectOption(free.id);
  await has(form.locator('.ac-task-note'), ['品牌型号跟着任务定']);
  same(await form.locator('[name="robot_id"]').inputValue(), robots.dog.id, 'the task\'s brand and model is chosen');
  same(await form.locator('[name="robot_id"] option:not([disabled])').count(), 1, 'and the other one cannot be picked');
  await form.locator('[name="task_id"]').selectOption(other.id);
  same(await form.locator('[name="robot_id"] option:not([disabled])').count(), 2, 'a task without one leaves the choice open');
  await dialog.locator('[data-action="close"]').first().click();

  // ---------------------------------------------------------------- 5. from a task
  await page.evaluate(id => { sessionStorage.setItem('brain-project', id); }, other.id);
  await page.goto(cfg.url + '/#task');
  await main.locator('.ta-head h1', {hasText: '另一个任务'}).waitFor();     // once the tasks have arrived
  await main.locator('[data-action="taskapp-create"]').first().click();
  await form.waitFor();
  same(await form.locator('[name="task_id"]').inputValue(), other.id, 'the form opens with the task a person came from');
  must(page.url().endsWith('#skills'), 'on 机器人应用');
  await form.locator('[name="robot_id"]').selectOption(robots.arm.id);
  await offered();
  await form.locator('[name="name"]').fill('上料');
  await form.locator('[type="submit"]').click();
  await dialog.locator('.delivery-detail').waitFor();
  state = await appState();
  const second = state.applications.find(a => a.name === '上料');
  must(state.task_applications.some(b => b.project_id === other.id && b.application_id === second.id), 'created from a task, bound to that task');
  await dialog.locator('[data-action="close"]').first().click();
  await lacks(main, ['正在为任务']);

  // ---------------------------------------------------------------- 6. widths
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(350);          // the sidebar slides away on a narrow page
    must(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), `no sideways scroll at ${width}`);
    if ([320, 768, 1440].includes(width)) await page.screenshot({path: file('6-' + width)});
  }
  must(!errors.length, 'page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, engine: engine.name(), ...result}));
  await browser.close();
})().catch(async error => { await failed(); console.error(error); process.exit(1); });
