const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for R-08 (simulation labels, deploy refused) and R-07 (解除急停).
// Start tests/sim_reset_browser_fixture.py first.
// Usage: node tests/sim_reset_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-sim-reset-qa.json';
const shots = process.argv[3] || 'artifacts';
const SIM = '仿真本体的测试结果不能作为现场证据';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 930}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  // Expected HTTP refusals (401 before login, 400 for the refused deploy) are not JS errors.
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
  });
  const shot = name => page.screenshot({path: path.join(shots, name)});
  const click = async (a, id) => {
    const sel = `[data-action="${a}"]` + (id ? `[data-id="${id}"]` : '');
    const d = page.locator('#dialog[open] ' + sel);
    await (await d.count() ? d : page.locator(sel)).first().click();
  };
  const fill = (k, v) => page.locator(`#dialog [name="${k}"]`).fill(String(v));
  const submit = () => page.locator('#dialog button[type=submit]').click();
  const calls = () => page.evaluate(() => fetch('/fixture/calls').then(r => r.json()));
  const heading = name => page.getByRole('heading', {name, exact: true}).waitFor();
  const assert = (ok, message) => { if (!ok) throw Error(message); };

  // Account and project through the UI.
  await page.goto(cfg.url);
  await gateSignIn(page, {name: '仿真与急停验收', email: 'sim-reset@example.test', password: 'fixture-password-123'});
  await click('journey-start');
  await page.locator('form[data-form=project]').waitFor();
  await fill('name', '仿真证据与急停');
  await fill('objective', '仿真本体不能作为现场证据；停止后需现场解除急停');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});

  // Assets through the same API the UI uses.
  const ids = await page.evaluate(async cfg => {
    const robot = async (name, endpoint) => {
      const r = await api('robots', {project_id: projectId, name, family: 'mobile', endpoint, token: cfg.token});
      await api('robots/connect', {robot_id: r.id});
      return r.id;
    };
    const sim = await robot('仿真底盘（仿真本体）', cfg.sim_device);
    const real = await robot('巡检底盘（协议替身）', cfg.real_device);
    const skill = await api('skills', {project_id: projectId, name: '前往巡检点', version: '1', requires: ['navigation.goal'],
      entrypoint: 'navigate', success_condition: '到达巡检点', stop_condition: '超时或现场停止', model_ids: []});
    const app = (name, robot_id) => api('applications', {project_id: projectId, name, version: '1.0', robot_id,
      steps: [{skill_id: skill.id, parameters: {}, timeout_s: 30}]});
    const simApp = await app('仿真巡检', sim), realApp = await app('现场巡检', real);
    await refresh(true);
    return {sim, real, simApp: simApp.id, realApp: realApp.id};
  }, cfg);

  // Robot list marks the simulated body.
  await page.goto(cfg.url + '/#robots');
  const simRow = page.locator('#main article.card', {hasText: '仿真底盘（仿真本体）'});
  await simRow.locator('.status.sim').waitFor();
  assert(!(await page.locator('#main article.card', {hasText: '巡检底盘（协议替身）'}).locator('.status.sim').count()), 'Real robot labelled 仿真');
  await shot('01-robots-sim-badge.png');

  // Simulated body: on-site test is stored as simulation and cannot be published.
  await page.evaluate(id => deliveryOpen(id), ids.simApp);
  await page.getByText('应用交付清单', {exact: true}).waitFor();
  await click('deliver-check');
  await heading('仿真本体：测试结果不能作为现场证据');
  await click('deliver-onsite');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await page.locator('#dialog').getByText('仿真测试 · 通过', {exact: false}).first().waitFor();
  await page.locator('#dialog .notice', {hasText: SIM}).first().waitFor();
  assert(!(await page.locator('#dialog [data-action=deliver-publish], #dialog [data-action=deliver-start]').count()),
    'Simulation evidence offered publish/start');
  await shot('02-checklist-simulation-evidence.png');
  const simReport = await page.evaluate(() => state.evaluations.find(e => e.scope === 'simulation'));
  assert(simReport && simReport.passed === true, 'Simulation report missing or passed changed');
  const refused = await page.evaluate(async body => {
    try { await api('deployments', body); return 'accepted'; } catch (e) { return e.message; }
  }, {application_id: ids.simApp, report_id: simReport.id});
  assert(refused.includes(SIM), 'Deploy was not refused with the simulation message: ' + refused);
  // Show the refusal the way the console surfaces errors (toast), for the record.
  await page.evaluate(m => toast(m), refused);
  await page.locator('#toast.show', {hasText: SIM}).waitFor();
  await shot('03-deploy-refused.png');
  await click('deliver-report', simReport.id);
  await heading('仿真测试报告');
  await page.locator('#dialog').getByText('仿真证据（非现场）').waitFor();
  await shot('04-simulation-report.png');
  await click('close');
  await page.goto(cfg.url + '/#harness');
  await page.locator('#main td', {hasText: '仿真测试（不能作为现场证据）'}).locator('.status.sim').waitFor();
  await shot('05-report-list-sim.png');
  assert(!(await calls()).sim.includes('deploy'), 'Simulated device received deploy');

  // Real (non-simulated) double: on-site -> publish -> start -> stop latches.
  await page.evaluate(id => deliveryOpen(id), ids.realApp);
  await click('deliver-check');
  await heading('下一步：现场测试');
  await click('deliver-onsite');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await heading('下一步：发布到机器人');
  await click('deliver-publish');
  await page.locator('form[data-form=deliver-publish]').waitFor();
  await submit();
  await heading('应用已发布，可以准备启动');
  await click('deliver-start');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await page.getByText('启动工作 · 已受理', {exact: true}).waitFor();
  await click('operate-stop');
  await page.locator('form[data-form=operate]').waitFor();
  await submit();
  await page.getByText('请求停止 · 已受理', {exact: true}).waitFor();
  await page.evaluate(id => deliveryOpen(id), ids.realApp);
  await heading('急停已锁存');
  assert(await page.locator('#dialog [data-action=operate-stop]').count(), 'Stop button not available while latched');
  await shot('06-checklist-estop-latched.png');

  // Robots page also offers 解除急停.
  await click('close');
  await page.goto(cfg.url + '/#robots');
  await page.locator('#main article.card', {hasText: '巡检底盘（协议替身）'}).locator('[data-action=robot-reset]').waitFor();
  await shot('07-robots-reset-button.png');

  // Reset needs the on-site confirmation; the device is not asked without it.
  await page.evaluate(id => deliveryOpen(id), ids.realApp);
  await click('robot-reset');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await submit();
  assert(!(await calls()).real.includes('reset'), 'Reset sent without confirmation');
  await shot('08-reset-confirmation.png');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await page.locator('#toast.show', {hasText: '设备已确认解除急停'}).waitFor();
  await page.getByText('解除急停 · 已受理', {exact: true}).waitFor();
  assert(!(await page.locator('#dialog .estop-latched').count()), 'Latch section still shown after reset');
  await shot('09-after-reset.png');

  // Start works again after the reset.
  await click('deliver-start');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await page.getByText('启动工作 · 已受理', {exact: true}).first().waitFor();
  const actual = await calls();
  const count = (list, k) => list.filter(x => x === k).length;
  assert(count(actual.real, 'reset') === 1 && count(actual.real, 'start') === 2 && actual.latched === false,
    'Unexpected device calls ' + JSON.stringify(actual));
  await shot('10-restarted.png');
  assert(!errors.length, errors.join('\n'));
  console.log(JSON.stringify({passed: true, simulationLabels: true, deployRefused: true, resetNeedsConfirmation: true,
    resetThenStart: true, stopAlwaysAvailable: true, jsErrors: errors.length, screenshots: shots}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
