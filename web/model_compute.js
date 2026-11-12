'use strict';
// Issue #4：模型与算力——工程助手的大模型只对接 TokenFans，在“云平台账户”页配置。本体调查、工程任务、协作流程与本体适配都使用这里选定的默认模型。
// API Key 只保存在本机工作空间的凭据文件中；账号密码只用于一次换取 Key，不会保存。页面只拿到连接摘要。

function modelCompute() {
  return state.model_compute || {connected: false, models: []};
}

// The current project's managed TokenFans profile: pre-selected in every model picker.
function modelComputeDefaultId() {
  const mc = modelCompute();
  if (!mc.connected || !mc.default_model) return '';
  return rows('agent_models').find(m => m.billing === 'tokenfans')?.id || '';
}

function modelComputeBalance(b) {
  if (!b || typeof b.minor !== 'number') return '';
  const amount = (b.minor / 100).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return (b.currency === 'CNY' ? '¥ ' : b.currency + ' ') + amount;
}

const modelComputeUses = '本体调查、工程任务、协作流程与本体适配';

function modelComputeConnectChoices() {
  return `<div class="tkf-choices">
    <div class="tkf-choice"><b>粘贴 API Key</b><p>在 TokenFans 控制台的账户总览复制以 <span class="mono">sk-tkf-</span> 开头的 Key。</p>${button('粘贴 API Key', 'tkf-key', 'small')}</div>
    <div class="tkf-choice"><b>用 TokenFans 账号登录</b><p>输入邮箱和密码，自动读取账户的 API Key 与余额。密码只用这一次，不会保存。</p>${button('账号登录', 'tkf-login', 'secondary small')}</div>
  </div>`;
}

function modelComputeDefaultForm(mc) {
  const options = mc.models.map(m => [m, m]);
  const missing = !mc.default_model
    ? notice('还没有默认模型（原来的默认模型可能已不在 TokenFans 可用列表中）。选择一个模型后，工程助手才能调用模型。', true) : '';
  return `${missing}<form data-form="tkf-default" class="tkf-default"><div class="form-error" role="alert"></div>
    ${field('默认模型', select('model', options, mc.default_model || ''), `${modelComputeUses}使用它；已建的协作流程保持创建时的模型。列表来自 TokenFans 的 Codex 模型目录（${mc.models.length} 个）。`)}
    <div class="button-row"><button class="button small" type="submit">设为默认模型</button>${button('刷新模型列表', 'tkf-refresh', 'secondary small')}</div></form>`;
}

function modelComputeDetails(mc) {
  const balance = mc.balance
    ? `${h(modelComputeBalance(mc.balance))}<small class="muted">（登录时 ${h(date(mc.balance_at))}）</small>`
    : '<span class="muted">用账号登录可显示余额</span>';
  return `<div class="detail-meta tkf-meta">
    <div><b>账户</b>${mc.email ? h(mc.email) : '通过 API Key 连接'}</div>
    <div><b>API Key</b><span class="mono">${h(mc.key_hint || '')}</span></div>
    <div><b>余额</b>${balance}</div>
    <div><b>工程引擎地址</b><span class="mono">${h(mc.engine_base_url || '')}</span></div>
  </div>`;
}

function modelComputePanel() {
  const mc = modelCompute();
  const head = `<div class="panel-head"><h2>TokenFans 模型与算力</h2>${mc.connected ? badge('已连接', 'good') : badge('未连接')}</div>`;
  if (!mc.connected) {
    return `<section class="panel tkf-panel">${head}<div class="panel-body">
      <p class="tkf-lead">工程助手的大模型只对接 TokenFans：${modelComputeUses}都使用你在这里选定的默认模型，费用从你的 TokenFans 账户余额扣除。连接前，这些功能无法调用模型。</p>
      ${modelComputeConnectChoices()}
      <p class="inline-note">接入说明：<span class="mono">${h(mc.docs_url || 'https://tokenfans.ai/integrations/api')}</span>。账号登录使用 TokenFans 控制台接口（不在其公开 API 文档中）；登录失败时可改用 API Key。</p>
    </div></section>`;
  }
  return `<section class="panel tkf-panel">${head}<div class="panel-body">
    ${modelComputeDetails(mc)}${modelComputeDefaultForm(mc)}
    <div class="tkf-foot"><span class="inline-note">工程任务按 TokenFans 账单计费，Token 观察限额仍然生效。</span>
    <div class="button-row">${button('更换 Key', 'tkf-key', 'secondary small')}${button('重新登录', 'tkf-login', 'secondary small')}${button('断开连接', 'tkf-disconnect', 'secondary small')}</div></div>
  </div></section>`;
}

// The only option in the "工程模型" dialog (ux02.js).
function modelComputeOption(option) {
  const mc = modelCompute();
  const ready = mc.connected && mc.default_model;
  return option('TokenFans 模型', '一个 TokenFans 账户：在“云平台账户”页粘贴 API Key 或用账号登录。',
    '从 TokenFans 账户余额按用量扣费；Key 只保存在这台电脑上。',
    button(mc.connected ? '管理 TokenFans' : '连接 TokenFans', 'go-cloud', mc.connected ? 'secondary small' : 'small'),
    ready ? `<p class="inline-note">已连接：所有项目使用 <span class="mono">${h(mc.default_model)}</span>。</p>`
      : mc.connected ? '<p class="inline-note">已连接，但还没有默认模型。</p>' : '<p class="inline-note">连接后，所有项目都使用它。</p>');
}

// 平台模型与算力 page: the TokenFans settings moved to 云平台账户 (issue #4 follow-up).
function modelComputePointer() {
  const mc = modelCompute();
  const state = mc.connected ? `已连接 TokenFans，默认模型 <span class="mono">${h(mc.default_model || '未选择')}</span>。` : '尚未连接 TokenFans。';
  return notice(`工程助手的大模型只对接 TokenFans，在“云平台账户”页配置。${state} ` + button(mc.connected ? '管理 TokenFans' : '连接 TokenFans', 'go-cloud', 'secondary small'));
}

async function modelComputeAction(type) {
  if (type === 'tkf-key') {
    return form('粘贴 TokenFans API Key', '连接前会用这个 Key 读取 TokenFans 模型目录来校验',
      field('API Key', '<input name="api_key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-tkf-…" required>',
        '在 TokenFans 控制台 → 账户总览复制。Key 只保存在这台电脑的工作空间凭据文件中，页面不会再显示完整 Key。'), 'tkf-key', '校验并连接');
  }
  if (type === 'tkf-login') {
    return form('用 TokenFans 账号登录', '登录一次，读取账户的 API Key 与余额',
      field('邮箱', '<input name="email" type="email" autocomplete="username" required>') +
      field('密码', '<input name="password" type="password" autocomplete="current-password" required>', '密码只用于这一次登录，不会保存；读取 Key 后会立即退出 TokenFans 控制台会话。') +
      notice('账号登录使用 TokenFans 控制台接口（不在其公开 API 文档中），如果失败请改用“粘贴 API Key”。'), 'tkf-login', '登录并连接');
  }
  if (type === 'tkf-refresh') {
    await api('model-compute/models', {});
    await refresh(true);
    return toast('已刷新 TokenFans 模型列表');
  }
  if (type === 'tkf-disconnect') {
    return dialog('断开 TokenFans', '删除这台电脑上保存的 TokenFans API Key',
      `<div class="dialog-body"><p>断开后，工程助手无法调用模型，本体调查、工程任务、协作流程与本体适配都要等重新连接后才能启动；进行中的流程在下一步会提示重新连接；正在运行的工程会话会用已取得的 Key 跑完当前这一轮。</p>
      <div class="button-row">${button('断开连接', 'tkf-disconnect-confirm', 'danger small')}${button('取消', 'close', 'secondary small')}</div></div>`);
  }
  if (type === 'tkf-disconnect-confirm') {
    await api('model-compute/disconnect', {});
    await refresh(true);
    $('#dialog').close();
    return toast('已断开 TokenFans，本机保存的 Key 已删除');
  }
}

async function modelComputeSubmit(kind, p) {
  let summary;
  if (kind === 'tkf-key') summary = await api('model-compute/key', {api_key: p.api_key});
  if (kind === 'tkf-login') summary = await api('model-compute/login', {email: p.email, password: p.password});
  if (kind === 'tkf-default') summary = await api('model-compute/default', {model: p.model});
  await refresh(true);
  if ($('#dialog').open) $('#dialog').close();
  render();
  toast(kind === 'tkf-default' ? '默认模型已设为 ' + summary.default_model
    : '已连接 TokenFans，默认模型：' + (summary.default_model || '请选择'));
}
