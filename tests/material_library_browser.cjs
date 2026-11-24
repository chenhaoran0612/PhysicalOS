// Issue #33 browser acceptance: the 资料库 page. Signed out of the cloud it says so; signed in, a Word, an
// Excel, a PDF, a text, a URDF file and a driver package are uploaded with their labels and end up in
// 「解析中」; another type is turned away with the list of what is supported; an upload whose connection
// breaks continues with only the missing chunks; the same material is not taken twice; a material can be
// deleted; file names are shown as text.
// Fixture: tests/material_library_browser_fixture.py (a real cloud app on a temp directory, made-up files).
// Usage: node tests/material_library_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const LABELS = {vendor: '示例厂家', model: 'X1', version: '1.0'};
const MARKUP_NAME = '<img src=x onerror="window.__markupRan=1">说明.txt';
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-material-library-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `material-library-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const sha = buffer => crypto.createHash('sha256').update(buffer).digest('hex');
  const cloudMaterials = async () => (await (await page.request.get(cfg.url + '/fixture/materials')).json()).materials;
  const rows = () => page.locator('#material-list tbody tr');
  const queueRow = name => page.locator('.material-upload', {hasText: name});
  const result = {browser: engine.name()};

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '资料库验收', email: 'library-ui@example.test', password: 'desk-' + crypto.randomBytes(8).toString('hex') + '-7'});

  // 1 · The page is in the navigation; without a cloud login it says where to sign in.
  await page.locator('#navigation a[href="#library"]').click();
  await main.locator('h1', {hasText: '资料库'}).waitFor();
  must((await main.innerText()).includes('请先登录云平台账户'), 'Signed out of the cloud, the page does not say to sign in');
  must(await main.locator('[data-library-action="upload"]').count() === 0, 'An upload button is offered without a cloud login');
  await shot('1-signed-out');

  // 2 · Signed in: an empty library, and the supported types with their limits.
  await page.evaluate(async c => {
    await api('cloud/configure', {base_url: c.cloud});
    await api('cloud/login', {email: c.email, password: c.password});
    await refresh(true);
  }, cfg);
  await main.locator('.empty', {hasText: '资料库还是空的'}).waitFor();
  const note = await main.locator('.material-supported').innerText();
  for (const word of ['Word', 'Excel', 'PDF', 'URDF', '.tar.gz', '.deb', '驱动安装包']) must(note.includes(word), `The supported types do not mention ${word}`);
  await shot('2-empty');

  // 3 · A type that is not supported is turned away in the form, with what is supported; nothing is sent.
  const openForm = async () => {
    await main.locator('.page-head [data-library-action="upload"]').click();
    await dialog.locator('form[data-library-form="upload"]').waitFor();
  };
  const fillLabels = async (labels = LABELS) => {
    await dialog.locator('select[name="body_type"]').selectOption('quadruped');
    for (const [name, value] of Object.entries(labels)) await dialog.locator(`[name="${name}"]`).fill(value);
  };
  const pdf = Buffer.from('%PDF-1.4\n1 0 obj\n<< /Title (example) >>\nendobj\ntrailer\n<< >>\n%%EOF\n');
  let chunkRequests = 0, startRequests = 0;
  page.on('request', r => {
    if (r.url().includes('/api/library/chunk')) chunkRequests += 1;
    if (r.url().endsWith('/api/library/upload/start')) startRequests += 1;
  });
  await openForm();
  await dialog.locator('input[type=file]').setInputFiles([
    {name: 'setup.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('MZ' + 'x'.repeat(100))},
    {name: '规格书.pdf', mimeType: 'application/pdf', buffer: pdf}]);
  await fillLabels();
  await dialog.locator('button[type=submit]').click();
  const refusal = await dialog.locator('.form-error').innerText();
  must(refusal.includes('setup.exe') && refusal.includes('.pdf') && refusal.includes('.tar.gz'), 'The refusal does not name the file and what is supported: ' + refusal);
  must(startRequests === 0 && chunkRequests === 0, 'Something was sent although a chosen file is not supported');
  must(await page.locator('.material-upload').count() === 0, 'A refused selection was queued');
  await shot('3-refused');
  result.unsupportedRefused = true;

  // 4 · Word, Excel, PDF, text, URDF and a driver package: all accepted, all end in 「解析中」.
  const text = Buffer.from('# 安装说明\n\n先接好电源，再连接网线。\n');
  const urdf = Buffer.from('<?xml version="1.0"?>\n<robot name="demo"><link name="base_link"/></robot>\n');
  const six = [
    cfg.samples['操作手册.docx'].path, cfg.samples['关节参数表.xlsx'].path, cfg.samples['driver-1.0.tar.gz'].path,
  ];
  const inline = [{name: '规格书.pdf', mimeType: 'application/pdf', buffer: pdf},
                  {name: '安装说明.md', mimeType: 'text/markdown', buffer: text},
                  {name: 'robot.urdf', mimeType: 'application/xml', buffer: urdf}];
  const expected = {'操作手册.docx': cfg.samples['操作手册.docx'].sha256, '关节参数表.xlsx': cfg.samples['关节参数表.xlsx'].sha256,
                    'driver-1.0.tar.gz': cfg.samples['driver-1.0.tar.gz'].sha256, '规格书.pdf': sha(pdf), '安装说明.md': sha(text),
                    'robot.urdf': sha(urdf)};
  await dialog.locator('input[type=file]').setInputFiles(six);
  await dialog.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  await openForm();                                      // the second half, with the labels remembered
  must(await dialog.locator('[name="vendor"]').inputValue() === LABELS.vendor, 'The form does not remember the last labels');
  await dialog.locator('input[type=file]').setInputFiles(inline);
  await dialog.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  await page.waitForFunction(n => document.querySelectorAll('#material-list tbody tr').length === n, 6, {timeout: 60000});
  await page.waitForFunction(() => { const states = [...document.querySelectorAll('.material-upload-state')];
    return states.length === 6 && states.every(e => e.textContent.includes('解析中')); });
  const listed = await main.locator('#material-list').innerText();
  for (const name of Object.keys(expected)) must(listed.includes(name), `${name} is not listed`);
  must((await rows().locator('.status', {hasText: '解析中'}).count()) === 6, 'Not every material is shown as 解析中');
  must(listed.includes('四足') && listed.includes('示例厂家 / X1'), 'The labels are not shown in the list');
  let stored = await cloudMaterials();
  must(stored.length === 6, `The cloud holds ${stored.length} materials, not 6`);
  for (const m of stored) {
    must(expected[m.filename] === m.sha256, `${m.filename} arrived with another SHA-256`);
    must(m.status === 'parsing' && m.body_type === 'quadruped' && m.vendor === LABELS.vendor && m.version === LABELS.version, `${m.filename} has wrong labels or state`);
  }
  must(stored.find(m => m.filename === 'driver-1.0.tar.gz').kind === 'package', 'The driver package is not stored as a package');
  await shot('4-six-uploaded');
  result.sixAccepted = true;

  // 5 · The connection breaks after three chunks. The upload is shown as interrupted, and "继续" sends
  //     only what is missing.
  const big = Buffer.from('关节限位说明，第 N 行，力矩 12.5 N·m\n'.repeat(Math.ceil(cfg.chunk_bytes * 7.5 / 48)));
  const chunks = Math.ceil(big.length / cfg.chunk_bytes);
  must(chunks >= 6 && big.length < cfg.document_limit, 'The large sample does not span enough chunks');
  let passed = 0;
  await page.route('**/api/library/chunk*', route => (passed++ < 3 ? route.continue() : route.abort('connectionreset')));
  await openForm();
  await dialog.locator('input[type=file]').setInputFiles([{name: '限位说明-完整版.txt', mimeType: 'text/plain', buffer: big}]);
  await dialog.locator('[name="version"]').fill('2.0');
  await dialog.locator('button[type=submit]').click();
  const interrupted = queueRow('限位说明-完整版.txt');
  await interrupted.locator('[data-library-action="resume"]').waitFor({timeout: 30000});
  must((await interrupted.innerText()).includes('上传中断'), 'A broken connection is not shown as an interruption');
  must((await interrupted.locator('[role=progressbar]').getAttribute('aria-valuenow')) !== '0', 'The progress of the chunks that arrived is lost');
  must((await cloudMaterials()).length === 6, 'An interrupted upload is already listed as a material');
  await shot('5-interrupted');
  await page.unroute('**/api/library/chunk*');
  chunkRequests = 0;
  await interrupted.locator('[data-library-action="resume"]').click();
  await page.waitForFunction(n => document.querySelectorAll('#material-list tbody tr').length === n, 7, {timeout: 60000});
  must(chunkRequests === chunks - 3, `Continuing sent ${chunkRequests} chunks; ${chunks - 3} were missing`);
  stored = await cloudMaterials();
  const whole = stored.find(m => m.filename === '限位说明-完整版.txt');
  must(whole && whole.sha256 === sha(big) && whole.size === big.length, 'The continued upload did not arrive whole');
  await shot('6-resumed');
  result.resumed = {chunks, sentAfterResume: chunkRequests};

  // 6 · The same material in the same version is not taken twice; a name with markup stays text.
  await openForm();
  await dialog.locator('input[type=file]').setInputFiles([{name: '规格书.pdf', mimeType: 'application/pdf', buffer: pdf},
                                                         {name: MARKUP_NAME, mimeType: 'text/plain', buffer: Buffer.from('普通文字\n')}]);
  await dialog.locator('[name="version"]').fill('1.0');
  await dialog.locator('button[type=submit]').click();
  await queueRow('规格书.pdf').locator('.material-upload-state', {hasText: '已经有这份资料'}).last().waitFor({timeout: 30000});
  await page.waitForFunction(n => document.querySelectorAll('#material-list tbody tr').length === n, 8, {timeout: 60000});
  must(await page.locator('img[src="x"]').count() === 0, 'Markup in a file name became part of the page');
  must(await page.evaluate(() => window.__markupRan) === undefined, 'Markup in a file name ran');
  must((await main.innerText()).includes('_img src=x onerror=_window.__markupRan=1__说明.txt'), 'The cleaned name is not what is listed');
  must((await cloudMaterials()).length === 8, 'A duplicate was stored, or the text file was not');
  await shot('7-duplicate');
  await main.locator('[data-library-action="clear-done"]').click();
  must(await page.locator('.material-upload').count() === 0, 'Finished uploads stay in the list after clearing them');
  await shot('7-list');
  result.duplicateRefused = true;

  // 7 · Filters, then deleting one material.
  await page.locator('[data-library-filter="q"]').fill('driver');
  await page.locator('[data-library-filter="q"]').press('Enter');
  await page.waitForFunction(() => document.querySelectorAll('#material-list tbody tr').length === 1);
  must((await rows().first().innerText()).includes('driver-1.0.tar.gz'), 'The search does not find the driver package');
  await page.locator('[data-library-filter="q"]').fill('');
  await page.locator('[data-library-filter="q"]').press('Enter');
  await page.waitForFunction(() => document.querySelectorAll('#material-list tbody tr').length === 8);
  const target = stored.find(m => m.filename === 'robot.urdf');
  await page.locator(`tr[data-library-material="${target.id}"] [data-library-action="delete"]`).click();
  must((await dialog.innerText()).includes('robot.urdf') && (await dialog.innerText()).includes('不能恢复'), 'The confirmation does not say what is deleted');
  await shot('8-delete-confirm');
  await dialog.locator('[data-library-action="delete-confirm"]').click();
  await page.waitForFunction(id => !document.querySelector(`tr[data-library-material="${id}"]`), target.id);
  must(!(await cloudMaterials()).some(m => m.id === target.id), 'The deleted material is still in the cloud');
  result.deleted = true;

  // 8 · Keyboard: the upload button is reachable and opens the form; Escape closes it.
  await main.locator('.page-head [data-library-action="upload"]').focus();
  await page.keyboard.press('Enter');
  await dialog.locator('form[data-library-form="upload"]').waitFor();
  await page.keyboard.press('Escape');
  await dialog.waitFor({state: 'hidden'});

  // 9 · Narrow windows: nothing spills out of the page.
  result.widths = {};
  for (const width of [1440, 1024, 768, 375, 320]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForTimeout(150);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    must(overflow <= 1, `At ${width}px the page is ${overflow}px wider than the window`);
    await shot(`9-width-${width}`);
    result.widths[width] = 'ok';
  }

  must(errors.length === 0, 'Page errors: ' + errors.join(' | '));
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
