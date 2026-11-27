const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #67: a model trained in simulation is a skill of its robot. A simulated
// duck comes with every skill model installed as a skill; the skill is run from the robot's own skill
// list through the ordinary run form; the skills are listed for the brand and model.
// Start tests/duck_browser_fixture.py first (a made-up kit; NX_DUCK_REAL_KIT=1 for the real one).
// Usage: node tests/duck_skills_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  const shot = name => page.screenshot({path: path.join(shots, name + '.png'), fullPage: true});
  const must = (ok, message) => { if (!ok) throw Error(message); };

  await page.goto(cfg.url + '/#training');
  const secret = 'dk' + Math.random().toString(36).slice(2, 10) + '67x';
  await gateSignIn(page, {name: '小鸭', email: `duck67-${Date.now()}@example.test`, password: secret});
  const block = page.locator('.duck');
  await block.waitFor();
  const kit = await page.evaluate(() => state.duck_kit);
  const packaged = kit.shows.filter(s => s.packaged);
  const first = packaged[0];
  must(packaged.every(s => s.skill && s.skill.id === 'duck.' + s.id), 'The state does not say which skill a model is');
  must((await block.locator('.duck-head').innerText()).includes('都是这个品牌型号的技能'), 'The head does not say the models are skills');

  // ---- before a duck is there: the skill is named, and it will come with the duck
  const skillLine = id => block.locator(`[data-duck-show="${id}"] .duck-skill`);
  must((await skillLine(first.id).innerText()).includes('添加仿真小鸭时自动装上'), 'Without a duck the row does not say when the skill is installed');
  must(await block.locator('.duck-skill').count() === packaged.length, 'A model without a package has a skill line, or one with a package has none');

  // ---- add the duck: every model is installed on it as a skill
  await block.locator('[data-duck-add]').click();
  await page.waitForFunction(id => {
    const el = document.querySelector(`[data-duck-show="${id}"] .duck-skill`);
    return el && el.dataset.tone === 'good';
  }, first.id, {timeout: 120000});
  for (const one of packaged) {
    const text = await skillLine(one.id).innerText();
    must(text.includes('duck.' + one.id) && text.includes('已装在这个仿真小鸭上'), `「${one.title}」 is not shown as installed: ${text}`);
  }
  await shot('duck-skills-01-training');
  const robot = await page.evaluate(() => duckRobots(state.robots)[0]);

  // ---- the robot's own skill list has them, each with 「运行」
  await page.evaluate(() => { location.hash = 'robots'; });
  await page.locator('#main img[src*="microduck-adp102"]').first().waitFor();
  await page.evaluate(id => robotDetailOpen(id), robot.id);
  const dialog = page.locator('dialog[open], .dialog').last();
  const row = page.locator('.contract-line', {hasText: `duck.${first.id}@`});
  await row.waitFor();
  must((await page.locator('#robot-skills').innerText()).includes(String(packaged.length)), 'The count of installed skills is not the number of models');
  must((await row.innerText()).includes(first.title), 'The installed skill does not carry the model\'s name');
  await shot('duck-skills-02-robot');

  // ---- run it through the ordinary form: one parameter, one confirmation
  await row.locator('[data-action="sim-run"]').click();
  const form = page.locator('form[data-form="sim-run"]');
  await form.waitFor();
  must(await form.locator('[data-param-field="seed"]').count() === 1, 'The run form has no field for the start pose');
  must((await form.locator('[data-param-field="seed"]').innerText()).includes('起姿编号'), 'The field is not named in words');
  const ticks = form.locator('input[type=checkbox]');
  must(await ticks.count() === 1, 'A simulated robot is asked for more or fewer than one confirmation');
  // A made-up model passes from any start pose; a real one is run from the pose of its accepted replay (the default).
  if (kit.shows.some(x => x.id === 'nod')) await form.locator('[data-param-field="seed"] input').fill('7');
  await ticks.first().check();
  await shot('duck-skills-03-form');
  await form.locator('button[type=submit]').click();
  const done = page.locator('.sim-run[data-run-status="succeeded"]');
  await done.waitFor({timeout: 120000});
  must((await done.innerText()).includes('duck.' + first.id), 'The finished run is not the skill that was started');
  await shot('duck-skills-04-ran');
  await page.keyboard.press('Escape');

  // ---- the stage of the training page saw the same run
  await page.evaluate(() => { location.hash = 'training'; });
  await block.waitFor();
  await page.waitForFunction(() => /完整演完/.test(document.querySelector('.duck .duck-line')?.textContent || ''), null, {timeout: 30000});

  // ---- they are skills of the brand and model
  await page.evaluate(() => { location.hash = 'app-skills'; });
  const table = page.locator('#main table');
  await table.waitFor();
  const listed = await table.innerText();
  for (const one of packaged) must(listed.includes('duck.' + one.id) && listed.includes(one.title), `「${one.title}」 is not listed as a skill`);
  must(listed.includes('Microduck') && listed.includes('AD-P102'), 'The skills are not listed for the duck\'s brand and model');
  must(listed.includes('policy.run'), 'The skills do not say which capability they need');
  await shot('duck-skills-05-skills');

  // ---- narrow screens: the skill line does not overflow
  await page.evaluate(() => { location.hash = 'training'; });
  await block.waitFor();
  for (const width of [375, 768]) {
    await page.setViewportSize({width, height: 800});
    await page.waitForTimeout(150);
    const over = await page.evaluate(() => { const b = document.querySelector('.duck').getBoundingClientRect(); return [b.right, window.innerWidth, document.documentElement.scrollWidth]; });
    must(over[0] <= over[1] + 1 && over[2] <= over[1] + 1, `The block overflows at ${width}px: ${over}`);
    await shot('duck-skills-06-' + width);
  }
  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('duck skills browser acceptance passed');
})().catch(error => { console.error(error); process.exit(1); });
