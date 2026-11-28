const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issues #66 and #75: 仿真试运行 starts from the tasks; a task lists its skills, and every skill
// has its own environment, built once and saved; entering a skill starts the task's simulated robot in that skill's
// environment (or moves it there), with that skill on it.
// Start tests/ux01_browser_fixture.py first. With the robot model packages (.build/robot-models or
// NEUXMIND_ROBOT_MODELS_DIR) the task is for a real profile and runs in MuJoCo with a picture; without,
// it is for the example quadruped. The agent-written environment is covered by tests/test_task_sim.py.
// Usage: node tests/task_sim_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
const playwright = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts';
const engine = process.env.BRAIN_BROWSER === 'webkit' ? playwright.webkit : playwright.chromium;

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine === playwright.chromium ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  const shot = name => page.screenshot({path: path.join(shots, name + '.png')});
  const must = (ok, message) => { if (!ok) throw Error(message); };
  const eventually = async (check, what, timeout = 20000) => {
    const until = Date.now() + timeout;
    for (;;) {
      if (await check()) return;
      if (Date.now() > until) throw Error(what);
      await page.waitForTimeout(250);
    }
  };
  const root = page.locator('.tsim');
  const runs = page.locator('#main [data-rb-sim]');

  await page.goto(cfg.url + '/#robots');
  const secret = 'ts' + Math.random().toString(36).slice(2, 10) + '75x';
  await gateSignIn(page, {name: '任务仿真', email: `sim75-${Date.now()}@example.test`, password: secret});

  // ---------------------------------------------------------------- data: two tasks, one with an application
  const made = await page.evaluate(async () => {
    const profile = state.sim_catalog.profiles.find(p => p.available && !p.builtin && p.id !== (state.duck_kit || {}).profile_id)
      || state.sim_catalog.profiles.find(p => p.builtin && p.type === 'quadruped');
    const helper = await api('sims', {profile_id: profile.id});            // a robot of that brand and model, to make the application for
    await refresh(true);
    const robot = state.robots.find(r => r.id === helper.id);
    // Issue #69: the bundled packages are per brand and model; patrol is for the example quadruped, status_report for all.
    const offered = (await api('skills/examples', {robot_id: helper.id})).items.map(i => i.example);
    const entry = offered.includes('patrol') ? 'patrol' : 'status_report';
    const task = await api('projects', {name: '仓库巡检', objective: '从门口走到货架，绕场一圈回到起点'});
    const bare = await api('projects', {name: '还没想好', objective: '以后再说'});
    const skill = await api('skills', {project_id: task.id, name: '绕场巡逻', version: '1.0', requires: ['navigation.goal'], entrypoint: entry,
      success_condition: '回到起点', stop_condition: '超时', model_ids: [], robot_id: robot.id});
    const app = await api('applications', {project_id: task.id, name: '巡检应用', version: '1.0', robot_id: robot.id,
      steps: [{skill_id: skill.id, parameters: entry === 'patrol' ? {waypoints: [[0.6, 0.0], [0.0, 0.0]], loops: 1} : {}, timeout_s: 60}]});
    await api('task-apps/bind', {project_id: task.id, application_id: app.id});
    // Another skill of the same brand and model that no application of the task uses (issue #68: a registered skill).
    await api('skills', {project_id: task.id, name: '定点巡视', version: '1.0', requires: ['navigation.goal'], entrypoint: 'spot_check',
      success_condition: '到达', stop_condition: '超时', model_ids: [], robot_id: robot.id});
    await refresh(true);
    return {profile, task, bare, product: robot.product, helper: robot.id, entry, other: 'spot_check'};
  });
  must(made.product && made.product.vendor, 'The helper robot reports no brand and model: ' + JSON.stringify(made.product));
  const physics = !made.profile.builtin;

  // ---------------------------------------------------------------- 1. the page is the list of tasks
  await page.locator('#navigation a[href="#simulation"]').click();
  const card = root.locator(`[data-tsim-task="${made.task.id}"]`);
  await card.waitFor();
  must(await page.locator('h1').innerText() === '仿真试运行', 'The page is not titled 仿真试运行');
  must(await root.locator('.tsim-card').count() === 2, 'Not every task is listed');
  const text = await card.innerText();
  must(text.includes('仓库巡检') && text.includes(made.product.model) && text.includes('巡检应用') && /\d+ 个，0 个有仿真环境|\d+ 个，\d+ 个有仿真环境/.test(text),
    'The card does not say what the task is for and how many skills it has: ' + text);
  must(await card.getAttribute('data-tsim-status') === (physics ? 'none' : 'ready'), 'A new task does not start as not built');
  must((await card.locator('[data-tsim-open]').innerText()) === '选择技能', 'A task does not open on its skills');
  const blocked = root.locator(`[data-tsim-task="${made.bare.id}"]`);
  must(await blocked.getAttribute('data-tsim-status') === 'blocked' && (await blocked.innerText()).includes('绑定一个应用'), 'A task without an application does not say what is missing');
  await shot('sim75-01-tasks');

  // ---------------------------------------------------------------- 2. the task: its skills, each with its own environment
  await card.locator('[data-tsim-open]').click();
  await root.locator('.tsim-head h1').waitFor();
  must(await root.locator('.tsim-head h1').innerText() === '仓库巡检', 'The task page is not titled with the task');
  const skillCard = id => root.locator(`[data-tsim-skill-card="${id}"]`);
  await skillCard(made.entry).waitFor();
  must(await root.locator('.tsim-skill-card').first().getAttribute('data-tsim-skill-card') === made.entry, 'The skill of the task\'s application is not first');
  must((await skillCard(made.entry).innerText()).includes('任务的应用用到'), 'The skill card does not say the application uses it');
  must((await skillCard(made.other).innerText()).includes('这个品牌型号的技能'), 'Another skill of the brand and model is not listed');
  must(await skillCard(made.entry).getAttribute('data-tsim-status') === (physics ? 'none' : 'ready'), 'A skill does not start without an environment of its own');
  if (physics) must(!(await runs.isVisible()), 'The list of runs shows before any skill has an environment (issue #74)');
  must((await page.locator('#breadcrumb').innerText()).includes('仿真试运行 / 仓库巡检'), 'The breadcrumb does not name the task');
  await shot('sim75-02-skills');

  // ---------------------------------------------------------------- 3. one skill: build its environment once
  await skillCard(made.entry).locator('[data-tsim-skill-open]').click();
  await eventually(async () => (await root.locator('.tsim-head h1').innerText()) === '绕场巡逻', 'The skill page is not titled with the skill');
  must((await page.locator('#breadcrumb').innerText()).includes('仿真试运行 / 仓库巡检 / 绕场巡逻'), 'The breadcrumb does not name the skill');
  if (physics) {
    const form = root.locator('[data-tsim-form]');
    await form.waitFor();
    must((await root.locator('.tsim-right').innerText()).includes('只为这一个技能布置场地'), 'The build does not say it is for this one skill');
    await runs.waitFor({state: 'attached'});
    must(!(await runs.isVisible()), 'The list of the skill\'s runs shows before it has an environment (issue #74)');
    await form.locator('[type=submit]').click();                      // the agent needs consent: refused in place
    await eventually(async () => (await form.locator('.form-error').innerText()).includes('同意'), 'Building with the agent without consent is not refused in the form');
    await shot('sim75-03-build-form');
    await root.locator('[data-tsim-empty]').click();
    await root.locator('.tsim-saved').waitFor({timeout: 180000});
    must((await root.locator('.tsim-saved').innerText()).includes('空场地'), 'The saved environment does not say where it came from');
    await eventually(() => root.locator('.tsim-preview').evaluate(img => img.complete && img.naturalWidth > 0).catch(() => false), 'The preview of the saved environment did not load');
  } else {
    must((await root.locator('.tsim-saved').innerText()).includes('不用构建'), 'The example robot\'s own stage does not say nothing is built');
  }
  await root.locator('[data-tsim-enter]').waitFor();
  await runs.waitFor({state: 'visible'});                              // issue #74: now there is something to run in
  must((await runs.innerText()).includes('这个技能的仿真运行记录'), 'The list of the skill\'s runs is not there once it has an environment');
  must(await root.locator('.tsim-skill [data-tsim-run]').isDisabled(), 'A skill can be run before the simulation is entered');
  await shot('sim75-04-saved');

  // ---------------------------------------------------------------- 4. enter: the robot, the picture, the skill
  await root.locator('[data-tsim-enter]').click();
  await root.locator('.tsim-stage[data-running="1"]').waitFor({timeout: 300000});
  const robotId = await page.evaluate(() => tsim.view.robot.id);
  must(robotId !== made.helper, 'The task did not get a robot of its own');
  must(await page.evaluate(id => state.robots.find(r => r.id === id)?.sim?.kind, robotId), 'The task\'s robot is not among the robots of the state');
  if (physics) {
    await eventually(() => root.locator('.tsim-frame').evaluate(img => !img.hidden && img.naturalWidth > 0), 'No live picture of the simulation', 60000);
  } else {
    await root.locator('.tsim-map svg').waitFor();
  }
  const panel = root.locator(`.tsim-skill[data-tsim-skill="${made.entry}"]`);
  await eventually(async () => (await panel.innerText()).includes('已安装'), 'The skill was not installed on entering');
  must((await panel.innerText()).includes('第 1 步') && (made.entry !== 'patrol' || (await panel.innerText()).includes('loops = 1')), 'The skill does not show the values of its step');
  await eventually(async () => !(await panel.locator('[data-tsim-run]').isDisabled()), 'The skill cannot be run after entering');
  await shot('sim75-05-entered');

  // ---------------------------------------------------------------- 5. run the skill and watch it on the page
  await panel.locator('[data-tsim-run]').click();
  const run = page.locator('#dialog[open] form[data-form="sim-run"]');
  await run.waitFor();
  must((await run.innerText()).includes('第 1 步'), 'The run form does not say which step its values come from');
  await run.locator('[name=hardware_authorized]').check();
  await run.locator('[type=submit]').click();
  await page.locator('#dialog[open]').waitFor({state: 'detached'}).catch(() => {});
  must(!(await page.locator('#dialog').evaluate(d => d.open)), 'The person was taken away from the page after starting the run');
  await eventually(async () => /正在运行|已完成/.test(await root.innerText()), 'The run does not show on the page');
  await shot('sim75-06-running');
  await eventually(async () => (await root.locator('.tsim-run').innerText().catch(() => '')).includes('已完成'), 'The skill did not finish in the simulation', 120000);
  // The fixture does not follow runs (only the app's entry point does; tests/run_boards_browser.cjs covers that): the row is there.
  await eventually(async () => (await runs.locator('tr.rb-row').count()) === 1 && (await runs.innerText()).includes(made.entry),
    'The run is not in the skill\'s list of runs', 30000);
  await shot('sim75-07-finished');

  // ---------------------------------------------------------------- 6. stop latches; the page says so
  await root.locator('[data-action="simlive-stop"]').click();
  await eventually(async () => (await root.locator('.tsim-line').innerText()).includes('急停已锁存'), 'Stopping does not show as a latched e-stop');
  must(await root.locator('[data-tsim-reset]').isVisible(), 'No way to release the e-stop from the page');
  must(await panel.locator('[data-tsim-run]').isDisabled(), 'A skill can be run while the e-stop is latched');

  // ---------------------------------------------------------------- 7. saved: after a reload the skill page is back, nothing is built again
  await page.reload();
  await root.locator('.tsim-stage[data-running="1"]').waitFor();
  must(await root.locator('[data-tsim-form]').count() === 0, 'The build form is back after a reload');
  must((await root.locator('.tsim-head h1').innerText()) === '绕场巡逻', 'A reload does not stay on the skill');
  await page.setViewportSize({width: 375, height: 800});
  must(await page.evaluate(() => { const m = document.querySelector('#content-scroll'); return m.scrollWidth <= m.clientWidth + 1; }), 'The skill page overflows at 375 px');
  await shot('sim75-08-narrow');
  await page.setViewportSize({width: 1440, height: 900});

  // ---------------------------------------------------------------- 8. another skill has its own environment
  await root.locator('[data-tsim-up]').click();
  await skillCard(made.entry).waitFor();
  await eventually(async () => (await skillCard(made.entry).getAttribute('data-tsim-status')) === (physics ? 'saved' : 'ready'), 'The skill does not show as having an environment');
  must((await skillCard(made.entry).innerText()).includes('仿真机器人在这里'), 'The card of the skill the robot is in does not say so');
  must(await skillCard(made.other).getAttribute('data-tsim-status') === (physics ? 'none' : 'ready'), 'Another skill got an environment it was never given');
  await skillCard(made.other).locator('[data-tsim-skill-open]').click();
  await eventually(async () => (await root.locator('.tsim-head p').innerText()).includes('这个品牌型号的技能'), 'The other skill\'s page does not say where it comes from');
  if (physics) {
    await root.locator('[data-tsim-form]').waitFor();                 // its own environment, not the first skill's
    must(await root.locator('[data-tsim-enter]').count() === 0, 'A skill without an environment can be entered');
  } else {
    await eventually(async () => (await root.locator('.tsim-cover').innerText()).includes('的环境里'), 'The page does not say the robot is in another skill\'s environment');
    await root.locator('[data-tsim-enter]').click();
    await root.locator('.tsim-stage[data-running="1"]').waitFor({timeout: 120000});
    must(await page.evaluate(() => tsim.view.robot.skill) === made.other, 'Entering the other skill did not move the robot there');
    must(await page.evaluate(() => tsim.view.robot.id) === robotId, 'Entering another skill made a second robot');
  }
  await shot('sim75-09-other-skill');

  // ---------------------------------------------------------------- 9. the list says how far the task is
  await root.locator('[data-tsim-up]').click();
  await root.locator('[data-tsim-back]').click();
  await card.waitFor();
  await eventually(async () => (await card.getAttribute('data-tsim-status')) === (physics ? 'partial' : 'ready'), 'The list does not show the task\'s progress');
  must((await card.innerText()).includes(physics ? '1 / ' : '可以直接进入'), 'The card does not count the skills with an environment');
  await shot('sim75-10-list');

  // ---------------------------------------------------------------- 10. closing keeps everything; deleting one environment is asked twice
  await card.locator('[data-tsim-open]').click();
  await root.locator('.tsim-strip [data-tsim-leave]').click();
  await eventually(async () => !(await root.locator('.tsim-strip').innerText()).includes('运行中'), 'Closing the simulation from the task page did not work');
  if (physics) {
    await skillCard(made.entry).locator('[data-tsim-skill-open]').click();
    await root.locator('[data-tsim-remove]').click();
    await root.locator('[data-tsim-remove-yes]').click();
    await root.locator('[data-tsim-form]').waitFor();
    must(await page.evaluate(id => state.robots.some(r => r.id === id), robotId), 'Deleting one skill\'s environment took the task\'s robot');
  }

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(JSON.stringify({ok: true, engine: engine.name(), profile: made.profile.id, physics}));
})().catch(error => { console.error(error); process.exit(1); });
