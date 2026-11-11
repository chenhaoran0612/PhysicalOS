// Round 04 browser acceptance (§6.2): inventory → start "本体适配" → mapping table →
// adapter stage → conformance summary → adaptation report → robot detail summary
// next to the capability table. Data is isolated test data; nothing contacts a robot.
// Usage: node tests/adaptation_browser.cjs CONFIG.json SHOTS_DIR
const {chromium}=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),path=require('path');
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2])),shots=process.argv[3];fs.mkdirSync(shots,{recursive:true});
 const browser=await chromium.launch({executablePath:process.env.BRAIN_CHROMIUM_PATH});
 const WIDE={width:1440,height:930},SMALL={width:900,height:640};
 const page=await browser.newPage({viewport:WIDE}),errors=[],taken=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error'&&!m.text().startsWith('Failed to load resource'))errors.push(m.text());});
 const shot=async(name,both=false)=>{await page.waitForTimeout(200);await page.screenshot({path:path.join(shots,name+'-1440.png')});taken.push(name+'-1440.png');
  if(both){await page.setViewportSize(SMALL);await page.waitForTimeout(250);await page.screenshot({path:path.join(shots,name+'-900.png')});taken.push(name+'-900.png');await page.setViewportSize(WIDE);}};
 const click=async(action,id)=>{const sel=`[data-action="${action}"]`+(id?`[data-id="${id}"]`:'');const d=page.locator('#dialog[open] '+sel);await (await d.count()?d:page.locator(sel)).first().click();};
 const submit=async()=>{await page.locator('#dialog button[type=submit]').click();await page.waitForTimeout(300);const e=await page.locator('#dialog .form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 const approve=async(stage,note)=>{await page.locator(`[data-flow-stage="${stage}"] [data-action=engine-flow-review]`).waitFor({timeout:60000});await page.locator(`[data-flow-stage="${stage}"] [data-action=engine-flow-review]`).click();
  await page.locator('#dialog [name=decision]').selectOption('approve');await page.locator('#dialog [name=note]').fill(note);await submit();await page.locator('#workflow-live').waitFor();};
 const expectText=async(sel,text)=>{await page.locator(sel).filter({hasText:text}).first().waitFor({timeout:20000});};
 const login=await page.request.post(cfg.url+'/api/login',{data:{email:cfg.email,password:cfg.password}});if(!login.ok())throw Error('login '+login.status());
 await page.goto(cfg.url+'/#robots');await page.locator(`[data-action="engine-inventory"][data-id="${cfg.robot_id}"]`).waitFor();
 // 1. the inventory page offers the one-click template
 await click('engine-inventory',cfg.robot_id);await page.locator('#dialog [data-action="engine-flow-adapt"]').waitFor();
 await page.locator('#dialog [data-action="engine-flow-adapt"]').scrollIntoViewIfNeeded();await shot('01-inventory-adapt-entry');
 await click('engine-flow-adapt',cfg.robot_id);await expectText('#dialog .adapt-steps','一致性测试');await shot('02-adapt-start-form',true);
 await page.locator('#dialog [name=data_authorized]').check();await submit();
 // 2. stage 1: mapping table with evidence, downgraded "需确认"
 await page.locator('#workflow-live').waitFor();await page.locator('#adapt-mapping').waitFor({timeout:60000});
 await expectText('#adapt-mapping tr[data-cap="base.velocity"]','ROS 2 话题');await expectText('#adapt-mapping tr[data-cap="sensor.battery"]','需确认');
 await expectText('#adapt-mapping tr[data-cap="sensor.battery"]','Agent 原写');await expectText('#adapt-mapping','已由产品标为');
 await page.locator('#adapt-mapping').scrollIntoViewIfNeeded();await shot('03-mapping-table',true);
 await approve(0,'已核对映射与档案证据');
 // 3. stage 2: scaffolded adapter implemented by the assistant (native command evidence)
 await page.locator('[data-flow-stage="1"] [data-action=engine-flow-review]').waitFor({timeout:60000});
 await expectText('[data-flow-stage="1"]','adapter/IMPLEMENTATION.md');await expectText('[data-flow-stage="1"] #adapt-package','脚手架已实现');await expectText('[data-flow-stage="1"] #adapt-package','sensor.camera.depth');await expectText('[data-flow-stage="1"] #adapt-package','以你的用户身份运行');await page.locator('[data-flow-stage="1"]').scrollIntoViewIfNeeded();await shot('04-adapter-stage');
 await approve(1,'已检查适配器代码与实现说明');
 // 4. stage 3: product-run conformance summary
 await page.locator('[data-flow-stage="2"] #adapt-conformance').waitFor({timeout:60000});
 await expectText('[data-flow-stage="2"] #adapt-conformance','不等于现场验收证据');await expectText('[data-flow-stage="2"]','产品执行：通过');
 await expectText('[data-flow-stage="2"] #adapt-conformance tr[data-cap="system.health"]','通过');
 // summary fields follow conformance.report.v1
 await expectText('[data-flow-stage="2"] #adapt-conformance [data-fact="field_acceptance"]','false');await expectText('[data-flow-stage="2"] #adapt-conformance [data-fact="evidence_kind"]','simulation');
 await expectText('[data-flow-stage="2"] #adapt-conformance [data-level="dry"]','失败 0');await expectText('[data-flow-stage="2"] #adapt-conformance','ADR-011');await expectText('[data-flow-stage="2"] #adapt-conformance','已断网运行');await expectText('[data-flow-stage="2"] #adapt-conformance','适配器所在进程');
 if(await page.locator('[data-flow-stage="2"] #adapt-aborted').count())throw Error('unexpected aborted notice');
 await expectText('[data-flow-stage="2"] #adapt-conformance [data-fact="conformance_id"]','cf-');
 await page.locator('[data-flow-stage="2"] #adapt-conformance').scrollIntoViewIfNeeded();await shot('05-conformance-stage',true);
 await page.locator('[data-action=engine-flow-artifact][data-id$=":2:conformance/dry.report.json"]').click();await expectText('#dialog pre','conformance.report.v1');await expectText('#dialog pre','"field_acceptance": false');await click('engine-flow-run');
 await approve(2,'已核对 static/dry 报告；motion 未运行');
 // 5. stage 4: adaptation report
 await page.locator('[data-flow-stage="3"] [data-action=engine-flow-review]').waitFor({timeout:60000});
 await page.locator('[data-action=engine-flow-artifact][data-id$=":3:ADAPTATION_REPORT.md"]').click();
 for(const t of ['可用能力','未实现','需现场验证','风险'])await expectText('#dialog pre',t);await shot('06-adaptation-report');await click('engine-flow-run');
 await approve(3,'报告已阅');await page.locator('#workflow-live').getByText('流程已完成',{exact:true}).waitFor();await shot('07-flow-completed');
 await click('close');
 // 6. robot detail: latest conformance summary next to the capability table
 await click('detail-robots',cfg.robot_id);await page.locator('#dialog .detail-split #adapt-conformance').waitFor({timeout:20000});
 await expectText('#dialog #adapt-conformance','已验收报告');await expectText('#dialog #adapt-conformance','motion 级需要你在现场确认');
 await expectText('#dialog #adapt-conformance tr[data-cap="base.velocity"]','未运行');
 const split=await page.evaluate(()=>{const [a,b]=document.querySelectorAll('#dialog .detail-split>div');const r1=a.getBoundingClientRect(),r2=b.getBoundingClientRect();return {side:r2.left>=r1.right-1,top:Math.abs(r1.top-r2.top)<2};});
 if(!split.side||!split.top)throw Error('conformance summary is not beside the capability table: '+JSON.stringify(split));
 await shot('08-robot-detail-conformance',true);
 const narrow=await (async()=>{await page.setViewportSize(SMALL);await page.waitForTimeout(250);const o=await page.evaluate(()=>document.querySelector('#dialog').scrollWidth<=document.querySelector('#dialog').clientWidth+1);await page.setViewportSize(WIDE);return o;})();
 if(!narrow)throw Error('robot detail overflows horizontally at 900 px');
 await click('close');
 // 7. inventory page shows the run and the latest report
 await click('engine-inventory',cfg.robot_id);await page.locator('#inventory-adaptation').waitFor();await expectText('#inventory-adaptation','流程已完成');
 await page.locator('#inventory-adaptation').scrollIntoViewIfNeeded();await shot('09-inventory-after');
 // 8. an aborted run (e.g. operator emergency stop) renders as "测试中止", never as passed
 const aborted=await page.evaluate(()=>{const r={created_at:new Date().toISOString(),simulated:true,network_isolated:true,aborted:'一致性测试已中止：急停被其他来源锁存（operator）',final_latched:true,
   levels:{static:{requested:true,ran:true,ok:true,passed:10,failed:0,skipped:0,blocked:0,blocked_reasons:[]},dry:{requested:true,ran:true,ok:false,passed:1,failed:1,skipped:0,blocked:3,blocked_reasons:['一致性测试已中止']},motion:{requested:false,ran:false,ok:null}},
   verdict:{static:'passed',dry:'aborted',motion:'not_run'},capabilities:[{id:'safety.stop',title:'停止',static:'passed',dry:'failed',motion:'not_applicable'}],field_acceptance:false,evidence_kind:'simulation'};
   const box=document.createElement('div');box.id='aborted-probe';box.innerHTML=adaptConformanceSummary(r);document.querySelector('main').prepend(box);
   return {text:box.innerText,dry:box.querySelector('[data-level="dry"]').innerText,old:adaptVerdict({levels:{dry:'passed'},aborted:'x'},'dry')};});
 if(!aborted.text.includes('测试中止')||!aborted.text.includes('急停仍锁存')||!aborted.dry.includes('测试中止')||aborted.dry.includes('通过 ·')&&!aborted.dry.includes('测试中止')||aborted.old!=='aborted')throw Error('aborted summary: '+JSON.stringify(aborted));
 await page.locator('#aborted-probe').scrollIntoViewIfNeeded();await shot('10-aborted-summary');
 if(errors.length)throw Error(errors.join('\n'));
 console.log(JSON.stringify({passed:true,shots:taken,jsErrors:errors.length}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
