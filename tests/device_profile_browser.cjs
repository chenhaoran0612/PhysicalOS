// Browser acceptance for issue #27: the device profile of a real machine shows its system, hardware,
// ROS 2 (distribution, nodes, topics with message types, actions, services) and resource usage; a
// distribution other than Humble is marked unsupported; a device without ROS 2 says so without failing.
//   node tests/device_profile_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/device_profile_browser_fixture.py (a local SSH protocol fixture, no real machine; the
// server needs NEUXMIND_MAIL_DEV_ECHO=1). BRAIN_BROWSER=webkit runs it on WebKit, the engine of the
// desktop app (WKWebView); default Chromium.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const READ_API = '/api/engineering/profile/read';
// What `ros2 topic list` prints on the fixture's robot (tests/ros2_fixture.py TOPIC_LIST).
const TOPIC_LIST = ['/camera/color/camera_info', '/camera/color/image_raw', '/cmd_vel', '/diagnostics', '/imu/data', '/joint_states',
                    '/odom', '/parameter_events', '/rosout', '/scan', '/sensors/lidar/points', '/status/text', '/tf', '/tf_static'];

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-device-profile-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const reads = [];                                   // every profile read the page asks for
  page.on('request', r => { if (r.url().endsWith(READ_API)) reads.push(r.postDataJSON()); });

  const dialog = page.locator('#dialog'), profile = page.locator('#device-profile');
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
    for (const t of texts) check(text.includes(t), `Missing "${t}" in: ${text.slice(0, 800)}`);
  };
  const robotAnswers = async state => {
    const response = await fetch(`${cfg.control}/ros2/${state}`, {method: 'POST'});
    check(response.status === 204, `the fixture does not know the state ${state}`);
  };
  // "重新读取", then the answer is on the page.
  const readAgain = async () => {
    const before = reads.length;
    await profile.locator('[data-action="engine-profile-read"]').click();
    await profile.locator('[data-action="engine-profile-read"]:not([disabled])').waitFor({timeout: 30000});
    check(reads.length === before + 1, `one read per click, saw ${reads.length - before}`);
  };
  const names = () => profile.locator('#dp-list tbody .dp-name').allTextContents();
  const chip = label => profile.locator('.dp-list-bar .ls-chip', {hasText: label});
  const verdict = profile.locator('.dp-verdict');
  const fits = async what => {
    const overflow = await page.evaluate(() => {
      const d = document.querySelector('#dialog');
      return {page: document.documentElement.scrollWidth - window.innerWidth, dialog: d.scrollWidth - d.clientWidth};
    });
    check(overflow.page <= 0 && overflow.dialog <= 0, `${what}: horizontal overflow ${JSON.stringify(overflow)}`);
  };

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run (a letter and a digit are required)
  await gateSignIn(page, {name: '设备档案验收', email: `profile-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'prof1-' + crypto.randomBytes(8).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await fill('name', '巡检项目'); await fill('objective', '查看真机的设备档案');
  await submit();

  await page.goto(cfg.url + '/#robots');
  await page.locator('[data-action="engine-ssh"]').first().click();
  await fill('name', '巡检机器人'); await fill('host', '127.0.0.1'); await fill('port', cfg.target_port);
  await page.locator('#dialog [name=family]').selectOption('quadruped'); await require('./robot_model_helper.cjs').fillBrandAndModel(page);   // issue #32: a new device needs a type from the list
  await fill('username', cfg.target_user); await fill('password', cfg.target_password);
  await submit();
  await page.locator('#dialog input[type=checkbox]').check();                       // the host fingerprint
  await submit();
  await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 30000});      // issue #2: the scan after connecting
  await page.locator('#dialog [data-action="close"]').first().click();

  // The card of a real robot offers the profile; the first opening reads it without being asked.
  const card = page.locator('article.card', {hasText: '巡检机器人'});
  await card.locator('[data-action="engine-profile"]').click();
  await verdict.waitFor({timeout: 30000});
  check(reads.length === 1, `the first opening reads once, saw ${reads.length}`);
  await expectText(verdict, ['ROS 2 Humble', '支持']);
  check(!(await verdict.innerText()).includes('不支持'), 'Humble is the supported distribution');
  await expectText(profile, ['dog-fixture', 'NVIDIA Orin NX Developer Kit', 'aarch64', '42', 'rmw_cyclonedds_cpp', '正在运行的 ROS 进程']);

  // The acceptance: the topic list is what `ros2 topic list` prints on the device, each with its message type.
  check(JSON.stringify(await names()) === JSON.stringify(TOPIC_LIST), `topics: ${JSON.stringify(await names())}`);
  await expectText(profile.locator('#dp-list tr', {hasText: '/cmd_vel'}), ['geometry_msgs/msg/Twist']);
  await expectText(profile.locator('#dp-list tr', {hasText: '/status/text'}), ['std_msgs/msg/String', 'std_msgs/msg/Header', '多种类型']);
  await expectText(chip('话题'), ['14']); await expectText(chip('节点'), ['6']);
  await expectText(chip('动作'), ['2']); await expectText(chip('服务'), ['7']);
  await page.screenshot({path: `${shots}/device-profile-ros2-1440.png`});

  await chip('动作').click();
  check(JSON.stringify(await names()) === JSON.stringify(['/dock', '/navigate_to_pose']), `actions: ${JSON.stringify(await names())}`);
  await expectText(profile.locator('#dp-list'), ['nav2_msgs/action/NavigateToPose', 'robot_interfaces/action/Dock']);
  await chip('节点').click();
  check((await names()).includes('/chassis_bridge') && (await names()).length === 6, 'the six nodes');
  await chip('服务').click();
  await expectText(profile.locator('#dp-list tr', {hasText: '/chassis_bridge/set_mode'}), ['robot_interfaces/srv/SetMode']);

  // The search narrows the list while the field keeps its focus.
  await chip('话题').click();
  await profile.locator('#dp-search').click();
  await page.keyboard.type('lidar');
  check(JSON.stringify(await names()) === JSON.stringify(['/sensors/lidar/points']), `search by name: ${JSON.stringify(await names())}`);
  check(await page.evaluate(() => document.activeElement.id === 'dp-search'), 'the search keeps the focus while typing');
  await profile.locator('#dp-search').fill('LaserScan');
  check(JSON.stringify(await names()) === JSON.stringify(['/scan']), 'search by message type');
  await profile.locator('#dp-search').fill('no-such-topic');
  await expectText(profile.locator('#dp-list'), ['没有名称或类型包含']);
  await profile.locator('#dp-search').fill('');

  // Hardware and resources come from the same read.
  await profile.locator('#dp-tab-hardware').click();
  await expectText(profile, ['已识别硬件', '未加载驱动', 'Intel Corporation Wi-Fi 6 AX200', '去硬件初始化处理这些问题']);
  await page.screenshot({path: `${shots}/device-profile-hardware-1440.png`});
  await profile.locator('#dp-tab-resources').click();
  await expectText(profile, ['75%', '25%', '61.3 °C', 'component_container', '共 15 个进程']);
  check(!(await profile.innerText()).includes('--api-key'), 'command lines are never shown');
  await page.screenshot({path: `${shots}/device-profile-resources-1440.png`});

  // Opening it again shows the profile that was kept: no machine is read.
  await page.locator('#dialog [data-action="close"]').first().click();
  await card.locator('[data-action="engine-profile"]').click();
  await verdict.waitFor();
  await page.waitForTimeout(500);
  check(reads.length === 1, `reopening reads nothing, saw ${reads.length} reads`);
  check((await names()).length === TOPIC_LIST.length, 'the kept topic list is shown');

  // A ROS 2 read that does not finish: the earlier lists stay, with their time and the reason.
  await robotAnswers('stuck');
  await readAgain();
  await expectText(profile, ['ROS 2 的读取没有完成', '读到的结果']);
  check((await names()).length === TOPIC_LIST.length, 'the earlier topic list is kept');
  await expectText(verdict, ['ROS 2 Humble']);

  // The tool fails on the device: the lists are unknown, not empty, and what it said is quoted.
  await robotAnswers('failed');
  await readAgain();
  await expectText(profile, ['没有读全', '退出码 1', 'rmw_cyclonedds_cpp', '没有读到不等于没有']);
  await expectText(chip('话题'), ['没读到']);
  await page.screenshot({path: `${shots}/device-profile-failed-1440.png`});

  // Another distribution is marked unsupported; what could be read is still listed.
  await robotAnswers('foxy');
  await readAgain();
  await expectText(verdict, ['ROS 2 Foxy', '不支持', '只支持 Humble']);
  await expectText(profile.locator('#dp-tab-ros2'), ['不支持']);
  check((await names()).length === TOPIC_LIST.length, 'the lists of an unsupported distribution are still shown');
  await page.screenshot({path: `${shots}/device-profile-unsupported-1440.png`});

  // No ROS on the device: said plainly, not as a failure; the rest of the profile is there.
  await robotAnswers('absent');
  await readAgain();
  await expectText(verdict, ['没有读到 ROS 2', '这不算故障']);
  check(await profile.locator('.dp-list-bar').count() === 0, 'no lists are offered without ROS 2');
  check(await profile.locator('.notice.warn').count() === 0, 'a device without ROS 2 shows no warning');
  await expectText(profile, ['dog-fixture', 'NVIDIA Orin NX Developer Kit']);
  await page.screenshot({path: `${shots}/device-profile-absent-1440.png`});

  // Narrow windows: nothing spills sideways.
  await robotAnswers('humble');
  await readAgain();
  for (const [width, height] of [[1024, 900], [900, 900], [768, 900], [700, 900], [375, 800], [320, 700]]) {
    await page.setViewportSize({width, height});
    await page.waitForTimeout(200);
    for (const tab of ['ros2', 'hardware', 'resources']) {
      await profile.locator(`#dp-tab-${tab}`).click();
      await fits(`${width}px, ${tab}`);
    }
    await profile.locator('#dp-tab-ros2').click();
    await page.screenshot({path: `${shots}/device-profile-ros2-${width}.png`});
  }

  // The keyboard reaches the tabs and the lists; the focus stays where it was after a redraw.
  await page.setViewportSize({width: 1440, height: 1000});
  await profile.locator('#dp-tab-hardware').focus();
  await page.keyboard.press('Enter');
  check(await page.evaluate(() => document.activeElement.id === 'dp-tab-hardware'), 'the focus stays on the tab that was chosen');
  check(await profile.locator('#dp-tab-hardware').getAttribute('aria-selected') === 'true', 'the tab is selected by keyboard');

  check(!errors.length, 'page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(`device profile browser acceptance passed (${engine.name()})`);
})().catch(error => { console.error(error); process.exit(1); });
