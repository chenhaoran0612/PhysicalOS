# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 各 issue

| 来源 | 内容 | 会话 | 分支 / 工作区 | 状态 | 集成 |
|---|---|---|---|---|---|
| 修复 | 回环绑定不做反向 DNS；本地 Docker 云平台 | （上一会话） | `fix/loopback-bind-skip-reverse-dns`（已推送 origin） | ✅ 已提交 | ✅ 已合入 `integration` |
| #3 | 跳板机接入、粘贴 ssh 命令、真机只读实时状态 | PhysicalOS 代码解读 | `feat/ssh-jump-host-live-status` · `PhysicalOS/` | 🟡 功能完成，审查意见（1×P1、1×P2、P3）已修复；**等用户同意提交** | 修复后的快照已合入测试包 |
| #4 | TokenFans 模型与算力，工作空间默认模型 | PhysicalOS issue #4 | `feat/tokenfans-model-compute` · `PhysicalOS-tokenfans/` | 🟡 功能完成，审查意见已全部修复；**等用户同意提交** | 修复后的快照已合入测试包 |
| #2 | 初始化硬件扫描与修复（SKILL，默认用 #4 的模型） | PhysicalOS issue #2 | 未建分支 | 🟡 开发中 | — |
| 总指挥 | 文档核对、版本号统一与测试、0.15.0 | 合并、打包与文档维护 | `integration` · `PhysicalOS-integration/` | ✅ | — |

## 分支

- `integration`：main + 回环绑定修复与本地 Docker 云 + 文档 + 版本 0.15.0。只在本地，未推送。
- `integration-preview`：`integration` + #3、#4 的只读快照（`refs/integration/snapshots/issue3`、`issue4`）。测试包从这里构建；#3、#4 提交后改为按 commit 合入 `integration`，这个分支随后删除。

## 测试包

`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.15.0-arm64.dmg`（及同名 `.pkg`），含 #3 与 #4。测试结果与给用户的测试清单见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#交给用户测试0150-测试包)。

## 下一步

1. 用户在 #3、#4 会话里同意提交 → 两个会话提交到各自分支，并把 commit 发给总指挥。
2. 总指挥按 commit 合入 `integration` → 复测 → 重新打包（如代码与快照不同）。
3. 用户测试通过 → 合并 `integration` 到 main，推送（需用户同意）。
4. #2 交付后同样流程，另出测试包。
