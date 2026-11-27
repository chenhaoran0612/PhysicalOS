const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #65: in 「添加仿真机器人」 the type 「其他」 has the Microduck, and the dialog
// keeps its place and size whatever type is chosen (it used to get shorter for a type without a picture
// and jump, which the desktop app showed as a flicker).
// Start tests/duck_browser_fixture.py first (a made-up duck kit).
// Usage: node tests/sim_other_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
const playwright = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts';
const engine = process.env.BRAIN_BROWSER === 'webkit' ? playwright.webkit : playwright.chromium;
const DUCK = 'microduck-adp102';

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
  // Where the dialog and its parts are, to the pixel.
  const placed = () => page.evaluate(() => ['#dialog', '#dialog .dialog-head', '#dialog .dialog-footer', '#dialog .sp-preview'].map(sel => {
    const r = document.querySelector(sel).getBoundingClientRect();
    return [r.x, r.y, r.width, r.height].map(Math.round).join(',');
  }).join(' | '));
  // A real click in the middle of the tile, as a person does it.
  const clickType = async id => {
    const box = await form.locator(`label.sp-type:has(input[value="${id}"])`).boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForFunction(type => document.querySelector('#dialog [name=sim_type]:checked')?.value === type, id);
  };

  await page.goto(cfg.url + '/#robots');
  const secret = 'so' + Math.random().toString(36).slice(2, 10) + '65x';
  await gateSignIn(page, {name: '其他类型', email: `sim65-${Date.now()}@example.test`, password: secret});
  await page.locator('.robot-entry').first().waitFor();
  const catalog = await page.evaluate(() => state.sim_catalog);
  const duck = catalog.profiles.find(p => p.id === DUCK);
  must(duck && duck.type === 'other' && duck.available, 'The state does not list the duck under 其他 as one that can be added: ' + JSON.stringify(duck));
  must(catalog.profiles.filter(p => p.type === 'biped').every(p => p.vendor !== 'Microduck'), 'The duck is still listed with the humanoids');

  // ---------------------------------------------------------------- 1. the dialog keeps its place and size
  await page.locator('[data-action="sim-add"]').first().click();
  await form.waitFor();
  await form.locator('.sp-preview img').evaluate(img => img.complete || new Promise(done => { img.onload = img.onerror = done; }));
  const before = await placed();
  await shot('sim65-01-dialog');
  for (const type of ['other', 'manipulator', 'biped', 'wheel_legged', 'other', 'quadruped']) {
    await clickType(type);
    const now = await placed();
    must(now === before, `The dialog moved or changed size when 「${type}」 was chosen:\n  before ${before}\n  now    ${now}`);
  }

  // ---------------------------------------------------------------- 2. 「其他」 has the duck
  await clickType('other');
  const tile = await form.locator('label.sp-type:has(input[value="other"])').innerText();
  must(tile.includes('其他') && tile.includes('1 款'), 'The tile 其他 does not count the duck: ' + tile);
  const now = await chosen();
  must(now.vendor === 'Microduck' && now.profile === DUCK && !now.disabled, 'Choosing 其他 does not bring the duck: ' + JSON.stringify(now));
  const preview = form.locator('.sp-preview');
  must(await preview.locator('img').evaluate(img => img.complete && img.naturalWidth > 0 && img.src.includes('microduck-adp102')), 'The picture of the duck did not load');
  const said = await preview.innerText();
  must(said.includes('Microduck · AD-P102') && !said.includes('URDF'), 'The preview does not describe the duck: ' + said);
  await shot('sim65-02-other');

  // ---------------------------------------------------------------- 3. a type nothing of which can be added: same size
  // The catalogue is changed, the type chosen and everything read in one step of the page: a refresh of the
  // state cannot come in between.
  const none = await page.evaluate(id => {
    const profile = state.sim_catalog.profiles.find(p => p.id === id), form = document.querySelector('#dialog form[data-form="sim-add"]');
    const choose = type => { const radio = form.querySelector(`[name=sim_type][value=${type}]`); radio.checked = true; radio.dispatchEvent(new Event('change', {bubbles: true})); };
    const rects = () => ['#dialog', '#dialog .dialog-head', '#dialog .dialog-footer', '#dialog .sp-preview'].map(sel => {
      const r = document.querySelector(sel).getBoundingClientRect();
      return [r.x, r.y, r.width, r.height].map(Math.round).join(',');
    }).join(' | ');
    Object.assign(profile, {available: false, problem: '这台电脑上没有它的技能模型包'});
    choose('quadruped'); choose('other');
    const out = {disabled: form.querySelector('button[type=submit]').disabled, said: form.querySelector('.sp-preview').innerText,
      picture: Boolean(form.querySelector('.sp-preview img')), placed: rects()};
    Object.assign(profile, {available: true, problem: ''});
    choose('quadruped'); choose('other');
    return out;
  }, DUCK);
  must(none.disabled, 'A model that cannot be added can be submitted');
  must(none.said.includes('现在不能添加') && !none.picture, 'A brand whose model cannot be added does not say so: ' + none.said);
  must(none.placed === before, `The dialog changed size for a type without a picture:\n  before ${before}\n  now    ${none.placed}`);
  must((await chosen()).profile === DUCK, 'The duck is not chosen again');

  // ---------------------------------------------------------------- 4. the duck is added as a robot of the type 其他
  await form.locator('button[type=submit]').click();
  await page.waitForFunction(id => (state.robots || []).some(r => r.sim && r.status === 'connected' && r.product?.vendor === 'Microduck'), DUCK, {timeout: 120000});
  const robot = await page.evaluate(() => state.robots.find(r => r.product?.vendor === 'Microduck'));
  must(robot.family === 'other', 'The simulated duck is not of the type 其他: ' + robot.family);
  await shot('sim65-04-added');

  must(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('sim_other_browser: passed');
})().catch(error => { console.error(error); process.exit(1); });
