'use strict';
let revisionDraft=null;
function revisionHistory(a){const root=a.root_application_id||a.id,versions=rows('applications').filter(x=>(x.root_application_id||x.id)===root);return `<details class="page-purpose"><summary>版本记录（${versions.length}）</summary>${versions.map(x=>`<div class="contract-line"><b>${h(x.name)} · ${h(x.version)}${x.id===a.id?' · 当前查看':''}</b><p>${date(x.created_at)}${x.parent_application_id?' · 基于 '+h(find('applications',x.parent_application_id)?.version):''}</p>${x.id!==a.id?button('查看此版本','deliver-open','secondary small',x.id):''}</div>`).join('')}</details>`;}
function revisionStep(s={}){const el=document.createElement('div');el.className='step-editor revision-step';el.innerHTML=`<div class="section-head"><h3 class="revision-number"></h3><div class="button-row">${button('上移','revision-up','secondary small')}${button('下移','revision-down','secondary small')}${button('移除','revision-remove','secondary small')}</div></div>${field('使用技能',select('skill_id',rows('skills').map(x=>[x.id,x.name+' · '+x.version]),s.skill_id))}<div class="step-params"></div>${field('最长执行时间（秒）',`<input name="timeout_s" type="number" min="0.001" max="86400" step="any" value="${h(s.timeout_s??60)}" required>`)}`;$('#revision-steps').append(el);uxRenderStepParams(el,s.parameters);revisionNumbers();}
function revisionNumbers(){const all=[...document.querySelectorAll('.revision-step')];all.forEach((el,i)=>{$('.revision-number',el).textContent='工作步骤 '+(i+1);$('[data-action=revision-up]',el).disabled=i===0;$('[data-action=revision-down]',el).disabled=i===all.length-1;});}
function revisionEdit(){
 const d=revisionDraft;if(!d||d.project_id!==projectId)throw Error('项目已切换，请重新打开应用');
 form('修改应用，保存为新版本','原版本和它的测试、发布记录都会保留。这里只编辑工作配置。',field('应用名称',input('name','',d.name))+field('新版本号',input('version','例如：1.1',d.version))+field('目标机器人',select('robot_id',rows('robots').map(x=>[x.id,x.name]),d.robot_id))+`<div id="revision-steps"></div>${button('添加工作步骤','revision-add','secondary small')}<p class="inline-note">调整步骤顺序、技能、参数或执行时间后，先查看变更，再保存。新版本需要重新检查和现场测试。</p>`,'revision-preview','下一步：查看变更');
 d.steps.forEach(revisionStep);
}
function revisionValue(value){if(value===null)return '<p>无此步骤</p>';if(typeof value!=='object')return `<p class="output-reference">${h(value)}</p>`;return `<p>${h(value.skill)} · ${h(value.version)} · 最长 ${h(value.timeout_s)} 秒</p><details><summary>工作参数</summary>${json(value.parameters)}</details>`;}
async function revisionAction(type,id,el){
 if(type==='revision-edit'){
  const a=find('applications',id);if(!a||a.project_id!==projectId)throw Error('请在应用所属项目中打开');
  revisionDraft={project_id:projectId,source_application_id:id,name:a.name,version:'',robot_id:a.robot_id,steps:a.steps.map(s=>({skill_id:s.skill_id,parameters:structuredClone(s.parameters),timeout_s:s.timeout_s}))};return revisionEdit();
 }
 if(type==='revision-back')return revisionEdit();
 if(type==='revision-add')return revisionStep();
 const step=el?.closest('.revision-step');if(!step)return;
 if(type==='revision-remove')step.remove();
 if(type==='revision-up'&&step.previousElementSibling)step.previousElementSibling.before(step);
 if(type==='revision-down'&&step.nextElementSibling)step.nextElementSibling.after(step);
 revisionNumbers();
}
async function revisionSubmit(kind,p,f){
 if(!revisionDraft||revisionDraft.project_id!==projectId)throw Error('项目已切换，请重新打开应用');
 if(kind==='revision-preview'){
  revisionDraft={...revisionDraft,name:p.name,version:p.version,robot_id:p.robot_id,steps:[...f.querySelectorAll('.revision-step')].map((s,i)=>{let parameters;try{parameters=skillParamsCollect(s);}catch(x){throw Error('工作步骤 '+(i+1)+'：'+x.message);}return {skill_id:$('[name=skill_id]',s).value,parameters,timeout_s:Number($('[name=timeout_s]',s).value)};})};
  const plan=await api('applications/revision-preview',revisionDraft);revisionDraft.preview_digest=plan.preview_digest;
  return form('确认应用变更','保存新版本不会修改原应用，也不会向机器人发送请求。',`<div class="revision-comparison">${plan.changes.map(c=>`<section class="contract-line"><b>${h(c.label)}</b>${c.description?`<p>${h(c.description)}</p>`:''}<div class="purpose-grid"><div><small>原版本</small>${revisionValue(c.before)}</div><div><small>新版本</small>${revisionValue(c.after)}</div></div></section>`).join('')}</div>`+notice('新版本从配置检查开始，不能复用旧版本的现场通过报告。')+button('返回修改','revision-back','secondary small'),'revision-save','确认并保存新版本');
 }
 if(kind==='revision-save'){
  const a=await api('applications/revise',revisionDraft);await refresh(true);sessionStorage.setItem('journey-app:'+projectId,a.id);render();return deliveryOpen(a.id);
 }
}
