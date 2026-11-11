'use strict';
// 第 04 轮（INTERFACES §6.2）：本体适配工作流——从本体档案一键启动，查看能力映射表、
// 一致性测试报告摘要与适配报告。一致性报告只覆盖替身/仿真上的 static 与 dry，不等于现场验收证据。
const adaptMethodNames={ros2_topic:'ROS 2 话题',ros2_action:'ROS 2 动作/服务',vendor_sdk:'厂商 SDK',serial:'串口协议',other:'其他接口',unsupported:'不支持',needs_confirmation:'需确认'};
const adaptMethodClass={ros2_topic:'good',ros2_action:'good',vendor_sdk:'good',serial:'good',other:'blue',unsupported:'',needs_confirmation:'warn'};
const adaptResultNames={passed:'通过',failed:'失败',skipped:'跳过',blocked:'无法测试',aborted:'测试中止',not_declared:'未声明',not_run:'未运行',not_applicable:'不适用',unknown:'未知'};
const adaptResultClass={passed:'good',failed:'bad',skipped:'warn',blocked:'bad',aborted:'bad',not_run:'warn',unknown:'warn'};
const adaptSkipNames={not_applicable:'无可测内容',blocked:'无法测试'};
const adaptEvidenceNames={simulation:'仿真/替身证据（simulation）',conformance:'一致性测试证据（conformance）'};
const ADAPT_NOT_ACCEPTANCE='一致性报告不等于现场验收证据：static/dry 只在替身或仿真后端上验证接口与停止路径，不证明真机动作安全或可上岗。';
const ADAPT_MOTION='motion 级需要你在现场确认并显式授权，且只对真机执行；本轮不会自动执行。';
const adaptResult=v=>badge(adaptResultNames[v]||v||'—',adaptResultClass[v]||'');
function adaptLatestReport(robotId){return rows('conformance_reports').filter(r=>r.robot_id===robotId).sort((a,b)=>a.created_at.localeCompare(b.created_at)).at(-1)||null;}
function adaptRunFor(robotId){return rows('workflow_runs').filter(r=>r.definition.template==='robot_adaptation'&&r.definition.robot_id===robotId).sort((a,b)=>a.created_at.localeCompare(b.created_at)).at(-1)||null;}
// conformance.report.v1: levels.<level>.{requested, ran, ok, passed, failed, skipped, blocked, blocked_reasons};
// verdict = the desktop's one-word reading. Older summaries (first round-04 build) stored strings.
function adaptVerdict(report,k){const v=(report.verdict||{})[k];if(v)return v;if(report.aborted&&k!=='motion')return 'aborted';const l=(report.levels||{})[k];return typeof l==='string'?l:(l&&l.ok===true?'passed':l&&l.ran?'failed':'not_run');}
function adaptLevels(report){return `<div class="adapt-levels">${[['static','静态 static'],['dry','空跑 dry'],['motion','运动 motion']].map(([k,t])=>{const l=(report.levels||{})[k];const counts=l&&typeof l==='object'&&l.ran?`<small class="mono">通过 ${l.passed} · 失败 ${l.failed} · 跳过 ${l.skipped} · 无法测试 ${l.blocked}</small>`:'';const why=l&&typeof l==='object'&&(l.blocked_reasons||[]).length?`<small>${h(l.blocked_reasons.slice(0,2).join('；'))}</small>`:'';return `<div data-level="${k}"><small>${t}</small>${adaptResult(adaptVerdict(report,k))}${counts}${why}</div>`;}).join('')}</div>`;}
function adaptReportFacts(report){
 const facts=[['evidence_kind',adaptEvidenceNames[report.evidence_kind]||report.evidence_kind],['simulated',report.simulated===true?'是（simulated=true）':report.simulated===false?'否':null],['field_acceptance','false（不是现场验收）'],
  ['conformance_id',Object.values(report.conformance_ids||{}).join(' / ')||report.conformance_id],['runtime',report.runtime_version?`运行时 ${report.runtime_version} · 能力标准 ${report.spec_version||'—'}`:null],['manifest_digest',report.manifest_digest]];
 return `<dl class="adapt-facts">${facts.filter(([,v])=>v).map(([k,v])=>`<div data-fact="${k}"><dt class="mono">${k}</dt><dd${k==='manifest_digest'||k==='conformance_id'?' class="mono"':''}>${h(String(v))}</dd></div>`).join('')}</dl>`;
}
function adaptMappingTable(mapping){
 const counts=Object.entries(mapping.counts||{}).filter(([,n])=>n).map(([k,n])=>`<span class="tag">${h(adaptMethodNames[k]||k)} · ${n}</span>`).join('');
 return `<div class="adapt-block" id="adapt-mapping"><div class="section-head"><h3>能力映射表 <span class="badge-count">${mapping.capabilities.length}</span></h3><span>档案 ${h(mapping.inventory_id)}</span></div><div class="tags">${counts}</div>${mapping.downgraded?notice(`${mapping.downgraded} 项映射没有引用档案证据，已由产品标为“需确认”。`,true):''}`+
 table(['标准能力','实现方式','接口','档案证据','说明'],mapping.capabilities.map(r=>`<tr data-cap="${h(r.id)}"><td><span class="cell-main">${h(r.title)}</span><span class="cell-sub mono">${h(r.id)}${r.safety==='motion'?' · 运动':''}${r.mandatory?' · 必备':''}</span></td><td>${badge(adaptMethodNames[r.implementation]||r.implementation,adaptMethodClass[r.implementation]||'')}${r.downgraded?`<span class="cell-sub">Agent 原写：${h(adaptMethodNames[r.declared_implementation])}</span>`:''}</td><td class="mono">${h(r.interface||'—')}</td><td>${r.evidence_ids.length?r.evidence_ids.map(x=>`<span class="cell-sub mono">${h(x)}</span>`).join(''):'<span class="cell-sub">无证据</span>'}</td><td><span class="cell-sub">${h(r.notes||'')}</span></td></tr>`))+'</div>';
}
function adaptConformanceSummary(report,{compact=false,run=null}={}){
 if(!report)return `<div class="adapt-block" id="adapt-conformance"><div class="section-head"><h3>一致性测试</h3></div>${empty('还没有一致性报告','从本体档案启动“本体适配”，验收映射表与适配器后，平台会在替身/仿真上运行 static 与 dry 级测试。')}</div>`;
 const declared=report.capabilities.filter(c=>c.static!=='not_declared'),hidden=report.capabilities.length-declared.length;
 const review=report.review?(report.review.decision==='approved'?badge('已验收报告','good'):badge('已退回','warn')):badge('等待验收','warn');
 return `<div class="adapt-block" id="adapt-conformance"><div class="section-head"><h3>最新一致性报告</h3>${review}</div><p class="subtitle">${date(report.created_at)} · 替身后端 ${report.simulated?simBadge():''}${report.network_isolated===true?' · 已断网运行':report.network_isolated===false?' · <b>未断网运行</b>':''}</p>${report.aborted?`<div class="notice adapt-aborted" id="adapt-aborted"><span>!</span><div><b>测试中止（结论不是通过）</b><br>${h(report.aborted)}${report.final_latched?'<br>结束时急停仍锁存：请在现场确认后由操作员解除。':''}</div></div>`:''}${report.prelatched?notice('测试开始前急停已锁存：动态级别无法测试（blocked），测试不会替操作员解除急停。',true):''}${adaptLevels(report)}${notice(h(ADAPT_NOT_ACCEPTANCE)+(report.disclaimer?'<br><small>运行时声明：'+h(report.disclaimer)+'</small>':'')+(report.self_reported_note?'<br><small>'+h(report.self_reported_note)+'</small>':''),true)}${adaptReportFacts(report)}`+
 table(['能力','static','dry','motion'],declared.map(c=>`<tr data-cap="${h(c.id)}"><td><span class="cell-main">${h(c.title)}</span><span class="cell-sub mono">${h(c.id)}</span>${c.reason?`<span class="cell-sub">${h(c.reason)}</span>`:''}</td><td>${adaptResult(c.static)}</td><td>${adaptResult(c.dry)}${c.dry==='skipped'&&c.skip_kind?`<span class="cell-sub" data-skip="${h(c.skip_kind)}">${h(adaptSkipNames[c.skip_kind]||c.skip_kind)}</span>`:''}</td><td>${c.motion==='not_applicable'?'<span class="cell-sub">不适用</span>':adaptResult(c.motion)}</td></tr>`))+
 `<p class="inline-note">另有 ${hidden} 项标准能力未在适配器中声明（未实现或需确认）。${h(ADAPT_MOTION)}</p>${!compact&&run?button('查看本体适配流程','engine-flow-run','secondary small',run.id):''}</div>`;
}
function adaptStageExtra(r,s,index){
 const a=s.adaptation;if(!a)return '';
 if(a.mapping)return adaptMappingTable(a.mapping);
 if(a.conformance)return adaptConformanceSummary(a.conformance,{compact:true});
 if(a.report)return `<div class="adapt-block"><h3>适配报告</h3><div class="inventory-metrics"><div><b>${a.report.available}</b><span>可用（替身/仿真）</span></div><div><b>${a.report.not_implemented}</b><span>未实现</span></div><div><b>${a.report.needs_onsite_verification}</b><span>需现场验证</span></div><div><b>${a.report.risks}</b><span>风险</span></div></div>${notice(h(ADAPT_NOT_ACCEPTANCE),true)}</div>`;
 if(a.adapter_files)return `<p class="inline-note" id="adapt-package">运行时脚手架生成的适配器包：${a.adapter_files.length} 个文件 · 入口 <span class="mono">${h(a.adapter_entry||'')}</span><br>脚手架已实现：${h((a.scaffold_implemented||[]).join('、')||'无')}　待实现（不声明）：${h((a.scaffold_todo||[]).join('、')||'无')}${(a.mandatory_todo||[]).length?`<br><b>必备能力未实现：${h(a.mandatory_todo.join('、'))}——实现前运行时拒绝加载，一致性测试会失败。</b>`:''}${a.next_step_note?`<br><b>${h(a.next_step_note)}</b>`:''}</p>`;
 return '';
}
function adaptStartForm(robotId){
 const robot=find('robots',robotId),report=inventoryReports(robotId).find(x=>x.id===robot?.inventory_id)||inventoryReports(robotId).at(-1);
 if(!robot||!report)return toast('该本体还没有档案');
 if(!rows('agent_models').length)return engineAction('engine-models','needed');
 const active=adaptRunFor(robotId);if(active&&['running','stopping','awaiting_review','paused'].includes(active.status))return flowAction('engine-flow-run',active.id);
 const steps=[['01','能力映射','工程助手读档案与能力标准，逐项写出实现方式与证据'],['02','生成适配器','运行时脚手架生成适配器包，工程助手实现可实现的能力'],['03','一致性测试','平台在替身/仿真上运行 static 与 dry（不调用模型）'],['04','适配报告','可用 / 未实现 / 需现场验证 / 风险（不调用模型）']];
 return form('本体适配 · '+h(robot.name),'依据最新本体档案生成适配器初稿并做一致性测试；每个阶段完成后由你验收再继续。',
  `<div class="contract-line"><b>使用档案</b><p>${h(report.summary)}</p><p class="mono">${h(report.id)} · ${inventoryDate(report.created_at)}</p></div><div class="adapt-steps">${steps.map(([n,t,d])=>`<div><span>${n}</span><b>${t}</b><small>${d}</small></div>`).join('')}</div>`+
  picker('agent_models','model_id','阶段 1、2 使用的工程模型')+`<div class="field-row">${field('每阶段 Token 观察限额','<input name="max_tokens" type="number" min="1000" max="1000000" value="200000" required>')}${field('每阶段最长分钟','<input name="max_minutes" type="number" min="1" max="120" value="20" required>')}</div>`+
  notice(h(ADAPT_MOTION)+' 一致性测试在本机独立进程中运行生成的适配器代码（替身后端，不连接机器人）。',true)+
  engineConsent('授权工程助手读取该本体档案与证据、在专属工程目录生成与修改适配器代码并调用所选模型；授权平台在本机运行生成代码的一致性测试（替身后端）。'),
  'engine-flow-adapt-start','启动第一阶段',`data-id="${h(robotId)}" data-inventory="${h(report.id)}"`);
}
async function adaptSubmit(p,f){
 const r=await api('engineering/adaptation/start',{project_id:projectId,robot_id:f.dataset.id,inventory_id:f.dataset.inventory,model_id:p.model_id,max_tokens:Number(p.max_tokens),max_minutes:Number(p.max_minutes),data_authorized:f.elements.data_authorized.checked});
 await refresh(true);return flowAction('engine-flow-run',r.id);
}
