// Browser acceptance for issue #8: the status page of a real machine lists every piece of hardware
// with its driver and working state and the software that is running, and reads once a minute.
//   node tests/live_status_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ssh_jump_browser_fixture.py (local SSH protocol fixtures, no real machine; the server
// needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop
// app (WKWebView); default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const STATUS_API = '/api/engineering/ssh/status';

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ssh-jump-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  // The page's clock is ours: a minute can pass without the test waiting for one. It runs at normal speed otherwise.
  await page.clock.install();
  const reads = [];                                   // every status read the page asks for
  page.on('request', r => { if (r.url().endsWith(STATUS_API)) reads.push(r.postDataJSON()); });

  const dialog = page.locator('#dialog'), status = page.locator('#ssh-status');
  const fill = async (n, v) => page.locator(`#dialog [name="${n}"]`).fill(String(v));
  const submit = async () => {
    await page.locator('#dialog button[type=submit]').click();
    await page.waitForTimeout(150);
    const e = await page.locator('.form-error').allTextContents();
    if (e.some(Boolean)) throw Error(e.join(';'));
  };
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const expectText = async (locator, texts) => {
    const text = await locator.innerText();
    for (const t of texts) check(text.includes(t), `Missing "${t}" in: ${text.slice(0, 600)}`);
  };
  const confirmFingerprint = async title => {
    await dialog.getByText(title, {exact: true}).waitFor();
    await page.locator('#dialog input[type=checkbox]').check();
    await submit();
  };
  const readsAfter = async (count, what) => {
    for (let waited = 0; reads.length < count; waited += 100) {
      check(waited < 15000, `${what}: expected ${count} status reads, saw ${reads.length}`);
      await page.waitForTimeout(100);
    }
    await page.waitForTimeout(300);                    // the answer is rendered
  };
  const rowOf = text => status.locator('tbody tr', {hasText: text}).first();
  const stamp = () => status.locator('.ls-updated').innerText();

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '真机状态验收', email: `status-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'live1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '内网机器人'); await fill('objective', '查看真机的硬件与软件状态');
  await submit();

  // A robot behind a jump host: the status has to work through the same route.
  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="engine-ssh"]').first().click();
  await fill('name', '巡检机器人'); await fill('host', '127.0.0.1'); await fill('port', cfg.target_port);
  await fill('username', cfg.target_user); await fill('password', cfg.target_password);
  await page.locator('#dialog [name=route]').selectOption('jump');
  await fill('jump_host', '127.0.0.1'); await fill('jump_port', cfg.jump_port);
  await fill('jump_username', cfg.jump_user); await fill('jump_password', cfg.jump_password);
  await page.locator('#dialog details summary').click();
  await fill('watch_services', cfg.services.join('\n'));
  await submit();
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});      // issue #2: the scan after connecting
  await page.locator('#dialog [data-action="close"]').first().click();

  // 1. Opening the status: the basic figures first, then hardware and software. It says how often it reads.
  await page.goto(cfg.url + '/#robots');
  await page.locator('article.card', {hasText: '巡检机器人'}).locator('[data-action="engine-status"]').click();
  await expectText(dialog.locator('.dialog-head'), ['每分钟', '只读', '不会让机器人运动']);
  check(!(await dialog.locator('.dialog-head').innerText()).includes('3 秒'), 'The page still says it reads every 3 seconds');
  await status.locator('.ls-tiles').waitFor({timeout: 30000});
  check(reads.length === 2 && reads[0].quick === true && reads[1].quick === false,
        'Expected a quick read, then a full one: ' + JSON.stringify(reads));
  await expectText(status, ['在线', '真机 · 只读', '经跳板机', `${cfg.jump_user}@127.0.0.1:${cfg.jump_port}`, '上次更新', '立即刷新',
                            'dog-fixture', '6 分钟', '0.52 / 0.61 / 0.40', '61.3 °C', '75%', 'NVIDIA Orin NX Developer Kit', 'Ubuntu 20.04.6 LTS']);
  check(/\d+ 秒后自动刷新/.test(await status.locator('#ls-next').innerText()), 'No countdown to the next read');

  // 2. Hardware: every device, whether its driver is loaded, whether it works and why not.
  const total = Number(await status.locator('.ls-tiles b').first().innerText());
  check(total === 17 && await status.locator('tbody tr').count() === total, 'The table does not list every device');
  await expectText(status.locator('.ls-tiles'), ['已识别硬件', '工作正常', '需要处理', '未加载驱动', '未确认']);
  await expectText(rowOf('Wi-Fi 6 AX200'), ['网络', '未加载', '异常', '未加载驱动']);
  await expectText(rowOf('eth0'), ['已加载', 'r8168', '正常']);
  await expectText(rowOf('ttyUSB0'), ['已加载', 'ch341-uart', '需处理', '无权读写串口']);
  await expectText(rowOf('can0'), ['CAN 总线', '需处理', '未启用']);
  await expectText(rowOf('Cortex-A78AE'), ['处理器与内存', '正常']);
  await expectText(status, ['不代表已经通过功能测试', '去硬件初始化处理这些问题']);
  await page.screenshot({path: `${shots}/live-status-hardware.png`});
  const chip = id => status.locator(`.ls-chip[data-id="${id}"]`);
  await chip('!attention').focus();                    // from the keyboard: a click does not focus a button in Safari
  await page.keyboard.press('Enter');
  check(await status.locator('tbody tr').count() === 5, 'The "needs handling" filter does not show the 5 devices with problems');
  check(await status.locator('tbody tr .status.good').count() === 0, 'A working device is listed under "needs handling"');
  check(await chip('!attention').getAttribute('aria-pressed') === 'true', 'The chosen filter is not marked as pressed');
  check(await page.evaluate(() => document.activeElement?.dataset.id) === '!attention', 'Keyboard focus left the filter after it was chosen');
  await chip('serial').click();
  check(await status.locator('tbody tr').count() === 2, 'The category filter does not show the 2 serial ports');
  await chip('').click();
  check(await status.locator('tbody tr').count() === total, 'Back to all devices');
  check(await status.locator('pre, code').count() === 0, 'The status shows code');

  // 3. Software: middleware processes, services, containers and the busiest processes, each with its state.
  await status.locator('#ls-tab-software').focus();
  await page.keyboard.press('Enter');                  // the tabs work from the keyboard
  check(await status.locator('#ls-tab-software').getAttribute('aria-selected') === 'true', 'The software tab is not selected');
  await expectText(status, ['机器人中间件', '系统服务', 'Docker 容器', '占用资源最高的进程']);
  await expectText(rowOf('/sensors/lidar'), ['lidar_driver · lidar_driver_node', 'ROS 2 节点', '运行中', '50.0%', '89 MB', 'robot']);
  await expectText(rowOf('nav_bringup robot.launch.py'), ['ROS 2 launch']);
  await expectText(rowOf('Micro XRCE-DDS Agent'), ['中间件代理', 'MicroXRCEAgent']);
  await expectText(rowOf('nav-stack.service'), ['关注', '失败']);
  await expectText(rowOf('recorder'), ['关注', '未运行']);        // watched, not running: listed all the same
  await expectText(rowOf('bridge.service'), ['关注', '运行中']);
  await expectText(rowOf('map-upload.service'), ['反复重启']);
  await expectText(rowOf('robot-idle-start.service'), ['失败']);
  const services = status.locator('[data-block="services"] tbody tr');
  check((await services.first().innerText()).includes('nav-stack.service'), 'A watched, failed service is not listed first');
  check(await status.locator('[data-block="services"] .ls-watched').count() === 3, 'Not all three watched services are marked');
  await expectText(rowOf('nav-bridge'), ['registry.example.com/robot/nav-bridge:1.4', '运行中', 'Up 3 hours (healthy)']);
  await expectText(rowOf('perception'), ['不健康']);
  await expectText(rowOf('map-server'), ['异常退出']);
  await expectText(rowOf('calibration'), ['已退出']);
  await expectText(rowOf('python3 · uploader.py'), ['运行中', '20.0%', '244 MB']);
  await expectText(status, ['共 15 个进程', '只显示程序名，不显示命令行']);
  const softwareText = await status.innerText();
  for (const leak of ['--api-key', 'launch_params', '--ros-args', '--containerd']) {
    check(!softwareText.includes(leak), `A command line reached the page: ${leak}`);
  }
  await expectText(status.locator('.ls-tabs'), ['项需处理', '项异常']);       // both tabs say that something is wrong
  await page.screenshot({path: `${shots}/live-status-software.png`});

  // 4. Once a minute: nothing is read in between, a minute later it is, on the tab that was open.
  const before = await stamp();
  await page.waitForTimeout(4500);                     // the earlier page read every 3 seconds
  check(reads.length === 2, `The page read again within a few seconds (${reads.length} reads)`);
  await dialog.evaluate(d => { d.scrollTop = 320; });
  await page.clock.fastForward(61000);
  await readsAfter(3, 'a minute later');
  check(reads[2].quick === false, 'The minute read is not a full one');
  check(await stamp() !== before, 'The time of the last update did not change');
  check(await status.locator('#ls-tab-software').getAttribute('aria-selected') === 'true', 'The refresh switched tabs');
  check(Math.abs(await dialog.evaluate(d => d.scrollTop) - 320) < 4, 'The refresh moved the scroll position');
  await page.waitForTimeout(2500);
  check(reads.length === 3, 'More than one read for one minute');

  // 5. Reading now, on request. A read that fails says so and keeps what was shown.
  await status.locator('[data-action="engine-status-refresh"]').click();
  await readsAfter(4, 'manual refresh');
  await page.route('**' + STATUS_API, route => route.abort());
  await status.locator('[data-action="engine-status-refresh"]').click();
  await status.getByText('这一次没有读到状态', {exact: false}).waitFor();
  await expectText(status, ['dog-fixture', '系统服务', 'nav-stack.service']);
  await page.unroute('**' + STATUS_API);
  await status.locator('[data-action="engine-status-refresh"]').click();
  await status.getByText('这一次没有读到状态', {exact: false}).waitFor({state: 'detached'});
  // Software that has never been read (its command timed out) is said to be unread, not "this machine has no systemd".
  const unread = '设备在限定时间内没有返回软件状态';
  await page.route('**' + STATUS_API, async route => {
    const response = await route.fetch();
    const view = await response.json();
    view.software = {checked_at: null, duration_ms: null, complete: false, problem: unread, services: null, containers: null,
                     middleware: null, top: null, process_count: null, cores: null, notes: []};
    await route.fulfill({response, json: view});
  });
  await status.locator('[data-action="engine-status-refresh"]').click();
  await status.getByText(unread, {exact: false}).waitFor();
  check(await status.locator('[data-block]').count() === 0 && !(await status.locator('#ls-panel').innerText()).includes('systemd'),
        'Unread software is described as something else: ' + await status.locator('#ls-panel').innerText());
  await page.unroute('**' + STATUS_API);
  await status.locator('[data-action="engine-status-refresh"]').click();
  await rowOf('nav-stack.service').waitFor();

  // 6. The watched services are changed from here, and the page comes back to the same tab.
  await status.locator('[data-action="engine-status-services"]').click();
  await fill('watch_services', 'bridge');
  await submit();
  await status.locator('#ls-tab-software[aria-selected="true"]').waitFor({timeout: 15000});
  await rowOf('bridge.service').waitFor({timeout: 15000});
  await expectText(rowOf('bridge.service'), ['关注', '运行中']);
  check(await status.locator('[data-block="services"] .ls-watched').count() === 1, 'The watch list on the page did not change');
  check(await services.filter({hasText: 'recorder'}).count() === 0, 'A service that is no longer watched and not running is still listed');

  // 7. Layout: no sideways scrolling of the page or the dialog at desktop, tablet and phone widths.
  await status.locator('#ls-tab-hardware').click();
  for (const [width, height] of [[1440, 1000], [1024, 800], [768, 1000], [375, 800], [320, 700]]) {
    await page.setViewportSize({width, height});
    await page.waitForTimeout(150);
    const overflow = await page.evaluate(() => {
      const d = document.querySelector('#dialog'), box = d.getBoundingClientRect();
      return {page: document.documentElement.scrollWidth > window.innerWidth, dialog: d.scrollWidth > d.clientWidth + 1,
              outside: box.left < 0 || box.right > window.innerWidth + 1};
    });
    check(!overflow.page && !overflow.dialog && !overflow.outside, `Sideways overflow at ${width}px: ${JSON.stringify(overflow)}`);
    await page.screenshot({path: `${shots}/live-status-${width}.png`});
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.emulateMedia({reducedMotion: 'reduce'});
  check(await status.locator('.ls-tab').first().evaluate(el => getComputedStyle(el).transitionDuration) === '0s',
        'Tabs still animate when reduced motion is asked for');

  // 8. A closed dialog reads nothing.
  const atClose = reads.length;
  await page.locator('#dialog [data-action="close"]').first().click();
  await page.clock.fastForward(125000);
  await page.waitForTimeout(1500);
  check(reads.length === atClose, 'The status is still read after its dialog was closed');

  if (errors.length) throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, browser: engine.name(), statusReads: reads.length, allHardwareListed: total,
                              driverAndWorkingState: true, runningSoftware: true, noCommandLines: true, oncePerMinute: true,
                              manualRefresh: true, failedReadKeepsContent: true, unreadSoftwareSaidSo: true, watchedServicesEdited: true,
                              noSidewaysOverflow: true, closedDialogStopsReading: true, jsErrors: errors.length}));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
