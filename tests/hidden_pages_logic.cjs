// Pure-logic checks of web/hidden_pages.js (issue #49): which pages are hidden, and what happens to a
// control on another page that leads to one of them.
//   node tests/hidden_pages_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const hp = vm.runInContext(readFileSync('web/hidden_pages.js', 'utf8') +
  '\n;({HIDDEN_PAGES, HIDDEN_ACTIONS, hpIsHidden, hpRouteOf, hpDecide})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, what); checks += 1; };

// ---- the hidden pages: the three of the issue, and 协作流程 which is a page inside 工程助手
{
  same([...hp.HIDDEN_PAGES].sort(), ['agents', 'extensions', 'services', 'workflows'], 'the hidden pages');
  for (const route of ['agents', '#agents', 'workflows', 'extensions', '#services']) ok(hp.hpIsHidden(route), route + ' is hidden');
  for (const route of ['', '#', 'overview', 'task', 'robots', 'skills', 'models', 'resources', 'cloud', 'agent', 'agents2', null, undefined]) {
    ok(!hp.hpIsHidden(route), String(route) + ' is not hidden');
  }
  ok(Object.isFrozen(hp.HIDDEN_PAGES) && Object.isFrozen(hp.HIDDEN_ACTIONS), 'the lists cannot be changed by another script');
}

// ---- where a control leads
{
  same(hp.hpRouteOf({action: 'go-agents'}), 'agents', 'a go- action');
  same(hp.hpRouteOf({href: '#services'}), 'services', 'an address inside the console');
  same(hp.hpRouteOf({action: 'go-skills', href: '#agents'}), 'skills', 'the action is what a click does');
  same(hp.hpRouteOf({action: 'engine-discover'}), '', 'an action that opens no page');
  same(hp.hpRouteOf({href: 'https://example.test/#agents'}), '', 'an address outside the console');
  same(hp.hpRouteOf({}), '', 'nothing');
}

// ---- what happens to a control
{
  same(hp.hpDecide({action: 'go-robots'}), {kind: 'keep'}, 'a link to a page that is shown');
  same(hp.hpDecide({href: '#overview'}), {kind: 'keep'}, 'the brand link');
  same(hp.hpDecide({action: 'engine-discover'}), {kind: 'keep'}, '调研 stays');
  same(hp.hpDecide({action: 'journey-prepare'}), {kind: 'keep'}, '让助手准备 stays');
  same(hp.hpDecide({action: 'engine-flow-adapt'}), {kind: 'keep'}, '本体适配 stays');
  same(hp.hpDecide({action: 'service-add'}), {kind: 'keep'}, 'a dialog of 自有服务 is not the page');

  for (const page of hp.HIDDEN_PAGES) {
    same(hp.hpDecide({action: 'go-' + page}), {kind: 'remove'}, 'a button to ' + page);
    same(hp.hpDecide({href: '#' + page}), {kind: 'remove'}, 'a link to ' + page);
  }
  same(hp.hpDecide({action: 'go-extensions', href: '#extensions'}), {kind: 'remove'}, '「管理扩展」');
  same(hp.hpDecide({action: 'engine-work'}), {kind: 'remove'}, '「提出工程需求」 outside 工程助手');

  // The step of a task's page stays a step: it opens 机器人应用, where the same step is done.
  same(hp.hpDecide({action: 'go-agents', inStep: true}), {kind: 'retarget', action: 'go-skills'}, '准备能力');
  same(hp.hpDecide({action: 'go-services', inStep: true}), {kind: 'remove'}, 'a step with nowhere else to go');
  same(hp.hpDecide({action: 'go-harness', inStep: true}), {kind: 'keep'}, 'the other steps');

  // A card whose only control led to a hidden page goes as a whole; a card with more keeps the rest.
  same(hp.hpDecide({action: 'go-agents', aloneInCard: true}), {kind: 'remove-card'}, '「让助手帮忙开发」');
  same(hp.hpDecide({action: 'go-agents', aloneInCard: false}), {kind: 'remove'}, 'one of several controls');
  same(hp.hpDecide({action: 'go-models', aloneInCard: true}), {kind: 'keep'}, 'the other cards');
}

console.log(`hidden_pages_logic: ${checks} checks passed`);
