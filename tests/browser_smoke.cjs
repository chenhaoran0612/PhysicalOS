const {gateSignIn}=require('./gate_helper.cjs');
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const path=require('path');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const page=await browser.newPage({viewport:{width:1440,height:1040}});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const base=process.env.BRAIN_QA_URL||'http://127.0.0.1:8789';
 const device=process.env.BRAIN_QA_DEVICE;if(!device)throw Error('Set BRAIN_QA_DEVICE to the test-only browser_fixture.py endpoint');
 // The app opens on the sign-in screen (the sidebar is hidden until signed in); gateSignIn below waits for the navigation.
 await page.goto(base);await page.locator('#auth-gate form[data-acct-form]').waitFor();
 await page.screenshot({path:'artifacts/mac-workbench.png',fullPage:true});
 const click=async action=>page.locator(`[data-action="${action}"]`).first().click();
 const fill=async(name,value)=>page.locator(`#dialog [name="${name}"]`).fill(value);
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();await page.waitForTimeout(300);const e=await page.locator('.form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 await gateSignIn(page,{name:'安装包验收工作空间',email:`qa-${Date.now()}@example.test`,password:'test-only-password-123'});
 await click('journey-start');await fill('name','机器人交付流程验收');await fill('objective','验证本体接入、技能编排、工程检查和发布流程');await submit();
 await page.goto(base+'/#robots');await click('robot-add');await fill('name','协议测试设备');await page.locator('[name=family]').selectOption('mobile');await fill('endpoint',device);await fill('token','browser-fixture-token-12345678');await submit();await click('robot-connect');await page.waitForTimeout(300);
 await page.goto(base+'/#skills');await click('skill-add');await fill('name','导航技能');await fill('entrypoint','navigation.goto');await fill('requires','navigation.goal');await fill('success_condition','到达目标点');await fill('stop_condition','定位丢失或超时停止');await submit();
 await click('application-add');await fill('name','巡检应用');await page.locator('#steps-container .step-editor').first().waitFor();await fill('parameters','{"goal":[1,2]}');await submit();await page.getByText('应用交付清单',{exact:true}).waitFor();
 // Since round 02 the application's delivery checklist drives check → on-site test → publish → start/stop
 // (the old harness-pick / onsite-pick / deploy-pick dialogs now only pick an application).
 const dlg=a=>page.locator(`#dialog [data-action="${a}"]`).first().click();
 await dlg('deliver-check');await page.getByRole('heading',{name:'下一步：现场测试',exact:true}).waitFor();
 await dlg('deliver-onsite');await page.locator('form[data-form=deliver-onsite]').waitFor();await page.locator('[name=hardware_authorized]').check();await submit();await page.getByRole('heading',{name:'下一步：发布到机器人',exact:true}).waitFor();
 await dlg('deliver-publish');await page.locator('form[data-form=deliver-publish]').waitFor();await submit();await page.getByRole('heading',{name:'应用已发布，可以准备启动',exact:true}).waitFor();
 await dlg('deliver-start');await page.locator('form[data-form=deliver-start]').waitFor();await page.locator('[name=hardware_authorized]').check();await submit();await page.getByText('启动工作 · 已受理',{exact:true}).waitFor();
 await dlg('operate-stop');await page.locator('form[data-form=operate]').waitFor();await submit();await page.getByText('请求停止 · 已受理',{exact:true}).waitFor();await dlg('close');
 await page.goto(base+'/#harness');await click('harness-pick');await page.getByText('选择要继续交付的应用',{exact:true}).waitFor();await dlg('close');
 const pages=['overview','robots','models','skills','agents','simulation','harness','deployment','training','datasets','resources','billing','services'];
 for(const p of pages){await page.goto(base+'/#'+p);await page.locator('#main h1').waitFor();if(await page.locator('#main').innerText()==='')throw Error('Empty '+p);}
 await page.goto(base+'/#overview');await page.screenshot({path:'artifacts/mac-workspace-verified.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'artifacts/mobile-layout.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log(JSON.stringify({passed:true,views:pages.length,workflow:'signup/project/robot/skill/app/harness/onsite/deploy/start/stop',jsErrors:errors.length}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
