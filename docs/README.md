# 文档索引

> 由总指挥维护。新增、改名或停更文档时同步更新本页。

## 先读这几份

| 你想… | 读 |
|---|---|
| 知道项目是什么、怎么运行、怎么测试、怎么打包 | [根 README](../README.md) |
| 演示（全部仿真，不连真机、不扣费） | [DEMO.md](DEMO.md) |
| 知道现在做到哪、下一步做什么 | [ROADMAP.md](ROADMAP.md)、[evolution/live/STATUS.md](evolution/live/STATUS.md) |
| 参与开发（分工、分支、交付格式、合并流程） | [AGENT_COLLABORATION.md](AGENT_COLLABORATION.md) |

## 架构与契约

| 文档 | 内容 |
|---|---|
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | 当前目标架构：三层 + 一个契约、跨本体方式、安全原则 |
| [specs/CAPABILITIES_V1.md](specs/CAPABILITIES_V1.md) | 能力接口标准 v1（由 `spec/neuxmind_spec/capabilities.py` 生成，冲突时以源码为准） |
| [specs/INTERFACES_V1.md](specs/INTERFACES_V1.md) | 模块之间的传输接口契约（含各轮新增的 §5–§9） |
| [evolution/DECISIONS.md](evolution/DECISIONS.md) | 架构决策记录 ADR（只追加） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 0.2 时期的平台分层与证据原则；分层仍有效，具体组成以 TARGET_ARCHITECTURE 为准 |
| `product-architecture-20261103/`、`system-architecture-20261102/` | 产品与系统架构图快照（含生成脚本），不随版本更新 |

## 桌面控制台（`brainos/`、`web/`、`desktop/`）

| 文档 | 内容 |
|---|---|
| [USER_EXPERIENCE.md](USER_EXPERIENCE.md) | 用户目的、页面如何服务用户、各版本的交互约定 |
| [UI_STYLE.md](UI_STYLE.md) | 界面样式：下拉框、悬停与可读的展示内容（issue #7），表单里的提示框和折叠块（issue #14） |
| [HIDDEN_PAGES.md](HIDDEN_PAGES.md) | 暂时隐藏的页面（issue #49）：工程助手（含协作流程）、助手工具、自有服务只隐藏不删除；怎么恢复；浏览器脚本的测试开关 |
| [ENGINEERING_ENGINE.md](ENGINEERING_ENGINE.md) | 内置工程引擎（Codex app-server）、SSH 接入与本体调查、工程扩展、云网关计费、本体适配 |
| [HARDWARE_INIT.md](HARDWARE_INIT.md) | 连接后的硬件扫描、内置 Skill `hardware-init`、修复命令的审批与限制；故障、待确认和提示怎么分，旧扫描怎么重新定级（issue #47） |
| [DEVICE_PROFILE.md](DEVICE_PROFILE.md) | 设备档案：硬件、系统、ROS 2（发行版、节点、话题、动作、服务）和资源占用合成的一份记录，ROS 2 的只读命令 |
| [MOTION_DRIVER_CHECK.md](MOTION_DRIVER_CHECK.md) | 运控驱动检查：按本体类型核对应有的 ROS 2 话题和动作，缺少时作为一种硬件问题交给修复助手，修复后重新读取 ROS 2 核对 |
| [REPAIR_DRIVER_INSTALL.md](REPAIR_DRIVER_INSTALL.md) | 修复助手安装并启动运控驱动：资料库里的安装包、产品加载的 ROS 2 环境、启动前的现场确认、仍然拒绝的运动和解锁命令 |
| [JOB_RECIPES.md](JOB_RECIPES.md) | 训练与仿真任务的可视化配方协议 |
| [DEVICE_TRANSFER.md](DEVICE_TRANSFER.md) | 向设备传文件的通道：分块、压缩、续传、设备端 SHA-256 核对；允许写到哪里；失败原因一览 |
| [RUNTIME_LINK.md](RUNTIME_LINK.md) | 经 SSH 通道访问机器人上的管理层：通道只通到哪里、断开后自动重连、各种失败时的错误码、接口、测试 |
| [RUNTIME_INSTALL.md](RUNTIME_INSTALL.md) | 在控制台里安装管理层：随控制台发布的安装包、只在用户目录、断开后接着装、设备上已有管理层时怎么办、失败时留下什么和怎么清理、接口、测试 |
| [ROBOT_BODY.md](ROBOT_BODY.md) | 本体设置：让管理层接到机器人自己的 ROS 2（真实本体）、从机器人读取、应用到机器人；验证范围（没有在真机上走过这个界面）（issue #38 后续） |
| [RUNTIME_AUTOSTART.md](RUNTIME_AUTOSTART.md) | 开机自启：以登录账号运行的 systemd 服务、执行前列出要以管理员身份做的事并等人确认、sudo 密码只用一次、为什么没有开机自启、取消和卸载、接口、测试与容器里断电重启的实测 |
| [PACKAGE_DELIVERY.md](PACKAGE_DELIVERY.md) | 把资料库里的驱动安装包送到设备并核对：只送「可用」的、取和送两段各自续传、三处核对校验值、文件名换名、交给修复流程的送达记录 |
| [MATERIAL_LIBRARY.md](MATERIAL_LIBRARY.md) | 资料库：页面、桌面端后台、云平台三段怎么配合，分片上传与续传；issue #51 起按品牌型号归属，见 MODEL_LIBRARY.md |
| [MODEL_LIBRARY.md](MODEL_LIBRARY.md) | 资料库按品牌型号归属：同品牌同型号的机器人共用、旧资料怎么归过去、改一份资料的品牌型号、机器人模型（URDF，可带网格）单独一栏、接口、验证范围（issue #51） |
| [PRODUCT_NAME.md](PRODUCT_NAME.md) | 产品名称 Physical OS、应用和安装包的名字、数据目录在第一次启动时改名、登录页（issue #55） |
| [ROBOT_NO_TASK.md](ROBOT_NO_TASK.md) | 添加机器人不需要任务，也不会顺带建任务；任务在创建时绑定机器人（issue #56） |
| [BRAND_MODEL_INPUT.md](BRAND_MODEL_INPUT.md) | 品牌、型号各一个可下拉可输入的框，添加机器人和资料库上传都用它（issue #61） |
| [PAGE_TITLES.md](PAGE_TITLES.md) | 侧栏页面的标题统一按「工作台」的写法（issue #57） |
| [SIM_PROFILES.md](SIM_PROFILES.md) | 仿真机器人按类型、品牌、型号添加：内置档案、模型包怎么打、同一品牌型号的不同写法、旧仿真机器人删除一次（issue #54） |
| [MUJOCO_SIM.md](MUJOCO_SIM.md) | 用 MuJoCo 仿真机器人应用：交付清单里的「仿真环境」、工程助手写环境和人确认、仿真里有什么没有什么（不模拟步态）、结果不算现场证据、验证范围（只用编造的模型）（issue #51） |
| [MAIL_SERVICE.md](MAIL_SERVICE.md) | 验证码邮件：本机 `mail.json` 里的发信账号、`init` / `check` / `test` 命令、邮件的样子、发送失败时的提示（issue #37） |
| [CLOUD_AUTO_LOGIN.md](CLOUD_AUTO_LOGIN.md) | 登录应用时同时登录云平台：登录、注册、重置密码时一并登录或自动注册，“保持登录”传给云平台，旧版本的登录退出一次，默认云平台地址 |
| [MATERIAL_PARSING.md](MATERIAL_PARSING.md) | 资料解析：上传后自动解析（文档交给 WeKnora，网页先由云端取出文字；URDF 和安装包用规则）、四个状态、人修改和确认、设备档案里的「资料」标签、恶意文件的处理 |
| [REPAIR_MATERIALS.md](REPAIR_MATERIALS.md) | 修复会话查资料库：只给这台设备「可用」资料里人确认过的解析结果、LIBRARY_MATERIALS.json 和「资料库」事件、没有驱动资料时提示先上传、资料是参考不是指令 |
| [PLACE_SKILLS.md](PLACE_SKILLS.md) | 去指定地点、巡检、无图自由走：两项新标准能力（能力标准 1.2）、把机器人自带的导航程序接成动作、三个示例技能；验证范围（没有真机）（issue #38） |
| [CLOUD_SKILLS.md](CLOUD_SKILLS.md) | 技能在云端：技能库、程序和场景数据（资料库 `bundle` 类型）、机器人管理层向云端取调用、第三方开放接口 `/open/*`、权限范围、验证范围和已知限制（issue #43） |
| [CLOUD_SKILLS_CONSOLE.md](CLOUD_SKILLS_CONSOLE.md) | 控制台的「技能发布」「技能加载」两页，机器人页面的「远程调用」「程序和场景数据」两节（issue #43） |
| [SKILL_RUN.md](SKILL_RUN.md) | 直接运行已装技能，真机也可以：运行表单、真机运行前的现场确认和授权、`onsite_confirmation_required`、记录里写什么（issue #42） |
| [ROBOT_SCOPE.md](ROBOT_SCOPE.md) | 机器人属于账号，不属于某个任务：机器人和它的记录放在哪里、哪些接口按机器人处理、旧数据怎么迁移、退回旧版本会看到什么（issue #42 第二部分）；创建任务时选择机器人（第三部分） |
| [ROBOT_MODEL.md](ROBOT_MODEL.md) | 品牌型号：机器人的品牌型号从哪里来，机器人应用、登记的技能和云端技能版本属于一个品牌型号，现场测试证据和发布仍按台算，旧数据怎么处理，接口，验证范围（没有真机）（issue #48） |
| [TASK_WORKBENCH.md](TASK_WORKBENCH.md) | 工作台（原来叫任务工作台）：入口是任务概览（数量、名称、大小一致的卡片），一个任务的页面在 `#task`，上面有它的技能与配置；删除任务会带走什么、什么情况不能删（issue #44、#46；之后的改动见下面两行） |
| [TASK_APPS.md](TASK_APPS.md) | 任务绑定应用：应用属于账号，任务和应用多对多绑定、不在任务里开发；一个任务只用一个品牌型号的应用，任务选的机器人决定品牌型号；删除任务只解除绑定；旧数据怎么整理（issue #50） |
| [WORKBENCH_UI.md](WORKBENCH_UI.md) | 工作台页面精简：改名「工作台」、删掉的说明文字、任务卡片不显示机器人地址、「技能与配置」显示技能的运行信息（issue #52） |
| [ROBOTS_PAGE.md](ROBOTS_PAGE.md) | 「我的机器人」页面：页面跟着状态走，卡片只留下一步和实时状态，其余收进「更多功能」；以后往这个页面加内容要加在哪里；机器人的对话框共用一个框架：标题是功能、机器人名在下面，同一台机器人的对话框之间可以直接切换（issue #41）；页面上不放说明，「助手模型」「本体类型清单」在页头的「设置」里（issue #41 第三部分）；卡片只有图片、名称、在线与否、品牌型号和按钮，其余在「实时状态」里，品牌型号的两个下拉列表和图片的来源（issue #53） |
| [BODY_TYPES.md](BODY_TYPES.md) | 本体类型清单：内置四足和机械臂、添加和删除类型、每类的状态机模板标识和应有的运控接口、设备自报类型与人选类型的关系 |
| [HANDOFF.md](HANDOFF.md) | 0.2.0–0.7.1 桌面版交接记录（历史，不再更新；0.8.0 起见进化记录） |

## 各模块

| 模块 | 设计与用法 | 进度与进化条目 |
|---|---|---|
| `runtime/` 端侧运行时 | [runtime/README.md](../runtime/README.md) | `runtime/PROGRESS.md`、`runtime/EVOLUTION_ENTRY.md` |
| `cloud/` 云平台 | [cloud/README.md](../cloud/README.md)（含本地 Docker） | `cloud/PROGRESS.md`、`cloud/EVOLUTION_ENTRY.md` |
| `sdk/` Python SDK 与 `nx` | [sdk/README.md](../sdk/README.md) | `sdk/PROGRESS.md`、`sdk/EVOLUTION_ENTRY.md` |
| `brainos/` 桌面后端 | 根 README「桌面控制台」、ENGINEERING_ENGINE | `brainos/PROGRESS.md`、`brainos/EVOLUTION_ENTRY.md` |
| ROS 2 测试替身 | [runtime/tests/ros2_double/README.md](../runtime/tests/ros2_double/README.md) | — |

## 进化记录（只追加，不改写历史）

| 文档 | 内容 |
|---|---|
| [evolution/README.md](evolution/README.md) | 记录机制与格式 |
| [evolution/EVOLUTION_LOG.md](evolution/EVOLUTION_LOG.md) | 每轮一条，按时间倒序 |
| `evolution/iterations/` | 每轮的详细记录：目标、决策、分工、改动、验证、遗留 |
| `evolution/reviews/` | 独立审查报告 |
| [evolution/AUTOPILOT.md](evolution/AUTOPILOT.md) | 自主进化模式的规则与轮次队列 |
| [evolution/live/STATUS.md](evolution/live/STATUS.md) | 当前这一轮各会话的实时状态（会被覆盖，不是历史） |
