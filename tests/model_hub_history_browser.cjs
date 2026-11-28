const {gateSignIn} = require('./gate_helper.cjs');
// Browser acceptance for issue #76: in a round of improvement the candidate does not have to be uploaded.
// 「从历史中选择」 lists the model's other versions and the training project's evaluated model files (from a
// made-up dashboard); a click makes one the candidate. A file whose content is not what its checksum says is
// refused in the dialog; a version that already has a result on the round's set goes straight to the decision.
// Start tests/model_hub_history_fixture.py first.
// Usage: node tests/model_hub_history_browser.cjs [config.json] [screenshot-dir]     BRAIN_BROWSER=webkit for WebKit
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
  failed = async () => { await page.screenshot({path: path.join(shots, 'history-failed.png'), fullPage: true}); console.error((await page.locator('#dialog').innerText().catch(() => '')).slice(0, 800), (await page.locator('#main').innerText()).slice(0, 1200)); };
  const main = page.locator('#main');
  const round = main.locator('.mh-round');
  const step = () => round.locator('.mh-steps li.current').innerText();
  const form = kind => page.locator(`#dialog form[data-hub-form="${kind}"]`);
  const send = async kind => { await form(kind).locator('[type=submit]').click(); await form(kind).waitFor({state: 'hidden'}); };
  const history = page.locator('#dialog [data-hub-history]');
  const files = history.locator('[aria-label="训练项目里的模型文件"] .mhh-pick');
  const versions = history.locator('[aria-label="这个模型的版本"] .mhh-pick');
  const fileRow = key => history.locator(`.mhh-pick[data-id$="|t|${key}"]`);

  const startRound = async goal => {
    await main.locator('[data-action="hub-round-add"]').first().click();
    await form('round').locator('[name=set_id]').selectOption('hubs-duck-fixed');
    await form('round').locator('[name=goal]').fill(goal);
    await send('round');
    await round.locator('[data-action="hub-diagnose"]').click();          // the package already has a result on the fixed poses
    await form('diagnose').locator('[name=weaknesses]').fill('起姿 3 落地不稳');
    await form('diagnose').locator('[name=plan]').fill('加大落地奖励');
    await send('diagnose');
    await round.locator('[data-action="hub-history"]').waitFor();
  };

  // ---- a duck's skill model, a round at its training step
  await page.goto(cfg.url + '/#training');
  const secret = 'mh' + Math.random().toString(36).slice(2, 10) + '76x';
  await gateSignIn(page, {name: '历史', email: `hub76-${Date.now()}@example.test`, password: secret});
  await main.locator('.mh-board').waitFor();
  await page.evaluate(() => action('hub-improve', 'hubm-duck-nod'));
  await main.locator('.mh-start').waitFor();
  await startRound('独立站姿达到 100%');
  must((await step()).includes('训练'), 'The round is not at the training step');
  const body = await round.locator('.mh-round-body').innerText();
  must(body.includes('上传新模型文件') && body.includes('从历史中选择') && body.includes('或者从历史中选一个'), 'The training step does not offer the history next to the upload');
  await shot('history-01-training-step');

  // ---- the dialog: no other version yet, the training project's files newest first
  await round.locator('[data-action="hub-history"]').click();
  await history.waitFor();
  await history.locator('[aria-label="这个模型的版本"] .mhh-none').waitFor();                 // the package is the round's start: nothing else yet
  must(await versions.count() === 0, 'The package, the round\'s start, is offered as a candidate');
  await files.first().waitFor();
  must(await files.count() === 20, 'Not twenty files at first: ' + await files.count());
  must((await files.first().innerText()).includes('第 6 轮 · 第 24 次更新'), 'The newest file is not first');
  must((await history.locator('.mhh-sub').innerText()).includes('29 个评估过的模型文件'), 'The list does not say how many files there are');
  await history.locator('[data-action="hub-history-more"]').click();
  must(await files.count() === 29, 'More files are not shown on request');
  must(await page.evaluate(() => document.activeElement?.dataset.id || '') === (await files.nth(20).getAttribute('data-id')), 'The focus does not go to the first file just shown');
  must(await fileRow('1-4').isDisabled() && (await fileRow('1-4').innerText()).includes('这一轮的起点'), 'The file of the start can be chosen');
  await shot('history-02-dialog');
  await history.locator('[data-hub-history-full]').check();
  must(await files.count() === 5 && (await history.locator('.mhh-sub').innerText()).includes('5 个'), 'Only the files that passed every run are not what is left');
  await history.locator('[data-hub-history-full]').uncheck();
  await history.locator('[data-action="hub-history-more"]').click();

  // ---- a file whose content is not what the dashboard recorded is refused in the dialog, also when the
  // dialog is drawn again while it is fetched (the fixture serves it after 1.5 s)
  await fileRow('5-2').click();
  await history.locator('[data-hub-history-full]').check();
  must(await files.count() === 5 && await fileRow('4-8').isDisabled(), 'A redraw while a file is fetched lets another be chosen');
  must((await history.locator('.mhh-status').innerText()).includes('第 5 轮第 2 次更新'), 'A redraw while a file is fetched loses what is under way');
  await history.locator('.form-error').filter({hasText: '不一致'}).waitFor();
  must(await history.locator('.form-error').evaluate(el => { const r = el.getBoundingClientRect(), box = el.closest('.mhh').getBoundingClientRect(); return r.height > 0 && r.top >= box.top - 1 && r.bottom <= box.bottom + 1; }),
       'The refusal is not in sight where the row was clicked');
  must(await page.locator('#dialog').evaluate(d => d.open), 'The dialog closed after a refused file');
  must(!(await fileRow('4-8').isDisabled()), 'The rows stay disabled after a refusal');
  must((await step()).includes('训练'), 'A refused file moved the round on');
  await shot('history-03-refused');

  // ---- a good file becomes the candidate, also when the dialog is drawn again meanwhile; its result comes from the dashboard
  await fileRow('4-8').click();
  await history.locator('[data-hub-history-full]').uncheck();
  must(await history.locator('.form-error').innerText() === '', 'The old refusal stays up for the next choice');
  await history.waitFor({state: 'hidden'});
  await round.locator('[data-action="hub-eval-training"]').waitFor();
  must((await step()).includes('评测候选'), 'The chosen file is not the candidate');
  must((await round.locator('.mh-round-body').innerText()).includes('候选版本是「第 4 轮 · 第 8 次更新」'), 'The candidate is not named');
  await round.locator('[data-action="hub-eval-training"]').click();
  await round.locator('[data-action="hub-round-reject"]').waitFor();
  must((await round.locator('.mh-verdict').innerText()) === '主指标没有变化。', 'The dashboard\'s result of the file is not compared with the start');
  await shot('history-04-review');

  // ---- swapping: the candidate is marked in both lists
  await round.locator('details.mh-again summary').click();
  await round.locator('details.mh-again [data-action="hub-history"]').click();
  await files.first().waitFor();
  must(await versions.count() === 1 && await versions.first().isDisabled() && (await versions.first().innerText()).includes('现在的候选'), 'The candidate is not marked among the versions');
  await history.locator('[data-action="hub-history-more"]').click();
  must(await fileRow('4-8').isDisabled() && (await fileRow('4-8').innerText()).includes('现在的候选'), 'The candidate is not marked among the files');
  await page.keyboard.press('Escape');
  await history.waitFor({state: 'hidden'});

  // ---- the next round takes the rejected candidate, with the result it already has
  await round.locator('[data-action="hub-round-reject"]').click();
  await send('round-reject');
  await main.locator('.mh-past.rejected').waitFor();
  await startRound('再试一次');
  await round.locator('[data-action="hub-history"]').click();
  await versions.first().waitFor();
  const earlier = versions.first();
  must((await earlier.innerText()).includes('第 1 轮的候选') && (await earlier.innerText()).includes('成功率 100%') && (await earlier.innerText()).includes('选它，用已有的评测'),
       'An earlier candidate is not offered with its result: ' + await earlier.innerText());
  for (const width of [375, 768]) {
    await page.setViewportSize({width, height: 800});
    await page.waitForTimeout(150);
    const over = await page.evaluate(() => { const d = document.querySelector('#dialog'); return [d.scrollWidth, d.clientWidth, document.documentElement.scrollWidth, window.innerWidth]; });
    must(over[0] <= over[1] + 1 && over[2] <= over[3] + 1, `The dialog overflows at ${width}px: ${over}`);
    await shot('history-05-' + width);
  }
  await page.setViewportSize({width: 1440, height: 900});
  await earlier.focus();
  await page.keyboard.press('Enter');
  await history.waitFor({state: 'hidden'});
  await round.locator('[data-action="hub-round-accept"]').waitFor();
  must((await step()).includes('决定') && await page.locator('#dialog[open]').count() === 0, 'A version with a result does not go straight to the decision');
  await shot('history-06-reused');

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('model hub history browser acceptance passed');
})().catch(async error => { console.error(error); await failed().catch(() => {}); process.exit(1); });
