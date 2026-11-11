'use strict';
// User-facing guidance is derived from existing records; it grants no execution rights.
let journeyResume=false;
const pagePurposes={
 robots:['连接设备','连接你的机器人','没有真机可以先添加一台仿真机器人；真机可以让助手通过 SSH 调查，或直接连接已经装好接入程序的机器人。','试用不需要准备任何东西；真机需要 SSH 地址和账号，或接入程序的地址和令牌。','一台可以查看能力、安装技能并观察运行状态的机器人；真机还会得到设备档案。'],
 agents:['准备能力','让助手完成工程工作','告诉助手需要解决的问题，让它分析本体、开发接入程序、编写测试，并交付结果。','任务目标、已接入的机器人资料，以及一个可用的助手模型。','可以检查的代码、测试记录和待处理事项；之后再验证机器人实际表现。'],
 workflows:['准备能力 · 多角色协作','让多个助手分工完成任务','把分析、开发、测试和审查交给不同角色。每一步都有交付结果，你验收后再继续。','任务目标、各阶段的交付要求、模型和用量上限。','逐阶段验收的工程文件与测试记录。可暂停、退回并重试。'],
 skills:['准备能力','安排机器人怎么工作','把机器人已有的能力组合成工作流程，例如按路线巡检、搬运物品或完成服务任务。','目标机器人，以及已经实现的技能；缺少能力时可先交给工程助手开发。','绑定机器人和技能版本的应用，下一步进行验证与测试。'],
 models:['按需使用 · 现有模型','为机器人选择所需能力','从平台选择感知、理解、规划或动作模型，加入任务并测试；兼容的端侧模型可部署到机器人。','需要解决的问题，以及机器人的设备与运行环境。','可调用或已安装的模型。是否适合任务，还要通过测试确认。'],
 training:['按需使用 · 改进模型','现有模型不够用，再训练','用你的任务数据训练或微调模型。选择平台算力并设置预算，也可以使用自己的训练环境。','训练数据、基础模型和训练配置；已有模型满足需求时可以跳过。','训练记录、模型检查点和指标，之后与原模型比较并测试。'],
 simulation:['先验证 · 虚拟环境','先在仿真中试一试','在虚拟环境中运行任务，观察失败、碰撞和完成情况；也可以生成训练数据。','兼容的机器人与环境场景，以及可用的仿真服务。','仿真记录和问题清单，供改进方案及准备现场测试。当前版本尚未内置 3D 场景编辑器。'],
 harness:['验证效果','检查机器人能否完成任务','先检查设备与应用配置，再做现场测试。查看通过了什么、失败在哪里，以及还缺哪些验证。','一个机器人应用和可连接的设备；现场测试需确认实际环境条件。','绑定当前应用与机器人版本的验证报告，通过后才进入部署。'],
 deployment:['交付使用','把验证过的任务交给机器人','发布已通过现场测试的应用，随后发起工作或停止请求，查看设备返回的结果。','目标机器人、应用版本和匹配的现场测试报告。','设备上的发布版本与运行记录；以设备实际反馈确认工作状态。'],
 datasets:['按需使用 · 数据','准备训练和测试的数据','登记采集数据的位置与版本，让训练、仿真和测试使用同一份资料。','机器人轨迹、图像、状态等数据，以及执行环境可访问的地址。','可复用的数据记录。登记地址不会自动上传或转换原始数据。'],
 extensions:['进阶设置 · 助手工具','给助手添加方法和工具','保存可复用的工作方法，或连接知识库与研发工具。只在任务需要时选择。','工作方法文件（Skills）或工具服务地址（MCP）；基本工程任务不要求配置。','可供助手选择的工具与方法，以及明确的调用权限。'],
 services:['进阶设置 · 自有服务','接入你已有的研发服务','已有模型服务、训练环境或仿真引擎时，可把它们连接到当前任务。','兼容服务的地址和接入凭据。使用平台资源时无需自行配置这里。','通过连接检查的自有服务，可以用于后续任务。'],
 resources:['平台服务','选择平台提供的模型与算力','使用平台提供的模型调用、训练或仿真服务，查看价格、设置用量上限并提交任务。','可用的平台资源和账户额度。无需自行持有供应商账号。','任务执行记录、结果和实际用量。目录为空时表示平台尚未开通相应服务。'],
 billing:['费用管理','知道用在哪里，花了多少','查看模型、训练和仿真任务的预留额度与实际用量，核对每笔记录。','使用平台服务的账户；自有 API 的费用由对应供应商收取。','可用额度、已结算用量和待核对记录。当前支付渠道尚未开通。']
};
function journeyHeading(route){const p=pagePurposes[route];return p?{label:p[0],title:p[1],subtitle:p[2]}:null;}
function journeyHelp(route){const p=pagePurposes[route];if(!p)return '';return `<details class="page-purpose"><summary>这个页面怎么用</summary><div class="purpose-grid"><div><b>需要准备</b><p>${h(p[3])}</p></div><div><b>完成后得到</b><p>${h(p[4])}</p></div></div></details>`;}
function journeyContext(){
 const apps=rows('applications'),saved=sessionStorage.getItem('journey-app:'+projectId),app=apps.find(x=>x.id===saved)||apps[0];
 const robot=app?find('robots',app.robot_id):rows('robots')[0];
 const inventory=robot&&rows('robot_inventories').find(x=>x.robot_id===robot.id&&x.id===robot.inventory_id);
 const reports=app&&robot?rows('evaluations').filter(e=>e.application_id===app.id&&e.application_digest===app.digest&&e.robot_id===robot.id&&e.robot_manifest_digest===robot.manifest_digest):[];
 const check=reports.find(e=>e.scope==='static'),onsite=reports.find(e=>e.scope==='onsite');
 const unresolved=app?rows('operations').find(o=>o.application_id===app.id&&o.kind==='onsite'&&['dispatching','unknown'].includes(o.status)&&(!onsite||o.created_at>=onsite.created_at)):null;
 const release=app&&robot&&!unresolved?rows('deployments').find(d=>d.application_id===app.id&&d.application_digest===app.digest&&d.robot_id===robot.id&&d.status==='deployed'&&onsite?.passed&&d.report_id===onsite.id):null;
 const flow=rows('workflow_runs').find(r=>!r.definition.robot_id||r.definition.robot_id===robot?.id);
 return {app,robot,inventory,check,onsite,release,flow,unresolved};
}
// ux03: a user with only a simulated robot is sent down the path that works without a model or a real
// robot (run a skill, watch it, test in simulation), never to the engineering assistant.
const journeySim=r=>Boolean(r&&(r.sim||(r.manifest||{}).simulated===true));
function journeySimNext(c){
 if(c.robot.status!=='connected')return ['重新启动仿真机器人','仿真机器人已关闭。到“我的机器人”里重新启动它，就能继续运行技能。','去我的机器人','go-robots','',['去仿真试运行','go-simulation']];
 if(!c.app){
  if(!rows('skills').length)return ['先让仿真机器人动起来','在仿真机器人上安装一个示例技能（例如“巡逻”），授权后运行并实时观看。不需要助手模型，也不需要真机。','运行技能','sim-run-pick',c.robot.id,['去仿真试运行','go-simulation']];
  return ['把技能编排成应用','技能已加入项目。把它编排成机器人应用，就能在仿真机器人上检查和做仿真测试。','创建机器人应用','application-add','',['再运行一次技能','sim-run-pick',c.robot.id]];
 }
 if(c.unresolved)return ['先核对上次测试结果','之前的请求尚未确认，请查看交付清单里的记录后再继续。','查看交付清单','deliver-open',c.app.id];
 if(!c.check?.passed)return ['检查应用是否准备好','检查技能、能力和工作步骤；这里只检查配置，不会让机器人动作。',c.check?'重新检查应用':'检查应用配置','harness-run',c.app.id,['在仿真机器人上运行技能','sim-run-pick',c.robot.id]];
 return ['在仿真里验证应用','这是仿真机器人：可以运行技能、做一次仿真测试提前发现问题；仿真结果不计为现场测试，也不能发布。要真正部署，需要接入一台真实机器人。','查看交付清单','deliver-open',c.app.id,['接入真机','go-robots']];
}
function journeyNext(c){
 if(!c.robot)return ['连接你的机器人','输入连接信息，先让平台了解设备。','连接机器人','journey-connect'];
 if(journeySim(c.robot))return journeySimNext(c);
 if(!c.inventory&&c.robot.status!=='connected')return c.robot.connection_id?['先了解这台机器人','让助手读取硬件、软件和配置，整理已有能力与未知项。','让助手了解设备','engine-discover',c.robot.id]:['补充设备连接信息','机器人已登记，还需要连接才能了解它具备哪些能力。','连接这台机器人','journey-connect',c.robot.id];
 if(!c.app){
  if(c.flow&&['running','stopping','awaiting_review','paused','blocked','interrupted'].includes(c.flow.status))return ['继续助手的准备工作','查看当前阶段、处理问题或验收产物。工程完成后还需要安装接入程序、配置应用并测试。','查看准备进度','engine-flow-run',c.flow.id];
  if(c.robot.status!=='connected'&&c.flow?.status==='completed')return ['把接入程序连接到设备','助手已交付工程文件。需要完成设备端安装并连接控制服务，才能运行机器人应用。','配置设备连接','robot-config',c.robot.id];
  if(c.robot.status==='connected'&&rows('skills').length)return ['安排机器人工作步骤','设备已连接，选择已实现的技能，组成这次任务的应用。','创建机器人应用','application-add'];
  return ['补齐完成任务所需的能力','让助手根据任务目标和设备资料，准备接入代码、测试与交付说明。','让助手准备','journey-prepare'];
 }
 if(c.unresolved)return ['先核对现场测试结果','之前的请求尚未确认，请查看设备端记录后再继续。','查看交付清单','deliver-open',c.app.id];
 if(c.robot.status!=='connected')return ['恢复设备连接','当前应用已经保存。先连接目标机器人，再检查和测试。','检查设备连接','robot-connect',c.robot.id];
 if(!c.check?.passed)return ['检查应用是否准备好','检查设备能力、模型和工作步骤；这里只检查配置，不会让机器人动作。',c.check?'重新检查应用':'检查应用配置','harness-run',c.app.id];
 if(!c.onsite?.passed)return ['验证机器人实际表现','配置检查已有通过记录。接下来完成现场测试，确认这台机器人能执行当前应用。','继续现场验证','deliver-open',c.app.id];
 if(!c.release)return ['发布已验证的应用','当前应用已有匹配的现场测试记录。发布前系统会再次核对设备与版本。','查看发布准备','deliver-open',c.app.id];
 return ['查看设备运行与发布记录','应用已发布。你可以发起启动、请求停止，并核对设备反馈；发布不表示已经开始工作。','查看运行状态','deliver-open',c.app.id];
}
function journeyOverview(){
 const p=project(),c=journeyContext(),next=p?journeyNext(c):null;
 const steps=[['明确任务',p?.auto_created?'试用项目是自动建的；需要时再描述你的任务':'告诉平台机器人要做什么',!!p&&!p.auto_created,'journey-start'],['连接机器人','识别设备和现有能力',!!(c.inventory||c.robot?.status==='connected'),'go-robots'],['准备能力','选模型、开发接入与工作步骤',!!c.app,'go-agents'],['验证效果','先在仿真里试，再到现场测试',!!c.onsite?.passed,'go-harness'],['部署使用','发布后查看设备运行反馈',!!c.release,'go-deployment']];
 return heading('从任务到上岗',p?h(p.name):'你希望机器人完成什么工作？',p?h(p.objective):'告诉平台任务目标，连接机器人，准备所需能力，验证后部署使用。',button(p?'新建任务项目':'开始设置机器人','journey-start')+button('使用指南','journey-guide','secondary'))+
 `<section class="journey-next">${p?`<div><span class="eyebrow">现在做这一步</span><h2>${h(next[0])}</h2><p>${h(next[1])}</p>${c.robot?`<small>当前机器人：${h(c.robot.name)}${c.app?' · 应用：'+h(c.app.name):''}</small>`:''}</div><div>${button(next[2],next[3],'',next[4]||'')}${next[5]?button(next[5][0],next[5][1],'secondary',next[5][2]||''):''}${button('查看完整流程','journey-guide','text-link')}</div>`:`<div><span class="eyebrow">先说目标，不必先选技术</span><h2>例如：让机器人每天巡检仓库，<br>发现异常后生成报告。</h2><p>一个任务项目保存这项工作的机器人、模型、测试和部署记录。<br>接入已有能力；不够用时，再让助手开发、训练或改进。</p></div><div>${button('描述我的任务','journey-start')}</div>`}</section>`+
 `<div class="journey-steps">${steps.map(([t,d,done,a],i)=>`<button class="journey-step ${done?'has-record':''}" data-action="${a}"><span>${String(i+1).padStart(2,'0')}</span><b>${t}</b><small>${d}</small><em>${done?'已有记录':p?.auto_created&&i===0?'自动创建 · 可补充':p?'待准备':'从这里了解'}</em></button>`).join('')}</div>`+
 (p?`<p class="inline-note">进度根据当前机器人与应用的记录判断；设备连接、生成代码和通过仿真，都不等于已经通过现场测试。</p>${rows('applications').length>1?field('查看哪个应用的进度',select('journey_application',rows('applications').map(a=>[a.id,a.name]),c.app?.id)):''}`:'')+
 `<div class="section-head"><h2>需要时，再使用这些能力</h2><span>围绕同一个任务，可按需组合</span></div><div class="journey-options">${[
 ['models','选用现成模型','需要识别、理解、规划或动作能力时，先看看已有模型。','选择模型'],
 ['training','用自己的数据训练','现有模型效果不足时，用任务数据训练或微调。','训练模型'],
 ['simulation','在仿真中试运行','先在虚拟场景中检查方案，也可生成训练数据。','进入仿真'],
 ['agents','让助手帮忙开发','接入新设备、编写程序和测试，交给工程助手处理。','交给助手']
 ].map(([route,t,d,b])=>`<article class="journey-option"><span class="agent-avatar">${icon(route)}</span><h3>${t}</h3><p>${d}</p>${button(b,'go-'+route,'secondary small')}</article>`).join('')}</div>`+
 `<div class="section-head"><h2>我的任务项目</h2><span>${state.projects.length} 项</span></div>${state.projects.length?`<div class="cards">${state.projects.map(x=>`<article class="card"><div class="card-body"><div class="card-top">${badge(x.id===projectId?'当前任务':'任务项目',x.id===projectId?'good':'')}</div><h3>${h(x.name)}</h3><p>${h(x.objective)}</p></div><div class="card-bottom">${button('查看任务','project-select','small',x.id)}</div></article>`).join('')}</div>`:empty('先创建一个任务项目','写清工作目标和完成标准，后续资料与结果会保存在这里。')}`;
}
async function journeyAction(type,id){
 if(type==='journey-guide')return dialog('从任务到机器人工作','每一步都围绕你要完成的工作。',`<div class="dialog-body">${[
 ['1. 描述任务','写清机器人在哪里、做什么，以及怎样算完成。得到一个集中保存资料与结果的任务项目。'],
 ['2. 连接机器人','输入设备连接信息。助手识别硬件、软件和接口，整理本体档案。'],
 ['3. 准备所需能力','优先选用已有模型和技能；缺少接入程序或测试时，让助手开发。模型效果不足时再训练。'],
 ['4. 验证实际效果','按需先仿真，查看问题并改进；部署前完成配置检查和现场测试。'],
 ['5. 部署并使用','发布验证过的应用，发起工作请求，查看结果和异常，持续改进。']
 ].map(([t,d])=>`<div class="contract-line"><b>${t}</b>${d}</div>`).join('')}<p class="subtitle">平台算力和模型按可用目录选择，提交前查看用量上限。当前开发预览需要配置真实服务、模型与设备；未接通的能力会明确显示，不会自动变成可用。</p>${button(project()?'返回任务工作台':'开始设置机器人',project()?'journey-home':'journey-start')}</div>`);
 if(type==='journey-home'){$('#dialog').close();location.hash='overview';return;}
 if(type==='journey-start'){if(!state.user){journeyResume=true;return auth();}return action('project-add');}
 if(!state.user){journeyResume=true;return auth();}
 if(!project())return action('project-add');
 if(type==='journey-connect')return dialog('连接你的机器人','选择你目前拥有的连接方式。',`<div class="dialog-body"><div class="contract-line"><b>有 SSH 地址、用户名和密码或密钥</b>助手可以登录调查硬件、软件和配置，形成设备档案。<div class="spacer"></div>${button('通过 SSH 连接','engine-ssh','',id||'')}</div><div class="contract-line"><b>已有平台兼容的设备接入服务</b>填写服务地址，检查它提供的能力与状态。<div class="spacer"></div>${button('连接已有接入服务',id?'robot-config':'robot-add','secondary',id||'')}</div><p class="inline-note">当前支持 SSH 调查和兼容接入服务。仅有 USB、串口或厂商专有连接时，需要相应接入插件。</p></div>`);
 if(type==='journey-prepare'){
  if(!rows('robots').length)return journeyAction('journey-connect');
  if(!rows('agent_models').length)return engineAction('engine-models','needed');
  return form('让助手准备机器人','助手将按“分析设备 → 开发接入 → 编写测试 → 审查交付”执行。每阶段完成后由你验收。',field('需要完成的工作',textarea('objective','',project().objective))+field('目标机器人',select('robot_id',rows('robots').map(r=>[r.id,r.name]),journeyContext().robot?.id))+picker('agent_models','model_id','助手使用的模型')+`<details class="page-purpose"><summary>执行范围与用量设置</summary><p>四个角色使用同一模型，每阶段最多 30,000 Tokens、15 分钟；总分配上限 240,000 Tokens。开始前会再次列出设置与费用说明。可在“助手协作流程”中自定义角色、模型和工具。</p></details>`,'journey-prepare','查看执行计划');
 }
}
async function journeySubmit(kind,p,f){
 if(kind!=='journey-prepare')return;
 const stages=defaultFlowStages.map(s=>({name:s.name,role:s.role,objective:s.objective,model_id:p.model_id,required_outputs:[s.outputs],require_command:!!s.require_command,max_tokens:30000,max_minutes:15,skill_ids:[],mcp_ids:[]}));
 const definition=await api('engineering/workflows',{project_id:projectId,name:project().name+' · 工程准备',objective:p.objective,robot_id:p.robot_id,stages,max_total_tokens:240000});
 await refresh(true);return flowAction('engine-flow-start',definition.id);
}
document.addEventListener('change',e=>{if(e.target.name==='journey_application'){sessionStorage.setItem('journey-app:'+projectId,e.target.value);render();}});
views.overview=journeyOverview;
route();
