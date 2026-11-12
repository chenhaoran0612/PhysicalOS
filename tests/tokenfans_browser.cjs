// Shared by the browser acceptance scripts (issue #4): the desktop runs large models only
// through TokenFans, configured on the 云平台账户 page. The fixture's TokenFans stand-in
// (tests/tokenfans_fixture.py with routes) forwards each model id to that flow's scripted model.
// cfg.tokenfans_key is a fake key, random per fixture run.

async function connectTokenFans(page, cfg, {navigate = true} = {}) {
  if (navigate) await page.goto(cfg.url + '/#cloud');
  await page.locator('#main .tkf-panel').waitFor();
  await page.locator('#main .tkf-panel [data-action="tkf-key"]').first().click();
  await page.locator('#dialog [name="api_key"]').fill(cfg.tokenfans_key);
  await page.locator('#dialog button[type=submit]').click();
  await page.waitForFunction(() => state.model_compute && state.model_compute.connected, null, {timeout: 15000});
}

// Make `model` the default. Runs started afterwards use it; a started session or a
// workflow created earlier keeps its model.
async function useTokenFansModel(page, cfg, model) {
  await page.goto(cfg.url + '/#cloud');
  const form = page.locator('#main form[data-form="tkf-default"]');
  await form.waitFor();
  await form.locator('[name="model"]').selectOption(model);
  await form.locator('button[type=submit]').click();
  await page.waitForFunction(m => state.model_compute && state.model_compute.default_model === m &&
    rows('agent_models').some(x => x.billing === 'tokenfans' && x.model === m), model, {timeout: 15000});
}

module.exports = {connectTokenFans, useTokenFansModel};
