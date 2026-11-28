# 资料库先选品牌型号，机器人模型放进资料里，模型有更多格式（issue #77）

issue #77 的原话：资料库进来，先筛选品牌和型号，再展示资料。URDF 文件不要放在最上面，放在资料中。模型还可以是其他格式的，例如现在小黄鸭用的那种。（之后补充：还需要支持 STP 格式。）

> 状态：代码和测试完成；没有在真机上用过，也没有在用户正式使用的云平台上用过（本机云平台要随这一版重建才有新格式和 `GET /library/products`）。验证范围见最后一节。

用户在 issue 会话里定的事（2026-11-28）：

1. **进来先选品牌型号**：页面最上面一排品牌型号，选中一个，下面才列出它的资料；只有一个品牌型号时直接选中；记住上次选的。
2. **一个上传入口**：只留「上传资料」，对话框里选用途「资料」或「机器人模型」。机器人模型和其他资料列在同一张表里，仿真正在用的那份标「仿真使用中」；这个品牌型号还没有可用模型时，列表上方一句提示。
3. **模型格式**：除了 URDF，还收 MJCF（小黄鸭用的，MuJoCo 自己的格式）、SDF（Gazebo）、USD（Isaac Sim）、STP（STEP 三维 CAD 文件）。仿真（MuJoCo）只能读 URDF 和 MJCF；SDF、USD、STP 只保存，写明仿真暂时用不了。
4. **MJCF 在仿真里和 URDF 一样用**：只取机器人本身，去掉文件自带的地面、灯光、场景物体和自由关节，按现有规则给关节加执行器，放进仿真自己的环境；不模拟步态。

## 页面（`web/model_library.js`、`web/model_library.css`）

- 「资料库」页头下面是「品牌型号」一排按钮：账号里机器人的品牌型号在前，然后是云平台上有资料、但还没有这种机器人的品牌型号。每个按钮写着本体类型、有几份资料、有没有仿真模型。
- 选中的那个决定下面的列表（列表标题是「<品牌 型号> 的资料」，数量也是它的）。没有选中时只有一句「先在上面选一个品牌型号」。选择记在这台电脑的浏览器存储里（`nx.library.product`），读不到时（隐私窗口）只在这次打开有效。
- 列表里机器人模型一行写「机器人模型 · URDF / MJCF / SDF / USD / STP / ZIP 压缩包…」。这个品牌型号最新确认为「可用」的那一份写「仿真使用中」；更早确认的写「可用」。SDF、USD、STP 写「已入库」，下面一行是云端的说明（例如“SDF 模型，已保存；仿真（MuJoCo）暂时用不了这种格式”）。
- 列表上方：有仿真模型时一行“仿真使用的机器人模型：…”；没有时一条提示和「上传机器人模型」按钮（打开同一个上传对话框，用途选好「机器人模型」）。
- 「上传资料」对话框：先选用途。「资料」可以多选文件，后缀按云端的文档和安装包清单；「机器人模型」只能一个文件，后缀是 `.urdf .xml .sdf .usd .usda .usdc .usdz .stp .step .zip .tar.gz .tgz`。品牌型号默认是页面上选中的那个。上传后列表切到这些文件的品牌型号（新的品牌型号也会先出现在上面那排里）。
- 列表的筛选只剩本体类型和搜索；原来的“全部品牌型号”下拉没有了（由上面那排代替）。原来的「机器人模型（URDF）」一栏去掉了。
- 设备档案 →「资料」不变，只是提示改成“机器人模型请到「资料库」页上传”。

## 云平台

| 位置 | 改了什么 |
|---|---|
| `library_types.py` | `MODEL_ONLY`：`mjcf`（`.xml`，内容里要有 `<mujoco>`）、`sdf`（`<sdf>`）、`usd`（`.usd .usda .usdc`，开头是 `#usda ` 或 `PXR-USDC`）、`usdz`（ZIP 结构）、`stp`（`.stp .step`，开头是 `ISO-10303-21`）。这些只在 `kind: "model"` 时接受，不是文档类型：不在 `FORMATS`、文档后缀清单和上传说明里 |
| `library_model.py` | 模型后缀加上这些；`kept_only` / `kept_detail` / `refuse_edit`：SDF、USD、STP 只保存 |
| `library.py` | 上传时先按 `kind` 认模型专用的后缀，再判断“不支持这种文件类型” |
| `material_mjcf.py`（新） | 在受限子进程里按规则读 MJCF：`<include>`（只读同一个压缩包里的；和 MuJoCo 一样先按主文件所在目录找，每一份被引用的内容都计入元素上限）、默认类（`class`、上层刚体的 `childclass`、`main`）、角度单位（默认度，`<compiler angle="radian">` 时弧度）、`autolimits`（和 MuJoCo 一样只有下限小于上限才算有范围；`autolimits="false"` 时有 range 没写 limited 的关节写一句“仿真会建不起来”）、力矩（`actuatorfrcrange`，否则驱动它的执行器的 `forcerange`，`motor` 用控制范围 × 传动比；范围是 0 0 的不算）。没有名字的关节列为 `unnamed_joint_<n>` / `unnamed_free_<n>`，跳过已经有人用的名字（控制台在仿真里给它同一个名字）。球关节不列出，写一句说明 |
| `material_extract.py` | 压缩包里的模型：一个 `.urdf` 或一个 MJCF（`<mujoco>` 根、没有被别的 `.xml` include 的那个）读出来；两个以上 → 没解析出来（`model_many_urdf`）；只有 SDF / USD / STP → 只保存；都没有 → 没解析出来（`model_no_urdf`） |
| `material_fields.py` | 关节可以带 `unstated`：这种格式没有的上限（MJCF 没有速度上限），不算“缺” |
| `material_parse.py` | 只保存的模型直接是「已入库」，说明写在状态下面；解析结果 `editable: false`、`kept_only`；修改或确认 → 409 `model_kept_only` |
| `library_product.py`、`routes/library.py` | `GET /library/products`：本组织有资料的品牌型号（bundle 除外），每个带资料数 `materials` 和可用的机器人模型数 `usable_models`，最近有资料的在前，最多 500 个 |

## 控制台

- `brainos/model_library.py`：`library/products` 的 `products[]` = 机器人的品牌型号 + 云端有资料的品牌型号（同一个品牌型号合并，带上数量）。云平台是这一版之前的（`GET /library/products` 是 404）就只有机器人的；别的错误照常报出来。
- `brainos/mujoco_model.py`：模型可以是 `mjcf`（单个 `.xml`），压缩包里找一个 `.urdf` 或一个 MJCF；准备好的目录里是 `model.xml`（MJCF）或 `model.urdf`，`model.json` 多了 `model_format`。
- `brainos/mujoco_mjcf.py`（新，不加载 MuJoCo）：把 MJCF 写成 `model.xml`，只引用这里写下的文件：
  - `<include>` 换成被引用文件的内容（只从同一个压缩包里取，指向包外的不读，写一条警告）；
  - `<mesh file>` 指向 `meshes/m<N>.<后缀>`（和 URDF 一样只收 `.stl .obj .msh`，文本 STL 转成二进制）；找不到或读不了的网格连同用它的几何体一起去掉；
  - 其余所有引用文件的元素（贴图、高度场、皮肤、要挂接的模型、柔体网格）去掉，引用它们的地方一起去掉（材质保留颜色）；默认类里的文件名去掉；
  - 机器人用不到、大小由文件自己写的元素也去掉：内置的贴图和高度场、柔体、`<replicate>`、`<composite>`、插件、`<size>`、`<visual>`、`<statistic>`（代码审查时一个几十字节的 `<hfield nrow="200000" ncol="200000">` 让 MuJoCo 用了 11.5 GB）；
  - 没有名字的网格保留 MuJoCo 原来给它的名字（文件名去掉目录和后缀），否则用它的几何体找不到它；
  - `<include>` 展开后的元素总数有上限（20 万），一个文件被引用多次每次都算；
  - `<compiler>` 的目录换成 `meshes`，其余设置（角度单位、`autolimits` …）保留；
  - 没有名字的关节起名 `unnamed_joint_<n>`，和云端的解析结果一致。
- `brainos/mujoco_mjcf_build.py`（新，只在仿真进程里加载 MuJoCo）：机器人是世界下面关节最多的那棵刚体树；其余的（地面、灯光、相机、标记点、别的刚体）去掉；去掉自由关节和文件给它的位置（由仿真来放，站立高度和 URDF 一样算：不去掉时，文件里放在 0.2 米高的小黄鸭会离地 13 毫米）；文件自己的执行器、传感器、关键帧、接触对和排除、肌腱去掉；关节耦合（一个关节跟随另一个的 equality）保留，跟随的关节不加执行器，和 URDF 的 `mimic` 一样。关节的类型和上限用 MuJoCo 编译后的结果（默认类、度换成弧度都已处理），力矩取 `actuatorfrcrange`，否则取文件里驱动它的执行器能给的。质量和惯量的下限和 URDF 一样，没有几何体的刚体也能编译。
- `brainos/mujoco_build.py`：`model_format` 是 `mjcf` 时用上面这个读机器人，其余不变（载体、执行器、站立高度、碰撞规则和 URDF 一样）。挂接前把仿真自己的 `<option>`（时间步长、积分器、求解器次数…）抄给机器人：文件自己写的设置本来就不用，不抄的话 MuJoCo 4 每次挂接都会警告“Attach conflict”，并在当前目录写一个 `MUJOCO_LOG.TXT`。
- 仿真缺模型时的提示改成“上传它的 URDF 或 MJCF 模型”。

## 在哪里验证过，哪里没有

独立的代码审查（2026-11-28）找到并已修好：没有名字的网格改名后建不起来；几种元素能让 MuJoCo 吃光内存；改写时同级元素多会变成平方级耗时；嵌套 include 被当成第二个模型；范围 `0 0`、`1 0` 和力矩 `0 0` 列出的上限和 MuJoCo 实际用的不一样；起的名字可能和已有的撞；上传后的品牌型号按钮在列出来之后还留着。每一条都有对应的测试。审查没有找到能让 MuJoCo 读到准备目录以外文件的写法。

- `cloud/tests/test_library_model_formats.py`：MJCF 的读法（默认类、度、驱动、球关节、没有名字的关节、场景内容）、include、两个模型、DOCTYPE、SDF / USD / STP 只保存、内容和后缀不符被拒、模型格式不是文档、`GET /library/products`。`cloud/tests/test_library_model.py` 的格式清单跟着改了。
- `tests/test_library_by_product.py`：品牌型号清单的合并和旧云平台；MJCF 的准备（只留这里写的文件、include、网格目录、拒绝的情况）；MuJoCo 里建出来的机器人（关节、力矩、跟随、去掉的东西、站立）。`tests/test_mujoco_sim.py` 一条提示文字跟着改了。
- `tests/model_library_logic.cjs`：选哪个品牌型号、那一排、模型提示、模型行。
- `tests/mujoco_sim_browser.cjs`：按新的页面走一遍（自动选中、从提示上传模型、确认后「仿真使用中」、文档和模型列在一起），再建仿真环境。
- 用小黄鸭训练包里的真实 MJCF（`reports/adp102/scene.xml`，38 个网格、28 个关节）和 #78 要导入的 WonderDuck MJCF（照 #78 的打包方式：`scene.xml`、它包含的 `robot_groundcontact.xml`、`assets/`）在本机跑过云端读取、准备和建模：都只有一个主文件，网格齐全，站立时最低处离地 3 毫米（本地文件，没有进仓库）。#78 的 STEP 文件（`鸭子.stp`，471 MB）开头是 `ISO-10303-21;`，符合检查，大小在机器人模型上限 512 MiB 以内。

没有验证的：真机；外面部署的云平台；PostgreSQL（`GET /library/products` 用的是 `lower()`、`min()`、`count()`、`sum(case …)`，云端全套只在 SQLite 上跑过）；很大的 STP / USD 文件（测试里只有几十字节）。

已知的限制：

- 压缩包里的模型是哪种格式，列表上只写“ZIP 压缩包”；要看是 URDF 还是 MJCF，打开解析结果。
- MJCF 文件里的多个机器人只用关节最多的那一个；`<replicate>`、`<composite>` 等生成的关节解析结果里不列出（仿真里 MuJoCo 照常生成）。
- SDF、USD、STP 不解析，不能确认，仿真不用。
