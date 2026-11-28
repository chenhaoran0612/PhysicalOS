// Issue #73 browser acceptance (「应用编排」): the form that creates an application offers what 「技能发布」 published
// for the brand and model, although no robot of it is connected (the situation of the issue's screenshot).
//   0  an L2-U that is not connected, a task for it, the console signed in to the cloud, nothing published yet:
//      the form says there is nothing, and both ways to get skills;
//   1  the bundled examples are published to the cloud for 智身科技 钢镚 L2-U, as 「技能发布」 does;
//   2  the form offers them, counts them as published for the brand and model, and a step gets the
//      parameters of the release;
//   3  saving creates the application with that skill, bound to the task;
//   4  a robot of another brand and model is offered none of them;
//   5  signed out, the form says why the cloud's are not listed (what was made ready before stays);
//   6  the form does not overflow at 320, 375 and 1440.
// Fixture: tests/app_cloud_skills_browser_fixture.py (a stand-in cloud; no device, no real machine).
// Usage: node tests/app_cloud_skills_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DESKTOP = {width: 1440, height: 1000}, FORM = 'form[data-form="application"]';
const L2U = {vendor: '智身科技', model: '钢镚 L2-U'};
const PUBLISHED = ['去指定地点', '巡检', '巡逻', '挥手', '无图自由走', '状态报告'];
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2]));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `app-cloud-skills-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), main = page.locator('#main'), form = dialog.locator(FORM);
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const has = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const lacks = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (text.includes(t)) throw Error(`"${t}" should not be in: ${text.slice(0, 900)}`);
  };
  const post = (route, body) => page.evaluate(async ([r, b]) => {
    const response = await fetch('/api/' + r, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
    const result = await response.json();
    if (!response.ok) throw Error(r + ': ' + result.error);
    return result;
  }, [route, body]);
  const fixture = async what => (await page.request.get(`${cfg.url}/fixture/${what}`)).json();
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const open = async hash => { await page.goto(cfg.url + '/#' + hash); await page.evaluate(() => refresh(true)); await page.locator('#main .page-head').first().waitFor(); };
  // Open the create form on a task and wait until the server has made the brand and model's skills ready.
  const openForm = async taskId => {
    await open('skills');
    await main.locator('[data-action="application-add"]').click();
    await form.waitFor();
    await form.locator('[name="task_id"]').selectOption(taskId);
    await page.waitForFunction(() => {
      const text = document.querySelector('#dialog[open] .ac-status')?.textContent || '';
      return text && !text.includes('正在准备');
    });
    return (await form.locator('.ac-status').innerText()).trim();
  };
  // The skills a step offers. A step may be there from the brand and model the form opened with; with no skill its
  // only option is 「这个品牌型号还没有技能」, which has no value.
  const offered = () => form.locator('.step-editor').count().then(n => n
    ? form.locator('.step-editor select[name="skill_id"]').first().locator('option:not([disabled]):not([value=""])').allInnerTexts() : []);
  const close = () => dialog.locator('[data-action="close"]').first().click();
  const result = {browser: engine.name()};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '应用编排验收', email: `cloud-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'app73a-' + crypto.randomBytes(8).toString('hex')});
  same((await fixture('cloud-login')).releases, [], 'the stand-in cloud starts empty');

  // ---------------------------------------------------------------- 0. nothing published yet
  const home = (await appState()).robot_home;
  const l2u = await post('robots', {project_id: home, name: 'L2U', family: 'wheel_legged', ...L2U});
  must(l2u.status !== 'connected', 'the L2-U is not connected, as in the issue');
  const task = await post('projects', {name: '办公室每日巡检', objective: '每天把办公室巡检一遍', robot_id: l2u.id});
  let status = await openForm(task.id);
  await has(form.locator('.ac-task-note'), ['智身科技 钢镚 L2-U', '品牌型号跟着任务定']);
  must(status.includes('没有连接') && status.includes('「技能发布」里也没有发布给它的技能'), 'nothing anywhere: ' + status);
  same(await offered(), [], 'no skill to choose yet');
  await page.screenshot({path: file('0-nothing-published')});
  await close();

  // ---------------------------------------------------------------- 1. published for 钢镚 L2-U
  const sent = await post('cloud-skills/publish-examples', L2U);
  same(sent.results.map(r => r.status), PUBLISHED.map(() => 'published'), 'every bundled example was published');
  same((await fixture('state')).releases.length, PUBLISHED.length, 'the cloud holds them');
  must((await fixture('state')).releases.every(r => r.endsWith('钢镚 L2-U')), 'each for 钢镚 L2-U');

  // ---------------------------------------------------------------- 2. the form offers them
  status = await openForm(task.id);
  must(status.includes(`${PUBLISHED.length} 个技能可以直接选用（${PUBLISHED.length} 个来自「技能发布」）`), 'counted as published for it: ' + status);
  must(status.includes('连接后才列得出') && !status.includes('先连接一台，再回来创建'), 'no robot connected does not stop them: ' + status);
  const choices = await offered();
  same([...choices].sort(), [...PUBLISHED].sort(), 'the step offers what was published for the brand and model');
  result.offered = choices;
  const step = form.locator('.step-editor').first();
  await step.locator('select[name="skill_id"]').selectOption({label: '去指定地点'});
  await has(step.locator('.step-params'), ['去哪里']);              // the parameters of the release make the step's form
  await page.screenshot({path: file('2-published')});

  // ---------------------------------------------------------------- 3. saved with that skill, bound to the task
  await form.locator('[name="name"]').fill('办公室巡检');
  await form.locator('[type="submit"]').click();
  await dialog.locator('.delivery-detail').waitFor();
  const state = await appState();
  const made = state.applications.find(a => a.name === '办公室巡检');
  must(made, 'the application was created');
  same(made.steps.map(s => s.entrypoint), ['goto_place'], 'with the published skill as its step');
  same([made.product.vendor, made.product.model], [L2U.vendor, L2U.model], 'for 智身科技 钢镚 L2-U');
  must(state.task_applications.some(b => b.project_id === task.id && b.application_id === made.id), 'bound to the task');
  await close();

  // ---------------------------------------------------------------- 4. another brand and model gets none of them
  const arm = await post('robots', {project_id: home, name: '协作臂', family: 'manipulator', vendor: 'Acme', model: 'A1'});
  const armTask = await post('projects', {name: '上料', objective: '把料送到工位', robot_id: arm.id});
  status = await openForm(armTask.id);
  same(await offered(), [], 'nothing of the L2-U is offered to another brand and model');
  must(status.includes('「技能发布」里也没有发布给它的技能'), 'and the form says so: ' + status);
  await close();

  // ---------------------------------------------------------------- 5. signed out
  await fixture('cloud-logout');
  status = await openForm(task.id);
  must(status.includes('没有登录云平台'), 'signed out, the form says why the cloud\'s are not listed: ' + status);
  same((await offered()).length, PUBLISHED.length, 'what was made ready before is still offered');

  // ---------------------------------------------------------------- 6. widths
  for (const width of [320, 375, 1440]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(350);          // the sidebar slides away on a narrow page
    must(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), `no sideways scroll at ${width}`);
    await page.screenshot({path: file(`6-form-${width}`)});
  }
  await page.setViewportSize(DESKTOP);
  await close();

  must(!errors.length, 'page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ok: true, ...result}, null, 1));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
