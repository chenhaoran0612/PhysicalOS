// Browser acceptance for round 03 (contract §5.3): engineering assistant on cloud-platform models.
// Covers: choosing a cloud model after cloud login, an engineering task and an SSH discovery run on
// cloud models, the cloud metering panel, and user-facing errors (wallet too small, overage lock,
// budget below the cloud minimum, logged out). Proves no local ledger entry is written.
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
  const selectModel = label => page.locator('#dialog [name=model_id]').selectOption({label});

  // ------------------------------------------------------------ workspace + project
  await page.goto(cfg.url);
  await click('account');
  await fill('name', '第 03 轮云端工程验收');
  await fill('email', `cloud03-${Date.now()}@example.test`);
  await fill('password', 'fixture-password-123');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await click('journey-start');
  await fill('name', '云平台工程助手');
  await fill('objective', '用云平台模型调查本体并开发适配器');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  const projectId = await page.evaluate(() => projectId);

  // ------------------------------------------------------------ 1. logged out: no cloud option
  await page.goto(cfg.url + '/#agents');
  await click('engine-models');
  await dlg(':text("登录云平台账户后")').first().waitFor();
  assert(!(await dlg('[data-action="engine-cloud-model"]').count()), '未登录时不应出现云平台模型');
  await shot('01-models-logged-out');
  await click('close');

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

  // ------------------------------------------------------------ 3. choose cloud models
  await page.goto(cfg.url + '/#agents');
  await click('engine-models');
  await dlg('[data-action="engine-cloud-model"]').waitFor();
  await shot('03-models-logged-in');
  await click('engine-cloud-model');
  await dlg('[name=cloud_model]').waitFor();
  const options = await dlg('[name=cloud_model] option').allTextContents();
  assert(options.some(o => o.includes('Echo 编码模型') && o.includes('10 点 / 千 token')), '云平台模型列表缺少计价：' + options);
  await shot('04-choose-cloud-model');
  await page.locator('#dialog [name=cloud_model]').selectOption('echo-code');
  await submit();
  for (const id of ['echo-discover', 'echo-overrun']) {
    await page.evaluate(async id => { await api('engineering/models/cloud', {project_id: projectId, model: id}); await refresh(true); }, id);
  }
  await page.goto(cfg.url + '/#agents');
  await page.locator('#main :text("云平台计费")').first().waitFor();
  await shot('05-agents-cloud-models');

  // ------------------------------------------------------------ 4. engineering task on a cloud model
  await click('engine-work');
  await fill('name', '云端：适配器工程');
  await selectModel('云平台 · Echo 编码模型（测试替身）');
  const budget = await page.locator('#dialog [name=max_tokens]').inputValue();
  assert(Number(budget) >= 80000, '选择云平台模型后预算应提升到云端最低值以上：' + budget);
  assert(await page.locator('#dialog [data-cloud-budget]').isVisible(), '未显示云平台预算说明');
  await shot('06-work-form-cloud-budget');
  await page.locator('#dialog [name=data_authorized]').check();
  await submit();
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
  await page.goto(cfg.url + '/#robots');
  await click('engine-discover', seeded.robot_id);
  await selectModel('云平台 · Echo 调查模型（测试替身）');
  await page.locator('#dialog [name=data_authorized]').check();
  await shot('08-discovery-form');
  await submit();
  await waitLive('云平台已结算');
  text = await live();
  assert(text.includes('云端会话') && text.includes('云平台扣除'), '调查会话缺少云端计量');
  await shot('09-discovery-session-cloud-metering');
  await click('close');

  // ------------------------------------------------------------ 6. errors
  // 6a. wallet too small for the budget (1,000,000 tokens × 10 / 1k = 10,000 credits)
  await page.goto(cfg.url + '/#agents');
  await click('engine-work');
  await fill('name', '云端：预算超过钱包');
  await selectModel('云平台 · Echo 编码模型（测试替身）');
  await fill('max_tokens', 1000000);
  await page.locator('#dialog [name=data_authorized]').check();
  await submit();
  await waitLive('云平台钱包可用点数不足', 30000);
  text = await live();
  assert(/需要 10000 点，可用 \d+ 点/.test(text), '钱包不足提示缺少数字：' + text.slice(0, 400));
  await shot('10-error-wallet');
  await click('close');

  // 6b. overage lock
  await click('engine-work');
  await fill('name', '云端：用量超出预留');
  await selectModel('云平台 · Echo 超额模型（测试替身）');
  await page.locator('#dialog [name=data_authorized]').check();
  await submit();
  await waitLive('超过了预留', 60000);
  await waitLive('超出预留的调用', 15000);
  await shot('11-error-overage', true);
  await click('close');

  // 6c. budget below the cloud minimum (server-side check; bypass the form's min attribute)
  const small = await page.evaluate(async () => {
    try {
      await api('engineering/work', {project_id: projectId, model_id: rows('agent_models').find(m => m.model === 'echo-code').id,
        name: 'x', objective: 'x', data_authorized: true, max_tokens: 30000, max_minutes: 1});
      return '';
    } catch (e) { return e.message; }
  });
  assert(small.includes('至少 80000'), '小预算提示错误：' + small);

  // 6d. logged out: the cloud model is refused before anything starts
  await page.goto(cfg.url + '/#cloud');
  await click('cloud-logout');
  if (await dlg('button[type=submit]').count()) await submit();
  await page.waitForTimeout(500);
  await page.goto(cfg.url + '/#agents');
  await click('engine-work');
  await fill('name', '云端：未登录');
  await selectModel('云平台 · Echo 编码模型（测试替身）');
  await page.locator('#dialog [name=data_authorized]').check();
  const loggedOut = await submit(true);
  assert(loggedOut.includes('云平台未登录'), '未登录提示错误：' + loggedOut);
  await shot('12-error-logged-out');
  await click('close');

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
