const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #79: on 「训练与改进」 each of the duck's models opens its training and
// acceptance records (the training run, every accepted episode, the control without the trained
// correction, the versions before it); a recording of an accepted episode plays on the stage; a model that
// is no longer delivered has no run button; the model library shows every version and the accepted
// episodes of an imported evaluation; nothing overflows at 375 and 768 wide.
// Start tests/duck_training_data_fixture.py first (a made-up kit; NX_DUCK_REAL_KIT=1 for the real one).
// Usage: node tests/duck_training_data_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  const must = (ok, message) => { if (!ok) throw Error(message); };
  failed = async () => { await page.screenshot({path: path.join(shots, 'duck79-failed.png'), fullPage: true}); };
  const main = page.locator('#main');
  const block = page.locator('.duck');
  const panel = block.locator('[data-duck-part="evidence"]');

  await page.goto(cfg.url + '/#training');
  const secret = 'dr' + Math.random().toString(36).slice(2, 10) + '79x';
  await gateSignIn(page, {name: '小鸭', email: `duck79-${Date.now()}@example.test`, password: secret});
  await block.waitFor();
  const kit = await page.evaluate(() => state.duck_kit);
  const real = !kit.shows.some(s => s.id === 'nod');
  const packaged = kit.shows.filter(s => s.packaged), archived = kit.shows.filter(s => s.status === 'archived');
  must(packaged.length && kit.recordings === true, 'The kit has no packaged model or no recordings');
  must(await panel.isHidden(), 'The records of a model are open before one is asked for');

  // ---- every model says how long it lasts; one that is no longer delivered cannot be run
  for (const show of packaged) {
    const row = block.locator(`.duck-show[data-duck-show="${show.id}"]`);
    must((await row.locator('.duck-show-facts').innerText()).includes('每段 '), `${show.id} does not say how long it lasts`);
    must(await row.locator('[data-duck-evidence]').count() === 1, `${show.id} has no 训练与验收`);
  }
  for (const show of archived) {
    const row = block.locator(`.duck-show[data-duck-show="${show.id}"]`);
    must(await row.locator('[data-duck-run]').count() === 0 && (await row.innerText()).includes('已归档'), `${show.id} is not shown as archived`);
  }

  // ---- the records of one model
  const first = packaged[0];
  await block.locator(`[data-duck-evidence="${first.id}"]`).click();
  await panel.locator('.duck-ev-grid').waitFor();
  const text = await panel.innerText();
  for (const words of ['训练过程', '验收结果', '历次交付的版本', '固定组', '独立组', '累计训练步数', '对照']) {
    must(text.includes(words), `The records do not show 「${words}」: ${text.slice(0, 400)}`);
  }
  must(await block.locator(`[data-duck-evidence="${first.id}"]`).getAttribute('aria-expanded') === 'true', 'The open model is not marked');
  const rows = await panel.locator('.duck-cases tbody tr').count();
  must(rows === 20, `Not every accepted episode is listed (${rows})`);
  must((await panel.locator('.duck-ev-versions li').count()) === 1 + (first.history || []).length, 'Not every version is listed');
  if (!real) must(text.includes('一共训练了 3 轮'), 'The training runs of the kit\'s copy are not shown');
  await panel.screenshot({path: path.join(shots, 'duck79-01-records.png')});

  // ---- a recording plays on the stage, and the stage goes back to the live picture
  const play = panel.locator('[data-duck-recording]').first();
  const [show, group, seed] = (await play.getAttribute('data-duck-recording')).split('|');
  await play.click();
  const video = block.locator('.duck-video');
  await page.waitForFunction(() => { const v = document.querySelector('.duck .duck-video'); return v && !v.hidden && v.src.includes('/api/duck/recording'); });
  must((await block.locator('.duck-line').innerText()).includes(`起姿 ${seed}`), 'The stage does not say which episode it plays');
  const answer = await page.evaluate(async src => { const r = await fetch(src); return {status: r.status, type: r.headers.get('content-type'), size: (await r.arrayBuffer()).byteLength}; },
    await video.getAttribute('src'));
  must(answer.status === 200 && answer.type === 'video/mp4' && answer.size > 0, 'The recording is not served as a video: ' + JSON.stringify(answer));
  const ranged = await page.evaluate(async src => (await fetch(src, {headers: {Range: 'bytes=0-9'}})).status, await video.getAttribute('src'));
  must(ranged === 206, 'A byte range of a recording is not served (a WebKit player needs it)');
  const elsewhere = await page.evaluate(async q => (await fetch('/api/duck/recording?' + q)).status, `show=${show}&group=zero&seed=${seed}`);
  must(elsewhere === 404, 'A recording that is not offered is served');
  const odd = await page.evaluate(async q => (await fetch('/api/duck/recording?' + q)).status, `show=${show}&group=${group}&seed=%C2%B2`);
  must(odd === 404, 'A seed that is not a number is not answered as missing: ' + odd);
  if (real) {
    await page.waitForFunction(() => document.querySelector('.duck .duck-video').readyState >= 2, null, {timeout: 30000});
    const seconds = await video.evaluate(v => v.duration);
    must(seconds > 14 && seconds < 31, `The recording is not a whole 15–30 s episode (${seconds} s)`);
  }
  await block.locator('.duck-stage').screenshot({path: path.join(shots, 'duck79-02-recording.png')});
  await block.locator('[data-duck-live]').click();
  must(await video.isHidden(), 'The recording stays on the stage after 回到实时画面');

  // ---- closing gives the focus back to the button that opened it
  await panel.locator('[data-duck-evidence-close]').click();
  must(await panel.isHidden(), 'The records do not close');
  must(await page.evaluate(id => document.activeElement && document.activeElement.dataset.duckEvidence === id, first.id), 'The focus is lost when the records close');

  // ---- an archived model has only its versions
  if (archived.length) {
    await block.locator(`[data-duck-evidence="${archived[0].id}"]`).click();
    await panel.locator('.duck-ev-versions').waitFor();
    must((await panel.innerText()).includes('旧版本，不随这个安装带来'), 'An archived model does not say its package is not here');
    await panel.locator('[data-duck-evidence-close]').click();
  }

  // ---- narrow screens, with the records open
  await block.locator(`[data-duck-evidence="${first.id}"]`).click();
  await panel.locator('.duck-ev-grid').waitFor();
  for (const width of [375, 768]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(300);
    const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    must(over <= 0, `The page overflows by ${over} px at ${width} wide`);
    await panel.screenshot({path: path.join(shots, `duck79-03-${width}.png`)});
  }
  await page.setViewportSize({width: 1440, height: 900});

  // ---- the model library: every version, and the accepted episodes of the imported evaluation
  await page.evaluate(() => { location.hash = 'models'; });
  const card = main.locator('.mh-card', {has: page.locator('h3', {hasText: first.title})});
  await card.locator('[data-action="hub-open"]').click();
  await main.locator('.mh-detail').waitFor();
  must(await main.locator('.mh-versions > li').count() === 1 + (first.history || []).length, 'The model library does not list every version');
  const evaluation = main.locator('.mh-evals tr', {hasText: '独立起姿组'}).first();
  await evaluation.locator('[data-action="hub-eval-open"]').click();
  await page.locator('#dialog .mh-eval-detail').waitFor();
  const cells = await page.locator('#dialog .mh-eval-detail tbody tr').count();
  must(cells >= 10, `The imported evaluation does not list its accepted episodes (${cells} rows)`);
  must((await page.locator('#dialog .mh-eval-detail').innerText()).includes('秒'), 'The accepted episodes have no time');
  await page.locator('#dialog').screenshot({path: path.join(shots, 'duck79-04-library.png')});
  await page.locator('#dialog .close-dialog').click();

  must(!errors.length, 'The page reported errors: ' + errors.join(' | '));
  await browser.close();
  console.log(`duck_training_data_browser: passed (${real ? 'real kit' : 'made-up kit'}, ${engine === playwright.webkit ? 'WebKit' : 'Chromium'})`);
})().catch(async error => { try { await failed(); } catch { /* the page is gone */ } console.error(error); process.exit(1); });
