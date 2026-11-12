const {gateSignIn}=require('./gate_helper.cjs');
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs');
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2]||process.env.BRAIN_ENGINE_QA_CONFIG||'/tmp/neuxmind-engineering-qa.json'));
 // Skills and MCP are loaded and called by the real Codex runtime; the app-server stand-in (tests/codex_standin.py)
 // cannot prove that. Without the binary this acceptance is skipped, not passed.
 if(cfg.runtime==='standin'){console.log(JSON.stringify({skipped:true,reason:'needs the Codex 0.146.1 binary (NEUXMIND_TEST_AGENT_BINARY)'}));return;}
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const page=await browser.newPage({viewport:{width:1440,height:930}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const click=async action=>{const dialog=page.locator(`#dialog[open] [data-action="${action}"]`);await (await dialog.count()?dialog:page.locator(`[data-action="${action}"]`)).first().click();};
 const fill=async(name,value)=>page.locator(`#dialog [name="${name}"]`).fill(String(value));
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();await page.waitForTimeout(300);const e=await page.locator('.form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 await page.goto(cfg.url);await gateSignIn(page,{name:'工程扩展验收',email:`extensions-${Date.now()}@example.test`,password:'fixture-password-123'});
 await click('journey-start');await fill('name','工程工具接入');await fill('objective','验证 Skills 与 MCP 原生调用和审批');await submit();
 await page.goto(cfg.url+'/#extensions');await click('engine-ext-skill-add');await fill('name','robot-review');await fill('description','调查设备与检查工程接口时使用');await fill('instructions','SKILL_BROWSER_MARKER: use selected lookup_robot to review interfaces.');await submit();await page.locator('#dialog').waitFor({state:'hidden'});
 await click('engine-ext-skill-detail');await page.getByText('SKILL_BROWSER_MARKER:',{exact:false}).waitFor();
 const download=page.waitForEvent('download');await click('engine-ext-skill-export');const artifact=await download;const exported=JSON.parse(fs.readFileSync(await artifact.path(),'utf8'));if(!exported.files['SKILL.md'])throw Error('Missing exported skill');
 await click('engine-ext-skill-revise');await fill('version','2.0.0');exported.files['SKILL.md']=exported.files['SKILL.md'].replace('SKILL_BROWSER_MARKER','SKILL_BROWSER_REVISION_2');await page.locator('[name=package_file]').setInputFiles({name:'robot-review.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(exported))});await submit();await page.locator('#dialog').waitFor({state:'hidden'});
 await click('engine-ext-mcp-add');await fill('name','本地知识库 MCP（测试）');await fill('url',cfg.mcp);await fill('token','fixture-mcp-token');await page.locator('[name=connection_authorized]').check();await submit();await page.locator('[name=enabled_tools][value=lookup_robot]').waitFor({timeout:20000});await page.locator('[name=enabled_tools][value=lookup_robot]').check();await page.locator('[name=enabled]').check();await submit();await page.locator('#dialog').waitFor({state:'hidden'});
 await page.screenshot({path:'artifacts/engineering-extensions.png'});
 await page.goto(cfg.url+'/#agents');await click('engine-models');await click('engine-own-model');await fill('name','扩展调用模型（测试）');await fill('base_url',cfg.extension_model);await fill('model','fixture');await fill('api_key','fixture-model-token');await submit();
 await click('engine-work');await fill('name','使用 Skill 与 MCP 调查接口');await page.locator('[name=skill_ids]').first().check();await page.locator('[name=mcp_ids]').first().check();await page.locator('[name=data_authorized]').check();await fill('max_tokens',1000);await fill('max_minutes',1);await submit();
 await page.locator('[data-action=engine-approval]').waitFor({timeout:20000});await click('engine-approval');await page.screenshot({path:'artifacts/mcp-tool-approval.png'});await page.locator('[name=decision]').selectOption('accept');await submit();await page.locator('#engine-live [data-action=engine-resume]').waitFor({timeout:20000});
 const content=await page.locator('#engine-live').innerText();if(!content.includes('2.0.0')||!content.includes('MCP 工具')||!content.includes('已完成'))throw Error('Missing extension session evidence');await page.screenshot({path:'artifacts/extensions-agent-session.png'});await click('close');
 for(const route of ['extensions','agents','robots','models','skills','simulation','harness','deployment','training','datasets','resources','billing','services','overview']){await page.goto(cfg.url+'/#'+route);await page.locator('#main h1').waitFor();}
 await page.goto(cfg.url+'/#extensions');await page.setViewportSize({width:900,height:640});const geometry=()=>page.evaluate(()=>({top:document.querySelector('.topbar').getBoundingClientRect().top,left:document.querySelector('#sidebar').getBoundingClientRect().top,window:scrollY}));const before=await geometry();await page.locator('.content-scroll').hover();await page.mouse.wheel(0,1400);await page.waitForTimeout(200);if(JSON.stringify(before)!==JSON.stringify(await geometry()))throw Error('App chrome moved');
 await page.screenshot({path:'artifacts/extensions-900x640.png'});
 if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,views:14,flow:'skill/create/export/new-version/mcp/inspect/permissions/task/approve/result',fixedChrome:true,jsErrors:errors.length}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
