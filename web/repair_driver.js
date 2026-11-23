/* Issue #29 — the repair assistant installs and starts a missing motion driver.

   What a person sees in an approval of a repair session:
   * the lines the product runs in front of the command (the ROS 2 Humble environment, a workspace), right
     under the command, since they run too;
   * for the command that starts the motion driver: what the body may do, and the on-site tick. Approving
     without it is refused here and by the backend; with it, the confirmation is sent first
     (engineering/hardware/onsite), then the approval. This holds while the session runs without asking too;
   * the step that sends a driver package from the library: which file, its checksum and where it goes.
   Wraps the shared views (hardwareFixApproval, engineApproval, engineSubmit) instead of editing them. */

const RD_DELIVER = 'driver_package_deliver/requestApproval';
const RD_FIX = 'device_fix/requestApproval';
const RD_ONSITE = '我在机器人旁边，周围的人和障碍物已经清开，能随时按下硬件急停';     // driver_repair.ONSITE_TEXT
const RD_NOT_TICKED = '启动运控驱动前请先勾选“我在现场”';

RP_AUTO_METHODS.push(RD_DELIVER);       // sending a package follows the session's mode, like a repair command

function rdIsStart(s) {
  const a = s?.pending_approval;
  return a?.method === RD_FIX && a.request?.starts_motion_driver === true;
}

function rdSize(bytes) {
  if (!Number.isFinite(bytes)) return '未知';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function rdEnvironment(p) {
  const lines = Array.isArray(p?.environment) ? p.environment : [];
  if (!lines.length) return '';
  return `<div class="rd-env"><b>先加载 ROS 2 环境</b><span>执行时，产品会在上面的命令前面先运行这${lines.length === 1 ? '一' : '几'}行（由产品加上，助手改不了）：</span><pre class="json-view">${h(lines.join('\n'))}</pre></div>`;
}

function rdOnsite(s) {
  if (!rdIsStart(s)) return '';
  return `<div class="rd-start"><b>启动运控驱动前要有人在现场</b><p>驱动启动后本体可能自己上电、站立或收腿。只有在现场的人确认后才执行，开了“默认允许执行”也一样。整个修复不发运动指令，也不解除急停。</p>
  <label class="hardware-confirm rd-onsite"><input type="checkbox" name="onsite"><span><b>我在现场</b>：${h(RD_ONSITE)}</span></label></div>`;
}

function rdDeliverApproval(s) {
  const p = s.pending_approval.request || {}, scan = hwFind('hardware_scans', s.hardware?.scan_id);
  const titles = (p.issue_ids || []).map(id => scan?.issues.find(i => i.id === id)?.title || id);
  const labels = Object.values(p.labels || {}).filter(Boolean).map(h).join(' · ');
  const line = (label, value, cls = '') => `<div class="runtime-line"><span>${label}</span><b${cls ? ` class="${cls}"` : ''}>${value}</b></div>`;
  return `<div class="contract-line hw-fix rd-deliver"><div class="section-head"><b>把资料库里的驱动安装包送到机器人</b></div><p>${h(p.username)}@${h(p.host)}:${p.port}</p><p class="hw-fix-purpose">${h(p.purpose)}</p>
  <div class="runtime-list">${line('安装包', h(p.filename))}${labels ? line('标注', labels) : ''}${line('大小', rdSize(p.size))}${line('校验值 SHA-256', h(p.sha256), 'rd-break')}${line('放到设备上', h(p.device_target), 'rd-break')}${line('对应问题', titles.map(h).join('<br>'))}</div></div>`
    + notice('只送文件：在设备上核对校验值后放到上面的位置，不解压、不安装、不执行。文件名和标注是上传的人填的。安装是之后单独的一条修复命令，照常按你选的方式批准。', true);
}

{
  const baseFix = hardwareFixApproval;
  hardwareFixApproval = s => {
    const html = baseFix(s), env = rdEnvironment(s.pending_approval.request);
    // The environment lines run right before the command: show them right under it.
    const placed = env ? html.replace(/(<p class="hw-command-size">[\s\S]*?<\/p>)/, (m) => m + env) : html;
    return placed + rdOnsite(s);
  };
  const baseApproval = engineApproval;
  engineApproval = s => s.pending_approval?.method === RD_DELIVER ? rdDeliverApproval(s) + rpApprovalChoice(s) : baseApproval(s);
  const baseSubmit = engineSubmit;
  engineSubmit = async (kind, p, f) => {
    if (kind === 'engine-approval' && p.decision === 'accept') {
      const s = find('agent_sessions', f.dataset.id);
      if (rdIsStart(s)) {
        const box = f.elements.onsite;
        if (!box?.checked) {
          f.querySelector('.rd-onsite')?.classList.add('needs-confirm');
          throw Error(RD_NOT_TICKED);
        }
        // The approval id goes with its own type (the runtime numbers requests).
        await api('engineering/hardware/onsite', {project_id: projectId, session_id: s.id, approval_id: s.pending_approval.id, onsite: true});
      }
    }
    return baseSubmit(kind, p, f);
  };
}
