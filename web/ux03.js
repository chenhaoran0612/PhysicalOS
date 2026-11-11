'use strict';
// ux03 (round 05): the ux02 audit leftovers.
//  1. Application steps (create and "修改为新版本") get the same skill.json-driven parameter form as the
//     run dialog, with the raw-JSON advanced toggle. A skill without declared parameters keeps a JSON box.
//  2. Advanced fields inside a collapsed <details> open themselves when the browser flags them as missing.
// Nothing here grants an execution right; the server and the device still validate every parameter.

// Parameter declarations of a project skill: saved with the skill (since ux03), else the bundled
// example with the same entrypoint (skills added before ux03), else none.
function uxStepDecls(skillId) {
  const s = find('skills', skillId);
  if (!s) return null;
  if (Array.isArray(s.parameters)) return s.parameters;
  const g = typeof simGalleryCache === 'object' ? simGalleryCache[s.entrypoint] : null;
  return g ? g.skill.parameters || [] : null;
}

function uxStepParamsHtml(skillId, values) {
  const decls = uxStepDecls(skillId);
  if (decls) return skillParamsForm(decls, values, {legend: '工作参数'});
  return `<label class="field">工作参数（JSON）<textarea name="parameters" class="mono param-json" spellcheck="false">${h(JSON.stringify(values || {}, null, 2))}</textarea>` +
    `<small>这个技能没有登记参数说明，请按技能的要求直接填写；没有参数时保持 {}。</small></label>`;
}

// Suggested values for a new step: the example gallery's preset for bundled skills (as in the run dialog).
function uxStepPreset(skillId) {
  const s = find('skills', skillId), g = s && simGalleryCache[s.entrypoint];
  return g && g.preset ? structuredClone(g.preset) : {};
}

let uxGalleryLoading = null;
function uxEnsureGallery() {
  if (Object.keys(simGalleryCache).length || !projectId) return Promise.resolve();
  uxGalleryLoading = uxGalleryLoading || api('skills/examples', {project_id: projectId})
    .then(g => g.items.filter(x => x.valid).forEach(x => { simGalleryCache[x.skill.id] = x; }))
    .catch(() => {});
  return uxGalleryLoading;
}

// values: the step's current parameters (editing a version), or undefined for a new step.
function uxRenderStepParams(step, values) {
  const holder = step.querySelector('.step-params');
  const select = step.querySelector('[name="skill_id"]');
  if (!holder || !select) return;
  const paint = () => {
    holder.innerHTML = uxStepParamsHtml(select.value, values === undefined ? uxStepPreset(select.value) : values);
    holder.dataset.skill = select.value;
    delete holder.dataset.touched;
  };
  paint();
  // The gallery (declarations of skills added before ux03, presets) loads once; repaint if untouched.
  if (!Object.keys(simGalleryCache).length) {
    uxEnsureGallery().then(() => {
      if (holder.isConnected && holder.dataset.skill === select.value && !holder.dataset.touched) paint();
    });
  }
}
for (const type of ['input', 'click']) {
  document.addEventListener(type, e => { const box = e.target.closest && e.target.closest('.step-params'); if (box) box.dataset.touched = '1'; }, true);
}

// Choosing another skill for a step: show that skill's parameters (its suggested values or defaults).
document.addEventListener('change', e => {
  const select = e.target;
  if (select.name !== 'skill_id') return;
  const step = select.closest('.step-editor');
  if (step && step.querySelector('.step-params')) uxRenderStepParams(step);
});

// A required field inside a collapsed "高级" block: open the block so the browser can show what is missing.
document.addEventListener('invalid', e => {
  for (let d = e.target.closest && e.target.closest('details'); d; d = d.parentElement?.closest('details')) d.open = true;
}, true);
