'use strict';
const planUnit=u=>({minute:'分钟',hour:'小时',gpu_hour:'GPU 小时',episode:'轮', '1k_tokens':'千 Tokens'}[u]||u);
const planKinds={training:'训练',simulation:'仿真',evaluation:'测试',agent:'工程'};
const planAssets={datasets:['训练与测试数据','datasets'],models:['模型','models'],scenes:['仿真场景','simulation'],applications:['机器人应用','skills'],robots:['机器人','robots']};
let planDraft=null,planPreview=null,planLocked=false;
function planProviders(kind){return [...state.catalog.filter(x=>x.kind===kind&&x.status==='available').map(x=>({...x,source:'platform',key:'platform:'+x.id})),...rows('services').filter(x=>x.kind===kind&&!x.managed_resource_id&&x.status==='connected').map(x=>({...x,source:'own',key:'own:'+x.id}))];}
function planProvider(key){const [source,id]=key.split(':');const x=source==='platform'?state.catalog.find(x=>x.id===id):rows('services').find(x=>x.id===id);if(!x)throw Error('此服务不在当前项目中，请重新选择');return {...x,source,key};}
function planRecipes(p){return p.manifest?.job_recipes||[];}
function planRecipe(){const provider=planProvider(planDraft.provider);const recipe=planRecipes(provider).find(x=>x.id===planDraft.recipe_id);if(!recipe)throw Error('任务方案已变化，请重新选择服务');return {provider,recipe};}
function planField(f){
 const value=planDraft.values?.[f.key]??f.default??(f.prefill==='project_objective'?project().objective:''),name='plan_'+f.key,required=f.required?' required':'',desc=h(f.description||'');let control;
 if(f.type==='asset'){const options=rows(f.asset_kind);control=`<select name="${name}"${required}><option value="">请选择${h(f.label)}</option>${options.map(x=>`<option value="${h(x.id)}" ${x.id===value?'selected':''}>${h(x.name)}${x.version?' · '+h(x.version):''}</option>`).join('')}</select>`;}
 else if(f.type==='choice')control=`<select name="${name}"${required}><option value="">请选择</option>${f.options.map(x=>`<option value="${h(x.value)}" ${x.value===value?'selected':''}>${h(x.label)}</option>`).join('')}</select>`;
 else if(f.type==='boolean')control=`<select name="${name}"${required}><option value="">请选择</option><option value="true" ${value===true?'selected':''}>是</option><option value="false" ${value===false?'selected':''}>否</option></select>`;
 else if(f.type==='text')control=`<textarea name="${name}" maxlength="${f.max_length||2000}"${required}>${h(value)}</textarea>`;
 else control=`<input name="${name}" type="number" step="${f.type==='integer'?'1':'any'}" ${f.min!==undefined?'min="'+f.min+'"':''} ${f.max!==undefined?'max="'+f.max+'"':''} value="${h(value)}"${required}>`;
 return field(h(f.label)+(f.required?'':'（可选）'),control,desc);
}
function planEdit(){
 if(!planDraft||planDraft.project_id!==projectId)throw Error('任务项目已切换，请重新配置');const {provider,recipe}=planRecipe();
 const missing=recipe.fields.filter(f=>f.type==='asset'&&f.required&&!rows(f.asset_kind).length);
 const fields=field('任务名称',input('name','',planDraft.name))+recipe.fields.filter(f=>!f.advanced).map(planField).join('');
 const advanced=recipe.fields.filter(f=>f.advanced);
 form('配置'+planKinds[provider.kind]+'任务','2 / 3 · '+h(recipe.name)+' · '+h(provider.name),
 (missing.length?notice('还需要准备：'+missing.map(f=>h(f.label)).join('、')+'。先添加资产，再返回这里选择。',true)+`<div class="button-row">${[...new Set(missing.map(f=>f.asset_kind))].map(k=>button('准备'+planAssets[k][0],'go-'+planAssets[k][1],'secondary small')).join('')}</div><div class="spacer"></div>`:'')+fields+
 (advanced.length?`<details class="page-purpose"><summary>更多参数（已有默认设置）</summary>${advanced.map(planField).join('')}</details>`:'')+
 field('最长运行时间（分钟）',`<input name="max_minutes" type="number" min="1" max="43200" value="${planDraft.max_minutes||60}" required>`)+
 (provider.source==='platform'?field('用量上限（'+h(planUnit(provider.unit))+'）',`<input name="max_units" type="number" min="1" max="1000000" value="${planDraft.max_units||1}" required>`,h(provider.credits_per_unit)+' 点 / '+h(planUnit(provider.unit))+'；下一步查看总预留额度。'):notice('使用自有服务，费用和资源限制由该服务提供方管理。'))+
 `<p class="inline-note">预期得到：${h((recipe.outputs||[]).join('、')||'由服务返回执行结果')}。这是服务声明的产物，不代表任务已经完成。</p>${button('重新选择服务','plan-choose','secondary small',provider.kind)}`,'plan-preview','下一步：确认内容与预算');
 if(missing.length)$('#dialog [type=submit]').disabled=true;
}
async function planAction(type,id){
 if(!state.user)return auth();if(!project())return action('project-add');
 if(type==='plan-return')return planEdit();
 if(type==='plan-job')return planJobDetail(id);
 if(type==='plan-choose'){
  const providers=planProviders(id),offline=rows('services').filter(s=>s.kind===id&&!s.managed_resource_id&&s.status!=='connected');
  return dialog('新建'+planKinds[id]+'任务','1 / 3 · 选择在哪里执行，再选择它支持的方案。',`<div class="dialog-body">${providers.map(p=>`<div class="contract-line"><b>${h(p.name)} · ${p.source==='platform'?'平台服务':'自有服务'}</b><p>${h(p.description||'使用已连接的项目服务')}</p><p>${p.source==='platform'?h(p.credits_per_unit)+' 点 / '+h(planUnit(p.unit)):'由自有服务提供方计费'} · ${planRecipes(p).length?'支持可视化配置':'需高级配置'}</p>${button('选择此服务','plan-provider','small',p.key)}</div>`).join('')}${!providers.length?empty('还没有可用的'+planKinds[id]+'服务','平台服务开通后可以直接选择，也可以连接你已有的执行环境。'):''}${offline.map(s=>`<div class="contract-line"><b>${h(s.name)}</b><p>尚未通过连接检查，暂不能使用。</p>${button('前往检查连接','go-services','secondary small')}</div>`).join('')}<div class="button-row">${button('查看平台资源','resource-'+id,'secondary small')}${button('接入自有服务','service-'+id,'secondary small')}${planDraft?.project_id===projectId?button('返回上次配置','plan-return','secondary small'):''}</div></div>`);
 }
 if(type==='plan-provider'){
  const provider=planProvider(id),recipes=planRecipes(provider);
  if(!recipes.length)return dialog('此服务暂未提供简化配置',h(provider.name),`<div class="dialog-body"><p class="subtitle">服务需要先提供它支持的任务方案和参数，才能通过选择数据、模型或场景完成配置。你可以选用其他服务，或由工程人员使用高级配置。</p><div class="button-row">${button('选择其他服务','plan-choose','',provider.kind)}${button('高级配置','plan-advanced','secondary',id)}</div></div>`);
  return dialog('选择'+planKinds[provider.kind]+'方案','1 / 3 · '+h(provider.name),`<div class="dialog-body">${recipes.map(r=>`<div class="contract-line"><b>${h(r.name)}</b><p>${h(r.description)}</p><p>预期得到：${h((r.outputs||[]).join('、')||'执行结果')}</p>${button('使用此方案','plan-recipe','small',id+':'+r.id)}</div>`).join('')}</div>`);
 }
 if(type==='plan-recipe'){
  const parts=id.split(':'),key=parts.slice(0,2).join(':'),recipe_id=parts.slice(2).join(':');const provider=planProvider(key);
  planDraft={project_id:projectId,provider:key,recipe_id,name:project().name+' · '+planKinds[provider.kind],values:{},max_minutes:60,max_units:1};planPreview=null;planLocked=false;return planEdit();
 }
 if(type==='plan-advanced'){
  const provider=planProvider(id),managed=provider.source==='platform';return form('高级'+planKinds[provider.kind]+'配置','此入口供了解执行服务配置的工程人员使用。',field('任务名称',input('name','',project().name+' · '+planKinds[provider.kind]))+
  (!managed?`<input type="hidden" name="service_id" value="${h(provider.id)}">`:'')+field('服务要求的配置 JSON',textarea('config','','{\n  "objective": ""\n}'))+
  (managed?field('用量上限（'+h(planUnit(provider.unit))+'）','<input name="max_units" type="number" min="1" max="1000000" value="1" required>',provider.credits_per_unit+' 点 / '+h(planUnit(provider.unit))):'')+
  field('最长运行分钟','<input name="max_minutes" type="number" min="1" max="43200" value="60" required>'),managed?'managed-job':'job',managed?'预留额度并保存草稿':'保存草稿',`data-id="${provider.id}" data-kind="${provider.kind}"`);
 }
}
async function planSubmit(kind,p,f){
 if(!planDraft||planDraft.project_id!==projectId)throw Error('任务项目已切换，请重新配置');
 if(kind==='plan-preview'){
  const {provider,recipe}=planRecipe(),values={};
  for(const field of recipe.fields){const value=f.elements['plan_'+field.key].value;if(value==='')continue;values[field.key]=['integer','number'].includes(field.type)?Number(value):field.type==='boolean'?value==='true':value;}
  planDraft={...planDraft,name:p.name,values,max_minutes:Number(p.max_minutes),max_units:Number(p.max_units||1)};
  const payload={project_id:projectId,name:p.name,recipe_id:recipe.id,values,max_minutes:planDraft.max_minutes,...(provider.source==='platform'?{resource_id:provider.id,max_units:planDraft.max_units}:{service_id:provider.id})};
  planPreview=await api('jobs/preview',payload);planDraft.payload={...payload,preview_digest:planPreview.preview_digest};planLocked=false;
  const c=planPreview.cost;
  return form('确认'+planKinds[provider.kind]+'任务','3 / 3 · 确认后保存任务并发送到所选执行服务。',`<div class="plan-review"><h3>${h(planPreview.name)}</h3><p>${h(provider.name)} · ${h(recipe.name)} ${h(recipe.version)}</p>${planPreview.input_summary.map(x=>`<div class="check-row"><b>${h(x.label)}</b><span>${h(x.value)}</span></div>`).join('')}<div class="check-row"><b>最长运行</b><span>${planPreview.max_minutes} 分钟</span></div></div>`+
  (c?notice(`最多预留 <b>${c.reserved_credits} 点</b>（${c.max_units} ${h(planUnit(c.unit))} × ${c.price} 点）。按执行服务的实际用量结算；未提交的草稿取消后释放预留。`):notice('平台不为自有服务扣减额度；该服务可能单独收费。'))+
  `<details class="page-purpose"><summary>查看发送给服务的配置</summary>${json(planPreview.config)}</details>`+button('返回修改','plan-return','secondary small'),'plan-start','确认并开始'+planKinds[provider.kind]);
 }
 if(kind==='plan-start'){
  if(planLocked||!planDraft.payload)throw Error('此计划正在提交或结果待核对，请查看任务记录后再操作');planLocked=true;let job;
  try{job=await api(planDraft.payload.resource_id?'managed/jobs':'jobs',planDraft.payload);}
  catch(error){if(error.code&&error.code<500)planLocked=false;else{await refresh(true);error.message+='；提交结果不明，请关闭此窗口并查看任务记录，不要重复创建。';}throw error;}
  let submitError='';try{await api('jobs/submit',{job_id:job.id});}catch(error){submitError=error.message;}
  await refresh(true);$('#dialog').close();planJobDetail(job.id);if(submitError)toast('任务已保存，但尚未提交：'+submitError);
 }
}
function planJobDetail(id){const job=find('jobs',id);if(!job)return;const advice={draft:'任务已保存，尚未交给服务执行。可以提交或取消。',submitted:'服务已接收请求。刷新进度以查看实际执行状态。',queued:'任务正在排队，可以刷新进度或申请取消。',running:'服务报告任务正在执行。',succeeded:'服务报告执行完成。请检查返回结果，并验证它是否满足任务目标。',failed:'服务报告任务失败。查看日志后调整配置，不会自动重新执行。',unknown:'请求结果待核对，请先到执行服务确认是否已接收，避免重复执行。',cancelled:'任务已取消。',cancelling:'已发出取消请求，等待执行服务确认。'};
 const outputs=Object.entries(job.outputs||{}),reservation=state.billing?.reservations?.find(x=>x.id===job.reservation_id);
 dialog(h(job.name),'任务进度与结果',`<div class="dialog-body">${status(job.status)}<p class="subtitle">${h(advice[job.status]||'请查看服务返回的记录。')}</p>${job.error?notice(h(job.error),true):''}<div class="detail-meta"><div><b>执行服务</b>${h(job.provider_name||named('services',job.service_id))}</div><div><b>最长运行</b>${job.max_minutes||'—'} 分钟</div></div>${reservation?notice(reservation.status==='held'?'当前预留 '+reservation.amount+' 点；尚未完成费用结算。':'已结算 '+(reservation.actual_units*reservation.price)+' 点（'+reservation.actual_units+' '+h(planUnit(reservation.unit))+'）'+'，未使用的预留已释放。'):''}<div class="button-row">${job.status==='draft'?button('提交执行','job-submit','small',id):''}${job.remote_id?button('刷新进度','job-sync','secondary small',id):''}${['draft','submitted','queued','running'].includes(job.status)?button('取消任务','job-cancel','secondary small',id):''}</div><div class="spacer"></div><h3>返回的结果</h3>${outputs.length?outputs.map(([k,v])=>`<div class="contract-line"><b>${h({checkpoint:'模型检查点',metrics:'评测指标',report:'测试报告',report_ref:'报告地址',artifacts:'产物',findings:'检查结果',report_ids:'报告记录'}[k]||k)}</b>${typeof v==='object'?planOutput(v):`<p class="output-reference">${h(v)}</p>`}</div>`).join(''):empty('尚无返回结果','任务完成后，模型、报告或其他产物引用会显示在这里。')}${resultNext(job)}<h3>执行日志</h3>${(job.logs||[]).map(x=>`<div class="log-item"><p>${h(typeof x==='string'?x:x.message||JSON.stringify(x))}</p></div>`).join('')||'<p class="subtitle">暂无日志</p>'}<details class="page-purpose"><summary>参数与技术记录</summary>${json(job)}</details>${button('导出记录','export','secondary small','jobs:'+id)}</div>`);
}

function planOutput(value){if(value&& !Array.isArray(value)&&Object.values(value).every(v=>v===null||typeof v!=='object'))return Object.entries(value).map(([k,v])=>'<div class="check-row"><span>'+h(k)+'</span><b>'+h(v===true?'是':v===false?'否':v)+'</b></div>').join('');return '<details><summary>查看详细结果</summary>'+json(value)+'</details>';}
