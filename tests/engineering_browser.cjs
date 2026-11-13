const {gateSignIn}=require('./gate_helper.cjs');
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs');
const {connectTokenFans,useTokenFansModel}=require('./tokenfans_browser.cjs');  // issue #4: models run only through TokenFans
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2]||process.env.BRAIN_ENGINE_QA_CONFIG||'/tmp/neuxmind-engineering-qa.json'));
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const page=await browser.newPage({viewport:{width:1440,height:930}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const click=async action=>{const dialog=page.locator(`#dialog[open] [data-action="${action}"]`);await (await dialog.count()?dialog:page.locator(`[data-action="${action}"]`)).first().click();};
 const fill=async(name,value)=>page.locator(`#dialog [name="${name}"]`).fill(String(value));
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();await page.waitForTimeout(250);const e=await page.locator('.form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 await page.goto(cfg.url);await gateSignIn(page,{name:'工程验收工作空间',email:`engine-${Date.now()}@example.test`,password:'fixture-password-123'});
 await click('journey-start');await fill('name','通用本体接入验收');await fill('objective','SSH 调查、档案、工程开发闭环');await submit();
 await connectTokenFans(page,cfg);await useTokenFansModel(page,cfg,'fixture-discovery');
 await page.goto(cfg.url+'/#robots');await click('engine-ssh');await fill('name','SSH 协议验收设备');await fill('host','127.0.0.1');await fill('port',cfg.ssh_port);await fill('username','robot');await fill('password','fixture-password');await submit();
 await page.locator('#dialog').getByText('确认 SSH 主机指纹',{exact:true}).waitFor();await page.locator('#dialog input[type=checkbox]').check();await submit();
 await page.locator('#dialog [data-action="engine-discover"]').first().click({timeout:30000});await page.locator('#dialog').getByText('让 Agent 调查本体',{exact:true}).waitFor();
 await page.locator('[name=data_authorized]').check();await fill('max_tokens',1000);await fill('max_minutes',1);await submit();
 await page.locator('#engine-live [data-action=inventory-report]').waitFor({timeout:25000});
 await page.screenshot({path:'artifacts/agent-discovery-session.png'});await page.locator('#engine-live [data-action=inventory-report]').click();await page.locator('#dialog').getByText('Linux fixture',{exact:true}).waitFor();await page.screenshot({path:'artifacts/robot-inventory.png'});
 await click('inventory-evidence');await page.locator('#dialog').getByText('设备调查依据',{exact:true}).waitFor();if(!(await page.locator('#dialog').innerText()).includes('evidence-'))throw Error('Missing evidence');await click('close');
 await useTokenFansModel(page,cfg,'fixture-work');await page.goto(cfg.url+'/#agents');
 await click('engine-work');await fill('name','适配器与 Harness 工程');if((await page.locator('[name=model_id] option:checked').innerText())!=='TokenFans · fixture-work')throw Error('TokenFans default not pre-selected');await page.locator('[name=data_authorized]').check();await fill('max_tokens',1000);await fill('max_minutes',1);await submit();
 await page.locator('[data-action=engine-artifact]').waitFor({timeout:25000});await page.screenshot({path:'artifacts/agent-engineering-session.png'});await click('engine-artifact');if(!(await page.locator('#dialog pre').innerText()).includes('fixture adapter'))throw Error('Missing artifact');await click('close');
 for(const name of ['overview','robots','models','skills','agents','simulation','harness','deployment','training','datasets','resources','billing','services']){await page.goto(cfg.url+'/#'+name);await page.locator('#main h1').waitFor();}
 await page.goto(cfg.url+'/#agents');await page.setViewportSize({width:900,height:640});
 const position=()=>page.evaluate(()=>({top:document.querySelector('.topbar').getBoundingClientRect().top,left:document.querySelector('#sidebar').getBoundingClientRect().top,window:scrollY}));
 const before=await position();await page.locator('.content-scroll').hover();await page.mouse.wheel(0,1600);await page.waitForTimeout(250);const after=await position();if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Application chrome moved');
 await page.screenshot({path:'artifacts/agent-studio-900x640.png'});
 if(errors.length)throw Error(errors.join('\n'));
 console.log(JSON.stringify({passed:true,views:13,flow:'register/project/model/SSH/fingerprint/discovery/inventory/evidence/work/artifact',fixedChrome:true,jsErrors:errors.length}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
