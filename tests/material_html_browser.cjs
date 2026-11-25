// Issue #45 browser acceptance: an HTML page is a document of the 资料库. The page names .html among the
// supported types, the file chooser offers it, the upload is stored byte for byte, the knowledge service
// receives the page's words (not its markup, not its script), the answers fill the items, and nothing of the
// uploaded page ever runs in the console.
// Fixture: tests/material_html_browser_fixture.py (a real cloud app on a temp directory, a stand-in for the
// knowledge service; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// Usage: node tests/material_html_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const NAME = '安装说明.html';
const PAGE = Buffer.from(`<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>示例驱动安装说明</title>
<script>window.__pageRan = 1; var hiddenScriptText = 1;</script><style>body { color: red }</style></head>
<body><h1>demo_page_driver 3.2.0</h1>
<p>适用于 Ubuntu 22.04 和 ROS 2 humble。<a href="https://example.test/demo_page_driver">下载</a></p>
<pre>sudo apt install ros-humble-demo-page-driver</pre>
<img src="x" onerror="window.__pageRan = 2">
</body></html>
`);

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-material-html-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `material-html-${engine.name()}-${name}.png`)});
  const must = (condition, message) => { if (!condition) throw new Error(message); };
  const main = page.locator('#main'), dialog = page.locator('#dialog');
  const fixture = async what => (await page.request.get(cfg.url + '/fixture/' + what)).json();
  const row = page.locator('#material-list tbody tr', {hasText: NAME});

  try {
    await page.goto(cfg.url);
    // a throwaway account on the local fixture; the password is made up on every run
    await gateSignIn(page, {name: '网页资料验收', email: 'html-ui@example.test', password: 'page-' + crypto.randomBytes(8).toString('hex') + '-7'});
    await page.locator('#navigation a[href="#library"]').click();
    await main.locator('h1', {hasText: '资料库'}).waitFor();
    await page.evaluate(async c => {
      await api('cloud/configure', {base_url: c.cloud});
      await api('cloud/login', {email: c.email, password: c.password});
      await refresh(true);
    }, cfg);

    // 1 · The page says that web pages are taken.
    await main.locator('.empty', {hasText: '资料库还是空的'}).waitFor();
    const note = await main.locator('.material-supported').innerText();
    must(note.includes('.html') && note.includes('.htm') && note.includes('网页'), 'The supported types do not mention web pages: ' + note);

    // 2 · The file chooser offers it, and the form takes it without a refusal.
    await main.locator('.page-head [data-library-action="upload"]').click();
    await dialog.locator('form[data-library-form="upload"]').waitFor();
    const accept = await dialog.locator('input[type=file]').getAttribute('accept');
    must(accept.split(',').includes('.html') && accept.split(',').includes('.htm'), 'The file chooser does not offer .html: ' + accept);
    await dialog.locator('select[name="body_type"]').selectOption('quadruped');
    for (const [name, value] of Object.entries({vendor: '示例厂家', model: 'X1', version: '1.0'})) await dialog.locator(`[name="${name}"]`).fill(value);
    await dialog.locator('input[type=file]').setInputFiles([{name: NAME, mimeType: 'text/html', buffer: PAGE}]);
    await dialog.locator('button[type=submit]').click();
    await dialog.waitFor({state: 'hidden'});
    await row.waitFor({timeout: 60000});
    await shot('1-uploaded');

    // 3 · The cloud holds the page byte for byte, as a document.
    let stored = (await fixture('materials')).materials;
    must(stored.length === 1 && stored[0].filename === NAME, 'The cloud does not hold the page: ' + JSON.stringify(stored));
    must(stored[0].sha256 === crypto.createHash('sha256').update(PAGE).digest('hex'), 'The page arrived with another SHA-256');
    must(stored[0].format === 'html' && stored[0].kind === 'document', 'The page is not stored as an HTML document');

    // 4 · It is parsed: the knowledge service got the words of the page, and its answers fill the items.
    const until = Date.now() + 60000;
    while (Date.now() < until && stored[0].status === 'parsing') {
      await page.waitForTimeout(1000);
      stored = (await fixture('materials')).materials;
    }
    must(stored[0].status === 'parsed', 'The page was not parsed; it is ' + stored[0].status + ' ' + (stored[0].status_detail || ''));
    const sent = (await fixture('sent')).sent;
    must(sent.length === 1 && sent[0].fileName === '安装说明.md', 'The knowledge service got ' + JSON.stringify(sent.map(s => s.fileName)));
    const lines = sent[0].text.split('\n');
    must(lines.includes('# demo_page_driver 3.2.0') && lines.includes('sudo apt install ros-humble-demo-page-driver'), 'The words of the page did not arrive: ' + sent[0].text);
    must(sent[0].text.includes('下载 (https://example.test/demo_page_driver)'), 'The link target did not arrive');
    for (const unwanted of ['<', 'hiddenScriptText', '__pageRan', 'color: red']) must(!sent[0].text.includes(unwanted), `"${unwanted}" was sent to the knowledge service`);

    await page.evaluate(() => materialLibraryLoad());
    await row.locator('.status', {hasText: '待确认'}).waitFor({timeout: 15000});
    await row.locator('[data-parse-action="open"]').click();
    await dialog.locator('form[data-parse-form="result"]').waitFor({timeout: 15000});
    const values = await dialog.locator('input, textarea').evaluateAll(list => list.map(e => e.value).join('\n'));
    must(values.includes('demo_page_driver') && values.includes('3.2.0') && values.includes('sudo apt install ros-humble-demo-page-driver'),
      'The answers are not in the result: ' + values);
    await shot('2-result');

    // 5 · Nothing of the uploaded page became part of the console or ran in it.
    must(await page.evaluate(() => window.__pageRan) === undefined, 'A script of the uploaded page ran in the console');
    must(await page.locator('img[src="x"]').count() === 0, 'Markup of the uploaded page became part of the console');
    must(errors.length === 0, 'page errors: ' + errors.join(' | '));
    console.log(JSON.stringify({browser: engine.name(), ok: true}));
  } catch (err) {
    await shot('failure').catch(() => {});
    console.error('page errors:', errors);
    throw err;
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exit(1); });
