const {gateSignIn}=require('./gate_helper.cjs');
// Browser acceptance for ux04 (round 06), for a new user with one simulated quadruped.
//   Q5-05  the run form and the "高级 JSON" path refuse values outside the declared min/max/choices before
//          sending, on the field; a device refusal (param_out_of_range, stubbed: the runtime may not send
//          it yet) is shown on the form field it names;
//   Q5-10  units only from skill.json (no "m/s" or "s" guessed from a parameter name);
//   Q5-03  a reset that hangs or is refused with 409 reset_in_progress shows the real reason and the way
//          out (wait / 重启仿真机器人; the e-stop stays latched after the restart, then 解除急停) -- never
//          "无法连接接入服务" or "检查地址、凭据";
//   Q5-12  plain language first: robot detail without /v2/manifest, rate_hz, capability IDs on the first
//          screen; install pre-check with the run form's names; the trial project is not a described task;
//          the authorization checkbox explains itself; run ids collapsed; a disabled "运行技能" says why;
//          the operations table names the robot instead of its id.
// Start tests/ux01_browser_fixture.py with NX_FIXTURE_HANG_RESET=1 first (simulated bodies only).
// Usage: node tests/ux04_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux04';

function writeSkill() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nx-ux04-skill-'));
  fs.writeFileSync(path.join(dir, 'skill.json'), JSON.stringify({
    schema: 'skill.package.v1', id: 'ux04_check', version: '0.1.0', title: '参数检查',
    description: '只读取 system.health 并返回参数，不会让机器人运动。任何提供 system.health 的本体都能运行。',
    entry: 'skill.py:run', requires: [{id: 'system.health'}],
    parameters: [
      {name: 'loops', type: 'int', default: 1, min: 1, max: 3, title: '圈数', unit: '圈', description: '要做几遍'},
      {name: 'mode', type: 'enum', choices: ['slow', 'fast'], default: 'slow', title: '模式'},
      {name: 'speed', type: 'float', default: 0.5},
      {name: 'timeout', type: 'float', default: 2000},
    ],
    timeout_s: 30,
  }, null, 2));
  fs.writeFileSync(path.join(dir, 'skill.py'), 'def run(ctx, **params):\n    return {"health": ctx.read(\"system.health\"), \"params\": params}\n');
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
  const skillRuns = () => page.evaluate(async () => { await refresh(true); return state.operations.filter(o => o.kind === 'skill_run'); });
  const fixture = p => page.evaluate(async u => (await fetch(u)).json(), cfg.url + p);
  const auth = () => page.locator('#dialog input[name=hardware_authorized]');

  // ---------------------------------------------------------------- fresh workspace + one simulated quadruped
  await go('robots');
  await gateSignIn(page, {name: 'ux04 新用户', email: 'ux04@example.test', password: 'ux04-password-123'});
  await click('sim-add');
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  const quad = await page.evaluate(() => state.robots[0].id);

  // ---------------------------------------------------------------- Q5-12 #3: robot detail, plain first
  const detailText = await dlg('.dialog-body').innerText();
  for (const tech of ['/v2/manifest', 'hardware_estop', 'rate_hz', 'base.navigate_to', 'frame base_link']) {
    assert(!detailText.includes(tech), `机器人详情第一屏不应出现技术细节“${tech}”`);
  }
  assert(detailText.includes('它能做什么'), '能力区应以白话标题开头');
  assert(await dlg('.cap-row details.tech-detail').count() > 3, '能力 ID 与参数应收进“技术细节”');
  await shot('01-robot-detail');
  await dlg('.cap-row details.tech-detail summary').first().click();
  assert((await dlg('.cap-row details.tech-detail[open]').first().innerText()).match(/[a-z]+\.[a-z_]+/), '展开后应能看到能力 ID');
  await click('close');

  // ---------------------------------------------------------------- Q5-12 #4: trial project is not a described task
  await go('overview');
  const first = page.locator('#main .journey-step').first();
  if (await first.count()) {
    assert(!(await first.evaluate(el => el.classList.contains('has-record'))), '自动建的试用项目不应算作“明确任务 · 已有记录”');
    assert((await first.innerText()).includes('自动创建'), '第一步应说明试用项目是自动创建的');
    await shot('02-journey');
  }

  // ---------------------------------------------------------------- Q5-12 #1/#2: install pre-check in plain words
  await go('robots');
  await click('detail-robots', quad);
  await dlg('.cap-list').waitFor();
  await click('skillpkg-open', quad);
  await dlg('.gallery-item[data-example="patrol"]').waitFor();
  const gallery = await dlg('.gallery').innerText();
  assert(!/base\.navigate_to|legged\.posture/.test(gallery), '示例技能说明不应出现能力 ID');
  await dlg('details.folder-install summary').click();
  await dlg('details.folder-install input[name=path]').fill(skillDir);
  await dlg('details.folder-install button[type=submit]').click();
  await dlg('.preview-params').waitFor();
  const preview = await dlg('.dialog-body').innerText();
  assert(preview.includes('圈数') && preview.includes('loops') && preview.includes('范围 1 – 3') && preview.includes('单位 圈'),
    '预检里的参数应与运行表单同名（圈数 loops），带范围与单位：' + preview);
  assert(!/ · int| · enum| · float/.test(preview), '预检不应显示技术类型名 int/enum/float');
  assert(!preview.includes('system.health'), '预检的说明与能力标签不应出现能力 ID：' + preview);
  assert(preview.includes('需要机器人能做到'), '能力要求应是白话');
  await shot('03-install-precheck');
  await submit();
  await page.locator('form[data-form=sim-run] .param-form').waitFor({timeout: 30000});

  // ---------------------------------------------------------------- Q5-10: units only from skill.json
  const speedField = dlg('[data-param-field="speed"]'), timeoutField = dlg('[data-param-field="timeout"]');
  assert(await speedField.locator('.param-unit').count() === 0, '未声明单位的 speed 不应被标上 m/s');
  assert(await timeoutField.locator('.param-unit').count() === 0, '未声明单位的 timeout 不应被标上 s');
  assert((await speedField.innerText()).includes('单位未声明'), '未声明单位时应写“单位未声明”');
  assert((await speedField.innerText()).includes('速度'), '内置对照仍可提供中文名');
  assert(await dlg('[data-param-field="loops"] .param-unit').innerText() === '圈', '声明的单位应显示');
  const formText = await dlg('.param-form').innerText();
  assert(!/m\/s|单位 s\b/.test(formText), '表单里不应出现猜测的单位：' + formText);
  await shot('04-run-form-units');

  // ---------------------------------------------------------------- Q5-05: form refuses out-of-range before sending
  const loops = page.locator('#dialog [name="param:loops"]');
  await loops.fill('5');
  await auth().check();
  await submit();
  await dlg('[data-param-field="loops"].invalid .param-error:has-text("不能大于 3")').waitFor();
  assert((await dlg('.form-error').innerText()).includes('圈数（loops）：不能大于 3'), '表单顶部应说明哪个参数');
  assert(!(await skillRuns()).length, '超出声明范围时不应发出运行请求');
  await shot('05-run-form-out-of-range');
  await loops.fill('2');
  assert(await dlg('[data-param-field="loops"].invalid').count() === 0, '改正后字段标记应消失');

  // ---------------------------------------------------------------- Q5-05: advanced JSON is checked too
  const raw = page.locator('#dialog .raw-params');
  await raw.locator('summary').click();
  await page.locator('#dialog .raw-params[data-synced]').waitFor({state: 'attached'});
  const json = JSON.parse(await page.locator('#dialog [name=parameters]').inputValue());
  assert(json.loops === 2 && json.mode === 'slow', '高级 JSON 应与表单一致：' + JSON.stringify(json));
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify({...json, loops: 99, mode: 'turbo'}));
  await submit();
  await dlg('.raw-params .param-error').waitFor();
  const jsonError = await dlg('.form-error').innerText();
  assert(jsonError.includes('圈数（loops）：不能大于 3') && jsonError.includes('只能选 slow / fast'), '高级 JSON 的范围/选项错误应说清：' + jsonError);
  assert(!(await skillRuns()).length, '高级 JSON 越界时不应发出运行请求（Q5-05）');
  await shot('06-json-out-of-range');
  await page.locator('#dialog [name=parameters]').fill(JSON.stringify(json));
  await raw.locator('summary').click();
  await page.locator('#dialog .raw-params:not([data-synced])').waitFor({state: 'attached'});

  // ---------------------------------------------------------------- Q5-05: a device refusal lands on its field (stubbed)
  await page.route('**/api/robots/run-skill', route => route.fulfill({contentType: 'application/json', body: JSON.stringify({
    id: 'skill-run-stub', status: 'rejected', code: 'param_out_of_range', device_status: 400, param: 'loops', min: 1, max: 2,
    error: '设备拒绝运行：技能参数 loops=2 超出声明范围 [1, 2]'})}));
  await submit();
  await dlg('[data-param-field="loops"] .param-error[data-from="device"]').waitFor();
  const deviceField = await dlg('[data-param-field="loops"] .param-error').innerText();
  assert(deviceField.includes('圈数（loops）') && deviceField.includes('允许范围 1 – 2'), '设备的字段错误应显示在该字段上：' + deviceField);
  const deviceTop = await dlg('.form-error').innerText();
  assert(deviceTop.includes('参数不符合要求') && !/内部出错|网络|稍后重试/.test(deviceTop), '设备的参数拒绝应说成参数问题：' + deviceTop);
  await shot('07-device-field-error');
  await page.unroute('**/api/robots/run-skill');

  // ---------------------------------------------------------------- Q5-12 #5: the authorization checkbox explains itself
  await auth().uncheck();
  await submit();
  await dlg('.form-error:has-text("还没有勾选现场安全确认")').waitFor();
  assert(await dlg('.hardware-confirm.needs-confirm').count() === 1, '确认框应被标出');
  assert(!(await skillRuns()).length, '未授权时不应发出运行请求');
  await shot('08-authorization-explained');

  // ---------------------------------------------------------------- Q5-04 in the page: refusals are not faults
  const verdicts = await page.evaluate(() => [
    'base.navigate_to 被安全监督器拒绝：目标 x=500.0 超出允许工作区 [-200.0, 200.0]',
    'policy.action_chunk 被安全监督器拒绝：动作块最多 512 步，收到 600 步',
    'timeout_s 须为正的有限数值', '运行 r-123 超时'].map(m => explainError(m).plain));
  for (const v of verdicts) assert(!/内部出错|网络可能不稳定/.test(v), '安全/参数拒绝被说成故障：' + v);

  // ---------------------------------------------------------------- run for real, then stop
  await auth().check();
  await submit();
  await dlg('#sim-live .sim-run').waitFor({timeout: 30000});
  await dlg('#sim-live .sim-run:not([data-run-status="running"]):not([data-run-status="queued"])').waitFor({timeout: 30000});
  assert(await dlg('#sim-live .sim-run').getAttribute('data-run-status') === 'succeeded', '技能应运行成功：' + await dlg('#sim-live .sim-run').innerText());
  assert(await dlg('#sim-live .sim-run details.tech-detail').count() === 1, '运行编号应收进“技术细节”');
  assert(!(await dlg('#sim-live .sim-run').innerText()).match(/run-[0-9a-f]{6,}/), '运行编号不应显眼显示');
  const sent = (await skillRuns())[0].parameters;
  assert(sent.loops === 2 && sent.mode === 'slow', '发出的参数不对：' + JSON.stringify(sent));
  await click('simlive-stop', quad);
  await dlg('#sim-live .estop-latched').waitFor();
  assert(await dlg('[data-action="sim-run-pick"]').isDisabled(), '急停锁存时不能运行技能');
  assert((await dlg('[data-run-disabled]').innerText()).includes('急停已锁存'), '变灰的“运行技能”旁应写原因');
  await shot('09-live-latched');

  // ---------------------------------------------------------------- Q5-03: the reset hangs
  await fixture('/fixture/hang?on=1');
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('.reset-outcome').waitFor({timeout: 40000});
  const hung = dlg('.reset-outcome');
  assert(await hung.getAttribute('data-reset-code') === 'reset_in_progress', '卡住的复位应报出 reset_in_progress：' + await hung.innerText());
  assert(await hung.getAttribute('data-reset-status') === 'unknown', '第一次卡住的复位结果应为待核对');
  let text = await dlg('.dialog-body').innerText();
  for (const wrong of ['无法连接接入服务', '地址、凭据', '协议']) assert(!text.includes(wrong), `复位卡住时不应显示“${wrong}”：` + text);
  assert(text.includes('还在执行') && text.includes('重启') && text.includes('锁存'), '应说明真实原因与出路：' + text);
  assert(await dlg('[data-action="sim-restart"]').count() === 1, '仿真机器人应有“重启仿真机器人”按钮');
  await shot('10-reset-hangs');

  // A second attempt: 409 reset_in_progress at once, with the same explanation.
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('.reset-outcome[data-reset-status="rejected"][data-reset-code="reset_in_progress"]').waitFor({timeout: 20000});
  text = await dlg('.dialog-body').innerText();
  assert(text.includes('机器人拒绝了这次解除急停') && text.includes('保持远离') && text.includes('按“停止”') && !text.includes('无法连接'), '409 应显示真实原因：' + text);
  await dlg('.tech-detail summary').first().click();
  assert((await dlg('.tech-detail code').first().innerText()).includes('HTTP 409'), '技术细节应保留 409 与错误码');
  await shot('11-reset-409');

  // The live view knows too.
  await click('sim-live', quad);
  await dlg('#sim-live [data-reset-stuck]').waitFor({timeout: 10000});
  await shot('12-live-reset-stuck');

  // The way out: restart the simulated robot; the latch survives.
  const before = (await fixture('/fixture/sims')).sims.find(s => s.robot_id === quad).pid;
  await dlg('#sim-live [data-action="sim-restart"]').click();
  await page.waitForFunction(async ([u, id, old]) => {
    const r = await (await fetch(u)).json();
    const s = r.sims.find(x => x.robot_id === id);
    return s && s.alive && s.pid !== old;
  }, [cfg.url + '/fixture/sims', quad, before], {timeout: 60000, polling: 500});
  await fixture('/fixture/hang?on=0');
  await page.locator('#dialog #sim-live [data-reset-stuck]').waitFor({state: 'detached', timeout: 60000})
    .catch(() => { throw Error('重启后不应再显示复位卡住'); });
  await dlg('#sim-live .estop-latched').waitFor({timeout: 30000});
  assert((await dlg('#sim-live .sim-meta').innerText()).includes('已锁存'), '重启仿真机器人后急停应仍然锁存');
  await shot('13-after-restart-still-latched');
  await click('robot-reset', quad + '::live');
  await page.locator('form[data-form=robot-reset]').waitFor();
  await auth().check();
  await submit();
  await dlg('#sim-live .sim-meta:has-text("未锁存")').waitFor({timeout: 30000});

  // ---------------------------------------------------------------- Q5-12 #6: operations name the robot
  await click('close');
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await go('deployment');
  await page.locator('#main section.panel').last().scrollIntoViewIfNeeded();
  const opsText = await page.locator('#main section.panel').last().innerText();
  assert(opsText.includes('解除急停') && opsText.includes('机器人：') && opsText.includes('运行技能'), '操作记录应写明对象：' + opsText);
  assert(!/robot-[0-9a-f]{6,}/.test(opsText), '操作记录不应显示机器人内部编号：' + opsText);
  assert(opsText.includes('设备拒绝') && !opsText.includes('rejected'), '被拒绝的操作应显示中文状态：' + opsText);
  await shot('14-operations');

  assert(!errors.length, '页面错误：' + errors.join(' | '));
  await browser.close();
  fs.rmSync(skillDir, {recursive: true, force: true});
  console.log('ux04 browser acceptance passed; screenshots in ' + shots);
})().catch(err => { console.error(err); process.exit(1); });
