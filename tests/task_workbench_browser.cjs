// Browser acceptance for issue #44: 任务工作台 is a dashboard. It shows how many tasks the account has
// and what they are called, then every task as a card; a task is created from the head and deleted from
// its card; one task is a page of its own (#task).
// Issue #46: every card is the same size at every width and whatever its texts; the first-run guide is
// there on the first visit only; the head has no 「使用指南」 and the page does not say whose tasks these are.
//   node tests/task_workbench_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ux01_browser_fixture.py (a fresh state directory; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const LONG = '每天晚上十点到早上六点，沿仓库 A 区和 B 区的主通道巡检一遍。发现货架倾斜、地面有遗落物或通道被占用时拍照，' +
  '记录位置，并在早上八点前生成一份报告发给值班经理。电量低于百分之二十时自己回到充电位，充到八成再继续。遇到人时停下并让行。';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ux01-qa.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const card = name => main.locator('.tw-card', {hasText: name});
  const count = async () => Number(await main.locator('[data-tw-count]').innerText());
  const hash = () => page.evaluate(() => location.hash);
  const cardBoxes = () => main.locator('.tw-wall .tw-card').evaluateAll(els => els.map(el => {
    const r = el.getBoundingClientRect(); return {x: Math.round(r.x), w: Math.round(r.width * 2) / 2, h: Math.round(r.height * 2) / 2}; }));
  const sameSize = boxes => new Set(boxes.map(b => b.w)).size === 1 && new Set(boxes.map(b => b.h)).size === 1;
  const noSidewaysScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth &&
    document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth);
  // The page scrolls inside #content-scroll: for a picture of all of it the window is made as high as the content.
  const shot = async name => {
    const size = page.viewportSize(), high = await page.evaluate(() => document.querySelector('#content-scroll').scrollHeight + 80);
    await page.setViewportSize({width: size.width, height: Math.max(size.height, Math.min(high, 4000))});
    await page.screenshot({path: `${shots}/task-workbench-${name}.png`, fullPage: true});
    await page.setViewportSize(size);
  };
  const password = () => 'task1-' + crypto.randomBytes(8).toString('hex');
  const account = () => `tasks-${crypto.randomBytes(4).toString('hex')}@example.test`;
  const create = async (name, objective) => {
    await main.locator('.tw-head [data-action="journey-start"]').click();
    await page.locator('form[data-form=project]').waitFor();
    await fill('name', name); await fill('objective', objective);
    await submit();
    await main.locator('.tw-back').waitFor();
  };
  const home = async () => { await main.locator('.tw-back a').click(); await main.locator('.tw-board').waitFor(); };
  const first = account();

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '任务验收', email: first, password: password()});

  // 1. The entry is the dashboard. A new account has no task: the count says so and the page offers the first.
  await main.locator('.tw-board').waitFor();
  check(await main.locator('h1').innerText() === '任务工作台', 'The entry page is not titled 任务工作台');
  check(await count() === 0, 'A new account does not start with 0 tasks');
  check(await main.locator('.tw-empty [data-action="journey-start"]').isVisible(), 'The empty dashboard does not offer the first task');
  check(await main.locator('.tw-wall').count() === 0, 'A wall is shown without a task');
  check(await main.locator('.journey-next').count() === 0, 'The step-by-step view of one task is still on the entry page');
  const entry = await main.innerText();
  check(!entry.includes(first) && !entry.includes('其他账号看不到'), 'The dashboard still says whose tasks these are');
  check(!entry.includes('使用指南') && await main.locator('[data-action="task-guide"]').count() === 0, 'The head still has 使用指南');
  check(await main.locator('.tw-head button').count() === 1, 'The head has more than 新建任务');
  // 2. The first visit shows the first-run guide, and keeps it while the page is drawn again.
  check(await main.locator('.first-run').isVisible(), 'The first visit has no first-run guide');
  await page.evaluate(() => render());
  check(await main.locator('.first-run').isVisible(), 'The first-run guide left during the first visit');
  check(await noSidewaysScroll(), 'The empty dashboard scrolls sideways');
  await shot('1-empty');

  // 3. Creating a task leads into it: the page of one task, known to the sidebar and the breadcrumb.
  await create('仓库夜间巡检', LONG);
  check(await hash() === '#task', `A new task does not open as its own page: ${await hash()}`);
  await main.locator('.journey-next').getByText('连接你的机器人', {exact: true}).waitFor();
  check(await main.locator('h1').innerText() === '仓库夜间巡检', 'The page of a task is not titled with its name');
  check(await page.locator('#breadcrumb').innerText() === '任务工作台 / 仓库夜间巡检', 'The breadcrumb does not name the task');
  check(await page.locator('#navigation a[aria-current="page"]').getAttribute('href') === '#overview', 'The sidebar does not mark 任务工作台');
  check(await main.getByText('我的任务项目').count() === 0, 'The old list of tasks is still at the foot of a task');
  await shot('2-inside-a-task');
  // A reload (or a saved link) stays on the task: the page has no state for a moment and must not leave.
  await page.reload();
  await main.locator('.tw-back').waitFor();
  check(await hash() === '#task' && await main.locator('h1').innerText() === '仓库夜间巡检', 'Reloading the page of a task leaves it');

  // 4. Back on the dashboard: one task, by name and as a card.
  await home();
  check(await hash() === '#overview', 'The way back does not lead to the dashboard');
  // The page was loaded again meanwhile: this is the second visit, the first-run guide is gone.
  check(await main.locator('.first-run').count() === 0, 'The first-run guide is still there on the second visit');
  check(await count() === 1, 'The count is not 1 after the first task');
  check(await main.locator('.tw-names button').allInnerTexts().then(t => t.join('|').includes('仓库夜间巡检')), 'The dashboard does not name the task');
  check(await card('仓库夜间巡检').locator('.tw-objective').innerText() === LONG, 'The card does not show the goal');
  check((await card('仓库夜间巡检').locator('.tw-track-text').innerText()).includes('下一步：连接机器人'), 'The card does not say what is next');

  // 5. More tasks: the count and the names follow; the wall has columns, and every card is the same size
  //    although one goal is a paragraph and another a few words.
  await create('前台接待', '引导访客到会议室。'); await home();
  await create('工位上下料', '把料盘从推车搬到三号工位，空盘放回推车。'); await home();
  await create('园区巡逻', '白天每两小时沿园区外圈走一圈。'); await home();
  check(await count() === 4, 'The count is not 4');
  const names = await main.locator('.tw-names button span').allInnerTexts();
  check(['仓库夜间巡检', '前台接待', '工位上下料', '园区巡逻'].every(n => names.includes(n)), `The dashboard does not name every task: ${names}`);
  check(await main.locator('.tw-wall .tw-card').count() === 4, 'The wall does not have every task');
  const boxes = await cardBoxes();
  check(new Set(boxes.map(b => b.x)).size >= 2, `The wall is one column at 1440: ${JSON.stringify(boxes)}`);
  check(sameSize(boxes), `The cards are not the same size at 1440: ${JSON.stringify(boxes)}`);
  const parts = await main.locator('.tw-wall .tw-card').evaluateAll(els => els.map(el =>
    [...el.querySelectorAll('h3, .tw-objective, .tw-track-text, .tw-hold dd')].every(part => part.scrollWidth <= part.clientWidth + 1 || getComputedStyle(part).overflow === 'hidden')));
  check(parts.every(Boolean), 'A text of a card is not kept inside its line');
  check(await main.locator('.tw-card.current').count() === 1, 'The task opened last is not marked');
  check(await noSidewaysScroll(), 'The dashboard scrolls sideways at 1440');
  await shot('3-dashboard');

  // 6. Into a task by its name on the board, and by the keyboard from its card.
  await main.locator('.tw-names button', {hasText: '前台接待'}).click();
  await main.locator('.tw-back').waitFor();
  check(await main.locator('h1').innerText() === '前台接待', 'The name on the board does not open that task');
  await home();
  await card('工位上下料').locator('[data-action="task-open"]').focus();
  await page.keyboard.press('Enter');
  await main.locator('.tw-back').waitFor();
  check(await main.locator('h1').innerText() === '工位上下料', 'Enter on 进入任务 does not open the task');
  await home();
  check(await main.locator('.tw-card.current h3').innerText() === '工位上下料', 'The mark did not move to the task opened last');

  // 7. Deleting: the dialog says what goes; cancelling keeps the task; confirming removes it.
  await card('前台接待').locator('[data-action="task-remove"]').click();
  await dialog.getByText('只删除任务本身').waitFor();
  check((await dialog.locator('.dialog-head').innerText()).includes('前台接待'), 'The dialog does not name the task');
  await dialog.getByText('删除后不能恢复').waitFor();
  await shot('4-delete-dialog');
  await page.locator('#dialog .dialog-footer [data-action="close"]').click();
  check(await count() === 4, 'Cancelling deleted the task');
  await card('前台接待').locator('[data-action="task-remove"]').click();
  await page.locator('#dialog [data-action="task-remove-confirm"]').click();
  await card('前台接待').waitFor({state: 'detached'});
  check(await count() === 3, 'The count did not go down after deleting');
  check(!(await main.locator('.tw-names').innerText()).includes('前台接待'), 'The deleted task is still named on the board');
  const kept = await page.evaluate(async () => (await api('state')).projects.map(p => p.name));
  check(!kept.includes('前台接待') && kept.length === 3, `The server still has the task: ${kept}`);

  // 8. A task with a robot is not deleted: the dialog says why and leads to that task's robots.
  const patrol = await page.evaluate(async () => {
    const task = state.projects.find(p => p.name === '园区巡逻');
    await api('robots', {project_id: task.id, name: '巡逻狗', family: 'quadruped'});
    await refresh(true);
    return task.id;
  });
  check((await card('园区巡逻').locator('.tw-holds').innerText()).includes('巡逻狗'), 'The card does not name the task\'s robot');
  await card('园区巡逻').locator('[data-action="task-remove"]').click();
  await dialog.getByText('还有 1 台机器人（巡逻狗）').waitFor();
  check(await dialog.locator('[data-action="task-remove-confirm"]').count() === 0, 'A task with a robot can be confirmed for deletion');
  await shot('5-delete-blocked');
  await dialog.locator('[data-action="task-robots"]').click();
  await main.locator('article.card', {hasText: '巡逻狗'}).waitFor();
  check(await hash() === '#robots', 'The way to the robots does not open 我的机器人');
  check(await page.evaluate(id => projectId === id, patrol), 'The robots shown are not those of that task');
  await page.evaluate(() => { location.hash = 'overview'; });
  await main.locator('.tw-board').waitFor();

  // 9. The server decides: a robot added after the dialog opened keeps the task, and the dialog says why.
  await card('工位上下料').locator('[data-action="task-remove"]').click();
  await page.locator('#dialog [data-action="task-remove-confirm"]').waitFor();
  await page.evaluate(async () => {
    const task = state.projects.find(p => p.name === '工位上下料');
    await api('robots', {project_id: task.id, name: '上料臂', family: 'manipulator'});
  });
  await page.locator('#dialog [data-action="task-remove-confirm"]').click();
  await dialog.getByText('还有 1 台机器人（上料臂）').waitFor();
  check(await dialog.locator('[data-action="task-remove-confirm"]').count() === 0, 'The refused deletion can be confirmed again');
  await page.locator('#dialog [data-action="close"]').first().click();
  check(await count() === 3, 'A refused deletion changed the count');
  // Inside a task: no "one of N tasks", no 新建任务项目 or 使用指南 in its head (the skills section has its own script).
  await card('园区巡逻').locator('[data-action="task-open"]').click();
  await main.locator('.tw-back').waitFor();
  check((await main.locator('.tw-back').innerText()).trim() === '← 全部任务', 'The page of a task says more than the way back above its head');
  const inTask = await main.locator('.page-head button').allInnerTexts();
  check(!inTask.some(t => t.includes('新建任务项目') || t.includes('使用指南')), `The head of a task still has: ${inTask}`);
  await main.locator('#task-skills').getByText('巡逻狗').waitFor();
  await home();

  // 10. Every width: nothing scrolls sideways, the wall goes down to one column, and the cards stay the same
  //     size: by now one task has a long goal, two have a robot, and a fourth has the longest name allowed.
  await page.evaluate(async () => { await api('projects', {name: '名称很长的任务'.repeat(14) + '到头', objective: '短'}); await refresh(true); });
  await main.locator('.tw-wall .tw-card').nth(3).waitFor();
  for (const width of [320, 375, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(350);      // the sidebar slides away below 600
    check(await noSidewaysScroll(), `The dashboard scrolls sideways at ${width}`);
    const inside = await main.locator('.tw-card, .tw-board').evaluateAll(els => els.every(el => {
      const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= document.documentElement.clientWidth + 1;
    }));
    check(inside, `A card or the board leaves the window at ${width}`);
    const sizes = await cardBoxes();
    check(sizes.length === 4 && sameSize(sizes), `The cards are not the same size at ${width}: ${JSON.stringify(sizes)}`);
    if ([320, 768, 1920].includes(width)) await shot(`6-width-${width}`);
  }
  const narrow = await (async () => { await page.setViewportSize({width: 375, height: 900}); await page.waitForTimeout(350);      // the sidebar slides away below 600
    return main.locator('.tw-wall .tw-card').evaluateAll(els => new Set(els.map(el => Math.round(el.getBoundingClientRect().x))).size); })();
  check(narrow === 1, 'The wall is not one column on a phone');
  await page.setViewportSize({width: 1440, height: 1000});

  // 11. #task without a task is the dashboard (the last task was deleted, or an old link).
  await card('仓库夜间巡检').locator('[data-action="task-remove"]').click();
  await page.locator('#dialog [data-action="task-remove-confirm"]').click();
  await card('仓库夜间巡检').waitFor({state: 'detached'});

  // 12. Tasks belong to the account: another account starts with none and sees none of these.
  await page.evaluate(() => action('logout'));
  await page.locator('#auth-gate form[data-acct-form]').waitFor();
  await gateSignIn(page, {name: '另一个账号', email: account(), password: password()});
  await main.locator('.tw-board').waitFor();
  check(await count() === 0, 'Another account sees tasks');
  check(!(await main.innerText()).includes('园区巡逻'), 'Another account sees the first account\'s task');
  await page.evaluate(() => { location.hash = 'task'; });
  await main.locator('.tw-board').waitFor();
  await page.waitForFunction(() => location.hash === '#overview');
  check(await main.locator('.tw-back').count() === 0, '#task without a task is not the dashboard');

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('task_workbench_browser: passed' + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(e => { console.error(e); process.exit(1); });
