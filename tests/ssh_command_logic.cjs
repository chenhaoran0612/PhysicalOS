// Pure-logic checks of web/ssh_command.js: an ssh command line becomes connection fields.
//   node tests/ssh_command_logic.cjs
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(readFileSync('web/ssh_command.js', 'utf8'), sandbox);
const parse = text => JSON.parse(JSON.stringify(sandbox.sshParseCommand(text)));     // plain objects for deepEqual
const endpoint = (host, port, username) => ({host, port, username});
let checks = 0;
// ``jumps``: the jump hosts in order, first hop first; none for a direct connection.
const accepted = (text, target, ...jumps) => {
  const result = parse(text);
  assert.equal(result.ok, true, `${text} -> ${result.error}`);
  assert.deepEqual({target: result.target, jumps: result.jumps}, {target, jumps}, text);
  checks += 1;
  return result;
};
// A host given by a name that the jump host of level ``on`` resolves (see the nested forms below).
const named = (host, on, port = null, username = '', plain = false) =>
  ({host, port, username, resolveOn: on, ...(plain ? {plain: true} : {})});
const refused = (text, mention) => {
  const result = parse(text);
  assert.equal(result.ok, false, `${text} should be refused`);
  assert.match(result.error, mention, text);
  checks += 1;
};

// One jump host, like `ssh -J`.
accepted('ssh -J ops@203.0.113.10:2222 robot@192.168.50.20',
         endpoint('192.168.50.20', 22, 'robot'), endpoint('203.0.113.10', 2222, 'ops'));
accepted('ssh robot@192.168.1.100', endpoint('192.168.1.100', 22, 'robot'));
accepted('ssh -p 2222 robot@192.168.1.100', endpoint('192.168.1.100', 2222, 'robot'));
accepted('ssh -l robot -p2222 192.168.1.100', endpoint('192.168.1.100', 2222, 'robot'));
accepted('ssh -o ProxyJump=ops@gate.example.com -o Port=2200 robot@dog.example.com',
         endpoint('dog.example.com', 2200, 'robot'), endpoint('gate.example.com', 22, 'ops'));
accepted('ssh -o "User robot" -oPort=2200 192.168.1.100', endpoint('192.168.1.100', 2200, 'robot'));
accepted('ssh ssh://robot@192.168.1.100:2222', endpoint('192.168.1.100', 2222, 'robot'));
accepted('ssh robot@[2001:db8::1]:2222', endpoint('2001:db8::1', 2222, 'robot'));
accepted('robot@192.168.1.100', endpoint('192.168.1.100', 22, 'robot'));                 // the word "ssh" is optional
accepted(`$ ssh  -tt -J 'ops@203.0.113.10:2222'  "robot@192.168.50.20"`,                 // prompt, quotes, flag cluster
         endpoint('192.168.50.20', 22, 'robot'), endpoint('203.0.113.10', 2222, 'ops'));
accepted('ssh -vJ ops@203.0.113.10 robot@192.168.50.20',                                  // a value option ends a cluster
         endpoint('192.168.50.20', 22, 'robot'), endpoint('203.0.113.10', 22, 'ops'));
accepted('ssh -o ProxyJump=none robot@192.168.1.100', endpoint('192.168.1.100', 22, 'robot'));

// What the app cannot take from a command is said, not guessed.
let result = accepted('ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no robot@192.168.1.100 uptime',
                      endpoint('192.168.1.100', 22, 'robot'));
assert.ok(result.notes.some(note => note.includes('密钥文件')), 'the key file is not read and the user is told');
assert.ok(result.notes.some(note => note.includes('远程命令')), 'the remote command is ignored and the user is told');
result = accepted('ssh -J 203.0.113.10 robot@192.168.50.20', endpoint('192.168.50.20', 22, 'robot'), endpoint('203.0.113.10', 22, ''));
assert.ok(result.notes.some(note => note.includes('跳板机用户名')), 'a missing jump user is asked for, not invented');
result = accepted('ssh dog-nx', endpoint('dog-nx', 22, ''));
assert.ok(result.notes.some(note => note.includes('别名')), 'an ssh config alias cannot be resolved by the app');
assert.ok(result.notes.some(note => note.includes('用户名')));

// Issue #15: several jump hosts in a row, first hop first. Until then: "只支持一跳".
accepted('ssh -J a@h1.example.com,b@h2.example.com robot@192.168.1.100', endpoint('192.168.1.100', 22, 'robot'),
         endpoint('h1.example.com', 22, 'a'), endpoint('h2.example.com', 22, 'b'));
accepted('ssh -J ops@203.0.113.10:2222,relay@10.0.0.5,gate@10.1.0.7:2200 -p 2022 robot@192.168.50.20',
         endpoint('192.168.50.20', 2022, 'robot'), endpoint('203.0.113.10', 2222, 'ops'),
         endpoint('10.0.0.5', 22, 'relay'), endpoint('10.1.0.7', 2200, 'gate'));
accepted('ssh -o ProxyJump=a@h1.example.com,b@h2.example.com,c@h3.example.com robot@dog.example.com',
         endpoint('dog.example.com', 22, 'robot'), endpoint('h1.example.com', 22, 'a'),
         endpoint('h2.example.com', 22, 'b'), endpoint('h3.example.com', 22, 'c'));
accepted('ssh -J "a@h1.example.com, b@[2001:db8::2]:2200,ssh://c@h3.example.com:2022" robot@192.168.1.100',
         endpoint('192.168.1.100', 22, 'robot'), endpoint('h1.example.com', 22, 'a'),
         endpoint('2001:db8::2', 2200, 'b'), endpoint('h3.example.com', 2022, 'c'));
result = accepted('ssh -J h1.example.com,b@mid robot@192.168.1.100', endpoint('192.168.1.100', 22, 'robot'),
                  endpoint('h1.example.com', 22, ''), endpoint('mid', 22, 'b'));
assert.ok(result.notes.some(note => note.includes('第 1 级跳板机') && note.includes('用户名')), 'the hop without a user is named by its level');
assert.ok(result.notes.some(note => note.includes('mid') && note.includes('别名')), 'an alias among the jump hosts is pointed out');
assert.equal(result.resolveOnJump, false, 'with -J nothing is resolved on a jump host');

// Refusals name the reason.
refused('ssh -J a@h1.example.com,,b@h2.example.com robot@192.168.1.100', /跳板机/);
refused('ssh -J a@h1.example.com,b@h2;reboot robot@192.168.1.100', /第 2 级跳板机主机名无效/);
refused('ssh -J h1.example.com,h2.example.com,h3.example.com,h4.example.com,h5.example.com,h6.example.com robot@192.168.1.100',
        /最多 5 级/);
refused('ssh -o ProxyCommand="nc %h %p" robot@192.168.1.100', /ProxyCommand/);
refused('ssh robot@192.168.1.100;reboot', /主机/);
refused('ssh -p 70000 robot@192.168.1.100', /端口/);
refused('ssh -p abc robot@192.168.1.100', /端口/);
refused('ssh -Z robot@192.168.1.100', /-Z/);
refused('ssh -p', /-p/);
refused('ssh', /目标/);
refused('scp file robot@192.168.1.100:', /ssh/);
refused('ssh "robot@192.168.1.100', /引号/);

// Review findings: wrong-but-accepted results are worse than a refusal.
accepted('ssh -o HostName=a.example.com -o HostName=b.example.com u@h.example.com',       // like ssh: the first one wins
         endpoint('a.example.com', 22, 'u'));
refused('ssh -o HostName=%h.internal u@h.example.com', /HostName/);
accepted('ssh u@h.example.com \\\r\n -p 2222', endpoint('h.example.com', 2222, 'u'));     // a Windows line continuation
accepted('ssh -p 2222 \\\n  -J ops@gate.example.com \\\n  u@h.example.com',
         endpoint('h.example.com', 2222, 'u'), endpoint('gate.example.com', 22, 'ops'));
refused('ssh -G u@h.example.com', /-G/);                                                  // prints the config, no login
refused('ssh -W h.example.com:22 ops@gate.example.com', /-W/);
result = accepted('ssh -F ~/work/ssh_config u@h.example.com', endpoint('h.example.com', 22, 'u'));
assert.ok(result.notes.some(note => note.includes('配置文件')), 'an ssh config file is not read and the user is told');
result = accepted('ssh u@h.example.com:2222', endpoint('h.example.com', 2222, 'u'));
assert.ok(result.notes.some(note => note.includes('主机:端口')), 'ssh itself has no host:port form, so the guess is flagged');
result = accepted('ssh -J ops@gate.example.com:2222 u@h.example.com',
                  endpoint('h.example.com', 22, 'u'), endpoint('gate.example.com', 2222, 'ops'));
assert.deepEqual(result.notes, [], 'host:port is the real syntax of -J and needs no note');
refused('ssh u@h.example.com:22:33', /主机/);
accepted('ssh u@fe80::1%en0', endpoint('fe80::1%en0', 22, 'u'));
accepted('ssh ssh://u@h.example.com:2200/some/path', endpoint('h.example.com', 2200, 'u'));
accepted('ssh.exe u@h.example.com', endpoint('h.example.com', 22, 'u'));
accepted('LC_ALL=C TERM=xterm ssh u@h.example.com', endpoint('h.example.com', 22, 'u'));
accepted('ssh -o "User ro\\"bot" 192.168.1.100'.replace('ro\\"bot', 'robot'), endpoint('192.168.1.100', 22, 'robot'));

// Second review.
refused('ssh t@10.0.0.1:22:33', /主机/);                                                   // not an IPv6 address
refused('ssh -J ops@a.example.com -o ProxyJump=ops@b.example.com u@h.example.com', /两次/);
refused('ssh -J ops@%h.example.com u@h.example.com', /主机/);
refused('ssh u@%h.example.com', /主机/);
result = accepted('ssh -A u@h.example.com', endpoint('h.example.com', 22, 'u'));
assert.ok(result.notes.some(note => note.includes('Agent 转发')), 'agent forwarding is not done and the user is told');
accepted('ssh u@::ffff:192.0.2.1', endpoint('::ffff:192.0.2.1', 22, 'u'));

// Nested form: ssh to the jump host and run ssh there. It means what ssh means: the first
// host is the jump host, and the inner name is resolved by the jump host's own ssh config.
const nested = (text, target, ...jumps) => {
  const parsed = accepted(text, target, ...jumps);
  assert.equal(parsed.resolveOnJump, true, `${text}: the inner name belongs to the jump host`);
  return parsed;
};
result = nested("ssh -tt -p 2222 ops@203.0.113.10 'ssh -tt dog'",
                endpoint('dog', null, ''), endpoint('203.0.113.10', 2222, 'ops'));
assert.ok(!result.notes.some(note => note.includes('已忽略')), 'the inner ssh is the target, not an ignored command');
assert.ok(result.notes.some(note => note.includes('跳板机上的 SSH 配置')), 'the user is told where the name is resolved');
nested('ssh ops@203.0.113.10 ssh dog', endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'));       // unquoted
nested('ssh ops@203.0.113.10 "ssh -p 2200 robot@dog.internal"',
       endpoint('dog.internal', 2200, 'robot'), endpoint('203.0.113.10', 22, 'ops'));
nested("ssh ops@203.0.113.10 'ssh -l robot 192.168.50.20'",
       endpoint('192.168.50.20', null, 'robot'), endpoint('203.0.113.10', 22, 'ops'));
result = nested("ssh ops@203.0.113.10 'ssh dog uptime'", endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'));
assert.ok(result.notes.some(note => note.includes('远程命令')), 'a command after the inner host is still ignored');
assert.equal(parse('ssh -J ops@203.0.113.10 robot@192.168.1.100').resolveOnJump, false, '-J resolves nothing on the jump host');
// Issue #15: nested forms with more than one jump host. Every name is resolved where ssh itself would
// resolve it: by the ssh config of the host its ssh command runs on.
result = nested("ssh ops@203.0.113.10 'ssh mid ssh dog'",                                   // gate knows "mid", mid knows "dog"
                endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'), named('mid', 1));
assert.equal(result.resolveOn, 2, 'the innermost name belongs to the host before it');
assert.ok(result.notes.some(note => note.includes('mid') && note.includes('第 1 级跳板机')), 'who resolves a named jump host is said');
nested(`ssh -t ops@203.0.113.10 "ssh -t relay@mid 'ssh -p 2200 dog'"`,                       // quotes inside quotes
       endpoint('dog', 2200, ''), endpoint('203.0.113.10', 22, 'ops'), named('mid', 1, null, 'relay'));
result = nested("ssh -J gate.example.com ops@203.0.113.10 'ssh dog'",                       // -J outside, a name inside
                endpoint('dog', null, ''), endpoint('gate.example.com', 22, ''), endpoint('203.0.113.10', 22, 'ops'));
assert.equal(result.resolveOn, 2);
// An inner -J: both names are resolved on the host that runs that ssh, and the -J wins over a
// ProxyJump its config may have for the destination (and for every -J host but the first).
result = nested("ssh ops@203.0.113.10 'ssh -J mid dog'",
                endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'), named('mid', 1));
assert.deepEqual([result.resolveOn, result.plain], [1, true], 'the destination of an inner -J is resolved on the same host');
result = nested("ssh ops@203.0.113.10 'ssh -J relay@mid:2200,inner robot@dog'",
                endpoint('dog', null, 'robot'), endpoint('203.0.113.10', 22, 'ops'),
                named('mid', 1, 2200, 'relay'), named('inner', 1, null, '', true));
assert.deepEqual([result.resolveOn, result.plain], [1, true]);
result = nested("ssh ops@203.0.113.10 'ssh -o ProxyJump=none dog'", endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'));
assert.equal(result.plain, true, 'an inner ProxyJump=none also overrides the config');
assert.equal(nested("ssh ops@203.0.113.10 'ssh dog'", endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops')).plain, false);
nested("ssh a@h1.example.com 'ssh b ssh c ssh d ssh e ssh f'", endpoint('f', null, ''), endpoint('h1.example.com', 22, 'a'),
       named('b', 1), named('c', 2), named('d', 3), named('e', 4));                          // five jump hosts are the limit
refused("ssh a@h1.example.com 'ssh b ssh c ssh d ssh e ssh f ssh g'", /最多 5 级/);
refused("ssh ops@203.0.113.10 'ssh -J mid;reboot dog'", /第 2 级跳板机主机名无效/);
refused("ssh ops@203.0.113.10 'ssh mid ssh -o HostName=10.0.0.9 dog'", /HostName/);
refused("ssh ops@203.0.113.10 'ssh'", /目标/);
result = accepted("ssh ops@203.0.113.10 'sudo systemctl status ssh'", endpoint('203.0.113.10', 22, 'ops'));
assert.equal(result.resolveOnJump, false, 'a remote command that is not ssh stays an ignored remote command');

refused("ssh ops@203.0.113.10 'ssh -o HostName=10.0.0.9 dog'", /HostName/);                 // ssh would not resolve "dog" then
// Options of the inner ssh apply on the jump host, where this app runs no login: they are explained, not dropped silently.
result = nested("ssh ops@203.0.113.10 'ssh -A -i ~/.ssh/dog_key dog'", endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'));
assert.ok(result.notes.some(note => note.includes('-i') && note.includes('跳板机上的文件')), 'an inner key file lives on the jump host');
assert.ok(result.notes.some(note => note.includes('-A')), 'an inner -A is mentioned');
result = nested("ssh ops@203.0.113.10 'ssh -F ~/robots.conf dog'", endpoint('dog', null, ''), endpoint('203.0.113.10', 22, 'ops'));
assert.ok(result.notes.some(note => note.includes('-F') && note.includes('默认')), 'an inner config file is not what resolves the name');

assert.equal(parse('   ').ok, false);
console.log(`${checks} ssh command checks passed`);
