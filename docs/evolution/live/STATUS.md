# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-14

## 现在的状态

**0.18.0（build 28）已合并、回归、打包并部署到本机（2026-11-14 05:32）；用户验证后确认，`integration` 已合并到 main（快进）并推送（2026-11-14）。** 它是 0.17.0 加上 #8、#9、#10。记录和要检查的项目见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#细节调整的-issue8910-0180-build-28)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| 0.17.0 | 第 08 轮的 #2–#7、账号管理等 | 6417e4f | ✅ 已在 main |
| #8 | 状态页列出全部硬件和正在运行的软件，每分钟读取一次 | `feat/live-status-hardware` @ e8012a8（合并提交 800a930） | ✅ 已在 main |
| #10 | 硬件修复表单不再设置 Token 限额和运行时间 | `feat/repair-no-limits` @ 3549abe（合并提交 07a99fb） | ✅ 已在 main |
| #9 | SSH 机器人的“配置连接”打开 SSH 表单，原地修改这台机器人的连接 | `feat/ssh-connection-edit` @ b6c57d2（合并提交 91c668d） | ✅ 已在 main |
| 总指挥 | 文档、版本 0.18.0（build 28）、统一回归、打包、部署 | `integration` @ bf96566 及之后的文档提交 | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：包含以上全部。**已在用户验证并确认后合并到 main（快进，2026-11-14）并推送**，`origin/main` 与 `origin/integration` 指向同一个提交。之后的 issue 仍然先合到 `integration`，统一回归后由用户确认再合并到 main。
- 部署：`/Applications/NeuxMind Robot OS.app` 是 0.18.0（build 28），2026-11-14 05:32 替换并启动；后端报告版本 0.18.0，只监听回环地址。部署前的数据目录备份在 `~/Library/Application Support/NeuxMind Robot OS-backup-20261114-053212`。云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.18.0-arm64.dmg`（SHA-256 `700e83de20b4eab9dbf6648b9a0ea25a68c74a4dec31ff2ea58c18bbedc0c4a6`）和同名 `.pkg`（`acf17702e510e6aba7270489940d19cd1683166ec105da73f3fd6ae140ec1de6`）；0.17.0、0.16.0、0.15.1、0.15.0 的也还在同一目录。
- 新的 issue 分支请基于现在的 `integration`。浏览器验收经 `tests/gate_helper.cjs` 登录，测试服务端带 `NEUXMIND_MAIL_DEV_ECHO=1`；界面改动也要在 WebKit 上跑一遍（桌面应用是 WKWebView）。

## 待办

1. 0.18.0 已在 main。之后的细节调整另提 issue，或在原 issue 分支上追加提交；总指挥重新合并、回归、打包、部署，确认后再合并到 main。
2. GitHub 上 #5、#6、#8、#9、#10 还是打开状态（#7 已由用户关闭）：由用户关闭，或由用户让总指挥关闭。
3. 请用户和提交者决定：没有配置邮件服务时，内置测试账号默认是开启的，它的密码写在公开的源码里（`NEUXMIND_TEST_ACCOUNT=0` 可以关掉）。
4. `fix/preexisting-test-failures` 的独立审查留下的 5 条低级别意见，已在那个会话的工作区里改好（它报告新增 15 项单元测试，沙箱测试在 Linux 上通过），**尚未提交**，等用户在那个会话里确认；提交后总指挥再合入。
5. 各 issue 留下的小改动候选：`systemctl` 类只读命令统一加设备端时限（#8 提出）；硬件扫描与修改连接共用一把锁（#9 提出）；状态页的硬件扫描登记到 `HardwareInit.scanning`（#8 提出）。都没有排期，需要的话另提 issue。
6. Linux 容器里 `test_conformance_rereview04` 的一项计时类用例对宿主机负载敏感，留意是否再现。
7. 真实服务上的验证仍然没有：真实 TokenFans Key 跑调查、对话修复和硬件修复；真机上的调研、重新调研、修复、修改连接和状态页；真实邮件服务（SMTP）发验证码。
