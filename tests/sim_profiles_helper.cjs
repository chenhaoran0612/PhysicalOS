'use strict';
// Issue #54: 「添加仿真机器人」 chooses a type, a brand and a model. A browser script that used to tick one
// of the two old kinds calls this on the open dialog instead:
//   await require('./sim_profiles_helper.cjs').pickSimProfile(page, 'quadruped');
// 'quadruped' and 'arm' are the two example robots the app always had; any other value is a profile id.
const EXAMPLES = {quadruped: 'neuxmind-quadruped', arm: 'neuxmind-arm'};

async function pickSimProfile(page, which) {
  const profile = EXAMPLES[which] || which;
  const form = page.locator('#dialog form[data-form="sim-add"]');
  await form.waitFor();
  const found = await page.evaluate(id => (state.sim_catalog?.profiles || []).find(p => p.id === id) || null, profile);
  if (!found) throw new Error('这个安装的清单里没有仿真型号：' + profile);
  if (!found.available) throw new Error(`仿真型号 ${profile} 现在不能添加：${found.problem}`);
  await form.locator(`input[name="sim_type"][value="${found.type}"]`).check({force: true});
  await form.locator('select[name="sim_vendor"]').selectOption(found.vendor);
  await form.locator('select[name="profile_id"]').selectOption(profile);
  return found;
}

module.exports = {pickSimProfile, EXAMPLES};
