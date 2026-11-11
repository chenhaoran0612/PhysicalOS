'use strict';
const deliveryNext={prepare:['先补齐准备条件','下方列出了当前缺项。修复后重新检查。'],check:['先检查应用配置','检查设备能力、模型依赖和步骤设置，不会让机器人动作。'],onsite:['下一步：现场测试','配置已就绪，确认现场条件后验证机器人实际表现。'],publish:['下一步：发布到机器人','当前版本已有通过的现场报告。发布完成后，再单独启动工作。'],start:['应用已发布，可以准备启动','确认现场条件后发出启动请求；以设备反馈确认实际状态。'],reconcile:['先核对设备端状态','有请求尚未确认结果。请检查设备端记录，不要重复发送。'],
 testing:['等待现场测试结果','测试结束前不能发布或启动；结果返回后会自动显示在这里。']};
async function deliveryRead(id){const requestedProject=projectId;const d=await api('applications/readiness',{project_id:requestedProject,application_id:id});if(projectId!==requestedProject)throw Error('项目已切换，请重新打开交付清单');return d;}
function deliveryIdentity(d){return `<div class="detail-meta"><div><b>机器人</b>${h(d.robot.name)}${d.robot.simulated?' '+simBadge():''}</div><div><b>应用版本</b>${h(d.application.name)} · ${h(d.application.version)}</div></div>`;}
function deliveryStages(d){return `<div class="delivery-stages">${[['配置准备',d.prepared],['应用检查',d.prepared&&d.static_report?.passed],['现场测试',d.prepared&&d.onsite_report?.passed&&!d.unresolved_onsite],['发布版本',d.can_start]].map(([name,done],i)=>`<div class="${done?'done':''}"><span>${done?'✓':i+1}</span><b>${name}</b>${d.robot.simulated&&i>=2?'<small class="needs-real">需要真机</small>':''}</div>`).join('')}</div>`;}
async function deliveryOpen(id){
 const d=await deliveryRead(id),a=d.application;let [title,explanation]=deliveryNext[d.next_step];const failed=d.next_step==='onsite'&&d.onsite_report?.passed===false;if(failed){title='现场测试未通过，先查看问题';explanation='查看本次证据并修复问题后，再重新测试。当前不能发布或启动此应用。';}if(d.robot.simulated&&d.next_step==='onsite'){title='仿真本体：测试结果不能作为现场证据';explanation='这是一台仿真机器人。在它上面可以运行技能、实时观看，也可以做一次仿真测试来提前发现问题；但仿真结果不能证明真机能安全工作，所以不会计为现场测试，也不能发布。要真正部署：接入一台真实机器人，把应用的目标改为它（“修改为新版本”），在真机上通过现场测试后再发布。';}if(d.next_step==='prepare')explanation='需要处理：'+d.checks.filter(c=>!c.passed).map(c=>c.name).join('、')+'。';
 if(d.next_step==='start'&&d.start_blocked){title='急停已锁存，暂时不能启动';explanation='应用已发布，但设备急停处于锁存状态。请先在现场确认安全并“解除急停”，之后才能准备启动工作。';}
 let primary={check:button('检查应用配置','deliver-check','',id),onsite:button('准备现场测试','deliver-onsite','',id),publish:button('查看发布内容','deliver-publish','',id),start:button('准备启动工作','deliver-start','',id),prepare:button('重新检查配置','deliver-check','',id),reconcile:button('刷新交付状态','deliver-open','secondary',id)}[d.next_step];if(failed)primary=button('查看失败报告','deliver-report','',d.onsite_report.id)+button('修复后重新测试','deliver-onsite','secondary',id);
 // 第 01 轮 P3：急停锁存时“准备启动工作”不可用，并说明原因。
 if(d.next_step==='start'&&d.start_blocked)primary=button('准备启动工作','deliver-start','',id,true)+'<span class="inline-note">需先解除急停</span>';
 if(d.next_step==='testing')primary='';
 // ux02: on a simulated body the useful next steps are "run it in simulation" and "connect a real robot";
 // a simulation test stays available but is labelled for what it is.
 if(d.robot.simulated&&d.next_step==='onsite'&&!failed)primary=(find('robots',d.robot.id)?.sim?button('在仿真机器人上运行技能','sim-run-pick','',d.robot.id):'')+button('做一次仿真测试（不计为现场证据）','deliver-onsite','secondary',id)+button('接入真机','go-robots','secondary');
 const links=['journey-connect','go-agents','go-agents','go-models','go-skills'];
 dialog(h(a.name),'应用交付清单',`<div class="dialog-body delivery-detail">${deliveryIdentity(d)}${deliveryStages(d)}${deliverySimulationSection(d)}${deliveryTestingSection(d)}${deliveryEstopSection(d)}<section class="plan-review"><h3>${title}</h3><p>${explanation}</p><div class="button-row">${primary}${button('修改为新版本','revision-edit','secondary',id)}${d.can_stop?button('请求停止','operate-stop','secondary',d.release.id):''}</div></section>${revisionHistory(a)}<h3>机器人要做什么</h3>${a.steps.map((s,i)=>`<div class="contract-line"><b>${i+1}. ${h(named('skills',s.skill_id))}</b><p>完成条件：${h(s.success_condition)}</p><p>停止条件：${h(s.stop_condition)} · 最长 ${h(s.timeout_s)} 秒</p><details><summary>查看步骤参数与依赖</summary><p>设备能力：${h(s.requires.join('、'))}</p><p>模型：${h(Object.keys(s.model_digests).map(m=>named('models',m)).join('、')||'不依赖模型')}</p>${json(s.parameters)}</details></div>`).join('')}<h3>当前配置检查</h3><p class="inline-note">根据平台保存的连接和资产记录判断；现场测试、发布和启动前仍会核对设备。</p>${d.checks.map((c,i)=>`<div class="delivery-check"><div>${badge(c.passed?'已具备':'待处理',c.passed?'good':'warn')}<b>${h(c.name)}</b><p>${h(c.detail)}</p></div>${!c.passed?button(['连接机器人','补齐设备能力','检查技能版本','查看模型','检查工作步骤'][i],links[i],'secondary small',i===0?d.robot.id:''):''}</div>`).join('')}<div class="button-row">${button('保存一次配置检查记录','deliver-check','secondary small',id)}${d.can_test&&d.next_step!=='onsite'?button('重新进行现场测试','deliver-onsite','secondary small',id):''}</div><h3>当前版本的验证与发布</h3>${d.static_report?deliveryReportLine(d.static_report,'应用检查'):notice('尚无当前版本的应用检查记录。')}${d.onsite_report?deliveryReportLine(d.onsite_report,'现场测试'):notice('尚无与当前应用和机器人版本匹配的现场测试报告。')}${d.unresolved_onsite?notice(h(d.unresolved_onsite.error||'现场测试请求尚未确认，请核对设备端记录。'),true):''}${d.release?`<div class="contract-line"><b>发布记录 · ${h(stateNames[d.release.status]||d.release.status)}</b><p>${date(d.release.created_at)}</p>${d.release.error?`<p>${h(d.release.error)}</p>`:''}<p>${d.can_start?'当前版本与现场报告一致。':'此发布记录当前不能作为启动依据，请按上方提示处理。'}</p></div>`:''}${d.operations.length?'<h3>最近的设备请求</h3>'+d.operations.map(o=>`<div class="contract-line"><b>${h(operationNames[o.kind]||o.kind)}${o.simulated?' '+simBadge():''} · ${h(o.kind==='onsite'&&o.report_id&&find('evaluations',o.report_id)?.passed===false?'测试未通过':stateNames[o.status]||o.status)}</b><p>${date(o.created_at)}${o.error?' · '+h(o.error):''}</p>${o.kind==='onsite'&&o.report_id?button(o.simulated?'查看仿真结论':'查看测试结论','deliver-report','secondary small',o.report_id):''}${o.response?`<details><summary>设备返回内容</summary>${json(o.response)}</details>`:''}</div>`).join(''):''}<p class="inline-note">发布成功不表示机器人已经工作；请求受理也不表示任务完成。</p><details class="page-purpose"><summary>完整应用记录</summary>${json(a)}</details></div>`);
 $('#dialog').dataset.deliveryApp=id;
}
function deliveryReportLine(r,label){return `<div class="contract-line"><b>${label} · ${r.passed?'通过':'未通过'}</b><p>${date(r.created_at)}</p>${button('查看报告','deliver-report','secondary small',r.id)}</div>`;}
async function deliveryAction(type,id){
 if(!state.user)return auth();if(!project())return action('project-add');
 if(type==='deliver-open')return deliveryOpen(id);
 if(type==='deliver-create'){
  if(!rows('robots').length||!rows('skills').length)return dialog('先准备机器人和工作技能','应用用于安排已实现的设备能力，不会自动生成设备程序。',`<div class="dialog-body">${!rows('robots').length?notice('还没有目标机器人。先连接设备，让平台了解它的能力。')+button('连接机器人','journey-connect'):notice('还没有可执行技能。让工程助手开发接入程序与测试，完成设备端安装后登记技能。')+button('让助手准备','journey-prepare')+button('登记已有技能','skill-add','secondary')}</div>`);
  form('安排机器人的工作步骤','选择目标机器人，按顺序添加已有技能。保存应用不会执行机器人动作。',field('应用名称',input('name','',project().name))+field('版本',input('version','','1.0'))+picker('robots','robot_id','目标机器人')+(rows('robots').some(isSimRobot)?notice('<b>仿真</b> 目标选仿真机器人时，这个应用只能做仿真测试，不能发布到设备；要部署，请选择真机。'):'')+`<div id="steps-container"></div>${button('添加工作步骤','step-add','secondary small')}`,'application','保存并查看交付清单');return action('step-add');
 }
 if(type==='deliver-pick')return dialog('选择要继续交付的应用','检查、现场测试、发布与运行会始终对应所选应用版本。',`<div class="dialog-body">${rows('applications').map(a=>{const sim=isSimRobot(find('robots',a.robot_id));return `<div class="contract-line"><b>${h(a.name)} · ${h(a.version)}</b><p>${h(named('robots',a.robot_id))}${sim?' '+simBadge()+' 只能做仿真测试，不能发布到设备':''}</p>${button('查看交付清单','deliver-open','small',a.id)}</div>`;}).join('')||empty('还没有机器人应用','先安排机器人需要完成的工作步骤。',button('创建应用','deliver-create'))}</div>`);
 if(type==='deliver-report'){
  const r=find('evaluations',id);if(!r||r.project_id!==projectId)throw Error('报告不在当前项目中');
  return dialog(isSimReport(r)?'仿真测试报告':r.scope==='onsite'?'现场测试报告':'应用配置检查',h(named('applications',r.application_id)),`<div class="dialog-body delivery-detail">${badge(r.passed?'通过':'未通过',r.passed?'good':'warn')}${isSimReport(r)?' '+simBadge():''}<p class="subtitle">${isSimReport(r)?h(SIMULATION_NOTE)+'。':r.scope==='onsite'?'结果由设备端返回，绑定本次应用与机器人版本。':'这是配置检查，不是现场执行结果。'}</p>${isSimReport(r)&&r.simulation_reasons?r.simulation_reasons.map(x=>`<div class="contract-line"><b>仿真依据</b><p>${h(x)}</p></div>`).join(''):''}${(r.cases||[]).map(c=>`<div class="contract-line"><b>${c.passed?'✓':'待处理'} · ${h(c.name)}</b><p>${h(c.detail)}</p></div>`).join('')}${r.evidence_ref?`<div class="contract-line"><b>${isSimReport(r)?'仿真证据（非现场）':'现场证据'}</b><p class="output-reference">${h(r.evidence_ref)}</p></div>`:''}${button('返回应用交付清单','deliver-open','',r.application_id)}<details class="page-purpose"><summary>完整报告与设备记录</summary>${json(r)}</details></div>`);
 }
 if(type==='deliver-check'){await api('harness',{application_id:id});await refresh(true);return deliveryOpen(id);}
 const d=await deliveryRead(id);
 if(type==='deliver-onsite'){
  if(!d.can_test)throw Error('先补齐配置或核对未确认的现场请求');
  return form('确认现场测试','设备可能实际运动。检查现场条件后，再发送本次测试请求。',deliveryIdentity(d)+notice('将测试这份应用的 '+d.application.steps.length+' 个工作步骤。结果会返回到同一份交付清单。')+hardwareConfirm(),'deliver-onsite','确认并发起现场测试',`data-id="${id}" data-project="${h(projectId)}"`);
 }
 if(type==='deliver-publish'){
  if(!d.can_publish)throw Error('当前版本尚未具备发布条件，请查看交付清单');
  return form('确认发布应用','将把指定应用版本发送到这台机器人。发布不会启动工作。',deliveryIdentity(d)+deliveryReportLine(d.onsite_report,'作为依据的现场报告'),'deliver-publish','确认发布此版本',`data-id="${id}" data-report="${d.onsite_report.id}" data-project="${h(projectId)}"`);
 }
 if(type==='deliver-start'){
  if(!d.can_start)throw Error('当前应用尚未发布或验证记录已变化，请查看交付清单');
  if(d.start_blocked)throw Error('设备急停已锁存；请在现场确认安全后“解除急停”，再启动工作');
  return form('确认启动工作','向目标机器人发送此发布版本的启动请求。',deliveryIdentity(d)+hardwareConfirm(),'deliver-start','确认启动此版本',`data-id="${id}" data-release="${d.release.id}" data-project="${h(projectId)}"`);
 }
}
async function deliverySubmit(kind,p,f){
 if(f.dataset.project!==projectId)throw Error('项目已切换，请重新打开交付清单');
 const id=f.dataset.id;
 if(kind==='deliver-onsite')await api('onsite',{application_id:id,hardware_authorized:f.elements.hardware_authorized.checked});
 if(kind==='deliver-publish')await api('deployments',{application_id:id,report_id:f.dataset.report});
 if(kind==='deliver-start')await api('operations',{release_id:f.dataset.release,action:'start',hardware_authorized:f.elements.hardware_authorized.checked});
 await refresh(true);return deliveryOpen(id);
}

// 第 01 轮修复：仿真标注（R-08）与解除急停（R-07）。
const SIMULATION_NOTE = '仿真本体的测试结果不能作为现场证据；请在真实本体上进行现场测试';
const operationNames = {onsite: '现场测试', start: '启动工作', stop: '请求停止', reset: '解除急停', skill_install: '安装技能包', skill_run: '运行技能'};

// Deployment cards: a latched e-stop disables “启动工作” (round 01 P3).
function deploymentLatched(d) {
 const robot = find('robots', d.robot_id);
 return robot ? robotEstop(robot).latched : false;
}

function simBadge() {
 return badge('仿真', 'blue sim');
}

function isSimReport(r) {
 return Boolean(r && (r.scope === 'simulation' || r.simulated));
}

function reportScopeName(r) {
 if (isSimReport(r)) return '仿真测试（不能作为现场证据）';
 return r.scope === 'onsite' ? '设备端现场测试' : '静态工程检查';
}

// Mirrors brainos/delivery.estop_state for the robots page (the state snapshot has no readiness).
function robotEstop(r) {
 const last = state.operations.find(o => o.robot_id === r.id &&
  (['stop', 'start'].includes(o.kind) || (o.kind === 'reset' && o.status === 'accepted')));
 if (r.estop_latched === true) return {latched: true, reason: r.estop_reason || '设备报告急停已锁存'};
 const checkedAfter = r.estop_latched === false && last && (r.estop_checked_at || '') > last.created_at;
 if (last && last.kind === 'stop' && !checkedAfter) {
  return {latched: true, reason: '最近一次操作是停止；设备可能已锁存急停，需现场确认后解除'};
 }
 return {latched: false};
}

function newRequestId() {
 if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
 return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function resetConfirm() {
 return '<label class="hardware-confirm"><input type="checkbox" name="hardware_authorized" required>' +
  '<span>我在现场，已确认急停原因已排除、人员与设备处于安全状态，授权解除急停锁存。解除后机器人可以再次接受运动指令。</span></label>';
}

function deliverySimulationSection(d) {
 if (!d.simulation_notice) return '';
 const report = d.simulation_report;
 const line = report
  ? `<div class="contract-line"><b>仿真测试 · ${report.passed ? '通过' : '未通过'} ${simBadge()}</b>` +
    `<p>${date(report.created_at)} · 不能作为现场证据</p>${button('查看仿真报告', 'deliver-report', 'secondary small', report.id)}</div>`
  : '';
 return notice('<b>仿真</b> ' + h(d.simulation_notice) + (d.robot.simulated
   ? '。当前目标机器人是仿真本体，发布与启动不可用——这是有意的安全设计，不是故障。'
   : '。'), true) + line;
}

function deliveryEstopSection(d) {
 if (!d.estop || !d.estop.latched) return '';
 return `<section class="plan-review estop-latched"><h3>急停已锁存</h3><p>${h(d.estop.reason)}</p>` +
  `<div class="button-row">${button('解除急停', 'robot-reset', '', d.robot.id + ':' + d.application.id, !d.can_reset)}</div>` +
  `<p class="inline-note">解除前请在现场确认安全；“请求停止”始终可用。</p></section>`;
}

function robotResetForm(id) {
 const [robotId, appId, back] = String(id).split(':');
 const robot = find('robots', robotId);
 if (!robot) throw Error('找不到该机器人');
 // ux05-fix2 (Q7b-05/06): how long this robot's reset may take, as agreed with its runtime.
 const rp = robot.reset_policy || {};
 const wait = typeof rp.timeout_s === 'number'
  ? `<p class="inline-note" data-reset-wait data-expire-s="${h(rp.expire_s)}">桌面最多等待 ${h(rp.timeout_s)} 秒。` +
    (rp.reset_expire === true ? `复位超过 ${h(rp.expire_s)} 秒才完成时，机器人会把它作废、急停保持锁存。` : '这台机器人的运行时不会作废超时的复位：复位一旦完成，急停就会解除。') + '</p>'
  : '';
 return form('解除急停', h(robot.name) + ' · 将向设备发送解除急停锁存请求',
  notice('只有在现场确认急停原因已排除后才能解除。请求结果不明时会标记为待核对，不会自动重试。', true) + wait + resetConfirm(),
  'robot-reset', '确认解除急停',
  `data-id="${h(robotId)}" data-app="${h(appId || '')}" data-back="${h(back || '')}" data-request="${h(newRequestId())}" data-project="${h(projectId)}"`);
}

async function robotResetSubmit(p, f) {
 if (f.dataset.project !== projectId) throw Error('项目已切换，请重新打开');
 const result = await api('robots/reset', {
  robot_id: f.dataset.id,
  hardware_authorized: f.elements.hardware_authorized.checked,
  request_id: f.dataset.request,
 });
 await refresh(true);
 if (result.status !== 'accepted') {
  // ux04 (Q5-03): the real reason (e.g. 409 reset_in_progress) and the way out, never a bare toast.
  f.dataset.request = newRequestId();
  return uxResetOutcome(result, f.dataset.id, f.dataset.back);
 }
 toast('设备已确认解除急停');
 if (f.dataset.app) return deliveryOpen(f.dataset.app);
 if (f.dataset.back === 'live') return simLiveOpen(f.dataset.id);
 $('#dialog').close();
}
