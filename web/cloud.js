'use strict';
// 第 02 轮（INTERFACES §4.5）：云平台账户——地址、注册/登录、邮箱验证、钱包、适合当前机器人的模型。
// 会话令牌只保存在本机工作空间的凭据文件中，页面只拿到账户摘要。

let cloudView = {loaded: false, loading: false, overview: null, models: null, modelsError: '', error: '', robotId: '', probe: null};

function cloudAccount() {
  return state.cloud_account || {configured: false, logged_in: false, base_url: ''};
}

function cloudReset() {
  cloudView = {...cloudView, loaded: false, overview: null, models: null, modelsError: '', error: '', probe: null};
}

// ux01: before login, check whether a cloud answers at all and say so in plain words.
function cloudDefaultUrl() {
  return cloudAccount().default_base_url || 'http://127.0.0.1:8800';
}

function cloudRobots() {
  return rows('robots').filter(r => r.status === 'connected');
}

async function cloudLoad() {
  if (cloudView.loading) return;
  cloudView.loading = true;
  try {
    cloudView.error = '';
    cloudView.probe = null;
    if (!cloudAccount().logged_in) {
      try { cloudView.probe = await api('cloud/probe', {}); } catch (err) { cloudView.probe = {reachable: false, message: err.message}; }
    }
    cloudView.overview = cloudAccount().logged_in ? await api('cloud/overview', {}) : null;
    if (cloudView.overview?.error) cloudView.error = cloudView.overview.error;
    const robots = cloudRobots();
    if (!robots.some(r => r.id === cloudView.robotId)) cloudView.robotId = robots[0]?.id || '';
    cloudView.models = null;
    cloudView.modelsError = '';
    if (cloudAccount().logged_in && cloudView.robotId && !cloudView.error) {
      try {
        cloudView.models = await api('cloud/models', {robot_id: cloudView.robotId, project_id: projectId});
      } catch (err) {
        cloudView.modelsError = err.message;
      }
    }
  } catch (err) {
    cloudView.error = err.message;
  } finally {
    cloudView.loading = false;
    cloudView.loaded = true;
    // The overview may have dropped an expired session: reread the summary.
    await refresh(true);
    if (current === 'cloud') render();
  }
}

function cloudProbeNotice() {
  const p = cloudView.probe;
  if (!p) return notice(cloudView.loading || !cloudView.loaded ? '正在检查能否连上云平台…' : '还没有检查云平台。');
  if (p.reachable) return notice('已连上云平台 <span class="mono">' + h(p.base_url) + '</span>。');
  return `<div class="notice warn cloud-unreachable"><span>↗</span><div><b>暂时没有可用的云平台</b><p>${h(p.message || '')}</p></div></div>`;
}

function cloudAddressPanel(a) {
  const p = cloudView.probe;
  let body;
  if (a.configured) {
    body = `<p class="inline-note">当前地址</p><p class="mono cloud-address">${h(a.base_url)}</p>` +
      (a.insecure_http ? notice('这是本机地址，使用 http 连接，仅适合本地开发。', true) : '') +
      (!a.logged_in ? cloudProbeNotice() : '');
  } else if (p?.reachable) {
    body = notice(`在这台电脑上找到了 NeuxMind 云平台：<span class="mono">${h(p.base_url)}</span>`) +
      `<div class="button-row">${button('使用这个地址', 'cloud-use-default', 'small', p.base_url)}</div>`;
  } else {
    body = cloudProbeNotice() +
      `<p class="inline-note">云平台地址由你的团队或平台运营方提供，例如 https://cloud.example.com。` +
      `在这台电脑上运行云平台时，地址是 <span class="mono">${h(cloudDefaultUrl())}</span>。</p>`;
  }
  const buttons = `${button(a.configured ? '修改地址' : '填写云平台地址', 'cloud-address', a.configured || p?.reachable ? 'secondary small' : 'small')}` +
    (!a.logged_in ? button('重新检查', 'cloud-refresh', 'secondary small') : '');
  return panel('云平台地址', `<div class="panel-body">${body}<div class="spacer"></div><div class="button-row">${buttons}</div></div>`);
}

function cloudAccountPanel(a) {
  if (!a.configured) {
    return panel('账户', `<div class="panel-body"><p class="subtitle">云平台账户用来查看钱包余额、购买和授权模型。` +
      `先在右侧设置云平台地址，再登录或注册。</p>` +
      notice('不登录云平台也可以使用本机功能：添加仿真机器人、安装示例技能并观察它运行。' +
        `<div class="button-row">${button('去添加仿真机器人', 'go-robots', 'secondary small')}</div>`) + '</div>');
  }
  if (!a.logged_in) {
    return panel('账户', `<div class="panel-body">${cloudView.error ? notice(h(cloudView.error), true) : ''}` +
      `<p class="subtitle">使用 NeuxMind 云平台账户登录。没有账户可以直接注册。</p><div class="spacer"></div>` +
      `<div class="button-row">${button('登录云平台', 'cloud-login', 'small')}${button('注册新账户', 'cloud-register', 'secondary small')}</div></div>`);
  }
  const verified = a.email_verified
    ? badge('邮箱已验证', 'good')
    : badge('邮箱未验证', 'warn');
  const unverified = a.email_verified ? '' :
    notice('邮箱尚未验证：可以浏览模型目录和查看钱包；购买点数、模型授权和调用云端模型需要先完成验证。' +
      `<div class="button-row">${button('输入验证令牌', 'cloud-verify', 'small')}${button('重新发送验证邮件', 'cloud-resend', 'secondary small')}</div>`, true);
  const org = cloudView.overview?.organization ? `<div class="runtime-line"><span>组织</span><b>${h(cloudView.overview.organization)}</b></div>` : '';
  return panel('账户', `<div class="panel-body"><div class="runtime-list">` +
    `<div class="runtime-line"><span>登录邮箱</span><b>${h(a.email)}</b></div>` +
    `<div class="runtime-line"><span>名称</span><b>${h(a.name || '—')}</b></div>${org}` +
    `<div class="runtime-line"><span>邮箱验证</span>${verified}</div></div>${unverified}` +
    `<div class="spacer"></div>${button('退出云平台账户', 'cloud-logout', 'secondary small')}</div>`);
}

function cloudWallet() {
  const w = cloudView.overview?.wallet;
  if (!cloudAccount().logged_in) return '';
  if (!w) return `<div class="section-head"><h2>钱包</h2></div>` + notice(cloudView.loading ? '正在读取钱包…' : '暂时无法读取钱包。');
  return `<div class="section-head"><h2>钱包</h2><span>token 点 · 余额只来自云平台账本</span></div><div class="stats cloud-wallet">` +
    [[w.available, '可用'], [w.reserved, '已预留'], [w.balance, '账户余额']].map(([n, t]) =>
      `<div class="stat"><div class="stat-top">${t}</div><div class="stat-number">${h(n ?? '—')}</div><div class="stat-note">点</div></div>`).join('') +
    `</div>`;
}

const cloudModelKinds = {llm: '大语言模型', vla: '视觉-语言-动作', vision: '视觉', speech: '语音', policy: '控制策略'};
const cloudModelSources = {native: 'NeuxMind 自研', open: '开源', closed: '闭源服务'};

function cloudPricing(p) {
  if (!p) return '—';
  return {per_1k_tokens: `${p.credits} 点 / 千 token`, per_call: `${p.credits} 点 / 次`,
    license_once: `一次授权 ${p.credits} 点`, free: '免费'}[p.type] || '—';
}

function cloudModels() {
  if (!cloudAccount().logged_in) return '';
  const robots = cloudRobots();
  const head = `<div class="section-head"><h2>适合这台机器人的模型</h2>` +
    (robots.length ? `<select class="cloud-robot" data-cloud-robot aria-label="选择机器人">${robots.map(r =>
      `<option value="${h(r.id)}" ${r.id === cloudView.robotId ? 'selected' : ''}>${h(r.name)}</option>`).join('')}</select>` : '') + '</div>';
  if (!robots.length) return head + notice('当前项目还没有已连接的机器人。连接机器人后，这里按它的能力清单筛选模型。');
  if (cloudView.modelsError) return head + notice(h(cloudView.modelsError), true);
  const m = cloudView.models;
  if (!m) return head + notice('正在按机器人能力筛选模型…');
  const source = m.robot.manifest_source === 'v2' ? '按标准能力清单筛选' : '按 v1 能力名称换算后筛选（无参数信息）';
  const note = `<p class="inline-note">${h(source)}${m.robot.simulated ? ' · 当前机器人是仿真本体' : ''}。只列出机器人具备所需能力、形态与端侧推理运行时匹配的模型。</p>`;
  if (!m.models.length) return head + note + empty('没有适合这台机器人的模型', '云平台目录中的模型都需要这台机器人不具备的能力，或尚未上架模型。');
  return head + note + `<div class="cards">${m.models.map(x =>
    `<article class="card"><div class="card-body"><div class="card-top"><span class="model-symbol">${icon('models')}</span>` +
    `${badge(cloudModelSources[x.source] || x.source, 'good')}</div><h3>${h(x.name)}</h3>` +
    `<p>${h(cloudModelKinds[x.kind] || x.kind)} · v${h(x.version)}</p><div class="tags">` +
    `<span class="tag">${h(cloudPricing(x.pricing))}</span><span class="tag">${x.gateway ? '云端调用' : '端侧部署包'}</span>` +
    `${(x.requires_capabilities || []).map(c => `<span class="tag">${h(c)}</span>`).join('')}</div></div>` +
    `<div class="card-bottom"><small>${x.entitled ? '已获授权' : '尚未授权'}</small></div></article>`).join('')}</div>`;
}

function cloud() {
  const a = cloudAccount();
  if (!cloudView.loaded && !cloudView.loading) setTimeout(cloudLoad);
  return heading('CLOUD PLATFORM', '云平台账户', '配置工程助手使用的 TokenFans 大模型；登录 NeuxMind 云平台，查看钱包余额与适合当前机器人能力的模型。',
    a.logged_in ? button('刷新', 'cloud-refresh', 'secondary') : '') +
    // Issue #4: the large models the desktop runs come only from TokenFans, configured here.
    (typeof modelComputePanel === 'function' ? modelComputePanel() + '<div class="spacer"></div>' : '') +
    `<div class="grid-two">${cloudAccountPanel(a)}${cloudAddressPanel(a)}</div>` + cloudWallet() + cloudModels();
}

function cloudCredentialFields(register) {
  return (register ? field('名称（可选）', '<input name="name" maxlength="100" autocomplete="name">') : '') +
    field('邮箱', input('email', 'you@company.com', '', 'email')) +
    field('密码', `<input name="password" type="password" minlength="10" maxlength="256" autocomplete="${register ? 'new-password' : 'current-password'}" required>`,
      '至少 10 个字符。密码只发送给云平台，不保存在本机。');
}

function cloudDevTokenBox(v) {
  if (!v?.dev_token) return '';
  return `<div class="dev-token"><div>${badge('仅开发环境', 'warn')}</div>` +
    `<p>云平台运行在开发/测试环境，没有发送真实邮件（verification.simulated = true）。下面的开发令牌只能用于本地测试，正式环境中令牌只会出现在邮件里。</p>` +
    `<p class="mono output-reference">${h(v.dev_token)}</p>` +
    `<button type="button" class="button small" data-action="cloud-verify-dev" data-id="${h(v.dev_token)}">用开发令牌完成验证</button></div>`;
}

function cloudVerificationDialog(result, title) {
  const v = result.verification || {};
  const status = {sent: '验证邮件已发送，请查收后输入邮件中的令牌。',
    unavailable: '云平台没有配置邮件发送；请联系平台运营人员人工核实你的邮箱。',
    failed: '验证邮件发送失败，请稍后重新发送。'}[v.status] || '';
  dialog(title, h(result.account?.email || ''), `<div class="dialog-body">` +
    `<p>${h(status)}</p>${cloudDevTokenBox(v)}<div class="spacer"></div>` +
    `<div class="button-row">${button('输入验证令牌', 'cloud-verify', 'secondary small')}${button('完成', 'close', 'small')}</div></div>`);
}

async function cloudAction(type, id) {
  if (type === 'cloud-address') {
    return form('云平台地址', '只接受 https；本机开发服务可以使用 http。更换地址会退出当前云平台账户。',
      field('地址', input('base_url', 'https://cloud.example.com', cloudAccount().base_url || cloudView.probe?.base_url || cloudDefaultUrl()),
        `已预填在这台电脑上运行云平台时的地址；如果团队提供了云平台地址，请替换。`), 'cloud-address', '保存地址');
  }
  if (type === 'cloud-login') return form('登录云平台', h(cloudAccount().base_url), cloudCredentialFields(false), 'cloud-login', '登录');
  if (type === 'cloud-register') return form('注册云平台账户', h(cloudAccount().base_url), cloudCredentialFields(true), 'cloud-register', '注册');
  if (type === 'cloud-verify') {
    return form('验证邮箱', '输入验证邮件中的令牌（以 nxv_ 开头）。',
      field('验证令牌', input('token', 'nxv_…')), 'cloud-verify', '验证');
  }
  if (type === 'cloud-verify-dev') {
    await api('cloud/verify', {token: id});
    $('#dialog').close();
    toast('邮箱已验证');
    cloudReset();
    return refresh(true);
  }
  if (type === 'cloud-resend') {
    const result = await api('cloud/resend', {});
    return cloudVerificationDialog(result, '重新发送验证邮件');
  }
  if (type === 'cloud-logout') {
    await api('cloud/logout', {});
    toast('已退出云平台账户');
    cloudReset();
    return refresh(true);
  }
  if (type === 'cloud-use-default') {
    await api('cloud/configure', {base_url: id});
    toast('云平台地址已保存');
    cloudReset();
    return refresh(true);
  }
  if (type === 'cloud-refresh') {
    cloudReset();
    render();
    return;
  }
  if (type === 'cloud-models-for') {
    cloudView.robotId = id;
    cloudReset();
    $('#dialog').close();
    location.hash = 'cloud';
  }
}

async function cloudSubmit(kind, p, f) {
  if (kind === 'cloud-address') await api('cloud/configure', {base_url: p.base_url});
  if (kind === 'cloud-login') await api('cloud/login', {email: p.email, password: p.password});
  if (kind === 'cloud-verify') await api('cloud/verify', {token: p.token});
  if (kind === 'cloud-register') {
    const result = await api('cloud/register', {email: p.email, password: p.password, name: p.name});
    cloudReset();
    await refresh(true);
    if (result.status === 'claim_sent') {
      return dialog('该邮箱需要认领', '', `<div class="dialog-body"><p>${h(result.message)}</p>${button('知道了', 'close', 'small')}</div>`);
    }
    return cloudVerificationDialog(result, '注册成功');
  }
  cloudReset();
  await refresh(true);
  $('#dialog').close();
  toast({'cloud-address': '云平台地址已保存', 'cloud-login': '已登录云平台', 'cloud-verify': '邮箱已验证'}[kind] || '已保存');
}

document.addEventListener('change', e => {
  const select = e.target.closest('[data-cloud-robot]');
  if (!select) return;
  cloudView.robotId = select.value;
  cloudReset();
  render();
});
