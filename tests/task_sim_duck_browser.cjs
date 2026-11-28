const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #75 with the Microduck: in 仿真试运行 every skill model of the duck is its own environment
// (the stage it was trained on), not all of them on one stage. Entering a model puts the task's duck on that model's
// stage; running it goes through the runtime like any skill; entering another model moves the same duck to its stage.
// Start tests/duck_browser_fixture.py first (a made-up kit; NX_DUCK_REAL_KIT=1 for the real one).
// Usage: node tests/task_sim_duck_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  const shot = name => page.screenshot({path: path.join(shots, name + '.png')});
  const must = (ok, message) => { if (!ok) throw Error(message); };
  const eventually = async (check, what, timeout = 30000) => {
    const until = Date.now() + timeout;
    for (;;) {
      if (await check().catch(() => false)) return;
      if (Date.now() > until) throw Error(what);
      await page.waitForTimeout(250);
    }
  };
  const root = page.locator('.tsim');

  await page.goto(cfg.url + '/#robots');
  const secret = 'dk' + Math.random().toString(36).slice(2, 10) + '75x';
  await gateSignIn(page, {name: '小鸭仿真', email: `duck75-${Date.now()}@example.test`, password: secret});

  // ---------------------------------------------------------------- data: a task whose application performs the first skill model
  const made = await page.evaluate(async () => {
    const kit = state.duck_kit;
    const shows = kit.shows.filter(s => s.packaged && s.id !== 'fall').slice(0, 2);
    const duck = await api('sims', {profile_id: kit.profile_id});          // a duck of the brand and model, to make the application for
    await refresh(true);
    const task = await api('projects', {name: '小黄鸭能力展示', objective: '让小鸭表演它学会的动作'});
    const skill = await api('skills', {project_id: task.id, name: shows[0].title, version: '1.0', requires: ['policy.run'], entrypoint: 'duck.' + shows[0].id,
      success_condition: '做完', stop_condition: '超时', model_ids: [], robot_id: duck.id});
    const app = await api('applications', {project_id: task.id, name: '表演', version: '1.0', robot_id: duck.id,
      steps: [{skill_id: skill.id, parameters: {}, timeout_s: 120}]});
    await api('task-apps/bind', {project_id: task.id, application_id: app.id});
    await refresh(true);
    return {task, helper: duck.id, shows: shows.map(s => ({id: 'duck.' + s.id, show: s.id, title: s.title}))};
  });
  const [first, second] = made.shows;
  must(first && second, 'The kit has fewer than two skill models: ' + JSON.stringify(made.shows));

  // ---------------------------------------------------------------- 1. the task: every skill model is its own environment
  await page.locator('#navigation a[href="#simulation"]').click();
  const card = root.locator(`[data-tsim-task="${made.task.id}"]`);
  await card.waitFor();
  must(await card.getAttribute('data-tsim-status') === 'ready', 'A duck task does not say it can be entered directly');
  await card.locator('[data-tsim-open]').click();
  const skillCard = id => root.locator(`[data-tsim-skill-card="${id}"]`);
  await skillCard(first.id).waitFor();
  for (const one of made.shows) {
    must(await skillCard(one.id).getAttribute('data-tsim-status') === 'ready', `「${one.title}」 is not its own environment`);
    must((await skillCard(one.id).innerText()).includes('自带场地'), `「${one.title}」 does not say it brings its own stage`);
  }
  must(await root.locator('.tsim-skill-card').first().getAttribute('data-tsim-skill-card') === first.id, 'The skill model the application uses is not first');
  must(await root.locator('[data-tsim-skill-card^="duck."]').count() >= 2, 'The duck\'s skill models are not listed one by one');
  await shot('duck75-01-skills');

  // ---------------------------------------------------------------- 2. one skill model: its stage, entered
  await skillCard(first.id).locator('[data-tsim-skill-open]').click();
  await eventually(async () => (await root.locator('.tsim-head h1').innerText()) === first.title, 'The skill model\'s page is not titled with it');
  must((await root.locator('.tsim-saved').innerText()).includes('训练时用的场地'), 'The environment does not say it is the stage the model was trained on');
  must(await root.locator('[data-tsim-form]').count() === 0, 'A skill model of the duck offers to build an environment');
  await root.locator('[data-tsim-enter]').click();
  await root.locator('.tsim-stage[data-running="1"]').waitFor({timeout: 180000});
  const robotId = await page.evaluate(() => tsim.view.robot.id);
  must(robotId !== made.helper, 'The task did not get a duck of its own');
  await eventually(async () => (await page.evaluate(id => api('sims/duck/status', {robot_id: id}), robotId)).stage === first.show,
    `The duck is not on the stage of 「${first.title}」`, 120000);
  await eventually(() => root.locator('.tsim-frame').evaluate(img => !img.hidden && img.naturalWidth > 0), 'No live picture of the duck', 60000);
  const panel = root.locator(`.tsim-skill[data-tsim-skill="${first.id}"]`);
  await eventually(async () => !(await panel.locator('[data-tsim-run]').isDisabled()), 'The skill model cannot be run after entering', 60000);
  await shot('duck75-02-entered');

  // ---------------------------------------------------------------- 3. run it like any skill: an operation with a run record
  await panel.locator('[data-tsim-run]').click();
  const run = page.locator('#dialog[open] form[data-form="sim-run"]');
  await run.waitFor();
  await run.locator('[name=hardware_authorized]').check();
  await run.locator('[type=submit]').click();
  await page.locator('#dialog[open]').waitFor({state: 'detached'}).catch(() => {});
  await eventually(async () => (await root.locator('.tsim-run').innerText()).includes('已完成'), 'The skill model did not finish', 120000);
  await eventually(async () => (await page.locator('#main [data-rb-sim] tr.rb-row').count()) === 1, 'The run is not listed under this skill model');
  await shot('duck75-03-performed');

  // ---------------------------------------------------------------- 4. another skill model: the same duck moves to its stage
  await root.locator('[data-tsim-up]').click();
  await skillCard(second.id).waitFor();
  must((await skillCard(first.id).innerText()).includes('仿真机器人在这里'), 'The card of the model the duck is on does not say so');
  await skillCard(second.id).locator('[data-tsim-skill-open]').click();
  await eventually(async () => (await root.locator('.tsim-cover').innerText()).includes(`在「${first.title}」的环境里`),
    'The page does not say the duck is on another model\'s stage');
  must(await page.locator('#main [data-rb-sim] tr.rb-row').count() === 0, 'The other model\'s list of runs shows a run of the first one');
  await root.locator('[data-tsim-enter]').click();
  await root.locator('.tsim-stage[data-running="1"]').waitFor({timeout: 120000});
  must(await page.evaluate(() => tsim.view.robot.id) === robotId, 'Entering another skill model made a second duck');
  await eventually(async () => (await page.evaluate(id => api('sims/duck/status', {robot_id: id}), robotId)).stage === second.show,
    `The duck did not move to the stage of 「${second.title}」`, 120000);
  await eventually(async () => !(await root.locator('.tsim-line').innerText()).includes('正在换到'), 'The page still says the duck is moving', 60000);
  await shot('duck75-04-other-stage');

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(JSON.stringify({ok: true, engine: engine.name(), shows: made.shows.map(s => s.show)}));
})().catch(error => { console.error(error); process.exit(1); });
