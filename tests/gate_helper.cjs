// Shared by the browser acceptance tests: the app now opens on a full-page sign-in / register screen
// (web/account.js). Registration takes the e-mailed 6-digit code; the test server runs with
// NEUXMIND_MAIL_DEV_ECHO=1 so the code comes back in the /api/register/start response. Registers (or signs in, when register:false) through that screen and waits for the app.
async function gateSignIn(page, {name, email, password, register = true}) {
  const gate = page.locator('#auth-gate');
  await gate.locator('form[data-acct-form]').waitFor();
  const want = register ? 'register' : 'login';
  if (!(await gate.locator(`form[data-acct-form="${want}"]`).count())) await gate.locator(`[data-acct-mode="${want}"]`).click();
  const f = gate.locator(`form[data-acct-form="${register ? 'register' : 'login'}"]`);
  await f.waitFor();
  if (register) await f.locator('[name=name]').fill(String(name));
  await f.locator('[name=email]').fill(String(email));
  await f.locator('[name=password]').fill(String(password));
  if (register) await f.locator('[name=password2]').fill(String(password));
  if (register) {
    const started = page.waitForResponse(r => r.url().endsWith('/api/register/start'));
    await f.locator('[type=submit]').click();
    const body = await (await started).json();
    if (!body.dev_code) throw new Error('register/start returned no dev_code: ' + JSON.stringify(body));
    const c = gate.locator('form[data-acct-form="register-code"]');
    await c.waitFor();
    await c.locator('[data-otp] input').first().fill(body.dev_code);
    await c.locator('[type=submit]').click();
  } else {
    await f.locator('[type=submit]').click();
  }
  await page.locator('#auth-gate').waitFor({state: 'hidden'});
  await page.locator('#navigation a').first().waitFor();
}
module.exports = {gateSignIn};
