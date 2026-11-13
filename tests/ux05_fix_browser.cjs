const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for the round-07 review fixes on the desktop (real runtimes, nothing stubbed except §4):
//   Q7-04  a float option whose value is whole (1.0, "快") is selectable and reaches the skill as a float;
//   Q7-03  a robot that keeps moving after repeated stops ("safety.stop_not_obeyed", real latch guard) is
//          announced in a persistent banner under the top bar on any page and at the top of any dialog,
//          naming the robot, with a way to it -- for a REAL robot (added by address and token) as well as a
//          simulated one; the real robot's detail page shows the latch guard; the banner clears on reset;
//   Q7-11  for the simulated robot the banner says "仿真机器人：点“重启仿真机器人”或关闭仿真", never the
//          hardware e-stop;
//   Q7-05  the latch guard's field calibration is shown on the robot's detail page (real runtime), and a
//          runtime-reported loosened calibration (`relaxed`) as a warning (stubbed if the runtime has no flag).
// Start tests/ux01_browser_fixture.py with NX_FIXTURE_IGNORE_STOP=1 first (simulated bodies only).
// Usage: node tests/ux05_fix_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux05_fix';

function writeSkill() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nx-ux05fix-skill-'));
  // "value": 1.0 written literally, as a skill author would
  const json = JSON.stringify({
    schema: 'skill.package.v1', id: 'ux05fix_float', version: '0.1.0', title: '速度档位',
    description: '只返回收到的参数和类型，不会让机器人运动。', entry: 'skill.py:run', requires: [{id: 'system.health'}],
    parameters: [{name: 'speed', type: 'float', title: '速度', unit: 'm/s', default: 0.5,
      options: [{value: 0.5, label: '慢'}, {value: 1.0, label: '快'}, {value: 1.5, label: '很快'}]}],
    timeout_s: 30,
  }, null, 2).replace('"value": 1,', '"value": 1.0,');
  if (!json.includes('"value": 1.0,')) throw Error('skill.json 应写成 1.0');
  fs.writeFileSync(path.join(dir, 'skill.json'), json);
  fs.writeFileSync(path.join(dir, 'skill.py'), 'def run(ctx, **params):\n    return {"params": params, "types": {k: type(v).__name__ for k, v in params.items()}}\n');
  return dir;
}

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  if (!cfg.real?.ignore || !cfg.real?.relaxed) throw Error('夹具需要 NX_FIXTURE_IGNORE_STOP=1');
  fs.mkdirSync(shots, {recursive: true});
  const skillDir = writeSkill();
  const browser = await chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  page.setDefaultTimeout(20000);
  globalThis.__page = page;
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text());
  });
  const t0 = Date.now();
  const log = m => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${m}`);
  const shot = name => page.screenshot({path: path.join(shots, name + '.png')});
  const dlg = sel => page.locator('#dialog[open] ' + sel);
  const fill = (k, v) => page.locator(`#dialog [name="${k}"]`).fill(String(v));
  const submit = () => page.locator('#dialog button[type=submit]').first().click();
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const go = async hash => { await page.goto(cfg.url + '/#' + hash); await page.waitForTimeout(400); };
  const closeDialog = async () => { await dlg('[data-action="close"]').first().click(); await page.locator('#dialog').waitFor({state: 'hidden'}); };
  const fixture = p => page.evaluate(async u => (await fetch(u)).json(), cfg.url + p);
  const auth = () => page.locator('#dialog input[name=hardware_authorized]');
  const top = page.locator('#safety-banner');

  // ---------------------------------------------------------------- setup: a user, one simulated quadruped, two "real" robots
  await go('overview');
  await gateSignIn(page, {name: 'ux05-fix 用户', email: 'ux05fix@example.test', password: 'ux05fix-password-123'});
  await go('overview');
  await page.evaluate(() => refresh(true));
  await page.locator('#main .first-run [data-action="sim-add"]').click();
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  const quad = await page.evaluate(() => state.robots[0].id);
  await closeDialog();
  const addReal = async (name, r) => {
    await go('robots');
    await page.locator('#main [data-action="robot-add"]').first().click();
    await fill('name', name);
    await page.locator('#dialog select[name=family]').selectOption('quadruped');
    await fill('endpoint', r.endpoint);
    await fill('token', r.token);
    await submit();
    await page.locator('#dialog').waitFor({state: 'hidden'}).catch(() => {});
    const id = await page.evaluate(async n => { await refresh(true); return state.robots.find(x => x.name === n).id; }, name);
    await page.evaluate(async rid => { await api('robots/connect', {robot_id: rid}); await refresh(true); }, id);
    return id;
  };
  const real = await addReal('仓库巡检狗 A', cfg.real.ignore);
  const relaxed = await addReal('园区巡检狗 B', cfg.real.relaxed);
  const robots = await page.evaluate(() => state.robots.map(r => ({id: r.id, status: r.status, sim: Boolean(r.sim), simulated: r.manifest?.simulated})));
  for (const r of robots) assert(r.status === 'connected', '机器人应已连接：' + JSON.stringify(r));
  assert(robots.filter(r => !r.sim && r.simulated === false).length === 2, '两台应按真机处理：' + JSON.stringify(robots));
  log('setup done');

  // ---------------------------------------------------------------- Q7-04: a whole-valued float option, real runtime
  await page.evaluate(id => robotDetailOpen(id), quad);
  await dlg('.cap-list').waitFor();
  await dlg(`[data-action="skillpkg-open"][data-id="${quad}"]`).first().click();
  await dlg('details.folder-install').waitFor();
  await dlg('details.folder-install summary').click();
  await dlg('details.folder-install input[name=path]').fill(skillDir);
  await dlg('details.folder-install button[type=submit]').click();
  await dlg('.preview-params').waitFor();
  await submit();
  await page.locator('form[data-form=sim-run] .param-form').waitFor({timeout: 30000});
  const labels = await page.locator('#dialog select[name="param:speed"] option').allInnerTexts();
  assert(['慢', '快', '很快'].every(l => labels.includes(l)), '下拉框应显示档位名：' + labels);
  await page.locator('#dialog select[name="param:speed"]').selectOption({label: '快'});
  await shot('01-float-option-pick-fast');
  await auth().check();
  await submit();
  await dlg('#sim-live').waitFor({timeout: 20000});
  const op = await page.evaluate(async () => { await refresh(true); return state.operations.filter(o => o.kind === 'skill_run').pop(); });
  assert(op.status === 'accepted', '设备应接受 1.0（快）：' + JSON.stringify(op));
  await page.waitForFunction(() => /已完成/.test(document.querySelector('#sim-live .sim-run')?.textContent || ''), null, {timeout: 30000});
  const result = await page.evaluate(async id => (await api('robots/live', {robot_id: id, project_id: projectId})).run?.result, quad);
  assert(result?.params?.speed === 1 && result?.types?.speed === 'float', '技能应收到浮点 1.0：' + JSON.stringify(result));
  await dlg('#sim-live details summary:has-text("技能返回的结果")').click();
  await shot('02-float-option-accepted');
  await closeDialog();
  log('Q7-04 ok');

  // ---------------------------------------------------------------- Q7-05: calibration on the detail page (real runtime)
  await go('robots');
  await page.locator(`#main [data-action="detail-robots"][data-id="${relaxed}"]`).first().click();
  await dlg('#robot-safety [data-latch-guard="armed"]').waitFor({timeout: 15000});
  await dlg('#robot-safety [data-latch-calibrated], #robot-safety [data-latch-relaxed]').first().waitFor({timeout: 15000});
  const calText = await dlg('#robot-safety').innerText();
  assert(calText.includes('急停未锁存') && /位置（pos_m = 0\.2/.test(calText) && calText.includes('机器人清单'), '应显示现场标定：' + calText);
  const runtimeRelaxed = await dlg('#robot-safety [data-latch-relaxed]').count() > 0;
  log('runtime reports relaxed: ' + runtimeRelaxed);
  await shot('03-detail-calibration');
  await closeDialog();

  // ---------------------------------------------------------------- Q7-03 / Q7-11: drivers that ignore stop, real latch guard
  await fixture('/fixture/ignore?on=1');
  const stopped = await page.evaluate(async ids => Promise.all(ids.map(id => api('robots/stop', {robot_id: id, project_id: projectId}))), [real, quad]);
  assert(stopped.every(s => s.status === 'accepted'), '停止应被接受：' + JSON.stringify(stopped));
  // wait somewhere else in the app: the banner must find the user there
  await go('models');
  await page.locator(`#safety-banner [data-safety-alert="${real}"]`).waitFor({timeout: 90000});
  log('real robot banner');
  await page.locator(`#safety-banner [data-safety-alert="${quad}"]`).waitFor({timeout: 30000});
  log('sim robot banner');
  const realRow = await page.locator(`#safety-banner [data-safety-alert="${real}"]`).innerText();
  assert(realRow.includes('请立即使用硬件急停') && realRow.includes('仓库巡检狗 A') && realRow.includes('硬件急停（或切断动力）'), '真机横幅：' + realRow);
  assert(/已再停 \d+ 次/.test(realRow), '真机横幅应有再停次数：' + realRow);
  const simRow = await page.locator(`#safety-banner [data-safety-alert="${quad}"]`).innerText();
  assert(simRow.includes('仿真机器人：点“重启仿真机器人”或关闭仿真') && !simRow.includes('硬件急停'), '仿真横幅（Q7-11）：' + simRow);
  assert(await page.locator(`#safety-banner [data-safety-alert="${quad}"] [data-action="sim-restart"]`).count() === 1, '仿真横幅应有“重启仿真机器人”');
  assert(await page.locator(`#safety-banner [data-safety-alert="${relaxed}"]`).count() === 0, '服从停止的机器人不应出现在横幅里');
  await shot('04-global-banner-models-page');
  // still there on other pages and after a reload
  await go('overview');
  await top.locator(`[data-safety-alert="${real}"]`).waitFor({timeout: 10000});
  await go('robots');
  await top.locator(`[data-safety-alert="${real}"]`).waitFor({timeout: 10000});
  const rowsText = await page.locator('#main').innerText();
  assert(rowsText.includes('停止后仍在移动'), '机器人列表应有“停止后仍在移动”标记');
  await shot('05-global-banner-robots-page');
  // any dialog repeats the banner at its top (the page underneath is inert)
  await page.locator('#main [data-action="robot-add"]').first().click();
  await dlg('.safety-dialog-slot [data-safety-alert="' + real + '"]').waitFor({timeout: 5000});
  await shot('06-banner-over-dialog');
  await closeDialog();

  // the banner's button opens the real robot's detail page: latch guard and the alert
  await top.locator(`[data-safety-alert="${real}"] [data-action="detail-robots"]`).click();
  await dlg('#robot-safety [data-safety-alert]').waitFor({timeout: 15000});
  await dlg('#robot-safety [data-latch-guard="alert"]').waitFor({timeout: 15000});
  const detail = await dlg('#robot-safety').innerText();
  assert(detail.includes('急停已锁存') && detail.includes('急停期间检测到机器人仍在移动，已再次停止') && detail.includes('请立即使用硬件急停'),
    '真机详情应显示锁存守护：' + detail);
  assert(await dlg(`.safety-dialog-slot [data-safety-alert="${real}"]`).count() === 0, '详情页里不应在顶部重复这台的横幅');
  assert(await dlg(`.safety-dialog-slot [data-safety-alert="${quad}"]`).count() === 1, '其他机器人的横幅仍应在对话框顶部');
  await shot('07-real-detail-latch-guard');

  // released on site: the driver obeys again, the operator confirms and resets -> the banner goes
  await fixture('/fixture/ignore?on=0');
  await page.waitForTimeout(1500);
  await dlg('#robot-safety [data-action="robot-reset"]').click();
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await page.locator(`#safety-banner [data-safety-alert="${real}"]`).waitFor({state: 'detached', timeout: 15000});
  log('real robot banner cleared');
  assert(await page.locator(`#safety-banner [data-safety-alert="${quad}"]`).count() === 1, '仿真机器人的横幅应保留到它解除急停');

  // the simulated robot: its live view leads with the same words; the dialog does not repeat it on top
  await page.evaluate(id => simLiveOpen(id), quad);
  await dlg('#sim-live [data-stop-not-obeyed]').waitFor({timeout: 15000});
  const liveText = await dlg('#sim-live [data-stop-not-obeyed]').innerText();
  assert(liveText.includes('仿真机器人：点“重启仿真机器人”或关闭仿真') && !liveText.includes('硬件急停'), '实时状态横幅（Q7-11）：' + liveText);
  await page.waitForTimeout(500);
  assert(await dlg(`.safety-dialog-slot [data-safety-alert="${quad}"]`).count() === 0, '实时状态里不应重复同一台的横幅');
  const guard = await dlg('#sim-live [data-latch-guard]').innerText();
  assert(!guard.includes('硬件急停'), '仿真锁存守护不应提硬件急停：' + guard);
  await shot('08-sim-live-banner');
  await dlg('#sim-live [data-action="robot-reset"]').first().click();
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await page.locator(`#safety-banner [data-safety-alert="${quad}"]`).waitFor({state: 'detached', timeout: 20000});
  assert(await top.innerText() === '', '全部解除后不应再有横幅');
  log('sim banner cleared');
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(300);

  // ---------------------------------------------------------------- §4 (stubbed only if the runtime has no flag): loosened calibration
  if (!runtimeRelaxed) {
    await page.route('**/api/robots/safety', async route => {
      const res = await route.fetch();
      const v = await res.json();
      for (const e of v.robots || []) {
        if (e.robot_id === relaxed) e.relaxed = {relaxed: true, fields: ['pos_m', 'yaw_rad'], sources: ['manifest'], values: {pos_m: 0.2, yaw_rad: 0.2}, notes: []};
      }
      v.relaxed = [relaxed];
      await route.fulfill({response: res, json: v});
    });
  }
  await go('robots');
  await page.waitForFunction(() => /锁存守护已放宽/.test(document.querySelector('#main')?.textContent || ''), null, {timeout: 15000});
  await page.locator(`#main [data-action="detail-robots"][data-id="${relaxed}"]`).first().click();
  await dlg('#robot-safety [data-latch-relaxed]').waitFor({timeout: 15000});
  const relaxedText = await dlg('#robot-safety [data-latch-relaxed]').innerText();
  assert(relaxedText.includes('锁存守护的标定被放宽了') && /位置（pos_m = 0\.2(，默认 0\.05)?）/.test(relaxedText), '放宽警告：' + relaxedText);
  await shot('09-relaxed-warning' + (runtimeRelaxed ? '' : '-stubbed'));
  if (!runtimeRelaxed) await page.unroute('**/api/robots/safety');
  await closeDialog();

  // the watch stays cheap: no more than a few requests per second from this page
  const counts = [];
  page.on('request', r => { if (r.url().endsWith('/api/robots/safety')) counts.push(Date.now()); });
  await page.waitForTimeout(10000);
  assert(counts.length <= 7, '10 秒内 robots/safety 请求过多：' + counts.length);

  assert(errors.length === 0, '页面错误：' + errors.join(' | '));
  await fixture('/fixture/ignore?on=0');
  await browser.close();
  fs.rmSync(skillDir, {recursive: true, force: true});
  console.log(JSON.stringify({ok: true, runtime_relaxed: runtimeRelaxed, safety_requests_10s: counts.length, seconds: (Date.now() - t0) / 1000}));
})().catch(async e => {
  console.error(e);
  try {
    const page = globalThis.__page;
    if (page) {
      await page.screenshot({path: path.join(shots, 'failure.png')});
      console.error(await page.evaluate(() => JSON.stringify({safety: typeof safetyData !== 'undefined' ? safetyData : null,
        box: document.querySelector('#robot-safety')?.outerHTML?.slice(0, 1500)})));
    }
  } catch (x) { console.error('no failure details:', x.message); }
  process.exit(1);
});
