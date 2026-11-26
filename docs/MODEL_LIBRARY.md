# 资料库按品牌型号归属，机器人模型单独保存（issue #51）

资料原来各属于一台设备（issue #34）。现在资料库和机器人应用一样，按品牌型号归属（品牌型号见 [ROBOT_MODEL.md](ROBOT_MODEL.md)，issue #48）：同品牌同型号的机器人共用一份资料库。机器人模型（URDF）从普通资料里分出来，单独一栏，每个品牌型号用其中确认过的最新一份，仿真就用它（[MUJOCO_SIM.md](MUJOCO_SIM.md)）。

> 状态：代码和测试完成；**没有在任何真机上运行过，也没有在用户正式使用的云平台上运行过**（本机云平台要随这一版重建才有新接口）。验证范围见最后一节。

用户在 issue 会话里定的事（2026-11-26）：

1. **每个品牌型号一份机器人模型**，和其他资料分开管理；可以是单个 `.urdf`，也可以是带网格文件的压缩包。
2. **旧资料自动归到设备的品牌型号**；设备没有品牌型号的，资料留在原设备上，等填了品牌型号再归过去。机器人上的程序和场景数据（issue #43 的 bundle）仍然只属于一台设备。

## 规则

### 一份资料属于谁

- 资料上的 `vendor`、`model` 两个标注就是它所属的品牌型号（比较时不分大小写，多余空白并掉）。一台机器人用的资料，是标注等于它的品牌型号的那些。
- 上传时写的 `device_id` / `device_name` 云端不再保存（bundle 除外）：同一个文件、同样的标注，第二台设备再传会得到 409 `material_exists`。
- 还绑在一台设备上的资料（这一版之前上传、还没有归过去的）只算那一台的。
- 没有品牌型号的机器人没有资料库：设备档案的「资料」里不能上传，提示先填品牌型号；资料库里的安装包也送不到它上面（`robot_model_missing`）。

### 旧资料怎么归过去

云端不知道哪台机器人是什么品牌型号（机器人在控制台里），所以由控制台告诉它：`POST /library/materials/rebind` `{device_id, vendor, model}`，每台机器人、每个品牌型号只说一次，记在工作区的 `settings/library_rebind` 里。打开「资料库」页、打开设备的「资料」、修复会话读资料时都会补做；云平台没登录、连不上或没有权限时不算做过，下次再试。机器人后来改了品牌型号，会按新的再说一次（已经归到旧品牌型号下的资料不跟着走，它们属于那个品牌型号）。

云端对这台设备的每份资料（bundle 除外）：

| 情况 | 结果 |
|---|---|
| 那个品牌型号下没有它 | 改标注、解除设备绑定（`moved`） |
| 那里已有内容相同的一份（同标注、同 SHA-256） | 两份留一份：确认过（可用）的那份留下，都确认或都没确认时留原来就在那里的（`merged`）。文件本身还在 |
| 那里已有同名同版本、内容不同的一份 | 不动，仍然绑在这台设备上，在答复的 `kept[]` 里列出（不改名、不覆盖） |

### 改一份资料的品牌型号

标错了，或者上传时机器人还没有品牌型号：`PUT /library/materials/{id}/product` `{vendor, model}`。那里已有同内容的一份 → 409 `material_exists`；已有同名同版本的另一份 → 409 `version_exists`；bundle → 409 `bundle_needs_device`。页面上是每行的「改品牌型号」。已经确认为「可用」的资料改到**另一个**品牌型号后回到「待确认」，要有人对着新的品牌型号再核对一次（只是改写法、仍是同一个品牌型号时不变）。自动迁移不改状态：资料本来就是为那台设备确认的。

### 机器人模型（kind `model`）

- 上传时带 `kind: "model"`。只接受单个 `.urdf`，或装着**恰好一个** `.urdf` 和它的网格文件的 `.zip` / `.tar.gz`（`.tgz`）。`.xacro` 不接受（要先用 ROS 的工具展开成 `.urdf`，这里不运行它）。大小上限 `NEUXMIND_LIBRARY_MAX_MODEL_BYTES`，默认 512 MiB。
- 同一个 `.urdf` 可以既作为文档又作为机器人模型各存一份（两份资料）。不带 `kind` 时和以前一样：`.urdf` 是文档，压缩包是驱动安装包。
- 解析和 URDF 文档一样（连杆、关节、四个上限），在同一个受限的子进程里做；结果给人看、可以改、确认后才是「可用」。压缩包只读里面那一个 `.urdf`，其余成员只看名字；另外报告：
  - URDF 用到、但包里没有的网格文件；
  - 仿真读不了的网格文件（只读 `.stl`、`.obj`、`.msh`；`.dae` 等读不了）；
  - 单个 `.urdf` 用到网格时，提示把它们打成压缩包再传。
  这些只是提示，不影响确认：仿真里对应的部分不显示、不参与碰撞。
- 包里没有 `.urdf` → 没解析出来（`model_no_urdf`）；有多个 → 没解析出来（`model_many_urdf`，列出名字）；名字指向包外的 `.urdf` 不读。
- **每个品牌型号用哪一份**：状态「可用」的机器人模型里最新上传的那一份。要换，就上传新版本并确认。
- 云端从不解开它。解开在控制台做，见 [MUJOCO_SIM.md](MUJOCO_SIM.md) 的“机器人模型到本机”。

### 跟着变的地方

- **修复会话读资料**（issue #30，[REPAIR_MATERIALS.md](REPAIR_MATERIALS.md)）：读这台机器人品牌型号下「可用」的资料，加上还绑在它身上的。同品牌型号的两台机器人读到同一批。
- **把安装包送到设备**（issue #31，[PACKAGE_DELIVERY.md](PACKAGE_DELIVERY.md)）：只送这台机器人品牌型号的安装包（否则 `package_other_model`），或者还绑在它身上的。以前“没有绑设备的包可以送到任何机器人”不再成立。
- **解析**（issue #34，[MATERIAL_PARSING.md](MATERIAL_PARSING.md)）：同一个文件在两个品牌型号下各存一份时仍然只送知识库一次、只问一次模型。发给知识库的元数据不再带设备编号。

## 页面

`web/model_library.js`、`web/model_library.css`（在 `material_library.js`、`material_parse.js`、`robot_model.js` 之后加载，包装它们的函数）：

- **「资料库」页**
  - 「上传资料」先选**品牌型号**（这个账号里机器人的品牌型号，写着是哪几台），本体类型跟着它；也可以选“其他品牌型号（手动填写）”，为还没添加的机器人准备资料。
  - 新的一栏「机器人模型（URDF）」：每个品牌型号一行，写着当前使用的那一份、还没确认的、更早确认的；「上传机器人模型」只收 `.urdf` / `.zip` / `.tar.gz`。没有可用模型的品牌型号写明“仿真需要它”。
  - 列表可以按品牌型号筛选；每行多了「改品牌型号」。
- **设备档案 →「资料」**：列出这台机器人品牌型号的资料，写明和哪几台共用；上传时品牌型号是机器人自己的，只填版本。没有品牌型号的机器人只看到以前传给它的资料，上传处提示先填品牌型号。

## 接口

云端（前缀 `/api/v1`，权限同资料库的写操作）：

| 方法 路径 | 说明 |
|---|---|
| `POST /library/uploads` | 多了可选的 `kind: "model"`。415 `unsupported_type`（不是 `.urdf` / `.zip` / `.tar.gz`）、413 `file_too_large` |
| `GET /library/formats` | 多了 `model: {extensions, max_bytes}` |
| `GET /library/materials?kind=model&vendor=…&model=…` | 某个品牌型号的机器人模型（`vendor`、`model` 本来就是不分大小写的精确匹配） |
| `POST /library/materials/rebind` | `{device_id, vendor, model}` → `{moved, merged, kept[], more}`，可以重复调用；一次最多处理 2000 份，`more` 为真时控制台下次接着做 |
| `PUT /library/materials/{id}/product` | `{vendor, model}` → 资料 |

桌面端后台（`brainos/model_library.py`）：`library/products`（= `library/overview` 加 `products[]`、`rebound[]`，并先补做迁移）、`library/robot` `{robot_id}`（这台机器人的资料、品牌型号、和谁共用）、`library/move` `{material_id, vendor, model}`。`library/upload/start` 多转发一个 `kind`。

## 代码在哪里

| 位置 | 内容 |
|---|---|
| `cloud/neuxmind_cloud/library_model.py` | kind `model`：接受什么、大小上限、按 URDF 解析 |
| `cloud/neuxmind_cloud/library_product.py` | 迁移（`rebind`）和改一份资料的品牌型号（`move`） |
| `cloud/neuxmind_cloud/material_extract.py` | `model.urdf` / `model.zip` / `model.tar.gz` 三种读法（`ModelMembers`、`model_facts`） |
| `cloud/neuxmind_cloud/library.py`、`material_parse.py`、`routes/library.py` | 各几行：上传时的归属、解析时的种类、两条新路由 |
| `brainos/model_library.py` | 一份资料是不是这台机器人的（`owned`）、迁移（`sync`）、页面要的三个接口、送包前的核对 |
| `brainos/repair_materials.py`、`package_delivery.py`、`material_library.py` | 按品牌型号取资料、送包；转发 `kind` |
| `web/model_library.js`、`web/model_library.css` | 页面 |

## 在哪里验证过，哪里没有

- `cloud/tests/test_library_model.py`（15 项）：三种读法、缺网格和读不了的网格、没有或多个 URDF、DOCTYPE、大小上限、同一文件作为文档和模型。
- `cloud/tests/test_library_product.py`（12 项）：新上传不再按设备分开、迁移的三种情况、谁可以做、改一份资料的品牌型号。
- `tests/test_model_library.py`（8 项）、`tests/test_repair_materials*.py`、`tests/test_package_delivery*.py`（按新规则改过）：控制台这一侧，云端用替身或本机启动的真云端程序。
- `tests/model_library_logic.cjs`（47 项）、`tests/mujoco_sim_browser.cjs`（Chromium 和 WebKit）：页面。

没有验证的：真机；部署在外面的云平台；PostgreSQL（这次没有加列，迁移只改已有列的值；云端全套只在 SQLite 上跑过）；用户已有的真实资料的迁移（本机云平台上的那几份要等部署后看 `rebound` 的结果）；很大的带网格压缩包（测试里的模型只有几百字节）。

已知的限制：

- 设备档案里“同名同版本、内容不同”而留在原设备上的旧资料，页面上和别的资料列在一起，没有单独标出为什么没归过去（云端的答复里有原因，页面没有显示）。
- 资料的版本、本体类型标注仍然不能改，只能改品牌型号。
