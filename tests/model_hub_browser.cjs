const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #71: 模型库 (the board, the wall by category, the page of a model, uploading
// a model file), 训练与测试数据 (the two libraries), 训练与改进 (a whole round for a model of one's own with
// imported results), and an evaluation of a duck's skill model run in the simulation.
// Start tests/duck_browser_fixture.py first (a made-up kit: the shows nod, long, fall).
// Usage: node tests/model_hub_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
const playwright = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts';
const engine = process.env.BRAIN_BROWSER === 'webkit' ? playwright.webkit : playwright.chromium;

let failed = async () => {};
(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine === playwright.chromium ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  const shot = name => page.screenshot({path: path.join(shots, name + '.png'), fullPage: true});
  const must = (ok, message) => { if (!ok) throw Error(message); };
  failed = async () => { await page.screenshot({path: path.join(shots, 'hub-failed.png'), fullPage: true}); console.error((await page.locator('#main').innerText()).slice(0, 1500)); };
  const main = page.locator('#main');
  const form = kind => page.locator(`#dialog form[data-hub-form="${kind}"]`);
  const go = async hash => { await page.evaluate(to => { location.hash = to; }, hash); };
  const send = async kind => { await form(kind).locator('[type=submit]').click(); await form(kind).waitFor({state: 'hidden'}); };
  const card = name => main.locator('.mh-card', {has: page.locator('h3', {hasText: name})});
  const measures = async (kind, values) => { for (const [id, value] of Object.entries(values)) await form(kind).locator(`[name="measure:${id}"]`).fill(String(value)); };
  const file = (name, text) => ({name, mimeType: 'application/octet-stream', buffer: Buffer.from(text)});

  // ---- 模型库: the board, the wall by category, the duck's skill models
  await page.goto(cfg.url + '/#models');
  const secret = 'mh' + Math.random().toString(36).slice(2, 10) + '71x';
  await gateSignIn(page, {name: '模型', email: `hub71-${Date.now()}@example.test`, password: secret});
  await main.locator('.mh-board').waitFor();
  must(await main.locator('.page-head h1').innerText() === '模型库', 'The page is not titled 模型库');
  must(await main.locator('.page-head .eyebrow').innerText() === 'MODEL LIBRARY', 'The English line of the title is missing');
  must(await main.locator('.page-head p').count() === 0, 'The head has a sentence under the title');
  must(await main.locator('.mh-count strong').innerText() === '3', 'The board does not count the three skill models of the kit');
  must(await main.locator('.mh-cat').count() === 9, 'The board does not list the nine categories');
  must((await card('节目 nod').locator('.mh-metric strong').innerText()) === '100%', 'The card of a delivered skill model does not lead with its success rate');
  must((await card('节目 nod').locator('.mh-product').innerText()) === 'Microduck AD-P102', 'The card does not name the brand and model');
  const heights = await main.locator('.mh-wall .mh-card').evaluateAll(cards => cards.map(c => Math.round(c.getBoundingClientRect().height)));
  must(new Set(heights).size === 1, 'The cards of a wall are not one size: ' + heights);
  await shot('hub-01-library');

  // ---- a model of one's own, its page, a model file
  await main.locator('.page-head [data-action="hub-model-add"]').click();
  await form('model').locator('[name=name]').fill('走廊导航');
  await form('model').locator('[name=category]').selectOption('navigation');
  await form('model').locator('[name=vendor]').fill('NeuxMind');
  await form('model').locator('[name=model]').fill('CS1');
  await form('model').locator('[name=summary]').fill('在走廊里走到指定的点。');
  await send('model');
  await main.locator('.mh-detail').waitFor();
  must(await main.locator('h1').innerText() === '走廊导航', 'A new model does not open as its own page');
  must(await page.locator('#breadcrumb').innerText() === '模型库 / 走廊导航', 'The breadcrumb does not name the model');
  must(await page.locator('#navigation a[href="#models"]').getAttribute('aria-current') === 'page', 'The sidebar does not mark 模型库 on the page of a model');
  must((await main.locator('.mh-side').innerText()).includes('还没有模型文件'), 'A model without a file does not say so');
  const weights = 'made-up weights ' + 'x'.repeat(5000);
  await main.locator('.page-head [data-action="hub-version-add"]').click();
  await form('version').locator('[name=label]').fill('v1');
  await form('version').locator('[name=files]').setInputFiles(file('corridor-v1.onnx', weights));
  await form('version').locator('[name=note]').fill('第一次训练');
  await send('version');
  const v1 = main.locator('.mh-version', {hasText: 'corridor-v1.onnx'});
  await v1.waitFor();
  must((await v1.innerText()).includes('当前版本'), 'The first version is not the current one');
  const href = await v1.locator('a[download]').getAttribute('href');
  must(await page.evaluate(async url => (await (await fetch(url)).text()), href) === weights, 'The kept file is not what was uploaded');
  await shot('hub-02-model');
  await page.reload();
  await main.locator('.mh-detail').waitFor();
  must(await main.locator('h1').innerText() === '走廊导航', 'A reload of the page of a model leaves it');

  // ---- 训练与测试数据: the two libraries
  await go('datasets');
  await main.locator('#hub-evaluation').waitFor();
  must(await main.locator('.page-head h1').innerText() === '训练与测试数据', 'The page is not titled 训练与测试数据');
  must(await card('固定起姿组').count() === 1 && await card('独立起姿组').count() === 1, 'The start poses of the duck are not in the evaluation library');
  await main.locator('.page-head [data-action="hub-set-add"][data-id="evaluation"]').click();
  await form('set').locator('[name=name]').fill('走廊 50 点');
  must(await form('set').locator('[name=seeds]').count() === 1, 'A set of a motion model does not ask for start poses');
  await form('set').locator('[name=category]').selectOption('navigation');
  must(await form('set').locator('[name=seeds]').count() === 0 && await form('set').locator('[name=count]').count() === 1, 'A set of another category still asks for start poses');
  await form('set').locator('[name=description]').fill('三栋楼里的五十个目标点。');
  await form('set').locator('[name=count]').fill('50');
  await measures('set', {reach_rate: 90});
  await send('set');
  await card('走廊 50 点').waitFor();
  must((await card('走廊 50 点').locator('.mh-set-targets').innerText()).includes('到达成功率 ≥ 90%'), 'The set does not show its own target');
  await main.locator('.page-head [data-action="hub-set-add"][data-id="training"]').click();
  await form('set').locator('[name=name]').fill('走廊轨迹');
  await form('set').locator('[name=category]').selectOption('navigation');
  await form('set').locator('[name=description]').fill('十一月采集的轨迹。');
  await form('set').locator('[name=count]').fill('1200');
  await form('set').locator('[name=files]').setInputFiles(file('tracks.jsonl', '{"x":1}\n'));
  await send('set');
  await main.locator('#hub-training .mh-card', {hasText: '走廊轨迹'}).waitFor();
  must((await main.locator('#hub-training .mh-card', {hasText: '走廊轨迹'}).innerText()).includes('tracks.jsonl'), 'The training set does not name its file');
  await shot('hub-03-data');

  // ---- 训练与改进: one whole round with imported results
  await go('training');
  await main.locator('.mh-loop').waitFor();
  must(await main.locator('.page-head h1').innerText() === '训练与改进', 'The page is not titled 训练与改进');
  must(await main.locator('.duck').count() === 1, 'The stage of the duck is gone from 训练与改进');
  await shot('hub-04-training');
  await main.locator('.page-head [data-action="hub-pick"]').click();
  await page.locator('#dialog .mh-pick button', {hasText: '走廊导航'}).click();
  await main.locator('.mh-start').waitFor();
  must(await page.locator('#breadcrumb').innerText() === '训练与改进 / 走廊导航', 'The breadcrumb of the improvement page is wrong');
  await main.locator('.mh-start [data-action="hub-round-add"]').click();
  await form('round').locator('[name=goal]').fill('到达成功率从 80% 到 95%');
  await send('round');
  const round = main.locator('.mh-round');
  const step = () => round.locator('.mh-steps li.current').innerText();
  await round.waitFor();
  must((await step()).includes('基线评测'), 'A new round does not begin with the baseline');
  await round.locator('[data-action="hub-eval-import"]').click();
  await measures('eval-import', {reach_rate: 80, collision_rate: 4, position_error_m: 0.3});
  await form('eval-import').locator('[name=source]').fill('训练机上的评测报告');
  await send('eval-import');
  await round.locator('[data-action="hub-diagnose"]').waitFor();
  must((await round.locator('.mh-round-body').innerText()).includes('未达标'), 'The baseline is not judged against the targets');
  await round.locator('[data-action="hub-diagnose"]').click();
  await form('diagnose').locator('[name=weaknesses]').fill('转角处定位漂移');
  await form('diagnose').locator('[name=plan]').fill('加入转角轨迹重新训练');
  await form('diagnose').locator('[name=training_set]').check();
  await send('diagnose');
  await round.locator('[data-action="hub-version-add"]').waitFor();
  must((await step()).includes('训练'), 'The round is not at the training step');
  must((await round.locator('.mh-round-body').innerText()).includes('走廊轨迹'), 'The round does not name its training set');
  await shot('hub-05-round-training');
  await round.locator('[data-action="hub-version-add"]').click();
  await form('version').locator('[name=label]').fill('v2');
  await form('version').locator('[name=files]').setInputFiles(file('corridor-v2.onnx', 'second weights'));
  await send('version');
  await round.locator('[data-action="hub-eval-import"]').waitFor();
  must((await step()).includes('评测候选'), 'An uploaded file does not become the candidate of the round');
  await round.locator('[data-action="hub-eval-import"]').click();
  await measures('eval-import', {reach_rate: 96, collision_rate: 0, position_error_m: 0.15});
  await form('eval-import').locator('[name=source]').fill('训练机上的评测报告');
  await send('eval-import');
  await round.locator('[data-action="hub-round-accept"]').waitFor();
  must((await round.locator('.mh-verdict').innerText()) === '主指标变好，并且达标。', 'The review does not say how the candidate compares');
  must((await round.locator('tr.mh-better').first().innerText()).includes('+16%'), 'The comparison does not show the change');
  await shot('hub-06-round-review');
  await round.locator('[data-action="hub-round-accept"]').click();
  await send('round-accept');
  await main.locator('.mh-past.accepted').waitFor();
  must((await main.locator('.mh-past.accepted').innerText()).includes('80% → 96%'), 'The closed round does not show where it went');
  await main.locator('.page-head [data-action="hub-open"]').click();
  await main.locator('.mh-detail').waitFor();
  must((await main.locator('.mh-version', {hasText: 'corridor-v2.onnx'}).innerText()).includes('当前版本'), 'The accepted candidate is not the current version');
  must((await main.locator('.mh-side').innerText()).includes('已达标'), 'The model is not shown as meeting its targets');

  // ---- a duck's skill model is evaluated in the simulation, once per start pose
  const evaluate = async (name, passed, verdict) => {
    await go('models');
    await card(name).locator('[data-action="hub-open"]').click();
    await main.locator('.mh-detail').waitFor();
    await main.locator('[data-action="hub-eval-run"]').click();
    await form('eval-run').waitFor();
    if (await form('eval-run').locator('[data-action="hub-duck-add"]').count()) {
      await form('eval-run').locator('[data-action="hub-duck-add"]').click();
      await page.waitForFunction(() => hubDucks(state).length > 0, null, {timeout: 90000});
      await main.locator('[data-action="hub-eval-run"]').click();
    }
    await form('eval-run').locator('[name=robot_id]').waitFor({state: 'attached'});
    await form('eval-run').locator('[name=set_id]').selectOption('hubs-duck-fixed');
    await send('eval-run');
    const row = main.locator('.mh-evals tr', {hasText: '仿真里运行'}).first();
    await row.waitFor();
    await page.waitForFunction(text => [...document.querySelectorAll('.mh-evals tr')].some(r => r.innerText.includes('仿真里运行') && r.innerText.includes(text) && !r.innerText.includes('正在评测')), `${passed} / 10`, {timeout: 120000});
    must((await row.innerText()).includes(verdict), `The evaluation of ${name} is not judged ${verdict}: ${await row.innerText()}`);
    await row.locator('[data-action="hub-eval-open"]').click();
    await page.locator('#dialog .mh-eval-detail').waitFor();
    must(await page.locator(`#dialog .mh-run.${passed ? 'pass' : 'fail'}`).count() === 10, 'The runs of the evaluation are not shown one by one');
  };
  await evaluate('节目 nod', 10, '达标');
  await shot('hub-07-evaluation');
  await page.locator('#dialog .close-dialog').click();
  await evaluate('节目 fall', 0, '未达标');
  await page.locator('#dialog .close-dialog').click();

  // ---- a category of the board filters the wall; narrow screens do not overflow
  await go('models');
  await main.locator('.mh-cat', {hasText: '导航模型'}).click();
  must(await main.locator('.mh-group').count() === 1 && await card('走廊导航').count() === 1, 'A category of the board does not filter the wall');
  await main.locator('.mh-cat', {hasText: '导航模型'}).click();
  must(await main.locator('.mh-group').count() === 2, 'Clicking the category again does not show all of them');
  for (const hash of ['models', 'training', 'datasets']) {
    await go(hash);
    await main.locator('.mh-board').waitFor();
    for (const width of [375, 768, 1024, 1440]) {
      await page.setViewportSize({width, height: 900});
      await page.waitForTimeout(150);
      const over = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
      must(over[0] <= over[1] + 1, `#${hash} overflows at ${width}px: ${over}`);
      if (hash === 'models') await shot('hub-08-' + width);
    }
  }
  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('model hub browser acceptance passed');
})().catch(async error => { console.error(error); await failed().catch(() => {}); process.exit(1); });
