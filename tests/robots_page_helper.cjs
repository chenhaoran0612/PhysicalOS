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
    return proto.clickBeforeRobotsPage.call(this, options);
  };
  // Waiting for something to be visible, and reading its text: likewise through the list it is in.
  proto.waitFor = async function waitFor(options) {
    if (!options?.state || options.state === 'visible') await openListOf(this, options?.timeout);
    return proto.waitForBeforeRobotsPage.call(this, options);
  };
  proto.innerText = async function innerText(options) {
    await openListOf(this, options?.timeout);
    return proto.innerTextBeforeRobotsPage.call(this, options);
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

module.exports = {openRobotMenu, robotEntry, robotAction, addRobotWay, throughRobotsPage};
