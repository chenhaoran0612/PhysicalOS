# 品牌型号：机器人应用和技能按品牌型号归属（issue #48）

机器人应用原来只绑定一台机器人。现在每台机器人有品牌和型号，应用和技能属于一个品牌型号：同品牌同型号的机器人都能用，其他品牌型号的不能用。本文说明规则、旧数据怎么处理，以及在哪里验证过。

> 状态：代码和测试完成；**没有在任何真机上运行过**。验证范围见[“在哪里验证过，哪里没有”](#在哪里验证过哪里没有)。

用户在 issue 会话里定的三件事（2026-11-25）：

1. 应用**属于品牌型号，同型号通用**：同品牌同型号的任何一台机器人都能检查、现场测试、发布它，其他品牌型号的不能。
2. 品牌型号**由设备上报；设备没有上报时，添加机器人的时候必须填写**。
3. 除了机器人应用，**技能（技能资产、云端技能）也按品牌型号隔离**。

## 规则

### 机器人的品牌型号

- 记在机器人上的 `product`：`{vendor, model, source, updated_at}`，`source` 是 `device`（设备上报）或 `manual`（人填的）。
- 设备上报指能力清单（`/v1/manifest`、`/v2/manifest`）里的 `vendor` 和 `model`。两项都要有；空着或写着 `unknown`、`待确认` 之类的不算上报。
- 添加机器人：
  - 按服务地址添加（“连接已在运行的机器人”）：品牌和型号可以留空，这时控制台向填的地址读一次能力清单，设备上报了就用上报的；没上报、连不上或没填地址，就拒绝（`robot_model_required`），要求填写。
  - 通过 SSH 添加：只靠 SSH 的设备没有上报的途径，表单里品牌和型号必填。
  - 仿真机器人：启动后自己上报（`neuxmind-sim` / `sim-quadruped` 等）。
- 之后每次连接检查，设备上报了品牌型号就以上报的为准，会替换人填的（记一条 `robot.model_changed`）。设备上报的不能手工改（`robot_model_from_device`）。
- 人填的可以在机器人卡片的“更多功能 → 修改品牌型号”里改；没有品牌型号的旧机器人，卡片上有“未填写品牌型号”的提示和“填写品牌型号”入口。
- 两个品牌型号相同，指品牌和型号各自不分大小写相等。每项最多 100 个字符，多余的空白会并掉。

### 机器人应用

- 应用保存时带上目标机器人的品牌型号（`product: {vendor, model}`，算进应用摘要）。
- 检查、交付清单、现场测试、发布这几个请求可以带 `robot_id`，指明用哪一台；这台必须是应用的品牌型号，否则拒绝（`app_other_model`）。不带时用创建应用时选的那台；那台不在了或者品牌型号变了，就用同品牌型号里最早添加的一台；一台都没有就拒绝（`app_no_robot`）。
- 现场测试证据和发布版本仍然按台算：在甲机器人上通过的现场测试，不能用来发布到乙机器人。发布时不指明机器人，就发到证据所在的那台。
- “修改为新版本”不能把应用改到另一个品牌型号的机器人上（`app_other_model`）；可以换成同品牌型号的另一台。
- 交付清单里显示应用的品牌型号；同品牌型号有多台机器人时，可以在清单里选“用哪台机器人”，之后的检查、现场测试、发布都对选中的这台。

### 技能

- 登记的技能带 `product`。“使用机器人上已安装的技能”加入的技能，取那台机器人的品牌型号；手动登记时在表单里填。
- 应用只能编排自己品牌型号的技能（`skill_other_model`）；编排应用的表单里，只列出所选机器人品牌型号的技能。
- 同一个技能装在两种品牌型号的机器人上，要各加入一次，是两条技能记录。

### 云端技能（技能发布 / 技能加载）

- 云端的一个技能版本属于一个品牌型号：`skill_releases` 表多了 `vendor`、`model` 两列（旧库启动时自动补列，旧记录为空）。
- 发布：`POST /skills/releases` 的请求里多了可选的 `vendor`、`model`（要一起给，否则 400 `invalid_product`）。控制台发布时必须填（`skill_product_required`），包括“发布应用自带的示例技能”。
- 同一个版本内容相同再发一次仍然是原来那条；给的品牌型号和原来的不一样时拒绝（409 `skill_other_model`）。一个版本只属于一个品牌型号，给另一种机器人用要换版本号。
- 没有品牌型号的旧版本：`PUT /skills/{id}/releases/{version}/product` `{vendor, model}` 指定一次（已经有且不同 → 409 `skill_product_set`）。控制台在“技能发布”的版本列表里有“指定品牌型号”。
- 安装到机器人（预检和安装都查）：版本没有品牌型号 → `skill_product_missing`；机器人没有品牌型号 → `robot_model_missing`；两者不一样 → `skill_other_model`。
- 所有版本的 `Release` 里多了 `product`（`{vendor, model}` 或 `null`）。

没有改的地方：从本机文件夹直接装到一台机器人的技能包、机器人上的程序和场景数据（本来就绑定一台设备）、模型包（仍按本体类型和推理运行时判断）、第三方经开放接口的调用（按设备）。

## 旧数据和不经页面的调用

品牌型号的填写是在面向人的两个入口上强制的（路由 `robots` 和 `engineering/ssh/connect`）。直接调用 `Platform.add_robot` 的代码（单元测试是这样）可以不给，所以系统里允许“没有品牌型号”的记录，规则是：

- **没有品牌型号的机器人自成一类**：为它建的应用也没有品牌型号，只能用在这一台上。给它填了品牌型号（或设备后来上报了）之后，这些应用自动带上这个品牌型号，同品牌型号的机器人就都能用。应用的摘要不变，已有的测试证据照旧有效；发给设备时不带这个后补的品牌型号（设备会按收到的内容重算摘要）。应用里有别的品牌型号的技能时不补，它仍然只用于原来那一台。
- **没有品牌型号的技能**，在第一次被编排进一个有品牌型号的应用时，带上那个应用的品牌型号。
- 打开工作区时做一次整理（`RobotModels.migrate`）：保存的能力清单里有品牌型号的机器人补上 `product`；应用跟随它的机器人，技能跟随用到它的应用。原地保存，列表顺序不变；出错时工作区照原样打开，记一条 `robot_model.migration_failed`。

要知道的一种情况：人填的品牌型号被设备上报的不同值替换后（写法不同也算不同，例如填“Acme Robotics”、设备报“Acme”），原来按人填的值建的应用就没有可用的机器人了，打开它的交付清单会说明它属于哪个品牌型号；这时要为这种机器人另建应用。填写时尽量照设备铭牌或接入程序里的写法。

升级后用户要做的事：没有上报品牌型号的旧真机，在卡片上填一次。不填也能照旧使用，只是它的应用不能给别的机器人用。

## 代码在哪里

| 位置 | 内容 |
|---|---|
| `brainos/robot_model.py` | 规则都在这里：`RobotModels`（`register`、`connect_ssh`、`note_device`、`assign`、`target`、`fits`、`robots_for`、`application_fields`、`check_revision`、`bind_skills`、`skill_fields`、`adopt`、`migrate`） |
| `brainos/service.py` | 添加机器人、连接检查、登记技能、保存应用、检查、现场测试、发布、启动各调一处 |
| `brainos/delivery.py` | 交付清单按指明的机器人读；返回 `robots`（可用的机器人）；别的机器人未结束的现场测试不挡这一台 |
| `brainos/revisions.py` | 新版本不能换品牌型号 |
| `brainos/cloud_skills.py` | 发布带品牌型号、`bind`、安装前核对 |
| `cloud/neuxmind_cloud/skill_store.py`、`skill_models.py`、`db.py`、`routes/skills.py` | 云端的两列、发布、指定一次、视图 |
| `web/robot_model.js`、`web/robot_model.css` | 页面：添加机器人的两个输入框、卡片、“机器人应用”按品牌型号筛选、表单里的技能限制、交付清单里选机器人、云端技能的品牌型号 |
| `web/ux04_errors.js` | 新错误码的说明 |

接口：`POST /api/robots/model` `{project_id, robot_id, vendor, model}`；`POST /api/cloud-skills/bind` `{skill_id, version, vendor, model}`；`harness`、`applications/readiness`、`onsite`、`deployments` 多了可选的 `robot_id`；`skills` 多了可选的 `vendor`、`model` 或 `robot_id`。

## 在哪里验证过，哪里没有

- `tests/test_robot_model.py`（18 项）：品牌型号的来源和修改、应用在同型号机器人之间通用和对其他型号的拒绝、证据按台算、技能的归属、旧数据整理。设备是 HTTP 协议替身。
- `tests/test_cloud_skills.py` 里的 `BrandAndModel`（4 项）和 `cloud/tests/test_skill_product.py`（7 项）：发布、再发布、指定一次、安装核对、旧库补列（SQLite）。
- `tests/robot_model_logic.cjs`（29 项）：页面脚本里不碰页面的函数。
- `tests/robot_model_browser.cjs`：在 Chromium 和 WebKit 上走完添加机器人、筛选、编排应用、清单里换机器人、现场测试落到选中的那台、改品牌型号；`tests/cloud_skills_browser.cjs` 按新规则更新。

没有验证的：真机；真机的接入程序是否上报了品牌和型号（ROS 2 适配器默认上报 `unknown`，要在它的配置里填 `vendor`、`model`）；部署在外面的云平台；PostgreSQL 上的补列只由 `db.py` 原有的补列机制保证，这次没有单独在 PostgreSQL 上跑。
