# 用 MuJoCo 仿真机器人应用（issue #51）

为一个机器人应用生成仿真环境：机器人用它所属品牌型号的 URDF（[MODEL_LIBRARY.md](MODEL_LIBRARY.md)），环境由工程助手按应用的步骤、任务说明和这个品牌型号已确认的资料写出，人看过确认后，在里面启动一台仿真机器人，应用和技能照常在它上面运行。物理由 MuJoCo 计算。

> 状态：代码和测试完成，在本机用**编造的**四足和机械臂模型跑通过（Chromium、WebKit、打包后的后台）。**没有用任何真实厂家的 URDF 试过，没有和真机对比过。** 见最后一节。

用户在 issue 会话里定的事（2026-11-26）：

1. **应用能在仿真里跑**：MuJoCo 机器人作为一种仿真机器人接入，应用和技能在上面运行。机械臂的关节、夹爪走物理；底盘和四足的“按速度行走、导航”**直接移动机身，不模拟腿的步态**；碰撞照常计算。URDF 里没有行走控制器，真实步态要每种机器人自己的控制程序，这一版不做。
2. **环境由工程助手生成，人确认**后才能用；可以重新生成。也可以不用助手，先用只有地面的空场地。

## 仿真里有什么，没有什么

| | 这一版 |
|---|---|
| 机器人的形状、质量、关节和上限 | 来自 URDF |
| 关节 | 每个可动关节一个位置执行器（增益按 URDF 的力矩上限定）；`mimic` 关节跟随主关节 |
| 机械臂（本体类型 manipulator） | 底座固定在环境给的起点。能力和内置的仿真机械臂相同：`arm.joint_position`、`joint.state`，有夹爪关节时还有 `gripper.position`、`gripper.state`；关节数、名字、上下限、最大速度取自 URDF |
| 四足、轮式、人形 | 机身架在四个自由度上（x、y、高度、朝向），速度指令直接推动它。能力和内置的仿真四足相同：`base.velocity`、`base.navigate_to`、`base.navigate_to_place`（环境里有地点时）、`base.explore`、`legged.posture`（四足和人形）、里程计、定位、电量。**腿和轮子不产生推进力**；站、坐、趴只改变机身高度 |
| 和环境的碰撞 | 机器人会被固定的物体挡住、会推动可移动的物体；被挡住（走不动，或转不过去）超过 1.5 秒的导航失败，并说出被什么挡住；正在被推动的物体不算挡住，推得很慢的重物会一直推到技能自己的时限；机械臂 2 秒内到不了目标的动作失败，并说出碰到了什么；夹爪合不拢且碰着物体时报告“已抓住” |
| 机器人和地面、机器人自身各部分 | **不碰撞**。机器人的重量被抵消，不会下沉或翻倒 |
| 传感器 | 没有相机、雷达、IMU 噪声；IMU、电量是和内置仿真一样的简单值 |
| 夹爪关节 | 靠名字认（含 finger / gripper / claw / jaw / hand），或跟随这样的关节；上限那一端算“张开” |
| 其他本体类型（无人机等） | 不支持，会明说 |

仿真机器人上报的品牌和型号就是应用的品牌型号，并且 `simulated: true`：同品牌型号的应用可以在它上面检查和试运行，结果**不算现场证据，不能用来发布**（和内置仿真机器人的规则相同）。

## 流程

```
应用交付清单 →「仿真环境」
   │ sim/mujoco/overview              品牌型号、本体类型、资料库里可用的机器人模型（没有就说缺什么）
   ├─「用空场地」 sim/mujoco/empty
   └─「让工程助手生成环境」 sim/mujoco/generate   （要勾选同意；要有可用的模型算力）
        │  preparing    从云端取机器人模型（核对 SHA-256）、解开
        │  generating   工程助手读 SIMULATION.json，写 scene.json
        │  checking     子进程里用 MuJoCo 建一次、跑 1 秒、画预览
        ▼
   awaiting_review ──「确认可用」→ confirmed ──「启动仿真机器人」→ sims/mujoco
                   └─「不用这个」→ rejected          失败 → failed（写明原因）
```

- 一个应用同时只做一个环境；应用退出时还没做完的，下次打开标为失败，不续做。
- 确认过的环境可以启动多台仿真机器人；有机器人在用的环境不能删除或改成不用。
- 仿真机器人和内置的一样出现在「我的机器人」里（卡片写“MuJoCo 仿真 · 品牌型号”），关闭、重启、删除、急停都是原有的路径；应用关闭后再打开会自动恢复。

## 环境描述

工程助手写的不是 MuJoCo 的 XML，而是一个小 JSON（`neuxmind.mujoco-scene.v1`，`brainos/mujoco_scene.py`）。XML 可以引用文件、插件和别的 XML，这个格式不能：

```json
{"schema": "neuxmind.mujoco-scene.v1", "summary": "一两句说明",
 "floor": {"size": [6, 6]},
 "robot": {"x": 0, "y": 0, "yaw": 0},
 "objects": [{"name": "箱子", "shape": "box", "size": [0.4, 0.4, 0.4], "pos": [1.5, 0.3, 0.2],
              "yaw": 0, "color": "#b5651d", "movable": true, "mass": 2.0}],
 "places": [{"name": "门口", "x": 3.0, "y": 0.0}]}
```

- 形状只有 `box`、`sphere`、`cylinder`、`capsule`；单位是米、千克、弧度；`pos` 的 z 是物体中心离地的高度。
- 最多 200 个物体、50 个地点；每个数字有范围；名称 1–40 个字（字母、数字、汉字、空格、下划线、连字符），不能重复；不认识的字段一律拒绝（`sim_scene_invalid`）。
- `places` 成为机器人的 `base.navigate_to_place` 能认的地点，在画面里是地上的蓝色圆片。
- 页面上把每样东西列成一行给人核对。

### 交给工程助手的内容

会话的工作目录里有 `SIMULATION.json`（不算产物）：应用的名字和每一步（技能、参数、完成条件）、任务的名字和说明、机器人（品牌型号、本体类型、站立高度、大小、关节名、它在仿真里怎么动）、这个品牌型号「可用」资料的确认结果（最多 8 份，每份最多 3000 字）、用户写的补充说明、格式和一个例子。

- 开始前要用户勾选同意（和其他工程会话一样，`authorization_required`）；没有可用的模型算力时当场拒绝。
- 它是一次普通的工程会话（「工程助手」页里能看到，卡片上有「查看过程」），不联网、只能写自己的工作目录、最长 12 分钟。它要执行本地命令时可能请求批准，在「查看过程」里处理。
- 它写出的东西是数据：`scene.json` 只按上面的格式读，是符号链接或超过 256 KB 就不读；没写出来、格式不对都只让这一个环境失败。

## 机器人模型到本机

`brainos/mujoco_model.py`，只用标准库：

1. 用送安装包的同一套下载（`package_fetch`）：续传、核对云端给的 SHA-256，下载的副本用完即删。
2. 解开到 `<工作区>/robot-models/<SHA-256>/`：`model.urdf`、`meshes/m<N>.<后缀>`、`model.json`。**不按包里的名字写文件**：只读以 `.urdf` 或网格后缀（`.stl`、`.obj`、`.msh`）结尾的普通成员，写到这里起的名字下；链接、设备文件、指向包外的名字、加密的成员都不读；单个文件和总量有上限（URDF 32 MiB、网格每个 256 MiB、合计 1 GiB、成员 10 万个）。
3. URDF 用拒绝 DOCTYPE 和实体的解析器读，改写后保存：网格引用指到 `meshes/` 下（按路径尾部匹配，所以 `visual/trunk.stl` 和 `collision/trunk.stl` 分得开）；找不到或读不了的网格所在的形状去掉并给出提示；ASCII 的 STL 转成二进制；去掉 `<gazebo>`、`<transmission>`、原有的 `<mujoco>` 节，写入这里固定的编译设置。
4. 同一份模型（同一个 SHA-256）只解一次。

## MuJoCo 在哪里运行

控制台自己的后台进程**从不导入 MuJoCo，也不解析网格文件**：

- **检查和预览**：`brainos/mujoco_tool.py`，一个子进程（打包后是后台程序自己带 `--nx-mujoco-tool` 再启动一次），180 秒不结束就杀掉。它建的就是仿真机器人启动时要建的那个本体，并按运行时的规则核对它的能力表（例如关节下限不小于上限的机械臂在这里就被拒绝），所以通过检查的环境能启动。它输出一行 JSON 和 `preview.png`。机器人模型没有任何碰撞形状时检查照样通过，但环境卡片上会写明“不会被任何东西挡住”。
- **仿真机器人**：端侧运行时带 MuJoCo 本体（`serve --adapter brainos.mujoco_body:body`），和内置仿真机器人一样是应用的子进程，应用退出它就退出。启动时它要再建一次模型，所以等它启动的时限是 240 秒（内置仿真是 45 秒）。设置在它的状态目录里的 `mujoco.json`。
- **画面**：本体里一个线程离屏渲染（macOS 用系统的 OpenGL，不开窗口），有人在看（页面每半秒取一次图）时大约每秒两帧，写成 `view/frame.png`；页面经 `GET /api/sims/frame?id=<机器人>` 取。同目录的 `state.json` 有机器人正碰着什么、物体在哪里。没有 OpenGL 的环境里画不出图，仿真照常，页面写明原因。
- 机器人模型是别人上传的文件，网格由 MuJoCo 的 C 代码解析。这发生在上面两个子进程里，不在后台进程里；子进程和应用同一个用户身份运行，没有另外的沙箱。资料库里的文件是本组织的人上传的，按这个信任程度对待。

打包：`requirements-runtime.txt` 多了 `mujoco-core==4.0.1`（不带查看器的引擎）和 `numpy==2.5.5`，`scripts/build_macos.py` 把它们收进后台程序（多约 22 MB），`--freeze-check` 多一步：打包后的后台从一个 URDF 建出环境并画出预览。许可证在 `licenses/python/mujoco-*`、`numpy-LICENSE`（MuJoCo 是 Apache-2.0，它的第三方声明原样附上）。**打包用的环境要先 `pip install -r requirements-build.txt`**，否则 `build_macos.py --check` 会报导入失败。没有装 `mujoco` 的环境里应用照常运行，仿真入口会说“这个安装里没有 MuJoCo 仿真引擎”，相关测试跳过。

## 接口

桌面端后台（`brainos/mujoco_sim.py`；环境属于任务，仿真机器人属于账号）：

| 路径（POST，JSON） | 作用 |
|---|---|
| `sim/mujoco/overview` `{application_id}` | 品牌型号、本体类型、可用的机器人模型或缺什么（`problem`）、这个应用的环境、在这些环境里的仿真机器人 |
| `sim/mujoco/generate` `{application_id, note?, data_authorized, model_id?}` | 让工程助手生成 |
| `sim/mujoco/empty` `{application_id}` | 空场地 |
| `sim/mujoco/scene` `{scene_id}` | 一个环境现在的状态 |
| `sim/mujoco/review` `{scene_id, decision: confirm \| reject}` | 人的决定 |
| `sim/mujoco/remove` `{scene_id}` | 删除 |
| `sims/mujoco` `{scene_id, name?}` | 在确认过的环境里启动一台仿真机器人 |
| `sims/view` `{robot_id}` | 正碰着什么、物体位置、仿真时间、出错信息 |

`GET /api/sims/frame?id=<机器人编号>` 和 `GET /api/sim/mujoco/preview?id=<环境编号>` 返回 PNG（要登录；只给自己工作区里的）。

错误码（说明在 `web/ux04_errors.js`）：`sim_no_product`、`sim_no_robot_model`、`sim_family_unsupported`、`sim_engine_missing`、`sim_model_invalid`、`sim_check_failed`、`sim_scene_busy`、`sim_agent_failed`、`sim_scene_invalid`、`sim_scene_not_ready`、`sim_scene_in_use`。

## 代码在哪里

| 位置 | 内容 |
|---|---|
| `brainos/mujoco_scene.py` | 环境描述的格式和检查（不依赖 MuJoCo） |
| `brainos/mujoco_model.py` | 机器人模型解开、URDF 改写（不依赖 MuJoCo） |
| `brainos/mujoco_build.py` | URDF + 环境 → MuJoCo 模型（`World`） |
| `brainos/mujoco_body.py` | 运行时的本体：`MujocoBase`、`MujocoArm`、画面线程 |
| `brainos/mujoco_tool.py` | 子进程里的检查和预览 |
| `brainos/mujoco_sim.py` | 控制台这一侧：环境记录、工程助手、确认、启动机器人、取画面 |
| `web/mujoco_sim.js`、`web/mujoco_sim.css` | 交付清单里的入口、「仿真环境」对话框、实时状态里的画面 |
| `desktop/launcher.py`、`scripts/build_macos.py` | `--nx-mujoco-tool`、打包 |

## 测试

```bash
python -m unittest tests.test_mujoco_model      # 环境格式、模型解开（不需要 mujoco）
python -m unittest tests.test_mujoco_body       # 本体：站立、行走、被墙挡住、推箱子、机械臂、夹爪（需要 mujoco）
python -m unittest tests.test_mujoco_sim        # 控制台一侧：模型、空场地、工程助手、确认、真的启动一台仿真机器人
node tests/model_library_logic.cjs              # 页面逻辑
python tests/mujoco_sim_browser_fixture.py CONFIG.json &    # 浏览器验收的夹具（真云端程序、替身助手、真运行时 + MuJoCo）
node tests/mujoco_sim_browser.cjs CONFIG.json               # BRAIN_BROWSER=webkit 换成 WebKit
python scripts/build_macos.py --freeze-check                # 打包后的后台：内置仿真 + MuJoCo 建环境、画预览
```

`PYTHONPATH` 和其他桌面端测试相同（`spec:runtime:sdk:.:tests`）。

## 在哪里验证过，哪里没有

验证过（2026-11-26，这台 Mac，MuJoCo 4.0.1）：

- 编造的四足（一个躯干、四条两关节的腿）和机械臂（三个关节、两指夹爪）：站立高度正确、按速度行走的距离和速度对、被墙挡住并报告、推动箱子、趴下后不能走、机械臂到位、被柱子挡住并报告、夹爪两指同步、停止后保持不动。
- 从资料库到仿真的整条路：上传带网格的压缩包 → 云端解析 → 人确认 → 取到本机 → 空场地和助手写的环境 → 确认 → 启动仿真机器人 → 页面里看到画面（Chromium 和 WebKit）。助手是按脚本回答的替身。
- 打包后的后台能建出环境并画出预览。

没有验证的：

- **真实厂家的 URDF**：带大量网格、`.dae` 网格、非零初始姿态才站得住的四足、并联或闭链结构、很轻或很重的连杆。零位不是站姿的机器人会以零位“站”着。
- **真的大模型写的环境**：格式检查和建模检查都在，但写得合不合用只有人看。
- 仿真和真机的差别：没有对比过；这一版的行走本来就不是物理的。
- 打包后的应用里由 MuJoCo 本体跑的整台仿真机器人（打包检查只跑了检查工具和内置仿真机器人）；DMG 安装后的应用。
- Intel Mac、Linux、没有 OpenGL 的机器。
- 长时间运行（内存、帧率）、多台 MuJoCo 机器人同时运行。

已知的限制：

- 删除任务时，它的环境记录随任务删除，但 `<工作区>/mujoco-scenes/<编号>/` 里的预览图和 `robot-models/` 里解开的模型不清理。
- 环境确认后不能改；要改就重新生成。
- 仿真机器人启动后，换了资料库里的机器人模型不影响它；要用新模型，重新做环境。
