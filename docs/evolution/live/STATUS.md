# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 现在的状态

**0.15.1（build 25）已合并、打包并部署到本机，等用户验证。** 要看的项目见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#交给用户验证0151-build-25)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| 修复 | 回环绑定不做反向 DNS；本地 Docker 云平台 | `fix/loopback-bind-skip-reverse-dns` @ b0c92d9（已推送 origin） | ✅ 已合入 |
| #3 | 跳板机接入、粘贴 ssh 命令（含嵌套写法）、真机只读实时状态 | `feat/ssh-jump-host-live-status` @ 4434176 | ✅ 已合入 |
| #3 加固 | 执行请求加时限、超长输出不采用、里层选项的说明 | 同上，**未提交**；快照 110daf3 | ✅ 已按快照合入 |
| #4 | TokenFans 作为唯一的工程模型来源，在“云平台账户”页配置 | `feat/tokenfans-model-compute` @ 3064d82 | ✅ 已合入 |
| #4 后续 | 冷启动在“云平台账户”页时自动刷新不启动（旧缺陷）、密码框关闭时清空、验收脚本迁移 | 同上，**未提交**；快照 ecb570b | ✅ 已按快照合入 |
| #2 | 连接后硬件扫描与逐条审批修复；修复表单指向 TokenFans | `feat/hardware-scan` @ 7293784 | ✅ 已合入 |
| #2 后续 | 硬件扫描的执行请求加时限 | 同上，**未提交**；快照 ad2e1a6 | ✅ 已按快照合入 |
| 测试修复 | main 上原有的两处失败（运行时沙箱在容器里的进程数限制；桌面测试替身的 sed 写法） | `fix/preexisting-test-failures` @ 2731808 | ✅ 已合入 |
| 总指挥 | 文档、版本工具、统一回归、0.15.1 打包部署 | `integration` @ b0dccdf 及之后的文档提交 | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：main + 以上全部。**只在本地，未推送，未合并 main。**
- 部署：`/Applications/NeuxMind Robot OS.app`（0.15.1 build 25）；云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.15.1-arm64.dmg`、`.pkg`（同目录还留着 0.15.0 的）。
- 快照引用 `refs/integration/snapshots/issue2`、`issue3`、`issue4` 保留到各分支提交为止。

## 待办

1. 用户验证 0.15.1，问题记在对应 issue（#2 / #3 / #4）或告诉总指挥。
2. #2、#3、#4 三个会话各有一批以快照合入、但在自己分支上还没提交的改动，都在等用户在各自会话里确认。确认提交后分支才与 `integration` 一致；如果用户让它们再改，总指挥需要再合一次。
3. 用户确认后：合并 `integration` 到 main 并推送，关闭 issue。
4. `fix/preexisting-test-failures` 的独立审查留下 5 条低级别意见（一处过时注释、对 fork 被拒原因的判断偏宽、README 措辞、两处测试缺口），在那个会话里，尚未处理。
5. Linux 容器里 `test_conformance_rereview04` 的一项计时类用例对宿主机负载敏感（见轮次记录的“已知问题”），留意是否再现。
6. 真实服务上的验证仍然没有：用真实 TokenFans Key 跑一次工程任务；在真机上走一遍界面流程和修复会话。
