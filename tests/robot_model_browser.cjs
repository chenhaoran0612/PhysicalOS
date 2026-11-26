// Issue #48 browser acceptance: a robot has a brand and model; applications and skills belong to one.
//   1  adding a robot by its service address asks for the brand and model: a device that reports none is
//      refused without them (said in the form), and added once they are typed; the card shows them;
//   2  a device that reports its brand and model is added with nothing typed, and its card offers no edit;
//   3  「机器人应用」 lists by brand and model: a filter, and the brand and model on every application and skill;
//   4  the form of an application offers only the skills of the chosen robot's brand and model;
//   5  the delivery checklist chooses among the robots of the brand and model; a check and an on-site test
//      go to the robot chosen, and the other robot of the model has none of that evidence;
//   6  a robot of another brand and model is refused by the backend (code app_other_model);
//   7  a person changes the typed brand and model of a robot; the application is no longer for it;
//   8  nothing overflows at 375, 768 and 1440.
// Fixture: tests/robot_model_browser_fixture.py (three HTTP contract devices; no real machine).
// Usage: node tests/robot_model_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const {addRobotWay, robotAction} = require('./robots_page_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
const BETA = 'Beta <b> B2', ACME = 'Acme Robotics X1';      // a brand with markup in it must be shown as text
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-model-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `robot-model-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const has = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const calls = () => page.evaluate(() => fetch('/fixture/calls').then(r => r.json()));
  const card = robotId => page.locator(`article.card:has([data-id="${robotId}"])`).first();
  const robotNamed = async name => (await appState()).robots.find(r => r.name === name);
  const access = cfg.access;

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '品牌型号验收', email: 'robot-model@example.test', password: crypto.randomBytes(9).toString('hex') + 'A1'});
  await page.locator('[data-action="journey-start"]').first().click();
  await page.locator('form[data-form=project]').waitFor();
  await dialog.locator('[name=name]').fill('园区巡检');
  await dialog.locator('[name=objective]').fill('沿路线巡检');
  await dialog.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});

  // 1 ---- a device that reports no brand and model: typed, or refused
  await page.goto(cfg.url + '/#robots');
  await addRobotWay(page, 'robot-add');
  const add = dialog.locator('form[data-form=robot]');
  await add.waitFor();
  await has(add, ['品牌', '型号', '设备没有上报时必须填写']);
  await add.locator('[name=name]').fill('巡检狗一号');
  await add.locator('[name=family]').selectOption('quadruped');
  await add.locator('[name=endpoint]').fill(cfg.devices['silent-1']);
  await add.locator('[name=token]').fill(access);
  await add.locator('button[type=submit]').click();
  await page.waitForFunction(() => /品牌和型号/.test(document.querySelector('#dialog .form-error')?.textContent || ''));
  same((await appState()).robots.length, 0, 'robots after the refused form');
  await add.locator('[name=vendor]').fill('Beta <b>');
  await add.locator('[name=model]').fill('B2');
  await add.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  const first = await robotNamed('巡检狗一号');
  same(first.product && [first.product.vendor, first.product.model, first.product.source], ['Beta <b>', 'B2', 'manual'], 'the typed brand and model');
  await has(card(first.id), [BETA]);
  must(await page.locator('.robot-model-name b').count() === 0, 'markup in a brand was not escaped');
  await page.screenshot({path: file('1-card')});

  // 2 ---- a device that reports them: nothing typed; and a second robot of the first brand and model
  const ids = await page.evaluate(async ({cfg, access}) => {
    const reporting = await api('robots', {project_id: projectId, name: '协作臂', family: 'manipulator', endpoint: cfg.devices.reporting, token: access});
    const twin = await api('robots', {project_id: projectId, name: '巡检狗二号', family: 'quadruped', endpoint: cfg.devices['silent-2'], token: access,
                                      vendor: 'beta <B>', model: 'b2'});
    const firstId = state.robots.find(r => r.name === '巡检狗一号').id;
    for (const id of [firstId, reporting.id, twin.id]) await api('robots/connect', {robot_id: id});
    const skill = {project_id: projectId, version: '1', requires: ['navigation.goal'], entrypoint: 'navigate',
                   success_condition: '到达', stop_condition: '超时', model_ids: []};
    const walk = await api('skills', {...skill, name: '前往巡检点', robot_id: firstId});
    const reach = await api('skills', {...skill, name: '伸到货架', robot_id: reporting.id});
    await refresh(true);
    return {first: firstId, reporting: reporting.id, twin: twin.id, walk: walk.id, reach: reach.id};
  }, {cfg, access});
  let snapshot = await appState();
  const reporting = snapshot.robots.find(r => r.id === ids.reporting);
  same([reporting.product.vendor, reporting.product.model, reporting.product.source], ['Acme Robotics', 'X1', 'device'], 'what the device reported');
  await has(card(ids.reporting), [ACME]);
  must(await card(ids.reporting).locator('[data-action="engine-robot-model"]').count() === 0, 'a reported brand and model can be typed over');
  must(await card(ids.first).locator('[data-action="engine-robot-model"]').count() === 1, 'no way to change a typed brand and model');

  // 3 ---- 机器人应用 by brand and model
  await page.goto(cfg.url + '/#skills');
  const filter = page.locator('.robot-model-filter');
  await filter.waitFor();
  await has(filter, ['全部品牌型号', ACME, '1 台机器人', BETA, '2 台机器人']);
  same(await page.locator('#main table tbody tr').count(), 2, 'skills listed for all');
  await filter.locator('.robot-model-chip', {hasText: ACME}).click();
  same(await page.locator('#main table tbody tr').count(), 1, 'skills listed for one brand and model');
  await has(page.locator('#main table tbody'), ['伸到货架', ACME]);
  same(await filter.locator('.robot-model-chip.active').getAttribute('aria-pressed'), 'true', 'the chosen filter');
  await filter.locator('.robot-model-chip').first().click();

  // 4 ---- an application takes the skills of its robot's brand and model
  await page.locator('#main [data-action="application-add"]').first().click();
  const form = dialog.locator('form[data-form=application]');
  await form.waitFor();
  await has(form.locator('[name=robot_id]'), [BETA, ACME]);
  await form.locator('[name=robot_id]').selectOption(ids.first);
  await dialog.locator('[data-action="step-add"]').click();
  const step = form.locator('.step-editor').first();
  await step.waitFor();
  const offered = () => step.locator('[name=skill_id] option').evaluateAll(all => all.filter(o => !o.disabled).map(o => o.textContent));
  same(await offered(), ['前往巡检点'], 'skills offered for the first brand and model');
  await has(form.locator('.robot-model-step-note'), [BETA, '同品牌型号的每一台机器人']);
  await form.locator('[name=robot_id]').selectOption(ids.reporting);
  same(await offered(), ['伸到货架'], 'skills offered after choosing the other robot');
  same(await step.locator('[name=skill_id]').inputValue(), ids.reach, 'the chosen skill follows the robot');
  await form.locator('[name=robot_id]').selectOption(ids.first);
  same(await step.locator('[name=skill_id]').inputValue(), ids.walk, 'the chosen skill follows the robot back');
  await form.locator('[name=name]').fill('每日巡检');
  await page.screenshot({path: file('4-application-form')});
  await form.locator('button[type=submit]').click();

  // 5 ---- the checklist chooses among the robots of the brand and model
  const line = dialog.locator('.robot-model-delivery');
  await line.waitFor();
  await has(line, [BETA, '同品牌型号的 2 台机器人']);
  const pick = line.locator('select[data-rm-delivery]');
  same(await pick.locator('option').allInnerTexts(), ['巡检狗一号', '巡检狗二号'], 'the robots the application can be used with');
  snapshot = await appState();
  const application = snapshot.applications[0];
  same(application.product, {vendor: 'Beta <b>', model: 'B2'}, 'the brand and model of the application');
  await page.screenshot({path: file('5-checklist')});
  await pick.selectOption(ids.twin);
  await page.waitForFunction(id => document.querySelector('#dialog select[data-rm-delivery]')?.value === id &&
    /巡检狗二号/.test(document.querySelector('#dialog .detail-meta')?.textContent || ''), ids.twin);
  await dialog.locator('[data-action="deliver-check"]').first().click();
  await dialog.getByRole('heading', {name: '下一步：现场测试', exact: true}).waitFor();
  await dialog.locator('[data-action="deliver-onsite"]').first().click();
  const onsite = dialog.locator('form[data-form=deliver-onsite]');
  await onsite.waitFor();
  await has(onsite, ['巡检狗二号']);
  await onsite.locator('[name=hardware_authorized]').check();
  await onsite.locator('button[type=submit]').click();
  await dialog.getByRole('heading', {name: '下一步：发布到机器人', exact: true}).waitFor();
  const asked = await calls();
  must(asked['silent-2'].includes('evaluate') && !asked['silent-1'].includes('evaluate'), 'the test went to the wrong robot: ' + JSON.stringify(asked));
  snapshot = await appState();
  same(snapshot.evaluations.filter(e => e.scope === 'onsite').map(e => e.robot_id), [ids.twin], 'the robot of the evidence');
  // The other robot of the model starts from its own check.
  await dialog.locator('select[data-rm-delivery]').selectOption(ids.first);
  await dialog.getByRole('heading', {name: '先检查应用配置', exact: true}).waitFor();
  must(await dialog.locator('[data-action="deliver-publish"]').count() === 0, 'a robot without evidence of its own can be released to');

  // 6 ---- a robot of another brand and model is refused by the backend
  const refusal = await page.evaluate(async ({applicationId, robotId}) => {
    const response = await fetch('/api/harness', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({application_id: applicationId, robot_id: robotId})});
    return {status: response.status, body: await response.json()};
  }, {applicationId: application.id, robotId: ids.reporting});
  same([refusal.status, refusal.body.code], [400, 'app_other_model'], 'a check on a robot of another brand and model');
  await dialog.locator('.close-dialog').click();

  // 3 again ---- the application on its page
  await page.locator('#main .cards article.card').first().waitFor();
  await has(page.locator('#main .cards article.card').first(), ['每日巡检', BETA, '2 台同品牌型号的机器人可用']);
  await page.locator('.robot-model-filter .robot-model-chip', {hasText: ACME}).click();
  await has(page.locator('#main'), ['这个品牌型号还没有应用']);
  await page.locator('.robot-model-filter .robot-model-chip').first().click();

  // 8 ---- widths
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    must(wide <= 1, `the page scrolls sideways by ${wide}px at ${width}`);
    await page.screenshot({path: file(`8-apps-${width}`)});
  }
  await page.setViewportSize(DESKTOP);

  // 7 ---- a person changes the typed brand and model
  await page.goto(cfg.url + '/#robots');
  await robotAction(card(ids.first), 'engine-robot-model').click();
  const assign = dialog.locator('form[data-form="engine-robot-model"]');
  await assign.waitFor();
  same([await assign.locator('[name=vendor]').inputValue(), await assign.locator('[name=model]').inputValue()], ['Beta <b>', 'B2'], 'the form starts from what is saved');
  await has(assign, ['1 个应用不能再用在它上面']);
  await assign.locator('[name=vendor]').fill('Gamma');
  await assign.locator('[name=model]').fill('G3');
  await page.screenshot({path: file('7-assign')});
  await assign.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  await has(card(ids.first), ['Gamma G3']);
  const after = await page.evaluate(id => api('applications/readiness', {project_id: projectId, application_id: id}), application.id);
  same(after.robots.map(r => r.id), [ids.twin], 'the robots of the application after one changed its brand and model');

  must(!errors.length, 'page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({passed: true, engine: engine.name(), typedOrRefused: true, deviceReported: true, byBrandAndModel: true,
                              skillsOfTheModelOnly: true, robotChosenInChecklist: true, otherModelRefused: true, assigned: true, widths: WIDTHS}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
