// Issue #70 browser acceptance: 仿真试运行 / 验证与测试 / 部署与运行 show what was recorded, and a row opens
// the page of that record with the data of the run.
//   1  with nothing recorded the three pages have an empty board and say what will be listed;
//   2  a 巡检 run on a simulated robot is followed: its row ends as 已完成 with its time and distance;
//   3  the page of the record: the numbers, the five stages, the route, the series, the events;
//      a reload stays on it, 「←」 goes back to the list it was opened from;
//   4  部署与运行 lists every request sent to a robot, the newest first; 验证与测试 keeps its actions;
//   5  nothing is wider than the page at 375 / 768 / 1440.
// Fixture: tests/run_boards_browser_fixture.py (a simulated quadruped; no real machine).
// Usage: node tests/run_boards_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-run-boards-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const file = name => path.join(dir, `run-boards-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure'), fullPage: true}).catch(() => {}); console.error('page errors:', errors); };
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const main = page.locator('#main');
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) if (!text.includes(t)) throw Error(`Missing "${t}" in: ${text.slice(0, 900)}`);
  };
  const open = async route => { await page.evaluate(r => { location.hash = r; }, route); await page.locator(`#navigation a[href="#${route}"].active`).waitFor(); };
  const shots = async name => {
    await page.evaluate(() => document.querySelector('#toast').classList.remove('show'));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: DESKTOP.height});
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      must(overflow <= 1, `${name} is ${overflow}px wider than the page at ${width}px`);
      await page.screenshot({path: file(`${name}-${width}`), animations: 'disabled', fullPage: true});
    }
    await page.setViewportSize(DESKTOP);
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '巡检团队', email: `runs-${crypto.randomBytes(4).toString('hex')}@example.test`, password: crypto.randomBytes(12).toString('hex') + 'a1'});

  // 1 nothing recorded yet
  await open('harness');
  await expectText(main, ['验证与测试', '测试记录', '还没有记录', '还没有测试记录']);
  must(await main.locator('.rb-board .rb-count strong').innerText() === '0', 'an empty board counts 0');
  must(await main.locator('.empty [data-action="harness-pick"]').count() === 1, 'the empty list offers the way to a first check');
  must(await main.locator('.page-head [data-action]').count() === 3, 'the three actions of the page stay in its head');
  await open('deployment');
  await expectText(main, ['部署与运行', '已发布的应用', '暂无设备操作']);
  await open('simulation');
  await main.locator('[data-rb-sim]').waitFor();
  await expectText(main.locator('[data-rb-sim]'), ['仿真运行记录', '还没有仿真记录']);
  must(await main.locator('[data-rb-sim]').count() === 1 && await main.locator('[data-rb-sim-board]').count() === 1, 'the board and the list are on the page once');
  must(await main.evaluate(el => el.querySelector('.page-head').nextElementSibling.matches('[data-rb-sim-board]')), 'the board is right under the head of the page');
  must(await main.evaluate(el => el.lastElementChild.matches('[data-rb-sim]')), 'the list is at the foot of the page');

  // 2 a simulated quadruped runs 巡检 and is followed
  const robot = await page.evaluate(async () => {
    const made = await api('sims', {project_id: state.robot_home, kind: 'quadruped'});
    await refresh(true);
    return made.robot || made;
  });
  must(robot && robot.id, 'the simulated robot was added: ' + JSON.stringify(robot).slice(0, 300));
  await page.waitForFunction(id => state.robots.some(r => r.id === id && r.status === 'connected'), robot.id, {timeout: 90000});
  const run = await page.evaluate(async id => {
    const preview = await api('skills/package-preview', {project_id: state.robot_home, robot_id: id, example: 'patrol_places'});
    await api('skills/package-install', {project_id: state.robot_home, robot_id: id, example: 'patrol_places', request_id: 'install-patrol', package_digest: preview.package_digest});
    const op = await api('robots/run-skill', {project_id: state.robot_home, robot_id: id, skill_id: 'patrol_places', request_id: 'patrol-1', parameters: {}, hardware_authorized: true});
    await refresh(true);
    return op;
  }, robot.id);
  must(run.status === 'accepted', '巡检 was accepted: ' + JSON.stringify(run).slice(0, 400));
  const row = main.locator(`[data-rb-sim] tr.rb-row[data-id="op:${run.id}"]`);
  await row.waitFor();
  await expectText(row, ['运行技能', 'patrol_places', '仿真']);
  await row.locator('.status:has-text("运行中")').waitFor({timeout: 30000});
  await row.locator('.status:has-text("已完成")').waitFor({timeout: 180000});
  const data = await row.locator('.rb-data').innerText();
  must(/秒|分/.test(data) && /米/.test(data), 'the row has the time and the distance of the run: ' + data);
  await expectText(main.locator('[data-rb-sim-board] .rb-board'), ['1', '100%', '完成 / 通过']);
  await shots('simulation');

  // 3 the page of the record
  await row.locator('[data-action="rb-open"]').click();
  await page.waitForFunction(() => location.hash === '#record');
  await main.locator('.rr-path polyline').waitFor();
  await expectText(main, ['RUN RECORD', '运行技能 · patrol_places', '已完成', '用时', '走过的距离', '这台机器人走到了哪一步', '运行过程', '起点', '终点', '设备记下的事件', '运行结束']);
  must(await main.locator('.rr-flow li').count() === 5, 'the five stages are drawn');
  must(await main.locator('.rr-flow li.done').count() === 3, 'a simulated robot with a skill and a run has reached three stages');
  must(await main.locator('.rr-series').count() >= 1, 'at least one series is drawn');
  must(await main.locator('.rr-events li').count() >= 2, 'the events of the run are listed');
  must(await page.locator('#navigation a[href="#simulation"].active').count() === 1, 'the sidebar marks the list the record was opened from');
  must((await page.locator('#breadcrumb').innerText()).includes('仿真试运行 / 记录'), 'the breadcrumb names the list and the record');
  const points = await main.locator('.rr-path polyline').getAttribute('points');
  must(points.split(' ').length >= 5, 'the route has the sampled positions: ' + points.slice(0, 120));
  await shots('record');
  await page.reload();
  await page.locator('#auth-gate').waitFor({state: 'hidden'});
  await main.locator('.rr-path polyline').waitFor({timeout: 30000});
  must(await page.evaluate(() => location.hash) === '#record', 'a reload stays on the record');
  await main.locator('.tw-back a').click();
  await page.waitForFunction(() => location.hash === '#simulation');
  await main.locator('[data-rb-sim] tr.rb-row').first().waitFor();

  // 4 every request is on 部署与运行; 验证与测试 keeps its actions
  await open('deployment');
  const log = main.locator('section.panel').last();
  await expectText(log, ['运行技能', '安装技能包', '机器人：', '已完成']);
  must(!/robot-[0-9a-f]{6}/.test(await log.innerText()), 'no internal id is shown in the list');
  must(await log.locator('tr.rb-row').count() === 2, 'the install and the run are listed');
  must((await log.locator('tr.rb-row').first().innerText()).includes('运行技能'), 'the newest record is first');
  await shots('deployment');
  await log.locator('tr.rb-row').last().click();                       // the whole row opens the record
  await page.waitForFunction(() => location.hash === '#record');
  await expectText(main, ['安装技能包 · patrol_places']);
  must(await page.locator('#navigation a[href="#deployment"].active').count() === 1, 'opened from 部署与运行');
  await main.locator('.tw-back a').click();
  await page.waitForFunction(() => location.hash === '#deployment');
  await open('harness');
  await main.locator('[data-sim-explain="only"]').waitFor();
  await main.locator('.page-head [data-action="harness-pick"]').click();
  await page.locator('#dialog[open]').waitFor();
  await page.locator('#dialog [data-action="close"]').first().click();
  await shots('harness');

  must(!errors.length, 'page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(`run boards browser (${engine.name()}): passed`);
})().catch(async error => { await failed(); console.error(error); process.exit(1); });
