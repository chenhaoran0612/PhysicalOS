# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 现在的状态

**0.15.1（build 25）已合并、打包并部署到本机，等用户验证。** 要看的项目见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#交给用户验证0151-build-25)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| 修复 | 回环绑定不做反向 DNS；本地 Docker 云平台 | `fix/loopback-bind-skip-reverse-dns` @ b0c92d9（已推送 origin） | ✅ 已合入 |
| #3 | 跳板机接入、粘贴 ssh 命令（含嵌套写法）、真机只读实时状态 | `feat/ssh-jump-host-live-status` @ 4434176 | ✅ 已合入 |
| #3 加固 | 执行请求加时限、超长输出不采用、里层选项的说明 | 同上 @ c6b41bd（与快照 110daf3 内容相同） | ✅ 已合入 |
| #4 | TokenFans 作为唯一的工程模型来源，在“云平台账户”页配置 | `feat/tokenfans-model-compute` @ 3064d82 | ✅ 已合入 |
| #4 后续 | 冷启动在“云平台账户”页时自动刷新不启动（旧缺陷）、密码框关闭时清空、验收脚本迁移 | 同上 @ 00d07c1（与快照 ecb570b 内容相同） | ✅ 已合入 |
| #2 | 连接后硬件扫描与逐条审批修复；修复表单指向 TokenFans | `feat/hardware-scan` @ 7293784 | ✅ 已合入 |
| #2 后续 | 硬件扫描的执行请求加时限 | 同上 @ 4196529（与快照 ad2e1a6 内容相同） | ✅ 已合入 |
| 测试修复 | main 上原有的两处失败（运行时沙箱在容器里的进程数限制；桌面测试替身的 sed 写法） | `fix/preexisting-test-failures` @ 2731808 | ✅ 已合入 |
| 总指挥 | 文档、版本工具、统一回归、0.15.1 打包部署 | `integration` @ b0dccdf 及之后的文档提交 | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：旧 main（b0b6793）+ 以上全部。**已按用户要求推送到 `origin/integration`（2026-11-12）；未合并 main。** 推送前对要发布的 49 个提交（全部补丁和提交说明）扫描过真实环境信息和密钥，没有发现。
- 部署：`/Applications/NeuxMind Robot OS.app`（0.15.1 build 25）；云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.15.1-arm64.dmg`、`.pkg`（同目录还留着 0.15.0 的）。
- #2、#3、#4 的分支都已提交，内容与此前按快照合入的相同；快照引用 `refs/integration/snapshots/*` 已全部删除。自打包的提交（b0dccdf）以来，代码目录没有变化。

## main 上的新提交（未并入）

`origin/main` 在 2026-11-12 多了一个提交 212da95（其他成员提交的“账号管理与邮箱验证”：应用启动时先显示整页的登录 / 注册界面，注册要填邮件里的 6 位验证码；没有配置 SMTP 时邮件不发出，写到 `<状态目录>/mail-outbox.txt`）。

- `integration` 和已部署的 0.15.1（build 25）**不含**这个提交。
- 试合并（没有写入任何分支）：4 个共用文件有 7 处冲突（`brainos/server.py` 2、`web/app.js` 3、`web/index.html` 1、`web/style.css` 1），另外 10 个重叠文件可以自动合并。
- 这个提交把 main 上原有的 20 个浏览器验收脚本改成经 `tests/gate_helper.cjs` 登录；本轮各 issue 新增的脚本没有改，至少 `tests/ssh_jump_browser.cjs` 还在用旧的账号对话框，其余要逐个核对。
- 并入之后要重新做统一回归并打新包；`integration` 合并到 main 之前必须先做这一步。

## 待办

1. 用户验证 0.15.1，问题记在对应 issue（#2 / #3 / #4）或告诉总指挥。
2. 把 `origin/main` 的 212da95 并入 `integration`（见上一节），回归、打新包。之后用户确认，再把 `integration` 合并到 main 并推送，关闭 issue。
3. `fix/preexisting-test-failures` 的独立审查留下 5 条低级别意见（一处过时注释、对 fork 被拒原因的判断偏宽、README 措辞、两处测试缺口），在那个会话里，尚未处理。
4. Linux 容器里 `test_conformance_rereview04` 的一项计时类用例对宿主机负载敏感（见轮次记录的“已知问题”），留意是否再现。
5. 真实服务上的验证仍然没有：用真实 TokenFans Key 跑一次工程任务；在真机上走一遍界面流程和修复会话。
