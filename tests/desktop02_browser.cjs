// Browser acceptance for round 02: capability table, skill package install (compatible and
// incompatible), cloud login and wallet, asynchronous on-site test in progress, start disabled
// while the e-stop is latched. Start tests/desktop02_browser_fixture.py first.
// Usage: node tests/desktop02_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-desktop02-qa.json';
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
  // Expected HTTP refusals (401 before login, 400 for refused requests) are not JS errors.
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
  });
  const taken = [];
  const shot = async (name, both = false) => {
    await page.waitForTimeout(150);
    await page.screenshot({path: path.join(shots, name + '-1440.png')});
    taken.push(name + '-1440.png');
    if (both) {
      await page.setViewportSize(SMALL);
      await page.waitForTimeout(250);
      await page.screenshot({path: path.join(shots, name + '-900.png')});
      taken.push(name + '-900.png');
      await page.setViewportSize(WIDE);
    }
  };
  const dlg = sel => page.locator('#dialog[open] ' + sel);
  const click = async (action, id) => {
    const sel = `[data-action="${action}"]` + (id ? `[data-id="${id}"]` : '');
    const inDialog = dlg(sel);
    await (await inDialog.count() ? inDialog : page.locator(sel)).first().click();
  };
  const fill = (k, v) => page.locator(`#dialog [name="${k}"]`).fill(String(v));
  const submit = () => page.locator('#dialog[open] button[type=submit]').click();
  const dialogText = text => dlg(`:text("${text}")`).first().waitFor({timeout: 15000});
  const fixture = p => page.evaluate(p => fetch(p).then(r => r.json()), p);
  const assert = (ok, message) => { if (!ok) throw Error(message); };

  // ---------------------------------------------------------------- account and project
  await page.goto(cfg.url);
  await click('journey-start');
  await fill('name', '第 02 轮验收');
  await fill('email', 'desktop02@example.test');
  await fill('password', 'fixture-password-123');
  await submit();
  await page.locator('form[data-form=project]').waitFor();
  await fill('name', '园区巡检');
  await fill('objective', '四足机器人每天巡检园区，发现异常时停止并报告');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});

  const ids = await page.evaluate(async cfg => {
    const robot = async (name, endpoint, token) => {
      const r = await api('robots', {project_id: projectId, name, family: 'quadruped', endpoint, token});
      await api('robots/connect', {robot_id: r.id});
      return r.id;
    };
    const device = await robot('巡检四足 B（协议替身）', cfg.device, cfg.device_token);
    const sim = await robot('仿真四足 A', cfg.runtime, cfg.runtime_token);
    const skill = await api('skills', {project_id: projectId, name: '园区巡逻', version: '0.1.0',
      requires: ['base.navigate_to'], entrypoint: 'patrol', success_condition: '依次到达全部巡检点',
      stop_condition: '超时、定位丢失或现场停止', model_ids: []});
    const app = await api('applications', {project_id: projectId, name: '每日园区巡检', version: '1.0',
      robot_id: device, steps: [{skill_id: skill.id, parameters: {waypoints: [[2, 0], [2, 2]]}, timeout_s: 120}]});
    await refresh(true);
    return {sim, device, app: app.id};
  }, cfg);

  // ---------------------------------------------------------------- 1. capability table
  await page.goto(cfg.url + '/#robots');
  await page.locator(`[data-action="detail-robots"][data-id="${ids.sim}"]`).waitFor();
  await shot('01-robots');
  await click('detail-robots', ids.sim);
  await dlg('.cap-list').waitFor();
  const caps = await dlg('.dialog-body').innerText();
  for (const text of ['它能做什么', '底盘速度', '会产生运动 · 需现场授权', '不产生运动',
    '导航到目标', '持续动作', '仿真', '已安装技能包']) {
    assert(caps.includes(text), '能力表缺少：' + text);
  }
  // ux04 (Q5-12): IDs and declared parameters are collapsed under 技术细节, still there.
  const capTech = await dlg('.cap-list').evaluate(el => [...el.querySelectorAll('.cap-tech')].map(d => d.textContent).join(' '));
  for (const text of ['base.velocity', 'max_vx', '1.0 m/s']) {
    assert(capTech.includes(text), '技术细节缺少：' + text);
  }
  await shot('02-capabilities', true);
  await dlg('.cap-list').evaluate(el => el.scrollIntoView());
  await shot('02b-capabilities-list');

  // ---------------------------------------------------------------- 2. skill packages
  await click('skillpkg-open', ids.sim);
  // ux01: the dialog opens on the example gallery; a folder is the advanced option.
  await dlg('.folder-install summary').click();
  await fill('path', cfg.skills.wave);
  await submit();
  await dialogText('不兼容：不能安装');
  const incompatible = await dlg('.dialog-body').innerText();
  assert(incompatible.includes('「机械臂关节位置」（arm.joint_position）'), '不兼容原因缺少机械臂关节位置');
  assert(incompatible.includes('「夹爪」'), '不兼容原因缺少夹爪');
  assert(await dlg('button[type=submit]').count() === 0, '不兼容时不应出现安装按钮');
  await shot('03-skill-incompatible', true);
  assert((await fixture('/fixture/state')).runtime_skills.length === 0, '不兼容技能不应发送到机器人');

  await click('skillpkg-back');
  await fill('path', cfg.skills.fast);
  await submit();
  await dialogText('不兼容：不能安装');
  const violation = await dlg('.dialog-body').innerText();
  assert(violation.includes('技能需要至少 2.0 m/s，这台机器人声明为 1.0 m/s'), '参数不满足的说明缺失');
  await shot('03b-skill-parameter-violation');

  await click('skillpkg-back');
  await fill('path', cfg.skills.patrol);
  await submit();
  await dialogText('兼容：可以安装');
  await shot('04-skill-compatible', true);
  await submit();
  await dlg('.cap-list').waitFor();
  await dialogText('巡逻');
  const installed = await dlg('.dialog-body').innerText();
  assert(installed.includes('patrol@0.1.0'), '已安装列表缺少 patrol');
  assert((await fixture('/fixture/state')).runtime_skills.includes('patrol'), '运行时未安装 patrol');
  await dlg('.badge-count').nth(1).evaluate(el => el.scrollIntoView());
  await shot('05-skill-installed');
  await click('close');

  // ---------------------------------------------------------------- 3. cloud account
  await page.goto(cfg.url + '/#cloud');
  await page.getByRole('heading', {name: '云平台账户', exact: true}).waitFor();
  await shot('06-cloud-empty');
  await click('cloud-address');
  await fill('base_url', 'http://cloud.example.com');
  await submit();
  await dlg('.form-error:text("https")').waitFor();
  await fill('base_url', cfg.cloud);
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await click('cloud-register');
  await fill('name', '园区团队');
  await fill('email', 'desktop02-cloud@example.test');
  await fill('password', 'cloud-password-123');
  await submit();
  await dialogText('仅开发环境');
  const devText = await dlg('.dev-token').innerText();
  assert(/nxv_/.test(devText), '开发令牌未显示');
  await shot('07-cloud-dev-token', true);
  const stateText = await page.evaluate(() => JSON.stringify(state));
  assert(!/nxs_/.test(stateText), '会话令牌出现在前端状态中');
  await click('cloud-verify-dev');
  await page.locator('.status:text("邮箱已验证")').waitFor({timeout: 15000});
  await page.locator('.cloud-wallet').waitFor({timeout: 15000});
  await page.locator('select[data-cloud-robot]').selectOption(ids.sim);
  await page.locator('.card h3:text("四足导航策略（测试替身）")').waitFor({timeout: 15000});
  const cloudPage = await page.locator('#main').innerText();
  assert(cloudPage.includes('通用对话模型'), '通用模型未列出');
  assert(!cloudPage.includes('机械臂抓取策略'), '不适合四足的模型不应列出');
  assert(cloudPage.includes('按标准能力清单筛选'), '未说明按 v2 清单筛选');
  await shot('08-cloud-wallet-models', true);
  await page.locator('.section-head:has-text("适合这台机器人的模型")').evaluate(el => el.scrollIntoView());
  await shot('08b-cloud-models', true);

  // ---------------------------------------------------------------- 4. async on-site test
  await page.evaluate(id => deliveryOpen(id), ids.app);
  await click('deliver-check', ids.app);
  await dialogText('下一步：现场测试');
  await click('deliver-onsite', ids.app);
  await page.locator('#dialog [name=hardware_authorized]').check();
  await submit();
  await dialogText('现场测试进行中');
  await page.waitForTimeout(3200);                       // several status polls while it runs
  const running = await dlg('.onsite-running').innerText();
  assert(running.includes('最近查询'), '未显示轮询状态');
  const stopButton = dlg('.onsite-running [data-action="livetest-stop"]');
  assert(await stopButton.isEnabled(), '测试进行中“请求停止”必须可用');
  await shot('09-onsite-running', true);
  await fixture('/fixture/finish');
  await dialogText('下一步：发布到机器人');                 // updated by the poller, no click
  await shot('10-onsite-passed');

  // Publish, then a stop latches the e-stop: “准备启动工作” is disabled with the reason.
  await click('deliver-publish', ids.app);
  await submit();
  await dialogText('应用已发布，可以准备启动');
  assert(await dlg('[data-action="deliver-start"]').first().isEnabled(), '未锁存时启动应可用');
  await dlg('[data-action="operate-stop"]').first().click();
  await submit();
  await page.evaluate(id => deliveryOpen(id), ids.app);
  await dialogText('急停已锁存，暂时不能启动');
  const start = dlg('[data-action="deliver-start"]').first();
  assert(await start.isDisabled(), '急停锁存时“准备启动工作”必须禁用');
  await dialogText('需先解除急停');
  await shot('11-estop-start-disabled', true);
  await click('close');
  await page.goto(cfg.url + '/#deployment');
  await page.locator('.card-note:text("急停已锁存")').waitFor();
  assert(await page.locator('[data-action="operate-start"]').first().isDisabled(), '部署页启动按钮必须禁用');
  await shot('12-deployment-latched');

  // Reset on site, then a second test stopped while running ends as failed.
  await page.evaluate(id => deliveryOpen(id), ids.app);
  await click('robot-reset', ids.device + ':' + ids.app);
  await page.locator('#dialog [name=hardware_authorized]').check();
  await submit();
  await dialogText('应用已发布，可以准备启动');
  assert(await dlg('[data-action="deliver-start"]').first().isEnabled(), '解除急停后启动应可用');
  await click('deliver-onsite', ids.app);
  await page.locator('#dialog [name=hardware_authorized]').check();
  await submit();
  await dialogText('现场测试进行中');
  await dlg('.onsite-running [data-action="livetest-stop"]').click();
  await dialogText('现场测试未通过，先查看问题');
  await dialogText('急停已锁存');
  await shot('13-onsite-stopped');
  const final = await fixture('/fixture/state');
  assert(final.latched, '停止后设备应锁存');
  assert(final.device_calls.filter(c => c === 'POST /v1/evaluate').length === 2, '每次测试只应发送一次 evaluate');

  assert(errors.length === 0, 'JS 异常：' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, js_errors: errors.length, screenshots: taken}));
  await browser.close();
})().catch(async err => {
  console.error(err);
  process.exit(1);
});
