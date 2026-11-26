// Issue #50 browser acceptance: 任务工作台 binds robot applications; it does not make them.
//   0  with no task at all, 「机器人应用」 asks for none: an application is made there from its form;
//   1  the page of a new task has no steps to develop with: it says no brand and model is fixed yet,
//      that nothing is bound, and sends the person to 「机器人应用」;
//   2  「去创建应用」 opens 「机器人应用」, which says whom the application is being made for; an application
//      made there is the account's and bound to nothing; one click binds it and goes back to the task;
//   3  the task shows the application: brand and model, steps, robots, how far it is; a check moves it on;
//   4  the dashboard card and board say what the task has bound;
//   5  a second task binds the same application from its own dialog (many to many), and 「机器人应用」
//      names both tasks on the card;
//   6  an application of another brand and model cannot be bound: the dialog says why and the backend
//      refuses (code task_other_model);
//   7  unbinding asks first, removes the relation only, and frees the task for another brand and model;
//   8  deleting a task says the applications stay, and they do;
//   9  reloading on the task keeps the page; nothing overflows from 320 to 1920.
// Fixture: tests/robot_model_browser_fixture.py (three HTTP contract devices; no real machine).
// Usage: node tests/task_apps_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [320, 375, 768, 1024, 1440, 1920], DESKTOP = {width: 1440, height: 1000};
const BETA = 'Beta <b> B2', ACME = 'Acme Robotics X1';      // a brand with markup in it must be shown as text
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-model-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `task-apps-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main');
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
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth &&
    document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth);
  const shot = async name => {
    const size = page.viewportSize(), high = await page.evaluate(() => document.querySelector('#content-scroll').scrollHeight + 80);
    await page.setViewportSize({width: size.width, height: Math.max(size.height, Math.min(high, 6000))});
    await page.screenshot({path: file(name), fullPage: true});
    await page.setViewportSize(size);
  };
  const newTask = async (name, objective) => {
    await main.locator('.tw-head [data-action="journey-start"]').click();
    await dialog.locator('[name="name"]').fill(name);
    await dialog.locator('[name="objective"]').fill(objective);
    await dialog.locator('button[type=submit]').click();
    await main.locator('.ta-head').waitFor();
  };
  const appRow = name => main.locator('.ta-app', {hasText: name});
  const appCard = name => main.locator('article.card', {hasText: name});

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '任务应用验收', email: 'task-apps@example.test', password: crypto.randomBytes(9).toString('hex') + 'A1'});
  await main.locator('.tw-board').waitFor();

  // 0 ---- no task yet: 机器人应用 needs none, and its form makes an application of the account
  await page.goto(cfg.url + '/#skills');
  await main.locator('.page-head [data-action="application-add"]').waitFor();
  await lacks(main, ['先告诉平台机器人要完成什么工作']);
  await page.evaluate(async ({cfg}) => {
    const arm = await api('robots', {project_id: projectId, name: '协作臂', family: 'manipulator', endpoint: cfg.devices.reporting, token: cfg.access});
    await api('robots/connect', {robot_id: arm.id});
    await api('skills', {project_id: projectId, robot_id: arm.id, name: '伸到货架', version: '1', requires: ['navigation.goal'], entrypoint: 'navigate',
                         success_condition: '到达', stop_condition: '超时', model_ids: []});
    await refresh(true);
  }, {cfg});
  await main.locator('.page-head [data-action="application-add"]').click();
  const made = dialog.locator('form[data-form=application]');
  await made.waitFor();
  await made.locator('[name=name]').fill('货架取件');
  await made.locator('.step-editor [name=skill_id]').first().waitFor();
  await made.locator('button[type=submit]').click();
  await dialog.getByText('应用交付清单').waitFor();
  await dialog.locator('[data-action="close"]').first().click();
  let snapshot = await appState();
  same([snapshot.projects.length, snapshot.applications.map(a => [a.name, a.project_id === snapshot.robot_home])], [0, [['货架取件', true]]],
       'an application made from the form with no task');
  await page.goto(cfg.url + '/#overview');
  await main.locator('.tw-board').waitFor();

  // 1 ---- a new task: nothing to develop with, nothing bound, no brand and model yet
  await newTask('仓库巡检', '每天沿仓库通道巡检一圈，发现异常就报告。');
  must(await main.locator('.journey-next, .journey-step, .journey-options').count() === 0, 'the page of a task still has the steps to develop with');
  await has(main.locator('.ta-model'), ['品牌型号', '还没有确定', '不同型号之间不通用']);
  await has(main.locator('.ta-empty'), ['这个任务还没有绑定应用', '工作台不开发应用', '从账号已有的应用里选一个绑定']);
  same(await main.locator('.ta-head .button').allInnerTexts().then(t => t.map(x => x.trim())), ['绑定应用', '去创建应用'], 'the buttons in the head of a task');
  must(await main.locator('#task-skills').count() === 0, 'the skills section is shown for a task without an application');
  await main.locator('.ta-head [data-action="taskapp-pick"]').click();
  await has(dialog, ['绑定第一个应用后，这个任务就固定为它的品牌型号', ACME, '货架取件', '去创建应用']);
  await dialog.locator('.ta-pick-foot [data-action="close"]').click();
  await shot('1-new-task');

  // 2 ---- 去创建应用: 机器人应用 says whom it is for; what is made there is the account's
  await main.locator('.ta-head [data-action="taskapp-create"]').click();
  await main.locator('.ta-banner').waitFor();
  must(page.url().endsWith('#skills'), `「去创建应用」 did not open 机器人应用: ${page.url()}`);
  await has(main.locator('.ta-banner'), ['正在为任务「仓库巡检」准备应用', '绑定到这个任务']);
  must(await main.locator('.page-head [data-action="application-add"]').count() === 1, '机器人应用 has no way to create an application');
  const ids = await page.evaluate(async ({cfg}) => {
    const robot = (name, family, device, more = {}) => api('robots', {project_id: projectId, name, family, endpoint: cfg.devices[device], token: cfg.access, ...more});
    const dog = await robot('巡检狗一号', 'quadruped', 'silent-1', {vendor: 'Beta <b>', model: 'B2'});
    const twin = await robot('巡检狗二号', 'quadruped', 'silent-2', {vendor: 'beta <B>', model: 'b2'});
    await api('robots/connect', {robot_id: dog.id});
    const arm = state.robots.find(r => r.name === '协作臂');
    const skill = {project_id: projectId, version: '1', requires: ['navigation.goal'], entrypoint: 'navigate',
                   success_condition: '到达', stop_condition: '超时', model_ids: []};
    const walk = await api('skills', {...skill, name: '前往巡检点', robot_id: dog.id});
    const made = (name, robotId, skillId) => api('applications', {project_id: projectId, name, version: '1', robot_id: robotId,
                                                                  steps: [{skill_id: skillId, parameters: {}, timeout_s: 60}]});
    const round = await made('通道巡检', dog.id, walk.id);
    const pick = state.applications.find(a => a.name === '货架取件');
    await refresh(true);
    return {dog: dog.id, twin: twin.id, arm: arm.id, round: round.id, pick: pick.id, task: projectId, home: state.robot_home};
  }, {cfg});
  snapshot = await appState();
  same(snapshot.applications.map(a => a.project_id), [ids.home, ids.home], 'where applications made while a task is open are kept');
  same(snapshot.skills.map(s => s.project_id), [ids.home, ids.home], 'where their skills are kept');
  same(snapshot.task_applications, [], 'bindings after making two applications');
  await has(appCard('通道巡检'), ['还没有绑定到任务', '绑定到这个任务']);
  await shot('2-apps-page-for-a-task');
  await appCard('通道巡检').locator('[data-action="taskapp-bind-to"]').click();
  await appRow('通道巡检').waitFor();
  must(page.url().endsWith('#task'), `binding from 机器人应用 did not go back to the task: ${page.url()}`);

  // 3 ---- the task shows what it has bound
  await has(main.locator('.ta-model'), [BETA, '2 台这样的机器人', '1 台已连接']);
  must(await main.locator('.ta-model b, .ta-app b').count() === 0, 'markup in a brand was not escaped');
  await has(appRow('通道巡检'), ['v1', BETA, '1 步', '前往巡检点', '2 台可用', '1 台已连接', '配置检查', '现场测试', '发布', '下一步：配置检查']);
  same(await main.locator('.ta-app').count(), 1, 'applications on the task');
  await main.locator('#task-skills').waitFor();
  await has(main.locator('#task-skills'), ['巡检狗一号', '巡检狗二号']);
  await lacks(main.locator('#task-skills'), ['协作臂']);                     // a robot of another brand and model is not this task's
  await page.evaluate(async id => { await api('harness', {application_id: id}); await refresh(true); }, ids.round);
  await page.waitForFunction(() => /下一步：现场测试/.test(document.querySelector('.ta-app')?.innerText || ''));
  same(await appRow('通道巡检').locator('.ta-steps .done').count(), 1, 'steps of the application that have passed');
  await appRow('通道巡检').locator('[data-action="deliver-open"]').click();
  await dialog.getByText('应用交付清单').waitFor();
  await has(dialog, ['通道巡检', BETA]);
  await dialog.locator('[data-action="close"]').first().click();
  must(await noSidewaysScroll(), 'the page of a task scrolls sideways at 1440');
  await shot('3-task-with-application');

  // 4 ---- the dashboard
  await main.locator('.tw-back a').click();
  const card = main.locator('.tw-card', {hasText: '仓库巡检'});
  await card.waitFor();
  await has(card, ['品牌型号', BETA, '绑定了 1 个应用 · 0 个已发布', '1 个 · 通道巡检', '2 台 · 1 台已连接']);
  await has(main.locator('.tw-figures'), ['绑定的应用', '1']);
  await shot('4-dashboard');

  // 5 ---- a second task binds the same application from its own dialog
  await newTask('夜间巡检', '夜里再巡一圈。');
  await main.locator('.ta-head [data-action="taskapp-pick"]').click();
  await has(dialog, ['绑定第一个应用后，这个任务就固定为它的品牌型号', BETA, ACME, '通道巡检', '货架取件']);
  await page.screenshot({path: file('5-pick-dialog')});
  await dialog.locator('.ta-pick-row', {hasText: '通道巡检'}).locator('[data-action="taskapp-bind"]').click();
  await appRow('通道巡检').waitFor();
  must(!(await dialog.isVisible()), 'the dialog stayed open after binding');
  snapshot = await appState();
  same(snapshot.task_applications.map(b => b.application_id), [ids.round, ids.round], 'one application bound to two tasks');
  await page.goto(cfg.url + '/#skills');
  await appCard('通道巡检').waitFor();
  must(await main.locator('.ta-banner').count() === 0, 'the banner of a finished errand is still shown');
  await has(appCard('通道巡检'), ['已绑定任务：', '仓库巡检', '夜间巡检']);
  await has(appCard('货架取件'), ['还没有绑定到任务']);

  // 6 ---- another brand and model is refused, with the reason
  await appCard('货架取件').locator('[data-action="taskapp-tasks"]').click();
  await has(dialog, ['绑定到任务', '货架取件', ACME, '仓库巡检', '夜间巡检', `这个任务用的是「${BETA}」的应用`]);
  must(await dialog.locator('[data-action="taskapp-bind-to"]:not([disabled])').count() === 0, 'an application of another brand and model can be bound from the dialog');
  await page.screenshot({path: file('6-other-model')});
  await dialog.locator('[data-action="close"]').first().click();
  const refusal = await page.evaluate(({task, app}) => api('task-apps/bind', {project_id: task, application_id: app}).then(() => null, e => [e.reason, e.message]), {task: ids.task, app: ids.pick});
  must(refusal && refusal[0] === 'task_other_model' && refusal[1].includes('Acme Robotics X1'), `the backend did not refuse it as expected: ${JSON.stringify(refusal)}`);

  // 7 ---- unbinding asks first, removes the relation only, and frees the task
  await page.evaluate(id => { projectId = id; sessionStorage.setItem('brain-project', id); location.hash = 'task'; }, ids.task);
  await appRow('通道巡检').locator('[data-action="taskapp-unbind"]').click();
  await has(dialog, ['解除绑定', '只解除这个任务和应用的关系', '保留在「机器人应用」里']);
  await dialog.locator('[data-action="close"]').first().click();
  same(await main.locator('.ta-app').count(), 1, 'applications on the task after cancelling');
  await appRow('通道巡检').locator('[data-action="taskapp-unbind"]').click();
  await dialog.locator('[data-action="taskapp-unbind-confirm"]').click();
  await main.locator('.ta-empty').waitFor();
  await has(main.locator('.ta-empty'), ['从账号已有的应用里选一个绑定']);
  snapshot = await appState();
  same(snapshot.applications.length, 2, 'applications of the account after unbinding');
  same(snapshot.evaluations.length, 1, 'test reports after unbinding');
  same(snapshot.task_applications.length, 1, 'bindings after unbinding one');
  await main.locator('.ta-empty [data-action="taskapp-pick"]').click();
  await dialog.locator('.ta-pick-row', {hasText: '货架取件'}).locator('[data-action="taskapp-bind"]').click();
  await appRow('货架取件').waitFor();
  await has(main.locator('.ta-model'), [ACME, '1 台这样的机器人']);
  await has(main.locator('#task-skills'), ['协作臂']);

  // 8 ---- deleting a task leaves the applications
  await main.locator('.tw-back a').click();
  await main.locator('.tw-card', {hasText: '夜间巡检'}).locator('[data-action="task-remove"]').click();
  await has(dialog, ['和 1 个机器人应用的绑定（通道巡检）', '绑定的应用本身不会删除']);
  await page.screenshot({path: file('8-delete-task')});
  await dialog.locator('[data-action="task-remove-confirm"]').click();
  await main.locator('.tw-card', {hasText: '夜间巡检'}).waitFor({state: 'detached'});
  snapshot = await appState();
  same(snapshot.applications.length, 2, 'applications of the account after deleting a task');
  same(snapshot.task_applications.map(b => b.application_id), [ids.pick], 'bindings after deleting a task');

  // 9 ---- a reload stays on the task; every width
  await main.locator('.tw-card', {hasText: '仓库巡检'}).locator('[data-action="task-open"]').click();
  await appRow('货架取件').waitFor();
  await page.reload();
  await appRow('货架取件').waitFor();
  must(page.url().endsWith('#task'), `a reload left the page of the task: ${page.url()}`);
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(350);      // the sidebar slides away below 600
    must(await noSidewaysScroll(), `the page of a task scrolls sideways at ${width}`);
    const inside = await main.locator('.ta-model, .ta-app, .ta-app header > *, .ta-steps, .ta-facts dd').evaluateAll(els => els.every(el => {
      const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= document.documentElement.clientWidth + 1;
    }));
    must(inside, `a part of the task's page leaves the window at ${width}`);
    if ([320, 768].includes(width)) await shot(`9-width-${width}`);
  }
  await page.setViewportSize(DESKTOP);

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('task_apps_browser: passed' + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
