const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for round 03 (contract §5.3): engineering assistant on cloud-platform models.
// Issue #4: the UI offers only TokenFans models, before and after cloud login (checked here). The
// cloud gateway path stays in the backend for older sessions and the engineering API, so cloud
// models are added and runs started through the API, and their sessions are checked in the UI:
// an engineering task and an SSH discovery run on cloud models, the cloud metering panel, and
// user-facing errors (wallet too small, overage lock, budget below the cloud minimum, logged out).
// Proves no local ledger entry is written.
// Start tests/cloud03_browser_fixture.py first.
// Usage: node tests/cloud03_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-cloud03-qa.json';
const shots = process.argv[3] || 'artifacts';
const WIDE = {width: 1440, height: 930};
const SMALL = {width: 900, height: 640};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: WIDE});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
  });
  const taken = [];
  const shot = async (name, both = false) => {
    await page.waitForTimeout(200);
    await page.screenshot({path: path.join(shots, name + '-1440.png')});
    taken.push(name + '-1440.png');
    if (both) {
      await page.setViewportSize(SMALL);
      await page.waitForTimeout(300);
      await page.screenshot({path: path.join(shots, name + '-900.png')});
      taken.push(name + '-900.png');
      await page.setViewportSize(WIDE);
    }
  };
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const dlg = sel => page.locator('#dialog[open] ' + sel);
  const click = async (action, id) => {
    const sel = `[data-action="${action}"]` + (id ? `[data-id="${id}"]` : '');
    const inDialog = dlg(sel);
    await (await inDialog.count() ? inDialog : page.locator(sel)).first().click();
  };
  const fill = (k, v) => page.locator(`#dialog [name="${k}"]`).fill(String(v));
  const submit = async (expectError = false) => {
    await page.locator('#dialog[open] button[type=submit]').click();
    await page.waitForTimeout(400);
    const e = (await page.locator('#dialog[open] .form-error').allTextContents()).filter(Boolean);
    if (e.length && !expectError) throw Error(e.join(';'));
    return e.join(';');
  };
  const fixture = p => page.evaluate(p => fetch(p).then(r => r.json()), p);
  const live = () => page.locator('#engine-live').innerText();
  const waitLive = async (text, timeout = 60000) => {
    await page.locator('#engine-live').getByText(text, {exact: false}).first().waitFor({timeout});
  };
  // The 工程模型 dialog offers TokenFans and nothing else (issue #4).
  const tokenfansOnly = async (when, name) => {
    await page.goto(cfg.url + '/#agents');
    await click('engine-models');
    await dlg('.model-option').first().waitFor();
    const titles = await dlg('.model-option h3').allTextContents();
    assert(titles.length === 1 && titles[0].includes('TokenFans'), when + '：工程模型只应提供 TokenFans：' + titles);
    for (const gone of ['engine-cloud-model', 'engine-platform-model', 'engine-own-model']) {
      assert(!(await dlg(`[data-action="${gone}"]`).count()), when + '：不应提供 ' + gone);
    }
    await shot(name);
    await click('close');
  };
  // Start a run through the engineering API and open its session view, as the form would.
  const startRun = (route, payload) => page.evaluate(async ([route, payload]) => {
    try {
      const s = await api(route, {project_id: projectId, data_authorized: true, max_minutes: 15, ...payload});
      await refresh(true);
      await engineAction('engine-session', s.id);
      return {id: s.id};
    } catch (e) { return {error: e.message}; }
  }, [route, payload]);

  // ------------------------------------------------------------ workspace + project
  await page.goto(cfg.url);
  await gateSignIn(page, {name: '第 03 轮云端工程验收', email: `cloud03-${Date.now()}@example.test`, password: 'fixture-password-123'});
  await click('journey-start');
  await fill('name', '云平台工程助手');
  await fill('objective', '用云平台模型调查本体并开发适配器');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  const projectId = await page.evaluate(() => projectId);

  // ------------------------------------------------------------ 1. logged out: TokenFans only
  await tokenfansOnly('未登录云平台', '01-models-logged-out');

  // ------------------------------------------------------------ 2. cloud login
  await page.goto(cfg.url + '/#cloud');
  await click('cloud-address');
  await fill('base_url', cfg.cloud);
  await submit();
  await click('cloud-login');
  await fill('email', cfg.email);
  await fill('password', cfg.password);
  await submit();
  await page.locator('.cloud-wallet').waitFor({timeout: 15000});
  await shot('02-cloud-logged-in');
  const walletStart = (await fixture('/fixture/state')).cloud_wallet;

  // ------------------------------------------------------------ 3. cloud models: not offered in the UI, still in the API
  await tokenfansOnly('已登录云平台', '03-models-logged-in');
  const listing = await page.evaluate(() => api('cloud/gateway-models', {}));
  const echo = (listing.models || []).find(m => m.id === 'echo-code');
  assert(echo && String(echo.name).includes('Echo 编码模型') && echo.pricing && echo.pricing.credits === 10,
    '云平台模型目录缺少计价：' + JSON.stringify(listing).slice(0, 400));
  const models = await page.evaluate(async ids => {
    const out = {};
    for (const id of ids) out[id] = (await api('engineering/models/cloud', {project_id: projectId, model: id})).id;
    await refresh(true);
    return out;
  }, ['echo-code', 'echo-discover', 'echo-overrun']);
  await page.goto(cfg.url + '/#agents');
  await page.locator('#main h1').waitFor();
  assert(!(await page.locator('#main :text("云平台计费")').count()), '云平台模型不应出现在工程助手的模型列表中');
  await shot('05-agents-tokenfans-only');

  // ------------------------------------------------------------ 4. engineering task on a cloud model
  let run = await startRun('engineering/work', {model_id: models['echo-code'], name: '云端：适配器工程', objective: '为本体生成适配器', max_tokens: 80000});
  assert(run.id, '云平台模型的工程任务未能启动：' + run.error);
  await waitLive('云平台已结算');
  await page.locator('#engine-live [data-action=engine-artifact]').first().waitFor({timeout: 20000});
  let text = await live();
  assert(text.includes('云平台扣除') && text.includes('本机账本不记账'), '会话记录缺少云端计量');
  assert(/云平台扣除\s*\d+ 点/.test(text), '未显示扣除点数');
  await shot('07-work-session-cloud-metering', true);
  await click('engine-artifact');
  assert((await page.locator('#dialog pre').innerText()).includes('cloud adapter'), '工程产物缺失');
  await click('close');

  // ------------------------------------------------------------ 5. discovery on a cloud model
  const seeded = await fixture('/fixture/seed-ssh?project=' + projectId);
  await page.evaluate(() => refresh(true));
  run = await startRun('engineering/start', {robot_id: seeded.robot_id, model_id: models['echo-discover'], max_tokens: 200000});
  assert(run.id, '云平台模型的本体调查未能启动：' + run.error);
  await waitLive('云平台已结算');
  text = await live();
  assert(text.includes('云端会话') && text.includes('云平台扣除'), '调查会话缺少云端计量');
  await shot('09-discovery-session-cloud-metering');
  await click('close');

  // ------------------------------------------------------------ 6. errors
  // 6a. wallet too small for the budget (1,000,000 tokens × 10 / 1k = 10,000 credits)
  run = await startRun('engineering/work', {model_id: models['echo-code'], name: '云端：预算超过钱包', objective: 'x', max_tokens: 1000000});
  assert(run.id, '预算超过钱包的任务应先创建再报错：' + run.error);
  await waitLive('云平台钱包可用点数不足', 30000);
  text = await live();
  assert(/需要 10000 点，可用 \d+ 点/.test(text), '钱包不足提示缺少数字：' + text.slice(0, 400));
  await shot('10-error-wallet');
  await click('close');

  // 6b. overage lock
  run = await startRun('engineering/work', {model_id: models['echo-overrun'], name: '云端：用量超出预留', objective: 'x', max_tokens: 80000});
  assert(run.id, '超额模型的任务未能启动：' + run.error);
  await waitLive('超过了预留', 60000);
  await waitLive('超出预留的调用', 15000);
  await shot('11-error-overage', true);
  await click('close');

  // 6c. budget below the cloud minimum (server-side check; bypass the form's min attribute)
  const small = await page.evaluate(async id => {
    try {
      await api('engineering/work', {project_id: projectId, model_id: id,
        name: 'x', objective: 'x', data_authorized: true, max_tokens: 30000, max_minutes: 1});
      return '';
    } catch (e) { return e.message; }
  }, models['echo-code']);
  assert(small.includes('至少 80000'), '小预算提示错误：' + small);

  // 6d. logged out: the cloud model is refused before anything starts
  await page.goto(cfg.url + '/#cloud');
  await click('cloud-logout');
  if (await dlg('button[type=submit]').count()) await submit();
  await page.waitForTimeout(500);
  run = await startRun('engineering/work', {model_id: models['echo-code'], name: '云端：未登录', objective: 'x', max_tokens: 80000});
  assert(!run.id && String(run.error).includes('云平台未登录'), '未登录提示错误：' + JSON.stringify(run));

  // ------------------------------------------------------------ 7. ledger: cloud only
  const state = await fixture('/fixture/state');
  assert(state.local_ledger_entries === 0 && state.local_reservations === 0, '本机账本被写入：' + JSON.stringify(state));
  const charged = state.sessions.map(s => s.cloud_session?.metering?.credits_charged || 0).reduce((a, b) => a + b, 0);
  assert(walletStart.balance - state.cloud_wallet.balance === charged, '云端钱包变化与会话记录不一致');
  assert(state.cloud_wallet.reserved === 0, '云端预留未释放');
  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, runtime: cfg.runtime, screenshots: taken, cloud_charged: charged,
    sessions: state.sessions.map(s => [s.name, s.status, s.metering_status]), jsErrors: errors.length}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
