// Issue #4: the desktop runs large models only through TokenFans, configured on the 云平台账户 page.
//
// 1. Helpers shared by the browser acceptance scripts (require('./tokenfans_browser.cjs')): the
//    fixture's TokenFans stand-in (tests/tokenfans_fixture.py with routes) forwards each model id
//    to that flow's scripted model. cfg.tokenfans_key is a fake key, random per fixture run.
// 2. Run directly, the acceptance of the TokenFans panel itself, against a stand-in (never the
//    paid service): node tests/tokenfans_browser.cjs CONFIG.json [SHOTS_DIR]
//    with python3 tests/tokenfans_browser_fixture.py CONFIG.json running.

async function connectTokenFans(page, cfg, {navigate = true} = {}) {
  if (navigate) await page.goto(cfg.url + '/#cloud');
  await page.locator('#main .tkf-panel').waitFor();
  await page.locator('#main .tkf-panel [data-action="tkf-key"]').first().click();
  await page.locator('#dialog [name="api_key"]').fill(cfg.tokenfans_key);
  await page.locator('#dialog button[type=submit]').click();
  await page.waitForFunction(() => state.model_compute && state.model_compute.connected, null, {timeout: 15000});
}

// Make `model` the default. Runs started afterwards use it; a started session or a
// workflow created earlier keeps its model.
async function useTokenFansModel(page, cfg, model) {
  await page.goto(cfg.url + '/#cloud');
  const form = page.locator('#main form[data-form="tkf-default"]');
  await form.waitFor();
  await form.locator('[name="model"]').selectOption(model);
  await form.locator('button[type=submit]').click();
  await page.waitForFunction(m => state.model_compute && state.model_compute.default_model === m &&
    rows('agent_models').some(x => x.billing === 'tokenfans' && x.model === m), model, {timeout: 15000});
}

module.exports = {connectTokenFans, useTokenFansModel};

// ------------------------------------------------------------------ panel acceptance (run directly)
async function panelAcceptance(cfgPath, shots) {
  const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
  const fs = require('fs'), path = require('path'), crypto = require('crypto');
  const fix = JSON.parse(fs.readFileSync(cfgPath));
  const cfg = {...fix, tokenfans_key: fix.api_key};
  if (shots) fs.mkdirSync(shots, {recursive: true});
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const WIDE = {width: 1440, height: 930}, SMALL = {width: 900, height: 640};
  const context = await browser.newContext({viewport: WIDE});
  // Records, per page load, whether the first state request went out before or after every deferred
  // script had run: before, a render with data can reach a page function from a later file.
  await context.addInitScript(() => {
    let loaded = false;
    document.addEventListener('DOMContentLoaded', () => { loaded = true; });
    const realFetch = window.fetch;
    window.fetch = function (url, ...rest) {
      if (String(url) === '/api/state' && !window.__firstStateRequest) window.__firstStateRequest = loaded ? 'after-load' : 'during-load';
      return realFetch.call(this, url, ...rest);
    };
  });
  const firstStateAfterLoad = async (p, where) => {
    const when = await p.evaluate(() => window.__firstStateRequest);
    if (when !== 'after-load') fail(`cold start at ${where}: first state request ${when || 'never sent'}`);
  };
  const page = await context.newPage(), errors = [], taken = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  const fail = msg => { throw Error(msg); };
  const shot = async name => { if (!shots) return; await page.waitForTimeout(200); await page.screenshot({path: path.join(shots, name + '.png')}); taken.push(name + '.png'); };
  const panel = page.locator('#main .tkf-panel');
  const panelText = async text => panel.filter({hasText: text}).first().waitFor({timeout: 15000});
  const dialogError = async text => page.locator('#dialog .form-error').filter({hasText: text}).first().waitFor({timeout: 15000});
  const submitDialog = () => page.locator('#dialog button[type=submit]').click();
  // The full key and the password must never reach the page: DOM, inputs, state or storage.
  const noSecrets = async where => {
    const seen = await page.evaluate(() => ({html: document.documentElement.outerHTML, state: JSON.stringify(state),
      storage: JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}),
      inputs: [...document.querySelectorAll('input,textarea')].map(i => i.value).join('\n')}));
    for (const [what, secret] of [['full API key', fix.api_key], ['TokenFans password', fix.password]]) {
      const where_ = Object.keys(seen).filter(k => seen[k].includes(secret));
      if (where_.length) fail(`${what} visible in the page (${where_.join(', ')}) after ${where}`);
    }
  };
  const mc = () => page.evaluate(() => state.model_compute);
  const tokenfansProfiles = () => page.evaluate(() => rows('agent_models').filter(m => m.billing === 'tokenfans').map(m => m.model));

  // a test-only app account with one project (the managed TokenFans profile is per project)
  // Registration needs the e-mailed code: the test server runs with NEUXMIND_MAIL_DEV_ECHO=1 and no
  // SMTP server, so the code comes back in the answer and nothing is mailed. The password needs a letter and a digit.
  const account = {email: 'tokenfans-ui@example.test', password: 'ui1-' + crypto.randomBytes(8).toString('hex'), name: 'TokenFans 验收'};
  const started = await page.request.post(cfg.url + '/api/register/start', {data: account});
  if (!started.ok()) fail('register/start ' + started.status());
  const {dev_code} = await started.json();
  if (!dev_code) fail('register/start returned no dev_code: start the fixture with NEUXMIND_MAIL_DEV_ECHO=1');
  const reg = await page.request.post(cfg.url + '/api/register', {data: {...account, code: dev_code}});
  if (!reg.ok()) fail('register ' + reg.status());
  // 0. cold start straight at /#cloud, as the desktop's 刷新 (Cmd+R) does on this page: the panel
  //    renders without page errors and the 4 s auto-refresh runs -- a change made outside the page
  //    (the API, not this page's scripts) shows up without any interaction, both ways.
  await page.goto(cfg.url + '/#cloud');
  await panelText('未连接'); await firstStateAfterLoad(page, '/#cloud');
  await page.evaluate(async () => { await api('projects', {name: 'TokenFans 验收', objective: '只用于界面验收，全部为测试资料。'}); await refresh(true); });
  const outside = async (route, data) => { const r = await page.request.post(cfg.url + '/api/' + route, {data}); if (!r.ok()) fail(route + ' ' + r.status()); };
  await outside('model-compute/key', {api_key: fix.api_key});
  await panel.filter({hasText: '已连接'}).first().waitFor({timeout: 12000});
  await outside('model-compute/disconnect', {});
  await panel.filter({hasText: '未连接'}).first().waitFor({timeout: 12000});
  if (errors.length) fail('cold start at /#cloud: ' + errors.join('; '));

  // 1. not connected: both ways to connect; 平台模型与算力 points here
  await panelText('粘贴 API Key'); await panelText('账号登录');
  if ((await tokenfansProfiles()).length) fail('a TokenFans profile exists before connecting');
  await shot('01-not-connected');
  await page.goto(cfg.url + '/#resources');
  await page.locator('#main .notice').filter({hasText: '尚未连接 TokenFans'}).first().waitFor();
  await page.locator('#main [data-action="go-cloud"]').first().click();
  await page.waitForFunction(() => location.hash === '#cloud'); await panel.waitFor();

  // 2. a malformed key and a rejected key are refused with their messages
  await panel.locator('[data-action="tkf-key"]').first().click();
  await page.locator('#dialog [name="api_key"]').fill('not a key');
  await submitDialog(); await dialogError('API Key 格式无效');
  await page.locator('#dialog [name="api_key"]').fill('sk-tkf-' + crypto.randomBytes(16).toString('hex'));
  await submitDialog(); await dialogError('TokenFans 拒绝了该 API Key');
  if ((await mc()).connected) fail('connected with a rejected key');
  await page.locator('#dialog [data-action="close"]').first().click();

  // 3. connect with the key: hint only, default = first model of the Codex catalogue
  await connectTokenFans(page, cfg);
  await panelText('已连接'); await panelText('通过 API Key 连接'); await panelText('…' + fix.api_key.slice(-4));
  await panelText(fix.tokenfans + '/codex/v1');
  let s = await mc();
  if (s.default_model !== s.models[0] || s.models.length < 2) fail('unexpected default after connecting: ' + JSON.stringify(s));
  if (JSON.stringify(await tokenfansProfiles()) !== JSON.stringify([s.models[0]])) fail('managed profile does not follow the default');
  await noSecrets('connecting with the key');
  await shot('02-connected-key');

  // 4. another default model; the profile and the 平台模型与算力 pointer follow it; refresh keeps it
  const other = s.models[1];
  await useTokenFansModel(page, cfg, other);
  await page.locator('[data-action="tkf-refresh"]').first().click();
  await page.locator('#toast').filter({hasText: '已刷新 TokenFans 模型列表'}).waitFor();
  if ((await mc()).default_model !== other) fail('refresh changed the default model');
  await page.goto(cfg.url + '/#resources');
  await page.locator('#main .notice').filter({hasText: '默认模型 ' + other}).first().waitFor();

  // 5. account login: wrong password refused; the right one shows the account and balance,
  //    keeps the chosen default and never keeps the password
  await page.goto(cfg.url + '/#cloud'); await panel.waitFor();
  await panel.locator('[data-action="tkf-login"]').first().click();
  await page.locator('#dialog [name="email"]').fill(fix.email);
  await page.locator('#dialog [name="password"]').fill('wrong-' + crypto.randomBytes(4).toString('hex'));
  await submitDialog(); await dialogError('邮箱或密码不正确');
  await page.locator('#dialog [name="password"]').fill(fix.password);
  await submitDialog();
  await page.waitForFunction(e => state.model_compute && state.model_compute.email === e, fix.email, {timeout: 15000});
  await panelText(fix.email); await panelText('¥ 123.45');
  if ((await mc()).default_model !== other) fail('login replaced the chosen default model');
  await noSecrets('account login');
  await shot('03-connected-login');
  await page.setViewportSize(SMALL); await page.waitForTimeout(250);
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) fail('horizontal overflow at 900 px');
  await shot('04-connected-900'); await page.setViewportSize(WIDE);

  // 6. disconnect (with confirmation): no TokenFans model is offered any more
  await panel.locator('[data-action="tkf-disconnect"]').first().click();
  await page.locator('#dialog [data-action="tkf-disconnect-confirm"]').click();
  await page.waitForFunction(() => state.model_compute && !state.model_compute.connected, null, {timeout: 15000});
  await panelText('未连接');
  if ((await tokenfansProfiles()).length) fail('a TokenFans profile is still offered after disconnecting');
  await noSecrets('disconnecting');
  await shot('05-disconnected');

  // 7. every page as the first page of a session (signed in): no page errors
  const routes = await page.evaluate(() => [...new Set([...navigation.map(([k]) => k), ...Object.keys(views)])]);
  for (const route of routes) {
    const cold = await context.newPage(), coldErrors = [];
    cold.on('pageerror', e => coldErrors.push(e.message));
    await cold.goto(cfg.url + '/#' + route);
    await cold.waitForFunction(() => state.user, null, {timeout: 15000});
    await firstStateAfterLoad(cold, '/#' + route);
    await cold.close();
    if (coldErrors.length) fail(`cold start at /#${route}: ${coldErrors.join('; ')}`);
  }

  if (errors.length) fail(errors.join('\n'));
  console.log(JSON.stringify({passed: true, shots: taken, coldStartRoutes: routes.length, jsErrors: errors.length}));
  await browser.close();
}

if (require.main === module) {
  if (!process.argv[2]) { console.error('usage: node tests/tokenfans_browser.cjs CONFIG.json [SHOTS_DIR]'); process.exit(2); }
  panelAcceptance(process.argv[2], process.argv[3]).catch(e => { console.error(e); process.exit(1); });
}
