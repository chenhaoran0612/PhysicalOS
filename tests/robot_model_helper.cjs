// Shared by the browser acceptance tests since issue #48: the forms that add a robot ask for its brand
// and model (required over SSH; by service address when the device reports none). One line after the
// script chose the body type in such a form:
//   await require('./robot_model_helper.cjs').fillBrandAndModel(page);
// fills the two inputs when the open dialog has them and they are empty. A form without them (changing
// the body type of an existing robot, connecting an existing robot) is left as it is.
const PRODUCT = {vendor: '示例机器人公司', model: 'X1'};

async function fillBrandAndModel(page, product = PRODUCT) {
  // Issue #53: the two inputs are two pick-lists now (web/robot_catalog.js).
  if (await page.locator('#dialog [data-rc-picker]').count()) return pickBrandAndModel(page.locator('#dialog'), product, true);
  for (const name of ['vendor', 'model']) {
    const input = page.locator(`#dialog input[name="${name}"]`);
    if (await input.count() && !(await input.first().inputValue())) await input.first().fill(product[name]);
  }
}

// Chooses a brand and model in the pick-lists of a form (`scope`: a locator the picker is inside of):
// from the lists when they have them, otherwise 「其他」 and typed. With `onlyIfEmpty` a picker that
// already has a brand is left as it is.
const OTHER = '__other__';
async function pickBrandAndModel(scope, product = PRODUCT, onlyIfEmpty = false) {
  const picker = scope.locator('[data-rc-picker]').first();
  if (onlyIfEmpty && (await picker.locator('[name="vendor"]').inputValue())) return;
  const has = (list, value) => list.locator('option').evaluateAll((options, v) => options.some(o => o.value === v), value);
  const vendors = picker.locator('[data-rc="vendor"]'), models = picker.locator('[data-rc="model"]');
  if (await has(vendors, product.vendor)) {
    await vendors.selectOption(product.vendor);
    if (await has(models, product.model)) return models.selectOption(product.model);
    await models.selectOption(OTHER);
  } else {
    await vendors.selectOption(OTHER);
    await picker.locator('[name="vendor"]').fill(product.vendor);
  }
  await picker.locator('[name="model"]').fill(product.model);
}

module.exports = {fillBrandAndModel, pickBrandAndModel, PRODUCT};
