# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 现在的状态

**0.17.0（build 27）已合并、回归、打包并部署到本机（2026-11-13 04:14），等用户做最终检查。** 它是 0.16.0（已在 main）加上 #5、#6、#7 和一处集成修正。记录和要看的项目见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#验证后的新-issue567-0170-build-27)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| main | 0.16.0：第 08 轮的 #2、#3、#4、账号管理等 | `origin/main` @ a59bc49 | ✅ 已在 main |
| #5 | 调查本体不再限制 Token 和运行时间 | `feat/investigation-no-limits` @ 2224a49（合并提交 3488405） | ✅ 已合入 `integration` |
| #7 | 控制台下拉框、同色系悬停状态、运行中不显示代码和 JSON | `feat/style-polish` @ f308172（合并提交 e6d75b4） | ✅ 已合入 `integration` |
| #6 | 调研状态、重新调研、对话修复 | `feat/investigation-states` @ 01cfa7e（合并提交 ae05219） | ✅ 已合入 `integration` |
| 总指挥 | 审批里的命令保持可见（#7 与 #2 / #6 的冲突） | `integration` @ c0904c6 | ✅ |
| 总指挥 | 文档、版本 0.17.0（build 27）、统一回归、打包、部署 | `integration` @ 0719f8f 及之后的文档提交 | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：main（a59bc49）+ 以上全部，已推送到 `origin/integration`。**#5、#6、#7 还没有合并到 main**：用户最终检查并确认后再合并（快进）。
- 部署：`/Applications/NeuxMind Robot OS.app` 是 0.17.0（build 27），2026-11-13 04:14 替换并启动；后端报告版本 0.17.0，只监听回环地址。部署前的数据目录备份在 `~/Library/Application Support/NeuxMind Robot OS-backup-20261113-041426`。云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.17.0-arm64.dmg`（SHA-256 `db68de38df070ad09aba843abc935a3c6f6ee4ad646b8de4f9b0754844c542d0`）和同名 `.pkg`（`b74ef4b6df027751d089603b446e26bf24f2247e74dd99e5c4251744a6b673df`）；0.16.0、0.15.1、0.15.0 的也还在同一目录。
- 新的 issue 分支请基于现在的 `integration`。浏览器验收经 `tests/gate_helper.cjs` 登录，测试服务端带 `NEUXMIND_MAIL_DEV_ECHO=1`；界面改动也要在 WebKit 上跑一遍（桌面应用是 WKWebView）。

## 待办

1. 用户做最终检查（要看的项目在轮次记录的“最终检查要看的”）。
2. 用户确认后：把 `integration` 合并到 main 并推送，关闭 #5、#6、#7。
3. 请用户确认集成时定的一点：审批框里要批准的命令默认可见（其余代码按 #7 折叠）。
4. 请用户和提交者决定：没有配置邮件服务时，内置测试账号默认是开启的，它的密码写在公开的源码里（`NEUXMIND_TEST_ACCOUNT=0` 可以关掉）。
5. `fix/preexisting-test-failures` 的独立审查留下 5 条低级别意见，尚未处理。
6. Linux 容器里 `test_conformance_rereview04` 的一项计时类用例对宿主机负载敏感，留意是否再现。
7. 真实服务上的验证仍然没有：真实 TokenFans Key 跑调查和对话修复；真机上的调研、重新调研、修复；真实邮件服务（SMTP）发验证码。
