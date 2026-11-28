// Browser acceptance for issue #72 (整体 Icon 优化): the mark is drawn, every page of the sidebar has a
// picture of its own from the one table, and no symbol is left as a character on any page.
//   node tests/icons_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ux01_browser_fixture.py (a fresh state directory; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ux01-qa.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };

  // How a mark is drawn: a masked box in the colour of the place, with no character showing.
  const mark = selector => page.locator(selector).first().evaluate(el => {
    const style = getComputedStyle(el), box = el.getBoundingClientRect();
    return {mask: style.maskImage || style.webkitMaskImage || '', fontSize: style.fontSize, color: style.backgroundColor,
            width: box.width, height: box.height, inner: [...el.children].map(c => getComputedStyle(c).display)};
  });
  const drawnMark = (m, where) => {
    check(m.mask.includes('data:image/svg+xml') && m.mask.includes('M18 45V19L36 45V29L46 19'), `${where}: the mark is not the drawn N: ${m.mask.slice(0, 80)}`);
    check(m.fontSize === '0px' && m.inner.every(d => d === 'none'), `${where}: the old characters still show: ${JSON.stringify(m)}`);
    check(Math.round(m.width) === 34 && Math.round(m.height) === 34, `${where}: the mark is not 34 × 34: ${m.width} × ${m.height}`);
  };
  // Symbols still shown as characters, anywhere on the page that is open.
  const leftAsText = () => page.evaluate(() => {
    const left = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || parent.closest(NX_SKIP) || !node.nodeValue.trim()) continue;
      if (nxGlyphParts(node.nodeValue)) left.push(node.nodeValue.trim() + ' in ' + parent.tagName + '.' + parent.className);
    }
    return left;
  });

  await page.goto(cfg.url);
  await page.locator('#auth-gate form[data-acct-form]').waitFor();
  const gate = await mark('#auth-gate .auth-logo .brand-mark');
  drawnMark(gate, 'sign-in page');
  await page.screenshot({path: path.join(shots, 'icons-sign-in.png')});
  await gateSignIn(page, {name: '图标验收', email: `icons-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'icons1-' + crypto.randomBytes(8).toString('hex')});

  const side = await mark('#sidebar .brand .brand-mark');
  drawnMark(side, 'sidebar');
  check(side.color === 'rgb(217, 240, 179)' || side.color === 'rgb(205, 235, 171)', 'the mark of the sidebar is not lime: ' + side.color);

  // The sidebar: one picture per page, each from the table, all drawn alike.
  const nav = await page.evaluate(() => [...document.querySelectorAll('#navigation a[href^="#"]')].map(a => {
    const svg = a.querySelector('.nav-icon svg'), drawn = svg && svg.querySelector('path');
    const style = svg ? getComputedStyle(svg) : {};
    return {route: a.getAttribute('href').slice(1), name: a.innerText.trim(), d: drawn && drawn.getAttribute('d'),
            table: NX_ICON_PATHS[a.getAttribute('href').slice(1)] || null, stroke: style.strokeWidth, cap: style.strokeLinecap,
            join: style.strokeLinejoin, fill: style.fill, width: svg && svg.getBoundingClientRect().width};
  }));
  check(nav.length >= 12, 'the sidebar was not found: ' + nav.length);
  for (const item of nav) {
    check(item.table && item.d === item.table, `${item.name}: the picture is not the one of the table`);
    check(item.stroke === '1.75px' || item.stroke === '1.75', `${item.name}: stroke ${item.stroke}`);
    check(item.cap === 'round' && item.join === 'round' && item.fill === 'none', `${item.name}: not drawn the one way`);
    check(Math.round(item.width) === 18, `${item.name}: ${item.width} wide`);
  }
  check(new Set(nav.map(item => item.d)).size === nav.length, 'two pages of the sidebar share a picture: ' + nav.map(item => item.name).join(' '));

  // No symbol is left as a character, on any page of the sidebar; what was replaced still reads the same.
  let pages = 0;
  for (const item of nav) {
    await page.locator(`#navigation a[href="#${item.route}"]`).click();
    await page.locator(`#navigation a[href="#${item.route}"].active`).waitFor();
    const left = await leftAsText();
    check(!left.length, `${item.name}: still characters: ${left.join(' | ')}`);
    pages += 1;
  }
  await page.locator('#navigation a[href="#overview"]').click();
  const guide = page.locator('#system-info');
  check((await guide.innerText()).trim() === '使用指南 ↗' && await guide.locator('svg.nx-i').count() === 1,
        'the guide button does not read as before with its arrow drawn: ' + await guide.innerHTML());
  const arrow = await guide.locator('svg.nx-i').evaluate(svg => ({d: svg.querySelector('path').getAttribute('d'), open: NX_ICON_PATHS.open,
    width: svg.getBoundingClientRect().width, hidden: svg.nextElementSibling.getBoundingClientRect().width}));
  check(arrow.d === arrow.open && arrow.width > 8 && arrow.hidden === 0, 'the arrow of the guide button: ' + JSON.stringify(arrow));
  await page.screenshot({path: path.join(shots, 'icons-workbench.png')});

  // A dialog closes by a drawn cross, and it still closes.
  await guide.click();
  const close = page.locator('#dialog .close-dialog').first();
  await close.waitFor();
  check(await close.locator('svg.nx-i').count() === 1 && (await close.textContent()).trim() === '×', 'the cross of a dialog is not drawn');
  await page.screenshot({path: path.join(shots, 'icons-dialog.png')});
  await close.click();
  await page.locator('#dialog[open]').waitFor({state: 'detached'}).catch(() => page.locator('#dialog').waitFor({state: 'hidden'}));

  // What a page writes later is drawn too, and drawing twice changes nothing.
  const later = await page.evaluate(async () => {
    const probe = document.createElement('p');
    probe.id = 'nx-probe';
    probe.innerHTML = '<button type="button">← 返回</button><span>✓</span><code>← raw</code><em>我的机器人 → 能力表</em>';
    document.querySelector('#main').appendChild(probe);
    await new Promise(resolve => setTimeout(resolve, 0));
    const once = probe.innerHTML;
    const again = nxDrawGlyphs(probe);
    const result = {icons: probe.querySelectorAll('svg.nx-i').length, again, same: probe.innerHTML === once, text: probe.innerText,
                    code: probe.querySelector('code').innerHTML, words: probe.querySelector('em').innerHTML};
    probe.remove();
    return result;
  });
  check(later.icons === 2 && later.again === 0 && later.same, 'new content is not drawn once: ' + JSON.stringify(later));
  check(later.text.trim() === '← 返回✓← raw我的机器人 → 能力表', 'the words changed: ' + JSON.stringify(later.text));
  // Text rewritten in place is drawn too (the place of the picture when nobody is signed in is written
  // on every redraw), lines of a log are left as they were written, and a line is drawn in one pass.
  const rewritten = await page.evaluate(async () => {
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const probe = document.createElement('div');
    probe.innerHTML = '<span id="nx-a">甲</span><b id="nx-b">乙</b><div class="log-item"><p>✓ done</p></div><i id="nx-c">✓ ✓ 通过</i>';
    document.querySelector('#main').appendChild(probe);
    await tick();
    probe.querySelector('#nx-a').firstChild.nodeValue = '✓';
    probe.querySelector('#nx-b').textContent = '↑';
    const avatar = document.querySelector('#user-avatar'), before = avatar.textContent;
    avatar.textContent = '↗';
    await tick();
    const result = {a: probe.querySelectorAll('#nx-a svg.nx-i').length, b: probe.querySelectorAll('#nx-b svg.nx-i').length,
      log: probe.querySelector('.log-item p').innerHTML, twice: probe.querySelectorAll('#nx-c svg.nx-i').length,
      lines: probe.querySelectorAll('#nx-c .nx-line').length,
      person: avatar.querySelector('svg.nx-i path')?.getAttribute('d') === NX_ICON_PATHS.user, avatarText: avatar.textContent};
    avatar.textContent = before;
    probe.remove();
    return result;
  });
  check(rewritten.a === 1 && rewritten.b === 1, 'text rewritten in place is not drawn: ' + JSON.stringify(rewritten));
  check(rewritten.person && rewritten.avatarText === '↗', 'nobody signed in is not drawn as a person: ' + JSON.stringify(rewritten));
  check(rewritten.log === '✓ done', 'a line of a log was touched: ' + rewritten.log);
  check(rewritten.twice === 1 && rewritten.lines === 1, 'a line was drawn in more than one pass: ' + JSON.stringify(rewritten));
  // In a flex row too, the words keep their space and stay one line.
  const row = await page.evaluate(async () => {
    const probe = document.createElement('a');
    probe.style.display = 'inline-flex';
    probe.textContent = '← 全部任务';
    document.querySelector('#main').appendChild(probe);
    await new Promise(resolve => setTimeout(resolve, 0));
    const icon = probe.querySelector('svg.nx-i').getBoundingClientRect(), words = document.createRange();
    words.selectNodeContents(probe.querySelector('.nx-line').lastChild);
    const result = {text: probe.innerText, items: probe.children.length, gap: words.getBoundingClientRect().left - icon.right};
    probe.remove();
    return result;
  });
  check(row.text === '← 全部任务' && row.items === 1, 'a flex link does not read as before: ' + JSON.stringify(row));
  check(row.gap > -1 && row.gap < 12, 'the arrow and its words are not next to each other: ' + JSON.stringify(row));
  check(later.code === '← raw' && later.words === '我的机器人 → 能力表', 'code or a sentence was touched: ' + JSON.stringify(later));

  // The narrow layout opens the sidebar by a drawn menu.
  await page.setViewportSize({width: 375, height: 800});
  const menu = page.locator('#menu-toggle');
  await menu.waitFor();
  check(await menu.locator('svg.nx-i').count() === 1, 'the menu button is not drawn');
  await page.screenshot({path: path.join(shots, 'icons-narrow.png')});

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(`icons_browser: passed, ${nav.length} sidebar icons, ${pages} pages` + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(e => { console.error(e); process.exit(1); });
