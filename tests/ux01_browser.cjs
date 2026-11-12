const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for ux01: a first-time user gets a working (simulated) robot and watches it
// run a skill, entirely inside the app -- no terminal, no tokens, no typed paths.
// Start tests/ux01_browser_fixture.py first.
// Usage: node tests/ux01_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts';

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
  const dialogText = text => dlg(`:text("${text}")`).first().waitFor();
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const robotsOf = () => page.evaluate(() => state.robots.map(r => ({id: r.id, name: r.name, sim: r.sim, status: r.status})));

  // ---------------------------------------------------------------- 0. fresh workspace
  await page.goto(cfg.url + '/#robots');
  await gateSignIn(page, {name: '第一次使用', email: 'ux01@example.test', password: 'ux01-password-123'});
  await page.locator('.robot-entry').waitFor();
  const entry = await page.locator('#main').innerText();
  for (const text of ['添加仿真机器人', '通过 SSH 调查真机', '连接已在运行的机器人', '没有真机，先试一试']) {
    assert(entry.includes(text), '我的机器人页缺少：' + text);
  }
  assert(!entry.includes('SSH 接入并调查') && !entry.includes('接入已有适配服务'), '旧的入口按钮文字仍在');
  await shot('01-robots-fresh');

  // ---------------------------------------------------------------- 1. add a simulated quadruped
  await click('sim-add');
  await page.locator('form[data-form=sim-add]').waitFor();
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await shot('02-sim-add');
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});      // capability table of the new robot
  let robots = await robotsOf();
  assert(robots.length === 1 && robots[0].sim && robots[0].status === 'connected', '仿真四足未连接：' + JSON.stringify(robots));
  const quad = robots[0].id;
  const caps = await dlg('.dialog-body').innerText();
  for (const text of ['导航到目标', '自己走到你指定的目标位置', '仿真', '已安装技能包']) {
    assert(caps.includes(text), '能力表缺少：' + text);
  }
  // ux04 (Q5-12): the capability ID stays available, collapsed under 技术细节.
  const capTech = await dlg('.cap-list').evaluate(el => [...el.querySelectorAll('.cap-tech')].map(d => d.textContent).join(' '));
  assert(capTech.includes('base.navigate_to') && !caps.includes('base.navigate_to'), '能力 ID 应收在“技术细节”里');
  assert(!JSON.stringify(await page.evaluate(() => state.robots)).includes('"token"'), '页面状态中不应出现令牌');
  await shot('03-capabilities');

  // ---------------------------------------------------------------- 2. install patrol from the gallery
  await click('skillpkg-open', quad);
  await dlg('.gallery-item[data-example="patrol"]').waitFor();
  const patrolCard = await dlg('.gallery-item[data-example="patrol"]').innerText();
  const waveCard = await dlg('.gallery-item[data-example="wave"]').innerText();
  assert(patrolCard.includes('适用于这台机器人'), '巡逻应适用于四足');
  assert(waveCard.includes('这台机器人不能用') && waveCard.includes('机械臂关节位置'), '挥手应不适用于四足并说明原因');
  assert(await dlg('[data-action="skillpkg-example"][data-id="' + quad + '|wave"]').isDisabled(), '不兼容技能的安装按钮应禁用');
  assert(await dlg('.folder-install').count() === 1, '缺少“从文件夹安装（高级）”');
  await shot('04-gallery');
  await click('skillpkg-example', quad + '|patrol');
  await dialogText('兼容：可以安装');
  await shot('05-patrol-compatible');
  await submit();

  // ---------------------------------------------------------------- 3. authorize and run
  await page.locator('form[data-form=sim-run]').waitFor();
  await dialogText('技能已安装');
  assert((await page.locator('#dialog [name=parameters]').inputValue()).includes('waypoints'), '巡逻参数未预填');
  await shot('06-run-authorize');
  await submit();                                  // not authorized yet: the browser blocks the form
  await page.waitForTimeout(400);
  assert(await page.locator('form[data-form=sim-run]').count() === 1, '未授权时不应开始运行');
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();

  // ---------------------------------------------------------------- 4. watch it move
  await dlg('#sim-live .sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  const poseText = () => dlg('#sim-live .pose-line').innerText();
  const before = await poseText();
  await page.waitForTimeout(2500);
  const after = await poseText();
  assert(before !== after, '实时状态中的位置没有变化：' + before);
  assert(await dlg('#sim-live .sim-trail').count() === 1, '俯视图没有画出路线');
  await shot('07-live-running');

  // ---------------------------------------------------------------- 5. stop: latches the e-stop
  await click('simlive-stop', quad);
  await dlg('#sim-live .estop-latched').waitFor();
  const stopped = await dlg('#sim-live').innerText();
  assert(stopped.includes('急停已锁存'), '停止后应显示急停已锁存');
  await dlg('#sim-live .sim-run:not([data-run-status="running"])').waitFor();
  assert(await dlg('[data-action="sim-run-pick"]').isDisabled(), '急停锁存时不能运行技能');
  const p1 = await poseText();
  await page.waitForTimeout(1500);
  assert(p1 === await poseText(), '停止后机器人仍在移动');
  await shot('08-live-stopped');

  // ---------------------------------------------------------------- 6. reset (on-site confirmation)
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('#sim-live .sim-meta:has-text("未锁存")').waitFor();
  assert(!(await dlg('[data-action="sim-run-pick"]').isDisabled()), '解除急停后应可以再次运行');
  await shot('09-live-reset');
  await click('close');

  // ---------------------------------------------------------------- 7. a simulated arm: joints move
  await page.goto(cfg.url + '/#robots');
  await click('sim-add');
  await page.locator('#dialog input[name=kind][value=arm]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  robots = await robotsOf();
  const arm = robots.find(r => r.sim?.kind === 'arm').id;
  await click('skillpkg-open', arm);
  await dlg('.gallery-item[data-example="wave"]:has-text("适用于这台机器人")').waitFor();
  const otherLine = await dlg('.gallery-item[data-example="patrol"]').innerText();
  assert(otherLine.includes('仿真四足机器人 ✓ 可用'), '技能卡片应显示其他机器人的兼容性：' + otherLine);
  await click('skillpkg-example', arm + '|wave');
  await dialogText('兼容：可以安装');
  await submit();
  await page.locator('form[data-form=sim-run]').waitFor();
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('#sim-live .sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  const j1 = await dlg('#sim-live .joint-list').innerText();
  await page.waitForTimeout(1500);
  assert(j1 !== await dlg('#sim-live .joint-list').innerText(), '关节值没有变化');
  await shot('10-arm-live');
  await click('simlive-stop', arm);
  await dlg('#sim-live .estop-latched').waitFor();
  await click('close');

  // ---------------------------------------------------------------- 8. robots page with two simulated robots
  await page.goto(cfg.url + '/#robots');
  await page.locator('.sim-card').nth(1).waitFor();
  await shot('11-robots-two-sims');
  const sims = await page.evaluate(() => fetch('/fixture/sims').then(r => r.json()));
  assert(sims.sims.length === 2 && sims.sims.every(s => s.alive), '应有两个仿真进程：' + JSON.stringify(sims));

  // ---------------------------------------------------------------- 9. cloud page explains itself
  await page.goto(cfg.url + '/#cloud');
  await page.locator('.cloud-unreachable').first().waitFor();
  const cloudText = await page.locator('#main').innerText();
  assert(cloudText.includes('这台电脑上没有运行 NeuxMind 云平台') && cloudText.includes('不登录也可以'),
    '云平台页没有用平实的话说明连不上：' + cloudText);
  await shot('12-cloud-page');
  await click('cloud-address');
  const defaultUrl = await page.evaluate(() => state.cloud_account.default_base_url);
  assert(defaultUrl && (await page.locator('#dialog [name=base_url]').inputValue()) === defaultUrl, '云平台地址未预填');
  await shot('13-cloud-address-prefilled');
  await click('close');

  assert(!errors.length, 'JS 错误：' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, quad, arm, shots}));
  await browser.close();
})().catch(async err => {
  console.error(err);
  process.exit(1);
});
