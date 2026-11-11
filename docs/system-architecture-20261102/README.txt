NeuxMind Robot OS — 产品完整系统架构
日期：2026-11-02；现有产品实现基线：v0.6.3。

交付文件
neuxmind-system-architecture.svg：可缩放、可打印的完整矢量图。
neuxmind-system-architecture.png：2200 × 2580 的图片版。
neuxmind-system-architecture.mmd：便于二次编辑的 Mermaid 语义总览（较主图简化）。
architecture-modules.json：33 个主模块的名称、职责与实现状态；治理、RSI、执行监督和契约在图中另外展开。
build_diagram.py / render.cjs：生成与截图脚本。
layout-check.json：主模块文字边界检查结果。

架构定位
这是完整目标架构，并叠加当前实现状态，不是已经商用上线的系统清单。
Mac 是产品入口与本地工程环境；平台云提供共享模型、算力、商业服务与治理；机器人端完成本地执行闭环。
当前 v0.6.3 的业务控制面主要在 Mac 本地。框架迁移到云服务、生产租户体系、支付和资源调度均仍需建设。
训练与仿真按需调用，可以直接由作业控制面调度，不要求每次经过 Agent，也不要求所有机器人任务都重新训练。
所有平台算力都是可配置供给，不绑定某台机器、8 张卡或任何供应商。

状态口径
已有：源码具备本地实现，不表示所有功能已在真实模型、真实机器人上验收。
框架：已有局部实现、资产登记、协议或通用网关，完整后端/厂商实现仍需接入。
规划：白皮书或本次产品设计中的目标能力，尚未成为当前完整产品功能。
外部：厂商控制器、机器人硬件等外部系统，按已验证能力连接。

关键边界
工程 Agent 生成代码与测试；端侧监督器管理动作权限、时效和设备执行。
工程 Skills/MCP 与机器人端技能是不同资产；生成工程文件不等于技能已经安装。
云端教师帮助生产与纠正训练数据，不成为本地实时控制的必需依赖。
RSI 同时研究任务能力及改进器自身，但不能修改冻结验收、权限、预算或自行批准生产。
任务、模型和本体必须经过兼容性检查与现场证据验证；支持多种形态不等于任意硬件无条件即插即用。

核对来源（相对于仓库根目录）
output/robot-brain-whitepaper-20261014/index.html — 白皮书 v1.2，2026-11-01 扩展系统框架与 RSI。
output/robot-brain-whitepaper-20261014/framework.py — 八个子系统及系统总览的设计内容。
apps/robot-os/docs/ARCHITECTURE.md — 产品边界、分层、契约和商业平台规划。
apps/robot-os/docs/ENGINEERING_ENGINE.md — 工程运行时、SSH、Skills/MCP 与实际能力边界。
apps/robot-os/docs/HANDOFF.md — 截至 v0.6.3 的实现与历史验证记录。
apps/robot-os/brainos/*.py、web/*.js、desktop/ — 本次只读核对的实现。

此次只创建架构图及生成源文件；未修改产品运行代码、白皮书、安装包或展示服务。
验证：主图 33 个模块文字无边界溢出；PNG 已目视检查；Mermaid 为语义编辑源，本轮未运行 Mermaid 渲染器。
源码分支 codex/rigid-articulation，产物未提交。
