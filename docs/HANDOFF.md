# macOS 桌面交付 — 2026-10-15

> **历史文档，不再更新。** 记录 0.2.0–0.7.1 桌面版的交接（当时代码位于另一个仓库的 `apps/robot-os/`、分支 `codex/rigid-articulation`）。0.8.0 起的变更、验证和遗留见 [evolution/EVOLUTION_LOG.md](evolution/EVOLUTION_LOG.md)，当前状态见 [ROADMAP.md](ROADMAP.md)。

分支：`codex/rigid-articulation`。本任务代码均位于 `apps/robot-os/`，尚未提交；仓库原有大量修改未触碰。白皮书依据为 `output/robot-brain-whitepaper-20261014/index.html`。现有 Studio、集群、隧道和展示服务未修改。

## 当前产物

- `dist/NeuxMind-Robot-OS-0.2.0-arm64.dmg`：约 10 MB，拖入 Applications 安装。
- `dist/NeuxMind-Robot-OS-0.2.0-arm64.pkg`：固定安装到 /Applications，关闭 bundle relocation。
- `dist/SHA256SUMS.txt`：两种安装包摘要。
- `dist/NeuxMind Robot OS.app`：本机已启动，原生宿主自动启动其内部独立后端。
- `artifacts/mac-workbench.png`、`mac-workspace-verified.png`、`mobile-layout.png`：浏览器验收截图。

构建平台为 Apple Silicon / macOS 26.3，目标 macOS 13+。内置 Python 动态库最低系统版本为 11.0，宿主目标 13.0；仅在本机系统实测，其他系统版本未验证。采用 ad-hoc 本地签名，未完成 Developer ID 签名与 Apple 公证。Windows 未处理。

## 本轮验证

- `python3 -m unittest discover -s tests -v`：20 项通过，8.685 秒。
- `node --check web/app.js` 与浏览器验收脚本语法检查通过。
- Playwright：13 个导航页面加载；注册、项目、本体协议连接、技能、应用、Harness、现场测试协议、部署、启动、停止流程通过；无 JavaScript 异常。仅使用测试替身，不是真实机器人验收。
- Cocoa / WKWebView 打包应用成功启动；通过原生辅助功能树和截图确认加载实际页面。后端进程路径来自应用 Resources/backend，不依赖系统 Python。
- `hdiutil verify` 通过；只读挂载最终 DMG 后 `codesign --verify --deep --strict` 通过。
- `pkgutil --expand-full` 解包 PKG，签名校验通过；PackageInfo 固定 /Applications 且 relocatable=false。
- 未以 root 安装 PKG，未测试 Intel Mac、Windows、外部下载后的 Gatekeeper 放行或真实支付。

打包期间发现 Desktop/iCloud 的 FinderInfo 扩展属性污染复制后的 .app。最初磁盘镜像校验未通过；现已改用系统临时目录暂存、清除生成物扩展属性，并在构建脚本中加入最终 DMG 挂载签名检查。PKG 验证的第一次检查错误地把空 `<relocate/>` 当作启用迁移；改为读取 XML `relocatable=false` 属性后通过，安装配置本身正确。

## 构建与复验

构建解释器：`.build/packaging-venv/bin/python`，Python 3.12 / PyInstaller 6.22.3。

```sh
.build/packaging-venv/bin/python scripts/build_macos.py
python3 -m unittest discover -s tests -v
node --check web/app.js
```

浏览器验收需独立 QA 后端和 `tests/browser_fixture.py`（`PYTHONPATH=.`）打印的测试设备地址；设置 `BRAIN_QA_URL`、`BRAIN_QA_DEVICE`。Playwright 和 Chromium 可通过 `BRAIN_PLAYWRIGHT_MODULE` 与 `BRAIN_CHROMIUM_PATH` 指定，默认使用安装的 playwright。协议测试端口与临时 QA 后端已停止。用于开发预览的 8788 服务与原生应用保留。

## 已实现和剩余边界

客户端覆盖项目、本体、模型目录、技能应用、Agent、训练、仿真场景与任务、Harness、部署运行、数据、资源和账单。服务端实现本地账户隔离、平台资源与模型目录、预算预留/结算、持久化，以及机器人/执行器协议。

当前是可运行框架，不是完整商用机器人操作系统。真实训练器、仿真引擎、LLM Agent 执行器、模型权重/网关、厂商驱动、3D 编辑器、支付服务和托管 SaaS 未集成。内置 Agent 是确定性工程检查；作业调用接口不代表训练或仿真已经完成。下一步按 `ARCHITECTURE.md` 接入实际执行服务与端侧能力。用户未确定首个本体，继续建设通用平台；算力和模型由平台供应，不绑定用户个人机器。

## 0.2.1：固定侧栏头尾

按用户反馈修正侧栏：品牌、工作空间、导航标题和底部状态区不收缩、不参与滚动；只有导航菜单容器弹性占满剩余高度并独立滚动，阻止菜单滚动传递至主页面。保留所有导航条目的完整高度与可达性。

通过浏览器实测 1440×930、900×640、390×844：滚动菜单及主页面前后头尾坐标不变，侧栏自身 scrollTop=0，菜单末项可见，底部未越过视口。截图：`artifacts/sidebar-fixed-900x640.png`。此项为布局修复，未重复运行无关后端测试。桌面版本升级为 0.2.1 / build 3，安装包同版本重新生成。

0.2.1 安装包构建完成，DMG 挂载后的签名校验通过。Cocoa/WKWebView 内刷新后确认头尾固定、中间导航单独滚动。为消除 File Provider 签名竞态，整个 .app 编译、签名和打包过程均在系统临时目录完成，最后复制本地预览应用。

## 0.2.2：移除重复工作空间按钮

按用户反馈删除侧栏品牌下方的“你的工作空间”固定按钮，以及对应的渲染、无入口切换弹窗和样式。项目仍通过现有项目工作台卡片选择；账号入口保留在顶栏。导航标题与品牌留 32px 间距，中间菜单获得更多可用高度。

前端语法检查通过；1440×930、900×640、390×844 均确认按钮不存在、13 个导航入口保留、头尾固定、底部未超出视口，无 JavaScript 异常。截图：`artifacts/sidebar-without-workspace.png`。桌面版本 0.2.2 / build 4。

0.2.2 的 DMG/PKG 已生成，DMG 挂载签名校验通过。当前桌面应用刷新后，原生辅助功能树确认品牌下方已直接进入导航、重复工作空间按钮消失。

## 0.2.3：固定整个应用框架

根页面禁止滚动及边缘回弹；右侧 shell 固定为窗口高度，topbar 不收缩；新增 content-scroll 独立承载 main 与页脚的滚动。左侧导航仍独立滚动，左右容器均禁用滚动传递和边缘回弹。路由切换改为重置右侧内容容器，而非 window。

JS 语法检查通过。Chromium 滚轮验证 1440×930、900×640：右侧内容可滚动，左侧各固定区域与 topbar 坐标完全不变，window/html/body 滚动值为零；导航滚动不影响右侧；切换路由重置内容位置。390×844 的第一次检查在侧栏展开动画未结束时读取坐标，产生误报；等待 CSS 动画完成后复验通过，主内容 scrollTop 保持 734，菜单自身从 0 滚动到 30，所有固定区域坐标不变。截图 `artifacts/fixed-app-frame-900x640.png`。

版本升级为 0.2.3 / build 5；此轮未修改业务后端逻辑。

0.2.3 DMG/PKG 已生成，最终 DMG 挂载签名检查通过。当前 WKWebView 原生窗口刷新后滚动右侧至底部，截图确认顶部面包屑/账号导航保留、左侧品牌/菜单/状态区位置稳定；根文档不再具有滚动条。

## 0.3.0：内置工程运行时与 Agent 自主本体接入

当前交付版本升级为 0.3.0 / build 6。上文“LLM Agent 运行时未集成”仅适用于 0.2.x，现在由本节与 ENGINEERING_ENGINE.md 更新。

新增 brainos/agent_runtime.py、engineering.py、device_access.py 和 web/engineering.js，接入固定版本的真实 Codex app-server 0.146.1。产品品牌统一为 NeuxMind，Apache 2.0/NOTICE 和 SSH 依赖许可随安装包保留，并有原生菜单入口。来源、归档完整性和二进制摘要见 runtime-lock.json。

实现 SSH 指纹检查、密码/私钥/SSH Agent 认证、受控只读探针与指定目录配置读取；Agent 选择工具、保存证据、形成归属本体的版本化软硬件档案。工程模式读取项目档案，原生执行本地命令、修改文件、运行测试，记录产物/差异/计划。实现持续会话、恢复、停止、问题、审批队列和运行时协作事件。调查与工程代码都不自动赋予实机动作或部署资格。

用户可添加自有 Responses 工具调用 API；平台可上架 reasoning 模型网关，按千 Token 预留额度，经受限会话令牌执行并在关停后依据网关证据结算。网关响应不明时保留预留，标记待核对。尚未部署真实供应商网关或支付渠道，未消耗付费 Token。当前本机凭据依赖文件权限，未接系统钥匙串。Skills/MCP 管理、浏览器/桌面控制、云端执行、专有连接方式和产品级角色编排未完成；不可把携带上游二进制描述为全部功能均已产品化。

验证：

- 原有产品/商业协议 20 项通过；新增工程集成 10 项通过（最后一次新增测试运行 3.536 秒）。实际使用随包原生运行时、本地 Responses SSE 及本地 SSH 协议替身，覆盖档案、命令产物、恢复/停止、并发请求队列、预算预留/结算；未使用真实机器人或在线模型。
- tests/engineering_browser.cjs 完成注册、项目、模型、SSH 指纹、调查、档案、证据、工程任务和产物预览；13 个导航页无 JS 异常，900×640 滚动时左右导航固定。
- tests/frozen_engine_smoke.py 针对实际 .app 内冻结后端，通过 HTTP API 完成 SSH → 内置引擎 → 保存档案，证明 SSH 动态库和运行时随包可用。
- 编译、ad-hoc 签名和最终 DMG 挂载签名检查通过。PKG 解包签名通过，固定 /Applications、relocatable=false。Intel Mac、其他 macOS 版本、真实付费调用与实机部署未验证。
- 截图：artifacts/agent-discovery-session.png、robot-inventory.png、agent-engineering-session.png、agent-studio-900x640.png。测试数据只存在于测试临时工作空间。

产物：dist/NeuxMind-Robot-OS-0.3.0-arm64.dmg（约 142 MB）、同版本 .pkg（约 129 MB）、SHA256SUMS-0.3.0.txt。体积增加来自打包原生工程运行时。仍为本地签名的开发预览，尚未 Developer ID 签名/公证。

构建使用 .build/packaging-venv/bin/python；依赖固定在 requirements-runtime.txt。先运行 scripts/prepare_agent_runtime.py，再运行 scripts/build_macos.py。工程测试命令：.build/packaging-venv/bin/python -m unittest discover -s tests -p test_engineering.py -v。源码未提交，没有生成提交 SHA。

最终复验：0.3.0 Cocoa/WKWebView 宿主已重启，新后端显示 0.3，原生辅助功能树确认“SSH 接入并调查”“从连接信息到本体档案”等新界面。最终 DMG 与 PKG 重新生成，PKG 解包后签名复验通过，摘要已更新。开发预览 8788 已重启为新后端，原生应用保留打开；仅测试 fixture 服务已停止。

## 0.4.0：工程 Skills 与 HTTP MCP

本轮继续建设内置工程执行能力，增加“工程扩展”页面及 brainos/extensions.py、web/extensions.js。版本为 0.4.0 / build 7，固定运行时仍为 0.146.1。工作区/分支未变化，代码仍未提交，仅修改 apps/robot-os。

- Skill：创建、导入单个 SKILL.md 或 JSON 文本包、内容预览、导出、发布不可变版本、停用。辅助脚本/资料作为文本文件保留；导入不执行脚本。原生解析器校验元数据，路径与大小受限。
- 工程任务明确选择 Skill 与 MCP，快照固定版本/摘要。通过原生 skill 输入与目录加载，自动发现的未选择 Skills 禁用；不复用用户个人配置。旧会话续接不自动升级版本。
- MCP：Streamable HTTP + 可选 Bearer Token；原生工具发现、工具允许列表、逐次调用审批、调用结果事件。凭据不进入配置文件、普通快照、项目文件或模型输入。连接检查不调用模型、不执行服务工具。
- 停用扩展或收回工具权限会中断相关会话；续接检查权限。连接检查与权限修改并发时保留最新撤销结果，修复了旧检查结果可能覆盖停用状态的问题。工具定义变化通过摘要检测，任务启动时阻止未经复核的变化。
- MCP 资源读取是独立能力，界面明确说明工具列表不限制资源读取。普通 MCP 表单、OAuth、本地 stdio 进程、插件市场仍未开放，扩展不会绕过 SSH 调查的只读限制。

验证与产物：

- 全套 35 项测试通过（15.693 秒）；收尾新增并发撤权用例后，受影响的 6 项扩展测试全部通过（5.401 秒），累计 36 个测试用例。原生运行时实际执行 Skill 加载及 MCP 调用，拒绝审批时无工具执行，撤权时停止，旧 Skill 版本保持。
- tests/extensions_browser.cjs：创建 Skill、导出、导入新版本、配置 MCP、发现与选择工具、任务选择扩展、审批、完成结果，14 个页面无 JS 异常，900×640 应用导航固定。
- tests/frozen_engine_smoke.py：实际安装包冻结后端中完成 SSH 调查、Skill 创建/加载、MCP 发现/调用审批与档案保存。使用本机协议替身，不是线上模型或实机验收。
- 截图：artifacts/engineering-extensions.png、mcp-tool-approval.png、extensions-agent-session.png、extensions-900x640.png。
- 安装包：dist/NeuxMind-Robot-OS-0.4.0-arm64.dmg、同版本 .pkg、SHA256SUMS-0.4.0.txt。本地签名开发预览，Developer ID/Apple 公证未完成。

复验命令：.build/packaging-venv/bin/python -m unittest discover -s tests -v；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/frozen_engine_smoke.py。UI fixture 为 tests/engineering_browser_fixture.py，浏览器脚本使用其生成的 /tmp/neuxmind-engineering-qa.json；无真实凭据或收费模型。

最终安装包复验通过：冻结后端报告 skills_and_mcp=true、call_approval=true、inventory_saved=true；最终 PKG 解包后的深层签名校验通过，DMG 挂载签名校验通过，摘要已更新。0.4.0 原生应用已重启并打开“工程扩展”页面，开发预览 8788 同步更新。测试 fixture 服务已停止，用户数据目录未写入测试资产。

## 0.5.0：多角色工程流程与逐阶段交付

2026-10-15，分支 codex/rigid-articulation，源码未提交。本轮只修改 apps/robot-os；没有启动其他开发 Agent、访问集群、修改展示服务或使用用户付费模型。

新增 brainos/workflows.py 与 web/workflows.js。Agent Studio → 协作流程支持 1–8 个串行阶段，自定义角色/任务/模型/Skills/MCP/交付路径/预算，可调整顺序。默认模板包含本体分析、适配器开发、Harness 和交付审查。每阶段创建独立工程会话；生成 HANDOFF.json 和文件后检查契约、成功命令证据，保存带摘要的交付快照。用户通过验收后才启动下一阶段，上游快照复制到新工作目录。停止、暂停验收、退回、带反馈重试、导出记录均已接通。

总 Token 分配上限在尝试启动前扣减，每阶段最多三次尝试；重试不自动发生、不自动增加预算。已报告用量和费用结算另行记录，不能将分配预算描述成 BYOK 服务的硬费用上限。应用重启后中断的流程不会自动重放，工作流会话禁止从普通续接入口绕过阶段控制。平台计量待核对时阻断交付。成功命令证据不等于测试内容正确，也不授予机器人部署或动作资格。

发现并修复：原生 commandExecution 的 aggregatedOutput 可能为 null，初次回归因此失败，改为可空处理后相关测试通过；通用 select 的 required 阻止了“不关联特定本体”的表单提交，已对可选本体字段移除；产物自动收集排除隐藏目录、node_modules 和 Python 缓存，避免正常测试缓存阻塞交付。浏览器首次快照断言读取了尚未替换的旧弹窗，改为等待目标内容后通过。原生检查中修正未登录状态错误提示引擎缺失的问题。

验证：

- 全套 42 项通过，20.721 秒。之后补充快照篡改用例和缓存过滤，受影响的 7 项流程测试全部通过，16.791 秒；共 43 个唯一测试用例。
- tests/workflows_browser.cjs：创建、移除/重排阶段、启动、原生命令、产物预览、暂停/恢复验收、两阶段交接与完成；无 JS 异常。900×640 的左右导航与窗口滚动位置固定。四个前端脚本语法检查通过。
- tests/frozen_engine_smoke.py 已扩展并通过，实际 .app 冻结后端中完成 SSH 调查、Skill/MCP 原生加载/审批、两阶段独立会话及上游快照交接。使用本机 Responses/MCP/SSH 协议替身，不是付费在线模型或真实机器人。
- 截图：artifacts/workflow-editor.png、workflow-stage-review.png、workflow-completed.png、workflows-900x640.png。
- 版本 0.5.0 / build 8。DMG 挂载签名校验和 PKG 解包深层签名检查通过；最终包更新后复验结果追加于下方。

复验命令：.build/packaging-venv/bin/python -m unittest discover -s tests -v；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/frozen_engine_smoke.py；浏览器使用 tests/engineering_browser_fixture.py 及 tests/workflows_browser.cjs。流程契约、预算定义和限制见 ENGINEERING_ENGINE.md 的 0.5.0 节。

下一步建议是工程产物到可安装本体适配器的发布与验证链路，或远程隔离执行服务接入；需要保持已有协议与实机验收边界。并行/分支编排、流程定义修订 UI、浏览器工具、真实训练/仿真后端和付费供应商网关尚未完成。当前安装包仍为本地签名开发预览，没有 Developer ID / Apple 公证。

0.5.0 最终冻结后端、SSH、Skills/MCP 和两阶段协作复验通过，DMG 挂载与 PKG 解包签名通过。之后用户明确要求从使用者角度简化所有设计，本轮继续完成下面的 0.5.1；最终交付以 0.5.1 为准。

## 0.5.1：从功能目录转为用户任务路径

用户强调系统目的、如何使用和用来做什么必须简单明了。本轮据此调整入口与信息结构，保留 0.5.0 的实际能力，不把新的导航和说明当成后端功能完成。

- 新增 web/journey.js，首页围绕“描述任务 → 连接机器人 → 准备能力 → 验证效果 → 部署使用”。保存目标后展示当前缺项和下一步按钮；首次注册可继续填写任务，不丢失开始意图。
- 导航改为任务路径、模型与数据、工具与账户三组。15 个页面明确用途；功能页可展开“需要准备 / 完成后得到”。左侧头尾和右侧顶栏保持固定。
- 工程助手主界面改为了解设备、开发测试和角色协作，运行时实现信息移出主操作区域。简化准备入口沿用项目目标和目标机器人，生成四阶段计划；必须查看计划并授权后才调用模型。
- 训练、模型和仿真作为任务按需使用的能力；平台资源按入口保留训练/仿真分类，模型来源按钮可实际筛选。未上架、未开通支付等状态改为用户可理解的说明。
- 首页按当前应用摘要、机器人身份和本体摘要匹配验证报告，最近失败记录不被旧成功掩盖；发布记录还需对应匹配的报告。不将生成代码、发布应用或发出启动请求显示为机器人已经完成工作。
- 详细设计约定保存在 docs/USER_EXPERIENCE.md。深层训练/仿真表单仍依赖执行器配置，工程到实机安装和应用注册仍需接通，后续继续简化，不宣称整个平台商业体验已经完成。

验证与交付：

- tests/journey_browser.cjs 通过：首次进入、注册衔接、任务目标、连接选择、准备计划、15 页面用途说明；验证生成计划未启动模型调用；旧版本证据、旧发布报告和后续失败不会误报进度。900×640 与 390×844 无横向溢出，应用导航固定，无 JS 异常。
- tests/workflows_browser.cjs 在新入口下复验通过，保留两角色实际执行、快照交接、暂停/恢复与逐阶段验收。现有浏览器脚本的新建入口更新为 journey-start。初次旧脚本因 project-add 入口已改名而失败，更新真实用户入口后通过。
- 首次 journey 浏览器测试未等注册后表单切换即填写，导致必填项未完成；改为等待具体 form 后通过。页面用途定位遇到另一个高级设置 details，收紧到用途区后通过。没有通过放宽业务规则掩盖失败。
- 0.5.1 实际冻结后端 smoke 全部通过：role_workflow/review_gates/upstream_snapshot/skills_and_mcp/call_approval/packaged_ssh/embedded_agent/inventory_saved 均为 true。仍使用本机协议替身，无付费模型或真实机器人。
- 0.5.1 / build 9 的 DMG 挂载签名及 PKG 解包深层签名通过，SHA256SUMS-0.5.1.txt 已生成。原生 Cocoa/WKWebView 重启后确认新首页、中文导航分组与任务入口加载成功。
- 新截图：artifacts/user-journey-welcome.png、user-journey-task.png、user-journey-900x640.png、user-journey-390x844.png。
- 仅更新 apps/robot-os，源码仍未提交。原生应用与 8788 开发预览保留，临时 QA 服务停止；用户实际工作空间未写入测试资产。

## 0.6.0：训练与仿真的引导配置和可读结果

2026-10-15，分支 codex/rigid-articulation，源码未提交。本轮只修改 apps/robot-os；未访问集群、真实机器人或收费模型，未修改受保护展示服务。

- 新增 brainos/recipes.py 与 web/planner.js。执行服务可在 manifest 中声明可选 job_recipes，界面据此生成“选择服务与任务类型 → 填写参数 → 确认预算并启动”流程。训练、仿真及其他执行任务共用入口；保留未声明配方服务的高级 JSON 配置，不推测服务不支持的参数。
- 支持文本、数字、整数、选择、布尔及当前项目资产字段；沿用项目目标、隐藏高级默认参数，缺少数据/场景等资产时说明准备步骤。返回修改保留输入。任务详情直接展示状态、产物、指标、日志和费用，技术记录收起。
- POST /api/jobs/preview 只生成权威配置和预算预览，不创建任务、不预留余额、不执行。确认摘要覆盖资产快照、配方、价格、参数和服务地址；变更后须重新预览。启动前再次校验服务地址和配方，防止旧确认发往新服务；创建结果不明时界面阻止立即重复确认。资产快照限定字段，不复制凭据。
- 契约及限制见 docs/JOB_RECIPES.md，用户路径更新于 docs/USER_EXPERIENCE.md。服务需要实际接入并声明配方才能使用简化表单。本轮未实现真实训练引擎、仿真运行时、支付或商业云网关；用户手动刷新执行状态，未新增自动轮询。

验证与交付：

- 全套 50 项测试通过，25.518 秒（/tmp/neuxmind-060-tests.log）。之后补充执行地址固定及输入校验，受影响的 8 项 recipe 测试全部通过，4.200 秒（/tmp/neuxmind-recipes-final.log）；共 51 个唯一用例，未声称最后一次完整运行 51 项。前端 app.js / planner.js 语法检查通过。
- tests/planner_browser.cjs 通过：无服务与缺资产提示、返回保留配置、预览无副作用、托管训练启动/结果/结算、自有仿真场景与次数、无配方高级入口。900×640 导航固定，零 JS 异常。训练和仿真均为本机 HTTP 协议替身，无实际模型训练或物理仿真。
- tests/frozen_engine_smoke.py 在最终 .app 内通过：配方预览与执行、SSH、Skills/MCP 调用审批、两角色流程、验收门及上游交付快照。日志 /tmp/neuxmind-060-frozen.log，版本 0.6.0。
- 安装包 0.6.0 / build 10：dist/NeuxMind-Robot-OS-0.6.0-arm64.dmg、同版本 .pkg 和 SHA256SUMS-0.6.0.txt。DMG 挂载签名、PKG 解包深层签名通过，包内前端文件与源码一致。Apple Silicon macOS 13+，仍为本地签名开发预览，未做 Developer ID / Apple 公证。
- 原生 Cocoa/WKWebView 已重启并确认 0.6 页面及“新建训练任务”入口。截图 artifacts/task-service-empty.png、training-simple-form.png、training-budget-review.png、training-result-readable.png。
- 收尾保留原生应用与 8788 开发预览，停止本轮临时 QA 服务；用户实际工作空间未写入测试资产。

## 0.6.1：从训练结果到可使用的模型

2026-10-15，分支 codex/rigid-articulation，源码未提交。仅修改 apps/robot-os，保留其他任务的未提交文件；未访问集群或真实机器人，没有使用付费模型。

本轮验收里程碑：成功训练任务返回 checkpoint 地址后，用户可以保存模型产物、查看来源和指标、配置已加载产物的推理服务并进行调用测试。原始产物与可调用配置分别保存，不把检查点登记当成已可部署模型。真实权重加载、端侧打包、真实训练/仿真和支付不在本轮范围。

- 新增 brainos/results.py 和 web/results.js；jobs/model-preview 只读预览、jobs/save-model 确认保存。只接受同项目成功训练任务的字符串检查点，保存前校验来源摘要。同任务同检查点确定性 ID + 工作空间锁保证并发/重复保存幂等，保留首次保存的指标；新检查点创建新资产。
- 训练产物包含执行时资产白名单快照、参数摘要、配方引用、来源任务及指标。没有服务凭据，不下载权重，不自动绑定服务/安装能力；训练产物直接 infer 被服务端拒绝。models 支持 source_model_id，要求确认已加载且服务已连接；创建独立调用记录并保留来源，原产物不变。
- 模型详情展示用途、来源和下一步，当前项目模型移到页面前部，平台目录位于其后，工程助手模型入口折叠保留。仿真/远程测试结果提供改进与现场验证入口，不自动生成现场证据。
- 修复强制 refresh 被后台读取吞掉的竞争：等待当前读取后再刷新，避免保存成功却暂时看不到新资产。
- 契约和使用路径已更新 docs/JOB_RECIPES.md、docs/USER_EXPERIENCE.md，README 及版本升级到 0.6.1 / build 11。

验证：

- 全套 56 项通过，25.931 秒；日志 /tmp/neuxmind-061-tests.log。新增 5 项结果用例覆盖并发去重、不可执行资产、快照不变、过期预览、项目隔离、状态限制、独立调用配置与加载确认。使用本地记录与协议替身，不代表真实模型效果。
- tests/planner_browser.cjs 最终通过：完整训练预览/提交/计费、结果保存/返回来源、缺推理服务提示、配置调用、实际 fixture HTTP 调用、来源保留、刷新竞争、仿真后续入口、高级兼容入口及固定导航，零 JS 异常。日志 /tmp/neuxmind-061-browser-final.log。
- 新截图 artifacts/training-model-origin.png、model-library-next-steps.png、simulation-next-steps.png；已目视检查模型详情和调整后的模型库布局。前端相关脚本语法通过。
- 首次启动浏览器 fixture 缺少 PYTHONPATH 导致导入失败；改用 PYTHONPATH=.:tests 后成功。未放宽业务断言。

复验命令：.build/packaging-venv/bin/python -m unittest discover -s tests -v；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/planner_browser_fixture.py，随后执行 tests/planner_browser.cjs（需要 BRAIN_PLAYWRIGHT_MODULE / BRAIN_CHROMIUM_PATH）；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/frozen_engine_smoke.py。

最终交付：0.6.1 冻结后端 smoke 全部通过，新增 training_result_saved=true，既有 SSH、Skills/MCP 审批与角色流程仍通过（/tmp/neuxmind-061-frozen.log）。DMG 校验/挂载签名、PKG 解包深层签名检查通过，包内 web 文件与源码逐字节一致。产物为 dist/NeuxMind-Robot-OS-0.6.1-arm64.dmg、同版本 .pkg、SHA256SUMS-0.6.1.txt。原生窗口已重启并验证 0.6.1 版本与模型库顺序；8788 开发预览同步重启，本轮 QA fixture 停止。仍为 Apple Silicon macOS 13+ 本地签名开发预览，尚无 Developer ID / Apple 公证。用户实际数据目录没有写入测试资产。

## 0.6.2：同一应用的验证、发布与运行衔接

2026-10-15，分支 codex/rigid-articulation，源码未提交。仅修改 apps/robot-os；没有访问集群、真实机器人、付费模型或受保护展示服务。

本轮验收里程碑：用户从应用进入统一交付清单，知道机器人要做什么、当前缺什么、下一步如何检查/现场测试/发布/启动；所有记录绑定同一个应用版本。清单只读，真实动作保留明确授权；后续失败不能被旧通过报告覆盖。本轮不新增机器人驱动、技能自动实现、真实遥测或通用未知请求对账。

- 新增 brainos/delivery.py、web/delivery.js 和 POST /api/applications/readiness。应用检查逻辑抽为 application_checks，供只读状态和原 harness 共用，避免浏览清单自动生成报告。返回当前检查项、匹配现场证据、发布版本与最近设备请求，不返回设备令牌。
- 应用保存后直接打开交付清单。配置缺项给出连接/模型/工程入口；没有设备或技能时引导准备。工作步骤显示完成/停止条件与时限，参数折叠。现场测试、发布、启动确认页明确机器人和应用版本，不需重复选择目标。
- 人可读配置/现场报告替代默认 JSON。失败置于清单顶部，设备请求完成与测试通过分别呈现。首页、应用、报告和发布记录入口回到对应清单。现场授权复选框保留，发布不会自动启动，查看清单/发布预览不会执行设备。
- deploy 与 operate(start) 共享最新匹配现场证据校验：较新失败/未知测试阻止旧证据，新通过报告需要对应重新发布；启动前再检查当前依赖。stop 不被这些发布条件阻断。现场请求补充应用与机器人摘要便于追溯。既有存储无需迁移。
- 更新 docs/USER_EXPERIENCE.md、README；版本 0.6.2 / build 12。未知设备请求依然需现场核对，本轮没有伪造解决状态的按钮。

验证：

- 全套 60 项测试通过，28.257 秒；/tmp/neuxmind-062-tests.log。新增 4 项交付测试覆盖只读/项目隔离、匹配证据、后续失败阻断旧发布与启动、未知现场结果、新报告要求新发布、当前依赖失效与停止可用。
- tests/delivery_browser.cjs 通过本机 HTTP 协议替身：缺项引导、创建应用、只读清单、保存配置检查、现场授权、发布预览无动作、发布不启动、启动/停止、后续失败禁用旧证据、首页返回当前应用、900×640 固定导航、零 JS 异常。最终日志 /tmp/neuxmind-062-browser-final.log。
- 截图：artifacts/application-delivery-checklist.png、application-publish-review.png、application-delivery-running.png、application-delivery-failed.png。已目视检查清单、发布确认及失败状态。前端语法检查通过。
- 仅使用测试设备适配器，不产生真实机器人动作。协议通过不等于实机验收。

复验：.build/packaging-venv/bin/python -m unittest discover -s tests -v；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/delivery_browser_fixture.py 后执行 tests/delivery_browser.cjs（设置 BRAIN_PLAYWRIGHT_MODULE 与 BRAIN_CHROMIUM_PATH）；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/frozen_engine_smoke.py。

最终交付：0.6.2 冻结后端 smoke 通过，新增 application_delivery_checklist=true，保留训练结果保存、配方任务、SSH、Skills/MCP、角色流程与交付快照通过（/tmp/neuxmind-062-frozen.log）。DMG 校验/挂载签名和 PKG 解包深层签名检查通过；包内前端与源码逐字节一致。产物为 dist/NeuxMind-Robot-OS-0.6.2-arm64.dmg、同版本 .pkg 和 SHA256SUMS-0.6.2.txt。原生 Cocoa/WKWebView 已重启，界面确认版本 0.6.2；8788 开发预览同步更新，本轮 QA 服务停止，用户工作空间未写入测试资产。Apple Silicon macOS 13+ 本地签名开发预览；Developer ID / Apple 公证仍未完成。

## 0.6.3：应用修改、变更预览与版本历史

2026-10-15，分支 codex/rigid-articulation，未提交。仅修改 apps/robot-os，保留其他任务文件；没有真实机器人操作、集群任务或付费模型调用。

本轮里程碑：从交付清单修改已有应用，沿用工作配置、调整步骤并查看差异，保存独立版本后重新检查和验证；旧版本、旧证据与发布记录完整保留。未新增设备代码自动生成、技能参数 schema 或自动回退。

- 新增 brainos/revisions.py 与 web/revisions.js。服务共用 application_manifest 校验；revision-preview 只读返回规范化配置、逐字段/逐步骤差异与确认摘要。revise 在工作空间锁内重新验证并保存，保留父应用 ID/摘要及根应用关系。
- 同一应用历史内版本号唯一；同一父版本、版本号和配置的重复/并发提交返回同一记录，冲突配置拒绝。预览后参数或依赖改变须重新确认；原技能摘要改变时要求登记并明确选择新技能，不悄悄替换原绑定。
- 编辑器支持步骤添加、移除、上移/下移、参数和时限调整，返回编辑保留输入。确认页标出新增/移除、技能/参数/时限变化。保存不触发设备，新版本没有旧应用的报告或发布资格。
- 交付清单展示版本历史，可回看原版与旧报告；新版本保存后立即更新首页选择和下一步。查看旧版不部署、不执行回退。
- 文档见 docs/USER_EXPERIENCE.md 的 0.6.3 节，README 已更新。版本 0.6.3 / build 13。

验证：

- 全套 65 项测试通过，30.867 秒（/tmp/neuxmind-063-tests.log）。差异说明文字完善后受影响 5 项版本测试再次通过（/tmp/neuxmind-063-revisions.log）。覆盖只读预览、原记录不变、旧证据隔离、并发幂等、版本冲突、过期确认、技能摘要、项目隔离及步骤变更。
- tests/delivery_browser.cjs 增加从失败应用进入修改、步骤重排/移除、变更预览无写入、返回保留输入、新旧版本记录及首页新版本检查入口；既有现场授权、发布/启动/停止和后续失败限制保留。
- 首次浏览器运行在最后仍寻找旧版的首页交付入口而超时；新版本应从配置检查开始，更新断言为新版本号与检查入口，并修正保存后首页立即重绘。未降低证据隔离要求。
- 截图 artifacts/application-revision-review.png、application-version-history.png；已目视检查变更确认与历史布局，JS 语法通过。测试使用临时本地设备协议替身，用户工作空间未写入测试资产。

复验：.build/packaging-venv/bin/python -m unittest discover -s tests -v；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/delivery_browser_fixture.py 后执行 tests/delivery_browser.cjs；PYTHONPATH=.:tests .build/packaging-venv/bin/python tests/frozen_engine_smoke.py。

最终交付：最终浏览器复验通过，immutableRevisionAndHistory=true，零 JS 异常（/tmp/neuxmind-063-browser-final.log）。0.6.3 冻结后端 smoke 全部通过，immutable_application_revision=true，既有交付清单、训练产物、SSH、Skills/MCP 和角色流程仍通过（/tmp/neuxmind-063-frozen.log）。DMG 校验/挂载签名和 PKG 解包深层签名通过；包内前端与源码一致。安装包为 dist/NeuxMind-Robot-OS-0.6.3-arm64.dmg、同版本 .pkg，摘要为 SHA256SUMS-0.6.3.txt。原生应用重启后确认版本 0.6.3，8788 开发预览同步更新；本轮 QA 服务停止。仍是 Apple Silicon macOS 13+ 本地签名开发预览，Developer ID / Apple 公证未完成。

## 0.7.0：Mac 自适应本体调查（2026-11-03）

分支 codex/rigid-articulation。只修改 apps/robot-os，源码未提交；保留仓库其他任务改动。目标输入是用户 SSH 信息及工程模型，输出是持续调查会话、带证据的本体档案、覆盖缺口和本地分析产物。本轮不进行真实机器人动作、不访问集群、不修改 Studio。

实现：

- 调查启用内置 0.146.1 app-server 的原生计划、本地命令与文件分析能力，仍使用隔离工作目录和所选产品模型。远端凭据留在 SSH 连接层；本地资料 DEVICE_CONTEXT.json 排除本体控制令牌，原始数据不作为指令。
- 新增 device_overview（一次调用、九项独立证据）、device_evidence（完整证据分页）、device_exec（展示精确命令/用途/目标/时限，单次批准后执行）、device_request_scope（本会话新增 SFTP 目录授权）。原有定向探针保留，文本/源码和目录可分页读取；拒绝特殊文件、越界符号链接、凭据路径。
- 自定义 SSH shell 并没有只读沙箱，UI 明确提示实际账号权限；不能用用途说明假装已强制只读。停止会关闭当前通道，但不保证已发出的远端进程已结束。未批准或过期审批不会执行。
- 后端计算调查覆盖范围，区分未查、已采集、受限；无证据类别保留。旧线程因动态工具集合不可变，在契约升级时以已有档案/证据创建新线程，保留旧线程引用；当前契约 3。新线程正常原生续接。
- SSH 优先路径在补配模型后返回同一本体；额外命令、目录申请、覆盖数、分析产物、上次调查入口和中断续接均已接通。供应商首包超时/502 返回可理解的失败信息，保留详情和证据，不自动追加收费重试。
- 修复 Mac 系统代理误拦截回环模型服务，原生进程对回环地址设置 NO_PROXY。原生命令使用明确的最小 PATH；同一经完整性验证的上游归档内 ripgrep 15.2.0/e89fff89ac 随包分发，SHA-256 与归档路径固定，保留 MIT 与 PCRE2 10.45 许可。不是复制用户开发机的工具。
- 版本 0.7.0 / build 14。调查默认 200000 Tokens 观察限额、15 分钟，用户可修改；它不是费用金额或自有 API 的硬限额。

验证：

- 最终全套 72 项测试通过，34.012 秒，日志 /tmp/neuxmind-070-acceptance-tests.log。含真实内置运行时、实际 SSH/SFTP 协议、命令/目录审批、拒绝与取消、长文件、完整证据分页、原生 rg 分析、同一线程续接、凭据隔离、覆盖不足及友好网关错误。协议测试中的模型为本机 SSE 替身。
- tests/discovery_browser.cjs 通过：SSH 优先、缺模型后回到本体、命令和目录审批、证据、覆盖不足、分析产物、上次调查入口、900×640 固定导航，零 JS 异常；日志 /tmp/neuxmind-discovery-ui-final.log。一次复跑因复用了已注册的测试工作空间失败；重启本轮隔离 fixture 后通过，没有放宽断言。
- 最终冻结包 tests/frozen_engine_smoke.py 通过：adaptive_discovery、bundled_search_tool、ssh_scope_approval 及原有交付/训练/版本/Skills/MCP/流程能力全部为 true。日志 /tmp/neuxmind-070-installed-acceptance.log。首次 smoke 的测试客户端被系统代理拦截，改为仅对本机 fixture 使用无代理 opener 后通过，产品 connector 本来已禁用代理。
- DMG 自校验、挂载深层签名；最终 PKG 解包深层签名、包内 web 与源码字节一致，以及三项原生工具存在均通过。SHA256SUMS-0.7.0.txt 已重生成。
- artifacts/discovery-ssh-approval.png、discovery-agent-session.png、discovery-evidence-inventory.png 为隔离 UI 验收截图，已目视检查命令确认界面。

真实模型验收（未完成）：

用户授权使用指定第三方 Responses 网关并明确选择 gpt-6-astra；没有提供实际机器人地址。测试因此连接仅返回合成资料的 SSH/SFTP fixture，绝不执行收到的远端命令。tests/validate_live_discovery.py 是显式选择的付费模型验证脚本，不进入安装包，不包含密钥。

第一轮发现原生环境缺 rg 导致模型把搜索失败误判为缺文件，3 分钟内中止，报告用量 41400；补齐本地上下文准确路径/读取指引和随包工具后，第二轮成功读取上下文，但逐项探针往返耗时，在 3 分钟中止，报告用量 52807。两次失败报告均保留。

第三轮加入批量体检，窗口为 6 分钟。真实模型完成九类基础探针，申请并获得厂商目录读取权限，列出 ROS、机器人配置和厂商目录；随后供应商返回 HTTP 502，错误为 responses stream did not produce first valid SSE chunk before 1m0s precommit timeout。最终档案没有生成，驱动握手/协议/bitrate 的完整模型验收未通过。报告用量 79842（含重复输入和缓存上下文，不是账单金额）。没有用本机替身结果冒充真实模型成功，也没有替换用户指定模型或无界自动重试。

报告：artifacts/live-discovery-astra.json、live-discovery-astra-context-fix.json、live-discovery-astra-overview.json。临时凭据文件已移除；密钥不在源码、报告或安装包中。实际工作空间未写入验收机器人或模型。

交付：dist/NeuxMind-Robot-OS-0.7.0-arm64.dmg、同版本 .pkg、SHA256SUMS-0.7.0.txt。仍为 Apple Silicon macOS 13+ 本地签名开发预览，未做 Developer ID/Apple 公证。原生窗口最后检查时用户 Mac 锁定，CUA 拒绝重启操作；没有绕过锁屏，原生窗口尚未切换到新版本。已有窗口显示 0.6.3，需用户解锁后正常重启应用。

下一步：供应商解决 60 秒首包网关超时后，用相同模型继续真实多轮调查验收；再由用户提供测试本体验证厂商服务、驱动、控制协议与配置覆盖。尚不能宣称与 Codex 全部产品能力完全等效，也没有实机上岗资格。

## 0.7.1：可阅读、可追溯的本体档案（2026-11-04）

用户已选定“本体接入与设备档案”优先；本轮只开发 apps/robot-os 软件，白皮书与展示服务没有修改。分支 codex/rigid-articulation，父仓库未提交，保留其他任务改动。

- 新增 web/inventory.js，将当前档案、类别/关键词筛选、待确认项、证据阅读、历史选择和上一版记录差异整合为同一档案窗口；标题固定，支持小窗口。
- 当前/历史正文及证据引用分别绑定所选报告。历史会话的“查看本体档案”也指向该会话生成的报告，修复总是跳到当前档案的问题。返回证据页保留搜索和类别。
- 记录比较按类别与名称分组，保留同名多条记录，区分新增、描述变化、依据更新与本版未记录；不推断物理硬件移除，不把未知项消失当作已解决。
- “补齐未知项 / 继续调查”以当前档案预填请求，用户确认授权后才调用模型；有进行中的调查则回到该会话。没有扩大 SSH、模型或设备权限，没有自动执行/收费重试。
- 继续使用现有不可变报告和 evidence API，无数据迁移。更新桌面/API 版本为 0.7.1 / build 15，新增 requirements-build.txt 与从干净源码启动、构建说明。

验证：
- 原有 72 项 unittest 全部通过，34.662 秒；artifacts/inventory-071/regression.log。独立调查 7 项也通过。
- node tests/inventory_logic.cjs：6 项比较语义检查通过，覆盖重复名称、类别差异、记录缺失、描述变化、证据更新与重复引用。
- 浏览器使用 tests/inventory_browser_fixture.py 的隔离合成资料：类别+关键词筛选、证据往返、历史版本切换、受限证据提示、版本差异、历史来源会话回到对应旧档案、继续调查预填/授权表单、900×640 窗口边界通过；零 JS 错误。未发送继续调查表单，没有实际模型或机器人调用。
- 0.7.1 DMG/PKG 已生成，DMG 自校验、只读挂载签名验证通过；全部包内 web 文件逐字节与源码一致；SHA256SUMS-0.7.1.txt 保存。
- tests/frozen_engine_smoke.py 对实际 0.7.1 包运行通过，验证 inventory.js 实际路由、真实打包运行时/SSH 协议与既有工程、审批、训练/交付/版本流程。模型/机器人仍为本地协议替身，不代表实机验收。
- 截图 artifacts/inventory-071/inventory-overview.png、inventory-comparison.png。原生用户应用未重启、用户工作空间未写入测试资产。

源码交付：用户明确要求推送 https://git.neuxnet.com/physical-ai/neu-physical-code 。正在隔离整理完整软件源码；远端 HTTPS 在本机 TLS 握手失败，尚未到认证阶段，不能声称已推送。后续 Git 结果另附。
