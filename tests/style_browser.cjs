// Issue #7 acceptance: the console's own dropdown, hover states that stay in their colour family,
// and no code or JSON on screen (data shows as readable rows; code only inside a fold).
// Usage: [STYLE_BROWSER=webkit] node tests/style_browser.cjs <fixture-config.json> [screenshot-dir]
// (fixture: tests/ux01_browser_fixture.py, a fresh temporary state; the server needs NEUXMIND_MAIL_DEV_ECHO=1)
const path = require('path');
const crypto = require('crypto');
const {gateSignIn} = require('./gate_helper.cjs');
const playwright = require(process.env.BRAIN_PLAYWRIGHT_MODULE || 'playwright');
// STYLE_BROWSER=webkit runs the same checks on WebKit, the engine of the desktop app's WKWebView.
const onWebkit = process.env.STYLE_BROWSER === 'webkit';

const check = (ok, message) => { if (!ok) throw new Error(message); };

// --- colour helpers for the hover matrix -------------------------------------------------
const rgb = css => {
  const m = css.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
  return {r, g, b, a};
};
const over = (top, under) => ({
  r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1,
});
const luminance = ({r, g, b}) => {
  const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
const contrast = (a, b) => { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const distance = (a, b) => Math.hypot(a.r - b.r, a.g - b.g, a.b - b.b);

// Every button variant and context that style.css paints, on the backdrop it sits on.
const VARIANTS = [
  {name: 'primary', backdrop: '#ffffff', html: '<button class="button">主要</button>'},
  {name: 'secondary', backdrop: '#ffffff', html: '<button class="button secondary">次要</button>'},
  {name: 'danger', backdrop: '#ffffff', html: '<button class="button danger">删除</button>'},
  {name: 'danger-outline', backdrop: '#ffffff', html: '<button class="button danger-outline">停止</button>'},
  {name: 'text-link', backdrop: '#f3f7ee', html: '<button class="button text-link">查看完整流程</button>'},
  {name: 'hero', backdrop: '#f3f7ee', html: '<div class="hero"><button class="button">创建</button></div>'},
  {name: 'safety', backdrop: '#b44a42', html: '<div class="safety-banner"><button class="button">急停</button></div>'},
  {name: 'safety-secondary', backdrop: '#b44a42', html: '<div class="safety-banner"><button class="button secondary">知道了</button></div>'},
  {name: 'stop-secondary', backdrop: '#b44a42', html: '<div class="stop-not-obeyed"><button class="button secondary">知道了</button></div>'},
];

async function hoverMatrix(page) {
  await page.evaluate(variants => {
    const box = document.createElement('div');
    box.id = 'hover-matrix';
    box.style.cssText = 'position:fixed;left:0;top:0;z-index:1000;display:grid;grid-template-columns:repeat(3,auto);gap:8px;padding:8px;background:#fff';
    // Keep each context class (it is what the hover rules key on) but drop its page layout.
    const flat = 'display:block;position:static;min-height:0;margin:0;padding:0;border:0;background:none;box-shadow:none';
    const pair = html => `${html}<span> </span>${html.replace('<button ', '<button disabled ')}`;
    box.innerHTML = variants.map(v => `<div data-variant="${v.name}" style="padding:6px;background:${v.backdrop}">${pair(v.html).replace(/<div class="([^"]+)">/g, `<div class="$1" style="${flat}">`)}</div>`).join('');
    document.body.append(box);
  }, VARIANTS);
  const problems = [];
  for (const v of VARIANTS) {
    const row = page.locator(`#hover-matrix [data-variant="${v.name}"]`);
    const [live, off] = [row.locator('button:not([disabled])'), row.locator('button[disabled]')];
    const read = async el => el.evaluate(b => { const s = getComputedStyle(b); return {bg: s.backgroundColor, fg: s.color, line: s.textDecorationLine, edge: s.borderTopColor}; });
    await page.mouse.move(1400, 900);
    const rest = await read(live);
    await live.hover();
    const hot = await read(live);
    const offRest = await read(off);
    await off.hover({force: true});
    const offHot = await read(off);
    const backdrop = rgb(await row.evaluate(r => getComputedStyle(r).backgroundColor));
    const [bgRest, bgHot] = [over(rgb(rest.bg), backdrop), over(rgb(hot.bg), backdrop)];
    const ratio = contrast(over(rgb(hot.fg), bgHot), bgHot);
    const step = distance(bgRest, bgHot);
    if (ratio < 3) problems.push(`${v.name}: text contrast on hover is ${ratio.toFixed(2)} (${hot.fg} on ${hot.bg})`);
    if (step < 4 && hot.fg === rest.fg && hot.line === rest.line && hot.edge === rest.edge) problems.push(`${v.name}: no visible hover feedback`);
    if (step > 40) problems.push(`${v.name}: hover jumps from ${rest.bg} to ${hot.bg} (distance ${step.toFixed(0)})`);
    if (offRest.bg !== offHot.bg || offRest.fg !== offHot.fg) problems.push(`${v.name}: disabled button changes on hover (${offRest.bg} → ${offHot.bg})`);
  }
  await page.evaluate(() => document.getElementById('hover-matrix').remove());
  check(!problems.length, 'Hover states:\n  ' + problems.join('\n  '));
}

async function dropdownInDialog(page, shots) {
  await page.locator('[data-action="robot-add"]').first().click();
  const dlg = page.locator('#dialog');
  const native = dlg.locator('select[name=family]');
  const trigger = dlg.locator('select[name=family] + .nx-select');
  await trigger.waitFor();
  check(await native.evaluate(s => s.classList.contains('nx-native') && s.getBoundingClientRect().width <= 1), 'native select is not hidden');
  check((await trigger.getAttribute('role')) === 'combobox', 'trigger is not a combobox');
  const label = await native.evaluate(s => s.selectedOptions[0].textContent.trim());
  check((await trigger.innerText()).trim() === label, `trigger shows "${await trigger.innerText()}", select holds "${label}"`);
  if (shots) await page.screenshot({path: path.join(shots, 'dropdown-closed.png')});

  // Pointer: open, pick, change event reaches the app's listeners.
  await page.evaluate(() => { window.__changes = 0; document.addEventListener('change', e => { if (e.target.name === 'family') window.__changes++; }); });
  await trigger.click();
  const list = page.locator('.nx-select-list');
  await list.waitFor();
  check((await trigger.getAttribute('aria-expanded')) === 'true', 'aria-expanded not set');
  check((await list.locator('[role=option]').count()) === (await native.locator('option').count()), 'list and select disagree on options');
  await list.locator('[role=option]', {hasText: '四足机器人'}).hover();
  if (shots) await page.screenshot({path: path.join(shots, 'dropdown-open.png')});
  await list.locator('[role=option]', {hasText: '四足机器人'}).click();
  await list.waitFor({state: 'detached'});
  check((await native.inputValue()) === 'quadruped', 'picking an option did not set the select');
  check((await page.evaluate(() => window.__changes)) === 1, 'change event not fired exactly once');
  check((await trigger.innerText()).trim() === '四足机器人', 'trigger did not follow the pick');

  // Keyboard: arrows move, Enter picks, Escape closes the list but not the dialog.
  await trigger.focus();
  await page.keyboard.press('ArrowDown');
  await list.waitFor();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await list.waitFor({state: 'detached'});
  check((await native.inputValue()) === 'humanoid', `keyboard pick gave ${await native.inputValue()}`);
  await page.keyboard.press('ArrowDown');
  await list.waitFor();
  await page.keyboard.press('Escape');
  await list.waitFor({state: 'detached'});
  check(await dlg.evaluate(d => d.open), 'Escape on the list closed the dialog');
  // Safari/WKWebView: clicking a <button> does not focus it. Open by a click that leaves focus elsewhere.
  await page.evaluate(() => { document.activeElement?.blur(); document.querySelector('#dialog select[name=family] + .nx-select').dispatchEvent(new MouseEvent('click', {bubbles: true})); });
  await list.waitFor();
  check(await trigger.evaluate(t => document.activeElement === t), 'opening the list did not focus the trigger');
  await page.keyboard.press('Escape');
  await list.waitFor({state: 'detached'});
  check(await dlg.evaluate(d => d.open), 'Escape after an unfocused click closed the dialog');
  check(await trigger.evaluate(t => document.activeElement === t), 'focus did not return to the trigger');

  // Code paths: selectOption (tests) and select.value = … (app code) both show on the trigger.
  await native.selectOption('aerial');
  check((await trigger.innerText()).trim() === '无人机', 'selectOption not mirrored');
  await native.evaluate(s => { s.value = 'mobile'; });
  check((await trigger.innerText()).trim() === '移动机器人', 'programmatic value not mirrored');
  await page.locator('#dialog [data-action=close]').first().click();
}

async function requiredAndMultiple(page) {
  await page.evaluate(() => {
    const f = document.createElement('form');
    f.id = 'dropdown-probe';
    f.style.cssText = 'position:fixed;left:20px;top:20px;width:320px;z-index:1000;background:#fff;padding:12px';
    f.innerHTML = '<label class="field">必选项<select name="need" required><option value="">请选择</option><option value="a">甲</option></select></label>'
      + '<label class="field">多选<select name="many" multiple><option value="x">X</option><option value="y">Y</option><option value="z">Z</option></select></label>'
      + '<button type="submit">提交</button>';
    f.addEventListener('submit', e => { e.preventDefault(); window.__submitted = true; });
    document.body.append(f);
  });
  const probe = page.locator('#dropdown-probe');
  await probe.locator('button[type=submit]').click();
  check(!(await page.evaluate(() => window.__submitted)), 'form with an empty required select was submitted');
  await probe.locator('.nx-select-error').waitFor();
  // The browser moves focus after all `invalid` events have run, so wait for it rather than read it once.
  const marked = await page.waitForFunction(() => {
    const t = document.querySelector('#dropdown-probe select[name=need] + .nx-select');
    return t && t.classList.contains('is-invalid') && document.activeElement === t;
  }, null, {timeout: 3000}).then(() => true, () => false);
  check(marked, 'invalid select not marked/focused');
  await probe.locator('select[name=need] + .nx-select').click();
  await page.locator('.nx-select-list [role=option]', {hasText: '甲'}).click();
  check(!(await probe.locator('.nx-select-error').count()), 'error stays after a valid pick');

  const many = probe.locator('select[name=many] + .nx-select');
  await many.click();
  const list = page.locator('.nx-select-list');
  check((await list.getAttribute('aria-multiselectable')) === 'true', 'multiple select list not multiselectable');
  await list.locator('[role=option]', {hasText: 'X'}).click();
  await list.locator('[role=option]', {hasText: 'Z'}).click();
  check(await list.isVisible(), 'multiple list closed after one pick');
  await page.keyboard.press('Escape');
  const chosen = await probe.locator('select[name=many]').evaluate(s => [...s.selectedOptions].map(o => o.value).join(','));
  check(chosen === 'x,z', `multiple select holds ${chosen}`);
  check((await many.innerText()).trim() === 'X、Z', `multiple trigger shows ${await many.innerText()}`);
  await probe.locator('button[type=submit]').click();
  check(await page.evaluate(() => window.__submitted), 'valid form did not submit');
  await page.evaluate(() => document.getElementById('dropdown-probe').remove());
}

// --- nothing on screen is code -----------------------------------------------------------
// Text the user can see (folds that are still closed do not count) must hold no JSON syntax,
// code fences, tracebacks or <pre> blocks.
async function codeOnScreen(page) {
  return page.evaluate(() => {
    const folded = el => { for (let d = el.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) { if (!d.open && el.closest('summary')?.parentElement !== d) return true; } return false; };
    const found = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode, el = node.parentElement, text = node.textContent.trim();
      if (!text || !el || el.closest('script,style,textarea,select,option,.nx-native,[data-keep-code]')) continue;
      if (!el.getClientRects().length || folded(el)) continue;
      if (/[{[]\s*"[^"]*"\s*:|"\s*:\s*[{["\d]|```|Traceback \(most recent call last\)/.test(text)) found.push(text.slice(0, 120));
      else if (el.closest('pre')) found.push('<pre> ' + text.slice(0, 80));
    }
    return found;
  });
}

async function readableViews(page, shots) {
  const fill = () => page.evaluate(() => dialog('数据展示检查', '用页面自己的展示函数渲染', '<div class="dialog-body">'
    + json({exit_code: 0, ok: true, host: 'robot.local', tags: ['camera', 'lidar'], created_at: '2026-11-12T10:20:30Z', detail: {speed: 0.5, unit: 'm/s'}})
    + '<h3>实际命令</h3><pre class="json-view">sudo ls /dev\nlsusb -t</pre>'
    + '<div class="engine-transcript">'
    + '<div class="log-item"><span class="tag">命令</span><p>{"type":"commandExecution","command":"ls /dev","status":"completed","exitCode":0}</p></div>'
    + '<div class="log-item"><span class="tag">命令输出</span><p>total 8\ndrwxr-xr-x 2 root root 4096 video0\n-rw-r--r-- 1 root root 12 notes.txt</p></div>'
    + '<div class="log-item"><span class="tag">工程助手</span><p>## 检查结果\n- 相机 **已识别**\n\n先检查驱动：\n```bash\nlsmod | grep uvc\n```\n然后重启服务。</p></div>'
    + '</div>'
    + hwCommand('sudo modprobe uvcvideo\nsudo systemctl restart camera') + '</div>'));
  await fill();
  const dlg = page.locator('#dialog');
  await dlg.locator('.nx-readable').first().waitFor();
  const seen = await codeOnScreen(page);
  check(!seen.length, 'code visible in the data dialog:\n  ' + seen.join('\n  '));
  check(await dlg.getByText('退出码', {exact: true}).first().isVisible(), 'JSON keys are not shown as labels');
  check(await dlg.getByText('执行命令', {exact: true}).first().isVisible(), 'transcript item has no readable headline');
  check(await dlg.getByText(/然后重启服务/).isVisible(), 'prose around a code fence was lost');
  check(await dlg.locator('.nx-h', {hasText: '检查结果'}).isVisible(), 'markdown heading not formatted');
  check(await dlg.locator('details.nx-fold', {hasText: '展开查看命令输出'}).count() === 1, 'raw command output is not folded');
  if (shots) await page.screenshot({path: path.join(shots, 'readable-dialog.png')});
  const fold = dlg.locator('[data-was~="hw-command"] details.nx-fold');
  await fold.locator('summary').click();
  check(await fold.locator('.nx-raw').isVisible() && (await fold.locator('.nx-raw').innerText()).includes('modprobe uvcvideo'), 'fold does not open to the exact command');
  await fill(); // the app re-renders dialogs while polling: an opened fold stays open
  check(await dlg.locator('[data-was~="hw-command"] details.nx-fold[open]').count() === 1, 'opened fold closed again on re-render');
  check(await dlg.locator('details.nx-fold[open]').count() === 1, 'other folds opened too');
  const stream = n => page.evaluate(k => dialog('实时输出', '', '<div class="dialog-body"><h3>采集内容</h3><pre class="json-view">' + Array.from({length: k}, (_, i) => `ros2 topic echo /scan --once  # ${i}`).join('\n') + '</pre></div>'), n);
  await stream(3);
  await dlg.locator('details.nx-fold > summary').first().click();
  for (const n of [4, 9]) {
    await stream(n);
    check(await dlg.locator('details.nx-fold[open]').count() === 1, `fold over live output closed when it grew to ${n} lines`);
  }
  await page.locator('#dialog [data-action=close]').first().click();
}

async function pagesHaveNoCode(page, base) {
  const pages = ['overview', 'robots', 'agents', 'skills', 'simulation', 'harness', 'deployment', 'models', 'training', 'datasets', 'extensions', 'services', 'resources', 'billing', 'cloud'];
  const problems = [];
  for (const name of pages) {
    await page.goto(`${base}/#${name}`);
    await page.locator('#main .page-head, #main h1').first().waitFor();
    const seen = await codeOnScreen(page);
    if (seen.length) problems.push(`${name}: ${seen.join(' | ')}`);
  }
  check(!problems.length, 'code visible on pages:\n  ' + problems.join('\n  '));
}

(async () => {
  const info = require(path.resolve(process.argv[2]));
  const shots = process.argv[3] || '';
  const browser = onWebkit ? await playwright.webkit.launch() : await playwright.chromium.launch({executablePath: process.env.BRAIN_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  await page.emulateMedia({reducedMotion: 'reduce'});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(info.url);
  await gateSignIn(page, {name: '样式验收', email: `style-${Date.now()}@example.test`, password: 'Aa1' + crypto.randomBytes(9).toString('hex')});
  await page.locator('[data-action="journey-start"]').first().click();
  await page.locator('#dialog [name=name]').fill('样式验收项目');
  await page.locator('#dialog [name=objective]').fill('检查下拉框、悬停和展示内容');
  await page.locator('#dialog button[type=submit]').click();
  await page.locator('#dialog').waitFor({state: 'hidden'});
  await page.goto(info.url + '/#robots');
  await dropdownInDialog(page, shots);
  await requiredAndMultiple(page);
  await hoverMatrix(page);
  await readableViews(page, shots);
  await pagesHaveNoCode(page, info.url);
  check(!errors.length, 'page errors: ' + errors.join(' | '));
  console.log(`style browser check passed (${onWebkit ? 'webkit' : 'chromium'})`);
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
