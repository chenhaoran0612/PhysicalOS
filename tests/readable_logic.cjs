// Pure-logic checks of web/readable.js (issue #7): machine data becomes readable text, never code.
//   node tests/readable_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/readable.js', 'utf8'), sandbox);
const R = sandbox.nxReadable;
const plain = v => JSON.parse(JSON.stringify(v));
// Visible text: drop collapsed <details> bodies (code lives only there) and all tags.
const visible = html => html
  .replace(/<details[^>]*>\s*<summary[^>]*>([\s\S]*?)<\/summary>[\s\S]*?<\/details>/g, ' $1 ')
  .replace(/<[^>]+>/g, ' ').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const noCode = (html, what) => {
  const text = visible(html);
  assert.doesNotMatch(text, /[{[]\s*"|"\s*:\s*["{[\d]|":/, `${what}: JSON syntax visible -> ${text}`);
  assert.doesNotMatch(text, /```/, `${what}: code fence visible`);
};
let checks = 0;
const ok = () => { checks += 1; };

// parseLoose: JSON, truncated JSON, Python reprs; plain text is not data.
assert.deepEqual(plain(R.parseLoose('{"a":1,"b":[true,null]}')), {value: {a: 1, b: [true, null]}, truncated: false}); ok();
assert.deepEqual(plain(R.parseLoose('{"type":"commandExecution","command":"ls -la","status":"compl')),
  {value: {type: 'commandExecution', command: 'ls -la', status: 'compl'}, truncated: true}); ok();
assert.deepEqual(plain(R.parseLoose('{"a":1,"b')), {value: {a: 1}, truncated: true}); ok();
assert.deepEqual(plain(R.parseLoose('[1,2')), {value: [1, 2], truncated: true}); ok();
assert.deepEqual(plain(R.parseLoose("{'error': 'boom', 'ok': False, 'x': None, 'n': [1, 2]}")), {value: {error: 'boom', ok: false, x: null, n: [1, 2]}, truncated: false}); ok();
assert.equal(R.parseLoose('hello world'), null); ok();
assert.equal(R.parseLoose('42'), null, 'bare scalars are text, not data'); ok();
assert.equal(R.parseLoose('{not json at all'), null); ok();

// classify
assert.equal(R.classify('{"a":1}'), 'json'); ok();
assert.equal(R.classify('Traceback (most recent call last):\n  File "x.py", line 3, in run\nValueError: 速度超出范围'), 'traceback'); ok();
assert.equal(R.classify('sudo apt-get install -y ros-humble-desktop && ls /opt/ros'), 'code'); ok();
assert.equal(R.classify('diff --git a/x.py b/x.py\n@@ -1,2 +1,2 @@\n-a = 1\n+a = 2'), 'code'); ok();
assert.equal(R.classify('已经读取设备信息，接下来检查相机驱动是否加载。'), 'prose'); ok();
assert.equal(R.classify('The camera driver is loaded and the topic publishes at 30 Hz.'), 'prose'); ok();
assert.equal(R.classify('  '), 'empty'); ok();

// renderValue: labels, values, escaping, no JSON syntax on screen.
{
  const html = R.renderValue({exit_code: 0, ok: true, error: null, host: '<img src=x>', created_at: '2026-11-12T10:20:30Z', tags: ['camera', 'lidar']});
  for (const label of ['退出码', '成功', '错误', '主机']) assert.ok(html.includes(label), `label ${label} missing: ${html}`);
  assert.ok(html.includes('是'), 'true is 是'); assert.ok(html.includes('—'), 'null is —');
  assert.ok(html.includes('&lt;img src=x&gt;') && !html.includes('<img'), 'values are escaped');
  assert.ok(!visible(html).includes('T10:20:30Z'), 'ISO time is shown as a local time');
  assert.ok(html.includes('nx-chip'), 'short lists are chips');
  noCode(html, 'flat object'); ok();
}
{
  const html = R.renderValue({type: 'commandExecution', status: 'completed', exitCode: 0, command: 'ls -la /dev', aggregatedOutput: 'video0\nvideo1'});
  assert.ok(html.includes('执行命令') && html.includes('已完成'), 'typed record gets a headline');
  assert.ok(/<details[^>]*>\s*<summary[^>]*>[^<]*命令/.test(html), 'command is folded away');
  assert.ok(!visible(html).includes('ls -la'), 'command text is not visible by default');
  noCode(html, 'command record'); ok();
}
{
  const deep = {a: {b: {c: {d: {e: 1}}}}};
  const html = R.renderValue(deep);
  assert.ok(visible(html).includes('个字段'), 'deep nesting is summarised');
  noCode(html, 'deep'); ok();
}
{
  const many = Array.from({length: 30}, (_, i) => ({name: `设备 ${i}`, ok: i % 2 === 0}));
  const html = R.renderValue(many);
  assert.ok(visible(html).includes('设备 0') && visible(html).includes('还有'), 'long lists show the first items and a count');
  noCode(html, 'list of records'); ok();
}
assert.ok(visible(R.renderValue({})).includes('无') && visible(R.renderValue([])).includes('无')); ok();
assert.ok(R.renderValue({someUnknownKey: 1}).includes('Some unknown key'), 'unknown keys are humanised'); ok();
assert.ok(visible(R.renderValue({note: 'x'.repeat(900)})).includes('展开'), 'long strings fold'); ok();

// renderText: picks the right view for any string.
{
  const html = R.renderText('{"fingerprint":"SHA256:abc","key_type":"ssh-ed25519"}');
  assert.ok(html.includes('指纹') && html.includes('密钥类型')); noCode(html, 'fingerprint'); ok();
}
{
  const html = R.renderText('{"type":"fileChange","changes":[{"path":"adapter.py","kind":"add","diff":"+import os"}],"stat');
  assert.ok(html.includes('修改文件') && html.includes('adapter.py'), 'truncated item still readable');
  assert.ok(visible(html).includes('内容不完整'), 'truncation is said'); noCode(html, 'truncated item'); ok();
}
{
  const html = R.renderText('先检查驱动：\n```bash\nlsmod | grep uvc\n```\n然后重启服务。');
  assert.ok(visible(html).includes('先检查驱动') && visible(html).includes('然后重启服务'));
  assert.ok(!visible(html).includes('lsmod'), 'fenced code folded'); noCode(html, 'prose with fence'); ok();
}
{
  const html = R.renderText('Traceback (most recent call last):\n  File "x.py", line 3, in run\nValueError: 速度超出范围');
  assert.ok(visible(html).includes('速度超出范围'), 'traceback shows its message');
  assert.ok(!visible(html).includes('File "x.py"'), 'stack frames folded'); ok();
}
{
  const html = R.renderText('sudo systemctl restart neuxmind-runtime\njournalctl -u neuxmind-runtime -n 50');
  assert.ok(!visible(html).includes('systemctl'), 'code folded'); assert.ok(visible(html).includes('2 行'), 'line count shown'); ok();
}
assert.ok(R.renderText('请稍候，<b>正在</b>处理').includes('&lt;b&gt;'), 'prose is escaped'); ok();
{
  const html = R.renderText('设备返回 `ok`，速度 **0.5 米/秒**');
  assert.ok(!html.includes('`') && html.includes('<b>0.5 米/秒</b>'), 'inline marks become plain emphasis'); ok();
}

// Markdown reports (LLM / adaptation reports) read as formatted text, not markup.
{
  const md = '## 可用能力\n- 底盘移动\n- 相机 **已验证**\n\n| 接口 | 状态 |\n|---|---|\n| 移动 | 可用 |\n| 机械臂 | 未实现 |\n\n> 需现场验证';
  assert.equal(R.classify(md), 'prose'); ok();
  const html = R.renderText(md);
  const text = visible(html);
  assert.doesNotMatch(text, /(^|\s)##\s|\|---|\|\s*接口\s*\||^\s*-\s/m, `markdown syntax visible: ${text}`);
  assert.ok(html.includes('<table') && text.includes('机械臂') && text.includes('未实现'), 'table becomes a table');
  assert.ok(text.includes('可用能力') && text.includes('• 底盘移动') && html.includes('<b>已验证</b>') && text.includes('需现场验证')); ok();
}
assert.equal(R.classify('| name | state |\n|---|---|\n| arm | ready |'), 'prose', 'markdown tables are not code'); ok();
assert.ok(R.renderValue({field_acceptance: false}).includes('现场验收'), 'adaptation report keys are labelled'); ok();

assert.ok(visible(R.renderValue({schema: 'conformance.report.v1'})).includes('conformance.report.v1'), 'identifier strings are not folded'); ok();
{
  const html = R.renderValue({tool: 'lookup_robot', arguments: '{"robot_id":"r1","depth":2}'});
  assert.ok(visible(html).includes('r1') && !html.includes('nx-fold'), 'JSON-string arguments become rows'); noCode(html, 'arguments'); ok();
}

// Review fixes: prototype keys, an escaped backslash at the cut, code with Chinese comments.
assert.equal(R.label('constructor'), 'Constructor'); assert.equal(R.label('__proto__'), 'Proto'); ok();
assert.ok(!/native code|function/.test(visible(R.renderValue({constructor: 'x', status: 'constructor', __proto__x: 1}))), 'no Object internals on screen'); ok();
assert.deepEqual(plain(R.parseLoose('{"path":"C:\\\\data\\\\')), {value: {path: 'C:\\data\\'}, truncated: true}, 'an escaped backslash at the cut is kept'); ok();
assert.deepEqual(plain(R.parseLoose('{"a":"x\\')), {value: {a: 'x'}, truncated: true}, 'a dangling escape at the cut is dropped'); ok();
assert.equal(R.classify('# 读取相机参数\nimport cv2\ncap = cv2.VideoCapture(0)  # 打开相机\nprint(cap.isOpened())'), 'code', 'code with Chinese comments is code'); ok();
assert.equal(R.classify('diff --git a/说明.md b/说明.md\n@@ -1 +1 @@\n-旧的说明文字\n+新的说明文字'), 'code', 'a diff of Chinese text is code'); ok();
assert.equal(R.classify('相机已连接（USB 3.0），分辨率 1920×1080；接下来检查驱动 uvcvideo 是否加载。'), 'prose'); ok();

console.log(`readable logic: ${checks} checks passed`);
