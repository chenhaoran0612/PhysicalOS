const {gateSignIn}=require('./gate_helper.cjs');
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs');
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2]||process.env.BRAIN_QA_CONFIG||'/tmp/neuxmind-engineering-qa.json'));
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const page=await browser.newPage({viewport:{width:1440,height:930}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const click=async action=>{const dialog=page.locator(`#dialog[open] [data-action="${action}"]`);await (await dialog.count()?dialog:page.locator(`[data-action="${action}"]`)).first().click();};
 const fill=async(name,value)=>page.locator(`#dialog [name="${name}"]`).fill(String(value));
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();await page.waitForTimeout(300);const e=await page.locator('.form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 await page.goto(cfg.url);await gateSignIn(page,{name:'协作流程验收',email:`flow-${Date.now()}@example.test`,password:'fixture-password-123'});
 await click('journey-start');await fill('name','多角色工程交接');await fill('objective','验证阶段输出、验收、预算与交接');await submit();
 await page.goto(cfg.url+'/#agents');await click('engine-models');await click('engine-own-model');await fill('name','流程模型（本地测试）');await fill('base_url',cfg.workflow_model);await fill('model','fixture');await fill('api_key','fixture-flow-secret');await submit();await click('go-workflows');await click('engine-flow-create');
 await fill('name','本体分析与适配协作');await fill('objective','通过两个独立角色验证工程文件交接');await fill('max_total_tokens',4000);
 await page.locator('[data-action=engine-flow-remove]').last().click();await page.locator('[data-action=engine-flow-remove]').last().click();
 await page.locator('[data-action=engine-flow-up]').last().click();await page.locator('[data-action=engine-flow-down]').first().click();
 const stages=page.locator('.flow-editor');
 for(let i=0;i<2;i++){const stage=stages.nth(i);await stage.locator('[name=stage_name]').fill(i?'实现并验证接口':'梳理本体接口');await stage.locator('[name=role]').fill(i?'PIPELINE_TWO':'PIPELINE_ONE');await stage.locator('[name=stage_objective]').fill('Generate result.txt and HANDOFF.json with native command evidence.');await stage.locator('[name=outputs]').fill('result.txt');await stage.locator('[name=stage_tokens]').fill('1000');await stage.locator('[name=stage_minutes]').fill('1');await stage.locator('[name=require_command]').check();}
 await page.screenshot({path:'artifacts/workflow-editor.png'});await submit();await page.locator('#dialog').waitFor({state:'hidden'});await click('engine-flow-start');await page.locator('[name=data_authorized]').check();await submit();
 await page.locator('[data-action=engine-flow-review]').waitFor({timeout:20000});await page.screenshot({path:'artifacts/workflow-stage-review.png'});
 await page.locator('[data-action=engine-flow-artifact][data-id$=":result.txt"]').first().click();await page.locator('#dialog pre').filter({hasText:'upstream-payload'}).waitFor();await click('engine-flow-run');
 await click('engine-flow-stop');await page.locator('[data-action=engine-flow-continue]').waitFor();await click('engine-flow-continue');await click('engine-flow-review');await page.locator('[name=decision]').selectOption('approve');await fill('note','已核对交付文件及原生命令记录');await submit();
 await page.locator('[data-flow-stage="1"] [data-action=engine-flow-review]').waitFor({timeout:20000});await page.locator('[data-action=engine-flow-artifact][data-id$=":1:result.txt"]').click();await page.locator('#dialog pre').filter({hasText:'upstream-payload'}).waitFor();await click('engine-flow-run');await click('engine-flow-review');await page.locator('[name=decision]').selectOption('approve');await fill('note','已核对上游内容交接和本阶段结果');await submit();await page.locator('#workflow-live').getByText('流程已完成',{exact:true}).waitFor();await page.screenshot({path:'artifacts/workflow-completed.png'});await click('close');
 await page.setViewportSize({width:900,height:640});const geometry=()=>page.evaluate(()=>({top:document.querySelector('.topbar').getBoundingClientRect().top,left:document.querySelector('#sidebar').getBoundingClientRect().top,window:scrollY}));const before=await geometry();await page.locator('.content-scroll').hover();await page.mouse.wheel(0,1600);await page.waitForTimeout(200);if(JSON.stringify(before)!==JSON.stringify(await geometry()))throw Error('Chrome moved');await page.screenshot({path:'artifacts/workflows-900x640.png'});
 if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,flow:'editor/reorder/start/native-command/snapshot/pause/review/handoff/complete',fixedChrome:true,jsErrors:errors.length}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
