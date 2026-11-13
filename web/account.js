// 账号：打开即为登录 / 注册页；注册与找回密码都要邮箱验证码；登录状态由 HttpOnly 会话 cookie 记住；
// 顶栏显示当前用户；账号面板可改名、改密码、退出。表单逐项校验（邮箱格式、密码规则与强度、两次输入一致、大写锁定），
// 密码可切换显示，验证码为 6 格输入（支持粘贴）。
let acctChecked=false,acctMode='login',acctPending=null,acctResendAt=0,acctTimer=0;
const acctEsc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const acctDate=t=>{if(!t)return '—';const d=new Date(t*1000);return d.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});};
const acctInitial=u=>(u?.name||u?.email||'?').trim().slice(0,1).toUpperCase();

/* ---------- 校验规则（与服务端 brainos/auth.py 保持一致） ---------- */
const ACCT_EMAIL_RE=/^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;
const ACCT_DOMAINS=['gmail.com','qq.com','163.com','126.com','outlook.com','hotmail.com','icloud.com','foxmail.com','sina.com','yahoo.com','aliyun.com'];
function acctTypo(email){
  const at=email.lastIndexOf('@');if(at<1)return '';
  const dom=email.slice(at+1).toLowerCase();if(!dom||ACCT_DOMAINS.includes(dom))return '';
  const dist=(a,b)=>{const d=Array.from({length:a.length+1},(_,i)=>[i]);for(let j=1;j<=b.length;j++)d[0][j]=j;
    for(let i=1;i<=a.length;i++)for(let j=1;j<=b.length;j++)d[i][j]=Math.min(d[i-1][j]+1,d[i][j-1]+1,d[i-1][j-1]+(a[i-1]===b[j-1]?0:1));return d[a.length][b.length];};
  const best=ACCT_DOMAINS.map(x=>[x,dist(dom,x)]).sort((a,b)=>a[1]-b[1])[0];
  return best&&best[1]>0&&best[1]<=2?email.slice(0,at+1)+best[0]:'';
}
const acctRules=pw=>({len:pw.length>=10,mix:/[A-Za-zÀ-￿]/.test(pw)&&/\d/.test(pw)});
function acctStrength(pw){
  if(!pw)return [0,''];
  const r=acctRules(pw);if(!r.len||!r.mix)return [1,'弱'];
  const pts=(pw.length>=14)+(/[a-z]/.test(pw)&&/[A-Z]/.test(pw))+(/[^A-Za-z0-9]/.test(pw));
  return pts<=1?[2,'一般']:pts===2?[3,'强']:[4,'很强'];
}

/* ---------- 表单片段 ---------- */
const ACCT_EYE='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
const ACCT_EYE_OFF='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18"/><path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';

// kind：email | name | password（登录）| new-password（设置新密码，带规则与强度）| confirm（data-match 指向要一致的字段）
function acctField(o){
  const id=(o.idp||'af-')+o.name,pw=o.kind==='password'||o.kind==='new-password'||o.kind==='confirm';
  const type=pw?'password':o.kind==='email'?'email':'text';
  const ac=o.ac||({email:'email',name:'name',password:'current-password','new-password':'new-password',confirm:'new-password'})[o.kind];
  return `<div class="af" data-af="${o.name}" data-kind="${o.kind}"${o.match?` data-match="${o.match}"`:''}>
   <div class="af-top"><label class="af-label" for="${id}">${o.label}</label>${o.aside||''}</div>
   <div class="af-box"><input id="${id}" name="${o.name}" type="${type}" autocomplete="${ac}" maxlength="${o.kind==='email'?200:o.kind==='name'?100:256}" value="${acctEsc(o.value||'')}" spellcheck="false" aria-describedby="${id}-msg"${o.autofocus?' autofocus':''}>
    ${pw?`<button type="button" class="af-eye" data-acct-eye aria-label="显示密码" aria-pressed="false">${ACCT_EYE}</button>`:''}</div>
   ${o.kind==='new-password'?`<div class="pw-meter" data-level="0" aria-hidden="true"><i></i><i></i><i></i><i></i><em></em></div>
   <ul class="pw-rules"><li data-rule="len">至少 10 个字符</li><li data-rule="mix">同时包含字母和数字</li></ul>`:''}
   <p class="af-msg" id="${id}-msg" aria-live="polite"></p>${pw?'<p class="af-caps" hidden>大写锁定已开启</p>':''}
  </div>`;
}
function acctOtp(){
  return `<div class="af" data-af="code" data-kind="code"><div class="af-top"><label class="af-label" for="af-code-0">验证码</label></div>
   <div class="otp" data-otp>${[0,1,2,3,4,5].map(i=>`<input id="af-code-${i}" inputmode="numeric" pattern="[0-9]*" autocomplete="${i?'off':'one-time-code'}" aria-label="验证码第 ${i+1} 位">`).join('')}</div>
   <input type="hidden" name="code"><p class="af-msg" aria-live="polite"></p></div>`;
}
const acctBanner='<div class="form-error" role="alert"></div>';
const acctSubmit=(label,busy)=>`<button class="button auth-submit" type="submit" data-busy="${busy}">${label}</button>`;
function acctSentTo(){
  const p=acctPending;if(!p)return '';
  return `<p class="auth-sub">验证码已发送至 <b>${acctEsc(p.email)}</b>，10 分钟内有效。</p>`+
   (p.delivery==='outbox'?`<div class="auth-devnote"><b>未配置邮件服务</b>验证码已写入 <code>${acctEsc(p.outbox)}</code></div>`:'');
}
const acctResend=()=>`<button type="button" class="text-link" data-acct-resend>重新发送</button>`;

function acctFormHtml(){
  const m=acctMode,p=acctPending||{};
  if(m==='register-code')return `<h1>验证邮箱</h1>${acctSentTo()}
   <form data-acct-form="register-code" novalidate>${acctBanner}${acctOtp()}${acctSubmit('完成注册','正在创建账号…')}</form>
   <p class="auth-switch">没收到？${acctResend()}<span class="auth-dot">·</span><button type="button" class="text-link" data-acct-mode="register">修改邮箱</button></p>`;
  if(m==='forgot')return `<h1>找回密码</h1><p class="auth-sub">输入注册时使用的邮箱，我们会发送验证码。</p>
   <form data-acct-form="forgot" novalidate>${acctBanner}${acctField({name:'email',label:'邮箱',kind:'email',value:p.email,autofocus:true})}${acctSubmit('发送验证码','正在发送…')}</form>
   <p class="auth-switch"><button type="button" class="text-link" data-acct-mode="login">返回登录</button></p>`;
  if(m==='reset')return `<h1>设置新密码</h1>${acctSentTo()}
   <form data-acct-form="reset" novalidate>${acctBanner}${acctOtp()}
    ${acctField({name:'password',label:'新密码',kind:'new-password'})}${acctField({name:'password2',label:'确认新密码',kind:'confirm',match:'password'})}
    ${acctSubmit('重置密码并登录','正在重置…')}</form>
   <p class="auth-switch">没收到？${acctResend()}<span class="auth-dot">·</span><button type="button" class="text-link" data-acct-mode="login">返回登录</button></p>`;
  if(m==='register')return `<h1>创建账号</h1>
   <form data-acct-form="register" novalidate>${acctBanner}
    ${acctField({name:'name',label:'名字',kind:'name',value:p.name,autofocus:true})}
    ${acctField({name:'email',label:'邮箱',kind:'email',value:p.email})}
    ${acctField({name:'password',label:'密码',kind:'new-password',value:p.password})}
    ${acctField({name:'password2',label:'确认密码',kind:'confirm',match:'password',value:p.password})}
    <label class="auth-remember"><input type="checkbox" name="remember"${p.remember===false?'':' checked'}> 保持登录</label>
    ${acctSubmit('继续','正在发送验证码…')}</form>
   <p class="auth-switch">已有账号？<button type="button" class="text-link" data-acct-mode="login">登录</button></p>`;
  return `<h1>登录</h1>
   <form data-acct-form="login" novalidate>${acctBanner}
    ${acctField({name:'email',label:'邮箱',kind:'email',value:p.email,autofocus:true})}
    ${acctField({name:'password',label:'密码',kind:'password',aside:'<button type="button" class="text-link" data-acct-mode="forgot">忘记密码？</button>'})}
    <label class="auth-remember"><input type="checkbox" name="remember" checked> 保持登录</label>
    ${acctSubmit('登录','正在登录…')}</form>
   <p class="auth-switch">还没有账号？<button type="button" class="text-link" data-acct-mode="register">创建账号</button></p>`;
}

function acctGateHtml(){
  return `<div class="auth-layout">
   <aside class="auth-aside" aria-hidden="true">
    <div class="auth-logo"><span class="brand-mark">n<span>↗</span></span><span>NeuxMind<small>ROBOT OS</small></span></div>
    <div class="auth-pitch"><h2>让你的机器人，<br>准备上岗。</h2>
     <ol class="auth-steps"><li>接入本体</li><li>开发应用</li><li>测试验证</li><li>部署上岗</li></ol></div>
    <small class="auth-legal">© ${new Date().getFullYear()} NeuxMind</small>
   </aside>
   <div class="auth-main" role="main"><div class="auth-card" data-mode="${acctMode}">
    <div class="auth-logo auth-logo-compact"><span class="brand-mark">n<span>↗</span></span><span>NeuxMind</span></div>
    ${acctFormHtml()}</div></div></div>`;
}

/* ---------- 逐项校验 ---------- */
const acctAf=(form,name)=>form.querySelector(`.af[data-af="${name}"]`);
function acctSetMsg(af,msg,html){
  if(!af)return;const el=af.querySelector('.af-msg');
  af.classList.toggle('is-invalid',!!msg);
  af.querySelectorAll('input:not([type=hidden])').forEach(i=>i.setAttribute('aria-invalid',msg?'true':'false'));
  if(el){if(html)el.innerHTML=html;else el.textContent=msg||'';}
}
function acctValidate(af,{final=false}={}){
  if(!af)return true;
  const kind=af.dataset.kind,form=af.closest('form');
  const input=af.querySelector('input[name]:not([type=hidden])')||af.querySelector('input[type=hidden]');
  const v=kind==='code'?String(form.querySelector('[name=code]').value):input.value;
  let msg='',html='';
  if(kind==='email'){
    const e=v.trim();
    if(!e)msg=final?'请输入邮箱':'';
    else if(!ACCT_EMAIL_RE.test(e))msg='邮箱格式不正确，例如 name@company.com';
    else{const fix=acctTypo(e);if(fix){msg=' ';html=`是不是 <button type="button" class="text-link" data-acct-fix="${acctEsc(fix)}">${acctEsc(fix)}</button>？`;}}
  }else if(kind==='name'){if(!v.trim()&&final)msg='请输入名字';}
  else if(kind==='password'){if(!v&&final)msg='请输入密码';}
  else if(kind==='new-password'){const r=acctRules(v);if((v||final)&&!(r.len&&r.mix))msg=v?'密码不符合要求':'请设置密码';
    const email=(form.querySelector('[name=email]')?.value.trim()||acctPending?.email||state.user?.email||'').toLowerCase();
    if(!msg&&email&&[email,email.split('@')[0]].includes(v.toLowerCase()))msg='密码不能与邮箱相同';}
  else if(kind==='confirm'){const other=form.querySelector(`[name="${af.dataset.match}"]`)?.value||'';
    if(!v)msg=final?'请再次输入密码':'';else if(v!==other)msg='两次输入的密码不一致';}
  else if(kind==='code'){if(!/^\d{6}$/.test(v))msg=final||v.length===6?'请输入 6 位数字验证码':'';}
  // 邮箱拼写提示只是建议，不阻止提交
  const blocking=!!msg&&!html;
  acctSetMsg(af,msg.trim()?msg:'',html);
  if(html)af.classList.remove('is-invalid');
  if(!msg&&v&&kind!=='name'&&kind!=='password')af.classList.add('is-valid');else af.classList.remove('is-valid');
  return !blocking;
}
function acctValidateForm(form){
  let first=null;
  form.querySelectorAll('.af').forEach(af=>{if(!acctValidate(af,{final:true})&&!first)first=af;});
  if(first)(first.querySelector('.otp input')||first.querySelector('input'))?.focus();
  return !first;
}
function acctMeter(af){
  const v=af.querySelector('input').value,[lvl,label]=acctStrength(v),r=acctRules(v);
  const m=af.querySelector('.pw-meter');if(m){m.dataset.level=String(lvl);m.querySelector('em').textContent=label?'强度：'+label:'';}
  af.querySelectorAll('.pw-rules li').forEach(li=>li.classList.toggle('ok',!!r[li.dataset.rule]));
}
// 服务端错误放到对应的输入框下面；无法对应的放在表单顶部
function acctServerError(form,message){
  const msg=message||'操作失败，请重试';
  const banner=form.querySelector('.form-error');
  const put=(name,text,html)=>{const af=acctAf(form,name);if(!af)return false;acctSetMsg(af,text,html);(af.querySelector('.otp input')||af.querySelector('input'))?.focus();return true;};
  if(/此邮箱已注册/.test(msg)&&put('email',msg,`此邮箱已注册，<button type="button" class="text-link" data-acct-mode="login">直接登录</button>`))return;
  if(/有效的邮箱/.test(msg)&&put('email','邮箱格式不正确'))return;
  if(/验证码/.test(msg)&&!/发送/.test(msg)&&put('code',msg))return;
  if(/当前密码/.test(msg)&&put('current_password',msg))return;
  if(/^(密码|这个密码|新密码)/.test(msg)&&(put('password',msg)||put('new_password',msg)))return;
  if(/名称|名字/.test(msg)&&put('name',msg))return;
  if(banner)banner.textContent=msg;
}
function acctBusy(btn,on){
  if(!btn)return;
  if(on){btn.dataset.label=btn.textContent;btn.disabled=true;btn.classList.add('is-busy');btn.innerHTML=`<span class="spin" aria-hidden="true"></span>${acctEsc(btn.dataset.busy||'请稍候…')}`;}
  else if(btn.dataset.label){btn.disabled=false;btn.classList.remove('is-busy');btn.textContent=btn.dataset.label;}
}

function acctApplyGate(){
  const gate=document.getElementById('auth-gate');if(!gate)return;
  const need=acctChecked&&!state.user;
  document.body.classList.toggle('auth-required',need);
  document.body.classList.toggle('auth-pending',!acctChecked);
  if(need){if(gate.dataset.mode!==acctMode||gate.hidden){gate.innerHTML=acctGateHtml();gate.dataset.mode=acctMode;acctFocus();}gate.hidden=false;}
  else{gate.hidden=true;gate.innerHTML='';delete gate.dataset.mode;}
}
function acctFocus(){
  requestAnimationFrame(()=>{const g=document.getElementById('auth-gate');if(!g||g.hidden)return;
    const t=g.querySelector('.otp input')||[...g.querySelectorAll('.auth-card input:not([type=hidden]):not([type=checkbox])')].find(i=>!i.value)||g.querySelector('.auth-card input');t?.focus();});
}

// 用户信息显示在侧边栏底部（app.js renderSidebarUser）；这里只负责退出后清掉上一位用户的账号面板。
function acctTopbar(){
  if(state.user)return;
  const panel=document.querySelector('#dialog .acct-panel');
  if(panel){const d=document.getElementById('dialog');if(d?.open)d.close();panel.remove();}
}

const acctBaseRender=render;
render=function(){acctBaseRender();acctTopbar();acctApplyGate();};

function acctPanel(){
  const u=state.user;if(!u)return;
  dialog('我的账号','',`<div class="dialog-body acct-panel">
   <div class="acct-head"><span class="acct-avatar">${acctEsc(acctInitial(u))}</span><div><b>${acctEsc(u.name)}</b><small>${acctEsc(u.email)}</small></div></div>
   <dl class="acct-facts"><dt>注册时间</dt><dd>${acctDate(u.created_at)}</dd><dt>最近登录</dt><dd>${acctDate(u.last_login)}</dd></dl>
   <section class="acct-section"><h3>基本信息</h3>
    <form data-acct-form="profile" novalidate>${acctBanner}${acctField({name:'name',label:'显示名称',kind:'name',value:u.name,idp:'acct-'})}
     <button class="button secondary" type="submit" data-busy="正在保存…">保存</button></form></section>
   <section class="acct-section"><h3>修改密码</h3>
    <form data-acct-form="password" novalidate>${acctBanner}
     ${acctField({name:'current_password',label:'当前密码',kind:'password',idp:'acct-'})}
     ${acctField({name:'new_password',label:'新密码',kind:'new-password',idp:'acct-'})}
     ${acctField({name:'new_password2',label:'确认新密码',kind:'confirm',match:'new_password',idp:'acct-'})}
     <button class="button secondary" type="submit" data-busy="正在修改…">修改密码</button></form></section>
   <div class="acct-foot"><button type="button" class="button danger-outline" data-action="logout">退出登录</button></div>
  </div>`);
}

/* ---------- 交互 ---------- */
function acctOtpSync(box){
  const form=box.closest('form'),code=[...box.querySelectorAll('input')].map(i=>i.value).join('');
  form.querySelector('[name=code]').value=code;
  const af=box.closest('.af');if(af.classList.contains('is-invalid')||code.length===6)acctValidate(af);
}
function acctOtpFill(input,digits){
  const boxes=[...input.closest('[data-otp]').querySelectorAll('input')];let i=boxes.indexOf(input);
  if(!digits){input.value='';return;}
  for(const d of digits){if(i>=boxes.length)break;boxes[i].value=d;i++;}
  (boxes[Math.min(i,boxes.length-1)]).focus();
}

document.addEventListener('click',e=>{
  const m=e.target.closest('[data-acct-mode]');
  if(m){e.preventDefault();
    const f=m.closest('form');const email=f?.querySelector('[name=email]')?.value.trim();
    acctMode=m.dataset.acctMode;
    if(acctMode==='login'||acctMode==='forgot')acctPending=email&&ACCT_EMAIL_RE.test(email)?{email}:(acctMode==='forgot'?acctPending&&{email:acctPending.email}:null);
    acctApplyGate();return;}
  const eye=e.target.closest('[data-acct-eye]');
  if(eye){e.preventDefault();const inp=eye.parentElement.querySelector('input');const show=inp.type==='password';
    const pos=inp.selectionStart;inp.type=show?'text':'password';eye.innerHTML=show?ACCT_EYE_OFF:ACCT_EYE;
    eye.setAttribute('aria-pressed',String(show));eye.setAttribute('aria-label',show?'隐藏密码':'显示密码');
    inp.focus();try{inp.setSelectionRange(pos,pos);}catch{}return;}
  const fix=e.target.closest('[data-acct-fix]');
  if(fix){e.preventDefault();const af=fix.closest('.af');const inp=af.querySelector('input');inp.value=fix.dataset.acctFix;acctValidate(af);inp.focus();return;}
  const rs=e.target.closest('[data-acct-resend]');
  if(rs){e.preventDefault();acctResendCode(rs);return;}
  const a=e.target.closest('[data-action="account"]');
  if(a){e.preventDefault();e.stopImmediatePropagation();if(state.user)acctPanel();return;}
},true);

document.addEventListener('input',e=>{
  const t=e.target;if(!t.closest?.('[data-acct-form]'))return;
  const otp=t.closest('[data-otp]');
  if(otp){acctOtpFill(t,String(t.value).replace(/\D/g,''));acctOtpSync(otp);return;}
  const af=t.closest('.af');if(!af)return;
  const form=af.closest('form');form.querySelector('.form-error').textContent='';
  if(af.dataset.kind==='new-password'){acctMeter(af);if(af.classList.contains('is-invalid')||acctRules(t.value).len&&acctRules(t.value).mix)acctValidate(af);
    const c=form.querySelector(`.af[data-match="${af.dataset.af}"]`);if(c&&c.querySelector('input').value)acctValidate(c);return;}
  if(af.classList.contains('is-invalid')||af.classList.contains('is-valid')||af.dataset.kind==='confirm'&&t.value.length>=(form.querySelector(`[name="${af.dataset.match}"]`)?.value.length||0))acctValidate(af);
},true);

document.addEventListener('focusout',e=>{
  const t=e.target;if(!t.closest?.('[data-acct-form]')||t.closest('[data-otp]'))return;
  const af=t.closest('.af');if(!af||!t.value)return;
  // 点到"显示密码"、"直接登录"等按钮时不要打断
  if(e.relatedTarget&&af.contains(e.relatedTarget))return;
  acctValidate(af);
},true);

document.addEventListener('keydown',e=>{
  const t=e.target;
  if(t.closest?.('[data-otp]')){
    const boxes=[...t.closest('[data-otp]').querySelectorAll('input')],i=boxes.indexOf(t);
    if(e.key==='Backspace'&&!t.value&&i>0){e.preventDefault();boxes[i-1].value='';boxes[i-1].focus();acctOtpSync(t.closest('[data-otp]'));}
    else if(e.key==='ArrowLeft'&&i>0){e.preventDefault();boxes[i-1].focus();}
    else if(e.key==='ArrowRight'&&i<5){e.preventDefault();boxes[i+1].focus();}
  }
},true);
const acctCaps=e=>{const t=e.target;if(!(t instanceof HTMLInputElement)||!t.closest('.af')||!e.getModifierState)return;
  const caps=t.closest('.af').querySelector('.af-caps');if(caps)caps.hidden=!e.getModifierState('CapsLock');};
document.addEventListener('keyup',acctCaps,true);document.addEventListener('keydown',acctCaps,true);
document.addEventListener('focusin',e=>{if(e.target.closest?.('[data-otp]'))e.target.select?.();},true);

// "重新发送" 倒计时：只改按钮文字，不重绘表单（避免清空已输入的内容）。
function acctTick(){
  const b=document.querySelector('#auth-gate [data-acct-resend]');
  if(!b){clearInterval(acctTimer);acctTimer=0;return;}
  const left=Math.ceil((acctResendAt-Date.now())/1000);
  b.disabled=left>0;b.textContent=left>0?`重新发送（${left}s）`:'重新发送';
  if(left<=0){clearInterval(acctTimer);acctTimer=0;}
}
function acctStartTimer(seconds){acctResendAt=Date.now()+seconds*1000;clearInterval(acctTimer);acctTimer=setInterval(acctTick,1000);acctTick();}
function acctCodeSent(r,extra){
  acctPending={...(acctPending||{}),...extra,email:r.email,delivery:r.delivery,outbox:r.outbox||''};
  if(r.dev_code)window.nxDevCode=r.dev_code; // 仅自动化测试（NEUXMIND_MAIL_DEV_ECHO=1 且未配置邮件服务）
}
async function acctSignedIn(r,message){
  acctPending=null;clearInterval(acctTimer);acctTimer=0;
  state={...state,user:r.user};acctChecked=true;acctMode='login';
  await refresh(true);toast(message);
}
async function acctResendCode(b){
  const form=document.querySelector('#auth-gate form');const err=form?.querySelector('.form-error');if(err)err.textContent='';
  if(!acctPending)return;
  b.disabled=true;
  try{
    const r=acctMode==='register-code'?await api('register/start',acctPending):await api('password/forgot',{email:acctPending.email});
    acctCodeSent(r,{});acctStartTimer(r.resend_after||60);toast('验证码已重新发送');
    form?.querySelectorAll('.otp input').forEach(i=>i.value='');if(form)acctOtpSync(form.querySelector('[data-otp]'));form?.querySelector('.otp input')?.focus();
  }catch(x){if(form)acctServerError(form,x.message);b.disabled=false;}
}

document.addEventListener('submit',async e=>{
  const f=e.target.closest('[data-acct-form]');if(!f)return;
  e.preventDefault();e.stopImmediatePropagation();
  const kind=f.dataset.acctForm,btn=f.querySelector('[type="submit"]');
  f.querySelector('.form-error').textContent='';
  if(btn?.disabled||!acctValidateForm(f))return;
  const d=Object.fromEntries(new FormData(f));
  acctBusy(btn,true);
  try{
    if(kind==='login'){
      const r=await api('login',{email:d.email.trim(),password:d.password,remember:!!d.remember});
      await acctSignedIn(r,`欢迎回来，${r.user.name}`);return;
    }
    if(kind==='register'){
      const payload={name:d.name.trim(),email:d.email.trim(),password:d.password,remember:!!d.remember};
      const r=await api('register/start',payload);
      acctCodeSent(r,payload);acctMode='register-code';acctApplyGate();acctStartTimer(r.resend_after||60);return;
    }
    if(kind==='register-code'){
      const r=await api('register',{...acctPending,code:d.code});
      await acctSignedIn(r,`欢迎，${r.user.name}！账号已创建`);return;
    }
    if(kind==='forgot'){
      const r=await api('password/forgot',{email:d.email.trim()});
      acctCodeSent(r,{});acctMode='reset';acctApplyGate();acctStartTimer(r.resend_after||60);return;
    }
    if(kind==='reset'){
      const r=await api('password/reset',{email:acctPending.email,code:d.code,password:d.password});
      await acctSignedIn(r,'密码已重置，已登录');return;
    }
    if(kind==='profile'){const r=await api('account/profile',{name:String(d.name||'').trim()});state={...state,user:{...state.user,...r.user}};render();acctPanel();toast('名称已保存');return;}
    if(kind==='password'){
      const r=await api('account/password',{current_password:d.current_password,new_password:d.new_password});
      acctPanel();toast(r.signed_out_sessions?`密码已修改，已退出其他 ${r.signed_out_sessions} 处登录`:'密码已修改');return;
    }
  }catch(x){acctServerError(f,x.message);}
  finally{if(btn?.isConnected)acctBusy(btn,false);}
},true);

// 启动时先确认是否已登录：已登录直接进入界面，否则显示登录 / 注册页（不再先闪出空白工作台）。
// 最多等 6 秒；本地服务没回应时也不会一直停在"正在连接"。
(async()=>{
  const timeout=new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),6000));
  try{const r=await Promise.race([api('me'),timeout]);if(r&&r.user){state={...state,user:r.user};}else if(r&&r.timeout){window.nxBootNote='本地服务响应较慢，请稍候或重新打开应用。';}}catch(err){window.nxBootNote='检查登录状态失败：'+(err&&err.message||err);}
  acctChecked=true;
  if(!state.user)acctMode='login';
  try{render();}catch(err){window.nxBootNote='界面渲染出错：'+(err&&err.message||err);acctApplyGate();}
  if(window.nxBootNote){const g=document.querySelector('#auth-gate .form-error');if(g)g.textContent=window.nxBootNote;}
  window.nxAccountReady=true;
})();
