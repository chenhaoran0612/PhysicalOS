'use strict';
// Issue #70 — the page of one record (#record), opened from a row of web/run_boards.js.
//
// What it shows, top to bottom: what was asked and how it ended; the numbers of the run; where this
// robot and application stand along the five stages (接入 → 安装技能 → 仿真 → 真机测试 → 部署运行);
// the route the robot took and its speed, battery and progress over time (from the samples of
// brainos/run_records.py); the steps and the result; the device's events; the record as it is stored.
// A part without data is not drawn, and a run without collected data says why.
//
// The functions down to rrEventTime are pure (data in, markup or facts out): tests/run_boards_logic.cjs.

const RR_POLL_MS = 2000;
const RR_PATH = {w: 520, h: 300, pad: 26};
const RR_LINE = {w: 520, h: 120, padX: 40, padY: 14};
const RR_SERIES = [['speed', '速度', '米/秒'], ['battery', '电量', '%'], ['progress', '进度', '']];
const RR_STEP = {succeeded: ['good', '完成'], failed: ['bad', '失败'], skipped: ['', '没有执行'], stopped: ['warn', '被停止'], running: ['blue', '进行中']};
const RR_EVENT = {started: '开始运行', 'step.started': '步骤开始', 'step.finished': '步骤结束', progress: '进度', log: '日志', timeout: '超时',
                  cancel_requested: '收到停止请求', exception: '出错', finished: '运行结束'};

const rrEsc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const rrNum = (v, digits = 2) => (typeof v === 'number' && Number.isFinite(v) ? String(Math.round(v * 10 ** digits) / 10 ** digits) : '—');

// The sampled positions as points of the drawing: {points: [[px, py]], bounds} or null without two positions.
function rrPathPoints(samples, box = RR_PATH) {
  const at = (samples || []).filter(s => typeof s.x === 'number' && typeof s.y === 'number');
  if (at.length < 2) return null;
  const xs = at.map(s => s.x), ys = at.map(s => s.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const span = Math.max(maxX - minX, maxY - minY, 0.5);                       // metres across; a robot that stood still gets half a metre
  const scale = Math.min(box.w - 2 * box.pad, box.h - 2 * box.pad) / span;
  const offX = (box.w - (maxX - minX) * scale) / 2, offY = (box.h - (maxY - minY) * scale) / 2;
  const points = at.map(s => [Math.round((offX + (s.x - minX) * scale) * 10) / 10, Math.round((box.h - offY - (s.y - minY) * scale) * 10) / 10]);   // y up, as on a map
  return {points, bounds: {minX, maxX, minY, maxY}, span, scale};
}

function rrPathSvg(samples) {
  const drawn = rrPathPoints(samples);
  if (!drawn) return '';
  const {points, span, scale} = drawn, first = points[0], last = points[points.length - 1];
  const metre = span > 20 ? 10 : span > 8 ? 5 : span > 2 ? 1 : 0.5, bar = Math.round(metre * scale);
  return `<svg class="rr-path" viewBox="0 0 ${RR_PATH.w} ${RR_PATH.h}" role="img" aria-label="机器人这次运行走过的路线">` +
    `<polyline points="${points.map(p => p.join(',')).join(' ')}" />` +
    `<circle class="start" cx="${first[0]}" cy="${first[1]}" r="6" /><circle class="end" cx="${last[0]}" cy="${last[1]}" r="6" />` +
    `<g class="scale"><line x1="16" y1="${RR_PATH.h - 14}" x2="${16 + bar}" y2="${RR_PATH.h - 14}" /><text x="${22 + bar}" y="${RR_PATH.h - 10}">${metre} 米</text></g></svg>`;
}

// One value over time: {points, min, max, end} or null with fewer than two values.
function rrLinePoints(samples, key, box = RR_LINE) {
  const at = (samples || []).filter(s => typeof s[key] === 'number' && typeof s.t === 'number');
  if (at.length < 2) return null;
  const values = at.map(s => s[key]), t0 = at[0].t, t1 = at[at.length - 1].t;
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < 1e-9) { min -= 0.5; max += 0.5; }
  const px = s => box.padX + (t1 > t0 ? (s.t - t0) / (t1 - t0) : 0) * (box.w - box.padX - 8);
  const py = s => box.h - box.padY - (s[key] - min) / (max - min) * (box.h - 2 * box.padY);
  return {points: at.map(s => [Math.round(px(s) * 10) / 10, Math.round(py(s) * 10) / 10]), min: Math.min(...values), max: Math.max(...values), seconds: t1 - t0};
}

function rrLineSvg(samples, key, name, unit) {
  const drawn = rrLinePoints(samples, key);
  if (!drawn) return '';
  const show = v => (key === 'progress' ? Math.round(v * 100) + '%' : rrNum(v) + (unit ? ' ' + unit : ''));
  return `<figure class="rr-series"><figcaption><b>${rrEsc(name)}</b><span>${rrEsc(show(drawn.min))} – ${rrEsc(show(drawn.max))}</span></figcaption>` +
    `<svg viewBox="0 0 ${RR_LINE.w} ${RR_LINE.h}" role="img" aria-label="${rrEsc(name)}随时间的变化，最低 ${rrEsc(show(drawn.min))}，最高 ${rrEsc(show(drawn.max))}">` +
    `<line class="axis" x1="${RR_LINE.padX}" y1="${RR_LINE.h - RR_LINE.padY}" x2="${RR_LINE.w - 8}" y2="${RR_LINE.h - RR_LINE.padY}" />` +
    `<polyline points="${drawn.points.map(p => p.join(',')).join(' ')}" /></svg></figure>`;
}

// "+12.3 秒" from the start of the run; '' when the event has no time the page can read.
function rrEventTime(event, startedAt) {
  const seconds = v => { const t = typeof v === 'number' ? v * 1000 : Date.parse(v); return Number.isFinite(t) ? t / 1000 : null; };
  const at = seconds(event.ts), from = seconds(startedAt);
  if (at === null || from === null) return '';
  return '+' + Math.max(0, Math.round((at - from) * 10) / 10) + ' 秒';
}

// ---------------------------------------------------------------- what the page keeps

let rrFull = {};                 // key -> {operation, record, following, missing} from runs/record
let rrAsked = {};                // key -> the summary text it was asked for, so one state is asked once
let rrFailed = {};               // key -> why the record could not be read

async function rrLoad(key, force = false) {
  if (!key.startsWith('op:')) return;
  const summary = JSON.stringify(rbRecords.find(r => 'op:' + r.operation_id === key) || null);
  if (!force && rrAsked[key] === summary && !(rrFull[key] || {}).following) return;
  rrAsked[key] = summary;
  try {
    const full = await api('runs/record', {project_id: projectId, operation_id: key.slice(3)});
    delete rrFailed[key];
    if (JSON.stringify(full) === JSON.stringify(rrFull[key])) return;
    rrFull[key] = full;
  } catch (error) {
    rrFailed[key] = error.message || '读不到这条记录';
  }
  if (current === 'record' && rbOpened().key === key && !$('#dialog').open) rrRedraw();
}

// Redraws the page and leaves it as the person had it: the opened folds stay open, the list of events stays where it was scrolled to.
function rrRedraw() {
  const opened = [...document.querySelectorAll('#main .rr-raw')].map(d => d.open), scrolled = $('#main .rr-events')?.scrollTop || 0;
  render();
  document.querySelectorAll('#main .rr-raw').forEach((d, i) => { d.open = Boolean(opened[i]); });
  if ($('#main .rr-events')) $('#main .rr-events').scrollTop = scrolled;
}

const rrRefresh = () => { const key = rbOpened().key; if (key) rrLoad(key, Boolean(rrFailed[key])); };
const rrRetry = () => { const key = rbOpened().key; if (!key) return; delete rrFailed[key]; rrLoad(key, true); render(); };

// ---------------------------------------------------------------- markup

const rrFact = (name, value, note = '') => `<div><dt>${h(name)}</dt><dd>${value}</dd>${note ? `<small>${h(note)}</small>` : ''}</div>`;

function rrFlow(entry) {
  if (!entry.robot_id && !entry.application_id) return '';
  const steps = rbFlow(state, rbAll(), entry.robot_id, entry.application_id);
  return `<section class="rr-block"><h2>这台机器人走到了哪一步</h2><ol class="rr-flow">${steps.map((s, i) =>
    `<li class="${s.state}"><span>${i + 1}</span><b>${h(s.name)}</b><small>${h(s.text)}${s.at ? ' · ' + date(s.at) : ''}</small></li>`).join('')}</ol></section>`;
}

function rrMotion(record) {
  const samples = record ? record.samples || [] : [];
  const path = rrPathSvg(samples), lines = RR_SERIES.map(([key, name, unit]) => rrLineSvg(samples, key, name, unit)).join('');
  if (!path && !lines) return '';
  const at = samples.filter(s => typeof s.x === 'number'), first = at[0], last = at[at.length - 1];
  return `<section class="rr-block"><h2>运行过程</h2><div class="rr-motion">` +
    (path ? `<figure class="rr-map">${path}<figcaption><i class="start"></i>起点（${rrNum(first.x)}, ${rrNum(first.y)}）<i class="end"></i>终点（${rrNum(last.x)}, ${rrNum(last.y)}）` +
      `${record.distance_m ? ` · 共 ${h(record.distance_m)} 米` : ''}</figcaption></figure>` : '') +
    `<div class="rr-lines">${lines}</div></div>` +
    `<p class="rr-note">每 ${h(rrNum(record.sample_interval_s || 1, 1))} 秒记录一次，共 ${samples.length} 个点。</p></section>`;
}

function rrSteps(steps, title) {
  if (!steps || !steps.length) return '';
  return `<section class="rr-block"><h2>${h(title)}</h2><ol class="rr-steps">${steps.map((s, i) => {
    const [tone, text] = Object.hasOwn(RR_STEP, String(s.status)) ? RR_STEP[s.status] : ['', s.status || '—'];
    return `<li><span>${i + 1}</span><div><b>${h(s.name || '步骤 ' + (i + 1))}</b>${s.detail ? `<small>${h(s.detail)}</small>` : ''}</div>${badge(text, tone)}</li>`;
  }).join('')}</ol></section>`;
}

function rrEvents(record) {
  const events = record ? record.events || [] : [];
  if (!events.length) return '';
  return `<section class="rr-block"><h2>设备记下的事件 <span class="badge-count">${events.length}</span></h2><ol class="rr-events">${events.map(e =>
    `<li class="${h(String(e.type || '').replace(/[^a-z]/g, '-'))}"><time>${h(rrEventTime(e, record.started_at))}</time><b>${h((Object.hasOwn(RR_EVENT, String(e.type)) && RR_EVENT[e.type]) || e.type || '事件')}` +
    `${typeof e.index === 'number' ? ` ${e.index + 1}` : ''}</b><span>${h([e.message, e.error].filter(Boolean).join(' · '))}</span></li>`).join('')}</ol></section>`;
}

const rrValues = (title, value) => (value === undefined || value === null || (typeof value === 'object' && !Object.keys(value).length) ? ''
  : `<section class="rr-block"><h2>${h(title)}</h2>${typeof value === 'object' && !Array.isArray(value)
    ? `<dl class="rr-values">${Object.entries(value).map(([k, v]) => `<div><dt>${h(k)}</dt><dd>${h(typeof v === 'object' ? JSON.stringify(v) : v)}</dd></div>`).join('')}</dl>`
    : `<p class="rr-plain">${h(typeof value === 'object' ? JSON.stringify(value) : value)}</p>`}</section>`);

// The cases of a report as steps; what is not a case is left out.
const rrCases = cases => (Array.isArray(cases) ? cases : []).filter(c => c && typeof c === 'object').map(c => ({name: c.name, status: c.passed ? 'succeeded' : 'failed', detail: c.detail}));

const rrRaw = (title, value) => (value ? `<details class="rr-raw"><summary>${h(title)}</summary>${json(value)}</details>` : '');

function rrHead(entry, from) {
  const names = {simulation: '仿真试运行', harness: '验证与测试', deployment: '部署与运行'};
  return `<div class="tw-back"><a href="#${h(from)}" data-action="rb-back">← ${h(names[from] || '返回')}</a></div>` +
    `<div class="page-head rr-head"><div><div class="eyebrow">RUN RECORD</div><h1>${h(entry.name)}${entry.subject ? ' · ' + h(entry.subject) : ''}</h1></div>` +
    `<div class="button-row">${rbBadge(entry.status)}${entry.sim ? simBadge() : ''}</div></div>`;
}

function rrOperation(entry) {
  const full = rrFull[entry.key], record = full ? full.record : null, operation = full ? full.operation : find('operations', entry.id);
  const report = entry.report_id ? find('evaluations', entry.report_id) : null;
  const facts = [
    rrFact('机器人', h(entry.robot || '—'), entry.tasks.map(t => t.name).join('、')),
    rrFact('发出时间', date(entry.at)),
    rrFact('用时', h(rbDuration(record ? record.duration_s : entry.duration_s) || '—'), record && record.stop_reason ? '停止原因：' + record.stop_reason : ''),
    rrFact('走过的距离', h(record && record.distance_m ? record.distance_m + ' 米' : '—')),
    entry.steps ? rrFact('步骤', h(`${entry.steps_passed} / ${entry.steps}`)) : '',
  ].join('');
  const error = (record && record.error) || (operation && operation.error) || '';
  const waiting = !full && !rrFailed[entry.key] ? '<p class="rr-note">正在读取这次运行的数据…</p>' : '';
  const missing = rrFailed[entry.key] ? notice(h(rrFailed[entry.key]) + ' ' + button('再读一次', 'rb-retry', 'secondary small'), true)
    : full && !record && ['skill_run', 'onsite', 'start'].includes(entry.kind) ? notice(h(full.missing || '这次运行的数据没有收集到')) : '';
  return `<dl class="rr-facts">${facts}</dl>` +
    (error ? `<div class="notice warn"><span>↗</span><div>${h(error)}</div></div>` : '') +
    (record && record.note ? notice(h(record.note)) : '') + waiting + missing +
    (full && full.following ? '<p class="rr-live" role="status"><i></i>正在运行，数据每两秒更新一次。</p>' : '') +
    rrFlow(entry) + rrMotion(record) +
    rrSteps(record && record.steps && record.steps.length ? record.steps : report ? rrCases((report.details || {}).cases) : [],
      entry.kind === 'onsite' ? '测试的每一步' : '步骤') +
    rrValues('运行参数', operation && operation.parameters) +
    rrValues('技能返回的结果', record && !(record.result && record.result.steps) ? record.result : null) +
    rrEvents(record) +
    rrRaw('这条操作记录', operation) + rrRaw('测试报告', report) + rrRaw('收集到的运行数据', record ? {...record, samples: `${(record.samples || []).length} 个采样点`} : null);
}

function rrReport(entry) {
  const report = find('evaluations', entry.id);
  if (!report) return '';
  const cases = Array.isArray(report.cases) ? report.cases : (report.details || {}).cases || [];
  return `<dl class="rr-facts">${[rrFact('机器人', h(entry.robot || '—'), entry.tasks.map(t => t.name).join('、')), rrFact('时间', date(entry.at)),
    rrFact('范围', h(typeof reportScopeName === 'function' ? reportScopeName(report) : entry.name)), rrFact('检查项', h(`${entry.steps_passed} / ${entry.steps}`))].join('')}</dl>` +
    rrFlow(entry) + rrSteps(rrCases(cases), '检查的每一项') + rrRaw('这份报告', report);
}

function rrRelease(entry, all) {
  const d = find('deployments', entry.id);
  const mine = all.filter(e => e.release_id === d.id), runs = mine.filter(e => e.kind === 'start'), totals = rbTotals(runs);
  const report = d.report_id ? all.find(e => e.report_id === d.report_id) : null;
  return `<dl class="rr-facts">${[rrFact('机器人', h(named('robots', d.robot_id))), rrFact('发布时间', date(d.created_at)),
    rrFact('启动工作', h(runs.length + ' 次'), totals.rate === null ? '' : `${totals.pass} 次完成，${totals.fail} 次失败`),
    rrFact('累计运行', h((totals.seconds && rbDuration(totals.seconds)) || '—'), totals.metres ? `走过 ${totals.metres} 米` : '')].join('')}</dl>` +
    (d.error ? notice(h(d.error), true) : '') +
    `<div class="button-row rr-actions">${button('启动工作', 'operate-start', 'small', d.id, d.status !== 'deployed' || deploymentLatched(d))}` +
    `${button('请求停止', 'operate-stop', 'secondary small', d.id, d.status !== 'deployed')}${report ? button('发布所依据的测试', 'rb-open', 'secondary small', report.key) : ''}</div>` +
    '<p class="rr-note">停止请求不能替代设备硬件急停。</p>' +
    rrFlow({robot_id: d.robot_id, application_id: d.application_id}) +
    rbListSection('这个版本的运行与操作', mine, '<div class="empty"><b>还没有启动过</b>启动工作之后，每次运行的结果和数据会列在这里。</div>') + rrRaw('这条发布记录', d);
}

function rrView() {
  const opened = rbOpened(), from = RB_PAGES.includes(opened.from) ? opened.from : 'harness';
  const all = rbAll();
  const release = opened.key && opened.key.startsWith('dep:') ? find('deployments', opened.key.slice(4)) : null;
  const entry = release
    ? {key: opened.key, id: release.id, name: RB_KIND.release, subject: release.application_name, sim: false,
       status: {key: Object.hasOwn(RB_OP, String(release.status)) ? RB_OP[release.status][0] : 'none', text: stateNames[release.status] || release.status}}
    : all.find(e => e.key === opened.key);
  if (!entry) {
    return `<div class="tw-back"><a href="#${h(from)}" data-action="rb-back">← 返回</a></div>` +
      (last ? `<section class="panel">${empty('找不到这条记录', '它可能已经被删除。', button('返回列表', 'rb-back', 'secondary small'))}</section>` : '');
  }
  if (entry.key.startsWith('op:') && !rrFull[entry.key] && !rrFailed[entry.key]) rrLoad(entry.key);
  return rrHead(entry, from) + `<div class="rr-page">${release ? rrRelease(entry, all) : entry.source === 'report' ? rrReport(entry) : rrOperation(entry)}</div>`;
}

// The sidebar and the breadcrumb know the page as part of the list it was opened from.
function rrChrome() {
  const from = RB_PAGES.includes(rbOpened().from) ? rbOpened().from : 'harness';
  const link = document.querySelector(`#navigation a[href="#${from}"]`), crumb = document.getElementById('breadcrumb');
  if (link) { link.classList.add('active'); link.setAttribute('aria-current', 'page'); }
  if (crumb) crumb.textContent = `${(navigation.find(x => x[0] === from) || [])[1] || ''} / 记录`;
}

if (typeof document !== 'undefined') setInterval(() => { if (current === 'record' && state.user && !document.hidden) { const key = rbOpened().key; if (key && (rrFull[key] || {}).following) rrLoad(key, true); } }, RR_POLL_MS);
