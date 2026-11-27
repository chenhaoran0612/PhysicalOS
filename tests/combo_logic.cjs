// Pure-logic checks of web/combo.js (issue #61): one control that is a list to choose from and a box to
// type in. What the list holds for a typed text, which option the arrow keys reach, and the markup.
//   node tests/combo_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
const combo = vm.runInContext(readFileSync('web/combo.js', 'utf8') +
  '\n;({comboNorm, comboFilter, comboIndexOf, comboStep, comboInput, comboOptionsHtml, comboSource, COMBO_LIST_ID})', sandbox);

let checks = 0;
const ok = (condition, what) => { assert.ok(condition, what); checks += 1; };
const same = (actual, expected, what) => { assert.equal(actual, expected, what); checks += 1; };
const count = (html, piece) => html.split(piece).length - 1;

const OPTIONS = [{value: '宇树科技', keys: ['unitree', 'Unitree Robotics']}, {value: 'Universal Robots', keys: ['ur']},
  {value: 'KUKA', keys: []}, {value: 'Beta <b>'}];
const values = list => list.map(o => o.value).join('|');

// ---- what the list holds for a typed text
{
  same(values(combo.comboFilter(OPTIONS, '')), values(OPTIONS), 'nothing typed: everything');
  same(values(combo.comboFilter(OPTIONS, '   ')), values(OPTIONS), 'only spaces: everything');
  same(values(combo.comboFilter(OPTIONS, '宇树')), '宇树科技', 'part of a name');
  same(values(combo.comboFilter(OPTIONS, 'UNI')), '宇树科技|Universal Robots', 'another spelling finds it too, without case');
  same(values(combo.comboFilter(OPTIONS, 'universal-robots')), 'Universal Robots', 'spaces and hyphens do not matter');
  same(values(combo.comboFilter(OPTIONS, 'zzz')), '', 'nothing fits');
  same(combo.comboFilter(OPTIONS, 'ku')[0], OPTIONS[2], 'the options are the same objects, not copies');
  same(combo.comboIndexOf(OPTIONS, 'kuka'), 2, 'the option a text is');
  same(combo.comboIndexOf(OPTIONS, 'unitree'), 0, 'another spelling of an option is that option');
  same(combo.comboIndexOf([{value: 'ur', keys: []}, ...OPTIONS], 'UR'), 0, 'a name comes before another option\'s other spelling');
  same(combo.comboIndexOf(OPTIONS, 'uni'), -1, 'part of a name is not the option');
  same(combo.comboIndexOf(OPTIONS, ''), -1, 'nothing typed is no option');
}

// ---- the arrow keys
{
  same(combo.comboStep(-1, 1, 4), 0, 'down from nothing: the first');
  same(combo.comboStep(-1, -1, 4), 3, 'up from nothing: the last');
  same(combo.comboStep(3, 1, 4), 0, 'down from the last: round to the first');
  same(combo.comboStep(0, -1, 4), 3, 'up from the first: round to the last');
  same(combo.comboStep(1, 1, 4), 2, 'one down');
  same(combo.comboStep(0, 1, 0), -1, 'an empty list has nothing to reach');
}

// ---- the markup: one input, with a button that opens the list
{
  const html = combo.comboInput({name: 'vendor', source: 'someChoices', value: 'Beta <b>"', label: '品牌', placeholder: '选择或输入', required: true, attrs: 'data-x="1"'});
  ok(count(html, '<input') === 1 && count(html, '<select') === 0, 'one input, no second control');
  ok(/<input name="vendor"[^>]*value="Beta &lt;b&gt;&quot;"/.test(html), 'the value is written as text');
  ok(/<input[^>]*role="combobox"[^>]*aria-autocomplete="list"[^>]*aria-expanded="false"/.test(html), 'said to be a list one can type in');
  ok(/<input[^>]*data-combo-source="someChoices"[^>]*data-x="1"[^>]* required>/.test(html), 'where its options come from, the caller\'s attributes, required');
  ok(/<input[^>]*autocomplete="off"/.test(html), 'the browser does not offer its own list over ours');
  ok(/<input[^>]*aria-label="品牌"/.test(html), 'the box is called 品牌, not 品牌 plus the name of its button');
  ok(/<button type="button"[^>]*tabindex="-1"[^>]*aria-label="展开品牌列表"[^>]*data-combo-toggle/.test(html), 'a button opens the list; the keyboard uses the arrow keys instead');
  ok(!combo.comboInput({name: 'model', source: 's', label: '型号'}).includes('required'), 'not required unless asked');
}

// ---- the list
{
  const html = combo.comboOptionsHtml(OPTIONS.map((o, i) => (i === 0 ? {...o, note: '我的机器人'} : o)), 1, 3);
  same(count(html, 'role="option"'), 4, 'one line per option');
  ok(/id="combo-option-1"[^>]*aria-selected="true"/.test(html) && count(html, 'aria-selected="true"') === 1, 'the option the keys are on');
  ok(/id="combo-option-3"[^>]*class="[^"]*combo-chosen/.test(html) && count(html, 'combo-chosen') === 1, 'the option the box holds');
  ok(html.includes('Beta &lt;b&gt;') && !html.includes('<b>'), 'names are written as text');
  ok(html.includes('<small>我的机器人</small>'), 'a note next to an option');
  const none = combo.comboOptionsHtml([], -1, -1);
  ok(count(none, 'role="option"') === 0 && none.includes('直接输入'), 'nothing to choose from: it says to type');
}

// ---- where the options come from: a function of the page, named on the input
{
  sandbox.someChoices = input => [{value: 'for ' + input.dataset.comboSource}];
  sandbox.notAList = () => 'x';
  same(combo.comboSource({dataset: {comboSource: 'someChoices'}})[0].value, 'for someChoices', 'the named function is asked');
  same(combo.comboSource({dataset: {comboSource: 'missing'}}).length, 0, 'no such function: no options');
  same(combo.comboSource({dataset: {comboSource: 'notAList'}}).length, 0, 'not a list: no options');
  same(combo.comboSource({dataset: {comboSource: 'constructor'}}).length, 0, 'only functions the page defined are asked');
  sandbox.manyChoices = () => Array.from({length: 1000}, (unused, i) => ({value: 'brand ' + i}));
  same(combo.comboFilter(combo.comboSource({dataset: {comboSource: 'manyChoices'}}), 'brand 999').length, 1, 'typing finds an option however long the list is');
}

console.log(`combo_logic: ${checks} checks passed`);
