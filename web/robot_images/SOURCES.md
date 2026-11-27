# 机器人图片的来源

`web/robot_catalog.js` 按品牌型号引用这里的图片（issue #53）。

## 型号图片（`*.webp`）

除下面单独列出的一张外，来自 [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie)（提交 `eea4c7431b63da8278b0ff31638e0b0e941cec0e`）`assets/` 目录下各模型的预览图，是开源机器人模型的渲染图，不是厂商的产品照片。缩到 360×360 并转成 WebP，没有做别的改动。各模型的许可见该仓库对应目录的 `LICENSE`：

| 文件 | Menagerie 目录 | 许可 |
| --- | --- | --- |
| agilex-piper | agilex_piper | MIT |
| anybotics-anymal-c | anybotics_anymal_c | BSD-3-Clause |
| apptronik-apollo | apptronik_apollo | Apache-2.0 |
| arx-l5 | arx_l5 | BSD-3-Clause |
| booster-t1 | booster_t1 | Apache-2.0 |
| boston-dynamics-spot | boston_dynamics_spot | BSD-3-Clause |
| fanuc-crx-5ia | fanuc_crx5ia | Apache-2.0 |
| flexiv-rizon-4 | flexiv_rizon4 | Apache-2.0 |
| fourier-n1 | fourier_n1 | Apache-2.0 |
| franka-panda | franka_emika_panda | Apache-2.0 |
| kinova-gen3 | kinova_gen3 | BSD-3-Clause |
| kuka-iiwa-14 | kuka_iiwa_14 | BSD-3-Clause |
| pal-talos | pal_talos | Apache-2.0 |
| pndbotics-adam-lite | pndbotics_adam_lite | MIT |
| rethink-sawyer | rethink_robotics_sawyer | Apache-2.0 |
| robotis-op3 | robotis_op3 | Apache-2.0 |
| so-101 | robotstudio_so101 | Apache-2.0 |
| so-arm100 | trs_so_arm100 | Apache-2.0 |
| trossen-vx300s | trossen_vx300s | BSD-3-Clause |
| trossen-wx250s | trossen_wx250s | BSD-3-Clause |
| ufactory-lite-6 | ufactory_lite6 | BSD-3-Clause |
| ufactory-xarm-7 | ufactory_xarm7 | BSD-3-Clause |
| unitree-a1 | unitree_a1 | BSD-3-Clause |
| unitree-g1 | unitree_g1 | BSD-3-Clause |
| unitree-go1 | unitree_go1 | BSD-3-Clause |
| unitree-go2 | unitree_go2 | BSD-3-Clause |
| unitree-h1 | unitree_h1 | BSD-3-Clause |
| unitree-z1 | unitree_z1 | BSD-3-Clause |
| universal-robots-ur10e | universal_robots_ur10e | BSD-3-Clause |
| universal-robots-ur5e | universal_robots_ur5e | BSD-3-Clause |

另有两张不是来自 Menagerie：

| 文件 | 来源 |
| --- | --- |
| zsibot-l2u | 智身科技 钢镚 L2-U（轮足）。由厂商提供的 URDF 模型（L2W Ultra 轮足环视版 V4）渲染的装配预览图，去掉背景后缩到 360×360；模型文件本身不在本仓库里 |
| deeprobotics-lite3 | 云深处科技 绝影 Lite3（issue #54）。本项目用 [deep_robotics_model](https://github.com/DeepRoboticsLab/deep_robotics_model)（BSD-3-Clause，提交 `d1a3900e7faf7eda9e28f20715b42e871dbfd295`）的 URDF 模型在 MuJoCo 里渲染的图，不是厂商的产品照片；模型文件本身不在本仓库里 |
| deeprobotics-m20 | 云深处科技 山猫 M20（issue #54）。本项目用 [deep_robotics_model](https://github.com/DeepRoboticsLab/deep_robotics_model)（BSD-3-Clause，提交 `d1a3900e7faf7eda9e28f20715b42e871dbfd295`）的 URDF 模型在 MuJoCo 里渲染的图，不是厂商的产品照片；模型文件本身不在本仓库里 |
| agibot-a3 | 智元机器人 A3（issue #54）。本项目用 [A3-A3U-robot-model](https://github.com/AgibotTech/A3-A3U-robot-model)（木兰宽松许可证第 2 版，提交 `589f508ff357447c610a3f3004419035ddc8f153`）的 URDF 模型在 MuJoCo 里渲染的图，不是厂商的产品照片；模型文件本身不在本仓库里 |
| xingji-cs1 | 星玑科技 CS1（四足）。取自厂商《四足机器人二次开发版本开发手册》（2025-12-27）里的整机图，去掉背景和标注后缩到 360×360；手册和 CAD 文件不在本仓库里 |

同系列里没有单独图片的型号用了同系列的图（FR3 用 Panda，xArm 6 用 xArm 7，iiwa 7 用 iiwa 14，CRX-10iA 用 CRX-5iA，Rizon 4s 用 Rizon 4）。

## 本体类型示意图（`type-*.svg`）

本项目自己画的线条图：清单里没有图片的型号、清单之外的品牌型号，按本体类型显示其中一张。

## 加一张图片

把 360×360、透明或浅色底的 WebP 放进这个目录（文件名只用小写字母、数字和连字符），在 `web/robot_catalog.js` 对应型号的第三个参数写上文件名（不带扩展名），并在上表加一行来源和许可。
