// Issue #55 browser acceptance: the sign-in page after the rename to 「Physical OS」.
//   - the page title and the left side carry the new name; "Robot OS" is nowhere on the page;
//   - the four steps (接入本体 / 开发应用 / 测试验证 / 部署上岗) are gone; the slogan is the largest text on the left;
//   - the left side moves (light, orbits), and stands still when the system asks for less motion;
//   - nothing overflows at 1440, 1024, 768 and 375 wide; at phone width the left side gives way to the form;
//   - the form still signs in (built-in test account) and the app behind it uses the new name.
// Usage: NEUXMIND_MAIL_DEV_ECHO=1 NEUXMIND_CLOUD_AUTO_LOGIN=0 python3 -m brainos.server --port 8855 --state-dir <temp dir>
//        node tests/login_page_browser.cjs http://127.0.0.1:8855 <screenshot dir>
const {chromium, webkit} = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const {gateSignIn} = require('./gate_helper.cjs');

const base = process.argv[2], shots = process.argv[3];
const STEPS = ['接入本体', '开发应用', '测试验证', '部署上岗'];
const WIDTHS = [[1440, 900], [1024, 768], [768, 1024], [375, 812]];
const ASIDE_HIDDEN_BELOW = 861;
const MIN_SLOGAN_PX = 40;
const fail = message => { console.error('FAIL', message); process.exit(1); };
const ok = (condition, message) => { if (!condition) fail(message); console.log('ok', message); };

const runningAnimations = page => page.evaluate(() =>
  document.querySelector('.auth-aside').getAnimations({subtree: true}).filter(a => a.playState === 'running').length);
const overflow = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

(async () => {
  fs.mkdirSync(shots, {recursive: true});
  const engine = process.env.BRAIN_BROWSER === 'webkit' ? webkit : chromium;
  const browser = await engine.launch();
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(base);
  const gate = page.locator('#auth-gate');
  await gate.locator('form[data-acct-form="login"]').waitFor();

  // The name.
  ok(await page.title() === 'NeuxMind · Physical OS', 'the page title is "NeuxMind · Physical OS"');
  const aside = gate.locator('.auth-aside');
  const asideText = await aside.innerText();
  ok(/PHYSICAL OS/.test(asideText), 'the left side names Physical OS');
  ok(!/robot\s*os/i.test(await page.locator('body').innerText()), 'the page nowhere says Robot OS');

  // The steps are gone, the slogan stays and is the largest text.
  for (const step of STEPS) ok(!asideText.includes(step), `the left side no longer lists 「${step}」`);
  ok(await gate.locator('.auth-steps').count() === 0, 'no step list is left in the page');
  const slogan = aside.locator('.auth-slogan');
  ok((await slogan.innerText()).replace(/\s+/g, '') === '让你的机器人，准备上岗。', 'the slogan reads 让你的机器人，准备上岗。');
  const sizes = await aside.evaluate(el => {
    const px = node => parseFloat(getComputedStyle(node).fontSize);
    const others = [...el.querySelectorAll('.auth-logo, .auth-kicker, .auth-legal')].map(px);
    return {slogan: px(el.querySelector('.auth-slogan')), others: Math.max(...others)};
  });
  ok(sizes.slogan >= MIN_SLOGAN_PX && sizes.slogan >= sizes.others * 2, `the slogan is set large (${sizes.slogan}px against ${sizes.others}px)`);
  const lit = await aside.locator('.auth-slogan-lit').evaluate(el => getComputedStyle(el).backgroundImage);
  ok(lit.includes('gradient'), 'the second line of the slogan is lit with a gradient');

  // The slogan does not run into the logo, the footer line or the edge of the panel.
  const boxes = await aside.evaluate(el => {
    const box = selector => el.querySelector(selector).getBoundingClientRect();
    const s = box('.auth-slogan'), logo = box('.auth-logo'), legal = box('.auth-legal'), a = el.getBoundingClientRect();
    return {clear: s.top > logo.bottom && s.bottom < legal.top, inside: s.left >= a.left && s.right <= a.right};
  });
  ok(boxes.clear && boxes.inside, 'the slogan sits between the logo and the footer line, inside the panel');

  // Motion, and no motion on request.
  ok(await runningAnimations(page) >= 6, 'light and orbits are moving');
  await page.waitForTimeout(1800);
  await page.screenshot({path: shots + '/01-login-1440.png'});
  await page.emulateMedia({reducedMotion: 'reduce'});
  ok(await runningAnimations(page) === 0, 'nothing moves when less motion is asked for');
  const named = await aside.evaluate(el => [...el.querySelectorAll('*')].flatMap(n => [n, [n, '::before'], [n, '::after']])
    .map(t => Array.isArray(t) ? getComputedStyle(t[0], t[1]).animationName : getComputedStyle(t).animationName).filter(n => n && n !== 'none'));
  ok(named.length === 0, 'no element of the left side keeps an animation' + (named.length ? ': ' + named.join(',') : ''));
  const still = await slogan.locator('span').first().evaluate(el => getComputedStyle(el).opacity);
  ok(still === '1', 'the slogan is fully shown without its entrance');
  await page.screenshot({path: shots + '/02-login-reduced-motion.png'});
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await page.waitForTimeout(2200); // the entrance plays again; the pictures below are of the settled page

  // Widths.
  for (const [width, height] of WIDTHS) {
    await page.setViewportSize({width, height});
    ok(await overflow(page) <= 0, `no sideways overflow at ${width}px`);
    const shown = await aside.isVisible();
    ok(shown === (width >= ASIDE_HIDDEN_BELOW), `the left side is ${width >= ASIDE_HIDDEN_BELOW ? 'shown' : 'hidden'} at ${width}px`);
    ok(await gate.locator('form[data-acct-form="login"] [type=submit]').isVisible(), `the sign-in button is on screen at ${width}px`);
    await page.screenshot({path: `${shots}/03-login-${width}.png`});
  }
  await page.setViewportSize({width: 1440, height: 600});
  await page.waitForTimeout(2200); // the left side was hidden at phone width: its entrance plays again
  const low = await slogan.evaluate(el => ({shown: [...el.children].every(s => getComputedStyle(s).opacity === '1'), top: el.getBoundingClientRect().top, logoBottom: el.closest('.auth-aside').querySelector('.auth-logo').getBoundingClientRect().bottom, bottom: el.getBoundingClientRect().bottom, legalTop: el.closest('.auth-aside').querySelector('.auth-legal').getBoundingClientRect().top}));
  ok(await overflow(page) <= 0 && low.shown && low.top > low.logoBottom && low.bottom < low.legalTop, 'a low window still shows the whole slogan');
  await page.screenshot({path: shots + '/04-login-low-window.png'});
  await page.setViewportSize({width: 1440, height: 900});

  // Going to another form keeps the left side as it is: same element, entrance not played again.
  await aside.evaluate(el => { el.dataset.kept = 'yes'; });
  await gate.locator('[data-acct-mode="register"]').click();
  await gate.locator('form[data-acct-form="register"]').waitFor();
  ok(await aside.getAttribute('data-kept') === 'yes', 'opening 创建账号 keeps the left side in place');
  ok(await slogan.locator('span').first().evaluate(el => getComputedStyle(el).opacity) === '1', 'the slogan does not come in again');
  await page.screenshot({path: shots + '/05-register.png'});

  const stamp = Date.now();
  const letters = 'Qw' + stamp.toString(36) + 'z7';
  await gateSignIn(page, {name: '改名检查', email: `rename${stamp}@example.test`, password: letters + letters});
  ok(await gate.isHidden(), 'registering through the new page reaches the app');
  ok(!/robot\s*os/i.test(await page.locator('body').innerText()), 'the app nowhere says Robot OS');
  await page.screenshot({path: shots + '/06-app.png'});

  ok(errors.length === 0, 'no script errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
  await browser.close();
  console.log('login page: all checks passed');
})().catch(e => fail(e.stack || String(e)));
