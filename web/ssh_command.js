// Turn an ssh command line into connection fields. Pure functions: the command is never
// executed and nothing is read from this machine (no ~/.ssh/config, no key files).
// Issue #15: several jump hosts in a row (-J a,b,c, ProxyJump=a,b,c, nested ssh commands).

const SSH_VALUE_OPTIONS = 'BbcDEeFIiJLlmOopQRSWw';     // options followed by a value
const SSH_FLAG_OPTIONS = '46AaCfGgKkMNnqsTtVvXxYy';    // options without one
const SSH_NOT_A_LOGIN = 'GOQVW';                       // print config / control / query / version / stdio forward
const SSH_HOST_PATTERN = /^[A-Za-z0-9._:%-]{1,253}$/;  // same characters the backend accepts
const SSH_IPV6_GROUP = /^[0-9A-Fa-f]{1,4}$/;
const SSH_IPV6_GROUPS = 8;
const SSH_IPV4_PATTERN = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
const SSH_ZONE_PATTERN = /^[A-Za-z0-9._-]+$/;
const SSH_USER_PATTERN = /^[A-Za-z0-9_.@-]{1,80}$/;
const SSH_ENV_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
const SSH_QUOTED_ESCAPES = '"\\$`';                    // what a backslash escapes inside double quotes
const SSH_DEFAULT_PORT = 22;
const SSH_MAX_PORT = 65535;
const SSH_MAX_JUMPS = 5;                               // jump hosts in a row; the backend has the same limit (brainos/ssh_route.py)

// Length of the line break at text[i] (0 if none): a backslash before it continues the line.
const sshLineBreak = (text, i) => text.startsWith('\r\n', i) ? 2 : text[i] === '\n' ? 1 : 0;

function sshTokens(text) {
  const tokens = [];
  let current = '', started = false, quote = '';
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '\\' && quote !== "'" && sshLineBreak(text, i + 1)) {
      i += sshLineBreak(text, i + 1);                  // line continuation: no character, no token
    } else if (quote) {
      if (c === quote) quote = '';
      else if (c === '\\' && quote === '"' && SSH_QUOTED_ESCAPES.includes(text[i + 1] ?? '')) current += text[++i];
      else current += c;
    } else if (c === '"' || c === "'") {
      quote = c; started = true;
    } else if (c === '\\' && i + 1 < text.length) {
      current += text[++i]; started = true;
    } else if (/\s/.test(c)) {
      if (started) tokens.push(current);
      current = ''; started = false;
    } else {
      current += c; started = true;
    }
  }
  if (quote) throw Error('命令里的引号没有闭合');
  if (started) tokens.push(current);
  return tokens;
}

function sshPortNumber(value) {
  const port = Number(value);
  if (!/^\d+$/.test(String(value)) || port < 1 || port > SSH_MAX_PORT) throw Error(`端口无效：${value}`);
  return port;
}

// [user@]host[:port], ssh://[user@]host[:port][/path] or [user@][ipv6]:port
function sshEndpointSpec(spec) {
  const uri = /^ssh:\/\//i.test(spec);
  let rest = uri ? spec.slice('ssh://'.length).split('/')[0] : spec, username = '';
  const at = rest.lastIndexOf('@');
  if (at >= 0) { username = rest.slice(0, at); rest = rest.slice(at + 1); }
  const bracketed = rest.match(/^\[([^\]]+)\](?::(.*))?$/);
  if (bracketed) return {host: bracketed[1], port: bracketed[2] ?? null, username, uri};
  const parts = rest.split(':');
  return parts.length === 2 ? {host: parts[0], port: parts[1], username, uri} : {host: rest, port: null, username, uri};
}

// An IPv6 address, optionally with a %zone and an embedded IPv4 tail (::ffff:192.0.2.1).
function sshIsIpv6(host) {
  const [address, zone, ...extra] = host.split('%');
  if (extra.length || (zone !== undefined && !SSH_ZONE_PATTERN.test(zone))) return false;
  let groups = address.split(':');
  if (SSH_IPV4_PATTERN.test(groups[groups.length - 1])) groups = [...groups.slice(0, -1), '0', '0'];
  const gaps = address.split('::').length - 1;                       // "::" stands for a run of zero groups
  if (gaps > 1 || address.includes(':::')) return false;
  const written = groups.filter(group => group !== '');
  if (!written.every(group => SSH_IPV6_GROUP.test(group))) return false;
  return gaps ? written.length < SSH_IPV6_GROUPS : groups.length === SSH_IPV6_GROUPS;
}

function sshCheckedEndpoint(host, port, username, label) {
  // ":" and "%" only belong in an IPv6 address; in a name they are a typo or an ssh_config token (%h).
  const wellFormed = host.includes(':') ? sshIsIpv6(host) : !host.includes('%');
  if (!SSH_HOST_PATTERN.test(host) || host.startsWith('-') || !wellFormed) throw Error(`${label}主机名无效：${host}`);
  if (username && (!SSH_USER_PATTERN.test(username) || username.startsWith('-'))) throw Error(`${label}用户名无效：${username}`);
  return {host, port, username};
}

const sshLooksLikeAlias = host => !host.includes('.') && !host.includes(':') && host !== 'localhost';

// -J / ProxyJump: the jump hosts in order, first hop first. null for "none".
function sshSetJump(found, value) {
  if (found.jump !== undefined) throw Error('命令里指定了两次跳板机（-J 或 ProxyJump），请只保留一个');
  found.jump = value.trim().toLowerCase() === 'none' ? null : value.split(',').map(spec => sshEndpointSpec(spec.trim()));
}

function sshConfigOption(found, value) {
  const match = value.match(/^\s*([A-Za-z]+)\s*[=\s]\s*(.*)$/);
  if (!match) return;
  const key = match[1].toLowerCase(), setting = match[2].trim();
  if (key === 'port' && found.port === null) found.port = sshPortNumber(setting);
  if (key === 'user' && !found.user) found.user = setting;
  if (key === 'hostname' && !found.hostName) {
    if (/%[A-Za-z]/.test(setting) && !setting.includes(':')) throw Error('不支持 HostName 里的 % 占位符；请直接写主机名或 IP');
    found.hostName = setting;
  }
  if (key === 'proxyjump') sshSetJump(found, setting);
  if (key === 'identityfile') found.keyFile = true;
  if (key === 'proxycommand') throw Error('不支持 ProxyCommand；请改用 -J 跳板机 的写法，或在下面逐项填写');
}

function sshApplyOption(found, option, value) {
  if (option === 'p' && found.port === null) found.port = sshPortNumber(value);
  if (option === 'l' && !found.user) found.user = value;
  if (option === 'J') sshSetJump(found, value);
  if (option === 'o') sshConfigOption(found, value);
  if (option === 'i') found.keyFile = true;
  if (option === 'F') found.configFile = true;
}

// Reads the options of tokens[index]; returns the index of the last token it consumed.
function sshReadOptions(found, tokens, index) {
  const cluster = tokens[index].slice(1);
  for (let k = 0; k < cluster.length; k++) {
    const option = cluster[k];
    if (SSH_NOT_A_LOGIN.includes(option)) throw Error(`这条命令带 -${option}，不是用来登录主机的；请粘贴平时登录用的 ssh 命令`);
    if (option === 'A') found.agentForwarding = true;
    if (SSH_FLAG_OPTIONS.includes(option)) continue;
    if (!SSH_VALUE_OPTIONS.includes(option)) throw Error(`不认识的选项 -${option}`);
    const attached = cluster.slice(k + 1);
    const value = attached || tokens[++index];
    if (value === undefined) throw Error(`选项 -${option} 后面缺少参数`);
    sshApplyOption(found, option, value);
    break;
  }
  return index;
}

function sshArguments(text) {
  const tokens = sshTokens(text);
  if (tokens[0] === '$') tokens.shift();
  while (tokens.length && SSH_ENV_ASSIGNMENT.test(tokens[0])) tokens.shift();      // LC_ALL=C ssh ...
  if (!tokens.length) throw Error('请粘贴一条 ssh 命令');
  if (sshIsSshProgram(tokens[0])) tokens.shift();
  else if (!tokens[0].includes('@') && !/^ssh:\/\//i.test(tokens[0])) {
    throw Error('只能解析 ssh 命令，例如：ssh -J 用户@跳板机 用户@目标机');
  }
  return tokens;
}

const sshIsSshProgram = token => /^ssh(\.exe)?$/i.test(String(token).split('/').pop());

// Options, destination and the remote command (the tokens after the destination) of one ssh invocation.
function sshScan(tokens) {
  const found = {destination: '', user: '', port: null, hostName: '', jump: undefined,
                 keyFile: false, configFile: false, agentForwarding: false, remote: []};
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === '--') {
      if (!found.destination && tokens[i + 1] !== undefined) found.destination = tokens[++i];
      found.remote = tokens.slice(i + 1);
      break;
    }
    if (token.startsWith('-') && token.length > 1) { i = sshReadOptions(found, tokens, i); continue; }
    if (found.destination) { found.remote = tokens.slice(i); break; }       // what follows the host runs on it
    found.destination = token;
  }
  if (!found.destination) throw Error('命令里没有目标主机：ssh 后面要有 用户名@主机');
  // A quoted remote command arrives as one token: ssh gate 'ssh -tt dog'.
  if (found.remote.length === 1 && /\s/.test(found.remote[0])) found.remote = sshTokens(found.remote[0]);
  return found;
}

// The places one ssh invocation names, in the order of the route: its -J hosts, then its destination.
// `on` is the level of the jump host the invocation runs on (0: this machine). A name given there is
// resolved there, by that host's ssh config. As with ssh itself, a -J on the command line overrides
// the ProxyJump that config may have for the destination and for every -J host but the first (`plain`).
function sshInvocationPlaces(found, on) {
  if (on && found.hostName) throw Error('嵌套命令里的 -o HostName 不支持：请把内层 ssh 的目标直接写成那个地址');
  const port = spec => (spec.port === null ? null : sshPortNumber(spec.port));
  const hops = (found.jump || []).map((spec, index) => ({host: spec.host, port: port(spec), username: spec.username, on, plain: index > 0}));
  const spec = sshEndpointSpec(found.destination);
  return [...hops, {host: found.hostName || spec.host, port: found.port ?? port(spec), username: found.user || spec.username,
                    on, plain: found.jump !== undefined, guessedPort: spec.port !== null && !spec.uri && found.port === null}];
}

// Every place of a command, however deep its ssh commands are nested: `ssh a 'ssh b ssh c'` runs
// the second ssh on a and the third on b. Returns the places and the invocations they came from.
function sshCollect(outer) {
  const places = [], invocations = [];
  for (let found = outer; ; found = sshScan(found.remote.slice(1))) {
    places.push(...sshInvocationPlaces(found, places.length));     // it runs on the place named last
    invocations.push(found);
    if (!sshIsSshProgram(found.remote[0])) break;
  }
  return {places, invocations};
}

// A place as the form takes it: an address, or a name for the jump host of level `resolveOn` to resolve.
function sshPlace(place, label) {
  const checked = sshCheckedEndpoint(place.host, place.on ? place.port : place.port ?? SSH_DEFAULT_PORT, place.username, label);
  if (!place.on) return checked;
  return {...checked, resolveOn: place.on, ...(place.plain ? {plain: true} : {})};
}

function sshSharedNotes(found) {
  const notes = [];
  if (found.agentForwarding) notes.push('命令里的 -A 已忽略：这里不做 Agent 转发，每一跳各自用本机的 SSH Agent 登录。');
  if (found.configFile) notes.push('命令里指定了配置文件（-F）。这里不读取 SSH 配置文件，其中的设置不会生效。');
  if (found.keyFile) notes.push('命令里指定了密钥文件（-i）。这里不读取本机的密钥文件：请先用 ssh-add 把它加入 SSH Agent，或在下面粘贴私钥内容。');
  return notes;
}

// Options of an inner ssh of a nested command. They apply on a jump host, where nothing logs in from here.
function sshInnerNotes(inner) {
  const notes = [];
  if (inner.keyFile) notes.push('内层 ssh 的密钥文件（-i）是跳板机上的文件，这里用不到：每一跳都在本机登录，请确认本机的 SSH Agent 里有能登录的密钥，或在下面粘贴私钥内容。');
  if (inner.configFile) notes.push('内层 ssh 指定了配置文件（-F）。解析名称时用的是跳板机上默认的 SSH 配置，那个文件里的设置不会生效；如果名称只写在那个文件里，请直接填写地址。');
  if (inner.agentForwarding) notes.push('内层 ssh 的 -A 已忽略：这里不做 Agent 转发。');
  return notes;
}

// What the user should know about one place: a missing user, a guessed port, an alias this machine
// cannot resolve, or the jump host that will resolve its name. `who`: how a jump host is called
// ('' for the device); `jumpLabel(level)`: how the jump host of a level is called.
function sshPlaceNotes(place, checked, who, jumpLabel) {
  const notes = [], isTarget = !who;
  if (place.on) {
    const where = jumpLabel(place.on), what = isTarget ? '' : '是跳板机，';
    notes.push(`“${checked.host}”${what}会按${where}上的 SSH 配置解析，和在那台跳板机上执行 ssh ${checked.host} 一样；` +
               '解析出的地址、用户名和端口会在确认指纹时显示。' +
               (place.plain ? '' : '那里的配置如果给这个名称配了 ProxyJump，其中的跳板机会自动加进路线，同样逐个确认指纹。'));
    return notes;
  }
  if (!checked.username) notes.push(isTarget ? '命令里没有写用户名，请在下面补上 SSH 用户名。' : `命令里没有写${who}用户名，请在下面补上。`);
  if (place.guessedPort) {
    notes.push(`已按“主机:端口”解析出端口 ${checked.port}。ssh 命令本身没有这种写法（它用 -p），请确认端口是对的。`);
  }
  if (sshLooksLikeAlias(checked.host)) {
    notes.push(`“${checked.host}”像是 ~/.ssh/config 里的别名。这里不读取那个文件，请换成能直接访问的主机名或 IP。`);
  }
  return notes;
}

// ssh [-J a,b,c] target [command], and the nested forms where the command is another ssh.
function sshParse(text) {
  const {places, invocations} = sshCollect(sshScan(sshArguments(text)));
  const total = places.length - 1;
  if (total > SSH_MAX_JUMPS) throw Error(`跳板机最多 ${SSH_MAX_JUMPS} 级，这条命令里有 ${total} 级`);
  const jumpLabel = level => (total > 1 ? `第 ${level} 级跳板机` : '跳板机');
  const label = index => (index === total ? '目标' : jumpLabel(index + 1));
  const checked = places.map((place, index) => sshPlace(place, label(index)));
  const last = places[total], nested = invocations.length > 1;
  // The device first, then the jump hosts in order, then what applies to the whole command.
  const notes = [...sshPlaceNotes(last, checked[total], '', jumpLabel),
                 ...places.slice(0, total).flatMap((place, index) =>
                   sshPlaceNotes(place, checked[index], jumpLabel(index + 1), jumpLabel))];
  if (last.on) notes.push('每一跳都在本机登录（本机的 SSH Agent、密钥或密码），不使用跳板机上保存的密钥。');
  notes.push(...sshSharedNotes(invocations[0]), ...invocations.slice(1).flatMap(sshInnerNotes));
  if (invocations[invocations.length - 1].remote.length) notes.push(nested ? '已忽略内层 ssh 后面的远程命令。' : '已忽略命令末尾的远程命令。');
  const {resolveOn = null, plain = false, ...target} = checked[total];
  return {target, jumps: checked.slice(0, total), notes, resolveOn, plain, resolveOnJump: resolveOn !== null};
}

// {ok: true, target, jumps, notes, resolveOn, plain, resolveOnJump} or {ok: false, error}.
// jumps: the jump hosts in order, first hop first; empty for a direct connection. A jump host with
// `resolveOn` is a name for the jump host of that level to resolve (its port may be null), and so is
// the target when resolveOn is a level (resolveOnJump is then true). `plain`: the route to that name
// was given on the command line, so a ProxyJump in the resolving host's config does not count.
function sshParseCommand(text) {
  try {
    return {ok: true, ...sshParse(String(text ?? ''))};
  } catch (err) {
    return {ok: false, error: err.message};
  }
}
