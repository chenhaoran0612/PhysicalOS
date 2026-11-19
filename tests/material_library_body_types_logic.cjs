// Issue #33 × issue #32: the material library labels uploads with a type from the platform's list of
// body types (brainos/body_types.py, `state.body_types`), the same list as adding a device, and no longer
// offers the five standard values that list left out. Without the list it falls back to `state.families`.
//   node tests/material_library_body_types_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const FAMILIES = {quadruped: '四足机器人', manipulator: '机械臂', mobile: '移动底盘', humanoid: '人形机器人',
                  aerial: '飞行器', composite: '复合本体', custom: '自定义'};
const LIST = [{id: 'quadruped', name: '四足机器人', builtin: true}, {id: 'manipulator', name: '机械臂', builtin: true},
              {id: 'bodytype-1a2b', name: '轮式巡检车', builtin: false}];

function load(state) {
  const sandbox = {state, familyNames: FAMILIES, h: v => String(v ?? ''), navigation: [], views: {}, icons: {},
                   document: {addEventListener: () => {}, querySelector: () => null}, location: {hash: ''},
                   setTimeout, clearTimeout};
  vm.createContext(sandbox);
  return vm.runInContext(readFileSync('web/material_library.js', 'utf8') + ';({materialFamilies, materialBodyType})', sandbox);
}

let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };

const listed = load({families: FAMILIES, body_types: LIST});
check(Object.keys(listed.materialFamilies()), ['quadruped', 'manipulator', 'bodytype-1a2b'], 'the choices are the list of body types');
check(listed.materialBodyType('bodytype-1a2b'), '轮式巡检车', 'an added type is shown by its name');
check(listed.materialBodyType('mobile'), '移动底盘', 'a type no longer listed still shows its name');
check(listed.materialBodyType('gone-type'), 'gone-type', 'an unknown type is shown as it is');

const older = load({families: FAMILIES});
check(Object.keys(older.materialFamilies()), Object.keys(FAMILIES), 'without the list: the standard values, as before');
const empty = load({families: FAMILIES, body_types: []});
check(Object.keys(empty.materialFamilies()), Object.keys(FAMILIES), 'an empty list is treated as no list');

console.log(`material_library_body_types_logic: ${checks} checks passed`);
