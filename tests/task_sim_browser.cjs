const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #66: 仿真试运行 starts from the tasks; a task's environment is built once
// and saved; entering starts the task's simulated robot in it, with the skills of its applications.
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

  await page.goto(cfg.url + '/#robots');
  const secret = 'ts' + Math.random().toString(36).slice(2, 10) + '66x';
  await gateSignIn(page, {name: '任务仿真', email: `sim66-${Date.now()}@example.test`, password: secret});

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
    await refresh(true);
    return {profile, task, bare, product: robot.product, helper: robot.id, entry};
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
  must(text.includes('仓库巡检') && text.includes(made.product.model) && text.includes('巡检应用'), 'The card does not say what the task is for: ' + text);
  must(await card.getAttribute('data-tsim-status') === (physics ? 'none' : 'ready'), 'A new task does not start as not built');
  const blocked = root.locator(`[data-tsim-task="${made.bare.id}"]`);
  must(await blocked.getAttribute('data-tsim-status') === 'blocked' && (await blocked.innerText()).includes('绑定一个应用'), 'A task without an application does not say what is missing');
  must(await page.locator('#main').innerText().then(t => !t.includes('仿真任务') && !t.includes('场景资产')), 'The old sections are still on the page');
  await shot('sim66-01-tasks');

  // ---------------------------------------------------------------- 2. choose the task, build once
  await card.locator('[data-tsim-open]').click();
  await root.locator('.tsim-head h1').waitFor();
  must(await root.locator('.tsim-head h1').innerText() === '仓库巡检', 'The task page is not titled with the task');
  if (physics) {
    const form = root.locator('[data-tsim-form]');
    await form.waitFor();
    await form.locator('[type=submit]').click();                        // the agent needs consent: refused in place
    await eventually(async () => (await form.locator('.form-error').innerText()).includes('同意'), 'Building with the agent without consent is not refused in the form');
    await shot('sim66-02-build-form');
    await root.locator('[data-tsim-empty]').click();
    await root.locator('.tsim-saved').waitFor({timeout: 180000});
    must((await root.locator('.tsim-saved').innerText()).includes('空场地'), 'The saved environment does not say where it came from');
    await eventually(() => root.locator('.tsim-preview').evaluate(img => img.complete && img.naturalWidth > 0).catch(() => false), 'The preview of the saved environment did not load');
  }
  await root.locator('[data-tsim-enter]').waitFor();
  must(await root.locator('.tsim-skill [data-tsim-run]').isDisabled(), 'A skill can be run before the simulation is entered');
  await shot('sim66-03-saved');

  // ---------------------------------------------------------------- 3. enter: the robot, the picture, the skill
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
  const skill = root.locator(`[data-tsim-skill="${made.entry}"]`);
  await eventually(async () => (await skill.innerText()).includes('已安装'), 'The skill of the task\'s application was not installed on entering');
  must((await skill.innerText()).includes('第 1 步') && (made.entry !== 'patrol' || (await skill.innerText()).includes('loops = 1')), 'The skill does not show the values of its step');
  await eventually(async () => !(await skill.locator('[data-tsim-run]').isDisabled()), 'The skill cannot be run after entering');
  await shot('sim66-04-entered');

  // ---------------------------------------------------------------- 4. run the skill and watch it on the page
  await skill.locator('[data-tsim-run]').click();
  const run = page.locator('#dialog[open] form[data-form="sim-run"]');
  await run.waitFor();
  must((await run.innerText()).includes('第 1 步'), 'The run form does not say which step its values come from');
  await run.locator('[name=hardware_authorized]').check();
  await run.locator('[type=submit]').click();
  await page.locator('#dialog[open]').waitFor({state: 'detached'}).catch(() => {});
  must(!(await page.locator('#dialog').evaluate(d => d.open)), 'The person was taken away from the page after starting the run');
  await eventually(async () => /正在运行|已完成/.test(await root.innerText()), 'The run does not show on the page');
  await shot('sim66-05-running');
  await eventually(async () => (await root.locator('.tsim-run').innerText().catch(() => '')).includes('已完成'), 'The skill did not finish in the simulation', 120000);
  await shot('sim66-06-finished');

  // ---------------------------------------------------------------- 5. stop latches; the page says so
  await root.locator('[data-action="simlive-stop"]').click();
  await eventually(async () => (await root.locator('.tsim-line').innerText()).includes('急停已锁存'), 'Stopping does not show as a latched e-stop');
  must(await root.locator('[data-tsim-reset]').isVisible(), 'No way to release the e-stop from the page');
  must(await skill.locator('[data-tsim-run]').isDisabled(), 'A skill can be run while the e-stop is latched');

  // ---------------------------------------------------------------- 6. saved: after a reload nothing is built again
  await page.reload();
  await root.locator('.tsim-stage[data-running="1"]').waitFor();
  must(await root.locator('[data-tsim-form]').count() === 0, 'The build form is back after a reload');
  await page.setViewportSize({width: 375, height: 800});
  must(await page.evaluate(() => { const m = document.querySelector('#content-scroll'); return m.scrollWidth <= m.clientWidth + 1; }), 'The task page overflows at 375 px');
  await shot('sim66-07-narrow');
  await page.setViewportSize({width: 1440, height: 900});
  await root.locator('[data-tsim-back]').click();
  await card.waitFor();
  await eventually(async () => (await card.getAttribute('data-tsim-status')) === 'saved', 'The list does not show the task as saved');
  must((await card.locator('[data-tsim-open]').innerText()) === '进入仿真', 'A saved task does not offer 进入仿真');
  await shot('sim66-08-list-saved');

  // ---------------------------------------------------------------- 7. closing keeps everything; deleting is asked twice
  await card.locator('[data-tsim-open]').click();
  await root.locator('[data-tsim-leave]').click();
  await root.locator('[data-tsim-enter]').waitFor();
  if (physics) {
    await root.locator('[data-tsim-remove]').click();
    await root.locator('[data-tsim-remove-yes]').click();
    await root.locator('[data-tsim-form]').waitFor();
    must(await page.evaluate(id => !state.robots.some(r => r.id === id), robotId), 'Deleting the environment left its robot');
  }

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(JSON.stringify({ok: true, engine: engine.name(), profile: made.profile.id, physics}));
})().catch(error => { console.error(error); process.exit(1); });
