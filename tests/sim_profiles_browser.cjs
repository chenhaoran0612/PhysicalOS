const {gateSignIn} = require('./gate_helper.cjs');
const {pickSimProfile} = require('./sim_profiles_helper.cjs');
// Browser acceptance for issue #54: 「添加仿真机器人」 chooses a type, a brand and a model, and a robot of
// a built-in profile starts from the model package next to the app (no library, no cloud).
// Start tests/ux01_browser_fixture.py first (it serves the worktree; the packages are the ones
// `python scripts/robot_models.py` built under .build/robot-models, or NEUXMIND_ROBOT_MODELS_DIR).
// Usage: node tests/sim_profiles_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  require('./robots_page_helper.cjs').throughRobotsPage(page);
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  const shot = name => page.screenshot({path: path.join(shots, name + '.png')});
  const must = (ok, message) => { if (!ok) throw Error(message); };
  const form = page.locator('#dialog[open] form[data-form="sim-add"]');
  const chosen = () => form.evaluate(f => ({type: f.querySelector('[name=sim_type]:checked')?.value, vendor: f.querySelector('[name=sim_vendor]').value,
    profile: f.querySelector('[name=profile_id]').value, disabled: f.querySelector('button[type=submit]').disabled}));
  const eventually = async (check, what, timeout = 20000) => {
    const until = Date.now() + timeout;
    for (;;) {
      if (await check()) return;
      if (Date.now() > until) throw Error(what);
      await page.waitForTimeout(250);
    }
  };

  await page.goto(cfg.url + '/#robots');
  const secret = 'sp' + Math.random().toString(36).slice(2, 10) + '54x';
  await gateSignIn(page, {name: '仿真机器人', email: `sim54-${Date.now()}@example.test`, password: secret});
  await page.locator('.robot-entry').waitFor();
  const catalog = await page.evaluate(() => state.sim_catalog);
  must(catalog.types.map(t => t.name).join() === '机械臂,轮足机器狗,四足机械狗,双足机器人,其他', 'The state does not carry the five types');
  const real = catalog.profiles.filter(p => p.available && !p.builtin);

  // ---------------------------------------------------------------- 1. the dialog
  await page.locator('[data-action="sim-add"]').first().click();
  await form.waitFor();
  must(await form.locator('input[name=sim_type]').count() === 5, 'The dialog does not offer five types');
  must(await form.locator('input[name=kind]').count() === 0, 'The two old kinds are still in the dialog');
  const start = await chosen();
  must(start.type === 'quadruped' && start.profile && !start.disabled, 'The dialog does not open on a dog that can be added: ' + JSON.stringify(start));
  must(await form.locator('.sp-preview img').evaluate(img => img.complete && img.naturalWidth > 0), 'The picture of the chosen model did not load');
  await shot('sim54-01-dialog');
  // a type without a model: said, and nothing can be added
  await form.locator('input[name=sim_type][value=other]').check({force: true});
  must((await form.locator('.sp-preview').innerText()).includes('这个类型暂时没有可以仿真的型号'), 'A type without models does not say so');
  must((await chosen()).disabled, 'A type without models can still be submitted');
  // keyboard: the types are one radio group
  await form.locator('input[name=sim_type][value=other]').focus();
  await page.keyboard.press('ArrowLeft');
  must((await chosen()).type === 'biped', 'The arrow keys do not move between the types');
  await shot('sim54-02-biped');
  // every type that has a model follows through to one
  for (const type of catalog.types) {
    const some = catalog.profiles.filter(p => p.type === type.id && p.available);
    await form.locator(`input[name=sim_type][value=${type.id}]`).check({force: true});
    const now = await chosen();
    must(Boolean(now.profile) === Boolean(some.length) && now.disabled === !some.length, `Type ${type.name}: ` + JSON.stringify(now));
  }
  // narrow window: nothing is cut off
  await page.setViewportSize({width: 375, height: 800});
  must(await page.evaluate(() => { const d = document.querySelector('#dialog'); return d.scrollWidth <= d.clientWidth + 1; }), 'The dialog overflows at 375 px');
  await shot('sim54-03-narrow');
  await page.setViewportSize({width: 1440, height: 900});

  // ---------------------------------------------------------------- 2. the example robot the app always had
  await pickSimProfile(page, 'quadruped');
  await form.locator('button[type=submit]').click();
  await page.locator('#dialog[open] .cap-list').waitFor({timeout: 60000});
  await page.locator('#dialog[open] [data-action="close"]').first().click();
  let robots = await page.evaluate(() => state.robots.map(r => ({id: r.id, name: r.name, kind: r.sim?.kind, state: r.sim?.state, product: r.product})));
  must(robots.length === 1 && robots[0].kind === 'quadruped' && robots[0].state === 'running', 'The example quadruped did not start: ' + JSON.stringify(robots));

  // ---------------------------------------------------------------- 3. robots of real profiles (those whose package is here)
  if (!real.length) console.log('sim_profiles_browser: no model package on this computer, the real profiles were not started');
  for (const profile of real) {
    await page.locator('[data-action="sim-add"]').first().click();
    await pickSimProfile(page, profile.id);
    await form.locator('button[type=submit]').click();
    await page.locator('#dialog[open] .cap-list').waitFor({timeout: 300000});
    const robot = await page.evaluate(id => state.robots.map(r => ({id: r.id, name: r.name, sim: r.sim, product: r.product, status: r.status, family: r.family}))
      .find(r => r.sim?.profile_id === id || (r.sim?.kind === 'mujoco' && r.name.includes(id))) || state.robots.at(-1) && null, profile.id);
    const added = robot || await page.evaluate(([vendor, model]) => { const r = state.robots.find(x => x.product?.vendor === vendor && x.product?.model === model); return r && {id: r.id, name: r.name, sim: r.sim, product: r.product, status: r.status}; }, [profile.vendor, profile.model]);
    must(added && added.sim.kind === 'mujoco' && added.sim.state === 'running' && added.status === 'connected', `${profile.id} did not start: ` + JSON.stringify(added));
    must(added.product.vendor === profile.vendor && added.product.model === profile.model, `${profile.id} reports another brand and model: ` + JSON.stringify(added.product));
    await page.locator('#dialog[open] [data-action="close"]').first().click();
    // its live picture
    await page.evaluate(id => action('sim-live', id), added.id);
    const frame = page.locator('#dialog[open] #mj-frame');
    await frame.waitFor({state: 'attached', timeout: 90000});
    await eventually(() => frame.evaluate(img => !img.hidden && img.complete && img.naturalWidth === 480), `the live picture of ${profile.id} did not appear`, 60000);
    const mode = await page.evaluate(async id => (await api('robots/live', {robot_id: id, project_id: state.robot_home})).state?.mode, added.id);
    must(mode === 'sim:stand' || profile.type === 'manipulator', `${profile.id} is not standing: ${mode}`);
    await shot('sim54-live-' + profile.id);
    await page.locator('#dialog[open] [data-action="close"]').first().click();
    await page.evaluate(id => api('sims/stop', {robot_id: id}), added.id);        // one MuJoCo robot at a time keeps this light
    await page.evaluate(() => refresh(true));
  }
  await page.evaluate(() => { location.hash = 'robots'; return refresh(true); });      // not a second goto: WebKit reloads and reports the poll it cut off
  await page.locator('article.rp-card').first().waitFor();
  await shot('sim54-04-robots');
  must(!errors.length, 'Errors on the page: ' + errors.join(' | '));
  console.log(`sim_profiles_browser: passed (${real.length} real profiles started: ${real.map(p => p.id).join(', ') || 'none'})`);
  await browser.close();
})().catch(async e => { console.error('sim_profiles_browser FAILED:', e.message); process.exit(1); });
