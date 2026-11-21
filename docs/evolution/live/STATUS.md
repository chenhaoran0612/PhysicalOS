# 实时进度 · 第 09 轮（R1 修复）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-19-iter09.md](../iterations/2026-11-19-iter09.md)，上一轮见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-21

## 现在的状态

**0.20.1（build 31）已进 `integration`（fefdbd2）、回归、打包并部署到本机（2026-11-21 07:05），等用户统一检查。** 它是 0.19.0 加上 R1 修复第一批的 9 个 issue、总指挥的衔接修复，以及 0.20.0 打包之后的三处改动（删除确认框列出设备档案、#18 的后续改动、云平台 `GET /me` 按 Key 权限收窄）。要检查的项目见 [第 09 轮记录](../iterations/2026-11-19-iter09.md#交给用户验证0200-build-30) 和 [0.20.1 一节](../iterations/2026-11-19-iter09.md#0201-build-31)。

| issue | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| #15 | F01 多级跳板机 | `feat/ssh-multi-hop` @ c294b92（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| #18 | F04 向设备传文件 | `feat/device-upload` @ 23224c3（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| #22 | F08 安全限值 | `feat/safety-limits` @ cc6d49a（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| #24 | F10 心跳和在线状态 | `feat/heartbeat-online-state` @ 5b180c3（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| #25 | F11 设备上的记录 | `feat/device-records-retention` @ 93a183f | 🟡 在 0.20.1 里，等用户验证 |
| #26 | F12 告警上报 | `feat/device-alert-report` @ 85bb643 | 🟡 在 0.20.1 里，等用户验证 |
| #27 | F13 读取 ROS 2 档案 | `feat/ros2-profile` @ 965c0ca | 🟡 在 0.20.1 里，等用户验证 |
| #32 | F18 本体类型清单 | `feat/body-type-list` @ 9e85199（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| #33 | F19 资料库 | `feat/material-library` @ 6f6f653 | 🟡 在 0.20.1 里，等用户验证 |
| — | 云平台 `GET /me` 按 Key 权限收窄 | `fix/me-key-scope` @ 103fab5（合并会话代为提交） | 🟡 在 0.20.1 里，等用户验证 |
| 总指挥 | 衔接修复 350de57、3616a3c、a4e1188、848bd4e，测试修正；版本 0.20.0（build 30）和 0.20.1（build 31）；回归、打包、部署、文档 | `integration` @ fefdbd2 | 🟡 等用户验证 |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）的代码是 fefdbd2（0.20.1），之后只有文档提交，已推送到 `origin/integration`；main 仍是 df3a108（0.19.0），合并到 main 要用户确认。
- 部署：`/Applications/NeuxMind Robot OS.app` 是 0.20.1（build 31），2026-11-21 07:05 替换并启动。部署前的数据目录备份在 `~/Library/Application Support/NeuxMind Robot OS-backup-20261121-070532`。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.20.1-arm64.dmg`（SHA-256 `e79a1936708cad4a0c7a4bd02ca433f0da7c15d4756988f65cf31fc9a5b05763`）和同名 `.pkg`（`b939a48cd8a12e195a86c750738a0877c675b84fc14de0b0cc41d270addfca66`）；0.20.0 及更早的也在 `PhysicalOS-integration/dist/`。
- 真机：运行时仍是 0.4.0（`~/neuxmind`，回环 8790，本机手工隧道 18790）。#22、#25、#26 改了运行时，更新真机要用户同意，且脚本和运行时要一起换。
- 新的 issue 分支请基于 `integration`（0.20.1 的代码是 fefdbd2，之后只加了文档）。浏览器验收经 `tests/gate_helper.cjs` 登录，测试服务端带 `NEUXMIND_MAIL_DEV_ECHO=1`；添加设备的表单要先选本体类型（#32）；界面改动也要在 WebKit 上跑一遍。提交钩子的密钥扫描会把 `password: '<12 个字以上>'` 当成写死的密码，提示文字里的字段名请加引号。

## 开着的 23 个 issue（2026-11-21）

用户 2026-11-21：「现在开着的一共有23个issue，处理完成之后一起打包验证」。下一次打包等这些都处理完。

| 状态 | issue |
|---|---|
| 已在 main（0.19.0），GitHub 上仍开着 | #11、#12、#13 |
| 在 0.20.1 里（已进 `integration`），等用户验证 | #15、#18、#22、#24、#25、#26、#27、#32、#33 |
| 第二批，正在做 | #16、#17、#28、#31、#34 |
| 等依赖 | #19（等 #17）、#20（等 #19）、#21（等 #22、#23）、#29（等 #28）、#30（等 #34） |
| 还没有会话 | #14 |

同日关闭的两个：#23（设备接口表）关闭为不做，它的工作区和分支已删除，#21 的依赖里还写着它，要重新定；#35（移除设备时处理设备上的内容）关闭为已完成。

第二批由研发计划会话 2026-11-21 启动，工作区都基于 `integration` @ 8736aa1（0.20.1）。分支名以各会话交付时的说明为准。

| issue | 内容 | 分支 | 工作区 |
|---|---|---|---|
| #16 | 验证连接 | `feat/verify-connection` | `PhysicalOS-verify-connection/` |
| #17 | 平台转发管理层服务 | `feat/runtime-forward` | `PhysicalOS-runtime-forward/` |
| #28 | 运控驱动检查 | `feat/motion-driver-check` | `PhysicalOS-driver-check/` |
| #31 | 安装包送到设备并核对 | `feat/driver-package-delivery` | `PhysicalOS-package-delivery/` |
| #34 | 资料解析 | `feat/material-parsing` | `PhysicalOS-material-parsing/` |

合并时预计重叠的地方：
- #16 和 #17 都在连接层：`brainos/device_access.py`、`brainos/connector.py`、`web/app.js` 里管理层地址的部分。
- #28 和 #31 都在修复流程附近：`brainos/hardware_init.py`。#28 原本要和 #23 共用「标准接口对应哪种消息类型」的约定，#23 已关闭。
- #34 改云平台的共享文件（`app.py`、`config.py`、`db.py`、`maintenance.py`、`auth.py`）和 `web/material_library.js`；#31 按 #34 定的资料状态取值判断。

## 待办

1. **开着的 23 个 issue 都处理完后一起合并、打包、验证**（见上面的表）。0.20.1 已部署，要检查的项目在第 09 轮记录里；合并到 main 由用户决定。
2. 各会话按默认做法交付、等用户确认的决定：#15 的 5 级上限、#22 的六项默认、#32 的内置类型内容、#24 的重试策略、#25 磁盘被别的东西占满时不清记录，见第 09 轮记录。
3. #14「配置连接页面修改」还没有会话；#21 的依赖 #23 已关闭为不做，要重新定。#11、#12、#13 已在 main 里，GitHub 上仍开着。
4. 真机上的运行时要不要换成这一版（运行时改了安全限值、记录、告警上报），要用户决定。
5. #18 的弱网验收线已改（2026-11-19，见 issue #18 正文）：带宽 1 Mbps 以内、丢包率 30%、时延 300 ms 以上时要传完且校验一致；60% 时不要求传完，只要求不留下损坏的文件、网络恢复后从断点续传到完成。按新线还没有测，放到下一次打包时跑；以前在 60% 下测时 SSH 建连本身失败。
6. 以前的待办仍在：#12 留下的问题（机器人连不上时是否也能不经登录测试直接保存）；内置测试账号默认开启待定；`fix/preexisting-test-failures` 的 5 条低级别意见在那个会话里等确认；真实 TokenFans、SMTP、真机上的完整流程还没有验证。
