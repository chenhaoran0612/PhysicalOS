'use strict';
// ux05 (round 07): a first-run guide on 任务工作台 for a brand-new user.
//   1. 添加仿真机器人  2. 安装示例技能  3. 运行并观看
// Each step is checked off from the workspace's own records (robots and operations), never from clicks, so a
// step done elsewhere (e.g. from 我的机器人) is ticked too. Dismissible; the choice is a per-browser convenience.
// Nothing here grants an execution right: authorization, e-stop and sim-only rules stay on the server.

const UX_FIRSTRUN_KEY = 'nx-first-run-guide-dismissed';
let uxFirstRunClosed = false;              // this page session, even when browser storage is unavailable

function uxFirstRunDismissed() {
  if (uxFirstRunClosed) return true;
  try { return localStorage.getItem(UX_FIRSTRUN_KEY) === '1'; } catch { return false; }
}

function uxFirstRunState() {
  const all = state.robots || [];
  const sims = all.filter(isSimRobot);
  const mine = sims.filter(r => r.project_id === projectId);
  const robot = mine[0] || sims[0] || null;
  const simIds = new Set(sims.map(r => r.id));
  const ops = (state.operations || []).filter(o => simIds.has(o.robot_id));
  const installed = ops.some(o => o.kind === 'skill_install' && o.status === 'succeeded');
  const ran = ops.some(o => o.kind === 'skill_run' && o.status === 'accepted');
  return {robot, real: all.some(r => !isSimRobot(r)), done: [Boolean(robot), installed, ran]};
}

// Shown to a user who has no real robot yet and has not closed it.
function uxFirstRunGuide() {
  if (!state || uxFirstRunDismissed()) return '';
  const s = uxFirstRunState();
  if (s.real) return '';
  const r = s.robot, live = r && r.sim && r.status === 'connected';
  const steps = [
    ['添加仿真机器人', '在这台电脑上启动一台虚拟的四足机器人，不用接线，也不需要真机。',
      button('添加仿真机器人', 'sim-add', 'small')],
    ['安装示例技能', '从示例库装一个技能（例如“巡逻”），装之前会先检查它和机器人是否匹配。',
      live ? button('安装示例技能', 'skillpkg-open', 'small', r.id) : ''],
    ['运行并观看', '确认周围安全、授权运行，然后在“实时状态”里看它动起来；随时可以按“停止”。',
      live ? button('运行并观看', 'sim-run-pick', 'small', r.id) : ''],
  ];
  const next = s.done.indexOf(false);
  const all = next < 0;
  const blocked = !all && next > 0 && !live
    ? `<p class="inline-note">仿真机器人现在没有运行。到“我的机器人”里启动它，就能继续。</p>${button('去我的机器人', 'go-robots', 'secondary small')}` : '';
  return `<section class="first-run" data-first-run="${all ? 'done' : 'open'}" aria-label="首次使用引导">` +
    `<div class="first-run-head"><div><span class="eyebrow">首次使用引导</span>` +
    `<h2>${all ? '三步都完成了' : '三步看到机器人动起来'}</h2>` +
    `<p>${all ? '你已经在仿真机器人上运行过技能。接下来可以把技能编排成应用，或接入一台真机。' : '不需要真机、助手模型或任何配置。每做完一步，这里会自动打勾。'}</p></div>` +
    `<button type="button" class="button secondary small" data-first-run-dismiss>${all ? '知道了，收起' : '不需要引导，关闭'}</button></div>` +
    `<ol class="first-run-steps">${steps.map(([t, d, b], i) => {
      const done = s.done[i], current = i === next;
      return `<li class="${done ? 'done' : current ? 'current' : 'later'}" data-first-run-step="${i + 1}" data-done="${done ? '1' : '0'}">` +
        `<span class="first-run-mark" aria-hidden="true">${done ? '✓' : i + 1}</span>` +
        `<div><b>${h(t)}</b>${done ? '<em>已完成</em>' : ''}<p>${h(d)}</p>${current && b ? b : ''}${current && !b ? blocked : ''}</div></li>`;
    }).join('')}</ol></section>`;
}

{
  const orig = views.overview;
  views.overview = () => {
    const html = orig();
    const guide = uxFirstRunGuide();
    if (!guide) return html;
    const at = html.indexOf('<section class="journey-next">');
    return at >= 0 ? html.slice(0, at) + guide + html.slice(at) : guide + html;
  };
}

document.addEventListener('click', e => {
  if (!e.target.closest?.('[data-first-run-dismiss]')) return;
  e.preventDefault();
  uxFirstRunClosed = true;
  try { localStorage.setItem(UX_FIRSTRUN_KEY, '1'); } catch { /* the guide simply comes back next time */ }
  e.target.closest('.first-run')?.remove();
});

// app.js renders the first screen before this file loads (logged out, nothing re-renders it): draw it again.
if (typeof current !== 'undefined' && current === 'overview' && typeof render === 'function') render();
