// Issue #14 browser acceptance: the layout of the SSH connection form, as "通过 SSH 接入本体" and as
// "配置连接". No block of the form lies on another one or sticks to it, none is pulled towards its
// neighbour by a negative margin, nothing sticks out sideways, and every note of "配置连接" stands next
// to what it explains: the name the device had on the jump
// host right under the address it was resolved to, and what decides whether the fingerprints are
// read again together at the end of the form, above the button it changes.
// Fixture: tests/ssh_jump_browser_fixture.py (local SSH protocol fixtures, no real machine).
// Usage: node tests/ssh_form_layout_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn} = require('./gate_helper.cjs');
const engine = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright')[process.env.BRAIN_BROWSER || 'chromium'];
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DIRECT_ROBOT = '直连的机器人', NAMED_ROBOT = '跳板机上叫 dog 的机器人';
const WINDOW = {width: 1390, height: 930}, NARROW = {width: 560, height: 900};
const MIN_GAP = 8;                  // px between a block and the one above it
const MIN_BLOCKS = 6;               // the emptiest state of the form (a direct connection) shows more blocks than this
// What a reader takes for one block of the form.
const BLOCKS = '.notice,.hardware-confirm,.field,.inline-note,.step-editor,details,summary,.form-error,#ssh-jump>p';
// Blocks the states of the form must show (see layoutProblems).
const ALWAYS = ['details.page-purpose', 'details.page-purpose>summary'];
const JUMP = ['#ssh-jump>.inline-note', '.ssh-hop', '#ssh-jump>p:not(.inline-note)', '#ssh-jump>.hardware-confirm'];
const OPEN_FOLD = ['details.page-purpose[open]>.field'];
const EDIT = ['.dialog-body>.notice', '.dialog-body>.hardware-confirm', '.dialog-body>.inline-note'];
const NAMED = ['#ssh-edit-alias>.notice'];
const SECOND_HOP = ['.ssh-hop[data-hop="2"]>label.inline-note'];
let failed = async () => {};

// Runs in the page: the blocks of the open SSH form that overlap, touch, are pulled by a negative margin or
// leave the form sideways. `required`: blocks this state of the form must show, so that a check of a form
// whose markup changed does not pass by measuring nothing.
function layoutProblems([selector, minGap, minBlocks, required]) {
  const body = document.querySelector('#dialog form[data-form="engine-ssh"] .dialog-body');
  if (!body) return ['The SSH form is not open'];
  const shown = el => {
    if (!el.getClientRects().length) return false;
    const fold = el.parentElement.closest('details:not([open])');      // of a closed fold only the summary shows
    return !fold || (el.tagName === 'SUMMARY' && el.parentElement === fold);
  };
  const label = el => `${el.id ? '#' + el.id : el.className || el.tagName.toLowerCase()} “${el.textContent.trim().slice(0, 14)}”`;
  const boxes = [...body.querySelectorAll(selector)].filter(shown).map(el => ({el, rect: el.getBoundingClientRect(), name: label(el)}));
  const related = (a, b) => a.el.contains(b.el) || b.el.contains(a.el);
  const across = (a, b) => Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
  const down = (a, b) => Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
  const frame = body.getBoundingClientRect(), style = getComputedStyle(body), problems = [];
  const left = frame.left + parseFloat(style.paddingLeft), right = frame.right - parseFloat(style.paddingRight);
  if (boxes.length < minBlocks) problems.push(`Only ${boxes.length} blocks of the form were found`);
  for (const wanted of required) {
    if (!boxes.some(box => box.el.matches(wanted))) problems.push(`${wanted} is not shown`);
  }
  for (const box of boxes) {
    if (box.rect.left < left - 0.5 || box.rect.right > right + 0.5) problems.push(`${box.name} sticks out of the form sideways`);
    const margins = getComputedStyle(box.el);
    if (parseFloat(margins.marginTop) < 0 || parseFloat(margins.marginBottom) < 0) problems.push(`${box.name} has a negative margin`);
  }
  boxes.forEach((a, index) => boxes.slice(index + 1).forEach(b => {
    if (!related(a, b) && across(a, b) > 0.5 && down(a, b) > 0.5) problems.push(`${a.name} and ${b.name} overlap by ${down(a, b).toFixed(1)}px`);
  }));
  for (const box of boxes) {
    const above = boxes.filter(other => other !== box && !related(other, box) && across(other, box) > 0.5 &&
                                        other.rect.bottom <= box.rect.top + 0.5);
    if (!above.length) continue;
    const nearest = above.reduce((found, other) => (other.rect.bottom > found.rect.bottom ? other : found));
    const gap = box.rect.top - nearest.rect.bottom;
    if (gap < minGap - 0.5) problems.push(`${box.name} is only ${gap.toFixed(1)}px below ${nearest.name}`);
  }
  const dialog = document.querySelector('#dialog');
  if (dialog.scrollWidth > dialog.clientWidth + 1) problems.push('The dialog scrolls sideways');
  return problems;
}

// Runs in the page: where the notes of "配置连接" stand.
function notePlaces() {
  const form = document.querySelector('#dialog form[data-form="engine-ssh"]');
  const follows = (first, second) => Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
  const hostRow = form.querySelector('[name=host]').closest('.field-row'), username = form.querySelector('[name=username]').closest('.field');
  const alias = form.querySelector('#ssh-edit-alias'), route = form.querySelector('#ssh-edit-route');
  const rescan = form.querySelector('[name=rescan]').closest('label'), fold = form.querySelector('details.page-purpose');
  const submit = form.querySelector('[type=submit]');
  return {
    alias: Boolean(alias),
    aliasUnderHost: Boolean(alias) && alias.previousElementSibling === hostRow && alias.nextElementSibling === username &&
      alias.getBoundingClientRect().top >= form.querySelector('[name=host]').getBoundingClientRect().bottom,
    aliasNotes: form.querySelectorAll('#ssh-edit-alias').length,
    fingerprintsTogether: route.nextElementSibling === rescan,
    fingerprintsLast: follows(fold, route) && follows(rescan, submit) &&
      [...form.querySelectorAll('.field')].every(field => follows(field, route)),
  };
}

(async () => {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2] || '/tmp/neuxmind-ssh-jump-ui.json'));
  const shots = process.argv[3] || 'artifacts';
  fs.mkdirSync(shots, {recursive: true});
  const browser = await engine.launch(engine.name() === 'chromium' ? {executablePath: process.env.BRAIN_CHROMIUM_PATH} : {});
  const page = await browser.newPage({viewport: WINDOW}), errors = [];
  require('./robots_page_helper.cjs').throughRobotsPage(page);   // issue #41: opens 更多功能 / 添加机器人 before a click that needs it
  page.on('pageerror', e => errors.push(e.message));
  const shot = name => page.screenshot({path: path.join(shots, `ssh-form-layout-${name}.png`)});
  failed = async () => { await shot('failure').catch(() => {}); console.error('page errors:', errors); };

  const dialog = page.locator('#dialog');
  const control = name => dialog.locator(`[name="${name}"]`);
  const fill = (name, text) => control(name).fill(String(text));
  const click = async action => {
    const inDialog = page.locator(`#dialog[open] [data-action="${action}"]`);
    await (await inDialog.count() ? inDialog : page.locator(`[data-action="${action}"]`)).first().click();
  };
  const settled = () => page.waitForFunction(() => {
    const button = document.querySelector('#dialog button[type=submit]');
    return !button || !button.disabled;
  });
  const submit = async () => {
    await dialog.locator('button[type=submit]').click();
    await settled();
    const error = (await page.locator('#dialog .form-error').allTextContents()).filter(Boolean).join(';');
    if (error) throw Error(error);
  };
  const confirmFingerprint = async title => {
    await dialog.getByText(title, {exact: true}).waitFor();
    await dialog.locator('input[type=checkbox]').check();
    await submit();
  };
  const must = (condition, message) => { if (!condition) throw Error(message); };
  const card = name => page.locator('article.card', {hasText: name});
  const connected = async () => {
    await dialog.getByText('硬件清单', {exact: true}).waitFor({timeout: 60000});
    await click('close');
  };
  // A second `goto` to the address already shown reloads the page on WebKit and cuts a poll off: read the state again instead.
  const robotsPage = async () => {
    if (page.url().endsWith('/#robots')) await page.evaluate(() => refresh(true));
    else await page.goto(cfg.url + '/#robots');
  };
  const openEdit = async name => {
    await robotsPage();
    await card(name).waitFor();
    await card(name).locator('[data-action="engine-ssh-edit"]').click();
    await dialog.getByText('配置连接', {exact: true}).waitFor();
  };
  // The form as it stands now must be laid out cleanly, in the window of the desktop app and in a narrow one.
  const laidOut = async (state, required = []) => {
    for (const [size, viewport] of [['window', WINDOW], ['narrow', NARROW]]) {
      await page.setViewportSize(viewport);
      const problems = await page.evaluate(layoutProblems, [BLOCKS, MIN_GAP, MIN_BLOCKS, [...ALWAYS, ...required]]);
      if (problems.length) {
        await shot(`${state}-${size}-problem`);
        throw Error(`${state} (${size} window): ${problems.join('; ')}`);
      }
    }
    await page.setViewportSize(WINDOW);
  };
  const scrollTo = selector => dialog.locator(selector).first().evaluate(el => el.scrollIntoView({block: 'center'}));
  // While `investigated` names a robot, the state the page reads holds a running investigation of it: the
  // fixture has no run to offer, and "配置连接" only has to name one (issue #12).
  let investigated = null;
  await page.route('**/api/state', async route => {
    const response = await route.fetch();
    if (!investigated) return route.fulfill({response});
    const state = await response.json(), robot = state.robots.find(r => r.name === investigated);
    const at = new Date().toISOString();
    state.agent_sessions.push({id: 'agent-session-layout-check', project_id: robot.project_id, robot_id: robot.id,
                               connection_id: robot.connection_id, mode: 'discovery', status: 'running', name: `${robot.name} · 本体调查`,
                               model: 'fixture-model', model_id: 'agent-model-fixture', billing: 'tokenfans', objective: '调查本体',
                               usage: {}, events: [], max_tokens: null, max_minutes: null, created_at: at, updated_at: at,
                               thread_id: null, turn_id: null, pending_question: null});
    return route.fulfill({response, json: state});
  });
  const result = {};

  await page.goto(cfg.url);
  // a throwaway account on the local fixture; the password is made up on every run
  await gateSignIn(page, {name: '表单排版验收', email: `layout-${crypto.randomBytes(4).toString('hex')}@example.test`,
                          password: 'layout1-' + crypto.randomBytes(8).toString('hex')});
  await click('journey-start');
  await fill('name', '连接表单排版'); await fill('objective', '检查连接表单的排版');
  await submit();

  // 1 · A new connection: direct, then through a jump host with the device given by a name on it.
  await robotsPage();
  await click('engine-ssh');
  await laidOut('new connection, direct');
  await fill('name', DIRECT_ROBOT); await fill('host', '127.0.0.1'); await fill('port', cfg.target_port);
  await dialog.locator('[name=family]').selectOption('quadruped');
  await fill('username', cfg.target_user); await fill('password', cfg.target_password);
  await dialog.locator('details.page-purpose summary').click();
  await laidOut('new connection, the advanced settings open', OPEN_FOLD);
  await submit();
  await confirmFingerprint('确认 SSH 主机指纹');
  await connected();

  await robotsPage();
  await click('engine-ssh');
  const command = control('ssh_command');
  await command.fill(`ssh -tt -p ${cfg.jump_port} ${cfg.jump_user}@127.0.0.1 'ssh -tt dog'`);
  await command.blur();                              // applies the command once, as leaving the field does
  must(await control('resolve_on_jump').isChecked(), 'The nested command did not mark the device as a name on the jump host');
  await fill('name', NAMED_ROBOT);
  await dialog.locator('[name=family]').selectOption('quadruped');
  await control('auth').selectOption('password'); await control('jump_auth').selectOption('password');
  await fill('password', cfg.target_password); await fill('jump_password', cfg.jump_password);
  await laidOut('new connection through a jump host', JUMP);
  await scrollTo('[name=resolve_on_jump]');
  await shot('1-new-jump');
  await dialog.locator('[data-ssh-hop="add"]').click();
  await dialog.locator('[data-hop="2"]').waitFor();
  await laidOut('new connection through two jump hosts', [...JUMP, ...SECOND_HOP]);
  await dialog.locator('[data-hop="2"] [data-ssh-hop="remove"]').click();
  await dialog.locator('[data-hop="2"]').waitFor({state: 'detached'});
  await submit();
  await confirmFingerprint('第 1 步：确认跳板机指纹');
  await confirmFingerprint('第 2 步：确认目标机指纹');
  await connected();
  result.newConnectionLaidOut = true;

  // 2 · "配置连接" of a robot that was connected directly: no note about a name, the fingerprint notes last.
  await openEdit(DIRECT_ROBOT);
  let places = await page.evaluate(notePlaces);
  must(!places.alias, 'A direct connection shows the note about a name on the jump host');
  must(places.fingerprintsTogether && places.fingerprintsLast, 'Fingerprint notes of a direct connection: ' + JSON.stringify(places));
  await laidOut('edit, direct', EDIT);
  await click('close');

  // 3 · "配置连接" of the robot the jump host knows by name, in every state the form takes.
  await openEdit(NAMED_ROBOT);
  places = await page.evaluate(notePlaces);
  must(places.aliasUnderHost && places.aliasNotes === 1, 'The note about the name is not right under the address: ' + JSON.stringify(places));
  must(places.fingerprintsTogether && places.fingerprintsLast, 'Fingerprint notes are not together at the end: ' + JSON.stringify(places));
  must((await dialog.locator('#ssh-edit-alias').innerText()).includes('dog'), 'The note does not name what the device is called on the jump host');
  must(await page.locator('#ssh-edit-route').isHidden(), 'The route note shows although nothing was changed');
  await laidOut('edit, as opened', [...EDIT, ...JUMP, ...NAMED]);
  await scrollTo('#ssh-edit-alias');
  await shot('2-edit-name-note');

  await control('resolve_on_jump').check();          // as in the issue's screenshot: resolve the name again, read the fingerprints again
  await page.locator('#ssh-edit-route').waitFor({state: 'visible'});
  await control('rescan').check();
  await laidOut('edit, fingerprints to be read again', [...EDIT, ...JUMP, ...NAMED, '#ssh-edit-route>.notice']);
  await scrollTo('[name=resolve_on_jump]');
  await shot('3-edit-as-in-issue');
  await scrollTo('[name=rescan]');
  await shot('4-edit-fingerprint-notes');

  await dialog.locator('details.page-purpose summary').click();
  await laidOut('edit, the advanced settings open', [...EDIT, ...JUMP, ...NAMED, ...OPEN_FOLD]);
  await dialog.locator('[data-ssh-hop="add"]').click();
  await dialog.locator('[data-hop="2"]').waitFor();
  await dialog.locator('[data-hop="2"] [name=jump2_resolve]').check();
  await laidOut('edit, two jump hosts', [...EDIT, ...JUMP, ...NAMED, ...OPEN_FOLD, ...SECOND_HOP]);
  await scrollTo('[data-hop="2"] [name=jump2_resolve]');
  await shot('5-edit-two-hops');
  places = await page.evaluate(notePlaces);
  must(places.aliasUnderHost && places.aliasNotes === 1 && places.fingerprintsTogether && places.fingerprintsLast,
       'The notes moved while the form was edited: ' + JSON.stringify(places));

  await control('route').selectOption('direct');
  await page.locator('#ssh-jump').waitFor({state: 'hidden'});
  await laidOut('edit, changed to a direct connection', [...EDIT, ...NAMED, ...OPEN_FOLD]);
  await click('close');

  // 4 · The same robot while it is being investigated: the form names the run above its other notes.
  investigated = NAMED_ROBOT;
  await openEdit(NAMED_ROBOT);
  await dialog.locator('#ssh-edit-running').waitFor();
  await laidOut('edit, while an investigation runs', [...EDIT, ...JUMP, ...NAMED, '#ssh-edit-running>.notice']);
  await shot('6-edit-while-running');
  await click('close');
  investigated = null;
  await page.evaluate(() => refresh(true));
  result.editLaidOut = true;
  result.notesNextToWhatTheyExplain = true;

  must(!errors.length, 'Page errors: ' + errors.join('; '));
  console.log(JSON.stringify({engine: engine.name(), ...result, pageErrors: errors.length}));
  await browser.close();
})().catch(async error => {
  await failed();
  console.error(error);
  process.exit(1);
});
