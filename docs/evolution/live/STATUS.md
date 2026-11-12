# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 现在的状态

**0.15.0 已合并、打包并部署到本机，等用户测试。** 测试清单见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#交给用户测试0150)。

| 来源 | 内容 | 分支 @ 提交 | 状态 |
|---|---|---|---|
| 修复 | 回环绑定不做反向 DNS；本地 Docker 云平台 | `fix/loopback-bind-skip-reverse-dns` @ b0c92d9（已推送 origin） | ✅ 已合入 `integration` |
| #3 | 跳板机接入、粘贴 ssh 命令、真机只读实时状态、慢链路超时 | `feat/ssh-jump-host-live-status` @ 2b57eb3 | ✅ 已合入；只读状态经公网跳板机在真机上验证 |
| #4 | TokenFans 模型与算力，工作空间默认模型 | `feat/tokenfans-model-compute` @ 1f01e3a | ✅ 已合入；只在替身上验证，真实 TokenFans 未验证 |
| #2 | 连接后硬件扫描与逐条审批修复（内置 Skill `hardware-init`） | `feat/hardware-scan` @ 0aead87 | ✅ 已合入；只读扫描在一台 Jetson 真机上验证，修复会话只在替身上验证 |
| 总指挥 | 文档、版本号统一与测试、0.15.0、打包、部署 | `integration` | ✅ |

## 分支与部署

- `integration`（工作区 `PhysicalOS-integration/`）：main + 以上全部。**只在本地，未推送，未合并 main。**
- 部署：`/Applications/NeuxMind Robot OS.app`（0.15.0）；云平台在本地 Docker（`http://127.0.0.1:8800`）。
- 安装包：`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.15.0-arm64.dmg`、`.pkg`。
- 预览分支 `integration-preview` 和快照引用已删除。

## 待办

1. 用户测试 0.15.0，问题记在对应 issue（#2 / #3 / #4）或告诉总指挥。
2. 各 issue 会话的后续修复以新提交的形式发给总指挥，合入 `integration` 后重新测试、打包、部署。
3. 用户确认后：合并 `integration` 到 main 并推送，关闭 issue。
4. 另一会话在 `fix/preexisting-test-failures` 上排查 main 上原有的两处测试失败（`test_adaptation` 一项、Linux 容器里的 `test_sandbox`），尚未提交。
5. 真实服务上的验证：用真实 TokenFans Key 跑一次工程任务；在真机上走一遍界面流程和修复会话。
