'use strict';
// 第 02 轮（INTERFACES §4.1）：异步现场测试。桌面端轮询设备上的测试状态；
// 测试进行中“请求停止”始终可用；结果未知时只记录，不重试。

let livetestBusy = false;

function runningOnsiteTests() {
  return state.operations.filter(o => o.kind === 'onsite' && o.status === 'running' && o.evaluation_id);
}

async function livetestTick() {
  if (livetestBusy || !state.user) return;
  const running = runningOnsiteTests().slice(0, 3);
  if (!running.length) return;
  livetestBusy = true;
  let finished = null;
  try {
    for (const op of running) {
      const result = await api('onsite/poll', {operation_id: op.id});
      if (result.status !== 'running') finished = result;
      livetestStatusLine(result);
    }
  } catch (err) {
    // A failed read is shown on the next tick; the test keeps running on the device.
  } finally {
    livetestBusy = false;
  }
  if (finished) {
    await refresh(true);
    const openApp = $('#dialog').open ? $('#dialog').dataset.deliveryApp : '';
    if (openApp && openApp === finished.application_id) await deliveryOpen(openApp);
    toast(finished.status === 'succeeded' ? '现场测试已结束，结果已保存' : '现场测试结果待核对');
  }
}

function livetestStatusLine(op) {
  const el = document.querySelector(`[data-livetest-status="${CSS.escape(op.id)}"]`);
  if (!el) return;
  el.textContent = livetestStatusText(op);
}

function livetestStatusText(op) {
  const polled = op.polled_at ? '最近查询 ' + new Date(op.polled_at).toLocaleTimeString('zh-CN', {hour12: false}) : '等待第一次查询';
  return op.poll_error ? `${polled} · 暂时读不到设备状态：${op.poll_error}` : polled;
}

function deliveryTestingSection(d) {
  const op = d.onsite_running;
  if (!op) return '';
  const progress = typeof op.progress === 'number'
    ? `<div class="progress-track"><i style="width:${Math.round(op.progress * 100)}%"></i></div>` : '';
  return `<section class="plan-review onsite-running"><h3><span class="pulse" aria-hidden="true"></span>现场测试进行中</h3>` +
    `<p>设备正在执行这份应用的 ${d.application.steps.length} 个工作步骤，${date(op.created_at)} 开始。结果返回后这里会自动更新。` +
    `如现场出现异常，可随时请求停止；停止后本次测试记为未通过。</p>${progress}` +
    `<p class="inline-note" data-livetest-status="${h(op.id)}">${h(livetestStatusText(op))}</p>` +
    `<div class="button-row">${button('请求停止', 'livetest-stop', 'danger', d.robot.id + ':' + d.application.id)}` +
    `${button('立即刷新状态', 'livetest-refresh', 'secondary small', d.application.id)}</div>` +
    `<p class="inline-note">软件停止请求不能替代现场硬件急停。</p></section>`;
}

async function livetestAction(type, id) {
  if (type === 'livetest-refresh') {
    await livetestTick();
    return deliveryOpen(id);
  }
  if (type === 'livetest-stop') {
    const [robotId, appId] = String(id).split(':');
    // Stop is sent at once: no confirmation step stands between the user and a stop.
    const result = await api('robots/stop', {robot_id: robotId, application_id: appId || undefined, project_id: projectId});
    toast(result.status === 'accepted' ? '设备已确认停止请求' : '停止结果待核对：' + (result.error || '请立即到现场确认'));
    await livetestTick();
    await refresh(true);
    if (appId) return deliveryOpen(appId);
  }
}

setInterval(livetestTick, 1500);
