'use strict';
// Issue #6 — the engineering assistant's investigation of a robot, in three states:
// 未调研 (the start form) → 调研中 (progress) → 调研报告 (the report). "重新调研" runs the whole
// flow again: hardware scan, investigation, a new report version. The report hands driver and
// network problems to a fix chat, where every command that changes the device is approved first.
// Like web/hardware.js, this wraps the shared engine and inventory views instead of editing them.
const INV_STEPS=[['未调研','填写目标并开始'],['调研中','扫描、体检与深入调查'],['调研报告','档案、问题与修复']];
const INV_STATE_NAMES={not_started:'未调研',running:'调研中',finished:'调研完成',unfinished:'调研未完成'};
const INV_STATE_TONES={running:'blue',finished:'good',unfinished:'warn'};
const INV_PHASE_STATES={done:['完成','good'],active:['进行中','blue'],pending:['等待',''],failed:['出错','bad'],stopped:['已停止','warn'],incomplete:['未保存档案','warn'],skipped:['未完成','warn']};
const INV_GROUPS=[['硬件驱动',/^(?:pci-driver|nvidia-driver|usb-driver)/],['网络连通性',/^net-/],['设备权限',/-perm(?::|$)/],['存储',/^rootfs/],['系统服务',/^unit:/],['内核日志',/^log-/]];
const INV_ACTIVE=['starting','running','waiting_input','cancelling'];
const INV_RERUN_OBJECTIVE='重新完整调研此本体：从基础体检开始，重新读取系统、计算硬件、传感器、接口、已有驱动与软件服务、机器人中间件和配置文件，不沿用旧档案的结论；结合本次硬件扫描结果，保存新的带证据档案，列出未知项及适配建议。';
let invBypass=false,invBaseAction=null;
const invState=robotId=>(state.investigations||[]).find(x=>x.robot_id===robotId);
const invRobots=()=>rows('robots').filter(r=>r.connection_id&&!r.sim);
const invIsChat=s=>s?.mode==='repair'&&!!s.hardware?.chat;
const invGroupOf=id=>(INV_GROUPS.find(([,re])=>re.test(id))||['其他'])[0];
const invText=t=>`<span class="inv-text">${h(t)}</span>`;
const invRepair=id=>(state.hardware_repairs||[]).find(r=>r.id===id);
const invScan=id=>(state.hardware_scans||[]).find(s=>s.id===id);
if(typeof HW_SOURCE==='object')HW_SOURCE.investigation='重新调研时扫描';

// ---- shared pieces -------------------------------------------------------------------------
function invStepper(key){
 const at=key==='finished'?2:key==='not_started'?0:1;
 return `<ol class="inv-steps" aria-label="调研阶段">${INV_STEPS.map(([t,sub],i)=>`<li class="${i<at?'done':i===at?'current':''}"${i===at?' aria-current="step"':''}><span class="inv-step-n" aria-hidden="true">${i<at?'✓':i+1}</span><span><b>${i===1&&key==='unfinished'?'调研未完成':t}</b><small>${sub}</small></span></li>`).join('')}</ol>`;
}
function invFlow(full){return `<p class="inv-flow" aria-label="调研会经历的阶段">${[full?'硬件扫描（只读）':'连接本体','基础体检','深入调查','生成档案'].map((t,i)=>`<span><b>${i+1}</b>${t}</span>`).join('<i aria-hidden="true">→</i>')}</p>`;}
function invPhases(p){
 return `<ol class="inv-phases">${p.phases.map(x=>{const [label,tone]=INV_PHASE_STATES[x.state]||[x.state,''];return `<li class="inv-phase is-${h(x.state)}"><span class="inv-phase-mark" aria-hidden="true"></span><span class="inv-phase-name">${h(x.label)}</span><span class="inv-phase-detail">${h(x.detail||'')}</span>${badge(label,tone)}</li>`;}).join('')}</ol>`;
}

// ---- 1 · not started: the start form, or a chooser when several robots qualify ---------------
async function invStart(robotId){
 await invBaseAction('engine-discover',robotId);
 const body=$('#dialog form[data-form="engine-discover"] .dialog-body');
 if(body&&!body.querySelector('.inv-steps'))body.insertAdjacentHTML('afterbegin',invStepper('not_started')+invFlow(false));
}
function invChooser(){
 const label={not_started:'开始调研',running:'查看进展',finished:'查看报告',unfinished:'查看记录'};
 dialog('工程助手调研','选择要调研的机器人。每台机器人同时只进行一次调研。',`<div class="dialog-body"><div class="inv-choose">${invRobots().map(r=>{const k=invState(r.id)?.state||'not_started',a=invState(r.id)?.progress?.phases.find(x=>x.state==='active');
  return `<article class="inv-choice"><div><b>${h(r.name)}</b><small>${h(INV_STATE_NAMES[k])}${a?' · '+h(a.label):''}</small></div>${badge(INV_STATE_NAMES[k],INV_STATE_TONES[k]||'')}${button(label[k],'engine-discover','small',r.id)}</article>`;}).join('')}</div></div>`);
}
// The investigation entry opens what the robot's state calls for, never a second run.
async function invOpen(robotId){
 if(!robotId){const list=invRobots();if(list.length>1)return invChooser();if(!list.length)return invBaseAction('engine-discover');robotId=list[0].id;}
 const st=invState(robotId);
 if(!st)return invBaseAction('engine-discover',robotId);
 if(st.state==='running')return invBaseAction('engine-session',st.session_id);
 if(st.state==='finished')return inventoryOpen(robotId);
 if(st.state==='unfinished')return invBaseAction('engine-session',st.last_session_id);
 return invStart(robotId);
}

// ---- 2 · running: a progress header on the session view ------------------------------------
function invSessionHeader(s){
 const st=invState(s.robot_id);if(!st)return '';
 if(st.progress?.session_id===s.id){
  const p=st.progress,ended=!INV_ACTIVE.includes(s.status);
  const wait=p.waiting==='approval'?notice('助手在等你批准一项操作：点下方“查看操作并决定”。',true):p.waiting==='question'?notice('助手在等你回答问题：点下方“回答问题”。'):'';
  const why={failed:'这次调研出错，没有生成报告。',interrupted:'这次调研已停止，没有生成报告。'}[s.status]||'这次调研结束了，但没有保存档案。';
  return `<section class="inv-head" aria-label="调研进展">${invStepper(st.state)}${invPhases(p)}${wait}${ended?`<div class="inv-row">${notice(why+'可以“继续会话”补充要求，也可以重新调研，完整走一遍流程。',true)}${button('重新调研','engine-inv-rerun','secondary small',s.robot_id)}</div>`:''}</section>`;
 }
 if(s.inventory_id&&s.inventory_id===st.inventory_id)return `<section class="inv-head" aria-label="调研结果">${invStepper('finished')}<div class="inv-row"><div><b>调研完成，报告已生成</b><p class="subtitle">档案、待确认项和可以修复的问题都在调研报告里。</p></div><div class="button-row">${button('查看调研报告','engine-inv-report','small',s.robot_id)}${st.state==='running'?'':button('重新调研','engine-inv-rerun','secondary small',s.robot_id)}</div></div></section>`;
 return '';
}

// ---- 3 · finished: the report, with a re-run and problems to fix -----------------------------
function invReportHead(robotId,st){
 const busy=st.state==='running'||!!st.busy_session_id,run=st.last_run;
 const lost={failed:'出错',interrupted:'被停止'}[run?.status]||'没有保存档案';
 return `<section class="inv-head inv-report-head" aria-label="调研阶段">${invStepper('finished')}<div class="inv-row"><p class="subtitle">重新调研会从只读硬件扫描开始，完整走一遍调研，生成新的档案版本；现在的档案保留为历史版本，可以对比。</p>${button('重新调研','engine-inv-rerun','secondary small',robotId,busy)}</div>
 ${run?notice(`最近一次重新调研${lost}${run.error?'：'+h(run.error):''}；下面仍是之前的报告。 `+button('查看那次调研','engine-session','secondary small',run.session_id),true):''}${busy?`<p class="inline-note">${st.state==='running'?'正在调研，完成后会生成新的报告。':'这台机器人上有修复对话正在进行，结束后才能重新调研。'}</p>`:''}</section>`;
}
function invIssues(scan){
 if(!scan.issues.length)return `<p class="inv-clear">${badge('最近一次扫描没有发现问题','good')} 仍可以直接描述要修复的内容。</p>`;
 const groups=[...INV_GROUPS.map(([t])=>t),'其他'].map(t=>[t,scan.issues.filter(i=>invGroupOf(i.id)===t)]).filter(([,list])=>list.length);
 return groups.map(([t,list])=>`<fieldset class="inv-fix-group"><legend>${t} <span>${list.length}</span></legend>${list.map(i=>`<label class="inv-issue sev-${h(i.severity)}"><input type="checkbox" name="issue_ids" value="${h(i.id)}" ${typeof hwPreselect==='function'&&hwPreselect(i)?'checked':''}><span><span class="inv-issue-head">${typeof hwBadge==='function'?hwBadge(HW_SEVERITY,i.severity):''}<b>${h(i.title)}</b></span><small>${h(i.detail)}</small></span></label>`).join('')}</fieldset>`).join('');
}
function invChatTitle(s){const first=s.chat?.first_message||(s.events||[]).find(e=>e.kind==='user')?.message||s.name;return first.length>48?first.slice(0,48)+'…':first;}
function invFixSection(robotId,st){
 const scan=typeof hwLatest==='function'?hwLatest(robotId):null;
 const chats=(st.fix_session_ids||[]).map(id=>find('agent_sessions',id)).filter(Boolean),live=chats.find(s=>INV_ACTIVE.includes(s.status));
 const blocked=st.state==='running'?'正在调研，完成后再开始修复。':st.busy_session_id&&!live?'这台机器人上有其他工程会话正在执行，结束后才能开始修复。':'';
 const history=chats.length?`<div class="inv-chats"><h4>修复对话</h4>${chats.map(s=>`<div class="check-row"><span>${date(s.created_at)} · ${h(invChatTitle(s))}</span><span class="button-row">${status(s.status)}${button('打开对话','engine-inv-chat','secondary small',s.id)}</span></div>`).join('')}</div>`:'';
 const head=`<div class="section-head"><h3 id="inv-fix-title">问题与修复</h3>${button('重新扫描硬件','engine-inv-scan','secondary small',robotId,!!blocked||!!live)}</div>`;
 if(!scan)return `<section class="inv-fix" aria-labelledby="inv-fix-title">${head}${empty('还没有硬件扫描结果','用只读命令列出驱动、网络、设备权限等问题，再在对话里让助手修复。',button('扫描硬件','engine-inv-scan','small',robotId,!!blocked))}${history}</section>`;
 const intro=`<p class="subtitle">来自 ${date(scan.created_at)} 的只读硬件扫描。勾选要修复的问题，或直接描述要修的内容（驱动、网络连通性、设备权限……）。助手在对话里排查，每条修改设备的命令都先给你看，你批准后才执行。</p>`;
 if(live)return `<section class="inv-fix" aria-labelledby="inv-fix-title">${head}${intro}<div class="inv-row">${notice('有一个修复对话正在进行。')}${button('回到对话','engine-inv-chat','small',live.id)}</div>${history}</section>`;
 const models=rows('agent_models').length?picker('agent_models','model_id','使用的模型'):notice('还没有可用的工程模型。 '+button('配置模型','engine-models','secondary small'),true);
 return `<section class="inv-fix" aria-labelledby="inv-fix-title">${head}${intro}<form data-form="engine-inv-fix" data-id="${h(robotId)}"><div class="form-error" role="alert"></div>${invIssues(scan)}
 <div class="inv-composer"><label class="field">想让助手修什么<textarea name="message" rows="3" maxlength="4000" placeholder="例如：相机驱动没有加载，帮我装好；机器人连不上外网，帮我看看网络。"></textarea><small>可以只勾选上面的问题，也可以只描述；之后在对话里继续补充。</small></label>${models}
 <label class="hardware-confirm"><input type="checkbox" name="data_authorized" required><span>授权读取本体信息并交给所选模型分析。</span></label>
 <label class="hardware-confirm"><input type="checkbox" name="modify_authorized" required><span>允许助手提出修改设备的命令。每条命令执行前都会先给我看，由我单独批准；被拒绝的命令不会执行。</span></label>
 ${blocked?`<p class="inline-note">${blocked}</p>`:''}<div class="button-row"><button class="button" type="submit" ${blocked||!rows('agent_models').length?'disabled':''}>开始对话修复</button></div></div></form>${history}</section>`;
}
function invDecorateReport(robotId){
 const st=invState(robotId),body=$('#dialog .inventory-detail');
 if(!st||!body||inventoryView?.robotId!==robotId||inventoryView.reportId!==st.inventory_id||body.querySelector('.inv-report-head'))return;
 body.insertAdjacentHTML('afterbegin',invReportHead(robotId,st));
 const after=body.querySelector('section.inventory-unknowns');
 if(after)after.insertAdjacentHTML('afterend',invFixSection(robotId,st));else body.insertAdjacentHTML('beforeend',invFixSection(robotId,st));
}
function invRerunForm(robotId){
 const robot=find('robots',robotId),st=invState(robotId);if(!robot)return toast('当前项目中没有这台机器人');
 if(st?.state==='running')return invBaseAction('engine-session',st.session_id);
 if(!rows('agent_models').length){enginePendingDiscoveryRobot=robotId;return invBaseAction('engine-models','needed');}
 form('重新调研 · '+h(robot.name),'从只读硬件扫描开始，完整走一遍调研，生成新的档案版本。',
  invStepper('not_started')+invFlow(true)+picker('agent_models','model_id','选择工程模型')+field('目标与要求',textarea('objective','',INV_RERUN_OBJECTIVE))+
  notice('本次调研不会读取旧档案的结论，以重新读取的证据为准；旧档案保留为历史版本，可以在报告里对比。')+
  engineConsent('授权读取本体信息、在专属工作目录分析资料，并调用所选模型。额外 SSH 命令和新目录将单独显示供我确认。'),'engine-inv-rerun','开始重新调研',`data-id="${h(robotId)}"`);
}

// ---- fix chat --------------------------------------------------------------------------------
function invAct(kind,text,tone='',extra=''){return `<div class="inv-act ${tone}"><span class="inv-act-icon" aria-hidden="true">${{fix:'⚒',read:'⌕',scan:'⟳',report:'✓',warn:'!',local:'›_',file:'✎'}[kind]||'•'}</span><span class="inv-act-text">${h(text)}</span>${extra}</div>`;}
function invLocal(e){
 let item={};try{item=JSON.parse(e.message);}catch{return invAct('local','在本机工作目录运行了分析命令');}
 const code=item.exitCode??item.exit_code;
 return `<details class="inv-act local"><summary><span class="inv-act-icon" aria-hidden="true">›_</span><span class="inv-act-text">本机分析命令${code==null?'':' · 退出码 '+h(code)}</span></summary><pre>${h(item.command||'')}</pre></details>`;
}
function invChatLog(s){
 const out=[],events=s.events||[];
 for(const e of events){
  const proof=e.evidence_id?button('依据','engine-evidence','secondary small',s.robot_id+':'+e.evidence_id):'';
  if(e.kind==='user')out.push(`<div class="inv-msg me"><div class="inv-bubble">${invText(e.message)}</div><time>${date(e.at)}</time></div>`);
  else if(e.kind==='message')out.push(`<div class="inv-msg ai"><span class="inv-avatar" aria-hidden="true">${icon('agents')}</span><div><div class="inv-bubble">${invText(e.message)}</div><time>${date(e.at)}</time></div></div>`);
  else if(e.kind==='fix')out.push(invAct('fix',e.message,/退出码 0）$/.test(e.message)?'good':'warn',proof));
  else if(e.kind==='tool')out.push(invAct('read','读取：'+e.message,'',proof));
  else if(e.kind==='hardware_scan')out.push(invAct('scan',e.message,'',proof));
  else if(e.kind==='repair_report')out.push(invAct('report',e.message,'good'));
  else if(e.kind==='tool_error')out.push(invAct('warn',e.message,'warn'));
  else if(e.kind==='commandExecution')out.push(invLocal(e));
  else if(e.kind==='fileChange')out.push(invAct('file','在本机工作目录修改了文件'));
  else if(['approval','stopped','error'].includes(e.kind))out.push(`<p class="inv-sys ${e.kind==='error'?'bad':''}">${h(e.message)}</p>`);
 }
 if(INV_ACTIVE.includes(s.status)&&!s.pending_approval&&!s.pending_question){
  const since=events.map(e=>e.kind).lastIndexOf('user'),said=events.slice(since+1).filter(e=>e.kind==='message').map(e=>e.message).join(''),tail=(s.stream_text||'').slice(said.length);
  out.push(`<div class="inv-msg ai live"><span class="inv-avatar" aria-hidden="true">${icon('agents')}</span><div><div class="inv-bubble">${tail?invText(tail):''}<span class="inv-typing" role="img" aria-label="助手正在处理"><i></i><i></i><i></i></span></div></div></div>`);
 }
 const a=s.pending_approval;
 // data-keep-code: the exact command being approved stays in full view (#7 folds code elsewhere).
 if(a)out.push(`<article class="inv-approval" data-keep-code aria-label="需要你批准"><div class="inv-approval-head"><b>需要你批准</b><span>只批准这一次；不批准就不会执行</span></div>${engineApproval(s)}<div class="button-row">${button('批准执行','engine-inv-approve','small',s.id+'|accept|'+a.id)}${button('拒绝','engine-inv-approve','secondary small',s.id+'|decline|'+a.id)}</div></article>`);
 if(s.pending_question)out.push(`<article class="inv-approval"><div class="inv-approval-head"><b>助手需要你补充信息</b></div><div class="button-row">${button('回答问题','engine-answer','small',s.id)}</div></article>`);
 if(s.status==='failed'&&s.error)out.push(`<p class="inv-sys bad">${h(s.error)}</p>`);
 return out.join('')||`<p class="inv-sys">正在连接机器人…</p>`;
}
function invChatScope(s){
 const ids=s.hardware?.issue_ids||[],scan=invScan(s.hardware?.scan_id),report=invRepair(s.repair_report_id);
 const titles=ids.map(id=>scan?.issues.find(i=>i.id===id)?.title||id);
 return `<div class="inv-scope"><span class="inv-scope-label">${ids.length?'所选问题':'按你的描述修复'}</span>${titles.map(t=>`<span class="tag">${h(t)}</span>`).join('')}<span class="inv-scope-meta">已执行 ${s.hardware?.fixes||0} 条修复命令</span>${button('返回调研报告','engine-inv-report','secondary small',s.robot_id)}</div>${report?`<p class="inv-report-line">${badge('修复报告','good')} ${h(report.summary)}</p>`:''}`;
}
function invChatActions(s){
 const active=INV_ACTIVE.includes(s.status);
 const label=s.pending_approval?'等待你批准上面的操作':s.pending_question?'等待你回答':s.status==='cancelling'?'正在停止…':active?'助手正在处理…':s.status==='failed'?'这一轮出错了，可以补充说明后再发送':'轮到你了';
 return `<span class="inv-chat-state ${active?'busy':''}" role="status">${h(label)}</span><span class="button-row">${active?button('停止','engine-stop','danger small',s.id):''}<button class="button" type="submit" ${active?'disabled':''}>发送</button></span>`;
}
function invChat(sid){
 const s=find('agent_sessions',sid);if(!s)return toast('找不到这个修复对话');
 dialog('对话修复 · '+h(find('robots',s.robot_id)?.name||'机器人'),h(s.model)+' · 修改设备的每条命令都要你批准',`<div class="dialog-body inv-chat"><div id="inv-chat-scope">${invChatScope(s)}</div><div class="inv-chat-log" id="inv-chat-log" role="log" aria-live="polite" aria-label="对话记录">${invChatLog(s)}</div>
 <form class="inv-chat-compose" data-form="engine-inv-say" data-id="${h(sid)}"><div class="form-error" role="alert"></div><label class="inv-sr" for="inv-chat-input">发给工程助手</label><textarea id="inv-chat-input" name="message" rows="2" maxlength="4000" placeholder="继续描述要排查或修复的内容（Enter 发送，Shift+Enter 换行）"></textarea><div class="inv-chat-actions" id="inv-chat-actions">${invChatActions(s)}</div></form></div>`);
 const d=$('#dialog');d.classList.add('wide');d.dataset.invChat=sid;invChatSync(true);$('#inv-chat-input')?.focus();
}
function invChatSync(force=false){
 const d=$('#dialog'),sid=d.dataset.invChat;if(!d.open||!sid)return;
 const s=find('agent_sessions',sid),log=$('#inv-chat-log');if(!s||!log)return;
 const sig=JSON.stringify([(s.events||[]).length,s.events?.at(-1)?.id,s.status,s.pending_approval?.id,s.pending_question?.id,(s.stream_text||'').length,s.repair_report_id,s.hardware?.fixes]);
 if(!force&&d.dataset.invChatSig===sig)return;
 d.dataset.invChatSig=sig;const near=log.scrollHeight-log.scrollTop-log.clientHeight<80;
 log.innerHTML=invChatLog(s);$('#inv-chat-scope').innerHTML=invChatScope(s);$('#inv-chat-actions').innerHTML=invChatActions(s);
 if(near||force)log.scrollTop=log.scrollHeight;
}

// ---- actions and wiring ----------------------------------------------------------------------
async function invAction(type,id){
 if(type==='engine-inv-rerun')return invRerunForm(id);
 if(type==='engine-inv-report')return inventoryOpen(id);
 if(type==='engine-inv-chat')return invChat(id);
 if(type==='engine-inv-approve'){
  // Bound to the approval that was shown: if another one replaced it, nothing is decided.
  const [sid,decision,...rest]=id.split('|'),pending=find('agent_sessions',sid)?.pending_approval;
  if(!pending||String(pending.id)!==rest.join('|')){toast('这项操作已经变化，请重新查看后再决定');await refresh(true);return invChatSync(true);}
  await api('engineering/approve',{session_id:sid,approval_id:pending.id,decision});await refresh(true);return invChatSync(true);
 }
 if(type==='engine-inv-scan'){
  const section=$('#dialog .inv-fix');if(section)section.innerHTML=`<div class="hw-scanning" role="status"><span class="hw-spinner" aria-hidden="true"></span><div><b>正在扫描硬件…</b><p>只读命令，不修改设备。</p></div></div>`;
  try{await api('engineering/hardware/scan',{project_id:projectId,robot_id:id,source:'manual'});}
  catch(err){toast(err.message);}
  await refresh(true);
  if($('#dialog').open&&inventoryView?.robotId===id&&$('#dialog .inventory-detail'))inventoryOpen(id,inventoryView.reportId,true);
 }
}
async function invSubmit(kind,p,f){
 if(kind==='engine-inv-rerun'){
  const s=await api('engineering/investigation/restart',{project_id:projectId,robot_id:f.dataset.id,model_id:p.model_id,objective:p.objective,data_authorized:f.elements.data_authorized.checked});
  await refresh(true);$('#dialog').close();return invBaseAction('engine-session',s.id);
 }
 if(kind==='engine-inv-fix'){
  const issue_ids=[...f.querySelectorAll('[name=issue_ids]:checked')].map(x=>x.value),message=(p.message||'').trim();
  if(!issue_ids.length&&!message)throw Error('请勾选要修复的问题，或描述要修复的内容');
  const s=await api('engineering/investigation/fix',{project_id:projectId,robot_id:f.dataset.id,issue_ids,message,model_id:p.model_id,
   data_authorized:f.elements.data_authorized.checked,modify_authorized:f.elements.modify_authorized.checked});
  await refresh(true);return invChat(s.id);
 }
 const message=(p.message||'').trim();if(!message)throw Error('先写下要告诉助手的内容');
 await api('engineering/investigation/fix/message',{project_id:projectId,session_id:f.dataset.id,message});
 f.elements.message.value='';await refresh(true);invChatSync(true);
 setTimeout(()=>invChatSync(true),0);   // after the form handler re-enables its button
}
{
 const baseAction=engineAction,baseSubmit=engineSubmit,baseBody=engineSessionBody,baseCard=engineRobotCard,baseRefresh=engineRefresh,baseDialog=dialog,baseInventory=inventoryOpen,baseInventoryAction=inventoryAction;
 invBaseAction=baseAction;
 engineAction=async(type,id)=>{
  if(state.user&&project()){
   if(type==='engine-discover'&&!invBypass)return invOpen(id);
   if(type==='engine-session'&&invIsChat(find('agent_sessions',id)))return invChat(id);
   // Evidence opened from the chat replaces it: offer the way back (approvals keep waiting there).
   const chat=$('#dialog').open&&$('#dialog').dataset.invChat;
   if(type==='engine-evidence'&&chat){await baseAction(type,id);$('#dialog .dialog-body')?.insertAdjacentHTML('afterbegin',`<div class="button-row inv-back">${button('← 返回对话','engine-inv-chat','secondary small',chat)}</div>`);return;}
   if(type.startsWith('engine-inv-'))return invAction(type,id);
  }
  return baseAction(type,id);
 };
 engineSubmit=async(kind,p,f)=>{
  if(kind.startsWith('engine-inv-'))return invSubmit(kind,p,f);
  const st=kind==='engine-discover'?invState(p.robot_id):null;
  if(st?.state==='running'){$('#dialog').close();toast('这台机器人正在调研，已为你打开调研进展');return baseAction('engine-session',st.session_id);}
  return baseSubmit(kind,p,f);
 };
 engineSessionBody=s=>{
  let html=baseBody(s);if(s.mode!=='discovery'||!s.robot_id)return html;
  html=html.replace(/<span class="tag">hardware_scan<\/span>/g,'<span class="tag">硬件扫描</span>');
  const head=invSessionHeader(s),at=html.indexOf('</p>');
  return !head?html:at<0?head+html:html.slice(0,at+4)+head+html.slice(at+4);
 };
 engineRobotCard=r=>{
  let html=baseCard(r);const st=invState(r.id);if(r.sim||!st)return html;
  const main={not_started:'开始调研',finished:'调研报告',unfinished:'调研未完成'}[st.state]||'开始调研';
  html=html.replace('>Agent 调查</button>','>'+main+'</button>').replace('>查看调查</button>','>'+(st.state==='running'?'调研进展':'回到修复对话')+'</button>');
  const a=st.progress?.phases.find(x=>x.state==='active');
  const tag=st.state==='running'?badge('调研中'+(a?' · '+a.label+(a.detail?' '+a.detail:''):''),'blue'):st.state==='unfinished'?badge('调研未完成','warn'):'';
  return tag?html.replace('<div class="tags">','<div class="tags">'+tag):html;
 };
 engineRefresh=()=>{baseRefresh();invChatSync();};
 dialog=(...args)=>{const d=$('#dialog');delete d.dataset.invChat;delete d.dataset.invChatSig;return baseDialog(...args);};
 inventoryOpen=(robotId,reportId,keep=false)=>{const result=baseInventory(robotId,reportId,keep);invDecorateReport(robotId);return result;};
 // "补齐未知项" on a report whose session is gone opens the start form on purpose.
 inventoryAction=async(type,id)=>{if(type!=='inventory-continue')return baseInventoryAction(type,id);invBypass=true;try{return await baseInventoryAction(type,id);}finally{invBypass=false;}};
}
document.addEventListener('keydown',e=>{
 if(e.key!=='Enter'||e.shiftKey||e.isComposing||e.keyCode===229||!e.target.matches?.('.inv-chat-compose textarea'))return;
 e.preventDefault();const b=e.target.form?.querySelector('[type=submit]');if(b&&!b.disabled)e.target.form.requestSubmit();
});
render();
