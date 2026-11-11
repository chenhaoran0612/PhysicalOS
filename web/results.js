'use strict';
let resultPlan=null;
function resultModelState(m){return m.status==='artifact'?badge('训练产物 · 待配置','warn'):m.package_ref?badge('端侧部署包','good'):status(find('services',m.service_id)?.status||'unconnected');}
function resultModelActions(m){return button('查看与使用','result-model','secondary small',m.id)+(m.status==='artifact'?button('配置调用','result-connect','small',m.id):'')+(m.package_ref?button('一键部署','model-deploy','small',m.id):'')+(m.service_id?button('调用测试','model-infer','small',m.id):'');}
function resultNext(job){
 if(job.status!=='succeeded')return '';
 if(job.kind==='training'){
  const saved=rows('models').find(m=>m.status==='artifact'&&m.provenance?.job_id===job.id&&m.model_ref===job.outputs?.checkpoint);
  return `<section class="plan-review"><h3>接下来：使用训练结果</h3><p class="subtitle">保存到模型库后，可以继续训练，或配置已加载该产物的推理服务。端侧部署还需要匹配设备运行时的模型包。</p>${saved?button('查看已保存的模型','result-model','',saved.id):typeof job.outputs?.checkpoint==='string'&&job.outputs.checkpoint.trim()?button('保存到模型库','result-save','',job.id):notice('服务尚未返回可登记的检查点地址。请联系服务提供方补充结果。')}</section>`;
 }
 if(job.kind==='simulation'||job.kind==='evaluation')return `<section class="plan-review"><h3>接下来：检查效果，再决定是否上机</h3><p class="subtitle">根据报告确认目标是否达到。需要改进时返回工程助手；准备上机时，先检查应用并完成现场测试。这里的结果不会自动成为现场验收证据。</p><div class="button-row">${button('检查应用与现场测试','go-harness','small')}${button('继续改进方案','go-agents','secondary small')}</div></section>`;
 return '';
}
async function resultAction(type,id){
 if(type==='result-save'){
  resultPlan=await api('jobs/model-preview',{job_id:id,project_id:projectId});
  if(resultPlan.saved_model_id)return resultAction('result-model',resultPlan.saved_model_id);
  return form('保存训练结果到模型库','保留来源、训练输入和指标，方便后续使用与追溯。',field('模型名称',input('name','',resultPlan.name.slice(0,100)))+field('模型用途',input('task_type','例如：仓库异常识别'))+`<div class="contract-line"><b>检查点地址</b><p class="output-reference">${h(resultPlan.checkpoint)}</p></div>`+notice('保存的是产物地址和训练记录，不会下载权重、启动服务或部署机器人。'),'result-save','保存到模型库',`data-id="${id}" data-proof="${h(resultPlan.preview_digest)}" data-project="${h(projectId)}"`);
 }
 const m=find('models',id);if(!m||m.project_id!==projectId)throw Error('请在模型所属项目中打开');
 if(type==='result-connect'){
  const services=rows('services').filter(s=>s.kind==='inference'&&s.status==='connected');
  if(!services.length)return dialog('先准备模型调用服务',h(m.name),`<div class="dialog-body"><p class="subtitle">需要一个已加载此训练产物的推理服务。连接检查通过后，回来填写该服务里的模型名称以及输入、输出要求。</p><div class="contract-line"><b>要加载的检查点</b><p class="output-reference">${h(m.model_ref)}</p></div>${button('接入模型调用服务','service-inference')}${button('查看已有服务','go-services','secondary')}</div>`);
  return form('配置模型调用',h(m.name)+' · 创建可调用版本，原训练记录会保留。',field('模型名称',input('name','',(m.name+' · 调用').slice(0,100)))+field('已加载此产物的推理服务',select('service_id',services.map(s=>[s.id,s.name])))+field('服务中的模型名称或版本',input('model_ref','填写推理服务实际识别的模型引用'))+field('输入要求',textarea('input_contract','字段、数据格式、单位与坐标要求'))+field('输出说明',textarea('output_contract','结果格式、动作空间与使用限制'))+`<label class="hardware-confirm"><input type="checkbox" name="loaded_confirm" required><span>我确认所选服务已加载此训练产物；保存后还需通过调用测试验证效果。</span></label>`,'result-connect','保存调用配置',`data-id="${h(id)}" data-project="${h(projectId)}"`);
 }
 if(type==='result-model'){
  const origin=m.provenance,children=rows('models').filter(x=>x.source_model_id===id);
  const explanation=m.status==='artifact'?'已保存训练产物。下一步可以配置调用，也可以在训练方案中选择它继续改进。':m.package_ref?'选择兼容机器人安装模型包，再验证应用效果。':'调用配置已登记。先测试输入与输出，再将模型用于机器人应用。';
  return dialog(h(m.name),'模型详情与下一步',`<div class="dialog-body model-detail">${resultModelState(m)}<p class="subtitle">${explanation}</p><div class="detail-meta"><div><b>用途</b>${h(m.task_type)}</div><div><b>模型引用</b><span class="output-reference">${h(m.model_ref)}</span></div></div><div class="button-row">${m.status==='artifact'?button('配置调用','result-connect','',id)+button('用于新训练','plan-choose','secondary','training'):''}${m.service_id?button('调用测试','model-infer','',id)+button('配置机器人应用','go-skills','secondary'):''}${m.package_ref?button('部署模型包','model-deploy','',id):''}</div>${children.length?'<h3>已配置的调用版本</h3>'+children.map(x=>`<div class="contract-line"><b>${h(x.name)}</b>${button('查看调用配置','result-model','secondary small',x.id)}</div>`).join(''):''}${origin?`<h3>来自哪次训练</h3><div class="contract-line"><b>${h(origin.job_name)}</b><p class="output-reference">${h(origin.checkpoint)}</p>${button('查看来源任务','plan-job','secondary small',origin.job_id)}</div>${(origin.input_summary||[]).map(x=>`<div class="check-row"><b>${h(x.label)}</b><span>${h(x.value)}</span></div>`).join('')}<h3>保存时的训练指标</h3>${planOutput(origin.metrics)}<p class="inline-note">这些是保存时的服务报告。地址内容未由平台下载校验，不能据此判定实机效果。</p>`:''}${m.input_contract?`<h3>输入要求</h3><p class="output-reference">${h(m.input_contract)}</p><h3>输出说明</h3><p class="output-reference">${h(m.output_contract)}</p>`:''}<details class="page-purpose"><summary>完整模型记录</summary>${json(m)}</details></div>`);
 }
}
async function resultSubmit(kind,p,f){
 if(f.dataset.project!==projectId)throw Error('项目已切换，请重新打开模型');
 let model;
 if(kind==='result-save')model=await api('jobs/save-model',{project_id:projectId,job_id:f.dataset.id,preview_digest:f.dataset.proof,name:p.name,task_type:p.task_type});
 if(kind==='result-connect'){
  const source=find('models',f.dataset.id);
  model=await api('models',{...p,source_model_id:source.id,task_type:source.task_type,loaded_confirm:f.elements.loaded_confirm.checked});
 }
 await refresh(true);location.hash='models';resultAction('result-model',model.id);
}
