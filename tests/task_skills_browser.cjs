// Browser acceptance for issue #46, the page of one task: it shows the skills the task's robots have,
// where the task uses them and how each use is configured (the order of places, points with their
// coordinates, numbers with units); the head has no 「新建任务项目」 and no 「使用指南」, and the page no
// longer says "one of N tasks".
//   node tests/task_skills_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ux01_browser_fixture.py (a fresh state directory; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// The robot is a simulated quadruped the console starts itself (a real runtime, a simulated body).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ux01-qa.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const main = page.locator('#main'), section = main.locator('#task-skills');
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth &&
    document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth);
  const shot = async name => {
    const size = page.viewportSize(), high = await page.evaluate(() => document.querySelector('#content-scroll').scrollHeight + 80);
    await page.setViewportSize({width: size.width, height: Math.max(size.height, Math.min(high, 6000))});
    await page.screenshot({path: `${shots}/task-skills-${name}.png`, fullPage: true});
    await page.setViewportSize(size);
  };
  const skill = id => section.locator(`[data-ts-skill="${id}"]`);
  const row = (id, from, title) => skill(id).locator(`.ts-use[data-from="${from}"] .ts-row`, {hasText: title});

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '技能验收', email: `skills-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'skills1-' + crypto.randomBytes(8).toString('hex')});
  await main.locator('.tw-board').waitFor();

  // 1. A new task: its page has the way back and nothing else above the head; the section says what is missing.
  await main.locator('.tw-head [data-action="journey-start"]').click();
  await page.locator('#dialog [name="name"]').fill('办公室日常巡检');
  await page.locator('#dialog [name="objective"]').fill('完成每天的办公室日常巡检任务。');
  await page.locator('#dialog button[type=submit]').click();
  await main.locator('.tw-back').waitFor();
  check((await main.locator('.tw-back').innerText()).trim() === '← 全部任务', `The line above the head says more than the way back: ${await main.locator('.tw-back').innerText()}`);
  check(!(await main.innerText()).includes('个任务之一'), 'The page still says it is one of N tasks');
  const headButtons = await main.locator('.page-head button').allInnerTexts();
  check(!headButtons.some(t => t.includes('新建任务项目') || t.includes('使用指南')), `The head of a task still has: ${headButtons}`);
  check(await main.locator('.journey-next').isVisible() && await main.locator('.journey-step').count() === 5, 'The steps of the task left its page');
  await section.getByText('这个任务还没有机器人').waitFor();
  const order = await main.evaluate(el => ['.journey-steps', '#task-skills', '.journey-options'].map(s => el.querySelector(s).getBoundingClientRect().top));
  check(order[0] < order[1] && order[1] < order[2], `The skills are not between the steps and the optional abilities: ${order}`);
  await shot('1-no-robot');

  // 2. A simulated quadruped with three skills: one used by an application, one run directly, one not used.
  const ids = await page.evaluate(async () => {
    const robot = await api('sims', {kind: 'quadruped', project_id: projectId});
    const install = async example => {
      const preview = await api('skills/package-preview', {robot_id: robot.id, example, project_id: projectId});
      await api('skills/package-install', {robot_id: robot.id, example, package_digest: preview.package_digest,
                                           request_id: 'install-' + example + '-' + Date.now(), project_id: projectId});
      return preview.skill;
    };
    const patrolPlaces = await install('patrol_places');
    await install('patrol');
    await install('free_walk');
    const registered = await api('skills', {project_id: projectId, name: '巡检', entrypoint: 'patrol_places', version: patrolPlaces.version,
      requires: ['base.navigate_to_place', 'safety.stop'], success_condition: '每个地点都到达', stop_condition: '有一处没到',
      parameters: JSON.stringify(patrolPlaces.parameters)});
    await api('applications', {project_id: projectId, name: '每日巡检', robot_id: robot.id, version: '1',
      steps: [{skill_id: registered.id, parameters: {places: '会议室、二楼', return_to: '出发点', loops: 2}, timeout_s: 900}]});
    await api('robots/run-skill', {robot_id: robot.id, skill_id: 'patrol', parameters: {waypoints: [[0.5, 0], [0.5, 0.5]], loops: 1},
                                   hardware_authorized: true, request_id: 'run-' + Date.now(), project_id: projectId});
    await refresh(true);
    return {robot: robot.id};
  });
  await section.locator(`[data-ts-robot="${ids.robot}"]`).getByText('技能列表是刚从这台机器人上读到的。').waitFor({timeout: 20000});
  const robot = section.locator(`[data-ts-robot="${ids.robot}"]`);
  check((await robot.locator('.ts-robot-head').innerText()).includes('已安装 3 个技能 · 这个任务用到 2 个'), `The robot's counts are wrong: ${await robot.locator('.ts-robot-head').innerText()}`);
  const places = await robot.locator('.ts-places .ts-chip').allInnerTexts();
  check(['出发点', '会议室', '二楼'].every(p => places.includes(p)), `The places the robot knows are not listed: ${places}`);
  check((await robot.locator('.ts-places').innerText()).includes('没有报告它们的坐标和所用的地图'), 'The page does not say that coordinates and the map were not reported');

  // The application's step: the order of the places as saved, where the patrol ends, how often, the time limit.
  check((await skill('patrol_places').locator('.ts-use[data-from="app"] .ts-from').innerText()).includes('应用「每日巡检」第 1 步（共 1 步）'), 'The step that uses the skill is not named');
  check((await skill('patrol_places').locator('.ts-from').innerText()).includes('最多 900 秒'), 'The time limit of the step is not shown');
  const route = await row('patrol_places', 'app', '巡检地点').locator('.ts-route li').allInnerTexts();
  check(route.join('>') === '会议室>二楼', `The order of the patrol is not the saved one: ${route}`);
  check((await row('patrol_places', 'app', '最后回到').locator('.ts-route li').allInnerTexts()).join() === '出发点', 'Where the patrol ends is not shown');
  check((await row('patrol_places', 'app', '圈数').locator('dd').innerText()).includes('2 圈'), 'The number of rounds is not shown with its unit');
  check((await row('patrol_places', 'app', '最快速度').locator('dd').innerText()).includes('未填写'), 'A parameter left empty is not said to be empty');
  // The direct run: its points in order, with coordinates.
  check((await skill('patrol').locator('.ts-from').innerText()).includes('最近一次直接运行（共 1 次）'), 'The direct run is not named');
  const points = await row('patrol', 'run', '航点').locator('.ts-points li').allInnerTexts();
  check(points.length === 2 && /x\s*0\.5/.test(points[1]) && /y\s*0\.5/.test(points[1]), `The points of the run are not shown with coordinates: ${JSON.stringify(points)}`);
  // The skill the task does not use: said so, with the values it would run with.
  check(await skill('free_walk').getByText('这个任务还没有用到').isVisible(), 'An unused skill is not told apart');
  check(await skill('free_walk').locator('.ts-row[data-origin="default"]').count() >= 1, 'An unused skill does not show its default values');
  check(await noSidewaysScroll(), 'The page of the task scrolls sideways at 1440');
  await shot('2-skills');

  // 3. Reading again keeps the section; nothing else on the page is drawn again by it.
  await page.evaluate(() => { document.querySelector('.journey-next').dataset.kept = '1'; });
  await robot.locator('[data-action="task-skills-reload"]').click();
  await section.locator(`[data-ts-robot="${ids.robot}"]`).getByText('技能列表是刚从这台机器人上读到的。').waitFor({timeout: 20000});
  check(await page.evaluate(() => document.querySelector('.journey-next').dataset.kept === '1'), 'Reading the skills again drew the whole page again');

  // 4. Every width: nothing scrolls sideways and nothing leaves the window.
  for (const width of [320, 375, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(350);      // the sidebar slides away below 600
    check(await noSidewaysScroll(), `The page of the task scrolls sideways at ${width}`);
    const inside = await section.locator('.ts-robot, .ts-use, .ts-route li, .ts-points li, .ts-chip').evaluateAll(els => els.every(el => {
      const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= document.documentElement.clientWidth + 1;
    }));
    check(inside, `A part of the skills section leaves the window at ${width}`);
    if ([320, 768].includes(width)) await shot(`3-width-${width}`);
  }
  await page.setViewportSize({width: 1440, height: 1000});

  // 5. The robot stopped: the section falls back to what the console recorded, and says so.
  await page.evaluate(async id => { await api('sims/stop', {robot_id: id}); await refresh(true); }, ids.robot);
  await section.locator(`[data-ts-robot="${ids.robot}"]`).getByText('按控制台的安装记录显示').waitFor({timeout: 20000});
  check((await skill('patrol_places').locator('.ts-marks').innerText()).includes('按记录已安装'), 'A skill installed by the console is not shown from its record');
  check((await row('patrol_places', 'app', '巡检地点').locator('.ts-route li').allInnerTexts()).join('>') === '会议室>二楼', 'The configuration is lost when the robot is not connected');
  check(await section.locator('[data-action="task-skills-reload"]').count() === 0, 'A robot that is not connected is offered for reading');
  await shot('4-not-connected');

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('task_skills_browser: passed' + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(e => { console.error(e); process.exit(1); });
