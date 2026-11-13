// Issue #6 browser acceptance: the investigation entry shows one of three states — the start
// form, the progress of a running investigation (never a second form), the report — plus a
// fix chat from the report and a full re-run. Fixture: tests/investigation_browser_fixture.py.
// Usage: node tests/investigation_browser.cjs CONFIG.json [screenshot dir]
// BRAIN_BROWSER=webkit runs it on WebKit, the engine of the desktop app (WKWebView); default Chromium.
const {gateSignIn}=require('./gate_helper.cjs');
const engine=require(process.env.BRAIN_PLAYWRIGHT_MODULE||'playwright')[process.env.BRAIN_BROWSER||'chromium'];
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {connectTokenFans}=require('./tokenfans_browser.cjs');
let failed=async()=>{};
(async()=>{
 const cfg=JSON.parse(fs.readFileSync(process.argv[2]||'/tmp/neuxmind-investigation-ui.json'));
 const shots=process.argv[3]||'artifacts';fs.mkdirSync(shots,{recursive:true});
 const browser=await engine.launch(engine.name()==='chromium'?{executablePath:process.env.BRAIN_CHROMIUM_PATH}:{});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 failed=async()=>{await page.screenshot({path:path.join(shots,'investigation-failure.png')}).catch(()=>{});console.error('page errors:',errors);};
 const dlg=page.locator('#dialog');
 const shot=name=>page.screenshot({path:path.join(shots,'investigation-'+name+'.png')});
 const click=async a=>{const d=page.locator(`#dialog[open] [data-action="${a}"]`);await (await d.count()?d:page.locator(`[data-action="${a}"]`)).first().click();};
 const fill=async(n,v)=>dlg.locator(`[name="${n}"]`).first().fill(String(v));
 const submit=async(form='#dialog form')=>{await page.locator(form).locator('button[type=submit]').first().click();await page.waitForTimeout(200);const e=await page.locator(form+' .form-error').allTextContents();if(e.some(Boolean))throw Error(e.join(';'));};
 const step=async want=>{const t=await dlg.locator('.inv-steps li[aria-current=step]').first().innerText();if(!t.includes(want))throw Error(`current step is "${t}", expected ${want}`);};
 const approveInSession=async()=>{await page.locator('#engine-live [data-action=engine-approval]').waitFor({timeout:30000});await click('engine-approval');await dlg.getByText('在机器人上执行 SSH 命令',{exact:true}).waitFor();await dlg.locator('[name=decision]').selectOption('accept');await submit();};
 const result={};

 await page.goto(cfg.url);await gateSignIn(page,{name:'调研状态验收',email:`investigation-${crypto.randomBytes(4).toString('hex')}@example.test`,password:'Inv'+crypto.randomBytes(6).toString('hex')+'7'});
 await click('journey-start');await fill('name','巡检机器人接入');await fill('objective','认识机器人并修好驱动与网络');await submit();
 await page.goto(cfg.url+'/#robots');await click('engine-ssh');await fill('name','园区巡检狗');await fill('host','127.0.0.1');await fill('port',cfg.ssh_port);await fill('username','robot');await fill('password',cfg.ssh_password);await submit();
 await dlg.getByText('确认 SSH 主机指纹',{exact:true}).waitFor();await dlg.locator('input[type=checkbox]').check();await submit();
 // Hardware initialisation opens after connecting (#2); its "next step" is the investigation entry.
 await dlg.locator('[data-action="engine-discover"]').first().click({timeout:30000});
 await dlg.getByText('这一步需要先给助手配一个大模型',{exact:true}).waitFor();await click('go-cloud');await connectTokenFans(page,cfg,{navigate:false});

 // 1 · not started: the start form, with the three-state stepper.
 await dlg.getByText('让 Agent 调查本体',{exact:true}).waitFor();await step('未调研');
 if(!(await dlg.locator('.inv-flow').innerText()).includes('基础体检'))throw Error('No phase outline on the start form');
 await shot('1-not-started');result.notStarted=true;
 await dlg.locator('[name=data_authorized]').check();await submit();

 // 2 · running: progress, waiting for an SSH command approval.
 await page.locator('#engine-live .inv-phases').waitFor({timeout:30000});await page.locator('#engine-live [data-action=engine-approval]').waitFor({timeout:30000});
 await step('调研中');
 const phases=await page.locator('#engine-live .inv-phases').innerText();
 for(const t of ['连接本体','基础体检','深入调查','生成档案'])if(!phases.includes(t))throw Error('Missing phase '+t);
 if(!(await page.locator('#engine-live .inv-head').innerText()).includes('等你批准'))throw Error('Waiting for approval not shown in progress');
 await shot('2-running');
 // The issue: opening the investigation again while it runs must show the progress, not another form.
 await click('close');await page.goto(cfg.url+'/#robots');
 if(!(await page.locator('#main .card').first().innerText()).includes('调研中'))throw Error('Robot card does not say 调研中');
 await page.locator('#main button',{hasText:'调研进展'}).first().click();await page.locator('#engine-live .inv-phases').waitFor();
 if(await page.locator('#dialog form[data-form="engine-discover"]').count())throw Error('Start form offered during a run');
 await click('close');await page.goto(cfg.url+'/#agents');await page.locator('#main [data-action="engine-discover"]').first().click();
 await page.locator('#engine-live .inv-phases').waitFor();
 if(await page.locator('#dialog form[data-form="engine-discover"]').count())throw Error('Agents page offered a second investigation');
 result.runningOpensProgress=true;
 await approveInSession();

 // 3 · finished: the report, with re-run and problems to fix.
 await page.locator('#engine-live [data-action=engine-inv-report]').waitFor({timeout:30000});await step('调研报告');
 await click('engine-inv-report');await dlg.locator('.inv-report-head').waitFor();await step('调研报告');
 const fix=dlg.locator('.inv-fix');await fix.waitFor();
 const groups=await fix.locator('legend').allInnerTexts();
 if(!groups.some(g=>g.startsWith('设备权限')))throw Error('Fix section has no 设备权限 group: '+groups.join(','));
 await shot('3-report');await fix.scrollIntoViewIfNeeded();await shot('3-report-fix');result.report=true;
 await click('close');await page.goto(cfg.url+'/#robots');await page.locator('#main button',{hasText:'调研报告'}).first().click();await dlg.locator('.inv-report-head').waitFor();

 // Fix chat from the report: one issue plus the user's own words.
 await fix.locator('[name=issue_ids]').evaluateAll(xs=>xs.forEach(x=>{x.checked=false;}));
 await fix.locator('[name=issue_ids][value^="serial-perm"]').first().check();
 await fix.locator('[name=message]').fill('底盘串口打不开，帮我修好');
 await fix.locator('[name=data_authorized]').check();await fix.locator('[name=modify_authorized]').check();
 await submit('#dialog .inv-fix form');
 await dlg.locator('.inv-chat').waitFor();await dlg.locator('.inv-approval').waitFor({timeout:30000});
 if(!(await dlg.locator('.inv-approval').innerText()).includes('usermod -aG dialout robot'))throw Error('Approval card lacks the exact command');
 if(!(await dlg.locator('.inv-chat-compose [type=submit]').isDisabled()))throw Error('Send enabled while the assistant works');
 await shot('4-chat-approval');
 await dlg.locator('.inv-approval [data-action="engine-inv-approve"]',{hasText:'批准执行'}).click();
 await dlg.locator('#inv-chat-log').getByText(cfg.answers[0]).waitFor({timeout:30000});
 await page.waitForFunction(()=>!document.querySelector('.inv-chat-compose [type=submit]').disabled,null,{timeout:15000});
 await page.locator('#inv-chat-input').fill('再看看 eth1 为什么没有网');await page.locator('#inv-chat-input').press('Enter');
 await dlg.locator('#inv-chat-log').getByText(cfg.answers[1]).waitFor({timeout:30000});
 const mine=await dlg.locator('.inv-msg.me .inv-bubble').allInnerTexts();
 if(mine.length!==2||mine[1]!=='再看看 eth1 为什么没有网')throw Error('User messages: '+JSON.stringify(mine));
 if(!(await dlg.locator('.inv-act.good').first().innerText()).includes('退出码 0'))throw Error('Fix command result not shown');
 // Evidence of the fix opens in its own view, with the way back to the conversation.
 await dlg.locator('.inv-act.good [data-action="engine-evidence"]').first().click();await dlg.getByText('设备调查证据',{exact:true}).waitFor();
 await click('engine-inv-chat');await dlg.locator('#inv-chat-log').getByText(cfg.answers[1]).waitFor();
 await shot('5-chat');result.fixChat=true;

 // Full re-run: hardware scan first, then the investigation, then a second report version.
 await click('engine-inv-report');await dlg.locator('.inv-report-head').waitFor();
 if(!(await dlg.locator('.inv-chats').innerText()).includes('底盘串口打不开'))throw Error('Chat not listed on the report');
 await click('engine-inv-rerun');await dlg.getByText('开始重新调研').waitFor();await step('未调研');
 if(!(await dlg.locator('.inv-flow').innerText()).includes('硬件扫描'))throw Error('Re-run outline lacks the hardware scan');
 await dlg.locator('[name=data_authorized]').check();await submit();
 await page.locator('#engine-live .inv-phases').waitFor({timeout:30000});
 if(!(await page.locator('#engine-live .inv-phase').first().innerText()).includes('硬件扫描'))throw Error('Re-run does not start with the hardware scan');
 // The scan runs before the model, so it is done by the time the run waits for the SSH approval.
 await page.locator('#engine-live [data-action=engine-approval]').waitFor({timeout:30000});
 const first=await page.locator('#engine-live .inv-phase').first().innerText();
 if(!/项硬件/.test(first))throw Error('Scan phase has no result: '+first);
 await shot('6-rerun-running');
 await approveInSession();
 await page.locator('#engine-live [data-action=engine-inv-report]').waitFor({timeout:30000});
 await click('engine-inv-report');await dlg.locator('.inv-report-head').waitFor();
 const versions=await dlg.locator('[name=inventory-version] option').count();if(versions!==2)throw Error('Expected 2 report versions, got '+versions);
 await shot('7-rerun-report');result.rerun=true;

 // Narrow screens: no horizontal overflow on the report and the chat.
 for(const [w,hgt] of [[900,700],[375,812]]){
  await page.setViewportSize({width:w,height:hgt});await page.waitForTimeout(200);
  const over=await page.evaluate(()=>{const d=document.querySelector('#dialog');return [document.documentElement.scrollWidth>innerWidth,d.scrollWidth>d.clientWidth+1];});
  if(over.some(Boolean))throw Error(`Overflow at ${w}px: `+over);
  await shot('8-report-'+w);
 }
 await dlg.locator('.inv-chats [data-action="engine-inv-chat"]').first().click();await dlg.locator('.inv-chat').waitFor();
 if(await page.evaluate(()=>document.querySelector('#dialog').scrollWidth>document.querySelector('#dialog').clientWidth+1))throw Error('Chat overflows at 375px');
 await shot('9-chat-375');result.narrow=true;
 if(errors.length)throw Error(errors.join('\n'));
 console.log(JSON.stringify({passed:true,...result,jsErrors:errors.length}));await browser.close();
})().catch(async e=>{console.error(e);await failed();process.exit(1);});
