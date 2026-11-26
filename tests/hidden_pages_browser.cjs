// Browser acceptance for issue #49 (功能简化): 「工程助手」(with 协作流程), 「助手工具」 and 「自有服务」 are
// hidden for now. They are not in the sidebar or the account menu, their addresses open 任务工作台, and
// no other page or dialog still leads to them; with the test switch on, the pages are back unchanged.
//   node tests/hidden_pages_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ux01_browser_fixture.py (a fresh state directory; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const {showHiddenPages} = require('./hidden_pages_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const HIDDEN = {agents: '工程助手', workflows: '协作流程', extensions: '助手工具', services: '自有服务'};
const NAV_BEFORE = 18;          // tests/ux03_browser.cjs counts the sidebar the same way

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ux01-qa.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog[open]');
  const navTexts = p => p.locator('#navigation a').evaluateAll(links => links.map(a => a.innerText.trim()));
  const leadsToHidden = (p, scope) => p.locator(scope).evaluate((root, pages) =>
    [...root.querySelectorAll('[data-action], a[href^="#"]')]
      .filter(el => pages.includes((el.dataset.action || '').replace(/^go-/, '')) && (el.dataset.action || '').startsWith('go-') ||
                    pages.includes((el.getAttribute('href') || '').slice(1)))
      .map(el => el.outerHTML.slice(0, 120)), Object.keys(HIDDEN));

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '功能简化验收', email: `hidden-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'hidden1-' + crypto.randomBytes(8).toString('hex')});
  await main.locator('.tw-board').waitFor();

  // 1. The sidebar and the account menu no longer list the three pages; everything else is where it was.
  const nav = await navTexts(page);
  check(!nav.some(t => Object.values(HIDDEN).includes(t)), `The sidebar still lists a hidden page: ${nav}`);
  check(nav.length === NAV_BEFORE - 3, `The sidebar lost or gained something else: ${nav}`);
  for (const kept of ['任务工作台', '我的机器人', '机器人应用', '模型库', '平台模型与算力', '用量与账单', '云平台账户']) {
    check(nav.includes(kept), `The sidebar lost 「${kept}」: ${nav}`);
  }
  await page.locator('#user-menu-toggle').click();
  await page.locator('#user-menu').waitFor();
  const menu = await page.locator('#user-menu button').allInnerTexts();
  check(!menu.some(t => t.includes('助手工具') || t.includes('服务连接')), `The account menu still leads to a hidden page: ${menu}`);
  check(menu.some(t => t.includes('云平台账户')) && menu.some(t => t.includes('用量与账单')) && menu.some(t => t.includes('使用指南')),
        `The account menu lost something else: ${menu}`);
  await page.screenshot({path: `${shots}/hidden-pages-1-sidebar-and-menu.png`});
  await page.keyboard.press('Escape');

  // 2. The address of a hidden page opens 任务工作台, typed in the address bar or opened afresh.
  for (const route of Object.keys(HIDDEN)) {
    await page.evaluate(r => { location.hash = r; }, route);
    await page.waitForFunction(() => location.hash === '#overview');
    await main.locator('.tw-board').waitFor();
    check(!(await main.innerText()).includes('让助手完成工程工作'), `#${route} still draws 工程助手`);
    await page.evaluate(() => { location.hash = 'robots'; });
    await page.waitForFunction(() => location.hash === '#robots');
  }
  for (const route of Object.keys(HIDDEN)) {
    await page.goto('about:blank');
    await page.goto(cfg.url + '/#' + route);
    await main.locator('.tw-board').waitFor();
    check(await page.evaluate(() => location.hash) === '#overview', `Opening #${route} afresh did not end on 任务工作台`);
    check((await page.locator('#breadcrumb').innerText()).trim() === '任务工作台', `The breadcrumb of #${route}: ${await page.locator('#breadcrumb').innerText()}`);
  }

  // 3. The page of a task: five steps as before, 准备能力 opens 机器人应用, and no card sends people to 工程助手.
  await page.evaluate(() => { location.hash = 'overview'; });
  await main.locator('.tw-head [data-action="journey-start"]').click();
  await page.locator('#dialog [name="name"]').fill('仓库巡检');
  await page.locator('#dialog [name="objective"]').fill('每天检查仓库通道并报告异常。');
  await page.locator('#dialog button[type=submit]').click();
  await main.locator('.tw-back').waitFor();
  check((await leadsToHidden(page, '#main')).length === 0, `The page of a task still leads to a hidden page: ${await leadsToHidden(page, '#main')}`);
  await page.screenshot({path: `${shots}/hidden-pages-2-task.png`, fullPage: true});
  // The step view of a task (web/journey.js). Issue #50 draws the page of a task without it: then there
  // is nothing more to check here, and the rules for a step and a card stay in tests/hidden_pages_logic.cjs.
  if (await main.locator('.journey-step').count()) {
    check(await main.locator('.journey-step').count() === 5, 'The page of a task no longer has five steps');
    const options = await main.locator('.journey-options .journey-option h3').allInnerTexts();
    check(!options.includes('让助手帮忙开发') && options.includes('选用现成模型') && options.includes('在仿真中试运行'),
          `The optional abilities of a task: ${options}`);
    await main.locator('.journey-step', {hasText: '准备能力'}).click();
    await page.waitForFunction(() => location.hash === '#skills' && document.querySelector('#breadcrumb').textContent.trim() === '机器人应用');
  }

  // 4. Every page that is still shown: nothing on it leads to a hidden page.
  for (const route of ['robots', 'skills', 'simulation', 'harness', 'deployment', 'models', 'training', 'datasets', 'resources', 'billing', 'cloud']) {
    await page.evaluate(r => { location.hash = r; }, route);
    await page.waitForFunction(r => location.hash === '#' + r && document.querySelector('#navigation a.active')?.getAttribute('href') === '#' + r, route);
    await main.locator('h1').first().waitFor();
    const left = await leadsToHidden(page, '#main');
    check(left.length === 0, `#${route} still leads to a hidden page: ${left}`);
  }

  // 5. Dialogs: a link to a hidden page is taken out, what the dialog is for stays.
  await page.evaluate(async () => {
    await api('services', {project_id: projectId, name: '团队仿真环境', kind: 'simulation', endpoint: 'http://127.0.0.1:9'});
    await refresh(true);
    await action('plan-choose', 'simulation');
  });
  await dialog.getByText('团队仿真环境').waitFor();
  check((await dialog.innerText()).includes('尚未通过连接检查'), 'The dialog no longer says why the service cannot be chosen');
  check((await leadsToHidden(page, '#dialog')).length === 0, `The task dialog still leads to a hidden page: ${await leadsToHidden(page, '#dialog')}`);
  check(await dialog.locator('[data-action="resource-simulation"]').count() === 1, 'The task dialog lost its other buttons');
  await page.screenshot({path: `${shots}/hidden-pages-3-dialog.png`});
  // 「提出工程需求」 outside 工程助手 (the device profile offers it): its work is listed only on that page.
  await page.evaluate(() => dialog('设备档案', '验收用', '<div class="dialog-body"><div class="button-row">' +
    button('查看来源调查', 'engine-session', 'secondary small', 'x') + button('交给工程助手开发', 'engine-work', 'secondary small') +
    button('本体适配：生成适配器并测试', 'engine-flow-adapt', 'small', 'x') + '</div></div>'));
  await dialog.locator('[data-action="engine-flow-adapt"]').waitFor();
  check(await dialog.locator('[data-action="engine-work"]').count() === 0, '「交给工程助手开发」 is still offered');
  check(await dialog.locator('[data-action="engine-session"]').count() === 1 && await dialog.locator('[data-action="engine-flow-adapt"]').count() === 1,
        'The buttons next to it were taken out too');
  await dialog.locator('[data-action="close"]').click();

  // 6. Hidden, not deleted: with the test switch the pages are back, with the records made above.
  const shown = await context.newPage();
  shown.on('pageerror', e => errors.push(e.message));
  await showHiddenPages(shown);
  await shown.goto(cfg.url + '/#services');
  await shown.locator('#main article.card', {hasText: '团队仿真环境'}).waitFor();
  const all = await navTexts(shown);
  check(all.length === NAV_BEFORE && ['工程助手', '助手工具', '自有服务'].every(t => all.includes(t)), `With the switch the sidebar is: ${all}`);
  await shown.goto(cfg.url + '/#agents');
  await shown.locator('#main').getByText('让助手完成工程工作').waitFor();
  await shown.locator('#main [data-action="go-workflows"]').click();
  await shown.waitForFunction(() => location.hash === '#workflows');
  await shown.goto(cfg.url + '/#extensions');
  await shown.locator('#main').getByText('MCP 服务').first().waitFor();

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('hidden_pages_browser: passed' + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(e => { console.error(e); process.exit(1); });
