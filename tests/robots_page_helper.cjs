// Shared by the browser acceptance tests since issue #41: on "我的机器人" a card keeps two buttons in
// sight and the rest in its 「更多功能」 list, and once there is a robot the three ways to add another
// are behind 「添加机器人」. These helpers do what a person does: open the list, or the dialog, first.
//
//   await robotAction(card, 'engine-ssh-edit').click()   a control of the card, wherever it is now
//   await addRobotWay(page, 'engine-ssh')                 「通过 SSH 调查真机」, on the page or in the dialog

// Opens the 「更多功能」 list of a card (a Playwright locator of the card). Does nothing when it is open.
async function openRobotMenu(card) {
  const menu = card.locator('details.robot-menu');
  await menu.locator('summary').waitFor();
  if (!(await menu.evaluate(el => el.open))) await menu.locator('summary').click();
  await menu.locator('.robot-menu-panel').waitFor();
}

// An entry of the card's list, with the list opened.
function robotEntry(card, action) {
  const entry = card.locator(`.robot-menu-panel [data-action="${action}"]`);
  return {
    async click(options) { await openRobotMenu(card); await entry.click(options); },
    locator: entry,
  };
}

// A control of the card by its action: clicked where it is in sight, otherwise through the list.
function robotAction(card, action) {
  const any = card.locator(`[data-action="${action}"]`);
  return {
    async click(options) {
      await any.first().waitFor({state: 'attached'});
      if (!(await any.first().isVisible()) && await card.locator('details.robot-menu').count()) await openRobotMenu(card);
      await any.first().click(options);
    },
    locator: any,
  };
}

// One of the three ways to add a robot ('sim-add' | 'engine-ssh' | 'robot-add'): on the page while
// there is no robot, behind 「添加机器人」 afterwards.
async function addRobotWay(page, action) {
  const onPage = page.locator(`#main .robot-entry [data-action="${action}"]`), add = page.locator('#main [data-action="engine-robots-add"]');
  await onPage.or(add).first().waitFor();
  if (await onPage.count()) return onPage.click();
  await add.click();
  await page.locator(`#dialog .rp-add [data-action="${action}"]`).click();
}

// For the scripts written before issue #41, which click a card's control or one of the three ways by
// its selector: one line after the page is created,
//   require('./robots_page_helper.cjs').throughRobotsPage(page);
// and every locator.click(), .waitFor() (for visible) and .innerText() of that page first does what a person now has to do: opens the 「更多功能」
// list the control is in, or 「添加机器人」 when the three ways are behind it. Nothing else changes: a
// control that is in sight is clicked as before, and a control that does not exist still times out.
// What this does not cover: isVisible(), count(), allInnerTexts() and the text of a whole card (what is in
// a closed list is not part of it). Open the list first (openRobotMenu).
const ADD_WAY = /data-action=\\?"(sim-add|engine-ssh|robot-add)\\?"/;
function throughRobotsPage(page) {
  const proto = Object.getPrototypeOf(page.locator('html'));
  if (proto.clickBeforeRobotsPage) return;
  proto.clickBeforeRobotsPage = proto.click;
  proto.waitForBeforeRobotsPage = proto.waitFor;
  proto.innerTextBeforeRobotsPage = proto.innerText;
  proto.click = async function click(options) {
    const target = this.first(), owner = this.page(), timeout = options?.timeout;
    const add = owner.locator('#main [data-action="engine-robots-add"]');
    {
      // One of the three ways, asked for without naming a robot ("配置 SSH" on a card has the same action
      // and a data-id): where the page has 「添加机器人」, the way is in that button's dialog.
      const way = ADD_WAY.exec(String(this));
      if (way && !/data-id|#dialog|\.card\b/.test(String(this))) {
        await target.or(add).first().waitFor({state: 'attached', timeout});
        if (await add.count() && !(await owner.locator('#dialog[open]').count())) {
          await proto.clickBeforeRobotsPage.call(add, {timeout});
          return await proto.clickBeforeRobotsPage.call(owner.locator(`#dialog .rp-add [data-action="${way[1]}"]`), options);
        }
      }
    }
    await openListOf(this, timeout);
    // Issue #53: a control the card keeps out of sight is clicked where a person finds it, in 「实时状态」.
    const kept = await keptOnCard(this, timeout);
    if (kept) return clickInStatus(owner, kept, options);
    return proto.clickBeforeRobotsPage.call(this, options);
  };
  // Waiting for something to be visible, and reading its text: likewise through the list it is in.
  proto.waitFor = async function waitFor(options) {
    if (!options?.state || options.state === 'visible') {
      await openListOf(this, options?.timeout);
      // Issue #53: what the card keeps out of sight is "visible" once it is on the card and not hidden by its own feature.
      if (await keptOnCard(this, options?.timeout)) return waitKept(this, options?.timeout);
    }
    return proto.waitForBeforeRobotsPage.call(this, options);
  };
  proto.innerText = async function innerText(options) {
    await openListOf(this, options?.timeout);
    const text = await proto.innerTextBeforeRobotsPage.call(this, options);
    // Issue #53: the text of a whole card is what it shows and what it keeps for 「实时状态」 (the same
    // elements are shown there), so a script that looks for a state "on the card" still finds it.
    // The heartbeat line of a card likewise: the card shows its badge, the rest of the line is kept.
    const line = el => [...el.children].map(part => part.innerText || part.textContent).filter(Boolean).join(' ');
    const kept = await this.first().evaluate((el, lineSource) => {
      const whole = eval(lineSource);
      if (el.matches?.('article.rp-card .rp-title > .hb-line')) return {instead: whole(el)};
      if (!el.matches?.('article.rp-card')) return null;
      return {more: [...el.querySelectorAll('.rp-title > .hb-line, .rp-details')].map(part => (part.matches('.hb-line') ? whole(part) : part.innerText || part.textContent)).join('\n')};
    }, String(line)).catch(() => null);
    if (kept?.instead !== undefined) return kept.instead;
    return kept?.more ? text + '\n' + kept.more : text;
  };
}

// Opens the 「更多功能」 list the first element of the locator is in, if it is in a closed one.
async function openListOf(locator, timeout) {
  try {
    const proto = Object.getPrototypeOf(locator), target = locator.first();
    await proto.waitForBeforeRobotsPage.call(target, {state: 'attached', timeout});
    await target.evaluate(el => {
      const menu = el.closest('details.robot-menu');
      // Not for the button of a list itself: clicking it is what opens the list.
      if (menu && !menu.open && !el.closest('summary')) menu.querySelector('summary').click();
    });
  } catch { /* not there: the call that follows reports it as it always did */ }
}

// ---------------------------------------------------------------- issue #53: what a card keeps out of sight
// A card shows a picture, a name, online or not, the brand and model, and its buttons. Everything else the
// features put on it (when the last heartbeat was and why none comes, the address, the tags, start at boot,
// the progress of an installation) is kept in the card, out of sight, and shown in the robot's 「实时状态」.
//   * reading it (innerText, textContent, count) works as before: the elements are still in the card, and
//     innerText() of a whole card answers what it shows followed by what it keeps;
//   * waiting for it to be visible waits until it is there and its own feature does not hide it;
//   * clicking a control in it opens 「实时状态」, clicks the same control there and closes the dialog
//     again unless the click itself opened something;
//   * robotStatus(page, card) opens 「实时状态」 and returns the locator of the overview, for a script
//     that wants to look at what a person sees.
const KEPT = `el => {
  const card = el.closest('article.rp-card');
  if (!card) return null;
  const line = el.closest('.rp-title > .hb-line');
  if (!el.closest('.rp-details') && !(line && el.closest('.hb-facts, .hb-trouble'))) return null;
  const all = [...card.querySelectorAll('.rp-title > .hb-line, .rp-title > .hb-line *, .rp-details *')];
  return {robot: card.dataset.rpRobot, index: all.indexOf(el)};
}`;

async function keptOnCard(locator, timeout) {
  try {
    const proto = Object.getPrototypeOf(locator), target = locator.first();
    await proto.waitForBeforeRobotsPage.call(target, {state: 'attached', timeout});
    const found = await target.evaluate(eval(KEPT));
    return found && found.index >= 0 ? found : null;
  } catch { return null; }
}

async function waitKept(locator, timeout = 30000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const shown = await locator.first().evaluate(el => {
      for (let at = el; at && !at.matches('.rp-details'); at = at.parentElement) if (at.hidden) return false;
      return true;
    }).catch(() => false);
    if (shown) return;
    if (Date.now() > deadline) throw Error(`waitFor: ${locator} is not shown in 实时状态 within ${timeout} ms`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}

async function openStatus(page, robotId, timeout) {
  const proto = Object.getPrototypeOf(page.locator('html')), click = proto.clickBeforeRobotsPage || proto.click, wait = proto.waitForBeforeRobotsPage || proto.waitFor;
  const overview = page.locator(`#dialog[open] .rp-overview[data-rp-overview="${robotId}"]`);
  if (await overview.count()) return {overview, opened: false};
  await click.call(page.locator(`article.rp-card[data-rp-robot="${robotId}"] .rp-status-button`), {timeout});
  await wait.call(overview, {timeout});
  return {overview, opened: true};
}

async function clickInStatus(page, kept, options) {
  const proto = Object.getPrototypeOf(page.locator('html'));
  const {opened} = await openStatus(page, kept.robot, options?.timeout);
  await page.evaluate(index => {
    document.querySelectorAll('[data-rp-twin]').forEach(el => el.removeAttribute('data-rp-twin'));
    [...document.querySelector('#dialog .rp-overview').querySelectorAll('*')][index]?.setAttribute('data-rp-twin', '');
  }, kept.index);
  await proto.clickBeforeRobotsPage.call(page.locator('#dialog [data-rp-twin]'), options);
  // What was clicked acts in place (a retry): the dialog that was opened for it is closed again.
  await page.waitForTimeout(150);
  if (opened && await page.locator(`#dialog[open] .rp-overview[data-rp-overview="${kept.robot}"]`).count()) {
    await proto.clickBeforeRobotsPage.call(page.locator('#dialog [data-action="close"]').first());
  }
}

// Opens 「实时状态」 of a card (a locator of it) and returns the locator of the overview in the dialog.
async function robotStatus(page, card) {
  const robotId = await card.first().getAttribute('data-rp-robot');
  return (await openStatus(page, robotId)).overview;
}

module.exports = {openRobotMenu, robotEntry, robotAction, addRobotWay, throughRobotsPage, robotStatus};
