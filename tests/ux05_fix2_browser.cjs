const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for the round-07 re-review fixes on the desktop (ux05-fix2, ux05-fix3), real runtimes throughout.
// The runtime reports `capabilities.reset_expire`, `reset_expected_s` and the one-shot drift notice itself; only the
// "older runtime" wording is checked by stubbing `reset_expire` away on the desktop API (page.route).
//   Q7b-04  a skill declaration typed into "登记技能" with a float option 1.0 ("快"), an application step that
//           picks it: the real runtime's /v1/validate, /v1/evaluate and /v1/deploy accept it (it rejects 1);
//   Q7b-07  mixed calibration (pos_m stricter, yaw_drift_rad_s / escalate_after looser): only the looser keys
//           are called "放宽" (from the runtime's relaxed_keys); pos_m is listed as calibration;
//   Q7b-05/06/08  a real robot whose reset hangs (manifest safety.reset_expected_s = 7 -> expire_s 12, wait 16 s):
//           the real runtime supports reset_expire, so after the limit the outcome says *this* reset is void and
//           the latch stays -- and the runtime does keep the latch after the late return. With `reset_expire`
//           stubbed to false / missing the form and outcome say plainly a late reset may still release the latch
//           (never "已作废") and offer 停止;
//   fix3    the runtime's safety.latched_drift_notice (body slid >5 cm while latched, /fixture/creep): shown once
//           per latch in the latch-guard panel of the sim live view (two latches) and a real robot's detail
//           page, worded as the runtime sent it; gone after the reset;
//   Q7b-10  a robot of project B that ignores stop is announced while the user is in project A, with the
//           project's name and a way there; polling goes on in a hidden window while a robot is latched.
// Start tests/ux01_browser_fixture.py with NX_FIXTURE_IGNORE_STOP=1 NX_FIXTURE_FIX2=1 first.
// Usage: node tests/ux05_fix2_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux05_fix2';

const PARAMS = [{name: 'speed', type: 'float', title: '速度', unit: 'm/s', default: 0.5,
  options: [{value: 0.5, label: '慢'}, {value: 1.0, label: '快'}, {value: 1.5, label: '很快'}]}];
// What a person types (and what skill.json says): "value": 1.0 literally.
const PARAMS_TEXT = JSON.stringify(PARAMS, null, 1).replace(/"value": 1,/, '"value": 1.0,');

function writeSkill() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nx-ux05fix2-skill-'));
  const json = JSON.stringify({
    schema: 'skill.package.v1', id: 'ux05fix2_float', version: '0.1.0', title: '速度档位',
    description: '只返回收到的参数和类型，不会让机器人运动。', entry: 'skill.py:run', requires: [{id: 'system.health'}],
    parameters: PARAMS, timeout_s: 30,
  }, null, 2).replace('"value": 1,', '"value": 1.0,');
  if (!json.includes('"value": 1.0,') || !PARAMS_TEXT.includes('"value": 1.0,')) throw Error('skill.json 应写成 1.0');
  fs.writeFileSync(path.join(dir, 'skill.json'), json);
  fs.writeFileSync(path.join(dir, 'skill.py'), 'def run(ctx, **params):\n    return {"params": params, "types": {k: type(v).__name__ for k, v in params.items()}}\n');
  return dir;
}

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  if (!cfg.real?.ignore || !cfg.real?.mixed || !cfg.real?.hang || !cfg.real?.creep) throw Error('夹具需要 NX_FIXTURE_IGNORE_STOP=1 NX_FIXTURE_FIX2=1');
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
  const call = (p, body) => page.evaluate(([p, body]) => api(p, body), [p, body]);

  // ---------------------------------------------------------------- setup: a user, project A with real robots
  await go('overview');
  await gateSignIn(page, {name: 'ux05-fix2 用户', email: 'ux05fix2@example.test', password: 'ux05fix2-password-123'});
  await go('overview');
  await page.evaluate(() => refresh(true));
  await page.locator('#main .first-run [data-action="sim-add"]').click();
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  await closeDialog();
  const projectA = await page.evaluate(() => projectId);
  const addReal = async (name, r, pid) => {
    const robot = await call('robots', {project_id: pid, name, family: 'quadruped', endpoint: r.endpoint, token: r.token});
    await call('robots/connect', {robot_id: robot.id});
    await page.evaluate(() => refresh(true));
    return robot.id;
  };
  const mixed = await addReal('混合标定巡检狗', cfg.real.mixed, projectA);
  const hang = await addReal('复位会卡住的巡检狗', cfg.real.hang, projectA);
  log('setup done');

  // ---------------------------------------------------------------- Q7b-04: hand-entered float option, every send path
  await page.evaluate(id => robotDetailOpen(id), mixed);
  await dlg('.cap-list').waitFor();
  await dlg(`[data-action="skillpkg-open"][data-id="${mixed}"]`).first().click();
  await dlg('details.folder-install').waitFor();
  await dlg('details.folder-install summary').click();
  await dlg('details.folder-install input[name=path]').fill(skillDir);
  await dlg('details.folder-install button[type=submit]').click();
  await dlg('.preview-params').waitFor();
  await submit();
  await page.waitForFunction(() => /已安装|技能包已安装/.test(document.body.textContent), null, {timeout: 30000});
  await page.keyboard.press('Escape').catch(() => {});
  await go('skills');
  await page.evaluate(() => action('skill-add'));
  await page.locator('form[data-form=skill]').waitFor();
  await fill('name', '速度档位（手工登记）'); await fill('version', '0.1.0'); await fill('entrypoint', 'ux05fix2_float');
  await fill('requires', 'system.health'); await fill('success_condition', '返回参数'); await fill('stop_condition', '超时');
  await dlg('details:has([name=parameter_decls]) summary').click();
  await fill('parameter_decls', PARAMS_TEXT);
  await shot('01-register-skill-float-text');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'}).catch(() => {});
  const skill = await page.evaluate(async () => { await refresh(true); return state.skills.find(s => s.entrypoint === 'ux05fix2_float'); });
  assert(skill && skill.parameters?.[0]?.type === 'float', '技能应已登记：' + JSON.stringify(skill));
  await page.evaluate(() => action('application-add'));
  await page.locator('form[data-form=application]').waitFor();
  await fill('name', '速度档位应用'); await fill('version', '0.1.0');
  await page.locator('#dialog select[name=robot_id]').selectOption(mixed);
  if (!await page.locator('#dialog .step-editor').count()) await page.locator('#dialog [data-action="step-add"]').click();
  await page.locator('#dialog .step-editor select[name=skill_id]').selectOption(skill.id);
  await page.locator('#dialog .step-editor select[name="param:speed"]').waitFor();
  await page.locator('#dialog .step-editor select[name="param:speed"]').selectOption({label: '快'});
  await shot('02-application-step-fast');
  await submit();
  const app = await page.evaluate(async () => { await refresh(true); return state.applications.find(a => a.name === '速度档位应用'); });
  assert(app && app.steps[0].parameters.speed === 1, '应用应已保存：' + JSON.stringify(app));
  await page.keyboard.press('Escape').catch(() => {});
  const harness = await call('harness', {application_id: app.id});
  assert(harness.passed, '工程检查应通过：' + JSON.stringify(harness));
  let op = await call('onsite', {application_id: app.id, hardware_authorized: true});
  for (let i = 0; i < 60 && op.status === 'running'; i++) {
    await page.waitForTimeout(500);
    op = await call('onsite/poll', {operation_id: op.id});
  }
  assert(op.status === 'succeeded', '设备端执行前检查与现场测试应接受 1.0（快）：' + JSON.stringify(op));
  const report = await page.evaluate(async id => { await refresh(true); return state.evaluations.find(e => e.id === id); }, op.report_id);
  assert(report?.passed === true, '现场测试应通过：' + JSON.stringify(report));
  const release = await call('deployments', {application_id: app.id, report_id: report.id});
  assert(release.status === 'deployed', '部署应被设备接受：' + JSON.stringify(release));
  log('Q7b-04 ok (validate, evaluate, deploy accepted 1.0)');

  // ---------------------------------------------------------------- Q7b-07: only looser keys are "放宽"
  await page.evaluate(id => robotDetailOpen(id), mixed);
  await dlg('#robot-safety [data-latch-relaxed]').waitFor({timeout: 20000});
  const relaxedText = await dlg('#robot-safety [data-latch-relaxed]').innerText();
  assert(relaxedText.includes('偏航漂移（yaw_drift_rad_s = 0.002，默认 0.0003）') && relaxedText.includes('升级次数（escalate_after = 30，默认 10）'),
    '放宽警告应列出更宽松的键：' + relaxedText);
  assert(!relaxedText.includes('pos_m'), '更严格的 pos_m 不应列为放宽：' + relaxedText);
  const calText = await dlg('#robot-safety [data-latch-calibrated]').innerText();
  assert(calText.includes('位置（pos_m = 0.03') && calText.includes('不比默认宽松'), 'pos_m 应作为现场标定列出：' + calText);
  await shot('03-relaxed-only-looser-keys');
  await closeDialog();
  log('Q7b-07 ok');

  // ---------------------------------------------------------------- Q7b-05/06/08: a hung reset on a real robot
  // The real runtime advertises `capabilities.reset_expire` and `reset_expected_s` (manifest 7 s): the desktop sends
  // expire_s = max(8, 7 + 5) = 12 and waits 16 s.
  const RT = await page.evaluate(async id => { await refresh(true); return state.robots.find(x => x.id === id).reset_policy; }, hang);
  assert(RT && RT.reset_expire === true && RT.reset_expected_s === 7 && RT.expire_s === 12 && RT.timeout_s === 16,
    '真运行时应报告 reset_expire 与 reset_expected_s=7：' + JSON.stringify(RT));
  const stopped = await call('robots/stop', {robot_id: hang, project_id: projectA});
  assert(stopped.status === 'accepted', '停止应被接受：' + JSON.stringify(stopped));
  await page.evaluate(async () => { await refresh(true); });
  const openReset = async () => {
    await page.evaluate(id => action('robot-reset', id), hang);
    await page.locator('form[data-form=robot-reset]').waitFor();
    return dlg('[data-reset-wait]').innerText();
  };
  const resetOnce = async () => {
    const wait = await openReset();
    await auth().check();
    const started = Date.now();
    await submit();
    await dlg('.reset-outcome').waitFor({timeout: 40000});
    return {wait, waited: (Date.now() - started) / 1000, text: await dlg('.dialog-body').innerText(),
      head: await dlg('.reset-outcome h3').innerText(), note: dlg('[data-reset-pending]')};
  };
  const voidWords = ['已作废', '会把它作废', '作废、急停保持锁存', '超时作废'];
  // primary: the real runtime voids the late reset
  await fixture('/fixture/hang?on=1');
  let r = await resetOnce();
  assert(r.wait.includes('桌面最多等待 16 秒') && r.wait.includes('复位超过 12 秒才完成时，机器人会把它作废、急停保持锁存'),
    '复位表单应按真运行时说明时限与作废：' + r.wait);
  assert(!r.wait.includes('不会作废超时的复位'), '真运行时支持作废，不应说不会作废：' + r.wait);
  assert(r.waited >= 15, '桌面应等到时限之后才放弃：' + r.waited);
  assert(r.head === '这次解除急停已超时作废，急停保持锁存', '支持作废时的标题：' + r.head);
  assert(await r.note.getAttribute('data-reset-expire') === 'supported' && await r.note.getAttribute('data-reset-late') !== null, '应标记为支持作废、本次超时');
  assert(r.text.includes('超过 12 秒的时限，现在在机器人上已作废') && r.text.includes('急停也继续保持锁存') && r.text.includes('重新解除急停'),
    '应说明这次复位已作废：' + r.text);
  for (const wrong of ['如果它超过时限才完成', '请稍候', '上一次复位', '急停可能自动解除', '不会作废超时的复位']) assert(!r.text.includes(wrong), `不应出现“${wrong}”：` + r.text);
  await shot('04-reset-late-void');
  await page.keyboard.press('Escape').catch(() => {});
  await page.evaluate(id => robotDetailOpen(id), hang);
  await dlg('#robot-safety [data-reset-stuck][data-reset-expire="supported"]').waitFor({timeout: 20000});
  await shot('05-detail-reset-stuck-void');
  await fixture('/fixture/hang?on=0');
  // the late reset returns on the robot: the runtime voided it (the desktop sent expire_s), the latch stays
  await page.waitForTimeout(3000);
  let s = await call('robots/safety', {project_id: projectA, robot_id: hang, all_projects: true});
  assert(s.latched.includes(hang), '迟到的复位应被运行时作废、急停保持锁存：' + JSON.stringify(s.latched));
  log('Q7b-05/06/08 real runtime (reset_expire, expire_s 12) ok');

  // an older runtime (no `reset_expire`): stubbed on the desktop API, the robot itself is unchanged
  let stubMode = 'false';
  const strip = v => {
    if (Array.isArray(v)) return v.map(strip);
    if (!v || typeof v !== 'object') return v;
    const o = {};
    for (const [k, x] of Object.entries(v)) {
      if (k === 'reset_expire') { if (stubMode === 'false') o[k] = false; continue; }
      o[k] = strip(x);
    }
    return o;
  };
  const oldRuntime = async route => {
    const res = await route.fetch();
    if (!(res.headers()['content-type'] || '').includes('json')) return route.fulfill({response: res});
    await route.fulfill({response: res, json: strip(await res.json())});
  };
  await page.route('**/api/**', oldRuntime);
  await page.keyboard.press('Escape').catch(() => {});
  // reset_expire missing: the form never promises a void
  stubMode = 'missing';
  await page.evaluate(async () => { await refresh(true); });
  const w = await openReset();
  assert(w.includes('不会作废超时的复位') && !voidWords.some(x => w.includes(x)), 'reset_expire 缺失时不应说会作废：' + w);
  await page.keyboard.press('Escape').catch(() => {});
  stubMode = 'false';
  await page.evaluate(async () => { await refresh(true); });
  await fixture('/fixture/hang?on=1');
  r = await resetOnce();
  assert(r.wait.includes('桌面最多等待 16 秒') && r.wait.includes('不会作废超时的复位'), '旧运行时的复位表单：' + r.wait);
  assert(r.waited >= 15, '桌面应等到时限之后才放弃：' + r.waited);
  assert(r.head === '这次解除急停超时了，复位还在执行', '旧运行时的标题应说“这次”：' + r.head);
  assert(await r.note.getAttribute('data-reset-expire') === 'unsupported' && await r.note.getAttribute('data-reset-late') !== null, '应标记为旧运行时、本次超时');
  assert(r.text.includes('不会作废超时的复位') && r.text.includes('急停可能自动解除') && r.text.includes('请保持远离') && r.text.includes('按“停止”'),
    '旧运行时应如实说明：' + r.text);
  for (const wrong of [...voidWords, '上一次复位', '如果它超过时限才完成', '请稍候']) assert(!r.text.includes(wrong), `旧运行时不应出现“${wrong}”：` + r.text);
  assert(await dlg('[data-action="simlive-stop"]').count() === 1, '应提供“停止（取消那次复位）”');
  await shot('06-reset-late-old-runtime');
  await page.keyboard.press('Escape').catch(() => {});
  await page.evaluate(id => robotDetailOpen(id), hang);
  await dlg('#robot-safety [data-reset-stuck][data-reset-expire="unsupported"]').waitFor({timeout: 20000});
  const stuckText = await dlg('#robot-safety [data-reset-stuck]').innerText();
  assert(!voidWords.some(x => stuckText.includes(x)), '旧运行时的详情不应说已作废：' + stuckText);
  await shot('07-detail-reset-stuck-old-runtime');
  await fixture('/fixture/hang?on=0');
  await page.unroute('**/api/**', oldRuntime);
  await page.waitForTimeout(3000);
  s = await call('robots/safety', {project_id: projectA, robot_id: hang, all_projects: true});
  assert(s.latched.includes(hang), '（真运行时）作废后急停仍应锁存');
  await page.keyboard.press('Escape').catch(() => {});
  const ok = await call('robots/reset', {robot_id: hang, hardware_authorized: true, request_id: 'fix2-final-' + Date.now()});
  assert(ok.status === 'accepted' && ok.expire_s === 12 && ok.reset_expire === true, '正常复位应成功：' + JSON.stringify(ok));
  log('Q7b-05/08 old-runtime wording (stubbed) ok');

  // ---------------------------------------------------------------- ux05-fix3: the runtime's one-shot drift notice
  // While latched the body slides 9 cm (fixture /fixture/creep): the real runtime emits safety.latched_drift_notice
  // once per latch; the latch-guard panel shows its message exactly once and drops it after the reset.
  const NOTICE = '急停期间机器人离停下位置已超过';
  const occurrences = async scope => (await dlg(scope).innerText()).split(NOTICE).length - 1;
  const onceFor = async (scope, seconds) => {
    for (let i = 0; i < seconds; i++) {
      assert(await occurrences(scope) === 1, '漂移提示应只出现一次：' + await dlg(scope).innerText());
      await page.waitForTimeout(1000);
    }
  };
  const runtimeLatch = async rb => {
    const st = await (await fetch(rb.endpoint + '/v1/status', {headers: {Authorization: 'Bearer ' + rb.token}})).json();
    return st.latched_motion;
  };
  await fixture('/fixture/creep?on=1');
  // (a) the desktop's simulated robot, live view, two latches
  const quad = await page.evaluate(() => state.robots.find(x => x.sim)?.id);
  assert(quad, '应有仿真机器人');
  for (const round of [1, 2]) {
    await page.keyboard.press('Escape').catch(() => {});
    await page.evaluate(id => simLiveOpen(id), quad);
    await dlg('#sim-live').waitFor();
    await dlg('#sim-live [data-action="simlive-stop"]').first().click();
    await dlg('#sim-live .estop-latched').waitFor({timeout: 15000});
    await dlg(`#sim-live [data-latch-guard="alert"] li:has-text("${NOTICE}")`).waitFor({timeout: 30000});
    const line = (await dlg(`#sim-live [data-latch-guard="alert"] li:has-text("${NOTICE}")`).innerText()).trim();
    assert(/^急停期间机器人离停下位置已超过 \d+ 厘米（底盘位置），可能是里程计漂移，也可能在移动，请到现场确认$/.test(line), '仿真实时视图的漂移提示：' + line);
    await onceFor('#sim-live', 5);
    if (round === 1) await shot('08-sim-live-drift-notice');
    await dlg('#sim-live [data-action="robot-reset"]').first().click();
    await page.locator('form[data-form=robot-reset]').waitFor();
    await auth().check();
    await submit();
    await dlg('#sim-live .sim-meta:has-text("未锁存")').waitFor({timeout: 30000});
    assert(await occurrences('#sim-live') === 0, '解除急停后漂移提示应消失');
    log(`drift notice sim live view, latch ${round} ok`);
  }
  // (b) a real robot (runtime by address and token), its detail page
  const creep = await addReal('急停后会滑移的巡检狗', cfg.real.creep, projectA);
  await page.keyboard.press('Escape').catch(() => {});
  const cs = await call('robots/stop', {robot_id: creep, project_id: projectA});
  assert(cs.status === 'accepted', '停止应被接受');
  await page.evaluate(id => robotDetailOpen(id), creep);
  await dlg(`#robot-safety [data-latch-guard="alert"] li:has-text("${NOTICE}")`).waitFor({timeout: 30000});
  const lmRt = await runtimeLatch(cfg.real.creep);
  assert(lmRt.drift_notice === true && lmRt.drift_notice_detail?.channel === 'base_position' && lmRt.drift_notice_detail.displacement > 0.05,
    '运行时应报告一次漂移提示：' + JSON.stringify(lmRt));
  const detailLine = (await dlg(`#robot-safety [data-latch-guard="alert"] li:has-text("${NOTICE}")`).innerText()).trim();
  assert(detailLine === lmRt.drift_notice_detail.message, `详情应原样显示运行时的提示：${detailLine} ≠ ${lmRt.drift_notice_detail.message}`);
  await onceFor('#robot-safety', 6);
  assert(await occurrences('.dialog-body') === 1, '整个详情对话框里漂移提示只出现一次');
  await shot('09-detail-drift-notice');
  await dlg('#robot-safety [data-action="robot-reset"]').click();
  await page.locator('form[data-form=robot-reset]').waitFor();
  const cw = await dlg('[data-reset-wait]').innerText();
  assert(cw.includes('桌面最多等待 12 秒') && cw.includes('复位超过 8 秒才完成时'), '未声明 reset_expected_s 时用 8 秒：' + cw);
  await auth().check();
  await submit();
  await page.locator('form[data-form=robot-reset]').waitFor({state: 'hidden', timeout: 30000});
  await page.keyboard.press('Escape').catch(() => {});
  await page.evaluate(id => robotDetailOpen(id), creep);
  await dlg('#robot-safety [data-latch-guard="armed"]').waitFor({timeout: 20000});
  assert(await occurrences('#robot-safety') === 0, '解除急停后详情不应再有漂移提示');
  assert((await runtimeLatch(cfg.real.creep)).drift_notice === false, '运行时复位后 drift_notice 应清除');
  await fixture('/fixture/creep?on=0');
  await page.keyboard.press('Escape').catch(() => {});
  log('drift notice robot detail ok');

  // ---------------------------------------------------------------- Q7b-10: another project's robot, hidden window
  const projectB = (await call('projects', {name: '项目 B 园区', objective: '夜间巡检'})).id;
  const ignore = await addReal('B 项目巡检狗', cfg.real.ignore, projectB);
  await page.evaluate(pid => { projectId = pid; sessionStorage.setItem('brain-project', pid); render(); }, projectA);
  await fixture('/fixture/ignore?on=1');
  const st = await call('robots/stop', {robot_id: ignore, project_id: projectB});
  assert(st.status === 'accepted', '停止应被接受');
  await go('models');
  await top.locator(`[data-safety-alert="${ignore}"]`).waitFor({timeout: 90000});
  const row = await top.locator(`[data-safety-alert="${ignore}"]`).innerText();
  assert(row.includes('B 项目巡检狗') && row.includes('（项目：项目 B 园区）'), '横幅应写明另一个项目：' + row);
  assert(await page.evaluate(() => projectId) === projectA, '用户仍在项目 A');
  await shot('10-banner-other-project');
  // hidden window, robot latched: polling goes on
  const counts = [];
  page.on('request', q => { if (q.url().endsWith('/api/robots/safety')) counts.push(Date.now()); });
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(8000);
  const hiddenLatched = counts.length;
  assert(hiddenLatched >= 2, '页面隐藏且有锁存时仍应轮询：' + hiddenLatched);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await top.locator(`[data-safety-alert="${ignore}"] [data-action="safety-goto"]`).click();
  await dlg('#robot-safety [data-safety-alert]').waitFor({timeout: 15000});
  assert(await page.evaluate(() => projectId) === projectB, '“切换到该项目查看”应切换到项目 B');
  await shot('11-switched-to-project-b');
  await fixture('/fixture/ignore?on=0');
  await page.waitForTimeout(1500);
  await dlg('#robot-safety [data-action="robot-reset"]').click();
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await top.locator(`[data-safety-alert="${ignore}"]`).waitFor({state: 'detached', timeout: 20000});
  await page.keyboard.press('Escape').catch(() => {});
  // hidden and quiet: slow polling only (bounded)
  await page.waitForFunction(() => !safetyData.latched.length, null, {timeout: 20000});
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
  });
  await page.waitForTimeout(1000);
  const before = counts.length;
  await page.waitForTimeout(8000);
  const hiddenQuiet = counts.length - before;
  assert(hiddenQuiet <= 1, '页面隐藏且无锁存时应放慢轮询：' + hiddenQuiet);
  log('Q7b-10 ok');

  assert(errors.length === 0, '页面错误：' + errors.join(' | '));
  await browser.close();
  fs.rmSync(skillDir, {recursive: true, force: true});
  console.log(JSON.stringify({ok: true, hidden_latched_8s: hiddenLatched, hidden_quiet_8s: hiddenQuiet, seconds: (Date.now() - t0) / 1000}));
})().catch(async e => {
  console.error(e);
  try {
    const page = globalThis.__page;
    if (page) {
      await page.screenshot({path: path.join(shots, 'failure.png')});
      console.error(await page.evaluate(() => JSON.stringify({safety: typeof safetyData !== 'undefined' ? safetyData : null,
        dialog: document.querySelector('#dialog[open]')?.innerText?.slice(0, 1500)})));
    }
  } catch (x) { console.error('no failure details:', x.message); }
  process.exit(1);
});
