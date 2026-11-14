// Pure-logic checks of web/ssh_edit.js (issue #9): when an edit of a robot's SSH connection needs its
// fingerprints read again, and when a login has to be typed. The backend rule for the first question is
// ConnectionEdit._confirmed_before, checked with the same cases in tests/test_ssh_edit.py (AddressRuleTests).
//   node tests/ssh_edit_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

// The file wraps three functions of earlier scripts when it loads; stand-ins are enough here.
const sandbox = {engineRobotCard: () => '', engineAction: async () => {}, engineSubmit: async () => {},
                 render: () => {}, document: {addEventListener: () => {}}};
vm.createContext(sandbox);
const {sshEditAddressChanged: changed, sshEditMissingSecret: missing} =
  vm.runInContext(readFileSync('web/ssh_edit.js', 'utf8') + '\n;({sshEditAddressChanged, sshEditMissingSecret})', sandbox);
let checks = 0;
const check = (actual, expected, what) => { assert.deepEqual(actual, expected, what); checks += 1; };

const direct = {host: '192.0.2.10', port: 22, username: 'robot', auth: 'password'};
const viaJump = {...direct, jump: {host: '203.0.113.10', port: 2222, username: 'ops', auth: 'password'}};
const form = (saved, changes = {}) => ({
  route: saved.jump ? 'jump' : 'direct', host: saved.host, port: String(saved.port), username: saved.username, auth: saved.auth,
  ...(saved.jump ? {jump_host: saved.jump.host, jump_port: String(saved.jump.port),
                    jump_username: saved.jump.username, jump_auth: saved.jump.auth} : {}),
  ...changes,
});

// A direct connection.
check(changed(form(direct), direct), false, 'nothing changed');
check(changed(form(direct, {host: ' 192.0.2.10 '}), direct), false, 'spaces around the host');
check(changed(form(direct, {username: 'operator'}), direct), false, 'another user on the same host');
check(changed(form(direct, {host: '192.0.2.11'}), direct), true, 'another host');
check(changed(form(direct, {port: '2222'}), direct), true, 'another port');
check(changed(form(direct, {port: ''}), direct), true, 'no port');
check(changed(form(direct, {route: 'jump', jump_host: '203.0.113.10', jump_port: '22', jump_username: 'ops'}), direct), true,
      'a jump host was added');

// A connection through a jump host.
check(changed(form(viaJump), viaJump), false, 'nothing changed through a jump host');
check(changed(form(viaJump, {jump_username: 'admin'}), viaJump), false, 'another user on the same jump host');
check(changed(form(viaJump, {jump_host: '203.0.113.11'}), viaJump), true, 'another jump host');
check(changed(form(viaJump, {jump_port: '22'}), viaJump), true, 'another jump port');
check(changed(form(viaJump, {resolve_on_jump: '1'}), viaJump), true, 'the name is to be resolved again');
// A direct form has its jump controls disabled, so they are not in the form data at all.
check(changed({route: 'direct', host: direct.host, port: '22', username: 'robot', auth: 'password'}, viaJump), true,
      'the jump host was removed');

// A login has to be typed only when its method is not the saved one.
check(missing(form(direct), direct), '', 'empty password, password saved: kept');
check(missing(form(direct, {password: 'typed'}), direct), '', 'a typed password');
check(missing(form(direct, {auth: 'agent'}), direct), '', 'the SSH agent needs nothing');
check(missing(form(direct, {auth: 'key'}), direct), '请输入SSH 私钥', 'a key was never saved');
check(missing(form(direct, {auth: 'key', private_key: 'typed'}), direct), '', 'a typed key');
check(missing(form(viaJump, {jump_auth: 'key'}), viaJump), '请输入跳板机 SSH 私钥', 'no key saved for the jump host');
check(missing(form(viaJump), viaJump), '', 'both saved logins kept');
check(missing(form(direct, {route: 'jump', jump_host: '203.0.113.10', jump_port: '22', jump_username: 'ops', jump_auth: 'password'}), direct),
      '请输入跳板机 SSH 密码', 'a new jump host has no saved login');
check(missing({...form(viaJump), route: 'direct', jump_auth: 'key'}, viaJump), '', 'jump fields of a direct form are ignored');

console.log(`${checks} ssh edit checks passed`);
