# 仿真机器人按类型、品牌、型号添加（issue #54）

「添加仿真机器人」原来只有两个编造的机器人可选（四足、机械臂）。现在先选**类型**，再选**品牌**和**型号**，启动的是这个型号自己的 URDF 模型在 MuJoCo 里的仿真。每个支持的品牌型号在应用里有一份**基础档案**：类型、图片、机器人模型从哪里来、站立时各关节的角度。

> 状态：代码和测试完成，八个型号在本机的浏览器里逐个添加、启动、看到画面（Chromium）。**没有和任何真机对比过；行走不是物理的**（见 [MUJOCO_SIM.md](MUJOCO_SIM.md)）。验证范围见最后一节。

用户在 issue 会话里定的事（2026-11-26）：

1. 「历史已经接入数据删除」指**旧的仿真机器人**：账号里已有的仿真机器人和它们的运行记录删除一次；真机和它们的档案、记录不动。
2. 「设备信息做成基础档案」指**随应用内置**：选了类型、品牌、型号就能启动，不用先到资料库上传模型，也不需要登录云平台。
3. 智身科技和星玑科技的模型是厂家文件，**不进公开仓库，只进安装包**。
4. 「智源」是智元机器人（AgiBot）。
5. 现在的 L2U 就是**智身科技 钢镚 L2-U**，CS1 是**星玑科技 CS1**。

## 支持哪些

类型按 issue 的顺序：机械臂、轮足机器狗、四足机械狗、双足机器人、其他。

| 品牌 | 型号 | 类型 | 模型来源 | 许可 | 说明 |
|---|---|---|---|---|---|
| 宇树科技 | Go2 | 四足机械狗 | [unitree_ros](https://github.com/unitreerobotics/unitree_ros) `robots/go2_description` | BSD-3-Clause | 官方网格是 COLLADA，打包时转成 STL，颜色没有保留 |
| 宇树科技 | G1（23 自由度） | 双足机器人 | 同上 `robots/g1_description/g1_23dof.urdf` | BSD-3-Clause | |
| 宇树科技 | Z1 | 机械臂 | 同上 `robots/z1_description` | BSD-3-Clause | 网格同样由 COLLADA 转换；URDF 里没有夹爪 |
| 云深处科技 | 绝影 Lite3 | 四足机械狗 | [deep_robotics_model](https://github.com/DeepRoboticsLab/deep_robotics_model) `Lite3` | BSD-3-Clause | |
| 云深处科技 | 山猫 M20 | 轮足机器狗 | 同上 `M20` | BSD-3-Clause | |
| 智元机器人 | A3 | 双足机器人 | [A3-A3U-robot-model](https://github.com/AgibotTech/A3-A3U-robot-model) `a3_t2d5` | 木兰宽松许可证第 2 版 | |
| 智身科技 | 钢镚 L2-U | 轮足机器狗 | 厂家文件（URDF 和网格），不公开 | — | |
| 星玑科技 | CS1 | 四足机械狗 | 厂家文件（从设备读到的 URDF），不公开 | — | **没有网格文件**：外形是按关节位置画出的方块和圆柱，不是真实外形；站立姿态是按 URDF 估的 |
| NeuxMind 示例 | 通用四足、通用六轴机械臂 | 四足机械狗、机械臂 | 没有模型文件 | — | 应用原来就有的两个仿真机器人，不经过 MuJoCo |

「其他」类型下是 Microduck AD-P102（issue #63 加入、issue #65 归到这个类型，见 [DUCK_SKILLS.md](DUCK_SKILLS.md)；它不是按 URDF 建的，预览里的说明也不同）。某个类型下没有能添加的型号时，对话框里会写明。

对话框的大小不随所选类型变（issue #65）：预览框没有图片时和有图片时一样高（`web/sim_profiles.css` 的 `.sp-preview`）。原来选到没有图片的类型时对话框会矮 22 像素并重新居中，在桌面应用里表现为点击「其他」后画面闪一下、标题栏和按钮栏错位。验收脚本 `tests/sim_other_browser.cjs`（配 `tests/duck_browser_fixture.py`）逐个点类型，对比对话框各部分的位置和大小。

这五个类型也是添加真机时「本体类型」的内置清单（issue #58，见 [BODY_TYPES.md](BODY_TYPES.md)）：改 `TYPES` 就是同时改两处。
从档案添加的仿真机器人，登记的类型是档案的类型（轮足的 L2-U 是轮足机器狗，不再是四足）；仿真里用的仍是 `family`。

没有找到可以用的公开模型、所以这一版没有的：智元的机器狗（D1 Max / MaxPro 的仓库没有许可证）、灵犀 X1（URDF 所在的仓库没有许可证文件）、远征 A2（没有公开的 URDF）；宇树 Go2-W、B2-W（网格更大，这一版每个品牌先做一到三款）。

固定的提交号、每个型号取哪些文件，都写在 `brainos/sim_profiles.py` 的 `PROFILES` 里。

## 仓库里没有任何机器人模型

模型文件不在这个仓库里（`tests/test_sim_profiles.py` 有一项检查仓库里没有 `.urdf`、`.stl`、`.dae`、`.step`）。打包时由 `scripts/robot_models.py` 做成每个型号一个包：

```bash
python scripts/robot_models.py                 # → .build/robot-models/<编号>.zip 和 NOTICE.txt
python scripts/robot_models.py --offline       # 只用已经取到本机的
python scripts/robot_models.py --private DIR   # 不公开的模型在哪个文件夹
```

- **公开的**：用 `git` 从各自的 GitHub 仓库按固定的提交取（稀疏检出，只取用到的目录），缓存在 `.build/robot-model-sources/`。取到的提交号和指定的不一致就不用。
- **不公开的**（智身 L2-U、星玑 CS1）：从打包电脑上的一个文件夹读，默认 `~/code/PhysicalOS-private/robot-models/`（可以用 `--private` 或环境变量 `NEUXMIND_PRIVATE_ROBOT_MODELS` 指到别处），里面是 `zsibot-l2u/`（`urdf/`、`meshes/`）和 `xingji-cs1/`（`robot_description.urdf`）。这个文件夹不在任何 git 仓库里。**没有这个文件夹的电脑上打出的安装包没有这两款**，对话框里它们显示为不能添加并写明原因，其余型号照常。
- 每个包里有 `model.urdf`、`meshes/…`、来源的 `LICENSE.txt`、`SOURCE.json`（来源仓库和提交号、许可、做过哪些改动、每个文件的 SHA-256）。`NOTICE.txt` 汇总每个包的来源和许可，随包一起进安装包。同样的输入打出的包逐字节相同。
- 打包时做的改动：COLLADA 网格转成二进制 STL（仿真只读 STL / OBJ / MSH）；一个 `<visual>` 里重复的 `<material>` 只留第一个（MuJoCo 的 URDF 读取器不接受两个）；去掉 `<gazebo>`、`<transmission>`；`stand_in_shapes` 的型号（只有 CS1）给没有网格文件的连杆画上简单形状。没有声明 `stand_in_shapes` 的型号缺了网格就不打这个包。
- 只在这个型号自己的目录里找网格（一个仓库的检出由几个型号共用）；是符号链接的文件、指到来源目录之外的文件不读。
- 这一次打不出来的型号（没有网络又没有取过、不公开的文件夹不在、文件有问题），连它以前打出的包也一并去掉：安装包里不会留着不是这次要的旧包。
- `scripts/build_macos.py` 打包前会运行它，把 `.build/robot-models/` 收进后台程序的 `robot_models/`（现在八个包共约 36 MB）；`--check` 的报告里有 `robot_models`（这次能仿真的型号）。取不到的包只是被跳过，不会让打包失败。

应用在哪里找这些包：环境变量 `NEUXMIND_ROBOT_MODELS_DIR`；打包后的应用里是后台程序自带的 `robot_models/`；在源码里运行是 `.build/robot-models/`。

## 添加一台

`POST /api/sims` `{profile_id, project_id?}`（`brainos/sim_profiles.py` 的 `SimProfiles.add`）：

1. 「NeuxMind 示例」的两款走原来的路径（`SimRobots.add`）。
2. 其他型号：没有 MuJoCo 引擎或没有这个型号的包 → `sim_profile_unavailable`。包解开到 `<工作区>/robot-models/<包的 SHA-256>/`（和资料库里的机器人模型用同一种解法、同一个目录，见 [MUJOCO_SIM.md](MUJOCO_SIM.md)「机器人模型到本机」；同一个包只解一次）；解不开 → `sim_model_invalid`。
3. 在空场地里启动一台 MuJoCo 仿真机器人：上报的品牌型号就是档案里的品牌型号，`simulated: true`，记录上有 `sim.profile_id`。它和 issue #51 的仿真机器人是同一种（同样的能力、画面、关闭和删除、应用关闭后再打开自动恢复），只是不属于某个应用的仿真环境。
4. 档案里的站立姿态（`pose`，按关节名的通配写法）交给仿真：机器狗的 URDF 零位是腿伸直，膝关节的零位常常在它自己的限位之外。角度会被限制在关节自己的限位以内。

旧的写法 `{kind: 'quadruped' | 'arm'}` 仍然可用（测试和脚本里很多地方这样写），页面不再这样发。

`GET /api/state` 多了 `sim_catalog`：`types[]`（`id`、`name`）和 `profiles[]`（`id`、`vendor`、`model`、`type`、`image`、`builtin`、`licence`、`available`、`problem`）。

仿真里什么是真的、什么不是，和 issue #51 完全一样：形状、质量、关节来自 URDF；**行走直接移动机身，不模拟步态**，腿和轮子不产生推进力；机器人不和地面、不和自己碰撞。轮足机器狗在仿真里和四足一样移动。

## 同一个品牌型号的不同写法

真机上报的品牌型号常常是另一种写法：现在那台 L2U 上报的是 `zs` / `L2U`。品牌型号的比较（`robot_model.key`，机器人应用和技能按它判断“是不是同一个品牌型号”）现在先换成档案里的写法再比：`zs`、`智身`、`zsibot` 都是智身科技，`L2U`、`L2 Ultra`、`钢镚 L2U` 都是钢镚 L2-U；`星玑`、`xingji` 都是星玑科技。所以给真机 L2U 做的应用可以在「智身科技 钢镚 L2-U」的仿真机器人上检查和试运行。清单在 `sim_profiles.py` 的 `VENDOR_ALIASES`、`MODEL_ALIASES`；不认识的品牌照原样比较。

页面上比较品牌型号的两处（`web/robot_model.js` 的 `rmKey`、`web/task_apps.js` 的 `taKind` / `taRobotKind`）用同一张表：它随状态下发（`product_aliases`），页面用 `spCanon()` 换成档案里的写法再比，所以任务页上真机的应用和同型号的仿真机器人也对得上。清单之外的型号（例如 `zs / L2-W` 和 `智身科技 / 钢镚 L2-W`）仍按字面比较。

记录里保存的仍是原来的写法，没有改写任何已有记录。**云端资料库不知道这份清单**：资料库仍按字面（不分大小写）匹配品牌型号，`zs / L2U` 下的资料和 `智身科技 / 钢镚 L2-U` 下的资料在云端是两处。

`web/robot_catalog.js`（issue #53 的品牌型号下拉列表）跟着改了名字：「星玑」改为「星玑科技」，「钢镚 L2U」改为「钢镚 L2-U」，原来的写法仍然认得；智身科技多认一个 `zs`；智元机器人加了 A3；Lite3、M20、A3 有了图片（用它们的公开模型渲染的，不是产品照片）。

## 旧的仿真机器人删除一次

每个工作区打开时做一次（`SimProfiles.remove_old_sims`，做过后记在 `settings/sim_profiles`）：删除所有没有 `sim.profile_id` 的仿真机器人——原来的通用四足、通用机械臂、issue #51 在应用的仿真环境里启动的 MuJoCo 机器人——连同它们的状态目录（装在上面的技能）和指向它们的操作记录（`operations`），每台写一条审计事件 `robot.sim_removed`（`reason: issue-54`）。**不是仿真的机器人一律不动**。要删哪几台先记下来再动手，所以中途被打断（应用被关掉、断电）下次打开会接着做完，包括记录已经删掉、操作记录还在的那些。之后添加的仿真机器人不会再被删除，包括用旧写法 `{kind}` 添加的和在应用的仿真环境里启动的。

应用、测试报告、部署记录里提到这些机器人的地方不改（和手动删除仿真机器人时一样）；应用的仿真环境（`mujoco_scenes`）留着，可以在里面重新启动机器人。

## 页面

`web/sim_profiles.js`、`web/sim_profiles.css`（替换 `web/sim.js` 的 `simAddForm`，包住 `simSubmit`；没有改 `web/sim.js`）：

- 五个类型是一组单选（方向键可以切换），每个写着现在能添加几款；选了类型，品牌和型号两个下拉跟着变，默认是第一个能添加的；下面是这个型号的图片和一句它是怎么仿真的。
- 不能添加的型号仍列在下拉里，灰着，括号里写原因；一个类型没有型号、或一个品牌的型号都不能添加时，说明原因，「添加并启动」不可点。
- 对话框打开时停在四足机械狗里第一个能添加的型号。

浏览器脚本里原来勾选旧的两种的地方，改成 `require('./sim_profiles_helper.cjs').pickSimProfile(page, 'quadruped' | 'arm' | <档案编号>)`。

## 代码在哪里

| 位置 | 内容 |
|---|---|
| `brainos/sim_profiles.py` | 类型、档案清单（来源、姿态）、别的写法；`SimProfiles`：清单给页面、添加一台、旧仿真机器人删除一次 |
| `scripts/robot_models.py` | 取模型、COLLADA 转 STL、替代形状、打包、`NOTICE.txt` |
| `brainos/mujoco_build.py`、`mujoco_body.py` | 多一个 `pose`（起始和保持的关节角度）；有外观形状的连杆不再同时画出它的碰撞形状（只有碰撞形状的连杆照旧画出） |
| `brainos/robot_model.py`、`web/robot_model.js`、`web/task_apps.js` | 比较品牌型号前先换成档案里的写法 |
| `brainos/service.py`、`server.py` | 接上：状态里的 `sim_catalog`、`sims` 路由按 `profile_id` 分流、打开工作区时的一次删除、两个静态文件 |
| `scripts/build_macos.py` | 打包前生成模型包并收进后台程序；打包后的后台冒烟多一步：列出档案并启动一台 |
| `web/sim_profiles.js`、`web/sim_profiles.css` | 对话框 |
| `web/robot_catalog.js`、`web/robot_images/` | 名字、三张新图片及来源 |

## 测试

```bash
python -m unittest tests.test_sim_profiles         # 清单、别的写法、添加（编造的包，真的 MuJoCo 机器人）、旧仿真删除一次；本机有真实的包时逐个建模并检查站得住
python -m unittest tests.test_robot_models_pack    # 打包工具：COLLADA、替代形状、包的内容、逐字节相同
node tests/sim_profiles_logic.cjs                  # 对话框的逻辑
python tests/ux01_browser_fixture.py CONFIG.json & # 浏览器验收的夹具
node tests/sim_profiles_browser.cjs CONFIG.json    # BRAIN_BROWSER=webkit 换成 WebKit；本机有哪些包就启动哪些型号
python scripts/build_macos.py --freeze-check       # 打包后的后台：列出档案，启动第一个有包的型号
```

`PYTHONPATH` 和其他桌面端测试相同（`spec:runtime:sdk:.:tests`）。没有装 `mujoco` 的环境里需要它的测试跳过。

## 在哪里验证过，哪里没有

验证过（2026-11-26，这台 Mac，MuJoCo 4.0.1）：

- 八个真实的包：解开没有任何提示、按档案的姿态建模、运行 2 秒关节不漂、机身停在站立高度。
- 浏览器（Chromium）里从对话框逐个添加八个型号：启动、连接、上报的品牌型号和档案一致、实时状态里出现画面、机器狗和人形处于站立状态。
- 旧仿真机器人的删除：用测试里摆好的旧数据（通用四足、issue #51 的 MuJoCo 机器人、一台真机记录、各自的操作记录）。

没有验证的：

- **和真机的对比**；各型号的站立姿态只是看起来站着（宇树、云深处的角度取自各家强化学习仓库的默认值，CS1 是我按 URDF 估的，L2-U、A3、Z1 用 URDF 零位）。
- **用户正式数据上的那一次删除**：只在测试数据上跑过；装上这一版后第一次打开会执行。
- CS1 的真实外形（没有网格文件）；Go2、Z1 的颜色。
- 示例技能在每个型号上的运行（只在编造的模型上有测试）；多台 MuJoCo 机器人同时运行；长时间运行。
- Intel Mac、Linux、没有 OpenGL 的机器；别的电脑上打包（没有不公开的那个文件夹时应当少两款，这条路径只有单元测试）。

已知的限制：

- 实时状态里的画面离机器人比较远（镜头距离是 issue #51 按有物体的环境定的）。
- 仿真机器人的类型在记录里是标准的本体类型（轮足和四足都是 `quadruped`，双足是 `humanoid`）；「轮足」只体现在档案和图片上。
- 换了新版的模型包后，已经添加的仿真机器人仍用它添加时解开的那一份；要用新的，删除后重新添加。
