// Shared by the browser acceptance tests since issue #48: the forms that add a robot ask for its brand
// and model (required over SSH; by service address when the device reports none). One line after the
// script chose the body type in such a form:
//   await require('./robot_model_helper.cjs').fillBrandAndModel(page);
// fills the two inputs when the open dialog has them and they are empty. A form without them (changing
// the body type of an existing robot, connecting an existing robot) is left as it is.
const PRODUCT = {vendor: '示例机器人公司', model: 'X1'};

async function fillBrandAndModel(page, product = PRODUCT) {
  if (await page.locator('#dialog [data-rc-picker]').count()) return pickBrandAndModel(page.locator('#dialog'), product, true);
  for (const name of ['vendor', 'model']) {
    const input = page.locator(`#dialog input[name="${name}"]`);
    if (await input.count() && !(await input.first().inputValue())) await input.first().fill(product[name]);
  }
}

// Sets the brand and model of a form (`scope`: a locator the picker is inside of). Since issue #61 each
// is one box that is a list and can be typed in (web/combo.js): the text is typed, which also covers a
// brand or model that is not listed. The box is left afterwards, so its list is closed and the form has
// heard the change. With `onlyIfEmpty` a picker that already has a brand is left as it is.
async function pickBrandAndModel(scope, product = PRODUCT, onlyIfEmpty = false) {
  const picker = scope.locator('[data-rc-picker]').first();
  const vendor = picker.locator('[name="vendor"]'), model = picker.locator('[name="model"]');
  if (onlyIfEmpty && (await vendor.inputValue())) return;
  await vendor.fill(product.vendor);
  await vendor.blur();
  await model.fill(product.model);
  await model.blur();
}

// Chooses an option of such a box from its list, as a person does with the mouse: opens the list with the
// button of the box and clicks the option.
async function chooseFromList(page, box, text) {
  await box.locator('xpath=..').locator('[data-combo-toggle]').click();
  await page.locator('#combo-list [role="option"]', {hasText: text}).first().click();
}

module.exports = {fillBrandAndModel, pickBrandAndModel, chooseFromList, PRODUCT};
