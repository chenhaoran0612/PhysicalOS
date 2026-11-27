# 本体类型清单（issue #32，需求 R1-11）

更新：2026-11-27（issue #58：内置类型改成和仿真档案一样的五类）。代码在 `brainos/body_types.py`、`web/body_types.js`、`web/body_types.css`。

## 是什么

添加设备时要选一个本体类型。可选的类型来自一份由平台维护的清单：

- 内置五类，和「添加仿真机器人」的基础档案（[SIM_PROFILES.md](SIM_PROFILES.md)）是同一份、同样的名称和顺序：
  **机械臂**（`manipulator`）、**轮足机器狗**（`wheel_legged`）、**四足机械狗**（`quadruped`）、**双足机器人**（`biped`）、**其他**（`other`）。
  内置类型不能删除。issue #58 之前只有四足机器人和机械臂两类，选了轮足的型号（如钢镚 L2-W）也只能登记成四足。
- 可以添加新类型，也可以删除没有设备在用的类型。
- 每个类型有三样内容：

| 内容 | 字段 | 说明 |
| --- | --- | --- |
| 名称 | `name` | 1–40 个字符，清单内不重名。比较和保存前先统一写法：全角字母数字转半角、多个空白并成一个空格、不分大小写；不能有看不见的字符 |
| 状态机本体模板 | `state_machine_template` | 模板的标识。状态机模板本身（需求 R4）还没有实现，这里先登记标识：小写字母、数字、点、下划线、连字符，以字母或数字开头，最长 100 |
| 应有的运控接口 | `required_capabilities` | 这类本体应该具备的接口，用标准能力的编号表示（`spec/neuxmind_spec/capabilities.py` 里的编号，如 `base.velocity`），至少一个。运控驱动检查（issue #28）按它核对 |

内置五类的内容（名称、编号、顺序取自 `brainos/sim_profiles.py` 的 `TYPES`，有测试保证两边一致）：

| 类型 | 编号 | 对应的标准取值 | 状态机模板 | 应有的运控接口 |
| --- | --- | --- | --- | --- |
| 机械臂 | `manipulator` | `manipulator` | `manipulator.default` | `safety.stop`、`joint.state`、`arm.joint_position` |
| 轮足机器狗 | `wheel_legged` | `quadruped` | `wheel_legged.default` | `safety.stop`、`base.velocity`、`legged.posture`、`base.odometry` |
| 四足机械狗 | `quadruped` | `quadruped` | `quadruped.default` | `safety.stop`、`base.velocity`、`legged.posture`、`base.odometry` |
| 双足机器人 | `biped` | `humanoid` | `biped.default` | `safety.stop`、`base.velocity`、`legged.posture`、`joint.state` |
| 其他 | `other` | `custom` | `other.default` | `safety.stop`、`system.health` |

轮足、双足、其他这三类的模板标识和应有接口是 issue #58 时按相近的类型定的，没有和真机核对过；状态机模板本身（R4）仍未实现。

仿真四足和仿真机械臂声明的能力包含各自类型要求的接口，仿真四足也满足轮足的（有测试）。

「对应的标准取值」是这类本体在设备清单里自报的 `family`。需要标准取值的代码用 `body_types.standard_family(编号)`：
内置类型取上表，旧取值原样返回，后来添加的类型是 `custom`。

## 类型跟着型号走（issue #58）

添加机器人的两个表单里，在品牌、型号的清单（`web/robot_catalog.js`）里选了一个型号，本体类型会自动选成这个型号的类型
（钢镚 L2-W → 轮足机器狗，G1 → 双足机器人）。只在人还没有自己选类型时这样做：类型是空的，或者是上一个型号带出来的。
人自己选过的类型不会被后选的型号改掉。型号是手填的（「其他型号」）时不动类型。
清单里型号的类型到本体类型的对应：`quadruped`、`wheel_legged`、`manipulator` 同名，`humanoid` → `biped`，`wheeled` → `other`。

已经登记的机器人不会被自动改类型：以前只能登记成四足的轮足机器人（例如真机 钢镚 L2-U）要在「修改类型」里改成轮足机器狗。
仿真机器人例外：从档案添加的仿真机器人，类型就是档案的类型，升级后打开一次会把以前存成四足的轮足仿真机器人改过来
（`SimProfiles.align_types`）；仿真本身仍按标准取值运行。

读类型的几处：

- 安装管理层（`runtime_install_plan.ADAPTERS`）：轮足机器狗和四足机械狗都装仿真四足本体，机械臂装仿真机械臂；双足机器人、其他没有对应的仿真本体，照旧拒绝（`install_body_unsupported`）。
- 本体适配生成适配器（`adaptation.scaffold_spec`）、应用的 MuJoCo 仿真（`mujoco_sim`）：按 `standard_family` 取标准取值，所以轮足按四足、双足按人形处理。

## 三条规则

**1. 设备的类型以人选的为准。** 连接检查读到的设备自报类型（清单 `manifest` 里的 `family`）不再覆盖人选的类型，
只是留在 `robot['manifest']['family']` 里。设备自报的是清单里的另一个类型时，卡片上显示“设备自报：…”，
“修改类型”里可以一键改成它；自报的值不在清单里时只在“修改类型”和详情里说明，不提示修改。

**2. 标准不变，清单列自己的类型。** 标准（`spec/neuxmind_spec/manifest.py` 的 `FAMILIES`）仍是 7 个取值，设备清单的校验、
端侧运行时、SDK、云端模型目录都不变。模型包标注的是标准取值，所以端侧模型部署仍按设备自报的类型匹配。
平台清单里没有标准的 5 个取值（移动机器人 `mobile`、人形 `humanoid`、无人机 `aerial`、复合 `composite`、其他/自定义 `custom`；清单里的「双足机器人」「其他」是 `biped`、`other`，不是这几个旧值）：

- 以前登记的设备保留原来的值，卡片上标“旧类型，不在清单里”，不会被自动修改，其余功能照常；
- 新设备不能选这 5 个值，添加设备的两个表单里类型是必填项，开始时没有选中任何一项；
- 可以在“修改类型”里把旧类型的设备改成清单里的类型，改完以后不能再改回旧值。

**3. 还有设备在用的类型不能删除。** 删除时检查这个账号所有项目里的设备；有设备在用就拒绝，并列出这些设备和它们所在的项目。
检查和删除在同一把锁里完成，登记设备、修改类型也在这把锁里保存，所以不会出现“刚检查完就有设备选了它”的情况。

**已登记设备的类型只有“修改类型”能改。** 这条由保存记录的地方保证（`brainos/store.py` 的 `Store.owned`，
`BodyTypes` 启动时登记 `robots → family`）：别的代码保存一条已有的机器人记录时，类型一律取库里当时的值。
连接检查、读取详情、现场测试这些流程都是“先读记录、等设备回答、再把整条记录写回”，等待期间人改了类型也不会被写回去的旧记录改掉。
要改类型的代码用 `store.put('robots', record, owner=True)`，目前只有 `BodyTypes.assign`。

## 保存在哪

清单随桌面端保存，在每个登录账号自己的工作区里（和设备在同一个库，记录种类 `body_types`）。只保存后来添加的类型，
五类内置写在代码里。所以：不登录云平台、离线也能用；不同账号各有一份清单；云端不知道这份清单。
以后需要多台电脑共用一份清单时，再做云端同步。

`robot['family']` 保存的是类型的编号：内置类型是上表的五个编号，后来添加的是 `bodytype-…`，旧设备是原来的取值。
读这个字段的地方要知道它不再一定是标准取值：

- 任务配方拿到的机器人信息（`brainos/recipes.py`）、交付清单、安全监视里的 `family` 都是这个编号；
- 本体适配生成适配器时（`brainos/adaptation.py`），写的是 `standard_family` 给出的标准取值；
- 需要设备自报的标准取值时读 `robot['manifest']['family']`（云端模型匹配、端侧模型部署已经这样做）。

清单最多添加 100 个类型（整份清单随每次读取状态一起发给页面）。

## 界面

- “我的机器人”页面最下面有一个折叠的“本体类型清单（N 类）”：每类的名称、状态机模板、应有的运控接口、在用设备数；
  “添加本体类型”；后来添加的类型有“删除”。删除在用的类型时，对话框列出在用的设备，不提供删除按钮。
- “通过 SSH 调查真机”和“连接已在运行的机器人”两个表单里的“本体类型”来自这份清单。
- 真机卡片上类型名称旁有“修改类型”。仿真机器人的类型由仿真种类决定，不能修改。
- 机器人详情里显示本体类型；设备自报的类型和它不一样时一并显示。

## 接口（桌面端）

- `POST /api/body-types` `{name, state_machine_template, required_capabilities}` → 新类型
  `{id, name, state_machine_template, required_capabilities, builtin: false, created_at}`。
  拒绝：`body_type_name_invalid`、`body_type_name_taken`、`body_type_template_invalid`、`body_type_interfaces_required`、
  `body_type_interface_unknown`、`body_type_limit`。
- `POST /api/body-types/remove` `{body_type_id}` → `{removed}`。
  拒绝：`body_type_in_use`（带 `devices: [{id, name, project_id, project_name}]` 和 `devices_total`，列表最多 500 台）、
  `body_type_builtin`、`body_type_unknown`。
- `POST /api/robots/body-type` `{project_id, robot_id, family}` → 修改后的机器人记录。只改类型，别的字段不动。
  拒绝：`body_type_required`（不在清单里）、`body_type_sim_fixed`（仿真机器人）。
- `POST /api/robots`、`POST /api/engineering/ssh/connect`（新建机器人时）：`family` 必须是清单里的类型，否则 `body_type_required`。
- `GET /api/state` 多两项：`body_types`（清单，内置在前）、`body_type_interfaces`（可选的标准能力：`id, title, kind, safety`）。
  原有的 `families` 保留，用来显示旧取值的名称。

添加、删除、修改类型都写审计事件：`body_type.added`、`body_type.removed`、`robot.body_type_changed`（带 `from`、`to`）。

## 测试

- `python -m unittest tests.test_body_types`：清单的增删、在用不能删并列出设备、新设备必须选清单里的类型、
  设备自报不覆盖人选的、等设备回答期间改的类型不被写回的旧记录改掉、修改类型、
  升级后已登记设备的类型不变（7 个旧取值逐个检查）、接口、不同账号各有一份清单。
- `node tests/body_types_logic.cjs`：页面上的名称、选项、卡片、清单、对话框（纯逻辑）。
- `node tests/body_types_browser.cjs <info.json> [截图目录]`，fixture 用 `tests/body_types_browser_fixture.py`
  （服务需要 `NEUXMIND_MAIL_DEV_ECHO=1`）。加 `BRAIN_BROWSER=webkit` 在 WebKit（桌面应用的内核）上跑。

## 没有做的

- 状态机模板本身（R4）；这里只有标识。
- 按 `required_capabilities` 检查设备（issue #28）。
- 修改一个类型的内容（名称、模板、接口）。现在只能添加和删除。
- 本体适配流程交给工程助手的资料里，类型还只是编号，没有带上类型的名称、模板和应有的接口。
- 通过 SSH 接入时，没选类型的请求要到登录测试通过之后才被拒绝（页面上类型是必填，走不到这一步；直接调接口会）。
- 清单的云端同步；云端模型目录也没有接这份清单。
- 让设备自报平台新增的类型（需要放开标准里 `family` 的取值，影响端侧运行时、云端和 SDK）。
