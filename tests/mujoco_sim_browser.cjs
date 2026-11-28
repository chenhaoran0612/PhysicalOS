// Issue #51 browser acceptance: the library belongs to brands and models, the robot model is kept apart,
// and an application is simulated with MuJoCo.
//   1 · 资料库 starts from the brand and model (issue #77: chosen on top; robot models are in its list).
//   2 · A robot model (a .zip with a URDF and a mesh) is uploaded for the brand and model (用途 机器人模型), read
//       by the cloud, checked and confirmed by a person: its row says 仿真使用中.
//   3 · A document is uploaded by choosing the brand and model (no typing); both are listed together.
//   4 · The application's checklist opens 仿真环境: an empty environment is built, previewed and confirmed,
//       and a simulated robot of the application's brand and model starts in it, with a live picture.
//   5 · The engineering assistant writes an environment from the application; its objects are listed for review.
// Fixture: tests/mujoco_sim_browser_fixture.py (real cloud app on a temp directory, scripted assistant,
// the real edge runtime with the MuJoCo body; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// Usage: node tests/mujoco_sim_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const fs = require('fs');
const path = require('path');

const PRODUCT = 'Acme Robotics Go-7';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-mujoco-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `mujoco-sim-${engine.name()}-${name}.png`)});
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const fixtureFile = async name => Buffer.from(await (await page.request.get(cfg.url + '/fixture/' + name)).body());
  const loaded = locator => locator.evaluate(img => img.complete && img.naturalWidth > 0);
  const eventually = async (check, what, ms = 30000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (await check()) return;
      await page.waitForTimeout(250);
    }
    throw new Error('Timed out: ' + what);
  };

  try {
    await page.goto(cfg.url);
    await gateSignIn(page, {email: cfg.email, password: cfg.password, register: false});
    await page.evaluate(async c => {
      await api('cloud/configure', {base_url: c.cloud});
      await api('cloud/login', {email: c.cloud_email, password: c.cloud_password});
      await refresh(true);
    }, cfg);
    await page.locator('#navigation a[href="#library"]').click();
    await main.locator('h1', {hasText: '资料库'}).waitFor();

    // 1 · Issue #77: the page starts from the brand and model (the only one is chosen); no section of its own
    //     for robot models; above the list it says that a simulation has no model yet.
    const chip = main.locator('[data-ml-action="product"][aria-pressed="true"]', {hasText: PRODUCT});
    await chip.waitFor();
    must((await main.locator('h2', {hasText: PRODUCT + ' 的资料'}).count()) === 1, 'The list is not the chosen brand and model\'s');
    must(!(await main.innerText()).includes('机器人模型（URDF）') && await main.locator('.ml-model-row').count() === 0, 'Robot models still have a section on top');
    const note = main.locator('#ml-model-note');
    await note.locator('[data-ml-action="upload-model"]').waitFor();
    must((await note.innerText()).includes('还没有可用的机器人模型'), 'It does not say that the simulation has no model: ' + await note.innerText());
    await shot('1-library-empty');

    // 2 · Upload the robot model through the one upload form (用途 机器人模型), confirm what was read.
    await note.locator('[data-ml-action="upload-model"]').click();
    const upload = dialog.locator('form[data-library-form="upload"][data-ml-upload]');
    await upload.waitFor();
    must(await upload.locator('input[name=purpose][value=model]').isChecked(), 'The form is not for a robot model');
    const accepted = (await upload.locator('input[type=file]').getAttribute('accept')).split(',');
    must(['.urdf', '.xml', '.sdf', '.usd', '.stp'].every(e => accepted.includes(e)), 'The chooser does not offer every model format: ' + accepted);
    must(!(await upload.locator('input[type=file]').evaluate(i => i.multiple)), 'A robot model is one file');
    must(await upload.locator('[data-ml-typed]').isHidden(), 'The body type is asked although the robot has one');
    // issue #61: the brand and model are two boxes, filled in with those of the robot
    must(`${await upload.locator('[name=vendor]').inputValue()} ${await upload.locator('[name=model]').inputValue()}` === PRODUCT, 'The brand and model is not filled in');
    await shot('2-upload-form');
    await upload.locator('input[type=file]').setInputFiles({name: 'notes.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4')});
    await upload.locator('[name=version]').fill('1.0');
    await upload.locator('[type=submit]').click();
    must((await upload.locator('.form-error').innerText()).includes('不能作为机器人模型'), 'A PDF was not turned away as a robot model');
    await upload.locator('input[type=file]').setInputFiles({name: 'demo_dog.zip', mimeType: 'application/zip', buffer: await fixtureFile('dog.zip')});
    await upload.locator('[type=submit]').click();
    await main.locator('.material-upload.good', {hasText: 'demo_dog.zip'}).waitFor({timeout: 30000});
    const file = page.locator('#material-list tbody tr', {hasText: 'demo_dog.zip'});
    await file.waitFor({timeout: 30000});
    must((await file.innerText()).includes('机器人模型'), 'The robot model is not listed as one: ' + await file.innerText());
    await eventually(async () => {
      if ((await file.innerText()).includes('待确认')) return true;
      await main.locator('.page-head [data-library-action="refresh"]').click();
      await page.waitForTimeout(700);
      return false;
    }, 'the robot model was not read by the cloud');
    await shot('2-model-parsed');
    await file.locator('button', {hasText: '解析结果'}).click();
    await dialog.locator('.mp-result, .mp-panel, form').first().waitFor();
    const joints = await dialog.innerText();
    must(joints.includes('fl_thigh_joint') && joints.includes('demo_dog'), 'The parsed joints of the robot model are not shown: ' + joints.slice(0, 300));
    await dialog.locator('button', {hasText: '确认可用'}).first().click();
    await eventually(async () => (await dialog.innerText()).includes('可用') && !(await dialog.innerText()).includes('待确认'), 'the robot model was not confirmed');
    await dialog.locator('.close-dialog').click();
    await main.locator('.page-head [data-library-action="refresh"]').click();
    await eventually(async () => (await file.innerText()).includes('仿真使用中'), 'the confirmed model does not say a simulation uses it', 15000);
    await note.locator('.ml-model-in-use', {hasText: 'demo_dog.zip'}).waitFor();
    await shot('2-model-in-use');

    // 3 · A document for the same brand and model: chosen, not typed; listed with the model.
    await main.locator('.page-head [data-library-action="upload"]').click();
    const doc = dialog.locator('form[data-library-form="upload"]');
    await doc.waitFor();
    must(await doc.locator('input[name=purpose][value=document]').isChecked(), 'The upload is not for a document by default');
    await doc.locator('input[type=file]').setInputFiles({name: '说明.md', mimeType: 'text/markdown', buffer: Buffer.from('# 驱动说明\n')});
    await doc.locator('[name=version]').fill('1.0');
    await doc.locator('[type=submit]').click();
    const docRow = page.locator('#material-list tbody tr', {hasText: '说明.md'});
    await docRow.waitFor({timeout: 30000});
    must((await docRow.innerText()).includes('Acme Robotics / Go-7'), 'The document is not labelled with the chosen brand and model: ' + await docRow.innerText());
    must(await docRow.locator('[data-ml-action="move"]').count() === 1, 'A material cannot be moved to another brand and model');
    await eventually(async () => await page.locator('#material-list tbody tr').count() === 2, 'the model and the document are not listed together');
    await eventually(async () => (await chip.innerText()).includes('2 份资料'), 'the brand and model does not count what was just stored');
    await shot('3-library');

    // 4 · The application's simulation: empty environment, preview, confirmation, robot, picture.
    await page.evaluate(id => deliveryOpen(id), cfg.application_id);
    await dialog.locator('.mj-entry [data-mj-action="open"]').click();
    const sim = dialog.locator('#mj-body');
    await sim.locator('.mj-meta', {hasText: 'demo_dog.zip'}).waitFor();
    must((await sim.locator('.mj-meta').innerText()).includes(PRODUCT), 'The dialog does not name the brand and model');
    await sim.locator('[data-mj-action="empty"]').click();
    const scene = sim.locator('.mj-scene').first();
    await sim.locator('.mj-scene[data-mj-status="awaiting_review"]').waitFor({timeout: 90000});
    await eventually(() => loaded(scene.locator('.mj-preview')), 'the preview picture did not load');
    must((await scene.innerText()).includes('机器人从 (0, 0) 出发'), 'The environment does not say what is in it: ' + await scene.innerText());
    await shot('4-scene-to-confirm');
    await scene.locator('[data-mj-action="confirm"]').click();
    await sim.locator('.mj-scene[data-mj-status="confirmed"]').waitFor();
    await scene.locator('[data-mj-action="start"]').click();
    await dialog.locator('#mj-frame').waitFor({state: 'attached', timeout: 90000});
    await eventually(() => loaded(dialog.locator('#mj-frame')), 'the live picture of the simulation did not appear', 40000);
    must(await dialog.locator('#mj-frame').evaluate(img => img.naturalWidth === 480 && img.naturalHeight === 360), 'The live picture has an unexpected size');
    await eventually(async () => (await dialog.locator('#mj-touch').innerText()).includes('没有碰到环境里的东西'), 'the simulation does not say what the robot touches');
    must((await dialog.innerText()).includes('仿真'), 'The live dialog does not mark the robot as simulated');
    await shot('4-live');
    const robots = await page.evaluate(() => state.robots.filter(r => r.sim?.kind === 'mujoco').map(r => ({name: r.name, product: r.product, status: r.status})));
    must(robots.length === 1 && robots[0].status === 'connected' && robots[0].product.vendor === 'Acme Robotics' && robots[0].product.model === 'Go-7',
      'The simulated robot is not one of the application\'s brand and model: ' + JSON.stringify(robots));
    await dialog.locator('.close-dialog').click();

    // 5 · The assistant writes an environment for the application.
    await page.evaluate(id => deliveryOpen(id), cfg.application_id);
    await dialog.locator('.mj-entry [data-mj-action="open"]').click();
    await sim.locator('.mj-scene').first().waitFor();
    must((await sim.locator('.mj-robot').innerText()).includes('Acme Robotics Go-7 仿真'), 'The running robot is not listed with its environment');
    await sim.locator('[data-mj-action="form-open"]').click();
    const form = sim.locator('form[data-mj-form="generate"]');
    await form.locator('[name=note]').fill('墙在东边');
    await form.locator('[type=submit]').click();
    must(await form.locator('[name=data_authorized]').evaluate(el => !el.validity.valid), 'The assistant could be started without consent');
    await form.locator('[name=data_authorized]').check();
    await form.locator('[type=submit]').click();
    const written = sim.locator('.mj-scene[data-mj-status="awaiting_review"]', {hasText: '工程助手生成'});
    await written.waitFor({timeout: 120000});
    const text = await written.innerText();
    must(text.includes('墙：方块') && text.includes('箱子') && text.includes('地点 门口'), 'The written environment is not listed for review: ' + text);
    await eventually(() => loaded(written.locator('.mj-preview')), 'the preview of the written environment did not load');
    await shot('5-written-scene');

    must(!errors.length, 'Page errors: ' + errors.join(' | '));
    console.log('mujoco_sim_browser: PASS (' + engine.name() + ')');
  } catch (err) {
    await shot('failure').catch(() => {});
    console.error('mujoco_sim_browser: FAIL (' + engine.name() + '): ' + err.message);
    if (errors.length) console.error('page errors: ' + errors.join(' | '));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
