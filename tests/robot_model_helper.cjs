// Shared by the browser acceptance tests since issue #48: the forms that add a robot ask for its brand
// and model (required over SSH; by service address when the device reports none). One line after the
// script chose the body type in such a form:
//   await require('./robot_model_helper.cjs').fillBrandAndModel(page);
// fills the two inputs when the open dialog has them and they are empty. A form without them (changing
// the body type of an existing robot, connecting an existing robot) is left as it is.
const PRODUCT = {vendor: '示例机器人公司', model: 'X1'};

async function fillBrandAndModel(page, product = PRODUCT) {
  for (const name of ['vendor', 'model']) {
    const input = page.locator(`#dialog input[name="${name}"]`);
    if (await input.count() && !(await input.first().inputValue())) await input.first().fill(product[name]);
  }
}

module.exports = {fillBrandAndModel, PRODUCT};
