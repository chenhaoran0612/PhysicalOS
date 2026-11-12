# 实时进度 · 第 08 轮（issue 驱动）

> 总指挥（「合并、打包与文档维护」会话）维护，每次合并或打包后更新。本文件会被覆盖，历史见 [iterations/2026-11-12-iter08.md](../iterations/2026-11-12-iter08.md)。
> 更新：2026-11-12

## 各 issue

| 来源 | 内容 | 会话 | 分支 / 工作区 | 状态 | 集成 |
|---|---|---|---|---|---|
| 修复 | 回环绑定不做反向 DNS；本地 Docker 云平台 | （上一会话） | `fix/loopback-bind-skip-reverse-dns`（已推送 origin） | ✅ 已提交 | ✅ 已合入 `integration` |
| #3 | 跳板机接入、粘贴 ssh 命令、真机只读实时状态 | PhysicalOS 代码解读 | `feat/ssh-jump-host-live-status` · `PhysicalOS/` | ✅ 已提交 1bf4b14；真机只读状态已验证；真机暴露的超时修复**待用户同意提交** | ✅ 1bf4b14 已合入 `integration`；超时修复快照在测试包里 |
| #4 | TokenFans 模型与算力，工作空间默认模型 | PhysicalOS issue #4 | `feat/tokenfans-model-compute` · `PhysicalOS-tokenfans/` | ✅ 已提交 1f01e3a（审查意见已全部修复） | ✅ 已合入 `integration` |
| #2 | 初始化硬件扫描与修复（SKILL，默认用 #4 的模型） | PhysicalOS issue #2 | `feat/hardware-scan` · `PhysicalOS-hardware/` | 🟡 功能完成；扫描已在一台 Jetson 真机上验证（只读）；需更新 #3 的浏览器验收；独立审查 1×P1（禁止列表有缺口）、4×P2 待修；**等用户同意提交** | 快照已合入测试包 |
| 总指挥 | 文档核对、版本号统一与测试、0.15.0 | 合并、打包与文档维护 | `integration` · `PhysicalOS-integration/` | ✅ | — |

## 分支

- `integration`：main + 回环绑定修复与本地 Docker 云 + 文档 + 版本 0.15.0 + #3（1bf4b14）+ #4（1f01e3a）。只在本地，未推送。
- `integration-preview`：`integration` + #2、#4 的只读快照 + #3 超时修复的快照（`refs/integration/snapshots/issue2`、`issue3`、`issue4`）+ 对 #3 浏览器验收的临时调整。**永不推送**（旧快照里有一份含真实地址的测试文件）。测试包从这里构建；#3、#4 提交后改为按 commit 合入 `integration`，这个分支随后删除。

## 测试包

`PhysicalOS-integration/dist/NeuxMind-Robot-OS-0.15.0-arm64.dmg`（及同名 `.pkg`），含 #2、#3、#4。测试结果与给用户的测试清单见 [第 08 轮记录](../iterations/2026-11-12-iter08.md#交给用户测试0150-测试包)。

## 下一步

1. 用户在 #2、#3（超时修复）会话里同意提交 → 各会话提交到自己的分支，并把 commit 发给总指挥。用户要求等所有 issue 都处理完再统一测试。
2. 总指挥按 commit 合入 `integration` → 复测 → 重新打包（如代码与快照不同）。
3. 用户测试通过 → 合并 `integration` 到 main，推送（需用户同意）。
4. 删除 `integration-preview` 和 `refs/integration/snapshots/*`。
