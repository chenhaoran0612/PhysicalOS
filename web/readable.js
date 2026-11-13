'use strict';
// Issue #7: nothing the console shows while it runs may be code, least of all JSON.
// Pure formatting (no DOM): machine values become labelled rows, chips and plain sentences;
// commands, raw output and code only appear folded inside a <details> the user can open.
// web/readable_dom.js applies this to whatever the pages render; tests/readable_logic.cjs covers it.
(() => {
  const MAX_DEPTH = 3;
  const MAX_ITEMS = 12;
  const MAX_CHIPS = 16;
  const LONG_TEXT = 400;
  const CJK_PROSE_RATIO = 0.15;
  const CODE_LINE_RATIO = 0.4;
  const CJK_CODE_LINE_RATIO = 0.6;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  // ---- labels ---------------------------------------------------------------------------
  const LABELS = {
    id: '编号', name: '名称', title: '标题', label: '名称', description: '说明', summary: '摘要', note: '备注', notes: '备注',
    type: '类型', kind: '类别', status: '状态', state: '状态', phase: '阶段', stage: '阶段', progress: '进度', ok: '成功', success: '成功',
    passed: '通过', failed: '失败', error: '错误', errors: '错误', message: '信息', detail: '详情', details: '详情', reason: '原因', reasons: '原因',
    warning: '提醒', warnings: '提醒', result: '结果', results: '结果', output: '输出', outputs: '输出', value: '值', values: '值',
    command: '命令', cmd: '命令', commands: '命令', script: '脚本', args: '参数', arguments: '参数', params: '参数', parameters: '参数',
    stdout: '输出内容', stderr: '错误输出', aggregatedoutput: '输出内容', exit_code: '退出码', exitcode: '退出码', returncode: '退出码',
    cwd: '工作目录', path: '路径', paths: '路径', file: '文件', files: '文件', changes: '改动', diff: '改动内容', patch: '改动内容', content: '内容',
    host: '主机', hostname: '主机名', port: '端口', username: '用户名', user: '用户', fingerprint: '指纹', key_type: '密钥类型', algorithm: '算法',
    url: '地址', endpoint: '服务地址', address: '地址', ip: 'IP 地址', timeout: '超时', timeout_seconds: '时限（秒）', purpose: '用途',
    duration: '用时', duration_ms: '用时（毫秒）', durationms: '用时（毫秒）', elapsed: '用时', seconds: '秒数',
    created_at: '创建时间', updated_at: '更新时间', started_at: '开始时间', finished_at: '结束时间', completed_at: '完成时间', time: '时间', timestamp: '时间', at: '时间', date: '日期',
    version: '版本', revision: '版本', model: '模型', model_id: '模型', provider: '提供方', server: '服务', tool: '工具', tools: '工具',
    robot: '机器人', robot_id: '机器人', project_id: '项目', skill: '技能', skill_id: '技能', application: '应用', device: '设备', devices: '设备',
    driver: '驱动', vendor: '厂商', product: '产品', serial: '序列号', category: '类别', severity: '严重程度', level: '级别', priority: '优先级',
    count: '数量', total: '合计', size: '大小', bytes: '字节', lines: '行数', items: '条目', data: '数据', metrics: '指标', score: '得分',
    loss: '损失', accuracy: '准确率', epoch: '轮次', epochs: '轮次', step: '步骤', steps: '步骤', episodes: '回合数',
    tokens: 'Token', input_tokens: '输入 Token', output_tokens: '输出 Token', total_tokens: 'Token 合计', max_tokens: '最大 Token 数',
    cost: '费用', price: '价格', credits: '点数', currency: '币种', enabled: '启用', required: '必填', default: '默认值', min: '最小值', max: '最大值',
    unit: '单位', speed: '速度', position: '位置', pos_m: '位置（米）', goal: '目标', target: '目标', mode: '模式', source: '来源', evidence: '依据',
    simulated: '仿真', sha256: '校验值', digest: '校验值', checksum: '校验值', encoding: '编码', language: '语言', format: '格式',
    tag: '标签', tags: '标签', labels: '标签', capabilities: '能力', capability: '能力', interfaces: '接口', topics: '话题', services: '服务',
    runtime: '运行时', field_acceptance: '现场验收', evidence_kind: '证据类型', conformance_id: '一致性报告编号', manifest_digest: '清单校验值', disclaimer: '说明',
    input: '输入', inputschema: '输入格式', outputschema: '输出格式', schema: '格式', options: '选项', choices: '选项', truncated: '已截断', timed_out: '超时',
  };
  // Keys whose values are code or raw machine text: always folded.
  const CODE_KEYS = new Set(['command', 'cmd', 'commands', 'script', 'stdout', 'stderr', 'aggregatedoutput', 'output_text', 'diff', 'patch',
    'unified_diff', 'content', 'code', 'source_code', 'traceback', 'stack', 'stacktrace', 'raw', 'body', 'arguments']);
  const ENUMS = {
    commandexecution: '执行命令', filechange: '修改文件', mcptoolcall: '调用工具', collabagenttoolcall: '协作助手', agentmessage: '助手回复',
    reasoning: '思考过程', websearch: '联网搜索', usermessage: '你的消息', command_output: '命令输出', tool_error: '工具出错',
    completed: '已完成', complete: '已完成', succeeded: '已完成', success: '成功', done: '已完成', failed: '失败', failure: '失败', error: '出错',
    running: '进行中', inprogress: '进行中', in_progress: '进行中', pending: '等待中', queued: '排队中', started: '已开始', declined: '已拒绝',
    approved: '已批准', approve: '批准', accept: '接受', accepted: '已接受', rejected: '已拒绝', cancelled: '已取消', canceled: '已取消',
    interrupted: '已停止', stopped: '已停止', timeout: '超时', skipped: '已跳过', add: '新增', added: '新增', delete: '删除', deleted: '删除',
    update: '修改', updated: '修改', modify: '修改', modified: '修改', info: '提示', warn: '注意', warning: '注意', critical: '严重', high: '高', medium: '中', low: '低',
  };
  const ENUM_KEYS = new Set(['type', 'kind', 'status', 'state', 'phase', 'severity', 'level', 'decision', 'result']);
  const normKey = key => String(key).toLowerCase();
  const own = (map, key) => (map && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined);
  function label(key) {
    const k = normKey(key);
    if (own(LABELS, k)) return LABELS[k];
    const words = String(key).replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_\-.]+/g, ' ').trim().toLowerCase();
    return words ? words[0].toUpperCase() + words.slice(1) : String(key);
  }
  function enumText(value) {
    const k = normKey(value).replace(/[\s-]/g, '');
    const appNames = typeof stateNames === 'object' ? stateNames : null;
    const known = own(appNames, value) || own(ENUMS, k) || own(ENUMS, normKey(value));
    return typeof known === 'string' ? known : value;
  }

  // ---- parsing --------------------------------------------------------------------------
  // Closes a JSON text that was cut off (stored transcripts are capped at a few KB).
  function closeTruncated(text) {
    const stack = [];
    let inString = false;
    let escaped = false;
    for (const c of text) {
      if (inString) {
        if (escaped) escaped = false;
        else if (c === '\\') escaped = true;
        else if (c === '"') inString = false;
      } else if (c === '"') inString = true;
      else if (c === '{' || c === '[') stack.push(c === '{' ? '}' : ']');
      else if (c === '}' || c === ']') stack.pop();
    }
    let fixed = (inString && escaped ? text.slice(0, -1) : text) + (inString ? '"' : '');
    const trims = [/,\s*"(?:[^"\\]|\\.)*"\s*:?\s*$/, /,\s*$/, /:\s*$/, /\{\s*"(?:[^"\\]|\\.)*"\s*:?\s*$/];
    for (let i = 0; i < 4; i += 1) {
      const before = fixed;
      fixed = fixed.replace(trims[0], '').replace(trims[1], '').replace(trims[2], ': null');
      if (/\{\s*"(?:[^"\\]|\\.)*"\s*$/.test(fixed)) fixed = fixed.replace(/"(?:[^"\\]|\\.)*"\s*$/, '');
      if (fixed === before) break;
    }
    return fixed + stack.reverse().join('');
  }
  const fromPython = text => text
    .replace(/'((?:[^'\\]|\\.)*)'/g, (_, s) => JSON.stringify(s.replace(/\\'/g, "'")))
    .replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/\bNone\b/g, 'null');
  const tryParse = text => { try { return {value: JSON.parse(text)}; } catch { return null; } };
  const isData = v => v !== null && typeof v === 'object';

  function parseLoose(text) {
    const t = String(text ?? '').trim();
    if (!/^[{[]/.test(t)) return null;
    const whole = tryParse(t);
    if (whole) return isData(whole.value) ? {value: whole.value, truncated: false} : null;
    const py = /'/.test(t) ? tryParse(fromPython(t)) : null;
    if (py && isData(py.value)) return {value: py.value, truncated: false};
    if (!/^[{[]\s*["{[\]}\d-]|^\[\s*(true|false|null)/.test(t)) return null;
    const cut = tryParse(closeTruncated(t));
    return cut && isData(cut.value) ? {value: cut.value, truncated: true} : null;
  }

  // ---- classification -------------------------------------------------------------------
  const TABLE_ROW = /^\s*\|.*\|\s*$/;
  const NEUTRAL_LINE = /^\s*#{1,6}(\s|$)/; // a Markdown heading or a comment: says nothing either way
  const STRONG_CODE = /^(diff --git |@@ .* @@|#!)/m;
  const CODE_LINE = [
    /^\s*(\$|#!|>>>|sudo |cd |ls\b|cat |echo |grep |apt(-get)? |pip3? |python3? |ros2 |roslaunch |rosrun |export |source |systemctl |journalctl |docker |git |curl |wget |chmod |mkdir |rm |cp |mv |tail |head |find |sed |awk |make |cmake |npm |node )/,
    /^(diff --git|@@ .* @@|\+\+\+ |--- |index [0-9a-f]+\.\.)/, /^\s*(import |from \S+ import |def |class |function |const |let |var |#include|<\?xml|package |public |private )/,
    /(&&|\|\||\s\|\s|;\s*$|=>|\s--[a-z][\w-]*|^\s*[{}[\]];?$|\w+\(.*\)\s*[;{]?$)/,
    /^\s*[A-Za-z_][\w.]*(\[[^\]]*\])?\s*[-+*/]?=\s*([\w.]+\(|["'[{]|-?\d+(\.\d+)?\s*(#.*)?$|True\b|False\b|None\b|null\b|true\b|false\b)/,
  ];
  // Kind of a text plus its parsed data (when it is JSON), so callers parse only once.
  function inspect(text) {
    const t = String(text ?? '').trim();
    if (!t) return {kind: 'empty'};
    const data = parseLoose(t);
    return data ? {kind: 'json', data} : {kind: classifyText(t)};
  }
  const classify = text => inspect(text).kind;
  function classifyText(t) {
    if (/Traceback \(most recent call last\)/.test(t)) return 'traceback';
    if (STRONG_CODE.test(t)) return 'code';
    const solid = t.replace(/\s/g, '');
    const cjk = (solid.match(/[㐀-鿿＀-￯　-〿]/g) || []).length;
    const cjkHeavy = cjk / solid.length >= CJK_PROSE_RATIO;
    const lines = t.split('\n').filter(l => l.trim() && !TABLE_ROW.test(l) && !NEUTRAL_LINE.test(l));
    if (!lines.length) return 'prose';
    const codeLines = lines.filter(l => CODE_LINE.some(re => re.test(l))).length;
    // Mostly-Chinese text is code only when most of its lines are (e.g. a script with Chinese comments).
    return codeLines / lines.length >= (cjkHeavy ? CJK_CODE_LINE_RATIO : CODE_LINE_RATIO) ? 'code' : 'prose';
  }

  // ---- rendering ------------------------------------------------------------------------
  const lineCount = text => String(text).replace(/\n+$/, '').split('\n').length;
  function folded(summary, text) {
    return `<details class="nx-fold"><summary>${esc(summary)}</summary><pre class="nx-raw">${esc(text)}</pre></details>`;
  }
  const foldCode = (text, what = '内容') => folded(`展开查看${what}（${lineCount(text)} 行）`, text);
  const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;
  function timeText(value) {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  }

  function scalar(value, key) {
    if (value === null || value === undefined || value === '') return '<span class="nx-empty">—</span>';
    if (typeof value === 'boolean') return value ? '<span class="nx-yes">是</span>' : '<span class="nx-no">否</span>';
    if (typeof value === 'number') return esc(Number.isInteger(value) ? value.toLocaleString('zh-CN') : Number(value.toFixed(4)).toLocaleString('zh-CN', {maximumFractionDigits: 4}));
    const text = String(value);
    if (key && CODE_KEYS.has(normKey(key))) {
      const data = parseLoose(text); // e.g. tool arguments sent as a JSON string
      return data ? renderAny(data.value, 1) : foldCode(text, label(key));
    }
    if (ISO_TIME.test(text)) return esc(timeText(text));
    if (key && ENUM_KEYS.has(normKey(key)) && /^[\w-]+$/.test(text)) return `<span class="nx-tag">${esc(enumText(text))}</span>`;
    return renderText(text, {nested: true});
  }

  function headline(obj) {
    const kind = obj.type ?? obj.kind;
    if (typeof kind !== 'string' || !/^[\w-]+$/.test(kind)) return '';
    const title = enumText(kind);
    if (title === kind && !ENUMS[normKey(kind)]) return '';
    const status = typeof (obj.status ?? obj.state) === 'string' ? `<span class="nx-tag">${esc(enumText(obj.status ?? obj.state))}</span>` : '';
    return `<div class="nx-head"><b>${esc(title)}</b>${status}</div>`;
  }

  function renderObject(obj, depth) {
    const keys = Object.keys(obj);
    if (!keys.length) return '<span class="nx-empty">无</span>';
    if (depth >= MAX_DEPTH) return `<span class="nx-empty">${keys.length} 个字段</span>`;
    const head = headline(obj);
    const shown = head ? keys.filter(k => !['type', 'kind', 'status', 'state'].includes(k)) : keys;
    const rows = shown.map(k => `<div class="nx-row"><div class="nx-key">${esc(label(k))}</div><div class="nx-val">${renderAny(obj[k], depth + 1, k)}</div></div>`);
    return `<div class="nx-data">${head}${rows.join('')}</div>`;
  }

  function renderArray(list, depth, key) {
    if (!list.length) return '<span class="nx-empty">无</span>';
    if (list.every(v => v === null || ['string', 'number', 'boolean'].includes(typeof v))) {
      if (key && CODE_KEYS.has(normKey(key))) return foldCode(list.join('\n'), label(key));
      const long = list.some(v => String(v).length > 60 || /\n/.test(String(v)));
      if (long) return `<div class="nx-lines">${list.slice(0, MAX_ITEMS).map(v => `<div>${scalar(v)}</div>`).join('')}${more(list.length - MAX_ITEMS)}</div>`;
      return `<div class="nx-chips">${list.slice(0, MAX_CHIPS).map(v => `<span class="nx-chip">${typeof v === 'string' ? esc(v) : scalar(v)}</span>`).join('')}${more(list.length - MAX_CHIPS)}</div>`;
    }
    if (depth >= MAX_DEPTH) return `<span class="nx-empty">${list.length} 项</span>`;
    return `<div class="nx-list">${list.slice(0, MAX_ITEMS).map(v => renderAny(v, depth + 1)).join('')}${more(list.length - MAX_ITEMS)}</div>`;
  }
  const more = n => (n > 0 ? `<span class="nx-more">还有 ${n} 项</span>` : '');

  function renderAny(value, depth = 0, key = '') {
    if (Array.isArray(value)) return renderArray(value, depth, key);
    if (isData(value)) return renderObject(value, depth);
    return scalar(value, key);
  }

  function renderValue(value) {
    return `<div class="nx-readable">${renderAny(value, 0)}</div>`;
  }

  // Inline marks: **bold**, `term`, [text](link) read as plain emphasis (input is already escaped).
  const inline = html => html.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>').replace(/`([^`\n]+)`/g, '<span class="nx-term">$1</span>')
    .replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, '$1');
  const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
  const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(esc(c.trim())));
  function table(rows) {
    const [head, ...body] = rows.filter(r => !TABLE_RULE.test(r)).map(cells);
    const tr = (cols, tag) => `<tr>${cols.map(c => `<${tag}>${c}</${tag}>`).join('')}</tr>`;
    return `<table class="nx-table"><thead>${tr(head, 'th')}</thead><tbody>${body.map(r => tr(r, 'td')).join('')}</tbody></table>`;
  }
  // Light Markdown: headings, bullets, quotes and tables become formatted text, never markup.
  function markdownLite(text) {
    const out = [];
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
      if (TABLE_ROW.test(lines[i]) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
        const rows = [];
        while (i < lines.length && (TABLE_ROW.test(lines[i]) || TABLE_RULE.test(lines[i]))) rows.push(lines[i++]);
        i -= 1;
        out.push(table(rows));
        continue;
      }
      const line = lines[i];
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) continue;
      const heading = line.match(/^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/);
      if (heading) { out.push(`<b class="nx-h">${inline(esc(heading[1]))}</b>`); continue; }
      out.push(inline(esc(line.replace(/^(\s*)[-*+]\s+/, '$1• ').replace(/^\s*>\s?/, ''))));
    }
    return out.join('\n').replace(/\n?(<table[\s\S]*?<\/table>)\n?/g, '$1').replace(/(<b class="nx-h">[^\n]*<\/b>)\n/g, '$1');
  }

  // Prose: ```fences``` fold away; the text between them is light Markdown.
  function renderProse(text) {
    const parts = String(text).split(/```[\w+-]*\n?([\s\S]*?)(?:```|$)/);
    const html = parts.map((part, i) => {
      if (i % 2) return part.trim() ? foldCode(part.replace(/\n$/, ''), '代码') : '';
      const trimmed = part.replace(i > 0 ? /^[ \t]*\n/ : /^$/, '').replace(i < parts.length - 1 ? /\n[ \t]*$/ : /$^/, '');
      return markdownLite(trimmed);
    }).join('');
    return `<div class="nx-prose">${html.trim()}</div>`;
  }

  function renderTraceback(text) {
    const lines = String(text).trim().split('\n');
    const last = lines[lines.length - 1].trim();
    const message = last.replace(/^[\w.]+(Error|Exception|Exit|Interrupt|Warning)\s*:\s*/, '') || last;
    return `<div class="nx-error"><b>出现错误：</b>${esc(message)}</div>${folded('展开查看技术细节', text)}`;
  }

  function renderText(text, opts = {}) {
    const t = String(text ?? '');
    const seen = inspect(t);
    switch (seen.kind) {
      case 'empty': return '<span class="nx-empty">—</span>';
      case 'json': {
        const parsed = seen.data;
        const note = parsed.truncated ? '<div class="nx-note">内容不完整：记录过长，只保留了开头部分。</div>' : '';
        return `${renderAny(parsed.value, opts.nested ? 1 : 0)}${note}`;
      }
      case 'traceback': return renderTraceback(t);
      case 'code': return foldCode(t, opts.what || '内容');
      default:
        if (t.length > LONG_TEXT && opts.nested) return `${renderProse(t.slice(0, LONG_TEXT))}${folded('展开全部', t)}`;
        return renderProse(t);
    }
  }

  const hasLabel = key => Object.prototype.hasOwnProperty.call(LABELS, normKey(key));
  globalThis.nxReadable = {parseLoose, classify, renderValue, renderText, renderProse, foldCode, label, hasLabel, enumText, esc, CODE_KEYS};
})();
