const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #63: the Microduck's skill models on 「训练与改进」 — adding a simulated
// duck, running a skill model on it, stopping one, a failed one, the replay video, the training table,
// and that the block survives a redraw of the page.
// Start tests/duck_browser_fixture.py first (a made-up kit; NX_DUCK_REAL_KIT=1 for the real one).
// Usage: node tests/duck_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  const shot = name => page.screenshot({path: path.join(shots, name + '.png'), fullPage: true});
  const must = (ok, message) => { if (!ok) throw Error(message); };
  const line = page.locator('.duck .duck-line');
  const says = async (text, timeout = 30000) => {
    const until = Date.now() + timeout;
    for (;;) {
      const now = await line.innerText();
      if (now.includes(text)) return now;
      if (Date.now() > until) throw Error(`The stage never said 「${text}」; it says 「${now}」`);
      await page.waitForTimeout(100);
    }
  };
  const run = id => page.locator(`.duck [data-duck-run="${id}"]`);

  await page.goto(cfg.url + '/#training');
  const secret = 'dk' + Math.random().toString(36).slice(2, 10) + '63x';
  await gateSignIn(page, {name: '小鸭', email: `duck63-${Date.now()}@example.test`, password: secret});
  const block = page.locator('.duck');
  await block.waitFor();
  const kit = await page.evaluate(() => state.duck_kit);
  const real = !kit.shows.some(s => s.id === 'nod');
  must(kit.available && kit.vendor === 'Microduck' && kit.model === 'AD-P102', 'The state does not carry an available kit: ' + kit.problem);
  must((await block.locator('.duck-head h2').innerText()) === 'Microduck · AD-P102', 'The head does not name the brand and model');
  must(await block.locator('.duck-show').count() === kit.shows.length, 'Not every skill model is listed');
  must(await page.locator('.duck-head img').evaluate(i => i.complete && i.naturalWidth > 0), 'The picture of the duck did not load');

  // ---- no duck yet: nothing can be run, one can be added
  must((await block.locator('.duck-empty').innerText()).includes('还没有仿真小鸭'), 'Without a duck the stage does not say so');
  must(await run(kit.shows[0].id).isDisabled(), 'A skill model can be run without a duck');
  await shot('duck-01-empty');
  await block.locator('[data-duck-add]').click();
  await says('站在起点，可以开始', 90000);
  must(await block.locator('.duck-empty').count() === 0, 'The stage still says there is no duck');
  const robot = await page.evaluate(() => duckRobots(state.robots)[0]);
  must(robot && robot.status === 'connected' && robot.product.vendor === 'Microduck', 'The simulated duck is not a connected robot of its brand');
  await page.evaluate(() => { document.querySelector('.duck').dataset.kept = 'yes'; });

  if (real) {
    // ---- the real kit: the bow, performed on the engine it was accepted on
    await run('bow_roll').click();
    await says('贴地前滚一圈', 60000);
    must(await block.locator('[data-duck-stop]').isVisible(), 'No stop button while performing');
    await page.locator('.duck-frame:not([hidden])').waitFor();
    await shot('duck-02-performing');
    const said = await says('完整演完', 60000);
    must(/用时 4\.5\d 秒/.test(said), 'The bow did not take the recorded 4.5 s: ' + said);
    await shot('duck-03-done');
    await block.locator('[data-duck-replay="bow_roll"]').click();
    const video = block.locator('.duck-video');
    await video.waitFor();
    await page.waitForFunction(() => document.querySelector('.duck-video').readyState >= 2, null, {timeout: 20000});
    must(await video.evaluate(v => v.duration > 3 && v.videoWidth > 0), 'The replay video does not play');
  } else {
    // ---- a made-up kit: success, a stop, a failure
    await run('nod').click();
    await says('「节目 nod」完整演完，用时 0.50 秒');
    must(await block.locator('.duck-progress').getAttribute('aria-valuenow') === '100', 'The progress bar is not full after a finished show');
    must(await block.locator('[data-duck-stop]').isHidden(), 'The stop button stays after the show');
    await run('long').click();
    await says('「节目 long」 · 走');
    must(await run('nod').isDisabled(), 'Another skill model can be started while one is performed');
    await shot('duck-02-performing');
    await block.locator('[data-duck-stop]').click();
    await says('「节目 long」已停止');
    await run('fall').click();
    await says('「节目 fall」没有成功：脚以外的部位触地');
    must(await line.getAttribute('data-tone') === 'bad', 'A failed show is not marked as one');
    await block.locator('[data-duck-home]').click();
    await shot('duck-03-failed');
    await block.locator('[data-duck-replay="nod"]').click();
    const video = block.locator('.duck-video');
    await video.waitFor();
    must((await video.getAttribute('src')).endsWith('/api/duck/replay?show=nod'), 'The replay is not asked from the package');
  }
  const ranged = await page.evaluate(async id => {
    const r = await fetch('/api/duck/replay?show=' + id, {headers: {Range: 'bytes=0-3'}});
    return [r.status, r.headers.get('Content-Range'), (await r.arrayBuffer()).byteLength, (await fetch('/api/duck/replay?show=nothing')).status];
  }, real ? 'bow_roll' : 'nod');
  must(ranged[0] === 206 && /^bytes 0-3\/\d+$/.test(ranged[1]) && ranged[2] === 4 && ranged[3] === 404, 'The replay does not answer byte ranges: ' + ranged);
  must(await block.locator('.duck-frame').isHidden(), 'The live picture shows over the replay');
  await block.locator('[data-duck-live]').click();
  must(await block.locator('.duck-video').isHidden(), 'The replay stays after going back to the live picture');

  // ---- the block is kept across a redraw of the page
  await page.evaluate(() => refresh(true));
  await page.waitForTimeout(400);
  must(await page.evaluate(() => document.querySelectorAll('.duck').length === 1 && document.querySelector('.duck').dataset.kept === 'yes'),
    'The block was thrown away by a redraw');

  // ---- training
  const training = block.locator('.duck-training');
  await training.locator('table tbody tr').first().waitFor();
  const head = await training.locator('.duck-training-head').innerText();
  must(real ? /训练看板|安装包里的记录/.test(head) : head.includes('安装包里的记录') && head.includes('共 12 轮'), 'The training block does not say where its figures come from: ' + head);
  if (!real) {
    const cells = await training.locator('tbody tr').first().locator('td').allInnerTexts();
    must(cells[1].includes('10/10') && cells[1].includes('第 3 轮第 8 次') && cells[2].includes('7/10') && cells[3].includes('5 次'), 'Best, latest and count are wrong: ' + cells);
    must(await training.locator('svg.duck-spark polyline').count() === 1, 'No trend line');
  }
  must(await training.locator('a[target=_blank]').count() === 1, 'No way to the full dashboard');
  await shot('duck-04-training');

  // ---- the same robot on 我的机器人, with its picture
  await page.evaluate(() => { location.hash = 'robots'; });
  const picture = page.locator('#main img[src*="microduck-adp102"]').first();
  await picture.waitFor();
  must(await picture.evaluate(i => i.complete && i.naturalWidth > 0), 'The robot card does not show the duck\'s picture');
  must((await page.locator('#main .rp-brand').first().innerText()).includes('Microduck'), 'The robot card does not name the brand');
  await shot('duck-05-robots');

  // ---- narrow screens: nothing overflows
  await page.evaluate(() => { location.hash = 'training'; });
  await block.waitFor();
  for (const width of [375, 768, 1024]) {
    await page.setViewportSize({width, height: 800});
    await page.waitForTimeout(150);
    const over = await page.evaluate(() => { const b = document.querySelector('.duck').getBoundingClientRect(); return [b.right, window.innerWidth, document.documentElement.scrollWidth]; });
    must(over[0] <= over[1] + 1 && over[2] <= over[1] + 1, `The block overflows at ${width}px: ${over}`);
    await shot('duck-06-' + width);
  }
  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('duck browser acceptance passed' + (real ? ' (real kit)' : ''));
})().catch(error => { console.error(error); process.exit(1); });
