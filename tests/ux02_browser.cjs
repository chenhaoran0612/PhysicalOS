// Browser acceptance for ux02 (round 05: "站在用户的需求来设计").
// A brand-new user with one simulated quadruped:
//   (c) every page's empty state offers a concrete next step; technical errors are explained plainly,
//       with the technical detail collapsible;
//   (a) "simulation is never field evidence" is explained on 验证与测试 / 部署与运行 before the user tries,
//       with the right next steps (run in simulation, connect a real robot);
//   (b) skill run parameters come from a form generated from skill.json, with a raw-JSON advanced toggle;
//   (d) 工程助手 without a model says what the model is for and how to configure one.
// Safety semantics are re-checked: authorization for motion, e-stop latch/reset, sim-only direct run.
// Start tests/ux01_browser_fixture.py first (fresh, empty workspace; simulated bodies only).
// Usage: node tests/ux02_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux02';
const SIM = '仿真本体的测试结果不能作为现场证据';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
  });
  const shot = name => page.screenshot({path: path.join(shots, name + '.png')});
  const dlg = sel => page.locator('#dialog[open] ' + sel);
  const click = async (a, id) => {
    const sel = `[data-action="${a}"]` + (id ? `[data-id="${id}"]` : '');
    const d = dlg(sel);
    await (await d.count() ? d : page.locator(sel)).first().click();
  };
  const fill = (k, v) => page.locator(`#dialog [name="${k}"]`).fill(String(v));
  const submit = () => page.locator('#dialog button[type=submit]').first().click();
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const go = async hash => { await page.goto(cfg.url + '/#' + hash); await page.waitForTimeout(400); };
  const skillRuns = () => page.evaluate(async () => { await refresh(true); return state.operations.filter(o => o.kind === 'skill_run'); });

  // ---------------------------------------------------------------- 0. fresh workspace + one simulated quadruped
  await go('robots');
  await click('sim-add');
  await page.locator('form[data-form=register]').waitFor();
  await fill('name', 'ux02 新用户');
  await fill('email', 'ux02@example.test');
  await fill('password', 'ux02-password-123');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await click('sim-add');
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  const quad = await page.evaluate(() => state.robots[0].id);
  await click('close');

  // ---------------------------------------------------------------- (c) every page: empty states have a next step
  const pages = await page.evaluate(() => navigation.map(([k, t]) => [k, t]));
  for (const [key, title] of pages) {
    await go(key);
    const bare = await page.locator('#main .empty').evaluateAll(els => els
      .filter(e => !e.querySelector('button, a[href]'))
      .map(e => e.innerText.replace(/\s+/g, ' ').slice(0, 60)));
    assert(!bare.length, `「${title}」有没有下一步按钮的空状态：${bare.join(' | ')}`);
  }

  // ---------------------------------------------------------------- (a) simulation explained before the user tries
  for (const [key, lead] of [['harness', '这里只能做仿真测试，不能做现场测试'], ['deployment', '“发布应用”还不能用']]) {
    await go(key);
    const box = page.locator('#main [data-sim-explain="only"]');
    await box.waitFor();
    const text = await box.innerText();
    for (const want of [lead, '用仿真机器人可以', '用仿真机器人不能', '要真正部署，需要', '不是故障']) {
      assert(text.includes(want), `${key} 仿真说明缺少：${want}`);
    }
    for (const a of ['sim-run-pick', 'go-simulation', 'go-robots']) {
      assert(await box.locator(`[data-action="${a}"]`).count() === 1, `${key} 仿真说明缺少按钮 ${a}`);
    }
    await shot(key === 'harness' ? '01-harness-sim-explained' : '02-deployment-sim-explained');
  }
  await go('simulation');
  await page.locator('#main .sim-workbench-card [data-action="sim-run-pick"][data-id="' + quad + '"]').waitFor();
  await shot('03-simulation-workbench');
  // The explanation's "go to simulation" button really goes there.
  await go('deployment');
  await page.locator('#main [data-sim-explain] [data-action="go-simulation"]').click();
  await page.waitForFunction(() => location.hash === '#simulation');

  // ---------------------------------------------------------------- (d) assistant without a model
  await go('agents');
  await page.locator('#main [data-action="engine-work"]').first().click();
  await dlg('.model-why').waitFor();
  const why = await dlg('.dialog-body').innerText();
  for (const want of ['这一步需要先给助手配一个大模型', '阅读机器人资料、编写接入程序和测试', 'NeuxMind 云平台模型', '平台付费模型',
    '自己的模型 API', 'API 地址、模型 ID 和密钥', '不需要助手模型']) {
    assert(why.includes(want), '助手模型说明缺少：' + want);
  }
  assert(!(await dlg('[data-action="engine-cloud-model"]').count()), '未登录云平台时不应出现“使用云平台模型”');
  assert(await dlg('[data-action="go-cloud"]').count() === 1, '缺少“先登录云平台账户”');
  assert(await dlg('button:has-text("暂未上架")').isDisabled(), '平台模型未上架时应显示为不可用，而不是死胡同');
  await shot('04-assistant-model-needed');
  await click('engine-own-model');
  await page.locator('form[data-form=engine-model]').waitFor();
  await click('close');

  // ---------------------------------------------------------------- (b) generated parameter form
  await go('robots');
  await click('detail-robots', quad);
  await dlg('.cap-list').waitFor();
  await click('skillpkg-open', quad);
  await dlg('.gallery-item[data-example="patrol"]').waitFor();
  await click('skillpkg-example', quad + '|patrol');
  await dlg(':text("兼容：可以安装")').first().waitFor();
  await submit();
  await page.locator('form[data-form=sim-run] .param-form').waitFor();
  const raw = page.locator('#dialog .raw-params');
  assert(!(await raw.evaluate(d => d.open)), '高级 JSON 默认应收起');
  assert(!(await page.locator('#dialog [name=parameters]').isVisible()), '默认不应显示 JSON 文本框');
  assert(await page.locator('#dialog [data-param-field="waypoints"] .point-row').count() === 4, '航点应显示为 4 行坐标');
  const loops = page.locator('#dialog [name="param:loops"]');
  assert(await loops.getAttribute('type') === 'number' && await loops.inputValue() === '1', 'loops 应为数字输入，默认 1');
  assert(await page.locator('#dialog [name="param:tolerance"]').inputValue() === '0.15', 'tolerance 应带默认值 0.15');
  assert((await page.locator('#dialog [data-param-field="loops"] small').innerText()).includes('整数'), '应写明参数类型');
  await shot('05-run-form-generated');

  // Bad value: plain explanation, nothing sent.
  await loops.fill('1.5');
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('.form-error:has-text("圈数（loops）：需要整数")').waitFor();   // ux03: human label first
  assert(!(await skillRuns()).length, '参数无效时不应发出运行请求');
  await loops.fill('1');
  // Add and remove a waypoint.
  await page.locator('#dialog [data-ux-point="add"]').click();
  assert(await page.locator('#dialog [data-param-field="waypoints"] .point-row').count() === 5, '添加航点失败');
  await page.locator('#dialog [data-param-field="waypoints"] [data-ux-point="remove"]').last().click();
  assert(await page.locator('#dialog [data-param-field="waypoints"] .point-row').count() === 4, '删除航点失败');

  // Advanced JSON mirrors the form; edits flow back when it is closed.
  const openRaw = async () => { await raw.locator('summary').click(); await page.locator('#dialog .raw-params[data-synced]').waitFor({state: 'attached'}); };
  await openRaw();
  const json = JSON.parse(await page.locator('#dialog [name=parameters]').inputValue());
  assert(json.loops === 1 && json.waypoints.length === 4 && json.tolerance === 0.15, '高级 JSON 与表单不一致：' + JSON.stringify(json));
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify({...json, loops: 2}));
  await shot('06-run-form-advanced-json');
  await raw.locator('summary').click();                       // the toggle event is asynchronous
  await page.waitForFunction(() => document.querySelector('#dialog [name="param:loops"]')?.value === '2', null, {timeout: 3000})
    .catch(() => { throw Error('收起高级 JSON 后表单应同步为 loops=2'); });
  // Broken JSON in advanced mode: plain explanation + collapsible technical detail.
  await openRaw();
  await page.locator('#dialog [name=parameters]').fill('{"loops": 2,');
  await submit();
  await dlg('.form-error .plain-error:has-text("JSON 格式不对")').waitFor();
  await shot('07-run-form-bad-json');
  assert(!(await skillRuns()).length, 'JSON 无效时不应发出运行请求');
  await raw.locator('summary').click();                       // back to the form
  await page.locator('#dialog .raw-params:not([data-synced])').waitFor({state: 'attached'});

  // Safety: without authorization the browser blocks the run.
  await page.locator('#dialog input[name=hardware_authorized]').uncheck();
  await submit();
  await page.waitForTimeout(400);
  assert(!(await skillRuns()).length && await page.locator('form[data-form=sim-run]').count() === 1, '未授权时不应开始运行');
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('#sim-live .sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  const sent = (await skillRuns())[0].parameters;
  assert(sent.loops === 2 && sent.waypoints.length === 4 && sent.tolerance === 0.15, '发出的参数不对：' + JSON.stringify(sent));
  // Stop latches; running is blocked until the on-site reset.
  await click('simlive-stop', quad);
  await dlg('#sim-live .estop-latched').waitFor();
  assert(await dlg('[data-action="sim-run-pick"]').isDisabled(), '急停锁存时不能运行技能');
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('#sim-live .sim-meta:has-text("未锁存")').waitFor();
  await click('close');

  // ---------------------------------------------------------------- installed skill -> project -> application
  await go('skills');
  await page.locator('#main [data-action="ux-adopt"]').click();
  await dlg('[data-action="ux-adopt-one"][data-id="' + quad + '|patrol"]').click();
  await dlg('.contract-line:has-text("巡逻") :text("已加入项目")').waitFor();
  await shot('08-adopt-installed-skill');
  await click('application-add');
  await page.locator('form[data-form=application]').waitFor();
  assert((await dlg('.dialog-body').innerText()).includes('目标选仿真机器人时，这个应用只能做仿真测试'), '创建应用时应提前说明仿真目标的限制');
  await fill('name', '园区巡逻');
  // ux03: steps use the generated form; the advanced JSON is one click away.
  await page.locator('#dialog .step-editor .raw-params summary').click();
  await page.locator('#dialog .step-editor .raw-params[data-synced]').waitFor({state: 'attached'});
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify({waypoints: [[0.3, 0]]}));
  await submit();
  await page.getByText('应用交付清单', {exact: true}).waitFor();
  await click('deliver-check');
  await page.getByRole('heading', {name: '仿真本体：测试结果不能作为现场证据', exact: true}).waitFor();
  const checklist = await dlg('.dialog-body').innerText();
  assert(checklist.includes('需要真机') && checklist.includes('不是故障'), '交付清单应标出哪些阶段需要真机');
  assert(await dlg('.plan-review [data-action="sim-run-pick"]').count() === 1, '交付清单应提供“在仿真机器人上运行技能”');
  assert(await dlg('.plan-review [data-action="go-robots"]').count() === 1, '交付清单应提供“接入真机”');
  assert((await dlg('.plan-review [data-action="deliver-onsite"]').innerText()).includes('不计为现场证据'), '仿真测试按钮应写明不计为现场证据');
  assert(!(await dlg('[data-action=deliver-publish], [data-action=deliver-start]').count()), '仿真本体不应提供发布或启动');
  await shot('09-checklist-sim');
  // A simulation test still runs (labelled), and publishing stays refused by the server.
  await click('deliver-onsite');
  await page.locator('[name=hardware_authorized]').check();
  await submit();
  await page.locator('#dialog').getByText('仿真测试 ·', {exact: false}).first().waitFor({timeout: 30000});
  assert(!(await dlg('[data-action=deliver-publish], [data-action=deliver-start]').count()), '仿真证据不应提供发布或启动');
  await shot('10-checklist-after-sim-test');
  const refused = await page.evaluate(async () => {
    const app = state.applications[0];
    const report = state.evaluations.find(e => e.application_id === app.id && e.scope !== 'static');
    try { await api('deployments', {application_id: app.id, report_id: report.id}); return 'accepted'; } catch (e) { return e.message; }
  });
  assert(refused.includes(SIM), '服务器应拒绝仿真证据发布：' + refused);
  await click('close');
  // The same refusal, shown the way the console surfaces errors: plain first, technical detail collapsed.
  await page.evaluate(m => showError(Error(m)), refused);
  const toastBox = page.locator('#toast.show.error');
  await toastBox.waitFor();
  assert((await toastBox.locator('.plain-error').innerText()).includes('这是有意的安全设计，不是故障'), '错误提示应先用白话解释');
  assert(!(await toastBox.locator('.tech-detail').evaluate(d => d.open)), '技术细节应默认收起');
  assert((await toastBox.locator('.tech-detail code').textContent()).includes(SIM), '技术细节应保留原文');
  await shot('11-error-plain-with-detail');
  await page.evaluate(() => toast('已保存'));
  assert(!(await page.locator('#toast').evaluate(t => t.classList.contains('error'))), '普通提示不应沿用错误样式');

  // A device-side refusal with technical text: explained plainly (skill missing on the robot).
  const explained = await page.evaluate(() => explainError('设备端未通过应用执行前检查（步骤 1（x）技能已安装：技能 x 未安装）'));
  assert(explained.plain.includes('还没有安装到这台机器人上') && explained.detail.includes('未安装'), '执行前检查错误未解释：' + JSON.stringify(explained));
  const english = await page.evaluate(() => explainError('Connection refused'));
  assert(english.plain.includes('连不上') && english.detail === 'Connection refused', '英文技术错误未解释');

  // Deliver picker labels simulated targets.
  await go('deployment');
  await page.locator('#main [data-action="deploy-pick"]').first().click();
  assert((await dlg('.dialog-body').innerText()).includes('只能做仿真测试，不能发布到设备'), '应用选择列表应标出仿真目标');
  await click('close');

  // Real robots still cannot be run directly (server rule unchanged).
  const direct = await page.evaluate(async () => {
    try { await api('robots/run-skill', {robot_id: 'robot-nope', skill_id: 'patrol', request_id: 'r-ux02', parameters: {}, hardware_authorized: false}); return 'accepted'; }
    catch (e) { return e.message; }
  });
  assert(direct !== 'accepted', '未授权的直接运行不应被接受');

  assert(!errors.length, '页面错误：' + errors.join(' | '));
  await browser.close();
  console.log('ux02 browser acceptance passed; screenshots in ' + shots);
})().catch(err => { console.error(err); process.exit(1); });
