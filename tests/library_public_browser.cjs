// Issue #78 browser acceptance: the public library (公共资料库) on the 资料库 page. An ordinary account sees the
// public materials marked 「公共」, without the buttons that would change them, and their parse result read-only;
// the platform operator sees the same rows with those buttons and can confirm the model.
// Fixture: tests/library_public_browser_fixture.py (a real cloud app on a temp directory, made-up files).
// Usage: node tests/library_public_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-library-public-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `library-public-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const row = name => page.locator('#material-list tbody tr', {hasText: name});
  const result = {browser: engine.name()};

  const signInToCloud = async email => {
    await page.evaluate(async ([c, e]) => {
      await api('cloud/configure', {base_url: c.cloud});
      await api('cloud/login', {email: e, password: c.password});
      await refresh(true);
    }, [cfg, email]);
    await page.reload();                  // the page keeps the list it read for the previous account
    await page.locator('#navigation a[href="#library"]').click();
    await main.locator('h1', {hasText: '资料库'}).waitFor();
    await row('demo-arm.zip').waitFor();
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '公共资料库验收', email: 'library-public-ui-' + crypto.randomBytes(4).toString('hex') + '@example.test', password: 'desk-' + crypto.randomBytes(8).toString('hex') + '-7'});

  // 1 · An ordinary account: both public materials are listed, marked 「公共」, without 删除 / 改品牌型号.
  await signInToCloud(cfg.email);
  for (const name of ['demo-arm.zip', 'demo-manual.pdf']) {
    const r = row(name);
    must(await r.getAttribute('data-lp-public') !== null && await r.getAttribute('data-lp-read-only') !== null, `${name} is not marked public and read-only`);
    must((await r.locator('.lp-badge').innerText()).trim() === '公共', `${name} does not say 公共`);
    must(!(await r.locator('[data-library-action="delete"]').isVisible()), `${name} offers 删除 to an ordinary account`);
    must(!(await r.locator('[data-ml-action="move"]').isVisible()), `${name} offers 改品牌型号 to an ordinary account`);
  }
  must((await row('demo-arm.zip').innerText()).includes('示例公共厂家'), 'the brand of the public model is not shown');
  await shot('1-ordinary-account');
  result.ordinaryRows = 'public, read-only';

  // 2 · Its parse result is shown, read-only, with who may change it.
  await row('demo-arm.zip').locator('[data-parse-action="open"]').click();
  const panel = dialog.locator('.mp-panel');
  await panel.waitFor();
  await dialog.locator('.lp-note').waitFor();
  must((await dialog.locator('.lp-note').innerText()).includes('只有平台运营人员能修改、确认或删除'), 'the result does not say who may change it');
  must(await dialog.locator('form[data-parse-form]').count() === 0, 'the read-only result is still a form');
  must(await dialog.locator('fieldset[disabled]').count() === 1, 'the fields of the read-only result are not disabled');
  must(!(await dialog.locator('[data-parse-action="confirm"]').isVisible()), '确认可用 is offered to an ordinary account');
  must((await panel.innerText()).includes('shoulder'), 'the joint read from the URDF is not shown');
  await shot('2-read-only-result');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('#dialog')?.open);

  // 3 · The platform operator: the same rows without the read-only mark; confirming the model works.
  await signInToCloud(cfg.operator);
  const operatorRow = row('demo-arm.zip');
  must(await operatorRow.getAttribute('data-lp-public') !== null, 'the operator does not see the row as public');
  must(await operatorRow.getAttribute('data-lp-read-only') === null, 'the operator sees the row read-only');
  must(await operatorRow.locator('[data-library-action="delete"]').isVisible(), 'the operator is not offered 删除');
  await operatorRow.locator('[data-parse-action="open"]').click();
  await dialog.locator('form[data-parse-form="result"]').waitFor();
  must(await dialog.locator('.lp-note').count() === 0, 'the operator is told the result is read-only');
  await shot('3-operator-result');
  await dialog.locator('[data-parse-action="confirm"]').click();
  await page.waitForFunction(async () => {
    const list = await api('library/list', {kind: 'model'});
    return (list.materials || []).some(m => m.filename === 'demo-arm.zip' && m.status === 'usable');
  }, null, {timeout: 15000});
  result.operatorConfirmed = true;

  // 4 · Back as the ordinary account: the model is 可用 for it too.
  await page.keyboard.press('Escape').catch(() => {});
  await signInToCloud(cfg.email);
  // Since #77 the usable model a brand and model simulates with reads 「仿真使用中」 rather than 「可用」.
  const usableText = await row('demo-arm.zip').innerText();
  must(usableText.includes('仿真使用中') || usableText.includes('可用'), 'the confirmed public model is not usable for an ordinary account');
  await shot('4-usable-for-everyone');
  must(errors.length === 0, 'page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, ...result}));
  await browser.close();
})().catch(async e => {
  await failed();
  console.error(e);
  process.exit(1);
});
