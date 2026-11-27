// Issue #60 browser acceptance: 机器人应用 is a waterfall of applications; an application is a workflow.
//   0  with nothing yet, the page says what an application is and starts with the skills; the skills
//      table and 「注册技能」 are not on it;
//   1  the waterfall: every application once, its brand and model, its workflow step by step; cards
//      of different heights in several columns on a wide page, one column on a narrow one;
//   2  「技能」 opens the page of the skills, part of 机器人应用 in the sidebar and the breadcrumb;
//   3  a card opens the application's own page: the workflow from start to end, robots, progress,
//      tasks, versions; reloading keeps it; a check from there moves the progress on;
//   4  「修改工作流」 saves a new version and the page shows it; an older version says so;
//   5  a new application is made for a brand and model, not for a robot: one choice per brand and
//      model, the skills offered follow it, and what is saved is the brand and model's;
//   6  the keyboard opens a card; reduced motion; nothing overflows from 320 to 1920.
// Fixture: tests/robot_model_browser_fixture.py (three HTTP contract devices; no real machine).
// Usage: node tests/app_gallery_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [320, 375, 768, 1024, 1440, 1920], SHOT_WIDTHS = [320, 768, 1024, 1440], DESKTOP = {width: 1440, height: 1000};
const BETA = 'Beta <b> B2', ACME = 'Acme Robotics X1';      // a brand with markup in it must be shown as text
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-robot-model-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `app-gallery-${engine.name()}-${name}.png`);
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
  const eachWidth = async (name, check) => {
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      // On a narrow page the sidebar slides away: look once it has.
      await page.waitForFunction(() => innerWidth > 600 || document.querySelector('#sidebar').getBoundingClientRect().right <= 0);
      must(await noSidewaysScroll(), `${name} scrolls sideways at ${width}px`);
      if (check) await check(width);
      if (SHOT_WIDTHS.includes(width)) await shot(`${name}-${width}`);
    }
    await page.setViewportSize(DESKTOP);
  };
  const cards = main.locator('.ag-waterfall article.card.ag-card');
  const card = name => cards.filter({hasText: name});
  const boxes = () => cards.evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.height)]; }));
  const crumb = () => page.locator('#breadcrumb').innerText();
  const activeNav = () => page.locator('#navigation a.active').allInnerTexts().then(t => t.map(x => x.trim()));
  const closeDialog = () => dialog.locator('[data-action="close"]').first().click();

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '应用瀑布流验收', email: 'app-gallery@example.test', password: crypto.randomBytes(9).toString('hex') + 'A1'});
  await main.locator('.tw-board').waitFor();

  // 0 ---- nothing yet: what an application is, skills first; no skills table on this page
  await page.goto(cfg.url + '/#skills');
  await main.locator('.page-head [data-action="application-add"]').waitFor();
  await has(main, ['机器人应用', '还没有应用']);                              // issue #68: an empty page says so and nothing else
  await lacks(main, ['技能资产', '注册技能']);
  must(await main.locator('table').count() === 0, 'the skills table is still on 机器人应用');
  must(await main.locator('[data-action="ux-adopt"], [data-action="appg-skills"]').count() === 0, 'the page still has a way to the skills (issue #68)');
  await shot('0-empty');

  const ids = await page.evaluate(async ({cfg}) => {
    const robot = (name, family, device, more = {}) => api('robots', {project_id: projectId, name, family, endpoint: cfg.devices[device], token: cfg.access, ...more});
    const arm = await robot('协作臂', 'manipulator', 'reporting');
    const dog = await robot('巡检狗一号', 'quadruped', 'silent-1', {vendor: 'Beta <b>', model: 'B2'});
    const twin = await robot('巡检狗二号', 'quadruped', 'silent-2', {vendor: 'beta <B>', model: 'b2'});
    await api('robots/connect', {robot_id: arm.id});
    await api('robots/connect', {robot_id: twin.id});            // the twin is connected, the first dog is not
    const skill = {project_id: projectId, version: '1', requires: ['navigation.goal'], entrypoint: 'navigate',
                   success_condition: '到达', stop_condition: '超时', model_ids: []};
    const walk = await api('skills', {...skill, name: '前往巡检点', robot_id: dog.id});
    const look = await api('skills', {...skill, name: '环顾<四周>', version: '2', robot_id: dog.id});
    const reach = await api('skills', {...skill, name: '伸到货架', robot_id: arm.id});
    const made = (name, robotId, skills) => api('applications', {project_id: projectId, name, version: '1', robot_id: robotId,
      steps: skills.map((s, i) => ({skill_id: s.id, parameters: i ? {} : {goal: [1, 2]}, timeout_s: 60 + i}))});
    const pick = await made('货架取件', arm.id, [reach]);
    const round = await made('通道巡检', dog.id, [walk, look, walk]);
    const night = await made('夜间巡逻', dog.id, [walk, look, walk, look, walk, look, walk]);
    await refresh(true);
    return {arm: arm.id, dog: dog.id, twin: twin.id, walk: walk.id, look: look.id, reach: reach.id, pick: pick.id, round: round.id, night: night.id};
  }, {cfg});

  // 1 ---- the waterfall
  await cards.first().waitFor();
  same(await cards.count(), 3, 'one card per application');
  same(await cards.locator('.ag-open').allInnerTexts(), ['夜间巡逻', '通道巡检', '货架取件'], 'newest first');
  await has(card('通道巡检'), [BETA, 'v1', '前往巡检点', '环顾<四周>', '2 台同品牌型号的机器人可用 · 1 台已连接', '还没有绑定到任务']);
  same(await card('通道巡检').locator('.ag-step').count(), 3, 'the steps of a workflow on its card');
  await has(card('夜间巡逻'), ['还有 1 步']);
  await has(card('货架取件'), [ACME, '伸到货架', '1 台同品牌型号的机器人可用']);
  await lacks(main, ['技能资产']);
  must(await main.locator('table').count() === 0, 'a table on the waterfall page');
  let seen = await boxes();
  must(new Set(seen.map(b => b[0])).size >= 2, `the cards are not in several columns at 1440px: ${JSON.stringify(seen)}`);
  must(new Set(seen.map(b => b[1])).size >= 2, `the cards all have one height: ${JSON.stringify(seen)}`);
  await eachWidth('1-waterfall', async width => {
    if (width > 375) return;
    seen = await boxes();
    must(new Set(seen.map(b => b[0])).size === 1, `more than one column at ${width}px: ${JSON.stringify(seen)}`);
  });
  must(await page.locator('.robot-model-filter').count() === 0, 'the filter by brand and model is still on the page (issue #68)');
  await page.evaluate(async () => { await api('projects', {name: '巡检任务', objective: '每天巡检一圈'}); await refresh(true); });   // issue #68: an application is created for a task
  await main.locator('.page-head [data-action="application-add"]').click();
  await dialog.locator('form[data-form=application] [name=robot_id]').waitFor();
  same(await dialog.locator('form[data-form=application] [name=robot_id] option').evaluateAll(list => list.map(o => o.value)), [ids.arm, ids.twin],
       'a connected robot stands for its brand and model');
  await closeDialog();

  // 2 ---- the skills have their own page
  await page.goto(cfg.url + '/#app-skills');                                 // issue #68: no button on 机器人应用 leads here
  await main.locator('.ag-skills table').waitFor();
  must(page.url().endsWith('#app-skills'), `「技能」 did not open the page of the skills: ${page.url()}`);
  same(await main.locator('.ag-skills tbody tr').count(), 3, 'every skill');
  await has(main.locator('.ag-skills'), ['前往巡检点', '环顾<四周>', '伸到货架', BETA, ACME, '2 个应用在用', '1 个应用在用']);
  same([await crumb(), await activeNav()], ['机器人应用 / 技能', ['机器人应用']], 'where the page of the skills is');
  must(await main.locator('.page-head [data-action="skill-add"]').count() === 1, 'a skill cannot be registered on the page of the skills');
  await eachWidth('2-skills');
  await main.locator('[data-action="appg-back"]').click();
  await cards.first().waitFor();
  must(page.url().endsWith('#skills'), `「全部应用」 did not go back: ${page.url()}`);

  // 3 ---- an application opens as its workflow: anywhere on its card
  await card('通道巡检').locator('.ag-flow').scrollIntoViewIfNeeded();
  const flow = await card('通道巡检').locator('.ag-flow').boundingBox();
  await page.mouse.click(flow.x + flow.width / 2, flow.y + flow.height / 2);       // not on the name: on the steps
  await main.locator('.ag-nodes').waitFor();
  must(page.url().endsWith('#app'), `a card did not open its application: ${page.url()}`);
  same(await main.locator('h1').innerText(), '通道巡检', 'the name of the application');
  same(await main.locator('.ag-node-step h3').allInnerTexts().then(t => t.map(x => x.replace(/\s+/g, ' ').trim())),
       ['前往巡检点 v1', '环顾<四周> v2', '前往巡检点 v1'], 'the workflow, in order');
  same(await main.locator('.ag-nodes > li').evaluateAll(list => [list[0].className, list[list.length - 1].className]),
       ['ag-node ag-node-start', 'ag-node ag-node-end'], 'from a start to an end');
  await has(main.locator('.ag-node-step').first(), ['navigate', '到达', '超时', '最长 60 秒', 'navigation.goal', 'goal', '[1,2]']);
  await has(main.locator('.ag-node-step').nth(1), ['最长 61 秒', '没有参数']);
  await has(main.locator('.ag-app-head'), [BETA, 'v1']);
  await has(main.locator('.ag-side'), ['2 台同品牌型号的机器人可用 · 1 台已连接', '巡检狗二号、巡检狗一号', '下一步：配置检查', '还没有绑定到任务', '当前查看']);
  same([await crumb(), await activeNav()], ['机器人应用 / 通道巡检', ['机器人应用']], 'where the page of an application is');
  await page.reload();
  await main.locator('.ag-nodes').waitFor();
  same([page.url().endsWith('#app'), await main.locator('h1').innerText()], [true, '通道巡检'], 'reloading keeps the application');
  await page.evaluate(async id => { await api('robots/connect', {robot_id: id}); await refresh(true); }, ids.dog);   // the check goes to the robot it was made with
  await has(main.locator('.ag-side'), ['2 台同品牌型号的机器人可用 · 2 台已连接', '巡检狗一号、巡检狗二号']);
  await main.locator('.ag-app-head [data-action="harness-run"]').click();
  await dialog.getByText('应用交付清单').waitFor();
  await closeDialog();
  await main.locator('.ag-side .ag-progress .done').first().waitFor();
  await has(main.locator('.ag-side'), ['下一步：现场测试']);
  await main.locator('.ag-app-head [data-action="deliver-open"]').click();
  await dialog.getByText('应用交付清单').waitFor();
  await closeDialog();
  await eachWidth('3-application');

  // 4 ---- 修改工作流: a new version; the page follows it
  await main.locator('.ag-app-head [data-action="revision-edit"]').click();
  const change = dialog.locator('form[data-form="revision-preview"]');
  await change.waitFor();
  await has(change, ['品牌型号']);
  await lacks(change, ['目标机器人']);
  same(await change.locator('[name=robot_id]').inputValue(), ids.dog, 'changing an application keeps the robot it was made with');
  await change.locator('[name=version]').fill('1.1');
  await change.locator('[data-action="revision-add"]').click();
  same(await change.locator('.revision-step').count(), 4, 'a step is added');
  await change.locator('button[type=submit]').click();
  await dialog.locator('form[data-form="revision-save"] button[type=submit]').click();
  await main.locator('.ag-node-step').nth(3).waitFor();
  if (await dialog.evaluate(d => d.open)) await closeDialog();
  await has(main.locator('.ag-app-head'), ['v1.1 · 共 2 个版本']);
  await has(main.locator('.ag-side'), ['下一步：配置检查']);
  await main.locator('.ag-versions [data-action="appg-version"]').click();
  await main.locator('.ag-stale').waitFor();
  await has(main.locator('.ag-stale'), ['这不是最新版本', 'v1.1']);
  same(await main.locator('.ag-node-step').count(), 3, 'the older version shows its own workflow');
  await main.locator('.ag-stale [data-action="appg-version"]').click();
  await main.locator('.ag-node-step').nth(3).waitFor();
  must(await main.locator('.ag-stale').count() === 0, 'the newest version still says it is old');
  await main.locator('[data-action="appg-back"]').click();
  await cards.first().waitFor();
  same(await cards.count(), 3, 'a new version is not another card');
  await has(card('通道巡检'), ['v1.1 · 共 2 个版本']);
  await card('通道巡检').locator('[data-action="taskapp-tasks"]').click();                      // the button on a card is not the card
  await dialog.locator('.ta-pick-row', {hasText: '巡检任务'}).waitFor();                         // issue #68: the task made above for the form
  must(page.url().endsWith('#skills'), 'the button on a card opened the application');
  await closeDialog();
  same(await card('通道巡检').locator('.ag-step').count(), 4, 'the card shows the newest workflow');

  // 5 ---- a new application is for a brand and model
  await main.locator('.page-head [data-action="application-add"]').click();
  const made = dialog.locator('form[data-form=application]');
  await made.waitFor();
  await has(made, ['品牌型号']);
  await lacks(made, ['目标机器人', '目标选仿真机器人']);
  await has(dialog, ['选任务和品牌型号']);                                   // issue #68: the form asks for a task too
  same(await made.locator('[name=robot_id] option').allInnerTexts(), [`${ACME} · 1 台机器人`, `${BETA} · 2 台机器人`], 'one choice per brand and model');
  same(await made.locator('[name=robot_id] option').evaluateAll(list => list.map(o => o.value)), [ids.arm, ids.dog],
       'with both connected, the one added first');
  // Issue #68: the form asks the server for the skills of the brand and model; they are offered once it has answered.
  const offered = async () => {
    await page.waitForFunction(() => { const t = document.querySelector('#dialog .ac-status')?.textContent || ''; return t && !t.includes('正在准备'); });
    return made.locator('.step-editor [name=skill_id] option:not([disabled])').allInnerTexts();
  };
  same(await offered(), ['伸到货架'], 'the skills of the first brand and model');
  await made.locator('[name=robot_id]').selectOption({label: `${BETA} · 2 台机器人`});
  same((await offered()).sort(), ['前往巡检点', '环顾<四周>'].sort(), 'the skills follow the brand and model');
  await made.locator('[name=name]').fill('清晨巡检');
  await made.locator('button[type=submit]').click();
  await dialog.getByText('应用交付清单').waitFor();
  await closeDialog();
  await card('清晨巡检').waitFor();
  same(await cards.count(), 4, 'the new application is in the waterfall');
  await has(card('清晨巡检'), [BETA, '2 台同品牌型号的机器人可用']);
  const saved = (await appState()).applications.find(a => a.name === '清晨巡检');
  same([saved.robot_id, saved.product.vendor, saved.product.model], [ids.dog, 'Beta <b>', 'B2'], 'what was saved');
  // Issue #68: the page has no filter by brand and model; the form starts from the task's brand and model (tests/app_create_browser.cjs).

  // 6 ---- keyboard, reduced motion, widths
  await card('货架取件').locator('.ag-open').focus();
  await page.keyboard.press('Enter');
  await main.locator('.ag-nodes').waitFor();
  same(await main.locator('h1').innerText(), '货架取件', 'Enter on a card opens it');
  await main.locator('[data-action="appg-back"]').click();
  await cards.first().waitFor();
  await page.emulateMedia({reducedMotion: 'reduce'});
  same(await cards.first().evaluate(el => getComputedStyle(el).transitionDuration), '0s', 'reduced motion');
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await eachWidth('6-waterfall-four');

  same(errors, [], 'page errors');
  await browser.close();
  console.log('app gallery browser acceptance passed');
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
