NeuxMind Robot OS — 产品部署与系统架构（2026-11-03）

交付：neuxmind-product-architecture.png（3400×2470 高清图）
      neuxmind-product-architecture.svg（可缩放、可编辑矢量图）

本图替代 2026-11-02 功能分层图的表达方式，按实际部署边界组织：
用户 Mac 工作台、平台云端、机器人端、外部服务与生态。
它表达完整目标架构，不表示全部模块已经实现。

蓝色边界是平台云端；绿色边界是现场机器人端；灰色是用户侧与外部系统。
箭头显示 API/任务调度、批准的能力包下发、经验数据回传及本地动作反馈。
独立执行监督处于模型输出和本体控制器之间；云端推理不承担硬实时控制。
新本体需具备适配器、兼容性检查和现场验证，不能把通用平台等同于所有硬件即插即用。

内容依据：本轮用户要求；output/robot-brain-whitepaper-20261014/index.html；
apps/robot-os/docs/ARCHITECTURE.md、ENGINEERING_ENGINE.md、USER_EXPERIENCE.md、HANDOFF.md。
开放工程运行时在用户图中使用平台自己的工程 Agent 名称；开源许可和归属信息仍需在分发物中依法保留。

复现：python3 build.py && node render.cjs
layout-check.json 保存节点内文本越界与全图文字重叠检查结果。
已渲染检查；未改产品实现、安装包、Studio、服务或集群。新增文件未提交。
