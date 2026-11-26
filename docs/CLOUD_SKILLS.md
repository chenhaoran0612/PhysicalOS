# 技能在云端：存放、下载、第三方调用（issue #43）

技能代码、机器人上的程序和场景数据原来只在本机和机器人上。本文说明它们怎样放到云平台、怎样从云平台装到机器人，以及第三方怎样经云平台的开放接口调用一台机器人上的技能。

> 状态：代码和测试完成；**没有在任何真机上运行过，也没有连过部署在外面的云平台**。验证范围见[“在哪里验证过，哪里没有”](#在哪里验证过哪里没有)。

用户在 issue 会话里定的三件事（2026-11-24）：

1. 第三方的调用由**机器人上的管理层自己向云端取**（只出不进），桌面应用不开也能被调用。
2. 运动类技能的远程调用靠**机器人主人预先开启**：在控制台里按机器人、按技能打开“允许远程调用”，可随时关闭；没打开的技能，远程调用被拒绝。停止随时可用。
3. 放到云端的包括**技能包**，以及**机器人上的程序和场景数据**；都存在自己组织名下，不公开。

## 总览

```
开发者 / 控制台                         云平台 (/api/v1)                          机器人（管理层）
  发布技能包  ───────────────▶  技能库 /skills                                     
  收进程序和场景数据 ─────────▶  资料库 /library（kind = bundle）                  
  从云端安装 ◀───────────────  /skills/…/package   ──（控制台经现有通道）──▶  /v2/skills/install
  装程序和场景数据 ◀──────────  /library/materials/{id}/content ─（#18 通道）─▶  ~/neuxmind-bundles/…
                                                                                   
第三方（API Key: skills:invoke）                                                    
  POST /open/robots/{id}/skills/{skill}/calls ─▶ 调用队列 ◀── POST /device-link/sync（每 2 秒，Key: device:link）
  GET  /open/calls/{id}  ◀──────────────────── 状态、进度、结果 ◀── 同一个请求带回
  POST /open/robots/{id}/stop ────────────────▶ 下一次同步带给机器人 ──▶ 停止并锁存
```

## 1. 云端技能库（`cloud/neuxmind_cloud/skill_store.py`，路由 `routes/skills.py`）

一条记录 = 某个组织的某个技能的某个版本（`skill.package.v1`，含文件）。只在本组织内可见。

权限：`skills:read`（所有角色）、`skills:write`（owner / admin / developer）；两个都可以作为 API Key 的权限范围。

| 方法 路径 | 请求 → 响应 |
|---|---|
| POST `/skills/releases` | `{package: skill.package.v1（含 files）, notes?}` → 201 `Release`。用 spec 校验；`digest` 用和管理层安装时相同的算法。同一组织、同一 `id@version`：内容相同 → 200 原记录（`existing: true`）；内容不同 → 409 `skill_version_exists`。这个路径的请求体上限单独放宽到 3 MiB |
| GET `/skills` | → `{skills: [{id, title, description, latest: Release, versions: [version…]}]}`，按 `id` 排序 |
| GET `/skills/{skill_id}` | → `{id, releases: [Release…]}`（新的在前）；没有 → 404 `skill_not_found` |
| GET `/skills/{skill_id}/releases/{version}` | → `Release` |
| GET `/skills/{skill_id}/releases/{version}/package` | → `{release: Release, package: skill.package.v1（含 files）}`：这就是“下载到本地” |
| PUT `/skills/{skill_id}/releases/{version}/product` | issue #48：`{vendor, model}` → `Release`。给还没有品牌型号的版本指定一次；已经有且不同 → 409 `skill_product_set`。发布时也可以带 `vendor`、`model`（要一起给，否则 400 `invalid_product`；同一版本换一个品牌型号再发 → 409 `skill_other_model`）。`Release` 里的 `product` 是 `{vendor, model}` 或 `null`。见 [ROBOT_MODEL.md](ROBOT_MODEL.md) |
| DELETE `/skills/{skill_id}/releases/{version}` | → `{deleted: true}`（已装到机器人上的不受影响） |

`Release` = `{skill_id, version, title, description, digest, requires: [id…], optional: [id…], parameters: [...原样], motion: bool, timeout_s, files: [{path, size}], size, notes, created_by, created_at}`。`motion`：`requires` + `optional` 里有任何一项在能力标准里是运动类（或需要现场授权的）就为真。

`digest` 是管理层安装时算的那个值（对“校验过的 skill.json + 文件”取 SHA-256），不是控制台预览本机文件夹时用的那个；下载到的 `package` 是校验并规整过的内容，装到机器人上得到同一个 `digest`。发布和删除要求账户的邮箱已验证。`created_by` 是 `user:<编号>` 或 `key:<编号>`。

上限：每个组织 500 条；包的大小和文件数与 `sdk/neuxmind/package.py`、`brainos/skill_packages.py` 相同。错误码：`invalid_skill_package`、`skill_version_exists`、`skill_not_found`、`skill_quota_exceeded`。

## 2. 机器人上的程序和场景数据（资料库里的一种新类型 `bundle`）

有的机器人靠自己电脑上的程序行走（[PLACE_SKILLS.md](PLACE_SKILLS.md) 的 `programs`），这些程序和它们用的场景数据（地点标记、参考地图）是一个目录。它作为一个 `.tar.gz` 存进资料库（[MATERIAL_LIBRARY.md](MATERIAL_LIBRARY.md)），沿用资料库的分片上传、校验、配额和下载。

- 上传时多给一个字段 `kind: "bundle"`（只接受 `.tar.gz` / `.tgz`）。`bundle` 不解析：入库后状态直接是 `usable`，解析接口对它回答 409 `not_parsed_kind`。
- 大小上限 64 MiB。
- 它必须绑定一台设备（`device_id`、`device_name` 都要给，否则 400 `bundle_needs_device`），因为场景数据只对那一台机器人的那个场地有意义。
- 列表可以用 `kind=bundle` 过滤。#31 的驱动安装包送达不接受它（那里只接受 `package`）。

控制台两个动作（`brainos/robot_bundle.py`）：

- **收进云端**：经已保存的 SSH 连接，在机器人上用一段固定脚本把一个目录打成 `.tar.gz`（不跟随符号链接；跳过 `runs/`、`__pycache__/`、`*.bak*`、`*.pyc`；大小上限 64 MiB），取回本机，再上传到资料库。目录由人填写（例如 `~/walk_programs`），需要人确认。带凭据名字的文件和目录不带走，留下了什么会列出来。
- **装到机器人**：从云端取回并核对 SHA-256（`package_fetch`），经 #18 的通道送到 `~/neuxmind-bundles/<名字>/<SHA-256 前 16 位>.tar.gz`，再用固定脚本解到同名目录（拒绝绝对路径、`..`、指向目录外的链接；不保留属主），成功后把 `~/neuxmind-bundles/<名字>/current` 指向它。**不会覆盖机器人上原来的目录**，也不改适配器的配置；要让管理层用新的一份，由人把 `ros2-config.json` 里的路径改到 `current`。需要人确认。

## 3. 机器人连到云端（管理层 `runtime/neuxmind_runtime/cloud_link.py`）

沿用告警上报的那份配置文件（`--cloud-config`：`url`、`key`、`ca_file`，只走 HTTPS）。Key 带 `device:link` 权限范围时启用（同一个 Key 可以同时带 `alerts:report`）；配置里可以写 `"link": false` 关掉。没有配置时什么都不发。

管理层每 `poll_after_s`（云端给，默认 2 秒；有远程调用在跑时 1 秒；连不上时退到最长 30 秒）发一次：

`POST /api/v1/device-link/sync`（API Key，范围 `device:link`；登录会话被拒绝）

```json
{"schema": "neuxmind.device_link.v1",
 "device": {"instance_id": "…", "name": "…", "family": "…", "simulated": false, "runtime_version": "…"},
 "state": {"estop_latched": false, "busy": false, "motion_blocked": false},
 "skills": [{"id": "goto_place", "version": "0.1.0", "digest": "…", "title": "…", "description": "…",
             "parameters": [], "requires": ["base.navigate_to_place"], "motion": true, "timeout_s": 1800,
             "remote": {"allowed": true, "set_by": "…", "set_at": 0}}],
 "calls": [{"call_id": "cal_…", "status": "running", "run_id": "…", "progress": 0.4, "detail": "…",
            "result": null, "error": null}],
 "stops": ["stp_…"]}
```

→ `{"accepted": true, "device_id": "dlk_…", "poll_after_s": 2, "calls": [{"call_id", "skill_id", "version", "parameters", "timeout_s"}], "cancel": ["cal_…"], "stop": {"stop_id": "stp_…", "reason": "…"} | null}`

回答里还有 `settled`（云端已经记下终态、机器人不必再报的调用）、`rejected_skills`（云端没有收下的技能条目和原因）、`server_time`；下发的每个调用带着云端看到的 `digest`。

规则：

- `state` 的三项每次都要是布尔值；缺了或不是布尔值，云端按“是”处理（宁可拒绝调用）。`remote.allowed` 只有字面的 `true` 才算打开。
- 云端同一时间只给一台机器人下发一个调用；有停止还没被确认时不下发，新的调用以 409 `stop_pending` 拒绝。
- 调用运行超过 `timeout_s` + 120 秒，云端自己请求取消。
- Key 属于组织，不属于某一台机器人：一个带 `device:link` 的 Key 可以替本组织的任何一台机器人同步（和告警上报的 Key 一样）。它到不了别的组织。放在机器人上的 Key 只给这两个权限范围。
- `skills` 是这台机器人上**已安装**的技能和每个技能的远程开关；每次同步都带全量（上限 200 个）。
- 云端把 `queued` 的调用放进回答里并记为 `dispatched`；机器人在下一次同步的 `calls` 里报告它的状态之前，云端每次都重发。**机器人按 `call_id` 去重**（记在状态目录里，重启后仍然有效），同一个调用最多执行一次。
- 调用排队超过 `pickup_timeout_s`（默认 30 秒）没被取走 → `expired`，不再下发：机器人离线时发的运动指令不会在它恢复之后突然执行。已经下发、但机器人一直没有提到的调用，也只在下发后的 30 秒内重发；之后改为列在 `cancel` 里（万一它其实收到了），到 120 秒记为 `lost`。机器人自己也把关：下发的调用带 `created_at`，按同一个回答里的 `server_time`（都是云端的时钟）算，超过 150 秒的不执行，回报 `expired`。
- 机器人失联期间（调用已是 `lost`）发来的取消和停止不会丢：机器人回来的第一次同步就带给它，先停止、再取消、最后才看新的调用。**对一台有调用没结束就失联的机器人发的停止不会过期**，它什么时候回来就什么时候停下并锁存（解除要在现场）。
- 连不上时等待逐步拉长到 30 秒，但有远程调用在跑时最长只等 2 秒。管理层关闭过程中到达的回答，只执行其中的停止。
- `calls` 里机器人报告的状态：`running`、`succeeded`、`failed`、`cancelled`、`stopped`、`rejected`。终态带 `result` 或 `error: {code, message}`。机器人对一个终态一直报告到云端在回答里不再提它（回答的 `settled: ["cal_…"]`）。
- `stop`：机器人执行和 `POST /v1/stop` 相同的停止（锁存；解除要在现场），并在 `stops` 里回报已执行的 `stop_id`。
- 机器人拒绝一个调用（`rejected`）的原因码：`skill_not_installed`、`remote_not_allowed`、`estop_latched`、`device_busy`（同一时间只跑一个远程调用）、`invalid_parameters`、`not_authorized`、`version_mismatch`。
- 机器人 60 秒没有同步 → 云端认为它离线；它名下未结束的调用在 `lost_after_s`（默认 120 秒）后记为 `lost`（结果未知，不是失败）。

**远程开关（管理层本机接口，控制台经现有通道调用）：**

| 方法 路径 | 请求 → 响应 |
|---|---|
| GET `/v2/remote` | → `{schema: "neuxmind.remote_access.v1", link: {configured, enabled, state: off / connecting / online / error, host, last_sync_at, last_error, device_id}, skills: {<id>: {allowed, digest, set_by, set_at}}, calls: [最近 50 个远程调用的 {call_id, skill_id, status, run_id, started_at, finished_at, error}]}` |
| POST `/v2/remote/skills` | `{skill_id, allowed: bool, set_by, hardware_authorized, request_id}` → `{accepted: true, …同 GET}`。打开（`allowed: true`）需要 `hardware_authorized: true`（人在控制台里确认）和非空的 `set_by`；关闭不需要。技能没安装 → 404 `skill_not_found` |

- 开关绑定技能的 `digest`：技能重装成别的内容后，开关自动失效（`invalidated: true`），要重新打开。打开时请求里要带人在确认框里看到的那个 `digest`，和机器人上现在装的不一样（或没带）→ 409 `skill_changed`，什么都不改。
- 关闭总是先生效再落盘。落盘失败时回答 500 `remote_not_saved`，开关保持关闭并不断重试写入，另写一个小文件记下“这些技能是关的”；连它也写不了时，重启后所有开关都按关闭处理。关闭时这个技能正在跑的远程调用被取消（`cancelled`，原因 `remote_not_allowed`）。
- 急停锁存时所有远程调用都拒绝，不运动的技能也一样。
- 远程调用以 `hardware_authorized = true` 运行，依据就是主人打开的这个开关；其余和本机运行完全一样：参数在设备端校验，安全监督器照常把关（急停锁存、没填安全限值、不认识的地点、速度上限都照样拒绝）。
- 每个远程调用在设备记录里留一条（谁打开的开关、调用编号、技能、参数摘要、结果）。
- 连接线程和告警上报一样，只读安全状态，不占停止路径上的任何锁；它的任何故障都只表现为“没连上”。

## 4. 开放平台：第三方调用（`cloud/neuxmind_cloud/skill_calls.py`，路由 `routes/open_platform.py`）

认证：`Authorization: Bearer <API Key>`，权限范围 `skills:invoke`（owner / admin / developer 可以签发，签发人的邮箱要已验证）。只接受 API Key，登录会话回答 403 `api_key_required`。只能调用本组织的机器人。

除下表外还有 `GET /open/stops/{stop_id}`（`pending` / `delivered` / `acknowledged` / `expired`）；发起调用时可以带 `version`，`timeout_s` 只能比技能自己的短。停止对离线的机器人也收下，120 秒没被取走就作废；已经取走的停止在机器人确认之前一直有效。

| 方法 路径 | 请求 → 响应 |
|---|---|
| GET `/open/robots` | → `{robots: [{device_id, instance_id, name, family, simulated, online, estop_latched, busy, motion_blocked, runtime_version, last_seen_at, skills: n}]}` |
| GET `/open/robots/{device_id}/skills` | → `{device_id, online, skills: [{id, version, title, description, parameters, requires, motion, timeout_s, remote_allowed}]}`：每个技能要哪些参数（名字、类型、是否必填、选项、范围、单位、说明）都在这里 |
| POST `/open/robots/{device_id}/skills/{skill_id}/calls` | `{request_id, parameters?: {}, timeout_s?}` → 202 `{call: Call}`。同一个 `request_id` 再发一次返回同一个调用（200） |
| GET `/open/calls/{call_id}` | → `{call: Call}` |
| GET `/open/calls?device_id=&status=&limit=&before=` | → `{calls: [Call…], more, next_before}`，新的在前 |
| POST `/open/calls/{call_id}/cancel` | → `{call: Call}`；还在排队的直接 `cancelled`，已下发的在下一次同步时通知机器人取消 |
| POST `/open/robots/{device_id}/stop` | `{reason?}` → 202 `{stop_id, delivered: false}`；机器人在下一次同步（最多几秒）时停止并锁存。**这是经网络的尽力而为，不能代替现场急停** |

`Call` = `{id, device_id, skill_id, version, parameters, status, progress, detail, result, error: {code, message} | null, request_id, created_by, created_at, dispatched_at, started_at, finished_at}`。

`status`：`queued` → `dispatched` → `running` → `succeeded` / `failed` / `cancelled` / `stopped`；另外有 `rejected`（机器人拒绝）、`expired`（没被取走）、`lost`（机器人失联，结果未知）。

发起调用时云端先查（不代替机器人自己的检查）：

| 情况 | 回答 |
|---|---|
| 没有这台机器人 / 不是本组织的 | 404 `device_not_found` |
| 机器人离线 | 409 `device_offline` |
| 技能没装在这台机器人上 | 409 `skill_not_installed` |
| 主人没有打开远程调用 | 403 `remote_not_allowed` |
| 急停锁存中 | 409 `estop_latched` |
| 参数不符合技能的声明（多余的名字、缺必填、类型、选项、范围） | 400 `invalid_parameters`，带 `problems: [{name, problem}]` |
| 这台机器人已经有一个没结束的远程调用 | 409 `call_in_progress`，带 `call_id` |
| 调用太频繁（每个 Key 每分钟 60 次） | 429 `rate_limited` |

每次调用、取消、停止都写审计记录。调用记录保留 30 天。

### 例子

```bash
KEY=nx_live_…            # 权限范围 skills:invoke
BASE=https://cloud.example.com/api/v1

curl -s -H "Authorization: Bearer $KEY" $BASE/open/robots
curl -s -H "Authorization: Bearer $KEY" $BASE/open/robots/dlk_0123/skills
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"request_id": "order-42-step-1", "parameters": {"place": "会议室"}}' \
     $BASE/open/robots/dlk_0123/skills/goto_place/calls
curl -s -H "Authorization: Bearer $KEY" $BASE/open/calls/cal_4567
curl -s -X POST -H "Authorization: Bearer $KEY" $BASE/open/robots/dlk_0123/stop
```

## 5. 控制台

- **云端技能**（`brainos/cloud_skills.py`、`web/cloud_skills.js`、`web/cloud_skills.css`）：两个页面，「技能发布」和「技能加载」，看同一个云端技能库；「技能发布」把一个本机技能文件夹**发布到云端**（先按现有规则读取和校验），「技能加载」把云端的一个版本**安装到机器人**（下载 → 核对 `digest` → 兼容性说明 → 现有的 `POST /v2/skills/install`）。应用自带的示例技能可以一键发布到自己的技能库。
- **远程调用**（机器人页面上的一节）：这台机器人和云端连没连上；每个已安装技能一个“允许远程调用”开关（打开时要确认，运动类技能的确认里写明“第三方可以让机器人运动”）；最近的远程调用。
- **程序和场景数据**：见第 2 节的两个动作。

控制台这一部分的细节（页面、桌面端接口、两段设备脚本的规则、收进云端时留下不带的文件）在 [CLOUD_SKILLS_CONSOLE.md](CLOUD_SKILLS_CONSOLE.md)。

## 在哪里验证过，哪里没有

验证过的（全部是替身和本机的临时环境，**没有真机，没有部署在外面的云平台**）：

- 云平台 `cloud/tests/test_skill_store.py`、`test_device_link.py`、`test_open_platform.py`、`test_library_bundle.py`：技能库的发布、幂等、版本冲突、配额、组织隔离；同步接口的权限、输入的界限、调用从排队到结束的每一步（下发、重发的时限、取消、停止、过期、失联、机器人回来之后）；开放接口表里的每一种拒绝、参数预检、限速；`bundle` 不解析、不能当驱动安装包、不同类型的未完成上传不续传。`digest` 和管理层安装时算的逐字节相同（用 `runtime/examples/skills/goto_place` 对过）。整套云端测试在 SQLite 和 PostgreSQL 上各跑一遍（结果见交付说明）。
- 管理层 `runtime/tests/test_cloud_link.py`、`test_cloud_link_review.py`、`test_remote_access.py`（仿真四足，Linux 容器里）：一次调用的全过程、每一种拒绝、取消、停止并锁存、重复下发只执行一次、运行中重启、开关关掉、云端回答残缺或过大、连不上、Key 没有权限范围、关闭过程中到达的回答、关不掉盘时的开关。`test_cloud_link_https.py`：真实的 HTTPS（临时证书）上的报文。`test_cloud_link_real_cloud.py`：**管理层的连接代码对着真实的云平台应用**（同一进程里，不经网络）走通调用、取消、停止和确认、机器人上关开关、没有权限范围的 Key。
- 控制台 `tests/test_cloud_skills.py`、`test_cloud_skills_real.py`（对着真实的云平台应用和仿真机器人：发布示例、从云端安装、`digest` 一致、收进和装回程序包）、`test_remote_calls.py`（含真实管理层的 `/v2/remote`）、`test_robot_bundle.py`、`test_robot_bundle_scripts.py`（两段设备脚本在 macOS 和 Ubuntu 22.04 的 GNU tar 1.34、dash 和 bash、root 和普通用户下真的执行）；三个页面脚本的逻辑测试；浏览器验收 `tests/cloud_skills_browser.cjs` 在 Chromium 和 WebKit 上通过。
- 两轮独立审查（远程调用这条路；控制台和设备脚本）复现出的问题都已修正并各有测试。

没有验证的：

- **真机上的任何一步**：没有给任何机器人更新管理层、配置云端连接、打开远程开关或发起远程调用；设备脚本没有在真机上运行过（真机的 `tar`、`find`、经跳板机和弱网的回传都没试过）。
- **部署在外面的云平台**：连接只走 HTTPS，本机 Docker 里的云平台是明文的，所以管理层和它之间没有真的连过；要先在它前面放 TLS。
- 弱网、长时间断线之后的实际表现只在测试里用时钟推演过。
- 第三方技能代码的隔离没有变化（ADR-018）：这里开放的是“调用自己组织已经装好的技能”，不是让第三方上传代码。

## 已知限制

- 放在机器人上的 Key 属于组织而不属于某一台机器人；同一个 `instance_id` 的两台机器人（克隆的系统盘，或拿到本组织 `device:link` Key 的人）都能取到同一个调用，先同步的那个还能取走并确认本该给真机的停止。“最多执行一次”是对一台机器人的状态目录说的。
- 经云端的停止要等机器人下一次同步，正常是几秒，断网时到不了。**它不能代替现场急停。**
- 一个没有被机器人确认收到的调用，在最多两分钟里会让这台机器人显示为“有调用在进行”，新的调用被拒绝。
- 收进云端时只按确切的名字留下凭据类文件（清单见控制台文档）；名字不寻常的密钥文件、写在配置文件里的密码会一起进云端。收之前请看一眼目录。
- 装到机器人不会改适配器的配置：要用新的一份，由人把 `ros2-config.json` 里的路径改到 `~/neuxmind-bundles/<名字>/current`。
- 控制台“安装管理层”还不能替设备写云端连接的配置文件（和告警上报一样，要在设备上放）。
- SDK 的命令行（`nx`）还没有发布技能和调用开放接口的命令；第三方现在按本文的 HTTP 接口调用。
