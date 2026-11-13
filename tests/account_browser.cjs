// 账号验收（界面）：登录 / 注册页逐项校验（邮箱格式与拼写提示、密码规则与强度、两次输入一致）、密码显示切换、
// 6 格验证码（粘贴、错误码）、内置测试账号、记住登录、改名、改密码、退出、旧密码失效、重复注册、忘记密码重置、手机宽度。
// 用法：NEUXMIND_MAIL_DEV_ECHO=1 python3 -m brainos.server --port 8795 --state-dir <临时目录> 之后
//       node tests/account_browser.cjs http://127.0.0.1:8795 <截图目录>
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');const fs=require('fs');
const base=process.argv[2],shots=process.argv[3],stateFile=shots+'/storage.json';fs.mkdirSync(shots,{recursive:true});
const log=(...a)=>console.log(...a);const fail=m=>{console.error('FAIL',m);process.exit(1);};
const msgOf=(p,name)=>p.locator(`.af[data-af="${name}"] .af-msg`);
const expectMsg=async(p,name,re)=>{await p.waitForFunction(([n,src])=>new RegExp(src).test(document.querySelector(`.af[data-af="${n}"] .af-msg`)?.textContent||''),[name,re.source],{timeout:5000}).catch(async()=>fail(`${name} message ${re} got "${await msgOf(p,name).textContent()}"`));};
const openMenu=async p=>{await p.click('#user-menu-toggle');await p.waitForSelector('#user-menu:not([hidden])');};
const openAccount=async p=>{await openMenu(p);await p.click('#user-account-action');await p.waitForSelector('#dialog[open] .acct-panel');};
const who=p=>p.evaluate(()=>document.querySelector('#sidebar-user-name').textContent+' '+document.querySelector('#sidebar-user-email').textContent);
const signedIn=p=>p.waitForFunction(()=>document.querySelector('#auth-gate').hidden&&document.querySelector('#sidebar-user-email').textContent.includes('@'));
const logout=async p=>{await openMenu(p);await p.click('#user-logout');await p.waitForSelector('#auth-gate:not([hidden]) form[data-acct-form="login"]');};
(async()=>{
 const b=await chromium.launch();let ctx=await b.newContext({viewport:{width:1440,height:900}});let p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(String(e)));
 await p.goto(base);await p.waitForSelector('#auth-gate:not([hidden]) form[data-acct-form="login"]');
 if(await p.locator('.sidebar').isVisible())fail('sidebar visible before login');await p.screenshot({path:shots+'/01-login.png'});

 // 登录页：空提交逐项提示；邮箱格式；密码显示切换
 await p.click('.auth-submit');await expectMsg(p,'email',/请输入邮箱/);await expectMsg(p,'password',/请输入密码/);
 await p.fill('[name=email]','not-an-email');await p.locator('[name=password]').focus();await expectMsg(p,'email',/邮箱格式不正确/);
 await p.fill('[name=password]','abc');if(await p.getAttribute('[name=password]','type')!=='password')fail('password visible by default');
 await p.click('[data-af="password"] [data-acct-eye]');if(await p.getAttribute('[name=password]','type')!=='text')fail('eye toggle did not show password');
 if(await p.getAttribute('[data-af="password"] [data-acct-eye]','aria-label')!=='隐藏密码')fail('eye label');
 await p.screenshot({path:shots+'/02-login-validation.png'});
 await p.click('[data-af="password"] [data-acct-eye]');if(await p.getAttribute('[name=password]','type')!=='password')fail('eye toggle did not hide');
 log('login: required/email format/eye toggle ok');

 // 内置测试账号可直接登录
 await p.fill('[name=email]','test@neuxmind.ai');await p.fill('[name=password]','Test123456');await p.click('.auth-submit');
 await signedIn(p);if(!(await who(p)).includes('test@neuxmind.ai'))fail('test account in sidebar: '+await who(p));
 log('built-in test account ok');await logout(p);

 // 注册：拼写提示、密码规则与强度、两次不一致
 await p.click('#auth-gate [data-acct-mode="register"]');await p.waitForSelector('form[data-acct-form="register"]');
 await p.fill('[name=name]','测试用户');await p.fill('[name=email]','zy@gmial.com');await p.locator('[name=password]').focus();
 await expectMsg(p,'email',/是不是 zy@gmail\.com/);await p.click('[data-acct-fix]');
 if(await p.inputValue('[name=email]')!=='zy@gmail.com')fail('typo fix not applied');await p.fill('[name=email]','zy@example.com');
 await p.fill('[name=password]','abcdefghij');
 if(await p.locator('.pw-rules li[data-rule="len"].ok').count()!==1||await p.locator('.pw-rules li[data-rule="mix"].ok').count()!==0)fail('rules ticks');
 if(await p.getAttribute('.pw-meter','data-level')!=='1')fail('weak meter');
 await p.fill('[name=password]','password-123');
 if(await p.getAttribute('.pw-meter','data-level')!=='2')fail('meter for password-123: '+await p.getAttribute('.pw-meter','data-level'));
 await p.fill('[name=password2]','password-999');await p.locator('[name=name]').focus();await expectMsg(p,'password2',/两次输入的密码不一致/);
 await p.screenshot({path:shots+'/03-register-validation.png'});
 await p.fill('[name=password2]','password-123');await p.click('.auth-submit');

 // 验证码：6 格、粘贴、错误码
 await p.waitForSelector('form[data-acct-form="register-code"]');
 const rs=p.locator('[data-acct-resend]');if(!(await rs.isDisabled())||!(await rs.textContent()).includes('s）'))fail('resend not cooling down');
 if(!(await p.textContent('.auth-card')).includes('zy@example.com'))fail('code page lacks email');
 const code1=await p.evaluate(()=>window.nxDevCode);if(!/^\d{6}$/.test(code1||''))fail('no dev code');
 await p.locator('[data-otp] input').first().fill(code1==='000000'?'111111':'000000');
 if(await p.inputValue('[name=code]')!==(code1==='000000'?'111111':'000000'))fail('otp did not spread');
 await p.click('.auth-submit');await expectMsg(p,'code',/验证码不正确/);
 await p.screenshot({path:shots+'/04-register-code.png'});log('wrong code rejected');
 await p.locator('[data-otp] input').first().fill(code1);await p.click('.auth-submit');
 await p.waitForSelector('.sidebar',{state:'visible'});await signedIn(p);
 const chip=await who(p);if(!chip.includes('测试用户')||!chip.includes('zy@example.com'))fail('chip '+chip);
 log('after register chip:',chip.trim());await p.screenshot({path:shots+'/05-in-app.png'});

 // 账号面板：改名、改密码（规则、当前密码错误）
 await openAccount(p);await p.screenshot({path:shots+'/06-account.png'});
 await p.fill('form[data-acct-form="profile"] [name=name]','测试用户（改名）');await p.click('form[data-acct-form="profile"] [type=submit]');
 await p.waitForFunction(()=>document.querySelector('#sidebar-user-name').textContent.includes('测试用户（改名）'));log('rename ok');
 await p.fill('[name=current_password]','wrong-password-1');await p.fill('[name=new_password]','newpass-4567');await p.fill('[name=new_password2]','newpass-4567');
 await p.click('form[data-acct-form="password"] [type=submit]');await expectMsg(p,'current_password',/当前密码不正确/);log('wrong current pw rejected');
 await p.fill('[name=current_password]','password-123');await p.fill('[name=new_password]','newpass-4567');await p.fill('[name=new_password2]','newpass-4567');
 await p.click('form[data-acct-form="password"] [type=submit]');await p.waitForSelector('#toast:text("密码已修改")');log('password changed');
 await p.keyboard.press('Escape');

 // 重开应用：记住登录
 await ctx.storageState({path:stateFile});await ctx.close();
 ctx=await b.newContext({viewport:{width:1440,height:900},storageState:stateFile});p=await ctx.newPage();p.on('pageerror',e=>errs.push(String(e)));
 await p.goto(base);await signedIn(p);if(await p.locator('#auth-gate').isVisible())fail('gate shown although remembered');
 log('reopen remembered:',await who(p));

 // 退出；旧密码失效，新密码可用
 await logout(p);log('logout -> login page');
 await p.fill('[name=email]','zy@example.com');await p.fill('[name=password]','password-123');await p.click('.auth-submit');await p.waitForSelector('.form-error:text("邮箱或密码不正确")');
 await p.fill('[name=password]','newpass-4567');await p.click('.auth-submit');await signedIn(p);log('login with new password ok');

 // 重复注册：提示在邮箱下面，并可直接去登录
 await logout(p);
 await p.click('#auth-gate [data-acct-mode="register"]');await p.fill('[name=name]','x');await p.fill('[name=email]','zy@example.com');await p.fill('[name=password]','password-123');await p.fill('[name=password2]','password-123');
 await p.click('.auth-submit');await expectMsg(p,'email',/此邮箱已注册/);
 await p.click('.af[data-af="email"] [data-acct-mode="login"]');await p.waitForSelector('form[data-acct-form="login"]');
 if(await p.inputValue('[name=email]')!=='zy@example.com')fail('email not carried to login');log('duplicate email -> sign in link ok');

 // 忘记密码 -> 验证码 -> 新密码 -> 已登录
 await p.click('#auth-gate [data-acct-mode="forgot"]');await p.waitForSelector('form[data-acct-form="forgot"]');
 if(await p.inputValue('[name=email]')!=='zy@example.com')fail('email not carried to forgot');
 await p.click('.auth-submit');await p.waitForSelector('form[data-acct-form="reset"]');
 const code2=await p.evaluate(()=>window.nxDevCode);
 await p.locator('[data-otp] input').first().fill(code2);await p.fill('[name=password]','short1');await p.fill('[name=password2]','short1');
 await p.click('.auth-submit');await expectMsg(p,'password',/不符合要求/);await p.screenshot({path:shots+'/07-reset.png'});
 await p.fill('[name=password]','reset-pass-789');await p.fill('[name=password2]','reset-pass-789');await p.click('.auth-submit');
 await signedIn(p);await p.waitForSelector('#toast:text("密码已重置")');log('password reset via code ok');
 await logout(p);
 await p.fill('[name=email]','zy@example.com');await p.fill('[name=password]','newpass-4567');await p.click('.auth-submit');await p.waitForSelector('.form-error:text("邮箱或密码不正确")');
 await p.fill('[name=password]','reset-pass-789');await p.click('.auth-submit');await signedIn(p);log('old password dead, reset password works');
 await logout(p);

 // 手机宽度
 await p.setViewportSize({width:390,height:844});await p.screenshot({path:shots+'/08-mobile-login.png'});
 await p.click('#auth-gate [data-acct-mode="register"]');await p.screenshot({path:shots+'/09-mobile-register.png'});
 const overflow=await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth);if(overflow)fail('horizontal scroll on mobile');
 log('JS errors',JSON.stringify(errs));await b.close();if(errs.length)process.exit(2);
})().catch(e=>{console.error('FAILED',e);process.exit(1);});
