'use strict';
// Issue #57 — 标题统一. Every page of the sidebar is titled the way 工作台 is: the page's English name in
// small capitals above, the name the sidebar calls it by below.
//
//   * heading() of web/app.js draws the head of an ordinary page with pageTitleMarkup(current); what the
//     page itself asked for (its own English line, a sentence as the title, the step label of
//     web/journey.js) is used only for a page that is not in the table. The sentence under the title stays.
//   * 我的机器人 (web/robots_page.js) has its own head and asks pageTitleMarkup('robots').
//   * 工作台 (web/task_workbench.js) is where the format comes from and writes it out itself.
//
// The name below must be the one in the sidebar: tests/page_titles_logic.cjs compares the two.
// The page of one task (#task) is titled with the task's name and is not in the table.

const PAGE_TITLES = Object.freeze({
  overview: ['WORKBENCH', '工作台'],
  robots: ['MY ROBOTS', '我的机器人'],
  skills: ['ROBOT APPLICATIONS', '机器人应用'],
  simulation: ['SIMULATION TRIALS', '仿真试运行'],
  harness: ['VERIFICATION & TESTING', '验证与测试'],
  deployment: ['DEPLOYMENT & OPERATIONS', '部署与运行'],
  models: ['MODEL LIBRARY', '模型库'],
  training: ['TRAINING & IMPROVEMENT', '训练与改进'],
  datasets: ['TRAINING & TEST DATA', '训练与测试数据'],
  resources: ['PLATFORM MODELS & COMPUTE', '平台模型与算力'],
  billing: ['USAGE & BILLING', '用量与账单'],
  cloud: ['CLOUD ACCOUNT', '云平台账户'],
  library: ['MATERIAL LIBRARY', '资料库'],
  'cloud-skills': ['SKILL PUBLISHING', '技能发布'],
  'cloud-skill-load': ['SKILL LOADING', '技能加载'],
  // Hidden for now (issue #49, web/hidden_pages.js); titled the same way when they are shown.
  agents: ['ENGINEERING ASSISTANT', '工程助手'],
  workflows: ['ASSISTANT WORKFLOWS', '协作流程'],
  extensions: ['ASSISTANT TOOLS', '助手工具'],
  services: ['OWN SERVICES', '自有服务'],
});

// What a page is called: {en, zh}, or null for a page that is not in the table.
function pageTitle(route) {
  if (!Object.hasOwn(PAGE_TITLES, route)) return null;
  const [en, zh] = PAGE_TITLES[route];
  return {en, zh};
}

// The two lines of a page head, or '' for a page that is not in the table.
function pageTitleMarkup(route) {
  const title = pageTitle(route);
  return title ? `<div class="eyebrow">${h(title.en)}</div><h1>${h(title.zh)}</h1>` : '';
}
