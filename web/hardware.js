'use strict';
// Issue #2 — hardware initialisation after an SSH connection.
// Read-only scan table, the problems it found, and repair sessions that run the
// built-in hardware-init Skill on the workspace default model (TokenFans, issue #4).
const HW_CATEGORIES={compute:'处理器与内存',gpu:'GPU 与加速器',storage:'存储',network:'网络',camera:'相机与视频',serial:'串口',can:'CAN 总线',usb:'USB 设备',pci:'PCI 设备',sensor:'传感器与总线',audio:'音频',input:'输入设备',power:'电源与电池',service:'系统服务'};
const HW_STATUS={ok:['正常','good'],warning:['需处理','warn'],error:['异常','bad'],unknown:['未确认','']};
const HW_SEVERITY={error:['错误','bad'],warning:['警告','warn'],info:['提示','']};
const HW_OUTCOMES={fixed:['已修复','good'],pending_reboot:['重启后生效','blue'],not_reproduced:['未复现','good'],not_fixed:['未解决','bad'],needs_user:['需现场处理','warn'],skipped:['未处理','']};
const HW_SUDO={none:'不需要',root:'以 root 账号执行',sudo_password:'通过 sudo 执行，用已保存的 SSH 登录密码完成验证（密码不会发送给模型）',sudo_nopasswd:'通过 sudo -n 执行（该账号需要配置免密 sudo，否则命令会失败）'};
const HW_RISK={low:['低风险','good'],medium:['中风险','warn'],high:['高风险','bad']};
let hardwareView=null;
const hwList=k=>(state[k]||[]).filter(x=>x.project_id===projectId);
const hwFind=(k,id)=>(state[k]||[]).find(x=>x.id===id);
const hwScans=robotId=>hwList('hardware_scans').filter(s=>s.robot_id===robotId).sort((a,b)=>b.created_at.localeCompare(a.created_at));
const hwLatest=robotId=>hwScans(robotId)[0];
const HW_SOURCE={connect:'连接后自动扫描',manual:'手动扫描',repair:'修复后重新扫描'};
const hwRepairs=robotId=>hwList('hardware_repairs').filter(r=>r.robot_id===robotId).sort((a,b)=>b.created_at.localeCompare(a.created_at));
const hwRunning=robotId=>rows('agent_sessions').find(s=>s.robot_id===robotId&&s.mode==='repair'&&engineActive(s.status));
const hwBadge=(map,key)=>{const [t,c]=map[key]||[key,''];return badge(t,c);};
// Errors and warnings need action; notes (info) are shown but never counted or pre-selected.
const hwActionable=scan=>scan.issues.filter(i=>i.severity!=='info');
const hwPreselect=i=>i.fixable==='auto'&&i.severity!=='info';

function hwSystem(scan){
 const s=scan.system||{},mem=s.memory_kib?(s.memory_kib/1048576).toFixed(1)+' GiB':'';
 const items=[['型号',s.model||s.hostname],['系统',s.os],['内核',s.kernel],['架构',s.arch],['处理器',[s.cpu,s.cores&&s.cores+' 核'].filter(Boolean).join(' · ')],['内存',mem],['Jetson',s.jetson],['SSH 用户',s.user]].filter(([,v])=>v);
 return `<dl class="hw-system">${items.map(([k,v])=>`<div><dt>${k}</dt><dd>${h(v)}</dd></div>`).join('')}</dl>`;
}
const hwDuration=ms=>ms==null?'—':ms<1000?Math.max(1,Math.round(ms))+' 毫秒':(ms/1000).toFixed(1)+' 秒';
function hwMetrics(scan){
 const m=scan.summary,issues=hwActionable(scan).length;
 return `<div class="hw-metrics"><div><b>${m.devices}</b><span>已识别硬件</span></div><div><b>${m.ok}</b><span>状态正常</span></div><div class="${m.errors?'bad':issues?'warn':'good'}"><b>${issues}</b><span>需要处理的问题</span></div><div><b>${hwDuration(scan.duration_ms)}</b><span>只读扫描用时</span></div></div>`;
}
function hwIssues(scan,robotId,locked){
 if(!scan.issues.length)return `<div class="hw-clear">${badge('未发现问题','good')}<p>扫描到的硬件都已加载驱动、网络与设备权限正常。没有问题不等于已通过功能验证，相机画面、底盘控制等仍需各自测试。</p></div>`;
 const auto=scan.issues.filter(hwPreselect).length,actionable=hwActionable(scan).length,notes=scan.issues.length-actionable;
 return `<section class="hw-issues" aria-labelledby="hw-issues-title"><div class="section-head"><h3 id="hw-issues-title">${actionable?'需要处理的问题':'提示'} <span>${actionable||notes}</span>${actionable&&notes?`<small>另有 ${notes} 条提示</small>`:''}</h3><div class="button-row">${button('重新扫描','hardware-scan','secondary small',robotId)}${button('让助手修复所选问题','hardware-repair','small',robotId,locked||!scan.issues.length)}</div></div>
 <p class="subtitle">已默认勾选可以远程处理的问题。助手按内置 Skill 诊断，每条修改设备的命令都会先给你看、由你批准后才执行。</p>
 <div class="hw-issue-list">${scan.issues.map(i=>`<label class="hw-issue sev-${h(i.severity)}"><input type="checkbox" name="hw-issue" value="${h(i.id)}" ${hwPreselect(i)?'checked':''} ${locked?'disabled':''}><span class="hw-issue-body"><span class="hw-issue-head">${hwBadge(HW_SEVERITY,i.severity)}<b>${h(i.title)}</b><span class="tag">${i.fixable==='auto'?'可远程处理':i.severity==='info'?'由你判断':'需要现场处理'}</span></span><span class="hw-issue-detail">${h(i.detail)}</span>${i.hint?`<span class="hw-issue-hint">建议：${h(i.hint)}</span>`:''}</span></label>`).join('')}</div></section>`;
}
function hwTable(scan){
 const category=hardwareView?.category||'',counts=scan.summary.categories||{};
 const chips=[['','全部',scan.devices.length],...Object.entries(HW_CATEGORIES).filter(([k])=>counts[k]).map(([k,t])=>[k,t,counts[k]])];
 const devices=scan.devices.filter(d=>!category||d.category===category);
 return `<div class="hw-filters" role="toolbar" aria-label="按类别筛选硬件">${chips.map(([k,t,n])=>`<button type="button" class="hw-chip" data-action="hardware-filter" data-id="${h(k)}" aria-pressed="${k===category}">${h(t)}<span>${n}</span></button>`).join('')}</div>
 ${table(['设备','类别','接口','驱动','状态'],devices.map(d=>`<tr class="hw-row-${h(d.status)}"><td><span class="cell-main">${h(d.name)}</span><span class="cell-sub">${h(d.detail)}</span></td><td>${h(HW_CATEGORIES[d.category]||d.category)}</td><td>${h(d.bus||'—')}</td><td><code class="hw-driver">${h(d.driver||'—')}</code></td><td>${hwBadge(HW_STATUS,d.status)}</td></tr>`))}`;
}
function hwRepairHistory(robotId){
 const reports=hwRepairs(robotId),run=hwRunning(robotId);
 if(!reports.length&&!run)return '';
 return `<section class="hw-history"><h3>修复记录</h3>${run?notice(`助手正在处理硬件问题（${h(stateNames[run.status]||run.status)}）。${run.pending_approval?'<b>有一条修复命令等待你批准。</b>':''} ${button('查看进度','engine-session','small',run.id)}`,!!run.pending_approval):''}
 ${reports.map(r=>{const counts=Object.keys(HW_OUTCOMES).map(k=>[k,r.results.filter(x=>x.outcome===k).length]).filter(([,n])=>n);
  return `<details class="hw-report"><summary><span>${date(r.created_at)} · 执行 ${r.fix_count} 条修复命令</span><span class="button-row">${counts.map(([k,n])=>badge(HW_OUTCOMES[k][0]+' '+n,HW_OUTCOMES[k][1])).join('')}</span></summary><p>${h(r.summary)}</p>
  ${table(['问题','结果','处理过程','验证'],r.results.map(x=>`<tr><td><span class="cell-main">${h(x.title)}</span><span class="cell-sub">${h(x.issue_id)}</span></td><td>${hwBadge(HW_OUTCOMES,x.outcome)}</td><td class="hw-wrap">${h(x.actions)}</td><td class="hw-wrap">${h(x.verification)}</td></tr>`))}
  ${r.followups.length?`<h4>接下来需要你做</h4><ul>${r.followups.map(x=>`<li>${h(x)}</li>`).join('')}</ul>`:''}<div class="button-row">${find('agent_sessions',r.session_id)?button('查看修复会话','engine-session','secondary small',r.session_id):''}</div></details>`;}).join('')}</section>`;
}
function hardwareOpen(robotId,keep=false){
 const robot=find('robots',robotId);if(!robot)return toast('当前项目中没有这台机器人');
 const c=find('device_connections',robot.connection_id),scan=hwLatest(robotId),run=hwRunning(robotId);
 if(!keep||hardwareView?.robotId!==robotId)hardwareView={robotId,category:''};
 const where=c?h(`${c.username}@${c.host}:${c.port}`)+(typeof sshRouteLabel==='function'?h(sshRouteLabel(c)):''):'未配置 SSH';
 if(!scan){dialog('硬件初始化 · '+h(robot.name),where,`<div class="dialog-body hardware-init">${empty('还没有扫描这台机器人的硬件','用只读命令列出处理器、GPU、相机、串口、网卡、USB 与 PCI 设备及其驱动，找出驱动缺失、网络未连通、设备无权限等问题。不调用模型，不修改设备。',button('开始扫描','hardware-scan','',robotId))}</div>`);$('#dialog').dataset.hardwareRobot=robotId;return;}
 dialog('硬件初始化 · '+h(robot.name),`${where} · ${HW_SOURCE[scan.source]||'扫描'}于 ${date(scan.created_at)} · 只读`,`<div class="dialog-body hardware-init">
 ${hwSystem(scan)}${hwMetrics(scan)}
 ${scan.notes.length?notice(scan.notes.map(h).join('<br>'),true):''}
 ${run?notice('助手正在处理这台机器人的硬件问题，完成前不能再发起修复。 '+button('查看进度','engine-session','small',run.id),true):''}
 ${hwIssues(scan,robotId,!!run)}
 <section class="hw-devices" aria-labelledby="hw-devices-title"><div class="section-head"><h3 id="hw-devices-title">硬件清单</h3>${scan.issues.length?'':button('重新扫描','hardware-scan','secondary small',robotId)}</div><div id="hw-table">${hwTable(scan)}</div></section>
 ${hwRepairHistory(robotId)}
 <div class="button-row hw-next">${button('下一步：让 Agent 深入调查本体','engine-discover','small',robotId)}${button('原始扫描输出','hardware-raw','secondary small',scan.id)}${button('查看内置 Skill','hardware-skill','secondary small')}</div>
 </div>`);
 Object.assign($('#dialog').dataset,{hardwareRobot:robotId,hardwareScan:scan.id,hardwareSig:hwSignature(robotId)});
}
// Right after a successful SSH connection: scan first, then show the table.
async function hardwareAfterConnect(robotId){
 const robot=find('robots',robotId);
 dialog('硬件初始化 · '+h(robot?.name||''),'连接成功',`<div class="dialog-body hardware-init"><div class="hw-scanning" role="status"><span class="hw-spinner" aria-hidden="true"></span><div><b>正在扫描硬件…</b><p>用只读命令读取处理器、GPU、相机、串口、网卡、USB 与 PCI 设备及驱动状态。不调用模型，不修改设备，通常需要几秒。</p></div></div></div>`);
 $('#dialog').dataset.hardwareRobot=robotId;
 return hardwareScan(robotId,'connect');
}
async function hardwareScan(robotId,source='manual'){
 let result;
 try{result=await api('engineering/hardware/scan',{project_id:projectId,robot_id:robotId,source});}
 catch(err){
  if($('#dialog').open&&$('#dialog').dataset.hardwareRobot===robotId)return dialog('硬件初始化','扫描没有完成',`<div class="dialog-body hardware-init">${notice(h(err.message),true)}<div class="button-row">${button('重试扫描','hardware-scan','',robotId)}${button('跳过，让 Agent 调查本体','engine-discover','secondary',robotId)}</div></div>`);
  throw err;
 }
 await refresh(true);
 if($('#dialog').open&&$('#dialog').dataset.hardwareRobot===robotId)return hardwareOpen(robotId,true);
 toast(`硬件扫描完成：${result.summary.devices} 项硬件，${result.issues.length} 个问题`);
}
function hardwareRepairForm(robotId){
 const scan=hwLatest(robotId),chosen=[...document.querySelectorAll('#dialog [name=hw-issue]:checked')].map(x=>x.value);
 if(!scan)return toast('请先扫描硬件');
 if(!chosen.length)return toast('请至少勾选一个要处理的问题');
 const issues=scan.issues.filter(i=>chosen.includes(i.id)),models=rows('agent_models');
 const tokenfans=state.model_compute;
 const modelField=models.length?picker('agent_models','model_id','使用的模型')+(tokenfans?.connected?`<p class="inline-note hw-model-note">默认使用“云平台账户”中设置的 TokenFans 默认模型${tokenfans.default_model?'（'+h(tokenfans.default_model)+'）':''}。</p>`:''):notice('还没有可用的模型。工程助手的大模型只对接 TokenFans：先在“云平台账户”连接，再回到这里勾选问题开始修复。 '+button('配置模型','engine-models','secondary small','needed'),true);
 form('让助手修复硬件问题',`${issues.length} 个问题 · 内置 Skill hardware-init`,`<div class="hw-repair-issues">${issues.map(i=>`<label class="extension-choice"><input type="checkbox" name="issue_ids" value="${h(i.id)}" checked><span><b>${h(i.title)}</b><small>${h(i.hint||i.detail)}</small></span></label>`).join('')}</div>
 ${modelField}
 ${notice('产品会直接拒绝明显危险的命令（重启、刷写固件、格式化磁盘、修改 SSH 或账号、发布运动指令等）。这只是兜底规则，无法覆盖所有写法：每条命令执行前请以你的核对为准。需要重启才能生效的改动会在报告中写明，由你安排。')}
 ${engineConsent('授权读取本体信息并交给所选模型分析，费用由对应模型平台计量。')}
 <label class="hardware-confirm"><input type="checkbox" name="modify_authorized" required><span>允许助手提出修改设备的命令（加载驱动、安装驱动或固件包、调整网络和设备权限）。每条命令执行前会显示原命令、风险和回退方法，由我逐条批准。</span></label>`,
 'hardware-repair','开始修复',`data-id="${h(robotId)}" data-scan="${h(scan.id)}"`);
 if(!models.length)$('#dialog [type=submit]').disabled=true;
}
async function hardwareAction(type,id){
 if(!state.user)return auth();if(!project())return action('project-add');
 if(type==='hardware-open')return hardwareOpen(id);
 if(type==='hardware-scan'){const robot=find('robots',id);dialog('硬件初始化 · '+h(robot?.name||''),'重新扫描',`<div class="dialog-body hardware-init"><div class="hw-scanning" role="status"><span class="hw-spinner" aria-hidden="true"></span><div><b>正在扫描硬件…</b><p>只读命令，不修改设备。</p></div></div></div>`);$('#dialog').dataset.hardwareRobot=id;return hardwareScan(id);}
 if(type==='hardware-filter'){if(!hardwareView)return;hardwareView.category=id||'';const scan=hwLatest(hardwareView.robotId);if(scan&&$('#hw-table'))$('#hw-table').innerHTML=hwTable(scan);return;}
 if(type==='hardware-repair')return hardwareRepairForm(id);
 if(type==='hardware-raw'){const scan=await api('engineering/hardware/detail',{project_id:projectId,scan_id:id});return dialog('原始扫描输出',date(scan.created_at)+' · 退出码 '+h(scan.raw?.exit_code??'—'),`<div class="dialog-body hardware-init"><div class="button-row">${button('← 返回硬件清单','hardware-open','secondary small',scan.robot_id)}</div>${scan.raw?.truncated||scan.raw?.timed_out?notice('扫描输出被截断或超时，结果可能不完整。',true):''}<h3>采集内容</h3><pre class="json-view hw-raw">${h(scan.raw?.stdout||'（没有输出）')}</pre>${scan.raw?.stderr?'<h3>错误输出</h3><pre class="json-view">'+h(scan.raw.stderr)+'</pre>':''}</div>`);}
 if(type==='hardware-skill'){const skill=await api('engineering/hardware/skill',{project_id:projectId});return dialog('内置 Skill · '+h(skill.name),'v'+h(skill.version)+' · 摘要 '+h(skill.digest.slice(0,12)),`<div class="dialog-body hardware-init">${notice('硬件修复会话会固定加载这个 Skill。它随应用版本发布，项目中不可修改。')}${Object.entries(skill.files).map(([name,text])=>`<details class="hw-report" ${name==='SKILL.md'?'open':''}><summary>${h(name)}</summary><pre class="json-view hw-raw">${h(text)}</pre></details>`).join('')}</div>`);}
}
async function hardwareSubmit(kind,p,f){
 if(kind!=='hardware-repair')return;
 const issue_ids=[...f.querySelectorAll('[name=issue_ids]:checked')].map(x=>x.value);
 if(!issue_ids.length)throw Error('请至少保留一个要处理的问题');
 const result=await api('engineering/hardware/repair',{project_id:projectId,robot_id:f.dataset.id,scan_id:f.dataset.scan,issue_ids,model_id:p.model_id||undefined,
  data_authorized:f.elements.data_authorized.checked,modify_authorized:f.elements.modify_authorized.checked});
 await refresh(true);$('#dialog').close();return engineAction('engine-session',result.id);
}
// The whole command, never clipped, with its size: nothing can hide below a scroll edge.
const hwCommand=c=>`<pre class="json-view hw-command">${h(c)}</pre><p class="hw-command-size">共 ${String(c).split('\n').length} 行 · ${String(c).length} 个字符 · 已完整显示</p>`;
// Approval of one device_fix command (engine sessions show every other kind as before).
function hardwareFixApproval(s){
 const p=s.pending_approval.request,scan=hwFind('hardware_scans',s.hardware?.scan_id),titles=p.issue_ids.map(id=>scan?.issues.find(i=>i.id===id)?.title||id);
 return `<div class="contract-line hw-fix"><div class="section-head"><b>在机器人上执行修复命令</b>${hwBadge(HW_RISK,p.risk)}</div><p>${h(p.username)}@${h(p.host)}:${p.port}</p><p class="hw-fix-purpose">${h(p.purpose)}</p>${hwCommand(p.command)}
 <div class="runtime-list"><div class="runtime-line"><span>对应问题</span><b>${titles.map(h).join('<br>')}</b></div><div class="runtime-line"><span>管理员权限</span><b>${h(HW_SUDO[p.sudo]||p.sudo)}</b></div><div class="runtime-line"><span>如何回退</span><b>${h(p.rollback)}</b></div><div class="runtime-line"><span>最长执行</span><b>${p.timeout_seconds} 秒 · 仅批准这一次</b></div></div>
 ${p.warnings?.length?`<ul class="hw-warnings">${p.warnings.map(w=>`<li>${h(w)}</li>`).join('')}</ul>`:''}</div>`+notice('这条命令会修改设备，远端没有沙箱，执行时使用该 SSH 账号的实际权限。请确认它只针对上面列出的问题；拒绝后助手会改用其他方法，或把问题记为需要你处理。',true);
}
// In a repair session an investigation command is treated as a possible change.
function hardwareExecApproval(s){
 const p=s.pending_approval.request;
 return `<div class="contract-line hw-fix"><div class="section-head"><b>在机器人上执行 SSH 命令（硬件修复会话）</b></div><p>${h(p.username)}@${h(p.host)}:${p.port}</p><p class="hw-fix-purpose">${h(p.purpose)}</p>${hwCommand(p.command)}<div class="runtime-list"><div class="runtime-line"><span>最长执行</span><b>${p.timeout_seconds} 秒 · 仅批准这一次</b></div></div></div>`+notice('这条命令使用该 SSH 账号的实际权限，可能修改设备。在修复会话里它按一次改动记录：执行后，助手要再重新扫描，才能声明问题已修复。',true);
}
function hardwareSessionPanel(s){
 const scan=hwFind('hardware_scans',s.hardware?.scan_id),report=hwFind('hardware_repairs',s.repair_report_id),ids=s.hardware?.issue_ids||[];
 const results=report?Object.fromEntries(report.results.map(r=>[r.issue_id,r])):{};
 return `<div class="contract-line hw-session"><div class="section-head"><b>硬件初始化 · ${ids.length} 个待处理问题</b>${button('查看硬件清单','hardware-open','secondary small',s.robot_id)}</div><ul>${ids.map(id=>{const issue=scan?.issues.find(i=>i.id===id),r=results[id];return `<li>${r?hwBadge(HW_OUTCOMES,r.outcome):badge('处理中')} ${h(issue?.title||id)}</li>`;}).join('')}</ul><p class="subtitle">已执行 ${s.hardware?.fixes||0} 条修复命令 · 内置 Skill ${h(s.hardware?.skill?.name||'hardware-init')} v${h(s.hardware?.skill?.version||'')}</p>${report?`<p>${h(report.summary)}</p>`:''}</div>`;
}
function hardwareCardTag(r){
 const scan=hwLatest(r.id);if(!scan)return '<span class="tag">硬件待扫描</span>';
 const n=hwActionable(scan).length;
 return n?badge(`硬件 ${scan.summary.devices} 项 · ${n} 个问题`,scan.summary.errors?'bad':'warn'):badge(`硬件 ${scan.summary.devices} 项正常`,'good');
}
// Wrap the shared engine views instead of editing them (they are also changed by #3/#4).
{
 const baseApproval=engineApproval,baseBody=engineSessionBody,baseCard=engineRobotCard,labels={fix:'修复操作',hardware_scan:'硬件扫描',repair_report:'修复报告',incomplete:'待继续'};
 engineApproval=s=>{const m=s.pending_approval?.method;return m==='device_fix/requestApproval'?hardwareFixApproval(s):s.mode==='repair'&&m==='device_exec/requestApproval'?hardwareExecApproval(s):baseApproval(s);};
 engineSessionBody=s=>{let html=baseBody(s);if(s.mode!=='repair')return html;html=html.replace(/<span class="tag">(fix|hardware_scan|repair_report|incomplete)<\/span>/g,(m,k)=>`<span class="tag">${labels[k]}</span>`);const at=html.indexOf('</p>');return at<0?hardwareSessionPanel(s)+html:html.slice(0,at+4)+hardwareSessionPanel(s)+html.slice(at+4);};
 engineRobotCard=r=>{const html=baseCard(r);if(r.sim||!r.connection_id)return html;return html.replace('<div class="tags">','<div class="tags">'+hardwareCardTag(r)).replace('<div class="card-bottom"><div class="button-row">','<div class="card-bottom"><div class="button-row">'+button('硬件初始化','hardware-open','secondary small',r.id));};
}
$('#dialog').addEventListener('close',()=>{delete $('#dialog').dataset.hardwareRobot;delete $('#dialog').dataset.hardwareSig;});
// Any other dialog replaces the hardware view: a scan that finishes later must not reopen over it.
{const baseDialog=dialog;dialog=(...args)=>{const d=$('#dialog');delete d.dataset.hardwareRobot;delete d.dataset.hardwareSig;delete d.dataset.hardwareScan;return baseDialog(...args);};}
// Keep an open hardware dialog current while a repair runs (the 4-second state refresh):
// re-render only when this robot's scan, reports or repair session changed, keeping the ticked issues.
const hwSignature=robotId=>{const run=hwRunning(robotId);return JSON.stringify([hwLatest(robotId)?.id,hwRepairs(robotId).map(r=>r.id),run?.status,run?.pending_approval?.id]);};
const hardwareBaseRefresh=engineRefresh;
engineRefresh=()=>{
 hardwareBaseRefresh();
 const d=$('#dialog'),robotId=d.dataset.hardwareRobot;
 if(!d.open||!robotId||!$('#dialog .hw-metrics')||$('#dialog form')||d.dataset.hardwareSig===hwSignature(robotId))return;
 const scanId=d.dataset.hardwareScan,ticked=new Set([...d.querySelectorAll('[name=hw-issue]:checked')].map(x=>x.value)),scroll=d.scrollTop;
 hardwareOpen(robotId,true);
 if(scanId===d.dataset.hardwareScan)d.querySelectorAll('[name=hw-issue]').forEach(x=>{if(!x.disabled)x.checked=ticked.has(x.value);});
 d.scrollTop=scroll;
};
render();
