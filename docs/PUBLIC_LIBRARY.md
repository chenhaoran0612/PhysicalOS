# 公共资料库与资料收集（issue #78）

issue #78「资料库数据收集」：把品牌型号清单里每款设备在网上公开的机器人模型（URDF）都抓下来；用户放在 `materials/` 里的两套具身设备资料（宇树 R1-7A 机械臂、幻尔 WonderDuck）也收进来，其中一些要自己到网上或 GitHub 去取。

> 状态：代码和测试完成；数据已经收集到本机（见“这一次收了什么”）。**还没有导入用户正在用的云平台**：导入在部署时做（见“导入”）。没有在真机上用过这些模型。

用户在 issue 会话里定的事（2026-11-28）：

1. **放进公共资料库**：所有账号都能看到（一开始选的是“导入你账号的资料库”，随后改成公共的）。
2. **设备范围是品牌型号清单里的全部**（`web/robot_catalog.js`，添加机器人时可选的那些），网上找不到公开模型的列表说明。
3. **没有许可证的模型也收**，标明“来源仓库没有许可证文件：仅供内部参考，不得再分发”。

## 公共资料库

云端（`cloud/neuxmind_cloud/library_public.py`）：

- 公共资料是一个固定组织 `org_00000000000000000000`（「公共资料库」，没有成员）的普通资料。文件存在资料库卷里这个组织下，解析（WeKnora、规则）和别的资料一样。
- **每个账号都能读**：资料列表里和自己的资料排在一起，`public: true`；可以打开、下载、看解析结果。按品牌型号读资料的地方（修复会话读资料、把安装包送到设备、仿真找机器人模型）也读得到。
- **只有平台运营人员能改**：删除、修改或确认解析结果、改品牌型号。别人做这些得到 403 `public_read_only`。列表里每份资料带 `read_only`，说这个人是不是只能看。
- 一个账号可以另外上传一份和公共资料内容相同的文件，存在它自己的资料库里（两边各算各的）。
- 运营人员给公共资料改品牌型号时，“同名同版本”按公共资料库里的资料判断，不按运营人员自己的资料库。运营人员操作公共资料留下的审计记录记在他自己的组织下（记的是谁做了什么；公共资料库没有成员，记在那里没人看得到）。

改动的地方（都只是几行）：`library.py` 的列表读 `library_public.orgs(p)`（自己的组织和公共资料库），`_own_material` 多一个 `read` 参数（读的时候公共资料也算）；`get_material`、下载、`material_parse.get_result` 用读的方式；解析结果也带 `public` / `read_only`（`editable` 仍然表示“解析完了”）。

桌面端：`brainos/material_library.py` 和 `material_parse.py` 把 `public`、`read_only` 两个字段转给页面。页面（`web/library_public.js`、`web/library_public.css`，包装 `materialRow`、`parsePanel`）：公共资料的名字前面有「公共」；只能看的行不显示「删除」「改品牌型号」；解析结果对只能看的人显示成不能改的样子，并写明只有平台运营人员能修改、确认或删除。

> 和 issue #77 的关系：#77 新加的 `GET /library/products`（资料库页顶上那排品牌型号）只数自己组织的资料。两边合并后要把它的条件改成 `Material.org_id.in_(library_public.orgs(p))`，否则只在公共资料库里有资料的品牌型号不会出现在那排按钮里。#77 让机器人模型也可以是 MJCF、SDF、USD、STP；下面的 WonderDuck、Microduck 的 MJCF 模型和两个 STEP 文件要等 #77 合并后才能被读成模型（在没有 #77 的云端上，MJCF 压缩包会解析失败，STEP 文件不收）。

## 收集：`scripts/library_collect.py`

```bash
python scripts/library_collect.py            # 全部 → ~/code/PhysicalOS-private/library-data
python scripts/library_collect.py --offline  # 只用已经取到本机的
python scripts/library_collect.py --only unitree-go2 hiwonder-wonderduck-mjcf
```

需要 `git`、网络（第一次）和 Python 包 `xacro`（展开 `.xacro`，`pip install xacro==2.1.1`）。来源全部写在 `scripts/library_sources.json`：

| 一节 | 内容 |
|---|---|
| `models` | 每款一个机器人模型：GitHub 仓库和固定的提交（`urdf`、`xacro` 加参数，或仓库里压缩包中的 URDF `archive`），或不公开的模型文件夹（`private`，默认 `~/code/PhysicalOS-private/robot-models`） |
| `local_models` | 从厂家资料里取出的模型（MJCF）：压缩包里的一个文件夹，或一个文件夹（`flatten`：一个 MJCF 引用的网格散在别处时，拷进 `assets/` 并改写引用） |
| `packages` | 一个仓库在固定提交的源码压缩包（SDK、控制程序） |
| `local` | 用户 `materials/` 里的文件（`root: materials`，默认 `~/code/PhysicalOS/materials`），或从网页取下的页面和下载（`root: extra`，默认 `~/code/PhysicalOS-private/library-sources`） |
| `skipped` | 清单里网上没有公开模型的型号和原因 |
| `not_collected` | 厂家资料里没收的文件和原因 |

**展开 `.xacro` 等于运行仓库里的内容**：用隔离的 Python（`-I`，不从检出的文件夹导入任何东西）、只有 `PATH`、一个临时 `HOME` 和语言设置的环境运行，`$(env …)` / `$(optenv …)` 拿不到这台电脑的任何值，不会被写进人人能看的模型；xacro 2.1 自己拒绝 `${…}` 里的 `__…__` 名字。只展开来源表里固定提交的文件，新加 xacro 来源时仍要看一眼。仓库里的压缩包解开时只取普通文件，名字带 `..`、绝对路径、`__MACOSX` 的跳过，解开后最多 2 GB、2 万个文件；下载的源码包最多 2 GB。URDF 必须是 UTF-8，带 DOCTYPE 或实体的不收。

模型包的做法和 `scripts/robot_models.py`（issue #54）相同，函数也是从那里拿的：稀疏检出、只取用到的目录、取到的提交号必须和指定的一致；COLLADA 网格转成 STL（仿真只读 STL / OBJ / MSH）；一个 `<visual>` 里重复的 `<material>` 只留第一个；去掉 `<gazebo>`、`<transmission>`；包里是 `model.urdf`、`meshes/…`、来源的 `LICENSE.txt`、`SOURCE.json`（仓库、提交、许可、做过的改动、每个文件的 SHA-256）。和 #54 不同的地方：**找不到的网格不让整个包失败**，引用原样保留并写进说明（资料库解析时也会报出来）；GLB、FBX 这类仿真读不了的网格原样放进包里。

输出文件夹（不在任何 git 仓库里）：`models/`、`packages/`、`materials/<设备>/`、`manifest.json`（导入用）、`REPORT.md`（收了什么、许可、没收的和原因）。

**仓库里没有任何模型或厂家文件**：`scripts/library_sources.json` 只有网址、提交号和路径。

## 这一次收了什么（2026-11-28）

收集到 `~/code/PhysicalOS-private/library-data`（1.4 GB，145 份，清单和原因在那里的 `REPORT.md`）：

- **机器人模型 116 份**，覆盖品牌型号清单 121 款里的 111 款：其中 39 份由 `.xacro` 展开；15 份来自没有许可证的仓库（仅供内部参考）；智身 L1 Maker、L2-W 和方舟无限 L5 不是这个型号本身的模型，是最接近的一款（说明里写着）；智身 L2-U、星玑 CS1 是厂家文件（星玑 CS1 没有网格）。
- **宇树 R1-7A**：官方 SDK 里的左臂、右臂 URDF，遥操作仓库里带 Dex1 夹爪的单臂和双臂整机，官方 STEP 简化模型；SDK（main、r1-dex-1、algo 三个分支）、遥操作程序、图形化控制台；宇树文档中心的 8 页开发指南（转成 Markdown）和产品手册 PDF。
- **幻尔 WonderDuck**：MJCF 模型（取自厂家的训练工程，在 MuJoCo 里能加载）、整机 STEP；9 份教程和 2 份原理图 PDF、控制程序源码、强化学习训练源码和带已训练模型的训练工程包、Studio 源码。
- **Microduck AD-P102**：应用里仿真套件用的同一个 MJCF（网格拷进包里）。

网上没有公开模型的 10 款（智身 L2、SP1、NE01、RX，Tesla Optimus，Figure 02/03，1X NEO，优必选 Walker S1，众擎 SE01）和没收的厂家文件（WonderDuck 的系统镜像和舵机资料只在百度网盘、各种安装程序、重复文件）都在 `REPORT.md` 里写了原因。

在一个临时的云平台上试导入（SQLite，没有 WeKnora）：145 份里存进 144 份，WonderDuck 的 `.stp` 因为没有 #77 被拒；110 个 URDF 模型读出连杆和关节、等待确认；银河通用 G1 的 URDF 里被动轮关节没有速度和力矩上限，列为缺项（可以由人补上或标“不适用”）；4 个 MJCF / STEP 模型要等 #77；20 份文档要 WeKnora 才能解析；9 个源码 / SDK 压缩包被当作驱动安装包读，显示“没解析出来：缺名称、版本”，文件本身已保存、可以下载，运营人员也可以补上名称和版本后确认。

## 导入

在云平台的服务器上（要直接访问数据库，和 `create-operator` 等管理命令一样）：

```bash
python -m neuxmind_cloud library-import --dir <收集的文件夹> --dry-run   # 只检查标注、类型和大小
python -m neuxmind_cloud library-import --dir <收集的文件夹>
```

本机的云平台在 Docker 里：把文件夹只读挂进容器再运行，例如
`docker compose … run --rm -v ~/code/PhysicalOS-private/library-data:/import:ro cloud python -m neuxmind_cloud library-import --dir /import`（compose 文件和覆盖文件同部署云平台时）。

- 每个文件按上传时的同样规则检查（文件名、类型、内容与后缀相符、大小上限），`kind: model` 的是机器人模型。
- 再运行一次只存新的：同样内容、同样标注的已经在了就跳过（`exists`）；同名同版本但内容不同的报出来、不动（`version_exists`）。
- 存进去的资料进入“解析中”，由云端的解析程序照常处理：URDF 模型读出连杆和关节，等人确认；文档交给 WeKnora。**公共资料只有平台运营人员能确认**。确认之前，机器人模型不会被仿真使用（仿真只用“可用”的模型）。
- 输出一段 JSON：`stored`、`exists`、`refused[]`（文件和原因）；`--dry-run` 时是 `passed`，并且不写数据库。有被拒绝的时退出码是 1。

## 测试

```bash
cd cloud/tests && PYTHONPATH=..:../../spec:. python -m unittest test_library_public   # 公共资料库：导入、谁能看、谁能改
python -m unittest tests.test_library_collect                                         # 来源表覆盖清单、打包、整次收集（不联网）
node tests/library_public_logic.cjs                                                   # 页面：「公共」、只读的行和解析结果
```

`test_library_collect` 里展开 `.xacro` 的一项要装了 `xacro` 才运行，否则跳过。
