'use strict';
// ux04 (round 06, Q5-04): plain-language errors, classified by structure first.
//
//   1. The machine-readable `code` from the runtime / cloud / desktop body decides (table below).
//   2. Without a code, the HTTP status the device or cloud answered decides (`device_status`, else the
//      status of the desktop response when it is not the desktop's own 400).
//   3. Only then a few narrow text heuristics. They never turn numbers in a message ("x=500.0", "512 步")
//      into "服务内部出错", nor a parameter named timeout_s into "网络超时"; safety words come first.
//
// A safety refusal is never presented as a fault to retry: it says what was refused and what to change.
// Pure functions only (no DOM): tests/ux04_errors.cjs runs this file in a bare VM.

const UX_KIND_NAMES = {safety: '安全拒绝', param: '参数不符合要求', auth: '需要授权', state: '机器人当前状态不允许',
  sim: '仿真机器人的限制', network: '连接问题', service: '服务出错', account: '账户', notfound: '找不到', input: '请求不符合要求',
  skill: '技能包有误', pending: '还没有结果'};

const UX_RESET_RESTART = '如果一直这样：重启机器人上的运行时服务（急停锁存会保留），然后再确认现场安全、解除急停。仿真机器人可以直接点“重启仿真机器人”。';

// code → [kind, plain explanation, next steps...]
const UX_CODES = {
  // ---- safety refusals (runtime supervisor, latch, authorization) -- never "retry later"
  estop_latched: ['safety', '急停处于锁存状态，机器人不会执行任何动作。', '先在现场确认急停原因已排除、人员和设备安全', '然后点“解除急停”，再重新发起'],
  safety_rejected: ['safety', '机器人的安全监督器拒绝了这个动作（例如目标超出允许的工作区，或速度、步数超过上限）。这是安全保护，不是故障，原样重试也会被拒绝。', '展开“技术细节”看是哪一项超限', '修改参数，让动作落在允许范围内后再运行'],
  hardware_not_authorized: ['auth', '这个操作会让机器人运动，需要现场授权。', '确认机器人周围安全后，勾选“我确认……授权”再提交'],
  authorization_required: ['auth', '这个操作会让机器人运动（或可以再次运动），需要你在现场确认安全并授权。', '勾选表单下方的现场安全确认后再提交'],
  operator_required: ['auth', '这个操作需要平台运营人员的权限。', '请联系平台运营方'],
  // ux05 (Q6-11): the desktop's own record of a reset that was sent while the previous one still hangs. Not a
  // refusal (it may yet complete), so it must not say "被拒绝".
  // ux05-fix2 (Q7b-05): these base sentences hold for any runtime (a late reset may still release the latch);
  // uxClassify swaps in the "void after the limit" wording only when the robot advertises it (reset_expire).
  reset_pending: ['pending', '解除急停的请求已经发给机器人，但机器人上一次复位（例如起立）还在执行、一直没有完成，所以这次请求的结果要等它完成后才知道。注意：它一旦完成，急停可能自动解除，本体可能起立或动作——在此期间请保持远离。', '要取消：按“停止”（那次复位会失败，急停保持锁存）', UX_RESET_RESTART],
  reset_in_progress: ['safety', '机器人上一次“解除急停”（复位）还在执行，所以这次复位被拒绝了。注意：上一次复位一旦完成，急停可能自动解除，本体可能起立或动作——在此期间请保持远离。', '要取消那次复位：按“停止”（复位会失败，急停保持锁存）', UX_RESET_RESTART],
  conformance_running: ['state', '机器人正在做一致性测试。测试期间它拒绝其他来源的运动、技能运行和复位（停止始终可用）。', '等一致性测试结束后再试', '需要中断时，随时可以按“停止”'],
  stop_count_mismatch: ['safety', '解除急停期间机器人又被停止了一次，为了安全，急停继续保持锁存。', '重新确认现场安全', '再点一次“解除急停”'],
  stale_after_stop: ['safety', '这个请求是在“停止”之前发出的；停止之后它已经作废，不会执行。', '确认现场安全后重新发起'],
  joint_state_required: ['safety', '机械臂没有提供关节状态，运行时无法为关节动作限速，所以拒绝执行（安全设计）。', '在机器人接入程序（适配器）里提供 joint.state 后再试'],
  joint_names_required: ['safety', '机械臂没有声明关节名称和顺序，运行时无法对应每个关节来限速，所以拒绝执行（安全设计）。', '在适配器清单里按机械臂指令顺序声明 joint_names 后再试'],
  latched_motion: ['safety', '急停锁存后机器人仍在运动，运行时已再次发出停止。', '立即在现场确认机器人状态，必要时使用硬件急停'],
  runs_active: ['state', '机器人上还有技能在运行，这个操作需要等它结束。', '等当前技能结束，或先按“停止”'],
  busy: ['state', '机器人正忙（正在处理另一项操作）。', '稍等片刻再试'],
  in_progress: ['state', '同样的操作正在进行中。', '等它完成后查看结果，不要重复提交'],
  install_busy: ['state', '机器人正在安装另一个技能或模型。', '等安装完成后再试'],
  conformance_busy: ['state', '机器人已经在做一致性测试。', '等当前测试结束'],
  outcome_unknown: ['state', '这个请求之前发出过，但结果没有得到确认；为安全起见不会重复执行。', '先在“实时状态”或现场核对机器人状态', '需要时重新打开表单，用新的请求再发起'],
  manifest_changed: ['state', '机器人的能力清单变了（例如换了驱动或配置），之前的检查结果不再适用。', '点“连接检查”重新读取能力', '重新做应用检查'],
  // ---- parameters
  param_out_of_range: ['param', '有一个参数超出了技能声明的允许范围，机器人拒绝运行。', '按提示把这个参数改到允许范围内'],
  // ux05 (Q6-08): each parameter refusal says what is actually wrong
  param_type: ['param', '有一个参数的类型不对（例如应该填数字却填了文字，或整数填了小数），机器人拒绝运行。', '按字段旁的提示改成正确的类型'],
  param_choice: ['param', '有一个参数不是技能允许的选项之一，机器人拒绝运行。', '从列表里选一个允许的选项'],
  param_length: ['param', '有一个参数的长度不对（例如数字的个数或文字的长短不符合技能的要求），机器人拒绝运行。', '按字段旁的提示修改长度'],
  param_required: ['param', '有必填的参数还没有填写，机器人拒绝运行。', '补上必填参数'],
  param_declaration_invalid: ['skill', '技能包本身的参数声明有误（例如默认值不在它自己声明的范围内），这不是你填写的问题，改参数也解决不了。', '联系技能作者修正 skill.json 并发布新版本', '拿到新版本后重新安装这个技能'],
  param_unknown: ['param', '填写了这个技能不认识的参数。', '删除多出来的参数，或改用表单填写'],
  invalid_parameter: ['param', '有一个参数的值不符合要求。', '按提示修改参数'],
  parameter_not_allowed: ['param', '填写了这个技能不接受的参数。', '删除多出来的参数，或改用表单填写'],
  // ---- simulated robots (ADR-011)
  simulated_only: ['sim', '真机不能从这里直接运行技能：真机需要先把技能编排成“机器人应用”，通过现场测试后再发布和启动，这样每次运动都有据可查。', '去“机器人应用”创建应用'],
  simulated_evidence: ['sim', '这是一台仿真机器人。仿真里的结果只能帮你发现问题，不能证明真机能安全工作，所以不能当作现场测试证据，也不能发布到设备——这是有意的安全设计，不是故障。', '要真正部署，请先接入一台真实机器人', '在真机上通过现场测试后再发布'],
  sim_failed: ['service', '仿真机器人没有启动成功。', '到“我的机器人”里重新启动它；仍失败时把技术细节发给技术支持'],
  // ---- compatibility / installation
  incompatible: ['input', '技能或应用与这台机器人的能力不匹配，机器人拒绝执行。', '在机器人详情里查看它具备的能力', '换一台合适的机器人，或修改技能的要求'],
  capability_not_provided: ['input', '机器人没有提供这一步需要的能力。', '在机器人详情里查看它具备哪些能力'],
  unknown_capability: ['input', '用到了能力标准里没有的能力名称。', '检查技能或应用里的能力名称'],
  skill_not_installed: ['input', '这个技能还没有安装到这台机器人上。', '在“我的机器人 → 能力与技能”里先安装技能'],
  validation_failed: ['input', '机器人端的执行前检查没有通过，这次请求没有执行。', '展开“技术细节”查看是哪一项没通过'],
  invalid_skill: ['input', '技能包的内容不符合规范。', '检查 skill.json 后重新安装'],
  invalid_skill_id: ['input', '技能编号不符合规范。', '检查技能编号'],
  invalid_package: ['input', '技能包格式不对。', '检查技能文件夹里的 skill.json'],
  package_ref_refused: ['input', '机器人拒绝从这个地址获取安装包。', '检查安装包地址，或改用本机文件安装'],
  version_immutable: ['input', '这个版本已经存在且内容不同；同一版本号不能覆盖。', '改一个新的版本号再安装'],
  exists: ['input', '同名的内容已经存在。', '换一个名称，或先删除旧的'],
  invalid_level: ['input', '一致性测试的级别填写不对。', '选择列表里给出的级别'],
  invalid_manifest: ['input', '机器人的能力清单格式不对。', '检查机器人接入程序（适配器）的清单'],
  invalid_conformance_report: ['input', '一致性测试报告无效或被改动过。', '重新运行一致性测试'],
  conformance_outdated: ['input', '一致性测试报告已经过期（机器人或适配器有变化）。', '重新运行一致性测试'],
  conformance_robot_mismatch: ['input', '一致性测试报告不是这台机器人的。', '用这台机器人重新运行一致性测试'],
  conformance_manifest_mismatch: ['input', '一致性测试时的能力清单和现在不一致。', '重新运行一致性测试'],
  conformance_unknown: ['input', '找不到这次一致性测试。', '重新运行一致性测试'],
  conformance_unsupported: ['input', '这台机器人的运行时不支持一致性测试。', '升级机器人上的运行时'],
  evidence_required: ['input', '发布前需要真机现场测试的证据。', '先在真机上通过现场测试'],
  field_acceptance_refused: ['input', '现场验收没有通过，不能继续。', '查看验收报告里的未通过项'],
  device_run_refused: ['input', '设备拒绝运行这个请求。', '展开“技术细节”查看原因'],
  // ---- models on the robot
  model_not_found: ['notfound', '找不到这个模型。', '检查模型名称，或先安装模型'],
  model_ambiguous: ['input', '有多个同名模型，无法确定用哪一个。', '用完整的模型编号指定'],
  model_in_use: ['state', '这个模型正在被使用，暂时不能改动。', '先停止使用它的技能'],
  models_by_adapter: ['input', '这台机器人的模型由厂商驱动管理，不能从这里安装。', '按厂商说明安装模型'],
  model_exists: ['input', '同名模型已经存在。', '换一个名称或版本'],
  model_not_hosted: ['input', '这个模型没有在平台上托管。', '选择平台已托管的模型'],
  n_unsupported: ['input', '这个模型服务不支持一次生成多个结果。', '去掉 n 参数后重试'],
  // ---- request plumbing (desktop handles these; the user just reopens the form)
  request_id_required: ['input', '请求缺少编号（程序问题）。', '关闭对话框重新打开后再试'],
  request_id_reused: ['input', '这个请求编号已经用过，为避免重复执行被拒绝。', '关闭对话框重新打开后再试'],
  bad_request: ['input', '机器人拒绝了这个请求：请求内容不符合要求。', '展开“技术细节”查看原因'],
  invalid_request: ['input', '请求内容不符合要求。', '展开“技术细节”查看原因'],
  conflict: ['state', '机器人当前的状态不允许这个操作。', '展开“技术细节”查看原因'],
  error: ['input', '机器人拒绝了这个请求。', '展开“技术细节”查看原因'],
  payload_too_large: ['input', '提交的内容太大。', '减小内容后再试'],
  content_not_allowed: ['input', '内容不被允许。', '修改内容后再试'],
  secret_in_card: ['input', '内容里像是包含了密钥或密码，为安全起见被拒绝。', '去掉密钥后再提交'],
  reason_required: ['input', '这个操作需要填写原因。', '填写原因后再提交'],
  robot_required: ['input', '这个操作需要先选择机器人。', '选择一台机器人后再试'],
  // ---- not found / not supported
  not_found: ['notfound', '对方没有找到要操作的对象（可能已被删除）。', '刷新页面后再试'],
  evaluation_not_found: ['notfound', '找不到这次测试。', '刷新后查看测试记录'],
  release_not_found: ['notfound', '机器人上找不到这个发布版本。', '重新发布应用'],
  conformance_not_found: ['notfound', '找不到这次一致性测试。', '刷新后查看'],
  robot_not_found: ['notfound', '找不到这台机器人。', '检查机器人地址或重新添加'],
  runtime_not_found: ['notfound', '找不到机器人运行时。', '确认机器人上已安装并启动运行时'],
  adapter_not_found: ['notfound', '找不到这个适配器。', '检查适配器名称'],
  unknown_template: ['notfound', '找不到这个模板。', '选择列表里给出的模板'],
  unknown_provider: ['notfound', '不认识这个模型服务商。', '选择支持的服务商'],
  no_package: ['notfound', '没有可下载的安装包。', '联系提供方'],
  not_implemented: ['notfound', '机器人上的运行时还不支持这个功能。', '升级机器人上的运行时'],
  runtime_unsupported: ['notfound', '机器人上的运行时版本不支持这个操作。', '升级机器人上的运行时'],
  ros2_unavailable: ['notfound', '这台电脑或机器人上没有可用的 ROS 2 环境。', '安装并加载 ROS 2 后再试'],
  // ---- service / adapter faults (real faults: retry is reasonable)
  // ux05 (Q6-08): the desktop's own 500 -- the operation may or may not have reached the robot. Never "retry".
  desktop_internal_error: ['service', '本机 NeuxMind 后台处理这个操作时出错了，操作有没有执行无法确认。', '先在“实时状态”或现场核对机器人状态，再决定要不要重新发起', '如果一直出现，把技术细节发给技术支持'],
  payload_too_deep: ['input', '提交的内容嵌套层数太多，没有执行。', '简化参数后再提交'],
  internal_error: ['service', '对方服务内部出错了。可以稍后重试；如果一直出现，请把技术细节发给技术支持。', '稍后重试'],
  adapter_fault: ['service', '机器人驱动（适配器）出错，或返回了不符合规范的数据。', '在现场核对机器人状态', '把技术细节发给适配器的开发者'],
  adapter_load_failed: ['service', '适配器加载失败。', '检查适配器代码与依赖'],
  adapter_incomplete: ['input', '适配器还没有实现必需的功能。', '补全适配器后再试'],
  adapter_ambiguous: ['input', '找到多个可用的适配器，无法确定用哪一个。', '明确指定适配器'],
  install_failed: ['service', '安装没有成功。', '展开“技术细节”查看原因'],
  bad_response: ['service', '对方返回的数据格式不对。', '把技术细节发给技术支持'],
  upstream_stream_error: ['service', '模型服务的输出中断了。', '稍后重试'],
  email_send_failed: ['service', '邮件没有发送成功。', '稍后重试'],
  email_sender_not_configured: ['service', '平台还没有配置发信服务。', '联系平台运营方'],
  config_error: ['input', '配置文件有错误。', '检查配置后重试'],
  scaffold_invalid: ['input', '生成接入程序的输入不完整或有误。', '检查能力映射后重试'],
  unsafe_scaffold_input: ['safety', '生成接入程序的输入里有不安全的内容，已拒绝。', '检查输入内容'],
  port_in_use: ['state', '端口已经被占用。', '换一个端口，或关闭占用它的程序'],
  already_running: ['state', '已经在运行了。', '无需重复启动'],
  stream_consumed: ['input', '数据流已经被读取过（程序问题）。', '重新发起请求'],
  wait_timeout: ['state', '等待结果超时，但操作本身可能仍在进行。', '稍后查看结果，不要重复提交'],
  // ---- connectivity (desktop-side codes from brainos/connector.py)
  device_timeout: ['network', '机器人（接入服务）没有在时限内回应，这次操作的结果未知；结果未知的操作不会自动重试。', '先在“实时状态”或现场核对，再决定是否重试'],
  device_unreachable: ['network', '连不上机器人的接入服务：它可能没有启动，或者地址、端口不对。', '确认机器人上的运行时服务正在运行', '点“连接检查”'],
  connection_failed: ['network', '连接失败。', '确认对方服务正在运行、地址正确'],
  // ---- login / account / cloud
  unauthorized: ['account', '登录状态已失效，或访问令牌不对。', '重新登录，或检查访问令牌'],
  not_logged_in: ['account', '还没有登录云平台账户。', '先登录云平台账户'],
  access_token_unavailable: ['account', '拿不到访问令牌。', '重新登录云平台账户'],
  invalid_token: ['account', '访问令牌无效。', '检查或重新生成访问令牌'],
  invalid_api_key: ['account', 'API 密钥无效。', '检查密钥是否填写正确'],
  invalid_credentials: ['account', '邮箱或密码不对。', '重新输入，或找回密码'],
  invalid_session_token: ['account', '会话已失效。', '重新登录'],
  session_expired: ['account', '会话已过期。', '重新登录'],
  session_required: ['account', '需要先登录。', '登录后再试'],
  session_closed: ['state', '这次助手会话已经结束。', '新建一次会话'],
  session_busy: ['state', '这次会话正在处理上一条请求。', '稍等片刻再试'],
  session_overage_locked: ['account', '这次会话的用量超出预留，已被锁定。', '在“用量与账单”里处理超额后再继续'],
  overage_resolved: ['state', '超额已经处理过了。', '无需再次操作'],
  email_taken: ['account', '这个邮箱已经注册过。', '直接登录，或找回密码'],
  email_unverified: ['account', '邮箱还没有验证。', '打开验证邮件里的链接'],
  email_pending_verification: ['account', '邮箱正在等待验证。', '打开验证邮件里的链接'],
  email_pending_claim: ['account', '这个邮箱正在等待认领。', '按邮件提示完成认领'],
  already_verified: ['account', '邮箱已经验证过了。', '直接登录'],
  verification_token_invalid: ['account', '验证链接无效。', '重新发送验证邮件'],
  verification_token_expired: ['account', '验证链接已过期。', '重新发送验证邮件'],
  verification_wrong_account: ['account', '这个验证链接属于另一个账户。', '用对应的账户登录后再打开'],
  recovery_token_invalid: ['account', '找回密码的链接无效。', '重新申请找回密码'],
  recovery_token_expired: ['account', '找回密码的链接已过期。', '重新申请找回密码'],
  not_member: ['account', '你不是这个组织的成员。', '请管理员邀请你'],
  invalid_scope: ['account', '申请的权限范围无效。', '检查权限范围'],
  scope_missing: ['account', '缺少需要的权限。', '重新授权并勾选所需权限'],
  scope_escalation: ['account', '不能申请比原来更高的权限。', '使用原有权限范围'],
  tool_not_allowed: ['account', '这个工具没有被允许使用。', '在助手工具设置里允许它'],
  upstream_not_allowed: ['account', '不允许连接这个上游服务。', '选择平台允许的服务'],
  provider_mismatch: ['input', '模型服务商与配置不一致。', '检查模型配置'],
  pricing_not_session_compatible: ['input', '这个计费方式不支持会话使用。', '选择支持会话的方案'],
  entitlement_required: ['account', '还没有开通这项服务。', '在云平台开通后再试'],
  already_entitled: ['account', '已经开通过了。', '无需重复开通'],
  insufficient_credits: ['account', '余额或额度不足。', '在“用量与账单”里充值或申请额度'],
  order_not_payable: ['account', '这个订单当前不能支付。', '刷新订单状态'],
  amount_mismatch: ['account', '金额与订单不一致。', '刷新后重新确认金额'],
  already_settled: ['account', '已经结算过了。', '无需重复结算'],
  nothing_to_charge: ['account', '没有需要扣费的用量。', '无需操作'],
  reservation_not_held: ['account', '预留的额度已经释放。', '重新发起'],
  ledger_conflict: ['state', '账本记录冲突（同时有别的操作）。', '刷新后再试'],
};

// Codes that start like these are parameter refusals even when not listed (e.g. a new runtime code).
const uxIsParamCode = c => /^param(eter)?_/.test(c);
const UX_PARAM_GENERIC = ['param', '有一个参数不符合技能的要求，机器人拒绝运行。', '按提示修改参数'];

// ux05 (Q6-08): why a reset failed with estop_latched (runtime `cause`, INTERFACES §8).
const UX_RESET_CAUSES = {
  latch_guard_reset_grace: ['safety', '解除急停时，机器人起立/复位的动作超过了允许的时间仍在运动，锁存守护为安全起见再次停止了它，急停继续保持锁存。这不是你的操作有误。', '在现场确认机器人状态和周围安全，再点一次“解除急停”', '如果每次都这样：请适配器开发者检查复位动作，或调大锁存守护的复位宽限（reset_grace_s）'],
  // ux05-fix2: the robot voided a reset that returned after the limit the desktop sent (expire_s, INTERFACES §9.1).
  reset_expired: ['safety', '这次复位用的时间超过了时限，机器人已把它作废，急停继续保持锁存。这是为了不让一次没人等着的复位自己解除急停。', '在现场确认机器人状态和周围安全', '需要时再点一次“解除急停”', '如果每次都超时：请适配器开发者在机器人清单里声明复位时长（safety.reset_expected_s）'],
  stop_during_reset: ['safety', '解除急停执行期间又收到了一次“停止”，所以急停继续保持锁存。', '确认是谁按了停止、现场是否安全', '确认安全后再点“解除急停”'],
};

// ux05-fix2 (Q7b-05/08): what happens to a reset that has not returned, as this robot's runtime supports it.
// `f`: {reset_expire, expire_s, in_progress_s}; `own`: the desktop's own request outlived its limit.
function uxLateResetText(f, own) {
  f = f || {};
  const lim = typeof f.expire_s === 'number' && Number.isFinite(f.expire_s) ? f.expire_s : null;
  const ran = typeof f.in_progress_s === 'number' && Number.isFinite(f.in_progress_s) ? f.in_progress_s : null;
  if (f.reset_expire === true && lim !== null) {
    if (own) return `这次复位已经超过 ${lim} 秒的时限，现在在机器人上已作废：即使它之后完成，急停也继续保持锁存。它返回之前本体可能还在动作，请保持远离；需要时确认安全后重新解除急停。`;
    if (ran !== null && ran >= lim) return `复位已经执行了 ${Math.round(ran)} 秒，超过了桌面发出复位时给的 ${lim} 秒时限，在机器人上已作废：即使它之后完成，急停也继续保持锁存。它返回之前本体可能还在动作，请保持远离；需要时确认安全后重新解除急停。`;
    return `复位如果超过 ${lim} 秒的时限才完成，会被机器人作废、急停继续保持锁存。在它返回之前本体可能正在动作，请保持远离；按“停止”可以立即取消它。`;
  }
  return '这台机器人的运行时不会作废超时的复位：它一旦完成，急停可能自动解除，本体可能起立或动作。请保持远离；按“停止”可以取消它（急停保持锁存）。';
}

const uxHasCJK = s => /[一-鿿]/.test(s);

// Status-only meaning (no code). `s` is the status the device/cloud answered.
function uxByStatus(s) {
  if (s === 401) return ['account', '登录状态已失效，或访问令牌不对。', '重新登录，或检查访问令牌'];
  if (s === 403) return ['auth', '对方拒绝了这个操作（没有权限或缺少授权）。', '展开“技术细节”查看原因'];
  if (s === 404) return ['notfound', '对方没有提供这个功能（可能是版本较旧的接入服务）。', '升级接入服务'];
  if (s === 409) return ['state', '机器人当前的状态不允许这个操作。', '展开“技术细节”查看原因'];
  if (s === 413) return ['input', '提交的内容太大。', '减小内容后再试'];
  if (s === 429) return ['state', '请求太频繁。', '稍等片刻再试'];
  if (s === 400 || s === 422) return ['input', '对方拒绝了这个请求：请求内容不符合要求。', '展开“技术细节”查看原因'];
  if (s >= 500 && s < 600) return ['service', '对方服务内部出错了。可以稍后重试；如果一直出现，请把技术细节发给技术支持。', '稍后重试'];
  return null;
}

// Narrow text heuristics, used only when there is neither a code nor a device/cloud status.
// Order matters: safety words first; numbers alone never mean a server error.
const uxSpecificRules = [
  [/仿真本体的测试结果不能作为现场证据/, () => UX_CODES.simulated_evidence],
  [/直接运行技能目前只对仿真机器人开放/, () => UX_CODES.simulated_only],
  [/急停已锁存/, () => UX_CODES.estop_latched],
  [/被安全监督器拒绝|安全监督器|超出(允许)?(的)?(工作区|范围|限位|限制)|限位|工作区|最多 ?\d+ ?步/, () => UX_CODES.safety_rejected],
  [/复位.*尚未返回|reset_in_progress/, () => UX_CODES.reset_in_progress],
  [/需要 ?hardware_authorized|需要先勾选现场安全确认|需要在现场确认安全并明确授权/, () => UX_CODES.authorization_required],
  [/设备端未通过应用执行前检查/, m => /技能已安装/.test(m) && /未安装|找不到|not installed|没有安装/i.test(m)
    ? ['input', '机器人端的执行前检查没有通过：应用里用到的技能还没有安装到这台机器人上。', '先在“我的机器人 → 能力与技能”里把技能安装到机器人，再重试']
    : /参数/.test(m) ? ['param', '机器人端的执行前检查没有通过：某个步骤的参数不符合技能的要求。', '在应用里修改参数后保存为新版本，再重试']
    : ['input', '机器人端的执行前检查没有通过，所以这次请求没有发给机器人执行。', '展开“技术细节”可以看到是哪一项没通过']],
  [/技能不接受参数/, () => ['param', '填写了这个技能不认识的参数。', '删除多出来的参数，或改用表单填写']],
  [/缺少技能参数/, () => ['param', '有必填的参数还没有填写。', '补上必填参数']],
  [/超出允许范围|不能小于|不能大于|须在 .* 之间|须为 \[.*\] 之一|不在可选项/, () => UX_CODES.param_out_of_range],
  [/须为正的?有限|须为有限|须为整数|须为数字|须为正数/, () => ['param', '有一个参数的值不符合要求（例如需要正数）。', '按提示修改参数']],
  [/技能参数 .* 需要|parameters\.\S+ 需要/, () => ['param', '有一个参数的类型不对（例如应该填数字却填了文字）。', '按表单提示修改']],
  [/不能包含 NaN|无穷大|非有限数值/, () => ['param', '参数里有无效的数字（NaN 或无穷大）。', '改成普通数字']],
  [/运行参数不是合法 JSON|不是合法 JSON|Unexpected token|JSON\.parse|JSON at position/, () => ['input', '高级模式里的 JSON 格式不对（常见原因：少了引号、逗号或括号）。', '关闭“高级”改用表单填写']],
];
// Generic wording: only when there is neither a code nor a device/cloud status.
const uxGenericRules = [
  // A run that exceeded its own time limit is not a network problem.
  [/运行 ?\S* ?超时|超过(了)?(技能|运行)?(的)?时限|运行时间超过/, () => ['state', '技能运行超过了允许的时长，已被停止。', '查看实时状态里的记录', '需要更长时间时，调大技能或步骤的时限']],
  [/Failed to fetch|NetworkError|Load failed|^请求失败$/, () => ['network', '连不上本机的 NeuxMind 后台服务。', '确认应用仍在运行，然后重试']],
  [/Connection refused|ECONNREFUSED|Errno 111|拒绝连接|无法连接|连接被拒绝/, () => UX_CODES.device_unreachable],
  // Network timeouts only in explicit wording -- never a bare "timeout" (timeout_s is a parameter name).
  [/timed out|ETIMEDOUT|未在时限内响应|没有在 [\d.]+ 秒内响应|连接超时|响应超时|请求超时/i, () => UX_CODES.device_timeout],
  [/Name or service not known|nodename nor servname|getaddrinfo|Errno -2|Errno 8/, () => ['network', '找不到这个地址对应的服务器。', '检查地址是否拼写正确']],
  [/CERTIFICATE_VERIFY_FAILED|SSL: |certificate verify/i, () => ['network', '对方的安全证书无法验证。', '确认地址正确，或联系提供服务的团队']],
  [/未登录|登录已过期|请先登录/, () => ['account', '登录状态已失效，请重新登录。', '重新登录']],
];
const uxTextRules = [...uxSpecificRules, ...uxGenericRules];

// HTTP status written explicitly in a message ("HTTP 503", "status 500") -- never a bare number.
const uxExplicitStatus = m => {
  const hit = /\bHTTP[ /]?(\d{3})\b|\bstatus(?: code)?[ =:]+(\d{3})\b/i.exec(m);
  return hit ? Number(hit[1] || hit[2]) : null;
};

// The parameter a refusal is about: a body field first, else the runtime's own wording.
function uxRefusedParam(body, message) {
  for (const k of ['param', 'parameter', 'field']) if (body && typeof body[k] === 'string' && body[k]) return body[k];
  const m = /(?:技能参数|缺少技能参数|参数) ?([A-Za-z_][\w]*)/.exec(message || '');
  return m ? m[1] : '';
}

// Normalise what callers have: an Error from api() (status, reason, body), an operation result
// ({error, code, device_status}), or a bare string.
function uxErrorInput(input) {
  if (input === null || input === undefined) return {message: '', code: '', status: null, body: {}};
  if (typeof input === 'string') return {message: input, code: '', status: null, body: {}};
  const body = input.body && typeof input.body === 'object' ? input.body : (input instanceof Error ? {} : input);
  const message = String(input.message ?? input.error ?? body.error ?? '');
  const code = typeof input.reason === 'string' ? input.reason : typeof body.code === 'string' ? body.code
    : typeof input.code === 'string' ? input.code : '';
  // The status that carries meaning: what the device/cloud answered; the desktop's own 400 says nothing.
  let status = Number.isInteger(body.device_status) ? body.device_status : Number.isInteger(input.device_status) ? input.device_status : null;
  const own = Number.isInteger(input.status) ? input.status : null;
  const fromDevice = status !== null;
  if (status === null && own !== null && own !== 400) status = own;
  return {message, code, status, body, own, fromDevice};
}

function uxClassify(input) {
  const e = uxErrorInput(input);
  const detail = e.message.trim() || '未知错误';
  let entry = null, source = 'text';
  if (e.code === 'estop_latched' && UX_RESET_CAUSES[e.body?.cause]) { entry = UX_RESET_CAUSES[e.body.cause]; source = 'code+cause'; }
  // A generic code whose `problem` names the parameter refusal (Q6-08: bad_request + problem: param_unknown).
  else if (e.code === 'bad_request' && e.body?.problem === 'param_unknown') { entry = UX_CODES.param_unknown; source = 'code'; }
  else if (e.code && UX_CODES[e.code]) { entry = UX_CODES[e.code]; source = 'code'; }
  else if (e.code && uxIsParamCode(e.code)) { entry = UX_PARAM_GENERIC; source = 'code'; }
  // The desktop's own 500 (no device status): outcome unknown -- check first, never "retry later".
  else if (!e.code && e.own === 500 && !e.fromDevice && /操作未确认/.test(detail)) { entry = UX_CODES.desktop_internal_error; source = 'status'; }

  if (!entry && e.status !== null) {
    // A generic status still yields to an explicit safety/param sentence from the device.
    const rule = uxSpecificRules.find(([p]) => p.test(detail));
    if (rule) { entry = rule[1](detail); source = 'status+text'; }
    else { entry = uxByStatus(e.status); source = 'status'; }
  }
  if (!entry) {
    const rule = uxTextRules.find(([p]) => p.test(detail));
    if (rule) entry = rule[1](detail);
    else {
      const s = uxExplicitStatus(detail);
      if (s) entry = uxByStatus(s);
      else if (/Internal Server Error|Traceback/.test(detail)) entry = UX_CODES.internal_error;
      else if (/\bUnauthorized\b/.test(detail)) entry = uxByStatus(401);
      else if (/\bForbidden\b/.test(detail)) entry = uxByStatus(403);
      else if (/\bNot Found\b/.test(detail)) entry = uxByStatus(404);
    }
  }
  const out = {code: e.code || '', status: e.status, source};
  if (!entry) {
    // Already plain Chinese from the desktop: show it as is; raw technical text gets a generic line.
    if (!uxHasCJK(detail)) return {...out, kind: '', plain: '操作没有完成。展开“技术细节”可以看到系统返回的原始信息。', next: [], detail};
    return {...out, kind: '', plain: detail, next: [], detail: ''};
  }
  const [kind, plain, ...next] = entry;
  const res = {...out, kind, plain, next, detail};
  const b = e.body || {};
  if (kind === 'param' || kind === 'skill') {
    const param = uxRefusedParam(e.body, detail);
    if (param) {
      res.param = param;
      if ((b.min !== undefined && b.min !== null) || (b.max !== undefined && b.max !== null)) res.range = {min: b.min ?? undefined, max: b.max ?? undefined};
      if (Array.isArray(b.choices || b.options)) res.choices = b.choices || b.options;
      if (b.constraint && typeof b.constraint === 'object') res.constraint = b.constraint;
      if (typeof b.expected === 'string') res.expected = b.expected;
      if (typeof b.problem === 'string') res.problem = b.problem;
    }
  }
  // ux05 (Q6-08): what the runtime adds about a reset -- how long the previous one has been running, the
  // cause, and its own hint for the operator.
  if (typeof b.reset_in_progress_s === 'number' && Number.isFinite(b.reset_in_progress_s)) {
    res.reset_in_progress_s = b.reset_in_progress_s;
    if (['reset_in_progress', 'reset_pending'].includes(e.code)) res.plain = plain + `（上一次复位已经执行了 ${Math.round(b.reset_in_progress_s)} 秒）`;
  }
  // ux05-fix2 (Q7b-05/08): the desktop knows whether this robot voids a late reset (`reset_expire`, from its
  // /v1/status) and whether its own request already outlived the limit (`expired`). Say exactly that.
  if (['reset_in_progress', 'reset_pending'].includes(e.code) && (typeof b.reset_expire === 'boolean' || b.expired === true)) {
    const facts = {reset_expire: b.reset_expire, expire_s: b.expire_s, in_progress_s: b.reset_in_progress_s};
    if (b.expired === true && b.own_reset === true) {
      res.plain = `解除急停的请求已经发给机器人，但这次复位（例如起立）超过 ${b.expire_s} 秒还在执行、没有返回。` + uxLateResetText(facts, true);
      res.expired = true;
      res.next = b.reset_expire === true ? ['在现场确认机器人状态和周围安全', '需要时再点一次“解除急停”', UX_RESET_RESTART]
        : ['要取消：按“停止”（这次复位会失败，急停保持锁存）', UX_RESET_RESTART];
    } else {
      const lead = e.code === 'reset_pending'
        ? '解除急停的请求已经发给机器人，但机器人上一次复位（例如起立）还在执行、一直没有完成，所以这次请求的结果要等它完成后才知道。'
        : '机器人上一次“解除急停”（复位）还在执行，所以这次复位被拒绝了。';
      res.plain = lead + uxLateResetText(facts, false) + (res.reset_in_progress_s !== undefined ? `（上一次复位已经执行了 ${Math.round(res.reset_in_progress_s)} 秒）` : '');
    }
  }
  if (typeof b.cause === 'string' && b.cause) res.cause = b.cause;
  if (typeof b.hint === 'string' && b.hint.trim()) res.hint = b.hint.trim();
  // Generic codes/statuses: when the device said something specific in Chinese, show that too.
  if (['bad_request', 'conflict', 'error', 'invalid_request'].includes(e.code) || (source === 'status' && uxHasCJK(detail))) {
    res.plain = plain.replace(/。$/, '') + '：' + detail;
  }
  return res;
}

// The label a declared option has (Q6-07: `options: [{value, label}]`); the value itself otherwise.
function uxChoiceLabel(value, decl) {
  const opt = Array.isArray(decl?.options) ? decl.options.find(o => o && o.value === value) : null;
  return opt ? String(opt.label) : typeof value === 'string' ? value : JSON.stringify(value);
}

const UX_EXPECTED_TYPES = {int: '整数', float: '数字', bool: '是 / 否', str: '文字', enum: '列表中的一个选项', vector: '一组数字',
  floats: '一组数字', pose: '7 个数字的位姿', blob_ref: '文件引用', object: '多项内容'};

// A field-level line for a parameter refusal, e.g. "圈数（loops）：允许范围 1 – 3", by what is wrong (Q6-08).
function uxParamRefusalText(c, label, decl) {
  const bits = [];
  const k = c.code || '';
  if (k === 'param_required') bits.push('必填');
  else if (k === 'param_type') bits.push('类型不对' + (c.expected ? '，需要' + (UX_EXPECTED_TYPES[c.expected] || c.expected) : ''));
  else if (k === 'param_declaration_invalid') bits.push('技能包对这个参数的声明有误，请联系技能作者');
  else if (k === 'param_length') {
    const t = c.constraint || {};
    if (t.length !== undefined) bits.push(`长度不对，需要 ${t.length} 个`);
    else if (t.min_length !== undefined || t.max_length !== undefined) bits.push(`长度不对，允许 ${t.min_length ?? 0} – ${t.max_length ?? '不限'}`);
    else bits.push('长度不对');
  }
  if (c.range && k !== 'param_declaration_invalid') bits.push('允许范围 ' + (c.range.min ?? '不限') + ' – ' + (c.range.max ?? '不限'));
  if (c.choices && k !== 'param_declaration_invalid') bits.push('可选 ' + c.choices.map(x => uxChoiceLabel(x, decl)).join(' / '));
  return (label || c.param) + '：' + (bits.length ? bits.join('，') : '机器人不接受这个值') + '（' + c.detail.replace(/^设备拒绝运行：/, '') + '）';
}
