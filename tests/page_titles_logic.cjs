// Issue #57 — 标题统一. Every page of the sidebar is titled the way 工作台 is: its English name in small
// capitals above, the name the sidebar calls it by below (web/page_titles.js). Pure-logic checks.
//   node tests/page_titles_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const escapes = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const h = v => String(v ?? '').replace(/[&<>"']/g, c => escapes[c]);
const source = name => readFileSync('web/' + name, 'utf8');
const lineOf = (name, start) => {
  const line = source(name).split('\n').find(l => l.startsWith(start));
  assert.ok(line, `${name} has no line starting with ${start}`);
  return line;
};

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };

// The sidebar as issue #57 shows it (0.21.13), top to bottom.
const SIDEBAR = ['工作台', '我的机器人', '机器人应用', '仿真试运行', '验证与测试', '部署与运行', '模型库', '训练与改进', '训练与测试数据',
                 '平台模型与算力', '用量与账单', '云平台账户', '资料库', '技能发布', '技能加载'];
const ROUTE_OF = {'资料库': 'library', '技能发布': 'cloud-skills', '技能加载': 'cloud-skill-load'};   // added by their own scripts
const HIDDEN = ['agents', 'workflows', 'extensions', 'services'];                                    // issue #49: kept, not shown

// web/app.js: the sidebar and the head every ordinary page is drawn with, as they are written there.
const sandbox = {h, current: 'overview'};
vm.createContext(sandbox);
const page = vm.runInContext(
  source('page_titles.js') + '\n' + source('journey.js').split('\nfunction journeyContext')[0] + '\n' +
  lineOf('app.js', 'const navigation=') + '\n' + lineOf('app.js', 'const heading=') +
  '\n;({navigation, heading, pageTitle, pageTitleMarkup, PAGE_TITLES, at: route => { current = route; }})', sandbox);

const routeOf = name => ROUTE_OF[name] || page.navigation.find(item => item[1] === name)?.[0];

// 1. Every page of the sidebar has an English name and is called what the sidebar calls it.
for (const name of SIDEBAR) {
  const route = routeOf(name);
  ok(route, `「${name}」 is a page of the sidebar`);
  const title = page.pageTitle(route);
  ok(title, `「${name}」 (#${route}) has a title`);
  same(title.zh, name, `#${route} is titled with its sidebar name`);
  ok(/^[A-Z]+(?: (?:& )?[A-Z]+)*$/.test(title.en), `#${route}: "${title.en}" is English in capitals`);
}
for (const [route, name] of page.navigation) {
  same(page.pageTitle(route)?.zh, name, `#${route} of web/app.js is titled with its sidebar name`);
}
for (const [route, name] of [['library', "navigation.push([MATERIAL_ROUTE, '资料库'])"], ['cloud-skills', "[CLOUD_SKILLS_ROUTE]: '技能发布'"],
                             ['cloud-skill-load', "[CLOUD_SKILLS_LOAD_ROUTE]: '技能加载'"]]) {
  ok(source(route === 'library' ? 'material_library.js' : 'cloud_skills.js').includes(name), `#${route} is still called that in its own script`);
}
for (const route of HIDDEN) ok(page.pageTitle(route), `the hidden page #${route} has a title too`);
same(new Set(Object.values(page.PAGE_TITLES).map(t => t[0])).size, Object.keys(page.PAGE_TITLES).length, 'no two pages share an English name');
same(page.pageTitle('task'), null, 'the page of one task is not a page of the sidebar');
same(page.pageTitle('constructor'), null, 'only own entries count');

// 2. The format: the English line, then the name.
same(page.pageTitleMarkup('overview'), '<div class="eyebrow">WORKBENCH</div><h1>工作台</h1>', 'the format of 工作台');
same(page.pageTitleMarkup('harness'), '<div class="eyebrow">VERIFICATION &amp; TESTING</div><h1>验证与测试</h1>', 'an ampersand is escaped');
same(page.pageTitleMarkup('task'), '', 'nothing for a page that is not in the table');
ok(source('task_workbench.js').includes('<div class="eyebrow">WORKBENCH</div><h1>${TW_NAME}</h1>') &&
   source('task_workbench.js').includes("const TW_NAME = '工作台';"), '工作台 itself is written in that format');

// 3. heading() of web/app.js draws every ordinary page with it, whatever the page asked for.
for (const name of SIDEBAR.slice(1)) {
  const route = routeOf(name);
  page.at(route);
  const html = page.heading('OLD ENGLISH', '一句以前的话。', '说明。', '<button>B</button>');
  ok(html.startsWith(`<div class="page-head"><div>${page.pageTitleMarkup(route)}`), `#${route}: the head starts with the English line and the name`);
  ok(!html.includes('OLD ENGLISH') && !html.includes('一句以前的话'), `#${route}: what the page used to be titled is gone`);
  same(html.split('<h1>').length, 2, `#${route}: one title`);
  ok(html.includes('<button>B</button>'), `#${route}: the buttons stay`);
  // Issue #59: nothing under the title, and no 「这个页面怎么用」 after the head.
  ok(!html.includes('class="subtitle"') && !html.includes('说明。'), `#${route}: no sentence under the title`);
  ok(!html.includes('page-purpose') && !html.includes('这个页面怎么用') && !html.includes('需要准备') && !html.includes('完成后得到'),
     `#${route}: no 「这个页面怎么用」`);
  same(html, `<div class="page-head"><div>${page.pageTitleMarkup(route)}</div><div class="button-row"><button>B</button></div></div>`,
       `#${route}: the head is the two lines and the buttons, nothing else`);
}
for (const route of HIDDEN) {
  page.at(route);
  const html = page.heading('X', 'Y', '说明。');
  ok(!html.includes('class="subtitle"') && !html.includes('page-purpose'), `the hidden page #${route} has neither when it is shown again`);
}
page.at('harness');
ok(!page.heading('X', 'Y', 'Z').includes('验证效果'), 'the step label of web/journey.js is no longer the line above the title');
ok(!page.heading('X', 'Y', 'Z').includes('先检查设备与应用配置'), 'its sentence is no longer under the title (issue #59)');
page.at('cloud');
ok(!page.heading('X', 'Y', '云平台说明').includes('云平台说明'), 'a page without a step loses the sentence it gives too (issue #59)');
page.at('somewhere-else');
ok(page.heading('EN', '标题', '说明').includes('<div class="eyebrow">EN</div><h1>标题</h1>'), 'a page outside the table is drawn as it asks');
ok(!page.heading('EN', '标题', '说明').includes('class="subtitle"'), 'and it has no sentence under the title either (issue #59)');
ok(!source('journey.js').includes('这个页面怎么用') && !source('app.js').includes('journeyHelp'), 'the block 「这个页面怎么用」 is no longer written anywhere');

// 4. 我的机器人 has its own head: the same format, and the count of robots under the name.
const robots = vm.createContext({h, button: () => '', badge: () => '', rows: () => [], state: {robots: []}});
const rp = vm.runInContext(source('page_titles.js') + '\n' + source('robots_page.js') + '\n;({rpHead})', robots);
const empty = rp.rpHead('', ''), counted = rp.rpHead('1 台真机 · 2 台仿真', '');
ok(empty.includes('<div class="eyebrow">MY ROBOTS</div><h1>我的机器人</h1>'), '我的机器人: the English line and the name, with no robot too');
ok(!empty.includes('rp-count'), 'no count without a robot');
ok(counted.includes('<div class="eyebrow">MY ROBOTS</div><h1>我的机器人</h1><p class="subtitle rp-count">1 台真机 · 2 台仿真</p>'),
   'the count of robots is under the name');
same(counted.split('class="eyebrow"').length, 2, 'one line above the name');

console.log(`page_titles_logic: ${checks} checks passed`);
