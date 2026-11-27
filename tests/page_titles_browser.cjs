// Browser acceptance for issue #57 (标题统一): every page of the sidebar is titled the way 工作台 is —
// its English name in small capitals above, the name the sidebar calls it by below, drawn alike.
//   node tests/page_titles_browser.cjs <fixture-config.json> [screenshot-dir]
// Fixture: tests/ux01_browser_fixture.py (a fresh state directory; the server needs NEUXMIND_MAIL_DEV_ECHO=1).
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app.
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const {gateSignIn} = require('./gate_helper.cjs');
const crypto = require('crypto');
const fs = require('fs');

const WORKBENCH = {en: 'WORKBENCH', zh: '工作台'};
const STYLE = ['fontSize', 'fontWeight', 'letterSpacing', 'color', 'marginBottom', 'textTransform', 'fontFamily'];

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || process.env.BRAIN_QA_CONFIG || '/tmp/neuxmind-ux01-qa.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const main = page.locator('#main');
  // The head of the page that is open: the lines of its title block in order, and how the two are drawn.
  const readHead = () => main.locator('.page-head').first().evaluate((el, props) => {
    if (!el.isConnected) return null;              // drawn anew between finding it and reading it: its styles read empty
    const block = el.firstElementChild, eyebrow = block.querySelector('.eyebrow'), title = block.querySelector('h1');
    const drawn = node => node ? Object.fromEntries(props.map(p => [p, getComputedStyle(node)[p]])) : null;
    const box = node => node ? node.getBoundingClientRect() : null;
    return {
      heads: document.querySelectorAll('#main .page-head').length, titles: document.querySelectorAll('#main h1').length,
      eyebrows: el.querySelectorAll('.eyebrow').length,
      order: [...block.children].map(c => c.className || c.tagName.toLowerCase()),
      en: eyebrow?.innerText.trim(), zh: title?.innerText.trim(), eyebrowStyle: drawn(eyebrow), titleStyle: drawn(title),
      eyebrowLeft: box(eyebrow)?.left, titleLeft: box(title)?.left, gap: box(title) && box(eyebrow) ? box(title).top - box(eyebrow).bottom : null,
    };
  }, STYLE);
  // Some pages draw themselves once more right after they open (the run records' first answer on 仿真试运行 and the other
  // record pages, 云平台账户 when its account arrives). A head replaced that way is read again; what is checked is unchanged.
  const head = async () => {
    for (let round = 0; round < 5; round += 1) {
      const at = await readHead();
      if (at) return at;
    }
    throw Error('The page head was replaced five times while it was being read');
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '标题统一验收', email: `titles-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'titles1-' + crypto.randomBytes(8).toString('hex')});
  await main.locator('.tw-board').waitFor();

  // 1. 工作台 is the model.
  const model = await head();
  check(model.en === WORKBENCH.en && model.zh === WORKBENCH.zh, `工作台 is titled ${model.en} / ${model.zh}`);
  check(model.order[0] === 'eyebrow' && model.order[1] === 'h1', `工作台: the title block is ${model.order}`);

  // 2. Every page of the sidebar, opened from the sidebar.
  const links = await page.locator('#navigation a').evaluateAll(all => all.map(a => ({route: a.getAttribute('href').slice(1), name: a.innerText.trim()})));
  check(links.length >= 15, `The sidebar has ${links.length} pages`);
  const seen = new Map();
  for (const {route, name} of links) {
    await page.locator(`#navigation a[href="#${route}"]`).click();
    await page.waitForFunction(r => location.hash === '#' + r, route);
    await main.locator('.page-head h1', {hasText: name}).waitFor();
    const at = await head();
    check(at.zh === name, `「${name}」 is titled 「${at.zh}」`);
    check(/^[A-Z]+(?: (?:& )?[A-Z]+)*$/.test(at.en || ''), `「${name}」: the line above the title is 「${at.en}」, not its English name`);
    check(!seen.has(at.en), `「${name}」 and 「${seen.get(at.en)}」 share the English name ${at.en}`);
    seen.set(at.en, name);
    check(at.heads === 1 && at.titles === 1 && at.eyebrows === 1, `「${name}」 has ${at.heads} heads, ${at.titles} titles, ${at.eyebrows} lines above`);
    check(at.order[0] === 'eyebrow' && at.order[1] === 'h1', `「${name}」: the title block is ${at.order}`);
    for (const prop of STYLE) {
      check(at.eyebrowStyle[prop] === model.eyebrowStyle[prop], `「${name}」: the English line's ${prop} is ${at.eyebrowStyle[prop]}, on 工作台 ${model.eyebrowStyle[prop]}`);
      check(at.titleStyle[prop] === model.titleStyle[prop], `「${name}」: the title's ${prop} is ${at.titleStyle[prop]}, on 工作台 ${model.titleStyle[prop]}`);
    }
    check(Math.abs(at.eyebrowLeft - model.eyebrowLeft) < 1 && Math.abs(at.titleLeft - model.titleLeft) < 1 && Math.abs(at.gap - model.gap) < 1,
          `「${name}」: the title stands at ${at.eyebrowLeft}/${at.titleLeft} with a gap of ${at.gap}; 工作台 ${model.eyebrowLeft}/${model.titleLeft}, ${model.gap}`);
    check((await page.locator('#breadcrumb').innerText()).includes(name), `The breadcrumb of 「${name}」: ${await page.locator('#breadcrumb').innerText()}`);
    await page.screenshot({path: `${shots}/page-titles-${String(seen.size).padStart(2, '0')}-${route}.png`});
  }
  check(seen.get('WORKBENCH') === '工作台' && seen.get('MY ROBOTS') === '我的机器人', `The English names: ${[...seen.keys()]}`);

  // 3. A page opened afresh by its address is titled the same way (the first drawing, before the state arrives, included).
  await page.goto('about:blank');
  await page.goto(cfg.url + '/#harness');
  await main.locator('.page-head h1', {hasText: '验证与测试'}).waitFor();
  check((await head()).en === 'VERIFICATION & TESTING', `Opened afresh, 验证与测试 has ${(await head()).en} above its title`);

  // 4. The page of one task keeps the task's name alone (issue #52): it is not a page of the sidebar.
  await page.locator('#navigation a[href="#overview"]').click();
  await main.locator('.tw-head [data-action="journey-start"]').click();
  await page.locator('#dialog [name="name"]').fill('仓库巡检');
  await page.locator('#dialog [name="objective"]').fill('每天检查仓库通道并报告异常。');
  await page.locator('#dialog button[type=submit]').click();
  await main.locator('.tw-back').waitFor();
  check(await main.locator('h1').innerText() === '仓库巡检' && await main.locator('.page-head .eyebrow').count() === 0,
        'The page of a task is not titled with the name of the task alone');

  check(!errors.length, 'Page errors: ' + errors.join(' | '));
  await browser.close();
  console.log(`page_titles_browser: passed, ${seen.size} pages` + (engine.name() === 'chromium' ? '' : ` (${engine.name()})`));
})().catch(e => { console.error(e); process.exit(1); });
