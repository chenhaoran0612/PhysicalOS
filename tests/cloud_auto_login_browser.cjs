// Issue #36 browser acceptance: signing into the app signs into the cloud (the material library) too.
//  1 · A session from the version before: the 资料库 page asks for a cloud sign-in (the issue's screenshot).
//  2 · With this version the app starts on the sign-in page and says why; nothing else is asked.
//  3 · Signing in again: the 资料库 page is open, no second sign-in.
//  4 · The cloud sign-in lapses while the app is open: the page offers 「重新登录」, which leads to the sign-in page.
//  5 · It lapsed while the app was closed: the app starts on the sign-in page and says so.
// Fixture: tests/cloud_auto_login_browser_fixture.py (the real cloud app on a temp directory).
// Usage: node tests/cloud_auto_login_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-cloud-auto-login-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `cloud-auto-login-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), gate = page.locator('#auth-gate');
  const fixture = async what => (await page.request.get(cfg.url + '/fixture/' + what)).json();
  const account = {name: '云登录验收', email: `auto-${crypto.randomBytes(4).toString('hex')}@example.test`,
    password: 'desk-' + crypto.randomBytes(8).toString('hex') + '-7'};
  const library = async () => {
    await page.locator('#navigation a[href="#library"]').click();
    await main.locator('h1', {hasText: '资料库'}).waitFor();
  };
  const gateNote = async () => {
    await gate.locator('form[data-acct-form="login"]').waitFor();
    return gate.locator('.auth-note').innerText();
  };
  const result = {browser: engine.name()};

  // 1 · The version before: signed into the app, the library asks for a second sign-in.
  await page.goto(cfg.url);
  await gateSignIn(page, account);
  await library();
  must((await main.innerText()).includes('请先登录云平台账户'), 'The old version should ask for a cloud sign-in');
  must(await main.locator('[data-cloud-auto-login]').count() === 0, 'A reason is shown although the automatic sign-in never ran');
  await shot('1-old-version');

  // 2 · This version starts: the old session is signed out once, and the sign-in page says why.
  await fixture('auto-login?on=1');
  await page.reload();
  const why = await gateNote();
  must(why.includes('云平台') && why.includes('重新登录'), 'The sign-in page does not say why: ' + why);
  await shot('2-signed-out-once');

  // 3 · Signing in again signs into the cloud: the library is open.
  await gateSignIn(page, {...account, register: false});
  await library();
  await main.locator('.empty', {hasText: '资料库还是空的'}).waitFor();
  must(!(await main.innerText()).includes('请先登录云平台账户'), 'The library still asks for a cloud sign-in');
  const cloud = await page.evaluate(() => state.cloud_account);
  must(cloud.logged_in && cloud.email === account.email, 'The cloud account is not the app account: ' + JSON.stringify(cloud));
  must(cloud.email_verified === true, 'The development cloud should have verified the new account');
  result.status = cloud.auto_login.status;
  await shot('3-library-open');
  // Starting the app again changes nothing.
  await page.reload();
  await page.locator('#navigation a').first().waitFor();
  must(await gate.isHidden(), 'A second start signed the user out again');

  // 4 · The cloud sign-in lapses while the app is open.
  await fixture('expire-cloud-sessions');
  await library();
  await page.evaluate(async () => { try { await api('cloud/overview', {}); } catch (e) { /* the 401 drops the dead session */ } await refresh(true); });
  const lapsed = main.locator('[data-cloud-auto-login="expired"]');
  await lapsed.waitFor();
  must((await lapsed.innerText()).includes('到期'), 'The page does not say the cloud sign-in lapsed');
  await shot('4-lapsed-while-open');
  await main.locator('button[data-action="logout"]', {hasText: '重新登录'}).click();
  await gate.locator('form[data-acct-form="login"]').waitFor();
  await gateSignIn(page, {...account, register: false});
  await library();
  await main.locator('.empty', {hasText: '资料库还是空的'}).waitFor();

  // 5 · It lapsed while the app was closed: the app starts on the sign-in page.
  await fixture('expire-cloud-sessions');
  await page.reload();
  const expired = await gateNote();
  must(expired.includes('到期'), 'The sign-in page does not say the cloud sign-in lapsed: ' + expired);
  await shot('5-lapsed-while-closed');
  await gateSignIn(page, {...account, register: false});
  await library();
  await main.locator('.empty', {hasText: '资料库还是空的'}).waitFor();

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, ...result}));
  await browser.close();
})().catch(async error => { await failed(); console.error('FAIL', error.message); process.exit(1); });
