from pathlib import Path
from html import escape
import json
ROOT=Path(__file__).resolve().parent
W,H=2200,2580
out=[];nodes=[]
colors={'ink':'#1d332f','muted':'#52665e','line':'#bdcec5','green':'#28654d','blue':'#386fa6','purple':'#775795','paper':'#f8faf7','planned':'#9c7137'}
def text(x,y,s,size=22,fill='ink',weight=400,anchor='start'):
 out.append(f'<text x="{x}" y="{y}" fill="{colors.get(fill,fill)}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>')
def rect(x,y,w,h,fill='#fff',stroke='#d7e2db',r=14,dash=''):
 out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"'+(f' stroke-dasharray="{dash}"' if dash else '')+'/>')
def path(points,color='green',dash=False,arrow=True):
 d='M '+' L '.join(f'{x},{y}' for x,y in points)
 out.append(f'<path d="{d}" fill="none" stroke="{colors[color]}" stroke-width="2.4"'+(' stroke-dasharray="7 6"' if dash else '')+(f' marker-end="url(#{color})"' if arrow else '')+'/>')
def band(y,title,sub,h):
 rect(52,y,1720,h,'#f0f5f0','#d4dfd6',18);text(76,y+34,title,24,weight=600);text(76,y+62,sub,18,'muted')
def node(id,x,y,w,title,lines,status='规划',h=124):
 out.append(f'<g data-node="{id}">')
 rect(x,y,w,h,stroke='#b9cdc0' if status=='已有' else '#d1dcd5',dash='6 4' if status=='规划' else '')
 text(x+16,y+29,title,22,weight=600)
 for i,s in enumerate(lines):text(x+16,y+59+i*25,s,18,'muted')
 text(x+w-14,y+h-13,{'已有':'● 已有实现','框架':'◐ 框架 / 协议','规划':'○ 目标建设','外部':'◇ 外部系统 / 按需接入'}[status],15,'green' if status=='已有' else 'planned',anchor='end')
 out.append('</g>')
 nodes.append({'id':id,'title':title,'lines':lines,'status':status})
def bus(y,label):
 path([(82,y),(1742,y)],'green',arrow=False);rect(570,y-15,680,30,colors['paper'],colors['paper'],0);text(910,y+6,label,18,'green',anchor='middle')
xs=[76,416,756,1096,1436];nw=312
out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc"><title id="title">NeuxMind Robot OS 产品完整系统架构</title><desc id="desc">完整目标架构及 v0.6.3 实现标记。包含 Mac 客户端、平台控制面、工程 Agent、模型数据训练仿真工厂、独立验证发布、机器人端、原有运控、治理与 RSI。箭头区分任务控制、批准制品和反馈数据。</desc><defs>')
for c in ['green','blue','purple']:
 out.append(f'<marker id="{c}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 Z" fill="{colors[c]}"/></marker>')
out.append('</defs><g font-family="PingFang SC, Microsoft YaHei, sans-serif">');rect(0,0,W,H,colors['paper'],colors['paper'],0)
text(56,56,'NEUXMIND / ROBOT OS',20,'green',600);text(56,108,'产品完整系统架构',42,weight=600)
text(56,147,'让用户围绕一个工作目标，完成接入、能力准备、训练仿真、验证、部署和持续运行。',23,'muted')
text(56,181,'目标架构 · 2026-11-02   |   实现基线：v0.6.3   |   实线框不代表已做实机验收；虚线框为待建设模块',18,'muted')
text(1810,56,'图例 / 阅读方式',21,weight=600)
for y,c,label,dashed in [(91,'green','任务、配置与控制',False),(124,'blue','批准制品下发',False),(157,'purple','反馈、数据与研究',True)]:
 path([(1810,y-6),(1855,y-6)],c,dashed);text(1867,y,label,18,'muted')
# User journey
rect(52,212,1720,56,'#e3eee3','#c2d5c5',12)
text(912,248,'描述工作  →  接入本体  →  选用 / 开发能力  →  按需训练与仿真  →  验证  →  部署运行  →  持续改进',24,'green',500,'middle')
# 1
band(292,'01  用户 Mac · 桌面工作台与本地连接','用户入口；交互与工程执行可本地运行。macOS Cocoa / WKWebView，Windows 后续适配。',222)
node('desktop',xs[0],370,nw,'任务与交付工作台',['项目 / 应用版本 / 交付清单','训练、仿真、模型与账单入口'],'已有')
node('local',xs[1],370,nw,'本地工作空间',['本地 API / 账号隔离 / SQLite','工程文件 / 资产快照 / 会话'],'已有')
node('connect',xs[2],370,nw,'设备连接管理',['SSH / SFTP / HTTP 适配器','主机指纹 / 连接资料 / 档案'],'已有')
node('ux',xs[3],370,nw,'开发与可视化工具',['3D 场景 / 技能图 / 代码工作区','回放 / 可视化诊断 / 扩展 SDK'])
node('dist',xs[4],370,nw,'安装与客户端维护',['已有 Apple Silicon 安装包','公证、自动更新、系统密钥待补'],'框架')
path([(910,514),(910,555)]);text(935,543,'本地 API / 平台 HTTPS API',18,'muted')
# 2
band(558,'02  平台控制面 · 产品业务与商业服务','目标部署：平台云或企业私有环境；当前业务原型主要运行在 Mac 本地，尚不是托管 SaaS。',222)
node('iam',xs[0],636,nw,'组织、身份与访问',['组织 / 团队 / RBAC / SSO','API 鉴权 / 租户与项目隔离'])
node('assets',xs[1],636,nw,'统一资产与版本',['本体 / 模型 / 数据 / 场景','技能 / 应用 / 证据与版本谱系'],'框架')
node('jobs',xs[2],636,nw,'作业与流程控制',['方案 / 参数 / 时限 / 状态','预算 / 取消 / 审批 / 审计'],'框架')
node('catalog',xs[3],636,nw,'平台供给与模型目录',['自研模型 / 开源模型 / 闭源 API','平台资源与自有服务统一选用'],'框架')
node('billing',xs[4],636,nw,'用量、额度与商业计费',['已有预留 / 计量证据 / 结算框架','支付 / 订阅 / 对账 / 发票待补'],'框架')
bus(812,'统一契约：项目资产、作业预算、事件、权限、版本与证据')
for x in [232,912,1592]:path([(x,812),(x,846)])
# 3
band(850,'03  工程 Agent 与 Harness · 发现设备、开发能力、验证工程','工程运行时处理代码和工具；机器人实时动作由端侧执行监督与厂商控制器负责。',222)
node('agent',xs[0],928,nw,'内置工程运行时',['会话 / 计划 / 文件编辑 / 命令','本地测试 / 差异 / 问答 / 审批'],'已有')
node('roles',xs[1],928,nw,'多角色任务协作',['本体分析 → 适配开发 → Harness','串行阶段 / 交付快照 / 人工验收'],'已有')
node('discovery',xs[2],928,nw,'本体调查与工程上下文',['受控 SSH 探针 / 原始证据','软硬件档案 / 未知项 / 适配建议'],'已有')
node('tools',xs[3],928,nw,'工程扩展与二次开发',['版本化 Skills / HTTP MCP','选择工具 / 权限收回 / 逐次审批'],'已有')
node('sandbox',xs[4],928,nw,'远程工程与插件生态',['容器沙箱 / CI / 工具连接器','并行流程 / 插件市场 / SDK'])
bus(1104,'工程产物与实验计划 → 执行服务；模型调用 → 统一模型网关')
for x in [232,912,1592]:path([(x,1104),(x,1138)])
# 4
band(1142,'04  资源执行面 · 模型、数据、训练与仿真工厂','任务在隔离 Worker 中执行；资源可来自平台云、企业集群或用户自有服务，不固定 GPU 型号与卡数。',270)
node('gateway',xs[0],1220,nw,'统一模型网关',['供应商路由 / 模型选择 / 限额','闭源 Token / 自有 API / 推理池'])
node('data',xs[1],1220,nw,'数据与经验工厂',['公开数据 / 示教 / 授权轨迹','质检 / 标注 / 去重 / 数据分割'])
node('teacher',xs[2],1220,nw,'教师与采集执行器',['强模型规划 / 空间标注 / 纠正','仿真或受控真机执行后入库'])
node('train',xs[3],1220,nw,'训练与模型优化',['微调 / 模仿 / 蒸馏 / 量化','共享表征＋任务专家＋本体头'])
node('sim',xs[4],1220,nw,'仿真与测试执行器',['Continuum / 外部仿真适配','传感器 / 随机化 / 回放 / 评测'])
text(86,1386,'公共执行底座：资源调度与配额 · 队列 · 容器隔离 · GPU 租约 · 检查点恢复 · 日志与指标 · 实际用量计量 〔目标建设〕',20,'muted')
# actual data relations, under nodes gap
path([(1068,1282),(1090,1282)],'purple',True)
path([(1408,1282),(1430,1282)],'purple',True)
path([(1232,1412),(1232,1466)],'purple',True);text(1250,1444,'候选模型 / 实验结果',18,'muted')
# 5
band(1468,'05  独立验证、制品注册与发布 · 从候选到可交付能力','工程 Agent、训练作业、RSI 只能提交候选；兼容性、证据与发布权限由独立流程核对。',222)
node('harness',xs[0],1546,nw,'配置与工程检查',['依赖 / 能力匹配 / 版本固定','工程测试记录与失败反馈'],'已有')
node('eval',xs[1],1546,nw,'独立模型与任务评测',['离线 / 仿真 / 真机 / 端侧性能','留出集 / 回归 / 失败恢复'])
node('registry',xs[2],1546,nw,'模型与能力制品库',['已有资产版本与训练来源','对象存储 / 包签名 / 兼容矩阵'],'框架')
node('release',xs[3],1546,nw,'验证与发布门',['已有现场证据绑定 / 发布协议','旧证据失效 / 新版本重新验证'],'框架')
node('fleet',xs[4],1546,nw,'机群与发布运营',['设备分组 / 灰度 / 远程监测','原子更新 / 回滚 / 故障对账'])
for x in [388,728,1068,1408]:path([(x,1610),(x+24,1610)],'blue')
path([(910,1690),(910,1748)],'blue');text(940,1726,'模型包＋技能包＋适配包＋配置＋批准范围',20,'blue')
# 6
band(1752,'06  机器人端 · 本地大脑、执行监督与生命周期','本地实时闭环不等待云端教师；模型、记录与网络故障不能阻塞设备定义的停止路径。',364)
node('edge',xs[0],1830,nw,'Edge 接入网关',['已有认证 / 幂等 / 状态查询','测试、安装、发布与操作协议'],'框架')
node('loader',xs[1],1830,nw,'包管理与推理运行时',['版本校验 / 兼容加载 / 模型缓存','硬件后端 / 热切换 / 断网可用'])
node('task',xs[2],1830,nw,'任务与技能执行',['任务记忆 / 前后置条件 / 调度','超时 / 取消 / 有界恢复'])
node('observe',xs[3],1830,nw,'同步观测与状态估计',['视觉 / 深度 / 关节 / 力触觉','时间同步 / 标定 / 状态与记忆'])
node('policy',xs[4],1830,nw,'专业端侧模型池',['感知 / 操作 / 移动 / 交互','任务专家 / 动作头 / 成败检测'])
rect(76,1980,1672,104,'#e8f0e9','#bad0bd',12)
text(96,2010,'本地执行总线与监督器',23,'green',600)
text(96,2041,'Observation → 专家策略 → ActionChunk → 独占控制权 / 有效期 / 模式 / 单位 / 限幅 → 本体适配',21,'ink')
text(96,2069,'执行反馈 → 结果检测 / 恢复 / 停止；记录器异步保存轨迹、介入与版本，不阻塞实时控制 〔目标建设〕',18,'muted')
for x in [232,572,912,1252,1592]:path([(x,1954),(x,1977)])
path([(910,2116),(910,2170)]);text(940,2149,'按已验证能力下发；实测反馈反向返回',19,'muted')
# 7
band(2174,'07  本体适配、厂商控制与真实机器人','目标覆盖多种本体；每个型号仍须完成接口、标定、驱动与现场资格验证。',228)
node('adapter',76,2252,520,'本体适配器与驱动插件',['能力清单 / 坐标与时钟 / 动作语义转换 / IK 与约束','ROS 2 或厂商 SDK / 串口 / USB / 工业网络'],'框架')
node('oem',622,2252,520,'保留厂商运控与硬件边界',['底盘 / 飞控 / 全身控制 / 位置、速度、阻抗或力矩','硬件急停 / 硬限位 / 原有控制周期 / 工具 IO'],'外部')
node('bodies',1168,2252,580,'任意形态，通过能力契约适配',['机械臂 / 移动操作 / 四足 / 人形 / 无人机 / 自定义','传感器、执行器与外部工具；以实际开放接口为准'],'外部')
path([(598,2312),(619,2312)]);path([(1144,2312),(1165,2312)])
# Jobs may bypass engineering; the layers are not mandatory sequential user steps.
path([(52,740),(28,740),(28,1180),(52,1180)])
out.append('<text transform="translate(20,1100) rotate(-90)" fill="#28654d" font-size="17">训练 / 仿真可直接调度</text>')
# feedback spine
path([(1760,2346),(1786,2346),(1786,1350),(1752,1350)],'purple',True)
text(1778,2135,'经验回流',17,'purple',anchor='end')
# right cross-cutting sidebars
rect(1800,212,348,506,'#f1f4f1','#cfdad1',16)
text(1820,249,'贯穿各层 · 平台治理',24,weight=600)
items=[('身份与租户边界','团队权限 / 项目范围 / 授权'),('凭据与供应商秘密','密钥托管 / 最小权限 / 轮换'),('统一资产与对象存储','数据 / 模型 / 工程制品 / 谱系'),('持久队列与事件总线','工作流 / 资源调度 / 幂等与恢复'),('可观测与成本管理','追踪 / 用量证据 / 告警 / 对账')]
for i,(title,sub) in enumerate(items):
 y=291+i*78;text(1820,y,title,21,weight=500);text(1820,y+29,sub,18,'muted')
text(1820,693,'目标平台能力；本地已具部分框架',16,'planned')
rect(1800,742,348,760,'#f3eff5','#d9cde0',16)
text(1820,780,'RSI · 持续与递归改进',24,'purple',600)
text(1820,812,'目标建设 · 独立于实时控制',18,'muted')
rsi=[('01 发现问题与研究记忆',['失败 / 漂移 / 成本 / 新需求','历史实验 / 候选与祖先谱系']),('02 提出改进候选',['数据 / 模型 / 技能 / 工具','根因假设 / 补丁 / 实验计划']),('03 隔离实验与独立评测',['固定预算 / 对照 / 留出集','复现 / 回归 / 收益与成本']),('04 归档、选择、申请发布',['有效与失败候选都保留','通过统一发布门，不能自批']),('05 改进研究代理自身',['工具 / 规划 / 搜索 / 记忆','新改进器再次接受独立评测'])]
for i,(title,ls) in enumerate(rsi):
 y=844+i*118;text(1820,y,title,21,weight=500)
 for j,l in enumerate(ls):text(1820,y+29+j*25,l,18,'muted')
 if i<4:path([(1968,y+64),(1968,y+91)],'purple',True)
text(1820,1468,'目标 / 验收 / 权限 / 预算不可自改',17,'purple')
path([(1786,1160),(1798,1160)],'purple',True)
path([(1800,1490),(1780,1490),(1780,1508),(1746,1508)],'purple',True)
# contracts
rect(1800,1526,348,560,'#f1f4f1','#cfdad1',16)
text(1820,1566,'跨层契约与对象',24,weight=600)
contracts=[('任务与技能','Task / Skill / Application'),('设备与能力','Robot / BodyCapabilities'),('执行与计量','Job / Reservation / Usage'),('数据与训练','Episode / Dataset / Experiment'),('感知与控制','Observation / Action / Feedback'),('制品与交付','Model / Adapter / Release'),('证据与改进','Evaluation / Proposal / Archive')]
for i,(title,sub) in enumerate(contracts):
 y=1612+i*61;text(1820,y,title,20,weight=500);text(1820,y+25,sub,16,'muted')
text(1820,2067,'语义对象；具体 schema 分阶段实现',16,'planned')
rect(1800,2110,348,292,'#eff2ec','#cfd8c7',16)
text(1820,2150,'当前落地位置',24,weight=600)
for i,s in enumerate(['Mac：界面＋本地业务控制面','Mac：工程运行时＋SSH 调查','服务端：外部协议执行器接入','端侧：通用网关＋厂商适配接口','共享云服务与端侧智能待建设','已有协议验证 ≠ 已完成实机验收']):text(1820,2192+i*32,s,18,'muted')
# bottom notes
text(56,2445,'主闭环：用户任务 → 工程与模型能力 → 独立验证 → 批准制品 → 机器人执行 → 授权经验 → 训练 / RSI → 新候选',23,'green',500)
text(56,2484,'模型与算力可按量付费，也可接入自有资源；用户侧统一模型选择与预算，供应商凭据由服务端管理。',21,'muted')
text(56,2520,'来源：白皮书 v1.2（含 2026-11-01 系统框架）＋ Robot OS v0.6.3 代码与交接记录。模块分层为产品目标设计。',18,'muted')
text(56,2551,'“已有”表示已具本地实现；“框架”表示局部实现或协议链路；“规划”表示完整能力待建设；“外部”表示厂商系统及实体设备。',18,'muted')
out.append('</g></svg>')
(ROOT/'neuxmind-system-architecture.svg').write_text('\n'.join(out))
(ROOT/'architecture-modules.json').write_text(json.dumps(nodes,ensure_ascii=False,indent=2))
# Editable Mermaid overview: semantic source, less dense than the printable atlas.
groups=[('客户端',['desktop','local','connect','ux','dist']),('平台控制面',['iam','assets','jobs','catalog','billing']),('工程 Agent 与 Harness',['agent','roles','discovery','tools','sandbox']),('资源执行面',['gateway','data','teacher','train','sim']),('验证与发布',['harness','eval','registry','release','fleet']),('机器人端',['edge','loader','task','observe','policy']),('本体与硬件',['adapter','oem','bodies'])]
m=['flowchart TB'];lookup={n['id']:n for n in nodes}
for i,(title,ids) in enumerate(groups):
 m.append(f'  subgraph L{i}["{title}"]');m.append('    direction LR')
 for id in ids:
  n=lookup[id];m.append(f'    {id}["{n["title"]}<br/>{n["status"]}"]')
 m.append('  end')
m+=['  desktop --> local','  local --> assets','  local --> jobs','  local --> catalog','  connect --> discovery','  jobs --> roles','  roles --> agent','  agent --> tools','  agent --> sandbox','  agent --> gateway','  discovery --> assets','  catalog --> gateway','  jobs --> train','  jobs --> sim','  billing -.预算与计量.-> jobs','  iam -.授权.-> jobs','  data --> train','  gateway --> teacher','  teacher --> sim','  teacher -.执行后轨迹.-> data','  sim -.轨迹与结果.-> data','  train --> eval','  agent --> harness','  harness --> eval','  eval --> registry','  registry --> release','  release --> fleet','  release --> edge','  edge --> loader','  edge --> task','  loader --> policy','  observe --> policy','  task --> supervisor["执行监督与动作仲裁<br/>规划"]','  policy --> supervisor','  supervisor --> adapter','  adapter --> oem','  oem --> bodies','  bodies -.观测与反馈.-> observe','  observe -.授权经验.-> data','  data -.失败信号.-> rsi["RSI：发现→提案→沙箱→独立验证→归档<br/>研究代理自身改进 / 规划"]','  rsi --> train','  rsi --> sandbox','  rsi --> eval','  fleet -.灰度与运行反馈.-> edge']
(ROOT/'neuxmind-system-architecture.mmd').write_text('\n'.join(m)+'\n')
print('Generated',len(nodes),'modules',W,H)
