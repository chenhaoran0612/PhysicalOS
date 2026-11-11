'use strict';
// Inventory views are read-only projections of the stored, immutable reports.
let inventoryView=null;
function inventoryReports(robotId){return rows('robot_inventories').filter(r=>r.robot_id===robotId).sort((a,b)=>a.created_at.localeCompare(b.created_at));}
function inventoryChanges(before,after){
 const group=report=>{const map=new Map();for(const c of report.components||[]){const key=JSON.stringify([c.category,c.name]);if(!map.has(key))map.set(key,[]);map.get(key).push(c);}return map;};
 const old=group(before),next=group(after),changes=[];
 const descriptions=items=>JSON.stringify(items.map(c=>c.details).sort());
 const evidence=items=>JSON.stringify([...new Set(items.flatMap(c=>c.evidence_ids||[]))].sort());
 for(const [key,items] of next){const previous=old.get(key);const [category,name]=JSON.parse(key);if(!previous)changes.push({kind:'added',category,name,before:[],after:items});else if(descriptions(previous)!==descriptions(items))changes.push({kind:'changed',category,name,before:previous,after:items});else if(evidence(previous)!==evidence(items))changes.push({kind:'evidence',category,name,before:previous,after:items});}
 for(const [key,items] of old)if(!next.has(key)){const [category,name]=JSON.parse(key);changes.push({kind:'unrecorded',category,name,before:items,after:[]});}
 return changes;
}
function inventoryDate(value){return value?new Date(value).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'时间未记录';}
function inventoryUnknowns(report){const categories=report.unconfirmed_categories||Object.keys(engineCategories).filter(k=>!(report.components||[]).some(c=>c.category===k));return [...categories.map(k=>(engineCategories[k]||k)+'：尚无有证据的条目'),...(report.unknowns||[])];}
function inventoryCards(report){
 const q=(inventoryView.query||'').trim().toLocaleLowerCase(),category=inventoryView.category||'';
 const items=(report.components||[]).filter(c=>(!category||c.category===category)&&(!q||[c.name,c.details,engineCategories[c.category]].join(' ').toLocaleLowerCase().includes(q)));
 return `<p class="subtitle" role="status">显示 ${items.length} / ${(report.components||[]).length} 项发现</p>`+Object.entries(engineCategories).map(([key,title])=>{const selected=items.filter(c=>c.category===key);return selected.length?`<section class="inventory-group"><h3>${h(title)} <span>${selected.length}</span></h3>${selected.map(c=>`<article class="inventory-component"><h4>${h(c.name)}</h4><p>${h(c.details)}</p><div class="button-row">${(c.evidence_ids||[]).map((id,i)=>button('依据 '+(i+1),'inventory-evidence','secondary small',id)).join('')}</div></article>`).join('')}</section>`:'';}).join('')+(items.length?'':empty('没有匹配的发现','换一个关键词或选择其他类别。未记录不等于设备不存在。'));
}
function inventoryDiff(before,after){
 if(!before)return notice('这是此本体最早保存的档案，暂无更早版本可比较。');
 const changes=inventoryChanges(before,after),names={added:'本版新增记录',changed:'描述变化',unrecorded:'本版未记录',evidence:'依据更新'};
 const groups=Object.keys(names).map(kind=>{const items=changes.filter(x=>x.kind===kind);return items.length?`<details class="inventory-diff" ${kind==='evidence'?'':'open'}><summary>${names[kind]} · ${items.length} 项</summary>${items.map(c=>`<div class="inventory-component"><h4>${h(engineCategories[c.category])} / ${h(c.name)}</h4>${kind==='changed'?`<div class="inventory-comparison"><div><small>上一版</small><p>${h(c.before.map(x=>x.details).join('\n'))}</p></div><div><small>所选版本</small><p>${h(c.after.map(x=>x.details).join('\n'))}</p></div></div>`:`<p>${h((c.after.length?c.after:c.before).map(x=>x.details).join('\n'))}</p>`}</div>`).join('')}</details>`:'';}).join('');
 const oldUnknown=inventoryUnknowns(before),newUnknown=inventoryUnknowns(after),added=newUnknown.filter(x=>!oldUnknown.includes(x)),absent=oldUnknown.filter(x=>!newUnknown.includes(x));
 return `<p class="subtitle">与 ${inventoryDate(before.created_at)} 保存的上一版比较。仅比较档案记录；条目缺席不表示硬件已移除，未知项未再列出也不表示已解决。</p>${groups||notice('组件描述与依据引用没有变化。')}${added.length||absent.length?`<h3>待确认项的记录变化</h3>${added.map(x=>`<p>新增待确认：${h(x)}</p>`).join('')}${absent.map(x=>`<p>本版未再列出：${h(x)}</p>`).join('')}`:''}${before.summary!==after.summary?`<details class="inventory-diff"><summary>摘要变化</summary><small>上一版</small><p>${h(before.summary)}</p><small>所选版本</small><p>${h(after.summary)}</p></details>`:''}`;
}
function inventoryOpen(robotId,reportId,keep=false){
 const robot=rows('robots').find(r=>r.id===robotId);if(!robot){toast('当前项目中没有这个本体');return;}
 const reports=inventoryReports(robotId),currentReport=reports.find(x=>x.id===robot.inventory_id)||reports.at(-1),report=reportId?reports.find(x=>x.id===reportId):currentReport;
 if(!report){dialog(h(robot.name)+' · 本体档案','尚未保存档案',`<div class="dialog-body">${empty('还没有调查档案','连接本体并完成一次调查后，硬件、软件、证据和未知项会保存在这里。',button('开始调查','engine-discover','',robot.id))}</div>`);return;}
 if(!keep||inventoryView?.robotId!==robotId)inventoryView={robotId,query:'',category:''};inventoryView.reportId=report.id;
 const index=reports.findIndex(x=>x.id===report.id),history=report.id!==currentReport.id,unknowns=inventoryUnknowns(report),evidenceCount=new Set((report.components||[]).flatMap(c=>c.evidence_ids||[])).size;
 const run=rows('agent_sessions').find(s=>s.robot_id===robotId&&s.mode==='discovery'&&engineActive(s.status));
 const options=reports.map((r,i)=>[r.id,`档案 ${String(i+1).padStart(2,'0')} · ${inventoryDate(r.created_at)}${r.id===currentReport.id?' · 当前':''}`]).reverse();
 dialog(h(robot.name)+' · 本体档案',`${h(familyNames[robot.family]||robot.family)} · ${history?'历史版本':'当前版本'} · ${inventoryDate(report.created_at)}`,`<div class="dialog-body inventory-detail">
 <div class="inventory-controls">${field('档案版本',select('inventory-version',options,report.id))}${button(run?'查看正在进行的调查':'补齐未知项 / 继续调查',run?'engine-session':'inventory-continue','small',run?.id||robotId)}</div>
 ${history?notice('正在查看历史档案；不会覆盖当前档案，也不会执行设备操作。 '+button('回到当前档案','inventory-latest','secondary small',robotId)):''}
 <p class="inventory-summary">${h(report.summary)}</p><div class="inventory-metrics"><div><b>${report.components.length}</b><span>有依据的发现</span></div><div><b>${evidenceCount}</b><span>引用证据</span></div><div><b>${unknowns.length}</b><span>待确认项</span></div><div><b>${reports.length}</b><span>档案版本</span></div></div>
 ${notice('档案记录调查时可见的信息；“已发现”不等于已验证可用。控制、部署和现场任务需要各自验证。')}
 <details class="inventory-diff"><summary>基础调查覆盖范围</summary>${report.coverage?engineCoverage(report.coverage):'<p class="subtitle">此历史版本未记录基础调查覆盖统计。</p>'}</details>
 <div class="inventory-controls inventory-search">${field('搜索硬件、软件或配置','<input name="inventory-search" type="search" placeholder="例如：相机、ROS、驱动、控制接口" value="'+h(inventoryView.query)+'">')}${field('类别',select('inventory-category',[['','全部类别'],...Object.entries(engineCategories).map(([k,t])=>[k,t+' · '+report.components.filter(c=>c.category===k).length])],inventoryView.category))}</div>
 <div id="inventory-components">${inventoryCards(report)}</div>
 <section class="inventory-unknowns"><h3>还需要确认什么</h3>${unknowns.length?'<ul>'+unknowns.map(x=>`<li>${h(x)}</li>`).join('')+'</ul>':'<p>本版未列出未知项；这不代表所有软硬件已被完整识别或通过验证。</p>'}</section>
 <h3>下一步适配建议</h3>${(report.recommendations||[]).length?'<ul>'+report.recommendations.map(x=>`<li>${h(x)}</li>`).join('')+'</ul>':'<p class="subtitle">本版没有保存适配建议。</p>'}
 <details class="inventory-diff" id="inventory-history"><summary>与上一版相比，记录了哪些变化</summary>${inventoryDiff(reports[index-1],report)}</details>
 <div class="button-row">${button('导出所选档案','export','secondary small','robot_inventories:'+report.id)}${find('agent_sessions',report.session_id)?button('查看来源调查','engine-session','secondary small',report.session_id):''}${button('交给工程助手开发','engine-work','secondary small')}${history?'':button('本体适配：生成适配器并测试','engine-flow-adapt','small',robotId)}</div>
 ${history?'':inventoryAdaptation(robotId)}</div>`);
 $('#dialog').scrollTop=0;
}
function inventoryAdaptation(robotId){
 const report=adaptLatestReport(robotId),run=adaptRunFor(robotId);
 if(!report&&!run)return '';
 return `<section class="inventory-unknowns" id="inventory-adaptation"><h3>本体适配</h3>${run?`<p>${h(run.name)} · ${flowStatus(run.status)} · 第 ${run.current_stage+1} / ${run.stages.length} 阶段 ${button('查看流程','engine-flow-run','secondary small',run.id)}</p>`:''}${report?`<p>最新一致性报告：static ${adaptResult(adaptVerdict(report,'static'))} dry ${adaptResult(adaptVerdict(report,'dry'))} motion ${adaptResult(adaptVerdict(report,'motion'))} ${button('在本体详情中查看','detail-robots','secondary small',robotId)}</p><p class="subtitle">${h(ADAPT_NOT_ACCEPTANCE)}</p>`:''}</section>`;
}
async function inventoryAction(type,id){
 if(type==='inventory-latest')return inventoryOpen(id);
 if(type==='inventory-report'){const r=rows('robot_inventories').find(r=>r.id===id);return r?inventoryOpen(r.robot_id,r.id):toast('当前项目中没有此档案');}
 if(type==='inventory-continue'){
  const robot=rows('robots').find(r=>r.id===id);if(!robot)return;
  const active=rows('agent_sessions').find(s=>s.robot_id===id&&s.mode==='discovery'&&engineActive(s.status));if(active)return engineAction('engine-session',active.id);
  const reports=inventoryReports(id),report=reports.find(r=>r.id===robot.inventory_id)||reports.at(-1),session=report&&find('agent_sessions',report.session_id);
  await engineAction(session?'engine-resume':'engine-discover',session?.id||id);
  const message=$('#dialog [name=message]')||$('#dialog [name=objective]');
  if(message&&report)message.value=('基于当前本体档案 '+report.id+' 继续调查。优先核对以下缺口，保存新的带证据档案；无法确认的项目继续标为未知。\n'+inventoryUnknowns(report).map(x=>'- '+x).join('\n')).slice(0,5000);
  return;
 }
 if(type==='inventory-evidence'){
  const view={...inventoryView},report=rows('robot_inventories').find(x=>x.id===view.reportId&&x.robot_id===view.robotId);
  if(!report||!report.components.some(c=>c.evidence_ids.includes(id)))return toast('该证据不在所选档案中');
  const evidence=await api('engineering/evidence',{robot_id:view.robotId,evidence_id:id});
  if(!$('#dialog').open||!$('#inventory-components')||inventoryView?.reportId!==view.reportId)return;
  const result=evidence.result||{},args=evidence.arguments||{},connection=find('device_connections',evidence.connection_id),limited=result.error||result.timed_out||result.truncated||result.next_offset!=null||(result.exit_code!=null&&result.exit_code!==0);
  const sources=report.components.filter(c=>c.evidence_ids.includes(id)).map(c=>c.name);
  const text=result.stdout??result.text;
  dialog('设备调查依据',inventoryDate(evidence.created_at),`<div class="dialog-body inventory-detail"><div class="button-row">${button('← 返回所选档案','inventory-back','secondary small')}</div><div class="inventory-source"><b>${h(result.label||({device_probe:'基础探针',device_exec:'SSH 命令',device_config:'配置读取'}[evidence.tool]||evidence.tool))}</b><p>用于支持：${h(sources.join('、'))}</p><p>${connection?h(connection.username+'@'+connection.host+':'+connection.port):'连接记录不可用'} · ${h(evidence.id)}</p></div>${limited?notice('这份证据存在读取限制或错误，请结合原始结果判断。截断内容未被自动补全。',true):''}${args.command?'<h3>实际命令</h3><pre class="json-view">'+h(args.command)+'</pre>':''}${result.path||args.path?'<p>读取路径：<code>'+h(result.path||args.path)+'</code></p>':''}${args.purpose?'<p>用途：'+h(args.purpose)+'</p>':''}${result.exit_code!=null?'<p>退出码：'+h(result.exit_code)+'</p>':''}${result.error?notice(h(result.error),true):''}${text!==undefined?'<h3>采集内容</h3><pre class="json-view">'+h(text||'（没有文本输出）')+'</pre>':''}${result.stderr?'<h3>错误输出</h3><pre class="json-view">'+h(result.stderr)+'</pre>':''}${result.entries?'<h3>目录内容</h3>'+table(['名称','大小'],result.entries.map(x=>'<tr><td>'+h(x.name)+'</td><td>'+h(x.size)+' B</td></tr>')):''}${result.next_offset!=null?'<p class="subtitle">采集记录仍有后续内容（偏移 '+h(result.next_offset)+'），需要继续调查才能读取。</p>':''}<details class="inventory-diff"><summary>完整证据记录</summary>${json(evidence)}</details></div>`);return;
 }
 if(type==='inventory-back'&&inventoryView)return inventoryOpen(inventoryView.robotId,inventoryView.reportId,true);
}
document.addEventListener('input',event=>{if(!event.target.matches('[name=inventory-search]')||!inventoryView)return;inventoryView.query=event.target.value;const report=rows('robot_inventories').find(x=>x.id===inventoryView.reportId);if(report&&$('#inventory-components'))$('#inventory-components').innerHTML=inventoryCards(report);});
document.addEventListener('change',event=>{if(!inventoryView)return;if(event.target.matches('[name=inventory-version]'))inventoryOpen(inventoryView.robotId,event.target.value,true);if(event.target.matches('[name=inventory-category]')){inventoryView.category=event.target.value;const report=rows('robot_inventories').find(x=>x.id===inventoryView.reportId);if(report&&$('#inventory-components'))$('#inventory-components').innerHTML=inventoryCards(report);}});
