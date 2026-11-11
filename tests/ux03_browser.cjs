// Browser acceptance for ux03 (round 05): the ux02 audit leftovers, for a brand-new user with one
// simulated quadruped.
//   (a) 任务工作台 recommends the path that works with a simulated robot (运行技能 / 仿真试运行),
//       never the engineering assistant that is blocked without a model;
//   (b) parameter forms show human labels (skill.json title, else the built-in map) with the raw key as
//       secondary text and units next to the inputs;
//   (c) 机器人应用: application steps (create and "修改为新版本") use the same generated form as the run
//       dialog, with the raw-JSON advanced toggle; a skill without declared parameters keeps a JSON box;
//       the manual skill registration form is in plain language;
//   (d) 模型库 / 自有服务 / 助手工具 / 仿真场景: basic fields first, advanced ones behind "高级"; a required
//       field inside a collapsed block opens it instead of failing silently;
//   (e) at 1440x900 every sidebar item, down to 云平台账户, is visible without a hidden scroll.
// Start tests/ux01_browser_fixture.py first (fresh, empty workspace; simulated bodies only).
// Usage: node tests/ux03_browser.cjs [config.json] [screenshot-dir]
const {chromium} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

const configPath = process.argv[2] || '/tmp/neuxmind-ux01-qa.json';
const shots = process.argv[3] || 'artifacts/ux03';

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
  const st = () => page.evaluate(async () => { await refresh(true); return state; });
  const nextAction = () => page.locator('#main .journey-next [data-action]').first().getAttribute('data-action');

  // ---------------------------------------------------------------- (e) sidebar at 1440x900
  await go('overview');
  const nav = await page.evaluate(() => {
    const n = document.querySelector('#navigation'), links = [...n.querySelectorAll('a')];
    const last = links.at(-1).getBoundingClientRect(), bottom = document.querySelector('.sidebar-bottom').getBoundingClientRect();
    return {count: links.length, lastText: links.at(-1).innerText.trim(), scrollable: n.scrollHeight > n.clientHeight + 1,
      lastBottom: last.bottom, footerTop: bottom.top, height: innerHeight};
  });
  assert(nav.count === 15 && nav.lastText === '云平台账户', '侧栏项目不对：' + JSON.stringify(nav));
  assert(!nav.scrollable, '1440×900 下侧栏不应需要滚动：' + JSON.stringify(nav));
  assert(nav.lastBottom <= nav.footerTop && nav.lastBottom <= nav.height, '“云平台账户”应完整可见：' + JSON.stringify(nav));
  await page.locator('#navigation a[href="#cloud"]').click();
  await page.waitForFunction(() => location.hash === '#cloud');

  // ---------------------------------------------------------------- fresh workspace + one simulated quadruped
  await go('robots');
  await click('sim-add');
  await page.locator('form[data-form=register]').waitFor();
  await fill('name', 'ux03 新用户');
  await fill('email', 'ux03@example.test');
  await fill('password', 'ux03-password-123');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await click('sim-add');
  await page.locator('#dialog input[name=kind][value=quadruped]').check();
  await submit();
  await dlg('.cap-list').waitFor({timeout: 60000});
  const quad = await page.evaluate(() => state.robots[0].id);
  await click('close');

  // ---------------------------------------------------------------- (a) workbench: the sim path, not the assistant
  await go('overview');
  assert(await nextAction() === 'sim-run-pick', '只有仿真机器人时，工作台应推荐“运行技能”，而不是 ' + await nextAction());
  const next = await page.locator('#main .journey-next').innerText();
  assert(next.includes('不需要助手模型') && !next.includes('让助手准备'), '工作台推荐文案不对：' + next);
  assert(await page.locator('#main .journey-next [data-action="go-simulation"]').count() === 1, '工作台应提供“去仿真试运行”');
  assert(!(await page.locator('#main .journey-next [data-action="journey-prepare"]').count()), '不应推荐被模型挡住的工程助手');
  await shot('01-workbench-sim-first');
  // The recommended button really works: it offers the example skills (nothing installed yet).
  await page.locator('#main .journey-next [data-action="sim-run-pick"]').click();
  await dlg('.gallery-item[data-example="patrol"]').waitFor();
  await click('skillpkg-example', quad + '|patrol');
  await dlg(':text("兼容：可以安装")').first().waitFor();
  await submit();

  // ---------------------------------------------------------------- (b) run dialog: human labels and units
  await page.locator('form[data-form=sim-run] .param-form').waitFor();
  const label = name => page.locator(`#dialog [data-param-field="${name}"] .param-label`).innerText();
  assert((await label('loops')).startsWith('圈数') && (await label('loops')).includes('loops'), '圈数应为中文标签，原始键为次要文字：' + await label('loops'));
  assert((await label('tolerance')).startsWith('到达容差'), 'tolerance 应显示为“到达容差”');
  assert((await label('waypoints')).startsWith('航点'), 'waypoints 应显示为“航点”');
  assert(await page.locator('#dialog [data-param-field="tolerance"] .param-input .param-unit').innerText() === 'm', '到达容差旁应显示单位 m');
  assert(await page.locator('#dialog [data-param-field="loops"] .param-input .param-unit').innerText() === '圈', '圈数旁应显示单位');
  assert(await page.locator('#dialog [data-param-field="waypoints"] .point-row .param-unit').count() === 8, '每个航点坐标旁应显示单位');
  assert((await page.locator('#dialog [data-param-field="loops"] small').innerText()).includes('整数'), '仍应写明参数类型');
  await shot('02-run-form-labels-units');
  await page.locator('#dialog [name="param:loops"]').fill('0.5');
  await page.locator('#dialog input[name=hardware_authorized]').check();
  await submit();
  await dlg('.form-error:has-text("圈数（loops）：需要整数")').waitFor();
  await click('close');

  // The workbench follows: an installed skill but none in the project -> still the sim path.
  await go('overview');
  assert(await nextAction() === 'sim-run-pick', '技能已安装、未加入项目时仍应推荐运行技能');

  // ---------------------------------------------------------------- (c) application steps use the generated form
  await go('skills');
  await page.locator('#main [data-action="ux-adopt"]').click();
  await dlg('[data-action="ux-adopt-one"][data-id="' + quad + '|patrol"]').click();
  await dlg('.contract-line:has-text("巡逻") :text("已加入项目")').waitFor();
  const adopted = (await st()).skills.find(s => s.entrypoint === 'patrol');
  assert(adopted && adopted.parameters.map(p => p.name).join() === 'waypoints,loops,tolerance', '加入项目的技能应保存参数说明：' + JSON.stringify(adopted));
  await click('close');
  await go('overview');
  assert(await nextAction() === 'application-add', '技能加入项目后应推荐创建应用，而不是 ' + await nextAction());

  await go('skills');
  await page.evaluate(() => action('application-add'));
  await page.locator('form[data-form=application] .step-editor .param-form').waitFor();
  const step = page.locator('#dialog .step-editor').first();
  await step.locator('[data-param-field="waypoints"] .point-row').first().waitFor();
  await page.waitForFunction(() => document.querySelectorAll('#dialog .step-editor [data-param-field="waypoints"] .point-row').length === 4, null, {timeout: 5000})
    .catch(() => { throw Error('新步骤应带上示例的 4 个航点'); });
  assert((await step.locator('[data-param-field="loops"] .param-label').innerText()).startsWith('圈数'), '步骤参数应有中文标签');
  assert(await step.locator('[data-param-field="tolerance"] .param-unit').innerText() === 'm', '步骤参数应显示单位');
  assert(!(await step.locator('[name=parameters]').isVisible()), '步骤默认不应显示 JSON 文本框');
  await fill('name', '园区巡逻');
  await step.locator('[name="param:loops"]').fill('2');
  // Advanced JSON mirrors the step's form, and edits flow back.
  await step.locator('.raw-params summary').click();
  await step.locator('.raw-params[data-synced]').waitFor({state: 'attached'});
  const stepJson = JSON.parse(await step.locator('[name=parameters]').inputValue());
  assert(stepJson.loops === 2 && stepJson.waypoints.length === 4 && stepJson.tolerance === 0.15, '步骤高级 JSON 与表单不一致：' + JSON.stringify(stepJson));
  await step.locator('[name=parameters]').fill(JSON.stringify({...stepJson, loops: 3}));
  await step.locator('.raw-params summary').click();
  await page.waitForFunction(() => document.querySelector('#dialog .step-editor [name="param:loops"]')?.value === '3', null, {timeout: 3000})
    .catch(() => { throw Error('收起高级 JSON 后步骤表单应同步为 loops=3'); });
  await shot('03-application-step-form');
  // A wrong value names the step and the parameter; nothing is saved.
  await step.locator('[name="param:loops"]').fill('1.5');
  await submit();
  await dlg('.form-error:has-text("步骤 1：请修改这些参数：圈数（loops）：需要整数")').waitFor();
  assert(!(await st()).applications.length, '参数无效时不应保存应用');
  await step.locator('[name="param:loops"]').fill('3');
  await submit();
  await page.getByText('应用交付清单', {exact: true}).waitFor();
  let app = (await st()).applications[0];
  assert(app.steps[0].parameters.loops === 3 && app.steps[0].parameters.waypoints.length === 4 && app.steps[0].parameters.tolerance === 0.15,
    '保存的步骤参数不对：' + JSON.stringify(app.steps[0].parameters));

  // "修改为新版本" shows the same form with the current values.
  await click('revision-edit', app.id);
  await page.locator('form[data-form=revision-preview] .revision-step .param-form').waitFor();
  assert(await page.locator('#dialog .revision-step [name="param:loops"]').inputValue() === '3', '修改版本时应显示当前参数');
  await page.locator('#dialog .revision-step [name="param:tolerance"]').fill('0.3');
  await fill('version', '1.1');
  await shot('04-revision-step-form');
  await submit();
  await page.locator('form[data-form=revision-save]').waitFor();
  await submit();
  await page.getByText('应用交付清单', {exact: true}).waitFor();
  const revised = (await st()).applications.find(a => a.parent_application_id === app.id);
  assert(revised && revised.steps[0].parameters.tolerance === 0.3 && revised.steps[0].parameters.loops === 3, '新版本参数不对：' + JSON.stringify(revised?.steps));
  await click('close');

  // With an application on a simulated robot, the workbench still never points at the assistant.
  await go('overview');
  const appNext = await nextAction();
  assert(['harness-run', 'deliver-open'].includes(appNext), '有仿真应用时工作台推荐不对：' + appNext);
  assert(!(await page.locator('#main .journey-next [data-action="journey-prepare"]').count()), '有仿真应用时不应推荐工程助手');

  // Manual registration: plain language; optional parameter declarations drive the step form.
  await go('skills');
  await page.evaluate(() => action('skill-add'));
  await page.locator('form[data-form=skill]').waitFor();
  const reg = await dlg('.dialog-body').innerText();
  for (const word of ['执行端技能标识', '需要的设备能力', '绑定模型']) assert(!reg.includes(word), '登记技能表单仍有行话：' + word);
  for (const want of ['机器人上的技能名', '它要用到机器人的哪些能力', '怎样算做完了', '什么情况下应该停下来']) assert(reg.includes(want), '登记技能表单缺少白话：' + want);
  assert(await dlg('[data-action="ux-adopt"]').count() === 1, '登记表单应指向“使用已安装的技能”');
  await shot('05-skill-register-plain');
  await fill('name', '原地转圈');
  await fill('entrypoint', 'spin');
  await fill('requires', 'base.velocity');
  await fill('success_condition', '转满指定圈数');
  await fill('stop_condition', '急停或超时');
  await dlg('details:has([name=parameter_decls]) summary').click();
  await fill('parameter_decls', JSON.stringify([{name: 'turns', type: 'int', default: 1, title: '转几圈', unit: '圈', description: '原地转的圈数'}]));
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await page.evaluate(() => api('skills', {project_id: projectId, name: '旧接口技能', version: '0.1.0', entrypoint: 'legacy.thing',
    requires: ['base.velocity'], model_ids: [], success_condition: '完成', stop_condition: '超时'}).then(() => refresh(true)));
  const skills = (await st()).skills;
  const spin = skills.find(s => s.entrypoint === 'spin');
  assert(spin && spin.parameters[0].title === '转几圈' && spin.parameters[0].unit === '圈', '登记的参数说明未保存：' + JSON.stringify(spin));
  await page.evaluate(() => action('application-add'));
  const first = page.locator('#dialog .step-editor').first();
  await first.locator('[name=skill_id]').selectOption({label: '原地转圈'});
  await first.locator('[data-param-field="turns"]').waitFor();
  assert((await first.locator('[data-param-field="turns"] .param-label').innerText()).startsWith('转几圈'), 'skill.json 的 title 应优先');
  assert(await first.locator('[data-param-field="turns"] .param-unit').innerText() === '圈', 'skill.json 的 unit 应显示');
  await first.locator('[name=skill_id]').selectOption({label: '旧接口技能'});
  await first.locator('.step-params textarea[name=parameters]').waitFor();
  assert(await first.locator('.step-params textarea[name=parameters]').isVisible(), '没有参数说明的技能应直接显示 JSON 文本框');
  assert(!(await first.locator('.param-form').count()), '没有参数说明的技能不应生成空表单');
  await click('close');

  // ---------------------------------------------------------------- (d) progressive disclosure
  const hiddenUntilAdvanced = async (names, label) => {
    for (const n of names) assert(!(await page.locator(`#dialog [name="${n}"]`).isVisible()), `${label}：${n} 应收在“高级”里`);
    const summary = dlg('details summary:has-text("高级")').first();
    assert(await summary.count() === 1, label + '：缺少“高级”');
    await summary.click();
    for (const n of names) assert(await page.locator(`#dialog [name="${n}"]`).isVisible(), `${label}：展开“高级”后应能看到 ${n}`);
  };
  await go('models');
  await page.evaluate(() => action('model-add'));
  await page.locator('form[data-form=model]').waitFor();
  let text = await dlg('.dialog-body').innerText();
  assert(!text.includes('契约') || text.indexOf('契约') > text.indexOf('高级'), '模型表单的“契约”应在“高级”里');
  assert(await dlg('[data-action="service-inference"]').count() === 1, '没有推理服务时应给出添加按钮');
  await shot('06-model-add-basic');
  await hiddenUntilAdvanced(['input_contract', 'output_contract'], '接入模型');
  assert((await page.locator('#dialog [name=input_contract]').inputValue()).length > 0, '契约应有默认说明，基本使用不必填写');
  await click('close');

  await go('services');
  assert(!(await page.locator('#main').innerText()).includes('版本化协议'), '自有服务页仍有“版本化协议”');
  await page.evaluate(() => action('service-add'));
  await page.locator('form[data-form=service]').waitFor();
  await hiddenUntilAdvanced(['token'], '接入自己的服务');
  await click('close');

  await go('extensions');
  await page.evaluate(() => action('engine-ext-mcp-add'));
  await page.locator('form[data-form=engine-ext-mcp]').waitFor();
  text = await dlg('.dialog-body').innerText();
  assert(!text.includes('Streamable HTTP') && !text.includes('Bearer'), '工具服务表单的协议术语应收在“高级”里');
  await dlg('details summary:has-text("高级")').click();
  assert((await dlg('.dialog-body').innerText()).includes('Streamable HTTP'), '展开“高级”后应说明协议');
  await shot('07-mcp-add-basic');
  await click('close');

  await go('simulation');
  await page.evaluate(() => action('scene-add'));
  await page.locator('form[data-form=scene]').waitFor();
  assert(!(await page.locator('#dialog [name=scene_ref]').isVisible()), '场景版本地址应收在“高级”里');
  await shot('08-scene-add-basic');
  await fill('name', '仓库通道 A');
  await fill('description', '两排货架');
  await fill('engine', 'Continuum');
  await submit();                                   // scene_ref is required and still collapsed
  await page.locator('#dialog [name=scene_ref]').waitFor({state: 'visible', timeout: 3000})
    .catch(() => { throw Error('必填的高级字段为空时，应自动展开“高级”'); });
  assert(!(await st()).scenes.length, '缺少必填项时不应保存场景');
  await fill('scene_ref', 'scene://warehouse/a');
  await submit();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  const scene = (await st()).scenes[0];
  assert(scene && scene.scene_ref === 'scene://warehouse/a' && scene.version === '1', '场景保存不对：' + JSON.stringify(scene));

  await go('overview');
  await shot('09-workbench-after');
  assert(!errors.length, '页面错误：' + errors.join(' | '));
  await browser.close();
  console.log(JSON.stringify({passed: true, sidebar: nav, screenshots: shots}));
})().catch(err => { console.error(err); process.exit(1); });
