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

- `integration`（工作区 `PhysicalOS-integration/`）= fefdbd2（0.20.1），已推送到 `origin/integration`；main 仍是 df3a108（0.19.0），合并到 main 要用户确认。
- 部署：`/Applications/NeuxMind Robot OS.app` 是 0.20.1（build 31），2026-11-21 07:05 替换并启动。部署前的数据目录备份在 `~/Library/Application Support/NeuxMind Robot OS-backup-20261121-070532`。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.20.1-arm64.dmg`（SHA-256 `e79a1936708cad4a0c7a4bd02ca433f0da7c15d4756988f65cf31fc9a5b05763`）和同名 `.pkg`（`b939a48cd8a12e195a86c750738a0877c675b84fc14de0b0cc41d270addfca66`）；0.20.0 及更早的也在 `PhysicalOS-integration/dist/`。
- 真机：运行时仍是 0.4.0（`~/neuxmind`，回环 8790，本机手工隧道 18790）。#22、#25、#26 改了运行时，更新真机要用户同意，且脚本和运行时要一起换。
- 新的 issue 分支请基于 `integration`（fefdbd2）。浏览器验收经 `tests/gate_helper.cjs` 登录，测试服务端带 `NEUXMIND_MAIL_DEV_ECHO=1`；添加设备的表单要先选本体类型（#32）；界面改动也要在 WebKit 上跑一遍。提交钩子的密钥扫描会把 `password: '<12 个字以上>'` 当成写死的密码，提示文字里的字段名请加引号。

## 待办

1. **用户统一检查 0.20.1**（要检查的项目在第 09 轮记录里）。检查通过后由用户决定是否合并到 main。
2. 各会话按默认做法交付、等用户确认的决定：#15 的 5 级上限、#22 的六项默认、#32 的内置类型内容、#24 的重试策略、#25 磁盘被别的东西占满时不清记录，见第 09 轮记录。
3. GitHub 上 #11、#12、#13 已在 main（0.19.0）里，仍是打开状态；#14「配置连接页面修改」还没有会话；#15–#35 里 #16、#17、#19–#21、#23、#28–#31、#34、#35 还没有会话。
4. 真机上的运行时要不要换成这一版（运行时改了安全限值、记录、告警上报），要用户决定。
5. 以前的待办仍在：#12 留下的问题（机器人连不上时是否也能不经登录测试直接保存）；内置测试账号默认开启待定；`fix/preexisting-test-failures` 的 5 条低级别意见在那个会话里等确认；真实 TokenFans、SMTP、真机上的完整流程还没有验证。
