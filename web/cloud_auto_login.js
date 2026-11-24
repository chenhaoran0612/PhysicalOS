// Issue #36：登录应用时用同一邮箱和密码同时登录云平台（资料库）。
// 这里只负责说明：云平台没有登录时，是自动登录没成功（原因由本地服务给出），还是登录到期了。
// 到期时，重新登录应用即可同时登录云平台；其余情况仍然去“云平台账户”页处理。
function cloudAutoLoginInfo() {
  return (state.cloud_account || {}).auto_login || null;
}

// 一次云平台登录的标记：账户邮箱 + 最近一次（自动或手工）登录、退出的时间。同一账户重新登录后它会变，
// 页面据此重新读取，而不是留着登录到期时得到的那次回答。
function cloudSignInKey() {
  const account = state.cloud_account || {};
  return `${account.email || ''}|${(account.auto_login || {}).at || ''}`;
}

// 需要云平台的页面在未登录时显示的提示。sentence 是这个页面原来的那句话（可信的固定文字）。
function cloudSignInNotice(sentence) {
  const info = cloudAutoLoginInfo();
  if (info && info.relogin) {
    return notice(`<span data-cloud-auto-login="expired">${h(info.message)}</span>` +
      `<div class="button-row">${button('重新登录', 'logout', 'small')}` +
      `${button('只登录云平台', 'go-cloud', 'secondary small')}</div>`);
  }
  const reason = info && info.message
    ? `<p class="inline-note" data-cloud-auto-login="${h(info.status)}">${h(info.message)}</p>` : '';
  return notice(sentence + reason + `<div class="button-row">${button('去登录云平台', 'go-cloud', 'small')}</div>`);
}

// “云平台账户”页未登录时，登录按钮上方的一行说明（没有可说的就是空串）。
function cloudAutoLoginNote() {
  const info = cloudAutoLoginInfo();
  if (!info || !info.message || cloudAccount().logged_in) return '';
  const again = info.relogin ? `<div class="button-row">${button('重新登录', 'logout', 'small')}</div>` : '';
  return notice(`<span data-cloud-auto-login="${h(info.relogin ? 'expired' : info.status)}">${h(info.message)}</span>${again}`);
}
