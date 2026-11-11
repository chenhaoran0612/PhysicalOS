'use strict';
// ux02 (round 05): "站在用户的需求来设计".
//  1. Every technical error gets a plain Chinese explanation; the original text stays in a collapsible block.
//  2. Simulated robots: say plainly, before the user tries, that simulation can never become field
//     evidence (ADR-011), what a simulated robot IS good for, and what real deployment needs.
//  3. Skill run parameters: a form generated from skill.json, with a raw-JSON advanced toggle.
// Nothing here grants an execution right: authorization, e-stop and sim-only rules stay on the server.

// ---------------------------------------------------------------- 1. plain errors
// ux04 (Q5-04): the classification lives in ux04_errors.js (by code and status first, text last).
const UX_SIM_PLAIN = UX_CODES.simulated_evidence[1];

// err: an Error from api() (status, reason, body), an operation result ({error, code, ...}) or a string.
function explainError(err) {
  // Our own check of a form (ux04): already plain and specific, nothing was sent.
  if (Array.isArray(err?.fields)) return {plain: err.message, detail: '', kind: 'param', next: [], code: '', status: null};
  return uxClassify(err);
}

function errorBlock(err) {
  const e = explainError(err);
  const tag = e.kind ? `<span class="error-kind" data-kind="${h(e.kind)}">${h(UX_KIND_NAMES[e.kind] || '')}</span>` : '';
  const next = e.next?.length ? `<ul class="error-next">${e.next.map(x => `<li>${h(x)}</li>`).join('')}</ul>` : '';
  const tech = e.detail ? [e.detail, e.code ? '错误码 ' + e.code : '', e.cause ? '原因 ' + e.cause : '', e.status ? 'HTTP ' + e.status : ''].filter(Boolean).join(' · ') : '';
  // ux05 (Q6-08): the robot's own hint for the operator, when it gave one
  const hint = e.hint ? `<p class="error-hint" data-error-hint>机器人的提示：${h(e.hint)}</p>` : '';
  return `<span class="plain-error" data-error-code="${h(e.code || '')}"${e.cause ? ` data-error-cause="${h(e.cause)}"` : ''}>${tag}${h(e.plain)}</span>${next}${hint}` +
    (tech ? `<details class="tech-detail"><summary>技术细节</summary><code>${h(tech)}</code></details>` : '');
}

function formError(el, err) {
  if (!el) return;
  const e = explainError(err);
  // An already-plain message stays a bare text node (as before ux02); only technical ones get the block.
  if (!e.detail && !e.next?.length && !e.kind) el.textContent = String(err?.message ?? err ?? '');
  else el.innerHTML = errorBlock(err);
  // ux04 (Q5-05): put a parameter problem next to its field as well.
  const form = el.closest('form');
  if (form && typeof uxMarkParamFields === 'function') uxMarkParamFields(form, err, e);
}

let uxToastHover = false;
function showError(err) {
  const t = $('#toast');
  t.innerHTML = errorBlock(err);
  t.classList.add('show', 'error');
  clearTimeout(toastTimer);
  const hide = () => { if (uxToastHover) { toastTimer = setTimeout(hide, 1000); return; } t.classList.remove('show', 'error'); };
  toastTimer = setTimeout(hide, 9000);
}
// A normal toast after an error toast must not keep the error styling.
{
  const plainToast = toast;
  toast = text => { $('#toast').classList.remove('error'); uxToastHover = false; return plainToast(text); };
}
document.addEventListener('DOMContentLoaded', () => {
  const t = $('#toast');
  if (!t) return;
  t.addEventListener('mouseenter', () => { uxToastHover = true; });
  t.addEventListener('mouseleave', () => { uxToastHover = false; });
});

// ---------------------------------------------------------------- 2. simulated robots
const isSimRobot = r => Boolean(r && (r.sim || (r.manifest || {}).simulated === true));
const uxSimRobots = () => rows('robots').filter(isSimRobot);
const uxRealRobots = () => rows('robots').filter(r => !isSimRobot(r));

// What a user can and cannot do with a simulated robot, with the next step for each.
function simDeployNotice(where) {
  const sims = uxSimRobots(), reals = uxRealRobots();
  if (!sims.length) return '';
  const first = sims[0];
  const runnable = first.sim && first.status === 'connected';
  const runButton = runnable ? button('在仿真机器人上运行技能', 'sim-run-pick', '', first.id) : '';
  if (reals.length) {
    return `<section class="plan-review sim-explain" data-sim-explain="mixed"><h3>${simBadge()} 仿真机器人只能做仿真测试</h3>` +
      `<p>${h(sims.map(r => r.name).join('、'))} 是仿真机器人：在它上面的测试结果不计为现场证据，也不能发布。` +
      `发布与正式运行只对真机（${h(reals.map(r => r.name).join('、'))}）开放。</p></section>`;
  }
  const lead = where === 'deployment'
    ? '你现在只有仿真机器人，所以这里的“发布应用”还不能用。'
    : '你现在只有仿真机器人，所以这里只能做仿真测试，不能做现场测试。';
  return `<section class="plan-review sim-explain" data-sim-explain="only"><h3>${simBadge()} ${lead}</h3>` +
    `<div class="sim-explain-grid">` +
    `<div><b>用仿真机器人可以</b><p>安装技能、授权运行并实时观看它动起来；随时停止（会锁存急停，需手动解除）；在应用交付清单里做仿真测试，提前发现问题。</p></div>` +
    `<div><b>用仿真机器人不能</b><p>把结果当作现场测试证据，或发布到设备。仿真结果不能证明真机能安全工作——这是有意的安全设计，不是故障。</p></div>` +
    `<div><b>要真正部署，需要</b><p>接入一台真实机器人（让助手通过 SSH 调查，或连接已装好接入程序的机器人），在真机上通过现场测试，然后发布。</p></div>` +
    `</div><div class="button-row">${runButton}${button('去仿真试运行', 'go-simulation', runnable ? 'secondary' : '')}${button('接入真机', 'go-robots', 'secondary')}</div></section>`;
}

// The simulation page: the user's simulated robots are the simulation they already have.
function simWorkbench() {
  const sims = uxSimRobots().filter(r => r.sim);
  const body = sims.length
    ? `<div class="cards">${sims.map(r => {
        const live = r.status === 'connected';
        return `<article class="card sim-workbench-card"><div class="card-body"><div class="card-top"><span class="agent-avatar">${icon(r.sim.kind === 'arm' ? 'robots' : 'quadruped')}</span>${simBadge()}</div>` +
          `<h3>${h(r.name)}</h3><p>${live ? '运行在这台电脑上，可以马上运行技能并实时观看。' : '仿真已关闭。到“我的机器人”里重新启动它。'}</p></div>` +
          `<div class="card-bottom">${live ? button('运行技能', 'sim-run-pick', 'small', r.id) + button('实时状态', 'sim-live', 'secondary small', r.id) + button('安装技能', 'skillpkg-open', 'secondary small', r.id)
            : button('去我的机器人', 'go-robots', 'secondary small')}</div></article>`;
      }).join('')}</div>`
    : empty('还没有仿真机器人', '在这台电脑上启动一台虚拟的四足机器人或机械臂，不用接线，马上可以运行技能、看它动起来。', button('添加仿真机器人', 'sim-add', 'small'));
  return `<div class="section-head"><h2>你的仿真机器人</h2><span>最快的试运行方式 · 不需要仿真服务</span></div>${body}` +
    `<p class="inline-note">下面的“仿真任务”用于接入外部仿真引擎（场景、批量运行、生成训练数据），属于进阶用法；只想看机器人动起来，用上面的仿真机器人即可。</p><div class="spacer"></div>`;
}

function uxInsertAfterHead(html, extra) {
  if (!extra) return html;
  const help = typeof journeyHelp === 'function' ? journeyHelp(current) : '';
  const at = help ? html.indexOf(help) : -1;
  if (at >= 0) return html.slice(0, at + help.length) + extra + html.slice(at + help.length);
  const head = html.indexOf('</div></div>', html.indexOf('page-head'));
  return head >= 0 ? html.slice(0, head + 12) + extra + html.slice(head + 12) : extra + html;
}

{
  const wrap = (key, extra) => { const orig = views[key]; views[key] = () => uxInsertAfterHead(orig(), state.user && project() ? extra() : ''); };
  wrap('harness', () => simDeployNotice('harness'));
  wrap('deployment', () => simDeployNotice('deployment'));
  wrap('simulation', simWorkbench);
}

// ---------------------------------------------------------------- 3. skill run parameters
// ux05 (Q6-11): no jargon -- "结构化数据" said nothing to a new user.
const uxTypeNames = {int: '整数', float: '数字', bool: '是 / 否', str: '文字', enum: '选项', vector: '一组数字',
  floats: '一组数字', pose: '位姿（7 个数字）', blob_ref: '文件引用', object: '多项内容'};
const uxTypeName = d => d.type === 'object' && uxParamBuiltin[d.name]?.points ? '坐标点列表' : uxTypeNames[d.type] || d.type;

// ux05 (Q6-07): the declared options as [{value, label}] -- `options` from the desktop (typed values with labels),
// else plain `choices`. The value is sent exactly as declared (1 stays a number, "1" a string).
function uxOptions(d) {
  if (Array.isArray(d?.options) && d.options.length && d.options.every(o => o && typeof o === 'object' && 'value' in o)) {
    return d.options.map(o => ({value: o.value, label: String(o.label ?? o.value)}));
  }
  return Array.isArray(d?.choices) ? d.choices.map(c => ({value: c, label: typeof c === 'string' ? c : JSON.stringify(c)})) : [];
}
const uxOptionLabels = d => uxOptions(d).map(o => o.label);

// ux03: human labels. skill.json `title` / `description` / `unit` win; this small built-in map supplies
// titles (and a description) when a skill does not declare them. The raw key stays visible.
// ux04 (Q5-10): no units here -- a third-party `timeout` may be milliseconds, a `speed` rad/s.
const uxParamBuiltin = {
  waypoints: {title: '航点', description: '机器人依次前往的位置，每个点是 x、y 坐标', points: 2},
  loops: {title: '圈数', description: '整条路线走几遍'},
  tolerance: {title: '到达容差', description: '离航点多近就算到达'},
  cycles: {title: '次数', description: '来回挥动几次'},
  amplitude: {title: '幅度', description: '摆动的幅度'},
  goal: {title: '目标点'}, target: {title: '目标'}, speed: {title: '速度'},
  max_speed: {title: '最高速度'}, duration: {title: '时长'}, timeout: {title: '超时'},
  timeout_s: {title: '超时'}, distance: {title: '距离'}, angle: {title: '角度'},
  height: {title: '高度'}, count: {title: '数量'}, interval: {title: '间隔'},
};
function uxParamInfo(d) {
  const b = uxParamBuiltin[d.name] || {};
  // Q5-10: the unit only ever comes from the skill's own declaration; the built-in map gives titles only.
  return {title: d.title || b.title || d.name, unit: d.unit || '', description: d.description || b.description || ''};
}
// "圈数（loops）" in messages; just the key when there is no better name.
const uxParamName = d => { const t = uxParamInfo(d).title; return t === d.name ? d.name : `${t}（${d.name}）`; };

const uxIsPointList = v => Array.isArray(v) && v.length > 0 &&
  v.every(p => Array.isArray(p) && p.length === v[0].length && p.length >= 2 && p.length <= 7 && p.every(x => typeof x === 'number'));

function uxPointRow(point, dims, unit = '') {
  const labels = dims === 2 ? ['x', 'y'] : dims === 3 ? ['x', 'y', 'z'] : null;
  return `<div class="point-row">${point.map((x, i) =>
    `<label><span>${labels ? labels[i] : i + 1}</span><input type="number" step="any" data-axis="${i}" value="${h(x)}" required>${unit && labels ? `<em class="param-unit">${h(unit)}</em>` : ''}</label>`).join('')}` +
    `<button type="button" class="button secondary small" data-ux-point="remove" aria-label="删除这个点">删除</button></div>`;
}

function uxParamControl(decl, value) {
  const name = 'param:' + decl.name;
  const has = value !== undefined;
  const t = decl.type;
  // ux04: the range is kept as data, not as min/max attributes: the browser's own bubble would block the
  // submit with a generic message; ours names the parameter and the allowed range (uxCheckValue).
  const range = [decl.min !== undefined ? `data-min="${h(decl.min)}"` : '', decl.max !== undefined ? `data-max="${h(decl.max)}"` : ''].join(' ');
  // ux05 (Q6-07): any parameter with declared choices/options is a select showing the labels; each <option>
  // carries the index, and the form sends options[index].value -- the raw, typed value.
  const opts = t === 'bool' ? [] : uxOptions(decl);
  if (opts.length) {
    const at = has ? opts.findIndex(o => o.value === value) : -1;
    const blank = decl.required || (has && at >= 0) ? '' : `<option value="">${has ? '（请选择）' : '（使用技能默认值）'}</option>`;
    return `<select name="${h(name)}" data-ptype="choice">${blank}${opts.map((o, i) => `<option value="${i}" ${i === at ? 'selected' : ''}>${h(o.label)}</option>`).join('')}</select>`;
  }
  if (t === 'int' || t === 'float') {
    // step="any": an integer check in the browser would block submit with a generic message;
    // ours says which parameter and why.
    return `<input name="${h(name)}" type="number" step="${h(decl.step || 'any')}" ${range} value="${has ? h(value) : ''}" ${decl.required ? 'required' : ''} data-ptype="${t}">`;
  }
  if (t === 'bool') return `<label class="inline-check"><input name="${h(name)}" type="checkbox" data-ptype="bool" ${value ? 'checked' : ''}> 是</label>`;
  if (t === 'str' || t === 'blob_ref') return `<input name="${h(name)}" type="text" value="${has ? h(value) : ''}" ${decl.required ? 'required' : ''} data-ptype="str">`;
  if (['vector', 'floats', 'pose'].includes(t)) {
    return `<input name="${h(name)}" type="text" inputmode="decimal" placeholder="用逗号分隔，例如 0, 0.5, 1" value="${has && Array.isArray(value) ? h(value.join(', ')) : ''}" ${decl.required ? 'required' : ''} data-ptype="${t}">`;
  }
  // An empty coordinate list we know (e.g. waypoints): start with one editable point instead of a JSON box.
  if (value === undefined && t === 'object' && uxParamBuiltin[decl.name]?.points) value = [Array(uxParamBuiltin[decl.name].points).fill(0)];
  if (uxIsPointList(value)) {
    const dims = value[0].length, unit = dims <= 3 ? uxParamInfo(decl).unit : '';
    return `<div class="point-list" data-param="${h(decl.name)}" data-ptype="points" data-dims="${dims}" data-unit="${h(unit)}">${value.map(p => uxPointRow(p, dims, unit)).join('')}` +
      `<button type="button" class="button secondary small" data-ux-point="add">添加一个点</button></div>`;
  }
  return `<textarea name="${h(name)}" class="mono param-json" spellcheck="false" data-ptype="object" ${decl.required ? 'required' : ''}>${has ? h(JSON.stringify(value, null, 2)) : ''}</textarea>`;
}

// The control plus its unit, side by side (ux03).
function uxParamInput(decl, value) {
  const control = uxParamControl(decl, value), unit = uxParamInfo(decl).unit;
  const inline = unit && !/class="point-list"|<textarea|type="checkbox"/.test(control);
  return `<div class="param-input">${control}${inline ? `<span class="param-unit">${h(unit)}</span>` : ''}</div>`;
}

function uxParamNote(decl) {
  const info = uxParamInfo(decl);
  const bits = [uxTypeName(decl)];
  if (info.unit) bits.push('单位 ' + info.unit);
  else if (['float', 'vector', 'floats'].includes(decl.type)) bits.push('单位未声明');
  if (decl.min !== undefined || decl.max !== undefined) bits.push('范围 ' + (decl.min ?? '不限') + ' – ' + (decl.max ?? '不限'));
  if (uxOptions(decl).length && decl.type !== 'enum') bits.push('可选 ' + uxOptionLabels(decl).join(' / '));
  if (decl.length) bits.push(decl.length + ' 个数值');
  const dflt = 'default' in decl ? (uxOptions(decl).length ? uxChoiceLabel(decl.default, {options: uxOptions(decl)}) : JSON.stringify(decl.default)) : '';
  bits.push(decl.required ? '必填' : 'default' in decl ? '默认 ' + dflt : '可不填');
  return (info.description ? h(info.description) + '<br>' : '') + `<span class="param-meta">${h(bits.join(' · '))}</span>`;
}

function uxParamLabel(decl) {
  const title = uxParamInfo(decl).title;
  return `<span class="param-label">${h(title)}${title !== decl.name ? ` <span class="param-key mono">${h(decl.name)}</span>` : ''}</span>`;
}

// decls: parameter declarations from skill.json; preset: suggested or current values.
// Everything sits in one .param-scope, so a form can hold several (one per application step).
function skillParamsForm(decls, preset, opts = {}) {
  const values = {};
  for (const d of decls) {
    if (preset && d.name in preset) values[d.name] = preset[d.name];
    else if ('default' in d) values[d.name] = d.default;
  }
  const raw = JSON.stringify(Object.keys(preset || {}).length ? preset : values, null, 2);
  const fields = decls.length
    ? decls.map(d => `<div class="field param-field" data-param-field="${h(d.name)}">${uxParamLabel(d)}${uxParamInput(d, values[d.name])}<small>${uxParamNote(d)}</small></div>`).join('')
    : '<p class="inline-note">这个技能没有需要填写的参数。</p>';
  return `<div class="param-scope"><fieldset class="param-form" data-decls="${h(JSON.stringify(decls))}"><legend>${h(opts.legend || '运行参数')}</legend>${fields}</fieldset>` +
    `<details class="page-purpose raw-params"><summary>高级：直接编辑 JSON</summary>` +
    `<p class="inline-note">展开时按这里的 JSON ${opts.legend ? '保存' : '运行'}；收起后回到表单。</p>` +
    `<textarea name="parameters" spellcheck="false" class="mono">${h(raw)}</textarea></details></div>`;
}

// ux04 (Q5-05): one check of a value against its declaration (type, min/max, choices), used by both the
// form and the "高级：直接编辑 JSON" path, so nothing outside the declared range is sent.
function uxCheckValue(d, v) {
  const t = d.type;
  if (t === 'int' || t === 'float') {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '需要填数字';
    if (t === 'int' && !Number.isInteger(v)) return '需要整数';
  } else if (t === 'bool') {
    if (typeof v !== 'boolean') return '需要 true 或 false';
  } else if (t === 'str' || t === 'blob_ref' || t === 'enum') {
    if (typeof v !== 'string') return '需要文字';
  } else if (['vector', 'floats', 'pose'].includes(t)) {
    if (!Array.isArray(v) || v.some(x => typeof x !== 'number' || !Number.isFinite(x))) return '需要一组数字';
    const want = t === 'pose' ? 7 : d.length;
    if (want && v.length !== want) return `需要 ${want} 个数字，现在是 ${v.length} 个`;
  }
  if (typeof v === 'number') {
    if (d.min !== undefined && v < d.min) return `不能小于 ${d.min}（允许范围 ${d.min} – ${d.max ?? '不限'}）`;
    if (d.max !== undefined && v > d.max) return `不能大于 ${d.max}（允许范围 ${d.min ?? '不限'} – ${d.max}）`;
  }
  // Strict, like the runtime (Q6-06/Q6-07): "1" is not 1, true is not 1.
  const opts = uxOptions(d);
  if (opts.length && !opts.some(o => o.value === v)) return '只能选 ' + opts.map(o => o.label).join(' / ');
  return '';
}

function uxParamProblems(problems) {
  const err = Error('请修改这些参数：' + problems.map(p => p.text).join('；'));
  err.fields = problems;
  return err;
}

function uxParamsFromForm(scope) {
  const set = scope.querySelector('.param-form');
  if (!set) return {};
  const decls = JSON.parse(set.dataset.decls || '[]');
  const out = {}, problems = [];
  const bad = (d, text) => problems.push({name: d.name, text: `${uxParamName(d)}：${text}`});
  for (const d of decls) {
    const wrap = set.querySelector(`[data-param-field="${CSS.escape(d.name)}"]`);
    if (!wrap) continue;
    const pts = wrap.querySelector('[data-ptype="points"]');
    if (pts) {
      const list = [...pts.querySelectorAll('.point-row')].map(r => [...r.querySelectorAll('input')].map(i => Number(i.value)));
      if (list.some(p => p.some(x => !Number.isFinite(x)))) bad(d, '每个点都要填数字');
      else if (!list.length && d.required) bad(d, '至少需要一个点');
      else out[d.name] = list;
      continue;
    }
    const el = wrap.querySelector('[name^="param:"]');
    if (!el) continue;
    const t = el.dataset.ptype;
    if (t === 'bool') { out[d.name] = el.checked; continue; }
    const text = el.value.trim();
    if (!text) { if (d.required) bad(d, '必填'); continue; }
    let value;
    if (t === 'choice') {
      const opt = uxOptions(d)[Number(text)];
      if (!opt) { bad(d, '请从列表里选择'); continue; }
      value = opt.value;                         // the raw declared value, typed
    } else if (t === 'int' || t === 'float') {
      value = Number(text);
    } else if (['vector', 'floats', 'pose'].includes(t)) {
      const nums = text.split(/[\s,，]+/).filter(Boolean).map(Number);
      if (nums.some(x => !Number.isFinite(x))) { bad(d, '只能填数字，用逗号分隔'); continue; }
      value = nums;
    } else if (t === 'object') {
      try { value = JSON.parse(text); } catch { bad(d, '格式不对（需要 JSON，例如 [[1, 0], [1, 1]]）'); continue; }
    } else {
      value = text;
    }
    const problem = t === 'object' ? '' : uxCheckValue(d, value);
    if (problem) { bad(d, problem); continue; }
    out[d.name] = value;
  }
  if (problems.length) throw uxParamProblems(problems);
  return out;
}

// The raw JSON is checked against the same declarations before anything is sent (Q5-05).
function uxCheckRawParams(scope, value) {
  const set = scope.querySelector('.param-form');
  if (!set) return;                              // no declarations known: the device decides
  const decls = JSON.parse(set.dataset.decls || '[]');
  const problems = [];
  const known = new Set(decls.map(d => d.name));
  for (const key of Object.keys(value)) {
    if (!known.has(key)) problems.push({name: '', text: `${key}：这个技能没有这个参数`});
  }
  for (const d of decls) {
    if (!(d.name in value)) {
      if (d.required) problems.push({name: d.name, text: `${uxParamName(d)}：必填`});
      continue;
    }
    const problem = d.type === 'object' ? '' : uxCheckValue(d, value[d.name]);
    if (problem) problems.push({name: d.name, text: `${uxParamName(d)}：${problem}`});
  }
  if (problems.length) throw uxParamProblems(problems);
}

// Parameters of a run form or an application step: raw JSON when the advanced block is open (or when the
// skill declares no parameters and only a JSON box is shown), otherwise the generated form.
function skillParamsCollect(scope) {
  const raw = scope.querySelector('.raw-params'), text = scope.querySelector('textarea[name="parameters"]');
  if (raw?.open || (!scope.querySelector('.param-form') && text)) {
    let value;
    try { value = JSON.parse(text.value.trim() || '{}'); } catch { throw Error('运行参数不是合法 JSON，请检查引号、逗号和括号'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('运行参数须为 JSON 对象，例如 {"loops": 1}');
    uxCheckRawParams(scope, value);
    return value;
  }
  return uxParamsFromForm(scope);
}

document.addEventListener('click', e => {
  const b = e.target.closest('[data-ux-point]');
  if (!b) return;
  e.preventDefault();
  const list = b.closest('.point-list');
  if (b.dataset.uxPoint === 'remove') {
    if (list.querySelectorAll('.point-row').length > 1) b.closest('.point-row').remove();
    return;
  }
  const dims = Number(list.dataset.dims) || 2;
  const last = [...list.querySelectorAll('.point-row')].pop();
  const seed = last ? [...last.querySelectorAll('input')].map(i => Number(i.value) || 0) : Array(dims).fill(0);
  b.insertAdjacentHTML('beforebegin', uxPointRow(seed, dims, list.dataset.unit || ''));
});

// Keep the form and the raw JSON in step when the advanced block is toggled.
document.addEventListener('toggle', e => {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || !d.classList.contains('raw-params')) return;
  const f = d.closest('.param-scope') || d.closest('form');
  const err = d.closest('form')?.querySelector('.form-error');
  const text = f.querySelector('textarea[name="parameters"]');
  if (d.open) {
    try { text.value = JSON.stringify(uxParamsFromForm(f), null, 2); if (err) err.textContent = ''; }
    catch (x) { /* keep the previous JSON; the form shows what is wrong on submit */ }
    d.dataset.synced = '1';
    return;
  }
  delete d.dataset.synced;
  let value;
  try { value = JSON.parse(text.value || '{}'); } catch { return; }
  const set = f.querySelector('.param-form');
  const decls = JSON.parse(set?.dataset.decls || '[]');
  for (const decl of decls) {
    const wrap = set.querySelector(`[data-param-field="${CSS.escape(decl.name)}"]`);
    if (wrap && decl.name in value) wrap.querySelector('.param-input').outerHTML = uxParamInput(decl, value[decl.name]);
  }
}, true);

// ---------------------------------------------------------------- 4. assistant model (工程助手)
// What the model is for, the three ways to get one, and what each needs -- instead of a bare picker.
function engineModelsDialog(reason) {
  const models = rows('agent_models');
  const logged = typeof cloudAccount === 'function' && cloudAccount().logged_in;
  const offers = state.catalog.filter(x => x.kind === 'reasoning');
  const why = reason === 'needed' && !models.length
    ? `<section class="plan-review model-why"><h3>这一步需要先给助手配一个大模型</h3><p>工程助手靠大模型阅读机器人资料、编写接入程序和测试。你刚才要做的事需要它，配好后回到这里继续即可。</p></section>`
    : `<section class="plan-review model-why"><h3>助手模型是做什么的</h3><p>工程助手靠大模型阅读机器人资料、编写接入程序和测试。它与机器人身上运行的模型（模型库）分开管理。</p></section>`;
  const option = (title, needs, cost, action, extra = '') =>
    `<article class="card model-option"><div class="card-body"><h3>${title}</h3><p><b>需要：</b>${needs}</p><p><b>费用：</b>${cost}</p>${extra}</div><div class="card-bottom">${action}</div></article>`;
  const cloud = option('NeuxMind 云平台模型', '一个云平台账户（登录即可）。', '从云平台钱包按用量扣费。',
    logged ? button('使用云平台模型', 'engine-cloud-model', 'small') : button('先登录云平台账户', 'go-cloud', 'secondary small'),
    logged ? `<p class="inline-note">已登录：${h(cloudAccount().email || '')}</p>` : '<p class="inline-note">登录云平台账户后即可选用。</p>');
  const platform = option('平台付费模型', '平台运营方已上架工程模型。', '从本机工作空间额度按用量扣费。',
    offers.length ? button('使用平台付费模型', 'engine-platform-model', 'secondary small')
      : `<button type="button" class="button secondary small" disabled>暂未上架</button>`,
    offers.length ? '' : '<p class="inline-note">当前还没有可选的平台工程模型。</p>');
  const own = option('自己的模型 API', '模型服务的 API 地址、模型 ID 和密钥。服务需支持 OpenAI Responses 接口与工具调用（OpenAI 或兼容网关均可）。',
    '由该模型服务商直接收取；密钥只保存在这台电脑上。', button('配置自己的模型', 'engine-own-model', 'secondary small'));
  const skip = models.length ? '' : notice('暂时不想配置？添加仿真机器人、安装示例技能、运行并观看它动起来，都不需要助手模型。 ' + button('去我的机器人', 'go-robots', 'secondary small'));
  return dialog('选择工程模型', '工程助手需要一个大模型才能工作',
    `<div class="dialog-body">${why}${models.length ? engineModelsSummary() : ''}<div class="cards model-options">${cloud}${platform}${own}</div>${skip}</div>`);
}

// ---------------------------------------------------------------- 5. installed skills -> project skills
// The skills already installed on a robot (e.g. the example "巡逻") become project skills an application can use.
async function uxInstalledSkills() {
  const out = [];
  for (const r of rows('robots').filter(r => r.status === 'connected')) {
    try {
      const detail = await api('robots/detail', {robot_id: r.id, project_id: projectId});
      for (const s of detail.skills.items || []) out.push({robot: r, skill: s});
    } catch { /* a robot that cannot be read is skipped */ }
  }
  return out;
}

const uxAdopted = s => rows('skills').find(x => x.entrypoint === s.id && x.version === s.version);

async function ux02Action(type, id) {
  if (type === 'ux-adopt') {
    const items = await uxInstalledSkills();
    const body = items.length
      ? items.map(({robot, skill}) => {
          const done = uxAdopted(skill);
          return `<div class="contract-line"><div class="section-head"><b>${h(skill.title)}</b>${done ? badge('已加入项目', 'good')
            : button('加入项目', 'ux-adopt-one', 'small', robot.id + '|' + skill.id)}</div>` +
            `<p>${h(skill.description)}</p><small>安装在：${h(robot.name)}${isSimRobot(robot) ? ' ' + simBadge() : ''} · <span class="mono">${h(skill.id)}@${h(skill.version)}</span></small></div>`;
        }).join('') + `<div class="button-row">${button('创建应用', 'application-add', 'small')}</div>`
      : empty('机器人上还没有安装技能', '先在“我的机器人 → 能力与技能”里安装一个技能（可以从示例技能库里选）。',
          rows('robots')[0] ? button('安装技能', 'skillpkg-open', 'small', rows('robots')[0].id) : button('添加仿真机器人', 'sim-add', 'small'));
    return dialog('使用机器人上已安装的技能', '加入项目后，就能把它编排进机器人应用', `<div class="dialog-body">${body}</div>`);
  }
  if (type === 'ux-adopt-one') {
    const [robotId, skillId] = String(id).split('|');
    const items = await uxInstalledSkills();
    const hit = items.find(x => x.robot.id === robotId && x.skill.id === skillId);
    if (!hit) throw Error('找不到这台机器人上的这个技能，可能已被卸载');
    const s = hit.skill;
    if (!uxAdopted(s)) {
      await api('skills', {project_id: projectId, name: s.title, version: s.version, entrypoint: s.id,
        requires: s.requires.map(x => x.id), model_ids: [], parameters: s.parameters || [],
        success_condition: '技能运行结束，设备报告成功',
        stop_condition: '设备报告失败、急停或超过时限时停止并报告'});
      await refresh(true);
    }
    toast('“' + s.title + '”已加入项目，可以在应用里使用');
    return ux02Action('ux-adopt');
  }
}
