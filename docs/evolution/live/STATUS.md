# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 现在的状态

**0.17.0（build 27）已合并、回归、打包并部署到本机（2026-11-13 04:14）；用户最终检查后确认，`integration` 已合并到 main（快进）并推送（2026-11-13）。** 它是 0.16.0 加上 #5、#6、#7 和一处集成修正。记录和要看的项目见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#验证后的新-issue567-0170-build-27)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| 0.16.0 | 第 08 轮的 #2、#3、#4、账号管理等 | a59bc49 | ✅ 已在 main |
| #5 | 调查本体不再限制 Token 和运行时间 | `feat/investigation-no-limits` @ 2224a49（合并提交 3488405） | ✅ 已在 main |
| #7 | 控制台下拉框、同色系悬停状态、运行中不显示代码和 JSON | `feat/style-polish` @ f308172（合并提交 e6d75b4） | ✅ 已在 main |
| #6 | 调研状态、重新调研、对话修复 | `feat/investigation-states` @ 01cfa7e（合并提交 ae05219） | ✅ 已在 main |
| 总指挥 | 审批里的命令保持可见（#7 与 #2 / #6 的冲突） | `integration` @ c0904c6 | ✅ |
| 总指挥 | 文档、版本 0.17.0（build 27）、统一回归、打包、部署 | `integration` @ 0719f8f 及之后的文档提交 | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：包含以上全部。**已在用户最终检查并确认后合并到 main（快进，2026-11-13）并推送**，`origin/main` 与 `origin/integration` 指向同一个提交。之后的 issue 仍然先合到 `integration`，统一回归后由用户确认再合并到 main。
- 部署：`/Applications/NeuxMind Robot OS.app` 是 0.17.0（build 27），2026-11-13 04:14 替换并启动；后端报告版本 0.17.0，只监听回环地址。部署前的数据目录备份在 `~/Library/Application Support/NeuxMind Robot OS-backup-20261113-041426`。云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.17.0-arm64.dmg`（SHA-256 `db68de38df070ad09aba843abc935a3c6f6ee4ad646b8de4f9b0754844c542d0`）和同名 `.pkg`（`b74ef4b6df027751d089603b446e26bf24f2247e74dd99e5c4251744a6b673df`）；0.16.0、0.15.1、0.15.0 的也还在同一目录。
- 新的 issue 分支请基于现在的 `integration`。浏览器验收经 `tests/gate_helper.cjs` 登录，测试服务端带 `NEUXMIND_MAIL_DEV_ECHO=1`；界面改动也要在 WebKit 上跑一遍（桌面应用是 WKWebView）。

## 进行中的 issue（2026-11-14 起）

| issue | 内容 | 会话 / 计划的分支 | 状态 |
|---|---|---|---|
| #8 真机真实状态页面修复 | 状态页展示全部硬件及其状态（驱动、能否正常工作）、正在运行的软件及其状态；刷新改为每分钟一次 | 总指挥启动的会话；`feat/live-status-hardware`（`PhysicalOS-live-status/`） | ⏳ 进行中 |
| #9 配合连接修改 | SSH 接入的机器人点“配置连接”时，打开与“通过 SSH 调查真机”相同的 SSH 表单，修改的是这台机器人的 SSH 连接方式（用户已确认这个理解；issue 的截图没有上传成功） | 总指挥启动的会话；`feat/ssh-connection-edit`（`PhysicalOS-ssh-config/`） | ⏳ 进行中 |
| #10 让助手修复硬件问题 | 硬件修复表单也不再设置 Token 限额和运行时间（与 #5 对调查的处理一致） | 用户启动的会话 | ⏳ 进行中 |

三个都提交后，总指挥统一合入、回归、打包、部署。基线是 `integration` = `origin/main` @ 6417e4f。

## 待办

1. #7 已由用户关闭；#5、#6 在 GitHub 上还是打开状态：由用户关闭，或由用户让总指挥关闭。
2. 集成时定的一点已随这次合并进入 main：审批框里要批准的命令默认可见（其余代码按 #7 折叠）。用户没有提出不同意见；要改回折叠只需去掉 `web/readable_dom.js` 的 `build()` 里那一行。
3. 请用户和提交者决定：没有配置邮件服务时，内置测试账号默认是开启的，它的密码写在公开的源码里（`NEUXMIND_TEST_ACCOUNT=0` 可以关掉）。
4. `fix/preexisting-test-failures` 的独立审查留下的 5 条低级别意见，已在那个会话的工作区里改好（它报告新增 15 项单元测试，沙箱测试在 Linux 上通过），**尚未提交**，等用户在那个会话里确认；提交后总指挥再合入。
5. Linux 容器里 `test_conformance_rereview04` 的一项计时类用例对宿主机负载敏感，留意是否再现。
6. 真实服务上的验证仍然没有：真实 TokenFans Key 跑调查和对话修复；真机上的调研、重新调研、修复；真实邮件服务（SMTP）发验证码。
