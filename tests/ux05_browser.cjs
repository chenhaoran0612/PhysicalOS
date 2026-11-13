const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for ux05 (round 07), for a brand-new user with one simulated quadruped.
//   first run  任务工作台 shows a 3-step guide (添加仿真机器人 → 安装示例技能 → 运行并观看); each step is ticked
//              from the workspace's records as it is done; the guide can be closed and stays closed;
//   Q6-07      `options: [{value, label}]` and `choices` are a select showing labels; the raw typed value is sent
//              and the device accepts it (strict typed comparison);
//   Q6-08      param_type / param_declaration_invalid / param_choice each say what is wrong (stubbed device
//              answers); a reset refused by the latch guard says so (cause); the desktop's own 500 never says
//              "稍后重试";
//   Q6-11      a hung reset reads consistently; a real robot's hung reset gets numbered steps; the live view says
//              the body's state in Chinese (no `sim:stand:navigating`); after 解除急停 the old stop message is gone;
//   latch      the latch guard in plain words while latched (stubbed status), and a banner when
//              `safety.stop_not_obeyed` is reported (stubbed); simulated: restart/close the simulation (Q7-11).
// Start tests/ux01_browser_fixture.py with NX_FIXTURE_HANG_RESET=1 first (simulated bodies only).
// Usage: node tests/ux05_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux05';

function writeSkill() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nx-ux05-skill-'));
  fs.writeFileSync(path.join(dir, 'skill.json'), JSON.stringify({
    schema: 'skill.package.v1', id: 'ux05_options', version: '0.1.0', title: '选项检查',
    description: '只读取机器人健康状态并返回参数，不会让机器人运动。',
    entry: 'skill.py:run', requires: [{id: 'system.health'}],
    parameters: [
      {name: 'pace', type: 'str', title: '节奏', default: 'slow',
        options: [{value: 'slow', label: '慢慢走'}, {value: 'fast', label: '快步走'}]},
      {name: 'laps', type: 'int', title: '圈数', default: 1,
        options: [{value: 1, label: '一圈'}, {value: 2, label: '两圈'}, {value: 3, label: '三圈'}]},
      {name: 'side', type: 'enum', title: '方向', choices: ['left', 'right'], default: 'left'},
    ],
    timeout_s: 30,
  }, null, 2));
  fs.writeFileSync(path.join(dir, 'skill.py'), 'def run(ctx, **params):\n    return {"params": params, "types": {k: type(v).__name__ for k, v in params.items()}}\n');
  return dir;
}

(async () => {
  const cfg = JSON.parse(fs.readFileSync(configPath));
  fs.mkdirSync(shots, {recursive: true});
  const skillDir = writeSkill();
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
  const closeDialog = async () => { await click('close'); await page.locator('#dialog').waitFor({state: 'hidden'}); };
  const ops = kind => page.evaluate(async k => { await refresh(true); return state.operations.filter(o => o.kind === k); }, kind);
  const fixture = p => page.evaluate(async u => (await fetch(u)).json(), cfg.url + p);
  const auth = () => page.locator('#dialog input[name=hardware_authorized]');
  const guide = page.locator('#main .first-run');
  const stepDone = async n => (await guide.locator(`[data-first-run-step="${n}"]`).getAttribute('data-done')) === '1';
  const overview = async () => { await go('overview'); await page.evaluate(() => refresh(true)); await guide.waitFor(); };

  // ---------------------------------------------------------------- first run: the guide, nothing done yet
  await go('overview');
  await gateSignIn(page, {name: 'ux05 新用户', email: 'ux05@example.test', password: 'ux05-password-123'});
  await guide.waitFor();
  assert(await guide.locator('[data-first-run-step]').count() === 3, '引导应有 3 步');
  const guideText = await guide.innerText();
  for (const t of ['添加仿真机器人', '安装示例技能', '运行并观看']) assert(guideText.includes(t), '引导缺少：' + t);
  assert(!(await stepDone(1)) && !(await stepDone(2)) && !(await stepDone(3)), '新用户三步都应未完成');
  assert(await guide.locator('li.current[data-first-run-step="1"] [data-action="sim-add"]').count() === 1, '第 1 步应是当前步并有按钮');
  await shot('01-first-run-guide');

  // step 1 from the guide (a brand-new user registers first)
  await overview();
  await guide.locator('[data-action="sim-add"]').click();
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  const quad = await page.evaluate(() => state.robots[0].id);
  await closeDialog();
  await overview();
  assert(await stepDone(1) && !(await stepDone(2)), '添加仿真机器人后第 1 步应自动打勾');
  assert(await guide.locator('li.current[data-first-run-step="2"] [data-action="skillpkg-open"]').count() === 1, '第 2 步应成为当前步');
  await shot('02-guide-step1-done');

  // step 2 from the guide: install 巡逻 from the gallery; the preview has no jargon (Q6-11 #3)
  await guide.locator('[data-action="skillpkg-open"]').click();
  await dlg('.gallery-item[data-example="patrol"]').waitFor();
  await click('skillpkg-example', quad + '|patrol');
  await dlg('.preview-params').waitFor();
  const preview = await dlg('.preview-params').innerText();
  assert(!preview.includes('结构化数据') && preview.includes('坐标点列表'), '预检参数类型应是白话：' + preview);
  await submit();
  await page.locator('form[data-form=sim-run]').waitFor({timeout: 30000});
  await closeDialog();
  await overview();
  assert(await stepDone(1) && await stepDone(2) && !(await stepDone(3)), '安装示例技能后第 2 步应自动打勾');
  await shot('03-guide-step2-done');

  // step 3 from the guide: run and watch -- long enough to stop it halfway
  await guide.locator('[data-action="sim-run-pick"]').click();
  await click('sim-run', quad + '|patrol');
  await page.locator('form[data-form=sim-run] .param-form').waitFor();
  await page.locator('#dialog [name="param:loops"]').fill('5');
  await auth().check();
  await submit();
  await dlg('#sim-live .sim-run[data-run-status="running"]').waitFor({timeout: 30000});
  // Q6-11 #3: the body's state in Chinese, the internal string only as a tooltip
  await page.waitForFunction(() => /前往目标点/.test(document.querySelector('#sim-live [data-mode]')?.textContent || ''), null, {timeout: 15000});
  const mode = dlg('#sim-live [data-mode]');
  const modeText = await mode.innerText();
  assert(!/sim:|navigating|stand/.test(modeText) && modeText.includes('站立'), '状态应是中文：' + modeText);
  assert((await mode.getAttribute('title')).startsWith('sim:'), '原始字串应只在提示里');
  await shot('04-live-running-plain-mode');

  // stop: latched; the latch guard is watching and quiet
  await click('simlive-stop', quad);
  await dlg('#sim-live .estop-latched').waitFor();
  await dlg('#sim-live .sim-run[data-run-status="stopped"]').waitFor({timeout: 15000});
  await dlg('#sim-live [data-latch-guard="quiet"]').waitFor({timeout: 10000});
  assert((await dlg('#sim-live [data-run-stopped]').innerText()).includes('被“停止”中断'), '停止后应说明运行被中断');
  await shot('05-live-latched-guard-quiet');

  // ---------------------------------------------------------------- latch guard in words + stop not obeyed (stubbed status)
  let stub = 'guard';
  await page.route('**/api/robots/live', async route => {
    const res = await route.fetch();
    const v = await res.json();
    if (v.estop?.latched && stub) {
      v.latched_motion = {enabled: true, active: true, since: 'latch', restops: 5, invalid_restops: 2, seen: 9,
        feedback_ok: false, feedback_problems: ['joint.state'], read_in_progress_s: null, displaced: true, max_drift_m: 0.07};
      if (stub === 'not-obeyed') v.stop_not_obeyed = {seq: 99, type: 'safety.stop_not_obeyed', ts: Date.now() / 1000, note: '连续 3 次停止后仍在运动'};
    }
    await route.fulfill({response: res, json: v});
  });
  await dlg('#sim-live [data-latch-guard="alert"]').waitFor({timeout: 10000});
  const guardText = await dlg('#sim-live [data-latch-guard="alert"]').innerText();
  assert(guardText.includes('急停期间检测到机器人仍在移动，已再次停止 3 次'), '锁存守护应说明再停次数：' + guardText);
  assert(guardText.includes('反馈数据异常，按可能在动处理'), '锁存守护应说明反馈异常：' + guardText);
  // Q7-11: a simulated robot has no hardware e-stop -- restart or close the simulation instead
  assert(guardText.includes('重启仿真机器人') && !guardText.includes('硬件急停') && !/restops|feedback_ok|latched_motion/.test(guardText), '锁存守护应是白话：' + guardText);
  assert(await dlg('#sim-live [data-stop-not-obeyed]').count() === 0, '没有 stop_not_obeyed 时不应有横幅');
  await shot('06-live-latch-guard-alert');
  stub = 'not-obeyed';
  await dlg('#sim-live [data-stop-not-obeyed]').waitFor({timeout: 10000});
  const banner = dlg('#sim-live [data-stop-not-obeyed]');
  const bannerText = await banner.innerText();
  assert(bannerText.includes('仿真机器人：点“重启仿真机器人”或关闭仿真') && !bannerText.includes('硬件急停'), '仿真横幅应让人重启或关闭仿真（Q7-11）：' + bannerText);
  assert(await banner.locator('[data-action="sim-restart"]').count() === 1, '仿真横幅应有“重启仿真机器人”');
  const first = await page.evaluate(() => document.querySelector('#sim-live').firstElementChild?.matches('[data-stop-not-obeyed]'));
  assert(first, '横幅应在实时状态最上方');
  await shot('07-live-stop-not-obeyed');
  stub = '';
  await dlg('#sim-live [data-stop-not-obeyed]').waitFor({state: 'detached', timeout: 10000});
  await page.unroute('**/api/robots/live');

  // ---------------------------------------------------------------- Q6-11 #1: a hung reset reads consistently
  await fixture('/fixture/hang?on=1');
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('.reset-outcome[data-reset-code="reset_in_progress"]').waitFor({timeout: 40000});
  let text = await dlg('.reset-outcome').innerText();
  assert(await dlg('.reset-outcome').getAttribute('data-reset-status') === 'unknown', '第一次卡住的复位应为待核对');
  // ux05-fix3: the real runtime reports reset_expire, and this request outlived its 8 s limit (Q7b-08): the head
  // and the explanation both speak of *this* reset, now void (was "等上一次复位完成" before the runtime had it).
  assert(await dlg('[data-reset-pending]').getAttribute('data-reset-expire') === 'supported' && await dlg('[data-reset-pending]').getAttribute('data-reset-late') !== null,
    '真运行时支持作废、本次超时');
  assert(text.includes('这次解除急停已超时作废，急停保持锁存') && text.includes('超过 8 秒的时限，现在在机器人上已作废') && !text.includes('上一次复位'),
    '标题与说明应一致：' + text);
  assert(!text.includes('拒绝'), '待核对的复位不应说成“拒绝”：' + text);
  assert(await dlg('[data-action="simlive-stop"]').count() === 1, '应能按“停止”取消那次复位');
  await shot('08-reset-hung-consistent');
  // cancel it with stop, then let the hung reset return: it fails, the latch stays
  await dlg('[data-action="simlive-stop"]').click();
  await page.waitForTimeout(800);
  await fixture('/fixture/hang?on=0');
  await click('sim-live', quad);
  await page.locator('#dialog #sim-live [data-reset-stuck]').waitFor({state: 'detached', timeout: 60000});
  await dlg('#sim-live .estop-latched').waitFor({timeout: 30000});

  // ---------------------------------------------------------------- Q6-08: reset refused by the latch guard (stubbed device answer)
  await page.route('**/api/robots/reset', route => route.fulfill({contentType: 'application/json', body: JSON.stringify({
    id: 'reset-stub', robot_id: quad, kind: 'reset', status: 'rejected', code: 'estop_latched', device_status: 409,
    cause: 'latch_guard_reset_grace', error: '复位动作（adapter.reset()）超过允许时间仍在进行且本体仍在运动，锁存守护已再次停止；保持锁存'})}));
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('.reset-outcome [data-error-cause="latch_guard_reset_grace"]').waitFor();
  text = await dlg('.reset-outcome').innerText();
  assert(text.includes('锁存守护为安全起见再次停止了它') && text.includes('不是你的操作有误'), '应说明是守护所致：' + text);
  assert(!text.includes('急停处于锁存状态，机器人不会执行任何动作'), '不应只给通用的锁存说明');
  await shot('09-reset-refused-by-guard');
  await page.unroute('**/api/robots/reset');

  // ---------------------------------------------------------------- Q6-11 #4: after 解除急停 the stop message is gone
  await click('sim-live', quad);
  await dlg('#sim-live .estop-latched').waitFor();
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('#sim-live .sim-meta:has-text("未锁存")').waitFor({timeout: 30000});
  const runText = await dlg('#sim-live .sim-run').innerText();
  assert(!runText.includes('已收到停止指令'), '解除急停后不应再挂着“已收到停止指令”：' + runText);
  assert(runText.includes('急停已经解除') && runText.includes('上一次技能'), '应说明那是上一次运行：' + runText);
  assert(await dlg('#sim-live .sim-run .notice').count() === 0, '上一次的停止不应以警告显示');
  assert(await dlg('#sim-live [data-latch-guard]').count() === 0, '未锁存时不显示锁存守护');
  await shot('10-live-after-reset');
  await closeDialog();

  // all three steps done
  await overview();
  assert(await stepDone(1) && await stepDone(2) && await stepDone(3), '运行过技能后三步都应打勾');
  assert(await guide.getAttribute('data-first-run') === 'done', '引导应显示已完成');
  await shot('11-guide-all-done');

  // ---------------------------------------------------------------- Q6-07: options with labels, typed values
  await go('robots');
  await click('detail-robots', quad);
  await dlg('.cap-list').waitFor();
  await click('skillpkg-open', quad);
  await dlg('details.folder-install').waitFor();
  await dlg('details.folder-install summary').click();
  await dlg('details.folder-install input[name=path]').fill(skillDir);
  await dlg('details.folder-install button[type=submit]').click();
  await dlg('.preview-params').waitFor();
  const optPreview = await dlg('.preview-params').innerText();
  assert(optPreview.includes('可选 慢慢走 / 快步走') && optPreview.includes('可选 一圈 / 两圈 / 三圈'), '预检应显示选项名称：' + optPreview);
  assert(!optPreview.includes("{'value'") && !optPreview.includes('"value"'), '不应出现字面量：' + optPreview);
  await submit();
  await page.locator('form[data-form=sim-run] .param-form').waitFor({timeout: 30000});
  const pace = page.locator('#dialog select[name="param:pace"]'), laps = page.locator('#dialog select[name="param:laps"]');
  const labels = await pace.locator('option').allInnerTexts();
  assert(JSON.stringify(labels) === JSON.stringify(['慢慢走', '快步走']), '下拉框应显示 label：' + labels);
  assert(await pace.locator('option:checked').innerText() === '慢慢走', '默认值应选中');
  const lapLabels = await laps.locator('option').allInnerTexts();
  assert(JSON.stringify(lapLabels) === JSON.stringify(['一圈', '两圈', '三圈']), '整数选项应显示 label：' + lapLabels + ' ' +
    await page.locator('#dialog .param-form').getAttribute('data-decls'));
  assert(await page.locator('#dialog select[name="param:side"] option').count() === 2, 'choices 也应是下拉框');
  await pace.selectOption({label: '快步走'});
  await laps.selectOption({label: '两圈'});
  await shot('12-options-select');
  // advanced JSON shows the raw typed values
  const raw = page.locator('#dialog .raw-params');
  await raw.locator('summary').click();
  await page.locator('#dialog .raw-params[data-synced]').waitFor({state: 'attached'});
  const json = JSON.parse(await page.locator('#dialog [name=parameters]').inputValue());
  assert(json.pace === 'fast' && json.laps === 2 && json.side === 'left', '高级 JSON 应是原始值：' + JSON.stringify(json));
  // a string "2" for an int option is refused before sending (strict, like the runtime)
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify({...json, laps: '2'}));
  await auth().check();
  await submit();
  await dlg('.raw-params .param-error').waitFor();
  assert((await dlg('.form-error').innerText()).includes('圈数（laps）：需要填数字'), '字符串 "2" 不应当作 2');
  // a value of the right type that is not one of the options is refused with the labels
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify({...json, laps: 7, pace: 'turbo'}));
  await submit();
  await dlg('.form-error:has-text("只能选 一圈 / 两圈 / 三圈")').waitFor();
  assert((await dlg('.form-error').innerText()).includes('只能选 慢慢走 / 快步走'), '选项错误应列出选项名称');
  assert(!(await ops('skill_run')).some(o => o.skill_id === 'ux05_options'), '本地检查不通过时不应发出运行请求');
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify(json));
  await raw.locator('summary').click();
  await page.locator('#dialog .raw-params:not([data-synced])').waitFor({state: 'attached'});
  assert(await laps.locator('option:checked').innerText() === '两圈', '收起高级后表单应保持选项');

  // ---------------------------------------------------------------- Q6-08: each parameter refusal its own words (stubbed device)
  const refusals = [
    ['param_type', {param: 'laps', expected: 'int'}, ['类型不对'], '13-param-type'],
    ['param_choice', {param: 'pace', choices: ['slow', 'fast']}, ['不是技能允许的选项', '可选 慢慢走 / 快步走'], '14-param-choice'],
    ['param_declaration_invalid', {param: 'laps', min: 1, max: 3}, ['技能包本身的参数声明有误', '联系技能作者'], '15-param-declaration'],
  ];
  for (const [code, extra, want, name] of refusals) {
    await page.route('**/api/robots/run-skill', route => route.fulfill({contentType: 'application/json', body: JSON.stringify({
      id: 'skill-run-stub', status: 'rejected', code, device_status: 400, ...extra, error: `设备拒绝运行：技能参数 ${extra.param} 不符合（${code}）`})}));
    await submit();
    await dlg(`.form-error [data-error-code="${code}"]`).waitFor();
    const all = (await dlg('.form-error').innerText()) + ' ' + (await dlg('.param-form').innerText());
    for (const w of want) assert(all.includes(w), `${code} 应说明“${w}”：` + all);
    assert(!all.includes('超出了技能声明的允许范围'), `${code} 不应说成超出范围：` + all);
    await dlg('.form-error').scrollIntoViewIfNeeded();
    if (code === 'param_declaration_invalid') assert(await dlg('.param-field.invalid').count() === 0, '声明有误不是用户填错，不应标红字段');
    await shot(name);
    await page.unroute('**/api/robots/run-skill');
  }

  // the device accepts the typed values (strict comparison on the runtime)
  const before = (await ops('skill_run')).length;
  await submit();
  await dlg('#sim-live .sim-run').waitFor({timeout: 30000});
  await dlg('#sim-live .sim-run[data-run-status="succeeded"]').waitFor({timeout: 30000});
  const runs = await ops('skill_run');
  assert(runs.length === before + 1, '应发出一次运行');
  const sent = runs.find(o => o.skill_id === 'ux05_options').parameters;
  assert(sent.pace === 'fast' && sent.laps === 2 && typeof sent.laps === 'number', '发出的应是原始类型的值：' + JSON.stringify(sent));
  await closeDialog();

  // ---------------------------------------------------------------- Q6-08: the desktop's own 500 never says "retry later"
  await page.route('**/api/robots/stop', route => route.fulfill({status: 500, contentType: 'application/json',
    body: JSON.stringify({error: '内部错误，操作未确认；请查看本地服务', code: 'desktop_internal_error'})}));
  await click('sim-live', quad);
  await dlg('#sim-live .sim-meta').waitFor();
  await click('simlive-stop', quad);
  await page.locator('#toast.show.error').waitFor();
  const toastText = await page.locator('#toast').innerText();
  assert(!toastText.includes('稍后重试') && toastText.includes('核对机器人状态'), '桌面自身 500 应先核对状态：' + toastText);
  await shot('16-desktop-500');
  await page.unroute('**/api/robots/stop');
  await closeDialog();

  // ---------------------------------------------------------------- Q6-11 #2: a real robot's hung reset -- numbered steps
  await page.evaluate(() => {
    state.robots.push({id: 'robot-real-stub', name: '仓库巡检狗', project_id: projectId, family: 'quadruped', status: 'connected',
      endpoint: 'http://192.168.1.20:8790', capabilities: [], manifest: {}});
    uxResetOutcome({status: 'rejected', code: 'reset_in_progress', device_status: 409, reset_in_progress_s: 47.6,
      hint: '适配器复位长时间未返回，可能卡在驱动里。', error: '适配器上一次复位（adapter.reset()）尚未返回（已 47.6 s）'}, 'robot-real-stub');
  });
  await dlg('[data-reset-howto="real"]').waitFor();
  const howto = await dlg('[data-reset-howto="real"]').innerText();
  assert(await dlg('[data-reset-howto="real"] ol > li').count() === 5, '真机应给出 5 个编号步骤');
  assert(await dlg('[data-action="simlive-stop"]').count() === 1, '真机也应能按“停止”取消那次复位');
  assert(howto.includes('192.168.1.20') && howto.includes('sudo systemctl restart neuxmind-runtime') && howto.includes('硬件急停'), '步骤应具体：' + howto);
  const realText = await dlg('.reset-outcome').innerText();
  assert(realText.includes('已经执行了 48 秒') && realText.includes('机器人的提示'), '应带上复位时长与运行时的提示：' + realText);
  await shot('17-real-reset-howto');
  await closeDialog();
  await page.evaluate(() => refresh(true));

  // ---------------------------------------------------------------- the guide can be closed and stays closed
  await overview();
  await guide.locator('[data-first-run-dismiss]').click();
  await guide.waitFor({state: 'detached'});
  await page.reload();
  await page.waitForTimeout(800);
  await page.evaluate(() => refresh(true));
  assert(await page.locator('#main .first-run').count() === 0, '关闭后引导不应再出现');
  assert(await page.locator('#main .journey-next').count() === 1, '工作台其余内容应照常');
  await shot('18-guide-closed');

  assert(!errors.length, '页面错误：' + errors.join(' | '));
  await browser.close();
  fs.rmSync(skillDir, {recursive: true, force: true});
  console.log('ux05 browser acceptance passed; screenshots in ' + shots);
})().catch(err => { console.error(err); process.exit(1); });
