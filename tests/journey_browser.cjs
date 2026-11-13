const {gateSignIn}=require('./gate_helper.cjs');
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs');
const {connectTokenFans}=require('./tokenfans_browser.cjs');
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2]||process.env.BRAIN_QA_CONFIG||'/tmp/neuxmind-engineering-qa.json'));
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const page=await browser.newPage({viewport:{width:1440,height:930}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const click=async action=>{const inside=page.locator(`#dialog[open] [data-action="${action}"]`);await (await inside.count()?inside:page.locator(`[data-action="${action}"]`)).first().click();};
 const fill=async(name,value)=>page.locator(`#dialog [name="${name}"]`).fill(String(value));
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();};
 await page.goto(cfg.url);await gateSignIn(page,{name:'用户路径验收',email:`journey-${Date.now()}@example.test`,password:'fixture-password-123'});await page.getByRole('heading',{name:'你希望机器人完成什么工作？',exact:true}).waitFor();await page.screenshot({path:'artifacts/user-journey-welcome.png'});
 await click('journey-start');
 await page.locator('form[data-form=project]').waitFor();await fill('name','仓库异常巡检');await fill('objective','每天检查仓库通道，识别障碍并输出异常报告。');await submit();await page.locator('#dialog').waitFor({state:'hidden'});
 await page.locator('.journey-next').getByText('连接你的机器人',{exact:true}).waitFor();await click('journey-connect');await page.getByText('有 SSH 地址、用户名和密码或密钥',{exact:true}).waitFor();await click('close');
 // Real HTTP persistence for task assets; no model invocation or device action.
 await page.evaluate(async()=>{const s=await api('state'),pid=s.projects[0].id;await api('robots',{project_id:pid,name:'巡检设备',family:'mobile',endpoint:''});await refresh(true);});
 // Issue #4: models come only from TokenFans, connected on 云平台账户 (fake key from the fixture's stand-in).
 await connectTokenFans(page,cfg);
 await page.goto(cfg.url+'/#agents');await click('journey-prepare');await page.locator('[name=objective]').waitFor();if(!(await page.locator('#dialog [name=model_id]').inputValue()).startsWith('agent-model-tokenfans-'))throw Error('Plan does not use the TokenFans model');if(!(await page.locator('[name=objective]').inputValue()).includes('检查仓库通道'))throw Error('Task goal was lost');await submit();await page.getByRole('heading',{name:'启动 仓库异常巡检 · 工程准备',exact:true}).waitFor();
 const state=await page.evaluate(()=>api('state'));if(state.workflow_runs.length||state.agent_sessions.length)throw Error('Plan unexpectedly started paid execution');if(state.engineering_workflows[0].stages.length!==4)throw Error('Missing default roles');await click('close');
 for(const route of ['robots','models','skills','agents','extensions','workflows','simulation','harness','deployment','training','datasets','resources','billing','services']){
  await page.goto(cfg.url+'/#'+route);await page.locator('#main > .page-purpose > summary').first().click();await page.locator('#main > .page-purpose').first().getByText('需要准备',{exact:true}).waitFor();await page.locator('#main > .page-purpose').first().getByText('完成后得到',{exact:true}).waitFor();
 }
 // UI progression must not count unrelated or superseded evidence as delivery.
 const progress=await page.evaluate(()=>{
  const original=state;const p={id:'p',name:'Task',objective:'Goal'},robot={id:'r',project_id:'p',name:'Robot',status:'connected',manifest_digest:'current'},app={id:'a',project_id:'p',name:'App',robot_id:'r',digest:'app-current'};
  state={...blank(),user:{name:'QA'},projects:[p],robots:[robot],applications:[app]};projectId='p';
  state.evaluations=[{id:'wrong',project_id:'p',application_id:'a',application_digest:'app-old',robot_id:'r',robot_manifest_digest:'current',scope:'onsite',passed:true}];const wrong=!!journeyContext().onsite;
  state.evaluations=[{id:'new',project_id:'p',application_id:'a',application_digest:'app-current',robot_id:'r',robot_manifest_digest:'current',scope:'onsite',passed:true}];state.deployments=[{id:'d',project_id:'p',application_id:'a',application_digest:'app-current',robot_id:'r',report_id:'old',status:'deployed'}];const stale=!!journeyContext().release;
  state.evaluations.unshift({...state.evaluations[0],id:'failed',passed:false});const failed=journeyContext().onsite.passed;
  state=original;projectId=original.projects[0].id;render();return {wrong,stale,failed};
 });if(progress.wrong||progress.stale||progress.failed)throw Error('Misleading progress: '+JSON.stringify(progress));
 await page.goto(cfg.url+'/#overview');await page.screenshot({path:'artifacts/user-journey-task.png'});
 for(const [w,h] of [[900,640],[390,844]]){await page.setViewportSize({width:w,height:h});const before=await page.evaluate(()=>({top:document.querySelector('.topbar').getBoundingClientRect().top,left:document.querySelector('#sidebar').getBoundingClientRect().top,window:scrollY}));await page.locator('.content-scroll').hover();await page.mouse.wheel(0,1400);await page.waitForTimeout(200);const after=await page.evaluate(()=>({top:document.querySelector('.topbar').getBoundingClientRect().top,left:document.querySelector('#sidebar').getBoundingClientRect().top,window:scrollY}));if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Application chrome moved');if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');await page.screenshot({path:`artifacts/user-journey-${w}x${h}.png`});}
 if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,pages:15,flow:'welcome/register/goal/next-action/connect/prepare-plan',noUnapprovedExecution:true,evidenceMatching:true,fixedChrome:true,jsErrors:errors.length}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
