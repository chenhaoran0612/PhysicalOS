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
const accepted = (text, target, jump = null) => {
  const result = parse(text);
  assert.equal(result.ok, true, `${text} -> ${result.error}`);
  assert.deepEqual({target: result.target, jump: result.jump}, {target, jump}, text);
  checks += 1;
  return result;
};
const refused = (text, mention) => {
  const result = parse(text);
  assert.equal(result.ok, false, `${text} should be refused`);
  assert.match(result.error, mention, text);
  checks += 1;
};

// The command the feature exists for: one jump host, like `ssh -J`.
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

// Refusals name the reason.
refused('ssh -J a@h1.example.com,b@h2.example.com robot@192.168.1.100', /一跳/);
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

assert.equal(parse('   ').ok, false);
console.log(`${checks} ssh command checks passed`);
