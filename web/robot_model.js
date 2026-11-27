'use strict';
// Issue #48: a robot has a brand and model; applications and skills belong to one brand and model
// (brainos/robot_model.py holds the rules and refuses what does not fit). This file
//   * asks for the brand and model where a robot is added, shows them on its card, and lets a person
//     fill them in for a robot whose device reports none;
//   * draws "机器人应用" by brand and model: a filter, and the brand and model on every application and skill;
//   * keeps the forms to one brand and model: the skills offered for an application are those of the
//     chosen robot's brand and model, a registered skill names the brand and model it is for;
//   * lets the delivery checklist of an application choose among the robots of its brand and model;
//   * asks which brand and model a skill is published to the cloud for, and shows it on every release.
// It wraps form / engineRobotCard / engineAction / engineSubmit / action / api / views.skills and a few
// functions of delivery.js and cloud_skills.js instead of editing them, as body_types.js does.

const RM_NONE = '未填写品牌型号';
const RM_MAX = 100;
const RM_ACTIONS = ['engine-robot-model'];
const RM_FORM = 'engine-robot-model';
const RM_CARD_TAGS = '<div class="tags">';
const RM_CARD_BOTTOM = '<div class="card-bottom"><div class="button-row">';
const RM_FAMILY_FIELD = /<label class="field">本体形态<select name="family" required>[\s\S]*?<\/select><\/label>/;
// Requests about an application that go to the robot chosen in its delivery checklist.
const RM_APP_ROUTES = ['applications/readiness', 'harness', 'onsite', 'deployments'];
const RM_PUBLISH_ROUTES = ['cloud-skills/publish', 'cloud-skills/publish-examples'];
const RM_ALL = '', RM_UNSET = '-';

let rmFilter = RM_ALL;                    // the brand and model "机器人应用" shows; '' for all
const rmDeliveryRobot = {};               // application id -> the robot chosen in its checklist
let rmOpenDelivery = '';                  // the application whose checklist is open (its forms included), '' when none is
let rmLastDelivery = null;                // what the checklist last read
let rmExamplesProduct = null;             // the brand and model the bundled examples are being published for

// ---------------------------------------------------------------- pure
function rmProduct(record) {
  const p = record && record.product;
  return p && typeof p.vendor === 'string' && typeof p.model === 'string' && p.vendor && p.model ? {vendor: p.vendor, model: p.model} : null;
}
// Issue #54: another spelling of a known brand and model is the same one (spCanon, web/sim_profiles.js), as robot_model.key says.
const rmCanon = product => (typeof spCanon === 'function' ? spCanon(product) : product);
const rmKey = product => (product ? (rmCanon(product).vendor + '\n' + rmCanon(product).model).toLowerCase() : RM_UNSET);
const rmLabel = product => (product ? product.vendor + ' ' + product.model : RM_NONE);
const rmSame = (a, b) => Boolean(a && b) && rmKey(a) === rmKey(b);

// The brands and models of a list of robots, each once, in the order the robots were added, with their robots.
function rmProducts(robots) {
  const seen = new Map();
  [...robots].sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''))).forEach(robot => {
    const product = rmProduct(robot);
    if (!product) return;
    const key = rmKey(product);
    if (!seen.has(key)) seen.set(key, {key, product, label: rmLabel(product), robots: []});
    seen.get(key).robots.push(robot);
  });
  return [...seen.values()];
}

// Whether an application can be used with a robot (the rule of RobotModels.fits).
function rmFits(application, robot) {
  const product = rmProduct(application);
  return product ? rmSame(product, rmProduct(robot)) : robot.id === application.robot_id;
}

// Whether a skill can go into an application for this robot: its own brand and model, or none yet.
function rmSkillFits(skill, robot) {
  const product = rmProduct(skill);
  return !product || rmSame(product, rmProduct(robot));
}

// Whether a record is shown under the chosen filter.
function rmShown(record, filter) {
  return filter === RM_ALL || rmKey(rmProduct(record)) === filter;
}

// ---------------------------------------------------------------- small pieces of markup
const rmTag = (product, cls = '') => `<span class="tag robot-model-tag ${product ? '' : 'robot-model-missing'} ${cls}">${h(rmLabel(product))}</span>`;

function rmInputs(product, required) {
  const req = required ? 'required' : '';
  const known = rmProducts(state.robots || []);
  const list = (id, values) => `<datalist id="${id}">${[...new Set(values)].map(v => `<option value="${h(v)}"></option>`).join('')}</datalist>`;
  return '<div class="robot-model-inputs">' +
    field('品牌', `<input name="vendor" maxlength="${RM_MAX}" placeholder="例如：示例机器人公司" value="${h(product?.vendor || '')}" list="rm-vendors" autocomplete="off" ${req}>`) +
    field('型号', `<input name="model" maxlength="${RM_MAX}" placeholder="例如：X1" value="${h(product?.model || '')}" list="rm-models" autocomplete="off" ${req}>`) +
    list('rm-vendors', known.map(k => k.product.vendor)) + list('rm-models', known.map(k => k.product.model)) + '</div>';
}

// The brand and model something is for (a skill, a release in the cloud): two inputs that suggest what
// the account's robots have, filled in when they all are of one brand and model.
function rmProductField(note) {
  const known = rmProducts(state.robots || []);
  return rmInputs(known.length === 1 ? known[0].product : null, true) + `<p class="inline-note robot-model-note">${note}</p>`;
}

function rmTyped(root) {
  const vendor = (root?.querySelector('[name="vendor"]')?.value || '').trim(), model = (root?.querySelector('[name="model"]')?.value || '').trim();
  return vendor && model ? {vendor, model} : null;
}

// ---------------------------------------------------------------- adding a robot, and its card
// The fields of a form that adds a robot, with the brand and model after the body type.
function rmFormFields(fields, kind) {
  if (typeof fields !== 'string' || !RM_FAMILY_FIELD.test(fields)) return fields;
  if (kind === 'robot') {
    return fields.replace(RM_FAMILY_FIELD, found => found + rmInputs(null, false) +
      '<p class="inline-note robot-model-note">设备自己上报了品牌和型号时可以留空（要先填好下面的服务地址）；设备没有上报时必须填写。应用和技能按品牌型号归属，不同品牌型号之间不通用。</p>');
  }
  if (kind === 'engine-ssh') {
    return fields.replace(RM_FAMILY_FIELD, found => found + rmInputs(null, true) +
      '<p class="inline-note robot-model-note">应用和技能按品牌型号归属，不同品牌型号之间不通用。</p>');
  }
  return fields;
}

// The skill form names the brand and model the skill is for; the application forms show it on every robot.
function rmSkillFormFields(fields, kind) {
  if (typeof fields !== 'string') return fields;
  if (kind === 'skill') {
    return rmProductField('这个技能是给哪个品牌型号的机器人用的：它只能编排进同一品牌型号的应用。') + fields;
  }
  if (kind === 'application' || kind === 'revision-preview') {
    return fields.replace(/<option value="([^"]*)"\s*(selected)?\s*>([^<]*)<\/option>/g, (all, id, selected, text) => {
      const robot = (state.robots || []).find(r => h(r.id) === id);
      return robot ? `<option value="${id}" ${selected || ''}>${text}（${h(rmLabel(rmProduct(robot)))}）</option>` : all;
    });
  }
  return fields;
}

// The card of a robot: the brand and model next to its body type (a robot without one is flagged, in
// sight), and for a robot whose device reports none a way to fill them in.
const RM_CARD_LINE = /<p>(?:(?!<\/p>|<br>)[\s\S])*<br>/;
function rmCard(html, robot) {
  const product = rmProduct(robot);
  let out = html;
  if (product && RM_CARD_LINE.test(out)) {
    out = out.replace(RM_CARD_LINE, line => line.slice(0, -4) + ` · <span class="robot-model-name">${h(rmLabel(product))}</span><br>`);
  } else if (!product && out.includes(RM_CARD_TAGS)) {
    out = out.replace(RM_CARD_TAGS, () => RM_CARD_TAGS + `<span class="status warn robot-model-flag"><i class="dot"></i>${RM_NONE}</span>`);
  }
  // What the device reports is not typed over; a simulated robot always reports.
  if (robot.product?.source !== 'device' && !robot.sim && out.includes(RM_CARD_BOTTOM)) {
    out = out.replace(RM_CARD_BOTTOM, () => RM_CARD_BOTTOM +
      `<button type="button" class="button secondary small" data-action="engine-robot-model" data-id="${h(robot.id)}" ` +
      `aria-label="${product ? '修改' : '填写'}品牌型号：${h(robot.name)}">${product ? '修改品牌型号' : '填写品牌型号'}</button>`);
  }
  return out;
}

function rmAssignForm(robot) {
  const product = rmProduct(robot);
  const mine = (state.applications || []).filter(a => rmProduct(a) && rmSame(rmProduct(a), product)).length;
  const notes = product
    ? notice(`这台机器人现在是「${h(rmLabel(product))}」。改成另一个品牌型号以后，属于「${h(rmLabel(product))}」的 ${mine} 个应用不能再用在它上面。`, true)
    : notice('这台设备没有上报品牌和型号。填写以后，为它建的应用和技能就属于这个品牌型号，同品牌型号的其他机器人也能使用。');
  return ['品牌型号', h(robot.name), notes + rmInputs(product, true) +
    '<p class="inline-note">之后做连接检查时，设备如果自己上报了品牌和型号，会以设备上报的为准。</p>', RM_FORM, '保存', `data-id="${h(robot.id)}"`];
}

async function rmAction(type, id) {
  const robot = find('robots', id);
  if (!robot) return toast('这台机器人已经不在了');
  return form(...rmAssignForm(robot));
}

async function rmSubmit(p, f) {
  await api('robots/model', {project_id: projectId, robot_id: f.dataset.id, vendor: p.vendor, model: p.model});
  await refresh(true);
  $('#dialog').close();
  return toast('品牌型号已保存');
}

// ---------------------------------------------------------------- 机器人应用, by brand and model
function rmFilterBar() {
  const apps = rows('applications'), skills = rows('skills');
  const groups = rmProducts(state.robots || []);
  // A brand and model that only applications or skills still name (its robots are gone) is listed too.
  [...apps, ...skills].forEach(x => {
    const product = rmProduct(x);
    if (product && !groups.some(g => g.key === rmKey(product))) groups.push({key: rmKey(product), product, label: rmLabel(product), robots: []});
  });
  const unset = [...apps, ...skills].some(x => !rmProduct(x));
  if (rmFilter !== RM_ALL && rmFilter !== RM_UNSET && !groups.some(g => g.key === rmFilter)) rmFilter = RM_ALL;
  if (rmFilter === RM_UNSET && !unset) rmFilter = RM_ALL;
  const count = key => apps.filter(a => rmShown(a, key)).length;
  const chip = (key, text, sub) => `<button type="button" class="robot-model-chip ${rmFilter === key ? 'active' : ''}" data-action="rm-filter" ` +
    `data-id="${h(key)}" aria-pressed="${rmFilter === key}"><b>${h(text)}</b><small>${h(sub)}</small></button>`;
  if (!groups.length && !unset) return '';
  return '<div class="robot-model-filter" role="group" aria-label="按品牌型号查看">' + chip(RM_ALL, '全部品牌型号', `${apps.length} 个应用`) +
    groups.map(g => chip(g.key, g.label, `${g.robots.length} 台机器人 · ${count(g.key)} 个应用`)).join('') +
    (unset ? chip(RM_UNSET, RM_NONE, `${count(RM_UNSET)} 个应用`) : '') + '</div>';
}

function rmApplicationCard(a) {
  const usable = (state.robots || []).filter(r => rmFits(a, r));
  const where = rmProduct(a) ? `${usable.length} 台同品牌型号的机器人可用` : `只用于：${h(named('robots', a.robot_id))}`;
  return `<article class="card"><div class="card-body"><div class="card-top"><span class="agent-avatar">${icon('skills')}</span>${badge('v' + a.version)}</div>` +
    `<h3>${h(a.name)}</h3><p>${rmTag(rmProduct(a))}<br>${where}<br>${a.steps.length} 个技能步骤</p>` +
    `<div class="tags">${a.steps.slice(0, 3).map(s => `<span class="tag">${h(named('skills', s.skill_id))}</span>`).join('')}</div></div>` +
    `<div class="card-bottom"><small class="mono">${a.digest.slice(0, 12)}</small><div class="button-row">` +
    `${button('交付清单', 'detail-applications', 'secondary small', a.id)}${button('检查', 'harness-run', 'small', a.id)}</div></div></article>`;
}

function rmAppsPage() {
  const bar = rmFilterBar();                 // settles the filter first
  const apps = rows('applications').filter(a => rmShown(a, rmFilter)), all = rows('applications').length;
  const skills = rows('skills').filter(s => rmShown(s, rmFilter)), allSkills = rows('skills').length;
  const appsBody = apps.length ? `<div class="cards">${apps.map(rmApplicationCard).join('')}</div>`
    : `<section class="panel">${all ? empty('这个品牌型号还没有应用', '应用属于一个品牌型号：为这种机器人创建的应用，只在这里出现。', button('创建应用', 'application-add', 'secondary small'))
      : empty('创建机器人要执行的工作流程', '选择目标本体，按顺序组合技能，设置参数与超时。', button('创建应用', 'application-add', 'secondary small'))}</section>`;
  const skillsBody = skills.length ? table(['技能名称', '品牌型号', '执行入口', '能力要求', '模型', ''], skills.map(s =>
      `<tr><td><span class="cell-main">${h(s.name)}</span><span class="cell-sub">v${h(s.version)}</span></td><td>${rmProduct(s) ? rmTag(rmProduct(s)) : '<span class="cell-sub">第一次编排进应用时确定</span>'}</td>` +
      `<td class="mono">${h(s.entrypoint)}</td><td>${s.requires.map(c => `<span class="tag">${h(c)}</span>`).join(' ')}</td><td>${s.model_ids.length}</td>` +
      `<td>${button('详情', 'detail-skills', 'secondary small', s.id)}</td></tr>`))
    : allSkills ? empty('这个品牌型号还没有技能', '技能属于一个品牌型号，只能编排进同一品牌型号的应用。', button('使用机器人上已安装的技能', 'ux-adopt', 'small') + ' ' + button('手动登记技能', 'skill-add', 'secondary small'))
      : empty('先把技能加入项目', '机器人上已经安装的技能（例如示例技能“巡逻”）可以直接加入；也可以手动登记设备端已有的技能。', button('使用机器人上已安装的技能', 'ux-adopt', 'small') + ' ' + button('手动登记技能', 'skill-add', 'secondary small'));
  return heading('SKILLS & APPLICATIONS', '把能力组合成工作。', '注册可复用技能，再编排成应用版本。应用和技能属于一个品牌型号的机器人，不同品牌型号之间互相隔离。',
      button('注册技能', 'skill-add', 'secondary') + button(icon('plus') + ' 创建应用', 'application-add')) + noProject() + bar +
    `<div class="section-head"><h2>机器人应用</h2><span>版本固定 · 能力匹配 · 失败停止</span></div>${appsBody}<div class="spacer"></div>` +
    `<div class="section-head"><h2>技能资产</h2><span>代码 / 模型 / 厂商能力均可作为技能实现</span></div><section class="panel">${skillsBody}</section>`;
}

// ---------------------------------------------------------------- forms: one brand and model at a time
// In a form that puts an application together: only the skills of the chosen robot's brand and model can be picked.
function rmLimitSteps(root = $('#dialog')) {
  const formEl = root?.querySelector?.('form[data-form="application"], form[data-form="revision-preview"]');
  const robot = formEl ? find('robots', formEl.elements.robot_id?.value) : null;
  if (!formEl || !robot) return;
  formEl.querySelectorAll('.step-editor select[name="skill_id"]').forEach(selectEl => {
    [...selectEl.options].forEach(option => {
      const skill = find('skills', option.value);
      const fits = !skill || rmSkillFits(skill, robot);
      option.disabled = !fits;
      option.hidden = !fits;
    });
    if (selectEl.selectedOptions[0]?.disabled) {
      const first = [...selectEl.options].find(o => !o.disabled);
      selectEl.value = first ? first.value : '';
      selectEl.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
  let note = formEl.querySelector('.robot-model-step-note');
  if (!note) {
    note = document.createElement('p');
    note.className = 'inline-note robot-model-step-note';
    formEl.querySelector('#steps-container, #revision-steps')?.before(note);
  }
  const product = rmProduct(robot);
  note.textContent = product ? `这个应用将属于「${rmLabel(product)}」：只能选这个品牌型号的技能，同品牌型号的每一台机器人都能使用它。`
    : `「${robot.name}」还没有填写品牌型号：这个应用只能用在这一台机器人上。在“我的机器人”里填写品牌型号后，同品牌型号的机器人就都能使用。`;
}

// ---------------------------------------------------------------- the delivery checklist: which robot
// Only while the checklist of that application is open: a "检查" on its card goes to the robot it was made with.
function rmDeliveryChoice(applicationId) {
  const application = find('applications', applicationId), robot = find('robots', rmDeliveryRobot[applicationId]);
  return rmOpenDelivery === applicationId && application && robot && rmFits(application, robot) ? robot.id : '';
}

function rmDeliveryDecorate(id) {
  const d = rmLastDelivery, meta = $('#dialog .delivery-detail .detail-meta');
  if (!d || d.application?.id !== id || !meta || $('#dialog .robot-model-delivery')) return;
  const product = rmProduct(d.application), robots = Array.isArray(d.robots) ? d.robots : [];
  const choose = robots.length > 1
    ? `<label class="robot-model-pick">用哪台机器人<select data-rm-delivery="${h(id)}" aria-label="用哪台机器人">${robots.map(r =>
        `<option value="${h(r.id)}" ${r.id === d.robot.id ? 'selected' : ''}>${h(r.name)}${r.simulated ? '（仿真）' : ''}</option>`).join('')}</select></label>` : '';
  const line = document.createElement('div');
  line.className = 'robot-model-delivery';
  line.innerHTML = `<span><b>品牌型号</b> ${rmTag(product)}</span><span class="inline-note">${product
    ? (robots.length > 1 ? `同品牌型号的 ${robots.length} 台机器人都能使用这个应用；检查、现场测试和发布按所选的这一台进行，证据各算各的。` : '同品牌型号的机器人都能使用这个应用；现在只有这一台。')
    : '这台机器人没有填写品牌型号，应用只能用在它上面。'}</span>${choose}`;
  meta.after(line);
}

// ---------------------------------------------------------------- what is sent
function rmPublishProduct() {
  return rmExamplesProduct || rmTyped($('#dialog form[data-cloudskill-form="publish"]'));
}

function rmPayload(path, payload) {
  if (!payload || typeof payload !== 'object') return payload;
  if (RM_APP_ROUTES.includes(path) && payload.application_id && !payload.robot_id) {
    const robotId = rmDeliveryChoice(payload.application_id);
    return robotId ? {...payload, robot_id: robotId} : payload;
  }
  if (RM_PUBLISH_ROUTES.includes(path) && !payload.vendor) {
    const product = rmPublishProduct();
    return product ? {...payload, ...product} : payload;
  }
  return payload;
}

// ---------------------------------------------------------------- cloud skills
function rmCloudCard(html, row) {
  const product = rmProduct(row.latest);
  return html.replace(RM_CARD_TAGS, () => RM_CARD_TAGS + rmTag(product, 'robot-model-release'));
}

function rmCloudReleaseRow(html, release, loading) {
  const product = rmProduct(release), key = release.skill_id + '|' + release.version;
  const bind = !product && !loading ? ' ' + button('指定品牌型号', 'rm-release-bind', 'secondary small', key) : '';
  const line = `<p class="robot-model-release-line">${product ? '适用品牌型号：' + rmTag(product)
    : '<span class="robot-model-missing">还没有说明给哪个品牌型号用，指定之后才能安装到机器人。</span>' + bind}</p>`;
  return html.replace(/<\/div>$/, () => line + '</div>');
}

function rmPublishDecorate() {
  const formEl = $('#dialog form[data-cloudskill-form="publish"]');
  if (!formEl || formEl.querySelector('[name="vendor"]')) return;
  const holder = document.createElement('div');
  holder.innerHTML = rmProductField('这个版本只能安装到这个品牌型号的机器人上。给另一种机器人用，请换一个版本号再发布。');
  formEl.querySelector('.dialog-body')?.append(...holder.childNodes);
}

function rmAskProduct(title, sub, kind, attrs, submit) {
  return form(title, sub, rmProductField('只能安装到这个品牌型号的机器人上。'), kind, submit, attrs);
}

async function rmCloudSubmit(kind, p, f) {
  const product = rmTyped(f);
  if (!product) throw Error('请填写品牌和型号');
  if (kind === 'engine-rm-release-bind') {
    const [skillId, version] = String(f.dataset.id).split('|');
    await api('cloud-skills/bind', {skill_id: skillId, version, ...product});
    $('#dialog').close();
    toast('已指定品牌型号：' + rmLabel(product));
    return cloudSkillsLoad();
  }
  rmExamplesProduct = product;                // kind === 'engine-rm-examples'
  try {
    return await rmBaseExamples();
  } finally {
    rmExamplesProduct = null;
  }
}

let rmBaseExamples = null;
{
  const baseCard = engineRobotCard, baseForm = form, baseEngineAction = engineAction, baseEngineSubmit = engineSubmit;
  const baseAction = action, baseApi = api;
  engineRobotCard = r => rmCard(baseCard(r), r);
  form = (title, sub, fields, kind, ...rest) => {
    const out = baseForm(title, sub, rmSkillFormFields(rmFormFields(fields, kind), kind), kind, ...rest);
    rmLimitSteps();
    return out;
  };
  engineAction = async (type, id) => (RM_ACTIONS.includes(type) && state.user ? rmAction(type, id) : baseEngineAction(type, id));
  engineSubmit = async (kind, p, f) => {
    if (kind === RM_FORM) return rmSubmit(p, f);
    if (kind === 'engine-rm-release-bind' || kind === 'engine-rm-examples') return rmCloudSubmit(kind, p, f);
    return baseEngineSubmit(kind, p, f);
  };
  action = async (type, id, el) => {
    if (type === 'rm-filter') { rmFilter = id || RM_ALL; return render(); }
    if (type === 'rm-release-bind') return rmAskProduct('指定品牌型号', h(String(id).replace('|', '@')), 'engine-rm-release-bind', `data-id="${h(id)}"`, '保存');
    const out = await baseAction(type, id, el);
    if (type === 'step-add' || type === 'revision-add' || type === 'revision-edit' || type === 'revision-back') rmLimitSteps();
    return out;
  };
  api = (path, payload) => baseApi(path, rmPayload(path, payload));
  views.skills = rmAppsPage;
  if (typeof deliveryRead === 'function') {
    const baseRead = deliveryRead, baseOpen = deliveryOpen;
    deliveryRead = async id => { rmLastDelivery = await baseRead(id); return rmLastDelivery; };
    deliveryOpen = async id => { rmOpenDelivery = id; const out = await baseOpen(id); rmDeliveryDecorate(id); return out; };
  }
  if (typeof cloudSkillCard === 'function') {
    const baseCloudCard = cloudSkillCard, baseRow = cloudSkillReleaseRow, baseShow = cloudSkillPublishShow;
    rmBaseExamples = cloudSkillPublishExamples;
    cloudSkillCard = (row, ...rest) => rmCloudCard(baseCloudCard(row, ...rest), row);
    cloudSkillReleaseRow = (release, loading = false) => rmCloudReleaseRow(baseRow(release, loading), release, loading);
    cloudSkillPublishShow = r => { const out = baseShow(r); rmPublishDecorate(); return out; };
    cloudSkillPublishExamples = async () => rmAskProduct('发布应用自带的示例技能', '这些示例技能发布给哪个品牌型号的机器人用',
      'engine-rm-examples', '', '发布');
  }
}
document.querySelector('#dialog')?.addEventListener('close', () => { rmOpenDelivery = ''; });
document.addEventListener('change', e => {
  if (e.target.matches?.('form[data-form="application"] [name="robot_id"], form[data-form="revision-preview"] [name="robot_id"]')) rmLimitSteps();
  const applicationId = e.target.dataset?.rmDelivery;
  if (applicationId) {
    rmDeliveryRobot[applicationId] = e.target.value;
    deliveryOpen(applicationId).catch(err => (typeof showError === 'function' ? showError(err) : toast(err.message)));
  }
});
render();
