// Issue #61 browser acceptance: a brand, and a model, is one control everywhere, a list to choose from that
// can also be typed in. No pick-list with 「其他」 next to a second input any more.
//   1  the form that adds a robot has two controls for brand and model, and nothing else for them;
//   2  the button of the box opens the whole list, above the dialog and inside the window; an option is
//      chosen with the mouse; the model box then offers the models of that brand;
//   3  typing narrows the list (another spelling of a brand finds it); the arrow keys and Enter choose
//      without sending the form; Escape closes the list and leaves the dialog open; a model of the old
//      brand does not stay under another brand;
//   4  a brand and model that are not listed are simply typed, and saved as typed (markup stays text);
//   5  the brand of a robot the account has comes first in the list of every other form, marked;
//      「修改品牌型号」 starts from what is saved;
//   6  the form of a skill (机器人应用) has the same two controls;
//   7  nothing overflows at 375, 768 and 1440, and the list stays inside the window.
// Fixture: tests/robot_model_browser_fixture.py (HTTP contract devices; no real machine).
// Usage: node tests/brand_combo_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const {addRobotWay, robotAction} = require('./robots_page_helper.cjs');
const {chooseFromList} = require('./robot_model_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const WIDTHS = [375, 768, 1440], DESKTOP = {width: 1440, height: 1000};
const OWN = {vendor: 'Beta <b>', model: 'B2'};               // a brand with markup in it must be shown as text
let failed = async () => {};

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-brand-combo-ui.json'));
  const dir = process.argv[3] || 'artifacts';
  fs.mkdirSync(dir, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: DESKTOP}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.emulateMedia({reducedMotion: 'reduce'});            // screenshots show the list as it rests, not half faded in
  const file = name => path.join(dir, `brand-combo-${engine.name()}-${name}.png`);
  failed = async () => { await page.screenshot({path: file('failure')}).catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog'), list = page.locator('#combo-list');
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const same = (actual, expected, what) => must(JSON.stringify(actual) === JSON.stringify(expected),
                                                `${what}: ${JSON.stringify(actual)} instead of ${JSON.stringify(expected)}`);
  const appState = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const options = () => list.locator('[role="option"] > span').allInnerTexts();
  const toggleOf = box => box.locator('xpath=..').locator('[data-combo-toggle]');
  // The list is drawn where a person can reach it: inside the window, and nothing of the dialog over it.
  const reachable = async what => {
    const seen = await page.evaluate(() => {
      const el = document.querySelector('#combo-list'), box = el.getBoundingClientRect();
      const first = el.querySelector('[role="option"]').getBoundingClientRect();
      const top = document.elementFromPoint(first.left + first.width / 2, first.top + first.height / 2);
      return {inside: box.left >= 0 && box.top >= 0 && box.right <= innerWidth && box.bottom <= innerHeight, onTop: Boolean(top && top.closest('#combo-list')),
              box: [box.left, box.top, box.right, box.bottom].map(Math.round), window: [innerWidth, innerHeight]};
    });
    must(seen.inside && seen.onTop, `${what}: the list is cut off or covered ${JSON.stringify(seen)}`);
  };

  await page.goto(cfg.url);
  await gateSignIn(page, {name: '品牌选择验收', email: 'brand-combo@example.test', password: crypto.randomBytes(9).toString('hex') + 'A1'});

  // 1 ---- two controls, nothing else
  await page.goto(cfg.url + '/#robots');
  await addRobotWay(page, 'robot-add');
  const add = dialog.locator('form[data-form=robot]');
  await add.waitFor();
  const picker = add.locator('[data-rc-picker]'), vendor = picker.locator('[name=vendor]'), model = picker.locator('[name=model]');
  same([await picker.locator('input').count(), await picker.locator('select').count(), await picker.locator('label').count()], [2, 0, 2], 'inputs, pick-lists and fields of the picker');
  const labels = (await picker.innerText()).replace(/\s+/g, ' ').trim();
  must(labels === '品牌 型号', `the picker says more than 品牌 and 型号: ${labels}`);
  must(await list.count() === 0 || await list.isHidden(), 'a list is open before anything was done');
  await page.screenshot({path: file('1-two-controls')});

  // 2 ---- the button opens the whole list; the mouse chooses
  await toggleOf(vendor).click();
  await list.waitFor();
  const all = await options();
  must(all.length > 30 && all.includes('智身科技') && all.includes('宇树科技') && !all.includes('neuxmind-sim'), `the brands of the list are not offered: ${all.slice(0, 8)}`);
  must(!(await list.innerText()).includes('其他'), 'the list still has an 「其他」');
  same(await vendor.getAttribute('aria-expanded'), 'true', 'the box says its list is open');
  await reachable('the brand list');
  await page.screenshot({path: file('2-brand-list')});
  must(await list.isVisible(), 'taking a picture closed the list');
  await toggleOf(vendor).click();
  must(await list.isHidden(), 'the button does not close the list again');
  await chooseFromList(page, vendor, '智身科技');
  same([await vendor.inputValue(), await list.isHidden()], ['智身科技', true], 'the brand chosen with the mouse, and the list after it');
  must(await vendor.evaluate(el => document.activeElement === el), 'the box lost the focus when an option was clicked');
  await toggleOf(model).click();
  await list.waitFor();
  const models = await options();
  must(models.includes('钢镚 L2') && models.includes('钢镚 L2-U') && !models.includes('Go2'), `the models are not those of the brand: ${models}`);
  await reachable('the model list');
  await list.locator('[role="option"]', {hasText: '钢镚 L2-U'}).click();
  same(await model.inputValue(), '钢镚 L2-U', 'the model chosen with the mouse');

  // 3 ---- typing narrows; the keys choose; Escape closes only the list
  await vendor.click();
  await list.waitFor();
  must((await list.locator('.combo-chosen').innerText()).includes('智身科技'), 'the list does not show which brand the box holds');
  await vendor.fill('uni');
  same(await options(), ['宇树科技', 'Universal Robots'], 'the brands a typed "uni" leaves (unitree is another spelling of 宇树科技)');
  await page.keyboard.press('ArrowDown');
  same(await vendor.getAttribute('aria-activedescendant'), 'combo-option-0', 'the option the arrow key is on');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Enter');
  same([await vendor.inputValue(), await list.isHidden(), await dialog.evaluate(el => el.open)], ['宇树科技', true, true], 'Enter on an option chooses it and sends nothing');
  same((await appState()).robots.length, 0, 'robots after Enter in the list');
  same(await model.inputValue(), '', 'the model of the old brand under the new brand');
  await model.fill('go');
  await list.waitFor();
  must((await options()).every(text => /go/i.test(text)) && (await options()).includes('Go2'), `the models a typed "go" leaves: ${await options()}`);
  await page.keyboard.press('Escape');
  same([await list.isHidden(), await dialog.evaluate(el => el.open), await model.inputValue()], [true, true, 'go'], 'Escape with the list open');

  // 4 ---- a brand and model that are not listed are typed
  await vendor.fill(OWN.vendor);
  must(await list.isHidden(), 'a list is offered for a brand nobody knows');
  await model.fill(OWN.model);
  await toggleOf(model).click();
  must((await list.innerText()).includes('直接输入') && await list.locator('[role="option"]').count() === 0, 'the empty list does not say to type');
  await toggleOf(model).click();
  await add.locator('[name=name]').fill('巡检狗一号');
  await add.locator('[name=family]').selectOption('quadruped');
  await add.locator('[name=endpoint]').fill(cfg.devices['silent-1']);
  await add.locator('[name=token]').fill(cfg.access);
  await page.screenshot({path: file('4-typed')});
  await add.locator('button[type=submit]').click();
  await dialog.waitFor({state: 'hidden'});
  const robot = (await appState()).robots.find(r => r.name === '巡检狗一号');
  same(robot && robot.product && [robot.product.vendor, robot.product.model], [OWN.vendor, OWN.model], 'the typed brand and model, as saved');

  // 5 ---- the account's own brand comes first in every other form; the edit starts from what is saved
  await page.evaluate(() => refresh(true));
  await robotAction(page.locator(`article.card:has([data-id="${robot.id}"])`).first(), 'engine-robot-model').click();
  const assign = dialog.locator('form[data-form="engine-robot-model"]');
  await assign.waitFor();
  const assignVendor = assign.locator('[name=vendor]'), assignModel = assign.locator('[name=model]');
  same([await assignVendor.inputValue(), await assignModel.inputValue()], [OWN.vendor, OWN.model], 'the form starts from what is saved');
  same([await assign.locator('[data-rc-picker] input').count(), await assign.locator('[data-rc-picker] select').count()], [2, 0], 'the controls of the edit');
  await toggleOf(assignVendor).click();
  await list.waitFor();
  same((await list.locator('[role="option"]').first().innerText()).replace(/\s+/g, ' ').trim(), `${OWN.vendor} 已有`, 'the first brand of the list');
  must(await list.locator('b').count() === 0, 'markup in a brand was not written as text');
  must((await list.locator('.combo-chosen').count()) === 1, 'the saved brand is not marked as the one in the box');
  await page.screenshot({path: file('5-own-first')});
  await toggleOf(assignVendor).click();
  await toggleOf(assignModel).click();
  same(await options(), [OWN.model], 'the models of an own brand');
  await page.keyboard.press('Escape');
  must(await dialog.evaluate(el => el.open), 'Escape closed the dialog with the list');
  same(await assignVendor.getAttribute('aria-label'), '品牌', 'what the box is called');
  // the dialog is drawn again while a list is open (WebKit does not tell the removed input): no list is left behind
  await toggleOf(assignVendor).click();
  await list.waitFor();
  await page.evaluate(() => dialog('另一个对话框', '', '<div class="dialog-body"><p>没有输入框</p></div>'));
  must(await list.count() === 0 || await list.isHidden(), 'the list of the form that is gone is still on the screen');
  await dialog.locator('.close-dialog').click();
  await dialog.waitFor({state: 'hidden'});

  // 6 ---- the form of a skill has the same two controls, filled in when every robot is of one brand and model
  await page.goto(cfg.url + '/#app-skills');      // issue #60: the skills have a page of their own
  await page.locator('#main [data-action="skill-add"]').first().click();
  const skill = dialog.locator('form[data-form=skill]');
  await skill.waitFor();
  same([await skill.locator('[data-rc-picker] input').count(), await skill.locator('[data-rc-picker] select').count(), await skill.locator('[data-rc-picker] label').count()],
       [2, 0, 2], 'the controls of the skill form');
  same([await skill.locator('[name=vendor]').inputValue(), await skill.locator('[name=model]').inputValue()], [OWN.vendor, OWN.model], 'the skill form starts with the brand and model of the only robot');

  // 7 ---- widths
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 760});
    const wide = await dialog.evaluate(el => Math.max(el.scrollWidth - el.clientWidth, document.documentElement.scrollWidth - document.documentElement.clientWidth));
    must(wide <= 1, `the dialog or the page scrolls sideways by ${wide}px at ${width}`);
    const boxes = await skill.locator('[data-rc-picker] .combo').evaluateAll(els => els.map(el => { const b = el.getBoundingClientRect(); return [b.left, b.right, innerWidth]; }));
    must(boxes.every(([left, right, inner]) => left >= 0 && right <= inner), `a box is outside the window at ${width}: ${JSON.stringify(boxes)}`);
    const skillVendor = skill.locator('[name=vendor]');
    await skillVendor.scrollIntoViewIfNeeded();
    await toggleOf(skillVendor).click();
    await list.waitFor();
    await reachable(`the brand list at ${width}`);
    await page.screenshot({path: file(`7-skill-${width}`)});
    await page.keyboard.press('Escape');
    must(await list.isHidden(), `the list stays open at ${width}`);
  }
  await page.setViewportSize(DESKTOP);

  must(!errors.length, 'page errors: ' + errors.join(' | '));
  console.log(JSON.stringify({passed: true, engine: engine.name(), twoControls: true, listAboveDialog: true, mouse: true, keyboard: true,
                              typedNotListed: true, ownFirst: true, skillForm: true, widths: WIDTHS}));
  await browser.close();
})().catch(async e => { await failed(); console.error(e); process.exit(1); });
